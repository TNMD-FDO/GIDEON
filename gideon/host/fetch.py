"""Defer corpus fetches and read their queue and file outcomes on the host."""

import datetime
import hashlib
import json
import math
import re
import stat
from dataclasses import dataclass, fields
from pathlib import Path
from typing import TypeGuard
from urllib.parse import urlsplit

from gideon.host import report, stack, worker
from gideon.host.render.worker import (
    DNS_PATTERN,
    FAILURE_REASONS,
    FAILURE_SUFFIX,
    FETCH_QUEUE,
    FETCH_TASK,
    FRESH_FORM,
    KEPT_FORM,
    LANE_COUNT,
    RECORD_SUFFIX,
    RESERVED_SUFFIXES,
    RESOLVE_DIR,
    SEGMENT_PATTERN,
    SNAPSHOTS_ROOT,
    SOURCE_PATTERN,
    URL_MAX_LENGTH,
    WORKER_SERVICE_NAME,
)
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike


@dataclass(frozen=True, slots=True)
class FetchRecord:
    """Every field of a completed fetch's file record."""

    state: str
    form: str
    host: str
    path: str
    etag: str | None
    last_modified: str | None
    total: int | None
    durable: int
    size: int
    sha256: str
    fetched_at: str
    job: int
    seconds: float
    resumes: int
    schema: int


@dataclass(frozen=True, slots=True)
class FetchRead:
    """One queue row and its completed record or failure."""

    job: worker.JobRow
    record: FetchRecord | None
    failure: Problem | None


def _destination_problem(destination: str, form: str) -> Problem | None:
    if form not in {KEPT_FORM, FRESH_FORM}:
        return Problem(
            "fetch form is invalid",
            "Use kept for a dated snapshot or fresh for a path under resolve/.",
        )
    parts = destination.split("/")
    if not 2 <= len(parts) <= 8 or any(
        re.fullmatch(SEGMENT_PATTERN, part) is None
        or part in {".", ".."}
        or part.endswith(RESERVED_SUFFIXES)
        for part in parts
    ):
        return Problem(
            "fetch destination is invalid",
            "Use two to eight slash-separated names, each starting with a letter "
            "or digit, with only letters, digits, dots, underscores, and hyphens; "
            "do not use a reserved file suffix.",
        )
    if form == KEPT_FORM:
        if parts[0] == RESOLVE_DIR or re.fullmatch(SOURCE_PATTERN, parts[0]) is None:
            return Problem(
                "fetch destination is invalid for the kept form",
                "Use <source>-YYYY-MM-DD/<path> for a kept file.",
            )
        try:
            datetime.date.fromisoformat(parts[0][-10:])
        except ValueError:
            return Problem(
                "fetch destination has an invalid date",
                "Use a real YYYY-MM-DD date in <source>-YYYY-MM-DD/<path>.",
            )
    elif parts[0] != RESOLVE_DIR:
        return Problem(
            "fetch destination is invalid for the fresh form",
            "Use resolve/<path> for a fresh file.",
        )
    return None


def _url_problem(url: str) -> Problem | None:
    fix = (
        "Use an HTTPS URL of at most " + str(URL_MAX_LENGTH)
        + " characters with a lowercase DNS host, no userinfo or fragment, "
        "and no port other than 443."
    )
    if (
        len(url) > URL_MAX_LENGTH or not url.startswith("https://")
        or any(ord(char) < 33 for char in url)
    ):
        return Problem("fetch URL is invalid", fix)
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return Problem("fetch URL is invalid", fix)
    if (
        parsed.scheme != "https" or host is None
        or parsed.username is not None or parsed.password is not None
        or parsed.fragment or port not in {None, 443}
        or re.fullmatch(DNS_PATTERN, host) is None
        or len(host) > 253 or any(len(label) > 63 for label in host.split("."))
        or parsed.netloc != (host if port is None else f"{host}:443")
    ):
        return Problem("fetch URL is invalid", fix)
    return None


def snapshot_destination(source: str, date: datetime.date | str, path: str) -> str:
    """Compose one dated source snapshot's relative file path."""

    day = date.isoformat() if isinstance(date, datetime.date) else date
    destination = f"{source}-{day}/{path}"
    problem = _destination_problem(destination, KEPT_FORM)
    if problem is not None:
        raise ValueError(problem.problem)
    return destination


def resolve_destination(path: str) -> str:
    """Compose one relative file path under the reserved resolve directory."""

    destination = f"{RESOLVE_DIR}/{path}"
    problem = _destination_problem(destination, FRESH_FORM)
    if problem is not None:
        raise ValueError(problem.problem)
    return destination


def defer_fetch(
    host: Host,
    rendered_dir: PathLike,
    *,
    destination: str,
    url: str,
    form: str,
) -> int | Problem:
    """Validate a public source address and defer its worker transfer."""

    problem = _destination_problem(destination, form)
    if problem is not None:
        return problem
    problem = _url_problem(url)
    if problem is not None:
        return problem
    if form == KEPT_FORM:
        lane = int.from_bytes(hashlib.sha256(destination.encode()).digest(), "big") % LANE_COUNT
        lock = f"fetch-lane-{lane}"
    else:
        lock = destination
    return worker.defer(
        host,
        rendered_dir,
        task=FETCH_TASK,
        queue=FETCH_QUEUE,
        args={"destination": destination, "url": url, "form": form},
        lock=lock,
    )


def _logs_fix(rendered_dir: PathLike) -> str:
    return f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, then retry."


def _record_path(snapshots_root: PathLike, destination: str, suffix: str) -> Path:
    return Path(snapshots_root) / f"{destination}{suffix}"


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_json(host: Host, path: Path, kind: str) -> dict[str, object] | None | Problem:
    try:
        if not host.exists(path):
            return None
        value: object = json.loads(host.read_text(path))
    except (OSError, UnicodeError, ValueError):
        return Problem(f"fetch {kind} file could not be read", "Read the worker logs, then retry.")
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return Problem(f"fetch {kind} file is invalid", "Read the worker logs, then retry.")
    return value


def _record_from_json(value: dict[str, object], form: str) -> FetchRecord | None:
    if set(value) != {field.name for field in fields(FetchRecord)}:
        return None
    if (
        not _is_int(value["schema"]) or value["schema"] != 1
        or value["state"] != "whole" or value["form"] != form
    ):
        return None
    size = value["size"]
    durable = value["durable"]
    total = value["total"]
    sha256 = value["sha256"]
    seconds = value["seconds"]
    if (
        not _is_int(size) or size < 0
        or not _is_int(durable) or durable != size
        or (total is not None and (not _is_int(total) or total != size))
        or not isinstance(sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        or not isinstance(value["host"], str)
        or re.fullmatch(DNS_PATTERN, value["host"]) is None
        or not isinstance(value["path"], str)
        or not value["path"].startswith("/")
        or "?" in value["path"] or "#" in value["path"]
        or (value["etag"] is not None and not isinstance(value["etag"], str))
        or (value["last_modified"] is not None and not isinstance(value["last_modified"], str))
        or not isinstance(value["fetched_at"], str)
        or not _is_int(value["job"]) or value["job"] < 1
        or not isinstance(seconds, (int, float)) or isinstance(seconds, bool)
        or not math.isfinite(seconds) or seconds < 0
        or not _is_int(value["resumes"]) or value["resumes"] < 0
    ):
        return None
    try:
        fetched_at = datetime.datetime.fromisoformat(value["fetched_at"])
    except ValueError:
        return None
    if fetched_at.tzinfo is None:
        return None
    return FetchRecord(**value)  # type: ignore[arg-type]


def read_record(
    host: Host,
    destination: str,
    *,
    snapshots_root: PathLike = SNAPSHOTS_ROOT,
) -> FetchRecord | None | Problem:
    """Read a whole file's record through the host seam, if one is present."""

    form = FRESH_FORM if destination.startswith(f"{RESOLVE_DIR}/") else KEPT_FORM
    problem = _destination_problem(destination, form)
    if problem is not None:
        return problem
    path = _record_path(snapshots_root, destination, RECORD_SUFFIX)
    value = _read_json(host, path, "record")
    if isinstance(value, Problem) or value is None:
        return value
    if value.get("state") == "partial":
        if (
            set(value) != {field.name for field in fields(FetchRecord)}
            or not _is_int(value["schema"]) or value["schema"] != 1
            or value["form"] != form
            or not _is_int(value["durable"]) or value["durable"] < 0
            or value["size"] is not None or value["sha256"] is not None
            or value["fetched_at"] is not None
        ):
            return Problem("fetch record file is invalid", "Read the worker logs, then retry.")
        return None
    record = _record_from_json(value, form)
    if record is None:
        return Problem("fetch record file is invalid", "Read the worker logs, then retry.")
    target = _record_path(snapshots_root, destination, "")
    try:
        if not host.exists(target):
            return None
        info = host.stat(target)
    except OSError:
        return Problem("fetch file could not be read", "Read the worker logs, then retry.")
    if not stat.S_ISREG(info.st_mode) or info.st_size != record.size:
        return Problem("fetch file does not match its record", "Read the worker logs, then retry.")
    return record


def _failure_from_json(value: dict[str, object]) -> dict[str, object] | None:
    if set(value) != {"schema", "job", "reason", "host", "status", "error", "at"}:
        return None
    reason = value["reason"]
    host = value["host"]
    status = value["status"]
    error = value["error"]
    if (
        not _is_int(value["schema"]) or value["schema"] != 1
        or not _is_int(value["job"]) or value["job"] < 1
        or not isinstance(reason, str) or reason not in FAILURE_REASONS
        or (host is not None and (
            not isinstance(host, str) or re.fullmatch(DNS_PATTERN, host) is None
        ))
        or (reason == "refused-host" and host is None)
        or (status is not None and (
            not _is_int(status) or not 100 <= status <= 599
        ))
        or (error is not None and (
            not isinstance(error, str)
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", error) is None
        ))
        or not isinstance(value["at"], str)
    ):
        return None
    try:
        at = datetime.datetime.fromisoformat(value["at"])
    except ValueError:
        return None
    return value if at.tzinfo is not None else None


def _failure_problem(value: dict[str, object], rendered_dir: PathLike) -> Problem:
    reason = value["reason"]
    host = value["host"]
    status = value["status"]
    if reason == "refused-host":
        named = f" {host}" if host is not None else ""
        return Problem(
            f"the egress service refused host{named} outside the corpus allowlist group",
            "Add the corpus host to the corpus group in config/egress.yaml in a release, "
            f"then run {report.command('apply')}.",
        )
    if reason == "changed":
        return Problem(
            "the upstream object changed during transfer; its partial was discarded",
            "Fetch this destination again.",
        )
    location = f" host {host}" if host is not None else ""
    outcome = f" status {status}" if status is not None else ""
    return Problem(f"fetch failed: {reason}{location}{outcome}", _logs_fix(rendered_dir))


def read_fetch(
    host: Host,
    rendered_dir: PathLike,
    job_id: int,
    *,
    destination: str,
    snapshots_root: PathLike = SNAPSHOTS_ROOT,
) -> FetchRead | Problem:
    """Combine one queue row with its whole record or filed failure."""

    form = FRESH_FORM if destination.startswith(f"{RESOLVE_DIR}/") else KEPT_FORM
    problem = _destination_problem(destination, form)
    if problem is not None:
        return problem
    row = worker.read_job(host, rendered_dir, job_id)
    if isinstance(row, Problem):
        return row
    if row.status in {"todo", "doing"}:
        return FetchRead(row, None, None)
    if row.status == "succeeded":
        record = read_record(host, destination, snapshots_root=snapshots_root)
        if isinstance(record, Problem):
            return Problem(record.problem, _logs_fix(rendered_dir))
        if record is None:
            return Problem("fetch job succeeded without a whole file and record", _logs_fix(rendered_dir))
        return FetchRead(row, record, None)
    if row.status in {"failed", "aborted", "cancelled"}:
        path = _record_path(snapshots_root, destination, FAILURE_SUFFIX)
        value = _read_json(host, path, "failure")
        if isinstance(value, Problem):
            return Problem(value.problem, _logs_fix(rendered_dir))
        # A failure file from another job for this destination is not this job's.
        if value is not None and value.get("job") == job_id:
            failure = _failure_from_json(value)
            if failure is None:
                return Problem("fetch failure file is invalid", _logs_fix(rendered_dir))
            return FetchRead(row, None, _failure_problem(failure, rendered_dir))
        return FetchRead(
            row, None,
            Problem(f"fetch job {job_id} {row.status} without its failure file", _logs_fix(rendered_dir)),
        )
    return Problem("fetch job has an unknown status", _logs_fix(rendered_dir))
