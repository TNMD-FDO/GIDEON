"""Validate the human-checked section labels for segmenter eval cases."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from gideon.evaluation.evalset import (
    ROLE_PATTERN,
    SEGMENTER_ID_PATTERN,
    Finding,
    valid_iso_date,
)
from gideon.host.render.worker import SECTION_TYPES

LABELS_PATH: Final[Path] = Path("build-gates", "segmenter-300.labels.jsonl")
LABEL_KEYS: Final[tuple[str, ...]] = ("case_id", "text", "sections", "signed")
TEXT_KEYS: Final[tuple[str, ...]] = ("sha256", "length")
SECTION_KEYS: Final[tuple[str, ...]] = ("start", "end", "type")
SIGNED_KEYS: Final[tuple[str, ...]] = ("by", "on")
LABEL_LINE_FIX: Final[str] = "Correct the segmenter labels JSONL line, then retry."
_FILE_FIX: Final[str] = "Restore the segmenter labels JSONL file, then retry."
_SHA256: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class LabelSpan:
    """One typed interval in canonical-text code points."""

    start: int
    end: int
    section_type: str


@dataclass(frozen=True, slots=True)
class SegmenterLabel:
    """One checked tiling bound to a canonical text hash."""

    case_id: str
    sha256: str
    length: int
    sections: tuple[LabelSpan, ...]
    signed_by: str
    signed_on: str

    def __repr__(self) -> str:
        """Show identifiers and counts without section content."""

        return (
            f"SegmenterLabel(case_id={self.case_id!r}, sha256={self.sha256!r}, "
            f"length={self.length!r}, sections={len(self.sections)}, "
            f"signed_by={self.signed_by!r}, signed_on={self.signed_on!r})"
        )


@dataclass(frozen=True, slots=True)
class LabelReadResult:
    """Valid labels or every finding collected while reading the file."""

    records: tuple[SegmenterLabel, ...] | None = None
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the file was read without findings."""

        return self.records is not None and not self.findings


@dataclass(frozen=True, slots=True)
class LocatedLabel:
    """One valid label and its source line."""

    line: int
    record: SegmenterLabel


@dataclass(frozen=True, slots=True)
class LabelParseResult:
    """Located valid labels and all findings from a byte stream."""

    records: tuple[LocatedLabel, ...] = ()
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether parsing found no fault."""

        return not self.findings


def _finding(file: str, line: int, case_id: str | None, rule: str, fix: str = LABEL_LINE_FIX) -> Finding:
    return Finding(file, case_id, line, rule, fix)


def validate_line(
    value: object, file: str = LABELS_PATH.as_posix(), line: int = 1
) -> tuple[Finding, ...]:
    """Validate one JSON value without including its contents in findings."""

    if not isinstance(value, Mapping):
        return (_finding(file, line, None, "line is not one JSON object"),)
    case_id = value.get("case_id")
    located_id = (
        case_id
        if isinstance(case_id, str) and SEGMENTER_ID_PATTERN.fullmatch(case_id) is not None
        else None
    )
    findings: list[Finding] = []

    def add(rule: str) -> None:
        findings.append(_finding(file, line, located_id, rule))

    if tuple(value) != LABEL_KEYS:
        add("keys or order")
    if not isinstance(case_id, str) or SEGMENTER_ID_PATTERN.fullmatch(case_id) is None:
        add("case_id pattern")

    text = value.get("text")
    length: int | None = None
    if not isinstance(text, Mapping):
        add("text mapping")
    else:
        if tuple(text) != TEXT_KEYS:
            add("text keys or order")
        sha256 = text.get("sha256")
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            add("text.sha256")
        raw_length = text.get("length")
        if not isinstance(raw_length, int) or isinstance(raw_length, bool) or raw_length <= 0:
            add("text.length")
        else:
            length = raw_length

    sections = value.get("sections")
    if not isinstance(sections, list) or not sections:
        add("sections non-empty list")
    else:
        previous_end: int | None = None
        for index, section in enumerate(sections, start=1):
            prefix = f"sections[{index}]"
            if not isinstance(section, Mapping):
                add(f"{prefix} mapping")
                previous_end = None
                continue
            if tuple(section) != SECTION_KEYS:
                add(f"{prefix} keys or order")
            start, end = section.get("start"), section.get("end")
            if (
                not isinstance(start, int) or isinstance(start, bool)
                or not isinstance(end, int) or isinstance(end, bool)
                or start < 0 or end <= start
            ):
                add(f"{prefix} span")
                previous_end = None
            else:
                if index == 1 and start != 0:
                    add(f"{prefix} starts at zero")
                if previous_end is not None and start != previous_end:
                    add(f"{prefix} gap" if start > previous_end else f"{prefix} overlap or unsorted")
                previous_end = end
            if section.get("type") not in SECTION_TYPES:
                add(f"{prefix} type")
        if length is not None and previous_end is not None and previous_end != length:
            add("sections end at text.length")

    signed = value.get("signed")
    if not isinstance(signed, Mapping):
        add("signed mapping")
    else:
        if tuple(signed) != SIGNED_KEYS:
            add("signed keys or order")
        role = signed.get("by")
        if not isinstance(role, str) or ROLE_PATTERN.fullmatch(role) is None:
            add("signed.by role")
        if not valid_iso_date(signed.get("on")):
            add("signed.on ISO date")
    return tuple(findings)


def _record(value: Mapping[str, object]) -> SegmenterLabel:
    text = cast(Mapping[str, object], value["text"])
    signed = cast(Mapping[str, object], value["signed"])
    sections = cast(list[Mapping[str, object]], value["sections"])
    return SegmenterLabel(
        cast(str, value["case_id"]), cast(str, text["sha256"]), cast(int, text["length"]),
        tuple(LabelSpan(cast(int, part["start"]), cast(int, part["end"]), cast(str, part["type"])) for part in sections),
        cast(str, signed["by"]), cast(str, signed["on"]),
    )


def serialize(record: SegmenterLabel) -> str:
    """Serialize one label in its ordered, newline-terminated JSONL form."""

    value = {
        "case_id": record.case_id,
        "text": {"sha256": record.sha256, "length": record.length},
        "sections": [
            {"start": part.start, "end": part.end, "type": part.section_type}
            for part in record.sections
        ],
        "signed": {"by": record.signed_by, "on": record.signed_on},
    }
    return json.dumps(value, ensure_ascii=False) + "\n"


def path_for(set_root: str | Path) -> Path:
    """Return the labels file under one eval-set root."""

    return Path(set_root) / LABELS_PATH


def parse_bytes(data: bytes, file: str = LABELS_PATH.as_posix()) -> LabelParseResult:
    """Parse already-read labels, retaining line locations and all findings."""

    findings: list[Finding] = []
    if data and not data.endswith(b"\n"):
        findings.append(_finding(file, 0, None, "file has no final newline", _FILE_FIX))
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        findings.append(_finding(file, 1, None, "file is not UTF-8", _FILE_FIX))
        return LabelParseResult(findings=tuple(findings))
    records: list[LocatedLabel] = []
    seen: set[str] = set()
    for line, line_text in enumerate(content.splitlines(), start=1):
        try:
            value = json.loads(line_text)
        except json.JSONDecodeError:
            findings.append(_finding(file, line, None, "line is not one JSON object"))
            continue
        line_findings = validate_line(value, file, line)
        findings.extend(line_findings)
        if not line_findings and isinstance(value, Mapping):
            record = _record(value)
            records.append(LocatedLabel(line, record))
            if record.case_id in seen:
                findings.append(_finding(file, line, record.case_id, "case_id appears more than once"))
            seen.add(record.case_id)
    return LabelParseResult(
        records=tuple(records),
        findings=tuple(sorted(findings, key=lambda item: (item.file, item.line or 0, item.id or "", item.rule))),
    )


def read(path: str | Path) -> LabelReadResult:
    """Read the labels file or its set root, collecting all findings."""

    candidate = Path(path)
    file_path = path_for(candidate) if candidate.is_dir() else candidate
    file = LABELS_PATH.as_posix() if candidate.is_dir() else candidate.as_posix()
    try:
        data = file_path.read_bytes()
    except OSError:
        return LabelReadResult(findings=(_finding(file, 0, None, "file cannot be read", _FILE_FIX),))
    parsed = parse_bytes(data, file)
    if parsed.findings:
        return LabelReadResult(findings=parsed.findings)
    return LabelReadResult(records=tuple(located.record for located in parsed.records))
