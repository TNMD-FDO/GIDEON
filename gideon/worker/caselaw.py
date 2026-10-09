"""Turn staged case-law opinions into durable document and opinion rows."""

import fcntl
import hashlib
import itertools
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final, Protocol

import psycopg

import gideon.host.cas
from gideon.casecite.found import FoundCitation
from gideon.extraction import grammar
from gideon.host.report import Problem
from gideon.host.sysio import RealHost

from . import (
    anchors,
    citations,
    fetch,
    opiniontext,
    sections,
    settings,
    staging,
    treatment,
)

CASELAW_TASK: Final = "gideon.worker.tasks.caselaw"
CASELAW_QUEUE: Final = "caselaw"
CASELAW_FAILURE_NAME: Final = "caselaw-failed.json"
LOCK_SUFFIX: Final = ".caselaw.lock"
CASELAW_FAILURE_REASONS: Final = frozenset({
    "invalid", "missing-stage", "stage-mismatch", "malformed", "store",
    "database", "local", "busy", "segmenter", "anchors", "text-mismatch",
    "citations", "treatment",
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
_OPINION_COLUMNS: Final = ("id", "cluster_id", "type", "per_curiam", *opiniontext.TEXT_SOURCES)
_POSITIVE_INTEGER: Final = re.compile(r"[0-9]+")
_ISO_DATE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

PRESENT_SQL: Final = (
    "SELECT o.opinion_id, d.doc_id, d.status, d.attempts, d.text_source, "
    "d.sha256, d.canonical_text_sha256, "
    "EXISTS (SELECT 1 FROM sections WHERE doc_id = d.doc_id) AS sectioned, "
    "d.anchored_at IS NOT NULL AS anchored, "
    "d.citations_parsed_at IS NOT NULL AS cited, "
    "d.treatment_pattern_set AS treated_under "
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
    "ingested_at = %s, anchored_at = %s, citations_parsed_at = %s, "
    "treatment_pattern_set = %s, treated_at = %s WHERE doc_id = %s"
)
FINISH_FAILED_SQL: Final = (
    "UPDATE documents SET status = 'failed', failure_reason = %s, sha256 = %s, "
    "ingested_at = %s WHERE doc_id = %s"
)
SECTION_SQL: Final = (
    "INSERT INTO sections (section_id, doc_id, ordinal, section_type, typed_by, "
    "char_start, char_end, label, ref_offset, parent_section_id) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
ANCHOR_SQL: Final = (
    "INSERT INTO anchors (anchor_id, doc_id, kind, label, char_start, char_end, attrs) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)"
)
ANCHORED_SQL: Final = "UPDATE documents SET anchored_at = %s WHERE doc_id = %s"
READ_SECTIONS_SQL: Final = (
    "SELECT section_id, doc_id, ordinal, section_type, typed_by, char_start, "
    "char_end, label, ref_offset, parent_section_id FROM sections "
    "WHERE doc_id = %s ORDER BY ordinal"
)
CITATION_SQL: Final = (
    "INSERT INTO citations (citation_id, doc_id, ordinal, char_start, char_end, "
    "section_id, section_type, cite_type, cite_form, raw_cite, reporter_cite, "
    "pincite, key, to_cluster, pattern_id) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
MARK_CITED_SQL: Final = (
    "UPDATE documents SET citations_parsed_at = %s WHERE doc_id = %s"
)
READ_CITATIONS_SQL: Final = (
    "SELECT citation_id, doc_id, ordinal, char_start, char_end, section_id, "
    "section_type, cite_type, cite_form, raw_cite, reporter_cite, pincite, "
    "key, to_cluster, pattern_id FROM citations WHERE doc_id = %s ORDER BY ordinal"
)
SIGNAL_SQL: Final = (
    "INSERT INTO citation_signals (signal_id, citation_id, doc_id, signal_source, "
    "pattern_set, treatment_signal, qualifier, effective_section, state, "
    "no_state_reason, char_start, char_end, found_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
MARK_TREATED_SQL: Final = (
    "UPDATE documents SET treatment_pattern_set = %s, treated_at = %s WHERE doc_id = %s"
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
    text_source: str | None
    sha256: str | None
    canonical_text_sha256: str | None
    sectioned: bool
    anchored: bool
    cited: bool
    treated_under: str | None

    @property
    def treated(self) -> bool:
        """Whether the current pattern set completed for this document."""

        return self.treated_under == treatment.PATTERN_SET_ID


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
class NewSection:
    """A document's typed text span ready for the record."""

    section_id: str
    doc_id: str
    ordinal: int
    section_type: str
    typed_by: str
    char_start: int
    char_end: int
    label: str | None
    ref_offset: int | None
    parent_section_id: str | None


@dataclass(frozen=True, slots=True)
class NewAnchor:
    """An immutable anchor row ready for the record."""

    anchor_id: str
    doc_id: str
    kind: str
    label: str
    char_start: int
    char_end: int
    attrs: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class IngestCounts:
    """Counts from one bounded or whole court stream."""

    seen: int
    present: int
    ready: int
    failed: int
    retried: int
    interrupted: int
    sectioned: int
    anchored: int
    pgmap_checked: int
    pgmap_disagreeing: int
    cited: int
    treated: int
    signalled: int


class DocumentRecord(Protocol):
    """The row operations needed while ingesting one court."""

    def present(self, source: str, snapshot_date: date, court: str) -> dict[int, PresentRow]: ...
    def begin(self, document: NewDocument, opinion: NewOpinion) -> None: ...
    def retry(self, doc_id: str) -> None: ...
    def give_back(self, doc_id: str) -> None: ...
    def finish_ready(
        self, doc_id: str, sha256: str, canonical_sha256: str, at: datetime,
        sections: tuple[NewSection, ...], anchors: tuple[NewAnchor, ...],
        citation_rows: tuple[citations.NewCitation, ...],
        signal_rows: tuple[treatment.NewSignal, ...],
    ) -> None: ...
    def write_sections(self, doc_id: str, sections: tuple[NewSection, ...]) -> None: ...
    def write_anchors(
        self, doc_id: str, anchors: tuple[NewAnchor, ...], at: datetime,
    ) -> None: ...
    def read_sections(self, doc_id: str) -> tuple[NewSection, ...]: ...
    def write_citations(
        self, doc_id: str, citation_rows: tuple[citations.NewCitation, ...], at: datetime,
    ) -> None: ...
    def read_citations(self, doc_id: str) -> tuple[citations.NewCitation, ...]: ...
    def write_signals(
        self, doc_id: str, signal_rows: tuple[treatment.NewSignal, ...], at: datetime,
    ) -> None: ...
    def finish_failed(
        self, doc_id: str, reason: str, at: datetime, sha256: str | None = None,
    ) -> None: ...
    def close(self) -> None: ...


def _section_statements(
    doc_id: str, rows: tuple[NewSection, ...],
) -> tuple[tuple[str, tuple[object, ...]], ...]:
    return tuple(
        (SECTION_SQL, (
            row.section_id, doc_id, row.ordinal, row.section_type, row.typed_by,
            row.char_start, row.char_end, row.label, row.ref_offset,
            row.parent_section_id,
        ))
        for row in rows
    )


def _anchor_statements(
    doc_id: str, rows: tuple[NewAnchor, ...],
) -> tuple[tuple[str, tuple[object, ...]], ...]:
    return tuple(
        (ANCHOR_SQL, (
            row.anchor_id, doc_id, row.kind, row.label, row.char_start, row.char_end,
            json.dumps(dict(row.attrs), sort_keys=True),
        ))
        for row in rows
    )


def _citation_statements(
    doc_id: str, rows: tuple[citations.NewCitation, ...],
) -> tuple[tuple[str, tuple[object, ...]], ...]:
    return tuple(
        (CITATION_SQL, (
            row.citation_id, doc_id, row.ordinal, row.char_start, row.char_end,
            row.section_id, row.section_type, row.cite_type, row.cite_form,
            row.raw_cite, row.reporter_cite, row.pincite, row.key,
            row.to_cluster, row.pattern_id,
        ))
        for row in rows
    )


def _signal_statements(
    doc_id: str, rows: tuple[treatment.NewSignal, ...], at: datetime,
) -> tuple[tuple[str, tuple[object, ...]], ...]:
    return tuple(
        (SIGNAL_SQL, (
            row.signal_id, row.citation_id, doc_id, row.signal_source,
            row.pattern_set, row.treatment_signal, row.qualifier,
            row.effective_section, row.state, row.no_state_reason,
            row.char_start, row.char_end, at,
        ))
        for row in rows
    )


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
            int(opinion_id): PresentRow(
                str(doc_id), str(status), int(attempts),
                str(text_source) if text_source is not None else None,
                str(sha256) if sha256 is not None else None,
                str(canonical_sha256) if canonical_sha256 is not None else None,
                bool(sectioned), bool(anchored), bool(cited),
                str(treated_under) if treated_under is not None else None,
            )
            for opinion_id, doc_id, status, attempts, text_source, sha256,
            canonical_sha256, sectioned, anchored, cited, treated_under in rows
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

    def finish_ready(
        self, doc_id: str, sha256: str, canonical_sha256: str, at: datetime,
        sections: tuple[NewSection, ...], anchors: tuple[NewAnchor, ...],
        citation_rows: tuple[citations.NewCitation, ...],
        signal_rows: tuple[treatment.NewSignal, ...],
    ) -> None:
        """Commit a ready document and all completed passes together."""

        self._run(((FINISH_READY_SQL, (
                       sha256, canonical_sha256, at, at, at,
                       treatment.PATTERN_SET_ID, at, doc_id,
                   )),
                   *_section_statements(doc_id, sections),
                   *_anchor_statements(doc_id, anchors),
                   *_citation_statements(doc_id, citation_rows),
                   *_signal_statements(doc_id, signal_rows, at)))

    def write_sections(self, doc_id: str, sections: tuple[NewSection, ...]) -> None:
        """Commit sections for an already ready document."""

        self._run(_section_statements(doc_id, sections))

    def write_anchors(
        self, doc_id: str, anchors: tuple[NewAnchor, ...], at: datetime,
    ) -> None:
        """Commit anchors and the anchoring mark for an already ready document."""

        self._run((*_anchor_statements(doc_id, anchors),
                   (ANCHORED_SQL, (at, doc_id))))

    def read_sections(self, doc_id: str) -> tuple[NewSection, ...]:
        """Read a ready document's recorded sections in ordinal order."""

        rows = self._run(((READ_SECTIONS_SQL, (doc_id,)),), read=True)
        return tuple(NewSection(*row) for row in rows)

    def write_citations(
        self, doc_id: str, citation_rows: tuple[citations.NewCitation, ...], at: datetime,
    ) -> None:
        """Commit a ready document's citations and completed mark together."""

        self._run((*_citation_statements(doc_id, citation_rows),
                   (MARK_CITED_SQL, (at, doc_id))))

    def read_citations(self, doc_id: str) -> tuple[citations.NewCitation, ...]:
        """Read recorded citation edges in ordinal order."""

        rows = self._run(((READ_CITATIONS_SQL, (doc_id,)),), read=True)
        return tuple(citations.NewCitation(*row) for row in rows)

    def write_signals(
        self, doc_id: str, signal_rows: tuple[treatment.NewSignal, ...], at: datetime,
    ) -> None:
        """Commit treatment findings and the pattern-set mark together."""

        self._run((*_signal_statements(doc_id, signal_rows, at),
                   (MARK_TREATED_SQL, (treatment.PATTERN_SET_ID, at, doc_id))))

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
    sectioned: int = 0
    anchored: int = 0
    pgmap_checked: int = 0
    pgmap_disagreeing: int = 0
    cited: int = 0
    treated: int = 0
    signalled: int = 0
    ambiguous: int = 0
    error: str = "-"

    def counts(self) -> IngestCounts:
        return IngestCounts(
            self.seen, self.present, self.ready, self.failed,
            self.retried, self.interrupted, self.sectioned, self.anchored,
            self.pgmap_checked, self.pgmap_disagreeing, self.cited,
            self.treated, self.signalled,
        )


def _log(progress: _Progress, action: str) -> None:
    logger.info(
        "action=caselaw_%s job_id=%d label=%s court=%s seen=%d present=%d "
        "ready=%d failed=%d retried=%d interrupted=%d sectioned=%d anchored=%d "
        "pgmap_checked=%d pgmap_disagreeing=%d cited=%d treated=%d "
        "signalled=%d ambiguous=%d seconds=%.3f error=%s",
        action, progress.job, progress.label, progress.court, progress.seen,
        progress.present, progress.ready, progress.failed, progress.retried,
        progress.interrupted, progress.sectioned, progress.anchored,
        progress.pgmap_checked, progress.pgmap_disagreeing, progress.cited,
        progress.treated, progress.signalled,
        progress.ambiguous,
        max(0.0, progress.monotonic() - progress.started),
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
    courts: Mapping[str, Mapping[str, object]] | None,
) -> dict[str, treatment.Geography]:
    """Refuse queue arguments outside the stage and geography grammars."""

    if (
        not _valid_dated_name(label, staging.LABEL_PATTERN)
        or not _valid_dated_name(snapshot, staging.SNAPSHOT_PATTERN)
        or not isinstance(court, str)
        or re.fullmatch(staging.COURT_PATTERN, court) is None
        or limit is not None and (type(limit) is not int or not 1 <= limit <= LIMIT_MAX)
        or not isinstance(courts, Mapping)
    ):
        raise CaselawFailure("invalid")
    parsed: dict[str, treatment.Geography] = {}
    for key, value in courts.items():
        if not isinstance(key, str) or re.fullmatch(staging.COURT_PATTERN, key) is None:
            raise CaselawFailure("invalid")
        try:
            parsed[key] = treatment.parse_geography(value)
        except ValueError as exc:
            raise CaselawFailure("invalid") from exc
    return parsed


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


def _load_cite_map(
    whole: Path, courts: list[str], court: str, progress: _Progress,
) -> tuple[
    dict[int, list[str]], dict[tuple[str, str, str], int | None], dict[int, str],
]:
    citing_court: dict[int, list[str]] = {}
    by_key: dict[tuple[str, str, str], int | None] = {}
    cluster_court: dict[int, str] = {}
    table = "citations"
    for staged_court in courts:
        court_dir = whole / staged_court
        if court_dir.is_symlink() or not court_dir.is_dir():
            raise CaselawFailure("stage-mismatch", table=table)
        with _table_rows(court_dir, table, _CITATION_COLUMNS, progress) as (columns, rows):
            for fields, _ in rows:
                cluster_id = _positive_integer(fields[columns["cluster_id"]], table)
                cluster_court[cluster_id] = staged_court
                key = (
                    fields[columns["volume"]], fields[columns["reporter"]],
                    fields[columns["page"]],
                )
                if staged_court == court:
                    citing_court.setdefault(cluster_id, []).append(" ".join(key))
                if key not in by_key:
                    by_key[key] = cluster_id
                elif by_key[key] is not None and by_key[key] != cluster_id:
                    by_key[key] = None
                    progress.ambiguous += 1
    return citing_court, by_key, cluster_court


def _document_id(opinion_id: int, snapshot_date: date) -> str:
    payload = f"caselaw\n{opinion_id}\n{snapshot_date.isoformat()}".encode()
    return hashlib.sha256(payload).hexdigest()


def _section_id(doc_id: str, start: int, end: int) -> str:
    return hashlib.sha256(f"{doc_id}\n{start}\n{end}".encode()).hexdigest()


def _anchor_id(doc_id: str, kind: str, scheme: str, start: int, end: int) -> str:
    return hashlib.sha256(f"{doc_id}\n{kind}\n{scheme}\n{start}\n{end}".encode()).hexdigest()


def _section_rows(
    doc_id: str, parsed: opiniontext.Parsed, *, opinion_type: str,
    per_curiam: bool, column: str,
) -> tuple[tuple[NewSection, ...], tuple[sections.Section, ...]]:
    try:
        result = sections.segment(
            parsed, opinion_type=opinion_type, per_curiam=per_curiam, column=column,
        )
        ids = tuple(_section_id(doc_id, section.char_start, section.char_end)
                    for section in result)
        rows = tuple(
            NewSection(
                ids[ordinal], doc_id, ordinal, section.section_type,
                section.typed_by, section.char_start, section.char_end,
                section.label, section.ref_offset,
                ids[section.parent] if section.parent is not None else None,
            )
            for ordinal, section in enumerate(result)
        )
        return rows, result
    except Exception as exc:  # any segmenter exception is a defect, filed by class.
        raise CaselawFailure("segmenter", error=type(exc).__name__) from exc


def _anchor_rows(
    doc_id: str, parsed: opiniontext.Parsed, section_result: tuple[sections.Section, ...],
) -> tuple[tuple[NewAnchor, ...], int, int]:
    try:
        result = anchors.anchor(parsed, section_result)
        rows = tuple(
            NewAnchor(
                _anchor_id(doc_id, item.kind, dict(item.attrs)["scheme"],
                           item.char_start, item.char_end),
                doc_id, item.kind, item.label, item.char_start, item.char_end,
                dict(item.attrs),
            )
            for item in result.anchors
        )
        return rows, result.pgmap_checked, result.pgmap_disagreeing
    except Exception as exc:  # an anchorer defect is filed by class before this opinion's write.
        raise CaselawFailure("anchors", error=type(exc).__name__) from exc


def _precedential(raw: str) -> str:
    # The two CourtListener values that state a status; every other is unknown.
    return {"Published": "published", "Unpublished": "unpublished"}.get(raw, "unknown")


def _put(host: RealHost, root: Path, text: str) -> str:
    stored = gideon.host.cas.put(host, text.encode("utf-8"), root=root)
    if isinstance(stored, Problem):
        # The store's problem may contain a path or supplied content.
        raise CaselawFailure("store")
    return stored


def _citation_rows(
    doc_id: str, text: str, section_rows: tuple[NewSection, ...],
    find: Callable[[str], tuple[FoundCitation, ...]],
    resolve: citations.Resolver,
) -> tuple[citations.NewCitation, ...]:
    try:
        return citations.edges(
            doc_id, text, section_rows, find(text), grammar.extract(text), resolve,
        )
    except Exception as exc:  # a citation defect fails the job.
        raise CaselawFailure("citations", error=type(exc).__name__) from exc


def _signal_rows(
    doc_id: str, text: str, section_rows: tuple[NewSection, ...],
    citation_rows: tuple[citations.NewCitation, ...],
    citing: treatment.Geography, cluster_court: Mapping[int, str],
    geography: Mapping[str, treatment.Geography],
) -> tuple[treatment.NewSignal, ...]:
    try:
        return treatment.signals(
            doc_id, text, section_rows, citation_rows, citing,
            cluster_court.get, geography.get,
        )
    except Exception as exc:  # a treatment defect fails the job, filed by class.
        raise CaselawFailure("treatment", error=type(exc).__name__) from exc


def _parse_opinion(
    record: DocumentRecord, host: RealHost, store_root: Path, doc_id: str,
    chosen: tuple[str, str] | None, opinion_type: str, per_curiam: bool,
    progress: _Progress,
    clock: Callable[[], datetime],
    find: Callable[[str], tuple[FoundCitation, ...]],
    resolve: citations.Resolver,
    citing: treatment.Geography, cluster_court: Mapping[int, str],
    geography: Mapping[str, treatment.Geography],
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
    section_rows, section_result = _section_rows(
        doc_id, parsed, opinion_type=opinion_type,
        per_curiam=per_curiam, column=column,
    )
    anchor_rows, checked, disagreeing = _anchor_rows(doc_id, parsed, section_result)
    citation_rows = _citation_rows(doc_id, parsed.text, section_rows, find, resolve)
    signal_rows = _signal_rows(
        doc_id, parsed.text, section_rows, citation_rows,
        citing, cluster_court, geography,
    )
    canonical = _put(host, store_root, parsed.text)
    record.finish_ready(
        doc_id, original, canonical, clock(), section_rows, anchor_rows, citation_rows,
        signal_rows,
    )
    progress.ready += 1
    progress.anchored += 1
    progress.pgmap_checked += checked
    progress.pgmap_disagreeing += disagreeing
    progress.cited += 1
    progress.treated += 1
    progress.signalled += len(signal_rows)


def _backfill(
    record: DocumentRecord, host: RealHost, store_root: Path, present: PresentRow,
    *, opinion_type: str, per_curiam: bool, progress: _Progress,
    clock: Callable[[], datetime],
    find: Callable[[str], tuple[FoundCitation, ...]],
    resolve: citations.Resolver,
    citing: treatment.Geography, cluster_court: Mapping[int, str],
    geography: Mapping[str, treatment.Geography],
) -> None:
    if present.canonical_text_sha256 is None:
        raise CaselawFailure("text-mismatch")
    if present.sectioned and present.anchored:
        canonical = gideon.host.cas.get(
            host, present.canonical_text_sha256, root=store_root,
        )
        if isinstance(canonical, Problem):
            raise CaselawFailure("store")
        if hashlib.sha256(canonical).hexdigest() != present.canonical_text_sha256:
            raise CaselawFailure("text-mismatch")
        try:
            text = canonical.decode("utf-8")
        except UnicodeError as exc:
            raise CaselawFailure("text-mismatch", error=type(exc).__name__) from exc
        section_rows = record.read_sections(present.doc_id)
        citation_rows = (
            record.read_citations(present.doc_id) if present.cited else
            _citation_rows(present.doc_id, text, section_rows, find, resolve)
        )
        signal_rows = () if present.treated else _signal_rows(
            present.doc_id, text, section_rows, citation_rows,
            citing, cluster_court, geography,
        )
        if not present.cited:
            record.write_citations(present.doc_id, citation_rows, clock())
            progress.cited += 1
        if not present.treated:
            record.write_signals(present.doc_id, signal_rows, clock())
            progress.treated += 1
            progress.signalled += len(signal_rows)
        return
    if present.sha256 is None or present.text_source is None:
        raise CaselawFailure("text-mismatch")
    original = gideon.host.cas.get(host, present.sha256, root=store_root)
    if isinstance(original, Problem):
        raise CaselawFailure("store")
    try:
        parsed = opiniontext.canonical_text(present.text_source, original.decode("utf-8"))
    except Exception as exc:  # a walk that no longer reads its original is a corpus event.
        raise CaselawFailure("text-mismatch", error=type(exc).__name__) from exc
    if hashlib.sha256(parsed.text.encode("utf-8")).hexdigest() != present.canonical_text_sha256:
        raise CaselawFailure("text-mismatch")
    section_rows, section_result = _section_rows(
        present.doc_id, parsed, opinion_type=opinion_type,
        per_curiam=per_curiam, column=present.text_source,
    )
    anchor_rows, checked, disagreeing = _anchor_rows(present.doc_id, parsed, section_result)
    recorded_sections = record.read_sections(present.doc_id) if present.sectioned else section_rows
    citation_rows = (
        record.read_citations(present.doc_id) if present.cited else
        _citation_rows(present.doc_id, parsed.text, recorded_sections, find, resolve)
    )
    signal_rows = () if present.treated else _signal_rows(
        present.doc_id, parsed.text, recorded_sections, citation_rows,
        citing, cluster_court, geography,
    )
    if not present.sectioned:
        record.write_sections(present.doc_id, section_rows)
        progress.sectioned += 1
    if not present.anchored:
        record.write_anchors(present.doc_id, anchor_rows, clock())
        progress.anchored += 1
    progress.pgmap_checked += checked
    progress.pgmap_disagreeing += disagreeing
    if not present.cited:
        record.write_citations(present.doc_id, citation_rows, clock())
        progress.cited += 1
    if not present.treated:
        record.write_signals(present.doc_id, signal_rows, clock())
        progress.treated += 1
        progress.signalled += len(signal_rows)


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
    *, courts: Mapping[str, Mapping[str, object]] | None = None,
    clock: Callable[[], datetime] = _clock,
    monotonic: Callable[[], float] = time.monotonic,
    find: Callable[[str], tuple[FoundCitation, ...]] | None = None,
) -> IngestCounts:
    """Stream one staged court into document rows and stored text objects."""

    del snapshots_root
    started = monotonic()
    progress = _Progress(job, label, court, started, started, monotonic)
    try:
        geography = validate_arguments(label, snapshot, court, limit, courts)
        try:
            whole = staging.work_path(work_root, label, snapshot)
            staged = staging.read_record(whole / staging.STAGE_RECORD_NAME)
        except staging.StageFailure as exc:
            reason = "local" if exc.reason == "local" else "stage-mismatch"
            raise CaselawFailure(reason, error=exc.error) from exc
        if staged is None or not whole.is_dir():
            raise CaselawFailure("missing-stage")
        if (staged.label != label or staged.snapshot != snapshot
                or court not in staged.courts or set(geography) != set(staged.courts)):
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
                reporter_cites, cite_map, cluster_court = _load_cite_map(
                    whole, staged.courts, court, progress,
                )
                if find is None:
                    try:
                        from gideon.casecite.adapter import find_citations
                    except ImportError as exc:
                        raise CaselawFailure("citations", error="ImportError") from exc
                    find = find_citations
                snapshot_date = date.fromisoformat(snapshot[-10:])
                source = snapshot[:-staging.DATE_SUFFIX_LENGTH]
                present = record.present(source, snapshot_date, court)
                host = RealHost()
                with _table_rows(court_dir, "opinions", _OPINION_COLUMNS, progress) as (columns, rows):
                    for fields, _ in itertools.islice(rows, limit):
                        progress.seen += 1
                        opinion_id = _positive_integer(fields[columns["id"]], "opinions")
                        cluster_id = _positive_integer(fields[columns["cluster_id"]], "opinions")
                        per_curiam_value = fields[columns["per_curiam"]]
                        if per_curiam_value not in {"t", "f"}:
                            raise CaselawFailure("malformed", table="opinions", error="ValueError")
                        per_curiam = per_curiam_value == "t"
                        opinion_type = fields[columns["type"]]
                        chosen = opiniontext.choose({
                            name: fields[columns[name]] for name in opiniontext.TEXT_SOURCES
                        })
                        previous = present.get(opinion_id)
                        if (previous is not None and previous.status == "ready"
                                and not (previous.sectioned and previous.anchored
                                         and previous.cited and previous.treated)):
                            _backfill(
                                record, host, store_root, previous,
                                opinion_type=opinion_type, per_curiam=per_curiam,
                                progress=progress, clock=clock, find=find,
                                resolve=cite_map.get,
                                citing=geography[court], cluster_court=cluster_court,
                                geography=geography,
                            )
                            continue
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
                                        tuple(reporter_cites.get(cluster_id, ())),
                                        opinion_type,
                                    ),
                                )
                                active_doc = doc_id
                            _parse_opinion(
                                record, host, store_root, active_doc, chosen,
                                opinion_type, per_curiam, progress, clock,
                                find, cite_map.get,
                                geography[court], cluster_court, geography,
                            )
                        except CaselawFailure as exc:
                            if active_doc is not None and exc.reason in {
                                "store", "database", "local", "segmenter", "anchors",
                                "citations", "treatment",
                            }:
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
