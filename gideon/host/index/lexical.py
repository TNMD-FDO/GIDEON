"""Validate and defer lexical builds and read their queue and record outcomes."""

import re
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

from gideon.host import caselaw, models, report, stack, staging, worker
from gideon.host.index import record
from gideon.host.index.generations import GenerationInputs
from gideon.host.render.opensearch import (
    OPENSEARCH_PASSWORD_SECRET_NAME,
    OPENSEARCH_REST_PORT,
    OPENSEARCH_SERVICE_NAME,
    OPENSEARCH_USER,
)
from gideon.host.render.worker import (
    COLLECTION_NAMES,
    GENERATION_PATTERN,
    LEXICAL_QUEUE,
    LEXICAL_TASK,
    WORKER_SERVICE_NAME,
)
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike

COMMAND_PATH = "index build"


@dataclass(frozen=True, slots=True)
class LexicalRead:
    """One queue row and its completed or failed lexical build."""

    job: worker.JobRow
    done: bool
    failure: Problem | None
    reason: str | None


def _logs_fix(rendered_dir: PathLike, command_path: str) -> str:
    return (
        f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, then run "
        f"{report.command(command_path)} again."
    )


def _name_problem(name: object, command_path: str) -> Problem | None:
    if (
        isinstance(name, str) and re.fullmatch(GENERATION_PATTERN, name) is not None
        and len(name.encode()) < 255
    ):
        return None
    return Problem(
        "lexical generation name is invalid",
        f"Use a recorded generation name under 255 bytes, then run {report.command(command_path)} again.",
    )


def _inputs_problem(inputs: GenerationInputs, command_path: str) -> Problem | None:
    retry = f"then run {report.command(command_path)} again."
    if inputs.collection not in COLLECTION_NAMES:
        return Problem("lexical collection is invalid", f"Use a registered collection, {retry}")
    for problem in (
        _name_problem(inputs.name, command_path),
        staging.label_problem(inputs.label, command_path, subject="lexical"),
        staging.snapshot_problem(inputs.snapshot, command_path, subject="lexical"),
        caselaw.pipeline_problem(inputs.pipeline, command_path),
    ):
        if problem is not None:
            return problem
    if not isinstance(inputs.embedding_space, str) or models.EMBEDDING_SPACE_ID.fullmatch(
        inputs.embedding_space
    ) is None:
        return Problem("lexical embedding space is invalid", f"Use the embedding space id from models.lock, {retry}")
    if not isinstance(inputs.inputs_hash, str) or staging.DIGEST_PATTERN.fullmatch(inputs.inputs_hash) is None:
        return Problem("lexical inputs hash is invalid", f"Use the recorded lowercase sha256 inputs hash, {retry}")
    if (
        not isinstance(inputs.courts, tuple) or not inputs.courts
        or not isinstance(inputs.geography, Mapping)
    ):
        return Problem("lexical courts are invalid", f"Use sorted court ids and their geography from the court map, {retry}")
    for court in inputs.courts:
        problem = caselaw.court_problem(court, command_path, subject="lexical")
        if problem is None:
            problem = caselaw.courts_problem(court, inputs.geography, command_path)
        if problem is not None:
            return problem
    if inputs.courts != tuple(sorted(set(inputs.courts))) or set(inputs.courts) != set(inputs.geography):
        return Problem("lexical courts are invalid", f"Use sorted distinct court ids with matching geography, {retry}")
    return None


def defer_lexical(
    host: Host, rendered_dir: PathLike, inputs: GenerationInputs,
    *, command_path: str = COMMAND_PATH,
) -> int | Problem:
    """Defer a validated build under its generation's queueing lock."""

    problem = _inputs_problem(inputs, command_path)
    if problem is not None:
        return problem
    args: dict[str, object] = {
        "name": inputs.name, "collection": inputs.collection, "label": inputs.label,
        "snapshot": inputs.snapshot,
        "courts": {
            court: {"level": geography.level, "circuit": geography.circuit, "state": geography.state}
            for court, geography in inputs.geography.items()
        },
        "pipeline": inputs.pipeline, "embedding_space": inputs.embedding_space,
        "inputs_hash": inputs.inputs_hash,
    }
    return worker.defer(
        host, rendered_dir, task=LEXICAL_TASK, queue=LEXICAL_QUEUE,
        args=args, lock=f"index-{inputs.name}",
    )


def live_job(
    host: Host, rendered_dir: PathLike, lock: str, *, command_path: str = COMMAND_PATH,
) -> int | None | Problem:
    """Find the first queued or running job under a generation's lock."""

    if not isinstance(lock, str) or not lock.startswith("index-"):
        return Problem("lexical queueing lock is invalid", f"Run {report.command(command_path)} with a recorded generation.")
    problem = _name_problem(lock.removeprefix("index-"), command_path)
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
        return Problem(f"lexical job query could not run ({type(exc).__name__})", tool_fix)
    if result.returncode == 127:
        return Problem("lexical job query is unavailable (exit 127)", tool_fix)
    fix = _logs_fix(rendered_dir, command_path)
    if result.returncode != 0:
        return Problem(f"lexical job query failed (exit {result.returncode})", fix)
    value = result.stdout.strip()
    if not value:
        return None
    if not value.isascii() or not value.isdecimal() or int(value) < 1:
        return Problem("lexical job query returned an invalid job id", fix)
    return int(value)


def _failure_problem(
    reason: str, name: str, rendered_dir: PathLike, command_path: str,
) -> Problem:
    retry = report.command(command_path)
    if reason == "store":
        return Problem(
            "lexical store failed",
            f"Run {stack.logs_fix(rendered_dir, OPENSEARCH_SERVICE_NAME)}, then "
            f"{report.command('apply')}, then run {retry} again.",
        )
    if reason == "busy":
        return Problem("lexical build is busy with another job", f"Wait, then run {retry} again.")
    if reason == "mapping-mismatch":
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
            f"lexical {reason}: the record and canonical text store disagree",
            f"Run {report.command('restore --from staging')} from a matching backup set, "
            f"then {report.command('apply')}, then run {retry} again.",
        )
    if reason == "generation-mismatch":
        return Problem("lexical generation no longer matches its inputs", f"Run {retry} again.")
    return Problem(f"lexical {reason} failure", _logs_fix(rendered_dir, command_path))


def read_lexical(
    host: Host, rendered_dir: PathLike, job_id: int, name: str,
    *, command_path: str = COMMAND_PATH,
) -> LexicalRead | Problem:
    """Combine a queue status with the generation's recorded failure reason."""

    problem = _name_problem(name, command_path)
    if problem is not None:
        return problem
    job = worker.read_job(host, rendered_dir, job_id)
    if isinstance(job, Problem):
        return job
    if job.status in {"todo", "doing"}:
        return LexicalRead(job, False, None, None)
    if job.status == "succeeded":
        return LexicalRead(job, True, None, None)
    if job.status not in {"failed", "aborted", "cancelled"}:
        return Problem("lexical job has an unknown status", _logs_fix(rendered_dir, command_path))
    generation = record.read_generation(host, rendered_dir, name, command_path=command_path)
    if isinstance(generation, Problem):
        return generation
    reason = generation.failure_reason if generation is not None and generation.state == "failed" else "local"
    assert reason is not None
    return LexicalRead(job, False, _failure_problem(reason, name, rendered_dir, command_path), reason)
