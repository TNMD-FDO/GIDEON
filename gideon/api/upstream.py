"""The bounded HTTP client for the upstream serving engine."""

from dataclasses import dataclass
from typing import Final

import httpx

from .settings import Settings

# exempt: upstream connection bound from the HTTP stack research note and the plan.
UPSTREAM_CONNECT_TIMEOUT_SECONDS: Final[float] = 2.0
# exempt: upstream read bound kept below the frontend's ten-second model-list timeout.
UPSTREAM_READ_TIMEOUT_SECONDS: Final[float] = 5.0


@dataclass(frozen=True, slots=True)
class UpstreamResponse:
    """The upstream status and body, without exposing transport diagnostics."""

    status_code: int
    content: bytes
    content_type: str | None


class EngineClient:
    """Call the engine's OpenAI-compatible API under its ``/v1`` base URL."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        timeout = httpx.Timeout(
            UPSTREAM_READ_TIMEOUT_SECONDS,
            connect=UPSTREAM_CONNECT_TIMEOUT_SECONDS,
        )
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {settings.engine_api_key}"},
            timeout=timeout,
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

    async def aclose(self) -> None:
        """Close the shared HTTP client."""

        await self._client.aclose()
