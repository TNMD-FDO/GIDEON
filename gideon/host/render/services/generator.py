"""Compose definition for gideon-generator."""

from collections.abc import Mapping

from gideon.host.images import RegistryTarget
from gideon.host.models import ModelPin
from gideon.host.render import RenderInputs
from gideon.host.render.engine import ENGINE_READY_SECONDS as ENGINE_READY_SECONDS
from gideon.host.render.engine import GENERATOR
from gideon.host.render.services.model_server import (
    ENGINE_ACCESS_LOG_EXCLUDED_PATHS as ENGINE_ACCESS_LOG_EXCLUDED_PATHS,
)
from gideon.host.render.services.model_server import (
    ENGINE_HEALTH_PATH as ENGINE_HEALTH_PATH,
)
from gideon.host.render.services.model_server import (
    ENGINE_HEALTHCHECK as ENGINE_HEALTHCHECK,
)
from gideon.host.render.services.model_server import (
    ENGINE_SERVER as ENGINE_SERVER,
)
from gideon.host.render.services.model_server import (
    ENGINE_USAGE_SWITCHES as ENGINE_USAGE_SWITCHES,
)
from gideon.host.render.services.model_server import (
    ModelServerService,
    model_command,
    model_pin,
    model_service,
    model_wrapper,
)


def engine_wrapper(secret_path: str, server: str) -> list[str]:
    """Build the generator's fail-closed API-key entrypoint."""

    return model_wrapper(secret_path, server, GENERATOR)


def generator_pin(inputs: RenderInputs) -> ModelPin:
    """Return the profile's generator pin."""

    return model_pin(inputs, GENERATOR)


def engine_command(pin: ModelPin) -> list[str]:
    """Build the generator command from its locked serving baseline."""

    return model_command(pin, GENERATOR)


def engine_service(
    inputs: RenderInputs, target: RegistryTarget | None = None
) -> Mapping[str, object]:
    """Build the GPU-only generator service from release and profile inputs."""

    return model_service(inputs, GENERATOR, target)


class GeneratorService(ModelServerService):
    """The gideon-generator service in the Compose project."""

    def __init__(self) -> None:
        super().__init__(GENERATOR)
