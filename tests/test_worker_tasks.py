"""Verification and recovery tasks over Procrastinate's in-memory connector."""

import asyncio
import bz2
import datetime
import hashlib
import inspect
import json
import tempfile
import threading
import unittest
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import procrastinate
from procrastinate.testing import InMemoryConnector
from test_worker_dense import (
    FakeDenseRecord,
    FakeEmbedder,
    FakeVectorStore,
    dense_inputs,
)
from test_worker_lexical import TEXT, FakeRecord, FakeStore, inputs_for

from gideon.host import cas
from gideon.host.index import sample
from gideon.host.render import worker
from gideon.worker import (
    agreement,
    caselaw,
    dense,
    dupes,
    fetch,
    lexical,
    overruled,
    staging,
    tasks,
)


class Unread(httpx.SyncByteStream):
    """An answer body left unread, as the network serves it."""

    def __init__(self, content: bytes) -> None:
        self.content = content

    def __iter__(self) -> Iterator[bytes]:
        yield self.content


class RecoveryTask(unittest.TestCase):
    def test_both_index_tasks_forward_sample_selection_and_fill_a_live_generation(self) -> None:
        selection = {"gold": [], "fill_per_million": sample.SAMPLE_FILL_MAX}
        name = sample.sample_name(str(inputs_for()["name"]))
        with tempfile.TemporaryDirectory() as temporary:
            store_root, work_root = Path(temporary) / "cas", Path(temporary) / "work"
            work_root.mkdir()
            digest = hashlib.sha256(TEXT.encode()).hexdigest()
            path = cas.object_path(digest, store_root)
            path.parent.mkdir(parents=True)
            path.write_text(TEXT, encoding="utf-8")
            lexical_record, lexical_store = FakeRecord(digest), FakeStore()
            assert lexical_record.generation is not None
            lexical_record.generation = replace(lexical_record.generation, gen_id=name, state="live")
            context = procrastinate.JobContext(
                app=procrastinate.App(connector=InMemoryConnector()),
                job=procrastinate.jobs.Job(id=77, queue=lexical.LEXICAL_QUEUE, lock=None,
                                          queueing_lock=None, task_name=lexical.LEXICAL_TASK),
                start_timestamp=0.0, abort_reason=lambda: None,
            )
            with (
                patch.object(lexical, "PsycopgGenerations", return_value=lexical_record),
                patch.object(lexical, "HttpxStore", return_value=lexical_store),
                patch.object(cas, "ROOT", store_root),
                patch.object(staging, "WORK_ROOT", work_root),
            ):
                tasks.lexical(context, **{**inputs_for(), "name": name, "sample": selection})  # type: ignore[arg-type]
            self.assertIsNotNone(lexical_record.finished)
            self.assertEqual(lexical_record.generation.state, "live")
            self.assertTrue(lexical_store.rows)
            events: list[str] = []
            dense_store = FakeVectorStore(events)
            dense_record = FakeDenseRecord(digest, dense_store, events)
            embedder = FakeEmbedder(events)
            assert dense_record.generation is not None
            dense_record.generation = replace(dense_record.generation, gen_id=name, state="live")
            dense_context = procrastinate.JobContext(
                app=context.app,
                job=procrastinate.jobs.Job(id=78, queue=dense.DENSE_QUEUE, lock=None,
                                          queueing_lock=None, task_name=dense.DENSE_TASK),
                start_timestamp=0.0, abort_reason=lambda: None,
            )
            with (
                patch.object(dense, "PsycopgGenerations", return_value=dense_record),
                patch.object(dense, "HttpxVectorStore", return_value=dense_store),
                patch.object(dense, "HttpxEmbedder", return_value=embedder),
                patch.object(cas, "ROOT", store_root),
                patch.object(staging, "WORK_ROOT", work_root),
            ):
                tasks.dense(dense_context, **{**dense_inputs(), "name": name, "sample": selection})  # type: ignore[arg-type]
            self.assertIsNotNone(dense_record.finished)
            self.assertEqual(dense_record.generation.state, "live")
            self.assertEqual(dense_record.counts.dense_manifest, 1)
            self.assertTrue(dense_store.collections[name])

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
            {tasks.VERIFY_TASK, tasks.RECOVERY_TASK, fetch.FETCH_TASK,
             staging.STAGE_TASK, caselaw.CASELAW_TASK, agreement.AGREEMENT_TASK,
             lexical.LEXICAL_TASK, dense.DENSE_TASK, worker.OVERRULED_TASK, dupes.DUPES_TASK},
        )
        fetching = app.tasks[fetch.FETCH_TASK]
        self.assertEqual(fetching.queue, fetch.FETCH_QUEUE)
        self.assertTrue(fetching.pass_context)
        self.assertIsNone(fetching.queueing_lock)
        self.assertFalse(inspect.iscoroutinefunction(tasks.fetch))
        staged = app.tasks[staging.STAGE_TASK]
        self.assertEqual(staged.queue, staging.STAGE_QUEUE)
        self.assertTrue(staged.pass_context)
        self.assertIsNone(staged.queueing_lock)
        self.assertFalse(inspect.iscoroutinefunction(tasks.stage))
        measured = app.tasks[agreement.AGREEMENT_TASK]
        self.assertEqual(measured.queue, agreement.AGREEMENT_QUEUE)
        self.assertTrue(measured.pass_context)
        self.assertIsNone(measured.retry_strategy)
        self.assertFalse(inspect.iscoroutinefunction(tasks.agreement))
        indexed = app.tasks[lexical.LEXICAL_TASK]
        self.assertEqual(indexed.queue, lexical.LEXICAL_QUEUE)
        self.assertTrue(indexed.pass_context)
        self.assertIsNone(indexed.queueing_lock)
        self.assertIsNone(indexed.retry_strategy)
        self.assertFalse(inspect.iscoroutinefunction(tasks.lexical))
        embedded = app.tasks[dense.DENSE_TASK]
        self.assertEqual(embedded.queue, dense.DENSE_QUEUE)
        self.assertTrue(embedded.pass_context)
        self.assertIsNone(embedded.queueing_lock)
        self.assertIsNone(embedded.retry_strategy)
        self.assertFalse(inspect.iscoroutinefunction(tasks.dense))
        clustered = app.tasks[dupes.DUPES_TASK]
        self.assertEqual(clustered.queue, dupes.DUPES_QUEUE)
        self.assertTrue(clustered.pass_context)
        self.assertIsNone(clustered.queueing_lock)
        self.assertIsNone(clustered.retry_strategy)
        self.assertFalse(inspect.iscoroutinefunction(tasks.dupes))

    def test_dupes_job_runs_off_the_event_loop_and_passes_build_inputs(self) -> None:
        values = inputs_for()
        connector = InMemoryConnector()
        app = procrastinate.App(connector=connector)
        tasks.register_tasks(app)
        threads: list[bool] = []
        jobs: list[int] = []

        async def exercise() -> int:
            async with app.open_async():
                job = await app.configure_task(dupes.DUPES_TASK).defer_async(**values)  # type: ignore[arg-type]
                await asyncio.wait_for(
                    app.run_worker_async(
                        wait=False, listen_notify=False, queues=[dupes.DUPES_QUEUE],
                    ),
                    timeout=15,
                )
            return job

        with tempfile.TemporaryDirectory() as temporary:
            store_root, work_root = Path(temporary) / "cas", Path(temporary) / "work"
            with (
                patch.object(dupes, "PsycopgGenerations") as record_factory,
                patch.object(dupes, "FileSignatures") as signatures_factory,
                patch.object(dupes, "ProcessSketcher") as sketcher_factory,
            ):
                def through_fake(
                    inputs: Mapping[str, object], job: int,
                    record: dupes.GenerationRecord, signatures: dupes.SignatureStore,
                    sketcher: dupes.Sketcher, root: Path, *, work_root: Path,
                ) -> None:
                    threads.append(threading.current_thread() is threading.main_thread())
                    jobs.append(job)
                    self.assertEqual(inputs, {**values, "sample": None})
                    self.assertIs(record, record_factory.return_value)
                    self.assertIs(signatures, signatures_factory.return_value)
                    self.assertIs(sketcher, sketcher_factory.return_value)
                    self.assertEqual(root, store_root)
                    self.assertEqual(work_root, Path(temporary) / "work")

                with (
                    patch.object(dupes, "build", side_effect=through_fake),
                    patch.object(cas, "ROOT", store_root),
                    patch.object(staging, "WORK_ROOT", work_root),
                ):
                    job = asyncio.run(exercise())
                record_factory.assert_called_once_with()
                signatures_factory.assert_called_once_with()
                sketcher_factory.assert_called_once_with(store_root)
            self.assertEqual(connector.jobs[job]["status"], "succeeded")
            self.assertEqual(jobs, [job])
            self.assertEqual(threads, [False])

    def test_dense_jobs_run_off_the_event_loop_and_record_success_or_failure(self) -> None:
        values = dense_inputs()
        connector = InMemoryConnector()
        app = procrastinate.App(connector=connector)
        tasks.register_tasks(app)
        original_build = dense.build
        threads: list[bool] = []
        jobs: list[int] = []

        async def exercise() -> tuple[int, int]:
            async with app.open_async():
                whole = await app.configure_task(dense.DENSE_TASK).defer_async(**values)  # type: ignore[arg-type]
                failed = await app.configure_task(dense.DENSE_TASK).defer_async(**values)  # type: ignore[arg-type]
                await asyncio.wait_for(app.run_worker_async(wait=False, listen_notify=False, queues=[dense.DENSE_QUEUE]), timeout=15)
            return whole, failed

        with tempfile.TemporaryDirectory() as temporary:
            store_root, work_root = Path(temporary) / "cas", Path(temporary) / "work"
            work_root.mkdir()
            digest = hashlib.sha256(TEXT.encode()).hexdigest()
            path = cas.object_path(digest, store_root)
            path.parent.mkdir(parents=True)
            path.write_text(TEXT, encoding="utf-8")
            events: list[str] = []
            whole_store, failed_store = FakeVectorStore(events), FakeVectorStore(events)
            whole_record = FakeDenseRecord(digest, whole_store, events)
            failed_record = FakeDenseRecord(digest, failed_store, events)
            whole_embedder, failed_embedder = FakeEmbedder(events), FakeEmbedder(events)
            failed_embedder.failure = dense.DenseFailure("embed")
            records = iter((whole_record, failed_record))
            stores = iter((whole_store, failed_store))
            embedders = iter((whole_embedder, failed_embedder))

            def record_factory() -> FakeDenseRecord:
                threads.append(threading.current_thread() is threading.main_thread())
                return next(records)

            def store_factory() -> FakeVectorStore:
                threads.append(threading.current_thread() is threading.main_thread())
                return next(stores)

            def embed_factory(served_model: str, dimensions: int) -> FakeEmbedder:
                threads.append(threading.current_thread() is threading.main_thread())
                self.assertEqual((served_model, dimensions), (values["served_model"], values["dimensions"]))
                return next(embedders)

            def through_fake(
                inputs: Mapping[str, object], job: int, record: dense.GenerationRecord,
                store: dense.VectorStore, embedder: dense.Embedder, pace: dense.Pace,
                store_root: Path, *, work_root: Path,
            ) -> dense.DenseCounts:
                threads.append(threading.current_thread() is threading.main_thread())
                jobs.append(job)
                self.assertIsNone(inputs["limit"])
                self.assertIsInstance(pace, dense.NoBackOff)
                return original_build(inputs, job, record, store, embedder, pace, store_root,
                                      work_root=work_root, monotonic=lambda: 0.0)

            with (
                patch.object(dense, "PsycopgGenerations", side_effect=record_factory),
                patch.object(dense, "HttpxVectorStore", side_effect=store_factory),
                patch.object(dense, "HttpxEmbedder", side_effect=embed_factory),
                patch.object(dense, "build", side_effect=through_fake),
                patch.object(cas, "ROOT", store_root),
                patch.object(staging, "WORK_ROOT", work_root),
            ):
                whole, failed = asyncio.run(exercise())
            self.assertEqual(threads, [False] * 8)
            self.assertEqual(jobs, [whole, failed])
            self.assertEqual(connector.jobs[whole]["status"], "succeeded")
            self.assertEqual(connector.jobs[failed]["status"], "failed")
            self.assertEqual(connector.jobs[failed]["attempts"], 1)
            self.assertIsNotNone(whole_record.finished)
            self.assertEqual(failed_record.failure, "embed")
            self.assertTrue(all(client.closed for client in (
                whole_record, failed_record, whole_store, failed_store, whole_embedder, failed_embedder,
            )))

    def test_dense_task_passes_the_limit_and_preserves_build_or_constructor_failure(self) -> None:
        context = procrastinate.JobContext(
            app=procrastinate.App(connector=InMemoryConnector()),
            job=procrastinate.jobs.Job(id=77, queue=dense.DENSE_QUEUE, lock=None,
                                      queueing_lock=None, task_name=dense.DENSE_TASK),
            start_timestamp=0.0, abort_reason=lambda: None,
        )
        values = {**dense_inputs(), "limit": 3}
        failure = dense.DenseFailure("embed")
        with (
            patch.object(dense, "PsycopgGenerations") as record_factory,
            patch.object(dense, "HttpxVectorStore") as store_factory,
            patch.object(dense, "HttpxEmbedder") as embed_factory,
            patch.object(dense, "build", side_effect=failure) as build,
            self.assertRaises(dense.DenseFailure) as raised,
        ):
            tasks.dense(context, **values)  # type: ignore[arg-type]
        self.assertIs(raised.exception, failure)
        self.assertEqual(build.call_args.args[:5], ({**values, "sample": None}, 77, record_factory.return_value,
                                                   store_factory.return_value, embed_factory.return_value))
        self.assertIsInstance(build.call_args.args[5], dense.NoBackOff)
        self.assertEqual(build.call_args.args[6], cas.ROOT)
        self.assertEqual(build.call_args.kwargs, {"work_root": staging.WORK_ROOT})
        with (
            patch.object(dense, "PsycopgGenerations") as record_factory,
            patch.object(dense, "HttpxVectorStore") as store_factory,
            patch.object(dense, "HttpxEmbedder", side_effect=failure),
            self.assertRaises(dense.DenseFailure) as raised,
        ):
            record_factory.return_value.close.side_effect = OSError("private cleanup")
            tasks.dense(context, **values)  # type: ignore[arg-type]
        self.assertIs(raised.exception, failure)
        record_factory.return_value.close.assert_called_once()
        store_factory.return_value.close.assert_called_once()

    def test_dense_task_refuses_a_missing_job_id(self) -> None:
        context = procrastinate.JobContext(
            app=procrastinate.App(connector=InMemoryConnector()),
            job=procrastinate.jobs.Job(queue=dense.DENSE_QUEUE, lock=None, queueing_lock=None,
                                      task_name=dense.DENSE_TASK),
            start_timestamp=0.0, abort_reason=lambda: None,
        )
        with self.assertRaises(dense.DenseFailure) as raised:
            tasks.dense(context, **dense_inputs())  # type: ignore[arg-type]
        self.assertEqual(raised.exception.reason, "invalid")

    def test_lexical_jobs_run_off_the_event_loop_and_write_failure_to_the_row(self) -> None:
        inputs = inputs_for()
        threads: list[bool] = []
        jobs: list[int] = []
        original_build = lexical.build
        connector = InMemoryConnector()
        app = procrastinate.App(connector=connector)
        tasks.register_tasks(app)

        async def exercise() -> tuple[int, int]:
            async with app.open_async():
                whole = await app.configure_task(lexical.LEXICAL_TASK).defer_async(**inputs)  # type: ignore[arg-type]
                failed = await app.configure_task(lexical.LEXICAL_TASK).defer_async(**inputs)  # type: ignore[arg-type]
                await app.run_worker_async(wait=False, listen_notify=False)
            return whole, failed

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store_root, work_root = root / "cas", root / "work"
            store_root.mkdir()
            work_root.mkdir()
            digest = hashlib.sha256(TEXT.encode()).hexdigest()
            path = cas.object_path(digest, store_root)
            path.parent.mkdir(parents=True)
            path.write_text(TEXT)
            whole_record, failed_record = FakeRecord(digest), FakeRecord(digest)
            whole_store, failed_store = FakeStore(), FakeStore()
            failed_store.fail_at = "bulk"
            records = iter((whole_record, failed_record))
            stores = iter((whole_store, failed_store))

            def record_factory() -> FakeRecord:
                threads.append(threading.current_thread() is threading.main_thread())
                return next(records)

            def store_factory() -> FakeStore:
                threads.append(threading.current_thread() is threading.main_thread())
                return next(stores)

            def through_fake(
                inputs: Mapping[str, object], job: int, record: lexical.GenerationRecord,
                store: lexical.LexicalStore, store_root: Path, *, work_root: Path,
            ) -> lexical.BuildCounts:
                threads.append(threading.current_thread() is threading.main_thread())
                jobs.append(job)
                return original_build(inputs, job, record, store, store_root,
                                      work_root=work_root, monotonic=lambda: 0.0)

            with (
                patch.object(lexical, "PsycopgGenerations", side_effect=record_factory) as record_creation,
                patch.object(lexical, "HttpxStore", side_effect=store_factory) as store_creation,
                patch.object(lexical, "build", side_effect=through_fake),
                patch.object(cas, "ROOT", store_root),
                patch.object(staging, "WORK_ROOT", work_root),
            ):
                whole, failed = asyncio.run(exercise())
            self.assertEqual((record_creation.call_count, store_creation.call_count), (2, 2))
            self.assertEqual(threads, [False] * 6)
            self.assertEqual(jobs, [whole, failed])
            self.assertEqual(connector.jobs[whole]["status"], "succeeded")
            self.assertEqual(connector.jobs[failed]["status"], "failed")
            self.assertEqual(connector.jobs[failed]["attempts"], 1)
            self.assertIsNotNone(whole_record.finished)
            self.assertIsNone(whole_record.failure)
            self.assertEqual(failed_record.failure, "store")
            self.assertIsNotNone(failed_record.generation)
            assert failed_record.generation is not None
            self.assertEqual(failed_record.generation.state, "failed")
            self.assertTrue(whole_record.closed and failed_record.closed)
            self.assertTrue(whole_store.closed and failed_store.closed)

    def test_lexical_task_reraises_the_build_failure(self) -> None:
        app = procrastinate.App(connector=InMemoryConnector())
        context = procrastinate.JobContext(
            app=app,
            job=procrastinate.jobs.Job(
                id=77, queue=lexical.LEXICAL_QUEUE, lock=None,
                queueing_lock=None, task_name=lexical.LEXICAL_TASK,
            ),
            start_timestamp=0.0,
            abort_reason=lambda: None,
        )
        failure = lexical.LexicalFailure("store")
        with (
            patch.object(lexical, "PsycopgGenerations") as record_creation,
            patch.object(lexical, "HttpxStore") as store_creation,
            patch.object(lexical, "build", side_effect=failure),
            self.assertRaises(lexical.LexicalFailure) as raised,
        ):
            tasks.lexical(context, **inputs_for())  # type: ignore[arg-type]
        self.assertIs(raised.exception, failure)
        record_creation.return_value.fail.assert_not_called()
        store_creation.return_value.close.assert_not_called()

    def test_caselaw_task_files_and_reraises_a_job_failure(self) -> None:
        app = procrastinate.App(connector=InMemoryConnector())
        tasks.register_tasks(app)
        registered = app.tasks[caselaw.CASELAW_TASK]
        self.assertEqual(registered.queue, caselaw.CASELAW_QUEUE)
        self.assertTrue(registered.pass_context)
        self.assertIsNone(registered.retry_strategy)
        self.assertFalse(inspect.iscoroutinefunction(tasks.caselaw))

        job_id = 77
        label = "corpus-2099-01-01"
        snapshot = "fiction-2099-01-02"
        court = "fictioncourt"
        courts = {court: {"level": "circuit", "circuit": "ca6", "state": None}}
        context = procrastinate.JobContext(
            app=app,
            job=procrastinate.jobs.Job(
                id=job_id, queue=caselaw.CASELAW_QUEUE, lock=None,
                queueing_lock=None, task_name=caselaw.CASELAW_TASK,
            ),
            start_timestamp=0.0,
            abort_reason=lambda: None,
        )
        failure = caselaw.CaselawFailure("store")
        with (
            patch.object(caselaw, "PsycopgRecord") as record_factory,
            patch.object(caselaw, "ingest", side_effect=failure) as ingest,
            patch.object(caselaw, "write_job_failure") as file_failure,
            self.assertRaises(caselaw.CaselawFailure) as raised,
        ):
            tasks.caselaw(context, label, snapshot, court, courts=courts,
                          pipeline=worker.PIPELINE_VERSION)
        self.assertIs(raised.exception, failure)
        ingest.assert_called_once_with(
            tasks.SNAPSHOTS_ROOT, staging.WORK_ROOT, cas.ROOT,
            label, snapshot, court, None, job_id, record_factory.return_value,
            courts=courts, pipeline=worker.PIPELINE_VERSION,
        )
        file_failure.assert_called_once_with(
            staging.WORK_ROOT, label, snapshot, court, job_id, failure,
        )

    def test_agreement_job_runs_off_event_loop_and_files_failure(self) -> None:
        label = "corpus-2099-01-01"
        snapshot = "fictional-dump-2099-01-01"
        court = "court1"
        input = {"path": "citation-map.csv.bz2", "sha256": "0" * 64}
        threads: list[bool] = []

        def fail(*_args: object, **_kwargs: object) -> None:
            threads.append(threading.current_thread() is threading.main_thread())
            raise agreement.AgreementFailure("malformed", table=agreement.AGREEMENT_TABLE)

        with tempfile.TemporaryDirectory() as temporary:
            work_root = Path(temporary) / "work"
            (work_root / label / "fictional-dump").mkdir(parents=True)
            app = procrastinate.App(connector=InMemoryConnector())
            job_id = 77
            context = procrastinate.JobContext(
                app=app,
                job=procrastinate.jobs.Job(
                    id=job_id, queue=agreement.AGREEMENT_QUEUE, lock=None,
                    queueing_lock=None, task_name=agreement.AGREEMENT_TASK,
                ),
                start_timestamp=0.0,
                abort_reason=lambda: None,
            )

            failures: list[agreement.AgreementFailure] = []

            def exercise() -> None:
                try:
                    tasks.agreement(context, label, snapshot, court, input, worker.PIPELINE_VERSION)
                except agreement.AgreementFailure as failure:
                    failures.append(failure)

            with (
                patch.object(agreement, "agreement", side_effect=fail) as measure,
                patch.object(agreement, "PsycopgEdges"),
                patch.object(staging, "WORK_ROOT", work_root),
            ):
                thread = threading.Thread(target=exercise)
                thread.start()
                thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual([failure.reason for failure in failures], ["malformed"])
            self.assertEqual(threads, [False])
            self.assertEqual(measure.call_args.kwargs["pipeline"], worker.PIPELINE_VERSION)
            failure_path = (
                work_root / label / "fictional-dump"
                / f"{court}.{agreement.AGREEMENT_FAILURE_NAME}"
            )
            value = json.loads(failure_path.read_text())
            self.assertEqual((value["job"], value["court"], value["reason"], value["table"]),
                             (job_id, court, "malformed", agreement.AGREEMENT_TABLE))

    def test_overruled_task_runs_off_event_loop_and_files_its_failure(self) -> None:
        app = procrastinate.App(connector=InMemoryConnector())
        tasks.register_tasks(app)
        registered = app.tasks[overruled.OVERRULED_TASK]
        self.assertEqual(registered.queue, overruled.OVERRULED_QUEUE)
        self.assertTrue(registered.pass_context)
        self.assertIsNone(registered.queueing_lock)
        self.assertIsNone(registered.retry_strategy)
        self.assertFalse(inspect.iscoroutinefunction(tasks.overruled))
        label = "corpus-2099-01-03"
        snapshot = "treatment-2099-01-01"
        caselaw_snapshot = "fictional-dump-2099-01-02"
        input = {"path": "decisions-overruled.html", "sha256": "a" * 64}
        job_id = 77
        context = procrastinate.JobContext(
            app=app,
            job=procrastinate.jobs.Job(
                id=job_id, queue=overruled.OVERRULED_QUEUE, lock=None,
                queueing_lock=None, task_name=overruled.OVERRULED_TASK,
            ),
            start_timestamp=0.0, abort_reason=lambda: None,
        )
        threads: list[bool] = []
        failures: list[overruled.OverruledFailure] = []
        failure = overruled.OverruledFailure("malformed", table="decisions-overruled.html")

        def fail(*_args: object, **_kwargs: object) -> None:
            threads.append(threading.current_thread() is threading.main_thread())
            raise failure

        def exercise() -> None:
            try:
                tasks.overruled(context, label, snapshot, caselaw_snapshot, "scotus", input)
            except overruled.OverruledFailure as exc:
                failures.append(exc)

        with tempfile.TemporaryDirectory() as temporary:
            work_root = Path(temporary) / "work"
            whole = work_root / label / "treatment"
            whole.mkdir(parents=True)
            with (
                patch.object(overruled, "overruled", side_effect=fail) as run_job,
                patch.object(overruled, "PsycopgRecord") as record,
                patch.object(staging, "WORK_ROOT", work_root),
            ):
                thread = threading.Thread(target=exercise)
                thread.start()
                thread.join(2)
                self.assertFalse(thread.is_alive())
            self.assertEqual(threads, [False])
            self.assertEqual(failures, [failure])
            run_job.assert_called_once_with(
                tasks.SNAPSHOTS_ROOT, work_root, label, snapshot, caselaw_snapshot,
                "scotus", input, job_id, record.return_value,
            )
            value = json.loads((whole / "overruled-failed.json").read_text())
            self.assertEqual((value["job"], value["court"], value["reason"], value["table"]),
                             (job_id, "scotus", "malformed", "decisions-overruled.html"))

    def test_overruled_task_requires_a_job_id_before_opening_the_record(self) -> None:
        context = procrastinate.JobContext(
            app=procrastinate.App(connector=InMemoryConnector()),
            job=procrastinate.jobs.Job(
                id=None, queue=overruled.OVERRULED_QUEUE, lock=None,
                queueing_lock=None, task_name=overruled.OVERRULED_TASK,
            ),
            start_timestamp=0.0, abort_reason=lambda: None,
        )
        with (patch.object(overruled, "PsycopgRecord") as record,
              self.assertRaises(overruled.OverruledFailure) as raised):
            tasks.overruled(context, "corpus-2099-01-03", "treatment-2099-01-01",
                            "fictional-dump-2099-01-02", "scotus",
                            {"path": "decisions-overruled.html", "sha256": "a" * 64})
        self.assertEqual(raised.exception.reason, "invalid")
        record.assert_not_called()

    def test_stage_jobs_run_off_the_event_loop_and_a_failure_is_filed(self) -> None:
        snapshot = "fictions-2099-01-02"
        threads: list[bool] = []
        original_stage = staging.stage

        with tempfile.TemporaryDirectory() as temporary:
            snapshots = Path(temporary) / "snapshots"
            work = Path(temporary) / "work"
            dump = snapshots / snapshot
            dump.mkdir(parents=True)
            work.mkdir()
            bodies = {
                "courts": b"id,note\ncourt1,fictitious selected\n",
                "dockets": b"id,court_id\nd1,court1\n",
                "opinion-clusters": b"id,docket_id\ncl1,d1\n",
                "citations": b"cluster_id,note\ncl1,fictitious citation\n",
                "opinions": b"cluster_id,note\ncl1,fictitious opinion\n",
            }
            inputs: dict[str, dict[str, str]] = {}
            for table, body in bodies.items():
                path = dump / f"{table}.csv.bz2"
                compressed = bz2.compress(body)
                path.write_bytes(compressed)
                digest = hashlib.sha256(compressed).hexdigest()
                inputs[table] = {"path": path.name, "sha256": digest}
                fetch.write_record(
                    path.with_name(path.name + fetch.RECORD_SUFFIX),
                    fetch.FetchRecord(
                        "whole", fetch.KEPT_FORM, "archive.example.test", f"/{path.name}",
                        None, None, len(compressed), len(compressed), len(compressed),
                        digest, "2099-01-02T00:00:00+00:00", 3, 0.0, 0,
                    ),
                )

            def in_thread(*args: object, **kwargs: object) -> staging.StageRecord:
                threads.append(threading.current_thread() is threading.main_thread())
                return original_stage(*args, **kwargs)  # type: ignore[arg-type]

            connector = InMemoryConnector()
            app = procrastinate.App(connector=connector)
            tasks.register_tasks(app)

            async def exercise() -> tuple[int, int]:
                async with app.open_async():
                    whole = await app.configure_task(staging.STAGE_TASK).defer_async(
                        label="corpus-2099-01-03", snapshot=snapshot,
                        courts=["court1"], inputs=inputs,  # type: ignore[arg-type]
                    )
                    missing = await app.configure_task(staging.STAGE_TASK).defer_async(
                        label="corpus-2099-01-04", snapshot=snapshot,
                        courts=["absent"], inputs=inputs,  # type: ignore[arg-type]
                    )
                    await app.run_worker_async(wait=False, listen_notify=False)
                return whole, missing

            with (
                patch.object(staging, "stage", new=in_thread),
                patch.object(tasks, "SNAPSHOTS_ROOT", snapshots),
                patch.object(staging, "WORK_ROOT", work),
            ):
                whole, missing = asyncio.run(exercise())
            self.assertEqual(connector.jobs[whole]["status"], "succeeded")
            self.assertEqual(connector.jobs[missing]["status"], "failed")
            self.assertEqual(threads, [False, False])
            record_path = work / "corpus-2099-01-03/fictions" / staging.STAGE_RECORD_NAME
            self.assertEqual(staging.read_record(record_path).job, whole)  # type: ignore[union-attr]
            failure_path = work / "corpus-2099-01-04" / f"fictions.{staging.STAGE_FAILURE_NAME}"
            failure = json.loads(failure_path.read_text())
            self.assertEqual((failure["job"], failure["reason"], failure["court"]),
                             (missing, "unknown-court", "absent"))

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
