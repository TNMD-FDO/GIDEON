"""Provision the command-line tools used by the host commands."""

from gideon.host.steps import (
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    apt_install,
    package_installed,
)

# ldap-utils supports preflight's LDAP bind; skopeo lets
# `gideon registry mirror` copy images.lock into the release registry by
# digest; age and rsync support the backup and restore commands.
_PACKAGES = ("ldap-utils", "skopeo", "age", "rsync")
_ARCHIVE_PACKAGES = ("rclone",)


def _missing(context: ProvisionContext, packages: tuple[str, ...]) -> list[str]:
    return [package for package in packages if not package_installed(context, package)]


def _check_packages(context: ProvisionContext, packages: tuple[str, ...]) -> CheckResult:
    missing = _missing(context, packages)
    if not missing:
        verb = "is" if len(packages) == 1 else "are"
        return CheckResult(Disposition.CONVERGED, f"{' and '.join(packages)} {verb} installed", "")
    names = " ".join(missing)
    return CheckResult(
        Disposition.DRIFT,
        f"{', '.join(missing)} not installed",
        f"Install with apt-get install -y {names}, then re-run provision.",
    )


def _install_packages(context: ProvisionContext, packages: tuple[str, ...]) -> None:
    missing = _missing(context, packages)
    if missing:
        apt_install(context, missing)


class HostToolsStep(Step):
    """Install the host tools the bare-host commands shell out to."""

    name = "host-tools"
    summary = "install host command-line tools required by preflight, backups, and the registry mirror"

    def check(self, context: ProvisionContext) -> CheckResult:
        return _check_packages(context, _PACKAGES)

    def apply(self, context: ProvisionContext) -> None:
        _install_packages(context, _PACKAGES)


class ArchiveToolsStep(Step):
    """Install the source archive's transfer tool on the build box."""

    name = "archive-tools"
    summary = "install rclone for the build box's source-archive push"
    build_box_only = True

    def check(self, context: ProvisionContext) -> CheckResult:
        return _check_packages(context, _ARCHIVE_PACKAGES)

    def apply(self, context: ProvisionContext) -> None:
        _install_packages(context, _ARCHIVE_PACKAGES)
