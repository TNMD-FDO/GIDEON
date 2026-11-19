"""Maintenance window commands over a fake site, lock, and event record."""

import contextlib
import io
import json
import os
import subprocess
import unittest
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID
from zoneinfo import ZoneInfo

from test_backup import FakeHost as BackupHost

from gideon.cli import build_parser
from gideon.host import backuplock, report, site, window
from gideon.host.sysio import Command, PathLike

SITE_PATH = "/fictitious-site/site.yaml"
RENDERED = "/fictitious-rendered"
SITE = (Path(__file__).parent / "fixtures/site/second-office.yaml").read_text(encoding="utf-8")
CONFIG = site.load_site_text(SITE).config
assert CONFIG is not None
ZONE = ZoneInfo(CONFIG.office.timezone)
LOCK = backuplock.WINDOW_LOCK.path
NOW = datetime(2025, 1, 10, 19, tzinfo=UTC)
ACTOR = "example-operator"
START = "2025-01-13 12:00"
END = "2025-01-13 13:00"


class FakeHost(BackupHost):
    """Keep the rows and expose which calls were covered by the box lock."""

    def __init__(self) -> None:
        super().__init__(files={SITE_PATH: SITE})
        self.events: list[dict[str, object]] = []
        self.operations: list[tuple[str, bool]] = []
        self.release_count = 0
        self.newest_error = False
        self.append_error = False
        self.hide_read_back = False
        self.read_back_error = False
        self.before_newest: Callable[[], None] | None = None

    def release_lock(self, path: PathLike) -> None:
        assert os.fspath(path) in self.locks
        super().release_lock(path)
        self.release_count += 1

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        assert input is not None
        assert "psql" in argv and RENDERED in argv
        values: dict[str, str] = {}
        for line in input.splitlines():
            if line.startswith("\\set "):
                _, name, value = line.split(" ", 2)
                values[name] = value[1:-1]
        locked = LOCK in self.locks
        command = list(argv)
        if "INSERT INTO public.maintenance_window_events" in input:
            self.operations.append(("append", locked))
            if self.append_error:
                return subprocess.CompletedProcess(command, 3, "", "fictitious diagnostic")
            self.events.append({
                "id": len(self.events) + 1, "key": values["v_key"], "at": NOW.isoformat(),
                "act": values["v_act"], "actor": values["v_actor"],
                "starts_at": values["v_starts_at"] or None, "ends_at": values["v_ends_at"] or None,
            })
            return subprocess.CompletedProcess(command, 0, "", "")

        assert "FROM public.maintenance_window_events" in input
        if "WHERE key" in input:
            self.operations.append(("landed", locked))
            if self.read_back_error:
                return subprocess.CompletedProcess(command, 3, "", "fictitious diagnostic")
            rows = [] if self.hide_read_back else [row for row in self.events if row["key"] == values["v_key"]]
        else:
            self.operations.append(("newest", locked))
            if self.before_newest is not None:
                self.before_newest()
            if self.newest_error:
                return subprocess.CompletedProcess(command, 3, "", "psql:<stdin>:5: ERROR:  42P01\n")
            rows = self.events
        return subprocess.CompletedProcess(command, 0, json.dumps(rows[-1]) + "\n" if rows else "", "")


def invoke(
    host: FakeHost, subcommand: str, *flags: str, now: datetime = NOW, actor: str | None = ACTOR,
) -> tuple[int, str, str]:
    args = build_parser().parse_args(["window", subcommand, *flags])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        if subcommand == "show":
            code = window.run_window_show(args, host=host, rendered_dir=RENDERED, site_path=SITE_PATH, now=now)
        else:
            run = window.run_window_set if subcommand == "set" else window.run_window_withdraw
            code = run(args, host=host, rendered_dir=RENDERED, site_path=SITE_PATH, now=now, actor=actor)
    return code, out.getvalue(), err.getvalue()


def record_window(
    host: FakeHost, start: str = START, end: str = END, *, now: datetime = NOW, actor: str | None = ACTOR,
) -> tuple[int, str, str]:
    return invoke(host, "set", "--start", start, "--end", end, now=now, actor=actor)


class SetWindow(unittest.TestCase):
    def test_first_event_records_the_instant_and_actor_under_one_claim(self) -> None:
        host = FakeHost()
        code, out, err = record_window(host, START.replace(" ", "T"))
        self.assertEqual((code, err), (0, ""))
        [event] = host.events
        self.assertEqual(UUID(str(event["key"])).version, 4)
        self.assertEqual(event["act"], "set")
        self.assertEqual(event["actor"], ACTOR)
        for field, minute in (("starts_at", START), ("ends_at", END)):
            expected = datetime.fromisoformat(minute).replace(tzinfo=ZONE).astimezone(UTC)
            self.assertEqual(event[field], expected.isoformat())
        first, second = out.splitlines()
        self.assertTrue(first.startswith("window: ok"))
        self.assertIn(f"office timezone {ZONE.key}, newest event none", first)
        self.assertTrue(second.startswith("record: ok"))
        self.assertIn(f"event {event['key']}", second)
        self.assertIn("from Monday, January 13 at 12:00 PM until Monday, January 13 at 1:00 PM", second)
        self.assertEqual(host.operations, [("newest", True), ("append", True), ("landed", True)])
        self.assertEqual(host.locks, {})
        self.assertEqual(host.release_count, 1)

    def test_running_window_can_move_to_a_later_day_and_read_ahead(self) -> None:
        host = FakeHost()
        self.assertEqual(record_window(host, "2025-01-10 11:30", "2025-01-10 13:30")[0], 0)
        self.assertIn("is under way", invoke(host, "show")[1])
        first_key = host.events[-1]["key"]
        code, out, err = record_window(host)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(host.events), 2)
        self.assertEqual(host.events[0]["key"], first_key)
        self.assertNotEqual(host.events[-1]["key"], first_key)
        self.assertIn(f"moved {first_key}", out)
        self.assertIn(f"{host.events[-1]['key']} is ahead", invoke(host, "show")[1])

    def test_working_day_warning_skips_the_weekend(self) -> None:
        for start, warns in (
            ("2025-01-11 14:00", True),
            ("2025-01-13 11:59", True),
            (START, False),
            ("2025-01-13 12:01", False),
            ("2025-01-10 11:00", True),
        ):
            with self.subTest(start=start):
                host = FakeHost()
                code, out, err = record_window(host, start)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(len(host.events), 1)
                warning = "announce a maintenance window at least one working day ahead"
                self.assertEqual(warning in out, warns)
                self.assertEqual("docs/runbooks/install-upgrade.md §2" in out, warns)

    def test_bad_spans_refuse_with_fixes_and_write_nothing(self) -> None:
        for start, end, now, detail, flag in (
            ("fictitious text", END, NOW, "must be an office time", "--start"),
            ("2025-02-30 12:00", END, NOW, "must be an office time", "--start"),
            (START, END + ":00", NOW, "must be an office time", "--end"),
            (START, END + "+00:00", NOW, "must be an office time", "--end"),
            (START, START, NOW, "--end must be after --start", "--end"),
            ("2025-01-10 11:00", "2025-01-10 12:00", NOW, "--end must be after now", "--end"),
            ("2025-04-10 13:01", "2025-04-10 14:00", NOW, "more than 90 days", "--start"),
            ("2025-03-09 02:30", "2025-03-09 04:00", NOW, "does not exist", "--start"),
            ("2025-03-09 01:30", "2025-03-09 02:30", NOW, "does not exist", "--end"),
            ("2025-11-02 01:30", "2025-11-02 03:00", datetime(2025, 11, 1, tzinfo=UTC), "occurs twice", "--start"),
        ):
            with self.subTest(start=start, end=end), patch.object(report, "_installed_form", True):
                host = FakeHost()
                code, out, err = record_window(host, start, end, now=now)
                self.assertEqual((code, err), (1, ""))
                self.assertIn("window: refuse", out)
                self.assertIn(detail, out)
                self.assertIn("Fix: Run gideon window set", out)
                self.assertIn(flag, out.split("Fix:")[1])
                self.assertNotIn("fictitious text", out)
                self.assertEqual((host.events, host.operations, host.locks), ([], [], {}))

    def test_start_exactly_at_the_limit_is_accepted(self) -> None:
        host = FakeHost()
        self.assertEqual(record_window(host, "2025-04-10 13:00", "2025-04-10 14:00")[0], 0)

    def test_actor_defaults_to_sudo_or_the_effective_account(self) -> None:
        for env, expected in (({"SUDO_USER": "example-invoker"}, "example-invoker"), ({}, "example-effective")):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), patch(
                "gideon.host.window.pwd.getpwuid", return_value=SimpleNamespace(pw_name="example-effective"),
            ) as lookup:
                host = FakeHost()
                self.assertEqual(record_window(host, actor=None)[0], 0)
                self.assertEqual(host.events[0]["actor"], expected)
                if env:
                    lookup.assert_not_called()
                else:
                    lookup.assert_called_once_with(0)

    def test_actor_control_characters_refuse_without_echo_or_write(self) -> None:
        for character in ("\n", "\x00", "\x7f", "\u0085"):
            with self.subTest(character=character):
                host = FakeHost()
                code, out, err = record_window(host, actor=f"example-unsafe{character}account")
                self.assertEqual((code, out), (1, ""))
                self.assertIn("control character", err)
                self.assertIn("Fix:", err)
                self.assertNotIn("example-unsafe", err)
                self.assertEqual((host.events, host.operations, host.locks), ([], [], {}))


class WithdrawAndShow(unittest.TestCase):
    def test_withdraw_appends_and_read_back_confirms_its_own_event(self) -> None:
        host = FakeHost()
        self.assertEqual(record_window(host)[0], 0)
        code, out, err = invoke(host, "withdraw")
        self.assertEqual((code, err), (0, ""))
        previous, event = host.events
        self.assertNotEqual(event["key"], previous["key"])
        self.assertEqual((event["act"], event["actor"], event["starts_at"], event["ends_at"]), ("withdraw", ACTOR, None, None))
        first, second = out.splitlines()
        self.assertTrue(first.startswith("window: ok"))
        self.assertIn(f"record: ok — event {event['key']} withdrew {previous['key']}", second)
        self.assertEqual(host.operations[-3:], [("newest", True), ("append", True), ("landed", True)])
        self.assertEqual(host.locks, {})
        self.assertEqual(invoke(host, "show"), (0, "No maintenance window is recorded.\n", ""))

    def test_withdraw_without_a_standing_window_refuses_and_releases(self) -> None:
        for installed, command in ((True, "gideon window set"), (False, "sudo python3 -m gideon window set")):
            with self.subTest(installed=installed), patch.object(report, "_installed_form", installed):
                host = FakeHost()
                code, out, err = invoke(host, "withdraw")
                self.assertEqual((code, err), (1, ""))
                self.assertIn("window: refuse — no maintenance window stands.", out)
                self.assertIn(f"Fix: Run {command}", out)
                self.assertEqual(host.operations, [("newest", True)])
                self.assertEqual(host.events, [])
                self.assertEqual(host.locks, {})
                self.assertEqual(host.release_count, 1)

    def test_show_and_note_cover_ahead_under_way_and_no_window(self) -> None:
        host = FakeHost()
        self.assertEqual(invoke(host, "show"), (0, "No maintenance window is recorded.\n", ""))
        self.assertEqual(record_window(host)[0], 0)
        event = host.events[0]
        start = datetime.fromisoformat(str(event["starts_at"]))
        end = datetime.fromisoformat(str(event["ends_at"]))
        span = "from Monday, January 13 at 12:00 PM until Monday, January 13 at 1:00 PM"
        host.operations.clear()
        for now, phase in ((NOW, "ahead"), (start, "under way")):
            with self.subTest(now=now):
                self.assertEqual(invoke(host, "show", now=now), (
                    0, f"Maintenance window {event['key']} is {phase} {span}, "
                    f"recorded by {ACTOR} on Friday, January 10 at 12:00 PM.\n", "",
                ))
                self.assertEqual(invoke(host, "show", "--note", now=now), (
                    0, f"The next maintenance window is {span}.\n", "",
                ))
        self.assertEqual(invoke(host, "show", now=end), (0, "No maintenance window is recorded.\n", ""))
        self.assertEqual(invoke(host, "show", "--note", now=end), (0, "No maintenance window is announced.\n", ""))
        self.assertEqual(host.operations, [("newest", False)] * 6)


class RefusalsAndLock(unittest.TestCase):
    def test_root_refusal_precedes_lock_and_read(self) -> None:
        for subcommand, flags in (("set", ("--start", START, "--end", END)), ("withdraw", ()), ("show", ("--note",))):
            with self.subTest(subcommand=subcommand):
                host = FakeHost()
                host.euid = 1000
                code, out, err = invoke(host, subcommand, *flags)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("root is required", err)
                self.assertIn(f"Fix: Run {report.command(f'window {subcommand}', sudo=False)}", err)
                self.assertEqual((host.mkdir_calls, host.operations), ([], []))

    def test_a_held_lock_names_its_holder_and_prevents_both_writes(self) -> None:
        holder = backuplock.Record("gideon window set", os.getpid() + 1, NOW - timedelta(minutes=1))
        host = FakeHost()
        host.locks[LOCK] = holder.to_json()
        for code, out, err in (record_window(host), invoke(host, "withdraw")):
            self.assertEqual((code, out), (1, ""))
            self.assertIn(f"held by {holder.command} since {holder.started.isoformat()} (pid {holder.pid})", err)
            self.assertIn("Fix: Wait", err)
        self.assertEqual((host.events, host.operations, host.release_count), ([], [], 0))
        self.assertEqual(host.locks, {LOCK: holder.to_json()})

    def test_overlapping_withdrawals_append_exactly_one_event(self) -> None:
        host = FakeHost()
        self.assertEqual(record_window(host)[0], 0)
        first_pid = os.getpid()
        other: list[tuple[int, str, str]] = []

        def overlap() -> None:
            host.before_newest = None
            with patch("gideon.host.backuplock.os.getpid", return_value=first_pid + 1):
                other.append(invoke(host, "withdraw"))

        host.before_newest = overlap
        self.assertEqual(invoke(host, "withdraw")[0], 0)
        [(code, out, err)] = other
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"held by gideon window withdraw since {NOW.isoformat()} (pid {first_pid})", err)
        self.assertIn("Fix:", err)
        self.assertEqual([event["act"] for event in host.events], ["set", "withdraw"])
        self.assertEqual(host.locks, {})
        self.assertEqual(host.release_count, 2)

    def test_missing_read_failed_append_and_missing_read_back_release_the_lock(self) -> None:
        for failure, detail, event_count, command in (
            ("newest_error", "window: refuse — the maintenance window event table is missing", 0, "apply"),
            ("append_error", "record: refuse — maintenance window writer failed: exit 3", 0, None),
            ("hide_read_back", "could not be confirmed", 1, "window show"),
            ("read_back_error", "was written but could not be read back", 1, "window show"),
        ):
            with self.subTest(failure=failure), patch.object(report, "_installed_form", True):
                host = FakeHost()
                setattr(host, failure, True)
                code, out, err = record_window(host)
                self.assertEqual((code, err), (1, ""))
                self.assertIn(detail, out)
                self.assertIn("Fix:", out)
                if command is not None:
                    self.assertIn(report.command(command), out)
                self.assertNotIn("fictitious diagnostic", out)
                self.assertNotIn("record: ok", out)
                self.assertEqual(len(host.events), event_count)
                self.assertEqual(host.locks, {})
                self.assertEqual(host.release_count, 1)

    def test_show_note_read_failure_produces_no_sentence_or_lock(self) -> None:
        host = FakeHost()
        host.newest_error = True
        code, out, err = invoke(host, "show", "--note")
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"Fix: Run {report.command('apply')}", err)
        self.assertEqual(host.operations, [("newest", False)])
