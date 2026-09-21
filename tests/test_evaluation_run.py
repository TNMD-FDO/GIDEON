"""Evaluation runner and CLI contracts from spec §§18.2 and 18.6."""

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
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import gideon
from gideon.cli import main
from gideon.evaluation import command, judge, reference
from gideon.evaluation.evalset import SET_ROOT, LoadedSet, load_set
from gideon.evaluation.extraction_slice import run_extraction
from gideon.evaluation.results import CaseResult, RunContext, SliceResult
from gideon.evaluation.slices import SLICE_RUNNERS
from gideon.extraction import KEYED_TYPES, SECTION_TYPES, ExactObject, extract
from gideon.extraction.scoring import MIN_RECALL
from gideon.host.sysio import Host

ROOT = Path(__file__).resolve().parents[1]
EVALUATION = ROOT / "gideon" / "evaluation"


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
    ) -> None:
        self.probe_rc = probe_rc
        self.write_rc = write_rc
        self.commit_rc = commit_rc
        self.commit_stdout = commit_stdout
        self.status_rc = status_rc
        self.status_stdout = status_stdout
        self.no_git = no_git
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

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
        raise NotImplementedError

    def geteuid(self) -> int:
        return 0


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
    return RunContext(cast(Host, EvalHost()), "/tmp/evaluation-rendered", None, None, 1, lambda _line: None)


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
        with patch.object(command, "_run_slice", return_value=failing_result):
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
            with patch.object(command, "_run_slice", return_value=failing):
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
            with patch.object(command, "_run_slice", return_value=failing):
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
            with patch.object(command, "_run_slice", return_value=failing):
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

    def test_flags_and_slice_selection_refuse_with_fixes(self) -> None:
        cases = (
            ["eval", "run", "--slice", "extraction", "--decision"],
            ["eval", "run", "--slice", "extraction", "--force"],
            ["eval", "run"],
            ["eval", "run", "--slice", "missing-slice"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                code, stdout, stderr = _invoke(argv, **_run_kwargs(EvalHost()))
                self.assertEqual(code, 1)
                self.assertIn("Fix:", stderr if stderr else stdout)
                self.assertNotIn("reference:", stdout)

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
                    self.assertTrue(
                        top in standard_library or top in {"yaml", "gideon"},
                        f"{path}: non-standard import {name}",
                    )


if __name__ == "__main__":
    unittest.main()
