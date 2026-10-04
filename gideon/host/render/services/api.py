"""Compose definition for gideon-api."""

from collections.abc import Mapping

from gideon.host.images import RegistryTarget
from gideon.host.render import RenderInputs
from gideon.host.render.api import API_SERVICE_NAME, api_enabled
from gideon.host.render.services import ServiceDefinition


class ApiService(ServiceDefinition):
    """The gideon-api service in the Compose project."""

    name = API_SERVICE_NAME

    def applies(self, inputs: RenderInputs) -> bool:
        return api_enabled(inputs.no_gpu)

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        # The block's body stays in render/compose.py, where a test replaces
        # the names it reads through that module's namespace; the import is
        # function-level because compose.py imports this package.
        from gideon.host.render.compose import api_service

        return api_service(inputs, target)
