"""Qdrant identity shared by its service, scrape job, and disk layout.

These readers need the same store name, ports, and data root without importing
one another's render or provisioning code.
"""

from typing import Final

QDRANT_SERVICE_NAME: Final = "qdrant"
QDRANT_REST_PORT: Final = 6333
QDRANT_GRPC_PORT: Final = 6334
QDRANT_METRICS_PORT: Final = 6336
QDRANT_SECRET_NAME: Final = "qdrant_api_key"
QDRANT_JOB_NAME: Final = "qdrant"
QDRANT_DATA_ROOT: Final = "/data/fast/qdrant"
QDRANT_STORAGE_MOUNT: Final = "/qdrant/storage"


def qdrant_metrics_target() -> str:
    """Return the metrics listener as a Compose host and port."""

    return f"{QDRANT_SERVICE_NAME}:{QDRANT_METRICS_PORT}"
