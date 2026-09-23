"""The office ``feedback`` section: the window's ratings counted by model.

A ``rated`` row is never ``fired``: a thumbs-down becomes a person's step only
through the monthly packet (improvement ticket 06), so the report's fired count
and ``status``'s waiting block pass these rows by.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from gideon.host.report import Problem
from gideon.improvement.feedback import FeedbackReading
from gideon.improvement.sections import Context, Row, RowState, Scope, SectionReport

FEEDBACK_WINDOW_DAYS: Final[int] = 30
RATED_STATE: Final[RowState] = "rated"
HEADER_DETAIL: Final[str] = "{ratings} ratings in the last {days} days, {listed} listed"
ROW_DETAIL: Final[str] = "up {up}, down {down}, newest {newest}"
_SECONDS_PER_DAY: Final[int] = 24 * 60 * 60
_TIME_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True, slots=True)
class ModelRatings:
    """The recent up/down counts and newest rating time for one model."""

    up: int
    down: int
    newest: int


def summarise(reading: FeedbackReading, now: float) -> dict[str, ModelRatings]:
    """Count the window's ratings by model id, in id order."""

    cutoff = now - FEEDBACK_WINDOW_DAYS * _SECONDS_PER_DAY
    totals: dict[str, ModelRatings] = {}
    for record in reading.records:
        if record.created_at < cutoff:
            continue
        prior = totals.get(record.model_id, ModelRatings(0, 0, record.created_at))
        totals[record.model_id] = ModelRatings(
            up=prior.up + (record.rating == "up"),
            down=prior.down + (record.rating == "down"),
            newest=max(prior.newest, record.created_at),
        )
    return dict(sorted(totals.items()))


@dataclass(frozen=True, slots=True)
class RatingsSection:
    """Report the ratings the frontend lists, one row per rated model."""

    name: str = "feedback"
    scope: Scope = "office"

    def render(self, context: Context) -> SectionReport | Problem:
        """Read the seam once; a refused read is the section's refusal."""

        reading = context.feedback()
        if isinstance(reading, Problem):
            return reading

        summaries = summarise(reading, context.now())
        ratings = sum(summary.up + summary.down for summary in summaries.values())
        listed = len(reading.records) + reading.skipped
        detail = HEADER_DETAIL.format(
            ratings=ratings,
            days=FEEDBACK_WINDOW_DAYS,
            listed=listed,
        )
        if reading.skipped:
            detail += f", {reading.skipped} unreadable"

        rows = tuple(
            Row(
                model_id,
                RATED_STATE,
                ROW_DETAIL.format(
                    up=summary.up,
                    down=summary.down,
                    newest=datetime.fromtimestamp(summary.newest, UTC).strftime(_TIME_FORMAT),
                ),
            )
            for model_id, summary in summaries.items()
        )
        return SectionReport(detail, rows)


FEEDBACK_SECTION: Final[RatingsSection] = RatingsSection()
