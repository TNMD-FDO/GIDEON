"""The office ``guardrail`` section: the window's trips beside the thumbs-downs
given in the chats they tripped in, joined by chat id, ids and figures alone.

A thumbs-down in a tripped chat is a user's report of a false trip, which a
person reads and rules on at the monthly review. Its rows are ``rated`` or
``not fired`` and never ``fired``, so the report's fired count and ``status``'s
waiting block pass them by. The eval identity's deliberate trips are outside it.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from gideon.guardrail.writer import (
    GUARDRAIL_TRIPS_TABLE,
    SOURCE_VOCABULARY,
    TRIP_CHAT_ID_PATTERN,
)
from gideon.host.report import Problem
from gideon.improvement import ratings
from gideon.improvement.feedback import FeedbackReading
from gideon.improvement.ratings import RATED_STATE
from gideon.improvement.sections import (
    READ_FIX,
    Context,
    Row,
    RowState,
    Scope,
    SectionReport,
)

TRIPS_STATEMENT: Final[str] = (
    f"SELECT family, pattern_id, chat_id, count(*) FROM {GUARDRAIL_TRIPS_TABLE} "
    f"WHERE at >= '{{cutoff}}' AND source = '{SOURCE_VOCABULARY[0]}' "
    "GROUP BY family, pattern_id, chat_id "
    "ORDER BY family, pattern_id, chat_id"
)
# A family name or a pattern id (``deadline/days-elapsed@1``) as the reader
# prints it; anything else is an unreadable row, never quoted.
_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_./@-]+")
NOT_FIRED_STATE: Final[RowState] = "not fired"
PARSE_PROBLEM: Final[str] = "metrics reader returned {count} unreadable trip rows"
HEADER_DETAIL: Final[str] = (
    "{trips} trips in the last {days} days, {rated} rated down in tripped chats, "
    "{elsewhere} rated down in other chats, {unkeyed} trips without a chat id"
)
PATTERN_DETAIL: Final[str] = "{trips} trips, {rated} rated down"
REPORT_DETAIL: Final[str] = "message {message_id}, down {created_at}, {pattern_ids}"


@dataclass(frozen=True, slots=True)
class TripCount:
    """The count for one family, pattern, and chat in the query window."""

    family: str
    pattern_id: str
    chat_id: str | None
    trips: int


@dataclass(frozen=True, slots=True)
class PatternTrips:
    """The trip and joined rating counts for one pattern id."""

    pattern_id: str
    trips: int
    rated_down: int


@dataclass(frozen=True, slots=True)
class Report:
    """One down rating joined to the patterns that tripped in its chat."""

    chat_id: str
    message_id: str
    created_at: int
    pattern_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Review:
    """The ordered pattern summaries and down ratings in the trip window."""

    patterns: tuple[PatternTrips, ...]
    reports: tuple[Report, ...]
    trips: int
    elsewhere: int
    unkeyed: int


def _cutoff(now: float) -> datetime:
    return datetime.fromtimestamp(now, UTC) - timedelta(days=ratings.FEEDBACK_WINDOW_DAYS)


def statement(now: float) -> str:
    """Build the trip query with a UTC cutoff literal from the caller's clock,
    never the server's, so both sides of the join share one window."""

    return TRIPS_STATEMENT.format(cutoff=_cutoff(now).strftime(ratings.TIME_FORMAT))


def parse_rows(lines: tuple[str, ...]) -> tuple[TripCount, ...] | Problem:
    """Parse the reader's grouped rows; any unreadable row refuses the read,
    naming how many and never quoting one."""

    parsed: list[TripCount] = []
    unreadable = 0
    for line in lines:
        columns = line.split("|")
        if len(columns) != 4:
            unreadable += 1
            continue
        family, pattern_id, chat_text, trips_text = columns
        if (
            not _NAME_PATTERN.fullmatch(family)
            or not _NAME_PATTERN.fullmatch(pattern_id)
            or (chat_text and not TRIP_CHAT_ID_PATTERN.fullmatch(chat_text))
            or not trips_text.isascii()
            or not trips_text.isdecimal()
            or int(trips_text) < 1
        ):
            unreadable += 1
            continue
        parsed.append(TripCount(family, pattern_id, chat_text or None, int(trips_text)))
    if unreadable:
        return Problem(PARSE_PROBLEM.format(count=unreadable), READ_FIX)
    return tuple(parsed)


def review(
    trips: tuple[TripCount, ...], reading: FeedbackReading, now: float
) -> Review:
    """Join recent down ratings to trips by chat id, without reading text."""

    cutoff = _cutoff(now)
    pattern_trips: dict[str, int] = {}
    pattern_rated: dict[str, int] = {}
    chat_patterns: dict[str, set[str]] = {}
    trip_total = 0
    unkeyed = 0
    for trip in trips:
        trip_total += trip.trips
        pattern_trips[trip.pattern_id] = (
            pattern_trips.get(trip.pattern_id, 0) + trip.trips
        )
        if trip.chat_id is None:
            unkeyed += trip.trips
        else:
            chat_patterns.setdefault(trip.chat_id, set()).add(trip.pattern_id)

    reports: list[Report] = []
    elsewhere = 0
    for record in reading.records:
        if (
            record.rating != "down"
            or datetime.fromtimestamp(record.created_at, UTC) < cutoff
        ):
            continue
        pattern_ids = chat_patterns.get(record.chat_id)
        if not pattern_ids:
            elsewhere += 1
            continue
        ordered_patterns = tuple(sorted(pattern_ids))
        reports.append(
            Report(
                record.chat_id,
                record.message_id,
                record.created_at,
                ordered_patterns,
            )
        )
        for pattern_id in ordered_patterns:
            pattern_rated[pattern_id] = pattern_rated.get(pattern_id, 0) + 1

    patterns = tuple(
        PatternTrips(pattern_id, count, pattern_rated.get(pattern_id, 0))
        for pattern_id, count in sorted(pattern_trips.items())
    )
    ordered_reports = tuple(
        sorted(reports, key=lambda item: (item.chat_id, item.created_at, item.message_id))
    )
    return Review(patterns, ordered_reports, trip_total, elsewhere, unkeyed)


def _rows(result: Review) -> tuple[Row, ...]:
    patterns = tuple(
        Row(
            item.pattern_id,
            RATED_STATE if item.rated_down else NOT_FIRED_STATE,
            PATTERN_DETAIL.format(trips=item.trips, rated=item.rated_down),
        )
        for item in result.patterns
    )
    reports = tuple(
        Row(
            item.chat_id,
            RATED_STATE,
            REPORT_DETAIL.format(
                message_id=item.message_id,
                created_at=datetime.fromtimestamp(item.created_at, UTC).strftime(
                    ratings.TIME_FORMAT
                ),
                pattern_ids=", ".join(item.pattern_ids),
            ),
        )
        for item in result.reports
    )
    return patterns + reports


@dataclass(frozen=True, slots=True)
class GuardrailSection:
    """Render grouped trip counts and their joined down ratings."""

    name: str = "guardrail"
    scope: Scope = "office"

    def render(self, context: Context) -> SectionReport | Problem:
        """Read trips, then ratings, and return ids and figures only."""

        now = context.now()
        trip_lines = context.query(statement(now))
        if isinstance(trip_lines, Problem):
            return trip_lines
        trip_counts = parse_rows(trip_lines)
        if isinstance(trip_counts, Problem):
            return trip_counts
        reading = context.feedback()
        if isinstance(reading, Problem):
            return reading

        result = review(trip_counts, reading, now)
        detail = HEADER_DETAIL.format(
            trips=result.trips,
            days=ratings.FEEDBACK_WINDOW_DAYS,
            rated=len(result.reports),
            elsewhere=result.elsewhere,
            unkeyed=result.unkeyed,
        )
        if reading.skipped:
            detail += f", {reading.skipped} unreadable"
        return SectionReport(detail, _rows(result))


TRIPS_SECTION: Final[GuardrailSection] = GuardrailSection()
