"""The Starlette application and request boundary for the API service."""

import logging
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Match, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import BearerAuthMiddleware
from .relay import UPSTREAM_ERROR, CompletionRelay
from .settings import Settings
from .upstream import EngineClient

_HEALTH_PATH = "/health"
_MODELS_PATH = "/v1/models"
_COMPLETIONS_PATH = "/v1/chat/completions"
_REQUEST_LOG = logging.getLogger("gideon.api.request")

_UNMATCHED = "-"


class RequestLogMiddleware:
    """Log method, route template, status, and elapsed seconds without content.

    A response that never started logs ``-`` for its status, and a caller that
    left before the response finished adds ``disconnected``.
    """

    def __init__(self, app: ASGIApp, routes: Sequence[BaseRoute]) -> None:
        self.app = app
        self.routes = routes

    def _template(self, scope: Scope) -> str:
        for route in self.routes:
            match, _ = route.matches(scope)
            if match is not Match.NONE and isinstance(route, Route):
                return route.path
        return _UNMATCHED

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == _HEALTH_PATH:
            await self.app(scope, receive, send)
            return
        status: int | None = None
        response_finished = False
        disconnected = False
        started = time.perf_counter()

        async def capture(message: Message) -> None:
            nonlocal response_finished, status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_finished = True

        async def capture_receive() -> Message:
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect" and not response_finished:
                disconnected = True
            return message

        try:
            await self.app(scope, capture_receive, capture)
        except Exception:
            status = 500
            raise
        finally:
            elapsed = time.perf_counter() - started
            _REQUEST_LOG.info(
                "%s %s %s %.3fs%s",
                scope.get("method", ""),
                self._template(scope),
                "-" if status is None else status,
                elapsed,
                " disconnected" if disconnected else "",
            )


async def health(_: Request) -> JSONResponse:
    """Return the fixed process-health response without contacting the engine."""

    return JSONResponse({"status": "ok"})


async def models(request: Request) -> Response:
    """Pass the engine's model-list status and body through the service."""

    result = await request.app.state.engine.list_models()
    if result is None:
        return JSONResponse(UPSTREAM_ERROR, status_code=502)
    headers = {}
    if result.content_type is not None:
        headers["content-type"] = result.content_type
    return Response(content=result.content, status_code=result.status_code, headers=headers)


def create_app(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    """Build the service application with one shared upstream client."""

    @asynccontextmanager
    async def lifespan(application: Starlette) -> AsyncIterator[None]:
        engine = EngineClient(settings, transport=transport)
        application.state.engine = engine
        try:
            yield
        finally:
            await engine.aclose()

    routes = [
        Route(_HEALTH_PATH, health, methods=["GET"]),
        Route(_MODELS_PATH, models, methods=["GET"]),
        # An instance, not a function: Starlette calls a non-function endpoint
        # as an ASGI application, which is the relay's shape.
        Route(
            _COMPLETIONS_PATH,
            CompletionRelay(
                settings.source_header, settings.chat_header, settings.eval_identity
            ),
            methods=["POST"],
        ),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(BearerAuthMiddleware, key=settings.api_key, health_path=_HEALTH_PATH)
    # Added last, so outermost: a refused request is logged too.
    app.add_middleware(RequestLogMiddleware, routes=routes)
    return app
