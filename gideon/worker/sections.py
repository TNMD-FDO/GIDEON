"""Divide canonical opinion text into typed, contiguous sections."""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from .opiniontext import Block, Parsed


SECTION_TYPES: Final = (
    "syllabus", "headmatter", "majority", "plurality", "per_curiam",
    "concurrence", "dissent", "concurrence_dissent", "footnote", "appendix",
    "order", "unknown",
)
TYPED_BY: Final = ("row", "flag", "element", "line", "markup", "none")
ROW_TYPES: Final = {
    "015unamimous": "majority",
    "020lead": "majority",
    "025plurality": "plurality",
    "030concurrence": "concurrence",
    "035concurrenceinpart": "concurrence_dissent",
    "040dissent": "dissent",
}
ELEMENT_TYPES: Final = {
    "majority": "majority",
    "unanimous": "majority",
    "plurality": "plurality",
    "concurrence": "concurrence",
    "concurring-in-part-and-dissenting-in-part": "concurrence_dissent",
    "dissent": "dissent",
}

# exempt: the line bound is a starting value to be measured against opinion samples.
LINE_MAX_CHARS: Final = 300

# exempt: these heading shapes are starting values to be measured against opinion samples.
HEADING_CUES: Final = {
    "opinion": "majority",
    "opinion of the court": "majority",
    "majority opinion": "majority",
    "dissent": "dissent",
    "dissenting opinion": "dissent",
    "concurrence": "concurrence",
    "concurring opinion": "concurrence",
    "concurring in part and dissenting in part": "concurrence_dissent",
    "dissenting in part and concurring in part": "concurrence_dissent",
    "per curiam": "per_curiam",
    "syllabus": "syllabus",
    "appendix": "appendix",
    "appendix to opinion of the court": "appendix",
    "order": "order",
    "notes": "notes",
}
_SEPARATE: Final = frozenset({"concurrence", "dissent", "concurrence_dissent"})
_COURT: Final = frozenset({"majority", "plurality", "per_curiam"})
_APPENDIX_HEADING: Final = re.compile(r"appendix (?:[a-z]|[0-9])\Z")
_HEADING_PUNCT: Final = re.compile(r"[^\w\s]|_")
_SPACES: Final = re.compile(r"\s+")

# exempt: these bounded byline and disposition shapes are starting values to be measured.
_NAME: Final = r"[A-Z][A-Za-z'’.-]*(?:\s+[A-Z][A-Za-z'’.-]*){0,3}"
_ROLE: Final = (
    r"(?:C\.\s*J\.|JJ\.|J\.|Chief Judge|Circuit Judge|District Judge|"
    r"Senior (?:Circuit |District )?Judge|Judge|Chief Justice|Justice)"
)
# A byline is a name and role followed by nothing, a joiners' clause (which may
# wrap), or a disposition; a separate opinion's disposition runs to the line's
# end in its own closed words, so prose that begins with a judge's name and
# goes on to say "dissenting in that case, argued ..." is not one.
_DISPOSITION_WORDS: Final = (
    r"(?:in|part|and|the|judgment|result|concurring|dissenting|from|denial|of|"
    r"certiorari|per|curiam)\b"
)
_BYLINE_SUFFIX: Final = (
    r"[\s.,:;]*(?:$|(?:with\s+whom\b[\s\S]{0,200}?\bjoin(?:s|ed)?\b[\s.,:;]*)?"
    r"(?:$|(?:delivered|announced|for\s+the\s+court)\b"
    rf"|(?:concurring|dissenting)\b(?:[\s,]+{_DISPOSITION_WORDS})*[\s.,:;]*$))"
)
_AUTHOR: Final = re.compile(
    rf"^(?:{_NAME},\s*(?i:{_ROLE})|(?i:(?:Mr\.\s+)?(?:Chief\s+)?Justice)\s+{_NAME})"
    rf"(?i:{_BYLINE_SUFFIX})"
)
_PER_CURIAM: Final = re.compile(r"per\s+curiam\b", re.IGNORECASE)
_CONCUR: Final = re.compile(r"\bconcurr(?:ing|ence|ed|s)?\b", re.IGNORECASE)
_DISSENT: Final = re.compile(r"\bdissent(?:ing|ed|s)?\b", re.IGNORECASE)
_DISSENT_IN_PART: Final = re.compile(r"\bdissenting\s+in\s+part\b", re.IGNORECASE)
_PLURALITY: Final = re.compile(r"\bannounced\s+the\s+judgment\s+of\s+the\s+court\b", re.IGNORECASE)
_MAJORITY: Final = re.compile(
    r"\b(?:delivered\s+the\s+opinion\s+of\s+the\s+court|for\s+the\s+court|"
    r"opinion\s+of\s+the\s+court)\b", re.IGNORECASE,
)
_JOINED: Final = re.compile(r"\bjoined\b", re.IGNORECASE)
_ROLE_COUNT: Final = re.compile(rf"(?i:{_ROLE})")
_PER_CURIAM_LINE: Final = re.compile(r"per\s+curiam[.:;]?\Z", re.IGNORECASE)
_LEADING_LABEL: Final = re.compile(r"^\s*(\[[^\[\]\s]+\]|\d+\.|\*+)(?=\s|$)")
_LAWBOX_LABEL: Final = re.compile(r"^\s*(\[[^\[\]\s]+\])(?=\s|$)")


@dataclass(frozen=True, slots=True)
class Section:
    """A typed span with an optional footnote reference and parent index."""

    section_type: str
    typed_by: str
    char_start: int
    char_end: int
    label: str | None
    ref_offset: int | None
    parent: int | None


@dataclass(frozen=True, slots=True)
class _Span:
    start: int
    end: int
    section_type: str
    typed_by: str
    label: str | None = None


@dataclass(frozen=True, slots=True)
class _Paragraph:
    start: int
    end: int
    line: str


@dataclass(frozen=True, slots=True)
class _Run:
    start: int
    end: int
    span: int | None
    opinion: int | None


def _paragraphs(text: str) -> list[_Paragraph]:
    paragraphs: list[_Paragraph] = []
    start = 0
    while start < len(text):
        end = text.find("\n\n", start)
        if end < 0:
            end = len(text)
        line = text[start:end].strip()
        if line:
            # The paragraph begins at its text, so a separator stays with the section before.
            first = start + len(text[start:end]) - len(text[start:end].lstrip())
            paragraphs.append(_Paragraph(first, end, line))
        start = end + 2
    return paragraphs


def _heading(line: str) -> str | None:
    normalized = _SPACES.sub(" ", _HEADING_PUNCT.sub(" ", line.casefold())).strip()
    if _APPENDIX_HEADING.fullmatch(normalized):
        return "appendix"
    return HEADING_CUES.get(normalized)


def _author(line: str) -> bool:
    return bool(_PER_CURIAM_LINE.fullmatch(line) or _AUTHOR.match(line))


def _disposition(line: str) -> str | None:
    if _PER_CURIAM.search(line):
        return "per_curiam"
    concur = bool(_CONCUR.search(line))
    dissent = bool(_DISSENT.search(line))
    if concur and dissent or _DISSENT_IN_PART.search(line):
        return "concurrence_dissent"
    if dissent:
        return "dissent"
    if concur:
        return "concurrence"
    if _PLURALITY.search(line):
        return "plurality"
    if _MAJORITY.search(line):
        return "majority"
    return None


def _participation(line: str) -> bool:
    separate = bool(_CONCUR.search(line) or _DISSENT.search(line))
    if _MAJORITY.search(line) and separate:
        return True
    return bool(_JOINED.search(line) and len(_ROLE_COUNT.findall(line)) >= 2
                and _disposition(line) is None)


def _eligible(line: str) -> bool:
    return bool(line) and len(line) <= LINE_MAX_CHARS


def _base(opinion_type: str | None, per_curiam: bool, element: str | None) -> tuple[str, str]:
    row = ROW_TYPES.get(opinion_type or "")
    mapped = ELEMENT_TYPES.get(element or "")
    if row is not None and mapped is not None and row != mapped:
        return "unknown", "none"
    if row is not None:
        return row, "row"
    if per_curiam:
        return "per_curiam", "flag"
    if mapped is not None:
        return mapped, "element"
    return "unknown", "none"


def _cued(cue: str | None, running: tuple[str, str], base: tuple[str, str]) -> tuple[str, str]:
    if cue is None:
        return running
    if cue in _SEPARATE or cue in {"syllabus", "appendix", "order"}:
        return cue, "line"
    # A court-opinion cue never types text a typed source or an earlier line calls separate.
    if base[0] in _SEPARATE or running[0] in _SEPARATE:
        return "unknown", "none"
    if running[0] in _COURT:
        return running
    # After a syllabus, appendix, or order, a typed court opinion resumes its own type.
    if base[0] in _COURT:
        return base
    return cue, "line"


def _label(text: str, start: int, end: int, block_label: str | None, parsed: Parsed) -> str | None:
    if block_label:
        return block_label
    for marker in parsed.markers:
        if marker.kind == "footnote-mark" and marker.offset == start and marker.label.strip() != "↩":
            return marker.label.strip()
    match = _LEADING_LABEL.match(text[start:end])
    return match.group(1) if match else None


def _key(label: str | None) -> str:
    return (label or "").strip().strip("[]").rstrip(".").strip()


def _merged_exclusions(spans: list[tuple[int, int]]) -> tuple[list[int], list[int]]:
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [span[0] for span in merged], [span[1] for span in merged]


def _overlaps(start: int, end: int, starts: list[int], ends: list[int]) -> bool:
    index = bisect_right(ends, start)
    return index < len(starts) and starts[index] < end


def _structural(parsed: Parsed, column: str, paragraphs: list[_Paragraph]) -> list[_Span]:
    text = parsed.text
    spans = [
        _Span(block.start, block.end, "headmatter" if block.kind == "headmatter" else "footnote",
              "markup", block.label or None)
        for block in parsed.blocks if block.kind in {"headmatter", "footnote"}
        and 0 <= block.start < block.end <= len(text)
    ]
    if column == "html_lawbox":
        in_notes = False
        first: int | None = None
        label: str | None = None
        for paragraph in paragraphs:
            if _heading(paragraph.line) == "notes":
                in_notes = True
                continue
            if not in_notes:
                continue
            match = _LAWBOX_LABEL.match(paragraph.line)
            if match:
                if first is not None:
                    spans.append(_Span(first, paragraph.start, "footnote", "markup", label))
                first = paragraph.start
                label = match.group(1)
        if first is not None:
            spans.append(_Span(first, len(text), "footnote", "markup", label))

    starts, ends = _merged_exclusions([
        (span.start, span.end) for span in spans
    ] + [(block.start, block.end) for block in parsed.blocks if block.kind == "quote"])
    for paragraph in paragraphs:
        if (_eligible(paragraph.line) and not _overlaps(paragraph.start, paragraph.end, starts, ends)
                and _author(paragraph.line) and _participation(paragraph.line)):
            spans.append(_Span(paragraph.start, paragraph.end, "headmatter", "line"))

    ordered = sorted(spans, key=lambda span: (span.start, span.end))
    merged: list[_Span] = []
    for span in ordered:
        if (merged and span.section_type == "headmatter" and span.typed_by == "markup"
                and merged[-1].section_type == "headmatter" and merged[-1].typed_by == "markup"
                and span.start >= merged[-1].end
                and not text[merged[-1].end:span.start].strip()):
            previous = merged[-1]
            merged[-1] = _Span(previous.start, span.end, "headmatter", "markup")
        else:
            merged.append(span)
    return merged


def _runs(text: str, spans: list[_Span], opinions: list[Block]) -> list[_Run]:
    points = {0, len(text)}
    starts: dict[int, list[tuple[str, int]]] = defaultdict(list)
    ends: dict[int, list[tuple[str, int]]] = defaultdict(list)
    def add(kind: str, index: int, start: int, end: int) -> None:
        start = max(0, min(len(text), start))
        end = max(0, min(len(text), end))
        if start >= end:
            return
        points.update((start, end))
        starts[start].append((kind, index))
        ends[end].append((kind, index))

    for index, span in enumerate(spans):
        add("span", index, span.start, span.end)
    for index, opinion in enumerate(opinions):
        add("opinion", index, opinion.start, opinion.end)
    active_spans: set[int] = set()
    active_opinions: set[int] = set()
    ordered = sorted(points)
    runs: list[_Run] = []
    for start, end in zip(ordered, ordered[1:], strict=False):
        for kind, index in ends[start]:
            (active_spans if kind == "span" else active_opinions).discard(index)
        for kind, index in starts[start]:
            (active_spans if kind == "span" else active_opinions).add(index)
        span_index = min(active_spans, key=lambda index: (
            spans[index].section_type != "footnote", spans[index].end - spans[index].start,
        )) if active_spans else None
        opinion_index = min(active_opinions, key=lambda index: (
            opinions[index].end - opinions[index].start
        )) if active_opinions else None
        if (runs and runs[-1].end == start and runs[-1].span == span_index
                and runs[-1].opinion == opinion_index):
            previous = runs[-1]
            runs[-1] = _Run(previous.start, end, span_index, opinion_index)
        else:
            runs.append(_Run(start, end, span_index, opinion_index))
    return runs


def _finalize(text: str, openings: list[tuple[int, str, str, str | None]]) -> tuple[Section, ...]:
    if not text:
        return ()
    if not openings:
        openings = [(0, "unknown", "none", None)]
    openings.sort(key=lambda opening: opening[0])
    distinct: list[tuple[int, str, str, str | None]] = []
    for opening in openings:
        if distinct and opening[0] == distinct[-1][0]:
            distinct[-1] = opening
        else:
            distinct.append(opening)
    distinct[0] = (0, *distinct[0][1:])
    sections = tuple(
        Section(kind, typed_by, start, distinct[index + 1][0] if index + 1 < len(distinct) else len(text),
                label, None, None)
        for index, (start, kind, typed_by, label) in enumerate(distinct)
        if start < (distinct[index + 1][0] if index + 1 < len(distinct) else len(text))
    )
    assert sections[0].char_start == 0
    assert sections[-1].char_end == len(text)
    assert all(left.char_end == right.char_start and left.char_start < left.char_end
               for left, right in zip(sections, sections[1:], strict=False))
    assert all(section.char_start < section.char_end for section in sections)
    return sections


def segment(
    parsed: Parsed, *, opinion_type: str | None, per_curiam: bool, column: str,
) -> tuple[Section, ...]:
    """Return sections that tile the parsed text in document order."""

    text = parsed.text
    if not text:
        return ()
    paragraphs = _paragraphs(text)
    paragraph_starts = [paragraph.start for paragraph in paragraphs]
    spans = _structural(parsed, column, paragraphs)
    opinions = [block for block in parsed.blocks if block.kind == "opinion"]
    quote_starts, quote_ends = _merged_exclusions([
        (block.start, block.end) for block in parsed.blocks if block.kind == "quote"
    ])
    openings: list[tuple[int, str, str, str | None]] = []
    base: tuple[str, str] = ("unknown", "none")
    running: tuple[str, str] | None = None
    current_opinion: int | None = None
    for run in _runs(text, spans, opinions):
        if run.span is not None:
            span = spans[run.span]
            start = run.start + len(text[run.start:run.end]) - len(text[run.start:run.end].lstrip())
            if start < run.end:
                label = _label(text, run.start, run.end, span.label, parsed) if span.section_type == "footnote" else None
                openings.append((start, span.section_type, span.typed_by, label))
            continue
        content = text[run.start:run.end]
        if not content.strip():
            continue
        if run.opinion != current_opinion or running is None:
            element = opinions[run.opinion].label if run.opinion is not None else None
            base = running = _base(opinion_type, per_curiam, element)
            current_opinion = run.opinion
        start = run.start + len(content) - len(content.lstrip())
        openings.append((start, *running, None))
        for paragraph_index in range(bisect_left(paragraph_starts, run.start), len(paragraphs)):
            paragraph = paragraphs[paragraph_index]
            if paragraph.start >= run.end:
                break
            if paragraph.start < run.start or paragraph.end > run.end or not _eligible(paragraph.line):
                continue
            if _overlaps(paragraph.start, paragraph.end, quote_starts, quote_ends):
                continue
            heading = _heading(paragraph.line)
            if heading == "notes":
                continue
            if heading is not None:
                running = _cued(heading, running, base)
            elif _author(paragraph.line):
                running = _cued(_disposition(paragraph.line), running, base)
            else:
                continue
            openings.append((paragraph.start, *running, None))
    sections = _finalize(text, openings)

    marks: dict[str, deque[int]] = defaultdict(deque)
    footnote_ranges = [(section.char_start, section.char_end) for section in sections
                       if section.section_type == "footnote"]
    for marker in parsed.markers:
        if marker.kind != "footnote-mark" or marker.label.strip() == "↩":
            continue
        if any(start <= marker.offset < end for start, end in footnote_ranges):
            continue
        marks[_key(marker.label)].append(marker.offset)
    resolved = list(sections)
    for index, section in enumerate(sections):
        if section.section_type != "footnote":
            continue
        matches = marks[_key(section.label)]
        if not section.label or not matches:
            continue
        offset = matches.popleft()
        parent = next((other_index for other_index, other in enumerate(sections)
                       if other.section_type != "footnote"
                       and other.char_start <= offset < other.char_end), None)
        resolved[index] = replace(section, ref_offset=offset, parent=parent)
    result = tuple(resolved)
    assert result[0].char_start == 0 and result[-1].char_end == len(text)
    assert all(left.char_end == right.char_start
               for left, right in zip(result, result[1:], strict=False))
    return result
