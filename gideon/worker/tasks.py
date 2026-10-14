"""Queue worker tasks for verification, recovery, corpus work, and index builds."""

import asyncio
import logging
from collections.abc import Mapping
from contextlib import suppress
from typing import Final

import procrastinate
from procrastinate.exceptions import UniqueViolation

import gideon.host.cas
import gideon.worker.dense

from . import agreement as citation_agreement
from . import caselaw as document_ingest
from . import lexical as lexical_build
from . import overruled as listed_overrulings
from . import staging
from .fetch import (
    FETCH_QUEUE,
    FETCH_TASK,
    SNAPSHOTS_ROOT,
    FetchFailure,
    transfer,
    write_job_failure,
)
from .staging import STAGE_QUEUE, STAGE_TASK

VERIFY_TASK: Final = "gideon.worker.tasks.verify"
VERIFY_QUEUE: Final = "verify"
RECOVERY_TASK: Final = "gideon.worker.tasks.retry_stalled_jobs"
RECOVERY_QUEUE: Final = "maintenance"
# exempt: one-minute recovery bounds the time a stopped job waits to run again.
RECOVERY_CRON: Final = "* * * * *"
RECOVERY_LOCK: Final = "retry_stalled_jobs"
# exempt: thirty seconds allows several missed ten-second worker heartbeats.
STALLED_SECONDS: Final = 30

_LOGGER = logging.getLogger(__name__)


async def verify(hold_seconds: float = 0) -> None:
    """Hold one queue job open when requested by an operator proof."""

    if hold_seconds:
        await asyncio.sleep(hold_seconds)


async def retry_stalled_jobs(context: procrastinate.JobContext, timestamp: int) -> None:
    """Return jobs held by stalled workers to the queue."""

    del timestamp
    stalled = await context.app.job_manager.get_stalled_jobs(
        seconds_since_heartbeat=STALLED_SECONDS
    )
    for job in stalled:
        try:
            await context.app.job_manager.retry_job(job)
        except UniqueViolation:
            # A job under the same queueing lock already waits, so this one
            # stays doing and is retried by a later pass once that one has run;
            # the rest of this pass goes on.
            _LOGGER.info("stalled job %d waits for its queueing lock", job.id)


def fetch(
    context: procrastinate.JobContext, destination: str, url: str, form: str,
) -> None:
    """Run a file transfer outside the worker's event loop."""

    job_id = context.job.id
    if job_id is None:
        raise FetchFailure("invalid")
    try:
        transfer(SNAPSHOTS_ROOT, destination, url, form, job_id)
    except FetchFailure as failure:
        write_job_failure(SNAPSHOTS_ROOT, destination, form, job_id, failure)
        raise


def stage(
    context: procrastinate.JobContext, label: str, snapshot: str,
    courts: list[str], inputs: dict[str, dict[str, str]],
) -> None:
    """Stage a pinned snapshot outside the worker's event loop."""

    job_id = context.job.id
    if job_id is None:
        raise staging.StageFailure("invalid")
    try:
        staging.stage(
            SNAPSHOTS_ROOT, staging.WORK_ROOT, label, snapshot, courts, inputs, job_id,
        )
    except staging.StageFailure as failure:
        staging.write_job_failure(staging.WORK_ROOT, label, snapshot, job_id, failure)
        raise


def caselaw(
    context: procrastinate.JobContext, label: str, snapshot: str,
    court: str, limit: int | None = None,
    courts: Mapping[str, Mapping[str, object]] | None = None,
    *, pipeline: str,
) -> None:
    """Ingest one staged court outside the worker's event loop."""

    job_id = context.job.id
    if job_id is None:
        raise document_ingest.CaselawFailure("invalid")
    try:
        document_ingest.ingest(
            SNAPSHOTS_ROOT, staging.WORK_ROOT, gideon.host.cas.ROOT,
            label, snapshot, court, limit, job_id, document_ingest.PsycopgRecord(),
            courts=courts, pipeline=pipeline,
        )
    except document_ingest.CaselawFailure as failure:
        document_ingest.write_job_failure(
            staging.WORK_ROOT, label, snapshot, court, job_id, failure,
        )
        raise


def agreement(
    context: procrastinate.JobContext, label: str, snapshot: str,
    court: str, input: dict[str, str],
) -> None:
    """Measure one staged court outside the worker's event loop."""

    job_id = context.job.id
    if job_id is None:
        raise citation_agreement.AgreementFailure("invalid")
    try:
        citation_agreement.agreement(
            SNAPSHOTS_ROOT, staging.WORK_ROOT, label, snapshot, court,
            input, job_id, citation_agreement.PsycopgEdges(),
        )
    except citation_agreement.AgreementFailure as failure:
        citation_agreement.write_job_failure(
            staging.WORK_ROOT, label, snapshot, court, job_id, failure,
        )
        raise


def overruled(
    context: procrastinate.JobContext, label: str, snapshot: str,
    caselaw_snapshot: str, court: str, input: dict[str, str],
) -> None:
    """Resolve the captured list outside the worker's event loop."""

    job_id = context.job.id
    if job_id is None:
        raise listed_overrulings.OverruledFailure("invalid")
    try:
        listed_overrulings.overruled(
            SNAPSHOTS_ROOT, staging.WORK_ROOT, label, snapshot, caselaw_snapshot,
            court, input, job_id, listed_overrulings.PsycopgRecord(),
        )
    except listed_overrulings.OverruledFailure as failure:
        listed_overrulings.write_job_failure(
            staging.WORK_ROOT, label, snapshot, court, job_id, failure,
        )
        raise


def lexical(
    context: procrastinate.JobContext, name: str, collection: str, label: str,
    snapshot: str, courts: Mapping[str, Mapping[str, object]], pipeline: str,
    embedding_space: str, inputs_hash: str,
) -> None:
    """Build a lexical generation outside the worker's event loop."""

    job_id = context.job.id
    if job_id is None:
        raise lexical_build.LexicalFailure("invalid")
    lexical_build.build(
        {
            "name": name, "collection": collection, "label": label,
            "snapshot": snapshot, "courts": courts, "pipeline": pipeline,
            "embedding_space": embedding_space, "inputs_hash": inputs_hash,
        },
        job_id, lexical_build.PsycopgGenerations(),
        lexical_build.HttpxStore(), gideon.host.cas.ROOT,
        work_root=staging.WORK_ROOT,
    )


def dense(
    context: procrastinate.JobContext, name: str, collection: str, label: str,
    snapshot: str, courts: Mapping[str, Mapping[str, object]], pipeline: str,
    embedding_space: str, inputs_hash: str, served_model: str, dimensions: int,
    limit: int | None = None,
) -> None:
    """Build a dense generation outside the worker's event loop."""

    job_id = context.job.id
    if job_id is None:
        raise gideon.worker.dense.DenseFailure("invalid")
    record = gideon.worker.dense.PsycopgGenerations()
    store = None
    try:
        store = gideon.worker.dense.HttpxVectorStore()
        embedder = gideon.worker.dense.HttpxEmbedder(served_model, dimensions)
    except BaseException:
        # A constructor refusal must survive cleanup of already-created clients.
        for client in (record, store):
            if client is not None:
                with suppress(Exception):
                    client.close()
        raise
    gideon.worker.dense.build(
        {
            "name": name, "collection": collection, "label": label,
            "snapshot": snapshot, "courts": courts, "pipeline": pipeline,
            "embedding_space": embedding_space, "inputs_hash": inputs_hash,
            "served_model": served_model, "dimensions": dimensions, "limit": limit,
        },
        job_id, record, store, embedder, gideon.worker.dense.NoBackOff(),
        gideon.host.cas.ROOT, work_root=staging.WORK_ROOT,
    )


def register_tasks(app: procrastinate.App) -> None:
    """Register verification, recovery, and synchronous corpus and index jobs."""

    app.task(name=VERIFY_TASK, queue=VERIFY_QUEUE)(verify)
    task = app.task(
        name=RECOVERY_TASK,
        queue=RECOVERY_QUEUE,
        queueing_lock=RECOVERY_LOCK,
        pass_context=True,
    )(retry_stalled_jobs)
    app.periodic(cron=RECOVERY_CRON)(task)
    app.task(
        name=FETCH_TASK,
        queue=FETCH_QUEUE,
        pass_context=True,
        retry=False,
    )(fetch)
    app.task(
        name=STAGE_TASK,
        queue=STAGE_QUEUE,
        pass_context=True,
        retry=False,
    )(stage)
    app.task(
        name=document_ingest.CASELAW_TASK,
        queue=document_ingest.CASELAW_QUEUE,
        pass_context=True,
        retry=False,
    )(caselaw)
    app.task(
        name=citation_agreement.AGREEMENT_TASK,
        queue=citation_agreement.AGREEMENT_QUEUE,
        pass_context=True,
        retry=False,
    )(agreement)
    app.task(
        name=listed_overrulings.OVERRULED_TASK,
        queue=listed_overrulings.OVERRULED_QUEUE,
        pass_context=True,
        retry=False,
    )(overruled)
    app.task(
        name=lexical_build.LEXICAL_TASK,
        queue=lexical_build.LEXICAL_QUEUE,
        pass_context=True,
        retry=False,
    )(lexical)
    app.task(
        name=gideon.worker.dense.DENSE_TASK,
        queue=gideon.worker.dense.DENSE_QUEUE,
        pass_context=True,
        retry=False,
    )(dense)
