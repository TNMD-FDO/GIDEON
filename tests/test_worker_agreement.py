"""Citation-map staging over a fictitious staged corpus and kept map file."""

import bz2
import datetime
import fcntl
import hashlib
import json
import shutil
import tempfile
import threading
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import test_worker_caselaw
import test_worker_staging

from gideon.worker import agreement, fetch, settings


class FakeEdges:
    def __init__(self) -> None:
        self.resolved: dict[int, set[int]] = {10: {200}, 11: set()}
        self.unresolved: dict[int, set[str]] = {11: {"1 Fiction 2"}}
        self.closed = 0

    def ready_edges(self, source: str, snapshot_date: datetime.date, court: str) -> dict[int, set[int]]:
        del source, snapshot_date, court
        return self.resolved

    def unresolved_cites(self, source: str, snapshot_date: datetime.date, court: str) -> dict[int, set[str]]:
        del source, snapshot_date, court
        return self.unresolved

    def close(self) -> None:
        self.closed += 1


class WorkerAgreement(unittest.TestCase):
    def setUp(self) -> None:
        fixture = test_worker_staging.WorkerStaging(
            "test_selected_files_keep_input_bytes_under_each_header"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        fixture.replace_table("dockets", (
            b"id,court_id,note\r\n"
            b"1,court1,fictional\r\n"
            b"2,court2,fictional\n"
            b"3,court3,fictional\r\n"
        ))
        fixture.replace_table("opinion-clusters", (
            b"id,docket_id,note\n"
            b"100,1,fictional\r\n"
            b"200,2,fictional\n"
            b"300,3,fictional\r\n"
        ))
        fixture.replace_table("opinions", (
            b"id,cluster_id,note\n"
            b"10,100,fictional\r\n"
            b"11,100,fictional\n"
            b"20,200,fictional\r\n"
            b"30,300,outside\n"
        ))
        fixture.replace_table("citations", (
            b"cluster_id,volume,reporter,page\r\n"
            b"100,1,Fiction,2\r\n"
            b"200,2,Fiction,3\n"
            b"300,3,Fiction,4\r\n"
        ))
        self.staged = fixture.run_stage()
        self.header = b'"citing_opinion_id","cited_opinion_id","note"\r\n'
        self.lines = [
            b'"10","20","PRIVATE_MAP_SENTINEL first"\r\n',
            b'"11","99","PRIVATE_MAP_SENTINEL multi\nline"\n',
            b'"20","10","PRIVATE_MAP_SENTINEL second"\r\n',
            b'"30","10","PRIVATE_MAP_SENTINEL outside"\n',
        ]
        self.map_path = fixture.dump / "citation-map.csv.bz2"
        self.input = self.replace_map(self.header + b"".join(self.lines))
        self.map_dir = fixture.whole / agreement.AGREEMENT_MAP_DIRECTORY

    def run_agreement(
        self, edges: FakeEdges | None = None,
        *, clock: datetime.datetime | None = None,
    ) -> agreement.AgreementRecord:
        return agreement.agreement(
            self.fixture.snapshots_root, self.fixture.work_root,
            self.fixture.label, self.fixture.snapshot, self.fixture.courts[0],
            self.input, 42, edges or FakeEdges(),
            clock=lambda: clock or self.fixture.instant,
            monotonic=lambda: 10.0,
        )

    def replace_map(self, body: bytes) -> dict[str, str]:
        content = bz2.compress(body)
        self.map_path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        record_path = self.map_path.with_name(self.map_path.name + fetch.RECORD_SUFFIX)
        record_path.unlink(missing_ok=True)
        fetch.write_record(record_path, fetch.FetchRecord(
            "whole", fetch.KEPT_FORM, "archive.example.test", f"/{self.map_path.name}",
            None, None, len(content), len(content), len(content), digest,
            self.fixture.instant.isoformat(), 3, 0.0, 0,
        ))
        return {"path": self.map_path.name, "sha256": digest}

    def progress(self, job: int = 42) -> agreement._Progress:
        return agreement._Progress(job, self.fixture.label, lambda: 10.0, 10.0, 10.0, {})

    def stage_map(self, *, input: dict[str, str] | None = None) -> agreement.MapRecord:
        progress = self.progress()
        opinions = agreement._load_opinions(self.fixture.whole, self.staged, progress)
        return agreement._stage_map(
            self.fixture.dump, self.fixture.whole, self.staged,
            self.input if input is None else input, opinions, progress,
            clock=lambda: self.fixture.instant,
        )

    def test_argument_grammar_and_closed_failure_reasons(self) -> None:
        agreement.validate_arguments(
            self.fixture.label, self.fixture.snapshot, self.fixture.courts[0], self.input,
        )
        for label, snapshot, court, input in (
            ("bad-label", self.fixture.snapshot, self.fixture.courts[0], self.input),
            (self.fixture.label, "bad-snapshot", self.fixture.courts[0], self.input),
            (self.fixture.label, self.fixture.snapshot, "../court", self.input),
            (self.fixture.label, self.fixture.snapshot, self.fixture.courts[0],
             {"path": "../map", "sha256": self.input["sha256"]}),
        ):
            with self.subTest(label=label, snapshot=snapshot, court=court, input=input):
                with self.assertRaises(agreement.AgreementFailure) as raised:
                    agreement.validate_arguments(label, snapshot, court, input)
                self.assertEqual(raised.exception.reason, "invalid")
        with self.assertRaises(ValueError):
            agreement.AgreementFailure("unknown")

    def test_opinions_index_and_map_files_keep_selected_input_bytes(self) -> None:
        progress = self.progress()
        opinions = agreement._load_opinions(self.fixture.whole, self.staged, progress)
        self.assertEqual(opinions, {10: (0, 100), 11: (0, 100), 20: (1, 200)})
        record = agreement._stage_map(
            self.fixture.dump, self.fixture.whole, self.staged, self.input,
            opinions, progress, clock=lambda: self.fixture.instant,
        )
        self.assertEqual((self.map_dir / "court1.csv").read_bytes(),
                         self.header + self.lines[0] + self.lines[1])
        self.assertEqual((self.map_dir / "court2.csv").read_bytes(),
                         self.header + self.lines[2])
        self.assertNotIn(self.lines[3], (self.map_dir / "court1.csv").read_bytes())
        self.assertEqual(record, agreement.MapRecord(
            self.fixture.label, self.fixture.source, self.fixture.snapshot,
            self.fixture.courts,
            {"path": self.map_path.name, "sha256": self.input["sha256"],
             "size": self.map_path.stat().st_size},
            len(self.lines), {"court1": 2, "court2": 1},
            {"court1": 2, "court2": 1}, 42, 0.0,
            self.fixture.instant.isoformat(),
        ))
        self.assertEqual(json.loads((self.map_dir / agreement.AGREEMENT_MAP_RECORD_NAME).read_text()),
                         asdict(record))
        self.assertTrue((self.fixture.whole / agreement.MAP_LOCK_NAME).is_file())

    def test_second_call_finds_whole_record_without_restaging(self) -> None:
        first = self.stage_map()
        before = {
            path.name: (path.stat().st_mtime_ns, path.read_bytes())
            for path in self.map_dir.iterdir()
        }
        with patch("gideon.worker.agreement.bz2.open", side_effect=AssertionError("restaged")):
            second = self.stage_map()
        self.assertEqual(second, first)
        self.assertEqual({
            path.name: (path.stat().st_mtime_ns, path.read_bytes())
            for path in self.map_dir.iterdir()
        }, before)

    def test_stale_partial_is_removed_before_staging(self) -> None:
        partial = self.fixture.whole / f"{agreement.AGREEMENT_MAP_DIRECTORY}{fetch.PARTIAL_SUFFIX}"
        partial.mkdir()
        (partial / "stale").write_text("fictional stale partial")
        self.stage_map()
        self.assertFalse(partial.exists())
        self.assertFalse((self.map_dir / "stale").exists())

    def test_disagreeing_whole_map_directory_is_invalid(self) -> None:
        self.stage_map()
        record_path = self.map_dir / agreement.AGREEMENT_MAP_RECORD_NAME
        value = json.loads(record_path.read_text())
        value["label"] = "corpus-2099-01-02"
        record_path.unlink()
        record_path.write_text(json.dumps(value))
        with self.assertRaises(agreement.AgreementFailure) as raised:
            self.stage_map()
        self.assertEqual(raised.exception.reason, "invalid")
        self.assertEqual(json.loads(record_path.read_text()), value)

    def test_malformed_map_rows_name_the_table(self) -> None:
        for body in (
            b"wrong,columns\n1,2\n",
            self.header + b'"10","20"\n',
            self.header + b'"zero","20","fictional"\n',
            self.header + b'"10","0","fictional"\n',
        ):
            with self.subTest(body=body):
                self.input = self.replace_map(body)
                with self.assertRaises(agreement.AgreementFailure) as raised:
                    self.stage_map()
                self.assertEqual((raised.exception.reason, raised.exception.table),
                                 ("malformed", agreement.AGREEMENT_TABLE))
                self.assertFalse(self.map_dir.exists())

    def test_missing_staged_court_directory_is_stage_mismatch(self) -> None:
        shutil.rmtree(self.fixture.whole / self.fixture.courts[1])
        with self.assertRaises(agreement.AgreementFailure) as raised:
            agreement._load_opinions(self.fixture.whole, self.staged, self.progress())
        self.assertEqual((raised.exception.reason, raised.exception.table),
                         ("stage-mismatch", "opinions"))

    def test_cluster_cites_join_staged_columns_with_single_spaces(self) -> None:
        cites = agreement._load_cluster_cites(self.fixture.whole, self.staged, self.progress())
        self.assertEqual(cites, {100: {"1 Fiction 2"}, 200: {"2 Fiction 3"}})

    def test_compare_counts_both_sides_and_the_seen_missed_split(self) -> None:
        result = agreement.compare(
            {10, 11, 20, 40},
            {10: {100, 200}, 11: set(), 20: {200}, 40: set()},
            {11: {"3 Fiction 4"}, 20: {"other cite"}},
            {300: {"3 Fiction 4"}, 100: {"1 Fiction 2"}},
            {10: (0, 100), 11: (0, 100), 20: (1, 200),
             21: (1, 200), 30: (0, 300), 40: (0, 400)},
            [(10, 10), (10, 20), (10, 21), (11, 30),
             (20, 10), (20, 999), (30, 10)],
        )
        self.assertEqual(result, agreement.Comparison(4, 3, 6, 1, 4, 2, 1, 1, 1))
        self.assertEqual(
            agreement.compare({10}, {10: set()}, {}, {}, {10: (0, 100)}, []),
            agreement.Comparison(1, 0, 0, 0, 0, 0, 0, 0, 0),
        )

    def test_job_writes_figure_and_rewrites_it_without_restaging(self) -> None:
        edges = FakeEdges()
        with self.assertLogs(agreement.logger, level="INFO") as captured:
            figure = self.run_agreement(edges)
        self.assertEqual(edges.closed, 1)
        self.assertEqual(
            (figure.documents, figure.gideon_pairs, figure.map_rows,
             figure.map_outside, figure.map_pairs, figure.agreed,
             figure.gideon_only, figure.map_only_seen, figure.map_only_missed),
            (2, 1, 2, 1, 1, 1, 0, 0, 0),
        )
        path = self.fixture.whole / f"{self.fixture.courts[0]}.{agreement.AGREEMENT_RECORD_NAME}"
        self.assertEqual(json.loads(path.read_text()), asdict(figure))
        log = "\n".join(captured.output)
        self.assertIn("action=agreement_end", log)
        self.assertIn("map_outside=1", log)
        self.assertNotIn("PRIVATE_MAP_SENTINEL", log)
        self.assertNotIn("PRIVATE_STAGE_SENTINEL", log)
        map_record = (self.map_dir / agreement.AGREEMENT_MAP_RECORD_NAME).read_bytes()
        later = self.fixture.instant + datetime.timedelta(seconds=1)
        with patch("gideon.worker.agreement.bz2.open", side_effect=AssertionError("restaged")):
            second = self.run_agreement(clock=later)
        self.assertEqual((second.documents, second.map_pairs),
                         (figure.documents, figure.map_pairs))
        self.assertEqual(second.computed_at, later.isoformat())
        self.assertEqual(json.loads(path.read_text()), asdict(second))
        self.assertEqual((self.map_dir / agreement.AGREEMENT_MAP_RECORD_NAME).read_bytes(),
                         map_record)

    def test_job_refuses_a_noninteger_in_a_staged_map_row(self) -> None:
        self.stage_map()
        path = self.map_dir / f"{self.fixture.courts[0]}.csv"
        path.unlink()
        path.write_bytes(self.header + b'"no-id","20","fictional"\n')
        with self.assertRaises(agreement.AgreementFailure) as raised:
            self.run_agreement()
        self.assertEqual((raised.exception.reason, raised.exception.table),
                         ("malformed", agreement.AGREEMENT_TABLE))

    def test_second_court_waits_on_the_map_lock_then_finds_it_whole(self) -> None:
        self.stage_map()
        finished = threading.Event()
        results: list[agreement.AgreementRecord | Exception] = []

        def second_court() -> None:
            try:
                results.append(agreement.agreement(
                    self.fixture.snapshots_root, self.fixture.work_root,
                    self.fixture.label, self.fixture.snapshot, self.fixture.courts[1],
                    self.input, 43, FakeEdges(),
                    clock=lambda: self.fixture.instant, monotonic=lambda: 10.0,
                ))
            except (agreement.AgreementFailure, OSError) as exc:
                results.append(exc)
            finally:
                finished.set()

        lock_path = self.fixture.whole / agreement.MAP_LOCK_NAME
        with (patch("gideon.worker.agreement.bz2.open", side_effect=AssertionError("restaged")),
              lock_path.open("r") as lock):
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            waiter = threading.Thread(target=second_court)
            waiter.start()
            self.assertFalse(finished.wait(0.2))
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            waiter.join(5)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(len(results), 1)
        figure = results[0]
        assert isinstance(figure, agreement.AgreementRecord)
        self.assertEqual((figure.court, figure.job), (self.fixture.courts[1], 43))
        self.assertTrue(
            (self.fixture.whole / f"{self.fixture.courts[1]}.{agreement.AGREEMENT_RECORD_NAME}").is_file()
        )

    def test_busy_court_lock_files_no_failure(self) -> None:
        lock_path = self.fixture.whole / f"{self.fixture.courts[0]}{agreement.AGREEMENT_LOCK_SUFFIX}"
        failure_path = self.fixture.whole / f"{self.fixture.courts[0]}.{agreement.AGREEMENT_FAILURE_NAME}"
        with lock_path.open("w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                with self.assertRaises(agreement.AgreementFailure) as raised:
                    self.run_agreement()
                self.assertEqual(raised.exception.reason, "busy")
                agreement.write_job_failure(
                    self.fixture.work_root, self.fixture.label, self.fixture.snapshot,
                    self.fixture.courts[0], 42, raised.exception,
                )
                self.assertFalse(failure_path.exists())
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def test_failure_records_use_the_closed_reasons_and_clear_on_next_job(self) -> None:
        path = self.fixture.whole / f"{self.fixture.courts[0]}.{agreement.AGREEMENT_FAILURE_NAME}"
        for reason in sorted(agreement.AGREEMENT_FAILURE_REASONS - {"busy"}):
            with self.subTest(reason=reason):
                failure = agreement.AgreementFailure(reason, table="fictional-table")
                agreement.write_job_failure(
                    self.fixture.work_root, self.fixture.label, self.fixture.snapshot,
                    self.fixture.courts[0], 42, failure, clock=lambda: self.fixture.instant,
                )
                value = json.loads(path.read_text())
                self.assertEqual(value, asdict(agreement.AgreementFailureRecord(
                    42, self.fixture.courts[0], reason, "fictional-table", None,
                    self.fixture.instant.isoformat(),
                )))
        self.run_agreement()
        self.assertFalse(path.exists())

    def test_invalid_arguments_and_missing_stage_touch_no_files(self) -> None:
        before = set(self.fixture.whole.rglob("*"))
        for label, snapshot, court, input in (
            ("bad-label", self.fixture.snapshot, self.fixture.courts[0], self.input),
            (self.fixture.label, "bad-snapshot", self.fixture.courts[0], self.input),
            (self.fixture.label, self.fixture.snapshot, "../court", self.input),
            (self.fixture.label, self.fixture.snapshot, self.fixture.courts[0],
             {"path": "../map", "sha256": self.input["sha256"]}),
        ):
            with self.subTest(label=label, snapshot=snapshot, court=court, input=input):
                with self.assertRaises(agreement.AgreementFailure) as raised:
                    agreement.agreement(
                        self.fixture.snapshots_root, self.fixture.work_root,
                        label, snapshot, court, input, 42, FakeEdges(),
                    )
                self.assertEqual(raised.exception.reason, "invalid")
                self.assertEqual(set(self.fixture.whole.rglob("*")), before)
        (self.fixture.whole / "stage.json").unlink()
        with self.assertRaises(agreement.AgreementFailure) as raised:
            self.run_agreement()
        self.assertEqual(raised.exception.reason, "missing-stage")

    def test_symbolic_links_and_input_refusals(self) -> None:
        target = self.fixture.work_root / "fictional-target"
        target.write_text("fictional target")
        path = self.fixture.whole / f"{self.fixture.courts[0]}.{agreement.AGREEMENT_FAILURE_NAME}"
        path.symlink_to(target)
        with self.assertRaises(agreement.AgreementFailure) as raised:
            self.run_agreement()
        self.assertEqual(raised.exception.reason, "stage-mismatch")
        self.assertEqual(target.read_text(), "fictional target")
        path.unlink()
        lock_path = self.fixture.whole / f"{self.fixture.courts[0]}{agreement.AGREEMENT_LOCK_SUFFIX}"
        lock_path.unlink()
        lock_path.symlink_to(target)
        with self.assertRaises(agreement.AgreementFailure) as raised:
            self.run_agreement()
        self.assertEqual(raised.exception.reason, "stage-mismatch")
        lock_path.unlink()
        outside_dir = self.fixture.work_root / "fictional-map-dir"
        outside_dir.mkdir()
        self.map_dir.symlink_to(outside_dir, target_is_directory=True)
        with self.assertRaises(agreement.AgreementFailure) as raised:
            self.run_agreement()
        self.assertEqual(raised.exception.reason, "invalid")
        self.map_dir.unlink()
        displaced_map = self.fixture.work_root / "fictional-map-file"
        self.map_path.rename(displaced_map)
        self.map_path.symlink_to(displaced_map)
        with self.assertRaises(agreement.AgreementFailure) as raised:
            self.run_agreement()
        self.assertEqual(raised.exception.reason, "invalid")
        self.map_path.unlink()
        displaced_map.rename(self.map_path)
        self.assertEqual(target.read_text(), "fictional target")
        fetch_record = self.map_path.with_name(self.map_path.name + fetch.RECORD_SUFFIX)
        fetch_record.unlink()
        with self.assertRaises(agreement.AgreementFailure) as raised:
            self.run_agreement()
        self.assertEqual(raised.exception.reason, "missing-input")

    def test_database_and_local_failures_close_the_record(self) -> None:
        edges = FakeEdges()
        failure = agreement.AgreementFailure("database")
        with (patch.object(edges, "ready_edges", side_effect=failure),
              patch.object(edges, "close", side_effect=RuntimeError("private close detail")),
              self.assertRaises(agreement.AgreementFailure) as raised):
            self.run_agreement(edges)
        self.assertIs(raised.exception, failure)
        with (patch("gideon.worker.agreement.staging._write_json", side_effect=OSError("private")),
              self.assertRaises(agreement.AgreementFailure) as raised):
            self.run_agreement()
        self.assertEqual(raised.exception.reason, "local")


class PsycopgEdgeRecord(unittest.TestCase):
    def test_two_statements_bind_only_source_date_and_court(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            password = Path(temporary) / "password"
            password.write_text("fictional secret")
            configuration = settings.Settings(
                "db.example.test", 5432, "gideon", "gideon_worker", password, 2,
            )
            connection = test_worker_caselaw._CapturedConnection()
            record = agreement.PsycopgEdges(
                connect=lambda **_kwargs: connection, worker_settings=configuration,
            )
            snapshot_date = datetime.date(2099, 1, 1)
            connection.rows = [(10, 100), (10, 100), (11, None)]
            self.assertEqual(record.ready_edges("fictional-dump", snapshot_date, "court1"),
                             {10: {100}, 11: set()})
            connection.rows = [(11, "1 Fiction 2"), (11, "1 Fiction 2")]
            self.assertEqual(record.unresolved_cites("fictional-dump", snapshot_date, "court1"),
                             {11: {"1 Fiction 2"}})
            self.assertEqual(connection.statements, [
                (agreement.READY_EDGES_SQL, ("fictional-dump", snapshot_date, "court1")),
                (agreement.UNRESOLVED_CITES_SQL, ("fictional-dump", snapshot_date, "court1")),
            ])
            self.assertIn("LEFT JOIN citations", agreement.READY_EDGES_SQL)
            self.assertIn("c.to_cluster IS NOT NULL", agreement.READY_EDGES_SQL)
            self.assertIn("d.status = 'ready'", agreement.READY_EDGES_SQL)
            self.assertIn("c.to_cluster IS NULL", agreement.UNRESOLVED_CITES_SQL)
            self.assertIn("c.reporter_cite IS NOT NULL", agreement.UNRESOLVED_CITES_SQL)
            self.assertEqual((connection.commits, connection.rollbacks), (2, 0))
            record.close()
            self.assertTrue(connection.closed)

    def test_driver_error_is_database_with_class_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            password = Path(temporary) / "password"
            password.write_text("fictional secret")
            configuration = settings.Settings(
                "db.example.test", 5432, "gideon", "gideon_worker", password, 2,
            )
            connection = test_worker_caselaw._CapturedConnection()
            connection.fail_execute = True
            record = agreement.PsycopgEdges(
                connect=lambda **_kwargs: connection, worker_settings=configuration,
            )
            with self.assertRaises(agreement.AgreementFailure) as raised:
                record.ready_edges("fictional-dump", datetime.date(2099, 1, 1), "court1")
            self.assertEqual((raised.exception.reason, raised.exception.error),
                             ("database", "OperationalError"))
            self.assertNotIn("private database detail", str(raised.exception))
            self.assertEqual(connection.rollbacks, 1)
            record.close()


if __name__ == "__main__":
    unittest.main()
