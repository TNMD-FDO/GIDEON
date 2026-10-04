"""Compose document projected from the ordered service registry."""

from collections.abc import Mapping
from typing import Final

from gideon.host.images import RegistryTarget, parse_registry, reference
from gideon.host.models import GIGABYTE, HardwareProfile
from gideon.host.render import Artifact, RenderInputs
from gideon.host.render.api import (
    API_CHAT_HEADER,
    API_HEALTH_PATH,
    API_IMAGE_NAME,
    API_INSTRUCTION_MOUNT,
    API_INSTRUCTION_PATH,
    API_MOUNT_TARGET,
    API_SECRET_NAME,
    API_SOURCE_HEADER,
    API_WORKING_DIRECTORY,
)
from gideon.host.render.engine import ENGINE_PORT, ENGINE_SECRET_NAME, engine_base_url
from gideon.host.render.owui import (
    EVAL_IDENTITY,
    GENERAL_MODEL_ID,
    PERMISSIONS_TEMPLATE,
)
from gideon.host.render.services import (
    all_service_names,
    applying_services,
    store_services,
)
from gideon.host.render.services import (
    image_pin as image_pin,
)
from gideon.host.render.services.generator import (
    ENGINE_ACCESS_LOG_EXCLUDED_PATHS as ENGINE_ACCESS_LOG_EXCLUDED_PATHS,
)
from gideon.host.render.services.generator import (
    ENGINE_HEALTHCHECK as ENGINE_HEALTHCHECK,
)
from gideon.host.render.services.generator import (
    ENGINE_READY_SECONDS as ENGINE_READY_SECONDS,
)
from gideon.host.render.services.generator import (
    ENGINE_USAGE_SWITCHES as ENGINE_USAGE_SWITCHES,
)
from gideon.host.render.services.generator import (
    engine_command as engine_command,
)
from gideon.host.render.services.generator import (
    engine_service as engine_service,
)
from gideon.host.render.services.generator import (
    engine_wrapper as engine_wrapper,
)
from gideon.host.render.services.generator import generator_pin
from gideon.host.render.services.grafana import (
    grafana_environment as grafana_environment,
)
from gideon.host.render.services.node_exporter import (
    BUILD_BOX_UNIT_PATTERN as BUILD_BOX_UNIT_PATTERN,
)
from gideon.host.render.services.node_exporter import (
    UNIT_PATTERN as UNIT_PATTERN,
)
from gideon.host.render.services.open_webui import (
    OWUI_COMMAND as OWUI_COMMAND,
)
from gideon.host.render.services.open_webui import (
    OWUI_ENV_FILE as OWUI_ENV_FILE,
)
from gideon.host.render.services.open_webui import (
    OWUI_HEALTHCHECK as OWUI_HEALTHCHECK,
)
from gideon.host.render.services.postgres import (
    POSTGRES_HEALTHCHECK as POSTGRES_HEALTHCHECK,
)
from gideon.host.render.services.searxng import (
    SEARXNG_HEALTHCHECK as SEARXNG_HEALTHCHECK,
)
from gideon.host.render.yamlout import dump

PROJECT_NAME: Final = "gideon"
NETWORK_NAME: Final = "gideon"

# The tier apply starts and converges (roles, databases, migrations) before
# any other service is recreated: the registry's store projection, bound once
# at import.
STORE_SERVICES: tuple[str, ...] = store_services()

# The API image carries Python but no curl, so its healthcheck uses the
# standard-library client; these are SearXNG's starting bounds. exempt: the
# service is expected to become ready in a second or two.
API_HEALTHCHECK: Final[Mapping[str, object]] = {
    "test": [
        "CMD",
        "python",
        "-c",
        "import urllib.request; urllib.request.urlopen("
        f"'http://127.0.0.1:{ENGINE_PORT}{API_HEALTH_PATH}', timeout=4).read()",
    ],
    "interval": "30s",
    "timeout": "5s",
    "retries": 3,
    "start_period": "30s",
}


def api_service(
    inputs: RenderInputs,
    target: RegistryTarget,
    *,
    rendered_root: str = "/etc/gideon/rendered",
) -> Mapping[str, object]:
    """Build the API service from the applying checkout's mounted package.

    It has no ports: callers reach it on the Compose network. The read-only
    checkout mount is the tree that applied this render. A change to the
    mounted instruction or source-digest label recreates the service;
    General's model id and the engine's served name move its block.
    """

    if not inputs.checkout:
        raise ValueError(
            "Render input checkout is empty; the gideon-api service needs the "
            "release checkout's absolute path. Re-run render with a checkout."
        )
    if not inputs.api_sources_digest:
        raise ValueError(
            "Render input api_sources_digest is empty; the gideon-api service's "
            "label needs the digest of its declared sources. Re-run render from a "
            "release checkout, whose loader gathers it."
        )
    return {
        "image": reference(target, image_pin(inputs, API_IMAGE_NAME)),
        "restart": "unless-stopped",
        "environment": {
            "GIDEON_ENGINE_URL": engine_base_url(),
            "GIDEON_ENGINE_API_KEY_FILE": f"/run/secrets/{ENGINE_SECRET_NAME}",
            "GIDEON_API_KEY_FILE": f"/run/secrets/{API_SECRET_NAME}",
            "GIDEON_INSTRUCTION_FILE": API_INSTRUCTION_MOUNT,
            "GIDEON_API_PORT": str(ENGINE_PORT),
            "GIDEON_SOURCE_HEADER": API_SOURCE_HEADER,
            "GIDEON_EVAL_IDENTITY": EVAL_IDENTITY.email,
            "GIDEON_CHAT_HEADER": API_CHAT_HEADER,
            "GIDEON_MODEL_ID": GENERAL_MODEL_ID,
            "GIDEON_ENGINE_MODEL": generator_pin(inputs).serve.served_name,
            "TZ": inputs.site.office.timezone,
        },
        "command": ["python", "-m", "gideon.api"],
        "working_dir": API_WORKING_DIRECTORY,
        "volumes": [
            f"{inputs.checkout}/gideon:{API_MOUNT_TARGET}:ro",
            f"{rendered_root}/{API_INSTRUCTION_PATH}:{API_INSTRUCTION_MOUNT}:ro",
        ],
        "group_add": [str(inputs.facts.service_gid)],
        "secrets": [
            ENGINE_SECRET_NAME,
            API_SECRET_NAME,
            "postgres_gideon_audit_password",
        ],
        "labels": {"org.gideon.api-sources-digest": inputs.api_sources_digest},
        "healthcheck": dict(API_HEALTHCHECK),
        "networks": ["gideon"],
    }


class ComposeArtifact(Artifact):
    """Render the ordered Compose project containing the GIDEON services."""

    name = "compose"
    relative_path = "compose.yaml"
    template_paths = (PERMISSIONS_TEMPLATE,)

    def emit(self, inputs: RenderInputs) -> str:
        return dump(_compose_document(inputs))


def service_names(inputs: RenderInputs) -> tuple[str, ...]:
    """The Compose services this release renders, in file order."""

    return tuple(service_blocks(inputs))


def service_blocks(inputs: RenderInputs) -> Mapping[str, object]:
    """The rendered document's service blocks by name, in file order."""

    document = _compose_document(inputs)
    services = document["services"]
    assert isinstance(services, Mapping)
    return services


def compose_top_level(inputs: RenderInputs) -> Mapping[str, object]:
    """The rendered document without its service blocks: the sections every service shares."""

    document = dict(_compose_document(inputs))
    del document["services"]
    return document


def service_images(inputs: RenderInputs) -> tuple[str, ...]:
    """The image references the rendered services name, in file order.

    What the stack pulls: apply probes and verifies these, not every lock pin —
    a no-GPU host renders no DCGM exporter, so its pin stays in the lock and the
    mirror but is never pulled (the acceptance VM's first install found this).
    """

    services = service_blocks(inputs)
    images: list[str] = []
    for service in services.values():
        assert isinstance(service, Mapping)
        image = service["image"]
        assert isinstance(image, str)
        images.append(image)
    return tuple(images)


def memory_limit_bytes(profile: HardwareProfile, service: str) -> int:
    """Return a service's decimal-gigabyte memory limit in bytes."""

    row = profile.memory_row(service)
    if row is None:
        raise ValueError(
            f"Cannot render Compose: profile {profile.name} has no memory row for "
            f"service '{service}'. Add memory.{service}.gb to models.lock, then "
            "re-run render."
        )
    return row.gb * GIGABYTE


def _compose_document(inputs: RenderInputs) -> Mapping[str, object]:
    target = parse_registry(inputs.site.registry)
    if target is None:
        raise ValueError(
            "Cannot render Compose: the registry key is not usable. "
            "Correct registry in /etc/gideon/site.yaml, then re-run render."
        )

    # The memory table is held to every defined service in both directions,
    # before the marker and the site gate which ones this host runs: the lock
    # is release content, complete everywhere, so a no-GPU host or a
    # search-off site refuses the same lock a GPU host would.
    known_services = all_service_names()
    unknown_services = tuple(
        row.service
        for row in inputs.profile.memory
        if row.service not in known_services
    )
    if unknown_services:
        names = ", ".join(unknown_services)
        allowed = ", ".join(known_services)
        raise ValueError(
            f"Cannot render Compose: profile {inputs.profile.name}'s memory table names "
            f"{names}, which no GIDEON service is called; the services are {allowed}. "
            "Remove or rename the row in models.lock, then re-run render."
        )
    missing_services = tuple(
        name for name in known_services if inputs.profile.memory_row(name) is None
    )
    if missing_services:
        names = ", ".join(f"service '{name}'" for name in missing_services)
        rows = ", ".join(f"memory.{name}.gb" for name in missing_services)
        raise ValueError(
            f"Cannot render Compose: profile {inputs.profile.name} has no memory row for "
            f"{names}. Add {rows} to models.lock, then re-run render."
        )
    # Every rendered service carries its row's limit, applied here in one place
    # after the marker and the site have settled which services this host runs.
    # The key is mem_limit, not deploy.resources.limits.memory, and the value
    # is an exact byte count: Compose reads a g suffix as binary, 7.4 % over the
    # lock's decimal-gigabyte unit.
    services: dict[str, object] = {}
    for definition in applying_services(inputs):
        block = definition.block(inputs, target)
        services[definition.name] = {
            **block,
            "mem_limit": memory_limit_bytes(inputs.profile, definition.name),
        }

    secrets: dict[str, object] = {
        "tls_key": {"file": "/etc/gideon/secrets/tls_key"},
        "postgres_superuser_password": {
            "file": "/etc/gideon/secrets/postgres_superuser_password"
        },
        "webui_secret_key": {"file": "/etc/gideon/secrets/webui_secret_key"},
        "grafana_admin_password": {
            "file": "/etc/gideon/secrets/grafana_admin_password"
        },
        "ldap_bind_password": {"file": "/etc/gideon/secrets/ldap_bind_password"},
        "postgres_gideon_ro_metrics_password": {
            "file": "/etc/gideon/secrets/postgres_gideon_ro_metrics_password"
        },
        "postgres_gideon_audit_password": {
            "file": "/etc/gideon/secrets/postgres_gideon_audit_password"
        },
    }
    if inputs.site.alerts.smtp.user:
        secrets["smtp_password"] = {"file": "/etc/gideon/secrets/smtp_password"}

    # The engine key and the API key are declared when a rendered block
    # mounts them, after the fixed entries.
    used_secrets = {
        secret
        for block in services.values()
        if isinstance(block, Mapping)
        for secret in block.get("secrets", ())
    }
    if ENGINE_SECRET_NAME in used_secrets:
        secrets[ENGINE_SECRET_NAME] = {
            "file": f"/etc/gideon/secrets/{ENGINE_SECRET_NAME}"
        }
    if API_SECRET_NAME in used_secrets:
        secrets[API_SECRET_NAME] = {"file": f"/etc/gideon/secrets/{API_SECRET_NAME}"}
    return {
        "name": PROJECT_NAME,
        "services": services,
        "networks": {NETWORK_NAME: {}},
        "volumes": {"caddy_data": {}, "caddy_config": {}},
        "secrets": secrets,
    }
