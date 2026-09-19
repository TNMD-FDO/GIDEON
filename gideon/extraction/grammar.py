"""The GIDEON regex grammar: the exact-object families of spec §11.2.

Every pattern is a bounded regular expression with a versioned id in the
``family/pattern@N`` convention and declares the one type it emits; the
registry's order is the families' precedence, and ``registry_types`` is what
the extraction gate is handed, so a family that emits nothing stays gated.
``extract`` finds every candidate, gives a position to the earliest candidate
(at one start, the family first in precedence, then the longest), and returns
the objects ordered and disjoint under the contract in ``contract.py``.

A section is a ``statute`` only when its construction states a title, else a
``bare_section`` with no key.  A hyphen after a letter joins one section
(``2000e-5``); a hyphen or en dash between two all-digit numbers is a range
only under a plural marker, its two endpoints the objects.  A markerless
bare section needs a subsection or a cue word beside it, and a number in the
year range needs a subsection or a following cue; a bare section declines
inside a rule, C.F.R., state-code, appendix, or habeas-rules construction.
A pattern whose behaviour changes takes ``@N+1``, and ``GRAMMAR_VERSION``
moves with any pattern.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from gideon.extraction.contract import ExactObject, ObjectType

GRAMMAR_VERSION: Final[int] = 1

BARE_SECTION_CUES: Final[tuple[str, ...]] = (
    "under",
    "court",
    "apply",
    "applies",
    "conviction",
    "convictions",
)


@dataclass(frozen=True, slots=True)
class Pattern:
    """One immutable, versioned bounded expression and its emitted type."""

    id: str
    expression: re.Pattern[str]
    type: ObjectType


_WS = r"[\s\u00a0]{1,8}"
_OPTIONAL_WS = r"[\s\u00a0]{0,8}"
_CODE_GAP = r"[.\s\u00a0]{0,3}"
_CODE_TOKEN = (
    rf"(?:U{_CODE_GAP}S{_CODE_GAP}C{_CODE_GAP}A{_CODE_GAP}"
    rf"|U{_CODE_GAP}S{_CODE_GAP}C{_CODE_GAP}"
    rf"|U{_CODE_GAP}S{_CODE_GAP}Code"
    rf"|United{_WS}States{_WS}Code)"
)
_GUIDELINE_ID = r"[0-9][A-Za-z][0-9]{1,3}\.[0-9]{1,3}"
_GUIDELINE_TOKEN = rf"U{_CODE_GAP}S{_CODE_GAP}S{_CODE_GAP}G{_CODE_GAP}"
_GUIDELINE_MARKER = r"(?:§{1,2}|section|sec[.])"
_GUIDELINE_PREFIX = (
    rf"(?:(?:{_GUIDELINE_TOKEN}|Guideline{_WS}){_OPTIONAL_WS}(?:{_GUIDELINE_MARKER}{_OPTIONAL_WS}){{0,1}}"
    rf"|{_GUIDELINE_MARKER}{_OPTIONAL_WS})"
)
_SECTION_ATOM = r"[0-9]{1,6}[A-Za-z]{0,2}"
_SECTION_TOKEN = rf"{_SECTION_ATOM}(?:[-–]{_SECTION_ATOM}){{0,1}}"
_NUMBER_ONLY = r"[0-9]{3,6}[A-Za-z]{0,2}"
_SUBSECTION = r"\([A-Za-z0-9]{1,4}\)"
_SUBSECTIONS = rf"(?:{_SUBSECTION}){{0,8}}"
_SECTION_MARKER = r"(?:§{1,2}|sections|section|secs[.]|sec[.])"
_PLURAL_MARKER = r"(?:§{2}|sections|secs[.])"
_LIST_SEPARATOR = rf"(?:,{_OPTIONAL_WS}(?:and|or|&)|,|and|or|&|through|to)"
_LEFT_BOUNDARY = r"(?<![A-Za-z0-9_.–-])"
_RIGHT_BOUNDARY = r"(?![A-Za-z0-9_–-]|[.][A-Za-z0-9])"
_RULE_GAP = r"[.\s\u00a0]{0,3}"
_RULE_NUMBER = r"[0-9]{1,4}(?:\.[0-9]{1,2}){0,1}"
_CRIMINAL_SHORT = rf"(?:Fed{_RULE_GAP}R{_RULE_GAP}Cr(?:im){{0,1}}{_RULE_GAP}P{_RULE_GAP}|F{_RULE_GAP}R{_RULE_GAP}Cr(?:im){{0,1}}{_RULE_GAP}P{_RULE_GAP})"
_EVIDENCE_SHORT = rf"(?:Fed{_RULE_GAP}R{_RULE_GAP}Evid{_RULE_GAP}|F{_RULE_GAP}R{_RULE_GAP}E{_RULE_GAP})"
_APPELLATE_SHORT = rf"(?:Fed{_RULE_GAP}R{_RULE_GAP}App{_RULE_GAP}P{_RULE_GAP}|F{_RULE_GAP}R{_RULE_GAP}(?:App{_RULE_GAP}P|A{_RULE_GAP}P){_RULE_GAP})"
_CIVIL_SHORT = rf"(?:Fed{_RULE_GAP}R{_RULE_GAP}Civ{_RULE_GAP}P{_RULE_GAP}|F{_RULE_GAP}R{_RULE_GAP}(?:Civ{_RULE_GAP}P|C{_RULE_GAP}P){_RULE_GAP})"
_CRIMINAL_LONG = rf"Federal{_WS}Rule(?:s){{0,1}}{_WS}of{_WS}Criminal{_WS}Procedure"
_EVIDENCE_LONG = rf"Federal{_WS}Rule(?:s){{0,1}}{_WS}of{_WS}Evidence"
_APPELLATE_LONG = rf"Federal{_WS}Rule(?:s){{0,1}}{_WS}of{_WS}Appellate{_WS}Procedure"
_CIVIL_LONG = rf"Federal{_WS}Rule(?:s){{0,1}}{_WS}of{_WS}Civil{_WS}Procedure"
_RULE_SETS: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("18a", "Crim", _CRIMINAL_SHORT, _CRIMINAL_LONG),
    ("28a", "Civil", _CIVIL_SHORT, _CIVIL_LONG),
    ("28a", "App", _APPELLATE_SHORT, _APPELLATE_LONG),
    ("28a", "Evid", _EVIDENCE_SHORT, _EVIDENCE_LONG),
)
"""The four pilot rule sets: the USLM appendix title, the set's path segment, its forms."""
_RULE_SET_LONG_SOURCE = "(?:" + "|".join(long for _, _, _, long in _RULE_SETS) + ")"
_RULE_SET_SOURCE = (
    "(?:" + "|".join(short for _, _, short, _ in _RULE_SETS) + f"|{_RULE_SET_LONG_SOURCE})"
)
_RULE_SET_FORMS: Final[tuple[tuple[str, str, re.Pattern[str]], ...]] = tuple(
    (title, segment, re.compile(f"(?:{short}|{long})", re.IGNORECASE))
    for title, segment, short, long in _RULE_SETS
)

_TITLED_SECTION_SOURCE = (
    rf"{_LEFT_BOUNDARY}(?P<title>[0-9]{{1,3}}){_WS}{_CODE_TOKEN}{_WS}"
    rf"(?:(?P<marker>(?:§|section|sec[.])){_OPTIONAL_WS}){{0,1}}"
    rf"(?P<section>{_SECTION_TOKEN})(?P<subsections>{_SUBSECTIONS}){_RIGHT_BOUNDARY}"
)
_LIST_SOURCE = (
    rf"{_LEFT_BOUNDARY}(?P<title>[0-9]{{1,3}}){_WS}{_CODE_TOKEN}{_WS}"
    rf"(?P<list_marker>{_PLURAL_MARKER}){_OPTIONAL_WS}"
    rf"(?P<body>{_SECTION_TOKEN}{_SUBSECTIONS}"
    rf"(?:{_OPTIONAL_WS}{_LIST_SEPARATOR}{_OPTIONAL_WS}(?:{_SECTION_MARKER}{_OPTIONAL_WS})?"
    rf"{_SECTION_TOKEN}{_SUBSECTIONS}){{0,3}}){_RIGHT_BOUNDARY}"
)
_BARE_SOURCE = (
    rf"(?:"
    rf"{_LEFT_BOUNDARY}(?P<marker>{_SECTION_MARKER}){_OPTIONAL_WS}"
    rf"(?P<marked_section>{_SECTION_TOKEN})(?P<marked_subsections>{_SUBSECTIONS})"
    rf"{_RIGHT_BOUNDARY}"
    rf"|{_LEFT_BOUNDARY}(?P<sub_section>{_SECTION_TOKEN})"
    rf"(?P<subsections_required>(?:{_SUBSECTION}){{1,8}}){_RIGHT_BOUNDARY}"
    rf"|{_LEFT_BOUNDARY}(?P<cue_number>{_NUMBER_ONLY}){_RIGHT_BOUNDARY}"
    rf")"
)
_GUIDELINE_SOURCE = (
    rf"{_LEFT_BOUNDARY}(?P<guideline_prefix>{_GUIDELINE_PREFIX}){{0,1}}"
    rf"(?P<guideline_id>{_GUIDELINE_ID})(?P<subsections>{_SUBSECTIONS}){_RIGHT_BOUNDARY}"
)
_COURT_RULE_SOURCE = (
    rf"{_LEFT_BOUNDARY}(?P<set_form>{_RULE_SET_SOURCE}){_WS}"
    rf"(?P<number>{_RULE_NUMBER})(?P<subsections>{_SUBSECTIONS}){_RIGHT_BOUNDARY}"
    rf"|{_LEFT_BOUNDARY}Rule{_WS}(?P<long_number>{_RULE_NUMBER})"
    rf"(?P<long_subsections>{_SUBSECTIONS}){_WS}of{_WS}the{_WS}"
    rf"(?P<long_set>{_RULE_SET_LONG_SOURCE}){_RIGHT_BOUNDARY}"
)
_BARE_RULE_SOURCE = (
    rf"{_LEFT_BOUNDARY}Rule{_WS}(?P<bare_number>{_RULE_NUMBER})"
    rf"(?P<bare_subsections>{_SUBSECTIONS}){_RIGHT_BOUNDARY}"
)


PATTERN_REGISTRY: Final[tuple[Pattern, ...]] = (
    Pattern(
        "usc/titled-section@1",
        re.compile(_TITLED_SECTION_SOURCE, re.IGNORECASE),
        "statute",
    ),
    Pattern(
        "usc/titled-list-member@1",
        re.compile(_LIST_SOURCE, re.IGNORECASE),
        "statute",
    ),
    Pattern("ussg/id@1", re.compile(_GUIDELINE_SOURCE, re.IGNORECASE), "guideline"),
    Pattern(
        "rules/set-and-number@1",
        re.compile(_COURT_RULE_SOURCE, re.IGNORECASE),
        "court_rule",
    ),
    Pattern("rules/bare-number@1", re.compile(_BARE_RULE_SOURCE, re.IGNORECASE), "bare_rule"),
    Pattern("usc/bare-section@1", re.compile(_BARE_SOURCE, re.IGNORECASE), "bare_section"),
)

_SUBSECTION_CAPTURE = re.compile(r"\(([A-Za-z0-9]{1,4})\)")
_LIST_MEMBER_CAPTURE = re.compile(
    rf"(?:{_SECTION_MARKER}{_OPTIONAL_WS}){{0,1}}"
    rf"(?P<section>{_SECTION_TOKEN})(?P<subsections>{_SUBSECTIONS})",
    re.IGNORECASE,
)
_RANGE_CAPTURE = re.compile(r"(?P<left>[0-9]{1,6})(?P<dash>[-–])(?P<right>[0-9]{1,6})")
_WORD_CAPTURE = re.compile(r"[A-Za-z][A-Za-z-]{0,31}")
_RULE_CONTEXT = re.compile(
    rf"(?<![A-Za-z])(?:Rule|{_RULE_SET_SOURCE}){_OPTIONAL_WS}$", re.IGNORECASE
)
_CFR_CONTEXT = re.compile(r"\bc[.]?[\s\u00a0]*f[.]?[\s\u00a0]*r[.]?[\s\u00a0]*$", re.IGNORECASE)
_STATE_CONTEXT = re.compile(
    r"\b[A-Za-z]{2,12}[.]?[\s\u00a0]+Code[\s\u00a0]+Ann[.]?[\s\u00a0]*$",
    re.IGNORECASE,
)
_APPENDIX_CONTEXT = re.compile(
    rf"\bapp[.]?{_WS}[0-9]{{1,3}}{_OPTIONAL_WS}$", re.IGNORECASE
)
_STATE_CODE_SHAPE = re.compile(r"[0-9]{1,6}-[0-9]{1,6}-[0-9]{1,6}\Z")
_HABEAS_RULES_CONTEXT = re.compile(
    rf"\brules{_WS}governing{_WS}(?:{_SECTION_MARKER}{_OPTIONAL_WS}){{0,1}}$", re.IGNORECASE
)
_HABEAS_RULE_FOLLOWING = re.compile(
    rf"^{_WS}of{_WS}the{_WS}rules{_WS}governing{_WS}"
    rf"(?:section|§){_OPTIONAL_WS}225[45]{_WS}(?:cases|proceedings)\b",
    re.IGNORECASE,
)
_SUPREME_RULE_PREFIX = re.compile(
    rf"(?:\bsupreme{_WS}court|\bsup[.]{_OPTIONAL_WS}ct[.]?){_OPTIONAL_WS}$",
    re.IGNORECASE,
)
_YEAR_RANGE: Final[range] = range(1900, 2100)
_CUE_WINDOW: Final[int] = 48


@dataclass(frozen=True, slots=True)
class _Candidate:
    start: int
    end: int
    precedence: int
    objects: tuple[ExactObject, ...]


def registry_types() -> tuple[ObjectType, ...]:
    """Return the object types statically declared by the pattern registry."""

    result: list[ObjectType] = []
    for pattern in PATTERN_REGISTRY:
        if pattern.type not in result:
            result.append(pattern.type)
    return tuple(result)


def _subsections(value: str) -> tuple[str, ...]:
    return tuple(match.group(1) for match in _SUBSECTION_CAPTURE.finditer(value))


def _statute(
    text: str,
    start: int,
    end: int,
    title: str,
    section: str,
    pattern_id: str,
) -> ExactObject:
    return ExactObject(
        "statute",
        start,
        end,
        text[start:end],
        f"/us/usc/t{title}/s{section}",
        pattern_id,
        _subsections(text[start:end]),
    )


def _bare(text: str, start: int, end: int, pattern_id: str) -> ExactObject:
    return ExactObject(
        "bare_section",
        start,
        end,
        text[start:end],
        pattern_id=pattern_id,
        subsections=_subsections(text[start:end]),
    )


def _guideline(
    text: str,
    start: int,
    end: int,
    guideline_id: str,
    pattern_id: str,
) -> ExactObject:
    return ExactObject(
        "guideline",
        start,
        end,
        text[start:end],
        f"ussg/{guideline_id.upper()}",
        pattern_id,
        _subsections(text[start:end]),
    )


def _rule_path(rule_set: str, number: str) -> str:
    title, segment = next(
        (title, segment)
        for title, segment, form in _RULE_SET_FORMS
        if form.fullmatch(rule_set)
    )
    return f"/us/usc/t{title}/courtRules/{segment}/rule{number}"


def _court_rule(
    text: str,
    start: int,
    end: int,
    rule_set: str,
    number: str,
    pattern_id: str,
) -> ExactObject:
    return ExactObject(
        "court_rule",
        start,
        end,
        text[start:end],
        _rule_path(rule_set, number),
        pattern_id,
        _subsections(text[start:end]),
    )


def _bare_rule(text: str, start: int, end: int, pattern_id: str) -> ExactObject:
    return ExactObject(
        "bare_rule",
        start,
        end,
        text[start:end],
        pattern_id=pattern_id,
        subsections=_subsections(text[start:end]),
    )


def _range_parts(section: str) -> tuple[str, str, int] | None:
    match = _RANGE_CAPTURE.fullmatch(section)
    if match is None:
        return None
    return match.group("left"), match.group("right"), match.start("dash")


def _titled_candidate(
    text: str, match: re.Match[str], pattern_id: str, precedence: int
) -> _Candidate:
    start, end = match.span()
    statute = _statute(text, start, end, match.group("title"), match.group("section"), pattern_id)
    return _Candidate(start, end, precedence, (statute,))


def _list_candidate(
    text: str, match: re.Match[str], pattern_id: str, precedence: int
) -> _Candidate | None:
    title = match.group("title")
    body = match.group("body")
    body_start = match.start("body")
    members = tuple(_LIST_MEMBER_CAPTURE.finditer(body))
    if not members:
        return None
    objects: list[ExactObject] = []
    for index, member in enumerate(members):
        section = member.group("section")
        member_start = body_start + member.start("section")
        member_end = body_start + member.end()
        object_start = match.start() if index == 0 else body_start + member.start()
        range_parts = _range_parts(section)
        if range_parts is None:
            objects.append(_statute(text, object_start, member_end, title, section, pattern_id))
            continue
        left, right, dash_offset = range_parts
        dash_start = member_start + dash_offset
        objects.append(_statute(text, object_start, dash_start, title, left, pattern_id))
        objects.append(_statute(text, dash_start + 1, member_end, title, right, pattern_id))
    return _Candidate(match.start(), match.end(), precedence, tuple(objects))


def _guideline_candidate(
    text: str, match: re.Match[str], pattern_id: str, precedence: int
) -> _Candidate:
    prefix = match.group("guideline_prefix")
    start = match.start("guideline_prefix") if prefix is not None else match.start("guideline_id")
    return _Candidate(
        start,
        match.end(),
        precedence,
        (_guideline(text, start, match.end(), match.group("guideline_id"), pattern_id),),
    )


def _court_rule_candidate(
    text: str, match: re.Match[str], pattern_id: str, precedence: int
) -> _Candidate:
    rule_set = match.group("set_form") or match.group("long_set")
    number = match.group("number") or match.group("long_number")
    return _Candidate(
        match.start(),
        match.end(),
        precedence,
        (_court_rule(text, match.start(), match.end(), rule_set, number, pattern_id),),
    )


def _bare_rule_candidate(
    text: str, match: re.Match[str], pattern_id: str, precedence: int
) -> _Candidate | None:
    start = match.start()
    end = match.end()
    prefix = text[max(0, start - _CUE_WINDOW) : start]
    following = text[end : end + _CUE_WINDOW]
    if _SUPREME_RULE_PREFIX.search(prefix) or _HABEAS_RULE_FOLLOWING.match(following):
        return None
    return _Candidate(start, end, precedence, (_bare_rule(text, start, end, pattern_id),))


_WORD_BEFORE = re.compile(rf"({_WORD_CAPTURE.pattern}){_OPTIONAL_WS}$")
_WORD_AFTER = re.compile(rf"{_OPTIONAL_WS}({_WORD_CAPTURE.pattern})")


def _word_before(text: str, start: int) -> str | None:
    match = _WORD_BEFORE.search(text[max(0, start - _CUE_WINDOW) : start])
    return None if match is None else match.group(1).casefold()


def _word_after(text: str, end: int) -> str | None:
    match = _WORD_AFTER.match(text[end : end + _CUE_WINDOW])
    return None if match is None else match.group(1).casefold()


def _has_bare_cue(text: str, start: int, end: int) -> bool:
    following = _word_after(text, end) in BARE_SECTION_CUES
    number = text[start:end]
    if number.isdigit() and int(number) in _YEAR_RANGE:
        return following
    return following or _word_before(text, start) in BARE_SECTION_CUES


def _declined_bare(text: str, start: int, end: int) -> bool:
    prefix = text[max(0, start - 96) : start]
    section = text[start:end]
    if _RULE_CONTEXT.search(prefix) or _CFR_CONTEXT.search(prefix):
        return True
    if _STATE_CONTEXT.search(prefix) or _APPENDIX_CONTEXT.search(prefix):
        return True
    if _HABEAS_RULES_CONTEXT.search(prefix):
        return True
    return _STATE_CODE_SHAPE.fullmatch(section) is not None


def _bare_candidate(text: str, match: re.Match[str], pattern_id: str) -> _Candidate | None:
    if match.group("marked_section") is not None:
        start = match.start()
        end = match.end()
    elif match.group("sub_section") is not None:
        start = match.start("sub_section")
        end = match.end()
    else:
        start = match.start("cue_number")
        end = match.end("cue_number")
        if not _has_bare_cue(text, start, end):
            return None
    if _declined_bare(text, start, end):
        return None
    marker = match.group("marker")
    section = match.group("marked_section")
    if marker and section and marker.casefold() in {"§§", "sections", "secs."}:
        range_parts = _range_parts(section)
        if range_parts is not None:
            _, _, dash_offset = range_parts
            dash_start = match.start("marked_section") + dash_offset
            return _Candidate(
                start,
                end,
                5,
                (
                    _bare(text, start, dash_start, pattern_id),
                    _bare(text, dash_start + 1, end, pattern_id),
                ),
            )
    return _Candidate(start, end, 5, (_bare(text, start, end, pattern_id),))


def _candidates(text: str) -> Iterator[_Candidate]:
    for precedence, pattern in enumerate(PATTERN_REGISTRY):
        for match in pattern.expression.finditer(text):
            if pattern.id == "usc/titled-section@1":
                yield _titled_candidate(text, match, pattern.id, precedence)
            elif pattern.id == "usc/titled-list-member@1":
                candidate = _list_candidate(text, match, pattern.id, precedence)
                if candidate is not None:
                    yield candidate
            elif pattern.id == "ussg/id@1":
                yield _guideline_candidate(text, match, pattern.id, precedence)
            elif pattern.id == "rules/set-and-number@1":
                yield _court_rule_candidate(text, match, pattern.id, precedence)
            elif pattern.id == "rules/bare-number@1":
                candidate = _bare_rule_candidate(text, match, pattern.id, precedence)
                if candidate is not None:
                    yield candidate
            else:
                candidate = _bare_candidate(text, match, pattern.id)
                if candidate is not None:
                    yield candidate


def _overlaps(left: ExactObject, right: ExactObject) -> bool:
    return left.start < right.end and right.start < left.end


def extract(text: str) -> tuple[ExactObject, ...]:
    """Extract deterministic, ordered, non-overlapping exact objects from text."""

    selected: list[ExactObject] = []
    candidates = sorted(
        _candidates(text),
        key=lambda candidate: (candidate.start, candidate.precedence, -(candidate.end - candidate.start)),
    )
    for candidate in candidates:
        if any(
            _overlaps(existing, incoming)
            for existing in selected
            for incoming in candidate.objects
        ):
            continue
        selected.extend(candidate.objects)
    selected.sort(key=lambda value: value.start)
    return tuple(selected)
