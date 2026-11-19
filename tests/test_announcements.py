"""The announcement validation rules and the standing set."""

import os
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_backuplock import LockFake
from test_windowrecord import END, EVENT, KEY, RENDERED, START, FakeHost, reply

from gideon.host import backuplock, report, stack
from gideon.host.announcements import (
    ID_PATTERN,
    LEVELS,
    Announcement,
    command_announcement,
    command_holder,
    note_line,
    office_time,
    standing,
    window_announcements,
)
from gideon.host.render.worker import (
    METRICS_ROLE,
    WORKER_DATABASE_HOST,
    WORKER_DATABASE_NAME,
)
from gideon.host.sysio import PathLike
from gideon.host.windowrecord import Window

_NOTICE_ID = "moon-cafe-1"
_NOTICE_TEXT = "The moon cafe closes at 21:00 - reopening tomorrow."
_COMMAND_RECORD = backuplock.Record("fixture upgrade", 1234, EVENT.at)


class StandingHost(LockFake, FakeHost):
    """Serve a window event and, by default, no announcement locks."""

    def __init__(self, *, stdout: str = "") -> None:
        LockFake.__init__(self)
        FakeHost.__init__(self, stdout=stdout)
        self.files[backuplock.LOCK_TABLE] = ""

    def hold(self, lock: backuplock.Lock, record: backuplock.Record) -> None:
        inode = len(self.stats) + 1
        self.stats[lock.path] = os.stat_result((0, inode, 0, 1, 0, 0, 0, 0, 0, 0))
        self.files[lock.path] = record.to_json()
        self.files[backuplock.LOCK_TABLE] = self.read_text(backuplock.LOCK_TABLE) + (
            f"{inode}: FLOCK ADVISORY WRITE {record.pid} 00:00:{inode} 0 EOF\n"
        )


def window_query_argv(rendered_dir: PathLike) -> tuple[str, ...]:
    """The metrics role's command that reads the maintenance window record."""

    return tuple(stack.exec_argv(
        rendered_dir, WORKER_DATABASE_HOST, "psql", "-U", METRICS_ROLE,
        "-d", WORKER_DATABASE_NAME, "-X", "-v", "ON_ERROR_STOP=1",
        "-v", "VERBOSITY=sqlstate", "-tA", "-f", "-",
    ))


class Announcements(unittest.TestCase):
    def test_fixed_sentence_is_accepted_at_each_level(self) -> None:
        for level in LEVELS:
            with self.subTest(level=level):
                notice = Announcement(_NOTICE_ID, level, _NOTICE_TEXT, dismissible=True)
                self.assertEqual(notice.text, _NOTICE_TEXT)
                self.assertEqual(notice.level, level)

    def test_invalid_id_is_refused_without_the_text(self) -> None:
        for invalid_id in ("", "Moon-cafe", "moon_cafe", "moon/cafe", "moon cafe", "moon-café"):
            with self.subTest(invalid_id=invalid_id):
                with self.assertRaisesRegex(
                    ValueError, "lowercase letters, digits, and hyphens"
                ) as raised:
                    Announcement(invalid_id, LEVELS[0], _NOTICE_TEXT, dismissible=False)
                self.assertIn(repr(invalid_id), str(raised.exception))
                self.assertNotIn(_NOTICE_TEXT, str(raised.exception))

    def test_invalid_level_is_refused_without_the_text(self) -> None:
        with self.assertRaisesRegex(ValueError, "needs a level") as raised:
            Announcement(_NOTICE_ID, "interplanetary", _NOTICE_TEXT, dismissible=False)
        self.assertIn(_NOTICE_ID, str(raised.exception))
        self.assertNotIn(_NOTICE_TEXT, str(raised.exception))

    def test_invalid_text_is_refused_without_echoing_it(self) -> None:
        cases = (
            ("", "non-empty text"),
            (f"{_NOTICE_TEXT}\nThe moon bakery stays open.", "one line"),
            (f"{_NOTICE_TEXT}\n", "one line"),
            (f"{_NOTICE_TEXT}\u2028The moon bakery stays open.", "one line"),
            *(
                (f"{_NOTICE_TEXT} {character}", "Markdown punctuation")
                for character in "$\\`*_<[~"
            ),
            *(
                (f"{marker} {_NOTICE_TEXT}", "heading or list marker")
                for marker in ("#", "-", " 12.")
            ),
            (f"{_NOTICE_TEXT} https://moon-cafe.invalid", "free of URLs"),
        )
        for invalid_text, rule in cases:
            with self.subTest(invalid_text=invalid_text):
                with self.assertRaisesRegex(ValueError, rule) as raised:
                    Announcement(_NOTICE_ID, LEVELS[0], invalid_text, dismissible=False)
                self.assertIn(_NOTICE_ID, str(raised.exception))
                self.assertNotIn(_NOTICE_TEXT, str(raised.exception))


class OfficeTime(unittest.TestCase):
    def test_midnight_noon_and_office_date(self) -> None:
        zone = ZoneInfo("America/Denver")
        for hour, expected in (
            (6, "Friday, July 3 at 12:04 AM"),
            (18, "Friday, July 3 at 12:04 PM"),
            (5, "Thursday, July 2 at 11:04 PM"),
        ):
            with self.subTest(hour=hour):
                instant = datetime(2026, 7, 3, hour, 4, tzinfo=UTC)
                self.assertEqual(office_time(instant, zone), expected)

    def test_note_uses_each_endpoints_offset_across_a_clock_change(self) -> None:
        start = datetime(2025, 3, 9, 8, 30, tzinfo=UTC)
        end = start + timedelta(hours=1)
        window = Window(_NOTICE_ID, start, end, start - timedelta(days=1), "example-operator")
        zone = ZoneInfo("America/Denver")
        self.assertEqual(
            note_line(window, zone),
            "The next maintenance window is from Sunday, March 9 at 1:30 AM "
            "until Sunday, March 9 at 3:30 AM.",
        )
        [ahead] = window_announcements(window, window.recorded_at, zone)
        [under_way] = window_announcements(window, start, zone)
        self.assertEqual(
            ahead.text, "GIDEON will be unavailable for maintenance from Sunday, March 9 at 1:30 AM "
            "until Sunday, March 9 at 3:30 AM.",
        )
        self.assertEqual(
            under_way.text, "GIDEON is under maintenance and may be unavailable until Sunday, March 9 at 3:30 AM.",
        )

    def test_empty_note_is_the_whole_sentence(self) -> None:
        self.assertEqual(note_line(None, UTC), "No maintenance window is announced.")

    def test_sentences_accept_all_weekdays_and_months(self) -> None:
        weekdays: set[str] = set()
        months: set[str] = set()
        start = datetime(2026, 1, 1, 15, 0, tzinfo=UTC)
        for day in range(365):
            instant = start + timedelta(days=day)
            weekday, date = office_time(instant, UTC).split(", ")
            weekdays.add(weekday)
            months.add(date.split()[0])
            window = Window(_NOTICE_ID, instant - timedelta(hours=1), instant, start, "example-operator")
            [ahead] = window_announcements(window, window.starts_at - timedelta(hours=1), UTC)
            [under_way] = window_announcements(window, window.starts_at, UTC)
            for text in (note_line(window, UTC), ahead.text, under_way.text):
                self.assertTrue(text.endswith(f"until {weekday}, {date}."))
        self.assertEqual(weekdays, {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"})
        self.assertEqual(months, {
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        })


class Standing(unittest.TestCase):
    def test_no_event_withdrawal_and_end_all_have_no_standing_announcement(self) -> None:
        for payload, now in (
            ("", START), (reply(act="withdraw", starts_at=None, ends_at=None), START), (reply(), END),
        ):
            with self.subTest(payload=payload, now=now):
                host = StandingHost(stdout=payload)
                holder = command_holder(host)
                self.assertEqual(holder, backuplock.Holding(False, None))
                self.assertEqual(standing(host, RENDERED, holder=holder, now=now, timezone=UTC), ())
                [(argv, sql)] = host.calls
                self.assertEqual(tuple(argv), window_query_argv(RENDERED))
                self.assertIn("FROM public.maintenance_window_events", sql or "")

    def test_phases_have_distinct_ids_and_dismissal_policies(self) -> None:
        start = datetime(2026, 7, 3, 0, 0, tzinfo=UTC)
        end = start + timedelta(hours=12)
        host = StandingHost(stdout=reply(starts_at=start.isoformat(), ends_at=end.isoformat()))
        holder = command_holder(host)
        self.assertEqual(standing(host, RENDERED, holder=holder, now=start - timedelta(seconds=1), timezone=UTC), (
            Announcement(
                f"window-{KEY}-ahead", "info", "GIDEON will be unavailable for maintenance from "
                "Friday, July 3 at 12:00 AM until Friday, July 3 at 12:00 PM.", dismissible=True,
            ),
        ))
        self.assertEqual(standing(host, RENDERED, holder=holder, now=start, timezone=UTC), (
            Announcement(
                f"window-{KEY}-under-way", "warning", "GIDEON is under maintenance and may be unavailable "
                "until Friday, July 3 at 12:00 PM.", dismissible=False,
            ),
        ))

    def test_reused_numeric_id_cannot_reuse_a_dismissal_id(self) -> None:
        moved = "22222222-2222-4222-8222-222222222222"
        for key in (KEY, moved):
            host = StandingHost(stdout=reply(id=EVENT.id, key=key))
            holder = command_holder(host)
            for now, phase in ((EVENT.at, "ahead"), (START, "under-way")):
                with self.subTest(key=key, phase=phase):
                    result = standing(host, RENDERED, holder=holder, now=now, timezone=UTC)
                    assert isinstance(result, tuple)
                    self.assertEqual([item.id for item in result], [f"window-{key}-{phase}"])

    def test_unreadable_record_returns_a_problem(self) -> None:
        host = StandingHost(stdout="fictitious unreadable reply")
        host.hold(backuplock.UNAVAILABLE_LOCK, _COMMAND_RECORD)
        holder = command_holder(host)
        self.assertEqual(standing(host, RENDERED, holder=holder, now=START, timezone=UTC), report.Problem(
            "maintenance window reader returned an unreadable event.", f"Run {report.command('apply')}, then retry.",
        ))

    def test_primary_holder_precedes_nested_and_standing_uses_the_supplied_reading(self) -> None:
        host = StandingHost()
        nested = replace(_COMMAND_RECORD, command="fixture apply", pid=_COMMAND_RECORD.pid + 1)
        host.hold(backuplock.UNAVAILABLE_LOCK, _COMMAND_RECORD)
        host.hold(backuplock.UNAVAILABLE_NESTED_LOCK, nested)
        primary = command_holder(host)
        self.assertEqual(primary, backuplock.Holding(True, _COMMAND_RECORD))
        del host.stats[backuplock.UNAVAILABLE_LOCK.path]
        fallback = command_holder(host)
        self.assertEqual(fallback, backuplock.Holding(True, nested))

        with patch.object(host, "stat", wraps=host.stat) as read_lock:
            self.assertEqual(standing(host, RENDERED, holder=primary, now=START, timezone=UTC), (
                command_announcement(_COMMAND_RECORD, None, START, UTC),
            ))
            self.assertEqual(standing(host, RENDERED, holder=fallback, now=START, timezone=UTC), (
                command_announcement(nested, None, START, UTC),
            ))
            read_lock.assert_not_called()

    def test_command_stands_first_ahead_and_replaces_window_under_way(self) -> None:
        host = StandingHost(stdout=reply())
        host.hold(backuplock.UNAVAILABLE_LOCK, _COMMAND_RECORD)
        holder = command_holder(host)
        window = Window(KEY, START, END, EVENT.at, EVENT.actor)
        zone = ZoneInfo("America/Denver")
        ahead = standing(host, RENDERED, holder=holder, now=EVENT.at, timezone=zone)
        under_way = standing(host, RENDERED, holder=holder, now=START, timezone=zone)
        assert isinstance(ahead, tuple) and isinstance(under_way, tuple)
        command_ahead, window_ahead = ahead
        [command_under_way] = under_way
        self.assertEqual(command_ahead.text, "GIDEON is unavailable right now.")
        self.assertEqual(window_announcements(window, EVENT.at, zone), (window_ahead,))
        self.assertEqual(command_under_way.text, (
            f"GIDEON is unavailable right now, and maintenance may continue until {office_time(END, zone)}."
        ))
        self.assertEqual(command_under_way.id, command_ahead.id)
        for notice in (command_ahead, command_under_way):
            self.assertEqual(notice.level, "warning")
            self.assertFalse(notice.dismissible)

    def test_command_id_uses_the_instant_and_pid(self) -> None:
        original = command_announcement(_COMMAND_RECORD, None, START, UTC)
        for record in (
            replace(_COMMAND_RECORD, pid=_COMMAND_RECORD.pid + 1),
            replace(_COMMAND_RECORD, started=_COMMAND_RECORD.started + timedelta(microseconds=1)),
        ):
            with self.subTest(record=record):
                notice = command_announcement(record, None, START, UTC)
                self.assertNotEqual(notice.id, original.id)
                self.assertRegex(notice.id, ID_PATTERN)
        equivalent = replace(
            _COMMAND_RECORD, started=_COMMAND_RECORD.started.astimezone(ZoneInfo("America/Denver")),
        )
        self.assertEqual(command_announcement(equivalent, None, START, UTC), original)

    def test_unreadable_primary_record_leaves_the_window_as_it_stands(self) -> None:
        host = StandingHost(stdout=reply())
        host.hold(backuplock.UNAVAILABLE_LOCK, _COMMAND_RECORD)
        host.hold(backuplock.UNAVAILABLE_NESTED_LOCK, replace(_COMMAND_RECORD, pid=_COMMAND_RECORD.pid + 1))
        host.files[backuplock.UNAVAILABLE_LOCK.path] = ""
        holder = command_holder(host)
        self.assertEqual(holder, backuplock.Holding(True, None))
        window = Window(KEY, START, END, EVENT.at, EVENT.actor)
        self.assertEqual(
            standing(host, RENDERED, holder=holder, now=START, timezone=UTC),
            window_announcements(window, START, UTC),
        )

    def test_unreadable_lock_table_is_a_problem(self) -> None:
        host = StandingHost()
        host.hold(backuplock.UNAVAILABLE_LOCK, _COMMAND_RECORD)
        host.files[backuplock.LOCK_TABLE] = PermissionError("fixture access failure")
        holder = command_holder(host)
        self.assertIsInstance(holder, report.Problem)
        assert isinstance(holder, report.Problem)
        self.assertIn(backuplock.LOCK_TABLE, holder.fix)
        self.assertIs(standing(host, RENDERED, holder=holder, now=START, timezone=UTC), holder)
        self.assertEqual(host.calls, [])

    def test_standing_passes_the_window_read_bound(self) -> None:
        host = StandingHost()
        holder = command_holder(host)
        timeout = 2.5
        with patch.object(host, "run", wraps=host.run) as run:
            self.assertEqual(standing(host, RENDERED, holder=holder, now=START, timezone=UTC, timeout=timeout), ())
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs["timeout"], timeout)
