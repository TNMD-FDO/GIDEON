"""The streamed and whole-response completion relay for the API service."""

import json
import logging
from collections.abc import Awaitable, Callable

import anyio
import httpx
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

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
    """Relay one completion response as a stream or after reading it in full."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        try:
            body = await request.body()
        except ClientDisconnect:
            # No response started, so the log's status stays absent.
            return
        content_type = request.headers.get("content-type")

        async def work() -> None:
            upstream = await request.app.state.engine.completion(body, content_type)
            if upstream is None:
                await JSONResponse(UPSTREAM_ERROR, status_code=502)(scope, receive, send)
                return

            try:
                upstream_type = upstream.headers.get("content-type")
                if upstream.status_code == 200 and _is_event_stream(upstream_type):
                    await self._stream(upstream, upstream_type, send)
                    return
                try:
                    content = await upstream.aread()
                except httpx.HTTPError:
                    await JSONResponse(UPSTREAM_ERROR, status_code=502)(scope, receive, send)
                    return
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
        self, upstream: httpx.Response, content_type: str | None, send: Send
    ) -> None:
        """Send an event-stream response one upstream chunk at a time."""

        headers = [] if content_type is None else [(b"content-type", content_type.encode("latin-1"))]
        await send(
            {
                "type": "http.response.start",
                "status": upstream.status_code,
                "headers": headers,
            }
        )
        try:
            async for chunk in upstream.aiter_raw():
                await send(
                    {
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": True,
                    }
                )
        except httpx.HTTPError as exc:
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
        await send({"type": "http.response.body", "body": b"", "more_body": False})
