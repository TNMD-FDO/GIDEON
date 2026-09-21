"""The judgments@1 metric definition from spec §18.2, criteria 1 and 2."""

import itertools
import math
import unittest
from typing import cast

from gideon.evaluation.rankmetrics import (
    DEFINITION_ID,
    HOLE_DEPTH,
    NDCG_DEPTH,
    OVERLAP_THRESHOLD,
    RANKING_FLOOR,
    RECALL_DEPTH,
    RELEVANT_GRADE,
    Coordinates,
    CreditedRank,
    crediting,
    hole_at_10,
    meets,
    ndcg_at_10,
    recall_at_50,
)

HASH = "a" * 64
OTHER_HASH = "b" * 64


def coordinate(start: int, end: int, source: str = "source-a", digest: str = HASH) -> Coordinates:
    return source, digest, start, end


def graded(start: int, end: int, grade: int, source: str = "source-a", digest: str = HASH) -> tuple[Coordinates, int]:
    return coordinate(start, end, source, digest), grade


class Definition(unittest.TestCase):
    """The release constants are the judgments@1 protocol."""

    def test_definition_and_protocol_constants(self) -> None:
        self.assertEqual(DEFINITION_ID, "judgments@1")
        self.assertEqual(NDCG_DEPTH, 10)
        self.assertEqual(HOLE_DEPTH, 10)
        self.assertEqual(RECALL_DEPTH, 50)
        self.assertEqual(RELEVANT_GRADE, 2)
        self.assertEqual(OVERLAP_THRESHOLD, 0.5)
        self.assertEqual(RANKING_FLOOR, 25)


class Figures(unittest.TestCase):
    """The three reported figures and their undefined populations."""

    def test_perfect_and_reversed_rankings(self) -> None:
        passages = (graded(0, 4, 3), graded(10, 14, 2), graded(20, 24, 1))
        perfect = tuple(item[0] for item in passages)
        reversed_ranking = tuple(reversed(perfect))

        self.assertEqual(ndcg_at_10(perfect, passages), 1.0)
        self.assertEqual(recall_at_50(perfect, passages), 1.0)
        self.assertEqual(hole_at_10(perfect, passages), 0.0)

        # Reversed DCG = 1/log2(2) + 2/log2(3) + 3/log2(4) = 1 + 1.26186 + 1.5.
        # Ideal DCG = 3/log2(2) + 2/log2(3) + 1/log2(4) = 3 + 1.26186 + 0.5.
        reversed_expected = (1 + 2 / math.log2(3) + 3 / 2) / (
            3 + 2 / math.log2(3) + 1 / 2
        )
        self.assertAlmostEqual(
            cast(float, ndcg_at_10(reversed_ranking, passages)), reversed_expected
        )
        self.assertEqual(recall_at_50(reversed_ranking, passages), 1.0)
        self.assertEqual(hole_at_10(reversed_ranking, passages), 0.0)

    def test_unjudged_only_list_and_undefined_populations(self) -> None:
        passages = (graded(0, 4, 3), graded(10, 14, 2))
        unjudged = (coordinate(20, 24), coordinate(30, 34))
        self.assertEqual(ndcg_at_10(unjudged, passages), 0.0)
        self.assertEqual(hole_at_10(unjudged, passages), 1.0)
        self.assertIsNone(hole_at_10(unjudged, ()))

        no_relevant = (graded(0, 4, 1),)
        self.assertIsNone(recall_at_50((coordinate(0, 4),), no_relevant))

        all_zero = (graded(0, 4, 0),)
        zero_list = (coordinate(0, 4), coordinate(10, 14))
        self.assertIsNone(ndcg_at_10(zero_list, all_zero))
        self.assertIsNone(recall_at_50(zero_list, all_zero))
        # One of two chunks meets the grade-0 passage, so Hole@10 = 1/2.
        self.assertEqual(hole_at_10(zero_list, all_zero), 0.5)

    def test_ruled_examples(self) -> None:
        a = graded(0, 4, 3)
        b = graded(10, 14, 2)
        c = graded(20, 24, 0)
        x = coordinate(30, 34)
        y = coordinate(40, 44)
        ranked = (x, a[0], y, b[0])

        # DCG = 0 + 3/log2(3) + 0 + 2/log2(5).
        # IDCG = 3/log2(2) + 2/log2(3) + 0/log2(4), giving about 0.646.
        self.assertAlmostEqual(cast(float, ndcg_at_10(ranked, (a, b, c))), 0.646, places=3)
        self.assertEqual(hole_at_10(ranked, (a, b, c)), 0.5)

        p = graded(0, 10, 3)
        r = graded(20, 30, 2)
        p1 = coordinate(0, 5)
        p2 = coordinate(5, 10)
        credited = crediting((p1, p2, r[0]), (p, r))
        self.assertEqual(credited, (CreditedRank(True, 3), CreditedRank(True, 0), CreditedRank(True, 2)))
        # Credited DCG = 3/log2(2) + 0/log2(3) + 2/log2(4).
        # Ideal DCG = 3/log2(2) + 2/log2(3), giving about 0.939.
        self.assertAlmostEqual(
            cast(float, ndcg_at_10((p1, p2, r[0]), (p, r))), 0.939, places=3
        )
        # Paying P twice would instead be (3 + 3/log2(3) + 2/log2(4)) /
        # (3 + 2/log2(3)) = 1.383, which the crediting walk prevents.
        uncredited = (3 + 3 / math.log2(3) + 2 / math.log2(4)) / (
            3 + 2 / math.log2(3)
        )
        self.assertAlmostEqual(uncredited, 1.383, places=3)

    def test_recall_counts_a_met_passage_once(self) -> None:
        passage = graded(0, 10, 2)
        ranked = (coordinate(0, 5), coordinate(5, 10))
        self.assertEqual(recall_at_50(ranked, (passage,)), 1.0)

    def test_depth_edges_and_empty_list(self) -> None:
        passages = (graded(0, 4, 3), graded(10, 14, 2))
        short = (coordinate(0, 4), coordinate(20, 24))
        self.assertEqual(hole_at_10(short, passages), 0.5)

        target = graded(200, 210, 3)
        long = tuple(coordinate(index * 2, index * 2 + 1) for index in range(51))
        at_fifty = long[:49] + (target[0],) + (long[49], long[50])
        after_fifty = long[:50] + (target[0],)
        self.assertEqual(recall_at_50(at_fifty, (target,)), 1.0)
        self.assertEqual(recall_at_50(after_fifty, (target,)), 0.0)
        self.assertEqual(hole_at_10(long, (target,)), 1.0)

        self.assertEqual(ndcg_at_10((), passages), 0.0)
        self.assertEqual(recall_at_50((), passages), 0.0)
        self.assertIsNone(hole_at_10((), passages))

    def test_ndcg_never_exceeds_one_in_small_exhaustive_sweep(self) -> None:
        passages = (graded(0, 2, 3), graded(4, 6, 2), graded(8, 10, 1))
        unjudged = coordinate(12, 14)
        ranked_items = tuple(item[0] for item in passages) + (unjudged,)
        for ranking in itertools.permutations(ranked_items):
            with self.subTest(ranking=ranking):
                value = ndcg_at_10(ranking, passages)
                self.assertIsNotNone(value)
                self.assertLessEqual(cast(float, value), 1.0)

    def test_missing_canonical_hash_is_unmet_but_still_scored(self) -> None:
        passage = graded(0, 10, 3)
        ranked = (coordinate(0, 10, digest=OTHER_HASH),)
        self.assertEqual(crediting(ranked, (passage,)), (CreditedRank(False, 0),))
        # The hash mismatch yields DCG 0 / IDCG 3, recall 0, and one hole.
        self.assertEqual(ndcg_at_10(ranked, (passage,)), 0.0)
        self.assertEqual(recall_at_50(ranked, (passage,)), 0.0)
        self.assertEqual(hole_at_10(ranked, (passage,)), 1.0)


class OverlapAndCrediting(unittest.TestCase):
    """The half-open overlap and deterministic crediting rules."""

    def test_overlap_edges(self) -> None:
        judged = coordinate(0, 10)
        cases = (
            ("inside", coordinate(2, 8), True),
            ("containing", coordinate(0, 20), True),
            ("straddling at threshold", coordinate(8, 12), True),
            ("just under threshold", coordinate(9, 12), False),
            ("just over threshold", coordinate(7, 12), True),
            ("one-code-point overlap", coordinate(9, 11), True),
            ("adjacent", coordinate(10, 12), False),
            ("other source", coordinate(0, 10, source="source-b"), False),
            ("other hash", coordinate(0, 10, digest=OTHER_HASH), False),
        )
        for name, chunk, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(meets(chunk, judged), expected)
        self.assertEqual(OVERLAP_THRESHOLD, 0.5)

    def test_crediting_ties_and_repeated_meeting(self) -> None:
        higher_grade = graded(0, 10, 3)
        lower_grade = graded(0, 4, 2)
        grade_walk = crediting(
            (coordinate(0, 6), coordinate(5, 10)),
            (higher_grade, lower_grade),
        )
        self.assertEqual(grade_walk, (CreditedRank(True, 3), CreditedRank(True, 0)))

        larger_overlap = graded(0, 10, 2)
        smaller_overlap = graded(0, 4, 2)
        second_chunk = coordinate(5, 10)
        first_walk = crediting(
            (coordinate(0, 6), second_chunk),
            (larger_overlap, smaller_overlap),
        )
        self.assertEqual(first_walk, (CreditedRank(True, 2), CreditedRank(True, 0)))

        lower_coordinate = graded(0, 4, 2)
        higher_coordinate = graded(6, 10, 2)
        second_walk = crediting(
            (coordinate(0, 10), lower_coordinate[0]),
            (lower_coordinate, higher_coordinate),
        )
        self.assertEqual(second_walk, (CreditedRank(True, 2), CreditedRank(True, 0)))

        unjudged = crediting((coordinate(20, 24),), (lower_coordinate,))
        self.assertEqual(unjudged, (CreditedRank(False, 0),))
