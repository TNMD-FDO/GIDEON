"""Clustering job arguments and recorded failures over the host seam."""

import json
import subprocess
import unittest
from dataclasses import asdict, replace

from test_index_lexical_host import JOB_ID, QueueHost, sample_inputs
from test_index_record import NAME, RENDERED, inputs_for, row_for

from gideon.host import caselaw, report, stack, worker
from gideon.host.index.dupes import defer_dupes, read_dupes
from gideon.host.index.lexical import BuildRead
from gideon.host.index.sample import GoldDocument
from gideon.host.render.worker import (
    DUPES_FAILURE_REASONS,
    DUPES_LOCK_PREFIX,
    DUPES_QUEUE,
    DUPES_TASK,
    WORKER_SERVICE_NAME,
)
from gideon.host.report import Problem


def failed_host(reason: str) -> QueueHost:
    host = QueueHost()
    host.status = "failed"
    host.generation_output = json.dumps(asdict(replace(row_for(), state="failed", failure_reason=reason)))
    return host


class Defer(unittest.TestCase):
    def test_sample_arguments_are_bound_on_stdin_without_the_reporting_version(self) -> None:
        selection = sample_inputs()
        inputs = replace(inputs_for(), sample=selection)
        host = QueueHost()
        self.assertEqual(defer_dupes(host, RENDERED, inputs), JOB_ID)
        self.assertEqual(len(host.calls), 1)
        argv, sql = host.calls[0]
        assert sql is not None
        args = json.loads(sql.split("\\set v_args '", 1)[1].split("'", 1)[0])
        self.assertEqual(args["sample"], {
            "gold": [{"source_id": document.source_id, "sha256": document.sha256} for document in selection.gold],
            "fill_per_million": selection.fill_per_million,
        })
        self.assertNotIn(selection.set_version, sql)
        for document in selection.gold:
            self.assertNotIn(document.source_id, " ".join(argv))
            if document.sha256 is not None:
                self.assertNotIn(document.sha256, " ".join(argv))

    def test_invalid_sample_refuses_before_queueing_with_clustering_words(self) -> None:
        selection = sample_inputs()
        sentinel = "FICTITIOUS invalid selection; do not print"
        for invalid, field in (
            (sentinel, "sample"),
            (replace(selection, gold=(GoldDocument(sentinel, None),)), "source_id"),
            (replace(selection, gold=(GoldDocument("example:opinion:17", sentinel),)), "sha256"),
            (replace(selection, fill_per_million=0), "fill_per_million"),
        ):
            with self.subTest(field=field):
                host = QueueHost()
                result = defer_dupes(
                    host, RENDERED, replace(inputs_for(), sample=invalid),  # type: ignore[arg-type]
                    command_path="index build caselaw --sample",
                )
                assert isinstance(result, Problem)
                self.assertIn("clustering", result.problem)
                self.assertIn(field, result.problem)
                self.assertNotIn(sentinel, result.problem + result.fix)
                self.assertIn(report.command("index build caselaw --sample"), result.fix)
                self.assertEqual(host.calls, [])

    def test_statement_binds_all_arguments_and_lock_on_stdin(self) -> None:
        inputs = inputs_for()
        host = QueueHost()
        self.assertEqual(defer_dupes(host, RENDERED, inputs), JOB_ID)
        self.assertEqual(len(host.calls), 1)
        argv, sql = host.calls[0]
        self.assertEqual(argv, worker.psql_argv(RENDERED))
        assert sql is not None
        args_line = next(line for line in sql.splitlines() if line.startswith("\\set v_args "))
        args = json.loads(args_line.removeprefix("\\set v_args '").removesuffix("'"))
        self.assertEqual(args, {
            "name": inputs.name, "collection": inputs.collection, "label": inputs.label,
            "snapshot": inputs.snapshot, "pipeline": inputs.pipeline,
            "embedding_space": inputs.embedding_space, "inputs_hash": inputs.inputs_hash,
            "courts": {court: asdict(geography) for court, geography in inputs.geography.items()},
        })
        for key, value in (("v_task", DUPES_TASK), ("v_queue", DUPES_QUEUE),
                           ("v_lock", f"{DUPES_LOCK_PREFIX}{inputs.name}")):
            self.assertIn(worker.bind(key, value), sql)
            self.assertNotIn(value, " ".join(argv))
        for value in (inputs.name, inputs.label, inputs.snapshot, inputs.pipeline,
                      inputs.embedding_space, inputs.inputs_hash):
            self.assertNotIn(value, " ".join(argv))
        self.assertIn(":'v_lock'", sql)

    def test_a_found_generation_keeps_its_name_with_a_new_label(self) -> None:
        inputs = replace(inputs_for(), label="corpus-2099-02-03")
        self.assertEqual(defer_dupes(QueueHost(), RENDERED, inputs), JOB_ID)

    def test_each_grammar_refuses_before_queueing_with_clustering_words(self) -> None:
        inputs = inputs_for()
        invalid: tuple[dict[str, object], ...] = (
            {"name": "invalid"}, {"name": NAME.upper()}, {"name": NAME + "\n"},
            {"name": NAME + "a" * 255}, {"collection": "unknown"},
            {"label": "corpus-2099-1-3"}, {"label": "corpus-2099-02-30"},
            {"snapshot": "fictions;2099-01-02"}, {"snapshot": "fictions-2099-02-30"},
            {"pipeline": "1000.0.0"}, {"pipeline": "invalid"},
            {"embedding_space": "Example"}, {"embedding_space": "example--space"},
            {"embedding_space": ""}, {"inputs_hash": "A" * 64}, {"inputs_hash": "a" * 63},
            {"courts": ()}, {"courts": ["court1"]}, {"courts": ("Court1",)},
            {"courts": ("court1", "court1")}, {"courts": ("z9", "court1")},
            {"geography": {}}, {"geography": None},
            {"geography": {"court1": {"level": "circuit"}}},
            {"geography": {"court1": caselaw.CourtGeography("unknown", None, None)}},
            {"geography": {"court1": caselaw.CourtGeography("circuit", "unknown", None)}},
            {"geography": {"court1": caselaw.CourtGeography("circuit", "ca6", "unknown")}},
            {"geography": {**inputs.geography, "other1": next(iter(inputs.geography.values()))}},
        )
        for change in invalid:
            with self.subTest(change=change):
                host = QueueHost()
                result = defer_dupes(host, RENDERED, replace(inputs, **change))  # type: ignore[arg-type]
                assert isinstance(result, Problem)
                self.assertIn("clustering", result.problem)
                self.assertIn(report.command("index build"), result.fix)
                self.assertEqual(host.calls, [])

    def test_failed_or_unavailable_defer_refuses_without_retry_or_diagnostics(self) -> None:
        for code, error in ((3, None), (127, None), (0, FileNotFoundError()), (0, subprocess.SubprocessError())):
            with self.subTest(code=code, error=error):
                host = QueueHost()
                host.code, host.error, host.stderr = code, error, "private text sentinel"
                result = defer_dupes(host, RENDERED, inputs_for())
                assert isinstance(result, Problem)
                self.assertIn(stack.logs_fix(RENDERED, WORKER_SERVICE_NAME), result.fix)
                self.assertNotIn(host.stderr, result.problem + result.fix)
                self.assertEqual(len(host.calls), 1)


class Read(unittest.TestCase):
    def test_waiting_and_success_do_not_read_generation_failures(self) -> None:
        for status in ("todo", "doing", "succeeded"):
            with self.subTest(status=status):
                host = QueueHost()
                host.status = status
                self.assertEqual(read_dupes(host, RENDERED, JOB_ID, NAME),
                                 BuildRead(worker.JobRow(JOB_ID, status, 1), status == "succeeded", None, None))
                self.assertEqual(len(host.calls), 1)

    def test_every_failure_reason_reads_the_generation_and_offers_its_fix(self) -> None:
        for reason in DUPES_FAILURE_REASONS:
            with self.subTest(reason=reason):
                host = failed_host(reason)
                result = read_dupes(host, RENDERED, JOB_ID, NAME)
                assert isinstance(result, BuildRead) and result.failure is not None
                self.assertFalse(result.done)
                self.assertEqual(result.reason, reason)
                self.assertEqual(result.job.id, JOB_ID)
                self.assertEqual(len(host.calls), 2)
                self.assertIn("clustering", result.failure.problem)
                fix = result.failure.fix
                retry = report.command("index build")
                self.assertIn(retry, fix)
                if reason in {"invalid", "local", "database"}:
                    self.assertIn(stack.logs_fix(RENDERED, WORKER_SERVICE_NAME), fix)
                elif reason in {"text-mismatch", "missing-text"}:
                    self.assertIn(report.command("restore --from staging"), fix)
                    self.assertIn(report.command("apply"), fix)
                elif reason == "busy":
                    self.assertIn("Wait", fix)
                elif reason == "generation-mismatch":
                    self.assertIn("no longer matches its inputs", result.failure.problem)
                elif reason == "cluster-budget":
                    self.assertEqual(result.failure.problem, "clustering ran past its hour budget")
                    self.assertIn("Cut a smaller lockfile", fix)
                    self.assertLess(fix.index(report.command("corpus cut")), fix.index(retry))
                    self.assertIn("a corpus of this size needs incremental clustering first", fix)

    def test_terminal_statuses_and_missing_failed_rows(self) -> None:
        for status in ("failed", "aborted", "cancelled"):
            for output, reason in ((failed_host("busy").generation_output, "busy"),
                                   ("", "local"), (json.dumps(asdict(row_for())), "local")):
                with self.subTest(status=status, reason=reason):
                    host = QueueHost()
                    host.status, host.generation_output = status, output
                    result = read_dupes(host, RENDERED, JOB_ID, NAME)
                    assert isinstance(result, BuildRead) and result.failure is not None
                    self.assertEqual(result.reason, reason)

    def test_invalid_name_unknown_status_and_failed_reads_refuse(self) -> None:
        host = QueueHost()
        result = read_dupes(host, RENDERED, JOB_ID, "invalid")
        assert isinstance(result, Problem)
        self.assertIn("clustering", result.problem)
        self.assertEqual(host.calls, [])
        host.status = "unknown"
        result = read_dupes(host, RENDERED, JOB_ID, NAME)
        assert isinstance(result, Problem)
        self.assertIn("clustering", result.problem)
        for output in ("", "31|failed", "32|failed|1", "31|failed|bad"):
            with self.subTest(output=output):
                host = QueueHost()
                host.job_output = output
                self.assertIsInstance(read_dupes(host, RENDERED, JOB_ID, NAME), Problem)
        for code, output in ((1, json.dumps(asdict(row_for()))), (0, "broken")):
            with self.subTest(code=code, output=output):
                host = failed_host("local")
                host.generation_code, host.generation_output = code, output
                self.assertIsInstance(read_dupes(host, RENDERED, JOB_ID, NAME), Problem)
        host = QueueHost()
        host.code, host.stderr = 1, "private text sentinel"
        result = read_dupes(host, RENDERED, JOB_ID, NAME)
        assert isinstance(result, Problem)
        self.assertNotIn(host.stderr, result.problem + result.fix)
        self.assertEqual(len(host.calls), 1)

    def test_fixes_follow_the_installed_command_form(self) -> None:
        report.set_installed_form(True)
        self.addCleanup(report.set_installed_form, False)
        for reason in DUPES_FAILURE_REASONS:
            with self.subTest(reason=reason):
                result = read_dupes(failed_host(reason), RENDERED, JOB_ID, NAME)
                assert isinstance(result, BuildRead) and result.failure is not None
                self.assertIn("gideon index build", result.failure.fix)
                self.assertNotIn("python3 -m gideon", result.failure.fix)
