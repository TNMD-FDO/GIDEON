"""Verification and recovery tasks over Procrastinate's in-memory connector."""

import asyncio
import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import procrastinate
from procrastinate.testing import InMemoryConnector

from gideon.host.render import worker
from gideon.worker import tasks


class RecoveryTask(unittest.TestCase):
    def test_registered_on_maintenance_with_one_minute_cron_and_lock(self) -> None:
        app = procrastinate.App(connector=InMemoryConnector())
        tasks.register_tasks(app)
        registered = app.tasks[tasks.RECOVERY_TASK]
        self.assertEqual(
            registered.queue, tasks.RECOVERY_QUEUE,
            "Fix: fetch recovery work from the maintenance queue.",
        )
        self.assertEqual(
            registered.queueing_lock, tasks.RECOVERY_LOCK,
            "Fix: prevent a backlog of waiting recovery jobs.",
        )
        periodic = app.periodic_registry.periodic_tasks[(tasks.RECOVERY_TASK, "")]
        self.assertEqual(
            periodic.cron, tasks.RECOVERY_CRON,
            "Fix: schedule recovery once per minute.",
        )
        verify = app.tasks[tasks.VERIFY_TASK]
        self.assertEqual(
            (verify.name, verify.queue),
            (worker.WORKER_VERIFY_TASK, worker.WORKER_VERIFY_QUEUE),
            "Fix: keep the registered verify task equal to the host's queue identity.",
        )

    def test_verify_job_runs_without_hold_and_with_injected_sleep(self) -> None:
        for hold in (None, 7):
            with self.subTest(hold=hold):
                connector = InMemoryConnector()
                app = procrastinate.App(connector=connector)
                tasks.register_tasks(app)
                sleep = AsyncMock()

                async def exercise(
                    app: procrastinate.App, hold: int | None, sleep: AsyncMock
                ) -> int:
                    async with app.open_async():
                        configured = app.configure_task(tasks.VERIFY_TASK)
                        kwargs = {} if hold is None else {"hold_seconds": hold}
                        job_id = await configured.defer_async(**kwargs)
                        with patch.object(tasks, "asyncio", SimpleNamespace(sleep=sleep)):
                            await app.run_worker_async(wait=False, listen_notify=False)
                    return job_id

                job_id = asyncio.run(exercise(app, hold, sleep))
                self.assertEqual(
                    connector.jobs[job_id]["status"], "succeeded",
                    "Fix: register and execute the verify task through the queue.",
                )
                if hold is None:
                    sleep.assert_not_awaited()
                else:
                    sleep.assert_awaited_once_with(hold)

    def test_stalled_job_is_retried_and_live_job_is_left_doing(self) -> None:
        connector = InMemoryConnector()
        app = procrastinate.App(connector=connector)
        tasks.register_tasks(app)

        async def exercise() -> None:
            defer = app.configure_task("fixture.job", queue="fixture")
            stalled_id = await defer.defer_async()
            live_id = await defer.defer_async()
            stalled_worker = await app.job_manager.register_worker()
            stalled_job = await app.job_manager.fetch_job(None, stalled_worker)
            self.assertIsNotNone(stalled_job)
            live_worker = await app.job_manager.register_worker()
            live_job = await app.job_manager.fetch_job(None, live_worker)
            self.assertIsNotNone(live_job)
            connector.workers[stalled_worker] = (
                procrastinate.utils.utcnow()
                - datetime.timedelta(seconds=tasks.STALLED_SECONDS + 1)
            )
            context = procrastinate.JobContext(
                app=app,
                job=app.tasks[tasks.RECOVERY_TASK].configure().job,
                start_timestamp=0.0,
                abort_reason=lambda: None,
            )
            await tasks.retry_stalled_jobs(context, 0)
            self.assertEqual(
                connector.jobs[stalled_id]["status"], "todo",
                "Fix: retry jobs held by workers with stale heartbeats.",
            )
            self.assertEqual(connector.jobs[stalled_id]["attempts"], 1)
            self.assertIn(
                "deferred_for_retry",
                [event["type"] for event in connector.events[stalled_id]],
            )
            self.assertEqual(
                connector.jobs[live_id]["status"], "doing",
                "Fix: leave work owned by a live worker untouched.",
            )
            self.assertEqual(connector.jobs[live_id]["attempts"], 0)

        asyncio.run(exercise())

    def test_a_retry_refused_by_its_queueing_lock_does_not_stop_the_pass(self) -> None:
        app = procrastinate.App(connector=InMemoryConnector())
        tasks.register_tasks(app)
        first = SimpleNamespace(id=1)
        second = SimpleNamespace(id=2)
        retried: list[int] = []

        async def retry_job(job: SimpleNamespace) -> None:
            if job is first:
                raise procrastinate.exceptions.UniqueViolation(
                    constraint_name="procrastinate_jobs_queueing_lock_idx_v1",
                    queueing_lock=tasks.RECOVERY_LOCK,
                )
            retried.append(job.id)

        context = SimpleNamespace(app=SimpleNamespace(job_manager=SimpleNamespace(
            get_stalled_jobs=AsyncMock(return_value=[first, second]),
            retry_job=retry_job,
        )))
        asyncio.run(tasks.retry_stalled_jobs(context, 0))  # type: ignore[arg-type]
        self.assertEqual(
            retried, [2],
            "Fix: a stalled job whose queueing lock is held must not stop the recovery pass.",
        )


if __name__ == "__main__":
    unittest.main()
