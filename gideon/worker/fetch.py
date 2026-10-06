"""Validate and fetch a corpus file into a durable snapshot record."""

import fcntl
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final, Literal, NoReturn, Protocol
from urllib.parse import urljoin, urlsplit

import httpx

FETCH_TASK: Final = "gideon.worker.tasks.fetch"
FETCH_QUEUE: Final = "fetch"
SNAPSHOTS_ROOT: Final = Path("/data/bulk/snapshots")
RESOLVE_DIR: Final = "resolve"
KEPT_FORM: Final = "kept"
FRESH_FORM: Final = "fresh"
PARTIAL_SUFFIX: Final = ".partial"
RECORD_SUFFIX: Final = ".fetch.json"
FAILURE_SUFFIX: Final = ".fetch-failed.json"
TEMP_SUFFIX: Final = ".tmp"
# exempt: two kept transfers at once leave two of the worker's four slots free.
LANE_COUNT: Final = 2
DIR_MODE: Final = 0o2770
FILE_MODE: Final = 0o440
PARTIAL_MODE: Final = 0o640
FAILURE_REASONS: Final = frozenset({
    "invalid", "refused-host", "egress-failed", "upstream-status", "changed",
    "redirect", "transport", "local", "busy",
})
SEGMENT_PATTERN: Final = r"[A-Za-z0-9][A-Za-z0-9._-]*"
SOURCE_PATTERN: Final = rf"{SEGMENT_PATTERN}-[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}"
DNS_PATTERN: Final = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
URL_MAX_LENGTH: Final = 2048
RESERVED_SUFFIXES: Final = (PARTIAL_SUFFIX, RECORD_SUFFIX, FAILURE_SUFFIX)
# The agent names the project so an upstream can reach its maintainers. It
# carries no release: `gideon/__init__.py` is outside the worker's sources.
PUBLIC_REPOSITORY_URL: Final = "https://github.com/TNMD-FDO/GIDEON"
# exempt: a checkpoint every 64 MiB costs one flush per few seconds of stream.
CHECKPOINT_BYTES: Final = 64 * 1024 * 1024
# exempt: one MiB pieces keep the hash and the write in step at low memory.
CHUNK_BYTES: Final = 1024 * 1024
# exempt: a tunnel through the egress service opens within these seconds.
CONNECT_SECONDS: Final = 15.0
# exempt: a stream silent this long is a dropped connection, retried in the run.
READ_SECONDS: Final = 60.0
# exempt: the waits before each retry with no byte gained since the last; one
# more failure past the last wait ends the run, so a throttled or failing
# upstream gets about a minute and a half before the job fails.
RETRY_WAITS_SECONDS: Final = (5.0, 20.0, 60.0)
MAX_NO_PROGRESS_FAILURES: Final = len(RETRY_WAITS_SECONDS) + 1
# exempt: five redirects permit a public object to move without allowing a loop.
MAX_REDIRECTS: Final = 5
# exempt: one progress line per minute bounds log volume during long transfers.
LOG_INTERVAL_SECONDS: Final = 60
CONTENT_RANGE_PATTERN: Final = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+|\*)\Z")
PROXY_STATUS_PATTERN: Final = re.compile(r"([0-9]{3})\b")
REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
logger = logging.getLogger(__name__)


class FetchFailure(Exception):
    """A safe, structured failure for the queue and failure-file reader."""

    def __init__(
        self, reason: str, *, host: str | None = None,
        status: int | None = None, error: str | None = None,
    ) -> None:
        if reason not in FAILURE_REASONS:
            raise ValueError("unknown fetch failure reason")
        self.reason = reason
        self.host = host
        self.status = status
        self.error = error
        super().__init__(reason)


@dataclass(frozen=True)
class FetchRecord:
    state: Literal["partial", "whole"]
    form: str
    host: str
    path: str
    etag: str | None
    last_modified: str | None
    total: int | None
    durable: int
    size: int | None
    sha256: str | None
    fetched_at: str | None
    job: int
    seconds: float
    resumes: int
    schema: int = 1


@dataclass(frozen=True)
class FetchFailureRecord:
    job: int
    reason: str
    host: str | None
    status: int | None
    error: str | None
    at: str
    schema: int = 1


def validate_destination(destination: str, form: str) -> None:
    """Refuse any destination outside the form's bounded relative grammar."""

    parts = destination.split("/")
    if form not in {KEPT_FORM, FRESH_FORM} or not 2 <= len(parts) <= 8:
        raise FetchFailure("invalid")
    if any(
        re.fullmatch(SEGMENT_PATTERN, part) is None
        or part in {".", ".."}
        or part.endswith(RESERVED_SUFFIXES)
        for part in parts
    ):
        raise FetchFailure("invalid")
    if form == KEPT_FORM:
        if parts[0] == RESOLVE_DIR or re.fullmatch(SOURCE_PATTERN, parts[0]) is None:
            raise FetchFailure("invalid")
        try:
            date.fromisoformat(parts[0][-10:])
        except ValueError as exc:
            raise FetchFailure("invalid") from exc
    elif parts[0] != RESOLVE_DIR:
        raise FetchFailure("invalid")


def validate_url(url: str) -> tuple[str, str]:
    """Return only the safe host and path retained in records."""

    if (
        len(url) > URL_MAX_LENGTH or not url.startswith("https://")
        or any(ord(char) < 33 for char in url)
    ):
        raise FetchFailure("invalid")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise FetchFailure("invalid") from exc
    if (
        parsed.scheme != "https" or host is None
        or parsed.username is not None or parsed.password is not None
        or parsed.fragment or port not in {None, 443}
        or re.fullmatch(DNS_PATTERN, host) is None
        or len(host) > 253 or any(len(label) > 63 for label in host.split("."))
        or parsed.netloc != (host if port is None else f"{host}:443")
    ):
        raise FetchFailure("invalid")
    return host, parsed.path or "/"


def destination_path(root: Path, destination: str, form: str) -> Path:
    """Resolve a validated name without following any existing symbolic link."""

    validate_destination(destination, form)
    if root.is_symlink() or not root.is_dir():
        raise FetchFailure("local", error="FileNotFoundError")
    path = root
    for part in destination.split("/"):
        path /= part
        if path.is_symlink():
            raise FetchFailure("invalid")
    if not path.resolve(strict=False).is_relative_to(root.resolve(strict=True)):
        raise FetchFailure("invalid")
    for suffix in (PARTIAL_SUFFIX, RECORD_SUFFIX, FAILURE_SUFFIX):
        if path.with_name(path.name + suffix).is_symlink():
            raise FetchFailure("invalid")
    return path


def _flush_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_directories(root: Path, parent: Path) -> None:
    current = root
    for part in parent.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise FetchFailure("invalid")
        current.mkdir(mode=DIR_MODE, exist_ok=True)
        os.chmod(current, DIR_MODE, follow_symlinks=False)


def _write_json(path: Path, value: FetchRecord | FetchFailureRecord, mode: int) -> None:
    temporary = path.with_name(path.name + TEMP_SUFFIX)
    # A write stopped part-way leaves its temporary, possibly already read-only;
    # the directory's group write lets the next writer remove it and start anew.
    temporary.unlink(missing_ok=True)
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        os.fchmod(output.fileno(), mode)
        json.dump(asdict(value), output, sort_keys=True, separators=(",", ":"))
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    _flush_directory(path.parent)


def write_record(path: Path, record: FetchRecord) -> None:
    """Atomically write a partial or whole transfer record."""

    _write_json(path, record, FILE_MODE if record.state == "whole" else PARTIAL_MODE)


def write_failure(path: Path, record: FetchFailureRecord) -> None:
    """Atomically write the structured outcome of a failed job."""

    _write_json(path, record, PARTIAL_MODE)


def _read_json(path: Path) -> dict[str, object] | None:
    if path.is_symlink():
        raise FetchFailure("invalid")
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as source:
            value = json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FetchFailure("invalid", error=type(exc).__name__) from exc
    if not isinstance(value, dict):
        raise FetchFailure("invalid")
    return value


def read_record(path: Path) -> FetchRecord | None:
    """Read and validate a schema-one transfer record."""

    value = _read_json(path)
    if value is None:
        return None
    try:
        record = FetchRecord(**value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise FetchFailure("invalid") from exc
    if (
        record.schema != 1 or not isinstance(record.state, str)
        or record.state not in {"partial", "whole"}
        or not isinstance(record.form, str)
        or record.form not in {KEPT_FORM, FRESH_FORM}
        or not isinstance(record.host, str) or not isinstance(record.path, str)
        or (record.etag is not None and not isinstance(record.etag, str))
        or (record.last_modified is not None and not isinstance(record.last_modified, str))
        or not isinstance(record.durable, int) or isinstance(record.durable, bool)
        or record.durable < 0
        or not isinstance(record.job, int) or isinstance(record.job, bool) or record.job < 0
        or not isinstance(record.seconds, (float, int)) or isinstance(record.seconds, bool)
        or record.seconds < 0
        or not isinstance(record.resumes, int) or isinstance(record.resumes, bool)
        or record.resumes < 0
        or (record.total is not None and (
            not isinstance(record.total, int) or record.total < 0
        ))
        or (record.state == "whole" and (
            not isinstance(record.size, int) or record.size != record.durable
            or not isinstance(record.sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", record.sha256) is None
            or not isinstance(record.fetched_at, str)
        ))
    ):
        raise FetchFailure("invalid")
    return record


def read_failure(path: Path) -> FetchFailureRecord | None:
    """Read and validate a schema-one failed-job record."""

    value = _read_json(path)
    if value is None:
        return None
    try:
        record = FetchFailureRecord(**value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise FetchFailure("invalid") from exc
    if (
        record.schema != 1 or not isinstance(record.reason, str)
        or record.reason not in FAILURE_REASONS
        or not isinstance(record.job, int) or isinstance(record.job, bool)
        or not isinstance(record.at, str)
        or (record.host is not None and not isinstance(record.host, str))
        or (record.status is not None and not isinstance(record.status, int))
        or (record.error is not None and not isinstance(record.error, str))
    ):
        raise FetchFailure("invalid")
    return record


def _client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        transport=transport,
        follow_redirects=False,
        timeout=httpx.Timeout(
            connect=CONNECT_SECONDS, read=READ_SECONDS, write=READ_SECONDS,
            pool=CONNECT_SECONDS,
        ),
        headers={
            "User-Agent": f"GIDEON (+{PUBLIC_REPOSITORY_URL})",
            # Lengths, ranges, and the hash all count the bytes as served.
            "Accept-Encoding": "identity",
        },
    )


def _clock() -> datetime:
    return datetime.now(UTC)


class _Digest(Protocol):
    def update(self, data: bytes) -> None: ...

    def hexdigest(self) -> str: ...


@dataclass
class _Progress:
    path: Path
    partial: Path
    record_path: Path
    destination: str
    form: str
    host: str
    url_path: str
    job: int
    started: datetime
    clock: Callable[[], datetime]
    digest: _Digest = field(default_factory=hashlib.sha256)
    size: int = 0
    durable: int = 0
    total: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    seconds_before: float = 0.0
    resumes: int = 0
    ranged: bool = False
    last_progress_log: datetime | None = None

    def seconds(self) -> float:
        return self.seconds_before + max(0.0, (self.clock() - self.started).total_seconds())

    def partial_record(self) -> FetchRecord:
        return FetchRecord(
            "partial", self.form, self.host, self.url_path, self.etag,
            self.last_modified, self.total, self.durable, None, None, None,
            self.job, self.seconds(), self.resumes,
        )


class _Retryable(Exception):
    def __init__(self, *, status: int | None = None, error: str | None = None) -> None:
        self.status = status
        self.error = error


def _log(progress: _Progress, action: str, *, sha256: str = "-", error: str = "-") -> None:
    logger.info(
        "action=fetch_%s job_id=%d form=%s host=%s destination=%s "
        "offset=%d total=%s sha256=%s seconds=%.3f error=%s",
        action, progress.job, progress.form, progress.host, progress.destination,
        progress.size, progress.total if progress.total is not None else "-",
        sha256, progress.seconds(), error,
    )


def _log_progress(progress: _Progress) -> None:
    now = progress.clock()
    previous = progress.last_progress_log or progress.started
    if (now - previous).total_seconds() >= LOG_INTERVAL_SECONDS:
        _log(progress, "progress")
        progress.last_progress_log = now


@contextmanager
def _lock_partial(path: Path) -> Iterator[bool]:
    """Hold one inode locked until the transfer has finished its final flush."""

    existed = path.exists()
    descriptor = os.open(path, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, PARTIAL_MODE)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FetchFailure("busy") from exc
        try:
            os.fchmod(descriptor, PARTIAL_MODE)
            yield existed
        finally:
            _remove_if_empty(path, descriptor)
    finally:
        os.close(descriptor)


def _remove_if_empty(path: Path, descriptor: int) -> None:
    """Remove the lock's own partial when the job ends with no byte in it."""

    held = os.fstat(descriptor)
    try:
        named = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return
    # Still holding the lock, so no other writer can have put bytes in it.
    if (named.st_dev, named.st_ino) == (held.st_dev, held.st_ino) and held.st_size == 0:
        path.unlink()
        _flush_directory(path.parent)


def _discard_incomplete(progress: _Progress) -> None:
    progress.partial.unlink(missing_ok=True)
    progress.record_path.unlink(missing_ok=True)
    _flush_directory(progress.path.parent)


def _reset_incomplete(progress: _Progress) -> None:
    with progress.partial.open("r+b") as output:
        output.truncate(0)
        output.flush()
        os.fsync(output.fileno())
    progress.record_path.unlink(missing_ok=True)
    _flush_directory(progress.path.parent)


def _open_or_resume(progress: _Progress, partial_existed: bool) -> FetchRecord | None:
    """Recover a kept file or prepare a clean transfer from byte zero."""

    if progress.form == FRESH_FORM:
        _reset_incomplete(progress)
        return None
    record = read_record(progress.record_path)
    if record is not None and record.state == "whole":
        if (
            progress.path.is_file()
            and progress.path.stat().st_size == record.size
        ):
            progress.partial.unlink(missing_ok=True)
            _flush_directory(progress.path.parent)
            return record
        if (
            not progress.path.exists() and partial_existed and progress.partial.is_file()
            and progress.partial.stat().st_size == record.size
        ):
            os.chmod(progress.partial, FILE_MODE)
            os.replace(progress.partial, progress.path)
            _flush_directory(progress.path.parent)
            return record
        raise FetchFailure("local", host=progress.host, error="IncompleteWholeFile")
    if progress.path.exists():
        raise FetchFailure("local", host=progress.host, error="FileExistsError")
    if record is None or not partial_existed:
        _reset_incomplete(progress)
        return None
    if (
        record.form != progress.form or record.host != progress.host
        or record.path != progress.url_path or record.etag is None
    ):
        _reset_incomplete(progress)
        return None
    if record.total is not None and record.durable > record.total:
        raise FetchFailure("local", host=progress.host, error="InvalidOffset")
    if progress.partial.stat().st_size < record.durable:
        raise FetchFailure("local", host=progress.host, error="MissingDurableBytes")
    os.chmod(progress.partial, PARTIAL_MODE)
    with progress.partial.open("r+b") as output:
        output.truncate(record.durable)
        output.flush()
        os.fsync(output.fileno())
    remaining = record.durable
    with progress.partial.open("rb") as source:
        while remaining:
            chunk = source.read(min(CHUNK_BYTES, remaining))
            if not chunk:
                raise FetchFailure("local", host=progress.host, error="MissingDurableBytes")
            progress.digest.update(chunk)
            remaining -= len(chunk)
    progress.size = progress.durable = record.durable
    progress.total = record.total
    progress.etag = record.etag
    progress.last_modified = record.last_modified
    progress.seconds_before = record.seconds
    progress.resumes = record.resumes
    progress.ranged = True
    return None


def _checkpoint(progress: _Progress) -> None:
    """Flush bytes before advancing the offset named by a partial record."""

    with progress.partial.open("r+b") as output:
        output.flush()
        os.fsync(output.fileno())
    progress.durable = progress.size
    write_record(progress.record_path, progress.partial_record())


def _changed(progress: _Progress) -> NoReturn:
    _discard_incomplete(progress)
    raise FetchFailure("changed", host=progress.host)


def _answer(progress: _Progress, response: httpx.Response) -> int | None:
    """Classify the answer before any body byte reaches the partial."""

    status = response.status_code
    if progress.ranged and status in {200, 412, 416}:
        _changed(progress)
    if status in {408, 429} or 500 <= status <= 599:
        raise _Retryable(status=status)
    if progress.ranged:
        if status != 206:
            raise FetchFailure("upstream-status", host=progress.host, status=status)
        match = CONTENT_RANGE_PATTERN.fullmatch(response.headers.get("content-range", ""))
        if match is None:
            _changed(progress)
        start, end = int(match[1]), int(match[2])
        total = None if match[3] == "*" else int(match[3])
        if (
            response.headers.get("etag") != progress.etag
            or start != progress.size or end < start
            or total != progress.total
            or (total is not None and end >= total)
        ):
            _changed(progress)
        progress.last_modified = response.headers.get("last-modified", progress.last_modified)
        return end - start + 1
    if status != 200:
        raise FetchFailure("upstream-status", host=progress.host, status=status)
    length = response.headers.get("content-length")
    try:
        total = int(length) if length is not None else None
    except ValueError as exc:
        raise FetchFailure("transport", host=progress.host, error="ValueError") from exc
    if total is not None and total < 0:
        raise FetchFailure("transport", host=progress.host, error="ValueError")
    progress.total = total
    progress.etag = response.headers.get("etag")
    progress.last_modified = response.headers.get("last-modified")
    return total


@contextmanager
def _response(
    client: httpx.Client, url: str, headers: dict[str, str],
) -> Iterator[httpx.Response]:
    """Follow safe redirects while preserving the request's range conditions."""

    current = url
    redirects = 0
    while True:
        host, _ = validate_url(current)
        try:
            with client.stream(
                "GET", current, headers=headers, follow_redirects=False,
            ) as response:
                if response.status_code in REDIRECT_STATUSES:
                    if redirects >= MAX_REDIRECTS:
                        raise FetchFailure("redirect", host=host, status=response.status_code)
                    location = response.headers.get("location")
                    if not location:
                        raise FetchFailure("redirect", host=host, status=response.status_code)
                    try:
                        next_url = urljoin(current, location)
                        validate_url(next_url)
                    except (FetchFailure, ValueError) as exc:
                        raise FetchFailure(
                            "redirect", host=host, status=response.status_code,
                        ) from exc
                    current = next_url
                    redirects += 1
                    continue
                yield response
                return
        except httpx.ProxyError as exc:
            match = PROXY_STATUS_PATTERN.match(str(exc))
            status = int(match[1]) if match is not None else None
            raise FetchFailure(
                "refused-host" if status == 403 else "egress-failed",
                host=host, status=status, error=type(exc).__name__,
            ) from exc


def _stream_into_partial(client: httpx.Client, url: str, progress: _Progress) -> bool:
    """Read one answer and return whether it reached the object's end."""

    headers = (
        {"Range": f"bytes={progress.size}-", "If-Match": progress.etag}
        if progress.ranged and progress.etag is not None else {}
    )
    if progress.ranged:
        progress.resumes += 1
        _log(progress, "resume")
    with _response(client, url, headers) as response:
        expected = _answer(progress, response)
        received = 0
        # The lock made the partial, so the stream always opens it in place.
        with progress.partial.open("r+b") as output:
            output.seek(progress.size)
            for chunk in response.iter_raw(CHUNK_BYTES):
                if (
                    (expected is not None and received + len(chunk) > expected)
                    or (progress.total is not None and progress.size + len(chunk) > progress.total)
                ):
                    raise FetchFailure("transport", host=progress.host, error="OversizedBody")
                output.write(chunk)
                progress.digest.update(chunk)
                progress.size += len(chunk)
                received += len(chunk)
                _log_progress(progress)
                if progress.size - progress.durable >= CHECKPOINT_BYTES:
                    output.flush()
                    os.fsync(output.fileno())
                    progress.durable = progress.size
                    write_record(progress.record_path, progress.partial_record())
        if expected is not None and received != expected:
            raise _Retryable(error="IncompleteBody")
        if progress.total is not None and progress.size > progress.total:
            raise FetchFailure("transport", host=progress.host, error="OversizedBody")
        if progress.total is not None and progress.size < progress.total:
            _checkpoint(progress)
            progress.ranged = True
            return False
        return True


def _retry(progress: _Progress, failure: _Retryable, failures: int, sleep: Callable[[float], None]) -> None:
    if failures >= MAX_NO_PROGRESS_FAILURES:
        reason = "upstream-status" if failure.status is not None else "transport"
        raise FetchFailure(reason, host=progress.host, status=failure.status, error=failure.error)
    if progress.size and progress.etag is None:
        _reset_incomplete(progress)
        progress.digest = hashlib.sha256()
        progress.size = progress.durable = 0
        progress.total = None
        progress.last_modified = None
        progress.ranged = False
    elif progress.size > progress.durable:
        _checkpoint(progress)
        progress.ranged = True
    elif progress.size:
        progress.ranged = True
    sleep(RETRY_WAITS_SECONDS[max(failures, 1) - 1])


def _complete(progress: _Progress, on_step: Callable[[str], None] | None) -> FetchRecord:
    """Flush partial, publish whole record, rename, then flush directory."""

    with progress.partial.open("r+b") as output:
        output.flush()
        os.fsync(output.fileno())
    if on_step is not None:
        on_step("flushed")
    record = FetchRecord(
        "whole", progress.form, progress.host, progress.url_path,
        progress.etag, progress.last_modified, progress.total, progress.size,
        progress.size, progress.digest.hexdigest(),
        progress.clock().astimezone(UTC).isoformat(), progress.job,
        progress.seconds(), progress.resumes,
    )
    write_record(progress.record_path, record)
    if on_step is not None:
        on_step("recorded")
    os.chmod(progress.partial, FILE_MODE)
    os.replace(progress.partial, progress.path)
    if on_step is not None:
        on_step("renamed")
    _flush_directory(progress.path.parent)
    return record


def _finished(progress: _Progress, record: FetchRecord) -> FetchRecord:
    progress.size = record.size or 0
    progress.total = record.total
    _log(progress, "end", sha256=record.sha256 or "-")
    return record


def transfer(
    root: Path, destination: str, url: str, form: str, job: int,
    *, client_factory: Callable[[], httpx.Client] = _client,
    clock: Callable[[], datetime] = _clock,
    sleep: Callable[[float], None] = time.sleep,
    on_step: Callable[[str], None] | None = None,
) -> FetchRecord:
    """Fetch or resume one file and publish its durable whole record."""

    host, url_path = validate_url(url)
    path = destination_path(root, destination, form)
    progress = _Progress(
        path, path.with_name(path.name + PARTIAL_SUFFIX),
        path.with_name(path.name + RECORD_SUFFIX), destination, form, host, url_path, job,
        clock(), clock,
    )
    failure_path = path.with_name(path.name + FAILURE_SUFFIX)
    try:
        if not path.parent.is_dir():
            _ensure_directories(root, path.parent)
        if form == KEPT_FORM:
            existing = read_record(progress.record_path)
            if (
                existing is not None and existing.state == "whole"
                and path.is_file() and path.stat().st_size == existing.size
            ):
                failure_path.unlink(missing_ok=True)
                _log(progress, "start")
                _flush_directory(path.parent)
                return _finished(progress, existing)
        with _lock_partial(progress.partial) as partial_existed:
            _ensure_directories(root, path.parent)
            failure_path.unlink(missing_ok=True)
            _log(progress, "start")
            whole = _open_or_resume(progress, partial_existed)
            if whole is not None:
                return _finished(progress, whole)
            if progress.total is not None and progress.size == progress.total:
                return _finished(progress, _complete(progress, on_step))
            failures = 0
            with client_factory() as client:
                while True:
                    before = progress.size
                    try:
                        complete = _stream_into_partial(client, url, progress)
                    except httpx.HTTPError as exc:
                        failure = _Retryable(error=type(exc).__name__)
                    except _Retryable as exc:
                        failure = exc
                    else:
                        if complete:
                            return _finished(progress, _complete(progress, on_step))
                        failures = 0
                        continue
                    if progress.total is not None and progress.size == progress.total:
                        return _finished(progress, _complete(progress, on_step))
                    saved_progress = progress.size > before and progress.etag is not None
                    failures = 0 if saved_progress else failures + 1
                    _retry(progress, failure, failures, sleep)
    except FetchFailure as exc:
        _log(progress, "failed", error=exc.error or type(exc).__name__)
        raise
    except httpx.HTTPError as exc:
        _log(progress, "failed", error=type(exc).__name__)
        raise FetchFailure("transport", host=host, error=type(exc).__name__) from exc
    except OSError as exc:
        _log(progress, "failed", error=type(exc).__name__)
        raise FetchFailure("local", host=host, error=type(exc).__name__) from exc


def write_job_failure(
    root: Path, destination: str, form: str, job: int,
    failure: FetchFailure, *, clock: Callable[[], datetime] = _clock,
) -> None:
    """Persist a failed job when its destination remains safe and writable."""

    if failure.reason == "busy":
        return
    try:
        path = destination_path(root, destination, form)
        _ensure_directories(root, path.parent)
        write_failure(
            path.with_name(path.name + FAILURE_SUFFIX),
            FetchFailureRecord(
                job, failure.reason, failure.host, failure.status, failure.error,
                clock().astimezone(UTC).isoformat(),
            ),
        )
    except (FetchFailure, OSError):
        # An invalid path has no safe place for a failure file. The queue row
        # still records the exception, with only its reason exposed in logs.
        return
