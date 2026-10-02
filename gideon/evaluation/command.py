"""The ordered host command for loading, running, and gating an eval slice."""

import argparse
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast
from uuid import uuid4

import gideon
from gideon.evaluation import (
    challenger,
    ranked,
    rankmetrics,
    record,
    reference,
    stacks,
    window,
)
from gideon.evaluation import decision as decision_stats
from gideon.evaluation.evalset import (
    SET_ROOT,
    EvalSetLoadResult,
    Finding,
    LoadedSet,
    load_set,
    print_findings,
    select_cases,
)
from gideon.evaluation.results import CaseResult, JSONValue, RunContext, SliceResult
from gideon.evaluation.slices import SLICE_RUNNERS, SliceSpec
from gideon.evaluation.turns import access, door, run
from gideon.host import backuplock, courts, engine, nogpu, site, stack
from gideon.host.report import Problem, StageResult, print_stage, refusal
from gideon.host.sysio import Host, LockingHost, PathLike, RealHost

_COMMAND: Final[str] = "eval run"
NIGHTLY_KIND: Final[str] = "nightly"
NIGHTLY_LOCK_POLL_SECONDS: Final[int] = 60
_SLICE_FIX: Final[str] = "Run gideon eval run --slice extraction."
_LOAD_FIX: Final[str] = "Correct every listed eval-set finding, then retry."
_NO_GPU_FIX: Final[str] = "Run the evaluation on a GPU host, then retry."
_UNSIGNED_RESULT_FIX: Final[str] = "The runner must select through the loader, then retry."
_WINDOW_START_FIX: Final[str] = "Start the run at the window's opening, then retry."
_LOSES_FIX: Final[str] = "A change that loses is not adopted; keep the default."
_PARTIAL_DETAIL: Final[str] = "partial: aborted at the window's end, nothing kept"


@dataclass(frozen=True, slots=True)
class _EnginePreconditions:
    """The engine and record inputs established before grading starts."""

    hardware_profile: str
    served_model_name: str
    provenance: tuple[str | None, bool | None]
    site_config: site.SiteConfig
    turns: access.TurnAccess | None
    started: datetime
    end: datetime
    forced: bool


@dataclass(slots=True)
class _EngineLockClaim:
    """Whether this command, rather than a caller, took the engine lock."""

    taken: bool = False


@dataclass(frozen=True, slots=True)
class _RecordOutcome:
    """Whether the record stage refused, and whether it wrote rows at all.

    A skipped record — a set outside the release, an unreachable database —
    leaves the command running but writes no run row, so no later fix may
    name a run id that does not exist.
    """

    ok: bool
    written: bool


@dataclass(frozen=True, slots=True)
class _RunBodyOutcome:
    """The pass exit code and whether its row can anchor another pass."""

    exit_code: int
    written: bool = False
    partial: bool = False


@dataclass(frozen=True, slots=True)
class _RunRepeats:
    """The kept results and counts needed to report a bounded run."""

    result: SliceResult
    requested_repeats: int
    completed_repeats: int
    overrun: datetime | None

    @property
    def partial(self) -> bool:
        """Whether the run reached its deadline before every repeat completed."""

        return self.overrun is not None


def load_set_with_courts(
    set_root: Path,
    *,
    court_path: Path,
    host: Host | None,
) -> EvalSetLoadResult:
    court_result = courts.load_court_map(court_path, host=host)
    if not court_result.ok or court_result.court_map is None:
        details = "; ".join(error.problem for error in court_result.errors)
        return EvalSetLoadResult(
            findings=(
                Finding(
                    court_path.as_posix(),
                    None,
                    0,
                    details or "court map could not be loaded",
                    "Restore courts.yaml from the release checkout, then retry.",
                ),
            )
        )
    return load_set(set_root, court_result.court_map.courts)


def _run_slice(
    spec: SliceSpec, loaded: LoadedSet, slice_name: str, context: RunContext
) -> SliceResult:
    return spec.runner(loaded, slice_name, context)


def _run_repeats(
    spec: SliceSpec,
    loaded: LoadedSet,
    slice_name: str,
    context: RunContext,
    *,
    decision_run: bool,
) -> _RunRepeats:
    """Call the runner inside the run's deadline and keep only completed calls.

    A decision run calls the runner once per repeat, relabelling each call's
    results with its repeat number, since the runner itself runs one repeat;
    an ordinary run is one call whose results keep the runner's numbering. The
    checkpoint is called before and after every call, so a call whose last turn
    crossed the deadline is discarded rather than kept: work past the window's
    end never reaches a record. The overrun ends the loop here and is never
    re-raised, so the completed repeats are still recorded and gated.
    """

    requested = decision_stats.DECISION_REPEATS if decision_run else spec.repeats
    calls = requested if decision_run else 1
    completed: list[SliceResult] = []
    kept_results: list[CaseResult] = []
    report_lines: list[str] = []
    overrun: datetime | None = None

    for repeat in range(1, calls + 1):
        try:
            context.checkpoint()
            result = _run_slice(spec, loaded, slice_name, context)
            context.checkpoint()
        except window.WindowOverrun as exc:
            overrun = exc.end
            break
        completed.append(result)
        if decision_run:
            kept_results.extend(replace(case, repeat=repeat) for case in result.results)
            verdict = "pass" if result.verdict else "fail"
            report_lines.append(f"repeat {repeat} of {requested}: {verdict}\n")
            report_lines.append(result.report)
            if result.report and not result.report.endswith("\n"):
                report_lines.append("\n")

    if decision_run:
        # Zero tolerance holds on every repeat: one failing repeat fails the run.
        return _RunRepeats(
            SliceResult(
                verdict=bool(completed) and all(repeat.verdict for repeat in completed),
                report="".join(report_lines),
                results=tuple(kept_results),
            ),
            requested,
            len(completed),
            overrun,
        )
    if completed:
        return _RunRepeats(completed[0], requested, requested, None)
    return _RunRepeats(SliceResult(False, "", ()), requested, 0, overrun)


def _gate(slice_result: SliceResult, slice_spec: SliceSpec, *, partial: bool) -> bool:
    """Print the gate from only the slice verdict and the spec's gate texts.

    The gate of a slice that keeps no reference; ``_reference_gate`` is the
    other. The tripwire in ``tests/test_judge_never_gates.py`` reads this
    function's attributes and keeps score, metrics, judge fields, and case
    results out of the gate. A partial run kept nothing, so its row names the
    abort rather than a verdict the slice never reached.
    """

    if partial:
        print_stage(StageResult("gate", False, _PARTIAL_DETAIL, _WINDOW_START_FIX))
        return False
    if slice_result.verdict:
        print_stage(StageResult("gate", True, slice_spec.gate_pass, ""))
        return True
    print_stage(StageResult("gate", False, slice_spec.gate_fail, slice_spec.gate_fix))
    return False


def _compare_reference(
    loaded: LoadedSet,
    slice_name: str,
    slice_result: SliceResult,
    *,
    checkout: Path,
    host: Host,
) -> tuple[reference.Comparison, str]:
    """Compare the run with the slice's committed reference and print its lines.

    Returns the comparison and the eval-set version the reference names, empty
    when no reference file was read. The comparison is folded from each
    result's case id, repeat, and verdict alone.
    """

    reference_result = reference.read_reference(
        checkout,
        slice_name,
        loaded.slice_lists[slice_name],
        loaded.version,
        host=host,
    )
    if reference_result.findings:
        print_findings(reference_result.findings)
        comparison = reference.Comparison("malformed", None, (), (), (), (), False)
    else:
        current = reference.fold_repeats(
            tuple(
                (result.case_id, result.repeat, result.verdict)
                for result in slice_result.results
            )
        )
        comparison = reference.compare_reference(
            reference_result.reference,
            current,
            loaded.version,
        )

    reference_files = () if reference_result.reference is None else reference_result.reference.files
    reference_version = reference_files[0].eval_set_version if reference_files else ""
    print(f"reference: {comparison.outcome}")
    if reference_files:
        print(f"reference tag {reference_files[0].tag}")
        print(f"reference version {reference_version}")
    for name, ids in (
        ("regressed", comparison.regressed),
        ("gained", comparison.gained),
        ("new", comparison.new),
        ("dropped", comparison.dropped),
    ):
        if ids:
            print(f"{name} {' '.join(ids)}")
    return comparison, reference_version


def _reference_detail(
    comparison: reference.Comparison,
    *,
    slice_name: str,
    set_version: str,
    reference_version: str,
    run_id: str,
    written: bool,
) -> str:
    """Word the reference half of a gate row from the comparison alone.

    Shared by ``_reference_gate`` and ``_decision_gate`` and walked by the
    tripwire beside them, since a gate's reads include its helpers'.
    """

    if comparison.outcome == "absent":
        return f"no reference for {slice_name}"
    if comparison.outcome == "malformed":
        return "reference malformed"
    if comparison.outcome == "other-version":
        return f"reference is for {reference_version}, not {set_version}"
    if comparison.regressed:
        count = len(comparison.regressed)
        noun = "regression" if count == 1 else "regressions"
        return f"{count} {noun} against {comparison.tag}"
    detail = f"no regression against {comparison.tag}"
    if comparison.outcome == "stale" and written:
        detail += f"; re-record with gideon eval reference --run {run_id}"
    return detail


def _reference_fix(
    comparison: reference.Comparison,
    slice_spec: SliceSpec,
    *,
    slice_name: str,
    run_id: str,
    written: bool,
    command_flags: str,
    challenger_mode: bool = False,
) -> str:
    """Return the fix for a failing reference half, or empty when it held."""

    if comparison.regressed:
        return reference.REGRESSION_FIX
    if comparison.outcome == "other-version":
        if written:
            return f"Run sudo python3 -m gideon eval reference --run {run_id} as root with the stack up, then retry."
        return _record_root_fix(
            slice_name, slice_spec, command_flags, challenger_mode=challenger_mode
        )
    if comparison.outcome == "malformed":
        return reference.SLICE_REPAIR_FIX
    return ""


def _reference_gate(
    slice_result: SliceResult,
    slice_spec: SliceSpec,
    comparison: reference.Comparison,
    *,
    slice_name: str,
    set_version: str,
    reference_version: str,
    run_id: str,
    written: bool,
    command_flags: str,
    partial: bool,
    challenger_mode: bool = False,
) -> bool:
    """Print the gate of a slice that compares against its reference.

    Walked by ``tests/test_judge_never_gates.py`` beside ``_gate``: beyond the
    slice verdict and the spec's gate texts it reads the comparison alone,
    which ``_compare_reference`` folds from per-case verdicts.
    """

    if partial:
        print_stage(StageResult("gate", False, _PARTIAL_DETAIL, _WINDOW_START_FIX))
        return False
    bounds_ok = slice_result.verdict
    comparison_refused = comparison.outcome in {"other-version", "malformed"}
    gate_verdict = bounds_ok and not comparison.regressed and not comparison_refused

    bounds_detail = slice_spec.gate_pass if bounds_ok else slice_spec.gate_fail
    reference_detail = _reference_detail(
        comparison,
        slice_name=slice_name,
        set_version=set_version,
        reference_version=reference_version,
        run_id=run_id,
        written=written,
    )
    fixes: list[str] = []
    if not bounds_ok:
        fixes.append(slice_spec.gate_fix)
    reference_fix = _reference_fix(
        comparison,
        slice_spec,
        slice_name=slice_name,
        run_id=run_id,
        written=written,
        command_flags=command_flags,
        challenger_mode=challenger_mode,
    )
    if reference_fix:
        fixes.append(reference_fix)

    print_stage(
        StageResult(
            "gate",
            gate_verdict,
            f"{bounds_detail}; {reference_detail}",
            "; ".join(fixes),
        )
    )
    return gate_verdict


def _decision_gate(
    slice_result: SliceResult,
    slice_spec: SliceSpec,
    comparison: reference.Comparison,
    paired: decision_stats.PairedDecision,
    run_result: _RunRepeats,
    *,
    slice_name: str,
    set_version: str,
    reference_version: str,
    run_id: str,
    written: bool,
    command_flags: str,
) -> bool:
    """Print the gate of a decision run: bounds, reference, decision, completion.

    Walked by ``tests/test_judge_never_gates.py`` beside the other two gates:
    beyond the reference gate's reads it reads the decision's verdict word and
    the run's repeat counts alone, never a paired figure, so an interval can
    reject a change but no judge-derived value can reach the gate. The slice
    verdict is already every repeat's, so a bound broken on any one repeat
    fails the run whatever the interval says. The fix is the first failing
    half's.
    """

    bounds_ok = slice_result.verdict
    reference_ok = not comparison.regressed and comparison.outcome not in {
        "other-version",
        "malformed",
    }
    loses = paired.verdict == decision_stats.LOSES
    gate_verdict = bounds_ok and reference_ok and not loses and not run_result.partial

    bounds_detail = slice_spec.gate_pass if bounds_ok else slice_spec.gate_fail
    reference_detail = _reference_detail(
        comparison,
        slice_name=slice_name,
        set_version=set_version,
        reference_version=reference_version,
        run_id=run_id,
        written=written,
    )
    detail = f"{bounds_detail}; {reference_detail}; decision {paired.verdict}"
    if run_result.partial:
        detail += (
            f"; partial: aborted at the window's end after {run_result.completed_repeats} "
            f"of {run_result.requested_repeats} repeats"
        )

    if not bounds_ok:
        fix = slice_spec.gate_fix
    elif not reference_ok:
        fix = _reference_fix(
            comparison,
            slice_spec,
            slice_name=slice_name,
            run_id=run_id,
            written=written,
            command_flags=command_flags,
        )
    elif loses:
        fix = _LOSES_FIX
    elif run_result.partial:
        fix = _WINDOW_START_FIX
    else:
        fix = ""

    print_stage(StageResult("gate", gate_verdict, detail, fix))
    return gate_verdict


def _git_fix(checkout: str) -> str:
    return (
        f"Run git -c safe.directory={checkout} -C {checkout} rev-parse HEAD and "
        f"git -c safe.directory={checkout} -C {checkout} status --porcelain as the checkout owner, then retry."
    )


def git_argv(checkout: PathLike, *arguments: str) -> list[str]:
    """Build one git probe argv with the checkout's safe-directory grant."""

    checkout_text = str(checkout)
    return [
        "git",
        "-c",
        f"safe.directory={checkout_text}",
        "-C",
        checkout_text,
        *arguments,
    ]


def _provenance(
    io: Host, checkout: Path
) -> tuple[tuple[str | None, bool | None] | None, str | None]:
    """Read commit and dirty state, or return a record-stage refusal."""

    checkout_text = str(checkout)
    try:
        if not io.exists(checkout / ".git"):
            return (None, None), None
        commit = io.run(git_argv(checkout, "rev-parse", "HEAD"))
    except (OSError, subprocess.SubprocessError):
        return None, _git_fix(checkout_text)
    if commit.returncode != 0 or not commit.stdout.strip():
        return None, _git_fix(checkout_text)

    try:
        status = io.run(git_argv(checkout, "status", "--porcelain"))
    except (OSError, subprocess.SubprocessError):
        return None, _git_fix(checkout_text)
    if status.returncode != 0:
        return None, _git_fix(checkout_text)
    return (commit.stdout.strip(), bool(status.stdout.splitlines())), None


def _retry_command(
    slice_name: str,
    command_flags: str = "",
    *,
    challenger_mode: bool = False,
    ranked_flag: str = "",
) -> str:
    """Build the command that retries the selected evaluation mode."""

    if challenger_mode:
        return f"eval run --challenger --stack ci{command_flags}"
    return f"eval run --slice {slice_name}{ranked_flag}{command_flags}"


def _engine_root_fix(
    slice_name: str, command_flags: str, *, challenger_mode: bool = False
) -> str:
    return f"Run sudo python3 -m gideon {_retry_command(slice_name, command_flags, challenger_mode=challenger_mode)}, then retry."


def _waits_for_engine_lock(kind: str) -> bool:
    """Manual refuses a holder, smoke passes its caller's nested lock, nightly waits."""

    return kind == NIGHTLY_KIND


def _waited_duration(started: datetime, finished: datetime) -> str:
    """Describe the time spent waiting for the engine lock in hours and minutes."""

    seconds = max(0, int((finished - started).total_seconds()))
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours} h {minutes} min"


def _holder_text(holder: backuplock.Record | None) -> str:
    # A holder between its flock and its write has no record yet, and is waited on.
    if holder is None:
        return "another gideon command (record unreadable)"
    return f"{holder.command} (pid {holder.pid})"


def _record_root_fix(
    slice_name: str,
    slice_spec: SliceSpec,
    command_flags: str = "",
    *,
    challenger_mode: bool = False,
) -> str:
    ranked_flag = " --ranked <file>" if slice_spec.takes_ranked else ""
    return (
        f"Run sudo python3 -m gideon {_retry_command(slice_name, command_flags, challenger_mode=challenger_mode, ranked_flag=ranked_flag)} "
        "as root with the stack up, then retry."
    )


def _ranked_required_fix(slice_name: str, command_flags: str) -> str:
    return f"Run gideon eval run --slice {slice_name} --ranked <file>{command_flags}, then retry."


def _ranked_forbidden_fix(slice_name: str) -> str:
    return f"Remove --ranked when running --slice {slice_name}, then retry."


def _paired_flag_problem(
    slice_name: str, *, decision: bool, against: object, kind: str
) -> Problem | None:
    if decision and (not isinstance(against, str) or not against):
        return Problem(
            "--decision requires --against",
            f"Run gideon eval run --slice {slice_name} --decision --against <run id>, then retry.",
        )
    if against is not None and not decision:
        return Problem("--against requires --decision", "Remove --against or add --decision, then retry.")
    if decision and kind != "manual":
        # A decision run's record carries the word decision, so another word would be lost.
        return Problem(
            f"--decision records its own kind, not {kind!r}",
            f"Remove --kind {kind} when running --decision, then retry.",
        )
    return None


def _flag_problem(
    slice_name: str,
    slice_spec: SliceSpec | None,
    *,
    decision: bool,
    force: bool,
) -> Problem | None:
    """Refuse a flag the named slice cannot honour.

    A slice no registry entry names is left to ``load``, whose refusal lists
    the slices the set has, so a flag never masks the real mistake.
    """

    if slice_spec is None:
        return None
    if decision and slice_spec.decision is None:
        names = ", ".join(sorted(name for name, spec in SLICE_RUNNERS.items() if spec.decision))
        return Problem(
            f"slice {slice_name!r} has no decision metric",
            f"Choose a decision slice ({names}), then run gideon eval run --slice <name> "
            "--decision --against <run id> and retry.",
        )
    if force and not slice_spec.reaches_engine:
        return Problem(
            f"--force is not valid for slice {slice_name!r}, which does not reach the engine",
            f"Remove --force when running --slice {slice_name}, then retry.",
        )
    return None


def _read_comparand(
    io: Host,
    rendered_dir: PathLike,
    loaded: LoadedSet,
    slice_name: str,
    run_id: str,
) -> record.ComparandRun | Problem:
    """Read the recorded run a decision pairs against, or the refusal.

    Pairing is by case id, so the comparand must be a run of the same slice
    over the same eval-set version; a differing set digest is reported by the
    stage, not refused, since a case's text never changes under its id.
    """

    comparand, problem = record.read_run_metrics(io, rendered_dir, run_id)
    if problem is not None:
        return problem
    assert comparand is not None
    if comparand.slice != slice_name:
        return Problem(
            f"comparand run {run_id} is for slice {comparand.slice!r}, not {slice_name!r}",
            f"Pass --against the id of a recorded {slice_name} run, then retry.",
        )
    if comparand.eval_set_version != loaded.version:
        return Problem(
            f"comparand run {run_id} is for eval-set {comparand.eval_set_version}, not {loaded.version}",
            f"Pass --against the id of a recorded {slice_name} run over {loaded.version}, then retry.",
        )
    return comparand


def _paired_decision(
    loaded: LoadedSet,
    slice_result: SliceResult,
    slice_spec: SliceSpec,
    comparand: record.ComparandRun,
    *,
    requested_repeats: int,
    completed_repeats: int,
) -> decision_stats.PairedDecision:
    metric = slice_spec.decision
    assert metric is not None
    candidate = decision_stats.per_case_values(
        ((result.case_id, result.repeat, result.metrics) for result in slice_result.results),
        metric,
    )
    against = decision_stats.per_case_values(
        (
            (case_id, repeat, cast(Mapping[str, JSONValue], metrics))
            for case_id, repeat, metrics in comparand.results
        ),
        metric,
    )
    paired_ids = candidate.keys() & against.keys()
    clusters = {
        case_id: cast(str, loaded.cases_by_id[case_id]["cluster_id"])
        for case_id in paired_ids
    }
    return decision_stats.paired_decision(
        candidate,
        against,
        clusters,
        metric,
        against=comparand.run_id,
        requested_repeats=requested_repeats,
        completed_repeats=completed_repeats,
        digest_equal=comparand.set_digest == loaded.digest,
    )


def _engine_preconditions(
    io: Host,
    rendered_dir: PathLike,
    *,
    checkout: Path,
    models_path: PathLike,
    site_path: PathLike,
    started: datetime,
    supplied_set: bool,
    decision: bool,
    kind: str,
    force: bool,
    slice_name: str,
    slice_spec: SliceSpec,
    loaded: LoadedSet,
    paths: stacks.StackPaths,
    command_flags: str,
    lock_claim: _EngineLockClaim,
    sleep: Callable[[float], None],
    clock: Callable[[], datetime],
    challenger_mode: bool = False,
    side_prompt_id: str | None = None,
) -> _EnginePreconditions | None:
    """Refuse engine runs before a request when a required seam is unavailable."""

    if io.geteuid() != 0:
        print_stage(
            StageResult(
                "preconditions",
                False,
                "root privileges are required",
                _engine_root_fix(
                    slice_name, command_flags, challenger_mode=challenger_mode
                ),
            )
        )
        return None
    if nogpu.is_no_gpu_host(io):
        print_stage(
            StageResult(
                "preconditions",
                False,
                "the no-GPU marker refuses an engine-reaching slice",
                _NO_GPU_FIX,
            )
        )
        return None
    site_result = site.load_site(Path(site_path), host=io)
    if not site_result.ok or site_result.config is None:
        detail = site.render_errors(site_result.errors)
        fix = site_result.errors[0].fix if site_result.errors else "Create a valid site file, then retry."
        print_stage(StageResult("preconditions", False, detail, fix))
        return None
    config = site_result.config
    judgement = (
        window.decision_judgement(started, config.office.timezone)
        if decision
        else (
            window.nightly_judgement(started, config.office.timezone)
            if kind == NIGHTLY_KIND
            else window.window_judgement(started, config.office.timezone)
        )
    )
    engine_call_count = (
        None
        if slice_spec.engine_calls is None
        else slice_spec.engine_calls(loaded, slice_name)
    )
    # The any-hour allowance sizes a person's ordinary run, not a decision
    # repeat or a scheduled nightly run.
    waived = (
        not decision
        and kind != NIGHTLY_KIND
        and not judgement.inside
        and engine_call_count is not None
        and engine_call_count <= run.SMOKE_TURNS
    )
    if engine_call_count is None:
        calls_detail = ""
    elif waived:
        calls_detail = (
            f"{engine_call_count} engine calls within the any-hour allowance of {run.SMOKE_TURNS}, "
        )
    else:
        calls_detail = f"{engine_call_count} engine calls, "
    if not judgement.inside and not waived and not force:
        over = (
            ""
            if engine_call_count is None
            else f"; {engine_call_count} engine calls exceed the any-hour allowance of {run.SMOKE_TURNS}"
        )
        # The weekend judgement's description already says which window it missed.
        detail = (
            judgement.description
            if decision
            else f"outside the quiet window: {judgement.description}"
        ) + over
        fix = (
            f"Next opening is {judgement.next_opening.isoformat()}{over}; "
            "add --force for an announced window."
        )
        print_stage(
            StageResult(
                "preconditions",
                False,
                detail,
                fix,
            )
        )
        return None

    lock_command = "gideon " + _retry_command(
        slice_name,
        command_flags if challenger_mode else f" --stack {paths.name}",
        challenger_mode=challenger_mode,
    )
    effective_start = started
    lock_outcome = backuplock.take(
        cast(LockingHost, io),
        command=lock_command,
        now=effective_start,
        lock=backuplock.ENGINE_LOCK,
    )
    waited_for_lock = (
        lock_outcome.state is backuplock.State.REFUSED and _waits_for_engine_lock(kind)
    )
    first_holder = lock_outcome.holder
    if waited_for_lock:
        since = (
            ""
            if first_holder is None
            else f" since {first_holder.started.isoformat()}"
        )
        print(
            f"waiting for the engine lock held by {_holder_text(first_holder)}{since}; "
            f"polling every {NIGHTLY_LOCK_POLL_SECONDS} s until {judgement.end.isoformat()}"
        )
        while lock_outcome.state is backuplock.State.REFUSED:
            sleep(NIGHTLY_LOCK_POLL_SECONDS)
            effective_start = clock()
            if effective_start >= judgement.end:
                print_stage(
                    StageResult(
                        "preconditions",
                        False,
                        f"engine lock held by {_holder_text(lock_outcome.holder)} through the night's end "
                        f"{judgement.end.isoformat()}; waited {_waited_duration(started, effective_start)}",
                        "The next nightly fires at 21:00 office time; run this suite by hand "
                        "inside the window with "
                        f"sudo python3 -m gideon {_retry_command(slice_name, command_flags if challenger_mode else '', challenger_mode=challenger_mode)}.",
                    )
                )
                return None
            lock_outcome = backuplock.take(
                cast(LockingHost, io),
                command=lock_command,
                now=effective_start,
                lock=backuplock.ENGINE_LOCK,
            )
    if lock_outcome.state is backuplock.State.REFUSED:
        assert lock_outcome.problem is not None
        print_stage(
            StageResult(
                "preconditions",
                False,
                lock_outcome.problem.problem,
                lock_outcome.problem.fix,
            )
        )
        return None
    if lock_outcome.state is backuplock.State.HELD:
        lock_claim.taken = True
        lock_detail = "engine lock taken"
        if waited_for_lock:
            lock_detail += (
                f" after {_waited_duration(started, effective_start)} "
                f"behind {_holder_text(first_holder)}"
            )
    else:
        lock_detail = "engine lock held by this process"

    target = engine.resolve_engine_target(
        io,
        rendered_dir,
        hardware_profile=config.hardware_profile,
        models_path=models_path,
        sleep=sleep,
    )
    if isinstance(target, Problem):
        print_stage(StageResult("preconditions", False, target.problem, target.fix))
        return None

    if paths.name == "ci" and not io.exists(paths.turns_dir / "compose.yaml"):
        print_stage(
            StageResult(
                "preconditions",
                False,
                "the CI sibling's Compose file is unavailable",
                "Run sudo python3 -m tools.cistack up, then retry.",
            )
        )
        return None

    turns: access.TurnAccess | None = None
    if slice_spec.drives_turns:
        password = access.read_eval_password(io)
        if isinstance(password, Problem):
            print_stage(StageResult("preconditions", False, password.problem, password.fix))
            return None
        probe = door.probe(
            io,
            paths.turns_dir,
            served_name=target.served_model_name,
            max_time=run.TURN_TIMEOUT_SECONDS,
        )
        if probe.problem is not None:
            print_stage(
                StageResult("preconditions", False, probe.problem.problem, probe.problem.fix)
            )
            return None
        turns = access.TurnAccess(
            password=password,
            client_factory=access.make_client_factory(
                config.hostname,
                stack=paths.name,
                timeout=run.TURN_TIMEOUT_SECONDS,
            ),
            sentinel=run.new_sentinel(),
        )

    provenance: tuple[str | None, bool | None] = (None, None)
    if not supplied_set:
        resolved_provenance, provenance_fix = _provenance(io, checkout)
        if resolved_provenance is None:
            print_stage(
                StageResult(
                    "preconditions",
                    False,
                    "git provenance could not be read",
                    provenance_fix or _git_fix(str(checkout)),
                )
            )
            return None
        provenance = resolved_provenance
        probe_problem = record.probe(io, rendered_dir)
        if probe_problem is not None:
            print_stage(
                StageResult(
                    "preconditions",
                    False,
                    probe_problem,
                    stack.logs_fix(rendered_dir, record.POSTGRES_SERVICE),
                )
            )
            return None
    # One row for the stage, as engine verify prints one. The writer clause
    # states the guarantee and no more: the probe proves the role connects, so a
    # write can still fail after the run and is reported at the record stage.
    prompt_id = side_prompt_id if challenger_mode else slice_spec.judge_prompt
    prompt_id = prompt_id or "none"
    window_detail = judgement.description + (", forced" if force else "")
    writer = (
        "set supplied, so no writer probe"
        if supplied_set
        else f"{record.EVAL_ROLE} connects (the insert is not proven)"
    )
    print_stage(
        StageResult(
            "preconditions",
            True,
            f"root, no-GPU marker absent, site, {window_detail}, "
            + f"{calls_detail}{lock_detail}, "
            f"profile {target.profile_name}, served model {target.served_model_name}, "
            f"prompt {prompt_id}, {writer}"
            + (
                ", eval password read, door probed"
                if slice_spec.drives_turns
                else ""
            ),
            "",
        )
    )
    return _EnginePreconditions(
        target.profile_name,
        target.served_model_name,
        provenance,
        config,
        turns,
        effective_start,
        judgement.end,
        force,
    )


def _record(
    loaded: LoadedSet,
    slice_name: str,
    slice_result: SliceResult,
    *,
    started: datetime,
    finished: datetime,
    checkout: Path,
    host: Host,
    rendered_dir: PathLike,
    site_path: PathLike,
    supplied_set: bool,
    run_id: str,
    gate_verdict: bool,
    stack_name: str,
    kind: str,
    command_flags: str,
    forced: bool,
    partial: bool,
    requested_repeats: int,
    decision_json: Mapping[str, JSONValue] | None,
    slice_spec: SliceSpec,
    prepared: _EnginePreconditions | None,
    overrides: Mapping[str, object] = {},
    challenger_mode: bool = False,
) -> _RecordOutcome:
    """Write the run and its results.

    *prepared* is the engine path's already-resolved profile, provenance, and
    site file, whose ``preconditions`` stage also probed the writer; ``None`` is
    the engine-free path, which resolves each of them here, in the order and
    with the rows it has always printed.
    """

    if supplied_set:
        print_stage(
            StageResult(
                "record",
                True,
                "skipped — a set outside the release is never recorded",
                "",
            )
        )
        return _RecordOutcome(True, False)

    if prepared is None:
        probe_problem = record.probe(host, rendered_dir)
        if probe_problem is not None:
            print_stage(
                StageResult(
                    "record",
                    True,
                    f"skipped — no database reachable; rows were not written ({probe_problem})",
                    _record_root_fix(
                        slice_name,
                        slice_spec,
                        command_flags,
                        challenger_mode=challenger_mode,
                    ),
                )
            )
            return _RecordOutcome(True, False)

    site_config = None if prepared is None else prepared.site_config
    if site_config is None:
        site_result = site.load_site(Path(site_path), host=host)
        if not site_result.ok or site_result.config is None:
            detail = "; ".join(error.problem for error in site_result.errors)
            fix = site_result.errors[0].fix if site_result.errors else "Create a valid site file, then retry."
            print_stage(StageResult("record", False, f"site file could not be loaded: {detail}", fix))
            return _RecordOutcome(False, False)
        site_config = site_result.config

    provenance = None if prepared is None else prepared.provenance
    if provenance is None:
        resolved_provenance, provenance_fix = _provenance(host, checkout)
        if resolved_provenance is None:
            print_stage(
                StageResult(
                    "record",
                    False,
                    "git provenance could not be read; rows were not written",
                    provenance_fix or _git_fix(str(checkout)),
                )
            )
            return _RecordOutcome(False, False)
        provenance = resolved_provenance
    git_sha, git_dirty = provenance
    resolved_hardware_profile = (
        site_config.hardware_profile if prepared is None else prepared.hardware_profile
    )
    run = record.RunRow(
        run_id=run_id,
        started_at=started,
        finished_at=finished,
        product_version=gideon.__version__,
        corpus_lockfile=None,
        eval_set_version=loaded.version,
        hardware_profile=resolved_hardware_profile,
        stack=stack_name,
        generation_id=None,
        kind=kind,
        slice=slice_name,
        overrides=overrides,
        repeats=requested_repeats,
        git_sha=git_sha,
        git_dirty=git_dirty,
        set_digest=loaded.digest,
        verdict="pass" if gate_verdict else "fail",
        forced=forced,
        partial=partial,
        decision=decision_json,
    )
    results = tuple(
        record.ResultRow(
            run_id=run_id,
            run_started_at=started,
            case_id=result.case_id,
            repeat=result.repeat,
            verdict=result.verdict,
            metrics=result.metrics,
            judge=result.judge,
            provenance_ref=None,
            latency_ms=result.latency_ms,
        )
        for result in slice_result.results
    )
    write_problem = record.write_rows(host, rendered_dir, run, results)
    if write_problem is not None:
        print_stage(
            StageResult(
                "record",
                False,
                f"run {run_id} and {len(results)} result rows were not written ({write_problem})",
                stack.logs_fix(rendered_dir, record.POSTGRES_SERVICE),
            )
        )
        return _RecordOutcome(False, False)
    print_stage(
        StageResult(
            "record",
            True,
            f"run {run_id} recorded (1 run row, {len(results)} result rows)",
            "",
        )
    )
    return _RecordOutcome(True, True)


def _run_body(
    args: argparse.Namespace,
    *,
    set_root: Path,
    court_path: Path,
    host: Host,
    checkout: Path,
    rendered_dir: PathLike,
    site_path: PathLike,
    started: datetime,
    run_id: str,
    supplied_set: bool,
    finished_clock: Callable[[], datetime],
    models_path: PathLike,
    sleep: Callable[[float], None],
    paths: stacks.StackPaths,
    kind: str,
    command_flags: str,
    lock_claim: _EngineLockClaim,
    challenger_mode: bool = False,
    side_slice: str | None = None,
    side_prompt_id: str | None = None,
    subject_change: Callable[[RunContext, str], RunContext] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> _RunBodyOutcome:
    decision = bool(getattr(args, "decision", False))
    force = bool(getattr(args, "force", False))
    against = getattr(args, "against", None)
    slice_name = side_slice if challenger_mode else getattr(args, "slice", None)
    slice_label = slice_name if isinstance(slice_name, str) and slice_name else "<name>"
    paired_problem = _paired_flag_problem(
        slice_label,
        decision=decision,
        against=against,
        kind=kind,
    )
    if paired_problem is not None:
        print(refusal(_COMMAND, paired_problem.problem, paired_problem.fix), file=sys.stderr)
        return _RunBodyOutcome(1)
    if not isinstance(slice_name, str) or not slice_name:
        print(refusal(_COMMAND, "no slice was selected", _SLICE_FIX), file=sys.stderr)
        return _RunBodyOutcome(1)

    slice_spec = SLICE_RUNNERS.get(slice_name)
    flag_problem = _flag_problem(
        slice_name,
        slice_spec,
        decision=decision,
        force=force,
    )
    if flag_problem is not None:
        print(refusal(_COMMAND, flag_problem.problem, flag_problem.fix), file=sys.stderr)
        return _RunBodyOutcome(1)

    loaded_result = load_set_with_courts(set_root, court_path=court_path, host=host)
    if loaded_result.findings:
        print_findings(loaded_result.findings)
        print_stage(StageResult("load", False, "eval set refused", _LOAD_FIX))
        return _RunBodyOutcome(1)
    loaded = loaded_result.loaded
    if loaded is None:
        print_stage(StageResult("load", False, "eval set was not loaded", _LOAD_FIX))
        return _RunBodyOutcome(1)
    if slice_name not in loaded.slices:
        available = ", ".join(sorted(loaded.slices)) or "none"
        print_stage(
            StageResult(
                "load",
                False,
                f"unknown slice {slice_name!r}; available: {available}",
                _SLICE_FIX,
            )
        )
        return _RunBodyOutcome(1)

    if slice_spec is None:
        selected = loaded.slices[slice_name]
        print_stage(
            StageResult(
                "load",
                True,
                f"{loaded.version}: {len(loaded.cases_by_id)} cases, {len(selected)} in {slice_name}, digest {loaded.digest}",
                "",
            )
        )
        print_stage(
            StageResult(
                "run",
                False,
                f"no runner serves slice {slice_name!r}",
                "Implement the runner named by the slice, then retry.",
            )
        )
        return _RunBodyOutcome(1)

    if paths.name == "ci" and not slice_spec.drives_turns and slice_spec.judge_prompt is None:
        print_stage(
            StageResult(
                "load",
                False,
                f"slice {slice_name} cannot run on the CI sibling: the slice reaches nothing a stack names",
                "Run this slice with --stack production, then retry.",
            )
        )
        return _RunBodyOutcome(1)

    selected = loaded.slices[slice_name]
    print_stage(
        StageResult(
            "load",
            True,
            f"{loaded.version}: {len(loaded.cases_by_id)} cases, {len(selected)} in {slice_name}, digest {loaded.digest}",
            "",
        )
    )

    ranked_lists: Mapping[str, tuple[rankmetrics.Coordinates, ...]] | None = None
    run_overrides: Mapping[str, object] = {} if overrides is None else overrides
    ranked_path = getattr(args, "ranked", None)
    if slice_spec.takes_ranked:
        if not isinstance(ranked_path, str) or not ranked_path:
            print_stage(
                StageResult(
                    "ranked",
                    False,
                    f"slice {slice_name} requires --ranked",
                    _ranked_required_fix(slice_name, command_flags),
                )
            )
            return _RunBodyOutcome(1)
        active_ids = set(loaded.active_ids)
        allowed_ids = tuple(case_id for case_id in selected if case_id in active_ids)
        ranked_result = ranked.read(ranked_path, allowed_ids)
        if not ranked_result.ok:
            print_findings(ranked_result.findings)
            print_stage(
                StageResult("ranked", False, "ranked file refused", ranked.RANKED_FIX)
            )
            return _RunBodyOutcome(1)
        assert ranked_result.ranked is not None and ranked_result.sha256 is not None
        ranked_lists = ranked_result.ranked
        run_overrides = {
            "judgments": {
                "definition": rankmetrics.DEFINITION_ID,
                "ranked_sha256": ranked_result.sha256,
            }
        }
        passage_count = sum(len(values) for values in ranked_lists.values())
        print_stage(
            StageResult(
                "ranked",
                True,
                f"{len(ranked_lists)} queries, {passage_count} passages, SHA-256 {ranked_result.sha256}",
                "",
            )
        )
    elif ranked_path is not None:
        print_stage(
            StageResult(
                "ranked",
                False,
                f"slice {slice_name} does not take --ranked",
                _ranked_forbidden_fix(slice_name),
            )
        )
        return _RunBodyOutcome(1)

    engine_preconditions: _EnginePreconditions | None = None
    if slice_spec.reaches_engine:
        engine_preconditions = _engine_preconditions(
            host,
            rendered_dir,
            checkout=checkout,
            models_path=models_path,
            site_path=site_path,
            started=started,
            supplied_set=supplied_set,
            decision=decision,
            kind=kind,
            force=force,
            slice_name=slice_name,
            slice_spec=slice_spec,
            loaded=loaded,
            paths=paths,
            command_flags=command_flags,
            lock_claim=lock_claim,
            sleep=sleep,
            clock=finished_clock,
            challenger_mode=challenger_mode,
            side_prompt_id=side_prompt_id,
        )
        if engine_preconditions is None:
            return _RunBodyOutcome(1)

    comparand: record.ComparandRun | None = None
    if decision:
        comparand_result = _read_comparand(
            host,
            rendered_dir,
            loaded,
            slice_name,
            cast(str, against),
        )
        if isinstance(comparand_result, Problem):
            print_stage(
                StageResult(
                    "comparand",
                    False,
                    comparand_result.problem,
                    comparand_result.fix,
                )
            )
            return _RunBodyOutcome(1)
        comparand = comparand_result
        repeats_word = "repeat" if comparand.repeats == 1 else "repeats"
        repeats_detail = f"{comparand.repeats} {repeats_word}"
        if comparand.partial:
            # A partial comparand pairs what it holds, so the row says how much.
            completed = len({repeat for _case_id, repeat, _metrics in comparand.results})
            repeats_detail = f"{completed} of {comparand.repeats} {repeats_word}, partial"
        digest = "equal" if comparand.set_digest == loaded.digest else "differs"
        print_stage(
            StageResult(
                "comparand",
                True,
                f"run {comparand.run_id}: {comparand.slice}, {comparand.eval_set_version}, "
                f"{repeats_detail}, {len(comparand.results)} result rows, "
                f"set digest {digest}",
                "",
            )
        )

    context = RunContext(
        host=host,
        rendered_dir=paths.turns_dir,
        engine_dir=rendered_dir,
        served_model_name=(
            None
            if engine_preconditions is None
            else engine_preconditions.served_model_name
        ),
        judge_prompt_id=slice_spec.judge_prompt,
        repeats=slice_spec.repeats,
        progress=print,
        ranked=ranked_lists,
        turns=None if engine_preconditions is None else engine_preconditions.turns,
    )
    if subject_change is not None:
        assert side_prompt_id is not None
        context = subject_change(context, side_prompt_id)
    if engine_preconditions is not None:
        context = replace(
            context,
            checkpoint=window.deadline_checkpoint(finished_clock, engine_preconditions.end),
        )
    run_result = _run_repeats(
        slice_spec,
        loaded,
        slice_name,
        context,
        decision_run=decision,
    )
    slice_result = run_result.result
    selection = select_cases(loaded, slice_name)
    # Any unsigned id, not the slice's alone: a runner that reached past its
    # own selection must not put the case it found into a gate's count either.
    unsigned_results = tuple(
        dict.fromkeys(
            result.case_id
            for result in slice_result.results
            if result.case_id in loaded.unsigned_ids
        )
    )
    if unsigned_results:
        print_stage(
            StageResult(
                "run",
                False,
                f"runner returned unsigned cases: {' '.join(unsigned_results)}",
                _UNSIGNED_RESULT_FIX,
            )
        )
        return _RunBodyOutcome(1)
    if decision or run_result.partial:
        cases = len({result.case_id for result in slice_result.results})
        run_detail = (
            f"{len(slice_result.results)} results over {cases} active cases, "
            f"{run_result.completed_repeats} of {run_result.requested_repeats} repeats completed"
        )
        if run_result.overrun is not None:
            run_detail += (
                f"; aborted at the window end {run_result.overrun.isoformat()}; "
                "the repeat in flight discarded"
            )
    # A repeated slice returns one result per case AND repeat, so counting the
    # results would call four gradings of two cases "four cases".
    elif slice_spec.repeats == 1:
        run_detail = f"{len(slice_result.results)} active cases evaluated"
    else:
        cases = len({result.case_id for result in slice_result.results})
        run_detail = (
            f"{len(slice_result.results)} results over {cases} active cases "
            f"at {slice_spec.repeats} repeats"
        )
    if selection.takes_signoff:
        run_detail += f", {len(selection.unsigned)} unsigned excluded"
    print_stage(StageResult("run", True, run_detail, ""))
    print(slice_result.report, end="")

    comparison: reference.Comparison | None = None
    reference_version = ""
    if slice_spec.compares_reference:
        comparison, reference_version = _compare_reference(
            loaded,
            slice_name,
            slice_result,
            checkout=checkout,
            host=host,
        )
    paired: decision_stats.PairedDecision | None = None
    if decision:
        assert comparand is not None
        paired = _paired_decision(
            loaded,
            slice_result,
            slice_spec,
            comparand,
            requested_repeats=run_result.requested_repeats,
            completed_repeats=run_result.completed_repeats,
        )
        print_stage(StageResult("decision", True, decision_stats.describe(paired), ""))
    # A refused comparison judges nothing, so the run row carries the slice
    # gate's verdict alone; the gate row still refuses, and its fix is the
    # writer's or the restore. Refusing at load would leave the first run of a
    # new set version unrecordable, and that run is the writer's own input.
    recorded_verdict = (
        slice_result.verdict
        and not (comparison is not None and comparison.regressed)
        and not (paired is not None and paired.verdict == decision_stats.LOSES)
        and not run_result.partial
    )

    recorded = _record(
        loaded,
        slice_name,
        slice_result,
        started=started if engine_preconditions is None else engine_preconditions.started,
        finished=finished_clock(),
        checkout=checkout,
        host=host,
        rendered_dir=rendered_dir,
        site_path=site_path,
        supplied_set=supplied_set,
        run_id=run_id,
        gate_verdict=recorded_verdict,
        stack_name=paths.name,
        kind="decision" if decision else kind,
        command_flags=command_flags,
        forced=False if engine_preconditions is None else engine_preconditions.forced,
        partial=run_result.partial,
        requested_repeats=run_result.requested_repeats,
        decision_json=None if paired is None else decision_stats.to_json(paired),
        slice_spec=slice_spec,
        prepared=engine_preconditions,
        overrides=run_overrides,
        challenger_mode=challenger_mode,
    )
    if decision:
        assert paired is not None
        assert comparison is not None
        gate_ok = _decision_gate(
            slice_result,
            slice_spec,
            comparison,
            paired,
            run_result,
            slice_name=slice_name,
            set_version=loaded.version,
            reference_version=reference_version,
            run_id=run_id,
            written=recorded.written,
            command_flags=command_flags,
        )
    elif comparison is None:
        gate_ok = _gate(slice_result, slice_spec, partial=run_result.partial)
    else:
        gate_ok = _reference_gate(
            slice_result,
            slice_spec,
            comparison,
            slice_name=slice_name,
            set_version=loaded.version,
            reference_version=reference_version,
            run_id=run_id,
            written=recorded.written,
            command_flags=command_flags,
            partial=run_result.partial,
            challenger_mode=challenger_mode,
        )
    return _RunBodyOutcome(
        0 if gate_ok and recorded.ok else 1,
        written=recorded.written,
        partial=run_result.partial,
    )


def _side_overrides(
    entry: challenger.ChallengerEntry, side: str, value: str, *, pairs: str | None = None
) -> Mapping[str, object]:
    """The run row's ``overrides`` for one side of a challenger pair."""

    fields: dict[str, object] = {
        challenger.NAME_FIELD: entry.name,
        challenger.SUBJECT_FIELD: entry.subject,
        challenger.SIDE_FIELD: side,
        challenger.VALUE_FIELD: value,
    }
    if pairs is not None:
        fields[challenger.PAIRS_FIELD] = pairs
    return {challenger.OVERRIDE_KEY: fields}


def _challenger_flag_problem(args: argparse.Namespace) -> Problem | None:
    """Refuse flags that cannot describe a committed challenger pair."""

    kind = getattr(args, "kind", "manual")
    flags = (f" --kind {kind}" if kind == NIGHTLY_KIND else "") + (
        " --force" if getattr(args, "force", False) else ""
    )
    fix = f"Run gideon {_retry_command('', flags, challenger_mode=True)}, then retry."
    for name in ("slice", "set", "ranked", "decision", "against"):
        value = getattr(args, name, None)
        if value is not None and value is not False:
            return Problem(f"--challenger cannot be combined with --{name}", fix)
    if getattr(args, "stack", "production") != "ci":
        return Problem("--challenger requires --stack ci", fix)
    if kind == "smoke":
        return Problem("--challenger cannot use --kind smoke", fix)
    return None


def run_eval(
    args: argparse.Namespace,
    *,
    host: Host | None = None,
    checkout_root: PathLike | None = None,
    rendered_dir: PathLike = "/etc/gideon/rendered",
    site_path: PathLike = "/etc/gideon/site.yaml",
    clock: Callable[[], datetime] | None = None,
    run_id_factory: Callable[[], str] | None = None,
    court_path: PathLike | None = None,
    models_path: PathLike | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run ``eval run``'s ordered stages and return the exit code."""

    checkout = Path(__file__).parents[2] if checkout_root is None else Path(checkout_root)
    io = RealHost() if host is None else host
    now = (lambda: datetime.now(UTC)) if clock is None else clock
    new_run_id = (lambda: str(uuid4())) if run_id_factory is None else run_id_factory
    challenger_mode = bool(getattr(args, "challenger", False))
    entry: challenger.ChallengerEntry | None = None
    subject: challenger.ChallengerSubject | None = None
    if challenger_mode:
        flag_problem = _challenger_flag_problem(args)
        if flag_problem is not None:
            print(refusal(_COMMAND, flag_problem.problem, flag_problem.fix), file=sys.stderr)
            return 1
        if not nogpu.is_build_box(io):
            print_stage(
                StageResult(
                    "challenger",
                    False,
                    nogpu.NOT_BUILD_BOX_DETAIL,
                    "The challenger runs on the build box alone; on this box run "
                    "sudo python3 -m gideon eval run --slice <name> instead.",
                )
            )
            return 1
        challenger_result = challenger.load_challenger(
            checkout / challenger.CHALLENGER_PATH, host=io
        )
        if challenger_result.findings:
            print(challenger.render_findings(challenger_result.findings), file=sys.stderr)
            print_stage(
                StageResult(
                    "challenger",
                    False,
                    "committed challenger refused",
                    challenger_result.findings[0].fix,
                )
            )
            return 1
        assert challenger_result.config is not None
        entry = challenger_result.config.challenger
        if entry is None:
            print_stage(
                StageResult(
                    "challenger",
                    True,
                    "skipped — none set; nothing was evaluated and nothing recorded",
                    "",
                )
            )
            return 0
        subject = next(item for item in challenger.SUBJECTS if item.name == entry.subject)
        print_stage(
            StageResult(
                "challenger",
                True,
                f"{entry.name}: subject {entry.subject}, slice {subject.slice_name}, "
                f"release {entry.release}, challenger {entry.challenger}",
                "",
            )
        )
    stack_name = getattr(args, "stack", "production")
    kind = getattr(args, "kind", "manual")
    paths = stacks.resolve_stack(stack_name, rendered_dir)
    command_flags = ("" if challenger_mode else paths.flag_fragment) + (
        f" --kind {kind}" if kind != "manual" else ""
    )
    against = getattr(args, "against", None)
    if getattr(args, "decision", False) and isinstance(against, str) and against:
        command_flags += f" --decision --against {against}"
    if getattr(args, "force", False):
        command_flags += " --force"
    actual_models = checkout / "models.lock" if models_path is None else models_path
    supplied_root = getattr(args, "set", None)
    set_root = checkout / SET_ROOT if supplied_root is None else Path(supplied_root)
    selected_court_path = courts.default_courts_path() if court_path is None else Path(court_path)
    lock_claim = _EngineLockClaim()

    def run_pass(
        run_id: str,
        *,
        side_slice: str | None = None,
        side_prompt_id: str | None = None,
        subject_change: Callable[[RunContext, str], RunContext] | None = None,
        overrides: Mapping[str, object] | None = None,
    ) -> _RunBodyOutcome:
        return _run_body(
            args,
            set_root=set_root,
            court_path=selected_court_path,
            host=io,
            checkout=checkout,
            rendered_dir=rendered_dir,
            site_path=site_path,
            started=now(),
            run_id=run_id,
            supplied_set=supplied_root is not None,
            finished_clock=now,
            models_path=actual_models,
            sleep=sleep,
            paths=paths,
            kind=kind,
            command_flags=command_flags,
            lock_claim=lock_claim,
            challenger_mode=challenger_mode,
            side_slice=side_slice,
            side_prompt_id=side_prompt_id,
            subject_change=subject_change,
            overrides=overrides,
        )

    try:
        if not challenger_mode:
            return run_pass(new_run_id()).exit_code

        assert entry is not None and subject is not None
        release_id, challenger_id = new_run_id(), new_run_id()
        print_stage(
            StageResult(
                "side", True, f"release run {release_id}: {entry.release}", ""
            )
        )
        release_outcome = run_pass(
            release_id,
            side_slice=subject.slice_name,
            side_prompt_id=entry.release,
            overrides=_side_overrides(entry, challenger.RELEASE_SIDE, entry.release),
        )
        if not release_outcome.written or release_outcome.partial:
            print_stage(
                StageResult(
                    "side",
                    False,
                    f"challenger run {challenger_id} did not start: release run {release_id} "
                    "was not recorded or was partial, so nothing can pair",
                    _engine_root_fix(
                        subject.slice_name, command_flags, challenger_mode=True
                    ),
                )
            )
            return 1

        print_stage(
            StageResult(
                "side",
                True,
                f"challenger run {challenger_id}: {entry.challenger}, pairs {release_id}",
                "",
            )
        )
        challenger_outcome = run_pass(
            challenger_id,
            side_slice=subject.slice_name,
            side_prompt_id=entry.challenger,
            subject_change=subject.change,
            overrides=_side_overrides(
                entry, challenger.CHALLENGER_SIDE, entry.challenger, pairs=release_id
            ),
        )
        return 0 if release_outcome.exit_code == challenger_outcome.exit_code == 0 else 1
    finally:
        # A nested pass holds nothing of its own; the real host keys one descriptor per path.
        if lock_claim.taken:
            backuplock.release(cast(LockingHost, io), lock=backuplock.ENGINE_LOCK)
