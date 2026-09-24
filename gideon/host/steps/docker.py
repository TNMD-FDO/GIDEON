"""Convergence of Docker, its daemon policy, and journald retention."""

import json
import re
from pathlib import Path

from gideon.host.images import is_loopback_registry, is_plain_registry, parse_registry
from gideon.host.steps import (
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    StepFailure,
    apt_install,
    fetch_file,
)

_KEYRING = Path("/etc/apt/keyrings/gideon-docker.asc")
_SOURCE = Path("/etc/apt/sources.list.d/gideon-docker.list")
_DAEMON = Path("/etc/docker/daemon.json")
_CONTAINERD_CONFIG = Path("/etc/containerd/config.toml")
_CONTAINERD_DEFAULT_ROOT = Path("/var/lib/containerd")
_CONTAINERD_ROOT = Path("/var/lib/docker/containerd")
_JOURNALD = Path("/etc/systemd/journald.conf.d/gideon.conf")
_KEY_URL = "https://download.docker.com/linux/ubuntu/gpg"
_REPO = "deb [arch=amd64 signed-by=/etc/apt/keyrings/gideon-docker.asc] https://download.docker.com/linux/ubuntu resolute stable\n"
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
_VERSION = re.compile(r"(?:^|\s)v?(\d+)(?:\.(\d+))?")


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
        if not context.host.exists(_KEYRING) or not context.host.exists(_SOURCE):
            return CheckResult(Disposition.DRIFT, "the Docker APT repository is missing", "Configure the Docker APT repository, then re-run provision.")
        if _keyring_empty(context):
            return CheckResult(Disposition.DRIFT, f"{_KEYRING} is empty or unreadable", "Re-download the Docker repository key, then re-run provision.")
        try:
            source = context.host.read_text(_SOURCE)
        except (OSError, UnicodeError) as exc:
            return CheckResult(Disposition.UNFIXABLE, f"cannot read {_SOURCE}: {exc}", "Repair the Docker APT repository, then re-run provision.")
        if source != _REPO:
            return CheckResult(Disposition.DRIFT, f"{_SOURCE} does not match the Docker repository", "Rewrite the Docker APT repository, then re-run provision.")

        docker = context.host.run(["docker", "--version"])
        docker_version = _version(docker.stdout) if docker.returncode == 0 else None
        if docker_version is None or docker_version[0] < context.lock.minimums.docker:
            return CheckResult(Disposition.DRIFT, "Docker is below the locked minimum version", "Install the locked Docker engine packages, then re-run provision.")
        compose = context.host.run(["docker", "compose", "version"])
        compose_version = _version(compose.stdout) if compose.returncode == 0 else None
        if compose_version is None or compose_version[0] < context.lock.minimums.compose:
            return CheckResult(Disposition.DRIFT, "Docker Compose is below the locked minimum version", "Install the locked Docker Compose plugin, then re-run provision.")

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
        return CheckResult(Disposition.CONVERGED, "Docker, containerd, and their host policies are current", "")

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
        context.host.mkdir(_KEYRING.parent, mode=0o755, parents=True, exist_ok=True)
        # Keyring strictly before the source entry: a failed fetch must never
        # leave a source line referencing a keyring that is not there, or every
        # later apt-get update on the box fails.
        if not context.host.exists(_KEYRING) or _keyring_empty(context):
            fetch_file(context, _KEY_URL, str(_KEYRING))
        context.host.write_text(_SOURCE, _REPO)
        apt_install(
            context,
            [
                "docker-ce",
                "docker-ce-cli",
                "containerd.io",
                "docker-buildx-plugin",
                "docker-compose-plugin",
            ],
        )

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
