"""Expose queue depth and worker liveness as Prometheus text metrics."""

import logging
import threading
from collections.abc import Callable, Iterable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Final

import psycopg

from .fetch import FETCH_QUEUE
from .health import CONNECT_TIMEOUT
from .settings import Settings, connection_kwargs
from .staging import STAGE_QUEUE
from .tasks import RECOVERY_QUEUE, VERIFY_QUEUE

# exempt: an address; the unpublished listener shares the engine's port.
METRICS_PORT: Final = 8000
METRICS_PATH: Final = "/metrics"
CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"
KNOWN_QUEUES: Final = (VERIFY_QUEUE, RECOVERY_QUEUE, FETCH_QUEUE, STAGE_QUEUE)
LIVE_STATUSES: Final = ("todo", "doing")
QUEUE_JOBS_METRIC: Final = "gideon_worker_queue_jobs"
QUEUE_JOBS_HELP: Final = "M15 worker queue depth by queue and status."
HEARTBEAT_AGE_METRIC: Final = "gideon_worker_heartbeat_age_seconds"
HEARTBEAT_AGE_HELP: Final = "Age of the newest worker heartbeat in seconds."
CONCURRENCY_METRIC: Final = "gideon_worker_concurrency"
CONCURRENCY_HELP: Final = "Number of worker job slots."
# exempt: mechanics; three seconds to connect plus two statements at 2.5
# seconds each leaves two seconds under the ten-second scrape timeout.
STATEMENT_TIMEOUT_MS: Final = 2500
# exempt: mechanics; a client that sends nothing is dropped before the next scrape.
REQUEST_TIMEOUT_SECONDS: Final = 5
DEPTH_QUERY: Final = (
    "SELECT queue_name, status, count(*) FROM procrastinate_jobs "
    "WHERE status IN ('todo', 'doing') GROUP BY queue_name, status"
)
HEARTBEAT_AGE_QUERY: Final = (
    "SELECT EXTRACT(EPOCH FROM (now() - last_heartbeat)) "
    "FROM procrastinate_workers ORDER BY last_heartbeat DESC LIMIT 1"
)

MetricsRead = tuple[list[tuple[str, str, int]], float | None]

logger = logging.getLogger(__name__)


def read_metrics(
    settings: Settings, connect: Callable[..., Any] = psycopg.connect
) -> MetricsRead:
    """Read live job counts and the newest worker heartbeat on one connection."""

    with (
        connect(
            **connection_kwargs(settings),
            connect_timeout=CONNECT_TIMEOUT,
            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
        ) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(DEPTH_QUERY)
        rows = cursor.fetchall()
        cursor.execute(HEARTBEAT_AGE_QUERY)
        heartbeat = cursor.fetchone()
    age = None if heartbeat is None else float(heartbeat[0])
    return rows, age


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_metrics(
    rows: Iterable[tuple[str, str, int]],
    heartbeat_age: float | None,
    concurrency: int,
) -> str:
    """Format queue and worker gauges as stable Prometheus exposition text."""

    counts = {(queue, status): count for queue, status, count in rows}
    other_queues = sorted({queue for queue, _ in counts} - set(KNOWN_QUEUES))
    lines = [
        f"# HELP {QUEUE_JOBS_METRIC} {QUEUE_JOBS_HELP}",
        f"# TYPE {QUEUE_JOBS_METRIC} gauge",
    ]
    for queue in (*KNOWN_QUEUES, *other_queues):
        for status in LIVE_STATUSES:
            lines.append(
                f"{QUEUE_JOBS_METRIC}"
                f'{{queue="{_escape_label(queue)}",status="{status}"}} '
                f"{counts.get((queue, status), 0)}"
            )
    lines.extend((
        f"# HELP {HEARTBEAT_AGE_METRIC} {HEARTBEAT_AGE_HELP}",
        f"# TYPE {HEARTBEAT_AGE_METRIC} gauge",
    ))
    if heartbeat_age is not None:
        lines.append(f"{HEARTBEAT_AGE_METRIC} {heartbeat_age}")
    lines.extend((
        f"# HELP {CONCURRENCY_METRIC} {CONCURRENCY_HELP}",
        f"# TYPE {CONCURRENCY_METRIC} gauge",
        f"{CONCURRENCY_METRIC} {concurrency}",
    ))
    return "\n".join(lines) + "\n"


def start_metrics_server(
    settings: Settings,
    *,
    host: str = "0.0.0.0",
    port: int = METRICS_PORT,
    read: Callable[[Settings], MetricsRead] = read_metrics,
) -> HTTPServer:
    """Serve one scrape at a time on a daemon thread."""

    class MetricsHandler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT_SECONDS

        def do_GET(self) -> None:
            if self.path != METRICS_PATH:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                rows, age = read(settings)
                body = format_metrics(rows, age, settings.concurrency).encode("utf-8")
            except Exception as exc:  # noqa: BLE001 - a failed scrape is unavailable.
                logger.error("Metrics read failed: %s", type(exc).__name__)
                self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", CONTENT_TYPE)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer((host, port), MetricsHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
