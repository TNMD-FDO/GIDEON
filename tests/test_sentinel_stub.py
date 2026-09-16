"""Hosted-tier contracts for the search sentinel's standard-library stub."""

import ast
import contextlib
import http.client
import importlib.util
import io
import json
import sys
import threading
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STUB_PATH = ROOT / "tests/contract/sentinel/stub.py"


def load_stub() -> Any:
    spec = importlib.util.spec_from_file_location("search_sentinel_stub", STUB_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


STUB = load_stub()

RESPONSE_START_MARKER = "<stdout has no getvalue>"


class RecordingStubHandler(STUB.StubHandler):  # type: ignore[name-defined]
    response_start_log: str = RESPONSE_START_MARKER

    def send_response(self, code: int, message: str | None = None) -> None:
        getvalue = getattr(sys.stdout, "getvalue", None)
        type(self).response_start_log = (
            getvalue() if getvalue is not None else RESPONSE_START_MARKER
        )
        super().send_response(code, message)


class StubServerTests(unittest.TestCase):
    server: Any
    thread: threading.Thread
    port: int

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = STUB.ThreadingHTTPServer(("127.0.0.1", 0), RecordingStubHandler)
        cls.port = int(cls.server.server_address[1])
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self) -> None:
        STUB.MODE = "ok"
        STUB.SENTINEL = "fixture-sentinel"
        STUB.MODEL_ID = "fixture-model"
        RecordingStubHandler.response_start_log = RESPONSE_START_MARKER

    def request(
        self, method: str, path: str, body: object | None = None
    ) -> tuple[int, dict[str, str], bytes, str]:
        """Return the response and redirected log.

        The log is read the instant the response has been read, which is exact
        because the stub writes the line before its first response byte.
        """

        encoded = None
        headers: dict[str, str] = {}
        if body is not None:
            encoded = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(encoded))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            result = (
                response.status,
                {name.lower(): value for name, value in response.getheaders()},
                response.read(),
            )
            connection.close()
        return (*result, output.getvalue())

    def test_request_line_is_written_before_the_response(self) -> None:
        requests = (
            ("GET", "/health", None),
            (
                "POST",
                "/v1/chat/completions",
                {"model": STUB.MODEL_ID, "stream": True},
            ),
        )
        for method, path, body in requests:
            with self.subTest(method=method, path=path):
                _status, _headers, _body, log = self.request(method, path, body)
                self.assertEqual(RecordingStubHandler.response_start_log, log)
                self.assertTrue(log.endswith("\n"))

    def test_health_and_models_have_openai_shape_in_both_modes(self) -> None:
        for mode in ("ok", "engine-error"):
            with self.subTest(mode=mode):
                STUB.MODE = mode
                status, _headers, body, _log = self.request("GET", "/health")
                self.assertEqual((status, body), (200, b"ok\n"))
                status, _headers, body, _log = self.request("GET", "/v1/models")
                self.assertEqual(status, 200)
                document = json.loads(body)
                self.assertEqual(document["object"], "list")
                self.assertEqual(document["data"][0]["id"], STUB.MODEL_ID)

    def test_chat_completions_return_the_query_object_in_both_modes(self) -> None:
        for mode in ("ok", "engine-error"):
            with self.subTest(mode=mode):
                STUB.MODE = mode
                status, _headers, body, _log = self.request(
                    "POST",
                    "/v1/chat/completions",
                    {"model": STUB.MODEL_ID, "stream": False},
                )
                self.assertEqual(status, 200)
                message = json.loads(body)["choices"][0]["message"]
                self.assertEqual(
                    json.loads(message["content"]), {"queries": [STUB.SENTINEL]}
                )

    def test_streamed_completion_has_deltas_finish_and_done_in_both_modes(self) -> None:
        for mode in ("ok", "engine-error"):
            with self.subTest(mode=mode):
                STUB.MODE = mode
                status, headers, body, _log = self.request(
                    "POST",
                    "/v1/chat/completions",
                    {"model": STUB.MODEL_ID, "stream": True},
                )
                self.assertEqual(status, 200)
                self.assertTrue(headers["content-type"].startswith("text/event-stream"))
                events = body.decode().split("\n\n")
                self.assertEqual(events[-2], "data: [DONE]")
                chunks = [json.loads(event.removeprefix("data: ")) for event in events[:-2]]
                self.assertEqual(chunks[1]["choices"][0]["delta"]["content"], "Stub completion.")
                self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")

    def test_search_engines_and_pages_have_their_fixed_shapes_in_both_modes(self) -> None:
        for mode in ("ok", "engine-error"):
            with self.subTest(mode=mode):
                STUB.MODE = mode
                status, _headers, body, _log = self.request("GET", "/json?q=fixture-sentinel")
                self.assertEqual(status, 200)
                results = json.loads(body)
                self.assertEqual(len(results), 3)
                self.assertEqual(
                    [result["url"] for result in results],
                    [f"{STUB.PAGE_ORIGIN}/page/{number}" for number in range(1, 4)],
                )
                self.assertRegex(STUB.PAGE_ORIGIN, r"^http://[a-z]+(\.[a-z]+)+:8000$")
                self.assertNotIn(STUB.SENTINEL.encode(), body)

                status, _headers, body, _log = self.request("GET", "/search?q=fixture-sentinel")
                self.assertEqual(status, 200 if mode == "ok" else 500)
                if mode == "ok":
                    self.assertEqual(body, b"<html><body></body></html>")
                else:
                    self.assertEqual(body, b"stub engine error\n")

                status, _headers, body, _log = self.request("GET", "/searxng/search")
                self.assertEqual((status, body), (500, b"stub SearXNG failure\n"))
                status, _headers, body, _log = self.request("GET", "/page/2")
                self.assertEqual(status, 200)
                self.assertIn(b"<title>Stub page 2</title>", body)
                self.assertIn(b"<p>Stub page 2.</p>", body)

    def test_request_line_is_content_free_and_marks_only_matching_q(self) -> None:
        _status, _headers, _body, log = self.request("GET", "/json?q=fixture-sentinel")
        self.assertEqual(log.strip(), "GET /json status=200 sentinel=yes")
        self.assertNotIn("?", log)
        self.assertNotIn(STUB.SENTINEL, log)

        _status, _headers, _body, log = self.request(
            "GET", "/json?q=private%20query"
        )
        self.assertEqual(log.strip(), "GET /json status=200 sentinel=no")
        self.assertNotIn("private query", log)

        _status, _headers, _body, log = self.request(
            "POST",
            "/v1/chat/completions",
            {"messages": [{"content": STUB.SENTINEL}], "stream": False},
        )
        self.assertEqual(log.strip(), "POST /v1/chat/completions status=200 sentinel=no")
        self.assertNotIn(STUB.SENTINEL, log)

    def test_unknown_path_is_a_logged_404(self) -> None:
        status, _headers, body, log = self.request("GET", "/unknown?q=fixture-sentinel")
        self.assertEqual((status, body), (404, b"not found\n"))
        self.assertEqual(log.strip(), "GET /unknown status=404 sentinel=yes")
        self.assertNotIn(STUB.SENTINEL, log)


class StandardLibraryOnly(unittest.TestCase):
    def test_stub_imports_only_the_declared_standard_library_modules(self) -> None:
        tree = ast.parse(STUB_PATH.read_text(), filename=str(STUB_PATH))
        allowed = {"http.server", "json", "os", "sys", "time", "urllib.parse"}
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module
                self.assertIn(module, {"http.server", "urllib.parse"})
                if module is not None:
                    imports.add(module)
        self.assertEqual(imports, allowed)


if __name__ == "__main__":
    unittest.main()
