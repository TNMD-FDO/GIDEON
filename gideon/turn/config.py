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
        "final_k": int,
        "hierarchy.mode": Literal["weighted", "lexicographic", "off"],
        "hierarchy.weights": Mapping[str, float],
        "final_reserved_controlling": int,
        "per_document_cap": int,
        "per_hit_chunks": int,
        "context_budget_tokens": int,
        "abstention.floor": float,
        "abstention.thin_below": int,
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
    "final_k": 20,  # E22 and E25 correct the unpinned depth of the final set.
    "hierarchy.mode": "weighted",  # E23 compares weighted and lexicographic order.
    "hierarchy.weights": MappingProxyType({  # E23's starting weights, in AUTHORITY_TIERS order.
        "scotus": 1.0,
        "circuit_published": 0.92,
        "circuit_unpublished": 0.70,
        "district_home": 0.60,
        "state_home": 0.60,
        "persuasive": 0.35,
        "authority": 1.0,
        "matter": 1.0,
    }),
    "final_reserved_controlling": 5,  # E23 corrects the reserved seats.
    "per_document_cap": 3,  # E24 corrects the cap.
    "per_hit_chunks": 3,  # E12 governs when neighbour expansion is enabled.
    "context_budget_tokens": 40_000,  # E25 corrects the retrieved-token budget.
    "abstention.floor": 0.5,  # E26 tunes this placeholder; 0 is the off state.
    "abstention.thin_below": 3,  # exempt: thin support is one or two hits.
})
# The largest retrieval depth or hit threshold an override may ask for.
_HARD_CAP: Final = 500
# The widest budget an override may ask: E25's widest arm.
_BUDGET_CEILING: Final = 80_000


@dataclass(frozen=True, slots=True)
class OverrideRefusal:
    """The offending override key and the reason validation failed."""

    key: str
    state: Literal["override_not_allowed", "override_invalid"]


def _valid_override(key: str, value: object, *, reranker: bool) -> bool:
    match key:
        case (
            "leg_limit" | "pool_cap" | "reservations.exact_object" | "reservations.controlling"
            | "stage2_depth" | "final_k" | "abstention.thin_below"
        ):
            return type(value) is int and 0 < value <= _HARD_CAP
        case "rrf.k":
            return type(value) is int and value > 0
        case "collapse":
            return isinstance(value, str) and value in ("on", "off")
        case "rerank.stage2":
            return isinstance(value, str) and (value == "off" or (reranker and value == "on"))
        case "boilerplate_weight" | "abstention.floor":
            return (type(value) is int or type(value) is float) and 0 <= value <= 1
        case "query_plan" | "rerank.stage1":
            return isinstance(value, str) and value == "off"
        case "rerank.semaphore" | "per_hit_chunks":
            return type(value) is int and value == RETRIEVAL_CONFIG[key]
        case "rerank.timeout_s":
            return type(value) is int and 1 <= value <= 60
        case "hierarchy.mode":
            return isinstance(value, str) and value in ("weighted", "lexicographic", "off")
        case "hierarchy.weights":
            release_weights = cast(Mapping[str, float], RETRIEVAL_CONFIG[key])
            return (
                isinstance(value, Mapping) and value.keys() == release_weights.keys()
                and all((type(weight) is int or type(weight) is float) and 0 <= weight <= 1 for weight in value.values())
            )
        case "final_reserved_controlling" | "per_document_cap":
            return type(value) is int and 0 <= value <= _HARD_CAP
        case "context_budget_tokens":
            return type(value) is int and 0 < value <= _BUDGET_CEILING
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
        # One float spelling, so 1 and 1.0, or 0 and -0.0, hash alike.
        config[key] = float(cast(float, value)) + 0.0 if key in ("boilerplate_weight", "abstention.floor") else value
    effective = cast(RetrievalConfig, config)
    if effective["final_reserved_controlling"] > effective["final_k"]:
        return OverrideRefusal("final_reserved_controlling", "override_invalid")
    if effective["rerank.stage2"] == "off":
        if overrides is not None and "abstention.floor" in overrides and effective["abstention.floor"] != 0.0:
            return OverrideRefusal("abstention.floor", "override_invalid")
        effective["abstention.floor"] = 0.0
    weights = effective["hierarchy.weights"]
    release_weights = cast(Mapping[str, float], RETRIEVAL_CONFIG["hierarchy.weights"])
    effective["hierarchy.weights"] = MappingProxyType({tier: float(weights[tier]) + 0.0 for tier in release_weights})
    return effective


def _mapping_value(value: object) -> dict[object, object]:
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"{type(value).__name__} is not a JSON value")


def config_hash(config: Mapping[str, object]) -> str:
    """Hash the configuration's compact, sorted-key JSON encoding, nested mappings included."""

    encoded = json.dumps(dict(config), default=_mapping_value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
