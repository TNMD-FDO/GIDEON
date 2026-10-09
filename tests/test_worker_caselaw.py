"""Court opinion ingest over fictitious staged files and a checked record."""

from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import json
import logging
import stat
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from dataclasses import astuple, dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import psycopg

from gideon.casecite.found import FoundCitation
from gideon.host import cas
from gideon.host.sysio import RealHost
from gideon.worker import (
    anchors,
    caselaw,
    citations,
    fetch,
    logs,
    opiniontext,
    sections,
    settings,
    staging,
    treatment,
)

DOCKET_COLUMNS = (
    "id", "date_created", "date_modified", "source", "appeal_from_str",
    "assigned_to_str", "referred_to_str", "panel_str", "date_last_index",
    "date_cert_granted", "date_cert_denied", "date_argued", "date_reargued",
    "date_reargument_denied", "date_filed", "date_terminated",
    "date_last_filing", "case_name_short", "case_name", "case_name_full", "slug",
    "docket_number", "docket_number_core", "pacer_case_id", "cause",
    "nature_of_suit", "jury_demand", "jurisdiction_type",
    "appellate_fee_status", "appellate_case_type_information", "mdl_status",
    "filepath_local", "filepath_ia", "filepath_ia_json", "ia_upload_failure_count",
    "ia_needs_upload", "ia_date_first_change", "view_count", "date_blocked",
    "blocked", "appeal_from_id", "assigned_to_id", "court_id", "idb_data_id",
    "originating_court_information_id", "referred_to_id", "federal_dn_case_type",
    "federal_dn_office_code", "federal_dn_judge_initials_assigned",
    "federal_dn_judge_initials_referred", "federal_defendant_number",
    "parent_docket_id", "docket_number_raw", "docket_number_source",
)
CLUSTER_COLUMNS = (
    "id", "date_created", "date_modified", "judges", "date_filed",
    "date_filed_is_approximate", "slug", "case_name_short", "case_name",
    "case_name_full", "scdb_id", "scdb_decision_direction", "scdb_votes_majority",
    "scdb_votes_minority", "source", "procedural_history", "attorneys",
    "nature_of_suit", "posture", "syllabus", "headnotes", "summary", "disposition",
    "history", "other_dates", "cross_reference", "correction", "citation_count",
    "precedential_status", "date_blocked", "blocked", "filepath_json_harvard",
    "filepath_pdf_harvard", "docket_id", "arguments", "headmatter",
)
CITATION_COLUMNS = (
    "id", "volume", "reporter", "page", "type", "cluster_id",
    "date_created", "date_modified",
)
OPINION_COLUMNS = (
    "id", "date_created", "date_modified", "author_str", "per_curiam",
    "joined_by_str", "type", "sha1", "page_count", "download_url", "local_path",
    "plain_text", "html", "html_lawbox", "html_columbia", "html_anon_2020",
    "xml_harvard", "xml_scan", "html_with_citations", "extracted_by_ocr",
    "author_id", "cluster_id",
)


def _opinion(opinion_id: str, cluster_id: str, **text: str) -> dict[str, str]:
    return {**dict.fromkeys(OPINION_COLUMNS, ""), "id": opinion_id,
            "cluster_id": cluster_id, "type": "020", "per_curiam": "f", **text}


def _doc_id(opinion_id: int, snapshot: date) -> str:
    return hashlib.sha256(f"caselaw\n{opinion_id}\n{snapshot.isoformat()}".encode()).hexdigest()


@dataclass
class _Row:
    opinion_id: int
    court: str
    document: caselaw.NewDocument
    opinion: caselaw.NewOpinion | None
    status: str
    attempts: int
    sha256: str | None = None
    canonical_text_sha256: str | None = None
    failure_reason: str | None = None
    ingested_at: datetime | None = None
    anchored_at: datetime | None = None
    sections: tuple[caselaw.NewSection, ...] = field(default_factory=tuple)
    anchors: tuple[caselaw.NewAnchor, ...] = field(default_factory=tuple)
    citations: tuple[citations.NewCitation, ...] = field(default_factory=tuple)
    cited_at: datetime | None = None
    treatment_pattern_set: str | None = None
    treated_at: datetime | None = None
    signals: tuple[treatment.NewSignal, ...] = field(default_factory=tuple)


class _Record:
    """Hold opinion rows while enforcing the document migration's checks."""

    def __init__(self) -> None:
        self.rows: dict[int, _Row] = {}
        self.events: list[tuple[str, str]] = []
        self.present_calls: list[tuple[str, date, str]] = []
        self.closed = 0
        self.fail_present: caselaw.CaselawFailure | None = None

    def _row(self, doc_id: str) -> _Row:
        return next(row for row in self.rows.values() if row.document.doc_id == doc_id)

    def _check(self, row: _Row) -> None:
        assert 0 <= row.attempts <= caselaw.MAX_ATTEMPTS
        assert row.status in {"processing", "ready", "failed", "withdrawn"}
        assert (row.status == "processing") == (row.ingested_at is None)
        assert (row.status == "failed") == (row.failure_reason is not None)
        assert row.status == "ready" or row.canonical_text_sha256 is None
        assert row.status != "ready" or (
            row.sha256 is not None and row.canonical_text_sha256 is not None
        )
        assert row.status != "processing" or row.sha256 is None
        if row.failure_reason is not None:
            assert row.failure_reason in caselaw.DOCUMENT_FAILURE_REASONS
        if row.opinion is not None:
            assert row.opinion.doc_id == row.document.doc_id
        assert row.status == "ready" or not row.sections
        assert row.status == "ready" or not row.anchors
        assert row.anchored_at is None or row.status == "ready"
        assert row.cited_at is None or row.status == "ready"
        assert (row.treatment_pattern_set is None) == (row.treated_at is None)
        assert row.treated_at is None or row.cited_at is not None
        assert all(section.doc_id == row.document.doc_id for section in row.sections)
        assert all(anchor.doc_id == row.document.doc_id for anchor in row.anchors)
        assert all(citation.doc_id == row.document.doc_id for citation in row.citations)
        assert all(signal.doc_id == row.document.doc_id for signal in row.signals)

    def seed(
        self, opinion_id: int, status: str, attempts: int, *,
        court: str, source: str, snapshot: date, now: datetime,
        sectioned: bool | None = None, anchored: bool | None = None,
        cited: bool | None = None,
    ) -> _Row:
        document = caselaw.NewDocument(
            _doc_id(opinion_id, snapshot), "caselaw", source, snapshot, "plain_text", now,
        )
        row = _Row(
            opinion_id, court, document, None, status, attempts,
            sha256="a" * 64 if status == "ready" else None,
            canonical_text_sha256="b" * 64 if status == "ready" else None,
            failure_reason="no-text" if status == "failed" else None,
            ingested_at=None if status == "processing" else now,
            anchored_at=now if (status == "ready" and anchored is not False) else None,
        )
        if sectioned is None:
            sectioned = status == "ready"
        if cited is None:
            cited = status == "ready" and sectioned
        if cited:
            row.cited_at = now
            row.treatment_pattern_set = treatment.PATTERN_SET_ID
            row.treated_at = now
        if sectioned:
            row.sections = (caselaw.NewSection(
                "c" * 64, document.doc_id, 0, "unknown", "none", 0, 1,
                None, None, None,
            ),)
        self._check(row)
        self.rows[opinion_id] = row
        return row

    def present(self, source: str, snapshot_date: date, court: str) -> dict[int, caselaw.PresentRow]:
        self.present_calls.append((source, snapshot_date, court))
        if self.fail_present is not None:
            raise self.fail_present
        return {
            opinion_id: caselaw.PresentRow(
                row.document.doc_id, row.status, row.attempts, row.document.text_source,
                row.sha256, row.canonical_text_sha256, bool(row.sections),
                row.anchored_at is not None, row.cited_at is not None,
                row.treatment_pattern_set,
            )
            for opinion_id, row in self.rows.items()
            if row.document.source == source
            and row.document.source_snapshot == snapshot_date and row.court == court
        }

    def begin(self, document: caselaw.NewDocument, opinion: caselaw.NewOpinion) -> None:
        assert opinion.opinion_id not in self.rows
        row = _Row(opinion.opinion_id, opinion.court, document, opinion, "processing", 1)
        self._check(row)
        self.rows[opinion.opinion_id] = row
        self.events.append(("begin", document.doc_id))

    def retry(self, doc_id: str) -> None:
        row = self._row(doc_id)
        assert row.status == "processing" and row.attempts < caselaw.MAX_ATTEMPTS
        row.attempts += 1
        self._check(row)
        self.events.append(("retry", doc_id))

    def give_back(self, doc_id: str) -> None:
        row = self._row(doc_id)
        assert row.status == "processing" and row.attempts > 0
        row.attempts -= 1
        self._check(row)
        self.events.append(("give_back", doc_id))

    def finish_ready(
        self, doc_id: str, sha256: str, canonical_sha256: str, at: datetime,
        sections: tuple[caselaw.NewSection, ...], anchor_rows: tuple[caselaw.NewAnchor, ...],
        citation_rows: tuple[citations.NewCitation, ...],
        signal_rows: tuple[treatment.NewSignal, ...],
    ) -> None:
        row = self._row(doc_id)
        assert row.status == "processing"
        row.status = "ready"
        row.sha256 = sha256
        row.canonical_text_sha256 = canonical_sha256
        row.ingested_at = at
        row.anchored_at = at
        row.cited_at = at
        row.sections = sections
        row.anchors = anchor_rows
        row.citations = citation_rows
        row.signals = signal_rows
        row.treatment_pattern_set = treatment.PATTERN_SET_ID
        row.treated_at = at
        self._check(row)
        self.events.append(("ready", doc_id))

    def write_sections(
        self, doc_id: str, sections: tuple[caselaw.NewSection, ...],
    ) -> None:
        row = self._row(doc_id)
        assert row.status == "ready" and not row.sections
        row.sections = sections
        self._check(row)
        self.events.append(("sectioned", doc_id))

    def write_anchors(
        self, doc_id: str, anchor_rows: tuple[caselaw.NewAnchor, ...], at: datetime,
    ) -> None:
        row = self._row(doc_id)
        assert row.status == "ready" and row.anchored_at is None and not row.anchors
        row.anchors = anchor_rows
        row.anchored_at = at
        self._check(row)
        self.events.append(("anchored", doc_id))

    def read_sections(self, doc_id: str) -> tuple[caselaw.NewSection, ...]:
        return self._row(doc_id).sections

    def write_citations(
        self, doc_id: str, citation_rows: tuple[citations.NewCitation, ...], at: datetime,
    ) -> None:
        row = self._row(doc_id)
        assert row.status == "ready" and row.sections and row.cited_at is None
        row.citations = citation_rows
        row.cited_at = at
        self._check(row)
        self.events.append(("cited", doc_id))

    def read_citations(self, doc_id: str) -> tuple[citations.NewCitation, ...]:
        return self._row(doc_id).citations

    def write_signals(
        self, doc_id: str, signal_rows: tuple[treatment.NewSignal, ...], at: datetime,
    ) -> None:
        row = self._row(doc_id)
        assert row.status == "ready" and row.cited_at is not None
        assert row.treatment_pattern_set != treatment.PATTERN_SET_ID
        row.signals += signal_rows
        row.treatment_pattern_set = treatment.PATTERN_SET_ID
        row.treated_at = at
        self._check(row)
        self.events.append(("treated", doc_id))

    def finish_failed(
        self, doc_id: str, reason: str, at: datetime, sha256: str | None = None,
    ) -> None:
        row = self._row(doc_id)
        assert row.status == "processing"
        row.status = "failed"
        row.failure_reason = reason
        row.sha256 = sha256
        row.ingested_at = at
        self._check(row)
        self.events.append(("failed", doc_id))

    def close(self) -> None:
        self.closed += 1


class WorkerCaselaw(unittest.TestCase):
    """A staged court produces durable objects or named document and job failures."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.snapshots_root = root / "snapshots"
        self.work_root = root / "work"
        self.store_root = root / "store"
        self.snapshots_root.mkdir()
        self.work_root.mkdir()
        self.store_root.mkdir(mode=cas.DIRECTORY_MODE)
        self.store_root.chmod(cas.DIRECTORY_MODE)
        self.label = "corpus-2099-01-01"
        self.snapshot = "fictional-dump-2099-01-02"
        self.snapshot_date = date(2099, 1, 2)
        self.source = "fictional-dump"
        self.court = "court1"
        self.staged_courts = [self.court]
        self.job = 41
        self.now = datetime(2099, 1, 2, 12, 34, tzinfo=UTC)
        self.whole = self.work_root / self.label / self.source
        self.court_dir = self.whole / self.court
        self.failure_path = self.whole / f"{self.court}.{caselaw.CASELAW_FAILURE_NAME}"
        self.lock_path = self.whole / f"{self.court}{caselaw.LOCK_SUFFIX}"
        self.record = _Record()

    def _csv(
        self, table: str, columns: tuple[str, ...], rows: list[dict[str, str]],
        *, court_dir: Path | None = None,
    ) -> None:
        with ((court_dir or self.court_dir) / f"{table}.csv").open(
            "w", encoding="utf-8", newline="",
        ) as output:
            output.write(",".join(columns) + "\n")
            writer = csv.writer(
                output, delimiter=",", quotechar='"', escapechar="\\",
                doublequote=False, quoting=csv.QUOTE_ALL, lineterminator="\n",
            )
            for row in rows:
                writer.writerow([row.get(column, "") for column in columns])

    def stage(
        self, *, dockets: list[dict[str, str]] | None = None,
        clusters: list[dict[str, str]] | None = None,
        citations: list[dict[str, str]] | None = None,
        opinions: list[dict[str, str]] | None = None,
        record_courts: list[str] | None = None,
    ) -> None:
        if dockets is None:
            dockets = [{"id": "10", "docket_number": "23-A", "court_id": self.court}]
        if clusters is None:
            clusters = [{
                "id": "100", "date_filed": "2099-01-01", "date_filed_is_approximate": "f",
                "precedential_status": "Published", "docket_id": "10",
            }]
        if citations is None:
            citations = [{"id": "1", "cluster_id": "100", "volume": "1",
                          "reporter": "Example", "page": "2"}]
        if opinions is None:
            opinions = [_opinion("1", "100", xml_harvard="<opinion><p>Fiction.</p></opinion>")]
        self.court_dir.mkdir(parents=True, exist_ok=True)
        tables = {
            "dockets": (DOCKET_COLUMNS, dockets),
            "opinion-clusters": (CLUSTER_COLUMNS, clusters),
            "citations": (CITATION_COLUMNS, citations),
            "opinions": (OPINION_COLUMNS, opinions),
        }
        for table, (columns, rows) in tables.items():
            self._csv(table, columns, rows)
        counts = {table: len(rows) for table, (_, rows) in tables.items()}
        courts = [self.court] if record_courts is None else record_courts
        self.staged_courts = courts
        staging.write_record(self.whole / staging.STAGE_RECORD_NAME, staging.StageRecord(
            self.label, self.source, self.snapshot, courts,
            {table: {"path": f"{table}.csv.bz2", "sha256": "a" * 64, "size": 1}
             for table in staging.STAGE_TABLES},
            {"courts": len(courts), **counts},
            {court: dict(counts) for court in courts},
            self.job, 0.0, self.now.isoformat(),
        ))

    def run_ingest(self, *, limit: int | None = None, record: _Record | None = None,
                   monotonic: Callable[[], float] = time.monotonic,
                   find: Callable[[str], tuple[FoundCitation, ...]] = lambda _text: (),
                   courts: dict[str, dict[str, object]] | None = None) -> caselaw.IngestCounts:
        if courts is None:
            courts = {court: {"level": "circuit", "circuit": "ca6", "state": None}
                      for court in self.staged_courts}
        return caselaw.ingest(
            self.snapshots_root, self.work_root, self.store_root,
            self.label, self.snapshot, self.court, limit, self.job,
            self.record if record is None else record,
            courts=courts,
            clock=lambda: self.now, monotonic=monotonic, find=find,
        )

    def assert_failure(
        self, failure: caselaw.CaselawFailure, *, file: bool = True,
    ) -> None:
        caselaw.write_job_failure(
            self.work_root, self.label, self.snapshot, self.court, self.job,
            failure, clock=lambda: self.now,
        )
        if not file:
            self.assertFalse(self.failure_path.exists())
            return
        self.assertEqual(json.loads(self.failure_path.read_text()), {
            "schema": 1, "job": self.job, "court": self.court,
            "reason": failure.reason, "table": failure.table,
            "error": failure.error, "at": self.now.isoformat(),
        })
        self.assertEqual(stat.S_IMODE(self.failure_path.stat().st_mode), fetch.PARTIAL_MODE)

    def _objects(self) -> set[str]:
        return {path.name for path in self.store_root.rglob("*") if path.is_file()}

    def _ready_from_original(
        self, raw: str, *, column: str = "plain_text", sectioned: bool = False,
        anchored: bool = True,
    ) -> _Row:
        self.stage(opinions=[_opinion("1", "100", **{column: raw})])
        row = self.record.seed(
            1, "ready", 1, court=self.court, source=self.source,
            snapshot=self.snapshot_date, now=self.now, sectioned=sectioned,
            anchored=anchored,
        )
        row.document = replace(row.document, text_source=column)
        original = cas.put(RealHost(), raw.encode(), root=self.store_root)
        assert isinstance(original, str)
        row.sha256 = original
        parsed = opiniontext.canonical_text(column, raw)
        row.canonical_text_sha256 = hashlib.sha256(parsed.text.encode()).hexdigest()
        self.record._check(row)
        return row

    def _ready_without_citations(self, text: str) -> _Row:
        self.stage(opinions=[_opinion("1", "100", plain_text=text)])
        row = self.record.seed(
            1, "ready", 1, court=self.court, source=self.source,
            snapshot=self.snapshot_date, now=self.now, cited=False,
        )
        canonical = cas.put(RealHost(), text.encode(), root=self.store_root)
        assert isinstance(canonical, str)
        row.canonical_text_sha256 = canonical
        row.sections = (caselaw.NewSection(
            "c" * 64, row.document.doc_id, 0, "majority", "line", 0, len(text),
            None, None, None,
        ),)
        self.record._check(row)
        return row

    def test_new_document_citations_finish_with_the_mark_and_failed_has_none(self) -> None:
        text = "We overruled 1 Example 2."
        self.stage(opinions=[
            _opinion("1", "100", plain_text=text, type="020lead"),
            _opinion("2", "100"),
        ])

        def find(source: str) -> tuple[FoundCitation, ...]:
            fragment = "1 Example 2"
            start = source.index(fragment)
            return (FoundCitation(
                start, start + len(fragment), "full", "case", 0,
                "1", "Example", "2", fragment, None,
            ),)

        counts = self.run_ingest(find=find)
        self.assertEqual((counts.ready, counts.failed, counts.cited,
                          counts.treated, counts.signalled), (1, 1, 1, 1, 1))
        ready, failed = self.record.rows[1], self.record.rows[2]
        self.assertEqual(ready.cited_at, self.now)
        self.assertEqual(len(ready.citations), 1)
        edge = ready.citations[0]
        self.assertEqual((edge.raw_cite, edge.to_cluster, edge.doc_id),
                         ("1 Example 2", 100, ready.document.doc_id))
        self.assertEqual(edge.section_id, ready.sections[0].section_id)
        self.assertEqual(ready.treatment_pattern_set, treatment.PATTERN_SET_ID)
        self.assertEqual(ready.treated_at, self.now)
        self.assertEqual(len(ready.signals), 1)
        self.assertEqual((ready.signals[0].citation_id, ready.signals[0].state),
                         (edge.citation_id, "negative"))
        self.assertEqual(text[ready.signals[0].char_start:ready.signals[0].char_end],
                         "overruled")
        self.assertIsNone(failed.cited_at)
        self.assertEqual(failed.citations, ())
        self.assertIsNone(failed.treated_at)

    def test_label_wide_map_resolves_other_court_and_counts_ambiguous_keys(self) -> None:
        text = "We overruled 5 Other 6 and 7 Shared 8."
        self.stage(
            opinions=[_opinion("1", "100", plain_text=text, type="020lead")],
            record_courts=[self.court, "court2"],
            citations=[{"id": "1", "cluster_id": "100", "volume": "7",
                        "reporter": "Shared", "page": "8"}],
        )
        second = self.whole / "court2"
        second.mkdir()
        self._csv("citations", CITATION_COLUMNS, [
            {"id": "2", "cluster_id": "200", "volume": "5",
             "reporter": "Other", "page": "6"},
            {"id": "3", "cluster_id": "201", "volume": "7",
             "reporter": "Shared", "page": "8"},
        ], court_dir=second)

        def find(source: str) -> tuple[FoundCitation, ...]:
            return tuple(
                FoundCitation(
                    source.index(fragment), source.index(fragment) + len(fragment),
                    "full", "case", index, volume, reporter, page, fragment, None,
                )
                for index, (fragment, volume, reporter, page) in enumerate((
                    ("5 Other 6", "5", "Other", "6"),
                    ("7 Shared 8", "7", "Shared", "8"),
                ))
            )

        with self.assertLogs(caselaw.logger, level="INFO") as captured:
            self.run_ingest(find=find, courts={
                self.court: {"level": "circuit", "circuit": "ca6", "state": None},
                "court2": {"level": "circuit", "circuit": "ca5", "state": None},
            })
        row = self.record.rows[1]
        self.assertEqual(tuple(edge.to_cluster for edge in row.citations), (200, None))
        self.assertEqual(tuple((signal.state, signal.no_state_reason)
                               for signal in row.signals),
                         (("caution", None), (None, "unresolved")))
        self.assertEqual(row.opinion.reporter_cites if row.opinion else None,
                         ("7 Shared 8",))
        self.assertIn("ambiguous=1", captured.output[-1])
        self.assertNotIn("Shared", captured.output[-1])
        second.joinpath("citations.csv").unlink()
        with self.assertRaises(caselaw.CaselawFailure) as raised:
            self.run_ingest(find=find)
        self.assertEqual(raised.exception.reason, "stage-mismatch")

    def test_citation_only_backfill_reads_canonical_without_rewalk(self) -> None:
        text = "We overruled 1 Example 2."
        row = self._ready_without_citations(text)

        def find(source: str) -> tuple[FoundCitation, ...]:
            fragment = "1 Example 2"
            start = source.index(fragment)
            return (FoundCitation(
                start, start + len(fragment), "full", "case", 0,
                "1", "Example", "2", fragment, None,
            ),)

        with patch.object(opiniontext, "canonical_text", side_effect=AssertionError("rewalk")):
            counts = self.run_ingest(find=find)
        self.assertEqual((counts.sectioned, counts.anchored, counts.cited, row.attempts),
                         (0, 0, 1, 1))
        self.assertEqual(self.record.events, [
            ("cited", row.document.doc_id), ("treated", row.document.doc_id),
        ])
        self.assertEqual(row.cited_at, self.now)
        self.assertEqual(row.citations[0].to_cluster, 100)
        self.assertEqual((counts.treated, counts.signalled), (1, 1))
        self.assertEqual(row.signals[0].citation_id, row.citations[0].citation_id)
        self.assertEqual(self.run_ingest(find=find).present, 1)
        self.assertEqual(self.record.events, [
            ("cited", row.document.doc_id), ("treated", row.document.doc_id),
        ])

        second = _Record()
        self.record = second
        altered = self._ready_without_citations(text)
        with (patch("gideon.worker.caselaw.gideon.host.cas.get", return_value=b"altered"),
              self.assertRaises(caselaw.CaselawFailure) as raised):
            self.run_ingest(find=find)
        self.assertEqual(raised.exception.reason, "text-mismatch")
        self.assertIsNone(altered.cited_at)
        self.assertEqual(self.record.events, [])

    def test_treatment_only_backfill_reads_recorded_edges_without_rewalk(self) -> None:
        text = "We overruled 1 Example 2."
        row = self._ready_from_original(text, sectioned=True)
        canonical = cas.put(RealHost(), text.encode(), root=self.store_root)
        assert isinstance(canonical, str)
        row.canonical_text_sha256 = canonical
        row.sections = (caselaw.NewSection(
            "d" * 64, row.document.doc_id, 0, "majority", "line", 0, len(text),
            None, None, None,
        ),)
        start = text.index("1 Example 2")
        row.citations = (citations.NewCitation(
            "e" * 64, row.document.doc_id, 0, start, start + len("1 Example 2"),
            row.sections[0].section_id, "majority", "case_cite", "full",
            "1 Example 2", "1 Example 2", None, None, 100,
            "fictional-edges",
        ),)
        row.treatment_pattern_set = None
        row.treated_at = None
        before = (row.status, row.attempts, row.sha256, row.canonical_text_sha256)
        with patch.object(opiniontext, "canonical_text", side_effect=AssertionError("rewalk")):
            counts = self.run_ingest(find=lambda _text: self.fail("adapter used"))
        self.assertEqual((counts.cited, counts.treated, counts.signalled), (0, 1, 1))
        self.assertEqual((row.status, row.attempts, row.sha256,
                          row.canonical_text_sha256), before)
        self.assertEqual(self.record.events, [("treated", row.document.doc_id)])
        self.assertEqual((row.signals[0].citation_id, row.signals[0].state),
                         (row.citations[0].citation_id, "negative"))
        self.assertEqual(self.run_ingest().present, 1)
        self.assertEqual(self.record.events, [("treated", row.document.doc_id)])

        row.treatment_pattern_set = "fictional-older-pattern"
        row.treated_at = self.now
        row.signals = ()
        self.assertEqual(self.run_ingest().treated, 1)
        self.assertEqual(row.treatment_pattern_set, treatment.PATTERN_SET_ID)

    def test_treatment_defect_gives_back_and_files_only_its_class(self) -> None:
        sentinel = "PRIVATE_TREATMENT_SENTINEL"
        self.stage(opinions=[_opinion("1", "100", plain_text=sentinel)])
        with (patch.object(treatment, "signals", side_effect=RuntimeError(sentinel)),
              self.assertLogs(caselaw.logger, level="INFO") as logged,
              self.assertRaises(caselaw.CaselawFailure) as raised):
            self.run_ingest()
        self.assertEqual((raised.exception.reason, raised.exception.error),
                         ("treatment", "RuntimeError"))
        self.assertEqual((self.record.rows[1].status, self.record.rows[1].attempts),
                         ("processing", 0))
        self.assertEqual([event for event, _ in self.record.events],
                         ["begin", "give_back"])
        self.assert_failure(raised.exception)
        self.assertNotIn(sentinel, self.failure_path.read_text())
        self.assertNotIn(sentinel, "\n".join(logged.output))

    def test_unanchored_backfill_cites_over_the_recorded_sections(self) -> None:
        text = "We overruled 1 Example 2."
        row = self._ready_from_original(text, sectioned=True, anchored=False)
        row.cited_at = None
        row.treatment_pattern_set = None
        row.treated_at = None
        recorded = (caselaw.NewSection(
            "9" * 64, row.document.doc_id, 0, "majority", "line", 0, len(text),
            None, None, None,
        ),)
        row.sections = recorded

        def find(source: str) -> tuple[FoundCitation, ...]:
            fragment = "1 Example 2"
            start = source.index(fragment)
            return (FoundCitation(
                start, start + len(fragment), "full", "case", 0,
                "1", "Example", "2", fragment, None,
            ),)

        counts = self.run_ingest(find=find)
        self.assertEqual((counts.sectioned, counts.anchored, counts.cited,
                          counts.treated, counts.signalled), (0, 1, 1, 1, 1))
        self.assertEqual(self.record.events, [
            ("anchored", row.document.doc_id), ("cited", row.document.doc_id),
            ("treated", row.document.doc_id),
        ])
        self.assertEqual(row.sections, recorded)
        self.assertEqual(row.citations[0].section_id, recorded[0].section_id)
        self.assertEqual(row.citations[0].to_cluster, 100)
        self.assertEqual(row.signals[0].state, "negative")

    def test_citation_defect_gives_back_and_missing_adapter_refuses_before_rows(self) -> None:
        sentinel = "PRIVATE_CITE_SENTINEL"
        self.stage(opinions=[_opinion("1", "100", plain_text=sentinel)])

        def fail(_text: str) -> tuple[FoundCitation, ...]:
            raise RuntimeError(sentinel)

        with self.assertRaises(caselaw.CaselawFailure) as raised:
            self.run_ingest(find=fail)
        self.assertEqual((raised.exception.reason, raised.exception.error),
                         ("citations", "RuntimeError"))
        self.assert_failure(raised.exception)
        self.assertNotIn(sentinel, self.failure_path.read_text())
        self.assertEqual((self.record.rows[1].status, self.record.rows[1].attempts),
                         ("processing", 0))
        self.assertEqual([event for event, _ in self.record.events], ["begin", "give_back"])

        fresh = _Record()
        with (patch.dict(sys.modules, {"gideon.casecite.adapter": None}),
              self.assertRaises(caselaw.CaselawFailure) as missing):
            caselaw.ingest(
                self.snapshots_root, self.work_root, self.store_root,
                self.label, self.snapshot, self.court, None, self.job, fresh,
                courts={self.court: {"level": "circuit", "circuit": "ca6", "state": None}},
                clock=lambda: self.now,
            )
        self.assertEqual((missing.exception.reason, missing.exception.error),
                         ("citations", "ImportError"))
        self.assertEqual(fresh.rows, {})
        self.assertEqual(fresh.present_calls, [])

    def test_ready_failed_outcomes_and_stored_bytes(self) -> None:
        raw = "<opinion><p>Cafe\u0301<page-number>*3</page-number> wins.</p></opinion>"
        no_text = _opinion("2", "100", html_with_citations="<p>derived only</p>")
        unparseable = "<"
        empty = "<opinion><page-number>*3</page-number></opinion>"
        self.stage(opinions=[
            _opinion("1", "100", xml_harvard=raw), no_text,
            _opinion("3", "100", xml_harvard=unparseable),
            _opinion("4", "100", xml_harvard=empty),
        ])
        self.assertEqual(stat.S_IMODE(self.store_root.stat().st_mode), cas.DIRECTORY_MODE)
        counts = self.run_ingest()
        self.assertEqual(counts, caselaw.IngestCounts(4, 0, 1, 3, 0, 0, 0, 1, 0, 0, 1, 1, 0))
        self.assertEqual(self.record.present_calls, [(self.source, self.snapshot_date, self.court)])
        self.assertEqual(self.record.closed, 1)

        ready = self.record.rows[1]
        self.assertEqual(ready.document.doc_id, _doc_id(1, self.snapshot_date))
        self.assertEqual((ready.document.profile, ready.document.source,
                          ready.document.source_snapshot, ready.document.text_source),
                         ("caselaw", self.source, self.snapshot_date, "xml_harvard"))
        self.assertEqual((ready.status, ready.attempts, ready.ingested_at, ready.anchored_at),
                         ("ready", 1, self.now, self.now))
        self.assertEqual(cas.get(RealHost(), ready.sha256 or "", root=self.store_root), raw.encode())
        canonical = opiniontext.canonical_text("xml_harvard", raw).text.encode()
        self.assertEqual(canonical, "Café wins.".encode())
        self.assertEqual(ready.canonical_text_sha256, hashlib.sha256(canonical).hexdigest())
        self.assertEqual(cas.get(RealHost(), ready.canonical_text_sha256 or "",
                                 root=self.store_root), canonical)
        expected = sections.segment(
            opiniontext.canonical_text("xml_harvard", raw),
            opinion_type="020", per_curiam=False, column="xml_harvard",
        )
        self.assertEqual(len(ready.sections), len(expected))
        for index, (written, section) in enumerate(zip(ready.sections, expected, strict=True)):
            self.assertEqual(written.ordinal, index)
            self.assertEqual(written.doc_id, ready.document.doc_id)
            self.assertEqual(written.section_id, hashlib.sha256(
                f"{ready.document.doc_id}\n{section.char_start}\n{section.char_end}".encode()
            ).hexdigest())
            self.assertEqual(
                (written.section_type, written.typed_by, written.char_start,
                 written.char_end, written.label, written.ref_offset,
                 written.parent_section_id),
                (section.section_type, section.typed_by, section.char_start,
                 section.char_end, section.label, section.ref_offset,
                 ready.sections[section.parent].section_id
                 if section.parent is not None else None),
            )
        parsed = opiniontext.canonical_text("xml_harvard", raw)
        derived = anchors.anchor(parsed, expected)
        self.assertEqual(len(ready.anchors), len(derived.anchors))
        for written_anchor, derived_anchor in zip(ready.anchors, derived.anchors, strict=True):
            scheme = dict(derived_anchor.attrs)["scheme"]
            self.assertEqual(written_anchor.anchor_id, hashlib.sha256(
                f"{ready.document.doc_id}\n{derived_anchor.kind}\n{scheme}\n"
                f"{derived_anchor.char_start}\n{derived_anchor.char_end}".encode()
            ).hexdigest())
            self.assertEqual((written_anchor.doc_id, written_anchor.kind, written_anchor.label,
                              written_anchor.char_start, written_anchor.char_end,
                              dict(written_anchor.attrs)),
                             (ready.document.doc_id, derived_anchor.kind, derived_anchor.label,
                              derived_anchor.char_start, derived_anchor.char_end,
                              dict(derived_anchor.attrs)))

        blank = self.record.rows[2]
        self.assertEqual((blank.status, blank.failure_reason, blank.document.text_source,
                          blank.sha256, blank.canonical_text_sha256, blank.anchored_at,
                          blank.anchors),
                         ("failed", "no-text", None, None, None, None, ()))
        for opinion_id, original, reason in (
            (3, unparseable, "unparseable"), (4, empty, "empty"),
        ):
            with self.subTest(opinion_id=opinion_id):
                row = self.record.rows[opinion_id]
                self.assertEqual((row.status, row.failure_reason, row.canonical_text_sha256),
                                 ("failed", reason, None))
                self.assertEqual(row.sha256, hashlib.sha256(original.encode()).hexdigest())
                self.assertEqual(cas.get(RealHost(), row.sha256 or "", root=self.store_root),
                                 original.encode())
                self.assertEqual((row.sections, row.anchors, row.anchored_at), ((), (), None))

    def test_each_text_source_becomes_a_ready_document(self) -> None:
        examples = {
            "xml_harvard": "<opinion><p>Harvard text.</p></opinion>",
            "html_columbia": "<p>Columbia text.</p>",
            "html_lawbox": "<p>Lawbox text.</p>",
            "html_anon_2020": "<bodytext><p>Anonymous text.</p></bodytext>",
            "html": "<p>Generic text.</p>",
            "plain_text": "Plain text.",
        }
        self.stage(opinions=[
            _opinion(str(index), "100", html_with_citations="<p>derived only</p>",
                     **{column: examples[column]})
            for index, column in enumerate(opiniontext.TEXT_SOURCES, start=1)
        ])
        self.assertEqual(self.run_ingest().ready, len(opiniontext.TEXT_SOURCES))
        for index, column in enumerate(opiniontext.TEXT_SOURCES, start=1):
            with self.subTest(column=column):
                row = self.record.rows[index]
                self.assertEqual((row.status, row.document.text_source), ("ready", column))
                self.assertEqual(cas.get(RealHost(), row.sha256 or "", root=self.store_root),
                                 examples[column].encode())
                canonical = opiniontext.canonical_text(column, examples[column]).text.encode()
                self.assertEqual(row.canonical_text_sha256,
                                 hashlib.sha256(canonical).hexdigest())
                self.assertEqual(cas.get(RealHost(), row.canonical_text_sha256 or "",
                                         root=self.store_root), canonical)
                self.assertEqual((row.anchored_at, row.anchors), (self.now, ()))
        events = list(self.record.events)
        self.assertEqual(self.run_ingest(), caselaw.IngestCounts(6, 6, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(self.record.events, events)

    def test_unexpected_parse_exception_is_an_unparseable_document(self) -> None:
        raw = "<opinion><p>Fictitious body.</p></opinion>"
        self.stage(opinions=[_opinion("1", "100", xml_harvard=raw)])
        with patch.object(opiniontext, "canonical_text", side_effect=RecursionError("private text")):
            counts = self.run_ingest()
        self.assertEqual(counts, caselaw.IngestCounts(1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        row = self.record.rows[1]
        self.assertEqual((row.status, row.failure_reason, row.canonical_text_sha256),
                         ("failed", "unparseable", None))
        self.assertEqual(cas.get(RealHost(), row.sha256 or "", root=self.store_root),
                         raw.encode())

    def test_precedential_dockets_and_citations_keep_the_staged_metadata(self) -> None:
        statuses = ("Published", "Unpublished", "Errata", "")
        clusters = [{
            "id": str(100 + index), "date_filed": "2099-01-01",
            "date_filed_is_approximate": "t" if index == 0 else "f",
            "precedential_status": raw, "docket_id": str(10 + index),
        } for index, raw in enumerate(statuses)]
        dockets = [{"id": str(10 + index), "court_id": self.court,
                    "docket_number": "" if index == 0 else f"23-{index}"}
                   for index in range(len(statuses))]
        citations = [
            {"id": "1", "cluster_id": "100", "volume": "1", "reporter": "Example", "page": "2"},
            {"id": "2", "cluster_id": "100", "volume": "3", "reporter": "Example", "page": "4"},
        ]
        self.stage(
            dockets=dockets, clusters=clusters, citations=citations,
            opinions=[_opinion(str(index + 1), str(100 + index), plain_text="Fiction.")
                      for index in range(len(statuses))],
        )
        self.assertEqual(self.run_ingest().ready, len(statuses))
        for index, (raw, expected) in enumerate(zip(
            statuses, ("published", "unpublished", "unknown", "unknown"), strict=True,
        ), start=1):
            with self.subTest(raw=raw):
                opinion = self.record.rows[index].opinion
                assert opinion is not None
                self.assertEqual((opinion.precedential, opinion.precedential_raw), (expected, raw))
                self.assertEqual(opinion.opinion_type, "020")
        first = self.record.rows[1].opinion
        second = self.record.rows[2].opinion
        assert first is not None and second is not None
        self.assertEqual((first.docket, first.reporter_cites, first.decided_date,
                          first.decided_date_is_approximate),
                         (None, ("1 Example 2", "3 Example 4"), date(2099, 1, 1), True))
        self.assertEqual(second.reporter_cites, ())

    def test_staged_rows_use_quoted_values_and_backslash_escapes(self) -> None:
        docket_number = 'Fiction "quoted" \\ path'
        self.stage(dockets=[{
            "id": "10", "docket_number": docket_number, "court_id": self.court,
        }])
        staged_bytes = (self.court_dir / "dockets.csv").read_bytes()
        self.assertIn(b'"Fiction \\"quoted\\" \\\\ path"', staged_bytes)
        self.assertIn(b'"1",', (self.court_dir / "opinions.csv").read_bytes())
        self.assertIn(b',"100"\n', (self.court_dir / "opinions.csv").read_bytes())
        self.assertEqual(self.run_ingest().ready, 1)
        opinion = self.record.rows[1].opinion
        assert opinion is not None
        self.assertEqual(opinion.docket, docket_number)

    def test_present_terminal_rows_are_skipped_without_writes(self) -> None:
        self.stage(opinions=[_opinion(str(index), "100", plain_text=f"Fiction {index}.")
                             for index in range(1, 4)])
        for index, status in enumerate(("ready", "failed", "withdrawn"), start=1):
            self.record.seed(index, status, 1, court=self.court, source=self.source,
                             snapshot=self.snapshot_date, now=self.now)
        counts = self.run_ingest()
        self.assertEqual(counts, caselaw.IngestCounts(3, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(self.record.events, [])
        self.assertEqual(self._objects(), set())

    def test_section_ids_depend_on_document_even_for_equal_text(self) -> None:
        body = "Fictitious opinion body."
        self.stage(opinions=[
            _opinion("1", "100", plain_text=body),
            _opinion("2", "100", plain_text=body),
        ])
        self.assertEqual(self.run_ingest().ready, 2)
        first, second = self.record.rows[1], self.record.rows[2]
        self.assertEqual(first.canonical_text_sha256, second.canonical_text_sha256)
        self.assertEqual(
            [(s.char_start, s.char_end) for s in first.sections],
            [(s.char_start, s.char_end) for s in second.sections],
        )
        self.assertTrue(first.sections)
        self.assertNotEqual(first.sections[0].section_id, second.sections[0].section_id)

    def test_anchor_ids_bind_document_and_scheme_for_equal_text(self) -> None:
        raw = (
            '<opinion><p>A<page-number>*8</page-number>'
            '<page-number>**72</page-number>B</p></opinion>'
        )
        self.stage(opinions=[
            _opinion("1", "100", xml_harvard=raw),
            _opinion("2", "100", xml_harvard=raw),
        ])
        self.assertEqual(self.run_ingest().anchored, 2)
        first, second = self.record.rows[1], self.record.rows[2]
        self.assertEqual(first.canonical_text_sha256, second.canonical_text_sha256)
        self.assertEqual([dict(row.attrs)["scheme"] for row in first.anchors], ["1", "2"])
        self.assertEqual([row.char_start for row in first.anchors], [1, 1])
        self.assertEqual(len({row.anchor_id for row in (*first.anchors, *second.anchors)}), 4)

    def test_footnote_section_writes_the_parent_id(self) -> None:
        raw = (
            '<opinion><p>Fictitious body<footnotemark>1</footnotemark>.</p>'
            '<footnote label="1"><p>Fictitious note.</p></footnote></opinion>'
        )
        self.stage(opinions=[_opinion("1", "100", xml_harvard=raw)])
        self.assertEqual(self.run_ingest().ready, 1)
        written = self.record.rows[1].sections
        footnote = next(section for section in written if section.section_type == "footnote")
        self.assertEqual(footnote.label, "1")
        self.assertEqual(footnote.parent_section_id, written[0].section_id)
        self.assertEqual(footnote.ref_offset,
                         opiniontext.canonical_text("xml_harvard", raw).text.index("body") + 4)

    def test_ready_backfill_reads_original_without_counting_an_attempt(self) -> None:
        row = self._ready_from_original("Fictitious recovered opinion.")
        original_state = (row.status, row.attempts, row.sha256, row.canonical_text_sha256)
        self.assertEqual(self.run_ingest(), caselaw.IngestCounts(1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1, 1, 0))
        self.assertEqual(
            (row.status, row.attempts, row.sha256, row.canonical_text_sha256),
            original_state,
        )
        self.assertTrue(row.sections)
        self.assertEqual((row.cited_at, row.citations), (self.now, ()))
        events = [("sectioned", row.document.doc_id), ("cited", row.document.doc_id),
                  ("treated", row.document.doc_id)]
        self.assertEqual(self.record.events, events)
        self.assertEqual(self.run_ingest(), caselaw.IngestCounts(1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(self.record.events, events)

    def test_ready_backfill_writes_only_missing_anchors_and_counts_pgmap(self) -> None:
        raw = '<opinion><p pgmap="1 2 3">A<page-number>*8</page-number>B</p></opinion>'
        row = self._ready_from_original(raw, column="xml_harvard", sectioned=True,
                                        anchored=False)
        original_state = (row.status, row.attempts, row.sha256, row.canonical_text_sha256,
                          row.sections)
        with self.assertLogs(caselaw.logger, level="INFO") as logged:
            counts = self.run_ingest()
        self.assertEqual(counts, caselaw.IngestCounts(1, 0, 0, 0, 0, 0, 0, 1, 1, 1, 0, 0, 0))
        for token in ("anchored=1", "pgmap_checked=1", "pgmap_disagreeing=1"):
            self.assertIn(token, logged.output[-1])
        self.assertEqual((row.status, row.attempts, row.sha256, row.canonical_text_sha256,
                          row.sections), original_state)
        self.assertEqual(row.anchored_at, self.now)
        self.assertEqual([(anchor.label, dict(anchor.attrs)["scheme"])
                          for anchor in row.anchors], [("8", "1")])
        self.assertEqual(self.record.events, [("anchored", row.document.doc_id)])
        self.assertEqual(self.run_ingest(), caselaw.IngestCounts(1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(self.record.events, [("anchored", row.document.doc_id)])

    def test_ready_backfill_writes_sections_and_anchors_from_one_parse(self) -> None:
        raw = '<opinion><p>A<page-number>*8</page-number>B</p></opinion>'
        row = self._ready_from_original(raw, column="xml_harvard", anchored=False)
        original_state = (row.status, row.attempts, row.sha256, row.canonical_text_sha256)
        with patch.object(opiniontext, "canonical_text", wraps=opiniontext.canonical_text) as walk:
            counts = self.run_ingest()
        self.assertEqual(walk.call_count, 1)
        self.assertEqual(counts, caselaw.IngestCounts(1, 0, 0, 0, 0, 0, 1, 1, 0, 0, 1, 1, 0))
        self.assertEqual((row.status, row.attempts, row.sha256, row.canonical_text_sha256),
                         original_state)
        self.assertTrue(row.sections)
        self.assertTrue(row.anchors)
        self.assertEqual(row.anchored_at, self.now)
        events = ["sectioned", "anchored", "cited", "treated"]
        self.assertEqual([event for event, _ in self.record.events], events)
        self.assertEqual(self.run_ingest(), caselaw.IngestCounts(1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        self.assertEqual([event for event, _ in self.record.events], events)

    def test_backfill_refuses_text_mismatch_and_missing_original(self) -> None:
        row = self._ready_from_original("Fictitious original opinion.")
        altered = cas.put(
            RealHost(), b"Fictitious altered opinion.", root=self.store_root,
        )
        assert isinstance(altered, str)
        row.sha256 = altered
        with self.assertRaises(caselaw.CaselawFailure) as raised:
            self.run_ingest()
        self.assertEqual(raised.exception.reason, "text-mismatch")
        self.assert_failure(raised.exception)
        self.assertEqual((row.status, row.attempts, row.sections), ("ready", 1, ()))

        row.document = replace(row.document, text_source="bad-column")
        with self.assertRaises(caselaw.CaselawFailure) as refused:
            self.run_ingest()
        self.assertEqual((refused.exception.reason, refused.exception.error),
                         ("text-mismatch", "ValueError"))
        self.assert_failure(refused.exception)
        row.document = replace(row.document, text_source="plain_text")
        row.sha256 = "f" * 64
        with self.assertRaises(caselaw.CaselawFailure) as missing:
            self.run_ingest()
        self.assertEqual(missing.exception.reason, "store")
        self.assert_failure(missing.exception)
        self.assertEqual(self.record.events, [])

    def test_segmenter_exception_refuses_new_and_backfill_without_spending_attempt(self) -> None:
        self.stage(opinions=[_opinion("1", "100", plain_text="Fictitious opinion.")])
        with (patch.object(sections, "segment", side_effect=RuntimeError("private body")),
              self.assertRaises(caselaw.CaselawFailure) as raised):
            self.run_ingest()
        self.assertEqual((raised.exception.reason, raised.exception.error),
                         ("segmenter", "RuntimeError"))
        self.assert_failure(raised.exception)
        row = self.record.rows[1]
        self.assertEqual((row.status, row.attempts, row.sections), ("processing", 0, ()))
        self.assertEqual([event for event, _ in self.record.events], ["begin", "give_back"])

        second = _Record()
        self.record = second
        backfill = self._ready_from_original("Fictitious recovered opinion.")
        with (patch.object(sections, "segment", side_effect=RuntimeError("private body")),
              self.assertRaises(caselaw.CaselawFailure) as refused):
            self.run_ingest()
        self.assertEqual((refused.exception.reason, refused.exception.error),
                         ("segmenter", "RuntimeError"))
        self.assert_failure(refused.exception)
        self.assertEqual((backfill.status, backfill.attempts, backfill.sections),
                         ("ready", 1, ()))
        self.assertEqual(self.record.events, [])

    def test_anchorer_exception_refuses_before_writes_and_gives_back_attempt(self) -> None:
        self.stage(opinions=[_opinion("1", "100", xml_harvard=(
            '<opinion><p>A<page-number>*8</page-number>B</p></opinion>'
        ))])
        with (patch.object(anchors, "anchor", side_effect=RuntimeError("private body")),
              self.assertLogs(caselaw.logger, level="INFO") as logged,
              self.assertRaises(caselaw.CaselawFailure) as raised):
            self.run_ingest()
        self.assertEqual((raised.exception.reason, raised.exception.error),
                         ("anchors", "RuntimeError"))
        self.assert_failure(raised.exception)
        row = self.record.rows[1]
        self.assertEqual((row.status, row.attempts, row.sections, row.anchors, row.anchored_at),
                         ("processing", 0, (), (), None))
        self.assertEqual([event for event, _ in self.record.events], ["begin", "give_back"])
        self.assertIn("action=caselaw_failed", logged.output[-1])
        for token in ("anchored=0", "pgmap_checked=0", "pgmap_disagreeing=0"):
            self.assertIn(token, logged.output[-1])
        self.assertNotIn("private body", logged.output[-1])

        self.record = _Record()
        backfill = self._ready_from_original("Fictitious recovered opinion.", anchored=False)
        with (patch.object(anchors, "anchor", side_effect=RuntimeError("private body")),
              self.assertRaises(caselaw.CaselawFailure) as refused):
            self.run_ingest()
        self.assertEqual((refused.exception.reason, refused.exception.error),
                         ("anchors", "RuntimeError"))
        self.assertEqual((backfill.status, backfill.attempts, backfill.sections,
                          backfill.anchors, backfill.anchored_at),
                         ("ready", 1, (), (), None))
        self.assertEqual(self.record.events, [])

    def test_per_curiam_flag_is_read_and_bad_value_refuses_opinions(self) -> None:
        self.stage(opinions=[_opinion(
            "1", "100", per_curiam="t", plain_text="Fictitious body.",
        )])
        self.assertEqual(self.run_ingest().ready, 1)
        self.assertEqual(self.record.rows[1].sections[0].section_type, "per_curiam")
        self.stage(opinions=[_opinion(
            "2", "100", per_curiam="maybe", plain_text="Fictitious body.",
        )])
        with self.assertRaises(caselaw.CaselawFailure) as raised:
            self.run_ingest()
        self.assertEqual((raised.exception.reason, raised.exception.table),
                         ("malformed", "opinions"))
        self.assert_failure(raised.exception)
        self.assertNotIn(2, self.record.rows)

    def test_processing_attempts_retry_and_interrupt_at_the_bound(self) -> None:
        self.stage(opinions=[_opinion(str(index), "100", plain_text=f"Fiction {index}.")
                             for index in range(1, 4)])
        for index, attempts in enumerate((0, 1, caselaw.MAX_ATTEMPTS), start=1):
            self.record.seed(index, "processing", attempts, court=self.court,
                             source=self.source, snapshot=self.snapshot_date, now=self.now)
        counts = self.run_ingest()
        self.assertEqual(counts, caselaw.IngestCounts(3, 0, 2, 1, 2, 1, 0, 2, 0, 0, 2, 2, 0))
        self.assertEqual([(self.record.rows[index].status, self.record.rows[index].attempts)
                          for index in (1, 2)], [("ready", 1), ("ready", 2)])
        third = self.record.rows[3]
        self.assertEqual((third.status, third.failure_reason, third.sha256),
                         ("failed", "interrupted", None))
        self.assertNotIn(hashlib.sha256(b"Fiction 3.").hexdigest(), self._objects())
        self.assertEqual([event for event, _ in self.record.events],
                         ["retry", "ready", "retry", "ready", "failed"])

    def test_store_refusal_gives_back_then_retries_from_zero(self) -> None:
        self.stage()
        self.store_root.rmdir()
        self.store_root.write_text("occupied fictitious store root")
        with self.assertRaises(caselaw.CaselawFailure) as raised:
            self.run_ingest()
        self.assertEqual(raised.exception.reason, "store")
        first = self.record.rows[1]
        self.assertEqual((first.status, first.attempts, first.sha256),
                         ("processing", 0, None))
        self.assertEqual([event for event, _ in self.record.events], ["begin", "give_back"])
        self.assert_failure(raised.exception)
        self.store_root.unlink()
        self.store_root.mkdir(mode=cas.DIRECTORY_MODE)
        self.store_root.chmod(cas.DIRECTORY_MODE)
        self.assertEqual(self.run_ingest(), caselaw.IngestCounts(1, 0, 1, 0, 1, 0, 0, 1, 0, 0, 1, 1, 0))
        self.assertEqual((first.status, first.attempts), ("ready", 1))
        self.assertEqual([event for event, _ in self.record.events],
                         ["begin", "give_back", "retry", "ready"])
        self.assertFalse(self.failure_path.exists())

    def test_limit_counts_seen_rows_and_repeat_changes_nothing(self) -> None:
        self.stage(opinions=[_opinion(str(index), "100", plain_text=f"Fiction {index}.")
                             for index in range(1, 4)])
        self.assertEqual(self.run_ingest(limit=2), caselaw.IngestCounts(2, 0, 2, 0, 0, 0, 0, 2, 0, 0, 2, 2, 0))
        names = self._objects()
        events = list(self.record.events)
        self.assertEqual(self.run_ingest(limit=2), caselaw.IngestCounts(2, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(self._objects(), names)
        self.assertEqual(self.record.events, events)
        self.assertNotIn(3, self.record.rows)

    def test_invalid_arguments_and_missing_stage_file_safe_reasons(self) -> None:
        self.stage()
        invalid = (
            ("bad", self.snapshot, self.court, None),
            (self.label, "fictional-dump-2099-99-02", self.court, None),
            (self.label, self.snapshot, "Bad Court", None),
            (self.label, self.snapshot, self.court, True),
            (self.label, self.snapshot, self.court, caselaw.LIMIT_MAX + 1),
            (self.label, self.snapshot, self.court, 0),
        )
        for label, snapshot, court, limit in invalid:
            with self.subTest(arguments=(label, snapshot, court, limit)):
                with self.assertRaises(caselaw.CaselawFailure) as raised:
                    caselaw.ingest(self.snapshots_root, self.work_root, self.store_root,
                                   label, snapshot, court, limit, self.job, self.record)
                self.assertEqual(raised.exception.reason, "invalid")
                if label == self.label and snapshot == self.snapshot and court == self.court:
                    self.assert_failure(raised.exception)
        self.assertEqual(self.record.present_calls, [])
        (self.whole / staging.STAGE_RECORD_NAME).unlink()
        with self.assertRaises(caselaw.CaselawFailure) as raised:
            self.run_ingest()
        self.assertEqual(raised.exception.reason, "missing-stage")
        self.assert_failure(raised.exception)

    def test_courts_argument_is_validated_before_record_access(self) -> None:
        self.stage()
        invalid_values: tuple[dict[str, dict[str, object]], ...] = (
            {"Bad Court": {"level": "circuit", "circuit": "ca6", "state": None}},
            {self.court: {"level": "imaginary", "circuit": None, "state": None}},
            {self.court: {"level": "circuit", "circuit": "ca12", "state": None}},
            {self.court: {"level": "circuit", "circuit": "ca6", "state": "ZZ"}},
        )
        for courts in invalid_values:
            with self.subTest(courts=courts), self.assertRaises(caselaw.CaselawFailure) as raised:
                self.run_ingest(courts=courts)
            self.assertEqual(raised.exception.reason, "invalid")
        for mapping, expected in ((None, "invalid"), ({}, "stage-mismatch")):
            with self.subTest(courts=mapping), self.assertRaises(caselaw.CaselawFailure) as raised:
                caselaw.ingest(
                    self.snapshots_root, self.work_root, self.store_root,
                    self.label, self.snapshot, self.court, None, self.job, self.record,
                    courts=mapping,
                )
            self.assertEqual(raised.exception.reason, expected)
        with self.assertRaises(caselaw.CaselawFailure) as raised:
            self.run_ingest(courts={
                self.court: {"level": "circuit", "circuit": "ca6", "state": None},
                "court2": {"level": "circuit", "circuit": "ca5", "state": None},
            })
        self.assertEqual(raised.exception.reason, "stage-mismatch")
        self.assertEqual(self.record.present_calls, [])
        self.assertEqual(self.record.rows, {})

    def test_stage_chain_refusals_name_the_damaged_court(self) -> None:
        self.stage(record_courts=["othercourt"])
        with self.assertRaises(caselaw.CaselawFailure) as raised:
            self.run_ingest()
        self.assertEqual(raised.exception.reason, "stage-mismatch")
        self.assert_failure(raised.exception)
        cases: tuple[
            tuple[list[dict[str, str]] | None, list[dict[str, str]] | None], ...
        ] = (
            ([], None),
            (None, []),
            ([{"id": "10", "docket_number": "23-A", "court_id": "othercourt"}], None),
        )
        for dockets, clusters in cases:
            with self.subTest(dockets=dockets, clusters=clusters):
                self.stage(dockets=dockets, clusters=clusters)
                with self.assertRaises(caselaw.CaselawFailure) as raised:
                    self.run_ingest()
                self.assertEqual((raised.exception.reason, raised.exception.table),
                                 ("stage-mismatch", "opinions"))
                self.assert_failure(raised.exception)
        self.assertEqual(self.record.rows, {})

    def test_malformed_header_id_date_and_boolean_name_the_table(self) -> None:
        cases: tuple[tuple[str, str, str], ...] = (
            ("opinions", "header", "ValueError"),
            ("opinions", "width", "StageFailure"),
            ("dockets", "id", "ValueError"),
            ("opinion-clusters", "date", "ValueError"),
            ("opinion-clusters", "boolean", "ValueError"),
        )
        for table, defect, error in cases:
            with self.subTest(table=table, defect=defect):
                self.stage()
                path = self.court_dir / f"{table}.csv"
                body = path.read_text()
                if defect == "header":
                    body = body.replace("type,", "missing_type,", 1)
                elif defect == "width":
                    body += '"extra"\n'
                elif defect == "id":
                    body = body.replace('"10"', '"zero"', 1)
                elif defect == "date":
                    body = body.replace('"2099-01-01"', '"2099-99-01"', 1)
                else:
                    body = body.replace('"f"', '"maybe"', 1)
                path.write_text(body)
                with self.assertRaises(caselaw.CaselawFailure) as raised:
                    self.run_ingest()
                self.assertEqual((raised.exception.reason, raised.exception.table,
                                  raised.exception.error), ("malformed", table, error))
                self.assert_failure(raised.exception)

    def test_database_and_local_failures_are_filed_with_classes(self) -> None:
        self.stage()
        self.record.fail_present = caselaw.CaselawFailure("database", error="OperationalError")
        with self.assertRaises(caselaw.CaselawFailure) as raised:
            self.run_ingest()
        self.assertEqual((raised.exception.reason, raised.exception.error),
                         ("database", "OperationalError"))
        self.assert_failure(raised.exception)
        self.assertEqual(self.record.closed, 1)
        self.record.fail_present = None
        with (patch("gideon.worker.caselaw.os.open", side_effect=PermissionError("private path")),
              self.assertRaises(caselaw.CaselawFailure) as raised):
            self.run_ingest()
        self.assertEqual((raised.exception.reason, raised.exception.error),
                         ("local", "PermissionError"))
        self.assert_failure(raised.exception)

    def test_busy_lock_and_symbolic_links_refuse_without_following(self) -> None:
        self.stage()
        with self.lock_path.open("w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with self.assertRaises(caselaw.CaselawFailure) as raised:
                    self.run_ingest()
                self.assertEqual(raised.exception.reason, "busy")
                self.assert_failure(raised.exception, file=False)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        self.lock_path.unlink()

        target = self.work_root / "outside-court"
        self.court_dir.rename(target)
        self.court_dir.symlink_to(target, target_is_directory=True)
        with self.assertRaises(caselaw.CaselawFailure) as raised:
            self.run_ingest()
        self.assertEqual(raised.exception.reason, "stage-mismatch")
        self.court_dir.unlink()
        target.rename(self.court_dir)

        target = self.work_root / "outside-lock"
        target.write_text("fictitious lock target")
        self.lock_path.symlink_to(target)
        with self.assertRaises(caselaw.CaselawFailure) as raised:
            self.run_ingest()
        self.assertEqual(raised.exception.reason, "stage-mismatch")
        self.assertEqual(target.read_text(), "fictitious lock target")
        self.assertEqual(self.record.rows, {})

    def test_logs_exclude_opinion_docket_and_citation_text(self) -> None:
        sentinel = "PRIVATE_CASELAW_SENTINEL"
        self.stage(
            dockets=[{"id": "10", "docket_number": sentinel, "court_id": self.court}],
            citations=[{"id": "1", "cluster_id": "100", "volume": "1",
                        "reporter": sentinel, "page": "2"}],
            opinions=[_opinion("1", "100", plain_text=sentinel)],
        )
        output = io.StringIO()
        root_logger = logging.getLogger()
        previous_handlers = list(root_logger.handlers)
        previous_level = root_logger.level
        ticks = iter(range(0, 10000, fetch.LOG_INTERVAL_SECONDS + 1))
        logs.install_handler(output)
        try:
            self.run_ingest(
                monotonic=lambda: float(next(ticks)),
                find=lambda text: (FoundCitation(
                    text.index(sentinel), text.index(sentinel) + len(sentinel),
                    "reference", "unknown", None, None, None, None, None, None,
                ),),
            )
        finally:
            root_logger.handlers[:] = previous_handlers
            root_logger.setLevel(previous_level)
        lines = output.getvalue().splitlines()
        self.assertTrue(any("action=caselaw_progress" in line for line in lines))
        self.assertTrue(any("action=caselaw_end" in line for line in lines))
        for line in lines:
            self.assertNotIn(sentinel, line)
            self.assertIn("sectioned=", line)
            self.assertIn("anchored=", line)
            self.assertIn("pgmap_checked=", line)
            self.assertIn("pgmap_disagreeing=", line)
            self.assertIn("cited=", line)
            self.assertIn("treated=", line)
            self.assertIn("signalled=", line)
            self.assertIn("ambiguous=", line)
        self.assertEqual(self.record.rows[1].citations[0].raw_cite, sentinel)


class _CapturedConnection:
    """Capture SQL calls and transaction boundaries without a database."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.rows: list[tuple[object, ...]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.fail_execute = False
        self.fail_commit = False
        self.fail_close = False

    def cursor(self) -> _CapturedConnection:
        return self

    def __enter__(self) -> _CapturedConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, parameters: tuple[object, ...]) -> None:
        self.statements.append((statement, parameters))
        if self.fail_execute:
            raise psycopg.OperationalError("private database detail")

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows

    def commit(self) -> None:
        if self.fail_commit:
            raise psycopg.OperationalError("private database detail")
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        if self.fail_close:
            raise psycopg.OperationalError("private database detail")
        self.closed = True


class PsycopgDocumentRecord(unittest.TestCase):
    """The SQL record binds metadata and commits each transition once."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.password = Path(temporary.name) / "password"
        self.password.write_text("fictitious-secret")
        self.configuration = settings.Settings(
            "db.example.test", 5432, "gideon", "gideon_worker", self.password, 2,
        )
        self.connection = _CapturedConnection()
        self.connection_kwargs: dict[str, object] = {}

    def connect(self, **kwargs: object) -> _CapturedConnection:
        self.connection_kwargs = kwargs
        return self.connection

    def test_statements_bind_only_record_fields_and_commit_once_per_method(self) -> None:
        now = datetime(2099, 1, 2, 12, 34, tzinfo=UTC)
        snapshot = now.date()
        document = caselaw.NewDocument(
            "a" * 64, "caselaw", "fictional-dump", snapshot, "plain_text", now,
        )
        opinion = caselaw.NewOpinion(
            91, document.doc_id, "court1", 100, "23-A", snapshot, False,
            "published", "Published", ("1 Example 2", "3 Example 4"), "020",
        )
        self.connection.rows = [(
            91, document.doc_id, "processing", 0, "plain_text", None, None, False, False,
            False, None,
        )]
        section = caselaw.NewSection(
            "d" * 64, document.doc_id, 0, "footnote", "markup", 4, 9,
            "1", 2, None,
        )
        backfilled = replace(section, section_id="e" * 64)
        anchor = caselaw.NewAnchor(
            "f" * 64, document.doc_id, "reporter_page", "72", 4, 9,
            {"stars": "2", "scheme": "2"},
        )
        backfilled_anchor = replace(anchor, anchor_id="0" * 64)
        citation = citations.NewCitation(
            "1" * 64, document.doc_id, 0, 4, 9, section.section_id,
            section.section_type, "case_cite", "full", "1 U.S. 2",
            "1 U.S. 2", None, None, 100, "fictional-pattern@1",
        )
        backfilled_citation = replace(
            citation, citation_id="2" * 64, section_id=backfilled.section_id,
        )
        signal = treatment.NewSignal(
            "3" * 64, citation.citation_id, document.doc_id, "pattern",
            treatment.PATTERN_SET_ID, "overruled", "none", "majority",
            "negative", None, 1, 9,
        )
        backfilled_signal = replace(
            signal, signal_id="4" * 64, citation_id=backfilled_citation.citation_id,
        )
        record = caselaw.PsycopgRecord(connect=self.connect, worker_settings=self.configuration)
        self.assertEqual(record.present(document.source, snapshot, opinion.court), {
            91: caselaw.PresentRow(
                document.doc_id, "processing", 0, "plain_text", None, None, False, False,
                False, None,
            ),
        })
        record.begin(document, opinion)
        record.retry(document.doc_id)
        record.give_back(document.doc_id)
        record.finish_ready(
            document.doc_id, "b" * 64, "c" * 64, now, (section,), (anchor,), (citation,),
            (signal,),
        )
        record.write_sections(document.doc_id, (backfilled,))
        record.write_anchors(document.doc_id, (backfilled_anchor,), now)
        self.connection.rows = [astuple(section)]
        self.assertEqual(record.read_sections(document.doc_id), (section,))
        record.write_citations(document.doc_id, (backfilled_citation,), now)
        self.connection.rows = [astuple(citation)]
        self.assertEqual(record.read_citations(document.doc_id), (citation,))
        record.write_signals(document.doc_id, (backfilled_signal,), now)
        record.finish_failed(document.doc_id, "unparseable", now, "b" * 64)
        record.close()
        self.assertTrue(self.connection.closed)
        self.assertEqual(self.connection.commits, 12)
        self.assertEqual(self.connection.rollbacks, 0)
        self.assertEqual(self.connection_kwargs, {
            **settings.connection_kwargs(self.configuration), "autocommit": False,
        })
        self.assertEqual(self.connection.statements, [
            (caselaw.PRESENT_SQL, (document.source, snapshot, opinion.court)),
            (caselaw.BEGIN_DOCUMENT_SQL, (
                document.doc_id, document.profile, document.source, snapshot,
                document.text_source, now,
            )),
            (caselaw.BEGIN_OPINION_SQL, (
                opinion.opinion_id, opinion.doc_id, opinion.court, opinion.cluster_id,
                opinion.docket, opinion.decided_date, opinion.decided_date_is_approximate,
                opinion.precedential, opinion.precedential_raw,
                ["1 Example 2", "3 Example 4"], opinion.opinion_type,
            )),
            (caselaw.RETRY_SQL, (document.doc_id,)),
            (caselaw.GIVE_BACK_SQL, (document.doc_id,)),
            (caselaw.FINISH_READY_SQL, (
                "b" * 64, "c" * 64, now, now, now,
                treatment.PATTERN_SET_ID, now, document.doc_id,
            )),
            (caselaw.SECTION_SQL, (
                section.section_id, document.doc_id, 0, "footnote", "markup",
                4, 9, "1", 2, None,
            )),
            (caselaw.ANCHOR_SQL, (
                anchor.anchor_id, document.doc_id, "reporter_page", "72", 4, 9,
                '{"scheme": "2", "stars": "2"}',
            )),
            (caselaw.CITATION_SQL, (
                citation.citation_id, document.doc_id, citation.ordinal,
                citation.char_start, citation.char_end, citation.section_id,
                citation.section_type, citation.cite_type, citation.cite_form,
                citation.raw_cite, citation.reporter_cite, citation.pincite,
                citation.key, citation.to_cluster, citation.pattern_id,
            )),
            (caselaw.SIGNAL_SQL, (
                signal.signal_id, signal.citation_id, document.doc_id,
                signal.signal_source, signal.pattern_set, signal.treatment_signal,
                signal.qualifier, signal.effective_section, signal.state,
                signal.no_state_reason, signal.char_start, signal.char_end, now,
            )),
            (caselaw.SECTION_SQL, (
                backfilled.section_id, document.doc_id, 0, "footnote", "markup",
                4, 9, "1", 2, None,
            )),
            (caselaw.ANCHOR_SQL, (
                backfilled_anchor.anchor_id, document.doc_id, "reporter_page", "72", 4, 9,
                '{"scheme": "2", "stars": "2"}',
            )),
            (caselaw.ANCHORED_SQL, (now, document.doc_id)),
            (caselaw.READ_SECTIONS_SQL, (document.doc_id,)),
            (caselaw.CITATION_SQL, (
                backfilled_citation.citation_id, document.doc_id,
                backfilled_citation.ordinal, backfilled_citation.char_start,
                backfilled_citation.char_end, backfilled_citation.section_id,
                backfilled_citation.section_type, backfilled_citation.cite_type,
                backfilled_citation.cite_form, backfilled_citation.raw_cite,
                backfilled_citation.reporter_cite, backfilled_citation.pincite,
                backfilled_citation.key, backfilled_citation.to_cluster,
                backfilled_citation.pattern_id,
            )),
            (caselaw.MARK_CITED_SQL, (now, document.doc_id)),
            (caselaw.READ_CITATIONS_SQL, (document.doc_id,)),
            (caselaw.SIGNAL_SQL, (
                backfilled_signal.signal_id, backfilled_signal.citation_id,
                document.doc_id, backfilled_signal.signal_source,
                backfilled_signal.pattern_set, backfilled_signal.treatment_signal,
                backfilled_signal.qualifier, backfilled_signal.effective_section,
                backfilled_signal.state, backfilled_signal.no_state_reason,
                backfilled_signal.char_start, backfilled_signal.char_end, now,
            )),
            (caselaw.MARK_TREATED_SQL, (treatment.PATTERN_SET_ID, now, document.doc_id)),
            (caselaw.FINISH_FAILED_SQL, ("unparseable", "b" * 64, now, document.doc_id)),
        ])
        for statement, parameters in self.connection.statements:
            self.assertNotIn("PRIVATE_CASELAW_SENTINEL", statement)
            self.assertIn("%s", statement)
            self.assertIsInstance(parameters, tuple)
        self.assertIn("%s::jsonb", caselaw.ANCHOR_SQL)

    def test_psycopg_error_rolls_back_and_exposes_only_its_class(self) -> None:
        self.connection.fail_execute = True
        record = caselaw.PsycopgRecord(connect=self.connect, worker_settings=self.configuration)
        with self.assertRaises(caselaw.CaselawFailure) as raised:
            record.retry("a" * 64)
        self.assertEqual((raised.exception.reason, raised.exception.error),
                         ("database", "OperationalError"))
        self.assertNotIn("private database detail", str(raised.exception))
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 1))
        record.close()

    def test_connect_error_is_database_without_secret_text(self) -> None:
        def refuse(**_kwargs: object) -> _CapturedConnection:
            raise psycopg.OperationalError("private database detail")

        record = caselaw.PsycopgRecord(connect=refuse, worker_settings=self.configuration)
        with self.assertRaises(caselaw.CaselawFailure) as raised:
            record.present("fictional-dump", date(2099, 1, 2), "court1")
        self.assertEqual((raised.exception.reason, raised.exception.error),
                         ("database", "OperationalError"))
        self.assertNotIn("private database detail", str(raised.exception))

    def test_commit_and_close_errors_roll_back_with_class_only(self) -> None:
        self.connection.fail_commit = True
        record = caselaw.PsycopgRecord(connect=self.connect, worker_settings=self.configuration)
        with self.assertRaises(caselaw.CaselawFailure) as raised:
            record.retry("a" * 64)
        self.assertEqual((raised.exception.reason, raised.exception.error),
                         ("database", "OperationalError"))
        self.assertEqual(self.connection.rollbacks, 1)
        self.assertNotIn("private database detail", str(raised.exception))

        self.connection.fail_commit = False
        self.connection.fail_close = True
        with self.assertRaises(caselaw.CaselawFailure) as raised:
            record.close()
        self.assertEqual((raised.exception.reason, raised.exception.error),
                         ("database", "OperationalError"))
        self.assertEqual(self.connection.rollbacks, 2)
        self.assertNotIn("private database detail", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
