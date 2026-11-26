"""The release retrieval config, override boundaries, and canonical hash."""

import unittest

from gideon.turn.config import (
    _HARD_CAP,
    RETRIEVAL_CONFIG,
    OverrideRefusal,
    config_hash,
    effective_config,
)


class Config(unittest.TestCase):
    def test_release_config_has_exactly_the_retrieval_keys(self) -> None:
        self.assertEqual(set(RETRIEVAL_CONFIG), {
            "leg_limit", "pool_cap", "reservations.exact_object", "rrf.k", "collapse",
            "boilerplate_weight", "query_plan",
        })
        self.assertEqual(effective_config(), RETRIEVAL_CONFIG)

    def test_hash_is_stable_across_calls_and_key_order_and_changes_with_an_override(self) -> None:
        first, second = effective_config(), effective_config({})
        assert not isinstance(first, OverrideRefusal) and not isinstance(second, OverrideRefusal)
        digest = config_hash(first)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(digest, config_hash(second))
        self.assertEqual(digest, config_hash(dict(reversed(list(first.items())))))

        collapse = "off" if first["collapse"] == "on" else "on"
        reservation = 1 if first["reservations.exact_object"] != 1 else _HARD_CAP
        for key, value in (("collapse", collapse), ("reservations.exact_object", reservation)):
            with self.subTest(key=key):
                changed = effective_config({key: value})
                assert not isinstance(changed, OverrideRefusal)
                self.assertEqual(changed, {**first, key: value})
                self.assertNotEqual(digest, config_hash(changed))
        self.assertEqual(config_hash(RETRIEVAL_CONFIG), digest)

    def test_an_equal_weight_in_either_spelling_hashes_alike(self) -> None:
        for spellings in ((1, 1.0), (0, 0.0, -0.0)):
            with self.subTest(spellings=spellings):
                configs = [effective_config({"boilerplate_weight": value}) for value in spellings]
                digests = {config_hash(config) for config in configs if not isinstance(config, OverrideRefusal)}
                self.assertEqual(len(digests), 1)

    def test_override_boundaries_accept_only_the_supported_values(self) -> None:
        cases: tuple[tuple[str, tuple[object, ...], tuple[object, ...]], ...] = (
            ("leg_limit", (1, 500), (0, 501, True, 1.0)),
            ("pool_cap", (1, 500), (0, 501, False, "1")),
            ("reservations.exact_object", (1, _HARD_CAP), (0, -1, _HARD_CAP + 1, True, 1.0, "1")),
            ("rrf.k", (1, 1001), (0, -1, True, 1.0)),
            ("collapse", ("on", "off"), (True, "ON", "fixture-mode")),
            ("boilerplate_weight", (0, 1.0), (-0.01, 1.01, False, "0", float("nan"), float("inf"))),
            ("query_plan", ("off",), ("on", False, None)),
        )
        for key, accepted, refused in cases:
            for value in accepted:
                with self.subTest(key=key, accepted=value):
                    self.assertEqual(effective_config({key: value}), {**RETRIEVAL_CONFIG, key: value})
            for value in refused:
                with self.subTest(key=key, refused=value):
                    self.assertEqual(effective_config({key: value}), OverrideRefusal(key, "override_invalid"))
        self.assertEqual(
            effective_config({"fixture_unknown": 1}), OverrideRefusal("fixture_unknown", "override_not_allowed"),
        )
