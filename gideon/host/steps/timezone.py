"""Host timezone provisioning."""

from gideon.host.steps import (
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    site_required,
)

_SHOW_TIMEZONE = ("timedatectl", "show", "-p", "Timezone", "--value")
_SYSTEMD_TIMEDATED_FIX = (
    "Ensure systemd-timedated is available and running, then re-run provision."
)


class TimezoneStep(Step):
    """Converge the host timezone to the office timezone."""

    name = "timezone"
    summary = "set the host clock's timezone from office.timezone"
    needs_site = True

    def check(self, context: ProvisionContext) -> CheckResult:
        if context.site is None:
            return site_required()

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

        actual = result.stdout.strip()
        expected = context.site.office.timezone
        if actual == expected:
            return CheckResult(
                Disposition.CONVERGED,
                f"host timezone is {actual}",
                "",
            )
        return CheckResult(
            Disposition.DRIFT,
            f"host timezone is {actual!r}; site office.timezone is {expected!r}",
            f"Run timedatectl set-timezone {expected}, then re-run provision.",
        )

    def apply(self, context: ProvisionContext) -> None:
        if context.site is None:
            raise RuntimeError("site file is required by timezone")
        context.host.run(
            ["timedatectl", "set-timezone", context.site.office.timezone],
            check=True,
        )
