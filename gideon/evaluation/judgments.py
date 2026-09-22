"""Validate the judgments JSONL file and compute grader agreement."""

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, TypeGuard, cast

from gideon.evaluation.evalset import JUDGMENT_ID_PATTERN, Finding

JUDGMENTS_PATH: Final[Path] = Path("judgments", "judgments.jsonl")
JUDGMENT_KEYS: Final[tuple[str, ...]] = (
    "query_id",
    "source_id",
    "sha256",
    "start",
    "end",
    "grade",
    "grader",
    "assessment",
)
ATTORNEY_ROLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:CHU-attorney|TRAD-attorney)-[1-9][0-9]*"
)
SOURCE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._:/-]+")
_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_ASSESSMENTS: Final[frozenset[str]] = frozenset({"primary", "second"})
JUDGMENT_LINE_FIX: Final[str] = "Correct the judgments JSONL line, then retry."
_FILE_FIX: Final[str] = "Restore the judgments JSONL file, then retry."
KAPPA_NO_PAIRS: Final[Literal["no-pairs"]] = "no-pairs"
KAPPA_NO_VARIATION: Final[Literal["no-variation"]] = "no-variation"
KappaValue = float | Literal["no-pairs", "no-variation"]


@dataclass(frozen=True, slots=True)
class Judgment:
    """One content-free grade for one gold-evidence coordinate."""

    query_id: str
    source_id: str
    sha256: str
    start: int
    end: int
    grade: int
    grader: str
    assessment: str


@dataclass(frozen=True, slots=True)
class JudgmentReadResult:
    """The valid judgments or every finding collected while reading them."""

    records: tuple[Judgment, ...] | None = None
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the file was read without a finding."""

        return self.records is not None and not self.findings


@dataclass(frozen=True, slots=True)
class LocatedJudgment:
    """One valid judgment and its source line in the JSONL file."""

    line: int
    record: Judgment


@dataclass(frozen=True, slots=True)
class JudgmentParseResult:
    """Every located judgment the bytes yielded, beside every finding.

    Unlike ``JudgmentReadResult``, the records stand whatever the findings say:
    the loader reports a malformed line and still resolves the query ids of the
    lines that parsed, so one read of the file produces every finding at once.
    """

    records: tuple[LocatedJudgment, ...] = ()
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the bytes parsed without a finding."""

        return not self.findings


@dataclass(frozen=True, slots=True)
class Agreement:
    """Cohen kappa values pooled over passages graded by both readers."""

    pair_count: int
    query_count: int
    four_grade_kappa: KappaValue
    relevant_kappa: KappaValue


def _finding(file: str, line: int, rule: str, fix: str = JUDGMENT_LINE_FIX) -> Finding:
    """Build a content-free finding for a judgments file location."""

    return Finding(file, None, line, rule, fix)


def _integer(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def coordinate_findings(
    value: Mapping[str, object],
    file: str = JUDGMENTS_PATH.as_posix(),
    line: int = 1,
) -> tuple[Finding, ...]:
    """Validate the four gold-evidence coordinate fields without naming values."""

    findings: list[Finding] = []
    source_id = value.get("source_id")
    if (
        not isinstance(source_id, str)
        or not 1 <= len(source_id) <= 200
        or SOURCE_ID_PATTERN.fullmatch(source_id) is None
    ):
        findings.append(_finding(file, line, "source_id"))

    sha256 = value.get("sha256")
    if not isinstance(sha256, str) or _SHA256_PATTERN.fullmatch(sha256) is None:
        findings.append(_finding(file, line, "sha256"))

    start = value.get("start")
    end = value.get("end")
    start_ok = _integer(start) and start >= 0
    end_ok = _integer(end) and end > 0
    if not start_ok:
        findings.append(_finding(file, line, "start"))
    if not end_ok:
        findings.append(_finding(file, line, "end"))
    if start_ok and end_ok and not cast(int, start) < cast(int, end):
        findings.append(_finding(file, line, "coordinates"))
    return tuple(findings)


def validate_line(
    value: object,
    file: str = JUDGMENTS_PATH.as_posix(),
    line: int = 1,
) -> tuple[Finding, ...]:
    """Validate one parsed JSON value without echoing any input value."""

    if not isinstance(value, Mapping):
        return (_finding(file, line, "line is not one JSON object"),)

    findings: list[Finding] = []
    if tuple(value) != JUDGMENT_KEYS:
        findings.append(_finding(file, line, "keys or order"))

    query_id = value.get("query_id")
    if not isinstance(query_id, str) or JUDGMENT_ID_PATTERN.fullmatch(query_id) is None:
        findings.append(_finding(file, line, "query_id pattern"))

    findings.extend(coordinate_findings(value, file, line))

    grade = value.get("grade")
    if not _integer(grade) or not 0 <= grade <= 3:
        findings.append(_finding(file, line, "grade"))

    grader = value.get("grader")
    if not isinstance(grader, str) or ATTORNEY_ROLE_PATTERN.fullmatch(grader) is None:
        findings.append(_finding(file, line, "grader role"))

    assessment = value.get("assessment")
    if not isinstance(assessment, str) or assessment not in _ASSESSMENTS:
        findings.append(_finding(file, line, "assessment"))

    return tuple(findings)


def _record(value: Mapping[str, object]) -> Judgment:
    """Build a record after ``validate_line`` has accepted its fields."""

    return Judgment(
        cast(str, value["query_id"]),
        cast(str, value["source_id"]),
        cast(str, value["sha256"]),
        cast(int, value["start"]),
        cast(int, value["end"]),
        cast(int, value["grade"]),
        cast(str, value["grader"]),
        cast(str, value["assessment"]),
    )


def coordinates(record: Judgment) -> tuple[str, str, int, int]:
    """The record's gold-evidence coordinates, the metrics' key for a passage."""

    return record.source_id, record.sha256, record.start, record.end


def _file_findings(
    records: Iterable[LocatedJudgment],
    file: str,
) -> tuple[Finding, ...]:
    """Apply the cross-line uniqueness rules without naming record values."""

    findings: list[Finding] = []
    triples: set[tuple[str, tuple[str, str, int, int], str]] = set()
    assessments: set[tuple[str, tuple[str, str, int, int], str]] = set()
    for located in records:
        line = located.line
        record = located.record
        located_coordinates = coordinates(record)
        triple = (record.query_id, located_coordinates, record.grader)
        if triple in triples:
            findings.append(_finding(file, line, "query-coordinates-grader appears more than once"))
        else:
            triples.add(triple)
        assessed = (record.query_id, located_coordinates, record.assessment)
        if assessed in assessments:
            findings.append(_finding(file, line, "assessment appears more than once for coordinates"))
        else:
            assessments.add(assessed)
    return tuple(findings)


def serialize(record: Judgment) -> str:
    """Serialize one record in the fixed, newline-terminated JSONL form."""

    value = {key: getattr(record, key) for key in JUDGMENT_KEYS}
    return json.dumps(value, ensure_ascii=False) + "\n"


def path_for(set_root: str | Path) -> Path:
    """Return the judgments file's path under one eval-set root."""

    return Path(set_root) / JUDGMENTS_PATH


def _path_and_file(path: str | Path) -> tuple[Path, str]:
    """Accept either the judgments file itself or the set root holding it."""

    candidate = Path(path)
    if candidate.is_dir():
        return path_for(candidate), JUDGMENTS_PATH.as_posix()
    return candidate, candidate.as_posix()


def parse_bytes(
    data: bytes, file: str = JUDGMENTS_PATH.as_posix()
) -> JudgmentParseResult:
    """Parse already-read judgments bytes and retain each valid record's line."""

    findings: list[Finding] = []
    if data and not data.endswith(b"\n"):
        findings.append(_finding(file, 0, "file has no final newline", _FILE_FIX))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        findings.append(_finding(file, 1, "file is not UTF-8", _FILE_FIX))
        return JudgmentParseResult(findings=tuple(findings))

    records: list[LocatedJudgment] = []
    for line, line_text in enumerate(text.splitlines(), start=1):
        try:
            value = json.loads(line_text)
        except (json.JSONDecodeError, TypeError):
            findings.append(_finding(file, line, "line is not one JSON object"))
            continue
        line_findings = validate_line(value, file, line)
        findings.extend(line_findings)
        if not line_findings and isinstance(value, Mapping):
            records.append(LocatedJudgment(line, _record(value)))

    findings.extend(_file_findings(records, file))
    return JudgmentParseResult(
        records=tuple(records),
        findings=tuple(
            sorted(findings, key=lambda item: (item.file, item.line or 0, item.rule))
        ),
    )


def read(path: str | Path) -> JudgmentReadResult:
    """Read a judgments file, or its set root, and collect all findings."""

    file_path, file = _path_and_file(path)
    try:
        data = file_path.read_bytes()
    except OSError:
        return JudgmentReadResult(
            findings=(_finding(file, 0, "file cannot be read", _FILE_FIX),)
        )

    parsed = parse_bytes(data, file)
    if parsed.findings:
        return JudgmentReadResult(findings=parsed.findings)
    return JudgmentReadResult(
        records=tuple(located.record for located in parsed.records)
    )


def _kappa(pairs: Iterable[tuple[int, int]], *, collapsed: bool) -> KappaValue:
    values = tuple((primary >= 2, second >= 2) if collapsed else (primary, second) for primary, second in pairs)
    if not values:
        return KAPPA_NO_PAIRS

    observed = sum(primary == second for primary, second in values) / len(values)
    primary_counts = Counter(primary for primary, _second in values)
    second_counts = Counter(second for _primary, second in values)
    expected = sum(
        (primary_counts[category] / len(values)) * (second_counts[category] / len(values))
        for category in set(primary_counts) | set(second_counts)
    )
    if expected == 1.0:
        return KAPPA_NO_VARIATION
    return (observed - expected) / (1.0 - expected)


def agreement(records: Iterable[Judgment]) -> Agreement:
    """Compute both pooled Cohen kappa statistics from judgment records."""

    by_coordinate: dict[tuple[str, tuple[str, str, int, int]], dict[str, int]] = {}
    for record in records:
        key = (record.query_id, coordinates(record))
        by_coordinate.setdefault(key, {}).setdefault(record.assessment, record.grade)

    pairs: list[tuple[int, int]] = []
    query_ids: set[str] = set()
    for (query_id, _coordinates_key), grades in by_coordinate.items():
        if "primary" not in grades or "second" not in grades:
            continue
        pairs.append((grades["primary"], grades["second"]))
        query_ids.add(query_id)
    return Agreement(
        pair_count=len(pairs),
        query_count=len(query_ids),
        four_grade_kappa=_kappa(pairs, collapsed=False),
        relevant_kappa=_kappa(pairs, collapsed=True),
    )
