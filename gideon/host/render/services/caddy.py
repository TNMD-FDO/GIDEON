"""Compose definition for caddy."""

from collections.abc import Mapping

from gideon.host.images import RegistryTarget, reference
from gideon.host.render import RenderInputs
from gideon.host.render.services import ServiceDefinition, image_pin


class CaddyService(ServiceDefinition):
    """The caddy service in the Compose project."""

    name = "caddy"

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        return {
            "image": reference(target, image_pin(inputs, "caddy")),
            "restart": "unless-stopped",
            "environment": {
                "TZ": inputs.site.office.timezone,
            },
            "ports": ["0.0.0.0:443:443"],
            "volumes": [
                "/etc/gideon/rendered/caddy/Caddyfile:/etc/caddy/Caddyfile:ro",
                "/etc/gideon/tls:/etc/gideon/tls:ro",
                "caddy_data:/data",
                "caddy_config:/config",
            ],
            "secrets": ["tls_key"],
            "networks": ["gideon"],
        }
