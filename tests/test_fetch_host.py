"""The host corpus-fetch facade over an in-memory Host seam."""

import hashlib
import json
import os
import stat
import subprocess
import unittest
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import cast

from gideon.host import backupset, fetch, report, stack
from gideon.host.render import worker
from gideon.host.sysio import Command, PathLike, RealHost
from gideon.worker.fetch import FetchFailure, validate_destination, validate_url

RENDERED = "/rendered"
SNAPSHOTS = "/snapshots"
DESTINATION = "fictions-2026-09-30/bulk-data/whole.json"
URL = "https://archive.example.test/bulk-data/whole.json?private-query"
JOB_ID = 31


class FakeHost(RealHost):
    def __init__(self, *, status: str = "succeeded", code: int = 0) -> None:
        super().__init__()
        self.status = status
        self.code = code
        self.calls: list[tuple[list[str], str | None]] = []
        self.files: dict[str, str] = {}
        self.sizes: dict[str, int] = {}

    def run(
        self,
        argv: Command,
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, cwd, env, timeout, passthrough
        self.calls.append((list(argv), input))
        if input is None:
            raise AssertionError("psql must read its statement on stdin")
        output = (
            f"{JOB_ID}\n" if "procrastinate_defer_jobs_v1" in input
            else f"{JOB_ID}|{self.status}|1\n"
        )
        return subprocess.CompletedProcess(list(argv), self.code, output, "private diagnostic")

    def exists(self, path: PathLike) -> bool:
        return str(path) in self.files or str(path) in self.sizes

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        return self.files[str(path)]

    def stat(self, path: PathLike) -> os.stat_result:
        size = self.sizes[str(path)]
        return os.stat_result((stat.S_IFREG | 0o440, 0, 0, 1, 0, 0, size, 0, 0, 0))


def record_for(body: bytes = b"Fictitious corpus bytes") -> dict[str, object]:
    return {
        "schema": 1, "state": "whole", "form": worker.KEPT_FORM,
        "host": "archive.example.test", "path": "/bulk-data/whole.json",
        "etag": '"fictitious-etag"', "last_modified": None,
        "total": len(body), "durable": len(body), "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "fetched_at": "2026-10-06T12:00:00+00:00", "job": JOB_ID,
        "seconds": 3.5, "resumes": 0,
    }


def failure_for(reason: str, *, job: int = JOB_ID) -> dict[str, object]:
    return {
        "schema": 1, "job": job, "reason": reason,
        "host": "archive.example.test", "status": 403,
        "error": None, "at": "2026-10-06T12:00:00+00:00",
    }


class FetchHost(unittest.TestCase):
    def test_backup_inventory_roots_do_not_include_snapshots(self) -> None:
        snapshots = worker.SNAPSHOTS_ROOT
        for root in backupset.inventory_roots("/opt/gideon"):
            source = Path(root.source)
            with self.subTest(root=root.name):
                self.assertFalse(
                    source == snapshots
                    or source in snapshots.parents
                    or snapshots in source.parents,
                    "Fix: keep snapshots outside every backup inventory root.",
                )

    def test_host_and_worker_grammars_agree(self) -> None:
        destinations = (
            (DESTINATION, worker.KEPT_FORM, True),
            ("resolve/index.json", worker.FRESH_FORM, True),
            ("fictions-2026-09-30/" + "/".join(["part"] * 7), worker.KEPT_FORM, True),
            ("../outside/file", worker.KEPT_FORM, False),
            ("resolve/index.json", worker.KEPT_FORM, False),
            (DESTINATION, worker.FRESH_FORM, False),
            ("fictions-2026-13-30/file", worker.KEPT_FORM, False),
            ("fictions-2026-09-30/file.partial", worker.KEPT_FORM, False),
            ("fictions-2026-09-30/" + "/".join(["part"] * 8), worker.KEPT_FORM, False),
        )
        for destination, form, expected in destinations:
            with self.subTest(destination=destination, form=form):
                host_accepts = fetch._destination_problem(destination, form) is None
                try:
                    validate_destination(destination, form)
                except FetchFailure:
                    worker_accepts = False
                else:
                    worker_accepts = True
                self.assertEqual((host_accepts, worker_accepts), (expected, expected))
        addresses = (
            (URL, True),
            ("https://archive.example.test:443/path?query=yes", True),
            ("http://archive.example.test/path", False),
            ("https://UPPER.example.test/path", False),
            ("https://user@archive.example.test/path", False),
            ("https://archive.example.test:444/path", False),
            ("https://archive.example.test/path#fragment", False),
            ("https://archive.example.test/path with spaces", False),
            ("https://archive.example.test/" + "x" * worker.URL_MAX_LENGTH, False),
        )
        for url, expected in addresses:
            with self.subTest(url=url):
                host_accepts = fetch._url_problem(url) is None
                try:
                    validate_url(url)
                except FetchFailure:
                    worker_accepts = False
                else:
                    worker_accepts = True
                self.assertEqual((host_accepts, worker_accepts), (expected, expected))

    def test_destination_helpers_and_grammar_refuse_before_any_child(self) -> None:
        self.assertEqual(
            fetch.snapshot_destination("fictions", "2026-09-30", "bulk-data/whole.json"),
            DESTINATION,
        )
        self.assertEqual(fetch.resolve_destination("index.json"), "resolve/index.json")
        for destination, form, url in (
            ("../elsewhere/file", worker.KEPT_FORM, URL),
            ("resolve/index.json", worker.KEPT_FORM, URL),
            ("fictions-2026-09-30/file", worker.FRESH_FORM, URL),
            ("fictions-2026-13-30/file", worker.KEPT_FORM, URL),
            ("fictions-2026-09-30/file.fetch.json", worker.KEPT_FORM, URL),
            (DESTINATION, "unknown", URL),
            (DESTINATION, worker.KEPT_FORM, "http://archive.example.test/file"),
            (DESTINATION, worker.KEPT_FORM, "https://User@archive.example.test/file"),
            (DESTINATION, worker.KEPT_FORM, "https://archive.example.test:444/file"),
            (DESTINATION, worker.KEPT_FORM, "https://archive.example.test/file#fragment"),
            (DESTINATION, worker.KEPT_FORM, "https://UPPER.example.test/file"),
            (DESTINATION, worker.KEPT_FORM, "https://archive.example.test/" + "x" * worker.URL_MAX_LENGTH),
        ):
            with self.subTest(destination=destination, url=url):
                host = FakeHost()
                result = fetch.defer_fetch(
                    host, RENDERED, destination=destination, url=url, form=form
                )
                self.assertIsInstance(result, report.Problem)
                assert isinstance(result, report.Problem)
                self.assertTrue(result.fix)
                self.assertEqual(host.calls, [])

    def test_defer_binds_all_values_and_assigns_stable_lanes(self) -> None:
        host = FakeHost()
        first = fetch.defer_fetch(
            host, RENDERED, destination=DESTINATION, url=URL, form=worker.KEPT_FORM
        )
        second = fetch.defer_fetch(
            host, RENDERED, destination=DESTINATION, url=URL, form=worker.KEPT_FORM
        )
        self.assertEqual((first, second), (JOB_ID, JOB_ID))
        argv, sql = host.calls[0]
        assert sql is not None
        expected_argv = stack.exec_argv(
            RENDERED, "postgres", "psql", "-U", worker.WORKER_ROLE,
            "-d", worker.WORKER_DATABASE_NAME, "-tA", "-F", "|",
            "-v", "ON_ERROR_STOP=1", "-f", "-",
        )
        self.assertEqual(argv, expected_argv)
        for value in (DESTINATION, URL, worker.FETCH_TASK, worker.FETCH_QUEUE):
            self.assertNotIn(value, " ".join(argv))
            self.assertIn(value, sql)
        lane = int.from_bytes(hashlib.sha256(DESTINATION.encode()).digest(), "big") % worker.LANE_COUNT
        self.assertIn(f"\\set v_lock 'fetch-lane-{lane}'", sql)
        self.assertLess(lane, worker.LANE_COUNT)
        self.assertEqual(host.calls[0][1], host.calls[1][1])
        self.assertIn(
            "ROW(:'v_queue', :'v_task', 0, :'v_lock', NULL, :'v_args'::jsonb, NULL)",
            sql,
        )
        self.assertEqual(
            fetch.defer_fetch(
                host, RENDERED, destination="resolve/index.json", url=URL,
                form=worker.FRESH_FORM,
            ),
            JOB_ID,
        )
        fresh_sql = host.calls[-1][1]
        assert fresh_sql is not None
        self.assertIn("\\set v_lock 'resolve/index.json'", fresh_sql)
        self.assertNotIn("private diagnostic", sql)

    def test_defer_refuses_a_failed_psql_by_return_code(self) -> None:
        host = FakeHost(code=127)
        result = fetch.defer_fetch(
            host, RENDERED, destination=DESTINATION, url=URL, form=worker.KEPT_FORM
        )
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn("could not enqueue", result.problem)
        self.assertIn("logs", result.fix)

    def test_read_fetch_waits_then_reads_every_whole_record_field(self) -> None:
        host = FakeHost(status="doing")
        waiting = fetch.read_fetch(
            host, RENDERED, JOB_ID, destination=DESTINATION, snapshots_root=SNAPSHOTS
        )
        self.assertIsInstance(waiting, fetch.FetchRead)
        assert isinstance(waiting, fetch.FetchRead)
        self.assertEqual((waiting.job.status, waiting.record, waiting.failure, waiting.reason),
                         ("doing", None, None, None))
        host.status = "succeeded"
        expected = record_for()
        record_path = f"{SNAPSHOTS}/{DESTINATION}{worker.RECORD_SUFFIX}"
        target = f"{SNAPSHOTS}/{DESTINATION}"
        host.files[record_path] = json.dumps(expected)
        host.sizes[target] = cast(int, expected["size"])
        outcome = fetch.read_fetch(
            host, RENDERED, JOB_ID, destination=DESTINATION, snapshots_root=SNAPSHOTS
        )
        self.assertIsInstance(outcome, fetch.FetchRead)
        assert isinstance(outcome, fetch.FetchRead)
        self.assertIsNone(outcome.failure)
        self.assertIsNone(outcome.reason)
        self.assertIsNotNone(outcome.record)
        assert outcome.record is not None
        self.assertEqual(asdict(outcome.record), expected)
        self.assertEqual(
            fetch.read_record(host, DESTINATION, snapshots_root=SNAPSHOTS), outcome.record
        )
        host.sizes.pop(target)
        self.assertIsNone(fetch.read_record(host, DESTINATION, snapshots_root=SNAPSHOTS))
        self.assertIsInstance(
            fetch.read_fetch(host, RENDERED, JOB_ID, destination=DESTINATION,
                             snapshots_root=SNAPSHOTS),
            report.Problem,
        )

    def test_failed_job_uses_its_own_failure_file_and_both_command_forms(self) -> None:
        host = FakeHost(status="failed")
        path = f"{SNAPSHOTS}/{DESTINATION}{worker.FAILURE_SUFFIX}"
        host.files[path] = json.dumps(failure_for("refused-host"))
        try:
            for installed in (False, True):
                with self.subTest(installed=installed):
                    report.set_installed_form(installed)
                    outcome = fetch.read_fetch(
                        host, RENDERED, JOB_ID, destination=DESTINATION,
                        snapshots_root=SNAPSHOTS,
                    )
                    self.assertIsInstance(outcome, fetch.FetchRead)
                    assert isinstance(outcome, fetch.FetchRead)
                    self.assertIsNone(outcome.record)
                    self.assertIsNotNone(outcome.failure)
                    assert outcome.failure is not None
                    self.assertEqual(outcome.reason, "refused-host")
                    self.assertIn("archive.example.test", outcome.failure.problem)
                    self.assertIn("corpus", outcome.failure.problem)
                    self.assertIn("config/egress.yaml", outcome.failure.fix)
                    self.assertIn(report.command("apply"), outcome.failure.fix)
        finally:
            report.set_installed_form(False)
        host.files[path] = json.dumps(failure_for("changed"))
        changed = fetch.read_fetch(
            host, RENDERED, JOB_ID, destination=DESTINATION, snapshots_root=SNAPSHOTS
        )
        assert isinstance(changed, fetch.FetchRead) and changed.failure is not None
        self.assertEqual(changed.reason, "changed")
        self.assertIn("changed", changed.failure.problem)
        self.assertIn("Fetch this destination again", changed.failure.fix)
        host.files[path] = json.dumps(failure_for("upstream-status"))
        upstream = fetch.read_fetch(
            host, RENDERED, JOB_ID, destination=DESTINATION, snapshots_root=SNAPSHOTS
        )
        assert isinstance(upstream, fetch.FetchRead) and upstream.failure is not None
        self.assertEqual(upstream.reason, "upstream-status")
        self.assertIn("status 403", upstream.failure.problem)
        self.assertIn("logs", upstream.failure.fix)
        for reason in worker.FAILURE_REASONS - {
            "refused-host", "changed", "upstream-status",
        }:
            with self.subTest(reason=reason):
                host.files[path] = json.dumps(failure_for(reason))
                outcome = fetch.read_fetch(
                    host, RENDERED, JOB_ID, destination=DESTINATION,
                    snapshots_root=SNAPSHOTS,
                )
                assert isinstance(outcome, fetch.FetchRead) and outcome.failure is not None
                self.assertEqual(outcome.reason, reason)
                self.assertIn(reason, outcome.failure.problem)
                self.assertIn("logs", outcome.failure.fix)
        host.files[path] = json.dumps({"job": JOB_ID + 1, "stale": "different job"})
        stale = fetch.read_fetch(
            host, RENDERED, JOB_ID, destination=DESTINATION, snapshots_root=SNAPSHOTS
        )
        assert isinstance(stale, fetch.FetchRead) and stale.failure is not None
        self.assertEqual(stale.reason, "local")
        self.assertNotIn("outside the corpus", stale.failure.problem)
        self.assertIn("logs", stale.failure.fix)
        del host.files[path]
        missing = fetch.read_fetch(
            host, RENDERED, JOB_ID, destination=DESTINATION, snapshots_root=SNAPSHOTS
        )
        assert isinstance(missing, fetch.FetchRead) and missing.failure is not None
        self.assertEqual(missing.reason, "local")

    def test_invalid_record_and_failure_file_are_problems(self) -> None:
        host = FakeHost(status="succeeded")
        host.files[f"{SNAPSHOTS}/{DESTINATION}{worker.RECORD_SUFFIX}"] = "not json"
        self.assertIsInstance(
            fetch.read_record(host, DESTINATION, snapshots_root=SNAPSHOTS),
            report.Problem,
        )
        host.status = "failed"
        host.files[f"{SNAPSHOTS}/{DESTINATION}{worker.FAILURE_SUFFIX}"] = "not json"
        self.assertIsInstance(
            fetch.read_fetch(host, RENDERED, JOB_ID, destination=DESTINATION,
                             snapshots_root=SNAPSHOTS),
            report.Problem,
        )


if __name__ == "__main__":
    unittest.main()
