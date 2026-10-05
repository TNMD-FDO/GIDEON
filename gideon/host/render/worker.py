"""Names shared by the queue worker's render and host command."""

from typing import Final

WORKER_SERVICE_NAME: Final[str] = "gideon-worker"
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

DATABASE_HOST_ENV: Final[str] = "GIDEON_WORKER_DB_HOST"
DATABASE_PORT_ENV: Final[str] = "GIDEON_WORKER_DB_PORT"
DATABASE_NAME_ENV: Final[str] = "GIDEON_WORKER_DB_NAME"
DATABASE_ROLE_ENV: Final[str] = "GIDEON_WORKER_DB_ROLE"
PASSWORD_FILE_ENV: Final[str] = "GIDEON_WORKER_PASSWORD_FILE"
CONCURRENCY_ENV: Final[str] = "GIDEON_WORKER_CONCURRENCY"
