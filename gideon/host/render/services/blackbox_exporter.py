"""Compose definition for blackbox-exporter."""

from collections.abc import Mapping

from gideon.host.images import RegistryTarget, reference
from gideon.host.render import RenderInputs
from gideon.host.render.services import ServiceDefinition, image_pin


class BlackboxExporterService(ServiceDefinition):
    """The blackbox-exporter service in the Compose project."""

    name = "blackbox-exporter"

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        return {
            "image": reference(target, image_pin(inputs, "blackbox-exporter")),
            "restart": "unless-stopped",
            "environment": {"TZ": inputs.site.office.timezone},
            "command": ["--config.file=/etc/blackbox_exporter/config.yml"],
            "volumes": [
                "/etc/gideon/rendered/blackbox/blackbox.yml:/etc/blackbox_exporter/config.yml:ro",
                "/etc/gideon/ca.pem:/etc/gideon/ca.pem:ro",
            ],
            "networks": ["gideon"],
        }
