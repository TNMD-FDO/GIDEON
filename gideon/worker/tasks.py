"""Queue worker tasks for verification and stalled-job recovery."""

import asyncio
import logging
from typing import Final

import procrastinate
from procrastinate.exceptions import UniqueViolation

VERIFY_TASK: Final = "gideon.worker.tasks.verify"
VERIFY_QUEUE: Final = "verify"
RECOVERY_TASK: Final = "gideon.worker.tasks.retry_stalled_jobs"
RECOVERY_QUEUE: Final = "maintenance"
# exempt: one-minute recovery bounds the time a stopped job waits to run again.
RECOVERY_CRON: Final = "* * * * *"
RECOVERY_LOCK: Final = "retry_stalled_jobs"
# exempt: thirty seconds allows several missed ten-second worker heartbeats.
STALLED_SECONDS: Final = 30

_LOGGER = logging.getLogger(__name__)


async def verify(hold_seconds: float = 0) -> None:
    """Hold one queue job open when requested by an operator proof."""

    if hold_seconds:
        await asyncio.sleep(hold_seconds)


async def retry_stalled_jobs(context: procrastinate.JobContext, timestamp: int) -> None:
    """Return jobs held by stalled workers to the queue."""

    del timestamp
    stalled = await context.app.job_manager.get_stalled_jobs(
        seconds_since_heartbeat=STALLED_SECONDS
    )
    for job in stalled:
        try:
            await context.app.job_manager.retry_job(job)
        except UniqueViolation:
            # A job under the same queueing lock already waits, so this one
            # stays doing and is retried by a later pass once that one has run;
            # the rest of this pass goes on.
            _LOGGER.info("stalled job %d waits for its queueing lock", job.id)


def register_tasks(app: procrastinate.App) -> None:
    """Register verification and periodic stalled-job recovery."""

    app.task(name=VERIFY_TASK, queue=VERIFY_QUEUE)(verify)
    task = app.task(
        name=RECOVERY_TASK,
        queue=RECOVERY_QUEUE,
        queueing_lock=RECOVERY_LOCK,
        pass_context=True,
    )(retry_stalled_jobs)
    app.periodic(cron=RECOVERY_CRON)(task)
