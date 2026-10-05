"""The model servers' identities as their clients see them.

A model server serves one pinned model on the engine image; the family is one
member per model role of the lock, the generator first. A member holds its
service name (the stable alias), container port, secret name, scrape job name,
slow-start allowance, and the words its start refusal uses for the key file.
Its served name and GPU are its pin's, the served name held equal to the
service name by a test. The generator's identity is shared by its Compose
definition (``render/services/generator.py``), the frontend's connection
(``render/owui.py``), Prometheus's configuration, and Grafana's "Engine down"
rule; it lives here so the frontend module can name the engine without
importing the document builder that imports it.
"""

from dataclasses import dataclass
from typing import Final

ENGINE_SERVICE_NAME: Final = "gideon-generator"
ENGINE_SECRET_NAME: Final = "engine_api_key"
ENGINE_PORT: Final = 8000
ENGINE_JOB_NAME: Final = "engine"
ENGINE_READY_SECONDS: Final = 900

EMBED_SERVICE_NAME: Final = "gideon-embed"
EMBED_SECRET_NAME: Final = "embed_api_key"
EMBED_JOB_NAME: Final = "embed"
EMBED_PORT: Final = 8000
# The smallest whole multiple of 300 seconds at least four times the slower of
# two measured starts on the reference box: a fresh container healthy after
# 57.7 s, and the same container after its weights were evicted from the page
# cache after 24.6 s (its compile cache kept). A slower start re-derives it.
# exempt: a starting bound no register measures
EMBED_READY_SECONDS: Final = 300


@dataclass(frozen=True, slots=True)
class ModelServerMember:
    """One model role's service identity and startup allowance."""

    role: str
    service_name: str
    port: int
    secret_name: str
    job_name: str
    ready_seconds: int
    key_file_words: str


GENERATOR: Final = ModelServerMember(
    "generator",
    ENGINE_SERVICE_NAME,
    ENGINE_PORT,
    ENGINE_SECRET_NAME,
    ENGINE_JOB_NAME,
    ENGINE_READY_SECONDS,
    "engine API key",
)
EMBED: Final = ModelServerMember(
    "embed",
    EMBED_SERVICE_NAME,
    EMBED_PORT,
    EMBED_SECRET_NAME,
    EMBED_JOB_NAME,
    EMBED_READY_SECONDS,
    "embedding server API key",
)
MODEL_SERVERS: Final = (GENERATOR, EMBED)


def model_server(role: str) -> ModelServerMember | None:
    """Return the member for a model role, if present."""

    return next((member for member in MODEL_SERVERS if member.role == role), None)


def base_url(member: ModelServerMember) -> str:
    """Return a member's OpenAI-compatible API base URL."""

    return f"http://{member.service_name}:{member.port}/v1"


def metrics_target(member: ModelServerMember) -> str:
    """Return a member's Prometheus target as a Compose host and port."""

    return f"{member.service_name}:{member.port}"


def engine_base_url() -> str:
    """Return the generator's OpenAI-compatible API base URL."""

    return base_url(GENERATOR)


def engine_metrics_target() -> str:
    """Return the generator's Prometheus target as a Compose host and port."""

    return metrics_target(GENERATOR)
