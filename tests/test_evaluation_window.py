"""Window boundaries give evaluation work a predictable end and next opening."""

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


def _morning(day: date, timezone_name: str = ZONE) -> datetime:
    return datetime.combine(
        day,
        time(window.QUIET_WINDOW_END_HOUR),
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
                _morning(monday + timedelta(days=1)),
            ),
            (
                datetime.combine(monday, time(window.QUIET_WINDOW_END_HOUR), tzinfo=ZoneInfo(ZONE)),
                False,
                f"office hours ({window.QUIET_WINDOW_END_HOUR:02d}:00 {ZONE}, {monday.strftime('%A')})",
                _opening(monday),
                _morning(monday + timedelta(days=1)),
            ),
            (
                datetime.combine(monday - timedelta(days=3), time(20, 0), tzinfo=ZoneInfo(ZONE)),
                True,
                f"inside the quiet window (20:00 {ZONE}, {(monday - timedelta(days=3)).strftime('%A')})",
                _opening(monday),
                _morning(monday),
            ),
            (
                datetime.combine(monday - timedelta(days=2), time(12, 0), tzinfo=ZoneInfo(ZONE)),
                True,
                f"weekend ({(monday - timedelta(days=2)).strftime('%A')})",
                _opening(monday),
                _morning(monday),
            ),
            (
                datetime.combine(monday - timedelta(days=1), time(23, 0), tzinfo=ZoneInfo(ZONE)),
                True,
                f"weekend ({(monday - timedelta(days=1)).strftime('%A')})",
                _opening(monday),
                _morning(monday),
            ),
            (
                datetime.combine(monday, time(window.QUIET_WINDOW_END_HOUR - 1), tzinfo=ZoneInfo(ZONE)),
                True,
                f"inside the quiet window ({window.QUIET_WINDOW_END_HOUR - 1:02d}:00 {ZONE}, {monday.strftime('%A')})",
                _opening(monday),
                _morning(monday),
            ),
        )
        for clock, inside, description, next_opening, end in cases:
            with self.subTest(clock=clock):
                result = window.window_judgement(clock, ZONE)
                self.assertEqual(result.inside, inside)
                self.assertEqual(result.description, description)
                self.assertEqual(result.next_opening, next_opening)
                self.assertEqual(result.end, end)

    def test_deadline_is_the_next_weekday_morning(self) -> None:
        monday = _monday()
        wednesday = monday + timedelta(days=2)
        friday = monday + timedelta(days=4)
        cases = (
            (datetime.combine(monday + timedelta(days=5), time(12), tzinfo=ZoneInfo(ZONE)), monday + timedelta(days=7)),
            (datetime.combine(wednesday, time(20), tzinfo=ZoneInfo(ZONE)), wednesday + timedelta(days=1)),
            (datetime.combine(wednesday, time(12), tzinfo=ZoneInfo(ZONE)), wednesday + timedelta(days=1)),
            (datetime.combine(friday, time(15), tzinfo=ZoneInfo(ZONE)), monday + timedelta(days=7)),
        )
        for clock, end_day in cases:
            with self.subTest(clock=clock):
                self.assertEqual(window.window_judgement(clock, ZONE).end, _morning(end_day))

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
        decision_result = window.decision_judgement(clock, zone_name)
        next_weekday = _next_weekday(transition_day)
        self.assertTrue(result.inside)
        self.assertEqual(result.description, f"weekend ({clock.strftime('%A')})")
        self.assertEqual(result.next_opening, _opening(next_weekday, zone_name))
        self.assertEqual(result.end, _morning(next_weekday, zone_name))
        # The opening is 19:00 local on the far side of the shift, so its UTC
        # offset differs from the clock's: the arithmetic is local, not fixed.
        self.assertNotEqual(result.next_opening.utcoffset(), clock.utcoffset())
        self.assertEqual(result.next_opening.hour, window.QUIET_WINDOW_START_HOUR)
        self.assertTrue(decision_result.inside)
        self.assertEqual(decision_result.next_opening, clock)
        self.assertEqual(decision_result.end, _morning(next_weekday, zone_name))
        self.assertNotEqual(decision_result.end.utcoffset(), clock.utcoffset())

    def test_naive_clock_is_read_as_utc(self) -> None:
        monday = _monday()
        naive = datetime.combine(monday, time(window.QUIET_WINDOW_END_HOUR))
        aware = naive.replace(tzinfo=UTC)
        self.assertEqual(
            window.window_judgement(naive, ZONE),
            window.window_judgement(aware, ZONE),
        )
        self.assertEqual(
            window.decision_judgement(naive, ZONE),
            window.decision_judgement(aware, ZONE),
        )


class DecisionWindow(unittest.TestCase):
    """The weekend boundary admits Friday night through Monday morning."""

    def test_edges_and_next_opening(self) -> None:
        monday = _monday()
        friday = monday + timedelta(days=4)
        saturday = friday + timedelta(days=1)
        wednesday = monday + timedelta(days=2)
        next_monday = monday + timedelta(days=7)
        cases = (
            (
                datetime.combine(friday, time(18, 59), tzinfo=ZoneInfo(ZONE)),
                False,
                f"outside the weekend window (Friday 18:59 {ZONE})",
                _opening(friday),
                _morning(next_monday),
            ),
            (
                datetime.combine(friday, time(19), tzinfo=ZoneInfo(ZONE)),
                True,
                f"weekend window (Friday 19:00 {ZONE})",
                datetime.combine(friday, time(19), tzinfo=ZoneInfo(ZONE)),
                _morning(next_monday),
            ),
            (
                datetime.combine(monday, time(5, 59), tzinfo=ZoneInfo(ZONE)),
                True,
                f"weekend window (Monday 05:59 {ZONE})",
                datetime.combine(monday, time(5, 59), tzinfo=ZoneInfo(ZONE)),
                _morning(monday),
            ),
            (
                datetime.combine(monday, time(6), tzinfo=ZoneInfo(ZONE)),
                False,
                f"outside the weekend window (Monday 06:00 {ZONE})",
                _opening(friday),
                _morning(monday + timedelta(days=1)),
            ),
            (
                datetime.combine(saturday, time(12), tzinfo=ZoneInfo(ZONE)),
                True,
                f"weekend window (Saturday 12:00 {ZONE})",
                datetime.combine(saturday, time(12), tzinfo=ZoneInfo(ZONE)),
                _morning(next_monday),
            ),
            (
                datetime.combine(wednesday, time(20), tzinfo=ZoneInfo(ZONE)),
                False,
                f"outside the weekend window (Wednesday 20:00 {ZONE})",
                _opening(friday),
                _morning(wednesday + timedelta(days=1)),
            ),
        )
        self.assertEqual(friday.weekday(), window.DECISION_WINDOW_START_WEEKDAY)
        self.assertEqual(monday.weekday(), window.DECISION_WINDOW_END_WEEKDAY)
        for clock, inside, description, next_opening, end in cases:
            with self.subTest(clock=clock):
                result = window.decision_judgement(clock, ZONE)
                self.assertEqual(result.inside, inside)
                self.assertEqual(result.description, description)
                self.assertEqual(result.next_opening, next_opening)
                self.assertEqual(result.end, end)

    def test_checkpoint_raises_at_and_after_the_deadline(self) -> None:
        end = _morning(_monday())
        readings = iter((end - timedelta(microseconds=1), end))
        checkpoint = window.deadline_checkpoint(lambda: next(readings), end)
        checkpoint()
        with self.assertRaises(window.WindowOverrun) as raised:
            checkpoint()
        self.assertEqual(raised.exception.end, end)

        past_checkpoint = window.deadline_checkpoint(
            lambda: end + timedelta(microseconds=1), end
        )
        with self.assertRaises(window.WindowOverrun) as raised:
            past_checkpoint()
        self.assertEqual(raised.exception.end, end)


if __name__ == "__main__":
    unittest.main()
