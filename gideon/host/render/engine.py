"""The engine's identity as its clients see it.

The service name (the stable alias; the profile's served name is held equal to
it by a test), the container port, and the secret name are
shared by the Compose service in ``render/compose.py`` and by the frontend's
connection in ``render/owui.py``; they live here so the frontend module can
name the engine without importing the document builder that imports it. The
scrape job's name and the metrics target are the same identity as Prometheus's
configuration and Grafana's "Engine down" rule see it (``render/prometheus.py``,
``render/grafana.py``).
"""

from typing import Final

ENGINE_SERVICE_NAME: Final = "gideon-generator"
ENGINE_SECRET_NAME: Final = "engine_api_key"
ENGINE_PORT: Final = 8000
ENGINE_JOB_NAME: Final = "engine"


def engine_base_url() -> str:
    """Return the engine's OpenAI-compatible API base URL."""

    return f"http://{ENGINE_SERVICE_NAME}:{ENGINE_PORT}/v1"


def engine_metrics_target() -> str:
    """Return the engine's Prometheus target as a Compose host and port."""

    return f"{ENGINE_SERVICE_NAME}:{ENGINE_PORT}"
