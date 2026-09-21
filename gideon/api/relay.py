"""The judged streamed and whole-response completion relay for the API service."""

import json
import logging
from collections.abc import Awaitable, Callable, Mapping

import anyio
import httpx
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from gideon import guardrail

from . import judged
from .sse import DONE_EVENT, EventReassembler

_RELAY_LOG = logging.getLogger("gideon.api.relay")
UPSTREAM_ERROR = {
    "error": {
        "message": "The upstream engine is unavailable.",
        "type": "server_error",
        "param": None,
        "code": "upstream_unavailable",
    }
}
_UPSTREAM_ERROR_EVENT = (
    b"data: " + json.dumps(UPSTREAM_ERROR, separators=(",", ":")).encode("utf-8") + b"\n\n"
)
_STREAM_END = b"data: [DONE]\n\n"
# Two newlines, not one: a failure that cut an event mid-line needs the line
# ended before the blank line that closes the event.
_EVENT_BREAK = b"\n\n"


def _is_event_stream(content_type: str | None) -> bool:
    """Return whether a content type has the event-stream media type."""

    if content_type is None:
        return False
    media_type = content_type.partition(";")[0].strip()
    return media_type.casefold() == "text/event-stream"


async def _run_with_disconnect(work: Callable[[], Awaitable[None]], receive: Receive) -> bool:
    """Run *work* beside a receive watch and report whether the caller left."""

    disconnected = False

    async with anyio.create_task_group() as task_group:
        async def run_work() -> None:
            try:
                await work()
            finally:
                task_group.cancel_scope.cancel()

        async def watch_disconnect() -> None:
            nonlocal disconnected
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected = True
                    task_group.cancel_scope.cancel()
                    return

        task_group.start_soon(run_work)
        task_group.start_soon(watch_disconnect)

    return disconnected


class CompletionRelay:
    """Relay one judged completion response as a stream or after reading it in full."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        try:
            body = await request.body()
        except ClientDisconnect:
            # No response started, so the log's status stays absent.
            return
        content_type = request.headers.get("content-type")

        async def work() -> None:
            state = judged.stream_state_from_body(body)
            upstream = await request.app.state.engine.completion(body, content_type)
            if upstream is None:
                await JSONResponse(UPSTREAM_ERROR, status_code=502)(scope, receive, send)
                return

            try:
                upstream_type = upstream.headers.get("content-type")
                if upstream.status_code == 200 and _is_event_stream(upstream_type):
                    await self._stream(upstream, upstream_type, state, send)
                    return
                try:
                    content = await upstream.aread()
                except httpx.HTTPError as exc:
                    _RELAY_LOG.warning(
                        "upstream completion failed: %s", type(exc).__name__
                    )
                    await JSONResponse(UPSTREAM_ERROR, status_code=502)(scope, receive, send)
                    return
                if upstream.status_code == 200:
                    judged_content, failure = judged.judge_completion(content, state)
                    if failure is not None or judged_content is None:
                        _RELAY_LOG.warning("completion unjudged: %s", failure)
                        await JSONResponse(
                            judged.UNJUDGED_ERROR, status_code=502
                        )(scope, receive, send)
                        return
                    content = judged_content
                headers = {} if upstream_type is None else {"content-type": upstream_type}
                await Response(
                    content=content,
                    status_code=upstream.status_code,
                    headers=headers,
                )(scope, receive, send)
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()

        if await _run_with_disconnect(work, receive):
            # The work was cancelled where it stood; nothing further is sent.
            return

    async def _stream(
        self,
        upstream: httpx.Response,
        content_type: str | None,
        state: guardrail.StreamState,
        send: Send,
    ) -> None:
        """Judge and send an event-stream response one event at a time."""

        headers = [] if content_type is None else [(b"content-type", content_type.encode("latin-1"))]
        await send(
            {
                "type": "http.response.start",
                "status": upstream.status_code,
                "headers": headers,
            }
        )
        reassembler = EventReassembler()
        mechanics = judged.StreamMechanics(state)
        try:
            async for chunk in upstream.aiter_raw():
                # The reassembler's overrun and any failure of the mechanics
                # enter the error doctrine at one place, so nothing the relay
                # cannot read is ever relayed.
                try:
                    events = reassembler.feed(chunk)
                    for event in events:
                        payloads, tripped = mechanics.process(event)
                        if tripped:
                            await self._close_upstream(upstream)
                            await self._send_payloads(payloads, send)
                            await self._send_final_body(send)
                            self._log_mechanics_failure(mechanics)
                            return
                        await self._send_payloads(payloads, send)
                except Exception as exc:  # noqa: BLE001 - SSEEventTooLargeError among them.
                    await self._fail_closed(upstream, mechanics, send, exc)
                    return
            # A clean end of body the stream never announced: the held tail is
            # settled before the body ends, and a truncated last event is the
            # error path.
            try:
                if reassembler.has_pending_bytes():
                    raise ValueError("incomplete SSE event")
                payloads, tripped = mechanics.finish()
            except Exception as exc:  # noqa: BLE001 - the mechanics fail closed.
                await self._fail_closed(upstream, mechanics, send, exc)
                return
            if tripped:
                await self._close_upstream(upstream)
            await self._send_payloads(payloads, send)
            self._log_mechanics_failure(mechanics)
        except httpx.HTTPError as exc:
            # The tail rides out before the error events, so a failure never
            # overtakes text the window still holds.
            try:
                payloads, tripped = mechanics.finish()
            except Exception as mechanics_exc:  # noqa: BLE001 - the mechanics fail closed.
                await self._fail_closed(upstream, mechanics, send, mechanics_exc)
                return
            if tripped:
                # The tail tripped, so the error events are not sent — but the
                # transport failure that ended the stream is still logged.
                await self._close_upstream(upstream)
                await self._send_payloads(payloads, send)
                await self._send_final_body(send)
                _RELAY_LOG.warning("upstream completion failed: %s", type(exc).__name__)
                self._log_mechanics_failure(mechanics)
                return
            await self._send_payloads(payloads, send)
            await send({"type": "http.response.body", "body": _EVENT_BREAK, "more_body": True})
            await send(
                {
                    "type": "http.response.body",
                    "body": _UPSTREAM_ERROR_EVENT,
                    "more_body": True,
                }
            )
            await send({"type": "http.response.body", "body": _STREAM_END, "more_body": True})
            _RELAY_LOG.warning("upstream completion failed: %s", type(exc).__name__)
        await self._send_final_body(send)

    @staticmethod
    def _log_mechanics_failure(mechanics: judged.StreamMechanics) -> None:
        """Name a failure the mechanics caught themselves, by class alone."""

        if mechanics.failure is not None:
            _RELAY_LOG.warning("judged stream failed: %s", mechanics.failure)

    async def _close_upstream(self, upstream: httpx.Response) -> None:
        """Close an upstream response without allowing cancellation to interrupt it."""

        with anyio.CancelScope(shield=True):
            await upstream.aclose()

    async def _send_payloads(
        self,
        payloads: list[dict[str, object] | str],
        send: Send,
    ) -> None:
        """Send each judged payload as one event-stream body message."""

        for payload in payloads:
            if payload == DONE_EVENT:
                body = _STREAM_END
            else:
                if not isinstance(payload, Mapping):
                    raise TypeError("judged stream payload is not a mapping")
                body = (
                    b"data: "
                    + json.dumps(payload, separators=(",", ":")).encode("utf-8")
                    + _EVENT_BREAK
                )
            await send({"type": "http.response.body", "body": body, "more_body": True})

    async def _send_final_body(self, send: Send) -> None:
        """Close the response body after all judged stream payloads are sent."""

        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _fail_closed(
        self,
        upstream: httpx.Response,
        mechanics: judged.StreamMechanics,
        send: Send,
        error: Exception,
    ) -> None:
        """End a stream the relay could not read as a trip ends it.

        The upstream is closed before anything is sent, as on a judged trip,
        and the failure is named by its exception class alone.
        """

        payloads, _tripped = mechanics.fail_closed()
        await self._close_upstream(upstream)
        await self._send_payloads(payloads, send)
        await self._send_final_body(send)
        _RELAY_LOG.warning("judged stream failed: %s", type(error).__name__)
