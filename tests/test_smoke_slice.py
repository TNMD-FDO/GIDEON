"""Composite smoke runner and its frozen input contract."""

from __future__ import annotations

import ast
import os
import shutil
import tempfile
import unittest
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from test_evaluation_run import EvalHost, _invoke, _run_kwargs
from test_guardrails_slice import JudgingDoorHost, _fixture_turns

import gideon
from gideon.evaluation import (
    command,
    extraction_slice,
    guardrails_slice,
    reference,
    smoke_slice,
    stacks,
)
from gideon.evaluation.evalset import SET_ROOT, LoadedSet, load_set, select_cases
from gideon.evaluation.results import CaseResult, JSONValue, RunContext, SliceResult
from gideon.evaluation.slices import SLICE_RUNNERS
from gideon.evaluation.turns import run as turn_run
from gideon.host.sysio import PathLike
from tools.exportboundary import absent_from_export

ROOT = Path(__file__).resolve().parents[1]
_SAMPLE = guardrails_slice.FRONTEND_SAMPLE
_SMOKE_LIST = Path("slices/smoke")


def _load(root: Path) -> LoadedSet:
    result = load_set(root)
    assert result.ok, tuple(finding.text() for finding in result.findings)
    assert result.loaded is not None
    return result.loaded


def _temporary_smoke_set(root: Path) -> tuple[Path, LoadedSet]:
    """Copy the set and narrow smoke's lists to a small real-artifact fixture."""

    target = root / SET_ROOT.name
    shutil.copytree(ROOT / SET_ROOT, target)
    source = _load(target)
    grouped: dict[str, list[str]] = {}
    for case_id in _SAMPLE:
        case = source.cases_by_id[case_id]
        grouped.setdefault(cast(str, case["category"]), []).append(case_id)
    for family, case_ids in grouped.items():
        (target / _SMOKE_LIST / f"{family}.ids").write_text(
            "".join(f"{case_id}\n" for case_id in case_ids), encoding="utf-8"
        )

    active = set(source.active_ids)
    extraction_lists = source.slice_lists["extraction"]
    for list_name, source_ids in extraction_lists.items():
        candidates = tuple(
            case_id
            for case_id in source_ids
            if case_id in active
            and cast(dict[str, object], source.cases_by_id[case_id].get("expected", {})).get(
                "objects"
            )
        )
        destination = target / _SMOKE_LIST / f"{list_name}.ids"
        if not destination.is_file():
            continue
        if candidates:
            destination.write_text(f"{candidates[0]}\n", encoding="utf-8")
    loaded = _load(target)
    return target, loaded


def _context(
    loaded: LoadedSet, *, host: JudgingDoorHost | None = None
) -> tuple[JudgingDoorHost, RunContext]:
    host = JudgingDoorHost() if host is None else host
    guardrails = tuple(
        case_id
        for case_id in loaded.active_ids
        if loaded.cases_by_id[case_id].get("suite") == "guardrails"
    )
    _host, _frontend, context = _fixture_turns(
        replace(loaded, active_ids=guardrails), host=host
    )
    return host, replace(context, judge_prompt_id=None)


def _mutating_guardrails(
    case_id: str,
    mutation: Callable[[CaseResult], CaseResult],
) -> Callable[[LoadedSet, str, RunContext], SliceResult]:
    original = guardrails_slice.run_guardrails

    def run(
        loaded: LoadedSet, slice_name: str, context: RunContext
    ) -> SliceResult:
        result = original(loaded, slice_name, context)
        rows = tuple(
            mutation(row) if row.case_id == case_id else row for row in result.results
        )
        return replace(result, results=rows)

    return run


class CommandDoorHost(JudgingDoorHost):
    """The fake door host with the lock and sibling tree used by eval run."""

    def __init__(self) -> None:
        super().__init__()
        self.locks: dict[str, str] = {}

    def exists(self, path: PathLike) -> bool:
        if Path(path) == Path(stacks.CI_ROOT) / "compose.yaml":
            return True
        return Path(path).exists() or super().exists(path)

    def listdir(self, path: PathLike) -> list[str]:
        if Path(path).is_dir():
            return sorted(child.name for child in Path(path).iterdir())
        return super().listdir(path)

    def stat(self, path: PathLike) -> os.stat_result:
        if Path(path).exists():
            return Path(path).stat()
        return super().stat(path)

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        try:
            return super().read_text(path, encoding=encoding)
        except FileNotFoundError:
            return Path(path).read_text(encoding=encoding)

    def take_lock(self, path: PathLike, record: str) -> str | None:
        key = os.fspath(path)
        holder = self.locks.get(key)
        if holder is None:
            self.locks[key] = record
        return holder

    def release_lock(self, path: PathLike) -> None:
        self.locks.pop(os.fspath(path), None)


class SmokeRunner(unittest.TestCase):
    """The composite gates only its zero-tolerance guardrail facts."""

    def test_each_zero_tolerance_cause_fails_by_id(self) -> None:
        for cause in ("unblocked", "leaked", "errored", "disagreeing"):
            with self.subTest(cause=cause), tempfile.TemporaryDirectory() as directory:
                _root, loaded = _temporary_smoke_set(Path(directory))
                host, context = _context(loaded)
                positive = next(
                    case_id
                    for case_id in _SAMPLE
                    if cast(list[str], loaded.cases_by_id[case_id]["labels"])[1]
                    == "positive"
                )

                def mutation(row: CaseResult, failure: str = cause) -> CaseResult:
                    metrics = dict(row.metrics)
                    if failure == "unblocked":
                        metrics["class"] = "answered"
                        metrics.pop("checks", None)
                    elif failure == "leaked":
                        metrics["stream"] = "leak"
                    elif failure == "errored":
                        metrics["problem"] = next(iter(guardrails_slice.PROBLEMS))
                    else:
                        frontend = cast(Mapping[str, JSONValue], metrics["frontend"])
                        metrics["frontend"] = {**frontend, "agrees": False}
                    return replace(row, metrics=metrics)

                with patch.dict(smoke_slice._RUNNERS, {"guardrails": _mutating_guardrails(positive, mutation)}):
                    result = smoke_slice.run_smoke(loaded, "smoke", context)
                self.assertFalse(result.verdict)
                self.assertIn(positive, result.report)
                self.assertIn(f"{cause} 1:", result.report)
                self.assertEqual(host.judge_requests, [])

    def test_answered_positive_with_its_figure_absent_is_blocked_as_the_suite_reads_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _root, loaded = _temporary_smoke_set(Path(directory))
            _host, context = _context(loaded)
            positive = next(
                case_id
                for case_id in _SAMPLE
                if cast(list[str], loaded.cases_by_id[case_id]["labels"])[1] == "positive"
            )

            def answered_without_figure(row: CaseResult) -> CaseResult:
                metrics = dict(row.metrics)
                metrics["class"] = "answered"
                metrics["checks"] = {"must_not": True}
                frontend = cast(Mapping[str, JSONValue], metrics["frontend"])
                metrics["frontend"] = {**frontend, "agrees": True}
                return replace(row, metrics=metrics)

            with patch.dict(smoke_slice._RUNNERS, {"guardrails": _mutating_guardrails(positive, answered_without_figure)}):
                result = smoke_slice.run_smoke(loaded, "smoke", context)
        self.assertTrue(result.verdict, result.report)
        self.assertIn("unblocked 0: none", result.report)

    def test_control_replacement_and_extraction_bounds_are_reported_not_gated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _root, loaded = _temporary_smoke_set(Path(directory))
            host, context = _context(loaded)
            control = next(
                case_id
                for case_id in _SAMPLE
                if cast(list[str], loaded.cases_by_id[case_id]["labels"])[1]
                == "control"
            )

            def replace_control(row: CaseResult) -> CaseResult:
                metrics = dict(row.metrics)
                frontend = cast(Mapping[str, JSONValue], metrics["frontend"])
                metrics["class"] = "replaced"
                metrics["frontend"] = {**frontend, "class": "replaced", "agrees": True}
                return replace(row, metrics=metrics)

            with (
                patch.dict(smoke_slice._RUNNERS, {"guardrails": _mutating_guardrails(control, replace_control)}),
                patch.object(extraction_slice, "extract", return_value=()),
            ):
                result = smoke_slice.run_smoke(loaded, "smoke", context)
        self.assertTrue(result.verdict, result.report)
        self.assertIn("controls replaced 1 (reported, not gated)", result.report)
        self.assertIn("controls declined 0 (reported, not gated)", result.report)
        self.assertIn("extraction bounds fail (reported, not gated)", result.report)
        self.assertIn("guardrails report (its verdict is reported, not smoke's)", result.report)
        self.assertIn("extraction report (its verdict is reported, not smoke's)", result.report)
        self.assertEqual(host.judge_requests, [])

    def test_case_from_an_unrouted_suite_fails_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _root, loaded = _temporary_smoke_set(Path(directory))
            case_id = next(
                case_id
                for case_id in select_cases(loaded, "smoke").counted
                if loaded.cases_by_id[case_id].get("suite") == "build-gates"
            )
            cases_by_id = dict(loaded.cases_by_id)
            cases_by_id[case_id] = {**cases_by_id[case_id], "suite": "fictional-suite"}
            loaded = replace(loaded, cases_by_id=cases_by_id)
            host, context = _context(loaded)
            result = smoke_slice.run_smoke(loaded, "smoke", context)
        row = next(row for row in result.results if row.case_id == case_id)
        self.assertFalse(result.verdict)
        self.assertEqual(row.verdict, "fail")
        self.assertIn(f"unrouted suite cases: {case_id}", result.report)
        self.assertEqual(host.judge_requests, [])

    def test_no_turn_access_fails_each_guardrails_case_without_host_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _root, loaded = _temporary_smoke_set(Path(directory))
            host, context = _context(loaded)
            result = smoke_slice.run_smoke(
                loaded, "smoke", replace(context, turns=None)
            )
        guardrail_ids = {
            case_id
            for case_id in select_cases(loaded, "smoke").counted
            if loaded.cases_by_id[case_id].get("suite") == "guardrails"
        }
        guardrail_rows = tuple(row for row in result.results if row.case_id in guardrail_ids)
        self.assertEqual({row.case_id for row in guardrail_rows}, guardrail_ids)
        self.assertTrue(all(row.verdict == "fail" for row in guardrail_rows))
        self.assertTrue(
            all(row.metrics.get("problem") == "turns-unavailable" for row in guardrail_rows)
        )
        self.assertEqual(host.requests, [])
        self.assertEqual(host.judge_requests, [])

    def test_unsigned_case_is_removed_by_loader_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _root, loaded = _temporary_smoke_set(Path(directory))
            unsigned_id = next(
                case_id
                for case_id in _SAMPLE
                if cast(list[str], loaded.cases_by_id[case_id]["labels"])[1]
                == "positive"
            )
            loaded = replace(
                loaded, unsigned_ids=loaded.unsigned_ids | {unsigned_id}
            )
            host, context = _context(loaded)
            result = smoke_slice.run_smoke(loaded, "smoke", context)
        self.assertNotIn(unsigned_id, {row.case_id for row in result.results})
        self.assertEqual(host.judge_requests, [])


class SmokeSet(unittest.TestCase):
    """The registered composite follows the committed slice files."""

    def test_committed_lists_load_match_sources_and_bound_engine_calls(self) -> None:
        loaded = _load(ROOT / SET_ROOT)
        self.assertTrue(set(_SAMPLE) <= set(loaded.active_ids))
        smoke_selection = select_cases(loaded, "smoke")
        guardrail_ids = tuple(
            case_id
            for case_id in smoke_selection.counted
            if loaded.cases_by_id[case_id].get("suite") == "guardrails"
        )
        self.assertEqual(set(guardrail_ids), set(_SAMPLE))
        roles_by_family: dict[str, set[str]] = {}
        for case_id in guardrail_ids:
            case = loaded.cases_by_id[case_id]
            roles_by_family.setdefault(cast(str, case["category"]), set()).add(
                cast(list[str], case["labels"])[1]
            )
        self.assertTrue(roles_by_family)
        self.assertTrue(all(roles == {"positive", "control"} for roles in roles_by_family.values()))

        extraction_ids = tuple(
            case_id
            for case_id in smoke_selection.counted
            if loaded.cases_by_id[case_id].get("suite") == "build-gates"
        )
        expected_calls = len(guardrail_ids) + sum(
            case_id in _SAMPLE for case_id in guardrail_ids
        )
        self.assertEqual(
            smoke_slice.engine_calls(loaded, "smoke"),
            expected_calls,
        )
        self.assertEqual(expected_calls, 2 * len(_SAMPLE))
        self.assertEqual(smoke_slice.engine_calls(loaded, "extraction"), 0)
        self.assertTrue(extraction_ids)

        for list_name in loaded.slice_lists["smoke"]:
            relative = SET_ROOT / "slices" / "smoke" / f"{list_name}.ids"
            if absent_from_export(relative, ROOT):
                continue
            data = (ROOT / relative).read_bytes()
            self.assertTrue(data.endswith(b"\n"), relative)
        for list_name in loaded.slice_lists["smoke"]:
            if list_name not in loaded.slice_lists["extraction"]:
                continue
            smoke_path = ROOT / SET_ROOT / "slices" / "smoke" / f"{list_name}.ids"
            extraction_path = ROOT / SET_ROOT / "slices" / "extraction" / f"{list_name}.ids"
            if absent_from_export(smoke_path.relative_to(ROOT), ROOT):
                continue
            self.assertEqual(smoke_path.read_bytes(), extraction_path.read_bytes())
        self.assertLessEqual(smoke_slice.engine_calls(loaded, "smoke"), turn_run.SMOKE_TURNS)

    def test_runner_selection_excludes_unsigned_cases_and_records_real_metric_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _root, loaded = _temporary_smoke_set(Path(directory))
            host, context = _context(loaded)
            result = smoke_slice.run_smoke(loaded, "smoke", context)
            guardrail_ids = {
                case_id
                for case_id in select_cases(loaded, "smoke").counted
                if loaded.cases_by_id[case_id].get("suite") == "guardrails"
            }
            guardrail_result_rows = tuple(
                row for row in result.results if row.case_id in guardrail_ids
            )
            self.assertTrue(guardrail_result_rows)
            for row in guardrail_result_rows:
                self.assertTrue({"role", "class", "stream"} <= set(row.metrics))
                self.assertNotIn("problem", row.metrics)
                if row.case_id in _SAMPLE:
                    frontend = cast(Mapping[str, object], row.metrics["frontend"])
                    self.assertIn("class", frontend)
                    self.assertIsInstance(frontend.get("agrees"), bool)
            unsigned_id = guardrail_result_rows[0].case_id
            unsigned = replace(
                loaded, unsigned_ids=loaded.unsigned_ids | {unsigned_id}
            )
            narrowed_result = smoke_slice.run_smoke(unsigned, "smoke", context)
        self.assertNotIn(unsigned_id, {row.case_id for row in narrowed_result.results})
        self.assertEqual(host.judge_requests, [])

    def test_registered_runner_signs_nothing_and_imports_stdlib_and_gideon(self) -> None:
        loaded = _load(ROOT / SET_ROOT)
        self.assertEqual(select_cases(loaded, "smoke").unsigned, ())
        self.assertIs(SLICE_RUNNERS["smoke"].runner, smoke_slice.run_smoke)
        tree = ast.parse(
            (ROOT / "gideon/evaluation/smoke_slice.py").read_text(encoding="utf-8")
        )
        allowed = set(__import__("sys").stdlib_module_names) | {"gideon"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = tuple(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                modules = ((node.module or "").split(".", 1)[0],)
            else:
                continue
            self.assertTrue(set(modules) <= allowed, modules)


class SmokeCommand(unittest.TestCase):
    def test_reference_regression_blocks_through_eval_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_parent = root / "inputs"
            input_parent.mkdir()
            set_root, loaded = _temporary_smoke_set(input_parent)
            checkout = root / "checkout"
            checkout.mkdir()
            shutil.copytree(ROOT / "eval", checkout / "eval")
            shutil.rmtree(checkout / "eval" / "reference", ignore_errors=True)
            shutil.copy(ROOT / "courts.yaml", checkout / "courts.yaml")

            extraction_ids = tuple(
                case_id
                for case_id in select_cases(loaded, "smoke").counted
                if loaded.cases_by_id[case_id].get("suite") == "build-gates"
            )
            target = extraction_ids[0]
            for list_name, case_ids in loaded.slice_lists["smoke"].items():
                value = reference.ReferenceFile(
                    format=reference.FORMAT_VERSION,
                    product_version=gideon.__version__,
                    corpus_lockfile=None,
                    eval_set_version=loaded.version,
                    hardware_profile="fictitious-profile",
                    tag=f"v{gideon.__version__}",
                    slice="smoke",
                    list=list_name,
                    repeats=1,
                    set_digest=loaded.digest,
                    cases=dict.fromkeys(case_ids, "pass"),
                )
                path = checkout / reference.REFERENCE_ROOT / "smoke" / f"{list_name}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(reference.serialize_reference(value), encoding="utf-8")

            host = CommandDoorHost()
            _host, turns_context = _context(loaded, host=host)
            assert turns_context.turns is not None

            def regress_extraction(
                narrowed: LoadedSet, slice_name: str, _context: RunContext
            ) -> SliceResult:
                selected = select_cases(narrowed, slice_name)
                return SliceResult(
                    True,
                    "planted extraction result\n",
                    tuple(
                        CaseResult(
                            case_id,
                            1,
                            "fail" if case_id == target else "pass",
                            {},
                        )
                        for case_id in selected.counted
                    ),
                )

            kwargs = _run_kwargs(cast(EvalHost, host), checkout=checkout)
            with (
                patch.object(
                    command.engine,
                    "resolve_engine_target",
                    return_value=command.engine.EngineTarget(
                        "fictitious-profile", "fictitious-model", 1
                    ),
                ),
                patch.object(
                    command.access,
                    "load_general_instruction",
                    return_value=turns_context.turns.instruction,
                ),
                patch.object(
                    command.access,
                    "read_eval_password",
                    return_value=turns_context.turns.password,
                ),
                patch.object(
                    command.access,
                    "make_client_factory",
                    return_value=turns_context.turns.client_factory,
                ),
                patch.object(
                    command.door,
                    "probe",
                    return_value=command.door.ProbeResult(True, "door ready", None),
                ),
                patch.dict(smoke_slice._RUNNERS, {"build-gates": regress_extraction}),
                patch.object(command.stacks.secrets, "select_directory"),
            ):
                code, stdout, stderr = _invoke(
                    [
                        "eval", "run", "--slice", "smoke", "--stack", "ci",
                        "--kind", "smoke", "--set", str(set_root),
                    ],
                    **kwargs,
                )

        self.assertEqual(code, 1, stdout)
        self.assertEqual(stderr, "")
        self.assertIn("smoke: pass", stdout)
        self.assertIn(f"regressed {target}", stdout)
        self.assertIn("gate: refuse", stdout)
        self.assertIn(target, stdout)
        self.assertEqual(host.locks, {})


if __name__ == "__main__":
    unittest.main()
