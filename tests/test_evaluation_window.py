"""Quiet-window judgement contracts from spec §18.5."""

import unittest
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from gideon.evaluation import window

ZONE = "Etc/UTC"


def _monday() -> date:
    current = date(2099, 1, 1)
    while current.weekday() != 0:
        current += timedelta(days=1)
    return current


def _opening(day: date, timezone_name: str = ZONE) -> datetime:
    return datetime.combine(
        day,
        time(window.QUIET_WINDOW_START_HOUR),
        tzinfo=ZoneInfo(timezone_name),
    )


def _next_weekday(day: date) -> date:
    candidate = day + timedelta(days=1)
    while candidate.weekday() in window.WEEKEND_DAYS:
        candidate += timedelta(days=1)
    return candidate


class QuietWindow(unittest.TestCase):
    """The window's boundaries and calendar transitions are deterministic."""

    def test_weekday_edges_and_weekend_report_the_next_opening(self) -> None:
        monday = _monday()
        cases = (
            (
                datetime.combine(monday, time(window.QUIET_WINDOW_START_HOUR), tzinfo=ZoneInfo(ZONE)),
                True,
                f"inside the quiet window ({window.QUIET_WINDOW_START_HOUR:02d}:00 {ZONE}, {monday.strftime('%A')})",
                _opening(monday),
            ),
            (
                datetime.combine(monday, time(window.QUIET_WINDOW_END_HOUR), tzinfo=ZoneInfo(ZONE)),
                False,
                f"office hours ({window.QUIET_WINDOW_END_HOUR:02d}:00 {ZONE}, {monday.strftime('%A')})",
                _opening(monday),
            ),
            (
                datetime.combine(monday - timedelta(days=3), time(20, 0), tzinfo=ZoneInfo(ZONE)),
                True,
                f"inside the quiet window (20:00 {ZONE}, {(monday - timedelta(days=3)).strftime('%A')})",
                _opening(monday),
            ),
            (
                datetime.combine(monday - timedelta(days=2), time(12, 0), tzinfo=ZoneInfo(ZONE)),
                True,
                f"weekend ({(monday - timedelta(days=2)).strftime('%A')})",
                _opening(monday),
            ),
            (
                datetime.combine(monday - timedelta(days=1), time(23, 0), tzinfo=ZoneInfo(ZONE)),
                True,
                f"weekend ({(monday - timedelta(days=1)).strftime('%A')})",
                _opening(monday),
            ),
            (
                datetime.combine(monday, time(window.QUIET_WINDOW_END_HOUR - 1), tzinfo=ZoneInfo(ZONE)),
                True,
                f"inside the quiet window ({window.QUIET_WINDOW_END_HOUR - 1:02d}:00 {ZONE}, {monday.strftime('%A')})",
                _opening(monday),
            ),
        )
        for clock, inside, description, next_opening in cases:
            with self.subTest(clock=clock):
                result = window.window_judgement(clock, ZONE)
                self.assertEqual(result.inside, inside)
                self.assertEqual(result.description, description)
                self.assertEqual(result.next_opening, next_opening)

    def test_daylight_saving_transition_keeps_the_local_window(self) -> None:
        zone_name = "America/Chicago"
        zone = ZoneInfo(zone_name)
        first = datetime(2099, 1, 1, 12, tzinfo=zone)
        transition_day = next(
            (first + timedelta(days=offset)).date()
            for offset in range(366)
            if (first + timedelta(days=offset)).utcoffset()
            != (first + timedelta(days=offset + 1)).utcoffset()
        )
        clock = datetime.combine(transition_day, time(12), tzinfo=zone)
        result = window.window_judgement(clock, zone_name)
        next_weekday = _next_weekday(transition_day)
        self.assertTrue(result.inside)
        self.assertEqual(result.description, f"weekend ({clock.strftime('%A')})")
        self.assertEqual(result.next_opening, _opening(next_weekday, zone_name))
        # The opening is 19:00 local on the far side of the shift, so its UTC
        # offset differs from the clock's: the arithmetic is local, not fixed.
        self.assertNotEqual(result.next_opening.utcoffset(), clock.utcoffset())
        self.assertEqual(result.next_opening.hour, window.QUIET_WINDOW_START_HOUR)

    def test_naive_clock_is_read_as_utc(self) -> None:
        monday = _monday()
        naive = datetime.combine(monday, time(window.QUIET_WINDOW_END_HOUR))
        aware = naive.replace(tzinfo=UTC)
        self.assertEqual(
            window.window_judgement(naive, ZONE),
            window.window_judgement(aware, ZONE),
        )


if __name__ == "__main__":
    unittest.main()
