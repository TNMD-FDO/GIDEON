"""Convergence of Docker, its daemon policy, and journald retention.

The step installs Docker by the recipe's two apt files when it is absent,
accepts it when present and sufficient whatever file installed it, and refuses
a second apt source for its repository before writing anything, since two
entries under different keys break apt for the whole box.
"""

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from gideon.host import aptsources
from gideon.host.images import is_loopback_registry, is_plain_registry, parse_registry
from gideon.host.steps import (
    PREREQUISITE_FLOOR_FIX,
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    StepFailure,
    apt_install,
    fetch_file,
    package_version,
)

_KEYRING = Path("/etc/apt/keyrings/docker.asc")
_SOURCE = Path("/etc/apt/sources.list.d/docker.sources")
_DAEMON = Path("/etc/docker/daemon.json")
_CONTAINERD_CONFIG = Path("/etc/containerd/config.toml")
_CONTAINERD_DEFAULT_ROOT = Path("/var/lib/containerd")
_CONTAINERD_ROOT = Path("/var/lib/docker/containerd")
_JOURNALD = Path("/etc/systemd/journald.conf.d/gideon.conf")
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


def _daemon(context: ProvisionContext) -> dict[str, object]:
    desired: dict[str, object] = {
        "data-root": "/var/lib/docker",
        "features": {"cdi": True},
        "log-driver": "journald",
    }
    if context.site is not None and context.site.egress_proxy:
        desired["proxies"] = {
            "http-proxy": context.site.egress_proxy,
            "https-proxy": context.site.egress_proxy,
        }
    if context.site is not None:
        target = parse_registry(context.site.registry)
        if (
            target is not None
            and is_plain_registry(target.authority)
            and not is_loopback_registry(target.authority)
        ):
            desired["insecure-registries"] = [target.authority]
    return desired


def _daemon_text(value: dict[str, object]) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


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


def _containerd_changed(context: ProvisionContext) -> bool:
    try:
        return context.host.read_text(_CONTAINERD_CONFIG) != _CONTAINERD_TEXT
    except (OSError, UnicodeError):
        return True


class DockerEngineStep(Step):
    """Converge Docker packages, daemon policy, and service state."""

    name = "docker-engine"
    summary = "install Docker and converge its daemon, containerd, and journald policies"
    requires = ("disk-layout",)

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
        if version.result is not None:
            return version.result

        desired = _daemon(context)
        if not context.host.exists(_DAEMON):
            return CheckResult(Disposition.DRIFT, f"{_DAEMON} is missing", f"Write the provision-owned {_DAEMON}, then re-run provision.")
        try:
            current = json.loads(context.host.read_text(_DAEMON))
        except (OSError, UnicodeError, ValueError) as exc:
            return CheckResult(Disposition.DRIFT, f"{_DAEMON} is not valid JSON: {exc}", f"Rewrite the provision-owned {_DAEMON}, then re-run provision.")
        if current != desired:
            return CheckResult(Disposition.DRIFT, f"{_DAEMON} differs from the desired Docker policy", f"Rewrite the provision-owned {_DAEMON}, then re-run provision.")
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
        if not context.host.exists(_CONTAINERD_CONFIG):
            return CheckResult(Disposition.DRIFT, f"{_CONTAINERD_CONFIG} is missing", f"Write the provision-owned {_CONTAINERD_CONFIG}, then re-run provision.")
        try:
            containerd = context.host.read_text(_CONTAINERD_CONFIG)
        except (OSError, UnicodeError) as exc:
            return CheckResult(Disposition.UNFIXABLE, f"cannot read {_CONTAINERD_CONFIG}: {exc}", f"Repair the provision-owned {_CONTAINERD_CONFIG}, then re-run provision.")
        if containerd != _CONTAINERD_TEXT:
            return CheckResult(Disposition.DRIFT, f"{_CONTAINERD_CONFIG} differs from the desired containerd policy", f"Rewrite the provision-owned {_CONTAINERD_CONFIG}, then re-run provision.")
        if not context.host.exists(_JOURNALD):
            return CheckResult(Disposition.DRIFT, f"{_JOURNALD} is missing", "Write the journald retention drop-in, then re-run provision.")
        try:
            journald = context.host.read_text(_JOURNALD)
        except (OSError, UnicodeError) as exc:
            return CheckResult(Disposition.UNFIXABLE, f"cannot read {_JOURNALD}: {exc}", "Repair the journald drop-in, then re-run provision.")
        if journald != _JOURNALD_TEXT:
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
        return CheckResult(Disposition.CONVERGED, detail, "")

    def apply(self, context: ProvisionContext) -> None:
        try:
            default_populated = _store_populated(context, _CONTAINERD_DEFAULT_ROOT)
            desired_populated = _store_populated(context, _CONTAINERD_ROOT)
        except OSError as exc:
            raise StepFailure(
                f"cannot list the containerd store: {exc}",
                _CONTAINERD_LIST_FIX,
            ) from exc
        if default_populated and not desired_populated:
            raise StepFailure(
                f"containerd store is populated under {_CONTAINERD_DEFAULT_ROOT} but empty under {_CONTAINERD_ROOT}",
                _CONTAINERD_STORE_FIX,
            )
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

        desired = _daemon(context)
        daemon_changed = True
        if context.host.exists(_DAEMON):
            try:
                daemon_changed = (
                    json.loads(context.host.read_text(_DAEMON)) != desired
                )
            except (OSError, UnicodeError, ValueError):
                daemon_changed = True
        context.host.mkdir(_DAEMON.parent, mode=0o755, parents=True, exist_ok=True)
        context.host.write_text(_DAEMON, _daemon_text(desired))

        containerd_changed = _containerd_changed(context)
        context.host.mkdir(_CONTAINERD_CONFIG.parent, mode=0o755, parents=True, exist_ok=True)
        context.host.write_text(_CONTAINERD_CONFIG, _CONTAINERD_TEXT)
        context.host.run(["systemctl", "enable", "--now", "containerd"], check=True)
        if containerd_changed:
            context.host.run(["systemctl", "restart", "containerd"], check=True)

        journald_changed = True
        if context.host.exists(_JOURNALD):
            try:
                journald_changed = context.host.read_text(_JOURNALD) != _JOURNALD_TEXT
            except (OSError, UnicodeError):
                journald_changed = True
        context.host.mkdir(_JOURNALD.parent, mode=0o755, parents=True, exist_ok=True)
        context.host.write_text(_JOURNALD, _JOURNALD_TEXT)
        if journald_changed:
            context.host.run(["systemctl", "restart", "systemd-journald"], check=True)
        context.host.run(["systemctl", "enable", "--now", "docker"], check=True)
        if daemon_changed or containerd_changed:
            # enable --now does not re-read daemon.json on an already-running
            # engine; a changed policy takes effect only across a restart.  A
            # containerd root change likewise needs Docker to reconnect.
            context.host.run(["systemctl", "restart", "docker"], check=True)
