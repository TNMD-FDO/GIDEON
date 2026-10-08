"""Stage the pinned corpus dump as byte-preserved per-court files."""

import bz2
import csv
import fcntl
import io
import json
import logging
import math
import os
import re
import shutil
import sys
import time
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import BinaryIO, Final, Literal

from . import fetch
from .fetch import (
    DIR_MODE,
    FILE_MODE,
    PARTIAL_MODE,
    PARTIAL_SUFFIX,
    SEGMENT_PATTERN,
    TEMP_SUFFIX,
)

STAGE_TASK: Final = "gideon.worker.tasks.stage"
STAGE_QUEUE: Final = "stage"
WORK_ROOT: Final = Path("/data/work")
STAGE_RECORD_NAME: Final = "stage.json"
STAGE_FAILURE_NAME: Final = "stage-failed.json"
# The lock outlives the partial directory, which each run removes and remakes.
LOCK_SUFFIX: Final = ".stage.lock"
STAGE_TABLES: Final = (
    "courts", "dockets", "opinion-clusters", "citations", "opinions",
)
COURT_PATTERN: Final = r"[a-z0-9]{1,32}"
STAGE_FAILURE_REASONS: Final = frozenset({
    "invalid", "missing-input", "input-mismatch", "unknown-court",
    "malformed", "local", "busy",
})
LABEL_PATTERN: Final = r"corpus-([0-9]{4}-[0-9]{2}-[0-9]{2})"
SNAPSHOT_PATTERN: Final = rf"{SEGMENT_PATTERN}-([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})"
DIGEST_PATTERN: Final = r"[0-9a-f]{64}"
# The snapshot name's date suffix, `-YYYY-MM-DD`, follows its source.
DATE_SUFFIX_LENGTH: Final = 11
REQUIRED_COLUMNS: Final = {
    "courts": ("id",),
    "dockets": ("id", "court_id"),
    "opinion-clusters": ("id", "docket_id"),
    "citations": ("cluster_id",),
    "opinions": ("cluster_id",),
}
# A record of an opinion's text runs to megabytes; the bound is the dump's.
csv.field_size_limit(sys.maxsize)
logger = logging.getLogger(__name__)


def _clock() -> datetime:
    return datetime.now(UTC)


class StageFailure(Exception):
    """A safe stage refusal with a reason from the closed vocabulary."""

    def __init__(
        self, reason: str, *, table: str | None = None,
        court: str | None = None, error: str | None = None,
    ) -> None:
        if reason not in STAGE_FAILURE_REASONS:
            raise ValueError("unknown stage failure reason")
        self.reason = reason
        self.table = table
        self.court = court
        self.error = error
        super().__init__(reason)


@dataclass(frozen=True)
class StageRecord:
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
    schema: int = 1


@dataclass(frozen=True)
class StageFailureRecord:
    job: int
    reason: str
    table: str | None
    court: str | None
    error: str | None
    at: str
    schema: int = 1


def _dated_name(value: object, pattern: str) -> bool:
    if not isinstance(value, str):
        return False
    match = re.fullmatch(pattern, value)
    if match is None:
        return False
    try:
        date.fromisoformat(match.group(1))
    except ValueError:
        return False
    return True


def _valid_courts(courts: object) -> bool:
    return (
        isinstance(courts, list) and bool(courts)
        and all(isinstance(court, str) and re.fullmatch(COURT_PATTERN, court)
                for court in courts)
        and courts == sorted(set(courts))
    )


def _valid_inputs(inputs: object, *, sizes: bool) -> bool:
    if not isinstance(inputs, dict) or set(inputs) != set(STAGE_TABLES):
        return False
    for item in inputs.values():
        if not isinstance(item, dict) or set(item) != ({"path", "sha256", "size"} if sizes else {"path", "sha256"}):
            return False
        path = item["path"]
        digest = item["sha256"]
        if (
            not isinstance(path, str)
            or re.fullmatch(SEGMENT_PATTERN, path) is None
            or path in {".", ".."} or path.endswith(fetch.RESERVED_SUFFIXES)
            or not isinstance(digest, str)
            or re.fullmatch(DIGEST_PATTERN, digest) is None
        ):
            return False
        if sizes and (type(item["size"]) is not int or item["size"] < 0):
            return False
    return True


def validate_arguments(
    label: object, snapshot: object, courts: object, inputs: object,
) -> None:
    """Refuse queue arguments outside the lockfile and kept-fetch grammars."""

    if (
        not _dated_name(label, LABEL_PATTERN)
        or not _dated_name(snapshot, SNAPSHOT_PATTERN)
        or not _valid_courts(courts)
        or not _valid_inputs(inputs, sizes=False)
    ):
        raise StageFailure("invalid")


def _safe_path(root: Path, parts: tuple[str, ...], siblings: tuple[str, ...] = ()) -> Path:
    if root.is_symlink() or not root.is_dir():
        raise StageFailure("local", error="FileNotFoundError")
    path = root
    for part in parts:
        path /= part
        if path.is_symlink():
            raise StageFailure("invalid")
    if not path.resolve(strict=False).is_relative_to(root.resolve(strict=True)):
        raise StageFailure("invalid")
    if any(path.with_name(path.name + suffix).is_symlink() for suffix in siblings):
        raise StageFailure("invalid")
    return path


def snapshot_path(root: Path, snapshot: str) -> Path:
    """Resolve a kept snapshot directory without following a symbolic link."""

    if not _dated_name(snapshot, SNAPSHOT_PATTERN):
        raise StageFailure("invalid")
    return _safe_path(root, (snapshot,))


def work_path(root: Path, label: str, snapshot: str) -> Path:
    """Resolve a label's source directory and its sidecars without symlinks."""

    if not _dated_name(label, LABEL_PATTERN):
        raise StageFailure("invalid")
    if not _dated_name(snapshot, SNAPSHOT_PATTERN):
        raise StageFailure("invalid")
    path = _safe_path(
        root, (label, snapshot[:-DATE_SUFFIX_LENGTH]),
        (PARTIAL_SUFFIX, f".{STAGE_FAILURE_NAME}", LOCK_SUFFIX),
    )
    if (path / STAGE_RECORD_NAME).is_symlink():
        raise StageFailure("invalid")
    return path


def input_records(
    snapshot_dir: Path, inputs: dict[str, dict[str, str]],
) -> dict[str, dict[str, str | int]]:
    """Read every whole fetch record and compare its pinned digest and bytes."""

    if not _valid_inputs(inputs, sizes=False):
        raise StageFailure("invalid")
    result: dict[str, dict[str, str | int]] = {}
    for table in STAGE_TABLES:
        item = inputs[table]
        path = snapshot_dir / item["path"]
        if path.is_symlink() or path.with_name(path.name + fetch.RECORD_SUFFIX).is_symlink():
            raise StageFailure("invalid", table=table)
        try:
            record = fetch.read_record(path.with_name(path.name + fetch.RECORD_SUFFIX))
        except fetch.FetchFailure as exc:
            raise StageFailure("invalid", table=table, error=exc.error) from exc
        if record is None or record.state != "whole" or not path.is_file():
            raise StageFailure("missing-input", table=table)
        # A whole record's size is validated as an int equal to its offset.
        size = record.durable
        if (
            record.form != fetch.KEPT_FORM or record.sha256 != item["sha256"]
            or path.stat().st_size != size
        ):
            raise StageFailure("input-mismatch", table=table)
        result[table] = {"path": item["path"], "sha256": item["sha256"], "size": size}
    return result


@contextmanager
def lock_partial(path: Path) -> Iterator[None]:
    """Hold the label's partial marker exclusively for a stage run."""

    if path.is_symlink():
        raise StageFailure("invalid")
    descriptor = os.open(path, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, PARTIAL_MODE)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StageFailure("busy") from exc
        os.fchmod(descriptor, PARTIAL_MODE)
        yield
    finally:
        os.close(descriptor)


def _write_json(path: Path, value: Mapping[str, object], mode: int) -> None:
    temporary = path.with_name(path.name + TEMP_SUFFIX)
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        os.fchmod(output.fileno(), mode)
        json.dump(value, output, sort_keys=True, separators=(",", ":"))
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    fetch._flush_directory(path.parent)


def write_record(path: Path, record: StageRecord) -> None:
    """Atomically write a whole stage record inside the partial directory."""

    _write_json(path, asdict(record), FILE_MODE)


def write_failure(path: Path, fields: Mapping[str, object]) -> None:
    """Atomically write a failed job's outcome, the stage's or the ingest's fields."""

    _write_json(path, fields, PARTIAL_MODE)


def read_record(path: Path) -> StageRecord | None:
    """Read and validate a whole schema-one stage record."""

    if path.is_symlink():
        raise StageFailure("invalid")
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as source:
            value = json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StageFailure("invalid", error=type(exc).__name__) from exc
    if not isinstance(value, dict):
        raise StageFailure("invalid")
    try:
        record = StageRecord(**value)
    except TypeError as exc:
        raise StageFailure("invalid") from exc
    if (
        type(record.schema) is not int or record.schema != 1
        or not _dated_name(record.label, LABEL_PATTERN)
        or not isinstance(record.source, str)
        or re.fullmatch(SEGMENT_PATTERN, record.source) is None
        or not _dated_name(record.snapshot, SNAPSHOT_PATTERN)
        or record.source != record.snapshot[:-DATE_SUFFIX_LENGTH]
        or not _valid_courts(record.courts)
        or not _valid_inputs(record.inputs, sizes=True)
        or not isinstance(record.records, dict) or set(record.records) != set(STAGE_TABLES)
        or any(type(count) is not int or count < 0 for count in record.records.values())
        or not isinstance(record.counts, dict) or set(record.counts) != set(record.courts)
        or any(
            not isinstance(counts, dict)
            or set(counts) != set(STAGE_TABLES[1:])
            or any(type(count) is not int or count < 0 for count in counts.values())
            for counts in record.counts.values()
        )
        or type(record.job) is not int or record.job < 0
        or type(record.seconds) not in {int, float}
        or not math.isfinite(record.seconds) or record.seconds < 0
        or not isinstance(record.staged_at, str)
    ):
        raise StageFailure("invalid")
    return record


@dataclass
class _Progress:
    job: int
    label: str
    monotonic: Callable[[], float]
    started: float
    records: dict[str, int]
    last_progress: float

    def seconds(self) -> float:
        return max(0.0, self.monotonic() - self.started)

    def row(self, table: str) -> None:
        self.records[table] += 1
        _log_progress(self, table)


def _log(progress: _Progress, action: str, *, table: str = "-", error: str = "-") -> None:
    logger.info(
        "action=stage_%s job_id=%d label=%s table=%s records=%d seconds=%.3f error=%s",
        action, progress.job, progress.label, table,
        progress.records.get(table, sum(progress.records.values())),
        progress.seconds(), error,
    )


def _log_progress(progress: _Progress, table: str) -> None:
    now = progress.monotonic()
    if now - progress.last_progress >= fetch.LOG_INTERVAL_SECONDS:
        _log(progress, "progress", table=table)
        progress.last_progress = now


def read_rows(
    path: Path, table: str,
    opener: Callable[[Path, Literal["rb"]], bz2.BZ2File | BinaryIO],
) -> Generator[tuple[list[str], list[str]], None, None]:
    """Yield each parsed record with the input lines it was read from.

    The opener reads either a compressed dump or an uncompressed staged file.
    The lines are valid until the next record is read; most records are
    skipped, so they are joined and encoded only where one is written.
    """

    with io.TextIOWrapper(
        opener(path, "rb"), encoding="utf-8", errors="strict", newline="",
    ) as source:
        consumed: list[str] = []

        def tee() -> Iterator[str]:
            for line in source:
                consumed.append(line)
                yield line

        reader = csv.reader(
            tee(), delimiter=",", quotechar='"', escapechar="\\",
            doublequote=False, strict=True,
        )
        while True:
            try:
                row = next(reader)
            except StopIteration:
                return
            except (csv.Error, UnicodeDecodeError, EOFError) as exc:
                raise StageFailure("malformed", table=table, error=type(exc).__name__) from exc
            yield row, consumed
            consumed.clear()


def encoded_lines(lines: list[str]) -> bytes:
    # The text was decoded strictly, so its UTF-8 encoding is the input's bytes.
    return "".join(lines).encode("utf-8")


def read_header(
    rows: Iterator[tuple[list[str], list[str]]], table: str,
    *, columns: tuple[str, ...] | None = None,
) -> tuple[dict[str, int], int, bytes]:
    """Return required column positions, row width, and the original header bytes."""

    try:
        fields, lines = next(rows)
    except StopIteration as exc:
        raise StageFailure("malformed", table=table, error=type(exc).__name__) from exc
    try:
        positions = {name: fields.index(name) for name in (
            REQUIRED_COLUMNS[table] if columns is None else columns
        )}
    except ValueError as exc:
        raise StageFailure("malformed", table=table, error=type(exc).__name__) from exc
    return positions, len(fields), encoded_lines(lines)


def checked_rows(
    rows: Iterator[tuple[list[str], list[str]]], width: int, table: str,
    on_row: Callable[[str], None] | None = None,
) -> Iterator[tuple[list[str], list[str]]]:
    """Refuse width changes and report each complete data row."""

    for fields, lines in rows:
        if len(fields) != width:
            raise StageFailure("malformed", table=table)
        if on_row is not None:
            on_row(table)
        yield fields, lines


def _make_directory(path: Path) -> None:
    path.mkdir(mode=DIR_MODE, exist_ok=True)
    os.chmod(path, DIR_MODE, follow_symlinks=False)


@contextmanager
def _outputs(directory: Path, courts: list[str], table: str) -> Iterator[list[BinaryIO]]:
    """Create one exclusive file per court and flush each before closing."""

    opened: list[BinaryIO] = []
    try:
        for court in courts:
            path = directory / court / f"{table}.csv"
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
            )
            opened.append(os.fdopen(descriptor, "wb"))
        yield opened
    finally:
        try:
            for output in opened:
                output.flush()
                os.fsync(output.fileno())
                os.fchmod(output.fileno(), FILE_MODE)
        finally:
            for output in opened:
                output.close()


def _scan_courts(
    path: Path, courts: list[str], progress: _Progress,
) -> dict[str, int]:
    table = STAGE_TABLES[0]
    rows = read_rows(path, table, bz2.open)
    try:
        columns, width, _ = read_header(rows, table)
        found: set[str] = set()
        court_index = {court: index for index, court in enumerate(courts)}
        for fields, _ in checked_rows(rows, width, table, progress.row):
            if fields[columns["id"]] in court_index:
                found.add(fields[columns["id"]])
        for court in courts:
            if court not in found:
                raise StageFailure("unknown-court", table=table, court=court)
        _log(progress, "table", table=table)
        return court_index
    finally:
        rows.close()


def _stream_related(
    path: Path, directory: Path, courts: list[str], table: str,
    lookup: dict[str, int], reference: str, progress: _Progress,
    counts: dict[str, dict[str, int]],
    ids: dict[str, int] | None = None,
) -> None:
    rows = read_rows(path, table, bz2.open)
    try:
        columns, width, header = read_header(rows, table)
        with _outputs(directory, courts, table) as outputs:
            for output in outputs:
                output.write(header)
            for fields, lines in checked_rows(rows, width, table, progress.row):
                court = lookup.get(fields[columns[reference]])
                if court is None:
                    continue
                if ids is not None:
                    ids[fields[columns["id"]]] = court
                outputs[court].write(encoded_lines(lines))
                counts[courts[court]][table] += 1
        _log(progress, "table", table=table)
    finally:
        rows.close()


def _matching_record(
    path: Path, label: str, snapshot: str, courts: list[str],
    inputs: dict[str, dict[str, str]],
) -> StageRecord | None:
    record = read_record(path / STAGE_RECORD_NAME)
    if record is None:
        return None
    if (
        record.label != label or record.snapshot != snapshot or record.courts != courts
        or any(
            record.inputs[table]["path"] != inputs[table]["path"]
            or record.inputs[table]["sha256"] != inputs[table]["sha256"]
            for table in STAGE_TABLES
        )
    ):
        raise StageFailure("invalid")
    return record


def _stage_files(
    snapshot_dir: Path, partial: Path, courts: list[str],
    inputs: dict[str, dict[str, str]], progress: _Progress,
) -> dict[str, dict[str, int]]:
    counts = {court: dict.fromkeys(STAGE_TABLES[1:], 0) for court in courts}
    court_index = _scan_courts(snapshot_dir / inputs["courts"]["path"], courts, progress)
    docket_ids: dict[str, int] = {}
    cluster_ids: dict[str, int] = {}
    for table, lookup, reference, ids in (
        ("dockets", court_index, "court_id", docket_ids),
        ("opinion-clusters", docket_ids, "docket_id", cluster_ids),
        ("citations", cluster_ids, "cluster_id", None),
        ("opinions", cluster_ids, "cluster_id", None),
    ):
        _stream_related(
            snapshot_dir / inputs[table]["path"], partial, courts, table,
            lookup, reference, progress, counts, ids,
        )
    return counts


def stage(
    snapshots_root: Path, work_root: Path, label: str, snapshot: str,
    courts: list[str], inputs: dict[str, dict[str, str]], job: int,
    *, clock: Callable[[], datetime] = _clock,
    monotonic: Callable[[], float] = time.monotonic,
) -> StageRecord:
    """Build one source's per-court stage from its pinned whole snapshot files."""

    started = monotonic()
    progress = _Progress(job, "-", monotonic, started, dict.fromkeys(STAGE_TABLES, 0), started)
    try:
        validate_arguments(label, snapshot, courts, inputs)
        progress.label = label
        snapshot_dir = snapshot_path(snapshots_root, snapshot)
        whole = work_path(work_root, label, snapshot)
        label_dir = whole.parent
        if not label_dir.is_dir():
            _make_directory(label_dir)
        existing = _matching_record(whole, label, snapshot, courts, inputs)
        if existing is not None:
            return existing
        lock_path = whole.with_name(whole.name + LOCK_SUFFIX)
        failure_path = whole.with_name(whole.name + f".{STAGE_FAILURE_NAME}")
        partial = whole.with_name(whole.name + PARTIAL_SUFFIX)
        with lock_partial(lock_path):
            failure_path.unlink(missing_ok=True)
            existing = _matching_record(whole, label, snapshot, courts, inputs)
            if existing is not None:
                return existing
            if whole.exists():
                raise StageFailure("invalid")
            _log(progress, "start")
            recorded_inputs = input_records(snapshot_dir, inputs)
            if partial.exists():
                shutil.rmtree(partial)
            _make_directory(partial)
            for court in courts:
                _make_directory(partial / court)
            counts = _stage_files(snapshot_dir, partial, courts, inputs, progress)
            for court in courts:
                fetch._flush_directory(partial / court)
            record = StageRecord(
                label, snapshot[:-DATE_SUFFIX_LENGTH], snapshot, courts,
                recorded_inputs, progress.records, counts, job, progress.seconds(),
                clock().astimezone(UTC).isoformat(),
            )
            write_record(partial / STAGE_RECORD_NAME, record)
            os.replace(partial, whole)
            fetch._flush_directory(label_dir)
            _log(progress, "end")
            return record
    except StageFailure as exc:
        _log(progress, "failed", table=exc.table or "-", error=exc.error or type(exc).__name__)
        raise
    except OSError as exc:
        _log(progress, "failed", error=type(exc).__name__)
        raise StageFailure("local", error=type(exc).__name__) from exc


def write_job_failure(
    work_root: Path, label: str, snapshot: str, job: int,
    failure: StageFailure, *, clock: Callable[[], datetime] = _clock,
) -> None:
    """Persist a failed job when its work directory is safe and writable."""

    if failure.reason == "busy":
        return
    try:
        whole = work_path(work_root, label, snapshot)
        _make_directory(whole.parent)
        write_failure(
            whole.with_name(whole.name + f".{STAGE_FAILURE_NAME}"),
            asdict(StageFailureRecord(
                job, failure.reason, failure.table, failure.court, failure.error,
                clock().astimezone(UTC).isoformat(),
            )),
        )
    except (StageFailure, OSError):
        return
