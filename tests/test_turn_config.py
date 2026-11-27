"""The release retrieval config, override boundaries, and canonical hash."""

import unittest
from types import MappingProxyType
from typing import Final, get_args

from gideon.turn.config import (
    _BUDGET_CEILING,
    _HARD_CAP,
    RETRIEVAL_CONFIG,
    OverrideRefusal,
    config_hash,
    effective_config,
)
from gideon.turn.states import RequestWord
from gideon.turn.tier import AUTHORITY_TIERS


class Config(unittest.TestCase):
    def test_release_config_has_exactly_the_retrieval_keys(self) -> None:
        self.assertEqual(set(RETRIEVAL_CONFIG), {
            "leg_limit", "pool_cap", "reservations.exact_object", "reservations.controlling", "rrf.k", "collapse",
            "boilerplate_weight", "query_plan", "rerank.stage1", "rerank.stage2", "stage2_depth",
            "rerank.semaphore", "rerank.timeout_s", "final_k",
            "hierarchy.mode", "hierarchy.weights", "final_reserved_controlling", "per_document_cap", "per_hit_chunks",
            "context_budget_tokens",
        })
        self.assertEqual(effective_config(reranker=True), RETRIEVAL_CONFIG)
        self.assertLessEqual(set(RETRIEVAL_CONFIG), set(get_args(RequestWord)))

    def test_stage2_off_default_and_on_refusal_without_a_reranker(self) -> None:
        off = effective_config(reranker=False)
        self.assertEqual(off, {**RETRIEVAL_CONFIG, "rerank.stage2": "off"})
        for reranker in (False, True):
            with self.subTest(reranker=reranker):
                self.assertEqual(effective_config({"rerank.stage2": "off"}, reranker=reranker), off)
        self.assertEqual(
            effective_config({"rerank.stage2": "on"}, reranker=False),
            OverrideRefusal("rerank.stage2", "override_invalid"),
        )

    def test_hash_is_stable_across_calls_and_key_order_and_changes_with_an_override(self) -> None:
        first, second = effective_config(reranker=True), effective_config({}, reranker=True)
        assert not isinstance(first, OverrideRefusal) and not isinstance(second, OverrideRefusal)
        digest = config_hash(first)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(digest, config_hash(second))
        self.assertEqual(digest, config_hash(dict(reversed(list(first.items())))))
        before_controlling = {key: value for key, value in first.items() if key != "reservations.controlling"}
        self.assertNotEqual(digest, config_hash(before_controlling))

        collapse = "off" if first["collapse"] == "on" else "on"
        exact_object = 1 if first["reservations.exact_object"] != 1 else _HARD_CAP
        controlling = 1 if first["reservations.controlling"] != 1 else _HARD_CAP
        final_k = _HARD_CAP if first["final_k"] < _HARD_CAP else first["final_reserved_controlling"]
        for key, value in (
            ("collapse", collapse), ("reservations.exact_object", exact_object), ("reservations.controlling", controlling),
            ("final_k", final_k), ("hierarchy.mode", "off" if first["hierarchy.mode"] != "off" else "weighted"),
            ("final_reserved_controlling", 0 if first["final_reserved_controlling"] else 1),
            ("per_document_cap", 0 if first["per_document_cap"] else 1),
            ("context_budget_tokens", 1 if first["context_budget_tokens"] != 1 else _BUDGET_CEILING),
        ):
            with self.subTest(key=key):
                changed = effective_config({key: value}, reranker=True)
                assert not isinstance(changed, OverrideRefusal)
                self.assertEqual(changed, {**first, key: value})
                self.assertNotEqual(digest, config_hash(changed))
        fixed_key: Final = "per_hit_chunks"
        self.assertNotEqual(digest, config_hash({key: value for key, value in first.items() if key != fixed_key}))
        same = effective_config({fixed_key: first[fixed_key]}, reranker=True)
        assert not isinstance(same, OverrideRefusal)
        self.assertEqual(digest, config_hash(same))
        self.assertEqual(config_hash(RETRIEVAL_CONFIG), digest)

    def test_an_equal_weight_in_either_spelling_hashes_alike(self) -> None:
        for spellings in ((1, 1.0), (0, 0.0, -0.0)):
            with self.subTest(spellings=spellings):
                configs = [effective_config({"boilerplate_weight": value}, reranker=True) for value in spellings]
                digests = {config_hash(config) for config in configs if not isinstance(config, OverrideRefusal)}
                self.assertEqual(len(digests), 1)

    def test_override_boundaries_accept_only_the_supported_values(self) -> None:
        reservation_cases = ((1, _HARD_CAP), (0, -1, _HARD_CAP + 1, True, 1.0, "1"))
        cap_cases = ((0, _HARD_CAP), (-1, _HARD_CAP + 1, True, 1.0, "1"))
        seats = RETRIEVAL_CONFIG["rerank.semaphore"]
        assert type(seats) is int
        per_hit = RETRIEVAL_CONFIG["per_hit_chunks"]
        assert type(per_hit) is int
        cases: tuple[tuple[str, tuple[object, ...], tuple[object, ...]], ...] = (
            ("leg_limit", (1, 500), (0, 501, True, 1.0)),
            ("pool_cap", (1, 500), (0, 501, False, "1")),
            ("reservations.exact_object", *reservation_cases),
            ("reservations.controlling", *reservation_cases),
            ("rrf.k", (1, 1001), (0, -1, True, 1.0)),
            ("collapse", ("on", "off"), (True, "ON", "fixture-mode")),
            ("boilerplate_weight", (0, 1.0), (-0.01, 1.01, False, "0", float("nan"), float("inf"))),
            ("query_plan", ("off",), ("on", False, None)),
            ("rerank.stage1", ("off",), ("on", False, None)),
            ("rerank.stage2", ("on", "off"), (True, "ON", "fixture-mode")),
            ("stage2_depth", (1, _HARD_CAP), (0, -1, _HARD_CAP + 1, True, 1.0, "1")),
            ("final_k", *reservation_cases),
            ("rerank.semaphore", (seats,), (0, seats + 1, True, float(seats), str(seats))),
            ("rerank.timeout_s", (1, 60), (0, -1, 61, False, 1.0, "1")),
            ("hierarchy.mode", ("weighted", "lexicographic", "off"), (None, 1, "WEIGHTED", "fixture-mode")),
            ("final_reserved_controlling", *cap_cases),
            ("per_document_cap", *cap_cases),
            ("per_hit_chunks", (per_hit,), (0, per_hit + 1, True, float(per_hit), str(per_hit))),
            ("context_budget_tokens", (1, _BUDGET_CEILING), (0, -1, _BUDGET_CEILING + 1, True, 1.0, "1")),
        )
        for key, accepted, refused in cases:
            supporting: dict[str, object] = {}
            if key == "final_k":
                supporting["final_reserved_controlling"] = 0
            elif key == "final_reserved_controlling":
                supporting["final_k"] = _HARD_CAP
            for value in accepted:
                with self.subTest(key=key, accepted=value):
                    self.assertEqual(
                        effective_config({**supporting, key: value}, reranker=True), {**RETRIEVAL_CONFIG, **supporting, key: value},
                    )
            for value in refused:
                with self.subTest(key=key, refused=value):
                    self.assertEqual(effective_config({key: value}, reranker=True), OverrideRefusal(key, "override_invalid"))
        self.assertEqual(
            effective_config({"fixture_unknown": 1}, reranker=True), OverrideRefusal("fixture_unknown", "override_not_allowed"),
        )

    def test_controlling_seats_are_checked_against_final_k_after_all_overrides(self) -> None:
        key = "final_reserved_controlling"
        overrides = {key: 2, "final_k": 1}
        for ordered in (overrides, dict(reversed(list(overrides.items())))):
            with self.subTest(keys=tuple(ordered)):
                self.assertEqual(effective_config(ordered, reranker=True), OverrideRefusal(key, "override_invalid"))

        overrides = {key: 2, "final_k": 2}
        for ordered in (overrides, dict(reversed(list(overrides.items())))):
            with self.subTest(keys=tuple(ordered)):
                self.assertEqual(effective_config(ordered, reranker=True), {**RETRIEVAL_CONFIG, **overrides})

    def test_weights_are_immutable_copies_in_authority_tier_order(self) -> None:
        key: Final = "hierarchy.weights"
        release = RETRIEVAL_CONFIG[key]
        first = effective_config(reranker=True)
        second = effective_config(reranker=True)
        assert not isinstance(first, OverrideRefusal) and not isinstance(second, OverrideRefusal)
        self.assertIsNot(first[key], release)
        self.assertIsNot(second[key], first[key])
        supplied = dict.fromkeys(reversed(AUTHORITY_TIERS), 1)
        override = effective_config({key: supplied}, reranker=True)
        assert not isinstance(override, OverrideRefusal)
        supplied[AUTHORITY_TIERS[0]] = 0
        self.assertEqual(override[key], dict.fromkeys(AUTHORITY_TIERS, 1.0))
        for weights in (release, first[key], second[key], override[key]):
            assert isinstance(weights, MappingProxyType)
            self.assertEqual(tuple(weights), AUTHORITY_TIERS)
            with self.assertRaises(TypeError):
                weights[AUTHORITY_TIERS[0]] = 0.0  # type: ignore[index]

    def test_weight_override_requires_all_tiers_and_only_unit_interval_numbers(self) -> None:
        key: Final = "hierarchy.weights"
        weights = dict.fromkeys(AUTHORITY_TIERS, 0.5)
        first_tier, *remaining_tiers = AUTHORITY_TIERS
        for invalid in (
            None, list(weights.items()), {}, {tier: weights[tier] for tier in remaining_tiers},
            {**weights, "fixture-tier": 0.5},
            *({**weights, first_tier: value} for value in (True, "0.5", -0.1, 1.1, float("nan"), float("inf"))),
        ):
            with self.subTest(invalid=invalid):
                self.assertEqual(effective_config({key: invalid}, reranker=True), OverrideRefusal(key, "override_invalid"))

        digests = set()
        for high, low in ((1, 0), (1.0, 0.0), (1.0, -0.0)):
            config = effective_config({key: {**weights, first_tier: high, remaining_tiers[0]: low}}, reranker=True)
            assert not isinstance(config, OverrideRefusal)
            self.assertTrue(all(type(weight) is float for weight in config[key].values()))
            digests.add(config_hash(config))
        self.assertEqual(len(digests), 1)

    def test_hash_changes_for_any_weight_and_is_independent_of_nested_key_order(self) -> None:
        key: Final = "hierarchy.weights"
        config = effective_config(reranker=True)
        assert not isinstance(config, OverrideRefusal)
        digest = config_hash(config)
        weights = config[key]
        self.assertEqual(digest, config_hash({**config, key: dict(reversed(list(weights.items())))}))
        for tier, weight in weights.items():
            with self.subTest(tier=tier):
                changed = effective_config({key: {**weights, tier: 0 if weight else 1}}, reranker=True)
                assert not isinstance(changed, OverrideRefusal)
                self.assertNotEqual(digest, config_hash(changed))
