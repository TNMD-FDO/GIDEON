"""Fuse ranked retrieval legs, seat exact-object pins, and collapse the pool."""

import builtins
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class LegHit:
    """One chunk's rank and metadata from a retrieval leg."""

    chunk_id: str
    rank: int
    dupe_cluster: str
    boilerplate: bool


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
    pinned: bool = False
    object: int | None = None

    def to_wire(self) -> dict[str, builtins.object]:
        return {
            "chunk_id": self.chunk_id,
            "pool_rank": self.pool_rank,
            "score": self.score,
            "leg_ranks": dict(self.leg_ranks),
            "dupe_cluster": self.dupe_cluster,
            "boilerplate": self.boilerplate,
            "pinned": self.pinned,
            "object": self.object,
        }


@dataclass(frozen=True, slots=True)
class FusionResult:
    """The final pool and the counts at each fusion step."""

    entries: tuple[PoolEntry, ...]
    counts: Mapping[str, int]


def fuse(
    legs: Mapping[str, Sequence[LegHit]], *, rrf_k: int, boilerplate_weight: float,
    collapse: bool, pool_cap: int, pins: Sequence[PinnedHit] = (), reservation: int = 0,
) -> FusionResult:
    """Rank semantic hits by reciprocal rank and place reserved hits first."""

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
        # Use the best-ranked observation's cluster if the stores disagree.
        _, best = min(by_leg.items(), key=lambda item: (item[1].rank, item[0]))
        semantic[chunk_id] = PoolEntry(chunk_id, 0, score, leg_ranks, best.dupe_cluster, boilerplate)

    pinned: dict[str, PoolEntry] = {}
    reserved: Counter[int] = Counter()
    for pin in pins:
        hit = pin.hit
        # A chunk already seated, by this object or an earlier one, takes no further seat.
        if hit.chunk_id in pinned or reserved[pin.object] >= reservation:
            continue
        reserved[pin.object] += 1
        entry = semantic.pop(hit.chunk_id, None)
        if entry is None:
            entry = PoolEntry(hit.chunk_id, 0, 0.0, (), hit.dupe_cluster, hit.boilerplate)
        pinned[hit.chunk_id] = replace(entry, pinned=True, object=pin.object)

    ranked = [*pinned.values(), *sorted(
        semantic.values(), key=lambda entry: (-entry.score, min(rank for _, rank in entry.leg_ranks), entry.chunk_id),
    )]
    union = len(ranked)
    if collapse:
        clusters: dict[str, PoolEntry] = {}
        for entry in ranked:
            clusters.setdefault(entry.dupe_cluster, entry)
        ranked = list(clusters.values())

    entries = tuple(replace(entry, pool_rank=rank) for rank, entry in enumerate(ranked[:pool_cap], start=1))
    pinned_seated = sum(entry.pinned for entry in entries)
    counts = MappingProxyType({
        "union": union,
        "after_collapse": len(ranked),
        "weighted": weighted,
        "cut": len(ranked) - len(entries),
        "pinned": pinned_seated,
        "pinned_cut": sum(entry.pinned for entry in ranked[pool_cap:]),
    })
    return FusionResult(entries, counts)
