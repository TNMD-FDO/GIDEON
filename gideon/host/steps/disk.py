"""Safe convergence of GIDEON's data volume or a sufficient existing /data mount.

The step refuses to mount over an unmounted /data holding entries.
It never changes a /data filesystem it did not build.
"""

import json
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from gideon.host import cas
from gideon.host.checks import format_gb
from gideon.host.models import GIGABYTE, select_profile
from gideon.host.render import worker
from gideon.host.render.opensearch import (
    OPENSEARCH_DATA_ROOT,
    OPENSEARCH_GID,
    OPENSEARCH_UID,
)
from gideon.host.render.qdrant import QDRANT_DATA_ROOT
from gideon.host.report import Problem
from gideon.host.steps import (
    BOX_WIDE_SHORTFALL_FIX,
    SITE_MISSING_FIX,
    BoxWideSetting,
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    StepFailure,
    apt_install,
    box_wide_shortfall,
    passwd_entry,
)
from gideon.host.sysio import Host

_LSBLK = [
    "lsblk",
    "-J",
    "-b",
    "-o",
    "NAME,TYPE,SIZE,WWN,FSTYPE,MOUNTPOINT,PKNAME",
]
_FSTAB = Path("/etc/fstab")
_DATA_MOUNT = Path("/data")
_VG = "vg_data"
_LV = "data"
# The box prints GIDEON's own volume as /dev/mapper/vg_data-data.
_OWN_SOURCES: Final = (f"/dev/mapper/{_VG}-{_LV}", f"/dev/{_VG}/{_LV}")
_SUPPORTED_FILESYSTEMS: Final = frozenset({"xfs", "ext4"})
_DATA_MOUNT_SETTING: Final = BoxWideSetting("/data mount")
# These are the fixed users in the pinned Prometheus and Grafana images.  The
# data step owns the directories before Compose creates either container.
PROMETHEUS_UID: Final = 65534
PROMETHEUS_GID: Final = 65534
GRAFANA_UID: Final = 472
GRAFANA_GID: Final = 472


@dataclass(frozen=True, slots=True)
class _DataDirectory:
    """One managed /data directory and the source of its owner ids."""

    relative_path: str
    owner: str
    mode: int


_DATA_DIRS: Final[tuple[_DataDirectory, ...]] = (
    _DataDirectory("fast", "gideon", 0o755),
    _DataDirectory(
        str(Path(QDRANT_DATA_ROOT).relative_to(_DATA_MOUNT)), "gideon", 0o750
    ),
    _DataDirectory(
        str(Path(OPENSEARCH_DATA_ROOT).relative_to(_DATA_MOUNT)), "opensearch", 0o700
    ),
    _DataDirectory("bulk", "gideon", 0o755),
    _DataDirectory(
        str(cas.ROOT.relative_to(_DATA_MOUNT)), "gideon", cas.DIRECTORY_MODE
    ),
    _DataDirectory(
        str(worker.SNAPSHOTS_ROOT.relative_to(_DATA_MOUNT)), "gideon", worker.DIR_MODE
    ),
    _DataDirectory("work", "gideon", 0o755),
    _DataDirectory("models", "gideon", 0o755),
    _DataDirectory("registry", "gideon", 0o755),
    _DataDirectory("drill", "gideon", 0o755),
    _DataDirectory("ci", "gideon", 0o755),
    _DataDirectory("backup-staging", "gideon", 0o755),
    _DataDirectory("acceptance", "gideon", 0o755),
    _DataDirectory("observability", "gideon", 0o755),
    _DataDirectory("observability/prometheus", "prometheus", 0o750),
    _DataDirectory("observability/grafana", "grafana", 0o750),
)


def _owner_ids(uid: int, gid: int) -> Mapping[str, tuple[int, int]]:
    """The uid/gid pair behind each ``_DataDirectory.owner`` name."""

    return {
        "gideon": (uid, gid),
        "prometheus": (PROMETHEUS_UID, PROMETHEUS_GID),
        "grafana": (GRAFANA_UID, GRAFANA_GID),
        "opensearch": (OPENSEARCH_UID, OPENSEARCH_GID),
    }
_BEGIN = "# GIDEON BEGIN provision:disk-layout"
_END = "# GIDEON END provision:disk-layout"
_RESERVE_BYTES = 2**40
_REINSTALL_FIX = "Reinstall the host's operating system, then re-run provision."
_DISK_FIX = (
    "Resolve the data disk manually without wiping foreign data, then re-run provision."
)
_DATA_MOVE_FIX = "Move the entries onto the data volume by the procedure in docs/runbooks/install-upgrade.md §8, then re-run provision."
_DATA_DIR_FIX = "Make /data a directory or remove it, then re-run provision."
_DATA_NAMES_CAP = 10
_MOUNT_READ_FIX = "Restore a working findmnt and a readable /data mount, then re-run provision."
_MODELS_FIX = "Load models.lock from the release checkout, then re-run provision."


@dataclass(frozen=True, slots=True)
class _Disk:
    name: str
    size: int
    wwn: str
    nodes: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class _Mount:
    source: str
    fstype: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _Inspection:
    os_disk: _Disk | None
    data_disk: _Disk | None
    data_kind: str | None
    pv_exists: bool = False
    vg_exists: bool = False
    lv_exists: bool = False
    filesystem: str | None = None
    uuid: str | None = None
    vg_size: int | None = None
    mount: _Mount | None = None
    owned: bool = True
    mount_error: str | None = None
    fstab: str | None = None
    uid: int | None = None
    gid: int | None = None
    error: str | None = None
    error_fix: str = _DISK_FIX
    data_names: tuple[str, ...] = ()
    data_list_error: str | None = None


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _flatten(node: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    children = node.get("children")
    flattened: list[Mapping[str, object]] = [node]
    if isinstance(children, list):
        for child in children:
            if isinstance(child, Mapping):
                flattened.extend(_flatten(child))
    return tuple(flattened)


def _lsblk(host: Host) -> tuple[_Disk, ...] | str:
    result = host.run(_LSBLK)
    if result.returncode != 0:
        return "lsblk could not enumerate block devices"
    try:
        document = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        return f"lsblk returned invalid JSON: {exc}"
    if not isinstance(document, Mapping) or not isinstance(
        document.get("blockdevices"), list
    ):
        return "lsblk returned no block-device list"
    disks: list[_Disk] = []
    for value in document["blockdevices"]:
        if not isinstance(value, Mapping) or value.get("type") != "disk":
            continue
        try:
            size = int(value.get("size", 0))
        except (TypeError, ValueError):
            return f"lsblk returned an invalid size for {value.get('name', '(unknown)')}"
        name = _text(value.get("name"))
        if not name:
            return "lsblk returned a disk without a name"
        disks.append(
            _Disk(
                name=name,
                size=size,
                wwn=_text(value.get("wwn")),
                nodes=_flatten(value),
            )
        )
    return tuple(disks)


def _candidate_text(disks: Sequence[_Disk]) -> str:
    return ", ".join(
        f"{disk.name} (WWN: {disk.wwn or '(none)'})" for disk in disks
    ) or "(none)"


def _has_mount(nodes: Sequence[Mapping[str, object]], mountpoint: str) -> bool:
    return any(
        node.get("mountpoint") == mountpoint
        and node.get("type") == "lvm"
        and node.get("fstype") == "ext4"
        for node in nodes
    )


def _has_swap(nodes: Sequence[Mapping[str, object]]) -> bool:
    return any(node.get("type") == "lvm" and node.get("fstype") == "swap" for node in nodes)


def _report_rows(
    result: subprocess.CompletedProcess[str], section: str
) -> list[Mapping[str, object]]:
    if result.returncode != 0:
        return []
    try:
        document = json.loads(result.stdout)
    except (TypeError, ValueError):
        return []
    if not isinstance(document, Mapping) or not isinstance(document.get("report"), list):
        return []
    rows: list[Mapping[str, object]] = []
    for report in document["report"]:
        if isinstance(report, Mapping) and isinstance(report.get(section), list):
            rows.extend(row for row in report[section] if isinstance(row, Mapping))
    return rows


def _lvm_state(
    host: Host, disk: _Disk
) -> tuple[bool, bool, bool, str | None, int | None]:
    pvs = _report_rows(
        host.run(["pvs", "--reportformat", "json", "-o", "pv_name,vg_name"]),
        "pv",
    )
    pvs_for_disk = [
        row
        for row in pvs
        if _text(row.get("pv_name")) == f"/dev/{disk.name}"
        or _text(row.get("pv_name")).startswith(f"/dev/{disk.name}")
    ]
    pv_exists = any(_text(row.get("vg_name")) == _VG for row in pvs_for_disk)
    foreign_pv = next(
        (
            _text(row.get("vg_name"))
            for row in pvs_for_disk
            if _text(row.get("vg_name")) != _VG
        ),
        None,
    )
    vgs = _report_rows(
        host.run(
            [
                "vgs",
                "--reportformat",
                "json",
                "--units",
                "b",
                "-o",
                "vg_name,vg_size",
            ]
        ),
        "vg",
    )
    vg_exists = any(_text(row.get("vg_name")) == _VG for row in vgs)
    vg_size: int | None = None
    for row in vgs:
        if _text(row.get("vg_name")) == _VG:
            try:
                vg_size = int(_text(row.get("vg_size")).rstrip("Bb"))
            except ValueError:
                vg_size = None
            break
    lvs = _report_rows(
        host.run(
            [
                "lvs",
                "--reportformat",
                "json",
                "-o",
                "lv_name,vg_name,lv_path",
            ]
        ),
        "lv",
    )
    lv_exists = any(
        _text(row.get("lv_name")) == _LV and _text(row.get("vg_name")) == _VG
        for row in lvs
    )
    return pv_exists, vg_exists, lv_exists, foreign_pv, vg_size


def _vg_size_bytes(host: Host) -> int | None:
    """Read the current VG capacity after any preceding LVM mutation."""

    rows = _report_rows(
        host.run(
            [
                "vgs",
                "--reportformat",
                "json",
                "--units",
                "b",
                "-o",
                "vg_name,vg_size",
            ]
        ),
        "vg",
    )
    for row in rows:
        if _text(row.get("vg_name")) == _VG:
            try:
                return int(_text(row.get("vg_size")).rstrip("Bb"))
            except ValueError:
                return None
    return None


def _blkid(host: Host, field: str) -> str | None:
    result = host.run(["blkid", "-o", "value", "-s", field, "/dev/vg_data/data"])
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _read_mount(host: Host) -> tuple[_Mount | None, str | None]:
    result = host.run(
        [
            "findmnt", "-rn", "-b", "-o", "TARGET,SOURCE,FSTYPE,SIZE",
            "--mountpoint", str(_DATA_MOUNT),
        ]
    )
    if result.returncode in (126, 127):
        return None, (
            f"findmnt could not run (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    if result.returncode != 0:
        return None, None
    fields = result.stdout.split()
    if len(fields) != 4 or fields[0] != str(_DATA_MOUNT):
        return None, "findmnt returned an unreadable /data mount"
    try:
        size_bytes = int(fields[3])
    except ValueError:
        return None, "findmnt returned an unreadable /data mount size"
    if size_bytes < 0:
        return None, "findmnt returned an unreadable /data mount size"
    return _Mount(fields[1], fields[2], size_bytes), None


def _occupied_data_detail(names: tuple[str, ...]) -> str:
    count = len(names)
    entry = "entry" if count == 1 else "entries"
    shown = ", ".join(names[:_DATA_NAMES_CAP])
    remaining = count - _DATA_NAMES_CAP
    if remaining > 0:
        shown += f", and {remaining} more"
    return f"{_DATA_MOUNT} is not a mount point and holds {count} {entry}: {shown}"


def _read_fstab(host: Host) -> str | None:
    if not host.exists(_FSTAB):
        return ""
    try:
        return host.read_text(_FSTAB)
    except (OSError, UnicodeError):
        return None


def _fstab_block(uuid: str) -> str:
    return f"{_BEGIN}\nUUID={uuid} /data xfs defaults,nofail 0 2\n{_END}"


def _fstab_has_block(text: str, block: str) -> bool:
    lines = text.splitlines()
    begin = [index for index, line in enumerate(lines) if line == _BEGIN]
    end = [index for index, line in enumerate(lines) if line == _END]
    return (
        len(begin) == 1
        and len(end) == 1
        and begin[0] < end[0]
        and lines[begin[0] : end[0] + 1] == block.splitlines()
    )


def _foreign_lines(text: str) -> list[str]:
    """Every line outside the managed marker blocks."""

    lines = text.splitlines()
    foreign: list[str] = []
    index = 0
    while index < len(lines):
        if lines[index] == _BEGIN:
            closing = next(
                (candidate for candidate in range(index + 1, len(lines)) if lines[candidate] == _END),
                None,
            )
            if closing is not None:
                index = closing + 1
                continue
        foreign.append(lines[index])
        index += 1
    return foreign


def _unmanaged_data_entry(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    fields = stripped.split()
    return len(fields) >= 2 and fields[1] == str(_DATA_MOUNT)


def _has_unmanaged_data_entry(text: str) -> bool:
    return any(_unmanaged_data_entry(line) for line in _foreign_lines(text))


def _replace_fstab(text: str, block: str) -> str:
    # A non-managed line mounting /data is a prior unmanaged version of our
    # entry (the installer's, typically) — claimed by the block, never kept
    # beside it: two fstab entries for one mount point is the real corruption.
    foreign = [
        line for line in _foreign_lines(text) if not _unmanaged_data_entry(line)
    ]
    prefix = "\n".join(foreign)
    if prefix:
        prefix += "\n"
    return prefix + block + "\n"


def _directory_state(host: Host, uid: int, gid: int) -> str | None:
    owners = _owner_ids(uid, gid)
    for directory in _DATA_DIRS:
        path = _DATA_MOUNT / directory.relative_path
        if not host.exists(path):
            return f"directory {path} is missing"
        try:
            details = host.stat(path)
        except OSError as exc:
            return f"cannot inspect {path}: {exc}"
        if stat.S_IMODE(details.st_mode) != directory.mode:
            return f"directory {path} mode is not {directory.mode:04o}"
        expected_uid, expected_gid = owners[directory.owner]
        if details.st_uid != expected_uid or details.st_gid != expected_gid:
            return f"directory {path} is not owned by {directory.owner}"
    return None


def _inspect(context: ProvisionContext) -> _Inspection:
    disks_or_error = _lsblk(context.host)
    if isinstance(disks_or_error, str):
        return _Inspection(None, None, None, error=disks_or_error)
    disks = disks_or_error
    os_candidates = [
        disk
        for disk in disks
        if any(node.get("mountpoint") == "/" for node in disk.nodes)
    ]
    if len(os_candidates) != 1:
        return _Inspection(
            None,
            None,
            None,
            error=(
                "could not positively identify exactly one OS disk whose partition "
                f"tree hosts /: {_candidate_text(os_candidates)}"
            ),
            error_fix=_DISK_FIX,
        )
    os_disk = os_candidates[0]
    os_nodes = os_disk.nodes
    if not _has_mount(os_nodes, "/"):
        return _Inspection(
            os_disk, None, None,
            error="OS root is not an ext4 LV mounted at /",
            error_fix=_REINSTALL_FIX,
        )
    if not _has_mount(os_nodes, "/var/lib/docker"):
        return _Inspection(
            os_disk, None, None,
            error="/var/lib/docker is not an ext4 LV mounted at /var/lib/docker",
            error_fix=_REINSTALL_FIX,
        )
    if not _has_swap(os_nodes):
        return _Inspection(
            os_disk, None, None,
            error="the OS swap LV is missing",
            error_fix=_REINSTALL_FIX,
        )

    mount, mount_error = _read_mount(context.host)
    if mount_error is not None:
        return _Inspection(os_disk, None, None, mount_error=mount_error)
    owned = mount is None or mount.source in _OWN_SOURCES
    if not owned:
        entry = passwd_entry(context, "gideon")
        return _Inspection(
            os_disk, None, None,
            mount=mount,
            owned=False,
            uid=entry.uid if entry is not None else None,
            gid=entry.gid if entry is not None else None,
        )

    candidates = [disk for disk in disks if disk.name != os_disk.name]
    if len(candidates) != 1:
        return _Inspection(
            os_disk,
            None,
            None,
            error=(
                "data disk must be exactly one remaining disk-type device; "
                f"candidates: {_candidate_text(candidates)}"
            ),
            error_fix=_DISK_FIX,
        )
    data_disk = candidates[0]
    if not data_disk.wwn:
        return _Inspection(
            os_disk,
            None,
            None,
            error=(
                f"data disk {data_disk.name} carries no stable identity (WWN); "
                "nothing is mutated without one"
            ),
            error_fix=_DISK_FIX,
        )
    if data_disk.size <= os_disk.size:
        return _Inspection(
            os_disk,
            None,
            None,
            error=(
                f"data disk {data_disk.name} ({data_disk.size} B) is not larger "
                f"than the OS disk ({os_disk.size} B); wrong size class for the "
                "data-disk layout"
            ),
            error_fix=_DISK_FIX,
        )
    data_names: tuple[str, ...] = ()
    data_list_error: str | None = None
    if mount is None and context.host.exists(_DATA_MOUNT):
        try:
            data_names = tuple(sorted(context.host.listdir(_DATA_MOUNT)))
        except FileNotFoundError:
            pass
        except OSError as exc:
            data_list_error = f"cannot list {_DATA_MOUNT}: {exc}"

    pv_exists, vg_exists, lv_exists, foreign_pv, vg_size = _lvm_state(
        context.host, data_disk
    )
    filesystem = _blkid(context.host, "TYPE") if lv_exists else None
    if foreign_pv is not None:
        data_kind = "foreign"
    else:
        foreign_fstype = next(
            (
                _text(node.get("fstype"))
                for node in data_disk.nodes
                if node.get("fstype")
                and _text(node.get("fstype")) not in {"LVM2_member", "xfs"}
            ),
            None,
        )
        if foreign_fstype is not None or (filesystem is not None and filesystem != "xfs"):
            data_kind = "foreign"
        elif pv_exists or lv_exists:
            data_kind = "partial-GIDEON"
        elif len(data_disk.nodes) > 1 or any(node.get("fstype") for node in data_disk.nodes):
            data_kind = "foreign"
        else:
            # lsblk sees nothing; confirm with wipefs before calling it blank —
            # any signature it reports (or a failed probe) refuses mutation.
            probe = context.host.run(["wipefs", "-n", f"/dev/{data_disk.name}"])
            if probe.returncode != 0 or probe.stdout.strip():
                data_kind = "foreign"
            else:
                data_kind = "blank"

    uuid = _blkid(context.host, "UUID") if filesystem == "xfs" else None
    fstab = _read_fstab(context.host) if uuid is not None else None
    entry = passwd_entry(context, "gideon")
    return _Inspection(
        os_disk=os_disk,
        data_disk=data_disk,
        data_kind=data_kind,
        pv_exists=pv_exists,
        vg_exists=vg_exists,
        lv_exists=lv_exists,
        filesystem=filesystem,
        uuid=uuid,
        vg_size=vg_size,
        mount=mount,
        fstab=fstab,
        uid=entry.uid if entry is not None else None,
        gid=entry.gid if entry is not None else None,
        data_names=data_names,
        data_list_error=data_list_error,
    )


def _failure(detail: str, fix: str = _DISK_FIX) -> CheckResult:
    return CheckResult(Disposition.UNFIXABLE, detail, fix)


def _mount_description(mount: _Mount) -> str:
    return f"{mount.source} ({mount.fstype}, {format_gb(mount.size_bytes)})"


def _foreign_mount_refusal(context: ProvisionContext, mount: _Mount) -> CheckResult | None:
    if context.site is None:
        return CheckResult(
            Disposition.PENDING_INPUT,
            f"/data is mounted from {_mount_description(mount)}; "
            "the site file names the profile whose floor judges it",
            SITE_MISSING_FIX,
        )
    if context.models is None:
        return _failure(
            f"models.lock was not loaded; cannot judge /data mount {_mount_description(mount)}",
            _MODELS_FIX,
        )
    profile = select_profile(context.models, context.site.hardware_profile)
    if isinstance(profile, Problem):
        return _failure(profile.problem, profile.fix)
    floor = profile.requires.data_volume_gb
    if mount.fstype not in _SUPPORTED_FILESYSTEMS or (
        not context.no_gpu and mount.size_bytes < floor * GIGABYTE
    ):
        needed = "xfs or ext4 filesystem"
        if not context.no_gpu:
            needed += f" of at least {floor} GB"
        return _failure(
            box_wide_shortfall(
                _DATA_MOUNT_SETTING,
                f"{mount.fstype}, {mount.size_bytes} bytes ({format_gb(mount.size_bytes)})",
                needed,
                mount.source,
            ),
            BOX_WIDE_SHORTFALL_FIX,
        )
    return None


def _directory_result(
    context: ProvisionContext, inspection: _Inspection, *, detail: str
) -> CheckResult:
    if inspection.uid is None or inspection.gid is None:
        return CheckResult(
            Disposition.DRIFT,
            "the gideon service user is missing; data directories cannot yet be owned",
            "Converge the service-user step, then re-run provision.",
        )
    missing_directory = _directory_state(context.host, inspection.uid, inspection.gid)
    if missing_directory is not None:
        return CheckResult(
            Disposition.DRIFT,
            missing_directory,
            "Create the /data directory tree owned by gideon, then re-run provision.",
        )
    return CheckResult(Disposition.CONVERGED, detail, "")


def _apply_directories(context: ProvisionContext) -> None:
    entry = passwd_entry(context, "gideon")
    if entry is None:
        raise StepFailure(
            "the gideon service user is missing; data directories cannot yet be owned",
            "Converge the service-user step, then re-run provision.",
        )
    owners = _owner_ids(entry.uid, entry.gid)
    for directory in _DATA_DIRS:
        path = _DATA_MOUNT / directory.relative_path
        owner_uid, owner_gid = owners[directory.owner]
        context.host.mkdir(path, mode=directory.mode, exist_ok=True)
        context.host.chmod(path, directory.mode)
        context.host.chown(path, owner_uid, owner_gid)


class DiskLayoutStep(Step):
    """Converge GIDEON's disk or accept a sufficient existing /data mount.

    A foreign mount is never changed; only GIDEON's directories are made on it.
    """

    name = "disk-layout"
    summary = "identify disks and converge the data filesystem safely"
    requires = ("service-user",)
    settings = (_DATA_MOUNT_SETTING,)

    def check(self, context: ProvisionContext) -> CheckResult:
        inspection = _inspect(context)
        if inspection.error is not None:
            return _failure(inspection.error, inspection.error_fix)
        if inspection.mount_error is not None:
            return _failure(inspection.mount_error, _MOUNT_READ_FIX)
        if inspection.data_list_error is not None:
            return _failure(
                inspection.data_list_error,
                _DATA_DIR_FIX,
            )
        if inspection.data_names:
            return _failure(
                _occupied_data_detail(inspection.data_names), _DATA_MOVE_FIX
            )
        if not inspection.owned:
            assert inspection.mount is not None
            refusal = _foreign_mount_refusal(context, inspection.mount)
            if refusal is not None:
                return refusal
            return _directory_result(
                context,
                inspection,
                detail=(
                    "data layout is current; accepted /data mount "
                    f"{_mount_description(inspection.mount)}"
                ),
            )
        if inspection.data_disk is None or inspection.data_kind is None:
            return _failure("the data disk was not positively identified")
        if inspection.data_kind == "foreign":
            return _failure(
                "identified data disk has a foreign signature; "
                f"{inspection.data_disk.name} (WWN: {inspection.data_disk.wwn or '(none)'})"
            )
        if inspection.vg_exists and not inspection.pv_exists:
            return _failure(
                "vg_data exists but has no PV on the identified data disk; "
                "converging would build the LV on the wrong device"
            )
        if not inspection.pv_exists:
            return CheckResult(
                Disposition.DRIFT,
                "the data disk PV is missing",
                "Create the data PV, then re-run provision.",
            )
        if not inspection.vg_exists:
            return CheckResult(
                Disposition.DRIFT,
                "the vg_data volume group is missing",
                "Create vg_data on the identified data disk, then re-run provision.",
            )
        if not inspection.lv_exists:
            if inspection.vg_size is None:
                return _failure("the vg_data size could not be read safely")
            if inspection.vg_size <= _RESERVE_BYTES:
                return _failure(
                    "vg_data cannot spare the required 1 TiB unallocated reserve"
                )
            return CheckResult(
                Disposition.DRIFT,
                "the vg_data/data LV is missing",
                "Create the data LV, then re-run provision.",
            )
        if inspection.filesystem is None:
            command = context.host.run(["which", "mkfs.xfs"])
            if command.returncode != 0:
                return CheckResult(
                    Disposition.DRIFT,
                    "xfsprogs is missing (mkfs.xfs was not found)",
                    "Install xfsprogs, then re-run provision.",
                )
            return CheckResult(
                Disposition.DRIFT,
                "the vg_data/data LV has no filesystem",
                "Create the XFS filesystem on the data LV, then re-run provision.",
            )
        if inspection.filesystem != "xfs":
            return _failure("the data LV carries a filesystem other than XFS")
        if inspection.uuid is None:
            return _failure("the XFS data LV has no readable UUID")
        expected = _fstab_block(inspection.uuid)
        if inspection.fstab is None:
            return _failure("/etc/fstab is unreadable")
        if not _fstab_has_block(inspection.fstab, expected):
            return CheckResult(
                Disposition.DRIFT,
                "the managed /data fstab entry is missing or differs",
                "Rewrite the managed /data fstab block, then re-run provision.",
            )
        if _has_unmanaged_data_entry(inspection.fstab):
            return CheckResult(
                Disposition.DRIFT,
                "an unmanaged /data fstab entry exists beside the managed block",
                "Rewrite the managed /data fstab block, then re-run provision.",
            )
        if inspection.mount is None:
            return CheckResult(
                Disposition.DRIFT,
                "/data is not mounted",
                "Mount the data filesystem at /data, then re-run provision.",
            )
        return _directory_result(context, inspection, detail="data layout is current")

    def apply(self, context: ProvisionContext) -> None:
        inspection = _inspect(context)
        if inspection.error is not None:
            raise StepFailure(inspection.error, inspection.error_fix)
        if inspection.mount_error is not None:
            raise StepFailure(inspection.mount_error, _MOUNT_READ_FIX)
        if inspection.data_list_error is not None:
            raise StepFailure(inspection.data_list_error, _DATA_DIR_FIX)
        if inspection.data_names:
            raise StepFailure(
                _occupied_data_detail(inspection.data_names), _DATA_MOVE_FIX
            )
        if not inspection.owned:
            assert inspection.mount is not None
            refusal = _foreign_mount_refusal(context, inspection.mount)
            if refusal is not None:
                raise StepFailure(refusal.detail, refusal.fix)
            _apply_directories(context)
            return
        if inspection.data_disk is None or inspection.data_kind == "foreign":
            raise RuntimeError("refusing to mutate a foreign or unidentified data disk")
        if inspection.vg_exists and not inspection.pv_exists:
            raise RuntimeError(
                "vg_data exists but has no PV on the identified data disk"
            )
        # Mutate through the stable identity, never the enumeration name: the
        # by-id symlink re-validates the WWN at the moment of use ([36]).
        data_device = f"/dev/disk/by-id/wwn-{inspection.data_disk.wwn}"
        if not context.host.exists(data_device):
            raise RuntimeError(
                f"stable identity path {data_device} does not resolve; "
                "refusing to mutate by enumeration name"
            )
        if not inspection.pv_exists:
            context.host.run(["pvcreate", data_device], check=True)
        if not inspection.vg_exists:
            context.host.run(["vgcreate", _VG, data_device], check=True)
        if not inspection.lv_exists:
            # Size from the VG's own capacity, never the raw disk: PV metadata
            # and extent rounding make the disk size an over-ask.
            vg_size = _vg_size_bytes(context.host)
            if vg_size is None:
                raise RuntimeError("cannot read the vg_data size")
            size = vg_size - _RESERVE_BYTES
            if size <= 0:
                raise RuntimeError(
                    "vg_data is smaller than the 1 TiB unallocated reserve"
                )
            context.host.run(
                ["lvcreate", "--name", _LV, "--size", f"{size}B", _VG],
                check=True,
            )
        if inspection.filesystem is None:
            command = context.host.run(["which", "mkfs.xfs"])
            if command.returncode != 0:
                apt_install(context, ["xfsprogs"])
            context.host.run(["mkfs.xfs", "/dev/vg_data/data"], check=True)
        uuid = _blkid(context.host, "UUID")
        if uuid is None:
            raise RuntimeError("mkfs.xfs did not produce a readable UUID")
        current_fstab = _read_fstab(context.host)
        if current_fstab is None:
            raise RuntimeError("/etc/fstab is unreadable")
        expected = _fstab_block(uuid)
        if not _fstab_has_block(current_fstab, expected):
            context.host.write_text(_FSTAB, _replace_fstab(current_fstab, expected))
        context.host.mkdir(_DATA_MOUNT, mode=0o755, exist_ok=True)
        current_mount, mount_error = _read_mount(context.host)
        if mount_error is not None:
            raise StepFailure(mount_error, _MOUNT_READ_FIX)
        if current_mount is not None and current_mount.source not in _OWN_SOURCES:
            raise StepFailure(
                f"/data was mounted from {_mount_description(current_mount)} "
                "while provision built GIDEON's volume",
                "Re-run provision to judge the new /data mount.",
            )
        if current_mount is None:
            context.host.run(["mount", str(_DATA_MOUNT)], check=True)
        _apply_directories(context)
