"""Contract tests for the committed judgments file and its append-only pins."""

from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final
from unittest import TestCase

from test_judgments_queries import QUERIES_PATH, _parse_lines

from gideon.evaluation import evalset, judgments
from tools.exportboundary import absent_from_export, in_export_tree

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
JUDGMENTS_PATH: Final[Path] = judgments.path_for(evalset.SET_ROOT)
# Append each intake's summary pair after the existing pins.
PINNED_PREFIXES: Final[tuple[tuple[int, str], ...]] = (
    # First pilot manifest, built 2026-10-15.
    (20, "d3f7c6bb7538982331d0601ce46bcc9899cdac9403314d26f5127d33c51d4a2b"),
    # Second pilot manifest, built 2026-10-16.
    (80, "9cbd6e6dc6811875efc42ed9b03f03363c383d08bbc2b2191343ddbd08cd9dbd"),
    # Main pool manifest, built 2026-10-20.
    (1099, "48fdf9a83f4299506ba5e2c6bae55b735e7ed8656b566a0d9e7a1d5f660ed268"),
    # Top-up pool manifest, built 2026-10-20.
    (1169, "4d29ac5aa793960a23ad999631db516fdd8eabd5572d049fef9cdb9e79b91900"),
)


def _check_prefixes(data: bytes) -> list[str]:
    if not PINNED_PREFIXES:
        return ["line 0 document: no prefix pins"]
    raw_lines = data.splitlines(keepends=True)
    line_count = data.count(b"\n")
    findings: list[str] = []
    previous_count = 0
    for count, expected_digest in PINNED_PREFIXES:
        if count <= previous_count:
            findings.append(f"line 0 document: prefix count {count} is not strictly rising")
        if count > line_count:
            findings.append(f"line 0 document: prefix count {count} exceeds line count")
        elif hashlib.sha256(b"".join(raw_lines[:count])).hexdigest() != expected_digest:
            findings.append(f"line 0 document: prefix digest at count {count}")
        previous_count = count
    if previous_count != line_count:
        findings.append("line 0 document: line count does not equal the last prefix")
    return findings


class JudgmentsContract(TestCase):
    """Check the file's bytes and records with findings limited to line and rule."""

    def setUp(self) -> None:
        # Skipped only in an export: a full checkout that lost the file fails.
        if in_export_tree(ROOT):
            self.skipTest("judgments file is absent from this exported tree")
        self.path = ROOT / JUDGMENTS_PATH
        self.assertTrue(self.path.is_file(), "judgments file is missing from a full checkout")

    def _located_records(self) -> tuple[judgments.LocatedJudgment, ...]:
        parsed = judgments.parse_bytes(self.path.read_bytes())
        findings = [f"line {item.line}: {item.rule}" for item in parsed.findings]
        self.assertEqual(findings, [])
        return parsed.records

    def test_every_committed_line_is_pinned(self) -> None:
        self.assertEqual(_check_prefixes(self.path.read_bytes()), [])

    def test_prefix_check_detects_an_edit_to_one_line(self) -> None:
        with TemporaryDirectory() as directory:
            copy_path = Path(directory) / self.path.name
            # One leading space changes the first line's bytes and preserves its JSON.
            copy_path.write_bytes(b" " + self.path.read_bytes())
            self.assertTrue(judgments.read(copy_path).ok)
            findings = _check_prefixes(copy_path.read_bytes())
        self.assertEqual(
            findings,
            [f"line 0 document: prefix digest at count {count}" for count, _ in PINNED_PREFIXES],
        )

    def test_file_reads_cleanly(self) -> None:
        result = judgments.read(self.path)
        findings = [f"line {item.line}: {item.rule}" for item in result.findings]
        self.assertTrue(result.ok, "\n".join(findings))

    def test_every_second_assessment_has_a_primary(self) -> None:
        records = self._located_records()
        primary = {
            (located.record.query_id, judgments.coordinates(located.record))
            for located in records
            if located.record.assessment == "primary"
        }
        findings = [
            f"line {located.line}: second assessment has no primary"
            for located in records
            if located.record.assessment == "second"
            and (located.record.query_id, judgments.coordinates(located.record)) not in primary
        ]
        self.assertEqual(findings, [])

    def test_every_query_id_is_in_the_queries_file(self) -> None:
        if absent_from_export(QUERIES_PATH, ROOT):
            self.skipTest("queries file is absent from this exported tree")
        queries, query_findings = _parse_lines((ROOT / QUERIES_PATH).read_bytes())
        self.assertEqual(query_findings, [])
        query_ids = {
            query_id
            for query in queries
            if isinstance(query_id := query.record.get("id"), str)
        }
        findings = [
            f"line {located.line}: query_id is missing from the queries file"
            for located in self._located_records()
            if located.record.query_id not in query_ids
        ]
        self.assertEqual(findings, [])

    def test_every_grader_has_an_attorney_role(self) -> None:
        findings = [
            f"line {located.line}: grader role"
            for located in self._located_records()
            if judgments.ATTORNEY_ROLE_PATTERN.fullmatch(located.record.grader) is None
        ]
        self.assertEqual(findings, [])
