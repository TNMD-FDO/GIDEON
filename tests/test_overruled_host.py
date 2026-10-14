"""Host list-job deferral, snapshot counts, and bounded failure recovery."""

import json
import subprocess
import unittest
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import patch

from test_agreement_host import JOB, RENDERED, FakeHost

from gideon.host import overruled, report, stack, worker
from gideon.host.render import worker as identity
from gideon.host.sysio import Command, PathLike

WORK_ROOT = Path("/fictional-work")
LABEL = "corpus-2099-01-03"
SNAPSHOT = "treatment-2099-01-01"
CASELAW_SNAPSHOT = "fictional-dump-2099-01-02"
INPUT = {"path": "decisions-overruled.html", "sha256": "a" * 64}


def counts_row() -> dict[str, Any]:
    return {
        "list_rows": 3, "rows": 5, "clusters": 2,
        "overruled": {"cite": 2, "name": 1, "ambiguous": 1, "absent": 1},
        "overruling": {"cite": 3, "docket": 1, "absent": 1},
    }


def failure(reason: str, *, job: int = JOB) -> dict[str, object]:
    return {
        "schema": 1, "job": job, "court": "scotus", "reason": reason,
        "table": None, "error": None, "at": "2099-01-03T12:00:00+00:00",
    }


class CountsHost(FakeHost):
    """Add count replies and explicit I/O refusals to the queue fake."""

    def __init__(self) -> None:
        super().__init__()
        self.counts_output = json.dumps(counts_row())
        self.queue_output: str | None = None
        self.run_error: OSError | subprocess.SubprocessError | None = None
        self.read_error: OSError | UnicodeError | None = None

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if self.run_error is not None:
            self.calls.append((list(argv), input))
            raise self.run_error
        if input is not None and "public.known_overruled" in input:
            self.calls.append((list(argv), input))
            return subprocess.CompletedProcess(list(argv), self.code, self.counts_output, "PRIVATE_SENTINEL")
        if self.queue_output is not None:
            self.calls.append((list(argv), input))
            return subprocess.CompletedProcess(list(argv), self.code, self.queue_output, "PRIVATE_SENTINEL")
        return super().run(argv, check=check, input=input, cwd=cwd, env=env,
                           timeout=timeout, passthrough=passthrough)

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        if self.read_error is not None:
            raise self.read_error
        try:
            return self.files[str(path)]
        except KeyError:
            raise FileNotFoundError(str(path)) from None


class OverruledHost(unittest.TestCase):
    def setUp(self) -> None:
        self.host = CountsHost()
        self.work_dir = WORK_ROOT / LABEL / "treatment"
        self.failure_path = str(self.work_dir / "overruled-failed.json")

    def defer(self, **changes: Any) -> int | report.Problem:
        arguments: dict[str, Any] = {
            "label": LABEL, "snapshot": SNAPSHOT, "caselaw_snapshot": CASELAW_SNAPSHOT,
            "court": "scotus", "input": INPUT,
        }
        arguments.update(changes)
        return overruled.defer_overruled(self.host, RENDERED, **arguments)

    def read_counts(self, **changes: Any) -> overruled.OverruledCounts | report.Problem:
        arguments: dict[str, Any] = {
            "source": "treatment", "snapshot_date": "2099-01-01",
            "caselaw_source": "fictional-dump", "caselaw_snapshot_date": "2099-01-02",
            "court": "scotus",
        }
        arguments.update(changes)
        return overruled.read_overruled_counts(self.host, RENDERED, **arguments)

    def read(self, **changes: Any) -> overruled.OverruledRead | report.Problem:
        arguments: dict[str, Any] = {"label": LABEL, "snapshot": SNAPSHOT, "work_root": WORK_ROOT}
        arguments.update(changes)
        return overruled.read_overruled(self.host, RENDERED, JOB, **arguments)

    def test_defer_binds_arguments_and_label_lock_only_on_stdin(self) -> None:
        self.assertEqual(self.defer(), JOB)
        self.assertEqual(len(self.host.calls), 1)
        argv, sql = self.host.calls[0]
        self.assertEqual(argv, worker.psql_argv(RENDERED))
        assert sql is not None
        for value in (LABEL, SNAPSHOT, CASELAW_SNAPSHOT, "scotus", identity.OVERRULED_TASK,
                      identity.OVERRULED_QUEUE, INPUT["path"], INPUT["sha256"]):
            self.assertNotIn(value, " ".join(argv))
            self.assertIn(value, sql)
        self.assertIn(f"\\set v_lock 'overruled-{LABEL}'", sql)
        args = json.loads(sql.split("\\set v_args '", 1)[1].split("'\n", 1)[0])
        self.assertEqual(args, {"label": LABEL, "snapshot": SNAPSHOT,
                                "caselaw_snapshot": CASELAW_SNAPSHOT, "court": "scotus", "input": INPUT})

    def test_defer_rejects_each_grammar_before_running_a_child(self) -> None:
        changes: dict[str, Any]
        for changes in (
            {"label": "../bad"}, {"label": "corpus-2099-02-30"},
            {"snapshot": "../bad"}, {"snapshot": "treatment-2099-02-30"},
            {"snapshot": "other-2099-01-01"}, {"caselaw_snapshot": "../bad"},
            {"caselaw_snapshot": "fictional-dump-2099-02-30"},
            {"court": "../bad"}, {"court": "Court1"}, {"court": "c" * 33}, {"court": "ca1"},
            {"input": {}}, {"input": {"path": "../bad", "sha256": "a" * 64}},
            {"input": {"path": "bad.partial", "sha256": "a" * 64}},
            {"input": {"path": INPUT["path"], "sha256": "A" * 64}},
            {"input": {**INPUT, "extra": "forbidden"}},
        ):
            with self.subTest(changes=changes):
                result = self.defer(**changes)
                assert isinstance(result, report.Problem)
                self.assertIn(report.command("corpus install"), result.fix)
                self.assertEqual(self.host.calls, [])

    def test_counts_bind_all_five_values_and_query_only_the_record(self) -> None:
        result = self.read_counts()
        assert isinstance(result, overruled.OverruledCounts)
        self.assertEqual(asdict(result), counts_row())
        self.assertEqual(len(self.host.calls), 1)
        argv, sql = self.host.calls[0]
        self.assertEqual(argv, worker.psql_argv(RENDERED))
        assert sql is not None
        for name, value in (
            ("v_source", "treatment"), ("v_snapshot_date", "2099-01-01"),
            ("v_caselaw_source", "fictional-dump"), ("v_caselaw_snapshot_date", "2099-01-02"),
            ("v_court", "scotus"),
        ):
            self.assertIn(worker.bind(name, value), sql)
            self.assertIn(f":'{name}'", overruled.OVERRULED_COUNTS_SQL)
            self.assertNotIn(value, " ".join(argv))
        self.assertTrue(sql.endswith(overruled.OVERRULED_COUNTS_SQL))
        self.assertIn("count(DISTINCT list_order)", sql)
        self.assertIn("count(DISTINCT overruled_cluster)", sql)
        self.assertIn("FROM public.known_overruled", sql)
        self.assertNotIn("JOIN", sql)
        for column, variable in (
            ("source", "v_source"), ("source_snapshot", "v_snapshot_date"),
            ("caselaw_source", "v_caselaw_source"), ("caselaw_snapshot", "v_caselaw_snapshot_date"),
        ):
            self.assertIn(f"{column} = :'{variable}'", sql)

    def test_counts_follow_a_restored_record_and_accept_zero_or_sparse_maps(self) -> None:
        self.host.files[str(self.work_dir / "counts.json")] = json.dumps(counts_row())
        empty = {"list_rows": 0, "rows": 0, "clusters": 0, "overruled": {}, "overruling": {}}
        self.host.counts_output = json.dumps(empty)
        result = self.read_counts()
        assert isinstance(result, overruled.OverruledCounts)
        self.assertEqual(asdict(result), empty)
        sparse = {"list_rows": 1, "rows": 2, "clusters": 0,
                  "overruled": {"absent": 2}, "overruling": {"ambiguous": 2}}
        self.host.counts_output = json.dumps(sparse)
        result = self.read_counts()
        assert isinstance(result, overruled.OverruledCounts)
        self.assertEqual(asdict(result), sparse)

    def test_counts_reject_bad_keys_values_and_arithmetic(self) -> None:
        cases: list[object] = [None, [], "not an object", {**counts_row(), "extra": 1},
                               {key: value for key, value in counts_row().items() if key != "rows"}]
        for field in ("list_rows", "rows", "clusters"):
            cases.extend({**counts_row(), field: bad} for bad in (-1, True, "1", 1.5, None))
        for field, forbidden in (("overruled", "docket"), ("overruling", "name")):
            bad: object
            for bad in ({"invented": 5}, {forbidden: 5}, {"cite": -1}, {"cite": True},
                        {"cite": "5"}, {"cite": 4}, [], None):
                cases.append({**counts_row(), field: bad})
        cases.extend(({**counts_row(), "list_rows": 0}, {**counts_row(), "list_rows": 6},
                      {**counts_row(), "clusters": 4}, {**counts_row(), "clusters": 0}))
        for value in cases:
            with self.subTest(value=value):
                self.host.counts_output = json.dumps(value)
                result = self.read_counts()
                assert isinstance(result, report.Problem)
                self.assertIn("row is invalid", result.problem)
                self.assertIn("logs", result.fix)
        for value in ("not json", json.dumps(counts_row()) + "\n{}"):
            self.host.counts_output = value
            self.assertIsInstance(self.read_counts(), report.Problem)

    def test_counts_reject_invalid_arguments_before_psql(self) -> None:
        for changes in (
            {"source": "../bad"}, {"source": "other"}, {"caselaw_source": "../bad"},
            {"snapshot_date": "2099-02-30"}, {"snapshot_date": "2099-1-01"},
            {"snapshot_date": "extra-2099-01-01"}, {"snapshot_date": None},
            {"caselaw_snapshot_date": "2099-02-30"}, {"caselaw_snapshot_date": "bad"},
            {"court": "../bad"}, {"court": "ca1"},
        ):
            with self.subTest(changes=changes):
                result = self.read_counts(**changes)
                assert isinstance(result, report.Problem)
                self.assertIn(report.command("corpus install"), result.fix)
                self.assertEqual(self.host.calls, [])

    def test_child_return_codes_take_priority_over_parseable_stdout(self) -> None:
        for code in (1, 3, 127):
            self.host.code = code
            for operation in (self.defer, self.read_counts, self.read):
                with self.subTest(code=code, operation=operation.__name__):
                    before = len(self.host.calls)
                    result = operation()
                    assert isinstance(result, report.Problem)
                    self.assertIn(f"exit {code}", result.problem)
                    self.assertIn("logs", result.fix)
                    self.assertIn(report.command("corpus install"), result.fix)
                    self.assertEqual(len(self.host.calls), before + 1)
                    self.assertNotIn("PRIVATE_SENTINEL", result.problem)

    def test_unavailable_child_is_a_refusal_without_retry(self) -> None:
        for error in (FileNotFoundError("PRIVATE_SENTINEL"), subprocess.TimeoutExpired("private", 1)):
            self.host.run_error = error
            for operation in (self.defer, self.read_counts, self.read):
                with self.subTest(operation=operation.__name__, error=type(error).__name__):
                    before = len(self.host.calls)
                    result = operation()
                    assert isinstance(result, report.Problem)
                    self.assertIn("logs", result.fix)
                    self.assertIn(report.command("corpus install"), result.fix)
                    self.assertNotIn("PRIVATE_SENTINEL", result.problem)
                    self.assertEqual(len(self.host.calls), before + 1)

    def test_queue_waits_and_completes_without_reading_completion_files(self) -> None:
        self.host.read_error = OSError("must not read derived files")
        for status in ("todo", "doing", "succeeded"):
            self.host.status = status
            result = self.read()
            assert isinstance(result, overruled.OverruledRead)
            self.assertEqual(result.row, worker.JobRow(JOB, status, 1))
            self.assertEqual(result.done, status == "succeeded")
            self.assertIsNone(result.failure)
            self.assertIsNone(result.reason)

    def test_failed_terminal_without_this_jobs_failure_is_local(self) -> None:
        for status in ("failed", "aborted", "cancelled"):
            for value in (None, failure("invalid", job=JOB + 1)):
                with self.subTest(status=status, value=value):
                    self.host.status = status
                    self.host.files.clear()
                    if value is not None:
                        self.host.files[self.failure_path] = json.dumps(value)
                    result = self.read()
                    assert isinstance(result, overruled.OverruledRead)
                    self.assertFalse(result.done)
                    self.assertEqual(result.reason, "local")
                    assert result.failure is not None
                    self.assertIn("logs", result.failure.fix)

    def test_every_failure_reason_has_its_recovery_fix_in_both_command_forms(self) -> None:
        expected = {
            "invalid": ("logs",), "database": ("logs",), "local": ("logs",), "busy": ("Wait",),
            "missing-input": (SNAPSHOT, INPUT["path"], ".fetch.json", "Remove"),
            "input-mismatch": (SNAPSHOT, INPUT["path"], ".fetch.json", "Remove"),
            "missing-stage": (str(WORK_ROOT / LABEL / "caselaw"), "Remove"),
            "stage-mismatch": (str(WORK_ROOT / LABEL / "caselaw"), "Remove"),
            "malformed": ("docs/runbooks/release-files.md §10", "new capture"),
        }
        self.assertEqual(set(expected), identity.OVERRULED_FAILURE_REASONS)
        self.host.status = "failed"
        for installed in (False, True):
            with patch.object(report, "_installed_form", installed):
                for reason, parts in expected.items():
                    with self.subTest(reason=reason, installed=installed):
                        self.host.files[self.failure_path] = json.dumps(failure(reason))
                        result = self.read(command_path=f"corpus install {LABEL}")
                        assert isinstance(result, overruled.OverruledRead)
                        self.assertFalse(result.done)
                        self.assertEqual(result.reason, reason)
                        assert result.failure is not None
                        for part in parts:
                            self.assertIn(part, result.failure.fix)
                        self.assertIn(report.command(f"corpus install {LABEL}"), result.failure.fix)
                        if "logs" in parts:
                            self.assertIn(stack.logs_fix(RENDERED, identity.WORKER_SERVICE_NAME), result.failure.fix)
                self.host.files[self.failure_path] = json.dumps({**failure("malformed"), "table": "opinion-clusters"})
                result = self.read()
                assert isinstance(result, overruled.OverruledRead)
                assert result.failure is not None
                self.assertIn("opinion-clusters", result.failure.problem)
                self.assertIn(str(WORK_ROOT / LABEL / "caselaw"), result.failure.fix)
                self.assertNotIn("new capture", result.failure.fix)

    def test_failure_file_rejects_untrusted_fields(self) -> None:
        self.host.status = "failed"
        for key, bad in (
            ("schema", True), ("schema", 2), ("court", "ca1"), ("job", float(JOB)),
            ("reason", "invented"), ("table", "PRIVATE_SENTINEL"), ("error", "PRIVATE SENTINEL"),
            ("at", "2099-01-03"), ("at", True),
        ):
            with self.subTest(key=key, bad=bad):
                self.host.files[self.failure_path] = json.dumps({**failure("invalid"), key: bad})
                result = self.read()
                assert isinstance(result, report.Problem)
                self.assertIn("invalid", result.problem)
                self.assertNotIn("PRIVATE", result.problem)
        value: object
        for value in ([], "PRIVATE_SENTINEL", {**failure("invalid"), "extra": 1},
                      {key: value for key, value in failure("invalid").items() if key != "error"}):
            self.host.files[self.failure_path] = json.dumps(value)
            self.assertIsInstance(self.read(), report.Problem)
        self.host.files[self.failure_path] = "not json"
        self.assertIsInstance(self.read(), report.Problem)
        self.host.read_error = OSError("PRIVATE_SENTINEL")
        self.host.files[self.failure_path] = json.dumps(failure("local"))
        self.assertIsInstance(self.read(), report.Problem)

    def test_read_rejects_grammars_and_unknown_or_invalid_queue_rows(self) -> None:
        for changes in ({"label": "../bad"}, {"snapshot": "../bad"}, {"snapshot": "other-2099-01-01"}):
            self.assertIsInstance(self.read(**changes), report.Problem)
            self.assertEqual(self.host.calls, [])
        self.host.status = "unknown"
        result = self.read()
        assert isinstance(result, report.Problem)
        self.assertIn("logs", result.fix)
        for output in ("not a job", f"{JOB + 1}|succeeded|1"):
            self.host.queue_output = output
            self.assertIsInstance(self.read(), report.Problem)
