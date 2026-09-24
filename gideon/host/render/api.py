"""The ``gideon-api`` service's identity as its clients see it.

The Compose block in ``render/compose.py``, the blackbox job in
``render/prometheus.py``, and the probe rule in ``render/grafana.py`` share
these names, as ``render/engine.py`` holds the engine's, so neither observer
imports the document builder. The service listens on the engine's port number
and the frontend's connection names the service by hostname alone.
The API Compose block reads the email header name through ``API_SOURCE_HEADER``
for the source word and the chat id through ``API_CHAT_HEADER`` for the trip row;
it is the one rendered artifact that reads forwarded header names.
``API_SOURCES`` is the code the container imports from the mounted checkout,
including the shared guardrail judge: its digest is the block's label, so a
change there recreates the service and nothing else.
"""

from typing import Final

from gideon.host.render.engine import ENGINE_PORT

API_SERVICE_NAME: Final[str] = "gideon-api"
API_IMAGE_NAME: Final[str] = "gideon"
API_SECRET_NAME: Final[str] = "gideon_api_key"
API_JOB_NAME: Final[str] = "api"
API_HEALTH_PATH: Final[str] = "/health"
API_USER_NAME_HEADER: Final[str] = "X-OpenWebUI-User-Name"
API_USER_EMAIL_HEADER: Final[str] = "X-OpenWebUI-User-Email"
API_USER_ROLE_HEADER: Final[str] = "X-OpenWebUI-User-Role"
API_SOURCE_HEADER: Final[str] = API_USER_EMAIL_HEADER
API_CHAT_HEADER: Final[str] = "X-OpenWebUI-Chat-Id"
API_MOUNT_TARGET: Final[str] = "/opt/gideon-src/gideon"
API_WORKING_DIRECTORY: Final[str] = "/opt/gideon-src"
API_SOURCES: Final[tuple[str, ...]] = ("gideon/api", "gideon/guardrail")


def api_base_url() -> str:
    """Return the API's OpenAI-compatible base URL."""

    return f"http://{API_SERVICE_NAME}:{ENGINE_PORT}/v1"


def api_health_url() -> str:
    """Return the API's Compose-network health endpoint."""

    return f"http://{API_SERVICE_NAME}:{ENGINE_PORT}{API_HEALTH_PATH}"


def api_enabled(no_gpu: bool) -> bool:
    """Return whether the API belongs in this host's rendered stack."""

    return not no_gpu
