"""Find treatment language on citation edges and classify its court standing."""

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from gideon.worker.citations import NewCitation, SectionSpan

PATTERN_SET_ID: Final = "treatment/patterns@1"
PATTERN_WINDOW: Final = 300
# Keep the display switch off until the pattern set's precision is measured.
SIGNAL_PATTERN: Final = False
TREATMENT_SIGNALS: Final = (
    "overruled", "abrogated", "superseded", "reversed", "vacated", "disapproved",
)
QUALIFIERS: Final = ("in_part", "on_other_grounds", "none")
SIGNAL_SOURCES: Final = ("pattern", "list", "llm")
TREATMENT_STATES: Final = ("negative", "caution")
NO_STATE_REASONS: Final = ("non_holding", "lineage_unverified", "unresolved")
HOLDING_SECTIONS: Final = ("majority", "per_curiam")
LINEAGE_SIGNALS: Final = ("reversed", "vacated")
LEVELS: Final = (
    "scotus", "circuit", "district", "state_supreme", "state_appellate", "other",
)
CIRCUIT_PATTERN: Final = re.compile(r"(?:ca(?:[1-9]|10|11)|cadc|cafc)\Z")
STATE_PATTERN: Final = re.compile(
    r"(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|"
    r"MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|"
    r"SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC|AS|GU|MP|PR|VI)\Z"
)
ABBREVIATIONS: Final = (
    "v.", "Id.", "id.", "e.g.", "i.e.", "cf.", "et al.", "Inc.", "Co.", "Corp.",
    "Ltd.", "No.", "Nos.", "Mr.", "Mrs.", "Ms.", "Dr.", "Jr.", "Sr.",
    "St.", "Ct.", "Cir.", "Dist.", "App.", "Div.", "Supp.", "Fed.",
    "Crim.", "Civ.", "Ann.", "Stat.", "Sec.", "Art.", "Ch.", "Pt.", "Vol.",
)

_PARAGRAPH = re.compile(r"\n[ \t]*\n")
_TERMINATOR = re.compile(r"[.?!][\"'\u2019\u201d)]*\s+(?=[A-Z0-9\"'\u201c(])")
_WORDS = re.compile(r"\b\w+\b")
_VERBS = re.compile(
    r"\b(?P<overruled>overrul(?:e|es|ed|ing))\b|"
    r"\b(?P<abrogated>abrogat(?:e|es|ed|ing))\b|"
    r"\b(?P<superseded>supersed(?:e|es|ed|ing))\b|"
    r"\b(?P<reversed>reversed)\b|"
    r"\b(?P<vacated>vacated)\b|"
    r"\b(?P<disapproved>disapprov(?:e|es|ed|ing))\b",
    re.IGNORECASE,
)
_QUALIFIER = re.compile(r"\b(in\s+part|on\s+other\s+grounds)\b", re.IGNORECASE)
_DIRECTION = re.compile(
    r"\s+(?:(?:in\s+part|on\s+other\s+grounds)\s+(?:by|in)|"
    r"by|in(?!\s+part\b))\b",
    re.IGNORECASE,
)
_ABBREVIATION_REACH: Final = max(len(item) for item in ABBREVIATIONS) + 1
_BASE_FORMS = frozenset(("overrule", "abrogate", "supersede", "disapprove"))
_NEGATORS = frozenset(("not", "never", "nor", "neither", "no"))
_MODALS = frozenset(("would", "should", "could", "might", "may", "must"))

type ClusterCourt = Callable[[int], str | None]
type GeographyOf = Callable[[str], Geography | None]


class TreatmentSection(SectionSpan, Protocol):
    """A section placed for treatment, with a footnote's referring section."""

    @property
    def parent_section_id(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class Geography:
    """A court's level and optional federal or state location."""

    level: str
    circuit: str | None
    state: str | None


@dataclass(frozen=True, slots=True)
class NewSignal:
    """One immutable finding on a citation edge."""

    signal_id: str
    citation_id: str
    doc_id: str
    signal_source: str
    pattern_set: str
    treatment_signal: str
    qualifier: str
    effective_section: str
    state: str | None
    no_state_reason: str | None
    char_start: int
    char_end: int


def parse_geography(value: object) -> Geography:
    """Validate the geography carried in a worker job argument."""

    if not isinstance(value, Mapping) or set(value) != {"level", "circuit", "state"}:
        raise ValueError("court geography must have level, circuit, and state")
    level = value["level"]
    circuit = value["circuit"]
    state = value["state"]
    if not isinstance(level, str) or level not in LEVELS:
        raise ValueError("invalid court level")
    if circuit is not None and (
        not isinstance(circuit, str) or CIRCUIT_PATTERN.fullmatch(circuit) is None
    ):
        raise ValueError("invalid court circuit")
    if state is not None and (
        not isinstance(state, str) or STATE_PATTERN.fullmatch(state) is None
    ):
        raise ValueError("invalid court state")
    return Geography(level, circuit, state)


def _abbreviation(text: str, position: int) -> bool:
    # A bounded tail keeps the check constant-time however long the opinion is.
    tail_start = max(0, position + 1 - _ABBREVIATION_REACH)
    tail = text[tail_start:position + 1]
    for abbreviation in ABBREVIATIONS:
        if tail.endswith(abbreviation):
            before = position - len(abbreviation)
            if before < 0 or not text[before].isalpha():
                return True
    return position > 0 and text[position - 1].isupper() and (
        position == 1 or not text[position - 2].isalpha()
    )


def sentence(
    text: str, start: int, end: int, bounds: SectionSpan,
    spans: Sequence[NewCitation],
) -> tuple[int, int]:
    """Bound the citation's sentence by its section and a fixed text window."""

    left = max(bounds.char_start, start - PATTERN_WINDOW)
    right = min(bounds.char_end, end + PATTERN_WINDOW)
    for match in _PARAGRAPH.finditer(text, left, right):
        if match.end() <= start:
            left = match.end()
        elif match.start() >= end:
            right = min(right, match.start())
            break
    for match in _TERMINATOR.finditer(text, left, right):
        point = match.start()
        if text[point] == "." and (
            any(span.char_start <= point < span.char_end for span in spans)
            or _abbreviation(text, point)
        ):
            continue
        if match.end() <= start:
            left = match.end()
        elif point >= end:
            right = point + 1 + len(match.group()[1:].rstrip(" \t\n\r"))
            break
    return left, right


def _excluded(fragment: str, verb_start: int, form: str) -> bool:
    words = [match.group().casefold() for match in _WORDS.finditer(fragment, 0, verb_start)]
    return (
        "whether" in words
        or any(word in _NEGATORS for word in words[-3:])
        or (form in _BASE_FORMS and (
            (bool(words) and words[-1] == "to")
            or any(word in _MODALS for word in words[-2:])
        ))
    )


def effective_section(
    row: NewCitation, sections: Mapping[str, TreatmentSection],
) -> str:
    """Use the referring opinion section for a footnote when it is recorded."""

    section = sections.get(row.section_id)
    if section is None:
        raise ValueError("citation section is absent")
    if row.section_type != "footnote":
        return row.section_type
    parent_id = section.parent_section_id
    parent = sections.get(parent_id) if parent_id is not None else None
    return parent.section_type if parent is not None else "footnote"


def binds(citing: Geography, cited: Geography) -> bool:
    """Classify the categorical authority of the citing court over the cited court."""

    if citing.level == "scotus":
        return True
    if citing.level == "circuit":
        return (
            citing.circuit is not None
            and cited.level in {"circuit", "district"}
            and cited.circuit == citing.circuit
        )
    if citing.level == "state_supreme":
        return (
            citing.state is not None
            and cited.level in {"state_supreme", "state_appellate"}
            and cited.state == citing.state
        )
    return False


def signals(
    doc_id: str, text: str, sections: Sequence[TreatmentSection],
    citation_rows: Sequence[NewCitation], citing: Geography,
    cluster_court: ClusterCourt, geography_of: GeographyOf,
) -> tuple[NewSignal, ...]:
    """Find the first eligible treatment occurrence for each case citation."""

    by_id = {section.section_id: section for section in sections}
    rows: list[NewSignal] = []
    for row in sorted(citation_rows, key=lambda item: item.ordinal):
        if row.cite_type != "case_cite":
            continue
        section = by_id.get(row.section_id)
        if section is None:
            raise ValueError("citation section is absent")
        first, last = sentence(text, row.char_start, row.char_end, section, citation_rows)
        fragment = text[first:last]
        for match in _VERBS.finditer(fragment):
            if _excluded(fragment, match.start(), match.group().casefold()):
                continue
            start = first + match.start()
            if _DIRECTION.match(fragment, match.end()) and row.char_end > start:
                continue
            signal = match.lastgroup
            if signal is None:
                continue
            qualifier_match = _QUALIFIER.search(fragment, match.end())
            qualifier = "none"
            if qualifier_match is not None:
                qualifier = "_".join(qualifier_match.group().casefold().split())
            standing = effective_section(row, by_id)
            state: str | None = None
            reason: str | None = None
            if standing not in HOLDING_SECTIONS:
                reason = "non_holding"
            elif signal in LINEAGE_SIGNALS:
                reason = "lineage_unverified"
            else:
                court_id = cluster_court(row.to_cluster) if row.to_cluster is not None else None
                cited = geography_of(court_id) if court_id is not None else None
                if cited is None:
                    reason = "unresolved"
                else:
                    state = "negative" if binds(citing, cited) else "caution"
            signal_id = hashlib.sha256(
                f"{row.citation_id}\npattern\n{PATTERN_SET_ID}".encode()
            ).hexdigest()
            rows.append(NewSignal(
                signal_id, row.citation_id, doc_id, "pattern", PATTERN_SET_ID,
                signal, qualifier, standing, state, reason, start,
                first + match.end(),
            ))
            break
    return tuple(rows)
