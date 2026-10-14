"""The known-overruled parse, resolution, job, and record over fictitious fixtures."""

import csv
import fcntl
import hashlib
import io
import json
import logging
import shutil
import stat
import tempfile
import unittest
from collections import Counter
from collections.abc import Iterable, Mapping, Set
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import psycopg
from test_worker_caselaw import _CapturedConnection

from gideon.host import fetch as host_fetch
from gideon.host.sysio import RealHost
from gideon.worker import fetch, logs, overruled, settings, staging

ROOT = Path(__file__).resolve().parent.parent


def table(*rows: str) -> str:
    return (
        '<table id="unrelated"><tbody><tr><td>ignored</td></tr></tbody></table>'
        '<table id="appendix2Table"><thead><tr><th>Order</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


def listed_row(
    order: int, decisions: str, years: str,
    *, ruling: str = "Fictional Vale v. Fable,<br>500 U.S. 10 (2000)",
    ruling_year: str = "2000",
) -> str:
    return (
        f"<tr><td>{order}</td><td>{ruling}</td><td>{ruling_year}</td>"
        f"<td>{decisions}</td><td>{years}</td></tr>"
    )


class Parse(unittest.TestCase):
    def test_cite_year_entities_and_transparent_markup(self) -> None:
        text = table(listed_row(
            7, '<em>Fictional Aster &amp; Co. v. Fable,</em><br>'
            '<a href="fictional"> 123 U.S. 45 (1920)</a>', "1920",
        ))
        self.assertEqual(overruled.parse_table(text), (overruled.ListedPair(
            7, 0,
            overruled.ListedDecision("Fictional Vale v. Fable", "500 U.S. 10", None, 2000, "none"),
            overruled.ListedDecision("Fictional Aster & Co. v. Fable", "123 U.S. 45", None, 1920, "none"),
        ),))

    def test_bare_names_and_ordered_years_are_kept_beside_cited_decisions(self) -> None:
        pairs = overruled.parse_table(table(listed_row(
            8, "Fictional First v. Fable,<br>Fictional Second, Inc. v. Fable,<br>"
            "198 U.S. 299 (1905);<br>Fictional Third v. Fable,<br>"
            "78 U.S. (11 Wall.) 423 (1870)", "1906<br><br>1905<br><br>1870",
        )))
        self.assertEqual(tuple((p.order, p.ordinal) for p in pairs), ((8, 0), (8, 1), (8, 2)))
        self.assertEqual(tuple(p.overruled for p in pairs), (
            overruled.ListedDecision("Fictional First v. Fable", None, None, 1906, "none"),
            overruled.ListedDecision("Fictional Second, Inc. v. Fable", "198 U.S. 299", None, 1905, "none"),
            overruled.ListedDecision("Fictional Third v. Fable", "78 U.S. 423", None, 1870, "none"),
        ))
        self.assertEqual(len({p.overruling for p in pairs}), 1)

    def test_nominative_cites_pincites_and_qualifiers(self) -> None:
        pairs = overruled.parse_table(table(listed_row(
            9, "Fictional Alder v. Fable,<br>78 U.S. (11 Wall.) 610 (1870);<br>"
            "Fictional Birch v. Fable,<br>68 U.S. (1 Wall.) 73, 77 (1864) (in part);<br>"
            "Fictional Cedar v. Fable,<br>67 U.S. (2 Black) 499 (1863)",
            "1870<br>1864<br>1863",
            ruling="Fictional Grove v. Fable,<br>85 U.S. (18 Wall.) 648 (1874) (in part)",
            ruling_year="1874",
        )))
        self.assertEqual(pairs[0].overruling.cite, "85 U.S. 648")
        self.assertEqual(pairs[0].overruling.qualifier, "in_part")
        self.assertEqual(tuple(p.overruled.cite for p in pairs), (
            "78 U.S. 610", "68 U.S. 73", "67 U.S. 499",
        ))
        self.assertEqual(tuple(p.overruled.qualifier for p in pairs), ("none", "in_part", "none"))

    def test_docket_and_date_form_and_names_with_commas(self) -> None:
        pairs = overruled.parse_table(table(listed_row(
            10, "Fictional Old Council v. Fable,", "1901",
            ruling="Fictional Council, Local No. 438 v. Fable,<br>No. 25-332 (U.S. June 29, 2026)",
            ruling_year="2026",
        )))
        self.assertEqual(pairs[0].overruling, overruled.ListedDecision(
            "Fictional Council, Local No. 438 v. Fable", None, "25-332", 2026, "none",
        ))
        self.assertEqual(pairs[0].overruled.cite, None)
        self.assertEqual(pairs[0].overruled.year, 1901)

    def test_first_identifier_ends_the_overruling_name(self) -> None:
        for identifiers in (
            "No. 20-123 (U.S. Apr. 20, 2000), 500 U.S. 10 (2000)",
            "500 U.S. 10 (2000), No. 20-123",
        ):
            with self.subTest(identifiers=identifiers):
                pair, = overruled.parse_table(table(listed_row(
                    1, "Fictional Old v. Fable,", "1900",
                    ruling=f"Fictional New v. Fable, {identifiers}",
                )))
                self.assertEqual(pair.overruling.name, "Fictional New v. Fable")
                self.assertEqual((pair.overruling.cite, pair.overruling.docket), ("500 U.S. 10", "20-123"))

    def test_multiline_names_nfc_and_self_closing_breaks(self) -> None:
        pair, = overruled.parse_table(table(listed_row(
            11, "Fictional Cafe\u0301<br/>Harbor Authority v. Fable<br/>"
            "100 U.S. 20 (1880)", "1880",
        )))
        self.assertEqual(pair.overruled.name, "Fictional Café Harbor Authority v. Fable")

    def test_disagreeing_year_counts_prefer_cite_year_then_single_column_year(self) -> None:
        decisions = (
            "Fictional Bare v. Fable,<br>Fictional Cited v. Fable,<br>"
            "123 U.S. 45 (1920);<br>Fictional Other v. Fable,"
        )
        for years, expected in (
            ("1940", (1940, 1920, 1940)),
            ("1940<br>1950", (None, 1920, None)),
            ("", (None, 1920, None)),
            ("1940<br>1950<br>1960", (1940, 1950, 1960)),
        ):
            with self.subTest(years=years):
                pairs = overruled.parse_table(table(listed_row(12, decisions, years)))
                self.assertEqual(tuple(p.overruled.year for p in pairs), expected)

    def test_missing_table_bad_columns_and_incomplete_rows_refuse_with_file_name(self) -> None:
        valid = listed_row(1, "Fictional Old v. Fable,", "1900")
        for text in (
            "<html>no table</html>",
            table("<tr><td>1</td><td>Fictional Short v. Fable</td></tr>"),
            table(valid.replace("</tr>", "<td>extra</td></tr>")),
            table(valid.replace("</tr>", "")),
            table(valid.replace("<td>1</td>", "<td>0</td>")),
            table(valid.replace("<td>2000</td>", "<td>unknown</td>")),
            table(listed_row(1, "", "")),
            table(valid) + table(valid),
        ):
            with self.subTest(text=text), self.assertRaises(overruled.OverruledFailure) as caught:
                overruled.parse_table(text)
            self.assertEqual(caught.exception.reason, "malformed")
            self.assertEqual(caught.exception.table, "decisions-overruled.html")
            self.assertNotIn("Fictional", str(caught.exception))

    def test_failure_vocabulary_is_closed(self) -> None:
        reasons = {
            "invalid", "missing-input", "input-mismatch", "missing-stage",
            "stage-mismatch", "malformed", "database", "local", "busy",
        }
        self.assertEqual(overruled.OVERRULED_FAILURE_REASONS, reasons)
        self.assertEqual(set(overruled.OVERRULED_RESOLUTIONS), {
            "cite", "name", "docket", "ambiguous", "absent",
        })
        self.assertEqual(set(overruled.QUALIFIERS), {"in_part", "on_other_grounds", "none"})
        for reason in reasons:
            failure = overruled.OverruledFailure(reason, table="fictional.csv", error="ValueError")
            self.assertEqual((failure.reason, failure.table, failure.error), (reason, "fictional.csv", "ValueError"))
        with self.assertRaises(ValueError):
            overruled.OverruledFailure("unrecognised")


class CaptureTripwire(unittest.TestCase):
    def test_committed_capture_counts_and_complete_pairs(self) -> None:
        text = (ROOT / "corpus/lockfiles/corpus-2026-10-14/treatment.decisions-overruled.html").read_text()
        pairs = overruled.parse_table(text)
        orders = Counter(pair.order for pair in pairs)
        self.assertEqual(set(orders), set(range(1, 238)))
        self.assertEqual(len(pairs), 293)
        self.assertEqual(sum(p.overruled.cite is not None for p in pairs), 193)
        self.assertEqual(sum(p.overruled.cite is None for p in pairs), 100)
        self.assertEqual(Counter(p.overruled.qualifier for p in pairs), {"none": 257, "in_part": 36})
        self.assertEqual(len({p.order for p in pairs if p.overruling.docket is not None}), 14)
        self.assertEqual(len({p.order for p in pairs if p.overruling.cite is not None}), 223)
        for pair in pairs:
            with self.subTest(order=pair.order, ordinal=pair.ordinal):
                self.assertTrue(pair.overruling.name)
                self.assertTrue(pair.overruled.name)
                self.assertIsNotNone(pair.overruling.year)
                self.assertIsNotNone(pair.overruled.year)
        for order, count in orders.items():
            self.assertEqual(tuple(p.ordinal for p in pairs if p.order == order), tuple(range(count)))


class Normalisation(unittest.TestCase):
    def test_quotes_ampersands_punctuation_case_and_whitespace(self) -> None:
        for text, expected in (
            ("Fictional ‘Aster’s’ & “Birch” Co., v. Fable", "fictional asters and birch co v fable"),
            ("FICTIONAL ASTER'S AND BIRCH CO. v. Fable", "fictional asters and birch co v fable"),
            ("  Fictional\nCafe\u0301\t v. Fable 42! ", "fictional café v fable 42"),
            ("Fictional A&B v. Fable", "fictional a and b v fable"),
            ("Fictional Straße v. Fable", "fictional strasse v fable"),
        ):
            with self.subTest(text=text):
                self.assertEqual(overruled.normal_name(text), expected)


class Resolution(unittest.TestCase):
    def setUp(self) -> None:
        self.pair = overruled.ListedPair(
            7, 0,
            overruled.ListedDecision("Fictional New v. Fable", "500 U.S. 10", "20-123", 2000, "none"),
            overruled.ListedDecision("Fictional Old v. Fable", "100 U.S. 20", None, 1880, "in_part"),
        )
        self.clusters = (
            overruled.ClusterFacts(11, frozenset({"500 U.S. 10"}), "20-123", 2000),
            overruled.ClusterFacts(22, frozenset({"100 U.S. 20"}), "80-456", 1880),
        )

    def resolve_fixture(
        self,
        clusters: Iterable[overruled.ClusterFacts],
        names: Mapping[tuple[str, int], Set[int]] | None = None,
        pairs: Iterable[overruled.ListedPair] | None = None,
        *, source: str = "treatment", source_snapshot: date = date(2040, 1, 2),
        caselaw_source: str = "fictional-caselaw", caselaw_snapshot: date = date(2040, 1, 3),
    ) -> tuple[overruled.OverruledRow, ...]:
        return overruled.resolve(
            (self.pair,) if pairs is None else pairs, clusters, names or {},
            source=source, source_snapshot=source_snapshot,
            caselaw_source=caselaw_source, caselaw_snapshot=caselaw_snapshot,
        )

    def test_record_cites_resolve_both_sides_and_preserve_every_column(self) -> None:
        row, = self.resolve_fixture(self.clusters)
        expected_id = hashlib.sha256(
            b"treatment\n2040-01-02\nfictional-caselaw\n2040-01-03\n7\n0",
        ).hexdigest()
        self.assertEqual(row, overruled.OverruledRow(
            expected_id, "treatment", date(2040, 1, 2), "fictional-caselaw", date(2040, 1, 3),
            7, 0, "Fictional New v. Fable", "500 U.S. 10", "20-123", 2000, 11, "cite",
            "Fictional Old v. Fable", "100 U.S. 20", 1880, 22, "cite", "in_part",
        ))
        self.assertNotIn("found_at", asdict(row))

    def test_parsed_identifiers_match_the_record_cite_and_dump_docket_forms(self) -> None:
        pairs = overruled.parse_table(table(listed_row(
            1, "Fictional Old v. Fable,<br>100 U.S. (2 Fable) 20, 21 (1880)", "1880",
            ruling="Fictional New v. Fable,<br>No. 20-123 (U.S. May 1, 2000)",
        )))
        row, = self.resolve_fixture(self.clusters, pairs=pairs)
        self.assertEqual((row.overruling_cluster, row.overruling_resolution), (11, "docket"))
        self.assertEqual((row.overruled_cluster, row.overruled_resolution), (22, "cite"))

    def test_name_fallback_is_exact_by_year_and_record_membership(self) -> None:
        key = ("fictional old v fable", 1880)
        for cite in (None, "999 U.S. 999"):
            with self.subTest(cite=cite):
                pair = replace(self.pair, overruled=replace(self.pair.overruled, cite=cite))
                row, = self.resolve_fixture(self.clusters, {key: {22, 999}}, (pair,))
                self.assertEqual((row.overruled_cluster, row.overruled_resolution), (22, "name"))
                row, = self.resolve_fixture(self.clusters, {(key[0], 1881): {22}}, (pair,))
                self.assertEqual((row.overruled_cluster, row.overruled_resolution), (None, "absent"))
                row, = self.resolve_fixture(self.clusters, {key: {999}}, (pair,))
                self.assertEqual((row.overruled_cluster, row.overruled_resolution), (None, "absent"))

    def test_name_key_uses_the_shared_normalisation(self) -> None:
        pair = replace(self.pair, overruled=replace(
            self.pair.overruled, name="FICTIONAL ‘Old’ & Co. v. Fable", cite=None,
        ))
        row, = self.resolve_fixture(self.clusters, {("fictional old and co v fable", 1880): {22}}, (pair,))
        self.assertEqual((row.overruled_cluster, row.overruled_resolution), (22, "name"))

    def test_cites_take_priority_over_names_and_dockets(self) -> None:
        clusters = (*self.clusters, overruled.ClusterFacts(33, frozenset(), "20-123", 2000))
        row, = self.resolve_fixture(clusters, {("fictional old v fable", 1880): {33}})
        self.assertEqual((row.overruling_cluster, row.overruling_resolution), (11, "cite"))
        self.assertEqual((row.overruled_cluster, row.overruled_resolution), (22, "cite"))

    def test_ambiguous_cites_do_not_fall_back_to_a_unique_name_or_docket(self) -> None:
        clusters = (*self.clusters, overruled.ClusterFacts(
            33, frozenset({"500 U.S. 10", "100 U.S. 20"}), None, 1880,
        ))
        row, = self.resolve_fixture(clusters, {("fictional old v fable", 1880): {22}})
        self.assertEqual((row.overruling_cluster, row.overruling_resolution), (None, "ambiguous"))
        self.assertEqual((row.overruled_cluster, row.overruled_resolution), (None, "ambiguous"))

    def test_ambiguous_name_or_docket_keeps_no_cluster(self) -> None:
        pair = replace(self.pair,
                       overruling=replace(self.pair.overruling, cite=None),
                       overruled=replace(self.pair.overruled, cite=None))
        clusters = (*self.clusters, overruled.ClusterFacts(33, frozenset(), "No. 20-123", 1880))
        row, = self.resolve_fixture(clusters, {("fictional old v fable", 1880): {22, 33}}, (pair,))
        self.assertEqual((row.overruling_cluster, row.overruling_resolution), (None, "ambiguous"))
        self.assertEqual((row.overruled_cluster, row.overruled_resolution), (None, "ambiguous"))

    def test_duplicate_facts_for_one_cluster_are_one_candidate(self) -> None:
        row, = self.resolve_fixture((*self.clusters, *self.clusters))
        self.assertEqual((row.overruling_cluster, row.overruled_cluster), (11, 22))

    def test_docket_fallback_ignores_prefix_and_whitespace_but_requires_the_whole_key(self) -> None:
        for cite in (None, "999 U.S. 999"):
            for docket in ("20-123", " No. 20-123 ", "20 - 123"):
                with self.subTest(cite=cite, docket=docket):
                    pair = replace(self.pair, overruling=replace(self.pair.overruling, cite=cite))
                    cluster = replace(self.clusters[0], docket=docket)
                    row, = self.resolve_fixture((cluster,), pairs=(pair,))
                    self.assertEqual((row.overruling_cluster, row.overruling_resolution), (11, "docket"))
        pair = replace(self.pair, overruling=replace(self.pair.overruling, cite=None, docket="No. 20 - 123"))
        row, = self.resolve_fixture(self.clusters, pairs=(pair,))
        self.assertEqual(row.overruling_cluster, 11)
        for unmatched_docket in ("120-123", "20-123, 20-124", "20-1234", None):
            row, = self.resolve_fixture((replace(self.clusters[0], docket=unmatched_docket),), pairs=(pair,))
            self.assertEqual((row.overruling_cluster, row.overruling_resolution), (None, "absent"))

    def test_yearless_decision_resolves_by_cite_alone(self) -> None:
        pair = replace(self.pair, overruled=replace(self.pair.overruled, year=None))
        row, = self.resolve_fixture(self.clusters, pairs=(pair,))
        self.assertEqual((row.overruled_cluster, row.overruled_resolution), (22, "cite"))
        pair = replace(pair, overruled=replace(pair.overruled, cite=None))
        row, = self.resolve_fixture(self.clusters, {("fictional old v fable", 1880): {22}}, (pair,))
        self.assertEqual((row.overruled_cluster, row.overruled_resolution), (None, "absent"))

    def test_overruling_does_not_resolve_by_name_or_overruled_by_docket(self) -> None:
        pair = replace(self.pair,
                       overruling=replace(self.pair.overruling, cite=None, docket=None),
                       overruled=replace(self.pair.overruled, cite=None, docket="80-456"))
        row, = self.resolve_fixture(self.clusters, {("fictional new v fable", 2000): {11}}, (pair,))
        self.assertEqual((row.overruling_resolution, row.overruled_resolution), ("absent", "absent"))

    def test_missing_identifiers_never_match_an_empty_record_key(self) -> None:
        pair = replace(self.pair,
                       overruling=replace(self.pair.overruling, cite=None, docket=None),
                       overruled=replace(self.pair.overruled, cite=None))
        cluster = overruled.ClusterFacts(11, frozenset({""}), " ", 2000)
        row, = self.resolve_fixture((cluster,), pairs=(pair,))
        self.assertEqual((row.overruling_resolution, row.overruled_resolution), ("absent", "absent"))

    def test_all_pairs_are_retained_in_order_even_when_absent(self) -> None:
        pairs = (self.pair, replace(self.pair, ordinal=1), replace(self.pair, order=8))
        rows = self.resolve_fixture((), pairs=pairs)
        self.assertEqual(tuple((r.list_order, r.ordinal) for r in rows), ((7, 0), (7, 1), (8, 0)))
        self.assertTrue(all(r.overruled_resolution == r.overruling_resolution == "absent" for r in rows))
        self.assertEqual(len({r.row_id for r in rows}), len(pairs))
        self.assertEqual(self.resolve_fixture(self.clusters, pairs=()), ())

    def test_identity_changes_with_either_source_or_snapshot_but_not_resolution(self) -> None:
        row, = self.resolve_fixture(self.clusters)
        absent, = self.resolve_fixture(())
        self.assertEqual(row.row_id, absent.row_id)
        variants = (
            self.resolve_fixture(self.clusters, source="fictional-treatment"),
            self.resolve_fixture(self.clusters, source_snapshot=date(2040, 2, 2)),
            self.resolve_fixture(self.clusters, caselaw_source="fictional-other-caselaw"),
            self.resolve_fixture(self.clusters, caselaw_snapshot=date(2040, 2, 3)),
        )
        self.assertEqual(len({row.row_id, *(rows[0].row_id for rows in variants)}), 5)
        self.assertEqual(self.resolve_fixture(reversed(self.clusters)), (row,))


class FakeRecord:
    """Hold cluster facts and immutable rows across repeated job calls."""

    def __init__(self) -> None:
        self.clusters = (
            overruled.ClusterFacts(11, frozenset({"500 U.S. 10"}), "20-123", 2000),
            overruled.ClusterFacts(22, frozenset({"100 U.S. 20"}), None, 1880),
            *(overruled.ClusterFacts(cluster, frozenset(), None, 1881 + cluster - 23)
              for cluster in range(23, 28)),
        )
        self.rows: dict[str, overruled.OverruledRow] = {}
        self.reads: list[tuple[str, date, str]] = []
        self.writes: list[tuple[tuple[overruled.OverruledRow, ...], datetime]] = []
        self.closed = 0

    def scotus_clusters(self, source: str, snapshot_date: date, court: str) -> tuple[overruled.ClusterFacts, ...]:
        self.reads.append((source, snapshot_date, court))
        return self.clusters

    def write_rows(self, rows: tuple[overruled.OverruledRow, ...], found_at: datetime) -> tuple[int, int]:
        self.writes.append((rows, found_at))
        inserted = 0
        for row in rows:
            if row.row_id not in self.rows:
                self.rows[row.row_id] = row
                inserted += 1
        return inserted, len(rows) - inserted

    def close(self) -> None:
        self.closed += 1


class Job(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.snapshots_root = self.root / "snapshots"
        self.work_root = self.root / "work"
        self.snapshots_root.mkdir()
        self.work_root.mkdir()
        self.label = "corpus-2099-01-03"
        self.snapshot = "treatment-2099-01-01"
        self.caselaw_snapshot = "fictional-dump-2099-01-02"
        self.court = "scotus"
        self.job = 42
        self.instant = datetime(2099, 1, 3, 12, 34, tzinfo=UTC)
        self.dump = self.snapshots_root / self.snapshot
        self.dump.mkdir()
        self.path = self.dump / "decisions-overruled.html"
        self.whole = self.work_root / self.label / "treatment"
        self.failure_path = self.whole / "overruled-failed.json"
        self.stage_dir = self.work_root / self.label / "fictional-dump"
        (self.stage_dir / self.court).mkdir(parents=True)
        self.stage_path = self.stage_dir / "stage.json"
        self.names_path = self.stage_dir / self.court / "opinion-clusters.csv"
        self.sentinel = "PRIVATE_OVERRULED_SENTINEL"
        self.capture = table(
            listed_row(1, f"Fictional {self.sentinel} Old v. Fable,<br>"
                       "100 U.S. 20 (1880) (in part)", "1880"),
            listed_row(2, "Fictional Alias Short v. Fable,", "1881",
                       ruling="Fictional New v. Fable, No. 20-123 (U.S. May 1, 2000)"),
            listed_row(3, "Fictional Alias Full v. Fable,", "1882"),
            listed_row(4, "Fictional Alias Main v. Fable,", "1883"),
            listed_row(5, "Fictional Shared v. Fable,<br>Fictional Stage Only v. Fable,",
                       "1884<br>1885", ruling="Fictional Missing v. Fable, 999 U.S. 999 (2000)"),
        )
        self.seed(self.capture.encode())
        self.staged = staging.StageRecord(
            self.label, "fictional-dump", self.caselaw_snapshot, [self.court],
            {name: {"path": f"{name}.csv.bz2", "sha256": "a" * 64, "size": 0}
             for name in staging.STAGE_TABLES},
            dict.fromkeys(staging.STAGE_TABLES, 0),
            {self.court: dict.fromkeys(staging.STAGE_TABLES[1:], 0)},
            7, 0.0, self.instant.isoformat(),
        )
        staging.write_record(self.stage_path, self.staged)
        self.write_names([
            (23, "Fictional Unused v. Fable", "Fictional Alias Short v. Fable", "", "1881-01-01"),
            (24, "", "", "Fictional Alias Full v. Fable", "1882-01-01"),
            (25, "Fictional Alias Main v. Fable", "", "", "1883-01-01"),
            (26, "Fictional Shared v. Fable", "", "", "1884-01-01"),
            (27, "Fictional Shared v. Fable", "", "", "1884-01-01"),
            (999, "Fictional Stage Only v. Fable", "", "", "1885-01-01"),
        ])
        self.record = FakeRecord()

    def seed(self, body: bytes) -> None:
        self.path.write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()
        self.input = {"path": self.path.name, "sha256": digest}
        host_fetch.write_seed_record(
            RealHost(), self.snapshots_root, f"{self.snapshot}/{self.path.name}",
            url="https://archive.example.test/fictional-overruled/", size=len(body),
            sha256=digest, fetched_at=self.instant.isoformat(),
        )

    def write_names(self, rows: list[tuple[object, ...]]) -> None:
        with self.names_path.open("w", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(("id", "case_name", "case_name_short", "case_name_full", "date_filed"))
            writer.writerows(rows)

    def run_job(self, record: overruled.OverruledRecord | None = None, **changes: Any) -> overruled.OverruledCounts:
        arguments: dict[str, Any] = {
            "snapshots_root": self.snapshots_root, "work_root": self.work_root,
            "label": self.label, "snapshot": self.snapshot, "caselaw_snapshot": self.caselaw_snapshot,
            "court": self.court, "input": self.input, "job": self.job,
            "record": self.record if record is None else record,
            "clock": lambda: self.instant, "monotonic": lambda: 10.0,
        }
        arguments.update(changes)
        return overruled.overruled(**arguments)

    def assert_filed(self, failure: overruled.OverruledFailure) -> None:
        overruled.write_job_failure(
            self.work_root, self.label, self.snapshot, self.court, self.job, failure,
            clock=lambda: self.instant,
        )
        self.assertEqual(json.loads(self.failure_path.read_text()), {
            "schema": 1, "job": self.job, "court": self.court,
            "reason": failure.reason, "table": failure.table, "error": failure.error,
            "at": self.instant.isoformat(),
        })
        self.assertEqual(stat.S_IMODE(self.failure_path.stat().st_mode), fetch.PARTIAL_MODE)

    def test_seeded_capture_records_every_pair_and_only_a_lock_on_disk(self) -> None:
        seed_record = fetch.read_record(self.path.with_name(self.path.name + fetch.RECORD_SUFFIX))
        assert seed_record is not None
        self.assertEqual((seed_record.form, seed_record.job), ("seeded", None))
        counts = self.run_job()
        self.assertEqual(counts, overruled.OverruledCounts(
            5, 6, {"cite": 1, "name": 3, "ambiguous": 1, "absent": 1},
            {"cite": 3, "docket": 1, "ambiguous": 0, "absent": 2}, 6, 0, 0.0,
        ))
        self.assertEqual(self.record.reads, [("fictional-dump", date(2099, 1, 2), "scotus")])
        self.assertEqual(self.record.closed, 1)
        self.assertEqual(len(self.record.writes), 1)
        self.assertEqual(self.record.writes[0][1], self.instant)
        rows = tuple(self.record.rows.values())
        self.assertEqual(tuple((r.list_order, r.ordinal, r.overruled_cluster, r.overruled_resolution) for r in rows), (
            (1, 0, 22, "cite"), (2, 0, 23, "name"), (3, 0, 24, "name"), (4, 0, 25, "name"),
            (5, 0, None, "ambiguous"), (5, 1, None, "absent"),
        ))
        self.assertEqual(rows[0].qualifier, "in_part")
        self.assertIn(self.sentinel, rows[0].overruled_name)
        for row in rows:
            self.assertEqual((row.source, row.source_snapshot, row.caselaw_source, row.caselaw_snapshot),
                             ("treatment", date(2099, 1, 1), "fictional-dump", date(2099, 1, 2)))
        self.assertEqual({p.name for p in self.whole.iterdir()}, {"overruled.lock"})
        self.assertEqual(stat.S_IMODE(self.whole.stat().st_mode), fetch.DIR_MODE)

    def test_replay_is_present_only_and_a_restored_record_is_refilled(self) -> None:
        self.run_job()
        original = dict(self.record.rows)
        self.failure_path.write_text("stale fictional failure")
        second = self.run_job()
        self.assertEqual((second.inserted, second.present), (0, len(original)))
        self.assertFalse(self.failure_path.exists())
        self.assertEqual(self.record.rows, original)
        self.record.rows.clear()
        restored = self.run_job()
        self.assertEqual((restored.inserted, restored.present), (len(original), 0))
        self.assertEqual(self.record.rows, original)
        self.assertEqual(self.record.closed, 3)

    def test_invalid_grammars_touch_no_files(self) -> None:
        before = set(self.root.rglob("*"))
        for changes in (
            {"label": "../bad"}, {"label": "corpus-2099-02-30"},
            {"snapshot": "treatment-2099-02-30"}, {"snapshot": "other-2099-01-01"},
            {"caselaw_snapshot": "../bad"}, {"caselaw_snapshot": "fictional-dump-2099-02-30"},
            {"court": "../scotus"}, {"court": "ca1"}, {"job": True}, {"job": 0},
            {"input": {"path": "../capture", "sha256": "a" * 64}},
            {"input": {"path": self.path.name, "sha256": "bad"}},
            {"input": {**self.input, "extra": "forbidden"}},
        ):
            with self.subTest(changes=changes), self.assertRaises(overruled.OverruledFailure) as raised:
                self.run_job(**changes)
            self.assertEqual(raised.exception.reason, "invalid")
            self.assertEqual(set(self.root.rglob("*")), before)
        self.assertEqual((self.record.reads, self.record.writes), ([], []))
        self.whole.mkdir()
        with self.assertRaises(overruled.OverruledFailure) as raised:
            self.run_job(job=0)
        self.assert_filed(raised.exception)

    def test_input_absence_and_digest_or_size_mismatch_are_filed(self) -> None:
        record_path = self.path.with_name(self.path.name + fetch.RECORD_SUFFIX)
        for absent in (self.path, record_path):
            saved = absent.read_bytes()
            absent.unlink()
            with self.subTest(absent=absent.name), self.assertRaises(overruled.OverruledFailure) as raised:
                self.run_job()
            self.assertEqual(raised.exception.reason, "missing-input")
            self.assert_filed(raised.exception)
            absent.write_bytes(saved)
        with self.assertRaises(overruled.OverruledFailure) as raised:
            self.run_job(input={**self.input, "sha256": "0" * 64})
        self.assertEqual(raised.exception.reason, "input-mismatch")
        self.assert_filed(raised.exception)
        self.path.write_bytes(b"short")
        with self.assertRaises(overruled.OverruledFailure) as raised:
            self.run_job()
        self.assertEqual(raised.exception.reason, "input-mismatch")
        self.assert_filed(raised.exception)

    def test_missing_or_disagreeing_stage_is_filed(self) -> None:
        self.stage_path.unlink()
        with self.assertRaises(overruled.OverruledFailure) as raised:
            self.run_job()
        self.assertEqual(raised.exception.reason, "missing-stage")
        self.assert_filed(raised.exception)
        for changes in (
            {"label": "corpus-2099-01-04"}, {"snapshot": "fictional-dump-2099-01-04"},
            {"courts": ["ca1"], "counts": {"ca1": dict.fromkeys(staging.STAGE_TABLES[1:], 0)}},
        ):
            staging.write_record(self.stage_path, replace(self.staged, **changes))
            with self.subTest(changes=changes), self.assertRaises(overruled.OverruledFailure) as raised:
                self.run_job()
            self.assertEqual(raised.exception.reason, "stage-mismatch")
            self.assert_filed(raised.exception)

    def test_malformed_capture_and_staged_rows_name_their_input(self) -> None:
        for body in (b"no table", b"\xff", table("<tr><td>short row</td></tr>").encode()):
            self.seed(body)
            with self.subTest(body=body), self.assertRaises(overruled.OverruledFailure) as raised:
                self.run_job()
            self.assertEqual((raised.exception.reason, raised.exception.table),
                             ("malformed", "decisions-overruled.html"))
            self.assert_filed(raised.exception)
        self.seed(self.capture.encode())
        for csv_text in (
            "wrong,columns\n1,2\n", "id,case_name,case_name_short,case_name_full,date_filed\nshort\n",
            "id,case_name,case_name_short,case_name_full,date_filed\n0,Fictional,,,1880-01-01\n",
            "id,case_name,case_name_short,case_name_full,date_filed\n1,Fictional,,,bad-date\n",
            'id,case_name,case_name_short,case_name_full,date_filed\n"unterminated\n',
        ):
            self.names_path.write_text(csv_text)
            with self.subTest(csv_text=csv_text), self.assertRaises(overruled.OverruledFailure) as raised:
                self.run_job()
            self.assertEqual((raised.exception.reason, raised.exception.table), ("malformed", "opinion-clusters"))
            self.assert_filed(raised.exception)

    def test_missing_staged_file_or_court_is_a_stage_mismatch(self) -> None:
        self.names_path.unlink()
        with self.assertRaises(overruled.OverruledFailure) as raised:
            self.run_job()
        self.assertEqual((raised.exception.reason, raised.exception.table), ("stage-mismatch", "opinion-clusters"))
        self.assert_filed(raised.exception)
        shutil.rmtree(self.names_path.parent)
        with self.assertRaises(overruled.OverruledFailure) as raised:
            self.run_job()
        self.assertEqual(raised.exception.reason, "stage-mismatch")
        self.assert_filed(raised.exception)

    def test_database_refusals_close_and_preserve_the_original_failure(self) -> None:
        for method in ("scotus_clusters", "write_rows"):
            failure = overruled.OverruledFailure("database", error="OperationalError")
            with (self.subTest(method=method), patch.object(self.record, method, side_effect=failure),
                  patch.object(self.record, "close", side_effect=RuntimeError(self.sentinel)) as close,
                  self.assertRaises(overruled.OverruledFailure) as raised):
                self.run_job()
            self.assertIs(raised.exception, failure)
            close.assert_called_once_with()
            self.assert_filed(failure)

    def test_local_filesystem_failure_is_filed_with_its_class_alone(self) -> None:
        with (patch.object(overruled, "parse_table", side_effect=OSError(self.sentinel)),
              self.assertRaises(overruled.OverruledFailure) as raised):
            self.run_job()
        self.assertEqual((raised.exception.reason, raised.exception.error), ("local", "OSError"))
        self.assert_filed(raised.exception)
        self.assertNotIn(self.sentinel, self.failure_path.read_text())

    def test_busy_preserves_an_existing_failure_file(self) -> None:
        self.whole.mkdir()
        self.failure_path.write_text("existing fictional failure")
        with (self.whole / "overruled.lock").open("w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                with self.assertRaises(overruled.OverruledFailure) as raised:
                    self.run_job()
                self.assertEqual(raised.exception.reason, "busy")
                overruled.write_job_failure(
                    self.work_root, self.label, self.snapshot, self.court, self.job, raised.exception,
                )
                self.assertEqual(self.failure_path.read_text(), "existing fictional failure")
                self.assertEqual(self.record.rows, {})
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def test_links_at_input_work_and_stage_paths_are_refused_without_following(self) -> None:
        self.whole.mkdir()
        lock_path = self.whole / "overruled.lock"
        lock_path.touch()
        for path, expected in (
            (self.path, "invalid"), (self.path.with_name(self.path.name + fetch.RECORD_SUFFIX), "invalid"),
            (self.dump, "invalid"), (self.whole, "invalid"),
            (lock_path, "invalid"), (self.failure_path, "invalid"),
            (self.stage_dir, "stage-mismatch"), (self.stage_path, "stage-mismatch"),
            (self.names_path.parent, "stage-mismatch"), (self.names_path, "stage-mismatch"),
        ):
            with self.subTest(path=path):
                displaced = self.root / "displaced"
                existed = path.exists()
                if existed:
                    path.rename(displaced)
                else:
                    displaced.write_text("fictional target")
                before = displaced.read_bytes() if displaced.is_file() else None
                path.symlink_to(displaced, target_is_directory=displaced.is_dir())
                try:
                    with self.assertRaises(overruled.OverruledFailure) as raised:
                        self.run_job()
                    self.assertEqual(raised.exception.reason, expected)
                    if before is not None:
                        self.assertEqual(displaced.read_bytes(), before)
                finally:
                    path.unlink()
                    if existed:
                        displaced.rename(path)
                    else:
                        displaced.unlink()

    def test_failure_writer_never_creates_work_or_follows_a_failure_link(self) -> None:
        failure = overruled.OverruledFailure("malformed", table="decisions-overruled.html")
        overruled.write_job_failure(self.work_root, self.label, self.snapshot, self.court, self.job, failure)
        self.assertFalse(self.whole.exists())
        self.whole.mkdir()
        target = self.root / "untouched"
        target.write_text("fictional target")
        self.failure_path.symlink_to(target)
        overruled.write_job_failure(self.work_root, self.label, self.snapshot, self.court, self.job, failure)
        self.assertEqual(target.read_text(), "fictional target")

    def test_real_log_handler_excludes_capture_and_failure_text(self) -> None:
        output = io.StringIO()
        root_logger = logging.getLogger()
        handlers, level = list(root_logger.handlers), root_logger.level
        handler = logs.install_handler(output)
        ticks = iter(range(0, 10000, overruled.PROGRESS_INTERVAL_SECONDS + 1))
        try:
            self.run_job(monotonic=lambda: float(next(ticks)))
            with self.assertRaises(overruled.OverruledFailure) as raised:
                self.run_job(label=self.sentinel)
            self.assertEqual(raised.exception.reason, "invalid")
            with (patch.object(overruled, "parse_table", side_effect=OSError(self.sentinel)),
                  self.assertRaises(overruled.OverruledFailure) as raised):
                self.run_job()
            self.assert_filed(raised.exception)
        finally:
            root_logger.handlers[:] = handlers
            root_logger.setLevel(level)
            handler.close()
        text = output.getvalue()
        for action in ("overruled_progress", "overruled_end", "overruled_failed"):
            self.assertIn(f"action={action}", text)
        self.assertNotIn(self.sentinel, text)
        self.assertNotIn("Fictional", text)
        self.assertNotIn(self.sentinel, self.failure_path.read_text())


class CapturedConnection(_CapturedConnection):
    """Add insertion row counts and a chosen failure to the shared SQL capture."""

    def __init__(self) -> None:
        super().__init__()
        self.rowcount = 1
        self.rowcounts: list[int] = []
        self.fail_at: int | None = None

    def execute(self, statement: str, parameters: tuple[object, ...]) -> None:
        super().execute(statement, parameters)
        if self.fail_at == len(self.statements):
            raise psycopg.OperationalError("fictional private database detail")
        self.rowcount = self.rowcounts.pop(0) if self.rowcounts else 1


def record_row(ordinal: int = 0) -> overruled.OverruledRow:
    return overruled.OverruledRow(
        hashlib.sha256(f"fictional-row-{ordinal}".encode()).hexdigest(),
        "treatment", date(2099, 1, 1), "fictional-dump", date(2099, 1, 2),
        7, ordinal, "Fictional New v. Fable", "500 U.S. 10", None, 2000, 11, "cite",
        "Fictional Old v. Fable", None, 1880, None, "absent", "none",
    )


class PsycopgRecord(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.password = Path(temporary.name) / "password"
        self.password.write_text("fictional-secret")
        self.configuration = settings.Settings(
            "db.example.test", 5432, "gideon", "gideon_worker", self.password, 2,
            "http://store.example.test:9200", "example-user", self.password,
            "http://vectors.example.test:6333", self.password,
            "http://embed.example.test:8000", self.password,
        )
        self.connection = CapturedConnection()
        self.record = overruled.PsycopgRecord(
            connect=lambda **_kwargs: self.connection, worker_settings=self.configuration,
        )
        self.instant = datetime(2099, 1, 3, tzinfo=UTC)

    def test_cluster_query_binds_snapshot_and_court_and_folds_all_opinions(self) -> None:
        self.connection.rows = [
            (11, ["500 U.S. 10"], "20-123", 2000),
            (11, ["500 U.S. 10", "1 Fiction 2"], "20-123", 2000),
            (22, [], None, None),
        ]
        self.assertEqual(self.record.scotus_clusters("fictional-dump", date(2099, 1, 2), "scotus"), (
            overruled.ClusterFacts(11, frozenset({"500 U.S. 10", "1 Fiction 2"}), "20-123", 2000),
            overruled.ClusterFacts(22, frozenset(), None, None),
        ))
        self.assertEqual(self.connection.statements, [
            (overruled.SCOTUS_CLUSTERS_SQL, ("fictional-dump", date(2099, 1, 2), "scotus")),
        ])
        for clause in (
            "JOIN documents AS d ON d.doc_id = o.doc_id",
            "d.source = %s", "d.source_snapshot = %s", "o.court = %s",
            "o.reporter_cites", "o.docket", "EXTRACT(YEAR FROM o.decided_date)",
            "ORDER BY o.cluster_id, o.opinion_id",
        ):
            self.assertIn(clause, overruled.SCOTUS_CLUSTERS_SQL)
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (1, 0))
        self.record.close()
        self.assertTrue(self.connection.closed)

    def test_insert_binds_all_row_fields_and_counts_conflicts_in_one_transaction(self) -> None:
        rows = (record_row(), record_row(1))
        self.connection.rowcounts = [1, 0]
        self.assertEqual(self.record.write_rows(rows, self.instant), (1, 1))
        expected = tuple((
            row.row_id, "treatment", date(2099, 1, 1), "fictional-dump", date(2099, 1, 2),
            7, ordinal, "Fictional New v. Fable", "500 U.S. 10", None, 2000, 11, "cite",
            "Fictional Old v. Fable", None, 1880, None, "absent", "none", self.instant,
        ) for ordinal, row in enumerate(rows))
        self.assertEqual(self.connection.statements, [
            (overruled.INSERT_ROW_SQL, parameters) for parameters in expected
        ])
        self.assertTrue(overruled.INSERT_ROW_SQL.startswith("INSERT INTO known_overruled ("))
        columns = overruled.INSERT_ROW_SQL.split("(", 1)[1].split(")", 1)[0]
        self.assertEqual(tuple(columns.split(", ")), (
            "row_id", "source", "source_snapshot", "caselaw_source", "caselaw_snapshot",
            "list_order", "ordinal", "overruling_name", "overruling_cite", "overruling_docket",
            "overruling_year", "overruling_cluster", "overruling_resolution", "overruled_name",
            "overruled_cite", "overruled_year", "overruled_cluster", "overruled_resolution",
            "qualifier", "found_at",
        ))
        self.assertIn("ON CONFLICT (row_id) DO NOTHING", overruled.INSERT_ROW_SQL)
        self.assertEqual(overruled.INSERT_ROW_SQL.count("%s"), len(expected[0]))
        self.assertNotIn("Fictional", overruled.INSERT_ROW_SQL)
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (1, 0))

    def test_read_driver_error_rolls_back_with_class_only(self) -> None:
        self.connection.fail_execute = True
        with self.assertRaises(overruled.OverruledFailure) as raised:
            self.record.scotus_clusters("fictional-dump", date(2099, 1, 2), "scotus")
        self.assertEqual((raised.exception.reason, raised.exception.error), ("database", "OperationalError"))
        self.assertNotIn("private", str(raised.exception))
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 1))

    def test_insert_or_commit_failure_rolls_back_the_whole_pass(self) -> None:
        for fail_commit in (False, True):
            with self.subTest(fail_commit=fail_commit):
                connection = CapturedConnection()
                connection.fail_commit = fail_commit
                connection.fail_at = None if fail_commit else 2
                record = overruled.PsycopgRecord(
                    connect=lambda connection=connection, **_kwargs: connection,
                    worker_settings=self.configuration,
                )
                with self.assertRaises(overruled.OverruledFailure) as raised:
                    record.write_rows((record_row(), record_row(1)), self.instant)
                self.assertEqual((raised.exception.reason, raised.exception.error), ("database", "OperationalError"))
                self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
                record.close()
                self.assertTrue(connection.closed)

    def test_connection_settings_and_close_failures_keep_exception_classes_only(self) -> None:
        with (patch.object(self.record, "_connect", side_effect=psycopg.OperationalError("private")),
              self.assertRaises(overruled.OverruledFailure) as raised):
            self.record.scotus_clusters("fictional-dump", date(2099, 1, 2), "scotus")
        self.assertEqual((raised.exception.reason, raised.exception.error), ("database", "OperationalError"))
        self.password.unlink()
        with self.assertRaises(overruled.OverruledFailure) as raised:
            self.record.write_rows((record_row(),), self.instant)
        self.assertEqual((raised.exception.reason, raised.exception.error), ("local", "WorkerSettingsError"))
        self.assertEqual(self.connection.statements, [])
        self.password.write_text("fictional-secret")
        self.record.scotus_clusters("fictional-dump", date(2099, 1, 2), "scotus")
        self.connection.fail_close = True
        with self.assertRaises(overruled.OverruledFailure) as raised:
            self.record.close()
        self.assertEqual((raised.exception.reason, raised.exception.error), ("database", "OperationalError"))
        self.assertEqual(self.connection.rollbacks, 1)

    def test_closing_an_unopened_record_never_connects(self) -> None:
        with patch.object(self.record, "_connect") as connect:
            self.record.close()
        connect.assert_not_called()
