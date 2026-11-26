"""Select released generator sampling and build streamed completion requests."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class GeneratorSampling:
    """Default sampling held to the digest of its generation config."""

    config_digest: str
    fields: Mapping[str, int | float]


# exempt: the pinned model card's settings, corrected at a generator bump. Each entry carries the digest of the
# generation_config.json it was read from, so a bump that moves the file is refused until the entry is read again;
# a file without min_p leaves the engine's zero, set here explicitly.
GENERATOR_SAMPLING: Final[Mapping[str, GeneratorSampling]] = {
    "Qwen/Qwen3.8-27B-FP8": GeneratorSampling(
        config_digest="sha256:e70c136c1b78ddc1fb0905bac8e733a4dc448d4f852a5dd75143fffc70be550e",
        fields={"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0},
    ),
}
# A starting value E30 corrects: room for the reasoning and the answer in one stream.
RESEARCH_MAX_TOKENS: Final = 16_384


def sampling_for(repository: str) -> GeneratorSampling | None:
    return GENERATOR_SAMPLING.get(repository)


def request_body(
    served_model_name: str, messages: Sequence[Mapping[str, str]], sampling: GeneratorSampling,
    *, thinking_off: bool = False,
) -> dict[str, object]:
    """Build the request with the model's defaults and an optional thinking-off switch."""

    body: dict[str, object] = {
        "model": served_model_name,
        "messages": [dict(message) for message in messages],
        "max_tokens": RESEARCH_MAX_TOKENS,
        **sampling.fields,
        "stream": True,
    }
    if thinking_off:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    return body
