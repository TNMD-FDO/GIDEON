"""The gold-drop bounds and the content-free runner and command contracts."""

import ast
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

from test_evaluation_record import read_psql_set
from test_evaluation_run import (
    ROOT,
    EvalHost,
    _copy_sibling_artifacts,
    _generation_row,
    _invoke,
    _run_kwargs,
    _sibling_row,
)
from test_evaluation_set import (
    _judgment,
    _judgment_case,
    _research_qa_case,
    _signoff,
    _write,
    _write_research_cases,
    _write_signoffs,
)

from gideon.evaluation import golddrop_slice, judgments, record, sample, signoffs
from gideon.evaluation.evalset import (
    SET_ROOT,
    SHAPE_REGISTRY,
    LoadedSet,
    load_set,
    select_cases,
)
from gideon.evaluation.generation import COLLECTION, GenerationAccess
from gideon.evaluation.judgments import Judgment
from gideon.evaluation.results import RunContext, SliceResult
from gideon.host import report, stack
from gideon.host.index.sample import sample_name
from gideon.host.render.ci import CI_ROOT, CI_STACK

PRIVATE = "[FICTIONAL TEST ONLY: private signed answer and question]"
MANIFEST_HASH = "e" * 64


class GoldDropHost(EvalHost):
    """Serve manifest JSON lines and the command's other reads without a store."""

    def __init__(self) -> None:
        super().__init__()
        self.manifest: dict[str, str] = {}
        self.manifest_code = 0
        self.manifest_stdout: str | None = None
        self.manifest_error: OSError | subprocess.SubprocessError | None = None

    def run(
        self, argv: Sequence[str], *, check: bool = False,
        input: str | None = None, cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None, timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if input is not None and "FROM public.generation_documents" in input:
            self.calls.append((tuple(argv), input))
            if self.manifest_error is not None:
                raise self.manifest_error
            reply = self.manifest_stdout
            if reply is None:
                reply = "".join(json.dumps({
                    "source_id": source_id, "canonical_text_sha256": sha256,
                }) + "\n" for source_id, sha256 in sorted(self.manifest.items()))
            return subprocess.CompletedProcess(list(argv), self.manifest_code, reply, PRIVATE)
        return super().run(
            list(argv), check=check, input=input, cwd=cwd, env=env,
            timeout=timeout, passthrough=passthrough,
        )


def _context(host: GoldDropHost) -> RunContext:
    row = _generation_row(state="built")
    return RunContext(
        host=host, turns_dir="/tmp/fictitious-gold-stack",
        production_dir="/tmp/fictitious-production-stack", served_model_name=None,
        judge_prompt_id=None, repeats=1, progress=lambda _line: None,
        generation=GenerationAccess(row["gen_id"], row["corpus_lockfile"], row["state"]),
    )


def _manifest(loaded: LoadedSet) -> dict[str, str]:
    return {
        source_id: min(hashes) if hashes else MANIFEST_HASH
        for source_id, hashes in sample.gold_hashes(loaded).items()
    }


class DropBounds(unittest.TestCase):
    """A document counts once, and the ceiling is an exact whole-number ratio."""

    def test_zero_tolerance_category_constant_is_removed(self) -> None:
        self.assertFalse(hasattr(golddrop_slice, "ZERO_TOLERANCE_CATEGORIES"))

    def test_treatment_and_section_attribution_shapes_are_zero_tolerance(self) -> None:
        for category in ("treatment", "section-attribution"):
            with self.subTest(category=category):
                self.assertIs(SHAPE_REGISTRY[("research-qa", category)].zero_tolerance, True)

    def test_absent_changed_present_and_unversioned_gold(self) -> None:
        gold = {
            "fictional/shared": frozenset({"a" * 64, "b" * 64}),
            "fictional/present": frozenset({"a" * 64}),
            "fictional/changed": frozenset({"a" * 64}),
            "fictional/absent": frozenset({"a" * 64}),
            "fictional/must-cite": frozenset(),
        }
        manifest = {
            "fictional/shared": "a" * 64, "fictional/present": "a" * 64,
            "fictional/changed": "b" * 64, "fictional/must-cite": "c" * 64,
        }
        verdict = golddrop_slice.dropped_documents(gold, manifest)
        self.assertEqual(verdict.absent, ("fictional/absent",))
        self.assertEqual(verdict.changed, ("fictional/changed", "fictional/shared"))
        self.assertEqual(golddrop_slice.dropped_documents({}, {}).absent, ())
        self.assertEqual(golddrop_slice.dropped_documents({}, {}).changed, ())
        unversioned = golddrop_slice.dropped_documents({"fictional/must-cite": frozenset()}, {})
        self.assertEqual((unversioned.absent, unversioned.changed), (("fictional/must-cite",), ()))

    def test_gate_edges_and_zero_tolerance_at_any_ratio(self) -> None:
        for zero, dropped, total, expected in (
            (0, 0, 0, True), (0, 1, 20, True), (0, 1, 19, False),
            (1, 1, 20, False), (1, 1, 1_000_000, False),
        ):
            with self.subTest(zero=zero, dropped=dropped, total=total):
                gate = golddrop_slice.slice_gate(zero, dropped, total)
                self.assertEqual(gate.ok, expected)
                self.assertEqual(bool(gate.reasons), not expected)
                if zero:
                    self.assertIn("zero-tolerance", " ".join(gate.reasons))
                if total == 19:
                    self.assertIn("ceiling", " ".join(gate.reasons))


class GoldDropFixture(unittest.TestCase):
    """Load signed and unsigned fictitious cases through the real set loader."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.checkout = Path(temporary.name) / "checkout"
        self.checkout.mkdir()
        shutil.copy(ROOT / "courts.yaml", self.checkout / "courts.yaml")
        self.root = self.checkout / SET_ROOT
        cases = tuple(
            _research_qa_case(f"research-qa-{index:03d}", category, PRIVATE)
            for index, category in enumerate(("lookup", "synthesis", "retrieval"), 1)
        )
        _write_research_cases(self.root, cases)
        _write(self.root, "slices/gold-drop/fictitious.ids", "\n".join(str(case["id"]) for case in cases))
        self.signed = tuple(
            replace(_signoff(f"research-qa-{index:03d}", PRIVATE), must_cite=(f"fictional/source-{index}",))
            for index in (1, 2)
        )
        _write_signoffs(self.root, self.signed)
        self.loaded = self.load()
        self.host = GoldDropHost()
        self.host.manifest = _manifest(self.loaded)
        self.output = ""
        self.errors = ""

    def load(self) -> LoadedSet:
        result = load_set(self.root)
        self.assertTrue(result.ok, result.findings)
        assert result.loaded is not None
        return result.loaded

    def with_judgments(self, rows: tuple[Judgment, ...]) -> LoadedSet:
        cases = tuple(_judgment_case(query_id, PRIVATE) for query_id in sorted({row.query_id for row in rows}))
        _write(self.root, "judgments/queries.jsonl", "\n".join(json.dumps(case) for case in cases))
        _write(self.root, judgments.JUDGMENTS_PATH.as_posix(),
               "".join(judgments.serialize(row) for row in rows).rstrip("\n"))
        return self.load()

    def run_slice(self, loaded: LoadedSet | None = None, context: RunContext | None = None) -> SliceResult:
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = golddrop_slice.run_gold_drop(
                self.loaded if loaded is None else loaded, "gold-drop",
                _context(self.host) if context is None else context,
            )
        self.output, self.errors = output.getvalue(), errors.getvalue()
        self.assertEqual((self.output, self.errors), ("", ""))
        self.assertNotIn(PRIVATE, repr(result.results) + result.report + self.output + self.errors)
        self.assertNotIn("Traceback", result.report + self.output + self.errors)
        return result


class Runner(GoldDropFixture):
    """The runner reads the record once and reports ids and hashes, never answers."""

    def test_manifest_uses_the_selected_stack_reader_and_stdin_bindings_only(self) -> None:
        context = _context(self.host)
        checkpoints: list[int] = []
        checkpoint = Mock(side_effect=lambda: checkpoints.append(len(self.host.calls)))
        result = self.run_slice(context=replace(context, checkpoint=checkpoint))
        self.assertTrue(result.verdict, result.report)
        self.assertEqual(result.gated_cases, sum(
            SHAPE_REGISTRY[cast(
                tuple[str, str],
                (self.loaded.cases_by_id[case_id]["suite"], self.loaded.cases_by_id[case_id]["category"]),
            )].zero_tolerance
            for case_id in select_cases(self.loaded, "gold-drop").counted
        ))
        self.assertIn(
            "zero-tolerance cases dropped 0: none\n"
            "1 case, true failure rate ≤ 100.0 % (3/n at 95 %)\n",
            result.report,
        )
        self.assertGreaterEqual(checkpoint.call_count, 1)
        self.assertEqual(checkpoints[0], 0)
        self.assertEqual(len(self.host.calls), 1)
        argv, sql = self.host.calls[0]
        self.assertEqual(argv, tuple(record.reader_argv(context.turns_dir)))
        assert context.generation is not None and sql is not None
        for variable, expected in (
            ("v_name", context.generation.name),
            ("v_source_ids", json.dumps(list(sample.gold_hashes(self.loaded)), separators=(",", ":"))),
        ):
            line = next(line for line in sql.splitlines() if line.startswith(f"\\set {variable} "))
            self.assertEqual(read_psql_set(line, variable), expected)
        self.assertNotIn(context.generation.name, " ".join(argv))
        self.assertTrue(all(source_id not in " ".join(argv) for source_id in self.host.manifest))
        self.assertIn("public.generation_documents", sql)
        self.assertIn("row_to_json", sql)
        self.assertIn("generation " + context.generation.name, result.report)
        self.assertIn(context.generation.corpus_lockfile, result.report)

    def test_planted_judged_coordinate_absent_fails_gate_and_names_query_only_in_report(self) -> None:
        judged = replace(_judgment(), source_id="fictional/judged-only", sha256="b" * 64)
        loaded = self.with_judgments((judged,))
        self.host.manifest = _manifest(loaded)
        del self.host.manifest[judged.source_id]
        result = self.run_slice(loaded)
        self.assertFalse(result.verdict)
        self.assertTrue(all(row.verdict == "pass" for row in result.results))
        self.assertEqual(result.gated_cases, 1)
        self.assertIn(
            "zero-tolerance cases dropped 0: none\n"
            "1 case, failed: no bound (3/n at 95 % is a clean run's)\n",
            result.report,
        )
        self.assertEqual(tuple(row.case_id for row in result.results), select_cases(loaded, "gold-drop").counted)
        self.assertIn(f"{judged.query_id}: {judged.source_id}@{judged.sha256}", result.report)
        self.assertIn("gold-drop: fail", result.report)

    def test_missing_lookup_or_edition_must_cite_fails_its_row_and_gate(self) -> None:
        for category in ("lookup", "edition"):
            with self.subTest(category=category):
                cases = tuple(
                    _research_qa_case(item.case_id, category if index == 0 else "synthesis", PRIVATE)
                    for index, item in enumerate(self.signed)
                )
                _write_research_cases(self.root, cases)
                _write(self.root, "slices/gold-drop/fictitious.ids", "\n".join(item.case_id for item in self.signed))
                loaded = self.load()
                self.host.manifest = _manifest(loaded)
                missing = self.signed[0].must_cite[0]
                del self.host.manifest[missing]
                result = self.run_slice(loaded)
                row = next(row for row in result.results if row.case_id == self.signed[0].case_id)
                self.assertFalse(result.verdict)
                self.assertEqual(row.verdict, "fail")
                self.assertEqual(result.gated_cases, sum(
                    SHAPE_REGISTRY[cast(
                        tuple[str, str],
                        (loaded.cases_by_id[case_id]["suite"], loaded.cases_by_id[case_id]["category"]),
                    )].zero_tolerance
                    for case_id in select_cases(loaded, "gold-drop").counted
                ))
                self.assertIn(
                    "1 case, failed: no bound (3/n at 95 % is a clean run's)\n",
                    result.report,
                )
                self.assertEqual(row.metrics, {"must_cite": 1, "dropped": [missing], "zero_tolerance": True})
                self.assertIn(f"zero-tolerance cases dropped 1: {row.case_id}", result.report)

    def test_synthesis_drop_among_twenty_gold_fails_row_but_passes_gate(self) -> None:
        judged = tuple(replace(_judgment(), source_id=f"fictional/source-{index}") for index in range(1, 21))
        loaded = self.with_judgments(judged)
        self.host.manifest = _manifest(loaded)
        missing = self.signed[1].must_cite[0]
        del self.host.manifest[missing]
        result = self.run_slice(loaded)
        self.assertTrue(result.verdict, result.report)
        row = next(row for row in result.results if row.case_id == self.signed[1].case_id)
        self.assertEqual((row.verdict, row.metrics["dropped"], row.metrics["zero_tolerance"]), ("fail", [missing], False))
        self.assertIn("gold documents 20: dropped 1 (absent 1, hash changed 0), ceiling 5 %", result.report)
        self.assertIn(f"cases dropped (reported) 1: {row.case_id}", result.report)
        self.assertIn("zero-tolerance cases dropped 0: none", result.report)

    def test_registry_flag_controls_the_zero_tolerance_gate(self) -> None:
        judged = tuple(replace(_judgment(), source_id=f"fictional/source-{index}") for index in range(1, 21))
        loaded = self.with_judgments(judged)
        for category, index in (("lookup", 0), ("synthesis", 1)):
            key = ("research-qa", category)
            spec = SHAPE_REGISTRY[key]
            for zero_tolerance in (False, True):
                with self.subTest(category=category, zero_tolerance=zero_tolerance):
                    self.host.manifest = _manifest(loaded)
                    missing = self.signed[index].must_cite[0]
                    del self.host.manifest[missing]
                    with patch.dict(SHAPE_REGISTRY, {key: replace(spec, zero_tolerance=zero_tolerance)}):
                        result = self.run_slice(loaded)
                    row = next(row for row in result.results if row.case_id == self.signed[index].case_id)
                    self.assertEqual(row.verdict, "fail")
                    self.assertEqual(row.metrics["dropped"], [missing])
                    self.assertIs(row.metrics["zero_tolerance"], zero_tolerance)
                    self.assertEqual(result.verdict, not zero_tolerance)

    def test_changed_judged_hash_drops_the_document_and_must_cite_case(self) -> None:
        judged = replace(_judgment(), source_id=self.signed[0].must_cite[0])
        loaded = self.with_judgments((judged,))
        self.host.manifest = _manifest(loaded)
        self.host.manifest[judged.source_id] = "b" * 64
        result = self.run_slice(loaded)
        self.assertFalse(result.verdict)
        self.assertEqual(result.results[0].verdict, "fail")
        self.assertIn("dropped 1 (absent 0, hash changed 1)", result.report)
        self.assertIn("absent source ids: none", result.report)
        self.assertIn(f"hash changed source ids: {judged.source_id}", result.report)

    def sample_context(self) -> RunContext:
        context = _context(self.host)
        assert context.generation is not None
        return replace(context, generation=replace(
            context.generation, name=sample_name(context.generation.name),
        ))

    def test_sample_reports_gold_absent_from_it_unbuilt_and_counts_the_rest(self) -> None:
        judged = replace(_judgment(), source_id="fictional/judged-only", sha256="b" * 64)
        loaded = self.with_judgments((judged,))
        self.host.manifest = _manifest(loaded)
        missing = self.signed[0].must_cite[0]
        del self.host.manifest[missing]
        del self.host.manifest[judged.source_id]
        result = self.run_slice(loaded, self.sample_context())
        self.assertTrue(result.verdict, result.report)
        self.assertTrue(all(row.verdict == "pass" for row in result.results))
        unbuilt = " ".join(sorted((missing, judged.source_id)))
        self.assertIn(
            f"unbuilt gold documents 2 (absent from the sample, not counted): {unbuilt}\n"
            f"gold documents {len(sample.gold_hashes(loaded)) - 2}: dropped 0 (absent 0, hash changed 0), ",
            result.report,
        )
        self.assertIn("judged queries with dropped coordinates 0:\n", result.report)
        self.assertIn("gold-drop: pass", result.report)
        self.assertNotIn("unbuilt", self.run_slice(loaded).report)

    def test_sample_still_drops_a_changed_hash(self) -> None:
        judged = replace(_judgment(), source_id=self.signed[0].must_cite[0])
        loaded = self.with_judgments((judged,))
        self.host.manifest = _manifest(loaded)
        self.host.manifest[judged.source_id] = "b" * 64
        result = self.run_slice(loaded, self.sample_context())
        self.assertFalse(result.verdict)
        self.assertEqual(result.results[0].verdict, "fail")
        self.assertIn("unbuilt gold documents 0 (absent from the sample, not counted): none\n", result.report)
        self.assertIn("dropped 1 (absent 0, hash changed 1)", result.report)

    def test_two_queries_two_hashes_drop_one_document_and_only_lost_coordinates(self) -> None:
        rows = (
            replace(_judgment("judgments-001"), source_id="fictional/shared", sha256="a" * 64),
            replace(_judgment("judgments-002"), source_id="fictional/shared", sha256="b" * 64),
        )
        loaded = self.with_judgments(rows)
        for present in (True, False):
            with self.subTest(present=present):
                self.host.manifest = _manifest(loaded)
                if present:
                    self.host.manifest[rows[0].source_id] = rows[0].sha256
                else:
                    del self.host.manifest[rows[0].source_id]
                result = self.run_slice(loaded)
                self.assertFalse(result.verdict)
                self.assertIn(f"gold documents {len(sample.gold_hashes(loaded))}: dropped 1 ", result.report)
                query_lines = tuple(line for line in result.report.splitlines() if line.startswith("judgments-"))
                lost = rows[1:] if present else rows
                self.assertEqual(query_lines, tuple(f"{row.query_id}: {row.source_id}@{row.sha256}" for row in lost))
                self.assertIn(f"judged queries with dropped coordinates {len(lost)}:", result.report)
                self.assertIn("absent source ids: " + ("none" if present else rows[0].source_id), result.report)
                self.assertIn("hash changed source ids: " + (rows[0].source_id if present else "none"), result.report)

    def test_unsigned_cases_are_excluded_and_signed_answer_reaches_no_result_or_stream(self) -> None:
        result = self.run_slice()
        selection = select_cases(self.loaded, "gold-drop")
        self.assertTrue(result.verdict, result.report)
        self.assertEqual(tuple(row.case_id for row in result.results), selection.counted)
        self.assertTrue(selection.unsigned)
        self.assertTrue(all(row.case_id not in selection.unsigned for row in result.results))
        self.assertIn(f"{len(selection.counted)} counted, {len(selection.unsigned)} unsigned excluded", result.report)
        self.assertNotIn(PRIVATE, repr(result) + self.output + self.errors)

    def test_zero_gold_all_unsigned_passes_with_zero_counts(self) -> None:
        (self.root / signoffs.SIGNOFFS_PATH).unlink()
        loaded = self.load()
        self.host.manifest = {}
        result = self.run_slice(loaded)
        self.assertTrue(result.verdict, result.report)
        self.assertEqual(result.results, ())
        self.assertEqual(result.gated_cases, 0)
        self.assertIn("zero-tolerance cases dropped 0: none\n0 cases, no bound\n", result.report)
        self.assertIn("0 counted, 3 unsigned excluded", result.report)
        self.assertIn("gold documents 0: dropped 0 (absent 0, hash changed 0), ceiling 5 %", result.report)

    def test_unresolved_generation_fails_every_counted_case_without_a_read(self) -> None:
        result = self.run_slice(context=replace(_context(self.host), generation=None))
        self.assertFalse(result.verdict)
        self.assertIsNone(result.gated_cases)
        self.assertNotIn("3/n", result.report)
        self.assertEqual(tuple(row.case_id for row in result.results), select_cases(self.loaded, "gold-drop").counted)
        self.assertTrue(all(row.verdict == "fail" and row.metrics == {"problem": "generation-unresolved"} for row in result.results))
        self.assertIn("Fix: Run " + report.command("eval run --slice gold-drop --generation <name>"), result.report)
        self.assertEqual(self.host.calls, [])

    def test_failed_unavailable_or_invalid_manifest_fails_every_counted_case_with_fix(self) -> None:
        identifier = self.signed[0].must_cite[0]
        valid = {"source_id": identifier, "canonical_text_sha256": MANIFEST_HASH}
        invalid: tuple[object, ...] = (
            [], {**valid, "answer": PRIVATE}, {"source_id": identifier},
            {**valid, "source_id": "fictional/not-requested"},
            {**valid, "source_id": [identifier]}, {**valid, "canonical_text_sha256": "invalid"},
        )
        scenarios = (
            (1, None, None), (127, None, None),
            (0, OSError(PRIVATE), None), (0, subprocess.TimeoutExpired("fictitious psql", 1), None),
            (0, None, "fictitious malformed JSON\n"),
            (0, None, json.dumps(valid) + "\n" + json.dumps(valid) + "\n"),
            *((0, None, json.dumps(value) + "\n") for value in invalid),
        )
        for code, error, reply in scenarios:
            with self.subTest(code=code, error=type(error).__name__, reply=reply):
                self.host = GoldDropHost()
                self.host.manifest = _manifest(self.loaded)
                self.host.manifest_code, self.host.manifest_error, self.host.manifest_stdout = code, error, reply
                result = self.run_slice()
                self.assertFalse(result.verdict)
                self.assertIsNone(result.gated_cases)
                self.assertNotIn("3/n", result.report)
                self.assertEqual(tuple(row.case_id for row in result.results), select_cases(self.loaded, "gold-drop").counted)
                self.assertTrue(all(row.verdict == "fail" and row.metrics == {"problem": "record-unreachable"} for row in result.results))
                self.assertIn("record-unreachable: generation manifest", result.report)
                self.assertIn("Fix: Run " + stack.logs_fix(_context(self.host).turns_dir, record.POSTGRES_SERVICE), result.report)
                self.assertEqual(len(self.host.calls), 1)


class Command(GoldDropFixture):
    """The real command resolves, runs, and records the gate over the fake record."""

    def test_named_generation_records_its_two_fields_and_keeps_the_answer_private(self) -> None:
        row = _generation_row(state="built")
        row["corpus_lockfile"] = "corpus-2099-01-02"
        self.host.generation_output = json.dumps(row) + "\n"
        with patch("gideon.evaluation.stacks.secrets.select_directory"):
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "gold-drop", "--stack", CI_STACK, "--generation", row["gen_id"]],
                **_run_kwargs(self.host, checkout=self.checkout, court_path=self.checkout / "courts.yaml"),
            )
        self.assertEqual((code, stderr), (0, ""), stdout)
        writes = [sql for _argv, sql in self.host.calls if "INSERT INTO eval_runs" in (sql or "")]
        self.assertEqual(len(writes), 1)
        sql = writes[0]
        assert sql is not None
        for variable, expected in (("corpus_lockfile", row["corpus_lockfile"]), ("generation_id", row["gen_id"])):
            line = next(line for line in sql.splitlines() if line.startswith(f"\\set {variable} "))
            self.assertEqual(read_psql_set(line, variable), expected)
            self.assertIn(f":'{variable}'", sql)
        reads = [argv for argv, sql in self.host.calls if "generation_documents" in (sql or "")]
        self.assertEqual(reads, [tuple(record.reader_argv(CI_ROOT))])
        self.assertIn("record: ok — run", stdout)
        self.assertIn("gold-drop: pass", stdout)
        self.assertIn("unsigned excluded", stdout)
        self.assertNotIn(PRIVATE, stdout + stderr + sql)

    def test_unnamed_sibling_sample_absent_refuses_preconditions_with_fix(self) -> None:
        _copy_sibling_artifacts(self.checkout)
        row = _sibling_row(self.checkout)
        with patch("gideon.evaluation.stacks.secrets.select_directory"):
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "gold-drop", "--stack", CI_STACK],
                **_run_kwargs(self.host, checkout=self.checkout, court_path=self.checkout / "courts.yaml"),
            )
        self.assertEqual((code, stderr), (1, ""))
        self.assertEqual(stdout.count("preconditions:"), 1)
        self.assertIn(f"generation {row['gen_id']} is not recorded on the {CI_STACK} stack", stdout)
        self.assertIn(report.command(f"index build {COLLECTION} --sample --lockfile {row['corpus_lockfile']}"), stdout)
        self.assertIn(report.command(f"eval run --slice gold-drop --stack {CI_STACK}") + " --generation <name>", stdout)
        self.assertNotIn("run: ok", stdout)
        self.assertNotIn("record:", stdout)
        self.assertNotIn("Traceback", stdout)
        self.assertFalse(any("generation_documents" in (sql or "") or "INSERT INTO" in (sql or "") for _argv, sql in self.host.calls))

    def test_generation_flag_for_guardrails_refuses_before_any_row(self) -> None:
        code, stdout, stderr = _invoke(
            ["eval", "run", "--slice", "guardrails", "--generation", _generation_row()["gen_id"]],
            **_run_kwargs(self.host, checkout=self.checkout),
        )
        self.assertEqual((code, stdout, self.host.calls), (1, "", []))
        self.assertIn("which reads no generation", stderr)
        self.assertIn("Fix: Remove --generation when running --slice guardrails, then retry.", stderr)
        self.assertNotIn("Traceback", stderr)


class Imports(unittest.TestCase):
    def test_every_import_is_standard_library_or_gideon(self) -> None:
        path = Path(golddrop_slice.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                names = (node.module or "",)
            else:
                continue
            for name in names:
                with self.subTest(name=name, line=node.lineno):
                    self.assertIn(name.split(".", 1)[0], {*sys.stdlib_module_names, "gideon"})
