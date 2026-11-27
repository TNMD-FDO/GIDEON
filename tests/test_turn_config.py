"""The release retrieval config, override boundaries, and canonical hash."""

import unittest
from typing import get_args

from gideon.turn.config import (
    _HARD_CAP,
    RETRIEVAL_CONFIG,
    OverrideRefusal,
    config_hash,
    effective_config,
)
from gideon.turn.states import RequestWord


class Config(unittest.TestCase):
    def test_release_config_has_exactly_the_retrieval_keys(self) -> None:
        self.assertEqual(set(RETRIEVAL_CONFIG), {
            "leg_limit", "pool_cap", "reservations.exact_object", "reservations.controlling", "rrf.k", "collapse",
            "boilerplate_weight", "query_plan", "rerank.stage1", "rerank.stage2", "stage2_depth",
            "rerank.semaphore", "rerank.timeout_s",
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
        for key, value in (
            ("collapse", collapse), ("reservations.exact_object", exact_object), ("reservations.controlling", controlling),
        ):
            with self.subTest(key=key):
                changed = effective_config({key: value}, reranker=True)
                assert not isinstance(changed, OverrideRefusal)
                self.assertEqual(changed, {**first, key: value})
                self.assertNotEqual(digest, config_hash(changed))
        self.assertEqual(config_hash(RETRIEVAL_CONFIG), digest)

    def test_an_equal_weight_in_either_spelling_hashes_alike(self) -> None:
        for spellings in ((1, 1.0), (0, 0.0, -0.0)):
            with self.subTest(spellings=spellings):
                configs = [effective_config({"boilerplate_weight": value}, reranker=True) for value in spellings]
                digests = {config_hash(config) for config in configs if not isinstance(config, OverrideRefusal)}
                self.assertEqual(len(digests), 1)

    def test_override_boundaries_accept_only_the_supported_values(self) -> None:
        reservation_cases = ((1, _HARD_CAP), (0, -1, _HARD_CAP + 1, True, 1.0, "1"))
        seats = RETRIEVAL_CONFIG["rerank.semaphore"]
        assert type(seats) is int
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
            ("rerank.semaphore", (seats,), (0, seats + 1, True, float(seats), str(seats))),
            ("rerank.timeout_s", (1, 60), (0, -1, 61, False, 1.0, "1")),
        )
        for key, accepted, refused in cases:
            for value in accepted:
                with self.subTest(key=key, accepted=value):
                    self.assertEqual(effective_config({key: value}, reranker=True), {**RETRIEVAL_CONFIG, key: value})
            for value in refused:
                with self.subTest(key=key, refused=value):
                    self.assertEqual(effective_config({key: value}, reranker=True), OverrideRefusal(key, "override_invalid"))
        self.assertEqual(
            effective_config({"fixture_unknown": 1}, reranker=True), OverrideRefusal("fixture_unknown", "override_not_allowed"),
        )
