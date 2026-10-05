"""Compose definition for gideon-api."""

from collections.abc import Mapping
from typing import Final

from gideon.host.images import RegistryTarget, reference
from gideon.host.render import RenderInputs
from gideon.host.render.api import (
    API_CHAT_HEADER,
    API_HEALTH_PATH,
    API_IMAGE_NAME,
    API_INSTRUCTION_MOUNT,
    API_INSTRUCTION_PATH,
    API_MOUNT_TARGET,
    API_SECRET_NAME,
    API_SERVICE_NAME,
    API_SOURCE_HEADER,
    API_WORKING_DIRECTORY,
    api_enabled,
)
from gideon.host.render.engine import ENGINE_PORT, ENGINE_SECRET_NAME, engine_base_url
from gideon.host.render.owui import EVAL_IDENTITY, GENERAL_MODEL_ID
from gideon.host.render.services import ServiceDefinition, image_pin, source_digest
from gideon.host.render.services.generator import generator_pin

API_SOURCES_DIGEST_LABEL: Final = "org.gideon.api-sources-digest"

# The API image carries Python but no curl, so its healthcheck uses the
# standard-library client; these are SearXNG's starting bounds. exempt: the
# service is expected to become ready in a second or two.
API_HEALTHCHECK: Final[Mapping[str, object]] = {
    "test": [
        "CMD",
        "python",
        "-c",
        "import urllib.request; urllib.request.urlopen("
        f"'http://127.0.0.1:{ENGINE_PORT}{API_HEALTH_PATH}', timeout=4).read()",
    ],
    "interval": "30s",
    "timeout": "5s",
    "retries": 3,
    "start_period": "30s",
}


def api_service(
    inputs: RenderInputs,
    target: RegistryTarget,
    *,
    rendered_root: str = "/etc/gideon/rendered",
) -> Mapping[str, object]:
    """Build the API service from the applying checkout's mounted package.

    It has no ports: callers reach it on the Compose network. The read-only
    checkout mount is the tree that applied this render. A change to the
    mounted instruction or source-digest label recreates the service;
    General's model id and the engine's served name move its block.
    """

    if not inputs.checkout:
        raise ValueError(
            "Render input checkout is empty; the gideon-api service needs the "
            "release checkout's absolute path. Re-run render with a checkout."
        )
    return {
        "image": reference(target, image_pin(inputs, API_IMAGE_NAME)),
        "restart": "unless-stopped",
        "environment": {
            "GIDEON_ENGINE_URL": engine_base_url(),
            "GIDEON_ENGINE_API_KEY_FILE": f"/run/secrets/{ENGINE_SECRET_NAME}",
            "GIDEON_API_KEY_FILE": f"/run/secrets/{API_SECRET_NAME}",
            "GIDEON_INSTRUCTION_FILE": API_INSTRUCTION_MOUNT,
            "GIDEON_API_PORT": str(ENGINE_PORT),
            "GIDEON_SOURCE_HEADER": API_SOURCE_HEADER,
            "GIDEON_EVAL_IDENTITY": EVAL_IDENTITY.email,
            "GIDEON_CHAT_HEADER": API_CHAT_HEADER,
            "GIDEON_MODEL_ID": GENERAL_MODEL_ID,
            "GIDEON_ENGINE_MODEL": generator_pin(inputs).serve.served_name,
            "TZ": inputs.site.office.timezone,
        },
        "command": ["python", "-m", "gideon.api"],
        "working_dir": API_WORKING_DIRECTORY,
        "volumes": [
            f"{inputs.checkout}/gideon:{API_MOUNT_TARGET}:ro",
            f"{rendered_root}/{API_INSTRUCTION_PATH}:{API_INSTRUCTION_MOUNT}:ro",
        ],
        "group_add": [str(inputs.facts.service_gid)],
        "secrets": [
            ENGINE_SECRET_NAME,
            API_SECRET_NAME,
            "postgres_gideon_audit_password",
        ],
        "labels": {API_SOURCES_DIGEST_LABEL: source_digest(inputs, API_SERVICE_NAME)},
        "healthcheck": dict(API_HEALTHCHECK),
        "networks": ["gideon"],
    }


class ApiService(ServiceDefinition):
    """The gideon-api service and its mounted API and guardrail sources.

    A change to either declared source moves the block's digest label.
    """

    name = API_SERVICE_NAME
    sources = ("gideon/api", "gideon/guardrail")

    def applies(self, inputs: RenderInputs) -> bool:
        return api_enabled(inputs.no_gpu)

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        return api_service(inputs, target)
