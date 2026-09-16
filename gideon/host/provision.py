"""The two-phase host provisioning runner."""

import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

from gideon.host.lock import HostLockLoadResult, load_host_lock, render_errors
from gideon.host.nogpu import (
    BUILD_BOX_ONLY_FIX,
    BUILD_BOX_PATH,
    NO_GPU_PATH,
    NOT_BUILD_BOX_DETAIL,
    build_box_declaration_problem,
    declaration_problem,
    declare,
    declare_build_box,
    is_build_box,
    is_no_gpu_host,
)
from gideon.host.report import Problem, command_detail, refusal
from gideon.host.site import SiteError, SiteLoadResult, load_site
from gideon.host.steps import (
    SITE_MISSING_FIX,
    STEPS,
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    StepFailure,
)
from gideon.host.sysio import Host, PathLike, RealHost


class StepOutcome(Enum):
    """The operator-facing outcome for one provisioning step."""

    OK = "ok"
    APPLIED = "applied"
    WOULD_APPLY = "would-apply"
    BLOCKED = "blocked"
    REBOOT_REQUIRED = "reboot-required"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class _StepState:
    outcome: StepOutcome
    result: CheckResult | None = None


_SITE_PATH: Final = "/etc/gideon/site.yaml"
_ROOT_FIX: Final = "Run gideon host provision as root, for example with sudo."


def _one_line(value: str) -> str:
    return " ".join(value.splitlines())


def _load_context(
    host: Host, lock_path: PathLike, site_path: PathLike
) -> ProvisionContext | None:
    lock_result: HostLockLoadResult = load_host_lock(lock_path, host=host)
    if not lock_result.ok or lock_result.lock is None:
        print(render_errors(lock_result.errors), file=sys.stderr)
        return None

    # An absent site file is the runbook's expected first-pass state (site
    # steps report blocked); a present-but-unloadable one refuses the run.
    if not host.exists(site_path):
        return ProvisionContext(host=host, lock=lock_result.lock, site=None)
    site_result: SiteLoadResult = load_site(Path(site_path), host=host)
    if site_result.errors or site_result.config is None:
        print(_render_site_errors(site_result.errors), file=sys.stderr)
        return None
    return ProvisionContext(
        host=host,
        lock=lock_result.lock,
        site=site_result.config,
    )


def _render_site_errors(errors: Sequence[SiteError]) -> str:
    return "\n".join(
        refusal("host provision", error.problem, error.fix) for error in errors
    )


def _site_blocked(step: Step, context: ProvisionContext) -> CheckResult | None:
    if step.needs_site and context.site is None:
        return CheckResult(
            disposition=Disposition.PENDING_INPUT,
            detail="site file is required by this step",
            fix=SITE_MISSING_FIX,
        )
    return None


def check_step(step: Step, context: ProvisionContext) -> CheckResult:
    """Run one step check with the provisioning exception guard."""

    blocked = _site_blocked(step, context)
    if blocked is not None:
        return blocked
    try:
        return step.check(context)
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - CLI boundary
        return CheckResult(
            disposition=Disposition.UNFIXABLE,
            detail=f"check raised {type(exc).__name__}: {exc}",
            fix=f"Inspect the {step.name} check and re-run provision.",
        )


def _child_failure(exc: subprocess.CalledProcessError) -> str:
    """What a failed child said: the row must explain itself, not name a class."""

    completed = subprocess.CompletedProcess(
        exc.cmd, exc.returncode, exc.stdout or "", exc.stderr or ""
    )
    argv = exc.cmd if isinstance(exc.cmd, str) else " ".join(map(str, exc.cmd))
    return f"{argv} exited {exc.returncode}: {command_detail(completed)}"


def _apply(
    step: Step, context: ProvisionContext
) -> tuple[CheckResult | None, str | None]:
    try:
        announcement = step.apply(context)
    except StepFailure as exc:
        return CheckResult(Disposition.UNFIXABLE, exc.detail, exc.fix), None
    except subprocess.CalledProcessError as exc:
        return (
            CheckResult(
                disposition=Disposition.UNFIXABLE,
                detail=f"apply failed: {_child_failure(exc)}",
                fix=f"Repair the {step.name} apply failure and re-run provision.",
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - CLI boundary
        return (
            CheckResult(
                disposition=Disposition.UNFIXABLE,
                detail=f"apply raised {type(exc).__name__}: {exc}",
                fix=f"Repair the {step.name} apply failure and re-run provision.",
            ),
            None,
        )
    return None, announcement


def _disposition_outcome(result: CheckResult) -> StepOutcome:
    if result.disposition is Disposition.CONVERGED:
        return StepOutcome.OK
    if result.disposition is Disposition.PENDING_INPUT:
        return StepOutcome.BLOCKED
    if result.disposition is Disposition.REBOOT_REQUIRED:
        return StepOutcome.REBOOT_REQUIRED
    return StepOutcome.FAILED


def _run_step(
    step: Step, context: ProvisionContext, *, dry_run: bool
) -> tuple[_StepState, bool, str | None]:
    result = check_step(step, context)
    if result.disposition is Disposition.DRIFT:
        halts_run = result.halts_run
        if dry_run:
            return _StepState(StepOutcome.WOULD_APPLY, result), halts_run, None
        apply_error, announcement = _apply(step, context)
        if apply_error is not None:
            return (
                _StepState(StepOutcome.FAILED, apply_error),
                halts_run or apply_error.halts_run,
                None,
            )
        result = check_step(step, context)
        if result.disposition is Disposition.CONVERGED:
            return (
                _StepState(StepOutcome.APPLIED, result),
                halts_run or result.halts_run,
                announcement,
            )
        outcome = _disposition_outcome(result)
        return _StepState(outcome, result), halts_run or result.halts_run, None
    outcome = _disposition_outcome(result)
    return _StepState(outcome, result), result.halts_run, None


def _render_step(step: Step, state: _StepState) -> None:
    detail = state.result.detail if state.result is not None else "prerequisite failed"
    line = f"{step.name}: {state.outcome.value} — {_one_line(detail)}"
    if state.result is not None and state.result.fix:
        line += f" Fix: {_one_line(state.result.fix)}"
    print(line)


def _render_summary(states: Sequence[_StepState]) -> None:
    counts = {
        outcome: sum(state.outcome is outcome for state in states)
        for outcome in StepOutcome
    }
    parts = [
        f"{counts[outcome]} {outcome.value}"
        for outcome in StepOutcome
        if counts[outcome]
    ]
    print(f"Summary: {len(states)} step(s); {', '.join(parts) or 'no steps'}.")


def _prerequisite_closure(
    target: Step, by_name: dict[str, Step]
) -> tuple[list[Step], str | None]:
    visited: set[str] = set()
    visiting: set[str] = set()
    ordered: list[Step] = []

    def visit(step: Step) -> str | None:
        if step.name in visiting:
            return f"prerequisite cycle includes {step.name!r}"
        if step.name in visited:
            return None
        visiting.add(step.name)
        for requirement in step.requires:
            prerequisite = by_name.get(requirement)
            if prerequisite is None:
                return f"step {step.name!r} requires unknown step {requirement!r}"
            error = visit(prerequisite)
            if error is not None:
                return error
        visiting.remove(step.name)
        visited.add(step.name)
        ordered.append(step)
        return None

    return ordered, visit(target)


def _only_fix(step: Step, result: CheckResult) -> str:
    if result.disposition is Disposition.DRIFT:
        return f"Run provision without --only, or --only {step.name} first."
    if result.fix:
        return result.fix
    if result.disposition is Disposition.REBOOT_REQUIRED:
        return "Reboot the host, then re-run provision."
    if result.disposition is Disposition.PENDING_INPUT:
        return SITE_MISSING_FIX
    return f"Apply the manual remediation for {step.name}, then re-run provision."


def _run_only(
    target: Step,
    registry: Sequence[Step],
    context: ProvisionContext,
    *,
    dry_run: bool,
    no_gpu: bool,
    build_box: bool,
) -> int:
    by_name = {step.name: step for step in registry}
    closure, error = _prerequisite_closure(target, by_name)
    if error is not None:
        print(
            refusal(
                "host provision",
                f"cannot run --only {target.name}: {error}",
                "Repair the step registry.",
            ),
            file=sys.stderr,
        )
        return 1

    for prerequisite in closure[:-1]:
        if no_gpu and prerequisite.gpu_host_only:
            continue
        if not build_box and prerequisite.build_box_only:
            continue
        result = check_step(prerequisite, context)
        if result.disposition is not Disposition.CONVERGED:
            print(
                refusal(
                    "host provision",
                    f"cannot run --only {target.name}: prerequisite "
                    f"{prerequisite.name} is {result.disposition.value}",
                    _only_fix(prerequisite, result),
                ),
                file=sys.stderr,
            )
            return 1

    state, _, announcement = _run_step(target, context, dry_run=dry_run)
    _render_step(target, state)
    if announcement is not None:
        print(announcement)
    _render_summary([state])
    return int(state.outcome is StepOutcome.FAILED)


def _declare_no_gpu(io: Host, *, dry_run: bool) -> Problem | None:
    """Declare the mode (or say what would be declared) and print the one line."""

    problem = declaration_problem(io)
    if problem is not None:
        return problem
    if is_no_gpu_host(io):
        print(f"no-gpu: ok — already declared at {NO_GPU_PATH}")
        return None
    if dry_run:
        print(f"no-gpu: would declare — {NO_GPU_PATH}")
        return None
    problem = declare(io)
    if problem is None:
        print(f"no-gpu: declared — {NO_GPU_PATH}")
    return problem


def _declare_build_box(io: Host, *, dry_run: bool) -> Problem | None:
    """Declare the build-box mode (or say what would be declared)."""

    problem = build_box_declaration_problem(io)
    if problem is not None:
        return problem
    if is_build_box(io):
        print(f"build-box: ok — already declared at {BUILD_BOX_PATH}")
        return None
    if dry_run:
        print(f"build-box: would declare — {BUILD_BOX_PATH}")
        return None
    problem = declare_build_box(io)
    if problem is None:
        print(f"build-box: declared — {BUILD_BOX_PATH}")
    return problem


def _prerequisite_unmet(
    states: dict[str, _StepState],
    prerequisite: Step,
    *,
    no_gpu: bool,
    build_box: bool,
) -> bool:
    """Whether a prerequisite's outcome blocks its dependants.

    A step skipped for either host mode is not a failed prerequisite: the
    registry's kvm prerequisite, for example, is skipped with it when the
    host is not the build box.
    """

    outcome = states.get(prerequisite.name, _StepState(StepOutcome.OK)).outcome
    if outcome is StepOutcome.FAILED:
        return True
    if outcome is StepOutcome.SKIPPED:
        return not (
            (no_gpu and prerequisite.gpu_host_only)
            or (not build_box and prerequisite.build_box_only)
        )
    return False


def run_provision(
    args: object,
    *,
    host: Host | None = None,
    lock_path: PathLike | None = None,
    site_path: PathLike = _SITE_PATH,
    steps: Sequence[Step] | None = None,
) -> int:
    """Run the selected provisioning steps and return a shell exit status."""

    registry = tuple(STEPS if steps is None else steps)
    if bool(getattr(args, "list", False)):
        for step in registry:
            if step.gpu_host_only:
                marker = " [gpu-host-only]"
            elif step.build_box_only:
                marker = " [build-box-only]"
            else:
                marker = ""
            print(f"{step.name}: {step.summary}{marker}")
        return 0

    io = host or RealHost()
    if io.geteuid() != 0:
        print(
            refusal("host provision", "root is required", _ROOT_FIX),
            file=sys.stderr,
        )
        return 1

    selected = getattr(args, "only", None)
    by_name = {step.name: step for step in registry}
    if selected is not None and selected not in by_name:
        valid = ", ".join(by_name) or "(none)"
        print(
            refusal(
                "host provision",
                f"unknown provisioning step {selected!r}; valid steps: {valid}",
                "Choose one of the listed steps or omit --only.",
            ),
            file=sys.stderr,
        )
        return 1

    root = Path(__file__).parents[2]
    actual_lock_path = root / "host.lock" if lock_path is None else lock_path
    context = _load_context(io, actual_lock_path, site_path)
    if context is None:
        return 1

    dry_run = bool(getattr(args, "dry_run", False))
    no_gpu_flag = bool(getattr(args, "no_gpu", False))
    build_box_flag = bool(getattr(args, "build_box", False))
    if no_gpu_flag:
        # The declaration precedes every step: it is what the steps read.
        problem = _declare_no_gpu(io, dry_run=dry_run)
        if problem is not None:
            print(refusal("host provision", problem.problem, problem.fix), file=sys.stderr)
            return 1

    if build_box_flag:
        # The declaration precedes every step: it is what the steps read.
        problem = _declare_build_box(io, dry_run=dry_run)
        if problem is not None:
            print(refusal("host provision", problem.problem, problem.fix), file=sys.stderr)
            return 1

    no_gpu = is_no_gpu_host(io) or no_gpu_flag
    build_box = is_build_box(io) or build_box_flag
    if selected is not None:
        target = by_name[selected]
        if no_gpu and target.gpu_host_only:
            print(
                refusal(
                    "host provision",
                    f"step {target.name} is skipped on a no-GPU host",
                    f"Remove {NO_GPU_PATH} to leave the mode, or choose another step.",
                ),
                file=sys.stderr,
            )
            return 1
        if not build_box and target.build_box_only:
            print(
                refusal(
                    "host provision",
                    f"step {target.name} runs on the build box only",
                    BUILD_BOX_ONLY_FIX,
                ),
                file=sys.stderr,
            )
            return 1
        return _run_only(
            target,
            registry,
            context,
            dry_run=dry_run,
            no_gpu=no_gpu,
            build_box=build_box,
        )

    states: dict[str, _StepState] = {}
    ordered_states: list[_StepState] = []
    halted = False
    for step in registry:
        announcement: str | None = None
        if no_gpu and step.gpu_host_only:
            state = _StepState(
                StepOutcome.SKIPPED,
                CheckResult(
                    disposition=Disposition.CONVERGED,
                    detail="no-GPU host",
                    fix="",
                ),
            )
        elif not build_box and step.build_box_only:
            state = _StepState(
                StepOutcome.SKIPPED,
                CheckResult(
                    disposition=Disposition.CONVERGED,
                    detail=NOT_BUILD_BOX_DETAIL,
                    fix="",
                ),
            )
        elif halted:
            state = _StepState(
                StepOutcome.SKIPPED,
                CheckResult(
                    disposition=Disposition.PENDING_INPUT,
                    detail="run halted by an earlier step",
                    fix="Resolve the earlier failed step, then re-run provision.",
                ),
            )
        else:
            failed_requirement = next(
                (
                    requirement
                    for requirement in step.requires
                    if _prerequisite_unmet(
                        states,
                        by_name[requirement],
                        no_gpu=no_gpu,
                        build_box=build_box,
                    )
                ),
                None,
            )
            if failed_requirement is not None:
                state = _StepState(
                    StepOutcome.SKIPPED,
                    CheckResult(
                        disposition=Disposition.PENDING_INPUT,
                        detail=f"prerequisite {failed_requirement} failed",
                        fix=f"Resolve {failed_requirement}, then re-run provision.",
                    ),
                )
            else:
                state, halted, announcement = _run_step(
                    step, context, dry_run=dry_run
                )
        states[step.name] = state
        ordered_states.append(state)
        _render_step(step, state)
        if announcement is not None:
            print(announcement)
    _render_summary(ordered_states)
    return int(any(state.outcome is StepOutcome.FAILED for state in ordered_states))
