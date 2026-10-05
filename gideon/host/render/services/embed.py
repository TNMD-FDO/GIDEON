"""Compose definition for gideon-embed."""

from gideon.host.render.engine import EMBED
from gideon.host.render.services.model_server import ModelServerService


class EmbedService(ModelServerService):
    """The gideon-embed service in the Compose project."""

    def __init__(self) -> None:
        super().__init__(EMBED)
