"""The release retrieval configuration and its validated per-turn overrides."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, TypedDict, cast

RetrievalConfig = TypedDict(
    "RetrievalConfig",
    {
        "leg_limit": int,
        "pool_cap": int,
        "reservations.exact_object": int,
        "rrf.k": int,
        "collapse": Literal["on", "off"],
        "boilerplate_weight": float,
        "query_plan": Literal["off"],
    },
)

# The release's retrieval config. Each number is a starting value its experiment corrects:
# leg_limit, pool_cap, and reservations.exact_object E19, rrf.k E20, boilerplate_weight E21.
RETRIEVAL_CONFIG: Final[Mapping[str, object]] = MappingProxyType({
    "leg_limit": 50,
    "pool_cap": 300,
    "reservations.exact_object": 20,
    "rrf.k": 60,
    "collapse": "on",
    "boilerplate_weight": 0.5,
    "query_plan": "off",
})
# The largest depth an override may ask of a leg or the pool.
_HARD_CAP: Final = 500


@dataclass(frozen=True, slots=True)
class OverrideRefusal:
    """The offending override key and the reason validation failed."""

    key: str
    state: Literal["override_not_allowed", "override_invalid"]


def _valid_override(key: str, value: object) -> bool:
    match key:
        case "leg_limit" | "pool_cap" | "reservations.exact_object":
            return type(value) is int and 0 < value <= _HARD_CAP
        case "rrf.k":
            return type(value) is int and value > 0
        case "collapse":
            return isinstance(value, str) and value in ("on", "off")
        case "boilerplate_weight":
            return (type(value) is int or type(value) is float) and 0 <= value <= 1
        case "query_plan":
            return isinstance(value, str) and value == "off"
        case _:
            return False


def effective_config(overrides: Mapping[str, object] | None = None) -> RetrievalConfig | OverrideRefusal:
    """Apply allowed overrides to a fresh copy of the release configuration."""

    config = dict(RETRIEVAL_CONFIG)
    for key, value in (overrides or {}).items():
        if key not in RETRIEVAL_CONFIG:
            return OverrideRefusal(key, "override_not_allowed")
        if not _valid_override(key, value):
            return OverrideRefusal(key, "override_invalid")
        # One spelling per weight, so 1 and 1.0, or 0 and -0.0, hash alike.
        config[key] = float(cast(float, value)) + 0.0 if key == "boilerplate_weight" else value
    return cast(RetrievalConfig, config)


def config_hash(config: Mapping[str, object]) -> str:
    """Hash the configuration's compact, sorted-key JSON encoding."""

    encoded = json.dumps(dict(config), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
