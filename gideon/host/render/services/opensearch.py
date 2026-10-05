"""Compose definition for the single OpenSearch lexical store."""

from collections.abc import Mapping
from typing import Final

from gideon.host.images import RegistryTarget, reference
from gideon.host.models import GIGABYTE
from gideon.host.render import RenderInputs
from gideon.host.render.opensearch import (
    OPENSEARCH_CERT_SECRET_NAME,
    OPENSEARCH_DATA_MOUNT,
    OPENSEARCH_DATA_ROOT,
    OPENSEARCH_KEY_SECRET_NAME,
    OPENSEARCH_NO_GPU_HEAP_MIB,
    OPENSEARCH_PASSWORD_SECRET_NAME,
    OPENSEARCH_PASSWORD_VARIABLE,
    OPENSEARCH_SECURITY_PATH,
    OPENSEARCH_SERVICE_NAME,
    OPENSEARCH_SETTINGS_PATH,
    opensearch_health_url,
    opensearch_heap_mib,
)
from gideon.host.render.services import (
    MountedSecret,
    ServiceDefinition,
    image_pin,
    secret_wrapper,
)

# exempt: the health bounds give the observed roughly 11-second warm start
# room for a cold container while still marking a failed init unhealthy.
OPENSEARCH_HEALTHCHECK: Final[Mapping[str, object]] = {
    "test": [
        "CMD",
        "curl",
        "-fsS",
        "-o",
        "/dev/null",
        opensearch_health_url("127.0.0.1"),
    ],
    "interval": "30s",
    "timeout": "5s",
    "retries": 3,
    "start_period": "30s",
}
# exempt: upstream documents 65536 as the open-file floor for the node.
_NOFILE_LIMIT: Final = 65536


class OpensearchService(ServiceDefinition):
    """The network-only lexical store, outside apply's record-store tier."""

    name = OPENSEARCH_SERVICE_NAME
    swap = False

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        row = inputs.profile.memory_row(self.name)
        if row is None:
            raise ValueError(
                f"Cannot render Compose: profile {inputs.profile.name} has no memory row for "
                f"service '{self.name}'. Add memory.{self.name}.gb to models.lock, then re-run render."
            )
        heap_mib = (
            OPENSEARCH_NO_GPU_HEAP_MIB
            if inputs.no_gpu
            else opensearch_heap_mib(row.gb * GIGABYTE)
        )
        return {
            "image": reference(target, image_pin(inputs, self.name)),
            "restart": "unless-stopped",
            "environment": {
                "TZ": inputs.site.office.timezone,
                # The build ARG does not become an image ENV; without this the
                # entrypoint installs upstream's demo users and certificates.
                "DISABLE_INSTALL_DEMO_CONFIG": "true",
                # The bundled background agent has no runnable main class.
                "DISABLE_PERFORMANCE_ANALYZER_AGENT_CLI": "true",
                # OpenSearch touches the full minimum heap at startup; the
                # final flag overrides the image's heap-dump-on-OOM setting.
                "OPENSEARCH_JAVA_OPTS": (
                    f"-Xms{heap_mib}m -Xmx{heap_mib}m -XX:-HeapDumpOnOutOfMemoryError"
                ),
            },
            "entrypoint": secret_wrapper(
                (
                    MountedSecret(
                        f"/run/secrets/{OPENSEARCH_PASSWORD_SECRET_NAME}",
                        OPENSEARCH_PASSWORD_VARIABLE,
                        "OpenSearch password",
                    ),
                ),
                "./opensearch-docker-entrypoint.sh",
                self.name,
            ),
            # A Compose entrypoint override drops the image CMD; the image's
            # entrypoint execs this argument after its own setup.
            "command": ["opensearch"],
            "volumes": [
                f"{OPENSEARCH_DATA_ROOT}:{OPENSEARCH_DATA_MOUNT}",
                f"/etc/gideon/rendered/opensearch/opensearch.yml:{OPENSEARCH_SETTINGS_PATH}:ro",
                f"/etc/gideon/rendered/opensearch/security:{OPENSEARCH_SECURITY_PATH}:ro",
            ],
            "secrets": [
                OPENSEARCH_PASSWORD_SECRET_NAME,
                OPENSEARCH_KEY_SECRET_NAME,
                OPENSEARCH_CERT_SECRET_NAME,
            ],
            "group_add": [str(inputs.facts.service_gid)],
            "ulimits": {"nofile": {"soft": _NOFILE_LIMIT, "hard": _NOFILE_LIMIT}},
            "healthcheck": dict(OPENSEARCH_HEALTHCHECK),
            "networks": ["gideon"],
        }
