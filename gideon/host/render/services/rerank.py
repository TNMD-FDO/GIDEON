"""Compose definition for gideon-rerank-2."""

from gideon.host.render.engine import RERANK
from gideon.host.render.services.model_server import ModelServerService


class RerankService(ModelServerService):
    """The gideon-rerank-2 service in the Compose project."""

    turn_path = True

    def __init__(self) -> None:
        super().__init__(RERANK)
