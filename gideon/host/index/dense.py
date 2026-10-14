"""Validate and defer dense builds, read their failures, and project their progress."""

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from gideon.host import report, stack, worker
from gideon.host.index import lexical
from gideon.host.index.generations import GenerationInputs
from gideon.host.render.engine import EMBED_SERVICE_NAME
from gideon.host.render.qdrant import (
    QDRANT_REST_PORT,
    QDRANT_SECRET_NAME,
    QDRANT_SERVICE_NAME,
)
from gideon.host.render.worker import (
    DENSE_LOCK_PREFIX,
    DENSE_QUEUE,
    DENSE_TASK,
    DIMENSIONS_MAX,
    SERVED_MODEL_PATTERN,
    WORKER_SERVICE_NAME,
)
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike

COMMAND_PATH: Final = "index build"
# exempt: three weeks is the starting tripwire for a projected whole-generation build.
TRIPWIRE_DAYS: Final = 21


@dataclass(frozen=True, slots=True)
class DenseInputs(GenerationInputs):
    """The generation's immutable scope plus its served model and vector width."""

    served_model: str
    dimensions: int


@dataclass(frozen=True, slots=True)
class Projection:
    """Rates and scope finish, with the whole-generation duration and verdict."""

    tokens_per_second: float
    chunks_per_second: float
    remaining: int
    finish: datetime
    generation_seconds: float
    verdict: Literal["inside", "past"]


def defer_dense(
    host: Host, rendered_dir: PathLike, inputs: DenseInputs, *,
    limit: int | None = None, command_path: str = COMMAND_PATH,
) -> int | Problem:
    """Defer a validated dense job under the generation's dense queue lock."""

    problem = lexical._inputs_problem(inputs, command_path, subject="dense")
    if problem is not None:
        return problem
    retry = f"then run {report.command(command_path)} again."
    if not isinstance(inputs.served_model, str) or re.fullmatch(SERVED_MODEL_PATTERN, inputs.served_model) is None:
        return Problem("dense served model is invalid", f"Use the profile's served model name from models.lock, {retry}")
    if type(inputs.dimensions) is not int or not 1 <= inputs.dimensions <= DIMENSIONS_MAX:
        return Problem("dense dimensions are invalid", f"Use a profile vector width from 1 to {DIMENSIONS_MAX}, {retry}")
    if limit is not None and (type(limit) is not int or limit < 1):
        return Problem("dense document limit is invalid", f"Use a positive integer document limit, {retry}")
    args = lexical._arguments(inputs)
    args.update(served_model=inputs.served_model, dimensions=inputs.dimensions)
    if limit is not None:
        args["limit"] = limit
    return worker.defer(host, rendered_dir, task=DENSE_TASK, queue=DENSE_QUEUE,
                        args=args, lock=f"{DENSE_LOCK_PREFIX}{inputs.name}")


def _failure_problem(reason: str, name: str, rendered_dir: PathLike, command_path: str) -> Problem:
    retry = report.command(command_path)
    if reason == "store":
        return Problem("dense vector store failed",
                       f"Run {stack.logs_fix(rendered_dir, QDRANT_SERVICE_NAME)}, then "
                       f"{report.command('apply')}, then run {retry} again.")
    if reason == "embed":
        return Problem("dense embedding server failed",
                       f"Run {stack.logs_fix(rendered_dir, EMBED_SERVICE_NAME)}, then "
                       f"{report.command('engine verify')}, then run {retry} again.")
    if reason == "config-mismatch":
        script = (
            "set -e -o pipefail; "
            f"api_key=$(cat /run/secrets/{QDRANT_SECRET_NAME}); "
            'test -n "$api_key"; '
            f"exec 3<>/dev/tcp/127.0.0.1/{QDRANT_REST_PORT}; "
            f"printf 'DELETE /collections/{name} HTTP/1.0\\r\\napi-key: %s\\r\\n\\r\\n' "
            '"$api_key" >&3; '
            "head -n 1 <&3 | grep -q ' 200 '"
        )
        command = shlex.join(stack.exec_argv(rendered_dir, QDRANT_SERVICE_NAME, "bash", "-c", script))
        return Problem("dense collection holds another release's configuration",
                       f"Remove the derived collection with {command}, then run {retry} again.")
    if reason == "chunk-too-long":
        return Problem("dense chunk exceeds the embedding server's token cap",
                       f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, restore the pipeline registry, "
                       f"then run {retry} again.")
    return lexical._failure_problem(reason, name, rendered_dir, command_path, subject="dense")


def read_dense(
    host: Host, rendered_dir: PathLike, job_id: int, name: str, *, command_path: str = COMMAND_PATH,
) -> lexical.BuildRead | Problem:
    """Combine a queue status with the generation's dense failure fix."""

    return lexical._read_build(host, rendered_dir, job_id, name, command_path=command_path,
                               subject="dense", failure_problem=_failure_problem)


def projection(counts: Mapping[str, int], now: datetime) -> Projection | None:
    """Project the remaining scope and whole generation from this attempt's writes."""

    seconds = counts.get("dense_seconds", 0)
    written = counts.get("dense_copied", 0) + counts.get("dense_embedded", 0)
    if seconds <= 0 or written <= 0:
        return None
    chunks_per_second = written / seconds
    remaining = max(0, counts.get("dense_scope", 0) - counts.get("dense_present", 0) - written)
    generation_seconds = counts.get("dense_total", 0) / chunks_per_second
    return Projection(
        counts.get("dense_tokens", 0) / seconds, chunks_per_second, remaining,
        now + timedelta(seconds=remaining / chunks_per_second), generation_seconds,
        "inside" if generation_seconds <= timedelta(days=TRIPWIRE_DAYS).total_seconds() else "past",
    )
