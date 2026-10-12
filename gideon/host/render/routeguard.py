"""Render the model servers' mounted route guard and its stable identities."""

from typing import Final

from gideon.host.render import Artifact, RenderInputs, template_text
from gideon.host.render.engine import MODEL_SERVERS

ROUTE_GUARD_SOURCE: Final[str] = "gideon/modelserver/guard.py"
ROUTE_GUARD_RENDERED_PATH: Final[str] = "model-servers/gideon_route_guard.py"
ROUTE_GUARD_MOUNT_DIR: Final[str] = "/etc/gideon/model-servers"
ROUTE_GUARD_MODULE: Final[str] = "gideon_route_guard"
ROUTE_GUARD_CLASS: Final[str] = "RouteGuard"


def route_guard_import_path() -> str:
    """Return the middleware class path loaded by the model server."""

    return f"{ROUTE_GUARD_MODULE}.{ROUTE_GUARD_CLASS}"


def route_guard_mount() -> str:
    """Return the read-only bind mount of the rendered module."""

    return (
        f"/etc/gideon/rendered/{ROUTE_GUARD_RENDERED_PATH}:"
        f"{ROUTE_GUARD_MOUNT_DIR}/{ROUTE_GUARD_MODULE}.py:ro"
    )


class RouteGuardArtifact(Artifact):
    """The guard's source text, owned by every model server that mounts it."""

    name = "model-servers-route-guard"
    relative_path = ROUTE_GUARD_RENDERED_PATH
    mode = 0o644
    owners = tuple(member.service_name for member in MODEL_SERVERS)
    source_paths = (ROUTE_GUARD_SOURCE,)

    def applies(self, inputs: RenderInputs) -> bool:
        """Emit the guard where model servers run."""

        return not inputs.no_gpu

    def emit(self, inputs: RenderInputs) -> str:
        """Emit the declared module text verbatim."""

        return template_text(inputs, ROUTE_GUARD_SOURCE)
