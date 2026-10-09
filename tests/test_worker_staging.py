"""Corpus staging over a fictitious five-table dump in temporary roots."""

import bz2
import fcntl
import hashlib
import io
import json
import logging
import os
import stat
import tempfile
import threading
import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from gideon.worker import fetch, staging


class WorkerStaging(unittest.TestCase):
    """The stage job preserves selected dump records and files its refusals."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.snapshots_root = root / "snapshots"
        self.work_root = root / "work"
        self.snapshots_root.mkdir()
        self.work_root.mkdir()
        self.label = "corpus-2099-01-01"
        self.snapshot = "fictional-dump-2099-01-01"
        self.source = "fictional-dump"
        self.courts = ["court1", "court2"]
        self.job = 41
        self.instant = datetime(2099, 1, 1, 12, 34, tzinfo=UTC)
        self.sentinel = b"PRIVATE_STAGE_SENTINEL"
        self.dump = self.snapshots_root / self.snapshot
        self.dump.mkdir()
        self.whole = self.work_root / self.label / self.source
        self.failure_path = self.whole.with_name(self.source + "." + staging.STAGE_FAILURE_NAME)
        self.partial = self.whole.with_name(self.source + fetch.PARTIAL_SUFFIX)
        self.lock = self.whole.with_name(self.source + staging.LOCK_SUFFIX)
        self.lines: dict[str, tuple[bytes, list[bytes]]] = {
            "courts": (
                b"id,note\r\n",
                [
                    b"court1,PRIVATE_STAGE_SENTINEL first\r\n",
                    b"court2,PRIVATE_STAGE_SENTINEL empty\n",
                    b"court3,PRIVATE_STAGE_SENTINEL outside\r\n",
                ],
            ),
            "dockets": (
                b"id,court_id,note,nullable,empty\r\n",
                [
                    b'd1,court1,"PRIVATE_STAGE_SENTINEL first\nsecond\r\nlast \\"quoted\\" and \\\\ slash",NULL,\r\n',
                    b"d2,court3,PRIVATE_STAGE_SENTINEL outside,NULL,\n",
                    b"d3,court1,PRIVATE_STAGE_SENTINEL third,NULL,\r\n",
                ],
            ),
            "opinion-clusters": (
                b"id,docket_id,note\n",
                [
                    b"cl1,d1,PRIVATE_STAGE_SENTINEL selected\r\n",
                    b"cl2,d2,PRIVATE_STAGE_SENTINEL outside\n",
                    b"cl3,d3,PRIVATE_STAGE_SENTINEL selected\n",
                ],
            ),
            "citations": (
                b"cluster_id,note\r\n",
                [
                    b"cl1,PRIVATE_STAGE_SENTINEL selected\r\n",
                    b"cl2,PRIVATE_STAGE_SENTINEL outside\n",
                    b"cl3,PRIVATE_STAGE_SENTINEL selected\r\n",
                ],
            ),
            "opinions": (
                b"cluster_id,note\n",
                [
                    b"cl1,PRIVATE_STAGE_SENTINEL selected\n",
                    b"cl2,PRIVATE_STAGE_SENTINEL outside\r\n",
                    b"cl3,PRIVATE_STAGE_SENTINEL selected\n",
                ],
            ),
        }
        self.inputs: dict[str, dict[str, str]] = {}
        for table, (header, rows) in self.lines.items():
            self.replace_table(table, header + b"".join(rows))

    def replace_table(self, table: str, body: bytes) -> None:
        path = self.dump / f"{table}.csv.bz2"
        content = bz2.compress(body)
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        self.inputs[table] = {"path": path.name, "sha256": digest}
        fetch.write_record(path.with_name(path.name + fetch.RECORD_SUFFIX), fetch.FetchRecord(
            "whole", fetch.KEPT_FORM, "archive.example.test", f"/{path.name}",
            None, None, len(content), len(content), len(content), digest,
            self.instant.isoformat(), 3, 0.0, 0,
        ))

    def run_stage(
        self, *, label: str | None = None, snapshot: str | None = None,
        courts: list[str] | None = None,
        inputs: dict[str, dict[str, str]] | None = None,
        work_root: Path | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> staging.StageRecord:
        return staging.stage(
            self.snapshots_root, work_root or self.work_root,
            self.label if label is None else label,
            self.snapshot if snapshot is None else snapshot,
            self.courts if courts is None else courts,
            self.inputs if inputs is None else inputs,
            self.job, clock=lambda: self.instant,
            monotonic=(lambda: 10.0) if monotonic is None else monotonic,
        )

    def assert_failure(self, failure: staging.StageFailure) -> dict[str, object]:
        staging.write_job_failure(
            self.work_root, self.label, self.snapshot, self.job, failure,
            clock=lambda: self.instant,
        )
        value = json.loads(self.failure_path.read_text())
        self.assertEqual(value, {
            "schema": 1, "job": self.job, "reason": failure.reason,
            "table": failure.table, "court": failure.court,
            "error": failure.error, "at": self.instant.isoformat(),
        })
        self.assertEqual(stat.S_IMODE(self.failure_path.stat().st_mode), fetch.PARTIAL_MODE)
        return value

    def test_selected_files_keep_input_bytes_under_each_header(self) -> None:
        """The five-table stage copies selected CSV record bytes unchanged."""

        self.run_stage()
        selected = {
            "dockets": (0, 2), "opinion-clusters": (0, 2),
            "citations": (0, 2), "opinions": (0, 2),
        }
        for table, indexes in selected.items():
            with self.subTest(table=table):
                header, rows = self.lines[table]
                self.assertEqual(
                    (self.whole / "court1" / f"{table}.csv").read_bytes(),
                    header + b"".join(rows[index] for index in indexes),
                )
        self.assertIn(b"\nsecond\r\n", (self.whole / "court1" / "dockets.csv").read_bytes())
        self.assertIn(b'\\"quoted\\" and \\\\ slash',
                      (self.whole / "court1" / "dockets.csv").read_bytes())
        self.assertIn(b",NULL,\r\n", (self.whole / "court1" / "dockets.csv").read_bytes())

    def test_outside_court_has_no_directory_or_rows(self) -> None:
        """A court omitted from the arguments contributes no work files."""

        self.run_stage()
        self.assertFalse((self.whole / "court3").exists())
        output = b"".join(path.read_bytes() for path in self.whole.glob("*/*.csv"))
        for table in staging.STAGE_TABLES[1:]:
            with self.subTest(table=table):
                self.assertNotIn(self.lines[table][1][1], output)

    def test_record_contains_every_field_and_count(self) -> None:
        """The persisted stage record describes every read and selected row."""

        record = self.run_stage()
        expected_inputs = {
            table: {
                **item,
                "size": (self.dump / item["path"]).stat().st_size,
            }
            for table, item in self.inputs.items()
        }
        expected = {
            "schema": 1, "label": self.label, "source": self.source,
            "snapshot": self.snapshot, "courts": self.courts,
            "inputs": expected_inputs,
            "records": {table: len(rows) for table, (_, rows) in self.lines.items()},
            "counts": {
                "court1": dict.fromkeys(staging.STAGE_TABLES[1:], 2),
                "court2": dict.fromkeys(staging.STAGE_TABLES[1:], 0),
            },
            "job": self.job, "seconds": 0.0,
            "staged_at": self.instant.isoformat(),
        }
        self.assertEqual(json.loads((self.whole / staging.STAGE_RECORD_NAME).read_text()), expected)
        self.assertEqual(staging.read_record(self.whole / staging.STAGE_RECORD_NAME), record)

    def test_court_without_related_rows_has_headers_and_zero_counts(self) -> None:
        """A selected court with no matching docket still receives four files."""

        record = self.run_stage()
        for table in staging.STAGE_TABLES[1:]:
            with self.subTest(table=table):
                self.assertEqual(record.counts["court2"][table], 0)
                self.assertEqual(
                    (self.whole / "court2" / f"{table}.csv").read_bytes(),
                    self.lines[table][0],
                )

    def test_work_directories_and_files_have_worker_modes(self) -> None:
        """The completed stage uses group directories and read-only files."""

        self.run_stage()
        for path in (self.whole.parent, self.whole, *(self.whole / court for court in self.courts)):
            with self.subTest(path=path):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), fetch.DIR_MODE)
        for path in (self.whole / staging.STAGE_RECORD_NAME, *self.whole.glob("*/*.csv")):
            with self.subTest(path=path):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), fetch.FILE_MODE)

    def test_second_run_returns_first_record_without_rewriting(self) -> None:
        """A whole matching stage is the idempotent answer."""

        first = self.run_stage()
        paths = [self.whole / staging.STAGE_RECORD_NAME, *self.whole.glob("*/*.csv")]
        before = {path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in paths}
        self.job += 1
        second = self.run_stage()
        self.assertEqual(second, first)
        self.assertEqual(
            {path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in paths}, before,
        )

    def test_stale_partial_directory_is_removed_and_replaced(self) -> None:
        """The lock protects removal of an interrupted prior partial stage."""

        self.partial.mkdir(parents=True)
        stale = self.partial / "old-run"
        stale.write_text("fictitious interrupted output")
        self.run_stage()
        self.assertFalse(self.partial.exists())
        self.assertFalse((self.whole / stale.name).exists())
        self.assertTrue((self.whole / staging.STAGE_RECORD_NAME).is_file())

    def test_invalid_arguments_refuse_before_either_root_changes(self) -> None:
        """Invalid names, courts, and input maps never enter either root."""

        bad = [
            {"label": "../other"},
            {"snapshot": "fictional-dump-2099-13-01"},
            {"courts": ["court2", "court1"]},
            {"courts": ["court1", "court1"]},
            {"inputs": {**self.inputs, "extra": self.inputs["courts"]}},
            {"inputs": {key: value for key, value in self.inputs.items() if key != "courts"}},
            {"inputs": {**self.inputs, "courts": {
                **self.inputs["courts"], "path": "courts.fetch.json",
            }}},
        ]
        before = {path.relative_to(self.snapshots_root): path.stat().st_mtime_ns
                  for path in self.snapshots_root.rglob("*")}
        for arguments in bad:
            with self.subTest(arguments=arguments):
                with self.assertRaises(staging.StageFailure) as raised:
                    self.run_stage(**arguments)  # type: ignore[arg-type]
                self.assertEqual(raised.exception.reason, "invalid")
                self.assertEqual(
                    {path.relative_to(self.snapshots_root): path.stat().st_mtime_ns
                     for path in self.snapshots_root.rglob("*")}, before,
                )
                self.assertEqual(list(self.work_root.iterdir()), [])
        # A safe path can hold the structured invalid outcome after the refusal.
        with self.assertRaises(staging.StageFailure) as raised:
            self.run_stage(courts=["court2", "court1"])
        self.assert_failure(raised.exception)
        self.assertFalse(self.whole.exists())

    def test_missing_input_records_and_absent_whole_file(self) -> None:
        """A missing, partial, or orphaned fetch record names its table."""

        table = "dockets"
        path = self.dump / self.inputs[table]["path"]
        sidecar = path.with_name(path.name + fetch.RECORD_SUFFIX)
        whole = json.loads(sidecar.read_text())
        for condition in ("no record", "partial record", "no file"):
            with self.subTest(condition=condition):
                if condition == "no record":
                    sidecar.unlink(missing_ok=True)
                elif condition == "partial record":
                    partial = {**whole, "state": "partial", "size": None,
                               "sha256": None, "fetched_at": None}
                    sidecar.write_text(json.dumps(partial))
                else:
                    sidecar.write_text(json.dumps(whole))
                    path.unlink()
                with self.assertRaises(staging.StageFailure) as raised:
                    self.run_stage()
                self.assertEqual((raised.exception.reason, raised.exception.table),
                                 ("missing-input", table))
                self.assert_failure(raised.exception)
                self.assertFalse(self.whole.exists())

    def test_input_digest_or_recorded_size_mismatch(self) -> None:
        """The job compares a pin digest and the whole fetch record's size."""

        table = "citations"
        altered = {key: item.copy() for key, item in self.inputs.items()}
        altered[table]["sha256"] = "a" * 64
        with self.assertRaises(staging.StageFailure) as raised:
            self.run_stage(inputs=altered)
        self.assertEqual((raised.exception.reason, raised.exception.table),
                         ("input-mismatch", table))
        self.assert_failure(raised.exception)

        path = self.dump / self.inputs[table]["path"]
        sidecar = path.with_name(path.name + fetch.RECORD_SUFFIX)
        whole = json.loads(sidecar.read_text())
        whole["size"] += 1
        whole["durable"] += 1
        sidecar.unlink()
        sidecar.write_text(json.dumps(whole))
        with self.assertRaises(staging.StageFailure) as raised:
            self.run_stage()
        self.assertEqual((raised.exception.reason, raised.exception.table),
                         ("input-mismatch", table))
        self.assert_failure(raised.exception)

    def test_one_input_record_uses_the_same_fetch_checks(self) -> None:
        table = "citations"
        item = self.inputs[table]
        path = self.dump / item["path"]
        self.assertEqual(staging.input_record(self.dump, table, item), {
            "path": path.name, "sha256": item["sha256"], "size": path.stat().st_size,
        })
        with self.assertRaises(staging.StageFailure) as raised:
            staging.input_record(self.dump, table, {**item, "sha256": "0" * 64})
        self.assertEqual((raised.exception.reason, raised.exception.table),
                         ("input-mismatch", table))

    def test_outputs_accept_per_court_paths(self) -> None:
        paths = [self.work_root / f"{court}.csv" for court in self.courts]
        with staging._outputs(paths) as outputs:
            for output, court in zip(outputs, self.courts, strict=True):
                output.write(court.encode())
        for path, court in zip(paths, self.courts, strict=True):
            self.assertEqual(path.read_bytes(), court.encode())

    def test_staged_rows_check_the_header_and_width(self) -> None:
        self.run_stage()
        path = self.work_root / "staged.csv"
        path.write_bytes((self.whole / self.courts[0] / "dockets.csv").read_bytes())
        with staging.staged_rows(path, "dockets", ("id", "court_id")) as (columns, rows):
            self.assertEqual([fields[columns["id"]] for fields, _ in rows], ["d1", "d3"])
        path.write_bytes(path.read_bytes() + b"extra\n")
        with (staging.staged_rows(path, "dockets", ("id",)) as (_, rows),
              self.assertRaises(staging.StageFailure) as raised):
            list(rows)
        self.assertEqual((raised.exception.reason, raised.exception.table),
                         ("malformed", "dockets"))

    def test_unknown_court_names_first_absent_court(self) -> None:
        """The courts table must contain each requested court id."""

        with self.assertRaises(staging.StageFailure) as raised:
            self.run_stage(courts=["absent1", "absent2"])
        self.assertEqual((raised.exception.reason, raised.exception.table,
                          raised.exception.court), ("unknown-court", "courts", "absent1"))
        self.assert_failure(raised.exception)

    def test_malformed_tables_name_table_without_leaking_a_field(self) -> None:
        """Bad CSV width, syntax, encoding, header, and empty input are malformed."""

        table = "opinions"
        header, rows = self.lines[table]
        bad_bodies = {
            "wrong width": header + b"cl1\n",
            "unterminated quote": header + b'cl1,"PRIVATE_STAGE_SENTINEL unfinished\n',
            "undecodable byte": header + b"cl1,PRIVATE_STAGE_SENTINEL \xff\n",
            "missing column": b"other,note\n" + b"cl1,PRIVATE_STAGE_SENTINEL\n",
            "empty file": b"",
        }
        for condition, body in bad_bodies.items():
            with self.subTest(condition=condition):
                self.replace_table(table, body)
                with self.assertRaises(staging.StageFailure) as raised:
                    self.run_stage()
                self.assertEqual((raised.exception.reason, raised.exception.table),
                                 ("malformed", table))
                self.assert_failure(raised.exception)
                self.assertFalse(self.whole.exists())
        self.replace_table(table, header + b"".join(rows))

    def test_unwritable_work_root_is_local(self) -> None:
        """A work root that cannot hold a label produces a local refusal."""

        blocked = self.work_root / "not-a-directory"
        blocked.write_text("fictitious occupied work root")
        with self.assertRaises(staging.StageFailure) as raised:
            self.run_stage(work_root=blocked)
        self.assertEqual(raised.exception.reason, "local")
        staging.write_job_failure(
            blocked, self.label, self.snapshot, self.job, raised.exception,
            clock=lambda: self.instant,
        )
        self.assertEqual(blocked.read_text(), "fictitious occupied work root")
        self.assertFalse(self.failure_path.exists())

    def test_local_output_error_is_filed_when_work_root_is_writable(self) -> None:
        """A local write error preserves its class in a writable failure file."""

        replace = os.replace

        def deny_final_rename(source: str | os.PathLike[str],
                              destination: str | os.PathLike[str]) -> None:
            if Path(source) == self.partial and Path(destination) == self.whole:
                raise PermissionError("fictitious denied work rename")
            replace(source, destination)

        with (patch("gideon.worker.staging.os.replace", side_effect=deny_final_rename),
              self.assertRaises(staging.StageFailure) as raised):
            self.run_stage()
        self.assertEqual(raised.exception.reason, "local")
        self.assertEqual(raised.exception.error, "PermissionError")
        self.assert_failure(raised.exception)

    def test_busy_lock_refuses_without_a_failure_file(self) -> None:
        """A held stable sibling lock makes a second stage job busy."""

        self.lock.parent.mkdir()
        with self.lock.open("w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with self.assertRaises(staging.StageFailure) as raised:
                    self.run_stage()
                self.assertEqual(raised.exception.reason, "busy")
                staging.write_job_failure(
                    self.work_root, self.label, self.snapshot, self.job,
                    raised.exception, clock=lambda: self.instant,
                )
                self.assertFalse(self.failure_path.exists())
                self.assertFalse(self.whole.exists())
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def test_waiting_lock_acquires_after_the_holder_releases(self) -> None:
        self.lock.parent.mkdir()
        started = threading.Event()
        acquired = threading.Event()
        failures: list[Exception] = []

        def wait_for_lock() -> None:
            try:
                started.set()
                with staging.lock_partial(self.lock, waiting=True):
                    acquired.set()
            except (staging.StageFailure, OSError) as exc:
                failures.append(exc)

        with self.lock.open("w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            waiter = threading.Thread(target=wait_for_lock)
            waiter.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(acquired.wait(0.05))
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        waiter.join(1)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(acquired.is_set())

    def test_disagreeing_whole_stage_record_is_invalid(self) -> None:
        """An existing whole stage cannot be overwritten for different inputs."""

        first = self.run_stage()
        record_path = self.whole / staging.STAGE_RECORD_NAME
        before = (record_path.stat().st_ino, record_path.stat().st_mtime_ns)
        changed = {key: item.copy() for key, item in self.inputs.items()}
        changed["opinions"]["sha256"] = "b" * 64
        with self.assertRaises(staging.StageFailure) as raised:
            self.run_stage(inputs=changed)
        self.assertEqual(raised.exception.reason, "invalid")
        self.assert_failure(raised.exception)
        self.assertEqual(staging.read_record(record_path), first)
        self.assertEqual((record_path.stat().st_ino, record_path.stat().st_mtime_ns), before)

    def test_symbolic_links_in_snapshot_and_work_paths_are_refused(self) -> None:
        """Every protected directory and stage sidecar rejects a symlink."""

        cases = (
            "snapshot", "label", "source", "partial", "failure", "lock", "record",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as target_dir:
                target = Path(target_dir)
                if case == "snapshot":
                    path = self.dump
                    path.rename(target / "dump")
                    path.symlink_to(target / "dump", target_is_directory=True)
                else:
                    self.whole.parent.mkdir(exist_ok=True)
                    path = {
                        "label": self.whole.parent,
                        "source": self.whole,
                        "partial": self.partial,
                        "failure": self.failure_path,
                        "lock": self.lock,
                        "record": self.whole / staging.STAGE_RECORD_NAME,
                    }[case]
                    if case == "label":
                        path.rmdir()
                    if case == "record":
                        self.whole.mkdir(exist_ok=True)
                    path.symlink_to(target)
                with self.assertRaises(staging.StageFailure) as raised:
                    self.run_stage()
                self.assertEqual(raised.exception.reason, "invalid")
                staging.write_job_failure(
                    self.work_root, self.label, self.snapshot, self.job,
                    raised.exception, clock=lambda: self.instant,
                )
                self.assertFalse(self.whole.exists() and self.whole.is_dir()
                                 and (self.whole / staging.STAGE_RECORD_NAME).is_file())
                if case != "snapshot":
                    self.assertEqual(list(target.iterdir()), [])
                path.unlink()
                if case == "snapshot":
                    (target / "dump").rename(self.dump)
                elif case == "record":
                    self.whole.rmdir()
                self.failure_path.unlink(missing_ok=True)

    def test_logs_never_contain_dump_fields_even_at_progress(self) -> None:
        """The structured stage log reports counts without any CSV field text."""

        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger("gideon.worker.staging")
        previous = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        ticks = iter(range(0, 10000, fetch.LOG_INTERVAL_SECONDS + 1))
        try:
            self.run_stage(monotonic=lambda: float(next(ticks)))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        lines = output.getvalue().splitlines()
        self.assertTrue(any("action=stage_progress" in line for line in lines))
        self.assertTrue(any("action=stage_end" in line for line in lines))
        for line in lines:
            self.assertNotIn(self.sentinel.decode(), line)


if __name__ == "__main__":
    unittest.main()
