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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from gideon.cli import main
from gideon.evaluation import command
from gideon.evaluation.evalset import SET_ROOT, LoadedSet, load_set
from gideon.evaluation.extraction_slice import SliceResult, run_extraction
from gideon.extraction import KEYED_TYPES, SECTION_TYPES, ExactObject, extract
from gideon.extraction.scoring import MIN_RECALL

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
        if os.fspath(path) == str(ROOT / ".git"):
            return not self.no_git
        return Path(path).exists()

    def listdir(self, path: str | os.PathLike[str]) -> list[str]:
        raise NotImplementedError

    def unlink(self, path: str | os.PathLike[str], *, missing_ok: bool = False) -> None:
        raise NotImplementedError

    def stat(self, path: str | os.PathLike[str]) -> os.stat_result:
        raise NotImplementedError

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


def _run_kwargs(host: EvalHost) -> dict[str, Any]:
    return {
        "host": host,
        "checkout_root": ROOT,
        "rendered_dir": "/tmp/evaluation-rendered",
        "site_path": ROOT / "config" / "site.example.yaml",
        "clock": lambda: NOW,
        "run_id_factory": lambda: RUN_ID,
    }


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
            "fictitious-digest",
        )

        result = run_extraction(loaded, "extraction")
        by_id = {case.case_id: case for case in result.results}
        self.assertEqual(tuple(case.case_id for case in result.results), ("case-a", "case-b", "case-c"))
        self.assertEqual(by_id["case-a"].verdict, "pass")
        self.assertEqual(by_id["case-a"].metrics["statute"], {"hits": 1, "false_hits": 0, "misses": 0})
        self.assertEqual(by_id["case-b"].verdict, "fail")
        self.assertEqual(by_id["case-b"].metrics["statute"]["false_hits"], 1)
        self.assertEqual(by_id["case-c"].verdict, "pass")
        self.assertEqual(by_id["case-c"].metrics["state_code"], {"hits": 0, "false_hits": 0, "misses": 1})
        self.assertGreaterEqual(by_id["case-a"].latency_ms, 0.0)


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
        clean_score = run_extraction(clean, "extraction").score
        scored_type = next(value for value in clean_score.by_type.values() if value.hits)
        labels = scored_type.hits + scored_type.misses
        added = 0
        while scored_type.hits / (labels + added) >= MIN_RECALL:
            added += 1
        active = set(clean.active_ids)
        target_ids = tuple(case_id for case_id in clean.slices["extraction"] if case_id in active)[:added]
        self.assertEqual(len(target_ids), added)
        landed_keys = {
            value["type"]: value["key"]
            for case in clean.cases_by_id.values()
            if isinstance(case.get("expected"), dict)
            for value in cast(
                list[object], cast(dict[str, object], case["expected"])["objects"]
            )
            if isinstance(value, dict)
            and isinstance(value.get("type"), str)
            and isinstance(value.get("key"), str)
        }

        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "eval-v1"
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
                        "type": scored_type.type,
                        "start": start,
                        "end": start + len(marker) - 1,
                        "text": marker[1:],
                    }
                    if scored_type.type in KEYED_TYPES:
                        object_value["key"] = landed_keys[scored_type.type]
                    if scored_type.type in SECTION_TYPES:
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

    def test_failing_release_run_is_recorded_before_gate(self) -> None:
        host = EvalHost()
        loaded = load_set(ROOT / SET_ROOT).loaded
        assert loaded is not None
        clean = run_extraction(loaded, "extraction")
        failing_score = clean.score.__class__(
            clean.score.by_type,
            clean.score.by_origin,
            clean.score.unlanded_label_counts,
            clean.score.misses,
            clean.score.false_hits,
            False,
        )
        failing_result = SliceResult(
            failing_score,
            clean.report,
            (clean.results[0].__class__(clean.results[0].case_id, "fail", clean.results[0].metrics, clean.results[0].latency_ms),
             *clean.results[1:]),
        )
        with patch.object(command, "_run_slice", return_value=failing_result):
            code, stdout, _ = _invoke(
                ["eval", "run", "--slice", "extraction"], **_run_kwargs(host)
            )
        self.assertEqual(code, 1)
        self.assertIn("record: ok — run", stdout)
        self.assertIn("gate: refuse", stdout)
        write_sql = next(input for argv, input in host.calls if argv[0] == "docker" and input != "SELECT 1;\n")
        self.assertIn("\\set run_verdict 'fail'", write_sql or "")
        self.assertIn("\\set result_0_verdict 'fail'", write_sql or "")

    def test_set_is_never_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "eval-v1"
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
        self.assertEqual([argv[0] for argv, _ in host.calls], ["docker"])

    def test_failed_write_refuses_and_gate_is_still_last(self) -> None:
        host = EvalHost(write_rc=1)
        code, stdout, _ = _invoke(
            ["eval", "run", "--slice", "extraction"], **_run_kwargs(host)
        )
        self.assertEqual(code, 1)
        self.assertIn("record: refuse", stdout)
        self.assertIn("gate: ok", stdout)
        self.assertLess(stdout.index("record:"), stdout.index("gate:"))

    def test_git_provenance_is_bound_and_dirty_state_is_recorded(self) -> None:
        host = EvalHost(status_stdout=" M changed.py\n")
        code, stdout, _ = _invoke(
            ["eval", "run", "--slice", "extraction"], **_run_kwargs(host)
        )
        self.assertEqual(code, 0)
        self.assertIn("record: ok", stdout)
        git_calls = [argv for argv, _ in host.calls if argv[0] == "git"]
        self.assertEqual(len(git_calls), 2)
        for argv in git_calls:
            self.assertEqual(argv[1:3], ("-c", f"safe.directory={ROOT}"))
            self.assertEqual(argv[3:5], ("-C", str(ROOT)))
        write_sql = next(input for argv, input in host.calls if argv[0] == "docker" and input != "SELECT 1;\n")
        self.assertIn("\\set git_dirty 'true'", write_sql or "")

    def test_no_git_entry_records_both_provenance_values_as_null_without_git(self) -> None:
        host = EvalHost(no_git=True)
        code, _stdout, _ = _invoke(
            ["eval", "run", "--slice", "extraction"], **_run_kwargs(host)
        )
        self.assertEqual(code, 0)
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
