"""Content-free event rows and their silent writer: the connection facts,
source vocabulary, ``TripRow``, and ``record_trip``. The Postgres driver is
imported inside ``write_event_row`` alone, which no host path calls.

Built over ``families``; ``window`` calls ``record_trip`` on a stream's trip.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from threading import Thread
from typing import Final

from gideon.guardrail.families import Trip

# The table and the source vocabulary, the user and eval words its members, have
# this one home: the improvement report's guardrail section imports both to read
# the rows this module writes.
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
TRIP_COLUMNS: Final[tuple[str, ...]] = (
    "branch",
    "family",
    "pattern_id",
    "source",
    "chat_id",
)
USER_SOURCE: Final[str] = "user"
EVAL_SOURCE: Final[str] = "eval"
SOURCE_VOCABULARY = (USER_SOURCE, EVAL_SOURCE)
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
    """Start the trip writer without making the service's response wait for the database."""

    Thread(target=write_trip_row, args=(row,), daemon=True).start()


def dispatch_event_row(
    table: str,
    partition_function: str,
    columns: tuple[str, ...],
    values: tuple[object, ...],
) -> None:
    """Start an event writer without making the response wait for the database."""

    Thread(
        target=write_event_row,
        args=(table, partition_function, columns, values),
        daemon=True,
    ).start()


def write_event_row(
    table: str,
    partition_function: str,
    columns: tuple[str, ...],
    values: tuple[object, ...],
    connect: Callable[..., object] | None = None,
) -> None:
    """Write one event row, dropping every local or database failure."""

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
            connection.execute(f"SELECT {partition_function}(now())")
            connection.execute(
                f"INSERT INTO {table} "
                f"({', '.join(columns)}) "
                f"VALUES ({', '.join(('%s',) * len(columns))})",
                values,
            )
    except Exception:  # noqa: BLE001 - the writer is intentionally silent and never raises.
        return


def write_trip_row(row: TripRow, connect: Callable[..., object] | None = None) -> None:
    """Write one trip row through the shared silent event writer."""

    write_event_row(
        GUARDRAIL_TRIPS_TABLE,
        GUARDRAIL_TRIPS_PARTITION_FUNCTION,
        TRIP_COLUMNS,
        (row.branch, row.family, row.pattern_id, row.source, row.chat_id),
        connect,
    )


def bounded_id(value: object) -> str | None:
    """Keep an id of the one bounded shape, so a header's free text never reaches a row."""

    return value if isinstance(value, str) and TRIP_CHAT_ID_PATTERN.fullmatch(value) else None


def record_trip(
    trip: Trip, branch: str | None, source: str, chat_id: str | None = None
) -> None:
    """Dispatch one content-free trip row without affecting the refusal."""

    try:
        row = TripRow(
            branch if branch is not None else UNKNOWN_BRANCH,
            trip.family,
            trip.pattern_id,
            source if source in SOURCE_VOCABULARY else USER_SOURCE,
            bounded_id(chat_id),
        )
        dispatch_trip_row(row)
    except Exception:  # noqa: BLE001 - trip recording cannot affect the refusal.
        return
