"""Evaluation runner and CLI contracts."""

import ast
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_evaluation_record import read_psql_set
from test_judge import engine_output, valid_content

import gideon
from gideon.cli import main
from gideon.evaluation import (
    challenger,
    command,
    judge,
    reference,
    signoffs,
    stacks,
    window,
)
from gideon.evaluation.evalset import SET_ROOT, LoadedSet, load_set, select_cases
from gideon.evaluation.extraction_slice import run_extraction
from gideon.evaluation.results import CaseResult, RunContext, SliceResult
from gideon.evaluation.slices import SLICE_RUNNERS
from gideon.extraction import KEYED_TYPES, SECTION_TYPES, ExactObject, extract
from gideon.extraction.scoring import MIN_RECALL
from gideon.host import backuplock, nogpu
from gideon.host.sysio import Host, PathLike
from tools.exportboundary import absent_from_export

ROOT = Path(__file__).resolve().parents[1]
EVALUATION = ROOT / "gideon" / "evaluation"
RESEARCH_QA_CASES_PATH = Path("eval/sets/eval-v1/research-qa/harvest.jsonl")


NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
RUN_ID = "11111111-2222-4333-8444-555555555555"


class EvalHost:
    def __init__(
        self,
        *,
        probe_rc: int = 0,
        write_rc: int = 0,
        commit_rc: int = 0,
        commit_stdout: str = "a" * 40 + "\n",
        status_rc: int = 0,
        status_stdout: str = "",
        no_git: bool = False,
        ci_stack_present: bool = True,
        effective_uid: int = 0,
    ) -> None:
        self.probe_rc = probe_rc
        self.write_rc = write_rc
        self.commit_rc = commit_rc
        self.commit_stdout = commit_stdout
        self.status_rc = status_rc
        self.status_stdout = status_stdout
        self.no_git = no_git
        self.ci_stack_present = ci_stack_present
        self.effective_uid = effective_uid
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.locks: dict[str, str] = {}
        self.lock_records: list[tuple[str, str]] = []

    def run(
        self,
        argv: tuple[str, ...] | list[str],
        *,
        check: bool = False,
        input: str | None = None,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, cwd, env, timeout, passthrough
        command_argv = tuple(argv)
        self.calls.append((command_argv, input))
        if command_argv[0] == "docker":
            rc = self.probe_rc if input == "SELECT 1;\n" else self.write_rc
            return subprocess.CompletedProcess(list(command_argv), rc, "", "database diagnostic")
        if command_argv[0] == "git" and command_argv[-2:] == ("rev-parse", "HEAD"):
            return subprocess.CompletedProcess(list(command_argv), self.commit_rc, self.commit_stdout, "git diagnostic")
        if command_argv[0] == "git":
            return subprocess.CompletedProcess(list(command_argv), self.status_rc, self.status_stdout, "git diagnostic")
        raise AssertionError(f"unexpected command: {command_argv}")

    def read_text(self, path: str | os.PathLike[str], *, encoding: str = "utf-8") -> str:
        return Path(path).read_text(encoding=encoding)

    def write_text(self, path: str | os.PathLike[str], text: str, *, encoding: str = "utf-8", mode: int = 0o644) -> None:
        raise NotImplementedError

    def exists(self, path: str | os.PathLike[str]) -> bool:
        if Path(path).name == ".git":
            return not self.no_git
        if Path(path) == Path(stacks.CI_ROOT) / "compose.yaml":
            return self.ci_stack_present
        return Path(path).exists()

    def listdir(self, path: str | os.PathLike[str]) -> list[str]:
        return os.listdir(path)

    def unlink(self, path: str | os.PathLike[str], *, missing_ok: bool = False) -> None:
        raise NotImplementedError

    def stat(self, path: str | os.PathLike[str]) -> os.stat_result:
        return Path(path).stat()

    def chmod(self, path: str | os.PathLike[str], mode: int) -> None:
        raise NotImplementedError

    def chown(self, path: str | os.PathLike[str], uid: int, gid: int) -> None:
        raise NotImplementedError

    def mkdir(self, path: str | os.PathLike[str], *, mode: int = 0o755, parents: bool = False, exist_ok: bool = False) -> None:
        del path, mode, parents, exist_ok

    def take_lock(self, path: str | os.PathLike[str], record: str) -> str | None:
        key = os.fspath(path)
        self.lock_records.append((key, record))
        holder = self.locks.get(key)
        if holder is None:
            self.locks[key] = record
        return holder

    def release_lock(self, path: str | os.PathLike[str]) -> None:
        self.locks.pop(os.fspath(path), None)

    def geteuid(self) -> int:
        return self.effective_uid


def _invoke(argv: list[str], **run_kwargs: Any) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    real_run_eval = command.run_eval
    injected = lambda args: real_run_eval(args, **run_kwargs)  # noqa: E731
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), patch.object(
        command, "run_eval", side_effect=injected
    ):
        code = main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def _run_kwargs(
    host: EvalHost,
    *,
    checkout: Path = ROOT,
    court_path: Path | None = None,
) -> dict[str, Any]:
    return {
        "host": host,
        "checkout_root": checkout,
        "rendered_dir": "/tmp/evaluation-rendered",
        "site_path": ROOT / "config" / "site.example.yaml",
        "clock": lambda: NOW,
        "run_id_factory": lambda: RUN_ID,
        "court_path": ROOT / "courts.yaml" if court_path is None else court_path,
    }


def _reference_checkout(directory: str) -> Path:
    checkout = Path(directory) / "checkout"
    checkout.mkdir()
    shutil.copytree(ROOT / "eval", checkout / "eval")
    # The release's own reference is not carried in: a temporary checkout is
    # absent until the case under test writes one.
    shutil.rmtree(checkout / "eval" / "reference", ignore_errors=True)
    shutil.copy(ROOT / "courts.yaml", checkout / "courts.yaml")
    return checkout


def _reference_files(
    checkout: Path,
    loaded: Any,
    results: tuple[Any, ...],
    *,
    eval_set_version: str | None = None,
    reference_verdicts: Mapping[str, reference.Verdict] | None = None,
) -> None:
    verdicts = {result.case_id: cast(reference.Verdict, result.verdict) for result in results}
    if reference_verdicts is not None:
        verdicts = dict(reference_verdicts)
    version = loaded.version if eval_set_version is None else eval_set_version
    for list_name, ids in loaded.slice_lists["extraction"].items():
        value = reference.ReferenceFile(
            format=reference.FORMAT_VERSION,
            product_version=gideon.__version__,
            corpus_lockfile=None,
            eval_set_version=version,
            hardware_profile="fictitious-profile",
            tag=f"v{gideon.__version__}",
            slice="extraction",
            list=list_name,
            repeats=1,
            set_digest=loaded.digest,
            cases={case_id: verdicts[case_id] for case_id in ids if case_id in verdicts},
        )
        path = checkout / reference.REFERENCE_ROOT / "extraction" / f"{list_name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(reference.serialize_reference(value), encoding="utf-8")


def _changed_result(loaded: Any, changes: Mapping[str, str]) -> SliceResult:
    clean = run_extraction(loaded, "extraction", _run_context())
    return replace(
        clean,
        results=tuple(
            replace(result, verdict=changes.get(result.case_id, result.verdict))
            for result in clean.results
        ),
    )


def _assert_comparison_lines(
    test: unittest.TestCase, stdout: str, outcome: str | None = None
) -> None:
    """The release's whitelist reads exactly one of each line.

    *outcome* is pinned only where the case is about the comparison; a case
    about the rows runs against the real checkout, whose committed reference
    moves with the set, and asserts the counts alone.
    """

    reference_lines = [line for line in stdout.splitlines() if line.startswith("reference: ")]
    verdict_lines = [line for line in stdout.splitlines() if line.startswith("verdict ")]
    test.assertEqual(len(reference_lines), 1, reference_lines)
    if outcome is not None:
        test.assertEqual(reference_lines, [f"reference: {outcome}"])
    test.assertEqual(len(verdict_lines), 1)


def _expected_object(obj: ExactObject) -> dict[str, object]:
    value: dict[str, object] = {
        "type": obj.type,
        "start": obj.start,
        "end": obj.end,
        "text": obj.text,
    }
    if obj.type in KEYED_TYPES:
        value["key"] = obj.key
    if obj.type in SECTION_TYPES:
        value["subsections"] = list(obj.subsections)
    return value


def _runner_case(case_id: str, question: str, expected: list[dict[str, object]]) -> dict[str, object]:
    return {
        "id": case_id,
        "question": question,
        "expected": {"objects": expected},
        "labels": ["invented"],
    }


def _run_context() -> RunContext:
    return RunContext(cast(Host, EvalHost()), "/tmp/evaluation-rendered", "/tmp/evaluation-rendered", None, None, 1, lambda _line: None)


def _passing_selection_runner(
    loaded: LoadedSet, slice_name: str, context: RunContext
) -> SliceResult:
    del context
    selection = select_cases(loaded, slice_name)
    return SliceResult(
        True,
        "fictional runner report\n",
        tuple(CaseResult(case_id, 1, "pass", {}) for case_id in selection.counted),
    )


def _case_metrics(result: CaseResult) -> Mapping[str, Mapping[str, int]]:
    return cast(Mapping[str, Mapping[str, int]], result.metrics)


def _expected_objects(case: Mapping[str, object]) -> list[object]:
    expected = case.get("expected")
    if not isinstance(expected, Mapping):
        return []
    objects = expected.get("objects")
    return cast(list[object], objects) if isinstance(objects, list) else []


class Runner(unittest.TestCase):
    """Per-case verdicts and metrics are derived from the shared score."""

    def test_per_case_verdict_metrics_and_latency(self) -> None:
        passing_question = "[FICTIONAL TEST ONLY] 18 U.S.C. § 3553(a)."
        passing_object = extract(passing_question)[0]
        false_question = (
            "[FICTIONAL TEST ONLY] 18 U.S.C. § 3553(a) and 18 U.S.C. § 3553(b)."
        )
        false_objects = extract(false_question)
        unlanded_question = "[FICTIONAL TEST ONLY] no landed citation appears here."
        cases = (
            _runner_case("case-a", passing_question, [_expected_object(passing_object)]),
            _runner_case("case-b", false_question, [_expected_object(false_objects[0])]),
            _runner_case(
                "case-c",
                unlanded_question,
                [
                    {
                        "type": "state_code",
                        "start": 0,
                        "end": len("FICTIONAL-STATE-CODE"),
                        "text": "FICTIONAL-STATE-CODE",
                    }
                ],
            ),
        )
        loaded = LoadedSet(
            "eval-v-test",
            {"suite/cases.jsonl": cases},
            {cast(str, case["id"]): case for case in cases},
            ("case-a", "case-b", "case-c"),
            {"extraction": ("case-a", "case-b", "case-c")},
            {"extraction": {"cases": ("case-a", "case-b", "case-c")}},
            "fictitious-digest",
        )

        result = run_extraction(loaded, "extraction", _run_context())
        by_id = {case.case_id: case for case in result.results}
        self.assertEqual(tuple(case.case_id for case in result.results), ("case-a", "case-b", "case-c"))
        self.assertEqual(by_id["case-a"].verdict, "pass")
        self.assertEqual(_case_metrics(by_id["case-a"])["statute"], {"hits": 1, "false_hits": 0, "misses": 0})
        self.assertEqual(by_id["case-b"].verdict, "fail")
        self.assertEqual(_case_metrics(by_id["case-b"])["statute"]["false_hits"], 1)
        self.assertEqual(by_id["case-c"].verdict, "pass")
        self.assertEqual(_case_metrics(by_id["case-c"])["state_code"], {"hits": 0, "false_hits": 0, "misses": 1})
        self.assertGreaterEqual(cast(float, by_id["case-a"].latency_ms), 0.0)


class Command(unittest.TestCase):
    """The in-process CLI exposes the ordered run and its refusals."""

    def test_quiet_window_refusal_fix_names_force(self) -> None:
        opening = NOW + timedelta(days=1)
        judgement = window.WindowJudgement(
            False,
            "fixture quiet window",
            opening,
            NOW + timedelta(days=2),
        )
        host = EvalHost()
        with patch.object(command.window, "window_judgement", return_value=judgement):
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "guardrails"],
                **_run_kwargs(host),
            )
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertIn("preconditions: refuse", stdout)
        self.assertIn(
            f"Next opening is {opening.isoformat()}; add --force for an announced window.",
            stdout,
        )
        self.assertNotIn("run: ok", stdout)
        self.assertEqual(host.lock_records, [])

    def test_force_outside_quiet_window_runs_engine_slice(self) -> None:
        host = EvalHost()
        loaded = load_set(ROOT / SET_ROOT).loaded
        assert loaded is not None
        case_id = next(
            case_id
            for case_id in loaded.active_ids
            if case_id in loaded.slices["guardrails"]
        )
        result = SliceResult(True, "fixture runner report\n", (CaseResult(case_id, 1, "pass", {}),))
        opening = NOW + timedelta(days=1)
        judgement = window.WindowJudgement(
            False,
            "fixture quiet window",
            opening,
            NOW + timedelta(days=2),
        )
        with (
            patch.object(
                command.engine,
                "resolve_engine_target",
                return_value=command.engine.EngineTarget(
                    "fixture-profile", "fixture-model", 1000
                ),
            ),
            patch.object(command.window, "window_judgement", return_value=judgement),
            patch.object(command.access, "read_eval_password", return_value="fixture password"),
            patch.object(command.access, "make_client_factory", return_value=cast(object, lambda **_kwargs: object())),
            patch.object(command.run, "new_sentinel", return_value="fixture-sentinel"),
            patch.object(
                command.door,
                "probe",
                return_value=command.door.ProbeResult(True, "fixture door", None),
            ),
            patch.object(
                command,
                "_run_repeats",
                return_value=command._RunRepeats(result, 1, 1, None),
            ) as runner,
        ):
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "guardrails", "--force"],
                **_run_kwargs(host),
            )
        self.assertEqual(code, 0, stdout + stderr)
        self.assertEqual(stderr, "")
        self.assertIn("preconditions: ok", stdout)
        self.assertIn("fixture quiet window, forced, engine lock taken, profile fixture-profile", stdout)
        self.assertIn("run: ok", stdout)
        runner.assert_called_once()
        write_sql = next(
            cast(str, input_text)
            for argv, input_text in host.calls
            if argv[0] == "docker"
            and input_text is not None
            and "INSERT INTO eval_runs" in input_text
        )
        self.assertIn("\\set forced 'true'", write_sql)

    def test_clean_committed_run_prints_ordered_rows(self) -> None:
        host = EvalHost()
        code, stdout, stderr = _invoke(
            ["eval", "run", "--slice", "extraction"], **_run_kwargs(host)
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        rows = tuple(stdout.index(f"{name}:") for name in ("load", "run", "record", "gate"))
        self.assertEqual(rows, tuple(sorted(rows)))
        self.assertIn(f"record: ok — run {RUN_ID} recorded", stdout)
        self.assertIn("gate: ok", stdout)
        _assert_comparison_lines(self, stdout)
        writes = [input for argv, input in host.calls if argv[0] == "docker" and input != "SELECT 1;\n"]
        self.assertEqual(len(writes), 1)
        self.assertIn("INSERT INTO eval_runs", writes[0] or "")
        loaded = load_set(ROOT / SET_ROOT).loaded
        assert loaded is not None
        self.assertEqual((writes[0] or "").count("INSERT INTO eval_results"), len(loaded.slices["extraction"]))
        case_lines = [
            line for line in (writes[0] or "").splitlines() if "_case_id '" in line
        ]
        expected_ids = tuple(sorted(
            case_id for case_id in loaded.slices["extraction"] if case_id in loaded.active_ids
        ))
        self.assertEqual(
            tuple(line.rsplit("'", 2)[1] for line in case_lines),
            expected_ids,
        )

    def test_planted_failure_refuses_at_gate_and_never_records(self) -> None:
        clean = load_set(ROOT / SET_ROOT).loaded
        self.assertIsNotNone(clean)
        assert clean is not None
        clean_result = run_extraction(clean, "extraction", _run_context())
        scored_case = next(
            result
            for result in clean_result.results
            if any(metric["hits"] for metric in _case_metrics(result).values())
        )
        scored_type = next(
            object_type
            for object_type, metric in _case_metrics(scored_case).items()
            if metric["hits"]
        )
        hits = sum(
            _case_metrics(result)[scored_type]["hits"]
            for result in clean_result.results
            if scored_type in _case_metrics(result)
        )
        misses = sum(
            _case_metrics(result)[scored_type]["misses"]
            for result in clean_result.results
            if scored_type in _case_metrics(result)
        )
        labels = hits + misses
        added = 0
        while hits / (labels + added) >= MIN_RECALL:
            added += 1
        active = set(clean.active_ids)
        target_ids = tuple(case_id for case_id in clean.slices["extraction"] if case_id in active)[:added]
        self.assertEqual(len(target_ids), added)
        landed_keys = {
            value["type"]: value["key"]
            for case in clean.cases_by_id.values()
            for value in _expected_objects(case)
            if isinstance(value, dict)
            and isinstance(value.get("type"), str)
            and isinstance(value.get("key"), str)
        }

        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / clean.version
            shutil.copytree(ROOT / SET_ROOT, copied)
            for path in sorted(copied.rglob("*.jsonl")):
                records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                changed = False
                for record in records:
                    if record.get("id") not in target_ids:
                        continue
                    question = record["question"]
                    marker = " [FICTIONAL TEST ONLY: planted evaluation miss]"
                    record["question"] = question + marker
                    start = len(question) + 1
                    object_value: dict[str, object] = {
                        "type": scored_type,
                        "start": start,
                        "end": start + len(marker) - 1,
                        "text": marker[1:],
                    }
                    if scored_type in KEYED_TYPES:
                        object_value["key"] = landed_keys[scored_type]
                    if scored_type in SECTION_TYPES:
                        object_value["subsections"] = []
                    record["expected"]["objects"].append(object_value)
                    changed = True
                if changed:
                    path.write_text(
                        "\n".join(json.dumps(record) for record in records) + "\n",
                        encoding="utf-8",
                    )

            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "extraction", "--set", str(copied)],
                **_run_kwargs(EvalHost()),
            )
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertIn("gate: refuse", stdout)
        self.assertIn("record: ok — skipped", stdout)
        _assert_comparison_lines(self, stdout)

    def test_failing_release_run_is_recorded_before_gate(self) -> None:
        host = EvalHost()
        loaded = load_set(ROOT / SET_ROOT).loaded
        assert loaded is not None
        clean = run_extraction(loaded, "extraction", _run_context())
        failing_result = SliceResult(
            False,
            clean.report,
            (CaseResult(
                clean.results[0].case_id,
                clean.results[0].repeat,
                "fail",
                clean.results[0].metrics,
                clean.results[0].judge,
                clean.results[0].latency_ms,
            ),
             *clean.results[1:]),
        )
        with patch.object(
            command, "_run_repeats", return_value=command._RunRepeats(failing_result, 1, 1, None)
        ):
            code, stdout, _ = _invoke(
                ["eval", "run", "--slice", "extraction"], **_run_kwargs(host)
            )
        self.assertEqual(code, 1)
        self.assertIn("record: ok — run", stdout)
        self.assertIn("gate: refuse", stdout)
        _assert_comparison_lines(self, stdout)
        write_sql = next(input for argv, input in host.calls if argv[0] == "docker" and input != "SELECT 1;\n")
        self.assertIn("\\set run_verdict 'fail'", write_sql or "")
        self.assertIn("\\set result_0_verdict 'fail'", write_sql or "")

    def test_regression_fails_gate_and_names_id_after_recording_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = _reference_checkout(directory)
            loaded = load_set(checkout / SET_ROOT).loaded
            assert loaded is not None
            clean = run_extraction(loaded, "extraction", _run_context())
            target = next(result.case_id for result in clean.results if result.verdict == "pass")
            _reference_files(checkout, loaded, clean.results)
            host = EvalHost()
            failing = _changed_result(loaded, {target: "fail"})
            with patch.object(command, "_run_repeats", return_value=command._RunRepeats(failing, 1, 1, None)):
                code, stdout, stderr = _invoke(
                    ["eval", "run", "--slice", "extraction"],
                    **_run_kwargs(host, checkout=checkout),
                )
            write_sql = next(
                input for argv, input in host.calls if argv[0] == "docker" and input != "SELECT 1;\n"
            )
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        _assert_comparison_lines(self, stdout, "regressed")
        self.assertIn(target, stdout)
        self.assertLess(stdout.index("record: ok — run"), stdout.index("gate: refuse"))
        self.assertIn("\\set run_verdict 'fail'", write_sql or "")

    def test_other_version_refuses_after_record_with_writer_fix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = _reference_checkout(directory)
            loaded = load_set(checkout / SET_ROOT).loaded
            assert loaded is not None
            clean = run_extraction(loaded, "extraction", _run_context())
            _reference_files(
                checkout,
                loaded,
                clean.results,
                eval_set_version="eval-v-fictitious-other",
            )
            host = EvalHost()
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "extraction"],
                **_run_kwargs(host, checkout=checkout),
            )
            write_sql = next(
                input for argv, input in host.calls if argv[0] == "docker" and input != "SELECT 1;\n"
            )
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        _assert_comparison_lines(self, stdout, "other-version")
        self.assertLess(stdout.index("record: ok — run"), stdout.index("gate: refuse"))
        self.assertIn(f"eval reference --run {RUN_ID}", stdout)
        # A refused comparison judges nothing, so the row carries the slice
        # gate's verdict alone — the first run of a new set version is the one
        # the new reference is written from, and it must not record as fail.
        self.assertIn("\\set run_verdict 'pass'", write_sql or "")

    def test_malformed_reference_refuses_with_restore_fix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = _reference_checkout(directory)
            loaded = load_set(checkout / SET_ROOT).loaded
            assert loaded is not None
            clean = run_extraction(loaded, "extraction", _run_context())
            _reference_files(checkout, loaded, clean.results)
            path = next((checkout / reference.REFERENCE_ROOT / "extraction").glob("*.json"))
            path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
            host = EvalHost()
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "extraction"],
                **_run_kwargs(host, checkout=checkout),
            )
        self.assertEqual(code, 1)
        self.assertIn("canonical serialization", stderr)
        _assert_comparison_lines(self, stdout, "malformed")
        self.assertIn(reference.SLICE_REPAIR_FIX, stdout)
        self.assertNotIn("eval reference --run", stdout)

    def test_bounds_and_regression_fixes_are_in_bounds_first_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = _reference_checkout(directory)
            loaded = load_set(checkout / SET_ROOT).loaded
            assert loaded is not None
            clean = run_extraction(loaded, "extraction", _run_context())
            target = next(result.case_id for result in clean.results if result.verdict == "pass")
            _reference_files(checkout, loaded, clean.results)
            failing = _changed_result(loaded, {target: "fail"})
            failing = replace(failing, verdict=False)
            with patch.object(command, "_run_repeats", return_value=command._RunRepeats(failing, 1, 1, None)):
                code, stdout, stderr = _invoke(
                    ["eval", "run", "--slice", "extraction"],
                    **_run_kwargs(EvalHost(), checkout=checkout),
                )
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        _assert_comparison_lines(self, stdout, "regressed")
        self.assertLess(
            stdout.index("Review the miss and false hit lines"),
            stdout.index(reference.REGRESSION_FIX),
        )

    def test_no_reference_says_so_and_the_bounds_decide_alone(self) -> None:
        """Criterion 3: the absence is stated and never changes the exit code."""

        with tempfile.TemporaryDirectory() as directory:
            checkout = _reference_checkout(directory)
            self.assertFalse((checkout / "eval" / "reference" / "extraction").exists())
            host = EvalHost()
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "extraction"],
                **_run_kwargs(host, checkout=checkout),
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        _assert_comparison_lines(self, stdout, "absent")
        self.assertIn("gate: ok", stdout)
        self.assertIn("no reference for extraction", stdout)

    def test_stale_reference_keeps_exit_zero_and_names_rerecord(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = _reference_checkout(directory)
            loaded = load_set(checkout / SET_ROOT).loaded
            assert loaded is not None
            clean = run_extraction(loaded, "extraction", _run_context())
            target = next(result.case_id for result in clean.results if result.verdict == "pass")
            reference_verdicts = {
                result.case_id: cast(reference.Verdict, "fail" if result.case_id == target else result.verdict)
                for result in clean.results
            }
            _reference_files(
                checkout,
                loaded,
                clean.results,
                reference_verdicts=reference_verdicts,
            )
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "extraction"],
                **_run_kwargs(EvalHost(), checkout=checkout),
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        _assert_comparison_lines(self, stdout, "stale")
        self.assertIn(f"re-record with gideon eval reference --run {RUN_ID}", stdout)

    def test_set_run_still_compares_against_release_checkout_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = _reference_checkout(directory)
            loaded = load_set(checkout / SET_ROOT).loaded
            assert loaded is not None
            copied = Path(directory) / "set-copy" / loaded.version
            copied.parent.mkdir()
            shutil.copytree(checkout / SET_ROOT, copied)
            clean = run_extraction(loaded, "extraction", _run_context())
            target = next(result.case_id for result in clean.results if result.verdict == "pass")
            _reference_files(checkout, loaded, clean.results)
            host = EvalHost()
            failing = _changed_result(loaded, {target: "fail"})
            with patch.object(command, "_run_repeats", return_value=command._RunRepeats(failing, 1, 1, None)):
                code, stdout, stderr = _invoke(
                    ["eval", "run", "--slice", "extraction", "--set", str(copied)],
                    **_run_kwargs(host, checkout=checkout),
                )
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        _assert_comparison_lines(self, stdout, "regressed")
        self.assertIn(target, stdout)
        self.assertIn("record: ok — skipped", stdout)

    def test_set_is_never_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = load_set(ROOT / SET_ROOT).loaded
            assert loaded is not None
            copied = Path(directory) / loaded.version
            shutil.copytree(ROOT / SET_ROOT, copied)
            host = EvalHost()
            code, stdout, _ = _invoke(
                ["eval", "run", "--slice", "extraction", "--set", str(copied)],
                **_run_kwargs(host),
            )
        self.assertEqual(code, 0)
        self.assertIn("record: ok — skipped", stdout)
        self.assertEqual(host.calls, [])

    def test_unreachable_database_skips_record_and_runs_no_git_probe(self) -> None:
        host = EvalHost(probe_rc=1)
        code, stdout, _ = _invoke(
            ["eval", "run", "--slice", "extraction"], **_run_kwargs(host)
        )
        self.assertEqual(code, 0)
        self.assertIn("rows were not written", stdout)
        _assert_comparison_lines(self, stdout)
        self.assertEqual([argv[0] for argv, _ in host.calls], ["docker"])

    def test_failed_write_refuses_and_gate_is_still_last(self) -> None:
        host = EvalHost(write_rc=1)
        code, stdout, _ = _invoke(
            ["eval", "run", "--slice", "extraction"], **_run_kwargs(host)
        )
        self.assertEqual(code, 1)
        self.assertIn("record: refuse", stdout)
        self.assertIn("gate: ok", stdout)
        _assert_comparison_lines(self, stdout)
        self.assertLess(stdout.index("record:"), stdout.index("gate:"))

    def test_git_provenance_is_bound_and_dirty_state_is_recorded(self) -> None:
        host = EvalHost(status_stdout=" M changed.py\n")
        code, stdout, _ = _invoke(
            ["eval", "run", "--slice", "extraction"], **_run_kwargs(host)
        )
        self.assertEqual(code, 0)
        self.assertIn("record: ok", stdout)
        _assert_comparison_lines(self, stdout)
        git_calls = [argv for argv, _ in host.calls if argv[0] == "git"]
        self.assertEqual(len(git_calls), 2)
        for argv in git_calls:
            self.assertEqual(argv[1:3], ("-c", f"safe.directory={ROOT}"))
            self.assertEqual(argv[3:5], ("-C", str(ROOT)))
        write_sql = next(input for argv, input in host.calls if argv[0] == "docker" and input != "SELECT 1;\n")
        self.assertIn("\\set git_dirty 'true'", write_sql or "")

    def test_no_git_entry_records_both_provenance_values_as_null_without_git(self) -> None:
        host = EvalHost(no_git=True)
        code, stdout, _ = _invoke(
            ["eval", "run", "--slice", "extraction"], **_run_kwargs(host)
        )
        self.assertEqual(code, 0)
        _assert_comparison_lines(self, stdout)
        self.assertFalse(any(argv[0] == "git" for argv, _ in host.calls))
        write_sql = next(input for argv, input in host.calls if argv[0] == "docker" and input != "SELECT 1;\n")
        self.assertIn("NULL, NULL, :'set_digest'", write_sql or "")

    def test_git_failure_or_empty_commit_refuses_before_write(self) -> None:
        for overrides in ({"commit_rc": 1}, {"commit_stdout": ""}, {"status_rc": 1}):
            with self.subTest(overrides=overrides):
                host = EvalHost(**overrides)
                code, stdout, _ = _invoke(
                    ["eval", "run", "--slice", "extraction"], **_run_kwargs(host)
                )
                self.assertEqual(code, 1)
                self.assertIn("record: refuse", stdout)
                _assert_comparison_lines(self, stdout)
                self.assertFalse(
                    any(argv[0] == "docker" and input != "SELECT 1;\n" for argv, input in host.calls)
                )
                self.assertIn("as the checkout owner", stdout)

    def test_flag_rules_refuse_before_any_stage(self) -> None:
        cases = (
            (["eval", "run", "--slice", "guardrails", "--decision"], "--decision requires --against"),
            (["eval", "run", "--slice", "guardrails", "--against", RUN_ID], "--against requires --decision"),
            (["eval", "run", "--slice", "extraction", "--decision", "--against", RUN_ID], "no decision metric"),
            (["eval", "run", "--slice", "guardrails", "--kind", "nightly", "--decision", "--against", RUN_ID], "records its own kind, not 'nightly'"),
            (["eval", "run", "--slice", "extraction", "--force"], "does not reach the engine"),
        )
        for argv, expected in cases:
            with self.subTest(argv=argv):
                host = EvalHost()
                code, stdout, stderr = _invoke(argv, **_run_kwargs(host))
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("gideon eval run:", stderr)
                self.assertIn(expected, stderr)
                self.assertIn("Fix:", stderr)
                if "nightly" in argv:
                    self.assertIn("Remove --kind nightly", stderr)
                self.assertEqual(host.calls, [])

    def test_slice_selection_refuses_with_fixes(self) -> None:
        for argv in (["eval", "run"], ["eval", "run", "--slice", "missing-slice"]):
            with self.subTest(argv=argv):
                code, stdout, stderr = _invoke(argv, **_run_kwargs(EvalHost()))
                self.assertEqual(code, 1)
                refusal_text = stderr if stderr else stdout
                self.assertIn("Fix:", refusal_text)
                self.assertNotIn("reference:", stdout)

        next_opening = datetime(2026, 9, 21, 19, 0, tzinfo=UTC)
        outside = command.window.WindowJudgement(
            False, "office hours", next_opening, NOW + timedelta(days=1)
        )
        with patch.object(command.window, "window_judgement", return_value=outside):
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "judge-triples"],
                **_run_kwargs(EvalHost()),
            )
        self.assertEqual(code, 1)
        self.assertIn("add --force for an announced window.", stderr or stdout)

    def test_refused_load_prints_no_reference_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing_courts = Path(directory) / "missing-courts.yaml"
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "extraction"],
                **_run_kwargs(EvalHost(), court_path=missing_courts),
            )
        self.assertEqual(code, 1)
        self.assertIn("Fix:", stderr if stderr else stdout)
        self.assertNotIn("reference:", stdout)

    def test_missing_runner_prints_no_reference_line(self) -> None:
        with patch.object(command, "SLICE_RUNNERS", {}):
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "extraction"], **_run_kwargs(EvalHost())
            )
        self.assertEqual(code, 1)
        # An empty stderr keeps the crash guard's own Fix line from passing this.
        self.assertEqual(stderr, "")
        self.assertIn("run: refuse — no runner serves slice 'extraction'", stdout)
        self.assertIn("Fix:", stdout)
        self.assertNotIn("reference:", stdout)

    def test_ranked_flag_refuses_for_extraction(self) -> None:
        host = EvalHost()
        code, stdout, stderr = _invoke(
            ["eval", "run", "--slice", "extraction", "--ranked", "/tmp/fictitious-ranked.jsonl"],
            **_run_kwargs(host),
        )
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertIn("ranked: refuse", stdout)
        self.assertIn("Remove --ranked", stdout)
        self.assertFalse(any(argv[0] == "docker" for argv, _input in host.calls))

    def test_signoff_slice_row_names_unsigned_count_and_zero(self) -> None:
        if absent_from_export(RESEARCH_QA_CASES_PATH, ROOT):
            self.skipTest("in an export the case this slice names is absent, so the set cannot load it")
        slice_name = "research-qa-test"
        replacement = dict(SLICE_RUNNERS)
        replacement[slice_name] = replace(
            SLICE_RUNNERS["extraction"],
            compares_reference=False,
            runner=_passing_selection_runner,
        )
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "eval-v1"
            shutil.copytree(ROOT / SET_ROOT, copied)
            ids_path = copied / "slices" / slice_name / "cases.ids"
            ids_path.parent.mkdir(parents=True)
            ids_path.write_text("research-qa-001\n", encoding="utf-8")
            with patch.object(command, "SLICE_RUNNERS", replacement):
                unsigned_code, unsigned_stdout, unsigned_stderr = _invoke(
                    ["eval", "run", "--slice", slice_name, "--set", str(copied)],
                    **_run_kwargs(EvalHost()),
                )
            signoff_path = copied / signoffs.SIGNOFFS_PATH
            signoff_path.parent.mkdir(parents=True, exist_ok=True)
            signoff_path.write_text(
                signoffs.serialize(
                    signoffs.SignOff(
                        "research-qa-001",
                        "A fictional signed answer.",
                        ("fictional/source-1",),
                        "CHU-attorney-1",
                        "2026-09-21",
                    )
                ),
                encoding="utf-8",
            )
            with patch.object(command, "SLICE_RUNNERS", replacement):
                signed_code, signed_stdout, signed_stderr = _invoke(
                    ["eval", "run", "--slice", slice_name, "--set", str(copied)],
                    **_run_kwargs(EvalHost()),
                )
        self.assertEqual(unsigned_code, 0)
        self.assertEqual(unsigned_stderr, "")
        self.assertIn("run: ok — 0 active cases evaluated, 1 unsigned excluded", unsigned_stdout)
        self.assertEqual(signed_code, 0)
        self.assertEqual(signed_stderr, "")
        self.assertIn("run: ok — 1 active cases evaluated, 0 unsigned excluded", signed_stdout)

    def test_runner_result_for_unsigned_case_refuses_before_record(self) -> None:
        if absent_from_export(RESEARCH_QA_CASES_PATH, ROOT):
            self.skipTest("in an export the only sign-off-taking cases are absent, so none is unsigned")
        loaded = load_set(ROOT / SET_ROOT).loaded
        self.assertIsNotNone(loaded)
        assert loaded is not None
        unsigned_id = min(loaded.unsigned_ids)
        bad_result = SliceResult(
            True,
            "unsigned result report\n",
            (CaseResult(unsigned_id, 1, "pass", {}, None, None),),
        )
        with patch.object(command, "_run_repeats", return_value=command._RunRepeats(bad_result, 1, 1, None)):
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "extraction"], **_run_kwargs(EvalHost())
            )
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertIn(f"runner returned unsigned cases: {unsigned_id}", stdout)
        self.assertIn("must select through the loader", stdout)
        self.assertNotIn("record:", stdout)
        self.assertNotIn("gate:", stdout)


def _invoke_engine_command(
    host: EvalHost,
    *,
    slice_name: str = "smoke",
    stack_name: str = "ci",
    kind: str = "smoke",
    rendered_dir: Path | None = None,
    set_root: Path | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    on_run: Callable[[RunContext], SliceResult] | None = None,
) -> tuple[int, str, str, dict[str, list[Any]]]:
    observed: dict[str, list[Any]] = {
        "contexts": [],
        "client": [],
        "door": [],
        "engine_paths": [],
    }

    def make_client(*args: Any, **kwargs: Any) -> object:
        observed["client"].append((args, kwargs))
        return object()

    def probe_door(*args: Any, **kwargs: Any) -> command.door.ProbeResult:
        observed["door"].append((args, kwargs))
        return command.door.ProbeResult(True, "door ready", None)

    def run_slice(
        _spec: Any, _loaded: LoadedSet, _slice_name: str, context: RunContext
    ) -> SliceResult:
        observed["contexts"].append(context)
        if on_run is not None:
            return on_run(context)
        return SliceResult(True, "smoke run report\n", ())

    def resolve_engine(
        _host: Host, path: PathLike, **_kwargs: Any
    ) -> command.engine.EngineTarget:
        observed["engine_paths"].append(path)
        return command.engine.EngineTarget("fictitious-profile", "fictitious-model", 1)

    set_args = [] if set_root is None else ["--set", str(set_root)]
    args = [
        "eval", "run", "--slice", slice_name, "--stack", stack_name,
        "--kind", kind, *set_args,
    ]
    run_kwargs = _run_kwargs(host)
    if rendered_dir is not None:
        run_kwargs["rendered_dir"] = rendered_dir
    if clock is not None:
        run_kwargs["clock"] = clock
    if sleep is not None:
        run_kwargs["sleep"] = sleep
    with (
        patch.object(
            command.engine,
            "resolve_engine_target",
            side_effect=resolve_engine,
        ),
        patch.object(command.access, "read_eval_password", return_value="fictitious-password"),
        patch.object(command.access, "make_client_factory", side_effect=make_client),
        patch.object(command.door, "probe", side_effect=probe_door),
        patch.object(command, "_run_slice", side_effect=run_slice),
        patch.object(command.stacks.secrets, "select_directory") as select_directory,
    ):
        outcome = _invoke(args, **run_kwargs)
        if stack_name == "ci":
            select_directory.assert_called_once_with(Path(stacks.CI_SECRETS_DIR))
        else:
            select_directory.assert_not_called()
    return (*outcome, observed)


def _nightly_start() -> datetime:
    site_result = command.site.load_site(ROOT / "config/site.example.yaml")
    assert site_result.config is not None, site_result.errors
    return datetime.combine(NOW.date(), time(21), tzinfo=ZoneInfo(site_result.config.office.timezone))


class EngineStackAndLock(unittest.TestCase):
    def test_ci_threads_turn_paths_records_identity_and_waives_the_window(self) -> None:
        host = EvalHost()
        production_dir = Path("/tmp/fictitious-production-rendered")
        office = command.window.WindowJudgement(
            False, "fictitious office hours", NOW + timedelta(hours=1), NOW + timedelta(days=1)
        )
        with patch.object(command.window, "window_judgement", return_value=office):
            code, stdout, stderr, observed = _invoke_engine_command(
                host, rendered_dir=production_dir
            )
        loaded = load_set(ROOT / SET_ROOT).loaded
        assert loaded is not None
        engine_calls = SLICE_RUNNERS["smoke"].engine_calls
        assert engine_calls is not None
        count = engine_calls(loaded, "smoke")

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn(
            f"{count} engine calls within the any-hour allowance of {command.run.SMOKE_TURNS}",
            stdout,
        )
        self.assertIn("engine lock taken", stdout)
        self.assertEqual(observed["engine_paths"], [production_dir])
        self.assertEqual(observed["client"][0][1]["stack"], "ci")
        self.assertEqual(observed["door"][0][0][1], Path(stacks.CI_ROOT))
        self.assertEqual(observed["door"][0][1]["max_time"], command.run.TURN_TIMEOUT_SECONDS)
        self.assertEqual(observed["client"][0][1]["timeout"], command.run.TURN_TIMEOUT_SECONDS)
        self.assertEqual(observed["contexts"][0].rendered_dir, Path(stacks.CI_ROOT))
        self.assertEqual(observed["contexts"][0].engine_dir, production_dir)
        self.assertEqual(host.locks, {})
        self.assertEqual(len(host.lock_records), 1)
        lock_record = backuplock.parse(host.lock_records[0][1])
        self.assertIsNotNone(lock_record)
        assert lock_record is not None
        self.assertIn("smoke", lock_record.command)
        self.assertIn("ci", lock_record.command)
        write_sql = next(
            input
            for argv, input in host.calls
            if argv[0] == "docker" and input is not None and input != "SELECT 1;\n"
        )
        write_argv = next(
            argv
            for argv, input in host.calls
            if argv[0] == "docker" and input is not None and input != "SELECT 1;\n"
        )
        self.assertIn("\\set run_stack 'ci'", write_sql)
        self.assertIn("\\set kind 'smoke'", write_sql)
        self.assertIn(str(production_dir), write_argv)
        self.assertNotIn(stacks.CI_ROOT, write_argv)

    def test_ci_refusals_are_registry_driven_before_the_lock(self) -> None:
        for slice_name, spec in SLICE_RUNNERS.items():
            if spec.drives_turns or spec.judge_prompt is not None:
                continue
            with self.subTest(slice_name=slice_name):
                if absent_from_export(f"eval/sets/eval-v1/slices/{slice_name}", ROOT):
                    self.skipTest("in an export this slice's lists are absent, so load refuses it first")
                host = EvalHost()
                with patch.object(command.stacks.secrets, "select_directory"):
                    code, stdout, stderr = _invoke(
                        ["eval", "run", "--slice", slice_name, "--stack", "ci"],
                        **_run_kwargs(host),
                    )
                self.assertEqual(code, 1)
                self.assertEqual(stderr, "")
                self.assertIn("load: refuse", stdout)
                self.assertIn("the slice reaches nothing a stack names", stdout)
                self.assertIn("Run this slice with --stack production", stdout)
                self.assertEqual(host.lock_records, [])

    def test_engine_lock_refusal_nested_pass_and_post_lock_refusal(self) -> None:
        lock = backuplock.ENGINE_LOCK
        other = EvalHost()
        other_pid = os.getpid() + 1
        other.locks[lock.path] = backuplock.Record(
            "fictitious nightly", other_pid, NOW
        ).to_json()
        code, stdout, _stderr, _observed = _invoke_engine_command(other)
        self.assertEqual(code, 1)
        self.assertIn("fictitious nightly", stdout)
        self.assertIn(f"ps -p {other_pid}", stdout)
        self.assertIn("then retry", stdout)
        self.assertIn("engine lock is held by", stdout)
        self.assertIn(lock.path, other.locks)

        unreadable = EvalHost()
        unreadable.locks[lock.path] = "visibly fictitious unreadable lock record"
        code, stdout, _stderr, _observed = _invoke_engine_command(unreadable)
        self.assertEqual(code, 1)
        self.assertIn("record unreadable", stdout)
        self.assertIn(lock.wait_fix, stdout)
        self.assertEqual(
            unreadable.locks[lock.path], "visibly fictitious unreadable lock record"
        )

        nested = EvalHost()
        own_record = backuplock.Record("caller owns lock", os.getpid(), NOW).to_json()
        nested.locks[lock.path] = own_record
        code, stdout, _stderr, _observed = _invoke_engine_command(nested)
        self.assertEqual(code, 0)
        self.assertIn("engine lock held by this process", stdout)
        self.assertEqual(nested.locks[lock.path], own_record)

        absent = EvalHost(ci_stack_present=False)
        code, stdout, _stderr, observed = _invoke_engine_command(absent)
        self.assertEqual(code, 1)
        self.assertIn("Run sudo python3 -m tools.cistack up, then retry.", stdout)
        self.assertEqual(observed["door"], [])
        self.assertEqual(absent.locks, {})

        unprivileged = EvalHost(effective_uid=1000)
        code, stdout, _stderr, _observed = _invoke_engine_command(unprivileged)
        self.assertEqual(code, 1)
        self.assertIn("--stack ci --kind smoke", stdout)
        self.assertEqual(unprivileged.lock_records, [])
        self.assertIn(
            "--stack ci --kind smoke",
            command._record_root_fix(
                "smoke", SLICE_RUNNERS["smoke"], " --stack ci --kind smoke"
            ),
        )

    def test_count_over_allowance_refuses_outside_window_and_releases(self) -> None:
        spec = SLICE_RUNNERS["smoke"]
        calls = spec.engine_calls
        assert calls is not None
        replacement = dict(SLICE_RUNNERS)
        replacement["smoke"] = replace(
            spec,
            engine_calls=lambda _loaded, _slice: command.run.SMOKE_TURNS + 1,
        )
        outside = command.window.WindowJudgement(
            False, "fictitious office closure", NOW + timedelta(hours=1), NOW + timedelta(days=1)
        )
        host = EvalHost()
        with (
            patch.object(command, "SLICE_RUNNERS", replacement),
            patch.object(command.window, "window_judgement", return_value=outside),
        ):
            code, stdout, _stderr, _observed = _invoke_engine_command(host)
        count = command.run.SMOKE_TURNS + 1
        self.assertEqual(code, 1)
        self.assertIn(f"{count} engine calls exceed the any-hour allowance", stdout)
        self.assertIn("Next opening is", stdout)
        self.assertEqual(host.locks, {})
        self.assertEqual(host.lock_records, [])

    def test_in_window_count_is_reported_without_a_waiver(self) -> None:
        inside = command.window.WindowJudgement(
            True, "fictitious office hours", NOW + timedelta(hours=1), NOW + timedelta(days=1)
        )
        host = EvalHost()
        with patch.object(command.window, "window_judgement", return_value=inside):
            code, stdout, _stderr, _observed = _invoke_engine_command(host)
        loaded = load_set(ROOT / SET_ROOT).loaded
        assert loaded is not None
        engine_calls = SLICE_RUNNERS["smoke"].engine_calls
        assert engine_calls is not None
        count = engine_calls(loaded, "smoke")
        self.assertEqual(code, 0)
        self.assertIn(f"{count} engine calls, engine lock taken", stdout)
        self.assertNotIn("within the any-hour allowance", stdout)


class NightlyCommand(unittest.TestCase):
    """Bound the timer's runs to one night so a wait cannot carry into daytime."""

    def test_free_lock_starts_at_once_and_uses_the_nights_deadline(self) -> None:
        host = EvalHost()
        started = _nightly_start()
        end = datetime.combine(started.date() + timedelta(days=1), time(6), tzinfo=started.tzinfo)
        now = [started]

        def unexpected_sleep(_seconds: float) -> None:
            self.fail("an available lock must not sleep")

        code, stdout, stderr, observed = _invoke_engine_command(
            host,
            slice_name="general-smoke",
            stack_name="production",
            kind=command.NIGHTLY_KIND,
            clock=lambda: now[0],
            sleep=unexpected_sleep,
        )
        self.assertEqual((code, stderr), (0, ""), stdout + stderr)
        self.assertIn("preconditions: ok", stdout)
        self.assertIn("inside the night", stdout)
        self.assertIn("engine lock taken", stdout)
        self.assertNotIn("waiting for the engine lock", stdout)
        production_dir = _run_kwargs(host)["rendered_dir"]
        self.assertEqual(observed["contexts"][0].rendered_dir, Path(production_dir))
        self.assertEqual(observed["contexts"][0].engine_dir, production_dir)
        self.assertEqual(len(host.lock_records), 1)
        lock_record = backuplock.parse(host.lock_records[0][1])
        assert lock_record is not None
        self.assertEqual(lock_record.started, started)
        self.assertEqual(host.locks, {})
        write_sql = next(
            cast(str, statement)
            for argv, statement in host.calls
            if argv[0] == "docker" and statement is not None and "INSERT INTO eval_runs" in statement
        )
        self.assertIn("\\set kind 'nightly'", write_sql)
        self.assertIn(f"\\set run_started_at '{started.isoformat()}'", write_sql)
        context = cast(RunContext, observed["contexts"][0])
        now[0] = end - timedelta(microseconds=1)
        context.checkpoint()
        now[0] = end
        with self.assertRaises(window.WindowOverrun) as raised:
            context.checkpoint()
        self.assertEqual(raised.exception.end, end)

    def test_reaching_the_deadline_records_a_partial_nightly_run(self) -> None:
        host = EvalHost()
        started = _nightly_start()
        end = datetime.combine(started.date() + timedelta(days=1), time(6), tzinfo=started.tzinfo)
        now = [started]

        def finish_at_deadline(_context: RunContext) -> SliceResult:
            now[0] = end
            return SliceResult(True, "visibly fictitious completed turn\n", ())

        def unexpected_sleep(_seconds: float) -> None:
            self.fail("an available lock must not sleep")

        code, stdout, stderr, _observed = _invoke_engine_command(
            host,
            slice_name="general-smoke",
            stack_name="production",
            kind=command.NIGHTLY_KIND,
            clock=lambda: now[0],
            sleep=unexpected_sleep,
            on_run=finish_at_deadline,
        )
        self.assertEqual((code, stderr), (1, ""))
        self.assertIn(f"aborted at the window end {end.isoformat()}", stdout)
        self.assertIn("1 run row, 0 result rows", stdout)
        self.assertIn("gate: refuse", stdout)
        write_sql = next(
            cast(str, statement)
            for argv, statement in host.calls
            if argv[0] == "docker" and statement is not None and "INSERT INTO eval_runs" in statement
        )
        self.assertIn("\\set kind 'nightly'", write_sql)
        self.assertIn("\\set partial 'true'", write_sql)
        self.assertIn("\\set run_verdict 'fail'", write_sql)
        self.assertIn(f"\\set run_started_at '{started.isoformat()}'", write_sql)

    def test_held_lock_waits_for_each_poll_and_records_the_effective_start(self) -> None:
        host = EvalHost()
        started = _nightly_start()
        held_by = backuplock.Record(
            "visibly fictitious hand run", os.getpid() + 1, started - timedelta(minutes=5)
        )
        host.locks[backuplock.ENGINE_LOCK.path] = held_by.to_json()
        now = [started]
        sleeps: list[float] = []
        poll_count = 3

        def advance(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += timedelta(seconds=seconds)
            if len(sleeps) == poll_count:
                del host.locks[backuplock.ENGINE_LOCK.path]

        code, stdout, stderr, _observed = _invoke_engine_command(
            host,
            slice_name="general-smoke",
            stack_name="production",
            kind=command.NIGHTLY_KIND,
            clock=lambda: now[0],
            sleep=advance,
        )
        self.assertEqual((code, stderr), (0, ""), stdout + stderr)
        self.assertEqual(sleeps, [command.NIGHTLY_LOCK_POLL_SECONDS] * poll_count)
        self.assertEqual(stdout.count("waiting for the engine lock held by"), 1)
        self.assertIn(
            f"{held_by.command} (pid {held_by.pid}) since {held_by.started.isoformat()}",
            stdout,
        )
        self.assertIn(
            f"polling every {command.NIGHTLY_LOCK_POLL_SECONDS} s until "
            f"{datetime.combine(started.date() + timedelta(days=1), time(6), tzinfo=started.tzinfo).isoformat()}",
            stdout,
        )
        self.assertIn(
            f"engine lock taken after 0 h {poll_count} min behind "
            f"{held_by.command} (pid {held_by.pid})",
            stdout,
        )
        self.assertEqual(len(host.lock_records), poll_count + 1)
        taken = backuplock.parse(host.lock_records[-1][1])
        assert taken is not None
        self.assertEqual(taken.started, now[0])
        self.assertEqual(host.locks, {})
        write_sql = next(
            cast(str, statement)
            for argv, statement in host.calls
            if argv[0] == "docker" and statement is not None and "INSERT INTO eval_runs" in statement
        )
        self.assertIn(f"\\set run_started_at '{now[0].isoformat()}'", write_sql)

    def test_held_lock_at_the_nights_end_skips_without_a_run(self) -> None:
        host = EvalHost()
        night = _nightly_start()
        end = datetime.combine(night.date() + timedelta(days=1), time(6), tzinfo=night.tzinfo)
        now = [end - timedelta(minutes=1)]
        held_by = backuplock.Record("visibly fictitious weekend decision", os.getpid() + 1, night)
        held_text = held_by.to_json()
        host.locks[backuplock.ENGINE_LOCK.path] = held_text
        sleeps: list[float] = []

        def advance(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += timedelta(seconds=seconds)

        code, stdout, stderr, _observed = _invoke_engine_command(
            host,
            slice_name="general-smoke",
            stack_name="production",
            kind=command.NIGHTLY_KIND,
            clock=lambda: now[0],
            sleep=advance,
        )
        self.assertEqual((code, stderr), (1, ""))
        self.assertEqual(sleeps, [command.NIGHTLY_LOCK_POLL_SECONDS])
        self.assertIn("preconditions: refuse", stdout)
        self.assertIn(f"engine lock held by {held_by.command} (pid {held_by.pid})", stdout)
        self.assertIn(f"through the night's end {end.isoformat()}; waited 0 h 1 min", stdout)
        self.assertIn("The next nightly fires at 21:00 office time", stdout)
        self.assertIn("sudo python3 -m gideon eval run --slice general-smoke", stdout)
        self.assertNotIn("run: ok", stdout)
        self.assertNotIn("record:", stdout)
        self.assertEqual(len(host.lock_records), 1)
        self.assertEqual(host.locks[backuplock.ENGINE_LOCK.path], held_text)
        self.assertFalse(any("INSERT INTO eval_runs" in (statement or "") for _, statement in host.calls))

    def test_outside_the_night_refuses_before_lock_for_weekday_and_weekend(self) -> None:
        night = _nightly_start()
        cases = (
            (night + timedelta(days=2), "general-smoke"),
            (night, "smoke"),
        )
        for day, slice_name in cases:
            now = day.replace(hour=9 if slice_name == "general-smoke" else 12)
            with self.subTest(now=now, slice_name=slice_name):
                host = EvalHost()
                sleeps: list[float] = []

                def read_clock(instant: datetime = now) -> datetime:
                    return instant

                code, stdout, stderr, _observed = _invoke_engine_command(
                    host,
                    slice_name=slice_name,
                    stack_name="production",
                    kind=command.NIGHTLY_KIND,
                    clock=read_clock,
                    sleep=sleeps.append,
                )
                self.assertEqual((code, stderr), (1, ""))
                self.assertIn("preconditions: refuse", stdout)
                self.assertIn("outside the night", stdout)
                self.assertIn("Next opening is", stdout)
                self.assertEqual(sleeps, [])
                self.assertEqual(host.lock_records, [])
                self.assertNotIn("record:", stdout)

    def test_two_suites_across_sunday_morning_skip_then_refuse(self) -> None:
        host = EvalHost()
        night = _nightly_start()
        end = datetime.combine(night.date() + timedelta(days=1), time(6), tzinfo=night.tzinfo)
        now = [end - timedelta(minutes=1)]
        held_by = backuplock.Record("visibly fictitious decision run", os.getpid() + 1, night)
        host.locks[backuplock.ENGINE_LOCK.path] = held_by.to_json()
        sleeps: list[float] = []

        def advance(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += timedelta(seconds=seconds)

        first_code, first_out, first_err, _ = _invoke_engine_command(
            host,
            slice_name="general-smoke",
            stack_name="production",
            kind=command.NIGHTLY_KIND,
            clock=lambda: now[0],
            sleep=advance,
        )
        probes_after_first = len(host.lock_records)
        second_code, second_out, second_err, _ = _invoke_engine_command(
            host,
            slice_name="guardrails",
            stack_name="production",
            kind=command.NIGHTLY_KIND,
            clock=lambda: now[0],
            sleep=advance,
        )
        self.assertEqual((first_code, first_err, second_code, second_err), (1, "", 1, ""))
        self.assertIn("through the night's end", first_out)
        self.assertIn("outside the night", second_out)
        self.assertNotIn("waiting for the engine lock", second_out)
        self.assertEqual(sleeps, [command.NIGHTLY_LOCK_POLL_SECONDS])
        self.assertEqual(len(host.lock_records), probes_after_first)
        self.assertEqual(probes_after_first, 1)
        self.assertFalse(any("INSERT INTO eval_runs" in (statement or "") for _, statement in host.calls))

    def test_manual_with_a_held_lock_refuses_without_waiting(self) -> None:
        host = EvalHost()
        night = _nightly_start()
        held_by = backuplock.Record("visibly fictitious first holder", os.getpid() + 1, night)
        host.locks[backuplock.ENGINE_LOCK.path] = held_by.to_json()
        sleeps: list[float] = []
        code, stdout, stderr, _observed = _invoke_engine_command(
            host,
            slice_name="general-smoke",
            stack_name="production",
            kind="manual",
            clock=lambda: night,
            sleep=sleeps.append,
        )
        self.assertEqual((code, stderr), (1, ""))
        self.assertIn("engine lock is held by", stdout)
        self.assertIn(f"ps -p {held_by.pid}", stdout)
        self.assertNotIn("waiting for the engine lock", stdout)
        self.assertEqual(sleeps, [])
        self.assertEqual(len(host.lock_records), 1)


class RunnerSelection(unittest.TestCase):
    """Every registered runner receives only the loader's counted cases."""

    def test_every_registered_runner_excludes_a_planted_unsigned_case(self) -> None:
        for slice_name, spec in SLICE_RUNNERS.items():
            with self.subTest(slice_name=slice_name):
                case_id = "extraction-001"
                loaded = LoadedSet(
                    version="eval-v-fictional",
                    cases_by_file={"fictional/cases.jsonl": ({"id": case_id},)},
                    cases_by_id={case_id: {"id": case_id}},
                    active_ids=(case_id,),
                    slices={slice_name: (case_id,)},
                    slice_lists={},
                    digest="fictional-digest",
                    unsigned_ids=frozenset({case_id}),
                )
                context = RunContext(
                    cast(Host, EvalHost()),
                    "/tmp/fictitious-rendered",
                    "/tmp/fictitious-rendered",
                    "fictitious-model",
                    "synthesis@1",
                    spec.repeats,
                    lambda _line: None,
                )
                result = spec.runner(loaded, slice_name, context)
                self.assertEqual(result.results, ())


class SliceRegistry(unittest.TestCase):
    """Every registered slice names a prompt that exists and a reference it reads."""

    def test_judge_prompts_are_registered(self) -> None:
        for slice_name, spec in SLICE_RUNNERS.items():
            if spec.judge_prompt is not None:
                with self.subTest(slice_name=slice_name):
                    self.assertIn(spec.judge_prompt, judge.PROMPT_REGISTRY)

    def test_every_committed_reference_is_one_eval_run_compares(self) -> None:
        committed = sorted(
            path.name for path in (ROOT / reference.REFERENCE_ROOT).iterdir() if path.is_dir()
        )
        self.assertIn("extraction", committed)
        for slice_name in committed:
            with self.subTest(slice_name=slice_name):
                self.assertTrue(SLICE_RUNNERS[slice_name].compares_reference)

    def test_takes_ranked_is_boolean_and_only_judgments_takes_one(self) -> None:
        for slice_name, spec in SLICE_RUNNERS.items():
            with self.subTest(slice_name=slice_name):
                self.assertIs(type(spec.takes_ranked), bool)
        self.assertTrue(SLICE_RUNNERS["judgments"].takes_ranked)
        self.assertTrue(
            all(
                not spec.takes_ranked
                for slice_name, spec in SLICE_RUNNERS.items()
                if slice_name != "judgments"
            )
        )

    def test_drives_turns_is_true_only_for_turn_driven_slices(self) -> None:
        for slice_name, spec in SLICE_RUNNERS.items():
            with self.subTest(slice_name=slice_name):
                self.assertIs(type(spec.drives_turns), bool)
                self.assertEqual(
                    spec.drives_turns,
                    slice_name in {"guardrails", "general-smoke", "smoke"},
                )

    def test_stack_resolver_selects_the_ci_secrets_directory_once(self) -> None:
        with patch.object(stacks.secrets, "select_directory") as select_directory:
            production = stacks.resolve_stack("production", "/tmp/rendered")
            select_directory.assert_not_called()
            self.assertEqual(production.name, "production")
            self.assertEqual(production.turns_dir, Path("/tmp/rendered"))
            self.assertIsNone(production.secrets_dir)

            ci = stacks.resolve_stack("ci", "/tmp/rendered")
            select_directory.assert_called_once_with(Path(stacks.CI_SECRETS_DIR))
            self.assertEqual(ci.name, "ci")
            self.assertEqual(ci.turns_dir, Path(stacks.CI_ROOT))
            self.assertEqual(ci.secrets_dir, Path(stacks.CI_SECRETS_DIR))
            self.assertEqual(ci.flag_fragment, " --stack ci")
            self.assertEqual(production.flag_fragment, "")

    def test_engine_call_estimate_is_set_only_for_smoke(self) -> None:
        self.assertEqual(
            {name for name, spec in SLICE_RUNNERS.items() if spec.engine_calls is not None},
            {"smoke"},
        )

    def test_decision_slices_run_one_repeat_per_call(self) -> None:
        for slice_name, spec in SLICE_RUNNERS.items():
            if spec.decision is not None:
                with self.subTest(slice_name=slice_name):
                    self.assertEqual(spec.repeats, 1)


class Imports(unittest.TestCase):
    """The evaluation package stays within the standard-library import boundary."""

    def test_every_import_is_standard_library_yaml_or_gideon(self) -> None:
        standard_library = set(sys.stdlib_module_names)
        for path in sorted(EVALUATION.rglob("*.py")):
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
                    top = name.split(".", 1)[0]
                    if (
                        path == EVALUATION / "turns/chromium.py"
                        and top == "playwright"
                    ):
                        # test_turns_browser.py owns the function-or-TYPE_CHECKING boundary.
                        continue
                    self.assertTrue(
                        top in standard_library or top in {"yaml", "gideon"},
                        f"{path}: non-standard import {name}",
                    )


class ChallengerHost(EvalHost):
    """A build-box host that serves the committed file and canned judge replies."""

    def __init__(self, *, document: str | None = None, build_box: bool = True, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.document = (ROOT / challenger.CHALLENGER_PATH).read_text() if document is None else document
        self.files = {
            str(ROOT / challenger.CHALLENGER_PATH): self.document,
            str(ROOT / "config/site.example.yaml"): (ROOT / "config/site.example.yaml").read_text(),
            str(ROOT / "courts.yaml"): (ROOT / "courts.yaml").read_text(),
        }
        self.build_box = build_box
        self.reads: list[str] = []
        self.exists_calls: list[str] = []
        self.releases = 0
        self.acquisitions = 0
        self.judge_requests: list[dict[str, Any]] = []

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        name = os.fspath(path)
        self.reads.append(name)
        if name not in self.files:
            raise FileNotFoundError(name)
        return self.files[name]

    def exists(self, path: PathLike) -> bool:
        name = os.fspath(path)
        self.exists_calls.append(name)
        if name == str(nogpu.BUILD_BOX_PATH):
            return self.build_box
        if name == str(nogpu.NO_GPU_PATH):
            return False
        if name == str(ROOT / ".git"):
            return not self.no_git
        if name == str(Path(stacks.CI_ROOT) / "compose.yaml"):
            return self.ci_stack_present
        return False

    def run(self, argv: tuple[str, ...] | list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = kwargs.get("input")
        if isinstance(payload, str) and payload.startswith("{"):
            request = json.loads(payload)
            if "response_format" in request:
                self.calls.append((tuple(argv), payload))
                self.judge_requests.append(request)
                return subprocess.CompletedProcess(list(argv), 0, engine_output(valid_content()), "")
        return super().run(argv, **kwargs)

    def release_lock(self, path: PathLike) -> None:
        self.releases += 1
        super().release_lock(path)

    def take_lock(self, path: PathLike, record: str) -> str | None:
        holder = super().take_lock(path, record)
        if holder is None:
            self.acquisitions += 1
        return holder


def _challenger_invoke(
    host: ChallengerHost,
    *,
    run_results: tuple[bool, ...] = (True, True),
    partial_release: bool = False,
    extra: tuple[str, ...] = (),
) -> tuple[int, str, str, list[RunContext]]:
    contexts: list[RunContext] = []
    ids = iter((RUN_ID, "22222222-3333-4444-8555-666666666666"))

    def run_slice(_spec: object, loaded: LoadedSet, slice_name: str, context: RunContext) -> SliceResult:
        contexts.append(context)
        prompt_id = context.judge_prompt_id
        assert prompt_id is not None and context.served_model_name is not None
        slots = {slot: f"visibly fictitious {slot}" for slot in judge.PROMPT_REGISTRY[prompt_id].slots}
        slots["candidate"] = "visibly-fictitious-case-text-sentinel"
        judge.grade(
            cast(Host, host), context.engine_dir,
            served_model_name=context.served_model_name,
            prompt=judge.PROMPT_REGISTRY[prompt_id], slots=slots,
        )
        case_id = next(case_id for case_id in loaded.slices[slice_name] if case_id in loaded.active_ids)
        verdict = run_results[len(contexts) - 1]
        return SliceResult(verdict, "fixture report\n", (CaseResult(case_id, 1, "pass" if verdict else "fail", {}),))

    def repeats(*args: Any, **kwargs: Any) -> command._RunRepeats:
        result = run_slice(args[0], args[1], args[2], args[3])
        return command._RunRepeats(result, 1, 0 if partial_release else 1, NOW if partial_release else None)

    kwargs = _run_kwargs(host)
    kwargs["run_id_factory"] = lambda: next(ids)
    with (
        patch.object(command.engine, "resolve_engine_target", return_value=command.engine.EngineTarget("fixture-profile", "fixture-model", 1000)),
        patch.object(command.window, "window_judgement", return_value=window.WindowJudgement(True, "fixture window", NOW, NOW + timedelta(hours=1))),
        patch.object(command.stacks.secrets, "select_directory"),
        patch.object(command, "_run_slice", side_effect=run_slice) if not partial_release else patch.object(command, "_run_repeats", side_effect=repeats),
    ):
        code, stdout, stderr = _invoke(["eval", "run", "--challenger", "--stack", "ci", *extra], **kwargs)
    return code, stdout, stderr, contexts


class ChallengerPair(unittest.TestCase):
    def test_retry_commands_parse_in_their_mode_and_ordinary_fix_is_unchanged(self) -> None:
        from gideon.cli import build_parser

        transcripts: list[str] = []
        for argv in (
            ["eval", "run", "--challenger", "--stack", "ci", "--slice", "judge-triples"],
            ["eval", "run", "--challenger", "--stack", "ci", "--kind", "smoke"],
        ):
            host = ChallengerHost()
            _code, stdout, stderr = _invoke(argv, **_run_kwargs(host))
            transcripts.append(stdout + stderr)
        host = ChallengerHost(write_rc=1)
        _code, stdout, stderr, _contexts = _challenger_invoke(host)
        transcripts.append(stdout + stderr)
        commands = [
            match.group(1)
            for transcript in transcripts
            for match in re.finditer(r"(?:Run (?:sudo python3 -m )?gideon )(eval run [^,\n]+), then retry", transcript)
        ]
        self.assertGreaterEqual(len(commands), 3)
        for text in commands:
            with self.subTest(command=text):
                args = build_parser().parse_args(shlex.split(text))
                self.assertIsNone(command._challenger_flag_problem(args))
        code, stdout, stderr = _invoke(["eval", "run"], **_run_kwargs(EvalHost()))
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(stderr, "gideon eval run: no slice was selected Fix: Run gideon eval run --slice extraction.\n")

    def test_flag_refusals_are_early_and_retry_is_parseable(self) -> None:
        from gideon.cli import build_parser

        flags = (("--slice", "judge-triples"), ("--set", "/tmp/fictitious-set"),
                 ("--ranked", "/tmp/fictitious-rank"), ("--decision",),
                 ("--against", RUN_ID), ("--kind", "smoke"))
        for extra in flags:
            with self.subTest(extra=extra):
                host = ChallengerHost()
                code, stdout, stderr = _invoke(
                    ["eval", "run", "--challenger", "--stack", "ci", *extra], **_run_kwargs(host)
                )
                self.assertEqual((code, stdout), (1, ""))
                self.assertIn("Fix:", stderr)
                self.assertFalse(host.calls)
                self.assertFalse(host.exists_calls)
                command_text = stderr.split("Run gideon ", 1)[1].split(", then retry", 1)[0]
                parsed = build_parser().parse_args(command_text.split())
                self.assertIsNone(command._challenger_flag_problem(parsed))
        for stack_flags in ((), ("--stack", "production")):
            host = ChallengerHost()
            args = ["eval", "run", "--challenger", *stack_flags]
            code, stdout, stderr = _invoke(args, **_run_kwargs(host))
            self.assertEqual((code, stdout), (1, ""))
            self.assertIn("--stack ci", stderr)
            self.assertFalse(host.exists_calls)
        host = ChallengerHost(build_box=False)
        code, stdout, stderr = _invoke(
            ["eval", "run", "--challenger", "--stack", "ci", "--force"], **_run_kwargs(host)
        )
        self.assertEqual((code, stderr), (1, ""))
        self.assertIn("build box", stdout)
        self.assertEqual(host.exists_calls, [str(nogpu.BUILD_BOX_PATH)])

    def test_loader_none_and_build_box_stages_stop_before_engine(self) -> None:
        for document, expected_code, expected in (
            ("version: 1\nchallenger: null\n", 0, "skipped — none set"),
            ("version: 1\nchallenger: [bad]\n", 1, "committed challenger refused"),
        ):
            with self.subTest(expected=expected):
                host = ChallengerHost(document=document)
                code, stdout, stderr = _challenger_invoke(host)[:3]
                self.assertEqual(code, expected_code)
                self.assertIn(expected, stdout)
                self.assertFalse(host.calls)
                self.assertFalse(host.lock_records)
                self.assertEqual(host.reads, [str(ROOT / challenger.CHALLENGER_PATH)])
                if expected_code:
                    self.assertIn("Fix:", stderr)
                else:
                    self.assertIn("nothing was evaluated and nothing recorded", stdout)
        host = ChallengerHost(build_box=False)
        code, stdout, _stderr = _challenger_invoke(host)[:3]
        self.assertEqual(code, 1)
        self.assertIn("challenger: refuse", stdout)
        self.assertFalse(host.reads)

    def test_two_passes_record_pair_and_grade_each_prompt(self) -> None:
        host = ChallengerHost()
        code, stdout, stderr, contexts = _challenger_invoke(host)
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertNotIn("visibly-fictitious-case-text-sentinel", stdout + stderr)
        loaded = challenger.load_challenger(ROOT / challenger.CHALLENGER_PATH)
        assert loaded.config is not None and loaded.config.challenger is not None
        entry = loaded.config.challenger
        subject = next(item for item in challenger.SUBJECTS if item.name == entry.subject)
        self.assertEqual(entry.release, SLICE_RUNNERS[subject.slice_name].judge_prompt)
        self.assertEqual([context.judge_prompt_id for context in contexts], [entry.release, entry.challenger])
        self.assertEqual(len(host.judge_requests), 2)
        self.assertEqual(
            [request["messages"][0]["content"] for request in host.judge_requests],
            [judge.PROMPT_REGISTRY[entry.release].system, judge.PROMPT_REGISTRY[entry.challenger].system],
        )
        rows = [payload for argv, payload in host.calls if argv[0] == "docker" and isinstance(payload, str) and payload.startswith("\\set")]
        self.assertEqual(len(rows), 2)
        ids = [read_psql_set(next(line for line in row.splitlines() if line.startswith("\\set run_id ")), "run_id") for row in rows]
        self.assertNotEqual(*ids)
        for index, row in enumerate(rows):
            self.assertIn("\\set run_stack 'ci'", row)
            self.assertIn("\\set kind 'manual'", row)
            value = json.loads(read_psql_set(next(line for line in row.splitlines() if line.startswith("\\set overrides ")), "overrides"))
            expected = {
                challenger.NAME_FIELD: entry.name, challenger.SUBJECT_FIELD: entry.subject,
                challenger.SIDE_FIELD: (challenger.RELEASE_SIDE, challenger.CHALLENGER_SIDE)[index],
                challenger.VALUE_FIELD: (entry.release, entry.challenger)[index],
            }
            if index:
                expected[challenger.PAIRS_FIELD] = ids[0]
            self.assertEqual(value, {challenger.OVERRIDE_KEY: expected})
        lines = stdout.splitlines()
        release_side = next(i for i, line in enumerate(lines) if line.startswith("side: ok") and "release run" in line)
        release_record = next(i for i, line in enumerate(lines) if line.startswith("record: ok"))
        candidate_side = next(i for i, line in enumerate(lines) if line.startswith("side: ok") and "challenger run" in line)
        candidate_record = next(i for i, line in enumerate(lines) if line.startswith("record: ok") and ids[1] in line)
        self.assertLess(release_side, release_record)
        self.assertLess(release_record, candidate_side)
        self.assertLess(candidate_side, candidate_record)
        self.assertIn(ids[0], lines[release_side])
        self.assertIn(ids[1], lines[candidate_side])
        self.assertEqual(sum("side: ok" in line for line in lines), 2)
        self.assertEqual(sum("record: ok" in line for line in lines), 2)
        self.assertEqual((host.acquisitions, host.releases), (1, 1))
        self.assertEqual(len({path for path, _record in host.lock_records}), 1)

    def test_stop_rule_and_exit_codes(self) -> None:
        for outcomes, expected_code in (((True, True), 0), ((False, True), 1), ((True, False), 1), ((False, False), 1)):
            with self.subTest(outcomes=outcomes):
                host = ChallengerHost()
                code, stdout, _stderr, contexts = _challenger_invoke(host, run_results=outcomes)
                self.assertEqual(code, expected_code)
                self.assertEqual(len(contexts), 2)
                self.assertEqual(stdout.count("side: ok"), 2)
        for host, partial in ((ChallengerHost(write_rc=1), False), (ChallengerHost(), True)):
            with self.subTest(partial=partial):
                code, stdout, _stderr, contexts = _challenger_invoke(host, partial_release=partial)
                self.assertEqual(code, 1)
                self.assertEqual(len(contexts), 1)
                self.assertIn("side: refuse", stdout)
                self.assertIn("--challenger --stack ci", stdout)
                self.assertEqual(host.releases, 1)


if __name__ == "__main__":
    unittest.main()
