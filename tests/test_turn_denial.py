"""Audit rows of ids alone and their write outcomes for conflicts-wall denials."""

import asyncio
import logging
import re
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import anyio
from psycopg import OperationalError
from psycopg.errors import (
    AdminShutdown,
    DeadlockDetected,
    DiskFull,
    InsufficientPrivilege,
    QueryCanceled,
    StatementCompletionUnknown,
)
from psycopg.types.json import Jsonb

from gideon.guardrail import writer
from gideon.turn import turnlog
from gideon.turn.caller import Denial
from gideon.turn.denial import (
    AUDIT_LOG_COLUMNS,
    AUDIT_LOG_PARTITION_FUNCTION,
    AUDIT_LOG_TABLE,
    DENIAL_KIND,
    Outcome,
    build_row,
    error_line,
    write_denial,
)

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "0001_audit_log.sql"
USER_ID = "fixture-user"
CHAT_ID = "fixture-chat"
KB_IDS = ("fixture-first", "fixture-second")
MOMENT = datetime(2032, 2, 29, 23, 59, 59, tzinfo=UTC)
NEXT_MONTH = datetime(2032, 3, 1, tzinfo=UTC)
TOKEN = "fictitious.denial.token"
KB_NAME = "Fictitious knowledge base name"


class DenialRows(unittest.TestCase):
    def test_columns_and_partition_function_match_the_migration(self) -> None:
        migration = MIGRATION.read_text(encoding="utf-8")
        table = re.search(rf"^CREATE TABLE {re.escape(AUDIT_LOG_TABLE)} \(\n(.*?)^\)", migration, re.M | re.S)
        assert table is not None
        columns = re.findall(r"^    ([a-z_]+) ", table[1], re.M)
        self.assertIn("id", columns)
        self.assertIn("actor_user_id", columns)
        self.assertEqual(AUDIT_LOG_COLUMNS, tuple(name for name in columns if name not in {"id", "actor_user_id"}))
        self.assertRegex(migration, rf"(?m)^CREATE OR REPLACE FUNCTION {re.escape(AUDIT_LOG_PARTITION_FUNCTION)}\(")

    def test_each_reason_builds_an_incident_with_a_fresh_run_id(self) -> None:
        cases: tuple[tuple[Denial, str, str | None, dict[str, str]], ...] = (
            (Denial("no_session", None, None, KB_IDS, ()), "eval", None, {}),
            (Denial("session_not_verified", None, "invalid", KB_IDS, ()), "user", CHAT_ID, {"session_reply": "invalid"}),
            (Denial("frontend_unavailable", None, "none", KB_IDS, ()), "user", CHAT_ID, {"session_reply": "none"}),
            (
                Denial("frontend_unavailable", USER_ID, None, KB_IDS, ((KB_IDS[1], 503),)),
                "user", CHAT_ID, {},
            ),
            (
                Denial("kb_not_readable", USER_ID, None, KB_IDS, ((KB_IDS[0], 401), (KB_IDS[1], 404))),
                "eval", CHAT_ID, {},
            ),
        )
        run_ids: set[UUID] = set()
        for denial, claim, chat_id, session_detail in cases:
            with self.subTest(reason=denial.reason, has_user=denial.user_id is not None):
                row = build_row(denial, claim, chat_id)
                self.assertEqual(row.run_id.version, 4)
                self.assertNotIn(row.run_id, run_ids)
                run_ids.add(row.run_id)
                expected_detail = {
                    "reason": denial.reason,
                    "result": "denied",
                    "claim": claim,
                    "failures": [{"kb_id": kb_id, "reply": reply} for kb_id, reply in denial.failures],
                    **session_detail,
                }
                self.assertEqual(row.detail, expected_detail)
                self.assertEqual(row.release, turnlog.product_version())
                self.assertEqual((row.user_id, row.chat_id, row.kb_ids), (denial.user_id, chat_id, KB_IDS))


class DenialWrites(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        password_file = Path(self.enterContext(tempfile.TemporaryDirectory())) / "password"
        self.password = "fixture-audit-password"
        password_file.write_text(self.password + "\n", encoding="utf-8")
        self.enterContext(patch.object(writer, "TRIP_PASSWORD_PATH", str(password_file)))
        self.connection = Mock(execute=AsyncMock(), close=AsyncMock())
        self.connect = AsyncMock(return_value=self.connection)
        self.denial = Denial("kb_not_readable", USER_ID, None, KB_IDS, ((KB_IDS[0], 401),))
        self.row = build_row(self.denial, "user", CHAT_ID)
        self.clock = Mock(return_value=MOMENT)

    async def test_kept_row_uses_one_moment_across_the_month_end(self) -> None:
        async def execute(sql: str, params: tuple[object, ...]) -> None:
            if sql.startswith("SELECT "):
                self.clock.return_value = NEXT_MONTH

        self.connection.execute.side_effect = execute
        self.assertEqual(await write_denial(self.row, connect=self.connect, clock=self.clock), "kept")
        self.clock.assert_called_once_with()
        self.connect.assert_awaited_once_with(
            host=writer.TRIP_DATABASE_HOST,
            port=writer.TRIP_DATABASE_PORT,
            dbname=writer.TRIP_DATABASE_NAME,
            user=writer.TRIP_DATABASE_USER,
            password=self.password,
            autocommit=True,
        )
        partition, insert = self.connection.execute.await_args_list
        self.assertEqual(partition.args, (f"SELECT {AUDIT_LOG_PARTITION_FUNCTION}(%s)", (MOMENT,)))
        sql, values = insert.args
        self.assertEqual(
            sql, f"INSERT INTO {AUDIT_LOG_TABLE} ({', '.join(AUDIT_LOG_COLUMNS)}) "
            f"VALUES ({', '.join('%s' for _ in AUDIT_LOG_COLUMNS)})",
        )
        self.assertIs(partition.args[1][0], values[0])
        self.assertEqual(values[:-1], (MOMENT, self.row.run_id, DENIAL_KIND, USER_ID, CHAT_ID, list(KB_IDS), self.row.release))
        self.assertIsInstance(values[-1], Jsonb)
        self.assertEqual(values[-1].obj, self.row.detail)
        self.connection.close.assert_awaited_once()

    async def test_failed_connect_is_not_kept(self) -> None:
        self.connect.side_effect = OperationalError("Fictitious refusal from the record")
        self.assertEqual(await write_denial(self.row, connect=self.connect, clock=self.clock), "not_kept")
        self.connection.execute.assert_not_called()
        self.connection.close.assert_not_called()

    async def test_server_refusing_the_insert_is_not_kept(self) -> None:
        # Three are operational errors the server answered, so a refusal is read by its SQLSTATE;
        # a cancelled statement refused although its class is the operator's.
        for error in (
            InsufficientPrivilege("Fictitious server restriction"), DiskFull("Fictitious full disk"),
            DeadlockDetected("Fictitious deadlock"), QueryCanceled("Fictitious statement timeout"),
        ):
            with self.subTest(error=type(error).__name__):
                self.connection.execute.reset_mock()
                self.connection.close.reset_mock()
                self.connection.execute.side_effect = [None, error]
                self.assertEqual(await write_denial(self.row, connect=self.connect, clock=self.clock), "not_kept")
                self.assertEqual(self.connection.execute.await_count, 2)
                self.connection.close.assert_awaited_once()

    async def test_deadline_closes_connection_before_joining_the_task(self) -> None:
        cases: tuple[tuple[str, Outcome, int], ...] = (("SELECT ", "not_kept", 1), ("INSERT ", "unknown", 2))
        for stalled, expected, statements in cases:
            with self.subTest(stalled=stalled):
                order: list[str] = []
                self.connection.execute.reset_mock()
                self.connection.close.reset_mock()

                async def execute(
                    sql: str, params: tuple[object, ...], prefix: str = stalled, events: list[str] = order,
                ) -> None:
                    if sql.startswith(prefix):
                        try:
                            await asyncio.Future()
                        finally:
                            events.append("task finished")

                async def close(events: list[str] = order) -> None:
                    events.append("connection closed")

                self.connection.execute.side_effect = execute
                self.connection.close.side_effect = close
                outcome = await write_denial(self.row, connect=self.connect, timeout=0, clock=self.clock)
                self.assertEqual(outcome, expected)
                self.assertEqual(self.connection.execute.await_count, statements)
                self.connection.close.assert_awaited_once()
                self.assertEqual(order, ["connection closed", "task finished"])

    async def test_lost_connection_after_send_is_unknown(self) -> None:
        for error in (
            OperationalError("Fictitious lost connection"), AdminShutdown("Fictitious shutdown"),
            StatementCompletionUnknown("Fictitious unknown completion"), OSError("Fictitious lost socket"),
        ):
            with self.subTest(error=type(error).__name__):
                self.connection.execute.reset_mock(side_effect=True)
                self.connection.close.reset_mock()
                self.connection.execute.side_effect = [None, error]
                self.assertEqual(await write_denial(self.row, connect=self.connect, clock=self.clock), "unknown")
                self.assertEqual(self.connection.execute.await_count, 2)
                self.connection.close.assert_awaited_once()

    async def test_cancelling_the_caller_still_keeps_the_row(self) -> None:
        partition_started = anyio.Event()
        release_partition = anyio.Event()
        outcomes: list[Outcome] = []

        async def execute(sql: str, params: tuple[object, ...]) -> None:
            if sql.startswith("SELECT "):
                partition_started.set()
                await release_partition.wait()

        async def caller() -> None:
            outcomes.append(await write_denial(self.row, connect=self.connect, clock=self.clock))

        self.connection.execute.side_effect = execute
        async with anyio.create_task_group() as group:
            group.start_soon(caller)
            await partition_started.wait()
            group.cancel_scope.cancel()
            release_partition.set()
        self.assertEqual(outcomes, ["kept"])
        self.assertEqual(self.connection.execute.await_count, 2)
        self.connection.close.assert_awaited_once()

    async def test_cancelling_the_asyncio_task_still_keeps_the_row(self) -> None:
        partition_started = asyncio.Event()
        release_partition = asyncio.Event()
        inserted = asyncio.Event()

        async def execute(sql: str, params: tuple[object, ...]) -> None:
            if sql.startswith("SELECT "):
                partition_started.set()
                await release_partition.wait()
            else:
                inserted.set()

        self.connection.execute.side_effect = execute
        caller = asyncio.create_task(write_denial(self.row, connect=self.connect, clock=self.clock))
        await partition_started.wait()
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        release_partition.set()
        async with asyncio.timeout(1):
            await inserted.wait()
        self.assertEqual(self.connection.execute.await_count, 2)

    async def test_a_cancelled_caller_still_logs_its_unconfirmed_write(self) -> None:
        partition_started = asyncio.Event()
        release_partition = asyncio.Event()

        async def execute(sql: str, params: tuple[object, ...]) -> None:
            if sql.startswith("SELECT "):
                partition_started.set()
                await release_partition.wait()
            else:
                raise OperationalError("Fictitious lost connection")

        self.connection.execute.side_effect = execute
        with self.assertLogs("gideon.turn", level=logging.ERROR) as captured:
            caller = asyncio.create_task(write_denial(self.row, connect=self.connect, clock=self.clock))
            await partition_started.wait()
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            release_partition.set()
            async with asyncio.timeout(1):
                while not captured.records:
                    await asyncio.sleep(0)
        self.assertEqual(
            [record.getMessage() for record in captured.records],
            [error_line(self.row.reason, "unknown", self.row.run_id)],
        )

    async def test_private_text_is_absent_from_the_row_and_log(self) -> None:
        row = build_row(self.denial, "user", KB_NAME)
        self.assertIsNone(row.chat_id)
        self.connection.execute.side_effect = [None, InsufficientPrivilege(f"{TOKEN} {KB_NAME}")]
        with self.assertLogs(level=logging.DEBUG) as captured:
            outcome = await write_denial(row, connect=self.connect, clock=self.clock)
        line = error_line(self.denial.reason, outcome, row.run_id)
        self.assertEqual(outcome, "not_kept")
        self.assertEqual(captured.records[0].name, "gideon.turn")
        self.assertEqual(captured.records[0].levelno, logging.ERROR)
        self.assertEqual(line, f"state=conflicts_wall_denial reason=kb_not_readable outcome=not_kept run_id={row.run_id}")
        self.assertEqual([record.getMessage() for record in captured.records], [line])
        values = self.connection.execute.await_args_list[-1].args[1]
        for private in (TOKEN, KB_NAME):
            self.assertNotIn(private, repr(row))
            self.assertNotIn(private, repr((*values[:-1], values[-1].obj)))
            for record in captured.records:
                self.assertNotIn(private, repr(vars(record)))
