"""Deterministic sampling, bounded arguments, and shared document identities."""

import ast
import hashlib
import math
import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gideon.host.index import sample
from gideon.host.render.worker import GENERATION_PATTERN


class Selection(unittest.TestCase):
    def test_selection_is_repeatable_label_seeded_and_near_its_rate(self) -> None:
        documents = tuple(f"{number:064x}" for number in range(20_000))
        label = "corpus-2099-01-03"
        rate = sample.SAMPLE_FILL_PER_MILLION
        selected = {document for document in documents if sample.in_fill(label, document, rate)}
        again = {document for document in documents if sample.in_fill(label, document, rate)}
        other = {document for document in documents if sample.in_fill("corpus-2099-01-04", document, rate)}
        self.assertEqual(selected, again)
        self.assertNotEqual(selected, other)
        fraction = rate / sample.SAMPLE_FILL_MAX
        expected = len(documents) * fraction
        deviation = math.sqrt(len(documents) * fraction * (1 - fraction))
        self.assertLessEqual(abs(len(selected) - expected), 6 * deviation)

    def test_rates_zero_one_and_the_full_population_have_exact_boundaries(self) -> None:
        label, document = "corpus-2099-01-03", "fictitious-document"
        half = sample.SAMPLE_FILL_MAX // 2
        for value, rate, expected in (
            (0, 0, False),
            (0, 1, True),
            ((1 << 64) - 1, 1, False),
            ((1 << 64) - 1, sample.SAMPLE_FILL_MAX, True),
            ((1 << 63) - 1, half, True),
            (1 << 63, half, False),
        ):
            with self.subTest(value=value, rate=rate):
                digest = value.to_bytes(8, "big") + b"\xff" * 24
                with patch.object(sample.hashlib, "sha256", return_value=SimpleNamespace(digest=lambda digest=digest: digest)):
                    self.assertIs(sample.in_fill(label, document, rate), expected)
        for number in range(128):
            self.assertFalse(sample.in_fill(label, str(number), 0))
            self.assertTrue(sample.in_fill(label, str(number), sample.SAMPLE_FILL_MAX))

    def test_source_id_and_sample_name_have_the_shared_store_grammar(self) -> None:
        self.assertEqual(sample.source_id("fictitious-source", 123), "fictitious-source:opinion:123")
        name = "caselaw__corpus-2099-01-03__fictitious-space"
        selected = sample.sample_name(name)
        self.assertEqual(selected, name + "__sample")
        self.assertRegex(selected, "^" + GENERATION_PATTERN + "$")

    def test_hash_uses_the_base_seed_and_rate_without_the_reporting_version(self) -> None:
        base, label, rate = "a" * 64, "corpus-2099-01-03", 321
        expected = hashlib.sha256(f"{base}\nsample\n{label}\n{rate}".encode()).hexdigest()
        digest = sample.sample_inputs_hash(base, label, rate)
        self.assertEqual(digest, expected)
        self.assertEqual(digest, sample.sample_inputs_hash(base, label, rate))
        for changed in (("b" * 64, label, rate), (base, "corpus-2099-01-04", rate), (base, label, rate + 1)):
            with self.subTest(changed=changed):
                self.assertNotEqual(digest, sample.sample_inputs_hash(*changed))
        inputs = sample.SampleInputs((), rate, "eval-v999")
        self.assertEqual(sample.arguments(inputs), sample.arguments(replace(inputs, set_version="eval-v1000")))


class Arguments(unittest.TestCase):
    def setUp(self) -> None:
        self.inputs = sample.SampleInputs((
            sample.GoldDocument("fictitious:opinion:1", "a" * 64),
            sample.GoldDocument("fictitious:opinion:2", None),
        ), 321, "eval-v999")

    def test_frozen_inputs_serialize_to_valid_arguments_without_the_version(self) -> None:
        value = sample.arguments(self.inputs)
        self.assertEqual(value, {
            "gold": [{"source_id": document.source_id, "sha256": document.sha256} for document in self.inputs.gold],
            "fill_per_million": self.inputs.fill_per_million,
        })
        self.assertEqual(sample.validate(value), ())
        self.assertNotIn("set_version", value)
        self.assertNotIn(self.inputs.set_version, repr(value))
        for target, field, changed in ((self.inputs, "fill_per_million", 999), (self.inputs.gold[0], "source_id", "other")):
            with self.subTest(field=field), self.assertRaises(FrozenInstanceError):
                setattr(target, field, changed)

    def test_empty_gold_nullable_hash_and_source_id_boundaries_are_valid(self) -> None:
        for gold in ([], [{"source_id": "a", "sha256": None}],
                     [{"source_id": "a" * sample.SOURCE_ID_MAX_LENGTH, "sha256": "f" * 64}],
                     [{"source_id": "Fictitious_./:-9", "sha256": None}]):
            for rate in (1, sample.SAMPLE_FILL_MAX):
                with self.subTest(gold=gold, rate=rate):
                    self.assertEqual(sample.validate({"gold": gold, "fill_per_million": rate}), ())

    def test_gold_count_accepts_the_bound_and_refuses_the_next_entry(self) -> None:
        document = {"source_id": "fictitious:opinion:1", "sha256": None}
        self.assertEqual(sample.validate({"gold": [document] * sample.GOLD_MAX, "fill_per_million": 1}), ())
        self.assertEqual(sample.validate({"gold": [document] * (sample.GOLD_MAX + 1), "fill_per_million": 1}), ("gold",))

    def test_invalid_top_level_shapes_and_gold_entries_name_only_the_field(self) -> None:
        valid = sample.arguments(self.inputs)
        for value, field in (
            (None, "sample"), ([], "sample"), ("FICTITIOUS_PRIVATE_SENTINEL", "sample"),
            ({**valid, "extra": "FICTITIOUS_PRIVATE_SENTINEL"}, "keys"),
            ({"gold": []}, "keys"), ({"fill_per_million": 1}, "keys"),
            ({**valid, "gold": ()}, "gold"), ({**valid, "gold": None}, "gold"),
            ({**valid, "gold": [None]}, "gold"),
            ({**valid, "gold": [{"source_id": "fictitious"}]}, "gold"),
            ({**valid, "gold": [{"source_id": "fictitious", "sha256": None, "extra": "FICTITIOUS_PRIVATE_SENTINEL"}]}, "gold"),
        ):
            with self.subTest(field=field, value=value):
                findings = sample.validate(value)
                self.assertIn(field, findings)
                self.assertNotIn("FICTITIOUS_PRIVATE_SENTINEL", repr(findings))

    def test_invalid_ids_hashes_and_rates_are_refused_without_echoing_values(self) -> None:
        for identifier in (None, 123, "", "a" * (sample.SOURCE_ID_MAX_LENGTH + 1),
                           "FICTITIOUS PRIVATE SENTINEL", "line\n", "fictitious☃", "fictitious?query"):
            with self.subTest(identifier=identifier):
                self.assertEqual(sample.validate({"gold": [{"source_id": identifier, "sha256": None}],
                                                  "fill_per_million": 1}), ("source_id",))
        for digest in (123, "", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "FICTITIOUS_PRIVATE_SENTINEL"):
            with self.subTest(digest=digest):
                self.assertEqual(sample.validate({"gold": [{"source_id": "fictitious", "sha256": digest}],
                                                  "fill_per_million": 1}), ("sha256",))
        for rate in (0, -1, sample.SAMPLE_FILL_MAX + 1, True, False, 1.0, "1", None):
            with self.subTest(rate=rate):
                self.assertEqual(sample.validate({"gold": [], "fill_per_million": rate}), ("fill_per_million",))

    def test_shared_module_imports_only_the_standard_library(self) -> None:
        tree = ast.parse(Path(sample.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                targets = [node.module or ""]
            else:
                continue
            for target in targets:
                with self.subTest(module=target):
                    self.assertIn(target.partition(".")[0], sys.stdlib_module_names)
