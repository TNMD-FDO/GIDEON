"""In-process ASGI-level contracts for the streamed and whole completion relay."""

import asyncio
import contextlib
import json
import logging
import sys
import tempfile
import threading
import types
import unittest
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import patch

import httpx
from starlette.types import Message, Scope

from gideon import guardrail
from gideon.api import judged, stamp
from gideon.api.app import create_app
from gideon.api.relay import UPSTREAM_ERROR
from gideon.api.settings import Settings
from gideon.api.sse import DONE_EVENT
from gideon.api.upstream import (
    UPSTREAM_COMPLETION_READ_TIMEOUT_SECONDS,
    UPSTREAM_READ_TIMEOUT_SECONDS,
)
from gideon.host.render.api import (
    API_USER_EMAIL_HEADER,
    API_USER_NAME_HEADER,
    API_USER_ROLE_HEADER,
)

API_KEY = "fixture-api-key"
ENGINE_KEY = "fixture-engine-key"
ENGINE_URL = "http://fixture-engine/v1"
SOURCE_HEADER = "X-Fixture-Source"
EVAL_IDENTITY = "eval@example.invalid"
_ASGI_WAIT_SECONDS = 1.0
# The application task has already finished wherever this bound is used, so a
# message that has not arrived by now never will.
_SILENCE_SECONDS = 0.05
# This fake connect must outlast ASGISession's per-message and application
# bounds. It is finite because a synchronous dispatch freezes the event loop
# until the fake connect returns, so no other timeout can end that wait.
_WRITER_BLOCK_SECONDS = _ASGI_WAIT_SECONDS * 10
_STREAM_ENVELOPE: dict[str, object] = {
    "id": "fixture-stream",
    "object": "chat.completion.chunk",
    "created": 1_700_000_000,
    "model": "fixture-model",
}
_STREAM_CONTENT_DELTAS = (
    "This neutral fixture paragraph explains a fictional record without calculating anything. "
    * 8,
    "This second neutral fixture paragraph keeps the answer descriptive and safe. " * 8,
)
_STREAM_ANSWER = "".join(_STREAM_CONTENT_DELTAS)


def _chunk_event(
    delta: Mapping[str, object], *, finish_reason: str | None = None
) -> bytes:
    event = dict(_STREAM_ENVELOPE)
    event["choices"] = [
        {
            "index": 0,
            "delta": dict(delta),
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
_TRIP_PADDING = "Neutral fixture text. " * 20
_TRIP_ANSWER = "The deadline is June 5, 2027."
# The window decides a sentence only once enough text has arrived behind it, so
# the padding after the date is what makes this a trip mid-stream rather than
# one at a finish chunk. The two events after it are the ones the relay must
# never read: on a trip it stops reading and closes the engine.
_TRIP_UNREAD = "This held text must never reach the caller. " * 20
_TRIP_EVENTS = (
    _chunk_event({"role": "assistant"}),
    _chunk_event({"content": _TRIP_PADDING}),
    _chunk_event({"content": _TRIP_ANSWER}),
    _chunk_event({"content": _TRIP_PADDING}),
    _chunk_event({"content": _TRIP_UNREAD}),
    _chunk_event({}, finish_reason="stop"),
)
_TRIP_RELEASED = 4
_TRIP_PROMPT = "Explain a fictitious legal rule."
_TRIP_REQUEST_BODY = json.dumps(
    {
        "stream": True,
        "model": "fixture-model",
        "messages": [{"role": "user", "content": _TRIP_PROMPT}],
    },
    separators=(",", ":"),
).encode()
_WHOLE_TRIP_REQUEST_BODY = _TRIP_REQUEST_BODY.replace(b'"stream":true', b'"stream":false')
_TRIP_CHOICE = {
    "index": 0,
    "message": {"role": "assistant", "content": _TRIP_ANSWER},
    "finish_reason": "stop",
}
_SENTINEL_HEADERS = {
    API_USER_NAME_HEADER: "sentinel-name-7f3c",
    API_USER_EMAIL_HEADER: "sentinel-email-7f3c@example.invalid",
    API_USER_ROLE_HEADER: "sentinel-role-7f3c",
    "X-OpenWebUI-User-Id": "sentinel-user-id-7f3c",
    "X-OpenWebUI-Chat-Id": "sentinel-chat-id-7f3c",
}


def whole_completion(choices: Sequence[object]) -> bytes:
    """Build a compact fixture body with the choices under test."""

    return json.dumps(
        {
            "id": "fixture-whole",
            "object": "chat.completion",
            "created": 1_700_000_000,
            "model": "fixture-model",
            "choices": choices,
        },
        separators=(",", ":"),
    ).encode()


def _parse_event(body: bytes) -> dict[str, object] | str:
    payload = body.removeprefix(b"data: ").removesuffix(b"\n\n")
    if payload == DONE_EVENT.encode("utf-8"):
        return DONE_EVENT
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise AssertionError("fixture event is not an object")
    return parsed


def _first_choice(event: Mapping[str, object]) -> Mapping[str, object]:
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AssertionError("fixture event has no choice")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise AssertionError("fixture choice is not an object")
    return choice


def _event_content(event: dict[str, object] | str) -> str:
    if isinstance(event, str):
        return ""
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = _first_choice(event)
    delta = choice.get("delta")
    if not isinstance(delta, Mapping):
        return ""
    content = delta.get("content")
    return content if isinstance(content, str) else ""


def _event_envelope(event: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in event.items() if key != "choices"}


@dataclass(frozen=True, slots=True)
class ASGIResponse:
    """The two response messages emitted for a response sent in one piece."""

    status_code: int
    headers: dict[bytes, bytes]
    body: bytes


class FixtureStream(httpx.AsyncByteStream):
    """Yield a whole fixture body and record the response close."""

    def __init__(self, body: bytes, failure: httpx.HTTPError | None = None) -> None:
        self.body = body
        self.failure = failure
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.failure is not None:
            raise self.failure
        yield self.body

    async def aclose(self) -> None:
        self.closed = True


class GatedStream(httpx.AsyncByteStream):
    """Release one upstream chunk at a time and record the response close."""

    def __init__(
        self,
        chunks: tuple[bytes, ...],
        failure: httpx.HTTPError | None = None,
    ) -> None:
        self.chunks = chunks
        self.gates = tuple(asyncio.Event() for _ in chunks)
        self.failure = failure
        self.closed = False

    def release(self, index: int) -> None:
        self.gates[index].set()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index, chunk in enumerate(self.chunks):
            await asyncio.wait_for(self.gates[index].wait(), _ASGI_WAIT_SECONDS)
            yield chunk
        if self.failure is not None:
            raise self.failure

    async def aclose(self) -> None:
        self.closed = True


class TripConnection:
    """Record the writer's insert without making the API wait for it."""

    def __init__(
        self,
        rows: list[tuple[str, str, str, str]],
        row_written: threading.Event,
    ) -> None:
        self.rows = rows
        self.row_written = row_written

    def __enter__(self) -> "TripConnection":
        return self

    def __exit__(self, *args: object) -> Literal[False]:
        return False

    def execute(self, statement: str, parameters: object = None) -> None:
        del statement
        if parameters is None:
            return
        if (
            not isinstance(parameters, tuple)
            or len(parameters) != 4
            or not all(isinstance(value, str) for value in parameters)
        ):
            raise AssertionError("writer parameters are not one content-free row")
        self.rows.append(cast(tuple[str, str, str, str], parameters))
        self.row_written.set()


class RecordingThread(threading.Thread):
    """Record every writer thread started by one driver's patched seam."""

    def __init__(
        self,
        threads: list[threading.Thread],
        group: None = None,
        target: Callable[..., object] | None = None,
        name: str | None = None,
        args: tuple[object, ...] = (),
        kwargs: Mapping[str, object] | None = None,
        *,
        daemon: bool | None = None,
    ) -> None:
        self.threads = threads
        super().__init__(
            group=group,
            target=target,
            name=name,
            args=args,
            kwargs=kwargs,
            daemon=daemon,
        )

    def start(self) -> None:
        self.threads.append(self)
        super().start()


@contextlib.contextmanager
def trip_driver(connect: Callable[..., object]) -> Iterator[None]:
    """Install the driver and password file without outliving their patches."""

    with tempfile.TemporaryDirectory() as directory:
        password_path = Path(directory) / "password"
        password_path.write_text("fixture-trip-password\n", encoding="utf-8")
        driver = types.ModuleType(guardrail.TRIP_DRIVER_MODULE)
        driver.connect = connect  # type: ignore[attr-defined]
        threads: list[threading.Thread] = []
        body_failed = True
        with (
            patch.object(guardrail, "TRIP_PASSWORD_PATH", str(password_path)),
            patch.dict(sys.modules, {guardrail.TRIP_DRIVER_MODULE: driver}),
            patch.object(guardrail, "Thread", partial(RecordingThread, threads)),
        ):
            try:
                yield
                body_failed = False
            finally:
                for thread in threads:
                    thread.join(_ASGI_WAIT_SECONDS)
                alive = sum(thread.is_alive() for thread in threads)
                if not body_failed and alive:
                    raise AssertionError(
                        f"{alive} writer thread(s) still alive after "
                        f"{_ASGI_WAIT_SECONDS} seconds"
                    )


class CapturedLogs(logging.Handler):
    """Collect root and relay records at DEBUG without changing production logging."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextlib.contextmanager
def capture_logs() -> Iterator[CapturedLogs]:
    """Capture relay and root records, including DEBUG, for data-discipline checks."""

    handler = CapturedLogs()
    root = logging.getLogger()
    relay = logging.getLogger("gideon.api.relay")
    root_level = root.level
    relay_level = relay.level
    root.addHandler(handler)
    relay.addHandler(handler)
    root.setLevel(logging.DEBUG)
    relay.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        relay.removeHandler(handler)
        root.setLevel(root_level)
        relay.setLevel(relay_level)


def waiting_connect(
    started: threading.Event,
    released: threading.Event,
    finished: threading.Event,
) -> Callable[..., object]:
    """Return a bounded connect released after the answer and before the join."""

    def connect(**kwargs: object) -> TripConnection:
        del kwargs
        started.set()
        released.wait(_WRITER_BLOCK_SECONDS)
        finished.set()
        return TripConnection([], threading.Event())

    return connect


@contextlib.contextmanager
def signal_dispatch(started: threading.Event) -> Iterator[None]:
    """Signal the synchronous dispatch before preserving its daemon behavior."""

    original = guardrail.dispatch_trip_row

    def dispatch(row: guardrail.TripRow) -> None:
        started.set()
        original(row)

    with patch.object(guardrail, "dispatch_trip_row", side_effect=dispatch):
        yield


class PendingEngine:
    """Hold an upstream answer until cancellation and record that cancellation."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.answer = asyncio.Event()
        self.cancelled = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.started.set()
        try:
            await asyncio.wait_for(self.answer.wait(), _ASGI_WAIT_SECONDS)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return httpx.Response(200, content=b"fixture whole answer", request=request)


class ASGISession:
    """Drive one application request with bounded message-by-message waits."""

    def __init__(
        self,
        app: Any,
        method: str,
        path: str,
        body: bytes,
        headers: Mapping[str, str] | Sequence[tuple[str, str]] | None,
        spec_version: str,
    ) -> None:
        self.app = app
        self.incoming: asyncio.Queue[Message] = asyncio.Queue()
        self.incoming.put_nowait(
            {"type": "http.request", "body": body, "more_body": False}
        )
        self.outgoing: asyncio.Queue[Message] = asyncio.Queue()
        header_pairs = (
            headers.items()
            if isinstance(headers, Mapping)
            else () if headers is None else headers
        )
        self.scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": spec_version},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (name.lower().encode("ascii"), value.encode("utf-8"))
                for name, value in header_pairs
            ],
            "client": ("fixture-client", 1234),
            "server": ("fixture-api", 8000),
        }
        self.application: asyncio.Task[None]
        self.lifespan: Any

    async def __aenter__(self) -> "ASGISession":
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()

        async def receive() -> Message:
            return await asyncio.wait_for(self.incoming.get(), _ASGI_WAIT_SECONDS)

        async def send(message: Message) -> None:
            await self.outgoing.put(message)

        self.application = asyncio.create_task(self.app(self.scope, receive, send))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        try:
            await self.wait_for_application()
        finally:
            await self.lifespan.__aexit__(exc_type, exc_value, traceback)

    async def next_message(self, bound: float = _ASGI_WAIT_SECONDS) -> Message:
        return await asyncio.wait_for(self.outgoing.get(), bound)

    async def wait_for_application(self) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(self.application), _ASGI_WAIT_SECONDS)
        except TimeoutError:
            self.application.cancel()
            await asyncio.gather(self.application, return_exceptions=True)
            raise

    def disconnect(self) -> None:
        self.incoming.put_nowait({"type": "http.disconnect"})


class ApiCompletions(unittest.TestCase):
    # The header the service is configured to decide on. It is deliberately not
    # one of the five the frontend forwards, so every case here proves the relay
    # reads the configured name; the sentinel case sets it to the email header
    # the render configures in production, where a value is actually read.
    source_header: str = SOURCE_HEADER

    def settings(self) -> Settings:
        return Settings(
            ENGINE_URL, ENGINE_KEY, API_KEY, 8000, self.source_header, EVAL_IDENTITY
        )

    def request(
        self,
        handler: httpx.MockTransport,
        method: str = "POST",
        path: str = "/v1/chat/completions",
        *,
        body: bytes = b'{"messages":[]}',
        headers: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
        spec_version: str = "2.4",
    ) -> ASGIResponse:
        async def run() -> ASGIResponse:
            app = create_app(self.settings(), transport=handler)
            async with ASGISession(
                app, method, path, body, headers, spec_version
            ) as session:
                start = await session.next_message()
                response_body = await session.next_message()

            self.assertEqual(start["type"], "http.response.start")
            self.assertEqual(response_body["type"], "http.response.body")
            response_headers = dict(cast(list[tuple[bytes, bytes]], start["headers"]))
            return ASGIResponse(
                status_code=int(start["status"]),
                headers=response_headers,
                body=cast(bytes, response_body["body"]),
            )

        return asyncio.run(run())

    async def gated_stream(
        self,
        content_type: str,
        *,
        chunks: tuple[bytes, ...] = _STREAM_EVENTS,
        failure: httpx.HTTPError | None = None,
    ) -> tuple[Message, list[Message], GatedStream]:
        stream = GatedStream(chunks, failure)

        def engine(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": content_type},
                stream=stream,
                request=request,
            )

        app = create_app(self.settings(), transport=httpx.MockTransport(engine))
        async with ASGISession(
            app,
            "POST",
            "/v1/chat/completions",
            b'{"stream":true,"prompt":"fixture prompt secret"}',
            {
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
                "X-Fixture-Header": "fixture header secret",
            },
            "2.4",
        ) as session:
            start = await session.next_message()
            bodies: list[Message] = []
            for index in range(len(chunks)):
                stream.release(index)
                bodies.append(await session.next_message())
            bodies.append(await session.next_message())
        return start, bodies, stream

    @staticmethod
    def message_headers(message: Message) -> dict[bytes, bytes]:
        return dict(cast(list[tuple[bytes, bytes]], message["headers"]))

    @staticmethod
    def message_body(message: Message) -> bytes:
        return cast(bytes, message["body"])

    @staticmethod
    def expected_row(source: str) -> tuple[str, str, str, str]:
        supplied, confirmation = guardrail.message_context(
            [{"role": "user", "content": _TRIP_PROMPT}], 1
        )
        expected = guardrail.judge_rendered(
            (_TRIP_ANSWER,),
            _TRIP_ANSWER,
            supplied,
            frozenset(confirmation),
        )
        if not isinstance(expected, guardrail.Trip):
            raise AssertionError("fixture answer stopped tripping")
        return ("fixture-model", expected.family, expected.pattern_id, source)

    @staticmethod
    def response_body(messages: Sequence[Message]) -> bytes:
        return b"".join(
            cast(bytes, message["body"])
            for message in messages
            if message["type"] == "http.response.body"
        )

    def whole_trip_response(
        self,
        engine_body: bytes | None = None,
        headers: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
    ) -> ASGIResponse:
        body = whole_completion([_TRIP_CHOICE]) if engine_body is None else engine_body

        def engine(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=body,
                request=request,
            )

        return self.request(
            httpx.MockTransport(engine),
            body=_WHOLE_TRIP_REQUEST_BODY,
            headers=headers,
        )

    def trip_response_bytes(self, streamed: bool) -> tuple[int, bytes]:
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            SOURCE_HEADER: EVAL_IDENTITY,
        }
        if streamed:
            messages, _ = asyncio.run(self.trip_stream(headers=headers))
            return int(messages[0]["status"]), self.response_body(messages)
        response = self.whole_trip_response(headers=headers)
        return response.status_code, response.body

    @staticmethod
    def recording_connect(
        rows: list[tuple[str, str, str, str]], row_written: threading.Event
    ) -> Callable[..., object]:
        def connect(**kwargs: object) -> TripConnection:
            del kwargs
            return TripConnection(rows, row_written)

        return connect

    def test_trip_row_has_branch_family_pattern_and_source_on_both_paths(self) -> None:
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            SOURCE_HEADER: EVAL_IDENTITY,
        }
        for streamed in (True, False):
            with self.subTest(path="stream" if streamed else "whole"):
                rows: list[tuple[str, str, str, str]] = []
                row_written = threading.Event()
                with trip_driver(self.recording_connect(rows, row_written)):
                    if streamed:
                        messages, _ = asyncio.run(self.trip_stream(headers=headers))
                        status = int(messages[0]["status"])
                    else:
                        response = self.whole_trip_response(headers=headers)
                        status = response.status_code
                    self.assertTrue(row_written.wait(_ASGI_WAIT_SECONDS))
                self.assertEqual(status, 200)
                self.assertEqual(rows, [self.expected_row("eval")])

    def test_whole_trip_records_once_for_multiple_choices_and_unjudged_tail(self) -> None:
        cases = (
            (
                "two tripping choices",
                [_TRIP_CHOICE, dict(_TRIP_CHOICE, index=1)],
                200,
                False,
            ),
            (
                "trip then unjudgeable choice",
                [_TRIP_CHOICE, {"index": 1}],
                502,
                True,
            ),
        )
        for name, choices, status, unjudged in cases:
            with self.subTest(case=name):
                rows: list[tuple[str, str, str, str]] = []
                row_written = threading.Event()
                with trip_driver(self.recording_connect(rows, row_written)):
                    response = self.whole_trip_response(
                        whole_completion(choices),
                        headers={
                            "Authorization": f"Bearer {API_KEY}",
                            SOURCE_HEADER: EVAL_IDENTITY,
                        },
                    )
                    self.assertTrue(row_written.wait(_ASGI_WAIT_SECONDS))
                self.assertEqual(response.status_code, status)
                self.assertEqual(rows, [self.expected_row("eval")])
                if unjudged:
                    self.assertEqual(response.body, json.dumps(judged.UNJUDGED_ERROR, separators=(",", ":")).encode())

    def test_source_header_values_reach_rows_only_as_eval_or_user(self) -> None:
        cases = (
            ("exact", SOURCE_HEADER, (EVAL_IDENTITY,), "eval"),
            ("case-folded", SOURCE_HEADER, (EVAL_IDENTITY.upper(),), "eval"),
            ("padded", SOURCE_HEADER, (f"  {EVAL_IDENTITY}  ",), "eval"),
            ("absent", None, (), "user"),
            ("empty", SOURCE_HEADER, ("",), "user"),
            ("repeated", SOURCE_HEADER, (EVAL_IDENTITY, EVAL_IDENTITY), "user"),
            ("another identity", SOURCE_HEADER, ("other@example.invalid",), "user"),
            ("different header", "X-Other-Header", (EVAL_IDENTITY,), "user"),
        )
        for name, header_name, values, source in cases:
            with self.subTest(case=name):
                request_headers: list[tuple[str, str]] = [
                    ("Authorization", f"Bearer {API_KEY}")
                ]
                if header_name is not None:
                    request_headers.extend((header_name, value) for value in values)
                rows: list[tuple[str, str, str, str]] = []
                row_written = threading.Event()
                with trip_driver(self.recording_connect(rows, row_written)):
                    response = self.whole_trip_response(headers=request_headers)
                    self.assertTrue(row_written.wait(_ASGI_WAIT_SECONDS))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(rows, [self.expected_row(source)])

    def test_writer_failure_or_waiting_connect_never_changes_the_answer(self) -> None:
        for streamed in (True, False):
            with self.subTest(path="stream" if streamed else "whole"):
                rows: list[tuple[str, str, str, str]] = []
                row_written = threading.Event()
                with trip_driver(self.recording_connect(rows, row_written)):
                    expected_status, expected_body = self.trip_response_bytes(streamed)

                def raising_connect(**kwargs: object) -> object:
                    del kwargs
                    raise RuntimeError("fixture writer connection refused")

                with trip_driver(raising_connect):
                    status, body = self.trip_response_bytes(streamed)
                self.assertEqual((status, body), (expected_status, expected_body))

                started = threading.Event()
                dispatch_started = threading.Event()
                released = threading.Event()
                finished = threading.Event()

                with signal_dispatch(dispatch_started), trip_driver(
                    waiting_connect(started, released, finished)
                ):
                    try:
                        status, body = self.trip_response_bytes(streamed)
                        self.assertTrue(dispatch_started.wait(_ASGI_WAIT_SECONDS))
                        self.assertTrue(started.wait(_ASGI_WAIT_SECONDS))
                        self.assertFalse(finished.is_set())
                        self.assertEqual((status, body), (expected_status, expected_body))
                    finally:
                        released.set()

    def test_trip_driver_fails_when_writer_outlives_fixture(self) -> None:
        started = threading.Event()
        released = threading.Event()
        finished = threading.Event()

        def blocked_connect(**kwargs: object) -> TripConnection:
            del kwargs
            started.set()
            released.wait(_WRITER_BLOCK_SECONDS)
            finished.set()
            return TripConnection([], threading.Event())

        row = guardrail.TripRow(
            "fixture-branch",
            "fixture-family",
            "fixture-pattern",
            "user",
        )
        try:
            with self.assertRaisesRegex(
                AssertionError, r"1 writer thread\(s\) still alive after 1.0 seconds"
            ), trip_driver(blocked_connect):
                guardrail.dispatch_trip_row(row)
                self.assertTrue(started.wait(_ASGI_WAIT_SECONDS))
        finally:
            released.set()

        self.assertTrue(finished.wait(_ASGI_WAIT_SECONDS))

    def test_forwarded_header_sentinels_stay_out_of_logs_rows_responses_and_engine(self) -> None:
        calls: list[httpx.Request] = []
        rows: list[tuple[str, str, str, str]] = []
        row_written = threading.Event()

        def engine(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(200, content=whole_completion([]), request=request)
            if len(calls) == 2:
                return httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    content=whole_completion([_TRIP_CHOICE]),
                    request=request,
                )
            if len(calls) == 3:
                return httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    content=whole_completion([{"index": 0}]),
                    request=request,
                )
            raise httpx.ConnectError("fixture upstream failure", request=request)

        headers = [("Authorization", f"Bearer {API_KEY}"), *_SENTINEL_HEADERS.items()]
        with (
            # The production configuration: the service decides on the email
            # header, so the sentinel under it is a value the relay really reads
            # and the leak paths below are exercised rather than vacuous.
            patch.object(self, "source_header", API_USER_EMAIL_HEADER),
            trip_driver(self.recording_connect(rows, row_written)),
            capture_logs() as captured,
        ):
            responses = [
                self.request(httpx.MockTransport(engine), headers=headers),
                self.request(
                    httpx.MockTransport(engine),
                    body=_WHOLE_TRIP_REQUEST_BODY,
                    headers=headers,
                ),
                self.request(
                    httpx.MockTransport(engine),
                    body=_WHOLE_TRIP_REQUEST_BODY,
                    headers=headers,
                ),
                self.request(httpx.MockTransport(engine), headers=headers),
            ]
            self.assertTrue(row_written.wait(_ASGI_WAIT_SECONDS))

        self.assertEqual([response.status_code for response in responses], [200, 200, 502, 502])
        self.assertEqual(len(rows), 2)
        for sentinel in _SENTINEL_HEADERS.values():
            for row in rows:
                self.assertNotIn(sentinel, repr(row))
            for response in responses:
                self.assertNotIn(sentinel.encode(), response.body)
            for request in calls:
                self.assertNotIn(sentinel.encode(), request.content)
                self.assertNotIn(sentinel, "\n".join(request.headers.values()))
            for record in captured.records:
                # getMessage covers the message and its arguments; a record can
                # also carry a traceback, which is where an exception built from
                # a header value would surface.
                self.assertNotIn(sentinel, record.getMessage())
                self.assertNotIn(sentinel, logging.Formatter().format(record))

    def test_stream_is_relayed_in_order_before_next_upstream_chunk(self) -> None:
        with self.assertLogs("gideon.api.request", level="INFO") as captured:
            start, bodies, stream = asyncio.run(self.gated_stream("text/event-stream"))

        self.assertEqual(start["status"], 200)
        events = [_parse_event(self.message_body(message)) for message in bodies[:-1]]
        self.assertEqual(len(events), len(_STREAM_EVENTS))
        first = events[0]
        self.assertIsInstance(first, dict)
        assert isinstance(first, dict)
        first_choice = _first_choice(first)
        self.assertEqual(first_choice["index"], 0)
        self.assertEqual(first_choice["delta"], {"role": "assistant"})
        self.assertIsNone(first_choice["finish_reason"])
        self.assertEqual(_event_envelope(first), _STREAM_ENVELOPE)
        self.assertEqual(
            "".join(_event_content(event) for event in events), _STREAM_ANSWER
        )
        expected_events = [_parse_event(body) for body in _STREAM_EVENTS]
        for event, expected in zip(events, expected_events, strict=True):
            if isinstance(event, dict):
                self.assertIsInstance(expected, dict)
                assert isinstance(expected, dict)
                self.assertEqual(_event_envelope(event), _event_envelope(expected))
        finish = events[3]
        self.assertIsInstance(finish, dict)
        assert isinstance(finish, dict)
        self.assertEqual(_first_choice(finish)["finish_reason"], "stop")
        usage = events[4]
        self.assertIsInstance(usage, dict)
        assert isinstance(usage, dict)
        self.assertEqual(usage, _parse_event(_STREAM_EVENTS[4]))
        self.assertEqual(events[5], DONE_EVENT)
        self.assertEqual(self.message_body(bodies[-1]), b"")
        self.assertTrue(all(message["more_body"] for message in bodies[:-1]))
        self.assertFalse(bodies[-1]["more_body"])
        self.assertTrue(stream.closed)
        self.assertEqual(len(captured.output), 1)
        self.assertIn("POST /v1/chat/completions 200", captured.output[0])
        for secret in ("fixture prompt secret", "fixture header secret", API_KEY, ENGINE_KEY):
            self.assertNotIn(secret, captured.output[0])

    def test_shaped_stream_keeps_chunk_count_and_stamps_its_last_text(self) -> None:
        answer = (
            "The invented reporter is 17 F.3d 204. " + "neutral fixture context. " * 40
        )
        chunks = (
            _chunk_event({"role": "assistant"}),
            _chunk_event({"content": answer}),
            _chunk_event({}, finish_reason="stop"),
            b"data: [DONE]\n\n",
        )
        start, bodies, stream = asyncio.run(
            self.gated_stream("text/event-stream", chunks=chunks)
        )

        events = [_parse_event(self.message_body(message)) for message in bodies[:-1]]
        self.assertEqual(start["status"], 200)
        self.assertEqual(len(events), len(chunks))
        text_events = [event for event in events if _event_content(event)]
        self.assertTrue(text_events)
        self.assertTrue(_event_content(text_events[-1]).endswith(stamp.STAMP_TAIL))
        self.assertEqual(_event_content(text_events[-1]).count(stamp.CITATION_STAMP), 1)
        self.assertEqual(events[-1], DONE_EVENT)
        self.assertEqual(self.message_body(bodies[-1]), b"")
        self.assertTrue(stream.closed)

    def test_stream_content_type_forms_are_preserved_without_a_length(self) -> None:
        for content_type in (
            "text/event-stream",
            "text/event-stream; charset=utf-8",
            "Text/Event-Stream",
        ):
            with self.subTest(content_type=content_type):
                start, bodies, _ = asyncio.run(self.gated_stream(content_type))

                headers = self.message_headers(start)
                self.assertEqual(headers[b"content-type"], content_type.encode("latin-1"))
                self.assertNotIn(b"content-length", headers)
                self.assertEqual(self.message_body(bodies[-1]), b"")

    def test_non_event_stream_success_is_returned_whole(self) -> None:
        completion = b'{"id":"fixture-whole","choices":[]}'
        stream = FixtureStream(completion)

        def engine(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=stream,
                request=request,
            )

        response = self.request(
            httpx.MockTransport(engine),
            body=b'{"stream":true}',
            headers={"Authorization": f"Bearer {API_KEY}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, completion)
        self.assertEqual(response.headers[b"content-type"], b"application/json")
        self.assertEqual(response.headers[b"content-length"], str(len(completion)).encode())
        self.assertTrue(stream.closed)

    def test_engine_error_is_returned_whole_for_a_stream_request(self) -> None:
        error_body = b'{"error":{"message":"fixture stream refusal"}}'

        def engine(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                headers={"content-type": "application/json"},
                content=error_body,
                request=request,
            )

        response = self.request(
            httpx.MockTransport(engine),
            body=b'{"stream":true}',
            headers={"Authorization": f"Bearer {API_KEY}"},
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.body, error_body)
        self.assertEqual(response.headers[b"content-type"], b"application/json")

    async def abandon_stream(self, spec_version: str) -> GatedStream:
        stream = GatedStream((_STREAM_EVENTS[0], _STREAM_EVENTS[1]))

        def engine(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
                request=request,
            )

        app = create_app(self.settings(), transport=httpx.MockTransport(engine))
        async with ASGISession(
            app,
            "POST",
            "/v1/chat/completions",
            b'{"stream":true,"prompt":"fixture prompt secret"}',
            {
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
                "X-Fixture-Header": "fixture header secret",
            },
            spec_version,
        ) as session:
            await session.next_message()
            stream.release(0)
            first = await session.next_message()
            self.assertEqual(
                _parse_event(self.message_body(first)), _parse_event(_STREAM_EVENTS[0])
            )
            session.disconnect()
            await session.wait_for_application()
            with self.assertRaises(TimeoutError):
                await session.next_message(_SILENCE_SECONDS)
        return stream

    def test_disconnect_mid_stream_closes_upstream_at_both_spec_versions(self) -> None:
        for spec_version in ("2.3", "2.4"):
            with self.subTest(spec_version=spec_version):
                with self.assertLogs("gideon.api.request", level="INFO") as captured:
                    stream = asyncio.run(self.abandon_stream(spec_version))

                self.assertTrue(stream.closed)
                self.assertEqual(len(captured.output), 1)
                self.assertTrue(captured.output[0].endswith(" disconnected"))
                self.assertIn("POST /v1/chat/completions 200", captured.output[0])
                for secret in (
                    "fixture prompt secret",
                    "fixture header secret",
                    API_KEY,
                    ENGINE_KEY,
                ):
                    self.assertNotIn(secret, captured.output[0])

    async def abandon_whole_completion(self) -> PendingEngine:
        pending = PendingEngine()
        app = create_app(self.settings(), transport=httpx.MockTransport(pending))
        async with ASGISession(
            app,
            "POST",
            "/v1/chat/completions",
            b'{"stream":false,"prompt":"fixture prompt secret"}',
            {
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
                "X-Fixture-Header": "fixture header secret",
            },
            "2.4",
        ) as session:
            await asyncio.wait_for(pending.started.wait(), _ASGI_WAIT_SECONDS)
            session.disconnect()
            await session.wait_for_application()
            with self.assertRaises(TimeoutError):
                await session.next_message(_SILENCE_SECONDS)
        return pending

    def test_disconnect_during_whole_completion_cancels_pending_upstream(self) -> None:
        with self.assertLogs("gideon.api.request", level="INFO") as captured:
            pending = asyncio.run(self.abandon_whole_completion())

        self.assertTrue(pending.cancelled)
        self.assertEqual(len(captured.output), 1)
        self.assertIn("POST /v1/chat/completions - ", captured.output[0])
        self.assertTrue(captured.output[0].endswith(" disconnected"))
        for secret in (
            "fixture prompt secret",
            "fixture header secret",
            API_KEY,
            ENGINE_KEY,
        ):
            self.assertNotIn(secret, captured.output[0])

    async def failed_stream(self, chunks: tuple[bytes, ...] = _STREAM_EVENTS[:2]) -> tuple[list[Message], GatedStream, httpx.HTTPError]:
        failure = httpx.ReadError("fixture mid-stream failure")
        stream = GatedStream(chunks, failure)

        def engine(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream; charset=utf-8"},
                stream=stream,
                request=request,
            )

        app = create_app(self.settings(), transport=httpx.MockTransport(engine))
        async with ASGISession(
            app,
            "POST",
            "/v1/chat/completions",
            b'{"stream":true,"prompt":"fixture prompt secret"}',
            {
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
                "X-Fixture-Header": "fixture header secret",
            },
            "2.4",
        ) as session:
            messages = [await session.next_message()]
            for index in range(len(stream.chunks)):
                stream.release(index)
                messages.append(await session.next_message())
            # The settled tail, the break, the error event, and the end marker.
            for _ in range(4):
                messages.append(await session.next_message())
            messages.append(await session.next_message())
        return messages, stream, failure

    def test_mid_stream_failure_ends_with_fixed_error_events_and_logs_class(self) -> None:
        with self.assertLogs("gideon.api.relay", level="WARNING") as captured:
            messages, stream, failure = asyncio.run(self.failed_stream())

        self.assertEqual(messages[0]["type"], "http.response.start")
        self.assertEqual(
            _parse_event(self.message_body(messages[1])),
            _parse_event(_STREAM_EVENTS[0]),
        )
        released = _parse_event(self.message_body(messages[2]))
        settled_tail = _parse_event(self.message_body(messages[3]))
        self.assertEqual(
            _event_content(released) + _event_content(settled_tail),
            _STREAM_CONTENT_DELTAS[0],
        )
        self.assertEqual(self.message_body(messages[4]), b"\n\n")
        self.assertEqual(_parse_event(self.message_body(messages[5])), UPSTREAM_ERROR)
        self.assertEqual(_parse_event(self.message_body(messages[6])), DONE_EVENT)
        self.assertEqual(self.message_body(messages[7]), b"")
        self.assertTrue(all(message["more_body"] for message in messages[1:7]))
        self.assertFalse(messages[7]["more_body"])
        self.assertTrue(stream.closed)
        self.assertEqual(len(captured.output), 1)
        self.assertIn(type(failure).__name__, captured.output[0])
        self.assertNotIn(str(failure), captured.output[0])
        for secret in (
            "fixture prompt secret",
            "fixture header secret",
            API_KEY,
            ENGINE_KEY,
        ):
            self.assertNotIn(secret, captured.output[0])

    def test_mid_stream_failure_after_shaped_text_sends_label_before_error_events(
        self,
    ) -> None:
        answer = (
            "The invented reporter is 17 F.3d 204. " + "neutral fixture context. " * 40
        )
        chunks = (
            _chunk_event({"role": "assistant"}),
            _chunk_event({"content": answer}),
        )
        with self.assertLogs("gideon.api.relay", level="WARNING"):
            messages, stream, _failure = asyncio.run(self.failed_stream(chunks))

        released = _parse_event(self.message_body(messages[2]))
        settled = _parse_event(self.message_body(messages[3]))
        self.assertEqual(
            _event_content(released) + _event_content(settled),
            answer + stamp.STAMP_TAIL,
        )
        self.assertTrue(_event_content(settled).endswith(stamp.STAMP_TAIL))
        self.assertEqual(self.message_body(messages[4]), b"\n\n")
        self.assertEqual(_parse_event(self.message_body(messages[5])), UPSTREAM_ERROR)
        self.assertEqual(_parse_event(self.message_body(messages[6])), DONE_EVENT)
        self.assertEqual(self.message_body(messages[7]), b"")
        self.assertTrue(stream.closed)

    def test_engine_error_after_shaped_text_sends_label_before_the_error_event(
        self,
    ) -> None:
        answer = (
            "The invented reporter is 17 F.3d 204. " + "neutral fixture context. " * 40
        )
        error_event = b'data: {"error":{"message":"fixture engine error"}}\n\n'
        stream = GatedStream(
            (
                _chunk_event({"role": "assistant"}),
                _chunk_event({"content": answer}),
                error_event,
            )
        )

        def engine(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
                request=request,
            )

        async def run() -> tuple[list[Message], GatedStream]:
            app = create_app(self.settings(), transport=httpx.MockTransport(engine))
            async with ASGISession(
                app,
                "POST",
                "/v1/chat/completions",
                b'{"stream":true,"prompt":"fixture prompt secret"}',
                {"Authorization": f"Bearer {API_KEY}"},
                "2.4",
            ) as session:
                messages = [await session.next_message()]
                for index in range(len(stream.chunks)):
                    stream.release(index)
                while messages[-1].get("more_body", True):
                    messages.append(await session.next_message())
            return messages, stream

        messages, stream = asyncio.run(run())
        events = [
            _parse_event(self.message_body(message)) for message in messages[1:-1]
        ]
        self.assertEqual(
            _event_content(events[1]) + _event_content(events[2]),
            answer + stamp.STAMP_TAIL,
        )
        self.assertTrue(_event_content(events[2]).endswith(stamp.STAMP_TAIL))
        self.assertEqual(events[3], {"error": {"message": "fixture engine error"}})
        self.assertEqual(self.message_body(messages[-1]), b"")
        self.assertTrue(stream.closed)

    async def first_event_failure(self) -> tuple[list[Message], GatedStream]:
        stream = GatedStream((b"data: not-json\n\n",))

        def engine(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
                request=request,
            )

        app = create_app(self.settings(), transport=httpx.MockTransport(engine))
        async with ASGISession(
            app,
            "POST",
            "/v1/chat/completions",
            b'{"stream":true,"prompt":"fixture prompt secret"}',
            {"Authorization": f"Bearer {API_KEY}"},
            "2.4",
        ) as session:
            messages = [await session.next_message()]
            stream.release(0)
            for _ in range(4):
                messages.append(await session.next_message())
        return messages, stream

    def test_first_event_failure_sends_only_envelope_free_trip_payloads(self) -> None:
        messages, stream = asyncio.run(self.first_event_failure())

        events = [_parse_event(self.message_body(message)) for message in messages[1:4]]
        self.assertEqual(len(events), 3)
        self.assertTrue(stream.closed)
        for event in events[:2]:
            self.assertIsInstance(event, dict)
            assert isinstance(event, dict)
            self.assertEqual(set(event), {"object", "choices"})
            self.assertEqual(event["object"], "chat.completion.chunk")
        self.assertEqual(events[2], DONE_EVENT)
        self.assertEqual(self.message_body(messages[4]), b"")
        self.assertNotIn(
            b"not-json",
            b"".join(
                self.message_body(message)
                for message in messages
                if "body" in message
            ),
        )

    async def trip_stream(
        self,
        *,
        headers: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
    ) -> tuple[list[Message], GatedStream]:
        stream = GatedStream(_TRIP_EVENTS)

        def engine(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
                request=request,
            )

        app = create_app(self.settings(), transport=httpx.MockTransport(engine))
        async with ASGISession(
            app,
            "POST",
            "/v1/chat/completions",
            _TRIP_REQUEST_BODY,
            headers
            if headers is not None
            else {"Authorization": f"Bearer {API_KEY}"},
            "2.4",
        ) as session:
            messages = [await session.next_message()]
            for index in range(_TRIP_RELEASED):
                stream.release(index)
            while messages[-1].get("more_body", True):
                messages.append(await session.next_message())
        return messages, stream

    def test_trip_closes_upstream_before_refusal_body_is_sent(self) -> None:
        messages, stream = asyncio.run(self.trip_stream())

        bodies = [self.message_body(message) for message in messages[1:]]
        events = [_parse_event(body) for body in bodies[:-1]]
        # The refusal is the first payload after the close: the last three
        # events are the refusal delta, the finish chunk, and the end marker.
        self.assertTrue(stream.closed)
        self.assertEqual(events[-1], DONE_EVENT)
        finish = events[-2]
        assert isinstance(finish, dict)
        self.assertEqual(_first_choice(finish)["finish_reason"], "stop")
        self.assertEqual(_first_choice(finish)["delta"], {})
        refusal = events[-3]
        assert isinstance(refusal, dict)
        released = "".join(_event_content(event) for event in events[:-3])
        self.assertGreater(len(released), 0)
        self.assertEqual(
            _event_content(refusal),
            guardrail.REFUSAL_SEPARATOR + guardrail.DEADLINE_FAMILY.refusal,
        )
        # The relay stopped reading: the two events the stub still held never
        # reached the caller, and neither did the date the window caught.
        self.assertNotIn(_TRIP_UNREAD, released)
        self.assertNotIn("June 5, 2027", released)
        self.assertEqual(bodies[-1], b"")

    def test_whole_completion_is_returned_intact_with_a_length(self) -> None:
        completion = b'{"id":"fixture-completion","choices":[]}'
        stream = FixtureStream(completion)

        def engine(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=stream,
                request=request,
            )

        response = self.request(
            httpx.MockTransport(engine),
            headers={"Authorization": f"Bearer {API_KEY}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, completion)
        self.assertEqual(response.headers[b"content-type"], b"application/json")
        self.assertEqual(response.headers[b"content-length"], str(len(completion)).encode())
        self.assertTrue(stream.closed)

    def test_whole_completion_stamps_shaped_and_preserves_free_bytes(self) -> None:
        cases = (
            ("shaped", "The invented reporter is 17 F.3d 204.", True),
            ("free", "A visibly fictitious answer has no citation shape.", False),
        )
        request_body = json.dumps(
            {
                "stream": False,
                "messages": [{"role": "user", "content": "fixture prompt"}],
            },
            separators=(",", ":"),
        ).encode()
        for name, answer, shaped in cases:
            with self.subTest(case=name):
                engine_body = whole_completion(
                    [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": answer},
                        }
                    ]
                )

                def engine(
                    request: httpx.Request, response_body: bytes = engine_body
                ) -> httpx.Response:
                    return httpx.Response(
                        200,
                        headers={"content-type": "application/json"},
                        content=response_body,
                        request=request,
                    )

                response = self.request(
                    httpx.MockTransport(engine),
                    body=request_body,
                    headers={"Authorization": f"Bearer {API_KEY}"},
                )
                self.assertEqual(response.status_code, 200)
                parsed = json.loads(response.body)
                actual = parsed["choices"][0]["message"]["content"]
                expected = answer + stamp.STAMP_TAIL if shaped else answer
                self.assertEqual(actual, expected)
                if not shaped:
                    self.assertEqual(response.body, engine_body)

    def test_whole_stamp_does_not_put_answer_or_label_in_logs(self) -> None:
        answer = "The invented reporter is 17 F.3d 204."
        engine_body = whole_completion(
            [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": answer},
                    "finish_reason": "stop",
                }
            ]
        )

        def engine(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=engine_body,
                request=request,
            )

        with capture_logs() as captured:
            response = self.request(
                httpx.MockTransport(engine),
                body=b'{"stream":false,"messages":[{"role":"user","content":"fixture prompt"}]}',
                headers={"Authorization": f"Bearer {API_KEY}"},
            )

        self.assertEqual(response.status_code, 200)
        for record in captured.records:
            rendered = logging.Formatter().format(record)
            self.assertNotIn(answer, rendered)
            self.assertNotIn(stamp.CITATION_STAMP, rendered)

    def test_completion_forwards_body_content_type_and_only_engine_identity(self) -> None:
        callers_body = b'{"messages":[{"content":"fixture prompt"}]}'
        calls: list[httpx.Request] = []

        def engine(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, content=b'{"choices":[]}', request=request)

        response = self.request(
            httpx.MockTransport(engine),
            body=callers_body,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json; charset=utf-8",
                "X-Caller-Header": "fixture caller header",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 1)
        request = calls[0]
        self.assertEqual(request.content, callers_body)
        self.assertEqual(request.headers["authorization"], f"Bearer {ENGINE_KEY}")
        self.assertEqual(request.headers["content-type"], "application/json; charset=utf-8")
        self.assertNotIn("x-caller-header", request.headers)

    def test_tripped_whole_completion_has_its_judged_content_length(self) -> None:
        completion = {
            "id": "fixture-whole",
            "object": "chat.completion",
            "created": 1_700_000_000,
            "model": "fixture-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "The deadline is June 5, 2027.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 6},
        }
        engine_body = json.dumps(completion, separators=(",", ":")).encode()

        def engine(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=engine_body,
                request=request,
            )

        response = self.request(
            httpx.MockTransport(engine),
            body=b'{"messages":[{"role":"user","content":"Explain a fictitious legal rule."}]}',
            headers={"Authorization": f"Bearer {API_KEY}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers[b"content-length"], str(len(response.body)).encode()
        )
        self.assertEqual(response.headers[b"content-type"], b"application/json")
        parsed = json.loads(response.body)
        self.assertEqual(
            parsed["choices"][0]["message"]["content"],
            guardrail.DEADLINE_FAMILY.refusal,
        )
        self.assertEqual(parsed["choices"][0]["finish_reason"], "stop")
        self.assertEqual(parsed["usage"], completion["usage"])
        self.assertNotIn(b"June 5, 2027", response.body)

    def test_unjudgeable_whole_completion_is_a_fixed_502_without_engine_bytes(self) -> None:
        engine_body = b'{"engine_secret":"fixture engine bytes","choices":[{"message":{"content":7}}]}'

        def engine(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=engine_body,
                request=request,
            )

        with self.assertLogs("gideon.api.relay", level="WARNING") as captured:
            response = self.request(
                httpx.MockTransport(engine),
                body=b'{"stream":false,"messages":[{"role":"user","content":"short fictitious chat"}]}',
                headers={"Authorization": f"Bearer {API_KEY}"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.headers[b"content-type"], b"application/json")
        self.assertEqual(json.loads(response.body), judged.UNJUDGED_ERROR)
        self.assertNotIn(b"fixture engine bytes", response.body)
        # The failure is named by exception class alone: no engine byte and no
        # prompt text reaches the log line.
        self.assertEqual(len(captured.output), 1)
        self.assertIn("TypeError", captured.output[0])
        for secret in ("fixture engine bytes", "short fictitious chat"):
            self.assertNotIn(secret, captured.output[0])

    def test_first_event_failure_logs_the_exception_class_alone(self) -> None:
        with self.assertLogs("gideon.api.relay", level="WARNING") as captured:
            asyncio.run(self.first_event_failure())

        self.assertEqual(len(captured.output), 1)
        self.assertIn("JSONDecodeError", captured.output[0])
        for secret in ("not-json", "fixture prompt secret"):
            self.assertNotIn(secret, captured.output[0])

    def test_engine_error_status_and_body_pass_through(self) -> None:
        error_body = b'{"error":{"message":"fixture refusal"}}'

        def engine(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                headers={"content-type": "application/json"},
                content=error_body,
                request=request,
            )

        response = self.request(
            httpx.MockTransport(engine),
            headers={"Authorization": f"Bearer {API_KEY}"},
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.body, error_body)
        self.assertEqual(response.headers[b"content-type"], b"application/json")

    def test_missing_or_wrong_key_is_refused_before_any_upstream_call(self) -> None:
        calls: list[httpx.Request] = []

        def engine(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, content=b"fixture response", request=request)

        transport = httpx.MockTransport(engine)
        missing = self.request(transport)
        wrong = self.request(transport, headers={"Authorization": "Bearer wrong-key"})

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(calls, [])

    def test_transport_failure_is_a_fixed_502(self) -> None:
        calls: list[httpx.Request] = []

        def engine(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise httpx.ConnectError("fixture transport failure", request=request)

        response = self.request(
            httpx.MockTransport(engine),
            headers={"Authorization": f"Bearer {API_KEY}"},
        )

        self.assertEqual(response.status_code, 502)
        self.assertIn(b'"upstream_unavailable"', response.body)
        self.assertEqual(len(calls), 1)

    def test_timeout_before_headers_is_a_fixed_502(self) -> None:
        calls: list[httpx.Request] = []

        def engine(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise httpx.ReadTimeout("fixture header timeout", request=request)

        response = self.request(
            httpx.MockTransport(engine),
            headers={"Authorization": f"Bearer {API_KEY}"},
        )

        self.assertEqual(response.status_code, 502)
        self.assertIn(b'"upstream_unavailable"', response.body)
        self.assertEqual(len(calls), 1)

    def test_timeout_during_whole_read_is_a_fixed_502(self) -> None:
        stream = FixtureStream(b"", httpx.ReadTimeout("fixture read timeout"))

        def engine(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream, request=request)

        response = self.request(
            httpx.MockTransport(engine),
            headers={"Authorization": f"Bearer {API_KEY}"},
        )

        self.assertEqual(response.status_code, 502)
        self.assertIn(b'"upstream_unavailable"', response.body)
        self.assertTrue(stream.closed)

    def test_completion_read_bound_is_longer_than_the_model_list_bound(self) -> None:
        timeouts: dict[str, dict[str, float]] = {}

        def engine(request: httpx.Request) -> httpx.Response:
            timeout = cast(dict[str, float], request.extensions["timeout"])
            timeouts[request.url.path] = timeout
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": []}, request=request)
            return httpx.Response(200, content=b'{"choices":[]}', request=request)

        transport = httpx.MockTransport(engine)
        completion = self.request(
            transport,
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        models = self.request(
            transport,
            method="GET",
            path="/v1/models",
            body=b"",
            headers={"Authorization": f"Bearer {API_KEY}"},
        )

        self.assertEqual(completion.status_code, 200)
        self.assertEqual(models.status_code, 200)
        self.assertEqual(
            timeouts["/v1/chat/completions"]["read"],
            UPSTREAM_COMPLETION_READ_TIMEOUT_SECONDS,
        )
        self.assertEqual(timeouts["/v1/models"]["read"], UPSTREAM_READ_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
