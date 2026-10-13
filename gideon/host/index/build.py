"""Build a collection's lexical index generation through the queue worker."""

import argparse
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from gideon.host import backuplock, caselaw, models, report, site, stack, staging
from gideon.host.corpus import artifacts, record, snapshots
from gideon.host.corpus.sources import SOURCES, SourceDefinition
from gideon.host.index import generations, lexical
from gideon.host.index import record as indexrecord
from gideon.host.render.opensearch import OPENSEARCH_SERVICE_NAME
from gideon.host.render.worker import WORKER_SERVICE_NAME
from gideon.host.report import Problem, StageResult
from gideon.host.sysio import LockingHost, PathLike, RealHost, WritableBytesHost

COMMAND_PATH = "index build"
SAMPLE_LANDING = "lands in slice 3 (v0.4.0)"


class IndexHost(WritableBytesHost, LockingHost, Protocol):
    """The artifact, subprocess, and lock operations needed by an index build."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _refuse(name: str, problem: Problem) -> int:
    row = StageResult(name, False, problem.problem, problem.fix)
    if name == "preconditions":
        print(report.stage_line(row), file=sys.stderr)
    else:
        report.print_stage(row)
    return 1


def _store_preconditions(host: IndexHost, rendered_dir: PathLike, command_path: str) -> Problem | None:
    declared = stack.declared_services(host, rendered_dir)
    if isinstance(declared, Problem):
        return declared
    if OPENSEARCH_SERVICE_NAME not in declared:
        return Problem("rendered stack has no lexical store", f"Run {report.command('apply')}, then run {report.command(command_path)} again.")
    tool_fix = f"Restore Docker Compose, then run {report.command(command_path)} again."
    try:
        result = host.run(stack.compose_argv(rendered_dir, "ps", "--all", "--format", "json"))
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"lexical store state could not be read ({type(exc).__name__})", tool_fix)
    if result.returncode == 127:
        return Problem("lexical store state command is unavailable (exit 127)", tool_fix)
    logs_fix = f"Run {stack.logs_fix(rendered_dir, OPENSEARCH_SERVICE_NAME)}, then run {report.command(command_path)} again."
    if result.returncode != 0:
        return Problem(f"lexical store state read failed (exit {result.returncode})", logs_fix)
    rows = stack.parse_ps(result.stdout)
    if rows is None:
        return Problem("lexical store service state is invalid", logs_fix)
    if not any(row.get("Service") == OPENSEARCH_SERVICE_NAME
               and row.get("State") == "running" and row.get("Health") == "healthy"
               for row in rows):
        return Problem("lexical store is not running and healthy", logs_fix)
    return None


def _preconditions(
    args: argparse.Namespace, host: IndexHost, rendered_dir: PathLike,
    checkout: Path, registry: Sequence[SourceDefinition], site_path: PathLike,
    command_path: str,
) -> generations.GenerationInputs | Problem:
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
    problem = _store_preconditions(host, rendered_dir, command_path)
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
    if profile.embedding_space is None:
        return Problem(
            f"profile {profile.name} has no embedding_space in models.lock",
            f"Restore the profile's embedding_space block in models.lock, then run {report.command(command_path)} again.",
        )
    space = profile.embedding_space.id
    try:
        name = generations.generation_name(collection.name, lockfile.label, space)
    except ValueError:
        return Problem("generation name exceeds the store's grammar or byte limit",
                       f"Restore a valid embedding space id in models.lock, then run {report.command(command_path)} again.")
    return generations.GenerationInputs(
        name, collection.name, lockfile.label, f"{source.name}-{pin.snapshot_date}",
        courts, geography, lockfile.pipeline, space,
        generations.inputs_hash(lockfile, collection.name, space, sources=registry),
    )


def _build(
    host: IndexHost, rendered_dir: PathLike, inputs: generations.GenerationInputs,
    command_path: str, sleep: Callable[[float], None], monotonic: Callable[[], float],
) -> int:
    job = lexical.live_job(host, rendered_dir, f"index-{inputs.name}", command_path=command_path)
    if isinstance(job, Problem):
        return _refuse("build", job)
    rejoined = job is not None
    if job is None:
        job = lexical.defer_lexical(host, rendered_dir, inputs, command_path=command_path)
    if isinstance(job, Problem):
        return _refuse("build", job)
    report.print_stage(StageResult("build", True, f"{inputs.name}: {'rejoined' if rejoined else 'deferred'} job {job}", ""))
    heartbeat_at = monotonic()
    while True:
        outcome = lexical.read_lexical(host, rendered_dir, job, inputs.name, command_path=command_path)
        if isinstance(outcome, Problem):
            return _refuse("build", outcome)
        if outcome.failure is not None:
            return _refuse("build", outcome.failure)
        if outcome.done:
            report.print_stage(StageResult("build", True, f"{inputs.name}: job {job} succeeded", ""))
            return 0
        now = monotonic()
        if now - heartbeat_at >= snapshots.HEARTBEAT_SECONDS:
            row = indexrecord.read_generation(host, rendered_dir, inputs.name, command_path=command_path)
            if isinstance(row, Problem):
                return _refuse("build", row)
            if row is None:
                return _refuse("build", Problem("generation progress row is missing", record.database_fix(rendered_dir, command_path)))
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
        f"chunks indexed {indexed}, present {present}, total {indexed + present}; "
        f"bytes {row.bytes}; lexical_built_at {row.lexical_built_at}; state {row.state}"
        + ("; empty index" if not counts.get("ready", 0) else ""), "",
    ))
    report.print_stage(StageResult("dense", True, "dense half not built; promote needs built", ""))
    return 0


def run_index_build(
    args: argparse.Namespace, *, host: IndexHost | None = None,
    rendered_dir: PathLike = "/etc/gideon/rendered", checkout: PathLike | None = None,
    site_path: PathLike = "/etc/gideon/site.yaml", clock: Callable[[], datetime] = _utc_now,
    sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
    sources: Sequence[SourceDefinition] = SOURCES,
) -> int:
    """Check inputs, record an identity, wait for its lexical build, and report."""

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
        report.print_stage(StageResult("preconditions", True, f"{inputs.collection}: {inputs.label}; {inputs.snapshot}; pipeline {inputs.pipeline}; embedding space {inputs.embedding_space}", ""))
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
        active_stage = "build"
        result = _build(io, rendered_dir, inputs, command_path, sleep, monotonic)
        if result:
            return result
        active_stage = "counts"
        return _counts(io, rendered_dir, inputs.name, command_path)
    except KeyboardInterrupt:
        if active_stage == "build":
            return _refuse(active_stage, Problem("interrupted; the build continues in the worker", f"Run {report.command(command_path)} again to rejoin it."))
        detail = "interrupted; the row stands" if recorded else "interrupted; nothing was recorded"
        return _refuse(active_stage, Problem(detail, f"Run {report.command(command_path)} again."))
    finally:
        if claim is not None:
            backuplock.release_claim(io, claim, lock=backuplock.CORPUS_LOCK)
