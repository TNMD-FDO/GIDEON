"""The guardrail's grammar: the match, gap, and lag bounds, the reasoning
placeholder and refusal separator, each family's vocabulary, and every bounded
pattern source and construction builder the families compile.

Standard library only; ``families``, ``judge``, and ``window`` import from it.
"""

import re

MAX_MATCH_CHARS = 150
MAX_GAP_CHARS = 80
# The restatement look-ahead settles the exemption after a family match.
RESTATEMENT_LOOKAHEAD_CHARS = 40
# Exclusion context is bounded by both the exemption look-behind and the
# restatement look-ahead so a rejected overlap is visible to stream judgement.
EXCLUSION_REACH_CHARS = 40
# The stream lag is the family match bound plus its restatement look-ahead.
LAG_CHARS = MAX_MATCH_CHARS + RESTATEMENT_LOOKAHEAD_CHARS
# The one reasoning value the hook ever releases, in the turn's first
# reasoning delta: a truthy delta opens the frontend's reasoning item (its
# "Thinking…" timer, closed with a duration by the first answer text), and a
# space leaves the item's body empty on screen and in the stored message.
REASONING_PLACEHOLDER = " "
# The in-stream refusal follows any answer text already released.
REFUSAL_SEPARATOR = "\n\n"

# The deadline family's vocabulary — starting values from greenfield ticket 07
# item 8, tuned on eval/seed/guardrails/deadline-trap.yaml and judged by the
# deadline-trap gate (spec §18.3).  A "lead" precedes a date in a computed
# answer ("the deadline is June 5, 2027"); "within" and "after" are absent on
# purpose, because they carry the doctrinal restatement of a period ("must be
# filed within 14 days after entry of judgment"), which is answered freely.
DEADLINE_LEADS = (
    "deadline",
    "due date",
    "last day",
    "no later than",
    "on or before",
    "expires",
    "expire",
    "runs out",
    "run out",
    "have until",
    "has until",
    "too late",
    "cutoff",
    "time limit",
)
# Leads that are computation only when the date follows almost immediately:
# "due by June 5, 2027", "filed by June 5, 2027" — but not "due process", "due
# diligence", or "filed by counsel on June 5, 2027".
SHORT_DEADLINE_LEADS = (
    "due",
    "must be filed by",
    "be filed by",
    "filed by",
    "file by",
)
DUE_EXCLUSIONS = ("process", "diligence", "course", "regard", "to")
# The reverse form names the date first and needs a linking verb ("June 5,
# 2027 is the deadline"); a symmetric window would also trip a doctrinal
# answer that mentions a date and, a clause later, the word deadline ("what
# happened on March 3, 2025, is for the person responsible for the deadline"),
# which the seed's controls hold above the ceiling.
DEADLINE_NOUNS = ("deadline", "due date", "last day", "cutoff", "due")
# The nouns a restatement construction names a date by: a question form's
# predicate ("whether June 5, 2027 is the deadline") and the "if the <noun>
# of <date>" slot.
DEADLINE_FIGURE_NOUNS = (
    "deadline",
    "due date",
    "date",
    "last day",
    "filing deadline",
    "count",
    "day count",
)

# The sentence-credit family draws the line at a present or conditional release,
# credit, or served-time computation.  Tense and voice keep past authority
# descriptions and doctrinal explanations out; rates are excluded after the
# count, caps by an overlapping exclusion, and user figures by the four
# restatement constructions.  Confirmation is limited to a supplied family
# figure beside a family ask, with the in-text affirmation carrying that
# context.
RELEASE_DATE_NOUNS = (
    "release date",
    "full-term date",
    "full term date",
    "out date",
    "out-date",
    "outdate",
    "date of release",
    "home confinement date",
    "home detention date",
    "halfway house date",
    "RRC date",
    "RRC placement date",
    "prerelease custody date",
)
RELEASE_DATE_MODIFIERS = (
    "projected",
    "anticipated",
    "expected",
    "estimated",
    "probable",
    "good conduct",
    "GCT",
    "statutory",
)
RELEASE_VERBS = (
    "be released",
    "get out",
    "be out",
    "walk out",
    "go home",
)
RELEASE_AUXILIARIES = (
    "will",
    "would",
    "should",
    "'ll",
    "is going to",
    "is expected to",
    "is likely to",
    "is set to",
    "is scheduled to",
)
EXPIRY_SUBJECTS = (
    "sentence",
    "term",
    "supervision",
    "supervised release",
    "probation",
    "custody",
    "imprisonment",
    "confinement",
)
EXPIRY_VERBS = (
    "expires",
    "will expire",
    "would expire",
    "should expire",
    "is set to expire",
    "is due to expire",
    "ends",
    "will end",
    "would end",
    "should end",
    "is up",
    "runs out",
    "will run out",
    "would run out",
    "should run out",
    "runs through",
    "runs until",
    "runs to",
    "terminates",
    "will terminate",
)
ELIGIBILITY_PLACES = (
    "home confinement",
    "home detention",
    "halfway house",
    "RRC",
    "RRC placement",
    "residential reentry",
    "prerelease custody",
    "community confinement",
    "release",
    "parole",
    "transfer",
)
RELEASE_LINKS = (
    "is",
    "would be",
    "will be",
    "should be",
    "falls in",
    "falls on",
    "would fall in",
    "would fall on",
    "will fall in",
    "will fall on",
    "lands in",
    "lands on",
    "moves to",
    "moves up to",
    "comes in",
    "comes on",
    "is set for",
    "is projected for",
    "is scheduled for",
    "of",
)
RELEASE_QUALIFIERS = (
    "about",
    "around",
    "roughly",
    "approximately",
    "likely",
    "now",
    "then",
    "therefore",
)
CREDIT_NOUNS = (
    "good time",
    "good conduct time",
    "good-time credit",
    "GCT",
    "earned time credit",
    "earned time credits",
    "ETC",
    "ETCs",
    "FSA credit",
    "FSA credits",
    "FSA time credit",
    "FSA time credits",
    "First Step Act credit",
    "First Step Act credits",
    "time credit",
    "time credits",
    "jail credit",
    "jail-time credit",
    "prior custody credit",
    "presentence credit",
    "pretrial credit",
    "credit for time served",
    "§ 3585(b) credit",
    "sentence credit",
    "credit",
)
EARNING_VERBS = (
    "earns",
    "will earn",
    "would earn",
    "has earned",
    "accrues",
    "will accrue",
    "has accrued",
    "is credited with",
    "will be credited with",
    "gets",
    "will get",
    "receives",
    "will receive",
    "is entitled to",
    "should receive",
)
REDUCTION_VERBS = (
    "reduces",
    "reduce",
    "would reduce",
    "will reduce",
    "reducing",
    "cuts",
    "cut",
    "would cut",
    "will cut",
    "shortens",
    "shorten",
    "would shorten",
    "will shorten",
    "takes",
    "take",
    "shaves",
    "shave",
    "knocks",
    "knock",
    "brings forward by",
    "moves up by",
    "advances by",
)
CREDIT_REMAINING_WORDS = ("remain", "remains", "remaining", "left", "to go", "to serve")
RATE_WORDS = (
    "per",
    "for each",
    "for every",
    "a year",
    "each year",
    "every year",
    "of each",
    "annually",
)
CAP_WORDS = (
    "up to",
    "not more than",
    "no more than",
    "at most",
    "a maximum of",
    "maximum of",
    "capped at",
    "as much as",
    "as many as",
    "not to exceed",
    "limited to",
)
SEASON_WORDS = ("spring", "summer", "fall", "autumn", "winter", "early", "mid", "late")
SENTENCE_FIGURE_NOUNS = (
    "release date",
    "out date",
    "date",
    "good time",
    "credit",
    "credits",
    "figure",
    "count",
    "number",
    "time",
)
SERVED_AUXILIARIES = (
    "would",
    "will",
    "'ll",
    "should",
    "is going to",
    "is likely to",
    "is expected to",
    "can expect to",
    "would actually",
    "will actually",
)
SERVED_WORD_AUXILIARIES = tuple(
    auxiliary for auxiliary in SERVED_AUXILIARIES if auxiliary != "'ll"
)
SERVED_ACTIONS = (
    "serve",
    "do",
    "spend",
    "be in custody for",
    "remain in custody for",
    "sit for",
)
SERVED_MONTH_UNITS = ("months", "month")
SERVED_QUALIFIERS = (
    "about",
    "roughly",
    "approximately",
    "around",
    "close to",
    "just over",
    "just under",
    "nearly",
)
ACTUAL_TIME_WORDS = ("actual", "real", "effective")
ACTUAL_TIME_NOUNS = ("time", "time served", "time in custody", "term")
ACTUAL_TIME_LINKS = (
    "is",
    "would be",
    "will be",
    "comes to",
    "works out to",
    "of",
)
PERCENTAGE_RESOLUTION_LINKS = (
    "roughly",
    "about",
    "or",
    "which is",
    "comes to",
    "is",
    "equals",
    "works out to",
)
PERCENTAGE_RESOLUTION_LINKS_WITHOUT_OR = tuple(
    link for link in PERCENTAGE_RESOLUTION_LINKS if link != "or"
)
PERCENTAGE_SUBJECT_WORDS = ("sentence", "term")
SENTENCE_CREDIT_CONFIRMATION_NOUNS = (
    "release date",
    "out date",
    "full-term date",
    "good time",
    "good conduct time",
    "credit",
    "credits",
    "time served",
    "serve",
    "home confinement",
    "halfway house",
    "RRC",
    "release",
    "released",
    "gets out",
    "get out",
    "getting out",
)
SENTENCE_CREDIT_CONFIRMATION_TARGETS = (
    "release date",
    "out date",
    "full-term date",
    "good time",
    "good conduct time",
    "credit",
    "credits",
    "time served",
    "date",
    "figure",
    "computation",
    "calculation",
    "number",
)
SENTENCE_CREDIT_CONFIRMATION_FAMILY_NOUNS = (
    "release date",
    "out date",
    "full-term date",
    "good time",
    "credits",
)

# The Guidelines family draws the line at a range or total the model resolved or
# placed on the matter. A range restated under a governing construction, a
# court's past-tense finding, a statute's years, or a guideline's own term range
# passes; a bare months range trips nothing. An illustrative table cell is a
# positive at the cost of the example; a court's range in a case description
# passes by the attribution construction; the user's own figures are handled
# by the restatement constructions. Total and point
# resolution require a resolving link, while thresholds and category
# definitions have no such link. "was" and "were" are never links or leads.
# An attributed figure is governed by an authority subject followed directly
# by its finding or recitation verb: actors are past-tense only, documents may
# use past or present tense. A modal, actor present tense, new sentence,
# coordinated verb, or comma breaks that attribution.
GUIDELINES_LEVEL_PREFIXES = ("total", "adjusted", "final", "base")
# U+2011 NON-BREAKING HYPHEN and U+202F NARROW NO-BREAK SPACE — the characters
# HARV-011 wrote — are written as regex escapes below; U+00A0 NO-BREAK SPACE is
# also admitted before the unit.
GUIDELINES_RANGE_SEPARATORS = (r"-", r"\u2013", r"\u2014", r"\u2011")
GUIDELINES_RANGE_WORDS = ("to",)
GUIDELINES_RANGE_UNITS = ("months", "month", "mos", "mo")
GUIDELINES_RESOLVING_LINKS = (
    "is",
    "yields",
    "would yield",
    "gives",
    "would give",
    "produces",
    "results in",
    "comes to",
    "calls for",
    "would be",
    "equals",
    "of",
)
GUIDELINES_MATTER_MODIFIERS = (
    "advisory",
    "guideline",
    "guidelines",
    "applicable",
    "sentencing",
)
GUIDELINES_MATTER_LEADS = ("faces", "is looking at", "is exposed to", "would face", "is facing")
GUIDELINES_RANGE_LINKS = ("is", "would be", "comes to", "falls at", "of")
GUIDELINES_TOTAL_WORDS = ("total", "adjusted", "final", "combined", "resulting")
# Links that resolve a total level; "was" and "were" stay out so past-tense
# authority descriptions remain outside this pattern.
GUIDELINES_LEVEL_LINKS = (
    "of",
    "is",
    "would be",
    "would then be",
    "comes to",
    "equals",
    "totals",
    "becomes",
    "at",
)
GUIDELINES_LINK_ADVERBS = ("therefore", "then", "now", "thus")
# Comparative words after a resolving link make a threshold, not a result.
GUIDELINES_COMPARATIVES = (
    "less than",
    "more than",
    "greater than",
    "at least",
    "at most",
    "below",
    "above",
    "under",
    "over",
)
# Comparative words after the result number likewise describe a rule.
GUIDELINES_TRAILING_COMPARATIVES = (
    "or greater",
    "or higher",
    "or more",
    "or less",
    "or lower",
    "or above",
    "or below",
)
# Units after a bare "for a total of N" make N a count of things, not a level.
GUIDELINES_UNIT_WORDS = (
    "month",
    "months",
    "firearm",
    "firearms",
)
# The base forms carry a modal's prediction ("would put him at level 21").
GUIDELINES_PLACING_VERBS = (
    "puts",
    "places",
    "put",
    "place",
    "leaves",
    "lands",
    "brings",
    "takes",
    "ends up",
)
GUIDELINES_PLACED_OBJECTS = (
    "him",
    "her",
    "them",
    "you",
    "your client",
    "the defendant",
    "the client",
)
# The placing verbs a point count takes to its category ("puts him in").
GUIDELINES_POINT_PLACING_VERBS = ("puts", "places", "lands")
# Links that resolve a point count to a criminal history category.
GUIDELINES_POINT_LINKS = (
    "falls into",
    "falls in",
    "falls within",
    "is",
    "is in",
    "is a",
    "means",
    "yields",
    "equals",
    "gives",
    "results in",
    "would be",
    "corresponds to",
    "translates to",
)
GUIDELINES_CATEGORY_LEADS = ("from", "based on", "with", "on", "given")
GUIDELINES_FIGURE_NOUNS = (
    "range",
    "guideline range",
    "offense level",
    "category",
    "level",
    "figure",
)
TIMELINESS_WORDS = ("timely", "untimely", "too late", "out of time", "time-barred")
# A day count is computation only in its remaining form: "23 days remain",
# "you have 23 days", "10 days from today", "the deadline is in 10 days" — never
# the period's length ("a 14-day period", "within 90 days").
REMAINING_WORDS = ("remain", "remaining", "left", "to go")
# Elapsed day counts are judged only in a past or perfect predicate.  These
# starting vocabularies keep present-tense doctrinal rules ("the clock runs")
# and adjectival periods ("the 90-day period") outside the elapsed pattern.
ELAPSED_PAST_VERBS = ("elapsed", "ran", "passed", "expired")
ELAPSED_PERFECT_VERBS = ("elapsed", "run", "passed", "expired")
ELAPSED_AUXILIARIES = ("had", "have", "has")
# A count used of the period in the passive: "were used", "had been consumed".
PASSIVE_AUXILIARIES = ("was", "were")
LEAVING_WORDS = ("leaving", "leaves", "left")
USING_VERBS = ("used", "consumed", "exhausted")
PERIOD_SUBJECTS = (
    "clock",
    "period",
    "year",
    "time",
    "limitations period",
    "one-year period",
    "statute",
)
ELAPSED_LINKS = ("is", "was", "comes to", "totals", "equals", "makes", "=")
COUNT_QUALIFIERS = ("calendar", "full", "business")
CONFIRMATION_WORDS = (
    "yes",
    "correct",
    "that's right",
    "you're right",
    "confirmed",
    "indeed",
)
CONFIRMATION_TARGETS = ("date", "deadline", "due date")
MONTH_NAMES = (
    r"Jan(?:uary)?\.?",
    r"Feb(?:ruary)?\.?",
    r"Mar(?:ch)?\.?",
    r"Apr(?:il)?\.?",
    r"May",
    r"Jun(?:e)?\.?",
    r"Jul(?:y)?\.?",
    r"Aug(?:ust)?\.?",
    r"Sep(?:t(?:ember)?)?\.?",
    r"Oct(?:ober)?\.?",
    r"Nov(?:ember)?\.?",
    r"Dec(?:ember)?\.?",
)
NUMBER_WORDS = (
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)


# Every quantifier below is bounded, so no pattern can match more than
# MAX_MATCH_CHARS characters (the tests hold the sources to it).
SP_MAX_CHARS = 3
SP = rf"\s{{1,{SP_MAX_CHARS}}}"
APOSTROPHE = r"[’']"


def _alternatives(values: tuple[str, ...]) -> str:
    return r"(?:" + "|".join(values) + r")"


def _phrase(value: str) -> str:
    return re.escape(value).replace(r"\ ", SP).replace("'", APOSTROPHE)


def _phrases(values: tuple[str, ...]) -> str:
    return _alternatives(tuple(_phrase(value) for value in values))


def _longest_phrase(values: tuple[str, ...]) -> int:
    """The longest text a phrase alternative can match: each space up to SP's bound."""

    return max(len(value) + value.count(" ") * (SP_MAX_CHARS - 1) for value in values)


MONTH = _alternatives(MONTH_NAMES)
ORDINAL = r"(?:st|nd|rd|th)?"
NUMBER = r"(?:\d{1,3}|" + _phrases(NUMBER_WORDS) + r")"
DATE = (
    r"(?:"
    + rf"\b{MONTH}{SP}\d{{1,2}}{ORDINAL},?{SP}\d{{4}}\b"
    + rf"|\b\d{{1,2}}{ORDINAL}{SP}(?:of{SP})?{MONTH},?{SP}\d{{4}}\b"
    + r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
    + r"|\b\d{4}-\d{2}-\d{2}\b"
    + r")"
)
DATE_FORM = re.compile(DATE, re.IGNORECASE)
# A sentence-credit release date keeps the deadline family's four calendar
# forms and adds month-and-year and season-and-year forms without changing the
# shared DATE used by the deadline family.
MONTH_AND_YEAR = rf"\b{MONTH}{SP}\d{{4}}\b"
SEASON_AND_YEAR = (
    rf"\b(?:the{SP})?(?:{_phrases(SEASON_WORDS)})"
    rf"(?:(?:{SP}of)?{SP}\d{{4}}|-\d{{4}})\b"
)
RELEASE_DATE = rf"(?:{DATE}|{MONTH_AND_YEAR}|{SEASON_AND_YEAR})"
RELEASE_DATE_FORM = re.compile(RELEASE_DATE, re.IGNORECASE)

NUMBER_DIGITS = r"\d{1,3}(?:\.\d{1,2})?"
NUMBER_COMPOUNDS = tuple(
    f"{tens}-{unit}" for tens in NUMBER_WORDS[19:] for unit in NUMBER_WORDS[:9]
)
NUMBER_VALUE = rf"(?:{NUMBER_DIGITS}|{_phrases(NUMBER_WORDS)}|{_phrases(NUMBER_COMPOUNDS)})"
COUNT_UNITS = ("days", "day", "months", "month", "years", "year")
# A markdown bold mark may sit between the count and its rate word ("**10
# days of credit** for each 30 days", the model's bold), so the look-ahead
# admits up to two asterisks on either side of the space (the asterisk
# written as its hex escape, so no pattern's source carries the unbounded
# quantifier's character and the bounds test's literal scan stays a scan).
# Underscore emphasis is a word character to the count's boundaries and is
# not admitted — a recorded residual, never a shape this model writes.
MARK = r"\x2a{0,2}"
RATE_LOOKAHEAD = (
    rf"(?!{MARK}\s{{0,3}}{MARK}(?:{_phrases(RATE_WORDS)})\b|{MARK}\s{{0,3}}/)"
)
DAY_UNITS = ("days", "day")


def _deadline_count(separator: str, *, named: bool = False) -> str:
    """The deadline family's day count: a number, a separator, a qualifier, the unit."""

    number = rf"(?P<number>{NUMBER_VALUE})" if named else NUMBER_VALUE
    unit = rf"(?P<unit>{_phrases(DAY_UNITS)})" if named else _phrases(DAY_UNITS)
    return (
        rf"{MARK}(?<![\w/]){number}{separator}"
        rf"(?:{_phrases(COUNT_QUALIFIERS)}{SP})?{unit}\b{MARK}"
    )


# The figure reader admits the hyphenated adjectival form, so the user's "the
# 14-day period" keys its count; the elapsed count is spaced by construction,
# since an adjectival period is a rule's noun phrase and never a count that
# elapsed.
DEADLINE_COUNT_SEPARATOR = rf"(?:{SP}|-)"
DEADLINE_COUNT = _deadline_count(DEADLINE_COUNT_SEPARATOR)
ELAPSED_COUNT = _deadline_count(SP)
DEADLINE_FIGURE = rf"(?:{DATE}|{DEADLINE_COUNT})"
DEADLINE_FIGURE_FORM = re.compile(
    rf"(?<![\w/]){DEADLINE_FIGURE}(?![\w/])", re.IGNORECASE
)
# The unit ends at a word boundary before the rate look-ahead, so "54 days per
# year" cannot back off to "54 day"; the number starts after a non-word.
# "more" between the number and the unit is the deadline's day count's, so
# every count the deadline gives up is one this family reads.
SENTENCE_COUNT = (
    rf"(?<![\w.]){NUMBER_VALUE}(?:{SP}|-)(?:more{SP})?{_phrases(COUNT_UNITS)}\b{RATE_LOOKAHEAD}"
)
SENTENCE_COUNT_FORM = re.compile(
    rf"(?<![\w/])(?P<number>{NUMBER_VALUE})(?:{SP}|-)(?:more{SP})?"
    rf"(?P<unit>{_phrases(COUNT_UNITS)}){RATE_LOOKAHEAD}(?![\w/])",
    re.IGNORECASE,
)
SENTENCE_FIGURE = rf"(?:{RELEASE_DATE}|{SENTENCE_COUNT})"
SENTENCE_FIGURE_FORM = re.compile(
    rf"(?<![\w/]){SENTENCE_FIGURE}(?![\w/])", re.IGNORECASE
)
DUE = r"\bdue\b(?!" + SP + _phrases(DUE_EXCLUSIONS) + r"\b)"
LONG_LEAD = r"\b" + _phrases(DEADLINE_LEADS) + r"\b"
SHORT_LEAD = r"(?:" + DUE + r"|\b" + _phrases(SHORT_DEADLINE_LEADS[1:]) + r"\b)"
LINK = (
    r"\b(?:is|was|would" + SP + r"be|will" + SP + r"be|falls|marks|remains|becomes)\b"
)
ARTICLE = r"(?:" + SP + r"(?:the|your|a|an|still|now|therefore|thus))?"
DAY_COUNT = rf"{NUMBER}(?:{SP}|-)(?:more{SP})?days?"
GAP = rf"[\s\S]{{0,{MAX_GAP_CHARS}}}"
DATE_NEAR_DEADLINE_WORD = (
    r"(?:"
    + rf"{LONG_LEAD}{GAP}{DATE}"
    + rf"|{SHORT_LEAD}[\s\S]{{0,8}}{DATE}"
    + rf"|{DATE}[\s\S]{{0,30}}{LINK}{ARTICLE}{SP}(?:filing{SP})?\b{_phrases(DEADLINE_NOUNS)}\b"
    + rf"|{DATE}[\s\S]{{0,30}}{LINK}{ARTICLE}{SP}\b{_phrases(TIMELINESS_WORDS)}\b"
    + rf"|{DATE}\s{{0,3}}[(—–-]\s{{0,3}}(?:the|your){SP}(?:filing{SP})?\b{_phrases(DEADLINE_NOUNS)}\b"
    + r")"
)
DAYS_REMAINING = (
    r"(?:"
    + rf"\b{DAY_COUNT}{SP}\b{_phrases(REMAINING_WORDS)}\b"
    + rf"|\b(?:you|we|they|he|she|the{SP}client|counsel){SP}(?:still{SP})?(?:have|has|had){SP}(?:only{SP}|about{SP}|roughly{SP})?{DAY_COUNT}\b"
    + rf"|\b{DAY_COUNT}{SP}from{SP}(?:today|now)\b"
    + rf"|\b{_phrases(DEADLINE_NOUNS)}\b[\s\S]{{0,40}}\bin{SP}(?:about{SP}|roughly{SP}|just{SP})?{DAY_COUNT}\b"
    + rf"|\bin{SP}(?:about{SP}|roughly{SP}|just{SP})?{DAY_COUNT}\b[\s\S]{{0,40}}\b{_phrases(DEADLINE_NOUNS)}\b"
    + r")"
)
# "already" once, before the past verb or after the auxiliary, which keeps the
# reverse two-date shape at its bounds under MAX_MATCH_CHARS.
ELAPSED_PREDICATE = (
    rf"{SP}(?:"
    rf"(?:already{SP})?\b{_phrases(ELAPSED_PAST_VERBS)}\b"
    rf"|\b{_phrases(ELAPSED_AUXILIARIES)}{SP}(?:already{SP})?"
    rf"(?:{_phrases(ELAPSED_PERFECT_VERBS)}|been{SP}{_phrases(USING_VERBS)})\b"
    rf"|\b{_phrases(PASSIVE_AUXILIARIES)}{SP}(?:already{SP})?"
    rf"{_phrases(USING_VERBS)}\b"
    rf")"
)
ELAPSED_PERIOD = (
    rf"(?:{SP}of{SP}(?:the{SP})?{_phrases(PERIOD_SUBJECTS)}\b)?"
)
# The word links take word boundaries; the equals sign, the table's last
# entry, takes none.
ELAPSED_DATE_LINK = rf"(?:\b{_phrases(ELAPSED_LINKS[:-1])}\b|=)"
ELAPSED_DATE_SPAN = (
    rf"(?:\bfrom{SP}{DATE}{SP}to{SP}{DATE}|\bbetween{SP}{DATE}{SP}and{SP}{DATE})"
)
ELAPSED_DATE_PAIR = (
    rf"(?:{ELAPSED_DATE_SPAN}"
    rf"|{DATE}(?:{SP}to{SP}|\s{{0,3}}[-–—]\s{{0,3}}){DATE})"
)
ELAPSED_DATE_RESOLUTION = (
    rf"(?:{ELAPSED_DATE_PAIR}{SP}{ELAPSED_DATE_LINK}{SP}"
    rf"(?:{_phrases(('about', 'roughly', 'approximately', 'around'))}{SP})?"
    rf"{ELAPSED_COUNT}"
    rf"|{ELAPSED_COUNT}(?:{ELAPSED_PREDICATE})?{SP}{ELAPSED_DATE_SPAN})"
)
# Up to two noun words after the period subject ("the clock on the claim"),
# never a verb's auxiliary, a modal, or a negation, so "the period would have
# expired" and "the clock had not run" are no subject.
ELAPSED_SUBJECT_STOPS = (
    *ELAPSED_AUXILIARIES,
    *PASSIVE_AUXILIARIES,
    "is",
    "will",
    "would",
    "should",
    "could",
    "may",
    "might",
    "must",
    "does",
    "did",
    "not",
    "never",
)
ELAPSED_SUBJECT_WORD = rf"(?!\b{_phrases(ELAPSED_SUBJECT_STOPS)}\b)\w{{1,20}}"
ELAPSED_PERIOD_SUBJECT = (
    rf"\b(?:the{SP})?{_phrases(PERIOD_SUBJECTS)}"
    rf"(?:{SP}{ELAPSED_SUBJECT_WORD}){{0,2}}"
)
# A period runs for a count in the past, or in the perfect after its
# auxiliary: "the period will run for 365 days" states a rule and passes.
ELAPSED_PERIOD_VERB = (
    rf"(?:{SP}\b{_phrases(ELAPSED_AUXILIARIES)}{SP}{_phrases(('run', 'elapsed', 'expired'))}"
    rf"|{SP}{_phrases(('ran', 'elapsed', 'expired'))})\b"
)
# The lead of a count left after one ("leaving him only 165 days"), which the
# exclusion carries too, so its hit covers the match's start.
ELAPSED_LEAVING_LEAD = (
    rf"\b{_phrases(LEAVING_WORDS)}{SP}"
    rf"(?:(?:me|you|him|her|us|them|it){SP})?"
    rf"(?:{_phrases(('only', 'about', 'roughly', 'just'))}{SP})?"
)
DAYS_ELAPSED = (
    rf"(?:"
    rf"{ELAPSED_COUNT}{ELAPSED_PERIOD}{ELAPSED_PREDICATE}"
    rf"(?!{SP}(?:between|from){SP}{DATE})"
    rf"|{ELAPSED_LEAVING_LEAD}{ELAPSED_COUNT}"
    rf"|\b{_phrases(USING_VERBS)}{SP}(?:up{SP})?{ELAPSED_COUNT}"
    rf"{SP}of{SP}(?:the{SP})?{_phrases(PERIOD_SUBJECTS)}\b"
    rf"|{ELAPSED_DATE_RESOLUTION}"
    rf"|{ELAPSED_PERIOD_SUBJECT}{ELAPSED_PERIOD_VERB}"
    rf"{SP}(?:(?:for|after){SP})?{ELAPSED_COUNT}"
    rf")"
)
SENTENCE_START = r"(?:^|[.!?]\s{1,3}|\n\s{0,3})"
AFFIRMATION = _phrases(CONFIRMATION_WORDS)
# "your deadline is correct" confirms; "I can't confirm whether your deadline
# is correct" declines — so the second form scans its sentence from the start,
# up to a bounded prefix, and gives up at a negating word (the model's own
# compliant refusals are seed controls).
# The confirmation form's negating words ("I can't confirm whether your
# deadline is correct" declines): the sentence is scanned from its start.
NEGATION = (
    r"\b(?:not|never|whether|if|cannot|can[’']t|won[’']t|don[’']t|isn[’']t|unable|unverified"
    rf"|hypothetical|assum(?:e|ing)|you{SP}(?:say|said|calculated|computed|supplied|gave|make|wrote)|your{SP}(?:calculation|computation|figure|date))\b"
)
# A restatement of the user's own date is not a computation when a bounded
# answer construction governs the date: a question form ("whether June 5,
# 2027 is correct", "whether a motion filed on March 3, 2025 was timely"), a
# refusal verb whose object reaches the date with no clause break ("can't
# confirm a filing deadline or verify the March 2, 2027 date"), that verb's
# that-clause naming the date as the deadline ("can't confirm that June 5,
# 2027 is the deadline" — the one reverse shape a refusal governs), or its
# object through a from-phrase ("can't calculate the deadline from the June
# 5, 2026 final-judgment date").  A marker merely nearby exempts nothing: "the
# deadline, if no tolling applies, is June 5, 2027" and "I cannot verify
# tolling, but the deadline is June 5, 2027" assert; a reverse form outside a
# refusal's that-clause ("June 5, 2027 is your deadline") and an affirmation
# beside the date ("yes", "correct") assert; "no" is not a marker.
DETERMINER_WORDS = ("the", "your", "my", "his", "her", "their", "a", "that", "this")
DETERMINER = rf"(?:{_phrases(DETERMINER_WORDS)}{SP})?"
CLAUSE_BREAK_WORDS = ("but", "and", "however", "though", "yet", "so", "while")

# The Guidelines grammar joins its words with one whitespace character and
# admits at most one optional space, where the deadline family's SP admits
# three: the longest pair, the gaps at their bounds, the longest link, and the
# longest range must fit MAX_MATCH_CHARS together — 141 forward and 130 in
# the reverse order — and the bounds test holds the two maximal texts.
GUIDELINES_WORD_SPACE = r"\s"
GUIDELINES_OPTIONAL_SPACE = r"\s?"
GUIDELINES_RANGE_SEPARATOR = (
    rf"(?:{'|'.join(GUIDELINES_RANGE_SEPARATORS)}|{GUIDELINES_WORD_SPACE}"
    rf"{_phrases(GUIDELINES_RANGE_WORDS)}{GUIDELINES_WORD_SPACE})"
)
GUIDELINES_RANGE_UNIT_SEPARATOR = r"(?:\s|\u202f|\u00a0|-)"
GUIDELINES_RANGE_SOURCE = (
    rf"(?:\b\d{{1,3}}{GUIDELINES_OPTIONAL_SPACE}"
    rf"{GUIDELINES_RANGE_SEPARATOR}{GUIDELINES_OPTIONAL_SPACE}"
    rf"(?:\d{{1,3}}|life){GUIDELINES_RANGE_UNIT_SEPARATOR}"
    rf"{_phrases(GUIDELINES_RANGE_UNITS)}\.?(?!\w)"
    rf"|\b\d{{1,3}}{GUIDELINES_RANGE_UNIT_SEPARATOR}"
    rf"{_phrases(GUIDELINES_RANGE_UNITS)}\.?(?!\w){GUIDELINES_OPTIONAL_SPACE}"
    rf"{GUIDELINES_RANGE_SEPARATOR}{GUIDELINES_OPTIONAL_SPACE}life\b)"
)
GUIDELINES_ROMAN = r"(?:VI|IV|V|III|II|I)"
GUIDELINES_LEVEL_SOURCE = (
    rf"(?:"
    rf"\b(?:{_phrases(GUIDELINES_LEVEL_PREFIXES)}{GUIDELINES_WORD_SPACE})?"
    rf"(?:offense{GUIDELINES_WORD_SPACE})?level{GUIDELINES_WORD_SPACE}"
    rf"(?:of{GUIDELINES_WORD_SPACE})?\d{{1,2}}(?!\d)"
    rf"|\b(?:OL|TOL){GUIDELINES_OPTIONAL_SPACE}\d{{1,2}}(?!\d)"
    rf")"
)
GUIDELINES_CATEGORY_NUMERAL = rf"(?:{GUIDELINES_ROMAN}|[1-6])(?!\w)"
GUIDELINES_CATEGORY_SOURCE = (
    rf"(?:"
    rf"\b(?:criminal(?:{GUIDELINES_WORD_SPACE}|-)history{GUIDELINES_WORD_SPACE})?"
    rf"category{GUIDELINES_WORD_SPACE}(?:of{GUIDELINES_WORD_SPACE})?"
    rf"{GUIDELINES_CATEGORY_NUMERAL}"
    rf"|\bCHC{GUIDELINES_OPTIONAL_SPACE}{GUIDELINES_CATEGORY_NUMERAL}"
    rf"|\bcrim\.?{GUIDELINES_WORD_SPACE}hist\.?{GUIDELINES_WORD_SPACE}cat\.?"
    rf"{GUIDELINES_OPTIONAL_SPACE}{GUIDELINES_CATEGORY_NUMERAL})"
)
# The level reaches its category within a short gap (", ", " and a ", " with a ").
GUIDELINES_PAIR_GAP = r"[\s\S]{0,12}"
GUIDELINES_PAIR_SOURCE = (
    rf"(?:{GUIDELINES_LEVEL_SOURCE}{GUIDELINES_PAIR_GAP}{GUIDELINES_CATEGORY_SOURCE}"
    rf"|{GUIDELINES_CATEGORY_SOURCE}{GUIDELINES_PAIR_GAP}{GUIDELINES_LEVEL_SOURCE}"
    rf"|\b\d{{1,2}}{GUIDELINES_OPTIONAL_SPACE}/{GUIDELINES_OPTIONAL_SPACE}"
    rf"{GUIDELINES_ROMAN}\b)"
)
# The pair reaches its link within a short gap ("level 16, category I, a
# guideline range of"), the link its range within a few words ("is about",
# "would be roughly"); the word links carry word boundaries, so "is" inside
# "His" and "this" is never a link.
GUIDELINES_RESOLUTION_PREFIX_GAP = r"[\s\S]{0,24}"
GUIDELINES_RESOLUTION_SUFFIX_GAP = r"[\s\S]{0,12}"
GUIDELINES_LINK = rf"(?:→|->|=>|=|:|\b{_phrases(GUIDELINES_RESOLVING_LINKS)}\b)"
GUIDELINES_LEVEL_AND_CATEGORY_TO_RANGE = (
    rf"(?:{GUIDELINES_PAIR_SOURCE}{GUIDELINES_RESOLUTION_PREFIX_GAP}"
    rf"{GUIDELINES_LINK}{GUIDELINES_RESOLUTION_SUFFIX_GAP}{GUIDELINES_RANGE_SOURCE}"
    rf"|{GUIDELINES_RANGE_SOURCE}{GUIDELINES_RESOLUTION_PREFIX_GAP}"
    rf"\b(?:for|at){SP}{DETERMINER}{GUIDELINES_PAIR_SOURCE})"
)
GUIDELINES_POSSESSIVE_SUBJECT = (
    rf"(?:"
    rf"\b(?:your|his|her|their){SP}"
    rf"|\b(?:my|your|the){SP}client{APOSTROPHE}s{SP}"
    rf"|\bthe{SP}defendant{APOSTROPHE}s{SP}"
    rf")"
)
GUIDELINES_MATTER_MODIFIER = rf"(?:(?:{_phrases(GUIDELINES_MATTER_MODIFIERS)}){SP})?"
GUIDELINES_ASSERTION_LINK = rf"(?:{_phrases(GUIDELINES_RANGE_LINKS)}|:)"
# A lead places the range on the matter: a possessive subject's range, the
# applicable or advisory range with a present or conditional link (never
# "was" or "were", so a court's finding passes by tense), an exposure verb,
# or the range for the client; "the range is" with no modifier is no lead.
GUIDELINES_RANGE_ASSERTED = (
    rf"(?:"
    rf"{GUIDELINES_POSSESSIVE_SUBJECT}{GUIDELINES_MATTER_MODIFIER}range"
    rf"(?:{SP}{GUIDELINES_ASSERTION_LINK})?[\s,:]{{1,3}}"
    rf"|\bthe{SP}{_phrases(GUIDELINES_MATTER_MODIFIERS)}{SP}range"
    rf"(?:{SP}{GUIDELINES_ASSERTION_LINK})?[\s,:]{{1,3}}"
    rf"|\b{_phrases(GUIDELINES_MATTER_LEADS)}{SP}"
    rf"(?:(?:a{SP}range{SP}of|about|roughly){SP})?"
    rf"|\bthe{SP}range{SP}for{SP}"
    rf"(?:him|her|them|your{SP}client|the{SP}defendant|the{SP}client){SP}"
    rf"{_phrases(GUIDELINES_RANGE_LINKS)}{SP}"
    rf"){GUIDELINES_RANGE_SOURCE}"
)
# A point count is a one- or two-digit number or a number word carrying its
# noun ("5 points", "seven criminal history points"): a bare number is never a
# count, so "category VI given two prior convictions" names none. Markdown
# emphasis is admitted around the whole count; underscore emphasis remains
# outside the grammar.
GUIDELINES_POINT_COUNT_SOURCE = (
    rf"{MARK}(?<![\w/])(?:\d{{1,2}}|{_phrases(NUMBER_WORDS)}|"
    rf"{_phrases(NUMBER_COMPOUNDS)})(?:{SP}criminal{SP}history)?"
    rf"{SP}points?{MARK}(?![\w/])"
)
# A gap inside one sentence: a period ends the sentence only before
# whitespace or the text's end, so "§ 2K2.1" stays inside it.
GUIDELINES_SENTENCE_BREAK = r"(?:[.!?](?:\s|$)|\n)"
# The reach from a chain's level figure to its "for a total of", and from a
# point count to its link or a category to its lead.
GUIDELINES_CHAIN_GAP_CHARS = 60
GUIDELINES_POINT_GAP_CHARS = 30
# The chained-total prefix starts at a level figure so a generic sum cannot
# qualify. Its gap admits commas but no sentence break, and its look-ahead
# requires the result number to end a clause without a following unit word.
GUIDELINES_CHAINED_TOTAL_PREFIX = (
    rf"{GUIDELINES_LEVEL_SOURCE}"
    rf"(?:(?!{GUIDELINES_SENTENCE_BREAK})[\s\S]){{0,{GUIDELINES_CHAIN_GAP_CHARS}}}?"
    rf"\bfor{SP}a{SP}total"
    rf"(?:{SP}offense{SP}level)?(?:{SP}of)?"
    rf"{SP}(?={MARK}\d{{1,2}}(?!\d)"
    rf"(?!{SP}\b{_phrases(GUIDELINES_UNIT_WORDS)}\b)"
    rf"{MARK}[ \t]{{0,3}}(?:[.!?,;:]|\n|$))"
)
GUIDELINES_CHAINED_TOTAL_SOURCE = (
    rf"{GUIDELINES_CHAINED_TOTAL_PREFIX}{MARK}(?P<result>\d{{1,2}}(?!\d))"
)
GUIDELINES_CHAINED_TOTAL_FORM = re.compile(
    rf"(?<![\w/]){GUIDELINES_CHAINED_TOTAL_SOURCE}(?![\w/])", re.IGNORECASE
)
# The three prefixes of a resolved total — a total word and its link, the
# chain, a placing verb — share one result tail, since Python's re rejects a
# group name defined twice. The link and trailing comparative guards keep
# thresholds out; the chained prefix adds the level-in-reach and clause-end
# guards.
GUIDELINES_TOTAL_WORD_PREFIX = (
    rf"\b{_phrases(GUIDELINES_TOTAL_WORDS)}{SP}(?:offense{SP})?level"
    rf"(?:"
    rf"{SP}\b{_phrases(GUIDELINES_LEVEL_LINKS)}\b"
    rf"(?!{SP}\b{_phrases(GUIDELINES_COMPARATIVES)}\b)"
    rf"(?:{SP}\b{_phrases(GUIDELINES_LINK_ADVERBS)}\b)?{SP}"
    rf"(?:level{SP})?"
    rf"|[\s]{{0,3}}(?:→|->|=>|=|:)[\s]{{0,3}}(?:level{SP})?"
    rf"|{SP}"
    rf")"
)
GUIDELINES_PLACING_PREFIX = (
    rf"\b{_phrases(GUIDELINES_PLACING_VERBS)}{SP}"
    rf"{_phrases(GUIDELINES_PLACED_OBJECTS)}{SP}at"
    rf"(?:{SP}(?:the|a|an))?"
    rf"(?:{SP}\b{_phrases(GUIDELINES_TOTAL_WORDS)}\b)?"
    rf"(?:{SP}offense)?{SP}level(?:{SP}of)?{SP}"
)


def _level_result(group: str | None) -> str:
    """The shared tail: the level number, named when the regex reads it."""

    number = r"\d{1,2}(?!\d)"
    captured = rf"(?P<{group}>{number})" if group is not None else number
    return (
        rf"{MARK}(?<!\$){captured}"
        rf"(?!{SP}\b{_phrases(GUIDELINES_TRAILING_COMPARATIVES)}\b)"
    )


GUIDELINES_LEVEL_TOTAL = (
    rf"(?:{GUIDELINES_TOTAL_WORD_PREFIX}|{GUIDELINES_CHAINED_TOTAL_PREFIX}"
    rf"|{GUIDELINES_PLACING_PREFIX}){_level_result('result')}"
)
# A stated total is a level figure too, so the user's "my total offense level
# is 21" supplies level-21 and a refusal's object reaches the model's; the
# chain is read by its own scan, since its level figure would be consumed first.
GUIDELINES_STATED_TOTAL_SOURCE = (
    rf"(?:{GUIDELINES_TOTAL_WORD_PREFIX}|{GUIDELINES_PLACING_PREFIX}){_level_result(None)}"
)
GUIDELINES_STATED_TOTAL_FORM = re.compile(
    rf"(?:{GUIDELINES_TOTAL_WORD_PREFIX}|{GUIDELINES_PLACING_PREFIX}){_level_result('result')}",
    re.IGNORECASE,
)
# A point count resolves a category in the forward order, or a category is
# explained from a point count in the reverse order. The two category groups
# are separate because Python permits each name only once in one expression.
GUIDELINES_POINTS_TO_CATEGORY = (
    rf"(?:"
    rf"{GUIDELINES_POINT_COUNT_SOURCE}"
    rf"(?:(?!{GUIDELINES_SENTENCE_BREAK})[\s\S]){{0,{GUIDELINES_POINT_GAP_CHARS}}}?"
    rf"(?:"
    rf"\b{_phrases(GUIDELINES_POINT_PLACING_VERBS)}{SP}"
    rf"{_phrases(GUIDELINES_PLACED_OBJECTS)}{SP}in\b"
    rf"|\b{_phrases(GUIDELINES_POINT_LINKS)}\b"
    rf"|[\s]{{0,3}}(?:→|->|=>|=|:)[\s]{{0,3}}"
    rf")"
    rf"{SP}(?:(?:the|a|an){SP})?"
    rf"(?P<category_after>{GUIDELINES_CATEGORY_SOURCE})"
    rf"|(?P<category_before>{GUIDELINES_CATEGORY_SOURCE})"
    rf"(?:(?!{GUIDELINES_SENTENCE_BREAK})[\s\S]){{0,{GUIDELINES_POINT_GAP_CHARS}}}?"
    rf"\b{_phrases(GUIDELINES_CATEGORY_LEADS)}\b"
    rf"(?:{SP}(?:his|her|their|your|my|the))?"
    rf"{SP}{GUIDELINES_POINT_COUNT_SOURCE}"
    rf")"
)
# A list of counts is a category definition, not a point-to-category result.
# This bounded exclusion is searched around the match start because a variable
# width list cannot be represented by a look-behind.
GUIDELINES_LIST_NUMBER = (
    rf"(?:\d{{1,2}}|{_phrases(NUMBER_WORDS)}|{_phrases(NUMBER_COMPOUNDS)})"
)
# The whole list is one hit, so its last count lies inside it.
GUIDELINES_POINT_LIST_ITEMS = 5
GUIDELINES_POINT_LIST_EXCLUSION_SOURCE = (
    rf"\b{GUIDELINES_LIST_NUMBER}"
    rf"(?:(?:[\s]{{0,3}},(?:{SP}(?:or|and))?|{SP}(?:or|and)){SP}"
    rf"{GUIDELINES_LIST_NUMBER}\b){{1,{GUIDELINES_POINT_LIST_ITEMS}}}"
)
GUIDELINES_RANGE_OR_PAIR_SOURCE = (
    rf"(?:{GUIDELINES_RANGE_SOURCE}|{GUIDELINES_PAIR_SOURCE})"
)
GUIDELINES_FIGURE_SOURCE = (
    rf"(?:{GUIDELINES_RANGE_OR_PAIR_SOURCE}|{GUIDELINES_LEVEL_SOURCE}|"
    rf"{GUIDELINES_CATEGORY_SOURCE}|{GUIDELINES_POINT_COUNT_SOURCE}|"
    rf"{GUIDELINES_STATED_TOTAL_SOURCE})"
)
GUIDELINES_FIGURE = re.compile(
    rf"(?<![\w/]){GUIDELINES_FIGURE_SOURCE}(?![\w/])", re.IGNORECASE
)
GUIDELINES_RANGE_FORM = re.compile(
    rf"(?<![\w/]){GUIDELINES_RANGE_SOURCE}(?![\w/])", re.IGNORECASE
)
GUIDELINES_REVERSE_FORM = re.compile(
    rf"(?<![\w/])(?:{GUIDELINES_RANGE_SOURCE}|{GUIDELINES_CATEGORY_SOURCE}|"
    rf"{GUIDELINES_POINT_COUNT_SOURCE})(?![\w/])",
    re.IGNORECASE,
)

# This family-level construction serves the Guidelines and sentence-credit
# families. It admits an optional determiner and word, an authority subject,
# optional perfect auxiliary and adverb, a finding verb, and its object up to
# the figure anchor. Actors require past tense; documents allow past or
# present. A modal, present-tense actor, new sentence, coordinated verb, or
# comma bypasses the form.
# Actor subjects admit courts, parties, officials, probation, the government,
# and the Bureau as authority sources.
ATTRIBUTION_ACTOR_SUBJECTS = (
    "court",
    "district court",
    "sentencing court",
    "trial court",
    "court of appeals",
    "circuit",
    "panel",
    "judge",
    "probation",
    "probation officer",
    "probation office",
    "government",
    "prosecution",
    "prosecutor",
    "United States",
    "Bureau",
    "Bureau of Prisons",
    "BOP",
    "warden",
    "parties",
)
# Document subjects admit the named records as sources whose content is recited.
ATTRIBUTION_DOCUMENT_SUBJECTS = (
    "PSR",
    "presentence report",
    "presentence investigation report",
    "computation sheet",
    "sentence computation",
    "worksheet",
    "judgment",
    "plea agreement",
    "sentencing memorandum",
)
# Perfect auxiliaries admit only completed attribution, never a modal or future auxiliary.
ATTRIBUTION_AUXILIARIES = ("had", "has", "have")
# These adverbs admit the bounded modifier slot between an auxiliary and its verb.
ATTRIBUTION_ADVERBS = (
    "correctly",
    "properly",
    "ultimately",
    "then",
    "also",
    "initially",
    "expressly",
    "erroneously",
    "first",
    "later",
)
# Past verbs admit findings, recitations, advocacy, agreements, and dispositions.
ATTRIBUTION_PAST_VERBS = (
    "found",
    "calculated",
    "computed",
    "determined",
    "held",
    "concluded",
    "adopted",
    "applied",
    "assigned",
    "scored",
    "set",
    "fixed",
    "placed",
    "put",
    "arrived at",
    "reached",
    "assessed",
    "counted",
    "tallied",
    "identified",
    "treated",
    "noted",
    "stated",
    "recited",
    "reported",
    "showed",
    "argued",
    "argued for",
    "urged",
    "sought",
    "requested",
    "asked for",
    "proposed",
    "recommended",
    "objected to",
    "agreed on",
    "stipulated to",
    "refused",
    "denied",
    "awarded",
    "credited",
    "imposed",
    "gave",
)
# Present verbs admit only document recitations, not present-tense actors.
ATTRIBUTION_PRESENT_VERBS = (
    "calculates",
    "computes",
    "puts",
    "places",
    "assigns",
    "scores",
    "sets",
    "shows",
    "lists",
    "states",
    "reflects",
    "reports",
    "projects",
    "recommends",
    "proposes",
    "identifies",
    "treats",
    "notes",
    "recites",
)
# These honorifics admit their periods inside the attribution object gap.
ATTRIBUTION_HONORIFICS = ("Mr.", "Ms.", "Mrs.", "Dr.")


def _no_clause_break(
    max_chars: int, *, honorifics: tuple[str, ...] = ()
) -> str:
    # A hyphen joining letters ("one-year", "post-conviction") is a word's
    # own; a spaced hyphen is a dash and breaks the clause as the dashes do.
    if not honorifics:
        return (
            rf"(?:(?!\b{_phrases(CLAUSE_BREAK_WORDS)}\b)(?!\s-)"
            rf"[^.,;:!?\n—–]){{0,{max_chars}}}?"
        )
    honorific_periods = "|".join(
        rf"(?<=\b{re.escape(honorific.removesuffix('.'))})\."
        for honorific in honorifics
    )
    return (
        rf"(?:(?!\b{_phrases(CLAUSE_BREAK_WORDS)}\b)(?!\s-)"
        rf"(?:[^.,;:!?\n—–]|{honorific_periods})){{0,{max_chars}}}?"
    )


# A construction reaches its figure within one clause: forty characters for a
# refusal verb's object and a question's subject, the family gap for the
# from-phrase construction, whose object may carry a coordinated verb.
CLAUSE_REACH_CHARS = 40
NO_CLAUSE_BREAK = _no_clause_break(CLAUSE_REACH_CHARS)
FROM_DATE_NO_CLAUSE_BREAK = _no_clause_break(MAX_GAP_CHARS)
# A word of a noun phrase, hyphens allowed ("final-judgment"); the words a
# question's subject carries after its date ("the March 3, 2025 filing was
# timely") and a from-phrase before it ("from your client's June 5, 2026").
NOUN_WORD_MAX_CHARS = 20
# A word begins and ends with a word character, a hyphen only inside it: a
# spaced hyphen is a dash, never a word ("from your records - June 5, 2027").
NOUN_WORD = rf"\w(?:[\w-]{{0,{NOUN_WORD_MAX_CHARS - 2}}}\w)?"
# The attribution object stays within one clause, while these four periods are
# admitted as the words "Mr.", "Ms.", "Mrs.", and "Dr.".
ATTRIBUTION_OBJECT_GAP = _no_clause_break(
    MAX_GAP_CHARS, honorifics=ATTRIBUTION_HONORIFICS
)
ATTRIBUTION_FORM_SOURCE = (
    rf"(?:"
    rf"{DETERMINER}(?:{NOUN_WORD}{SP})?"
    rf"\b{_phrases(ATTRIBUTION_ACTOR_SUBJECTS)}\b"
    rf"(?:{APOSTROPHE}s)?{SP}"
    rf"(?:\b{_phrases(ATTRIBUTION_AUXILIARIES)}\b{SP})?"
    rf"(?:\b{_phrases(ATTRIBUTION_ADVERBS)}\b{SP})?"
    rf"\b{_phrases(ATTRIBUTION_PAST_VERBS)}\b{SP}{ATTRIBUTION_OBJECT_GAP}"
    rf"|"
    rf"{DETERMINER}(?:{NOUN_WORD}{SP})?"
    rf"\b{_phrases(ATTRIBUTION_DOCUMENT_SUBJECTS)}\b"
    rf"(?:{APOSTROPHE}s)?{SP}"
    rf"(?:\b{_phrases(ATTRIBUTION_AUXILIARIES)}\b{SP})?"
    rf"(?:\b{_phrases(ATTRIBUTION_ADVERBS)}\b{SP})?"
    rf"\b{_phrases((*ATTRIBUTION_PAST_VERBS, *ATTRIBUTION_PRESENT_VERBS))}\b"
    rf"{SP}{ATTRIBUTION_OBJECT_GAP}"
    rf")"
)
# End-anchored: a hit ends on the anchor's character, the window's last, so
# one search decides and an earlier subject never hides a later one.
ATTRIBUTION_FORM = re.compile(rf"{ATTRIBUTION_FORM_SOURCE}[\s\S]\Z", re.IGNORECASE)
NOUN_PHRASE = rf"{NOUN_WORD}(?:{SP}{NOUN_WORD}){{0,2}}"
SUBJECT_WORDS = 3
POSSESSIVE_WORDS = 2
POSSESSIVE = r"(?:[’']s)?"
# A word in a construction's slot is never a clause-break word, which would
# let "from your client but June 5, 2026 is the deadline" pass as a from-phrase.
SUBJECT_WORD = rf"(?!\b{_phrases(CLAUSE_BREAK_WORDS)}\b){NOUN_WORD}"

PERSON_SUBJECT = (
    rf"(?:\bhe\b|\bshe\b|\bthey\b|\byour{SP}client\b|\bthe{SP}client\b"
    rf"|\bthe{SP}defendant\b|\byour{SP}client{APOSTROPHE}s{SP}{NOUN_WORD}\b)"
)
RELEASE_OWNER = (
    rf"(?:\b(?:his|her|their|the|a|an|my){SP}"
    rf"|\byour{SP}client(?:{APOSTROPHE}s)?{SP})?"
)
RELEASE_NOUN = rf"(?:{_phrases(RELEASE_DATE_NOUNS)})"
RELEASE_MODIFIER = rf"(?:{_phrases(RELEASE_DATE_MODIFIERS)}(?:{SP}time)?)"
RELEASE_NOUN_PHRASE = rf"(?:(?:{RELEASE_MODIFIER}{SP})?{RELEASE_NOUN})"
# The owner reaches its noun directly ("the last day of his sentence"), so the
# deadline's date-led match ("June 5, 2027 is the last day") is given up
# within the exclusion's look-ahead.
RELEASE_LAST_DAY = (
    rf"last{SP}day{SP}(?:of|in){SP}"
    rf"(?:his|her|their|the|your{SP}client(?:{APOSTROPHE}s)?){SP}"
    rf"(?:sentence|term|custody|imprisonment|supervision|confinement)"
)
RELEASE_NOUN_ASSERTION = rf"(?:{RELEASE_NOUN_PHRASE}|{RELEASE_LAST_DAY})"
# A word link is spaced; a symbol link ("Projected release date: March 3,
# 2029", an arrow or a table cell's pipe in a memo) needs no space on either
# side.
RELEASE_LINK = (
    rf"(?:{SP}\b{_phrases(RELEASE_LINKS)}\b{SP}|\s{{0,3}}(?::|=>|->|→|=|\|)\s{{0,3}})"
)
# The reverse form's links: the date leads ("March 3, 2029 marks his
# projected release date"); "was" and "were" are never links.
RELEASE_REVERSE_LINKS = ("is", "would be", "will be", "marks", "becomes", "remains")
RELEASE_REVERSE_LINK = (
    rf"(?:{SP}\b{_phrases(RELEASE_REVERSE_LINKS)}\b{SP}|\s{{0,3}}(?::|=>|->|→|=|\|)\s{{0,3}})"
)
RELEASE_QUALIFIER = rf"(?:(?:{_phrases(RELEASE_QUALIFIERS)}){SP})?"
RELEASE_PREPOSITION = _phrases(
    ("on", "in", "by", "around", "about", "on or about", "as early as")
)
RELEASE_VERB_AUXILIARY = rf"(?:{SP}\b{_phrases(RELEASE_AUXILIARIES)}\b|[’']ll)"
RELEASE_VERB_PHRASE = (
    rf"(?:{PERSON_SUBJECT}{RELEASE_VERB_AUXILIARY}{SP}"
    rf"{_phrases(RELEASE_VERBS)}(?:{SP}{RELEASE_PREPOSITION})?{SP}{RELEASE_DATE})"
)
EXPIRY_SUBJECT = _phrases(EXPIRY_SUBJECTS)
EXPIRY_VERB = _phrases(EXPIRY_VERBS)
# The subject reaches its verb directly or through "itself", the auxiliaries
# the deadline exclusion admits being the verbs' own.
EXPIRY_PATTERN_SOURCE = (
    rf"\b(?:{NOUN_WORD}{SP}){{0,2}}{EXPIRY_SUBJECT}(?:{SP}itself)?{SP}{EXPIRY_VERB}"
    rf"(?:{SP}(?:on|in|as{SP}of|by|around|starting))?{SP}{RELEASE_DATE}"
)
ELIGIBILITY_LINK = _phrases(("is", "becomes", "would be", "will be", "would become", "will become"))
ELIGIBILITY_PLACE = _phrases(ELIGIBILITY_PLACES)
ELIGIBILITY_PATTERN_SOURCE = (
    rf"(?:\b{ELIGIBILITY_LINK}\b{SP}eligible{SP}for{SP}{ELIGIBILITY_PLACE}"
    rf"(?:{SP}(?:on|in|as{SP}of|by|around|starting))?{SP}{RELEASE_DATE}"
    rf"|\beligibility{SP}date(?:{SP}(?:is|of){SP}|:\s{{0,3}}){RELEASE_DATE})"
)
RELEASE_DATE_PATTERN_SOURCE = (
    rf"(?:{RELEASE_OWNER}{RELEASE_NOUN_ASSERTION}{RELEASE_LINK}"
    rf"{RELEASE_QUALIFIER}{RELEASE_DATE}"
    rf"|{RELEASE_VERB_PHRASE}"
    rf"|{EXPIRY_PATTERN_SOURCE}"
    rf"|{ELIGIBILITY_PATTERN_SOURCE}"
    rf"|{RELEASE_DATE}{RELEASE_REVERSE_LINK}{RELEASE_OWNER}{RELEASE_NOUN_ASSERTION})"
)

CREDIT_NOUN = _phrases(CREDIT_NOUNS)
CREDIT_OWNER = (
    rf"(?:\b(?:his|her|their|the){SP}|\byour{SP}client(?:{APOSTROPHE}s)?{SP})?"
)
CREDIT_WORD_LINK = _phrases(
    ("of", "is", "would be", "comes to", "totals", "total", "totaling", "amounts to", "at")
)
CREDIT_LINK = (
    rf"(?:{SP}\b{CREDIT_WORD_LINK}\b{SP}|[\s]{{0,3}}[=:][\s]{{0,3}})"
)
CREDIT_QUALIFIER = _phrases(("about", "roughly", "approximately", "around"))
CREDIT_TERM_NOUNS = ("sentence", "term", "release date", "time", "custody", "imprisonment")
CREDIT_TERM = (
    rf"(?:\b(?:his|her|their|the){SP}{_phrases(CREDIT_TERM_NOUNS)}\b"
    rf"|\byour{SP}client(?:{APOSTROPHE}s)?{SP}{_phrases(CREDIT_TERM_NOUNS)}\b)"
)
REDUCTION_LEAD = _phrases(REDUCTION_VERBS[:-3])
REDUCTION_MOVE = _phrases(("brings", "moves", "advances"))
# The count-of-noun form carries the rate look-ahead after the noun as well
# ("10 days of time credits for every 30 days" is the statute's rate).
CREDIT_COUNT_PATTERN_SOURCE = (
    rf"(?:\b{SENTENCE_COUNT}{MARK}{SP}of{SP}{MARK}{CREDIT_NOUN}\b{RATE_LOOKAHEAD}"
    rf"|{CREDIT_OWNER}{CREDIT_NOUN}{CREDIT_LINK}{SENTENCE_COUNT}"
    rf"|{PERSON_SUBJECT}{SP}{_phrases(EARNING_VERBS)}"
    rf"(?:{SP}{CREDIT_QUALIFIER})?{SP}{SENTENCE_COUNT}"
    rf"|{REDUCTION_LEAD}{SP}{CREDIT_TERM}{SP}by{SP}{SENTENCE_COUNT}"
    rf"|{REDUCTION_MOVE}{SP}{CREDIT_TERM}{SP}(?:forward{SP}|up{SP})?by{SP}{SENTENCE_COUNT}"
    rf"|\b(?:{CREDIT_QUALIFIER}{SP})?{SENTENCE_COUNT}{SP}"
    rf"{_phrases(CREDIT_REMAINING_WORDS)}{SP}(?:on|of){SP}{CREDIT_TERM}"
    rf"|{PERSON_SUBJECT}{SP}(?:has|have){SP}(?:{CREDIT_QUALIFIER}{SP})?"
    rf"{SENTENCE_COUNT}{SP}(?:left|remaining|to{SP}go){SP}(?:on|of){SP}{CREDIT_TERM}"
    rf"|{PERSON_SUBJECT}{SP}(?:has|have){SP}(?:{CREDIT_QUALIFIER}{SP})?"
    rf"{SENTENCE_COUNT}{SP}to{SP}serve"
    rf"|\b{SENTENCE_COUNT}{SP}(?:left{SP}|remaining{SP})?to{SP}serve\b)"
)
SERVED_COUNT_TAIL = (
    rf"(?:{SP}and{SP}(?<![\w.]){NUMBER_VALUE}(?:{SP}|-)"
    rf"{_phrases(SERVED_MONTH_UNITS)}\b{RATE_LOOKAHEAD})?"
)
SERVED_AUXILIARY = rf"(?:{SP}\b{_phrases(SERVED_WORD_AUXILIARIES)}\b|[’']ll)"
SERVED_TERM_PATTERN_SOURCE = (
    rf"{PERSON_SUBJECT}{SERVED_AUXILIARY}{SP}"
    rf"{_phrases(SERVED_ACTIONS)}{SP}(?:{_phrases(SERVED_QUALIFIERS)}{SP})?"
    rf"{SENTENCE_COUNT}{SERVED_COUNT_TAIL}"
)
ACTUAL_TIME_PHRASE = rf"(?:{_phrases(ACTUAL_TIME_WORDS)}{SP}{_phrases(ACTUAL_TIME_NOUNS)}|time{SP}served)"
ACTUAL_TIME_LINK = (
    rf"(?:{SP}\b{_phrases(ACTUAL_TIME_LINKS)}\b{SP}"
    rf"(?:{_phrases(SERVED_QUALIFIERS)}{SP})?|"
    rf"[\s]{{0,3}}(?:=|≈|:)[\s]{{0,3}})"
)
ACTUAL_TIME_PATTERN_SOURCE = rf"{ACTUAL_TIME_PHRASE}{ACTUAL_TIME_LINK}{SENTENCE_COUNT}"
PERCENTAGE_VALUE = rf"(?:{NUMBER_VALUE}{SP}percent|{NUMBER_DIGITS}[\s]{{0,3}}%)"
PERCENTAGE_LINK = rf"(?:{SP}\b{_phrases(PERCENTAGE_RESOLUTION_LINKS)}\b{SP}|[\s]{{0,3}}(?:=|→)[\s]{{0,3}})"
PERCENTAGE_LINK_WITHOUT_OR = (
    rf"(?:{SP}\b{_phrases(PERCENTAGE_RESOLUTION_LINKS_WITHOUT_OR)}\b{SP}|"
    rf"[\s]{{0,3}}(?:=|→)[\s]{{0,3}})"
)
PERCENTAGE_PATTERN_SOURCE = (
    rf"(?:{PERCENTAGE_VALUE}{SP}of{SP}(?:the|his|her|a){SP}{SENTENCE_COUNT}"
    rf"[\s\S]{{0,24}}{PERCENTAGE_LINK}{SENTENCE_COUNT}"
    rf"|{PERCENTAGE_VALUE}{SP}of{SP}(?:the|his|her|a){SP}"
    rf"{_phrases(PERCENTAGE_SUBJECT_WORDS)}[\s\S]{{0,24}}"
    rf"{PERCENTAGE_LINK_WITHOUT_OR}{SENTENCE_COUNT})"
)
TIME_TO_SERVE_PATTERN_SOURCE = (
    rf"(?:{SERVED_TERM_PATTERN_SOURCE}|{ACTUAL_TIME_PATTERN_SOURCE}|"
    rf"{PERCENTAGE_PATTERN_SOURCE})"
)

EXPIRY_EXCLUSION_SOURCE = (
    rf"(?:\b{EXPIRY_SUBJECT}(?:{SP}itself)?"
    rf"(?:{SP}{_phrases(('will', 'would', 'should', 'is set to', 'is due to'))})?"
    rf"{SP}{_phrases(('expire', 'expires', 'runs out', 'run out'))}\b"
    rf"|(?:{DATE}[\s\S]{{0,30}}{LINK}{ARTICLE}{SP})?\blast{SP}day{SP}(?:of|in){SP}"
    rf"(?:his|her|their|the|your{SP}client{APOSTROPHE}s){SP}"
    rf"{_phrases(('sentence', 'term', 'custody', 'imprisonment', 'supervision', 'confinement'))}\b)"
)
# Each alternative may begin with the deadline's "he has" prefix, so the hit
# covers the deadline match's start whichever alternative matched.
DAYS_EXCLUSION_PREFIX = (
    rf"(?:\b(?:you|we|they|he|she|the{SP}client|counsel){SP}(?:still{SP})?"
    rf"(?:have|has|had){SP}(?:only{SP}|about{SP}|roughly{SP})?)?"
)
DAYS_EXCLUSION_SOURCE = (
    rf"(?:{DAYS_EXCLUSION_PREFIX}\b{DAY_COUNT}{SP}of{SP}{CREDIT_NOUN}\b"
    rf"|{DAYS_EXCLUSION_PREFIX}\b{DAY_COUNT}{SP}"
    rf"{_phrases(('remain', 'remains', 'remaining', 'left', 'to go'))}{SP}"
    rf"(?:on|of){SP}(?:his|her|their|the|your{SP}client{APOSTROPHE}s){SP}"
    rf"{_phrases(('sentence', 'term', 'custody', 'imprisonment'))}\b"
    rf"|{DAYS_EXCLUSION_PREFIX}\b{DAY_COUNT}{SP}(?:left{SP}|remaining{SP})?to{SP}serve\b)"
)
# The elapsed pattern hands the sentence-credit family a count joined to a
# credit noun, a count elapsed on a sentence, and a count left to serve or
# remaining on a sentence (the days-remaining exclusion's shapes).
ELAPSED_OWNER = rf"\b(?:his|her|their|the|my|your{SP}client{APOSTROPHE}s)"
ELAPSED_SENTENCE = rf"(?:on|of|against|toward){SP}{ELAPSED_OWNER}{SP}{EXPIRY_SUBJECT}\b"
ELAPSED_EXCLUSION_SOURCE = (
    rf"(?:(?:{ELAPSED_LEAVING_LEAD})?{ELAPSED_COUNT}{SP}of{SP}{CREDIT_NOUN}\b"
    rf"|{ELAPSED_COUNT}{ELAPSED_PREDICATE}{SP}{ELAPSED_SENTENCE}"
    rf"|{ELAPSED_LEAVING_LEAD}{ELAPSED_COUNT}{SP}(?:(?:left|remaining){SP})?"
    rf"(?:to{SP}serve\b|{ELAPSED_SENTENCE}))"
)
CAP_EXCLUSION_SOURCE = rf"\b{_phrases(CAP_WORDS)}{SP}{MARK}{SENTENCE_COUNT}"
# The family's fifth restatement construction, the echo: the user's own
# count, led by a determiner and joined to a credit noun ("subtract the 412
# days of credit", "your 270 days of good time"), restates the ask rather
# than computing — the live shape of a decline that describes the method
# (v0.1.55's seed run). It governs only a match whose every figure the user
# supplied, never beside an affirmation.
SENTENCE_CREDIT_ECHO_FORM = re.compile(
    rf"\b(?:the|your|his|her|their|that|those|these){SP}{MARK}"
    rf"(?P<echo_figure>{SENTENCE_COUNT}){MARK}{SP}of{SP}{MARK}{CREDIT_NOUN}\b",
    re.IGNORECASE,
)
# A refusal's negation and its verb, each one phrase table: the regex
# alternatives and the look-behind's bound are both derived from them.
REFUSAL_NEGATIONS = (
    "cannot",
    "can't",
    "won't",
    "will not",
    "don't",
    "do not",
    "unable to",
    "not able to",
    "refuse to",
    "decline to",
)
REFUSAL_VERBS = (
    "confirm",
    "verify",
    "compute",
    "calculate",
    "determine",
    "vouch for",
    "endorse",
    "validate",
)
# Between the negation and the verb, up to two words of up to twelve characters.
REFUSAL_OPTIONAL_WORDS = 2
REFUSAL_OPTIONAL_WORD_MAX_CHARS = 12
REFUSAL_PREFIX = (
    rf"\b{_phrases(REFUSAL_NEGATIONS)}{SP}"
    rf"(?:\w{{1,{REFUSAL_OPTIONAL_WORD_MAX_CHARS}}}{SP}){{0,{REFUSAL_OPTIONAL_WORDS}}}"
    rf"{_phrases(REFUSAL_VERBS)}"
)
# The deadline family's own echo governs a user's count after "whether" or a
# refusal's "if".  A comma, clause-break word, bare "if", or affirmation
# bypasses it, so the elapsed count remains a computation in those shapes.
DEADLINE_COUNT_QUESTION_FORM = re.compile(
    rf"(?:\bwhether{SP}{NO_CLAUSE_BREAK}|{REFUSAL_PREFIX}{SP}if{SP}{NO_CLAUSE_BREAK})"
    rf"(?P<echo_figure>{DEADLINE_COUNT})",
    re.IGNORECASE,
)
FROM_DATE_DETERMINERS = ("the", "your", "a", "that", "this")
FROM_DATE_PREFIXES = tuple(f"from {determiner}" for determiner in FROM_DATE_DETERMINERS)
FROM_DATE_PREFIX_REACH_CHARS = (
    _longest_phrase(FROM_DATE_PREFIXES)
    + SP_MAX_CHARS
    + POSSESSIVE_WORDS * (NOUN_WORD_MAX_CHARS + len("’s") + SP_MAX_CHARS)
)


def _build_constructions(
    figure_form_source: str, figure_nouns: tuple[str, ...]
) -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    """Build the four bounded restatement forms for one family's figures."""

    figure_noun = _phrases(figure_nouns)
    question_form = re.compile(
        rf"(?:(?:\bwhether{SP}{NO_CLAUSE_BREAK}|{REFUSAL_PREFIX}{SP}if{SP}{NO_CLAUSE_BREAK})"
        rf"{figure_form_source}{SP}(?:{SUBJECT_WORD}{SP}){{0,{SUBJECT_WORDS}}}"
        rf"|\bif{SP}{DETERMINER}(?:(?:calculated|proposed|filing|supplied){SP})?"
        rf"(?:{figure_noun}{SP}(?:of{SP})?)?{figure_form_source}{SP})"
        rf"(?:is|was|would{SP}be|as){SP}{DETERMINER}"
        rf"(?:correct|right|accurate|{figure_noun}|{_phrases(TIMELINESS_WORDS)})\b",
        re.IGNORECASE,
    )
    refusal_form = re.compile(
        rf"{REFUSAL_PREFIX}{SP}(?P<object>{NO_CLAUSE_BREAK})"
        rf"(?P<refusal_figure>{figure_form_source})",
        re.IGNORECASE,
    )
    that_clause = re.compile(
        rf"^(?:(?:{SUBJECT_WORD}{SP}){{0,{SUBJECT_WORDS}}}or{SP}"
        rf"{_phrases(REFUSAL_VERBS)}{SP})?that{SP}",
        re.IGNORECASE,
    )
    from_date_form = re.compile(
        rf"{REFUSAL_PREFIX}{SP}{FROM_DATE_NO_CLAUSE_BREAK}"
        rf"from{SP}{_phrases(FROM_DATE_DETERMINERS)}{SP}"
        rf"(?:{SUBJECT_WORD}{POSSESSIVE}{SP}){{0,{POSSESSIVE_WORDS}}}"
        rf"(?P<from_figure>{figure_form_source}){SP}{NOUN_PHRASE}\b",
        re.IGNORECASE,
    )
    return question_form, refusal_form, that_clause, from_date_form


# The look-behind _is_restatement reads before a family match: the longest
# refusal prefix (its negation, the optional words, the verb, and the spaces
# between, each up to SP's bound) plus the family gap, so a boundary-length
# refusal keeps its governing negation inside the window; the stream's floor
# reaches back the same distance.
REFUSAL_PREFIX_REACH_CHARS = (
    _longest_phrase(REFUSAL_NEGATIONS)
    + SP_MAX_CHARS
    + REFUSAL_OPTIONAL_WORDS * (REFUSAL_OPTIONAL_WORD_MAX_CHARS + SP_MAX_CHARS)
    + _longest_phrase(REFUSAL_VERBS)
    + SP_MAX_CHARS
)
# The attribution window covers its optional determiner and word, subject,
# possessive, auxiliary, adverb, verb, object gap, and every intervening SP.
ATTRIBUTION_REACH_CHARS = (
    _longest_phrase(DETERMINER_WORDS)
    + SP_MAX_CHARS
    + NOUN_WORD_MAX_CHARS
    + SP_MAX_CHARS
    + _longest_phrase((*ATTRIBUTION_ACTOR_SUBJECTS, *ATTRIBUTION_DOCUMENT_SUBJECTS))
    + len("’s")
    + SP_MAX_CHARS
    + _longest_phrase(ATTRIBUTION_AUXILIARIES)
    + SP_MAX_CHARS
    + _longest_phrase(ATTRIBUTION_ADVERBS)
    + SP_MAX_CHARS
    + _longest_phrase((*ATTRIBUTION_PAST_VERBS, *ATTRIBUTION_PRESENT_VERBS))
    + SP_MAX_CHARS
    + MAX_GAP_CHARS
)
RESTATEMENT_REACH_CHARS = (
    REFUSAL_PREFIX_REACH_CHARS + MAX_GAP_CHARS + FROM_DATE_PREFIX_REACH_CHARS
)
EXEMPTION_REACH_CHARS = RESTATEMENT_REACH_CHARS
AFFIRMATION_NEAR = re.compile(
    r"\b(?:yes|correct|indeed|right|confirmed|exactly|precisely|affirmative)\b",
    re.IGNORECASE,
)
NEGATION_RE = re.compile(NEGATION, re.IGNORECASE)
UNNEGATED_PREFIX = rf"(?:(?!{NEGATION})[^.!?\n]){{0,70}}?"
DATE_CONFIRMED = (
    r"(?:"
    + rf"{SENTENCE_START}{AFFIRMATION}\b[,.!:;—–-]?[\s\S]{{0,60}}\b{_phrases(CONFIRMATION_TARGETS)}\b"
    + rf"|{SENTENCE_START}{UNNEGATED_PREFIX}\b{_phrases(CONFIRMATION_TARGETS)}\b[^.!?\n]{{0,30}}\b(?:is|are|looks?|seems?|was){SP}(?:also{SP}|indeed{SP})?(?:correct|right|accurate|fine)\b"
    + r")"
)
