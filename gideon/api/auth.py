"""The pure ASGI bearer-key middleware for the service routes."""

import secrets
from collections.abc import Iterable

from starlette.types import ASGIApp, Receive, Scope, Send

_UNAUTHORIZED_BODY = (
    b'{"error":{"message":"Incorrect API key provided.",'
    b'"type":"invalid_request_error","param":null,"code":"invalid_api_key"}}'
)


class BearerAuthMiddleware:
    """Require the configured bearer key on every path except health."""

    def __init__(self, app: ASGIApp, key: str, health_path: str) -> None:
        self.app = app
        # The header arrives as bytes and is compared as bytes: a non-ASCII
        # token would make a string comparison raise rather than refuse.
        self.key = key.encode("utf-8")
        self.health_path = health_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == self.health_path:
            await self.app(scope, receive, send)
            return
        presented = self._authorization_value(scope.get("headers", ()))
        if presented is None or not secrets.compare_digest(presented, self.key):
            await self._refuse(send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _authorization_value(headers: Iterable[tuple[bytes, bytes]]) -> bytes | None:
        for name, value in headers:
            if name.lower() != b"authorization":
                continue
            scheme, separator, token = value.partition(b" ")
            if separator != b" " or scheme.lower() != b"bearer" or not token:
                return None
            return token
        return None

    @staticmethod
    async def _refuse(send: Send) -> None:
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(_UNAUTHORIZED_BODY)).encode("ascii")),
            (b"www-authenticate", b"Bearer"),
        ]
        await send({"type": "http.response.start", "status": 401, "headers": headers})
        await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})
