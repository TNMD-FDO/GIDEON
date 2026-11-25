"""Compose definition for gideon-api."""

from collections.abc import Mapping
from typing import Final

from gideon.host.cas import ROOT as CAS_ROOT
from gideon.host.images import RegistryTarget, reference
from gideon.host.render import RenderInputs
from gideon.host.render.api import (
    API_CHAT_HEADER,
    API_CORPUS_DATABASE_HOST_ENV,
    API_CORPUS_PASSWORD_FILE_ENV,
    API_EMBED_URL_ENV,
    API_ENGINE_URL_ENV,
    API_HEALTH_PATH,
    API_IMAGE_NAME,
    API_INSTRUCTION_MOUNT,
    API_INSTRUCTION_PATH,
    API_MOUNT_TARGET,
    API_RERANK_KEY_FILE_ENV,
    API_RERANK_URL_ENV,
    API_SECRET_NAME,
    API_SERVICE_NAME,
    API_SOURCE_HEADER,
    API_USER_ID_HEADER,
    API_WORKING_DIRECTORY,
    TURN_READER_ROLE,
    TURN_READER_SECRET_NAME,
    api_enabled,
)
from gideon.host.render.engine import (
    EMBED,
    ENGINE_PORT,
    ENGINE_SECRET_NAME,
    RERANK,
    engine_base_url,
)
from gideon.host.render.opensearch import (
    OPENSEARCH_TURN_SECRET_NAME,
    OPENSEARCH_TURN_USER,
    opensearch_rest_url,
)
from gideon.host.render.owui import EVAL_IDENTITY, GENERAL_MODEL_ID
from gideon.host.render.qdrant import QDRANT_READ_ONLY_SECRET_NAME, qdrant_rest_url
from gideon.host.render.services import ServiceDefinition, image_pin, source_digest
from gideon.host.render.services.generator import generator_pin
from gideon.host.render.services.model_server import model_server_members
from gideon.host.render.worker import (
    WORKER_DATABASE_HOST,
    WORKER_DATABASE_NAME,
    WORKER_DATABASE_PORT,
)

API_SOURCES_DIGEST_LABEL: Final = "org.gideon.api-sources-digest"

# The API image carries Python but no curl, so its healthcheck uses the
# standard-library client; these are SearXNG's starting bounds. exempt: the
# service is expected to become ready in a second or two.
API_HEALTHCHECK: Final[Mapping[str, object]] = {
    "test": [
        "CMD",
        "python",
        "-c",
        "import urllib.request; urllib.request.urlopen("
        f"'http://127.0.0.1:{ENGINE_PORT}{API_HEALTH_PATH}', timeout=4).read()",
    ],
    "interval": "30s",
    "timeout": "5s",
    "retries": 3,
    "start_period": "30s",
}


def api_service(
    inputs: RenderInputs,
    target: RegistryTarget,
    *,
    rendered_root: str = "/etc/gideon/rendered",
) -> Mapping[str, object]:
    """Build the API service from the applying checkout's mounted package.

    It has no ports: callers reach it on the Compose network. The read-only
    checkout mount is the tree that applied this render. A change to the
    mounted instruction or source-digest label recreates the service;
    General's model id and the engine's served name move its block.
    """

    if not inputs.checkout:
        raise ValueError(
            "Render input checkout is empty; the gideon-api service needs the "
            "release checkout's absolute path. Re-run render with a checkout."
        )
    password_file = f"/run/secrets/{TURN_READER_SECRET_NAME}"
    rerank_environment: dict[str, str] = {}
    rerank_secrets: list[str] = []
    if RERANK in model_server_members(inputs):
        rerank_environment = {
            API_RERANK_URL_ENV: f"http://{RERANK.service_name}:{RERANK.port}",
            API_RERANK_KEY_FILE_ENV: f"/run/secrets/{RERANK.secret_name}",
        }
        rerank_secrets.append(RERANK.secret_name)
    return {
        "image": reference(target, image_pin(inputs, API_IMAGE_NAME)),
        "restart": "unless-stopped",
        "environment": {
            API_ENGINE_URL_ENV: engine_base_url(),
            "GIDEON_ENGINE_API_KEY_FILE": f"/run/secrets/{ENGINE_SECRET_NAME}",
            "GIDEON_API_KEY_FILE": f"/run/secrets/{API_SECRET_NAME}",
            "GIDEON_INSTRUCTION_FILE": API_INSTRUCTION_MOUNT,
            "GIDEON_API_PORT": str(ENGINE_PORT),
            "GIDEON_SOURCE_HEADER": API_SOURCE_HEADER,
            "GIDEON_EVAL_IDENTITY": EVAL_IDENTITY.email,
            "GIDEON_CHAT_HEADER": API_CHAT_HEADER,
            "GIDEON_USER_HEADER": API_USER_ID_HEADER,
            "GIDEON_MODEL_ID": GENERAL_MODEL_ID,
            "GIDEON_ENGINE_MODEL": generator_pin(inputs).serve.served_name,
            "GIDEON_API_DB_HOST": WORKER_DATABASE_HOST,
            "GIDEON_API_DB_PORT": str(WORKER_DATABASE_PORT),
            "GIDEON_API_DB_NAME": WORKER_DATABASE_NAME,
            "GIDEON_API_DB_ROLE": TURN_READER_ROLE,
            "GIDEON_API_PASSWORD_FILE": password_file,
            API_CORPUS_DATABASE_HOST_ENV: WORKER_DATABASE_HOST,
            "GIDEON_API_CORPUS_DB_PORT": str(WORKER_DATABASE_PORT),
            "GIDEON_API_CORPUS_DB_NAME": WORKER_DATABASE_NAME,
            "GIDEON_API_CORPUS_DB_ROLE": TURN_READER_ROLE,
            API_CORPUS_PASSWORD_FILE_ENV: password_file,
            "GIDEON_API_CAS_ROOT": str(CAS_ROOT),
            "GIDEON_API_QDRANT_URL": qdrant_rest_url(),
            "GIDEON_API_QDRANT_KEY_FILE": f"/run/secrets/{QDRANT_READ_ONLY_SECRET_NAME}",
            API_EMBED_URL_ENV: f"http://{EMBED.service_name}:{EMBED.port}",
            "GIDEON_API_EMBED_KEY_FILE": f"/run/secrets/{EMBED.secret_name}",
            "GIDEON_API_OPENSEARCH_URL": opensearch_rest_url(),
            "GIDEON_API_OPENSEARCH_USER": OPENSEARCH_TURN_USER,
            "GIDEON_API_OPENSEARCH_PASSWORD_FILE": f"/run/secrets/{OPENSEARCH_TURN_SECRET_NAME}",
            **rerank_environment,
            "TZ": inputs.site.office.timezone,
        },
        "command": ["python", "-m", "gideon.api"],
        "working_dir": API_WORKING_DIRECTORY,
        "volumes": [
            f"{inputs.checkout}/gideon:{API_MOUNT_TARGET}:ro",
            f"{rendered_root}/{API_INSTRUCTION_PATH}:{API_INSTRUCTION_MOUNT}:ro",
            f"{CAS_ROOT}:{CAS_ROOT}:ro",
        ],
        "group_add": [str(inputs.facts.service_gid)],
        "secrets": [
            ENGINE_SECRET_NAME,
            API_SECRET_NAME,
            "postgres_gideon_audit_password",
            TURN_READER_SECRET_NAME,
            QDRANT_READ_ONLY_SECRET_NAME,
            EMBED.secret_name,
            OPENSEARCH_TURN_SECRET_NAME,
            *rerank_secrets,
        ],
        "labels": {API_SOURCES_DIGEST_LABEL: source_digest(inputs, API_SERVICE_NAME)},
        "healthcheck": dict(API_HEALTHCHECK),
        "networks": ["gideon"],
    }


class ApiService(ServiceDefinition):
    """The gideon-api service and its mounted API and guardrail sources.

    A change to either declared source moves the block's digest label.
    """

    name = API_SERVICE_NAME
    turn_path = True
    sources = ("gideon/api", "gideon/guardrail")

    def applies(self, inputs: RenderInputs) -> bool:
        return api_enabled(inputs.no_gpu)

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        return api_service(inputs, target)
