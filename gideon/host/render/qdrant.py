"""Qdrant identity shared by its service, frontend reader, scrape job, and disk layout.

These readers need the same store name, ports, and data root without importing
one another's render or provisioning code.
"""

from typing import Final

QDRANT_SERVICE_NAME: Final = "qdrant"
QDRANT_REST_PORT: Final = 6333
QDRANT_GRPC_PORT: Final = 6334
QDRANT_METRICS_PORT: Final = 6336
QDRANT_SECRET_NAME: Final = "qdrant_api_key"
QDRANT_READ_ONLY_SECRET_NAME: Final = "qdrant_read_only_api_key"
QDRANT_JOB_NAME: Final = "qdrant"
QDRANT_DATA_ROOT: Final = "/data/fast/qdrant"
QDRANT_STORAGE_MOUNT: Final = "/qdrant/storage"
QDRANT_SNAPSHOTS_ROOT: Final = "/data/fast/qdrant-snapshots"
QDRANT_SNAPSHOTS_MOUNT: Final = "/qdrant/snapshots"
# The server builds a snapshot under "<temp path>/tmp" and renames it into the
# snapshots directory, so both sit on one mount and no copy crosses filesystems.
QDRANT_TEMP_PATH: Final = QDRANT_SNAPSHOTS_MOUNT
QDRANT_SNAPSHOTS_TEMP_DIR: Final = "tmp"


def qdrant_metrics_target() -> str:
    """Return the metrics listener as a Compose host and port."""

    return f"{QDRANT_SERVICE_NAME}:{QDRANT_METRICS_PORT}"


def qdrant_rest_url() -> str:
    """Return HTTP; the pinned client defaults a scheme-less address with a key to HTTPS."""

    return f"http://{QDRANT_SERVICE_NAME}:{QDRANT_REST_PORT}"
