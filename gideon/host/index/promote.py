"""Gate a built generation, promote its record, and converge both store aliases."""

import argparse
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from gideon.host import backuplock, report, secrets, stack
from gideon.host.index import (
    aliases,
    build,
    buildreport,
    guards,
    lexical,
    record,
    sample,
    serving,
)
from gideon.host.render.ci import CI_ROOT, CI_SECRETS_DIR, CI_STACK, PRODUCTION_STACK
from gideon.host.render.worker import CLUSTER_BUDGET_SECONDS, GENERATION_PATTERN
from gideon.host.report import Problem, StageResult
from gideon.host.sysio import Host, LockingHost, PathLike, RealHost

COMMAND_PATH = "index promote"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _refuse(stage: str, problem: Problem, command_path: str) -> int:
    if report.command(command_path) not in problem.fix:
        problem = Problem(problem.problem, f"{problem.fix} Then run {report.command(command_path)} again.")
    report.print_stage(StageResult(stage, False, problem.problem, problem.fix))
    return 1


def _candidate(
    host: Host, rendered_dir: PathLike, name: str, is_sample: bool, command_path: str,
) -> tuple[record.GenerationRow, str] | Problem:
    row = record.read_generation(host, rendered_dir, name, command_path=command_path)
    if isinstance(row, Problem):
        return row
    if row is None:
        return Problem(
            f"generation {name} is not recorded",
            f"Run {report.command('index report')} to list the recorded generations, then name one.",
        )
    build_command = f"index build {row.collection}"
    if is_sample:
        build_command += " --sample"
    build_command += f" --lockfile {row.corpus_lockfile}"
    build_fix = f"Run {report.command(build_command)}, then run {report.command(command_path)} again."
    if row.state in {"building", "failed"}:
        return Problem(f"{name}: generation is {row.state}", build_fix)
    if row.state == "retired":
        return Problem(
            f"{name}: generation is retired and cannot be promoted again",
            f"Make a new lockfile with {report.command('corpus cut')}, "
            f"then run {report.command('corpus install <label>')} and promote its new generation.",
        )
    written_at: str | None = None
    if row.state == "built":
        if "dense_bytes" not in row.counts:
            return Problem(f"{name}: dense footprint is not recorded", build_fix)
        build_report = buildreport.read_report(host, rendered_dir, name, command_path=command_path)
        if isinstance(build_report, Problem):
            return build_report
        if build_report is None:
            return Problem(f"{name}: generation has no build report", build_fix)
        if build_report.failed_stage is not None:
            return Problem(f"{name}: newest build report failed at {build_report.failed_stage}", build_fix)
        cluster = build_report.gates.get("cluster")
        if cluster is None:
            return Problem(
                f"{name}: build report has no cluster gate (budget {CLUSTER_BUDGET_SECONDS} s)",
                build_fix,
            )
        if not cluster.passed:
            return Problem(
                f"{name}: cluster gate failed ({cluster.seconds} s, budget {cluster.budget} s)", build_fix,
            )
        if build_report.treatment is None:
            return Problem(f"{name}: build report has no treatment", build_fix)
        written_at = build_report.written_at
    promoted = record.read_promoted(host, rendered_dir, command_path=command_path)
    if isinstance(promoted, Problem):
        return promoted
    current = serving.select_serving(promoted, row.collection)
    if isinstance(current, Problem):
        return Problem(
            current.problem,
            f"Run {stack.logs_fix(rendered_dir, 'postgres')}, "
            f"then run {report.command(command_path)} again.",
        )
    counts = ", ".join(f"{key} {value}" for key, value in sorted(row.counts.items())) or "none"
    detail = f"{name}: state {row.state}; counts {counts}; bytes {row.bytes}"
    if row.state == "live":
        detail += f"; already live since {row.promoted_at}; the aliases converged"
    else:
        retiring = current.gen_id if current is not None else "none live"
        detail += f"; build report {written_at}; retiring {retiring}"
    return row, detail


def run_index_promote(
    args: argparse.Namespace, *, run_gates: Callable[..., int],
    host: LockingHost | None = None, rendered_dir: PathLike = "/etc/gideon/rendered",
    checkout: PathLike | None = None, site_path: PathLike = "/etc/gideon/site.yaml",
    clock: Callable[[], datetime] = _utc_now,
) -> int:
    """Check the candidate, run its gates, flip the record, then point each store alias."""

    io = RealHost() if host is None else host
    root = Path(checkout) if checkout is not None else Path(__file__).parents[3]
    name = args.gen
    command_path = f"{COMMAND_PATH} {name}"
    active_stage = "preconditions"
    claim: backuplock.Claim | None = None
    engine_lock: backuplock.Outcome | None = None
    flipped = False
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
        is_sample = name.endswith(f"__{sample.SAMPLE_SUFFIX}")
        build_command = f"index build {match.group(1)}"
        if is_sample:
            build_command += " --sample"
            rendered_dir = CI_ROOT
            secrets.select_directory(Path(CI_SECRETS_DIR))
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
        candidate = _candidate(io, rendered_dir, name, is_sample, command_path)
        if isinstance(candidate, Problem):
            return _refuse(active_stage, candidate, command_path)
        row, detail = candidate
        converge = row.state == "live"
        report.print_stage(StageResult(active_stage, True, detail, ""))

        active_stage = "aliases"
        targets = aliases.read_aliases(io, rendered_dir, row.collection, command_path=command_path)
        if isinstance(targets, Problem):
            return _refuse(active_stage, targets, command_path)
        report.print_stage(StageResult(
            active_stage, True,
            f"{row.collection}: lexical {', '.join(targets.lexical) or 'none'}; "
            f"dense {', '.join(targets.dense) or 'none'}", "",
        ))

        active_stage = "memory"
        if converge:
            report.print_stage(StageResult(active_stage, True, "already live; skipped memory check", ""))
        else:
            chunk_guards = build.chunk_guards(io, root, site_path, command_path)
            if isinstance(chunk_guards, Problem):
                return _refuse(active_stage, chunk_guards, command_path)
            resident = guards.read_resident(io, rendered_dir, candidate=name, command_path=command_path)
            if isinstance(resident, Problem):
                return _refuse(active_stage, resident, command_path)
            verdict = guards.judge_promotion(resident, chunk_guards)
            figures = (
                f"{name}: {guards.counted_text(verdict.counted)}; "
                f"{verdict.resident} ≤ serving ceiling {verdict.ceiling}"
            )
            if verdict.kind == "refused":
                live = next((item.name for item in verdict.counted if item.state == "live"), None)
                others = tuple(item for item in verdict.counted if item.name != name)
                fix = guards.resident_fix(others, live=live, command_path=command_path)
                if live is not None:
                    gc_command = "index gc --stack ci" if is_sample else "index gc"
                    fix += (
                        f"The live generation {live} retires at this flip and stays resident "
                        f"for seven days until {report.command(gc_command)} drops it. "
                    )
                fix += (
                    "Select a larger hardware_profile in /etc/gideon/site.yaml; the guards derive "
                    "from the profile's RAM, as docs/runbooks/release-files.md §4 says; then run "
                    f"{report.command(command_path)} again."
                )
                return _refuse(active_stage, Problem(f"{figures}; {verdict.reason}", fix), command_path)
            report.print_stage(StageResult(active_stage, True, figures, ""))

        active_stage = "gates"
        if converge:
            report.print_stage(StageResult(active_stage, True, "already live; skipped build-gates", ""))
        else:
            run_id = str(uuid4())
            code = run_gates(
                argparse.Namespace(
                    slice="build-gates", stack=stack_name, generation=name, kind="manual",
                    set=None, ranked=None, decision=False, against=None, force=False, challenger=False,
                ),
                host=io, checkout_root=checkout, run_id_factory=lambda: run_id,
            )
            if code != 0:
                return _refuse(active_stage, Problem(
                    f"build-gates run {run_id} failed (exit {code})",
                    "Read the run's rows above, starting with the failing part; fix the build or pipeline, "
                    f"then run {report.command(command_path)} again.",
                ), command_path)
            report.print_stage(StageResult(active_stage, True, f"build-gates run {run_id} passed", ""))

        active_stage = "flip"
        if converge:
            report.print_stage(StageResult(active_stage, True, "already live; skipped record promotion", ""))
            flipped = True
        else:
            outcome = record.promote_generation(io, rendered_dir, name, row.collection, command_path=command_path)
            if isinstance(outcome, Problem):
                return _refuse(active_stage, outcome, command_path)
            flipped = True
            report.print_stage(StageResult(
                active_stage, True,
                f"{name}: promoted at {outcome.promoted_at}; retired {', '.join(outcome.retired) or 'none'}; "
                "kept seven days, dropped by index gc", "",
            ))

        stores: tuple[Literal["lexical", "dense"], ...] = ("lexical", "dense")
        for store in stores:
            active_stage = store
            current = targets.lexical if store == "lexical" else targets.dense
            pointed = aliases.point_alias(io, rendered_dir, store, row.collection, name, current, command_path=command_path)
            if isinstance(pointed, Problem):
                return _refuse(active_stage, pointed, command_path)
            report.print_stage(StageResult(active_stage, True, pointed, ""))

        active_stage = "next"
        if is_sample:
            report.print_stage(StageResult(active_stage, True, "the sibling's stores are outside every backup set", ""))
        else:
            report.print_stage(StageResult(active_stage, True, f"{name}: serving generation promoted", ""))
            print(f"Next: run {report.command('backup run --full')} to snapshot both stores with the promoted generation.")
        return 0
    except KeyboardInterrupt:
        if flipped:
            detail = "interrupted; the generation is live; rerun to converge both aliases"
        elif active_stage == "flip":
            detail = "interrupted during promotion; read the record before retrying"
        else:
            detail = "interrupted; nothing was promoted or retired"
        return _refuse(active_stage, Problem(
            detail,
            f"Run {report.command(f'index report {name}')}, then run {report.command(command_path)} again.",
        ), command_path)
    finally:
        if claim is not None:
            backuplock.release_claim(io, claim, lock=backuplock.CORPUS_LOCK)
        if engine_lock is not None and engine_lock.state is backuplock.State.HELD:
            backuplock.release(io, lock=backuplock.ENGINE_LOCK)
