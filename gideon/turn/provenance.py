"""Build the retrieval reply from identifiers, ranks, and timings."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Protocol

from gideon.turn.fusion import FusionResult

if TYPE_CHECKING:
    from gideon.turn.config import RetrievalConfig
    from gideon.turn.finalset import FinalSet
    from gideon.turn.objectleg import ObjectLegResult
    from gideon.turn.objects import TurnObject
    from gideon.turn.scope import StatedScope


class _Generation(Protocol):
    @property
    def gen_id(self) -> str: ...

    @property
    def collection(self) -> str: ...

    @property
    def lockfiles(self) -> Sequence[str]: ...

    @property
    def embedding_space(self) -> str: ...


@dataclass(frozen=True, slots=True)
class LegRun:
    """The store and depth of one leg, with the number of hits it returned."""

    store: str
    limit: int
    returned: int


@dataclass(frozen=True, slots=True)
class ControllingLegRun(LegRun):
    """A controlling leg's run and the sorted court ids in its filter."""

    courts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RequestSpan:
    """A store request's start and end on the monotonic clock in seconds."""

    start: float
    end: float

    @property
    def seconds(self) -> float:
        return self.end - self.start


def retrieval_milliseconds(spans: Sequence[RequestSpan | None], fusion_seconds: float) -> float:
    """Return elapsed store time, counting overlap once, plus fusion time."""

    first, *rest = sorted((span for span in spans if span is not None), key=lambda span: span.start)
    store_seconds = first.seconds
    covered_until = first.end
    for span in rest:
        store_seconds += max(0.0, span.end - max(covered_until, span.start))
        covered_until = max(covered_until, span.end)
    return (store_seconds + fusion_seconds) * 1000


@dataclass(frozen=True, slots=True)
class TurnTimings:
    """Elapsed seconds for the turn stages and spans for the store requests."""

    record: float
    extraction: float
    embedding: float
    dense: RequestSpan
    controlling: RequestSpan | None
    lexical: RequestSpan
    objects: RequestSpan | None
    fusion: float
    rerank_passages: float
    rerank_queue: float
    rerank: float
    passages: float
    total: float

    def to_milliseconds(self) -> dict[str, float]:
        return {
            "record": self.record * 1000,
            "extraction": self.extraction * 1000,
            "embedding": self.embedding * 1000,
            "dense": self.dense.seconds * 1000,
            "controlling": self.controlling.seconds * 1000 if self.controlling is not None else 0.0,
            "lexical": self.lexical.seconds * 1000,
            "objects": self.objects.seconds * 1000 if self.objects is not None else 0.0,
            "fusion": self.fusion * 1000,
            "rerank_passages": self.rerank_passages * 1000,
            "rerank_queue": self.rerank_queue * 1000,
            "rerank": self.rerank * 1000,
            "passages": self.passages * 1000,
            "retrieval": retrieval_milliseconds((self.dense, self.controlling, self.lexical, self.objects), self.fusion),
            "total": self.total * 1000,
        }


def retrieval_provenance(
    *, config: RetrievalConfig, collection: str, legs: Mapping[str, LegRun], fusion: FusionResult,
    objects: Sequence[TurnObject], object_leg: ObjectLegResult, stated_scope: StatedScope | None, final_set: FinalSet,
) -> dict[str, object]:
    """Describe how the message's one sub-query produced the pool and final set."""

    seats = Counter(entry.object for entry in fusion.entries if entry.pinned)
    object_entries: list[dict[str, object]] = []
    out_of_corpus_objects: list[dict[str, object]] = []
    for obj in objects:
        exact = obj.exact
        results = object_leg.by_object.get(obj.index, ())
        hits = {result.search.name: len(result.hits) for result in results}
        out_of_corpus = (
            hits.get("held", 0) == 0 if exact.type == "case_cite" else not any(hits.values())
        )
        object_entries.append({
            "index": obj.index,
            "type": exact.type,
            "start": exact.start,
            "end": exact.end,
            "pattern_id": exact.pattern_id,
            "fields": [result.search.field for result in results],
            "leg": object_leg.owners.get(obj.index),
            "hits": hits,
            "seats": seats[obj.index],
            "out_of_corpus": out_of_corpus,
        })
        if out_of_corpus:
            out_of_corpus_objects.append({
                "index": obj.index,
                "type": exact.type,
                "resolved": "unresolved_in_scope" if stated_scope is not None else "unresolved",
                "citing_hits": hits.get("citing", 0),
            })

    if stated_scope is not None:
        out_of_corpus_objects.extend({"type": "court", "resolved": court} for court in stated_scope.out_of_corpus)

    scored = [entry for entry in fusion.entries if entry.stage2_score is not None]
    mode = config["hierarchy.mode"]
    ordered_by = "rank" if config["rerank.stage2"] == "off" else "score" if mode == "off" else mode
    return {
        "plan_mode": config["query_plan"],
        "sub_queries": [{"source": "message"}],
        "objects": object_entries,
        "out_of_corpus_objects": out_of_corpus_objects,
        "filters": {
            "floor": {"collections": [collection], "kb_ids": []},
            "user_stated_scope": None if stated_scope is None else {
                "named": [
                    {"court": named.court, "start": named.start, "end": named.end}
                    for named in stated_scope.named
                ],
                "courts": list(stated_scope.courts),
                "out_of_corpus": list(stated_scope.out_of_corpus),
            },
            "narrowing": None,
        },
        "legs": {
            **{name: asdict(run) for name, run in legs.items()},
            "objects": {
                "store": "opensearch", "limit": object_leg.limit,
                "returned": object_leg.returned, "searches": object_leg.searches,
            },
        },
        "tools": [],
        "fusion": dict(fusion.counts),
        "rerank": {
            "stage1": config["rerank.stage1"],
            "stage2": config["rerank.stage2"],
            "depth": config["stage2_depth"],
            "scored": len(scored),
            "pinned_scored": sum(entry.pinned for entry in scored),
            "reserved_scored": sum(entry.reserved is not None and not entry.pinned for entry in scored),
        },
        "final_set": {
            "k": config["final_k"],
            "mode": mode,
            "ordered_by": ordered_by,
            "reserved_controlling": config["final_reserved_controlling"],
            "per_document_cap": config["per_document_cap"],
            "budget_tokens": config["context_budget_tokens"],
            **final_set.counts,
        },
        "support": {
            "level": final_set.support_level,
            "count": len(final_set.hits),
            "thin_below": config["abstention.thin_below"],
            "floor": final_set.counts["floor"],
            "dropped": final_set.counts["floor_dropped"],
            "best_under_floor": final_set.counts["best_under_floor"],
            "question": {"kind": "law", "decided_by": "default"},
        },
    }


def reply_body(
    generation: _Generation, fusion: FusionResult, final_set: FinalSet, *, config: RetrievalConfig,
    legs: Mapping[str, LegRun], objects: Sequence[TurnObject], object_leg: ObjectLegResult,
    stated_scope: StatedScope | None, retrieval_config_hash: str, timings: TurnTimings,
) -> dict[str, object]:
    """Build the JSON-ready reply for a turn that reached retrieval."""

    return {
        "stage": "retrieve",
        "generation": {
            "gen_id": generation.gen_id,
            "collection": generation.collection,
            "lockfiles": list(generation.lockfiles),
            "embedding_space": generation.embedding_space,
        },
        "pool": [entry.to_wire() for entry in fusion.entries],
        "final_set": [hit.to_wire() for hit in final_set.hits],
        "support_level": final_set.support_level,
        "provenance": retrieval_provenance(
            config=config, collection=generation.collection, legs=legs, fusion=fusion,
            objects=objects, object_leg=object_leg, stated_scope=stated_scope, final_set=final_set,
        ),
        "retrieval_config_hash": retrieval_config_hash,
        "timings_ms": timings.to_milliseconds(),
    }
