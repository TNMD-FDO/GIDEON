"""Reciprocal rank fusion, cluster collapse, and pool order."""

import unittest
from dataclasses import replace

from gideon.turn.fusion import LegHit, PinnedHit, fuse
from gideon.turn.tier import Jurisdiction, authority_tier

JURISDICTION = Jurisdiction(("fixture-scotus",), ("fixture-circuit",), (), ())
NO_CONTROLLING_COUNTS = {
    "reserved": 0, "reserved_only": 0, "reserved_promoted": 0, "reserved_inherited": 0, "reserved_cut": 0,
}
CROSS_LEG_CLUSTER = {
    "dense": [
        LegHit("fixture-other", 1, "fixture-other-cluster", False, "fixture-scotus", "published"),
        LegHit("fixture-a-lower", 2, "fixture-duplicate-cluster", False, "fixture-scotus", "published"),
        LegHit("fixture-tail", 3, "fixture-tail-cluster", False, "fixture-scotus", "published"),
    ],
    "lexical": [LegHit("fixture-z-higher", 1, "fixture-duplicate-cluster", False, "fixture-other", "published")],
}


def tier_for(hit: LegHit) -> str:
    return authority_tier("caselaw", hit.court, hit.precedential, JURISDICTION)


class Fusion(unittest.TestCase):
    def test_equal_voice_and_one_entry_for_a_chunk_returned_by_both_legs(self) -> None:
        shared = LegHit("fixture-shared", 2, "fixture-shared-cluster", False, "fixture-scotus", "published")
        result = fuse({
            "dense": [LegHit("fixture-dense", 1, "fixture-dense-cluster", False, "fixture-circuit", "published"), shared],
            "lexical": [LegHit("fixture-lexical", 1, "fixture-lexical-cluster", False, "fixture-other", "unknown"), shared],
        }, tier_for=tier_for, rrf_k=5, boilerplate_weight=1, collapse=False, pool_cap=4)
        self.assertEqual([entry.chunk_id for entry in result.entries], ["fixture-shared", "fixture-dense", "fixture-lexical"])
        self.assertEqual(dict(result.entries[0].leg_ranks), {"dense": 2, "lexical": 2})
        self.assertAlmostEqual(result.entries[0].score, 2 / 7)
        for entry in result.entries[1:]:
            self.assertAlmostEqual(entry.score, 1 / 6)
        self.assertEqual(result.counts["union"], 3)
        self.assertEqual(result.entries[0].to_wire(), {
            "chunk_id": shared.chunk_id, "pool_rank": 1, "rank": 1, "score": 2 / 7, "stage2_score": None,
            "leg_ranks": {"dense": 2, "lexical": 2}, "dupe_cluster": shared.dupe_cluster, "boilerplate": False, "authority_tier": "scotus",
            "pinned": False, "object": None, "reserved": None, "controlling_rank": None,
        })
        self.assertEqual([entry.authority_tier for entry in result.entries[1:]], ["circuit_published", "persuasive"])

    def test_collapse_keeps_the_higher_ranked_member_before_applying_the_cap(self) -> None:
        legs = CROSS_LEG_CLUSTER
        result = fuse(legs, tier_for=tier_for, rrf_k=5, boilerplate_weight=1, collapse=True, pool_cap=2)
        self.assertEqual([entry.chunk_id for entry in result.entries], ["fixture-other", "fixture-z-higher"])
        self.assertEqual([entry.pool_rank for entry in result.entries], [1, 2])
        self.assertEqual(dict(result.entries[1].leg_ranks), {"lexical": 1})
        self.assertAlmostEqual(result.entries[1].score, 1 / 6)
        self.assertEqual(result.entries[1].authority_tier, "persuasive")
        self.assertEqual(result.counts, {
            "union": 4, "after_collapse": 3, "weighted": 0, "cut": 1, "pinned": 0, "pinned_cut": 0,
            **NO_CONTROLLING_COUNTS,
        })

        expanded = fuse(legs, tier_for=tier_for, rrf_k=5, boilerplate_weight=1, collapse=False, pool_cap=4)
        self.assertEqual({entry.chunk_id for entry in expanded.entries if entry.dupe_cluster == "fixture-duplicate-cluster"},
                         {"fixture-a-lower", "fixture-z-higher"})
        self.assertEqual(expanded.counts["after_collapse"], expanded.counts["union"])

    def test_boilerplate_multiplier_and_equal_scores_sort_by_best_rank_then_chunk_id(self) -> None:
        legs = {
            "dense": [
                LegHit("fixture-z-boilerplate", 1, "fixture-z-cluster", True, "fixture-scotus", "published"),
                LegHit("fixture-b", 3, "fixture-b-cluster", False, "fixture-scotus", "published"),
            ],
            "lexical": [LegHit("fixture-a", 3, "fixture-a-cluster", False, "fixture-scotus", "published")],
        }
        result = fuse(legs, tier_for=tier_for, rrf_k=1, boilerplate_weight=0.5, collapse=False, pool_cap=3)
        self.assertEqual([entry.score for entry in result.entries], [0.25, 0.25, 0.25])
        self.assertEqual([entry.chunk_id for entry in result.entries], ["fixture-z-boilerplate", "fixture-a", "fixture-b"])
        self.assertTrue(result.entries[0].boilerplate)
        self.assertEqual(result.counts["weighted"], 1)

    def test_pins_lead_in_supplied_order_and_keep_existing_semantic_scores(self) -> None:
        held = LegHit("fixture-held", 1, "fixture-held-cluster", False, "fixture-scotus", "published")
        citing = LegHit("fixture-citing", 1, "fixture-citing-cluster", True, "fixture-other", "published")
        overflow = LegHit("fixture-overflow", 2, "fixture-overflow-cluster", False, "fixture-scotus", "published")
        second = LegHit("fixture-next-object", 1, "fixture-next-cluster", False, "fixture-scotus", "published")
        top = LegHit("fixture-semantic-top", 1, "fixture-top-cluster", False, "fixture-scotus", "published")
        lexical_citing = replace(
            citing, rank=8, dupe_cluster="fixture-lexical-citing-cluster", boilerplate=False,
            court="fixture-circuit", precedential="unknown",
        )
        legs = {
            "dense": [top, overflow, replace(citing, rank=9)],
            "lexical": [lexical_citing],
        }
        baseline = fuse(legs, tier_for=tier_for, rrf_k=5, boilerplate_weight=0.5, collapse=False, pool_cap=5)
        self.assertEqual(
            fuse(legs, tier_for=tier_for, pins=(), reservation=2, rrf_k=5, boilerplate_weight=0.5, collapse=False, pool_cap=5),
            baseline,
        )
        result = fuse(legs, pins=[
            PinnedHit(held, 0), PinnedHit(citing, 0), PinnedHit(overflow, 0),
            PinnedHit(citing, 1), PinnedHit(second, 1), PinnedHit(overflow, 1),
        ], tier_for=tier_for, reservation=2, rrf_k=5, boilerplate_weight=0.5, collapse=False, pool_cap=5)

        # Object 1's repeat of a chunk object 0 holds takes none of its seats, so its overflow hit is seated.
        self.assertEqual([entry.chunk_id for entry in result.entries], [
            held.chunk_id, citing.chunk_id, second.chunk_id, overflow.chunk_id, top.chunk_id,
        ])
        self.assertEqual([entry.pool_rank for entry in result.entries], [1, 2, 3, 4, 5])
        self.assertEqual([entry.object for entry in result.entries], [0, 0, 1, 1, None])
        self.assertEqual([entry.pinned for entry in result.entries], [True, True, True, True, False])
        self.assertEqual(result.entries[0].score, 0.0)
        self.assertEqual(result.entries[0].leg_ranks, ())
        self.assertEqual(result.entries[0].authority_tier, "scotus")
        semantic_citing = next(entry for entry in baseline.entries if entry.chunk_id == citing.chunk_id)
        self.assertEqual(result.entries[1].score, semantic_citing.score)
        self.assertEqual(result.entries[1].leg_ranks, semantic_citing.leg_ranks)
        self.assertEqual(result.entries[1].dupe_cluster, lexical_citing.dupe_cluster)
        self.assertEqual(result.entries[1].authority_tier, "circuit_unpublished")
        self.assertEqual(result.entries[1].to_wire(), {
            **semantic_citing.to_wire(), "pool_rank": 2, "rank": 2, "pinned": True, "object": 0,
        })
        self.assertEqual(result.entries[-1].to_wire()["pinned"], False)
        self.assertIsNone(result.entries[-1].to_wire()["object"])
        self.assertEqual(result.counts, {
            "union": 5, "after_collapse": 5, "weighted": 1, "cut": 0, "pinned": 4, "pinned_cut": 0,
            **NO_CONTROLLING_COUNTS,
        })

    def test_a_chunk_an_object_already_holds_takes_none_of_its_seats(self) -> None:
        held = LegHit("fixture-held-and-citing", 1, "fixture-own-cluster", False, "fixture-scotus", "published")
        citing = LegHit("fixture-citing-only", 2, "fixture-citing-cluster", False, "fixture-scotus", "published")
        result = fuse({}, pins=[
            PinnedHit(held, 0), PinnedHit(held, 0), PinnedHit(citing, 0), PinnedHit(held, 1), PinnedHit(citing, 1),
        ], tier_for=tier_for, reservation=2, rrf_k=5, boilerplate_weight=0.5, collapse=True, pool_cap=5)

        self.assertEqual([(entry.chunk_id, entry.object) for entry in result.entries], [
            (held.chunk_id, 0), (citing.chunk_id, 0),
        ])
        self.assertEqual(result.counts["pinned"], 2)

    def test_first_pin_wins_the_collapse_and_only_cap_cuts_count_as_pinned_cut(self) -> None:
        first = LegHit("fixture-pin-first", 1, "fixture-shared-cluster", False, "fixture-scotus", "published")
        second = LegHit("fixture-pin-second", 2, "fixture-second-cluster", False, "fixture-scotus", "published")
        cutoff = LegHit("fixture-pin-cutoff", 1, "fixture-cutoff-cluster", False, "fixture-scotus", "published")
        result = fuse({"lexical": [
            replace(first, chunk_id="fixture-fused-same-cluster"),
            LegHit("fixture-fused-other-cluster", 2, "fixture-other-cluster", False, "fixture-scotus", "published"),
        ]}, pins=[
            PinnedHit(first, 0),
            PinnedHit(replace(first, chunk_id="fixture-pin-same-cluster"), 1),
            PinnedHit(second, 1),
            PinnedHit(cutoff, 2),
        ], tier_for=tier_for, reservation=2, rrf_k=5, boilerplate_weight=1, collapse=True, pool_cap=2)

        self.assertEqual([entry.chunk_id for entry in result.entries], [first.chunk_id, second.chunk_id])
        self.assertEqual(result.counts, {
            "union": 6, "after_collapse": 4, "weighted": 0, "cut": 2, "pinned": 2, "pinned_cut": 1,
            **NO_CONTROLLING_COUNTS,
        })

    def test_controlling_seats_add_no_score_or_rank_and_append_new_chunks_in_leg_order(self) -> None:
        ahead = LegHit("fixture-ahead", 1, "fixture-ahead-cluster", False, "fixture-other", "published")
        shared = LegHit("fixture-shared", 2, "fixture-shared-cluster", False, "fixture-circuit", "unknown")
        pin = LegHit("fixture-pin", 1, "fixture-pin-cluster", False, "fixture-scotus", "published")
        added = [
            LegHit(f"fixture-added-{rank}", rank, f"fixture-added-cluster-{rank}", True, "fixture-circuit", "published")
            for rank in (3, 4, 5)
        ]
        legs = {"dense": [ahead, shared]}
        pins = (PinnedHit(pin, 0),)
        baseline = fuse(
            legs, tier_for=tier_for, rrf_k=5, boilerplate_weight=0.5, collapse=True, pool_cap=6,
            pins=pins, reservation=1,
        )
        result = fuse(
            legs, tier_for=tier_for, rrf_k=5, boilerplate_weight=0.5, collapse=True, pool_cap=6,
            pins=pins, reservation=1, controlling_reservation=4,
            controlling_hits=(replace(shared, rank=1, boilerplate=True), replace(pin, rank=2), *added),
        )
        self.assertEqual(result.entries[:3], (
            replace(baseline.entries[0], controlling_rank=2), baseline.entries[1],
            replace(baseline.entries[2], controlling_rank=1),
        ))
        self.assertEqual([entry.chunk_id for entry in result.entries[3:]], [hit.chunk_id for hit in added[:2]])
        for entry, hit in zip(result.entries[3:], added[:2], strict=True):
            self.assertEqual((entry.score, entry.leg_ranks, entry.authority_tier), (0.0, (), "circuit_published"))
            self.assertEqual(entry.to_wire()["reserved"], "controlling")
            self.assertEqual(entry.to_wire()["controlling_rank"], hit.rank)
        self.assertEqual(result.counts, {
            **baseline.counts, "union": 5, "after_collapse": 5, "reserved": 4, "reserved_only": 3,
        })

    def test_controlling_seat_promotes_over_persuasive_survivor_with_its_own_score_and_ranks(self) -> None:
        legs = CROSS_LEG_CLUSTER
        ahead, dense_member, _ = legs["dense"]
        seated = replace(dense_member, rank=1)
        expanded = fuse(legs, tier_for=tier_for, rrf_k=5, boilerplate_weight=1, collapse=False, pool_cap=4)
        original = next(entry for entry in expanded.entries if entry.chunk_id == dense_member.chunk_id)
        result = fuse(
            legs, tier_for=tier_for, rrf_k=5, boilerplate_weight=1, collapse=True, pool_cap=2,
            controlling_hits=(seated,), controlling_reservation=1,
        )
        self.assertEqual(result.entries[0].chunk_id, ahead.chunk_id)
        self.assertEqual(result.entries[1], replace(original, pool_rank=2, rank=2, controlling_rank=seated.rank))
        self.assertEqual(result.counts, {
            "union": 4, "after_collapse": 3, "weighted": 0, "cut": 1, "pinned": 0, "pinned_cut": 0,
            **NO_CONTROLLING_COUNTS, "reserved": 1, "reserved_promoted": 1,
        })

    def test_controlling_survivor_inherits_the_rank_and_then_survives_the_cut(self) -> None:
        lexical_survivor = replace(CROSS_LEG_CLUSTER["lexical"][0], court="fixture-circuit")
        legs = {**CROSS_LEG_CLUSTER, "lexical": [lexical_survivor]}
        ahead, dense_member, _ = legs["dense"]
        seated = replace(dense_member, rank=1)
        baseline = fuse(legs, tier_for=tier_for, rrf_k=5, boilerplate_weight=1, collapse=True, pool_cap=2)
        self.assertEqual([entry.chunk_id for entry in baseline.entries], [ahead.chunk_id, lexical_survivor.chunk_id])
        self.assertIsNone(baseline.entries[1].controlling_rank)
        unmarked_cut = fuse(legs, tier_for=tier_for, rrf_k=5, boilerplate_weight=1, collapse=True, pool_cap=1)
        self.assertEqual(unmarked_cut.entries, baseline.entries[:1])
        for cap in (2, 1):
            with self.subTest(cap=cap):
                result = fuse(
                    legs, tier_for=tier_for, rrf_k=5, boilerplate_weight=1, collapse=True, pool_cap=cap,
                    controlling_hits=(seated,), controlling_reservation=1,
                )
                inherited = replace(baseline.entries[1], pool_rank=cap, rank=cap, controlling_rank=seated.rank)
                self.assertEqual(result.entries, (*baseline.entries[:cap - 1], inherited))
                self.assertEqual(result.counts, {
                    **baseline.counts, "cut": baseline.counts["after_collapse"] - cap,
                    "reserved": 1, "reserved_inherited": 1,
                })

    def test_pin_keeps_its_cluster_and_filling_the_cap_cuts_other_seats(self) -> None:
        pin = LegHit("fixture-pin", 1, "fixture-shared-cluster", False, "fixture-other", "published")
        seated = replace(pin, chunk_id="fixture-seated", court="fixture-circuit")
        separate = replace(seated, chunk_id="fixture-separate", dupe_cluster="fixture-unique-cluster", rank=2)
        result = fuse(
            {}, tier_for=tier_for, rrf_k=5, boilerplate_weight=1, collapse=True, pool_cap=1,
            pins=(PinnedHit(pin, 0),), reservation=1, controlling_hits=(seated, separate), controlling_reservation=2,
        )
        (survivor,) = result.entries
        self.assertEqual((survivor.chunk_id, survivor.pinned, survivor.reserved), (pin.chunk_id, True, None))
        self.assertEqual(result.counts, {
            "union": 3, "after_collapse": 2, "weighted": 0, "cut": 1, "pinned": 1, "pinned_cut": 0,
            **NO_CONTROLLING_COUNTS, "reserved_cut": 1,
        })

    def test_two_seated_members_of_a_cluster_keep_the_first_in_combined_order_with_its_rank(self) -> None:
        first = LegHit("fixture-first", 1, "fixture-shared-cluster", False, "fixture-scotus", "published")
        second = replace(first, chunk_id="fixture-second", rank=2)
        overflow = replace(first, chunk_id="fixture-overflow", dupe_cluster="fixture-unique-cluster", rank=3)
        result = fuse(
            {"dense": [first, second]}, tier_for=tier_for, rrf_k=5, boilerplate_weight=1, collapse=True, pool_cap=3,
            controlling_hits=(replace(second, rank=1), replace(first, rank=2), overflow), controlling_reservation=2,
        )
        (survivor,) = result.entries
        self.assertEqual((survivor.chunk_id, survivor.reserved, survivor.controlling_rank), (first.chunk_id, "controlling", 2))
        self.assertEqual(result.counts, {
            "union": 2, "after_collapse": 1, "weighted": 0, "cut": 0, "pinned": 0, "pinned_cut": 0,
            **NO_CONTROLLING_COUNTS, "reserved": 1,
        })

    def test_cut_keeps_three_pins_and_best_seven_seats_in_combined_order(self) -> None:
        pin_hits = [
            LegHit(f"fixture-pin-{rank}", rank, f"fixture-pin-cluster-{rank}", False, "fixture-scotus", "published")
            for rank in (3, 2, 1)
        ]
        controlling = [
            LegHit(f"fixture-seated-{rank}", rank, f"fixture-seated-cluster-{rank}", False, "fixture-scotus", "published")
            for rank in range(1, 21)
        ]
        unmarked = LegHit("fixture-unmarked", 1, "fixture-unmarked-cluster", False, "fixture-scotus", "published")
        voting_order = [unmarked, *reversed(controlling)]
        dense = [replace(hit, rank=rank) for rank, hit in enumerate(voting_order, start=1)]
        result = fuse(
            {"dense": dense}, tier_for=tier_for, rrf_k=5, boilerplate_weight=1, collapse=True, pool_cap=10,
            pins=tuple(PinnedHit(hit, 0) for hit in pin_hits), reservation=3,
            controlling_hits=controlling, controlling_reservation=len(controlling),
        )
        expected = [*pin_hits, *reversed(controlling[:7])]
        self.assertEqual([entry.chunk_id for entry in result.entries], [hit.chunk_id for hit in expected])
        self.assertEqual([entry.pool_rank for entry in result.entries], list(range(1, 11)))
        self.assertEqual([entry.controlling_rank for entry in result.entries], [None, None, None, *range(7, 0, -1)])
        self.assertEqual(result.counts, {
            "union": 24, "after_collapse": 24, "weighted": 0, "cut": 14, "pinned": 3, "pinned_cut": 0,
            **NO_CONTROLLING_COUNTS, "reserved": 7, "reserved_cut": 13,
        })
