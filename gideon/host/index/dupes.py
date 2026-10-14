"""Validate and defer clustering jobs, and read their failures with fixes."""

from gideon.host import report, worker
from gideon.host.index import lexical
from gideon.host.index.generations import GenerationInputs
from gideon.host.render.worker import DUPES_LOCK_PREFIX, DUPES_QUEUE, DUPES_TASK
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike

COMMAND_PATH = "index build"


def defer_dupes(
    host: Host, rendered_dir: PathLike, inputs: GenerationInputs,
    *, command_path: str = COMMAND_PATH,
) -> int | Problem:
    """Defer a validated clustering job under its generation's queue lock."""

    problem = lexical._inputs_problem(inputs, command_path, subject="clustering")
    if problem is not None:
        return problem
    return worker.defer(
        host, rendered_dir, task=DUPES_TASK, queue=DUPES_QUEUE,
        args=lexical._arguments(inputs), lock=f"{DUPES_LOCK_PREFIX}{inputs.name}",
    )


def _failure_problem(reason: str, name: str, rendered_dir: PathLike, command_path: str) -> Problem:
    if reason == "cluster-budget":
        return Problem(
            "clustering ran past its hour budget",
            f"Cut a smaller lockfile with {report.command('corpus cut')}, then run "
            f"{report.command(command_path)} again; a corpus of this size needs incremental clustering first.",
        )
    return lexical._failure_problem(reason, name, rendered_dir, command_path, subject="clustering")


def read_dupes(
    host: Host, rendered_dir: PathLike, job_id: int, name: str,
    *, command_path: str = COMMAND_PATH,
) -> lexical.BuildRead | Problem:
    """Combine a queue status with the generation's clustering failure fix."""

    return lexical._read_build(
        host, rendered_dir, job_id, name, command_path=command_path,
        subject="clustering", failure_problem=_failure_problem,
    )
