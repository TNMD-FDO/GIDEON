"""Ordered definitions and projections for Compose services."""

from collections.abc import Mapping

from gideon.host.images import ImagePin, RegistryTarget
from gideon.host.render import RenderInputs


class ServiceDefinition:
    """Base class for one Compose service.

    ``block`` is the service's block without ``mem_limit``, which the document
    appends from the memory table. ``store`` places the service in the tier
    apply converges before any other; ``slow_start_seconds`` is the seconds
    verify allows it to become healthy, so a service carrying one renders a
    healthcheck.
    """

    name: str = ""
    store: bool = False
    slow_start_seconds: int | None = None

    def applies(self, inputs: RenderInputs) -> bool:
        """Whether this service belongs in the rendered project."""

        del inputs
        return True

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        raise NotImplementedError


def image_pin(inputs: RenderInputs, name: str) -> ImagePin:
    pin = next(
        (candidate for candidate in inputs.images.images if candidate.name == name),
        None,
    )
    if pin is None:
        raise ValueError(
            f"Cannot render Compose: images.lock has no {name} pin. "
            f"Add the {name} image to images.lock, then re-run render."
        )
    return pin


def _registered_services() -> tuple[ServiceDefinition, ...]:
    from gideon.host.render.services.api import ApiService
    from gideon.host.render.services.blackbox_exporter import BlackboxExporterService
    from gideon.host.render.services.caddy import CaddyService
    from gideon.host.render.services.cadvisor import CadvisorService
    from gideon.host.render.services.dcgm_exporter import DcgmExporterService
    from gideon.host.render.services.generator import GeneratorService
    from gideon.host.render.services.grafana import GrafanaService
    from gideon.host.render.services.node_exporter import NodeExporterService
    from gideon.host.render.services.open_webui import OpenWebuiService
    from gideon.host.render.services.postgres import PostgresService
    from gideon.host.render.services.postgres_exporter import PostgresExporterService
    from gideon.host.render.services.prometheus import PrometheusService
    from gideon.host.render.services.searxng import SearxngService

    return (
        CaddyService(),
        PrometheusService(),
        NodeExporterService(),
        GrafanaService(),
        PostgresService(),
        OpenWebuiService(),
        GeneratorService(),
        ApiService(),
        SearxngService(),
        DcgmExporterService(),
        PostgresExporterService(),
        CadvisorService(),
        BlackboxExporterService(),
    )


SERVICES: list[ServiceDefinition] = list(_registered_services())


def applying_services(inputs: RenderInputs) -> tuple[ServiceDefinition, ...]:
    """Definitions belonging on this host and site, in document order."""

    return tuple(service for service in SERVICES if service.applies(inputs))


def all_service_names() -> tuple[str, ...]:
    """Every defined service name, regardless of host or site."""

    return tuple(service.name for service in SERVICES)


def store_services() -> tuple[str, ...]:
    """Names of services in the store tier."""

    return tuple(service.name for service in SERVICES if service.store)


def slow_start_services() -> dict[str, int]:
    """Allowances for services that need a longer health wait."""

    return {
        service.name: service.slow_start_seconds
        for service in SERVICES
        if service.slow_start_seconds is not None
    }
