"""Pure Compose document rendering for the throwaway backup drill project.

The blocks are the service definitions' with the drill's differences stated.
"""

from collections.abc import Mapping
from typing import Final

from gideon.host.images import parse_registry
from gideon.host.render import RenderInputs
from gideon.host.render.compose import (
    NETWORK_NAME,
    memory_limit_bytes,
    mounted_secret_names,
    with_memory_limit,
)
from gideon.host.render.owui import owui_environment
from gideon.host.render.pgbackrest import (
    PG_BACKREST_CONF_PATH,
    REPOSITORY_PATH,
)
from gideon.host.render.services.open_webui import OpenWebuiService
from gideon.host.render.services.postgres import PostgresService

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

    postgres = dict(PostgresService().block(inputs, target))
    postgres["volumes"] = [
        f"{DRILL_ROOT}/postgres:/var/lib/postgresql",
        f"{REPOSITORY_PATH}:{REPOSITORY_PATH}:ro",
        f"/etc/gideon/rendered/postgres/pgbackrest.conf:{PG_BACKREST_CONF_PATH}:ro",
    ]
    postgres["networks"] = [NETWORK_NAME]
    del postgres["command"]

    open_webui = dict(OpenWebuiService().block(inputs, target))
    open_webui["environment"] = {
        **owui_environment(inputs, engine=False, store=False),
        "WEBUI_URL": f"http://127.0.0.1:{DRILL_PORT}",
    }
    open_webui["volumes"] = [
        f"{DRILL_ROOT}/openwebui:/app/backend/data",
        "/etc/gideon/ca.pem:/etc/gideon/ca.pem:ro",
    ]
    open_webui["ports"] = [f"127.0.0.1:{DRILL_PORT}:8080"]

    services: dict[str, object] = {
        PostgresService.name: with_memory_limit(
            postgres,
            memory_limit_bytes(inputs.profile, PostgresService.name),
            swap=PostgresService.swap,
        ),
        OpenWebuiService.name: with_memory_limit(
            open_webui,
            memory_limit_bytes(inputs.profile, OpenWebuiService.name),
            swap=OpenWebuiService.swap,
        ),
    }

    secrets = {
        name: {"file": f"/etc/gideon/secrets/{name}"}
        for name in mounted_secret_names(services)
    }

    return {
        "name": DRILL_PROJECT,
        "services": services,
        "networks": {NETWORK_NAME: {}},
        "secrets": secrets,
    }
