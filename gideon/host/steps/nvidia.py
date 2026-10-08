"""Converge NVIDIA's driver and container toolkit from host readings.

An open driver from any packaging meets the branch floor; the recipe installs
one only when absent. A sufficient toolkit is accepted from any repository.
Both steps read CUDA apt sources before writes and install the recipe's source
only when needed. A loaded module differing from the installed one is reported
as a pending reboot.
"""

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from gideon.host import aptsources
from gideon.host.cotenants import guard
from gideon.host.steps import (
    PREREQUISITE_FLOOR_FIX,
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    StepFailure,
    apt_install,
    checksum_matches,
    fetch_file,
    package_version,
    stderr_first_line,
)
from gideon.host.sysio import Host

# The CUDA repository serves its signing key inside the cuda-keyring deb; the
# deb owns the keyring and its flat source entry.
_KEYRING_PACKAGE = "cuda-keyring"
_KEYRING_FILE = Path("/usr/share/keyrings/cuda-archive-keyring.gpg")
_REPOSITORY_BASE = "https://developer.download.nvidia.com/compute/cuda/repos"
_DEB_TMP = Path("/var/tmp/gideon-cuda-keyring.deb")
# An old source entry references a keyring that never downloaded and breaks
# apt-get update until removed.
_LEGACY_KEYRING = Path("/etc/apt/keyrings/gideon-nvidia.asc")
_LEGACY_SOURCE = Path("/etc/apt/sources.list.d/gideon-nvidia.list")
_LOADED_VERSION = Path("/sys/module/nvidia/version")
_LOADED_TAINT = Path("/sys/module/nvidia/taint")
_KERNEL_RELEASE = Path("/proc/sys/kernel/osrelease")
_MODINFO_VERSION = ("modinfo", "-F", "version", "nvidia")
_MODINFO_LICENSE = ("modinfo", "-F", "license", "nvidia")
_DRIVER_PACKAGES = (
    "dpkg-query", "-W", "-f=${Package} ${Status} ${Version}\\n", "nvidia-*"
)
_DRIVER_PREFIXES = (
    "nvidia-driver",
    "nvidia-open",
    "nvidia-dkms",
    "nvidia-kernel",
    "nvidia-headless",
)
_TOOLKIT_POLICY = ("apt-cache", "policy", "nvidia-container-toolkit")
_DPKG_STATUS_SOURCE = "/var/lib/dpkg/status"
_DRIVER_REBOOT_FIX = (
    "Reboot the host, then re-run provision "
    "(docs/runbooks/install-upgrade.md §1)."
)
_PENDING_REBOOT_FIX = (
    "Reboot in an announced maintenance window "
    "(docs/runbooks/install-upgrade.md §9), then re-run provision."
)
_DRIVER_REPO_FIX = "Remove the old NVIDIA apt entry, then re-run provision."
_DRIVER_PACKAGE_FIX = "Install the NVIDIA driver with open kernel modules, then re-run provision."
_DRIVER_HOLD_FIX = "Hold nvidia-open with apt-mark, then re-run provision."
_CLOSED_MODULES_FIX = (
    "Replace the named driver with open kernel modules at or above the branch floor "
    "in an announced maintenance window (docs/runbooks/install-upgrade.md §9), "
    "then re-run provision."
)
_NO_MODULE_FIX = (
    "Rebuild the module for the running kernel with dkms autoinstall or reinstall "
    "the named driver packages in an announced maintenance window "
    "(docs/runbooks/install-upgrade.md §9), then re-run provision."
)
_MODULE_READ_FIX = "Repair access to the NVIDIA module reading, then re-run provision."
_UNREADABLE_SOURCE_FIX = "Repair {file} so it can be read, then re-run provision."
_SEVERAL_SOURCES_FIX = (
    "Keep one apt source for NVIDIA's CUDA repository and remove the others "
    "from {files}, then re-run provision."
)
_FOREIGN_SOURCE_FIX = (
    "Remove {file} or install {package} from it yourself, then re-run provision."
)
_OCCUPIED_SOURCE_FIX = (
    "Remove {file} or complete it as the recipe's source, then re-run provision."
)
_TOOLKIT_FIX = "Install the pinned NVIDIA container toolkit, then re-run provision."
_PERSISTENCE_FIX = "Enable nvidia-persistenced, then re-run provision."
_CDI_FIX = "Generate the NVIDIA CDI specification, then re-run provision."
_TOOLKIT_PACKAGE = "nvidia-container-toolkit"
_PACKAGE_VERSION = re.compile(r"(?:^|\s)v?(\d+(?:\.\d+){0,3})(?=$|[\s\-+~:])")
_POLICY_SOURCE = re.compile(r"^\s*\d+\s+(\S+)\s+.*\bPackages\s*$")


@dataclass(frozen=True)
class _DriverReading:
    loaded: bool
    loaded_version: str | None
    loaded_closed: bool | None
    disk_version: str | None
    disk_open: bool
    packages: tuple[str, ...]
    recipe_package_version: str | None

    @property
    def present(self) -> bool:
        return (
            self.loaded
            or self.disk_version is not None
            or bool(self.packages)
            or self.recipe_package_version is not None
        )

    @property
    def judged_version(self) -> str | None:
        return self.disk_version if self.disk_version is not None else self.loaded_version

    @property
    def judged_open(self) -> bool:
        """The on-disk module is what the next boot loads, so it is judged first."""

        if self.disk_version is not None:
            return self.disk_open
        return not self.loaded_closed


class _SourceKind(Enum):
    UNREADABLE = "unreadable"
    SEVERAL = "several"
    FOREIGN = "foreign"
    RECIPE = "recipe"
    OCCUPIED = "occupied"
    NONE = "none"


@dataclass(frozen=True)
class _SourceReading:
    kind: _SourceKind
    files: tuple[str, ...] = ()
    count: int = 0
    error: str = ""


def _version_tuple(version: str) -> tuple[int, ...] | None:
    match = _PACKAGE_VERSION.search(version)
    if match is None:
        return None
    parts = tuple(int(part) for part in match.group(1).split("."))
    return parts + (0,) * (4 - len(parts))


def _at_least(version: str | None, minimum: str) -> bool:
    if version is None:
        return False
    actual = _version_tuple(version)
    expected = _version_tuple(minimum)
    return actual is not None and expected is not None and actual >= expected


def _pinning_package(context: ProvisionContext) -> str:
    """The branch-selection package installed before the driver recipe."""

    return f"nvidia-driver-pinning-{context.lock.driver.branch}"


def _repository(context: ProvisionContext) -> str:
    return f"{_REPOSITORY_BASE}/{context.lock.driver.repo}"


def _source_path(context: ProvisionContext) -> Path:
    return Path("/etc/apt/sources.list.d") / (
        f"cuda-{context.lock.driver.repo.replace('/', '-')}.list"
    )


def _recipe_entry(entry: aptsources.Entry, context: ProvisionContext) -> bool:
    return (
        entry.file == str(_source_path(context))
        and entry.form == "list"
        and entry.types == ("deb",)
        and any(
            aptsources.same_repository(uri, _repository(context)) for uri in entry.uris
        )
        and entry.suites == ("/",)
        and not entry.components
        and (entry.signed_by or "").strip() == str(_KEYRING_FILE)
    )


def _source_reading(context: ProvisionContext) -> _SourceReading:
    scan = aptsources.entries_for(context.host, _repository(context))
    if scan.unreadable:
        path, error = scan.unreadable[0]
        return _SourceReading(_SourceKind.UNREADABLE, (path,), error=error)
    entries = tuple(entry for entry in scan.entries if entry.file != str(_LEGACY_SOURCE))
    if len(entries) > 1:
        return _SourceReading(
            _SourceKind.SEVERAL,
            tuple(sorted({entry.file for entry in entries})),
            count=len(entries),
        )
    if entries:
        entry = entries[0]
        kind = (
            _SourceKind.RECIPE if _recipe_entry(entry, context) else _SourceKind.FOREIGN
        )
        return _SourceReading(kind, (entry.file,))
    if context.host.exists(_source_path(context)):
        return _SourceReading(_SourceKind.OCCUPIED, (str(_source_path(context)),))
    return _SourceReading(_SourceKind.NONE)


def _source_refusal(
    context: ProvisionContext,
    source: _SourceReading,
    *,
    recipe_must_install: bool,
    package: str,
) -> tuple[str, str] | None:
    source_path = _source_path(context)
    if source.kind is _SourceKind.UNREADABLE:
        return (
            f"cannot read {source.files[0]}: {source.error}",
            _UNREADABLE_SOURCE_FIX.format(file=source.files[0]),
        )
    if source.kind is _SourceKind.SEVERAL:
        return (
            f"{source.count} apt sources name NVIDIA's CUDA repository: "
            f"{', '.join(source.files)}",
            _SEVERAL_SOURCES_FIX.format(files=", ".join(source.files)),
        )
    if recipe_must_install and source.kind is _SourceKind.FOREIGN:
        return (
            f"{source.files[0]} names NVIDIA's CUDA repository and is not the recipe's source",
            _FOREIGN_SOURCE_FIX.format(file=source.files[0], package=package),
        )
    if recipe_must_install and source.kind is _SourceKind.OCCUPIED:
        return (
            f"{source_path} holds no enabled entry for NVIDIA's CUDA repository",
            _OCCUPIED_SOURCE_FIX.format(file=source_path),
        )
    return None


def _driver_loaded(context: ProvisionContext) -> bool:
    if not context.host.exists("/proc/driver/nvidia"):
        return False
    result = context.host.run(["lsmod"])
    return not any(line.split()[:1] == ["nouveau"] for line in result.stdout.splitlines())


def _modinfo_field(context: ProvisionContext, argv: tuple[str, ...]) -> str | None:
    """One field of the on-disk module, None when modinfo cannot read it."""

    result = context.host.run(argv)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _driver_packages(context: ProvisionContext) -> tuple[str, ...]:
    """Installed driver packages; a listing that matches nothing exits non-zero."""

    result = context.host.run(_DRIVER_PACKAGES)
    if result.returncode != 0:
        return ()
    names = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if (
            len(fields) >= 5
            and fields[3] == "installed"
            and fields[0].startswith(_DRIVER_PREFIXES)
            and not fields[0].startswith("nvidia-driver-pinning-")
        ):
            names.add(fields[0])
    return tuple(sorted(names))


def loaded_driver_version(host: Host) -> str | None:
    """The loaded nvidia module's version, None when no module is loaded."""

    try:
        version = host.read_text(_LOADED_VERSION).strip()
    except FileNotFoundError:
        return None
    return version or None


def _driver_reading(context: ProvisionContext) -> _DriverReading | CheckResult:
    loaded = _driver_loaded(context)
    loaded_version = None
    loaded_closed = None
    if loaded:
        try:
            loaded_version = context.host.read_text(_LOADED_VERSION).strip()
            loaded_closed = "P" in context.host.read_text(_LOADED_TAINT)
        except (OSError, UnicodeError) as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read the loaded NVIDIA module: {exc}",
                _MODULE_READ_FIX,
            )
        if not loaded_version:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"{_LOADED_VERSION} is empty",
                _MODULE_READ_FIX,
            )
    disk_version = _modinfo_field(context, _MODINFO_VERSION)
    # The kernel reads a licence naming GPL, such as Dual MIT/GPL, as open.
    disk_open = "GPL" in (_modinfo_field(context, _MODINFO_LICENSE) or "").upper()
    return _DriverReading(
        loaded,
        loaded_version,
        loaded_closed,
        disk_version,
        disk_open,
        _driver_packages(context),
        package_version(context, context.lock.driver.package),
    )


def _driver_refusal(
    context: ProvisionContext, reading: _DriverReading
) -> CheckResult | None:
    if not reading.present:
        return None
    packages = ", ".join(reading.packages) or "no package"
    version = reading.judged_version
    if version is None:
        try:
            kernel = context.host.read_text(_KERNEL_RELEASE).strip()
        except (OSError, UnicodeError) as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read the running kernel version: {exc}",
                _MODULE_READ_FIX,
            )
        if not kernel:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"{_KERNEL_RELEASE} is empty",
                _MODULE_READ_FIX,
            )
        return CheckResult(
            Disposition.UNFIXABLE,
            f"NVIDIA driver packages ({packages}) have no module for the running kernel {kernel}",
            _NO_MODULE_FIX,
        )
    if not reading.judged_open:
        return CheckResult(
            Disposition.UNFIXABLE,
            f"NVIDIA module {version} uses closed kernel modules (packages: {packages})",
            _CLOSED_MODULES_FIX,
        )
    branch = context.lock.driver.branch
    if not _at_least(version, branch):
        return CheckResult(
            Disposition.UNFIXABLE,
            f"NVIDIA module {version} is below the branch floor {branch} (packages: {packages})",
            PREREQUISITE_FLOOR_FIX.format(package=packages, floor=branch),
        )
    return None


def _foreign_report(context: ProvisionContext, source: _SourceReading) -> str:
    """The ok row's clause for a CUDA entry off the recipe's path."""

    if source.kind is not _SourceKind.FOREIGN:
        return ""
    recipe = _source_path(context)
    if source.files[0] == str(recipe):
        return f"; NVIDIA's apt source {recipe} is not the recipe's entry"
    return f"; NVIDIA's apt source is {source.files[0]}, not the recipe's {recipe}"


def _held(context: ProvisionContext, package: str) -> bool:
    result = context.host.run(["apt-mark", "showhold"])
    return package in result.stdout.splitlines()


def _install_repository(context: ProvisionContext, source: _SourceReading) -> None:
    if (
        source.kind is _SourceKind.RECIPE
        and package_version(context, _KEYRING_PACKAGE) is not None
        and context.host.exists(_KEYRING_FILE)
    ):
        return
    url = f"{_repository(context)}/{context.lock.driver.keyring_deb}"
    fetch_file(context, url, str(_DEB_TMP))
    if not checksum_matches(context, str(_DEB_TMP), context.lock.driver.keyring_sha256):
        raise StepFailure(
            "the downloaded cuda-keyring deb does not match the lock's checksum",
            "Check the lock's driver.keyring_sha256 against NVIDIA's published deb, "
            "then re-run provision.",
        )
    # The deb's source and pin are conffiles: a reinstall restores a removed
    # one only when told to, and the recipe's install needs both.
    context.host.run(["dpkg", "-i", "--force-confmiss", str(_DEB_TMP)], check=True)
    context.host.unlink(_DEB_TMP, missing_ok=True)


def _toolkit_shortfall(
    context: ProvisionContext, version: str | None
) -> CheckResult | None:
    """Refuse a present toolkit below the locked floor."""

    if version is None or _at_least(version, context.lock.minimums.toolkit):
        return None
    floor = context.lock.minimums.toolkit
    return CheckResult(
        Disposition.UNFIXABLE,
        f"{_TOOLKIT_PACKAGE} is at {version}, below the floor {floor}",
        PREREQUISITE_FLOOR_FIX.format(package=_TOOLKIT_PACKAGE, floor=floor),
    )


def _toolkit_policy_report(context: ProvisionContext) -> str:
    """Report other apt repositories offering the toolkit, or a failed policy read."""

    result = context.host.run(_TOOLKIT_POLICY)
    if result.returncode != 0:
        return f"; apt-cache policy could not be read: {stderr_first_line(result.stderr)}"
    repositories = sorted(
        {
            uri
            for line in result.stdout.partition("Version table:")[2].splitlines()
            if (match := _POLICY_SOURCE.fullmatch(line)) is not None
            if (uri := match.group(1)) != _DPKG_STATUS_SOURCE
            and not aptsources.same_repository(uri, _repository(context))
        }
    )
    if not repositories:
        return ""
    noun = "repository" if len(repositories) == 1 else "repositories"
    return (
        f"; also served by {len(repositories)} other apt {noun}: "
        + ", ".join(repositories)
    )


class NvidiaDriverStep(Step):
    """Accept a sufficient open module or install the absent driver recipe."""

    name = "nvidia-driver"
    summary = "accept or install an open NVIDIA driver at the branch floor"
    gpu_host_only = True

    def check(self, context: ProvisionContext) -> CheckResult:
        source = _source_reading(context)
        refusal = _source_refusal(
            context, source, recipe_must_install=False, package=context.lock.driver.package
        )
        if refusal is not None:
            return CheckResult(Disposition.UNFIXABLE, *refusal)
        reading = _driver_reading(context)
        if isinstance(reading, CheckResult):
            return reading
        driver_refusal = _driver_refusal(context, reading)
        if driver_refusal is not None:
            return driver_refusal
        driver = context.lock.driver.package
        if context.host.exists(_LEGACY_SOURCE) or context.host.exists(_LEGACY_KEYRING):
            return CheckResult(
                Disposition.DRIFT,
                "a legacy gideon-nvidia apt entry is present (breaks apt-get update)",
                _DRIVER_REPO_FIX,
            )
        if not reading.present:
            refusal = _source_refusal(
                context, source, recipe_must_install=True, package=context.lock.driver.package
            )
            if refusal is not None:
                return CheckResult(Disposition.UNFIXABLE, *refusal)
            return CheckResult(
                Disposition.DRIFT,
                "the NVIDIA driver is not installed",
                _DRIVER_PACKAGE_FIX,
            )
        if not reading.loaded:
            return CheckResult(
                Disposition.REBOOT_REQUIRED,
                "the NVIDIA package is converged but the driver is not loaded",
                _DRIVER_REBOOT_FIX,
            )
        reopened = bool(reading.loaded_closed) and reading.disk_open
        if reading.disk_version is not None and (
            reading.disk_version != reading.loaded_version or reopened
        ):
            detail = (
                f"NVIDIA driver {reading.loaded_version} is loaded and "
                f"{reading.disk_version} is installed: a reboot is pending"
            )
            if reopened:
                detail += "; the loaded module is closed and the installed module is open"
            return CheckResult(Disposition.REBOOT_REQUIRED, detail, _PENDING_REBOOT_FIX)
        if reading.recipe_package_version is not None and not _held(context, driver):
            return CheckResult(Disposition.DRIFT, f"{driver} is not held", _DRIVER_HOLD_FIX)
        detail = f"NVIDIA driver {reading.loaded_version} is loaded with open kernel modules"
        if reading.recipe_package_version is None:
            detail += (
                "; not the recipe's nvidia-open package "
                f"(packages: {', '.join(reading.packages) or 'no package'})"
            )
        if reading.disk_version is None:
            detail += "; the installed module's version could not be read"
        detail += _foreign_report(context, source)
        return CheckResult(Disposition.CONVERGED, detail, "")

    def apply(self, context: ProvisionContext) -> None:
        source = _source_reading(context)
        refusal = _source_refusal(
            context, source, recipe_must_install=False, package=context.lock.driver.package
        )
        if refusal is not None:
            raise StepFailure(*refusal)
        reading = _driver_reading(context)
        if isinstance(reading, CheckResult):
            raise StepFailure(reading.detail, reading.fix)
        driver_refusal = _driver_refusal(context, reading)
        if driver_refusal is not None:
            raise StepFailure(driver_refusal.detail, driver_refusal.fix)
        if not reading.present:
            refusal = _source_refusal(
                context, source, recipe_must_install=True, package=context.lock.driver.package
            )
            if refusal is not None:
                raise StepFailure(*refusal)
            guard(context, "installing the NVIDIA driver, which needs a reboot")
        driver = context.lock.driver.package
        # Remove the old source before any apt command reads it.
        context.host.unlink(_LEGACY_SOURCE, missing_ok=True)
        context.host.unlink(_LEGACY_KEYRING, missing_ok=True)
        context.host.unlink(f"{_LEGACY_KEYRING}.partial", missing_ok=True)
        if not reading.present:
            _install_repository(context, source)
            apt_install(context, [_pinning_package(context)])
            apt_install(context, [driver])
        # A driver of another packaging is never held; the recipe's always is.
        installed = reading.recipe_package_version is not None or not reading.present
        if installed and not _held(context, driver):
            context.host.run(["apt-mark", "hold", driver], check=True)


class NvidiaToolkitStep(Step):
    """Accept a sufficient toolkit or install it after the driver is live."""

    name = "nvidia-toolkit"
    summary = "install the NVIDIA container toolkit and CDI spec"
    gpu_host_only = True
    requires = ("nvidia-driver",)

    def check(self, context: ProvisionContext) -> CheckResult:
        source = _source_reading(context)
        refusal = _source_refusal(
            context, source, recipe_must_install=False, package=_TOOLKIT_PACKAGE
        )
        if refusal is not None:
            return CheckResult(Disposition.UNFIXABLE, *refusal)
        if not _driver_loaded(context):
            return CheckResult(
                Disposition.PENDING_INPUT,
                "the NVIDIA driver must be loaded before CDI generation",
                _DRIVER_REBOOT_FIX,
            )
        version = package_version(context, _TOOLKIT_PACKAGE)
        if version is None:
            refusal = _source_refusal(
                context, source, recipe_must_install=True, package=_TOOLKIT_PACKAGE
            )
            if refusal is not None:
                return CheckResult(Disposition.UNFIXABLE, *refusal)
            return CheckResult(
                Disposition.DRIFT,
                f"{_TOOLKIT_PACKAGE} is not installed",
                _TOOLKIT_FIX,
            )
        shortfall = _toolkit_shortfall(context, version)
        if shortfall is not None:
            return shortfall
        enabled = context.host.run(["systemctl", "is-enabled", "nvidia-persistenced"])
        if enabled.returncode != 0:
            return CheckResult(
                Disposition.DRIFT,
                "nvidia-persistenced is not enabled",
                _PERSISTENCE_FIX,
            )
        if not context.host.exists("/etc/cdi/nvidia.yaml"):
            return CheckResult(
                Disposition.DRIFT,
                "the NVIDIA CDI specification is missing",
                _CDI_FIX,
            )
        detail = "NVIDIA toolkit and CDI are current" + _toolkit_policy_report(context)
        detail += _foreign_report(context, source)
        return CheckResult(Disposition.CONVERGED, detail, "")

    def apply(self, context: ProvisionContext) -> None:
        source = _source_reading(context)
        refusal = _source_refusal(
            context, source, recipe_must_install=False, package=_TOOLKIT_PACKAGE
        )
        if refusal is not None:
            raise StepFailure(*refusal)
        version = package_version(context, _TOOLKIT_PACKAGE)
        shortfall = _toolkit_shortfall(context, version)
        if shortfall is not None:
            raise StepFailure(shortfall.detail, shortfall.fix)
        if version is None:
            refusal = _source_refusal(
                context, source, recipe_must_install=True, package=_TOOLKIT_PACKAGE
            )
            if refusal is not None:
                raise StepFailure(*refusal)
            _install_repository(context, source)
            apt_install(context, [_TOOLKIT_PACKAGE])
        context.host.run(
            ["systemctl", "enable", "--now", "nvidia-persistenced"], check=True
        )
        context.host.mkdir(Path("/etc/cdi"), mode=0o755, parents=True, exist_ok=True)
        context.host.run(
            [
                "nvidia-ctk",
                "cdi",
                "generate",
                "--output=/etc/cdi/nvidia.yaml",
            ],
            check=True,
        )
