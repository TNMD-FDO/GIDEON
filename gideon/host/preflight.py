"""The two-phase host preflight runner and its release-artifact refusals.

Preflight refuses before running checks when the host lock, models lock, egress
allowlist, court map, corpus lockfile, or site cannot be read and validated.
Under the advisory reading, which upgrade's pre-checkout stage alone passes, an
unconverged provision step warns with the caller's fix instead of refusing.
"""

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from gideon.host import nogpu
from gideon.host.checks import (
    CHECKS,
    CheckReport,
    PreflightCheck,
    PreflightContext,
    Severity,
)
from gideon.host.corpus.lockfile import check_courts, read_lockfile_directory
from gideon.host.corpus.lockfile import render_errors as render_lockfile_errors
from gideon.host.courts import default_courts_path, load_court_map
from gideon.host.courts import render_errors as render_courts_errors
from gideon.host.egress import load_egress_allowlist
from gideon.host.egress import render_errors as render_egress_errors
from gideon.host.lock import load_host_lock
from gideon.host.lock import render_errors as render_lock_errors
from gideon.host.models import load_models_lock
from gideon.host.models import render_errors as render_models_errors
from gideon.host.nogpu import NOT_BUILD_BOX_DETAIL
from gideon.host.provision import check_step
from gideon.host.report import command, refusal
from gideon.host.site import load_site
from gideon.host.site import render_errors as render_site_errors
from gideon.host.steps import (
    SITE_MISSING_FIX,
    STEPS,
    Disposition,
    ProvisionContext,
    Step,
)
from gideon.host.sysio import PathLike, ReadBytesHost, RealHost

_SITE_PATH: Final = "/etc/gideon/site.yaml"


def _root_fix() -> str:
    return f"Run {command('preflight', sudo=False)} as root, for example with sudo."


@dataclass(frozen=True, slots=True)
class ObservedRow:
    """A printed preflight row and whether it came from a provision step."""

    name: str
    report: CheckReport
    provision_step: bool


def _one_line(value: str) -> str:
    return " ".join(value.splitlines())


def _render_report(name: str, report: CheckReport) -> None:
    line = f"{name}: {report.severity.value} — {_one_line(report.detail)}"
    if report.fix:
        line += f" Fix: {_one_line(report.fix)}"
    print(line)


def _render_summary(reports: Sequence[CheckReport]) -> None:
    counts = {
        severity: sum(report.severity is severity for report in reports)
        for severity in Severity
    }
    parts = [
        f"{counts[severity]} {severity.value}"
        for severity in Severity
        if counts[severity]
    ]
    print(f"Summary: {len(reports)} check(s); {', '.join(parts) or 'no checks'}.")


def _exception_report(name: str, exc: Exception) -> CheckReport:
    return CheckReport(
        Severity.REFUSE,
        f"check raised {type(exc).__name__}: {exc}",
        f"Inspect the {name} check and re-run preflight.",
    )


def _run_step_checks(
    steps: Sequence[Step],
    context: ProvisionContext,
    *,
    no_gpu: bool,
    build_box: bool,
    advisory_fix: str | None,
) -> list[tuple[str, CheckReport]]:
    reports: list[tuple[str, CheckReport]] = []
    for step in steps:
        if no_gpu and step.gpu_host_only:
            reports.append(
                (step.name, CheckReport(Severity.PASS, "skipped: no-GPU host"))
            )
            continue
        if not build_box and step.build_box_only:
            reports.append(
                (
                    step.name,
                    CheckReport(Severity.PASS, f"skipped: {NOT_BUILD_BOX_DETAIL}"),
                )
            )
            continue
        result = check_step(step, context)
        if result.disposition is Disposition.CONVERGED:
            report = CheckReport(Severity.PASS, result.detail, result.fix)
        elif advisory_fix is not None:
            report = CheckReport(Severity.WARN, result.detail, advisory_fix)
        else:
            report = CheckReport(Severity.REFUSE, result.detail, result.fix)
        reports.append((step.name, report))
    return reports


def _run_preflight_checks(
    checks: Sequence[PreflightCheck], context: PreflightContext
) -> list[tuple[str, CheckReport]]:
    reports: list[tuple[str, CheckReport]] = []
    for check in checks:
        try:
            report = check.run(context)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - CLI boundary
            report = _exception_report(check.name, exc)
        reports.append((check.name, report))
    return reports


def run_preflight(
    args: object,
    *,
    host: ReadBytesHost | None = None,
    lock_path: PathLike | None = None,
    models_path: PathLike | None = None,
    site_path: PathLike = _SITE_PATH,
    steps: Sequence[Step] | None = None,
    checks: Sequence[PreflightCheck] | None = None,
    egress_path: PathLike | None = None,
    courts_path: PathLike | None = None,
    lockfiles_path: PathLike | None = None,
    advisory_fix: str | None = None,
    observer: Callable[[ObservedRow], None] | None = None,
) -> int:
    """Run provisioning checks and install-time checks without applying state."""

    del args
    io = host or RealHost()
    if io.geteuid() != 0:
        print(
            refusal("preflight", "root is required", _root_fix()),
            file=sys.stderr,
        )
        return 1

    root = Path(__file__).parents[2]
    actual_lock_path = root / "host.lock" if lock_path is None else lock_path
    actual_models_path = root / "models.lock" if models_path is None else models_path
    actual_egress_path = (
        root / "config/egress.yaml" if egress_path is None else egress_path
    )
    actual_courts_path = default_courts_path() if courts_path is None else courts_path
    actual_lockfiles_path = root / "corpus/lockfiles" if lockfiles_path is None else lockfiles_path

    lock_result = load_host_lock(actual_lock_path, host=io)
    models_result = load_models_lock(actual_models_path, host=io)
    egress_result = load_egress_allowlist(actual_egress_path, host=io)
    courts_result = load_court_map(actual_courts_path, host=io)
    lockfiles_result = read_lockfile_directory(actual_lockfiles_path, host=io)
    lockfile = lockfiles_result.newest
    lockfile_errors = lockfiles_result.errors
    if lockfile is not None and courts_result.court_map is not None:
        lockfile_errors += check_courts(lockfile, courts_result.court_map)
    if lock_result.errors:
        print(render_lock_errors(lock_result.errors), file=sys.stderr)
    if models_result.errors:
        print(render_models_errors(models_result.errors), file=sys.stderr)
    if egress_result.errors:
        print(render_egress_errors(egress_result.errors), file=sys.stderr)
    if courts_result.errors:
        print(render_courts_errors(courts_result.errors), file=sys.stderr)
    if lockfile_errors:
        print(render_lockfile_errors(lockfile_errors), file=sys.stderr)
    if (
        lock_result.errors
        or lock_result.lock is None
        or models_result.errors
        or models_result.lock is None
        or egress_result.errors
        or egress_result.allowlist is None
        or courts_result.errors
        or courts_result.court_map is None
        or lockfile_errors
    ):
        return 1

    if not io.exists(site_path):
        print(
            refusal("preflight", f"site file is missing: {site_path}", SITE_MISSING_FIX),
            file=sys.stderr,
        )
        return 1

    site_result = load_site(Path(site_path), host=io)
    if site_result.errors or site_result.config is None:
        print(render_site_errors(site_result.errors), file=sys.stderr)
        return 1

    no_gpu = nogpu.is_no_gpu_host(io)
    provision_context = ProvisionContext(
        host=io,
        lock=lock_result.lock,
        site=site_result.config,
        models=models_result.lock,
        no_gpu=no_gpu,
    )
    preflight_context = PreflightContext(
        host=io,
        lock=lock_result.lock,
        models=models_result.lock,
        site=site_result.config,
        egress=egress_result.allowlist,
        courts=courts_result.court_map,
        no_gpu=no_gpu,
        build_box=nogpu.is_build_box(io),
        lockfile=lockfile,
    )

    step_reports = _run_step_checks(
        tuple(STEPS if steps is None else steps),
        provision_context,
        no_gpu=preflight_context.no_gpu,
        build_box=preflight_context.build_box,
        advisory_fix=advisory_fix,
    )
    check_reports = _run_preflight_checks(
        tuple(CHECKS if checks is None else checks), preflight_context
    )
    reports = step_reports + check_reports
    for index, (name, report) in enumerate(reports):
        _render_report(name, report)
        if observer is not None:
            observer(ObservedRow(name, report, index < len(step_reports)))
    _render_summary([report for _, report in reports])
    return int(any(report.severity is Severity.REFUSE for _, report in reports))
