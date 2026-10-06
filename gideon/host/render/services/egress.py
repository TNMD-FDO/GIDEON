"""Compose definition for the corpus egress service."""

from collections.abc import Mapping
from typing import Final

from gideon.host.images import RegistryTarget, reference
from gideon.host.render import RenderInputs
from gideon.host.render.api import (
    API_IMAGE_NAME,
    API_MOUNT_TARGET,
    API_WORKING_DIRECTORY,
)
from gideon.host.render.egress import (
    EGRESS_ENV_FILE,
    EGRESS_GROUP,
    EGRESS_PORT,
    EGRESS_SERVICE_NAME,
    EGRESS_SOURCES_DIGEST_LABEL,
    HOSTS_ENV,
    INTERNAL_NETWORK_NAME,
    PORT_ENV,
)
from gideon.host.render.services import ServiceDefinition, image_pin, source_digest

# exempt: the local health request should finish before the next probe.
EGRESS_HEALTHCHECK: Final[Mapping[str, object]] = {
    "test": [
        "CMD",
        "python",
        "-c",
        "import urllib.request; urllib.request.urlopen("
        f"'http://127.0.0.1:{EGRESS_PORT}/healthz', timeout=4).read()",
    ],
    "interval": "30s",
    "timeout": "5s",
    "retries": 3,
    "start_period": "30s",
}


class EgressService(ServiceDefinition):
    """The mounted egress package on every host."""

    name = EGRESS_SERVICE_NAME
    sources = ("gideon/egress",)

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        if not inputs.checkout:
            raise ValueError(
                "Render input checkout is empty; the egress service needs the "
                "release checkout's absolute path. Re-run render with a checkout."
            )
        if inputs.egress is None:
            raise ValueError(
                "Render input egress is missing for gideon-egress; re-run render "
                "from a release checkout, whose loader reads config/egress.yaml."
            )
        group = inputs.egress.group(EGRESS_GROUP)
        if group is None:
            raise ValueError(
                "Render input egress has no corpus group. Correct config/egress.yaml, "
                "then re-run render."
            )
        block: dict[str, object] = {
            "image": reference(target, image_pin(inputs, API_IMAGE_NAME)),
            "restart": "unless-stopped",
            "command": ["python", "-m", "gideon.egress"],
            "working_dir": API_WORKING_DIRECTORY,
            "volumes": [f"{inputs.checkout}/gideon:{API_MOUNT_TARGET}:ro"],
            "read_only": True,
            "environment": {
                HOSTS_ENV: ",".join(host.host for host in group.hosts),
                PORT_ENV: str(EGRESS_PORT),
                "TZ": inputs.site.office.timezone,
            },
            "labels": {
                EGRESS_SOURCES_DIGEST_LABEL: source_digest(inputs, self.name)
            },
            "healthcheck": dict(EGRESS_HEALTHCHECK),
            "networks": ["gideon", INTERNAL_NETWORK_NAME],
        }
        if inputs.site.egress_proxy:
            block["env_file"] = [dict(entry) for entry in EGRESS_ENV_FILE]
        return block
