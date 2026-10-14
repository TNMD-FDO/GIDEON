"""Defer the captured overrulings job and read its counts and failures."""

import json
import re
import subprocess
from dataclasses import dataclass

from gideon.host import agreement, caselaw, report, stack, staging, worker
from gideon.host.corpus import sources
from gideon.host.render.worker import (
    OVERRULED_COURT,
    OVERRULED_FAILURE_NAME,
    OVERRULED_FAILURE_REASONS,
    OVERRULED_QUEUE,
    OVERRULED_RESOLUTIONS,
    OVERRULED_SOURCE,
    OVERRULED_TASK,
    OVERRULED_WORK_DIRECTORY,
    RECORD_SUFFIX,
    SNAPSHOTS_ROOT,
    STAGE_TABLES,
    WORK_ROOT,
    WORKER_SERVICE_NAME,
)
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike

COMMAND_PATH = "corpus install"
OVERRULED_COUNTS_SQL = f"""WITH selected_rows AS (
    SELECT list_order, overruled_cluster, overruled_resolution, overruling_resolution
    FROM public.known_overruled
    WHERE source = :'v_source'
      AND source_snapshot = :'v_snapshot_date'::date
      AND caselaw_source = :'v_caselaw_source'
      AND caselaw_snapshot = :'v_caselaw_snapshot_date'::date
      AND :'v_court' = '{OVERRULED_COURT}'
)
SELECT jsonb_build_object(
    'list_rows', (SELECT count(DISTINCT list_order) FROM selected_rows),
    'rows', (SELECT count(*) FROM selected_rows),
    'overruled', COALESCE((
        SELECT jsonb_object_agg(overruled_resolution, n) FROM (
            SELECT overruled_resolution, count(*) AS n
            FROM selected_rows GROUP BY overruled_resolution
        ) AS counts
    ), '{{}}'::jsonb),
    'overruling', COALESCE((
        SELECT jsonb_object_agg(overruling_resolution, n) FROM (
            SELECT overruling_resolution, count(*) AS n
            FROM selected_rows GROUP BY overruling_resolution
        ) AS counts
    ), '{{}}'::jsonb),
    'clusters', (SELECT count(DISTINCT overruled_cluster) FROM selected_rows)
);
"""


@dataclass(frozen=True, slots=True)
class OverruledCounts:
    """Validated counts over one treatment and case-law snapshot pair."""

    list_rows: int
    rows: int
    overruled: dict[str, int]
    overruling: dict[str, int]
    clusters: int


@dataclass(frozen=True, slots=True)
class OverruledRead:
    """One queue row and its completion or filed refusal."""

    row: worker.JobRow
    done: bool
    failure: Problem | None
    reason: str | None


def _logs_fix(rendered_dir: PathLike, command_path: str) -> str:
    return (
        f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, "
        f"then run {report.command(command_path)} again."
    )


def _snapshot_problem(snapshot: object, command_path: str) -> Problem | None:
    problem = staging.snapshot_problem(snapshot, command_path, subject="overruled")
    if problem is not None:
        return problem
    assert isinstance(snapshot, str)
    if snapshot[:-staging.DATE_SUFFIX_LENGTH] != OVERRULED_SOURCE:
        return Problem(
            "overruled snapshot source is invalid",
            f"Use a {OVERRULED_SOURCE}-YYYY-MM-DD snapshot, then run "
            f"{report.command(command_path)} again.",
        )
    return None


def _court_problem(court: object, command_path: str) -> Problem | None:
    problem = caselaw.court_problem(court, command_path, subject="overruled")
    if problem is not None:
        return problem
    if court != OVERRULED_COURT:
        return Problem(
            "overruled list is Supreme Court only",
            f"Use court {OVERRULED_COURT}, then run {report.command(command_path)} again.",
        )
    return None


def defer_overruled(
    host: Host, rendered_dir: PathLike, *, label: str, snapshot: str,
    caselaw_snapshot: str, court: str, input: dict[str, str],
    command_path: str = COMMAND_PATH,
) -> int | Problem:
    """Validate and defer the list job under the label's queue lock."""

    for problem in (
        staging.label_problem(label, command_path, subject="overruled"),
        _snapshot_problem(snapshot, command_path),
        staging.snapshot_problem(caselaw_snapshot, command_path, subject="overruled case-law"),
        _court_problem(court, command_path),
    ):
        if problem is not None:
            return problem
    if agreement.input_problem(input, command_path) is not None:
        return Problem(
            "overruled input is invalid",
            "Give one capture input with a one-segment path without a reserved suffix "
            f"and a 64-character lowercase sha256, then run {report.command(command_path)} again.",
        )
    result = worker.defer(
        host, rendered_dir, task=OVERRULED_TASK, queue=OVERRULED_QUEUE,
        args={"label": label, "snapshot": snapshot, "caselaw_snapshot": caselaw_snapshot,
              "court": court, "input": input},
        lock=f"overruled-{label}",
    )
    if isinstance(result, Problem):
        return Problem(result.problem, _logs_fix(rendered_dir, command_path))
    return result


def _counts_from_json(value: object) -> OverruledCounts | None:
    if not isinstance(value, dict) or set(value) != {
        "list_rows", "rows", "overruled", "overruling", "clusters",
    }:
        return None
    if any(type(value[key]) is not int or value[key] < 0 for key in ("list_rows", "rows", "clusters")):
        return None
    ruled = caselaw.counts_map(value["overruled"], frozenset(OVERRULED_RESOLUTIONS) - {"docket"})
    ruling = caselaw.counts_map(value["overruling"], frozenset(OVERRULED_RESOLUTIONS) - {"name"})
    if ruled is None or ruling is None or sum(ruled.values()) != value["rows"] or sum(ruling.values()) != value["rows"]:
        return None
    resolved = ruled.get("cite", 0) + ruled.get("name", 0)
    if (
        value["list_rows"] > value["rows"]
        or (value["list_rows"] == 0) != (value["rows"] == 0)
        or value["clusters"] > resolved
        or (value["clusters"] == 0) != (resolved == 0)
    ):
        return None
    return OverruledCounts(value["list_rows"], value["rows"], ruled, ruling, value["clusters"])


def read_overruled_counts(
    host: Host, rendered_dir: PathLike, *, source: str, snapshot_date: str,
    caselaw_source: str, caselaw_snapshot_date: str, court: str,
    command_path: str = COMMAND_PATH,
) -> OverruledCounts | Problem:
    """Read the record's counts with both snapshots and the court bound."""

    for name, dated in ((source, snapshot_date), (caselaw_source, caselaw_snapshot_date)):
        problem = staging.source_problem(name, command_path)
        if problem is not None:
            return problem
        if not isinstance(dated, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", dated) is None:
            return Problem("overruled snapshot date is invalid", "Use a real YYYY-MM-DD date, then run "
                           f"{report.command(command_path)} again.")
        problem = staging.snapshot_problem(f"{name}-{dated}", command_path, subject="overruled")
        if problem is not None:
            return problem
    if source != OVERRULED_SOURCE:
        return Problem("overruled source is invalid", f"Use source {OVERRULED_SOURCE}, then run "
                       f"{report.command(command_path)} again.")
    problem = _court_problem(court, command_path)
    if problem is not None:
        return problem
    sql = "\n".join((
        worker.bind("v_source", source), worker.bind("v_snapshot_date", snapshot_date),
        worker.bind("v_caselaw_source", caselaw_source),
        worker.bind("v_caselaw_snapshot_date", caselaw_snapshot_date),
        worker.bind("v_court", court), OVERRULED_COUNTS_SQL,
    ))
    try:
        result = host.run(worker.psql_argv(rendered_dir), input=sql)
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"overruled counts command could not run ({type(exc).__name__})",
                       _logs_fix(rendered_dir, command_path))
    if result.returncode != 0:
        return Problem(f"overruled counts read failed (exit {result.returncode})",
                       _logs_fix(rendered_dir, command_path))
    try:
        value: object = json.loads(result.stdout.strip())
    except ValueError:
        return Problem("overruled counts row is invalid", _logs_fix(rendered_dir, command_path))
    counts = _counts_from_json(value)
    if counts is None:
        return Problem("overruled counts row is invalid", _logs_fix(rendered_dir, command_path))
    return counts


def _failure_valid(value: object, job_id: int, capture_file: str) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema", "job", "court", "reason", "table", "error", "at",
    }:
        return False
    reason, table, error = value["reason"], value["table"], value["error"]
    return (
        type(value["schema"]) is int and value["schema"] == 1
        and type(value["job"]) is int and value["job"] == job_id
        and value["court"] == OVERRULED_COURT
        and isinstance(reason, str) and reason in OVERRULED_FAILURE_REASONS
        and (table is None or isinstance(table, str) and table in (*STAGE_TABLES, capture_file))
        and (error is None or isinstance(error, str) and staging.ERROR_PATTERN.fullmatch(error) is not None)
        and agreement.aware_time(value["at"])
    )


def _stage_fix(label: str, work_root: PathLike, command_path: str) -> str:
    directories = ", ".join(
        str(staging.work_directory(label, source.name, work_root=work_root))
        for source in sources.SOURCES if source.carries_courts
    )
    return f"Remove {directories}, then run {report.command(command_path)} again."


def _failure_problem(
    reason: str, table: str | None, error: str | None, *, rendered_dir: PathLike,
    label: str, snapshot: str, work_root: PathLike, capture_file: str, command_path: str,
) -> Problem:
    install = report.command(command_path)
    if reason in {"missing-input", "input-mismatch"}:
        path = SNAPSHOTS_ROOT / snapshot / capture_file
        return Problem(
            f"overruled capture input is {reason}",
            f"Remove {path} and its {RECORD_SUFFIX} record, then run {install} again.",
        )
    if reason in {"missing-stage", "stage-mismatch"}:
        return Problem(f"overruled stage is {reason}", _stage_fix(label, work_root, command_path))
    if reason == "malformed":
        if table is None or table == capture_file:
            return Problem(
                f"overruled {capture_file} file is malformed",
                "Save a new capture and cut a new lockfile following "
                "docs/runbooks/release-files.md §10, using "
                f"{report.command('corpus cut')}, then run {install} with the new label.",
            )
        return Problem(f"overruled {table} table is malformed", _stage_fix(label, work_root, command_path))
    if reason == "busy":
        return Problem("overruled is busy with another job", f"Wait, then run {install} again.")
    return Problem(f"overruled {reason} failure ({error or 'unknown error'})", _logs_fix(rendered_dir, command_path))


def read_overruled(
    host: Host, rendered_dir: PathLike, job_id: int, *, label: str, snapshot: str,
    work_root: PathLike = WORK_ROOT, command_path: str = COMMAND_PATH,
) -> OverruledRead | Problem:
    """Combine a queue row with only that job's validated failure file."""

    for problem in (
        staging.label_problem(label, command_path, subject="overruled"),
        _snapshot_problem(snapshot, command_path),
    ):
        if problem is not None:
            return problem
    row = worker.read_job(host, rendered_dir, job_id)
    if isinstance(row, Problem):
        return Problem(row.problem, _logs_fix(rendered_dir, command_path))
    if row.status in {"todo", "doing", "succeeded"}:
        return OverruledRead(row, row.status == "succeeded", None, None)
    if row.status not in {"failed", "aborted", "cancelled"}:
        return Problem("overruled job has an unknown status", _logs_fix(rendered_dir, command_path))
    definition = sources.source_by_name(OVERRULED_SOURCE)
    if definition is None or definition.captured_file is None:
        return Problem("overruled captured source is not registered", _logs_fix(rendered_dir, command_path))
    work_dir = staging.work_directory(label, OVERRULED_WORK_DIRECTORY, work_root=work_root)
    path = work_dir / OVERRULED_FAILURE_NAME
    try:
        value: object = json.loads(host.read_text(path)) if host.exists(path) else None
    except (OSError, UnicodeError, ValueError):
        return Problem("overruled failure file could not be read", _logs_fix(rendered_dir, command_path))
    if value is not None and not isinstance(value, dict):
        return Problem("overruled failure file is invalid", _logs_fix(rendered_dir, command_path))
    if value is None or value.get("job") != job_id:
        reason, table, error = "local", None, None
    else:
        if not _failure_valid(value, job_id, definition.captured_file):
            return Problem("overruled failure file is invalid", _logs_fix(rendered_dir, command_path))
        reason, table, error = value["reason"], value["table"], value["error"]
    return OverruledRead(
        row, False,
        _failure_problem(
            reason, table, error, rendered_dir=rendered_dir, label=label, snapshot=snapshot,
            work_root=work_root, capture_file=definition.captured_file, command_path=command_path,
        ), reason,
    )
