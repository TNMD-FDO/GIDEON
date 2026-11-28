"""Write and confirm one content-free audit_log row as the audit role."""

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal
from uuid import UUID

import anyio
from psycopg import AsyncConnection, DatabaseError
from psycopg.types.json import Jsonb

from gideon.guardrail import writer
from gideon.turn.generation import Connect

AUDIT_LOG_TABLE: Final = "audit_log"
AUDIT_LOG_PARTITION_FUNCTION: Final = "audit_log_ensure_partition"
AUDIT_LOG_COLUMNS: Final = ("at", "run_id", "kind", "user_id", "chat_id", "kb_ids", "release", "detail")
# exempt: a starting bound, corrected by the M1 reading.
WRITE_TIMEOUT_SECONDS: Final = 3.0

type Outcome = Literal["kept", "not_kept", "unknown"]

_WRITES: Final[set[asyncio.Task[Outcome]]] = set()
_LOG: Final = logging.getLogger("gideon.turn")
# SQLSTATE classes 08 (connection exception) and 57 (operator intervention) end the
# session with the commit unconfirmed; within them, 57014 (query canceled) is the server
# cancelling the statement, so it refused. 40003 (statement completion unknown) says so.
_UNCONFIRMED_CLASSES: Final = frozenset(("08", "57"))
_REFUSED_STATES: Final = frozenset(("57014",))
_UNCONFIRMED_STATES: Final = frozenset(("40003",))

_PARTITION_SQL: Final = f"SELECT {AUDIT_LOG_PARTITION_FUNCTION}(%s)"
_INSERT_SQL: Final = (
    f"INSERT INTO {AUDIT_LOG_TABLE} ({', '.join(AUDIT_LOG_COLUMNS)}) "
    f"VALUES ({', '.join(('%s',) * len(AUDIT_LOG_COLUMNS))})"
)


@dataclass(frozen=True, slots=True)
class AuditRow:
    """The row facts to bind to the audit write's moment."""

    run_id: UUID
    kind: str
    user_id: str | None
    chat_id: str | None
    kb_ids: tuple[str, ...]
    release: str
    detail: dict[str, object]
    log_reason: str

    def values(self, at: datetime) -> tuple[object, ...]:
        """Bind the row in audit column order."""
        return (at, self.run_id, self.kind, self.user_id, self.chat_id, list(self.kb_ids), self.release, Jsonb(self.detail))


def _server_refused(error: Exception) -> bool:
    """A statement error the server answered with its SQLSTATE, outside the connection classes.

    The connection-exception class and the operator-intervention class end the session,
    so the insert's commit is unconfirmed; any other answered state rejected the statement.
    A client-side error carries no SQLSTATE.
    """
    state = error.sqlstate if isinstance(error, DatabaseError) else None
    if state is None or state in _UNCONFIRMED_STATES:
        return False
    return state in _REFUSED_STATES or state[:2] not in _UNCONFIRMED_CLASSES


def _clock() -> datetime:
    return datetime.now(UTC)


async def write_row(
    row: AuditRow, *, connect: Connect = AsyncConnection.connect, timeout: float = WRITE_TIMEOUT_SECONDS,
    clock: Callable[[], datetime] = _clock,
) -> Outcome:
    """Write and confirm one audit row under a deadline, without exposing diagnostics."""
    connection: AsyncConnection[tuple[object, ...]] | None = None
    sent = False

    async def insert() -> Outcome:
        nonlocal connection, sent
        try:
            password = Path(writer.TRIP_PASSWORD_PATH).read_text(encoding="utf-8").rstrip("\r\n")
            if not password:
                return "not_kept"
            connection = await connect(
                host=writer.TRIP_DATABASE_HOST,
                port=writer.TRIP_DATABASE_PORT,
                dbname=writer.TRIP_DATABASE_NAME,
                user=writer.TRIP_DATABASE_USER,
                password=password,
                autocommit=True,
            )
            at = clock()
            await connection.execute(_PARTITION_SQL, (at,))
            values = row.values(at)
            sent = True
            await connection.execute(_INSERT_SQL, values)
        except Exception as error:  # noqa: BLE001 - the writer reports outcomes alone.
            return "not_kept" if not sent or _server_refused(error) else "unknown"
        return "kept"

    async def write_with_deadline() -> Outcome:
        task = asyncio.create_task(insert())
        try:
            done, _ = await asyncio.wait((task,), timeout=timeout)
            if not done or task.cancelled():
                return "unknown" if sent else "not_kept"
            return task.result()
        finally:
            if not task.done():
                task.cancel()
            # Close before awaiting cancellation of a stalled connect or statement.
            if connection is not None:
                with suppress(Exception):
                    await connection.close()
            await asyncio.gather(task, return_exceptions=True)

    async def write_and_report() -> Outcome:
        outcome = await write_with_deadline()
        if outcome != "kept":
            # Logged here and not by the caller, so a request cancelled mid-write still leaves its line.
            _LOG.error(error_line(row.kind, row.log_reason, outcome, row.run_id))
        return outcome

    # The write is its own task, held until it ends, so a caller cancelled natively
    # still re-raises at once while its row is written under the deadline; the scope
    # keeps an AnyIO cancellation from ending the wait for the outcome.
    writing = asyncio.create_task(write_and_report())
    _WRITES.add(writing)
    writing.add_done_callback(_WRITES.discard)
    outcome: Outcome = "not_kept"
    with anyio.CancelScope(shield=True):
        outcome = await asyncio.shield(writing)
    return outcome


def error_line(kind: str, log_reason: str, outcome: Outcome | Literal["not_built"], run_id: UUID) -> str:
    """Format the error line when an audit row could not be built or confirmed."""
    return f"state={kind} reason={log_reason} outcome={outcome} run_id={run_id}"
