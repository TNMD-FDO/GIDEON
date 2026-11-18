"""The ``judgments@2`` definition over ranked gold-evidence coordinates.

This docstring is the definition's one home. It is ``judgments@1`` with the
generation's manifest as a second input: the meets rule, the crediting walk,
the depths, the relevant grade, the overlap threshold, and the ranking floor
are ``rankmetrics``'s, imported and never restated, so the two versions cannot
drift on what they share. A rule that changes takes the next ``@N``.

A generation's manifest maps each source id to the one canonical-text SHA-256
recorded for it in ``generation_documents``. A judged passage is held when its
source id and hash match that mapping. Otherwise it is dropped and reported as
absent if the id has no manifest row or changed if the id has a different hash.

nDCG@10, recall@50, and Hole@10 use the ``judgments@1`` functions over a query's
held passages. The ideal ranking and recall's relevant-passage denominator
therefore count only held passages. When every passage is held, every figure
equals its ``judgments@1`` value. A query with primary grades and no held passage
is a dropped query. All three figures are undefined, and the query is left out
of the means. The ranking floor counts queries with at least one held passage.

A ranked entry is foreign when its source id is absent from the manifest or
its hash differs from the manifest's hash for that id. The entire ranked list
is checked. A foreign entry at any rank fails the run as an input error and
leaves that query unscored. A file drawn from another generation holding the
same documents under the same hashes passes this check, so the absence of
foreign entries cannot establish that the file came from the chosen generation.

``judgments@1`` read a judged passage the index does not hold as not
retrieved; this version removes that limit. The other remains: a chunk that
swallows two graded passages still earns only the higher grade.

The module is pure over integers, strings, and grades: no file, no clock, no
host, and no text. Nothing here rounds.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final, Literal

from gideon.evaluation.rankmetrics import (
    Coordinates,
    GradedPassage,
    hole_at_10,
    ndcg_at_10,
    recall_at_50,
)

__all__ = [
    "DEFINITION_ID",
    "DROP_KINDS",
    "DropKind",
    "DroppedPassage",
    "Manifest",
    "foreign_entries",
    "held_passages",
    "hole_at_10",
    "ndcg_at_10",
    "recall_at_50",
]

DEFINITION_ID: Final[str] = "judgments@2"

type Manifest = Mapping[str, str]
type DropKind = Literal["absent", "changed"]

DROP_KINDS: Final[tuple[DropKind, ...]] = ("absent", "changed")


@dataclass(frozen=True, slots=True)
class DroppedPassage:
    """A graded passage and the reason the generation does not hold it."""

    passage: GradedPassage
    kind: DropKind


def held_passages(
    graded: Iterable[GradedPassage], manifest: Manifest,
) -> tuple[tuple[GradedPassage, ...], tuple[DroppedPassage, ...]]:
    """Split graded passages into held and dropped tuples in input order."""

    held: list[GradedPassage] = []
    dropped: list[DroppedPassage] = []
    for passage in graded:
        source_id, sha256, _, _ = passage[0]
        manifest_hash = manifest.get(source_id)
        if manifest_hash == sha256:
            held.append(passage)
        else:
            kind: DropKind = "absent" if manifest_hash is None else "changed"
            dropped.append(DroppedPassage(passage, kind))
    return tuple(held), tuple(dropped)


def foreign_entries(ranked: Iterable[Coordinates], manifest: Manifest) -> tuple[Coordinates, ...]:
    """Return every foreign coordinate in rank order."""

    return tuple(entry for entry in ranked if manifest.get(entry[0]) != entry[1])
