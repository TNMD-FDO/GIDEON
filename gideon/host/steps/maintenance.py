"""Host maintenance package configuration."""

from pathlib import Path

from gideon.host.steps import (
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    apt_install,
    package_installed,
)

_DROP_IN = Path("/etc/apt/apt.conf.d/52gideon-auto-upgrades")
_DROP_IN_TEXT = (
    'APT::Periodic::Update-Package-Lists "1";\n'
    'APT::Periodic::Unattended-Upgrade "1";\n'
)


class UnattendedUpgradesStep(Step):
    """Enable Ubuntu's stock security-only unattended upgrades."""

    name = "unattended-upgrades"
    summary = "enable daily package lists and unattended security upgrades"

    def check(self, context: ProvisionContext) -> CheckResult:
        if not package_installed(context, "unattended-upgrades"):
            return CheckResult(
                Disposition.DRIFT,
                "unattended-upgrades is not installed",
                "Install unattended-upgrades, then re-run provision.",
            )
        if not context.host.exists(_DROP_IN):
            return CheckResult(
                Disposition.DRIFT,
                f"{_DROP_IN} is missing",
                f"Write {_DROP_IN}, then re-run provision.",
            )
        try:
            current = context.host.read_text(_DROP_IN)
        except (OSError, UnicodeError) as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read {_DROP_IN}: {exc}",
                f"Repair access to {_DROP_IN}, then re-run provision.",
            )
        if current != _DROP_IN_TEXT:
            return CheckResult(
                Disposition.DRIFT,
                f"{_DROP_IN} differs from the desired periodic policy",
                f"Rewrite {_DROP_IN}, then re-run provision.",
            )
        # Ubuntu's stock 50unattended-upgrades origins remain security-only;
        # this step owns only the periodic triggers.
        return CheckResult(Disposition.CONVERGED, "unattended upgrades are current", "")

    def apply(self, context: ProvisionContext) -> None:
        if not package_installed(context, "unattended-upgrades"):
            apt_install(context, ["unattended-upgrades"])
        context.host.write_text(_DROP_IN, _DROP_IN_TEXT)
