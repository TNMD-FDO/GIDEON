"""Provisioning of command-line tools required by preflight and the release registry."""

from gideon.host.steps import (
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    apt_install,
    package_installed,
)

# ldap-utils: preflight's LDAP bind (ticket 03); skopeo: `gideon registry mirror`
# copies images.lock into the release registry by digest (§2.4); age and rsync:
# the backup and restore surfaces (§19.1–19.2).
_PACKAGES = ("ldap-utils", "skopeo", "age", "rsync")


def _missing(context: ProvisionContext) -> list[str]:
    return [package for package in _PACKAGES if not package_installed(context, package)]


class HostToolsStep(Step):
    """Install the host tools the bare-host commands shell out to."""

    name = "host-tools"
    summary = "install host command-line tools required by preflight, backups, and the registry mirror"

    def check(self, context: ProvisionContext) -> CheckResult:
        missing = _missing(context)
        if not missing:
            return CheckResult(Disposition.CONVERGED, f"{' and '.join(_PACKAGES)} are installed", "")
        names = " ".join(missing)
        return CheckResult(
            Disposition.DRIFT,
            f"{', '.join(missing)} not installed",
            f"Install with apt-get install -y {names}, then re-run provision.",
        )

    def apply(self, context: ProvisionContext) -> None:
        missing = _missing(context)
        if missing:
            apt_install(context, missing)
