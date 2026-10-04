"""Compose definition for node-exporter."""

from collections.abc import Mapping
from typing import Final

from gideon.host.images import RegistryTarget, reference
from gideon.host.render import RenderInputs
from gideon.host.render.services import ServiceDefinition, image_pin

# The systemd collector's units: the registry and runner exist on the build
# box alone, so every other host collects GIDEON's.
BUILD_BOX_UNIT_PATTERN: Final = (
    r"^(gideon-registry|actions\.runner\..+|gideon-.*)\.service$"
)
UNIT_PATTERN: Final = r"^gideon-.*\.service$"


class NodeExporterService(ServiceDefinition):
    """The node-exporter service in the Compose project."""

    name = "node-exporter"

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        return {
            "image": reference(target, image_pin(inputs, "node-exporter")),
            "restart": "unless-stopped",
            "environment": {"TZ": inputs.site.office.timezone},
            "pid": "host",
            "command": [
                "--path.rootfs=/host",
                "--collector.systemd",
                "--collector.systemd.unit-include="
                + (BUILD_BOX_UNIT_PATTERN if inputs.build_box else UNIT_PATTERN),
            ],
            "volumes": [
                "/:/host:ro,rslave",
                "/run/dbus/system_bus_socket:/var/run/dbus/system_bus_socket:ro",
            ],
            # Ubuntu's D-Bus daemon mediates callers by AppArmor label and
            # Docker's default profile carries no D-Bus rules, so the
            # systemd collector is refused under it. Nothing here is
            # published, and every mount is read-only.
            "security_opt": ["apparmor=unconfined"],
            "networks": ["gideon"],
        }
