"""Pure Compose and Open WebUI artifacts for the standing CI sibling stack.

The relay reaches the engine through the integration network.
"""

import hashlib
from collections.abc import Mapping
from typing import Final

from gideon.host.images import parse_registry, reference
from gideon.host.models import GIGABYTE
from gideon.host.render import RenderInputs
from gideon.host.render.api import API_SECRET_NAME, API_SERVICE_NAME
from gideon.host.render.compose import (
    NETWORK_NAME,
    image_pin,
    integration_network_name,
    memory_limit_bytes,
    mounted_secret_names,
    with_memory_limit,
)
from gideon.host.render.engine import (
    EMBED_SECRET_NAME,
    ENGINE_PORT,
    ENGINE_SECRET_NAME,
    ENGINE_SERVICE_NAME,
    INTEGRATION_NETWORK_NAME,
)
from gideon.host.render.opensearch import (
    OPENSEARCH_DATA_MOUNT,
    OPENSEARCH_SECURITY_PATH,
    OPENSEARCH_SETTINGS_PATH,
)
from gideon.host.render.owui import (
    ApplyManifestArtifact,
    general_instruction_text,
    owui_env_file_text,
    owui_environment,
    owui_secret_environment,
)
from gideon.host.render.qdrant import (
    QDRANT_READ_ONLY_SECRET_NAME,
    QDRANT_STORAGE_MOUNT,
)
from gideon.host.render.searxng import SEARXNG_SECRET_NAME
from gideon.host.render.services.api import ApiService, api_service
from gideon.host.render.services.open_webui import OpenWebuiService
from gideon.host.render.services.opensearch import OpensearchService, opensearch_service
from gideon.host.render.services.postgres import PostgresService
from gideon.host.render.services.qdrant import QdrantService

CI_PROJECT: Final[str] = "gideon-ci"
# The stacks a run's turns drive, production first, the order --stack offers them in;
# here because the bare-host entry chain reads them for its parser.
PRODUCTION_STACK: Final[str] = "production"
CI_STACK: Final[str] = "ci"
STACKS: Final[tuple[str, str]] = (PRODUCTION_STACK, CI_STACK)
CI_ROOT: Final[str] = "/data/ci"
CI_SECRETS_DIR: Final[str] = f"{CI_ROOT}/secrets"
CI_PORT: Final[int] = 18100
CI_BASE_URL: Final[str] = f"http://127.0.0.1:{CI_PORT}"
RELAY_SERVICE_NAME: Final[str] = "engine-relay"
RELAY_SOURCE: Final[str] = "tools/cistack/relay.py"
RELAY_CONTAINER_PATH: Final[str] = "/relay/relay.py"
RELAY_MEMORY_LIMIT: Final[int] = 128 * 1024 * 1024
# The sibling has no recreate rule, only Compose's config diff at ``up -d``, so
# its API block carries the instruction's digest: a changed text moves the block
# and the service never keeps a startup text its mounted file no longer holds.
CI_INSTRUCTION_DIGEST_LABEL: Final[str] = "org.gideon.instruction-digest"
# The sibling's Postgres ceiling in the memory table's unit, decimal gigabytes,
# sized as the table's rows are: the smallest whole gigabyte at least four times
# the service's working-set peak, floor 1. The peak is the maximum by Compose
# project and service of container_memory_working_set_bytes over fourteen days,
# read from the box's cAdvisor series through Prometheus on loopback port 9090:
# 67 MiB on 2026-09-23, so 1. Re-take it the same way; a rise past the box
# ledger's GIDEON line is a ledger change first. A constant, not a table row,
# because the table is held to production's services in both directions and
# the sibling runs on the build box alone.
CI_POSTGRES_MEMORY_GB: Final[int] = 1
# The sibling's Qdrant ceiling, sized as the Postgres one above: the smallest
# whole gigabyte at least four times the working-set peak, floor 1, the peak
# read the same way: 405 MiB on 2026-10-05, over an empty store through one
# smoke run, so 2. Re-take it once the sibling's store holds a generation.
# A constant for the Postgres one's reason.
# exempt: a measured starting value, corrected by the table's rule.
CI_QDRANT_MEMORY_GB: Final[int] = 2
# The sibling's OpenSearch ceiling. Its heap is half the limit and touched at
# start, so four-fold headroom applies to what it holds beside the heap: the
# smallest whole gigabyte whose non-heap half is at least four times the
# off-heap peak, floor 1, the off-heap peak being the working-set peak less
# the heap in force when it was read. The off-heap part grows with the heap
# (the collector's structures), so the rule is re-applied at each new figure
# until it holds. Read as the Postgres one's, over an empty index, on
# 2026-10-05: 1486 MiB through one smoke run under a 2 GB ceiling's 953 MiB
# heap, 534 MiB off-heap, so 5; 3005 MiB under 5 GB's 2384 MiB heap, 621 MiB
# off-heap, four times 2.61 GB, so 6; 3532 MiB under 6 GB's 2861 MiB heap,
# 671 MiB off-heap, four times 2.82 GB, which 6 holds. Re-take it once the
# sibling's index holds a generation. A constant for the Postgres one's reason.
# exempt: a measured starting value, corrected by the stated rule.
CI_OPENSEARCH_MEMORY_GB: Final[int] = 6
CI_QDRANT_DATA_ROOT: Final[str] = f"{CI_ROOT}/qdrant"
CI_OPENSEARCH_DATA_ROOT: Final[str] = f"{CI_ROOT}/opensearch-data"
CI_OPENSEARCH_RENDERED_DIR: Final[str] = f"{CI_ROOT}/opensearch"
# The sibling env file's reads on a GPU host with the directory off: what
# ``tools.cistack`` loads from the sibling's secrets directory, no supplied one.
CI_SECRET_NAMES: Final[tuple[str, ...]] = (
    "postgres_openwebui_password",
    "gideon_admin_password",
    API_SECRET_NAME,
    QDRANT_READ_ONLY_SECRET_NAME,
)
# The sibling mints its own lexical-store password and transport pair. It skips
# the two model-server keys (production's engine key is mounted by path),
# Grafana's password, and SearXNG's key; none has a sibling consumer.
CI_SKIPPED_SECRETS: Final[tuple[str, ...]] = (
    ENGINE_SECRET_NAME,
    EMBED_SECRET_NAME,
    "grafana_admin_password",
    SEARXNG_SECRET_NAME,
)
CI_WIPE_PATHS: Final[tuple[str, ...]] = (
    f"{CI_ROOT}/postgres",
    f"{CI_ROOT}/openwebui",
    CI_QDRANT_DATA_ROOT,
    CI_OPENSEARCH_DATA_ROOT,
    CI_OPENSEARCH_RENDERED_DIR,
    f"{CI_ROOT}/open-webui",
    f"{CI_ROOT}/{API_SERVICE_NAME}",
    f"{CI_ROOT}/compose.yaml",
    f"{CI_SECRETS_DIR}/gideon_admin_api_key",
    f"{CI_SECRETS_DIR}/gideon_eval_api_key",
)


def _relay_base_url() -> str:
    return f"http://{RELAY_SERVICE_NAME}:{ENGINE_PORT}/v1"


def _ci_secret_file(name: str) -> str:
    if name == ENGINE_SECRET_NAME:
        return f"/etc/gideon/secrets/{name}"
    return f"{CI_SECRETS_DIR}/{name}"


def ci_compose_document(inputs: RenderInputs) -> Mapping[str, object]:
    """Build the sibling's document from the service definitions, stating its differences.

    Each block is its definition's with only the keys the sibling changes
    replaced or dropped; nothing here touches the host.
    """

    target = parse_registry(inputs.site.registry)
    if target is None:
        raise ValueError(
            "Cannot render CI Compose: the registry key is not usable. "
            "Correct registry in /etc/gideon/site.yaml, then re-run render."
        )

    postgres = dict(PostgresService().block(inputs, target))
    postgres["volumes"] = [f"{CI_ROOT}/postgres:/var/lib/postgresql"]
    postgres["networks"] = [NETWORK_NAME]
    del postgres["command"]

    open_webui = dict(OpenWebuiService().block(inputs, target))
    open_webui_env_file = open_webui["env_file"]
    assert isinstance(open_webui_env_file, list)
    open_webui["env_file"] = [
        {**entry, "path": "./open-webui/env"} for entry in open_webui_env_file
    ]
    open_webui["environment"] = {
        **owui_environment(inputs, search=False, directory=False),
        "WEBUI_URL": CI_BASE_URL,
    }
    open_webui["volumes"] = [f"{CI_ROOT}/openwebui:/app/backend/data"]
    open_webui["ports"] = [f"127.0.0.1:{CI_PORT}:8080"]

    qdrant = dict(QdrantService().block(inputs, target))
    qdrant["volumes"] = [f"{CI_QDRANT_DATA_ROOT}:{QDRANT_STORAGE_MOUNT}"]

    opensearch = dict(
        opensearch_service(
            inputs, target, limit_bytes=CI_OPENSEARCH_MEMORY_GB * GIGABYTE
        )
    )
    opensearch["volumes"] = [
        f"{CI_OPENSEARCH_DATA_ROOT}:{OPENSEARCH_DATA_MOUNT}",
        f"{CI_OPENSEARCH_RENDERED_DIR}/opensearch.yml:{OPENSEARCH_SETTINGS_PATH}:ro",
        f"{CI_OPENSEARCH_RENDERED_DIR}/security:{OPENSEARCH_SECURITY_PATH}:ro",
    ]

    api = dict(api_service(inputs, target, rendered_root=CI_ROOT))
    api_labels = api["labels"]
    assert isinstance(api_labels, Mapping)
    api["labels"] = {
        **api_labels,
        CI_INSTRUCTION_DIGEST_LABEL: hashlib.sha256(
            ci_instruction(inputs).encode("utf-8")
        ).hexdigest(),
    }
    api_environment = api["environment"]
    assert isinstance(api_environment, Mapping)
    api["environment"] = {
        **api_environment,
        "GIDEON_ENGINE_URL": _relay_base_url(),
    }
    api["depends_on"] = {
        "postgres": {"condition": "service_healthy"},
        RELAY_SERVICE_NAME: {"condition": "service_started"},
    }

    relay: dict[str, object] = {
        "image": reference(target, image_pin(inputs, "gideon")),
        "restart": "unless-stopped",
        "entrypoint": ["python3", RELAY_CONTAINER_PATH],
        "environment": {
            "RELAY_TARGET": f"{ENGINE_SERVICE_NAME}:{ENGINE_PORT}",
            "RELAY_PORT": str(ENGINE_PORT),
        },
        "volumes": [f"{inputs.checkout}/{RELAY_SOURCE}:{RELAY_CONTAINER_PATH}:ro"],
        "healthcheck": {
            "test": [
                "CMD",
                "python3",
                "-c",
                "import socket; socket.create_connection(('127.0.0.1', "
                f"{ENGINE_PORT}), timeout=4).close()",
            ],
            "interval": "30s",
            "timeout": "5s",
            "retries": 3,
            "start_period": "5s",
        },
        "networks": [NETWORK_NAME, INTEGRATION_NETWORK_NAME],
    }
    services: dict[str, object] = {
        PostgresService.name: with_memory_limit(
            postgres,
            CI_POSTGRES_MEMORY_GB * GIGABYTE,
            swap=PostgresService.swap,
        ),
        OpenWebuiService.name: with_memory_limit(
            open_webui,
            memory_limit_bytes(inputs.profile, OpenWebuiService.name),
            swap=OpenWebuiService.swap,
        ),
        QdrantService.name: with_memory_limit(
            qdrant,
            CI_QDRANT_MEMORY_GB * GIGABYTE,
            swap=QdrantService.swap,
        ),
        OpensearchService.name: with_memory_limit(
            opensearch,
            CI_OPENSEARCH_MEMORY_GB * GIGABYTE,
            swap=OpensearchService.swap,
        ),
        API_SERVICE_NAME: with_memory_limit(
            api,
            memory_limit_bytes(inputs.profile, API_SERVICE_NAME),
            swap=ApiService.swap,
        ),
        RELAY_SERVICE_NAME: with_memory_limit(relay, RELAY_MEMORY_LIMIT, swap=True),
    }

    secrets = {
        name: {"file": _ci_secret_file(name)} for name in mounted_secret_names(services)
    }

    return {
        "name": CI_PROJECT,
        "services": services,
        "networks": {
            NETWORK_NAME: {},
            INTEGRATION_NETWORK_NAME: {
                "external": True,
                "name": integration_network_name(),
            },
        },
        "secrets": secrets,
    }


def ci_env_file(inputs: RenderInputs) -> str:
    """Render the sibling's Open WebUI env file without directory settings."""

    return owui_env_file_text(owui_secret_environment(inputs, directory=False))


def ci_instruction(inputs: RenderInputs) -> str:
    """Render the instruction mounted by the sibling API service."""

    return general_instruction_text(inputs)


def ci_manifest(inputs: RenderInputs) -> str:
    """Render the sibling's desired Open WebUI state."""

    return ApplyManifestArtifact().emit(inputs)
