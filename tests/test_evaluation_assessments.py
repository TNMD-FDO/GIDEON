"""The model assessor's content-free grades file and its agreement with attorneys."""

import hashlib
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import TestCase

from gideon.evaluation.assessments import (
    ASSESSMENT_KEYS,
    ASSESSMENTS_FIX,
    ASSESSMENTS_LABEL,
    Assessment,
    AssessmentAgreement,
    AssessmentReadResult,
    agreement,
    model_identifier,
    parse_model_identifier,
    read,
    serialize,
    validate_line,
)
from gideon.evaluation.judgments import KAPPA_NO_PAIRS, KAPPA_NO_VARIATION
from gideon.evaluation.rankmetrics import RELEVANT_GRADE

FIXTURE_REPOSITORY = "example/fictitious-assessor"
FIXTURE_REVISION = "a" * 40
FIXTURE_MODEL = f"{FIXTURE_REPOSITORY}@{FIXTURE_REVISION}"
FIXTURE_PROMPT = "fictitious-passage-relevance@1"
QUERY = "judgments-001"
OTHER_QUERY = "judgments-002"
INACTIVE_QUERY = "judgments-999"
RECORD = Assessment(
    QUERY, "fictional/source-1", "b" * 64, 0, 4, RELEVANT_GRADE, FIXTURE_PROMPT, FIXTURE_MODEL,
)


def _rules(result: AssessmentReadResult) -> tuple[tuple[int | None, str], ...]:
    return tuple((finding.line, finding.rule) for finding in result.findings)


class AssessmentsFile(TestCase):
    """The assessments wire format carries coordinates, grades, and provenance."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.matter_dir = Path(directory.name) / "matter-fictional-client-v-fictional-defendant"
        self.matter_dir.mkdir()
        self.path = self.matter_dir / "fictional-grades.jsonl"

    def read_bytes(self, data: bytes) -> AssessmentReadResult:
        self.path.write_bytes(data)
        return read(self.path, (QUERY, OTHER_QUERY))

    def test_closed_ordered_keys_reject_text_reason_and_duplicate_keys(self) -> None:
        keys = ("query_id", "source_id", "sha256", "start", "end", "grade", "prompt", "model")
        value = json.loads(serialize(RECORD))
        self.assertEqual(ASSESSMENT_KEYS, keys)
        self.assertEqual(tuple(value), keys)
        for changed in (
            dict(reversed(tuple(value.items()))),
            {key: item for key, item in value.items() if key != "grade"},
            {**value, "text": "Fictitious passage never printed"},
            {**value, "reason": "Fictitious reason never printed"},
        ):
            with self.subTest(keys=tuple(changed)):
                result = self.read_bytes((json.dumps(changed) + "\n").encode())
                self.assertFalse(result.ok)
                self.assertIn((1, "keys or order"), _rules(result))

        duplicate = serialize(RECORD).replace('"grade":', '"grade": 0, "grade":')
        self.assertEqual(
            _rules(self.read_bytes(duplicate.encode())),
            ((1, "line is not one JSON object with unique keys"),),
        )

    def test_each_invalid_field_is_reported_by_rule(self) -> None:
        cases = (
            ("query_id pattern", {"query_id": "fictional query"}),
            ("source_id", {"source_id": "fictional source with spaces"}),
            ("sha256", {"sha256": "B" * 64}),
            ("start", {"start": True}),
            ("end", {"end": 0}),
            ("coordinates", {"start": RECORD.end}),
            ("grade", {"grade": -1}),
            ("grade", {"grade": 4}),
            ("grade", {"grade": True}),
            ("grade", {"grade": 1.0}),
            ("prompt pattern", {"prompt": "Fictitious-Prompt@1"}),
            ("prompt pattern", {"prompt": "fictitious-prompt@0"}),
            ("prompt pattern", {"prompt": "fictitious-prompt@01"}),
            ("model identifier", {"model": f"example@{FIXTURE_REVISION}"}),
            ("model identifier", {"model": f"{FIXTURE_REPOSITORY}@{'A' * 40}"}),
            ("model identifier", {"model": f"{FIXTURE_REPOSITORY}@{FIXTURE_REVISION[:-1]}"}),
            ("model identifier", {"model": "a fictitious free-text response"}),
            ("model identifier", {"model": f"Invented Person v. Imaginary County@{FIXTURE_REVISION}"}),
            ("model identifier", {"model": f"{FIXTURE_MODEL}@{FIXTURE_REVISION}"}),
        )
        for rule, override in cases:
            with self.subTest(override=override):
                value = json.loads(serialize(RECORD)) | override
                result = self.read_bytes((json.dumps(value) + "\n").encode())
                self.assertEqual(_rules(result), ((1, rule),))
                if rule == "model identifier":
                    self.assertIsNone(parse_model_identifier(value["model"]))

    def test_model_identifier_builds_and_parses_the_lock_grammar(self) -> None:
        built = model_identifier(FIXTURE_REPOSITORY, FIXTURE_REVISION)
        self.assertEqual(built, FIXTURE_MODEL)
        self.assertEqual(parse_model_identifier(built), (FIXTURE_REPOSITORY, FIXTURE_REVISION))
        for parts in (("fictional repository", FIXTURE_REVISION), (FIXTURE_REPOSITORY, "main")):
            with self.subTest(parts=parts), self.assertRaises(ValueError):
                model_identifier(*parts)

    def test_each_half_of_the_assessor_identifier_must_match(self) -> None:
        first = serialize(RECORD)
        for other in (
            replace(RECORD, query_id=OTHER_QUERY, prompt="fictitious-passage-relevance@2"),
            replace(RECORD, query_id=OTHER_QUERY, model=f"{FIXTURE_REPOSITORY}@{'c' * 40}"),
        ):
            with self.subTest(other=other):
                result = self.read_bytes((first + serialize(other)).encode())
                self.assertEqual(
                    _rules(result), ((2, "prompt and model must match the first assessment"),),
                )

    def test_serialize_read_serialize_preserves_bytes_and_digest(self) -> None:
        records = (
            RECORD,
            replace(RECORD, start=4, end=8, grade=0),
            replace(RECORD, query_id=OTHER_QUERY, grade=3),
        )
        data = "".join(map(serialize, records)).encode("utf-8")
        result = self.read_bytes(data)
        self.assertTrue(result.ok, result.findings)
        self.assertEqual(result.sha256, hashlib.sha256(data).hexdigest())
        source = result.assessments
        assert source is not None
        round_trip = tuple(
            Assessment(query_id, *coordinate, grade, source.prompt, source.model)
            for query_id, grades in source.grades.items()
            for coordinate, grade in grades.items()
        )
        self.assertEqual(round_trip, records)
        self.assertEqual("".join(map(serialize, round_trip)).encode("utf-8"), data)

    def test_reports_all_findings_without_values_or_paths(self) -> None:
        sentinel = "Fictitious client passage must remain private"
        malformed = json.loads(serialize(RECORD)) | {"source_id": sentinel, "reason": sentinel}
        data = (
            serialize(RECORD)
            + json.dumps(malformed) + "\n"
            + serialize(replace(RECORD, grade=0))
            + serialize(replace(RECORD, query_id=INACTIVE_QUERY))
        ).encode() + b"\xff\n[]"
        result = self.read_bytes(data)
        self.assertFalse(result.ok)
        self.assertIsNone(result.assessments)
        self.assertIsNone(result.sha256)
        self.assertEqual(_rules(result), (
            (0, "file has no final newline"),
            (2, "keys or order"), (2, "source_id"),
            (3, "coordinates appear more than once for query_id"),
            (4, "query_id is not allowed"),
            (5, "line is not UTF-8"),
            (6, "line is not one JSON object"),
        ))

        empty = self.read_bytes(b"")
        missing = read(self.matter_dir / "missing.jsonl", (QUERY,))
        self.assertEqual(_rules(empty), ((0, "file is empty"),))
        self.assertEqual(_rules(missing), ((0, "file cannot be read"),))
        self.assertEqual(ASSESSMENTS_LABEL, "assessments")
        for finding in result.findings + empty.findings + missing.findings:
            self.assertEqual(finding.file, ASSESSMENTS_LABEL)
            self.assertIsNone(finding.id)
            self.assertEqual(finding.fix, ASSESSMENTS_FIX)
            self.assertIn(f":line {finding.line}:", finding.text())
            for hidden in (sentinel, QUERY, INACTIVE_QUERY, FIXTURE_MODEL, FIXTURE_PROMPT,
                           self.matter_dir.name, self.path.name, "missing.jsonl"):
                self.assertNotIn(hidden, finding.text())

    def test_line_rules_apply_to_a_parsed_object(self) -> None:
        value = json.loads(serialize(RECORD))
        self.assertEqual(validate_line(value), ())
        findings = validate_line(value | {"end": False}, line=7)
        self.assertEqual([(item.line, item.rule) for item in findings], [(7, "end")])


class AssessorAgreement(TestCase):
    """The assessor's disagreement is computed on both grading scales."""

    def test_hand_computed_tables(self) -> None:
        low, high, above = RELEVANT_GRADE - 1, RELEVANT_GRADE, RELEVANT_GRADE + 1
        # Both tables have exact observed agreement 1/2 and expected 3/8.
        # A has perfect collapsed agreement. B has observed 3/4 and expected 1/2.
        cases = (
            (((low, low), (high, high), (high, above), (above, high)), 0, 1.0),
            (((low, low), (high, high), (low, high), (high, above)), 1, 1 / 2),
        )
        for pairs, relevant_differing, relevant_kappa in cases:
            with self.subTest(pairs=pairs):
                result = agreement(iter(pairs))
                self.assertEqual(result.pair_count, len(pairs))
                self.assertEqual(result.four_grade_differing, 2)
                self.assertEqual(result.relevant_differing, relevant_differing)
                assert isinstance(result.four_grade_kappa, float)
                assert isinstance(result.relevant_kappa, float)
                self.assertAlmostEqual(result.four_grade_kappa, 1 / 5)
                self.assertAlmostEqual(result.relevant_kappa, relevant_kappa)

    def test_no_pairs_and_no_variation(self) -> None:
        self.assertEqual(agreement(()), AssessmentAgreement(0, 0, 0, KAPPA_NO_PAIRS, KAPPA_NO_PAIRS))
        grade = RELEVANT_GRADE
        self.assertEqual(
            agreement(((grade, grade),) * 3),
            AssessmentAgreement(3, 0, 0, KAPPA_NO_VARIATION, KAPPA_NO_VARIATION),
        )
