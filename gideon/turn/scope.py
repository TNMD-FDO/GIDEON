"""Read the release's court file and resolve jurisdiction named in a turn."""

import json
import re
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

COURT_FILE_FORMAT: Final = 1
# The court-map vocabulary and the API's court-id shape, across the import boundary.
_LEVELS: Final = ("scotus", "circuit", "district", "state_supreme", "state_appellate", "other")
_COURT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,31}")
_STATE = re.compile(r"[A-Z]{2}")
_LOCKFILE_LABEL = re.compile(r"corpus-[0-9]{4}-[0-9]{2}-[0-9]{2}")

_ORDINALS: Final = (
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
    "ninth", "tenth", "eleventh",
)
# Geographic abbreviations used in court citations, keyed by the full name.
_STATE_ABBREVIATIONS: Final = {
    "alabama": "Ala.",
    "alaska": "Alaska",
    "american samoa": "Am. Sam.",
    "arizona": "Ariz.",
    "arkansas": "Ark.",
    "california": "Cal.",
    "colorado": "Colo.",
    "connecticut": "Conn.",
    "delaware": "Del.",
    "district of columbia": "D.C.",
    "florida": "Fla.",
    "georgia": "Ga.",
    "guam": "Guam",
    "hawaii": "Haw.",
    "idaho": "Idaho",
    "illinois": "Ill.",
    "indiana": "Ind.",
    "iowa": "Iowa",
    "kansas": "Kan.",
    "kentucky": "Ky.",
    "louisiana": "La.",
    "maine": "Me.",
    "maryland": "Md.",
    "massachusetts": "Mass.",
    "michigan": "Mich.",
    "minnesota": "Minn.",
    "mississippi": "Miss.",
    "missouri": "Mo.",
    "montana": "Mont.",
    "nebraska": "Neb.",
    "nevada": "Nev.",
    "new hampshire": "N.H.",
    "new jersey": "N.J.",
    "new mexico": "N.M.",
    "new york": "N.Y.",
    "north carolina": "N.C.",
    "north dakota": "N.D.",
    "northern mariana islands": "N. Mar. I.",
    "ohio": "Ohio",
    "oklahoma": "Okla.",
    "oregon": "Or.",
    "pennsylvania": "Pa.",
    "puerto rico": "P.R.",
    "rhode island": "R.I.",
    "south carolina": "S.C.",
    "south dakota": "S.D.",
    "tennessee": "Tenn.",
    "texas": "Tex.",
    "utah": "Utah",
    "vermont": "Vt.",
    "virgin islands": "V.I.",
    "virginia": "Va.",
    "washington": "Wash.",
    "west virginia": "W. Va.",
    "wisconsin": "Wis.",
    "wyoming": "Wyo.",
}
_DISTRICT_NAMES: Final = {
    "c.d.": "central district", "d.": "district", "e.d.": "eastern district",
    "m.d.": "middle district", "n.d.": "northern district", "s.d.": "southern district",
    "w.d.": "western district",
}
_INITIALS = re.compile(r"(?:[A-Z]\.)+")
# The abbreviated parts that are also common English words, for Oregon and Maine.
_WORD_PARTS: Final = frozenset(("or", "me"))
_ABBREVIATION = r"(?:(?:[a-z]\. ?)+[a-z]\.?(?!\w)|[a-z]+\.)"
_ABBREVIATIONS = re.compile(rf"(?<!\w){_ABBREVIATION}(?: ?{_ABBREVIATION})*")
_FORM_STARTS = re.compile(r"(?<!\w)(?:\w{1,3}|[^\w\s])")
_PARENTHETICAL_TOKENS = re.compile(r"[()]|\b[12][0-9]{3}\b")
_NOTE = r"nn?\.\s*[0-9]+(?:\s*[-–—]\s*[0-9]+)?"
_PIN_PAGE = rf"(?:(?:\*|¶{{1,2}}\s*)?[0-9]+(?:\s*[-–—]\s*\*?[0-9]+)?(?:\s*&?\s*{_NOTE})?|{_NOTE})"
_ATTACHED_PARENTHESIS = re.compile(
    rf"\s*(?:(?:,\s*)?(?:slip\s+op\.\s*)?(?:at\s+)?{_PIN_PAGE}(?:\s*(?:,|&|and)\s*{_PIN_PAGE})*\s*)?\(",
    re.IGNORECASE,
)


class CourtFileError(ValueError):
    """The release court file is unreadable or malformed."""


class LockfileOutsideRelease(ValueError):
    """A scoped turn's pinned generation uses a lockfile absent from the court file."""


class _ExactSpan(Protocol):
    @property
    def type(self) -> str: ...

    @property
    def start(self) -> int: ...

    @property
    def end(self) -> int: ...


@dataclass(frozen=True, slots=True)
class NamedCourt:
    """A court id and the half-open code-point offsets of its first match."""

    court: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class StatedScope:
    """The named courts, their sorted filter, and the sorted named ids outside the corpus."""

    named: tuple[NamedCourt, ...] = field(repr=False)
    courts: tuple[str, ...]
    out_of_corpus: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Court:
    level: str
    circuit: str | None
    state: str | None
    name: str = field(repr=False)


type _Form = tuple[re.Pattern[str], str | None]


@dataclass(frozen=True, slots=True)
class CourtFile:
    """The court names, geography, and lockfile membership for one release."""

    _courts: dict[str, _Court] = field(repr=False)
    _lockfiles: dict[str, tuple[str, ...]] = field(repr=False)
    _forms: dict[str, tuple[_Form, ...]] = field(repr=False)

    def match(self, message: str, objects: Sequence[_ExactSpan]) -> tuple[NamedCourt, ...]:
        """Read named courts in message order using the exact objects in extraction order."""

        source, offsets = _normalize_message(message)
        spans = [(obj.start, obj.end) for obj in objects]
        parentheticals = _citation_parentheticals(message, objects)
        candidates: list[tuple[int, int, str | None]] = []
        for start, end, court in _matching_forms(source, self._forms):
            original_start, original_end = offsets[start], offsets[end - 1] + 1
            parenthesis = bisect_right(parentheticals, original_start, key=lambda span: span[0])
            if (
                not _overlaps(spans, original_start, original_end)
                and (parenthesis == 0 or parentheticals[parenthesis - 1][1] < original_end)
            ):
                candidates.append((start, end, court))

        occupied = bytearray(len(source))
        selected: list[NamedCourt] = []
        for start, end, court in sorted(candidates, key=lambda item: (item[0] - item[1], item[0])):
            if any(occupied[start:end]):
                continue
            occupied[start:end] = b"\1" * (end - start)
            if court is not None:
                selected.append(NamedCourt(court, offsets[start], offsets[end - 1] + 1))

        first: dict[str, NamedCourt] = {}
        for named in sorted(selected, key=lambda item: item.start):
            first.setdefault(named.court, named)
        return tuple(first.values())

    def resolve(self, named: tuple[NamedCourt, ...], lockfiles: Iterable[str]) -> StatedScope | None:
        """Widen named courts and check their ids against the pinned lockfiles' union."""

        if not named:
            return None
        installed: set[str] = set()
        for label in lockfiles:
            if label not in self._lockfiles:
                raise LockfileOutsideRelease("pinned lockfile is absent from the court file")
            installed.update(self._lockfiles[label])

        named_ids = {match.court for match in named}
        circuits = {court_id for court_id in named_ids if self._courts[court_id].level == "circuit"}
        states = {
            court.state for court_id in named_ids
            if (court := self._courts[court_id]).level == "state_supreme" and court.state is not None
        }
        courts = named_ids | {
            court_id for court_id, court in self._courts.items()
            if court.level == "scotus"
            or (court.level == "district" and court.circuit in circuits)
            or (court.level in ("state_supreme", "state_appellate") and court.state in states)
        }
        return StatedScope(named, tuple(sorted(courts)), tuple(sorted(named_ids - installed)))


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise CourtFileError("duplicate JSON key")
    return result


def _invalid_constant(_: str) -> None:
    raise CourtFileError("invalid JSON constant")


def _read_court(value: object) -> _Court:
    if not isinstance(value, dict) or set(value) != {"level", "circuit", "state", "name"}:
        raise CourtFileError("invalid court keys")
    level, circuit, state, name = (value[key] for key in ("level", "circuit", "state", "name"))
    if not isinstance(level, str) or level not in _LEVELS:
        raise CourtFileError("invalid court level")
    if circuit is not None and (not isinstance(circuit, str) or not _COURT_ID.fullmatch(circuit)):
        raise CourtFileError("invalid court circuit id")
    if state is not None and (not isinstance(state, str) or not _STATE.fullmatch(state)):
        raise CourtFileError("invalid court state code")
    if not isinstance(name, str) or not name.strip():
        raise CourtFileError("invalid court name")
    return _Court(level, circuit, state, name)


def read_court_file(path: Path) -> CourtFile:
    """Read the exact court-file schema, with content-free diagnostics."""

    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        raise CourtFileError("court file is unreadable") from None
    except UnicodeDecodeError:
        raise CourtFileError("court file is not UTF-8") from None
    try:
        document = json.loads(source, object_pairs_hook=_unique_keys, parse_constant=_invalid_constant)
    except CourtFileError:
        raise
    except (ValueError, RecursionError):
        raise CourtFileError("invalid court file JSON") from None

    if not isinstance(document, dict) or set(document) != {"format", "courts", "lockfiles"}:
        raise CourtFileError("invalid court file keys")
    if type(document["format"]) is not int or document["format"] != COURT_FILE_FORMAT:
        raise CourtFileError("invalid court file format")
    if not isinstance(document["courts"], dict):
        raise CourtFileError("invalid courts mapping")
    if not isinstance(document["lockfiles"], dict):
        raise CourtFileError("invalid lockfiles mapping")

    courts: dict[str, _Court] = {}
    for court_id, value in document["courts"].items():
        if not _COURT_ID.fullmatch(court_id):
            raise CourtFileError("invalid court id")
        courts[court_id] = _read_court(value)

    lockfiles: dict[str, tuple[str, ...]] = {}
    for label, ids in document["lockfiles"].items():
        if not _LOCKFILE_LABEL.fullmatch(label):
            raise CourtFileError("invalid lockfile label")
        if not isinstance(ids, list):
            raise CourtFileError("invalid lockfile court list")
        if any(not isinstance(court_id, str) or not _COURT_ID.fullmatch(court_id) for court_id in ids):
            raise CourtFileError("invalid lockfile court id")
        if ids != sorted(ids):
            raise CourtFileError("unsorted lockfile court list")
        lockfiles[label] = tuple(ids)
    return CourtFile(courts, lockfiles, _build_forms(courts))


def _circuit_forms(name: str) -> Iterable[str]:
    short = name.removeprefix("court of appeals for the ")
    yield short
    for number, word in enumerate(_ORDINALS, 1):
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number, "th")
        numeral = f"{number}{suffix}"
        if short in (f"{word} circuit", f"{numeral} circuit"):
            yield from (f"{ordinal} {kind}" for ordinal in (word, numeral) for kind in ("circuit", "cir."))
            return
    if short in ("d.c. circuit", "district of columbia circuit"):
        yield from ("district of columbia circuit", "district of columbia cir.", "d.c. circuit", "d.c. cir.")
    elif short == "federal circuit":
        yield from ("federal cir.", "fed. circuit", "fed. cir.")


def _district_forms(name: str) -> Iterable[str]:
    short = name.removeprefix("district court, ")
    if short == name:
        return
    if short in _STATE_ABBREVIATIONS:
        prefix, state = "d.", short
        yield f"district court for the {state}"
    else:
        prefix, _, state = short.partition(" ")
        if prefix not in _DISTRICT_NAMES or state not in _STATE_ABBREVIATIONS:
            return
        yield short
        yield f"{_DISTRICT_NAMES[prefix]} of {state}"

    abbreviation = _STATE_ABBREVIATIONS[state]
    space = "" if _INITIALS.fullmatch(abbreviation) else " "
    yield f"{prefix}{space}{abbreviation.casefold()}"


def _state_court_forms(name: str) -> Iterable[str]:
    court, _, state = name.rpartition(" of ")
    if court and state in _STATE_ABBREVIATIONS:
        yield f"{state} {court}"
        return
    for state in _STATE_ABBREVIATIONS:
        prefix = f"{state} "
        if name.startswith(prefix):
            yield f"{name[len(prefix):]} of {state}"
            return


_SCOPING_BUILDERS: Final = {
    "circuit": _circuit_forms,
    "district": _district_forms,
    "state_supreme": _state_court_forms,
    "state_appellate": _state_court_forms,
}


def _court_forms(court: _Court) -> Iterable[str]:
    name = " ".join(court.name.casefold().split())
    builder = _SCOPING_BUILDERS.get(court.level)
    if builder is None:
        yield name
    else:
        yield from (form for form in dict.fromkeys((name, *builder(name))) if form not in _STATE_ABBREVIATIONS)


def _form_pattern(form: str) -> re.Pattern[str]:
    """Keep ordinary separators literal and make abbreviation punctuation optional."""

    pattern: list[str] = []
    end = 0
    for abbreviation in _ABBREVIATIONS.finditer(form):
        literal = form[end:abbreviation.start()]
        parts = re.findall(r"[a-z]+", abbreviation[0])
        pattern.append(re.escape(literal))
        for part, following in zip(parts, (*parts[1:], None), strict=True):
            if following is None:
                pattern.append(rf"{part}\.?")
            elif following in _WORD_PARTS:
                # After a bare space these parts read as words: "Class D or E" names no Oregon court.
                pattern.append(rf"{part}(?:\. ?)?")
            else:
                pattern.append(rf"{part}\.? ?")
        end = abbreviation.end()
    pattern.extend((re.escape(form[end:]), r"(?!\w)"))
    return re.compile("".join(pattern))


def _build_forms(courts: dict[str, _Court]) -> dict[str, tuple[_Form, ...]]:
    forms: dict[str, list[tuple[str, re.Pattern[str], str]]] = defaultdict(list)
    for court_id, court in courts.items():
        for form in _court_forms(court):
            # Only forms with the same letters and literal punctuation can collide.
            key = form.replace(".", "").replace(" ", "")
            forms[key].append((form, _form_pattern(form), court_id))

    # A shared scoping form is dropped; a name that scopes nothing always keeps its span.
    kept: dict[str, _Form] = {}
    for group in forms.values():
        for form, pattern, court_id in group:
            if courts[court_id].level not in _SCOPING_BUILDERS:
                kept.setdefault(form, (pattern, None))
            elif not any(
                other_id != court_id and (pattern.fullmatch(other) or other_pattern.fullmatch(form))
                for other, other_pattern, other_id in group
            ):
                kept.setdefault(form, (pattern, court_id))

    indexed: dict[str, list[_Form]] = defaultdict(list)
    for form, entry in kept.items():
        start = _FORM_STARTS.match(form)
        assert start is not None
        indexed[start[0]].append(entry)
    return {prefix: tuple(entries) for prefix, entries in indexed.items()}


def _matching_forms(source: str, forms: dict[str, tuple[_Form, ...]]) -> Iterable[tuple[int, int, str | None]]:
    for token in _FORM_STARTS.finditer(source):
        start = token.start()
        prefix = token[0]
        # A packed abbreviation can start with a shorter key than its typed token.
        for length in range(1, len(prefix) + 1):
            for pattern, court in forms.get(prefix[:length], ()):
                if (match := pattern.match(source, start)) is not None:
                    yield start, match.end(), court


def _overlaps(spans: Sequence[tuple[int, int]], start: int, end: int) -> bool:
    return any(span_start < end and start < span_end for span_start, span_end in spans)


def _normalize_message(message: str) -> tuple[str, list[int]]:
    parts: list[str] = []
    offsets: list[int] = []
    for offset, character in enumerate(message):
        if character.isspace():
            if not parts or parts[-1] != " ":
                parts.append(" ")
                offsets.append(offset)
        else:
            folded = character.casefold()
            parts.append(folded)
            offsets.extend([offset] * len(folded))
    return "".join(parts), offsets


def _citation_parentheticals(
    message: str, objects: Sequence[_ExactSpan],
) -> list[tuple[int, int]]:
    attached = {
        match.end() - 1 for obj in objects
        if obj.type in ("case_cite", "docket")
        if (match := _ATTACHED_PARENTHESIS.match(message, obj.end)) is not None
    }
    spans = [(obj.start, obj.end) for obj in objects]
    opens: list[int] = []
    blocked: list[tuple[int, int]] = []
    year = -1
    for token in _PARENTHETICAL_TOKENS.finditer(message):
        if _overlaps(spans, token.start(), token.end()):
            # A section or volume number inside an exact object is not a court parenthetical's year.
            continue
        if token[0] == "(":
            opens.append(token.start())
        elif token[0] == ")":
            if not opens:
                continue
            opening = opens.pop()
            if year > opening or opening in attached:
                blocked.append((opening + 1, token.start()))
        else:
            year = token.start()

    outermost: list[tuple[int, int]] = []
    for span in sorted(blocked):
        if not outermost or span[1] > outermost[-1][1]:
            outermost.append(span)
    return outermost
