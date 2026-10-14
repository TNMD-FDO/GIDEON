"""Build-report parsing, gathering, and append-only record contracts."""

import copy
import json
import subprocess
import unittest
from collections.abc import Mapping
from dataclasses import fields, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import test_agreement_host
import test_index_record

from gideon.host import caselaw, report, stack, staging, worker
from gideon.host.index import buildreport, generations
from gideon.host.index.record import GenerationRow
from gideon.host.render.worker import (
    AGREEMENT_RECORD_NAME,
    DOCUMENT_FAILURE_REASONS,
    GENERATION_FAILURE_REASONS,
    GENERATION_STATES,
    PRECEDENTIAL_VALUES,
    STAGE_RECORD_NAME,
    STAGE_TABLES,
    TEXT_SOURCES,
)
from gideon.host.report import Problem
from gideon.host.sysio import Command, PathLike

RENDERED = test_index_record.RENDERED
WORK_ROOT = Path("/fictitious-report-work")
COMMAND_PATH = "index build caselaw --lockfile corpus-2099-01-03"


def count_rows() -> dict[str, dict[str, Any]]:
    return {
        "court1": {
            "opinions": 5, "by_status": {"ready": 3, "failed": 1, "withdrawn": 1},
            "by_text_source": {"xml_harvard": 2, "plain_text": 2},
            "by_precedential": {"published": 2, "unpublished": 1, "unknown": 2},
            "by_failure_reason": {"no-text": 1},
        },
        "court2": {
            "opinions": 2, "by_status": {"failed": 2}, "by_text_source": {},
            "by_precedential": {"unknown": 2}, "by_failure_reason": {"empty": 2},
        },
    }


def anchor_rows() -> dict[str, dict[str, Any]]:
    return {
        "court1": {
            "ready": 3, "anchored": 2, "by_text_source": {
                "xml_harvard": {"documents": 2, "with_anchors": 1, "chars": 100, "anchored_chars": 40},
                "plain_text": {"documents": 1, "with_anchors": 0, "chars": 50, "anchored_chars": 0},
            },
        },
        "court2": {"ready": 0, "anchored": 0, "by_text_source": {}},
    }


def figure() -> buildreport.CourtAgreement:
    value = test_agreement_host.figure()
    return buildreport.CourtAgreement(**{field.name: value[field.name] for field in fields(buildreport.CourtAgreement)})  # type: ignore[arg-type]


def report_for() -> buildreport.BuildReport:
    row = test_index_record.row_for()
    assert row.lexical_built_at is not None
    lifecycle = int((datetime.fromisoformat(row.lexical_built_at) - datetime.fromisoformat(row.created_at)).total_seconds())
    courts = {}
    anchors = anchor_rows()
    for court, counts in count_rows().items():
        coverage = {source: caselaw.SourceCoverage(**value) for source, value in anchors[court]["by_text_source"].items()}
        courts[court] = buildreport.CourtEntry(
            caselaw.CourtCounts(**counts),
            caselaw.AnchorCounts(anchors[court]["ready"], anchors[court]["anchored"], coverage),
            figure() if court == "court1" else None,
        )
    return buildreport.BuildReport(
        row.gen_id, "2099-01-03T12:00:01.000001+00:00", "built", None, None, 17,
        courts, {}, {"accounted": {
            "court1": buildreport.Accounted(4, 5, 3, 1, True),
            "court2": buildreport.Accounted(3, 2, 0, 2, False),
        }}, buildreport.WallClock(
            17, 5, 11, row.created_at, row.lexical_built_at, row.lexical_built_at,
            row.lexical_built_at, lifecycle, True,
        ),
    )


def changed(payload: dict[str, Any], path: tuple[str, ...], value: object) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return result


class ReportHost(test_index_record.FakeHost):
    """Serve fictitious court aggregates and work files through a dict-only seam."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        inputs = test_index_record.inputs_for()
        self.work_dir = staging.work_directory(inputs.label, "fictions", work_root=WORK_ROOT)
        self.stage_path = str(self.work_dir / STAGE_RECORD_NAME)
        self.figure_path = str(self.work_dir / f"court1.{AGREEMENT_RECORD_NAME}")
        stage = {
            "schema": 1, "label": inputs.label, "source": "fictions", "snapshot": inputs.snapshot,
            "courts": ["court1", "court2"], "inputs": {
                table: {"path": f"fiction-{table}.csv.bz2", "sha256": "a" * 64, "size": 1}
                for table in STAGE_TABLES
            },
            "records": dict.fromkeys(STAGE_TABLES, 7), "counts": {
                "court1": dict.fromkeys(STAGE_TABLES[1:], 4),
                "court2": dict.fromkeys(STAGE_TABLES[1:], 3),
            }, "job": 31, "seconds": 0.5, "staged_at": report_for().written_at,
        }
        self.files = {self.stage_path: json.dumps(stage), self.figure_path: json.dumps(test_agreement_host.figure())}
        self.file_reads: list[str] = []
        self.counts = count_rows()
        self.anchors = anchor_rows()

    def exists(self, path: PathLike) -> bool:
        return str(path) in self.files

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        self.file_reads.append(str(path))
        if str(path) not in self.files:
            raise FileNotFoundError(str(path))
        return self.files[str(path)]

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        result = super().run(argv, check=check, input=input, cwd=cwd, env=env,
                             timeout=timeout, passthrough=passthrough)
        if result.returncode == 0 and input is not None:
            for statement, rows in ((caselaw.CASELAW_COUNTS_SQL, self.counts), (caselaw.ANCHOR_COUNTS_SQL, self.anchors)):
                if statement in input:
                    court = next(line for line in input.splitlines() if line.startswith("\\set v_court ")).split("'", 2)[1]
                    return subprocess.CompletedProcess(list(argv), 0, json.dumps(rows[court]), "")
        return result


class Parser(unittest.TestCase):
    def setUp(self) -> None:
        self.row = report_for()
        self.payload: dict[str, Any] = self.row.to_json()

    def test_complete_shape_and_empty_optional_observations_parse(self) -> None:
        self.assertEqual(buildreport.BuildReport.from_json(self.payload), self.row)
        empty = replace(self.row, courts={}, gates={}, metadata_changes={})
        self.assertEqual(buildreport.BuildReport.from_json(empty.to_json()), empty)
        self.assertEqual(self.row.courts["court2"].anchors.by_text_source, {})
        self.assertIsNone(self.row.courts["court2"].agreement)

    def test_generation_states_failure_reasons_and_stages_parse(self) -> None:
        for state in GENERATION_STATES:
            with self.subTest(state=state):
                row = replace(self.row, state=state, failure_reason="store" if state == "failed" else None)
                self.assertEqual(buildreport.BuildReport.from_json(row.to_json()), row)
        for reason in GENERATION_FAILURE_REASONS:
            for stage in buildreport.REPORT_STAGES:
                with self.subTest(reason=reason, stage=stage):
                    row = replace(self.row, state="failed", failure_reason=reason, failed_stage=stage)
                    self.assertEqual(buildreport.BuildReport.from_json(row.to_json()), row)

    def test_document_distribution_closed_sets_parse(self) -> None:
        for name, allowed in (("by_text_source", TEXT_SOURCES), ("by_precedential", PRECEDENTIAL_VALUES),
                              ("by_failure_reason", DOCUMENT_FAILURE_REASONS)):
            for key in allowed:
                with self.subTest(name=name, key=key):
                    value = changed(self.payload, ("courts", "court1", name), {key: 1})
                    observed = buildreport.BuildReport.from_json(value)
                    self.assertIsNotNone(observed)
                    assert observed is not None
                    self.assertEqual(getattr(observed.courts["court1"].counts, name), {key: 1})

    def test_each_fixed_key_set_refuses_missing_and_extra_keys(self) -> None:
        paths = (
            (), ("courts", "court1"), ("courts", "court1", "anchors"),
            ("courts", "court1", "anchors", "by_text_source", "xml_harvard"),
            ("courts", "court1", "agreement"), ("gates", "accounted", "court1"), ("wall_clock",),
        )
        for path in paths:
            target = self.payload
            for key in path:
                target = target[key]
            for key in target:
                with self.subTest(path=path, missing=key):
                    replacement = {name: value for name, value in target.items() if name != key}
                    value = changed(self.payload, path, replacement) if path else replacement
                    self.assertIsNone(buildreport.BuildReport.from_json(value))
            with self.subTest(path=path, extra=True):
                replacement = {**target, "unexpected": 1}
                value = changed(self.payload, path, replacement) if path else replacement
                self.assertIsNone(buildreport.BuildReport.from_json(value))

    def test_nonobjects_and_invalid_court_keys_refuse(self) -> None:
        value: object
        for value in (None, [], "fictitious nonobject", 1):
            with self.subTest(value=value):
                self.assertIsNone(buildreport.BuildReport.from_json(value))
        for path in (("courts",), ("metadata_changes",), ("gates",), ("wall_clock",),
                     ("gates", "accounted"), ("courts", "court1"),
                     ("courts", "court1", "anchors"), ("courts", "court1", "agreement")):
            with self.subTest(path=path):
                self.assertIsNone(buildreport.BuildReport.from_json(changed(self.payload, path, [])))
        for path in (("courts",), ("gates", "accounted")):
            for court in ("../court", "Court1", "court with spaces"):
                with self.subTest(path=path, court=court):
                    entry = self.payload["courts"]["court1"] if path == ("courts",) else self.payload["gates"]["accounted"]["court1"]
                    self.assertIsNone(buildreport.BuildReport.from_json(changed(self.payload, path, {court: entry})))

    def test_every_count_rejects_negative_boolean_and_fractional_values(self) -> None:
        paths = [("seconds",), ("courts", "court1", "opinions")]
        paths += [("courts", "court1", name, next(iter(self.payload["courts"]["court1"][name])))
                  for name in ("by_status", "by_text_source", "by_precedential", "by_failure_reason")]
        paths += [("courts", "court1", "anchors", name) for name in ("ready", "anchored")]
        paths += [("courts", "court1", "anchors", "by_text_source", "xml_harvard", name)
                  for name in self.payload["courts"]["court1"]["anchors"]["by_text_source"]["xml_harvard"]]
        paths += [("courts", "court1", "agreement", name) for name in self.payload["courts"]["court1"]["agreement"]
                  if name not in {"seconds", "computed_at"}]
        paths += [("gates", "accounted", "court1", name) for name in ("staged", "recorded", "ready", "failed")]
        paths += [("wall_clock", name) for name in ("run_seconds", "lexical_seconds", "dense_seconds", "lifecycle_seconds")]
        for path in paths:
            for value in (-1, True, 0.5, "1"):
                with self.subTest(path=path, value=value):
                    self.assertIsNone(buildreport.BuildReport.from_json(changed(self.payload, path, value)))

    def test_unknown_closed_set_values_and_invalid_identity_refuse(self) -> None:
        replacements: tuple[tuple[tuple[str, ...], object], ...] = (
            (("gen_id",), "fictitious invalid name"), (("state",), "unknown-state"),
            (("failed_stage",), "unknown-stage"), (("failure_reason",), "unknown-reason"),
            (("courts", "court1", "by_status"), {"unknown-status": 1}),
            (("courts", "court1", "by_text_source"), {"unknown-source": 1}),
            (("courts", "court1", "by_precedential"), {"unknown-precedential": 1}),
            (("courts", "court1", "by_failure_reason"), {"unknown-reason": 1}),
            (("courts", "court1", "anchors", "by_text_source"), {"unknown-source": {}}),
            (("gates",), {"unknown-gate": {}}),
        )
        for path, value in replacements:
            with self.subTest(path=path):
                self.assertIsNone(buildreport.BuildReport.from_json(changed(self.payload, path, value)))
        for state, reason in (("failed", None), ("built", "store"), ("failed", "unknown-reason")):
            with self.subTest(state=state, reason=reason):
                value = {**self.payload, "state": state, "failure_reason": reason}
                self.assertIsNone(buildreport.BuildReport.from_json(value))

    def test_each_timestamp_requires_an_aware_iso_string(self) -> None:
        paths = [("written_at",), ("courts", "court1", "agreement", "computed_at")]
        paths += [("wall_clock", key) for key in ("created_at", "lexical_built_at", "dense_built_at", "built_at")]
        for path in paths:
            for value in ("2099-01-03T12:00:00", "2099-01-03", "not-a-timestamp", 1):
                with self.subTest(path=path, value=value):
                    self.assertIsNone(buildreport.BuildReport.from_json(changed(self.payload, path, value)))

    def test_pass_bit_and_wall_clock_consistency_refuse(self) -> None:
        assert self.row.wall_clock.lifecycle_seconds is not None
        replacements = (
            (("gates", "accounted", "court1", "pass"), False),
            (("gates", "accounted", "court1", "pass"), None),
            (("gates", "accounted", "court1", "pass"), 1),
            (("gates", "accounted", "court1", "staged"), None),
            (("gates", "accounted", "court2", "pass"), True),
            (("wall_clock", "lifecycle_seconds"), self.row.wall_clock.lifecycle_seconds + 1),
            (("wall_clock", "lifecycle_seconds"), None),
            (("wall_clock", "built_at"), None),
            (("wall_clock", "built_at"), "2098-01-03T12:00:00+00:00"),
            (("wall_clock", "run_seconds"), self.row.seconds + 1),
            (("wall_clock", "whole_build"), 1),
        )
        for path, value in replacements:
            with self.subTest(path=path):
                self.assertIsNone(buildreport.BuildReport.from_json(changed(self.payload, path, value)))

    def test_agreement_arithmetic_and_anchor_coverage_refuse(self) -> None:
        replacements = (
            (("courts", "court1", "agreement", "job"), 0),
            (("courts", "court1", "agreement", "seconds"), -0.1),
            (("courts", "court1", "agreement", "seconds"), float("inf")),
            (("courts", "court1", "agreement", "seconds"), float("nan")),
            (("courts", "court1", "agreement", "agreed"), 10),
            (("courts", "court1", "agreement", "gideon_only"), 0),
            (("courts", "court1", "agreement", "map_only_seen"), 0),
            (("courts", "court1", "agreement", "map_outside"), 100),
            (("courts", "court1", "anchors", "anchored"), 10),
            (("courts", "court1", "anchors", "by_text_source", "xml_harvard", "with_anchors"), 10),
            (("courts", "court1", "anchors", "by_text_source", "xml_harvard", "anchored_chars"), 1000),
        )
        for path, value in replacements:
            with self.subTest(path=path):
                self.assertIsNone(buildreport.BuildReport.from_json(changed(self.payload, path, value)))

    def test_agreement_shares_match_the_record_and_handle_zero_pairs(self) -> None:
        observed = figure()
        self.assertEqual(observed.gideon_share(), round(100 * observed.agreed / observed.gideon_pairs))
        self.assertEqual(observed.map_share(), round(100 * observed.agreed / observed.map_pairs))
        empty = replace(observed, agreed=0, gideon_pairs=0, map_pairs=0, gideon_only=0, map_only_seen=0, map_only_missed=0)
        self.assertEqual((empty.gideon_share(), empty.map_share()), (0, 0))
        self.assertEqual(buildreport.CourtAgreement.from_json(empty.to_json()), empty)


class Gather(unittest.TestCase):
    def setUp(self) -> None:
        self.host = ReportHost()
        self.inputs = replace(test_index_record.inputs_for(), courts=("court2", "court1"))
        base = test_index_record.row_for()
        self.row = replace(base, state="built", counts={"seconds": 5, "dense_seconds": 11},
                           dense_built_at=base.lexical_built_at, built_at=base.lexical_built_at)

    def gather(self, row: GenerationRow | None = None) -> buildreport.BuildReport:
        result = buildreport.gather(
            self.host, RENDERED, self.inputs, row or self.row, 17, None, True,
            work_root=WORK_ROOT, command_path=COMMAND_PATH,
        )
        self.assertIsInstance(result, buildreport.BuildReport)
        assert isinstance(result, buildreport.BuildReport)
        return result

    def test_two_courts_optional_agreement_accounted_counts_and_wall_clock(self) -> None:
        observed = self.gather()
        self.assertEqual(tuple(observed.courts), ("court1", "court2"))
        for court, counts in self.host.counts.items():
            self.assertEqual(observed.courts[court].counts, caselaw.CourtCounts(**counts))
        self.assertEqual(observed.courts["court1"].agreement, figure())
        self.assertIsNone(observed.courts["court2"].agreement)
        self.assertEqual(observed.gates["accounted"]["court1"], buildreport.Accounted(4, 5, 3, 1, True))
        self.assertEqual(observed.gates["accounted"]["court2"], buildreport.Accounted(3, 2, 0, 2, False))
        self.assertEqual(self.host.file_reads.count(self.host.stage_path), 1)
        self.assertEqual(observed.wall_clock, report_for().wall_clock)
        self.assertEqual(observed.metadata_changes, {})
        courts = [next(line for line in sql.splitlines() if line.startswith("\\set v_court "))
                  for _, sql in self.host.calls if sql is not None]
        self.assertEqual(courts, [worker.bind("v_court", court) for court in ("court1", "court1", "court2", "court2")])

    def test_absent_work_records_keep_counts_and_nullable_observations(self) -> None:
        self.host.files.clear()
        observed = self.gather()
        for court, row in observed.gates["accounted"].items():
            self.assertIsNone(row.staged)
            self.assertIsNone(row.passed)
            self.assertIsNone(observed.courts[court].agreement)
            self.assertEqual(row.recorded, self.host.counts[court]["opinions"])
        self.assertEqual(buildreport.BuildReport.from_json(observed.to_json()), observed)

    def test_other_snapshot_and_missing_court_have_no_staged_comparison(self) -> None:
        original = json.loads(self.host.files[self.host.stage_path])
        for variant in (
            {**original, "snapshot": "fictions-2099-02-02"},
            {**original, "courts": ["court1"], "counts": {"court1": original["counts"]["court1"]}},
        ):
            with self.subTest(variant=variant):
                self.host.files[self.host.stage_path] = json.dumps(variant)
                observed = self.gather()
                self.assertIsNone(observed.gates["accounted"]["court2"].staged)
                self.assertIsNone(observed.gates["accounted"]["court2"].passed)
                if variant["snapshot"] != self.inputs.snapshot:
                    self.assertIsNone(observed.gates["accounted"]["court1"].staged)
                    self.assertIsNone(observed.gates["accounted"]["court1"].passed)

    def test_unbuilt_lifecycle_and_absent_half_seconds_are_null(self) -> None:
        observed = self.gather(replace(self.row, state="building", counts={}, dense_built_at=None, built_at=None))
        self.assertIsNone(observed.wall_clock.lifecycle_seconds)
        self.assertIsNone(observed.wall_clock.lexical_seconds)
        self.assertIsNone(observed.wall_clock.dense_seconds)
        self.assertEqual(buildreport.BuildReport.from_json(observed.to_json()), observed)

    def test_gathered_payload_round_trips_with_agreement_present_and_absent(self) -> None:
        for present in (True, False):
            with self.subTest(present=present):
                if not present:
                    self.host.files.pop(self.host.figure_path)
                observed = self.gather()
                encoded = json.dumps(observed.to_json())
                self.assertEqual(buildreport.BuildReport.from_json(json.loads(encoded)), observed)
                payload = json.loads(encoded)
                if present:
                    self.assertNotIn("schema", payload["courts"]["court1"]["agreement"])
                    self.assertNotIn("snapshot", payload["courts"]["court1"]["agreement"])

    def test_reader_problems_refuse_instead_of_returning_partial_reports(self) -> None:
        for path in (self.host.stage_path, self.host.figure_path):
            with self.subTest(path=path):
                saved = self.host.files[path]
                self.host.files[path] = "malformed fictitious record"
                observed = buildreport.gather(self.host, RENDERED, self.inputs, self.row, 17, None, False,
                                              work_root=WORK_ROOT, command_path=COMMAND_PATH)
                self.assertIsInstance(observed, Problem)
                assert isinstance(observed, Problem)
                self.assertIn(report.command(COMMAND_PATH), observed.fix)
                self.host.files[path] = saved
        self.host.code = 3
        self.host.calls.clear()
        observed = buildreport.gather(self.host, RENDERED, self.inputs, self.row, 17, None, False,
                                      work_root=WORK_ROOT, command_path=COMMAND_PATH)
        self.assertIsInstance(observed, Problem)
        self.assertEqual(len(self.host.calls), 1)


class Record(unittest.TestCase):
    def setUp(self) -> None:
        self.row = report_for()

    def test_writer_binds_one_payload_on_stdin_and_returns_database_time(self) -> None:
        returned = replace(self.row, written_at="2099-01-03T12:00:02.000002+00:00")
        host = ReportHost(stdout=json.dumps(returned.to_json()))
        self.assertEqual(buildreport.write_report(host, RENDERED, self.row), returned)
        self.assertEqual(len(host.calls), 1)
        argv, sql = host.calls[0]
        self.assertEqual(argv, worker.psql_argv(RENDERED))
        self.assertNotIn(self.row.gen_id, " ".join(argv))
        assert sql is not None
        self.assertIn(worker.bind("v_payload", json.dumps(self.row.to_json(), separators=(",", ":"))), sql)
        self.assertIn("INSERT INTO public.build_reports", sql)
        self.assertIn("RETURNING *", sql)
        self.assertEqual(sql.count("\\set "), 1)
        self.assertNotIn("UPDATE", sql)
        self.assertNotIn("DELETE", sql)

    def test_writer_refuses_failed_exit_invalid_payload_and_invalid_returned_rows(self) -> None:
        host = ReportHost(code=3, stdout=json.dumps(self.row.to_json()), stderr="private text sentinel")
        result = buildreport.write_report(host, RENDERED, self.row)
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("exit 3", result.problem)
        self.assertNotIn("private text sentinel", result.problem + result.fix)
        for stdout in ("", "malformed", "[]", json.dumps({}),
                       json.dumps(replace(self.row, gen_id=generations.generation_name("caselaw", "corpus-2099-02-03", test_index_record.SPACE)).to_json()),
                       json.dumps(self.row.to_json()) * 2):
            with self.subTest(stdout=stdout):
                result = buildreport.write_report(ReportHost(stdout=stdout), RENDERED, self.row)
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("invalid row", result.problem)
                self.assertIn(stack.logs_fix(RENDERED, "postgres"), result.fix)
        host = ReportHost()
        result = buildreport.write_report(host, RENDERED, replace(self.row, seconds=-1))
        self.assertIsInstance(result, Problem)
        self.assertEqual(host.calls, [])

    def test_reader_binds_the_name_and_selects_the_newest_row_or_none(self) -> None:
        host = ReportHost(stdout=json.dumps(self.row.to_json()))
        self.assertEqual(buildreport.read_report(host, RENDERED, self.row.gen_id), self.row)
        argv, sql = host.calls[0]
        self.assertEqual(argv, worker.psql_argv(RENDERED))
        self.assertNotIn(self.row.gen_id, " ".join(argv))
        assert sql is not None
        self.assertIn(worker.bind("v_name", self.row.gen_id), sql)
        self.assertIn("ORDER BY written_at DESC LIMIT 1", sql)
        self.assertIn("WHERE gen_id = :'v_name'", sql)
        self.assertIsNone(buildreport.read_report(ReportHost(stdout="\n"), RENDERED, self.row.gen_id))

    def test_reader_refuses_failed_exit_and_invalid_rows(self) -> None:
        other = replace(self.row, gen_id=generations.generation_name("caselaw", "corpus-2099-02-03", test_index_record.SPACE))
        for code, stdout, message in ((5, json.dumps(self.row.to_json()), "exit 5"), (0, "malformed", "invalid"),
                                      (0, "[]", "invalid"), (0, json.dumps(other.to_json()), "invalid")):
            with self.subTest(code=code, stdout=stdout):
                result = buildreport.read_report(ReportHost(code=code, stdout=stdout), RENDERED, self.row.gen_id)
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn(message, result.problem)
                self.assertIn(stack.logs_fix(RENDERED, "postgres"), result.fix)

    def test_unavailable_tools_and_database_fixes_use_installed_commands_without_retry(self) -> None:
        report.set_installed_form(True)
        self.addCleanup(report.set_installed_form, False)
        operations = (
            lambda host: buildreport.write_report(host, RENDERED, self.row, command_path=COMMAND_PATH),
            lambda host: buildreport.read_report(host, RENDERED, self.row.gen_id, command_path=COMMAND_PATH),
        )
        for operation in operations:
            for code, error in ((3, None), (127, None), (0, FileNotFoundError()), (0, subprocess.SubprocessError())):
                with self.subTest(code=code, error=error):
                    host = ReportHost(code=code, error=error)
                    result = operation(host)
                    self.assertIsInstance(result, Problem)
                    assert isinstance(result, Problem)
                    self.assertIn(f"gideon {COMMAND_PATH}", result.fix)
                    self.assertNotIn("python3", result.fix)
                    self.assertEqual(len(host.calls), 1)
                    if code == 127 or error is not None:
                        self.assertIn("Restore Docker Compose and psql", result.fix)
                    else:
                        self.assertIn(stack.logs_fix(RENDERED, "postgres"), result.fix)

    def test_summary_is_house_prose_with_identifiers_and_counts_alone(self) -> None:
        self.assertEqual(buildreport.summary(self.row), (
            f"{self.row.gen_id}: written_at {self.row.written_at}; courts 2; "
            "opinions 7, ready 3, failed 3; anchored 2 of 3; agreement figures 1 of 2 courts; "
            "accounted 1 of 2 staged; seconds 17"
        ))
        self.assertNotIn(str(WORK_ROOT), buildreport.summary(self.row))
        self.assertNotIn("=", buildreport.summary(self.row))
