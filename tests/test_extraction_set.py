"""The extraction scorer and frozen JSONL set contracts."""

import hashlib
import json
import re
import warnings
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
)
from gideon.extraction.grammar import registry_types
from gideon.extraction.scoring import SetScore, active_cases, build_report, score
from tools.exportboundary import absent_from_export
from tools.variants.axes import AXES

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
EXTRACTION_PATH: Final[Path] = Path("eval/sets/eval-v1/build-gates/extraction.jsonl")
INVENTED_PATH: Final[Path] = Path(
    "eval/sets/eval-v1/build-gates/extraction-invented.jsonl"
)
EXTRACTION_VARIANTS_PATH: Final[Path] = Path(
    "eval/sets/eval-v1/build-gates/extraction-variants.jsonl"
)
INVENTED_VARIANTS_PATH: Final[Path] = Path(
    "eval/sets/eval-v1/build-gates/extraction-invented-variants.jsonl"
)
SERIES_PATH: Final[Path] = Path("eval/sets/eval-v1/build-gates/series.txt")
"""The id series' files in order: the one record of which file a new id joins."""

HARVEST_PATH: Final[Path] = Path("eval/seed/prototype-qa/harvest.jsonl")
EXTRACTION_PINNED_PREFIXES: Final[tuple[tuple[int, str], ...]] = (
    (40, "dc1e0c928f1860cf7ae068e1ff96fd7418c5d03da2a449a175da26833f2d462e"),  # CSA-1 2026-09-19
)
EXTRACTION_VARIANTS_PINNED_PREFIXES: Final[tuple[tuple[int, str], ...]] = (
    (41, "b7db2d316bab4e354bd66efd77bee94e9316a361b64636778aaa3cba3f07ba3a"),  # CSA-1 2026-09-19
)
INVENTED_PINNED_PREFIXES: Final[tuple[tuple[int, str], ...]] = (
    (22, "8c057174f06ac4afc869a87642ac70eb0a92a3e3b10b2c14f0723d7ee53298a0"),  # CSA-1 2026-09-19
    (55, "3437b4b9e536c67c1a4e909a695afe0c3696fdc04514f0515f0af50c5b020c6e"),  # CSA-1 2026-09-19
)
INVENTED_VARIANTS_PINNED_PREFIXES: Final[tuple[tuple[int, str], ...]] = (
    (218, "cbdbec3e0ff9464f844d4c82d29e9c86ce2fb7c31687a2c58885ec0e65398156"),  # CSA-1 2026-09-19
)
AXIS_IDS: Final[frozenset[str]] = frozenset(axis.axis_id for axis in AXES)


class ExtractionResidualWarning(UserWarning):
    """An allowed scorer residual, identified without question text."""


def _warn_residuals(result: SetScore) -> None:
    """Raise one warning per residual the bounds allowed: ids, types, offsets."""

    with warnings.catch_warnings():
        warnings.simplefilter("default", ExtractionResidualWarning)
        for finding in (*result.misses, *result.false_hits):
            warnings.warn(
                f"{finding.case_id} {finding.type} {finding.start}:{finding.end}",
                ExtractionResidualWarning,
                stacklevel=2,
            )


ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^extraction-[0-9]{3}$")
SCOTUS_PARAGRAPH: Final[re.Pattern[str]] = re.compile(r"[0-9]+[.]([0-9]+)")
"""A Supreme Court Rule's dotted paragraph, its first ``subsections`` entry."""
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


def _label_findings(path: Path, line: ParsedLine) -> list[str]:
    record = line.record
    record_id = _record_id(line)
    location = f"line {line.number} {record_id}"
    question = record.get("question")
    expected = record.get("expected")
    findings: list[str] = []
    if not isinstance(question, str) or not isinstance(expected, dict):
        return findings
    objects_value = expected.get("objects")
    if not isinstance(objects_value, list):
        return findings

    for value in objects_value:
        if not isinstance(value, dict):
            continue
        object_type_value = value.get("type")
        if not isinstance(object_type_value, str) or object_type_value not in OBJECT_TYPES:
            continue
        object_type = cast(ObjectType, object_type_value)
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
            continue
        if not isinstance(text, str) or text != question[start:end]:
            continue

        key = value.get("key")
        if (
            object_type in KEYED_TYPES
            and isinstance(key, str)
            and KEY_PATTERNS[object_type].fullmatch(key) is None
        ):
            findings.append(_finding(path, location, "key form"))

        subsections_value = value.get("subsections")
        if (
            object_type in SECTION_TYPES
            and isinstance(subsections_value, list)
            and all(isinstance(item, str) for item in subsections_value)
        ):
            expected_subsections = tuple(re.findall(r"\(([^()]*)\)", text))
            paragraph = SCOTUS_PARAGRAPH.search(text)
            if object_type == "scotus_rule" and paragraph is not None:
                expected_subsections = (paragraph.group(1), *expected_subsections)
            if tuple(subsections_value) != expected_subsections:
                findings.append(_finding(path, location, "subsections match span"))
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


def _series_paths() -> tuple[Path, ...]:
    """The case files series.txt names, in its order; the file list has one home."""

    directory = SERIES_PATH.parent
    return tuple(directory / name for name in (ROOT / SERIES_PATH).read_text().split())


def _available_case_files(
    test_case: TestCase,
) -> tuple[tuple[Path, bytes, tuple[ParsedLine, ...]], ...]:
    files: list[tuple[Path, bytes, tuple[ParsedLine, ...]]] = []
    for path in _series_paths():
        if absent_from_export(path, ROOT):
            continue
        data, lines, parse_findings = _parse_jsonl(path)
        test_case.assertFalse(parse_findings, f"findings: {parse_findings}")
        files.append((path, data, lines))
    return tuple(files)


def _score_inputs(
    test_case: TestCase,
    *,
    public_only: bool = False,
) -> tuple[tuple[tuple[dict[str, object], ...], ...], dict[str, tuple[ExactObject, ...]]]:
    files = tuple(
        tuple(line.record for line in lines)
        for path, _, lines in _available_case_files(test_case)
        if not public_only or path in {INVENTED_PATH, INVENTED_VARIANTS_PATH}
    )
    active = active_cases(files)
    extracted = {
        cast(str, case["id"]): extract(cast(str, case["question"])) for case in active
    }
    return files, extracted


class SetContract(TestCase):
    """The reviewed extraction JSONL files are frozen data contracts."""

    def test_each_label_matches_the_question(self) -> None:
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
            (INVENTED_VARIANTS_PATH, INVENTED_VARIANTS_PINNED_PREFIXES),
            (EXTRACTION_PATH, EXTRACTION_PINNED_PREFIXES),
            (EXTRACTION_VARIANTS_PATH, EXTRACTION_VARIANTS_PINNED_PREFIXES),
        )
        findings: list[str] = []
        for path, expected_prefixes in prefixes:
            if absent_from_export(path, ROOT):
                continue
            data, lines, parse_findings = _parse_jsonl(path)
            findings.extend(parse_findings)
            findings.extend(_prefix_findings(path, data, expected_prefixes))
        self.assertFalse(findings, f"findings: {findings}")

    def test_ids_form_one_contiguous_series(self) -> None:
        files = _available_case_files(self)
        findings: list[str] = []
        numbers: list[int] = []
        for path, _, lines in files:
            previous = 0
            for line in lines:
                record_id = line.record.get("id")
                location = f"line {line.number} {_record_id(line)}"
                if not isinstance(record_id, str) or ID_PATTERN.fullmatch(record_id) is None:
                    findings.append(_finding(path, location, "id form"))
                    continue
                number = int(record_id.removeprefix("extraction-"))
                if number <= previous:
                    findings.append(_finding(path, location, "ids rise within a file"))
                previous = number
                numbers.append(number)

        # series.txt's order is the order ids were minted in, not ascending, so the
        # one series is contiguous over the union: from 1 here, from 41 in an export.
        first = 41 if absent_from_export(EXTRACTION_PATH, ROOT) else 1
        if sorted(numbers) != list(range(first, first + len(numbers))):
            findings.append(_finding(INVENTED_PATH, 0, "one contiguous id series"))
        self.assertFalse(findings, f"findings: {findings}")

    def test_variant_axes_are_registered(self) -> None:
        files = _available_case_files(self)
        findings: list[str] = []
        for path, _, lines in files:
            if path not in {INVENTED_VARIANTS_PATH, EXTRACTION_VARIANTS_PATH}:
                continue
            for line in lines:
                labels = line.record.get("labels")
                axis = labels[1] if isinstance(labels, list) and len(labels) > 1 else None
                if not isinstance(axis, str) or axis not in AXIS_IDS:
                    findings.append(
                        _finding(path, f"line {line.number} {_record_id(line)}", "variant axis")
                    )
        self.assertFalse(findings, f"findings: {findings}")

    def test_variant_files_follow_the_parent_boundary(self) -> None:
        """A variant sits in the file on its parent's side: no harvest text in a public one."""

        by_path = {path: lines for path, _, lines in _available_case_files(self)}
        findings: list[str] = []
        pairs = (
            (INVENTED_VARIANTS_PATH, INVENTED_PATH),
            (EXTRACTION_VARIANTS_PATH, EXTRACTION_PATH),
        )
        for variant_path, parent_path in pairs:
            if variant_path not in by_path or parent_path not in by_path:
                continue
            parent_ids = {line.record.get("id") for line in by_path[parent_path]}
            for line in by_path[variant_path]:
                if line.record.get("parent") not in parent_ids:
                    location = f"line {line.number} {_record_id(line)}"
                    findings.append(_finding(variant_path, location, "variant parent boundary"))
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
            "regulation": 10,
            "habeas_rule": 10,
            "scotus_rule": 10,
            "appendix_statute": 10,
            "docket": 10,
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
        _warn_residuals(result)
        print(build_report(result), end="")
        self.assertTrue(result.verdict, build_report(result))

    def test_registry_gate_over_public_files(self) -> None:
        files, extracted = _score_inputs(self, public_only=True)
        result = score(files, extracted, registry_types())
        _warn_residuals(result)
        print(build_report(result), end="")
        self.assertTrue(result.verdict, build_report(result))

    def test_every_registry_type_is_gated(self) -> None:
        files, extracted = _score_inputs(self)
        result = score(files, extracted, registry_types())
        for object_type in registry_types():
            with self.subTest(object_type):
                self.assertIn(object_type, result.by_type)
                self.assertTrue(result.by_type[object_type].gated)

    def test_residual_warning_is_bounded_to_id_type_and_offsets(self) -> None:
        case = _case("fictional-residual", [_label("state_code", 4, 12)])
        result = score([[case]], {}, registry_types())
        with self.assertWarns(ExtractionResidualWarning) as raised:
            _warn_residuals(result)
        self.assertEqual(
            str(raised.warning),
            "fictional-residual state_code 4:12",
        )
        self.assertNotIn(case["question"], str(raised.warning))


def _case(
    identifier: str,
    expected: list[dict[str, object]],
    *,
    origin: str = "invented",
    supersedes: str | None = None,
    parent: str | None = None,
) -> dict[str, object]:
    case: dict[str, object] = {
        "id": identifier,
        "labels": [origin],
        "question": f"[FICTIONAL TEST ONLY] {identifier} has no office value.",
        "expected": {"objects": expected},
    }
    if supersedes is not None:
        case["supersedes"] = supersedes
    if parent is not None:
        case["parent"] = parent
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

    def test_variant_is_ignored_with_a_superseded_parent(self) -> None:
        parent = _case("fictional-parent", [])
        replacement = _case("fictional-replacement", [], supersedes="fictional-parent")
        variant = _case("fictional-variant", [], origin="variant", parent="fictional-parent")
        result = score(
            [[parent, replacement, variant]],
            {"fictional-variant": (_object("statute", 4, 12),)},
            ("statute",),
        )
        self.assertEqual(result.by_type["statute"].false_hits, 0)
        self.assertEqual(result.by_type["statute"].misses, 0)

    def test_retired_parent_chain_retires_variants_transitively(self) -> None:
        original = _case("fictional-original", [], supersedes="fictional-parent")
        parent = _case("fictional-parent", [], parent="fictional-original")
        variant = _case("fictional-variant", [], origin="variant", parent="fictional-parent")
        child = _case("fictional-child", [], origin="variant", parent="fictional-variant")
        result = score(
            [[original, parent, variant, child]],
            {
                "fictional-parent": (_object("statute", 1, 2),),
                "fictional-variant": (_object("statute", 2, 3),),
                "fictional-child": (_object("statute", 3, 4),),
            },
            ("statute",),
        )
        self.assertEqual(result.by_type["statute"].false_hits, 0)

    def test_variant_parenting_works_across_two_files(self) -> None:
        parent = _case("fictional-parent", [_label("statute", 4, 12)], origin="harvest")
        variant = _case(
            "fictional-variant",
            [_label("statute", 4, 12)],
            origin="variant",
            parent="fictional-parent",
        )
        result = score(
            [[parent], [variant]],
            {
                "fictional-parent": (_object("statute", 4, 12),),
                "fictional-variant": (_object("statute", 4, 12),),
            },
            ("statute",),
        )
        self.assertEqual(result.by_type["statute"].hits, 2)

    def test_successor_parent_variant_is_counted(self) -> None:
        old_parent = _case("fictional-old-parent", [])
        new_parent = _case(
            "fictional-new-parent", [], supersedes="fictional-old-parent"
        )
        variant = _case(
            "fictional-new-variant",
            [_label("statute", 4, 12)],
            origin="variant",
            parent="fictional-new-parent",
        )
        result = score(
            [[old_parent, new_parent], [variant]],
            {"fictional-new-variant": (_object("statute", 4, 12),)},
            ("statute",),
        )
        self.assertEqual(result.by_type["statute"].hits, 1)

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
