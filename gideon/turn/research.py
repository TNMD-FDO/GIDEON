"""The release research configuration, its validation, and the combined turn hash."""

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Final

from gideon.turn.config import config_hash

RESEARCH_CONFIG: Final[Mapping[str, object]] = MappingProxyType({
    "tag_grammar": "off",  # E27 measures structured source tags.
    "release_unit": "paragraph",  # E28 measures the unit released after verification.
    "repair_bound": 1,  # E31 measures the bounded repair attempt count.
    "max_tokens": 16_384,  # E30 measures space for reasoning and the answer in one stream.
    "evidence_order": "tier",  # E29 measures the order of evidence in the prompt.
    "history_form": "name_only",  # exempt: the prompt carries earlier answers without citation strings.
    "analysis_marker": "on",  # exempt: the gate marks the sentences it classifies as analysis.
    "chip_document": "chunk",  # exempt: a source chip opens the evidence chunk.
    "nested_citations": "strict",  # exempt: the gate verifies citations inside quoted text.
})


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


ADMITTED_VALUES: Final[Mapping[str, tuple[str | int, ...] | Callable[[object], bool]]] = MappingProxyType({
    "tag_grammar": ("off",),
    "release_unit": ("paragraph",),
    "repair_bound": (1,),
    "max_tokens": _positive_int,
    "evidence_order": ("tier",),
    "history_form": ("name_only",),
    "analysis_marker": ("on",),
    "chip_document": ("chunk",),
    "nested_citations": ("strict",),
})


def check_research_config(config: Mapping[str, object]) -> str | None:
    """Return the first missing, unsupported, or unknown key, if any."""

    for key, admitted in ADMITTED_VALUES.items():
        if key not in config:
            return key
        value = config[key]
        valid = (
            admitted(value) if callable(admitted)
            else any(type(value) is type(option) and value == option for option in admitted)
        )
        if not valid:
            return key
    return next((key for key in config if key not in ADMITTED_VALUES), None)


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def turn_hash(retrieval: Mapping[str, object], research: Mapping[str, object] = RESEARCH_CONFIG) -> str:
    """Hash the effective retrieval and research settings in separate JSON entries."""

    return config_hash({"retrieval": _json_value(retrieval), "research": _json_value(research)})
