"""Select and order the passages that make up a research turn's evidence."""

from __future__ import annotations

import builtins
from collections import Counter
from collections.abc import Mapping, Sequence, Set
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from gideon.turn.tier import AUTHORITY_TIERS, CONTROLLING_TIERS

if TYPE_CHECKING:
    from gideon.turn.config import RetrievalConfig
    from gideon.turn.fusion import PoolEntry


class _PassageMetadata(Protocol):
    @property
    def doc_id(self) -> str: ...

    @property
    def token_count(self) -> int: ...


@dataclass(frozen=True, slots=True)
class FinalHit:
    """A numbered chunk in the final set, with its pool and passage metadata."""

    number: int
    chunk_id: str
    pool_rank: int
    rank: int
    stage2_score: float | None
    authority_tier: str
    seat: Literal["pin", "controlling", "score"]
    object: int | None
    doc_id: str
    tokens: int

    def to_wire(self) -> dict[str, builtins.object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FinalSet:
    """Ordered final hits and the counts of their selection."""

    hits: tuple[FinalHit, ...]
    counts: dict[str, object]


def choose(
    entries: Sequence[PoolEntry], passages: Mapping[str, _PassageMetadata], config: RetrievalConfig,
    held_chunk_ids: Mapping[int, Set[str]], *, stage_scored: bool,
) -> FinalSet:
    """Select from the candidates whose passages were read.

    Held chunk ids are grouped by the reporter cite that returned them.
    """

    def raw_score(entry: PoolEntry) -> float:
        score = entry.stage2_score
        assert score is not None
        return score

    candidates = sorted(
        (entry for entry in entries if entry.chunk_id in passages), key=lambda entry: (entry.pool_rank, entry.chunk_id),
    )
    exempt_documents: set[str] = set()
    for held_ids in held_chunk_ids.values():
        held_documents = Counter(passages[entry.chunk_id].doc_id for entry in candidates if entry.chunk_id in held_ids)
        if held_documents:
            # Counter keeps the order of first encounter for equal counts.
            (document, _), = held_documents.most_common(1)
            exempt_documents.add(document)

    unpinned = [entry for entry in candidates if not entry.pinned]
    if stage_scored:
        unpinned.sort(key=raw_score, reverse=True)

    cap = config["per_document_cap"]
    document_counts: Counter[str] = Counter()
    capped: set[str] = set()

    def cap_admits(chunk_id: str, *, freeing: str | None = None) -> bool:
        document = passages[chunk_id].doc_id
        occupied = document_counts[document] - int(document == freeing)
        if cap and document not in exempt_documents and occupied >= cap:
            capped.add(chunk_id)
            return False
        return True

    chosen: set[str] = set()
    for entry in unpinned:
        if len(chosen) == config["final_k"]:
            break
        if cap_admits(entry.chunk_id):
            chosen.add(entry.chunk_id)
            document_counts[passages[entry.chunk_id].doc_id] += 1

    mode = config["hierarchy.mode"]
    seated: set[str] = set()
    if mode != "off":
        controlling = [entry for entry in unpinned if entry.authority_tier in CONTROLLING_TIERS]
        needed = config["final_reserved_controlling"] - sum(entry.chunk_id in chosen for entry in controlling)
        if needed > 0:
            replaceable = [
                entry for entry in reversed(unpinned)
                if entry.chunk_id in chosen and entry.authority_tier not in CONTROLLING_TIERS
            ]
            for victim in replaceable[:needed]:
                freed_document = passages[victim.chunk_id].doc_id
                replacement = next((
                    entry for entry in controlling
                    if entry.chunk_id not in chosen and cap_admits(entry.chunk_id, freeing=freed_document)
                ), None)
                if replacement is None:
                    break
                chosen.remove(victim.chunk_id)
                document_counts[freed_document] -= 1
                chosen.add(replacement.chunk_id)
                document_counts[passages[replacement.chunk_id].doc_id] += 1
                seated.add(replacement.chunk_id)

    weights = config["hierarchy.weights"]
    tier_ranks = {
        tier: rank for rank, tier in enumerate(sorted(AUTHORITY_TIERS, key=weights.__getitem__, reverse=True))
    } if stage_scored and mode == "lexicographic" else {}

    def final_priority(entry: PoolEntry) -> tuple[int, float, int, str]:
        score = raw_score(entry) if stage_scored else 0.0
        if stage_scored and mode == "weighted":
            score *= weights[entry.authority_tier]
        # The ranked pool's rank breaks a tie, so a zero weight keeps the stage's own order.
        return tier_ranks.get(entry.authority_tier, 0), -score, entry.rank, entry.chunk_id

    ordered = sorted((entry for entry in candidates if entry.pinned or entry.chunk_id in chosen), key=final_priority)
    budget = config["context_budget_tokens"]
    tokens = sum(passages[entry.chunk_id].token_count for entry in ordered)
    protected: dict[int, str] = {}
    dropped: set[str] = set()
    if tokens > budget:
        pins = [entry for entry in candidates if entry.pinned and entry.object is not None]
        if stage_scored:
            pins.sort(key=raw_score, reverse=True)
        for pin in pins:
            assert pin.object is not None
            best = protected.setdefault(pin.object, pin.chunk_id)
            held = held_chunk_ids.get(pin.object)
            if held is not None and pin.chunk_id in held and best not in held:
                protected[pin.object] = pin.chunk_id

        protected_ids = set(protected.values())
        # The stable partition keeps each group's bottom-to-top order.
        bottom_up = list(reversed(ordered))
        bottom_up.sort(key=lambda entry: entry.chunk_id in protected_ids)
        for entry in bottom_up:
            dropped.add(entry.chunk_id)
            tokens -= passages[entry.chunk_id].token_count
            if tokens <= budget:
                break

    survivors = (entry for entry in ordered if entry.chunk_id not in dropped)
    hits: list[FinalHit] = []
    for number, entry in enumerate(survivors, start=1):
        passage = passages[entry.chunk_id]
        hits.append(FinalHit(
            number=number, chunk_id=entry.chunk_id, pool_rank=entry.pool_rank, rank=entry.rank,
            stage2_score=entry.stage2_score if stage_scored else None, authority_tier=entry.authority_tier,
            seat="pin" if entry.pinned else "controlling" if entry.chunk_id in seated else "score", object=entry.object,
            doc_id=passage.doc_id, tokens=passage.token_count,
        ))
    return FinalSet(tuple(hits), {
        "candidates": len(candidates),
        "size": len(hits),
        "pinned": sum(hit.seat == "pin" for hit in hits),
        "controlling_seated": sum(hit.seat == "controlling" for hit in hits),
        "by_tier": dict(Counter(hit.authority_tier for hit in hits)),
        "tokens": tokens,
        "document_capped": len(capped - chosen),  # A replacement can recover an earlier cap refusal.
        "exempt_documents": len(exempt_documents),
        "budget_dropped": len(dropped),
        "objects_unseated": sorted(index for index, chunk_id in protected.items() if chunk_id in dropped),
    })
