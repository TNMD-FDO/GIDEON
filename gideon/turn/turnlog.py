"""Collect research turn facts and dispatch a content-free event row."""

import ast
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal, Protocol

from gideon import guardrail
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
# Copy of evaluation/evalset.py across the service's mounted-source boundary.
EVAL_SET_VERSION: Final[str] = "eval-v1"
_PRODUCT_INITIALIZER: Final[Path] = Path(__file__).parent.parent / "__init__.py"

Outcome = Literal["reply", "complete", "truncated", "tripped", "failed", "disconnected"]


class _Passage(Protocol):
    @property
    def chunk_id(self) -> str: ...

    @property
    def pool_rank(self) -> int: ...


@dataclass(frozen=True, slots=True)
class AttemptCounters:
    """The thinking mode, finish reason, and last token counts of one attempt."""

    thinking: bool
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None


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
    evidence: Sequence[_Passage] | None = None
    generator_repository: str | None = None
    engine_counters: list[AttemptCounters] = field(default_factory=list)
    outcome: Outcome | None = None
    failure_state: StrEnum | None = None
    failure_word: str | None = None
    trip_family: str | None = None


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


def _pool_entry(entry: Mapping[str, object]) -> dict[str, object]:
    ranks = entry.get("leg_ranks")
    score = entry.get("score")
    stage2_score = entry.get("stage2_score")
    boilerplate = entry.get("boilerplate")
    pinned = entry.get("pinned")
    tier = entry.get("authority_tier")
    controlling_rank = entry.get("controlling_rank")
    return {
        "chunk_id": guardrail.bounded_id(entry.get("chunk_id")),
        "pool_rank": _nonnegative_int(entry.get("pool_rank")),
        "rank": _nonnegative_int(entry.get("rank")),
        "score": score if type(score) in (int, float) else None,
        "stage2_score": (
            stage2_score if (type(stage2_score) is int or type(stage2_score) is float) and 0 <= stage2_score <= 1 else None
        ),
        "leg_ranks": {
            leg: rank for leg, rank in ranks.items()
            if guardrail.bounded_id(leg) is not None and _nonnegative_int(rank) is not None
        } if isinstance(ranks, Mapping) else {},
        "dupe_cluster": guardrail.bounded_id(entry.get("dupe_cluster")),
        "boilerplate": boilerplate if isinstance(boilerplate, bool) else None,
        "authority_tier": tier if isinstance(tier, str) and tier in AUTHORITY_TIERS else None,
        "pinned": pinned if isinstance(pinned, bool) else None,
        "object": _nonnegative_int(entry.get("object")),
        "reserved": "controlling" if entry.get("reserved") == "controlling" else None,
        "controlling_rank": controlling_rank if type(controlling_rank) is int and controlling_rank > 0 else None,
    }


def _evidence_entry(number: int, passage: _Passage) -> dict[str, object]:
    return {
        "number": number,
        "chunk_id": guardrail.bounded_id(passage.chunk_id),
        "pool_rank": _nonnegative_int(passage.pool_rank),
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


def record_turn(turn: TurnLog, *, elapsed_ms: Callable[[], float]) -> None:
    """Dispatch the row without affecting the turn's response."""

    try:
        outcome = turn.outcome or "disconnected"
        timings = dict(turn.timings_ms)
        if outcome in ("failed", "disconnected"):
            timings["total"] = elapsed_ms()

        provenance = dict(turn.provenance) if turn.provenance is not None else None
        objects = _out_of_corpus(provenance.pop("out_of_corpus_objects", None)) if provenance is not None else None
        pool = [_pool_entry(entry) for entry in turn.pool] if turn.pool is not None else None
        evidence = (
            [_evidence_entry(number, passage) for number, passage in enumerate(turn.evidence, start=1)]
            if turn.evidence is not None else None
        )
        counters = [_attempt_entry(attempt) for attempt in turn.engine_counters] or None

        guardrail.dispatch_event_row(
            TURN_LOG_TABLE,
            TURN_LOG_PARTITION_FUNCTION,
            TURN_LOG_COLUMNS,
            (
                guardrail.bounded_id(turn.chat_id),
                guardrail.bounded_id(turn.message_id),
                turn.source if turn.source in guardrail.SOURCE_VOCABULARY else guardrail.USER_SOURCE,
                turn.stop_after,
                outcome,
                turn.failure_state.value if outcome == "failed" and turn.failure_state is not None else None,
                turn.failure_word if outcome == "failed" else None,
                turn.trip_family if outcome == "tripped" and turn.trip_family in guardrail.REFUSAL_BY_FAMILY else None,
                product_version(),
                EVAL_SET_VERSION,
                _json_column(list(turn.corpus_lockfiles)) if turn.corpus_lockfiles is not None else None,
                turn.generation_id,
                turn.embedding_space,
                turn.generator_repository,
                turn.retrieval_config_hash,
                _json_column(turn.overrides),
                turn.plan_mode,
                _json_column(timings),
                _json_column(provenance),
                _json_column(objects),
                _json_column(pool),
                _json_column(evidence),
                _json_column(counters),
                None,  # support_level
                None,  # gate_report
                None,  # misses
            ),
        )
    except Exception:  # noqa: BLE001 - recording cannot affect the response.
        return
