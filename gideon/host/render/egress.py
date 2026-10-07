"""Identity and optional parent proxy environment for the corpus egress service."""

from collections.abc import Mapping
from typing import Final

from gideon.host.images import proxy_url
from gideon.host.render import Artifact, RenderInputs
from gideon.host.render.proxy import PROXY_AUTH_NAME, proxy_credentials

EGRESS_SERVICE_NAME: Final = "gideon-egress"
EGRESS_PORT: Final = 3128
EGRESS_GROUP: Final = "corpus"
INTERNAL_NETWORK_NAME: Final = "internal"
INTERNAL_NO_PROXY_HOSTS: Final = ("prometheus", "postgres", EGRESS_SERVICE_NAME)
HOSTS_ENV: Final = "GIDEON_EGRESS_HOSTS"
PORT_ENV: Final = "GIDEON_EGRESS_PORT"
PARENT_PROXY_ENV: Final = "GIDEON_EGRESS_PARENT_PROXY"
EGRESS_SOURCES_DIGEST_LABEL: Final = "org.gideon.egress-sources-digest"
EGRESS_ENV_FILE: Final[tuple[Mapping[str, str], ...]] = (
    {"path": "/etc/gideon/rendered/gideon-egress/env", "format": "raw"},
)


def egress_proxy_url() -> str:
    """Return the internal URL used by the worker's proxy variables."""

    return f"http://{EGRESS_SERVICE_NAME}:{EGRESS_PORT}"


class EgressEnvArtifact(Artifact):
    """Render the parent proxy URL only when one is configured."""

    name = "gideon-egress-env"
    relative_path = "gideon-egress/env"
    mode = 0o600
    owners = (EGRESS_SERVICE_NAME,)
    secret = True

    def applies(self, inputs: RenderInputs) -> bool:
        return bool(inputs.site.egress_proxy)

    def secret_names(self, inputs: RenderInputs) -> tuple[str, ...]:
        if inputs.site.egress_proxy and PROXY_AUTH_NAME in inputs.secrets:
            return (PROXY_AUTH_NAME,)
        return ()

    def emit(self, inputs: RenderInputs) -> str:
        value = proxy_url(inputs.site.egress_proxy, proxy_credentials(inputs))
        return f"{PARENT_PROXY_ENV}={value}\n"
