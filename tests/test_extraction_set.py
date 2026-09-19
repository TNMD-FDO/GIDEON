"""The extraction scorer and frozen JSONL set contracts."""

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final, cast
from unittest import TestCase

from gideon.extraction import (
    KEYED_TYPES,
    OBJECT_TYPES,
    SECTION_TYPES,
    ExactObject,
    ObjectType,
    extract,
    key_violations,
    ordering_violations,
    span_violations,
)
from gideon.extraction.grammar import registry_types
from gideon.extraction.scoring import active_cases, build_report, score
from tools.exportboundary import absent_from_export

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
EXTRACTION_PATH: Final[Path] = Path("eval/sets/eval-v1/build-gates/extraction.jsonl")
INVENTED_PATH: Final[Path] = Path(
    "eval/sets/eval-v1/build-gates/extraction-invented.jsonl"
)
HARVEST_PATH: Final[Path] = Path("eval/seed/prototype-qa/harvest.jsonl")
EXTRACTION_PINNED_PREFIXES: Final[tuple[tuple[int, str], ...]] = (
    (40, "dc1e0c928f1860cf7ae068e1ff96fd7418c5d03da2a449a175da26833f2d462e"),  # CSA-1 2026-09-19
)
INVENTED_PINNED_PREFIXES: Final[tuple[tuple[int, str], ...]] = (
    (22, "8c057174f06ac4afc869a87642ac70eb0a92a3e3b10b2c14f0723d7ee53298a0"),  # CSA-1 2026-09-19
)
ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^extraction-[0-9]{3}$")
HARVEST_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^HARV-[0-9]{3}$")
CLUSTER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^harvest-chat-[0-9a-f]+$")
ROLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:CHU-attorney|CHU-investigator|CHU-paralegal|TRAD-attorney|"
    r"TRAD-investigator|TRAD-legal-assistant|support|CSA|unknown)-[1-9][0-9]*$"
)
QUERY_TYPES: Final[frozenset[str]] = frozenset(
    {"doctrinal", "statute", "case-specific", "other"}
)
BASE_CASE_KEYS: Final[tuple[str, ...]] = (
    "id",
    "suite",
    "category",
    "branch",
    "question",
    "expected",
    "labels",
)
KEY_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "statute": re.compile(r"/us/usc/t[0-9]+/s[0-9A-Za-z]+(?:-[0-9A-Za-z]+)?"),
    "guideline": re.compile(r"ussg/[0-9][A-Z][0-9]+\.[0-9]+"),
    "court_rule": re.compile(
        r"/us/usc/t(?:18a/courtRules/Crim|28a/courtRules/(?:Civil|App|Evid))"
        r"/rule[0-9]+(?:\.[0-9]+)?"
    ),
    "regulation": re.compile(r"cfr/[0-9]+/[0-9A-Za-z.-]+"),
    "habeas_rule": re.compile(r"rules/(?:2254|2255)/rule[0-9]+"),
    "scotus_rule": re.compile(r"rules/scotus/rule[0-9]+"),
    "appendix_statute": re.compile(
        r"/us/usc/t[0-9]+a/pl/[0-9]+/[0-9]+/s[0-9A-Za-z-]+"
    ),
}


@dataclass(frozen=True, slots=True)
class ParsedLine:
    """One JSONL object and its source line number."""

    number: int
    record: dict[str, object]


def _finding(path: Path, location: str | int, rule: str) -> str:
    return f"{path}:{location}: {rule}"


def _record_id(line: ParsedLine) -> str:
    value = line.record.get("id")
    return value if isinstance(value, str) else f"line-{line.number}"


def _origin(record: Mapping[str, object]) -> str | None:
    labels = record.get("labels")
    if isinstance(labels, list) and labels and isinstance(labels[0], str):
        return labels[0]
    return None


def _parse_jsonl(path: Path) -> tuple[bytes, tuple[ParsedLine, ...], list[str]]:
    full_path = ROOT / path
    if not full_path.is_file():
        return b"", (), [_finding(path, 0, "file is missing")]
    data = full_path.read_bytes()
    lines: list[ParsedLine] = []
    findings: list[str] = []
    for number, raw in enumerate(data.splitlines(), start=1):
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            findings.append(_finding(path, number, "malformed JSON object"))
            continue
        if not isinstance(decoded, dict):
            findings.append(_finding(path, number, "JSON value is not an object"))
            continue
        lines.append(ParsedLine(number, cast(dict[str, object], decoded)))
    return data, tuple(lines), findings


def _document_findings(
    path: Path, data: bytes, lines: Sequence[ParsedLine]
) -> list[str]:
    findings: list[str] = []
    if not data.endswith(b"\n"):
        findings.append(_finding(path, 0, "missing final newline"))
    if data.count(b"\n") != len(lines):
        findings.append(_finding(path, 0, "every line must be one JSON object"))
    return findings


def _prefix_findings(
    path: Path,
    data: bytes,
    prefixes: Sequence[tuple[int, str]],
) -> list[str]:
    findings: list[str] = []
    previous_count = 0
    line_count = data.count(b"\n")
    raw_lines = data.splitlines(keepends=True)
    for count, expected_digest in prefixes:
        if count <= previous_count:
            findings.append(_finding(path, 0, "prefix counts are not strictly rising"))
        if count > line_count:
            findings.append(_finding(path, 0, "prefix count exceeds line count"))
            previous_count = count
            continue
        actual_digest = hashlib.sha256(b"".join(raw_lines[:count])).hexdigest()
        if actual_digest != expected_digest:
            findings.append(_finding(path, 0, "prefix digest does not match"))
        previous_count = count
    if not prefixes:
        findings.append(_finding(path, 0, "PINNED_PREFIXES is empty"))
    elif prefixes[-1][0] != line_count:
        findings.append(_finding(path, 0, "last prefix does not pin the file"))
    return findings


def _expected_case_keys(record: Mapping[str, object]) -> tuple[str, ...]:
    keys = list(BASE_CASE_KEYS)
    if _origin(record) == "harvest":
        keys.append("seed")
    keys.extend(("cluster_id", "review"))
    if "supersedes" in record:
        keys.append("supersedes")
    keys.append("notes")
    return tuple(keys)


def _shape_findings(path: Path, line: ParsedLine) -> list[str]:
    record = line.record
    record_id = _record_id(line)
    location = f"line {line.number} {record_id}"
    findings: list[str] = []
    if tuple(record) != _expected_case_keys(record):
        findings.append(_finding(path, location, "case keys or order"))
    if record.get("suite") != "build-gates":
        findings.append(_finding(path, location, "suite"))
    if record.get("category") != "extraction":
        findings.append(_finding(path, location, "category"))
    if record.get("branch") != "legal":
        findings.append(_finding(path, location, "branch"))

    question = record.get("question")
    if (
        not isinstance(question, str)
        or not question
        or question != question.strip()
    ):
        findings.append(_finding(path, location, "question shape"))

    labels = record.get("labels")
    origin = _origin(record)
    if not isinstance(labels, list) or not labels or origin not in {"harvest", "invented"}:
        findings.append(_finding(path, location, "labels origin"))
    elif origin == "harvest":
        if len(labels) != 2 or not isinstance(labels[1], str) or labels[1] not in QUERY_TYPES:
            findings.append(_finding(path, location, "harvest labels"))
    elif len(labels) != 1:
        findings.append(_finding(path, location, "invented labels"))

    seed = record.get("seed")
    if origin == "harvest":
        if not isinstance(seed, str) or HARVEST_ID_PATTERN.fullmatch(seed) is None:
            findings.append(_finding(path, location, "harvest seed"))
    elif "seed" in record:
        findings.append(_finding(path, location, "invented seed is forbidden"))

    cluster_id = record.get("cluster_id")
    if not isinstance(cluster_id, str):
        findings.append(_finding(path, location, "cluster_id"))
    elif origin == "harvest":
        if CLUSTER_PATTERN.fullmatch(cluster_id) is None:
            findings.append(_finding(path, location, "harvest cluster_id"))
    elif cluster_id != record_id:
        findings.append(_finding(path, location, "invented cluster_id"))

    review = record.get("review")
    if not isinstance(review, dict) or tuple(review) != ("by", "on"):
        findings.append(_finding(path, location, "review keys or order"))
    else:
        reviewer = review.get("by")
        if not isinstance(reviewer, str) or ROLE_PATTERN.fullmatch(reviewer) is None:
            findings.append(_finding(path, location, "review.by role"))
        reviewed_on = review.get("on")
        try:
            valid_date = (
                isinstance(reviewed_on, str)
                and date.fromisoformat(reviewed_on).isoformat() == reviewed_on
            )
        except ValueError:
            valid_date = False
        if not valid_date:
            findings.append(_finding(path, location, "review.on ISO date"))

    if not isinstance(record.get("notes"), str):
        findings.append(_finding(path, location, "notes string"))
    if "supersedes" in record and not isinstance(record["supersedes"], str):
        findings.append(_finding(path, location, "supersedes id"))
    return findings


def _label_findings(path: Path, line: ParsedLine) -> list[str]:
    record = line.record
    record_id = _record_id(line)
    location = f"line {line.number} {record_id}"
    question = record.get("question")
    expected = record.get("expected")
    findings: list[str] = []
    if not isinstance(question, str) or not isinstance(expected, dict):
        return [_finding(path, location, "question and expected mapping")]
    objects_value = expected.get("objects")
    if not isinstance(objects_value, list):
        return [_finding(path, location, "expected.objects list")]

    exact_objects: list[ExactObject] = []
    for index, value in enumerate(objects_value, start=1):
        object_location = f"line {line.number} {record_id} object {index}"
        if not isinstance(value, dict):
            findings.append(_finding(path, object_location, "object mapping"))
            continue
        object_type_value = value.get("type")
        if not isinstance(object_type_value, str) or object_type_value not in OBJECT_TYPES:
            findings.append(_finding(path, object_location, "closed object type"))
            continue
        object_type = cast(ObjectType, object_type_value)
        expected_keys = ["type", "start", "end", "text"]
        if object_type in KEYED_TYPES:
            expected_keys.append("key")
        if object_type in SECTION_TYPES:
            expected_keys.append("subsections")
        if tuple(value) != tuple(expected_keys):
            findings.append(_finding(path, object_location, "object keys or order"))

        start = value.get("start")
        end = value.get("end")
        text = value.get("text")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end <= start
            or end > len(question)
        ):
            findings.append(_finding(path, object_location, "object offsets"))
            continue
        if not isinstance(text, str) or text != question[start:end]:
            findings.append(_finding(path, object_location, "verbatim object text"))
            continue

        key = value.get("key")
        if object_type in KEYED_TYPES:
            if not isinstance(key, str):
                findings.append(_finding(path, object_location, "key required"))
            elif object_type not in KEY_PATTERNS or KEY_PATTERNS[object_type].fullmatch(key) is None:
                findings.append(_finding(path, object_location, "key form"))
        elif "key" in value:
            findings.append(_finding(path, object_location, "key forbidden"))

        subsections_value = value.get("subsections")
        if object_type in SECTION_TYPES:
            if (
                not isinstance(subsections_value, list)
                or any(not isinstance(item, str) for item in subsections_value)
            ):
                findings.append(_finding(path, object_location, "subsections list"))
                subsections: tuple[str, ...] = ()
            else:
                subsections = tuple(
                    item for item in subsections_value if isinstance(item, str)
                )
            expected_subsections = tuple(re.findall(r"\(([^()]*)\)", text))
            if subsections != expected_subsections:
                findings.append(_finding(path, object_location, "subsections match span"))
        elif "subsections" in value:
            findings.append(_finding(path, object_location, "subsections forbidden"))
            subsections = ()
        else:
            subsections = ()

        exact_objects.append(
            ExactObject(
                object_type,
                start,
                end,
                text,
                key if isinstance(key, str) else None,
                subsections=subsections,
            )
        )

    for _ in span_violations(question, exact_objects):
        findings.append(_finding(path, location, "contract span invariant"))
    for _ in key_violations(exact_objects):
        findings.append(_finding(path, location, "contract key invariant"))
    for _ in ordering_violations(exact_objects):
        findings.append(_finding(path, location, "contract ordering invariant"))
    return findings


def _read_harvest(path: Path) -> tuple[dict[str, Mapping[str, object]], list[str]]:
    records: dict[str, Mapping[str, object]] = {}
    findings: list[str] = []
    for number, raw in enumerate((ROOT / path).read_bytes().splitlines(), start=1):
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            findings.append(_finding(path, number, "malformed harvest record"))
            continue
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            findings.append(_finding(path, number, "harvest record shape"))
            continue
        records[value["id"]] = cast(Mapping[str, object], value)
    return records, findings


def _available_case_files(
    test_case: TestCase,
) -> tuple[tuple[Path, bytes, tuple[ParsedLine, ...]], ...]:
    files: list[tuple[Path, bytes, tuple[ParsedLine, ...]]] = []
    if not absent_from_export(EXTRACTION_PATH, ROOT):
        data, lines, parse_findings = _parse_jsonl(EXTRACTION_PATH)
        test_case.assertFalse(parse_findings, f"findings: {parse_findings}")
        files.append((EXTRACTION_PATH, data, lines))
    data, lines, parse_findings = _parse_jsonl(INVENTED_PATH)
    test_case.assertFalse(parse_findings, f"findings: {parse_findings}")
    files.append((INVENTED_PATH, data, lines))
    return tuple(files)


def _score_inputs(
    test_case: TestCase,
    *,
    invented_only: bool = False,
) -> tuple[tuple[tuple[dict[str, object], ...], ...], dict[str, tuple[ExactObject, ...]]]:
    files = tuple(
        tuple(line.record for line in lines)
        for path, _, lines in _available_case_files(test_case)
        if not invented_only or path == INVENTED_PATH
    )
    active = active_cases(files)
    extracted = {
        cast(str, case["id"]): extract(cast(str, case["question"])) for case in active
    }
    return files, extracted


class SetContract(TestCase):
    """The reviewed extraction JSONL files are frozen data contracts."""

    def test_documents_have_one_final_newline(self) -> None:
        for path, data, lines in _available_case_files(self):
            findings = _document_findings(path, data, lines)
            self.assertFalse(findings, f"findings: {findings}")

    def test_each_case_has_the_committed_shape(self) -> None:
        findings = [
            finding
            for path, _, lines in _available_case_files(self)
            for line in lines
            for finding in _shape_findings(path, line)
        ]
        self.assertFalse(findings, f"findings: {findings}")

    def test_each_label_has_the_contract_shape(self) -> None:
        findings = [
            finding
            for path, _, lines in _available_case_files(self)
            for line in lines
            for finding in _label_findings(path, line)
        ]
        self.assertFalse(findings, f"findings: {findings}")

    def test_each_file_is_pinned_by_an_append_only_prefix(self) -> None:
        prefixes = (
            (INVENTED_PATH, INVENTED_PINNED_PREFIXES),
            (EXTRACTION_PATH, EXTRACTION_PINNED_PREFIXES),
        )
        findings: list[str] = []
        for path, expected_prefixes in prefixes:
            if path == EXTRACTION_PATH and absent_from_export(path, ROOT):
                continue
            data, lines, parse_findings = _parse_jsonl(path)
            findings.extend(parse_findings)
            findings.extend(_prefix_findings(path, data, expected_prefixes))
            findings.extend(_document_findings(path, data, lines))
        self.assertFalse(findings, f"findings: {findings}")

    def test_ids_form_one_contiguous_series_and_supersedes_is_earlier(self) -> None:
        files = _available_case_files(self)
        all_lines = [(path, line) for path, _, lines in files for line in lines]
        expected_number = 1 if not absent_from_export(EXTRACTION_PATH, ROOT) else 41
        findings: list[str] = []
        positions: dict[str, int] = {}
        for position, (path, line) in enumerate(all_lines):
            record_id = line.record.get("id")
            location = f"line {line.number} {_record_id(line)}"
            expected_id = f"extraction-{expected_number:03d}"
            if (
                not isinstance(record_id, str)
                or ID_PATTERN.fullmatch(record_id) is None
                or record_id != expected_id
            ):
                findings.append(_finding(path, location, "contiguous id series"))
            elif record_id in positions:
                findings.append(_finding(path, location, "duplicate id"))
            else:
                positions[record_id] = position
            expected_number += 1

        for path, line in all_lines:
            target = line.record.get("supersedes")
            if target is None:
                continue
            location = f"line {line.number} {_record_id(line)}"
            position = positions.get(_record_id(line), -1)
            if (
                not isinstance(target, str)
                or target not in positions
                or positions[target] >= position
            ):
                findings.append(_finding(path, location, "supersedes existing earlier id"))
        self.assertFalse(findings, f"findings: {findings}")

    def test_harvest_relationships(self) -> None:
        if absent_from_export(EXTRACTION_PATH, ROOT) or absent_from_export(HARVEST_PATH, ROOT):
            self.skipTest("harvest-derived extraction inputs are absent from this export")
        _, extraction_lines, extraction_parse_findings = _parse_jsonl(EXTRACTION_PATH)
        harvest, harvest_findings = _read_harvest(HARVEST_PATH)
        findings = [*extraction_parse_findings, *harvest_findings]
        harvest_lines = [
            line for line in extraction_lines if _origin(line.record) == "harvest"
        ]
        if len(harvest) != 40 or len(harvest_lines) != 40:
            findings.append(_finding(EXTRACTION_PATH, 0, "exactly forty harvest cases"))
        expected_seeds = sorted(harvest)
        actual_seeds = [line.record.get("seed") for line in harvest_lines]
        if actual_seeds != expected_seeds:
            findings.append(_finding(EXTRACTION_PATH, 0, "harvest seed order"))

        for line in harvest_lines:
            record = line.record
            location = f"line {line.number} {_record_id(line)}"
            seed = record.get("seed")
            if not isinstance(seed, str) or seed not in harvest:
                findings.append(_finding(EXTRACTION_PATH, location, "harvest seed relation"))
                continue
            harvest_record = harvest[seed]
            source = harvest_record.get("source")
            if not isinstance(source, Mapping) or not isinstance(source.get("chat_hash"), str):
                findings.append(_finding(EXTRACTION_PATH, location, "harvest chat hash"))
            elif record.get("cluster_id") != f"harvest-chat-{source['chat_hash']}":
                findings.append(_finding(EXTRACTION_PATH, location, "harvest cluster relation"))
            harvest_question = harvest_record.get("question")
            question = record.get("question")
            if not isinstance(harvest_question, str) or not isinstance(question, str) or question != harvest_question.strip():
                findings.append(_finding(EXTRACTION_PATH, location, "harvest question relation"))
            labels = record.get("labels")
            if (
                not isinstance(labels, list)
                or len(labels) != 2
                or labels[1] != harvest_record.get("query_type")
            ):
                findings.append(_finding(EXTRACTION_PATH, location, "harvest query type relation"))
        self.assertFalse(findings, f"findings: {findings}")

    def test_invented_file_meets_each_landed_floor_and_has_empty_cases(self) -> None:
        invented = next(
            lines for path, _, lines in _available_case_files(self) if path == INVENTED_PATH
        )
        counts: Counter[str] = Counter()
        empty_cases = 0
        for line in invented:
            expected = line.record.get("expected")
            objects = expected.get("objects") if isinstance(expected, dict) else None
            if not isinstance(objects, list):
                continue
            if not objects:
                empty_cases += 1
            for value in objects:
                if isinstance(value, Mapping) and isinstance(value.get("type"), str):
                    counts[value["type"]] += 1

        floor_table: dict[ObjectType, int] = {
            "guideline": 10,
            "court_rule": 10,
            "bare_rule": 10,
            "statute": 5,
            "bare_section": 5,
        }
        findings: list[str] = []
        if set(floor_table) != set(registry_types()):
            findings.append(_finding(INVENTED_PATH, 0, "registry types have floors"))
        for object_type in registry_types():
            floor = floor_table.get(object_type, 0)
            if counts[object_type] < floor:
                findings.append(_finding(INVENTED_PATH, 0, f"{object_type} label floor"))
        if empty_cases < 4:
            findings.append(_finding(INVENTED_PATH, 0, "four empty-object cases"))
        self.assertFalse(findings, f"findings: {findings}")

    def test_registry_gate_over_available_files(self) -> None:
        files, extracted = _score_inputs(self)
        result = score(files, extracted, registry_types())
        print(build_report(result), end="")
        self.assertTrue(result.verdict, build_report(result))

    def test_registry_gate_over_invented_file(self) -> None:
        files, extracted = _score_inputs(self, invented_only=True)
        result = score(files, extracted, registry_types())
        print(build_report(result), end="")
        self.assertTrue(result.verdict, build_report(result))

    def test_every_registry_type_is_gated(self) -> None:
        files, extracted = _score_inputs(self)
        result = score(files, extracted, registry_types())
        for object_type in registry_types():
            with self.subTest(object_type):
                self.assertIn(object_type, result.by_type)
                self.assertTrue(result.by_type[object_type].gated)


def _case(
    identifier: str,
    expected: list[dict[str, object]],
    *,
    origin: str = "invented",
    supersedes: str | None = None,
) -> dict[str, object]:
    case: dict[str, object] = {
        "id": identifier,
        "labels": [origin],
        "question": f"[FICTIONAL TEST ONLY] {identifier} has no office value.",
        "expected": {"objects": expected},
    }
    if supersedes is not None:
        case["supersedes"] = supersedes
    return case


def _label(
    object_type: str,
    start: int,
    end: int,
    *,
    key: str | None = None,
    subsections: tuple[str, ...] = (),
) -> dict[str, object]:
    value: dict[str, object] = {
        "type": object_type,
        "start": start,
        "end": end,
        "text": "[FICTIONAL SPAN]",
        "subsections": subsections,
    }
    if key is not None:
        value["key"] = key
    return value


def _object(
    object_type: str,
    start: int,
    end: int,
    *,
    key: str | None = None,
    subsections: tuple[str, ...] = (),
) -> ExactObject:
    return ExactObject(object_type, start, end, "[FICTIONAL SPAN]", key, None, subsections)  # type: ignore[arg-type]


class Scorer(TestCase):
    """The Phase 1 scorer cases, independent of any grammar."""

    def test_hit_requires_type_span_key_and_subsections(self) -> None:
        case = _case(
            "fictional-hit",
            [_label("statute", 4, 12, key="/us/usc/t18/s3663a", subsections=("b",))],
        )
        result = score(
            [[case]],
            {
                "fictional-hit": (
                    _object("statute", 4, 12, key="/US/USC/T18/S3663A", subsections=("b",)),
                )
            },
            ("statute",),
        )
        self.assertEqual(result.by_type["statute"].hits, 1)
        self.assertTrue(result.verdict)

    def test_wrong_key_is_both_a_miss_and_false_hit(self) -> None:
        case = _case(
            "fictional-wrong-key",
            [_label("statute", 4, 12, key="/us/usc/t18/s3663a")],
        )
        result = score(
            [[case]],
            {"fictional-wrong-key": (_object("statute", 4, 12, key="/us/usc/t18/s3663b"),)},
            ("statute",),
        )
        self.assertEqual(result.by_type["statute"].misses, 1)
        self.assertEqual(result.by_type["statute"].false_hits, 1)
        self.assertFalse(result.verdict)

    def test_wrong_subsections_is_both_a_miss_and_false_hit(self) -> None:
        case = _case(
            "fictional-wrong-subsections",
            [_label("statute", 4, 12, key="/us/usc/t18/s3663a", subsections=("b", "1"))],
        )
        result = score(
            [[case]],
            {
                "fictional-wrong-subsections": (
                    _object("statute", 4, 12, key="/us/usc/t18/s3663a", subsections=("b",)),
                )
            },
            ("statute",),
        )
        self.assertEqual(result.by_type["statute"].misses, 1)
        self.assertEqual(result.by_type["statute"].false_hits, 1)

    def test_partial_overlap_is_a_miss_and_false_hit(self) -> None:
        case = _case("fictional-partial", [_label("bare_section", 4, 12)])
        result = score(
            [[case]],
            {"fictional-partial": (_object("bare_section", 4, 11),)},
            ("bare_section",),
        )
        self.assertEqual(result.misses[0].start, 4)
        self.assertEqual(result.false_hits[0].end, 11)
        self.assertFalse(result.verdict)

    def test_superseded_case_is_ignored_across_two_files(self) -> None:
        old = _case(
            "fictional-old",
            [_label("statute", 4, 12, key="/us/usc/t18/s3663a")],
            origin="harvest",
        )
        successor = _case(
            "fictional-successor",
            [_label("statute", 4, 12, key="/us/usc/t18/s3663a")],
            supersedes="fictional-old",
        )
        result = score(
            [[old], [successor]],
            {
                "fictional-old": (_object("statute", 1, 2, key="/us/usc/t18/sbad"),),
                "fictional-successor": (_object("statute", 4, 12, key="/us/usc/t18/s3663a"),),
            },
            ("statute",),
        )
        self.assertEqual(result.by_type["statute"].hits, 1)
        self.assertEqual(result.by_type["statute"].misses, 0)
        self.assertEqual(result.by_type["statute"].false_hits, 0)
        self.assertEqual({finding.case_id for finding in result.false_hits}, set())

    def test_landed_type_with_no_label_fails(self) -> None:
        result = score(
            [[_case("fictional-no-guideline-label", [])]],
            {},
            ("guideline",),
        )
        self.assertTrue(result.by_type["guideline"].gated)
        self.assertIsNone(result.by_type["guideline"].recall)
        self.assertFalse(result.by_type["guideline"].verdict)
        self.assertFalse(result.verdict)

    def test_landed_type_that_emits_nothing_fails_recall(self) -> None:
        result = score(
            [
                [
                    _case(
                        "fictional-guideline-label",
                        [_label("guideline", 4, 12, key="ussg/2B1.1")],
                    )
                ]
            ],
            {},
            ("guideline",),
        )
        self.assertTrue(result.by_type["guideline"].gated)
        self.assertEqual(result.by_type["guideline"].recall, 0.0)
        self.assertFalse(result.verdict)

    def test_planted_failing_type_turns_the_verdict(self) -> None:
        result = score(
            [
                [
                    _case("fictional-statute-pass", [_label("statute", 4, 12, key="/us/usc/t18/s3663a")]),
                    _case("fictional-guideline-fail", [_label("guideline", 4, 12, key="ussg/2B1.1")]),
                ]
            ],
            {
                "fictional-statute-pass": (_object("statute", 4, 12, key="/us/usc/t18/s3663a"),),
                "fictional-guideline-fail": (_object("guideline", 4, 12, key="ussg/2B1.2"),),
            },
            ("statute", "guideline"),
        )
        self.assertTrue(result.by_type["statute"].verdict)
        self.assertFalse(result.by_type["guideline"].verdict)
        self.assertFalse(result.verdict)

    def test_report_names_ids_types_and_offsets_but_not_question_text(self) -> None:
        case = _case("fictional-report-case", [_label("statute", 4, 12, key="/us/usc/t18/s3663a")])
        result = score(
            [[case]],
            {"fictional-report-case": (_object("statute", 4, 11, key="/us/usc/t18/s3663b"),)},
            ("statute",),
        )
        report = build_report(result)
        self.assertIn("miss fictional-report-case statute 4:12", report)
        self.assertIn("false hit fictional-report-case statute 4:11", report)
        self.assertNotIn(case["question"], report)
