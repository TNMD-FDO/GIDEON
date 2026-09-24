"""The GIDEON regex grammar: the exact-object families.

Every pattern is a bounded regular expression with a versioned id in the
``family/pattern@N`` convention and declares the one type it emits; the
registry's order is the families' precedence, and ``registry_types`` is what
the extraction gate is handed, so a family that emits nothing stays gated.
``extract`` finds every candidate, gives a position to the earliest candidate
(at one start, the family first in precedence, then the longest), and returns
the objects ordered and disjoint under the contract in ``contract.py``.

A section is a ``statute`` only when its construction states a title, else a
``bare_section`` with no key.  A C.F.R. section is a ``regulation`` with its
dotted section in the key; a part cite is no object.  An appendix compilation
gets its path from the fixed title/ordinal table; one outside it is no object.
A habeas rule names the 2254 or 2255 set, while ``Habeas Rule N`` is an
unnamed ``bare_rule``.  A Supreme Court rule's dotted paragraph is its first
``subsections`` designator.  A docket's object is its number alone, its
marker outside: a district number with or without a marker, a two-part number
only under one, never inside a public-law cite or in a three-part shape.
A hyphen after a letter joins one section (``2000e-5``); a hyphen or en dash
between two all-digit numbers is a range only under a plural marker, its two
endpoints the objects.  A markerless bare section needs a subsection or a cue
word beside it, and a number in the year range needs a subsection or a
following cue; a dotted bare section needs its marker.  A bare section
declines inside a rule, C.F.R., state-code, appendix, or habeas-rules
construction.  A pattern whose behaviour changes takes ``@N+1``, and
``GRAMMAR_VERSION`` moves with any pattern.
"""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Final

from gideon.extraction.contract import ExactObject, ObjectType

GRAMMAR_VERSION: Final[int] = 2

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
_CFR_CODE_TOKEN = rf"C{_CODE_GAP}F{_CODE_GAP}R{_CODE_GAP}"
_GUIDELINE_ID = r"[0-9][A-Za-z][0-9]{1,3}\.[0-9]{1,3}"
_GUIDELINE_TOKEN = rf"U{_CODE_GAP}S{_CODE_GAP}S{_CODE_GAP}G{_CODE_GAP}"
_GUIDELINE_MARKER = r"(?:§{1,2}|section|sec[.])"
_GUIDELINE_PREFIX = (
    rf"(?:(?:{_GUIDELINE_TOKEN}|Guideline{_WS}){_OPTIONAL_WS}(?:{_GUIDELINE_MARKER}{_OPTIONAL_WS}){{0,1}}"
    rf"|{_GUIDELINE_MARKER}{_OPTIONAL_WS})"
)
_SECTION_ATOM = r"[0-9]{1,6}[A-Za-z]{0,2}"
_SECTION_TOKEN = rf"{_SECTION_ATOM}(?:[-–]{_SECTION_ATOM}){{0,1}}"
_CFR_SECTION_TOKEN = r"[0-9]{1,6}[.][0-9]{1,6}(?:[A-Za-z]{1,2}(?:[-–][0-9]{1,6})?)?"
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
_SCOTUS_SHORT = rf"Sup{_RULE_GAP}Ct{_RULE_GAP}(?:R|Rule){_RULE_GAP}"
_SCOTUS_LONG = rf"Supreme{_WS}Court{_WS}Rule{_WS}"
_RULE_SETS: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("18a", "Crim", _CRIMINAL_SHORT, _CRIMINAL_LONG),
    ("28a", "Civil", _CIVIL_SHORT, _CIVIL_LONG),
    ("28a", "App", _APPELLATE_SHORT, _APPELLATE_LONG),
    ("28a", "Evid", _EVIDENCE_SHORT, _EVIDENCE_LONG),
)
"""The four pilot rule sets: the USLM appendix title, the set's path segment, its forms."""
_APPENDIX_TABLE: Final[dict[tuple[int, int], str]] = {
    (18, 2): "pl/91/538",
    (18, 3): "pl/96/456",
}
"""The title and ordinal table for appendix compilation USLM paths."""
_ROMAN_ORDINALS: Final[dict[str, int]] = {
    "I": 1,
    "II": 2,
    "III": 3,
    "IV": 4,
    "V": 5,
    "VI": 6,
    "VII": 7,
    "VIII": 8,
    "IX": 9,
    "X": 10,
}
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
_CFR_RANGE_TOKEN = (
    r"[0-9]{1,6}[.][0-9]{1,6}[-–][0-9]{1,6}[.][0-9]{1,6}"
)
_CFR_LIST_SECTION_TOKEN = rf"(?:{_CFR_RANGE_TOKEN}|{_CFR_SECTION_TOKEN})"
_CFR_TITLED_SECTION_SOURCE = (
    rf"{_LEFT_BOUNDARY}(?P<title>[0-9]{{1,3}}){_WS}{_CFR_CODE_TOKEN}{_WS}"
    rf"(?:(?P<marker>(?:§|section|sec[.])){_OPTIONAL_WS}){{0,1}}"
    rf"(?P<section>{_CFR_SECTION_TOKEN})(?P<subsections>{_SUBSECTIONS}){_RIGHT_BOUNDARY}"
)
_CFR_LIST_SOURCE = (
    rf"{_LEFT_BOUNDARY}(?P<title>[0-9]{{1,3}}){_WS}{_CFR_CODE_TOKEN}{_WS}"
    rf"(?P<list_marker>{_PLURAL_MARKER}){_OPTIONAL_WS}"
    rf"(?P<body>{_CFR_LIST_SECTION_TOKEN}{_SUBSECTIONS}"
    rf"(?:{_OPTIONAL_WS}{_LIST_SEPARATOR}{_OPTIONAL_WS}(?:{_SECTION_MARKER}{_OPTIONAL_WS})?"
    rf"{_CFR_LIST_SECTION_TOKEN}{_SUBSECTIONS}){{0,3}}){_RIGHT_BOUNDARY}"
)
_BARE_MARKED_SECTION_TOKEN = rf"(?:{_CFR_SECTION_TOKEN}|{_SECTION_TOKEN})"
_BARE_SOURCE = (
    rf"(?:"
    rf"{_LEFT_BOUNDARY}(?P<marker>{_SECTION_MARKER}){_OPTIONAL_WS}"
    rf"(?P<marked_section>{_BARE_MARKED_SECTION_TOKEN})(?P<marked_subsections>{_SUBSECTIONS})"
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
_SCOTUS_RULE_SOURCE = (
    rf"{_LEFT_BOUNDARY}(?P<short_prefix>{_SCOTUS_SHORT})"
    rf"(?P<short_number>{_RULE_NUMBER})(?P<short_subsections>{_SUBSECTIONS})"
    rf"{_RIGHT_BOUNDARY}"
    rf"|{_LEFT_BOUNDARY}(?P<long_prefix>{_SCOTUS_LONG})"
    rf"(?P<long_number>{_RULE_NUMBER})(?P<long_subsections>{_SUBSECTIONS})"
    rf"{_RIGHT_BOUNDARY}"
    rf"|{_LEFT_BOUNDARY}Rule{_WS}(?P<of_number>{_RULE_NUMBER})"
    rf"(?P<of_subsections>{_SUBSECTIONS}){_WS}of{_WS}the{_WS}Rules{_WS}of{_WS}the{_WS}"
    rf"Supreme{_WS}Court{_RIGHT_BOUNDARY}"
)
_HABEAS_RULE_SOURCE = (
    rf"{_LEFT_BOUNDARY}Rule{_WS}(?P<long_number>{_RULE_NUMBER})"
    rf"(?P<long_subsections>{_SUBSECTIONS}){_WS}of{_WS}the{_WS}Rules{_WS}Governing{_WS}"
    rf"(?:Section|§){_OPTIONAL_WS}(?P<long_title>225[45]){_WS}"
    rf"(?:Cases|Proceedings){_RIGHT_BOUNDARY}"
    rf"|{_LEFT_BOUNDARY}(?P<short_marker>§|Section){_OPTIONAL_WS}"
    rf"(?P<short_title>225[45]){_WS}Rule{_WS}(?P<short_number>{_RULE_NUMBER})"
    rf"(?P<short_subsections>{_SUBSECTIONS}){_RIGHT_BOUNDARY}"
)
_HABEAS_UNNAMED_SOURCE = (
    rf"{_LEFT_BOUNDARY}Habeas{_WS}Rule{_WS}(?P<number>{_RULE_NUMBER})"
    rf"(?P<subsections>{_SUBSECTIONS}){_RIGHT_BOUNDARY}"
)
_APPENDIX_SOURCE = (
    rf"{_LEFT_BOUNDARY}(?P<title>[0-9]{{1,3}}){_WS}{_CODE_TOKEN}{_WS}"
    rf"App[.]?{_WS}(?P<ordinal>[0-9]{{1,3}}|[IVXLCDM]{{1,8}})"
    rf"{_OPTIONAL_WS}(?:,{_OPTIONAL_WS})?"
    rf"(?P<marker>§|section|sec[.]){_OPTIONAL_WS}"
    rf"(?P<section>{_SECTION_TOKEN})(?P<subsections>{_SUBSECTIONS}){_RIGHT_BOUNDARY}"
)
_DISTRICT_DOCKET_NUMBER = (
    r"[0-9]:[0-9]{2}-[A-Za-z]{2}-[0-9]{3,5}"
    r"(?:-[A-Za-z]{2,4}){0,3}(?:-[0-9]{1,3})?"
)
_DOCKET_MARKER = rf"(?:No[.]|Case{_WS}No[.]|Docket{_WS}No[.]|Dkt[.])"
_MARKED_DOCKET_NUMBER = r"[0-9]{2}-[0-9]{1,5}"
_DOCKET_DISTRICT_SOURCE = (
    rf"{_LEFT_BOUNDARY}(?P<number>{_DISTRICT_DOCKET_NUMBER}){_RIGHT_BOUNDARY}"
)
_DOCKET_MARKED_SOURCE = (
    rf"{_LEFT_BOUNDARY}{_DOCKET_MARKER}{_WS}"
    rf"(?P<number>{_MARKED_DOCKET_NUMBER}){_RIGHT_BOUNDARY}"
)
_BARE_RULE_SOURCE = (
    rf"{_LEFT_BOUNDARY}Rule{_WS}(?P<bare_number>{_RULE_NUMBER})"
    rf"(?P<bare_subsections>{_SUBSECTIONS}){_RIGHT_BOUNDARY}"
)


_SUBSECTION_CAPTURE = re.compile(r"\(([A-Za-z0-9]{1,4})\)")
_LIST_MEMBER_CAPTURE = re.compile(
    rf"(?:{_SECTION_MARKER}{_OPTIONAL_WS}){{0,1}}"
    rf"(?P<section>{_SECTION_TOKEN})(?P<subsections>{_SUBSECTIONS})",
    re.IGNORECASE,
)
_CFR_LIST_MEMBER_CAPTURE = re.compile(
    rf"(?:{_SECTION_MARKER}{_OPTIONAL_WS}){{0,1}}"
    rf"(?P<section>{_CFR_LIST_SECTION_TOKEN})(?P<subsections>{_SUBSECTIONS})",
    re.IGNORECASE,
)
_RANGE_CAPTURE = re.compile(r"(?P<left>[0-9]{1,6})(?P<dash>[-–])(?P<right>[0-9]{1,6})")
_CFR_RANGE_CAPTURE = re.compile(
    r"(?P<left>[0-9]{1,6}[.][0-9]{1,6})"
    r"(?P<dash>[-–])"
    r"(?P<right>[0-9]{1,6}[.][0-9]{1,6})"
)
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
    rf"\bapp[.]?(?:{_WS})(?:[0-9]{{1,3}}|[IVXLCDM]{{1,8}})"
    rf"{_OPTIONAL_WS}(?:,{_OPTIONAL_WS})?$",
    re.IGNORECASE,
)
_PUBLIC_LAW_CONTEXT = re.compile(
    rf"\bPub[.]{_OPTIONAL_WS}L[.]{_OPTIONAL_WS}$", re.IGNORECASE
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


def _regulation(
    text: str,
    start: int,
    end: int,
    title: str,
    section: str,
    pattern_id: str,
) -> ExactObject:
    return ExactObject(
        "regulation",
        start,
        end,
        text[start:end],
        f"cfr/{title}/{section}",
        pattern_id,
        _subsections(text[start:end]),
    )


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


def _habeas_rule(
    text: str,
    start: int,
    end: int,
    title: str,
    number: str,
    pattern_id: str,
) -> ExactObject:
    return ExactObject(
        "habeas_rule",
        start,
        end,
        text[start:end],
        f"rules/{title}/rule{number}",
        pattern_id,
        _subsections(text[start:end]),
    )


def _appendix_ordinal(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    return _ROMAN_ORDINALS.get(value.upper())


def _appendix_candidate(
    text: str, match: re.Match[str], pattern_id: str, precedence: int
) -> _Candidate | None:
    ordinal = _appendix_ordinal(match.group("ordinal"))
    path = _APPENDIX_TABLE.get((int(match.group("title")), ordinal)) if ordinal else None
    if path is None:
        return None
    start, end = match.span()
    appendix = ExactObject(
        "appendix_statute",
        start,
        end,
        text[start:end],
        f"/us/usc/t{match.group('title')}a/{path}/s{match.group('section')}",
        pattern_id,
        _subsections(text[start:end]),
    )
    return _Candidate(start, end, precedence, (appendix,))


def _docket(text: str, start: int, end: int, pattern_id: str) -> ExactObject:
    return ExactObject("docket", start, end, text[start:end], pattern_id=pattern_id)


def _district_docket_candidate(
    text: str, match: re.Match[str], pattern_id: str, precedence: int
) -> _Candidate:
    start, end = match.span("number")
    return _Candidate(start, end, precedence, (_docket(text, start, end, pattern_id),))


def _marked_docket_candidate(
    text: str, match: re.Match[str], pattern_id: str, precedence: int
) -> _Candidate | None:
    prefix = text[max(0, match.start() - _CUE_WINDOW) : match.start()]
    if _PUBLIC_LAW_CONTEXT.search(prefix):
        return None
    start, end = match.span("number")
    return _Candidate(start, end, precedence, (_docket(text, start, end, pattern_id),))


def _range_parts(
    section: str, capture: re.Pattern[str] = _RANGE_CAPTURE
) -> tuple[str, str, int] | None:
    match = capture.fullmatch(section)
    if match is None:
        return None
    return match.group("left"), match.group("right"), match.start("dash")


def _titled_candidate(
    text: str, match: re.Match[str], pattern_id: str, precedence: int
) -> _Candidate:
    start, end = match.span()
    statute = _statute(text, start, end, match.group("title"), match.group("section"), pattern_id)
    return _Candidate(start, end, precedence, (statute,))


def _regulation_candidate(
    text: str, match: re.Match[str], pattern_id: str, precedence: int
) -> _Candidate:
    start, end = match.span()
    regulation = _regulation(
        text, start, end, match.group("title"), match.group("section"), pattern_id
    )
    return _Candidate(start, end, precedence, (regulation,))


def _list_candidate_common(
    text: str,
    match: re.Match[str],
    pattern_id: str,
    precedence: int,
    member_capture: re.Pattern[str],
    range_capture: re.Pattern[str],
    object_builder: Callable[[str, int, int, str, str, str], ExactObject],
) -> _Candidate | None:
    title = match.group("title")
    body = match.group("body")
    body_start = match.start("body")
    members = tuple(member_capture.finditer(body))
    if not members:
        return None
    objects: list[ExactObject] = []
    for index, member in enumerate(members):
        section = member.group("section")
        member_start = body_start + member.start("section")
        member_end = body_start + member.end()
        object_start = match.start() if index == 0 else body_start + member.start()
        range_parts = _range_parts(section, range_capture)
        if range_parts is None:
            objects.append(
                object_builder(text, object_start, member_end, title, section, pattern_id)
            )
            continue
        left, right, dash_offset = range_parts
        dash_start = member_start + dash_offset
        objects.append(
            object_builder(text, object_start, dash_start, title, left, pattern_id)
        )
        objects.append(
            object_builder(text, dash_start + 1, member_end, title, right, pattern_id)
        )
    return _Candidate(match.start(), match.end(), precedence, tuple(objects))


def _list_candidate(
    text: str, match: re.Match[str], pattern_id: str, precedence: int
) -> _Candidate | None:
    return _list_candidate_common(
        text,
        match,
        pattern_id,
        precedence,
        _LIST_MEMBER_CAPTURE,
        _RANGE_CAPTURE,
        _statute,
    )


def _cfr_list_candidate(
    text: str, match: re.Match[str], pattern_id: str, precedence: int
) -> _Candidate | None:
    return _list_candidate_common(
        text,
        match,
        pattern_id,
        precedence,
        _CFR_LIST_MEMBER_CAPTURE,
        _CFR_RANGE_CAPTURE,
        _regulation,
    )


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


def _scotus_rule(
    text: str,
    start: int,
    end: int,
    number: str,
    parenthesized: str,
    pattern_id: str,
) -> ExactObject:
    rule_number, separator, paragraph = number.partition(".")
    subsections = ((paragraph,) if separator else ()) + _subsections(parenthesized)
    return ExactObject(
        "scotus_rule",
        start,
        end,
        text[start:end],
        f"rules/scotus/rule{rule_number}",
        pattern_id,
        subsections,
    )


def _scotus_rule_candidate(
    text: str, match: re.Match[str], pattern_id: str, precedence: int
) -> _Candidate:
    number = (
        match.group("short_number")
        or match.group("long_number")
        or match.group("of_number")
    )
    parenthesized = (
        match.group("short_subsections")
        or match.group("long_subsections")
        or match.group("of_subsections")
        or ""
    )
    return _Candidate(
        match.start(),
        match.end(),
        precedence,
        (
            _scotus_rule(
                text,
                match.start(),
                match.end(),
                number,
                parenthesized,
                pattern_id,
            ),
        ),
    )


def _habeas_rule_candidate(
    text: str, match: re.Match[str], pattern_id: str, precedence: int
) -> _Candidate:
    title = match.group("long_title") or match.group("short_title")
    number = match.group("long_number") or match.group("short_number")
    return _Candidate(
        match.start(),
        match.end(),
        precedence,
        (
            _habeas_rule(text, match.start(), match.end(), title, number, pattern_id),
        ),
    )


def _habeas_unnamed_candidate(
    text: str, match: re.Match[str], pattern_id: str, precedence: int
) -> _Candidate:
    return _Candidate(
        match.start(),
        match.end(),
        precedence,
        (_bare_rule(text, match.start(), match.end(), pattern_id),),
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


def _bare_candidate(
    text: str, match: re.Match[str], pattern_id: str, precedence: int
) -> _Candidate | None:
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
                precedence,
                (
                    _bare(text, start, dash_start, pattern_id),
                    _bare(text, dash_start + 1, end, pattern_id),
                ),
            )
    return _Candidate(start, end, precedence, (_bare(text, start, end, pattern_id),))


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
    Pattern(
        "cfr/titled-section@1",
        re.compile(_CFR_TITLED_SECTION_SOURCE, re.IGNORECASE),
        "regulation",
    ),
    Pattern(
        "cfr/titled-list-member@1",
        re.compile(_CFR_LIST_SOURCE, re.IGNORECASE),
        "regulation",
    ),
    Pattern(
        "usc/appendix-section@1",
        re.compile(_APPENDIX_SOURCE, re.IGNORECASE),
        "appendix_statute",
    ),
    Pattern("ussg/id@1", re.compile(_GUIDELINE_SOURCE, re.IGNORECASE), "guideline"),
    Pattern(
        "rules/set-and-number@1",
        re.compile(_COURT_RULE_SOURCE, re.IGNORECASE),
        "court_rule",
    ),
    Pattern(
        "rules/scotus-number@1",
        re.compile(_SCOTUS_RULE_SOURCE, re.IGNORECASE),
        "scotus_rule",
    ),
    Pattern(
        "rules/habeas-set-and-number@1",
        re.compile(_HABEAS_RULE_SOURCE, re.IGNORECASE),
        "habeas_rule",
    ),
    Pattern(
        "rules/habeas-unnamed-set@1",
        re.compile(_HABEAS_UNNAMED_SOURCE, re.IGNORECASE),
        "bare_rule",
    ),
    Pattern("rules/bare-number@1", re.compile(_BARE_RULE_SOURCE, re.IGNORECASE), "bare_rule"),
    Pattern(
        "docket/district@1",
        re.compile(_DOCKET_DISTRICT_SOURCE, re.IGNORECASE),
        "docket",
    ),
    Pattern(
        "docket/marked-number@1",
        re.compile(_DOCKET_MARKED_SOURCE, re.IGNORECASE),
        "docket",
    ),
    Pattern("usc/bare-section@2", re.compile(_BARE_SOURCE, re.IGNORECASE), "bare_section"),
)

PATTERN_BUILDERS: Final[
    dict[str, Callable[[str, re.Match[str], str, int], _Candidate | None]]
] = {
    "usc/titled-section@1": _titled_candidate,
    "usc/titled-list-member@1": _list_candidate,
    "cfr/titled-section@1": _regulation_candidate,
    "cfr/titled-list-member@1": _cfr_list_candidate,
    "usc/appendix-section@1": _appendix_candidate,
    "ussg/id@1": _guideline_candidate,
    "rules/set-and-number@1": _court_rule_candidate,
    "rules/scotus-number@1": _scotus_rule_candidate,
    "rules/habeas-set-and-number@1": _habeas_rule_candidate,
    "rules/habeas-unnamed-set@1": _habeas_unnamed_candidate,
    "rules/bare-number@1": _bare_rule_candidate,
    "docket/district@1": _district_docket_candidate,
    "docket/marked-number@1": _marked_docket_candidate,
    "usc/bare-section@2": _bare_candidate,
}


def _candidates(text: str) -> Iterator[_Candidate]:
    for precedence, pattern in enumerate(PATTERN_REGISTRY):
        builder = PATTERN_BUILDERS[pattern.id]
        for match in pattern.expression.finditer(text):
            candidate = builder(text, match, pattern.id, precedence)
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
