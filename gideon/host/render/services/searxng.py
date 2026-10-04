"""Compose definition for searxng."""

from collections.abc import Mapping
from typing import Final

from gideon.host.images import RegistryTarget, reference
from gideon.host.render import RenderInputs
from gideon.host.render.searxng import (
    SEARXNG_ENV_FILE,
    SEARXNG_LOGGING_PATH,
    SEARXNG_PORT,
    SEARXNG_SERVICE_NAME,
    SEARXNG_SETTINGS_PATH,
    search_enabled,
)
from gideon.host.render.services import ServiceDefinition, image_pin

# SearXNG runs as shipped — the pinned image's entrypoint,
# default user, and command untouched — on the Compose network alone, and is
# present iff web.search is on. The image declares no HEALTHCHECK and carries
# wget but no curl. These are starting bounds; the start period is the seconds
# Granian takes to serve /healthz on the box.
SEARXNG_HEALTHCHECK: Final[Mapping[str, object]] = {
    "test": [
        "CMD",
        "wget",
        "-q",
        "-O",
        "/dev/null",
        f"http://127.0.0.1:{SEARXNG_PORT}/healthz",
    ],
    "interval": "30s",
    "timeout": "5s",
    "retries": 3,
    "start_period": "30s",
}


def searxng_service(
    inputs: RenderInputs, target: RegistryTarget
) -> Mapping[str, object]:
    """Build SearXNG's run-as-shipped, Compose-network-only service.

    No ``ports`` (nothing outside the network reaches it), no ``user`` (as
    upstream's own Compose file runs it), no ``depends_on`` either way (a
    search is a per-turn call, and the frontend must start without it).
    The settings file is a read-only bind mount at the entrypoint's config
    path, which leaves an existing file untouched; the signing key and any
    proxy ride the env file, never the settings. The access log is Granian's
    default off, pinned by name so no request line carrying a query URL is
    ever written. The logging file separately sets SearXNG's network logger
    to ERROR: access-log off controls request lines, while this file suppresses
    application warning lines that carry a failed request URL.
    """

    return {
        "image": reference(target, image_pin(inputs, SEARXNG_SERVICE_NAME)),
        "restart": "unless-stopped",
        "environment": {
            "GRANIAN_LOG_ACCESS_ENABLED": "false",
            "GRANIAN_LOG_CONFIG": SEARXNG_LOGGING_PATH,
            "TZ": inputs.site.office.timezone,
        },
        "env_file": [dict(entry) for entry in SEARXNG_ENV_FILE],
        "volumes": [
            f"/etc/gideon/rendered/searxng/settings.yml:{SEARXNG_SETTINGS_PATH}:ro",
            f"/etc/gideon/rendered/searxng/logging.json:{SEARXNG_LOGGING_PATH}:ro",
        ],
        "healthcheck": dict(SEARXNG_HEALTHCHECK),
        "networks": ["gideon"],
    }


class SearxngService(ServiceDefinition):
    """The searxng service in the Compose project."""

    name = SEARXNG_SERVICE_NAME

    def applies(self, inputs: RenderInputs) -> bool:
        return search_enabled(inputs)

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        return searxng_service(inputs, target)
