"""Compose definition for postgres."""

from collections.abc import Mapping

from gideon.host.images import RegistryTarget, reference
from gideon.host.render import RenderInputs
from gideon.host.render.pgbackrest import (
    PG_BACKREST_CONF_PATH,
    PGDATA,
    REPOSITORY_PATH,
    STANZA,
)
from gideon.host.render.services import ServiceDefinition, image_pin

# Service fragments the drill document (render/drill.py) shares with the
# production project, so the two never drift apart.
POSTGRES_HEALTHCHECK: Mapping[str, object] = {
    "test": ["CMD", "pg_isready", "-U", "postgres", "-h", "127.0.0.1"],
    "interval": "10s",
    "timeout": "5s",
    "retries": 12,
    "start_period": "30s",
}


class PostgresService(ServiceDefinition):
    """The postgres service in the Compose project."""

    name = "postgres"
    store = True

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        return {
            "image": reference(target, image_pin(inputs, "postgres")),
            "restart": "unless-stopped",
            "environment": {
                "POSTGRES_PASSWORD_FILE": "/run/secrets/postgres_superuser_password",
                "PGDATA": PGDATA,
                "TZ": inputs.site.office.timezone,
            },
            "command": [
                "postgres",
                "-c",
                "archive_mode=on",
                "-c",
                f"archive_command=pgbackrest --stanza={STANZA} archive-push %p",
                "-c",
                "archive_timeout=300",
            ],
            "volumes": [
                "/data/fast/postgres:/var/lib/postgresql",
                f"{REPOSITORY_PATH}:{REPOSITORY_PATH}",
                f"/etc/gideon/rendered/postgres/pgbackrest.conf:{PG_BACKREST_CONF_PATH}:ro",
            ],
            "secrets": ["postgres_superuser_password"],
            "healthcheck": dict(POSTGRES_HEALTHCHECK),
            "networks": ["gideon"],
        }
