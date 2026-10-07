"""Render the office's upstream observation and pin notices."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final

from gideon.host.corpus import record
from gideon.host.report import Problem
from gideon.improvement.sections import (
    Context,
    Row,
    RowState,
    Scope,
    SectionReport,
    read_fix,
)

STATEMENT: Final[str] = record.WATCH_STATE_STATEMENT


def _utc_date(timestamp: str) -> date:
    return datetime.fromisoformat(timestamp).astimezone(UTC).date()


def parse_rows(lines: tuple[str, ...]) -> tuple[record.WatchState, ...] | Problem:
    """Count unreadable rows without echoing database output."""

    states = tuple(record.parse_watch_state(line) for line in lines)
    unreadable = sum(state is None for state in states)
    if unreadable:
        return Problem(
            f"metrics reader returned {unreadable} unreadable upstream rows", read_fix()
        )
    return tuple(state for state in states if state is not None)


def _row(state: record.WatchState) -> Row:
    row_state: RowState
    answered = state.newest_answered
    if answered is None:
        detail = "no answered upstream snapshot"
        row_state = "not yet measurable"
    else:
        pinned = (
            f"{state.pinned_date} ({state.pinned_label})"
            if state.pinned_label is not None else "none"
        )
        assert state.first_seen is not None
        detail = (
            f"newest upstream {answered.latest_label}, newest pinned {pinned}, "
            f"first sighting {_utc_date(state.first_seen)}"
        )
        row_state = "fired" if state.open else "not fired"
    if state.unanswered_since is not None:
        detail += (
            f", unanswered since {_utc_date(state.unanswered_since)} "
            f"({state.newest.detail})"
        )
    return Row(state.newest.source, row_state, detail)


@dataclass(frozen=True, slots=True)
class UpstreamSection:
    """Report each observed source's latest upstream and pinned dates."""

    name: str = "upstream"
    scope: Scope = "office"

    def render(self, context: Context) -> SectionReport | Problem:
        """Read the shared watch state once and render public facts only."""

        lines = context.query(STATEMENT)
        if isinstance(lines, Problem):
            return lines
        states = parse_rows(lines)
        if isinstance(states, Problem):
            return states
        newest = max(
            (datetime.fromisoformat(state.newest.observed_at) for state in states),
            default=None,
        )
        detail = (
            f"{len(states)} sources observed, "
            f"{sum(state.open for state in states)} open notices, "
            f"{sum(state.newest.outcome == 'unanswered' for state in states)} "
            "unanswered sources, newest observation "
            f"{newest.astimezone(UTC).date() if newest is not None else 'none'}"
        )
        return SectionReport(detail, tuple(_row(state) for state in states))


UPSTREAM_SECTION: Final[UpstreamSection] = UpstreamSection()
