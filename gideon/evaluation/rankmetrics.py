"""The ``judgments@1`` metric definition over ranked gold-evidence coordinates.

This docstring is the definition's one home (§18.2, [21] item 13). A rule or
threshold that changes takes the next ``@N`` rather than an edit here, as a
grammar pattern and a judge prompt do, so ticket 16's pairing can refuse to
pair two definitions.

**Meets.** A ranked chunk meets a judged passage iff both carry the same
source id and the same canonical-text SHA-256, and their half-open code-point
ranges overlap by at least ``OVERLAP_THRESHOLD`` of the *shorter* of the two
ranges. A chunk inside a judged passage and a judged passage inside a larger
chunk therefore both meet at 1.0, so a re-chunked index is measurable against
grades given under another chunking; equal offsets in another source never
meet. The threshold is a starting value (ADR-0017).

**Crediting.** The list is walked from rank 1 and a judged passage is credited
at most once. A chunk's gain is the highest grade among the not-yet-credited
passages it meets, and that passage becomes credited; ties break by the larger
overlap, then the lower coordinates, so the walk is deterministic. A chunk
meeting only credited passages keeps its rank, counts as judged, and gains 0;
one meeting no judged passage is unjudged and gains 0. Every gain is thus a
distinct passage's grade, so nDCG cannot exceed 1 and a small-chunk arm cannot
be paid twice for one passage.

**The three figures**, each a judged query's — a query with one or more primary
grades — and each ``None`` where it is undefined, so no caller ever reads a
figure the grading cannot support:

- ``ndcg_at_10`` — gain is the credited grade itself, the discount is
  ``log2(rank + 1)``, and the ideal ranking is the query's primary grades in
  descending order, first ten (trec_eval's and BEIR's definition, the one [21]
  item 13 cites: an unjudged passage holds its rank and gains 0). Undefined
  when the ideal is 0.
- ``recall_at_50`` — the relevant passages (grade at or above
  ``RELEVANT_GRADE``) met by at least one chunk among ranks 1 to
  ``RECALL_DEPTH``, over the query's relevant passages; meeting alone counts
  and crediting does not enter. Undefined with no relevant passage.
- ``hole_at_10`` — the unjudged chunks among ranks 1 to ``HOLE_DEPTH`` over the
  chunks present there, qualifying nDCG@10's pessimism. Undefined for an empty
  list, and for a query with no grades yet, whose trivially whole hole would
  let the state of the grading rather than the retriever move the number.

Below ``RANKING_FLOOR`` judged queries the set detects regressions and ranks
nothing, which the run's summary says when it applies.

**Two known limits, stated and not solved.** A chunk that swallows two graded
passages earns the higher grade alone, so a large-chunk arm cannot reach 1.0 on
such a query. And a judged passage whose canonical text the index under test
does not hold reads as not retrieved, since a ranked list states nothing about
what that index holds; the drop rule of §18.6 arrives with the input that can
prove absence, as this definition's next ``@N``.

The module is pure over integers, strings, and grades: no file, no clock, no
host, and no text. Nothing here rounds.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

DEFINITION_ID: Final[str] = "judgments@1"
NDCG_DEPTH: Final[int] = 10
RECALL_DEPTH: Final[int] = 50
HOLE_DEPTH: Final[int] = 10
RELEVANT_GRADE: Final[int] = 2
OVERLAP_THRESHOLD: Final[float] = 0.5
RANKING_FLOOR: Final[int] = 25

type Coordinates = tuple[str, str, int, int]
type GradedPassage = tuple[Coordinates, int]


@dataclass(frozen=True, slots=True)
class CreditedRank:
    """Whether one ranked chunk meets a judgment and the grade it gains."""

    judged: bool
    gain: int


def _overlap(left: Coordinates, right: Coordinates) -> int:
    """Return the number of shared code points in two half-open ranges."""

    return max(0, min(left[3], right[3]) - max(left[2], right[2]))


def meets(chunk: Coordinates, judged: Coordinates) -> bool:
    """Return whether a ranked chunk meets one judged passage."""

    if chunk[0] != judged[0] or chunk[1] != judged[1]:
        return False
    shorter = min(chunk[3] - chunk[2], judged[3] - judged[2])
    if shorter <= 0:
        return False
    return _overlap(chunk, judged) / shorter >= OVERLAP_THRESHOLD


def _best_candidate(
    chunk: Coordinates,
    passages: tuple[GradedPassage, ...],
    credited: set[Coordinates],
) -> GradedPassage | None:
    """Choose the deterministic uncredited passage for one ranked chunk."""

    candidates = tuple(
        passage
        for passage in passages
        if passage[0] not in credited and meets(chunk, passage[0])
    )
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda passage: (-passage[1], -_overlap(chunk, passage[0]), passage[0]),
    )


def crediting(
    ranked: Iterable[Coordinates], graded: Iterable[GradedPassage]
) -> tuple[CreditedRank, ...]:
    """Walk *ranked* and return its judged flag and credited gain per rank.

    A chunk is judged when it meets any primary passage, including passages
    already credited by an earlier rank.  Only an uncredited passage can
    supply a positive gain, and the selected passage is chosen by grade,
    overlap, and coordinates in that order.
    """

    passages = tuple(graded)
    credited: set[Coordinates] = set()
    results: list[CreditedRank] = []
    for chunk in ranked:
        judged = any(meets(chunk, coordinate) for coordinate, _grade in passages)
        selected = _best_candidate(chunk, passages, credited)
        if selected is None:
            results.append(CreditedRank(judged, 0))
            continue
        coordinate, grade = selected
        credited.add(coordinate)
        results.append(CreditedRank(judged, grade))
    return tuple(results)


def ndcg_at_10(
    ranked: Iterable[Coordinates], graded: Iterable[GradedPassage]
) -> float | None:
    """Return nDCG@10, or ``None`` when the ideal gain is zero."""

    ranked_values = tuple(ranked)[:NDCG_DEPTH]
    graded_values = tuple(graded)
    ideal_grades = sorted((grade for _coordinate, grade in graded_values), reverse=True)[
        :NDCG_DEPTH
    ]
    ideal = sum(
        grade / math.log2(rank + 2) for rank, grade in enumerate(ideal_grades)
    )
    if ideal == 0:
        return None
    actual = sum(
        credit.gain / math.log2(rank + 2)
        for rank, credit in enumerate(crediting(ranked_values, graded_values))
    )
    return actual / ideal


def recall_at_50(
    ranked: Iterable[Coordinates], graded: Iterable[GradedPassage]
) -> float | None:
    """Return recall@50, or ``None`` when no primary passage is relevant."""

    ranked_values = tuple(ranked)[:RECALL_DEPTH]
    relevant = tuple(
        coordinate for coordinate, grade in graded if grade >= RELEVANT_GRADE
    )
    if not relevant:
        return None
    met = sum(
        any(meets(chunk, coordinate) for chunk in ranked_values)
        for coordinate in relevant
    )
    return met / len(relevant)


def hole_at_10(
    ranked: Iterable[Coordinates], graded: Iterable[GradedPassage]
) -> float | None:
    """Return the top-ten share of unjudged chunks, or ``None`` where undefined.

    An empty list has no hole to report, and a query with no grades yet has no
    yardstick: counting its trivially whole hole would let the state of the
    grading, not the retriever, move the mean.
    """

    ranked_values = tuple(ranked)[:HOLE_DEPTH]
    graded_values = tuple(graded)
    if not ranked_values or not graded_values:
        return None
    judged_count = sum(
        any(meets(chunk, coordinate) for coordinate, _grade in graded_values)
        for chunk in ranked_values
    )
    return (len(ranked_values) - judged_count) / len(ranked_values)
