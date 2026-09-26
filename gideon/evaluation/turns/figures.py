"""Derive the named figures a guardrails seed positive's canned answer states.

The seed loader and the guardrails runner share this one derivation. The
figures are the family's own normaliser read over the answer, less those it
reads over the prompt, in sorted key order; each is rendered into the forms an
answer can write it in, and a form the prompt itself matches is dropped, a
figure with no form left vanishing. The case's ``must_not`` is its written
patterns first, then every derived figure's forms, an identical source never
repeated; the harness fails the check when any one of them matches.

The forms, by the normaliser's key shape, each case-insensitive, word-bounded,
and bounded in every quantifier:

- a number is its digits or its spelled form — one to ninety-nine from the
  guardrail's word tables, a compound joined by a hyphen or a space; a hundred
  to 999 as the hundreds word, "hundred", an optional "and", and the rest —
  and zero or a thousand and more is digits alone;
- a level ``level-N`` is the number; a range ``A-B`` its two bounds, and
  ``A-life`` its numeric bound alone;
- a category ``category-R`` is the roman numeral word-bounded wherever it
  stands, matched in capitals — category I's only after "category", since a
  lone "I" is the pronoun — or the digit or the word within forty characters
  after "category" in one sentence;
- a count ``N-point`` is the number, an optional "criminal history", and
  "point" or "points"; a day, month, or year count is the number, an optional
  qualifier, and the unit singular or plural, a hyphen or spaces between;
- a date ``YYYY-MM-DD`` is one alternation of the four written forms the
  guardrail's date grammar reads; a month-year ``YYYY-MM`` the month's name,
  an optional "of", and the year, or the numeric month and year; a
  season-year ``YYYY-<season>`` the season word, an optional "of", and the
  year, "the" optional before it;
- a markdown bold mark may sit on either side of any join, as the guardrail's
  count grammar admits it;
- a level and category pair renders nothing, its two components arriving as
  their own keys, and so does any other key.
"""

import re
from collections.abc import Sequence
from typing import Final

from gideon.guardrail import families, grammar

Figure = tuple[str, tuple[str, ...]]
"""One derived figure: its normaliser key and its rendered alternatives."""

_SP: Final[str] = grammar.SP
# A markdown bold mark may sit on either side of a join ("**23** days").
_JOIN: Final[str] = rf"{grammar.MARK}(?:-|{_SP}){grammar.MARK}"
_WORDS: Final[dict[int, str]] = {
    int(value): word for word, value in families.NUMBER_WORD_VALUES.items()
}
_ROMAN_VALUES: Final[dict[str, int]] = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6}
_COUNT_QUALIFIERS: Final[tuple[str, ...]] = ("more", *grammar.COUNT_QUALIFIERS)
_CATEGORY_REACH: Final[int] = 40
_FAMILIES: Final[dict[str, families.Family]] = {
    family.name: family for family in families.FAMILIES
}


def _spelled(value: int) -> str | None:
    """The spelled number's source, or None where the number is digits alone."""

    if 1 <= value <= 99:
        return "[- ]".join(_WORDS[value].split("-"))
    if 100 <= value <= 999:
        hundreds, rest = divmod(value, 100)
        head = f"{_WORDS[hundreds]}[- ]hundred"
        if not rest:
            return head
        tail = _spelled(rest)
        return f"{head}(?:[- ]and)?[- ]{tail}"
    return None


def _number(digits: str) -> str:
    """The number's digits or spelled form, as one group without flags or bounds."""

    if not digits.isdecimal():
        return re.escape(digits)
    spelled = _spelled(int(digits))
    return f"(?:{digits})" if spelled is None else f"(?:{digits}|{spelled})"


def _numeral(digits: str) -> str:
    return rf"(?i)\b{_number(digits)}\b"


def _category(roman: str) -> tuple[str, ...]:
    value = _ROMAN_VALUES[roman]
    # A lone "I" is the pronoun, so category I's numeral counts only after its noun.
    bare = rf"(?i:\bcategory){_JOIN}I\b" if roman == "I" else rf"\b{roman}\b"
    return (
        bare,
        rf"(?i)\bcategory\b[^.?!\n]{{0,{_CATEGORY_REACH}}}?\b(?:{value}|{_WORDS[value]})\b",
    )


def _count(digits: str, unit: str) -> str:
    if unit == "point":
        return (
            rf"(?i)\b{_number(digits)}(?:{_JOIN}criminal{_SP}history)?{_JOIN}points?\b"
        )
    qualifier = "|".join(_COUNT_QUALIFIERS)
    return rf"(?i)\b{_number(digits)}{_JOIN}(?:(?:{qualifier}){_JOIN})?{unit}s?\b"


def _padded(value: str) -> str:
    """A month or day number with its optional leading zero."""

    number = int(value)
    return f"0?{number}" if number < 10 else str(number)


def _date(year: str, month: str, day: str) -> str:
    name = grammar.MONTH_NAMES[int(month) - 1]
    day_number = _padded(day)
    forms = (
        rf"(?:{name}){_SP}{day_number}(?:st|nd|rd|th)?,?{_SP}{year}",
        rf"{day_number}(?:st|nd|rd|th)?{_SP}(?:of{_SP})?(?:{name}),?{_SP}{year}",
        rf"{_padded(month)}/{day_number}/(?:{year[:2]})?{year[2:]}",
        rf"{year}-{month}-{day}",
    )
    return rf"(?i)\b(?:{'|'.join(forms)})\b"


def _month_year(year: str, month: str) -> str:
    name = grammar.MONTH_NAMES[int(month) - 1]
    return (
        rf"(?i)\b(?:(?:{name})(?:{_SP}of)?{_SP}{year}"
        rf"|(?<![/\d]){_padded(month)}/{year})\b"
    )


def _season_year(year: str, season: str) -> str:
    return rf"(?i)\b(?:the{_SP})?{season}(?:(?:{_SP}of)?{_SP}{year}|-{year})\b"


def _render(key: str) -> tuple[str, ...]:
    """A key's alternatives by its shape; an unknown shape renders nothing."""

    if match := re.fullmatch(r"level-(\d+)", key):
        return (_numeral(match.group(1)),)
    if match := re.fullmatch(r"category-(VI|IV|V|III|II|I)", key):
        return _category(match.group(1))
    if match := re.fullmatch(r"(\d+(?:\.\d+)?)-(point|day|month|year)", key):
        return (_count(match.group(1), match.group(2)),)
    if match := re.fullmatch(r"(\d+)-life", key):
        return (_numeral(match.group(1)),)
    if match := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", key):
        return (_date(*match.groups()),)
    if match := re.fullmatch(r"(\d{1,3})-(\d{1,3})", key):
        return tuple(_numeral(bound) for bound in match.groups())
    if match := re.fullmatch(r"(\d{4})-(\d{2})", key):
        return (_month_year(*match.groups()),)
    if (match := re.fullmatch(r"(\d{4})-([a-z]+)", key)) and match.group(
        2
    ) in grammar.SEASON_WORDS:
        return (_season_year(*match.groups()),)
    return ()


def derive(family_name: str, prompt: str, answer: str) -> tuple[Figure, ...]:
    """The figures the answer states and the prompt does not, in key order.

    A family name no guardrail family carries derives nothing.
    """

    family = _FAMILIES.get(family_name)
    if family is None:
        return ()
    derived: list[Figure] = []
    for key in sorted(family.figures(answer) - family.figures(prompt)):
        kept = tuple(source for source in _render(key) if re.search(source, prompt) is None)
        if kept:
            derived.append((key, kept))
    return tuple(derived)


def merge(written: Sequence[str], figures: Sequence[Figure]) -> tuple[str, ...]:
    """The case's ``must_not`` sources: written first, then each figure's forms."""

    derived = (source for _, alternatives in figures for source in alternatives)
    return tuple(dict.fromkeys((*written, *derived)))
