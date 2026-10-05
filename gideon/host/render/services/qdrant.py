"""Compose definition for qdrant."""

from collections.abc import Mapping
from typing import Final

from gideon.host.images import RegistryTarget, reference
from gideon.host.render import RenderInputs
from gideon.host.render.qdrant import (
    QDRANT_DATA_ROOT,
    QDRANT_METRICS_PORT,
    QDRANT_REST_PORT,
    QDRANT_SECRET_NAME,
    QDRANT_SERVICE_NAME,
    QDRANT_STORAGE_MOUNT,
)
from gideon.host.render.services import ServiceDefinition, image_pin, secret_wrapper

# The image has no healthcheck, curl, or wget. Bash asks /readyz through its
# own TCP facility and requires a 200 status line; the check holds no dollar
# sign, which Compose would read as a variable. These bounds are starting
# values for the server's startup time.
QDRANT_HEALTHCHECK: Final[Mapping[str, object]] = {
    "test": [
        "CMD",
        "bash",
        "-c",
        f"exec 3<>/dev/tcp/127.0.0.1/{QDRANT_REST_PORT}; "
        "printf 'GET /readyz HTTP/1.0\\r\\n\\r\\n' >&3; "
        "head -n 1 <&3 | grep -q ' 200 '",
    ],
    "interval": "30s",
    "timeout": "5s",
    "retries": 3,
    "start_period": "30s",
}


class QdrantService(ServiceDefinition):
    """The qdrant service in the Compose project."""

    name = QDRANT_SERVICE_NAME
    swap = False

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        return {
            "image": reference(target, image_pin(inputs, self.name)),
            "restart": "unless-stopped",
            "environment": {
                "QDRANT__TELEMETRY_DISABLED": "true",
                "QDRANT__SERVICE__ENABLE_STATIC_CONTENT": "false",
                "QDRANT__SERVICE__METRICS_PORT": str(QDRANT_METRICS_PORT),
                "TZ": inputs.site.office.timezone,
            },
            "entrypoint": secret_wrapper(
                f"/run/secrets/{QDRANT_SECRET_NAME}",
                "QDRANT__SERVICE__API_KEY",
                "qdrant API key",
                "./qdrant",
                self.name,
            ),
            "volumes": [f"{QDRANT_DATA_ROOT}:{QDRANT_STORAGE_MOUNT}"],
            "secrets": [QDRANT_SECRET_NAME],
            "healthcheck": dict(QDRANT_HEALTHCHECK),
            "networks": ["gideon"],
        }
