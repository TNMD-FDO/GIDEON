"""Contract tests for the committed research-qa harvest cases."""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path
from typing import Final, cast

from gideon.evaluation import signoffs
from gideon.evaluation.evalset import SET_ROOT, load_set
from gideon.host.courts import load_court_map
from tools.exportboundary import absent_from_export

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
CASES_PATH: Final[Path] = Path("eval/sets/eval-v1/research-qa/harvest.jsonl")
QUERIES_PATH: Final[Path] = Path("eval/sets/eval-v1/judgments/queries.jsonl")
README_PATH: Final[Path] = Path("eval/sets/eval-v1/research-qa/README.md")
PINNED_PREFIXES: Final[tuple[tuple[int, str], ...]] = (
    (38, "5f2844da57a4bac19cef177cc5f362169f9f307234c5169107724c9507c82df7"),
)
EXPECTED_MAPPING: Final[tuple[tuple[str, str, str], ...]] = (
    ("research-qa-001", "HARV-001", "synthesis"),
    ("research-qa-002", "HARV-007", "edition"),
    ("research-qa-003", "HARV-008", "synthesis"),
    ("research-qa-004", "HARV-009", "synthesis"),
    ("research-qa-005", "HARV-011", "synthesis"),
    ("research-qa-006", "HARV-012", "synthesis"),
    ("research-qa-007", "HARV-015", "lookup"),
    ("research-qa-008", "HARV-016", "retrieval"),
    ("research-qa-009", "HARV-020", "synthesis"),
    ("research-qa-010", "HARV-021", "synthesis"),
    ("research-qa-011", "HARV-022", "retrieval"),
    ("research-qa-012", "HARV-023", "lookup"),
    ("research-qa-013", "HARV-024", "lookup"),
    ("research-qa-014", "HARV-025", "retrieval"),
    ("research-qa-015", "HARV-026", "retrieval"),
    ("research-qa-016", "HARV-027", "lookup"),
    ("research-qa-017", "HARV-028", "retrieval"),
    ("research-qa-018", "HARV-029", "retrieval"),
    ("research-qa-019", "HARV-030", "synthesis"),
    ("research-qa-020", "HARV-031", "retrieval"),
    ("research-qa-021", "HARV-032", "retrieval"),
    ("research-qa-022", "HARV-033", "retrieval"),
    ("research-qa-023", "HARV-034", "retrieval"),
    ("research-qa-024", "HARV-035", "synthesis"),
    ("research-qa-025", "HARV-036", "retrieval"),
    ("research-qa-026", "HARV-037", "synthesis"),
    ("research-qa-027", "HARV-038", "retrieval"),
    ("research-qa-028", "HARV-039", "retrieval"),
    ("research-qa-029", "HARV-040", "retrieval"),
    ("research-qa-030", "HARV-041", "retrieval"),
    ("research-qa-031", "HARV-042", "retrieval"),
    ("research-qa-032", "HARV-043", "retrieval"),
    ("research-qa-033", "HARV-044", "retrieval"),
    ("research-qa-034", "HARV-045", "retrieval"),
    ("research-qa-035", "HARV-046", "synthesis"),
    ("research-qa-036", "HARV-047", "retrieval"),
    ("research-qa-037", "HARV-048", "retrieval"),
    ("research-qa-038", "HARV-049", "retrieval"),
)


def _read_lines(path: Path) -> tuple[dict[str, object], ...]:
    values: list[dict[str, object]] = []
    for raw in path.read_bytes().splitlines():
        value = json.loads(raw)
        if isinstance(value, dict):
            values.append(cast(dict[str, object], value))
    return tuple(values)


class ResearchQAContract(unittest.TestCase):
    """The committed cases remain the reviewed harvest rows, never prose fixtures."""

    def setUp(self) -> None:
        if absent_from_export(CASES_PATH, ROOT):
            self.skipTest("research-qa cases are absent from this exported tree")
        self.case_data = (ROOT / CASES_PATH).read_bytes()
        self.cases = _read_lines(ROOT / CASES_PATH)

    def _queries(self) -> tuple[dict[str, object], ...]:
        """The judgments queries, skipping the calling case where they left."""

        if absent_from_export(QUERIES_PATH, ROOT):
            self.skipTest("judgments queries are absent from this exported tree")
        return _read_lines(ROOT / QUERIES_PATH)

    def test_every_committed_line_is_pinned(self) -> None:
        raw_lines = self.case_data.splitlines(keepends=True)
        self.assertTrue(PINNED_PREFIXES)
        for count, expected_digest in PINNED_PREFIXES:
            actual_digest = hashlib.sha256(b"".join(raw_lines[:count])).hexdigest()
            self.assertEqual(actual_digest, expected_digest)
        self.assertEqual(len(raw_lines), PINNED_PREFIXES[-1][0])

    def test_ids_are_one_unbroken_series(self) -> None:
        expected = tuple(f"research-qa-{index:03d}" for index in range(1, 39))
        actual = tuple(value.get("id") for value in self.cases)
        self.assertEqual(actual, expected)

    def test_seeds_equal_the_judgments_harvest_seeds_in_order(self) -> None:
        query_seeds: list[object] = []
        for value in self._queries():
            labels = value.get("labels")
            if isinstance(labels, list) and labels and labels[0] == "harvest":
                query_seeds.append(value.get("seed"))
        case_seeds = tuple(value.get("seed") for value in self.cases)
        self.assertEqual(case_seeds, tuple(query_seeds))

    def test_case_id_seed_category_mapping_is_the_plan_table(self) -> None:
        actual = tuple(
            (value.get("id"), value.get("seed"), value.get("category"))
            for value in self.cases
        )
        self.assertEqual(actual, EXPECTED_MAPPING)

    def test_each_case_matches_its_judgment_query_reviewed_fields(self) -> None:
        queries_by_seed = {
            value["seed"]: value
            for value in self._queries()
            if isinstance(value.get("seed"), str)
        }
        cases_by_seed = {
            value["seed"]: value
            for value in self.cases
            if isinstance(value.get("seed"), str)
        }
        fields = ("question", "labels", "jurisdiction", "reference_date", "cluster_id", "notes")
        for _case_id, seed, _category in EXPECTED_MAPPING:
            with self.subTest(seed=seed):
                query = queries_by_seed[seed]
                case = cases_by_seed[seed]
                actual = tuple(case.get(field) for field in fields)
                expected = tuple(query.get(field) for field in fields)
                self.assertTrue(actual == expected, seed)
                query_review = query.get("review")
                case_review = case.get("review")
                self.assertIsInstance(query_review, dict)
                self.assertIsInstance(case_review, dict)
                assert isinstance(query_review, dict) and isinstance(case_review, dict)
                self.assertTrue(
                    case_review.get("accepted_flags") == query_review.get("accepted_flags"),
                    seed,
                )

    def test_reviews_are_the_maintainer_read(self) -> None:
        actual: list[tuple[object, object]] = []
        for value in self.cases:
            review = value.get("review")
            if isinstance(review, dict):
                actual.append((review.get("by"), review.get("on")))
            else:
                actual.append((None, None))
        self.assertEqual(tuple(actual), (("CSA-1", "2026-09-21"),) * 38)

    def test_category_counts_are_derived_from_the_readme(self) -> None:
        counts: dict[str, int] = {}
        for _case_id, _seed, category in EXPECTED_MAPPING:
            counts[category] = counts.get(category, 0) + 1
        readme = (ROOT / README_PATH).read_text(encoding="utf-8")
        stated = {
            category: int(count)
            for category, count in re.findall(r"\| `(retrieval|synthesis|lookup|edition)` \| (\d+) \|", readme)
        }
        self.assertEqual(stated, counts)

    def test_every_case_is_unsigned_and_no_signoffs_file_is_committed(self) -> None:
        court_map = load_court_map(ROOT / "courts.yaml").court_map
        assert court_map is not None
        result = load_set(ROOT / SET_ROOT, court_map.courts)
        self.assertTrue(result.ok, result.findings)
        assert result.loaded is not None
        case_ids = frozenset(value.get("id") for value in self.cases)
        self.assertEqual(result.loaded.unsigned_ids, case_ids)
        self.assertEqual(result.loaded.signoffs, ())
        self.assertFalse((ROOT / SET_ROOT / signoffs.SIGNOFFS_PATH).exists())


if __name__ == "__main__":
    unittest.main()
