"""Named refusals and connection cleanup for live generation reads."""

import asyncio
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from psycopg import OperationalError

from gideon.api.settings import RecordCoordinates
from gideon.turn.generation import TURN_COLLECTION, read_generation
from gideon.turn.states import TurnFailure

type LiveRow = tuple[str, str, str, list[str]]


def record_with_rows(rows: Sequence[LiveRow]) -> tuple[MagicMock, AsyncMock]:
    cursor = AsyncMock()
    cursor.__aenter__.return_value = cursor
    cursor.fetchall.return_value = rows
    connection = MagicMock()
    connection.cursor.return_value = cursor
    connection.close = AsyncMock()
    return connection, cursor


class Generation(unittest.TestCase):
    def setUp(self) -> None:
        password = Path(self.enterContext(tempfile.TemporaryDirectory())) / "password"
        password.write_text("fixture-record-password", encoding="utf-8")
        self.record = RecordCoordinates("record.example.invalid", 5432, "fixture-db", "fixture-reader", password)

    def test_absent_and_ambiguous_live_rows_are_distinct_refusals(self) -> None:
        rows = [
            (f"fixture-generation-{index}", TURN_COLLECTION, "fixture-space", ["fixture-lockfile"])
            for index in (1, 2)
        ]
        for live_rows, state in (([], "no_live_generation"), (rows, "ambiguous_generation")):
            with self.subTest(state=state):
                connection, _ = record_with_rows(live_rows)
                with self.assertRaises(TurnFailure) as raised:
                    asyncio.run(read_generation(self.record, connect=AsyncMock(return_value=connection)))
                self.assertEqual(raised.exception.state, state)
                connection.close.assert_awaited_once()

    def test_failed_connect_refuses_without_exposing_driver_detail(self) -> None:
        detail = "Fictitious driver diagnostics"
        connect = AsyncMock(side_effect=OperationalError(detail))
        with self.assertRaises(TurnFailure) as raised:
            asyncio.run(read_generation(self.record, connect=connect))
        self.assertEqual(raised.exception.state, "record_unavailable")
        self.assertNotIn(detail, str(raised.exception.to_body()))

    def test_expired_statement_closes_the_connected_record(self) -> None:
        connection, cursor = record_with_rows([])
        cancelled: list[bool] = []

        async def stalled_statement(_: str) -> None:
            try:
                await asyncio.Future()
            finally:
                cancelled.append(connection.close.await_count == 1)

        cursor.execute.side_effect = stalled_statement
        with self.assertRaises(TurnFailure) as raised:
            asyncio.run(read_generation(self.record, connect=AsyncMock(return_value=connection), timeout=0))
        self.assertEqual((raised.exception.state, raised.exception.word), ("retrieval_timeout", "record"))
        connection.close.assert_awaited_once()
        self.assertEqual(cancelled, [True])
