"""Compose definition for the queue worker."""

from collections.abc import Mapping
from typing import Final

from gideon.host import weights
from gideon.host.cas import ROOT as CAS_ROOT
from gideon.host.images import RegistryTarget, reference
from gideon.host.render import RenderInputs
from gideon.host.render.api import (
    API_IMAGE_NAME,
    API_MOUNT_TARGET,
    API_WORKING_DIRECTORY,
)
from gideon.host.render.egress import (
    INTERNAL_NETWORK_NAME,
    INTERNAL_NO_PROXY_HOSTS,
    egress_proxy_url,
)
from gideon.host.render.engine import EMBED, EMBED_SECRET_NAME
from gideon.host.render.opensearch import (
    OPENSEARCH_PASSWORD_SECRET_NAME,
    OPENSEARCH_REST_PORT,
    OPENSEARCH_SERVICE_NAME,
    OPENSEARCH_USER,
)
from gideon.host.render.qdrant import QDRANT_SECRET_NAME, qdrant_rest_url
from gideon.host.render.services import ServiceDefinition, image_pin, source_digest
from gideon.host.render.worker import (
    CONCURRENCY_ENV,
    CORPUS_DATABASE_HOST_ENV,
    CORPUS_DATABASE_NAME_ENV,
    CORPUS_DATABASE_PORT_ENV,
    CORPUS_DATABASE_ROLE_ENV,
    CORPUS_PASSWORD_FILE_ENV,
    DATABASE_HOST_ENV,
    DATABASE_NAME_ENV,
    DATABASE_PORT_ENV,
    DATABASE_ROLE_ENV,
    EMBED_KEY_FILE_ENV,
    EMBED_URL_ENV,
    OPENSEARCH_PASSWORD_FILE_ENV,
    OPENSEARCH_URL_ENV,
    OPENSEARCH_USER_ENV,
    PASSWORD_FILE_ENV,
    QDRANT_KEY_FILE_ENV,
    QDRANT_URL_ENV,
    SNAPSHOTS_ROOT,
    WORK_ROOT,
    WORKER_CONCURRENCY,
    WORKER_DATABASE_HOST,
    WORKER_DATABASE_NAME,
    WORKER_DATABASE_PORT,
    WORKER_ROLE,
    WORKER_SECRET_NAME,
    WORKER_SERVICE_NAME,
)

WORKER_SOURCES_DIGEST_LABEL: Final = "org.gideon.worker-sources-digest"

# exempt: these bounds allow a database round trip before the next probe.
WORKER_HEALTHCHECK: Final[Mapping[str, object]] = {
    "test": ["CMD", "python", "-m", "gideon.worker.health"],
    "interval": "30s",
    "timeout": "10s",
    "retries": 3,
    "start_period": "30s",
}


class WorkerService(ServiceDefinition):
    """The queue worker writes snapshots, staged files, and stored objects."""

    name = WORKER_SERVICE_NAME
    sources = (
        "gideon/worker", "gideon/host/cas.py", "gideon/host/sysio.py",
        "gideon/host/report.py", "gideon/casecite", "gideon/extraction",
        "gideon/host/index/sample.py",
    )

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        if not inputs.checkout:
            raise ValueError(
                "Render input checkout is empty; the worker needs the release "
                "checkout's absolute path. Re-run render with a checkout."
            )
        return {
            "image": reference(target, image_pin(inputs, API_IMAGE_NAME)),
            "restart": "unless-stopped",
            "command": ["python", "-m", "gideon.worker"],
            "working_dir": API_WORKING_DIRECTORY,
            "volumes": [
                f"{inputs.checkout}/gideon:{API_MOUNT_TARGET}:ro",
                f"{SNAPSHOTS_ROOT}:{SNAPSHOTS_ROOT}",
                f"{WORK_ROOT}:{WORK_ROOT}",
                f"{CAS_ROOT}:{CAS_ROOT}",
                f"{weights.MODELS_ROOT}:{weights.MODELS_ROOT}:ro",
            ],
            "read_only": True,
            "group_add": [str(inputs.facts.service_gid)],
            "secrets": [
                WORKER_SECRET_NAME, OPENSEARCH_PASSWORD_SECRET_NAME,
                QDRANT_SECRET_NAME, EMBED_SECRET_NAME,
            ],
            "environment": {
                DATABASE_HOST_ENV: WORKER_DATABASE_HOST,
                DATABASE_PORT_ENV: str(WORKER_DATABASE_PORT),
                DATABASE_NAME_ENV: WORKER_DATABASE_NAME,
                DATABASE_ROLE_ENV: WORKER_ROLE,
                PASSWORD_FILE_ENV: f"/run/secrets/{WORKER_SECRET_NAME}",
                CORPUS_DATABASE_HOST_ENV: WORKER_DATABASE_HOST,
                CORPUS_DATABASE_PORT_ENV: str(WORKER_DATABASE_PORT),
                CORPUS_DATABASE_NAME_ENV: WORKER_DATABASE_NAME,
                CORPUS_DATABASE_ROLE_ENV: WORKER_ROLE,
                CORPUS_PASSWORD_FILE_ENV: f"/run/secrets/{WORKER_SECRET_NAME}",
                CONCURRENCY_ENV: str(WORKER_CONCURRENCY),
                OPENSEARCH_URL_ENV: f"http://{OPENSEARCH_SERVICE_NAME}:{OPENSEARCH_REST_PORT}",
                OPENSEARCH_USER_ENV: OPENSEARCH_USER,
                OPENSEARCH_PASSWORD_FILE_ENV: f"/run/secrets/{OPENSEARCH_PASSWORD_SECRET_NAME}",
                QDRANT_URL_ENV: qdrant_rest_url(),
                QDRANT_KEY_FILE_ENV: f"/run/secrets/{QDRANT_SECRET_NAME}",
                EMBED_URL_ENV: f"http://{EMBED.service_name}:{EMBED.port}",
                EMBED_KEY_FILE_ENV: f"/run/secrets/{EMBED_SECRET_NAME}",
                "HTTPS_PROXY": egress_proxy_url(),
                "HTTP_PROXY": egress_proxy_url(),
                "NO_PROXY": ",".join(
                    name for name in INTERNAL_NO_PROXY_HOSTS
                    if name != EMBED.service_name or not inputs.no_gpu
                ),
                "TZ": inputs.site.office.timezone,
            },
            "labels": {
                WORKER_SOURCES_DIGEST_LABEL: source_digest(inputs, self.name)
            },
            # exempt: above an idle worker's few seconds to unregister, below the
            # thirty-second stall window, so a job outliving a stop dies with its
            # process before another worker could read its silence as a stall.
            "stop_grace_period": "20s",
            "healthcheck": dict(WORKER_HEALTHCHECK),
            "networks": [INTERNAL_NETWORK_NAME],
        }
