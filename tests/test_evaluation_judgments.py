"""Judgments-file shape and agreement contracts for spec §18.4(a)."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from unittest import TestCase

from gideon.evaluation.evalset import ROLE_PATTERN
from gideon.evaluation.judgments import (
    ATTORNEY_ROLE_PATTERN,
    JUDGMENT_KEYS,
    KAPPA_NO_PAIRS,
    KAPPA_NO_VARIATION,
    Judgment,
    JudgmentReadResult,
    agreement,
    path_for,
    read,
    serialize,
)


def _record(
    *,
    query_id: str = "judgments-001",
    source_id: str = "fictional/source-1",
    sha256: str = "a" * 64,
    start: int = 0,
    end: int = 4,
    grade: int = 2,
    grader: str = "CHU-attorney-1",
    assessment: str = "primary",
) -> dict[str, object]:
    values: dict[str, object] = {
        "query_id": query_id,
        "source_id": source_id,
        "sha256": sha256,
        "start": start,
        "end": end,
        "grade": grade,
        "grader": grader,
        "assessment": assessment,
    }
    return {key: values[key] for key in JUDGMENT_KEYS}


def _judgment(
    index: int,
    primary: int,
    second: int,
) -> tuple[Judgment, Judgment]:
    query_id = f"judgments-{index:03d}"
    source_id = f"fictional/source-{index}"
    sha256 = f"{index:064x}"
    coordinate = (query_id, source_id, sha256, index, index + 1)
    return (
        Judgment(*coordinate, primary, "CHU-attorney-1", "primary"),
        Judgment(*coordinate, second, "TRAD-attorney-1", "second"),
    )


def _write_jsonl(root: Path, records: Iterable[Mapping[str, object]], *, final_newline: bool = True) -> Path:
    path = path_for(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(dict(record), ensure_ascii=False) + "\n" for record in records)
    if not final_newline:
        text = text.removesuffix("\n")
    path.write_text(text, encoding="utf-8")
    return path


def _finding_rules(result: JudgmentReadResult) -> tuple[str, ...]:
    return tuple(finding.rule for finding in result.findings)


class Shape(TestCase):
    """The closed line shape and every field rule are content-free."""

    def _read_one(self, record: Mapping[str, object], *, expect_findings: bool = True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v-fictional"
            path = _write_jsonl(root, (record,))
            result = read(path)
            if expect_findings:
                self.assertFalse(result.ok)
            return result

    def test_ordered_keys_are_exact_and_closed(self) -> None:
        base = _record()
        self.assertEqual(
            JUDGMENT_KEYS,
            ("query_id", "source_id", "sha256", "start", "end", "grade", "grader", "assessment"),
        )
        for extra_key in ("text", "question", "quote", "name"):
            with self.subTest(extra_key=extra_key):
                record = dict(base)
                record[extra_key] = "fictional extra"
                result = self._read_one(record)
                self.assertIn("keys or order", _finding_rules(result))

        reordered = {key: base[key] for key in reversed(JUDGMENT_KEYS)}
        result = self._read_one(reordered)
        self.assertIn("keys or order", _finding_rules(result))

    def test_each_field_rule_refuses_invalid_values(self) -> None:
        cases: tuple[tuple[str, object, str], ...] = (
            ("query_id", "fictional-query", "query_id pattern"),
            ("source_id", "fictional source", "source_id"),
            ("source_id", "s" * 201, "source_id"),
            ("sha256", "A" * 64, "sha256"),
            ("start", True, "start"),
            ("end", False, "end"),
            ("start", 4, "coordinates"),
            ("grade", True, "grade"),
            ("grade", 4, "grade"),
            ("grader", "CSA-1", "grader role"),
            ("grader", "CHU-paralegal-2", "grader role"),
            ("assessment", "fictional-reviewer", "assessment"),
        )
        for field, value, expected_rule in cases:
            with self.subTest(field=field, value=value):
                record = _record()
                record[field] = value
                result = self._read_one(record)
                self.assertIn(expected_rule, _finding_rules(result))

    def test_attorney_pattern_is_a_subset_of_loader_roles(self) -> None:
        loader_roles = (
            "CHU-attorney",
            "CHU-investigator",
            "CHU-paralegal",
            "TRAD-attorney",
            "TRAD-investigator",
            "TRAD-legal-assistant",
            "support",
            "CSA",
            "unknown",
        )
        # Every candidate the attorney pattern accepts must be a role id the loader
        # accepts too; the corpus carries the whole role vocabulary and the ordinal
        # edges, so a widened attorney pattern breaks the implication here.
        corpus = [
            f"{prefix}-{ordinal}"
            for prefix in loader_roles
            for ordinal in ("1", "2", "9", "10", "47", "0", "01", "", "1x", "-1")
        ]
        corpus += ["CHU-attorney", "chu-attorney-1", "CHU-attorney-1 ", "XCHU-attorney-1"]
        for candidate in corpus:
            if ATTORNEY_ROLE_PATTERN.fullmatch(candidate) is not None:
                self.assertIsNotNone(
                    ROLE_PATTERN.fullmatch(candidate),
                    f"attorney pattern accepts a non-role: {candidate!r}",
                )
        self.assertTrue(
            any(ATTORNEY_ROLE_PATTERN.fullmatch(candidate) for candidate in corpus),
            "the corpus must exercise the accepting side of the implication",
        )
        for refused in ("CSA-1", "CHU-paralegal-2", "TRAD-investigator-1", "support-3", "unknown-1"):
            with self.subTest(role=refused):
                self.assertIsNotNone(ROLE_PATTERN.fullmatch(refused))
                self.assertIsNone(ATTORNEY_ROLE_PATTERN.fullmatch(refused))

    def test_file_rules_and_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v-fictional"
            triple = _record(assessment="primary")
            second_for_triple = _record(assessment="second")
            result = read(_write_jsonl(root, (triple, second_for_triple)))
            self.assertIn(
                "query-coordinates-grader appears more than once",
                _finding_rules(result),
            )

            primary_one = _record(grader="CHU-attorney-1", assessment="primary")
            primary_two = _record(grader="TRAD-attorney-1", assessment="primary")
            result = read(_write_jsonl(root, (primary_one, primary_two)))
            self.assertIn(
                "assessment appears more than once for coordinates",
                _finding_rules(result),
            )

            result = read(_write_jsonl(root, (_record(),), final_newline=False))
            self.assertIn("file has no final newline", _finding_rules(result))

            empty_path = path_for(root)
            empty_path.write_bytes(b"")
            empty = read(empty_path)
            self.assertTrue(empty.ok)
            self.assertEqual(empty.records, ())

    def test_findings_never_render_input_values(self) -> None:
        # The sentinel carries a space, so it is invalid in every field including
        # source_id; each case must therefore produce a finding to inspect.
        sentinel = "FICTIONAL SENTINEL VALUE"
        for field in JUDGMENT_KEYS:
            with self.subTest(field=field):
                record = _record()
                record[field] = sentinel
                result = self._read_one(record)
                self.assertTrue(result.findings)
                rendered = "\n".join(finding.text() for finding in result.findings)
                self.assertNotIn(sentinel, rendered)
                self.assertNotIn("FICTIONAL", rendered)

    def test_a_line_that_is_not_one_json_object_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v-fictional"
            path = path_for(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('[1, 2]\nnot json at all\n', encoding="utf-8")
            result = read(path)
            self.assertFalse(result.ok)
            self.assertEqual(
                _finding_rules(result),
                ("line is not one JSON object", "line is not one JSON object"),
            )


class Serialization(TestCase):
    """The JSONL serializer and reader preserve the committed bytes."""

    def test_serialize_then_read_is_byte_stable(self) -> None:
        records = tuple(
            Judgment(
                query_id=f"judgments-{index:03d}",
                source_id=f"fictional/source-{index}",
                sha256=f"{index:064x}",
                start=index,
                end=index + 1,
                grade=index % 4,
                grader="CHU-attorney-1",
                assessment="primary",
            )
            for index in range(1, 4)
        )
        data = b"".join(serialize(record).encode("utf-8") for record in records)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v-fictional"
            path = path_for(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            result = read(path)
            self.assertTrue(result.ok)
            self.assertEqual(
                b"".join(serialize(record).encode("utf-8") for record in result.records or ()),
                data,
            )
            first_keys = tuple(json.loads(serialize(records[0])))
            self.assertEqual(first_keys, JUDGMENT_KEYS)


class AgreementTests(TestCase):
    """The pooled four-grade and relevant κ statistics use the stated tables."""

    def _assert_table(
        self,
        pairs: tuple[tuple[int, int], ...],
        four_grade: float,
        relevant: float,
    ) -> None:
        records = tuple(record for index, pair in enumerate(pairs, 1) for record in _judgment(index, *pair))
        result = agreement(records)
        self.assertEqual(result.pair_count, 10)
        self.assertEqual(result.query_count, 10)
        assert isinstance(result.four_grade_kappa, float)
        assert isinstance(result.relevant_kappa, float)
        self.assertAlmostEqual(result.four_grade_kappa, four_grade)
        self.assertAlmostEqual(result.relevant_kappa, relevant)

    def test_table_a_one_step_misses_stay_on_the_same_relevance_side(self) -> None:
        # A: observed = 6/10, expected = (4²+2²+2²+2²)/10² = 7/25;
        # κ = (3/5-7/25)/(1-7/25) = 4/9; collapsed observed = 1, κ = 1.
        pairs = (
            (0, 0),
            (0, 0),
            (0, 0),
            (1, 1),
            (2, 2),
            (3, 3),
            (0, 1),
            (1, 0),
            (2, 3),
            (3, 2),
        )
        self._assert_table(pairs, 4 / 9, 1.0)

    def test_table_b_misses_cross_the_relevance_line(self) -> None:
        # B: observed = 6/10, expected = (4²+2²+2·4+0²)/10² = 7/25;
        # κ = (3/5-7/25)/(1-7/25) = 4/9; collapsed observed = 3/5,
        # expected = 13/25, so κ = (3/5-13/25)/(1-13/25) = 1/6.
        pairs = (
            (0, 0),
            (0, 0),
            (0, 0),
            (0, 0),
            (2, 2),
            (2, 2),
            (1, 2),
            (1, 2),
            (3, 1),
            (3, 1),
        )
        self._assert_table(pairs, 4 / 9, 1 / 6)

    def test_no_pairs_and_no_variation_are_named_states(self) -> None:
        empty = agreement(())
        self.assertEqual(empty.pair_count, 0)
        self.assertEqual(empty.query_count, 0)
        self.assertEqual(empty.four_grade_kappa, KAPPA_NO_PAIRS)
        self.assertEqual(empty.relevant_kappa, KAPPA_NO_PAIRS)

        identical = tuple(
            record
            for index in range(1, 4)
            for record in _judgment(index, 2, 2)
        )
        no_variation = agreement(identical)
        self.assertEqual(no_variation.pair_count, 3)
        self.assertEqual(no_variation.query_count, 3)
        self.assertEqual(no_variation.four_grade_kappa, KAPPA_NO_VARIATION)
        self.assertEqual(no_variation.relevant_kappa, KAPPA_NO_VARIATION)


if __name__ == "__main__":
    import unittest

    unittest.main()
