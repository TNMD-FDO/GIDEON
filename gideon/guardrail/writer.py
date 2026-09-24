"""The guardrail trip's content-free row and its silent writer: the connection
facts and vocabulary, ``TripRow``, and ``record_trip``, whose Postgres driver is
imported inside ``write_trip_row`` alone.

Built over ``families``; ``window`` calls ``record_trip`` on a stream's trip.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from threading import Thread
from typing import Final

from gideon.guardrail.families import Trip

# The table and the source vocabulary have this one home: the improvement
# report's guardrail section imports both to read the rows this module writes.
GUARDRAIL_TRIPS_TABLE = "guardrail_trips"
GUARDRAIL_TRIPS_PARTITION_FUNCTION = "guardrail_trips_ensure_partition"
TRIP_DATABASE_HOST = "postgres"
TRIP_DATABASE_PORT = 5432
TRIP_DATABASE_NAME = "gideon"
TRIP_DATABASE_USER = "gideon_audit"
TRIP_PASSWORD_PATH = "/run/secrets/postgres_gideon_audit_password"
TRIP_CONNECT_TIMEOUT_SECONDS = 3
TRIP_STATEMENT_TIMEOUT_MILLISECONDS = 3000
TRIP_DRIVER_MODULE = "psycopg"
SOURCE_VOCABULARY = ("user", "eval")
UNKNOWN_BRANCH = "unknown"
# A chat id is kept only in an id's shape, so a header's free text never reaches
# the row or the reader that splits it on ``|``; the bound is a starting value.
TRIP_CHAT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{1,128}")


@dataclass(frozen=True, slots=True)
class TripRow:
    """The content-free row persisted for one guardrail trip."""

    branch: str
    family: str
    pattern_id: str
    source: str
    chat_id: str | None


def dispatch_trip_row(row: TripRow) -> None:
    """Start the trip writer without making the Filter wait for the database."""

    Thread(target=write_trip_row, args=(row,), daemon=True).start()


def write_trip_row(row: TripRow, connect: Callable[..., object] | None = None) -> None:
    """Write one trip row, dropping every local or database failure."""

    try:
        with open(TRIP_PASSWORD_PATH, encoding="utf-8") as password_file:
            password = password_file.read().rstrip("\r\n")
        if connect is None:
            import psycopg

            connect = psycopg.connect
        with connect(  # type: ignore[attr-defined]
            host=TRIP_DATABASE_HOST,
            port=TRIP_DATABASE_PORT,
            dbname=TRIP_DATABASE_NAME,
            user=TRIP_DATABASE_USER,
            password=password,
            connect_timeout=TRIP_CONNECT_TIMEOUT_SECONDS,
            options=f"-c statement_timeout={TRIP_STATEMENT_TIMEOUT_MILLISECONDS}",
        ) as connection:
            connection.execute(f"SELECT {GUARDRAIL_TRIPS_PARTITION_FUNCTION}(now())")
            connection.execute(
                f"INSERT INTO {GUARDRAIL_TRIPS_TABLE} "
                "(branch, family, pattern_id, source, chat_id) "
                "VALUES (%s, %s, %s, %s, %s)",
                (row.branch, row.family, row.pattern_id, row.source, row.chat_id),
            )
    except Exception:  # noqa: BLE001 - the writer is intentionally silent and never raises.
        return


def record_trip(
    trip: Trip, branch: str | None, source: str, chat_id: str | None = None
) -> None:
    """Dispatch one content-free trip row without affecting the refusal."""

    try:
        row = TripRow(
            branch if branch is not None else UNKNOWN_BRANCH,
            trip.family,
            trip.pattern_id,
            source if source in SOURCE_VOCABULARY else SOURCE_VOCABULARY[0],
            chat_id
            if isinstance(chat_id, str) and TRIP_CHAT_ID_PATTERN.fullmatch(chat_id)
            else None,
        )
        dispatch_trip_row(row)
    except Exception:  # noqa: BLE001 - trip recording cannot affect the refusal.
        return
