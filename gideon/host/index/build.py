"""Build a collection's near-duplicate clusters, lexical index, and vectors."""

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
    site,
    stack,
    staging,
)
from gideon.host.corpus import artifacts, record, snapshots
from gideon.host.corpus.sources import SOURCES, SourceDefinition
from gideon.host.index import buildreport, dense, dupes, generations, lexical
from gideon.host.index import record as indexrecord
from gideon.host.render.engine import EMBED
from gideon.host.render.opensearch import OPENSEARCH_SERVICE_NAME
from gideon.host.render.qdrant import QDRANT_SERVICE_NAME
from gideon.host.render.worker import (
    CLUSTER_BUDGET_SECONDS,
    DENSE_LOCK_PREFIX,
    DUPES_LOCK_PREFIX,
    WORK_ROOT,
    WORKER_SERVICE_NAME,
)
from gideon.host.report import Problem, StageResult
from gideon.host.sysio import LockingHost, PathLike, RealHost, WritableBytesHost

COMMAND_PATH = "index build"
SAMPLE_LANDING = "lands in slice 3 (v0.4.0)"


class IndexHost(WritableBytesHost, LockingHost, Protocol):
    """The artifact, subprocess, and lock operations needed by an index build."""


@dataclass(frozen=True, slots=True)
class _JobResult:
    """A job stage's exit code and whether this run deferred its job."""

    exit_code: int
    deferred: bool


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _refuse(name: str, problem: Problem) -> int:
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


def _preconditions(
    args: argparse.Namespace, host: IndexHost, rendered_dir: PathLike,
    checkout: Path, registry: Sequence[SourceDefinition], site_path: PathLike,
    command_path: str,
) -> dense.DenseInputs | Problem:
    collection = next((item for item in generations.COLLECTIONS if item.name == args.collection), None)
    if collection is None:
        return Problem("collection is not registered", f"Use a registered collection with {report.command(COMMAND_PATH)}.")
    if args.lockfile is not None:
        problem = staging.label_problem(args.lockfile, command_path, subject="index build")
        if problem is not None:
            return problem
    loaded, issue = artifacts.load_artifacts(host, rendered_dir, checkout, registry, command_path=command_path)
    if issue is not None:
        return Problem(issue.detail, issue.fix)
    assert loaded is not None
    for service, words in ((OPENSEARCH_SERVICE_NAME, "lexical store"),
                           (QDRANT_SERVICE_NAME, "vector store"), (EMBED.service_name, EMBED.words)):
        problem = _service_preconditions(host, rendered_dir, service, words, command_path)
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
    feeding = tuple(source for source in registry if collection.feeds(source))
    if len(feeding) != 1:
        return Problem("collection has no single feeding snapshot", f"Restore the source registry, then run {report.command(command_path)} again.")
    source = feeding[0]
    pin = lockfile.sources[source.name]
    courts = tuple(sorted(pin.courts or ()))
    geography = caselaw.court_geography(loaded.court_map, courts)
    if isinstance(geography, Problem):
        return geography
    rows = record.read_lockfiles(host, rendered_dir, command_path=command_path)
    if isinstance(rows, Problem):
        return rows
    row = next((item for item in rows if item.label == lockfile.label), None)
    if row is None:
        return Problem(
            f"lockfile {lockfile.label} is not recorded",
            f"Run {report.command(f'corpus install {lockfile.label}')} or {report.command('corpus cut')}, "
            f"then run {report.command(command_path)} again.",
        )
    if row.state == "superseded":
        return Problem(
            f"lockfile {lockfile.label} is superseded",
            f"Use a current lockfile with {report.command(f'{COMMAND_PATH} {collection.name}')}, then retry.",
        )
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
    profile = models.select_profile(model_lock.lock, loaded_site.config.hardware_profile)
    if isinstance(profile, Problem):
        return profile
    target = engine._embed_target(profile)
    if isinstance(target, Problem):
        return Problem(target.problem, snapshots.retry_fix(target.fix, command_path=command_path))
    space = target.space_id
    try:
        name = generations.generation_name(collection.name, lockfile.label, space)
    except ValueError:
        return Problem("generation name exceeds the store's grammar or byte limit",
                       f"Restore a valid embedding space id in models.lock, then run {report.command(command_path)} again.")
    return dense.DenseInputs(
        name, collection.name, lockfile.label, f"{source.name}-{pin.snapshot_date}",
        courts, geography, lockfile.pipeline, space,
        generations.inputs_hash(lockfile, collection.name, space, sources=registry),
        target.served_name, target.dimensions,
    )


def _cluster(
    host: IndexHost, rendered_dir: PathLike, inputs: generations.GenerationInputs,
    command_path: str, sleep: Callable[[float], None], monotonic: Callable[[], float],
) -> int:
    job = lexical.live_job(host, rendered_dir, f"{DUPES_LOCK_PREFIX}{inputs.name}",
                          prefix=DUPES_LOCK_PREFIX, command_path=command_path)
    if isinstance(job, Problem):
        return _refuse("cluster", job)
    rejoined = job is not None
    if job is None:
        job = dupes.defer_dupes(host, rendered_dir, inputs, command_path=command_path)
    if isinstance(job, Problem):
        return _refuse("cluster", job)
    report.print_stage(StageResult("cluster", True, f"{inputs.name}: {'rejoined' if rejoined else 'deferred'} job {job}", ""))
    heartbeat_at = monotonic()
    while True:
        outcome = dupes.read_dupes(host, rendered_dir, job, inputs.name, command_path=command_path)
        if isinstance(outcome, Problem):
            return _refuse("cluster", outcome)
        if outcome.failure is not None:
            return _refuse("cluster", outcome.failure)
        now = monotonic()
        if outcome.done or now - heartbeat_at >= snapshots.HEARTBEAT_SECONDS:
            row = indexrecord.read_generation(host, rendered_dir, inputs.name, command_path=command_path)
            if isinstance(row, Problem):
                return _refuse("cluster", row)
            if outcome.done and (row is None or row.clustered_at is None):
                return _refuse("cluster", Problem(
                    "succeeded clustering job has no recorded clustered_at",
                    f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, then run {report.command(command_path)} again.",
                ))
            if row is None:
                return _refuse("cluster", Problem("generation progress row is missing", record.database_fix(rendered_dir, command_path)))
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
                return 0
            report.print_stage(StageResult(
                "cluster", True, f"{inputs.name}: clustering, documents {counts.get('cluster_documents', 0)}, "
                f"texts {texts}, seconds {counts.get('cluster_seconds', 0)} of {CLUSTER_BUDGET_SECONDS}", "",
            ))
            heartbeat_at = now
        sleep(1.0)


def _build(
    host: IndexHost, rendered_dir: PathLike, inputs: generations.GenerationInputs,
    command_path: str, sleep: Callable[[float], None], monotonic: Callable[[], float],
) -> _JobResult:
    job = lexical.live_job(host, rendered_dir, f"index-{inputs.name}", command_path=command_path)
    if isinstance(job, Problem):
        return _JobResult(_refuse("build", job), False)
    rejoined = job is not None
    if job is None:
        job = lexical.defer_lexical(host, rendered_dir, inputs, command_path=command_path)
    if isinstance(job, Problem):
        return _JobResult(_refuse("build", job), False)
    report.print_stage(StageResult("build", True, f"{inputs.name}: {'rejoined' if rejoined else 'deferred'} job {job}", ""))
    heartbeat_at = monotonic()
    while True:
        outcome = lexical.read_lexical(host, rendered_dir, job, inputs.name, command_path=command_path)
        if isinstance(outcome, Problem):
            return _JobResult(_refuse("build", outcome), not rejoined)
        if outcome.failure is not None:
            return _JobResult(_refuse("build", outcome.failure), not rejoined)
        if outcome.done:
            report.print_stage(StageResult("build", True, f"{inputs.name}: job {job} succeeded", ""))
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
    job = lexical.live_job(host, rendered_dir, f"{DENSE_LOCK_PREFIX}{inputs.name}",
                          prefix=DENSE_LOCK_PREFIX, command_path=command_path)
    if isinstance(job, Problem):
        return _JobResult(_refuse("dense", job), False)
    rejoined = job is not None
    if job is None:
        job = dense.defer_dense(host, rendered_dir, inputs, limit=dense_limit, command_path=command_path)
    if isinstance(job, Problem):
        return _JobResult(_refuse("dense", job), False)
    detail = f"{inputs.name}: {'rejoined' if rejoined else 'deferred'} job {job}"
    if not rejoined and dense_limit is not None:
        detail += f", limit {dense_limit}"
    report.print_stage(StageResult("dense", True, detail, ""))
    heartbeat_at = monotonic()
    hour_printed = False
    while True:
        outcome = dense.read_dense(host, rendered_dir, job, inputs.name, command_path=command_path)
        if isinstance(outcome, Problem):
            return _JobResult(_refuse("dense", outcome), not rejoined)
        if outcome.failure is not None:
            return _JobResult(_refuse("dense", outcome.failure), not rejoined)
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
            return _JobResult(0, not rejoined)
        report.print_stage(StageResult("dense", True, _dense_progress(inputs.name, counts, clock()), ""))
        heartbeat_at = now
        sleep(1.0)


def _built(host: IndexHost, rendered_dir: PathLike, name: str, command_path: str) -> int:
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
        detail = (f"{name}: points {counts.get('dense_points', 0)}, indexed {counts.get('dense_indexed', 0)}, "
                  f"written {written} ({split}), "
                  f"tokens {counts.get('dense_tokens', 0)}, requests {counts.get('dense_requests', 0)}, "
                  f"seconds {counts.get('dense_seconds', 0)}; dense_built_at {row.dense_built_at}; state {row.state}")
    report.print_stage(StageResult("built", True, detail, ""))
    return 0


def _report(
    host: IndexHost, rendered_dir: PathLike, inputs: generations.GenerationInputs,
    seconds: int, failed_stage: str | None, whole_build: bool,
    work_root: PathLike, command_path: str,
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
        work_root=work_root, command_path=command_path,
    )
    if isinstance(gathered, Problem):
        return _refuse("report", gathered)
    written = buildreport.write_report(host, rendered_dir, gathered, command_path=command_path)
    if isinstance(written, Problem):
        return _refuse("report", written)
    report.print_stage(StageResult("report", True, buildreport.summary(written), ""))
    return 0


def run_index_build(
    args: argparse.Namespace, *, host: IndexHost | None = None,
    rendered_dir: PathLike = "/etc/gideon/rendered", checkout: PathLike | None = None,
    site_path: PathLike = "/etc/gideon/site.yaml", clock: Callable[[], datetime] = _utc_now,
    work_root: PathLike = WORK_ROOT,
    sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
    sources: Sequence[SourceDefinition] = SOURCES,
    dense_limit: int | None = None,
) -> int:
    """Check inputs, record an identity, and wait for clustering and both index builds."""

    if args.sample:
        print(f"gideon {COMMAND_PATH} --sample: not implemented; {SAMPLE_LANDING}", file=sys.stderr)
        return 1
    io = host if host is not None else RealHost()
    root = Path(checkout) if checkout is not None else Path(__file__).parents[3]
    command_path = f"{COMMAND_PATH} {args.collection}"
    if args.lockfile is not None:
        command_path += f" --lockfile {args.lockfile}"
    active_stage = "preconditions"
    recorded = False
    claim: backuplock.Claim | None = None
    try:
        started_at = monotonic()
        if io.geteuid() != 0:
            return _refuse(active_stage, Problem("root privileges are required", f"Run {report.command(command_path)}, then retry."))
        claim = backuplock.claim(io, command=report.command_name(COMMAND_PATH), now=clock(), lock=backuplock.CORPUS_LOCK)
        if claim.refusal is not None:
            return _refuse(active_stage, Problem(claim.refusal.detail, snapshots.retry_fix(claim.refusal.fix, command_path=command_path)))
        if not claim.taken:
            return _refuse(active_stage, Problem("a corpus command is already running in this process", f"Wait, then run {report.command(command_path)} again."))
        inputs = _preconditions(args, io, rendered_dir, root, sources, site_path, command_path)
        if isinstance(inputs, Problem):
            return _refuse(active_stage, inputs)
        report.print_stage(StageResult("preconditions", True, f"{inputs.collection}: {inputs.label}; {inputs.snapshot}; pipeline {inputs.pipeline}; embedding space {inputs.embedding_space}; served model {inputs.served_model}; dimensions {inputs.dimensions}", ""))
        active_stage = "generation"
        outcome = indexrecord.write_building(io, rendered_dir, inputs, command_path=command_path)
        if isinstance(outcome, Problem):
            return _refuse(active_stage, outcome)
        recorded = True
        report.print_stage(StageResult("generation", True, f"{outcome.name}: {outcome.state}, {outcome.outcome}", ""))
        if outcome.state == "live":
            report.print_stage(StageResult("generation", True, f"{outcome.name}: generation is live; nothing built", ""))
            return 0
        if outcome.state == "retired":
            return _refuse(active_stage, Problem(
                f"{outcome.name}: generation is retired",
                "A retired generation is not rebuilt in place: make a new lockfile with "
                f"{report.command('corpus cut')} and build it with {report.command(COMMAND_PATH)}, "
                f"or remove the retired generation with {report.command('index gc')} once its hold ends.",
            ))
        inputs = replace(inputs, name=outcome.name)
        # The guard and reserve rows belong here: read the profile's guards,
        # the promoted generation's bytes, and the free bytes under /data.
        active_stage = "cluster"
        result = _cluster(io, rendered_dir, inputs, command_path, sleep, monotonic)
        lexical_result = _JobResult(0, False)
        if not result:
            active_stage = "build"
            lexical_result = _build(io, rendered_dir, inputs, command_path, sleep, monotonic)
            result = lexical_result.exit_code
        dense_deferred = False
        if not result:
            active_stage = "counts"
            result = _counts(io, rendered_dir, inputs.name, command_path)
        if not result:
            active_stage = "dense"
            dense_result = _dense(io, rendered_dir, inputs, command_path, sleep, monotonic, clock, dense_limit)
            result = dense_result.exit_code
            dense_deferred = dense_result.deferred
        if not result:
            active_stage = "built"
            result = _built(io, rendered_dir, inputs.name, command_path)
        failed_stage = active_stage if result else None
        whole_build = (
            outcome.outcome == "created" and lexical_result.deferred
            and dense_deferred and not result
        )
        active_stage = "report"
        report_result = _report(
            io, rendered_dir, inputs, int(monotonic() - started_at), failed_stage,
            whole_build, work_root, command_path,
        )
        return result if result else report_result
    except KeyboardInterrupt:
        if active_stage == "cluster":
            return _refuse(active_stage, Problem("interrupted; the clustering continues in the worker", f"Run {report.command(command_path)} again to rejoin it."))
        if active_stage == "build":
            return _refuse(active_stage, Problem("interrupted; the build continues in the worker", f"Run {report.command(command_path)} again to rejoin it."))
        if active_stage == "dense":
            return _refuse(active_stage, Problem("interrupted; the embed continues in the worker", f"Run {report.command(command_path)} again to rejoin it."))
        if active_stage == "report":
            return _refuse(active_stage, Problem("interrupted; the report row may stand", f"Run {report.command(command_path)} again."))
        detail = "interrupted; the row stands" if recorded else "interrupted; nothing was recorded"
        return _refuse(active_stage, Problem(detail, f"Run {report.command(command_path)} again."))
    finally:
        if claim is not None:
            backuplock.release_claim(io, claim, lock=backuplock.CORPUS_LOCK)
