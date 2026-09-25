"""Judge-triples runner and command contracts."""

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_evaluation_record import read_psql_set
from test_judge import engine_output, valid_content

from gideon.evaluation import command, window
from gideon.evaluation.evalset import SET_ROOT, LoadedSet, load_set
from gideon.evaluation.results import CaseResult, RunContext, SliceResult
from gideon.evaluation.slices import SLICE_RUNNERS
from gideon.host import courts, site
from gideon.host.report import Problem
from gideon.host.sysio import Command, Host, PathLike

ROOT = Path(__file__).resolve().parents[1]
SITE_PATH = ROOT / "config" / "site.example.yaml"
RENDERED = "/tmp/judge-slice-rendered"
RUN_ID = "11111111-2222-4333-8444-555555555555"


def _site_timezone() -> ZoneInfo:
    result = site.load_site(SITE_PATH)
    assert result.config is not None
    return ZoneInfo(result.config.office.timezone)


SITE_TIMEZONE = _site_timezone()


def quiet_clock() -> datetime:
    return datetime(2026, 9, 18, 20, 0, tzinfo=SITE_TIMEZONE)


def office_clock() -> datetime:
    return datetime(2026, 9, 18, 12, 0, tzinfo=SITE_TIMEZONE)


def run_id() -> str:
    return RUN_ID


def no_sleep(_seconds: float) -> None:
    return None



class JudgeHost:
    """Fake Host recording provenance, writer, and engine-seam calls."""

    def __init__(
        self,
        output: str,
        *,
        probe_rc: int = 0,
        write_rc: int = 0,
        commit_rc: int = 0,
        status_rc: int = 0,
        geteuid: int = 0,
        no_gpu: bool = False,
        git_available: bool = True,
        timeout: bool = False,
    ) -> None:
        self.output = output
        self.probe_rc = probe_rc
        self.write_rc = write_rc
        self.commit_rc = commit_rc
        self.status_rc = status_rc
        self._geteuid = geteuid
        self.no_gpu = no_gpu
        self.git_available = git_available
        self.timeout = timeout
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.engine_calls: list[str] = []
        self.write_sql: list[str] = []
        self.git_calls: list[tuple[str, ...]] = []
        self.locks: dict[str, str] = {}

    def run(
        self,
        argv: Command,
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, cwd, env, passthrough
        command_argv = tuple(argv)
        self.calls.append((command_argv, input))
        if command_argv[0] == "git":
            self.git_calls.append(command_argv)
            if command_argv[-2:] == ("rev-parse", "HEAD"):
                return subprocess.CompletedProcess(
                    list(command_argv), self.commit_rc, "a" * 40 + "\n", "git diagnostic"
                )
            return subprocess.CompletedProcess(
                list(command_argv), self.status_rc, "", "git diagnostic"
            )
        if command_argv[0] != "docker" or input is None:
            raise AssertionError(f"unexpected command: {command_argv}")
        if input == "SELECT 1;\n":
            return subprocess.CompletedProcess(
                list(command_argv), self.probe_rc, "", "database diagnostic"
            )
        if input.startswith("\\set"):
            self.write_sql.append(input)
            return subprocess.CompletedProcess(
                list(command_argv), self.write_rc, "", "database diagnostic"
            )
        self.engine_calls.append(input)
        if self.timeout:
            raise subprocess.TimeoutExpired(command_argv, timeout or 0.0)
        return subprocess.CompletedProcess(
            list(command_argv), 0, self.output, "engine diagnostic"
        )

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        return Path(path).read_text(encoding=encoding)

    def exists(self, path: PathLike) -> bool:
        path_text = os.fspath(path)
        if path_text == "/etc/gideon/no-gpu":
            return self.no_gpu
        if path_text == str(ROOT / ".git"):
            return self.git_available
        return Path(path).exists()

    def geteuid(self) -> int:
        return self._geteuid

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del path, mode, parents, exist_ok

    def take_lock(self, path: PathLike, record: str) -> str | None:
        key = os.fspath(path)
        holder = self.locks.get(key)
        if holder is None:
            self.locks[key] = record
        return holder

    def release_lock(self, path: PathLike) -> None:
        self.locks.pop(os.fspath(path), None)


def _loaded() -> LoadedSet:
    court_result = courts.load_court_map(ROOT / "courts.yaml")
    assert court_result.court_map is not None
    result = load_set(ROOT / SET_ROOT, court_result.court_map.courts)
    assert result.loaded is not None, result.findings
    return result.loaded


def _runner_context(host: JudgeHost, progress: list[str]) -> RunContext:
    spec = SLICE_RUNNERS["judge-triples"]
    return RunContext(
        cast(Host, host),
        RENDERED,
        "fixture-model",
        spec.judge_prompt,
        spec.repeats,
        progress.append,
    )


def _run_runner(
    output: str,
) -> tuple[LoadedSet, SliceResult, list[str]]:
    loaded = _loaded()
    progress: list[str] = []
    host = JudgeHost(output)
    result = SLICE_RUNNERS["judge-triples"].runner(
        loaded, "judge-triples", _runner_context(host, progress)
    )
    return loaded, result, progress


def _invoke_command(
    host: JudgeHost,
    *,
    clock: object = quiet_clock,
    site_path: PathLike = SITE_PATH,
    set_path: PathLike | None = None,
    target: object | None = None,
) -> tuple[int, str, str]:
    args = argparse.Namespace(
        slice="judge-triples",
        decision=False,
        force=False,
        set=set_path,
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    target_value: object = command.engine.EngineTarget("fixture-profile", "fixture-model", 2048)
    if target is not None:
        target_value = target
    with (
        patch.object(command.engine, "resolve_engine_target", return_value=target_value),
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        code = command.run_eval(
            args,
            host=cast(Host, host),
            checkout_root=ROOT,
            rendered_dir=RENDERED,
            site_path=site_path,
            clock=cast(Any, clock),
            run_id_factory=run_id,
            sleep=no_sleep,
        )
    return code, stdout.getvalue(), stderr.getvalue()


def _supplied_set() -> tempfile.TemporaryDirectory[str]:
    directory = tempfile.TemporaryDirectory()
    shutil.copytree(ROOT / SET_ROOT, Path(directory.name) / SET_ROOT.name)
    return directory


class Runner(unittest.TestCase):
    """The runner's order, report, and never-gates result are observable."""

    def test_progress_is_repeat_major_and_id_ordered(self) -> None:
        loaded, result, progress = _run_runner(engine_output(valid_content()))
        active = set(loaded.active_ids)
        ids = tuple(
            sorted(
                case_id
                for case_id in loaded.slices["judge-triples"]
                if case_id in active
            )
        )
        expected = tuple(
            f"judge {case_id} repeat {repeat}"
            for repeat in range(1, SLICE_RUNNERS["judge-triples"].repeats + 1)
            for case_id in ids
        )
        self.assertEqual(tuple(line.split(":", 1)[0] for line in progress), expected)
        self.assertEqual(len(result.results), len(ids) * SLICE_RUNNERS["judge-triples"].repeats)

    def test_report_has_derived_band_rows_and_summary(self) -> None:
        loaded, result, _progress = _run_runner(engine_output(valid_content()))
        active = set(loaded.active_ids)
        ids = tuple(
            sorted(
                case_id
                for case_id in loaded.slices["judge-triples"]
                if case_id in active
            )
        )
        by_case: dict[str, list[CaseResult]] = {case_id: [] for case_id in ids}
        for case_result in result.results:
            by_case[case_result.case_id].append(case_result)
        in_band = sum(
            all(
                isinstance(case_result.judge, Mapping)
                and case_result.judge.get("in_band") is True
                for case_result in case_results
            )
            for case_results in by_case.values()
        )
        equal = sum(
            len({case_result.judge["score"] for case_result in case_results if case_result.judge})
            == 1
            for case_results in by_case.values()
        )
        failed = sum(case_result.verdict != "pass" for case_result in result.results)
        for case_id in ids:
            case = loaded.cases_by_id[case_id]
            expected = cast(Mapping[str, object], case["expected"])
            band = cast(list[int], expected["band"])
            case_results = by_case[case_id]
            row = next(line for line in result.report.splitlines() if line.startswith(case_id + " |"))
            columns = [column.strip() for column in row.split("|")]
            self.assertEqual(columns[1], f"{band[0]}-{band[1]}")
            for index, case_result in enumerate(case_results):
                assert case_result.judge is not None
                self.assertEqual(columns[2 + index * 2], str(case_result.judge["score"]))
                self.assertEqual(columns[3 + index * 2], str(case_result.judge["in_band"]))
            self.assertEqual(columns[-1], "True")
        summary = f"in band {in_band} of {len(ids)}, equal {equal} of {len(ids)}, failed gradings {failed}"
        self.assertIn(summary, result.report)

    def test_out_of_band_score_keeps_gate_green(self) -> None:
        with _supplied_set() as directory:
            host = JudgeHost(engine_output(valid_content(score=0)))
            code, stdout, _stderr = _invoke_command(host, set_path=Path(directory) / SET_ROOT.name)
        self.assertEqual(code, 0)
        self.assertIn("gate: ok — all judge gradings returned on-schema verdicts", stdout)

    def test_a_slice_that_keeps_no_reference_prints_no_comparison(self) -> None:
        """The ruling at the rebase onto v0.2.17: the gate row is the spec's text alone."""

        host = JudgeHost(engine_output(valid_content()))
        code, stdout, _stderr = _invoke_command(host)
        self.assertEqual(code, 0)
        self.assertEqual(
            [line for line in stdout.splitlines() if line.startswith("reference")], []
        )
        self.assertIn(
            "gate: ok — all judge gradings returned on-schema verdicts\n", stdout
        )

    def test_failed_grading_is_scoreless_and_gate_red(self) -> None:
        host = JudgeHost(engine_output("not JSON"))
        code, stdout, _stderr = _invoke_command(host)
        self.assertEqual(code, 1)
        self.assertIn("gate: refuse", stdout)
        self.assertTrue(host.write_sql)
        sql = host.write_sql[0]
        judge_line = next(line for line in sql.splitlines() if line.startswith("\\set result_0_judge "))
        field = json.loads(read_psql_set(judge_line, "result_0_judge"))
        self.assertNotIn("score", field)


class Preconditions(unittest.TestCase):
    """Every precondition refuses before an engine request and names its fix."""

    def test_each_refusal_stops_before_engine_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            invalid_site = Path(directory) / "invalid.yaml"
            invalid_site.write_text("office: [not a mapping]\n", encoding="utf-8")
            cases = (
                (
                    "root",
                    JudgeHost(engine_output(valid_content()), geteuid=1000),
                    quiet_clock,
                    SITE_PATH,
                    None,
                    None,
                    "root privileges are required",
                ),
                (
                    "no-gpu",
                    JudgeHost(engine_output(valid_content()), no_gpu=True),
                    quiet_clock,
                    SITE_PATH,
                    None,
                    None,
                    "no-GPU marker",
                ),
                (
                    "site",
                    JudgeHost(engine_output(valid_content())),
                    quiet_clock,
                    invalid_site,
                    None,
                    None,
                    "Invalid value for 'office'",
                ),
                (
                    "window",
                    JudgeHost(engine_output(valid_content())),
                    office_clock,
                    SITE_PATH,
                    None,
                    None,
                    "outside the quiet window",
                ),
                (
                    "target",
                    JudgeHost(engine_output(valid_content())),
                    quiet_clock,
                    SITE_PATH,
                    None,
                    Problem("target unavailable", "Fix the target"),
                    "target unavailable",
                ),
                (
                    "git",
                    JudgeHost(engine_output(valid_content()), commit_rc=1),
                    quiet_clock,
                    SITE_PATH,
                    None,
                    None,
                    "git provenance could not be read",
                ),
                (
                    "writer",
                    JudgeHost(engine_output(valid_content()), probe_rc=1),
                    quiet_clock,
                    SITE_PATH,
                    None,
                    None,
                    "eval writer failed",
                ),
            )
            for name, host, clock, site_path, set_path, target, phrase in cases:
                with self.subTest(name=name):
                    code, stdout, _stderr = _invoke_command(
                        host,
                        clock=clock,
                        site_path=site_path,
                        set_path=set_path,
                        target=target,
                    )
                    self.assertEqual(code, 1)
                    self.assertIn("preconditions: refuse", stdout)
                    self.assertIn("Fix:", stdout)
                    # The stage stops at its FIRST refusal, so each case must
                    # show its own refusal and not an earlier one standing in.
                    self.assertIn(phrase, stdout)
                    self.assertEqual(host.engine_calls, [])
                    if name == "window":
                        opening = window.window_judgement(
                            office_clock(), SITE_TIMEZONE.key
                        ).next_opening.isoformat()
                        self.assertIn(opening, stdout)

    def test_writer_probe_precedes_first_engine_request(self) -> None:
        host = JudgeHost(engine_output(valid_content()))
        code, _stdout, _stderr = _invoke_command(host)
        self.assertEqual(code, 0)
        probe_index = next(index for index, (_argv, input) in enumerate(host.calls) if input == "SELECT 1;\n")
        engine_index = next(index for index, (_argv, input) in enumerate(host.calls) if input and input.startswith("{"))
        self.assertLess(probe_index, engine_index)

    def test_supplied_set_skips_probe_provenance_and_record(self) -> None:
        with _supplied_set() as directory:
            host = JudgeHost(engine_output(valid_content()))
            code, stdout, _stderr = _invoke_command(
                host, set_path=Path(directory) / SET_ROOT.name
            )
        self.assertEqual(code, 0)
        self.assertTrue(host.engine_calls)
        self.assertFalse(any(input == "SELECT 1;\n" for _argv, input in host.calls))
        self.assertEqual(host.git_calls, [])
        self.assertEqual(host.write_sql, [])
        self.assertIn("record: ok — skipped — a set outside the release is never recorded", stdout)

    def test_record_repeats_and_judge_fields_are_bound(self) -> None:
        host = JudgeHost(engine_output(valid_content()))
        code, _stdout, _stderr = _invoke_command(host)
        self.assertEqual(code, 0)
        sql = host.write_sql[0]
        self.assertEqual(read_psql_set(next(line for line in sql.splitlines() if line.startswith("\\set repeats ")), "repeats"), "2")
        judge_lines = [line for line in sql.splitlines() if "_judge '" in line]
        loaded = _loaded()
        active = set(loaded.active_ids)
        expected_rows = len(
            [case_id for case_id in loaded.slices["judge-triples"] if case_id in active]
        ) * SLICE_RUNNERS["judge-triples"].repeats
        self.assertEqual(len(judge_lines), expected_rows)
        for line in judge_lines:
            field = json.loads(read_psql_set(line, line.split()[1]))
            self.assertIn("prompt", field)
            self.assertIn("score", field)

    def test_reason_text_never_reaches_command_stdout(self) -> None:
        reason = "Distinctive fixture sentence never printed to command stdout."
        with _supplied_set() as directory:
            host = JudgeHost(engine_output(valid_content(reason=reason)))
            code, stdout, _stderr = _invoke_command(
                host, set_path=Path(directory) / SET_ROOT.name
            )
        self.assertEqual(code, 0)
        self.assertNotIn(reason, stdout)

    def test_timeout_reports_unknown_figures_and_no_latency(self) -> None:
        host = JudgeHost(engine_output(valid_content()), timeout=True)
        code, stdout, _stderr = _invoke_command(host)
        self.assertEqual(code, 1)
        self.assertIn("prompt_tokens unknown", stdout)
        self.assertIn("completion_tokens unknown", stdout)
        self.assertIn("reason_length unknown", stdout)
        self.assertIn("seconds unknown", stdout)
        self.assertTrue(host.write_sql)
        self.assertNotIn("result_0_latency_ms", host.write_sql[0])


if __name__ == "__main__":
    unittest.main()
