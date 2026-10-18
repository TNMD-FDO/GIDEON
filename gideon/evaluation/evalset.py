"""Load and validate versioned evaluation sets and their frozen slices.

An extraction case is a harvest, an invented, or a variant case; a variant
names its parent, carries the parent's cluster, and retires with it. A judge
triple is an invented reference-and-candidate grading case. A research-qa case
is one reviewed research question. A harvest case's signed answer lives in the
sign-offs file; a synthetic case carries its own expected answer and retraced
gold evidence. A case is unsigned iff its shape takes a sign-off, it carries no
expected of its own, and no sign-off or label names it — it loads, and a
selection keeps it out of what a gate counts.
The shape's zero_tolerance flag marks categories whose dropped gold fails the
gate and whose synthetic cases require a full read. Other synthetic reviews
record whether the case was read or admitted on the batch's sample.
Guardrails family cases carry an expected turn class and, for positives, a
pattern id with an optional ``must_not`` string or list of regular expressions.
Tier-2 cases carry the instructed turn expectation without a family pattern.
General smoke cases carry the turn harness's expected class and checks.
Segmenter cases carry a drawn opinion's strata; checked section spans live in
their own labels file.
Count-gates cases carry one rule over a generation's build report.
"""

import hashlib
import json
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from gideon.evaluation.turns.cases import EXPECTATIONS, PRESENCE_VALUES
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

if TYPE_CHECKING:
    from gideon.evaluation.judgments import Judgment
    from gideon.evaluation.segmenter_labels import SegmenterLabel
    from gideon.evaluation.signoffs import SignOff

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
_HARVEST_CLUSTER: Final[re.Pattern[str]] = re.compile(r"harvest-chat-[0-9a-f]+")
_JUDGMENT_CLUSTER: Final[re.Pattern[str]] = re.compile(r"harvest-chat-[A-Za-z0-9_-]+")
RESEARCH_QA_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"research-qa-[0-9]{3,}")
SEGMENTER_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"segmenter-[0-9]{3,}")
COUNT_GATES_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"count-gates-[a-z]+(?:-[a-z]+)*-[0-9]{2}"
)
_NO_QUESTION_CATEGORIES: Final[frozenset[str]] = frozenset({"segmenter-300", "count-gates"})
SEGMENTER_TEXT_SOURCES: Final[tuple[str, ...]] = (
    "xml_harvard", "html_columbia", "html_lawbox", "html_anon_2020", "html", "plain_text",
)
TIER_2_CATEGORY: Final[str] = "tier-2-refusals"
GUARDRAIL_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[a-z]+(?:-[a-z0-9]+)*/[a-z]+(?:-[a-z]+)*(?:-[0-9]+)?-[0-9]{2,}"
)
GENERAL_SMOKE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[a-z]+(?:-[a-z]+)*-[0-9]{2,}"
)
_GUARDRAIL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[a-z]+(?:-[a-z]+)*/[a-z]+(?:-[a-z]+)*@[1-9][0-9]*"
)
_GUARDRAIL_TURNS: Final[frozenset[str]] = frozenset({"blocked", "clean", "instructed"})
_SMOKE_EXPECTED_KEYS: Final[tuple[str, ...]] = (
    "expect",
    "block",
    "sources",
    "search",
    "must",
    "must_not",
)
_EXPECTED_BASE_KEYS: Final[tuple[str, ...]] = (
    "id",
    "suite",
    "category",
    "branch",
    "question",
    "expected",
    "labels",
    "cluster_id",
    "review",
)
_QUESTION_BASE_KEYS: Final[tuple[str, ...]] = (
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
_SYNTHETIC_KEYS: Final[tuple[str, ...]] = (
    "id", "suite", "category", "branch", "question", "jurisdiction", "expected",
    "gold", "labels", "template", "cluster_id", "review", "notes",
)
GOLD_COORDINATE_KEYS: Final[tuple[str, ...]] = ("source_id", "sha256", "start", "end", "quote")
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
    """The ordered keys and fixed fields for one suite/category shape.

    ``question_origins`` is the origin vocabulary of a shape whose case is one
    reviewed research question, and empty for every other shape. Non-empty, it
    carries that family's one-line question and court-map checks. Harvest and
    written questions also carry wording labels and accepted review flags;
    synthetic questions use their own keys and labels. ``zero_tolerance`` marks
    shapes whose dropped gold fails the gate and whose synthetic cases require
    a full read; other synthetic reviews also say whether the case was read.
    """

    suite: str
    category: str
    base_keys: tuple[str, ...]
    fixed_fields: Mapping[str, str]
    id_pattern: re.Pattern[str]
    cluster_pattern: re.Pattern[str]
    review_keys: tuple[str, ...]
    question_origins: tuple[str, ...] = ()
    takes_signoff: bool = False
    synthetic_keys: tuple[str, ...] = ()
    zero_tolerance: bool = False
    expected_keys: tuple[str, ...] = ()
    gold_keys: tuple[str, ...] = ()

    @property
    def is_question_case(self) -> bool:
        """Whether this shape's case is one reviewed research question."""

        return bool(self.question_origins)

    def keys_for(self, record: Mapping[str, object], origin: str | None) -> tuple[str, ...]:
        """Return the exact key order allowed for *record*."""

        from gideon.evaluation import synthetic

        if origin == synthetic.SYNTHETIC_ORIGIN and self.synthetic_keys:
            return self.synthetic_keys + (("supersedes",) if "supersedes" in record else ())
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
        elif self.category == "triples" or self.suite in {"guardrails", "general"}:
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

    def review_keys_for(self, origin: str | None) -> tuple[str, ...]:
        """Synthetic reviews carry the reader, date, and sampled read status."""

        from gideon.evaluation import synthetic

        if origin == synthetic.SYNTHETIC_ORIGIN:
            return ("by", "on") if self.zero_tolerance else ("by", "on", "read")
        return self.review_keys

    def expected_keys_for(self) -> tuple[str, ...]:
        """Return the shape's answer keys or the default sign-off keys."""

        from gideon.evaluation import signoffs

        return self.expected_keys or signoffs.EXPECTED_KEYS

    def gold_keys_for(self) -> tuple[str, ...]:
        """Return the shape's evidence keys or the default coordinates."""

        return self.gold_keys or GOLD_COORDINATE_KEYS


SHAPE_REGISTRY: Final[Mapping[tuple[str, str], ShapeSpec]] = {
    ("build-gates", "extraction"): ShapeSpec(
        "build-gates",
        "extraction",
        ("id", "suite", "category", "branch", "question", "expected", "labels"),
        {"suite": "build-gates", "category": "extraction", "branch": "legal"},
        _EXTRACTION_ID,
        _HARVEST_CLUSTER,
        ("by", "on"),
    ),
    ("build-gates", "segmenter-300"): ShapeSpec(
        "build-gates",
        "segmenter-300",
        (
            "id", "suite", "category", "branch", "opinion_id", "court", "decade",
            "text_source", "labels", "cluster_id", "review", "notes",
        ),
        {"suite": "build-gates", "category": "segmenter-300", "branch": "legal"},
        SEGMENTER_ID_PATTERN,
        SEGMENTER_ID_PATTERN,
        ("by", "on"),
        takes_signoff=True,
    ),
    ("build-gates", "count-gates"): ShapeSpec(
        "build-gates",
        "count-gates",
        ("id", "suite", "category", "branch", "rule", "labels", "cluster_id", "review"),
        {"suite": "build-gates", "category": "count-gates", "branch": "legal"},
        COUNT_GATES_ID_PATTERN,
        COUNT_GATES_ID_PATTERN,
        ("by", "on"),
    ),
    ("judgments", "judgments"): ShapeSpec(
        "judgments",
        "judgments",
        _QUESTION_BASE_KEYS,
        {"suite": "judgments", "category": "judgments", "branch": "legal"},
        JUDGMENT_ID_PATTERN,
        _JUDGMENT_CLUSTER,
        ("by", "on", "accepted_flags"),
        question_origins=("harvest", "chu-written"),
    ),
    ("research-qa", "retrieval"): ShapeSpec(
        "research-qa",
        "retrieval",
        _QUESTION_BASE_KEYS,
        {"suite": "research-qa", "category": "retrieval", "branch": "legal"},
        RESEARCH_QA_ID_PATTERN,
        _HARVEST_CLUSTER,
        ("by", "on", "accepted_flags"),
        question_origins=("harvest", "synthetic"),
        takes_signoff=True,
        synthetic_keys=_SYNTHETIC_KEYS,
    ),
    ("research-qa", "synthesis"): ShapeSpec(
        "research-qa",
        "synthesis",
        _QUESTION_BASE_KEYS,
        {"suite": "research-qa", "category": "synthesis", "branch": "legal"},
        RESEARCH_QA_ID_PATTERN,
        _HARVEST_CLUSTER,
        ("by", "on", "accepted_flags"),
        question_origins=("harvest", "synthetic"),
        takes_signoff=True,
        synthetic_keys=_SYNTHETIC_KEYS,
    ),
    ("research-qa", "lookup"): ShapeSpec(
        "research-qa",
        "lookup",
        _QUESTION_BASE_KEYS,
        {"suite": "research-qa", "category": "lookup", "branch": "legal"},
        RESEARCH_QA_ID_PATTERN,
        _HARVEST_CLUSTER,
        ("by", "on", "accepted_flags"),
        question_origins=("harvest", "synthetic"),
        takes_signoff=True,
        synthetic_keys=_SYNTHETIC_KEYS,
        zero_tolerance=True,
    ),
    ("research-qa", "edition"): ShapeSpec(
        "research-qa",
        "edition",
        _QUESTION_BASE_KEYS,
        {"suite": "research-qa", "category": "edition", "branch": "legal"},
        RESEARCH_QA_ID_PATTERN,
        _HARVEST_CLUSTER,
        ("by", "on", "accepted_flags"),
        question_origins=("harvest",),
        takes_signoff=True,
        zero_tolerance=True,
    ),
    ("research-qa", "existence"): ShapeSpec(
        "research-qa",
        "existence",
        _QUESTION_BASE_KEYS,
        {"suite": "research-qa", "category": "existence", "branch": "legal"},
        RESEARCH_QA_ID_PATTERN,
        _HARVEST_CLUSTER,
        ("by", "on", "accepted_flags"),
        question_origins=("synthetic",),
        synthetic_keys=_SYNTHETIC_KEYS,
    ),
    ("research-qa", "treatment"): ShapeSpec(
        "research-qa",
        "treatment",
        _QUESTION_BASE_KEYS,
        {"suite": "research-qa", "category": "treatment", "branch": "legal"},
        RESEARCH_QA_ID_PATTERN,
        _HARVEST_CLUSTER,
        ("by", "on", "accepted_flags"),
        question_origins=("synthetic",),
        takes_signoff=False,
        synthetic_keys=_SYNTHETIC_KEYS,
        zero_tolerance=True,
        expected_keys=("answer", "must_cite", "state"),
        gold_keys=GOLD_COORDINATE_KEYS + ("citation_id", "signal_id", "pattern_set", "cluster"),
    ),
    ("research-qa", "section-attribution"): ShapeSpec(
        "research-qa",
        "section-attribution",
        _QUESTION_BASE_KEYS,
        {"suite": "research-qa", "category": "section-attribution", "branch": "legal"},
        RESEARCH_QA_ID_PATTERN,
        _HARVEST_CLUSTER,
        ("by", "on", "accepted_flags"),
        question_origins=("synthetic",),
        takes_signoff=False,
        synthetic_keys=_SYNTHETIC_KEYS,
        zero_tolerance=True,
        expected_keys=("answer", "must_cite", "section_type"),
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
    ("guardrails", "deadline-trap"): ShapeSpec(
        "guardrails",
        "deadline-trap",
        _EXPECTED_BASE_KEYS,
        {"suite": "guardrails", "category": "deadline-trap", "branch": "general"},
        GUARDRAIL_ID_PATTERN,
        GUARDRAIL_ID_PATTERN,
        ("by", "on"),
    ),
    ("guardrails", "guidelines-range"): ShapeSpec(
        "guardrails",
        "guidelines-range",
        _EXPECTED_BASE_KEYS,
        {"suite": "guardrails", "category": "guidelines-range", "branch": "general"},
        GUARDRAIL_ID_PATTERN,
        GUARDRAIL_ID_PATTERN,
        ("by", "on"),
    ),
    ("guardrails", "sentence-credit"): ShapeSpec(
        "guardrails",
        "sentence-credit",
        _EXPECTED_BASE_KEYS,
        {"suite": "guardrails", "category": "sentence-credit", "branch": "general"},
        GUARDRAIL_ID_PATTERN,
        GUARDRAIL_ID_PATTERN,
        ("by", "on"),
    ),
    ("guardrails", TIER_2_CATEGORY): ShapeSpec(
        "guardrails",
        TIER_2_CATEGORY,
        _EXPECTED_BASE_KEYS,
        {"suite": "guardrails", "category": TIER_2_CATEGORY, "branch": "general"},
        GUARDRAIL_ID_PATTERN,
        GUARDRAIL_ID_PATTERN,
        ("by", "on"),
    ),
    ("general", "smoke"): ShapeSpec(
        "general",
        "smoke",
        _EXPECTED_BASE_KEYS,
        {"suite": "general", "category": "smoke", "branch": "general"},
        GENERAL_SMOKE_ID_PATTERN,
        GENERAL_SMOKE_ID_PATTERN,
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
        if self.id is not None and self.line is not None:
            location += f" line {self.line}"
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
    judgments: tuple["Judgment", ...] = ()
    signoffs: tuple["SignOff", ...] = ()
    segmenter_labels: tuple["SegmenterLabel", ...] = ()
    unsigned_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Selection:
    """The counted and unsigned active ids selected from one slice.

    ``takes_signoff`` is whether the slice lists any active case whose shape
    takes a sign-off at all, which is what decides that the run row prints its
    exclusion clause — a fully signed slice still prints it, reading zero.
    """

    counted: tuple[str, ...]
    unsigned: tuple[str, ...]
    takes_signoff: bool = False


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


def _file_paths(root: Path, suffix: str, excluded_relatives: frozenset[str]) -> tuple[Path, ...]:
    """Every case file below *root*, less the relative paths named.

    The judgments, sign-offs, and segmenter labels files share suite directories
    and the suffix with case files, so they are skipped by name: a suffix rule
    would take a later case file with them.
    """

    suites = sorted(
        (path for path in root.iterdir() if path.is_dir() and path.name != "slices"),
        key=lambda path: _relative(root, path),
    )
    paths = [
        path
        for suite in suites
        for path in suite.rglob(f"*{suffix}")
        if path.is_file() and _relative(root, path) not in excluded_relatives
    ]
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
    *,
    check_final_newline: bool = True,
) -> bytes | None:
    relative = _relative(root, path)
    try:
        data = path.read_bytes()
    except (OSError, ValueError) as exc:
        findings.append(Finding(relative, None, 0, f"file cannot be read ({exc})", _READ_FIX))
        return None
    files[relative] = data
    if check_final_newline and not data.endswith(b"\n"):
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


def _shape_for(record: Mapping[str, object]) -> ShapeSpec | None:
    """The registered shape for a record's suite and category, or ``None``."""

    suite = record.get("suite")
    category = record.get("category")
    if not isinstance(suite, str) or not isinstance(category, str):
        return None
    return SHAPE_REGISTRY.get((suite, category))


def valid_iso_date(value: object) -> bool:
    """Whether *value* is an ISO date string equal to its own normal form."""

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
    *,
    origin: str | None = None,
) -> None:
    from gideon.evaluation import synthetic

    keys = spec.review_keys_for(origin)
    if not isinstance(review, dict) or tuple(review) != keys:
        findings.append(Finding(file, case_id, line, "review keys or order", _CASE_FIX))
        return
    reviewer = review.get("by")
    if (
        not isinstance(reviewer, str)
        or ROLE_PATTERN.fullmatch(reviewer) is None
        or (origin == synthetic.SYNTHETIC_ORIGIN and not reviewer.startswith("CSA-"))
    ):
        findings.append(Finding(file, case_id, line, "review.by role", _CASE_FIX))
    if not valid_iso_date(review.get("on")):
        findings.append(Finding(file, case_id, line, "review.on ISO date", _CASE_FIX))
    if "read" in keys and not isinstance(review.get("read"), bool):
        findings.append(Finding(file, case_id, line, "review.read bool", _CASE_FIX))
    if spec.is_question_case and origin != synthetic.SYNTHETIC_ORIGIN:
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
    from gideon.evaluation import synthetic

    relative = _relative(root, path)
    case_id = _case_id(record)
    spec = _shape_for(record)
    if spec is None:
        findings.append(Finding(relative, case_id, line, "unknown suite/category shape", _CASE_FIX))
        return
    if Path(relative).parts[0] != spec.suite:
        findings.append(Finding(relative, case_id, line, "suite does not match directory", _CASE_FIX))
    origin = _origin(record)
    id_pattern = (
        synthetic.synthetic_id_pattern(spec.category)
        if origin == synthetic.SYNTHETIC_ORIGIN and spec.synthetic_keys else spec.id_pattern
    )
    if not isinstance(case_id, str) or id_pattern.fullmatch(case_id) is None:
        findings.append(Finding(relative, case_id, line, "id pattern", _CASE_FIX))
    if tuple(record) != spec.keys_for(record, _origin(record)):
        findings.append(Finding(relative, case_id, line, "case keys or order", _CASE_FIX))
    for field, expected in spec.fixed_fields.items():
        if record.get(field) != expected:
            findings.append(Finding(relative, case_id, line, f"{field} must be {expected!r}", _CASE_FIX))

    question = record.get("question")
    if spec.category not in _NO_QUESTION_CATEGORIES and (
        not isinstance(question, str)
        or not question
        or question != question.strip()
        or (spec.is_question_case and ("\n" in question or "\r" in question))
    ):
        findings.append(Finding(relative, case_id, line, "question shape", _CASE_FIX))

    labels = record.get("labels")
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
    elif spec.is_question_case:
        if origin not in spec.question_origins:
            findings.append(Finding(relative, case_id, line, "labels.origin", _CASE_FIX))
        if origin == synthetic.SYNTHETIC_ORIGIN:
            if labels not in ([synthetic.SYNTHETIC_ORIGIN], [synthetic.SYNTHETIC_ORIGIN, synthetic.CLOSED_BOOK_LABEL]):
                findings.append(Finding(relative, case_id, line, "synthetic labels", _CASE_FIX))
            if spec.synthetic_keys:
                _validate_synthetic_fields(spec, record, relative, case_id, line, findings)
        elif len(labels) < 2:
            findings.append(Finding(relative, case_id, line, "labels", _CASE_FIX))
        else:
            wording = labels[1]
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
    elif spec.category == "segmenter-300":
        if labels != ["drawn"]:
            findings.append(Finding(relative, case_id, line, "labels must be drawn", _CASE_FIX))
        opinion_id = record.get("opinion_id")
        if not isinstance(opinion_id, int) or isinstance(opinion_id, bool) or opinion_id <= 0:
            findings.append(Finding(relative, case_id, line, "opinion_id positive integer", _CASE_FIX))
        court = record.get("court")
        if not isinstance(court, str) or _COURT_ID.fullmatch(court) is None:
            findings.append(Finding(relative, case_id, line, "court id", _CASE_FIX))
        elif court_ids is not None and court not in court_ids:
            findings.append(Finding(relative, case_id, line, "court absent from courts.yaml", _CASE_FIX))
        decade = record.get("decade")
        if not isinstance(decade, int) or isinstance(decade, bool) or decade % 10 != 0:
            findings.append(Finding(relative, case_id, line, "decade multiple of ten", _CASE_FIX))
        if record.get("text_source") not in SEGMENTER_TEXT_SOURCES:
            findings.append(Finding(relative, case_id, line, "text_source vocabulary", _CASE_FIX))
    elif spec.category == "count-gates":
        if labels != ["spec"]:
            findings.append(Finding(relative, case_id, line, "labels must be spec", _CASE_FIX))
        rule = record.get("rule")
        if (
            not isinstance(rule, str)
            or not rule
            or rule != rule.strip()
            or "\n" in rule
            or "\r" in rule
        ):
            findings.append(Finding(relative, case_id, line, "rule shape", _CASE_FIX))
    elif spec.suite == "guardrails":
        _validate_guardrail_fields(record, relative, case_id, line, findings)
    elif spec.suite == "general":
        _validate_smoke_fields(record, relative, case_id, line, findings)

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
    elif origin not in {"variant", synthetic.SYNTHETIC_ORIGIN} and cluster_id != case_id:
        findings.append(Finding(relative, case_id, line, "cluster_id", _CASE_FIX))

    if "notes" in spec.keys_for(record, origin) and not isinstance(record.get("notes"), str):
        findings.append(Finding(relative, case_id, line, "notes", _CASE_FIX))
    if "supersedes" in record and not isinstance(record["supersedes"], str):
        findings.append(Finding(relative, case_id, line, "supersedes string", _CASE_FIX))
    _validate_review(spec, record.get("review"), relative, case_id, line, findings, origin=origin)

    if spec.is_question_case:
        if "reference_date" in record and not valid_iso_date(record["reference_date"]):
            findings.append(Finding(relative, case_id, line, "reference_date", _CASE_FIX))
        jurisdiction = record.get("jurisdiction")
        if ("jurisdiction" in record or origin == synthetic.SYNTHETIC_ORIGIN) and (
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


def case_findings(
    record: Case, relative: str, line: int, court_ids: Iterable[str] | None = None,
) -> tuple[Finding, ...]:
    """Validate one case before intake, using the loader's shape checks."""

    findings: list[Finding] = []
    _validate_shape(
        Path("."), Path(relative), line, record,
        None if court_ids is None else frozenset(court_ids), findings,
    )
    return tuple(findings)


def _validate_synthetic_fields(
    spec: ShapeSpec, record: Case, file: str, case_id: str | None, line: int, findings: list[Finding],
) -> None:
    """Validate a generated answer and its code-retraced evidence coordinates."""

    from gideon.evaluation import judgments, synthetic
    from gideon.host.index.treatment import PATTERN_SET_PATTERN
    from gideon.host.render.worker import SECTION_TYPES, TREATMENT_STATES

    template = record.get("template")
    if (
        not isinstance(template, str)
        or synthetic.TEMPLATE_PATTERN.fullmatch(template) is None
        or not template.startswith(f"{record['category']}@")
        or template not in synthetic.TEMPLATES
    ):
        findings.append(Finding(file, case_id, line, "template", _CASE_FIX))
    expected = record.get("expected")
    must_cite: list[object] = []
    if not isinstance(expected, Mapping):
        findings.append(Finding(file, case_id, line, "expected mapping", _CASE_FIX))
    else:
        if tuple(expected) != spec.expected_keys_for():
            findings.append(Finding(file, case_id, line, "expected keys or order", _CASE_FIX))
        answer = expected.get("answer")
        if (
            not isinstance(answer, str) or not answer.strip()
            or "\n" in answer or "\r" in answer
        ):
            findings.append(Finding(file, case_id, line, "expected.answer", _CASE_FIX))
        cites = expected.get("must_cite")
        if isinstance(cites, list):
            must_cite = cites
        if (
            not isinstance(cites, list) or not cites
            or any(not isinstance(item, str) or judgments.SOURCE_ID_PATTERN.fullmatch(item) is None for item in cites)
            or len(set(cites)) != len(cites)
        ):
            findings.append(Finding(file, case_id, line, "expected.must_cite", _CASE_FIX))
        if "state" in spec.expected_keys_for() and expected.get("state") not in (*TREATMENT_STATES, synthetic.NO_STATE):
            findings.append(Finding(file, case_id, line, "expected.state", _CASE_FIX))
        if "section_type" in spec.expected_keys_for() and expected.get("section_type") not in SECTION_TYPES:
            findings.append(Finding(file, case_id, line, "expected.section_type", _CASE_FIX))
    gold = record.get("gold")
    if not isinstance(gold, list) or not gold:
        findings.append(Finding(file, case_id, line, "gold non-empty list", _CASE_FIX))
        return
    for index, entry in enumerate(gold, start=1):
        rule = f"gold[{index}]"
        if not isinstance(entry, Mapping):
            findings.append(Finding(file, case_id, line, f"{rule} mapping", _CASE_FIX))
            continue
        if tuple(entry) != spec.gold_keys_for():
            findings.append(Finding(file, case_id, line, f"{rule} keys or order", _CASE_FIX))
        findings.extend(
            Finding(file, case_id, line, f"{rule}.{finding.rule}", _CASE_FIX)
            for finding in judgments.coordinate_findings(entry, file, line)
        )
        for key in ("citation_id", "signal_id"):
            if key in spec.gold_keys_for():
                value = entry.get(key)
                if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                    findings.append(Finding(file, case_id, line, f"{rule}.{key}", _CASE_FIX))
        if "pattern_set" in spec.gold_keys_for():
            pattern_set = entry.get("pattern_set")
            if not isinstance(pattern_set, str) or re.fullmatch(PATTERN_SET_PATTERN, pattern_set) is None:
                findings.append(Finding(file, case_id, line, f"{rule}.pattern_set", _CASE_FIX))
        if "cluster" in spec.gold_keys_for():
            cluster = entry.get("cluster")
            if not _int(cluster) or cast(int, cluster) <= 0:
                findings.append(Finding(file, case_id, line, f"{rule}.cluster", _CASE_FIX))
        quote, start, end = entry.get("quote"), entry.get("start"), entry.get("end")
        if (
            not isinstance(quote, str) or not quote
            or (_int(start) and _int(end) and len(quote) != cast(int, end) - cast(int, start))
        ):
            findings.append(Finding(file, case_id, line, f"{rule}.quote length", _CASE_FIX))
        if entry.get("source_id") not in must_cite:
            findings.append(Finding(file, case_id, line, f"{rule}.source_id absent from expected.must_cite", _CASE_FIX))
    if isinstance(gold[0], Mapping) and record.get("cluster_id") != gold[0].get("source_id"):
        findings.append(Finding(file, case_id, line, "cluster_id must equal first gold source_id", _CASE_FIX))


def _validate_guardrail_fields(
    record: Case,
    file: str,
    case_id: str | None,
    line: int,
    findings: list[Finding],
) -> None:
    """Validate the guardrail role, expected turn, pattern, and qualified id."""

    category = record.get("category")
    if not isinstance(case_id, str) or not isinstance(category, str) or not case_id.startswith(f"{category}/"):
        findings.append(Finding(file, case_id, line, "id category prefix", _CASE_FIX))

    labels = record.get("labels")
    expected = record.get("expected")
    if (
        not isinstance(labels, list)
        or len(labels) != 2
        or labels[0] != "invented"
        or not isinstance(labels[1], str)
        or labels[1] not in {"positive", "control"}
    ):
        findings.append(Finding(file, case_id, line, "labels", _CASE_FIX))
        return
    if not isinstance(expected, dict):
        findings.append(Finding(file, case_id, line, "expected mapping", _CASE_FIX))
        return

    role = labels[1]
    if category == TIER_2_CATEGORY:
        if role != "positive":
            findings.append(Finding(file, case_id, line, "labels", _CASE_FIX))
        if tuple(expected) != ("turn",):
            findings.append(Finding(file, case_id, line, "expected keys or order", _CASE_FIX))
            return
        if expected.get("turn") != "instructed":
            findings.append(Finding(file, case_id, line, "expected.turn role", _CASE_FIX))
        return

    expected_keys = ("turn", "pattern") if role == "positive" else ("turn",)
    actual_keys = tuple(expected)
    if actual_keys != expected_keys and not (
        role == "positive" and actual_keys == (*expected_keys, "must_not")
    ):
        findings.append(Finding(file, case_id, line, "expected keys or order", _CASE_FIX))
        return
    expected_turn = "blocked" if role == "positive" else "clean"
    turn = expected.get("turn")
    if not isinstance(turn, str) or turn not in _GUARDRAIL_TURNS or turn != expected_turn:
        findings.append(Finding(file, case_id, line, "expected.turn role", _CASE_FIX))
    if role == "positive":
        pattern = expected.get("pattern")
        if not isinstance(pattern, str) or _GUARDRAIL_PATTERN.fullmatch(pattern) is None:
            findings.append(Finding(file, case_id, line, "expected.pattern", _CASE_FIX))
        if "must_not" in expected:
            must_not = expected["must_not"]
            if isinstance(must_not, str) and must_not:
                patterns = [must_not]
            elif (
                isinstance(must_not, list)
                and must_not
                and all(isinstance(item, str) and item for item in must_not)
            ):
                patterns = must_not
            else:
                findings.append(Finding(file, case_id, line, "expected.must_not", _CASE_FIX))
                return
            try:
                for item in patterns:
                    re.compile(item)
            except re.error:
                findings.append(Finding(file, case_id, line, "expected.must_not", _CASE_FIX))


def _validate_smoke_fields(
    record: Case,
    file: str,
    case_id: str | None,
    line: int,
    findings: list[Finding],
) -> None:
    """Validate General smoke labels and its turn-harness checks."""

    if record.get("labels") != ["invented"]:
        findings.append(Finding(file, case_id, line, "labels", _CASE_FIX))
    expected = record.get("expected")
    if not isinstance(expected, dict):
        findings.append(Finding(file, case_id, line, "expected mapping", _CASE_FIX))
        return
    if tuple(expected) != _SMOKE_EXPECTED_KEYS:
        findings.append(Finding(file, case_id, line, "expected keys or order", _CASE_FIX))
    value = expected.get("expect")
    if not isinstance(value, str) or value not in EXPECTATIONS:
        findings.append(Finding(file, case_id, line, "expected.expect", _CASE_FIX))
    for field in ("block", "sources"):
        value = expected.get(field)
        if not isinstance(value, str) or value not in PRESENCE_VALUES:
            findings.append(Finding(file, case_id, line, f"expected.{field}", _CASE_FIX))
    if not isinstance(expected.get("search"), bool):
        findings.append(Finding(file, case_id, line, "expected.search", _CASE_FIX))
    for field in ("must", "must_not"):
        patterns = expected.get(field)
        if not isinstance(patterns, list) or any(not isinstance(item, str) for item in patterns):
            findings.append(Finding(file, case_id, line, f"expected.{field}", _CASE_FIX))
            continue
        try:
            for pattern in patterns:
                re.compile(pattern)
        except re.error:
            findings.append(Finding(file, case_id, line, f"expected.{field} regex", _CASE_FIX))


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
    occurrences = tuple(occurrences)
    locations = {
        case_id: (relative, line)
        for relative, line, case in occurrences
        for case_id in (_case_id(case),)
        if case_id is not None
    }
    claims = (
        (relative, line, case_id, target)
        for relative, line, case in occurrences
        for case_id, target in ((_case_id(case), case.get("supersedes")),)
        if case_id is not None and isinstance(target, str)
    )
    for relative, line, case_id, target in sorted(claims, key=lambda claim: (_series(claim[2]) or ("", 0), claim[2])):
        case_series = _series(case_id)
        target_series = _series(target)
        # A guardrails seed retires a case under another stem (`control-30`
        # supersedes `thinking-01`), so there "earlier" is an earlier line of
        # the same file rather than a lower number of the same series.
        target_location = locations.get(target)
        earlier_guardrail_line = (
            cases_by_id.get(case_id, {}).get("suite") == "guardrails"
            and target_location is not None
            and target_location[0] == relative
            and target_location[1] < line
        )
        rule = None
        if target not in cases_by_id:
            rule = "supersedes names no case"
        elif not earlier_guardrail_line and (
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

    # The auxiliary file readers import Finding from this module; keep their
    # imports here so the loader does not create an import cycle.
    from gideon.evaluation import judgments, segmenter_labels, signoffs, synthetic

    set_root = Path(root)
    findings: list[Finding] = []
    if _VERSION_PATTERN.fullmatch(set_root.name) is None:
        findings.append(Finding(set_root.as_posix(), None, 0, "set version must match eval-vN", _READ_FIX))
    court_id_set = None if court_ids is None else frozenset(court_ids)
    judgments_file = judgments.JUDGMENTS_PATH.as_posix()
    signoffs_file = signoffs.SIGNOFFS_PATH.as_posix()
    labels_file = segmenter_labels.LABELS_PATH.as_posix()
    files: dict[str, bytes] = {}
    try:
        case_paths = _file_paths(set_root, ".jsonl", frozenset({judgments_file, signoffs_file, labels_file}))
        slice_directories = _slice_dirs(set_root)
    except OSError as exc:
        findings.append(Finding(set_root.as_posix(), None, 0, f"set cannot be read ({exc})", _READ_FIX))
        return EvalSetLoadResult(findings=tuple(sorted(findings, key=_finding_sort_key)))

    cases_by_file, occurrences = _read_cases(
        set_root, case_paths, court_id_set, findings, files
    )
    located_judgments: tuple[judgments.LocatedJudgment, ...] = ()
    judgments_path = set_root / judgments.JUDGMENTS_PATH
    if judgments_path.is_file():
        # The bytes join the digest's file map, so a changed grade moves the
        # digest as a changed case does; the parse owns the final-newline
        # finding, so the loader does not report the fault a second time.
        data = _read_bytes(
            set_root,
            judgments_path,
            findings,
            files,
            _CASE_FIX,
            check_final_newline=False,
        )
        if data is not None:
            parsed = judgments.parse_bytes(data, judgments_file)
            findings.extend(parsed.findings)
            located_judgments = parsed.records
    located_signoffs: tuple[signoffs.LocatedSignOff, ...] = ()
    signoffs_path = set_root / signoffs.SIGNOFFS_PATH
    if signoffs_path.is_file():
        # The bytes join the digest's file map, so a changed sign-off moves the
        # digest as a changed case does; the parse owns the final-newline
        # finding, so the loader does not report the fault a second time.
        data = _read_bytes(
            set_root,
            signoffs_path,
            findings,
            files,
            _CASE_FIX,
            check_final_newline=False,
        )
        if data is not None:
            parsed_signoffs = signoffs.parse_bytes(data, signoffs_file)
            findings.extend(parsed_signoffs.findings)
            located_signoffs = parsed_signoffs.records
    located_labels: tuple[segmenter_labels.LocatedLabel, ...] = ()
    labels_path = set_root / segmenter_labels.LABELS_PATH
    if labels_path.is_file():
        data = _read_bytes(
            set_root, labels_path, findings, files, _CASE_FIX, check_final_newline=False
        )
        if data is not None:
            parsed_labels = segmenter_labels.parse_bytes(data, labels_file)
            findings.extend(parsed_labels.findings)
            located_labels = parsed_labels.records
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

    # A case whose parent is retired is retired; the loader refuses a variant of
    # a variant, so the rule is one step deep and equals the scorer's transitive one.
    retired = superseded | {
        case_id
        for case_id, case in cases_by_id.items()
        if isinstance(case.get("parent"), str) and case["parent"] in superseded
    }
    # A grade is a yardstick only where its query is one the set still runs, so
    # the resolution waits for the supersedes pass above.
    for located in located_judgments:
        query_id = located.record.query_id
        judgment_case = cases_by_id.get(query_id)
        if judgment_case is None:
            rule = "query_id names no case"
        elif query_id in retired:
            rule = "query_id names a superseded case"
        elif judgment_case.get("suite") != "judgments":
            rule = "query_id names a case of another suite"
        else:
            continue
        findings.append(
            Finding(
                judgments_file,
                None,
                located.line,
                rule,
                judgments.JUDGMENT_LINE_FIX,
            )
        )

    # A sign-off stands only where its case is one the set still runs and one
    # whose shape takes a sign-off at all, so the resolution waits for the
    # supersedes pass above as the judgments resolution does.
    for signoff in located_signoffs:
        case_id = signoff.record.case_id
        signoff_case = cases_by_id.get(case_id)
        spec = None if signoff_case is None else _shape_for(signoff_case)
        if signoff_case is None:
            rule = "case_id names no case"
        elif case_id in retired:
            rule = "case_id names a superseded case"
        elif spec is None or not spec.takes_signoff:
            rule = "case_id names a case whose shape takes no sign-off"
        else:
            continue
        findings.append(
            Finding(
                signoffs_file,
                None,
                signoff.line,
                rule,
                signoffs.SIGNOFF_LINE_FIX,
            )
        )

    for located_label in located_labels:
        case_id = located_label.record.case_id
        label_case = cases_by_id.get(case_id)
        if label_case is None:
            rule = "case_id names no case"
        elif _shape_for(label_case) is not SHAPE_REGISTRY[("build-gates", "segmenter-300")]:
            rule = "case_id names a non-segmenter case"
        else:
            continue
        findings.append(Finding(labels_file, case_id, located_label.line, rule, segmenter_labels.LABEL_LINE_FIX))

    ordered_findings = tuple(sorted(findings, key=_finding_sort_key))
    if ordered_findings:
        return EvalSetLoadResult(findings=ordered_findings)
    active_ids = tuple(sorted(set(cases_by_id) - retired))
    signed_ids = frozenset(
        {located.record.case_id for located in located_signoffs}
        | {located.record.case_id for located in located_labels}
    )
    # A case is unsigned iff its shape takes a sign-off, it carries no
    # expected of its own, and no sign-off or label names it. Every shape
    # validated above; an unknown shape cannot reach this point.
    unsigned_ids = frozenset(
        case_id
        for case_id in active_ids
        for spec in (_shape_for(cases_by_id[case_id]),)
        if (
            spec is not None and spec.takes_signoff and case_id not in signed_ids
            and _origin(cases_by_id[case_id]) != synthetic.SYNTHETIC_ORIGIN
        )
    )
    return EvalSetLoadResult(
        loaded=LoadedSet(
            version=set_root.name,
            cases_by_file=cases_by_file,
            cases_by_id=cases_by_id,
            active_ids=active_ids,
            slices=slices,
            slice_lists=slice_lists,
            digest=_set_digest(files),
            judgments=tuple(located.record for located in located_judgments),
            signoffs=tuple(located.record for located in located_signoffs),
            segmenter_labels=tuple(located.record for located in located_labels),
            unsigned_ids=unsigned_ids,
        )
    )


def select_cases(loaded: LoadedSet, slice_name: str) -> Selection:
    """Split one slice's active ids into counted and unsigned cases."""

    active_ids = set(loaded.active_ids)
    counted: list[str] = []
    unsigned: list[str] = []
    takes_signoff = False
    for case_id in loaded.slices[slice_name]:
        if case_id not in active_ids:
            continue
        spec = _shape_for(loaded.cases_by_id[case_id])
        takes_signoff = takes_signoff or (spec is not None and spec.takes_signoff)
        target = unsigned if case_id in loaded.unsigned_ids else counted
        target.append(case_id)
    return Selection(
        counted=tuple(counted), unsigned=tuple(unsigned), takes_signoff=takes_signoff
    )
