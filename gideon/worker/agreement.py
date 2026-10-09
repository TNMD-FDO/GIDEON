"""Stage CourtListener's citation map and compare it with ready case edges."""

import bz2
import json
import logging
import math
import os
import re
import shutil
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final, Protocol

import psycopg

from . import fetch, settings, staging

AGREEMENT_TASK: Final = "gideon.worker.tasks.agreement"
AGREEMENT_QUEUE: Final = "caselaw"
AGREEMENT_TABLE: Final = "citation-map"
AGREEMENT_MAP_DIRECTORY: Final = "citation-map"
AGREEMENT_MAP_RECORD_NAME: Final = "map.json"
AGREEMENT_RECORD_NAME: Final = "agreement.json"
AGREEMENT_FAILURE_NAME: Final = "agreement-failed.json"
AGREEMENT_FAILURE_REASONS: Final = frozenset({
    "invalid", "missing-stage", "stage-mismatch", "missing-input",
    "input-mismatch", "malformed", "database", "local", "busy",
})
AGREEMENT_LOCK_SUFFIX: Final = ".agreement.lock"
MAP_LOCK_NAME: Final = "citation-map.lock"
MAP_COLUMNS: Final = ("citing_opinion_id", "cited_opinion_id")
OPINION_COLUMNS: Final = ("id", "cluster_id")
CITATION_COLUMNS: Final = ("cluster_id", "volume", "reporter", "page")
# exempt: the progress interval bounds content-free worker log traffic.
PROGRESS_INTERVAL_SECONDS: Final = 60
_POSITIVE_INTEGER: Final = re.compile(r"[0-9]+")
READY_EDGES_SQL: Final = (
    "SELECT o.opinion_id, c.to_cluster "
    "FROM opinions AS o JOIN documents AS d ON d.doc_id = o.doc_id "
    "LEFT JOIN citations AS c ON c.doc_id = d.doc_id "
    "AND c.cite_type = 'case_cite' AND c.to_cluster IS NOT NULL "
    "WHERE d.source = %s AND d.source_snapshot = %s AND o.court = %s "
    "AND d.status = 'ready'"
)
UNRESOLVED_CITES_SQL: Final = (
    "SELECT o.opinion_id, c.reporter_cite "
    "FROM opinions AS o JOIN documents AS d ON d.doc_id = o.doc_id "
    "JOIN citations AS c ON c.doc_id = d.doc_id "
    "WHERE d.source = %s AND d.source_snapshot = %s AND o.court = %s "
    "AND d.status = 'ready' AND c.cite_type = 'case_cite' "
    "AND c.to_cluster IS NULL AND c.reporter_cite IS NOT NULL"
)

logger = logging.getLogger(__name__)


def _clock() -> datetime:
    return datetime.now(UTC)


class AgreementFailure(Exception):
    """A safe agreement refusal with a reason from the closed vocabulary."""

    def __init__(
        self, reason: str, *, table: str | None = None, error: str | None = None,
    ) -> None:
        if reason not in AGREEMENT_FAILURE_REASONS:
            raise ValueError("unknown agreement failure reason")
        self.reason = reason
        self.table = table
        self.error = error
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class MapRecord:
    """The whole staged map's content-free record beside its court files."""

    label: str
    source: str
    snapshot: str
    courts: list[str]
    input: dict[str, str | int]
    records: int
    counts: dict[str, int]
    opinions: dict[str, int]
    job: int
    seconds: float
    staged_at: str
    schema: int = 1


@dataclass(frozen=True, slots=True)
class AgreementFailureRecord:
    """The content-free failed-job outcome beside a staged court."""

    job: int
    court: str
    reason: str
    table: str | None
    error: str | None
    at: str
    schema: int = 1


@dataclass(frozen=True, slots=True)
class Comparison:
    """The nine counts comparing resolved and staged-map citation pairs."""

    documents: int
    gideon_pairs: int
    map_rows: int
    map_outside: int
    map_pairs: int
    agreed: int
    gideon_only: int
    map_only_seen: int
    map_only_missed: int


@dataclass(frozen=True, slots=True)
class AgreementRecord:
    """One court's computed agreement figure in the work directory."""

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
    schema: int = 1


class EdgeRecord(Protocol):
    """The ready and unresolved case edges used by the comparison."""

    def ready_edges(self, source: str, snapshot_date: date, court: str) -> dict[int, set[int]]: ...
    def unresolved_cites(self, source: str, snapshot_date: date, court: str) -> dict[int, set[str]]: ...
    def close(self) -> None: ...


class PsycopgEdges:
    """Read one court's ready resolved and unresolved case-cite rows."""

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
                raise AgreementFailure("database", error=type(exc).__name__) from exc
            except settings.WorkerSettingsError as exc:
                raise AgreementFailure("local", error=type(exc).__name__) from exc
        return self._connection

    def _read(self, statement: str, source: str, snapshot_date: date, court: str) -> list[tuple[Any, ...]]:
        connection = self._get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(statement, (source, snapshot_date, court))
                rows: list[tuple[Any, ...]] = cursor.fetchall()
            connection.commit()
            return rows
        except psycopg.Error as exc:
            with suppress(psycopg.Error):
                connection.rollback()
            raise AgreementFailure("database", error=type(exc).__name__) from exc

    def ready_edges(self, source: str, snapshot_date: date, court: str) -> dict[int, set[int]]:
        """Include each ready opinion, including one with no resolved pair."""

        edges: dict[int, set[int]] = {}
        for opinion_id, cluster_id in self._read(READY_EDGES_SQL, source, snapshot_date, court):
            targets = edges.setdefault(int(opinion_id), set())
            if cluster_id is not None:
                targets.add(int(cluster_id))
        return edges

    def unresolved_cites(self, source: str, snapshot_date: date, court: str) -> dict[int, set[str]]:
        """Return each ready opinion's unresolved reporter-cite strings."""

        cites: dict[int, set[str]] = {}
        for opinion_id, reporter_cite in self._read(
            UNRESOLVED_CITES_SQL, source, snapshot_date, court,
        ):
            cites.setdefault(int(opinion_id), set()).add(str(reporter_cite))
        return cites

    def close(self) -> None:
        """Close the read connection, if one was opened."""

        if self._connection is not None:
            try:
                self._connection.close()
            except psycopg.Error as exc:
                with suppress(psycopg.Error):
                    self._connection.rollback()
                raise AgreementFailure("database", error=type(exc).__name__) from exc
            finally:
                self._connection = None


@dataclass
class _Progress:
    job: int
    label: str
    monotonic: Callable[[], float]
    started: float
    last_progress: float
    records: dict[str, int]

    def seconds(self) -> float:
        return max(0.0, self.monotonic() - self.started)

    def row(self, table: str) -> None:
        self.records[table] = self.records.get(table, 0) + 1
        now = self.monotonic()
        if now - self.last_progress >= PROGRESS_INTERVAL_SECONDS:
            logger.info(
                "action=agreement_progress job_id=%d label=%s table=%s records=%d seconds=%.3f",
                self.job, self.label, table, self.records[table], self.seconds(),
            )
            self.last_progress = now


def validate_arguments(label: object, snapshot: object, court: object, input: object) -> None:
    """Refuse queue arguments outside the staged-corpus and input grammars."""

    if (
        not staging._dated_name(label, staging.LABEL_PATTERN)
        or not staging._dated_name(snapshot, staging.SNAPSHOT_PATTERN)
        or not isinstance(court, str)
        or re.fullmatch(staging.COURT_PATTERN, court) is None
        or not staging.valid_input(input)
    ):
        raise AgreementFailure("invalid")


def _positive_integer(value: str, table: str) -> int:
    if _POSITIVE_INTEGER.fullmatch(value) is None:
        raise AgreementFailure("malformed", table=table, error="ValueError")
    try:
        number = int(value)
    except ValueError as exc:
        raise AgreementFailure("malformed", table=table, error=type(exc).__name__) from exc
    if number < 1:
        raise AgreementFailure("malformed", table=table, error="ValueError")
    return number


def _staged_failure(
    exc: staging.StageFailure, table: str, missing: str,
) -> AgreementFailure:
    """Map a staged file's refusal: a missing or linked file, else its rows."""

    if exc.reason == "invalid":
        return AgreementFailure(missing, table=table)
    return AgreementFailure("malformed", table=table, error=exc.error or type(exc).__name__)


def _load_opinions(
    work_dir: Path, staged: staging.StageRecord, progress: _Progress,
) -> dict[int, tuple[int, int]]:
    """Index staged opinion ids by court position and cluster id."""

    opinions: dict[int, tuple[int, int]] = {}
    table = "opinions"
    for index, court in enumerate(staged.courts):
        court_dir = work_dir / court
        if court_dir.is_symlink() or not court_dir.is_dir():
            raise AgreementFailure("stage-mismatch", table=table)
        try:
            with staging.staged_rows(
                court_dir / f"{table}.csv", table, OPINION_COLUMNS, progress.row,
            ) as (columns, rows):
                for fields, _ in rows:
                    opinion = _positive_integer(fields[columns["id"]], table)
                    cluster = _positive_integer(fields[columns["cluster_id"]], table)
                    if opinion in opinions:
                        raise AgreementFailure("malformed", table=table)
                    opinions[opinion] = (index, cluster)
        except staging.StageFailure as exc:
            raise _staged_failure(exc, table, "stage-mismatch") from exc
        except OSError as exc:
            raise AgreementFailure("local", table=table, error=type(exc).__name__) from exc
    return opinions


def _read_map_record(path: Path, courts: list[str]) -> MapRecord:
    if path.is_symlink() or not path.is_file():
        raise AgreementFailure("invalid")
    try:
        with path.open(encoding="utf-8") as source:
            value = json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgreementFailure("invalid", error=type(exc).__name__) from exc
    if not isinstance(value, dict):
        raise AgreementFailure("invalid")
    try:
        record = MapRecord(**value)
    except TypeError as exc:
        raise AgreementFailure("invalid") from exc
    if (
        type(record.schema) is not int or record.schema != 1
        or not staging._dated_name(record.label, staging.LABEL_PATTERN)
        or not isinstance(record.source, str)
        or re.fullmatch(staging.SEGMENT_PATTERN, record.source) is None
        or not staging._dated_name(record.snapshot, staging.SNAPSHOT_PATTERN)
        or record.source != record.snapshot[:-staging.DATE_SUFFIX_LENGTH]
        or record.courts != courts
        or not staging.valid_input(record.input, sizes=True)
        or type(record.records) is not int or record.records < 0
        or not isinstance(record.counts, dict) or set(record.counts) != set(courts)
        or any(type(count) is not int or count < 0 for count in record.counts.values())
        or sum(record.counts.values()) > record.records
        or not isinstance(record.opinions, dict) or set(record.opinions) != set(courts)
        or any(type(count) is not int or count < 0 for count in record.opinions.values())
        or type(record.job) is not int or record.job < 0
        or type(record.seconds) not in {int, float}
        or not math.isfinite(record.seconds) or record.seconds < 0
        or not isinstance(record.staged_at, str)
    ):
        raise AgreementFailure("invalid")
    return record


def _matching_map(
    map_dir: Path, staged: staging.StageRecord, input: dict[str, str | int],
) -> MapRecord | None:
    if map_dir.is_symlink():
        raise AgreementFailure("invalid")
    if not map_dir.exists():
        return None
    if not map_dir.is_dir():
        raise AgreementFailure("invalid")
    record = _read_map_record(map_dir / AGREEMENT_MAP_RECORD_NAME, staged.courts)
    if (
        record.label != staged.label or record.source != staged.source
        or record.snapshot != staged.snapshot or record.input != input
        or any(
            (map_dir / f"{court}.csv").is_symlink()
            or not (map_dir / f"{court}.csv").is_file()
            for court in staged.courts
        )
    ):
        raise AgreementFailure("invalid")
    return record


def _stage_map(
    snapshot_dir: Path, work_dir: Path, staged: staging.StageRecord,
    input: dict[str, str], opinions: dict[int, tuple[int, int]], progress: _Progress,
    *, clock: Callable[[], datetime] = _clock,
) -> MapRecord:
    """Stage the map once per label, preserving selected CSV record bytes."""

    try:
        recorded_input = staging.input_record(snapshot_dir, AGREEMENT_TABLE, input)
        map_dir = work_dir / AGREEMENT_MAP_DIRECTORY
        partial = work_dir / f"{AGREEMENT_MAP_DIRECTORY}{fetch.PARTIAL_SUFFIX}"
        with staging.lock_partial(work_dir / MAP_LOCK_NAME, waiting=True):
            existing = _matching_map(map_dir, staged, recorded_input)
            if existing is not None:
                return existing
            if partial.is_symlink():
                raise AgreementFailure("invalid")
            if partial.exists():
                shutil.rmtree(partial)
            staging._make_directory(partial)
            started = progress.monotonic()
            counts = dict.fromkeys(staged.courts, 0)
            opinion_counts = dict.fromkeys(staged.courts, 0)
            for index, _ in opinions.values():
                opinion_counts[staged.courts[index]] += 1
            rows = staging.read_rows(snapshot_dir / input["path"], AGREEMENT_TABLE, bz2.open)
            try:
                columns, width, header = staging.read_header(
                    rows, AGREEMENT_TABLE, columns=MAP_COLUMNS,
                )
                paths = [partial / f"{court}.csv" for court in staged.courts]
                with staging._outputs(paths) as outputs:
                    for output in outputs:
                        output.write(header)
                    for fields, lines in staging.checked_rows(
                        rows, width, AGREEMENT_TABLE, progress.row,
                    ):
                        citing = _positive_integer(fields[columns["citing_opinion_id"]], AGREEMENT_TABLE)
                        _positive_integer(fields[columns["cited_opinion_id"]], AGREEMENT_TABLE)
                        location = opinions.get(citing)
                        if location is None:
                            continue
                        index, _ = location
                        outputs[index].write(staging.encoded_lines(lines))
                        counts[staged.courts[index]] += 1
            finally:
                rows.close()
            record = MapRecord(
                staged.label, staged.source, staged.snapshot, staged.courts,
                recorded_input, progress.records.get(AGREEMENT_TABLE, 0), counts,
                opinion_counts, progress.job,
                max(0.0, progress.monotonic() - started),
                clock().astimezone(UTC).isoformat(),
            )
            staging._write_json(partial / AGREEMENT_MAP_RECORD_NAME, asdict(record), fetch.FILE_MODE)
            os.replace(partial, map_dir)
            fetch._flush_directory(work_dir)
            return record
    except staging.StageFailure as exc:
        raise AgreementFailure(
            exc.reason, table=exc.table,
            error=exc.error or (type(exc).__name__ if exc.reason == "malformed" else None),
        ) from exc
    except OSError as exc:
        raise AgreementFailure("local", error=type(exc).__name__) from exc


def _load_cluster_cites(
    work_dir: Path, staged: staging.StageRecord, progress: _Progress,
) -> dict[int, set[str]]:
    """Collect staged reporter cites by cluster across the label's courts."""

    cluster_cites: dict[int, set[str]] = {}
    table = "citations"
    for court in staged.courts:
        court_dir = work_dir / court
        if court_dir.is_symlink() or not court_dir.is_dir():
            raise AgreementFailure("stage-mismatch", table=table)
        try:
            with staging.staged_rows(
                court_dir / f"{table}.csv", table, CITATION_COLUMNS, progress.row,
            ) as (columns, rows):
                for fields, _ in rows:
                    cluster = _positive_integer(fields[columns["cluster_id"]], table)
                    cite = " ".join(fields[columns[name]] for name in ("volume", "reporter", "page"))
                    cluster_cites.setdefault(cluster, set()).add(cite)
        except staging.StageFailure as exc:
            raise _staged_failure(exc, table, "stage-mismatch") from exc
        except OSError as exc:
            raise AgreementFailure("local", table=table, error=type(exc).__name__) from exc
    return cluster_cites


def compare(
    ready: set[int], gideon_edges: dict[int, set[int]],
    unresolved_cites: dict[int, set[str]], cluster_cites: dict[int, set[str]],
    opinions: dict[int, tuple[int, int]], map_rows: Iterable[tuple[int, int]],
) -> Comparison:
    """Compare distinct opinion-to-cluster pairs over ready opinions."""

    gideon_pairs = {
        (opinion, cluster)
        for opinion in ready for cluster in gideon_edges.get(opinion, set())
    }
    mapped_pairs: set[tuple[int, int]] = set()
    rows = 0
    outside = 0
    for citing, cited in map_rows:
        if citing not in ready:
            continue
        rows += 1
        location = opinions.get(cited)
        if location is None:
            outside += 1
            continue
        mapped_pairs.add((citing, location[1]))
    map_only = mapped_pairs - gideon_pairs
    seen = sum(
        bool(unresolved_cites.get(citing, set()) & cluster_cites.get(cluster, set()))
        for citing, cluster in map_only
    )
    return Comparison(
        len(ready), len(gideon_pairs), rows, outside, len(mapped_pairs),
        len(mapped_pairs & gideon_pairs), len(gideon_pairs - mapped_pairs),
        seen, len(map_only) - seen,
    )


def agreement(
    snapshots_root: Path, work_root: Path, label: str, snapshot: str,
    court: str, input: dict[str, str], job: int, record: EdgeRecord,
    *, clock: Callable[[], datetime] = _clock,
    monotonic: Callable[[], float] = time.monotonic,
) -> AgreementRecord:
    """Measure one court's resolved edges against its staged citation map."""

    started = monotonic()
    progress = _Progress(job, label, monotonic, started, started, {})
    try:
        validate_arguments(label, snapshot, court, input)
        try:
            work_dir = staging.work_path(work_root, label, snapshot)
            staged = staging.read_record(work_dir / staging.STAGE_RECORD_NAME)
        except staging.StageFailure as exc:
            raise AgreementFailure(
                "local" if exc.reason == "local" else "stage-mismatch",
                error=exc.error,
            ) from exc
        if staged is None or not work_dir.is_dir():
            raise AgreementFailure("missing-stage")
        if staged.label != label or staged.snapshot != snapshot or court not in staged.courts:
            raise AgreementFailure("stage-mismatch")
        lock_path = work_dir / f"{court}{AGREEMENT_LOCK_SUFFIX}"
        if lock_path.is_symlink():
            raise AgreementFailure("stage-mismatch")
        # A held court lock surfaces as the stage's busy, mapped below.
        with staging.lock_partial(lock_path):
            failure_path = work_dir / f"{court}.{AGREEMENT_FAILURE_NAME}"
            if failure_path.is_symlink():
                raise AgreementFailure("stage-mismatch")
            failure_path.unlink(missing_ok=True)
            snapshot_dir = staging.snapshot_path(snapshots_root, snapshot)
            # Checked before the opinions' long read; a refusal maps by its reason below.
            staging.input_record(snapshot_dir, AGREEMENT_TABLE, input)
            opinions = _load_opinions(work_dir, staged, progress)
            _stage_map(snapshot_dir, work_dir, staged, input, opinions, progress, clock=clock)
            cluster_cites = _load_cluster_cites(work_dir, staged, progress)
            snapshot_date = date.fromisoformat(snapshot[-10:])
            try:
                gideon_edges = record.ready_edges(staged.source, snapshot_date, court)
                unresolved = record.unresolved_cites(staged.source, snapshot_date, court)
                map_path = work_dir / AGREEMENT_MAP_DIRECTORY / f"{court}.csv"
                try:
                    with staging.staged_rows(
                        map_path, AGREEMENT_TABLE, MAP_COLUMNS, progress.row,
                    ) as (columns, rows):
                        pairs = (
                            (
                                _positive_integer(fields[columns["citing_opinion_id"]], AGREEMENT_TABLE),
                                _positive_integer(fields[columns["cited_opinion_id"]], AGREEMENT_TABLE),
                            )
                            for fields, _ in rows
                        )
                        counts = compare(
                            set(gideon_edges), gideon_edges, unresolved,
                            cluster_cites, opinions, pairs,
                        )
                except staging.StageFailure as exc:
                    raise _staged_failure(exc, AGREEMENT_TABLE, "invalid") from exc
                figure = AgreementRecord(
                    label, snapshot, court, job, counts.documents,
                    counts.gideon_pairs, counts.map_rows, counts.map_outside,
                    counts.map_pairs, counts.agreed, counts.gideon_only,
                    counts.map_only_seen, counts.map_only_missed,
                    progress.seconds(), clock().astimezone(UTC).isoformat(),
                )
                path = work_dir / f"{court}.{AGREEMENT_RECORD_NAME}"
                if path.is_symlink():
                    raise AgreementFailure("invalid")
                staging._write_json(path, asdict(figure), fetch.FILE_MODE)
            except BaseException:
                with suppress(Exception):
                    record.close()
                raise
            else:
                record.close()
        logger.info(
            "action=agreement_end job_id=%d label=%s court=%s documents=%d "
            "gideon_pairs=%d map_rows=%d map_outside=%d map_pairs=%d agreed=%d "
            "gideon_only=%d map_only_seen=%d map_only_missed=%d seconds=%.3f",
            job, label, court, figure.documents, figure.gideon_pairs,
            figure.map_rows, figure.map_outside, figure.map_pairs, figure.agreed,
            figure.gideon_only, figure.map_only_seen, figure.map_only_missed,
            figure.seconds,
        )
        return figure
    except AgreementFailure as exc:
        _log_failed(progress, exc)
        raise
    except staging.StageFailure as exc:
        failure = AgreementFailure(exc.reason, table=exc.table, error=exc.error)
        _log_failed(progress, failure)
        raise failure from exc
    except OSError as exc:
        failure = AgreementFailure("local", error=type(exc).__name__)
        _log_failed(progress, failure)
        raise failure from exc


def _log_failed(progress: _Progress, failure: AgreementFailure) -> None:
    # The arguments may be the refused ones, so the line names none of them.
    logger.info(
        "action=agreement_failed job_id=%d reason=%s table=%s error=%s seconds=%.3f",
        progress.job, failure.reason, failure.table or "-", failure.error or "-",
        progress.seconds(),
    )


def write_job_failure(
    work_root: Path, label: str, snapshot: str, court: str, job: int,
    failure: AgreementFailure, *, clock: Callable[[], datetime] = _clock,
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
        path = whole / f"{court}.{AGREEMENT_FAILURE_NAME}"
        if path.is_symlink():
            return
        staging.write_failure(path, asdict(AgreementFailureRecord(
            job, court, failure.reason, failure.table, failure.error,
            clock().astimezone(UTC).isoformat(),
        )))
    except (staging.StageFailure, OSError):
        return
