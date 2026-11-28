"""Final-set selection and order over the candidate passages."""

import json
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace

from gideon.turn.config import OverrideRefusal, RetrievalConfig, effective_config
from gideon.turn.finalset import choose
from gideon.turn.fusion import PoolEntry
from gideon.turn.passages import Passage


def entry(
    chunk_id: str, pool_rank: int, score: float | None, *, pinned: bool = False, tier: str = "persuasive",
) -> PoolEntry:
    return PoolEntry(
        chunk_id, pool_rank, 1.0 / pool_rank, (), chunk_id, False, tier, rank=pool_rank,
        pinned=pinned, object=0 if pinned else None, stage2_score=score,
    )


def documents(entries: Sequence[PoolEntry], doc_ids: Mapping[str, str] | None = None) -> dict[str, Passage]:
    text = "A fictitious candidate passage."
    doc_ids = doc_ids or {}
    return {
        hit.chunk_id: Passage(
            hit.chunk_id, hit.pool_rank, "", text, doc_ids.get(hit.chunk_id, f"opinion-{hit.chunk_id}"), len(text.split()),
        )
        for hit in entries
    }


def config_for(overrides: Mapping[str, object] | None = None, *, stage_scored: bool) -> RetrievalConfig:
    config = effective_config(overrides, reranker=stage_scored)
    assert not isinstance(config, OverrideRefusal)
    return config


class FinalSet(unittest.TestCase):
    def test_picks_the_highest_raw_scores_of_the_read_candidates(self) -> None:
        config = config_for(stage_scored=True)
        limit = config["final_k"]
        floor = config["abstention.floor"]
        entries = [
            entry(f"fixture-chunk-{rank}", rank, floor + (1 - floor) * rank / (limit + 3))
            for rank in range(1, limit + 3)
        ]
        unread = entry("fixture-unread", len(entries) + 1, 1.0)
        passages = documents(entries)

        result = choose([*entries, unread], passages, config, {}, stage_scored=True)

        expected = list(reversed(entries))[:limit]
        self.assertEqual([hit.chunk_id for hit in result.hits], [item.chunk_id for item in expected])
        self.assertEqual(result.counts, {
            "candidates": len(entries), "size": limit, "pinned": 0, "by_tier": {"persuasive": limit},
            "tokens": sum(passages[item.chunk_id].token_count for item in expected),
            "controlling_seated": 0, "document_capped": 0, "exempt_documents": 0,
            "budget_dropped": 0, "objects_unseated": [],
            "floor": floor, "floor_dropped": 0, "best_under_floor": None,
        })

    def test_floor_leaves_only_survivors_for_the_pick_seats_cap_and_budget(self) -> None:
        config = config_for({"final_k": 2, "final_reserved_controlling": 1, "per_document_cap": 1}, stage_scored=True)
        floor = config["abstention.floor"]
        step = (1 - floor) / 4
        best = entry("fixture-best", 1, floor + 2 * step)
        duplicate = entry("fixture-duplicate", 2, floor + step)
        boundary = entry("fixture-boundary", 3, floor)
        home = entry("fixture-z-home", 4, floor / 2, tier="state_home")
        tied = entry("fixture-a-tied", 5, floor / 2)
        weakest = entry("fixture-weakest", 6, floor / 4)
        entries = [best, duplicate, boundary, home, tied, weakest]
        passages = documents(entries, dict.fromkeys((best.chunk_id, duplicate.chunk_id, home.chunk_id), "fixture-shared"))
        config["context_budget_tokens"] = passages[best.chunk_id].token_count + passages[boundary.chunk_id].token_count

        # The discarded held hit cannot exempt the shared opinion from the cap.
        result = choose(entries, passages, config, {0: {home.chunk_id}}, stage_scored=True)

        self.assertEqual([(hit.chunk_id, hit.seat) for hit in result.hits], [
            (best.chunk_id, "score"), (boundary.chunk_id, "score"),
        ])
        self.assertEqual(result.floor_dropped, (tied.chunk_id, home.chunk_id, weakest.chunk_id))
        self.assertEqual(result.counts, {
            "candidates": len(entries), "size": 2, "pinned": 0, "controlling_seated": 0, "by_tier": {"persuasive": 2},
            "tokens": config["context_budget_tokens"], "document_capped": 1, "exempt_documents": 0,
            "budget_dropped": 0, "objects_unseated": [],
            "floor": floor, "floor_dropped": 3, "best_under_floor": tied.stage2_score,
        })

    def test_support_counts_pins_against_the_default_and_overridden_threshold(self) -> None:
        config = config_for({"abstention.floor": 1}, stage_scored=True)
        overridden: RetrievalConfig = {**config, "abstention.thin_below": 1}
        threshold = config["abstention.thin_below"]
        pins = [entry(f"fixture-pin-{i}", i, 0.0, pinned=True) for i in range(1, threshold + 1)]
        unpinned = entry("fixture-unpinned", threshold + 1, 0.0)
        passages = documents([*pins, unpinned])
        for count, level in ((0, "none"), (1, "thin"), (threshold - 1, "thin"), (threshold, "ok")):
            with self.subTest(count=count):
                entries = [*pins[:count], unpinned]
                result = choose(entries, passages, config, {}, stage_scored=True)
                self.assertEqual([hit.chunk_id for hit in result.hits], [pin.chunk_id for pin in pins[:count]])
                self.assertEqual(result.floor_dropped, (unpinned.chunk_id,))
                self.assertEqual(result.support_level, level)
                self.assertEqual(
                    choose(entries, passages, overridden, {}, stage_scored=True).support_level,
                    "ok" if count else "none",
                )

    def test_every_pin_sits_beside_final_k_in_score_order(self) -> None:
        low_pin = entry("fixture-low-pin", 1, 0.1, pinned=True)
        high_pin = entry("fixture-high-pin", 2, 0.9, pinned=True)
        unseated = entry("fixture-unseated", 3, 0.3)
        best = replace(entry("fixture-best", 4, 0.7), rank=3)
        entries = [low_pin, high_pin, unseated, best]
        passages = documents(entries)

        config = config_for({"final_k": 1, "final_reserved_controlling": 0}, stage_scored=True)
        result = choose(entries, passages, config, {
            0: frozenset({low_pin.chunk_id}),
        }, stage_scored=True)

        self.assertEqual([hit.to_wire() for hit in result.hits], [
            {
                "number": number, "chunk_id": item.chunk_id, "pool_rank": item.pool_rank, "rank": item.rank,
                "stage2_score": item.stage2_score, "authority_tier": item.authority_tier,
                "seat": seat, "object": item.object,
                "doc_id": passages[item.chunk_id].doc_id, "tokens": passages[item.chunk_id].token_count,
            }
            for number, (item, seat) in enumerate(((high_pin, "pin"), (best, "score"), (low_pin, "pin")), start=1)
        ])
        self.assertEqual(result.counts["pinned"], 2)

    def test_equal_scores_break_by_pool_rank_then_chunk_id(self) -> None:
        config = config_for({"final_k": 2, "final_reserved_controlling": 0}, stage_scored=True)
        floor = config["abstention.floor"]
        first = entry("fixture-z", 1, floor)
        second = entry("fixture-a", 2, floor)
        third = entry("fixture-b", 2, floor)
        entries = [third, second, first]

        result = choose(entries, documents(entries), config, {}, stage_scored=True)

        self.assertEqual([hit.chunk_id for hit in result.hits], [first.chunk_id, second.chunk_id])

    def test_stage_off_takes_unpinned_candidates_in_pool_order_beside_pins(self) -> None:
        unread = entry("fixture-unread", 1, None)
        first = entry("fixture-first", 2, None)
        pin = entry("fixture-pin", 3, None, pinned=True)
        last = entry("fixture-last", 4, None)
        passages = documents([first, pin, last])

        config = config_for({"final_k": 1, "final_reserved_controlling": 0}, stage_scored=False)
        result = choose([last, unread, pin, first], passages, config, {}, stage_scored=False)

        self.assertEqual([(hit.chunk_id, hit.stage2_score) for hit in result.hits], [(first.chunk_id, None), (pin.chunk_id, None)])
        self.assertEqual(result.floor_dropped, ())
        self.assertEqual(result.counts["floor"], config["abstention.floor"])
        self.assertEqual(result.counts["floor_dropped"], 0)
        self.assertIsNone(result.counts["best_under_floor"])

    def test_document_cap_passes_over_a_chunk_and_does_not_count_pins(self) -> None:
        cap = config_for(stage_scored=True)["per_document_cap"]
        config = config_for({"final_k": cap + 1, "final_reserved_controlling": 0, "abstention.floor": 0}, stage_scored=True)
        pin = entry("fixture-pin", 1, 1.0, pinned=True)
        repeated = [entry(f"fixture-repeated-{rank}", rank, 1 / rank) for rank in range(2, cap + 3)]
        next_opinion = entry("fixture-next-opinion", cap + 3, 0.0)
        entries = [pin, *repeated, next_opinion]
        passages = documents(entries, dict.fromkeys((item.chunk_id for item in [pin, *repeated]), "fixture-shared"))

        for limit, expected, capped in (
            (cap, [pin, *repeated[:cap], next_opinion], 1),
            (0, [pin, *repeated], 0),
        ):
            with self.subTest(limit=limit):
                result = choose(entries, passages, {**config, "per_document_cap": limit}, {}, stage_scored=True)
                self.assertEqual([hit.chunk_id for hit in result.hits], [item.chunk_id for item in expected])
                self.assertEqual(result.counts["pinned"], 1)
                self.assertEqual(result.counts["document_capped"], capped)

    def test_reporter_exempts_the_document_with_more_held_hits_or_the_best_pool_rank(self) -> None:
        config = config_for({"abstention.floor": 0}, stage_scored=True)
        cap = config["per_document_cap"]
        early = [entry(f"fixture-early-{rank}", rank, 0.4) for rank in range(1, cap + 2)]
        later = [entry(f"fixture-later-{rank}", rank, 0.8) for rank in range(cap + 2, 2 * cap + 3)]
        unread = entry("fixture-unread", 2 * cap + 3, 1.0)
        entries = [*early, *later, unread]
        passages = documents(entries[:-1], {
            **dict.fromkeys((item.chunk_id for item in early), "fixture-first-opinion"),
            **dict.fromkeys((item.chunk_id for item in later), "fixture-second-opinion"),
        })
        most_held = {early[0].chunk_id, later[0].chunk_id, later[1].chunk_id}
        for held_by_object, kept, exemptions, capped in (
            ({0: most_held, 1: most_held, 2: {unread.chunk_id}}, [*later, *early[:cap]], 1, 1),
            ({0: {early[0].chunk_id, later[0].chunk_id}}, [*later[:cap], *early], 1, 1),
            ({0: {early[0].chunk_id}, 1: {later[0].chunk_id}}, [*later, *early], 2, 0),
        ):
            with self.subTest(held_by_object=held_by_object):
                result = choose(entries, passages, config, held_by_object, stage_scored=True)
                self.assertEqual([hit.chunk_id for hit in result.hits], [item.chunk_id for item in kept])
                self.assertEqual(result.counts["exempt_documents"], exemptions)
                self.assertEqual(result.counts["document_capped"], capped)

    def test_controlling_seats_displace_the_lowest_unpinned_hits_and_admit_state_home(self) -> None:
        pin = entry("fixture-pin", 1, 1.0, pinned=True, tier="scotus")
        best = entry("fixture-best", 2, 0.9)
        matter = entry("fixture-matter", 3, 0.8, tier="matter")
        low = entry("fixture-low", 4, 0.7)
        other_matter = entry("fixture-other-matter", 5, 0.6, tier="matter")
        other_persuasive = entry("fixture-other-persuasive", 6, 0.5)
        state = entry("fixture-state", 7, 0.4, tier="state_home")
        circuit = entry("fixture-circuit", 8, 0.3, tier="circuit_published")
        entries = [pin, best, matter, low, other_matter, other_persuasive, state, circuit]
        config = config_for({"final_k": 3, "final_reserved_controlling": 3, "abstention.floor": 0}, stage_scored=True)

        result = choose(entries, documents(entries), config, {}, stage_scored=True)

        self.assertEqual({hit.chunk_id: hit.seat for hit in result.hits}, {
            pin.chunk_id: "pin", best.chunk_id: "score", state.chunk_id: "controlling", circuit.chunk_id: "controlling",
        })
        self.assertEqual(result.counts["controlling_seated"], 2)

    def test_controlling_replacements_respect_the_cap_and_can_use_the_vacated_document_slot(self) -> None:
        best = entry("fixture-best", 1, 0.9)
        next_best = entry("fixture-next-best", 2, 0.8)
        lowest = entry("fixture-lowest", 3, 0.7)
        blocked = entry("fixture-blocked", 4, 0.6, tier="scotus")
        from_same_doc = entry("fixture-same-doc", 5, 0.5, tier="state_home")
        from_new_doc = entry("fixture-new-doc", 6, 0.4, tier="state_home")
        entries = [best, next_best, lowest, blocked, from_same_doc, from_new_doc]
        passages = documents(entries, {
            best.chunk_id: "fixture-full", blocked.chunk_id: "fixture-full",
            lowest.chunk_id: "fixture-vacated", from_same_doc.chunk_id: "fixture-vacated",
        })
        config = config_for({
            "final_k": 3, "final_reserved_controlling": 2, "per_document_cap": 1, "abstention.floor": 0,
        }, stage_scored=True)

        result = choose(entries, passages, config, {}, stage_scored=True)

        self.assertEqual({hit.chunk_id: hit.seat for hit in result.hits}, {
            best.chunk_id: "score", from_same_doc.chunk_id: "controlling", from_new_doc.chunk_id: "controlling",
        })
        self.assertEqual(result.counts["document_capped"], 1)

        # One more seat frees the first opinion's slot, so the earlier blocked hit can enter.
        result = choose(entries, passages, {**config, "final_reserved_controlling": 3}, {}, stage_scored=True)
        self.assertEqual({hit.chunk_id: hit.seat for hit in result.hits}, {
            blocked.chunk_id: "controlling", from_same_doc.chunk_id: "controlling", from_new_doc.chunk_id: "controlling",
        })
        self.assertEqual(result.counts["document_capped"], 0)

    def test_circuit_split_keeps_the_out_of_circuit_hits_that_outrank_marginal_home_hits(self) -> None:
        config = config_for(stage_scored=True)
        seats = config["final_reserved_controlling"]
        unreserved = config["final_k"] - seats
        strong_home = [entry(f"fixture-home-{i}", i, 0.95, tier="circuit_published") for i in range(1, seats + 1)]
        on_point = [entry(f"fixture-split-{i}", seats + i, 0.7) for i in range(1, unreserved + 1)]
        marginal_home = entry("fixture-marginal-home", seats + unreserved + 1, 0.55, tier="circuit_published")
        entries = [*strong_home, *on_point, marginal_home]

        result = choose(entries, documents(entries), config, {}, stage_scored=True)

        self.assertEqual({hit.chunk_id: hit.seat for hit in result.hits}, {
            item.chunk_id: "score" for item in [*strong_home, *on_point]
        })
        self.assertEqual(result.counts["controlling_seated"], 0)

    def test_orders_apply_to_the_same_selection_including_pins(self) -> None:
        persuasive = entry("fixture-persuasive", 1, 0.95, pinned=True)
        matter = entry("fixture-matter", 2, 0.8, tier="matter")
        circuit = entry("fixture-circuit", 3, 0.55, tier="circuit_published")
        scotus = entry("fixture-scotus", 4, 0.48, tier="scotus")
        district = entry("fixture-district", 5, 0.3, tier="district_home")
        state = entry("fixture-state", 6, 0.29, tier="state_home")
        best_circuit = entry("fixture-best-circuit", 7, 0.62, tier="circuit_published")
        entries = [persuasive, matter, circuit, scotus, district, state, best_circuit]
        passages = documents(entries)
        config = config_for(stage_scored=True)
        weights = config["hierarchy.weights"]

        def weighted(item: PoolEntry) -> float:
            assert item.stage2_score is not None
            return item.stage2_score * weights[item.authority_tier]

        for mode, expected in (
            ("weighted", sorted(entries, key=weighted, reverse=True)),
            ("lexicographic", [scotus, matter, best_circuit, circuit, district, state, persuasive]),
            ("off", [persuasive, matter, best_circuit, circuit, scotus, district, state]),
        ):
            with self.subTest(mode=mode):
                config = config_for({"hierarchy.mode": mode, "abstention.floor": 0}, stage_scored=True)
                result = choose(entries, passages, config, {}, stage_scored=True)
                self.assertEqual([(hit.number, hit.chunk_id) for hit in result.hits], [
                    (number, item.chunk_id) for number, item in enumerate(expected, start=1)
                ])

    def test_equal_weighted_scores_keep_pool_order_and_zero_weight_does_not_remove_a_hit(self) -> None:
        first = entry("fixture-first", 1, 0.25, tier="scotus")
        second = entry("fixture-second", 2, 0.5, tier="circuit_published")
        zero = entry("fixture-zero", 3, 1.0)
        entries = [second, zero, first]
        config = config_for(stage_scored=True)
        weights = {**config["hierarchy.weights"], "scotus": 1.0, "circuit_published": 0.5, "persuasive": 0.0}
        config = config_for({"hierarchy.weights": weights, "abstention.floor": 0}, stage_scored=True)

        result = choose(entries, documents(entries), config, {}, stage_scored=True)

        self.assertEqual([hit.chunk_id for hit in result.hits], [first.chunk_id, second.chunk_id, zero.chunk_id])

    def test_zero_weighted_ties_keep_the_ranked_order_and_the_budget_drops_the_weaker(self) -> None:
        # Fused order puts the weaker hit first; the stage's rank puts the stronger one first.
        weaker = replace(entry("fixture-weaker", 1, 0.2), rank=2)
        stronger = replace(entry("fixture-stronger", 2, 0.9), rank=1)
        entries = [weaker, stronger]
        passages = documents(entries)
        release = config_for(stage_scored=True)
        weights = {**release["hierarchy.weights"], "persuasive": 0.0}
        budget = passages[stronger.chunk_id].token_count
        config = config_for({"hierarchy.weights": weights, "context_budget_tokens": budget, "abstention.floor": 0}, stage_scored=True)

        result = choose(entries, passages, config, {}, stage_scored=True)

        self.assertEqual([hit.chunk_id for hit in result.hits], [stronger.chunk_id])
        self.assertEqual(result.counts["budget_dropped"], 1)

    def test_scored_off_mode_leaves_the_low_controlling_hit_below_the_cutoff(self) -> None:
        best = entry("fixture-best", 1, 0.9)
        home = entry("fixture-home", 2, 0.1, tier="state_home")
        entries = [best, home]
        config = config_for({
            "final_k": 1, "final_reserved_controlling": 1, "hierarchy.mode": "off", "abstention.floor": 0,
        }, stage_scored=True)

        result = choose(entries, documents(entries), config, {}, stage_scored=True)

        self.assertEqual([(hit.chunk_id, hit.seat) for hit in result.hits], [(best.chunk_id, "score")])

    def test_stage_off_keeps_pool_order_under_the_cap_and_the_modes_control_reservations(self) -> None:
        first = entry("fixture-first", 1, None)
        capped = entry("fixture-capped", 2, None)
        matter = entry("fixture-matter", 3, None, tier="matter")
        pin = entry("fixture-pin", 4, None, pinned=True)
        state = entry("fixture-state", 5, None, tier="state_home")
        circuit = entry("fixture-circuit", 6, None, tier="circuit_published")
        entries = [first, capped, matter, pin, state, circuit]
        passages = documents(entries, {first.chunk_id: "fixture-shared", capped.chunk_id: "fixture-shared"})
        for mode, expected in (
            ("weighted", [(first, "score"), (pin, "pin"), (state, "controlling")]),
            ("lexicographic", [(first, "score"), (pin, "pin"), (state, "controlling")]),
            ("off", [(first, "score"), (matter, "score"), (pin, "pin")]),
        ):
            with self.subTest(mode=mode):
                config = config_for({
                    "final_k": 2, "final_reserved_controlling": 1, "per_document_cap": 1, "hierarchy.mode": mode,
                }, stage_scored=False)
                result = choose(entries, passages, config, {}, stage_scored=False)
                self.assertEqual([(hit.chunk_id, hit.seat, hit.stage2_score) for hit in result.hits], [
                    (item.chunk_id, seat, None) for item, seat in expected
                ])
                self.assertEqual(result.counts["document_capped"], 1)

    def test_budget_keeps_exact_fit_and_drops_the_bottom_chunk_whole_one_token_over(self) -> None:
        first = entry("fixture-first", 1, 0.9)
        second = entry("fixture-second", 2, 0.8)
        displaced = entry("fixture-displaced", 3, 0.7)
        state = entry("fixture-state", 4, 0.2, tier="state_home")
        entries = [first, second, displaced, state]
        passages = documents(entries)
        selected = (first, second, state)
        total = sum(passages[item.chunk_id].token_count for item in selected)
        config = config_for({
            "final_k": len(selected), "final_reserved_controlling": 1, "abstention.floor": 0,
        }, stage_scored=True)

        for budget, expected, dropped, seats, level in (
            (total, selected, 0, 1, "ok"), (total - 1, selected[:-1], 1, 0, "thin"),
        ):
            with self.subTest(budget=budget):
                result = choose(entries, passages, {**config, "context_budget_tokens": budget}, {}, stage_scored=True)
                self.assertEqual([(hit.number, hit.chunk_id, hit.tokens) for hit in result.hits], [
                    (number, item.chunk_id, passages[item.chunk_id].token_count)
                    for number, item in enumerate(expected, start=1)
                ])
                self.assertEqual(result.counts, {
                    "candidates": len(entries), "size": len(expected), "pinned": 0, "controlling_seated": seats,
                    "by_tier": {tier: sum(item.authority_tier == tier for item in expected)
                                for tier in {item.authority_tier for item in expected}},
                    "tokens": sum(passages[item.chunk_id].token_count for item in expected),
                    "document_capped": 0, "exempt_documents": 0, "budget_dropped": dropped, "objects_unseated": [],
                    "floor": config["abstention.floor"], "floor_dropped": 0, "best_under_floor": None,
                })
                self.assertEqual(result.support_level, level)
                self.assertEqual(json.loads(json.dumps(result.counts)), result.counts)

    def test_budget_passes_over_a_protected_pin_and_drops_a_lower_pin_before_a_stronger_score_hit(self) -> None:
        best = entry("fixture-score", 1, 0.9)
        protected = entry("fixture-protected", 2, 0.4, pinned=True)
        lower = entry("fixture-lower-pin", 3, 0.2, pinned=True)
        entries = [best, protected, lower]
        passages = documents(entries)
        budget = passages[best.chunk_id].token_count + passages[protected.chunk_id].token_count
        config = config_for({"context_budget_tokens": budget}, stage_scored=True)

        result = choose(entries, passages, config, {}, stage_scored=True)
        self.assertEqual([hit.chunk_id for hit in result.hits], [best.chunk_id, protected.chunk_id])
        self.assertEqual(result.counts["budget_dropped"], 1)

        config["context_budget_tokens"] = passages[protected.chunk_id].token_count
        result = choose(entries, passages, config, {}, stage_scored=True)
        self.assertEqual([(hit.number, hit.chunk_id) for hit in result.hits], [(1, protected.chunk_id)])
        self.assertEqual(result.counts["pinned"], 1)
        self.assertEqual(result.counts["objects_unseated"], [])

    def test_budget_protects_each_reporter_cites_own_held_pin(self) -> None:
        shared = entry("fixture-first-cites-second-held", 1, 0.95, pinned=True, tier="scotus")
        first_held = entry("fixture-first-held", 2, 0.2, pinned=True)
        second_citing = replace(entry("fixture-second-citing", 3, 0.8, pinned=True), object=1)
        second_held = replace(entry("fixture-second-held", 4, 0.1, pinned=True), object=1)
        entries = [shared, first_held, second_citing, second_held]
        passages = documents(entries)
        budget = passages[first_held.chunk_id].token_count + passages[second_held.chunk_id].token_count
        config = config_for({"context_budget_tokens": budget}, stage_scored=True)

        result = choose(entries, passages, config, {
            0: {first_held.chunk_id}, 1: {shared.chunk_id, second_held.chunk_id},
        }, stage_scored=True)

        self.assertEqual([(hit.number, hit.chunk_id, hit.object) for hit in result.hits], [
            (1, first_held.chunk_id, 0), (2, second_held.chunk_id, 1),
        ])
        self.assertEqual(result.counts["pinned"], 2)
        self.assertEqual(result.counts["budget_dropped"], 2)
        self.assertEqual(result.counts["objects_unseated"], [])

    def test_budget_protects_by_raw_score_pool_rank_and_chunk_id_or_by_pool_order_with_the_stage_off(self) -> None:
        first = entry("fixture-first", 1, 0.4, pinned=True, tier="scotus")
        better_weighted = entry("fixture-b", 2, 0.8, pinned=True, tier="scotus")
        best_pin = entry("fixture-a", 2, 0.8, pinned=True)
        later = entry("fixture-later", 3, 0.8, pinned=True, tier="scotus")
        entries = [first, better_weighted, best_pin, later]
        passages = documents(entries)
        budget = passages[first.chunk_id].token_count
        held = {0: {"fixture-uncandidated-held-chunk"}}
        for scored, expected in ((True, best_pin), (False, first)):
            with self.subTest(scored=scored):
                config = config_for({"context_budget_tokens": budget}, stage_scored=scored)
                candidates = entries if scored else [replace(item, stage2_score=None) for item in entries]
                result = choose(candidates, passages, config, held, stage_scored=scored)
                self.assertEqual([(hit.number, hit.chunk_id) for hit in result.hits], [(1, expected.chunk_id)])
                self.assertEqual(result.counts["objects_unseated"], [])

    def test_budget_drops_protected_pins_bottom_first_and_names_their_unseated_objects(self) -> None:
        highest = replace(entry("fixture-highest", 1, 0.7, pinned=True, tier="scotus"), object=7)
        middle = replace(entry("fixture-middle", 2, 0.9, pinned=True), object=11)
        lowest = replace(entry("fixture-lowest", 3, 0.5, pinned=True), object=2)
        entries = [highest, middle, lowest]
        passages = documents(entries)
        tokens = passages[highest.chunk_id].token_count
        for budget, expected, unseated in (
            (tokens, [highest.chunk_id], [2, 11]), (tokens - 1, [], [2, 7, 11]),
        ):
            with self.subTest(budget=budget):
                config = config_for({"context_budget_tokens": budget}, stage_scored=True)
                result = choose(entries, passages, config, {}, stage_scored=True)
                self.assertEqual([hit.chunk_id for hit in result.hits], expected)
                self.assertEqual(result.counts["objects_unseated"], unseated)
                self.assertEqual(result.counts["size"], len(expected))
                self.assertEqual(result.counts["pinned"], len(expected))
                self.assertEqual(result.counts["tokens"], len(expected) * tokens)
                self.assertEqual(result.counts["budget_dropped"], len(unseated))
                self.assertEqual(result.support_level, "thin" if expected else "none")
