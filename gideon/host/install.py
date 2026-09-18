"""The ordered receiving-office install command (§3.6 step 5).

Install is composition: every phase is an existing command run in-process,
each printing its own rows, followed by one phase row here.  The engine verify
runner receives an in-process ``observe`` callback so its rows can gate install;
install refuses to go live when that gate fails, and its failed audit row names
the phase.  Re-running it on a live box is safe because each nested command is
idempotent.
"""

import argparse
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from gideon.host import apply, backup, backupset, drill, engine, preflight, site, users
from gideon.host import audit as audit_module
from gideon.host.report import StageResult, print_stage, refusal
from gideon.host.stages import site_problem
from gideon.host.sysio import Host, LockingHost, PathLike, RealHost

_SITE_PATH: Final = "/etc/gideon/site.yaml"
_RENDERED_DIR: Final = "/etc/gideon/rendered"
_ROOT_FIX: Final = "Run sudo python3 -m gideon install, then retry."
_SITE_FIX: Final = "Correct /etc/gideon/site.yaml, then retry install."
_AUDIT_FIX: Final = "Run sudo python3 -m gideon apply, then retry install."
ENGINE_VERIFY_SKIPPED_DETAIL: Final = "engine verify skipped — no-GPU host (§2.5)"

Runner = Callable[[argparse.Namespace], int]


@dataclass(frozen=True, slots=True)
class _Phase:
    """One nested command: its phase name, the form an operator re-runs, its arguments."""

    name: str
    command_path: str
    child: argparse.Namespace


# The phases install runs in order; `audit` and `url` follow and are install's own.
_PHASES: Final[tuple[_Phase, ...]] = (
    _Phase("preflight", "preflight", argparse.Namespace(command_path="preflight")),
    _Phase("apply", "apply", argparse.Namespace(command_path="apply")),
    _Phase(
        "reconcile",
        "users reconcile --now",
        argparse.Namespace(command_path="users reconcile", now=True),
    ),
    _Phase(
        "engine-verify", "engine verify", argparse.Namespace(command_path="engine verify")
    ),
    _Phase(
        "backup",
        "backup run",
        argparse.Namespace(command_path="backup run", full=False, label=None),
    ),
    _Phase("drill", "backup drill", argparse.Namespace(command_path="backup drill")),
)


def _default_runners(
    io: LockingHost, *, site_path: PathLike, rendered_dir: PathLike
) -> dict[str, Runner]:
    """The real commands, each given the keyword arguments its signature takes."""

    return {
        "preflight": lambda child: preflight.run_preflight(
            child, host=io, site_path=site_path
        ),
        "apply": lambda child: apply.run_apply(
            child, host=io, site_path=site_path, rendered_dir=rendered_dir
        ),
        "reconcile": lambda child: users.run_reconcile(
            child, host=io, site_path=site_path, rendered_dir=rendered_dir
        ),
        "engine-verify": lambda child: engine.run_engine_verify(
            child,
            host=io,
            site_path=site_path,
            rendered_dir=rendered_dir,
            observe=getattr(child, "observe", None),
        ),
        "backup": lambda child: backup.run_backup_run(
            child, host=io, site_path=site_path, rendered_dir=rendered_dir
        ),
        "drill": lambda child: drill.run_backup_drill(
            child, host=io, site_path=site_path, rendered_dir=rendered_dir
        ),
    }


def _phase_result(phase: _Phase, code: int) -> StageResult:
    if code == 0:
        return StageResult(phase.name, True, f"{phase.command_path} completed", "")
    return StageResult(
        phase.name,
        False,
        f"{phase.command_path} refused (exit {code})",
        f"Run sudo python3 -m gideon {phase.command_path}, correct its refusal, "
        "then re-run install.",
    )


_GATE_FIX: Final = (
    "Do not go live. Run sudo python3 -m gideon engine verify, correct its refusal, "
    "then re-run install."
)


def _gate_result(phase: _Phase, code: int, observed: list[StageResult]) -> StageResult:
    """The engine-verify phase row, judged from the rows the nested command printed.

    The one skipped row is a no-GPU host; a clean run counts its checks; a failed
    run names its failed rows, so the operator reads the check from the phase row.
    An injected runner that observes nothing gets the generic phase forms.
    """

    if not observed:
        return _phase_result(phase, code)
    if len(observed) == 1 and observed[0].name == "engine" and observed[0].detail.startswith("skipped"):
        return StageResult(phase.name, True, ENGINE_VERIFY_SKIPPED_DETAIL, "")
    if code == 0:
        checks = sum(row.name not in {"preconditions", "audit"} for row in observed)
        return StageResult(phase.name, True, f"engine verify completed ({checks} checks ok)", "")
    failed_names = ", ".join(row.name for row in observed if not row.ok)
    return StageResult(
        phase.name, False, f"engine verify refused (exit {code}): {failed_names}", _GATE_FIX
    )


def _newest_label(io: Host) -> str | None:
    """The label of the set the backup phase just produced, when it can be listed."""

    try:
        sets = backupset.list_sets(io)
    except OSError:
        return None
    return next((ref.label for ref in sets if ref.complete), None)


def _row(run_id: str, hostname: str, phase: str, **detail: object) -> audit_module.AuditRow:
    return audit_module.AuditRow(
        run_id, "install", None, None, None, (), {"phase": phase, "hostname": hostname, **detail}
    )


def _audit_stage(
    audit_api: Any, io: Host, rendered_dir: PathLike, row: audit_module.AuditRow, detail: str
) -> StageResult:
    problem = audit_api.write_rows(io, rendered_dir, (row,))
    if problem is not None:
        return StageResult("audit", False, f"{detail}: {problem}", _AUDIT_FIX)
    return StageResult("audit", True, detail, "")


def _print_summary(rows: list[StageResult]) -> None:
    ok = sum(row.ok for row in rows)
    counts = f"{ok} ok" + (f", {len(rows) - ok} refuse" if ok < len(rows) else "")
    print(f"Summary: {len(rows)} phase(s); {counts}.")


def _refuse(problem: str, fix: str) -> int:
    print(refusal("install", problem, fix), file=sys.stderr)
    return 1


def run_install(
    args: argparse.Namespace,
    *,
    host: LockingHost | None = None,
    site_path: PathLike = _SITE_PATH,
    rendered_dir: PathLike = _RENDERED_DIR,
    runners: Mapping[str, Runner] | None = None,
    audit: Any | None = None,
) -> int:
    """Run install's eight ordered phases; exit 0 iff every phase is ok."""

    del args
    io = host or RealHost()
    audit_api = audit if audit is not None else audit_module
    if io.geteuid() != 0:
        return _refuse("root is required.", _ROOT_FIX)
    loaded = site.load_site(Path(site_path), host=io)
    if loaded.errors or loaded.config is None:
        return _refuse(site_problem(loaded) or "the site file is invalid.", _SITE_FIX)
    hostname = loaded.config.hostname
    phase_runners = (
        runners
        if runners is not None
        else _default_runners(io, site_path=site_path, rendered_dir=rendered_dir)
    )

    run_id = str(uuid.uuid4())
    rows: list[StageResult] = []
    durations: dict[str, float] = {}
    set_label: str | None = None
    # The writer exists only once apply has converged the stores: a failure before
    # that leaves no row, and the nested commands' own rows are the record.
    intent_recorded = False

    def show(result: StageResult) -> StageResult:
        print_stage(result)
        rows.append(result)
        return result

    def record(phase: str, detail: str, **extra: object) -> StageResult:
        row = _row(run_id, hostname, phase, durations=dict(durations), set_label=set_label, **extra)
        return _audit_stage(audit_api, io, rendered_dir, row, detail)

    for phase in _PHASES:
        started = time.monotonic()
        if phase.name == "engine-verify":
            # A fresh child per run carries the observer, so the shared phase table
            # is never mutated and the gate row is judged from this run's rows alone.
            observed: list[StageResult] = []
            child = argparse.Namespace(**vars(phase.child), observe=observed.append)
            result = show(_gate_result(phase, phase_runners[phase.name](child), observed))
        else:
            result = show(_phase_result(phase, phase_runners[phase.name](phase.child)))
        durations[phase.name] = round(time.monotonic() - started, 3)
        if not result.ok:
            if intent_recorded:
                show(record("failed", f"recorded the failure of {phase.name}", failed_phase=phase.name))
            _print_summary(rows)
            return 1
        if phase.name == "apply":
            # The intent row is silent when written: the audit row on stdout is the applied one.
            intent = record("intent", "install intent recorded")
            if not intent.ok:
                show(intent)
                _print_summary(rows)
                return 1
            intent_recorded = True
        elif phase.name == "backup":
            set_label = _newest_label(io)

    if not show(record("applied", "install applied")).ok:
        _print_summary(rows)
        return 1
    url = f"https://{hostname}/"
    show(StageResult("url", True, f"sign in at {url}", ""))
    _print_summary(rows)
    print(url)
    return 0
