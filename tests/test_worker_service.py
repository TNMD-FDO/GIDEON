"""Queue worker settings, startup, health, pool, and log contracts."""

import asyncio
import io
import logging
import signal
import tempfile
import threading
import unittest
from collections.abc import Iterator, Mapping
from contextlib import redirect_stderr
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import procrastinate
from procrastinate.testing import InMemoryConnector

from gideon.worker import app as worker_app
from gideon.worker import health, logs, settings, tasks
from gideon.worker.__main__ import (
    SCHEMA_PROBE,
    install_stop_handlers,
    main,
    wait_for_schema,
)


class FakeCursor:
    def __init__(self, row: tuple[bool] | None = None, error: Exception | None = None) -> None:
        self.row = row
        self.error = error
        self.statements: list[str] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str) -> None:
        self.statements.append(statement)
        if self.error is not None:
            raise self.error

    def fetchone(self) -> tuple[bool] | None:
        return self.row


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self._cursor


class NoPauseEvent(threading.Event):
    def __init__(self) -> None:
        super().__init__()
        self.waits = 0

    def wait(self, timeout: float | None = None) -> bool:
        self.waits += 1
        return self.is_set()


class FakeRunApp:
    def __init__(self, events: list[str], failure: Exception | None = None) -> None:
        self.events = events
        self.failure = failure

    def open_async(self) -> "FakeRunApp":
        return self

    async def __aenter__(self) -> "FakeRunApp":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def run_worker_async(self) -> None:
        self.events.append("run")
        if self.failure is not None:
            raise self.failure


class BrokenEnvironment(Mapping[str, str]):
    def __init__(self, error: Exception) -> None:
        self.error = error

    def __getitem__(self, key: str) -> str:
        raise self.error

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0


class WorkerSettings(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.password_file = Path(self.directory.name) / "password"
        self.password_file.write_text("first-password\n", encoding="utf-8")
        self.environ = {
            settings.DATABASE_HOST_ENV: "postgres",
            settings.DATABASE_PORT_ENV: "5432",
            settings.DATABASE_NAME_ENV: "gideon",
            settings.DATABASE_ROLE_ENV: "gideon_worker",
            settings.PASSWORD_FILE_ENV: str(self.password_file),
            settings.CONCURRENCY_ENV: "4",
        }

    def test_each_missing_or_empty_variable_names_itself(self) -> None:
        for name in self.environ:
            for value in (None, " "):
                with self.subTest(name=name, value=value):
                    environ = dict(self.environ)
                    if value is None:
                        environ.pop(name)
                    else:
                        environ[name] = value
                    with self.assertRaisesRegex(ValueError, name):
                        settings.load_settings(environ)

    def test_port_and_concurrency_refuse_invalid_numbers(self) -> None:
        for name, value in (
            (settings.DATABASE_PORT_ENV, "0"),
            (settings.DATABASE_PORT_ENV, "65536"),
            (settings.DATABASE_PORT_ENV, "bad"),
            (settings.CONCURRENCY_ENV, "0"),
            (settings.CONCURRENCY_ENV, "bad"),
        ):
            with (
                self.subTest(name=name, value=value),
                self.assertRaisesRegex(ValueError, name),
            ):
                settings.load_settings({**self.environ, name: value})

    def test_missing_and_empty_password_files_name_the_path(self) -> None:
        self.password_file.unlink()
        with self.assertRaisesRegex(ValueError, str(self.password_file)):
            settings.load_settings(self.environ)
        self.password_file.write_text(" \n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, str(self.password_file)):
            settings.load_settings(self.environ)

    def test_settings_keep_only_the_password_path(self) -> None:
        loaded = settings.load_settings(self.environ)
        self.assertEqual(loaded.password_file, self.password_file)
        self.assertNotIn("first-password", repr(loaded))

    def test_wait_retries_connection_then_probe_and_reports_ready(self) -> None:
        loaded = settings.load_settings(self.environ)
        cursor = FakeCursor()
        calls: list[dict[str, object]] = []

        def connect(**kwargs: object) -> FakeConnection:
            calls.append(kwargs)
            if len(calls) == 1:
                raise ConnectionError("connection text must stay out of logs")
            if len(calls) == 2:
                return FakeConnection(FakeCursor(error=LookupError("schema absent")))
            return FakeConnection(cursor)

        stop = NoPauseEvent()
        self.assertTrue(wait_for_schema(loaded, stop, connect))
        self.assertEqual(len(calls), 3)
        self.assertEqual(stop.waits, 2)
        self.assertEqual(cursor.statements, [SCHEMA_PROBE])
        self.assertTrue(all(call["password"] == "first-password" for call in calls))

    def test_sigterm_ends_the_schema_wait_cleanly(self) -> None:
        stop = threading.Event()
        previous = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGTERM, signal.SIGINT)
        }
        try:
            install_stop_handlers(stop)
            handler = signal.getsignal(signal.SIGTERM)
            self.assertTrue(callable(handler))
            assert callable(handler)
            calls = 0

            def connect(**_kwargs: object) -> FakeConnection:
                nonlocal calls
                calls += 1
                handler(signal.SIGTERM, None)
                raise ConnectionError("database unavailable")

            self.assertFalse(
                wait_for_schema(settings.load_settings(self.environ), stop, connect)
            )
            self.assertEqual(calls, 1)
        finally:
            for signum, previous_handler in previous.items():
                signal.signal(signum, previous_handler)

    def test_health_exit_codes_and_content_free_failures(self) -> None:
        for row, failure, expected in (
            ((True,), None, (0, "")),
            ((False,), None, (1, "WorkerHeartbeatStale\n")),
            (None, None, (1, "WorkerNotRegistered\n")),
            (None, ConnectionError("private connection detail"), (1, "ConnectionError\n")),
        ):
            with self.subTest(row=row, failure=failure):
                output = io.StringIO()
                cursor = FakeCursor(row=row)

                def connect(
                    *, current_failure: Exception | None = failure,
                    current_cursor: FakeCursor = cursor,
                    **_kwargs: object,
                ) -> FakeConnection:
                    if current_failure is not None:
                        raise current_failure
                    return FakeConnection(current_cursor)

                with redirect_stderr(output):
                    code = health.main(connect, self.environ)
                self.assertEqual((code, output.getvalue()), expected)
                self.assertNotIn("private", output.getvalue())
                if failure is None:
                    self.assertEqual(cursor.statements, [health.HEARTBEAT_QUERY])

    def test_application_options_pool_size_and_password_refresh(self) -> None:
        loaded = settings.load_settings(self.environ)
        app = worker_app.build_app(loaded)
        self.assertIsInstance(app, procrastinate.App)
        self.assertEqual(app.import_paths, [])
        self.assertIn("gideon.worker.tasks.retry_stalled_jobs", app.tasks)
        self.assertEqual(
            app.worker_defaults,
            {
                "queues": None,
                "concurrency": loaded.concurrency,
                "wait": True,
                "listen_notify": True,
                "delete_jobs": "never",
                "update_heartbeat_interval": 10.0,
                "stalled_worker_timeout": 30.0,
                "shutdown_graceful_timeout": None,
            },
            "Fix: state every worker option explicitly when constructing the app.",
        )
        connector = app.connector
        self.assertIsInstance(connector, procrastinate.PsycopgConnector)
        assert isinstance(connector, procrastinate.PsycopgConnector)
        self.assertEqual(connector._pool_args["min_size"], loaded.concurrency + 1)
        self.assertEqual(connector._pool_args["max_size"], loaded.concurrency + 1)
        kwargs = connector._pool_args["kwargs"]
        self.assertTrue(callable(kwargs))
        assert callable(kwargs)
        self.assertEqual(kwargs()["password"], "first-password")
        self.password_file.write_text("rotated-password\n", encoding="utf-8")
        self.assertEqual(
            kwargs()["password"], "rotated-password",
            "Fix: resolve the mounted password for every new pool connection.",
        )

    def test_startup_prints_settings_refusal_and_other_exception_class(self) -> None:
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        try:
            for environ, expected in (
                ({**self.environ, settings.DATABASE_PORT_ENV: "bad"},
                 settings.DATABASE_PORT_ENV),
                ({**self.environ, settings.PASSWORD_FILE_ENV: str(self.password_file)},
                 str(self.password_file)),
                (BrokenEnvironment(ValueError("private environment detail")), "ValueError"),
                (BrokenEnvironment(RuntimeError("private environment detail")), "RuntimeError"),
            ):
                with self.subTest(expected=expected):
                    if expected == str(self.password_file):
                        self.password_file.unlink()
                    output = io.StringIO()
                    with redirect_stderr(output):
                        code = main(environ)
                    self.assertEqual(code, 1)
                    self.assertIn(expected, output.getvalue())
                    if expected in ("ValueError", "RuntimeError"):
                        self.assertNotIn("private environment detail", output.getvalue())
                    else:
                        self.assertIn("Fix:", output.getvalue())
                    self.assertNotIn("first-password", output.getvalue())
        finally:
            root.handlers[:] = handlers
            root.setLevel(level)

    def _run_main_case(self, failure: Exception | None) -> tuple[list[str], int, str]:
        events: list[str] = []
        server = Mock(spec=HTTPServer)
        server.shutdown.side_effect = lambda: events.append("stop")
        server.server_close.side_effect = lambda: events.append("close")

        def wait(*_args: object) -> bool:
            events.append("wait")
            return True

        def listen(_settings: settings.Settings) -> HTTPServer:
            events.append("listen")
            return server

        def build(_settings: settings.Settings) -> FakeRunApp:
            events.append("build")
            return FakeRunApp(events, failure)

        output = io.StringIO()
        with (
            patch("gideon.worker.__main__.install_handler"),
            patch("gideon.worker.__main__.install_stop_handlers"),
            patch("gideon.worker.__main__.wait_for_schema", side_effect=wait),
            patch("gideon.worker.__main__.build_app", side_effect=build),
            redirect_stderr(output),
        ):
            code = main(self.environ, server_factory=listen)
        return events, code, output.getvalue()

    def test_main_starts_listener_after_wait_and_stops_after_run(self) -> None:
        for failure in (None, RuntimeError("private run detail")):
            with self.subTest(failure=failure):
                events, code, output = self._run_main_case(failure)
                self.assertEqual(events, ["wait", "listen", "build", "run", "stop", "close"])
                self.assertEqual(code, 0 if failure is None else 1)
                if failure is None:
                    self.assertEqual(output, "")
                else:
                    self.assertIn("RuntimeError", output)
                    self.assertNotIn("private run detail", output)

    def test_listener_bind_failure_refuses_before_building_app(self) -> None:
        events: list[str] = []

        def wait(*_args: object) -> bool:
            events.append("wait")
            return True

        def listen(_settings: settings.Settings) -> HTTPServer:
            events.append("listen")
            raise OSError("private bind detail")

        output = io.StringIO()
        with (
            patch("gideon.worker.__main__.install_handler"),
            patch("gideon.worker.__main__.install_stop_handlers"),
            patch("gideon.worker.__main__.wait_for_schema", side_effect=wait),
            patch("gideon.worker.__main__.build_app") as build,
            redirect_stderr(output),
        ):
            code = main(self.environ, server_factory=listen)
        self.assertEqual(code, 1)
        self.assertEqual(events, ["wait", "listen"])
        build.assert_not_called()
        self.assertIn("worker failed: OSError.", output.getvalue())
        self.assertNotIn("private bind detail", output.getvalue())

    def test_log_handler_removes_values_and_retains_safe_fields(self) -> None:
        output = io.StringIO()
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        try:
            logs.install_handler(output)
            logger = logging.getLogger("procrastinate.worker")
            logger.error(
                "arguments=%s",
                "ARG_SENTINEL",
                extra={
                    "action": "job_error",
                    "job": {
                        "id": 17,
                        "task_name": "gideon.worker.tasks.retry_stalled_jobs",
                        "queue": "maintenance",
                        "result": "RESULT_SENTINEL",
                    },
                    "result": "RESULT_SENTINEL",
                },
                exc_info=(ValueError, ValueError("EXCEPTION_SENTINEL"), None),
            )
        finally:
            root.handlers[:] = handlers
            root.setLevel(level)
        text = output.getvalue()
        for sentinel in ("ARG_SENTINEL", "RESULT_SENTINEL", "EXCEPTION_SENTINEL"):
            self.assertNotIn(
                sentinel, text,
                "Fix: rewrite queue records on the stream handler before formatting.",
            )
        for value in ("job_id=17", "retry_stalled_jobs", "ValueError"):
            self.assertIn(value, text)

    def test_log_handler_takes_the_queues_false_exc_info(self) -> None:
        output = io.StringIO()
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        try:
            logs.install_handler(output)
            logging.getLogger("procrastinate.worker").log(
                logging.INFO,
                "Job succeeded",
                extra={"action": "job_success", "job": {"id": 3}},
                exc_info=False,
            )
        finally:
            root.handlers[:] = handlers
            root.setLevel(level)
        self.assertIn("action=job_success job_id=3", output.getvalue())

    def test_handler_drops_http_and_citation_loggers_at_every_level(self) -> None:
        output = io.StringIO()
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        try:
            handler = logs.install_handler(output)
            for name in (
                "httpx", "httpx._client", "httpcore", "httpcore.connection",
                "eyecite", "eyecite.find", "reporters_db", "reporters_db.data",
                "courts_db", "courts_db.data",
            ):
                for severity in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR):
                    record = logging.LogRecord(name, severity, __file__, 1,
                                               "SECRET_URL_SENTINEL", (), None)
                    self.assertFalse(handler.filter(record))
                    handler.handle(record)
            logging.getLogger("gideon.worker.fetch").info("safe fetch event")
        finally:
            root.handlers[:] = handlers
            root.setLevel(level)
        self.assertNotIn("SECRET_URL_SENTINEL", output.getvalue())
        self.assertIn("safe fetch event", output.getvalue())

    def test_a_job_finishes_through_the_installed_handler(self) -> None:
        output = io.StringIO()
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        connector = InMemoryConnector()
        app = procrastinate.App(connector=connector)
        tasks.register_tasks(app)

        async def exercise() -> int:
            async with app.open_async():
                job_id = await app.configure_task(tasks.VERIFY_TASK).defer_async()
                await app.run_worker_async(wait=False, listen_notify=False)
            return job_id

        try:
            logs.install_handler(output)
            job_id = asyncio.run(exercise())
        finally:
            root.handlers[:] = handlers
            root.setLevel(level)
        self.assertEqual(
            connector.jobs[job_id]["status"], "succeeded",
            "Fix: a log filter must never raise inside the queue's job path.",
        )
        self.assertIn(f"job_id={job_id}", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())


if __name__ == "__main__":
    unittest.main()
