"""Require the model server's bearer key on routes other than its probes."""

import os
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

KEY_VARIABLE = "VLLM_API_KEY"
OPEN_PATHS = ("/health", "/metrics")
_UNAUTHORIZED_BODY = b'{"error":"Unauthorized"}'


class RouteGuard:
    """Guard HTTP and websocket scopes before the model server handles them."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        key = os.environ.get(KEY_VARIABLE)
        if not key:
            raise RuntimeError(f"{KEY_VARIABLE} is missing or empty")
        self.app = app
        self.key = key.encode("utf-8")

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] not in {"http", "websocket"} or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        root_path = scope.get("root_path", "").rstrip("/")
        if root_path and path.startswith(root_path + "/"):
            path = path[len(root_path) :]
        if path in OPEN_PATHS or self._authorized(scope.get("headers", ())):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "http":
            await self._refuse_http(send, "http.response")
        elif "websocket.http.response" in scope.get("extensions", {}):
            await self._refuse_http(send, "websocket.http.response")
        else:
            await send({"type": "websocket.close", "code": 1008})

    def _authorized(self, headers: list[tuple[bytes, bytes]]) -> bool:
        for name, value in headers:
            if name.lower() != b"authorization":
                continue
            scheme, separator, token = value.partition(b" ")
            return (
                separator == b" "
                and scheme.lower() == b"bearer"
                and secrets.compare_digest(token, self.key)
            )
        return False

    @staticmethod
    async def _refuse_http(
        send: Callable[[dict[str, Any]], Awaitable[None]], prefix: str
    ) -> None:
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(_UNAUTHORIZED_BODY)).encode("ascii")),
            (b"www-authenticate", b"Bearer"),
        ]
        await send({"type": prefix + ".start", "status": 401, "headers": headers})
        await send({"type": prefix + ".body", "body": _UNAUTHORIZED_BODY})
