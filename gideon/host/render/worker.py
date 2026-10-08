"""Names shared by the queue worker's block, host command, and scrape job.

The Compose block, `worker verify`, and the Prometheus configuration read them
here; the worker package restates them, since it never imports the host.
"""

from pathlib import Path
from typing import Final

from gideon.host.render.engine import ENGINE_PORT

WORKER_SERVICE_NAME: Final[str] = "gideon-worker"
WORKER_JOB_NAME: Final[str] = "worker"
WORKER_METRICS_PORT: Final[int] = ENGINE_PORT
WORKER_METRICS_PATH: Final[str] = "/metrics"
WORKER_ROLE: Final[str] = "gideon_worker"
WORKER_SECRET_NAME: Final[str] = "postgres_gideon_worker_password"
WORKER_DATABASE_HOST: Final[str] = "postgres"
WORKER_DATABASE_PORT: Final[int] = 5432
WORKER_DATABASE_NAME: Final[str] = "gideon"
# exempt: four job slots are the starting value until a bulk build measures one.
WORKER_CONCURRENCY: Final[int] = 4
WORKER_VERIFY_TASK: Final[str] = "gideon.worker.tasks.verify"
WORKER_VERIFY_QUEUE: Final[str] = "verify"
WORKER_RECOVERY_TASK: Final[str] = "gideon.worker.tasks.retry_stalled_jobs"
WORKER_RECOVERY_QUEUE: Final[str] = "maintenance"
WORKER_RECOVERY_CRON: Final[str] = "* * * * *"
WORKER_MIGRATION_STEM: Final[str] = "0007_procrastinate_queue"

FETCH_TASK: Final[str] = "gideon.worker.tasks.fetch"
FETCH_QUEUE: Final[str] = "fetch"
STAGE_TASK: Final[str] = "gideon.worker.tasks.stage"
STAGE_QUEUE: Final[str] = "stage"
SNAPSHOTS_ROOT: Final = Path("/data/bulk/snapshots")
WORK_ROOT: Final = Path("/data/work")
DIR_MODE: Final[int] = 0o2770
RESOLVE_DIR: Final[str] = "resolve"
KEPT_FORM: Final[str] = "kept"
FRESH_FORM: Final[str] = "fresh"
RECORD_SUFFIX: Final[str] = ".fetch.json"
FAILURE_SUFFIX: Final[str] = ".fetch-failed.json"
PARTIAL_SUFFIX: Final[str] = ".partial"
STAGE_RECORD_NAME: Final[str] = "stage.json"
STAGE_FAILURE_NAME: Final[str] = "stage-failed.json"
STAGE_TABLES: Final = (
    "courts", "dockets", "opinion-clusters", "citations", "opinions",
)
COURT_PATTERN: Final[str] = r"[a-z0-9]{1,32}"
STAGE_FAILURE_REASONS: Final = frozenset({
    "invalid", "missing-input", "input-mismatch", "unknown-court",
    "malformed", "local", "busy",
})
RESERVED_SUFFIXES: Final = (PARTIAL_SUFFIX, RECORD_SUFFIX, FAILURE_SUFFIX)
# exempt: two fetch lanes are the starting value until the corpus run measures one.
LANE_COUNT: Final[int] = 2
SEGMENT_PATTERN: Final[str] = r"[A-Za-z0-9][A-Za-z0-9._-]*"
SOURCE_PATTERN: Final[str] = rf"{SEGMENT_PATTERN}-[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}"
DNS_PATTERN: Final[str] = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
# exempt: the address bound limits a queue argument's size.
URL_MAX_LENGTH: Final[int] = 2048
FAILURE_REASONS: Final = frozenset({
    "invalid", "refused-host", "egress-failed", "upstream-status", "changed",
    "redirect", "transport", "local", "busy",
})

DATABASE_HOST_ENV: Final[str] = "GIDEON_WORKER_DB_HOST"
DATABASE_PORT_ENV: Final[str] = "GIDEON_WORKER_DB_PORT"
DATABASE_NAME_ENV: Final[str] = "GIDEON_WORKER_DB_NAME"
DATABASE_ROLE_ENV: Final[str] = "GIDEON_WORKER_DB_ROLE"
PASSWORD_FILE_ENV: Final[str] = "GIDEON_WORKER_PASSWORD_FILE"
CONCURRENCY_ENV: Final[str] = "GIDEON_WORKER_CONCURRENCY"


def worker_metrics_target() -> str:
    """Return the metrics listener as a Compose host and port."""

    return f"{WORKER_SERVICE_NAME}:{WORKER_METRICS_PORT}"
