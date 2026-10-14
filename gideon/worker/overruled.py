"""Parse and record listed overrulings against held Supreme Court clusters."""

import hashlib
import logging
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Set
from contextlib import suppress
from dataclasses import asdict, astuple, dataclass, replace
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Final, Protocol

from . import fetch, settings, staging

OVERRULED_TASK: Final = "gideon.worker.tasks.overruled"
OVERRULED_QUEUE: Final = "caselaw"
OVERRULED_SOURCE: Final = "treatment"
OVERRULED_COURT: Final = "scotus"
OVERRULED_FAILURE_NAME: Final = "overruled-failed.json"
OVERRULED_LOCK_NAME: Final = "overruled.lock"
OVERRULED_FAILURE_REASONS: Final = frozenset({
    "invalid", "missing-input", "input-mismatch", "missing-stage",
    "stage-mismatch", "malformed", "database", "local", "busy",
})
OVERRULED_RESOLUTIONS: Final = ("cite", "name", "docket", "ambiguous", "absent")
OVERRULED_WORK_DIRECTORY: Final = "treatment"
QUALIFIERS: Final = ("in_part", "on_other_grounds", "none")
TABLE_ID: Final = "appendix2Table"
COLUMN_COUNT: Final = 5
INPUT_NAME: Final = "decisions-overruled.html"
CLUSTER_COLUMNS: Final = (
    "id", "case_name", "case_name_short", "case_name_full", "date_filed",
)
CITE_PATTERN: Final = re.compile(
    r"\b(\d{1,3}) U\.S\. (?:\(\d+ [A-Za-z. ]+\) )?(\d{1,4})\b(?:, \d+)?",
)
DOCKET_PATTERN: Final = re.compile(r"\bNo\. (\d{1,2}-\d{1,5})\b")
YEAR_PATTERN: Final = re.compile(r"\((\d{4})\)")
DATED_YEAR_PATTERN: Final = re.compile(r"\(U\.S\. [A-Za-z.]+ \d{1,2}, (\d{4})\)")
YEAR_COLUMN_PATTERN: Final = re.compile(r"\b\d{4}\b")
IN_PART_MARKER: Final = "(in part)"
# exempt: the progress interval bounds content-free worker log traffic.
PROGRESS_INTERVAL_SECONDS: Final = 60
SCOTUS_CLUSTERS_SQL: Final = (
    "SELECT o.cluster_id, o.reporter_cites, o.docket, "
    "EXTRACT(YEAR FROM o.decided_date)::integer "
    "FROM opinions AS o JOIN documents AS d ON d.doc_id = o.doc_id "
    "WHERE d.source = %s AND d.source_snapshot = %s AND o.court = %s "
    "ORDER BY o.cluster_id, o.opinion_id"
)
INSERT_ROW_SQL: Final = (
    "INSERT INTO known_overruled (row_id, source, source_snapshot, caselaw_source, "
    "caselaw_snapshot, list_order, ordinal, overruling_name, overruling_cite, "
    "overruling_docket, overruling_year, overruling_cluster, overruling_resolution, "
    "overruled_name, overruled_cite, overruled_year, overruled_cluster, "
    "overruled_resolution, qualifier, found_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (row_id) DO NOTHING"
)

logger = logging.getLogger(__name__)


def _clock() -> datetime:
    return datetime.now(UTC)


class OverruledFailure(Exception):
    """A content-free refusal with a reason from the closed vocabulary."""

    def __init__(
        self, reason: str, *, table: str | None = None, error: str | None = None,
    ) -> None:
        if reason not in OVERRULED_FAILURE_REASONS:
            raise ValueError("unknown overruled failure reason")
        self.reason = reason
        self.table = table
        self.error = error
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class ListedDecision:
    """A public caption and the identifiers printed beside it."""

    name: str
    cite: str | None
    docket: str | None
    year: int | None
    qualifier: str


@dataclass(frozen=True, slots=True)
class ListedPair:
    """One overruled decision at its position in the source table."""

    order: int
    ordinal: int
    overruling: ListedDecision
    overruled: ListedDecision


@dataclass(frozen=True, slots=True)
class ClusterFacts:
    """A recorded cluster's reporter cites, docket, and decision year."""

    cluster_id: int
    cites: frozenset[str]
    docket: str | None
    year: int | None


@dataclass(frozen=True, slots=True)
class OverruledRow:
    """A listed pair's immutable resolution under two source snapshots."""

    row_id: str
    source: str
    source_snapshot: date
    caselaw_source: str
    caselaw_snapshot: date
    list_order: int
    ordinal: int
    overruling_name: str
    overruling_cite: str | None
    overruling_docket: str | None
    overruling_year: int
    overruling_cluster: int | None
    overruling_resolution: str
    overruled_name: str
    overruled_cite: str | None
    overruled_year: int | None
    overruled_cluster: int | None
    overruled_resolution: str
    qualifier: str


@dataclass(frozen=True, slots=True)
class OverruledCounts:
    """The whole pass's resolution counts and insertion outcome."""

    list_rows: int
    rows: int
    overruled: dict[str, int]
    overruling: dict[str, int]
    inserted: int
    present: int
    seconds: float


@dataclass(frozen=True, slots=True)
class OverruledFailureRecord:
    """A content-free failed-job outcome in the treatment work directory."""

    job: int
    court: str
    reason: str
    table: str | None
    error: str | None
    at: str
    schema: int = 1


class OverruledRecord(Protocol):
    """The recorded clusters and append-only listed pairs used by the job."""

    def scotus_clusters(self, source: str, snapshot_date: date, court: str) -> tuple[ClusterFacts, ...]: ...
    def write_rows(self, rows: tuple[OverruledRow, ...], found_at: datetime) -> tuple[int, int]: ...
    def close(self) -> None: ...


class PsycopgRecord:
    """Read cluster facts and insert every listed pair in one transaction."""

    def __init__(
        self, *, connect: Callable[..., Any] | None = None,
        worker_settings: settings.Settings | None = None,
    ) -> None:
        import psycopg

        self._connect: Callable[..., Any] = connect if connect is not None else psycopg.connect
        self._settings = worker_settings
        self._connection: Any | None = None

    def _get_connection(self) -> Any:
        import psycopg

        if self._connection is None:
            try:
                configuration = self._settings or settings.load_settings()
                self._connection = self._connect(
                    **settings.connection_kwargs(configuration), autocommit=False,
                )
            except psycopg.Error as exc:
                raise OverruledFailure("database", error=type(exc).__name__) from exc
            except settings.WorkerSettingsError as exc:
                raise OverruledFailure("local", error=type(exc).__name__) from exc
        return self._connection

    def scotus_clusters(self, source: str, snapshot_date: date, court: str) -> tuple[ClusterFacts, ...]:
        """Fold each held cluster's opinions into one set of reporter cites."""

        import psycopg

        connection = self._get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(SCOTUS_CLUSTERS_SQL, (source, snapshot_date, court))
                rows = cursor.fetchall()
            connection.commit()
        except psycopg.Error as exc:
            with suppress(psycopg.Error):
                connection.rollback()
            raise OverruledFailure("database", error=type(exc).__name__) from exc
        clusters: dict[int, ClusterFacts] = {}
        for cluster_id, cites, docket, year in rows:
            cluster_id = int(cluster_id)
            facts = ClusterFacts(
                cluster_id, frozenset(cites), docket, int(year) if year is not None else None,
            )
            previous = clusters.get(cluster_id)
            if previous is not None:
                facts = replace(
                    facts, cites=previous.cites | facts.cites,
                    docket=previous.docket or facts.docket,
                    year=previous.year if previous.year is not None else facts.year,
                )
            clusters[cluster_id] = facts
        return tuple(clusters.values())

    def write_rows(self, rows: tuple[OverruledRow, ...], found_at: datetime) -> tuple[int, int]:
        """Commit the whole pass, counting conflicts as already present."""

        import psycopg

        connection = self._get_connection()
        inserted = 0
        try:
            with connection.cursor() as cursor:
                for row in rows:
                    cursor.execute(INSERT_ROW_SQL, (*astuple(row), found_at))
                    inserted += cursor.rowcount
            connection.commit()
        except psycopg.Error as exc:
            with suppress(psycopg.Error):
                connection.rollback()
            raise OverruledFailure("database", error=type(exc).__name__) from exc
        return inserted, len(rows) - inserted

    def close(self) -> None:
        """Close the record connection if it was opened."""

        import psycopg

        if self._connection is not None:
            try:
                self._connection.close()
            except psycopg.Error as exc:
                with suppress(psycopg.Error):
                    self._connection.rollback()
                raise OverruledFailure("database", error=type(exc).__name__) from exc
            finally:
                self._connection = None


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.found = False
        self._in_table = False
        self._in_body = False
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            if self._in_table or (dict(attrs).get("id") == TABLE_ID and self.found):
                raise OverruledFailure("malformed", table=INPUT_NAME)
            if dict(attrs).get("id") == TABLE_ID:
                self.found = self._in_table = True
        if not self._in_table:
            return
        if tag == "tbody":
            self._in_body = True
        elif tag == "tr" and self._in_body:
            if self._row is not None:
                raise OverruledFailure("malformed", table=INPUT_NAME)
            self._row = []
        elif tag == "td" and self._in_body:
            if self._row is None or self._cell is not None:
                raise OverruledFailure("malformed", table=INPUT_NAME)
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append("\n")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_table:
            return
        if tag == "td" and self._in_body:
            if self._cell is None or self._row is None:
                raise OverruledFailure("malformed", table=INPUT_NAME)
            text = unicodedata.normalize("NFC", "".join(self._cell))
            self._row.append("\n".join(
                " ".join(line.split()) for line in text.splitlines() if line.strip()
            ))
            self._cell = None
        elif tag == "tr" and self._in_body:
            if self._row is None or self._cell is not None or len(self._row) != COLUMN_COUNT:
                raise OverruledFailure("malformed", table=INPUT_NAME)
            self.rows.append(self._row)
            self._row = None
        elif tag in {"tbody", "table"}:
            if self._row is not None or self._cell is not None:
                raise OverruledFailure("malformed", table=INPUT_NAME)
            self._in_body = False
            if tag == "table":
                self._in_table = False


def _decision(name: str, identifiers: str) -> ListedDecision:
    cite = CITE_PATTERN.search(identifiers)
    docket = DOCKET_PATTERN.search(identifiers)
    year = YEAR_PATTERN.search(identifiers) or DATED_YEAR_PATTERN.search(identifiers)
    name = " ".join(name.split()).rstrip(",; ")
    if not name:
        raise OverruledFailure("malformed", table=INPUT_NAME)
    return ListedDecision(
        name,
        f"{cite[1]} U.S. {cite[2]}" if cite else None,
        docket[1] if docket else None,
        int(year[1]) if year else None,
        "in_part" if IN_PART_MARKER in identifiers else "none",
    )


def _overruled_decisions(text: str, years: str) -> tuple[ListedDecision, ...]:
    decisions: list[ListedDecision] = []
    name: list[str] = []
    for line in text.splitlines():
        cite = CITE_PATTERN.search(line)
        if cite is not None:
            name.append(line[:cite.start()])
            decisions.append(_decision(" ".join(name), line[cite.start():]))
            name = []
        elif line.endswith(","):
            if name:
                decisions.append(_decision(" ".join(name), ""))
            name = [line]
        else:
            name.append(line)
    if name:
        decisions.append(_decision(" ".join(name), ""))
    if not decisions:
        raise OverruledFailure("malformed", table=INPUT_NAME)
    column_years = tuple(int(year) for year in YEAR_COLUMN_PATTERN.findall(years))
    return tuple(
        replace(decision, year=(
            column_years[ordinal] if len(column_years) == len(decisions)
            else decision.year if decision.year is not None
            else column_years[0] if len(column_years) == 1 else None
        ))
        for ordinal, decision in enumerate(decisions)
    )


def parse_table(text: str) -> tuple[ListedPair, ...]:
    """Read every listed pair from the captured table's five-column body."""

    parser = _TableParser()
    parser.feed(text)
    parser.close()
    if not parser.found or parser._in_table or not parser.rows:
        raise OverruledFailure("malformed", table=INPUT_NAME)
    pairs: list[ListedPair] = []
    for order_text, ruling_text, ruling_year, ruled_text, ruled_years in parser.rows:
        if not order_text.isdecimal() or int(order_text) <= 0 or not re.fullmatch(r"\d{4}", ruling_year):
            raise OverruledFailure("malformed", table=INPUT_NAME)
        identifiers = tuple(
            match for pattern in (CITE_PATTERN, DOCKET_PATTERN)
            if (match := pattern.search(ruling_text)) is not None
        )
        identifier = min(identifiers, key=lambda match: match.start(), default=None)
        ruling = _decision(ruling_text[:identifier.start()] if identifier else ruling_text, ruling_text)
        ruling = replace(ruling, year=int(ruling_year))
        pairs.extend(
            ListedPair(int(order_text), ordinal, ruling, ruled)
            for ordinal, ruled in enumerate(_overruled_decisions(ruled_text, ruled_years))
        )
    return tuple(pairs)


def normal_name(text: str) -> str:
    """Apply the same exact name key to table captions and staged names."""

    text = unicodedata.normalize("NFC", text).translate(
        str.maketrans("‘’“”", "''\"\""),
    ).replace("&", " and ").casefold()
    return " ".join("".join(
        char for char in text if char.isalpha() or char.isdigit() or char.isspace()
    ).split())


def _normal_docket(text: str) -> str:
    return "".join(text.split()).removeprefix("No.")


def _resolution(candidates: Set[int], method: str) -> tuple[int | None, str]:
    if len(candidates) == 1:
        return next(iter(candidates)), method
    return None, "ambiguous" if candidates else "absent"


def resolve(
    pairs: Iterable[ListedPair],
    clusters: Iterable[ClusterFacts],
    names: Mapping[tuple[str, int], Set[int]],
    *,
    source: str,
    source_snapshot: date,
    caselaw_source: str,
    caselaw_snapshot: date,
) -> tuple[OverruledRow, ...]:
    """Resolve exact identifiers, retaining every absent or ambiguous pair."""

    cites: dict[str, set[int]] = {}
    dockets: dict[str, set[int]] = {}
    held: set[int] = set()
    for cluster in clusters:
        held.add(cluster.cluster_id)
        for cite in cluster.cites:
            cites.setdefault(cite, set()).add(cluster.cluster_id)
        if cluster.docket:
            dockets.setdefault(_normal_docket(cluster.docket), set()).add(cluster.cluster_id)
    rows: list[OverruledRow] = []
    for pair in pairs:
        ruling, ruled = pair.overruling, pair.overruled
        if ruling.year is None:
            raise OverruledFailure("malformed", table=INPUT_NAME)
        ruling_candidates = cites.get(ruling.cite, set()) if ruling.cite else set()
        ruling_method = "cite"
        if not ruling_candidates:
            ruling_candidates = dockets.get(_normal_docket(ruling.docket), set()) if ruling.docket else set()
            ruling_method = "docket"
        ruling_cluster, ruling_resolution = _resolution(ruling_candidates, ruling_method)
        ruled_candidates = cites.get(ruled.cite, set()) if ruled.cite else set()
        ruled_method = "cite"
        if not ruled_candidates and ruled.year is not None:
            ruled_candidates = held.intersection(names.get((normal_name(ruled.name), ruled.year), set()))
            ruled_method = "name"
        ruled_cluster, ruled_resolution = _resolution(ruled_candidates, ruled_method)
        row_id = hashlib.sha256("\n".join((
            source, source_snapshot.isoformat(), caselaw_source, caselaw_snapshot.isoformat(),
            str(pair.order), str(pair.ordinal),
        )).encode()).hexdigest()
        rows.append(OverruledRow(
            row_id, source, source_snapshot, caselaw_source, caselaw_snapshot,
            pair.order, pair.ordinal,
            ruling.name, ruling.cite, ruling.docket, ruling.year, ruling_cluster, ruling_resolution,
            ruled.name, ruled.cite, ruled.year, ruled_cluster, ruled_resolution, ruled.qualifier,
        ))
    return tuple(rows)


@dataclass
class _Progress:
    job: int
    label: str
    monotonic: Callable[[], float]
    started: float
    last_progress: float
    records: int = 0

    def seconds(self) -> float:
        return max(0.0, self.monotonic() - self.started)

    def row(self, table: str) -> None:
        self.records += 1
        now = self.monotonic()
        if now - self.last_progress >= PROGRESS_INTERVAL_SECONDS:
            logger.info(
                "action=overruled_progress job_id=%d label=%s table=%s records=%d seconds=%.3f",
                self.job, self.label, table, self.records, self.seconds(),
            )
            self.last_progress = now


def validate_arguments(
    label: object, snapshot: object, caselaw_snapshot: object,
    court: object, input: object, job: object,
) -> None:
    """Refuse queue arguments outside the snapshot and input grammars."""

    if (
        not staging._dated_name(label, staging.LABEL_PATTERN)
        or not staging._dated_name(snapshot, staging.SNAPSHOT_PATTERN)
        or not isinstance(snapshot, str)
        or snapshot[:-staging.DATE_SUFFIX_LENGTH] != OVERRULED_SOURCE
        or not staging._dated_name(caselaw_snapshot, staging.SNAPSHOT_PATTERN)
        or court != OVERRULED_COURT
        or not staging.valid_input(input)
        or type(job) is not int or job < 1
    ):
        raise OverruledFailure("invalid")


def _load_names(work_dir: Path, court: str, progress: _Progress) -> dict[tuple[str, int], set[int]]:
    table = "opinion-clusters"
    court_dir = work_dir / court
    if court_dir.is_symlink() or not court_dir.is_dir():
        raise OverruledFailure("stage-mismatch", table=table)
    names: dict[tuple[str, int], set[int]] = {}
    try:
        with staging.staged_rows(
            court_dir / f"{table}.csv", table, CLUSTER_COLUMNS, progress.row,
        ) as (columns, rows):
            for fields, _ in rows:
                raw_id = fields[columns["id"]]
                if re.fullmatch(r"[0-9]+", raw_id) is None:
                    raise OverruledFailure("malformed", table=table, error="ValueError")
                cluster_id = int(raw_id)
                if cluster_id < 1:
                    raise OverruledFailure("malformed", table=table, error="ValueError")
                year = date.fromisoformat(fields[columns["date_filed"]]).year
                for column in CLUSTER_COLUMNS[1:4]:
                    name = fields[columns[column]]
                    if name.strip():
                        names.setdefault((normal_name(name), year), set()).add(cluster_id)
    except staging.StageFailure as exc:
        raise OverruledFailure(
            "stage-mismatch" if exc.reason == "invalid" else "malformed",
            table=table, error=exc.error,
        ) from exc
    except ValueError as exc:
        raise OverruledFailure("malformed", table=table, error=type(exc).__name__) from exc
    except OSError as exc:
        raise OverruledFailure("local", table=table, error=type(exc).__name__) from exc
    return names


def overruled(
    snapshots_root: Path, work_root: Path, label: str, snapshot: str,
    caselaw_snapshot: str, court: str, input: dict[str, str], job: int,
    record: OverruledRecord, *, clock: Callable[[], datetime] = _clock,
    monotonic: Callable[[], float] = time.monotonic,
) -> OverruledCounts:
    """Resolve and record every pair in one captured overrulings table."""

    started = monotonic()
    progress = _Progress(job if type(job) is int else 0, label, monotonic, started, started)
    try:
        validate_arguments(label, snapshot, caselaw_snapshot, court, input, job)
        snapshot_dir = staging.snapshot_path(snapshots_root, snapshot)
        work_dir = staging.work_path(work_root, label, snapshot)
        if not work_dir.parent.is_dir():
            staging._make_directory(work_dir.parent)
        if not work_dir.is_dir():
            staging._make_directory(work_dir)
        try:
            stage_dir = staging.work_path(work_root, label, caselaw_snapshot)
            staged = staging.read_record(stage_dir / staging.STAGE_RECORD_NAME)
        except staging.StageFailure as exc:
            raise OverruledFailure(
                "local" if exc.reason == "local" else "stage-mismatch",
                error=exc.error,
            ) from exc
        if staged is None or not stage_dir.is_dir():
            raise OverruledFailure("missing-stage")
        if staged.label != label or staged.snapshot != caselaw_snapshot or court not in staged.courts:
            raise OverruledFailure("stage-mismatch")
        lock_path = work_dir / OVERRULED_LOCK_NAME
        if lock_path.is_symlink():
            raise OverruledFailure("invalid")
        with staging.lock_partial(lock_path):
            failure_path = work_dir / OVERRULED_FAILURE_NAME
            if failure_path.is_symlink():
                raise OverruledFailure("invalid")
            failure_path.unlink(missing_ok=True)
            staging.input_record(
                snapshot_dir, INPUT_NAME, input, forms=(fetch.KEPT_FORM, fetch.SEEDED_FORM),
            )
            try:
                pairs = parse_table((snapshot_dir / input["path"]).read_text(encoding="utf-8"))
            except UnicodeError as exc:
                raise OverruledFailure("malformed", table=INPUT_NAME, error=type(exc).__name__) from exc
            names = _load_names(stage_dir, court, progress)
            snapshot_date = date.fromisoformat(snapshot[-10:])
            caselaw_date = date.fromisoformat(caselaw_snapshot[-10:])
            try:
                clusters = record.scotus_clusters(staged.source, caselaw_date, court)
                rows = resolve(
                    pairs, clusters, names, source=OVERRULED_SOURCE,
                    source_snapshot=snapshot_date, caselaw_source=staged.source,
                    caselaw_snapshot=caselaw_date,
                )
                inserted, present = record.write_rows(rows, clock().astimezone(UTC))
            except BaseException:
                with suppress(Exception):
                    record.close()
                raise
            else:
                record.close()
        ruled_counts = dict.fromkeys(("cite", "name", "ambiguous", "absent"), 0)
        ruling_counts = dict.fromkeys(("cite", "docket", "ambiguous", "absent"), 0)
        for row in rows:
            ruled_counts[row.overruled_resolution] += 1
            ruling_counts[row.overruling_resolution] += 1
        counts = OverruledCounts(
            len({pair.order for pair in pairs}), len(rows), ruled_counts, ruling_counts,
            inserted, present, progress.seconds(),
        )
        logger.info(
            "action=overruled_end job_id=%d label=%s court=%s snapshot=%s caselaw_snapshot=%s "
            "list_rows=%d rows=%d overruled_cite=%d overruled_name=%d overruled_ambiguous=%d "
            "overruled_absent=%d overruling_cite=%d overruling_docket=%d overruling_ambiguous=%d "
            "overruling_absent=%d inserted=%d present=%d seconds=%.3f",
            job, label, court, snapshot, caselaw_snapshot, counts.list_rows, counts.rows,
            ruled_counts["cite"], ruled_counts["name"], ruled_counts["ambiguous"], ruled_counts["absent"],
            ruling_counts["cite"], ruling_counts["docket"], ruling_counts["ambiguous"], ruling_counts["absent"],
            inserted, present, counts.seconds,
        )
        return counts
    except OverruledFailure as exc:
        _log_failed(progress, exc)
        raise
    except staging.StageFailure as exc:
        failure = OverruledFailure(exc.reason, table=exc.table, error=exc.error)
        _log_failed(progress, failure)
        raise failure from exc
    except OSError as exc:
        failure = OverruledFailure("local", error=type(exc).__name__)
        _log_failed(progress, failure)
        raise failure from exc


def _log_failed(progress: _Progress, failure: OverruledFailure) -> None:
    # Refused arguments may contain text, so the failure line names none of them.
    logger.info(
        "action=overruled_failed job_id=%d reason=%s table=%s error=%s seconds=%.3f",
        progress.job, failure.reason, failure.table or "-", failure.error or "-", progress.seconds(),
    )


def write_job_failure(
    work_root: Path, label: str, snapshot: str, court: str, job: int,
    failure: OverruledFailure, *, clock: Callable[[], datetime] = _clock,
) -> None:
    """Atomically file a safe failure inside an existing treatment directory."""

    if failure.reason == "busy":
        return
    if not isinstance(court, str) or re.fullmatch(staging.COURT_PATTERN, court) is None:
        return
    if not staging._dated_name(snapshot, staging.SNAPSHOT_PATTERN):
        return
    if snapshot[:-staging.DATE_SUFFIX_LENGTH] != OVERRULED_SOURCE:
        return
    try:
        whole = staging.work_path(work_root, label, snapshot)
        if not whole.is_dir():
            return
        path = whole / OVERRULED_FAILURE_NAME
        if path.is_symlink():
            return
        staging.write_failure(path, asdict(OverruledFailureRecord(
            job, court, failure.reason, failure.table, failure.error,
            clock().astimezone(UTC).isoformat(),
        )))
    except (staging.StageFailure, OSError):
        return
