"""Gate mislabels, type precision, unknown share, and page-locator counts against checked labels and the record.

The document read is a corpus read: production's record on both stacks, the
sibling's holding no documents.
"""

import json
import re
import subprocess
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Final, Protocol, cast

from gideon.evaluation import bound, record, segmenter_leg
from gideon.evaluation.evalset import SEGMENTER_TEXT_SOURCES, LoadedSet, select_cases
from gideon.evaluation.results import CaseResult, JSONValue, RunContext, SliceResult
from gideon.host import cas, report, stack
from gideon.host.render.worker import PIPELINE_VERSION, SECTION_TYPED_BY, SECTION_TYPES
from gideon.host.report import Problem, failure_lines
from gideon.host.sysio import BytesHost

COURT_TYPES: Final[frozenset[str]] = frozenset({"majority", "plurality", "per_curiam"})
SEPARATE_TYPES: Final[frozenset[str]] = frozenset(
    {"concurrence", "dissent", "concurrence_dissent"}
)
# exempt: the gate's precision floor is a release decision, read on the frozen sample.
PRECISION_FLOOR: Final[float] = 0.95
# exempt: a starting ceiling, corrected by the frozen sample's reading and never by the gate.
UNKNOWN_CEILING: Final[float] = 0.15
PROBLEMS: Final[frozenset[str]] = frozenset({
    "record-unreachable", "not-ingested", "original-missing", "text-moved", "record-stale",
    "locator-count-differs",
}) | segmenter_leg.PROBLEMS
_SHA256: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
DOCUMENTS_SQL: Final[str] = """SELECT COALESCE(jsonb_agg(jsonb_build_object(
    'opinion_id', o.opinion_id,
    'doc_id', d.doc_id,
    'text_source', d.text_source,
    'sha256', d.sha256,
    'canonical_sha256', d.canonical_text_sha256,
    'opinion_type', o.opinion_type,
    'per_curiam', o.per_curiam,
    'sections', (SELECT COALESCE(jsonb_agg(
        jsonb_build_array(s.char_start, s.char_end, s.section_type, s.typed_by)
        ORDER BY s.ordinal), '[]'::jsonb)
        FROM sections s WHERE s.doc_id = d.doc_id AND s.pipeline = :'pipeline')
)), '[]'::jsonb)::text
FROM opinions o
JOIN documents d ON d.doc_id = o.doc_id AND d.status = 'ready'
WHERE o.opinion_id IN (
    SELECT value::bigint FROM jsonb_array_elements_text(:'opinion_ids'::jsonb) AS value
);
"""


class _Span(Protocol):
    @property
    def start(self) -> int: ...

    @property
    def end(self) -> int: ...

    @property
    def section_type(self) -> str: ...


class _RecordSpan(Protocol):
    @property
    def char_start(self) -> int: ...

    @property
    def char_end(self) -> int: ...

    @property
    def section_type(self) -> str: ...


@dataclass(frozen=True, slots=True)
class CaseFigures:
    """Character counts for one case or a sum of cases."""

    chars: int
    mislabel: int
    unknown: int
    matrix: Mapping[str, Mapping[str, int]]


@dataclass(frozen=True, slots=True)
class PageFigures:
    """Page-locator and page-map counts and differing ids for one text source."""

    elements: int
    markers: int
    differing: tuple[str, ...]
    pgmap_checked: int
    pgmap_disagreeing: int


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """Whether every code-computed segmenter bound holds."""

    ok: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecordSection:
    """One recorded section in canonical-text code points."""

    char_start: int
    char_end: int
    section_type: str
    typed_by: str


@dataclass(frozen=True, slots=True)
class RecordDocument:
    """A ready opinion's content-free document and section facts."""

    opinion_id: int
    doc_id: str
    text_source: str
    sha256: str
    canonical_sha256: str
    opinion_type: str
    per_curiam: bool | None
    sections: tuple[RecordSection, ...]


class _PageLocatorParser(HTMLParser):
    """Count the walk's page-locator element shapes without keeping text."""

    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = set((dict(attrs).get("class") or "").split())
        if (
            tag == "page-number"
            or tag == "span" and ("star-pagination" in classes or "ldml-pagenumber" in classes)
            or tag == "a" and "page-label" in classes
        ):
            self.count += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def page_locator_count(column: str, original: str) -> int:
    """Count page-locator elements in markup; plain text has none by rule."""

    if column == "plain_text":
        return 0
    parser = _PageLocatorParser()
    parser.feed(original)
    parser.close()
    return parser.count


def overlap_matrix(
    sections: Sequence[_Span], spans: Sequence[_Span]
) -> dict[str, dict[str, int]]:
    """Count code-point intersections by candidate and checked section type."""

    matrix: dict[str, dict[str, int]] = {}
    for section in sections:
        for span in spans:
            overlap = min(section.end, span.end) - max(section.start, span.start)
            if overlap > 0:
                row = matrix.setdefault(section.section_type, {})
                row[span.section_type] = row.get(span.section_type, 0) + overlap
    return matrix


def case_figures(matrix: Mapping[str, Mapping[str, int]]) -> CaseFigures:
    """Sum characters, protected mislabels, and candidate unknown characters."""

    chars = sum(count for row in matrix.values() for count in row.values())
    mislabel = sum(
        count
        for section_type, row in matrix.items()
        if section_type in COURT_TYPES
        for checked_type, count in row.items()
        if checked_type in SEPARATE_TYPES
    )
    unknown = sum(matrix.get("unknown", {}).values())
    return CaseFigures(chars, mislabel, unknown, matrix)


def record_agrees(sections: Sequence[_Span], recorded: Sequence[_RecordSpan]) -> bool:
    """Compare candidate and recorded boundaries and types in document order."""

    candidate = tuple((item.start, item.end, item.section_type) for item in sections)
    stored = tuple((item.char_start, item.char_end, item.section_type) for item in recorded)
    return candidate == stored


def slice_gate(
    matrix: Mapping[str, Mapping[str, int]], counted: int, measured: int, differing: int
) -> GateVerdict:
    """Apply the mislabel, precision, coverage, unknown, and page-locator bounds."""

    figures = case_figures(matrix)
    reasons: list[str] = []
    if counted == 0:
        reasons.append("no labelled case")
    if measured != counted:
        reasons.append("not every counted case was measured")
    if figures.mislabel:
        reasons.append("separate-opinion text under a court-opinion type")
    # `unknown` is the abstention, not a guess: its share has its own ceiling,
    # and the text under it is whatever the checker read there.
    for section_type in SECTION_TYPES:
        if section_type == "unknown":
            continue
        row = matrix.get(section_type, {})
        typed = sum(row.values())
        if typed and row.get(section_type, 0) / typed < PRECISION_FLOOR:
            reasons.append(f"{section_type} precision below floor")
    if figures.chars and figures.unknown / figures.chars > UNKNOWN_CEILING:
        reasons.append("unknown share above ceiling")
    if differing:
        reasons.append("page locators and markers differ")
    return GateVerdict(not reasons, tuple(reasons))


def _record_fix(context: RunContext) -> str:
    return (
        f"Run {report.command('apply')} and inspect "
        f"{stack.logs_fix(context.production_dir, record.POSTGRES_SERVICE)}, then retry."
    )


def _valid_hash(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _decode_document(value: object) -> RecordDocument | None:
    if not isinstance(value, dict) or set(value) != {
        "opinion_id", "doc_id", "text_source", "sha256", "canonical_sha256",
        "opinion_type", "per_curiam", "sections",
    }:
        return None
    opinion_id = value["opinion_id"]
    if (
        type(opinion_id) is not int or opinion_id <= 0
        or not _valid_hash(value["doc_id"])
        or value["text_source"] not in SEGMENTER_TEXT_SOURCES
        or not _valid_hash(value["sha256"])
        or not _valid_hash(value["canonical_sha256"])
        or not isinstance(value["opinion_type"], str)
        or (value["per_curiam"] is not None and type(value["per_curiam"]) is not bool)
        or not isinstance(value["sections"], list)
    ):
        return None
    sections: list[RecordSection] = []
    for item in value["sections"]:
        if (
            not isinstance(item, list) or len(item) != 4
            or type(item[0]) is not int or type(item[1]) is not int
            or item[0] < 0 or item[1] <= item[0]
            or item[2] not in SECTION_TYPES
            or item[3] not in SECTION_TYPED_BY
        ):
            return None
        sections.append(RecordSection(item[0], item[1], item[2], item[3]))
    return RecordDocument(
        opinion_id, value["doc_id"], value["text_source"], value["sha256"],
        value["canonical_sha256"], value["opinion_type"], value["per_curiam"], tuple(sections),
    )


def _read_documents(
    opinion_ids: tuple[int, ...], context: RunContext
) -> Mapping[int, RecordDocument] | Problem:
    """Read all ready opinions in one metrics-role statement against production's record.

    The documents are a corpus read, as a sample build's are: the sibling's
    record holds no documents, so both stacks read production's.
    """

    values = json.dumps(sorted(set(opinion_ids)))
    sql = f"\\set opinion_ids '{values}'\n\\set pipeline '{PIPELINE_VERSION}'\n{DOCUMENTS_SQL}"
    fix = _record_fix(context)
    try:
        result = context.host.run(record.reader_argv(context.production_dir), input=sql)
    except (OSError, subprocess.SubprocessError):
        return Problem("segmenter record read could not run", fix)
    if result.returncode != 0:
        return Problem(f"segmenter record read exited {result.returncode}", fix)
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return Problem("segmenter record reply is not JSON", fix)
    if not isinstance(value, list):
        return Problem("segmenter record reply is not a list", fix)
    documents: dict[int, RecordDocument] = {}
    requested = set(opinion_ids)
    for item in value:
        document = _decode_document(item)
        if (
            document is None or document.opinion_id not in requested
            or document.opinion_id in documents
        ):
            return Problem("segmenter record reply has invalid document facts", fix)
        documents[document.opinion_id] = document
    return documents


def _sum_matrix(
    total: dict[str, dict[str, int]], matrix: Mapping[str, Mapping[str, int]]
) -> None:
    for section_type, row in matrix.items():
        target = total.setdefault(section_type, {})
        for checked_type, count in row.items():
            target[checked_type] = target.get(checked_type, 0) + count


def _report(
    selection_count: int,
    unsigned_count: int,
    results: Sequence[CaseResult],
    matrix: Mapping[str, Mapping[str, int]],
    source_counts: Mapping[str, tuple[int, int]],
    page_figures: Mapping[str, PageFigures],
    elapsed: float,
    reasons: tuple[str, ...],
    fixes: Mapping[str, str],
    details: Sequence[str],
    *,
    verdict: bool,
) -> str:
    """Render ids, counts, and vocabulary words alone."""

    figures = case_figures(matrix)
    lines = [f"record pipeline {PIPELINE_VERSION}", f"image leg elapsed {elapsed:.3f} s"]
    for section_type in SECTION_TYPES:
        row = matrix.get(section_type, {})
        typed = sum(row.values())
        if not typed:
            continue
        if section_type == "unknown":
            lines.append(f"type unknown: {typed} characters (abstained)")
            continue
        confusion = sorted(
            ((count, checked) for checked, count in row.items() if checked != section_type),
            key=lambda item: (-item[0], item[1]),
        )
        largest = f"{confusion[0][1]} {confusion[0][0]}" if confusion else "none 0"
        lines.append(
            f"type {section_type}: {typed} characters, precision "
            f"{row.get(section_type, 0) / typed:.3f}, largest confusion {largest}"
        )
    mislabel_ids = [
        item.case_id for item in results
        if isinstance(item.metrics.get("mislabel"), int) and item.metrics["mislabel"]
    ]
    lines.append(f"mislabel {figures.mislabel}; ids {', '.join(mislabel_ids) or 'none'}")
    for source in SEGMENTER_TEXT_SOURCES:
        if source in source_counts:
            unknown, chars = source_counts[source]
            lines.append(f"unknown {source}: {unknown}/{chars} characters")
    for source in SEGMENTER_TEXT_SOURCES:
        if source in page_figures:
            pages = page_figures[source]
            lines.append(
                f"page {source}: {pages.elements} locators, {pages.markers} markers, "
                f"differing ids {', '.join(pages.differing) or 'none'}; "
                f"pgmap checked {pages.pgmap_checked}, disagreeing {pages.pgmap_disagreeing}"
            )
    failures: Counter[str] = Counter(
        cast(str, item.metrics["problem"])
        for item in results if isinstance(item.metrics.get("problem"), str)
    )
    failure_counts = ", ".join(
        f"{code} {failures[code]} ids "
        + ",".join(
            item.case_id for item in results if item.metrics.get("problem") == code
        )
        for code in sorted(failures)
    ) or "none"
    lines.append(
        f"counted {selection_count}, unsigned {unsigned_count}, "
        f"measured {sum('chars' in item.metrics for item in results)}, failed-by-code {failure_counts}; "
        f"{bound.bound_text(selection_count, verdict)}"
    )
    for item in details:
        lines.append(item)
    for reason in reasons:
        lines.append(f"gate: {reason}")
    for code, fix in sorted(fixes.items()):
        lines.extend(failure_lines(fix, f"{code}: review the named case ids"))
    return "\n".join(lines) + "\n"


def run_segmenter(eval_set: LoadedSet, slice_name: str, context: RunContext) -> SliceResult:
    """Measure counted cases against the candidate image and recorded sections."""

    selection = select_cases(eval_set, slice_name)
    counted = selection.counted
    labels = {item.case_id: item for item in eval_set.segmenter_labels}
    results: dict[str, CaseResult] = {}
    fixes: dict[str, str] = {}
    details: list[str] = []
    matrix: dict[str, dict[str, int]] = {}
    source_counts: dict[str, tuple[int, int]] = {}
    page_figures: dict[str, PageFigures] = {}
    elapsed = 0.0

    def fail(case_id: str, code: str, fix: str, latency_ms: float | None = None) -> None:
        results[case_id] = CaseResult(case_id, 1, "fail", {"problem": code}, latency_ms=latency_ms)
        fixes[code] = fix

    if not counted:
        gate = slice_gate(matrix, 0, 0, 0)
        return SliceResult(False, _report(0, len(selection.unsigned), (), matrix, source_counts, page_figures, elapsed, gate.reasons, fixes, details, verdict=False), (), gated_cases=0)
    if context.image is None:
        for case_id in counted:
            fail(case_id, "image-leg-failed", "Build the pinned gideon image with tools.imagebuild, then retry.")
        ordered = tuple(results[case_id] for case_id in counted)
        gate = slice_gate(matrix, len(counted), 0, 0)
        return SliceResult(False, _report(len(counted), len(selection.unsigned), ordered, matrix, source_counts, page_figures, elapsed, gate.reasons, fixes, details, verdict=False), ordered, gated_cases=len(counted))

    opinion_ids = tuple(cast(int, eval_set.cases_by_id[case_id]["opinion_id"]) for case_id in counted)
    documents = _read_documents(opinion_ids, context)
    if isinstance(documents, Problem):
        for case_id in counted:
            fail(case_id, "record-unreachable", documents.fix)
        ordered = tuple(results[case_id] for case_id in counted)
        gate = slice_gate(matrix, len(counted), 0, 0)
        return SliceResult(False, _report(len(counted), len(selection.unsigned), ordered, matrix, source_counts, page_figures, elapsed, gate.reasons, fixes, details, verdict=False), ordered, gated_cases=len(counted))

    available: dict[str, RecordDocument] = {}
    originals: dict[str, str] = {}
    for case_id, opinion_id in zip(counted, opinion_ids, strict=True):
        document = documents.get(opinion_id)
        if document is None:
            fail(case_id, "not-ingested", "Ingest the named opinion, then retry.")
            continue
        original = cas.get(cast(BytesHost, context.host), document.sha256)
        if isinstance(original, Problem):
            fail(case_id, "original-missing", original.fix)
            continue
        try:
            decoded = original.decode("utf-8")
        except UnicodeDecodeError:
            fail(case_id, "original-missing", "Restore the original UTF-8 object, then retry.")
            continue
        available[case_id] = document
        originals[case_id] = decoded

    if available:
        leg_inputs = tuple(
            segmenter_leg.LegInput(
                case_id, document.text_source, document.opinion_type,
                document.per_curiam if document.per_curiam is not None else
                any(section.typed_by == "flag" for section in document.sections),
            )
            for case_id, document in available.items()
        )
        started = time.monotonic()
        leg = segmenter_leg.run_leg(leg_inputs, originals, context)
        elapsed = time.monotonic() - started
        if isinstance(leg, Problem):
            details.append(leg.problem)
            for case_id in available:
                fail(case_id, "image-leg-failed", leg.fix, elapsed * 1000 / len(available))
        else:
            share_ms = elapsed * 1000 / len(available)
            for case_id, document in available.items():
                candidate = leg[case_id]
                label = labels[case_id]
                if (
                    candidate.sha256 != label.sha256
                    or candidate.sha256 != document.canonical_sha256
                    or candidate.length != label.length
                ):
                    fail(case_id, "text-moved", "Re-draft and re-check the named opinion as a superseding case, then retry.", share_ms)
                    details.append(
                        f"text-moved {case_id}: candidate {candidate.sha256[:12]}, "
                        f"label {label.sha256[:12]}, record {document.canonical_sha256[:12]}"
                    )
                    continue
                page_elements = page_locator_count(document.text_source, originals[case_id])
                page_markers = sum(item.kind == "page" for item in candidate.markers)
                pages_agree = page_elements == page_markers
                pages = page_figures.get(document.text_source, PageFigures(0, 0, (), 0, 0))
                page_figures[document.text_source] = PageFigures(
                    pages.elements + page_elements,
                    pages.markers + page_markers,
                    pages.differing + (() if pages_agree else (case_id,)),
                    pages.pgmap_checked + candidate.pgmap_checked,
                    pages.pgmap_disagreeing + candidate.pgmap_disagreeing,
                )
                part = overlap_matrix(candidate.sections, label.sections)
                figures = case_figures(part)
                _sum_matrix(matrix, part)
                unknown, chars = source_counts.get(document.text_source, (0, 0))
                source_counts[document.text_source] = (unknown + figures.unknown, chars + figures.chars)
                agrees = record_agrees(candidate.sections, document.sections)
                metrics: dict[str, JSONValue] = {
                    "chars": figures.chars,
                    "mislabel": figures.mislabel,
                    "unknown": figures.unknown,
                    "matrix": part,
                    "text_source": document.text_source,
                    "record_agrees": agrees,
                    "page_elements": page_elements,
                    "page_markers": page_markers,
                    "pgmap_checked": candidate.pgmap_checked,
                    "pgmap_disagreeing": candidate.pgmap_disagreeing,
                }
                if not pages_agree:
                    metrics["problem"] = "locator-count-differs"
                    fixes["locator-count-differs"] = (
                        "Compare the named case's page locators with the walk's markers; "
                        "a locator the walk drops is a finding on the opinion parser, never a moved bound."
                    )
                elif not agrees:
                    metrics["problem"] = "record-stale"
                    fixes["record-stale"] = "Re-ingest the named opinion at the candidate pipeline, then retry."
                results[case_id] = CaseResult(
                    case_id, 1, "pass" if figures.mislabel == 0 and agrees and pages_agree else "fail",
                    metrics, judge=None, latency_ms=share_ms,
                )
    ordered = tuple(results[case_id] for case_id in counted)
    measured = sum("chars" in item.metrics for item in ordered)
    differing = sum(item.metrics.get("problem") == "locator-count-differs" for item in ordered)
    gate = slice_gate(matrix, len(counted), measured, differing)
    verdict = gate.ok and all(item.verdict == "pass" for item in ordered)
    return SliceResult(
        verdict,
        _report(len(counted), len(selection.unsigned), ordered, matrix, source_counts, page_figures, elapsed, gate.reasons, fixes, details, verdict=verdict),
        ordered,
        gated_cases=len(counted),
    )
