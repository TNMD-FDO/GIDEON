"""Maintenance window events use the append-only record and aware instants."""

import json
import os
import subprocess
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_index_record import FakeHost as RecordHost

import gideon
from gideon.host import report, stack, windowrecord
from gideon.host.render.worker import (
    METRICS_ROLE,
    WORKER_DATABASE_HOST,
    WORKER_DATABASE_NAME,
)
from gideon.host.sysio import PathLike

RENDERED = "/fictitious-rendered"
KEY = "11111111-1111-4111-8111-111111111111"
ACTOR = "example-operator"
START = datetime(2099, 1, 3, 12, 0, tzinfo=UTC)
END = START + timedelta(hours=1)
RECORDED = START - timedelta(days=1)
EVENT = windowrecord.WindowEvent(17, KEY, RECORDED, "set", START, END, ACTOR)


class FakeHost(RecordHost):
    def stat(self, path: PathLike) -> os.stat_result:
        raise FileNotFoundError(os.fspath(path))


def reply(**changes: object) -> str:
    row = {
        "id": EVENT.id,
        "key": EVENT.key,
        "at": EVENT.at.isoformat(),
        "act": EVENT.act,
        "starts_at": START.isoformat(),
        "ends_at": END.isoformat(),
        "actor": EVENT.actor,
        **changes,
    }
    return json.dumps(row) + "\n"


class Append(unittest.TestCase):
    def test_set_ensures_partition_and_binds_values_in_one_audit_transaction(self) -> None:
        host = FakeHost(stdout="partition notice")
        actor = "example-o'brien"
        self.assertIsNone(windowrecord.append(
            host, RENDERED, key=KEY, act="set", actor=actor, starts_at=START, ends_at=END,
        ))
        [(argv, sql)] = host.calls
        self.assertEqual(argv, stack.exec_argv(
            RENDERED, WORKER_DATABASE_HOST, "psql", "-U", "gideon_audit", "-d", WORKER_DATABASE_NAME,
            "-v", "ON_ERROR_STOP=1", "--single-transaction", "-f", "-",
        ))
        assert sql is not None
        self.assertTrue(sql.startswith("SELECT public.maintenance_window_events_ensure_partition(now());\n"))
        self.assertEqual(sql.count("INSERT INTO public.maintenance_window_events"), 1)
        bindings = {
            "v_key": KEY,
            "v_act": "set",
            "v_starts_at": START.isoformat(),
            "v_ends_at": END.isoformat(),
            "v_actor": actor,
            "v_release": gideon.__version__,
        }
        for name, value in bindings.items():
            with self.subTest(name=name):
                escaped = value.replace("'", "''")
                self.assertIn(f"\\set {name} '{escaped}'\n", sql)
                self.assertIn(f":'{name}'", sql)
                self.assertNotIn(value, " ".join(argv))
        self.assertNotIn("RETURNING", sql)

    def test_withdraw_binds_absent_times(self) -> None:
        host = FakeHost()
        self.assertIsNone(windowrecord.append(host, RENDERED, key=KEY, act="withdraw", actor=ACTOR))
        [(_, sql)] = host.calls
        assert sql is not None
        self.assertIn("\\set v_act 'withdraw'\n", sql)
        for bound in ("v_starts_at", "v_ends_at"):
            self.assertIn(f"\\set {bound} ''\n", sql)
            self.assertIn(f"NULLIF(:'{bound}', '')::timestamptz", sql)

    def test_failure_reports_only_the_exit_status_and_fix(self) -> None:
        host = FakeHost(code=3, stdout="fictitious row output", stderr="fictitious row diagnostic")
        self.assertEqual(
            windowrecord.append(host, RENDERED, key=KEY, act="withdraw", actor=ACTOR),
            report.Problem(
                "maintenance window writer failed: exit 3",
                f"Run {stack.logs_fix(RENDERED, WORKER_DATABASE_HOST)}, then retry.",
            ),
        )

    def test_a_tool_that_cannot_run_is_a_refusal(self) -> None:
        host = FakeHost(error=FileNotFoundError("fictitious failure"))
        problem = windowrecord.append(host, RENDERED, key=KEY, act="withdraw", actor=ACTOR)
        self.assertIsInstance(problem, report.Problem)
        self.assertEqual(len(host.calls), 1)


class Read(unittest.TestCase):
    def test_newest_reads_by_id_as_metrics(self) -> None:
        host = FakeHost(stdout=reply())
        self.assertEqual(windowrecord.newest(host, RENDERED), EVENT)
        [(argv, sql)] = host.calls
        self.assertEqual(argv, stack.exec_argv(
            RENDERED, WORKER_DATABASE_HOST, "psql", "-U", METRICS_ROLE, "-d", WORKER_DATABASE_NAME,
            "-X", "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=sqlstate", "-tA", "-f", "-",
        ))
        assert sql is not None
        self.assertIn("FROM public.maintenance_window_events", sql)
        self.assertIn("ORDER BY id DESC LIMIT 1", sql)

    def test_newest_passes_its_bound_and_refuses_on_expiry(self) -> None:
        timeout = 2.5
        host = FakeHost(stdout=reply())
        with patch.object(host, "run", wraps=host.run) as run:
            self.assertEqual(windowrecord.newest(host, RENDERED, timeout=timeout), EVENT)
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs["timeout"], timeout)

        [(argv, _)] = host.calls
        stalled = FakeHost(error=subprocess.TimeoutExpired(argv, timeout))
        self.assertEqual(windowrecord.newest(stalled, RENDERED, timeout=timeout), report.Problem(
            "maintenance window reader failed: command could not run", f"Run {report.command('apply')}, then retry.",
        ))

    def test_landed_reads_by_bound_key_as_metrics(self) -> None:
        withdraw = replace(EVENT, act="withdraw", starts_at=None, ends_at=None)
        host = FakeHost(stdout=reply(act=withdraw.act, starts_at=None, ends_at=None))
        self.assertEqual(windowrecord.landed(host, RENDERED, KEY), withdraw)
        [(argv, sql)] = host.calls
        self.assertEqual(argv[argv.index("-U") + 1], METRICS_ROLE)
        self.assertNotIn(KEY, " ".join(argv))
        assert sql is not None
        self.assertIn(f"\\set v_key '{KEY}'\n", sql)
        self.assertIn("WHERE key = :'v_key'::uuid", sql)

    def test_a_successful_read_with_no_rows_returns_none(self) -> None:
        host = FakeHost()
        self.assertIsNone(windowrecord.newest(host, RENDERED))
        self.assertIsNone(windowrecord.landed(host, RENDERED, KEY))

    def test_missing_table_refuses_with_apply_as_the_fix(self) -> None:
        host = FakeHost(code=3, stderr="psql:<stdin>:6: ERROR:  42P01\n")
        self.assertEqual(windowrecord.newest(host, RENDERED), report.Problem(
            "the maintenance window event table is missing.", f"Run {report.command('apply')}, then retry.",
        ))

    def test_failed_read_refuses_even_if_stdout_contains_an_event(self) -> None:
        host = FakeHost(code=2, stdout=reply(), stderr="fictitious row diagnostic")
        self.assertEqual(windowrecord.landed(host, RENDERED, KEY), report.Problem(
            "maintenance window reader failed: exit 2", f"Run {report.command('apply')}, then retry.",
        ))

    def test_unreadable_reply_is_a_problem(self) -> None:
        for text in (
            "fictitious non-JSON reply", "null", "{}", reply(id=True),
            reply(at="2099-01-02T12:00:00"), reply(ends_at=None), reply(act="withdraw"),
        ):
            with self.subTest(text=text):
                self.assertIsInstance(windowrecord.newest(FakeHost(stdout=text), RENDERED), report.Problem)


class Standing(unittest.TestCase):
    def test_start_is_under_way_and_end_is_no_longer_standing(self) -> None:
        window = windowrecord.Window(KEY, START, END, RECORDED, ACTOR)
        tick = timedelta(microseconds=1)
        for now, expected_phase in ((START - tick, "ahead"), (START, "under-way"), (END - tick, "under-way")):
            with self.subTest(now=now):
                self.assertEqual(windowrecord.standing_window(EVENT, now), window)
                self.assertEqual(windowrecord.phase(window, now), expected_phase)
        self.assertIsNone(windowrecord.standing_window(EVENT, END))

    def test_a_withdrawal_or_empty_record_has_no_standing_window(self) -> None:
        withdraw = replace(EVENT, act="withdraw", starts_at=None, ends_at=None)
        self.assertIsNone(windowrecord.standing_window(withdraw, START))
        self.assertIsNone(windowrecord.standing_window(None, START))

    def test_repeated_wall_times_compare_as_instants(self) -> None:
        zone = ZoneInfo("America/Chicago")
        start = datetime(2020, 11, 1, 1, 30, tzinfo=zone, fold=0)
        end = start.replace(fold=1)
        event = replace(EVENT, starts_at=start, ends_at=end)
        window = windowrecord.standing_window(event, start)
        assert window is not None
        self.assertEqual(windowrecord.phase(window, end.replace(minute=0)), "under-way")
        self.assertIsNone(windowrecord.standing_window(event, end))
