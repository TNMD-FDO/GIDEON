"""Compose document projected from the ordered service registry."""

from collections.abc import Mapping
from typing import Final

from gideon.host.images import parse_registry
from gideon.host.models import GIGABYTE, HardwareProfile
from gideon.host.render import Artifact, RenderInputs
from gideon.host.render.engine import ENGINE_READY_SECONDS as ENGINE_READY_SECONDS
from gideon.host.render.owui import PERMISSIONS_TEMPLATE
from gideon.host.render.services import (
    all_service_names,
    applying_services,
    store_services,
)
from gideon.host.render.services import (
    image_pin as image_pin,
)
from gideon.host.render.services.api import (
    api_service as api_service,
)
from gideon.host.render.services.generator import (
    ENGINE_ACCESS_LOG_EXCLUDED_PATHS as ENGINE_ACCESS_LOG_EXCLUDED_PATHS,
)
from gideon.host.render.services.generator import (
    ENGINE_HEALTHCHECK as ENGINE_HEALTHCHECK,
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
# The swap a service that may not be swapped is still allowed: one 4 KiB page.
# A ceiling equal to the memory limit asks for none, but under the systemd
# cgroup driver runc writes that zero to the cgroup file alone and systemd
# records no swap limit, so the next daemon-reload (every apply runs one)
# resets the container to unlimited swap. A non-zero ceiling is recorded in
# systemd and survives the reload.
SWAP_CEILING_BYTES: Final = 4096

# The tier apply starts and converges (roles, databases, migrations) before
# any other service is recreated: the registry's store projection, bound once
# at import.
STORE_SERVICES: tuple[str, ...] = store_services()

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


def _mounted_secret_names(services: Mapping[str, object]) -> tuple[str, ...]:
    """Return mounted secret names once each, in first-mounted order."""

    names: dict[str, None] = {}
    for block in services.values():
        assert isinstance(block, Mapping)
        for name in block.get("secrets", ()):
            assert isinstance(name, str)
            names[name] = None
    return tuple(names)


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
    # lock's decimal-gigabyte unit. A definition that may not be swapped also
    # carries memswap_limit, its memory limit plus SWAP_CEILING_BYTES.
    services: dict[str, object] = {}
    for definition in applying_services(inputs):
        block = definition.block(inputs, target)
        limit = memory_limit_bytes(inputs.profile, definition.name)
        services[definition.name] = {
            **block,
            "mem_limit": limit,
            **(
                {"memswap_limit": limit + SWAP_CEILING_BYTES}
                if not definition.swap
                else {}
            ),
        }

    # A secret is declared when a rendered block mounts it, in first-mounted
    # order; a new credential joins through its block's list.
    secrets = {
        name: {"file": f"/etc/gideon/secrets/{name}"}
        for name in _mounted_secret_names(services)
    }
    return {
        "name": PROJECT_NAME,
        "services": services,
        "networks": {NETWORK_NAME: {}},
        "volumes": {"caddy_data": {}, "caddy_config": {}},
        "secrets": secrets,
    }
