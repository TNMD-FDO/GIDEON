"""Compose definition for dcgm-exporter."""

from collections.abc import Mapping

from gideon.host.images import RegistryTarget, reference
from gideon.host.render import RenderInputs
from gideon.host.render.prometheus import DCGM_COUNTERS_MOUNT, DCGM_COUNTERS_PATH
from gideon.host.render.services import ServiceDefinition, image_pin


class DcgmExporterService(ServiceDefinition):
    """The dcgm-exporter service in the Compose project."""

    name = "dcgm-exporter"

    def applies(self, inputs: RenderInputs) -> bool:
        return not inputs.no_gpu

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        return {
            "image": reference(target, image_pin(inputs, "dcgm-exporter")),
            "restart": "unless-stopped",
            "environment": {"TZ": inputs.site.office.timezone},
            "command": ["-f", DCGM_COUNTERS_MOUNT],
            "volumes": [
                f"/etc/gideon/rendered/{DCGM_COUNTERS_PATH}:{DCGM_COUNTERS_MOUNT}:ro"
            ],
            "devices": ["nvidia.com/gpu=all"],
            "cap_add": ["SYS_ADMIN"],
            "networks": ["gideon"],
        }
