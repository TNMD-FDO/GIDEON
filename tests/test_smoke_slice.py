"""Composite smoke runner and its frozen input contract."""

from __future__ import annotations

import ast
import inspect
import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from test_evaluation_run import (
    EvalHost,
    _assert_comparison_lines,
    _casecite_reply,
    _invoke,
    _run_kwargs,
)
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
from gideon.evaluation.evalset import (
    SET_ROOT,
    TIER_2_CATEGORY,
    LoadedSet,
    load_set,
    select_cases,
)
from gideon.evaluation.results import (
    CaseResult,
    ImageAccess,
    JSONValue,
    RunContext,
    SliceResult,
)
from gideon.evaluation.slices import SLICE_RUNNERS, SMOKE_PARTS
from gideon.evaluation.turns import run as turn_run
from gideon.host.sysio import PathLike
from tools.exportboundary import absent_from_export

ROOT = Path(__file__).resolve().parents[1]
_SAMPLE = guardrails_slice.FRONTEND_SAMPLE
_SMOKE_LIST = Path("slices/smoke")
_IMAGE_ACCESS = ImageAccess("registry.example/gideon@sha256:" + "a" * 64, Path("/fictitious-checkout"))


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


def _composite_with_runner(
    part_name: str, runner: Callable[[LoadedSet, str, RunContext], SliceResult]
) -> smoke_slice.Composite:
    """Replace one handed part's runner while preserving the parts' order."""

    return smoke_slice.Composite(
        {
            name: replace(spec, runner=runner) if name == part_name else spec
            for name, spec in SMOKE_PARTS.items()
        }
    )


class CommandDoorHost(JudgingDoorHost):
    """The fake door host with the lock and sibling tree used by eval run."""

    def __init__(self) -> None:
        super().__init__()
        self.locks: dict[str, str] = {}
        self.image_stdout: str | None = None
        self.image_returncode = 0
        self.image_error = ""

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if tuple(argv)[:3] == ("docker", "image", "inspect"):
            return subprocess.CompletedProcess(list(argv), 0, "", "")
        if tuple(argv)[:2] == ("docker", "run") and argv[-1] == "gideon.casecite":
            if self.image_error == "timeout":
                assert timeout is not None
                raise subprocess.TimeoutExpired(argv, timeout)
            assert input is not None
            output = _casecite_reply(input) if self.image_stdout is None else self.image_stdout
            return subprocess.CompletedProcess(
                list(argv), self.image_returncode, output, "PRIVATE MATTER TEXT"
            )
        return super().run(
            argv, check=check, input=input, cwd=cwd, env=env,
            timeout=timeout, passthrough=passthrough,
        )

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
        if Path(path).name == "images.lock":
            return (ROOT / "images.lock").read_text(encoding=encoding)
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
    """The composite gates zero-tolerance guardrail facts and image-leg failures."""

    def test_image_leg_timeout_exit_and_empty_reply_block_smoke(self) -> None:
        for failure in ("timeout", "exit", "empty"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                _root, loaded = _temporary_smoke_set(Path(directory))
                host = CommandDoorHost()
                _host, context = _context(loaded, host=host)
                if failure == "timeout":
                    host.image_error = "timeout"
                elif failure == "exit":
                    host.image_returncode = 3
                else:
                    host.image_stdout = ""
                result = SLICE_RUNNERS["smoke"].runner(
                    loaded, "smoke", replace(context, image=_IMAGE_ACCESS)
                )
                extraction_ids = {
                    case_id for case_id in select_cases(loaded, "smoke").counted
                    if loaded.cases_by_id[case_id]["suite"] == "build-gates"
                }
                failed = {
                    row.case_id for row in result.results
                    if row.metrics.get("problem") == "image-leg-failed"
                }
                self.assertEqual(failed, extraction_ids)
                self.assertFalse(result.verdict)
                self.assertIn(f"image leg failed {len(failed)}:", result.report)
                self.assertIn("zero-tolerance: fail", result.report)
                self.assertNotIn("PRIVATE MATTER TEXT", result.report)

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
                        metrics["problem"] = "turn-cut"
                    else:
                        frontend = cast(Mapping[str, JSONValue], metrics["frontend"])
                        metrics["frontend"] = {**frontend, "agrees": False}
                    return replace(row, metrics=metrics)

                composite = _composite_with_runner(
                    "guardrails", _mutating_guardrails(positive, mutation)
                )
                result = composite.run(loaded, "smoke", context)
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

            composite = _composite_with_runner(
                "guardrails", _mutating_guardrails(positive, answered_without_figure)
            )
            result = composite.run(loaded, "smoke", context)
        self.assertTrue(result.verdict, result.report)
        self.assertIn("unblocked 0: none", result.report)

    def test_control_replacement_and_extraction_bounds_are_reported_not_gated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _root, loaded = _temporary_smoke_set(Path(directory))
            host, context = _context(loaded, host=CommandDoorHost())
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

            composite = _composite_with_runner(
                "guardrails", _mutating_guardrails(control, replace_control)
            )
            with patch.object(extraction_slice, "extract", return_value=()):
                result = composite.run(loaded, "smoke", replace(context, image=_IMAGE_ACCESS))
        self.assertTrue(result.verdict, result.report)
        self.assertIn("controls replaced 1 (reported, not gated)", result.report)
        self.assertIn("controls declined 0 (reported, not gated)", result.report)
        self.assertIn("extraction bounds fail (reported, not gated)", result.report)
        self.assertIn("guardrails report (its verdict is reported, not smoke's)", result.report)
        self.assertIn("extraction report (its verdict is reported, not smoke's)", result.report)
        self.assertFalse([line for line in result.report.splitlines() if line.startswith("verdict ")])
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
            result = SLICE_RUNNERS["smoke"].runner(loaded, "smoke", context)
        row = next(row for row in result.results if row.case_id == case_id)
        self.assertFalse(result.verdict)
        self.assertEqual(row.verdict, "fail")
        self.assertIn(f"unrouted suite cases: {case_id}", result.report)
        self.assertEqual(host.judge_requests, [])

    def test_build_gates_case_in_an_unrouted_category_fails_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _root, loaded = _temporary_smoke_set(Path(directory))
            build_gates_ids = tuple(
                case_id
                for case_id in select_cases(loaded, "smoke").counted
                if loaded.cases_by_id[case_id]["suite"] == "build-gates"
            )
            self.assertTrue(build_gates_ids)
            case_id = build_gates_ids[0]
            cases_by_id = dict(loaded.cases_by_id)
            cases_by_id[case_id] = {**cases_by_id[case_id], "category": "misfiled"}
            loaded = replace(loaded, cases_by_id=cases_by_id)
            _host, context = _context(loaded)
            result = SLICE_RUNNERS["smoke"].runner(loaded, "smoke", context)

        rows = tuple(row for row in result.results if row.case_id == case_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].verdict, "fail")
        self.assertEqual(rows[0].metrics, {"problem": "unrouted-suite"})
        self.assertIn(f"unrouted suite cases: {case_id}", result.report)
        self.assertFalse(result.verdict)
        for other_id in build_gates_ids[1:]:
            with self.subTest(case_id=other_id):
                other_rows = tuple(row for row in result.results if row.case_id == other_id)
                self.assertEqual(len(other_rows), 1)
                self.assertIsNotNone(other_rows[0].latency_ms)

    def test_build_gates_case_reaches_only_the_part_naming_its_category(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _root, loaded = _temporary_smoke_set(Path(directory))
            case_id = next(
                case_id
                for case_id in select_cases(loaded, "smoke").counted
                if loaded.cases_by_id[case_id]["suite"] == "build-gates"
            )
            cases_by_id = dict(loaded.cases_by_id)
            cases_by_id[case_id] = {**cases_by_id[case_id], "category": "misfiled"}
            loaded = replace(loaded, cases_by_id=cases_by_id)
            extraction_received: list[str] = []
            misfiled_received: list[str] = []

            def record_extraction(
                narrowed: LoadedSet, slice_name: str, context: RunContext
            ) -> SliceResult:
                extraction_received.extend(select_cases(narrowed, slice_name).counted)
                return SMOKE_PARTS["extraction"].runner(narrowed, slice_name, context)

            def record_misfiled(
                narrowed: LoadedSet, slice_name: str, _context: RunContext
            ) -> SliceResult:
                misfiled_received.extend(select_cases(narrowed, slice_name).counted)
                return SliceResult(
                    True,
                    "planted part report\n",
                    tuple(CaseResult(found, 1, "pass", {}) for found in misfiled_received),
                )

            composite = smoke_slice.Composite(
                {
                    **SMOKE_PARTS,
                    "misfiled": replace(
                        SMOKE_PARTS["extraction"],
                        categories=frozenset({("build-gates", "misfiled")}),
                        runner=record_misfiled,
                    ),
                    "extraction": replace(SMOKE_PARTS["extraction"], runner=record_extraction),
                }
            )
            _host, context = _context(loaded)
            result = composite.run(loaded, "smoke", replace(context, turns=None))

        self.assertEqual(misfiled_received, [case_id])
        self.assertNotIn(case_id, extraction_received)
        rows = tuple(row for row in result.results if row.case_id == case_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].metrics, {})

    def test_duplicate_part_category_is_refused(self) -> None:
        extraction = SMOKE_PARTS["extraction"]
        category = next(iter(extraction.categories))
        with self.assertRaises(ValueError) as raised:
            smoke_slice.Composite({"extraction": extraction, "duplicate": replace(extraction)})
        self.assertIn(repr(category), str(raised.exception))

    def test_committed_rows_follow_the_parts_order(self) -> None:
        loaded = _load(ROOT / SET_ROOT)
        _host, context = _context(loaded)
        result = SLICE_RUNNERS["smoke"].runner(loaded, "smoke", replace(context, turns=None))
        counted = select_cases(loaded, "smoke").counted
        self.assertCountEqual((row.case_id for row in result.results), counted)
        positions = {row.case_id: index for index, row in enumerate(result.results)}
        guardrail_positions = tuple(
            positions[case_id]
            for case_id in counted
            if (
                loaded.cases_by_id[case_id]["suite"],
                loaded.cases_by_id[case_id]["category"],
            )
            in SMOKE_PARTS["guardrails"].categories
        )
        extraction_positions = tuple(
            positions[case_id]
            for case_id in counted
            if (
                loaded.cases_by_id[case_id]["suite"],
                loaded.cases_by_id[case_id]["category"],
            )
            in SMOKE_PARTS["extraction"].categories
        )
        self.assertTrue(guardrail_positions)
        self.assertTrue(extraction_positions)
        self.assertLess(max(guardrail_positions), min(extraction_positions))

    def test_no_turn_access_fails_each_guardrails_case_without_host_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _root, loaded = _temporary_smoke_set(Path(directory))
            host, context = _context(loaded)
            result = SLICE_RUNNERS["smoke"].runner(
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
            result = SLICE_RUNNERS["smoke"].runner(loaded, "smoke", context)
        self.assertNotIn(unsigned_id, {row.case_id for row in result.results})
        self.assertEqual(host.judge_requests, [])


class SmokeSet(unittest.TestCase):
    """The registered composite follows the committed slice files."""

    def test_tier_two_case_adds_two_calls_to_smoke_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            set_root, loaded = _temporary_smoke_set(Path(directory))
            tier_2_id = next(
                case_id
                for case_id in loaded.slice_lists["guardrails"][TIER_2_CATEGORY]
                if case_id in loaded.active_ids
            )
            engine_calls = SLICE_RUNNERS["smoke"].engine_calls
            turn_calls = SMOKE_PARTS["guardrails"].turn_calls
            assert engine_calls is not None and turn_calls is not None
            previous = engine_calls(loaded, "smoke")
            (set_root / _SMOKE_LIST / f"{TIER_2_CATEGORY}.ids").write_text(
                f"{tier_2_id}\n", encoding="utf-8"
            )
            planted = _load(set_root)

            guardrail_ids = tuple(
                case_id
                for case_id in select_cases(planted, "smoke").counted
                if (
                    planted.cases_by_id[case_id]["suite"],
                    planted.cases_by_id[case_id]["category"],
                )
                in SMOKE_PARTS["guardrails"].categories
            )
            guardrail_id_set = set(guardrail_ids)
            narrowed = replace(
                planted,
                slices={**planted.slices, "smoke": guardrail_ids},
                slice_lists={
                    **planted.slice_lists,
                    "smoke": {
                        name: tuple(case_id for case_id in ids if case_id in guardrail_id_set)
                        for name, ids in planted.slice_lists["smoke"].items()
                    },
                },
            )
            self.assertIn(tier_2_id, guardrail_ids)
            self.assertEqual(engine_calls(planted, "smoke"), turn_calls(narrowed, "smoke"))
            self.assertEqual(engine_calls(planted, "smoke"), previous + 2)

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
        engine_calls = SLICE_RUNNERS["smoke"].engine_calls
        assert engine_calls is not None
        self.assertEqual(engine_calls(loaded, "smoke"), expected_calls)
        self.assertEqual(expected_calls, 2 * len(_SAMPLE))
        self.assertEqual(engine_calls(loaded, "extraction"), 0)
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
        self.assertLessEqual(engine_calls(loaded, "smoke"), turn_run.SMOKE_TURNS)

    def test_runner_selection_excludes_unsigned_cases_and_records_real_metric_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _root, loaded = _temporary_smoke_set(Path(directory))
            host, context = _context(loaded)
            result = SLICE_RUNNERS["smoke"].runner(loaded, "smoke", context)
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
            narrowed_result = SLICE_RUNNERS["smoke"].runner(unsigned, "smoke", context)
        self.assertNotIn(unsigned_id, {row.case_id for row in narrowed_result.results})
        self.assertEqual(host.judge_requests, [])

    def test_registered_runner_signs_nothing_and_imports_stdlib_and_gideon(self) -> None:
        loaded = _load(ROOT / SET_ROOT)
        self.assertEqual(select_cases(loaded, "smoke").unsigned, ())
        self.assertIs(inspect.getmodule(SLICE_RUNNERS["smoke"].runner), smoke_slice)
        for name, spec in SMOKE_PARTS.items():
            self.assertIs(spec, SLICE_RUNNERS[name])
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
    def _run_fixture(self, *, fail_guardrail: bool, fail_extraction: bool) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            set_root, loaded = _temporary_smoke_set(root)
            if not fail_extraction:
                # One case per list misses the bounds' floors, so passing bounds
                # take the extraction slice's whole lists back.
                for list_name, case_ids in loaded.slice_lists["extraction"].items():
                    path = set_root / _SMOKE_LIST / f"{list_name}.ids"
                    if path.is_file():
                        path.write_text(
                            "".join(f"{case_id}\n" for case_id in case_ids), encoding="utf-8"
                        )
                loaded = _load(set_root)
            checkout = root / "checkout"
            checkout.mkdir()
            shutil.copytree(
                ROOT / guardrails_slice.SEED_ROOT, checkout / guardrails_slice.SEED_ROOT
            )
            host = CommandDoorHost()
            _host, turns_context = _context(loaded, host=host)
            assert turns_context.turns is not None
            positive = next(
                case_id for case_id in _SAMPLE
                if cast(list[str], loaded.cases_by_id[case_id]["labels"])[1] == "positive"
            )

            def unblocked(row: CaseResult) -> CaseResult:
                metrics = dict(row.metrics)
                metrics["class"] = "answered"
                metrics.pop("checks", None)
                return replace(row, metrics=metrics)

            guardrails_runner = (
                _mutating_guardrails(positive, unblocked)
                if fail_guardrail else guardrails_slice.run_guardrails
            )
            replacement = dict(SLICE_RUNNERS)
            replacement["smoke"] = replace(
                SLICE_RUNNERS["smoke"],
                runner=_composite_with_runner("guardrails", guardrails_runner).run,
            )
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
                patch.object(command, "SLICE_RUNNERS", replacement),
                (
                    patch.object(extraction_slice, "extract", return_value=())
                    if fail_extraction
                    else nullcontext()
                ),
                patch.object(command.stacks.secrets, "select_directory"),
            ):
                return _invoke(
                    [
                        "eval", "run", "--slice", "smoke", "--stack", "ci",
                        "--kind", "smoke", "--set", str(set_root),
                    ],
                    **_run_kwargs(cast(EvalHost, host), checkout=checkout),
                )

    def test_zero_tolerance_failure_prints_fail_with_passing_extraction_bounds(self) -> None:
        code, stdout, stderr = self._run_fixture(fail_guardrail=True, fail_extraction=False)
        self.assertEqual((code, stderr), (1, ""), stdout)
        self.assertIn("smoke: fail", stdout)
        self.assertIn("extraction bounds pass (reported, not gated)", stdout)
        _assert_comparison_lines(self, stdout, word="FAIL")

    def test_failing_extraction_bounds_print_pass_with_passing_zero_tolerance(self) -> None:
        code, stdout, stderr = self._run_fixture(fail_guardrail=False, fail_extraction=True)
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertIn("smoke: pass", stdout)
        self.assertIn("extraction bounds fail (reported, not gated)", stdout)
        _assert_comparison_lines(self, stdout, word="pass")

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

            replacement = dict(SLICE_RUNNERS)
            replacement["smoke"] = replace(
                SLICE_RUNNERS["smoke"],
                runner=_composite_with_runner("extraction", regress_extraction).run,
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
                patch.object(command, "SLICE_RUNNERS", replacement),
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
        self.assertIn("reference: regressed", stdout)
        self.assertIn(f"regressed {target}", stdout)
        _assert_comparison_lines(self, stdout, word="pass")
        self.assertIn("gate: refuse", stdout)
        self.assertIn(target, stdout)
        self.assertEqual(host.locks, {})


if __name__ == "__main__":
    unittest.main()
