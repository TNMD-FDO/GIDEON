"""Shared proxy URL rendering for service environment files."""

from collections.abc import Mapping
from typing import Final

from gideon.host.images import proxy_url
from gideon.host.render import RenderInputs

PROXY_AUTH_NAME: Final[str] = "proxy_auth"


def proxy_credentials(inputs: RenderInputs) -> tuple[str, str] | None:
    """Split the optional proxy secret into URL user-info components."""

    value = inputs.secrets.get(PROXY_AUTH_NAME)
    if value is None:
        return None
    user, separator, password = value.partition(":")
    if not separator or not user or not password:
        raise ValueError("proxy_auth must contain a non-empty user:password pair.")
    return user, password


def proxy_environment_values(inputs: RenderInputs) -> Mapping[str, str]:
    """Return HTTP(S) proxy variables for a configured egress proxy."""

    if not inputs.site.egress_proxy:
        return {}
    value = proxy_url(inputs.site.egress_proxy, proxy_credentials(inputs))
    return {"HTTP_PROXY": value, "HTTPS_PROXY": value}
