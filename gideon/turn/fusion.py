"""Fuse ranked retrieval legs, hold reserved seats, and collapse and cut the pool."""

import builtins
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Literal


@dataclass(frozen=True, slots=True)
class LegHit:
    """One chunk's rank and metadata from a retrieval leg."""

    chunk_id: str
    rank: int
    dupe_cluster: str
    boilerplate: bool
    court: str
    precedential: str


@dataclass(frozen=True, slots=True)
class PinnedHit:
    """One hit in pin order with the index of the object that found it."""

    hit: LegHit
    object: int


@dataclass(frozen=True, slots=True)
class PoolEntry:
    """One chunk in the fused pool and the leg ranks that contributed to it."""

    chunk_id: str
    pool_rank: int
    score: float
    leg_ranks: tuple[tuple[str, int], ...]
    dupe_cluster: str
    boilerplate: bool
    authority_tier: str
    rank: int = 0
    pinned: bool = False
    object: int | None = None
    controlling_rank: int | None = None
    stage2_score: float | None = None

    @property
    def reserved(self) -> Literal["controlling"] | None:
        return "controlling" if self.controlling_rank is not None else None

    def to_wire(self) -> dict[str, builtins.object]:
        return {
            "chunk_id": self.chunk_id,
            "pool_rank": self.pool_rank,
            "rank": self.rank,
            "score": self.score,
            "stage2_score": self.stage2_score,
            "leg_ranks": dict(self.leg_ranks),
            "dupe_cluster": self.dupe_cluster,
            "boilerplate": self.boilerplate,
            "authority_tier": self.authority_tier,
            "pinned": self.pinned,
            "object": self.object,
            "reserved": self.reserved,
            "controlling_rank": self.controlling_rank,
        }


@dataclass(frozen=True, slots=True)
class FusionResult:
    """The final pool and the counts at each fusion step."""

    entries: tuple[PoolEntry, ...]
    counts: Mapping[str, int]


def _cut_priority(entry: PoolEntry) -> tuple[int, int]:
    """Choose what reaches the cap, preserving input order within ties."""

    if entry.pinned:
        return (0, 0)
    if entry.controlling_rank is not None:
        return (1, entry.controlling_rank)
    return (2, 0)


def fuse(
    legs: Mapping[str, Sequence[LegHit]], *, tier_for: Callable[[LegHit], str], rrf_k: int, boilerplate_weight: float,
    collapse: bool, pool_cap: int, pins: Sequence[PinnedHit] = (), reservation: int = 0,
    controlling_hits: Sequence[LegHit] = (), controlling_reservation: int = 0,
) -> FusionResult:
    """Rank voting hits, then seat pins and controlling hits before collapse and cut."""

    chunks: dict[str, dict[str, LegHit]] = {}
    for leg, hits in legs.items():
        for hit in hits:
            by_leg = chunks.setdefault(hit.chunk_id, {})
            previous = by_leg.get(leg)
            if previous is None or hit.rank < previous.rank:
                by_leg[leg] = hit

    semantic: dict[str, PoolEntry] = {}
    weighted = 0
    for chunk_id, by_leg in chunks.items():
        leg_ranks = tuple((leg, hit.rank) for leg, hit in sorted(by_leg.items()))
        score = math.fsum(1 / (rrf_k + rank) for _, rank in leg_ranks)
        boilerplate = any(hit.boilerplate for hit in by_leg.values())
        if boilerplate:
            score *= boilerplate_weight
            weighted += 1
        # Use the best-ranked observation for the cluster and authority tier.
        _, best = min(by_leg.items(), key=lambda item: (item[1].rank, item[0]))
        semantic[chunk_id] = PoolEntry(chunk_id, 0, score, leg_ranks, best.dupe_cluster, boilerplate, tier_for(best))

    pinned: dict[str, PoolEntry] = {}
    object_seats: Counter[int] = Counter()
    for pin in pins:
        hit = pin.hit
        # A chunk already seated, by this object or an earlier one, takes no further seat.
        if hit.chunk_id in pinned or object_seats[pin.object] >= reservation:
            continue
        object_seats[pin.object] += 1
        entry = semantic.pop(hit.chunk_id, None)
        if entry is None:
            entry = PoolEntry(hit.chunk_id, 0, 0.0, (), hit.dupe_cluster, hit.boilerplate, tier_for(hit))
        pinned[hit.chunk_id] = replace(entry, pinned=True, object=pin.object)

    combined = dict(pinned)
    combined.update((entry.chunk_id, entry) for entry in sorted(
        semantic.values(), key=lambda entry: (-entry.score, min(rank for _, rank in entry.leg_ranks), entry.chunk_id),
    ))
    for hit in controlling_hits[:controlling_reservation]:
        entry = combined.get(hit.chunk_id)
        if entry is None:
            entry = PoolEntry(hit.chunk_id, 0, 0.0, (), hit.dupe_cluster, hit.boilerplate, tier_for(hit))
        elif entry.controlling_rank is not None:
            continue
        combined[hit.chunk_id] = replace(entry, controlling_rank=hit.rank)

    union = len(combined)
    ranked = list(combined.values())
    promoted = inherited = 0
    if collapse:
        clusters: dict[str, PoolEntry] = {}
        for entry in ranked:
            survivor = clusters.get(entry.dupe_cluster)
            if survivor is None:
                clusters[entry.dupe_cluster] = entry
            elif entry.controlling_rank is not None and not survivor.pinned and survivor.controlling_rank is None:
                if survivor.authority_tier == "persuasive":
                    clusters[entry.dupe_cluster] = entry
                    promoted += 1
                else:
                    clusters[entry.dupe_cluster] = replace(survivor, controlling_rank=entry.controlling_rank)
                    inherited += 1
        ranked = list(clusters.values())

    # Pick by seat priority, then emit in the combined order.
    kept_ids = {entry.chunk_id for entry in sorted(ranked, key=_cut_priority)[:pool_cap]}
    kept = (entry for entry in ranked if entry.chunk_id in kept_ids)
    entries = tuple(replace(entry, pool_rank=rank, rank=rank) for rank, entry in enumerate(kept, start=1))
    cut = [entry for entry in ranked if entry.chunk_id not in kept_ids]
    pinned_seated = sum(entry.pinned for entry in entries)
    reserved = [entry for entry in entries if entry.controlling_rank is not None]
    counts = MappingProxyType({
        "union": union,
        "after_collapse": len(ranked),
        "weighted": weighted,
        "cut": len(cut),
        "pinned": pinned_seated,
        "pinned_cut": sum(entry.pinned for entry in cut),
        "reserved": len(reserved),
        "reserved_only": sum(not entry.leg_ranks for entry in reserved),
        "reserved_promoted": promoted,
        "reserved_inherited": inherited,
        "reserved_cut": sum(entry.controlling_rank is not None for entry in cut),
    })
    return FusionResult(entries, counts)
