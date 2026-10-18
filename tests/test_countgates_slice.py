"""Count-rule judgments, record refusals, and content-free command results."""

import ast
import contextlib
import copy
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock, patch

from test_evaluation_record import read_psql_set
from test_evaluation_run import NOW, ROOT, RUN_ID, EvalHost
from test_evaluation_set import count_gate_case
from test_index_report import report_for

from gideon.cli import main
from gideon.evaluation import command, countgates_slice, record
from gideon.evaluation.conditions import EngineConditions
from gideon.evaluation.countgates_slice import CourtFigures
from gideon.evaluation.evalset import SET_ROOT, LoadedSet, load_set, select_cases
from gideon.evaluation.generation import COLLECTION, GenerationAccess
from gideon.evaluation.results import RunContext, SliceResult
from gideon.host import report, stack
from gideon.host.corpus.lockfile import (
    SCHEMA_VERSION,
    IndexDocument,
    Lockfile,
    SidecarEntry,
    SourcePin,
    load_lockfile,
    render_lockfile,
    render_sidecar,
)
from gideon.host.corpus.sources import SOURCES
from gideon.host.index import buildreport
from gideon.host.index.sample import SAMPLE_SUFFIX
from gideon.host.render.ci import CI_ROOT, CI_STACK
from gideon.host.sysio import PathLike

PRIVATE = "[FICTIONAL TEST ONLY: opinion text and private diagnostic]"
LABEL = "corpus-2099-01-03"
GENERATION = GenerationAccess(f"{COLLECTION}__{LABEL}__fictitious-embedding", LABEL, "built")
RULE_IDS = tuple(f"count-gates-{rule}-01" for rule in ("staged", "recorded", "settled"))


def _complete_report() -> buildreport.BuildReport:
    original = report_for()
    accounted = {
        court: replace(row, staged=row.recorded, passed=True)
        for court, row in original.gates["accounted"].items()
    }
    return replace(
        original, gen_id=GENERATION.name, gates={"accounted": accounted},
        metadata_changes={"fictional-private-metadata": PRIVATE},
    )


def _write_lockfile(checkout: Path, label: str = LABEL) -> Path:
    """Write fictitious source pins and their real temporary companion files."""

    directory = checkout / countgates_slice.LOCKFILE_DIRECTORY
    source_directory = directory / label
    source_directory.mkdir(parents=True, exist_ok=True)
    data = b"[FICTIONAL TEST ONLY: source index bytes]"
    digest = hashlib.sha256(data).hexdigest()
    pins: dict[str, SourcePin] = {}
    for source in SOURCES:
        name = source.captured_file or "fictional-index.xml"
        entries = (SidecarEntry(name, digest, len(data)),)
        sidecar = render_sidecar(entries).encode()
        (source_directory / f"{source.name}.sha256").write_bytes(sidecar)
        (source_directory / f"{source.name}.{name}").write_bytes(data)
        url = "https://example.invalid/fictional-source"
        pins[source.name] = SourcePin(
            "2099-01-02", url, None, hashlib.sha256(sidecar).hexdigest(),
            len(entries), len(data), (IndexDocument(name, url, digest, len(data)),),
            entries, ("court1", "court2") if source.carries_courts else None,
            source.captured,
        )
    lockfile = Lockfile(SCHEMA_VERSION, label, "1000.0.0", "2099-01-03T04:05:06Z", "tranche", pins)
    path = directory / f"{label}.yaml"
    path.write_text(render_lockfile(lockfile), encoding="utf-8")
    return path


class CountGatesHost(EvalHost):
    """Serve a planted build report and the command's other record reads."""

    def __init__(self) -> None:
        super().__init__()
        self.report_payload: dict[str, Any] = _complete_report().to_json()
        self.report_stdout: str | None = None
        self.report_code = 0
        self.report_error: OSError | subprocess.SubprocessError | None = None

    def run(
        self, argv: Sequence[str], *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if input is not None and "FROM public.build_reports" in input:
            self.calls.append((tuple(argv), input))
            if self.report_error is not None:
                raise self.report_error
            reply = self.report_stdout
            if reply is None:
                reply = json.dumps(self.report_payload) + "\n"
            return subprocess.CompletedProcess(list(argv), self.report_code, reply, PRIVATE)
        return super().run(
            list(argv), check=check, input=input, cwd=cwd, env=env,
            timeout=timeout, passthrough=passthrough,
        )


class FiguresAndJudges(unittest.TestCase):
    def test_parsed_report_figures_include_missing_statuses_and_both_court_sets(self) -> None:
        payload = _complete_report().to_json()
        parsed = buildreport.BuildReport.from_json(payload)
        self.assertIsNotNone(parsed)
        source = cast(buildreport.BuildReport, parsed)
        figures = countgates_slice.court_figures(source)
        self.assertEqual(tuple(figures), tuple(sorted(source.courts)))
        self.assertEqual(figures["court1"], CourtFigures(5, 5, 3, 1, 1, 1, 0))
        self.assertEqual(figures["court2"], CourtFigures(2, 2, 0, 2, 2, 0, 0))
        missing = replace(source, gates={"accounted": {"court1": source.gates["accounted"]["court1"]}})
        missing_figures = countgates_slice.court_figures(missing)
        self.assertIsNone(missing_figures["court2"].staged)
        self.assertEqual(missing_figures["court2"].recorded, source.courts["court2"].counts.opinions)
        extra = replace(source, gates={"accounted": {
            **source.gates["accounted"], "court3": buildreport.Accounted(0, 0, 0, 0, True),
        }})
        self.assertEqual(countgates_slice.court_figures(extra)["court3"], CourtFigures(0, 0, 0, 0, 0, 0, 0))
        no_accounted = countgates_slice.court_figures(replace(source, gates={}))
        self.assertTrue(all(row.staged is None for row in no_accounted.values()))
        self.assertEqual(countgates_slice.judge_staged(tuple(source.courts), no_accounted), tuple(source.courts))
        self.assertEqual(countgates_slice.judge_recorded(no_accounted), tuple(source.courts))

    def test_named_count_sums_every_failure_reason(self) -> None:
        source = _complete_report()
        entry = source.courts["court2"]
        entry = replace(entry, counts=replace(entry.counts, by_failure_reason={"no-text": 1, "empty": 1}))
        parsed = buildreport.BuildReport.from_json(replace(source, courts={**source.courts, "court2": entry}).to_json())
        self.assertIsNotNone(parsed)
        self.assertEqual(countgates_slice.court_figures(cast(buildreport.BuildReport, parsed))["court2"].named, 2)

    def test_judges_at_their_edges_return_sorted_court_ids(self) -> None:
        complete = CourtFigures(3, 3, 1, 1, 1, 1, 0)
        self.assertEqual(countgates_slice.judge_staged(("court2",), {"court1": complete}), ("court1", "court2"))
        self.assertEqual(countgates_slice.judge_staged(("court1",), {"court1": complete}), ())
        for staged, recorded, expected in ((None, 3, ("court1",)), (3, 2, ("court1",)), (3, 3, ()), (3, 4, ())):
            with self.subTest(staged=staged, recorded=recorded):
                rows = {"court1": replace(complete, staged=staged, recorded=recorded)}
                self.assertEqual(countgates_slice.judge_recorded(rows), expected)
                self.assertEqual(countgates_slice.judge_staged(("court1",), rows), ("court1",) if staged is None else ())
        self.assertEqual(countgates_slice.judge_settled({"court1": complete}), ())
        self.assertEqual(countgates_slice.judge_settled({
            "court2": replace(complete, processing=1), "court1": replace(complete, named=0),
        }), ("court1", "court2"))
        self.assertEqual(countgates_slice.judge_settled({"court1": replace(complete, named=2)}), ("court1",))


class CountGatesFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.checkout = Path(temporary.name) / "checkout"
        self.checkout.mkdir()
        shutil.copyfile(ROOT / "courts.yaml", self.checkout / "courts.yaml")
        self.root = self.checkout / SET_ROOT
        self.cases = tuple(count_gate_case(identifier) for identifier in RULE_IDS)
        for case in self.cases:
            case["rule"] = PRIVATE
        self.write_cases(self.cases)
        self.lockfile_path = _write_lockfile(self.checkout)
        lockfile = load_lockfile(self.lockfile_path)
        self.assertTrue(lockfile.ok, lockfile.errors)
        self.host = CountGatesHost()
        self.context = RunContext(
            host=self.host, turns_dir="/tmp/fictional-count-stack",
            production_dir="/tmp/fictional-production", served_model_name=None,
            judge_prompt_id=None, repeats=1, progress=lambda _line: None,
            checkout=self.checkout, generation=GENERATION,
        )

    def write_cases(self, cases: tuple[dict[str, object], ...], ids: tuple[str, ...] | None = None) -> None:
        path = self.root / "build-gates/count-gates.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(case) + "\n" for case in cases))
        path = self.root / "slices/count-gates/rules.ids"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(identifier + "\n" for identifier in (RULE_IDS if ids is None else ids)))
        loaded = load_set(self.root)
        self.assertTrue(loaded.ok, loaded.findings)
        self.loaded = cast(LoadedSet, loaded.loaded)

    def run_slice(self, context: RunContext | None = None) -> SliceResult:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = countgates_slice.run_count_gates(self.loaded, "count-gates", self.context if context is None else context)
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
        self.assertNotIn(PRIVATE, result.report + repr(result.results))
        self.assertNotIn("Traceback", result.report)
        return result


class Runner(CountGatesFixture):
    def test_planted_complete_report_passes_all_rules_and_uses_reader_stdin(self) -> None:
        checkpoints: list[int] = []
        checkpoint = Mock(side_effect=lambda: checkpoints.append(len(self.host.calls)))
        result = self.run_slice(replace(self.context, checkpoint=checkpoint))
        self.assertIsNone(result.gated_cases)
        self.assertTrue(result.verdict, result.report)
        self.assertEqual(tuple(row.case_id for row in result.results), select_cases(self.loaded, "count-gates").counted)
        self.assertTrue(all(row.verdict == "pass" and row.metrics == {"failing": [], "courts": 2} for row in result.results))
        self.assertEqual(len(self.host.calls), 1)
        argv, sql = self.host.calls[0]
        self.assertEqual(argv, tuple(record.reader_argv(self.context.turns_dir)))
        self.assertIsNotNone(sql)
        statement = cast(str, sql)
        line = next(line for line in statement.splitlines() if line.startswith("\\set v_name "))
        self.assertEqual(read_psql_set(line, "v_name"), GENERATION.name)
        self.assertTrue(statement.endswith(buildreport.READ_REPORT_SQL))
        self.assertNotIn(GENERATION.name, " ".join(argv))
        self.assertGreaterEqual(checkpoint.call_count, 1 + len(result.results))
        self.assertEqual(checkpoints[0], 0)

    def test_planted_unaccounted_opinion_fails_recorded_and_names_its_court(self) -> None:
        row = self.host.report_payload["gates"]["accounted"]["court1"]
        row.update(staged=row["recorded"] + 1, **{"pass": False})
        result = self.run_slice()
        self.assertFalse(result.verdict)
        failed = [row for row in result.results if row.verdict == "fail"]
        self.assertEqual(tuple(row.case_id for row in failed), ("count-gates-recorded-01",))
        self.assertEqual(failed[0].metrics, {"failing": ["court1"], "courts": 2})
        self.assertIn("count-gates-recorded-01: fail, court1", result.report)

    def test_planted_processing_or_unnamed_failure_fails_settled(self) -> None:
        for processing in (True, False):
            with self.subTest(processing=processing):
                self.host.report_payload = cast(dict[str, Any], _complete_report().to_json())
                if processing:
                    self.host.report_payload["courts"]["court1"]["by_status"]["processing"] = 1
                else:
                    self.host.report_payload["courts"]["court1"]["by_failure_reason"] = {}
                result = self.run_slice()
                self.assertFalse(result.verdict)
                failed = [row for row in result.results if row.verdict == "fail"]
                self.assertEqual(tuple(row.case_id for row in failed), ("count-gates-settled-01",))
                self.assertEqual(failed[0].metrics["failing"], ["court1"])

    def test_report_is_exactly_ids_states_timestamps_counts_and_rule_verdicts(self) -> None:
        result = self.run_slice()
        source = _complete_report()
        self.assertEqual(result.report.splitlines(), [
            f"generation {GENERATION.name} ({GENERATION.corpus_lockfile}, {GENERATION.state})",
            f"report written_at {source.written_at}, state {source.state}, failed_stage none",
            "courts 2 in the report, 2 in the lockfile",
            "court1: staged 5, recorded 5, ready 3, failed 1 (named 1), withdrawn 1, processing 0",
            "court2: staged 2, recorded 2, ready 0, failed 2 (named 2), withdrawn 0, processing 0",
            "count-gates-recorded-01: pass, none",
            "count-gates-settled-01: pass, none",
            "count-gates-staged-01: pass, none",
            "count-gates: pass",
        ])

    def test_report_absent_fix_uses_the_resolved_label_for_sample_and_production(self) -> None:
        _write_lockfile(self.checkout, "corpus-2099-02-03")
        self.host.report_stdout = ""
        for sample in (False, True):
            with self.subTest(sample=sample):
                generation = replace(GENERATION, name=GENERATION.name + (f"__{SAMPLE_SUFFIX}" if sample else ""))
                context = replace(self.context, generation=generation, turns_dir=CI_ROOT if sample else self.context.turns_dir)
                result = self.run_slice(context)
                self.assertFalse(result.verdict)
                self.assertTrue(all(row.metrics == {"problem": "report-absent"} and row.verdict == "fail" for row in result.results))
                build = f"index build {COLLECTION}" + (" --sample" if sample else "")
                self.assertIn("Fix: Run " + report.command(build + f" --lockfile {GENERATION.corpus_lockfile}"), result.report)
                self.assertNotIn("corpus-2099-02-03", result.report)
                if sample:
                    self.assertIn("on the sibling", result.report)

    def test_invalid_unreachable_and_unstartable_reads_fail_every_case_with_fix(self) -> None:
        wrong_name = copy.deepcopy(self.host.report_payload)
        wrong_name["gen_id"] = GENERATION.name + f"__{SAMPLE_SUFFIX}"
        malformed_shape = copy.deepcopy(self.host.report_payload)
        malformed_shape["courts"]["court1"]["by_status"]["ready"] = -1
        scenarios = (
            (1, None, None, "record-unreachable"), (127, None, None, "record-unreachable"),
            (0, OSError(PRIVATE), None, "record-unreachable"),
            (0, subprocess.TimeoutExpired("fictional psql", 1), None, "record-unreachable"),
            (0, None, PRIVATE, "report-invalid"),
            (0, None, json.dumps(wrong_name), "report-invalid"),
            (0, None, json.dumps(malformed_shape), "report-invalid"),
            (0, None, "{}", "report-invalid"),
            (0, None, json.dumps(self.host.report_payload) + "\n{}", "report-invalid"),
        )
        for code, error, reply, problem in scenarios:
            with self.subTest(code=code, error=type(error).__name__, problem=problem):
                self.host.calls.clear()
                self.host.report_code, self.host.report_error, self.host.report_stdout = code, error, reply
                result = self.run_slice()
                self.assertFalse(result.verdict)
                self.assertEqual(len(result.results), len(RULE_IDS))
                self.assertTrue(all(row.verdict == "fail" and row.metrics == {"problem": problem} for row in result.results))
                self.assertIn("Fix: Run " + stack.logs_fix(self.context.turns_dir, record.POSTGRES_SERVICE), result.report)
                self.assertEqual(len(self.host.calls), 1)

    def test_absent_lockfile_or_checkout_fails_staged_alone_with_other_rules_judged(self) -> None:
        for absent_checkout in (True, False):
            with self.subTest(absent_checkout=absent_checkout):
                if not absent_checkout:
                    self.lockfile_path.unlink()
                result = self.run_slice(replace(self.context, checkout=None) if absent_checkout else self.context)
                self.assertFalse(result.verdict)
                self.assertEqual(tuple(row.case_id for row in result.results if row.verdict == "fail"), ("count-gates-staged-01",))
                self.assertEqual(result.results[-1].metrics, {"problem": "lockfile-absent"})
                self.assertIn("courts 2 in the report, none in the lockfile", result.report)
                self.assertIn("count-gates-staged-01: fail (lockfile-absent)", result.report)
                self.assertIn("--generation <name>", result.report)
                self.assertIn("corpus cut", result.report)
                self.assertIn("Fix:", result.report)

    def test_invalid_lockfile_reports_error_count_only_and_still_judges_other_rules(self) -> None:
        self.lockfile_path.write_text(PRIVATE)
        error_count = len(load_lockfile(self.lockfile_path).errors)
        result = self.run_slice()
        self.assertFalse(result.verdict)
        self.assertEqual(result.results[-1].metrics, {"problem": "lockfile-invalid"})
        self.assertTrue(all(row.verdict == "pass" for row in result.results[:-1]))
        self.assertIn(f"lockfile errors {error_count}", result.report)
        self.assertIn("Fix: Run " + report.command("corpus cut"), result.report)

    def test_unknown_rule_series_fails_with_module_and_set_fix(self) -> None:
        unknown = count_gate_case("count-gates-fictional-01")
        self.write_cases((*self.cases, unknown), (*RULE_IDS, str(unknown["id"])))
        result = self.run_slice()
        self.assertFalse(result.verdict)
        failed = [row for row in result.results if row.verdict == "fail"]
        self.assertEqual(tuple(row.case_id for row in failed), (unknown["id"],))
        self.assertEqual(failed[0].metrics, {"problem": "unknown-rule"})
        self.assertIn("countgates_slice.py", result.report)
        self.assertIn("build-gates/count-gates.jsonl", result.report)
        self.assertIn("Fix:", result.report)

    def test_superseding_rule_runs_the_same_judge_with_only_the_successor_counted(self) -> None:
        new = count_gate_case("count-gates-staged-02")
        new["supersedes"] = "count-gates-staged-01"
        self.write_cases((*self.cases, new), ("count-gates-staged-01", "count-gates-staged-02"))
        self.host.report_payload["gates"]["accounted"]["court1"].update(staged=None, **{"pass": None})
        result = self.run_slice()
        self.assertFalse(result.verdict)
        self.assertEqual(tuple(row.case_id for row in result.results), (new["id"],))
        self.assertEqual(result.results[0].metrics, {"failing": ["court1"], "courts": 2})
        self.assertNotIn("count-gates-staged-01:", result.report)

    def test_unresolved_generation_fails_without_reading(self) -> None:
        result = self.run_slice(replace(self.context, generation=None))
        self.assertFalse(result.verdict)
        self.assertEqual(len(result.results), len(RULE_IDS))
        self.assertTrue(all(row.metrics == {"problem": "generation-unresolved"} for row in result.results))
        self.assertIn("Fix: Run " + report.command("eval run --slice count-gates --generation <name>"), result.report)
        self.assertEqual(self.host.calls, [])

    def test_zero_counted_rules_fails_without_result_rows(self) -> None:
        self.loaded = replace(self.loaded, slices={"count-gates": ()})
        result = self.run_slice()
        self.assertFalse(result.verdict)
        self.assertEqual(result.results, ())
        self.assertTrue(result.report.endswith("count-gates: fail\n"))


class Command(CountGatesFixture):
    def test_command_records_resolved_generation_lockfile_and_three_results(self) -> None:
        self.host.generation_output = json.dumps({
            "gen_id": GENERATION.name, "corpus_lockfile": GENERATION.corpus_lockfile, "state": GENERATION.state,
        }) + "\n"
        real_run = command.run_eval
        stdout, stderr = io.StringIO(), io.StringIO()
        def run(args: Any) -> int:
            return real_run(
                args, host=self.host, checkout_root=self.checkout,
                rendered_dir="/tmp/fictional-production", site_path=ROOT / "config/site.example.yaml",
                clock=lambda: NOW, run_id_factory=lambda: RUN_ID,
                court_path=self.checkout / "courts.yaml",
                conditions=lambda _started, _finished: EngineConditions(None, None, "not asked"),
            )
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), patch.object(
            command, "run_eval", side_effect=run,
        ), patch("gideon.evaluation.stacks.secrets.select_directory"):
            code = main(["eval", "run", "--slice", "count-gates", "--stack", CI_STACK, "--generation", GENERATION.name])
        self.assertEqual((code, stderr.getvalue()), (0, ""), stdout.getvalue())
        writes = [sql for _argv, sql in self.host.calls if sql and "INSERT INTO eval_runs" in sql]
        self.assertEqual(len(writes), 1)
        sql = writes[0]
        self.assertEqual(sql.count("INSERT INTO eval_runs"), 1)
        self.assertEqual(sql.count("INSERT INTO eval_results"), len(RULE_IDS))
        expected = {"corpus_lockfile": GENERATION.corpus_lockfile, "generation_id": GENERATION.name}
        counted = select_cases(self.loaded, "count-gates").counted
        expected.update({f"result_{index}_case_id": case_id for index, case_id in enumerate(counted)})
        for variable, value in expected.items():
            line = next(line for line in sql.splitlines() if line.startswith(f"\\set {variable} "))
            self.assertEqual(read_psql_set(line, variable), value)
            self.assertIn(f":'{variable}'", sql)
        reads = [argv for argv, sql in self.host.calls if sql and "FROM public.build_reports" in sql]
        self.assertEqual(reads, [tuple(record.reader_argv(CI_ROOT))])
        self.assertIn("record: ok", stdout.getvalue())
        self.assertIn("count-gates: pass", stdout.getvalue())
        self.assertNotIn(PRIVATE, stdout.getvalue() + stderr.getvalue() + sql)


class Imports(unittest.TestCase):
    def test_imports_are_standard_library_or_gideon(self) -> None:
        path = Path(countgates_slice.__file__)
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = (node.module or "",)
            else:
                continue
            for name in names:
                with self.subTest(name=name, line=node.lineno):
                    self.assertIn(name.split(".", 1)[0], {*sys.stdlib_module_names, "gideon"})
