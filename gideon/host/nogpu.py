"""The no-GPU and build-box declarations: host state read by later commands.

``host provision --no-gpu`` and ``host provision --build-box`` each record one
exclusive mode as a marker file under ``/etc/gideon``; provision, preflight,
render, apply, restore, and the backup set all read them through this module,
and no other command takes a flag: host state, never a site key.
"""

from pathlib import Path
from typing import Final

from gideon.host.report import Problem
from gideon.host.sysio import Host

NO_GPU_PATH: Final = Path("/etc/gideon/no-gpu")
BUILD_BOX_PATH: Final = Path("/etc/gideon/build-box")
_MARKER_TEXT: Final = (
    "This host was declared a no-GPU host by gideon host provision --no-gpu.\n"
    "Remove this file and re-run provision to leave the mode.\n"
)
_BUILD_BOX_MARKER_TEXT: Final = (
    "This host was declared the build box by gideon host provision --build-box.\n"
    "Remove this file, re-run provision, then apply to leave the mode; the KVM,\n"
    "registry, and runner units stay until removed by hand.\n"
)
_PCI_DEVICES_PATH: Final = Path("/sys/bus/pci/devices")
# lspci is not on a bare cloud image; sysfs is.
_NVIDIA_VENDOR_ID: Final = "0x10de"

NVIDIA_DEVICE_PROBLEM: Final = (
    "this host has an NVIDIA device, so it cannot be declared a no-GPU host."
)
NVIDIA_DEVICE_FIX: Final = (
    "Install the driver with host provision --only nvidia-driver, or remove "
    "the device and re-run host provision --no-gpu."
)
# The render/apply refusal on a GPU host whose driver is absent: both ways out.
GPU_DRIVER_FIX: Final = (
    "Run host provision --only nvidia-driver, or host provision --no-gpu on a "
    "host without a GPU."
)
NO_GPU_DECLARED_PROBLEM: Final = (
    "this host is declared a no-GPU host, so it cannot be declared the build box."
)
NO_GPU_DECLARED_FIX: Final = (
    f"Remove {NO_GPU_PATH}, then re-run host provision --build-box, or declare "
    "another host."
)
BUILD_BOX_DECLARED_PROBLEM: Final = (
    "this host is declared the build box, so it cannot be declared a no-GPU host."
)
BUILD_BOX_DECLARED_FIX: Final = (
    f"Remove {BUILD_BOX_PATH}, then re-run host provision --no-gpu."
)
NOT_BUILD_BOX_DETAIL: Final = (
    "not the build box: host provision --build-box declares one"
)
BUILD_BOX_ONLY_FIX: Final = (
    "Declare this host the build box with host provision --build-box, or choose "
    "another step."
)


def is_no_gpu_host(host: Host) -> bool:
    """Whether the host has declared the no-GPU mode."""

    return host.exists(NO_GPU_PATH)


def is_build_box(host: Host) -> bool:
    """Whether the host has declared the build-box mode.

    A hand-made pair of markers reads as a no-GPU host, so every command skips
    all five restricted steps rather than converging the build-box roles.
    """

    return host.exists(BUILD_BOX_PATH) and not is_no_gpu_host(host)


def has_nvidia_device(host: Host) -> bool:
    """Whether PCI sysfs exposes a device with NVIDIA's vendor id."""

    try:
        devices = host.listdir(_PCI_DEVICES_PATH)
    except FileNotFoundError:
        return False
    for device in devices:
        try:
            vendor = host.read_text(_PCI_DEVICES_PATH / device / "vendor")
        except FileNotFoundError:
            continue
        if vendor.strip().lower() == _NVIDIA_VENDOR_ID:
            return True
    return False


def declaration_problem(host: Host) -> Problem | None:
    """Why the mode cannot be declared here, or None when it can (or already is).

    A broken driver must refuse, never degrade silently: a host with an NVIDIA
    device is a GPU host whatever the driver's state.
    """

    # The other marker is checked first, so a pair refuses rather than reading
    # as already declared.
    if host.exists(BUILD_BOX_PATH):
        return Problem(BUILD_BOX_DECLARED_PROBLEM, BUILD_BOX_DECLARED_FIX)
    if is_no_gpu_host(host):
        return None
    if has_nvidia_device(host):
        return Problem(NVIDIA_DEVICE_PROBLEM, NVIDIA_DEVICE_FIX)
    return None


def declare(host: Host) -> Problem | None:
    """Write the marker absent-only (0644, root); an existing marker is left as it is.

    Provision runs before ``secrets-dirs``, so the parent directory may not
    exist yet.
    """

    problem = declaration_problem(host)
    if problem is not None or is_no_gpu_host(host):
        return problem
    try:
        host.mkdir(NO_GPU_PATH.parent, mode=0o755, parents=True, exist_ok=True)
        host.write_text(NO_GPU_PATH, _MARKER_TEXT, mode=0o644)
    except OSError as exc:
        return Problem(
            f"cannot write the no-GPU marker {NO_GPU_PATH}: {exc}",
            f"Create {NO_GPU_PATH} as root with mode 0644, then re-run provision.",
        )
    return None


def build_box_declaration_problem(host: Host) -> Problem | None:
    """Why the build-box mode cannot be declared here, or None when it can."""

    if is_no_gpu_host(host):
        return Problem(NO_GPU_DECLARED_PROBLEM, NO_GPU_DECLARED_FIX)
    return None


def declare_build_box(host: Host) -> Problem | None:
    """Write the build-box marker absent-only (0644, root)."""

    problem = build_box_declaration_problem(host)
    if problem is not None or is_build_box(host):
        return problem
    try:
        host.mkdir(BUILD_BOX_PATH.parent, mode=0o755, parents=True, exist_ok=True)
        host.write_text(BUILD_BOX_PATH, _BUILD_BOX_MARKER_TEXT, mode=0o644)
    except OSError as exc:
        return Problem(
            f"cannot write the build-box marker {BUILD_BOX_PATH}: {exc}",
            f"Create {BUILD_BOX_PATH} as root with mode 0644, then re-run provision.",
        )
    return None
