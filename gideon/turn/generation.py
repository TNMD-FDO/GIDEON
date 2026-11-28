"""Read the turn's live or named generation and pin both store addresses."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from psycopg import AsyncConnection, Error

from gideon.api.settings import RecordCoordinates
from gideon.host.index.serving import (
    AmbiguousGeneration,
    StoreNames,
    select_serving,
    store_names,
)
from gideon.turn.states import State, TurnFailure

TURN_COLLECTION: Final = "caselaw"
# exempt: a starting bound, corrected by the M1 reading.
RECORD_TIMEOUT_SECONDS: Final = 2.0
_GENERATION_SQL: Final = (
    "SELECT gen_id, collection, embedding_space, lockfiles "
    "FROM public.index_generations"
)
_LIVE_SQL: Final = _GENERATION_SQL + " WHERE state = 'live'"
_NAMED_SQL: Final = _GENERATION_SQL + " WHERE gen_id = %s AND state IN ('built', 'live')"

type Connect = Callable[..., Awaitable[AsyncConnection[tuple[object, ...]]]]


@dataclass(frozen=True, slots=True)
class _GenerationRow:
    gen_id: str
    collection: str
    embedding_space: str
    lockfiles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PinnedGeneration(_GenerationRow):
    """The generation and its store addresses for this turn."""

    stores: StoreNames


def _generation_row(row: tuple[object, ...]) -> _GenerationRow:
    gen_id, collection, embedding_space, lockfiles = row
    if not (
        isinstance(gen_id, str) and gen_id
        and isinstance(collection, str) and collection
        and isinstance(embedding_space, str) and embedding_space
        and isinstance(lockfiles, (list, tuple)) and lockfiles
        and all(isinstance(label, str) and label for label in lockfiles)
    ):
        raise ValueError("Invalid generation row.")
    return _GenerationRow(gen_id, collection, embedding_space, tuple(lockfiles))


async def read_generation(
    record: RecordCoordinates, *, name: str | None = None, connect: Connect = AsyncConnection.connect,
    timeout: float = RECORD_TIMEOUT_SECONDS,
) -> PinnedGeneration:
    """Select the turn's collection from live rows or a readable named row under one deadline."""

    connection: AsyncConnection[tuple[object, ...]] | None = None

    async def read_rows() -> tuple[_GenerationRow, ...]:
        nonlocal connection
        password = record.password_file.read_text(encoding="utf-8").strip()
        if not password:
            raise ValueError("Empty record password.")
        connection = await connect(
            host=record.host, port=record.port, dbname=record.database_name,
            user=record.role, password=password, autocommit=True,
        )
        async with connection.cursor() as cursor:
            if name is None:
                await cursor.execute(_LIVE_SQL)
            else:
                await cursor.execute(_NAMED_SQL, (name,))
            return tuple(_generation_row(row) for row in await cursor.fetchall())

    task = asyncio.create_task(read_rows())
    try:
        done, _ = await asyncio.wait((task,), timeout=timeout)
        if not done:
            raise TurnFailure(State.RETRIEVAL_TIMEOUT, "record")
        rows = task.result()
    except (Error, OSError, ValueError):
        raise TurnFailure(State.RECORD_UNAVAILABLE) from None
    finally:
        if not task.done():
            task.cancel()
        # Close the socket before waiting for the driver's cancellation handling.
        try:
            if connection is not None:
                await connection.close()
        finally:
            await asyncio.gather(task, return_exceptions=True)

    selected = select_serving(rows, TURN_COLLECTION)
    if selected is None:
        raise TurnFailure(State.NO_LIVE_GENERATION if name is None else State.GENERATION_NOT_READABLE)
    if isinstance(selected, AmbiguousGeneration):
        raise TurnFailure(State.AMBIGUOUS_GENERATION)
    row = next(row for row in rows if (row.gen_id, row.collection) == (selected.gen_id, selected.collection))
    return PinnedGeneration(row.gen_id, row.collection, row.embedding_space, row.lockfiles, store_names(selected))
