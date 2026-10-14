"""Lexical job arguments, queue lookups, and recorded failures on the host."""

import json
import shlex
import subprocess
import unittest
from collections.abc import Mapping
from dataclasses import asdict, replace

from test_index_record import NAME, RENDERED, FakeHost, inputs_for, row_for

from gideon.host import caselaw, report, stack, worker
from gideon.host.index.lexical import BuildRead, defer_lexical, live_job, read_lexical
from gideon.host.render.opensearch import (
    OPENSEARCH_PASSWORD_SECRET_NAME,
    OPENSEARCH_SERVICE_NAME,
)
from gideon.host.render.worker import (
    DENSE_LOCK_PREFIX,
    LEXICAL_FAILURE_REASONS,
    LEXICAL_QUEUE,
    LEXICAL_TASK,
    WORKER_SERVICE_NAME,
)
from gideon.host.report import Problem
from gideon.host.sysio import Command, PathLike

JOB_ID = 31


class QueueHost(FakeHost):
    """Serve the queue and generation from memory; no failure files exist."""

    def __init__(self) -> None:
        super().__init__()
        self.status = "succeeded"
        self.live_output = f"{JOB_ID}\n"
        self.job_output: str | None = None
        self.generation_output = json.dumps(asdict(row_for()))
        self.generation_code = 0

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        assert input is not None
        if "procrastinate_defer_jobs_v1" in input:
            self.stdout = f"{JOB_ID}\n"
        elif "SELECT id, status, attempts" in input:
            self.stdout = self.job_output if self.job_output is not None else f"{JOB_ID}|{self.status}|1\n"
        elif "SELECT id FROM procrastinate_jobs" in input:
            self.stdout = self.live_output
        else:
            self.stdout = self.generation_output
        result = super().run(argv, check=check, input=input, cwd=cwd, env=env, timeout=timeout, passthrough=passthrough)
        if "public.index_generations" in input and self.generation_code:
            return subprocess.CompletedProcess(result.args, self.generation_code, result.stdout, result.stderr)
        return result

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        raise AssertionError("lexical failures must come from the generation row")

    def exists(self, path: PathLike) -> bool:
        raise AssertionError("lexical failures must come from the generation row")


class Defer(unittest.TestCase):
    def test_arguments_are_ids_and_geography_with_values_only_on_stdin(self) -> None:
        inputs = inputs_for()
        host = QueueHost()
        self.assertEqual(defer_lexical(host, RENDERED, inputs), JOB_ID)
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
        self.assertIn(worker.bind("v_task", LEXICAL_TASK), sql)
        self.assertIn(worker.bind("v_queue", LEXICAL_QUEUE), sql)
        self.assertIn(worker.bind("v_lock", f"index-{inputs.name}"), sql)
        self.assertIn(":'v_lock'", sql)
        for value in (inputs.name, inputs.label, inputs.snapshot, inputs.inputs_hash, LEXICAL_TASK, LEXICAL_QUEUE):
            self.assertNotIn(value, argv)

    def test_a_found_generation_keeps_its_earlier_name_with_a_new_label(self) -> None:
        inputs = replace(inputs_for(), label="corpus-2099-02-03")
        self.assertEqual(defer_lexical(QueueHost(), RENDERED, inputs), JOB_ID)

    def test_every_argument_grammar_refuses_before_queueing(self) -> None:
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
                invalid_inputs = replace(inputs, **change)  # type: ignore[arg-type]
                result = defer_lexical(host, RENDERED, invalid_inputs)
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn(report.command("index build"), result.fix)
                self.assertEqual(host.calls, [])

    def test_defer_failure_is_a_refusal(self) -> None:
        host = QueueHost()
        host.code = 1
        result = defer_lexical(host, RENDERED, inputs_for())
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("exit 1", result.problem)
        self.assertEqual(len(host.calls), 1)


class LiveJob(unittest.TestCase):
    def test_dense_prefix_binds_its_lock_without_values_in_argv(self) -> None:
        host = QueueHost()
        lock = f"{DENSE_LOCK_PREFIX}{NAME}"
        self.assertEqual(live_job(host, RENDERED, lock, prefix=DENSE_LOCK_PREFIX), JOB_ID)
        argv, sql = host.calls[0]
        self.assertEqual(argv, worker.psql_argv(RENDERED))
        self.assertNotIn(lock, " ".join(argv))
        assert sql is not None
        self.assertIn(worker.bind("v_lock", lock), sql)
        self.assertIn("status IN ('todo', 'doing')", sql)

    def test_prefix_refusals_use_the_dense_subject(self) -> None:
        for lock in (f"index-{NAME}", "dense-invalid", f"{DENSE_LOCK_PREFIX}{NAME};"):
            with self.subTest(lock=lock):
                host = QueueHost()
                result = live_job(host, RENDERED, lock, prefix=DENSE_LOCK_PREFIX)
                assert isinstance(result, Problem)
                self.assertIn("dense", result.problem)
                self.assertEqual(host.calls, [])
        host = QueueHost()
        self.assertIsInstance(live_job(host, RENDERED, f"{DENSE_LOCK_PREFIX}{NAME}"), Problem)
        self.assertEqual(host.calls, [])

    def test_dense_query_failure_is_not_retried(self) -> None:
        for code, error in ((3, None), (127, None), (0, FileNotFoundError())):
            with self.subTest(code=code, error=error):
                host = QueueHost()
                host.code, host.error = code, error
                result = live_job(host, RENDERED, f"{DENSE_LOCK_PREFIX}{NAME}", prefix=DENSE_LOCK_PREFIX)
                assert isinstance(result, Problem)
                self.assertIn("dense", result.problem)
                self.assertIn(report.command("index build"), result.fix)
                self.assertEqual(len(host.calls), 1)

    def test_lookup_binds_the_lock_and_only_selects_live_statuses(self) -> None:
        host = QueueHost()
        lock = f"index-{NAME}"
        self.assertEqual(live_job(host, RENDERED, lock), JOB_ID)
        argv, sql = host.calls[0]
        self.assertEqual(argv, worker.psql_argv(RENDERED))
        self.assertNotIn(lock, " ".join(argv))
        assert sql is not None
        self.assertIn(worker.bind("v_lock", lock), sql)
        self.assertIn("lock = :'v_lock'", sql)
        self.assertIn("status IN ('todo', 'doing')", sql)
        self.assertIn("ORDER BY id LIMIT 1", sql)

    def test_absent_live_job_returns_none(self) -> None:
        host = QueueHost()
        host.live_output = "\n"
        self.assertIsNone(live_job(host, RENDERED, f"index-{NAME}"))

    def test_invalid_lock_refuses_before_any_statement(self) -> None:
        for lock in (NAME, "index-invalid", f"other-{NAME}", f"index-{NAME};"):
            with self.subTest(lock=lock):
                host = QueueHost()
                self.assertIsInstance(live_job(host, RENDERED, lock), Problem)
                self.assertEqual(host.calls, [])

    def test_malformed_id_or_failed_exit_refuses(self) -> None:
        for output in ("0", "-1", "bad", "31\n32", "٣١", "31|doing"):
            with self.subTest(output=output):
                host = QueueHost()
                host.live_output = output
                self.assertIsInstance(live_job(host, RENDERED, f"index-{NAME}"), Problem)
        host = QueueHost()
        host.code, host.stderr = 3, "private text sentinel"
        result = live_job(host, RENDERED, f"index-{NAME}")
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("exit 3", result.problem)
        self.assertNotIn("private text sentinel", result.problem + result.fix)
        self.assertIn(stack.logs_fix(RENDERED, WORKER_SERVICE_NAME), result.fix)

    def test_unavailable_tool_is_not_retried(self) -> None:
        for code, error in ((127, None), (0, FileNotFoundError()), (0, subprocess.SubprocessError())):
            with self.subTest(code=code, error=error):
                host = QueueHost()
                host.code, host.error = code, error
                result = live_job(host, RENDERED, f"index-{NAME}")
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("Restore Docker Compose and psql", result.fix)
                self.assertEqual(len(host.calls), 1)


class Read(unittest.TestCase):
    def test_waiting_and_success_do_not_read_failure_records(self) -> None:
        for status in ("todo", "doing", "succeeded"):
            with self.subTest(status=status):
                host = QueueHost()
                host.status = status
                result = read_lexical(host, RENDERED, JOB_ID, NAME)
                self.assertEqual(result, BuildRead(worker.JobRow(JOB_ID, status, 1), status == "succeeded", None, None))
                self.assertEqual(len(host.calls), 1)

    def test_every_failure_reason_comes_from_the_row_and_has_its_fix(self) -> None:
        for reason in LEXICAL_FAILURE_REASONS:
            with self.subTest(reason=reason):
                host = QueueHost()
                host.status = "failed"
                host.generation_output = json.dumps(asdict(replace(row_for(), state="failed", failure_reason=reason)))
                result = read_lexical(host, RENDERED, JOB_ID, NAME)
                self.assertIsInstance(result, BuildRead)
                assert isinstance(result, BuildRead) and result.failure is not None
                self.assertFalse(result.done)
                self.assertEqual(result.reason, reason)
                self.assertEqual(result.job.id, JOB_ID)
                self.assertEqual(len(host.calls), 2)
                self.assertIn(report.command("index build"), result.failure.fix)
                if reason in {"database", "local", "invalid"}:
                    self.assertIn(stack.logs_fix(RENDERED, WORKER_SERVICE_NAME), result.failure.fix)
                elif reason == "store":
                    self.assertIn(stack.logs_fix(RENDERED, OPENSEARCH_SERVICE_NAME), result.failure.fix)
                    self.assertIn(report.command("apply"), result.failure.fix)
                elif reason == "busy":
                    self.assertIn("Wait", result.failure.fix)
                elif reason in {"text-mismatch", "missing-text"}:
                    self.assertIn(report.command("restore --from staging"), result.failure.fix)
                elif reason == "mapping-mismatch":
                    self.assertNotIn("index gc", result.failure.fix)
                    command = result.failure.fix.removeprefix("Remove the derived index with ").split(", then run ", 1)[0]
                    argv = shlex.split(command)
                    self.assertEqual(argv[:-1], stack.exec_argv(RENDERED, OPENSEARCH_SERVICE_NAME, "sh", "-c"))
                    self.assertIn("--request DELETE", argv[-1])
                    self.assertIn("--config -", argv[-1])
                    self.assertIn(NAME, argv[-1])
                    self.assertIn(f"/run/secrets/{OPENSEARCH_PASSWORD_SECRET_NAME}", argv[-1])
                    self.assertNotIn("curl --user", argv[-1])

    def test_all_terminal_statuses_use_the_generation_failure(self) -> None:
        for status in ("failed", "aborted", "cancelled"):
            with self.subTest(status=status):
                host = QueueHost()
                host.status = status
                host.generation_output = json.dumps(asdict(replace(row_for(), state="failed", failure_reason="busy")))
                result = read_lexical(host, RENDERED, JOB_ID, NAME)
                self.assertIsInstance(result, BuildRead)
                assert isinstance(result, BuildRead)
                self.assertEqual(result.reason, "busy")

    def test_terminal_status_without_failed_generation_uses_local(self) -> None:
        for status in ("failed", "aborted", "cancelled"):
            for output in ("", json.dumps(asdict(row_for()))):
                with self.subTest(status=status, output=output):
                    host = QueueHost()
                    host.status, host.generation_output = status, output
                    result = read_lexical(host, RENDERED, JOB_ID, NAME)
                    self.assertIsInstance(result, BuildRead)
                    assert isinstance(result, BuildRead) and result.failure is not None
                    self.assertEqual(result.reason, "local")
                    self.assertIn(stack.logs_fix(RENDERED, WORKER_SERVICE_NAME), result.failure.fix)

    def test_invalid_name_unknown_status_and_failed_reads_refuse(self) -> None:
        host = QueueHost()
        self.assertIsInstance(read_lexical(host, RENDERED, JOB_ID, "invalid"), Problem)
        self.assertEqual(host.calls, [])
        host.status = "unknown"
        self.assertIsInstance(read_lexical(host, RENDERED, JOB_ID, NAME), Problem)
        for output in ("", "31|failed", "32|failed|1", "31|failed|bad"):
            with self.subTest(output=output):
                host = QueueHost()
                host.job_output = output
                self.assertIsInstance(read_lexical(host, RENDERED, JOB_ID, NAME), Problem)
        for generation_code, generation_output in ((1, json.dumps(asdict(row_for()))), (0, "broken")):
            with self.subTest(generation_code=generation_code):
                host = QueueHost()
                host.status = "failed"
                host.generation_code, host.generation_output = generation_code, generation_output
                self.assertIsInstance(read_lexical(host, RENDERED, JOB_ID, NAME), Problem)
        host = QueueHost()
        host.code = 1
        self.assertIsInstance(read_lexical(host, RENDERED, JOB_ID, NAME), Problem)
        self.assertEqual(len(host.calls), 1)

    def test_failure_fixes_follow_the_installed_command_form(self) -> None:
        report.set_installed_form(True)
        self.addCleanup(report.set_installed_form, False)
        for reason in LEXICAL_FAILURE_REASONS:
            with self.subTest(reason=reason):
                host = QueueHost()
                host.status = "failed"
                host.generation_output = json.dumps(asdict(replace(row_for(), state="failed", failure_reason=reason)))
                result = read_lexical(host, RENDERED, JOB_ID, NAME)
                assert isinstance(result, BuildRead) and result.failure is not None
                self.assertIn("gideon index build", result.failure.fix)
                self.assertNotIn("python3 -m gideon", result.failure.fix)
