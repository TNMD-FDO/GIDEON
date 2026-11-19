"""Read and write content-free model assessments and measure grader agreement."""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from gideon.evaluation.evalset import JUDGMENT_ID_PATTERN, Finding
from gideon.evaluation.judgments import KappaValue, cohen_kappa, coordinate_findings
from gideon.evaluation.rankmetrics import RELEVANT_GRADE, Coordinates
from gideon.host.models import HF_REPO, REVISION

ASSESSMENT_KEYS: Final[tuple[str, ...]] = (
    "query_id", "source_id", "sha256", "start", "end", "grade", "prompt", "model",
)
ASSESSMENTS_LABEL: Final[str] = "assessments"
ASSESSMENTS_FIX: Final[str] = "Correct the assessments JSONL file, then retry."
_PROMPT_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z]+(?:-[a-z]+)*@[1-9][0-9]*")


@dataclass(frozen=True, slots=True)
class Assessment:
    """One model grade for a query and a gold-evidence coordinate."""

    query_id: str
    source_id: str
    sha256: str
    start: int
    end: int
    grade: int
    prompt: str
    model: str


@dataclass(frozen=True, slots=True)
class Assessments:
    """Grades by query and coordinates from one prompt and model."""

    grades: Mapping[str, Mapping[Coordinates, int]]
    prompt: str
    model: str


@dataclass(frozen=True, slots=True)
class AssessmentReadResult:
    """The assessments and digest, or every content-free finding."""

    assessments: Assessments | None = None
    sha256: str | None = None
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the file was read without a finding."""

        return self.assessments is not None and self.sha256 is not None and not self.findings


@dataclass(frozen=True, slots=True)
class AssessmentAgreement:
    """Disagreement counts and Cohen kappa over paired grades."""

    pair_count: int
    four_grade_differing: int
    relevant_differing: int
    four_grade_kappa: KappaValue
    relevant_kappa: KappaValue


def parse_model_identifier(value: str) -> tuple[str, str] | None:
    """Read the repository and revision from the model's file identifier."""

    repository, _, revision = value.partition("@")
    if HF_REPO.fullmatch(repository) is None or REVISION.fullmatch(revision) is None:
        return None
    return repository, revision


def model_identifier(repository: str, revision: str) -> str:
    """Build a model identifier from a repository and revision in lock grammar."""

    identifier = f"{repository}@{revision}"
    if parse_model_identifier(identifier) is None:
        raise ValueError("model identifier requires a Hugging Face repository and revision commit")
    return identifier


def _finding(line: int, rule: str) -> Finding:
    return Finding(ASSESSMENTS_LABEL, None, line, rule, ASSESSMENTS_FIX)


def validate_line(value: object, line: int = 1) -> tuple[Finding, ...]:
    """Validate a parsed assessment object using line and rule alone in findings."""

    if not isinstance(value, Mapping):
        return (_finding(line, "line is not one JSON object"),)

    findings: list[Finding] = []
    if tuple(value) != ASSESSMENT_KEYS:
        findings.append(_finding(line, "keys or order"))

    query_id = value.get("query_id")
    if not isinstance(query_id, str) or JUDGMENT_ID_PATTERN.fullmatch(query_id) is None:
        findings.append(_finding(line, "query_id pattern"))

    findings.extend(
        _finding(line, finding.rule)
        for finding in coordinate_findings(value, ASSESSMENTS_LABEL, line)
    )

    grade = value.get("grade")
    if not isinstance(grade, int) or isinstance(grade, bool) or not 0 <= grade <= 3:
        findings.append(_finding(line, "grade"))

    prompt = value.get("prompt")
    if not isinstance(prompt, str) or _PROMPT_PATTERN.fullmatch(prompt) is None:
        findings.append(_finding(line, "prompt pattern"))

    model = value.get("model")
    if not isinstance(model, str) or parse_model_identifier(model) is None:
        findings.append(_finding(line, "model identifier"))

    return tuple(findings)


def serialize(record: Assessment) -> str:
    """Serialize one assessment in the fixed, newline-terminated form."""

    value = {key: getattr(record, key) for key in ASSESSMENT_KEYS}
    return json.dumps(value, ensure_ascii=False) + "\n"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate JSON key")
    return value


def read(path: str | Path, allowed_query_ids: Iterable[str]) -> AssessmentReadResult:
    """Read an assessments file for the supplied active query ids."""

    try:
        data = Path(path).read_bytes()
    except (OSError, ValueError):
        return AssessmentReadResult(findings=(_finding(0, "file cannot be read"),))
    if not data:
        return AssessmentReadResult(findings=(_finding(0, "file is empty"),))

    findings: list[Finding] = []
    if not data.endswith(b"\n"):
        findings.append(_finding(0, "file has no final newline"))

    allowed = frozenset(allowed_query_ids)
    grades: dict[str, dict[Coordinates, int]] = {}
    assessor: tuple[str, str] | None = None
    for line, line_bytes in enumerate(data.removesuffix(b"\n").split(b"\n"), start=1):
        try:
            value = json.loads(line_bytes.decode("utf-8"), object_pairs_hook=_unique_object)
        except UnicodeDecodeError:
            findings.append(_finding(line, "line is not UTF-8"))
            continue
        except (ValueError, RecursionError):
            findings.append(_finding(line, "line is not one JSON object with unique keys"))
            continue

        line_findings = validate_line(value, line)
        findings.extend(line_findings)
        query_id = value.get("query_id") if isinstance(value, dict) else None
        if (
            isinstance(query_id, str)
            and JUDGMENT_ID_PATTERN.fullmatch(query_id) is not None
            and query_id not in allowed
        ):
            findings.append(_finding(line, "query_id is not allowed"))
        if line_findings:
            continue

        record = Assessment(**value)
        pair = (record.prompt, record.model)
        if assessor is None:
            assessor = pair
        elif pair != assessor:
            findings.append(_finding(line, "prompt and model must match the first assessment"))

        coordinate = (record.source_id, record.sha256, record.start, record.end)
        query_grades = grades.setdefault(record.query_id, {})
        if coordinate in query_grades:
            findings.append(_finding(line, "coordinates appear more than once for query_id"))
        else:
            query_grades[coordinate] = record.grade

    if findings:
        return AssessmentReadResult(
            findings=tuple(sorted(findings, key=lambda item: (item.line or 0, item.rule)))
        )

    assert assessor is not None
    return AssessmentReadResult(
        assessments=Assessments(grades, *assessor), sha256=hashlib.sha256(data).hexdigest()
    )


def agreement(pairs: Iterable[tuple[int, int]]) -> AssessmentAgreement:
    """Compute disagreement over pairs of human and assessor grades."""

    values = tuple(pairs)
    return AssessmentAgreement(
        pair_count=len(values),
        four_grade_differing=sum(human != assessor for human, assessor in values),
        relevant_differing=sum(
            (human >= RELEVANT_GRADE) != (assessor >= RELEVANT_GRADE)
            for human, assessor in values
        ),
        four_grade_kappa=cohen_kappa(values, collapsed=False),
        relevant_kappa=cohen_kappa(values, collapsed=True),
    )
