"""Judge the run window and its deadline so execution stays inside its bound."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

QUIET_WINDOW_START_HOUR: Final[int] = 19
"""The quiet window opens at 19:00 office time."""

QUIET_WINDOW_END_HOUR: Final[int] = 6
"""The quiet window ends at 06:00 office time."""

WEEKEND_DAYS: Final[frozenset[int]] = frozenset({5, 6})
"""Saturday and Sunday are quiet-window days."""

DECISION_WINDOW_START_WEEKDAY: Final[int] = 4
"""Friday opens the decision window."""

DECISION_WINDOW_END_WEEKDAY: Final[int] = 0
"""Monday closes the decision window."""


@dataclass(frozen=True, slots=True)
class WindowJudgement:
    """A window's admission state, description, next opening, and deadline."""

    inside: bool
    description: str
    next_opening: datetime
    end: datetime


class WindowOverrun(Exception):
    """Signal that a run reached its deadline so its caller can record partial work."""

    def __init__(self, end: datetime) -> None:
        self.end = end
        super().__init__(f"evaluation window ended at {end.isoformat()}")


def _next_opening(local: datetime) -> datetime:
    opening = time(QUIET_WINDOW_START_HOUR)
    date = local.date()
    while True:
        if date.weekday() not in WEEKEND_DAYS:
            candidate = datetime.combine(date, opening, tzinfo=local.tzinfo)
            if candidate >= local:
                return candidate
        date += timedelta(days=1)


def _local_clock(clock: datetime, timezone_name: str) -> datetime:
    if clock.tzinfo is None or clock.utcoffset() is None:
        clock = clock.replace(tzinfo=UTC)
    return clock.astimezone(ZoneInfo(timezone_name))


def _next_weekday_morning(local: datetime) -> datetime:
    date = local.date()
    if local.time() >= time(QUIET_WINDOW_END_HOUR):
        date += timedelta(days=1)
    while date.weekday() in WEEKEND_DAYS:
        date += timedelta(days=1)
    return datetime.combine(
        date,
        time(QUIET_WINDOW_END_HOUR),
        tzinfo=local.tzinfo,
    )


def _decision_inside(local: datetime) -> bool:
    weekday = local.weekday()
    return (
        weekday in WEEKEND_DAYS
        or (
            weekday == DECISION_WINDOW_START_WEEKDAY
            and local.hour >= QUIET_WINDOW_START_HOUR
        )
        or (
            weekday == DECISION_WINDOW_END_WEEKDAY
            and local.hour < QUIET_WINDOW_END_HOUR
        )
    )


def _next_decision_opening(local: datetime, inside: bool) -> datetime:
    if inside:
        return local
    days = (DECISION_WINDOW_START_WEEKDAY - local.weekday()) % 7
    opening_date = local.date() + timedelta(days=days)
    opening = datetime.combine(
        opening_date,
        time(QUIET_WINDOW_START_HOUR),
        tzinfo=local.tzinfo,
    )
    if opening < local:
        opening += timedelta(days=7)
    return opening


def window_judgement(clock: datetime, timezone_name: str) -> WindowJudgement:
    """Return the quiet-window judgement and the next weekday deadline."""

    local = _local_clock(clock, timezone_name)
    weekday = local.strftime("%A")
    clock_text = local.strftime("%H:%M")
    weekend = local.weekday() in WEEKEND_DAYS
    after_hours = local.hour >= QUIET_WINDOW_START_HOUR or local.hour < QUIET_WINDOW_END_HOUR
    if weekend:
        description = f"weekend ({weekday})"
    elif after_hours:
        description = f"inside the quiet window ({clock_text} {timezone_name}, {weekday})"
    else:
        description = f"office hours ({clock_text} {timezone_name}, {weekday})"
    return WindowJudgement(
        weekend or after_hours,
        description,
        _next_opening(local),
        _next_weekday_morning(local),
    )


def decision_judgement(clock: datetime, timezone_name: str) -> WindowJudgement:
    """Return the weekend decision-window judgement and the next deadline."""

    local = _local_clock(clock, timezone_name)
    inside = _decision_inside(local)
    weekday = local.strftime("%A")
    clock_text = local.strftime("%H:%M")
    label = "weekend window" if inside else "outside the weekend window"
    description = f"{label} ({weekday} {clock_text} {timezone_name})"
    return WindowJudgement(
        inside,
        description,
        _next_decision_opening(local, inside),
        _next_weekday_morning(local),
    )


def deadline_checkpoint(clock: Callable[[], datetime], end: datetime) -> Callable[[], None]:
    """Return a check that raises when the injected clock reaches the deadline."""

    def checkpoint() -> None:
        if clock() >= end:
            raise WindowOverrun(end)

    return checkpoint
