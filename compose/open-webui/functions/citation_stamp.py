"""title: GIDEON citation stamp
version: 1
description: Appends General's fixed citation warning to any answer that carries a citation shape.
"""

# The citation stamp (spec §15, [06] item 16, ADR-0020): General's second
# mechanical guardrail, an outlet Filter attached to General's preset alone by
# the record's filterIds.  It reads the finished answer — the answer text
# alone, never the reasoning items — for anything citation-shaped and appends
# one fixed sentence after it: a warning label, never a verdict, so
# over-triggering is harmless and a miss is the failure, and on its own
# internal error it stamps anyway.  It runs at the outlet rather than in the
# stream because a trailing label's place is the end of the answer, which only
# the finished message has (slice-1 ticket 14).  The file runs inside the
# frontend's container and is imported by path in the unit tests, so it imports
# the standard library only, defines no Valves (nothing is tunable) and no
# toggle (a user could switch a toggleable Filter off), carries no requirements
# line (a pip install at load), and never contains the four import prefixes the
# frontend's rewriter replaces over the whole file — the word "from" followed
# by utils, apps, main, or config (docs/research/owui-filter-function.md §4.2).
# The two output helpers are copied from arithmetic_guardrail.py because a
# Function module cannot import another.
import re
from collections.abc import Mapping
from dataclasses import dataclass

# §15's sentence, verbatim: fixed product text, no site value (ADR-0028).
CITATION_STAMP = (
    "General does not verify citations. Anything you intend to rely on must be checked in Research."
)
STAMP_SEPARATOR = "\n\n"
# The longest text one pattern may match; a test holds every pattern to it.
MAX_MATCH_CHARS = 150
# The id of a message item the stamp has to create (the renderer reads an
# item's id for its key alone; the frontend's own ids are msg_ plus 24 hex).
STAMP_MESSAGE_ID = "msg_000000000000000000000000"

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


def answer_text(message: Mapping[str, object]) -> str:
    """The answer as the user reads it: the content, else the message items' text.

    The frontend's own fallback for an empty content is the concatenated text
    of the output's message items; the reasoning items are never part of it.
    """

    content = message.get("content")
    if isinstance(content, str) and content:
        return content
    output = message.get("output")
    if not isinstance(output, list):
        return ""
    texts: list[str] = []
    for item in output:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        parts = item.get("content")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                texts.append(str(part["text"]))
    return "".join(texts)


def detect(text: str) -> str | None:
    """The first family whose shape the text carries, in the fixed order, or None."""

    for pattern in PATTERNS:
        if pattern.regex.search(text) is not None:
            return pattern.pattern_id
    return None


def is_stamped(text: str) -> bool:
    """Whether the text, whitespace-normalised, already ends with the sentence."""

    return " ".join(text.split()).endswith(" ".join(CITATION_STAMP.split()))


def _last_message_item(output: list[object]) -> dict[str, object] | None:
    for item in reversed(output):
        if isinstance(item, dict) and item.get("type") == "message":
            return item
    return None


def _message_item(text: str) -> dict[str, object]:
    return {
        "type": "message",
        "id": STAMP_MESSAGE_ID,
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def _append_output_text(item: dict[str, object], tail: str) -> None:
    parts = item.get("content")
    if not isinstance(parts, list):
        parts = []
        item["content"] = parts
    for part in reversed(parts):
        if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
            part["text"] += tail
            return
    parts.append({"type": "output_text", "text": tail})


def append_stamp(message: dict[str, object], text: str) -> None:
    """Append the sentence to the answer, in the content and in the structured output.

    ``text`` is the answer as ``answer_text`` read it, so an empty content is
    replaced by the answer and the sentence, never by the sentence alone.  The
    output is touched only when the message carries a list: the renderer
    prefers a non-empty output to the content, so a message item the stamp
    has to create carries the whole stamped answer, not the label alone.
    """

    tail = STAMP_SEPARATOR + CITATION_STAMP
    message["content"] = text + tail
    output = message.get("output")
    if not isinstance(output, list):
        return
    item = _last_message_item(output)
    if item is None:
        output.append(_message_item(text + tail))
    else:
        _append_output_text(item, tail)


def _last_assistant(body: object) -> dict[str, object] | None:
    """The assistant message the outlet judges, or None for a body of another shape."""

    if not isinstance(body, Mapping):
        return None
    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    for item in reversed(messages):
        if isinstance(item, dict) and item.get("role") == "assistant":
            return item
    return None


def _stamp_anyway(body: object) -> None:
    message = _last_assistant(body)
    if message is None:
        return
    text = answer_text(message)
    if not is_stamped(text):
        append_stamp(message, text)


class Filter:
    """The citation stamp: General's outlet Filter (spec §15)."""

    def outlet(self, body: object) -> object:
        """Stamp a citation-shaped answer once; return the same body on every path.

        The same object is returned because the frontend persists and pushes to
        the screen whatever the outlet returns, and a None skips both
        (docs/research/owui-filter-function.md §5.3).
        """

        try:
            message = _last_assistant(body)
            if message is None:
                return body
            text = answer_text(message)
            if not is_stamped(text) and detect(text) is not None:
                append_stamp(message, text)
        except Exception:  # noqa: BLE001 - an internal error fails toward the label: over-triggering is harmless.
            try:
                _stamp_anyway(body)
            except Exception:  # noqa: BLE001 - a body the fallback cannot stamp is returned as it stands.
                return body
        return body
