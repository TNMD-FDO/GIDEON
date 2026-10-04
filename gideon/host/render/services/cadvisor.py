"""Compose definition for cadvisor."""

from collections.abc import Mapping

from gideon.host.images import RegistryTarget, reference
from gideon.host.render import RenderInputs
from gideon.host.render.services import ServiceDefinition, image_pin


class CadvisorService(ServiceDefinition):
    """The cadvisor service in the Compose project."""

    name = "cadvisor"

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        return {
            "image": reference(target, image_pin(inputs, "cadvisor")),
            "restart": "unless-stopped",
            "environment": {"TZ": inputs.site.office.timezone},
            "privileged": True,
            "command": [
                "--docker_only=true",
                "--housekeeping_interval=30s",
                "--disable_metrics=advtcp,app,cpu_topology,cpuset,hugetlb,memory_numa,perf_event,process,referenced_memory,resctrl,sched,tcp,udp",
            ],
            "volumes": [
                "/:/rootfs:ro",
                "/var/run:/var/run:rw",
                "/sys:/sys:ro",
                "/var/lib/docker:/var/lib/docker:ro",
                "/dev/disk:/dev/disk:ro",
            ],
            "networks": ["gideon"],
        }
