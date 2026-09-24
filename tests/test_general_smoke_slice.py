"""Managed General smoke runs over the fake frontend and eval command."""

from __future__ import annotations

import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

from test_evaluation_run import NOW, ROOT, EvalHost, _invoke, _run_kwargs
from test_turns import PASSWORD, Frontend, _tagged_case_id

from gideon import guardrail
from gideon.evaluation import command, general_smoke_slice
from gideon.evaluation.evalset import SET_ROOT, LoadedSet, load_set, select_cases
from gideon.evaluation.results import CaseResult, RunContext
from gideon.evaluation.turns import run
from gideon.evaluation.turns.access import TurnAccess
from gideon.host.sysio import Host

SENTINEL = "1234abcd"
_FICTIONAL_ANSWERS: dict[str, str] = {
    "doctrine-01": "A fictional civil procedure note explains a counterclaim.",
    "doctrine-02": "A fictional evidence note explains hearsay.",
    "plain-01": "A warmer fictional rewrite keeps both details.",
    "identity-02": "General is a fictional assistant for broad questions.",
    "citation-02": "General does not verify citations. This example is fictional.",
    "verify-02": "I cannot verify this fictional citation.",
    "matter-01": "General has no access to any case file.",
    "search-01": "A short fictional summary of today's featured article.",
}


def _loaded() -> LoadedSet:
    result = load_set(ROOT / SET_ROOT)
    assert result.loaded is not None, result.findings
    return result.loaded


class SmokeFrontend(Frontend):
    """The turn-harness fake with invented answers tailored to the loaded cases."""

    def __init__(
        self,
        loaded: LoadedSet,
        *,
        preexisting: int = 0,
    ) -> None:
        modes: dict[str, str] = {}
        self.answers: dict[str, str] = dict(_FICTIONAL_ANSWERS)
        self.omit_sources: set[str] = set()
        self.store_reasoning: set[str] = set()
        self.stream_payloads: list[str] = []
        for case_id in select_cases(loaded, "general-smoke").counted:
            expected = cast(dict[str, object], loaded.cases_by_id[case_id]["expected"])
            expectation = cast(str, expected["expect"])
            modes[case_id] = {
                "refused": "replaced",
                "answered": "answered",
                "not-confirmed": "declined",
                "recorded": "answered",
            }[expectation]
        super().__init__(guardrail, modes, preexisting=preexisting)

    def _new_chat(self, body: dict[str, object], mode: str) -> None:
        user_message = cast(dict[str, object], body["user_message"])
        case_id = _tagged_case_id(cast(str, user_message["content"]))
        super()._new_chat(body, mode)
        assistant_id = cast(str, body["id"])
        for chat in self.chats.values():
            record = chat.get("chat")
            if not isinstance(record, dict):
                continue
            history = cast(dict[str, object], record["history"])
            messages = cast(dict[str, object], history["messages"])
            assistant = messages.get(assistant_id)
            if not isinstance(assistant, dict):
                continue
            if case_id in self.answers and mode != "replaced":
                assistant["content"] = self.answers[case_id]
            if case_id in self.omit_sources:
                assistant.pop("sources", None)
            if case_id in self.store_reasoning:
                output = cast(list[dict[str, object]], assistant["output"])
                reasoning = next(item for item in output if item.get("type") == "reasoning")
                reasoning["content"] = [{"type": "output_text", "text": "fictional private reasoning"}]
            return
        raise AssertionError("the fake frontend did not retain the new chat")

    def stream(
        self,
        method: str,
        path: str,
        body: object | None,
        credential: str | None,
    ) -> Any:
        chunks = super().stream(method, path, body, credential)

        def capture() -> Any:
            for chunk in chunks:
                self.stream_payloads.append(cast(str, chunk))
                yield chunk

        return capture()


def _context(
    frontend: SmokeFrontend,
    progress: list[str],
) -> tuple[EvalHost, RunContext]:
    host = EvalHost()
    access = TurnAccess("fictional instruction", PASSWORD, frontend.factory, SENTINEL)
    context = RunContext(
        cast(Host, host),
        Path("/tmp/evaluation-rendered"),
        None,
        None,
        2,
        progress.append,
        turns=access,
    )
    return host, context


class GeneralSmokeRunner(unittest.TestCase):
    """The runner exposes turn facts through results, report, and progress."""

    def test_all_active_cases_pass_twice_and_keep_content_out_of_outputs(self) -> None:
        loaded = _loaded()
        frontend = SmokeFrontend(loaded, preexisting=1)
        progress: list[str] = []
        _host, context = _context(frontend, progress)
        original = run.frontend_turn

        def measured(*args: Any, **kwargs: Any) -> run.TurnRow:
            row = original(*args, **kwargs)
            row_name = cast(str, kwargs["row_name"])
            if row_name == "doctrine-01#1":
                return replace(row, elapsed=0.125)
            if row_name == "doctrine-02#1":
                return replace(row, elapsed=None)
            return row

        with patch.object(run, "frontend_turn", side_effect=measured):
            result = general_smoke_slice.run_general_smoke(loaded, "general-smoke", context)

        active_ids = select_cases(loaded, "general-smoke").counted
        expected_order = tuple(
            (case_id, repeat) for repeat in (1, 2) for case_id in active_ids
        )
        self.assertTrue(result.verdict, result.report)
        self.assertEqual(len(result.results), 22)
        self.assertEqual(
            tuple((row.case_id, row.repeat) for row in result.results), expected_order
        )
        self.assertTrue(all(row.verdict == "pass" for row in result.results))
        rows = {(row.case_id, row.repeat): row for row in result.results}
        self.assertEqual(rows[("doctrine-01", 1)].latency_ms, 125.0)
        self.assertIsNone(rows[("doctrine-02", 1)].latency_ms)
        self.assertEqual(len(progress), len(result.results))
        for row, line in zip(result.results, progress, strict=True):
            self.assertTrue(line.startswith(f"general-smoke {row.case_id}#{row.repeat}:"))
        self.assertIn("signed in as gideon-eval; leftover chats: 1", result.report)
        self.assertIn("eval identity's chats at the end: 1", result.report)
        self.assertEqual(len(frontend.deleted_chats), 22)
        self.assertIn("old/1", frontend.chats)
        self.assertNotIn("old/1", {chat_id for chat_id, _chat in frontend.deleted_chats})
        self.assertEqual(
            len([call for call in frontend.calls if call[1] == "/api/v1/auths/signin"]), 1
        )
        self.assertEqual(len(frontend.stream_calls), 22)
        self.assertNotIn(SENTINEL, result.report + "\n".join(progress))
        for record in loaded.cases_by_file["general/smoke.jsonl"]:
            self.assertNotIn(cast(str, record["question"]), result.report + "\n".join(progress))
        for answer in _FICTIONAL_ANSWERS.values():
            self.assertNotIn(answer, result.report + "\n".join(progress))
        for row in result.results:
            self.assertNotIn(SENTINEL, str(row.metrics))
            for record in loaded.cases_by_file["general/smoke.jsonl"]:
                self.assertNotIn(cast(str, record["question"]), str(row.metrics))
            for answer in _FICTIONAL_ANSWERS.values():
                self.assertNotIn(answer, str(row.metrics))
            self.assertFalse(any("judge" in key.lower() for key in row.metrics))

    def test_failed_checks_fail_the_turn_and_name_the_check_everywhere(self) -> None:
        loaded = _loaded()
        frontend = SmokeFrontend(loaded)
        frontend.answers["doctrine-01"] = "A fictional doctrinal answer omits its required term."
        frontend.answers["doctrine-02"] = "I can't compute that fictional answer for you."
        frontend.modes["doctrine-02"] = "declined"
        frontend.omit_sources.add("search-01")
        frontend.store_reasoning.add("identity-02")
        progress: list[str] = []
        _host, context = _context(frontend, progress)

        result = general_smoke_slice.run_general_smoke(loaded, "general-smoke", context)
        by_id: dict[str, list[CaseResult]] = {}
        for row in result.results:
            by_id.setdefault(row.case_id, []).append(row)
        failed_checks = {
            "doctrine-01": "must",
            "search-01": "sources",
            "identity-02": "withheld",
            "doctrine-02": "expect",
        }
        self.assertFalse(result.verdict)
        for case_id, check in failed_checks.items():
            with self.subTest(case_id=case_id):
                self.assertEqual(len(by_id[case_id]), 2)
                for row in by_id[case_id]:
                    self.assertEqual(row.verdict, "fail")
                    self.assertEqual(row.metrics["problem"], "check-failed")
                    self.assertIn(check, cast(list[str], row.metrics["failed"]))
                    self.assertFalse(cast(Mapping[str, bool], row.metrics["checks"])[check])
                self.assertIn(f"{case_id}: fail", result.report)
                self.assertIn("check-failed:", result.report)
                self.assertIn(check, result.report)
                self.assertTrue(
                    any(
                        line.startswith(f"general-smoke {case_id}#") and check in line
                        for line in progress
                    )
                )

    def test_stream_leak_fails_its_turn(self) -> None:
        loaded = _loaded()
        frontend = SmokeFrontend(loaded)
        frontend.modes["doctrine-01"] = "stream-leak"
        progress: list[str] = []
        _host, context = _context(frontend, progress)

        result = general_smoke_slice.run_general_smoke(loaded, "general-smoke", context)
        rows = [row for row in result.results if row.case_id == "doctrine-01"]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row.verdict, "fail")
            self.assertEqual(row.metrics["problem"], "stream-leak")
            self.assertEqual(row.metrics["stream"], "leak")
            self.assertIn("pattern", row.metrics)
        self.assertIn("doctrine-01: fail", result.report)
        self.assertTrue(any("doctrine-01#" in line and "stream leak" in line for line in progress))

    def test_refused_deletion_fails_and_reports_the_cleanup_fix(self) -> None:
        loaded = _loaded()
        frontend = SmokeFrontend(loaded)
        frontend.refuse_deletion = True
        progress: list[str] = []
        _host, context = _context(frontend, progress)

        result = general_smoke_slice.run_general_smoke(loaded, "general-smoke", context)
        self.assertFalse(result.verdict)
        self.assertTrue(all(row.metrics["problem"] == "cleanup-failed" for row in result.results))
        self.assertIn("cleanup-failed", result.report)
        self.assertIn(run.unverified_fix("gideon-eval"), result.report)

    def test_signin_refusal_fails_every_result_without_making_turns(self) -> None:
        loaded = _loaded()
        frontend = SmokeFrontend(loaded)
        frontend.refuse_signin_after = 0
        progress: list[str] = []
        _host, context = _context(frontend, progress)

        result = general_smoke_slice.run_general_smoke(loaded, "general-smoke", context)
        self.assertEqual(len(result.results), 22)
        self.assertTrue(all(row.metrics["problem"] == "frontend-signin" for row in result.results))
        self.assertEqual(
            [path for _method, path, _body in frontend.calls if path == "/api/chat/completions"],
            [],
        )
        self.assertEqual(frontend.stream_calls, [])
        self.assertIn("frontend signin:", result.report)
        self.assertIn("Fix:", result.report)

    def test_missing_turn_access_is_deterministic_and_makes_no_host_call(self) -> None:
        loaded = _loaded()
        frontend = SmokeFrontend(loaded)
        progress: list[str] = []
        host, context = _context(frontend, progress)
        unavailable = replace(context, turns=None)

        first = general_smoke_slice.run_general_smoke(loaded, "general-smoke", unavailable)
        second = general_smoke_slice.run_general_smoke(loaded, "general-smoke", unavailable)
        self.assertEqual(first, second)
        self.assertEqual(len(first.results), 22)
        self.assertTrue(all(row.metrics["problem"] == "turns-unavailable" for row in first.results))
        self.assertTrue(all("stream" not in row.metrics and "checks" not in row.metrics for row in first.results))
        self.assertIn("turn access unavailable", first.report)
        self.assertEqual(host.calls, [])
        self.assertEqual(frontend.calls, [])


class GeneralSmokeCommand(unittest.TestCase):
    """The public command resolves turn access and records the smoke results."""

    def _run_command(
        self, *, supplied_set: bool
    ) -> tuple[int, str, str, EvalHost, SmokeFrontend, MagicMock]:
        loaded = _loaded()
        frontend = SmokeFrontend(loaded)
        host = EvalHost()
        argv = ["eval", "run", "--slice", "general-smoke"]
        if supplied_set:
            argv.extend(("--set", str(ROOT / SET_ROOT)))
        with (
            patch.object(
                command.engine,
                "resolve_engine_target",
                return_value=command.engine.EngineTarget("fictional-profile", "fictional-model", 1000),
            ),
            patch.object(
                command.window,
                "window_judgement",
                return_value=command.window.WindowJudgement(True, "fixture quiet window", NOW),
            ),
            patch.object(command.access, "load_general_instruction", return_value="fictional instruction"),
            patch.object(command.access, "read_eval_password", return_value=PASSWORD),
            patch.object(command.access, "make_client_factory", return_value=frontend.factory),
            patch.object(command.run, "new_sentinel", return_value=SENTINEL),
            patch.object(
                command.door,
                "probe",
                return_value=command.door.ProbeResult(True, "fixture door", None),
            ),
            patch.object(command, "_compare_reference", wraps=command._compare_reference) as comparison,
        ):
            code, stdout, stderr = _invoke(argv, **_run_kwargs(host))
        return code, stdout, stderr, host, frontend, comparison

    def test_cli_set_run_resolves_turn_access_and_skips_recording(self) -> None:
        code, stdout, stderr, host, frontend, comparison = self._run_command(supplied_set=True)
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("instruction rendered, eval password read, door probed", stdout)
        self.assertIn("22 results over 11 active cases at 2 repeats", stdout)
        self.assertIn("record: ok — skipped", stdout)
        self.assertIn("reference: absent", stdout)
        comparison.assert_called_once()
        self.assertFalse(any(argv[0] == "docker" for argv, _input in host.calls))
        self.assertNotIn(SENTINEL, stdout + stderr)
        self.assertNotIn("fictional instruction", stdout + stderr)
        self.assertEqual(len(frontend.stream_calls), 22)
        self.assertNotIn(SENTINEL, "".join(frontend.stream_payloads))

    def test_cli_release_run_records_kind_stack_and_results_then_compares(self) -> None:
        code, stdout, stderr, host, frontend, comparison = self._run_command(supplied_set=False)
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("22 results over 11 active cases at 2 repeats", stdout)
        self.assertIn("record: ok — run", stdout)
        self.assertIn("reference: absent", stdout)
        comparison.assert_called_once()
        sql = "\n".join(
            cast(str, input_text)
            for argv, input_text in host.calls
            if argv[0] == "docker" and input_text != "SELECT 1;\n"
        )
        self.assertIn("\\set run_stack 'production'", sql)
        self.assertIn("\\set kind 'manual'", sql)
        self.assertEqual(sql.count("INSERT INTO eval_results"), 22)
        self.assertEqual(len(frontend.stream_calls), 22)
        self.assertNotIn(SENTINEL, stdout + stderr + sql)
        self.assertNotIn(SENTINEL, "".join(frontend.stream_payloads))


if __name__ == "__main__":
    unittest.main()
