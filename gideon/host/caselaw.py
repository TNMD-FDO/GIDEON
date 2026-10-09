"""Defer court opinion ingest and read its queue, failure, and count outcomes."""

import datetime
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from gideon.host import report, stack, staging, worker
from gideon.host.render.worker import (
    CASELAW_FAILURE_NAME,
    CASELAW_FAILURE_REASONS,
    CASELAW_LIMIT_MAX,
    CASELAW_QUEUE,
    CASELAW_TASK,
    COURT_PATTERN,
    DOCUMENT_FAILURE_REASONS,
    PRECEDENTIAL_VALUES,
    SECTION_TYPES,
    SEGMENT_PATTERN,
    STAGE_TABLES,
    TEXT_SOURCES,
    WORK_ROOT,
    WORKER_SERVICE_NAME,
)
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike

COMMAND_PATH = "corpus install"

# Null text sources and failure reasons are omitted from their maps.
CASELAW_COUNTS_SQL = """WITH selected AS (
    SELECT d.status, d.text_source, d.failure_reason, o.precedential
    FROM public.documents AS d
    JOIN public.opinions AS o ON o.doc_id = d.doc_id
    WHERE d.source = :'v_source'
      AND d.source_snapshot = :'v_snapshot_date'::date
      AND o.court = :'v_court'
)
SELECT jsonb_build_object(
    'opinions', (SELECT count(*) FROM selected),
    'by_status', COALESCE((
        SELECT jsonb_object_agg(status, n) FROM (
            SELECT status, count(*) AS n FROM selected GROUP BY status
        ) AS counts
    ), '{}'::jsonb),
    'by_text_source', COALESCE((
        SELECT jsonb_object_agg(text_source, n) FROM (
            SELECT text_source, count(*) AS n FROM selected
            WHERE text_source IS NOT NULL GROUP BY text_source
        ) AS counts
    ), '{}'::jsonb),
    'by_precedential', COALESCE((
        SELECT jsonb_object_agg(precedential, n) FROM (
            SELECT precedential, count(*) AS n FROM selected GROUP BY precedential
        ) AS counts
    ), '{}'::jsonb),
    'by_failure_reason', COALESCE((
        SELECT jsonb_object_agg(failure_reason, n) FROM (
            SELECT failure_reason, count(*) AS n FROM selected
            WHERE failure_reason IS NOT NULL GROUP BY failure_reason
        ) AS counts
    ), '{}'::jsonb)
);
"""

SECTION_COUNTS_SQL = """WITH ready_documents AS (
    SELECT d.doc_id
    FROM public.documents AS d
    JOIN public.opinions AS o ON o.doc_id = d.doc_id
    WHERE d.source = :'v_source'
      AND d.source_snapshot = :'v_snapshot_date'::date
      AND o.court = :'v_court'
      AND d.status = 'ready'
), typed AS (
    SELECT s.section_type, count(*) AS sections, sum(s.char_end - s.char_start) AS chars
    FROM public.sections AS s
    JOIN ready_documents AS d ON d.doc_id = s.doc_id
    GROUP BY s.section_type
)
SELECT jsonb_build_object(
    'ready', (SELECT count(*) FROM ready_documents),
    'sectioned', (
        SELECT count(DISTINCT s.doc_id)
        FROM public.sections AS s
        JOIN ready_documents AS d ON d.doc_id = s.doc_id
    ),
    'sections_by_type', COALESCE((
        SELECT jsonb_object_agg(section_type, sections) FROM typed
    ), '{}'::jsonb),
    'chars_by_type', COALESCE((
        SELECT jsonb_object_agg(section_type, chars) FROM typed
    ), '{}'::jsonb)
);
"""

ANCHOR_COUNTS_SQL = """WITH ready_documents AS (
    SELECT d.doc_id, COALESCE(d.text_source, '') AS text_source, d.anchored_at
    FROM public.documents AS d
    JOIN public.opinions AS o ON o.doc_id = d.doc_id
    WHERE d.source = :'v_source'
      AND d.source_snapshot = :'v_snapshot_date'::date
      AND o.court = :'v_court'
      AND d.status = 'ready'
), section_chars AS (
    SELECT s.doc_id, sum(s.char_end - s.char_start) AS chars
    FROM public.sections AS s
    JOIN ready_documents AS d ON d.doc_id = s.doc_id
    GROUP BY s.doc_id
), scheme_chars AS (
    SELECT a.doc_id, a.attrs ->> 'scheme' AS scheme,
           sum(a.char_end - a.char_start) AS chars
    FROM public.anchors AS a
    JOIN ready_documents AS d ON d.doc_id = a.doc_id
    WHERE a.kind = 'reporter_page'
    GROUP BY a.doc_id, a.attrs ->> 'scheme'
), anchor_chars AS (
    SELECT doc_id, max(chars) AS chars
    FROM scheme_chars
    GROUP BY doc_id
), by_source AS (
    SELECT d.text_source, count(*) AS documents, count(a.doc_id) AS with_anchors,
           sum(COALESCE(s.chars, 0)) AS chars,
           sum(COALESCE(a.chars, 0)) AS anchored_chars
    FROM ready_documents AS d
    LEFT JOIN section_chars AS s ON s.doc_id = d.doc_id
    LEFT JOIN anchor_chars AS a ON a.doc_id = d.doc_id
    GROUP BY d.text_source
)
SELECT jsonb_build_object(
    'ready', (SELECT count(*) FROM ready_documents),
    'anchored', (SELECT count(*) FROM ready_documents WHERE anchored_at IS NOT NULL),
    'by_text_source', COALESCE((
        SELECT jsonb_object_agg(text_source, jsonb_build_object(
            'documents', documents, 'with_anchors', with_anchors,
            'chars', chars, 'anchored_chars', anchored_chars
        )) FROM by_source
    ), '{}'::jsonb)
);
"""


@dataclass(frozen=True, slots=True)
class CaselawRead:
    """One queue row and its completed or failed court ingest."""

    job: worker.JobRow
    done: bool
    failure: Problem | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class CourtCounts:
    """Opinion totals and grouped counts for one source, snapshot, and court."""

    opinions: int
    by_status: dict[str, int]
    by_text_source: dict[str, int]
    by_precedential: dict[str, int]
    by_failure_reason: dict[str, int]


@dataclass(frozen=True, slots=True)
class SectionCounts:
    """Ready document coverage and section totals for one court."""

    ready: int
    sectioned: int
    sections_by_type: dict[str, int]
    chars_by_type: dict[str, int]


@dataclass(frozen=True, slots=True)
class SourceCoverage:
    """Ready document and reporter-page coverage for one text source."""

    documents: int
    with_anchors: int
    chars: int
    anchored_chars: int


@dataclass(frozen=True, slots=True)
class AnchorCounts:
    """Ready and anchored document counts with coverage by text source."""

    ready: int
    anchored: int
    by_text_source: dict[str, SourceCoverage]


def _logs_fix(rendered_dir: PathLike, command_path: str) -> str:
    return (
        f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, "
        f"then run {report.command(command_path)} again."
    )


def _court_problem(court: object, command_path: str) -> Problem | None:
    if isinstance(court, str) and re.fullmatch(COURT_PATTERN, court) is not None:
        return None
    return Problem(
        "caselaw court is invalid",
        "Use a lowercase court id of one to 32 letters or digits, then run "
        f"{report.command(command_path)} again.",
    )


def _limit_problem(limit: object, command_path: str) -> Problem | None:
    if limit is None or (
        isinstance(limit, int) and not isinstance(limit, bool)
        and 1 <= limit <= CASELAW_LIMIT_MAX
    ):
        return None
    return Problem(
        "caselaw limit is invalid",
        f"Use an integer from 1 to {CASELAW_LIMIT_MAX} or omit it, then run "
        f"{report.command(command_path)} again.",
    )


def defer_caselaw(
    host: Host, rendered_dir: PathLike, *, label: str, snapshot: str,
    court: str, limit: int | None = None, command_path: str = COMMAND_PATH,
) -> int | Problem:
    """Validate one court job and defer it under its label and court lock."""

    for problem in (
        staging.label_problem(label, command_path, subject="caselaw"),
        staging.snapshot_problem(snapshot, command_path, subject="caselaw"),
        _court_problem(court, command_path),
        _limit_problem(limit, command_path),
    ):
        if problem is not None:
            return problem
    args: dict[str, object] = {"label": label, "snapshot": snapshot, "court": court}
    if limit is not None:
        args["limit"] = limit
    return worker.defer(
        host, rendered_dir, task=CASELAW_TASK, queue=CASELAW_QUEUE,
        args=args, lock=f"caselaw-{label}-{court}",
    )


def _failure_valid(value: object, job_id: int, court: str) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema", "job", "court", "reason", "table", "error", "at",
    }:
        return False
    reason, table, error, at = (
        value["reason"], value["table"], value["error"], value["at"]
    )
    if (
        type(value["schema"]) is not int or value["schema"] != 1
        or type(value["job"]) is not int or value["job"] != job_id
        or value["court"] != court
        or not isinstance(reason, str) or reason not in CASELAW_FAILURE_REASONS
        or (table is not None and (not isinstance(table, str) or table not in STAGE_TABLES))
        or (error is not None and (
            not isinstance(error, str) or staging.ERROR_PATTERN.fullmatch(error) is None
        ))
        or not isinstance(at, str)
    ):
        return False
    try:
        parsed = datetime.datetime.fromisoformat(at)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _failure_problem(
    reason: str, table: str | None, error: str | None,
    rendered_dir: PathLike, work_dir: Path, command_path: str,
) -> Problem:
    install = report.command(command_path)
    if reason in {"missing-stage", "stage-mismatch"}:
        return Problem(
            f"caselaw stage is {reason}",
            f"Remove {work_dir}, then run {install} again.",
        )
    if reason == "malformed":
        return Problem(f"caselaw {table or 'stage'} file is malformed", _logs_fix(rendered_dir, command_path))
    if reason == "store":
        return Problem(
            "caselaw store failed",
            f"Run {report.command('host provision --only disk-layout')}, "
            f"then {report.command('apply')}, then {install} again.",
        )
    if reason in {"database", "local"}:
        return Problem(
            f"caselaw {reason} failure ({error or 'unknown error'})",
            f"Run {report.command('host provision')}, then {report.command('apply')}, "
            f"then {install} again.",
        )
    if reason == "busy":
        return Problem("caselaw is busy with another job", f"Wait, then run {install} again.")
    if reason in {"segmenter", "anchors"}:
        return Problem(
            f"caselaw {reason} failure ({error or 'unknown error'})",
            f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, report the defect, "
            f"then run {install} again after the fix.",
        )
    if reason == "text-mismatch":
        return Problem(
            "caselaw text no longer matches its recorded canonical text",
            f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, make a new corpus cut "
            f"with {report.command('corpus cut')}, then run {install} for its new label.",
        )
    return Problem(f"caselaw failed: {reason}", _logs_fix(rendered_dir, command_path))


def read_caselaw(
    host: Host, rendered_dir: PathLike, job_id: int, *, label: str,
    snapshot: str, court: str, work_root: PathLike = WORK_ROOT,
    command_path: str = COMMAND_PATH,
) -> CaselawRead | Problem:
    """Combine a queue row with this job's filed court failure."""

    for problem in (
        staging.label_problem(label, command_path, subject="caselaw"),
        staging.snapshot_problem(snapshot, command_path, subject="caselaw"),
        _court_problem(court, command_path),
    ):
        if problem is not None:
            return problem
    row = worker.read_job(host, rendered_dir, job_id)
    if isinstance(row, Problem):
        return row
    if row.status in {"todo", "doing"}:
        return CaselawRead(row, False, None, None)
    if row.status == "succeeded":
        return CaselawRead(row, True, None, None)
    if row.status not in {"failed", "aborted", "cancelled"}:
        return Problem("caselaw job has an unknown status", _logs_fix(rendered_dir, command_path))
    source = snapshot[:-staging.DATE_SUFFIX_LENGTH]
    work_dir = staging.work_directory(label, source, work_root=work_root)
    path = work_dir / f"{court}.{CASELAW_FAILURE_NAME}"
    try:
        value: object = json.loads(host.read_text(path)) if host.exists(path) else None
    except (OSError, UnicodeError, ValueError):
        return Problem("caselaw failure file could not be read", _logs_fix(rendered_dir, command_path))
    if value is not None and not isinstance(value, dict):
        return Problem("caselaw failure file is invalid", _logs_fix(rendered_dir, command_path))
    # A prior job's file does not describe this queue row.
    if value is None or value.get("job") != job_id:
        reason = "local"
        failure = _failure_problem(reason, None, None, rendered_dir, work_dir, command_path)
        return CaselawRead(row, False, failure, reason)
    if not _failure_valid(value, job_id, court):
        return Problem("caselaw failure file is invalid", _logs_fix(rendered_dir, command_path))
    reason = value["reason"]
    assert isinstance(reason, str)
    failure = _failure_problem(
        reason, value["table"], value["error"], rendered_dir, work_dir, command_path,
    )
    return CaselawRead(row, False, failure, reason)


def _counts_map(value: object, allowed: frozenset[str]) -> dict[str, int] | None:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or key not in allowed
        or type(count) is not int or count < 0
        for key, count in value.items()
    ):
        return None
    return value


def _counts_from_json(value: object) -> CourtCounts | None:
    if not isinstance(value, dict) or set(value) != {
        "opinions", "by_status", "by_text_source", "by_precedential", "by_failure_reason",
    } or type(value["opinions"]) is not int or value["opinions"] < 0:
        return None
    status = _counts_map(value["by_status"], frozenset({
        "processing", "ready", "failed", "withdrawn",
    }))
    sources = _counts_map(value["by_text_source"], frozenset(TEXT_SOURCES))
    precedential = _counts_map(value["by_precedential"], frozenset(PRECEDENTIAL_VALUES))
    reasons = _counts_map(value["by_failure_reason"], DOCUMENT_FAILURE_REASONS)
    if any(item is None for item in (status, sources, precedential, reasons)):
        return None
    assert status is not None and sources is not None and precedential is not None and reasons is not None
    return CourtCounts(value["opinions"], status, sources, precedential, reasons)


def _read_court_row(
    host: Host, rendered_dir: PathLike, statement: str, noun: str, *, source: str,
    snapshot_date: str, court: str, command_path: str,
) -> object | Problem:
    if not isinstance(source, str) or re.fullmatch(SEGMENT_PATTERN, source) is None:
        return Problem("caselaw source is invalid", f"Use one source name, then run {report.command(command_path)} again.")
    try:
        if not isinstance(snapshot_date, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", snapshot_date) is None:
            raise ValueError("date form")
        datetime.date.fromisoformat(snapshot_date)
    except ValueError:
        return Problem(
            "caselaw snapshot date is invalid",
            f"Use a real YYYY-MM-DD date, then run {report.command(command_path)} again.",
        )
    problem = _court_problem(court, command_path)
    if problem is not None:
        return problem
    sql = "\n".join((
        worker.bind("v_source", source),
        worker.bind("v_snapshot_date", snapshot_date),
        worker.bind("v_court", court), statement,
    ))
    try:
        result = host.run(worker.psql_argv(rendered_dir), input=sql)
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(
            f"caselaw {noun} command could not run ({type(exc).__name__})",
            f"Run {report.command('host provision')}, then {report.command('apply')}, "
            f"then {report.command(command_path)} again.",
        )
    if result.returncode == 127:
        return Problem(
            f"caselaw {noun} command is unavailable (exit 127)",
            f"Run {report.command('host provision')}, then {report.command('apply')}, "
            f"then {report.command(command_path)} again.",
        )
    if result.returncode != 0:
        return Problem(
            f"caselaw {noun} read failed (exit {result.returncode}): "
            f"{report.command_detail(result)}",
            _logs_fix(rendered_dir, command_path),
        )
    try:
        value: object = json.loads(result.stdout.strip())
    except ValueError:
        return Problem(f"caselaw {noun} row is invalid", _logs_fix(rendered_dir, command_path))
    return value


def read_counts(
    host: Host, rendered_dir: PathLike, *, source: str, snapshot_date: str,
    court: str, command_path: str = COMMAND_PATH,
) -> CourtCounts | Problem:
    """Read one JSON count row; null text sources and reasons are omitted."""

    value = _read_court_row(
        host, rendered_dir, CASELAW_COUNTS_SQL, "counts", source=source,
        snapshot_date=snapshot_date, court=court, command_path=command_path,
    )
    if isinstance(value, Problem):
        return value
    counts = _counts_from_json(value)
    if counts is None:
        return Problem("caselaw counts row is invalid", _logs_fix(rendered_dir, command_path))
    return counts


def _section_counts_from_json(value: object) -> SectionCounts | None:
    if not isinstance(value, dict) or set(value) != {
        "ready", "sectioned", "sections_by_type", "chars_by_type",
    }:
        return None
    ready, sectioned = value["ready"], value["sectioned"]
    if (type(ready) is not int or ready < 0 or type(sectioned) is not int
            or sectioned < 0 or sectioned > ready):
        return None
    allowed = frozenset(SECTION_TYPES)
    section_totals = _counts_map(value["sections_by_type"], allowed)
    char_totals = _counts_map(value["chars_by_type"], allowed)
    if section_totals is None or char_totals is None:
        return None
    return SectionCounts(ready, sectioned, section_totals, char_totals)


def read_section_counts(
    host: Host, rendered_dir: PathLike, *, source: str, snapshot_date: str,
    court: str, command_path: str = COMMAND_PATH,
) -> SectionCounts | Problem:
    """Read ready document coverage and section totals for one court."""

    value = _read_court_row(
        host, rendered_dir, SECTION_COUNTS_SQL, "sections", source=source,
        snapshot_date=snapshot_date, court=court, command_path=command_path,
    )
    if isinstance(value, Problem):
        return value
    counts = _section_counts_from_json(value)
    if counts is None:
        return Problem("caselaw sections row is invalid", _logs_fix(rendered_dir, command_path))
    return counts


def _anchor_counts_from_json(value: object) -> AnchorCounts | None:
    if not isinstance(value, dict) or set(value) != {
        "ready", "anchored", "by_text_source",
    }:
        return None
    ready, anchored = value["ready"], value["anchored"]
    if (type(ready) is not int or ready < 0 or type(anchored) is not int
            or anchored < 0 or anchored > ready):
        return None
    sources = value["by_text_source"]
    if not isinstance(sources, dict):
        return None
    coverage: dict[str, SourceCoverage] = {}
    for source, row in sources.items():
        if not isinstance(source, str) or source not in TEXT_SOURCES or not isinstance(row, dict):
            return None
        if set(row) != {"documents", "with_anchors", "chars", "anchored_chars"}:
            return None
        documents, with_anchors = row["documents"], row["with_anchors"]
        chars, anchored_chars = row["chars"], row["anchored_chars"]
        if any(type(number) is not int or number < 0 for number in (
            documents, with_anchors, chars, anchored_chars,
        )) or with_anchors > documents or anchored_chars > chars:
            return None
        coverage[source] = SourceCoverage(documents, with_anchors, chars, anchored_chars)
    return AnchorCounts(ready, anchored, coverage)


def read_anchor_counts(
    host: Host, rendered_dir: PathLike, *, source: str, snapshot_date: str,
    court: str, command_path: str = COMMAND_PATH,
) -> AnchorCounts | Problem:
    """Read ready document anchoring and reporter-page coverage for one court."""

    value = _read_court_row(
        host, rendered_dir, ANCHOR_COUNTS_SQL, "anchors", source=source,
        snapshot_date=snapshot_date, court=court, command_path=command_path,
    )
    if isinstance(value, Problem):
        return value
    counts = _anchor_counts_from_json(value)
    if counts is None:
        return Problem("caselaw anchors row is invalid", _logs_fix(rendered_dir, command_path))
    return counts
