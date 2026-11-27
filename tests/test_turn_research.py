"""The research configuration, its reader coverage, and the combined turn hash."""

import unittest
from types import MappingProxyType

from gideon.turn.config import RETRIEVAL_CONFIG, config_hash
from gideon.turn.research import (
    ADMITTED_VALUES,
    RESEARCH_CONFIG,
    check_research_config,
    turn_hash,
)

PENDING_READERS = {
    "release_unit": 18,
    "repair_bound": 18,
    "nested_citations": 21,
    "analysis_marker": 22,
    "chip_document": 26,
}


class TurnResearch(unittest.TestCase):
    def test_artifact_has_the_exact_research_keys_and_admitted_values(self) -> None:
        self.assertEqual(set(RESEARCH_CONFIG), {
            "tag_grammar", "release_unit", "repair_bound", "max_tokens", "evidence_order",
            "history_form", "analysis_marker", "chip_document", "nested_citations",
        })
        self.assertIsInstance(RESEARCH_CONFIG, MappingProxyType)
        self.assertEqual(ADMITTED_VALUES.keys(), RESEARCH_CONFIG.keys())
        self.assertIsNone(check_research_config(RESEARCH_CONFIG))
        for key, admitted in ADMITTED_VALUES.items():
            with self.subTest(key=key):
                value = RESEARCH_CONFIG[key]
                if callable(admitted):
                    self.assertEqual(key, "max_tokens")
                    self.assertTrue(admitted(value))
                else:
                    self.assertEqual(admitted, (value,))
        self.assertTrue(RESEARCH_CONFIG.keys().isdisjoint(RETRIEVAL_CONFIG))

    def test_check_names_missing_unknown_and_unsupported_keys(self) -> None:
        missing = dict(RESEARCH_CONFIG)
        del missing["release_unit"]
        self.assertEqual(check_research_config(missing), "release_unit")
        self.assertEqual(
            check_research_config({**RESEARCH_CONFIG, "fixture_key": "fixture value"}), "fixture_key",
        )
        for key in RESEARCH_CONFIG:
            with self.subTest(key=key):
                self.assertEqual(check_research_config({**RESEARCH_CONFIG, key: "fixture value"}), key)

    def test_token_bound_accepts_only_positive_integers(self) -> None:
        released = RESEARCH_CONFIG["max_tokens"]
        assert type(released) is int
        self.assertIsNone(check_research_config({**RESEARCH_CONFIG, "max_tokens": released + 1}))
        for value in (0, -released, True, float(released)):
            with self.subTest(value=value):
                self.assertEqual(check_research_config({**RESEARCH_CONFIG, "max_tokens": value}), "max_tokens")
        self.assertEqual(check_research_config({**RESEARCH_CONFIG, "repair_bound": True}), "repair_bound")

    def test_turn_hash_covers_both_artifacts_and_each_research_key(self) -> None:
        digest = turn_hash(RETRIEVAL_CONFIG)
        self.assertEqual(digest, config_hash({
            "retrieval": dict(RETRIEVAL_CONFIG), "research": dict(RESEARCH_CONFIG),
        }))
        self.assertEqual(digest, turn_hash(RETRIEVAL_CONFIG, RESEARCH_CONFIG))
        self.assertNotEqual(digest, config_hash(RETRIEVAL_CONFIG))
        for key in RESEARCH_CONFIG:
            with self.subTest(key=key):
                self.assertNotEqual(digest, turn_hash(RETRIEVAL_CONFIG, {**RESEARCH_CONFIG, key: "fixture value"}))

    def test_turn_hash_encodes_nested_mapping_views(self) -> None:
        options = {"fixture_stages": [MappingProxyType({"mode": "fictitious stage"})]}
        retrieval = MappingProxyType({**RETRIEVAL_CONFIG, "fixture_options": MappingProxyType(options)})
        self.assertEqual(turn_hash(retrieval), config_hash({
            "retrieval": {**RETRIEVAL_CONFIG, "fixture_options": {"fixture_stages": [{"mode": "fictitious stage"}]}},
            "research": dict(RESEARCH_CONFIG),
        }))
        self.assertNotEqual(turn_hash(retrieval), turn_hash(RETRIEVAL_CONFIG))

    def test_pending_readers_cover_the_keys_without_reader_tests(self) -> None:
        self.assertEqual(
            PENDING_READERS.keys(),
            RESEARCH_CONFIG.keys() - {"tag_grammar", "max_tokens", "history_form", "evidence_order"},
        )
