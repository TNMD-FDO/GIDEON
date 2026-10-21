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
_QUALIFIED_OPINION: Final = re.compile(r"(?:amended|supplemental|corrected) opinion")
_HEADING_PUNCT: Final = re.compile(r"[^\w\s]|_")
_SPACES: Final = re.compile(r"\s+")

# exempt: these bounded byline and disposition shapes are starting values to be measured.
PREAMBLE_MAX_CHARS: Final = 60
_JOINDER_VERBS: Final = r"\b(?:join|joins|joined|concur|concurs|concurred)\b"
_NAME: Final = r"[A-Z][A-Za-z'’.-]*(?:\s+[A-Z][A-Za-z'’.-]*){0,3}"
_ROLE: Final = (
    r"(?:C\.\s*J\.|JJ\.|J\.|Chief Judge|Circuit Judge|District Judge|"
    r"Senior (?:Circuit |District )?Judge|Judge|Chief Justice|Justice)"
)
_BEFORE: Final = re.compile(rf"(?:Before|BEFORE)\b:?[\s\S]*?\b(?i:{_ROLE})")
# A byline is a paragraph's prefix: a name and role followed by nothing, a
# joiners' clause (which may wrap), or a disposition, ending at the paragraph's
# end or at the first period after it that whitespace follows, a role's own
# period passed over. A separate opinion's disposition is its closed words, so
# prose such as "dissenting in that case, argued ..." is not one. One short
# clause naming no judge may stand before a disposition, never before a bare
# end, and is tried last, so a disposition's own comma never opens one; the
# disposition is read from the words after the joiners' clause, whose verb
# may be a concurrence's. The joiners' clause passes an honorific's or a
# role's period, as a delivery's tail does. A disposition's verb may be tensed
# ("dissented", "concurs"), may stand in parentheses, and may end in the adverb
# "specially". Two judges may share one byline, joined by "and", the first
# being its judge, and a name before its role may carry a generational suffix.
_DISPOSITION_WORDS: Final = (
    r"(?:in|part|and|the|judgment|result|concurring|dissenting|from|denial|of|"
    r"certiorari|per|curiam|specially)\b"
)
_BYLINE_END: Final = r"(?:[\s.,:;]*$|\.[*†‡]?(?=\s)|(?<=\.)(?=\s))"
_JUDGE: Final = re.compile(
    rf"(?:({_NAME})(?:,\s*(?i:Jr|Sr|II|III|IV)\.?)?,\s*(?i:{_ROLE})"
    rf"|(?i:(?:Mr\.\s+)?(?:Chief\s+)?Justice)\s+({_NAME}))"
)
_BYLINE_START: Final = (
    rf"{_JUDGE.pattern}(?:,?\s+(?i:and)\s+{_JUDGE.pattern})?"
)
_BYLINE_JOINERS: Final = (
    rf"(?:(?:with\s+)?whom\b[\s\S]{{0,200}}?{_JOINDER_VERBS}"
    rf"(?:Mr\.|{_ROLE}|[^.]){{0,80}}?[\s,:;]*)?"
)
_BYLINE_TAIL: Final = (
    rf"(?:(?:delivered|announced|for\s+the\s+court)\b(?:{_ROLE}|[^.])*?{_BYLINE_END}"
    rf"|\(?(?:concurring|dissenting|concurred|concurs|dissented|dissents)\b"
    rf"(?:[\s,]+{_DISPOSITION_WORDS})*\)?{_BYLINE_END}"
    r")"
)
_BYLINE_PREAMBLE: Final = (
    r"(?!\s*(?:(?:with\s+)?whom|concurring|dissenting|delivered|announced|"
    r"for\s+the\s+court|per\s+curiam)\b)"
    rf"(?![^.,]*\b{_ROLE}(?!\w))[^.,]{{1,{PREAMBLE_MAX_CHARS}}},\s*"
)
_BYLINE_SUFFIX: Final = (
    rf"[\s,:;]*(?:{_BYLINE_JOINERS}(?P<tail>{_BYLINE_TAIL})"
    rf"|{_BYLINE_JOINERS}{_BYLINE_END}"
    rf"|{_BYLINE_PREAMBLE}{_BYLINE_JOINERS}(?P<tail_after_preamble>{_BYLINE_TAIL}))"
)
_AUTHOR: Final = re.compile(
    rf"{_BYLINE_START}(?i:{_BYLINE_SUFFIX})"
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
_JOINDER: Final = re.compile(_JOINDER_VERBS, re.IGNORECASE)
_ROLE_COUNT: Final = re.compile(rf"(?i:{_ROLE})")
_PARTICIPANT: Final = re.compile(
    rf"^{_BYLINE_START}(?i:(?:{_ROLE}|[^.])*?\b(?:delivered|filed|joined|"
    rf"took\s+no\s+part|{_CONCUR.pattern}|{_DISSENT.pattern})\b)"
)
_CLOSING: Final = re.compile(r"(?:It\s+is\s+so\s+ordered|So\s+ordered)\.\s+", re.IGNORECASE)
_LINE_BREAK_HYPHEN: Final = re.compile(r"(?<=[a-z])-[ \t]*\n\s*(?=[a-z])")
_PER_CURIAM_PREFIX: Final = re.compile(r"per\s+curiam(?:[.:;]|$)", re.IGNORECASE)
_LEADING_LABEL: Final = re.compile(r"^\s*(\[[^\[\]\s]+\]|\d+\.|\*+)(?=\s|$)")
_LAWBOX_LABEL: Final = re.compile(r"^\s*(\[[^\[\]\s]+\])(?=\s|$)")

# exempt: these page shapes are starting values to be measured against opinion samples.
_SLIP_HEAD: Final = re.compile(r"^(?:\(Slip Opinion\)|Cite as:)")
_SLIP_EVEN_HEAD: Final = re.compile(r"[0-9]{1,3}\s+[A-Z][^a-z]*\bv\.(?:\s[^a-z]*)?(?:\b(?:ex rel|et al)\.[^a-z]*)*")
_SLIP_OPINION_HEAD: Final = re.compile(rf"(?i:Opinion|Statement)\s+(?i:of)\s+{_NAME},\s*(?i:{_ROLE})[.,:;]?")
_SLIP_NOTICE: Final = re.compile(r"^(?:NOTE|NOTICE):")
_SLIP_COURT: Final = re.compile(r"SUPREME COURT OF THE UNITED STATES")
_CALENDAR_DATE: Final = (
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+[0-9]{1,2},\s+[0-9]{4}"
)
_SLIP_DATE: Final = re.compile(rf"\[{_CALENDAR_DATE}\]|\bDecided\s+{_CALENDAR_DATE}\s*[*†‡]?")
_SLIP_HELD: Final = re.compile(r"^Held:")
_SLIP_DIVIDER: Final = re.compile(r"^[ \t]*—{2,}[ \t]*", re.MULTILINE)
_SLIP_FOOTNOTE: Final = re.compile(r"^([0-9]{1,3}|[*†‡])\s+\S")
_CIRCUIT_HEAD: Final = re.compile(
    r"Nos?\.\s+[0-9]{2}-[0-9]{4}(?:\s*[/,]\s*[0-9]{2}-[0-9]{4})*"
    r"(?:\s+.*)?\s+Page\s+[0-9]+"
)
_CIRCUIT_FOOTNOTE: Final = re.compile(r"^[ \t]*([0-9]{1,3})[ \t]*$", re.MULTILINE)


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


@dataclass(slots=True)
class _PageState:
    armed: str | None = None
    page_head_start: int | None = None
    expect_running_head: bool = False
    running_head_kind: str | None = None
    first_page: bool = False
    alone: bool = False
    alone_type: str | None = None
    after_running_head: bool = False
    in_caption: bool = False
    caption_dated: bool = False
    in_footnotes: bool = False
    footnote_owed: bool = False
    resume: bool = False
    switch: str | None = None


def paragraphs(text: str, start: int, end: int) -> list[_Paragraph]:
    """Return nonblank paragraph spans within a section of canonical text."""

    result: list[_Paragraph] = []
    position = start
    while position < end:
        separator = text.find("\n\n", position, end)
        paragraph_end = end if separator < 0 else separator
        line = text[position:paragraph_end].strip()
        if line:
            # The paragraph begins at its text, so a separator stays with the section before.
            first = position + len(text[position:paragraph_end]) - len(text[position:paragraph_end].lstrip())
            result.append(_Paragraph(first, paragraph_end, line))
        position = paragraph_end + 2
    return result


def _heading(line: str) -> str | None:
    normalized = _SPACES.sub(" ", _HEADING_PUNCT.sub(" ", line.casefold())).strip()
    if _APPENDIX_HEADING.fullmatch(normalized):
        return "appendix"
    if _QUALIFIED_OPINION.fullmatch(normalized):
        return "majority"
    return HEADING_CUES.get(normalized)


def _unbroken(line: str) -> str:
    return _LINE_BREAK_HYPHEN.sub("", line)


def _before(line: str) -> bool:
    return _BEFORE.match(_unbroken(line)[:LINE_MAX_CHARS]) is not None


def _after_closing(line: str) -> int:
    # A byline the dump ran on after the previous opinion's closing formula is read past it.
    closing = _CLOSING.match(line)
    return closing.end() if closing else 0


def _byline(line: str) -> str | None:
    # A word the dump hyphenated across a line break is read whole; one extra
    # character prevents a truncated paragraph from passing as a byline that
    # ends exactly at the bound.
    bounded = _unbroken(line)[:LINE_MAX_CHARS + 1]
    position = _after_closing(bounded)
    match = _PER_CURIAM_PREFIX.match(bounded, position) or _AUTHOR.match(bounded, position)
    if match and _eligible(bounded[:match.end()]):
        return bounded[:match.end()]
    return None


def _judge(prefix: str) -> str | None:
    # The full byline fixes the name's end; the first start's two branches
    # are the pattern's first two groups, so a second judge is never the one read.
    match = _AUTHOR.match(prefix, _after_closing(prefix))
    if match is None:
        return None
    return _SPACES.sub(" ", (match.group(1) or match.group(2)).casefold()).strip()


def _disposition(line: str) -> str | None:
    match = _AUTHOR.match(line, _after_closing(line))
    if match is not None:
        tail = match.group("tail") or match.group("tail_after_preamble")
        if tail is None:
            return None
        line = tail
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
    # A byline delivering the Court's opinion in part is the Court's, though it announces the judgment.
    if _MAJORITY.search(line):
        return "majority"
    if _PLURALITY.search(line):
        return "plurality"
    return None


def _participation(line: str) -> bool:
    prefix = _byline(line)
    if prefix is None:
        return False
    disposition = _disposition(prefix)
    if (_JOINDER.search(prefix) and len(_ROLE_COUNT.findall(prefix)) >= 2
            and (disposition is None or disposition in _COURT)):
        return True
    return bool(_PARTICIPANT.match(_unbroken(line)[len(prefix):].lstrip()))


def _eligible(line: str) -> bool:
    return bool(line) and len(line) <= LINE_MAX_CHARS


def _running_head(line: str) -> str | None:
    if not _eligible(line):
        return None
    heading = _heading(line)
    if heading == "syllabus":
        return "syllabus"
    if heading in {"majority", "per_curiam"}:
        return "opinion"
    if _SLIP_OPINION_HEAD.fullmatch(line) or _byline(line) == _unbroken(line):
        return "separate"
    return None


def _even_head(line: str) -> str | None:
    # An even page's head is its number and caption, read once a slip is armed;
    # the running head after it, when the dump ran both into one line, is returned.
    if _SLIP_EVEN_HEAD.fullmatch(line):
        return ""
    for space in _SPACES.finditer(line):
        if (_SLIP_EVEN_HEAD.fullmatch(line[:space.start()])
                and _running_head(line[space.end():]) is not None):
            return line[space.end():]
    return None


def _read_running_head(state: _PageState, line: str, running: tuple[str, str]) -> bool:
    state.running_head_kind = _running_head(line)
    if state.running_head_kind == "separate":
        prefix = _byline(line)
        disposition = _disposition(prefix) if prefix is not None else None
        if state.first_page:
            # A slip whose first page heads a separate writing holds that writing alone.
            state.alone = True
            state.alone_type = disposition
        elif disposition in _SEPARATE and disposition != running[0]:
            # A page headed by a separate opinion is that opinion's, whatever its byline read.
            state.switch = disposition
    state.first_page = False
    return state.running_head_kind is not None


def _page(
    state: _PageState, paragraph: _Paragraph, running: tuple[str, str],
) -> tuple[bool, _Span | None]:
    line = paragraph.line
    lines = line.splitlines()
    even = _even_head(lines[0].rstrip()) if state.armed == "slip" else None
    slip_head = state.armed in {None, "slip"} and _SLIP_HEAD.match(lines[0]) or even is not None
    circuit_head = (state.armed in {None, "circuit"} and len(lines) == 1
                    and _CIRCUIT_HEAD.fullmatch(line))
    if slip_head or circuit_head:
        state.first_page = state.armed is None
        state.armed = "slip" if slip_head else "circuit"
        if slip_head:
            # A caption that wraps leaves the running head on a later line of the head.
            later = [part.strip() for part in lines[1:]]
            head_line = even or next((part for part in later if _running_head(part)), later[0] if later else "")
            state.page_head_start = paragraph.start
            state.expect_running_head = not head_line
            state.running_head_kind = None
            state.after_running_head = bool(head_line) and _read_running_head(state, head_line, running)
        if state.in_footnotes or state.in_caption:
            state.in_footnotes = state.in_caption = False
            state.resume = True
        state.footnote_owed = False
        return True, None

    if state.armed == "slip":
        if state.expect_running_head:
            state.expect_running_head = False
            if _read_running_head(state, line, running):
                state.after_running_head = True
                return True, None
        after_running_head = state.after_running_head
        state.after_running_head = False
        if after_running_head and _SLIP_NOTICE.match(line):
            return True, None
        # A caption block reaches back only over its own page's head furniture.
        head, state.page_head_start = state.page_head_start, None
        if not state.in_caption and _SLIP_COURT.fullmatch(lines[0].rstrip()):
            start = head if head is not None else paragraph.start
            if state.alone:
                # Its front block takes the writing's own type, or none for a statement.
                state.alone = False
                if state.alone_type is None:
                    return True, None
                return False, _Span(start, paragraph.end, state.alone_type, "line")
            state.in_caption = True
            state.caption_dated = False
            return True, _Span(start, paragraph.end, "headmatter", "line")
        if state.in_caption:
            date = _SLIP_DATE.search(line)
            # A date line merged with the summary's first sentence takes the summary's mark.
            merged = date is not None and len(line) - date.end() > date.end()
            if date is not None and not merged:
                state.caption_dated = True
                return True, None
            if _byline(line) is not None:
                state.in_caption = False
            elif (state.running_head_kind == "syllabus"
                    and (merged or state.caption_dated or _SLIP_HELD.match(line))):
                state.in_caption = False
                state.resume = False
                return False, _Span(paragraph.start, paragraph.end, "syllabus", "line")
            elif merged or state.caption_dated:
                # After its date the block ends and the body resumes.
                state.in_caption = False
                state.resume = True
            else:
                return True, None
    # A footnote block opens at a slip's rule or a circuit's number on a line of its own.
    marker = _SLIP_DIVIDER if state.armed == "slip" else _CIRCUIT_FOOTNOTE if state.armed else None
    found = marker.search(line) if marker is not None else None
    if found is not None and found.start() > 0:
        if state.in_footnotes:
            found = None
        else:
            # Opened inside a paragraph, the paragraph takes its larger part's mark,
            # and a body part that is larger leaves the footnote to the next paragraph.
            state.in_footnotes = True
            rest = line[found.start():] if state.armed == "circuit" else line[found.end():].lstrip()
            if len(rest) <= found.start():
                state.footnote_owed = True
                return _body(state, paragraph, running)
            return True, _Span(paragraph.start, paragraph.end, "footnote", "line", _footnote_label(state, rest))
    if found is not None or state.in_footnotes:
        state.in_footnotes = True
        rest = line[found.end():].lstrip() if found is not None and state.armed == "slip" else line
        if not rest:
            # The rule alone continues the section; the footnote begins after it.
            state.footnote_owed = True
            return True, None
        label = _footnote_label(state, rest)
        if found is not None or label is not None or state.footnote_owed:
            state.footnote_owed = False
            return True, _Span(paragraph.start, paragraph.end, "footnote", "line", label)
        return True, None
    return _body(state, paragraph, running)


def _footnote_label(state: _PageState, text: str) -> str | None:
    opener = (_SLIP_FOOTNOTE if state.armed == "slip" else _CIRCUIT_FOOTNOTE).match(text)
    return opener.group(1) if opener else None


def _body(
    state: _PageState, paragraph: _Paragraph, running: tuple[str, str],
) -> tuple[bool, _Span | None]:
    line = paragraph.line
    if _eligible(line) and _participation(line):
        if state.armed is not None and running[0] == "syllabus":
            return True, None
        state.resume = True
        return True, _Span(paragraph.start, paragraph.end, "headmatter", "line")
    if state.resume or state.switch is not None:
        kind = running if state.switch is None else (state.switch, "line")
        state.resume, state.switch = False, None
        return False, _Span(paragraph.start, paragraph.end, *kind)
    return False, None


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


def _bare(
    running: tuple[str, str], base: tuple[str, str], armed: bool, bare_opened: bool,
    judge: str | None, opener: str | None,
) -> tuple[tuple[str, str], bool, bool, str | None]:
    # In an untyped document the first bare author line after the panel's line
    # opens the court's opinion, and a second under that opening is not guessed.
    if base == running == ("unknown", "none") and armed:
        return ("majority", "line"), False, True, None
    if bare_opened:
        return ("unknown", "none"), armed, False, None
    # Another judge's bare line under a court's opinion an author line opened
    # is not the court's; the document is typed by line, so the panel's arming is spent.
    if running[0] in _COURT and running[1] == "line" and opener is not None and judge != opener:
        return ("unknown", "none"), False, False, None
    return running, armed, bare_opened, opener


def _read_byline(
    prefix: str, running: tuple[str, str], base: tuple[str, str], armed: bool, bare_opened: bool,
    opener: str | None,
) -> tuple[tuple[str, str], bool, bool, str | None]:
    disposition = _disposition(prefix)
    judge = _judge(prefix)
    if disposition is None:
        return _bare(running, base, armed, bare_opened, judge, opener)
    resolved = _cued(disposition, running, base)
    opener = judge if resolved[0] in _COURT and resolved[1] == "line" else None
    return resolved, armed, False, opener


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

    # Head matter is what precedes the opinion, so a head-matter block that
    # begins after the first paragraph outside every block is a reporter's
    # note the vocabulary lacks.
    block_starts, block_ends = _merged_exclusions([(span.start, span.end) for span in spans])
    body = next((paragraph.start for paragraph in paragraphs
                 if not _overlaps(paragraph.start, paragraph.end, block_starts, block_ends)), None)
    if body is not None:
        spans = [replace(span, section_type="unknown", typed_by="none")
                 if span.section_type == "headmatter" and span.start > body else span
                 for span in spans]

    starts, ends = _merged_exclusions([
        (span.start, span.end) for span in spans
    ] + [(block.start, block.end) for block in parsed.blocks if block.kind == "quote"])
    for paragraph in paragraphs:
        if (column != "plain_text" and _eligible(paragraph.line)
                and not _overlaps(paragraph.start, paragraph.end, starts, ends)
                and _participation(paragraph.line)):
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
    paragraph_rows = paragraphs(text, 0, len(text))
    paragraph_starts = [paragraph.start for paragraph in paragraph_rows]
    spans = _structural(parsed, column, paragraph_rows)
    opinions = [block for block in parsed.blocks if block.kind == "opinion"]
    quote_starts, quote_ends = _merged_exclusions([
        (block.start, block.end) for block in parsed.blocks if block.kind == "quote"
    ])
    openings: list[tuple[int, str, str, str | None]] = []
    base: tuple[str, str] = ("unknown", "none")
    running: tuple[str, str] | None = None
    armed = bare_opened = False
    opener: str | None = None
    pending: str | None = None
    current_opinion: int | None = None
    page = _PageState() if column == "plain_text" else None
    for run in _runs(text, spans, opinions):
        if run.span is not None:
            span = spans[run.span]
            start = run.start + len(text[run.start:run.end]) - len(text[run.start:run.end].lstrip())
            if start < run.end:
                label = _label(text, run.start, run.end, span.label, parsed) if span.section_type == "footnote" else None
                openings.append((start, span.section_type, span.typed_by, label))
            if span.section_type == "headmatter" and span.typed_by == "markup":
                # An author line closing the block cues the body after it;
                # the block keeps its markup type.
                block_paragraphs = paragraphs(text, run.start, run.end)
                for paragraph in block_paragraphs:
                    if _before(paragraph.line):
                        armed = True
                closing = block_paragraphs[-1].line if block_paragraphs else ""
                pending = None if not closing or _participation(closing) else _byline(closing)
            continue
        content = text[run.start:run.end]
        if not content.strip():
            continue
        if run.opinion != current_opinion or running is None:
            element = opinions[run.opinion].label if run.opinion is not None else None
            base = running = _base(opinion_type, per_curiam, element)
            bare_opened = False
            opener = None
            current_opinion = run.opinion
        if pending is not None:
            running, armed, bare_opened, opener = _read_byline(
                pending, running, base, armed, bare_opened, opener,
            )
            pending = None
        start = run.start + len(content) - len(content.lstrip())
        openings.append((start, *running, None))
        for paragraph_index in range(bisect_left(paragraph_starts, run.start), len(paragraph_rows)):
            paragraph = paragraph_rows[paragraph_index]
            if paragraph.start >= run.end:
                break
            if paragraph.start < run.start or paragraph.end > run.end:
                continue
            if _overlaps(paragraph.start, paragraph.end, quote_starts, quote_ends):
                continue
            if page is not None:
                skip, opening = _page(page, paragraph, running)
                if opening is not None:
                    openings.append((opening.start, opening.section_type, opening.typed_by, opening.label))
                    if not skip:
                        if opening.section_type != running[0]:
                            bare_opened = False
                            opener = None
                        running = opening.section_type, opening.typed_by
                if skip:
                    continue
            heading = _heading(paragraph.line) if _eligible(paragraph.line) else None
            if heading == "notes":
                continue
            if heading is not None:
                bare_opened = False
                opener = None
                running = _cued(heading, running, base)
            elif _before(paragraph.line):
                armed = True
                continue
            elif (prefix := _byline(paragraph.line)) is not None:
                running, armed, bare_opened, opener = _read_byline(
                    prefix, running, base, armed, bare_opened, opener,
                )
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
