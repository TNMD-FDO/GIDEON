"""Project ready-document chunks and judge the profile's build refusal."""

import json
import subprocess
from dataclasses import dataclass
from typing import Literal

from gideon.host import caselaw, worker
from gideon.host.corpus.record import database_fix
from gideon.host.models import ChunkGuards
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike

CHUNK_PROJECTION_SQL = """WITH ready_documents AS (
    SELECT d.doc_id
    FROM public.documents AS d
    JOIN public.opinions AS o ON o.doc_id = d.doc_id
    WHERE d.source = :'v_source'
      AND d.source_snapshot = :'v_snapshot_date'::date
      AND o.court = ANY(:'v_courts'::text[])
      AND d.status = 'ready'
), selected_chunks AS (
    SELECT c.doc_id
    FROM public.chunks AS c
    JOIN ready_documents AS d ON d.doc_id = c.doc_id
    WHERE c.pipeline = :'v_pipeline'
)
SELECT jsonb_build_object(
    'ready', (SELECT count(*) FROM ready_documents),
    'chunked', (SELECT count(DISTINCT doc_id) FROM selected_chunks),
    'chunks', (SELECT count(*) FROM selected_chunks)
);
"""


@dataclass(frozen=True, slots=True)
class ChunkProjection:
    """The ready and chunked document counts, chunks, and projected chunks."""

    ready: int
    chunked: int
    chunks: int
    projected: int


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    """The projection's verdict and reason beside the profile's chunk limits."""

    kind: Literal["skipped", "refused", "cleared"]
    reason: str
    projection: ChunkProjection
    refusal: int
    ceiling: int


def read_chunk_projection(
    host: Host, rendered_dir: PathLike, *, source: str, snapshot_date: str,
    courts: tuple[str, ...], pipeline: str, command_path: str,
) -> ChunkProjection | Problem:
    """Read and project the chunk counts over one generation's scope."""

    for court in courts:
        problem = caselaw.court_problem(court, command_path, subject="chunk projection")
        if problem is not None:
            return problem
    sql = "\n".join((
        worker.bind("v_source", source),
        worker.bind("v_snapshot_date", snapshot_date),
        worker.bind("v_courts", "{" + ",".join(courts) + "}"),
        worker.bind("v_pipeline", pipeline),
        CHUNK_PROJECTION_SQL,
    ))
    fix = database_fix(rendered_dir, command_path)
    try:
        result = host.run(worker.psql_argv(rendered_dir), input=sql)
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"chunk projection command could not run ({type(exc).__name__})", fix)
    if result.returncode != 0:
        return Problem(f"chunk projection read failed (exit {result.returncode})", fix)
    try:
        value = json.loads(result.stdout)
    except ValueError:
        value = None
    if (
        not isinstance(value, dict) or set(value) != {"ready", "chunked", "chunks"}
        or any(type(count) is not int or count < 0 for count in value.values())
    ):
        return Problem("chunk projection counts are invalid", fix)
    ready, chunked, chunks = value["ready"], value["chunked"], value["chunks"]
    projected = (chunks * ready + chunked - 1) // chunked if chunked else 0
    return ChunkProjection(ready, chunked, chunks, projected)


def judge_guard(projection: ChunkProjection, guards: ChunkGuards) -> GuardVerdict:
    """Skip an insufficient sample, otherwise hold its projection to the refusal."""

    kind: Literal["skipped", "refused", "cleared"]
    if projection.ready == 0:
        kind, reason = "skipped", "no ready documents"
    elif projection.chunked * 100 < projection.ready:
        kind, reason = "skipped", "fewer than 1 % of ready documents chunked"
    elif projection.projected > guards.build_refusal_chunks:
        kind, reason = "refused", "projected chunks exceed the build refusal"
    else:
        kind, reason = "cleared", "projected chunks are within the build refusal"
    return GuardVerdict(
        kind, reason, projection, guards.build_refusal_chunks, guards.serving_ceiling_chunks,
    )
