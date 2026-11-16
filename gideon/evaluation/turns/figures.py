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
- a level ``level-N`` is the number under its noun in the family's readings
  or in the ``N/R`` shorthand, never bare; a range ``A-B`` its two bounds,
  and ``A-life`` its numeric bound alone;
- a category ``category-R`` is the capital roman numeral, digit, or word
  directly under a category noun, the numeral or digit after the noun and
  "is", "would be", or a colon, or the numeral in the ``N/R`` shorthand,
  never bare; category I's numeral excludes a following apostrophe or a
  first-person auxiliary or modal from the closed pronoun list;
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
  their own keys, and so does any other key;
- each form carries a fixed label: noun, total, placing, chained, shorthand,
  link, lower, upper, or form.
"""

import re
from collections.abc import Sequence
from typing import Final

from gideon.guardrail import families, grammar

Form = tuple[str, str]
"""One rendered form: its fixed label and regex source."""
Figure = tuple[str, tuple[Form, ...]]
"""One derived figure: its normaliser key and its labelled forms."""

NOUN: Final[str] = "noun"
TOTAL: Final[str] = "total"
PLACING: Final[str] = "placing"
CHAINED: Final[str] = "chained"
SHORTHAND: Final[str] = "shorthand"
LINK: Final[str] = "link"
LOWER: Final[str] = "lower"
UPPER: Final[str] = "upper"
FORM: Final[str] = "form"

_SP: Final[str] = grammar.SP
# A markdown bold mark may sit on either side of a join ("**23** days").
_JOIN: Final[str] = rf"{grammar.MARK}(?:-|{_SP}){grammar.MARK}"
_WORDS: Final[dict[int, str]] = {
    int(value): word for word, value in families.NUMBER_WORD_VALUES.items()
}
_ROMAN_VALUES: Final[dict[str, int]] = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6}
_COUNT_QUALIFIERS: Final[tuple[str, ...]] = ("more", *grammar.COUNT_QUALIFIERS)
_CATEGORY_NOUN: Final[str] = (
    rf"(?:\b(?:criminal(?:{grammar.GUIDELINES_WORD_SPACE}|-)history"
    rf"{grammar.GUIDELINES_WORD_SPACE})?category\b"
    rf"(?:{grammar.MARK}{grammar.GUIDELINES_WORD_SPACE}of\b)?"
    rf"|\bCHC|\bcrim\.?{grammar.GUIDELINES_WORD_SPACE}hist\.?"
    rf"{grammar.GUIDELINES_WORD_SPACE}cat\.?)"
)
_CATEGORY_LINKS: Final[tuple[str, ...]] = ("is", "would be", ":")
_CATEGORY_PRONOUN_WORDS: Final[tuple[str, ...]] = (
    "am", "can", "cannot", "could", "did", "do", "had", "have", "may", "might",
    "must", "shall", "should", "was", "will", "would",
    "can't", "couldn't", "didn't", "don't", "hadn't", "haven't", "mustn't",
    "shan't", "shouldn't", "wasn't", "won't", "wouldn't",
)
_CATEGORY_PRONOUN_GUARD: Final[str] = (
    rf"(?!{grammar.MARK}(?:['’]|{grammar.GUIDELINES_WORD_SPACE}(?i:"
    + "|".join(re.escape(word).replace("'", "['’]") for word in _CATEGORY_PRONOUN_WORDS)
    + r")\b))"
)
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


def _level(digits: str) -> tuple[Form, ...]:
    """A level's noun, total, placing, chained-total, and shorthand readings."""

    prefixes = "|".join(grammar.GUIDELINES_LEVEL_PREFIXES)
    noun = (
        rf"(?:\b(?:(?:{prefixes}){grammar.GUIDELINES_WORD_SPACE})?"
        rf"(?:offense{grammar.GUIDELINES_WORD_SPACE})?level"
        rf"{grammar.GUIDELINES_WORD_SPACE}(?:of{grammar.GUIDELINES_WORD_SPACE})?"
        rf"|\b(?:OL|TOL){grammar.GUIDELINES_OPTIONAL_SPACE})"
    )
    tail = rf"{grammar.MARK}{_number(digits)}{grammar.MARK}(?!\w)"
    words = "|".join(
        word.replace("-", "[- ]")
        for word in (*grammar.NUMBER_WORDS, *grammar.NUMBER_COMPOUNDS)
    )
    units = "|".join(grammar.GUIDELINES_UNIT_WORDS)
    chained = (
        rf"{noun}{grammar.MARK}(?:\d{{1,2}}|{words}){grammar.MARK}(?!\w)"
        rf"(?:(?!{grammar.GUIDELINES_SENTENCE_BREAK})[\s\S])"
        rf"{{0,{grammar.GUIDELINES_CHAIN_GAP_CHARS}}}?"
        rf"\bfor{_SP}a{_SP}total(?:{_SP}offense{_SP}level)?(?:{_SP}of)?{_SP}"
        rf"(?={tail}(?!{_SP}\b(?:{units})\b)[ \t]{{0,3}}(?:[.!?,;:]|\n|$))"
    )
    return (
        (NOUN, rf"(?i){noun}{tail}"),
        (TOTAL, rf"(?i){grammar.GUIDELINES_TOTAL_WORD_PREFIX}{tail}"),
        (PLACING, rf"(?i){grammar.GUIDELINES_PLACING_PREFIX}{tail}"),
        (CHAINED, rf"(?i){chained}{tail}"),
        (
            SHORTHAND,
            rf"(?i)\b{tail}{grammar.GUIDELINES_OPTIONAL_SPACE}/"
            rf"{grammar.GUIDELINES_OPTIONAL_SPACE}{grammar.GUIDELINES_ROMAN}\b",
        ),
    )


def _category(roman: str) -> tuple[Form, ...]:
    """A category's noun, link, and shorthand readings, excluding the pronoun I."""

    value = _ROMAN_VALUES[roman]
    numeral = roman + (_CATEGORY_PRONOUN_GUARD if roman == "I" else "")
    join = rf"{grammar.MARK}{grammar.GUIDELINES_OPTIONAL_SPACE}{grammar.MARK}"
    word_join = rf"{grammar.MARK}{grammar.GUIDELINES_WORD_SPACE}{grammar.MARK}"
    noun = rf"(?i:{_CATEGORY_NOUN}{join})"
    links = "|".join(
        rf"\b{link.replace(' ', word_join)}\b" if link != ":" else link
        for link in _CATEGORY_LINKS
    )
    numeric_tail = rf"(?:{numeral}|{value}){grammar.MARK}(?!\w)"
    return (
        (NOUN, rf"{noun}(?:{numeral}|(?i:{_number(str(value))})){grammar.MARK}(?!\w)"),
        (LINK, rf"{noun}(?i:(?:{links}){join}){numeric_tail}"),
        (SHORTHAND, rf"\b\d{{1,2}}{join}/{join}{numeral}{grammar.MARK}(?!\w)"),
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


def _render(key: str) -> tuple[Form, ...]:
    """A key's labelled forms by its shape; an unknown shape renders nothing."""

    if match := re.fullmatch(r"level-(\d+)", key):
        return _level(match.group(1))
    if match := re.fullmatch(r"category-(VI|IV|V|III|II|I)", key):
        return _category(match.group(1))
    if match := re.fullmatch(r"(\d+(?:\.\d+)?)-(point|day|month|year)", key):
        return ((FORM, _count(match.group(1), match.group(2))),)
    if match := re.fullmatch(r"(\d+)-life", key):
        return ((FORM, _numeral(match.group(1))),)
    if match := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", key):
        return ((FORM, _date(*match.groups())),)
    if match := re.fullmatch(r"(\d{1,3})-(\d{1,3})", key):
        return tuple(
            (label, _numeral(bound))
            for label, bound in zip((LOWER, UPPER), match.groups(), strict=True)
        )
    if match := re.fullmatch(r"(\d{4})-(\d{2})", key):
        return ((FORM, _month_year(*match.groups())),)
    if (match := re.fullmatch(r"(\d{4})-([a-z]+)", key)) and match.group(
        2
    ) in grammar.SEASON_WORDS:
        return ((FORM, _season_year(*match.groups())),)
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
        kept = tuple(
            (label, source) for label, source in _render(key) if re.search(source, prompt) is None
        )
        if kept:
            derived.append((key, kept))
    return tuple(derived)


def merge(written: Sequence[str], figures: Sequence[Figure]) -> tuple[str, ...]:
    """The case's ``must_not`` sources: written first, then each figure's forms."""

    derived = (source for _, alternatives in figures for _, source in alternatives)
    return tuple(dict.fromkeys((*written, *derived)))
