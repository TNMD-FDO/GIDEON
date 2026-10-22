"""Build a collection's near-duplicate clusters, indexes, and risk states, and append its report.

Production builds check smoke evidence after preconditions and before generation.
Samples record their given gold before guard, reserve, both indexes, gold,
treatment, and report across the corpus and sibling records.
"""

import argparse
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from gideon.host import (
    backuplock,
    caselaw,
    engine,
    models,
    report,
    secrets,
    site,
    stack,
    staging,
    worker,
)
from gideon.host.corpus import artifacts, record, snapshots
from gideon.host.corpus.lockfile import Lockfile
from gideon.host.corpus.sources import SOURCES, SourceDefinition
from gideon.host.index import (
    buildreport,
    dense,
    dupes,
    generations,
    guards,
    lexical,
    reserve,
    smoke,
    treatment,
)
from gideon.host.index import record as indexrecord
from gideon.host.index.sample import SampleInputs, sample_inputs_hash, sample_name
from gideon.host.render.ci import CI_QDRANT_DATA_ROOT, CI_ROOT, CI_SECRETS_DIR
from gideon.host.render.engine import EMBED
from gideon.host.render.opensearch import OPENSEARCH_SERVICE_NAME
from gideon.host.render.qdrant import QDRANT_DATA_ROOT, QDRANT_SERVICE_NAME
from gideon.host.render.worker import (
    CLUSTER_BUDGET_SECONDS,
    DENSE_LOCK_PREFIX,
    DUPES_LOCK_PREFIX,
    OVERRULED_SOURCE,
    WORK_ROOT,
    WORKER_SERVICE_NAME,
)
from gideon.host.report import Problem, StageResult
from gideon.host.sysio import Host, LockingHost, PathLike, RealHost, WritableBytesHost

COMMAND_PATH = "index build"


class IndexHost(WritableBytesHost, LockingHost, Protocol):
    """The artifact, subprocess, and lock operations needed by an index build."""


@dataclass(frozen=True, slots=True)
class _JobResult:
    """A job stage's exit code and whether this run deferred its job."""

    exit_code: int
    deferred: bool


@dataclass(frozen=True, slots=True)
class _Preconditions:
    """The build inputs, guards, treatment capture, and optional generation resume."""

    inputs: dense.DenseInputs
    treatment_snapshot: str | None
    chunk_guards: models.ChunkGuards
    resume_generation: str | None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _refuse(name: str, problem: Problem, *, command_path: str | None = None) -> int:
    if command_path is not None and report.command(command_path) not in problem.fix:
        problem = Problem(problem.problem, snapshots.retry_fix(problem.fix, command_path=command_path))
    row = StageResult(name, False, problem.problem, problem.fix)
    if name == "preconditions":
        print(report.stage_line(row), file=sys.stderr)
    else:
        report.print_stage(row)
    return 1


def _service_preconditions(
    host: IndexHost, rendered_dir: PathLike, service: str, words: str, command_path: str,
) -> Problem | None:
    logs = stack.logs_fix(rendered_dir, service)
    declared = stack.declared_services(host, rendered_dir)
    if isinstance(declared, Problem):
        return Problem(declared.problem, f"{declared.fix} Read {logs}, then run {report.command(command_path)} again.")
    if service not in declared:
        return Problem(f"rendered stack has no {words}", f"Run {report.command('apply')}, read {logs}, then run {report.command(command_path)} again.")
    tool_fix = f"Restore Docker Compose, read {logs}, then run {report.command(command_path)} again."
    try:
        result = host.run(stack.compose_argv(rendered_dir, "ps", "--all", "--format", "json"))
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"{words} state could not be read ({type(exc).__name__})", tool_fix)
    if result.returncode == 127:
        return Problem(f"{words} state command is unavailable (exit 127)", tool_fix)
    logs_fix = f"Run {logs}, then run {report.command(command_path)} again."
    if result.returncode != 0:
        return Problem(f"{words} state read failed (exit {result.returncode})", logs_fix)
    rows = stack.parse_ps(result.stdout)
    if rows is None:
        return Problem(f"{words} service state is invalid", logs_fix)
    if not any(row.get("Service") == service
               and row.get("State") == "running" and row.get("Health") == "healthy"
               for row in rows):
        return Problem(f"{words} is not running and healthy", logs_fix)
    return None


def check_services(
    host: IndexHost, rendered_dir: PathLike, command_path: str,
    *, embed_dir: PathLike | None = None,
) -> Problem | None:
    """Require the lexical store, vector store, and embedding server to be healthy."""

    for service, words in ((OPENSEARCH_SERVICE_NAME, "lexical store"),
                           (QDRANT_SERVICE_NAME, "vector store"), (EMBED.service_name, EMBED.words)):
        service_dir = embed_dir if service == EMBED.service_name and embed_dir is not None else rendered_dir
        problem = _service_preconditions(host, service_dir, service, words, command_path)
        if problem is not None:
            return problem
    return None


def _collection_scope(
    loaded: artifacts.CorpusArtifacts, lockfile: Lockfile,
    collection: generations.Collection, registry: Sequence[SourceDefinition],
    command_path: str,
) -> tuple[str, tuple[str, ...], Mapping[str, caselaw.CourtGeography]] | Problem:
    feeding = tuple(source for source in registry if collection.feeds(source))
    if len(feeding) != 1:
        return Problem("collection has no single feeding snapshot", f"Restore the source registry, then run {report.command(command_path)} again.")
    source = feeding[0]
    pin = lockfile.sources[source.name]
    courts = tuple(sorted(pin.courts or ()))
    geography = caselaw.court_geography(loaded.court_map, courts)
    if isinstance(geography, Problem):
        return geography
    return f"{source.name}-{pin.snapshot_date}", courts, geography


def _selected_profile(
    host: Host, checkout: Path, site_path: PathLike, command_path: str,
) -> models.HardwareProfile | Problem:
    loaded_site = site.load_site(Path(site_path), host=host)
    if loaded_site.errors:
        return Problem("; ".join(error.problem for error in loaded_site.errors),
                       snapshots.retry_fix("; ".join(dict.fromkeys(error.fix for error in loaded_site.errors)), command_path=command_path))
    assert loaded_site.config is not None
    model_lock = models.load_models_lock(checkout / "models.lock", host=host)
    if model_lock.errors:
        return Problem("; ".join(error.problem for error in model_lock.errors),
                       snapshots.retry_fix("; ".join(dict.fromkeys(error.fix for error in model_lock.errors)), command_path=command_path))
    assert model_lock.lock is not None
    return models.select_profile(model_lock.lock, loaded_site.config.hardware_profile)


def embed_target(
    host: IndexHost, checkout: Path, site_path: PathLike, command_path: str,
) -> engine._EmbedTarget | Problem:
    """Read the selected site's embedding pin and vector space."""

    profile = _selected_profile(host, checkout, site_path, command_path)
    if isinstance(profile, Problem):
        return profile
    target = engine._embed_target(profile)
    if isinstance(target, Problem):
        return Problem(target.problem, snapshots.retry_fix(target.fix, command_path=command_path))
    return target


def chunk_guards(
    host: Host, checkout: Path, site_path: PathLike, command_path: str,
) -> models.ChunkGuards | Problem:
    """Read the selected profile's chunk guards, which the guard row judges."""

    profile = _selected_profile(host, checkout, site_path, command_path)
    return profile if isinstance(profile, Problem) else profile.chunk_guards


def collection_inputs(
    loaded: artifacts.CorpusArtifacts, lockfile: Lockfile,
    collection: generations.Collection, registry: Sequence[SourceDefinition],
    target: engine._EmbedTarget, command_path: str,
) -> dense.DenseInputs | Problem:
    """Compute one collection's scope, generation identity, and embedding inputs."""

    scope = _collection_scope(loaded, lockfile, collection, registry, command_path)
    if isinstance(scope, Problem):
        return scope
    snapshot, courts, geography = scope
    space = target.space_id
    try:
        name = generations.generation_name(collection.name, lockfile.label, space)
    except ValueError:
        return Problem("generation name exceeds the store's grammar or byte limit",
                       f"Restore a valid embedding space id in models.lock, then run {report.command(command_path)} again.")
    return dense.DenseInputs(
        name, collection.name, lockfile.label, snapshot,
        courts, geography, lockfile.pipeline, space,
        generations.inputs_hash(lockfile, collection.name, space, sources=registry),
        target.served_name, target.dimensions,
    )


def treatment_snapshot(lockfile: Lockfile) -> str | None:
    """Name the treatment capture a lockfile pins, which the treatment stage reads."""

    pin = lockfile.sources.get(OVERRULED_SOURCE)
    return None if pin is None else f"{OVERRULED_SOURCE}-{pin.snapshot_date}"


def _preconditions(
    args: argparse.Namespace, host: IndexHost, rendered_dir: PathLike,
    checkout: Path, registry: Sequence[SourceDefinition], site_path: PathLike,
    command_path: str, *, sample: SampleInputs | None = None,
    production_dir: PathLike | None = None,
) -> _Preconditions | Problem:
    collection = next((item for item in generations.COLLECTIONS if item.name == args.collection), None)
    if collection is None:
        retry = COMMAND_PATH if sample is None else command_path
        return Problem("collection is not registered", f"Use a registered collection with {report.command(retry)}.")
    if args.lockfile is not None:
        problem = staging.label_problem(args.lockfile, command_path, subject="index build")
        if problem is not None:
            return problem
    if sample is not None:
        worker_stage = worker.worker_preconditions(host, rendered_dir, command_path=command_path)
        if not worker_stage.ok:
            return Problem(
                f"sibling {WORKER_SERVICE_NAME}: {worker_stage.detail}",
                f"Run sudo python3 -m tools.cistack up, read {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, "
                f"then run {report.command(command_path)} again.",
            )
    loaded, issue = artifacts.load_artifacts(
        host, rendered_dir, checkout, registry, command_path=command_path, check_worker=sample is None,
    )
    if issue is not None:
        return Problem(issue.detail, issue.fix)
    assert loaded is not None
    problem = check_services(host, rendered_dir, command_path, embed_dir=production_dir)
    if problem is not None:
        return problem
    lockfile = (
        next((item for item in loaded.directory.lockfiles if item.label == args.lockfile), None)
        if args.lockfile is not None else loaded.directory.newest
    )
    if lockfile is None:
        labels = ", ".join(item.label for item in loaded.directory.lockfiles) or "none"
        return Problem(
            "no matching committed corpus lockfile",
            f"Use a committed label from corpus/lockfiles/ ({labels}), or run {report.command('corpus cut')}, "
            f"then run {report.command(command_path)} again.",
        )
    problem = caselaw.pipeline_problem(lockfile.pipeline, command_path)
    if problem is not None:
        return problem
    # Validate the scope before the record and model reads, in the command's order.
    scope = _collection_scope(loaded, lockfile, collection, registry, command_path)
    if isinstance(scope, Problem):
        return scope
    rows = record.read_lockfiles(host, production_dir or rendered_dir, command_path=command_path)
    if isinstance(rows, Problem):
        return rows
    row = next((item for item in rows if item.label == lockfile.label), None)
    if row is None:
        return Problem(
            f"lockfile {lockfile.label} is not recorded",
            f"Run {report.command(f'corpus install {lockfile.label}')} or {report.command('corpus cut')}, "
            f"then run {report.command(command_path)} again.",
        )
    superseded = None
    if row.state == "superseded":
        retry = f"{COMMAND_PATH} {collection.name}" if sample is None else command_path
        superseded = Problem(
            f"lockfile {lockfile.label} is superseded",
            f"Use a current lockfile with {report.command(retry)}, then retry.",
        )
        if sample is not None:
            return superseded
    target = embed_target(host, checkout, site_path, command_path)
    if isinstance(target, Problem):
        return target
    guarded = chunk_guards(host, checkout, site_path, command_path)
    if isinstance(guarded, Problem):
        return guarded
    inputs = collection_inputs(loaded, lockfile, collection, registry, target, command_path)
    if isinstance(inputs, Problem):
        return inputs
    resume_generation = None
    if superseded is not None:
        generation = indexrecord.read_generation_by_inputs(
            host, rendered_dir, inputs.collection, inputs.inputs_hash, command_path=command_path,
        )
        if isinstance(generation, Problem):
            return generation
        # A failed row is the resume's own retry, as an unsuperseded build retries it.
        if generation is None or generation.state not in ("building", "failed") or lockfile.label not in generation.lockfiles:
            return superseded
        resume_generation = generation.gen_id
    if sample is not None:
        name = sample_name(inputs.name)
        if lexical._name_problem(name, command_path) is not None:
            return Problem("generation name exceeds the store's grammar or byte limit",
                           f"Restore a valid embedding space id in models.lock, then run {report.command(command_path)} again.")
        inputs = replace(
            inputs, name=name, sample=sample,
            inputs_hash=sample_inputs_hash(inputs.inputs_hash, lockfile.label, sample.fill_per_million),
        )
    return _Preconditions(inputs, treatment_snapshot(lockfile), guarded, resume_generation)


def _guard(
    host: IndexHost, rendered_dir: PathLike, inputs: generations.GenerationInputs,
    chunk_guards: models.ChunkGuards, command_path: str, *, corpus_dir: PathLike | None = None,
) -> int:
    # _collection_scope names the snapshot <source>-YYYY-MM-DD; the projection binds both halves.
    source, snapshot_date = inputs.snapshot[:-11], inputs.snapshot[-10:]
    projection = guards.read_chunk_projection(
        host, rendered_dir if corpus_dir is None else corpus_dir, source=source, snapshot_date=snapshot_date,
        courts=inputs.courts, pipeline=inputs.pipeline, command_path=command_path,
    )
    if isinstance(projection, Problem):
        return _refuse("guard", projection)
    resident = guards.read_resident(
        host, rendered_dir, candidate=inputs.name, command_path=command_path,
    )
    if isinstance(resident, Problem):
        return _refuse("guard", resident)
    verdict = guards.judge_guard(projection, chunk_guards, resident, candidate=inputs.name)
    margin = f"{verdict.ceiling}/{verdict.refusal}"
    figures = (
        f"{inputs.name}: ready {projection.ready}, chunked {projection.chunked}, "
        f"chunks {projection.chunks}, projected chunks {projection.projected}; "
        f"build refusal {verdict.refusal}, serving ceiling {verdict.ceiling}; "
        f"{guards.counted_text(verdict.counted)}; margin {margin}; "
        f"{verdict.resident} + {projection.projected} × {margin} ≤ serving ceiling {verdict.ceiling}"
    )
    if verdict.kind == "refused":
        live = next((row.name for row in verdict.counted if row.state == "live"), None)
        return _refuse("guard", Problem(
            f"{figures}; {verdict.reason}",
            guards.resident_fix(verdict.counted, live=live, command_path=command_path)
            + f"Make a smaller lockfile with {report.command('corpus cut')}, or select a larger "
            "hardware_profile in /etc/gideon/site.yaml; the guards derive from the profile's "
            "RAM, as docs/runbooks/release-files.md §4 says; then run "
            f"{report.command(command_path)} again.",
        ))
    detail = f"skipped: {verdict.reason}; {figures}" if verdict.kind == "skipped" else figures
    report.print_stage(StageResult("guard", True, detail, ""))
    return 0


def _reserve(
    host: IndexHost, rendered_dir: PathLike, inputs: generations.GenerationInputs,
    command_path: str,
) -> int:
    promoted = indexrecord.read_promoted(host, rendered_dir, command_path=command_path)
    if isinstance(promoted, Problem):
        return _refuse("reserve", promoted)
    measured = reserve.read_free_bytes(host)
    if isinstance(measured, Problem):
        return _refuse("reserve", Problem(
            measured.problem, snapshots.retry_fix(measured.fix, command_path=command_path),
        ))
    _, free = measured
    live = tuple(row for row in promoted if row.collection == inputs.collection)
    if not live:
        report.print_stage(StageResult(
            "reserve", True,
            f"skipped: no promoted generation for {inputs.collection}; reserve not judged; "
            f"/data free {free} bytes", "",
        ))
        return 0
    verdict = reserve.judge(free, sum(row.bytes for row in live))
    detail = reserve.describe(verdict)
    if not verdict.build_cleared:
        return _refuse("reserve", Problem(
            detail, f"Free space under /data as {reserve.RUNBOOK_ROW} says, then run "
            f"{report.command(command_path)} again.",
        ))
    report.print_stage(StageResult("reserve", True, detail, ""))
    return 0


def _cluster(
    host: IndexHost, rendered_dir: PathLike, inputs: generations.GenerationInputs,
    command_path: str, sleep: Callable[[float], None], monotonic: Callable[[], float],
) -> int:
    retry = command_path if inputs.sample is not None else None
    job = lexical.live_job(host, rendered_dir, f"{DUPES_LOCK_PREFIX}{inputs.name}",
                          prefix=DUPES_LOCK_PREFIX, command_path=command_path)
    if isinstance(job, Problem):
        return _refuse("cluster", job, command_path=retry)
    rejoined = job is not None
    if job is None:
        job = dupes.defer_dupes(host, rendered_dir, inputs, command_path=command_path)
    if isinstance(job, Problem):
        return _refuse("cluster", job, command_path=retry)
    report.print_stage(StageResult("cluster", True, f"{inputs.name}: {'rejoined' if rejoined else 'deferred'} job {job}", ""))
    heartbeat_at = monotonic()
    while True:
        outcome = dupes.read_dupes(host, rendered_dir, job, inputs.name, command_path=command_path)
        if isinstance(outcome, Problem):
            return _refuse("cluster", outcome, command_path=retry)
        if outcome.failure is not None:
            return _refuse("cluster", outcome.failure, command_path=retry)
        now = monotonic()
        if outcome.done or now - heartbeat_at >= snapshots.HEARTBEAT_SECONDS:
            row = indexrecord.read_generation(host, rendered_dir, inputs.name, command_path=command_path)
            if isinstance(row, Problem):
                return _refuse("cluster", row, command_path=retry)
            if outcome.done and (row is None or row.clustered_at is None):
                return _refuse("cluster", Problem(
                    "succeeded clustering job has no recorded clustered_at",
                    f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, then run {report.command(command_path)} again.",
                ), command_path=retry)
            if row is None:
                return _refuse("cluster", Problem("generation progress row is missing", record.database_fix(rendered_dir, command_path)),
                               command_path=retry)
            counts = row.counts
            chunks, texts = counts.get("cluster_chunks", 0), counts.get("cluster_texts", 0)
            if outcome.done:
                if counts.get("cluster_reused", 0) == 1:
                    detail = f"{inputs.name}: clusters stand, {chunks} chunks, {texts} texts; clustered_at {row.clustered_at}"
                else:
                    flagged = counts.get("cluster_flagged", 0)
                    percent = 100 * flagged / chunks if chunks else 0.0
                    detail = (
                        f"{inputs.name}: clusters {counts.get('cluster_clusters', 0)} over "
                        f"{counts.get('cluster_grouped', 0)} chunks of {chunks} "
                        f"({counts.get('cluster_members', 0)} near-duplicate texts of {texts}), "
                        f"boilerplate {counts.get('cluster_boilerplate', 0)} clusters over {flagged} chunks "
                        f"({percent:.1f} % of {chunks}); {counts.get('cluster_seconds', 0)} s of the "
                        f"{CLUSTER_BUDGET_SECONDS} s budget; clustered_at {row.clustered_at}"
                        + ("; nothing to cluster" if not chunks else "")
                    )
                report.print_stage(StageResult("cluster", True, detail, ""))
                if rejoined and inputs.sample is not None:
                    # A rejoined sample job may carry an earlier gold selection.
                    return _cluster(host, rendered_dir, inputs, command_path, sleep, monotonic)
                return 0
            report.print_stage(StageResult(
                "cluster", True, f"{inputs.name}: clustering, documents {counts.get('cluster_documents', 0)}, "
                f"texts {texts}, seconds {counts.get('cluster_seconds', 0)} of {CLUSTER_BUDGET_SECONDS}", "",
            ))
            heartbeat_at = now
        sleep(1.0)


def _job_failure(
    host: IndexHost, rendered_dir: PathLike, inputs: generations.GenerationInputs,
    outcome: lexical.BuildRead, command_path: str,
) -> Problem:
    """Report a failed sample refill while its promoted generation stands."""

    assert outcome.failure is not None
    if inputs.sample is not None and outcome.job.status == "failed":
        row = indexrecord.read_generation(host, rendered_dir, inputs.name, command_path=command_path)
        if isinstance(row, Problem):
            return row
        if row is not None and row.state == "live":
            return Problem(
                f"{inputs.name}: sample refill failed; the promoted generation stands",
                f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, then run {report.command(command_path)} again.",
            )
    return outcome.failure


def _build(
    host: IndexHost, rendered_dir: PathLike, inputs: generations.GenerationInputs,
    command_path: str, sleep: Callable[[float], None], monotonic: Callable[[], float],
) -> _JobResult:
    retry = command_path if inputs.sample is not None else None
    job = lexical.live_job(host, rendered_dir, f"index-{inputs.name}", command_path=command_path)
    if isinstance(job, Problem):
        return _JobResult(_refuse("build", job, command_path=retry), False)
    rejoined = job is not None
    if job is None:
        job = lexical.defer_lexical(host, rendered_dir, inputs, command_path=command_path)
    if isinstance(job, Problem):
        return _JobResult(_refuse("build", job, command_path=retry), False)
    report.print_stage(StageResult("build", True, f"{inputs.name}: {'rejoined' if rejoined else 'deferred'} job {job}", ""))
    heartbeat_at = monotonic()
    while True:
        outcome = lexical.read_lexical(host, rendered_dir, job, inputs.name, command_path=command_path)
        if isinstance(outcome, Problem):
            return _JobResult(_refuse("build", outcome, command_path=retry), not rejoined)
        if outcome.failure is not None:
            return _JobResult(_refuse("build", _job_failure(host, rendered_dir, inputs, outcome, command_path)), not rejoined)
        if outcome.done:
            report.print_stage(StageResult("build", True, f"{inputs.name}: job {job} succeeded", ""))
            if rejoined and inputs.sample is not None:
                # A rejoined sample job may carry an earlier gold selection, so
                # the current one runs next; its presence checks skip the rest.
                return _build(host, rendered_dir, inputs, command_path, sleep, monotonic)
            return _JobResult(0, not rejoined)
        now = monotonic()
        if now - heartbeat_at >= snapshots.HEARTBEAT_SECONDS:
            row = indexrecord.read_generation(host, rendered_dir, inputs.name, command_path=command_path)
            if isinstance(row, Problem):
                return _JobResult(_refuse("build", row), not rejoined)
            if row is None:
                return _JobResult(_refuse("build", Problem(
                    "generation progress row is missing", record.database_fix(rendered_dir, command_path),
                )), not rejoined)
            report.print_stage(StageResult(
                "build", True, f"{inputs.name}: building, documents {row.counts.get('ready', 0)}, "
                f"chunks indexed {row.counts.get('indexed', 0)}, seconds {row.counts.get('seconds', 0)}", "",
            ))
            heartbeat_at = now
        sleep(1.0)


def _counts(host: IndexHost, rendered_dir: PathLike, name: str, command_path: str) -> int:
    row = indexrecord.read_generation(host, rendered_dir, name, command_path=command_path)
    if isinstance(row, Problem):
        return _refuse("counts", row)
    if row is None or row.lexical_built_at is None:
        return _refuse("counts", Problem(
            "succeeded lexical job has no recorded lexical_built_at",
            f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, then run {report.command(command_path)} again.",
        ))
    counts = row.counts
    indexed, present = counts.get("indexed", 0), counts.get("present", 0)
    report.print_stage(StageResult(
        "counts", True, f"{name}: documents ready {counts.get('ready', 0)}, "
        f"chunked {counts.get('chunked', 0)}, unchunked {counts.get('unchunked', 0)}; "
        f"chunks indexed {indexed}, present {present}, leveled {counts.get('leveled', 0)}, total {indexed + present}; "
        f"bytes {row.bytes}; lexical_built_at {row.lexical_built_at}; state {row.state}"
        + ("; empty index" if not counts.get("ready", 0) else ""), "",
    ))
    return 0


def _dense_progress(name: str, counts: Mapping[str, int], now: datetime) -> str:
    present, copied, embedded = (counts.get(f"dense_{key}", 0) for key in ("present", "copied", "embedded"))
    written, scope = copied + embedded, counts.get("dense_scope", 0)
    if scope == present + written:
        return f"{name}: indexing {counts.get('dense_indexed', 0)} of {scope}"
    detail = (f"{name}: embedding, chunks written {written} of {scope} "
              f"(present {present}, copied {copied}, embedded {embedded}), tokens {counts.get('dense_tokens', 0)}")
    projected = dense.projection(counts, now)
    if projected is not None:
        hours = (projected.finish - now).total_seconds() / 3600
        detail += f", {projected.tokens_per_second:.1f} tok/s, projected finish {projected.finish.isoformat()} ({hours:.1f} h)"
    return detail


def _first_hour(name: str, counts: Mapping[str, int], now: datetime) -> str:
    seconds, tokens, written = (counts.get(f"dense_hour_{key}", 0) for key in ("seconds", "tokens", "written"))
    detail = f"{name}: first hour: tokens {tokens} in {seconds} s"
    if seconds > 0:
        detail += f", {tokens / seconds:.1f} tok/s"
    detail += f", written {written}"
    projected = dense.projection({**counts, "dense_seconds": seconds, "dense_tokens": tokens,
                                  "dense_copied": 0, "dense_embedded": written,
                                  "dense_scope": counts.get("dense_total", 0)}, now)
    if projected is not None:
        days, remainder = divmod(projected.generation_seconds, timedelta(days=1).total_seconds())
        detail += (f"; the generation of {counts.get('dense_total', 0)} chunks projected at "
                   f"{int(days)} d {remainder / 3600:.1f} h, finish {projected.finish.isoformat()}: "
                   f"{projected.verdict} the three-week tripwire")
    return detail


def _dense(
    host: IndexHost, rendered_dir: PathLike, inputs: dense.DenseInputs,
    command_path: str, sleep: Callable[[float], None], monotonic: Callable[[], float],
    clock: Callable[[], datetime],
    dense_limit: int | None = None,
) -> _JobResult:
    retry = command_path if inputs.sample is not None else None
    job = lexical.live_job(host, rendered_dir, f"{DENSE_LOCK_PREFIX}{inputs.name}",
                          prefix=DENSE_LOCK_PREFIX, command_path=command_path)
    if isinstance(job, Problem):
        return _JobResult(_refuse("dense", job, command_path=retry), False)
    rejoined = job is not None
    if job is None:
        job = dense.defer_dense(host, rendered_dir, inputs, limit=dense_limit, command_path=command_path)
    if isinstance(job, Problem):
        return _JobResult(_refuse("dense", job, command_path=retry), False)
    detail = f"{inputs.name}: {'rejoined' if rejoined else 'deferred'} job {job}"
    if not rejoined and dense_limit is not None:
        detail += f", limit {dense_limit}"
    report.print_stage(StageResult("dense", True, detail, ""))
    heartbeat_at = monotonic()
    hour_printed = False
    while True:
        outcome = dense.read_dense(host, rendered_dir, job, inputs.name, command_path=command_path)
        if isinstance(outcome, Problem):
            return _JobResult(_refuse("dense", outcome, command_path=retry), not rejoined)
        if outcome.failure is not None:
            return _JobResult(_refuse("dense", _job_failure(host, rendered_dir, inputs, outcome, command_path)), not rejoined)
        now = monotonic()
        heartbeat = now - heartbeat_at >= snapshots.HEARTBEAT_SECONDS
        if not heartbeat and not outcome.done:
            sleep(1.0)
            continue
        row = indexrecord.read_generation(host, rendered_dir, inputs.name, command_path=command_path)
        if isinstance(row, Problem):
            return _JobResult(_refuse("dense", row), not rejoined)
        if row is None:
            return _JobResult(_refuse("dense", Problem(
                "generation progress row is missing", record.database_fix(rendered_dir, command_path),
            )), not rejoined)
        counts = row.counts
        if not hour_printed and (counts.get("dense_hour_seconds", 0) > 0 or
                                (outcome.done and counts.get("dense_limit", 0) > 0 and "dense_hour_seconds" in counts)):
            report.print_stage(StageResult("first-hour", True, _first_hour(inputs.name, counts, clock()), ""))
            hour_printed = True
        if outcome.done:
            report.print_stage(StageResult("dense", True, f"{inputs.name}: job {job} succeeded, attempts {outcome.job.attempts}", ""))
            if rejoined and inputs.sample is not None:
                # As the build stage's rejoin: the current selection runs next.
                return _dense(host, rendered_dir, inputs, command_path, sleep, monotonic, clock, dense_limit)
            return _JobResult(0, not rejoined)
        report.print_stage(StageResult("dense", True, _dense_progress(inputs.name, counts, clock()), ""))
        heartbeat_at = now
        sleep(1.0)


def _built(
    host: IndexHost, rendered_dir: PathLike, name: str, command_path: str,
    *, qdrant_root: str = QDRANT_DATA_ROOT,
) -> int:
    row = indexrecord.read_generation(host, rendered_dir, name, command_path=command_path)
    if isinstance(row, Problem):
        return _refuse("built", row)
    if row is None or (row.dense_built_at is None and row.counts.get("dense_limit", 0) <= 0):
        return _refuse("built", Problem("succeeded dense job has no recorded dense_built_at",
                                      f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, then run {report.command(command_path)} again."))
    counts = row.counts
    present, copied, embedded = (counts.get(f"dense_{key}", 0) for key in ("present", "copied", "embedded"))
    written = copied + embedded
    split = f"present {present}, leveled {counts.get('dense_leveled', 0)}, copied {copied}, embedded {embedded}"
    if row.dense_built_at is None:
        detail = (f"{name}: limited to {counts['dense_limit']} documents, chunks written {written} of {counts.get('dense_scope', 0)}; "
                  f"dense half not built: run {report.command(command_path)} again")
    else:
        directory = Path(qdrant_root) / "collections" / name
        fix = (
            f"Inspect {directory}, run {stack.logs_fix(rendered_dir, QDRANT_SERVICE_NAME)}, "
            f"then run {report.command(command_path)} again to record the footprint."
        )
        try:
            measured = host.run(["du", "-sb", str(directory)])
        except (OSError, subprocess.SubprocessError) as exc:
            return _refuse("built", Problem(
                f"could not measure {directory} ({type(exc).__name__})", fix,
            ))
        if measured.returncode != 0:
            return _refuse("built", Problem(
                f"could not measure {directory} (exit {measured.returncode})", fix,
            ))
        fields = measured.stdout.split()
        if (
            len(fields) != 2 or not fields[0].isascii() or not fields[0].isdigit()
            or fields[1] != str(directory)
        ):
            return _refuse("built", Problem(f"invalid byte measurement for {directory}", fix))
        footprint = indexrecord.write_footprint(
            host, rendered_dir, name, int(fields[0]), command_path=command_path,
        )
        if isinstance(footprint, Problem):
            return _refuse("built", footprint)
        detail = (f"{name}: points {counts.get('dense_points', 0)}, indexed {counts.get('dense_indexed', 0)}, "
                  f"written {written} ({split}), "
                  f"tokens {counts.get('dense_tokens', 0)}, requests {counts.get('dense_requests', 0)}, "
                  f"seconds {counts.get('dense_seconds', 0)}; dense_built_at {row.dense_built_at}; state {row.state}; "
                  f"bytes lexical {footprint['lexical_bytes']}, dense {footprint['dense_bytes']}, total {footprint['bytes']}")
    report.print_stage(StageResult("built", True, detail, ""))
    return 0


def _treatment(
    host: IndexHost, rendered_dir: PathLike, inputs: generations.GenerationInputs,
    treatment_snapshot: str | None, command_path: str, *, corpus_dir: PathLike | None = None,
) -> treatment.Treatment | int:
    documents = None
    if inputs.sample is not None:
        rows = indexrecord.read_manifest_rows(host, rendered_dir, inputs.name, command_path)
        if isinstance(rows, Problem):
            return _refuse("treatment", rows)
        documents = tuple(doc_id for _, _, doc_id in rows)
    materialized = treatment.materialize(
        host, rendered_dir, inputs, treatment_snapshot, command_path=command_path,
        corpus_dir=corpus_dir, documents=documents,
    )
    if isinstance(materialized, Problem):
        return _refuse("treatment", materialized)
    report.print_stage(StageResult("treatment", True, f"{inputs.name}: {treatment.summary(materialized)}", ""))
    return materialized


def _report(
    host: IndexHost, rendered_dir: PathLike, inputs: generations.GenerationInputs,
    seconds: int, failed_stage: str | None, whole_build: bool,
    materialized: treatment.Treatment | None,
    work_root: PathLike, command_path: str, *, corpus_dir: PathLike | None = None,
) -> int:
    row = indexrecord.read_generation(host, rendered_dir, inputs.name, command_path=command_path)
    if isinstance(row, Problem):
        return _refuse("report", row)
    if row is None:
        return _refuse("report", Problem(
            "generation row for the build report is missing",
            record.database_fix(rendered_dir, command_path),
        ))
    # A limited dense finish leaves the generation unbuilt, so no run measured it whole.
    gathered = buildreport.gather(
        host, rendered_dir, inputs, row, seconds, failed_stage,
        whole_build and row.dense_built_at is not None,
        treatment=materialized, work_root=work_root, command_path=command_path, corpus_dir=corpus_dir,
    )
    if isinstance(gathered, Problem):
        return _refuse("report", gathered)
    written = buildreport.write_report(host, rendered_dir, gathered, command_path=command_path)
    if isinstance(written, Problem):
        return _refuse("report", written)
    report.print_stage(StageResult("report", True, buildreport.summary(written), ""))
    return 0


def _finish(
    host: IndexHost, rendered_dir: PathLike, inputs: dense.DenseInputs, command_path: str,
    result: int, whole_build: bool, *, started_at: float, monotonic: Callable[[], float],
    active_stage: list[str], treatment_snapshot: str | None, work_root: PathLike,
    corpus_dir: PathLike | None = None,
) -> int:
    materialized: treatment.Treatment | None = None
    if not result:
        active_stage[0] = "treatment"
        treated = _treatment(host, rendered_dir, inputs, treatment_snapshot, command_path, corpus_dir=corpus_dir)
        if isinstance(treated, int):
            result = treated
        else:
            materialized = treated
    failed_stage = active_stage[0] if result else None
    active_stage[0] = "report"
    report_result = _report(
        host, rendered_dir, inputs, int(monotonic() - started_at), failed_stage,
        whole_build and not result, materialized, work_root, command_path, corpus_dir=corpus_dir,
    )
    return result if result else report_result


def build_generation(
    host: IndexHost, rendered_dir: PathLike, inputs: dense.DenseInputs, command_path: str,
    *, created: bool, started_at: float, sleep: Callable[[float], None],
    monotonic: Callable[[], float], clock: Callable[[], datetime], active_stage: list[str],
    treatment_snapshot: str | None, chunk_guards: models.ChunkGuards,
    work_root: PathLike = WORK_ROOT, dense_limit: int | None = None,
) -> int:
    """Guard, reserve, cluster, build both halves, materialize treatment, and report under the caller's lock."""

    # A guard or reserve refusal deferred nothing and writes no build report.
    active_stage[0] = "guard"
    result = _guard(host, rendered_dir, inputs, chunk_guards, command_path)
    if result:
        return result
    active_stage[0] = "reserve"
    result = _reserve(host, rendered_dir, inputs, command_path)
    if result:
        return result
    active_stage[0] = "cluster"
    result = _cluster(host, rendered_dir, inputs, command_path, sleep, monotonic)
    lexical_result = _JobResult(0, False)
    if not result:
        active_stage[0] = "build"
        lexical_result = _build(host, rendered_dir, inputs, command_path, sleep, monotonic)
        result = lexical_result.exit_code
    dense_deferred = False
    if not result:
        active_stage[0] = "counts"
        result = _counts(host, rendered_dir, inputs.name, command_path)
    if not result:
        active_stage[0] = "dense"
        dense_result = _dense(host, rendered_dir, inputs, command_path, sleep, monotonic, clock, dense_limit)
        result = dense_result.exit_code
        dense_deferred = dense_result.deferred
    if not result:
        active_stage[0] = "built"
        result = _built(host, rendered_dir, inputs.name, command_path)
    return _finish(
        host, rendered_dir, inputs, command_path, result,
        created and lexical_result.deferred and dense_deferred, started_at=started_at,
        monotonic=monotonic, active_stage=active_stage, treatment_snapshot=treatment_snapshot,
        work_root=work_root,
    )


def finish_generation(
    host: IndexHost, rendered_dir: PathLike, inputs: dense.DenseInputs, command_path: str,
    *, started_at: float, monotonic: Callable[[], float], active_stage: list[str],
    treatment_snapshot: str | None, work_root: PathLike = WORK_ROOT,
) -> int:
    """Materialize treatment and report a generation already built, under the caller's lock."""

    return _finish(
        host, rendered_dir, inputs, command_path, 0, False, started_at=started_at,
        monotonic=monotonic, active_stage=active_stage, treatment_snapshot=treatment_snapshot,
        work_root=work_root,
    )


def _gold(host: IndexHost, rendered_dir: PathLike, inputs: dense.DenseInputs, command_path: str) -> int:
    """Report gold presence and changed judged hashes without gating on either.

    The given list was recorded at generation.
    """

    assert inputs.sample is not None
    gold = {document.source_id: document.sha256 for document in inputs.sample.gold}
    hashes = indexrecord.read_manifest(host, rendered_dir, inputs.name, sorted(gold), command_path)
    if isinstance(hashes, Problem):
        return _refuse("gold", hashes)
    absent = sorted(gold.keys() - hashes.keys())
    changed = sorted(identifier for identifier, digest in gold.items()
                     if digest is not None and identifier in hashes and hashes[identifier] != digest)
    report.print_stage(StageResult(
        "gold", True, f"{inputs.name}: gold {len(gold)}, present {len(hashes)}; "
        f"absent {', '.join(absent) or 'none'}; hash-changed {', '.join(changed) or 'none'}", "",
    ))
    return 0


def _build_sample(
    host: IndexHost, rendered_dir: PathLike, inputs: dense.DenseInputs, command_path: str,
    *, corpus_dir: PathLike, chunk_guards: models.ChunkGuards, treatment_snapshot: str | None,
    created: bool, started_at: float, work_root: PathLike,
    sleep: Callable[[float], None], monotonic: Callable[[], float],
    clock: Callable[[], datetime], active_stage: list[str], dense_limit: int | None = None,
) -> int:
    """Run a sample's guard, reserve, cluster, build, counts, dense, gold, built, treatment, and report.

    The generation stage has already recorded the given gold list.
    Corpus reads use corpus_dir; generation reads and writes use rendered_dir.
    Guard, reserve, and gold refusals return without a report; cluster, build, counts,
    dense, and built refusals run the finish stages under the engine lock.
    """

    active_stage[0] = "guard"
    result = _guard(host, rendered_dir, inputs, chunk_guards, command_path, corpus_dir=corpus_dir)
    if result:
        return result
    active_stage[0] = "reserve"
    result = _reserve(host, rendered_dir, inputs, command_path)
    if result:
        return result
    active_stage[0] = "cluster"
    result = _cluster(host, rendered_dir, inputs, command_path, sleep, monotonic)
    lexical_result = _JobResult(0, False)
    if not result:
        active_stage[0] = "build"
        lexical_result = _build(host, rendered_dir, inputs, command_path, sleep, monotonic)
        result = lexical_result.exit_code
    dense_deferred = False
    if not result:
        active_stage[0] = "counts"
        result = _counts(host, rendered_dir, inputs.name, command_path)
    if not result:
        active_stage[0] = "dense"
        dense_result = _dense(host, rendered_dir, inputs, command_path, sleep, monotonic, clock, dense_limit)
        result = dense_result.exit_code
        dense_deferred = dense_result.deferred
    if not result:
        active_stage[0] = "gold"
        result = _gold(host, rendered_dir, inputs, command_path)
        if result:
            return result
        active_stage[0] = "built"
        result = _built(host, rendered_dir, inputs.name, command_path, qdrant_root=CI_QDRANT_DATA_ROOT)
    return _finish(
        host, rendered_dir, inputs, command_path, result,
        created and lexical_result.deferred and dense_deferred, started_at=started_at,
        monotonic=monotonic, active_stage=active_stage, treatment_snapshot=treatment_snapshot,
        work_root=work_root, corpus_dir=corpus_dir,
    )


def run_index_build(
    args: argparse.Namespace, *, host: IndexHost | None = None,
    rendered_dir: PathLike = "/etc/gideon/rendered", checkout: PathLike | None = None,
    site_path: PathLike = "/etc/gideon/site.yaml", clock: Callable[[], datetime] = _utc_now,
    work_root: PathLike = WORK_ROOT,
    sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
    sources: Sequence[SourceDefinition] = SOURCES,
    dense_limit: int | None = None, sample: SampleInputs | None = None,
) -> int:
    """Check inputs, record an identity, cluster, build both indexes, materialize treatment, and write the report."""

    io = host if host is not None else RealHost()
    root = Path(checkout) if checkout is not None else Path(__file__).parents[3]
    command_path = f"{COMMAND_PATH} {args.collection}"
    production_dir = rendered_dir
    if sample is not None:
        command_path += " --sample"
        rendered_dir = CI_ROOT
        secrets.select_directory(Path(CI_SECRETS_DIR))
    if args.lockfile is not None:
        command_path += f" --lockfile {args.lockfile}"
    active_stage = ["preconditions"]
    recorded = False
    claim: backuplock.Claim | None = None
    engine_lock: backuplock.Outcome | None = None
    lock_detail = ""
    try:
        started_at = monotonic()
        if io.geteuid() != 0:
            return _refuse(active_stage[0], Problem("root privileges are required", f"Run {report.command(command_path)}, then retry."))
        if sample is None:
            claim = backuplock.claim(io, command=report.command_name(COMMAND_PATH), now=clock(), lock=backuplock.CORPUS_LOCK)
            if claim.refusal is not None:
                return _refuse(active_stage[0], Problem(claim.refusal.detail, snapshots.retry_fix(claim.refusal.fix, command_path=command_path)))
            if not claim.taken:
                return _refuse(active_stage[0], Problem("a corpus command is already running in this process", f"Wait, then run {report.command(command_path)} again."))
        else:
            try:
                engine_lock = backuplock.take(
                    io, command=report.command_name(command_path), now=clock(), lock=backuplock.ENGINE_LOCK,
                )
            except OSError:
                return _refuse(active_stage[0], Problem(
                    "engine lock could not be taken",
                    f"Repair access to the engine lock, then run {report.command(command_path)} again.",
                ))
            if engine_lock.state is backuplock.State.REFUSED:
                assert engine_lock.problem is not None
                return _refuse(active_stage[0], Problem(
                    engine_lock.problem.problem,
                    snapshots.retry_fix(engine_lock.problem.fix, command_path=command_path),
                ))
            lock_detail = "; engine lock taken" if engine_lock.state is backuplock.State.HELD else "; engine lock held by this process"
        prepared = _preconditions(
            args, io, rendered_dir, root, sources, site_path, command_path,
            sample=sample, production_dir=production_dir if sample is not None else None,
        )
        if isinstance(prepared, Problem):
            return _refuse(active_stage[0], prepared, command_path=command_path if sample is not None else None)
        inputs = prepared.inputs
        detail = f"{inputs.collection}: {inputs.label}; {inputs.snapshot}; pipeline {inputs.pipeline}; embedding space {inputs.embedding_space}; served model {inputs.served_model}; dimensions {inputs.dimensions}"
        if sample is not None:
            detail += f"; eval set {sample.set_version}; fill per million {sample.fill_per_million}"
        if prepared.resume_generation is not None:
            detail += f"; lockfile {inputs.label} is superseded; resuming {prepared.resume_generation}"
        report.print_stage(StageResult("preconditions", True, detail + lock_detail, ""))
        if sample is None:
            active_stage[0] = "smoke"
            evidence = smoke.smoke_evidence(io, rendered_dir, root, inputs, command_path)
            if isinstance(evidence, Problem):
                return _refuse("smoke", evidence)
            report.print_stage(StageResult("smoke", True, smoke.stage_detail(evidence), ""))
        conflict_fix = f"Run sudo python3 -m tools.cistack down --wipe, then run {report.command(command_path)} again."
        if sample is not None:
            active_stage[0] = "lockfile"
            problem = record.copy_lockfile(io, production_dir, rendered_dir, inputs.label, command_path, conflict_fix=conflict_fix)
            if problem is not None:
                return _refuse(active_stage[0], problem)
            report.print_stage(StageResult("lockfile", True, f"{inputs.label}: copied to the sibling", ""))
        active_stage[0] = "generation"
        if sample is not None:
            row = indexrecord.read_generation(io, rendered_dir, inputs.name, command_path=command_path)
            if isinstance(row, Problem):
                return _refuse(active_stage[0], row)
            if row is not None and row.inputs_hash != inputs.inputs_hash:
                return _refuse(active_stage[0], Problem(f"{inputs.name}: generation carries another inputs hash", conflict_fix))
        outcome = indexrecord.write_building(io, rendered_dir, inputs, command_path=command_path)
        if isinstance(outcome, Problem):
            return _refuse(active_stage[0], outcome)
        recorded = True
        if outcome.state == "retired":
            retry = COMMAND_PATH if sample is None else command_path
            return _refuse(active_stage[0], Problem(
                f"{outcome.name}: generation is retired",
                "A retired generation is not rebuilt in place: make a new lockfile with "
                f"{report.command('corpus cut')} and build it with {report.command(retry)}.",
            ))
        inputs = replace(inputs, name=outcome.name)
        generation_detail = f"{outcome.name}: {outcome.state}, {outcome.outcome}"
        if sample is not None:
            problem = indexrecord.write_gold(
                io, rendered_dir, inputs.name, sorted(document.source_id for document in sample.gold),
                command_path=command_path,
            )
            if isinstance(problem, Problem):
                return _refuse(active_stage[0], problem)
            generation_detail += f"; gold {len(sample.gold)} recorded"
        report.print_stage(StageResult("generation", True, generation_detail, ""))
        if outcome.state == "live" and sample is None:
            report.print_stage(StageResult("generation", True, f"{outcome.name}: generation is live; nothing built", ""))
            return 0
        if sample is not None:
            return _build_sample(
                io, rendered_dir, inputs, command_path, sleep=sleep, monotonic=monotonic,
                clock=clock, active_stage=active_stage, dense_limit=dense_limit,
                corpus_dir=production_dir, chunk_guards=prepared.chunk_guards,
                treatment_snapshot=prepared.treatment_snapshot, created=outcome.outcome == "created",
                started_at=started_at, work_root=work_root,
            )
        return build_generation(
            io, rendered_dir, inputs, command_path, created=outcome.outcome == "created",
            started_at=started_at, sleep=sleep, monotonic=monotonic, clock=clock,
            active_stage=active_stage, treatment_snapshot=prepared.treatment_snapshot,
            chunk_guards=prepared.chunk_guards, work_root=work_root, dense_limit=dense_limit,
        )
    except KeyboardInterrupt:
        if active_stage[0] == "cluster":
            return _refuse(active_stage[0], Problem("interrupted; the clustering continues in the worker", f"Run {report.command(command_path)} again to rejoin it."))
        if active_stage[0] == "build":
            return _refuse(active_stage[0], Problem("interrupted; the build continues in the worker", f"Run {report.command(command_path)} again to rejoin it."))
        if active_stage[0] == "dense":
            return _refuse(active_stage[0], Problem("interrupted; the embed continues in the worker", f"Run {report.command(command_path)} again to rejoin it."))
        if active_stage[0] == "built":
            return _refuse(active_stage[0], Problem(
                "interrupted; the row stands; the footprint is not recorded",
                f"Run {report.command(command_path)} again to record it.",
            ))
        if active_stage[0] == "treatment":
            return _refuse(active_stage[0], Problem(
                "interrupted; the generation's states are replaced whole by the next run",
                f"Run {report.command(command_path)} again.",
            ))
        if active_stage[0] == "report":
            return _refuse(active_stage[0], Problem("interrupted; the report row may stand", f"Run {report.command(command_path)} again."))
        detail = "interrupted; the row stands" if recorded else "interrupted; nothing was recorded"
        return _refuse(active_stage[0], Problem(detail, f"Run {report.command(command_path)} again."))
    finally:
        if claim is not None:
            backuplock.release_claim(io, claim, lock=backuplock.CORPUS_LOCK)
        if engine_lock is not None and engine_lock.state is backuplock.State.HELD:
            backuplock.release(io, lock=backuplock.ENGINE_LOCK)
