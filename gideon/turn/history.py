"""Build prior-turn messages with earlier assistant answers in name-only form."""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Protocol

from gideon.turn.research import RESEARCH_CONFIG


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One prior message with its text excluded from the representation."""

    role: str
    text: str = field(repr=False)


class CitationFinding(Protocol):
    """The citation span and classification needed to strip an answer."""

    @property
    def start(self) -> int: ...

    @property
    def end(self) -> int: ...

    @property
    def form(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def pincite(self) -> str | None: ...


_MARKER = re.compile(r"\[(?:SRC:)?[0-9]+\]")
_AT = re.compile(r"\bat[ \t]*\Z", re.IGNORECASE)
_DASH_PAGE = r"[ \t]*[-–—][ \t]*[0-9]+"
_PAGE = rf"[0-9]+(?:{_DASH_PAGE})?"
_PAGE_RUN = re.compile(
    rf"(?:(?P<range_end>{_DASH_PAGE})|(?P<first>[ \t]*{_PAGE}))?"
    rf"(?P<suffix>(?:[ \t]*,[ \t]*{_PAGE})*)"
)
_BOUNDARY = r"(?:[\[)!?;]|[.:](?![0-9])|\Z)"
# A court-and-year parenthetical, a year alone or after court-style tokens such as "6th Cir."
# or "N.D. Ill. Mar. 3,", ends a citation wherever the sentence goes on. Any other parenthetical
# ends one only when it closes before a boundary or a comma, so a prose aside such as
# "14 (of 20) petitions" or "14 (all filed in 2020) were" keeps its number.
_COURT_TOKEN = r"(?:[0-9]+(?:st|nd|rd|th|d)|[A-Z][A-Za-z.'’&]*|[0-9]{1,2},)"
_COURT_YEAR = rf"\((?:{_COURT_TOKEN}[ \t]+)*[0-9]{{4}}\)"
_CITATION_BOUNDARY = re.compile(
    rf"[ \t]*(?:{_BOUNDARY}|{_COURT_YEAR}|(?:\([^()]*\)[ \t]*)+(?:{_BOUNDARY}|,))"
)
_LEADING_PAGE = re.compile(r"(?:at[ \t]+)?\*{0,2}[0-9]+", re.IGNORECASE)


def _citation_span(_: str, finding: CitationFinding) -> tuple[int, int]:
    return finding.start, finding.end


def _full_span(text: str, finding: CitationFinding) -> tuple[int, int]:
    # The adapter's pin is the page itself, so it is removed wherever it directly
    # follows the comma: whole as reported, else by its leading page, whose range the
    # page-run rule then takes. The pages after it take the comma list's boundary check.
    reported = (finding.pincite or "").partition(",")[0].strip()
    leading = _LEADING_PAGE.match(reported)
    for pin in dict.fromkeys((reported, leading[0] if leading else "")):
        if pin and (match := re.match(rf"[ \t]*,[ \t]*{re.escape(pin)}(?![0-9])", text[finding.end:])):
            return finding.start, finding.end + match.end()
    return finding.start, finding.end


def _supra_span(text: str, finding: CitationFinding) -> tuple[int, int]:
    # A bare supra finding can include the sentence's period.
    end = finding.end - 1 if text[finding.end - 1] == "." else finding.end
    return finding.start, end


def _reference_pin_span(text: str, finding: CitationFinding) -> tuple[int, int] | None:
    if not finding.pincite:
        return None
    pin = re.sub(r"\Aat[ \t]+", "", finding.pincite, flags=re.IGNORECASE)
    match = re.search(
        rf"[ \t]+at[ \t]+{re.escape(pin)}[ \t]*\Z", text[finding.start:finding.end], re.IGNORECASE,
    )
    return (finding.start + match.start(), finding.end) if match else None


CITATION_RULES: Final[Mapping[str, Callable[[str, CitationFinding], tuple[int, int] | None]]] = MappingProxyType({
    "full": _full_span,
    "short": _citation_span,
    "id": _citation_span,
    "supra": _supra_span,
    "reference": _reference_pin_span,
})


def _page_run_end(text: str, start: int, end: int, limit: int) -> int:
    match = _PAGE_RUN.match(text, end, limit)
    assert match is not None
    if match["range_end"] and not text[end - 1].isdigit():
        return end
    if match["first"] and not _AT.search(text[start:end]):
        return end
    # Check the greedy comma list as a unit so a prose figure cannot make a shorter list match.
    if match["suffix"] and not _CITATION_BOUNDARY.match(text, match.end()):
        return match.start("suffix")
    return match.end()


def _cut(text: str, start: int, end: int, floor: int = 0) -> str:
    left_end = floor + len(text[floor:start].rstrip(" \t"))
    left = text[:left_end]
    right = text[end:].lstrip(" \t")
    if (
        (left_end < start or len(right) < len(text) - end)
        and left and left[-1] not in "\r\n([" and right and right[0] not in "\r\n,.;:!?)]"
    ):
        return f"{left} {right}"
    return left + right


def name_only(text: str, findings: Sequence[CitationFinding]) -> str:
    """Strip case citations, attached pages, and source markers from an answer.

    Drop the comma before a removed citation and keep the sentence's period
    when a bare supra span includes it. At each cut, fold adjoining
    horizontal space to one space and remove it at a line edge, after an
    opening bracket or parenthesis, or before closing punctuation.
    """

    ordered = sorted(findings, key=lambda finding: finding.start)
    previous_end = 0
    for finding in ordered:
        if not 0 <= finding.start < finding.end <= len(text):
            raise ValueError("citation span is outside the answer")
        if finding.start < previous_end:
            raise ValueError(f"overlapping citation at offset {finding.start}")
        if finding.form not in CITATION_RULES:
            raise ValueError(f"unsupported citation form at offset {finding.start}")
        previous_end = finding.end

    stripped = text
    for index in range(len(ordered) - 1, -1, -1):
        finding = ordered[index]
        if finding.kind != "case" or (span := CITATION_RULES[finding.form](text, finding)) is None:
            continue
        start, end = span
        floor = ordered[index - 1].end if index else 0
        limit = ordered[index + 1].start if index + 1 < len(ordered) else len(text)
        if end > limit:
            end = finding.end
        if end >= finding.end:
            end = _page_run_end(text, finding.start, end, limit)
        if start == finding.start:
            before = text[floor:start].rstrip(" \t")
            if before.endswith(","):
                start = floor + len(before) - 1
        stripped = _cut(stripped, start, end, floor)
    for marker in reversed(list(_MARKER.finditer(stripped))):
        stripped = _cut(stripped, marker.start(), marker.end())
    return stripped


def prior_turn_messages(
    entries: Sequence[HistoryEntry], findings: Mapping[int, Sequence[CitationFinding]],
) -> list[dict[str, str]]:
    """Build ordered messages using assistant findings keyed by entry position."""

    if RESEARCH_CONFIG["history_form"] != "name_only":
        raise ValueError("unsupported history_form")
    messages: list[dict[str, str]] = []
    for index, entry in enumerate(entries):
        if entry.role == "user":
            content = entry.text
        elif entry.role == "assistant":
            if index not in findings:
                raise ValueError(f"missing citation findings at history position {index}")
            content = name_only(entry.text, findings[index])
        else:
            raise ValueError(f"unsupported history role at position {index}")
        messages.append({"role": entry.role, "content": content})
    return messages
