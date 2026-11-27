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
        "reservations.controlling": int,
        "rrf.k": int,
        "collapse": Literal["on", "off"],
        "boilerplate_weight": float,
        "query_plan": Literal["off"],
        "rerank.stage1": Literal["off"],
        "rerank.stage2": Literal["on", "off"],
        "stage2_depth": int,
        "rerank.semaphore": int,
        "rerank.timeout_s": int,
    },
)

# The release's retrieval config. Each number is a starting value its experiment corrects:
# E19: leg_limit, pool_cap, reservations.exact_object, reservations.controlling.
# E20: rrf.k. E21: boilerplate_weight.
RETRIEVAL_CONFIG: Final[Mapping[str, object]] = MappingProxyType({
    "leg_limit": 50,
    "pool_cap": 300,
    "reservations.exact_object": 20,
    "reservations.controlling": 20,
    "rrf.k": 60,
    "collapse": "on",
    "boilerplate_weight": 0.5,
    "query_plan": "off",
    "rerank.stage1": "off",  # E5 and E22 govern when stage 1 is built.
    "rerank.stage2": "on",  # E22's judgments and the M2 latency reading correct this starting switch.
    "stage2_depth": 40,  # E22 corrects the unpinned depth.
    "rerank.semaphore": 4,  # The service-wide bound on calls in flight, corrected by the M3 reading.
    "rerank.timeout_s": 15,  # The queue wait a turn accepts, corrected by the M3 reading.
})
# The largest depth an override may ask of a leg, the pool, or stage 2.
_HARD_CAP: Final = 500


@dataclass(frozen=True, slots=True)
class OverrideRefusal:
    """The offending override key and the reason validation failed."""

    key: str
    state: Literal["override_not_allowed", "override_invalid"]


def _valid_override(key: str, value: object, *, reranker: bool) -> bool:
    match key:
        case "leg_limit" | "pool_cap" | "reservations.exact_object" | "reservations.controlling" | "stage2_depth":
            return type(value) is int and 0 < value <= _HARD_CAP
        case "rrf.k":
            return type(value) is int and value > 0
        case "collapse":
            return isinstance(value, str) and value in ("on", "off")
        case "rerank.stage2":
            return isinstance(value, str) and (value == "off" or (reranker and value == "on"))
        case "boilerplate_weight":
            return (type(value) is int or type(value) is float) and 0 <= value <= 1
        case "query_plan" | "rerank.stage1":
            return isinstance(value, str) and value == "off"
        case "rerank.semaphore":
            return type(value) is int and value == RETRIEVAL_CONFIG[key]
        case "rerank.timeout_s":
            return type(value) is int and 1 <= value <= 60
        case _:
            return False


def effective_config(
    overrides: Mapping[str, object] | None = None, *, reranker: bool,
) -> RetrievalConfig | OverrideRefusal:
    """Apply allowed overrides to the release configuration for this service."""

    config = dict(RETRIEVAL_CONFIG)
    if not reranker:
        config["rerank.stage2"] = "off"
    for key, value in (overrides or {}).items():
        if key not in RETRIEVAL_CONFIG:
            return OverrideRefusal(key, "override_not_allowed")
        if not _valid_override(key, value, reranker=reranker):
            return OverrideRefusal(key, "override_invalid")
        # One spelling per weight, so 1 and 1.0, or 0 and -0.0, hash alike.
        config[key] = float(cast(float, value)) + 0.0 if key == "boilerplate_weight" else value
    return cast(RetrievalConfig, config)


def config_hash(config: Mapping[str, object]) -> str:
    """Hash the configuration's compact, sorted-key JSON encoding."""

    encoded = json.dumps(dict(config), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
