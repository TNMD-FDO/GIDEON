"""Compose definition for prometheus."""

from collections.abc import Mapping

from gideon.host.images import RegistryTarget, reference
from gideon.host.render import RenderInputs
from gideon.host.render.egress import INTERNAL_NETWORK_NAME
from gideon.host.render.services import ServiceDefinition, image_pin


class PrometheusService(ServiceDefinition):
    """The prometheus service in the Compose project."""

    name = "prometheus"

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        return {
            "image": reference(target, image_pin(inputs, "prometheus")),
            "restart": "unless-stopped",
            "environment": {"TZ": inputs.site.office.timezone},
            "command": [
                "--config.file=/etc/prometheus/prometheus.yml",
                "--storage.tsdb.path=/data/observability/prometheus",
                "--storage.tsdb.retention.time=1y",
                "--web.listen-address=0.0.0.0:9090",
            ],
            "volumes": [
                "/etc/gideon/rendered/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro",
                "/data/observability/prometheus:/data/observability/prometheus",
            ],
            "ports": ["127.0.0.1:9090:9090"],
            # The internal network is for the worker's scrape alone; the
            # worker gains reach to this port, a read-only query API with its
            # admin and lifecycle switches off, and nothing else.
            "networks": ["gideon", INTERNAL_NETWORK_NAME],
        }
