"""The Open WebUI client, readiness wait, and the idempotent bootstrap (spec §3.5, §4.4)."""

import contextlib
import http.server
import json
import os
import subprocess
import threading
import time
import unittest
from collections.abc import Callable, Generator, Mapping
from pathlib import Path
from typing import Any, ClassVar, cast
from urllib.parse import unquote

import yaml  # type: ignore[import-untyped]

from gideon.host.owui import (
    Client,
    OwuiError,
    Response,
    bootstrap,
    ingress_client_factory,
    load_manifest,
    wait_ready,
)
from gideon.host.render.owui import (
    ARITHMETIC_GUARDRAIL_ID,
    BREAK_GLASS,
    CITATION_STAMP_ID,
    EVAL_IDENTITY,
    GENERAL_PRESET_ID,
    SERVICE_GROUP,
)
from gideon.host.secrets import SECRETS_DIR
from gideon.host.sysio import Command, PathLike

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "tests/fixtures/render/example/open-webui/manifest.yaml"
RENDERED = "/etc/gideon/rendered"
ADMIN_PASSWORD = "admin-pw"
EVAL_PASSWORD = "eval-pw"
ADMIN_KEY = "sk-admin-secret"
EVAL_KEY = "sk-eval-secret"


class FakeHost:
    def __init__(self, files: Mapping[str, str]) -> None:
        self.files = dict(files)
        self.modes: dict[str, int] = {}
        self.owners: dict[str, tuple[int, int]] = {}

    def run(self, argv: Command, *, check: bool = False, input: str | None = None, cwd: PathLike | None = None, env: Mapping[str, str] | None = None, timeout: float | None = None, passthrough: bool = False) -> subprocess.CompletedProcess[str]:
        # Minting a key writes a secret, which resolves the service group.
        if tuple(argv) == ("getent", "group", "gideon"):
            return subprocess.CompletedProcess(list(argv), 0, "gideon:x:4242:\n", "")
        raise NotImplementedError

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        key = os.fspath(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644) -> None:
        self.files[os.fspath(path)] = text
        self.modes[os.fspath(path)] = mode

    def exists(self, path: PathLike) -> bool:
        return os.fspath(path) in self.files

    def listdir(self, path: PathLike) -> list[str]:
        raise NotImplementedError

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        raise NotImplementedError

    def stat(self, path: PathLike) -> os.stat_result:
        raise NotImplementedError

    def chmod(self, path: PathLike, mode: int) -> None:
        raise NotImplementedError

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        self.owners[os.fspath(path)] = (uid, gid)

    def mkdir(self, path: PathLike, *, mode: int = 0o755, parents: bool = False, exist_ok: bool = False) -> None:
        raise NotImplementedError

    def geteuid(self) -> int:
        return 0


class Frontend:
    """An in-memory Open WebUI: the observed v0.11.0 shapes for the routes bootstrap uses."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {
            "u-admin": {"id": "u-admin", "email": BREAK_GLASS.email, "name": BREAK_GLASS.username, "role": "admin", "username": None},
        }
        self.passwords = {BREAK_GLASS.email: ADMIN_PASSWORD}
        self.keys: dict[str, str] = {}
        self.groups: dict[str, dict[str, Any]] = {}
        self.members: dict[str, set[str]] = {}
        self.functions: list[dict[str, Any]] = []
        self.models: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self.next_id = 0
        self.swallow_sync = False
        # The live listing built from a stale cache: every entry without its attachment key.
        self.stale_live_models = False

    def fresh_id(self, prefix: str) -> str:
        self.next_id += 1
        return f"{prefix}-{self.next_id}"

    def user_for(self, credential: str | None) -> dict[str, Any] | None:
        if credential is None:
            return None
        if credential.startswith("sk-"):
            for user_id, key in self.keys.items():
                if key == credential:
                    return self.users[user_id]
            return None
        if credential.startswith("token:"):
            return self.users.get(credential.partition(":")[2])
        return None

    def handle(self, method: str, path: str, body: object | None, credential: str | None) -> Response:
        self.calls.append((method, path))
        route = path.split("?")[0]
        if route == "/ready":
            return Response(200, {"status": True})
        if route == "/api/v1/auths/signin":
            assert isinstance(body, Mapping)
            for candidate in self.users.values():
                if candidate["email"] == body["email"] and self.passwords.get(candidate["email"]) == body["password"]:
                    return Response(200, {"token": f"token:{candidate['id']}", "token_type": "Bearer", **candidate})
            return Response(400, {"detail": "invalid credentials"})
        user = self.user_for(credential)
        if user is None:
            return Response(401, {"detail": "unauthorized"})
        if route == "/api/v1/auths/api_key":
            if user["role"] != "admin" and not any(
                self.groups[group_id]["permissions"].get("features", {}).get("api_keys") for group_id in self.members if user["id"] in self.members[group_id]
            ):
                return Response(403, {"detail": "api keys are not permitted"})
            key = f"sk-{user['id']}-{self.fresh_id('k')}"
            self.keys[user["id"]] = key
            return Response(200, {"api_key": key})
        if user["role"] != "admin":
            return Response(401, {"detail": "admin required"})
        if route == "/api/v1/users/all":
            return Response(200, {"users": list(self.users.values()), "total": len(self.users)})
        if route == "/api/v1/auths/add":
            assert isinstance(body, Mapping)
            user_id = self.fresh_id("u")
            self.users[user_id] = {"id": user_id, "email": body["email"], "name": body["name"], "role": body["role"], "username": None}
            self.passwords[body["email"]] = body["password"]
            return Response(200, {**self.users[user_id], "token": "unused", "token_type": "Bearer"})
        if route.startswith("/api/v1/users/") and route.endswith("/update"):
            assert isinstance(body, Mapping)
            user_id = route.split("/")[4]
            self.users[user_id]["role"] = body["role"]
            return Response(200, self.users[user_id])
        if route == "/api/v1/groups/":
            return Response(200, [{**group, "member_count": len(self.members[group["id"]])} for group in self.groups.values()])
        if route == "/api/v1/groups/create":
            assert isinstance(body, Mapping)
            group_id = self.fresh_id("g")
            self.groups[group_id] = {"id": group_id, "name": body["name"], "description": body["description"], "permissions": body["permissions"]}
            self.members[group_id] = set()
            return Response(200, self.groups[group_id])
        if route.startswith("/api/v1/groups/id/"):
            parts = route.split("/")
            group_id, action = parts[5], "/".join(parts[6:])
            if group_id not in self.groups:
                return Response(404, {"detail": "not found"})
            if action == "update":
                assert isinstance(body, Mapping)
                self.groups[group_id].update(name=body["name"], permissions=body["permissions"])
                return Response(200, self.groups[group_id])
            if action == "users":
                return Response(200, [self.users[user_id] for user_id in sorted(self.members[group_id])])
            if action == "users/add":
                assert isinstance(body, Mapping)
                self.members[group_id].update(body["user_ids"])
                return Response(200, self.groups[group_id])
            if action == "users/remove":
                assert isinstance(body, Mapping)
                self.members[group_id].difference_update(body["user_ids"])
                return Response(200, self.groups[group_id])
            if action == "delete":
                del self.groups[group_id]
                del self.members[group_id]
                return Response(200, True)
        if route == "/api/models":
            # The merged listing (docs/research/owui-model-record.md §4): the
            # record under `info` without its params, the preset flag by base id.
            entries: list[dict[str, Any]] = []
            for model in self.models:
                info = {key: value for key, value in model.items() if key != "params"}
                if self.stale_live_models and isinstance(info.get("meta"), dict):
                    info["meta"] = {key: value for key, value in info["meta"].items() if key != "filterIds"}
                entries.append(
                    {
                        "id": model["id"],
                        "name": model.get("name"),
                        "object": "model",
                        "preset": model.get("base_model_id") is not None,
                        "info": info,
                    }
                )
            return Response(200, {"data": entries})
        if route == "/api/v1/functions/":
            return Response(200, list(self.functions))
        if route.startswith("/api/v1/functions/id/"):
            parts = route.split("/")
            identifier = unquote(parts[5])
            function = next((item for item in self.functions if item.get("id") == identifier), None)
            if len(parts) == 7 and parts[6] == "valves":
                return Response(200, (function or {}).get("valves") or {})
            return Response(200, function)
        # The pinned frontend separates preset and base rows: `/list` is the
        # paged preset route and `/base` is the unpaged base route
        # (docs/research/owui-model-record.md §1.2–1.3, §8).
        if route == "/api/v1/models/base":
            return Response(200, [model for model in self.models if model.get("base_model_id") is None])
        if route == "/api/v1/functions/sync":
            assert isinstance(body, Mapping)
            if self.swallow_sync:
                return Response(200, [])
            self.functions = list(body["functions"])
            return Response(200, self.functions)
        if route == "/api/v1/models/list":
            page = int(path.partition("page=")[2] or 1)
            presets = [model for model in self.models if model.get("base_model_id") is not None]
            return Response(200, {"items": presets if page == 1 else [], "total": len(presets)})
        if route == "/api/v1/models/sync":
            assert isinstance(body, Mapping)
            if self.swallow_sync:
                return Response(200, [])
            self.models = list(body["models"])
            return Response(200, self.models)
        if route == "/api/v1/knowledge/":
            return Response(200, {"items": [], "total": 0})
        return Response(200, "<!doctype html>")


class FakeClient(Client):
    """The real client with its transport replaced by the in-memory frontend."""

    def __init__(self, frontend: Frontend, *, api_key: str | None = None, token: str | None = None) -> None:
        super().__init__("http://fake", api_key=api_key, token=token)
        self.frontend = frontend

    def request(self, method: str, path: str, body: object | None = None) -> Response:
        credential = self._api_key if self._api_key is not None else self._token
        return self.frontend.handle(method, path, body, credential)


def factory_for(frontend: Frontend):
    def factory(*, api_key: str | None = None, token: str | None = None) -> Client:
        return FakeClient(frontend, api_key=api_key, token=token)

    return factory


def manifest() -> Mapping[str, object]:
    return yaml.safe_load(MANIFEST_PATH.read_text())


def secrets(*, admin_key: bool = False, eval_key: bool = False) -> dict[str, str]:
    files = {
        f"{SECRETS_DIR}/gideon_admin_password": ADMIN_PASSWORD + "\n",
        f"{SECRETS_DIR}/gideon_eval_password": EVAL_PASSWORD + "\n",
    }
    if admin_key:
        files[f"{SECRETS_DIR}/gideon_admin_api_key"] = ADMIN_KEY + "\n"
    if eval_key:
        files[f"{SECRETS_DIR}/gideon_eval_api_key"] = EVAL_KEY + "\n"
    return files


class _Handler(http.server.BaseHTTPRequestHandler):
    stream_headers: ClassVar[dict[str, str | None]] = {}

    def _reply(self, status: int, payload: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError):
            self.wfile.write(payload)

    def _sse(self, payload: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
            self.wfile.flush()
        except BrokenPipeError:
            pass

    def do_GET(self) -> None:
        if self.path == "/ready":
            self._reply(200, b'{"status": true}')
        elif self.path == "/echo-auth":
            self._reply(200, json.dumps({"authorization": self.headers.get("Authorization")}).encode())
        elif self.path == "/echo-host":
            self._reply(200, json.dumps({"host": self.headers.get("Host")}).encode())
        elif self.path == "/html":
            self._reply(200, b"<!doctype html>", "text/html")
        elif self.path == "/empty":
            self._reply(204, b"")
        elif self.path == "/sse":
            _Handler.stream_headers = {
                "authorization": self.headers.get("Authorization"),
                "host": self.headers.get("Host"),
            }
            self._sse(
                b"data: first\n\n: ignored\n\n\n"
                b"data: second\n\n"
                b"data: third\n\n"
                b"data: [DONE]\n\n"
            )
        elif self.path == "/sse-truncated":
            self._sse(b"data: first\n\ndata: second\n\n")
        elif self.path == "/sse-json":
            self._reply(200, b'{"answer": "json"}')
        elif self.path == "/sse-error":
            self._reply(400, b"body must not appear in the error", "text/plain")
        elif self.path == "/slow":
            time.sleep(1.0)
            self._reply(200, b'{"slow": true}')
        elif self.path == "/sse-stalled":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                self.wfile.write(b"data: first\n\n")
                self.wfile.flush()
                time.sleep(1.0)
                self.wfile.write(b"data: second\n\ndata: [DONE]\n\n")
                self.wfile.flush()
            except BrokenPipeError:
                pass
        else:
            self._reply(404, b'{"detail": "not found"}')

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else None
        self._reply(200, json.dumps({"echo": body, "content_type": self.headers.get("Content-Type")}).encode())

    def log_message(self, format: str, *args: object) -> None:
        return


class ClientOverLoopback(unittest.TestCase):
    server: http.server.HTTPServer
    thread: threading.Thread

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def client(self, **kwargs: Any) -> Client:
        return Client(f"http://127.0.0.1:{self.server.server_port}", **kwargs)

    def test_json_round_trip_and_bearer_header(self) -> None:
        response = self.client(api_key="sk-1").request("POST", "/anything", {"a": 1})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, {"echo": {"a": 1}, "content_type": "application/json"})
        self.assertEqual(self.client(api_key="sk-1", token="t").request("GET", "/echo-auth").body, {"authorization": "Bearer sk-1"})
        self.assertEqual(self.client(token="t").request("GET", "/echo-auth").body, {"authorization": "Bearer t"})
        self.assertEqual(self.client().request("GET", "/echo-auth").body, {"authorization": None})

    def test_host_header_carries_the_site_hostname_the_ingress_routes_by(self) -> None:
        self.assertEqual(self.client(server_hostname="gideon.example.org").request("GET", "/echo-host").body, {"host": "gideon.example.org"})
        self.assertEqual(self.client().request("GET", "/echo-host").body, {"host": f"127.0.0.1:{self.server.server_port}"})

    def test_non_2xx_and_empty_bodies_do_not_raise(self) -> None:
        self.assertEqual(self.client().request("GET", "/missing").status, 404)
        self.assertEqual(self.client().request("GET", "/empty"), Response(204))

    def test_html_where_json_was_expected_is_a_refusal_naming_the_path(self) -> None:
        with self.assertRaises(OwuiError) as ctx:
            self.client().request("GET", "/html")
        self.assertIn("/html", ctx.exception.problem)

    def test_ready_and_wait_ready(self) -> None:
        self.assertTrue(self.client().ready())
        self.assertTrue(wait_ready(self.client(), attempts=1, sleep=lambda _: None).ok)

    def test_connection_refused_is_a_fix_bearing_error(self) -> None:
        with self.assertRaises(OwuiError) as ctx:
            Client("http://127.0.0.1:1").request("GET", "/ready")
        self.assertIn("then retry", ctx.exception.fix)

    def test_credentials_never_enter_the_response_repr(self) -> None:
        response = self.client(token="t").request("GET", "/echo-auth")
        self.assertNotIn("Bearer", repr(response))

    def test_stream_yields_data_payloads_in_order_and_stops_at_done(self) -> None:
        payloads = list(
            self.client(token="stream-token", server_hostname="gideon.example").stream(
                "GET", "/sse"
            )
        )
        self.assertEqual(payloads, ["first", "second", "third"])
        self.assertEqual(
            _Handler.stream_headers,
            {"authorization": "Bearer stream-token", "host": "gideon.example"},
        )

    def test_stream_requires_done_after_a_truncated_body(self) -> None:
        with self.assertRaises(OwuiError) as ctx:
            list(self.client().stream("GET", "/sse-truncated"))
        self.assertIn("before [DONE]", ctx.exception.problem)
        self.assertIn("then retry", ctx.exception.fix)

    def test_stream_requires_event_stream_content_type(self) -> None:
        with self.assertRaises(OwuiError) as ctx:
            list(self.client().stream("GET", "/sse-json"))
        self.assertIn("content type", ctx.exception.problem)
        self.assertIn("then retry", ctx.exception.fix)

    def test_stream_non_200_reports_status_without_body_text(self) -> None:
        with self.assertRaises(OwuiError) as ctx:
            list(self.client().stream("GET", "/sse-error"))
        self.assertIn("HTTP 400", ctx.exception.problem)
        self.assertNotIn("body must not appear", str(ctx.exception))

    def test_stream_deadline_applies_to_each_read(self) -> None:
        with self.assertRaises(OwuiError) as ctx:
            list(
                self.client(timeout=2.0).stream(
                    "GET",
                    "/sse-stalled",
                    deadline=time.monotonic() + 0.1,
                )
            )
        self.assertIn("timed out", ctx.exception.problem)
        self.assertIn("then retry", ctx.exception.fix)

    def test_stream_deadline_already_expired_before_first_read(self) -> None:
        with self.assertRaises(OwuiError) as ctx:
            list(
                self.client().stream(
                    "GET", "/sse", deadline=10.0, monotonic=lambda: 11.0
                )
            )
        self.assertIn("timed out", ctx.exception.problem)

    def test_stream_consumer_can_stop_early(self) -> None:
        stream = cast(Generator[str, None, None], self.client().stream("GET", "/sse"))
        self.assertEqual(next(stream), "first")
        stream.close()

    def test_client_timeout_bounds_a_slow_response(self) -> None:
        self.assertEqual(self.client(timeout=2.0).request("GET", "/slow").status, 200)
        with self.assertRaises(OwuiError) as ctx:
            self.client(timeout=0.05).request("GET", "/slow")
        self.assertIn("then retry", ctx.exception.fix)

    def test_factory_passes_its_timeout_to_every_client(self) -> None:
        """A turn runs for minutes; apply's default stays the factory's when the keyword is absent."""

        given = ingress_client_factory("gideon.example", ca_path=None, timeout=2.0)(token="t")
        default = ingress_client_factory("gideon.example", ca_path=None)(token="t")
        self.assertEqual(given._timeout, 2.0)
        self.assertEqual(default._timeout, Client("http://fake")._timeout)


class ClientConstruction(unittest.TestCase):
    def test_invalid_urls_refuse(self) -> None:
        for url in ("ftp://x", "http://", "http://user:pw@host", "http://host/?q=1", "http://host:99999"):
            with self.subTest(url=url), self.assertRaises(OwuiError):
                Client(url)

    def test_role_values_are_validated_client_side(self) -> None:
        client = FakeClient(Frontend(), api_key=ADMIN_KEY)
        with self.assertRaises(OwuiError):
            client.update_role("u-admin", "superuser")


class WaitReady(unittest.TestCase):
    def test_polls_until_ready_with_the_injected_sleep(self) -> None:
        answers = iter([Response(503), Response(200, {"status": False}), Response(200, {"status": True})])

        class Flaky:
            def request(self, method: str, path: str, body: object | None = None) -> Response:
                return next(answers)

        slept: list[float] = []
        result = wait_ready(Flaky(), attempts=5, sleep=slept.append)  # type: ignore[arg-type]
        self.assertTrue(result.ok)
        self.assertEqual(len(slept), 2)

    def test_gives_up_with_the_last_problem(self) -> None:
        class Down:
            def request(self, method: str, path: str, body: object | None = None) -> Response:
                raise OwuiError("Open WebUI request failed for /ready.")

        result = wait_ready(Down(), attempts=2, sleep=lambda _: None)  # type: ignore[arg-type]
        self.assertFalse(result.ok)
        self.assertIn("/ready", result.problem or "")


class Bootstrap(unittest.TestCase):
    def run_bootstrap(self, frontend: Frontend, host: FakeHost):
        return bootstrap(host, factory_for(frontend), manifest(), rendered_dir=RENDERED)

    def test_first_run_converges_everything_and_mints_both_keys(self) -> None:
        frontend = Frontend()
        host = FakeHost(secrets())
        report = self.run_bootstrap(frontend, host)
        self.assertTrue(report.ok, report.problem)
        self.assertEqual(report.created_groups, ("GIDEON-Users", "GIDEON-Admins", SERVICE_GROUP))
        self.assertEqual(report.minted, ("gideon_admin_api_key", "gideon_eval_api_key"))
        for name in report.minted:
            path = f"{SECRETS_DIR}/{name}"
            self.assertEqual(host.modes[path], 0o440)
            self.assertTrue(host.files[path].startswith("sk-"))
        names = {group["name"]: group for group in frontend.groups.values()}
        self.assertTrue(names[SERVICE_GROUP]["permissions"]["features"]["api_keys"])
        self.assertFalse(names["GIDEON-Users"]["permissions"]["features"]["api_keys"])
        eval_user = next(user for user in frontend.users.values() if user["email"] == EVAL_IDENTITY.email)
        self.assertEqual(eval_user["role"], "user")
        service_id = names[SERVICE_GROUP]["id"]
        self.assertEqual(frontend.members[service_id], {eval_user["id"]})
        # Only the eval identity holds a key besides the admin, and it was minted after its membership.
        self.assertEqual(set(frontend.keys), {"u-admin", eval_user["id"]})
        add_index = frontend.calls.index(("POST", f"/api/v1/groups/id/{service_id}/users/add"))
        mint_indexes = [index for index, call in enumerate(frontend.calls) if call == ("POST", "/api/v1/auths/api_key")]
        self.assertLess(add_index, mint_indexes[-1])

    def test_second_run_reports_nothing(self) -> None:
        frontend = Frontend()
        host = FakeHost(secrets())
        self.run_bootstrap(frontend, host)
        report = self.run_bootstrap(frontend, host)
        self.assertTrue(report.ok, report.problem)
        self.assertEqual(report, type(report)())

    def test_hand_added_function_and_stray_group_are_removed_by_id(self) -> None:
        frontend = Frontend()
        host = FakeHost(secrets())
        self.run_bootstrap(frontend, host)
        frontend.functions.append({"id": "hand_added", "type": "filter"})
        frontend.models.append({"id": "stray_model", "base_model_id": "gideon-generator"})
        frontend.models.append({"id": "stray_base"})
        stray = frontend.fresh_id("g")
        frontend.groups[stray] = {"id": stray, "name": "stray", "description": "", "permissions": {}}
        frontend.members[stray] = set()
        report = self.run_bootstrap(frontend, host)
        self.assertTrue(report.ok, report.problem)
        self.assertEqual(report.removed_functions, ("hand_added",))
        self.assertEqual(report.removed_models, ("stray_model", "stray_base"))
        self.assertEqual(report.removed_groups, ("stray",))
        self.assertEqual(
            [item["id"] for item in frontend.functions],
            [ARITHMETIC_GUARDRAIL_ID, CITATION_STAMP_ID],
        )
        expected_models = manifest()["models"]
        assert isinstance(expected_models, list)
        self.assertEqual(frontend.models, expected_models)

    def _assert_swallowed_model_correction_refuses(
        self,
        mutate: Callable[[dict[str, Any]], None],
        field: str,
        model_id: str = "gideon-generator",
    ) -> None:
        frontend = Frontend()
        host = FakeHost(secrets())
        self.run_bootstrap(frontend, host)
        mutate(next(model for model in frontend.models if model["id"] == model_id))
        frontend.swallow_sync = True
        report = self.run_bootstrap(frontend, host)
        self.assertFalse(report.ok)
        self.assertIn("models sync", report.problem or "")
        self.assertIn(model_id, report.problem or "")
        self.assertIn(field, report.problem or "")
        self.assertIn("logs open-webui", report.fix)

    def _assert_swallowed_general_correction_refuses(
        self, mutate: Callable[[dict[str, Any]], None], field: str
    ) -> None:
        self._assert_swallowed_model_correction_refuses(mutate, field, GENERAL_PRESET_ID)

    def test_swallowed_model_sync_with_missing_row_refuses(self) -> None:
        frontend = Frontend()
        frontend.swallow_sync = True
        desired = dict(manifest())
        desired["functions"] = []
        report = bootstrap(FakeHost(secrets()), factory_for(frontend), desired, rendered_dir=RENDERED)
        self.assertFalse(report.ok)
        self.assertIn("models sync", report.problem or "")
        self.assertIn("read-back differs by id", report.problem or "")
        self.assertIn(GENERAL_PRESET_ID, report.problem or "")
        self.assertIn("logs open-webui", report.fix)
        self.assertEqual(frontend.models, [])

    def test_swallowed_model_sync_with_stale_builtin_tools_refuses(self) -> None:
        def mutate(model: dict[str, Any]) -> None:
            meta = model["meta"]
            assert isinstance(meta, dict)
            capabilities = meta["capabilities"]
            assert isinstance(capabilities, dict)
            capabilities["builtin_tools"] = True

        self._assert_swallowed_model_correction_refuses(mutate, "meta.capabilities")

    def test_swallowed_model_sync_with_stale_params_refuses(self) -> None:
        def mutate(model: dict[str, Any]) -> None:
            model["params"] = {"temperature": 0.1}

        self._assert_swallowed_model_correction_refuses(mutate, "params")

    def test_swallowed_model_sync_with_extra_capability_refuses(self) -> None:
        def mutate(model: dict[str, Any]) -> None:
            meta = model["meta"]
            assert isinstance(meta, dict)
            capabilities = meta["capabilities"]
            assert isinstance(capabilities, dict)
            capabilities["usage"] = True

        self._assert_swallowed_model_correction_refuses(mutate, "meta.capabilities")

    def test_swallowed_model_sync_with_non_null_extra_meta_refuses(self) -> None:
        def mutate(model: dict[str, Any]) -> None:
            meta = model["meta"]
            assert isinstance(meta, dict)
            meta["description"] = "hand-edited"

        self._assert_swallowed_model_correction_refuses(mutate, "meta.description")

    def test_swallowed_model_sync_with_removed_base_grant_refuses(self) -> None:
        def mutate(model: dict[str, Any]) -> None:
            model["access_grants"] = []

        self._assert_swallowed_model_correction_refuses(mutate, "access_grants")

    def test_swallowed_model_sync_with_unhidden_base_refuses(self) -> None:
        def mutate(model: dict[str, Any]) -> None:
            meta = model["meta"]
            assert isinstance(meta, dict)
            meta["hidden"] = False

        self._assert_swallowed_model_correction_refuses(mutate, "meta.hidden")

    def test_swallowed_general_sync_with_changed_prompt_refuses(self) -> None:
        def mutate(model: dict[str, Any]) -> None:
            params = model["params"]
            assert isinstance(params, dict)
            params["system"] = "hand-edited"

        self._assert_swallowed_general_correction_refuses(mutate, "params")

    def test_swallowed_general_sync_with_dropped_mode_refuses(self) -> None:
        def mutate(model: dict[str, Any]) -> None:
            params = model["params"]
            assert isinstance(params, dict)
            del params["function_calling"]

        self._assert_swallowed_general_correction_refuses(mutate, "params")

    def test_swallowed_general_sync_with_changed_description_refuses(self) -> None:
        def mutate(model: dict[str, Any]) -> None:
            meta = model["meta"]
            assert isinstance(meta, dict)
            meta["description"] = "hand-edited"

        self._assert_swallowed_general_correction_refuses(mutate, "meta.description")

    def test_swallowed_general_sync_with_changed_suggestion_prompts_refuses(self) -> None:
        frontend = Frontend()
        host = FakeHost(secrets())
        self.run_bootstrap(frontend, host)
        suggestion_text = "Fictitious suggestion text used only by this test."
        general = next(model for model in frontend.models if model["id"] == GENERAL_PRESET_ID)
        meta = general["meta"]
        assert isinstance(meta, dict)
        meta["suggestion_prompts"] = [{"title": suggestion_text, "content": suggestion_text}]
        frontend.swallow_sync = True
        report = self.run_bootstrap(frontend, host)
        self.assertFalse(report.ok)
        problem = report.problem or ""
        self.assertIn("models sync", problem)
        self.assertIn(GENERAL_PRESET_ID, problem)
        self.assertIn("meta.suggestion_prompts", problem)
        self.assertNotIn(suggestion_text, problem)
        self.assertIn("logs open-webui", report.fix)

    def test_swallowed_general_sync_with_removed_filter_ids_refuses(self) -> None:
        def mutate(model: dict[str, Any]) -> None:
            meta = model["meta"]
            assert isinstance(meta, dict)
            del meta["filterIds"]

        self._assert_swallowed_general_correction_refuses(mutate, "meta.filterIds")

    def test_live_model_listing_is_read_after_the_models_sync(self) -> None:
        frontend = Frontend()
        report = self.run_bootstrap(frontend, FakeHost(secrets()))
        self.assertTrue(report.ok, report.problem)
        sync_positions = [index for index, call in enumerate(frontend.calls) if call == ("POST", "/api/v1/models/sync")]
        refresh_positions = [index for index, call in enumerate(frontend.calls) if call == ("GET", "/api/models")]
        self.assertEqual(len(refresh_positions), 1)
        self.assertGreater(refresh_positions[0], sync_positions[-1])

    def test_stale_live_listing_refuses_naming_the_attachment(self) -> None:
        frontend = Frontend()
        frontend.stale_live_models = True
        report = self.run_bootstrap(frontend, FakeHost(secrets()))
        self.assertFalse(report.ok)
        problem = report.problem or ""
        self.assertIn("models refresh", problem)
        self.assertIn(GENERAL_PRESET_ID, problem)
        self.assertIn("meta.filterIds", problem)
        self.assertIn("logs open-webui", report.fix)

    def test_swallowed_general_sync_with_removed_grant_refuses(self) -> None:
        def mutate(model: dict[str, Any]) -> None:
            model["access_grants"] = []

        self._assert_swallowed_general_correction_refuses(mutate, "access_grants")

    def test_swallowed_general_sync_with_flipped_capability_refuses(self) -> None:
        def mutate(model: dict[str, Any]) -> None:
            meta = model["meta"]
            assert isinstance(meta, dict)
            capabilities = meta["capabilities"]
            assert isinstance(capabilities, dict)
            capabilities["web_search"] = False

        self._assert_swallowed_general_correction_refuses(mutate, "meta.capabilities")

    def test_swallowed_model_sync_accepts_null_defaulted_meta_fields(self) -> None:
        frontend = Frontend()
        host = FakeHost(secrets())
        self.run_bootstrap(frontend, host)
        meta = frontend.models[0]["meta"]
        assert isinstance(meta, dict)
        meta.update(profile_image_url=None, description=None, knowledge=None)
        frontend.swallow_sync = True
        report = self.run_bootstrap(frontend, host)
        self.assertTrue(report.ok, report.problem)
        self.assertEqual(report, type(report)())

    def test_swallowed_functions_sync_with_missing_row_refuses(self) -> None:
        frontend = Frontend()
        frontend.swallow_sync = True
        desired = dict(manifest())
        desired["functions"] = [{"id": "desired-function"}]
        report = bootstrap(FakeHost(secrets()), factory_for(frontend), desired, rendered_dir=RENDERED)
        self.assertFalse(report.ok)
        self.assertIn("functions sync", report.problem or "")
        self.assertIn("read-back differs by id", report.problem or "")
        self.assertIn("desired-function", report.problem or "")
        self.assertIn("logs open-webui", report.fix)

    def _assert_swallowed_function_correction_refuses(
        self,
        mutate: Callable[[dict[str, Any]], None],
        field: str,
        function_id: str = ARITHMETIC_GUARDRAIL_ID,
    ) -> None:
        frontend = Frontend()
        host = FakeHost(secrets())
        self.run_bootstrap(frontend, host)
        function = next(item for item in frontend.functions if item["id"] == function_id)
        original_content = function["content"]
        mutate(function)
        frontend.swallow_sync = True
        report = self.run_bootstrap(frontend, host)
        self.assertFalse(report.ok)
        problem = report.problem or ""
        self.assertIn("functions sync", problem)
        self.assertIn(function_id, problem)
        self.assertIn(field, problem)
        self.assertNotIn(str(original_content), problem)
        self.assertIn("logs open-webui", report.fix)

    def test_swallowed_functions_sync_with_stale_content_refuses(self) -> None:
        self._assert_swallowed_function_correction_refuses(
            lambda function: function.update(content="hand-edited content"),
            "content",
        )

    def test_swallowed_functions_sync_with_inactive_row_refuses(self) -> None:
        self._assert_swallowed_function_correction_refuses(
            lambda function: function.update(is_active=False),
            "is_active",
        )

    def test_swallowed_functions_sync_with_non_global_row_refuses(self) -> None:
        self._assert_swallowed_function_correction_refuses(
            lambda function: function.update(is_global=False),
            "is_global",
        )

    def test_swallowed_functions_sync_with_stamp_global_row_refuses(self) -> None:
        self._assert_swallowed_function_correction_refuses(
            lambda function: function.update(is_global=True),
            "is_global",
            CITATION_STAMP_ID,
        )

    def test_swallowed_functions_sync_with_changed_description_refuses(self) -> None:
        def mutate(function: dict[str, Any]) -> None:
            meta = function["meta"]
            assert isinstance(meta, dict)
            meta["description"] = "hand-edited"

        self._assert_swallowed_function_correction_refuses(mutate, "meta.description")

    def test_swallowed_functions_sync_with_changed_type_refuses(self) -> None:
        self._assert_swallowed_function_correction_refuses(
            lambda function: function.update(type="pipe"),
            "type",
        )

    def test_swallowed_functions_sync_with_valves_refuses(self) -> None:
        self._assert_swallowed_function_correction_refuses(
            lambda function: function.update(valves={"unexpected": "value"}),
            "valves",
        )

    def test_ldap_owned_memberships_are_untouched_and_manifest_owned_ones_are_diffed(self) -> None:
        frontend = Frontend()
        host = FakeHost(secrets())
        self.run_bootstrap(frontend, host)
        names = {group["name"]: group["id"] for group in frontend.groups.values()}
        frontend.members[names["GIDEON-Users"]].add("u-admin")
        frontend.members[names[SERVICE_GROUP]].add("u-admin")
        report = self.run_bootstrap(frontend, host)
        self.assertTrue(report.ok, report.problem)
        self.assertIn("u-admin", frontend.members[names["GIDEON-Users"]])
        self.assertNotIn("u-admin", frontend.members[names[SERVICE_GROUP]])

    def test_drifted_permissions_are_pushed_back(self) -> None:
        frontend = Frontend()
        host = FakeHost(secrets())
        self.run_bootstrap(frontend, host)
        users_id = next(group["id"] for group in frontend.groups.values() if group["name"] == "GIDEON-Users")
        frontend.groups[users_id]["permissions"]["features"]["api_keys"] = True
        report = self.run_bootstrap(frontend, host)
        self.assertEqual(report.updated_groups, ("GIDEON-Users",))
        self.assertFalse(frontend.groups[users_id]["permissions"]["features"]["api_keys"])

    def test_existing_keys_are_never_reminted(self) -> None:
        frontend = Frontend()
        frontend.keys["u-admin"] = ADMIN_KEY
        host = FakeHost(secrets(admin_key=True))
        report = self.run_bootstrap(frontend, host)
        self.assertTrue(report.ok, report.problem)
        self.assertEqual(report.minted, ("gideon_eval_api_key",))
        self.assertEqual(host.files[f"{SECRETS_DIR}/gideon_admin_api_key"], ADMIN_KEY + "\n")

    def test_missing_break_glass_password_refuses_with_its_path(self) -> None:
        files = secrets()
        del files[f"{SECRETS_DIR}/gideon_admin_password"]
        report = self.run_bootstrap(Frontend(), FakeHost(files))
        self.assertFalse(report.ok)
        self.assertIn("gideon_admin_password", report.problem or "")
        self.assertNotIn(ADMIN_PASSWORD, report.problem or "")

    def test_server_failure_names_the_step_and_the_logs_fix_without_the_key(self) -> None:
        frontend = Frontend()
        original = frontend.handle

        def failing(method: str, path: str, body: object | None, credential: str | None) -> Response:
            if path == "/api/v1/groups/create":
                return Response(500, {"detail": "boom"})
            return original(method, path, body, credential)

        frontend.handle = failing  # type: ignore[method-assign]
        report = self.run_bootstrap(frontend, FakeHost(secrets()))
        self.assertFalse(report.ok)
        self.assertIn("group GIDEON-Users", report.problem or "")
        self.assertIn("HTTP 500", report.problem or "")
        self.assertIn("logs open-webui", report.fix)
        self.assertNotIn("sk-", report.problem or "")


class Manifest(unittest.TestCase):
    def test_load_manifest_reads_through_the_seam(self) -> None:
        host = FakeHost({f"{RENDERED}/open-webui/manifest.yaml": MANIFEST_PATH.read_text()})
        document = load_manifest(host, f"{RENDERED}/open-webui/manifest.yaml")
        functions = document["functions"]
        assert isinstance(functions, list)
        self.assertEqual(
            [function["id"] for function in functions],
            [ARITHMETIC_GUARDRAIL_ID, CITATION_STAMP_ID],
        )
        groups = document["groups"]
        assert isinstance(groups, list)
        self.assertEqual([group["name"] for group in groups], ["GIDEON-Users", "GIDEON-Admins", SERVICE_GROUP])

    def test_unreadable_manifest_raises_value_error_naming_it(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            load_manifest(FakeHost({}), f"{RENDERED}/open-webui/manifest.yaml")
        self.assertIn("manifest.yaml", str(ctx.exception))
