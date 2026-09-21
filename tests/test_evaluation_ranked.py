"""The ranked-list file contract for the judgments evaluation door."""

import hashlib
import json
import tempfile
from pathlib import Path
from unittest import TestCase

from gideon.evaluation.ranked import read

QUERY = "judgments-001"
OTHER_QUERY = "judgments-002"
UNKNOWN_QUERY = "judgments-999"
DO_NOT_ECHO = "DO_NOT_ECHO"


def _entry(
    index: int = 0,
    *,
    source_id: str | None = None,
    sha256: str | None = None,
    start: object = 0,
    end: object = 1,
) -> dict[str, object]:
    return {
        "source_id": source_id or f"fictional/source-{index}",
        "sha256": sha256 or f"{index:064x}",
        "start": start,
        "end": end,
    }


def _line(query_id: str, ranked: object, **extra: object) -> bytes:
    value: dict[str, object] = {"query_id": query_id, "ranked": ranked}
    value.update(extra)
    return (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")


def _write(root: Path, data: bytes) -> Path:
    path = root / "ranked.jsonl"
    path.write_bytes(data)
    return path


def _located(findings: object) -> tuple[tuple[int | None, str | None, str], ...]:
    """Each finding as its line, id, and rule.

    ``id`` is always ``None`` here: ``Finding.text`` renders an id in place of
    the line, so a ranked finding carries its query and ordinal in the rule and
    keeps the line, as the pool reader's does.
    """

    assert isinstance(findings, tuple)
    return tuple((finding.line, finding.id, finding.rule) for finding in findings)


class RankedReader(TestCase):
    """The reader accepts the closed ranked-list shape and reports all faults."""

    def test_valid_lists_allow_empty_and_unbounded_rank(self) -> None:
        entries = [_entry(index) for index in range(51)]
        data = _line(QUERY, []) + _line(OTHER_QUERY, entries)
        with tempfile.TemporaryDirectory() as directory:
            path = _write(Path(directory), data)
            result = read(path, (QUERY, OTHER_QUERY))

        self.assertTrue(result.ok, result.findings)
        self.assertIsNotNone(result.ranked)
        assert result.ranked is not None
        self.assertEqual(result.ranked[QUERY], ())
        self.assertEqual(len(result.ranked[OTHER_QUERY]), 51)
        self.assertEqual(result.sha256, hashlib.sha256(data).hexdigest())

    def test_key_order_is_a_finding(self) -> None:
        data = json.dumps(
            {"ranked": [], "query_id": QUERY}, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        with tempfile.TemporaryDirectory() as directory:
            result = read(_write(Path(directory), data), (QUERY,))

        self.assertEqual(
            _located(result.findings), ((1, None, f"query {QUERY}: keys or order"),)
        )
        # The line survives the location, which an id in Finding.id would hide.
        self.assertIn(":line 1:", result.findings[0].text())

    def test_extra_text_key_is_named_by_the_closed_key_case_without_echoing_value(self) -> None:
        data = _line(QUERY, [], text=DO_NOT_ECHO)
        with tempfile.TemporaryDirectory() as directory:
            result = read(_write(Path(directory), data), (QUERY,))

        self.assertEqual(
            _located(result.findings), ((1, None, f"query {QUERY}: keys or order"),)
        )
        self.assertNotIn(DO_NOT_ECHO, "\n".join(finding.text() for finding in result.findings))

    def test_repeated_query_is_a_finding(self) -> None:
        data = _line(QUERY, []) + _line(QUERY, [])
        with tempfile.TemporaryDirectory() as directory:
            result = read(_write(Path(directory), data), (QUERY,))

        self.assertEqual(
            _located(result.findings),
            ((2, None, f"query {QUERY}: query_id appears more than once"),),
        )

    def test_query_outside_allowed_set_is_a_finding(self) -> None:
        data = _line(UNKNOWN_QUERY, [])
        with tempfile.TemporaryDirectory() as directory:
            result = read(_write(Path(directory), data), (QUERY,))

        self.assertEqual(
            _located(result.findings),
            ((1, None, f"query {UNKNOWN_QUERY}: query_id is not allowed"),),
        )

    def test_each_coordinate_rule_is_reported_at_its_entry_ordinal(self) -> None:
        cases = (
            ("source_id", _entry(source_id="not a source")),
            ("sha256", _entry(sha256="A" * 64)),
            ("start", _entry(start=True)),
            ("end", _entry(end=0)),
            ("coordinates", _entry(start=1, end=1)),
        )
        for rule, entry in cases:
            with self.subTest(rule=rule):
                data = _line(QUERY, [entry])
                with tempfile.TemporaryDirectory() as directory:
                    result = read(_write(Path(directory), data), (QUERY,))

                self.assertEqual(
                    _located(result.findings),
                    ((1, None, f"query {QUERY} ranked 1: {rule}"),),
                )

    def test_repeated_coordinates_within_a_query_are_a_finding(self) -> None:
        entry = _entry()
        data = _line(QUERY, [entry, entry])
        with tempfile.TemporaryDirectory() as directory:
            result = read(_write(Path(directory), data), (QUERY,))

        self.assertEqual(
            _located(result.findings),
            ((1, None, f"query {QUERY} ranked 2: coordinates appear more than once"),),
        )

    def test_reports_every_finding_at_once_without_echoing_payload(self) -> None:
        data = (
            _line(QUERY, [_entry(source_id="not a source")], text=DO_NOT_ECHO)
            + _line(QUERY, [])
            + _line(UNKNOWN_QUERY, [])
        )
        with tempfile.TemporaryDirectory() as directory:
            result = read(_write(Path(directory), data), (QUERY,))

        self.assertEqual(
            _located(result.findings),
            (
                (1, None, f"query {QUERY} ranked 1: source_id"),
                (1, None, f"query {QUERY}: keys or order"),
                (2, None, f"query {QUERY}: query_id appears more than once"),
                (3, None, f"query {UNKNOWN_QUERY}: query_id is not allowed"),
            ),
        )
        self.assertNotIn(DO_NOT_ECHO, "\n".join(finding.text() for finding in result.findings))

    def test_not_utf8_is_one_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = read(_write(Path(directory), b"\xff\n"), (QUERY,))

        self.assertEqual(
            _located(result.findings),
            ((1, None, "file is not UTF-8"),),
        )

    def test_no_final_newline_is_one_finding(self) -> None:
        data = json.dumps({"query_id": QUERY, "ranked": []}, separators=(",", ":")).encode(
            "utf-8"
        )
        with tempfile.TemporaryDirectory() as directory:
            result = read(_write(Path(directory), data), (QUERY,))

        self.assertEqual(
            _located(result.findings),
            ((0, None, "file has no final newline"),),
        )

    def test_no_finding_names_the_file_s_own_path(self) -> None:
        """The file may live anywhere, so its directory never reaches a stream."""

        secret = "matter-fictional-client-v-fictional-defendant"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / secret
            root.mkdir()
            path = _write(root, _line(QUERY, [_entry(source_id="not a source")]))
            findings = read(path, (QUERY,)).findings
            missing = read(root / "absent.jsonl", (QUERY,)).findings

        self.assertTrue(findings)
        self.assertTrue(missing)
        for finding in findings + missing:
            with self.subTest(rule=finding.rule):
                self.assertEqual(finding.file, "ranked-list")
                self.assertNotIn(secret, finding.text())
                self.assertNotIn(str(root), finding.text())

    def test_unreadable_path_is_one_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not-a-file"
            path.mkdir()
            result = read(path, (QUERY,))

        self.assertEqual(
            _located(result.findings),
            ((0, None, "file cannot be read"),),
        )
