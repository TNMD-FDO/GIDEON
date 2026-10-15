"""Dense host arguments, generation failures, and progress projections over fakes."""

import json
import shlex
import subprocess
import unittest
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import UTC, datetime, timedelta

from test_index_lexical_host import JOB_ID, QueueHost, sample_inputs
from test_index_record import NAME, RENDERED, inputs_for, row_for

from gideon.host import caselaw, report, stack, worker
from gideon.host.index.dense import (
    TRIPWIRE_DAYS,
    DenseInputs,
    defer_dense,
    projection,
    read_dense,
)
from gideon.host.index.lexical import BuildRead
from gideon.host.index.sample import GoldDocument
from gideon.host.render.engine import EMBED_SERVICE_NAME
from gideon.host.render.qdrant import (
    QDRANT_REST_PORT,
    QDRANT_SECRET_NAME,
    QDRANT_SERVICE_NAME,
)
from gideon.host.render.worker import (
    DENSE_FAILURE_REASONS,
    DENSE_LOCK_PREFIX,
    DENSE_QUEUE,
    DENSE_TASK,
    DIMENSIONS_MAX,
    WORKER_SERVICE_NAME,
)
from gideon.host.report import Problem


def dense_inputs() -> DenseInputs:
    inputs = inputs_for()
    return DenseInputs(
        inputs.name, inputs.collection, inputs.label, inputs.snapshot, inputs.courts,
        inputs.geography, inputs.pipeline, inputs.embedding_space, inputs.inputs_hash,
        "example-embedding-model", 17,
    )


def failed_host(reason: str) -> QueueHost:
    host = QueueHost()
    host.status = "failed"
    host.generation_output = json.dumps(asdict(replace(row_for(), state="failed", failure_reason=reason)))
    return host


class Defer(unittest.TestCase):
    def test_dense_statement_binds_all_values_on_stdin_and_omits_an_absent_limit(self) -> None:
        inputs = dense_inputs()
        host = QueueHost()
        self.assertEqual(defer_dense(host, RENDERED, inputs), JOB_ID)
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
            "served_model": inputs.served_model, "dimensions": inputs.dimensions,
        })
        self.assertNotIn("sample", args)
        for key, value in (("v_task", DENSE_TASK), ("v_queue", DENSE_QUEUE),
                           ("v_lock", f"{DENSE_LOCK_PREFIX}{inputs.name}")):
            self.assertIn(worker.bind(key, value), sql)
            self.assertNotIn(value, " ".join(argv))
        for value in (inputs.name, inputs.label, inputs.snapshot, inputs.inputs_hash, inputs.served_model):
            self.assertNotIn(value, " ".join(argv))
        self.assertIn(":'v_lock'", sql)

    def test_sample_arguments_reach_the_dense_job_only_on_stdin(self) -> None:
        sample = sample_inputs()
        host = QueueHost()
        inputs = replace(dense_inputs(), sample=sample)

        self.assertEqual(defer_dense(host, RENDERED, inputs), JOB_ID)

        argv, sql = host.calls[0]
        assert sql is not None
        args = json.loads(sql.split("\\set v_args '", 1)[1].split("'", 1)[0])
        self.assertEqual(args["sample"], {
            "gold": [{"source_id": document.source_id, "sha256": document.sha256} for document in sample.gold],
            "fill_per_million": sample.fill_per_million,
        })
        self.assertNotIn(sample.set_version, sql)
        for document in sample.gold:
            self.assertNotIn(document.source_id, " ".join(argv))
            if document.sha256 is not None:
                self.assertNotIn(document.sha256, " ".join(argv))
        self.assertEqual(len(host.calls), 1)

    def test_invalid_sample_refuses_before_a_child_without_echoing_values(self) -> None:
        sample = sample_inputs()
        sentinel = "Private dense sample value; do not print"
        for invalid, field in (
            (sentinel, "sample"),
            (replace(sample, gold=(GoldDocument(sentinel, None),)), "source_id"),
            (replace(sample, gold=(GoldDocument("example:opinion:17", sentinel),)), "sha256"),
            (replace(sample, fill_per_million=False), "fill_per_million"),
        ):
            with self.subTest(field=field):
                host = QueueHost()
                result = defer_dense(
                    host, RENDERED, replace(dense_inputs(), sample=invalid),  # type: ignore[arg-type]
                    command_path="index build caselaw --sample",
                )
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("dense", result.problem)
                self.assertIn(field, result.problem)
                self.assertNotIn(sentinel, result.problem + result.fix)
                self.assertIn(report.command("index build caselaw --sample"), result.fix)
                self.assertEqual(host.calls, [])

    def test_limit_is_an_optional_positive_integer_keyword(self) -> None:
        host = QueueHost()
        self.assertEqual(defer_dense(host, RENDERED, dense_inputs(), limit=7), JOB_ID)
        self.assertIn('"limit":7', host.calls[0][1] or "")
        for limit in (0, -1, True, False, 1.5, "7"):
            with self.subTest(limit=limit):
                host = QueueHost()
                result = defer_dense(host, RENDERED, dense_inputs(), limit=limit)  # type: ignore[arg-type]
                assert isinstance(result, Problem)
                self.assertIn("dense document limit", result.problem)
                self.assertIn(report.command("index build"), result.fix)
                self.assertEqual(host.calls, [])

    def test_a_found_generation_keeps_its_name_with_a_new_label(self) -> None:
        self.assertEqual(defer_dense(QueueHost(), RENDERED, replace(dense_inputs(), label="corpus-2099-02-03")), JOB_ID)

    def test_each_grammar_refuses_before_queueing_with_dense_words(self) -> None:
        inputs = dense_inputs()
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
            {"served_model": ""}, {"served_model": "example/model"}, {"served_model": "model\n"},
            {"served_model": "model;echo"}, {"served_model": None},
            {"dimensions": 0}, {"dimensions": -1}, {"dimensions": DIMENSIONS_MAX + 1},
            {"dimensions": True}, {"dimensions": False}, {"dimensions": "17"}, {"dimensions": 17.0},
        )
        for change in invalid:
            with self.subTest(change=change):
                host = QueueHost()
                result = defer_dense(host, RENDERED, replace(inputs, **change))  # type: ignore[arg-type]
                assert isinstance(result, Problem)
                self.assertIn("dense", result.problem)
                self.assertIn(report.command("index build"), result.fix)
                self.assertEqual(host.calls, [])

    def test_dimension_boundaries_are_accepted(self) -> None:
        for dimensions in (1, DIMENSIONS_MAX):
            with self.subTest(dimensions=dimensions):
                self.assertEqual(defer_dense(QueueHost(), RENDERED, replace(dense_inputs(), dimensions=dimensions)), JOB_ID)

    def test_failed_or_unavailable_defer_is_a_refusal_without_retry_or_diagnostics(self) -> None:
        for code, error in ((3, None), (127, None), (0, FileNotFoundError()), (0, subprocess.SubprocessError())):
            with self.subTest(code=code, error=error):
                host = QueueHost()
                host.code, host.error, host.stderr = code, error, "private text sentinel"
                result = defer_dense(host, RENDERED, dense_inputs())
                assert isinstance(result, Problem)
                self.assertNotIn(host.stderr, result.problem + result.fix)
                self.assertEqual(len(host.calls), 1)


class Read(unittest.TestCase):
    def test_waiting_and_success_do_not_read_generation_failures(self) -> None:
        for status in ("todo", "doing", "succeeded"):
            with self.subTest(status=status):
                host = QueueHost()
                host.status = status
                self.assertEqual(read_dense(host, RENDERED, JOB_ID, NAME),
                                 BuildRead(worker.JobRow(JOB_ID, status, 1), status == "succeeded", None, None))
                self.assertEqual(len(host.calls), 1)

    def test_every_failure_reason_reads_the_generation_and_offers_its_fix(self) -> None:
        for reason in DENSE_FAILURE_REASONS:
            with self.subTest(reason=reason):
                host = failed_host(reason)
                result = read_dense(host, RENDERED, JOB_ID, NAME)
                assert isinstance(result, BuildRead) and result.failure is not None
                self.assertFalse(result.done)
                self.assertEqual(result.reason, reason)
                self.assertEqual(result.job.id, JOB_ID)
                self.assertEqual(len(host.calls), 2)
                self.assertIn("dense", result.failure.problem)
                fix = result.failure.fix
                retry = report.command("index build")
                self.assertIn(retry, fix)
                if reason in {"invalid", "local", "database"}:
                    self.assertIn(stack.logs_fix(RENDERED, WORKER_SERVICE_NAME), fix)
                elif reason in {"store", "embed"}:
                    service = QDRANT_SERVICE_NAME if reason == "store" else EMBED_SERVICE_NAME
                    action = report.command("apply" if reason == "store" else "engine verify")
                    self.assertLess(fix.index(stack.logs_fix(RENDERED, service)), fix.index(action))
                    self.assertLess(fix.index(action), fix.index(retry))
                elif reason == "chunk-too-long":
                    self.assertIn(stack.logs_fix(RENDERED, WORKER_SERVICE_NAME), fix)
                    self.assertIn("restore the pipeline registry", fix)
                elif reason in {"text-mismatch", "missing-text"}:
                    self.assertIn(report.command("restore --from staging"), fix)
                    self.assertIn(report.command("apply"), fix)
                elif reason == "busy":
                    self.assertIn("Wait", fix)
                elif reason == "generation-mismatch":
                    self.assertIn("no longer matches its inputs", result.failure.problem)
                elif reason == "config-mismatch":
                    self.assertIn("Remove the derived collection", fix)

    def test_collection_removal_reads_the_key_only_inside_the_store_container(self) -> None:
        host = failed_host("config-mismatch")
        result = read_dense(host, RENDERED, JOB_ID, NAME)
        assert isinstance(result, BuildRead) and result.failure is not None
        command = result.failure.fix.removeprefix("Remove the derived collection with ").split(", then run ", 1)[0]
        argv = shlex.split(command)
        self.assertEqual(argv[:-1], stack.exec_argv(RENDERED, QDRANT_SERVICE_NAME, "bash", "-c"))
        self.assertIn(f"/dev/tcp/127.0.0.1/{QDRANT_REST_PORT}", argv[-1])
        self.assertIn(f"DELETE /collections/{NAME} HTTP/1.0\\r\\napi-key: %s", argv[-1])
        self.assertIn(f"$(cat /run/secrets/{QDRANT_SECRET_NAME})", argv[-1])
        self.assertIn("set -e -o pipefail; api_key=$(cat ", argv[-1])
        self.assertIn('test -n "$api_key";', argv[-1])
        self.assertIn('"$api_key" >&3;', argv[-1])
        self.assertIn("grep -q ' 200 '", argv[-1])
        self.assertNotIn("private key sentinel", " ".join(argv))
        self.assertNotIn("index gc", result.failure.fix)
        self.assertEqual(len(host.calls), 2)
        for call, _ in host.calls:
            self.assertEqual(call, worker.psql_argv(RENDERED))

    def test_terminal_statuses_and_missing_failed_rows(self) -> None:
        for status in ("failed", "aborted", "cancelled"):
            for output, reason in ((failed_host("busy").generation_output, "busy"),
                                   ("", "local"), (json.dumps(asdict(row_for())), "local")):
                with self.subTest(status=status, reason=reason):
                    host = QueueHost()
                    host.status, host.generation_output = status, output
                    result = read_dense(host, RENDERED, JOB_ID, NAME)
                    assert isinstance(result, BuildRead) and result.failure is not None
                    self.assertEqual(result.reason, reason)

    def test_invalid_name_unknown_status_and_failed_reads_refuse(self) -> None:
        host = QueueHost()
        result = read_dense(host, RENDERED, JOB_ID, "invalid")
        assert isinstance(result, Problem)
        self.assertIn("dense", result.problem)
        self.assertEqual(host.calls, [])
        host.status = "unknown"
        result = read_dense(host, RENDERED, JOB_ID, NAME)
        assert isinstance(result, Problem)
        self.assertIn("dense", result.problem)
        for output in ("", "31|failed", "32|failed|1", "31|failed|bad"):
            with self.subTest(output=output):
                host = QueueHost()
                host.job_output = output
                self.assertIsInstance(read_dense(host, RENDERED, JOB_ID, NAME), Problem)
        for code, output in ((1, json.dumps(asdict(row_for()))), (0, "broken")):
            with self.subTest(code=code, output=output):
                host = failed_host("local")
                host.generation_code, host.generation_output = code, output
                self.assertIsInstance(read_dense(host, RENDERED, JOB_ID, NAME), Problem)
        host = QueueHost()
        host.code, host.stderr = 1, "private text sentinel"
        result = read_dense(host, RENDERED, JOB_ID, NAME)
        assert isinstance(result, Problem)
        self.assertNotIn(host.stderr, result.problem + result.fix)
        self.assertEqual(len(host.calls), 1)

    def test_fixes_follow_the_installed_command_form(self) -> None:
        report.set_installed_form(True)
        self.addCleanup(report.set_installed_form, False)
        for reason in DENSE_FAILURE_REASONS:
            with self.subTest(reason=reason):
                result = read_dense(failed_host(reason), RENDERED, JOB_ID, NAME)
                assert isinstance(result, BuildRead) and result.failure is not None
                self.assertIn("gideon index build", result.failure.fix)
                self.assertNotIn("python3 -m gideon", result.failure.fix)


class Projections(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2099, 1, 3, 12, tzinfo=UTC)

    def test_rates_remaining_finish_and_unlimited_generation_over_a_limited_scope(self) -> None:
        counts = {"dense_seconds": 20, "dense_tokens": 120, "dense_copied": 10,
                  "dense_embedded": 30, "dense_present": 60, "dense_scope": 180,
                  "dense_total": 1000, "dense_limit": 7, "indexed": 9999}
        result = projection(counts, self.now)
        assert result is not None
        self.assertEqual(result.tokens_per_second, counts["dense_tokens"] / counts["dense_seconds"])
        rate = (counts["dense_copied"] + counts["dense_embedded"]) / counts["dense_seconds"]
        self.assertEqual(result.chunks_per_second, rate)
        remaining = counts["dense_scope"] - counts["dense_present"] - counts["dense_copied"] - counts["dense_embedded"]
        self.assertEqual(result.remaining, remaining)
        self.assertEqual(result.finish, self.now + timedelta(seconds=remaining / rate))
        self.assertEqual(result.generation_seconds, counts["dense_total"] / rate)
        self.assertEqual(result.verdict, "inside")
        with self.assertRaises(FrozenInstanceError):
            result.remaining = 0  # type: ignore[misc]

    def test_both_verdicts_and_the_inclusive_tripwire(self) -> None:
        seconds = int(timedelta(days=TRIPWIRE_DAYS).total_seconds())
        for total, verdict in ((seconds - 1, "inside"), (seconds, "inside"), (seconds + 1, "past")):
            with self.subTest(total=total):
                result = projection({"dense_seconds": 1, "dense_embedded": 1, "dense_total": total}, self.now)
                assert result is not None
                self.assertEqual(result.generation_seconds, total)
                self.assertEqual(result.verdict, verdict)

    def test_completed_scope_clamps_remaining_and_copy_only_has_zero_token_rate(self) -> None:
        result = projection({"dense_seconds": 10, "dense_copied": 5, "dense_scope": 4,
                             "dense_present": 4, "dense_total": 50}, self.now)
        assert result is not None
        self.assertEqual(result.remaining, 0)
        self.assertEqual(result.finish, self.now)
        self.assertEqual(result.tokens_per_second, 0)
        self.assertEqual(result.chunks_per_second, 0.5)

    def test_no_elapsed_time_or_no_written_chunks_means_no_projection(self) -> None:
        for counts in ({}, {"seconds": 5, "indexed": 7}, {"dense_seconds": 10},
                       {"dense_seconds": 10, "dense_present": 100, "dense_tokens": 99},
                       {"dense_seconds": 0, "dense_embedded": 3},
                       {"dense_seconds": -1, "dense_copied": 3}):
            with self.subTest(counts=counts):
                self.assertIsNone(projection(counts, self.now))
