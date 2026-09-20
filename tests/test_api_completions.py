"""In-process ASGI-level contracts for the streamed and whole completion relay."""

import asyncio
import json
import unittest
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, cast

import httpx
from starlette.types import Message, Scope

from gideon.api.app import create_app
from gideon.api.relay import UPSTREAM_ERROR
from gideon.api.settings import Settings
from gideon.api.upstream import (
    UPSTREAM_COMPLETION_READ_TIMEOUT_SECONDS,
    UPSTREAM_READ_TIMEOUT_SECONDS,
)

API_KEY = "fixture-api-key"
ENGINE_KEY = "fixture-engine-key"
ENGINE_URL = "http://fixture-engine/v1"
_ASGI_WAIT_SECONDS = 1.0
# The application task has already finished wherever this bound is used, so a
# message that has not arrived by now never will.
_SILENCE_SECONDS = 0.05
_STREAM_EVENTS = (
    b'data: {"id":"fixture-stream","delta":{"role":"assistant"}}\n\n',
    b'data: {"id":"fixture-stream","delta":{"content":"fixture answer"}}\n\n',
    b'data: {"id":"fixture-stream","delta":{},"finish_reason":"stop"}\n\n',
    b"data: [DONE]\n\n",
)


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
        headers: Mapping[str, str] | None,
        spec_version: str,
    ) -> None:
        self.app = app
        self.incoming: asyncio.Queue[Message] = asyncio.Queue()
        self.incoming.put_nowait(
            {"type": "http.request", "body": body, "more_body": False}
        )
        self.outgoing: asyncio.Queue[Message] = asyncio.Queue()
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
                for name, value in (headers or {}).items()
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
    def settings(self) -> Settings:
        return Settings(ENGINE_URL, ENGINE_KEY, API_KEY, 8000)

    def request(
        self,
        handler: httpx.MockTransport,
        method: str = "POST",
        path: str = "/v1/chat/completions",
        *,
        body: bytes = b'{"messages":[]}',
        headers: Mapping[str, str] | None = None,
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

    def test_stream_is_relayed_in_order_before_next_upstream_chunk(self) -> None:
        with self.assertLogs("gideon.api.request", level="INFO") as captured:
            start, bodies, stream = asyncio.run(self.gated_stream("text/event-stream"))

        self.assertEqual(start["status"], 200)
        self.assertEqual(
            [self.message_body(message) for message in bodies[:-1]], list(_STREAM_EVENTS)
        )
        self.assertEqual(self.message_body(bodies[-1]), b"")
        self.assertTrue(all(message["more_body"] for message in bodies[:-1]))
        self.assertFalse(bodies[-1]["more_body"])
        self.assertTrue(stream.closed)
        self.assertEqual(len(captured.output), 1)
        self.assertIn("POST /v1/chat/completions 200", captured.output[0])
        for secret in ("fixture prompt secret", "fixture header secret", API_KEY, ENGINE_KEY):
            self.assertNotIn(secret, captured.output[0])

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
            self.assertEqual(self.message_body(first), _STREAM_EVENTS[0])
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

    async def failed_stream(self) -> tuple[list[Message], GatedStream, httpx.HTTPError]:
        failure = httpx.ReadError("fixture mid-stream failure")
        stream = GatedStream(_STREAM_EVENTS[:2], failure)

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
            # The break, the error event, the end marker, and the final message.
            for _ in range(4):
                messages.append(await session.next_message())
        return messages, stream, failure

    def test_mid_stream_failure_ends_with_fixed_error_events_and_logs_class(self) -> None:
        with self.assertLogs("gideon.api.relay", level="WARNING") as captured:
            messages, stream, failure = asyncio.run(self.failed_stream())

        error_event = (
            b"data: "
            + json.dumps(UPSTREAM_ERROR, separators=(",", ":")).encode("utf-8")
            + b"\n\n"
        )
        self.assertEqual(messages[0]["type"], "http.response.start")
        self.assertEqual(
            [self.message_body(message) for message in messages[1:3]],
            list(_STREAM_EVENTS[:2]),
        )
        self.assertEqual(self.message_body(messages[3]), b"\n\n")
        self.assertEqual(self.message_body(messages[4]), error_event)
        self.assertEqual(self.message_body(messages[5]), b"data: [DONE]\n\n")
        self.assertEqual(self.message_body(messages[6]), b"")
        self.assertTrue(all(message["more_body"] for message in messages[1:6]))
        self.assertFalse(messages[6]["more_body"])
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

    def test_completion_forwards_body_content_type_and_only_engine_identity(self) -> None:
        callers_body = b'{"messages":[{"content":"fixture prompt"}]}'
        calls: list[httpx.Request] = []

        def engine(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, content=b"fixture completion", request=request)

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
            return httpx.Response(200, content=b"fixture completion", request=request)

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
