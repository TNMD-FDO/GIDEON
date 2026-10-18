"""The corpus inventory reads only the record through a fake host."""

import argparse
import contextlib
import io
import json
import subprocess
import unittest
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace

import test_index_record
import test_index_report
import test_index_treatment

from gideon.host import caselaw, report, worker
from gideon.host.corpus.record import CutRow, SourceBinding, database_fix
from gideon.host.index import buildreport, generations, inventory
from gideon.host.index.record import GenerationRow
from gideon.host.render.worker import PRECEDENTIAL_VALUES, TEXT_SOURCES
from gideon.host.sysio import Command, PathLike

RENDERED = test_index_record.RENDERED


def lockfile_for() -> CutRow:
    row = test_index_record.row_for()
    return CutRow(
        row.corpus_lockfile, 1, row.pipeline, "2099-01-03T10:00:00+00:00",
        "quarterly", None, None, "cut",
        (SourceBinding("example", "2099-01-02"), SourceBinding("another-example", "2099-01-01")),
    )


def live_for() -> GenerationRow:
    row = test_index_record.row_for()
    return replace(row, state="live", dense_built_at=row.lexical_built_at,
                   built_at=row.lexical_built_at, promoted_at="2099-01-03T13:00:00+00:00",
                   counts={**row.counts, "present": 2})


class FakeHost(test_index_record.FakeHost):
    """Answer record reads and reject every other subprocess."""

    def __init__(self) -> None:
        super().__init__()
        self.euid = 0
        self.record_rows: dict[str, CutRow] = {}
        self.generation_rows: tuple[GenerationRow, ...] = ()
        self.reports: dict[str, buildreport.BuildReport] = {}
        self.fail_read: str | None = None
        self.outputs: dict[str, str] = {}

    def geteuid(self) -> int:
        return self.euid

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, cwd, timeout, passthrough
        assert env is None
        command = list(argv)
        assert command == worker.psql_argv(RENDERED)
        assert input is not None
        self.calls.append((command, input))
        if "FROM public.corpus_lockfiles" in input:
            kind = "lockfiles"
            rows = [asdict(row) for _, row in sorted(self.record_rows.items())]
        elif "FROM public.index_generations" in input:
            kind = "generations"
            rows = [asdict(row) for row in sorted(
                self.generation_rows, key=lambda row: (row.collection, row.created_at, row.gen_id),
            )]
        elif "FROM public.build_reports" in input:
            kind = "report"
            name = next(line for line in input.splitlines() if line.startswith("\\set v_name ")).split("'", 2)[1]
            build_report = self.reports.get(name)
            rows = [build_report.to_json()] if build_report is not None else []
        else:
            raise AssertionError(f"Unexpected statement: {input}")
        stdout = self.outputs.get(kind, "".join(json.dumps(row) + "\n" for row in rows))
        if kind == self.fail_read:
            if self.error is not None:
                raise self.error
            return subprocess.CompletedProcess(command, self.code, stdout, "private text sentinel")
        return subprocess.CompletedProcess(command, 0, stdout, "")


def run_report(
    host: FakeHost, name: str | None = None,
    *, collections: Sequence[generations.Collection] = generations.COLLECTIONS,
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = inventory.run_index_report(
            argparse.Namespace(generation=name), host=host, rendered_dir=RENDERED,
            collections=collections,
        )
    return code, out.getvalue(), err.getvalue()


class Inventory(unittest.TestCase):
    def test_root_refusal_precedes_any_read(self) -> None:
        host = FakeHost()
        host.euid = 1000
        code, out, err = run_report(host)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(err, report.refusal(
            "index report", "root privileges are required.", f"Run {report.command('index report')}.",
        ) + "\n")
        self.assertEqual(host.calls, [])

    def test_empty_record_prints_none_and_each_collection_without_a_live_generation(self) -> None:
        host = FakeHost()
        code, out, err = run_report(host)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out.splitlines(), [
            "lockfiles", "none", "generations", "none",
            *(f"inventory {item.name}: no live generation; name a generation to report it"
              for item in generations.COLLECTIONS),
        ])
        self.assertEqual(len(host.calls), 2)
        self.assertIn("FROM public.corpus_lockfiles", host.calls[0][1] or "")
        self.assertIn("FROM public.index_generations", host.calls[1][1] or "")

    def test_bare_listing_formats_lockfiles_and_generations_and_selects_live_subjects(self) -> None:
        host = FakeHost()
        cut = lockfile_for()
        installing = replace(cut, label="corpus-2099-01-04", state="installing", base=cut.label)
        installed = replace(cut, label="corpus-2099-01-05", state="installed",
                            installed_at="2099-01-05T10:00:00+00:00")
        superseded = replace(cut, label="corpus-2099-01-01", state="superseded")
        host.record_rows = {row.label: row for row in (installed, superseded, installing, cut)}
        live = live_for()
        building = replace(test_index_record.row_for(),
                           gen_id=generations.generation_name(live.collection, installing.label, live.embedding_space),
                           corpus_lockfile=installing.label, lockfiles=(cut.label, installing.label),
                           created_at="2099-01-04T11:00:00+00:00")
        host.generation_rows = (building, live)
        other = generations.Collection("example-collection", "example-profile", lambda _: False)
        collections = (*generations.COLLECTIONS, other)
        code, out, err = run_report(host, collections=collections)
        self.assertEqual((code, err), (0, ""))
        sources = ", ".join(f"{pin.source} {pin.snapshot_date}" for pin in cut.sources)
        expected = ["lockfiles"]
        for row in (cut, installing, installed):
            expected.append(
                f"{row.label}: {row.state}; pipeline {row.pipeline}; cut {row.cut_at}; "
                f"installed {row.installed_at or 'not installed'}; base {row.base or 'none'}; sources {sources}"
            )
        expected.extend(["superseded 1", "generations"])
        for generation in (live, building):
            line = (
                f"{generation.gen_id}: {generation.state}; {generation.collection}; "
                f"lockfiles {', '.join(generation.lockfiles)}; documents ready {generation.counts['ready']}; "
                f"chunks {generation.counts['indexed'] + generation.counts.get('present', 0)}; bytes {generation.bytes}; "
                f"created {generation.created_at}; lexical built {generation.lexical_built_at}; "
                f"dense built {generation.dense_built_at or 'not built'}"
            )
            if generation.promoted_at is not None:
                line += f"; promoted {generation.promoted_at}"
            expected.append(line)
        for collection in collections:
            if collection.name == live.collection:
                expected.extend([
                    f"inventory {live.gen_id}",
                    f"{live.gen_id}: {live.state}; {live.collection}; lockfile {live.corpus_lockfile}, "
                    f"served {', '.join(live.lockfiles)}; pipeline {live.pipeline}; embedding space {live.embedding_space}",
                    f"lexical: documents ready {live.counts['ready']}, chunked 0, unchunked 0; "
                    f"chunks indexed {live.counts['indexed']}, present {live.counts['present']}, leveled 0; "
                    f"bytes {live.bytes}; built {live.lexical_built_at}",
                    f"dense: points 0, indexed 0, written 0 (present 0, leveled 0, copied 0, embedded 0), "
                    f"tokens 0; built {live.dense_built_at}",
                    "clustering: none",
                    "report: none; no build report is recorded for this generation, "
                    "which predates the report or whose runs never reached its generation stage",
                ])
            else:
                expected.append(f"inventory {collection.name}: no live generation; name a generation to report it")
        self.assertEqual(out.splitlines(), expected)
        self.assertEqual(len(host.calls), 3)

    def test_every_live_row_is_a_subject_in_collection_registry_order(self) -> None:
        host = FakeHost()
        first = live_for()
        second = replace(first, gen_id=generations.generation_name(first.collection, "corpus-2099-01-04", first.embedding_space))
        host.generation_rows = (second, first)
        other = generations.Collection("example-collection", "example-profile", lambda _: False)
        code, out, err = run_report(host, collections=(other, *generations.COLLECTIONS))
        self.assertEqual((code, err), (0, ""))
        subjects = [line for line in out.splitlines() if line.startswith("inventory ")]
        self.assertEqual(subjects, [
            f"inventory {other.name}: no live generation; name a generation to report it",
            f"inventory {first.gen_id}", f"inventory {second.gen_id}",
        ])

    def test_failed_and_retired_rows_and_absent_counts_are_listed(self) -> None:
        host = FakeHost()
        failed = replace(test_index_record.row_for(), state="failed", failure_reason="store",
                         counts={}, lexical_built_at=None)
        live = live_for()
        retired = replace(live, state="retired", retired_at="2099-01-04T13:00:00+00:00",
                          gen_id=generations.generation_name(live.collection, "corpus-2099-01-04", live.embedding_space))
        host.generation_rows = (failed, retired)
        code, out, err = run_report(host)
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"{failed.gen_id}: failed, {failed.failure_reason};", out)
        self.assertIn("documents ready 0; chunks 0;", out)
        self.assertIn("lexical built not built; dense built not built", out)
        self.assertIn(f"; promoted {retired.promoted_at}; retired {retired.retired_at}", out)
        self.assertNotIn(f"inventory {failed.gen_id}", out)
        self.assertNotIn(f"inventory {retired.gen_id}", out)

    def test_unrecorded_name_refuses_with_the_listing_fix(self) -> None:
        host = FakeHost()
        name = test_index_record.row_for().gen_id
        code, out, err = run_report(host, name)
        self.assertEqual((code, out), (2, ""))
        self.assertIn(f"generation {name} is not recorded", err)
        self.assertIn(f"Fix: Run {report.command('index report')} to list the recorded generations, then name one.", err)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertEqual(len(host.calls), 2)

    def test_ill_formed_and_overlong_names_refuse_before_reading(self) -> None:
        row = test_index_record.row_for()
        prefix = f"{row.collection}__{row.corpus_lockfile}__"
        names = ("", "unknown", row.gen_id.upper(), row.gen_id + "\n", "../" + row.gen_id,
                 prefix + "a" * (255 - len(prefix.encode())))
        for name in names:
            with self.subTest(name=name):
                host = FakeHost()
                code, out, err = run_report(host, name)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("generation name is invalid", err)
                self.assertIn(f"Fix: Run {report.command('index report')} to list the recorded generations, then name one.", err)
                self.assertEqual(len(err.splitlines()), 1)
                self.assertEqual(host.calls, [])

    def test_each_failed_read_refuses_before_stdout_with_the_database_fix(self) -> None:
        for kind, prefix, calls in (("lockfiles", "corpus", 1), ("generations", "generation", 2)):
            with self.subTest(kind=kind):
                host = FakeHost()
                host.record_rows = {lockfile_for().label: lockfile_for()}
                host.generation_rows = (live_for(),)
                host.fail_read, host.code = kind, 3
                code, out, err = run_report(host)
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(err, report.refusal(
                    "index report", f"{prefix} record read failed (exit 3)", database_fix(RENDERED, "index report"),
                ) + "\n")
                self.assertNotIn("private text sentinel", err)
                self.assertEqual(len(host.calls), calls)

    def test_each_invalid_read_refuses_before_stdout(self) -> None:
        for kind, prefix, calls in (("lockfiles", "corpus", 1), ("generations", "generation", 2)):
            with self.subTest(kind=kind):
                host = FakeHost()
                host.outputs[kind] = "{}\n"
                code, out, err = run_report(host)
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(err, report.refusal(
                    "index report", f"{prefix} record row is invalid", database_fix(RENDERED, "index report"),
                ) + "\n")
                self.assertEqual(len(host.calls), calls)

    def test_each_unavailable_read_refuses_without_retry(self) -> None:
        for kind, calls in (("lockfiles", 1), ("generations", 2)):
            for exit_code, error in ((127, None), (0, FileNotFoundError()), (0, subprocess.SubprocessError())):
                with self.subTest(kind=kind, code=exit_code, error=error):
                    host = FakeHost()
                    host.fail_read, host.code, host.error = kind, exit_code, error
                    code, out, err = run_report(host)
                    self.assertEqual((code, out), (2, ""))
                    self.assertIn("Fix: Restore Docker Compose and psql", err)
                    self.assertIn(report.command("index report"), err)
                    self.assertEqual(len(err.splitlines()), 1)
                    self.assertEqual(len(host.calls), calls)


class InventoryBlock(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeHost()
        self.build_report = test_index_report.report_for()
        self.row = replace(live_for(), counts={
            "ready": 3, "chunked": 2, "unchunked": 1, "indexed": 7, "present": 2,
            "dense_points": 9, "dense_indexed": 8, "dense_present": 2,
            "dense_copied": 3, "dense_embedded": 4, "dense_tokens": 123, "bytes": 0,
            "leveled": 5, "dense_leveled": 6,
        })
        self.host.record_rows = {lockfile_for().label: lockfile_for()}
        self.host.generation_rows = (self.row,)
        self.host.reports[self.row.gen_id] = self.build_report

    def test_live_inventory_prints_the_report_and_generation_figures_in_order(self) -> None:
        code, out, err = run_report(self.host)
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        block = lines[lines.index(f"inventory {self.row.gen_id}"):]
        row, counts, saved = self.row, self.row.counts, self.build_report
        self.assertGreater(row.bytes, 0)
        self.assertGreater(counts["dense_copied"], 0)
        self.assertGreater(counts["dense_embedded"], 0)
        self.assertEqual(block[:8], [
            f"inventory {row.gen_id}",
            f"{row.gen_id}: {row.state}; {row.collection}; lockfile {row.corpus_lockfile}, "
            f"served {', '.join(row.lockfiles)}; pipeline {row.pipeline}; embedding space {row.embedding_space}",
            f"lexical: documents ready {counts['ready']}, chunked {counts['chunked']}, unchunked {counts['unchunked']}; "
            f"chunks indexed {counts['indexed']}, present {counts['present']}, leveled {counts['leveled']}; "
            f"bytes {row.bytes}; built {row.lexical_built_at}",
            f"dense: points {counts['dense_points']}, indexed {counts['dense_indexed']}, "
            f"written {counts['dense_copied'] + counts['dense_embedded']} "
            f"(present {counts['dense_present']}, leveled {counts['dense_leveled']}, "
            f"copied {counts['dense_copied']}, embedded {counts['dense_embedded']}), "
            f"tokens {counts['dense_tokens']}; built {row.dense_built_at}",
            "clustering: none",
            f"report: written {saved.written_at}; state {saved.state}; failed stage none; reason none; "
            f"seconds {saved.seconds}; whole build yes; lifecycle seconds {saved.wall_clock.lifecycle_seconds}",
            "gate cluster: none",
            "treatment: none",
        ])
        entries = tuple(saved.courts.values())
        coverage = [value for entry in entries for value in entry.anchors.by_text_source.values()]
        anchored = sum(entry.anchors.anchored for entry in entries)
        anchor_ready = sum(entry.anchors.ready for entry in entries)
        with_anchors = sum(value.with_anchors for value in coverage)
        documents = sum(value.documents for value in coverage)
        self.assertNotEqual(anchored, with_anchors)
        self.assertEqual(block[8],
            f"courts {len(entries)}: opinions {sum(entry.counts.opinions for entry in entries)}; "
            f"ready {sum(entry.counts.by_status.get('ready', 0) for entry in entries)}; "
            f"failed {sum(entry.counts.by_status.get('failed', 0) for entry in entries)}; "
            f"anchor pass {anchored} of {anchor_ready}; with anchors {with_anchors} of {documents} "
            f"({round(100 * with_anchors / documents)}%)")
        distributions: list[tuple[str, str, Sequence[str]]] = [
            ("status", "by_status", ("ready", "processing", "failed", "withdrawn")),
            ("text source", "by_text_source", TEXT_SOURCES),
            ("precedential", "by_precedential", PRECEDENTIAL_VALUES),
            ("failure reason", "by_failure_reason", ()),
        ]
        for offset, (label, attribute, order) in enumerate(distributions, start=9):
            total: Counter[str] = Counter()
            for entry in entries:
                total.update(getattr(entry.counts, attribute))
            keys = order or sorted(total)
            expected = ", ".join(f"{key} {total[key]}" for key in keys)
            self.assertEqual(block[offset], f"by {label}: {expected}")
        for offset, (court, entry) in enumerate(sorted(saved.courts.items())):
            court_block = block[13 + offset * 7:20 + offset * 7]
            expected_counts = []
            for label, attribute, order in distributions:
                values = getattr(entry.counts, attribute)
                text = ", ".join(f"{key} {values.get(key, 0)}" for key in (order or sorted(values))) or "none"
                prefix = f"{court}: "
                if label == "status":
                    prefix += f"opinions {entry.counts.opinions}; "
                expected_counts.append(f"{prefix}by {label} {text}")
            self.assertEqual(court_block[:4], expected_counts)
            coverage = list(entry.anchors.by_text_source.values())
            documents = sum(value.documents for value in coverage)
            with_anchors = sum(value.with_anchors for value in coverage)
            chars = sum(value.chars for value in coverage)
            anchored_chars = sum(value.anchored_chars for value in coverage)
            percent = round(100 * with_anchors / documents) if documents else 0
            char_percent = round(100 * anchored_chars / chars) if chars else 0
            source_text = ", ".join(
                f"{source} {value.with_anchors} of {value.documents} "
                f"({round(100 * value.with_anchors / value.documents) if value.documents else 0}%)"
                for source in TEXT_SOURCES
                if (value := entry.anchors.by_text_source.get(source)) is not None
            ) or "none"
            self.assertEqual(court_block[4],
                f"{court}: anchor pass {entry.anchors.anchored} of {entry.anchors.ready}; "
                f"with anchors {with_anchors} of {documents} ({percent}%); "
                f"anchored chars {char_percent}%; by text source {source_text}")
            figure = entry.agreement
            if figure is None:
                self.assertEqual(court_block[5], f"{court}: agreement none")
            else:
                self.assertEqual(court_block[5],
                    f"{court}: agreement {round(100 * figure.agreed / figure.gideon_pairs)}% of GIDEON pairs, "
                    f"{round(100 * figure.agreed / figure.map_pairs)}% of map pairs; "
                    f"job {figure.job}; computed {figure.computed_at}")
            accounted = saved.gates["accounted"][court]
            self.assertEqual(court_block[6],
                f"{court}: accounted staged {accounted.staged}, recorded {accounted.recorded}, "
                f"ready {accounted.ready}, failed {accounted.failed}, {'pass' if accounted.passed else 'fail'}")
        self.assertEqual(block[-1], "metadata changes: none")
        self.assertEqual(len(block), 14 + 7 * len(saved.courts))
        self.assertEqual(len(self.host.calls), 3)
        argv, sql = self.host.calls[-1]
        self.assertNotIn(row.gen_id, " ".join(argv))
        self.assertEqual(sql, worker.bind("v_name", row.gen_id) + "\n" + buildreport.READ_REPORT_SQL)

    def test_totals_sum_each_distribution_and_keep_pass_and_coverage_distinct(self) -> None:
        courts = self.build_report.courts
        totals = inventory.totals_for(courts)
        self.assertEqual(totals.opinions, sum(entry.counts.opinions for entry in courts.values()))
        self.assertEqual(totals.anchored, sum(entry.anchors.anchored for entry in courts.values()))
        self.assertEqual(totals.anchor_ready, sum(entry.anchors.ready for entry in courts.values()))
        coverage = [value for entry in courts.values() for value in entry.anchors.by_text_source.values()]
        self.assertEqual(totals.with_anchors, sum(value.with_anchors for value in coverage))
        self.assertEqual(totals.documents, sum(value.documents for value in coverage))
        self.assertNotEqual(totals.anchored, totals.with_anchors)
        for attribute in ("by_status", "by_text_source", "by_precedential", "by_failure_reason"):
            expected: Counter[str] = Counter()
            for entry in courts.values():
                expected.update(getattr(entry.counts, attribute))
            self.assertEqual(getattr(totals, attribute), dict(expected))
        self.assertEqual(totals.ready, totals.by_status["ready"])
        self.assertEqual(totals.failed, totals.by_status["failed"])

    def test_named_building_subject_reads_its_report_and_keeps_other_generations_in_the_listing(self) -> None:
        building = replace(self.row, state="building", promoted_at=None, dense_built_at=None, built_at=None)
        other = replace(self.row, gen_id=generations.generation_name(
            self.row.collection, "corpus-2099-02-03", self.row.embedding_space,
        ))
        saved = replace(self.build_report, state="building", wall_clock=replace(
            self.build_report.wall_clock, whole_build=False, built_at=None, lifecycle_seconds=None,
        ))
        self.host.generation_rows = (building, other)
        self.host.reports[building.gen_id] = saved
        code, out, err = run_report(self.host, building.gen_id)
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"{other.gen_id}: live;", out)
        self.assertIn(f"{building.gen_id}: building; {building.collection}; lockfile", out)
        self.assertIn(f"tokens {building.counts['dense_tokens']}; built not built", out)
        self.assertIn("whole build no; lifecycle seconds not built", out)
        self.assertEqual([line for line in out.splitlines() if line.startswith("inventory ")],
                         [f"inventory {building.gen_id}"])
        self.assertEqual(len(self.host.calls), 3)

    def test_no_report_prints_counts_and_a_tail_that_matches_the_generation_state(self) -> None:
        building = test_index_record.row_for()
        built = replace(self.row, state="built", promoted_at=None)
        failed = replace(building, state="failed", failure_reason="store")
        retired = replace(self.row, state="retired", retired_at="2099-01-04T13:00:00+00:00")
        for row in (building, built, failed, self.row, retired):
            with self.subTest(state=row.state):
                self.host.generation_rows = (row,)
                self.host.reports.clear()
                code, out, err = run_report(self.host, row.gen_id)
                self.assertEqual((code, err), (0, ""))
                lines = out.splitlines()
                self.assertIn(f"inventory {row.gen_id}", lines)
                self.assertTrue(any(line.startswith("lexical: documents ready ") for line in lines))
                self.assertTrue(any(line.startswith("dense: points ") for line in lines))
                line = "report: none; no build report is recorded for this generation"
                if row.state in {"building", "built", "failed"}:
                    line += f"; {report.command(f'index build {row.collection} --lockfile {row.corpus_lockfile}')} "
                    line += "writes one when the lockfile, pipeline, and embedding space still name it"
                else:
                    line += ", which predates the report or whose runs never reached its generation stage"
                    self.assertNotIn("index build", out)
                self.assertEqual(lines[-1], line)

    def test_report_read_failure_preserves_printed_listings_and_stops_subjects(self) -> None:
        second = replace(self.row, gen_id=generations.generation_name(
            self.row.collection, "corpus-2099-02-03", self.row.embedding_space,
        ))
        self.host.generation_rows = (self.row, second)
        self.host.fail_read, self.host.code = "report", 3
        code, out, err = run_report(self.host)
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("lockfiles\n"))
        self.assertIn("\ngenerations\n", out)
        self.assertIn(f"{self.row.gen_id}: live;", out)
        self.assertIn(f"{second.gen_id}: live;", out)
        self.assertNotIn("inventory ", out)
        self.assertEqual(err, report.refusal(
            "index report", "build report read failed (exit 3)", database_fix(RENDERED, "index report"),
        ) + "\n")
        self.assertNotIn("private text sentinel", err)
        self.assertEqual(len(self.host.calls), 3)

    def test_zero_denominators_and_missing_agreement_or_accounted_rows_are_explicit(self) -> None:
        court, entry = next(iter(self.build_report.courts.items()))
        assert entry.agreement is not None
        empty_figure = replace(entry.agreement, gideon_pairs=0, map_pairs=0,
                               agreed=0, gideon_only=0, map_only_seen=0, map_only_missed=0)
        empty = replace(entry, counts=caselaw.CourtCounts(0, {}, {}, {}, {}),
                        anchors=caselaw.AnchorCounts(0, 0, {
                            TEXT_SOURCES[0]: caselaw.SourceCoverage(0, 0, 0, 0),
                        }), agreement=empty_figure)
        self.host.reports[self.row.gen_id] = replace(self.build_report, courts={court: empty}, gates={})
        code, out, err = run_report(self.host)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("anchor pass 0 of 0; with anchors 0 of 0 (0%)", out)
        self.assertIn(f"anchored chars 0%; by text source {TEXT_SOURCES[0]} 0 of 0 (0%)", out)
        self.assertIn(f"{court}: agreement 0% of GIDEON pairs, 0% of map pairs;", out)
        self.assertIn(f"{court}: accounted none", out)
        self.assertIn("by failure reason: none", out)
        self.host.reports[self.row.gen_id] = replace(self.build_report, courts={}, gates={})
        code, out, err = run_report(self.host)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("courts 0: opinions 0; ready 0; failed 0;", out)

    def test_unstaged_accounted_and_zero_lifecycle_seconds_are_preserved(self) -> None:
        court = next(iter(self.build_report.courts))
        accounted = self.build_report.gates["accounted"][court]
        wall_clock = replace(self.build_report.wall_clock, built_at=self.row.created_at,
                             lifecycle_seconds=0)
        self.host.reports[self.row.gen_id] = replace(self.build_report, wall_clock=wall_clock,
            gates={"accounted": {court: replace(accounted, staged=None, passed=None)}})
        code, out, err = run_report(self.host)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("lifecycle seconds 0", out)
        self.assertIn(f"{court}: accounted staged none, recorded {accounted.recorded}, "
                      f"ready {accounted.ready}, failed {accounted.failed}, not staged", out)

    def test_failed_report_and_metadata_render_counts_or_presence_without_contents(self) -> None:
        changes = {"text_source": {"caption": "Fictitious private caption\nsecond line"},
                   "precedential": 5, "court": True}
        self.host.reports[self.row.gen_id] = replace(self.build_report, state="failed", failed_stage="dense",
                                                   failure_reason="embed", metadata_changes=changes)
        code, out, err = run_report(self.host)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("state failed; failed stage dense; reason embed;", out)
        self.assertEqual(out.splitlines()[-1],
                         f"metadata changes: court present, precedential {changes['precedential']}, text_source present")
        self.assertNotIn("Fictitious private caption", out)
        self.assertNotIn("second line", out)

    def test_distributions_and_courts_ignore_payload_key_order(self) -> None:
        courts = {}
        for court, entry in reversed(tuple(self.build_report.courts.items())):
            counts = replace(entry.counts,
                by_status=dict(reversed(tuple(entry.counts.by_status.items()))),
                by_text_source=dict(reversed(tuple(entry.counts.by_text_source.items()))),
                by_precedential=dict(reversed(tuple(entry.counts.by_precedential.items()))),
                by_failure_reason=dict(reversed(tuple(entry.counts.by_failure_reason.items()))))
            courts[court] = replace(entry, counts=counts, anchors=replace(entry.anchors,
                by_text_source=dict(reversed(tuple(entry.anchors.by_text_source.items())))))
        code, expected, err = run_report(self.host)
        self.assertEqual((code, err), (0, ""))
        self.host.reports[self.row.gen_id] = replace(self.build_report, courts=courts)
        self.assertEqual(run_report(self.host), (0, expected, ""))

    def test_every_stdout_line_is_plain_and_contains_no_path_caption_or_citation(self) -> None:
        private_text = "Fictitious caption: Example v. Example, 999 F.4th 123\n/private/example/opinion.txt"
        self.host.reports[self.row.gen_id] = replace(self.build_report, metadata_changes={"caption": private_text},
                                                   treatment=test_index_treatment.treatment_for())
        code, out, err = run_report(self.host)
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn(private_text, out)
        for line in out.splitlines():
            with self.subTest(line=line):
                self.assertEqual(line, report.one_line(line))
                self.assertNotIn("/", line)
                self.assertNotIn("Example v. Example", line)
                self.assertNotIn("999 F.4th 123", line)
                self.assertNotIn("\r", line)
        self.assertEqual(len(out.splitlines()), 19 + 7 * len(self.build_report.courts))

    def test_clustering_figures_come_from_the_generation_and_follow_the_dense_line(self) -> None:
        counts = {
            **self.row.counts, "cluster_clusters": 2, "cluster_grouped": 8, "cluster_chunks": 11,
            "cluster_members": 3, "cluster_texts": 7, "cluster_boilerplate": 1,
            "cluster_flagged": 4, "cluster_seconds": 13,
        }
        for reused, stamp in ((0, self.row.lexical_built_at), (1, None), (2, None)):
            with self.subTest(reused=reused, clustered_at=stamp):
                row = replace(self.row, counts={**counts, "cluster_reused": reused}, clustered_at=stamp)
                self.host.generation_rows = (row,)
                code, out, err = run_report(self.host)
                self.assertEqual((code, err), (0, ""))
                lines = out.splitlines()
                dense = next(i for i, line in enumerate(lines) if line.startswith("dense: "))
                self.assertEqual(lines[dense + 1],
                    f"clustering: clusters {counts['cluster_clusters']} over {counts['cluster_grouped']} "
                    f"chunks of {counts['cluster_chunks']} ({counts['cluster_members']} near-duplicate texts "
                    f"of {counts['cluster_texts']}); boilerplate {counts['cluster_boilerplate']} clusters "
                    f"over {counts['cluster_flagged']} chunks "
                    f"({round(100 * counts['cluster_flagged'] / counts['cluster_chunks'])}% of {counts['cluster_chunks']}); "
                    f"seconds {counts['cluster_seconds']}; reused {'yes' if reused == 1 else 'no'}; "
                    f"clustered {stamp or 'not clustered'}")
                self.assertTrue(lines[dense + 2].startswith("report: "))

    def test_clustering_with_zero_chunks_prints_zero_share_and_missing_counts_as_zero(self) -> None:
        row = replace(self.row, counts={**self.row.counts, "cluster_chunks": 0})
        self.host.generation_rows = (row,)
        code, out, err = run_report(self.host)
        self.assertEqual((code, err), (0, ""))
        self.assertIn(
            "clustering: clusters 0 over 0 chunks of 0 (0 near-duplicate texts of 0); "
            "boilerplate 0 clusters over 0 chunks (0% of 0); seconds 0; reused no; clustered not clustered\n",
            out,
        )

    def test_clustering_is_none_without_chunk_key_even_with_a_timestamp_or_report(self) -> None:
        row = replace(self.row, clustered_at=self.row.lexical_built_at,
                      counts={**self.row.counts, "cluster_seconds": 13})
        self.host.generation_rows = (row,)
        for saved in (self.build_report, None):
            with self.subTest(report_present=saved is not None):
                self.host.reports.clear()
                if saved is not None:
                    self.host.reports[row.gen_id] = saved
                code, out, err = run_report(self.host)
                self.assertEqual((code, err), (0, ""))
                self.assertIn("clustering: none\n", out)

    def test_clustering_figures_remain_visible_without_a_build_report(self) -> None:
        self.host.generation_rows = (replace(self.row, counts={**self.row.counts, "cluster_chunks": 11}),)
        self.host.reports.clear()
        code, out, err = run_report(self.host)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("clustering: clusters 0 over 0 chunks of 11", out)
        self.assertIn("report: none;", out)

    def test_cluster_gate_uses_recorded_budget_reuse_and_verdict_after_report_line(self) -> None:
        for gate in (buildreport.ClusterGate(13, 17, False, True),
                     buildreport.ClusterGate(19, 17, True, False), None):
            with self.subTest(gate=gate):
                saved = self.build_report
                if gate is not None:
                    saved = replace(saved, gates={**saved.gates, "cluster": gate})
                    if not gate.passed:
                        saved = replace(saved, state="failed", failed_stage="cluster", failure_reason="cluster-budget")
                self.host.reports[self.row.gen_id] = saved
                code, out, err = run_report(self.host)
                self.assertEqual((code, err), (0, ""))
                lines = out.splitlines()
                report_line = next(i for i, line in enumerate(lines) if line.startswith("report: "))
                expected = "gate cluster: none" if gate is None else (
                    f"gate cluster: seconds {gate.seconds} of budget {gate.budget}; "
                    f"reused {'yes' if gate.reused else 'no'}; {'pass' if gate.passed else 'fail'}"
                )
                self.assertEqual(lines[report_line + 1], expected)
                self.assertTrue(lines[report_line + 2].startswith("treatment: "))
                if gate is not None and not gate.passed:
                    self.assertIn("failed stage cluster; reason cluster-budget;", lines[report_line])

    def test_treatment_prints_each_risk_state_with_its_share_after_the_cluster_gate(self) -> None:
        saved = test_index_treatment.treatment_for()
        reversed_states = replace(saved, by_state=dict(reversed(tuple(saved.by_state.items()))))
        empty = replace(saved, clusters=0, by_state=dict.fromkeys(saved.by_state, 0))
        for distribution, states in (
            (reversed_states, "clusters 7; negative 2 (29%), caution 1 (14%), unchecked 4 (57%)"),
            (empty, "clusters 0; negative 0 (0%), caution 0 (0%), unchecked 0 (0%)"),
        ):
            with self.subTest(clusters=distribution.clusters):
                self.host.reports[self.row.gen_id] = replace(self.build_report, treatment=distribution)
                code, out, err = run_report(self.host)
                self.assertEqual((code, err), (0, ""))
                lines = out.splitlines()
                gate = lines.index("gate cluster: none")
                self.assertEqual(lines[gate + 1], f"treatment: {states}; materialized {saved.materialized_at}")
                self.assertTrue(lines[gate + 2].startswith("courts "))
                self.assertNotIn(saved.pattern_set, out)
