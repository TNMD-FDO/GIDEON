"""Check the worker's database role and latest heartbeat."""

import sys
from collections.abc import Callable, Mapping
from typing import Any, Final

import psycopg

from .settings import Settings, connection_kwargs, load_settings
from .tasks import STALLED_SECONDS

# exempt: the health probe has three seconds to connect before Compose retries it.
CONNECT_TIMEOUT: Final = 3
HEARTBEAT_QUERY: Final = (
    f"SELECT last_heartbeat >= now() - INTERVAL '{STALLED_SECONDS} seconds' "
    "FROM procrastinate_workers ORDER BY last_heartbeat DESC LIMIT 1"
)


class WorkerNotRegistered(Exception):
    """No queue worker has registered in the database."""


class WorkerHeartbeatStale(Exception):
    """The newest queue worker heartbeat is too old."""


def check_health(settings: Settings, connect: Callable[..., Any]) -> None:
    """Raise when the role, schema, or fresh worker row is unavailable."""

    with (
        connect(**connection_kwargs(settings), connect_timeout=CONNECT_TIMEOUT) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(HEARTBEAT_QUERY)
        row = cursor.fetchone()
    if row is None:
        raise WorkerNotRegistered
    if row[0] is not True:
        raise WorkerHeartbeatStale


def main(
    connect: Callable[..., Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Print only a failure's class and return the healthcheck exit code."""

    try:
        if connect is None:
            connect = psycopg.connect
        check_health(load_settings(environ), connect)
    except Exception as exc:  # noqa: BLE001 - the health probe prints a class only.
        print(type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
