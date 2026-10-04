"""Compose definition for open-webui."""

from collections.abc import Mapping
from typing import Final

from gideon.host.images import RegistryTarget, reference
from gideon.host.render import RenderInputs
from gideon.host.render.owui import owui_environment
from gideon.host.render.services import ServiceDefinition, image_pin

# The published OCI config carries no HEALTHCHECK (OCI drops Docker's), so
# the Dockerfile's check is declared here.
OWUI_HEALTHCHECK: Mapping[str, object] = {
    "test": [
        "CMD-SHELL",
        'curl --silent --fail http://localhost:8080/health | jq -ne "input.status == true"',
    ],
    "interval": "30s",
    "timeout": "10s",
    "retries": 5,
    "start_period": "120s",
}
# The frontend's command: the image declares no entrypoint and its command is
# ``bash start.sh``, whose script hands any container argument to uvicorn
# *in place of* its own defaults, so the two defaults are restated before
# ``--no-access-log``. The flag empties the access logger's
# handlers and stops its propagation, and it holds only because the rendered
# ``AUDIT_UVICORN_LOGGER_NAMES`` (render/owui.py) keeps the frontend's startup
# from re-attaching a handler there — each connection asks that logger for
# handlers once, when it opens. The frontend details are re-read at a frontend
# bump, and the drill's frontend shares the command.
# exempt: no figure — a decision.
OWUI_COMMAND: Final[tuple[str, ...]] = (
    "bash",
    "start.sh",
    "--workers",
    "1",
    "--ws-per-message-deflate",
    "true",
    "--no-access-log",
)
OWUI_ENV_FILE: tuple[Mapping[str, str], ...] = (
    {"path": "/etc/gideon/rendered/open-webui/env", "format": "raw"},
)


class OpenWebuiService(ServiceDefinition):
    """The open-webui service in the Compose project."""

    name = "open-webui"

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        return {
            "image": reference(target, image_pin(inputs, "open-webui")),
            "restart": "unless-stopped",
            "depends_on": {
                "postgres": {"condition": "service_healthy"},
            },
            "env_file": [dict(entry) for entry in OWUI_ENV_FILE],
            "environment": owui_environment(inputs),
            "command": list(OWUI_COMMAND),
            "volumes": [
                "/data/bulk/openwebui:/app/backend/data",
                "/etc/gideon/ca.pem:/etc/gideon/ca.pem:ro",
            ],
            "secrets": ["webui_secret_key"],
            "healthcheck": dict(OWUI_HEALTHCHECK),
            "networks": ["gideon"],
        }
