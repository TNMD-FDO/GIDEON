"""Retire a generation by name in the record for the next index gc sweep."""

import argparse
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime

from gideon.host import backuplock, report
from gideon.host.index import lexical, record, sample
from gideon.host.render.ci import CI_ROOT, CI_STACK, PRODUCTION_STACK
from gideon.host.render.worker import GENERATION_PATTERN
from gideon.host.report import Problem, StageResult
from gideon.host.sysio import Host, LockingHost, PathLike, RealHost

COMMAND_PATH = "index retire"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _refuse(stage: str, problem: Problem, command_path: str) -> int:
    if report.command(command_path) not in problem.fix:
        problem = Problem(problem.problem, f"{problem.fix} Then run {report.command(command_path)} again.")
    report.print_stage(StageResult(stage, False, problem.problem, problem.fix))
    return 1


def _candidate(
    host: Host, rendered_dir: PathLike, name: str, collection: str, is_sample: bool, command_path: str,
) -> tuple[record.GenerationRow, str] | Problem:
    row = record.read_generation(host, rendered_dir, name, command_path=command_path)
    if isinstance(row, Problem):
        return row
    if row is None:
        if is_sample:
            fix = (
                f"Run {report.command(f'index build {collection} --sample')}, "
                f"then run {report.command(command_path)} again."
            )
        else:
            fix = f"Run {report.command('index report')} to list the recorded generations, then name one."
        return Problem(f"generation {name} is not recorded", fix)
    if row.state == "live":
        return Problem(
            f"{name}: generation is live and retires only when another is promoted",
            f"Run {report.command('index promote <generation>')} over a built generation of "
            f"{row.collection}; the live one retires at that flip. "
            f"Then run {report.command(command_path)} again.",
        )
    if row.state == "retired":
        return row, f"{name}: already retired at {row.retired_at}; nothing to do"
    counts = ", ".join(f"{key} {value}" for key, value in sorted(row.counts.items())) or "none"
    reason = f" ({row.failure_reason})" if row.failure_reason is not None else ""
    return row, f"{name}: state {row.state}{reason}; counts {counts}; bytes {row.bytes}"


def run_index_retire(
    args: argparse.Namespace, *, host: LockingHost | None = None,
    rendered_dir: PathLike = "/etc/gideon/rendered", clock: Callable[[], datetime] = _utc_now,
) -> int:
    """Check the candidate and its jobs, retire its record, then name the sweep."""

    io = RealHost() if host is None else host
    name = args.gen
    command_path = f"{COMMAND_PATH} {name}"
    is_sample = name.endswith(f"__{sample.SAMPLE_SUFFIX}")
    state_fix = (
        f"Run {report.command(command_path)} again to read its state and confirm retirement."
        if is_sample else
        f"Run {report.command(f'index report {name}')}, then run {report.command(command_path)} again."
    )
    active_stage = "preconditions"
    claim: backuplock.Claim | None = None
    engine_lock: backuplock.Outcome | None = None
    retired = False
    try:
        if io.geteuid() != 0:
            problem = Problem("root privileges are required.", f"Run {report.command(command_path)}.")
            print(report.refusal(COMMAND_PATH, problem.problem, problem.fix), file=sys.stderr)
            return 2
        match = re.fullmatch(GENERATION_PATTERN, name)
        if match is None or len(name.encode()) >= 255:
            return _refuse(active_stage, Problem(
                "generation name is invalid",
                f"Run {report.command('index report')} to list the recorded generations, then name one.",
            ), command_path)
        build_command = f"index build {match.group(1)}"
        if is_sample:
            build_command += " --sample"
        build_command += f" --lockfile {name.split('__')[1]}"
        if is_sample:
            rendered_dir = CI_ROOT
            try:
                engine_lock = backuplock.take(
                    io, command=report.command_name(command_path), now=clock(), lock=backuplock.ENGINE_LOCK,
                )
            except OSError:
                return _refuse(active_stage, Problem(
                    "engine lock could not be taken",
                    f"Repair access to the engine lock, then run {report.command(command_path)} again.",
                ), command_path)
            if engine_lock.state is backuplock.State.REFUSED:
                assert engine_lock.problem is not None
                return _refuse(active_stage, engine_lock.problem, command_path)
            lock_detail = "engine lock taken" if engine_lock.state is backuplock.State.HELD else "engine lock held by this process"
            job = lexical.live_index_job(io, rendered_dir, report.command(command_path))
            if isinstance(job, Problem):
                return _refuse(active_stage, job, command_path)
            if job is not None:
                return _refuse(active_stage, Problem(
                    f"a sample build is running on the sibling (job {job})",
                    f"Wait for the job or rejoin it with {report.command(build_command)}, "
                    f"then run {report.command(command_path)} again.",
                ), command_path)
        else:
            claim = backuplock.claim(
                io, command=report.command_name(COMMAND_PATH), now=clock(), lock=backuplock.CORPUS_LOCK,
            )
            if claim.refusal is not None:
                return _refuse(active_stage, Problem(claim.refusal.detail, claim.refusal.fix), command_path)
            if not claim.taken:
                return _refuse(active_stage, Problem(
                    "a corpus command is already running in this process",
                    f"Wait, then run {report.command(command_path)} again.",
                ), command_path)
            lock_detail = claim.detail
        for prefix, subject in lexical.JOB_SUBJECTS.items():
            job = lexical.live_job(io, rendered_dir, f"{prefix}{name}", prefix=prefix, command_path=command_path)
            if isinstance(job, Problem):
                return _refuse(active_stage, job, command_path)
            if job is not None:
                return _refuse(active_stage, Problem(
                    f"{name}: {subject} job {job} is queued or running",
                    f"Wait for the job or rejoin it with {report.command(build_command)}, "
                    f"then run {report.command(command_path)} again.",
                ), command_path)
        stack_name = CI_STACK if is_sample else PRODUCTION_STACK
        report.print_stage(StageResult(active_stage, True, f"{stack_name}; {lock_detail}", ""))

        active_stage = "candidate"
        candidate = _candidate(io, rendered_dir, name, match.group(1), is_sample, command_path)
        if isinstance(candidate, Problem):
            return _refuse(active_stage, candidate, command_path)
        row, detail = candidate
        converge = row.state == "retired"
        report.print_stage(StageResult(active_stage, True, detail, ""))

        active_stage = "retire"
        if converge:
            report.print_stage(StageResult(active_stage, True, "already retired; skipped record retirement", ""))
            retired = True
        else:
            outcome = record.retire_generation(io, rendered_dir, name, command_path=command_path)
            if isinstance(outcome, Problem):
                # index report reads production alone; re-run retirement to read a sample's state.
                if is_sample and report.command(f"index report {name}") in outcome.fix:
                    outcome = Problem(outcome.problem, state_fix)
                return _refuse(active_stage, outcome, command_path)
            retired = True
            reason = f" ({outcome.failure_reason})" if outcome.failure_reason is not None else ""
            report.print_stage(StageResult(
                active_stage, True,
                f"{name}: retired at {outcome.retired_at} from {outcome.previous_state}{reason}; stores untouched", "",
            ))

        active_stage = "next"
        gc_command = "index gc --stack ci" if is_sample else "index gc"
        report.print_stage(StageResult(
            active_stage, True,
            f"{name}: never rebuilt in place; a new lockfile by {report.command('corpus cut')} "
            f"is the path; {report.command(gc_command)} drops its index and collection", "",
        ))
        print(f"Next: run {report.command(gc_command)} to drop its index and collection.")
        return 0
    except KeyboardInterrupt:
        if retired:
            detail = "interrupted; the generation is retired"
        elif active_stage == "retire":
            detail = "interrupted during retirement; read the record before retrying"
        else:
            detail = "interrupted; nothing was retired"
        return _refuse(active_stage, Problem(detail, state_fix), command_path)
    finally:
        if claim is not None:
            backuplock.release_claim(io, claim, lock=backuplock.CORPUS_LOCK)
        if engine_lock is not None and engine_lock.state is backuplock.State.HELD:
            backuplock.release(io, lock=backuplock.ENGINE_LOCK)
