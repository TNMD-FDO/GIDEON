"""Judge whether an evaluation run is inside the quiet window."""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

QUIET_WINDOW_START_HOUR: Final[int] = 19
"""The quiet window opens at 19:00 office time (§18.5)."""

QUIET_WINDOW_END_HOUR: Final[int] = 6
"""The quiet window ends at 06:00 office time (§18.5)."""

WEEKEND_DAYS: Final[frozenset[int]] = frozenset({5, 6})
"""Saturday and Sunday are quiet-window days (§18.5)."""


@dataclass(frozen=True, slots=True)
class WindowJudgement:
    """The quiet-window verdict, content-free description, and next opening."""

    inside: bool
    description: str
    next_opening: datetime


def _next_opening(local: datetime) -> datetime:
    opening = time(QUIET_WINDOW_START_HOUR)
    date = local.date()
    while True:
        if date.weekday() not in WEEKEND_DAYS:
            candidate = datetime.combine(date, opening, tzinfo=local.tzinfo)
            if candidate >= local:
                return candidate
        date += timedelta(days=1)


def window_judgement(clock: datetime, timezone_name: str) -> WindowJudgement:
    """Return the quiet-window judgement for *clock* in *timezone_name*."""

    if clock.tzinfo is None or clock.utcoffset() is None:
        clock = clock.replace(tzinfo=UTC)
    local = clock.astimezone(ZoneInfo(timezone_name))
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
    return WindowJudgement(weekend or after_hours, description, _next_opening(local))
