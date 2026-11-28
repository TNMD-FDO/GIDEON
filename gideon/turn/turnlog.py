"""Collect research turn facts and dispatch a content-free event row."""

import ast
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal, Protocol

from gideon import guardrail
from gideon.turn.finalset import SUPPORT_LEVELS, SupportLevel
from gideon.turn.tier import AUTHORITY_TIERS

TURN_LOG_TABLE: Final[str] = "turn_log"
TURN_LOG_PARTITION_FUNCTION: Final[str] = "turn_log_ensure_partition"
TURN_LOG_COLUMNS: Final[tuple[str, ...]] = (
    "chat_id",
    "message_id",
    "source",
    "stop_after",
    "outcome",
    "failure_state",
    "failure_word",
    "trip_family",
    "product_version",
    "eval_set_version",
    "corpus_lockfiles",
    "generation_id",
    "embedding_space",
    "generator_repository",
    "retrieval_config_hash",
    "overrides",
    "plan_mode",
    "timings_ms",
    "provenance",
    "out_of_corpus_objects",
    "pool",
    "evidence",
    "engine_counters",
    "support_level",
    "gate_report",
    "misses",
)
_JSON_COLUMNS: Final = frozenset({
    "corpus_lockfiles", "overrides", "timings_ms", "provenance", "out_of_corpus_objects",
    "pool", "evidence", "engine_counters", "gate_report", "misses",
})
# Copy of evaluation/evalset.py across the service's mounted-source boundary.
EVAL_SET_VERSION: Final[str] = "eval-v1"
_PRODUCT_INITIALIZER: Final[Path] = Path(__file__).parent.parent / "__init__.py"

Outcome = Literal["reply", "complete", "truncated", "tripped", "abstained", "failed", "disconnected"]


class _EvidenceHit(Protocol):
    @property
    def number(self) -> int: ...

    @property
    def chunk_id(self) -> str: ...

    @property
    def pool_rank(self) -> int: ...

    @property
    def rank(self) -> int: ...

    @property
    def stage2_score(self) -> float | None: ...

    @property
    def authority_tier(self) -> str: ...

    @property
    def seat(self) -> str: ...

    @property
    def object(self) -> int | None: ...

    @property
    def doc_id(self) -> str: ...

    @property
    def tokens(self) -> int: ...


@dataclass(frozen=True, slots=True)
class AttemptCounters:
    """The thinking mode, finish reason, and last token counts of one attempt."""

    thinking: bool
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class AbstentionMiss:
    """The floor and the scored candidates it excluded from an unsupported turn."""

    floor: float
    best_score: float | None
    chunk_ids: tuple[str, ...]


@dataclass(repr=False, slots=True)
class TurnLog:
    """Accumulate the admitted request's validated facts as its stages finish."""

    source: str
    chat_id: str | None
    message_id: str | None
    stop_after: str | None
    retrieval_config_hash: str
    overrides: dict[str, object] | None
    plan_mode: str
    generation_id: str | None = None
    corpus_lockfiles: Sequence[str] | None = None
    embedding_space: str | None = None
    timings_ms: dict[str, float | None] = field(default_factory=dict)
    provenance: Mapping[str, object] | None = None
    pool: Sequence[Mapping[str, object]] | None = None
    evidence: Sequence[_EvidenceHit] | None = None
    generator_repository: str | None = None
    engine_counters: list[AttemptCounters] = field(default_factory=list)
    outcome: Outcome | None = None
    failure_state: StrEnum | None = None
    failure_word: str | None = None
    trip_family: str | None = None
    support_level: SupportLevel | None = None
    miss: AbstentionMiss | None = None
    sent_count: int = 0


def product_version() -> str:
    """Read the release assignment from the mounted initializer at write time."""

    try:
        module = ast.parse(_PRODUCT_INITIALIZER.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, SyntaxError, ValueError):
        return "unknown"
    for statement in module.body:
        match statement:
            case ast.Assign(targets=[ast.Name(id="__version__")], value=ast.Constant(value=str(version))) if version:
                return version
    return "unknown"


def _out_of_corpus(entries: object) -> list[dict[str, str]] | None:
    if not isinstance(entries, list):
        return None
    projected = []
    for entry in entries:
        fields = entry if isinstance(entry, Mapping) else {}
        projected.append({
            "type": guardrail.bounded_id(fields.get("type")) or "unknown",
            "resolved": guardrail.bounded_id(fields.get("resolved")) or "unresolved",
        })
    return projected


def _nonnegative_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _positive_int(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _stage2_score(value: object) -> float | None:
    return value if (type(value) is int or type(value) is float) and 0 <= value <= 1 else None


def _authority_tier(value: object) -> str | None:
    return value if isinstance(value, str) and value in AUTHORITY_TIERS else None


def _pool_entry(entry: Mapping[str, object]) -> dict[str, object]:
    ranks = entry.get("leg_ranks")
    score = entry.get("score")
    boilerplate = entry.get("boilerplate")
    pinned = entry.get("pinned")
    return {
        "chunk_id": guardrail.bounded_id(entry.get("chunk_id")),
        "pool_rank": _nonnegative_int(entry.get("pool_rank")),
        "rank": _nonnegative_int(entry.get("rank")),
        "score": score if type(score) in (int, float) else None,
        "stage2_score": _stage2_score(entry.get("stage2_score")),
        "leg_ranks": {
            leg: rank for leg, rank in ranks.items()
            if guardrail.bounded_id(leg) is not None and _nonnegative_int(rank) is not None
        } if isinstance(ranks, Mapping) else {},
        "dupe_cluster": guardrail.bounded_id(entry.get("dupe_cluster")),
        "boilerplate": boilerplate if isinstance(boilerplate, bool) else None,
        "authority_tier": _authority_tier(entry.get("authority_tier")),
        "pinned": pinned if isinstance(pinned, bool) else None,
        "object": _nonnegative_int(entry.get("object")),
        "reserved": "controlling" if entry.get("reserved") == "controlling" else None,
        "controlling_rank": _positive_int(entry.get("controlling_rank")),
    }


def _evidence_entry(hit: _EvidenceHit) -> dict[str, object]:
    seat = hit.seat
    return {
        "number": _positive_int(hit.number),
        "chunk_id": guardrail.bounded_id(hit.chunk_id),
        "pool_rank": _nonnegative_int(hit.pool_rank),
        "rank": _nonnegative_int(hit.rank),
        "stage2_score": _stage2_score(hit.stage2_score),
        "authority_tier": _authority_tier(hit.authority_tier),
        "seat": seat if isinstance(seat, str) and seat in ("pin", "controlling", "score") else None,
        "object": _nonnegative_int(hit.object),
        "doc_id": guardrail.bounded_id(hit.doc_id),
        "tokens": _positive_int(hit.tokens),
    }


def _attempt_entry(attempt: AttemptCounters) -> dict[str, object]:
    return {
        "thinking": attempt.thinking,
        "finish_reason": guardrail.bounded_id(attempt.finish_reason),
        "prompt_tokens": _nonnegative_int(attempt.prompt_tokens),
        "completion_tokens": _nonnegative_int(attempt.completion_tokens),
        "reasoning_tokens": _nonnegative_int(attempt.reasoning_tokens),
    }


def _json_column(value: object) -> str | None:
    return None if value is None else json.dumps(value, separators=(",", ":"), allow_nan=False)


def _misses(miss: AbstentionMiss | None) -> list[dict[str, object]] | None:
    if miss is None:
        return None
    return [{
        "kind": "abstention",
        "floor": _stage2_score(miss.floor),
        "best_score": _stage2_score(miss.best_score),
        "chunk_ids": [
            identifier for chunk_id in miss.chunk_ids if (identifier := guardrail.bounded_id(chunk_id)) is not None
        ],
    }]


def kept_values(turn: TurnLog, *, elapsed_ms: Callable[[], float]) -> dict[str, object]:
    """Project the turn's facts by column name before serialization."""

    outcome = turn.outcome or "disconnected"
    timings = dict(turn.timings_ms)
    if outcome in ("failed", "disconnected"):
        timings["total"] = elapsed_ms()

    provenance = dict(turn.provenance) if turn.provenance is not None else None
    objects = _out_of_corpus(provenance.pop("out_of_corpus_objects", None)) if provenance is not None else None
    return {
        "chat_id": guardrail.bounded_id(turn.chat_id),
        "message_id": guardrail.bounded_id(turn.message_id),
        "source": turn.source if turn.source in guardrail.SOURCE_VOCABULARY else guardrail.USER_SOURCE,
        "stop_after": turn.stop_after,
        "outcome": outcome,
        "failure_state": turn.failure_state.value if outcome == "failed" and turn.failure_state is not None else None,
        "failure_word": turn.failure_word if outcome == "failed" else None,
        "trip_family": turn.trip_family if outcome == "tripped" and turn.trip_family in guardrail.REFUSAL_BY_FAMILY else None,
        "product_version": product_version(),
        "eval_set_version": EVAL_SET_VERSION,
        "corpus_lockfiles": list(turn.corpus_lockfiles) if turn.corpus_lockfiles is not None else None,
        "generation_id": turn.generation_id,
        "embedding_space": turn.embedding_space,
        "generator_repository": turn.generator_repository,
        "retrieval_config_hash": turn.retrieval_config_hash,
        "overrides": turn.overrides,
        "plan_mode": turn.plan_mode,
        "timings_ms": timings,
        "provenance": provenance,
        "out_of_corpus_objects": objects,
        "pool": [_pool_entry(entry) for entry in turn.pool] if turn.pool is not None else None,
        "evidence": [_evidence_entry(hit) for hit in turn.evidence] if turn.evidence is not None else None,
        "engine_counters": [_attempt_entry(attempt) for attempt in turn.engine_counters] or None,
        "support_level": turn.support_level if turn.support_level in SUPPORT_LEVELS else None,
        "gate_report": None,
        "misses": _misses(turn.miss),
    }


def record_turn(turn: TurnLog, *, elapsed_ms: Callable[[], float]) -> dict[str, object] | None:
    """Dispatch the row silently and return the kept values if projection succeeded."""

    values: dict[str, object] | None = None
    try:
        values = kept_values(turn, elapsed_ms=elapsed_ms)
        bound = tuple(
            _json_column(values[column]) if column in _JSON_COLUMNS else values[column]
            for column in TURN_LOG_COLUMNS
        )
        guardrail.dispatch_event_row(
            TURN_LOG_TABLE, TURN_LOG_PARTITION_FUNCTION, TURN_LOG_COLUMNS, bound,
        )
    except Exception:  # noqa: BLE001 - recording cannot affect the response.
        pass
    return values
