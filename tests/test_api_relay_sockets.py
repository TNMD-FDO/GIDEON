"""Real-socket contracts for the completion relay's flushing and disconnects."""

import http.client
import json
import select
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

import uvicorn

from gideon import guardrail
from gideon.api.app import create_app
from gideon.api.settings import Settings

API_KEY = "fixture-api-key"
ENGINE_KEY = "fixture-engine-key"
_WAIT_SECONDS = 3.0
_POLL_SECONDS = 0.05
_STREAM_CONTENT_TYPE = "text/event-stream; charset=utf-8"
_STREAM_ENVELOPE: dict[str, object] = {
    "id": "fixture-stream",
    "object": "chat.completion.chunk",
    "created": 1_700_000_000,
    "model": "fixture-model",
}
_STREAM_CONTENT_DELTAS = (
    "This neutral socket fixture explains a fictional record without calculating anything. "
    * 8,
    "This second socket fixture paragraph remains descriptive and safe. " * 8,
)
_STREAM_ANSWER = "".join(_STREAM_CONTENT_DELTAS)


def _chunk_event(
    delta: dict[str, object], *, finish_reason: str | None = None
) -> bytes:
    event = dict(_STREAM_ENVELOPE)
    event["choices"] = [
        {
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }
    ]
    return (
        b"data: "
        + json.dumps(event, separators=(",", ":")).encode("utf-8")
        + b"\n\n"
    )


def _usage_event() -> bytes:
    event = dict(_STREAM_ENVELOPE)
    event["choices"] = []
    event["usage"] = {"prompt_tokens": 3, "completion_tokens": 4}
    return (
        b"data: "
        + json.dumps(event, separators=(",", ":")).encode("utf-8")
        + b"\n\n"
    )


_STREAM_EVENTS = (
    _chunk_event({"role": "assistant"}),
    _chunk_event({"content": _STREAM_CONTENT_DELTAS[0]}),
    _chunk_event({"content": _STREAM_CONTENT_DELTAS[1]}),
    _chunk_event({}, finish_reason="stop"),
    _usage_event(),
    b"data: [DONE]\n\n",
)
# The trailing padding matters: the window decides a sentence only once enough
# text has arrived behind it, so without it this stream would trip at a finish
# chunk the stub never sends.
_TRIP_PADDING = "Neutral socket fixture text. " * 20
_TRIP_EVENTS = (
    _chunk_event({"role": "assistant"}),
    _chunk_event({"content": _TRIP_PADDING}),
    _chunk_event({"content": "The deadline is June 5, 2027."}),
    _chunk_event({"content": _TRIP_PADDING}),
)


def _parse_event(body: bytes) -> dict[str, object] | str:
    payload = body.removeprefix(b"data: ").removesuffix(b"\n\n")
    if payload == b"[DONE]":
        return "[DONE]"
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise AssertionError("fixture event is not an object")
    return parsed


def _event_content(event: dict[str, object] | str) -> str:
    if isinstance(event, str):
        return ""
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, dict):
        return ""
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        return ""
    content = delta.get("content")
    return content if isinstance(content, str) else ""


def _read_event(response: http.client.HTTPResponse) -> dict[str, object] | str:
    body = bytearray()
    while not body.endswith(b"\n\n"):
        body.extend(response.read(1))
    return _parse_event(bytes(body))


def _read_events(body: bytes) -> list[dict[str, object] | str]:
    return [_parse_event(part + b"\n\n") for part in body.split(b"\n\n") if part]


def _state_event(state: dict[str, object], name: str) -> threading.Event:
    return cast(threading.Event, state[name])


class StubHandler(BaseHTTPRequestHandler):
    """Serve the engine's model and completion endpoints over a test socket."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    @property
    def state(self) -> dict[str, object]:
        return cast(dict[str, object], cast(Any, self.server).fixture_state)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _write_chunk(self, body: bytes) -> None:
        self.wfile.write(f"{len(body):x}\r\n".encode("ascii"))
        self.wfile.write(body)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _peer_closed(self) -> bool:
        readable, _, _ = select.select([self.connection], [], [], 0)
        if not readable:
            return False
        try:
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def _wait_for_second_event(self) -> bool:
        deadline = time.monotonic() + _WAIT_SECONDS
        gate = _state_event(self.state, "second_event")
        while time.monotonic() < deadline:
            if gate.wait(_POLL_SECONDS):
                return True
            if self._peer_closed():
                _state_event(self.state, "peer_closed").set()
                return False
        _state_event(self.state, "stream_timed_out").set()
        return False

    def _wait_for_peer_closed(self) -> bool:
        deadline = time.monotonic() + _WAIT_SECONDS
        while time.monotonic() < deadline:
            if self._peer_closed():
                _state_event(self.state, "peer_closed").set()
                return True
            time.sleep(_POLL_SECONDS)
        _state_event(self.state, "stream_timed_out").set()
        return False

    def _stream_completion(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", _STREAM_CONTENT_TYPE)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self._write_chunk(_STREAM_EVENTS[0])
            _state_event(self.state, "first_event").set()
            if not self._wait_for_second_event():
                return
            for event in _STREAM_EVENTS[1:]:
                self._write_chunk(event)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            _state_event(self.state, "peer_closed").set()
        finally:
            _state_event(self.state, "stream_finished").set()

    def _stream_trip_completion(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", _STREAM_CONTENT_TYPE)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self._write_chunk(_TRIP_EVENTS[0])
            _state_event(self.state, "first_event").set()
            if not self._wait_for_second_event():
                return
            for event in _TRIP_EVENTS[1:]:
                self._write_chunk(event)
            self._wait_for_peer_closed()
        except OSError:
            _state_event(self.state, "peer_closed").set()
        finally:
            _state_event(self.state, "stream_finished").set()

    def _whole_completion(self) -> None:
        body = json.dumps(
            {
                "id": "fixture-whole",
                "object": "chat.completion",
                "choices": [],
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self._send(200, body, "application/json")

    def do_GET(self) -> None:
        if self.path == "/v1/models":
            body = json.dumps(
                {
                    "object": "list",
                    "data": [{"id": "fixture-model", "object": "model"}],
                },
                separators=(",", ":"),
            ).encode("utf-8")
            self._send(200, body, "application/json")
            return
        self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        if self.path != "/v1/chat/completions" or not isinstance(body, dict):
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return
        if body.get("stream") is True:
            if body.get("trip") is True:
                self._stream_trip_completion()
            else:
                self._stream_completion()
        else:
            self._whole_completion()


class StartedServer(uvicorn.Server):
    """Signal after uvicorn has bound its ephemeral listening socket."""

    def __init__(self, config: uvicorn.Config, started: threading.Event) -> None:
        super().__init__(config)
        self.started_event = started

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        self.started_event.set()


class ApiRelaySockets(unittest.TestCase):
    """Exercise the relay through uvicorn and real loopback sockets."""

    def setUp(self) -> None:
        self.stub_state: dict[str, object] = {
            name: threading.Event()
            for name in (
                "first_event",
                "second_event",
                "peer_closed",
                "stream_finished",
                "stream_timed_out",
                "stub_finished",
                "service_started",
                "service_finished",
            )
        }
        self.stub_server = cast(Any, ThreadingHTTPServer(("127.0.0.1", 0), StubHandler))
        self.stub_server.fixture_state = self.stub_state
        self.stub_port = self.stub_server.server_address[1]
        self.stub_thread = threading.Thread(target=self._serve_stub, name="fixture-engine")
        self.stub_thread.start()

        self.service_started = _state_event(self.stub_state, "service_started")
        settings = Settings(
            f"http://127.0.0.1:{self.stub_port}/v1",
            ENGINE_KEY,
            API_KEY,
            0,
        )
        self.service_server = StartedServer(
            uvicorn.Config(
                create_app(settings),
                host="127.0.0.1",
                port=0,
                lifespan="on",
                log_config=None,
                access_log=False,
            ),
            self.service_started,
        )
        self.service_thread = threading.Thread(
            target=self._serve_service, name="fixture-api"
        )
        self.service_thread.start()
        self.assertTrue(self.service_started.wait(_WAIT_SECONDS))
        servers = cast(list[Any], self.service_server.servers)
        self.assertTrue(servers)
        self.service_port = servers[0].sockets[0].getsockname()[1]

    def tearDown(self) -> None:
        _state_event(self.stub_state, "second_event").set()
        if _state_event(self.stub_state, "first_event").is_set():
            # Release and then outlive the handler thread, which the threading
            # server's daemon threads would otherwise leave mid-stream.
            self.assertTrue(
                _state_event(self.stub_state, "stream_finished").wait(_WAIT_SECONDS)
            )
        self.service_server.should_exit = True
        self.assertTrue(_state_event(self.stub_state, "service_finished").wait(_WAIT_SECONDS))
        self.service_thread.join()
        self.assertFalse(self.service_thread.is_alive())

        self.stub_server.shutdown()
        self.assertTrue(_state_event(self.stub_state, "stub_finished").wait(_WAIT_SECONDS))
        self.stub_thread.join()
        self.assertFalse(self.stub_thread.is_alive())
        self.stub_server.server_close()

    def _serve_stub(self) -> None:
        try:
            self.stub_server.serve_forever()
        finally:
            _state_event(self.stub_state, "stub_finished").set()

    def _serve_service(self) -> None:
        try:
            self.service_server.run()
        finally:
            _state_event(self.stub_state, "service_finished").set()

    def _connection(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.service_port, timeout=_WAIT_SECONDS)

    def _stream_request(self) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        connection = self._connection()
        body = json.dumps(
            {"stream": True, "messages": [{"content": "fixture prompt"}]},
            separators=(",", ":"),
        ).encode("utf-8")
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=body,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            },
        )
        return connection, connection.getresponse()

    def test_stream_flushes_first_event_before_second_gate(self) -> None:
        connection, response = self._stream_request()
        try:
            first = _read_event(response)
            self.assertEqual(first, _parse_event(_STREAM_EVENTS[0]))
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), _STREAM_CONTENT_TYPE)
            self.assertIsNone(response.getheader("Content-Length"))
            self.assertFalse(_state_event(self.stub_state, "second_event").is_set())

            _state_event(self.stub_state, "second_event").set()
            events = [first, *_read_events(response.read())]
            self.assertEqual(len(events), len(_STREAM_EVENTS))
            self.assertEqual(
                "".join(_event_content(event) for event in events), _STREAM_ANSWER
            )
            for event in events:
                if isinstance(event, dict):
                    # The usage chunk carries its own key beside the envelope.
                    expected = dict(_STREAM_ENVELOPE)
                    if event.get("choices") == []:
                        expected["usage"] = {"prompt_tokens": 3, "completion_tokens": 4}
                    self.assertEqual(
                        {key: value for key, value in event.items() if key != "choices"},
                        expected,
                    )
            finish = events[3]
            self.assertIsInstance(finish, dict)
            assert isinstance(finish, dict)
            finish_choices = finish["choices"]
            self.assertIsInstance(finish_choices, list)
            assert isinstance(finish_choices, list)
            self.assertEqual(finish_choices[0]["finish_reason"], "stop")
            self.assertEqual(events[4], _parse_event(_STREAM_EVENTS[4]))
            self.assertEqual(events[5], "[DONE]")
            self.assertTrue(_state_event(self.stub_state, "stream_finished").wait(_WAIT_SECONDS))
            self.assertFalse(_state_event(self.stub_state, "stream_timed_out").is_set())
        finally:
            connection.close()

    def test_client_close_reaches_stub_while_stream_is_gated(self) -> None:
        connection, response = self._stream_request()
        first = _read_event(response)
        self.assertEqual(first, _parse_event(_STREAM_EVENTS[0]))
        connection.close()

        self.assertTrue(_state_event(self.stub_state, "peer_closed").wait(_WAIT_SECONDS))
        self.assertFalse(_state_event(self.stub_state, "second_event").is_set())
        self.assertFalse(_state_event(self.stub_state, "stream_timed_out").is_set())

    def test_trip_closes_engine_before_client_reads_refusal(self) -> None:
        connection = self._connection()
        body = json.dumps(
            {"stream": True, "trip": True, "messages": [{"content": "fixture prompt"}]},
            separators=(",", ":"),
        ).encode("utf-8")
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=body,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        try:
            first = _read_event(response)
            self.assertEqual(first, _parse_event(_TRIP_EVENTS[0]))
            _state_event(self.stub_state, "second_event").set()
            # The engine's connection ends while the refusal is still unread by
            # this client: the fall is the service's close, never the caller's.
            self.assertTrue(_state_event(self.stub_state, "peer_closed").wait(_WAIT_SECONDS))
            self.assertFalse(_state_event(self.stub_state, "stream_timed_out").is_set())

            events = [first, *_read_events(response.read())]
            self.assertEqual(events[-1], "[DONE]")
            self.assertIn(
                guardrail.DEADLINE_FAMILY.refusal,
                "".join(_event_content(event) for event in events),
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
