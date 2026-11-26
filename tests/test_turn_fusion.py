"""Reciprocal rank fusion, cluster collapse, and pool order."""

import unittest

from gideon.turn.fusion import LegHit, PinnedHit, fuse


class Fusion(unittest.TestCase):
    def test_equal_voice_and_one_entry_for_a_chunk_returned_by_both_legs(self) -> None:
        shared = LegHit("fixture-shared", 2, "fixture-shared-cluster", False)
        result = fuse({
            "dense": [LegHit("fixture-dense", 1, "fixture-dense-cluster", False), shared],
            "lexical": [LegHit("fixture-lexical", 1, "fixture-lexical-cluster", False), shared],
        }, rrf_k=5, boilerplate_weight=1, collapse=False, pool_cap=4)
        self.assertEqual([entry.chunk_id for entry in result.entries], ["fixture-shared", "fixture-dense", "fixture-lexical"])
        self.assertEqual(dict(result.entries[0].leg_ranks), {"dense": 2, "lexical": 2})
        self.assertAlmostEqual(result.entries[0].score, 2 / 7)
        for entry in result.entries[1:]:
            self.assertAlmostEqual(entry.score, 1 / 6)
        self.assertEqual(result.counts["union"], 3)

    def test_collapse_keeps_the_higher_ranked_member_before_applying_the_cap(self) -> None:
        legs = {
            "dense": [
                LegHit("fixture-other", 1, "fixture-other-cluster", False),
                LegHit("fixture-a-lower", 2, "fixture-duplicate-cluster", False),
                LegHit("fixture-tail", 3, "fixture-tail-cluster", False),
            ],
            "lexical": [LegHit("fixture-z-higher", 1, "fixture-duplicate-cluster", False)],
        }
        result = fuse(legs, rrf_k=5, boilerplate_weight=1, collapse=True, pool_cap=2)
        self.assertEqual([entry.chunk_id for entry in result.entries], ["fixture-other", "fixture-z-higher"])
        self.assertEqual([entry.pool_rank for entry in result.entries], [1, 2])
        self.assertEqual(dict(result.entries[1].leg_ranks), {"lexical": 1})
        self.assertAlmostEqual(result.entries[1].score, 1 / 6)
        self.assertEqual(result.counts, {
            "union": 4, "after_collapse": 3, "weighted": 0, "cut": 1, "pinned": 0, "pinned_cut": 0,
        })

        expanded = fuse(legs, rrf_k=5, boilerplate_weight=1, collapse=False, pool_cap=4)
        self.assertEqual({entry.chunk_id for entry in expanded.entries if entry.dupe_cluster == "fixture-duplicate-cluster"},
                         {"fixture-a-lower", "fixture-z-higher"})
        self.assertEqual(expanded.counts["after_collapse"], expanded.counts["union"])

    def test_boilerplate_multiplier_and_equal_scores_sort_by_best_rank_then_chunk_id(self) -> None:
        legs = {
            "dense": [
                LegHit("fixture-z-boilerplate", 1, "fixture-z-cluster", True),
                LegHit("fixture-b", 3, "fixture-b-cluster", False),
            ],
            "lexical": [LegHit("fixture-a", 3, "fixture-a-cluster", False)],
        }
        result = fuse(legs, rrf_k=1, boilerplate_weight=0.5, collapse=False, pool_cap=3)
        self.assertEqual([entry.score for entry in result.entries], [0.25, 0.25, 0.25])
        self.assertEqual([entry.chunk_id for entry in result.entries], ["fixture-z-boilerplate", "fixture-a", "fixture-b"])
        self.assertTrue(result.entries[0].boilerplate)
        self.assertEqual(result.counts["weighted"], 1)

    def test_pins_lead_in_supplied_order_and_keep_existing_semantic_scores(self) -> None:
        held = LegHit("fixture-held", 1, "fixture-held-cluster", False)
        citing = LegHit("fixture-citing", 1, "fixture-citing-cluster", True)
        overflow = LegHit("fixture-overflow", 2, "fixture-overflow-cluster", False)
        second = LegHit("fixture-next-object", 1, "fixture-next-cluster", False)
        top = LegHit("fixture-semantic-top", 1, "fixture-top-cluster", False)
        legs = {
            "dense": [top, overflow, LegHit(citing.chunk_id, 9, citing.dupe_cluster, True)],
            "lexical": [LegHit(citing.chunk_id, 8, citing.dupe_cluster, False)],
        }
        baseline = fuse(legs, rrf_k=5, boilerplate_weight=0.5, collapse=False, pool_cap=5)
        self.assertEqual(
            fuse(legs, pins=(), reservation=2, rrf_k=5, boilerplate_weight=0.5, collapse=False, pool_cap=5),
            baseline,
        )
        result = fuse(legs, pins=[
            PinnedHit(held, 0), PinnedHit(citing, 0), PinnedHit(overflow, 0),
            PinnedHit(citing, 1), PinnedHit(second, 1), PinnedHit(overflow, 1),
        ], reservation=2, rrf_k=5, boilerplate_weight=0.5, collapse=False, pool_cap=5)

        # Object 1's repeat of a chunk object 0 holds takes none of its seats, so its overflow hit is seated.
        self.assertEqual([entry.chunk_id for entry in result.entries], [
            held.chunk_id, citing.chunk_id, second.chunk_id, overflow.chunk_id, top.chunk_id,
        ])
        self.assertEqual([entry.pool_rank for entry in result.entries], [1, 2, 3, 4, 5])
        self.assertEqual([entry.object for entry in result.entries], [0, 0, 1, 1, None])
        self.assertEqual([entry.pinned for entry in result.entries], [True, True, True, True, False])
        self.assertEqual(result.entries[0].score, 0.0)
        self.assertEqual(result.entries[0].leg_ranks, ())
        semantic_citing = next(entry for entry in baseline.entries if entry.chunk_id == citing.chunk_id)
        self.assertEqual(result.entries[1].score, semantic_citing.score)
        self.assertEqual(result.entries[1].leg_ranks, semantic_citing.leg_ranks)
        self.assertEqual(result.entries[1].to_wire(), {
            **semantic_citing.to_wire(), "pool_rank": 2, "pinned": True, "object": 0,
        })
        self.assertEqual(result.entries[-1].to_wire()["pinned"], False)
        self.assertIsNone(result.entries[-1].to_wire()["object"])
        self.assertEqual(result.counts, {
            "union": 5, "after_collapse": 5, "weighted": 1, "cut": 0, "pinned": 4, "pinned_cut": 0,
        })

    def test_a_chunk_an_object_already_holds_takes_none_of_its_seats(self) -> None:
        held = LegHit("fixture-held-and-citing", 1, "fixture-own-cluster", False)
        citing = LegHit("fixture-citing-only", 2, "fixture-citing-cluster", False)
        result = fuse({}, pins=[
            PinnedHit(held, 0), PinnedHit(held, 0), PinnedHit(citing, 0), PinnedHit(held, 1), PinnedHit(citing, 1),
        ], reservation=2, rrf_k=5, boilerplate_weight=0.5, collapse=True, pool_cap=5)

        self.assertEqual([(entry.chunk_id, entry.object) for entry in result.entries], [
            (held.chunk_id, 0), (citing.chunk_id, 0),
        ])
        self.assertEqual(result.counts["pinned"], 2)

    def test_first_pin_wins_the_collapse_and_only_cap_cuts_count_as_pinned_cut(self) -> None:
        first = LegHit("fixture-pin-first", 1, "fixture-shared-cluster", False)
        second = LegHit("fixture-pin-second", 2, "fixture-second-cluster", False)
        cutoff = LegHit("fixture-pin-cutoff", 1, "fixture-cutoff-cluster", False)
        result = fuse({"lexical": [
            LegHit("fixture-fused-same-cluster", 1, first.dupe_cluster, False),
            LegHit("fixture-fused-other-cluster", 2, "fixture-other-cluster", False),
        ]}, pins=[
            PinnedHit(first, 0),
            PinnedHit(LegHit("fixture-pin-same-cluster", 1, first.dupe_cluster, False), 1),
            PinnedHit(second, 1),
            PinnedHit(cutoff, 2),
        ], reservation=2, rrf_k=5, boilerplate_weight=1, collapse=True, pool_cap=2)

        self.assertEqual([entry.chunk_id for entry in result.entries], [first.chunk_id, second.chunk_id])
        self.assertEqual(result.counts, {
            "union": 6, "after_collapse": 4, "weighted": 0, "cut": 2, "pinned": 2, "pinned_cut": 1,
        })
