"""Contracts for committed count rules and their frozen public slice."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import cast

from gideon.evaluation.countgates_slice import RULES
from gideon.evaluation.evalset import (
    SET_ROOT,
    SHAPE_REGISTRY,
    LoadedSet,
    load_set,
    select_cases,
)
from tools.exportboundary import is_excluded

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = SET_ROOT / "build-gates/count-gates.jsonl"
IDS_PATH = SET_ROOT / "slices/count-gates/rules.ids"


class CountGatesSet(unittest.TestCase):
    def test_cases_and_list_have_the_same_three_ids_in_order_and_known_series(self) -> None:
        cases = [json.loads(line) for line in (ROOT / CASES_PATH).read_text().splitlines()]
        ids_text = (ROOT / IDS_PATH).read_text()
        ids = tuple(case["id"] for case in cases)
        self.assertEqual(len(cases), 3)
        self.assertEqual(tuple(ids_text.splitlines()), ids)
        self.assertTrue(ids_text.endswith("\n"))
        self.assertEqual(tuple(identifier.rsplit("-", 1)[0] for identifier in ids), tuple(RULES))
        shape = SHAPE_REGISTRY[("build-gates", "count-gates")]
        for case in cases:
            with self.subTest(case_id=case["id"]):
                self.assertEqual(tuple(case), shape.base_keys)
                self.assertEqual(case["labels"], ["spec"])
                self.assertEqual(case["cluster_id"], case["id"])
                self.assertTrue(case["rule"])
                self.assertEqual(case["rule"], case["rule"].strip())
                self.assertEqual(case["rule"].splitlines(), [case["rule"]])
                self.assertIsNotNone(shape.id_pattern.fullmatch(case["id"]))
                self.assertEqual(tuple(case["review"]), shape.review_keys)

    def test_set_loads_and_counts_every_rule_in_development_and_export(self) -> None:
        for path in (CASES_PATH, IDS_PATH):
            self.assertFalse(is_excluded(path.as_posix()))
        with tempfile.TemporaryDirectory() as directory:
            export_root = Path(directory)
            for source in (ROOT / SET_ROOT).rglob("*"):
                relative = source.relative_to(ROOT)
                if source.is_file() and not is_excluded(relative.as_posix()):
                    destination = export_root / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
            for checkout in (ROOT, export_root):
                with self.subTest(checkout=checkout):
                    result = load_set(checkout / SET_ROOT)
                    self.assertTrue(result.ok, result.findings)
                    loaded = cast(LoadedSet, result.loaded)
                    selection = select_cases(loaded, "count-gates")
                    ids = tuple((checkout / IDS_PATH).read_text().splitlines())
                    self.assertEqual(selection.counted, tuple(sorted(ids)))
                    self.assertEqual(selection.unsigned, ())
                    self.assertFalse(selection.takes_signoff)
