"""The Starlette application and request boundary for the API service."""

import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Match, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gideon.turn import objects
from gideon.turn.endpoint import TurnClients, TurnEndpoint
from gideon.turn.generation import Connect

from .auth import BearerAuthMiddleware
from .model import model_listing
from .relay import UPSTREAM_ERROR, CompletionRelay
from .settings import Settings
from .upstream import EngineClient

_HEALTH_PATH = "/health"
_MODELS_PATH = "/v1/models"
_COMPLETIONS_PATH = "/v1/chat/completions"
_TURN_PATH = "/turn"
_REQUEST_LOG = logging.getLogger("gideon.api.request")
_APP_LOG = logging.getLogger("gideon.api.app")
_STARTUP_LOG = logging.getLogger("gideon.api.startup")

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
    """List General when the engine confirms its served model is available."""

    result = await request.app.state.engine.list_models()
    if result is None:
        return JSONResponse(UPSTREAM_ERROR, status_code=502)
    if result.status_code == 200:
        listing = model_listing(
            result.content, request.app.state.engine_model, request.app.state.model_id
        )
        if listing is None:
            _APP_LOG.warning("upstream model listing unavailable")
            return JSONResponse(UPSTREAM_ERROR, status_code=502)
        return Response(content=listing, status_code=200, media_type="application/json")
    headers = {}
    if result.content_type is not None:
        headers["content-type"] = result.content_type
    return Response(content=result.content, status_code=result.status_code, headers=headers)


def create_app(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    embed_transport: httpx.AsyncBaseTransport | None = None,
    qdrant_transport: httpx.AsyncBaseTransport | None = None,
    opensearch_transport: httpx.AsyncBaseTransport | None = None,
    record_connect: Connect | None = None,
    case_extractor: objects.CaseExtractor | None = None,
    turn_clock: Callable[[], float] = time.monotonic,
) -> Starlette:
    """Build the service application and manage its upstream clients."""

    @asynccontextmanager
    async def lifespan(application: Starlette) -> AsyncIterator[None]:
        extractor = case_extractor
        if extractor is None:
            try:
                extractor = objects.load_case_extractor()
            except Exception:  # noqa: BLE001 - Legal extraction must never stop service startup.
                extractor = None
        application.state.case_extractor = extractor
        if extractor is None:
            _STARTUP_LOG.warning("Could not load gideon.casecite.adapter")

        async with AsyncExitStack() as resources:
            engine = EngineClient(settings, transport=transport)
            resources.push_async_callback(engine.aclose)
            application.state.engine = engine
            application.state.model_id = settings.model_id
            application.state.engine_model = settings.engine_model

            async def turn_client(url: str, transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
                return await resources.enter_async_context(httpx.AsyncClient(
                    base_url=url, transport=transport, trust_env=False, follow_redirects=False, timeout=None,
                ))

            application.state.turn_clients = TurnClients(
                await turn_client(settings.embed_url, embed_transport),
                await turn_client(settings.qdrant_url, qdrant_transport),
                await turn_client(settings.opensearch_url, opensearch_transport),
            )
            yield

    routes = [
        Route(_HEALTH_PATH, health, methods=["GET"]),
        Route(_MODELS_PATH, models, methods=["GET"]),
        # An instance, not a function: Starlette calls a non-function endpoint
        # as an ASGI application, which is the relay's shape.
        Route(
            _COMPLETIONS_PATH,
            CompletionRelay(settings),
            methods=["POST"],
        ),
        Route(_TURN_PATH, TurnEndpoint(settings, connect=record_connect, clock=turn_clock), methods=["POST"]),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(BearerAuthMiddleware, key=settings.api_key, health_path=_HEALTH_PATH)
    # Added last, so outermost: a refused request is logged too.
    app.add_middleware(RequestLogMiddleware, routes=routes)
    return app
