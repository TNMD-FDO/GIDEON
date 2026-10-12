"""The mounted model-server guard's ASGI behaviour and import boundary."""

import ast
import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from gideon.modelserver.guard import KEY_VARIABLE, RouteGuard

MODULE_DIR = Path(__file__).resolve().parent.parent / "gideon" / "modelserver"
KEY = "fictional-key-for-guard-tests"
UNAUTHORIZED_BODY = b'{"error":"Unauthorized"}'
UNAUTHORIZED_HEADERS = [
    (b"content-type", b"application/json"),
    (b"content-length", str(len(UNAUTHORIZED_BODY)).encode("ascii")),
    (b"www-authenticate", b"Bearer"),
]


class RecordingApp:
    def __init__(self) -> None:
        self.scopes: list[dict] = []

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        self.scopes.append(scope)


def call_guard(guard: RouteGuard, scope: dict) -> list[dict]:
    messages: list[dict] = []

    async def receive() -> dict:
        raise AssertionError("the guard must not read the request body")

    async def send(message: dict) -> None:
        messages.append(message)

    asyncio.run(guard(scope, receive, send))
    return messages


def http_scope(path: str, *, headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {"type": "http", "path": path, "method": "GET", "headers": headers or []}


class RouteDecisions(unittest.TestCase):
    """Only the two probes bypass the server's bearer-key gate."""

    def setUp(self) -> None:
        self.environment = patch.dict(os.environ, {KEY_VARIABLE: KEY})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.app = RecordingApp()
        self.guard = RouteGuard(self.app)

    def assert_http_refusal(self, scope: dict) -> None:
        called = list(self.app.scopes)
        self.assertEqual(
            call_guard(self.guard, scope),
            [
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": UNAUTHORIZED_HEADERS,
                },
                {"type": "http.response.body", "body": UNAUTHORIZED_BODY},
            ],
        )
        self.assertEqual(self.app.scopes, called)

    def test_routes_refuse_missing_and_wrong_keys(self) -> None:
        paths = (
            "/pooling",
            "/score",
            "/rerank",
            "/invocations",
            "/tokenize",
            "/v1/embeddings",
            "/v2/rerank",
            "/ping",
            "/v1/models",
            "/nothing",
            "/healthz",
            "/health/",
        )
        for path in paths:
            with self.subTest(path=path, key="missing"):
                self.assert_http_refusal(http_scope(path))
            with self.subTest(path=path, key="wrong"):
                self.assert_http_refusal(
                    http_scope(path, headers=[(b"Authorization", b"Bearer wrong-key")])
                )

    def test_probes_pass_without_a_key(self) -> None:
        for path in ("/health", "/metrics"):
            with self.subTest(path=path):
                scope = http_scope(path)
                self.assertEqual(call_guard(self.guard, scope), [])
                self.assertEqual(self.app.scopes[-1], scope)
        self.assertEqual(len(self.app.scopes), 2)

    def test_keyed_routes_pass_with_case_insensitive_header_and_scheme(self) -> None:
        for path in ("/tokenize", "/v1/embeddings"):
            with self.subTest(path=path):
                scope = http_scope(
                    path, headers=[(b"AUTHORIZATION", b"bEaReR " + KEY.encode())]
                )
                self.assertEqual(call_guard(self.guard, scope), [])
                self.assertEqual(self.app.scopes[-1], scope)
        self.assertEqual(len(self.app.scopes), 2)

    def test_options_and_lifespan_pass(self) -> None:
        options = http_scope("/tokenize")
        options["method"] = "OPTIONS"
        lifespan = {"type": "lifespan"}
        for scope in (options, lifespan):
            with self.subTest(scope=scope):
                self.assertEqual(call_guard(self.guard, scope), [])
        self.assertEqual(self.app.scopes, [options, lifespan])

    def test_root_path_is_removed_before_exact_probe_match(self) -> None:
        open_scope = http_scope("/prefix/health")
        open_scope["root_path"] = "/prefix"
        self.assertEqual(call_guard(self.guard, open_scope), [])
        self.assertEqual(self.app.scopes, [open_scope])

        closed_scope = http_scope("/prefix/health/")
        closed_scope["root_path"] = "/prefix"
        self.assert_http_refusal(closed_scope)
        self.assertEqual(self.app.scopes, [open_scope])

    def test_websocket_refusal_uses_denial_response_when_supported(self) -> None:
        scope = {
            "type": "websocket",
            "path": "/tokenize",
            "headers": [],
            "extensions": {"websocket.http.response": {}},
        }
        self.assertEqual(
            call_guard(self.guard, scope),
            [
                {
                    "type": "websocket.http.response.start",
                    "status": 401,
                    "headers": UNAUTHORIZED_HEADERS,
                },
                {"type": "websocket.http.response.body", "body": UNAUTHORIZED_BODY},
            ],
        )
        self.assertEqual(self.app.scopes, [])

    def test_websocket_refusal_closes_when_denial_response_is_unavailable(self) -> None:
        scope = {"type": "websocket", "path": "/tokenize", "headers": []}
        self.assertEqual(call_guard(self.guard, scope), [{"type": "websocket.close", "code": 1008}])
        self.assertEqual(self.app.scopes, [])


class Construction(unittest.TestCase):
    """A model server without a usable key refuses to start."""

    def test_missing_and_empty_variable_refuse_without_exposing_values(self) -> None:
        for environment in ({}, {KEY_VARIABLE: ""}):
            with self.subTest(environment=environment):
                with (
                    patch.dict(os.environ, environment, clear=True),
                    self.assertRaises(RuntimeError) as raised,
                ):
                    RouteGuard(RecordingApp())
                self.assertIn(KEY_VARIABLE, str(raised.exception))
                self.assertNotIn(KEY, str(raised.exception))


class ImportBoundary(unittest.TestCase):
    """The mounted copy imports only the standard library."""

    def test_every_modelserver_import_is_absolute_and_standard_library(self) -> None:
        files = sorted(MODULE_DIR.rglob("*.py"))
        self.assertTrue(files)
        for file in files:
            with self.subTest(file=file.name):
                tree = ast.parse(file.read_text(), filename=str(file))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        self.assertEqual(node.level, 0, f"{file}:{node.lineno}")
                        names = [node.module or ""]
                    else:
                        continue
                    for name in names:
                        with self.subTest(file=file.name, import_name=name):
                            self.assertIn(name.partition(".")[0], sys.stdlib_module_names)
