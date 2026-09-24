"""SearXNG's identity and its three rendered files.

The service name, port, secret name, and probe job name are shared by
the Compose service in ``render/compose.py``, the frontend's search connection
in ``render/owui.py``, and the probe in ``render/prometheus.py``; they live
here so those modules name the service without importing the document
builder. All three artifacts exist only while ``web.search`` is on — the
office's kill switch — and follow that key alone, GPU host or not.
The logging file keeps granian's own loggers and raises ``searx.network`` to
ERROR, so the one application warning carrying a query URL is dropped before a
handler sees it. The rendered settings follow the pinned SearXNG image's
defaults and the frontend's search requests.
"""

import json
from collections.abc import Mapping
from typing import Final

from gideon.host.render import Artifact, RenderInputs
from gideon.host.render.proxy import PROXY_AUTH_NAME, proxy_environment_values
from gideon.host.render.yamlout import dump
from gideon.host.site import WEB_ENGINE_VALUES, SiteConfig

SEARXNG_SERVICE_NAME: Final[str] = "searxng"
SEARXNG_PORT: Final[int] = 8080
SEARXNG_SECRET_NAME: Final[str] = "searxng_secret_key"
SEARXNG_JOB_NAME: Final[str] = "search"
SEARXNG_SETTINGS_PATH: Final[str] = "/etc/searxng/settings.yml"
SEARXNG_LOGGING_PATH: Final[str] = "/etc/searxng/logging.json"
# The parent logger covers every per-engine searx.network.<name> logger.  ERROR
# is the deliberate release decision: Granian's config is applied before the
# application imports. exempt: no figure.
SEARXNG_NETWORK_LOGGER: Final[str] = "searx.network"
SEARXNG_NETWORK_LOG_LEVEL: Final[str] = "ERROR"
SEARXNG_ENV_FILE: Final[tuple[Mapping[str, str], ...]] = (
    {"path": "/etc/gideon/rendered/searxng/env", "format": "raw"},
)


def site_search_enabled(site: SiteConfig) -> bool:
    """The one reading of ``web.search``: on, or off."""

    return site.web.search == "on"


def search_enabled(inputs: RenderInputs) -> bool:
    """Whether this render carries the service and the frontend's connection."""

    return site_search_enabled(inputs.site)


def searxng_query_url() -> str:
    """Return the bare query endpoint used by Open WebUI."""

    return f"http://{SEARXNG_SERVICE_NAME}:{SEARXNG_PORT}/search"


def searxng_health_url() -> str:
    """Return the Compose-network health endpoint."""

    return f"http://{SEARXNG_SERVICE_NAME}:{SEARXNG_PORT}/healthz"


def searxng_settings_document(inputs: RenderInputs) -> Mapping[str, object]:
    """Build the pinned SearXNG settings document for the site's engines."""

    engines = list(inputs.site.web.engines)
    for engine in engines:
        if engine not in WEB_ENGINE_VALUES:
            allowed = ", ".join(WEB_ENGINE_VALUES)
            raise ValueError(
                f"Cannot render SearXNG settings: web.engines names {engine!r}, "
                f"which is not one of {allowed}. Correct web.engines in "
                "/etc/gideon/site.yaml, then re-run render."
            )
    return {
        # The mapping form is what exposes keep_only; a bare `true` does not.
        # keep_only governs list membership only, and bing
        # and google are `disabled: true` in upstream's own defaults, so every
        # kept engine is re-enabled by name below.  Google is off for every
        # office unless the site file names it.
        "use_default_settings": {"engines": {"keep_only": engines}},
        "engines": [
            {"name": engine, "disabled": False}
            for engine in engines
        ],
        # Every value below is the pinned image's default made a rendered fact,
        # except the two formats: the frontend asks for JSON, which SearXNG
        # refuses with 403 unless listed. Debug off leaves the process at
        # WARNING; autocomplete and favicon resolvers are the only outbound
        # calls that are not a search. Both are user-triggered from an HTML
        # page nobody reaches, and are pinned off so search is the only outbound
        # call; the limiter and public_instance off need no valkey; no image
        # proxy. No secret_key here: the env supplies SEARXNG_SECRET, which
        # the loader lets win.
        "general": {"debug": False},
        "search": {
            "formats": ["html", "json"],
            "autocomplete": "",
            "favicon_resolver": "",
        },
        "server": {
            "limiter": False,
            "public_instance": False,
            "image_proxy": False,
            "method": "GET",
        },
    }


def searxng_logging_document() -> Mapping[str, object]:
    """Build the ``logging.config.dictConfig`` document granian applies.

    granian reads the file named by ``GRANIAN_LOG_CONFIG`` in every worker
    before it imports the application, and a top-level ``dict.update`` puts
    its ``loggers`` key in place of granian's own, so granian's two loggers are
    restated as it ships them; with no ``handlers`` or ``formatters`` key here,
    granian's shipped definitions stay. ``searx.network``'s explicit level
    survives the application's later root-level ``basicConfig`` and covers
    every engine's child logger. No input reaches the document: it is release
    content, the same on every host.
    """

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "loggers": {
            "_granian": {
                "handlers": ["console"],
                "level": "INFO",
                "propagate": False,
            },
            "granian.access": {
                "handlers": ["access"],
                "level": "INFO",
                "propagate": False,
            },
            SEARXNG_NETWORK_LOGGER: {"level": SEARXNG_NETWORK_LOG_LEVEL},
        },
    }


def searxng_secret_environment(inputs: RenderInputs) -> Mapping[str, str]:
    """Build SearXNG's raw secret environment file.

    SearXNG refuses to start on the placeholder key and has no ``_FILE`` form,
    so the generated key rides the env; so does a proxy with
    its credentials, because the pinned image's HTTP client honours the proxy
    environment while ``outgoing.proxies`` is unset, and a credential in
    the settings file would sit in a 0644 file.  No ``NO_PROXY``: the service
    reaches nothing on the Compose network.
    """

    secret = inputs.secrets.get(SEARXNG_SECRET_NAME)
    if secret is None:
        raise ValueError(f"Render secret is missing: {SEARXNG_SECRET_NAME}.")
    values: dict[str, str] = {"SEARXNG_SECRET": secret}
    values.update(proxy_environment_values(inputs))
    return values


class SearxngSettingsArtifact(Artifact):
    """Render SearXNG's site-scoped settings file."""

    name = "searxng-settings"
    relative_path = "searxng/settings.yml"
    mode = 0o644
    owners = (SEARXNG_SERVICE_NAME,)

    def applies(self, inputs: RenderInputs) -> bool:
        return search_enabled(inputs)

    def emit(self, inputs: RenderInputs) -> str:
        return dump(searxng_settings_document(inputs))


class SearxngEnvArtifact(Artifact):
    """Render SearXNG's raw secret environment file."""

    name = "searxng-env"
    relative_path = "searxng/env"
    mode = 0o600
    owners = (SEARXNG_SERVICE_NAME,)
    secret = True

    def secret_names(self, inputs: RenderInputs) -> tuple[str, ...]:
        """The names the env emitter reads: the signing key, and the proxy credential under a proxy."""

        names = [SEARXNG_SECRET_NAME]
        if inputs.site.egress_proxy and PROXY_AUTH_NAME in inputs.secrets:
            names.append(PROXY_AUTH_NAME)
        return tuple(names)

    def applies(self, inputs: RenderInputs) -> bool:
        return search_enabled(inputs)

    def emit(self, inputs: RenderInputs) -> str:
        return "".join(
            f"{name}={value}\n"
            for name, value in searxng_secret_environment(inputs).items()
        )


class SearxngLoggingArtifact(Artifact):
    """Render SearXNG's non-secret Granian logging configuration."""

    name = "searxng-logging"
    relative_path = "searxng/logging.json"
    mode = 0o644
    owners = (SEARXNG_SERVICE_NAME,)

    def applies(self, inputs: RenderInputs) -> bool:
        return search_enabled(inputs)

    def emit(self, inputs: RenderInputs) -> str:
        del inputs
        return json.dumps(searxng_logging_document(), indent=2, sort_keys=True) + "\n"
