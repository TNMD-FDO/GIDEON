"""The judgments@2 drop rule and its figures over fictitious coordinates."""

import ast
import math
import sys
import unittest
from pathlib import Path
from typing import cast

from test_evaluation_rankmetrics import OTHER_HASH, coordinate, graded

from gideon.evaluation import rankmetrics, rankmetrics2
from gideon.evaluation.rankmetrics2 import (
    DEFINITION_ID,
    DROP_KINDS,
    DroppedPassage,
    foreign_entries,
    held_passages,
    hole_at_10,
    ndcg_at_10,
    recall_at_50,
)


class Definition(unittest.TestCase):
    def test_definition_and_drop_kinds(self) -> None:
        self.assertEqual(DEFINITION_ID, "judgments@2")
        self.assertEqual(DROP_KINDS, ("absent", "changed"))

    def test_imports_are_standard_library_or_rankmetrics(self) -> None:
        path = Path(rankmetrics2.__file__)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                names = (node.module or "",)
            else:
                continue
            for name in names:
                self.assertTrue(
                    name == rankmetrics.__name__ or name.split(".")[0] in sys.stdlib_module_names,
                    name,
                )


class ManifestRules(unittest.TestCase):
    def test_holds_exact_hash_and_drops_each_passage_in_order(self) -> None:
        held_later_offset = graded(30, 40, 2)
        held_earlier_offset = graded(0, 10, 1)
        changed_later_offset = graded(30, 40, 3, digest=OTHER_HASH)
        changed_earlier_offset = graded(0, 10, 2, digest=OTHER_HASH)
        absent = graded(0, 10, 3, source="fictional/absent")
        passages = (
            changed_later_offset, held_later_offset, absent, held_earlier_offset, changed_earlier_offset,
        )
        source_id, sha256, _, _ = held_later_offset[0]

        held, dropped = held_passages(iter(passages), {source_id: sha256})

        self.assertEqual(held, (held_later_offset, held_earlier_offset))
        self.assertEqual(dropped, (
            DroppedPassage(changed_later_offset, "changed"),
            DroppedPassage(absent, "absent"),
            DroppedPassage(changed_earlier_offset, "changed"),
        ))

    def test_foreign_entries_at_first_rank_and_beyond_recall_depth(self) -> None:
        clean = tuple(coordinate(i, i + 1) for i in range(rankmetrics.RECALL_DEPTH))
        source_id, sha256, _, _ = clean[0]
        manifest = {source_id: sha256}
        absent = coordinate(0, 1, source="fictional/absent")
        changed = coordinate(0, 1, digest=OTHER_HASH)
        ranked = (absent, *clean, changed)

        self.assertEqual(foreign_entries(iter(ranked), manifest), (absent, changed))
        self.assertEqual(foreign_entries(clean, manifest), ())


class Figures(unittest.TestCase):
    def test_figures_over_held_passages(self) -> None:
        top = graded(0, 10, 3, source="fictional/absent")
        relevant = graded(10, 20, 2)
        lower = graded(20, 30, 1)
        source_id, sha256, _, _ = relevant[0]
        ranked = (coordinate(30, 40), relevant[0])
        held, _ = held_passages((top, relevant, lower), {source_id: sha256})

        # The grade-3 passage leaves the ideal and recall's denominator.
        # Rank 1 is unjudged and rank 2 gains 2. The held ideal has grades 2 and 1.
        discount = math.log2(3)
        expected_ndcg = (2 / discount) / (2 + 1 / discount)
        self.assertAlmostEqual(cast(float, ndcg_at_10(ranked, held)), expected_ndcg)
        self.assertEqual(recall_at_50(ranked, held), 1.0)
        self.assertEqual(hole_at_10(ranked, held), 0.5)

    def test_missing_canonical_hash_drops_the_passage_and_leaves_figures_undefined(self) -> None:
        passage = graded(0, 10, 3)
        ranked = (coordinate(0, 10, digest=OTHER_HASH),)
        source_id, sha256, _, _ = ranked[0]
        manifest = {source_id: sha256}

        held, dropped = held_passages((passage,), manifest)

        self.assertEqual(dropped, (DroppedPassage(passage, "changed"),))
        self.assertEqual(held, ())
        self.assertEqual(foreign_entries(ranked, manifest), ())
        self.assertIsNone(ndcg_at_10(ranked, held))
        self.assertIsNone(recall_at_50(ranked, held))
        self.assertIsNone(hole_at_10(ranked, held))

    def test_no_drops_matches_judgments1(self) -> None:
        passages = (graded(0, 4, 3), graded(10, 14, 2), graded(20, 24, 1))
        ranked = (passages[2][0], coordinate(30, 34), passages[0][0])
        source_id, sha256, _, _ = ranked[0]

        held, dropped = held_passages(passages, {source_id: sha256})

        self.assertEqual(held, passages)
        self.assertEqual(dropped, ())
        self.assertEqual(ndcg_at_10(ranked, held), rankmetrics.ndcg_at_10(ranked, passages))
        self.assertEqual(recall_at_50(ranked, held), rankmetrics.recall_at_50(ranked, passages))
        self.assertEqual(hole_at_10(ranked, held), rankmetrics.hole_at_10(ranked, passages))
