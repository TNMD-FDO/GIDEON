"""Shared generation job validation and reads, and lexical build deferral and fixes."""

import re
import shlex
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from gideon.host import caselaw, models, report, stack, staging, worker
from gideon.host.index import record, sample
from gideon.host.index.generations import GenerationInputs
from gideon.host.render.opensearch import (
    OPENSEARCH_PASSWORD_SECRET_NAME,
    OPENSEARCH_REST_PORT,
    OPENSEARCH_SERVICE_NAME,
    OPENSEARCH_USER,
)
from gideon.host.render.worker import (
    COLLECTION_NAMES,
    DENSE_LOCK_PREFIX,
    DUPES_LOCK_PREFIX,
    GENERATION_PATTERN,
    LEXICAL_QUEUE,
    LEXICAL_TASK,
    WORKER_SERVICE_NAME,
)
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike

COMMAND_PATH = "index build"
JOB_SUBJECTS = {"index-": "lexical", DENSE_LOCK_PREFIX: "dense", DUPES_LOCK_PREFIX: "clustering"}


@dataclass(frozen=True, slots=True)
class BuildRead:
    """One queue row and its completed or failed generation build."""

    job: worker.JobRow
    done: bool
    failure: Problem | None
    reason: str | None


def _logs_fix(rendered_dir: PathLike, command_path: str) -> str:
    return (
        f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, then run "
        f"{report.command(command_path)} again."
    )


def _name_problem(name: object, command_path: str, *, subject: str = "lexical") -> Problem | None:
    if (
        isinstance(name, str) and re.fullmatch(GENERATION_PATTERN, name) is not None
        and len(name.encode()) < 255
    ):
        return None
    return Problem(
        f"{subject} generation name is invalid",
        f"Use a recorded generation name under 255 bytes, then run {report.command(command_path)} again.",
    )


def _inputs_problem(inputs: GenerationInputs, command_path: str, *, subject: str = "lexical") -> Problem | None:
    retry = f"then run {report.command(command_path)} again."
    if inputs.collection not in COLLECTION_NAMES:
        return Problem(f"{subject} collection is invalid", f"Use a registered collection, {retry}")
    for problem in (
        _name_problem(inputs.name, command_path, subject=subject),
        staging.label_problem(inputs.label, command_path, subject=subject),
        staging.snapshot_problem(inputs.snapshot, command_path, subject=subject),
        caselaw.pipeline_problem(inputs.pipeline, command_path, subject=subject),
    ):
        if problem is not None:
            return problem
    if not isinstance(inputs.embedding_space, str) or models.EMBEDDING_SPACE_ID.fullmatch(
        inputs.embedding_space
    ) is None:
        return Problem(f"{subject} embedding space is invalid", f"Use the embedding space id from models.lock, {retry}")
    if not isinstance(inputs.inputs_hash, str) or staging.DIGEST_PATTERN.fullmatch(inputs.inputs_hash) is None:
        return Problem(f"{subject} inputs hash is invalid", f"Use the recorded lowercase sha256 inputs hash, {retry}")
    if (
        not isinstance(inputs.courts, tuple) or not inputs.courts
        or not isinstance(inputs.geography, Mapping)
    ):
        return Problem(f"{subject} courts are invalid", f"Use sorted court ids and their geography from the court map, {retry}")
    for court in inputs.courts:
        problem = caselaw.court_problem(court, command_path, subject=subject)
        if problem is None:
            problem = caselaw.courts_problem(court, inputs.geography, command_path, subject=subject)
        if problem is not None:
            return problem
    if inputs.courts != tuple(sorted(set(inputs.courts))) or set(inputs.courts) != set(inputs.geography):
        return Problem(f"{subject} courts are invalid", f"Use sorted distinct court ids with matching geography, {retry}")
    if inputs.sample is not None:
        if not isinstance(inputs.sample, sample.SampleInputs):
            findings = ("sample",)
        else:
            findings = sample.validate(sample.arguments(inputs.sample))
        if findings:
            return Problem(
                f"{subject} sample is invalid ({', '.join(findings)})",
                f"Use valid sample arguments, {retry}",
            )
    return None


def _arguments(inputs: GenerationInputs) -> dict[str, object]:
    arguments: dict[str, object] = {
        "name": inputs.name, "collection": inputs.collection, "label": inputs.label,
        "snapshot": inputs.snapshot,
        "courts": {
            court: {"level": geography.level, "circuit": geography.circuit, "state": geography.state}
            for court, geography in inputs.geography.items()
        },
        "pipeline": inputs.pipeline, "embedding_space": inputs.embedding_space,
        "inputs_hash": inputs.inputs_hash,
    }
    if inputs.sample is not None:
        arguments["sample"] = sample.arguments(inputs.sample)
    return arguments


def defer_lexical(
    host: Host, rendered_dir: PathLike, inputs: GenerationInputs,
    *, command_path: str = COMMAND_PATH,
) -> int | Problem:
    """Defer a validated build under its generation's queueing lock."""

    problem = _inputs_problem(inputs, command_path)
    if problem is not None:
        return problem
    return worker.defer(
        host, rendered_dir, task=LEXICAL_TASK, queue=LEXICAL_QUEUE,
        args=_arguments(inputs), lock=f"index-{inputs.name}",
    )


def live_job(
    host: Host, rendered_dir: PathLike, lock: str, *, prefix: str = "index-", command_path: str = COMMAND_PATH,
) -> int | None | Problem:
    """Find the first queued or running job under a generation's lock."""

    subject = JOB_SUBJECTS.get(prefix, "lexical") if isinstance(prefix, str) else "lexical"
    if not isinstance(prefix, str) or not prefix or not isinstance(lock, str) or not lock.startswith(prefix):
        return Problem(f"{subject} queueing lock is invalid", f"Run {report.command(command_path)} with a recorded generation.")
    problem = _name_problem(lock.removeprefix(prefix), command_path, subject=subject)
    if problem is not None:
        return problem
    sql = (
        worker.bind("v_lock", lock)
        + "\nSELECT id FROM procrastinate_jobs WHERE lock = :'v_lock' "
        "AND status IN ('todo', 'doing') ORDER BY id LIMIT 1;\n"
    )
    tool_fix = f"Restore Docker Compose and psql, then run {report.command(command_path)} again."
    try:
        result = host.run(worker.psql_argv(rendered_dir), input=sql)
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"{subject} job query could not run ({type(exc).__name__})", tool_fix)
    if result.returncode == 127:
        return Problem(f"{subject} job query is unavailable (exit 127)", tool_fix)
    fix = _logs_fix(rendered_dir, command_path)
    if result.returncode != 0:
        return Problem(f"{subject} job query failed (exit {result.returncode})", fix)
    value = result.stdout.strip()
    if not value:
        return None
    if not value.isascii() or not value.isdecimal() or int(value) < 1:
        return Problem(f"{subject} job query returned an invalid job id", fix)
    return int(value)


def live_index_job(
    host: Host, rendered_dir: PathLike, retry: str,
) -> int | None | Problem:
    """Find the first queued or running index job in a project's record.

    ``retry`` is the caller's whole command as its fix prints it. A project
    with no rendered document or no running Postgres holds no job.
    """

    fix = (
        f"Run {stack.logs_fix(rendered_dir, 'postgres')}, then run "
        f"{retry} again."
    )
    tool_fix = f"Restore Docker Compose and psql. {fix}"
    sql = (
        worker.bind("v_queue", LEXICAL_QUEUE)
        + "\nSELECT id FROM procrastinate_jobs WHERE queue_name = :'v_queue' "
        "AND status IN ('todo', 'doing') ORDER BY id LIMIT 1;\n"
    )
    try:
        if not host.exists(Path(rendered_dir) / "compose.yaml"):
            return None
        result = host.run(worker.psql_argv(rendered_dir), input=sql)
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"index job query could not run ({type(exc).__name__})", tool_fix)
    if result.returncode == 127:
        return Problem("index job query is unavailable (exit 127)", tool_fix)
    if result.returncode != 0:
        if result.returncode == 1 and 'service "postgres" is not running' in (
            line.strip() for line in (result.stderr + "\n" + result.stdout).splitlines()
        ):
            return None
        return Problem(f"index job query failed (exit {result.returncode})", fix)
    value = result.stdout.strip()
    if not value:
        return None
    if not value.isascii() or not value.isdecimal() or int(value) < 1:
        return Problem("index job query returned an invalid job id", fix)
    return int(value)


def _failure_problem(
    reason: str, name: str, rendered_dir: PathLike, command_path: str,
    *, subject: str = "lexical",
) -> Problem:
    retry = report.command(command_path)
    if reason == "store":
        return Problem(
            f"{subject} store failed",
            f"Run {stack.logs_fix(rendered_dir, OPENSEARCH_SERVICE_NAME)}, then "
            f"{report.command('apply')}, then run {retry} again.",
        )
    if reason == "busy":
        return Problem(f"{subject} build is busy with another job", f"Wait, then run {retry} again.")
    if reason == "mapping-mismatch" and subject == "lexical":
        script = (
            f"printf 'user = \"%s:%s\"\\n' {OPENSEARCH_USER} "
            f'"$(cat /run/secrets/{OPENSEARCH_PASSWORD_SECRET_NAME})" | '
            "curl --config - --fail --silent --show-error --request DELETE "
            f"http://127.0.0.1:{OPENSEARCH_REST_PORT}/{name}"
        )
        command = shlex.join(stack.exec_argv(rendered_dir, OPENSEARCH_SERVICE_NAME, "sh", "-c", script))
        return Problem(
            "lexical index holds another release's mapping",
            f"Remove the derived index with {command}, then run {retry} again.",
        )
    if reason in {"text-mismatch", "missing-text"}:
        return Problem(
            f"{subject} {reason}: the record and canonical text store disagree",
            f"Run {report.command('restore --from staging')} from a matching backup set, "
            f"then {report.command('apply')}, then run {retry} again.",
        )
    if reason == "generation-mismatch":
        return Problem(f"{subject} generation no longer matches its inputs", f"Run {retry} again.")
    return Problem(f"{subject} {reason} failure", _logs_fix(rendered_dir, command_path))


def _read_build(
    host: Host, rendered_dir: PathLike, job_id: int, name: str,
    *, command_path: str, subject: str,
    failure_problem: Callable[[str, str, PathLike, str], Problem],
) -> BuildRead | Problem:
    """Combine a queue status with the generation's recorded failure reason."""

    problem = _name_problem(name, command_path, subject=subject)
    if problem is not None:
        return problem
    job = worker.read_job(host, rendered_dir, job_id)
    if isinstance(job, Problem):
        return job
    if job.status in {"todo", "doing"}:
        return BuildRead(job, False, None, None)
    if job.status == "succeeded":
        return BuildRead(job, True, None, None)
    if job.status not in {"failed", "aborted", "cancelled"}:
        return Problem(f"{subject} job has an unknown status", _logs_fix(rendered_dir, command_path))
    generation = record.read_generation(host, rendered_dir, name, command_path=command_path)
    if isinstance(generation, Problem):
        return generation
    reason = generation.failure_reason if generation is not None and generation.state == "failed" else "local"
    assert reason is not None
    return BuildRead(job, False, failure_problem(reason, name, rendered_dir, command_path), reason)


def read_lexical(
    host: Host, rendered_dir: PathLike, job_id: int, name: str,
    *, command_path: str = COMMAND_PATH,
) -> BuildRead | Problem:
    """Combine a queue status with the generation's lexical failure fix."""

    return _read_build(host, rendered_dir, job_id, name, command_path=command_path,
                       subject="lexical", failure_problem=_failure_problem)
