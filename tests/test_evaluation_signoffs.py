"""Sign-offs-file shape and serialization contracts."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

from gideon.evaluation import signoffs

SENTINEL = "FICTIONAL SIGNED ANSWER SENTINEL"


def _record(
    case_id: str = "research-qa-001",
    *,
    answer: str = "The fictional signed answer.",
    must_cite: tuple[str, ...] = ("fictional/source-1",),
    signed_by: str = "CHU-attorney-1",
    signed_on: str = "2026-09-21",
) -> signoffs.SignOff:
    return signoffs.SignOff(case_id, answer, must_cite, signed_by, signed_on)


def _value(record: signoffs.SignOff) -> dict[str, object]:
    return json.loads(signoffs.serialize(record))


def _write(root: Path, values: tuple[Mapping[str, object], ...], *, final_newline: bool = True) -> Path:
    path = signoffs.path_for(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(dict(value), ensure_ascii=False) + "\n" for value in values)
    if not final_newline:
        text = text.removesuffix("\n")
    path.write_text(text, encoding="utf-8")
    return path


def _rules(result: signoffs.SignOffReadResult) -> tuple[str, ...]:
    return tuple(finding.rule for finding in result.findings)


class Shape(unittest.TestCase):
    """The closed line shape and every field rule are content-free."""

    def _read_one(self, value: object) -> signoffs.SignOffReadResult:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v-fictional"
            path = signoffs.path_for(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value) + "\n", encoding="utf-8")
            result = signoffs.read(path)
        self.assertFalse(result.ok)
        rendered = "\n".join(finding.text() for finding in result.findings)
        self.assertNotIn(SENTINEL, rendered)
        return result

    def test_ordered_keys_are_exact_for_line_expected_and_signed(self) -> None:
        self.assertEqual(signoffs.SIGNOFF_KEYS, ("case_id", "expected", "signed"))
        self.assertEqual(signoffs.EXPECTED_KEYS, ("answer", "must_cite"))
        self.assertEqual(signoffs.SIGNED_KEYS, ("by", "on"))
        base = _value(_record())
        for target, expected_rule in (
            (base, "keys or order"),
            (base["expected"], "expected keys or order"),
            (base["signed"], "signed keys or order"),
        ):
            with self.subTest(expected_rule=expected_rule):
                assert isinstance(target, dict)
                reordered = {key: target[key] for key in reversed(tuple(target))}
                if target is base:
                    value = reordered
                else:
                    value = dict(base)
                    value["expected" if target is base["expected"] else "signed"] = reordered
                self.assertIn(expected_rule, _rules(self._read_one(value)))

        extra = dict(base)
        extra["extra"] = "fictional"
        self.assertIn("keys or order", _rules(self._read_one(extra)))

    def test_each_field_rule_refuses_invalid_values(self) -> None:
        cases: tuple[tuple[str, object, str], ...] = (
            ("case_id", "research-qa-01", "case_id pattern"),
            ("expected", [], "expected mapping"),
            ("answer", "", "expected.answer"),
            ("answer", 3, "expected.answer"),
            ("must_cite", [], "expected.must_cite"),
            ("must_cite", "fictional/source-1", "expected.must_cite"),
            ("must_cite", ["fictional source"], "expected.must_cite"),
            ("signed", [], "signed mapping"),
            ("by", "CSA-1", "signed.by role"),
            ("on", "not-a-date", "signed.on ISO date"),
        )
        for field, value, expected in cases:
            with self.subTest(field=field, value=value):
                record = _value(_record())
                if field in ("answer", "must_cite"):
                    assert isinstance(record["expected"], dict)
                    record["expected"][field] = value
                elif field in ("by", "on"):
                    assert isinstance(record["signed"], dict)
                    record["signed"][field] = value
                else:
                    record[field] = value
                self.assertIn(expected, _rules(self._read_one(record)))

    def test_duplicate_case_id_and_missing_final_newline_are_file_findings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v-fictional"
            first = _value(_record())
            second = _value(_record(answer="Another fictional answer."))
            result = signoffs.read(_write(root, (first, second)))
            self.assertIn("case_id appears more than once", _rules(result))

            result = signoffs.read(_write(root, (first,), final_newline=False))
            self.assertIn("file has no final newline", _rules(result))

    def test_findings_never_render_answer_or_must_cite_values(self) -> None:
        value = _value(_record(answer=SENTINEL, must_cite=(SENTINEL,)))
        value["expected"] = {"answer": SENTINEL, "must_cite": [SENTINEL]}
        result = self._read_one(value)
        rendered = "\n".join(finding.text() for finding in result.findings)
        self.assertNotIn(SENTINEL, rendered)


class Serialization(unittest.TestCase):
    """The serializer, bytes parser, and reader preserve ordered JSONL bytes."""

    def test_serialize_then_read_is_byte_stable(self) -> None:
        records = (_record(), _record("research-qa-002", answer="Another answer."))
        data = b"".join(signoffs.serialize(record).encode("utf-8") for record in records)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v-fictional"
            path = signoffs.path_for(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            result = signoffs.read(path)
            self.assertTrue(result.ok, result.findings)
            assert result.records is not None
            rewritten = b"".join(signoffs.serialize(record).encode("utf-8") for record in result.records)
        self.assertEqual(rewritten, data)

    def test_bytes_parse_equals_read_and_keeps_locations(self) -> None:
        records = (_record(), _record("research-qa-002", answer="Another answer."))
        data = b"".join(signoffs.serialize(record).encode("utf-8") for record in records)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v-fictional"
            path = signoffs.path_for(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            parsed = signoffs.parse_bytes(data, signoffs.SIGNOFFS_PATH.as_posix())
            read_result = signoffs.read(path)
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.findings, read_result.findings)
        assert read_result.records is not None
        self.assertEqual(
            tuple(located.record for located in parsed.records), read_result.records
        )
        self.assertEqual(tuple(located.line for located in parsed.records), (1, 2))

    def test_repr_withholds_answer_but_record_keeps_it(self) -> None:
        record = _record(answer=SENTINEL)
        self.assertEqual(record.answer, SENTINEL)
        self.assertNotIn(SENTINEL, repr(record))
        self.assertIn("<redacted>", repr(record))


if __name__ == "__main__":
    unittest.main()
