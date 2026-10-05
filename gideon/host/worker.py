"""Enqueue one worker job and read its queue row back."""

import argparse
import json
import math
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, cast

from gideon.host import stack
from gideon.host.render.worker import (
    WORKER_DATABASE_NAME,
    WORKER_ROLE,
    WORKER_SERVICE_NAME,
    WORKER_VERIFY_QUEUE,
    WORKER_VERIFY_TASK,
)
from gideon.host.report import Problem, StageResult, Timeout, stage_line
from gideon.host.stages import run_stage
from gideon.host.sysio import Host, PathLike, RealHost

_RENDERED_DIR: Final[str] = "/etc/gideon/rendered"
_ROOT_FIX: Final[str] = "Run sudo python3 -m gideon worker verify."
_APPLY_FIX: Final[str] = "Run sudo python3 -m gideon apply, then retry."
# exempt: a free worker polls within five seconds; one minute allows queue work.
POLL_TIMEOUT_SECONDS: Final[int] = 60


@dataclass(frozen=True, slots=True)
class JobRow:
    """The queue fields this command reports for one job."""

    id: int
    status: str
    attempts: int


class _CapturedRun:
    """Keep command output for parsing while the stage sees only its exit code."""

    def __init__(self, host: Host) -> None:
        self.host = host
        self.result: subprocess.CompletedProcess[str] | None = None

    def run(
        self,
        argv: Sequence[str],
        *,
        input: str | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self.host.run(argv, input=input, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise OSError(type(exc).__name__) from None
        self.result = result
        diagnostic = f"exit {result.returncode}" if result.returncode else ""
        return subprocess.CompletedProcess(result.args, result.returncode, "", diagnostic)


def _command_stage(
    host: Host,
    name: str,
    argv: Sequence[str],
    detail: str,
    fix: str,
    *,
    input: str | None = None,
) -> tuple[StageResult, subprocess.CompletedProcess[str] | None]:
    capture = _CapturedRun(host)
    stage = run_stage(cast(Host, capture), name, argv, detail, fix, input=input)
    return stage, capture.result


def _logs_fix(rendered_dir: PathLike) -> str:
    return f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, then retry."


def _psql_argv(rendered_dir: PathLike) -> list[str]:
    return stack.exec_argv(
        rendered_dir,
        "postgres",
        "psql",
        "-U",
        WORKER_ROLE,
        "-d",
        WORKER_DATABASE_NAME,
        "-tA",
        "-F",
        "|",
        "-v",
        "ON_ERROR_STOP=1",
        "-f",
        "-",
    )


def _bind(name: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "''")
    return f"\\set {name} '{escaped}'"


def defer_job(
    host: Host, rendered_dir: PathLike, *, hold: float | None = None
) -> int | Problem:
    """Defer one verify job through the worker's database role."""

    if hold is not None and (isinstance(hold, bool) or not math.isfinite(hold) or hold < 0):
        return Problem(
            "the hold duration is invalid",
            "Use a nonnegative finite number of seconds, then retry.",
        )
    args = {} if hold is None else {"hold_seconds": hold}
    sql = "\n".join(
        (
            _bind("v_queue", WORKER_VERIFY_QUEUE),
            _bind("v_task", WORKER_VERIFY_TASK),
            _bind("v_args", json.dumps(args, separators=(",", ":"))),
            "SELECT (procrastinate_defer_jobs_v1(ARRAY[",
            "  ROW(:'v_queue', :'v_task', 0, NULL, NULL, :'v_args'::jsonb, NULL)",
            "]::procrastinate_job_to_defer_v1[]))[1];",
            "",
        )
    )
    stage, result = _command_stage(
        host,
        "enqueue",
        _psql_argv(rendered_dir),
        "could not enqueue a worker job",
        _logs_fix(rendered_dir),
        input=sql,
    )
    if not stage.ok or result is None:
        return Problem(stage.detail, stage.fix)
    value = result.stdout.strip()
    if not value.isascii() or not value.isdecimal() or int(value) < 1:
        return Problem("queue defer returned no job id", _logs_fix(rendered_dir))
    return int(value)


def read_job(host: Host, rendered_dir: PathLike, job_id: int) -> JobRow | Problem:
    """Read the status and attempt count of one queued job."""

    sql = (
        _bind("v_job_id", str(job_id))
        + "\nSELECT id, status, attempts FROM procrastinate_jobs "
        "WHERE id = :'v_job_id'::bigint;\n"
    )
    stage, result = _command_stage(
        host,
        "run",
        _psql_argv(rendered_dir),
        "could not read the worker job row",
        _logs_fix(rendered_dir),
        input=sql,
    )
    if not stage.ok or result is None:
        return Problem(stage.detail, stage.fix)
    parts = result.stdout.strip().split("|")
    if len(parts) != 3:
        return Problem("worker job row is missing or invalid", _logs_fix(rendered_dir))
    row_id, status, attempts = parts
    if (
        not row_id.isascii()
        or not row_id.isdecimal()
        or int(row_id) != job_id
        or not attempts.isascii()
        or not attempts.isdecimal()
    ):
        return Problem("worker job row is invalid", _logs_fix(rendered_dir))
    return JobRow(job_id, status, int(attempts))


def _preconditions(host: Host, rendered_dir: PathLike) -> StageResult:
    if host.geteuid() != 0:
        return StageResult("preconditions", False, "root privileges are required", _ROOT_FIX)
    declared = stack.declared_services(host, rendered_dir)
    if isinstance(declared, Problem):
        return StageResult("preconditions", False, declared.problem, declared.fix)
    if WORKER_SERVICE_NAME not in declared:
        return StageResult(
            "preconditions", False, "rendered stack has no worker", _APPLY_FIX
        )
    stage, result = _command_stage(
        host,
        "preconditions",
        stack.compose_argv(rendered_dir, "ps", "--all", "--format", "json"),
        "could not read worker service state",
        _logs_fix(rendered_dir),
    )
    if not stage.ok or result is None:
        return stage
    rows = stack.parse_ps(result.stdout)
    if rows is None:
        return StageResult(
            "preconditions", False, "worker service state is invalid", _logs_fix(rendered_dir)
        )
    if not any(
        row.get("Service") == WORKER_SERVICE_NAME
        and row.get("State") == "running"
        and row.get("Health") == "healthy"
        for row in rows
    ):
        return StageResult(
            "preconditions", False, "worker is not running and healthy", _logs_fix(rendered_dir)
        )
    return StageResult("preconditions", True, "root and healthy worker are ready", "")


def _wait_for_job(
    host: Host,
    rendered_dir: PathLike,
    job_id: int,
    *,
    hold: float | None,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[JobRow, float] | Problem:
    started = clock()
    bound = POLL_TIMEOUT_SECONDS + (hold or 0)
    while True:
        row = read_job(host, rendered_dir, job_id)
        if isinstance(row, Problem):
            return row
        elapsed = max(0.0, clock() - started)
        if row.status in {"succeeded", "failed", "aborted", "cancelled"}:
            return row, elapsed
        if row.status not in {"todo", "doing", "aborting"}:
            return Problem("worker job has an unknown status", _logs_fix(rendered_dir))
        if elapsed >= bound:
            return Timeout(
                "worker job did not finish before the bound; the worker may be busy",
                _logs_fix(rendered_dir),
            )
        sleep(min(1.0, bound - elapsed))


def run_worker_verify(
    args: argparse.Namespace,
    *,
    host: Host | None = None,
    rendered_dir: PathLike = _RENDERED_DIR,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Print three ordered stages for a job deferred and read through the queue."""

    del args
    io = host if host is not None else RealHost()
    preconditions = _preconditions(io, rendered_dir)
    if not preconditions.ok:
        print(stage_line(preconditions), file=sys.stderr)
        return 1
    print(stage_line(preconditions))

    job_id = defer_job(io, rendered_dir)
    if isinstance(job_id, Problem):
        print(stage_line(StageResult("enqueue", False, job_id.problem, job_id.fix)), file=sys.stderr)
        return 1
    print(stage_line(StageResult("enqueue", True, f"job {job_id} on {WORKER_VERIFY_QUEUE}", "")))

    outcome = _wait_for_job(
        io, rendered_dir, job_id, hold=None, clock=clock, sleep=sleep
    )
    if isinstance(outcome, Problem):
        print(stage_line(StageResult("run", False, outcome.problem, outcome.fix)))
        return 1
    row, elapsed = outcome
    detail = f"job {row.id}: {row.status}, attempts {row.attempts}, {elapsed:.1f}s"
    if row.status != "succeeded":
        print(stage_line(StageResult("run", False, detail, _logs_fix(rendered_dir))))
        return 1
    print(stage_line(StageResult("run", True, detail, "")))
    return 0
