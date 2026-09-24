"""The guardrail's three families: their refusals, the ``Pattern``, ``Family``,
and ``Trip`` shapes, the compiled patterns and confirmation contexts, the date,
count, and Guidelines normalisers, and ``FAMILIES`` with the refusal lookups.

In prefix mode a hit the judge did not trip on is never split from the context
that follows and decided it: a pattern rejecting on following context owes
``judge_text`` that context's span as a release constraint, as the rate form
and an exclusion hit reaching past its match do.

Built over ``grammar``; ``judge``, ``writer``, and ``window`` import from it.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from gideon.guardrail.grammar import (
    AFFIRMATION,
    APOSTROPHE,
    ATTRIBUTION_FORM,
    CAP_EXCLUSION_SOURCE,
    CREDIT_COUNT_PATTERN_SOURCE,
    DATE,
    DATE_CONFIRMED,
    DATE_FORM,
    DATE_NEAR_DEADLINE_WORD,
    DAYS_ELAPSED,
    DAYS_EXCLUSION_SOURCE,
    DAYS_REMAINING,
    DEADLINE_COUNT_QUESTION_FORM,
    DEADLINE_COUNT_SEPARATOR,
    DEADLINE_FIGURE,
    DEADLINE_FIGURE_FORM,
    DEADLINE_FIGURE_NOUNS,
    ELAPSED_EXCLUSION_SOURCE,
    EXPIRY_EXCLUSION_SOURCE,
    GUIDELINES_CHAINED_TOTAL_FORM,
    GUIDELINES_FIGURE,
    GUIDELINES_FIGURE_NOUNS,
    GUIDELINES_FIGURE_SOURCE,
    GUIDELINES_LEVEL_AND_CATEGORY_TO_RANGE,
    GUIDELINES_LEVEL_TOTAL,
    GUIDELINES_OPTIONAL_SPACE,
    GUIDELINES_POINT_LIST_EXCLUSION_SOURCE,
    GUIDELINES_POINTS_TO_CATEGORY,
    GUIDELINES_RANGE_ASSERTED,
    GUIDELINES_RANGE_FORM,
    GUIDELINES_RANGE_OR_PAIR_SOURCE,
    GUIDELINES_REVERSE_FORM,
    GUIDELINES_STATED_TOTAL_FORM,
    MARK,
    MONTH,
    MONTH_NAMES,
    NUMBER_COMPOUNDS,
    NUMBER_WORDS,
    ORDINAL,
    RATE_LOOKAHEAD,
    RATE_OPTIONAL,
    RELEASE_DATE_FORM,
    RELEASE_DATE_PATTERN_SOURCE,
    SEASON_WORDS,
    SENTENCE_COUNT_FORM,
    SENTENCE_CREDIT_CONFIRMATION_FAMILY_NOUNS,
    SENTENCE_CREDIT_CONFIRMATION_NOUNS,
    SENTENCE_CREDIT_CONFIRMATION_TARGETS,
    SENTENCE_CREDIT_ECHO_FORM,
    SENTENCE_FIGURE,
    SENTENCE_FIGURE_FORM,
    SENTENCE_FIGURE_NOUNS,
    SENTENCE_START,
    SP,
    TIME_TO_SERVE_PATTERN_SOURCE,
    UNNEGATED_PREFIX,
    _build_constructions,
    _deadline_count,
    _phrases,
)

DEADLINE_REFUSAL = (
    "GIDEON does not compute or confirm filing deadlines. Working out when something is due "
    "depends on legal judgments — what started the clock, what stopped it, and for how long — "
    "that a language model cannot make reliably, and the cost of being wrong can be the claim "
    "itself. Calculate this deadline with your unit's deadline procedure and a person who is "
    "responsible for it. GIDEON can explain how the limitations period works and cite the "
    "authorities — ask it that way."
)
GUIDELINES_REFUSAL = (
    "GIDEON does not compute or confirm a Sentencing Guidelines range. Turning an offense level "
    "and a criminal history category into a range, and the adjustments on the way there, are "
    "calculations a language model cannot make reliably — it has no table to read, only a memory "
    "of one, and a range recalled wrong is measured in months of a person’s liberty. Work the range "
    "out with the Sentencing Table and a person who is responsible for it. GIDEON can explain how "
    "a guideline applies and cite the authorities — ask it that way."
)
SENTENCE_CREDIT_REFUSAL = (
    "GIDEON does not compute or confirm sentence credit, good time, or a release date. "
    "When a person will actually be released turns on facts and judgments a language model does not have — "
    "the judgment as entered, every day of prior custody and what it was already credited against, "
    "the conduct and the programming the Bureau of Prisons will credit — and a date given wrong reaches a client as a promise. "
    "Get the figure from the Bureau of Prisons' sentence computation and a person who is responsible for it. "
    "GIDEON can explain how good conduct time, earned time credits, prior custody credit, and supervised release work and cite the authorities — ask it that way."
)
ERROR_PATTERN_ID = "guardrail/error@1"


@dataclass(frozen=True, slots=True)
class Pattern:
    """One versioned, bounded detector in a guardrail family."""

    pattern_id: str
    regex: re.Pattern[str]
    needs_context: bool = False
    exclusion: re.Pattern[str] | None = None
    yields_to: tuple[str, ...] = ()
    anchor: tuple[str, ...] = ()
    # The source with each rate look-ahead made an optional positive, compiled
    # for prefix mode's release constraints alone and never judged for a trip.
    rate_form: re.Pattern[str] | None = None


@dataclass(frozen=True, slots=True)
class Family:
    """One guardrail family and the context needed to judge its detectors."""

    name: str
    refusal: str
    patterns: tuple[Pattern, ...]
    pattern_set_version: int
    figure_form: re.Pattern[str]
    # The figure that, leading a match, marks the assertion shape ("June 5,
    # 2027 is the deadline"; "57–71 months for a level 25, category I
    # defendant"), exempt only through a refusal's that-clause.
    reverse_form: re.Pattern[str]
    figures: Callable[[str], frozenset[str]]
    figure_nouns: tuple[str, ...]
    constructions: tuple[
        re.Pattern[str], re.Pattern[str], re.Pattern[str], re.Pattern[str]
    ]
    # The confirmation context over the user's message and the pattern id the
    # answer's opening affirmation trips under it; None for a family with no
    # confirmation shape.
    confirmation: tuple[re.Pattern[str], str] | None
    confirmation_displaces: tuple[str, ...] = ()
    # A family's own further restatement construction, or None: a form whose
    # ``echo_figure`` group, lying inside a match every figure of which the
    # user supplied, exempts it (the sentence-credit echo or deadline
    # whether-count).
    echo_form: re.Pattern[str] | None = None
    # The attribution form serves the Guidelines and sentence-credit families;
    # the deadline family deliberately leaves this slot empty.
    attribution_form: re.Pattern[str] | None = None


@dataclass(frozen=True, slots=True)
class Trip:
    """The family and detector that caused a replacement."""

    family: str
    pattern_id: str



def _compiled(
    pattern_id: str,
    source: str,
    *,
    needs_context: bool = False,
    exclusion: str | None = None,
    yields_to: tuple[str, ...] = (),
    anchor: tuple[str, ...] = (),
) -> Pattern:
    rate_form = (
        re.compile(source.replace(RATE_LOOKAHEAD, RATE_OPTIONAL), re.IGNORECASE)
        if RATE_LOOKAHEAD in source
        else None
    )
    return Pattern(
        pattern_id,
        re.compile(source, re.IGNORECASE),
        needs_context,
        re.compile(exclusion, re.IGNORECASE) if exclusion is not None else None,
        yields_to,
        anchor,
        rate_form,
    )


DATE_NEAR_DEADLINE = _compiled(
    "deadline/date-near-deadline-word@1",
    DATE_NEAR_DEADLINE_WORD,
    exclusion=EXPIRY_EXCLUSION_SOURCE,
)
DAYS_REMAINING_PATTERN = _compiled(
    "deadline/days-remaining@1", DAYS_REMAINING, exclusion=DAYS_EXCLUSION_SOURCE
)
DATE_CONFIRMED_PATTERN = _compiled(
    "deadline/date-confirmed@1",
    DATE_CONFIRMED,
    yields_to=("sentence-credit",),
)
DAYS_ELAPSED_PATTERN = _compiled(
    "deadline/days-elapsed@1",
    DAYS_ELAPSED,
    exclusion=ELAPSED_EXCLUSION_SOURCE,
)
GUIDELINES_LEVEL_AND_CATEGORY_TO_RANGE_PATTERN = _compiled(
    "guidelines/level-and-category-to-range@1", GUIDELINES_LEVEL_AND_CATEGORY_TO_RANGE
)
GUIDELINES_RANGE_ASSERTED_PATTERN = _compiled(
    "guidelines/range-asserted@1", GUIDELINES_RANGE_ASSERTED
)
GUIDELINES_LEVEL_TOTAL_PATTERN = _compiled(
    "guidelines/level-total@1",
    GUIDELINES_LEVEL_TOTAL,
    anchor=("result",),
)
GUIDELINES_POINTS_TO_CATEGORY_PATTERN = _compiled(
    "guidelines/points-to-category@1",
    GUIDELINES_POINTS_TO_CATEGORY,
    exclusion=GUIDELINES_POINT_LIST_EXCLUSION_SOURCE,
    anchor=("category_after", "category_before"),
)
SENTENCE_CREDIT_RELEASE_DATE_PATTERN = _compiled(
    "sentence-credit/release-date@1", RELEASE_DATE_PATTERN_SOURCE
)
SENTENCE_CREDIT_CREDIT_COUNT_PATTERN = _compiled(
    "sentence-credit/credit-count@1",
    CREDIT_COUNT_PATTERN_SOURCE,
    exclusion=CAP_EXCLUSION_SOURCE,
)
SENTENCE_CREDIT_TIME_TO_SERVE_PATTERN = _compiled(
    "sentence-credit/time-to-serve@1", TIME_TO_SERVE_PATTERN_SOURCE
)

# The confirmation context: the user supplied a date and asked whether it is
# right, and the answer opens with an affirmation ("Yes, that's right").
CONFIRMATION_ASK = (
    r"(?:"
    + rf"\b(?:is|am{SP}i|did{SP}i|have{SP}i|was|are)\b[\s\S]{{0,60}}\b(?:correct|right|accurate)\b"
    + r"|\b(?:correct|right|accurate)\s{0,3}\?"
    + r"|\bconfirm(?:ed|ation|s)?\b"
    + rf"|\bis{SP}(?:my|the|this|that|our){SP}(?:filing{SP})?(?:deadline|due{SP}date)\b"
    + rf"|\bis{SP}(?:it|this|that){SP}due\b"
    + rf"|\bmy{SP}(?:filing{SP})?(?:deadline|due{SP}date){SP}(?:is|would{SP}be)\b"
    + r")"
)
CONFIRMATION_CONTEXT = re.compile(
    rf"(?:{DATE}[\s\S]{{0,120}}{CONFIRMATION_ASK}|{CONFIRMATION_ASK}[\s\S]{{0,120}}{DATE})",
    re.IGNORECASE,
)
# The Guidelines confirmation context reuses the ask vocabulary above with
# its deadline-specific alternatives replaced by range, level, and category
# targets, including "right?" or "correct?" directly after a figure.
GUIDELINES_CONFIRMATION_TARGETS = (
    "range",
    "guideline range",
    "guidelines range",
    "advisory range",
    "offense level",
    "category",
)
GUIDELINES_CONFIRMATION_ASK = (
    r"(?:"
    + rf"\b(?:is|am{SP}i|did{SP}i|have{SP}i|was|are)\b[\s\S]{{0,60}}\b(?:correct|right|accurate)\b"
    + r"|\b(?:correct|right|accurate)\s{0,3}\?"
    + r"|\bconfirm(?:ed|ation|s)?\b"
    + rf"|\bis{SP}(?:my|the|this|that|our){SP}(?:(?:guideline|advisory|applicable|sentencing){SP})?range\b"
    + rf"|\bis{SP}that{SP}(?:(?:right|correct){SP})?range\b"
    + rf"|(?<![\w/]){GUIDELINES_RANGE_OR_PAIR_SOURCE}(?![\w/])\s{{0,3}}\b(?:right|correct)\b\s{{0,3}}\?"
    + r")"
)
GUIDELINES_CONFIRMATION_CONTEXT = re.compile(
    rf"(?:{GUIDELINES_RANGE_OR_PAIR_SOURCE}[\s\S]{{0,120}}{GUIDELINES_CONFIRMATION_ASK}"
    rf"|{GUIDELINES_CONFIRMATION_ASK}[\s\S]{{0,120}}{GUIDELINES_RANGE_OR_PAIR_SOURCE})",
    re.IGNORECASE,
)
# The in-text confirmation shape is gated on that context: Guidelines
# doctrine opens with "Yes" beside "category" routinely — a career offender
# is category VI, Zone A permits probation — so it runs only under a figure
# plus an ask. A pair given for context beside a doctrine question carries no
# ask and passes; the deadline family's in-text shape is unconditional.
GUIDELINES_RANGE_CONFIRMED = (
    r"(?:"
    + rf"{SENTENCE_START}{AFFIRMATION}\b[,.!:;—–-]?[\s\S]{{0,60}}\b{_phrases(GUIDELINES_CONFIRMATION_TARGETS)}\b"
    + rf"|{SENTENCE_START}{UNNEGATED_PREFIX}\b{_phrases(GUIDELINES_CONFIRMATION_TARGETS)}\b[^.!?\n]{{0,30}}\b(?:is|are|looks?|seems?|was){SP}(?:also{SP}|indeed{SP})?(?:correct|right|accurate|fine)\b"
    + r")"
)
GUIDELINES_RANGE_CONFIRMED_PATTERN = _compiled(
    "guidelines/range-confirmed@1",
    GUIDELINES_RANGE_CONFIRMED,
    needs_context=True,
)
AFFIRMATION_OPEN = re.compile(rf"^\s{{0,20}}{AFFIRMATION}\b", re.IGNORECASE)
SENTENCE_CREDIT_GENERIC_CONFIRMATION_ASK = (
    r"(?:"
    + rf"\b(?:is|am{SP}i|did{SP}i|have{SP}i|was|are)\b[\s\S]{{0,60}}\b(?:correct|right|accurate)\b"
    + r"|\b(?:correct|right|accurate)\s{0,3}\?"
    + r"|\bconfirm(?:ed|ation|s)?\b"
    + r")"
)
SENTENCE_CREDIT_FAMILY_SPECIFIC_ASK = (
    rf"(?:\bis{SP}(?:his|her|their|the|my{SP}client{APOSTROPHE}s){SP}"
    rf"(?:(?:projected|anticipated|expected){SP})?"
    rf"{_phrases(SENTENCE_CREDIT_CONFIRMATION_FAMILY_NOUNS)}\b"
    rf"|\bis{SP}that{SP}(?:his|the){SP}(?:release|out){SP}date\b"
    rf"|\bdoes{SP}he{SP}get{SP}out(?:{SP}(?:on|in))?\b)"
)
SENTENCE_CREDIT_CONFIRMATION_ASK = (
    rf"(?:{SENTENCE_CREDIT_GENERIC_CONFIRMATION_ASK}|"
    rf"{SENTENCE_CREDIT_FAMILY_SPECIFIC_ASK})"
)
SENTENCE_CREDIT_CONFIRMATION_REACH_CHARS = 120
SENTENCE_CREDIT_CONFIRMATION_CONTEXT = re.compile(
    rf"(?:"
    rf"(?=[\s\S]{{0,{SENTENCE_CREDIT_CONFIRMATION_REACH_CHARS}}}{SENTENCE_FIGURE}"
    rf"[\s\S]{{0,{SENTENCE_CREDIT_CONFIRMATION_REACH_CHARS}}}{SENTENCE_CREDIT_GENERIC_CONFIRMATION_ASK})"
    rf"(?=[\s\S]{{0,{SENTENCE_CREDIT_CONFIRMATION_REACH_CHARS}}}\b"
    rf"{_phrases(SENTENCE_CREDIT_CONFIRMATION_NOUNS)}\b)[\s\S]{{0,{SENTENCE_CREDIT_CONFIRMATION_REACH_CHARS}}}"
    rf"|(?=[\s\S]{{0,{SENTENCE_CREDIT_CONFIRMATION_REACH_CHARS}}}{SENTENCE_CREDIT_GENERIC_CONFIRMATION_ASK}"
    rf"[\s\S]{{0,{SENTENCE_CREDIT_CONFIRMATION_REACH_CHARS}}}{SENTENCE_FIGURE})"
    rf"(?=[\s\S]{{0,{SENTENCE_CREDIT_CONFIRMATION_REACH_CHARS}}}\b"
    rf"{_phrases(SENTENCE_CREDIT_CONFIRMATION_NOUNS)}\b)[\s\S]{{0,{SENTENCE_CREDIT_CONFIRMATION_REACH_CHARS}}}"
    rf"|(?=[\s\S]{{0,{SENTENCE_CREDIT_CONFIRMATION_REACH_CHARS}}}{SENTENCE_FIGURE}"
    rf"[\s\S]{{0,{SENTENCE_CREDIT_CONFIRMATION_REACH_CHARS}}}{SENTENCE_CREDIT_FAMILY_SPECIFIC_ASK})"
    rf"[\s\S]{{0,{SENTENCE_CREDIT_CONFIRMATION_REACH_CHARS}}}"
    rf"|(?=[\s\S]{{0,{SENTENCE_CREDIT_CONFIRMATION_REACH_CHARS}}}{SENTENCE_CREDIT_FAMILY_SPECIFIC_ASK}"
    rf"[\s\S]{{0,{SENTENCE_CREDIT_CONFIRMATION_REACH_CHARS}}}{SENTENCE_FIGURE})"
    rf"[\s\S]{{0,{SENTENCE_CREDIT_CONFIRMATION_REACH_CHARS}}}"
    rf")",
    re.IGNORECASE,
)
SENTENCE_CREDIT_CONFIRMED = (
    r"(?:"
    + rf"{SENTENCE_START}{AFFIRMATION}\b[,.!:;—–-]?[\s\S]{{0,60}}\b{_phrases(SENTENCE_CREDIT_CONFIRMATION_TARGETS)}\b"
    + rf"|{SENTENCE_START}{UNNEGATED_PREFIX}\b{_phrases(SENTENCE_CREDIT_CONFIRMATION_TARGETS)}\b[^.!?\n]{{0,30}}\b(?:is|are|looks?|seems?){SP}(?:also{SP}|indeed{SP})?(?:correct|right|accurate|fine)\b"
    + r")"
)
SENTENCE_CREDIT_CONFIRMED_PATTERN = _compiled(
    "sentence-credit/release-date-confirmed@1",
    SENTENCE_CREDIT_CONFIRMED,
    needs_context=True,
)


MONTH_NUMBERS = {
    name[:3].lower(): f"{number:02d}"
    for number, name in enumerate(MONTH_NAMES, start=1)
}
_DATE_PARTS = (
    re.compile(
        rf"(?P<month>{MONTH}){SP}(?P<day>\d{{1,2}}){ORDINAL},?{SP}(?P<year>\d{{4}})",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<day>\d{{1,2}}){ORDINAL}{SP}(?:of{SP})?"
        rf"(?P<month>{MONTH}),?{SP}(?P<year>\d{{4}})",
        re.IGNORECASE,
    ),
    re.compile(r"(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{2,4})"),
    re.compile(r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"),
)


def _normalised_date(found: str) -> str:
    for pattern in _DATE_PARTS:
        match = pattern.fullmatch(found)
        if match is None:
            continue
        parts = match.groupdict()
        month = parts["month"]
        month_number = MONTH_NUMBERS.get(month[:3].lower(), month)
        year = parts["year"]
        if len(year) == 2:
            year = f"20{year}"
        return f"{year}-{int(month_number):02d}-{int(parts['day']):02d}"
    return found


def normalized_dates(text: str) -> frozenset[str]:
    return frozenset(_normalised_date(found) for found in DATE_FORM.findall(text))


DEADLINE_COUNT_FORM = re.compile(
    rf"{_deadline_count(DEADLINE_COUNT_SEPARATOR, named=True)}(?![\w/])", re.IGNORECASE
)


MONTH_AND_YEAR_PART = re.compile(
    rf"(?P<month>{MONTH}){SP}(?P<year>\d{{4}})", re.IGNORECASE
)
SEASON_AND_YEAR_PART = re.compile(
    rf"(?:the{SP})?(?P<season>{_phrases(SEASON_WORDS)})"
    rf"(?:(?:{SP}of)?{SP}(?P<year>\d{{4}})|-(?P<hyphen_year>\d{{4}}))",
    re.IGNORECASE,
)


def _normalised_release_date(found: str) -> str:
    if DATE_FORM.fullmatch(found) is not None:
        return _normalised_date(found)
    month_year = MONTH_AND_YEAR_PART.fullmatch(found)
    if month_year is not None:
        parts = month_year.groupdict()
        month = MONTH_NUMBERS.get(parts["month"][:3].lower(), parts["month"])
        return f"{parts['year']}-{int(month):02d}"
    season_year = SEASON_AND_YEAR_PART.fullmatch(found)
    if season_year is not None:
        parts = season_year.groupdict()
        year = parts["year"] or parts["hyphen_year"]
        return f"{year}-{parts['season'].lower()}"
    return found


# The count's canonical number: a number word or a hyphenated compound maps to
# its digits by this table (the units, the teens, then the tens), so the user's
# "eight years" and the model's "8 years" share one key.
NUMBER_WORD_VALUES = dict(
    zip(
        NUMBER_WORDS,
        [*map(str, range(1, 21)), *map(str, range(30, 100, 10))],
        strict=True,
    )
)
NUMBER_WORD_VALUES.update(
    {
        f"{tens}-{unit}": f"{NUMBER_WORD_VALUES[tens][0]}{NUMBER_WORD_VALUES[unit]}"
        for tens in NUMBER_WORDS[19:]
        for unit in NUMBER_WORDS[:9]
    }
)


def _normalised_count(match: re.Match[str]) -> str:
    number = match.group("number")
    canonical_number = NUMBER_WORD_VALUES.get(number.lower(), number)
    canonical_number = canonical_number.removesuffix(".0")
    return f"{canonical_number}-{match.group('unit').lower().rstrip('s')}"


def normalized_sentence_figures(text: str) -> frozenset[str]:
    dates = {
        _normalised_release_date(match.group(0))
        for match in RELEASE_DATE_FORM.finditer(text)
    }
    counts = {
        _normalised_count(match)
        for match in SENTENCE_COUNT_FORM.finditer(text)
    }
    return frozenset(dates | counts)


def normalized_deadline_figures(text: str) -> frozenset[str]:
    counts = {
        _normalised_count(match) for match in DEADLINE_COUNT_FORM.finditer(text)
    }
    return frozenset(normalized_dates(text) | counts)


_GUIDELINES_LEVEL_VALUE = re.compile(
    r"\b(?:level|OL|TOL)\s{0,3}(?:of\s{0,3})?(?P<value>\d{1,2})\b", re.IGNORECASE
)
_GUIDELINES_CATEGORY_VALUE = re.compile(
    r"\b(?:category|CHC|cat\.?)\s{0,3}(?:of\s{0,3})?(?P<value>VI|IV|V|III|II|I|[1-6])\b",
    re.IGNORECASE,
)
_GUIDELINES_CATEGORY_ROMANS = {
    "1": "I",
    "2": "II",
    "3": "III",
    "4": "IV",
    "5": "V",
    "6": "VI",
}
_GUIDELINES_POINT_VALUE = re.compile(
    rf"{MARK}(?P<value>\d{{1,2}}|{_phrases(NUMBER_COMPOUNDS)}|"
    rf"{_phrases(NUMBER_WORDS)})(?:{SP}criminal{SP}history)?"
    rf"{SP}points?{MARK}",
    re.IGNORECASE,
)


def _category_roman(category: re.Match[str]) -> str:
    value = category.group("value").upper()
    return _GUIDELINES_CATEGORY_ROMANS.get(value, value)


def _normalised_guidelines_figure(found: str) -> set[str]:
    """The keys of one written figure: a pair keys its two components beside itself."""

    if GUIDELINES_RANGE_FORM.fullmatch(found) is not None:
        numbers = re.findall(r"\d{1,3}", found)
        if "life" in found.lower():
            return {f"{int(numbers[0])}-life"}
        return {f"{int(numbers[0])}-{int(numbers[1])}"}
    shorthand = re.fullmatch(
        rf"\s*(\d{{1,2}}){GUIDELINES_OPTIONAL_SPACE}/{GUIDELINES_OPTIONAL_SPACE}"
        rf"(VI|IV|V|III|II|I)\s*",
        found,
        re.IGNORECASE,
    )
    if shorthand is not None:
        level_value, category_roman = int(shorthand.group(1)), shorthand.group(2).upper()
        return {
            f"{level_value}/{category_roman}",
            f"level-{level_value}",
            f"category-{category_roman}",
        }
    stated_total = GUIDELINES_STATED_TOTAL_FORM.fullmatch(found)
    if stated_total is not None:
        return {f"level-{int(stated_total.group('result'))}"}
    level = _GUIDELINES_LEVEL_VALUE.search(found)
    category = _GUIDELINES_CATEGORY_VALUE.search(found)
    if level is not None and category is not None:
        level_value, category_roman = int(level.group("value")), _category_roman(category)
        return {
            f"{level_value}/{category_roman}",
            f"level-{level_value}",
            f"category-{category_roman}",
        }
    if level is not None:
        return {f"level-{int(level.group('value'))}"}
    if category is not None:
        return {f"category-{_category_roman(category)}"}
    point_count = _GUIDELINES_POINT_VALUE.fullmatch(found)
    if point_count is not None:
        number = point_count.group("value")
        canonical_number = NUMBER_WORD_VALUES.get(number.lower(), number)
        return {f"{canonical_number}-point"}
    return {found}


def normalized_figures(text: str) -> frozenset[str]:
    figures: set[str] = set()
    for match in GUIDELINES_FIGURE.finditer(text):
        figures.update(_normalised_guidelines_figure(match.group(0)))
    figures.update(
        f"level-{int(match.group('result'))}"
        for match in GUIDELINES_CHAINED_TOTAL_FORM.finditer(text)
    )
    return frozenset(figures)


QUESTION_FORM, REFUSAL_FORM, THAT_CLAUSE, FROM_DATE_FORM = _build_constructions(
    DEADLINE_FIGURE, DEADLINE_FIGURE_NOUNS
)

DEADLINE_FAMILY = Family(
    name="deadline",
    refusal=DEADLINE_REFUSAL,
    patterns=(
        DATE_NEAR_DEADLINE,
        DAYS_REMAINING_PATTERN,
        DATE_CONFIRMED_PATTERN,
        DAYS_ELAPSED_PATTERN,
    ),
    pattern_set_version=2,
    figure_form=DEADLINE_FIGURE_FORM,
    reverse_form=DATE_FORM,
    figures=normalized_deadline_figures,
    figure_nouns=DEADLINE_FIGURE_NOUNS,
    constructions=(QUESTION_FORM, REFUSAL_FORM, THAT_CLAUSE, FROM_DATE_FORM),
    confirmation=(CONFIRMATION_CONTEXT, "deadline/date-confirmed@1"),
    echo_form=DEADLINE_COUNT_QUESTION_FORM,
)
GUIDELINES_QUESTION_FORM, GUIDELINES_REFUSAL_FORM, GUIDELINES_THAT_CLAUSE, GUIDELINES_FROM_DATE_FORM = _build_constructions(
    GUIDELINES_FIGURE_SOURCE, GUIDELINES_FIGURE_NOUNS
)
GUIDELINES_FAMILY = Family(
    name="guidelines",
    refusal=GUIDELINES_REFUSAL,
    patterns=(
        GUIDELINES_LEVEL_AND_CATEGORY_TO_RANGE_PATTERN,
        GUIDELINES_RANGE_ASSERTED_PATTERN,
        GUIDELINES_RANGE_CONFIRMED_PATTERN,
        GUIDELINES_LEVEL_TOTAL_PATTERN,
        GUIDELINES_POINTS_TO_CATEGORY_PATTERN,
    ),
    pattern_set_version=2,
    figure_form=GUIDELINES_FIGURE,
    reverse_form=GUIDELINES_REVERSE_FORM,
    figures=normalized_figures,
    figure_nouns=GUIDELINES_FIGURE_NOUNS,
    constructions=(
        GUIDELINES_QUESTION_FORM,
        GUIDELINES_REFUSAL_FORM,
        GUIDELINES_THAT_CLAUSE,
        GUIDELINES_FROM_DATE_FORM,
    ),
    confirmation=(GUIDELINES_CONFIRMATION_CONTEXT, "guidelines/range-confirmed@1"),
    attribution_form=ATTRIBUTION_FORM,
)
SENTENCE_CREDIT_QUESTION_FORM, SENTENCE_CREDIT_REFUSAL_FORM, SENTENCE_CREDIT_THAT_CLAUSE, SENTENCE_CREDIT_FROM_DATE_FORM = _build_constructions(
    SENTENCE_FIGURE, SENTENCE_FIGURE_NOUNS
)
SENTENCE_CREDIT_FAMILY = Family(
    name="sentence-credit",
    refusal=SENTENCE_CREDIT_REFUSAL,
    patterns=(
        SENTENCE_CREDIT_RELEASE_DATE_PATTERN,
        SENTENCE_CREDIT_CREDIT_COUNT_PATTERN,
        SENTENCE_CREDIT_TIME_TO_SERVE_PATTERN,
        SENTENCE_CREDIT_CONFIRMED_PATTERN,
    ),
    pattern_set_version=1,
    figure_form=SENTENCE_FIGURE_FORM,
    reverse_form=RELEASE_DATE_FORM,
    figures=normalized_sentence_figures,
    figure_nouns=SENTENCE_FIGURE_NOUNS,
    constructions=(
        SENTENCE_CREDIT_QUESTION_FORM,
        SENTENCE_CREDIT_REFUSAL_FORM,
        SENTENCE_CREDIT_THAT_CLAUSE,
        SENTENCE_CREDIT_FROM_DATE_FORM,
    ),
    confirmation=(
        SENTENCE_CREDIT_CONFIRMATION_CONTEXT,
        "sentence-credit/release-date-confirmed@1",
    ),
    confirmation_displaces=("deadline",),
    echo_form=SENTENCE_CREDIT_ECHO_FORM,
    attribution_form=ATTRIBUTION_FORM,
)
FAMILIES = (DEADLINE_FAMILY, GUIDELINES_FAMILY, SENTENCE_CREDIT_FAMILY)
# Every family's refusal in family order — the one export the tools read to
# recognise a replacement — and the lookup a trip's family selects its
# refusal by; the error path and a stateless stream keep the deadline refusal.
REFUSALS = tuple(family.refusal for family in FAMILIES)
REFUSAL_BY_FAMILY = {family.name: family.refusal for family in FAMILIES}


def _refusal_for(family_name: object) -> str:
    if isinstance(family_name, str):
        return REFUSAL_BY_FAMILY.get(family_name, DEADLINE_REFUSAL)
    return DEADLINE_REFUSAL
