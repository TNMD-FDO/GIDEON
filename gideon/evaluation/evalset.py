"""Load and validate versioned evaluation sets and their frozen slices.

An extraction case is a harvest, an invented, or a variant case; a variant
names its parent, carries the parent's cluster, and retires with it. A judge
triple is an invented reference-and-candidate grading case.
"""

import hashlib
import json
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final, cast

from gideon.extraction import (
    KEYED_TYPES,
    OBJECT_TYPES,
    SECTION_TYPES,
    ExactObject,
    ObjectType,
    key_violations,
    ordering_violations,
    span_violations,
)

type Case = dict[str, object]

EVAL_SET_VERSION: Final[str] = "eval-v1"
SET_ROOT: Final[Path] = Path("eval", "sets", EVAL_SET_VERSION)
"""The release's eval set, relative to the checkout root."""

_CASE_FIX: Final[str] = "Correct the named JSONL file, then retry."
_SLICE_FIX: Final[str] = "Correct the named slice id list, then retry."
_READ_FIX: Final[str] = "Restore the eval set from the release checkout, then retry."
_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"eval-v[0-9]+")
_EXTRACTION_ID: Final[re.Pattern[str]] = re.compile(r"extraction-[0-9]{3}")
_VARIANT_AXIS: Final[re.Pattern[str]] = re.compile(r"[a-z]+(?:-[a-z]+)*@[1-9][0-9]*")
JUDGMENT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"judgments-[0-9]{3,}")
_TRIPLE_ID: Final[re.Pattern[str]] = re.compile(r"judge-[0-9]{3}")
_HARVEST_ID: Final[re.Pattern[str]] = re.compile(r"HARV-[0-9]{3}")
_EXTRACTION_CLUSTER: Final[re.Pattern[str]] = re.compile(r"harvest-chat-[0-9a-f]+")
_JUDGMENT_CLUSTER: Final[re.Pattern[str]] = re.compile(r"harvest-chat-[A-Za-z0-9_-]+")
ROLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:CHU-attorney|CHU-investigator|CHU-paralegal|TRAD-attorney|"
    r"TRAD-investigator|TRAD-legal-assistant|support|CSA|unknown)-[1-9][0-9]*"
)
_COURT_ID: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")
_QUERY_TYPES: Final[frozenset[str]] = frozenset(
    {"doctrinal", "statute", "case-specific", "other"}
)


@dataclass(frozen=True, slots=True)
class ShapeSpec:
    """The ordered keys and fixed fields for one suite/category shape."""

    suite: str
    category: str
    base_keys: tuple[str, ...]
    fixed_fields: Mapping[str, str]
    id_pattern: re.Pattern[str]
    cluster_pattern: re.Pattern[str]
    review_keys: tuple[str, ...]

    def keys_for(self, record: Mapping[str, object], origin: str | None) -> tuple[str, ...]:
        """Return the exact key order allowed for *record*."""

        keys = list(self.base_keys)
        if self.category == "extraction":
            if origin == "variant":
                keys.extend(("parent", "cluster_id", "review"))
            elif origin == "harvest":
                keys.extend(("seed", "cluster_id", "review"))
            else:
                keys.extend(("cluster_id", "review"))
            if "supersedes" in record:
                keys.append("supersedes")
            keys.append("notes")
        elif self.category == "triples":
            if "supersedes" in record:
                keys.append("supersedes")
            keys.append("notes")
        elif origin == "harvest":
            question_index = keys.index("question") + 1
            optional = [key for key in ("reference_date", "jurisdiction") if key in record]
            keys[question_index:question_index] = optional
            labels_index = keys.index("labels")
            keys[labels_index + 1 : labels_index + 1] = ["seed"]
            if "supersedes" in record:
                keys.append("supersedes")
        elif "supersedes" in record:
            keys.append("supersedes")
        return tuple(keys)


SHAPE_REGISTRY: Final[Mapping[tuple[str, str], ShapeSpec]] = {
    ("build-gates", "extraction"): ShapeSpec(
        "build-gates",
        "extraction",
        ("id", "suite", "category", "branch", "question", "expected", "labels"),
        {"suite": "build-gates", "category": "extraction", "branch": "legal"},
        _EXTRACTION_ID,
        _EXTRACTION_CLUSTER,
        ("by", "on"),
    ),
    ("judgments", "judgments"): ShapeSpec(
        "judgments",
        "judgments",
        (
            "id",
            "suite",
            "category",
            "branch",
            "question",
            "labels",
            "cluster_id",
            "notes",
            "review",
        ),
        {"suite": "judgments", "category": "judgments", "branch": "legal"},
        JUDGMENT_ID_PATTERN,
        _JUDGMENT_CLUSTER,
        ("by", "on", "accepted_flags"),
    ),
    ("judge", "triples"): ShapeSpec(
        "judge",
        "triples",
        (
            "id",
            "suite",
            "category",
            "branch",
            "question",
            "expected",
            "candidate",
            "labels",
            "cluster_id",
            "review",
        ),
        {"suite": "judge", "category": "triples", "branch": "legal"},
        _TRIPLE_ID,
        _TRIPLE_ID,
        ("by", "on"),
    ),
}


@dataclass(frozen=True, slots=True)
class Finding:
    """One content-free eval-set finding, located by file and id or line."""

    file: str
    id: str | None
    line: int | None
    rule: str
    fix: str

    def text(self) -> str:
        """Render the finding without including case question text."""

        location = f"id {self.id}" if self.id is not None else f"line {self.line or 0}"
        return f"{self.file}:{location}: {self.rule} Fix: {self.fix}"


@dataclass(frozen=True, slots=True)
class LoadedSet:
    """The deterministic view of one eval-set directory."""

    version: str
    cases_by_file: Mapping[str, tuple[Case, ...]]
    cases_by_id: Mapping[str, Case]
    active_ids: tuple[str, ...]
    slices: Mapping[str, tuple[str, ...]]
    slice_lists: Mapping[str, Mapping[str, tuple[str, ...]]]
    digest: str


@dataclass(frozen=True, slots=True)
class EvalSetLoadResult:
    """A loaded set or every structural finding collected while reading it."""

    loaded: LoadedSet | None = None
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether loading produced a set without findings."""

        return self.loaded is not None and not self.findings


def print_findings(findings: Iterable[Finding]) -> None:
    """Print every finding on stderr, one per line, in the reported order."""

    for finding in findings:
        print(finding.text(), file=sys.stderr)


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _finding_sort_key(finding: Finding) -> tuple[str, int, str, str]:
    return (finding.file, finding.line or 0, finding.id or "", finding.rule)


def _file_paths(root: Path, suffix: str) -> tuple[Path, ...]:
    suites = sorted(
        (path for path in root.iterdir() if path.is_dir() and path.name != "slices"),
        key=lambda path: _relative(root, path),
    )
    paths = [path for suite in suites for path in suite.rglob(f"*{suffix}") if path.is_file()]
    return tuple(sorted(paths, key=lambda path: _relative(root, path)))


def _slice_dirs(root: Path) -> tuple[Path, ...]:
    slices_root = root / "slices"
    if not slices_root.is_dir():
        return ()
    return tuple(
        sorted(
            (path for path in slices_root.iterdir() if path.is_dir()),
            key=lambda path: _relative(root, path),
        )
    )


def _read_bytes(
    root: Path,
    path: Path,
    findings: list[Finding],
    files: dict[str, bytes],
    fix: str,
) -> bytes | None:
    relative = _relative(root, path)
    try:
        data = path.read_bytes()
    except (OSError, ValueError) as exc:
        findings.append(Finding(relative, None, 0, f"file cannot be read ({exc})", _READ_FIX))
        return None
    files[relative] = data
    if not data.endswith(b"\n"):
        findings.append(Finding(relative, None, 0, "file has no final newline", fix))
    return data


def _case_id(record: Mapping[str, object]) -> str | None:
    value = record.get("id")
    return value if isinstance(value, str) else None


def _origin(record: Mapping[str, object]) -> str | None:
    labels = record.get("labels")
    if isinstance(labels, list) and labels and isinstance(labels[0], str):
        return labels[0]
    return None


def _valid_date(value: object) -> bool:
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _validate_review(
    spec: ShapeSpec,
    review: object,
    file: str,
    case_id: str | None,
    line: int,
    findings: list[Finding],
) -> None:
    if not isinstance(review, dict) or tuple(review) != spec.review_keys:
        findings.append(Finding(file, case_id, line, "review keys or order", _CASE_FIX))
        return
    reviewer = review.get("by")
    if not isinstance(reviewer, str) or ROLE_PATTERN.fullmatch(reviewer) is None:
        findings.append(Finding(file, case_id, line, "review.by role", _CASE_FIX))
    if not _valid_date(review.get("on")):
        findings.append(Finding(file, case_id, line, "review.on ISO date", _CASE_FIX))
    if spec.category == "judgments":
        flags = review.get("accepted_flags")
        if (
            not isinstance(flags, list)
            or any(not isinstance(flag, str) for flag in flags)
            or flags != sorted(flags)
            or len(set(flags)) != len(flags)
        ):
            findings.append(Finding(file, case_id, line, "review.accepted_flags", _CASE_FIX))


def _int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _exact_object(value: Mapping[str, object], object_type: ObjectType) -> ExactObject | None:
    """The label as an object, or ``None`` when a field has the wrong JSON type."""

    start, end, text = value.get("start"), value.get("end"), value.get("text")
    key = value.get("key")
    subsections = value.get("subsections", [])
    if not (
        _int(start)
        and _int(end)
        and isinstance(text, str)
        and (key is None or isinstance(key, str))
        and isinstance(subsections, list)
        and all(isinstance(item, str) for item in subsections)
    ):
        return None
    return ExactObject(object_type, cast(int, start), cast(int, end), text, key, subsections=tuple(subsections))


def _validate_extraction_objects(
    question: str,
    expected: object,
    file: str,
    case_id: str | None,
    line: int,
    findings: list[Finding],
) -> None:
    """Hold each label to the exact-object contract, so the measure never raises on it.

    The fields' presence and JSON types are checked here; the span, the key
    rule, and the ordering are ``gideon.extraction.contract``'s own checks.
    """

    if not isinstance(expected, dict):
        findings.append(Finding(file, case_id, line, "expected mapping", _CASE_FIX))
        return
    values = expected.get("objects")
    if not isinstance(values, list):
        findings.append(Finding(file, case_id, line, "expected.objects list", _CASE_FIX))
        return
    labels: dict[int, ExactObject] = {}
    for index, value in enumerate(values, start=1):
        object_rule = f"expected.objects[{index}]"
        if not isinstance(value, dict):
            findings.append(Finding(file, case_id, line, f"{object_rule} mapping", _CASE_FIX))
            continue
        object_type_value = value.get("type")
        if not isinstance(object_type_value, str) or object_type_value not in OBJECT_TYPES:
            findings.append(Finding(file, case_id, line, f"{object_rule} closed type", _CASE_FIX))
            continue
        object_type = cast(ObjectType, object_type_value)
        expected_keys = (
            ("type", "start", "end", "text")
            + (("key",) if object_type in KEYED_TYPES else ())
            + (("subsections",) if object_type in SECTION_TYPES else ())
        )
        if tuple(value) != expected_keys:
            findings.append(Finding(file, case_id, line, f"{object_rule} keys or order", _CASE_FIX))
        label = _exact_object(value, object_type)
        if label is None:
            findings.append(Finding(file, case_id, line, f"{object_rule} field types", _CASE_FIX))
            continue
        labels[index] = label
    exact = list(labels.values())
    for rule, violations in (
        ("contract span invariant", span_violations(question, exact)),
        ("contract key invariant", key_violations(exact)),
        ("contract ordering invariant", ordering_violations(exact)),
    ):
        findings.extend(
            Finding(file, case_id, line, f"expected.objects[{index}] {rule}", _CASE_FIX)
            for index, label in labels.items()
            if any(label is violation for violation in violations)
        )


def _validate_triple_fields(
    record: Case,
    file: str,
    case_id: str | None,
    line: int,
    findings: list[Finding],
) -> None:
    expected = record.get("expected")
    if not isinstance(expected, dict) or tuple(expected) != ("answer", "band"):
        findings.append(Finding(file, case_id, line, "expected keys or order", _CASE_FIX))
    else:
        answer = expected["answer"]
        if not isinstance(answer, str) or not answer.strip():
            findings.append(Finding(file, case_id, line, "expected.answer", _CASE_FIX))
        band = expected["band"]
        if not (
            isinstance(band, list)
            and len(band) == 2
            and all(_int(value) and 0 <= value <= 3 for value in band)
            and band[0] <= band[1]
        ):
            findings.append(Finding(file, case_id, line, "expected.band", _CASE_FIX))

    candidate = record.get("candidate")
    if not isinstance(candidate, str) or not candidate.strip():
        findings.append(Finding(file, case_id, line, "candidate", _CASE_FIX))


def _validate_shape(
    root: Path,
    path: Path,
    line: int,
    record: Case,
    court_ids: frozenset[str] | None,
    findings: list[Finding],
) -> None:
    relative = _relative(root, path)
    case_id = _case_id(record)
    suite = record.get("suite")
    category = record.get("category")
    spec = SHAPE_REGISTRY.get((suite, category)) if isinstance(suite, str) and isinstance(category, str) else None
    if spec is None:
        findings.append(Finding(relative, case_id, line, "unknown suite/category shape", _CASE_FIX))
        return
    if Path(relative).parts[0] != spec.suite:
        findings.append(Finding(relative, case_id, line, "suite does not match directory", _CASE_FIX))
    if not isinstance(case_id, str) or spec.id_pattern.fullmatch(case_id) is None:
        findings.append(Finding(relative, case_id, line, "id pattern", _CASE_FIX))
    if tuple(record) != spec.keys_for(record, _origin(record)):
        findings.append(Finding(relative, case_id, line, "case keys or order", _CASE_FIX))
    for field, expected in spec.fixed_fields.items():
        if record.get(field) != expected:
            findings.append(Finding(relative, case_id, line, f"{field} must be {expected!r}", _CASE_FIX))

    question = record.get("question")
    if (
        not isinstance(question, str)
        or not question
        or question != question.strip()
        or (spec.category == "judgments" and ("\n" in question or "\r" in question))
    ):
        findings.append(Finding(relative, case_id, line, "question shape", _CASE_FIX))

    labels = record.get("labels")
    origin = _origin(record)
    if not isinstance(labels, list) or len(labels) < 1:
        findings.append(Finding(relative, case_id, line, "labels", _CASE_FIX))
        return
    if spec.category == "extraction":
        if origin not in {"harvest", "invented", "variant"}:
            findings.append(Finding(relative, case_id, line, "labels origin", _CASE_FIX))
        elif origin == "harvest" and (
            len(labels) != 2 or not isinstance(labels[1], str) or labels[1] not in _QUERY_TYPES
        ):
            findings.append(Finding(relative, case_id, line, "harvest labels", _CASE_FIX))
        elif origin == "invented" and len(labels) != 1:
            findings.append(Finding(relative, case_id, line, "invented labels", _CASE_FIX))
        elif origin == "variant" and (
            len(labels) != 2
            or not isinstance(labels[1], str)
            or _VARIANT_AXIS.fullmatch(labels[1]) is None
        ):
            findings.append(Finding(relative, case_id, line, "variant labels", _CASE_FIX))
    elif spec.category == "judgments":
        if len(labels) < 2:
            findings.append(Finding(relative, case_id, line, "labels", _CASE_FIX))
        else:
            wording = labels[1]
            if origin not in {"harvest", "chu-written"}:
                findings.append(Finding(relative, case_id, line, "labels.origin", _CASE_FIX))
            if wording not in {"verbatim", "rewritten"}:
                findings.append(Finding(relative, case_id, line, "labels.wording", _CASE_FIX))
            if origin == "harvest" and (len(labels) != 3 or labels[2] not in _QUERY_TYPES):
                findings.append(Finding(relative, case_id, line, "labels.query_type", _CASE_FIX))
            if origin == "chu-written" and len(labels) != 2:
                findings.append(Finding(relative, case_id, line, "labels", _CASE_FIX))
    elif spec.category == "triples" and (
        len(labels) != 2
        or origin != "invented"
        or not isinstance(labels[1], str)
        or labels[1] not in {"faithful", "wrong", "partial", "unsourced-length"}
    ):
        findings.append(Finding(relative, case_id, line, "labels", _CASE_FIX))

    seed = record.get("seed")
    if origin == "harvest":
        if not isinstance(seed, str) or _HARVEST_ID.fullmatch(seed) is None:
            findings.append(Finding(relative, case_id, line, "seed", _CASE_FIX))
    elif "seed" in record:
        findings.append(Finding(relative, case_id, line, "seed forbidden", _CASE_FIX))

    if origin == "variant":
        parent = record.get("parent")
        if not isinstance(parent, str) or _EXTRACTION_ID.fullmatch(parent) is None:
            findings.append(Finding(relative, case_id, line, "parent", _CASE_FIX))

    cluster_id = record.get("cluster_id")
    if not isinstance(cluster_id, str):
        findings.append(Finding(relative, case_id, line, "cluster_id", _CASE_FIX))
    elif origin == "harvest":
        if spec.cluster_pattern.fullmatch(cluster_id) is None:
            findings.append(Finding(relative, case_id, line, "cluster_id", _CASE_FIX))
    elif origin != "variant" and cluster_id != case_id:
        findings.append(Finding(relative, case_id, line, "cluster_id", _CASE_FIX))

    if not isinstance(record.get("notes"), str):
        findings.append(Finding(relative, case_id, line, "notes", _CASE_FIX))
    if "supersedes" in record and not isinstance(record["supersedes"], str):
        findings.append(Finding(relative, case_id, line, "supersedes string", _CASE_FIX))
    _validate_review(spec, record.get("review"), relative, case_id, line, findings)

    if spec.category == "judgments":
        if "reference_date" in record and not _valid_date(record["reference_date"]):
            findings.append(Finding(relative, case_id, line, "reference_date", _CASE_FIX))
        jurisdiction = record.get("jurisdiction")
        if "jurisdiction" in record and (
            not isinstance(jurisdiction, list)
            or not jurisdiction
            or any(
                not isinstance(item, str) or _COURT_ID.fullmatch(item) is None
                for item in jurisdiction
            )
        ):
            findings.append(Finding(relative, case_id, line, "jurisdiction", _CASE_FIX))
        elif court_ids is not None and isinstance(jurisdiction, list):
            for item in jurisdiction:
                if isinstance(item, str) and item not in court_ids:
                    findings.append(
                        Finding(
                            relative,
                            case_id,
                            line,
                            f"jurisdiction id {item!r} is absent from courts.yaml",
                            _CASE_FIX,
                        )
                    )
    elif spec.category == "extraction":
        if isinstance(question, str):
            _validate_extraction_objects(
                question, record.get("expected"), relative, case_id, line, findings
            )
    elif spec.category == "triples":
        _validate_triple_fields(record, relative, case_id, line, findings)


def _read_cases(
    root: Path,
    paths: Iterable[Path],
    court_ids: frozenset[str] | None,
    findings: list[Finding],
    files: dict[str, bytes],
) -> tuple[dict[str, tuple[Case, ...]], list[tuple[str, int, Case]]]:
    cases_by_file: dict[str, tuple[Case, ...]] = {}
    occurrences: list[tuple[str, int, Case]] = []
    for path in paths:
        relative = _relative(root, path)
        data = _read_bytes(root, path, findings, files, _CASE_FIX)
        if data is None:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            findings.append(Finding(relative, None, 1, f"line is not UTF-8 ({exc})", _CASE_FIX))
            continue
        file_cases: list[Case] = []
        for number, line_text in enumerate(text.splitlines(), start=1):
            try:
                value = json.loads(line_text)
            except json.JSONDecodeError:
                findings.append(Finding(relative, None, number, "line is not one JSON object", _CASE_FIX))
                continue
            if not isinstance(value, dict):
                findings.append(Finding(relative, None, number, "line is not one JSON object", _CASE_FIX))
                continue
            case = cast(Case, value)
            file_cases.append(case)
            _validate_shape(root, path, number, case, court_ids, findings)
            case_id = _case_id(case)
            if case_id is not None:
                occurrences.append((relative, number, case))
        cases_by_file[relative] = tuple(file_cases)
    return cases_by_file, occurrences


def _read_slices(
    root: Path,
    directories: Iterable[Path],
    findings: list[Finding],
    files: dict[str, bytes],
) -> tuple[
    dict[str, tuple[str, ...]],
    dict[str, dict[str, tuple[str, ...]]],
    dict[str, dict[str, tuple[str, int]]],
]:
    slices: dict[str, tuple[str, ...]] = {}
    slice_lists: dict[str, dict[str, tuple[str, ...]]] = {}
    locations: dict[str, dict[str, tuple[str, int]]] = {}
    for directory in directories:
        slice_name = directory.name
        paths = tuple(
            sorted(
                (path for path in directory.rglob("*.ids") if path.is_file()),
                key=lambda path: _relative(root, path),
            )
        )
        if not paths:
            findings.append(Finding(_relative(root, directory), None, 0, "slice directory has no id list", _SLICE_FIX))
            continue
        ids: list[str] = []
        lists: dict[str, tuple[str, ...]] = {}
        seen: dict[str, tuple[str, int]] = {}
        for path in paths:
            relative = _relative(root, path)
            data = _read_bytes(root, path, findings, files, _SLICE_FIX)
            if data is None:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                findings.append(Finding(relative, None, 1, f"line is not UTF-8 ({exc})", _SLICE_FIX))
                continue
            list_ids: list[str] = []
            for number, case_id in enumerate(text.splitlines(), start=1):
                if case_id in seen:
                    previous_file, previous_line = seen[case_id]
                    findings.append(
                        Finding(
                            relative,
                            case_id,
                            number,
                            f"id is listed twice in slice (also {previous_file}:{previous_line})",
                            _SLICE_FIX,
                        )
                    )
                else:
                    seen[case_id] = (relative, number)
                    ids.append(case_id)
                    list_ids.append(case_id)
            lists[path.stem] = tuple(sorted(list_ids))
        slices[slice_name] = tuple(sorted(ids))
        slice_lists[slice_name] = lists
        locations[slice_name] = seen
    return slices, slice_lists, locations


def _set_digest(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(files):
        bytes_digest = hashlib.sha256(files[relative]).hexdigest()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes_digest.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _series(case_id: str) -> tuple[str, int] | None:
    """An id's series and number: a case is earlier only within its series."""

    match = re.fullmatch(r"(.+)-(\d+)", case_id)
    return None if match is None else (match.group(1), int(match.group(2)))


def _supersedes_findings(
    occurrences: Iterable[tuple[str, int, Case]],
    cases_by_id: Mapping[str, Case],
    findings: list[Finding],
) -> set[str]:
    """Refuse a bad ``supersedes``; of two naming one target, the lower id keeps it."""

    superseded: set[str] = set()
    claims = (
        (relative, line, case_id, target)
        for relative, line, case in occurrences
        for case_id, target in ((_case_id(case), case.get("supersedes")),)
        if case_id is not None and isinstance(target, str)
    )
    for relative, line, case_id, target in sorted(claims, key=lambda claim: (_series(claim[2]) or ("", 0), claim[2])):
        case_series = _series(case_id)
        target_series = _series(target)
        rule = None
        if target not in cases_by_id:
            rule = "supersedes names no case"
        elif (
            case_series is None
            or target_series is None
            or target_series[0] != case_series[0]
            or target_series[1] >= case_series[1]
        ):
            rule = "supersedes names no earlier case of its id series"
        elif target in superseded:
            rule = "supersedes names a case another case already supersedes"
        if rule is None:
            superseded.add(target)
        else:
            findings.append(Finding(relative, case_id, line, rule, _CASE_FIX))
    return superseded


def _parent_findings(
    occurrences: Iterable[tuple[str, int, Case]],
    cases_by_id: Mapping[str, Case],
    findings: list[Finding],
) -> None:
    """Refuse a variant whose parent is absent, later, variant, or differently clustered."""

    claims = (
        (relative, line, case_id, parent, case)
        for relative, line, case in occurrences
        for case_id, parent in ((_case_id(case), case.get("parent")),)
        if case_id is not None and _origin(case) == "variant" and isinstance(parent, str)
    )
    for relative, line, case_id, parent, case in sorted(
        claims,
        key=lambda claim: (_series(claim[2]) or ("", 0), claim[2]),
    ):
        case_series = _series(case_id)
        parent_case = cases_by_id.get(parent)
        rule = None
        if parent_case is None:
            rule = "parent names no case"
        else:
            parent_series = _series(parent)
            if (
                case_series is None
                or parent_series is None
                or parent_series[0] != case_series[0]
                or parent_series[1] >= case_series[1]
            ):
                rule = "parent names no earlier case of its id series"
            elif _origin(parent_case) == "variant":
                rule = "parent names a variant"
            elif (
                isinstance(case.get("cluster_id"), str)
                and isinstance(parent_case.get("cluster_id"), str)
                and case["cluster_id"] != parent_case["cluster_id"]
            ):
                rule = "cluster_id differs from the parent's"
        if rule is not None:
            findings.append(Finding(relative, case_id, line, rule, _CASE_FIX))


def load_set(root: str | Path, court_ids: Iterable[str] | None = None) -> EvalSetLoadResult:
    """Load every suite case and frozen slice below *root*, or every finding."""

    set_root = Path(root)
    findings: list[Finding] = []
    if _VERSION_PATTERN.fullmatch(set_root.name) is None:
        findings.append(Finding(set_root.as_posix(), None, 0, "set version must match eval-vN", _READ_FIX))
    court_id_set = None if court_ids is None else frozenset(court_ids)
    files: dict[str, bytes] = {}
    try:
        case_paths = _file_paths(set_root, ".jsonl")
        slice_directories = _slice_dirs(set_root)
    except OSError as exc:
        findings.append(Finding(set_root.as_posix(), None, 0, f"set cannot be read ({exc})", _READ_FIX))
        return EvalSetLoadResult(findings=tuple(sorted(findings, key=_finding_sort_key)))

    cases_by_file, occurrences = _read_cases(
        set_root, case_paths, court_id_set, findings, files
    )
    slices, slice_lists, slice_locations = _read_slices(
        set_root, slice_directories, findings, files
    )

    cases_by_id: dict[str, Case] = {}
    first_occurrence: dict[str, tuple[str, int]] = {}
    for relative, line, case in occurrences:
        case_id = _case_id(case)
        if case_id is None:
            continue
        if case_id in first_occurrence:
            previous_file, previous_line = first_occurrence[case_id]
            findings.append(
                Finding(
                    relative,
                    case_id,
                    line,
                    f"duplicate id (also {previous_file}:{previous_line})",
                    _CASE_FIX,
                )
            )
            continue
        first_occurrence[case_id] = (relative, line)
        cases_by_id[case_id] = case

    superseded = _supersedes_findings(occurrences, cases_by_id, findings)
    _parent_findings(occurrences, cases_by_id, findings)
    for slice_name, ids in slices.items():
        for case_id in ids:
            if case_id not in cases_by_id:
                source_file, line = slice_locations[slice_name][case_id]
                findings.append(Finding(source_file, case_id, line, "slice id resolves to no case", _SLICE_FIX))

    ordered_findings = tuple(sorted(findings, key=_finding_sort_key))
    if ordered_findings:
        return EvalSetLoadResult(findings=ordered_findings)
    # A case whose parent is retired is retired; the loader refuses a variant of
    # a variant, so the rule is one step deep and equals the scorer's transitive one.
    retired = superseded | {
        case_id
        for case_id, case in cases_by_id.items()
        if isinstance(case.get("parent"), str) and case["parent"] in superseded
    }
    return EvalSetLoadResult(
        loaded=LoadedSet(
            version=set_root.name,
            cases_by_file=cases_by_file,
            cases_by_id=cases_by_id,
            active_ids=tuple(sorted(set(cases_by_id) - retired)),
            slices=slices,
            slice_lists=slice_lists,
            digest=_set_digest(files),
        )
    )
