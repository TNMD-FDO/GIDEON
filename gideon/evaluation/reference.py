"""The committed reference-file format and its per-case comparison.

Reference files are the one stable, content-free record of an evaluation slice:
``eval/reference/<slice>/<list>.json`` contains the format number, the release
identity, and one ``pass`` or ``fail`` verdict for each case in that id list.
The format deliberately excludes run ids, timestamps, metrics, judges, case
text, and other facts that can change without changing the release's verdicts.

This module is the one home of the format vocabulary and its path rule.  Its
reader reports structural findings through the eval-set loader's ``Finding``
shape, and its comparison only operates on already-loaded values.  A regression
is a reference ``pass`` that is a current ``fail``; gains, new cases, and
dropped cases make a reference stale but never make the gate fail.
"""

import json
import re
import stat
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

from gideon.evaluation.evalset import SHAPE_REGISTRY, Finding
from gideon.host.sysio import Host, PathLike, RealHost

type Verdict = Literal["pass", "fail"]
type CaseVerdict = tuple[str, int, str]
"""One evaluated ``(case id, repeat, verdict)`` row, as a run or a record yields it."""
type ReferenceOutcome = Literal[
    "current",
    "stale",
    "absent",
    "other-version",
    "regressed",
    "malformed",
]

FORMAT_VERSION: Final[int] = 1
VERDICTS: Final[tuple[Verdict, ...]] = ("pass", "fail")
OUTCOMES: Final[tuple[ReferenceOutcome, ...]] = (
    "current",
    "stale",
    "absent",
    "other-version",
    "regressed",
    "malformed",
)
REFERENCE_ROOT: Final[Path] = Path("eval", "reference")

_REFERENCE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "cases",
        "corpus_lockfile",
        "eval_set_version",
        "format",
        "hardware_profile",
        "list",
        "product_version",
        "repeats",
        "set_digest",
        "slice",
        "tag",
    }
)
_HEADER_FIELDS: Final[tuple[str, ...]] = tuple(
    sorted(_REFERENCE_KEYS - {"cases", "list"})
)
_CASE_ID_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    spec.id_pattern for spec in SHAPE_REGISTRY.values()
)
_SHA256: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_REFERENCE_FIX: Final[str] = "Restore the reference file from the release checkout, then retry."
SLICE_REPAIR_FIX: Final[str] = (
    "Restore every reference file for the slice from the release checkout, or remove them "
    "all and re-record, then retry."
)
REGRESSION_FIX: Final[str] = (
    "Repair the regression or supersede the case under the suite's README; to accept a "
    "loss, remove every reference file for the slice and re-record, then retry."
)


@dataclass(frozen=True, slots=True)
class ReferenceFile:
    """One canonical reference file for one frozen id list."""

    format: int
    product_version: str
    corpus_lockfile: str | None
    eval_set_version: str
    hardware_profile: str
    tag: str
    slice: str
    list: str
    repeats: int
    set_digest: str
    cases: Mapping[str, Verdict]


@dataclass(frozen=True, slots=True)
class SliceReference:
    """All reference files belonging to one evaluation slice."""

    files: tuple[ReferenceFile, ...]


@dataclass(frozen=True, slots=True)
class ReferenceLoadResult:
    """A slice reference or every finding collected while reading it."""

    reference: SliceReference | None = None
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether a complete, structurally sound reference was loaded."""

        return self.reference is not None and not self.findings


@dataclass(frozen=True, slots=True)
class Comparison:
    """The four per-case differences and the resulting reference outcome."""

    outcome: ReferenceOutcome
    tag: str | None
    regressed: tuple[str, ...]
    gained: tuple[str, ...]
    new: tuple[str, ...]
    dropped: tuple[str, ...]
    stale: bool


def serialize_reference(reference: ReferenceFile) -> str:
    """Return the stable, two-space, ASCII JSON for *reference*."""

    document = {
        "format": reference.format,
        "product_version": reference.product_version,
        "corpus_lockfile": reference.corpus_lockfile,
        "eval_set_version": reference.eval_set_version,
        "hardware_profile": reference.hardware_profile,
        "tag": reference.tag,
        "slice": reference.slice,
        "list": reference.list,
        "repeats": reference.repeats,
        "set_digest": reference.set_digest,
        "cases": dict(sorted(reference.cases.items())),
    }
    return json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n"


def _finding_sort_key(finding: Finding) -> tuple[str, int, str, str]:
    return (finding.file, finding.line or 0, finding.id or "", finding.rule)


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _reference_path(root: Path, slice_name: str) -> Path:
    return root / REFERENCE_ROOT / slice_name


def _valid_case_id(value: object) -> bool:
    return isinstance(value, str) and any(
        pattern.fullmatch(value) is not None for pattern in _CASE_ID_PATTERNS
    )


def _required_string(
    document: Mapping[str, object],
    field: str,
    relative: str,
    findings: list[Finding],
) -> str:
    """The string at *field*, or ``""`` and a finding that refuses the file."""

    value = document.get(field)
    if isinstance(value, str):
        return value
    findings.append(Finding(relative, None, 0, f"{field} must be a string", _REFERENCE_FIX))
    return ""


def _nullable_string(
    document: Mapping[str, object],
    field: str,
    relative: str,
    findings: list[Finding],
) -> str | None:
    value = document.get(field)
    if value is None or isinstance(value, str):
        return cast(str | None, value)
    findings.append(Finding(relative, None, 0, f"{field} must be a string or null", _REFERENCE_FIX))
    return None


def _parse_file(
    root: Path,
    path: Path,
    slice_name: str,
    list_ids: Mapping[str, tuple[str, ...]],
    text: str,
    findings: list[Finding],
    list_findings: list[Finding],
) -> ReferenceFile | None:
    """Parse one file, splitting findings that read the loaded set's id lists.

    Only *findings* refuse the file: a reference for another eval-set version
    is still parsed, because its header is what names that version, and the
    id lists of the set loaded now say nothing about the cases it holds.
    """

    relative = _relative(root, path)
    before = len(findings)
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        line = getattr(exc, "lineno", 1)
        findings.append(Finding(relative, None, line, "file is not one JSON object", _REFERENCE_FIX))
        return None
    if not isinstance(document, dict):
        findings.append(Finding(relative, None, 1, "file is not one JSON object", _REFERENCE_FIX))
        return None

    keys = set(document)
    for key in sorted(keys - _REFERENCE_KEYS):
        findings.append(Finding(relative, None, 0, f"unknown key {key!r}", _REFERENCE_FIX))
    for key in sorted(_REFERENCE_KEYS - keys):
        findings.append(Finding(relative, None, 0, f"missing key {key!r}", _REFERENCE_FIX))

    format_value = document.get("format")
    if not isinstance(format_value, int) or isinstance(format_value, bool) or format_value != FORMAT_VERSION:
        findings.append(Finding(relative, None, 0, "format number", _REFERENCE_FIX))

    product_version = _required_string(document, "product_version", relative, findings)
    corpus_lockfile = _nullable_string(document, "corpus_lockfile", relative, findings)
    eval_set_version = _required_string(document, "eval_set_version", relative, findings)
    hardware_profile = _required_string(document, "hardware_profile", relative, findings)
    tag = _required_string(document, "tag", relative, findings)
    header_slice = _required_string(document, "slice", relative, findings)
    header_list = _required_string(document, "list", relative, findings)

    if header_slice and header_slice != slice_name:
        findings.append(Finding(relative, None, 0, "slice does not match path", _REFERENCE_FIX))

    file_list = path.stem
    expected_ids = list_ids.get(file_list)
    if expected_ids is None:
        list_findings.append(Finding(relative, None, 0, "file names no id list", SLICE_REPAIR_FIX))
    if header_list and header_list != file_list:
        findings.append(Finding(relative, None, 0, "list does not match path", _REFERENCE_FIX))

    repeats_value = document.get("repeats")
    if isinstance(repeats_value, int) and not isinstance(repeats_value, bool) and repeats_value >= 1:
        repeats = repeats_value
    else:
        repeats = 0
        findings.append(Finding(relative, None, 0, "repeats must be a positive integer", _REFERENCE_FIX))

    digest_value = document.get("set_digest")
    if isinstance(digest_value, str) and _SHA256.fullmatch(digest_value) is not None:
        set_digest = digest_value
    else:
        set_digest = ""
        findings.append(Finding(relative, None, 0, "set_digest must be a SHA-256 digest", _REFERENCE_FIX))

    cases_value = document.get("cases")
    cases: dict[str, Verdict] = {}
    if not isinstance(cases_value, dict):
        findings.append(Finding(relative, None, 0, "cases must be an object", _REFERENCE_FIX))
    else:
        for case_id, verdict in sorted(cases_value.items()):
            if not _valid_case_id(case_id):
                findings.append(Finding(relative, case_id, 0, "case id pattern", _REFERENCE_FIX))
            elif verdict not in VERDICTS:
                findings.append(Finding(relative, case_id, 0, "case verdict", _REFERENCE_FIX))
            else:
                cases[case_id] = cast(Verdict, verdict)
            if expected_ids is not None and case_id not in expected_ids:
                list_findings.append(
                    Finding(relative, case_id, 0, "id is not named by its id list", _REFERENCE_FIX)
                )

    # Every field above either holds a validated value or left a finding behind
    # it, so the finding count is the one guard the file has to pass.
    reference = ReferenceFile(
        FORMAT_VERSION,
        product_version,
        corpus_lockfile,
        eval_set_version,
        hardware_profile,
        tag,
        header_slice,
        header_list,
        repeats,
        set_digest,
        cases,
    )
    if len(findings) > before:
        return None
    if text != serialize_reference(reference):
        findings.append(Finding(relative, None, 0, "bytes differ from canonical serialization", _REFERENCE_FIX))
        return None
    return reference


def read_reference(
    checkout_root: PathLike,
    slice_name: str,
    slice_lists: Mapping[str, Iterable[str]],
    eval_set_version: str,
    *,
    host: Host | None = None,
) -> ReferenceLoadResult:
    """Read every reference file for *slice_name* through the host seam.

    A slice whose files name another *eval_set_version* is returned whole and
    uncompared: its cases belong to a set this one does not hold, so the checks
    that read this set's id lists are not asked of it, and the writer replaces
    it. Structural findings still refuse, so a mixed-version slice is malformed
    on its header disagreement rather than silently replaceable.
    """

    root = Path(checkout_root)
    io = RealHost() if host is None else host
    normalized_lists = {
        name: tuple(sorted(ids)) for name, ids in slice_lists.items()
    }
    directory = _reference_path(root, slice_name)
    try:
        if not io.exists(directory):
            return ReferenceLoadResult()
        names = sorted(
            name
            for name in io.listdir(directory)
            if name.endswith(".json")
        )
    except (OSError, ValueError) as exc:
        relative = _relative(root, directory)
        finding = Finding(relative, None, 0, f"reference directory cannot be read ({exc})", _REFERENCE_FIX)
        return ReferenceLoadResult(findings=(finding,))

    if not names:
        return ReferenceLoadResult()

    findings: list[Finding] = []
    list_findings: list[Finding] = []
    references: list[ReferenceFile] = []
    present_lists: set[str] = set()
    for name in names:
        path = directory / name
        relative = _relative(root, path)
        try:
            if not stat.S_ISREG(io.stat(path).st_mode):
                findings.append(Finding(relative, None, 0, "reference path is not a file", _REFERENCE_FIX))
                continue
            text = io.read_text(path, encoding="utf-8")
        except (OSError, UnicodeError, ValueError) as exc:
            findings.append(Finding(relative, None, 0, f"file cannot be read ({exc})", _REFERENCE_FIX))
            continue
        if path.stem in normalized_lists:
            present_lists.add(path.stem)
        reference = _parse_file(
            root, path, slice_name, normalized_lists, text, findings, list_findings
        )
        if reference is not None:
            references.append(reference)

    if len(present_lists) > 0:
        for list_name in sorted(set(normalized_lists) - present_lists):
            missing_path = directory / f"{list_name}.json"
            list_findings.append(
                Finding(
                    _relative(root, missing_path),
                    None,
                    0,
                    "id list has no reference file (partial reference)",
                    SLICE_REPAIR_FIX,
                )
            )

    if references:
        baseline = references[0]
        for reference in references[1:]:
            for field in _HEADER_FIELDS:
                if getattr(reference, field) != getattr(baseline, field):
                    relative = _relative(root, directory / f"{reference.list}.json")
                    findings.append(
                        Finding(
                            relative,
                            None,
                            0,
                            f"{field} disagrees with {baseline.list}",
                            SLICE_REPAIR_FIX,
                        )
                    )
    findings.sort(key=_finding_sort_key)
    if findings:
        return ReferenceLoadResult(findings=tuple(findings))
    loaded = SliceReference(tuple(sorted(references, key=lambda value: value.list)))
    if any(value.eval_set_version != eval_set_version for value in references):
        return ReferenceLoadResult(loaded)
    list_findings.sort(key=_finding_sort_key)
    if list_findings:
        return ReferenceLoadResult(findings=tuple(list_findings))
    return ReferenceLoadResult(loaded)


def fold_repeats(results: Iterable[CaseVerdict]) -> Mapping[str, Verdict]:
    """Fold ``(case id, repeat, verdict)`` rows: a case passes only when every repeat did."""

    by_case: defaultdict[str, list[str]] = defaultdict(list)
    for case_id, _repeat, verdict in results:
        if verdict not in VERDICTS:
            raise ValueError("an evaluation result carries a verdict outside pass/fail")
        by_case[case_id].append(verdict)
    return {
        case_id: cast(Verdict, "pass" if all(value == "pass" for value in verdicts) else "fail")
        for case_id, verdicts in sorted(by_case.items())
    }


def compare_reference(
    reference: SliceReference | None,
    current: Mapping[str, Verdict],
    eval_set_version: str,
) -> Comparison:
    """Compare current per-case verdicts with a loaded slice reference."""

    if reference is None or not reference.files:
        return Comparison("absent", None, (), (), (), (), False)

    files = reference.files
    if any(value.eval_set_version != eval_set_version for value in files):
        return Comparison("other-version", files[0].tag, (), (), (), (), False)

    current_cases = dict(current)
    reference_cases = {
        case_id: verdict
        for value in files
        for case_id, verdict in value.cases.items()
    }
    regressed = tuple(
        sorted(
            case_id
            for case_id, verdict in reference_cases.items()
            if verdict == "pass" and current_cases.get(case_id) == "fail"
        )
    )
    gained = tuple(
        sorted(
            case_id
            for case_id, verdict in reference_cases.items()
            if verdict == "fail" and current_cases.get(case_id) == "pass"
        )
    )
    new = tuple(sorted(set(current_cases) - set(reference_cases)))
    dropped = tuple(sorted(set(reference_cases) - set(current_cases)))
    stale = bool(gained or new or dropped)
    outcome: ReferenceOutcome
    if regressed:
        outcome = "regressed"
    elif stale:
        outcome = "stale"
    else:
        outcome = "current"
    return Comparison(outcome, files[0].tag, regressed, gained, new, dropped, stale)
