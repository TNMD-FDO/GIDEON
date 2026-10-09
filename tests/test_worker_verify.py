"""The host worker command over a recording Compose and psql seam."""

import argparse
import contextlib
import io
import json
import os
import subprocess
import time
import unittest
from collections.abc import Callable, Mapping

from gideon.host import report, stack, worker
from gideon.host.render import worker as worker_identity
from gideon.host.sysio import Command, PathLike, RealHost

RENDERED = "/rendered"
JOB_ID = 17


class FakeHost(RealHost):
    def __init__(
        self,
        *,
        euid: int = 0,
        declared: bool = True,
        state: str = "running",
        health: str = "healthy",
        ps_code: int = 0,
        enqueue_output: str = f"{JOB_ID}\n",
        enqueue_code: int = 0,
        job_rows: tuple[str, ...] = (f"{JOB_ID}|succeeded|1\n",),
        read_code: int = 0,
    ) -> None:
        super().__init__()
        self.euid = euid
        self.declared = declared
        self.state = state
        self.health = health
        self.ps_code = ps_code
        self.enqueue_output = enqueue_output
        self.enqueue_code = enqueue_code
        self.job_rows = job_rows
        self.read_code = read_code
        self.reads = 0
        self.calls: list[tuple[list[str], str | None, Mapping[str, str] | None]] = []

    def geteuid(self) -> int:
        return self.euid

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        if str(path) != f"{RENDERED}/compose.yaml":
            raise FileNotFoundError(str(path))
        service = worker_identity.WORKER_SERVICE_NAME if self.declared else "example"
        return f"services:\n  {service}: {{}}\n"

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
        del check, cwd, timeout, passthrough
        command = list(argv)
        self.calls.append((command, input, env))
        if command == stack.compose_argv(RENDERED, "ps", "--all", "--format", "json"):
            row = {
                "Service": worker_identity.WORKER_SERVICE_NAME,
                "State": self.state,
                "Health": self.health,
            }
            return subprocess.CompletedProcess(command, self.ps_code, json.dumps(row), "")
        if input is None:
            raise AssertionError(f"unexpected command: {command}")
        if "procrastinate_defer_jobs_v1" in input:
            return subprocess.CompletedProcess(
                command, self.enqueue_code, self.enqueue_output, "argument-sentinel"
            )
        self.reads += 1
        row_text = self.job_rows[min(self.reads - 1, len(self.job_rows) - 1)]
        return subprocess.CompletedProcess(command, self.read_code, row_text, "argument-sentinel")


def run_command(
    host: FakeHost,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = worker.run_worker_verify(
            argparse.Namespace(),
            host=host,
            rendered_dir=RENDERED,
            clock=clock,
            sleep=sleep,
        )
    return code, out.getvalue(), err.getvalue()


class WorkerVerify(unittest.TestCase):
    def test_root_and_no_worker_fixes_follow_the_invoked_form(self) -> None:
        self.addCleanup(report.set_form_from_environment, os.environ.copy())
        for installed in (False, True):
            with self.subTest(installed=installed):
                report.set_installed_form(installed)
                command = "gideon" if installed else "sudo python3 -m gideon"
                for host, expected in (
                    (
                        FakeHost(euid=1000),
                        f"preconditions: refuse — root privileges are required Fix: Run {command} worker verify, then retry.\n",
                    ),
                    (
                        FakeHost(declared=False),
                        f"preconditions: refuse — rendered stack has no worker Fix: Run {command} apply, then retry.\n",
                    ),
                ):
                    with self.subTest(row=expected):
                        code, out, err = run_command(host)
                        self.assertEqual((code, out), (1, ""))
                        self.assertEqual(err, expected)
                        if installed:
                            self.assertNotIn("python3 -m gideon", err)

    def test_success_reports_three_stages_and_uses_exact_commands(self) -> None:
        host = FakeHost(
            job_rows=(
                f"{JOB_ID}|todo|0\n",
                f"{JOB_ID}|doing|0\n",
                f"{JOB_ID}|succeeded|1\n",
            )
        )
        now = [0.0]
        sleeps: list[float] = []

        def clock() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        code, out, err = run_command(host, clock=clock, sleep=sleep)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            [line.split(":", 1)[0] for line in out.splitlines()],
            ["preconditions", "enqueue", "run"],
        )
        self.assertIn(f"job {JOB_ID} on {worker_identity.WORKER_VERIFY_QUEUE}", out)
        self.assertIn(f"job {JOB_ID}: succeeded, attempts 1, 2.0s", out)
        self.assertEqual(sleeps, [1.0, 1.0])
        ps = stack.compose_argv(RENDERED, "ps", "--all", "--format", "json")
        psql = stack.exec_argv(
            RENDERED,
            "postgres",
            "psql",
            "-U",
            worker_identity.WORKER_ROLE,
            "-d",
            worker_identity.WORKER_DATABASE_NAME,
            "-tA",
            "-F",
            "|",
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            "-",
        )
        self.assertEqual([call[0] for call in host.calls], [ps, psql, psql, psql, psql])
        self.assertTrue(all(call[2] is None for call in host.calls))
        enqueue_sql = host.calls[1][1]
        assert enqueue_sql is not None
        self.assertIn(
            "ROW(:'v_queue', :'v_task', 0, NULL, NULL, :'v_args'::jsonb, NULL)",
            enqueue_sql,
            "Fix: keep the defer composite in the installed schema's field order.",
        )
        self.assertIn("\\set v_queue 'verify'", enqueue_sql)
        self.assertIn("\\set v_task 'gideon.worker.tasks.verify'", enqueue_sql)
        self.assertIn("\\set v_args '{}'", enqueue_sql)
        self.assertIn("procrastinate_defer_jobs_v1", enqueue_sql)
        self.assertTrue(all("WHERE id = :'v_job_id'::bigint" in call[1] for call in host.calls[2:] if call[1]))
        self.assertNotIn("argument-sentinel", out + err)
        for argv, _, _ in host.calls:
            self.assertNotIn("argument-sentinel", " ".join(argv))

    def test_hold_is_bound_on_stdin_and_absent_from_argv(self) -> None:
        host = FakeHost()
        result = worker.defer_job(host, RENDERED, hold=47)
        self.assertEqual(result, JOB_ID)
        argv, sql, env = host.calls[0]
        assert sql is not None
        self.assertIn("\\set v_args '{\"hold_seconds\":47}'", sql)
        self.assertNotIn("47", " ".join(argv))
        self.assertIsNone(env)

    def test_precondition_refusals_print_only_to_stderr_with_fixes(self) -> None:
        for host, phrase, fix in (
            (FakeHost(euid=1000), "root privileges", "sudo python3 -m gideon worker verify"),
            (FakeHost(declared=False), "no worker", "sudo python3 -m gideon apply"),
            (FakeHost(state="exited"), "not running", "logs gideon-worker"),
            (FakeHost(health="unhealthy"), "not running and healthy", "logs gideon-worker"),
            (FakeHost(ps_code=1), "could not read worker service state", "logs gideon-worker"),
        ):
            with self.subTest(phrase=phrase):
                code, out, err = run_command(host)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("preconditions: refuse", err)
                self.assertIn(phrase, err)
                self.assertIn(fix, err)
                self.assertIn("Fix:", err)

    def test_enqueue_refusals_have_a_fix_and_never_echo_psql_output(self) -> None:
        for host in (
            FakeHost(enqueue_code=1),
            FakeHost(enqueue_output="not-an-id"),
        ):
            with self.subTest(host=host):
                code, out, err = run_command(host)
                self.assertEqual(code, 1)
                self.assertEqual([line.split(":", 1)[0] for line in out.splitlines()], ["preconditions"])
                self.assertIn("enqueue: refuse", err)
                self.assertIn("Fix:", err)
                self.assertNotIn("argument-sentinel", out + err)

    def test_terminal_statuses_and_read_failure(self) -> None:
        for status, expected_code in (
            ("succeeded", 0),
            ("failed", 1),
            ("aborted", 1),
            ("cancelled", 1),
        ):
            with self.subTest(status=status):
                host = FakeHost(job_rows=(f"{JOB_ID}|{status}|2\n",))
                code, out, err = run_command(host)
                self.assertEqual((code, err), (expected_code, ""))
                self.assertIn(f"run: {'ok' if expected_code == 0 else 'refuse'}", out)
                self.assertIn(f"{status}, attempts 2", out)
                if expected_code:
                    self.assertIn("Fix:", out)
        host = FakeHost(read_code=1)
        code, out, err = run_command(host)
        self.assertEqual((code, err), (1, ""))
        self.assertIn("run: refuse", out)
        self.assertIn("Fix:", out)
        self.assertNotIn("argument-sentinel", out)

    def test_timeout_says_the_worker_may_be_busy(self) -> None:
        host = FakeHost(job_rows=(f"{JOB_ID}|doing|0\n",))
        now = [0.0]

        def clock() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        code, out, err = run_command(host, clock=clock, sleep=sleep)
        self.assertEqual((code, err), (1, ""))
        self.assertIn("run: refuse", out)
        self.assertIn("worker may be busy", out)
        self.assertIn("Fix:", out)
        self.assertEqual(now[0], worker.POLL_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
