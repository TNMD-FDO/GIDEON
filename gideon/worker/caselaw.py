"""Turn staged case-law opinions into durable document and opinion rows."""

import fcntl
import hashlib
import itertools
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final, Protocol

import psycopg

import gideon.host.cas
from gideon.host.report import Problem
from gideon.host.sysio import RealHost

from . import fetch, opiniontext, settings, staging

CASELAW_TASK: Final = "gideon.worker.tasks.caselaw"
CASELAW_QUEUE: Final = "caselaw"
CASELAW_FAILURE_NAME: Final = "caselaw-failed.json"
LOCK_SUFFIX: Final = ".caselaw.lock"
CASELAW_FAILURE_REASONS: Final = frozenset({
    "invalid", "missing-stage", "stage-mismatch", "malformed", "store",
    "database", "local", "busy",
})
DOCUMENT_FAILURE_REASONS: Final = frozenset({
    "no-text", "unparseable", "empty", "interrupted",
})
PRECEDENTIAL_VALUES: Final = ("published", "unpublished", "unknown")
# exempt: two attempts are a starting value; only a stopped parse spends one.
MAX_ATTEMPTS: Final = 2
# exempt: the bound limits a queue argument and exceeds any one court's rows.
LIMIT_MAX: Final = 10_000_000

_DOCKET_COLUMNS: Final = ("id", "docket_number", "court_id")
_CLUSTER_COLUMNS: Final = (
    "id", "date_filed", "date_filed_is_approximate", "precedential_status", "docket_id",
)
_CITATION_COLUMNS: Final = ("cluster_id", "volume", "reporter", "page")
_OPINION_COLUMNS: Final = ("id", "cluster_id", "type", *opiniontext.TEXT_SOURCES)
_POSITIVE_INTEGER: Final = re.compile(r"[0-9]+")
_ISO_DATE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

PRESENT_SQL: Final = (
    "SELECT o.opinion_id, d.doc_id, d.status, d.attempts "
    "FROM opinions AS o JOIN documents AS d ON d.doc_id = o.doc_id "
    "WHERE d.source = %s AND d.source_snapshot = %s AND o.court = %s"
)
BEGIN_DOCUMENT_SQL: Final = (
    "INSERT INTO documents (doc_id, profile, source, source_snapshot, text_source, "
    "status, attempts, begun_at) VALUES (%s, %s, %s, %s, %s, 'processing', 1, %s)"
)
BEGIN_OPINION_SQL: Final = (
    "INSERT INTO opinions (opinion_id, doc_id, court, cluster_id, docket, decided_date, "
    "decided_date_is_approximate, precedential, precedential_raw, reporter_cites, "
    "opinion_type) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
RETRY_SQL: Final = "UPDATE documents SET attempts = attempts + 1 WHERE doc_id = %s"
GIVE_BACK_SQL: Final = (
    "UPDATE documents SET attempts = GREATEST(attempts - 1, 0) WHERE doc_id = %s"
)
FINISH_READY_SQL: Final = (
    "UPDATE documents SET status = 'ready', sha256 = %s, canonical_text_sha256 = %s, "
    "ingested_at = %s WHERE doc_id = %s"
)
FINISH_FAILED_SQL: Final = (
    "UPDATE documents SET status = 'failed', failure_reason = %s, sha256 = %s, "
    "ingested_at = %s WHERE doc_id = %s"
)

logger = logging.getLogger(__name__)


def _clock() -> datetime:
    return datetime.now(UTC)


class CaselawFailure(Exception):
    """A safe job refusal with a reason from the closed vocabulary."""

    def __init__(
        self, reason: str, *, table: str | None = None, error: str | None = None,
    ) -> None:
        if reason not in CASELAW_FAILURE_REASONS:
            raise ValueError("unknown caselaw failure reason")
        self.reason = reason
        self.table = table
        self.error = error
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class CaselawFailureRecord:
    """The content-free failed-job outcome beside a staged court."""

    job: int
    court: str
    reason: str
    table: str | None
    error: str | None
    at: str
    schema: int = 1


@dataclass(frozen=True, slots=True)
class PresentRow:
    """An opinion's existing document state."""

    doc_id: str
    status: str
    attempts: int


@dataclass(frozen=True, slots=True)
class NewDocument:
    """The state fixed when an opinion begins processing."""

    doc_id: str
    profile: str
    source: str
    source_snapshot: date
    text_source: str | None
    begun_at: datetime


@dataclass(frozen=True, slots=True)
class NewOpinion:
    """CourtListener metadata bound to one document."""

    opinion_id: int
    doc_id: str
    court: str
    cluster_id: int
    docket: str | None
    decided_date: date
    decided_date_is_approximate: bool
    precedential: str
    precedential_raw: str
    reporter_cites: tuple[str, ...]
    opinion_type: str


@dataclass(frozen=True, slots=True)
class IngestCounts:
    """Counts from one bounded or whole court stream."""

    seen: int
    present: int
    ready: int
    failed: int
    retried: int
    interrupted: int


class DocumentRecord(Protocol):
    """The row operations needed while ingesting one court."""

    def present(self, source: str, snapshot_date: date, court: str) -> dict[int, PresentRow]: ...
    def begin(self, document: NewDocument, opinion: NewOpinion) -> None: ...
    def retry(self, doc_id: str) -> None: ...
    def give_back(self, doc_id: str) -> None: ...
    def finish_ready(self, doc_id: str, sha256: str, canonical_sha256: str, at: datetime) -> None: ...
    def finish_failed(
        self, doc_id: str, reason: str, at: datetime, sha256: str | None = None,
    ) -> None: ...
    def close(self) -> None: ...


class PsycopgRecord:
    """Write document transitions as one committed transaction per operation."""

    def __init__(
        self, *, connect: Callable[..., Any] = psycopg.connect,
        worker_settings: settings.Settings | None = None,
    ) -> None:
        self._connect = connect
        self._settings = worker_settings
        self._connection: Any | None = None

    def _get_connection(self) -> Any:
        if self._connection is None:
            try:
                configuration = self._settings or settings.load_settings()
                self._connection = self._connect(
                    **settings.connection_kwargs(configuration), autocommit=False,
                )
            except psycopg.Error as exc:
                raise CaselawFailure("database", error=type(exc).__name__) from exc
            except settings.WorkerSettingsError as exc:
                raise CaselawFailure("local", error=type(exc).__name__) from exc
        return self._connection

    def _run(
        self, statements: tuple[tuple[str, tuple[object, ...]], ...],
        *, read: bool = False,
    ) -> list[tuple[Any, ...]]:
        connection = self._get_connection()
        try:
            with connection.cursor() as cursor:
                for statement, parameters in statements:
                    cursor.execute(statement, parameters)
                rows: list[tuple[Any, ...]] = cursor.fetchall() if read else []
            connection.commit()
            return rows
        except psycopg.Error as exc:
            with suppress(psycopg.Error):
                connection.rollback()
            raise CaselawFailure("database", error=type(exc).__name__) from exc

    def present(self, source: str, snapshot_date: date, court: str) -> dict[int, PresentRow]:
        """Read existing opinion states for one source, snapshot, and court."""

        rows = self._run(((PRESENT_SQL, (source, snapshot_date, court)),), read=True)
        return {
            int(opinion_id): PresentRow(str(doc_id), str(status), int(attempts))
            for opinion_id, doc_id, status, attempts in rows
        }

    def begin(self, document: NewDocument, opinion: NewOpinion) -> None:
        """Insert a processing document and its opinion in one transaction."""

        self._run((
            (BEGIN_DOCUMENT_SQL, (
                document.doc_id, document.profile, document.source,
                document.source_snapshot, document.text_source, document.begun_at,
            )),
            (BEGIN_OPINION_SQL, (
                opinion.opinion_id, opinion.doc_id, opinion.court, opinion.cluster_id,
                opinion.docket, opinion.decided_date, opinion.decided_date_is_approximate,
                opinion.precedential, opinion.precedential_raw,
                list(opinion.reporter_cites), opinion.opinion_type,
            )),
        ))

    def retry(self, doc_id: str) -> None:
        """Count one more parse attempt for a processing document."""

        self._run(((RETRY_SQL, (doc_id,)),))

    def give_back(self, doc_id: str) -> None:
        """Undo an attempt counted before a clean job-level refusal."""

        self._run(((GIVE_BACK_SQL, (doc_id,)),))

    def finish_ready(self, doc_id: str, sha256: str, canonical_sha256: str, at: datetime) -> None:
        """Finish a parsed document with names of both stored objects."""

        self._run(((FINISH_READY_SQL, (sha256, canonical_sha256, at, doc_id)),))

    def finish_failed(
        self, doc_id: str, reason: str, at: datetime, sha256: str | None = None,
    ) -> None:
        """Finish an accounted-for document with a named failure."""

        self._run(((FINISH_FAILED_SQL, (reason, sha256, at, doc_id)),))

    def close(self) -> None:
        """Close the court's connection, if one was opened."""

        if self._connection is not None:
            try:
                self._connection.close()
            except psycopg.Error as exc:
                with suppress(psycopg.Error):
                    self._connection.rollback()
                raise CaselawFailure("database", error=type(exc).__name__) from exc
            finally:
                self._connection = None


@dataclass(frozen=True, slots=True)
class _Docket:
    number: str | None
    court: str


@dataclass(frozen=True, slots=True)
class _Cluster:
    docket_id: int
    decided_date: date
    approximate: bool
    precedential_raw: str


@dataclass(slots=True)
class _Progress:
    job: int
    label: str
    court: str
    started: float
    last_log: float
    monotonic: Callable[[], float]
    seen: int = 0
    present: int = 0
    ready: int = 0
    failed: int = 0
    retried: int = 0
    interrupted: int = 0
    error: str = "-"

    def counts(self) -> IngestCounts:
        return IngestCounts(
            self.seen, self.present, self.ready, self.failed,
            self.retried, self.interrupted,
        )


def _log(progress: _Progress, action: str) -> None:
    logger.info(
        "action=caselaw_%s job_id=%d label=%s court=%s seen=%d present=%d "
        "ready=%d failed=%d retried=%d interrupted=%d seconds=%.3f error=%s",
        action, progress.job, progress.label, progress.court, progress.seen,
        progress.present, progress.ready, progress.failed, progress.retried,
        progress.interrupted, max(0.0, progress.monotonic() - progress.started),
        progress.error,
    )


def _log_progress(progress: _Progress) -> None:
    now = progress.monotonic()
    if now - progress.last_log >= fetch.LOG_INTERVAL_SECONDS:
        _log(progress, "progress")
        progress.last_log = now


def _valid_dated_name(value: object, pattern: str) -> bool:
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


def validate_arguments(
    label: object, snapshot: object, court: object, limit: object,
) -> None:
    """Refuse queue arguments outside the stage's name and court grammars."""

    if (
        not _valid_dated_name(label, staging.LABEL_PATTERN)
        or not _valid_dated_name(snapshot, staging.SNAPSHOT_PATTERN)
        or not isinstance(court, str)
        or re.fullmatch(staging.COURT_PATTERN, court) is None
        or limit is not None and (type(limit) is not int or not 1 <= limit <= LIMIT_MAX)
    ):
        raise CaselawFailure("invalid")


def _positive_integer(value: str, table: str) -> int:
    if _POSITIVE_INTEGER.fullmatch(value) is None:
        raise CaselawFailure("malformed", table=table, error="ValueError")
    try:
        number = int(value)
    except ValueError as exc:
        raise CaselawFailure("malformed", table=table, error=type(exc).__name__) from exc
    if number < 1:
        raise CaselawFailure("malformed", table=table, error="ValueError")
    return number


def _date(value: str, table: str) -> date:
    if _ISO_DATE.fullmatch(value) is None:
        raise CaselawFailure("malformed", table=table, error="ValueError")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CaselawFailure("malformed", table=table, error=type(exc).__name__) from exc


@contextmanager
def _table_rows(
    court_dir: Path, table: str, columns: tuple[str, ...],
    progress: _Progress,
) -> Iterator[tuple[dict[str, int], Iterator[tuple[list[str], list[str]]]]]:
    path = court_dir / f"{table}.csv"
    if path.is_symlink() or not path.is_file():
        raise CaselawFailure("stage-mismatch", table=table)
    rows = staging.read_rows(path, table, open)
    try:
        positions, width, _ = staging.read_header(rows, table, columns=columns)
        yield positions, staging.checked_rows(
            rows, width, table, lambda _table: _log_progress(progress),
        )
    except staging.StageFailure as exc:
        raise CaselawFailure(
            "malformed", table=table, error=exc.error or type(exc).__name__,
        ) from exc
    except OSError as exc:
        raise CaselawFailure("local", table=table, error=type(exc).__name__) from exc
    finally:
        rows.close()


def _load_dockets(court_dir: Path, progress: _Progress) -> dict[int, _Docket]:
    dockets: dict[int, _Docket] = {}
    table = "dockets"
    with _table_rows(court_dir, table, _DOCKET_COLUMNS, progress) as (columns, rows):
        for fields, _ in rows:
            docket_id = _positive_integer(fields[columns["id"]], table)
            number = fields[columns["docket_number"]]
            dockets[docket_id] = _Docket(
                number if number.strip() else None, fields[columns["court_id"]],
            )
    return dockets


def _load_clusters(court_dir: Path, progress: _Progress) -> dict[int, _Cluster]:
    clusters: dict[int, _Cluster] = {}
    table = "opinion-clusters"
    with _table_rows(court_dir, table, _CLUSTER_COLUMNS, progress) as (columns, rows):
        for fields, _ in rows:
            cluster_id = _positive_integer(fields[columns["id"]], table)
            approximate = fields[columns["date_filed_is_approximate"]]
            if approximate not in {"t", "f"}:
                raise CaselawFailure("malformed", table=table, error="ValueError")
            clusters[cluster_id] = _Cluster(
                _positive_integer(fields[columns["docket_id"]], table),
                _date(fields[columns["date_filed"]], table),
                approximate == "t", fields[columns["precedential_status"]],
            )
    return clusters


def _load_citations(court_dir: Path, progress: _Progress) -> dict[int, list[str]]:
    citations: dict[int, list[str]] = {}
    table = "citations"
    with _table_rows(court_dir, table, _CITATION_COLUMNS, progress) as (columns, rows):
        for fields, _ in rows:
            cluster_id = _positive_integer(fields[columns["cluster_id"]], table)
            cite = " ".join(fields[columns[name]] for name in ("volume", "reporter", "page"))
            citations.setdefault(cluster_id, []).append(cite)
    return citations


def _document_id(opinion_id: int, snapshot_date: date) -> str:
    payload = f"caselaw\n{opinion_id}\n{snapshot_date.isoformat()}".encode()
    return hashlib.sha256(payload).hexdigest()


def _precedential(raw: str) -> str:
    # The two CourtListener values that state a status; every other is unknown.
    return {"Published": "published", "Unpublished": "unpublished"}.get(raw, "unknown")


def _put(host: RealHost, root: Path, text: str) -> str:
    stored = gideon.host.cas.put(host, text.encode("utf-8"), root=root)
    if isinstance(stored, Problem):
        # The store's problem may contain a path or supplied content.
        raise CaselawFailure("store")
    return stored


def _parse_opinion(
    record: DocumentRecord, host: RealHost, store_root: Path, doc_id: str,
    chosen: tuple[str, str] | None, progress: _Progress,
    clock: Callable[[], datetime],
) -> None:
    if chosen is None:
        record.finish_failed(doc_id, "no-text", clock())
        progress.failed += 1
        return
    column, raw = chosen
    original = _put(host, store_root, raw)
    try:
        parsed = opiniontext.canonical_text(column, raw)
    except opiniontext.OpinionTextFailure as exc:
        record.finish_failed(doc_id, exc.reason, clock(), original)
        progress.failed += 1
        return
    except Exception as exc:  # noqa: BLE001 - one opinion's parse is accounted for.
        progress.error = type(exc).__name__
        record.finish_failed(doc_id, "unparseable", clock(), original)
        progress.failed += 1
        return
    canonical = _put(host, store_root, parsed.text)
    record.finish_ready(doc_id, original, canonical, clock())
    progress.ready += 1


@contextmanager
def _court_lock(path: Path) -> Iterator[None]:
    if path.is_symlink():
        raise CaselawFailure("stage-mismatch")
    descriptor = os.open(
        path, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, fetch.PARTIAL_MODE,
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CaselawFailure("busy") from exc
        os.fchmod(descriptor, fetch.PARTIAL_MODE)
        yield
    finally:
        os.close(descriptor)


def ingest(
    snapshots_root: Path, work_root: Path, store_root: Path,
    label: str, snapshot: str, court: str, limit: int | None, job: int,
    record: DocumentRecord,
    *, clock: Callable[[], datetime] = _clock,
    monotonic: Callable[[], float] = time.monotonic,
) -> IngestCounts:
    """Stream one staged court into document rows and stored text objects."""

    del snapshots_root
    started = monotonic()
    progress = _Progress(job, label, court, started, started, monotonic)
    try:
        validate_arguments(label, snapshot, court, limit)
        try:
            whole = staging.work_path(work_root, label, snapshot)
            staged = staging.read_record(whole / staging.STAGE_RECORD_NAME)
        except staging.StageFailure as exc:
            reason = "local" if exc.reason == "local" else "stage-mismatch"
            raise CaselawFailure(reason, error=exc.error) from exc
        if staged is None or not whole.is_dir():
            raise CaselawFailure("missing-stage")
        if staged.label != label or staged.snapshot != snapshot or court not in staged.courts:
            raise CaselawFailure("stage-mismatch")
        court_dir = whole / court
        if court_dir.is_symlink() or not court_dir.is_dir():
            raise CaselawFailure("stage-mismatch")
        lock_path = whole / f"{court}{LOCK_SUFFIX}"
        failure_path = whole / f"{court}.{CASELAW_FAILURE_NAME}"
        with _court_lock(lock_path):
            if failure_path.is_symlink():
                raise CaselawFailure("stage-mismatch")
            failure_path.unlink(missing_ok=True)
            try:
                dockets = _load_dockets(court_dir, progress)
                clusters = _load_clusters(court_dir, progress)
                citations = _load_citations(court_dir, progress)
                snapshot_date = date.fromisoformat(snapshot[-10:])
                source = snapshot[:-staging.DATE_SUFFIX_LENGTH]
                present = record.present(source, snapshot_date, court)
                host = RealHost()
                with _table_rows(court_dir, "opinions", _OPINION_COLUMNS, progress) as (columns, rows):
                    for fields, _ in itertools.islice(rows, limit):
                        progress.seen += 1
                        opinion_id = _positive_integer(fields[columns["id"]], "opinions")
                        cluster_id = _positive_integer(fields[columns["cluster_id"]], "opinions")
                        chosen = opiniontext.choose({
                            name: fields[columns[name]] for name in opiniontext.TEXT_SOURCES
                        })
                        previous = present.get(opinion_id)
                        # Only a processing row is unfinished; every other status is final here.
                        if previous is not None and previous.status != "processing":
                            progress.present += 1
                            continue
                        if previous is not None and previous.attempts >= MAX_ATTEMPTS:
                            record.finish_failed(previous.doc_id, "interrupted", clock())
                            progress.failed += 1
                            progress.interrupted += 1
                            continue
                        active_doc: str | None = None
                        try:
                            if previous is not None:
                                record.retry(previous.doc_id)
                                active_doc = previous.doc_id
                                progress.retried += 1
                            else:
                                cluster = clusters.get(cluster_id)
                                docket = dockets.get(cluster.docket_id) if cluster else None
                                if cluster is None or docket is None or docket.court != court:
                                    raise CaselawFailure("stage-mismatch", table="opinions")
                                doc_id = _document_id(opinion_id, snapshot_date)
                                record.begin(
                                    NewDocument(
                                        doc_id, "caselaw", source, snapshot_date,
                                        chosen[0] if chosen else None, clock(),
                                    ),
                                    NewOpinion(
                                        opinion_id, doc_id, court, cluster_id, docket.number,
                                        cluster.decided_date, cluster.approximate,
                                        _precedential(cluster.precedential_raw),
                                        cluster.precedential_raw,
                                        tuple(citations.get(cluster_id, ())),
                                        fields[columns["type"]],
                                    ),
                                )
                                active_doc = doc_id
                            _parse_opinion(
                                record, host, store_root, active_doc, chosen,
                                progress, clock,
                            )
                        except CaselawFailure as exc:
                            if active_doc is not None and exc.reason in {"store", "database", "local"}:
                                with suppress(CaselawFailure, psycopg.Error):
                                    record.give_back(active_doc)
                            raise
                        except OSError as exc:
                            if active_doc is not None:
                                with suppress(CaselawFailure, psycopg.Error):
                                    record.give_back(active_doc)
                            raise CaselawFailure("local", error=type(exc).__name__) from exc
            except BaseException:
                # A close failure must not replace the refusal that ended the run.
                with suppress(CaselawFailure, psycopg.Error, OSError):
                    record.close()
                raise
            else:
                record.close()
        _log(progress, "end")
        return progress.counts()
    except CaselawFailure as exc:
        progress.error = exc.error or exc.reason
        _log(progress, "failed")
        raise
    except OSError as exc:
        progress.error = type(exc).__name__
        _log(progress, "failed")
        raise CaselawFailure("local", error=type(exc).__name__) from exc


def write_job_failure(
    work_root: Path, label: str, snapshot: str, court: str, job: int,
    failure: CaselawFailure, *, clock: Callable[[], datetime] = _clock,
) -> None:
    """Atomically file a safe failure beside a staged court when possible."""

    if failure.reason == "busy":
        return
    if not isinstance(court, str) or re.fullmatch(staging.COURT_PATTERN, court) is None:
        return
    try:
        whole = staging.work_path(work_root, label, snapshot)
        if not whole.is_dir():
            return
        path = whole / f"{court}.{CASELAW_FAILURE_NAME}"
        if path.is_symlink():
            return
        staging.write_failure(path, asdict(CaselawFailureRecord(
            job, court, failure.reason, failure.table, failure.error,
            clock().astimezone(UTC).isoformat(),
        )))
    except (staging.StageFailure, OSError):
        return
