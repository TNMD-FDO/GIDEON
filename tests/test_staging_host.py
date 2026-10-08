"""The host corpus-stage facade over an in-memory Host seam."""

import copy
import hashlib
import json
import subprocess
import unittest
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from gideon.host import backupset, report, stack, staging, worker
from gideon.host.render import worker as identity
from gideon.host.sysio import Command, PathLike, RealHost

RENDERED = "/rendered"
WORK_ROOT = Path("/fictitious-work")
LABEL = "corpus-2099-01-03"
SOURCE = "fictions"
SNAPSHOT = "fictions-2099-01-02"
JOB_ID = 31


class FakeHost(RealHost):
    """Serve queue rows and stage files only from in-memory values."""

    def __init__(self, *, status: str = "succeeded", code: int = 0) -> None:
        super().__init__()
        self.status = status
        self.code = code
        self.calls: list[tuple[list[str], str | None]] = []
        self.files: dict[str, str] = {}

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
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
        return str(path) in self.files

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        try:
            return self.files[str(path)]
        except KeyError as exc:
            raise FileNotFoundError(str(path)) from exc


def inputs_for() -> dict[str, dict[str, str]]:
    return {
        table: {
            "path": f"{table}-2099-01-02.csv.bz2",
            "sha256": hashlib.sha256(f"fictitious {table} bytes".encode()).hexdigest(),
        }
        for table in identity.STAGE_TABLES
    }


def record_for() -> dict[str, Any]:
    return {
        "schema": 1, "label": LABEL, "source": SOURCE, "snapshot": SNAPSHOT,
        "courts": ["court1", "court2"],
        "inputs": {table: {**item, "size": len(f"fictitious {table} bytes")}
                   for table, item in inputs_for().items()},
        "records": dict.fromkeys(identity.STAGE_TABLES, 2),
        "counts": {
            "court1": dict.fromkeys(identity.STAGE_TABLES[1:], 1),
            "court2": dict.fromkeys(identity.STAGE_TABLES[1:], 0),
        },
        "job": JOB_ID, "seconds": 3.5,
        "staged_at": "2099-01-03T12:00:00+00:00",
    }


def failure_for(
    reason: str, *, job: int = JOB_ID, table: str | None = None,
    court: str | None = None, error: str | None = None,
) -> dict[str, object]:
    return {
        "schema": 1, "job": job, "reason": reason, "table": table,
        "court": court, "error": error, "at": "2099-01-03T12:00:00+00:00",
    }


class StagingHost(unittest.TestCase):
    """The host validates queue arguments and reads durable stage outcomes."""

    def setUp(self) -> None:
        self.host = FakeHost()
        self.record_path = str(WORK_ROOT / LABEL / SOURCE / identity.STAGE_RECORD_NAME)
        self.failure_path = str(WORK_ROOT / LABEL / f"{SOURCE}.{identity.STAGE_FAILURE_NAME}")

    def read_record(self) -> staging.StageRecord | None | report.Problem:
        return staging.read_record(self.host, LABEL, SOURCE, work_root=WORK_ROOT)

    def read_stage(self, *, command_path: str = staging.COMMAND_PATH
                   ) -> staging.StageRead | report.Problem:
        return staging.read_stage(
            self.host, RENDERED, JOB_ID, label=LABEL, snapshot=SNAPSHOT,
            work_root=WORK_ROOT, command_path=command_path,
        )

    def test_work_directory_is_outside_every_backup_inventory_root(self) -> None:
        """The derived work root never enters a backup set."""

        self.assertEqual(staging.work_directory(LABEL, SOURCE, work_root=WORK_ROOT),
                         WORK_ROOT / LABEL / SOURCE)
        for root in backupset.inventory_roots("/opt/gideon"):
            source = Path(root.source)
            with self.subTest(root=root.name):
                self.assertFalse(
                    source == identity.WORK_ROOT
                    or source in identity.WORK_ROOT.parents
                    or identity.WORK_ROOT in source.parents,
                    "Fix: keep the derived work root outside every backup inventory root.",
                )

    def test_defer_puts_every_value_on_stdin_under_the_label_lock(self) -> None:
        """Only the bounded queue identity and arguments reach stdin-fed psql."""

        inputs = inputs_for()
        result = staging.defer_stage(
            self.host, RENDERED, label=LABEL, snapshot=SNAPSHOT,
            courts=["court1", "court2"], inputs=inputs,
        )
        self.assertEqual(result, JOB_ID)
        self.assertEqual(len(self.host.calls), 1)
        argv, sql = self.host.calls[0]
        self.assertEqual(argv, worker.psql_argv(RENDERED))
        assert sql is not None
        for value in (LABEL, SNAPSHOT, "court1", "court2", *inputs,
                      *(item["path"] for item in inputs.values()),
                      *(item["sha256"] for item in inputs.values()),
                      identity.STAGE_TASK, identity.STAGE_QUEUE):
            self.assertNotIn(value, " ".join(argv))
            self.assertIn(value, sql)
        self.assertIn(f"\\set v_lock 'stage-{LABEL}'", sql)
        self.assertIn("ROW(:'v_queue', :'v_task', 0, :'v_lock', NULL, :'v_args'::jsonb, NULL)", sql)
        self.assertNotIn("private diagnostic", sql)

    def test_each_argument_grammar_refuses_with_a_fix_before_any_child(self) -> None:
        """A malformed stage request never defers a job."""

        good = {"label": LABEL, "snapshot": SNAPSHOT,
                "courts": ["court1", "court2"], "inputs": inputs_for()}
        cases: tuple[tuple[str, object, str], ...] = (
            ("label", "../other", "corpus-YYYY-MM-DD"),
            ("label", "corpus-2099-13-03", "real date"),
            ("snapshot", "../outside", "<source>-YYYY-MM-DD"),
            ("snapshot", "fictions-2099-13-02", "real date"),
            ("courts", [], "non-empty sorted"),
            ("courts", ["court2", "court1"], "non-empty sorted"),
            ("courts", ["court1", "court1"], "distinct"),
            ("courts", ["Court1"], "lowercase court ids"),
            ("inputs", {key: item for key, item in inputs_for().items()
                        if key != "courts"}, "exactly courts"),
            ("inputs", {**inputs_for(), "extra": inputs_for()["courts"]}, "exactly courts"),
            ("inputs", {**inputs_for(), "courts": {
                **inputs_for()["courts"], "extra": "value",
            }}, "one-segment path"),
            ("inputs", {**inputs_for(), "courts": {
                **inputs_for()["courts"], "path": "courts.fetch.json",
            }}, "reserved suffix"),
            ("inputs", {**inputs_for(), "courts": {
                **inputs_for()["courts"], "sha256": "NOT_A_DIGEST",
            }}, "lowercase sha256"),
        )
        for field, value, fix in cases:
            with self.subTest(field=field, value=value):
                host = FakeHost()
                arguments = {**good, field: value}
                result = staging.defer_stage(host, RENDERED, **arguments)  # type: ignore[arg-type]
                self.assertIsInstance(result, report.Problem)
                assert isinstance(result, report.Problem)
                self.assertIn(field, result.problem)
                self.assertIn(fix, result.fix)
                self.assertIn(report.command("corpus install"), result.fix)
                self.assertEqual(host.calls, [])

    def test_defer_refuses_a_failed_psql_by_return_code(self) -> None:
        """The database command's nonzero status is a visible refusal."""

        host = FakeHost(code=127)
        result = staging.defer_stage(
            host, RENDERED, label=LABEL, snapshot=SNAPSHOT,
            courts=["court1", "court2"], inputs=inputs_for(),
        )
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn("could not enqueue", result.problem)
        self.assertIn("logs", result.fix)

    def test_read_record_absent_or_whole(self) -> None:
        """A whole record is validated through host.read_text."""

        self.assertIsNone(self.read_record())
        expected = record_for()
        self.host.files[self.record_path] = json.dumps(expected)
        result = self.read_record()
        self.assertIsInstance(result, staging.StageRecord)
        assert isinstance(result, staging.StageRecord)
        self.assertEqual(asdict(result), expected)

    def test_read_record_refuses_every_field_outside_its_schema(self) -> None:
        """The host rejects malformed field types, names, maps, and times."""

        base = record_for()
        cases: tuple[tuple[str, object], ...] = (
            ("schema", True), ("schema", 2),
            ("label", 7), ("label", "corpus-2099-13-03"),
            ("source", 7), ("source", "../other"), ("source", "other"),
            ("snapshot", 7), ("snapshot", "fictions-2099-13-02"),
            ("snapshot", "other-2099-01-02"),
            ("courts", "court1"),
            ("courts", ["court2", "court1"]),
            ("courts", ["Court1"]),
            ("inputs", []),
            ("inputs", {key: value for key, value in base["inputs"].items()
                        if key != "courts"}),
            ("records", []),
            ("records", {**base["records"], "dockets": -1}),
            ("counts", []),
            ("counts", {"court1": base["counts"]["court1"]}),
            ("job", True), ("job", 0),
            ("seconds", True), ("seconds", -1), ("seconds", float("inf")),
            ("staged_at", 7), ("staged_at", "2099-01-03T12:00:00"),
        )
        for field, bad in cases:
            with self.subTest(field=field, bad=bad):
                value = copy.deepcopy(base)
                value[field] = bad
                self.host.files[self.record_path] = json.dumps(value)
                result = self.read_record()
                self.assertIsInstance(result, report.Problem)
                assert isinstance(result, report.Problem)
                self.assertIn("stage record file is invalid", result.problem)
                self.assertIn(report.command("corpus install"), result.fix)
        nested: tuple[tuple[str, str, str | None, object], ...] = (
            ("inputs", "courts", "extra", "value"),
            ("inputs", "courts", "size", True),
            ("inputs", "courts", "sha256", "not-a-digest"),
            ("inputs", "courts", "path", "../outside"),
            ("records", "courts", None, True),
            ("counts", "court1", "extra", 1),
            ("counts", "court1", "dockets", -1),
        )
        for outer, row, nested_field, bad in nested:
            with self.subTest(outer=outer, field=nested_field):
                value = copy.deepcopy(base)
                mapping = value[outer][row]
                if nested_field is None:
                    value[outer][row] = bad
                else:
                    mapping[nested_field] = bad
                self.host.files[self.record_path] = json.dumps(value)
                self.assertIsInstance(self.read_record(), report.Problem)
        self.host.files[self.record_path] = "not json"
        self.assertIsInstance(self.read_record(), report.Problem)

    def test_read_stage_waits_then_succeeds_or_refuses_missing_record(self) -> None:
        """Queue states become waiting, a whole record, or a missing-record fix."""

        for status in ("todo", "doing"):
            with self.subTest(status=status):
                self.host.status = status
                waiting = self.read_stage()
                self.assertIsInstance(waiting, staging.StageRead)
                assert isinstance(waiting, staging.StageRead)
                self.assertEqual((waiting.job.status, waiting.record,
                                  waiting.failure, waiting.reason),
                                 (status, None, None, None))
        self.host.status = "succeeded"
        missing = self.read_stage()
        self.assertIsInstance(missing, report.Problem)
        assert isinstance(missing, report.Problem)
        self.assertIn("without a whole directory and record", missing.problem)
        self.assertIn(stack.logs_fix(RENDERED, identity.WORKER_SERVICE_NAME), missing.fix)
        expected = record_for()
        self.host.files[self.record_path] = json.dumps(expected)
        outcome = self.read_stage()
        self.assertIsInstance(outcome, staging.StageRead)
        assert isinstance(outcome, staging.StageRead)
        self.assertIsNone(outcome.failure)
        self.assertIsNone(outcome.reason)
        assert outcome.record is not None
        self.assertEqual(asdict(outcome.record), expected)

    def test_each_stage_failure_has_its_own_reason_and_fix(self) -> None:
        """Terminal job failures use their closed reason vocabulary and fixes."""

        self.host.status = "failed"
        cases = (
            ("unknown-court", "courts", "absent1", None,
             ("absent1", "courts.yaml", report.command("corpus cut"))),
            ("missing-input", "dockets", None, None,
             ("dockets", str(identity.SNAPSHOTS_ROOT / SNAPSHOT), ".fetch.json")),
            ("input-mismatch", "opinions", None, None,
             ("opinions", str(identity.SNAPSHOTS_ROOT / SNAPSHOT), ".fetch.json")),
            ("malformed", "citations", None, "UnicodeDecodeError",
             ("citations", stack.logs_fix(RENDERED, identity.WORKER_SERVICE_NAME))),
            ("local", None, None, "PermissionError",
             (report.command("host provision"), report.command("apply"))),
            ("busy", None, None, None, ("Wait for the running stage",)),
            ("invalid", None, None, None,
             (stack.logs_fix(RENDERED, identity.WORKER_SERVICE_NAME),)),
        )
        for reason, table, court, error, fragments in cases:
            with self.subTest(reason=reason):
                self.host.files[self.failure_path] = json.dumps(failure_for(
                    reason, table=table, court=court, error=error,
                ))
                outcome = self.read_stage()
                self.assertIsInstance(outcome, staging.StageRead)
                assert isinstance(outcome, staging.StageRead)
                self.assertEqual(outcome.reason, reason)
                self.assertIsNone(outcome.record)
                assert outcome.failure is not None
                for fragment in fragments:
                    self.assertIn(fragment, outcome.failure.problem + outcome.failure.fix)
                self.assertIn(report.command("corpus install"), outcome.failure.fix)

    def test_stale_or_absent_failure_file_is_local_for_each_terminal_status(self) -> None:
        """Another job's file and a missing file never claim this job's reason."""

        for status in ("failed", "aborted", "cancelled"):
            for stale in (False, True):
                with self.subTest(status=status, stale=stale):
                    self.host.status = status
                    self.host.files.pop(self.failure_path, None)
                    if stale:
                        self.host.files[self.failure_path] = json.dumps(failure_for(
                            "malformed", job=JOB_ID + 1, table="dockets",
                        ))
                    outcome = self.read_stage()
                    self.assertIsInstance(outcome, staging.StageRead)
                    assert isinstance(outcome, staging.StageRead)
                    self.assertEqual(outcome.reason, "local")
                    assert outcome.failure is not None
                    self.assertIn(report.command("host provision"), outcome.failure.fix)

    def test_invalid_failure_file_is_a_problem(self) -> None:
        """A court id appears only for unknown-court and matches its grammar."""

        self.host.status = "failed"
        cases = (
            failure_for("unknown-court", table="courts", court="Court1"),
            failure_for("malformed", table="dockets", court="court1"),
            failure_for("unknown-court", table="courts"),
        )
        for value in cases:
            with self.subTest(value=value):
                self.host.files[self.failure_path] = json.dumps(value)
                outcome = self.read_stage()
                self.assertIsInstance(outcome, report.Problem)
                assert isinstance(outcome, report.Problem)
                self.assertIn("failure file is invalid", outcome.problem)
                self.assertIn(stack.logs_fix(RENDERED, identity.WORKER_SERVICE_NAME), outcome.fix)


if __name__ == "__main__":
    unittest.main()
