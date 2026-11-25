"""Render the mounted template that formats a reranker's query and passage."""

from typing import Final

from gideon.host.render import Artifact, RenderInputs, template_text
from gideon.host.render.engine import MODEL_SERVERS

SCORE_TEMPLATE_PATH: Final = "model-servers/qwen3_reranker.jinja"
SCORE_TEMPLATE_SOURCE: Final = f"compose/{SCORE_TEMPLATE_PATH}"
SCORE_TEMPLATE_MOUNT_TARGET: Final = f"/etc/gideon/{SCORE_TEMPLATE_PATH}"
SCORE_TEMPLATE_MOUNT: Final = (
    f"/etc/gideon/rendered/{SCORE_TEMPLATE_PATH}:{SCORE_TEMPLATE_MOUNT_TARGET}:ro"
)


class ScoreTemplateArtifact(Artifact):
    """The verbatim template owned by model servers that mount it."""

    name = "model-servers-score-template"
    relative_path = SCORE_TEMPLATE_PATH
    owners = tuple(member.service_name for member in MODEL_SERVERS if member.score_template)
    source_paths = (SCORE_TEMPLATE_SOURCE,)

    def applies(self, inputs: RenderInputs) -> bool:
        return bool(self.rendered_owners(inputs))

    def rendered_owners(self, inputs: RenderInputs) -> tuple[str, ...]:
        from gideon.host.render.services.model_server import model_server_members

        return tuple(
            member.service_name for member in model_server_members(inputs) if member.score_template
        )

    def emit(self, inputs: RenderInputs) -> str:
        return template_text(inputs, SCORE_TEMPLATE_SOURCE)
