"""The bounded HTTP client for the upstream serving engine."""

from dataclasses import dataclass
from typing import Final

import httpx

from .settings import Settings

# exempt: upstream connection bound from the HTTP stack research note and the plan.
UPSTREAM_CONNECT_TIMEOUT_SECONDS: Final[float] = 2.0
# exempt: upstream read bound kept below the frontend's ten-second model-list timeout.
UPSTREAM_READ_TIMEOUT_SECONDS: Final[float] = 5.0
# exempt: completion read bound from the host's FRONTEND_TURN_TIMEOUT_SECONDS and the discovery ruling.
UPSTREAM_COMPLETION_READ_TIMEOUT_SECONDS: Final[float] = 600.0


def _bounds(read: float) -> httpx.Timeout:
    """Bound one request: the shared connect, write, and pool, and its own read."""

    # The model list's bound is the positional default, so it is also httpx's
    # write and pool bound, as it was before the completion had a read bound.
    return httpx.Timeout(
        UPSTREAM_READ_TIMEOUT_SECONDS,
        connect=UPSTREAM_CONNECT_TIMEOUT_SECONDS,
        read=read,
    )


@dataclass(frozen=True, slots=True)
class UpstreamResponse:
    """The upstream status and body, without exposing transport diagnostics."""

    status_code: int
    content: bytes
    content_type: str | None


class EngineClient:
    """Call the engine's OpenAI-compatible API under its ``/v1`` base URL."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # A completion is silent until it is finished, so it keeps the client's
        # connect, write, and pool bounds and lengthens the read bound alone.
        self._completion_timeout = _bounds(UPSTREAM_COMPLETION_READ_TIMEOUT_SECONDS)
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {settings.engine_api_key}"},
            timeout=_bounds(UPSTREAM_READ_TIMEOUT_SECONDS),
            transport=transport,
            trust_env=False,
        )
        self._engine_url = settings.engine_url

    async def list_models(self) -> UpstreamResponse | None:
        """Return the engine response, or ``None`` for a transport failure."""

        try:
            response = await self._client.get(f"{self._engine_url}/models")
        except httpx.RequestError:
            return None
        return UpstreamResponse(
            status_code=response.status_code,
            content=response.content,
            content_type=response.headers.get("content-type"),
        )

    async def completion(self, body: bytes, content_type: str | None) -> httpx.Response | None:
        """Open a completion response; the caller owns closing the returned response."""

        headers = {} if content_type is None else {"content-type": content_type}
        request = self._client.build_request(
            "POST",
            f"{self._engine_url}/chat/completions",
            content=body,
            headers=headers,
            timeout=self._completion_timeout,
        )
        try:
            return await self._client.send(request, stream=True)
        except httpx.RequestError:
            return None

    async def aclose(self) -> None:
        """Close the shared HTTP client."""

        await self._client.aclose()
