"""Pure pgBackRest configuration rendering for the Postgres service."""

from string import Template
from typing import Final

from gideon.host.render import Artifact, RenderInputs

STANZA: Final = "gideon"
REPOSITORY_PATH: Final = "/data/backup-staging/pgbackrest"
PGDATA: Final = "/var/lib/postgresql/18/docker"
PG_BACKREST_CONF_PATH: Final = "/etc/pgbackrest.conf"

_TEMPLATE_PATH: Final = "postgres/pgbackrest.conf.tmpl"


class PgBackRestConfArtifact(Artifact):
    """Render the Postgres service's pgBackRest configuration."""

    name = "pgbackrest-conf"
    relative_path = "postgres/pgbackrest.conf"
    mode = 0o644
    owners = ("postgres",)
    template_paths = (_TEMPLATE_PATH,)

    def emit(self, inputs: RenderInputs) -> str:
        try:
            template_text = inputs.templates[_TEMPLATE_PATH]
        except KeyError as exc:
            raise ValueError(
                f"Render template {_TEMPLATE_PATH} is missing."
            ) from exc
        try:
            return Template(template_text).substitute(
                stanza=STANZA,
                repository_path=REPOSITORY_PATH,
                local_days=inputs.site.backup.local_days,
                pgdata=PGDATA,
            )
        except KeyError as exc:
            placeholder = exc.args[0] if exc.args else "unknown"
            raise ValueError(
                f"Render template {_TEMPLATE_PATH} has an unfilled "
                f"placeholder: {placeholder}."
            ) from exc
