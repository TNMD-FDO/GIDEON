"""title: GIDEON arithmetic guardrail
version: 8
description: Enforces the no-model-arithmetic rule for filing deadlines, Sentencing Guidelines ranges, and sentence credit or release dates.
"""

# The arithmetic guardrail (spec §16, ADR-0006): the code enforcement of the
# no-model-arithmetic rule, one Filter global on every model, built as pattern
# families judged in FAMILIES order, each carrying its own refusal, its figure
# form and normaliser (the figures a user may supply, keyed canonically), the
# four restatement constructions built over that form, its confirmation
# context, and its own pattern-set version; the deadline family is first, with
# its fourth bounded days-elapsed pattern and date-or-day-count figure form,
# the Guidelines family second, and sentence credit third.  The deadline patterns
# carry bounded exclusions for sentence-expiry and credit-count wording, so
# those shapes are judged by the sentence-credit family.  Sentence credit's
# confirmation context displaces the deadline context, and the deadline's
# confirmation pattern yields to it.  A trip is replaced by the tripped
# family's refusal.  The
# outlet judges the finished answer; the stream hook runs inside the token
# stream with a lag window, so a matched span never reaches the screen
# (docs/research/owui-stream-hook.md for the pinned frontend's facts).  The
# generator's reasoning is withheld and never judged (slice-1 ticket 37):
# every reasoning delta leaves the hook empty but the turn's first, which
# carries one fixed space, because the pinned frontend opens its reasoning
# item — the "Thinking…" timer — for a truthy delta alone and absorbs an empty
# one without a trace, and the outlet stores no reasoning text on any path.
# This file runs inside the frontend's container and is imported by path in
# the unit tests, so it imports the standard library only — the one exception
# the trip writer's lazy import of the pinned image's Postgres driver, inside
# the function and guarded, so the file loads by path without it (slice-1
# ticket 11) — defines no Valves (nothing is tunable) and no toggle (a user
# could switch a toggleable Filter off), carries no requirements line (a pip
# install at load), and never contains the four import prefixes the
# frontend's rewriter replaces over the whole file — the word "from" followed
# by utils, apps, main, or config — since a rewritten comment would make the
# stored content differ from the manifest's (docs/research/owui-filter-function.md
# §4.2).  Its inlet holds two transport-only gates — the session gate and the
# branch gate — and neither reads a message (slice-1 tickets 09 and 43).
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Thread
from types import MappingProxyType

# The judge's default context: no figure supplied by the user for any family
# and no confirmation context matched — the stricter side.
NO_SUPPLIED: Mapping[str, frozenset[str]] = MappingProxyType({})

MAX_MATCH_CHARS = 150
MAX_GAP_CHARS = 80
# The restatement look-ahead settles the exemption after a family match.
RESTATEMENT_LOOKAHEAD_CHARS = 40
# Exclusion context is bounded by both the exemption look-behind and the
# restatement look-ahead so a rejected overlap is visible to stream judgement.
EXCLUSION_REACH_CHARS = 40
# The stream lag is the family match bound plus its restatement look-ahead.
LAG_CHARS = MAX_MATCH_CHARS + RESTATEMENT_LOOKAHEAD_CHARS
# The per-request stream state is kept under this metadata key.
STREAM_STATE_KEY = "gideon_arithmetic_guardrail"
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

# The Guidelines family draws the line at a range the model resolved or placed
# on the matter. A range restated under a governing construction, a court's
# past-tense finding, a statute's years, or a guideline's own term range
# passes; a bare months range trips nothing. An illustrative table cell is a
# positive at the cost of the example; a court's range in a case description
# is a seed control and ticket 45's attribution construction; the user's own
# figures are handled by the restatement constructions. "was" and "were" are
# never links or leads.
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
# The inlet's refusal for a user-role request arriving outside the chat
# window (the outlet's replacement reaches the window alone).
SESSION_REFUSAL = (
    "GIDEON answers only through its chat window. Open a chat and ask there."
)
# The inlet's refusal for a user-role request that names no preset branch.
BRANCH_REFUSAL = (
    "GIDEON answers only through one of its branches. Start a new chat and ask General."
)
# The evaluation identity (render/owui.py's EVAL_IDENTITY; a test holds the
# two equal): the one user-role account whose API calls the inlet lets through.
EVAL_IDENTITY_EMAIL = "gideon-eval@gideon.invalid"
ERROR_PATTERN_ID = "guardrail/error@1"
REPLACEMENT_MESSAGE_ID = "msg_000000000000000000000000"

GUARDRAIL_TRIPS_TABLE = "guardrail_trips"
GUARDRAIL_TRIPS_PARTITION_FUNCTION = "guardrail_trips_ensure_partition"
TRIP_DATABASE_HOST = "postgres"
TRIP_DATABASE_PORT = 5432
TRIP_DATABASE_NAME = "gideon"
TRIP_DATABASE_USER = "gideon_audit"
TRIP_PASSWORD_PATH = "/run/secrets/postgres_gideon_audit_password"
TRIP_CONNECT_TIMEOUT_SECONDS = 3
TRIP_STATEMENT_TIMEOUT_MILLISECONDS = 3000
TRIP_DRIVER_MODULE = "psycopg"
SOURCE_VOCABULARY = ("user", "eval")
UNKNOWN_BRANCH = "unknown"

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
DETERMINER = rf"(?:(?:the|your|my|his|her|their|a|that|this){SP})?"
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
GUIDELINES_FIGURE_SOURCE = rf"(?:{GUIDELINES_RANGE_SOURCE}|{GUIDELINES_PAIR_SOURCE})"
GUIDELINES_FIGURE = re.compile(
    rf"(?<![\w/]){GUIDELINES_FIGURE_SOURCE}(?![\w/])", re.IGNORECASE
)
GUIDELINES_RANGE_FORM = re.compile(
    rf"(?<![\w/]){GUIDELINES_RANGE_SOURCE}(?![\w/])", re.IGNORECASE
)


def _no_clause_break(max_chars: int) -> str:
    # A hyphen joining letters ("one-year", "post-conviction") is a word's
    # own; a spaced hyphen is a dash and breaks the clause as the dashes do.
    return (
        rf"(?:(?!\b{_phrases(CLAUSE_BREAK_WORDS)}\b)(?!\s-)"
        rf"[^.,;:!?\n—–]){{0,{max_chars}}}?"
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


@dataclass(frozen=True, slots=True)
class Pattern:
    """One versioned, bounded detector in a guardrail family."""

    pattern_id: str
    regex: re.Pattern[str]
    needs_context: bool = False
    exclusion: re.Pattern[str] | None = None
    yields_to: tuple[str, ...] = ()


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


@dataclass(frozen=True, slots=True)
class Trip:
    """The family and detector that caused a replacement."""

    family: str
    pattern_id: str


@dataclass(frozen=True, slots=True)
class TripRow:
    """The content-free row persisted for one guardrail trip."""

    branch: str
    family: str
    pattern_id: str
    source: str


class SessionRefusal(Exception):
    """The fixed refusal for a user request outside the chat window."""


class BranchRefusal(Exception):
    """The fixed refusal for a user request outside a preset branch."""


def _is_preset(model_entry: object) -> bool:
    """Return whether a model entry carries a non-empty base-model id."""

    # The entry is the process cache's discovered model; ``info`` is the record
    # merged onto it (with ``params`` deleted). A discovered model without a
    # record has no ``info`` in GIDEON's environment, or only ``meta`` if the
    # frontend's dormant default-metadata merge is enabled; the OpenAI router
    # swaps a preset for its base only after the Filters have run. This rule
    # therefore reads only ``info.base_model_id`` and never ``id`` or ``name``.
    if not isinstance(model_entry, Mapping):
        return False
    info = model_entry.get("info")
    if not isinstance(info, Mapping):
        return False
    base_model_id = info.get("base_model_id")
    return isinstance(base_model_id, str) and bool(base_model_id)


def _compiled(
    pattern_id: str,
    source: str,
    *,
    needs_context: bool = False,
    exclusion: str | None = None,
    yields_to: tuple[str, ...] = (),
) -> Pattern:
    return Pattern(
        pattern_id,
        re.compile(source, re.IGNORECASE),
        needs_context,
        re.compile(exclusion, re.IGNORECASE) if exclusion is not None else None,
        yields_to,
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
    + rf"|(?<![\w/]){GUIDELINES_FIGURE_SOURCE}(?![\w/])\s{{0,3}}\b(?:right|correct)\b\s{{0,3}}\?"
    + r")"
)
GUIDELINES_CONFIRMATION_CONTEXT = re.compile(
    rf"(?:{GUIDELINES_FIGURE_SOURCE}[\s\S]{{0,120}}{GUIDELINES_CONFIRMATION_ASK}"
    rf"|{GUIDELINES_CONFIRMATION_ASK}[\s\S]{{0,120}}{GUIDELINES_FIGURE_SOURCE})",
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


def rendered_texts(message: Mapping[str, object]) -> tuple[str, ...]:
    """Return the answer and non-reasoning output text parts as plain strings."""

    texts: list[str] = []
    content = message.get("content")
    if isinstance(content, str):
        texts.append(content)
    output = message.get("output")
    if not isinstance(output, list):
        return tuple(texts)
    for item in output:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") != "message":
            continue
        parts = item.get("content")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, Mapping):
                continue
            text = part.get("text")
            if isinstance(text, str):
                texts.append(text)
    return tuple(texts)


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
    zip(NUMBER_WORDS, [*map(str, range(1, 21)), *map(str, range(30, 100, 10))])
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


def _normalised_guidelines_figure(found: str) -> str:
    if GUIDELINES_RANGE_FORM.fullmatch(found) is not None:
        numbers = re.findall(r"\d{1,3}", found)
        if "life" in found.lower():
            return f"{int(numbers[0])}-life"
        return f"{int(numbers[0])}-{int(numbers[1])}"
    shorthand = re.fullmatch(
        rf"\s*(\d{{1,2}}){GUIDELINES_OPTIONAL_SPACE}/{GUIDELINES_OPTIONAL_SPACE}"
        rf"(VI|IV|V|III|II|I)\s*",
        found,
        re.IGNORECASE,
    )
    if shorthand is not None:
        return f"{int(shorthand.group(1))}/{shorthand.group(2).upper()}"
    level = _GUIDELINES_LEVEL_VALUE.search(found)
    category = _GUIDELINES_CATEGORY_VALUE.search(found)
    if level is None or category is None:
        return found
    category_value = category.group("value").upper()
    category_roman = _GUIDELINES_CATEGORY_ROMANS.get(category_value, category_value)
    return f"{int(level.group('value'))}/{category_roman}"


def normalized_figures(text: str) -> frozenset[str]:
    return frozenset(
        _normalised_guidelines_figure(match.group(0))
        for match in GUIDELINES_FIGURE.finditer(text)
    )


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
    ),
    pattern_set_version=1,
    figure_form=GUIDELINES_FIGURE,
    reverse_form=GUIDELINES_RANGE_FORM,
    figures=normalized_figures,
    figure_nouns=GUIDELINES_FIGURE_NOUNS,
    constructions=(
        GUIDELINES_QUESTION_FORM,
        GUIDELINES_REFUSAL_FORM,
        GUIDELINES_THAT_CLAUSE,
        GUIDELINES_FROM_DATE_FORM,
    ),
    confirmation=(GUIDELINES_CONFIRMATION_CONTEXT, "guidelines/range-confirmed@1"),
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


def _is_restatement(
    text: str, match: re.Match[str], family: Family
) -> bool:
    """Whether a family match is governed by a restating construction."""

    matched = match.group(0)
    reverse = family.reverse_form.match(matched) is not None
    before = text[max(0, match.start() - RESTATEMENT_REACH_CHARS) : match.start()]
    after = text[match.end() : match.end() + RESTATEMENT_LOOKAHEAD_CHARS]
    window = before + matched + after
    span_start, span_end = len(before), len(before) + len(matched)
    # A question form carries its predicate by construction and governs even a
    # reverse-shaped span ("whether June 5, 2027 is your deadline").
    question_form, refusal_form, that_clause, from_date_form = family.constructions
    for form in question_form.finditer(window):
        if form.start() < span_end and form.end() > span_start:
            return True
    if AFFIRMATION_NEAR.search(before[-40:] + matched):
        return False
    if family.echo_form is not None:
        for form in family.echo_form.finditer(window):
            figure_start, figure_end = form.span("echo_figure")
            if span_start <= figure_start and figure_end <= span_end:
                return AFFIRMATION_NEAR.search(after[:30]) is None
    # A refusal verb's object reaches the date with no clause break; a
    # reverse-shaped span ("June 5, 2027 is the deadline") only through the
    # refusal's that-clause, since outside it the shape asserts.
    for form in refusal_form.finditer(window):
        figure_start, figure_end = form.span("refusal_figure")
        if (
            span_start <= figure_start
            and figure_end <= span_end
            and (not reverse or that_clause.match(form.group("object")) is not None)
        ):
            return AFFIRMATION_NEAR.search(after[:30]) is None
    if reverse:
        return False  # a reverse-form assertion: "June 5, 2027 is your deadline"
    # The refusal verb's object through a "from the <date> <noun>" phrase.
    for form in from_date_form.finditer(window):
        figure_start, figure_end = form.span("from_figure")
        if span_start <= figure_start and figure_end <= span_end:
            return AFFIRMATION_NEAR.search(after[:30]) is None
    return False


@dataclass(frozen=True, slots=True)
class Constraint:
    """A context that must remain together before a stream can release it."""

    start: int
    end: int


def judge_text(
    text: str,
    supplied: Mapping[str, frozenset[str]] = NO_SUPPLIED,
    contexts: frozenset[str] = frozenset(),
    *,
    prefix: bool = False,
    since: int = 0,
) -> Trip | tuple[Constraint, ...] | None:
    """Return the first family pattern matching one rendered text.

    A match whose every figure the user supplied is skipped only when it is
    a locally scoped answer restatement (``_is_restatement``).  Any figure the
    user did not supply, any reverse-form assertion, and any affirmation beside
    the user's figure trips. Context-gated patterns are skipped while their
    family is absent from ``contexts``.

    In prefix mode the text is still being generated and ``since`` is the
    length judged with full context at the last call: a hit ending at or
    before it is skipped; a hit ending within the restatement look-ahead of
    the end is undecided and skipped.  The result is then the first trip, or
    else the release constraints of the exempt hits: the hit and its
    look-ahead, which the stream never splits.
    """

    constraints: list[Constraint] = []
    for family in FAMILIES:
        family_supplied = supplied.get(family.name, frozenset())
        for pattern in family.patterns:
            if pattern.needs_context and family.name not in contexts:
                continue
            if any(name in contexts for name in pattern.yields_to):
                continue
            for match in pattern.regex.finditer(text):
                if pattern.exclusion is not None:
                    # A hit of the pattern's exclusion that covers the match's
                    # start — the lead or the count the match begins with —
                    # makes the match another family's (or nobody's): no
                    # trip, no constraint.  A hit deeper inside the match (a
                    # second clause the deadline's greedy gap swallowed)
                    # rejects nothing, so the earlier family keeps its trip.
                    exclusion_start = max(0, match.start() - EXCLUSION_REACH_CHARS)
                    exclusion_end = min(len(text), match.end() + EXCLUSION_REACH_CHARS)
                    excluded = any(
                        exclusion_start + exclusion.start()
                        <= match.start()
                        < exclusion_start + exclusion.end()
                        for exclusion in pattern.exclusion.finditer(
                            text[exclusion_start:exclusion_end]
                        )
                    )
                    if excluded:
                        continue
                if match.end() <= since:
                    continue
                if prefix and match.end() + RESTATEMENT_LOOKAHEAD_CHARS > len(text):
                    continue
                found = family.figures(match.group(0))
                if not found or not found <= family_supplied:
                    return Trip(family.name, pattern.pattern_id)
                if not _is_restatement(text, match, family):
                    return Trip(family.name, pattern.pattern_id)
                if prefix:
                    constraints.append(
                        Constraint(
                            match.start(), match.end() + RESTATEMENT_LOOKAHEAD_CHARS
                        )
                    )
    return tuple(constraints) if prefix and constraints else None


def _last_assistant_position(messages: list[object]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == "assistant":
            return index
    return None


def last_assistant_message(messages: list[object]) -> dict[str, object] | None:
    """Return the last mutable assistant message, if the body has one."""

    position = _last_assistant_position(messages)
    if position is None:
        return None
    message = messages[position]
    return message if isinstance(message, dict) else None


def preceding_user_message(
    messages: list[object], assistant_position: int
) -> dict[str, object] | None:
    """Return the nearest user message before the assistant message."""

    for index in range(assistant_position - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == "user":
            return message
    return None


def message_context(
    messages: list[object], assistant_position: int
) -> tuple[Mapping[str, frozenset[str]], frozenset[str]]:
    """Read each family's supplied figures and confirmation context for one answer."""

    user_message = preceding_user_message(messages, assistant_position)
    user_content = user_message.get("content") if user_message is not None else None
    if not isinstance(user_content, str):
        user_content = ""
    supplied = {
        family.name: family.figures(user_content) for family in FAMILIES
    }
    contexts = {
        family.name
        for family in FAMILIES
        if family.confirmation is not None
        and user_content
        and family.confirmation[0].search(user_content)
    }
    for family in FAMILIES:
        if family.name in contexts:
            contexts.difference_update(family.confirmation_displaces)
    return supplied, frozenset(contexts)


def judge_rendered(
    texts: tuple[str, ...],
    answer: str,
    supplied: Mapping[str, frozenset[str]] = NO_SUPPLIED,
    contexts: frozenset[str] = frozenset(),
    *,
    prefix: bool = False,
    since: int = 0,
) -> Trip | tuple[Constraint, ...] | None:
    """Judge answer texts, then the answer's opening under the confirmation context.

    ``answer`` is the answer's own opening, never a window into it: the
    opening form is anchored at its start.  In prefix mode the opening is
    judged only once at least MAX_MATCH_CHARS of it exist ("Yes" may still
    become "Yesterday"); the lag holds more than that, so nothing of an answer
    is released before its opening is decided.
    """

    constraints: list[Constraint] = []
    for text in texts:
        result = judge_text(text, supplied, contexts, prefix=prefix, since=since)
        if isinstance(result, Trip):
            return result
        if result:
            constraints.extend(result)
    # The confirmation context is anchored at the answer's opening, never at
    # another output item.
    opening_decided = not prefix or len(answer) >= MAX_MATCH_CHARS
    if opening_decided and AFFIRMATION_OPEN.search(answer[:MAX_MATCH_CHARS]):
        # The bare opening names no target: the first family in FAMILIES order
        # whose context matched takes it.
        for family in FAMILIES:
            if family.name in contexts and family.confirmation is not None:
                return Trip(family.name, family.confirmation[1])
    return tuple(constraints) if prefix and constraints else None


def judge_message(
    message: Mapping[str, object], messages: list[object], assistant_position: int
) -> Trip | None:
    """Read the body's context and judge every rendered assistant text (the outlet's judgement)."""

    supplied, contexts = message_context(messages, assistant_position)
    answer = message.get("content")
    result = judge_rendered(
        rendered_texts(message),
        answer if isinstance(answer, str) else "",
        supplied,
        contexts,
    )
    return result if isinstance(result, Trip) else None


def judge_floor(since: int) -> int:
    """The start of the window a stream judges: every hit still decidable starts after ``since`` less
    the match bound, and its exemption context reaches back EXEMPTION_REACH_CHARS at most."""

    return max(0, since - MAX_MATCH_CHARS - EXEMPTION_REACH_CHARS)


def _new_text_state() -> dict[str, object]:
    return {
        "text": "",
        "released": 0,
        "decided": 0,
        "constraints": [],
        "finished": False,
    }


class StreamState(dict[str, object]):
    """The per-request stream state, kept on ``__metadata__`` under STREAM_STATE_KEY.

    A dict subclass, so the frontend's metadata stays a mapping tree, whose
    ``repr`` and ``str`` are content-free: the pinned frontend formats the
    whole request with ``%s`` into a DEBUG log line, and no character of the
    stream or of the user's dates may reach a log (spec §19.4).  The content
    entry holds its accumulated string, released length, decided length, and
    release constraints; the state also holds the placeholder flag, trip,
    finished flag, and inlet stash.
    """

    def __init__(
        self,
        supplied: Mapping[str, frozenset[str]],
        confirmation: frozenset[str],
        *,
        branch: str | None = None,
        source: str = "user",
    ) -> None:
        super().__init__(
            {
                "content": _new_text_state(),
                "placeholder_sent": False,
                "trip": None,
                "finished": False,
                "supplied": {
                    name: sorted(figures) for name, figures in supplied.items()
                },
                "confirmation": sorted(confirmation),
                "branch": branch,
                "source": source,
            }
        )

    def __repr__(self) -> str:
        entry = self.get("content")
        if isinstance(entry, dict):
            text = entry.get("text")
            length = len(text) if isinstance(text, str) else 0
            state = "finished" if entry.get("finished") else "open"
            content = f"{length}/{entry.get('released')}/{entry.get('decided')}/{state}"
        else:
            content = "invalid"
        return (
            f"StreamState(content={content}, placeholder_sent={self.get('placeholder_sent')}, "
            f"tripped={self.get('trip') is not None}, finished={self.get('finished')})"
        )

    __str__ = __repr__


def _stream_state(value: object) -> dict[str, object] | None:
    """The value as the stream's state, or ``None`` when it is not one."""

    keys = (
        "content",
        "placeholder_sent",
        "trip",
        "finished",
        "supplied",
        "confirmation",
        "branch",
        "source",
    )
    if isinstance(value, dict) and all(key in value for key in keys):
        return value
    return None


def _stream_entry(state: dict[str, object], field: str) -> dict[str, object]:
    entry = state.get(field)
    if not isinstance(entry, dict):
        raise TypeError("invalid stream text state")
    return entry


def _record_stream_trip(state: dict[str, object], trip: Trip) -> None:
    """Record the stream's trip once; the texts are discarded from here on."""

    if state.get("trip") is not None:
        return
    state["trip"] = {"family": trip.family, "pattern_id": trip.pattern_id}
    branch = state.get("branch")
    source = state.get("source")
    try:
        record_trip(
            trip,
            branch if isinstance(branch, str) else None,
            source if isinstance(source, str) else "user",
        )
    except Exception:  # noqa: BLE001, S110 - trip recording cannot affect the refusal, and nothing is logged.
        pass
    entry = _stream_entry(state, "content")
    entry["text"] = ""
    entry["constraints"] = []


Judge = Callable[..., "Trip | tuple[Constraint, ...] | None"]


class StreamCheck:
    """The bounded release of one streamed text: judge the window, release all but the tail.

    The mechanism knows no family — the judge and the field are parameters —
    so another bounded-regex check (the citation stamp, ticket 14) can copy
    it.  A released hit always carries the context that exempted it: the lag
    point never lands inside a constraint the judge reported.
    """

    def __init__(self, state: dict[str, object], field: str, *, judge: Judge) -> None:
        self.state = state
        self.field = field
        self._judge_function = judge

    def _supplied(self) -> dict[str, frozenset[str]]:
        supplied = self.state.get("supplied")
        if not isinstance(supplied, dict):
            raise TypeError("invalid stream stash")
        result: dict[str, frozenset[str]] = {}
        for name, figures in supplied.items():
            if not isinstance(name, str) or not isinstance(figures, list) or not all(
                isinstance(figure, str) for figure in figures
            ):
                raise TypeError("invalid stream stash")
            result[name] = frozenset(figures)
        return result

    def _confirmation(self) -> frozenset[str]:
        confirmation = self.state.get("confirmation")
        if not isinstance(confirmation, list) or not all(
            isinstance(name, str) for name in confirmation
        ):
            raise TypeError("invalid stream confirmation")
        return frozenset(confirmation)

    def _entry(self) -> tuple[dict[str, object], str, int, int]:
        entry = _stream_entry(self.state, self.field)
        text, released, decided = (
            entry.get("text"),
            entry.get("released"),
            entry.get("decided"),
        )
        if (
            not isinstance(text, str)
            or not isinstance(released, int)
            or not isinstance(decided, int)
        ):
            raise TypeError("invalid stream text state")
        return entry, text, released, decided

    @staticmethod
    def _constraints(entry: dict[str, object]) -> list[Constraint]:
        stored = entry.get("constraints")
        if not isinstance(stored, list):
            raise TypeError("invalid stream constraints")
        return [Constraint(int(item[0]), int(item[1])) for item in stored]

    def _judge(
        self, text: str, decided: int, *, prefix: bool
    ) -> Trip | tuple[Constraint, ...] | None:
        floor = judge_floor(decided)
        opening = text[:MAX_MATCH_CHARS]
        result = self._judge_function(
            (text[floor:],),
            opening,
            self._supplied(),
            self._confirmation(),
            prefix=prefix,
            since=max(0, decided - floor),
        )
        if isinstance(result, tuple):
            return tuple(
                Constraint(item.start + floor, item.end + floor) for item in result
            )
        return result

    def append(self, delta: str) -> str:
        """Append generated text and return what may now be released."""

        if self.state.get("trip") is not None:
            return ""
        entry, text, _, _ = self._entry()
        entry["text"] = text + delta
        return self.judge()

    def judge(self) -> str:
        """Judge the bounded window and return the text that may now be released."""

        if self.state.get("trip") is not None:
            return ""
        entry, text, released, decided = self._entry()
        result = self._judge(text, decided, prefix=True)
        if isinstance(result, Trip):
            _record_stream_trip(self.state, result)
            return ""
        fresh = tuple(result) if isinstance(result, tuple) else ()
        # Constraints persist until release has passed them; a decided hit is
        # not re-judged, but its context still travels with it.
        active = {(item.start, item.end): item for item in self._constraints(entry)}
        for item in fresh:
            active[(item.start, item.end)] = item
        constraints = [item for item in active.values() if item.end > released]
        entry["decided"] = max(decided, len(text) - RESTATEMENT_LOOKAHEAD_CHARS)
        target = max(0, len(text) - LAG_CHARS)
        moved = True
        while moved:
            moved = False
            for item in constraints:
                if item.start < target < item.end:
                    target = max(released, item.start)
                    moved = True
        target = max(released, min(target, len(text)))
        entry["constraints"] = [
            [item.start, item.end] for item in constraints if item.end > target
        ]
        entry["released"] = target
        return text[released:target]

    def finish(self) -> str:
        """Judge the complete text in ordinary mode and return its held tail."""

        if self.state.get("trip") is not None:
            return ""
        entry, text, released, decided = self._entry()
        if entry.get("finished"):
            return ""
        result = self._judge(text, decided, prefix=False)
        if isinstance(result, Trip):
            _record_stream_trip(self.state, result)
            return ""
        entry["released"] = len(text)
        entry["decided"] = len(text)
        entry["constraints"] = []
        entry["finished"] = True
        return text[released:]


_TEXT_KEYS = ("reasoning", "reasoning_content", "thinking", "content")
_REASONING_KEYS = ("reasoning", "reasoning_content", "thinking")


def _refusal_chunk(answer_released: bool, refusal: str) -> dict[str, object]:
    refusal = (REFUSAL_SEPARATOR if answer_released else "") + refusal
    return {
        "choices": [{"index": 0, "delta": {"content": refusal}, "finish_reason": None}]
    }


def _clear_text(event: object) -> object:
    """After a trip: a recognised chunk's structure kept and its text removed; anything else dropped.

    A falsy return drops the chunk on both of the frontend's paths, so a shape
    the hook cannot scrub (a Responses-API event, a non-dict) never carries
    model text past the refusal.
    """

    if not isinstance(event, dict):
        return None
    if "choices" not in event:
        return event if "selected_model_id" in event or "error" in event else None
    choices = event.get("choices")
    if not isinstance(choices, list):
        return None
    for choice in choices:
        if not isinstance(choice, dict):
            return None
        delta = choice.get("delta")
        if delta is not None and not isinstance(delta, dict):
            return None
        if isinstance(delta, dict):
            for key in _TEXT_KEYS:
                if key in delta:
                    delta[key] = ""
    return event


def _stream_tail(state: dict[str, object], field: str) -> str:
    entry = _stream_entry(state, field)
    text, released = entry.get("text"), entry.get("released")
    if (
        not isinstance(text, str)
        or not isinstance(released, int)
        or not 0 <= released <= len(text)
    ):
        raise TypeError("invalid stream tail")
    return text[released:]


def _message_output_item(
    message: dict[str, object],
) -> tuple[list[object], dict[str, object]]:
    output = message.get("output")
    if not isinstance(output, list):
        output = []
        message["output"] = output
    for item in reversed(output):
        if isinstance(item, dict) and item.get("type") == "message":
            return output, item
    item = {
        "type": "message",
        "id": REPLACEMENT_MESSAGE_ID,
        "status": "completed",
        "role": "assistant",
        "content": [],
    }
    output.append(item)
    return output, item


def _append_output_text(item: dict[str, object], tail: str) -> None:
    parts = item.get("content")
    if not isinstance(parts, list):
        parts = []
        item["content"] = parts
    for part in reversed(parts):
        if (
            isinstance(part, dict)
            and part.get("type") == "output_text"
            and isinstance(part.get("text"), str)
        ):
            part["text"] += tail
            return
    parts.append({"type": "output_text", "text": tail})


def _append_stream_tails(message: dict[str, object], state: dict[str, object]) -> None:
    content_tail = _stream_tail(state, "content")
    if not content_tail:
        return
    _, message_item = _message_output_item(message)
    content = message.get("content")
    if not isinstance(content, str):
        raise TypeError("invalid streamed message content")
    message["content"] = content + content_tail
    _append_output_text(message_item, content_tail)


def _scrub_reasoning(message: dict[str, object]) -> None:
    """Empty substantive reasoning text while retaining an empty timer item."""

    for key in ("reasoning", "reasoning_content"):
        value = message.get(key)
        if isinstance(value, str) and value not in ("", REASONING_PLACEHOLDER):
            message[key] = ""
    output = message.get("output")
    if not isinstance(output, list):
        return
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        parts = item.get("content")
        if not isinstance(parts, list):
            continue
        text_parts = [
            part
            for part in parts
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        if not text_parts or all(
            part["text"] in ("", REASONING_PLACEHOLDER) for part in text_parts
        ):
            continue
        for part in text_parts:
            part["text"] = ""


def _trip_chunk(state: dict[str, object], event: object) -> object:
    """The chunk that carries the refusal: the event's delta rewritten, or a fresh chunk when the event
    has no usable choice."""

    content = _stream_entry(state, "content")
    released = content.get("released")
    answer_released = isinstance(released, int) and released > 0
    stored_trip = state.get("trip")
    refusal = _refusal_for(
        stored_trip.get("family") if isinstance(stored_trip, dict) else None
    )
    if isinstance(event, dict):
        choices = event.get("choices")
        if (
            isinstance(choices, list)
            and len(choices) == 1
            and isinstance(choices[0], dict)
        ):
            choice = choices[0]
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                delta = {}
                choice["delta"] = delta
            for key in _TEXT_KEYS:
                if key in delta:
                    delta[key] = ""
            delta["content"] = (REFUSAL_SEPARATOR if answer_released else "") + refusal
            return event
    return _refusal_chunk(answer_released, refusal)


def _filter_chunk(state: dict[str, object], event: object) -> object:
    """Withhold, release, or refuse one chunk (docs/research/owui-stream-hook.md §5, §8)."""

    if state.get("trip") is not None:
        return _clear_text(event)
    if not isinstance(event, dict):
        raise TypeError("unrecognised stream event")
    if "choices" not in event:
        if "type" in event:
            raise TypeError(
                "unrecognised stream event"
            )  # a Responses-API event: not withholdable
        if "selected_model_id" in event or "error" in event:
            # The frontend's own announcement, or an upstream failure: nothing to
            # withhold; on an error the texts stay unfinished for the outlet's flush.
            return event
        raise TypeError("unrecognised stream event")
    choices = event["choices"]
    if not isinstance(choices, list):
        raise TypeError("unrecognised stream choices")
    if not choices:
        return event  # the usage chunk
    if len(choices) != 1 or not isinstance(choices[0], dict):
        raise TypeError("unrecognised stream choices")
    choice = choices[0]
    delta = choice.get("delta")
    if delta is None:
        delta = {}
    if not isinstance(delta, dict):
        raise TypeError("unrecognised stream delta")
    texts = {key: delta[key] for key in _TEXT_KEYS if key in delta}
    if any(not isinstance(value, str) for value in texts.values()):
        raise TypeError("unrecognised stream text")
    reasoning_keys = [key for key in _REASONING_KEYS if key in texts]
    if len(reasoning_keys) > 1:
        raise TypeError("ambiguous stream reasoning")
    reasoning_key = reasoning_keys[0] if reasoning_keys else None
    finish = choice.get("finish_reason") is not None
    content = StreamCheck(state, "content", judge=judge_rendered)
    out_content = ""
    if "content" in texts:
        out_content += content.append(texts["content"])
    if finish:
        out_content += content.finish()
        state["finished"] = True
    if state.get("trip") is not None:
        return _trip_chunk(state, event)
    for key in texts:
        delta[key] = ""
    if reasoning_key is not None:
        if state.get("placeholder_sent") is not True:
            delta[reasoning_key] = REASONING_PLACEHOLDER
            state["placeholder_sent"] = True
        else:
            delta[reasoning_key] = ""
    if out_content:
        delta["content"] = out_content
    if delta or "delta" in choice:
        choice["delta"] = delta
    return event


def _existing_output_message_id(message: Mapping[str, object]) -> str:
    output = message.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, Mapping) or item.get("type") != "message":
                continue
            identifier = item.get("id")
            if isinstance(identifier, str) and re.fullmatch(
                r"msg_[0-9a-f]{24}", identifier
            ):
                return identifier
    return REPLACEMENT_MESSAGE_ID


def replace_message(message: dict[str, object], refusal: str) -> None:
    """Replace the answer and structured output with one finished message item."""

    message["content"] = refusal
    message["output"] = [
        {
            "type": "message",
            "id": _existing_output_message_id(message),
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": refusal}],
        }
    ]


def dispatch_trip_row(row: TripRow) -> None:
    """Start the trip writer without making the Filter wait for the database."""

    Thread(target=write_trip_row, args=(row,), daemon=True).start()


def write_trip_row(row: TripRow, connect: Callable[..., object] | None = None) -> None:
    """Write one trip row, dropping every local or database failure."""

    try:
        with open(TRIP_PASSWORD_PATH, encoding="utf-8") as password_file:
            password = password_file.read().rstrip("\r\n")
        if connect is None:
            import psycopg

            connect = psycopg.connect
        with connect(
            host=TRIP_DATABASE_HOST,
            port=TRIP_DATABASE_PORT,
            dbname=TRIP_DATABASE_NAME,
            user=TRIP_DATABASE_USER,
            password=password,
            connect_timeout=TRIP_CONNECT_TIMEOUT_SECONDS,
            options=f"-c statement_timeout={TRIP_STATEMENT_TIMEOUT_MILLISECONDS}",
        ) as connection:
            connection.execute(f"SELECT {GUARDRAIL_TRIPS_PARTITION_FUNCTION}(now())")
            connection.execute(
                f"INSERT INTO {GUARDRAIL_TRIPS_TABLE} "
                "(branch, family, pattern_id, source) VALUES (%s, %s, %s, %s)",
                (row.branch, row.family, row.pattern_id, row.source),
            )
    except Exception:  # noqa: BLE001 - the writer is intentionally silent and never raises.
        return


def record_trip(trip: Trip, branch: str | None, source: str) -> None:
    """Dispatch one content-free trip row without affecting the refusal."""

    try:
        row = TripRow(
            branch if branch is not None else UNKNOWN_BRANCH,
            trip.family,
            trip.pattern_id,
            source if source in SOURCE_VOCABULARY else SOURCE_VOCABULARY[0],
        )
        dispatch_trip_row(row)
    except Exception:  # noqa: BLE001 - trip recording cannot affect the refusal.
        return


class Filter:
    """Global arithmetic guardrail Filter for Open WebUI."""

    def inlet(
        self,
        body: object,
        __user__: Mapping[str, object] | None = None,
        __metadata__: Mapping[str, object] | None = None,
        __model__: Mapping[str, object] | None = None,
    ) -> object:
        if not isinstance(__user__, Mapping) or not isinstance(__metadata__, Mapping):
            raise SessionRefusal(SESSION_REFUSAL)
        model = body.get("model") if isinstance(body, Mapping) else None
        branch = model if isinstance(model, str) else None
        source = "eval" if __user__.get("email") == EVAL_IDENTITY_EMAIL else "user"
        if isinstance(__metadata__, dict):
            # The stream's stash, for every turn the outlet will judge (admins
            # and the eval identity included): each family's canonical figures
            # and the names of families whose confirmation context matched.
            messages = body.get("messages") if isinstance(body, Mapping) else None
            if not isinstance(messages, list):
                messages = []
            supplied, contexts = message_context(messages, len(messages))
            __metadata__[STREAM_STATE_KEY] = StreamState(
                supplied, contexts, branch=branch, source=source
            )
        role = __user__.get("role")
        email = __user__.get("email")
        if role == "admin" or email == EVAL_IDENTITY_EMAIL:
            return body
        if (
            role == "user"
            and not __metadata__.get("session_id")
            and not __metadata__.get("chat_id")
        ):
            raise SessionRefusal(SESSION_REFUSAL)
        if role == "user" and not _is_preset(__model__):
            raise BranchRefusal(BRANCH_REFUSAL)
        return body

    def stream(self, event: object, __metadata__: object = None) -> object:
        """Withhold, release, or refuse one Chat Completions chunk; the hook never raises.

        The pinned frontend drops a raising hook's chunk and keeps streaming on
        the browser path and tears the whole response down on the API path
        (docs/research/owui-stream-hook.md §6), so every failure here is the
        refusal in-stream and the stream's text discarded from there on.
        """

        if not isinstance(__metadata__, dict):
            return _refusal_chunk(False, DEADLINE_REFUSAL)
        state = _stream_state(__metadata__.get(STREAM_STATE_KEY))
        if state is None:
            # No stash (a request the inlet never saw): an empty stash judges
            # every date as the model's own, the stricter side.
            state = StreamState({}, frozenset(), branch=None, source="user")
            __metadata__[STREAM_STATE_KEY] = state
        try:
            return _filter_chunk(state, event)
        except Exception:  # noqa: BLE001 - the guardrail fails closed on every internal error.
            _record_stream_trip(state, Trip(DEADLINE_FAMILY.name, ERROR_PATTERN_ID))
            return _trip_chunk(state, event)

    def outlet(self, body: object, __metadata__: object = None) -> object:
        """Judge the finished message, or consume the stream's verdict; the state never outlives the turn."""

        try:
            stream_state = (
                _stream_state(__metadata__.get(STREAM_STATE_KEY))
                if isinstance(__metadata__, dict)
                else None
            )
            if not isinstance(body, Mapping):
                return body
            messages = body.get("messages")
            if not isinstance(messages, list):
                return body
            assistant_position = _last_assistant_position(messages)
            if assistant_position is None:
                return body
            message = messages[assistant_position]
            if not isinstance(message, dict):
                return body
            should_record = False
            try:
                _scrub_reasoning(message)
                stored_trip = (
                    stream_state.get("trip") if stream_state is not None else None
                )
                if isinstance(stored_trip, dict):
                    # The stream tripped: the released prefix and the in-stream
                    # refusal become the refusal alone, no re-judgement.
                    family = stored_trip.get("family")
                    pattern_id = stored_trip.get("pattern_id")
                    if not isinstance(family, str) or not isinstance(pattern_id, str):
                        raise TypeError("invalid stream trip")
                    trip = Trip(family, pattern_id)
                else:
                    if (
                        stream_state is not None
                        and stream_state.get("finished") is False
                    ):
                        # An upstream that closed without a finish chunk: the held
                        # tails join the body before the whole is judged.
                        _append_stream_tails(message, stream_state)
                    trip = judge_message(message, messages, assistant_position)
                    if trip is None:
                        return body
                    should_record = True
                replace_message(message, _refusal_for(trip.family))
            except Exception:  # noqa: BLE001 - the guardrail fails closed on every internal error.
                trip = Trip("deadline", ERROR_PATTERN_ID)
                replace_message(message, DEADLINE_REFUSAL)
                should_record = True
            if should_record:
                branch = (
                    stream_state.get("branch") if stream_state is not None else None
                )
                if not isinstance(branch, str):
                    model = body.get("model")
                    branch = model if isinstance(model, str) else None
                source = (
                    stream_state.get("source") if stream_state is not None else "user"
                )
                if not isinstance(source, str):
                    source = "user"
                try:
                    record_trip(trip, branch, source)
                except Exception:  # noqa: BLE001 - trip recording cannot affect the refusal.
                    return body
            return body
        finally:
            if isinstance(__metadata__, dict):
                __metadata__.pop(STREAM_STATE_KEY, None)
