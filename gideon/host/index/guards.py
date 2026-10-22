"""Project chunks, read resident collections, and judge the profile's memory bounds."""

import json
import subprocess
from dataclasses import dataclass
from typing import Literal

from gideon.host import caselaw, derivedstores, report, worker
from gideon.host.corpus.record import database_fix
from gideon.host.index import lexical, record, sample
from gideon.host.models import ChunkGuards
from gideon.host.render.qdrant import QDRANT_SECRET_NAME
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
class ResidentCollection:
    """A store collection's exact point count and its recorded state."""

    name: str
    points: int
    state: str


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    """The projection's verdict beside resident counts and the profile's limits."""

    kind: Literal["skipped", "refused", "cleared"]
    reason: str
    projection: ChunkProjection
    refusal: int
    ceiling: int
    counted: tuple[ResidentCollection, ...]
    resident: int


@dataclass(frozen=True, slots=True)
class PromotionVerdict:
    """The resident sum's verdict against the profile's serving ceiling."""

    kind: Literal["refused", "cleared"]
    reason: str
    counted: tuple[ResidentCollection, ...]
    resident: int
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


def read_resident(
    host: Host, rendered_dir: PathLike, *, candidate: str, command_path: str,
) -> tuple[ResidentCollection, ...] | Problem:
    """Require other generations to be idle, then count the store's collections."""

    rows = record.read_generations(host, rendered_dir, command_path=command_path)
    if isinstance(rows, Problem):
        return rows
    for row in rows:
        if row.gen_id == candidate:
            continue
        for prefix, subject in lexical.JOB_SUBJECTS.items():
            job = lexical.live_job(
                host, rendered_dir, f"{prefix}{row.gen_id}", prefix=prefix,
                command_path=command_path,
            )
            if isinstance(job, Problem):
                return job
            if job is not None:
                build_command = f"index build {row.collection}"
                if row.gen_id.endswith(f"__{sample.SAMPLE_SUFFIX}"):
                    build_command += " --sample"
                build_command += f" --lockfile {row.corpus_lockfile}"
                return Problem(
                    f"{row.gen_id}: {subject} job {job} is queued or running",
                    f"Wait for the job or rejoin it with {report.command(build_command)}, "
                    f"then run {report.command(command_path)} again.",
                )
    key = derivedstores.store_secret(host, QDRANT_SECRET_NAME)
    if isinstance(key, Problem):
        return key
    names = derivedstores.qdrant_collection_names(host, rendered_dir, key)
    if isinstance(names, Problem):
        return names
    states = {row.gen_id: row.state for row in rows}
    counted: list[ResidentCollection] = []
    for name in names:
        points = derivedstores.qdrant_point_count(host, rendered_dir, key, name)
        if isinstance(points, Problem):
            return points
        if points is not None:
            counted.append(ResidentCollection(name, points, states.get(name, "unrecorded")))
    return tuple(counted)


def judge_guard(
    projection: ChunkProjection, guards: ChunkGuards,
    resident: tuple[ResidentCollection, ...], *, candidate: str,
) -> GuardVerdict:
    """Skip an insufficient sample, otherwise bound the projection and other residents."""

    counted = tuple(row for row in resident if row.name != candidate)
    total = sum(row.points for row in counted)
    refusal, ceiling = guards.build_refusal_chunks, guards.serving_ceiling_chunks
    kind: Literal["skipped", "refused", "cleared"]
    if projection.ready == 0:
        kind, reason = "skipped", "no ready documents"
    elif projection.chunked * 100 < projection.ready:
        kind, reason = "skipped", "fewer than 1 % of ready documents chunked"
    elif total >= ceiling or projection.projected * ceiling > (ceiling - total) * refusal:
        kind, reason = "refused", "resident chunks plus the projection at the margin exceed the serving ceiling"
    else:
        kind, reason = "cleared", "resident chunks plus the projection at the margin are within the serving ceiling"
    return GuardVerdict(
        kind, reason, projection, refusal, ceiling, counted, total,
    )


def judge_promotion(
    resident: tuple[ResidentCollection, ...], guards: ChunkGuards,
) -> PromotionVerdict:
    """Hold every resident collection, including the candidate, to the serving ceiling."""

    total = sum(row.points for row in resident)
    ceiling = guards.serving_ceiling_chunks
    kind: Literal["refused", "cleared"]
    if total > ceiling:
        kind, reason = "refused", "resident chunks exceed the serving ceiling"
    else:
        kind, reason = "cleared", "resident chunks are within the serving ceiling"
    return PromotionVerdict(kind, reason, resident, total, ceiling)


def counted_text(counted: tuple[ResidentCollection, ...]) -> str:
    """Name each counted collection with its state and points, then their sum."""

    names = ", ".join(f"{row.name} ({row.state}) {row.points}" for row in counted) or "none"
    return f"counted {names} = {sum(row.points for row in counted)}"


def resident_fix(
    counted: tuple[ResidentCollection, ...], *, live: str | None, command_path: str,
) -> str:
    """Describe the other resident collections and how their state permits reclamation."""

    grouped: dict[str, list[str]] = {}
    for row in counted:
        if row.name != live:
            grouped.setdefault(row.state, []).append(row.name)
    if not grouped:
        return ""
    gc_command = "index gc"
    if "--sample" in command_path.split() or command_path.endswith(f"__{sample.SAMPLE_SUFFIX}"):
        gc_command += " --stack ci"
    clauses: list[str] = []
    for state, names in sorted(grouped.items()):
        clause = f"{state}: {', '.join(sorted(names))}"
        if state == "retired":
            clause += f" (reclaim with {report.command(gc_command)} after the retirement hold)"
        elif state == "built":
            clause += " (promotable, or droppable after retirement)"
        elif state == "unrecorded":
            clause += " (outside the record)"
        clauses.append(clause)
    return "Other resident collections: " + "; ".join(clauses) + ". "
