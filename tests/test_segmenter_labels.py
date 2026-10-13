"""Human-checked segmenter label contracts."""

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from gideon.evaluation import segmenter_labels as labels

SENTINEL = "FICTIONAL OPINION TEXT THAT MUST NOT ENTER A FINDING"


def _value() -> dict[str, Any]:
    return {
        "case_id": "segmenter-001",
        "text": {"sha256": "a" * 64, "length": 10},
        "sections": [
            {"start": 0, "end": 4, "type": "majority"},
            {"start": 4, "end": 10, "type": "dissent"},
        ],
        "signed": {"by": "CSA-1", "on": "2026-09-01"},
    }


def _bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")


class Shape(unittest.TestCase):
    """A label is a closed, ordered, tiled reading of canonical text."""

    def test_exact_keys_and_valid_roles(self) -> None:
        self.assertEqual(labels.LABEL_KEYS, ("case_id", "text", "sections", "signed"))
        self.assertEqual(labels.TEXT_KEYS, ("sha256", "length"))
        self.assertEqual(labels.SECTION_KEYS, ("start", "end", "type"))
        self.assertEqual(labels.SIGNED_KEYS, ("by", "on"))
        for role in ("CSA-1", "CHU-paralegal-1", "CHU-attorney-1"):
            with self.subTest(role=role):
                value = _value()
                value["signed"]["by"] = role
                self.assertTrue(labels.parse_bytes(_bytes(value)).ok)

    def test_each_field_refuses_without_echoing_values(self) -> None:
        changes = (
            ("keys or order", lambda v: v.update(extra=SENTINEL)),
            ("case_id pattern", lambda v: v.update(case_id="wrong")),
            ("text keys or order", lambda v: v["text"].update(extra=SENTINEL)),
            ("text.sha256", lambda v: v["text"].update(sha256="A" * 64)),
            ("text.length", lambda v: v["text"].update(length=True)),
            ("sections non-empty list", lambda v: v.update(sections=[])),
            ("sections[1] keys or order", lambda v: v["sections"][0].update(extra=SENTINEL)),
            ("sections[1] starts at zero", lambda v: v["sections"][0].update(start=1)),
            ("sections[2] gap", lambda v: v["sections"][1].update(start=5)),
            ("sections[2] overlap or unsorted", lambda v: v["sections"][1].update(start=3)),
            ("sections[2] overlap or unsorted", lambda v: v["sections"][1].update(start=0)),
            ("sections[2] type", lambda v: v["sections"][1].update(type=SENTINEL)),
            ("sections end at text.length", lambda v: v["sections"][1].update(end=9)),
            ("signed keys or order", lambda v: v["signed"].update(extra=SENTINEL)),
            ("signed.by role", lambda v: v["signed"].update(by=SENTINEL)),
            ("signed.on ISO date", lambda v: v["signed"].update(on="yesterday")),
        )
        for rule, change in changes:
            with self.subTest(rule=rule):
                value = _value()
                change(value)
                parsed = labels.parse_bytes(_bytes(value))
                self.assertIn(rule, tuple(finding.rule for finding in parsed.findings))
                self.assertTrue(all(finding.file == labels.LABELS_PATH.as_posix() for finding in parsed.findings))
                self.assertTrue(all(finding.line == 1 for finding in parsed.findings))
                self.assertNotIn(SENTINEL, "\n".join(finding.text() for finding in parsed.findings))

    def test_collects_all_findings_on_one_line(self) -> None:
        value = _value()
        value["text"]["sha256"] = "bad"
        value["sections"][1]["start"] = 5
        value["signed"]["on"] = "bad"
        parsed = labels.parse_bytes(_bytes(value))
        self.assertEqual(
            {finding.rule for finding in parsed.findings},
            {"text.sha256", "sections[2] gap", "signed.on ISO date"},
        )
        self.assertTrue(all(finding.id == "segmenter-001" for finding in parsed.findings))


class File(unittest.TestCase):
    """The file is strict JSONL and preserves labels through serialization."""

    def test_serialize_read_and_repr(self) -> None:
        parsed = labels.parse_bytes(_bytes(_value()))
        self.assertTrue(parsed.ok)
        record = parsed.records[0].record
        self.assertEqual(labels.serialize(record).encode("utf-8"), _bytes(_value()))
        self.assertIn("sections=2", repr(record))
        self.assertNotIn(SENTINEL, repr(record))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            path = labels.path_for(root)
            path.parent.mkdir(parents=True)
            path.write_text(labels.serialize(record), encoding="utf-8")
            read = labels.read(root)
            self.assertTrue(read.ok)
            self.assertEqual(read.records, (record,))

    def test_file_rules_and_duplicate(self) -> None:
        valid = _bytes(_value())
        cases = (
            (valid.rstrip(b"\n"), "file has no final newline"),
            (valid + b"\n", "line is not one JSON object"),
            (b"\xff\n", "file is not UTF-8"),
            (b"null\n", "line is not one JSON object"),
            (valid + valid, "case_id appears more than once"),
        )
        for data, rule in cases:
            with self.subTest(rule=rule):
                parsed = labels.parse_bytes(data)
                self.assertIn(rule, tuple(finding.rule for finding in parsed.findings))
                self.assertTrue(all(finding.fix for finding in parsed.findings))
        with tempfile.TemporaryDirectory() as directory:
            result = labels.read(Path(directory) / "missing.jsonl")
            self.assertEqual(result.findings[0].rule, "file cannot be read")
