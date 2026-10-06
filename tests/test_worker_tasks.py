"""Verification and recovery tasks over Procrastinate's in-memory connector."""

import asyncio
import datetime
import inspect
import tempfile
import threading
import unittest
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import procrastinate
from procrastinate.testing import InMemoryConnector

from gideon.host.render import worker
from gideon.worker import fetch, tasks


class Unread(httpx.SyncByteStream):
    """An answer body left unread, as the network serves it."""

    def __init__(self, content: bytes) -> None:
        self.content = content

    def __iter__(self) -> Iterator[bytes]:
        yield self.content


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
        self.assertEqual(
            {name for name in app.tasks if name.startswith("gideon.worker.tasks.")},
            {tasks.VERIFY_TASK, tasks.RECOVERY_TASK, fetch.FETCH_TASK},
        )
        fetching = app.tasks[fetch.FETCH_TASK]
        self.assertEqual(fetching.queue, fetch.FETCH_QUEUE)
        self.assertTrue(fetching.pass_context)
        self.assertIsNone(fetching.queueing_lock)
        self.assertFalse(inspect.iscoroutinefunction(tasks.fetch))

    def test_fetch_jobs_run_off_the_event_loop_and_a_failure_is_filed(self) -> None:
        body = b"Fictitious queued corpus object."
        threads: list[bool] = []
        original_transfer = fetch.transfer

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("missing"):
                return httpx.Response(404)
            return httpx.Response(200, stream=Unread(body))

        def through_fake(
            root: Path, destination: str, url: str, form: str, job: int
        ) -> fetch.FetchRecord:
            threads.append(threading.current_thread() is threading.main_thread())
            return original_transfer(
                root, destination, url, form, job,
                client_factory=lambda: httpx.Client(transport=httpx.MockTransport(respond)),
            )

        connector = InMemoryConnector()
        app = procrastinate.App(connector=connector)
        tasks.register_tasks(app)

        async def exercise() -> tuple[int, int]:
            async with app.open_async():
                whole = await app.configure_task(fetch.FETCH_TASK).defer_async(
                    destination="fictions-2026-09-30/whole",
                    url="https://archive.example.test/whole", form=fetch.KEPT_FORM,
                )
                missing = await app.configure_task(fetch.FETCH_TASK).defer_async(
                    destination="fictions-2026-09-30/missing",
                    url="https://archive.example.test/missing", form=fetch.KEPT_FORM,
                )
                await app.run_worker_async(wait=False, listen_notify=False)
            return whole, missing

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(tasks, "transfer", side_effect=through_fake),
                patch.object(tasks, "SNAPSHOTS_ROOT", root),
            ):
                whole, missing = asyncio.run(exercise())
            self.assertEqual(connector.jobs[whole]["status"], "succeeded")
            self.assertEqual(connector.jobs[missing]["status"], "failed")
            self.assertEqual(threads, [False, False],
                             "Fix: keep the fetch task synchronous, so the queue runs it "
                             "in a thread and the heartbeat's loop stays free.")
            self.assertEqual((root / "fictions-2026-09-30/whole").read_bytes(), body)
            failure = fetch.read_failure(
                root / ("fictions-2026-09-30/missing" + fetch.FAILURE_SUFFIX)
            )
            assert failure is not None
            self.assertEqual((failure.job, failure.reason, failure.status),
                             (missing, "upstream-status", 404))

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
