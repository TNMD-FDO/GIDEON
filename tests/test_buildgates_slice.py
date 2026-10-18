"""Build-gates routing, reports, frozen lists, and recorded command outcomes."""

import ast
import json
import sys
import unittest
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import test_golddrop_slice
from test_evaluation_record import read_psql_set
from test_evaluation_run import ROOT, _generation_row, _invoke, _run_kwargs
from test_evaluation_set import (
    _case,
    _segmenter_case,
    _segmenter_label,
    _write,
    _write_segmenter_labels,
    count_gate_case,
)
from test_smoke_slice import _load

from gideon.evaluation import buildgates_slice, command, sample, smoke_slice
from gideon.evaluation.evalset import SET_ROOT, LoadedSet, select_cases
from gideon.evaluation.results import CallSurface, CaseResult, RunContext, SliceResult
from gideon.evaluation.slices import BUILD_GATES_PARTS, SLICE_RUNNERS, SliceSpec
from tools.exportboundary import absent_from_export

PRIVATE = "[FICTIONAL TEST ONLY: build-gates question and answer]"


def _parts(
    calls: list[tuple[str, LoadedSet]], *, failed: str | None = None,
) -> dict[str, SliceSpec]:
    """Record each part's input and return a planted content-free verdict."""

    def runner(name: str) -> Callable[[LoadedSet, str, RunContext], SliceResult]:
        def run(loaded: LoadedSet, slice_name: str, _context: RunContext) -> SliceResult:
            calls.append((name, loaded))
            return SliceResult(
                name != failed,
                f"fictitious {name} figures\n",
                tuple(
                    CaseResult(case_id, 1, "pass" if name != failed else "fail", {})
                    for case_id in select_cases(loaded, slice_name).counted
                ),
            )

        return run

    return {name: replace(spec, runner=runner(name)) for name, spec in BUILD_GATES_PARTS.items()}


class BuildGatesFixture(test_golddrop_slice.GoldDropFixture):
    """Load a small signed set with cases for every composite part."""

    def setUp(self) -> None:
        super().setUp()
        cases = (
            json.loads(_case("extraction-001", PRIVATE)),
            json.loads(_case("extraction-002", PRIVATE)),
            _segmenter_case(),
            count_gate_case(),
        )
        _write(self.root, "build-gates/fictitious.jsonl", "\n".join(json.dumps(case) for case in cases))
        _write_segmenter_labels(self.root, (_segmenter_label(),))
        _write(
            self.root, "slices/build-gates/fictitious.ids",
            "\n".join((*self.loaded.active_ids, *(str(case["id"]) for case in cases))),
        )
        self.loaded = self.load()
        self.calls: list[tuple[str, LoadedSet]] = []

    def run_composite(
        self, *, loaded: LoadedSet | None = None, failed: str | None = None,
        parts: Mapping[str, SliceSpec] | None = None,
    ) -> SliceResult:
        composite = smoke_slice.Composite(
            _parts(self.calls, failed=failed) if parts is None else parts,
            report=buildgates_slice.report,
        )
        return composite.run(
            self.loaded if loaded is None else loaded, "build-gates",
            test_golddrop_slice._context(self.host),
        )


class Routing(BuildGatesFixture):
    """Every counted case reaches its owner once, in the handed parts' order."""

    def test_parts_receive_only_their_cases_and_preserve_the_whole_sets_gold(self) -> None:
        result = self.run_composite()
        self.assertTrue(result.verdict, result.report)
        self.assertEqual(tuple(name for name, _loaded in self.calls), tuple(BUILD_GATES_PARTS))
        counted = select_cases(self.loaded, "build-gates").counted
        expected_order: list[str] = []
        for name, narrowed in self.calls:
            with self.subTest(part=name):
                expected = tuple(
                    case_id for case_id in counted
                    if (
                        self.loaded.cases_by_id[case_id]["suite"],
                        self.loaded.cases_by_id[case_id]["category"],
                    ) in BUILD_GATES_PARTS[name].categories
                )
                self.assertTrue(expected)
                self.assertEqual(select_cases(narrowed, "build-gates").counted, expected)
                self.assertEqual(narrowed.slices["build-gates"], expected)
                self.assertCountEqual(
                    tuple(case_id for ids in narrowed.slice_lists["build-gates"].values() for case_id in ids),
                    expected,
                )
                self.assertEqual(sample.gold_hashes(narrowed), sample.gold_hashes(self.loaded))
                expected_order.extend(expected)
        self.assertEqual(tuple(row.case_id for row in result.results), tuple(expected_order))
        self.assertCountEqual((row.case_id for row in result.results), counted)

    def test_parts_follow_the_handed_mapping_order(self) -> None:
        parts = dict(reversed(tuple(_parts(self.calls).items())))
        result = self.run_composite(parts=parts)
        self.assertTrue(result.verdict, result.report)
        self.assertEqual(tuple(name for name, _loaded in self.calls), tuple(parts))
        self.assertEqual(
            tuple(row.case_id for row in result.results),
            tuple(
                case_id for _name, narrowed in self.calls
                for case_id in select_cases(narrowed, "build-gates").counted
            ),
        )

    def test_stores_parts_run_even_when_no_case_routes_to_them(self) -> None:
        counted = select_cases(self.loaded, "build-gates").counted
        stores_categories = frozenset().union(*(
            spec.categories for spec in BUILD_GATES_PARTS.values()
            if CallSurface.STORES in spec.surfaces
        ))
        kept = tuple(
            case_id for case_id in counted
            if (
                self.loaded.cases_by_id[case_id]["suite"],
                self.loaded.cases_by_id[case_id]["category"],
            ) not in stores_categories
        )
        loaded = replace(self.loaded, slices={**self.loaded.slices, "build-gates": kept})
        result = self.run_composite(loaded=loaded)
        self.assertTrue(result.verdict, result.report)
        self.assertEqual(tuple(name for name, _loaded in self.calls), tuple(BUILD_GATES_PARTS))
        for name, narrowed in self.calls:
            if CallSurface.STORES in BUILD_GATES_PARTS[name].surfaces:
                with self.subTest(part=name):
                    self.assertEqual(select_cases(narrowed, "build-gates").counted, ())
        self.assertCountEqual((row.case_id for row in result.results), kept)

    def test_each_failing_part_fails_the_suite_by_name(self) -> None:
        for name in BUILD_GATES_PARTS:
            with self.subTest(part=name):
                self.calls.clear()
                result = self.run_composite(failed=name)
                self.assertFalse(result.verdict)
                self.assertIn(f"  {name} fail\n", result.report)
                self.assertIn("build-gates: fail;", result.report)
                self.assertEqual(tuple(part for part, _loaded in self.calls), tuple(BUILD_GATES_PARTS))

    def test_unrouted_counted_case_fails_by_id(self) -> None:
        case_id = "extraction-001"
        cases = dict(self.loaded.cases_by_id)
        cases[case_id] = {**cases[case_id], "category": "fictitious-unrouted"}
        result = self.run_composite(loaded=replace(self.loaded, cases_by_id=cases))
        self.assertFalse(result.verdict)
        self.assertIn(f"unrouted suite cases: {case_id}\n", result.report)
        rows = tuple(row for row in result.results if row.case_id == case_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].verdict, rows[0].metrics), ("fail", {"problem": "unrouted-suite"}))

    def test_nonstores_part_without_counted_cases_is_not_run_and_fails(self) -> None:
        for name, spec in BUILD_GATES_PARTS.items():
            if CallSurface.STORES in spec.surfaces:
                continue
            with self.subTest(part=name):
                self.calls.clear()
                kept = tuple(
                    case_id for case_id in select_cases(self.loaded, "build-gates").counted
                    if (
                        self.loaded.cases_by_id[case_id]["suite"],
                        self.loaded.cases_by_id[case_id]["category"],
                    ) not in spec.categories
                )
                loaded = replace(self.loaded, slices={**self.loaded.slices, "build-gates": kept})
                result = self.run_composite(loaded=loaded)
                self.assertFalse(result.verdict)
                self.assertNotIn(name, tuple(part for part, _loaded in self.calls))
                self.assertIn(f"{name}: not run\n", result.report)
                self.assertIn(f"  {name} not run\n", result.report)

    def test_case_text_and_fix_lines_are_absent_from_report_and_rows(self) -> None:
        result = self.run_composite()
        self.assertNotIn("Fix:", result.report)
        self.assertNotIn(PRIVATE, result.report + repr(result.results))
        for case in self.loaded.cases_by_id.values():
            question = case.get("question")
            if isinstance(question, str):
                self.assertNotIn(question, result.report + repr(result.results))
        for signed in self.loaded.signoffs:
            self.assertNotIn(signed.answer, result.report + repr(result.results))


class Reports(unittest.TestCase):
    """Every required part gates, and unknown counts remain distinct from zero."""

    def test_report_order_counts_and_all_parts_passing(self) -> None:
        for counts in ((2, None, 3, None), (0, None, None, None), (None,) * 4):
            with self.subTest(counts=counts):
                parts = {
                    name: SliceResult(True, f"fictitious {name} figures\n", (), gated_cases=count)
                    for name, count in reversed(tuple(zip(buildgates_slice.PART_ORDER, counts, strict=True)))
                }
                rows = (CaseResult("fictitious-case", 1, "pass", {"text": PRIVATE}),)
                verdict, text, gated_cases = buildgates_slice.report(parts, rows, ())
                self.assertTrue(verdict, text)
                known = tuple(count for count in counts if count is not None)
                self.assertEqual(gated_cases, sum(known) if known else None)
                expected = [
                    line for name in buildgates_slice.PART_ORDER
                    for line in (f"{name} report (its verdict is build-gates'):", f"fictitious {name} figures")
                ]
                expected.append("unrouted suite cases: none")
                expected.extend(f"  {name} pass" for name in buildgates_slice.PART_ORDER)
                expected.append(f"build-gates: pass; {len(rows)} cases")
                self.assertEqual(text, "\n".join(expected) + "\n")
                self.assertNotIn(PRIVATE, text)
                self.assertNotIn("Fix:", text)

    def test_every_absent_part_fails_including_when_no_part_ran(self) -> None:
        complete = {name: SliceResult(True, f"fictitious {name} figures\n", ()) for name in BUILD_GATES_PARTS}
        for absent in (*BUILD_GATES_PARTS, None):
            with self.subTest(absent=absent):
                parts = {name: result for name, result in complete.items() if name != absent} if absent else {}
                verdict, text, gated_cases = buildgates_slice.report(parts, (), ())
                self.assertFalse(verdict)
                self.assertIsNone(gated_cases)
                for name in BUILD_GATES_PARTS:
                    if name not in parts:
                        self.assertIn(f"{name}: not run\n", text)
                        self.assertIn(f"  {name} not run\n", text)
                self.assertIn("build-gates: fail; 0 cases\n", text)


class RegistryAndLists(unittest.TestCase):
    """The composite entry and frozen lists describe exactly its parts."""

    def test_order_surfaces_and_gate_settings(self) -> None:
        self.assertEqual(buildgates_slice.PART_ORDER, tuple(BUILD_GATES_PARTS))
        spec = SLICE_RUNNERS["build-gates"]
        self.assertEqual(spec.surfaces, frozenset().union(*(part.surfaces for part in BUILD_GATES_PARTS.values())))
        self.assertFalse(spec.compares_reference)
        self.assertEqual(spec.categories, frozenset())
        self.assertIsNone(spec.engine_calls)

    def test_committed_lists_load_and_mirror_parts_with_no_gold_drop_list(self) -> None:
        loaded = _load(ROOT / SET_ROOT)
        sources = {
            "sample": "segmenter-300", "rules": "count-gates",
            "harvest": "extraction", "invented": "extraction",
        }
        kept: set[str] = set()
        for list_name, part in sources.items():
            relative = SET_ROOT / "slices" / "build-gates" / f"{list_name}.ids"
            with self.subTest(list_name=list_name):
                if absent_from_export(relative, ROOT):
                    continue
                kept.add(list_name)
                source = ROOT / SET_ROOT / "slices" / part / f"{list_name}.ids"
                self.assertEqual((ROOT / relative).read_bytes(), source.read_bytes())
                self.assertEqual(loaded.slice_lists["build-gates"][list_name], loaded.slice_lists[part][list_name])
        self.assertEqual(set(loaded.slice_lists["build-gates"]), kept)
        self.assertTrue(select_cases(loaded, "build-gates").counted)
        for case_id in loaded.slices["build-gates"]:
            case = loaded.cases_by_id[case_id]
            self.assertNotIn((case["suite"], case["category"]), BUILD_GATES_PARTS["gold-drop"].categories)


class Command(BuildGatesFixture):
    """Both gate outcomes produce one recorded run through the CLI."""

    def test_passing_and_failing_parts_record_one_run(self) -> None:
        for failed in (None, *BUILD_GATES_PARTS):
            with self.subTest(failed=failed):
                host = test_golddrop_slice.GoldDropHost()
                row = _generation_row(state="built")
                host.generation_output = json.dumps(row) + "\n"
                self.calls.clear()
                composite = smoke_slice.Composite(_parts(self.calls, failed=failed), report=buildgates_slice.report)
                registry = {**SLICE_RUNNERS, "build-gates": replace(SLICE_RUNNERS["build-gates"], runner=composite.run)}
                with (
                    patch.object(command, "SLICE_RUNNERS", registry),
                    patch.object(command.stacks.secrets, "select_directory"),
                ):
                    code, stdout, stderr = _invoke(
                        ["eval", "run", "--slice", "build-gates", "--stack", "ci", "--generation", row["gen_id"]],
                        **_run_kwargs(host, checkout=self.checkout, court_path=self.checkout / "courts.yaml"),
                    )
                self.assertEqual((code, stderr), (0 if failed is None else 1, ""), stdout)
                verdict = "pass" if failed is None else "fail"
                self.assertIn(f"build-gates: {verdict};", stdout)
                if failed is not None:
                    self.assertIn(f"  {failed} fail\n", stdout)
                    self.assertIn(SLICE_RUNNERS["build-gates"].gate_fix, stdout)
                self.assertEqual(tuple(name for name, _loaded in self.calls), tuple(BUILD_GATES_PARTS))
                writes = [sql for _argv, sql in host.calls if sql is not None and "INSERT INTO eval_runs" in sql]
                self.assertEqual(len(writes), 1)
                sql = writes[0]
                self.assertEqual(sql.count("INSERT INTO eval_runs"), 1)
                self.assertEqual(sql.count("INSERT INTO eval_results"), len(select_cases(self.loaded, "build-gates").counted))
                line = next(line for line in sql.splitlines() if line.startswith("\\set run_verdict "))
                self.assertEqual(read_psql_set(line, "run_verdict"), verdict)
                self.assertIn("record: ok — run", stdout)
                self.assertNotIn("reference:", stdout)
                self.assertNotIn(PRIVATE, stdout + stderr + sql)
                self.assertNotIn(test_golddrop_slice.PRIVATE, stdout + stderr + sql)


class Imports(unittest.TestCase):
    def test_every_import_is_standard_library_or_gideon(self) -> None:
        path = Path(buildgates_slice.__file__)
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
