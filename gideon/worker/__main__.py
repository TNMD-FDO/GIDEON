"""Wait for the queue schema, serve metrics, and run the worker process."""

import asyncio
import logging
import signal
import sys
import threading
from collections.abc import Callable, Mapping
from http.server import HTTPServer
from typing import Any, Final

import psycopg

from .app import build_app
from .logs import install_handler
from .metrics import start_metrics_server
from .settings import Settings, WorkerSettingsError, connection_kwargs, load_settings

# exempt: two seconds bounds schema discovery without a busy connection loop.
SCHEMA_WAIT_SECONDS: Final = 2
# exempt: the connection probe is shorter than the next schema poll.
SCHEMA_CONNECT_TIMEOUT: Final = 1
SCHEMA_PROBE: Final = "SELECT 1 FROM procrastinate_jobs LIMIT 0"


def install_stop_handlers(stop: threading.Event) -> None:
    """Make the container's first process leave its schema wait on either signal."""

    def request_stop(signum: int, frame: object) -> None:
        del signum, frame
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)


def wait_for_schema(
    settings: Settings, stop: threading.Event, connect: Callable[..., Any]
) -> bool:
    """Poll the database until its job table is available or a stop arrives."""

    logger = logging.getLogger(__name__)
    logger.info("Waiting for queue schema")
    while not stop.is_set():
        kwargs = connection_kwargs(settings)
        try:
            with (
                connect(**kwargs, connect_timeout=SCHEMA_CONNECT_TIMEOUT) as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute(SCHEMA_PROBE)
        except Exception:  # noqa: BLE001 - the schema may be absent during apply.
            stop.wait(SCHEMA_WAIT_SECONDS)
            continue
        logger.info("Queue schema ready")
        return True
    logger.info("Queue schema wait stopped")
    return False


def main(
    environ: Mapping[str, str] | None = None,
    *,
    server_factory: Callable[[Settings], HTTPServer] = start_metrics_server,
) -> int:
    """Start the worker after its role can query the queue schema."""

    install_handler()
    try:
        settings = load_settings(environ)
        stop = threading.Event()
        install_stop_handlers(stop)
        if not wait_for_schema(settings, stop, psycopg.connect):
            return 0
        server = server_factory(settings)
        try:
            app = build_app(settings)

            async def run() -> None:
                async with app.open_async():
                    await app.run_worker_async()

            asyncio.run(run())
        finally:
            server.shutdown()
            server.server_close()
    except WorkerSettingsError as exc:
        print(f"worker failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - keep other failures content-free.
        print(
            f"worker failed: {type(exc).__name__}. "
            "Fix: check the worker's mounted secret and database logs.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
