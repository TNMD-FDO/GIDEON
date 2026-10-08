"""Worker queue metrics, bounded reads, and HTTP scrape responses."""

import logging
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from decimal import Decimal
from http.server import HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

import psycopg

from gideon.worker import caselaw, fetch, health, metrics, staging, tasks
from gideon.worker.settings import Settings, connection_kwargs


class FakeCursor:
    def __init__(
        self,
        rows: list[tuple[str, str, int]],
        heartbeat: tuple[Decimal] | None,
        failure: Exception | None = None,
    ) -> None:
        self.rows = rows
        self.heartbeat = heartbeat
        self.failure = failure
        self.statements: list[str] = []
        self.exited = False

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        self.exited = True

    def execute(self, statement: str) -> None:
        self.statements.append(statement)
        if len(self.statements) == 2 and self.failure is not None:
            raise self.failure

    def fetchall(self) -> list[tuple[str, str, int]]:
        return self.rows

    def fetchone(self) -> tuple[Decimal] | None:
        return self.heartbeat


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.exited = False

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        self.exited = True

    def cursor(self) -> FakeCursor:
        return self._cursor


class WorkerMetrics(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        password_file = Path(directory.name) / "password"
        password_file.write_text("fictitious-password\n", encoding="utf-8")
        self.settings = Settings(
            database_host="example.invalid",
            database_port=6543,
            database_name="example",
            database_role="example_worker",
            password_file=password_file,
            concurrency=7,
        )

    def test_formatter_emits_known_zeros_and_optional_heartbeat(self) -> None:
        body = metrics.format_metrics([], None, self.settings.concurrency)
        for queue in (
            tasks.VERIFY_QUEUE, tasks.RECOVERY_QUEUE, fetch.FETCH_QUEUE,
            staging.STAGE_QUEUE, caselaw.CASELAW_QUEUE,
        ):
            for status in ("todo", "doing"):
                with self.subTest(queue=queue, status=status):
                    self.assertIn(
                        f'{metrics.QUEUE_JOBS_METRIC}{{queue="{queue}",status="{status}"}} 0\n',
                        body,
                    )
        self.assertEqual(body.count(f"{metrics.QUEUE_JOBS_METRIC}{{"), 10)
        self.assertIn(f"# HELP {metrics.QUEUE_JOBS_METRIC} M15 ", body)
        self.assertIn(f"# TYPE {metrics.QUEUE_JOBS_METRIC} gauge\n", body)
        self.assertIn(f"# TYPE {metrics.HEARTBEAT_AGE_METRIC} gauge\n", body)
        self.assertFalse(
            any(line.startswith(f"{metrics.HEARTBEAT_AGE_METRIC} ") for line in body.splitlines())
        )
        self.assertIn(f"{metrics.CONCURRENCY_METRIC} {self.settings.concurrency}\n", body)
        self.assertTrue(body.endswith("\n"))

    def test_formatter_passes_through_extra_queue_and_escapes_its_label(self) -> None:
        extra = 'extra\\path"\nqueue'
        rows = [
            (extra, "doing", 3),
            (tasks.VERIFY_QUEUE, "todo", 2),
        ]
        body = metrics.format_metrics(rows, 12.5, self.settings.concurrency)
        self.assertEqual(body, metrics.format_metrics(reversed(rows), 12.5, self.settings.concurrency))
        self.assertIn(
            f'{metrics.QUEUE_JOBS_METRIC}{{queue="extra\\\\path\\"\\nqueue",status="doing"}} 3\n',
            body,
        )
        self.assertIn(
            f'{metrics.QUEUE_JOBS_METRIC}{{queue="{tasks.VERIFY_QUEUE}",status="todo"}} 2\n',
            body,
        )
        self.assertIn(f"{metrics.HEARTBEAT_AGE_METRIC} 12.5\n", body)

    def test_read_uses_two_bounded_statements_on_one_connection(self) -> None:
        rows = [(tasks.VERIFY_QUEUE, "todo", 2)]
        cursor = FakeCursor(rows, (Decimal("12.5"),))
        connection = FakeConnection(cursor)
        calls: list[dict[str, object]] = []

        def connect(**kwargs: object) -> FakeConnection:
            calls.append(kwargs)
            return connection

        self.assertEqual(metrics.read_metrics(self.settings, connect), (rows, 12.5))
        self.assertEqual(len(calls), 1)
        self.assertTrue(connection.exited)
        self.assertTrue(cursor.exited)
        self.assertEqual(calls[0]["connect_timeout"], health.CONNECT_TIMEOUT)
        self.assertEqual(
            {key: calls[0][key] for key in connection_kwargs(self.settings)},
            connection_kwargs(self.settings),
        )
        options = calls[0]["options"]
        self.assertIsInstance(options, str)
        assert isinstance(options, str)
        match = re.fullmatch(r"-c statement_timeout=([0-9]+)", options)
        self.assertIsNotNone(match)
        assert match is not None
        statement_seconds = int(match.group(1)) / 1000
        self.assertLess(health.CONNECT_TIMEOUT + 2 * statement_seconds, 10)
        self.assertEqual(len(cursor.statements), 2)
        depth, heartbeat = cursor.statements
        self.assertIn("procrastinate_jobs", depth)
        self.assertRegex(depth, r"status IN \('todo', 'doing'\)")
        self.assertRegex(depth, r"GROUP BY queue_name, status")
        self.assertIn("procrastinate_workers", heartbeat)
        self.assertIn("last_heartbeat", heartbeat)

    def test_cancelled_statement_exits_the_connection_context(self) -> None:
        failure = psycopg.errors.QueryCanceled("private database detail")
        cursor = FakeCursor([], None, failure)
        connection = FakeConnection(cursor)

        def connect(**_kwargs: object) -> FakeConnection:
            return connection

        with self.assertRaises(psycopg.errors.QueryCanceled):
            metrics.read_metrics(self.settings, connect)
        self.assertEqual(len(cursor.statements), 2)
        self.assertTrue(cursor.exited)
        self.assertTrue(connection.exited)

    def test_server_answers_scrapes_and_logs_only_a_read_failure(self) -> None:
        rows = [(tasks.VERIFY_QUEUE, "todo", 2)]
        failing = False

        def read(_settings: Settings) -> metrics.MetricsRead:
            if failing:
                raise ValueError("private database detail")
            return rows, 12.5

        before = set(threading.enumerate())
        server = metrics.start_metrics_server(
            self.settings,
            host="127.0.0.1",
            port=0,
            read=read,
        )
        serve_threads = set(threading.enumerate()) - before
        try:
            self.assertIs(type(server), HTTPServer)
            self.assertNotIsInstance(server, ThreadingMixIn)
            self.assertEqual(len(serve_threads), 1)
            self.assertTrue(next(iter(serve_threads)).daemon)
            address = f"http://127.0.0.1:{server.server_port}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with self.assertNoLogs(metrics.logger, level=logging.INFO):
                with opener.open(address + metrics.METRICS_PATH, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["Content-Type"], metrics.CONTENT_TYPE)
                    self.assertEqual(
                        response.read().decode("utf-8"),
                        metrics.format_metrics(rows, 12.5, self.settings.concurrency),
                    )
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    opener.open(address + "/missing", timeout=2)
                self.assertEqual(missing.exception.code, 404)
                missing.exception.close()

            failing = True
            with self.assertLogs(metrics.logger, level=logging.ERROR) as captured:
                with self.assertRaises(urllib.error.HTTPError) as unavailable:
                    opener.open(address + metrics.METRICS_PATH, timeout=2)
                self.assertEqual(unavailable.exception.code, 503)
                self.assertEqual(unavailable.exception.read(), b"")
                unavailable.exception.close()
            self.assertEqual(
                [record.getMessage() for record in captured.records],
                ["Metrics read failed: ValueError"],
            )
        finally:
            server.shutdown()
            server.server_close()
        for thread in serve_threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
