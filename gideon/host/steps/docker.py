"""Convergence of Docker, its daemon policy, and journald retention.

The step installs Docker by the recipe's two apt files when it is absent,
accepts it when present and sufficient whatever file installed it, and refuses
a second apt source for its repository before writing anything, since two
entries under different keys break apt for the whole box. It owns the keys
`dockerdaemon.OWNED_KEYS` names, sets each only at its default, and keeps every
other daemon key. The optional `default-address-pools` key holds the site's
range for Docker to carve into container networks.
Containerd's root and journald's storage are box-wide settings: their effects
are read, set at their defaults, accepted when met, and refused when short.
"""

import json
import re
import stat
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from gideon.host import aptsources, dockerdaemon
from gideon.host.cotenants import ACKNOWLEDGE_DISRUPTION_FLAG, guard
from gideon.host.images import is_loopback_registry, is_plain_registry, parse_registry
from gideon.host.steps import (
    BOX_WIDE_SHORTFALL_FIX,
    PREREQUISITE_FLOOR_FIX,
    BoxWideSetting,
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    StepFailure,
    apt_install,
    box_wide_shortfall,
    fetch_file,
    package_version,
    stderr_first_line,
)

_KEYRING = Path("/etc/apt/keyrings/docker.asc")
_SOURCE = Path("/etc/apt/sources.list.d/docker.sources")
_DAEMON = Path("/etc/docker/daemon.json")
_CONTAINERD_CONFIG = Path("/etc/containerd/config.toml")
_CONTAINERD_VERIFY = ("dpkg", "--verify", "containerd.io")
_CONTAINERD_DUMP = ("containerd", "--config", str(_CONTAINERD_CONFIG), "config", "dump")
_CONTAINERD_DEFAULT_ROOT = Path("/var/lib/containerd")
_CONTAINERD_ROOT = Path("/var/lib/docker/containerd")
_JOURNALD = Path("/etc/systemd/journald.conf.d/gideon.conf")
_JOURNALD_CAT = ("systemd-analyze", "cat-config", "systemd/journald.conf")
_JOURNALD_KEYS = ("Storage", "SystemMaxUse", "MaxRetentionSec")
_JOURNAL_DIR = Path("/var/log/journal")
# cat-config opens each file with a "# <path>" line; the shipped file's own
# comments also start "# /", so a header is a whole line naming one path.
_JOURNALD_HEADER = re.compile(r"^# (/\S+)$")
_KEY_URL = "https://download.docker.com/linux/ubuntu/gpg"
_REPOSITORY = "https://download.docker.com/linux/ubuntu"
_SOURCE_TYPE = "deb"
# The suite is the codename of the release pinned by the lock and moves with it.
_SUITE = "resolute"
_COMPONENT = "stable"
_ARCHITECTURE = "amd64"
_REPO = (
    f"Types: {_SOURCE_TYPE}\n"
    f"URIs: {_REPOSITORY}\n"
    f"Suites: {_SUITE}\n"
    f"Components: {_COMPONENT}\n"
    f"Architectures: {_ARCHITECTURE}\n"
    f"Signed-By: {_KEYRING}\n"
)
_PACKAGES = (
    "docker-ce",
    "docker-ce-cli",
    "containerd.io",
    "docker-buildx-plugin",
    "docker-compose-plugin",
)
_UNREADABLE_SOURCE_FIX = "Repair {file} so it can be read, then re-run provision."
_SEVERAL_SOURCES_FIX = "Keep one apt source for Docker's repository and remove the others from {files}, then re-run provision."
_FOREIGN_SOURCE_FIX = "Remove {file} or install Docker from it yourself, then re-run provision."
_OCCUPIED_SOURCE_FIX = (
    f"Remove {_SOURCE} or complete it as the recipe's source, then re-run provision."
)
_NO_SOURCE_FIX = (
    f"Add the recipe's {_KEYRING} and {_SOURCE} or install the packages yourself, "
    "then re-run provision."
)
_JOURNALD_TEXT = "[Journal]\nStorage=persistent\nSystemMaxUse=50G\nMaxRetentionSec=90day\n"
# Read from the installed containerd's `containerd config default` and `config
# dump` on 2026-09-10. The metadata database is created on every
# start, so store presence is judged by snapshot and content entries instead.
_CONTAINERD_TEXT = "version = 4\nroot = '/var/lib/docker/containerd'\ndisabled_plugins = ['io.containerd.grpc.v1.cri']\n"
_CONTAINERD_STORE_PATHS = (
    Path("io.containerd.snapshotter.v1.overlayfs/snapshots"),
    Path("io.containerd.content.v1.content/blobs/sha256"),
)
_CONTAINERD_STORE_FIX = "Follow the containerd store move procedure in docs/runbooks/install-upgrade.md §7, then re-run provision."
_CONTAINERD_LIST_FIX = "Repair the containerd store directory named above, then re-run provision."
_CONTAINERD_VERIFY_FIX = "Repair dpkg's containerd.io verification, then re-run provision."
_CONTAINERD_DUMP_FIX = f"Repair {_CONTAINERD_CONFIG} or the files it imports by hand (containerd config dump names the error), then re-run provision."
_CONTAINERD_READ_FIX = f"Repair access to {_CONTAINERD_CONFIG}, then re-run provision."
_JOURNALD_READ_FIX = "Repair journald's configuration so systemd-analyze can read it, then re-run provision."
_JOURNALD_DROP_IN_FIX = f"Repair access to {_JOURNALD}, then re-run provision."
_DAEMON_OBJECT_FIX = f"Repair {_DAEMON} by hand as one JSON object, then re-run provision."
_DAEMON_CONTAINER_FIX = f"Repair the named key in {_DAEMON} by hand, then re-run provision."
_DAEMON_SET_FIX = f"Announce a maintenance window to every project sharing the daemon (docs/runbooks/install-upgrade.md §9), since writing GIDEON's daemon.json keys restarts Docker, which reaches every container on the box; then re-run provision {ACKNOWLEDGE_DISRUPTION_FLAG}."
_DAEMON_SHORT_FIX = f"Agree the value of the named key with the box's other operators, set it in {_DAEMON} and restart Docker in an announced maintenance window, since the restart reaches every container, then re-run provision. See docs/runbooks/install-upgrade.md §10."
# Read the binaries' reports, not dpkg's epoch-prefixed versions (such as
# 5:29...). docker --version comes from docker-ce-cli, shipped at the engine version.
_VERSION = re.compile(r"(?:^|\s)v?(\d+)(?:\.(\d+))?")


@dataclass(frozen=True)
class _VersionReading:
    result: CheckResult | None
    docker_absent: bool


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


class _ContainerdKind(Enum):
    """containerd's config.toml as read; a moved file short of the need is a refusal."""

    ABSENT = "absent"
    OWN = "own"
    PACKAGE_DEFAULT = "package_default"
    MOVED_MET = "moved_met"


# The readings where provision writes its own containerd file and restarts.
_CONTAINERD_WRITABLE = (_ContainerdKind.ABSENT, _ContainerdKind.PACKAGE_DEFAULT)


@dataclass(frozen=True)
class _JournalKey:
    name: str
    value: str
    file: str


@dataclass(frozen=True)
class _JournaldReading:
    outside: tuple[_JournalKey, ...]
    drop_in: str | None


def _keyring_empty(context: ProvisionContext) -> bool:
    try:
        return not context.host.read_text(_KEYRING).strip()
    except (OSError, UnicodeError):
        return True


def _version(text: str) -> tuple[int, int] | None:
    match = _VERSION.search(text)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2) or 0)


def _version_reading(context: ProvisionContext) -> _VersionReading:
    """Report the first version problem and whether the engine is absent."""

    requirements = (
        (
            True,
            ("docker", "--version"),
            "docker-ce",
            context.lock.minimums.docker,
            "Docker is not installed",
            "Install the locked Docker engine packages, then re-run provision.",
        ),
        (
            False,
            ("docker", "compose", "version"),
            "docker-compose-plugin",
            context.lock.minimums.compose,
            "the Docker Compose plugin is not installed",
            "Install the locked Docker Compose plugin, then re-run provision.",
        ),
    )
    for docker_probe, argv, package, floor, absent_detail, absent_fix in requirements:
        probe = context.host.run(argv)
        version = _version(probe.stdout) if probe.returncode == 0 else None
        if version is None:
            return _VersionReading(
                CheckResult(Disposition.DRIFT, absent_detail, absent_fix),
                docker_absent=docker_probe,
            )
        if version[0] < floor:
            return _VersionReading(
                CheckResult(
                    Disposition.UNFIXABLE,
                    f"{package} is at {version[0]}.{version[1]}, below the floor {floor}",
                    PREREQUISITE_FLOOR_FIX.format(package=package, floor=floor),
                ),
                docker_absent=False,
            )
    return _VersionReading(None, docker_absent=False)


def _recipe_entry(entry: aptsources.Entry) -> bool:
    return (
        entry.file == str(_SOURCE)
        and _SOURCE_TYPE in entry.types
        and any(aptsources.same_repository(uri, _REPOSITORY) for uri in entry.uris)
        and _SUITE in entry.suites
        and _COMPONENT in entry.components
        and (entry.signed_by or "").strip() == str(_KEYRING)
        and (not entry.architectures or _ARCHITECTURE in entry.architectures)
    )


def _source_reading(context: ProvisionContext) -> _SourceReading:
    scan = aptsources.entries_for(context.host, _REPOSITORY)
    if scan.unreadable:
        path, error = scan.unreadable[0]
        return _SourceReading(_SourceKind.UNREADABLE, (path,), error=error)
    if len(scan.entries) > 1:
        return _SourceReading(
            _SourceKind.SEVERAL,
            tuple(sorted({entry.file for entry in scan.entries})),
            count=len(scan.entries),
        )
    if scan.entries:
        entry = scan.entries[0]
        kind = _SourceKind.RECIPE if _recipe_entry(entry) else _SourceKind.FOREIGN
        return _SourceReading(kind, (entry.file,))
    if context.host.exists(_SOURCE):
        return _SourceReading(_SourceKind.OCCUPIED, (str(_SOURCE),))
    return _SourceReading(_SourceKind.NONE)


def _missing_packages(context: ProvisionContext) -> tuple[str, ...]:
    return tuple(
        package for package in _PACKAGES if package_version(context, package) is None
    )


def _source_refusal(
    source: _SourceReading, *, docker_absent: bool, missing: tuple[str, ...] = ()
) -> tuple[str, str] | None:
    if source.kind is _SourceKind.UNREADABLE:
        return (
            f"cannot read {source.files[0]}: {source.error}",
            _UNREADABLE_SOURCE_FIX.format(file=source.files[0]),
        )
    if source.kind is _SourceKind.SEVERAL:
        return (
            f"{source.count} apt sources name Docker's repository: {', '.join(source.files)}",
            _SEVERAL_SOURCES_FIX.format(files=", ".join(source.files)),
        )
    if docker_absent and source.kind is _SourceKind.FOREIGN:
        return (
            f"{source.files[0]} names Docker's repository and is not the recipe's source",
            _FOREIGN_SOURCE_FIX.format(file=source.files[0]),
        )
    if docker_absent and source.kind is _SourceKind.OCCUPIED:
        return (
            f"{_SOURCE} holds no enabled entry for Docker's repository",
            _OCCUPIED_SOURCE_FIX,
        )
    if missing:
        return (
            f"{', '.join(missing)} not installed and no apt source serves Docker's repository",
            _NO_SOURCE_FIX,
        )
    return None


def _daemon_needs(context: ProvisionContext) -> tuple[dockerdaemon.Need, ...]:
    egress_proxy = context.site.egress_proxy if context.site is not None else None
    address_pool = context.site.docker_address_pool if context.site is not None else None
    insecure_registry = None
    if context.site is not None:
        target = parse_registry(context.site.registry)
        if (
            target is not None
            and is_plain_registry(target.authority)
            and not is_loopback_registry(target.authority)
        ):
            insecure_registry = target.authority
    return dockerdaemon.needs(egress_proxy, insecure_registry, address_pool)


def _daemon_read(
    context: ProvisionContext, wanted: tuple[dockerdaemon.Need, ...]
) -> CheckResult | tuple[dict[str, object], dockerdaemon.Reading] | None:
    if not context.host.exists(_DAEMON):
        return None
    try:
        current = json.loads(context.host.read_text(_DAEMON))
    except (ValueError, OSError, UnicodeError) as exc:
        return CheckResult(
            Disposition.UNFIXABLE,
            f"{_DAEMON} is not a JSON object: {exc}",
            _DAEMON_OBJECT_FIX,
        )
    if not isinstance(current, dict):
        return CheckResult(
            Disposition.UNFIXABLE,
            f"{_DAEMON} is not a JSON object: {dockerdaemon.render(current)}",
            _DAEMON_OBJECT_FIX,
        )
    return current, dockerdaemon.read(current, wanted)


def _daemon_mode(context: ProvisionContext) -> int:
    # Another key may hold a credential, so a rewrite keeps the file's own
    # permissions rather than widening them to the default.
    try:
        return stat.S_IMODE(context.host.stat(_DAEMON).st_mode)
    except FileNotFoundError:
        return 0o644


def _daemon_refusal(reading: dockerdaemon.Reading) -> CheckResult | None:
    if reading.malformed:
        detail = "; ".join(
            f"{name} is {found}, not {'a list' if dockerdaemon.CONTAINER_KINDS[name] is list else 'an object'}"
            for name, found in reading.malformed
        )
        return CheckResult(Disposition.UNFIXABLE, detail, _DAEMON_CONTAINER_FIX)
    if reading.short:
        detail = "; ".join(
            f"{name} is {found}, GIDEON needs {needed}"
            for name, found, needed in reading.short
        )
        return CheckResult(Disposition.UNFIXABLE, detail, _DAEMON_SHORT_FIX)
    return None


def _containerd_root(context: ProvisionContext) -> str | CheckResult:
    """The root containerd runs with, its imports merged in, as containerd reports it."""

    try:
        result = context.host.run(_CONTAINERD_DUMP)
    except OSError as exc:
        return CheckResult(
            Disposition.UNFIXABLE,
            f"containerd could not report its configuration: {exc}",
            _CONTAINERD_DUMP_FIX,
        )
    if result.returncode != 0:
        # containerd logs a structured line before the error it exits with.
        lines = result.stderr.strip().splitlines()
        reason = lines[-1] if lines else "no diagnostic"
        return CheckResult(
            Disposition.UNFIXABLE,
            f"containerd could not report its configuration: {reason}",
            _CONTAINERD_DUMP_FIX,
        )
    try:
        root = tomllib.loads(result.stdout).get("root")
    except tomllib.TOMLDecodeError as exc:
        return CheckResult(
            Disposition.UNFIXABLE,
            f"cannot parse containerd's configuration dump: {exc}",
            _CONTAINERD_DUMP_FIX,
        )
    if not isinstance(root, str):
        return CheckResult(
            Disposition.UNFIXABLE,
            "containerd's configuration dump names no root",
            _CONTAINERD_DUMP_FIX,
        )
    return root


def _containerd_package_default(context: ProvisionContext) -> bool | CheckResult:
    """True when dpkg finds the package's config.toml unmodified."""

    try:
        verified = context.host.run(_CONTAINERD_VERIFY)
    except OSError as exc:
        return CheckResult(
            Disposition.UNFIXABLE,
            f"dpkg could not verify containerd.io: {exc}",
            _CONTAINERD_VERIFY_FIX,
        )
    if verified.returncode not in (0, 1):
        return CheckResult(
            Disposition.UNFIXABLE,
            f"dpkg could not verify containerd.io: {stderr_first_line(verified.stderr)}",
            _CONTAINERD_VERIFY_FIX,
        )
    return not any(
        line.split()[-1:] == [str(_CONTAINERD_CONFIG)]
        for line in verified.stdout.splitlines()
    )


def _containerd_read(
    context: ProvisionContext, setting: BoxWideSetting
) -> _ContainerdKind | CheckResult:
    """Classify config.toml: its text and dpkg say whether GIDEON may write it,
    and the root in effect, imports included, says whether the need is met."""

    text = None
    if context.host.exists(_CONTAINERD_CONFIG):
        try:
            text = context.host.read_text(_CONTAINERD_CONFIG)
        except (OSError, UnicodeError) as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read {_CONTAINERD_CONFIG}: {exc}",
                _CONTAINERD_READ_FIX,
            )
    root = _containerd_root(context)
    if isinstance(root, CheckResult):
        return root
    if root == str(_CONTAINERD_ROOT):
        return _ContainerdKind.OWN if text == _CONTAINERD_TEXT else _ContainerdKind.MOVED_MET
    at_default = text is None
    if text is not None and text != _CONTAINERD_TEXT:
        verified = _containerd_package_default(context)
        if isinstance(verified, CheckResult):
            return verified
        at_default = verified
    if at_default and root == str(_CONTAINERD_DEFAULT_ROOT):
        return _ContainerdKind.ABSENT if text is None else _ContainerdKind.PACKAGE_DEFAULT
    # A file at the default, or provision's own, cannot set this root itself:
    # a file it imports moved it.
    moved_here = text is not None and text != _CONTAINERD_TEXT and not at_default
    source = str(_CONTAINERD_CONFIG) if moved_here else f"a file {_CONTAINERD_CONFIG} imports"
    return CheckResult(
        Disposition.UNFIXABLE,
        box_wide_shortfall(setting, root, str(_CONTAINERD_ROOT), source),
        BOX_WIDE_SHORTFALL_FIX,
    )


def _journald_read(
    context: ProvisionContext, setting: BoxWideSetting
) -> _JournaldReading | CheckResult:
    try:
        result = context.host.run(_JOURNALD_CAT)
    except OSError as exc:
        return CheckResult(
            Disposition.UNFIXABLE,
            f"systemd-analyze could not report journald's configuration: {exc}",
            _JOURNALD_READ_FIX,
        )
    if result.returncode != 0:
        return CheckResult(
            Disposition.UNFIXABLE,
            f"systemd-analyze could not report journald's configuration: {stderr_first_line(result.stderr)}",
            _JOURNALD_READ_FIX,
        )
    values: dict[str, _JournalKey | None] = dict.fromkeys(_JOURNALD_KEYS)
    file = "/etc/systemd/journald.conf"
    section = ""
    for line in result.stdout.splitlines():
        stripped = line.strip()
        header = _JOURNALD_HEADER.match(stripped)
        if header is not None:
            file = header.group(1)
            section = ""
        elif stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
        elif section == "Journal" and not stripped.startswith(("#", ";")) and "=" in stripped:
            name, value = (part.strip() for part in stripped.split("=", 1))
            if name in values:
                values[name] = _JournalKey(name, value, file) if value else None
    outside = tuple(
        key for key in values.values() if key is not None and key.file != str(_JOURNALD)
    )
    # Only a configuration another file moved is judged; at the default GIDEON sets it.
    storage = values["Storage"]
    value = storage.value if storage is not None else "auto"
    if outside and not (
        value == "persistent" or (value == "auto" and context.host.exists(_JOURNAL_DIR))
    ):
        found = f"Storage=auto without {_JOURNAL_DIR}" if value == "auto" else f"Storage={value}"
        source = (
            storage.file if storage is not None else "/etc/systemd/journald.conf's compiled default"
        )
        return CheckResult(
            Disposition.UNFIXABLE,
            box_wide_shortfall(setting, found, "persistent storage", source),
            BOX_WIDE_SHORTFALL_FIX,
        )
    drop_in = None
    if not outside and context.host.exists(_JOURNALD):
        try:
            drop_in = context.host.read_text(_JOURNALD)
        except (OSError, UnicodeError) as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read {_JOURNALD}: {exc}",
                _JOURNALD_DROP_IN_FIX,
            )
    return _JournaldReading(outside, drop_in)


def _store_populated(context: ProvisionContext, root: Path) -> bool:
    populated = False
    for relative_path in _CONTAINERD_STORE_PATHS:
        path = root / relative_path
        try:
            entries = context.host.listdir(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise OSError(f"{path}: {exc}") from exc
        populated = populated or bool(entries)
    return populated


def _store_refusal(context: ProvisionContext) -> CheckResult | None:
    try:
        default_populated = _store_populated(context, _CONTAINERD_DEFAULT_ROOT)
        desired_populated = _store_populated(context, _CONTAINERD_ROOT)
    except OSError as exc:
        return CheckResult(
            Disposition.UNFIXABLE,
            f"cannot list the containerd store: {exc}",
            _CONTAINERD_LIST_FIX,
        )
    if default_populated and not desired_populated:
        return CheckResult(
            Disposition.UNFIXABLE,
            f"containerd store is populated under {_CONTAINERD_DEFAULT_ROOT} but empty under {_CONTAINERD_ROOT}",
            _CONTAINERD_STORE_FIX,
        )
    return None


class DockerEngineStep(Step):
    """Converge Docker packages, daemon policy, and service state."""

    name = "docker-engine"
    summary = "install Docker and converge its daemon, containerd, and journald policies"
    requires = ("disk-layout",)
    settings = (BoxWideSetting("journald storage"), BoxWideSetting("containerd root"))

    def check(self, context: ProvisionContext) -> CheckResult:
        source = _source_reading(context)
        refusal = _source_refusal(source, docker_absent=False)
        if refusal is not None:
            return CheckResult(Disposition.UNFIXABLE, *refusal)
        version = _version_reading(context)
        if (
            version.result is not None
            and version.result.disposition is Disposition.UNFIXABLE
        ):
            return version.result
        missing = (
            _missing_packages(context)
            if not version.docker_absent
            and source.kind in (_SourceKind.NONE, _SourceKind.OCCUPIED)
            else ()
        )
        refusal = _source_refusal(
            source, docker_absent=version.docker_absent, missing=missing
        )
        if refusal is not None:
            return CheckResult(Disposition.UNFIXABLE, *refusal)
        wanted = _daemon_needs(context)
        daemon = _daemon_read(context, wanted)
        if isinstance(daemon, CheckResult):
            return daemon
        if daemon is not None:
            refusal_result = _daemon_refusal(daemon[1])
            if refusal_result is not None:
                return refusal_result
        containerd = None
        journald = None
        if not version.docker_absent:
            store_refusal = _store_refusal(context)
            if store_refusal is not None:
                return store_refusal
            containerd = _containerd_read(context, self.settings[1])
            if isinstance(containerd, CheckResult):
                return containerd
            journald = _journald_read(context, self.settings[0])
            if isinstance(journald, CheckResult):
                return journald
        if version.result is not None:
            return version.result

        assert isinstance(containerd, _ContainerdKind)
        assert isinstance(journald, _JournaldReading)
        if daemon is None:
            return CheckResult(Disposition.DRIFT, f"{_DAEMON} is missing", "Write GIDEON's daemon.json keys, then re-run provision.")
        reading = daemon[1]
        if reading.to_set:
            keys = ", ".join(reading.to_set)
            return CheckResult(Disposition.DRIFT, f"{_DAEMON} lacks GIDEON's keys: {keys}", _DAEMON_SET_FIX)
        if containerd in _CONTAINERD_WRITABLE:
            return CheckResult(
                Disposition.DRIFT,
                f"{_CONTAINERD_CONFIG} is at the package default; GIDEON sets containerd's root",
                f"Run provision, which writes {_CONTAINERD_CONFIG} and restarts containerd and Docker.",
            )
        if not journald.outside and journald.drop_in != _JOURNALD_TEXT:
            if journald.drop_in is None:
                return CheckResult(Disposition.DRIFT, f"{_JOURNALD} is missing", "Write the journald retention drop-in, then re-run provision.")
            return CheckResult(Disposition.DRIFT, f"{_JOURNALD} differs from the desired retention policy", "Rewrite the journald retention drop-in, then re-run provision.")
        containerd_enabled = context.host.run(["systemctl", "is-enabled", "containerd"])
        containerd_active = context.host.run(["systemctl", "is-active", "containerd"])
        docker_enabled = context.host.run(["systemctl", "is-enabled", "docker"])
        docker_active = context.host.run(["systemctl", "is-active", "docker"])
        if (
            containerd_enabled.returncode != 0
            or containerd_active.returncode != 0
            or docker_enabled.returncode != 0
            or docker_active.returncode != 0
        ):
            return CheckResult(Disposition.DRIFT, "Docker and containerd are not enabled and active", "Enable and start Docker and containerd, then re-run provision.")
        detail = "Docker, containerd, and their host policies are current"
        if source.kind is _SourceKind.FOREIGN and source.files[0] == str(_SOURCE):
            detail += f"; Docker's apt source {_SOURCE} is not the recipe's entry"
        elif source.kind is _SourceKind.FOREIGN:
            detail += f"; Docker's apt source is {source.files[0]}, not the recipe's {_SOURCE}"
        if reading.foreign:
            keys = ", ".join(reading.foreign)
            detail += f"; daemon.json also holds keys GIDEON does not own: {keys}"
        if containerd is _ContainerdKind.MOVED_MET:
            detail += f"; {_CONTAINERD_CONFIG} is not provision's text, its root is {_CONTAINERD_ROOT}"
        if journald.outside:
            detail += "; " + ", ".join(
                f"journald's {key.name} is set by {key.file}" for key in journald.outside
            )
        return CheckResult(Disposition.CONVERGED, detail, "")

    def apply(self, context: ProvisionContext) -> None:
        store_refusal = _store_refusal(context)
        if store_refusal is not None:
            raise StepFailure(store_refusal.detail, store_refusal.fix)
        version = _version_reading(context)
        if (
            version.result is not None
            and version.result.disposition is Disposition.UNFIXABLE
        ):
            raise StepFailure(version.result.detail, version.result.fix)
        source = _source_reading(context)
        missing = (
            _missing_packages(context)
            if not version.docker_absent
            and source.kind in (_SourceKind.NONE, _SourceKind.OCCUPIED)
            else ()
        )
        refusal = _source_refusal(
            source, docker_absent=version.docker_absent, missing=missing
        )
        if refusal is not None:
            raise StepFailure(*refusal)
        wanted = _daemon_needs(context)
        daemon = _daemon_read(context, wanted)
        if isinstance(daemon, CheckResult):
            raise StepFailure(daemon.detail, daemon.fix)
        if daemon is not None:
            refusal_result = _daemon_refusal(daemon[1])
            if refusal_result is not None:
                raise StepFailure(refusal_result.detail, refusal_result.fix)
        journald = _journald_read(context, self.settings[0])
        if isinstance(journald, CheckResult):
            raise StepFailure(journald.detail, journald.fix)
        containerd = None
        if not version.docker_absent:
            containerd = _containerd_read(context, self.settings[1])
            if isinstance(containerd, CheckResult):
                raise StepFailure(containerd.detail, containerd.fix)
        current = daemon[0] if daemon is not None else {}
        merged, daemon_changed = dockerdaemon.merge(current, wanted)
        journald_written = not journald.outside and journald.drop_in != _JOURNALD_TEXT
        # An absent Docker's containerd file is read only after the install, so
        # its write is counted as one to come; the guard passes there anyway,
        # since no runtime is running containers.
        restarts = []
        if version.docker_absent or containerd in _CONTAINERD_WRITABLE:
            restarts.append("restarting containerd and Docker")
        elif daemon_changed:
            restarts.append("restarting Docker")
        if journald_written:
            restarts.append("restarting journald")
        if restarts:
            guard(context, " and ".join(restarts))

        if version.docker_absent:
            context.host.mkdir(_KEYRING.parent, mode=0o755, parents=True, exist_ok=True)
            # Keyring strictly before the source entry: a failed fetch must never
            # leave a source line referencing a keyring that is not there, or every
            # later apt-get update on the box fails.
            if not context.host.exists(_KEYRING) or _keyring_empty(context):
                fetch_file(context, _KEY_URL, str(_KEYRING))
            if not context.host.exists(_SOURCE):
                context.host.write_text(_SOURCE, _REPO)
        apt_install(context, _PACKAGES)
        if version.docker_absent:
            containerd = _containerd_read(context, self.settings[1])
            if isinstance(containerd, CheckResult):
                raise StepFailure(containerd.detail, containerd.fix)
        assert isinstance(containerd, _ContainerdKind)

        if daemon_changed:
            context.host.mkdir(_DAEMON.parent, mode=0o755, parents=True, exist_ok=True)
            context.host.write_text(_DAEMON, dockerdaemon.text(merged), mode=_daemon_mode(context))

        containerd_written = containerd in _CONTAINERD_WRITABLE
        if containerd_written:
            context.host.mkdir(_CONTAINERD_CONFIG.parent, mode=0o755, parents=True, exist_ok=True)
            context.host.write_text(_CONTAINERD_CONFIG, _CONTAINERD_TEXT)
        context.host.run(["systemctl", "enable", "--now", "containerd"], check=True)
        if containerd_written:
            context.host.run(["systemctl", "restart", "containerd"], check=True)

        if journald_written:
            context.host.mkdir(_JOURNALD.parent, mode=0o755, parents=True, exist_ok=True)
            context.host.write_text(_JOURNALD, _JOURNALD_TEXT)
            context.host.run(["systemctl", "restart", "systemd-journald"], check=True)
        context.host.run(["systemctl", "enable", "--now", "docker"], check=True)
        if daemon_changed or containerd_written:
            # enable --now does not re-read daemon.json on an already-running
            # engine; only an owned key's change earns that restart. It reaches
            # every container on the box. A containerd root change likewise
            # needs Docker to reconnect.
            context.host.run(["systemctl", "restart", "docker"], check=True)
