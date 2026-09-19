"""Contract tests for the harvest-derived judgments queries file."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from typing import Final, cast
from unittest import TestCase

from tools.exportboundary import absent_from_export

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
QUERIES_PATH: Final[Path] = Path("eval/sets/eval-v1/judgments/queries.jsonl")
HARVEST_PATH: Final[Path] = Path("eval/seed/prototype-qa/harvest.jsonl")
FLAGGER_PATH: Final[Path] = Path("eval/seed/prototype-qa/scripts/redaction_flags.py")
# Append-only: an intake's summary prints the next pair, added beside these and never in place of one.
PINNED_PREFIXES: Final[tuple[tuple[int, str], ...]] = (
    (38, "0fba5d51cd44750b78f0c7940994dd8aa13ca9a84dd2ce3da8d2ad428b9b8b40"),  # the harvest's, CSA-1 2026-09-19
)
EXCLUDED_HARVEST_IDS: Final[frozenset[str]] = frozenset({"HARV-014", "HARV-017"})
# HARV-014 is a prompt-writing request; HARV-017 is a word-processor how-to.
ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^judgments-[0-9]{3,}$")
HARVEST_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^HARV-[0-9]{3}$")
CLUSTER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^harvest-chat-[A-Za-z0-9_-]+$")
ROLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:CHU-attorney|CHU-investigator|CHU-paralegal|TRAD-attorney|"
    r"TRAD-investigator|TRAD-legal-assistant|support|CSA|unknown)-[1-9][0-9]*$"
)
BASE_KEYS: Final[tuple[str, ...]] = (
    "id",
    "suite",
    "category",
    "branch",
    "question",
    "labels",
    "cluster_id",
    "notes",
    "review",
)
REVIEW_KEYS: Final[tuple[str, ...]] = ("by", "on", "accepted_flags")
QUERY_TYPES: Final[frozenset[str]] = frozenset(
    {"doctrinal", "statute", "case-specific", "other"}
)
SENTINEL: Final[str] = "SENTINEL_FICTITIOUS_QUERY"


@dataclass(frozen=True, slots=True)
class ParsedLine:
    """One parsed JSONL object and its source line number."""

    number: int
    record: dict[str, object]
    raw: bytes


def _line_finding(line_number: int, record_id: str, field: str) -> str:
    return f"line {line_number} {record_id}: {field}"


def _record_id(line: ParsedLine) -> str:
    value = line.record.get("id")
    return value if isinstance(value, str) else f"line-{line.number}"


def _origin(record: Mapping[str, object]) -> str | None:
    labels = record.get("labels")
    return labels[0] if isinstance(labels, list) and labels and isinstance(labels[0], str) else None


def _parse_lines(data: bytes) -> tuple[tuple[ParsedLine, ...], list[str]]:
    lines: list[ParsedLine] = []
    findings: list[str] = []
    for number, raw in enumerate(data.splitlines(keepends=True), 1):
        payload = raw[:-1] if raw.endswith(b"\n") else raw
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            findings.append(f"line {number}: malformed JSON object")
            continue
        if not isinstance(decoded, dict):
            findings.append(f"line {number}: JSON value is not an object")
            continue
        lines.append(ParsedLine(number, cast(dict[str, object], decoded), raw))
    return tuple(lines), findings


def _check_document(data: bytes, lines: Sequence[ParsedLine]) -> list[str]:
    findings: list[str] = []
    if not data.endswith(b"\n"):
        findings.append("line 0 document: missing final newline")
    if data.count(b"\n") != len(lines):
        findings.append("line 0 document: every line must be one JSON object")
    return findings


def _check_prefixes(
    data: bytes,
    lines: Sequence[ParsedLine],
    prefixes: Sequence[tuple[int, str]] = PINNED_PREFIXES,
) -> list[str]:
    findings: list[str] = []
    previous_count = 0
    line_count = data.count(b"\n")
    for count, expected_digest in prefixes:
        if count <= previous_count:
            findings.append(f"line 0 document: prefix count {count} is not strictly rising")
        if count > line_count:
            findings.append(f"line 0 document: prefix count {count} exceeds line count")
            previous_count = count
            continue
        raw_lines = data.splitlines(keepends=True)
        actual_digest = hashlib.sha256(b"".join(raw_lines[:count])).hexdigest()
        if actual_digest != expected_digest:
            findings.append(f"line 0 document: prefix digest at count {count}")
        previous_count = count
    if prefixes and prefixes[-1][0] != line_count:
        findings.append("line 0 document: line count does not equal the last prefix")
    return findings


def _check_ids(lines: Sequence[ParsedLine]) -> list[str]:
    findings: list[str] = []
    seen: set[str] = set()
    for ordinal, line in enumerate(lines, 1):
        record_id = line.record.get("id")
        expected = f"judgments-{ordinal:03d}"
        if not isinstance(record_id, str) or record_id != expected:
            findings.append(_line_finding(line.number, _record_id(line), "id"))
        elif record_id in seen:
            findings.append(_line_finding(line.number, record_id, "duplicate id"))
        else:
            seen.add(record_id)
    return findings


def _expected_keys(record: Mapping[str, object], origin: str | None) -> tuple[str, ...]:
    keys = list(BASE_KEYS)
    if origin == "harvest":
        question_index = keys.index("question") + 1
        optional = [key for key in ("reference_date", "jurisdiction") if key in record]
        keys[question_index:question_index] = optional
        labels_index = keys.index("labels")
        keys[labels_index + 1 : labels_index + 1] = ["seed"]
    if "supersedes" in record:
        keys.append("supersedes")
    return tuple(keys)


def _check_shape(lines: Sequence[ParsedLine]) -> list[str]:
    findings: list[str] = []
    for line in lines:
        record = line.record
        record_id = _record_id(line)
        labels = record.get("labels")
        origin = _origin(record)
        if tuple(record) != _expected_keys(record, origin):
            findings.append(_line_finding(line.number, record_id, "keys"))

        for field, expected in (
            ("suite", "judgments"),
            ("category", "judgments"),
            ("branch", "legal"),
        ):
            if record.get(field) != expected:
                findings.append(_line_finding(line.number, record_id, field))

        question = record.get("question")
        if (
            not isinstance(question, str)
            or not question
            or question != question.strip()
            or "\n" in question
            or "\r" in question
        ):
            findings.append(_line_finding(line.number, record_id, "question"))

        if not isinstance(labels, list) or len(labels) < 2:
            findings.append(_line_finding(line.number, record_id, "labels"))
            continue
        wording = labels[1]
        if origin not in {"harvest", "chu-written"}:
            findings.append(_line_finding(line.number, record_id, "labels.origin"))
        if wording not in {"verbatim", "rewritten"}:
            findings.append(_line_finding(line.number, record_id, "labels.wording"))
        if origin == "harvest":
            if len(labels) != 3 or labels[2] not in QUERY_TYPES:
                findings.append(_line_finding(line.number, record_id, "labels.query_type"))
        elif origin == "chu-written" and len(labels) != 2:
            findings.append(_line_finding(line.number, record_id, "labels"))

        seed = record.get("seed")
        if origin == "harvest":
            if not isinstance(seed, str) or HARVEST_ID_PATTERN.fullmatch(seed) is None:
                findings.append(_line_finding(line.number, record_id, "seed"))
        elif "seed" in record:
            findings.append(_line_finding(line.number, record_id, "seed"))

        cluster_id = record.get("cluster_id")
        if not isinstance(cluster_id, str):
            findings.append(_line_finding(line.number, record_id, "cluster_id"))
        elif origin == "harvest":
            if CLUSTER_PATTERN.fullmatch(cluster_id) is None:
                findings.append(_line_finding(line.number, record_id, "cluster_id"))
        elif cluster_id != record_id or not isinstance(record_id, str):
            findings.append(_line_finding(line.number, record_id, "cluster_id"))

        if not isinstance(record.get("notes"), str):
            findings.append(_line_finding(line.number, record_id, "notes"))

        review = record.get("review")
        if not isinstance(review, dict) or tuple(review) != REVIEW_KEYS:
            findings.append(_line_finding(line.number, record_id, "review"))
            continue
        reviewer = review.get("by")
        if not isinstance(reviewer, str) or ROLE_PATTERN.fullmatch(reviewer) is None:
            findings.append(_line_finding(line.number, record_id, "review.by"))
        review_date = review.get("on")
        try:
            valid_date = isinstance(review_date, str) and date.fromisoformat(review_date).isoformat() == review_date
        except ValueError:
            valid_date = False
        if not valid_date:
            findings.append(_line_finding(line.number, record_id, "review.on"))
        accepted_flags = review.get("accepted_flags")
        if (
            not isinstance(accepted_flags, list)
            or any(not isinstance(kind, str) for kind in accepted_flags)
            or accepted_flags != sorted(accepted_flags)
            or len(set(accepted_flags)) != len(accepted_flags)
        ):
            findings.append(_line_finding(line.number, record_id, "review.accepted_flags"))

        reference_date = record.get("reference_date")
        if "reference_date" in record:
            try:
                valid_reference = (
                    isinstance(reference_date, str)
                    and date.fromisoformat(reference_date).isoformat() == reference_date
                )
            except ValueError:
                valid_reference = False
            if not valid_reference:
                findings.append(_line_finding(line.number, record_id, "reference_date"))
        jurisdiction = record.get("jurisdiction")
        if "jurisdiction" in record and (
            not isinstance(jurisdiction, list)
            or not jurisdiction
            or any(not isinstance(court, str) or re.fullmatch(r"[a-z0-9]+", court) is None for court in jurisdiction)
        ):
            findings.append(_line_finding(line.number, record_id, "jurisdiction"))

        supersedes = record.get("supersedes")
        if "supersedes" in record and (
            not isinstance(supersedes, str) or ID_PATTERN.fullmatch(supersedes) is None
        ):
            findings.append(_line_finding(line.number, record_id, "supersedes"))
    return findings


def _check_supersedes(lines: Sequence[ParsedLine]) -> list[str]:
    findings: list[str] = []
    ids = [_record_id(line) for line in lines]
    positions = {record_id: position for position, record_id in enumerate(ids)}
    targets: set[str] = set()
    for position, line in enumerate(lines):
        target = line.record.get("supersedes")
        if target is None:
            continue
        record_id = _record_id(line)
        if (
            not isinstance(target, str)
            or target not in positions
            or positions[target] >= position
            or target in targets
        ):
            findings.append(_line_finding(line.number, record_id, "supersedes"))
        else:
            targets.add(target)
    return findings


def _check_harvest(
    lines: Sequence[ParsedLine], harvest: Mapping[str, Mapping[str, object]]
) -> list[str]:
    findings: list[str] = []
    harvest_lines = [line for line in lines if _origin(line.record) == "harvest"]
    expected_seeds = [seed for seed in harvest if seed not in EXCLUDED_HARVEST_IDS]
    actual_seeds = [line.record.get("seed") for line in harvest_lines]
    if actual_seeds != expected_seeds:
        findings.append("line 0 document: seed order")
    for line in harvest_lines:
        record = line.record
        record_id = _record_id(line)
        seed = record.get("seed")
        if not isinstance(seed, str) or seed not in harvest:
            findings.append(_line_finding(line.number, record_id, "seed"))
            continue
        source = harvest[seed].get("source")
        if not isinstance(source, dict) or not isinstance(source.get("chat_hash"), str):
            findings.append(_line_finding(line.number, record_id, "source.chat_hash"))
        elif record.get("cluster_id") != f"harvest-chat-{source['chat_hash']}":
            findings.append(_line_finding(line.number, record_id, "cluster_id"))
        labels = record.get("labels")
        if not isinstance(labels, list) or len(labels) != 3 or labels[2] != harvest[seed].get("query_type"):
            findings.append(_line_finding(line.number, record_id, "labels.query_type"))
        harvest_question = harvest[seed].get("question")
        query_question = record.get("question")
        if not isinstance(harvest_question, str) or not isinstance(query_question, str):
            findings.append(_line_finding(line.number, record_id, "question"))
        elif isinstance(labels, list) and len(labels) > 1:
            if labels[1] == "verbatim" and query_question != harvest_question.strip():
                findings.append(_line_finding(line.number, record_id, "question"))
            if labels[1] == "rewritten" and query_question == harvest_question.strip():
                findings.append(_line_finding(line.number, record_id, "question"))
    return findings


def _check_flagger(
    lines: Sequence[ParsedLine],
    find_flags: Callable[[str], Collection[Mapping[str, object]]],
) -> list[str]:
    findings: list[str] = []
    for line in lines:
        record = line.record
        record_id = _record_id(line)
        question = record.get("question")
        review = record.get("review")
        if not isinstance(question, str) or not isinstance(review, dict):
            findings.append(_line_finding(line.number, record_id, "question/review"))
            continue
        flags = find_flags(question)
        hard_kinds = _flag_kinds(flags, "hard")
        soft_kinds = _flag_kinds(flags, "soft")
        if hard_kinds:
            findings.append(_line_finding(line.number, record_id, "question"))
        accepted_flags = review.get("accepted_flags")
        if accepted_flags != soft_kinds:
            findings.append(_line_finding(line.number, record_id, "review.accepted_flags"))
    return findings


def _flag_kinds(flags: Collection[Mapping[str, object]], severity: str) -> list[str]:
    kinds: list[str] = []
    for flag in flags:
        kind = flag.get("kind")
        if flag.get("severity") == severity and isinstance(kind, str):
            kinds.append(kind)
    return sorted(set(kinds))


def _load_frozen_flagger(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("judgments_queries_redaction_flags", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("the frozen flagger cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_harvest(path: Path) -> tuple[dict[str, Mapping[str, object]], list[str]]:
    records: dict[str, Mapping[str, object]] = {}
    findings: list[str] = []
    for line_number, raw in enumerate(path.read_bytes().splitlines(), 1):
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            findings.append(f"line {line_number}: malformed harvest record")
            continue
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            findings.append(f"line {line_number}: malformed harvest record")
            continue
        records[record["id"]] = cast(Mapping[str, object], record)
    return records, findings


class QueriesContract(TestCase):
    """The committed JSONL shape is checked without exposing question text."""

    def setUp(self) -> None:
        if absent_from_export(QUERIES_PATH, ROOT):
            self.skipTest("queries file is absent from this exported tree")
        self.data = (ROOT / QUERIES_PATH).read_bytes()
        self.lines, parse_findings = _parse_lines(self.data)
        self.assertTrue(not parse_findings, f"parse findings: {parse_findings}")

    def test_document_has_objects_and_one_final_newline(self) -> None:
        findings = _check_document(self.data, self.lines)
        self.assertTrue(not findings, f"findings: {findings}")

    def test_every_committed_line_is_pinned(self) -> None:
        self.assertTrue(PINNED_PREFIXES, "PINNED_PREFIXES has no pair")
        findings = _check_prefixes(self.data, self.lines)
        self.assertTrue(not findings, f"findings: {findings}")

    def test_ids_are_the_contiguous_series(self) -> None:
        findings = _check_ids(self.lines)
        self.assertTrue(not findings, f"findings: {findings}")

    def test_shape_labels_fields_and_review_are_exact(self) -> None:
        findings = _check_shape(self.lines)
        self.assertTrue(not findings, f"findings: {findings}")

    def test_supersedes_is_earlier_and_unique(self) -> None:
        findings = _check_supersedes(self.lines)
        self.assertTrue(not findings, f"findings: {findings}")

    def test_harvest_relationships(self) -> None:
        if absent_from_export(HARVEST_PATH, ROOT):
            self.skipTest("harvest is absent from this exported tree")
        harvest, parse_findings = _read_harvest(ROOT / HARVEST_PATH)
        self.assertTrue(not parse_findings, f"harvest parse findings: {parse_findings}")
        findings = _check_harvest(self.lines, harvest)
        self.assertTrue(not findings, f"findings: {findings}")

    def test_flagger_relationships(self) -> None:
        if absent_from_export(FLAGGER_PATH, ROOT):
            self.skipTest("flagger is absent from this exported tree")
        module = _load_frozen_flagger(ROOT / FLAGGER_PATH)
        find_flags = cast(Callable[[str], Collection[Mapping[str, object]]], module.find_flags)
        findings = _check_flagger(self.lines, find_flags)
        self.assertTrue(not findings, f"findings: {findings}")


class FindingsCarryNoQuestion(TestCase):
    """Every checking helper, driven to failure over a sentinel question, never names it."""

    def test_no_finding_includes_the_sentinel(self) -> None:
        record = {
            "id": "judgments-002",
            "suite": "judgment",
            "category": "judgments",
            "branch": "legal",
            "question": f" {SENTINEL}",
            "labels": ["harvest", "verbatim", "doctrinal"],
            "seed": "HARV-001",
            "cluster_id": "harvest-chat-000000000000",
            "notes": "",
            "review": {"by": "nobody", "on": "2026-13-01", "accepted_flags": ["b", "a"]},
            "supersedes": "judgments-009",
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "queries.jsonl"
            path.write_bytes((json.dumps(record) + f"\n{SENTINEL} is no JSON\n").encode("utf-8"))
            data = path.read_bytes()
        lines, parse_findings = _parse_lines(data)
        harvest = {
            "HARV-001": {
                "question": f"{SENTINEL} as the record asked it",
                "query_type": "statute",
                "source": {"chat_hash": "111111111111"},
            }
        }

        def flagger(_question: str) -> tuple[Mapping[str, object], ...]:
            return ({"kind": "docket_number", "severity": "hard"}, {"kind": "dob", "severity": "soft"})

        checks = {
            "parse": parse_findings,
            "document": _check_document(data[:-1], lines),
            "prefixes": _check_prefixes(data, lines, ((1, "0" * 64),)),
            "ids": _check_ids(lines),
            "shape": _check_shape(lines),
            "supersedes": _check_supersedes(lines),
            "harvest": _check_harvest(lines, harvest),
            "flagger": _check_flagger(lines, flagger),
        }
        for name, findings in checks.items():
            with self.subTest(helper=name):
                self.assertTrue(findings, "the checking helper found nothing")
                self.assertTrue(all(SENTINEL not in finding for finding in findings), "a finding names the question")
