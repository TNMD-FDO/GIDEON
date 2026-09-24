"""The ordered host command for loading, running, and gating an eval slice."""

import argparse
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import uuid4

import gideon
from gideon.evaluation import ranked, rankmetrics, record, reference, window
from gideon.evaluation.evalset import (
    SET_ROOT,
    EvalSetLoadResult,
    Finding,
    LoadedSet,
    load_set,
    print_findings,
    select_cases,
)
from gideon.evaluation.results import RunContext, SliceResult
from gideon.evaluation.slices import SLICE_RUNNERS, SliceSpec
from gideon.evaluation.turns import access, door, run
from gideon.host import courts, engine, nogpu, site, stack
from gideon.host.report import Problem, StageResult, print_stage, refusal
from gideon.host.sysio import Host, PathLike, RealHost

_COMMAND: Final[str] = "eval run"
_FLAG_FIX: Final[str] = "Run gideon eval run --slice extraction; decision runs land in a later release."
_SLICE_FIX: Final[str] = "Run gideon eval run --slice extraction."
_LOAD_FIX: Final[str] = "Correct every listed eval-set finding, then retry."
_NO_GPU_FIX: Final[str] = "Run the evaluation on a GPU host, then retry."
_UNSIGNED_RESULT_FIX: Final[str] = "The runner must select through the loader, then retry."


@dataclass(frozen=True, slots=True)
class _EnginePreconditions:
    """The engine and record inputs established before grading starts."""

    hardware_profile: str
    served_model_name: str
    provenance: tuple[str | None, bool | None]
    site_config: site.SiteConfig
    turns: access.TurnAccess | None


@dataclass(frozen=True, slots=True)
class _RecordOutcome:
    """Whether the record stage refused, and whether it wrote rows at all.

    A skipped record — a set outside the release, an unreachable database —
    leaves the command running but writes no run row, so no later fix may
    name a run id that does not exist.
    """

    ok: bool
    written: bool


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


def _gate(slice_result: SliceResult, slice_spec: SliceSpec) -> bool:
    """Print the gate from only the slice verdict and the spec's gate texts.

    The gate of a slice that keeps no reference; ``_reference_gate`` is the
    other. The tripwire in ``tests/test_judge_never_gates.py`` reads this
    function's attributes and keeps score, metrics, judge fields, and case
    results out of the gate.
    """

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
) -> bool:
    """Print the gate of a slice that compares against its reference.

    Walked by ``tests/test_judge_never_gates.py`` beside ``_gate``: beyond the
    slice verdict and the spec's gate texts it reads the comparison alone,
    which ``_compare_reference`` folds from per-case verdicts.
    """

    bounds_ok = slice_result.verdict
    comparison_refused = comparison.outcome in {"other-version", "malformed"}
    gate_verdict = bounds_ok and not comparison.regressed and not comparison_refused

    bounds_detail = slice_spec.gate_pass if bounds_ok else slice_spec.gate_fail
    if comparison.outcome == "absent":
        reference_detail = f"no reference for {slice_name}"
    elif comparison.outcome == "malformed":
        reference_detail = "reference malformed"
    elif comparison.outcome == "other-version":
        reference_detail = f"reference is for {reference_version}, not {set_version}"
    elif comparison.regressed:
        count = len(comparison.regressed)
        noun = "regression" if count == 1 else "regressions"
        reference_detail = f"{count} {noun} against {comparison.tag}"
    else:
        reference_detail = f"no regression against {comparison.tag}"
        if comparison.outcome == "stale" and written:
            reference_detail += f"; re-record with gideon eval reference --run {run_id}"

    fixes: list[str] = []
    if not bounds_ok:
        fixes.append(slice_spec.gate_fix)
    if comparison.regressed:
        fixes.append(reference.REGRESSION_FIX)
    elif comparison.outcome == "other-version":
        if written:
            fixes.append(
                f"Run sudo python3 -m gideon eval reference --run {run_id} as root with the stack up, then retry."
            )
        else:
            fixes.append(_record_root_fix(slice_name, slice_spec))
    elif comparison.outcome == "malformed":
        fixes.append(reference.SLICE_REPAIR_FIX)

    print_stage(
        StageResult(
            "gate",
            gate_verdict,
            f"{bounds_detail}; {reference_detail}",
            "; ".join(fixes),
        )
    )
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


def _engine_root_fix(slice_name: str) -> str:
    return f"Run sudo python3 -m gideon eval run --slice {slice_name}, then retry."


def _record_root_fix(slice_name: str, slice_spec: SliceSpec) -> str:
    ranked_flag = " --ranked <file>" if slice_spec.takes_ranked else ""
    return (
        f"Run sudo python3 -m gideon eval run --slice {slice_name}{ranked_flag} "
        "as root with the stack up, then retry."
    )


def _ranked_required_fix(slice_name: str) -> str:
    return f"Run gideon eval run --slice {slice_name} --ranked <file>, then retry."


def _ranked_forbidden_fix(slice_name: str) -> str:
    return f"Remove --ranked when running --slice {slice_name}, then retry."


def _engine_preconditions(
    io: Host,
    rendered_dir: PathLike,
    *,
    checkout: Path,
    models_path: PathLike,
    site_path: PathLike,
    started: datetime,
    supplied_set: bool,
    slice_name: str,
    slice_spec: SliceSpec,
    sleep: Callable[[float], None],
) -> _EnginePreconditions | None:
    """Refuse engine runs before a request when a required seam is unavailable."""

    if io.geteuid() != 0:
        print_stage(
            StageResult(
                "preconditions",
                False,
                "root privileges are required",
                _engine_root_fix(slice_name),
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
    judgement = window.window_judgement(started, config.office.timezone)
    if not judgement.inside:
        fix = (
            f"Next opening is {judgement.next_opening.isoformat()}; "
            "--force lands in a later release."
        )
        print_stage(
            StageResult(
                "preconditions",
                False,
                f"outside the quiet window: {judgement.description}",
                fix,
            )
        )
        return None

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

    turns: access.TurnAccess | None = None
    if slice_spec.drives_turns:
        instruction = access.load_general_instruction(
            io,
            site_path=site_path,
            root=checkout,
            stack="production",
            command="gideon eval run",
        )
        if isinstance(instruction, Problem):
            print_stage(
                StageResult("preconditions", False, instruction.problem, instruction.fix)
            )
            return None
        password = access.read_eval_password(io)
        if isinstance(password, Problem):
            print_stage(StageResult("preconditions", False, password.problem, password.fix))
            return None
        probe = door.probe(
            io,
            rendered_dir,
            served_name=target.served_model_name,
            max_time=run.TURN_TIMEOUT_SECONDS,
        )
        if probe.problem is not None:
            print_stage(
                StageResult("preconditions", False, probe.problem.problem, probe.problem.fix)
            )
            return None
        turns = access.TurnAccess(
            instruction=instruction,
            password=password,
            client_factory=access.make_client_factory(
                config.hostname,
                stack="production",
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
    prompt_id = slice_spec.judge_prompt or "none"
    writer = (
        "set supplied, so no writer probe"
        if supplied_set
        else f"{record.EVAL_ROLE} connects (the insert is not proven)"
    )
    print_stage(
        StageResult(
            "preconditions",
            True,
            f"root, no-GPU marker absent, site, {judgement.description}, "
            f"profile {target.profile_name}, served model {target.served_model_name}, "
            f"prompt {prompt_id}, {writer}"
            + (
                ", instruction rendered, eval password read, door probed"
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
    slice_spec: SliceSpec,
    prepared: _EnginePreconditions | None,
    overrides: Mapping[str, object] = {},
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
                    _record_root_fix(slice_name, slice_spec),
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
        stack="production",
        generation_id=None,
        kind="manual",
        slice=slice_name,
        overrides=overrides,
        repeats=slice_spec.repeats,
        git_sha=git_sha,
        git_dirty=git_dirty,
        set_digest=loaded.digest,
        verdict="pass" if gate_verdict else "fail",
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
) -> int:
    if getattr(args, "decision", False) or getattr(args, "force", False):
        print(refusal(_COMMAND, "decision and force flags are not implemented", _FLAG_FIX), file=sys.stderr)
        return 1

    slice_name = getattr(args, "slice", None)
    if not isinstance(slice_name, str) or not slice_name:
        print(refusal(_COMMAND, "no slice was selected", _SLICE_FIX), file=sys.stderr)
        return 1

    loaded_result = load_set_with_courts(set_root, court_path=court_path, host=host)
    if loaded_result.findings:
        print_findings(loaded_result.findings)
        print_stage(StageResult("load", False, "eval set refused", _LOAD_FIX))
        return 1
    loaded = loaded_result.loaded
    if loaded is None:
        print_stage(StageResult("load", False, "eval set was not loaded", _LOAD_FIX))
        return 1
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
        return 1

    selected = loaded.slices[slice_name]
    print_stage(
        StageResult(
            "load",
            True,
            f"{loaded.version}: {len(loaded.cases_by_id)} cases, {len(selected)} in {slice_name}, digest {loaded.digest}",
            "",
        )
    )

    slice_spec = SLICE_RUNNERS.get(slice_name)
    if slice_spec is None:
        print_stage(
            StageResult(
                "run",
                False,
                f"no runner serves slice {slice_name!r}",
                "Implement the runner named by the slice, then retry.",
            )
        )
        return 1

    ranked_lists: Mapping[str, tuple[rankmetrics.Coordinates, ...]] | None = None
    run_overrides: Mapping[str, object] = {}
    ranked_path = getattr(args, "ranked", None)
    if slice_spec.takes_ranked:
        if not isinstance(ranked_path, str) or not ranked_path:
            print_stage(
                StageResult(
                    "ranked",
                    False,
                    f"slice {slice_name} requires --ranked",
                    _ranked_required_fix(slice_name),
                )
            )
            return 1
        active_ids = set(loaded.active_ids)
        allowed_ids = tuple(case_id for case_id in selected if case_id in active_ids)
        ranked_result = ranked.read(ranked_path, allowed_ids)
        if not ranked_result.ok:
            print_findings(ranked_result.findings)
            print_stage(
                StageResult("ranked", False, "ranked file refused", ranked.RANKED_FIX)
            )
            return 1
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
        return 1

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
            slice_name=slice_name,
            slice_spec=slice_spec,
            sleep=sleep,
        )
        if engine_preconditions is None:
            return 1

    context = RunContext(
        host=host,
        rendered_dir=rendered_dir,
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
    slice_result = _run_slice(slice_spec, loaded, slice_name, context)
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
        return 1
    # A repeated slice returns one result per case AND repeat, so counting the
    # results would call four gradings of two cases "four cases".
    if slice_spec.repeats == 1:
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
    # A refused comparison judges nothing, so the run row carries the slice
    # gate's verdict alone; the gate row still refuses, and its fix is the
    # writer's or the restore. Refusing at load would leave the first run of a
    # new set version unrecordable, and that run is the writer's own input.
    recorded_verdict = slice_result.verdict and not (
        comparison is not None and comparison.regressed
    )

    recorded = _record(
        loaded,
        slice_name,
        slice_result,
        started=started,
        finished=finished_clock(),
        checkout=checkout,
        host=host,
        rendered_dir=rendered_dir,
        site_path=site_path,
        supplied_set=supplied_set,
        run_id=run_id,
        gate_verdict=recorded_verdict,
        slice_spec=slice_spec,
        prepared=engine_preconditions,
        overrides=run_overrides,
    )
    if comparison is None:
        gate_ok = _gate(slice_result, slice_spec)
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
        )
    return 0 if gate_ok and recorded.ok else 1


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
    run_id = str(uuid4()) if run_id_factory is None else run_id_factory()
    actual_models = checkout / "models.lock" if models_path is None else models_path
    supplied_root = getattr(args, "set", None)
    set_root = checkout / SET_ROOT if supplied_root is None else Path(supplied_root)
    selected_court_path = courts.default_courts_path() if court_path is None else Path(court_path)
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
    )
