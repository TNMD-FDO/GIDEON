"""Standard-library HTTP stub for the search sentinel's engine and web paths.

Compose healthchecks consume ``/health``; the frontend consumes ``/v1/models``
and ``/v1/chat/completions``; SearXNG consumes
``/json`` through its JSON engine and ``/search`` through the shipped Bing
module; the frontend's failure path uses ``/searxng/search``, and its loader
consumes ``/page/<n>``. Every request is logged without its query string so the
contract can inspect the request path without writing query text. The line is
written before the first response byte, so a reader holding the stream until
the response is read has the line.
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PORT = 8000
# The origin the result pages are linked under: the frontend's loader validates
# a result URL with a library that rejects a hostname without a dot, so the
# pages use the stub's dotted Compose alias (declared in compose.yaml), while
# the engine and SearXNG reach the same server as "stub".
PAGE_ORIGIN = "http://stub.sentinel.test:8000"
MODE = os.environ.get("STUB_MODE", "ok")
SENTINEL = os.environ.get("STUB_SENTINEL", "stub-sentinel")
MODEL_ID = os.environ.get("STUB_MODEL_ID", "stub-model")
_STREAM_SENTENCE = "Stub completion."
_NOT_FOUND = b"not found\n"


def _route_and_query(path: str) -> tuple[str, dict[str, str]]:
    """Split a request target into its path and its first value per query name."""

    parts = urlsplit(path)
    query = parse_qs(parts.query, keep_blank_values=True)
    return parts.path, {name: values[0] for name, values in query.items()}


class StubHandler(BaseHTTPRequestHandler):
    """Serve the deterministic engine, SearXNG, and page endpoints."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        """Suppress BaseHTTPRequestHandler's default request logging."""

        del format, args

    def _log_request(self, route: str, query: dict[str, str], status: int) -> None:
        marker = "yes" if query.get("q") == SENTINEL else "no"
        sys.stdout.write(f"{self.command} {route} status={status} sentinel={marker}\n")
        sys.stdout.flush()

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _send_logged(
        self, route: str, query: dict[str, str], status: int, body: bytes, content_type: str
    ) -> None:
        self._log_request(route, query, status)
        self._send(status, body, content_type)

    def _completion_event(self, event: dict[str, object]) -> bytes:
        return b"data: " + json.dumps(event, separators=(",", ":")).encode() + b"\n\n"

    def _stream_completion(self, route: str, query: dict[str, str]) -> None:
        created = int(time.time())
        self._log_request(route, query, 200)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for event in (
            {
                "id": "chatcmpl-stub",
                "object": "chat.completion.chunk",
                "created": created,
                "model": MODEL_ID,
                "choices": [
                    {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
                ],
            },
            {
                "id": "chatcmpl-stub",
                "object": "chat.completion.chunk",
                "created": created,
                "model": MODEL_ID,
                "choices": [
                    {"index": 0, "delta": {"content": _STREAM_SENTENCE}, "finish_reason": None}
                ],
            },
            {
                "id": "chatcmpl-stub",
                "object": "chat.completion.chunk",
                "created": created,
                "model": MODEL_ID,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ):
            self.wfile.write(self._completion_event(event))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_GET(self) -> None:
        route, query = _route_and_query(self.path)
        if route == "/health":
            self._send_logged(route, query, 200, b"ok\n", "text/plain; charset=utf-8")
        elif route == "/v1/models":
            body = json.dumps(
                {
                    "object": "list",
                    "data": [{"id": MODEL_ID, "object": "model", "owned_by": "stub"}],
                },
                separators=(",", ":"),
            ).encode()
            self._send_logged(route, query, 200, body, "application/json")
        elif route == "/json":
            body = json.dumps(
                [
                    {
                        "url": f"{PAGE_ORIGIN}/page/{number}",
                        "title": f"Stub page {number}",
                        "content": f"Stub page {number} content.",
                    }
                    for number in range(1, 4)
                ],
                separators=(",", ":"),
            ).encode()
            self._send_logged(route, query, 200, body, "application/json")
        elif route == "/search":
            if MODE == "engine-error":
                self._send_logged(
                    route, query, 500, b"stub engine error\n", "text/plain; charset=utf-8"
                )
            else:
                self._send_logged(
                    route,
                    query,
                    200,
                    b"<html><body></body></html>",
                    "text/html; charset=utf-8",
                )
        elif route == "/searxng/search":
            self._send_logged(
                route, query, 500, b"stub SearXNG failure\n", "text/plain; charset=utf-8"
            )
        elif route.startswith("/page/") and route != "/page/":
            page = route.removeprefix("/page/")
            body = (
                f"<html><head><title>Stub page {page}</title></head>"
                f"<body><p>Stub page {page}.</p></body></html>"
            ).encode()
            self._send_logged(route, query, 200, body, "text/html; charset=utf-8")
        else:
            self._send_logged(route, query, 404, _NOT_FOUND, "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        route, query = _route_and_query(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            self._send_logged(
                route, query, 400, b"invalid JSON\n", "text/plain; charset=utf-8"
            )
            return
        if route != "/v1/chat/completions" or not isinstance(body, dict):
            self._send_logged(route, query, 404, _NOT_FOUND, "text/plain; charset=utf-8")
            return
        if body.get("stream") is True:
            self._stream_completion(route, query)
            return
        response = {
            "id": "chatcmpl-stub",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": MODEL_ID,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({"queries": [SENTINEL]}, separators=(",", ":")),
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        self._send_logged(
            route,
            query,
            200,
            json.dumps(response, separators=(",", ":")).encode(),
            "application/json",
        )


def main() -> None:
    """Run the stub on its fixed container port."""

    server = ThreadingHTTPServer(("0.0.0.0", PORT), StubHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
