"""Secret rotation behavior over an independent fake host and Compose stack."""

import argparse
import contextlib
import io
import json
import os
import stat as stat_module
import subprocess
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from gideon.host import grafana, nogpu, owui, pgbackrest, rotate, weights
from gideon.host.apply import run_apply
from gideon.host.egress import EgressAllowlist
from gideon.host.images import load_image_lock
from gideon.host.models import HardwareProfile
from gideon.host.owui import Response
from gideon.host.render import ARTIFACTS
from gideon.host.render.api import API_SERVICE_NAME
from gideon.host.render.command import run_render
from gideon.host.render.engine import ENGINE_SERVICE_NAME
from gideon.host.secrets import SECRET_REGISTRY, SECRETS_DIR
from gideon.host.site import SiteConfig, load_site
from gideon.host.stack import compose_argv, exec_argv
from gideon.host.sysio import Command, Host, PathLike
from gideon.host.tls import CA_PATH, CERT_PATH

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config/site.example.yaml"
SECOND = ROOT / "tests/fixtures/site/second-office.yaml"
TEMPLATE_PATHS = tuple(
    dict.fromkeys(path for artifact in ARTIFACTS for path in artifact.template_paths)
)
SITE = "/etc/gideon/site.yaml"
RENDERED = "/etc/gideon/rendered"
_COMMITTED_LOCK = load_image_lock(ROOT / "images.lock").lock
assert _COMMITTED_LOCK is not None
_DIGESTS = {pin.name: pin.digest for pin in _COMMITTED_LOCK.images}
PUBLIC_REF = f"ghcr.io/tnmd-fdo/caddy@{_DIGESTS['caddy']}"
LOOPBACK_REF = f"127.0.0.1:5000/caddy@{_DIGESTS['caddy']}"
SMI = ("nvidia-smi", "-L")
SERVICE_GROUP = ("getent", "group", "gideon")
VERSION = ("docker", "compose", "version")
PULL = tuple(compose_argv(RENDERED, "pull"))
STORE_UP = tuple(compose_argv(RENDERED, "up", "-d", "postgres"))
UP = tuple(compose_argv(RENDERED, "up", "-d", "--remove-orphans"))
PS = tuple(compose_argv(RENDERED, "ps", "--all", "--format", "json"))
PSQL_POSTGRES_QUERY = tuple(
    exec_argv(RENDERED, "postgres", "psql", "-U", "postgres", "-d", "postgres", "-tA", "-f", "-")
)
PSQL_POSTGRES_STATEMENT = tuple(
    exec_argv(
        RENDERED,
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-d",
        "postgres",
        "-v",
        "ON_ERROR_STOP=1",
        "-f",
        "-",
    )
)
PSQL_GIDEON_QUERY = tuple(
    exec_argv(RENDERED, "postgres", "psql", "-U", "gideon", "-d", "gideon", "-tA", "-f", "-")
)
PSQL_GIDEON_STATEMENT = tuple(
    exec_argv(
        RENDERED,
        "postgres",
        "psql",
        "-U",
        "gideon",
        "-d",
        "gideon",
        "-v",
        "ON_ERROR_STOP=1",
        "-f",
        "-",
    )
)
PSQL_MIGRATION = tuple(
    exec_argv(
        RENDERED,
        "postgres",
        "psql",
        "-U",
        "gideon",
        "-d",
        "gideon",
        "-v",
        "ON_ERROR_STOP=1",
        "--single-transaction",
        "-f",
        "-",
    )
)
PG_UID = tuple(exec_argv(RENDERED, "postgres", "id", "-u", "postgres"))
PG_GID = tuple(exec_argv(RENDERED, "postgres", "id", "-g", "postgres"))
PG_STANZA_CREATE = tuple(pgbackrest.exec_argv(RENDERED, "stanza-create"))
PG_CHECK = tuple(pgbackrest.exec_argv(RENDERED, "check"))
SYSTEMCTL_RELOAD = ("systemctl", "daemon-reload")
SYSTEMCTL_LINK_SERVICE = (
    "systemctl",
    "link",
    f"{RENDERED}/systemd/gideon-users-reconcile.service",
)
SYSTEMCTL_ENABLE = (
    "systemctl",
    "enable",
    "--now",
    f"{RENDERED}/systemd/gideon-users-reconcile.timer",
)
SYSTEMCTL_ACTIVE = ("systemctl", "is-active", "gideon-users-reconcile.timer")
SYSTEMCTL_LINK_BACKUP = (
    "systemctl",
    "link",
    f"{RENDERED}/systemd/gideon-backup.service",
)
SYSTEMCTL_ENABLE_BACKUP = (
    "systemctl",
    "enable",
    "--now",
    f"{RENDERED}/systemd/gideon-backup.timer",
)
SYSTEMCTL_ACTIVE_BACKUP = ("systemctl", "is-active", "gideon-backup.timer")
SYSTEMCTL_LINK_DRILL = (
    "systemctl",
    "link",
    f"{RENDERED}/systemd/gideon-backup-drill.service",
)
SYSTEMCTL_ENABLE_DRILL = (
    "systemctl",
    "enable",
    "--now",
    f"{RENDERED}/systemd/gideon-backup-drill.timer",
)
SYSTEMCTL_ACTIVE_DRILL = ("systemctl", "is-active", "gideon-backup-drill.timer")
SYSTEMCTL_LINK_VERIFY = (
    "systemctl",
    "link",
    f"{RENDERED}/systemd/gideon-backup-verify.service",
)
SYSTEMCTL_ENABLE_VERIFY = (
    "systemctl",
    "enable",
    "--now",
    f"{RENDERED}/systemd/gideon-backup-verify.timer",
)
SYSTEMCTL_ACTIVE_VERIFY = ("systemctl", "is-active", "gideon-backup-verify.timer")
CERT = CERT_PATH
HANDSHAKE = (
    "openssl",
    "s_client",
    "-connect",
    "127.0.0.1:443",
    "-servername",
    "gideon.example.org",
    "-verify_hostname",
    "gideon.example.org",
    "-CAfile",
    CA_PATH,
    "-verify_return_error",
)
SERVED_FP = ("openssl", "x509", "-noout", "-fingerprint", "-sha256")
PLACED_FP = ("openssl", "x509", "-in", CERT, "-noout", "-fingerprint", "-sha256")
LEAF = "-----BEGIN CERTIFICATE-----\nMIIC\n-----END CERTIFICATE-----\n"
FINGERPRINT = "sha256 Fingerprint=81:2C:7A:46:FB:2D:27:AA\n"

Outcome = subprocess.CompletedProcess[str] | list[subprocess.CompletedProcess[str]]


def done(
    argv: tuple[str, ...],
    rc: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), rc, stdout, stderr)


def healthy_ingress() -> dict[tuple[str, ...], Outcome]:
    return {
        HANDSHAKE: done(HANDSHAKE, stdout=f"CONNECTED\n{LEAF}---\nVerify return code: 0 (ok)\n"),
        SERVED_FP: done(SERVED_FP, stdout=FINGERPRINT),
        PLACED_FP: done(PLACED_FP, stdout=FINGERPRINT),
    }


def manifest_inspect(ref: str, *, insecure: bool) -> tuple[str, ...]:
    return ("docker", "manifest", "inspect", *(('--insecure',) if insecure else ()), ref)


def image_inspect(ref: str) -> tuple[str, ...]:
    return ("docker", "image", "inspect", "--format", "{{json .RepoDigests}}", ref)


def ps_rows(*rows: Mapping[str, str]) -> str:
    return "\n".join(json.dumps(row) for row in rows) + "\n"


_FIXTURE_SERVICES: tuple[str, ...] = tuple(
    yaml.safe_load((ROOT / "tests/fixtures/render/example/compose.yaml").read_text())["services"]
)
_HEALTHCHECKED = frozenset({"postgres", "open-webui", ENGINE_SERVICE_NAME, API_SERVICE_NAME})


def running_rows(
    *,
    include_dcgm: bool = True,
    include_engine: bool = True,
    include_api: bool = True,
    include_searxng: bool = True,
) -> str:
    services = tuple(
        service
        for service in _FIXTURE_SERVICES
        if (include_dcgm or service != "dcgm-exporter")
        and (include_engine or service != ENGINE_SERVICE_NAME)
        and (include_api or service != API_SERVICE_NAME)
        and (include_searxng or service != "searxng")
    )
    return ps_rows(
        *(
            {
                "Service": service,
                "State": "running",
                "Health": "healthy" if service in _HEALTHCHECKED else "",
            }
            for service in services
        )
    )


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
                return Response(200, [{"id": user_id} for user_id in group["user_ids"]])
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
            identifier = parts[5]
            function = next((item for item in self.functions if item.get("id") == identifier), None)
            if len(parts) == 7 and parts[6] == "valves":
                return Response(200, (function or {}).get("valves") or {})
            return Response(200, function)
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
            return Response(
                200,
                {
                    "data": [
                        {
                            "id": model["id"],
                            "info": {key: value for key, value in model.items() if key != "params"},
                        }
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

    def request(
        self,
        method: str,
        path: str,
        body: object | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> grafana.Response:
        del method, path, body, headers
        return grafana.Response(200, {"database": "ok"})


class ApplyHost:
    """A complete recording host for the apply and rotate stage sequences."""

    def __init__(
        self,
        commands: Mapping[tuple[str, ...], Outcome],
        files: Mapping[str, str],
        *,
        euid: int = 0,
    ) -> None:
        self.commands: dict[tuple[str, ...], Outcome] = dict(commands)
        self.files = dict(files)
        self.euid = euid
        self.calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []
        self.inputs: list[tuple[tuple[str, ...], str | None]] = []
        self.writes: list[str] = []
        self.write_modes: dict[str, int] = {}
        self.chown_calls: list[tuple[str, int, int]] = []
        self.chown_failure = False
        self.frontend = FakeFrontend()
        self.grafana = FakeGrafanaClient()

    def run(
        self,
        argv: Command,
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, cwd, timeout, passthrough
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
        del encoding
        key = os.fspath(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(
        self,
        path: PathLike,
        text: str,
        *,
        encoding: str = "utf-8",
        mode: int = 0o644,
    ) -> None:
        del encoding
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
        return sorted({name[len(key) :].split("/", 1)[0] for name in self.files if name.startswith(key)})

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        key = os.fspath(path)
        if key not in self.files and not missing_ok:
            raise FileNotFoundError(key)
        self.files.pop(key, None)

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
        del path, mode

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        if self.chown_failure:
            raise PermissionError(os.fspath(path))
        self.chown_calls.append((os.fspath(path), uid, gid))

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del path, mode, parents, exist_ok

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
        str(ROOT / "migrations" / "0001_audit_log.sql"): (
            ROOT / "migrations" / "0001_audit_log.sql"
        ).read_text(),
        SITE: site.read_text(),
        str(ROOT / "gideon/api/__init__.py"): (ROOT / "gideon/api/__init__.py").read_text(),
        str(ROOT / "gideon/guardrail.py"): (ROOT / "gideon/guardrail.py").read_text(),
        CERT: "cert",
        "/etc/gideon/ca.pem": "ca",
    }
    for secret in SECRET_REGISTRY:
        if secret.kind == "password":
            files[str(SECRETS_DIR / secret.name)] = f"fixture-{secret.name}\n"
    files["/etc/gideon/secrets/ldap_bind_password"] = "fixture-ldap-bind\n"
    site_result = load_site(site)
    assert site_result.config is not None
    if site_result.config.alerts.smtp.user:
        files["/etc/gideon/secrets/smtp_password"] = "fixture-smtp\n"
    return files


def healthy_commands(
    ref: str = PUBLIC_REF,
    *,
    insecure: bool = False,
    include_searxng: bool = True,
) -> dict[tuple[str, ...], Outcome]:
    commands: dict[tuple[str, ...], Outcome] = {
        SMI: done(SMI, stdout="GPU 0: X (UUID: GPU-aaaa)\nGPU 1: X (UUID: GPU-bbbb)\n"),
        SERVICE_GROUP: done(SERVICE_GROUP, stdout="gideon:x:4242:\n"),
        VERSION: done(VERSION, stdout="Docker Compose version v5.5.0\n"),
        PULL: done(PULL),
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
        PS: done(
            PS,
            stdout=running_rows(
                include_searxng=include_searxng,
                include_engine=True,
                include_dcgm=True,
            ),
        ),
        **healthy_ingress(),
    }
    for service in _FIXTURE_SERVICES:
        command = tuple(
            compose_argv(RENDERED, "up", "-d", "--no-deps", "--force-recreate", service)
        )
        commands.setdefault(command, done(command))
    references = tuple(
        ref.replace("/caddy@", f"/{name}@").replace(_DIGESTS["caddy"], digest)
        for name, digest in _DIGESTS.items()
    )
    for image_reference in references:
        manifest = manifest_inspect(image_reference, insecure=insecure)
        commands[manifest] = done(manifest, stdout="{}")
        inspect = image_inspect(image_reference)
        commands[inspect] = done(inspect, stdout=json.dumps([image_reference]) + "\n")
    return commands


def fake_pull(
    _io: Host,
    _site: SiteConfig,
    profile: HardwareProfile,
    _allowlist: EgressAllowlist,
) -> weights.PullOutcome:
    outcomes = tuple(
        weights.ModelOutcome(
            model.role,
            "present",
            tuple(weights.FileOutcome(file.path, "verified", file.size) for file in model.files),
        )
        for model in profile.models
    )
    return weights.PullOutcome(outcomes, (), True)


def client_factory(host: ApplyHost) -> Callable[..., owui.Client]:
    def make(**credentials: str | None) -> FakeClient:
        return FakeClient(host.frontend, **credentials)

    return make


def grafana_client_factory(host: ApplyHost) -> Callable[..., grafana.Client]:
    def make(*, credential: tuple[str, str] | None = None) -> FakeGrafanaClient:
        del credential
        return host.grafana

    return make


def run_apply_once(host: ApplyHost) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = run_apply(
            argparse.Namespace(),
            host=host,
            root=ROOT,
            pull_models=fake_pull,
            sleep=lambda _seconds: None,
            clock=lambda: 0.0,
            client_factory=client_factory(host),
            grafana_client_factory=grafana_client_factory(host),
        )
    return code, out.getvalue(), err.getvalue()


def run_rotate(host: ApplyHost, name: str) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = rotate.run_secrets_rotate(
            argparse.Namespace(name=name),
            host=host,
            root=ROOT,
            pull_models=fake_pull,
            sleep=lambda _seconds: None,
            clock=lambda: 0.0,
            client_factory=client_factory(host),
            grafana_client_factory=grafana_client_factory(host),
        )
    return code, out.getvalue(), err.getvalue()


def argv_calls(host: ApplyHost) -> list[tuple[str, ...]]:
    return [call[0] for call in host.calls]


def stages(output: str) -> list[str]:
    return [line.split(":", 1)[0] for line in output.splitlines()]


def force_recreate(service: str) -> tuple[str, ...]:
    return tuple(compose_argv(RENDERED, "up", "-d", "--no-deps", "--force-recreate", service))


def assert_no_secret_text(test: unittest.TestCase, output: str, extra: tuple[str, ...] = ()) -> None:
    values = tuple(
        f"fixture-{secret.name}"
        for secret in SECRET_REGISTRY
        if secret.kind == "password"
    ) + ("fixture-ldap-bind", "fixture-smtp") + extra
    for value in values:
        test.assertNotIn(value, output)


class RealStack(unittest.TestCase):
    def new_host(self, site: Path = EXAMPLE, *, include_searxng: bool = True) -> ApplyHost:
        site_result = load_site(site)
        assert site_result.config is not None
        loopback = site_result.config.registry.startswith("127.0.0.1:")
        host = ApplyHost(
            healthy_commands(
                LOOPBACK_REF if loopback else PUBLIC_REF,
                insecure=loopback,
                include_searxng=include_searxng,
            ),
            base_files(site),
        )
        if loopback:
            second_handshake = tuple(
                "gideon.exd.example.internal" if part == "gideon.example.org" else part
                for part in HANDSHAKE
            )
            host.commands[second_handshake] = host.commands[HANDSHAKE]
        return host

    def applied_host(self, site: Path = EXAMPLE, *, include_searxng: bool = True) -> ApplyHost:
        host = self.new_host(site, include_searxng=include_searxng)
        code, _out, err = run_apply_once(host)
        self.assertEqual((code, err), (0, ""))
        return host


class EngineRotation(RealStack):
    def test_engine_rotates_mount_and_carried_consumers_then_converges(self) -> None:
        host = self.applied_host()
        path = f"{SECRETS_DIR}/engine_api_key"
        old_value = host.files[path].strip()
        before_manifest = yaml.safe_load(host.files[f"{RENDERED}/applied.yaml"])
        baseline_calls = len(host.calls)
        baseline_writes = len(host.writes)

        code, out, err = run_rotate(host, "engine_api_key")

        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(
            stages(out),
            [
                "preconditions",
                "plan",
                "rotate",
                "recreate",
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
        new_value = host.files[path].strip()
        self.assertNotEqual(new_value, old_value)
        self.assertEqual(host.write_modes[path], 0o440)
        self.assertIn((path, 0, 4242), host.chown_calls)
        calls = argv_calls(host)[baseline_calls:]
        self.assertEqual(calls.count(force_recreate(ENGINE_SERVICE_NAME)), 1)
        self.assertEqual(calls.count(force_recreate(API_SERVICE_NAME)), 1)
        self.assertEqual(calls.count(force_recreate("open-webui")), 0)
        self.assertLess(
            calls.index(force_recreate(ENGINE_SERVICE_NAME)),
            calls.index(force_recreate(API_SERVICE_NAME)),
        )
        self.assertEqual(
            yaml.safe_load(host.files[f"{RENDERED}/applied.yaml"])["services"][ENGINE_SERVICE_NAME],
            before_manifest["services"][ENGINE_SERVICE_NAME],
        )
        self.assertEqual(host.writes[-1], f"{RENDERED}/applied.yaml")
        written = host.writes[baseline_writes:]
        allowed = {path}
        for written_path in written:
            if new_value in host.files[written_path]:
                self.assertIn(written_path, allowed)
        self.assertNotIn(f"{RENDERED}/open-webui/env", written)
        for command, environment in host.calls[baseline_calls:]:
            self.assertNotIn(new_value, command)
            if environment is not None:
                self.assertNotIn(new_value, environment.values())
        for command, input_text in host.inputs[baseline_calls:]:
            self.assertNotIn(new_value, command)
            self.assertNotIn(new_value, input_text or "")
        assert_no_secret_text(self, out + err, (new_value,))


class ApiRotation(RealStack):
    def test_gideon_api_key_rotates_mount_and_frontend_carrier_then_converges(self) -> None:
        host = self.applied_host()
        path = f"{SECRETS_DIR}/gideon_api_key"
        env_path = f"{RENDERED}/open-webui/env"
        old_value = host.files[path].strip()
        baseline_calls = len(host.calls)
        baseline_writes = len(host.writes)

        code, out, err = run_rotate(host, "gideon_api_key")

        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(
            stages(out),
            [
                "preconditions",
                "plan",
                "rotate",
                "recreate",
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
        self.assertIn("recreate: ok — recreated gideon-api: mount gideon_api_key", out)
        self.assertIn("start: ok — recreated open-webui: changed rendered files", out)
        new_value = host.files[path].strip()
        self.assertNotEqual(new_value, old_value)
        self.assertEqual(host.write_modes[path], 0o440)
        self.assertIn((path, 0, 4242), host.chown_calls)
        calls = argv_calls(host)[baseline_calls:]
        self.assertEqual(calls.count(force_recreate(ENGINE_SERVICE_NAME)), 0)
        self.assertEqual(calls.count(force_recreate(API_SERVICE_NAME)), 1)
        self.assertEqual(calls.count(force_recreate("open-webui")), 1)
        self.assertLess(
            calls.index(force_recreate(API_SERVICE_NAME)),
            calls.index(force_recreate("open-webui")),
        )
        self.assertIn(new_value, host.files[env_path])
        written = host.writes[baseline_writes:]
        allowed = {path, env_path}
        for written_path in written:
            if new_value in host.files[written_path]:
                self.assertIn(written_path, allowed)
        for command, environment in host.calls[baseline_calls:]:
            self.assertNotIn(new_value, command)
            if environment is not None:
                self.assertNotIn(new_value, environment.values())
        for command, input_text in host.inputs[baseline_calls:]:
            self.assertNotIn(new_value, command)
            self.assertNotIn(new_value, input_text or "")
        assert_no_secret_text(self, out + err, (new_value,))


class RefusalClasses(RealStack):
    def test_refusals_are_before_any_new_write_or_compose_call(self) -> None:
        host = self.applied_host()
        cases = (
            ("unknown-name", ("engine_api_key", "webui_secret_key")),
            ("tls_key", ("tls reload",)),
            ("ldap_bind_password", ("/etc/gideon/secrets/ldap_bind_password", "apply", "grafana")),
            ("postgres_gideon_password", ("Postgres role", "gideon")),
            ("gideon_admin_password", ("gideon-admin", "frontend")),
            ("grafana_admin_password", ("stored admin user", "/api/user/password")),
        )
        for name, expected in cases:
            with self.subTest(name=name):
                host.calls.clear()
                host.inputs.clear()
                host.writes.clear()
                code, out, err = run_rotate(host, name)
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertEqual(host.calls, [])
                self.assertEqual(host.writes, [])
                for text in expected:
                    self.assertIn(text, err)
                assert_no_secret_text(self, out + err)

    def test_root_refusal_has_the_given_name_and_no_host_call(self) -> None:
        host = ApplyHost(healthy_commands(), base_files(), euid=1000)
        code, out, err = run_rotate(host, "engine_api_key")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("secrets rotate engine_api_key", err)
        self.assertEqual(host.calls, [])
        assert_no_secret_text(self, out + err)


class Preconditions(RealStack):
    def test_no_applied_record_refuses_after_only_the_version_check(self) -> None:
        host = self.new_host()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run_render(argparse.Namespace(diff=False), host=host, root=ROOT), 0)
        host.calls.clear()
        host.writes.clear()
        code, out, err = run_rotate(host, "engine_api_key")
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertIn("preconditions: refuse", out)
        self.assertIn("apply", out)
        self.assertEqual(host.files[f"{SECRETS_DIR}/engine_api_key"], "fixture-engine_api_key\n")
        self.assertEqual(host.writes, [])
        self.assertEqual(argv_calls(host), [VERSION])
        assert_no_secret_text(self, out + err)

    def test_pending_render_change_refuses_before_the_secret_write(self) -> None:
        host = self.applied_host()
        host.files[SITE] = host.files[SITE].replace("csa1@example.org", "changed@example.org")
        host.calls.clear()
        host.writes.clear()
        code, out, err = run_rotate(host, "engine_api_key")
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertIn("render --diff", out)
        self.assertIn("sudo python3 -m gideon apply", out)
        self.assertEqual(host.files[f"{SECRETS_DIR}/engine_api_key"], "fixture-engine_api_key\n")
        self.assertEqual(host.writes, [])
        assert_no_secret_text(self, out + err)


class PartialRotation(RealStack):
    def test_chown_failure_reports_written_but_unowned_and_does_not_recreate(self) -> None:
        host = self.applied_host()
        baseline_calls = len(host.calls)
        old_value = host.files[f"{SECRETS_DIR}/engine_api_key"]
        host.chown_failure = True
        code, out, err = run_rotate(host, "engine_api_key")
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertIn("written but unowned", out)
        self.assertIn("sudo chown root:gideon /etc/gideon/secrets/engine_api_key", out)
        self.assertIn("sudo python3 -m gideon apply", out)
        self.assertIn("sudo python3 -m gideon secrets rotate engine_api_key", out)
        self.assertNotEqual(host.files[f"{SECRETS_DIR}/engine_api_key"], old_value)
        self.assertEqual(
            [call for call in argv_calls(host)[baseline_calls:] if "--force-recreate" in call],
            [],
        )
        assert_no_secret_text(self, out + err)

    def test_failed_mount_recreate_requires_apply_then_a_fresh_rotation(self) -> None:
        # The connection key is the carried secret since the cutover: its value
        # reaches the frontend's rendered env file, so a rotation that dies past
        # the write leaves a pending change the next run is refused on.
        host = self.applied_host()
        api_recreate = force_recreate(API_SERVICE_NAME)
        host.commands[api_recreate] = done(api_recreate, rc=1, stderr="service failed")
        code, out, err = run_rotate(host, "gideon_api_key")
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertIn("sudo docker compose", out)
        self.assertIn(f"logs {API_SERVICE_NAME}", out)
        self.assertIn("sudo python3 -m gideon apply", out)
        self.assertIn("sudo python3 -m gideon secrets rotate gideon_api_key again", out)
        first_value = host.files[f"{SECRETS_DIR}/gideon_api_key"]

        host.calls.clear()
        host.writes.clear()
        code, out, err = run_rotate(host, "gideon_api_key")
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertIn("preconditions: refuse", out)
        self.assertIn("apply", out)
        self.assertEqual(host.writes, [])
        self.assertEqual(host.files[f"{SECRETS_DIR}/gideon_api_key"], first_value)

        host.commands[api_recreate] = done(api_recreate)
        code, out, err = run_apply_once(host)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("start: ok — recreated open-webui: changed rendered files", out)
        code, out, err = run_rotate(host, "gideon_api_key")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("rotate: ok", out)
        assert_no_secret_text(self, out + err)

    def test_failed_engine_recreate_leaves_no_pending_change_so_a_rerun_proceeds(self) -> None:
        # The engine key is carried by nothing since the cutover — its two homes
        # are the engine's and the service's Compose mounts — so a rotation that
        # dies past the write moves no rendered byte, the recreate judgment stays
        # empty, and the recovery is the re-run itself rather than an apply.
        host = self.applied_host()
        engine_recreate = force_recreate(ENGINE_SERVICE_NAME)
        host.commands[engine_recreate] = done(engine_recreate, rc=1, stderr="engine failed")
        code, out, err = run_rotate(host, "engine_api_key")
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertIn(f"logs {ENGINE_SERVICE_NAME}", out)
        self.assertIn("sudo python3 -m gideon secrets rotate engine_api_key again", out)
        first_value = host.files[f"{SECRETS_DIR}/engine_api_key"]

        host.commands[engine_recreate] = done(engine_recreate)
        baseline_calls = len(host.calls)
        code, out, err = run_rotate(host, "engine_api_key")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("preconditions: ok", out)
        self.assertIn("rotate: ok", out)
        self.assertNotEqual(host.files[f"{SECRETS_DIR}/engine_api_key"], first_value)
        calls = argv_calls(host)[baseline_calls:]
        self.assertEqual(calls.count(force_recreate(ENGINE_SERVICE_NAME)), 1)
        self.assertEqual(calls.count(force_recreate(API_SERVICE_NAME)), 1)
        self.assertEqual(calls.count(force_recreate("open-webui")), 0)
        assert_no_secret_text(self, out + err)


class OtherRotations(RealStack):
    def test_session_key_recreates_frontend_by_mount_and_converge_recreates_nothing(self) -> None:
        host = self.applied_host()
        baseline = len(host.calls)
        code, out, err = run_rotate(host, "webui_secret_key")
        self.assertEqual((code, err), (0, ""), out)
        calls = argv_calls(host)[baseline:]
        self.assertEqual(calls.count(force_recreate("open-webui")), 1)
        self.assertIn("recreated open-webui: mount webui_secret_key", out)
        self.assertIn("recreate: ok — no store-tier rendered file or compose block changed", out)
        self.assertIn("start: ok — no other rendered file or compose block changed", out)
        assert_no_secret_text(self, out + err)

    def test_searxng_key_is_recreated_by_the_converge_and_skips_when_search_is_off(self) -> None:
        host = self.applied_host()
        baseline = len(host.calls)
        code, out, err = run_rotate(host, "searxng_secret_key")
        self.assertEqual((code, err), (0, ""), out)
        calls = argv_calls(host)[baseline:]
        self.assertEqual(calls.count(force_recreate("searxng")), 1)
        self.assertNotIn("mount searxng_secret_key", out)
        self.assertIn("start: ok — recreated searxng: changed rendered files", out)
        assert_no_secret_text(self, out + err)

        off = self.applied_host(SECOND, include_searxng=False)
        baseline_writes = len(off.writes)
        baseline_calls = len(off.calls)
        code, out, err = run_rotate(off, "searxng_secret_key")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("no service on this host consumes searxng_secret_key", out)
        self.assertEqual(len(off.writes), baseline_writes)
        self.assertEqual(argv_calls(off)[baseline_calls:].count(force_recreate("searxng")), 0)
        assert_no_secret_text(self, out + err)

    def test_minted_key_is_removed_and_reminted_without_recreating_a_service(self) -> None:
        host = self.applied_host()
        path = f"{SECRETS_DIR}/gideon_admin_api_key"
        self.assertIn(path, host.files)
        baseline = len(host.calls)
        code, out, err = run_rotate(host, "gideon_admin_api_key")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("file removed", out)
        self.assertIn("apply-manifest: ok — minted secrets: gideon_admin_api_key", out)
        self.assertIn(path, host.files)
        self.assertEqual(
            [call for call in argv_calls(host)[baseline:] if "--force-recreate" in call], []
        )
        code, out, err = run_rotate(host, "gideon_admin_api_key")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("apply-manifest: ok — minted secrets: gideon_admin_api_key", out)
        self.assertIn(path, host.files)
        assert_no_secret_text(self, out + err)

    def test_no_gpu_engine_key_is_a_no_write_skip(self) -> None:
        host = self.new_host()
        host.files[os.fspath(nogpu.NO_GPU_PATH)] = "declared\n"
        host.commands[PS] = done(
            PS,
            stdout=running_rows(
                include_dcgm=False, include_engine=False, include_api=False
            ),
        )
        code, _out, err = run_apply_once(host)
        self.assertEqual((code, err), (0, ""))
        baseline_writes = len(host.writes)
        baseline_calls = len(host.calls)
        code, out, err = run_rotate(host, "engine_api_key")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("no service on this host consumes engine_api_key", out)
        self.assertEqual(len(host.writes), baseline_writes)
        self.assertEqual(
            [call for call in argv_calls(host)[baseline_calls:] if "--force-recreate" in call], []
        )
        assert_no_secret_text(self, out + err)
