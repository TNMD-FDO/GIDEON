"""Generation selection, refusals, and connection cleanup for research turns."""

import asyncio
import tempfile
import unittest
from collections.abc import Sequence
from functools import partial
from http import HTTPStatus
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
                connection, cursor = record_with_rows(live_rows)
                with self.assertRaises(TurnFailure) as raised:
                    asyncio.run(read_generation(self.record, connect=AsyncMock(return_value=connection)))
                self.assertEqual(raised.exception.state, state)
                cursor.execute.assert_awaited_once_with(
                    "SELECT gen_id, collection, embedding_space, lockfiles "
                    "FROM public.index_generations WHERE state = 'live'",
                )
                connection.close.assert_awaited_once()

    def test_named_built_and_live_rows_pin_their_own_store_names(self) -> None:
        for state in ("built", "live"):
            with self.subTest(state=state):
                name = f"fixture-{state}-generation"
                space, lockfile = "fixture-space", "fixture-lockfile"
                connection, cursor = record_with_rows([(name, TURN_COLLECTION, space, [lockfile])])
                pinned = asyncio.run(read_generation(self.record, name=name, connect=AsyncMock(return_value=connection)))
                self.assertEqual((pinned.gen_id, pinned.collection, pinned.embedding_space, pinned.lockfiles), (
                    name, TURN_COLLECTION, space, (lockfile,),
                ))
                self.assertEqual((pinned.stores.lexical_index, pinned.stores.dense_collection), (name, name))
                cursor.execute.assert_awaited_once_with(
                    "SELECT gen_id, collection, embedding_space, lockfiles "
                    "FROM public.index_generations WHERE gen_id = %s AND state IN ('built', 'live')",
                    (name,),
                )
                connection.close.assert_awaited_once()

    def test_an_absent_filtered_or_foreign_named_row_is_not_readable(self) -> None:
        for reason in ("unknown", "retired", "building", "failed", "other-collection"):
            with self.subTest(reason=reason):
                name = f"fixture-{reason}-generation"
                # The WHERE clause leaves every missing or non-readable state out of the result.
                rows = [(name, "fixture-other-collection", "fixture-space", ["fixture-lockfile"])] if reason == "other-collection" else []
                connection, _ = record_with_rows(rows)
                with self.assertRaises(TurnFailure) as raised:
                    asyncio.run(read_generation(self.record, name=name, connect=AsyncMock(return_value=connection)))
                self.assertEqual(raised.exception.state, "generation_not_readable")
                self.assertEqual(raised.exception.status, HTTPStatus.CONFLICT)
                self.assertNotIn(name, str(raised.exception.to_body()))
                connection.close.assert_awaited_once()

    def test_failed_connect_refuses_without_exposing_driver_detail(self) -> None:
        detail = "Fictitious driver diagnostics"
        connect = AsyncMock(side_effect=OperationalError(detail))
        with self.assertRaises(TurnFailure) as raised:
            asyncio.run(read_generation(self.record, connect=connect))
        self.assertEqual(raised.exception.state, "record_unavailable")
        self.assertNotIn(detail, str(raised.exception.to_body()))

    def test_expired_statement_closes_the_connected_record(self) -> None:
        async def stalled_statement(connection: MagicMock, cancelled: list[bool], *_: object) -> None:
            try:
                await asyncio.Future()
            finally:
                cancelled.append(connection.close.await_count == 1)

        for name in (None, "fixture-named-generation"):
            with self.subTest(name=name):
                connection, cursor = record_with_rows([])
                cancelled: list[bool] = []
                cursor.execute.side_effect = partial(stalled_statement, connection, cancelled)
                with self.assertRaises(TurnFailure) as raised:
                    asyncio.run(read_generation(self.record, name=name, connect=AsyncMock(return_value=connection), timeout=0))
                self.assertEqual((raised.exception.state, raised.exception.word), ("retrieval_timeout", "record"))
                connection.close.assert_awaited_once()
                self.assertEqual(cancelled, [True])
