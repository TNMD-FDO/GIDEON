"""Convergence steps for the NVIDIA driver and container toolkit."""

import re
from pathlib import Path

from gideon.host.steps import (
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    apt_install,
    checksum_matches,
    fetch_file,
    package_version,
)

# The ubuntu2604 CUDA repo serves its signing key only inside the cuda-keyring
# deb (every loose .pub/.gpg path 404s); the deb owns the keyring and the
# repo's own sources entry.
_KEYRING_PACKAGE = "cuda-keyring"
_KEYRING_FILE = Path("/usr/share/keyrings/cuda-archive-keyring.gpg")
_DEB_URL = "https://developer.download.nvidia.com/compute/cuda/repos/{repo}/{deb}"
_DEB_TMP = Path("/var/tmp/gideon-cuda-keyring.deb")
# Pre-0.0.6 artifacts: a source entry referencing a keyring that never
# downloaded, which breaks apt-get update box-wide until removed.
_LEGACY_KEYRING = Path("/etc/apt/keyrings/gideon-nvidia.asc")
_LEGACY_SOURCE = Path("/etc/apt/sources.list.d/gideon-nvidia.list")
_DRIVER_REBOOT_FIX = (
    "Reboot the host per the §1.9 runbook, then re-run provision."
)
_DRIVER_REPO_FIX = "Configure the pinned NVIDIA CUDA APT repository, then re-run provision."
_DRIVER_PACKAGE_FIX = "Install the pinned NVIDIA driver package, then re-run provision."
_DRIVER_HOLD_FIX = "Hold the pinned NVIDIA driver package with apt-mark, then re-run provision."
_TOOLKIT_FIX = "Install the pinned NVIDIA container toolkit, then re-run provision."
_PERSISTENCE_FIX = "Enable nvidia-persistenced, then re-run provision."
_CDI_FIX = "Generate the NVIDIA CDI specification, then re-run provision."
_PACKAGE_VERSION = re.compile(r"(?:^|\s)v?(\d+(?:\.\d+){0,3})(?=$|[\s\-+~:])")


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
    """NVIDIA's branch-selection package, e.g. nvidia-driver-pinning-595.

    The CUDA repo ships one generic ``nvidia-open`` metapackage; the pinning
    package apt-pins it to the branch, so it is installed first and the bare
    metapackage then resolves inside the branch.
    """

    return f"nvidia-driver-pinning-{context.lock.driver.branch}"


def _driver_loaded(context: ProvisionContext) -> bool:
    if not context.host.exists("/proc/driver/nvidia"):
        return False
    result = context.host.run(["lsmod"])
    return not any(line.split()[:1] == ["nouveau"] for line in result.stdout.splitlines())


class NvidiaDriverStep(Step):
    """Install and hold the lock-pinned NVIDIA driver branch."""

    name = "nvidia-driver"
    summary = "install and hold the lock-pinned NVIDIA driver"
    gpu_host_only = True

    def check(self, context: ProvisionContext) -> CheckResult:
        if context.host.exists(_LEGACY_SOURCE) or context.host.exists(_LEGACY_KEYRING):
            return CheckResult(
                Disposition.DRIFT,
                "a legacy gideon-nvidia apt entry is present (breaks apt-get update)",
                _DRIVER_REPO_FIX,
            )
        if package_version(context, _KEYRING_PACKAGE) is None or not context.host.exists(
            _KEYRING_FILE
        ):
            return CheckResult(Disposition.DRIFT, "the NVIDIA CUDA APT repository is missing", _DRIVER_REPO_FIX)

        pinning = _pinning_package(context)
        if package_version(context, pinning) is None:
            return CheckResult(
                Disposition.DRIFT,
                f"{pinning} is not installed",
                _DRIVER_PACKAGE_FIX,
            )
        driver = context.lock.driver.package
        version = package_version(context, driver)
        branch = context.lock.driver.branch
        if version is None or not (
            version == branch
            or version.startswith((f"{branch}.", f"{branch}-", f"{branch}+", f"{branch}~"))
        ):
            return CheckResult(
                Disposition.DRIFT,
                f"{driver} is not installed at branch {branch}",
                _DRIVER_PACKAGE_FIX,
            )
        held = context.host.run(["apt-mark", "showhold"])
        if driver not in held.stdout.splitlines():
            return CheckResult(
                Disposition.DRIFT,
                f"{driver} is not held",
                _DRIVER_HOLD_FIX,
            )
        if not _driver_loaded(context):
            return CheckResult(
                Disposition.REBOOT_REQUIRED,
                "the NVIDIA package is converged but the driver is not loaded",
                _DRIVER_REBOOT_FIX,
            )
        return CheckResult(Disposition.CONVERGED, "NVIDIA driver is current and loaded", "")

    def apply(self, context: ProvisionContext) -> None:
        # Self-heal first: the pre-0.0.6 source entry references a keyring that
        # never downloaded and poisons every apt-get update until removed.
        context.host.unlink(_LEGACY_SOURCE, missing_ok=True)
        context.host.unlink(_LEGACY_KEYRING, missing_ok=True)
        context.host.unlink(f"{_LEGACY_KEYRING}.partial", missing_ok=True)
        if package_version(context, _KEYRING_PACKAGE) is None or not context.host.exists(
            _KEYRING_FILE
        ):
            url = _DEB_URL.format(
                repo=context.lock.driver.repo, deb=context.lock.driver.keyring_deb
            )
            fetch_file(context, url, str(_DEB_TMP))
            if not checksum_matches(
                context, str(_DEB_TMP), context.lock.driver.keyring_sha256
            ):
                raise RuntimeError("cuda-keyring deb checksum mismatch after download")
            # dpkg -i installs the keyring AND the repo's own sources entry in
            # one step — a source line never exists without its keyring.
            context.host.run(["dpkg", "-i", str(_DEB_TMP)], check=True)
            context.host.unlink(_DEB_TMP, missing_ok=True)
        context.host.run(["apt-get", "update"], check=True)
        context.host.run(
            ["apt-get", "install", "-y", _pinning_package(context)], check=True
        )
        context.host.run(
            ["apt-get", "install", "-y", context.lock.driver.package], check=True
        )
        context.host.run(
            ["apt-mark", "hold", context.lock.driver.package], check=True
        )


class NvidiaToolkitStep(Step):
    """Install the toolkit and generate CDI after the driver is live."""

    name = "nvidia-toolkit"
    summary = "install the NVIDIA container toolkit and CDI spec"
    gpu_host_only = True
    requires = ("nvidia-driver",)

    def check(self, context: ProvisionContext) -> CheckResult:
        if not _driver_loaded(context):
            return CheckResult(
                Disposition.PENDING_INPUT,
                "the NVIDIA driver must be loaded before CDI generation",
                _DRIVER_REBOOT_FIX,
            )
        version = package_version(context, "nvidia-container-toolkit")
        if not _at_least(version, context.lock.minimums.toolkit):
            return CheckResult(
                Disposition.DRIFT,
                f"nvidia-container-toolkit is below {context.lock.minimums.toolkit}",
                _TOOLKIT_FIX,
            )
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
        return CheckResult(Disposition.CONVERGED, "NVIDIA toolkit and CDI are current", "")

    def apply(self, context: ProvisionContext) -> None:
        apt_install(context, ["nvidia-container-toolkit"])
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
