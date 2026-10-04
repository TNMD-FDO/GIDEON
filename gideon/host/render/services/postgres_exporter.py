"""Compose definition for postgres-exporter."""

from collections.abc import Mapping

from gideon.host.images import RegistryTarget, reference
from gideon.host.render import RenderInputs
from gideon.host.render.services import ServiceDefinition, image_pin


class PostgresExporterService(ServiceDefinition):
    """The postgres-exporter service in the Compose project."""

    name = "postgres-exporter"

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        return {
            "image": reference(target, image_pin(inputs, "postgres-exporter")),
            "restart": "unless-stopped",
            "environment": {
                "DATA_SOURCE_URI": "postgres:5432/gideon?sslmode=disable",
                "DATA_SOURCE_USER": "gideon_ro_metrics",
                "DATA_SOURCE_PASS_FILE": "/run/secrets/postgres_gideon_ro_metrics_password",
                "TZ": inputs.site.office.timezone,
            },
            "group_add": [str(inputs.facts.service_gid)],
            "depends_on": {
                "postgres": {"condition": "service_healthy"},
            },
            "secrets": ["postgres_gideon_ro_metrics_password"],
            "networks": ["gideon"],
        }
