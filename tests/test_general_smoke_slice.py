"""Managed General smoke runs over the fake frontend and eval command."""

from __future__ import annotations

import json
import re
import unittest
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

from test_evaluation_run import (
    NOW,
    ROOT,
    EvalHost,
    _assert_comparison_lines,
    _invoke,
    _run_kwargs,
)
from test_turns import PASSWORD, Frontend, _tagged_case_id

from gideon import guardrail
from gideon.evaluation import command, general_smoke_slice, window
from gideon.evaluation.evalset import SET_ROOT, LoadedSet, load_set, select_cases
from gideon.evaluation.results import CaseResult, RunContext
from gideon.evaluation.turns import run
from gideon.evaluation.turns.access import TurnAccess
from gideon.host.engine import SAMPLE_ATTEMPTS
from gideon.host.owui import Response
from gideon.host.render.owui import EVAL_IDENTITY
from gideon.host.sysio import Host

SENTINEL = "1234abcd"
_FICTIONAL_ANSWERS: dict[str, str] = {
    "doctrine-03": "A fictional civil procedure note explains a counterclaim.",
    "doctrine-04": "A fictional evidence note explains hearsay.",
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
        self.attempt_modes: dict[str, list[str]] = {}
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

    def handle(
        self,
        method: str,
        path: str,
        body: object | None,
        credential: str | None,
    ) -> Response:
        if method == "POST" and path == "/api/chat/completions" and isinstance(body, dict):
            user_message = body.get("user_message")
            if isinstance(user_message, dict):
                case_id = _tagged_case_id(cast(str, user_message["content"]))
                modes = self.attempt_modes.get(case_id)
                if modes:
                    self.modes[case_id] = modes.pop(0)
        return super().handle(method, path, body, credential)

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
            if case_id in self.answers and mode == "answered":
                assistant["content"] = self.answers[case_id]
            if case_id in self.omit_sources:
                assistant.pop("sources", None)
            if case_id in self.store_reasoning:
                output = cast(list[dict[str, object]], assistant["output"])
                output.insert(
                    0,
                    {
                        "type": "reasoning",
                        "content": [{"type": "output_text", "text": "fictional private reasoning"}],
                    },
                )
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
    access = TurnAccess(PASSWORD, frontend.factory, SENTINEL)
    context = RunContext(
        cast(Host, host),
        Path("/tmp/evaluation-rendered"),
        Path("/tmp/evaluation-rendered"),
        None,
        None,
        2,
        progress.append,
        turns=access,
    )
    return host, context


def _recorded_metrics(sql: str) -> list[dict[str, object]]:
    return [
        json.loads(value.replace("''", "'").replace("\\\\", "\\"))
        for value in re.findall(r"(?m)^\\set result_\d+_metrics '(.*)'$", sql)
    ]


class GeneralSmokeRunner(unittest.TestCase):
    """The runner exposes turn facts through results, report, and progress."""

    def test_problem_vocabulary_matches_the_current_runner(self) -> None:
        self.assertEqual(
            general_smoke_slice.PROBLEMS,
            frozenset(
                {
                    "turns-unavailable",
                    "frontend-signin",
                    "turn-error",
                    "turn-cut",
                    "unverified",
                    "cleanup-failed",
                    "stream-error",
                    "stream-leak",
                    "check-failed",
                }
            ),
        )

    def test_all_active_cases_pass_twice_and_keep_content_out_of_outputs(self) -> None:
        loaded = _loaded()
        frontend = SmokeFrontend(loaded, preexisting=1)
        progress: list[str] = []
        _host, context = _context(frontend, progress)
        original = run.frontend_turn

        def measured(*args: Any, **kwargs: Any) -> run.TurnRow:
            row = original(*args, **kwargs)
            row_name = cast(str, kwargs["row_name"])
            if row_name == "doctrine-03#1":
                return replace(row, elapsed=0.125)
            if row_name == "doctrine-04#1":
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
        self.assertEqual(rows[("doctrine-03", 1)].latency_ms, 125.0)
        self.assertIsNone(rows[("doctrine-04", 1)].latency_ms)
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
            self.assertEqual(row.metrics["attempts"], 1)
            self.assertNotIn("first_class", row.metrics)
            self.assertNotIn(SENTINEL, str(row.metrics))
            for record in loaded.cases_by_file["general/smoke.jsonl"]:
                self.assertNotIn(cast(str, record["question"]), str(row.metrics))
            for answer in _FICTIONAL_ANSWERS.values():
                self.assertNotIn(answer, str(row.metrics))
            self.assertFalse(any("judge" in key.lower() for key in row.metrics))

    def test_decline_then_answer_keeps_one_passing_row_and_reports_both_attempts(self) -> None:
        loaded = _loaded()
        frontend = SmokeFrontend(loaded)
        frontend.attempt_modes["doctrine-03"] = ["declined", "answered", "answered"]
        progress: list[str] = []
        _host, context = _context(frontend, progress)

        result = general_smoke_slice.run_general_smoke(loaded, "general-smoke", context)

        turns = len(select_cases(loaded, "general-smoke").counted) * context.repeats
        self.assertTrue(result.verdict, result.report)
        self.assertEqual(len(result.results), turns)
        rows = {(row.case_id, row.repeat): row for row in result.results}
        row = rows[("doctrine-03", 1)]
        self.assertEqual(row.verdict, "pass")
        self.assertEqual(row.metrics["class"], "answered")
        self.assertEqual(row.metrics["attempts"], 2)
        self.assertEqual(row.metrics["first_class"], "declined")
        self.assertNotIn("problem", row.metrics)
        self.assertEqual(rows[("doctrine-03", 2)].metrics["attempts"], 1)
        self.assertNotIn("first_class", rows[("doctrine-03", 2)].metrics)
        self.assertEqual(frontend.attempt_modes["doctrine-03"], [])
        completions = [
            call for call in frontend.calls
            if call[0] == "POST" and call[1] == "/api/chat/completions"
        ]
        self.assertEqual(len(completions), turns + 1)
        self.assertEqual(len(frontend.stream_calls), turns + 1)
        self.assertEqual(len(frontend.deleted_chats), turns + 1)
        self.assertEqual(frontend.chats, {})
        self.assertEqual(len(progress), turns + 1)
        attempt_lines = [line for line in progress if line.startswith("general-smoke doctrine-03#1:")]
        self.assertEqual(len(attempt_lines), 2)
        self.assertIn(": declined;", attempt_lines[0])
        self.assertTrue(attempt_lines[0].endswith("; re-sent"))
        self.assertNotIn("; attempt", attempt_lines[0])
        self.assertIn(": answered; checks ok; stream clean;", attempt_lines[1])
        self.assertTrue(attempt_lines[1].endswith(f"; attempt 2 of {SAMPLE_ATTEMPTS}"))
        self.assertEqual(progress.index(attempt_lines[1]), progress.index(attempt_lines[0]) + 1)
        self.assertIn(
            f"doctrine-03: pass — repeat 1 answered (attempt 2 of {SAMPLE_ATTEMPTS}, "
            "first declined); repeat 2 answered",
            result.report,
        )
        self.assertIn(f"chats: {turns + 1} turns made, {turns + 1} chats deleted", result.report)
        self.assertIn(f"{turns + 1} turns, 1 re-sent;", result.report)
        output = result.report + "\n".join(progress) + str(result.results)
        self.assertNotIn(SENTINEL, output)
        self.assertNotIn("I can't compute that for you.", output)
        for record in loaded.cases_by_file["general/smoke.jsonl"]:
            self.assertNotIn(cast(str, record["question"]), output)
        for answer in frontend.answers.values():
            self.assertNotIn(answer, output)

    def test_decline_on_every_attempt_fails_that_repeat_at_the_bound(self) -> None:
        loaded = _loaded()
        frontend = SmokeFrontend(loaded)
        frontend.attempt_modes["plain-01"] = ["declined"] * SAMPLE_ATTEMPTS + ["answered"]
        progress: list[str] = []
        _host, context = _context(frontend, progress)

        result = general_smoke_slice.run_general_smoke(loaded, "general-smoke", context)

        self.assertFalse(result.verdict)
        rows = [row for row in result.results if row.case_id == "plain-01"]
        self.assertEqual(len(rows), context.repeats)
        self.assertEqual(rows[0].verdict, "fail")
        self.assertEqual(rows[0].metrics["class"], "declined")
        self.assertEqual(rows[0].metrics["problem"], "check-failed")
        self.assertEqual(rows[0].metrics["failed"], ["expect"])
        self.assertEqual(rows[0].metrics["attempts"], SAMPLE_ATTEMPTS)
        self.assertEqual(rows[0].metrics["first_class"], "declined")
        self.assertEqual(rows[1].verdict, "pass")
        self.assertEqual(rows[1].metrics["attempts"], 1)
        self.assertNotIn("first_class", rows[1].metrics)
        attempts = [line for line in progress if line.startswith("general-smoke plain-01#1:")]
        self.assertEqual(len(attempts), SAMPLE_ATTEMPTS)
        self.assertTrue(all(line.endswith("; re-sent") for line in attempts[:-1]))
        self.assertTrue(attempts[-1].endswith(f"; attempt {SAMPLE_ATTEMPTS} of {SAMPLE_ATTEMPTS}"))
        self.assertNotIn("re-sent", attempts[-1])
        self.assertIn(
            f"plain-01: fail — repeat 1 declined, check-failed: expect "
            f"(attempt {SAMPLE_ATTEMPTS} of {SAMPLE_ATTEMPTS}, first declined); repeat 2 answered",
            result.report,
        )
        turns = len(select_cases(loaded, "general-smoke").counted) * context.repeats
        completions = [call for call in frontend.calls if call[1] == "/api/chat/completions"]
        self.assertEqual(len(completions), turns + SAMPLE_ATTEMPTS - 1)
        self.assertEqual(len(frontend.deleted_chats), turns + SAMPLE_ATTEMPTS - 1)
        self.assertIn(f"{turns + SAMPLE_ATTEMPTS - 1} turns, 1 re-sent;", result.report)

    def test_breaks_and_other_expectations_are_never_resent(self) -> None:
        actual = run.frontend_turn
        for mode, case_id, problem, refuse_deletion, cut in (
            ("replaced", "plain-01", "check-failed", False, False),
            ("stream-leak", "plain-01", "stream-leak", False, False),
            ("declined", "plain-01", "cleanup-failed", True, False),
            ("declined", "plain-01", "turn-cut", False, True),
            ("answered", "compute-01", "check-failed", False, False),
        ):
            with self.subTest(mode=mode, problem=problem):
                loaded = _loaded()
                frontend = SmokeFrontend(loaded)
                frontend.attempt_modes[case_id] = [mode, "answered"]
                frontend.refuse_deletion = refuse_deletion
                progress: list[str] = []
                _host, context = _context(frontend, progress)
                context = replace(context, repeats=1)

                def cut_row(
                    *args: Any, cut: bool = cut, case_id: str = case_id, **kwargs: Any
                ) -> run.TurnRow:
                    row = actual(*args, **kwargs)
                    if cut and kwargs["case"].id == case_id:
                        return replace(row, cut=True)
                    return row

                with patch.object(run, "frontend_turn", side_effect=cut_row):
                    result = general_smoke_slice.run_general_smoke(loaded, "general-smoke", context)

                self.assertFalse(result.verdict)
                row = next(row for row in result.results if row.case_id == case_id)
                self.assertEqual(row.verdict, "fail")
                self.assertEqual(row.metrics["problem"], problem)
                if mode == "declined":
                    self.assertEqual(row.metrics["class"], "declined")
                self.assertEqual(row.metrics["attempts"], 1)
                self.assertNotIn("first_class", row.metrics)
                self.assertEqual(frontend.attempt_modes[case_id], ["answered"])
                completions = [call for call in frontend.calls if call[1] == "/api/chat/completions"]
                self.assertEqual(len(completions), len(select_cases(loaded, "general-smoke").counted))
                self.assertEqual(
                    len([line for line in progress if line.startswith(f"general-smoke {case_id}#1:")]),
                    1,
                )
                self.assertEqual(len(progress), len(result.results))
                self.assertFalse(any("re-sent" in line for line in progress))

    def test_declines_pass_other_expectations_without_resending(self) -> None:
        for expectation in ("recorded", "not-confirmed"):
            with self.subTest(expectation=expectation):
                loaded = _loaded()
                record = loaded.cases_by_id["plain-01"]
                expected = cast(dict[str, object], record["expected"])
                loaded = replace(loaded, cases_by_id={
                    **loaded.cases_by_id,
                    "plain-01": {**record, "expected": {**expected, "expect": expectation}},
                })
                frontend = SmokeFrontend(loaded)
                frontend.attempt_modes["plain-01"] = ["declined", "answered"]
                progress: list[str] = []
                _host, context = _context(frontend, progress)
                context = replace(context, repeats=1)

                result = general_smoke_slice.run_general_smoke(loaded, "general-smoke", context)

                self.assertTrue(result.verdict, result.report)
                row = next(row for row in result.results if row.case_id == "plain-01")
                self.assertEqual(row.verdict, "pass")
                self.assertEqual(row.metrics["expect"], expectation)
                self.assertEqual(row.metrics["class"], "declined")
                self.assertEqual(row.metrics["attempts"], 1)
                self.assertNotIn("first_class", row.metrics)
                self.assertEqual(frontend.attempt_modes["plain-01"], ["answered"])
                self.assertEqual(len(frontend.stream_calls), len(result.results))
                self.assertEqual(len(frontend.deleted_chats), len(result.results))
                self.assertFalse(any("re-sent" in line for line in progress))

    def test_turn_with_both_pattern_ids_reports_the_stored_one(self) -> None:
        loaded = _loaded()
        case_id = select_cases(loaded, "general-smoke").counted[0]
        frontend = SmokeFrontend(loaded)
        _host, context = _context(frontend, [])
        original = run.frontend_turn

        def both_ids(*args: Any, **kwargs: Any) -> run.TurnRow:
            row = original(*args, **kwargs)
            if kwargs["row_name"] == f"{case_id}#1":
                return replace(
                    row,
                    pattern_id="fictional-stored-pattern",
                    stream_pattern_id="fictional-stream-pattern",
                )
            return row

        with patch.object(run, "frontend_turn", side_effect=both_ids):
            result = general_smoke_slice.run_general_smoke(loaded, "general-smoke", context)

        row = next(
            row for row in result.results if (row.case_id, row.repeat) == (case_id, 1)
        )
        self.assertEqual(row.metrics["pattern"], "fictional-stored-pattern")

    def test_failed_checks_fail_the_turn_and_name_the_check_everywhere(self) -> None:
        loaded = _loaded()
        frontend = SmokeFrontend(loaded)
        frontend.answers["doctrine-03"] = "A fictional doctrinal answer omits its required term."
        frontend.modes["doctrine-04"] = "declined"
        frontend.omit_sources.add("search-01")
        frontend.store_reasoning.add("identity-02")
        progress: list[str] = []
        _host, context = _context(frontend, progress)

        result = general_smoke_slice.run_general_smoke(loaded, "general-smoke", context)
        by_id: dict[str, list[CaseResult]] = {}
        for row in result.results:
            by_id.setdefault(row.case_id, []).append(row)
        failed_checks = {
            "doctrine-03": "must",
            "search-01": "sources",
            "identity-02": "withheld",
            "doctrine-04": "expect",
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
                    self.assertEqual(row.metrics["attempts"], SAMPLE_ATTEMPTS if case_id == "doctrine-04" else 1)
                    if case_id == "doctrine-04":
                        self.assertEqual(row.metrics["first_class"], "declined")
                    else:
                        self.assertNotIn("first_class", row.metrics)
                self.assertIn(f"{case_id}: fail", result.report)
                self.assertIn("check-failed:", result.report)
                self.assertIn(check, result.report)
                self.assertTrue(
                    any(
                        line.startswith(f"general-smoke {case_id}#") and check in line
                        for line in progress
                    )
                )
        turns = len(select_cases(loaded, "general-smoke").counted) * context.repeats
        completions = [call for call in frontend.calls if call[1] == "/api/chat/completions"]
        self.assertEqual(len(completions), turns + (SAMPLE_ATTEMPTS - 1) * context.repeats)
        self.assertEqual(len(frontend.deleted_chats), len(completions))
        for repeat in range(1, context.repeats + 1):
            self.assertEqual(
                len([line for line in progress if line.startswith(f"general-smoke doctrine-04#{repeat}:")]),
                SAMPLE_ATTEMPTS,
            )
            self.assertIn(f"repeat {repeat} declined, check-failed: expect", result.report)

    def test_stream_leak_fails_its_turn(self) -> None:
        loaded = _loaded()
        frontend = SmokeFrontend(loaded)
        frontend.modes["doctrine-03"] = "stream-leak"
        progress: list[str] = []
        _host, context = _context(frontend, progress)

        result = general_smoke_slice.run_general_smoke(loaded, "general-smoke", context)
        rows = [row for row in result.results if row.case_id == "doctrine-03"]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row.verdict, "fail")
            self.assertEqual(row.metrics["problem"], "stream-leak")
            self.assertEqual(row.metrics["stream"], "leak")
            self.assertIn("pattern", row.metrics)
            self.assertEqual(row.metrics["attempts"], 1)
            self.assertNotIn("first_class", row.metrics)
        self.assertIn("doctrine-03: fail", result.report)
        self.assertTrue(any("doctrine-03#" in line and "stream leak" in line for line in progress))
        completions = [call for call in frontend.calls if call[1] == "/api/chat/completions"]
        turns = len(select_cases(loaded, "general-smoke").counted) * context.repeats
        self.assertEqual(len(completions), turns)

    def test_turn_cuts_fail_and_report_distinct_progress_slots(self) -> None:
        loaded = _loaded()
        frontend = SmokeFrontend(loaded)
        progress: list[str] = []
        _host, context = _context(frontend, progress)
        actual = run.frontend_turn

        def cut_rows(*args: Any, **kwargs: Any) -> run.TurnRow:
            row = actual(*args, **kwargs)
            case_id = cast(str, kwargs["case"].id)
            if case_id == "doctrine-03":
                return replace(row, cut=True, verdict_kind=None, stream_kind=None)
            if case_id == "doctrine-04":
                if cast(str, kwargs["row_name"]).endswith("#1"):
                    return replace(row, cut=True, stream_kind="error")
                return replace(row, verdict_kind=None, stream_kind=None)
            return row

        with patch.object(run, "frontend_turn", side_effect=cut_rows):
            result = general_smoke_slice.run_general_smoke(
                loaded, "general-smoke", context
            )

        rows = {(row.case_id, row.repeat): row for row in result.results}
        self.assertFalse(result.verdict)
        for case_id in ("doctrine-03", "doctrine-04"):
            self.assertEqual(rows[(case_id, 1)].verdict, "fail")
            self.assertEqual(rows[(case_id, 1)].metrics["problem"], "turn-cut")
            self.assertIn(f"{case_id}: fail", result.report)
        self.assertIn("repeat 1 error, turn-cut", result.report)
        self.assertTrue(
            any(
                line.startswith(
                    "general-smoke doctrine-03#1: "
                    f"cut at {run.TURN_TIMEOUT_SECONDS:.0f} s;"
                )
                for line in progress
            )
        )
        self.assertTrue(
            any(
                "general-smoke doctrine-04#1:" in line
                and f"stream cut at {run.TURN_TIMEOUT_SECONDS:.0f} s" in line
                for line in progress
            )
        )
        self.assertEqual(rows[("doctrine-04", 2)].metrics["problem"], "turn-error")

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
        self.assertEqual(
            result.report.splitlines()[-2],
            f"Fix: {run.unverified_fix(EVAL_IDENTITY.username)}",
        )

    def test_checkpoint_stops_before_the_next_turn(self) -> None:
        loaded = _loaded()
        frontend = SmokeFrontend(loaded)
        _host, context = _context(frontend, [])
        checkpoint_calls = 0

        def checkpoint() -> None:
            nonlocal checkpoint_calls
            checkpoint_calls += 1
            if checkpoint_calls == 3:
                raise window.WindowOverrun(NOW)

        with (
            patch.object(run, "frontend_turn", wraps=run.frontend_turn) as turn_spy,
            self.assertRaises(window.WindowOverrun),
        ):
            general_smoke_slice.run_general_smoke(
                loaded, "general-smoke", replace(context, checkpoint=checkpoint)
            )

        self.assertEqual(turn_spy.call_count, 2)
        self.assertEqual(len(frontend.deleted_chats), 2)

    def test_checkpoint_runs_before_every_attempt_including_resends(self) -> None:
        loaded = _loaded()
        frontend = SmokeFrontend(loaded)
        frontend.attempt_modes["doctrine-03"] = ["declined", "answered", "answered"]
        _host, context = _context(frontend, [])
        completed: list[int] = []

        def checkpoint() -> None:
            completed.append(len([
                call for call in frontend.calls if call[1] == "/api/chat/completions"
            ]))
            self.assertEqual(len(frontend.deleted_chats), completed[-1])

        result = general_smoke_slice.run_general_smoke(
            loaded, "general-smoke", replace(context, checkpoint=checkpoint)
        )

        self.assertTrue(result.verdict, result.report)
        self.assertEqual(completed, list(range(len(result.results) + 1)))
        self.assertEqual(len(frontend.deleted_chats), len(completed))

    def test_checkpoint_overrun_stops_before_resending_a_decline(self) -> None:
        loaded = _loaded()
        frontend = SmokeFrontend(loaded)
        frontend.attempt_modes["doctrine-03"] = ["declined", "answered"]
        progress: list[str] = []
        _host, context = _context(frontend, progress)

        def checkpoint() -> None:
            if progress and progress[-1].endswith("; re-sent"):
                raise window.WindowOverrun(NOW)

        with self.assertRaises(window.WindowOverrun):
            general_smoke_slice.run_general_smoke(
                loaded, "general-smoke", replace(context, checkpoint=checkpoint)
            )

        self.assertTrue(progress[-1].startswith("general-smoke doctrine-03#1: declined;"))
        self.assertEqual(frontend.attempt_modes["doctrine-03"], ["answered"])
        self.assertEqual(len(frontend.deleted_chats), len(progress))
        self.assertEqual(frontend.chats, {})

    def test_signin_refusal_fails_every_result_without_making_turns(self) -> None:
        loaded = _loaded()
        frontend = SmokeFrontend(loaded)
        frontend.refuse_signin_after = 0
        progress: list[str] = []
        _host, context = _context(frontend, progress)

        result = general_smoke_slice.run_general_smoke(loaded, "general-smoke", context)
        self.assertEqual(len(result.results), 22)
        self.assertTrue(all(row.metrics["problem"] == "frontend-signin" for row in result.results))
        for row in result.results:
            self.assertEqual(row.metrics["attempts"], 1)
            self.assertNotIn("first_class", row.metrics)
        self.assertEqual(
            [path for _method, path, _body in frontend.calls if path == "/api/chat/completions"],
            [],
        )
        self.assertEqual(frontend.stream_calls, [])
        self.assertEqual(
            result.report.splitlines()[:2],
            [
                "frontend signin: Open WebUI /api/v1/auths/signin returned HTTP 503.",
                "Fix: Check Open WebUI availability, then retry.",
            ],
        )

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
        for row in first.results:
            self.assertEqual(row.metrics["attempts"], 1)
            self.assertNotIn("first_class", row.metrics)
        self.assertTrue(all("stream" not in row.metrics and "checks" not in row.metrics for row in first.results))
        self.assertIn("turn access unavailable", first.report)
        self.assertEqual(host.calls, [])
        self.assertEqual(frontend.calls, [])


class GeneralSmokeCommand(unittest.TestCase):
    """The public command resolves turn access and records the smoke results."""

    def _run_command(
        self, *, supplied_set: bool, failing: bool = False, resent: bool = False
    ) -> tuple[int, str, str, EvalHost, SmokeFrontend, MagicMock]:
        loaded = _loaded()
        frontend = SmokeFrontend(loaded)
        if failing:
            frontend.modes["doctrine-03"] = "stream-leak"
        if resent:
            frontend.attempt_modes["doctrine-03"] = ["declined", "answered", "answered"]
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
                return_value=command.window.WindowJudgement(
                    True, "fixture quiet window", NOW, NOW + timedelta(days=1)
                ),
            ),
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
        self.assertIn("eval password read, door probed", stdout)
        self.assertIn("22 results over 11 active cases at 2 repeats", stdout)
        self.assertIn("record: ok — skipped", stdout)
        # The comparand is the committed eval/reference/general-smoke/; every fixture
        # case passes, so nothing regresses: stale while the id list has moved past
        # the recorded reference, current once a release re-records it.
        self.assertRegex(stdout, r"(?m)^reference: (?:current|stale)$")
        _assert_comparison_lines(self, stdout, word="pass")
        comparison.assert_called_once()
        self.assertFalse(any(argv[0] == "docker" for argv, _input in host.calls))
        self.assertNotIn(SENTINEL, stdout + stderr)
        self.assertEqual(len(frontend.stream_calls), 22)
        self.assertNotIn(SENTINEL, "".join(frontend.stream_payloads))

    def test_cli_release_run_records_kind_stack_and_results_then_compares(self) -> None:
        code, stdout, stderr, host, frontend, comparison = self._run_command(supplied_set=False)
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("22 results over 11 active cases at 2 repeats", stdout)
        self.assertIn("record: ok — run", stdout)
        # The comparand is the committed eval/reference/general-smoke/; every fixture
        # case passes, so nothing regresses: stale while the id list has moved past
        # the recorded reference, current once a release re-records it.
        self.assertRegex(stdout, r"(?m)^reference: (?:current|stale)$")
        _assert_comparison_lines(self, stdout, word="pass")
        comparison.assert_called_once()
        sql = "\n".join(
            cast(str, input_text)
            for argv, input_text in host.calls
            if argv[0] == "docker" and input_text != "SELECT 1;\n"
        )
        self.assertIn("\\set run_stack 'production'", sql)
        self.assertIn("\\set kind 'manual'", sql)
        self.assertEqual(sql.count("INSERT INTO eval_results"), 22)
        metrics = _recorded_metrics(sql)
        self.assertEqual(len(metrics), sql.count("INSERT INTO eval_results"))
        for row in metrics:
            self.assertEqual(row["attempts"], 1)
            self.assertNotIn("first_class", row)
        self.assertEqual(len(frontend.stream_calls), 22)
        self.assertNotIn(SENTINEL, stdout + stderr + sql)
        self.assertNotIn(SENTINEL, "".join(frontend.stream_payloads))

    def test_cli_resent_turn_records_attempts_without_adding_result_rows(self) -> None:
        code, stdout, stderr, host, frontend, comparison = self._run_command(
            supplied_set=False, resent=True
        )
        self.assertEqual((code, stderr), (0, ""), stdout)
        turns = len(select_cases(_loaded(), "general-smoke").counted) * 2
        self.assertIn("22 results over 11 active cases at 2 repeats", stdout)
        self.assertIn(f"{turns + 1} turns, 1 re-sent;", stdout)
        _assert_comparison_lines(self, stdout, word="pass")
        comparison.assert_called_once()
        sql = "\n".join(
            cast(str, input_text)
            for argv, input_text in host.calls
            if argv[0] == "docker" and input_text != "SELECT 1;\n"
        )
        self.assertEqual(sql.count("INSERT INTO eval_results"), turns)
        metrics = _recorded_metrics(sql)
        self.assertEqual(len(metrics), turns)
        self.assertEqual(sum(cast(int, row["attempts"]) for row in metrics), turns + 1)
        resent_rows = [row for row in metrics if cast(int, row["attempts"]) > 1]
        self.assertEqual(len(resent_rows), 1)
        self.assertEqual(resent_rows[0]["attempts"], 2)
        self.assertEqual(resent_rows[0]["first_class"], "declined")
        self.assertEqual(resent_rows[0]["class"], "answered")
        for row in metrics:
            if row is not resent_rows[0]:
                self.assertEqual(row["attempts"], 1)
                self.assertNotIn("first_class", row)
        completions = [call for call in frontend.calls if call[1] == "/api/chat/completions"]
        self.assertEqual(len(completions), turns + 1)
        self.assertEqual(len(frontend.deleted_chats), turns + 1)
        self.assertNotIn(SENTINEL, stdout + stderr + sql)
        for answer in frontend.answers.values():
            self.assertNotIn(answer, stdout + stderr + sql)

    def test_cli_set_run_with_a_resent_turn_still_skips_recording(self) -> None:
        code, stdout, stderr, host, frontend, comparison = self._run_command(
            supplied_set=True, resent=True
        )
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertIn("record: ok — skipped", stdout)
        self.assertIn("22 results over 11 active cases at 2 repeats", stdout)
        self.assertIn("1 re-sent;", stdout)
        self.assertFalse(any(argv[0] == "docker" for argv, _input in host.calls))
        turns = len(select_cases(_loaded(), "general-smoke").counted) * 2
        self.assertEqual(len(frontend.deleted_chats), turns + 1)
        comparison.assert_called_once()

    def test_cli_failing_turn_prints_fail(self) -> None:
        code, stdout, stderr, _host, _frontend, comparison = self._run_command(
            supplied_set=True, failing=True
        )
        self.assertEqual((code, stderr), (1, ""), stdout)
        self.assertIn("doctrine-03: fail", stdout)
        _assert_comparison_lines(self, stdout, word="FAIL")
        comparison.assert_called_once()


if __name__ == "__main__":
    unittest.main()
