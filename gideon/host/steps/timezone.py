"""Host timezone provisioning."""

from gideon.host.steps import (
    BOX_WIDE_SHORTFALL_FIX,
    BoxWideSetting,
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    StepFailure,
    box_wide_shortfall,
    site_required,
)

_SHOW_TIMEZONE = ("timedatectl", "show", "-p", "Timezone", "--value")
_DEFAULT_ZONES = ("Etc/UTC", "UTC")
_SYSTEMD_TIMEDATED_FIX = (
    "Ensure systemd-timedated is available and running, then re-run provision."
)


def _host_timezone(context: ProvisionContext) -> str | CheckResult:
    try:
        result = context.host.run(_SHOW_TIMEZONE)
    except OSError as exc:
        return CheckResult(
            Disposition.UNFIXABLE,
            f"timedatectl could not report the host timezone: {exc}",
            _SYSTEMD_TIMEDATED_FIX,
        )
    if result.returncode != 0:
        return CheckResult(
            Disposition.UNFIXABLE,
            "timedatectl could not report the host timezone",
            _SYSTEMD_TIMEDATED_FIX,
        )
    return result.stdout.strip()


class TimezoneStep(Step):
    """Set the box-wide zone only at its installed default, which all timers follow."""

    name = "timezone"
    summary = "set the host clock's timezone from office.timezone"
    needs_site = True
    settings = (BoxWideSetting("time zone"),)

    def check(self, context: ProvisionContext) -> CheckResult:
        if context.site is None:
            return site_required()

        actual = _host_timezone(context)
        if isinstance(actual, CheckResult):
            return actual
        expected = context.site.office.timezone
        if actual == expected:
            return CheckResult(
                Disposition.CONVERGED,
                f"host timezone is {actual}",
                "",
            )
        if actual in _DEFAULT_ZONES:
            return CheckResult(
                Disposition.DRIFT,
                f"host timezone is {actual!r}, the installed default; site office.timezone is {expected!r}",
                f"Run timedatectl set-timezone {expected}, then re-run provision.",
            )
        return CheckResult(
            Disposition.UNFIXABLE,
            box_wide_shortfall(self.settings[0], actual, expected) + " (site office.timezone)",
            BOX_WIDE_SHORTFALL_FIX,
        )

    def apply(self, context: ProvisionContext) -> None:
        if context.site is None:
            raise RuntimeError("site file is required by timezone")
        reading = self.check(context)
        if reading.disposition is Disposition.UNFIXABLE:
            raise StepFailure(reading.detail, reading.fix)
        if reading.disposition is Disposition.CONVERGED:
            return
        context.host.run(
            ["timedatectl", "set-timezone", context.site.office.timezone], check=True
        )
