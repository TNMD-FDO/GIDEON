"""General's citation detection and its fixed warning label (spec §15).

The service's judged paths append the label this module decides on, the turn
harness's classifier strips it from a measured length, and
``tests/test_api_stamp.py`` holds both.  It imports the standard library
alone: the harness imports it on the box's system Python, where the service
image's dependencies do not exist.

The detection and the text are the citation stamp Function's
(``compose/open-webui/functions/citation_stamp.py``), moved statement for
statement; ``tests/test_api_stamp.py`` holds the two files equal until
ticket 09's cutover deletes the Function.
"""

# The citation stamp (spec §15, [06] item 16, ADR-0020): General's second
# mechanical guardrail.  It reads the finished answer — the answer text alone,
# never the reasoning — for anything citation-shaped and appends one fixed
# sentence after it: a warning label, never a verdict, so over-triggering is
# harmless and a miss is the failure (ADR-0043), and the stamp's own internal
# error fails toward the label.
import re
from dataclasses import dataclass
from typing import Final

# ADR-0043 and §15: fixed product text, no site value (ADR-0028).
CITATION_STAMP = "General does not verify citations."
STAMP_SEPARATOR = "\n\n"
# The longest text one pattern may match; a test holds every pattern to it.
MAX_MATCH_CHARS = 150

REPORTER_FAMILY_ID = "citation/reporter@1"
CODE_FAMILY_ID = "citation/code@1"
RULE_FAMILY_ID = "citation/rule@1"
DATABASE_FAMILY_ID = "citation/database@1"

# Volume, abbreviation, page: the Supreme Court's reporters, the federal
# reporters and supplements, the federal appendix, the bankruptcy reporter, the
# Federal Register, and the regional reporters with their series.
REPORTER_ABBREVIATIONS = (
    "U.S.",
    "S. Ct.",
    "L. Ed.",
    "L. Ed. 2d",
    "F.",
    "F.2d",
    "F.3d",
    "F.4th",
    "F. Supp.",
    "F. Supp. 2d",
    "F. Supp. 3d",
    "F. App'x",
    "F. App’x",
    "Fed. Appx.",
    "B.R.",
    "Fed. Reg.",
    "So.",
    "So. 2d",
    "So. 3d",
    "P.",
    "P.2d",
    "P.3d",
    "N.E.",
    "N.E.2d",
    "N.E.3d",
    "N.W.",
    "N.W.2d",
    "N.W.3d",
    "S.E.",
    "S.E.2d",
    "S.W.",
    "S.W.2d",
    "S.W.3d",
    "A.",
    "A.2d",
    "A.3d",
)
# A code followed by a section symbol, or by the section number alone.
CODE_ABBREVIATIONS = (
    "U.S.C.",
    "U.S.C.A.",
    "U.S.C.S.",
    "USC",
    "C.F.R.",
    "CFR",
    "U.S.S.G.",
    "USSG",
    "Tenn. Code Ann.",
)
# A rule abbreviation alone is a shape — the ticket's own example is the bare
# abbreviation — with or without a rule number after it.
RULE_ABBREVIATIONS = (
    "Fed. R. Crim. P.",
    "Fed. R. Civ. P.",
    "Fed. R. App. P.",
    "Fed. R. Evid.",
    "Fed. R. Bankr. P.",
    "Sup. Ct. R.",
)
LEXIS_COURT_TOKENS = (
    "U.S. LEXIS",
    "U.S. Dist. LEXIS",
    "U.S. App. LEXIS",
)

# Every repeat carries an explicit upper bound: no pattern is unbounded.
_SPACE = r"\s{1,3}"
_GAP = r"\s{0,3}"
_BOUNDARY_START = r"(?<![\w.])"
_BOUNDARY_END = r"(?!\w)"
_VOLUME = r"(?:\d{1,6}|[_-]{3,12})"
_PAGE = r"(?:\d{1,8}|[_-]{3,12})"
# A section number: digits, an optional letter run (the Guidelines' 2D1.1),
# further parts joined by a dot or a hyphen (a state code's 40-35-501), and
# parenthesised subsections.
_SECTION = r"\d{1,6}(?:[A-Za-z]\d{0,3})?(?:[.-]\d{1,6}){0,3}(?:\s{0,2}\([A-Za-z0-9]{1,4}\)){0,6}"


@dataclass(frozen=True, slots=True)
class Pattern:
    """One bounded shape family: the id it reports and its compiled regex."""

    pattern_id: str
    regex: re.Pattern[str]


def _alternatives(values: tuple[str, ...]) -> str:
    """One alternation over fixed abbreviations, each internal space optional.

    The model writes "S. Ct." and "S.Ct." alike, so a space inside an
    abbreviation matches zero to three whitespace characters.
    """

    escaped = (re.escape(value).replace("\\ ", " ").replace(" ", _GAP) for value in values)
    return "(?:" + "|".join(escaped) + ")"


def _compiled(pattern_id: str, source: str) -> Pattern:
    return Pattern(pattern_id, re.compile(source, re.IGNORECASE))


REPORTER_PATTERN = _compiled(
    REPORTER_FAMILY_ID,
    _BOUNDARY_START + _VOLUME + _SPACE + _alternatives(REPORTER_ABBREVIATIONS) + _SPACE + _PAGE + _BOUNDARY_END,
)
CODE_PATTERN = _compiled(
    CODE_FAMILY_ID,
    # A code with an optional title before it and an optional section symbol
    # after it (18 U.S.C. § 3553(a), U.S.S.G. §2D1.1, 18 U.S.C. 3553), or the
    # bare section symbol with a number (§ 2255, §§ 3553-3554).
    _BOUNDARY_START
    + r"(?:\d{1,5}"
    + _SPACE
    + r")?"
    + _alternatives(CODE_ABBREVIATIONS)
    + _GAP
    + r"§{0,2}"
    + _GAP
    + _SECTION
    + _BOUNDARY_END
    + r"|"
    + _BOUNDARY_START
    + r"§{1,2}"
    + _GAP
    + _SECTION
    + _BOUNDARY_END,
)
RULE_PATTERN = _compiled(
    RULE_FAMILY_ID,
    _BOUNDARY_START + _alternatives(RULE_ABBREVIATIONS) + r"(?:" + _SPACE + r"\d{1,3}(?:\.\d{1,2})?)?" + _BOUNDARY_END,
)
DATABASE_PATTERN = _compiled(
    DATABASE_FAMILY_ID,
    _BOUNDARY_START
    + r"\d{4}"
    + _SPACE
    + r"WL"
    + _SPACE
    + r"\d{1,12}"
    + _BOUNDARY_END
    + r"|"
    + _BOUNDARY_START
    + r"\d{4}"
    + _SPACE
    + _alternatives(LEXIS_COURT_TOKENS)
    + _SPACE
    + r"\d{1,12}"
    + _BOUNDARY_END,
)
# The fixed order decides which family a text with several shapes reports.
PATTERNS = (REPORTER_PATTERN, CODE_PATTERN, RULE_PATTERN, DATABASE_PATTERN)


def detect(text: str) -> str | None:
    """The first family whose shape the text carries, in the fixed order, or None."""

    for pattern in PATTERNS:
        if pattern.regex.search(text) is not None:
            return pattern.pattern_id
    return None


def is_stamped(text: str) -> bool:
    """Whether the text, whitespace-normalised, already ends with the sentence."""

    return " ".join(text.split()).endswith(" ".join(CITATION_STAMP.split()))


# exempt: fixed product text (ADR-0043), never a tuned figure: the one string
# every reader of General's answer appends or strips.
STAMP_TAIL: Final[str] = STAMP_SEPARATOR + CITATION_STAMP


def tail_for(text: str) -> str:
    """The tail the answer is owed: the label, or the empty string.

    An answer :func:`is_stamped` already reads as stamped is owed nothing, one
    :func:`detect` finds a shape in is owed the label, and anything else is
    owed nothing.  The stamp's own failure fails toward the label, as the
    Function's fallback does — an already-stamped answer is still owed nothing
    when that second read succeeds — so this never raises and its callers need
    no guard of their own.  It logs nothing: it holds model text (spec §19.4).
    """

    try:
        if is_stamped(text):
            return ""
        if detect(text) is not None:
            return STAMP_TAIL
    except Exception:  # noqa: BLE001 - the stamp's own failure fails toward the label.
        try:
            return "" if is_stamped(text) else STAMP_TAIL
        except Exception:  # noqa: BLE001 - a text neither read survives is stamped.
            return STAMP_TAIL
    return ""
