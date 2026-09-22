"""Validate the research-question sign-offs JSONL file."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from gideon.evaluation.evalset import RESEARCH_QA_ID_PATTERN, Finding, valid_iso_date
from gideon.evaluation.judgments import ATTORNEY_ROLE_PATTERN, SOURCE_ID_PATTERN

SIGNOFFS_PATH: Final[Path] = Path("research-qa", "signoffs.jsonl")
SIGNOFF_KEYS: Final[tuple[str, ...]] = ("case_id", "expected", "signed")
EXPECTED_KEYS: Final[tuple[str, ...]] = ("answer", "must_cite")
SIGNED_KEYS: Final[tuple[str, ...]] = ("by", "on")
SIGNOFF_LINE_FIX: Final[str] = "Correct the sign-offs JSONL line, then retry."
_FILE_FIX: Final[str] = "Restore the sign-offs JSONL file, then retry."


@dataclass(frozen=True, slots=True)
class SignOff:
    """One attorney sign-off, with its answer withheld from repr."""

    case_id: str
    answer: str
    must_cite: tuple[str, ...]
    signed_by: str
    signed_on: str

    def __repr__(self) -> str:
        """Keep the attorney-written answer out of diagnostic representations."""

        return (
            "SignOff("
            f"case_id={self.case_id!r}, answer=<redacted>, "
            f"must_cite={self.must_cite!r}, signed_by={self.signed_by!r}, "
            f"signed_on={self.signed_on!r})"
        )


@dataclass(frozen=True, slots=True)
class SignOffReadResult:
    """The valid sign-offs or every finding collected while reading them."""

    records: tuple[SignOff, ...] | None = None
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the file was read without a finding."""

        return self.records is not None and not self.findings


@dataclass(frozen=True, slots=True)
class LocatedSignOff:
    """One valid sign-off and its source line in the JSONL file."""

    line: int
    record: SignOff


@dataclass(frozen=True, slots=True)
class SignOffParseResult:
    """Every located sign-off the bytes yielded, beside every finding."""

    records: tuple[LocatedSignOff, ...] = ()
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the bytes parsed without a finding."""

        return not self.findings


def _finding(
    file: str,
    line: int,
    rule: str,
    fix: str = SIGNOFF_LINE_FIX,
) -> Finding:
    """Build a content-free finding for a sign-offs file location."""

    return Finding(file, None, line, rule, fix)


def validate_line(
    value: object,
    file: str = SIGNOFFS_PATH.as_posix(),
    line: int = 1,
) -> tuple[Finding, ...]:
    """Validate one parsed JSON value without echoing any input value."""

    if not isinstance(value, Mapping):
        return (_finding(file, line, "line is not one JSON object"),)

    findings: list[Finding] = []
    if tuple(value) != SIGNOFF_KEYS:
        findings.append(_finding(file, line, "keys or order"))

    case_id = value.get("case_id")
    if not isinstance(case_id, str) or RESEARCH_QA_ID_PATTERN.fullmatch(case_id) is None:
        findings.append(_finding(file, line, "case_id pattern"))

    expected = value.get("expected")
    if not isinstance(expected, Mapping):
        findings.append(_finding(file, line, "expected mapping"))
    else:
        if tuple(expected) != EXPECTED_KEYS:
            findings.append(_finding(file, line, "expected keys or order"))
        answer = expected.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            findings.append(_finding(file, line, "expected.answer"))
        must_cite = expected.get("must_cite")
        if (
            not isinstance(must_cite, list)
            or not must_cite
            or any(
                not isinstance(source_id, str)
                or SOURCE_ID_PATTERN.fullmatch(source_id) is None
                for source_id in must_cite
            )
        ):
            findings.append(_finding(file, line, "expected.must_cite"))

    signed = value.get("signed")
    if not isinstance(signed, Mapping):
        findings.append(_finding(file, line, "signed mapping"))
    else:
        if tuple(signed) != SIGNED_KEYS:
            findings.append(_finding(file, line, "signed keys or order"))
        signed_by = signed.get("by")
        if (
            not isinstance(signed_by, str)
            or ATTORNEY_ROLE_PATTERN.fullmatch(signed_by) is None
        ):
            findings.append(_finding(file, line, "signed.by role"))
        if not valid_iso_date(signed.get("on")):
            findings.append(_finding(file, line, "signed.on ISO date"))

    return tuple(findings)


def _record(value: Mapping[str, object]) -> SignOff:
    """Build a record after validate_line has accepted its fields."""

    expected = cast(Mapping[str, object], value["expected"])
    signed = cast(Mapping[str, object], value["signed"])
    return SignOff(
        cast(str, value["case_id"]),
        cast(str, expected["answer"]),
        tuple(cast(list[str], expected["must_cite"])),
        cast(str, signed["by"]),
        cast(str, signed["on"]),
    )


def _file_findings(
    records: tuple[LocatedSignOff, ...],
    file: str,
) -> tuple[Finding, ...]:
    """Apply the one-case-id-per-file rule without naming any answer."""

    findings: list[Finding] = []
    seen: set[str] = set()
    for located in records:
        if located.record.case_id in seen:
            findings.append(_finding(file, located.line, "case_id appears more than once"))
        else:
            seen.add(located.record.case_id)
    return tuple(findings)


def serialize(record: SignOff) -> str:
    """Serialize one record in the fixed, newline-terminated JSONL form."""

    value = {
        "case_id": record.case_id,
        "expected": {
            "answer": record.answer,
            "must_cite": list(record.must_cite),
        },
        "signed": {
            "by": record.signed_by,
            "on": record.signed_on,
        },
    }
    return json.dumps(value, ensure_ascii=False) + "\n"


def path_for(set_root: str | Path) -> Path:
    """Return the sign-offs file path under one eval-set root."""

    return Path(set_root) / SIGNOFFS_PATH


def _path_and_file(path: str | Path) -> tuple[Path, str]:
    """Accept either the sign-offs file itself or its set root."""

    candidate = Path(path)
    if candidate.is_dir():
        return path_for(candidate), SIGNOFFS_PATH.as_posix()
    return candidate, candidate.as_posix()


def parse_bytes(
    data: bytes,
    file: str = SIGNOFFS_PATH.as_posix(),
) -> SignOffParseResult:
    """Parse already-read sign-offs bytes and retain valid record line numbers."""

    findings: list[Finding] = []
    if data and not data.endswith(b"\n"):
        findings.append(_finding(file, 0, "file has no final newline", _FILE_FIX))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        findings.append(_finding(file, 1, "file is not UTF-8", _FILE_FIX))
        return SignOffParseResult(findings=tuple(findings))

    records: list[LocatedSignOff] = []
    for line, line_text in enumerate(text.splitlines(), start=1):
        try:
            value = json.loads(line_text)
        except (json.JSONDecodeError, TypeError):
            findings.append(_finding(file, line, "line is not one JSON object"))
            continue
        line_findings = validate_line(value, file, line)
        findings.extend(line_findings)
        if not line_findings and isinstance(value, Mapping):
            records.append(LocatedSignOff(line, _record(value)))

    findings.extend(_file_findings(tuple(records), file))
    return SignOffParseResult(
        records=tuple(records),
        findings=tuple(
            sorted(findings, key=lambda item: (item.file, item.line or 0, item.rule))
        ),
    )


def read(path: str | Path) -> SignOffReadResult:
    """Read a sign-offs file, or its set root, and collect all findings."""

    file_path, file = _path_and_file(path)
    try:
        data = file_path.read_bytes()
    except OSError:
        return SignOffReadResult(
            findings=(_finding(file, 0, "file cannot be read", _FILE_FIX),)
        )

    parsed = parse_bytes(data, file)
    if parsed.findings:
        return SignOffReadResult(findings=parsed.findings)
    return SignOffReadResult(records=tuple(located.record for located in parsed.records))
