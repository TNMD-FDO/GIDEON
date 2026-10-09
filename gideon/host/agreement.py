"""Defer a court agreement job and read its figure or filed failure."""

import datetime
import json
import math
import re
from dataclasses import dataclass, fields
from pathlib import Path

from gideon.host import caselaw, report, stack, staging, worker
from gideon.host.render.worker import (
    AGREEMENT_FAILURE_NAME,
    AGREEMENT_FAILURE_REASONS,
    AGREEMENT_QUEUE,
    AGREEMENT_RECORD_NAME,
    AGREEMENT_TABLE,
    AGREEMENT_TASK,
    RECORD_SUFFIX,
    RESERVED_SUFFIXES,
    SEGMENT_PATTERN,
    SNAPSHOTS_ROOT,
    STAGE_TABLES,
    WORK_ROOT,
    WORKER_SERVICE_NAME,
)
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike

COMMAND_PATH = "corpus install"
_COUNTS = (
    "documents", "gideon_pairs", "map_rows", "map_outside", "map_pairs",
    "agreed", "gideon_only", "map_only_seen", "map_only_missed",
)


@dataclass(frozen=True, slots=True)
class AgreementRecord:
    """One court's validated agreement figure."""

    label: str
    snapshot: str
    court: str
    job: int
    documents: int
    gideon_pairs: int
    map_rows: int
    map_outside: int
    map_pairs: int
    agreed: int
    gideon_only: int
    map_only_seen: int
    map_only_missed: int
    seconds: float
    computed_at: str
    schema: int

    def gideon_share(self) -> int:
        """Return the agreed share of GIDEON pairs as a whole percent."""

        return round(100 * self.agreed / self.gideon_pairs) if self.gideon_pairs else 0

    def map_share(self) -> int:
        """Return the agreed share of map pairs as a whole percent."""

        return round(100 * self.agreed / self.map_pairs) if self.map_pairs else 0


@dataclass(frozen=True, slots=True)
class AgreementRead:
    """One queue row and its figure or filed failure."""

    job: worker.JobRow
    done: bool
    record: AgreementRecord | None
    failure: Problem | None
    reason: str | None


def _logs_fix(rendered_dir: PathLike, command_path: str) -> str:
    return (
        f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, "
        f"then run {report.command(command_path)} again."
    )


def _input_problem(value: object, command_path: str) -> Problem | None:
    if isinstance(value, dict) and set(value) == {"path", "sha256"}:
        path, digest = value["path"], value["sha256"]
        if (
            isinstance(path, str)
            and re.fullmatch(SEGMENT_PATTERN, path) is not None
            and not path.endswith(RESERVED_SUFFIXES)
            and isinstance(digest, str)
            and staging.DIGEST_PATTERN.fullmatch(digest) is not None
        ):
            return None
    return Problem(
        "agreement input is invalid",
        "Give one map input with a one-segment path without a reserved suffix "
        "and a 64-character lowercase sha256, then run "
        f"{report.command(command_path)} again.",
    )


def defer_agreement(
    host: Host, rendered_dir: PathLike, *, label: str, snapshot: str,
    court: str, input: dict[str, str], command_path: str = COMMAND_PATH,
) -> int | Problem:
    """Validate and defer one court's agreement job under its queue lock."""

    for problem in (
        staging.label_problem(label, command_path, subject="agreement"),
        staging.snapshot_problem(snapshot, command_path, subject="agreement"),
        caselaw.court_problem(court, command_path, subject="agreement"),
        _input_problem(input, command_path),
    ):
        if problem is not None:
            return problem
    return worker.defer(
        host, rendered_dir, task=AGREEMENT_TASK, queue=AGREEMENT_QUEUE,
        args={"label": label, "snapshot": snapshot, "court": court, "input": input},
        lock=f"agreement-{label}-{court}",
    )


def _aware_time(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _record_from_json(
    value: object, label: str, snapshot: str, court: str,
) -> AgreementRecord | None:
    if not isinstance(value, dict) or set(value) != {field.name for field in fields(AgreementRecord)}:
        return None
    if (
        type(value["schema"]) is not int or value["schema"] != 1
        or value["label"] != label or value["snapshot"] != snapshot
        or value["court"] != court
        or type(value["job"]) is not int or value["job"] < 1
        or any(type(value[name]) is not int or value[name] < 0 for name in _COUNTS)
        or type(value["seconds"]) not in (int, float)
        or not math.isfinite(value["seconds"]) or value["seconds"] < 0
        or not _aware_time(value["computed_at"])
    ):
        return None
    if (
        value["agreed"] > value["gideon_pairs"]
        or value["agreed"] > value["map_pairs"]
        or value["gideon_only"] != value["gideon_pairs"] - value["agreed"]
        or value["map_only_seen"] + value["map_only_missed"]
        != value["map_pairs"] - value["agreed"]
        or value["map_outside"] > value["map_rows"]
    ):
        return None
    return AgreementRecord(**value)


def read_record(
    host: Host, label: str, snapshot: str, court: str, *,
    work_root: PathLike = WORK_ROOT, command_path: str = COMMAND_PATH,
) -> AgreementRecord | None | Problem:
    """Read and validate one court's completed figure through the host seam."""

    for problem in (
        staging.label_problem(label, command_path, subject="agreement"),
        staging.snapshot_problem(snapshot, command_path, subject="agreement"),
        caselaw.court_problem(court, command_path, subject="agreement"),
    ):
        if problem is not None:
            return problem
    source = snapshot[:-staging.DATE_SUFFIX_LENGTH]
    path = staging.work_directory(label, source, work_root=work_root) / f"{court}.{AGREEMENT_RECORD_NAME}"
    try:
        if not host.exists(path):
            return None
        value: object = json.loads(host.read_text(path))
    except (OSError, UnicodeError, ValueError):
        return Problem("agreement record file could not be read", "Inspect the worker logs, then run "
                       f"{report.command(command_path)} again.")
    record = _record_from_json(value, label, snapshot, court)
    if record is None:
        return Problem("agreement record file is invalid", "Inspect the worker logs, then run "
                       f"{report.command(command_path)} again.")
    return record


def _failure_valid(value: object, job_id: int, court: str) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema", "job", "court", "reason", "table", "error", "at",
    }:
        return False
    reason, table, error = value["reason"], value["table"], value["error"]
    return (
        type(value["schema"]) is int and value["schema"] == 1
        and type(value["job"]) is int and value["job"] == job_id
        and value["court"] == court
        and isinstance(reason, str) and reason in AGREEMENT_FAILURE_REASONS
        and (table is None or isinstance(table, str) and table in (*STAGE_TABLES, AGREEMENT_TABLE))
        and (error is None or isinstance(error, str)
             and staging.ERROR_PATTERN.fullmatch(error) is not None)
        and _aware_time(value["at"])
    )


def _failure_problem(
    reason: str, table: str | None, error: str | None,
    rendered_dir: PathLike, snapshot: str, work_dir: Path, command_path: str,
) -> Problem:
    install = report.command(command_path)
    if reason in {"missing-input", "input-mismatch"}:
        return Problem(
            f"agreement {AGREEMENT_TABLE} input is {reason}",
            f"Remove the {AGREEMENT_TABLE} snapshot file and its {RECORD_SUFFIX} record "
            f"under {SNAPSHOTS_ROOT / snapshot}/, then run {install} again.",
        )
    if reason in {"missing-stage", "stage-mismatch"}:
        return Problem(f"agreement stage is {reason}", f"Remove {work_dir}, then run {install} again.")
    if reason == "malformed":
        return Problem(f"agreement {table or AGREEMENT_TABLE} file is malformed",
                       _logs_fix(rendered_dir, command_path))
    if reason in {"database", "local"}:
        return Problem(
            f"agreement {reason} failure ({error or 'unknown error'})",
            f"Run {report.command('host provision')}, then {report.command('apply')}, "
            f"then {install} again.",
        )
    if reason == "busy":
        return Problem("agreement is busy with another job", f"Wait, then run {install} again.")
    return Problem(f"agreement failed: {reason}", _logs_fix(rendered_dir, command_path))


def read_agreement(
    host: Host, rendered_dir: PathLike, job_id: int, *, label: str,
    snapshot: str, court: str, work_root: PathLike = WORK_ROOT,
    command_path: str = COMMAND_PATH,
) -> AgreementRead | Problem:
    """Combine the queue row with this job's figure or filed failure."""

    for problem in (
        staging.label_problem(label, command_path, subject="agreement"),
        staging.snapshot_problem(snapshot, command_path, subject="agreement"),
        caselaw.court_problem(court, command_path, subject="agreement"),
    ):
        if problem is not None:
            return problem
    row = worker.read_job(host, rendered_dir, job_id)
    if isinstance(row, Problem):
        return row
    if row.status in {"todo", "doing"}:
        return AgreementRead(row, False, None, None, None)
    source = snapshot[:-staging.DATE_SUFFIX_LENGTH]
    work_dir = staging.work_directory(label, source, work_root=work_root)
    if row.status == "succeeded":
        record = read_record(
            host, label, snapshot, court, work_root=work_root, command_path=command_path,
        )
        if isinstance(record, Problem):
            return Problem(record.problem, _logs_fix(rendered_dir, command_path))
        if record is None or record.job != job_id:
            return Problem("agreement job succeeded without its figure", _logs_fix(rendered_dir, command_path))
        return AgreementRead(row, True, record, None, None)
    if row.status not in {"failed", "aborted", "cancelled"}:
        return Problem("agreement job has an unknown status", _logs_fix(rendered_dir, command_path))
    path = work_dir / f"{court}.{AGREEMENT_FAILURE_NAME}"
    try:
        value: object = json.loads(host.read_text(path)) if host.exists(path) else None
    except (OSError, UnicodeError, ValueError):
        return Problem("agreement failure file could not be read", _logs_fix(rendered_dir, command_path))
    if value is not None and not isinstance(value, dict):
        return Problem("agreement failure file is invalid", _logs_fix(rendered_dir, command_path))
    if value is None or value.get("job") != job_id:
        reason = "local"
        return AgreementRead(
            row, False, None,
            _failure_problem(reason, None, None, rendered_dir, snapshot, work_dir, command_path),
            reason,
        )
    if not _failure_valid(value, job_id, court):
        return Problem("agreement failure file is invalid", _logs_fix(rendered_dir, command_path))
    reason = value["reason"]
    assert isinstance(reason, str)
    return AgreementRead(
        row, False, None,
        _failure_problem(reason, value["table"], value["error"],
                         rendered_dir, snapshot, work_dir, command_path),
        reason,
    )
