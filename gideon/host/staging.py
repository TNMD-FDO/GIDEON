"""Defer corpus staging and read its queue and file outcomes on the host."""

import datetime
import json
import math
import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import TypeGuard

from gideon.host import report, stack, worker
from gideon.host.render.worker import (
    COURT_PATTERN,
    RESERVED_SUFFIXES,
    SEGMENT_PATTERN,
    SNAPSHOTS_ROOT,
    SOURCE_PATTERN,
    STAGE_FAILURE_NAME,
    STAGE_FAILURE_REASONS,
    STAGE_QUEUE,
    STAGE_RECORD_NAME,
    STAGE_TABLES,
    STAGE_TASK,
    WORK_ROOT,
    WORKER_SERVICE_NAME,
)
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike

LABEL_PATTERN = re.compile(r"corpus-([0-9]{4}-[0-9]{2}-[0-9]{2})\Z")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
ERROR_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
DATE_SUFFIX_LENGTH = len("-YYYY-MM-DD")
COMMAND_PATH = "corpus install"


@dataclass(frozen=True, slots=True)
class StageRecord:
    """Every field of a completed stage's file record."""

    label: str
    source: str
    snapshot: str
    courts: list[str]
    inputs: dict[str, dict[str, str | int]]
    records: dict[str, int]
    counts: dict[str, dict[str, int]]
    job: int
    seconds: float
    staged_at: str
    schema: int


@dataclass(frozen=True, slots=True)
class StageRead:
    """One queue row and its completed stage record or failure."""

    job: worker.JobRow
    record: StageRecord | None
    failure: Problem | None
    reason: str | None


def _retry_fix(instruction: str, command_path: str) -> str:
    return f"{instruction} Then run {report.command(command_path)} again."


def _logs_fix(rendered_dir: PathLike, command_path: str) -> str:
    return _retry_fix(
        f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}.", command_path,
    )


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _aware_time(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _label_problem(label: object, command_path: str) -> Problem | None:
    match = LABEL_PATTERN.fullmatch(label) if isinstance(label, str) else None
    if match is None:
        return Problem(
            "stage label is invalid",
            _retry_fix("Use a label of the form corpus-YYYY-MM-DD.", command_path),
        )
    try:
        datetime.date.fromisoformat(match[1])
    except ValueError:
        return Problem(
            "stage label has an invalid date",
            _retry_fix("Use a real date in corpus-YYYY-MM-DD.", command_path),
        )
    return None


def _snapshot_problem(snapshot: object, command_path: str) -> Problem | None:
    if not isinstance(snapshot, str) or re.fullmatch(SOURCE_PATTERN, snapshot) is None:
        return Problem(
            "stage snapshot is invalid",
            _retry_fix("Use a kept snapshot of the form <source>-YYYY-MM-DD.", command_path),
        )
    try:
        datetime.date.fromisoformat(snapshot[-10:])
    except ValueError:
        return Problem(
            "stage snapshot has an invalid date",
            _retry_fix("Use a real date in <source>-YYYY-MM-DD.", command_path),
        )
    return None


def _source_problem(source: object, command_path: str) -> Problem | None:
    if not isinstance(source, str) or re.fullmatch(SEGMENT_PATTERN, source) is None:
        return Problem(
            "stage source is invalid",
            _retry_fix("Use one source name with no slash.", command_path),
        )
    return None


def _courts_valid(courts: object) -> bool:
    return (
        isinstance(courts, list) and bool(courts)
        and all(isinstance(court, str) and re.fullmatch(COURT_PATTERN, court)
                for court in courts)
        and courts == sorted(set(courts))
    )


def _courts_problem(courts: object, command_path: str) -> Problem | None:
    if _courts_valid(courts):
        return None
    return Problem(
        "stage courts are invalid",
        _retry_fix(
            "Use a non-empty sorted list of distinct lowercase court ids, "
            "each at most 32 letters or digits.", command_path,
        ),
    )


def _inputs_valid(inputs: object, *, sizes: bool) -> bool:
    keys = {"path", "sha256", "size"} if sizes else {"path", "sha256"}
    if not isinstance(inputs, dict) or set(inputs) != set(STAGE_TABLES):
        return False
    for item in inputs.values():
        if not isinstance(item, dict) or set(item) != keys:
            return False
        path, digest = item["path"], item["sha256"]
        if (
            not isinstance(path, str)
            or re.fullmatch(SEGMENT_PATTERN, path) is None
            or path.endswith(RESERVED_SUFFIXES)
            or not isinstance(digest, str)
            or DIGEST_PATTERN.fullmatch(digest) is None
        ):
            return False
        size = item.get("size")
        if sizes and (not _is_int(size) or size < 0):
            return False
    return True


def _inputs_problem(inputs: object, command_path: str) -> Problem | None:
    if _inputs_valid(inputs, sizes=False):
        return None
    return Problem(
        "stage inputs are invalid",
        _retry_fix(
            "Give exactly courts, dockets, opinion-clusters, citations, and opinions; "
            "each needs a one-segment path without a reserved suffix and a "
            "64-character lowercase sha256.", command_path,
        ),
    )


def work_directory(
    label: str, source: str, *, work_root: PathLike = WORK_ROOT,
) -> Path:
    """Compose the derived work directory for one label and source."""

    return Path(work_root) / label / source


def defer_stage(
    host: Host, rendered_dir: PathLike, *, label: str, snapshot: str,
    courts: list[str], inputs: dict[str, dict[str, str]],
    command_path: str = COMMAND_PATH,
) -> int | Problem:
    """Validate the bounded queue arguments before deferring one stage job."""

    for problem in (
        _label_problem(label, command_path),
        _snapshot_problem(snapshot, command_path),
        _courts_problem(courts, command_path),
        _inputs_problem(inputs, command_path),
    ):
        if problem is not None:
            return problem
    return worker.defer(
        host, rendered_dir, task=STAGE_TASK, queue=STAGE_QUEUE,
        args={"label": label, "snapshot": snapshot, "courts": courts, "inputs": inputs},
        lock=f"stage-{label}",
    )


def _read_json(
    host: Host, path: Path, kind: str, command_path: str,
) -> dict[str, object] | None | Problem:
    try:
        if not host.exists(path):
            return None
        value: object = json.loads(host.read_text(path))
    except (OSError, UnicodeError, ValueError):
        return Problem(
            f"stage {kind} file could not be read",
            _retry_fix("Inspect the worker logs and the stage file.", command_path),
        )
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return Problem(
            f"stage {kind} file is invalid",
            _retry_fix("Inspect the worker logs and the stage file.", command_path),
        )
    return value


def _record_from_json(value: dict[str, object], label: str, source: str) -> StageRecord | None:
    if set(value) != {field.name for field in fields(StageRecord)}:
        return None
    courts = value["courts"]
    records = value["records"]
    counts = value["counts"]
    seconds = value["seconds"]
    snapshot = value["snapshot"]
    if (
        not _is_int(value["schema"]) or value["schema"] != 1
        or value["label"] != label or _label_problem(value["label"], COMMAND_PATH) is not None
        or value["source"] != source or _source_problem(value["source"], COMMAND_PATH) is not None
        or _snapshot_problem(snapshot, COMMAND_PATH) is not None
        or not isinstance(snapshot, str)
        or source != snapshot[:-DATE_SUFFIX_LENGTH]
        or not isinstance(courts, list) or not _courts_valid(courts)
        or not _inputs_valid(value["inputs"], sizes=True)
        or not isinstance(records, dict) or set(records) != set(STAGE_TABLES)
        or any(not _is_int(count) or count < 0 for count in records.values())
        or not isinstance(counts, dict) or set(counts) != set(courts)
        or any(
            not isinstance(row, dict) or set(row) != set(STAGE_TABLES[1:])
            or any(not _is_int(count) or count < 0 for count in row.values())
            for row in counts.values()
        )
        or not _is_int(value["job"]) or value["job"] < 1
        or not isinstance(seconds, (int, float)) or isinstance(seconds, bool)
        or not math.isfinite(seconds) or seconds < 0
        or not _aware_time(value["staged_at"])
    ):
        return None
    return StageRecord(**value)  # type: ignore[arg-type]


def read_record(
    host: Host, label: str, source: str, *, work_root: PathLike = WORK_ROOT,
    command_path: str = COMMAND_PATH,
) -> StageRecord | None | Problem:
    """Read a whole stage record through the host seam, if one is present."""

    for problem in (_label_problem(label, command_path), _source_problem(source, command_path)):
        if problem is not None:
            return problem
    path = work_directory(label, source, work_root=work_root) / STAGE_RECORD_NAME
    value = _read_json(host, path, "record", command_path)
    if isinstance(value, Problem) or value is None:
        return value
    record = _record_from_json(value, label, source)
    if record is None:
        return Problem(
            "stage record file is invalid",
            _retry_fix("Inspect the worker logs and the stage record.", command_path),
        )
    return record


def _failure_from_json(value: dict[str, object]) -> dict[str, object] | None:
    if set(value) != {"schema", "job", "reason", "table", "court", "error", "at"}:
        return None
    reason = value["reason"]
    table = value["table"]
    court = value["court"]
    error = value["error"]
    if (
        not _is_int(value["schema"]) or value["schema"] != 1
        or not _is_int(value["job"]) or value["job"] < 1
        or not isinstance(reason, str) or reason not in STAGE_FAILURE_REASONS
        or (table is not None and (not isinstance(table, str) or table not in STAGE_TABLES))
        or (reason == "unknown-court") != (court is not None)
        or (court is not None and (
            not isinstance(court, str) or re.fullmatch(COURT_PATTERN, court) is None
        ))
        or (error is not None and (
            not isinstance(error, str) or ERROR_PATTERN.fullmatch(error) is None
        ))
        or not _aware_time(value["at"])
    ):
        return None
    return value


def _failure_problem(
    failure: dict[str, object], rendered_dir: PathLike, snapshot: str,
    command_path: str,
) -> Problem:
    reason = failure["reason"]
    table = failure["table"]
    if reason == "unknown-court":
        return Problem(
            f"stage court {failure['court']} is absent from the dump; "
            "courts.yaml and the lockfile courts disagree with it",
            f"Review courts.yaml, then make a new cut with {report.command('corpus cut')}; "
            f"run {report.command(command_path)} again.",
        )
    if reason in {"missing-input", "input-mismatch"}:
        return Problem(
            f"stage {table} input is {reason}",
            f"Remove the {table} snapshot file and its .fetch.json record under "
            f"{SNAPSHOTS_ROOT / snapshot}/, then run "
            f"{report.command(command_path)} again.",
        )
    if reason == "malformed":
        return Problem(f"stage {table} input is malformed", _logs_fix(rendered_dir, command_path))
    if reason == "local":
        return Problem(
            f"stage failed locally ({failure['error'] or 'unknown error'})",
            f"Run {report.command('host provision')}, then {report.command('apply')}, "
            f"then {report.command(command_path)} again.",
        )
    if reason == "busy":
        return Problem(
            "stage is busy with another job",
            f"Wait for the running stage, then run {report.command(command_path)} again.",
        )
    return Problem(f"stage failed: {reason}", _logs_fix(rendered_dir, command_path))


def read_stage(
    host: Host, rendered_dir: PathLike, job_id: int, *, label: str, snapshot: str,
    work_root: PathLike = WORK_ROOT, command_path: str = COMMAND_PATH,
) -> StageRead | Problem:
    """Combine one queue row with its whole stage record or filed failure."""

    for problem in (_label_problem(label, command_path), _snapshot_problem(snapshot, command_path)):
        if problem is not None:
            return problem
    source = snapshot[:-DATE_SUFFIX_LENGTH]
    row = worker.read_job(host, rendered_dir, job_id)
    if isinstance(row, Problem):
        return row
    if row.status in {"todo", "doing"}:
        return StageRead(row, None, None, None)
    if row.status == "succeeded":
        record = read_record(
            host, label, source, work_root=work_root, command_path=command_path,
        )
        if isinstance(record, Problem):
            return Problem(record.problem, _logs_fix(rendered_dir, command_path))
        if record is None:
            return Problem(
                "stage job succeeded without a whole directory and record",
                _logs_fix(rendered_dir, command_path),
            )
        return StageRead(row, record, None, None)
    if row.status in {"failed", "aborted", "cancelled"}:
        path = work_directory(label, source, work_root=work_root).with_name(
            f"{source}.{STAGE_FAILURE_NAME}"
        )
        value = _read_json(host, path, "failure", command_path)
        if isinstance(value, Problem):
            return Problem(value.problem, _logs_fix(rendered_dir, command_path))
        if value is not None and value.get("job") == job_id:
            failure = _failure_from_json(value)
            if failure is None:
                return Problem("stage failure file is invalid", _logs_fix(rendered_dir, command_path))
            return StageRead(
                row, None, _failure_problem(failure, rendered_dir, snapshot, command_path),
                str(failure["reason"]),
            )
        local: dict[str, object] = {"reason": "local", "error": None, "table": None}
        return StageRead(
            row, None, _failure_problem(local, rendered_dir, snapshot, command_path), "local",
        )
    return Problem("stage job has an unknown status", _logs_fix(rendered_dir, command_path))
