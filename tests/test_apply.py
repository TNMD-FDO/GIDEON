"""Apply contracts: stage order, the recreate rule, digest checks, verification (spec §3.5)."""

import argparse
import contextlib
import io
import json
import math
import os
import stat as stat_module
import subprocess
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import unquote

import yaml  # type: ignore[import-untyped]

from gideon.host import grafana, nogpu, owui, pgbackrest, weights
from gideon.host.apply import (
    _VERIFY_ATTEMPTS,
    _VERIFY_SLEEP_SECONDS,
    PRINT_ONCE_LINE,
    run_apply,
)
from gideon.host.egress import EgressAllowlist, load_egress_allowlist
from gideon.host.images import load_image_lock
from gideon.host.models import HardwareProfile, load_models_lock
from gideon.host.owui import Response
from gideon.host.render import ARTIFACTS
from gideon.host.render.command import run_render
from gideon.host.render.compose import ENGINE_READY_SECONDS
from gideon.host.render.engine import ENGINE_SERVICE_NAME
from gideon.host.render.grafana import GRAFANA_ADMIN_USER
from gideon.host.render.owui import ARITHMETIC_GUARDRAIL_ID, CITATION_STAMP_ID
from gideon.host.site import SiteConfig, load_site
from gideon.host.stack import compose_argv, exec_argv
from gideon.host.sysio import Command, Host, PathLike
from gideon.host.tls import CA_PATH, CERT_PATH

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config/site.example.yaml"
SECOND = ROOT / "tests/fixtures/site/second-office.yaml"
# Every template the artifact registry declares, so a new artifact never
# leaves this module's fake checkout behind.
TEMPLATE_PATHS = tuple(
    dict.fromkeys(path for artifact in ARTIFACTS for path in artifact.template_paths)
)
SITE = "/etc/gideon/site.yaml"
RENDERED = "/etc/gideon/rendered"
# The fakes answer for whatever the committed lock pins, so a pin-watch bump
# (ADR-0031) never breaks these contracts: the digests are the lock's, not the test's.
_COMMITTED_LOCK = load_image_lock(ROOT / "images.lock").lock
assert _COMMITTED_LOCK is not None, "the committed images.lock must load"
_DIGESTS = {pin.name: pin.digest for pin in _COMMITTED_LOCK.images}
CADDY_DIGEST = _DIGESTS["caddy"]
OPEN_WEBUI_DIGEST = _DIGESTS["open-webui"]
POSTGRES_DIGEST = _DIGESTS["postgres"]
PUBLIC_REF = f"ghcr.io/tnmd-fdo/caddy@{CADDY_DIGEST}"
LOOPBACK_REF = f"127.0.0.1:5000/caddy@{CADDY_DIGEST}"
SMI = ("nvidia-smi", "-L")
SERVICE_GROUP = ("getent", "group", "gideon")
VERSION = ("docker", "compose", "version")
PULL = tuple(compose_argv(RENDERED, "pull"))
RECREATE_CADDY = tuple(compose_argv(RENDERED, "up", "-d", "--no-deps", "--force-recreate", "caddy"))
RECREATE_POSTGRES = tuple(compose_argv(RENDERED, "up", "-d", "--no-deps", "--force-recreate", "postgres"))
RECREATE_OPEN_WEBUI = tuple(compose_argv(RENDERED, "up", "-d", "--no-deps", "--force-recreate", "open-webui"))
STORE_UP = tuple(compose_argv(RENDERED, "up", "-d", "postgres"))
UP = tuple(compose_argv(RENDERED, "up", "-d", "--remove-orphans"))
PS = tuple(compose_argv(RENDERED, "ps", "--all", "--format", "json"))
PSQL_POSTGRES_QUERY = tuple(exec_argv(RENDERED, "postgres", "psql", "-U", "postgres", "-d", "postgres", "-tA", "-f", "-"))
PSQL_POSTGRES_STATEMENT = tuple(exec_argv(RENDERED, "postgres", "psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-f", "-"))
PSQL_GIDEON_QUERY = tuple(exec_argv(RENDERED, "postgres", "psql", "-U", "gideon", "-d", "gideon", "-tA", "-f", "-"))
PSQL_GIDEON_STATEMENT = tuple(exec_argv(RENDERED, "postgres", "psql", "-U", "gideon", "-d", "gideon", "-v", "ON_ERROR_STOP=1", "-f", "-"))
PSQL_MIGRATION = tuple(exec_argv(RENDERED, "postgres", "psql", "-U", "gideon", "-d", "gideon", "-v", "ON_ERROR_STOP=1", "--single-transaction", "-f", "-"))
PG_UID = tuple(exec_argv(RENDERED, "postgres", "id", "-u", "postgres"))
PG_GID = tuple(exec_argv(RENDERED, "postgres", "id", "-g", "postgres"))
PG_STANZA_CREATE = tuple(pgbackrest.exec_argv(RENDERED, "stanza-create"))
PG_CHECK = tuple(pgbackrest.exec_argv(RENDERED, "check"))
SYSTEMCTL_RELOAD = ("systemctl", "daemon-reload")
SYSTEMCTL_LINK_SERVICE = ("systemctl", "link", f"{RENDERED}/systemd/gideon-users-reconcile.service")
SYSTEMCTL_ENABLE = ("systemctl", "enable", "--now", f"{RENDERED}/systemd/gideon-users-reconcile.timer")
SYSTEMCTL_ACTIVE = ("systemctl", "is-active", "gideon-users-reconcile.timer")
SYSTEMCTL_LINK_BACKUP = ("systemctl", "link", f"{RENDERED}/systemd/gideon-backup.service")
SYSTEMCTL_ENABLE_BACKUP = ("systemctl", "enable", "--now", f"{RENDERED}/systemd/gideon-backup.timer")
SYSTEMCTL_ACTIVE_BACKUP = ("systemctl", "is-active", "gideon-backup.timer")
SYSTEMCTL_LINK_DRILL = ("systemctl", "link", f"{RENDERED}/systemd/gideon-backup-drill.service")
SYSTEMCTL_ENABLE_DRILL = ("systemctl", "enable", "--now", f"{RENDERED}/systemd/gideon-backup-drill.timer")
SYSTEMCTL_ACTIVE_DRILL = ("systemctl", "is-active", "gideon-backup-drill.timer")
SYSTEMCTL_LINK_VERIFY = ("systemctl", "link", f"{RENDERED}/systemd/gideon-backup-verify.service")
SYSTEMCTL_ENABLE_VERIFY = ("systemctl", "enable", "--now", f"{RENDERED}/systemd/gideon-backup-verify.timer")
SYSTEMCTL_ACTIVE_VERIFY = ("systemctl", "is-active", "gideon-backup-verify.timer")
CERT = CERT_PATH
HANDSHAKE = ("openssl", "s_client", "-connect", "127.0.0.1:443", "-servername", "gideon.example.org", "-verify_hostname", "gideon.example.org", "-CAfile", CA_PATH, "-verify_return_error")
SERVED_FP = ("openssl", "x509", "-noout", "-fingerprint", "-sha256")
PLACED_FP = ("openssl", "x509", "-in", CERT, "-noout", "-fingerprint", "-sha256")
LEAF = "-----BEGIN CERTIFICATE-----\nMIIC\n-----END CERTIFICATE-----\n"
FINGERPRINT = "sha256 Fingerprint=81:2C:7A:46:FB:2D:27:AA\n"

Outcome = subprocess.CompletedProcess[str] | list[subprocess.CompletedProcess[str]]


def done(argv: tuple[str, ...], rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), rc, stdout, stderr)


def healthy_ingress() -> dict[tuple[str, ...], Outcome]:
    return {
        HANDSHAKE: done(HANDSHAKE, stdout=f"CONNECTED\n{LEAF}---\nVerify return code: 0 (ok)\n"),
        SERVED_FP: done(SERVED_FP, stdout=FINGERPRINT),
        PLACED_FP: done(PLACED_FP, stdout=FINGERPRINT),
    }


def manifest_inspect(ref: str, *, insecure: bool) -> tuple[str, ...]:
    return ("docker", "manifest", "inspect", *(("--insecure",) if insecure else ()), ref)


def image_inspect(ref: str) -> tuple[str, ...]:
    return ("docker", "image", "inspect", "--format", "{{json .RepoDigests}}", ref)


def ps_rows(*rows: Mapping[str, str]) -> str:
    return "\n".join(json.dumps(row) for row in rows) + "\n"


# The rendered project's services, from the example fixture: verify demands
# every one running, so the fake `compose ps` derives its rows here rather
# than naming services (a new service moves the fixture, never this table).
_FIXTURE_SERVICES: tuple[str, ...] = tuple(
    yaml.safe_load((ROOT / "tests/fixtures/render/example/compose.yaml").read_text())["services"]
)
_HEALTHCHECKED = frozenset({"postgres", "open-webui", ENGINE_SERVICE_NAME})


def running_rows(
    *,
    include_dcgm: bool = True,
    include_engine: bool = True,
    include_searxng: bool = True,
    omit: frozenset[str] = frozenset(),
    states: Mapping[str, str] | None = None,
) -> str:
    """Every service running (healthchecked ones healthy); *states* overrides a service's State."""

    states = states or {}
    rows = []
    services = tuple(
        service
        for service in _FIXTURE_SERVICES
        if (include_dcgm or service != "dcgm-exporter")
        and (include_engine or service != ENGINE_SERVICE_NAME)
        and (include_searxng or service != "searxng")
        and service not in omit
    )
    for service in services:
        health = "healthy" if service in _HEALTHCHECKED else ""
        state = states.get(service, "running")
        rows.append({"Service": service, "State": state, "Health": health if state == "running" else ""})
    return ps_rows(*rows)


class FakeFrontend:
    def __init__(self) -> None:
        self.users: list[dict[str, object]] = []
        self.groups: list[dict[str, object]] = []
        self.next_group = 1
        self.next_user = 1
        self.functions: list[dict[str, object]] = []
        self.models: list[dict[str, object]] = []

    def _group(self, value: Mapping[str, object]) -> dict[str, object]:
        return {
            "id": value["id"],
            "name": value["name"],
            "description": value.get("description", ""),
            "permissions": value.get("permissions", {}),
        }

    def request(self, method: str, path: str, body: object | None = None) -> Response:
        payload = dict(body) if isinstance(body, Mapping) else {}
        route = path.split("?", 1)[0]
        if route == "/ready":
            return Response(200, {"status": True})
        if route == "/api/v1/auths/signin":
            return Response(200, {"token": "t"})
        if route == "/api/v1/auths/api_key":
            return Response(200, {"api_key": "sk-x"})
        if route == "/api/v1/users/all":
            return Response(200, {"users": self.users, "total": len(self.users)})
        if route == "/api/v1/groups/" and method == "GET":
            return Response(200, [self._group(group) for group in self.groups])
        if route == "/api/v1/groups/create":
            group = {
                "id": f"g{self.next_group}",
                "name": payload.get("name", ""),
                "description": payload.get("description", ""),
                "permissions": payload.get("permissions", {}),
                "user_ids": [],
            }
            self.next_group += 1
            self.groups.append(group)
            return Response(200, self._group(group))
        if route.startswith("/api/v1/groups/id/"):
            parts = route.split("/")
            group_id = parts[5]
            group = next(group for group in self.groups if group["id"] == group_id)
            if parts[6] == "delete":
                self.groups.remove(group)
                return Response(200, True)
            if parts[6] == "update":
                group["name"] = payload.get("name", group["name"])
                group["permissions"] = payload.get("permissions", group["permissions"])
                return Response(200, self._group(group))
            if parts[6] == "users" and len(parts) == 7:
                return Response(
                    200,
                    [{"id": user_id} for user_id in group["user_ids"]],
                )
            if parts[6] == "users" and parts[7] in {"add", "remove"}:
                user_ids = payload.get("user_ids", [])
                assert isinstance(user_ids, list)
                current = group["user_ids"]
                assert isinstance(current, list)
                if parts[7] == "add":
                    current.extend(user_id for user_id in user_ids if user_id not in current)
                else:
                    current[:] = [user_id for user_id in current if user_id not in user_ids]
                return Response(200, self._group(group))
        if route == "/api/v1/auths/add":
            user = {
                "id": f"u{self.next_user}",
                "email": payload.get("email", ""),
                "name": payload.get("name", ""),
                "role": payload.get("role", "user"),
                "username": payload.get("name"),
            }
            self.next_user += 1
            self.users.append(user)
            return Response(200, user)
        if route.startswith("/api/v1/users/") and route.endswith("/update"):
            user_id = route.split("/")[4]
            user = next(user for user in self.users if user["id"] == user_id)
            user["role"] = payload.get("role", user["role"])
            return Response(200, user)
        if route == "/api/v1/functions/" and method == "GET":
            return Response(200, self.functions)
        if route.startswith("/api/v1/functions/id/") and method == "GET":
            parts = route.split("/")
            identifier = unquote(parts[5])
            function = next((item for item in self.functions if item.get("id") == identifier), None)
            if len(parts) == 7 and parts[6] == "valves":
                return Response(200, (function or {}).get("valves") or {})
            return Response(200, function)
        # `/list` returns presets while `/base` returns base rows, matching the
        # pinned frontend's two model listings (docs/research/owui-model-record.md §1.2–1.3, §8).
        if route == "/api/v1/models/base" and method == "GET":
            return Response(200, [model for model in self.models if model.get("base_model_id") is None])
        if route == "/api/v1/models/list" and method == "GET":
            presets = [model for model in self.models if model.get("base_model_id") is not None]
            page = int(path.partition("page=")[2] or 1)
            return Response(200, {"items": presets if page == 1 else [], "total": len(presets)})
        if route == "/api/v1/functions/sync":
            value = payload.get("functions", [])
            assert isinstance(value, list)
            self.functions = [dict(item) for item in value if isinstance(item, Mapping)]
            return Response(200, [])
        if route == "/api/v1/models/sync":
            value = payload.get("models", [])
            assert isinstance(value, list)
            self.models = [dict(item) for item in value if isinstance(item, Mapping)]
            return Response(200, [])
        if route == "/api/models":
            # The live listing bootstrap reads after the models sync: the
            # record under `info` without its params.
            return Response(
                200,
                {
                    "data": [
                        {"id": model["id"], "info": {key: value for key, value in model.items() if key != "params"}}
                        for model in self.models
                    ]
                },
            )
        raise AssertionError(f"unexpected frontend request: {method} {path}")


class FakeClient(owui.Client):
    def __init__(self, frontend: FakeFrontend, **credentials: str | None) -> None:
        super().__init__(
            "http://127.0.0.1",
            token=credentials.get("token"),
            api_key=credentials.get("api_key"),
        )
        self.frontend = frontend

    def request(self, method: str, path: str, body: object | None = None) -> Response:
        return self.frontend.request(method, path, body)


class FakeGrafanaClient(grafana.Client):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1")
        self.status = 200
        self.credential: tuple[str, str] | None = None
        self.calls: list[tuple[str, str]] = []

    def request(
        self,
        method: str,
        path: str,
        body: object | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> grafana.Response:
        del body, headers
        self.calls.append((method, path))
        return grafana.Response(self.status, {"database": "ok"})


class ApplyHost:
    """Commands map argv → outcome (or a queue); files is the rendered filesystem plus inputs."""

    def __init__(self, commands: Mapping[tuple[str, ...], Outcome], files: Mapping[str, str], *, euid: int = 0) -> None:
        self.commands: dict[tuple[str, ...], Outcome] = dict(commands)
        self.files = dict(files)
        self.euid = euid
        self.calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []
        self.inputs: list[tuple[tuple[str, ...], str | None]] = []
        self.writes: list[str] = []
        self.write_modes: dict[str, int] = {}
        self.mkdir_calls: list[tuple[str, int, bool, bool]] = []
        self.chmod_calls: list[tuple[str, int]] = []
        self.chown_calls: list[tuple[str, int, int]] = []
        self.pull_calls: list[tuple[SiteConfig, HardwareProfile, EgressAllowlist]] = []
        self.frontend = FakeFrontend()
        self.grafana = FakeGrafanaClient()

    def run(self, argv: Command, *, check: bool = False, input: str | None = None, cwd: PathLike | None = None, env: Mapping[str, str] | None = None, timeout: float | None = None, passthrough: bool = False) -> subprocess.CompletedProcess[str]:
        del check, cwd, timeout
        command = tuple(argv)
        self.calls.append((command, env))
        self.inputs.append((command, input))
        outcome = self.commands.get(command)
        if isinstance(outcome, list):
            return outcome.pop(0) if len(outcome) > 1 else outcome[0]
        if outcome is None:
            return subprocess.CompletedProcess(list(command), 127, "", "")
        return outcome

    def _is_dir(self, key: str) -> bool:
        return any(name.startswith(key.rstrip("/") + "/") for name in self.files)

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        key = os.fspath(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644) -> None:
        key = os.fspath(path)
        self.files[key] = text
        self.writes.append(key)
        self.write_modes[key] = mode

    def exists(self, path: PathLike) -> bool:
        key = os.fspath(path)
        return key in self.files or self._is_dir(key)

    def listdir(self, path: PathLike) -> list[str]:
        key = os.fspath(path).rstrip("/") + "/"
        if not self._is_dir(key):
            raise FileNotFoundError(key)
        return sorted({name[len(key):].split("/", 1)[0] for name in self.files if name.startswith(key)})

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        self.files.pop(os.fspath(path), None)

    def stat(self, path: PathLike) -> os.stat_result:
        key = os.fspath(path)
        if key in self.files:
            mode = stat_module.S_IFREG | 0o644
        elif self._is_dir(key):
            mode = stat_module.S_IFDIR | 0o755
        else:
            raise FileNotFoundError(key)
        return os.stat_result((mode, 0, 0, 1, 0, 0, 0, 0, 0, 0))

    def chmod(self, path: PathLike, mode: int) -> None:
        self.chmod_calls.append((os.fspath(path), mode))

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        self.chown_calls.append((os.fspath(path), uid, gid))

    def mkdir(self, path: PathLike, *, mode: int = 0o755, parents: bool = False, exist_ok: bool = False) -> None:
        self.mkdir_calls.append((os.fspath(path), mode, parents, exist_ok))

    def geteuid(self) -> int:
        return self.euid


def base_files(site: Path = EXAMPLE) -> dict[str, str]:
    files = {
        str(ROOT / "host.lock"): (ROOT / "host.lock").read_text(),
        str(ROOT / "images.lock"): (ROOT / "images.lock").read_text(),
        str(ROOT / "models.lock"): (ROOT / "models.lock").read_text(),
        str(ROOT / "config/egress.yaml"): (ROOT / "config/egress.yaml").read_text(),
        **{
            str(ROOT / "compose" / path): (ROOT / "compose" / path).read_text()
            for path in TEMPLATE_PATHS
        },
        str(ROOT / "migrations" / "0001_audit_log.sql"): (ROOT / "migrations" / "0001_audit_log.sql").read_text(),
        SITE: site.read_text(),
        CERT: "cert",
        "/etc/gideon/secrets/tls_key": "<never read>",
        "/etc/gideon/secrets/ldap_bind_password": "bind-password",
        "/etc/gideon/secrets/postgres_openwebui_password": "postgres-openwebui-password",
        "/etc/gideon/secrets/gideon_admin_password": "gideon-admin-password",
        "/etc/gideon/secrets/grafana_admin_password": "grafana-admin-password",
        "/etc/gideon/ca.pem": "ca",
    }
    site_result = load_site(site)
    assert site_result.config is not None
    if site_result.config.alerts.smtp.user:
        files["/etc/gideon/secrets/smtp_password"] = "smtp-password"
    return files


def healthy_commands(
    ref: str = PUBLIC_REF,
    *,
    insecure: bool = False,
    include_searxng: bool = True,
) -> dict[tuple[str, ...], Outcome]:
    smi = "GPU 0: X (UUID: GPU-aaaa)\nGPU 1: X (UUID: GPU-bbbb)\n"
    commands: dict[tuple[str, ...], Outcome] = {
        SMI: done(SMI, stdout=smi),
        SERVICE_GROUP: done(SERVICE_GROUP, stdout="gideon:x:4242:\n"),
        VERSION: done(VERSION, stdout="Docker Compose version v5.5.0\n"),
        PULL: done(PULL),
        RECREATE_CADDY: done(RECREATE_CADDY),
        RECREATE_POSTGRES: done(RECREATE_POSTGRES),
        RECREATE_OPEN_WEBUI: done(RECREATE_OPEN_WEBUI),
        STORE_UP: done(STORE_UP),
        UP: done(UP),
        PSQL_POSTGRES_QUERY: done(PSQL_POSTGRES_QUERY, stdout=""),
        PSQL_POSTGRES_STATEMENT: done(PSQL_POSTGRES_STATEMENT),
        PSQL_GIDEON_QUERY: done(PSQL_GIDEON_QUERY, stdout=""),
        PSQL_GIDEON_STATEMENT: done(PSQL_GIDEON_STATEMENT),
        PSQL_MIGRATION: done(PSQL_MIGRATION),
        SYSTEMCTL_RELOAD: done(SYSTEMCTL_RELOAD),
        SYSTEMCTL_LINK_SERVICE: done(SYSTEMCTL_LINK_SERVICE),
        SYSTEMCTL_ENABLE: done(SYSTEMCTL_ENABLE),
        SYSTEMCTL_ACTIVE: done(SYSTEMCTL_ACTIVE, stdout="active\n"),
        PG_UID: done(PG_UID, stdout="1234\n"),
        PG_GID: done(PG_GID, stdout="2345\n"),
        PG_STANZA_CREATE: done(PG_STANZA_CREATE),
        PG_CHECK: done(PG_CHECK),
        SYSTEMCTL_LINK_BACKUP: done(SYSTEMCTL_LINK_BACKUP),
        SYSTEMCTL_ENABLE_BACKUP: done(SYSTEMCTL_ENABLE_BACKUP),
        SYSTEMCTL_ACTIVE_BACKUP: done(SYSTEMCTL_ACTIVE_BACKUP, stdout="active\n"),
        SYSTEMCTL_LINK_DRILL: done(SYSTEMCTL_LINK_DRILL),
        SYSTEMCTL_ENABLE_DRILL: done(SYSTEMCTL_ENABLE_DRILL),
        SYSTEMCTL_ACTIVE_DRILL: done(SYSTEMCTL_ACTIVE_DRILL, stdout="active\n"),
        SYSTEMCTL_LINK_VERIFY: done(SYSTEMCTL_LINK_VERIFY),
        SYSTEMCTL_ENABLE_VERIFY: done(SYSTEMCTL_ENABLE_VERIFY),
        SYSTEMCTL_ACTIVE_VERIFY: done(SYSTEMCTL_ACTIVE_VERIFY, stdout="active\n"),
        PS: done(PS, stdout=running_rows(include_searxng=include_searxng)),
        **healthy_ingress(),
    }
    # The start stage recreates every changed owner outside the store tier on
    # a first apply: one answer per rendered service, derived like the rows.
    for service in _FIXTURE_SERVICES:
        recreate = tuple(compose_argv(RENDERED, "up", "-d", "--no-deps", "--force-recreate", service))
        commands.setdefault(recreate, done(recreate))
    # Every committed pin answers the registry and pull stages — derived from
    # the lock, so a new pin never moves this table.
    references = tuple(
        ref.replace("/caddy@", f"/{name}@").replace(CADDY_DIGEST, digest)
        for name, digest in _DIGESTS.items()
    )
    for image_reference in references:
        manifest = manifest_inspect(image_reference, insecure=insecure)
        commands[manifest] = done(manifest, stdout="{}")
        inspect = image_inspect(image_reference)
        commands[inspect] = done(inspect, stdout=json.dumps([image_reference]) + "\n")
    return commands


def apply(
    host: ApplyHost,
    pull_models: Callable[
        [Host, SiteConfig, HardwareProfile, EgressAllowlist], weights.PullOutcome
    ]
    | None = None,
    *,
    egress_path: PathLike | None = None,
    sleep_calls: list[float] | None = None,
    clock: Callable[[], float] | None = None,
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    naps: list[float] = []

    def fake_pull(
        _io: Host,
        site: SiteConfig,
        profile: HardwareProfile,
        allowlist: EgressAllowlist,
    ) -> weights.PullOutcome:
        host.pull_calls.append((site, profile, allowlist))
        models = tuple(
            weights.ModelOutcome(
                model.role,
                "present",
                tuple(
                    weights.FileOutcome(file.path, "verified", file.size)
                    for file in model.files
                ),
            )
            for model in profile.models
        )
        return weights.PullOutcome(models, (), True)

    def client_factory(**credentials: str | None) -> FakeClient:
        return FakeClient(host.frontend, **credentials)

    def grafana_client_factory(
        *, credential: tuple[str, str] | None = None
    ) -> FakeGrafanaClient:
        host.grafana.credential = credential
        return host.grafana

    def fake_sleep(seconds: float) -> None:
        naps.append(seconds)
        if sleep_calls is not None:
            sleep_calls.append(seconds)

    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = run_apply(
            argparse.Namespace(),
            host=host,
            root=ROOT,
            egress_path=egress_path,
            pull_models=pull_models or fake_pull,
            sleep=fake_sleep,
            clock=clock or (lambda: 0.0),
            client_factory=client_factory,
            grafana_client_factory=grafana_client_factory,
        )
    return code, out.getvalue(), err.getvalue()


def stages(out: str) -> list[str]:
    return [line.split(":")[0] for line in out.splitlines()]


def argv_calls(host: ApplyHost) -> list[tuple[str, ...]]:
    return [call[0] for call in host.calls]


class HappyPath(unittest.TestCase):
    def test_first_apply_runs_every_stage_and_records_the_applied_manifest(self) -> None:
        host = ApplyHost(healthy_commands(), base_files())
        code, out, err = apply(host)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(
            stages(out),
            [
                "preconditions",
                "secrets",
                "render",
                "registry",
                "pull",
                "models",
                "recreate",
                "stores",
                "start",
                "apply-manifest",
                "verify",
                "timers",
                "record",
            ],
        )
        self.assertIn("recreate: ok — recreated postgres: first apply", out)
        self.assertIn("engine_api_key", out)
        started = ", ".join(service for service in _FIXTURE_SERVICES if service != "postgres")
        self.assertIn(f"start: ok — recreated {started}: first apply", out)
        self.assertIn(RECREATE_CADDY, argv_calls(host))
        site_result = load_site(EXAMPLE)
        lock_result = load_models_lock(ROOT / "models.lock")
        allowlist_result = load_egress_allowlist(ROOT / "config/egress.yaml")
        assert site_result.config is not None
        assert lock_result.lock is not None
        assert allowlist_result.allowlist is not None
        expected_profile = lock_result.lock.profile(site_result.config.hardware_profile)
        assert expected_profile is not None
        self.assertEqual(host.pull_calls, [(site_result.config, expected_profile, allowlist_result.allowlist)])
        self.assertEqual(host.files[f"{RENDERED}/applied.yaml"], host.files[f"{RENDERED}/manifest.yaml"])
        self.assertLess(argv_calls(host).index(PSQL_MIGRATION), argv_calls(host).index(RECREATE_OPEN_WEBUI))
        self.assertEqual(host.writes[-1], f"{RENDERED}/applied.yaml")

    def test_second_apply_recreates_nothing(self) -> None:
        host = ApplyHost(healthy_commands(), base_files())
        apply(host)
        host.calls.clear()
        code, out, _ = apply(host)
        self.assertEqual(code, 0)
        self.assertIn("recreate: ok — no store-tier rendered file or compose block changed", out)
        self.assertIn("start: ok — no other rendered file or compose block changed", out)
        self.assertNotIn(RECREATE_CADDY, argv_calls(host))
        self.assertNotIn(RECREATE_POSTGRES, argv_calls(host))
        self.assertIn(UP, argv_calls(host))

    def test_standalone_render_between_applies_still_recreates_the_owner(self) -> None:
        host = ApplyHost(healthy_commands(), base_files())
        apply(host)
        host.files[SITE] = host.files[SITE].replace("gideon.example.org", "gideon2.example.org")
        handshake2 = tuple("gideon2.example.org" if part == "gideon.example.org" else part for part in HANDSHAKE)
        host.commands[handshake2] = host.commands[HANDSHAKE]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_render(argparse.Namespace(diff=False), host=host, root=ROOT), 0)
        host.calls.clear()
        code, out, _ = apply(host)
        self.assertEqual(code, 0, out)
        self.assertIn(
            "start: ok — recreated caddy, grafana, open-webui, blackbox-exporter: "
            "changed rendered files (caddy, blackbox-exporter); changed compose block "
            "(grafana, open-webui)",
            out,
        )

    def test_changed_store_compose_block_is_recreated_with_its_reason(self) -> None:
        host = ApplyHost(healthy_commands(), base_files())
        self.assertEqual(apply(host)[0], 0)
        applied = yaml.safe_load(host.files[f"{RENDERED}/applied.yaml"])
        applied["services"]["postgres"] = "0" * 64
        host.files[f"{RENDERED}/applied.yaml"] = yaml.safe_dump(
            applied, sort_keys=False
        )
        host.calls.clear()
        recreate_postgres = tuple(
            compose_argv(RENDERED, "up", "-d", "--no-deps", "--force-recreate", "postgres")
        )

        code, out, err = apply(host)

        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("recreate: ok — recreated postgres: changed compose block", out)
        self.assertIn(recreate_postgres, argv_calls(host))

    def test_changed_other_compose_block_is_recreated_in_start_with_its_reason(self) -> None:
        host = ApplyHost(healthy_commands(), base_files())
        self.assertEqual(apply(host)[0], 0)
        applied = yaml.safe_load(host.files[f"{RENDERED}/applied.yaml"])
        applied["services"]["grafana"] = "0" * 64
        host.files[f"{RENDERED}/applied.yaml"] = yaml.safe_dump(
            applied, sort_keys=False
        )
        host.calls.clear()
        recreate_grafana = tuple(
            compose_argv(RENDERED, "up", "-d", "--no-deps", "--force-recreate", "grafana")
        )

        code, out, err = apply(host)

        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("start: ok — recreated grafana: changed compose block", out)
        self.assertIn(recreate_grafana, argv_calls(host))

    def test_mapless_applied_manifest_recreates_every_service_as_blocks(self) -> None:
        host = ApplyHost(healthy_commands(), base_files())
        self.assertEqual(apply(host)[0], 0)
        applied = yaml.safe_load(host.files[f"{RENDERED}/applied.yaml"])
        del applied["services"]
        del applied["top_level"]
        host.files[f"{RENDERED}/applied.yaml"] = yaml.safe_dump(
            applied, sort_keys=False
        )
        host.calls.clear()
        started = ", ".join(
            service for service in _FIXTURE_SERVICES if service != "postgres"
        )

        code, out, err = apply(host)

        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("recreate: ok — recreated postgres: changed compose block", out)
        self.assertIn(f"start: ok — recreated {started}: changed compose block", out)
        for service in _FIXTURE_SERVICES:
            recreate = tuple(
                compose_argv(RENDERED, "up", "-d", "--no-deps", "--force-recreate", service)
            )
            self.assertIn(recreate, argv_calls(host))

    def test_mixed_recreate_reasons_are_grouped_in_fixed_order(self) -> None:
        host = ApplyHost(healthy_commands(), base_files())
        self.assertEqual(apply(host)[0], 0)
        applied = yaml.safe_load(host.files[f"{RENDERED}/applied.yaml"])
        applied["files"]["caddy/Caddyfile"]["sha256"] = "0" * 64
        applied["services"]["open-webui"] = "0" * 64
        applied["top_level"] = "0" * 64
        host.files[f"{RENDERED}/applied.yaml"] = yaml.safe_dump(
            applied, sort_keys=False
        )
        host.calls.clear()
        started_services = tuple(
            service for service in _FIXTURE_SERVICES if service != "postgres"
        )
        started = ", ".join(started_services)

        code, out, err = apply(host)

        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(
            f"start: ok — recreated {started}: changed rendered files (caddy); "
            f"changed compose block (open-webui); changed compose top-level ({started})",
            out,
        )

    def test_loopback_registry_uses_insecure_manifest_inspect_and_proxy_env(self) -> None:
        host = ApplyHost(
            healthy_commands(LOOPBACK_REF, insecure=True, include_searxng=False),
            base_files(SECOND),
        )
        handshake2 = tuple("gideon.exd.example.internal" if part == "gideon.example.org" else part for part in HANDSHAKE)
        host.commands[handshake2] = host.commands[HANDSHAKE]
        code, out, _ = apply(host)
        self.assertEqual(code, 0, out)
        inspect = manifest_inspect(LOOPBACK_REF, insecure=True)
        env = next(call[1] for call in host.calls if call[0] == inspect)
        assert env is not None
        self.assertEqual(env["HTTPS_PROXY"], "http://proxy.exd.example.internal:3128")
        self.assertIn("127.0.0.1", env["NO_PROXY"])

    def test_bridge_registry_uses_insecure_manifest_inspect(self) -> None:
        bridge_ref = LOOPBACK_REF.replace("127.0.0.1", "192.168.122.1")
        files = base_files(SECOND)
        files[SITE] = files[SITE].replace(
            "registry: 127.0.0.1:5000", "registry: 192.168.122.1:5000"
        )
        host = ApplyHost(
            healthy_commands(bridge_ref, insecure=True, include_searxng=False), files
        )
        handshake2 = tuple(
            "gideon.exd.example.internal"
            if part == "gideon.example.org"
            else part
            for part in HANDSHAKE
        )
        host.commands[handshake2] = host.commands[HANDSHAKE]
        code, out, _ = apply(host)
        self.assertEqual(code, 0, out)
        self.assertIn(manifest_inspect(bridge_ref, insecure=True), argv_calls(host))

    def test_hostname_registry_uses_tls_manifest_inspect(self) -> None:
        hostname_ref = LOOPBACK_REF.replace("127.0.0.1:5000", "registry.example:5000")
        files = base_files(SECOND)
        files[SITE] = files[SITE].replace(
            "registry: 127.0.0.1:5000", "registry: registry.example:5000"
        )
        host = ApplyHost(healthy_commands(hostname_ref, include_searxng=False), files)
        handshake2 = tuple(
            "gideon.exd.example.internal"
            if part == "gideon.example.org"
            else part
            for part in HANDSHAKE
        )
        host.commands[handshake2] = host.commands[HANDSHAKE]
        code, out, _ = apply(host)
        self.assertEqual(code, 0, out)
        self.assertIn(manifest_inspect(hostname_ref, insecure=False), argv_calls(host))


class Refusals(unittest.TestCase):
    def test_root_required(self) -> None:
        code, _, err = apply(ApplyHost(healthy_commands(), base_files(), euid=1000))
        self.assertEqual(code, 1)
        self.assertIn("Fix:", err)

    def test_missing_docker_compose_names_the_provision_step(self) -> None:
        commands = healthy_commands()
        del commands[VERSION]
        code, out, _ = apply(ApplyHost(commands, base_files()))
        self.assertEqual(code, 1)
        self.assertIn("preconditions: refuse", out)
        self.assertIn("docker-engine", out)

    def test_malformed_services_map_refuses_in_render_stage_with_repair_fix(self) -> None:
        host = ApplyHost(healthy_commands(), base_files())
        self.assertEqual(apply(host)[0], 0)
        applied = yaml.safe_load(host.files[f"{RENDERED}/applied.yaml"])
        applied["services"] = []
        host.files[f"{RENDERED}/applied.yaml"] = yaml.safe_dump(
            applied, sort_keys=False
        )

        code, out, err = apply(host)

        self.assertEqual((code, err), (1, ""))
        self.assertIn("render: refuse", out)
        self.assertIn("Repair the applied manifest, then re-run apply.", out)

    def test_missing_digest_in_registry_names_it_and_the_mirror_fix(self) -> None:
        references = (
            (PUBLIC_REF, CADDY_DIGEST),
            (PUBLIC_REF.replace("/caddy@", "/open-webui@").replace(CADDY_DIGEST, OPEN_WEBUI_DIGEST), OPEN_WEBUI_DIGEST),
            (PUBLIC_REF.replace("/caddy@", "/postgres@").replace(CADDY_DIGEST, POSTGRES_DIGEST), POSTGRES_DIGEST),
        )
        for image_reference, digest in references:
            with self.subTest(digest=digest):
                commands = healthy_commands()
                commands[manifest_inspect(image_reference, insecure=False)] = done(
                    manifest_inspect(image_reference, insecure=False),
                    1,
                    stderr="manifest unknown",
                )
                host = ApplyHost(commands, base_files())
                code, out, _ = apply(host)
                self.assertEqual(code, 1)
                self.assertIn(digest, out)
                self.assertIn("registry mirror", out)
                self.assertNotIn(PULL, argv_calls(host))

    def test_repo_digest_mismatch_refuses(self) -> None:
        commands = healthy_commands()
        commands[image_inspect(PUBLIC_REF)] = done(image_inspect(PUBLIC_REF), stdout=json.dumps(["ghcr.io/tnmd-fdo/caddy@sha256:" + "0" * 64]))
        host = ApplyHost(commands, base_files())
        code, out, _ = apply(host)
        self.assertEqual(code, 1)
        self.assertIn("pull: refuse", out)
        self.assertNotIn(RECREATE_CADDY, argv_calls(host))

    def test_missing_service_fails_verify_and_records_nothing(self) -> None:
        commands = healthy_commands()
        commands[PS] = done(PS, stdout="")
        host = ApplyHost(commands, base_files())
        code, out, _ = apply(host)
        self.assertEqual(code, 1)
        self.assertIn("stores: refuse", out)
        self.assertIn("missing service(s): postgres", out)
        self.assertNotIn(f"{RENDERED}/applied.yaml", host.files)

    def test_stopped_service_names_it_with_the_logs_fix(self) -> None:
        commands = healthy_commands()
        commands[PS] = done(PS, stdout=running_rows(states={"caddy": "exited"}))
        code, out, _ = apply(ApplyHost(commands, base_files()))
        self.assertEqual(code, 1)
        self.assertIn("caddy is not running", out)
        self.assertIn("logs caddy", out)

    def test_engine_health_polls_past_the_default_bound(self) -> None:
        commands = healthy_commands()
        starting = running_rows(states={ENGINE_SERVICE_NAME: "starting"})
        commands[PS] = [
            done(PS, stdout=running_rows()),
            *(done(PS, stdout=starting) for _ in range(32)),
            done(PS, stdout=running_rows()),
        ]
        host = ApplyHost(commands, base_files())
        sleeps: list[float] = []
        code, out, _ = apply(host, sleep_calls=sleeps)
        self.assertEqual(code, 0, out)
        self.assertGreater(sum(sleeps), _VERIFY_ATTEMPTS * _VERIFY_SLEEP_SECONDS)
        self.assertIn("engine healthy", out)

    def test_engine_failure_after_exhausted_budget_names_engine_logs(self) -> None:
        commands = healthy_commands()
        commands[PS] = [
            done(PS, stdout=running_rows()),
            done(PS, stdout=running_rows()),
            done(PS, stdout=running_rows(states={ENGINE_SERVICE_NAME: "starting"})),
        ]
        host = ApplyHost(commands, base_files())
        code, out, _ = apply(
            host,
            clock=iter((0.0, ENGINE_READY_SECONDS + 1.0)).__next__,
        )
        self.assertEqual(code, 1)
        self.assertIn("verify: refuse", out)
        self.assertIn(f"logs {ENGINE_SERVICE_NAME}", out)
        self.assertEqual(argv_calls(host).count(PS), 3)

    def test_engine_attempts_shrink_after_time_since_start(self) -> None:
        commands = healthy_commands()
        expected_attempts = max(
            1,
            math.floor(
                (ENGINE_READY_SECONDS - (950.0 - 100.0)) / _VERIFY_SLEEP_SECONDS
            ),
        )
        commands[PS] = [
            done(PS, stdout=running_rows()),
            done(PS, stdout=running_rows()),
            *(
                done(PS, stdout=running_rows(states={ENGINE_SERVICE_NAME: "starting"}))
                for _ in range(expected_attempts - 1)
            ),
            done(PS, stdout=running_rows()),
        ]
        host = ApplyHost(commands, base_files())
        code, out, _ = apply(
            host,
            clock=iter((100.0, 950.0, 962.0)).__next__,
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(argv_calls(host).count(PS), expected_attempts + 3)
        self.assertIn("engine healthy 862 s after start", out)

    def test_non_engine_failure_is_reported_before_engine_wait(self) -> None:
        commands = healthy_commands()
        commands[PS] = [
            done(PS, stdout=running_rows()),
            done(
                PS,
                stdout=running_rows(
                    omit=frozenset({"caddy"}),
                    states={ENGINE_SERVICE_NAME: "starting"},
                ),
            ),
        ]
        host = ApplyHost(commands, base_files())
        code, out, _ = apply(host)
        self.assertEqual(code, 1)
        self.assertIn("missing service(s): caddy", out)
        self.assertIn("logs caddy", out)
        self.assertEqual(argv_calls(host).count(PS), _VERIFY_ATTEMPTS + 1)

    def test_unexpected_service_fails_the_final_exact_read(self) -> None:
        rows = [json.loads(line) for line in running_rows().splitlines()]
        rows.append({"Service": "unexpected", "State": "running", "Health": ""})
        commands = healthy_commands()
        commands[PS] = [
            done(PS, stdout=running_rows()),
            done(PS, stdout=running_rows()),
            done(PS, stdout=running_rows()),
            done(PS, stdout=ps_rows(*rows)),
        ]
        host = ApplyHost(commands, base_files())
        code, out, _ = apply(host)
        self.assertEqual(code, 1)
        self.assertIn("unexpected service(s): unexpected", out)
        self.assertIn("logs unexpected", out)

    def test_slow_start_is_polled_then_passes(self) -> None:
        commands = healthy_commands()
        commands[PS] = [
            done(PS, stdout=""),
            done(PS, stdout=running_rows()),
        ]
        code, out, _ = apply(ApplyHost(commands, base_files()))
        self.assertEqual(code, 0, out)

    def test_ingress_probe_failure_fails_verify(self) -> None:
        commands = healthy_commands()
        commands[PLACED_FP] = done(PLACED_FP, stdout="sha256 Fingerprint=00:00\n")
        code, out, _ = apply(ApplyHost(commands, base_files()))
        self.assertEqual(code, 1)
        self.assertIn("verify: refuse", out)
        self.assertIn("does not match", out)

    def test_corrupt_applied_manifest_refuses(self) -> None:
        files = base_files()
        files[f"{RENDERED}/applied.yaml"] = "files: [nope]\n"
        code, out, _ = apply(ApplyHost(healthy_commands(), files))
        self.assertEqual(code, 1)
        self.assertIn("render: refuse", out)

    def test_foreign_file_refuses_before_any_docker_call(self) -> None:
        files = base_files()
        files[f"{RENDERED}/notes.txt"] = "hand-written\n"
        host = ApplyHost(healthy_commands(), files)
        code, _out, err = apply(host)
        self.assertEqual(code, 1)
        self.assertIn("notes.txt", err)
        self.assertNotIn(PULL, argv_calls(host))


class ModelsStage(unittest.TestCase):
    def test_present_and_fetched_details(self) -> None:
        host = ApplyHost(healthy_commands(), base_files())
        code, out, _ = apply(host)
        self.assertEqual(code, 0, out)
        profile = host.pull_calls[0][1]
        self.assertIn(f"models: ok — {len(profile.models)} model(s) present", out)

        def fetched(
            _io: Host,
            site: SiteConfig,
            received_profile: HardwareProfile,
            allowlist: EgressAllowlist,
        ) -> weights.PullOutcome:
            host.pull_calls.append((site, received_profile, allowlist))
            outcomes = tuple(
                weights.ModelOutcome(model.role, "fetched", ())
                for model in received_profile.models
            )
            return weights.PullOutcome(outcomes, (), True)

        host.calls.clear()
        code, out, _ = apply(host, fetched)
        self.assertEqual(code, 0, out)
        self.assertIn(
            f"models: ok — {len(profile.models)} model(s) fetched, 0 present", out
        )

    def test_failed_pull_stops_before_recreate(self) -> None:
        host = ApplyHost(healthy_commands(), base_files())
        problem = "the model could not be verified"
        fix = "Republish the source, then re-pin models.lock."

        def failed(
            _io: Host,
            _site: SiteConfig,
            profile: HardwareProfile,
            _allowlist: EgressAllowlist,
        ) -> weights.PullOutcome:
            model = profile.models[0]
            return weights.PullOutcome(
                (
                    weights.ModelOutcome(
                        model.role, "failed", (), problem=problem, fix=fix
                    ),
                ),
                (),
                False,
                problem,
                fix,
            )

        code, out, _ = apply(host, failed)
        self.assertEqual(code, 1)
        self.assertIn(f"models: refuse — {problem} Fix: {fix}", out)
        self.assertNotIn(RECREATE_POSTGRES, argv_calls(host))
        self.assertNotIn(RECREATE_CADDY, argv_calls(host))

    def test_a_pull_exception_is_one_refuse_row(self) -> None:
        host = ApplyHost(healthy_commands(), base_files())

        def raising(
            _io: Host, _site: SiteConfig, _profile: HardwareProfile, _allowlist: EgressAllowlist
        ) -> weights.PullOutcome:
            raise OSError(30, "Read-only file system", "/data/models/gideon")

        code, out, err = apply(host, raising)
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertIn("models: refuse — weights pull failed: OSError:", out)
        self.assertNotIn(RECREATE_POSTGRES, argv_calls(host))

    def test_unloadable_egress_fails_before_pull(self) -> None:
        files = base_files()
        bad_path = "/fixture/egress.yaml"
        files[bad_path] = "version: nope\n"
        host = ApplyHost(healthy_commands(), files)
        code, out, _ = apply(host, egress_path=bad_path)
        self.assertEqual(code, 1)
        self.assertIn("models: refuse", out)
        self.assertIn("config/egress.yaml", out)
        self.assertEqual(host.pull_calls, [])
        self.assertNotIn(RECREATE_POSTGRES, argv_calls(host))


class ServedFingerprint(unittest.TestCase):
    def test_probe_commands_are_part_of_verify(self) -> None:
        host = ApplyHost(healthy_commands(), base_files())
        apply(host)
        self.assertIn(SERVED_FP, argv_calls(host))


class NoGpuModeSwitch(unittest.TestCase):
    def test_no_gpu_verify_keeps_the_single_default_wait(self) -> None:
        host = ApplyHost(healthy_commands(), base_files())
        host.files[os.fspath(nogpu.NO_GPU_PATH)] = "declared\n"
        host.commands[PS] = done(
            PS, stdout=running_rows(include_dcgm=False, include_engine=False)
        )
        sleeps: list[float] = []
        code, out, _ = apply(host, sleep_calls=sleeps)
        self.assertEqual(code, 0, out)
        self.assertEqual(argv_calls(host).count(PS), 2)
        self.assertNotIn("engine healthy", out)
        self.assertLessEqual(len(sleeps), _VERIFY_ATTEMPTS - 1)

    def test_switching_mode_removes_and_restores_gpu_rendered_files(self) -> None:
        host = ApplyHost(healthy_commands(), base_files())

        code, out, _ = apply(host)
        self.assertEqual(code, 0, out)
        host.files[os.fspath(nogpu.NO_GPU_PATH)] = "declared\n"
        host.commands[PS] = done(
            PS, stdout=running_rows(include_dcgm=False, include_engine=False)
        )
        host.calls.clear()

        code, out, _ = apply(host)
        self.assertEqual(code, 0, out)
        self.assertNotIn(f"{RENDERED}/grafana/dashboards/gpu.json", host.files)
        self.assertNotIn("dcgm-exporter", yaml.safe_load(host.files[f"{RENDERED}/compose.yaml"])["services"])
        self.assertNotIn("dcgm-exporter:9400", host.files[f"{RENDERED}/prometheus/prometheus.yml"])
        # The registry and pull stages judge the rendered stack's images: the
        # exporter's pin is neither probed nor verified on a no-GPU host, since
        # compose pull never fetches it (the acceptance VM's first install).
        dcgm_ref = PUBLIC_REF.replace("/caddy@", "/dcgm-exporter@").replace(CADDY_DIGEST, _DIGESTS["dcgm-exporter"])
        self.assertNotIn(manifest_inspect(dcgm_ref, insecure=False), argv_calls(host))
        self.assertNotIn(image_inspect(dcgm_ref), argv_calls(host))

        del host.files[os.fspath(nogpu.NO_GPU_PATH)]
        host.commands[PS] = done(PS, stdout=running_rows())
        host.calls.clear()

        code, out, _ = apply(host)
        self.assertEqual(code, 0, out)
        self.assertIn(f"{RENDERED}/grafana/dashboards/gpu.json", host.files)
        self.assertIn("dcgm-exporter", yaml.safe_load(host.files[f"{RENDERED}/compose.yaml"])["services"])
        self.assertIn("dcgm-exporter:9400", host.files[f"{RENDERED}/prometheus/prometheus.yml"])
        self.assertIn(manifest_inspect(dcgm_ref, insecure=False), argv_calls(host))
        self.assertIn(image_inspect(dcgm_ref), argv_calls(host))


class NewStages(unittest.TestCase):
    """The stages this release adds: secrets, stores, start, apply-manifest, timers."""

    def test_stores_stage_prepares_and_checks_the_pgbackrest_stanza(self) -> None:
        host = ApplyHost(healthy_commands(), base_files())
        code, out, _ = apply(host)
        self.assertEqual(code, 0, out)
        calls = argv_calls(host)
        self.assertIn(PG_UID, calls)
        self.assertIn(PG_GID, calls)
        self.assertIn(PG_STANZA_CREATE, calls)
        self.assertIn(PG_CHECK, calls)
        self.assertLess(calls.index(PG_UID), calls.index(PG_STANZA_CREATE))
        self.assertLess(calls.index(PG_STANZA_CREATE), calls.index(PG_CHECK))
        self.assertIn("pgBackRest stanza gideon current; archiving verified", out)
        # Generated secrets are chowned to the service group as they are written;
        # the repository is the stores stage's one chown.
        self.assertEqual(
            [call for call in host.chown_calls if not call[0].startswith("/etc/gideon/secrets/")],
            [("/data/backup-staging/pgbackrest", 1234, 2345)],
        )

    def test_stanza_mismatch_refuses_before_the_frontend_starts(self) -> None:
        commands = healthy_commands()
        commands[PG_STANZA_CREATE] = done(
            PG_STANZA_CREATE,
            1,
            stderr="ERROR: archive info files do not match the database",
        )
        host = ApplyHost(commands, base_files())
        code, out, _ = apply(host)
        self.assertEqual(code, 1)
        self.assertIn("stores: refuse", out)
        self.assertIn("sudo python3 -m gideon restore --from staging", out)
        self.assertIn("move /data/backup-staging/pgbackrest aside", out)
        self.assertNotIn(RECREATE_OPEN_WEBUI, argv_calls(host))

    def test_break_glass_password_prints_once_on_the_run_that_generates_it(self) -> None:
        files = base_files()
        del files["/etc/gideon/secrets/gideon_admin_password"]
        del files["/etc/gideon/secrets/grafana_admin_password"]
        host = ApplyHost(healthy_commands(), files)
        code, out, _ = apply(host)
        self.assertEqual(code, 0, out)
        lines = [line for line in out.splitlines() if "into the office password manager now" in line]
        self.assertEqual(len(lines), 2)
        written = host.files["/etc/gideon/secrets/gideon_admin_password"].strip()
        admin_lines = [PRINT_ONCE_LINE.fullmatch(line) for line in lines]
        self.assertTrue(all(match is not None for match in admin_lines))
        admin_match = next(
            match for match in admin_lines if match is not None and "gideon-admin" in match.string
        )
        self.assertEqual(admin_match.group("value"), written)
        grafana_written = host.files["/etc/gideon/secrets/grafana_admin_password"].strip()
        grafana_match = next(
            match for match in admin_lines if match is not None and GRAFANA_ADMIN_USER in match.string
        )
        self.assertEqual(grafana_match.group("value"), grafana_written)
        self.assertEqual(host.write_modes["/etc/gideon/secrets/gideon_admin_password"], 0o440)
        self.assertIn("secrets: ok — created generated secrets:", out)
        code, out, _ = apply(host)
        self.assertEqual(code, 0, out)
        self.assertNotIn("password manager", out)
        self.assertIn("secrets: ok — no generated secret was missing", out)

    def test_migration_failure_refuses_in_stores_before_the_frontend_starts(self) -> None:
        commands = healthy_commands()
        commands[PSQL_MIGRATION] = done(PSQL_MIGRATION, 1, stderr="ERROR: syntax error")
        host = ApplyHost(commands, base_files())
        code, out, _ = apply(host)
        self.assertEqual(code, 1)
        self.assertIn("stores: refuse", out)
        self.assertIn("0001_audit_log.sql", out)
        self.assertNotIn(RECREATE_OPEN_WEBUI, argv_calls(host))
        self.assertNotIn(UP, argv_calls(host))
        self.assertNotIn(f"{RENDERED}/applied.yaml", host.files)

    def test_frontend_refusal_stops_before_verify_and_records_nothing(self) -> None:
        host = ApplyHost(healthy_commands(), base_files())
        original = host.frontend.request

        def failing(method: str, path: str, body: object | None = None) -> Response:
            if path == "/api/v1/groups/create":
                return Response(500, {"detail": "boom"})
            return original(method, path, body)

        host.frontend.request = failing  # type: ignore[method-assign]
        code, out, _ = apply(host)
        self.assertEqual(code, 1)
        self.assertIn("apply-manifest: refuse", out)
        self.assertIn("HTTP 500", out)
        self.assertIn("logs open-webui", out)
        self.assertNotIn(HANDSHAKE, argv_calls(host))
        self.assertNotIn(f"{RENDERED}/applied.yaml", host.files)
        self.assertNotIn("sk-", out)

    def test_manifest_stage_reports_removals_and_minting(self) -> None:
        host = ApplyHost(healthy_commands(), base_files())
        host.frontend.functions.append({"id": "hand_added"})
        code, out, _ = apply(host)
        self.assertEqual(code, 0, out)
        self.assertIn("removed functions: hand_added", out)
        self.assertIn("minted secrets: gideon_admin_api_key, gideon_eval_api_key", out)
        self.assertEqual(host.write_modes["/etc/gideon/secrets/gideon_admin_api_key"], 0o440)
        self.assertEqual(
            [item["id"] for item in host.frontend.functions],
            [ARITHMETIC_GUARDRAIL_ID, CITATION_STAMP_ID],
        )
        code, out, _ = apply(host)
        self.assertIn("apply-manifest: ok — frontend state matches the manifest", out)

    def test_timer_that_will_not_activate_refuses_with_journalctl(self) -> None:
        commands = healthy_commands()
        commands[SYSTEMCTL_ACTIVE] = done(SYSTEMCTL_ACTIVE, 3, stdout="inactive\n")
        host = ApplyHost(commands, base_files())
        code, out, _ = apply(host)
        self.assertEqual(code, 1)
        self.assertIn("timers: refuse", out)
        self.assertIn("journalctl -u gideon-users-reconcile.timer", out)
        self.assertNotIn(f"{RENDERED}/applied.yaml", host.files)
        calls = argv_calls(host)
        self.assertLess(calls.index(SYSTEMCTL_RELOAD), calls.index(SYSTEMCTL_LINK_SERVICE))
        self.assertLess(calls.index(SYSTEMCTL_LINK_SERVICE), calls.index(SYSTEMCTL_ENABLE))

    def test_verify_requires_the_frontend_to_report_ready(self) -> None:
        host = ApplyHost(healthy_commands(), base_files())
        original = host.frontend.request
        seen: list[str] = []

        def flaky_ready(method: str, path: str, body: object | None = None) -> Response:
            if path == "/ready":
                seen.append(path)
                # Ready during the manifest stage's wait, then not during verify.
                return Response(200, {"status": len(seen) == 1})
            return original(method, path, body)

        host.frontend.request = flaky_ready  # type: ignore[method-assign]
        code, out, _ = apply(host)
        self.assertEqual(code, 1)
        self.assertIn("verify: refuse", out)
        self.assertIn("/ready did not report status true", out)
        self.assertNotIn(f"{RENDERED}/applied.yaml", host.files)

    def test_verify_checks_grafana_after_the_frontend_and_names_its_logs_fix(self) -> None:
        host = ApplyHost(healthy_commands(), base_files())
        host.grafana.status = 503
        code, out, _ = apply(host)
        self.assertEqual(code, 1)
        self.assertIn("verify: refuse", out)
        self.assertIn("Grafana health endpoint returned HTTP 503", out)
        self.assertIn("logs grafana", out)
        self.assertEqual(host.grafana.calls[-1], ("GET", "/api/health"))
        # The health check carries no credential: the break-glass password is
        # read by `alerts test`, never by apply's verify.
        self.assertIsNone(host.grafana.credential)

    def test_stores_stage_waits_for_a_healthy_store_tier_only(self) -> None:
        commands = healthy_commands()
        commands[PS] = [
            done(PS, stdout=ps_rows({"Service": "postgres", "State": "running", "Health": "starting"})),
            done(PS, stdout=ps_rows({"Service": "postgres", "State": "running", "Health": "healthy"})),
            done(PS, stdout=running_rows()),
        ]
        host = ApplyHost(commands, base_files())
        code, out, _ = apply(host)
        self.assertEqual(code, 0, out)
        self.assertIn("stores: ok — created roles: openwebui, gideon, gideon_audit, gideon_ro_metrics; created databases: openwebui, gideon; applied migrations: 0001_audit_log", out)
