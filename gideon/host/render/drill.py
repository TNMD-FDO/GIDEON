"""Pure Compose document rendering for the throwaway backup drill project."""

from collections.abc import Mapping
from typing import Final

from gideon.host.images import parse_registry, reference
from gideon.host.render import RenderInputs
from gideon.host.render.compose import (
    OWUI_COMMAND,
    OWUI_ENV_FILE,
    OWUI_HEALTHCHECK,
    POSTGRES_HEALTHCHECK,
    image_pin,
    memory_limit_bytes,
)
from gideon.host.render.owui import owui_environment
from gideon.host.render.pgbackrest import (
    PG_BACKREST_CONF_PATH,
    PGDATA,
    REPOSITORY_PATH,
)

DRILL_PROJECT: Final = "gideon-drill"
DRILL_ROOT: Final = "/data/drill"
DRILL_PORT: Final = 18090


def drill_compose_document(inputs: RenderInputs) -> Mapping[str, object]:
    """Build the isolated drill Compose document without host I/O."""

    target = parse_registry(inputs.site.registry)
    if target is None:
        raise ValueError(
            "Cannot render drill Compose: the registry key is not usable. "
            "Correct registry in /etc/gideon/site.yaml, then re-run render."
        )

    postgres_pin = image_pin(inputs, "postgres")
    open_webui_pin = image_pin(inputs, "open-webui")
    environment = dict(owui_environment(inputs, engine=False))
    environment["WEBUI_URL"] = f"http://127.0.0.1:{DRILL_PORT}"

    return {
        "name": DRILL_PROJECT,
        "services": {
            "postgres": {
                "image": reference(target, postgres_pin),
                "restart": "unless-stopped",
                "environment": {
                    "POSTGRES_PASSWORD_FILE": "/run/secrets/postgres_superuser_password",
                    "PGDATA": PGDATA,
                    "TZ": inputs.site.office.timezone,
                },
                "volumes": [
                    f"{DRILL_ROOT}/postgres:/var/lib/postgresql",
                    f"{REPOSITORY_PATH}:{REPOSITORY_PATH}:ro",
                    f"/etc/gideon/rendered/postgres/pgbackrest.conf:{PG_BACKREST_CONF_PATH}:ro",
                ],
                "secrets": ["postgres_superuser_password"],
                "healthcheck": dict(POSTGRES_HEALTHCHECK),
                "networks": ["gideon"],
                "mem_limit": memory_limit_bytes(inputs.profile, "postgres"),
            },
            "open-webui": {
                "image": reference(target, open_webui_pin),
                "restart": "unless-stopped",
                "depends_on": {
                    "postgres": {"condition": "service_healthy"},
                },
                "env_file": [dict(entry) for entry in OWUI_ENV_FILE],
                "environment": environment,
                "command": list(OWUI_COMMAND),
                "ports": [f"127.0.0.1:{DRILL_PORT}:8080"],
                "volumes": [
                    f"{DRILL_ROOT}/openwebui:/app/backend/data",
                    "/etc/gideon/ca.pem:/etc/gideon/ca.pem:ro",
                ],
                "secrets": ["webui_secret_key"],
                "healthcheck": dict(OWUI_HEALTHCHECK),
                "networks": ["gideon"],
                "mem_limit": memory_limit_bytes(inputs.profile, "open-webui"),
            },
        },
        "networks": {"gideon": {}},
        "secrets": {
            "postgres_superuser_password": {
                "file": "/etc/gideon/secrets/postgres_superuser_password"
            },
            "webui_secret_key": {"file": "/etc/gideon/secrets/webui_secret_key"},
        },
    }
