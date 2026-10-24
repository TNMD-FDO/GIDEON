"""By-name retirement, refusals, and recovery over a store-less host."""

import argparse
import ast
import contextlib
import copy
import io
import json
import os
import subprocess
import unittest
from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from test_corpus_install import NOW
from test_index_record import NAME, RENDERED, row_for

from gideon.cli import main
from gideon.host import backuplock, report, secrets, stack, worker
from gideon.host.index import lexical, record, retire, sample
from gideon.host.render.ci import CI_ROOT, CI_STACK, PRODUCTION_STACK
from gideon.host.render.worker import WORKER_SERVICE_NAME
from gideon.host.report import StageResult
from gideon.host.sysio import Command, PathLike, RealHost

STAGES = ("preconditions", "candidate", "retire", "next")


class FakeHost(RealHost):
    """Record, queue, and lock state without external I/O or derived stores."""

    def __init__(self, *, sibling: bool = False, state: str = "built") -> None:
        super().__init__()
        self.directory = CI_ROOT if sibling else RENDERED
        self.name = sample.sample_name(NAME) if sibling else NAME
        row = row_for()
        complete = state in {"built", "live", "retired"}
        self.rows = {self.name: replace(
            row, gen_id=self.name, state=state,
            failure_reason="store" if state == "failed" else None,
            dense_built_at=row.lexical_built_at if complete else None,
            built_at=row.lexical_built_at if complete else None,
            promoted_at=NOW.isoformat() if state == "live" else None,
            retired_at=NOW.isoformat() if state == "retired" else None,
        )}
        self.jobs: dict[str, tuple[int, str]] = {}
        self.sibling_job: tuple[int, str] | None = None
        self.euid = 0
        self.files = {str(Path(self.directory) / "compose.yaml"): "fictitious compose"}
        self.reads: list[str] = []
        self.locks: dict[str, str] = {}
        self.lock_paths: list[str] = []
        self.releases: list[str] = []
        self.lock_error = False
        self.calls: list[tuple[list[str], str | None]] = []
        self.events: list[str] = []
        self.failures: dict[str, int] = {}
        self.interrupts: set[str] = set()
        self.before_retire: Callable[[], None] | None = None

    def geteuid(self) -> int:
        return self.euid

    def exists(self, path: PathLike) -> bool:
        return os.fspath(path) in self.files

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        key = os.fspath(path)
        self.reads.append(key)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def mkdir(self, path: PathLike, *, mode: int = 0o755, parents: bool = False, exist_ok: bool = False) -> None:
        assert os.fspath(path) == backuplock.LOCK_DIR
        if self.lock_error:
            raise OSError("fictitious lock directory inaccessible")

    def take_lock(self, path: PathLike, text: str) -> str | None:
        key = os.fspath(path)
        self.lock_paths.append(key)
        if key in self.locks:
            return self.locks[key]
        self.locks[key] = text
        return None

    def release_lock(self, path: PathLike) -> None:
        key = os.fspath(path)
        self.releases.append(key)
        del self.locks[key]

    @staticmethod
    def bound(sql: str, name: str) -> str:
        prefix = f"\\set {name} '"
        return next(line[len(prefix):-1] for line in sql.splitlines() if line.startswith(prefix))

    def event(self, name: str) -> int:
        self.events.append(name)
        if name in self.interrupts:
            raise KeyboardInterrupt
        return self.failures.get(name, 0)

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        assert env is None and input is not None
        command = list(argv)
        self.calls.append((command, input))
        assert command == worker.psql_argv(self.directory), f"unexpected child: {command}"
        return self.psql(command, input)

    def psql(self, argv: list[str], sql: str) -> subprocess.CompletedProcess[str]:
        data: object
        if "WHERE queue_name = :'v_queue'" in sql:
            event = "sibling-job"
            job = self.sibling_job
            data = job[0] if job is not None and job[1] in {"todo", "doing"} else ""
        elif "FROM procrastinate_jobs" in sql:
            lock = self.bound(sql, "v_lock")
            event = f"job:{lock}"
            job = self.jobs.get(lock)
            data = job[0] if job is not None and job[1] in {"todo", "doing"} else ""
        elif "row_to_json(generation)" in sql:
            event = "candidate"
            row = self.rows.get(self.bound(sql, "v_name"))
            data = asdict(row) if row is not None else None
        elif "DO $retirement$" in sql:
            code = self.event("retire")
            if code:
                return subprocess.CompletedProcess(argv, code, "", "fictitious database diagnostic")
            if self.before_retire is not None:
                self.before_retire()
            payload = json.loads(self.bound(sql, "v_payload"))
            candidate = self.rows.get(payload["name"])
            if candidate is None or candidate.state not in {"building", "built", "failed"}:
                return subprocess.CompletedProcess(
                    argv, 3, "", "ERROR: generation is no longer building, built, or failed",
                )
            self.rows[candidate.gen_id] = replace(
                candidate, state="retired", retired_at=NOW.isoformat(), failure_reason=None,
            )
            outcome = record.RetirementOutcome(
                candidate.gen_id, candidate.state, candidate.failure_reason, NOW.isoformat(),
            )
            return subprocess.CompletedProcess(argv, 0, json.dumps(asdict(outcome)), "")
        else:
            raise AssertionError("unexpected database statement")
        code = self.event(event)
        stdout = str(data) if event.startswith("job:") or event == "sibling-job" else "" if data is None else json.dumps(data)
        return subprocess.CompletedProcess(argv, code, stdout, "fictitious database diagnostic" if code else "")


class Retire(unittest.TestCase):
    """Retirement moves only the record; the next sweep owns the stores."""

    def setUp(self) -> None:
        self.host = FakeHost()

    def run_retire(self, name: str | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        before_locks = dict(self.host.locks)
        attempts = len(self.host.lock_paths)
        releases = len(self.host.releases)
        directory = secrets.current_directory()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = retire.run_index_retire(
                argparse.Namespace(gen=self.host.name if name is None else name),
                host=self.host, rendered_dir=RENDERED, clock=lambda: NOW,
            )
        self.assertEqual(self.host.locks, before_locks)
        expected = [path for path in self.host.lock_paths[attempts:] if path not in before_locks]
        self.assertEqual(self.host.releases[releases:], expected)
        self.assertEqual(secrets.current_directory(), directory)
        self.assertEqual(self.host.reads, [])
        return code, out.getvalue(), err.getvalue()

    def refuse(self, stage: str, phrase: str, *, name: str | None = None) -> str:
        code, out, err = self.run_retire(name)
        self.assertEqual((code, err), (1, ""), out)
        line = next(line for line in out.splitlines() if line.startswith(f"{stage}: refuse"))
        problem, fix = line.split(" Fix: ", 1)
        self.assertIn(phrase, problem)
        self.assertIn(report.command(f"index retire {self.host.name if name is None else name}"), fix)
        self.assertNotIn("Next:", out)
        return out

    def assert_untouched(self, before: dict[str, record.GenerationRow]) -> None:
        self.assertEqual(self.host.rows, before)
        self.assertNotIn("retire", self.host.events)
        self.assertFalse(any("UPDATE public.index_generations" in (sql or "") for _, sql in self.host.calls))

    def test_each_eligible_state_retires_with_one_stdin_write_and_names_the_sweep(self) -> None:
        for sibling in (False, True):
            for state in ("building", "built", "failed"):
                with self.subTest(sibling=sibling, state=state):
                    self.host = FakeHost(sibling=sibling, state=state)
                    before = self.host.rows[self.host.name]
                    code, out, err = self.run_retire()
                    self.assertEqual((code, err), (0, ""), out)
                    self.assertEqual([line.split(":", 1)[0] for line in out.splitlines() if not line.startswith("Next:")], list(STAGES))
                    for stage in STAGES:
                        self.assertIn(f"{stage}: ok", out)
                    expected = replace(before, state="retired", retired_at=NOW.isoformat(), failure_reason=None)
                    self.assertEqual(self.host.rows, {self.host.name: expected})
                    writes = [(argv, sql) for argv, sql in self.host.calls if "UPDATE public.index_generations" in (sql or "")]
                    self.assertEqual(len(writes), 1)
                    argv, sql = writes[0]
                    assert sql is not None
                    self.assertEqual(argv, worker.psql_argv(self.host.directory))
                    self.assertIn("ON_ERROR_STOP=1", argv)
                    self.assertTrue(sql.startswith(worker.bind(
                        "v_payload", json.dumps({"name": self.host.name}, separators=(",", ":")),
                    ) + "\n"))
                    self.assertEqual(sql.count("UPDATE public.index_generations"), 1)
                    self.assertIn("SET state = 'retired', retired_at = now(), failure_reason = NULL", sql)
                    for command, statement in self.host.calls:
                        self.assertEqual(command, worker.psql_argv(self.host.directory))
                        for value in (self.host.name, before.corpus_lockfile, "DO $retirement$", "UPDATE"):
                            self.assertNotIn(value, " ".join(command))
                        self.assertIsNotNone(statement)
                    candidate = next(line for line in out.splitlines() if line.startswith("candidate:"))
                    self.assertIn(f"state {state}", candidate)
                    self.assertIn(f"bytes {before.bytes}", candidate)
                    for key, count in before.counts.items():
                        self.assertIn(f"{key} {count}", candidate)
                    reason = f" ({before.failure_reason})" if before.failure_reason is not None else ""
                    if before.failure_reason is not None:
                        self.assertIn(f"state failed{reason}", candidate)
                    self.assertIn(
                        f"retire: ok — {self.host.name}: retired at {NOW.isoformat()} from {state}{reason}; stores untouched", out,
                    )
                    gc_command = "index gc --stack ci" if sibling else "index gc"
                    self.assertIn("never rebuilt in place", out)
                    self.assertIn(f"a new lockfile by {report.command('corpus cut')} is the path", out)
                    self.assertIn(f"Next: run {report.command(gc_command)} to drop its index and collection.", out)
                    lock = backuplock.ENGINE_LOCK if sibling else backuplock.CORPUS_LOCK
                    stack_name = CI_STACK if sibling else PRODUCTION_STACK
                    self.assertEqual(self.host.lock_paths, [lock.path])
                    self.assertIn(f"preconditions: ok — {stack_name}; {lock.noun} lock taken", out)
                    jobs = [f"job:{prefix}{self.host.name}" for prefix in lexical.JOB_SUBJECTS]
                    self.assertEqual(self.host.events, (["sibling-job"] if sibling else []) + jobs + ["candidate", "retire"])
                    self.assertEqual(record.read_generation(self.host, self.host.directory, self.host.name), expected)

    def test_live_generation_refuses_with_promote_fix_without_writing(self) -> None:
        for sibling in (False, True):
            with self.subTest(sibling=sibling):
                self.host = FakeHost(sibling=sibling, state="live")
                before = copy.deepcopy(self.host.rows)
                out = self.refuse("candidate", "generation is live and retires only when another is promoted")
                self.assertIn(report.command("index promote <generation>"), out)
                self.assertIn(f"over a built generation of {before[self.host.name].collection}", out)
                self.assertIn("the live one retires at that flip", out)
                self.assert_untouched(before)

    def test_retired_generation_and_a_successful_rerun_converge_without_writing(self) -> None:
        for sibling in (False, True):
            for already_retired in (False, True):
                with self.subTest(sibling=sibling, already_retired=already_retired):
                    self.host = FakeHost(sibling=sibling, state="retired" if already_retired else "built")
                    if not already_retired:
                        code, out, err = self.run_retire()
                        self.assertEqual((code, err), (0, ""), out)
                        self.host.calls.clear()
                        self.host.events.clear()
                    before = copy.deepcopy(self.host.rows)
                    code, out, err = self.run_retire()
                    self.assertEqual((code, err), (0, ""), out)
                    self.assertIn(f"already retired at {before[self.host.name].retired_at}; nothing to do", out)
                    self.assertIn("retire: ok — already retired; skipped record retirement", out)
                    gc_command = "index gc --stack ci" if sibling else "index gc"
                    self.assertIn(f"Next: run {report.command(gc_command)}", out)
                    self.assert_untouched(before)

    def test_absent_generation_fixes_address_the_record_on_its_own_stack(self) -> None:
        for sibling in (False, True):
            with self.subTest(sibling=sibling):
                self.host = FakeHost(sibling=sibling)
                self.host.rows.clear()
                out = self.refuse("candidate", f"generation {self.host.name} is not recorded")
                if sibling:
                    self.assertIn(report.command("index build caselaw --sample"), out)
                    self.assertNotIn("index report", out)
                else:
                    self.assertIn(report.command("index report"), out)
                self.assert_untouched({})

    def test_bad_names_refuse_with_listing_fix_before_any_lock_or_child(self) -> None:
        for name in ("", "invalid", "invalid__sample", NAME.upper(), NAME + "x" * 255):
            with self.subTest(name=name):
                self.host = FakeHost()
                before = copy.deepcopy(self.host.rows)
                out = self.refuse("preconditions", "generation name is invalid", name=name)
                self.assertIn(report.command("index report"), out)
                self.assertEqual(self.host.lock_paths, [])
                self.assertEqual(self.host.calls, [])
                self.assert_untouched(before)

    def test_root_refusal_is_on_stderr_with_exit_two_before_any_io(self) -> None:
        self.host.euid = 1000
        before = copy.deepcopy(self.host.rows)
        code, out, err = self.run_retire()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("gideon index retire: root privileges are required.", err)
        self.assertIn(f"Fix: Run {report.command(f'index retire {self.host.name}')}", err)
        self.assertEqual(self.host.lock_paths, [])
        self.assertEqual(self.host.calls, [])
        self.assert_untouched(before)

    def test_each_queued_or_running_job_refuses_before_candidate_or_write(self) -> None:
        for sibling in (False, True):
            for prefix, subject in lexical.JOB_SUBJECTS.items():
                for status in ("todo", "doing"):
                    with self.subTest(sibling=sibling, prefix=prefix, status=status):
                        self.host = FakeHost(sibling=sibling)
                        row = self.host.rows[self.host.name]
                        self.host.jobs[prefix + self.host.name] = (73, status)
                        before = copy.deepcopy(self.host.rows)
                        out = self.refuse("preconditions", f"{subject} job 73 is queued or running")
                        build_command = f"index build {row.collection}" + (" --sample" if sibling else "")
                        build_command += f" --lockfile {row.corpus_lockfile}"
                        self.assertIn(f"Wait for the job or rejoin it with {report.command(build_command)}", out)
                        self.assertNotIn("candidate", self.host.events)
                        self.assert_untouched(before)

    def test_sibling_build_refuses_before_generation_job_reads_or_write(self) -> None:
        for status in ("todo", "doing"):
            with self.subTest(status=status):
                self.host = FakeHost(sibling=True)
                row = self.host.rows[self.host.name]
                self.host.sibling_job = (81, status)
                before = copy.deepcopy(self.host.rows)
                out = self.refuse("preconditions", "sample build is running on the sibling (job 81)")
                self.assertIn(report.command(f"index build {row.collection} --sample --lockfile {row.corpus_lockfile}"), out)
                self.assertEqual(self.host.events, ["sibling-job"])
                self.assertEqual(self.host.lock_paths, [backuplock.ENGINE_LOCK.path])
                self.assert_untouched(before)

    def test_lock_refusals_and_nested_holders_preserve_existing_locks(self) -> None:
        for sibling in (False, True):
            for holder in ("foreign", "unreadable", "nested", "inaccessible"):
                with self.subTest(sibling=sibling, holder=holder):
                    self.host = FakeHost(sibling=sibling)
                    lock = backuplock.ENGINE_LOCK if sibling else backuplock.CORPUS_LOCK
                    if holder == "inaccessible":
                        self.host.lock_error = True
                    else:
                        self.host.locks[lock.path] = "unreadable" if holder == "unreadable" else backuplock.Record(
                            "fictitious lock holder", os.getpid() if holder == "nested" else os.getpid() + 1000, NOW,
                        ).to_json()
                    if sibling and holder == "nested":
                        code, out, err = self.run_retire()
                        self.assertEqual((code, err), (0, ""), out)
                        self.assertIn("engine lock held by this process", out)
                    else:
                        before = copy.deepcopy(self.host.rows)
                        phrase = "could not be taken" if holder == "inaccessible" else "already running in this process" if holder == "nested" else "record unreadable" if holder == "unreadable" else "fictitious lock holder"
                        self.refuse("preconditions", phrase)
                        self.assert_untouched(before)
                    self.assertEqual(self.host.releases, [])

    def test_record_and_job_failures_refuse_with_their_log_fix(self) -> None:
        for sibling in (False, True):
            for event in ("candidate", "retire", "sibling-job", *(f"job:{prefix}" for prefix in lexical.JOB_SUBJECTS)):
                if event == "sibling-job" and not sibling:
                    continue
                with self.subTest(sibling=sibling, event=event):
                    self.host = FakeHost(sibling=sibling)
                    key = event + self.host.name if event.startswith("job:") else event
                    self.host.failures[key] = 3
                    before = copy.deepcopy(self.host.rows)
                    stage = "candidate" if event == "candidate" else "retire" if event == "retire" else "preconditions"
                    out = self.refuse(stage, "exit 3")
                    service = WORKER_SERVICE_NAME if event.startswith("job:") else "postgres"
                    self.assertIn(stack.logs_fix(self.host.directory, service), out)
                    self.assertEqual(self.host.events.count(key), 1)
                    self.assertEqual(self.host.rows, before)
                    if event != "retire":
                        self.assert_untouched(before)

    def test_race_refuses_without_retiring_the_changed_row_and_uses_its_stack_fix(self) -> None:
        for sibling in (False, True):
            with self.subTest(sibling=sibling):
                self.host = FakeHost(sibling=sibling)
                changed = replace(self.host.rows[self.host.name], state="live", promoted_at=NOW.isoformat())

                def promote_candidate(changed: record.GenerationRow = changed) -> None:
                    self.host.rows[self.host.name] = changed

                self.host.before_retire = promote_candidate
                out = self.refuse("retire", "generation is no longer building, built, or failed; nothing was retired")
                self.assertEqual(self.host.rows, {self.host.name: changed})
                self.assertEqual(self.host.events.count("retire"), 1)
                if sibling:
                    self.assertNotIn("index report", out)
                    self.assertIn("again to read its state and confirm retirement", out)
                else:
                    self.assertIn(report.command(f"index report {self.host.name}"), out)

    def test_interrupts_in_each_stage_release_locks_and_report_the_write_boundary(self) -> None:
        print_stage = report.print_stage
        for sibling in (False, True):
            for stage in STAGES:
                with self.subTest(sibling=sibling, stage=stage):
                    self.host = FakeHost(sibling=sibling)
                    if stage == "preconditions":
                        self.host.interrupts.add("sibling-job" if sibling else f"job:index-{self.host.name}")
                    elif stage in {"candidate", "retire"}:
                        self.host.interrupts.add(stage)

                    def interrupt_next(row: StageResult, stage: str = stage) -> None:
                        if stage == "next" and row.name == "next" and row.ok:
                            raise KeyboardInterrupt
                        print_stage(row)

                    expected = (
                        "interrupted; the generation is retired" if stage == "next" else
                        "interrupted during retirement; read the record before retrying" if stage == "retire" else
                        "interrupted; nothing was retired"
                    )
                    with patch.object(report, "print_stage", side_effect=interrupt_next):
                        out = self.refuse(stage, expected)
                    self.assertEqual(self.host.rows[self.host.name].state, "retired" if stage == "next" else "built")
                    self.assertEqual(len(self.host.releases), 1)
                    if sibling:
                        self.assertNotIn("index report", out)
                    else:
                        self.assertIn(report.command(f"index report {self.host.name}"), out)

    def test_cli_dispatches_the_name_and_returns_the_guarded_runner_code(self) -> None:
        with patch.object(retire, "run_index_retire", return_value=3) as run:
            self.assertEqual(main(["index", "retire", NAME]), 3)
        run.assert_called_once()
        args = run.call_args.args[0]
        self.assertEqual(args.gen, NAME)
        self.assertEqual(args.command_path, "index retire")

    def test_cli_boundary_turns_unexpected_errors_into_a_refusal(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(retire, "run_index_retire", side_effect=ValueError("fictitious defect")),
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err),
        ):
            self.assertEqual(main(["index", "retire", NAME]), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("gideon index retire: internal error: ValueError: fictitious defect", err.getvalue())
        self.assertIn("Fix:", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_module_has_no_evaluation_or_worker_import_at_any_depth(self) -> None:
        tree = ast.parse(Path(retire.__file__).read_text())
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                imports.extend((base, *(f"{base}.{alias.name}" for alias in node.names)))
        self.assertFalse(any(
            name == prefix or name.startswith(prefix + ".")
            for name in imports for prefix in ("gideon.evaluation", "gideon.worker")
        ), imports)
