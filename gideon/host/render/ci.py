"""Pure Compose and Open WebUI artifacts for the standing CI sibling stack."""

from collections.abc import Mapping
from typing import Final

from gideon.host.images import parse_registry, reference
from gideon.host.render import RenderInputs
from gideon.host.render.api import API_SECRET_NAME, API_SERVICE_NAME
from gideon.host.render.compose import (
    NETWORK_NAME,
    OWUI_COMMAND,
    OWUI_ENV_FILE,
    OWUI_HEALTHCHECK,
    POSTGRES_HEALTHCHECK,
    PROJECT_NAME,
    api_service,
    image_pin,
    memory_limit_bytes,
)
from gideon.host.render.engine import (
    ENGINE_PORT,
    ENGINE_SECRET_NAME,
    ENGINE_SERVICE_NAME,
)
from gideon.host.render.owui import (
    ApplyManifestArtifact,
    owui_env_file_text,
    owui_environment,
    owui_secret_environment,
)
from gideon.host.render.pgbackrest import PGDATA
from gideon.host.render.searxng import SEARXNG_SECRET_NAME

CI_PROJECT: Final[str] = "gideon-ci"
CI_ROOT: Final[str] = "/data/ci"
CI_SECRETS_DIR: Final[str] = f"{CI_ROOT}/secrets"
CI_PORT: Final[int] = 18100
CI_BASE_URL: Final[str] = f"http://127.0.0.1:{CI_PORT}"
RELAY_SERVICE_NAME: Final[str] = "engine-relay"
RELAY_SOURCE: Final[str] = "tools/cistack/relay.py"
RELAY_CONTAINER_PATH: Final[str] = "/relay/relay.py"
RELAY_MEMORY_LIMIT: Final[int] = 128 * 1024 * 1024
# The sibling env file's reads on a GPU host with the directory off: what
# ``tools.cistack`` loads from the sibling's secrets directory, no supplied one.
CI_SECRET_NAMES: Final[tuple[str, ...]] = (
    "postgres_openwebui_password",
    "gideon_admin_password",
    API_SECRET_NAME,
)
# Generated entries the sibling's directory never holds: the engine key is
# production's, mounted by path, and the sibling runs no Grafana or SearXNG.
CI_SKIPPED_SECRETS: Final[tuple[str, ...]] = (
    ENGINE_SECRET_NAME,
    "grafana_admin_password",
    SEARXNG_SECRET_NAME,
)
CI_WIPE_PATHS: Final[tuple[str, ...]] = (
    f"{CI_ROOT}/postgres",
    f"{CI_ROOT}/openwebui",
    f"{CI_ROOT}/open-webui",
    f"{CI_ROOT}/compose.yaml",
    f"{CI_SECRETS_DIR}/gideon_admin_api_key",
    f"{CI_SECRETS_DIR}/gideon_eval_api_key",
)


def production_network_name() -> str:
    """Return production's Docker network name from its Compose constants."""

    return f"{PROJECT_NAME}_{NETWORK_NAME}"


def _relay_base_url() -> str:
    return f"http://{RELAY_SERVICE_NAME}:{ENGINE_PORT}/v1"


def _ci_secret_file(name: str) -> str:
    if name == ENGINE_SECRET_NAME:
        return f"/etc/gideon/secrets/{name}"
    return f"{CI_SECRETS_DIR}/{name}"


def ci_compose_document(inputs: RenderInputs) -> Mapping[str, object]:
    """Build the isolated sibling Compose document without host I/O."""

    target = parse_registry(inputs.site.registry)
    if target is None:
        raise ValueError(
            "Cannot render CI Compose: the registry key is not usable. "
            "Correct registry in /etc/gideon/site.yaml, then re-run render."
        )

    postgres_pin = image_pin(inputs, "postgres")
    open_webui_pin = image_pin(inputs, "open-webui")
    api = dict(api_service(inputs, target))
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

    open_webui_environment = dict(
        owui_environment(inputs, search=False, directory=False)
    )
    open_webui_environment["WEBUI_URL"] = CI_BASE_URL

    services: dict[str, object] = {
        "postgres": {
            "image": reference(target, postgres_pin),
            "restart": "unless-stopped",
            "environment": {
                "POSTGRES_PASSWORD_FILE": "/run/secrets/postgres_superuser_password",
                "PGDATA": PGDATA,
                "TZ": inputs.site.office.timezone,
            },
            "volumes": [f"{CI_ROOT}/postgres:/var/lib/postgresql"],
            "secrets": ["postgres_superuser_password"],
            "healthcheck": dict(POSTGRES_HEALTHCHECK),
            "networks": [NETWORK_NAME],
            "mem_limit": memory_limit_bytes(inputs.profile, "postgres"),
        },
        "open-webui": {
            "image": reference(target, open_webui_pin),
            "restart": "unless-stopped",
            "depends_on": {
                "postgres": {"condition": "service_healthy"},
            },
            "env_file": [{**entry, "path": "./open-webui/env"} for entry in OWUI_ENV_FILE],
            "environment": open_webui_environment,
            "command": list(OWUI_COMMAND),
            "ports": [f"127.0.0.1:{CI_PORT}:8080"],
            "volumes": [f"{CI_ROOT}/openwebui:/app/backend/data"],
            "secrets": ["webui_secret_key"],
            "healthcheck": dict(OWUI_HEALTHCHECK),
            "networks": [NETWORK_NAME],
            "mem_limit": memory_limit_bytes(inputs.profile, "open-webui"),
        },
        API_SERVICE_NAME: {
            **api,
            "mem_limit": memory_limit_bytes(inputs.profile, API_SERVICE_NAME),
        },
        RELAY_SERVICE_NAME: {
            "image": reference(target, image_pin(inputs, "gideon")),
            "restart": "unless-stopped",
            "entrypoint": ["python3", RELAY_CONTAINER_PATH],
            "environment": {
                "RELAY_TARGET": f"{ENGINE_SERVICE_NAME}:{ENGINE_PORT}",
                "RELAY_PORT": str(ENGINE_PORT),
            },
            "volumes": [
                f"{inputs.checkout}/{RELAY_SOURCE}:{RELAY_CONTAINER_PATH}:ro"
            ],
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
            "networks": [NETWORK_NAME, "production"],
            "mem_limit": RELAY_MEMORY_LIMIT,
        },
    }

    api_secret_names = api["secrets"]
    assert isinstance(api_secret_names, list)
    secrets: dict[str, Mapping[str, str]] = {
        "postgres_superuser_password": {
            "file": f"{CI_SECRETS_DIR}/postgres_superuser_password"
        },
        "webui_secret_key": {"file": f"{CI_SECRETS_DIR}/webui_secret_key"},
    }
    for name in api_secret_names:
        assert isinstance(name, str)
        secrets[name] = {"file": _ci_secret_file(name)}

    return {
        "name": CI_PROJECT,
        "services": services,
        "networks": {
            NETWORK_NAME: {},
            "production": {"external": True, "name": production_network_name()},
        },
        "secrets": secrets,
    }


def ci_env_file(inputs: RenderInputs) -> str:
    """Render the sibling's Open WebUI env file without directory settings."""

    return owui_env_file_text(owui_secret_environment(inputs, directory=False))


def ci_manifest(inputs: RenderInputs) -> str:
    """Render the sibling's desired Open WebUI state."""

    return ApplyManifestArtifact().emit(inputs)
