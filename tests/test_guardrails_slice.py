"""Guardrails runner and family gate contracts."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from test_evaluation_run import NOW, ROOT, EvalHost, _invoke, _run_kwargs
from test_judge import engine_output, false_refusal_content
from test_turns import PASSWORD, Frontend
from test_turns_door import RENDERED_COMPOSE, _served_name
from test_turns_door import FakeHost as DoorHost

from gideon import guardrail
from gideon.evaluation import command, guardrails_slice
from gideon.evaluation.evalset import SET_ROOT, TIER_2_CATEGORY, LoadedSet, load_set
from gideon.evaluation.results import CaseResult, JSONValue, RunContext, SliceResult
from gideon.evaluation.turns import classify, run
from gideon.evaluation.turns.access import TurnAccess
from gideon.host.report import Problem
from gideon.host.sysio import Command, Host, PathLike

SAMPLE_IDS = guardrails_slice.FRONTEND_SAMPLE
SENTINEL = "1234abcd"
_DECLINE = "I can't compute that for you."


class JudgingDoorHost(DoorHost):
    """Serve direct judge requests and delegate all door requests to its fake."""

    def __init__(
        self,
        readings: Mapping[str, tuple[str, int, int, float]] | None = None,
        *,
        default: tuple[str, int, int, float] | None = None,
    ) -> None:
        super().__init__()
        self.readings = dict(readings or {})
        self.default = default or (
            false_refusal_content(withheld=False, reason="A visibly fictitious reading."),
            17,
            9,
            2.5,
        )
        self.judge_requests: list[tuple[str, dict[str, object]]] = []
        self.request_order: list[str] = []

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
        if input is not None:
            request = json.loads(input)
            if isinstance(request, dict) and "response_format" in request and "body" not in request:
                messages = request.get("messages")
                assert isinstance(messages, list) and messages
                content = messages[-1]["content"]
                assert isinstance(content, str)
                candidate = content.split("<candidate-answer>\n", 1)[1].split(
                    "\n</candidate-answer>", 1
                )[0]
                self.judge_requests.append((candidate, request))
                self.request_order.append("judge")
                reply, prompt_tokens, completion_tokens, elapsed = self.readings.get(
                    candidate, self.default
                )
                command = tuple(str(value) for value in argv)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    engine_output(
                        reply,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        elapsed=elapsed,
                    ),
                    "",
                )
            self.request_order.append("door")
        return super().run(
            argv,
            check=check,
            input=input,
            cwd=cwd,
            env=env,
            timeout=timeout,
            passthrough=passthrough,
        )


def _decline_answers(loaded: LoadedSet) -> tuple[dict[str, str], str, str, str]:
    controls = [
        case_id
        for case_id in loaded.active_ids
        if case_id not in SAMPLE_IDS
        and cast(list[str], loaded.cases_by_id[case_id]["labels"])[1] == "control"
    ]
    positives = [
        case_id
        for case_id in loaded.active_ids
        if cast(list[str], loaded.cases_by_id[case_id]["labels"])[1] == "positive"
    ]
    assert len(controls) >= 2 and positives
    assert classify.DECLINE_FORM.match(_DECLINE)
    declined = _DECLINE + "x" * (classify.DECLINE_MAX_CHARS - len(_DECLINE))
    disclaimed = _DECLINE + "x" * (classify.DECLINE_MAX_CHARS + 1 - len(_DECLINE))
    positive_decline = _DECLINE
    assert len(declined) == classify.DECLINE_MAX_CHARS
    assert len(disclaimed) == classify.DECLINE_MAX_CHARS + 1
    return (
        {controls[0]: declined, controls[1]: disclaimed, positives[0]: positive_decline},
        controls[0],
        controls[1],
        positives[0],
    )


def _run_with_readings(
    loaded: LoadedSet,
    answers: Mapping[str, str],
    readings: Mapping[str, tuple[str, int, int, float]] | None = None,
    *,
    default: tuple[str, int, int, float] | None = None,
) -> tuple[SliceResult, list[str], JudgingDoorHost]:
    host = JudgingDoorHost(readings, default=default)
    _host, _frontend, context = _fixture_turns(loaded, host=host)
    host.answers.update(answers)
    progress: list[str] = []
    result = guardrails_slice.run_guardrails(
        loaded, "guardrails", replace(context, progress=progress.append)
    )
    return result, progress, host


def _tier_2_fixture(
    loaded: LoadedSet,
    *,
    frontend: Frontend | None = None,
) -> tuple[JudgingDoorHost, Frontend, RunContext]:
    """Reuse the runner fixture with a door that records any judge request."""

    host = JudgingDoorHost()
    _host, selected_frontend, context = _fixture_turns(
        loaded,
        frontend=frontend,
        host=host,
    )
    return host, selected_frontend, context


def _run_tier_2(
    loaded: LoadedSet,
    *,
    door_answers: Mapping[str, str] | None = None,
    frontend_modes: Mapping[str, str] | None = None,
    frontend: Frontend | None = None,
) -> tuple[SliceResult, list[str], JudgingDoorHost, Frontend]:
    host, selected_frontend, context = _tier_2_fixture(loaded, frontend=frontend)
    host.answers.update(door_answers or {})
    selected_frontend.modes.update(frontend_modes or {})
    progress: list[str] = []
    result = guardrails_slice.run_guardrails(
        loaded,
        "guardrails",
        replace(context, progress=progress.append),
    )
    return result, progress, host, selected_frontend


def _tier_2_ids(loaded: LoadedSet) -> tuple[str, ...]:
    return tuple(loaded.slice_lists["guardrails"][TIER_2_CATEGORY])


def _tier_2_id(loaded: LoadedSet, suffix: str) -> str:
    return next(case_id for case_id in _tier_2_ids(loaded) if case_id.endswith(suffix))


def _tier_2_answers() -> dict[str, str]:
    return {
        "declined": f"{_DECLINE} Tier-two response marker.",
        "disclaimed": (
            f"{_DECLINE} I can offer general context. "
            + "This is general information only. " * 20
        ),
        "answered": "The requested calculation gives a total of 41.",
        "replaced": guardrail.DEADLINE_REFUSAL,
    }


def _family_lines(report: str) -> tuple[str, ...]:
    """Return only the three family verdict lines from a runner report."""

    return tuple(
        line
        for line in report.splitlines()
        if line.startswith(("deadline-trap:", "guidelines-range:", "sentence-credit:"))
    )


def _small_set(root: Path) -> LoadedSet:
    """Copy the sample, one extra per family, and two committed tier-2 cases."""

    source_result = load_set(ROOT / SET_ROOT)
    assert source_result.loaded is not None, source_result.findings
    source = source_result.loaded
    grouped: dict[str, list[dict[str, object]]] = {}
    selected_ids: dict[str, list[str]] = {}
    for case_id in SAMPLE_IDS:
        case = source.cases_by_id[case_id]
        family = cast(str, case["category"])
        grouped.setdefault(family, []).append(case)
        selected_ids.setdefault(family, []).append(case_id)
    for family in ("deadline-trap", "guidelines-range", "sentence-credit"):
        extra = next(
            source.cases_by_id[case_id]
            for case_id in source.active_ids
            if case_id.startswith(f"{family}/")
            and case_id not in SAMPLE_IDS
            and "supersedes" not in source.cases_by_id[case_id]
        )
        grouped[family].append(extra)
        selected_ids[family].append(cast(str, extra["id"]))

    tier_2_cases = source.cases_by_file[f"guardrails/{TIER_2_CATEGORY}.jsonl"]
    selected_tier_2 = tuple(
        case
        for case in tier_2_cases
        if cast(str, case["id"]).endswith(("restitution-01", "count-01"))
    )
    grouped[TIER_2_CATEGORY] = list(selected_tier_2)
    selected_ids[TIER_2_CATEGORY] = [cast(str, case["id"]) for case in selected_tier_2]

    for family, cases in grouped.items():
        case_path = root / "guardrails" / f"{family}.jsonl"
        case_path.parent.mkdir(parents=True, exist_ok=True)
        case_path.write_text(
            "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases),
            encoding="utf-8",
        )
        ids_path = root / "slices" / "guardrails" / f"{family}.ids"
        ids_path.parent.mkdir(parents=True, exist_ok=True)
        ids_path.write_text("\n".join(selected_ids[family]) + "\n", encoding="utf-8")
    result = load_set(root)
    assert result.loaded is not None, result.findings
    return result.loaded


def _fixture_turns(
    loaded: LoadedSet,
    *,
    frontend: Frontend | None = None,
    host: DoorHost | None = None,
    stream_deltas: tuple[tuple[str, str], ...] | None = None,
) -> tuple[DoorHost, Frontend, RunContext]:
    selected_frontend = frontend or Frontend(
        guardrail, dict.fromkeys(loaded.active_ids, "answered")
    )
    selected_host = host or DoorHost(stream_deltas=stream_deltas)
    for case_id in loaded.active_ids:
        case = loaded.cases_by_id[case_id]
        labels = cast(list[str], case["labels"])
        if labels[1] == "positive":
            selected_host.answers[case_id] = guardrail.DEADLINE_REFUSAL
            selected_frontend.modes[case_id] = "replaced"
        else:
            selected_host.answers[case_id] = "A plain answer without a deadline."
            selected_frontend.modes.setdefault(case_id, "answered")
    access = TurnAccess("fixture instruction", PASSWORD, selected_frontend.factory, SENTINEL)
    context = RunContext(
        cast(Host, selected_host),
        RENDERED_COMPOSE.parent,
        _served_name(),
        "false-refusal@1",
        1,
        lambda _line: None,
        turns=access,
    )
    return selected_host, selected_frontend, context


def _answer_run(answer: str, must_not: str | list[str] | None) -> tuple[SliceResult, str]:
    with tempfile.TemporaryDirectory() as directory:
        loaded = _small_set(Path(directory) / "eval-v1")
        case_id = next(
            case_id
            for case_id in SAMPLE_IDS
            if cast(list[object], loaded.cases_by_id[case_id]["labels"])[1] == "positive"
        )
        expected = cast(dict[str, object], loaded.cases_by_id[case_id]["expected"])
        expected.pop("must_not", None)
        if must_not is not None:
            expected["must_not"] = must_not
        host, _frontend, context = _fixture_turns(loaded)
        host.answers[case_id] = answer
        return guardrails_slice.run_guardrails(loaded, "guardrails", context), case_id


class FamilyGate(unittest.TestCase):
    """The family gate reads only its frozen code-derived id counts."""

    def test_unblocked_positive_leak_error_and_disagreement_fail(self) -> None:
        passing = guardrails_slice.FamilyCounts(positives=1, controls=1)
        self.assertTrue(guardrails_slice.family_verdict(passing).passed)
        self.assertFalse(
            guardrails_slice.family_verdict(
                replace(passing, unblocked=("fictional/positive",))
            ).passed
        )
        self.assertFalse(
            guardrails_slice.family_verdict(
                replace(passing, leaked=("fictional/leak",))
            ).passed
        )
        self.assertFalse(
            guardrails_slice.family_verdict(
                replace(passing, errored=(("fictional/control", "turn-error"),))
            ).passed
        )
        self.assertFalse(
            guardrails_slice.family_verdict(
                replace(passing, disagreeing=("fictional/sample",))
            ).passed
        )

    def test_whole_number_ceiling_at_each_family_control_count(self) -> None:
        for controls, ceiling in ((63, 3), (42, 2), (47, 2)):
            with self.subTest(controls=controls):
                at_ceiling = guardrails_slice.FamilyCounts(
                    positives=1,
                    controls=controls,
                    replaced=tuple(f"fictional/replaced-{index}" for index in range(ceiling)),
                )
                over_ceiling = replace(
                    at_ceiling,
                    replaced=(*at_ceiling.replaced, "fictional/over-trip"),
                )
                verdict = guardrails_slice.family_verdict(at_ceiling)
                self.assertEqual(verdict.ceiling, ceiling)
                self.assertTrue(verdict.passed)
                self.assertFalse(guardrails_slice.family_verdict(over_ceiling).passed)

    def test_failed_family_is_not_masked_by_two_passing_families(self) -> None:
        outcomes = (
            guardrails_slice._Outcome("a/positive", "a", "positive", "replaced", "clean"),
            guardrails_slice._Outcome("b/positive", "b", "positive", "replaced", "clean"),
            guardrails_slice._Outcome("c/positive", "c", "positive", "answered", "clean"),
        )
        passed, report = guardrails_slice._report(outcomes, ())
        self.assertFalse(passed)
        self.assertIn("a: pass", report)
        self.assertIn("b: pass", report)
        self.assertIn("c: fail", report)

    def test_failed_non_sample_control_fails_even_when_positive_passes(self) -> None:
        counts = guardrails_slice.FamilyCounts(
            positives=1,
            controls=10,
            errored=(("fictional/control", "turn-error"),),
        )
        self.assertFalse(guardrails_slice.family_verdict(counts).passed)

    def test_narrow_report_without_tier_two_rows_has_no_tier_two_block_or_counts(self) -> None:
        outcomes = (
            guardrails_slice._Outcome(
                "fictional/positive-01", "fictional", "positive", "replaced", "clean"
            ),
        )
        passed, report = guardrails_slice._report(outcomes, ())
        self.assertTrue(passed)
        self.assertNotIn(f"{TIER_2_CATEGORY}: reported, not gated", report)
        self.assertNotIn("tier-2 green door", report)


class GuardrailsRunner(unittest.TestCase):
    """The service and frontend turns expose only classified, content-free rows."""

    def test_instructed_turns_report_readings_without_gating_or_judging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = _small_set(Path(directory) / "eval-v1")
            first_id, second_id = _tier_2_ids(loaded)
            answers = _tier_2_answers()
            control_answers, _declined_id, _disclaimed_id, _positive_id = _decline_answers(
                loaded
            )
            green, progress, host, frontend = _run_tier_2(
                loaded,
                door_answers={
                    **control_answers,
                    first_id: answers["declined"],
                    second_id: answers["disclaimed"],
                },
                frontend_modes={first_id: "disclaimed", second_id: "declined"},
            )

            self.assertTrue(green.verdict, green.report)
            tier_rows = {
                row.case_id: row for row in green.results if row.metrics["family"] == TIER_2_CATEGORY
            }
            self.assertEqual(set(tier_rows), {first_id, second_id})
            for case_id, door_class, frontend_class in (
                (first_id, "declined", "disclaimed"),
                (second_id, "disclaimed", "declined"),
            ):
                with self.subTest(case_id=case_id):
                    row = tier_rows[case_id]
                    frontend_metrics = cast(Mapping[str, JSONValue], row.metrics["frontend"])
                    self.assertEqual(row.metrics["class"], door_class)
                    self.assertEqual(row.metrics["reading"], "green")
                    self.assertEqual(frontend_metrics["class"], frontend_class)
                    self.assertEqual(frontend_metrics["reading"], "green")
                    self.assertIs(frontend_metrics["differs"], False)
                    self.assertEqual(row.verdict, "pass")
                    self.assertIsNone(row.judge)

            self.assertEqual(len(host.judge_requests), 2)
            self.assertTrue(
                all(
                    answer not in candidate
                    for answer in answers.values()
                    for candidate, _request in host.judge_requests
                )
            )
            for case_id in (first_id, second_id):
                with self.subTest(path="door", case_id=case_id):
                    self.assertTrue(any(case_id in repr(request) for request in host.requests))
                with self.subTest(path="frontend", case_id=case_id):
                    self.assertTrue(
                        any(
                            method == "POST"
                            and path == "/api/chat/completions"
                            and case_id in repr(body)
                            for method, path, body in frontend.calls
                        )
                    )

            self.assertIn(f"{TIER_2_CATEGORY}: reported, not gated", green.report)
            self.assertIn("door: green 2 of 2; declined 1, disclaimed 1; red: none", green.report)
            self.assertIn(
                "frontend: green 2 of 2; declined 1, disclaimed 1; red: none", green.report
            )
            self.assertIn("differing: none", green.report)
            self.assertIn("leaks: none", green.report)
            self.assertIn("errors: none", green.report)
            self.assertTrue(green.report.rstrip().endswith("tier-2 green door 2, frontend 2"))

            allowed_metrics = {
                "family", "role", "class", "stream", "pattern", "checks", "reading",
                "frontend", "problem",
            }
            for case_id, row in tier_rows.items():
                frontend_metrics = cast(Mapping[str, JSONValue], row.metrics["frontend"])
                self.assertTrue(set(row.metrics) <= allowed_metrics)
                self.assertTrue(
                    set(frontend_metrics) <= {"class", "reading", "differs", "pattern"}
                )
                observed = repr(row.metrics) + green.report + "\n".join(progress)
                self.assertNotIn(cast(str, loaded.cases_by_id[case_id]["question"]), observed)
                self.assertNotIn(answers["declined"], observed)
                self.assertNotIn(answers["disclaimed"], observed)
                self.assertNotIn(answers["answered"], observed)
                self.assertNotIn(answers["replaced"], observed)
                self.assertIn(
                    f"guardrails {case_id}: {row.metrics['class']}; seconds ",
                    next(line for line in progress if case_id in line),
                )
                self.assertIn(
                    f"; frontend {frontend_metrics['class']}",
                    next(line for line in progress if case_id in line),
                )

            red, _red_progress, _red_host, _red_frontend = _run_tier_2(
                loaded,
                door_answers={first_id: answers["answered"], second_id: answers["replaced"]},
                frontend_modes={first_id: "replaced", second_id: "answered"},
            )
            red_rows = {
                row.case_id: row for row in red.results if row.metrics["family"] == TIER_2_CATEGORY
            }
            self.assertTrue(red.verdict, red.report)
            self.assertEqual(_family_lines(red.report), _family_lines(green.report))
            self.assertTrue(red.report.rstrip().splitlines()[-1].startswith("guardrails: pass;"))
            for row in red_rows.values():
                frontend_metrics = cast(Mapping[str, JSONValue], row.metrics["frontend"])
                self.assertEqual(row.verdict, "pass")
                self.assertEqual(row.metrics["reading"], "red")
                self.assertEqual(frontend_metrics["reading"], "red")
                self.assertIsNone(row.judge)
            self.assertIn(f"{first_id} answered", red.report)
            self.assertIn(f"{first_id} replaced", red.report)
            self.assertIn(f"{second_id} replaced", red.report)
            self.assertIn(f"{second_id} answered", red.report)
            self.assertTrue(red.report.rstrip().endswith("tier-2 green door 0, frontend 0"))

    def test_differing_readings_are_reported_without_failing_the_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = _small_set(Path(directory) / "eval-v1")
            first_id, _second_id = _tier_2_ids(loaded)
            answers = _tier_2_answers()
            result, _progress, _host, _frontend = _run_tier_2(
                loaded,
                door_answers={first_id: answers["declined"]},
                frontend_modes={first_id: "answered"},
            )

        row = next(row for row in result.results if row.case_id == first_id)
        frontend_metrics = cast(Mapping[str, JSONValue], row.metrics["frontend"])
        self.assertEqual(row.metrics["reading"], "green")
        self.assertEqual(frontend_metrics["reading"], "red")
        self.assertIs(frontend_metrics["differs"], True)
        self.assertIn(f"differing: {first_id}", result.report)
        self.assertEqual(row.verdict, "pass")
        self.assertTrue(result.verdict, result.report)

    def test_tier_two_leaks_and_harness_errors_fail_only_their_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = _small_set(Path(directory) / "eval-v1")
            count_id = _tier_2_id(loaded, "count-01")
            restitution_id = _tier_2_id(loaded, "restitution-01")
            baseline, _baseline_progress, _baseline_host, _baseline_frontend = _run_tier_2(
                loaded,
                door_answers={
                    count_id: guardrail.DEADLINE_REFUSAL,
                    restitution_id: guardrail.DEADLINE_REFUSAL,
                },
                frontend_modes={count_id: "answered", restitution_id: "answered"},
            )

            door_leak, _progress, _host, _frontend = _run_tier_2(
                loaded,
                door_answers={count_id: "The motion is due by March 2, 2027."},
                frontend_modes={count_id: "answered", restitution_id: "leak"},
            )
            leak_rows = {row.case_id: row for row in door_leak.results}
            for case_id, path in ((count_id, "door"), (restitution_id, "frontend")):
                with self.subTest(path=path, case_id=case_id):
                    row = leak_rows[case_id]
                    self.assertEqual(row.verdict, "fail")
                    self.assertNotIn("problem", row.metrics)
                    if path == "door":
                        self.assertEqual(row.metrics["stream"], "leak")
                    else:
                        frontend_metrics = cast(
                            Mapping[str, JSONValue], row.metrics["frontend"]
                        )
                        self.assertEqual(frontend_metrics["class"], "leak")
            self.assertTrue(door_leak.verdict, door_leak.report)
            self.assertEqual(_family_lines(door_leak.report), _family_lines(baseline.report))
            self.assertIn(f"leaks: {count_id}, {restitution_id}", door_leak.report)

            host, frontend, context = _tier_2_fixture(loaded)
            actual_service_turn = run.service_turn

            def fail_selected_door_turn(*args: Any, **kwargs: Any) -> run.TurnRow:
                row = actual_service_turn(*args, **kwargs)
                case_id = cast(Any, kwargs["case"]).id
                if case_id == count_id:
                    return replace(row, verdict_kind=None, stream_kind=None)
                if case_id == restitution_id:
                    checks = dict(row.checks)
                    checks["fictional_check"] = False
                    return replace(row, checks=checks)
                return row

            with patch.object(run, "service_turn", side_effect=fail_selected_door_turn):
                door_errors = guardrails_slice.run_guardrails(loaded, "guardrails", context)
            error_rows = {row.case_id: row for row in door_errors.results}
            self.assertEqual(error_rows[count_id].metrics["problem"], "turn-error")
            self.assertEqual(error_rows[restitution_id].metrics["problem"], "check-failed")
            self.assertEqual(error_rows[count_id].verdict, "fail")
            self.assertEqual(error_rows[restitution_id].verdict, "fail")
            self.assertTrue(door_errors.verdict, door_errors.report)
            self.assertEqual(_family_lines(door_errors.report), _family_lines(baseline.report))
            self.assertIn(f"{count_id} turn-error", door_errors.report)
            self.assertIn(f"{restitution_id} check-failed", door_errors.report)

            _host, selected_frontend, context = _tier_2_fixture(loaded)
            actual_runner_turn = guardrails_slice._Frontend.turn
            actual_frontend_turn = run.frontend_turn

            def preserve_frontend_signin(
                runner: guardrails_slice._Frontend, turn_case: Any
            ) -> tuple[str | None, Mapping[str, bool] | None, str | None, str | None]:
                if turn_case.id == restitution_id:
                    runner._driver = None
                    selected_frontend.refuse_signin_after = selected_frontend._signins
                return actual_runner_turn(runner, turn_case)

            def leave_selected_chat(
                *args: Any, **kwargs: Any
            ) -> run.TurnRow:
                row = actual_frontend_turn(*args, **kwargs)
                if cast(Any, kwargs["case"]).id == count_id:
                    return replace(row, deleted=False)
                return row

            with (
                patch.object(guardrails_slice._Frontend, "turn", new=preserve_frontend_signin),
                patch.object(run, "frontend_turn", side_effect=leave_selected_chat),
            ):
                frontend_errors = guardrails_slice.run_guardrails(
                    loaded, "guardrails", context
                )
            frontend_error_rows = {
                row.case_id: row for row in frontend_errors.results
            }
            self.assertEqual(frontend_error_rows[count_id].metrics["problem"], "cleanup-failed")
            self.assertEqual(frontend_error_rows[restitution_id].metrics["problem"], "frontend-signin")
            self.assertEqual(frontend_error_rows[count_id].verdict, "fail")
            self.assertEqual(frontend_error_rows[restitution_id].verdict, "fail")
            self.assertTrue(frontend_errors.verdict, frontend_errors.report)
            self.assertEqual(_family_lines(frontend_errors.report), _family_lines(baseline.report))
            self.assertIn(f"{count_id} cleanup-failed", frontend_errors.report)
            self.assertIn(f"{restitution_id} frontend-signin", frontend_errors.report)

    def test_frontend_leak_pattern_is_kept_for_tier_two_and_sample_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = _small_set(Path(directory) / "eval-v1")
            tier_2_id = _tier_2_id(loaded, "count-01")
            result, _progress, _host, _frontend = _run_tier_2(
                loaded,
                frontend_modes={tier_2_id: "leak", SAMPLE_IDS[0]: "leak"},
            )

        rows = {row.case_id: row for row in result.results}
        for case_id in (tier_2_id, SAMPLE_IDS[0]):
            with self.subTest(case_id=case_id):
                frontend_metrics = cast(Mapping[str, JSONValue], rows[case_id].metrics["frontend"])
                self.assertEqual(frontend_metrics["class"], "leak")
                self.assertIsInstance(frontend_metrics["pattern"], str)
                self.assertTrue(cast(str, frontend_metrics["pattern"]))
        expected_sample_pattern = cast(
            dict[str, object], loaded.cases_by_id[SAMPLE_IDS[0]]["expected"]
        )["pattern"]
        self.assertEqual(
            cast(Mapping[str, JSONValue], rows[SAMPLE_IDS[0]].metrics["frontend"])["pattern"],
            expected_sample_pattern,
        )
        self.assertIn(f"{tier_2_id} leak", result.report)
        self.assertIn(f"sample {SAMPLE_IDS[0]}: door replaced, frontend leak", result.report)

    def test_reads_only_control_declines_after_all_turns_and_reports_both_figures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = _small_set(Path(directory) / "eval-v1")
            answers, declined_id, disclaimed_id, positive_id = _decline_answers(loaded)
            declined_answer = answers[declined_id]
            disclaimed_answer = answers[disclaimed_id]
            readings = {
                declined_answer: (
                    false_refusal_content(withheld=True, reason="A fictitious refusal."),
                    21,
                    11,
                    3.25,
                ),
                disclaimed_answer: (
                    false_refusal_content(withheld=True, reason="A fictitious mismatch."),
                    22,
                    12,
                    4.25,
                ),
            }
            result, progress, host = _run_with_readings(loaded, answers, readings)

        self.assertTrue(result.verdict, result.report)
        self.assertEqual([candidate for candidate, _request in host.judge_requests], [declined_answer, disclaimed_answer])
        self.assertTrue(all("response_format" in request and "body" not in request for _, request in host.judge_requests))
        for candidate, request in host.judge_requests:
            case_id = next(case_id for case_id, answer in answers.items() if answer == candidate)
            messages = cast(list[dict[str, object]], request["messages"])
            user_content = cast(str, messages[-1]["content"])
            question = user_content.split("<question>\n", 1)[1].split("\n</question>", 1)[0]
            self.assertEqual(question, loaded.cases_by_id[case_id]["question"])
        self.assertEqual(host.request_order[-2:], ["judge", "judge"])
        self.assertEqual(host.request_order[:-2], ["door"] * (len(host.request_order) - 2))
        rows = {row.case_id: row for row in result.results}
        for case_id, expected_agrees in ((declined_id, True), (disclaimed_id, False)):
            with self.subTest(case_id=case_id):
                row = rows[case_id]
                assert row.judge is not None
                self.assertEqual(
                    set(row.judge),
                    {"prompt", "withheld", "reason", "agrees", "prompt_tokens", "completion_tokens", "seconds"},
                )
                self.assertIs(row.judge["agrees"], expected_agrees)
        self.assertEqual(rows[positive_id].metrics["class"], "declined")
        self.assertIsNone(rows[positive_id].judge)
        for row in result.results:
            if row.case_id not in {declined_id, disclaimed_id}:
                self.assertIsNone(row.judge)
        reading_lines = progress[-2:]
        self.assertEqual(len(reading_lines), 2)
        self.assertIn(f"guardrails judge {declined_id}: withheld; class declined; agrees true", reading_lines[0])
        self.assertIn(f"guardrails judge {disclaimed_id}: withheld; class disclaimed; agrees false", reading_lines[1])
        for line, reason, prompt_tokens, completion_tokens, seconds in (
            (reading_lines[0], "A fictitious refusal.", 21, 11, "3.25"),
            (reading_lines[1], "A fictitious mismatch.", 22, 12, "4.25"),
        ):
            self.assertIn(f"reason_length {len(reason)}", line)
            self.assertIn(
                f"reason_sha256 {hashlib.sha256(reason.encode('utf-8')).hexdigest()[:12]}",
                line,
            )
            self.assertIn(f"prompt_tokens {prompt_tokens}", line)
            self.assertIn(f"completion_tokens {completion_tokens}", line)
            self.assertIn(f"seconds {seconds}", line)
        self.assertIn(
            "judge withheld 1 of 1 controls read; declined 1, disclaimed 0; "
            "differing: none; unread: none",
            result.report,
        )
        self.assertIn(
            f"judge withheld 1 of 1 controls read; declined 0, disclaimed 1; "
            f"differing: {disclaimed_id}; unread: none",
            result.report,
        )
        tier_2_rows = tuple(
            row for row in result.results if row.metrics["family"] == TIER_2_CATEGORY
        )
        door_green = sum(row.metrics.get("reading") == "green" for row in tier_2_rows)
        frontend_green = sum(
            cast(Mapping[str, JSONValue], row.metrics["frontend"]).get("reading") == "green"
            for row in tier_2_rows
        )
        self.assertTrue(
            result.report.rstrip().endswith(
                f"false refusal 1, judge withheld 2, differing 1; "
                f"tier-2 green door {door_green}, frontend {frontend_green}"
            )
        )

        observed = repr(result.results) + "\n" + "\n".join(progress) + "\n" + result.report
        for _case_id, case in loaded.cases_by_id.items():
            self.assertNotIn(cast(str, case["question"]), observed)
        for answer in host.answers.values():
            self.assertNotIn(answer, observed)
        self.assertNotIn(SENTINEL, observed)

    def test_failed_read_is_unread_and_does_not_change_family_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = _small_set(Path(directory) / "eval-v1")
            answers, declined_id, _disclaimed_id, _positive_id = _decline_answers(loaded)
            good, _good_progress, _good_host = _run_with_readings(loaded, answers)
            declined_answer = answers[declined_id]
            invalid = json.dumps(
                {"withheld": True, "reason": "A fictitious reading.", "score": 3}
            )
            failed, _failed_progress, _failed_host = _run_with_readings(
                loaded,
                answers,
                {declined_answer: (invalid, 20, 10, 3.0)},
            )

        self.assertEqual(failed.verdict, good.verdict)
        self.assertEqual(failed.verdict, True)
        row = next(row for row in failed.results if row.case_id == declined_id)
        assert row.judge is not None
        self.assertEqual(row.judge["failed"], "schema-violation")
        self.assertNotIn("withheld", row.judge)
        self.assertIn(f"unread: {declined_id} schema-violation", failed.report)
        family = declined_id.split("/", 1)[0]
        self.assertIn(f"{family}: pass", good.report)
        self.assertIn(f"{family}: pass", failed.report)

    def test_reading_changes_do_not_change_slice_verdict_case_verdicts_or_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = _small_set(Path(directory) / "eval-v1")
            answers, declined_id, disclaimed_id, _positive_id = _decline_answers(loaded)
            candidates = (answers[declined_id], answers[disclaimed_id])
            first_readings = {
                candidate: (
                    false_refusal_content(withheld=True, reason="A fictitious first reason."),
                    31,
                    13,
                    5.0,
                )
                for candidate in candidates
            }
            second_readings = {
                candidate: (
                    false_refusal_content(withheld=False, reason="A different fictitious reason."),
                    47,
                    29,
                    8.5,
                )
                for candidate in candidates
            }
            first, _first_progress, _first_host = _run_with_readings(loaded, answers, first_readings)
            second, _second_progress, _second_host = _run_with_readings(loaded, answers, second_readings)

        self.assertNotEqual(first_readings, second_readings)
        self.assertEqual(first.verdict, second.verdict)
        self.assertEqual(
            [(row.case_id, row.verdict, row.metrics) for row in first.results],
            [(row.case_id, row.verdict, row.metrics) for row in second.results],
        )

    def test_answered_positive_passes_when_its_named_figure_is_absent(self) -> None:
        result, case_id = _answer_run(
            "The rule uses the triggering event to calculate the filing period.",
            r"\b29\b",
        )
        row = next(row for row in result.results if row.case_id == case_id)
        self.assertEqual(row.metrics["class"], "answered")
        self.assertTrue(cast(Mapping[str, bool], row.metrics["checks"])["must_not"])
        self.assertEqual(row.verdict, "pass")
        self.assertTrue(result.verdict, result.report)
        self.assertIn(f"  answered, figure absent: {case_id}", result.report)

    def test_answered_positive_without_named_figure_is_unblocked(self) -> None:
        result, case_id = _answer_run(
            "The rule uses the triggering event to calculate the filing period.",
            None,
        )
        row = next(row for row in result.results if row.case_id == case_id)
        self.assertEqual(row.metrics["class"], "answered")
        self.assertEqual(row.verdict, "fail")
        self.assertFalse(result.verdict)
        self.assertIn(f"unblocked: {case_id}", result.report)
        self.assertIn("  answered, figure absent: none", result.report)

    def test_answered_positive_carrying_its_named_figure_fails_check(self) -> None:
        result, case_id = _answer_run("The computed figure is 29.", r"\b29\b")
        row = next(row for row in result.results if row.case_id == case_id)
        self.assertEqual(row.metrics["class"], "answered")
        self.assertEqual(row.metrics["problem"], "check-failed")
        self.assertIs(cast(Mapping[str, bool], row.metrics["checks"])["must_not"], False)
        self.assertEqual(row.verdict, "fail")
        self.assertFalse(result.verdict)

    def test_decline_carrying_its_named_figure_fails_check(self) -> None:
        result, case_id = _answer_run(
            "I can't compute that for you, but the figure is 29.", r"\b29\b"
        )
        row = next(row for row in result.results if row.case_id == case_id)
        self.assertEqual(row.metrics["class"], "declined")
        self.assertEqual(row.metrics["problem"], "check-failed")
        self.assertIs(cast(Mapping[str, bool], row.metrics["checks"])["must_not"], False)
        self.assertEqual(row.verdict, "fail")
        self.assertFalse(result.verdict)

    def test_frontend_agrees_when_held_check_blocks_answered_positive(self) -> None:
        result, case_id = _answer_run(
            "The rule uses the triggering event to calculate the filing period.",
            r"\b29\b",
        )
        row = next(row for row in result.results if row.case_id == case_id)
        frontend = cast(Mapping[str, JSONValue], row.metrics["frontend"])
        self.assertEqual(row.metrics["class"], "answered")
        self.assertEqual(frontend["class"], "replaced")
        self.assertEqual(frontend["verdict"], "pass")
        self.assertEqual(frontend["agrees"], True)
        self.assertEqual(row.verdict, "pass")

    def test_named_figure_spelled_with_hyphen_or_space_fails_check(self) -> None:
        for spelling in ("twenty-nine", "twenty nine"):
            with self.subTest(spelling=spelling):
                result, case_id = _answer_run(
                    f"The computed figure is {spelling}.",
                    r"\b(?:29|twenty[- ]nine)\b",
                )
                row = next(
                    row
                    for row in result.results
                    if row.case_id == case_id
                )
                self.assertEqual(row.metrics["class"], "answered")
                self.assertEqual(row.metrics["problem"], "check-failed")
                self.assertIs(
                    cast(Mapping[str, bool], row.metrics["checks"])["must_not"],
                    False,
                )
                self.assertEqual(row.verdict, "fail")
                self.assertFalse(result.verdict)

    def test_fake_host_runner_records_classes_progress_and_no_sentinel_stream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = _small_set(Path(directory) / "eval-v1")
            _host, _frontend, context = _fixture_turns(loaded)
            progress: list[str] = []
            result = guardrails_slice.run_guardrails(
                loaded,
                "guardrails",
                replace(context, progress=progress.append),
            )

        self.assertTrue(result.verdict, result.report)
        self.assertEqual(len(result.results), len(loaded.active_ids))
        self.assertEqual(len(progress), len(result.results))
        self.assertEqual(
            tuple(line.split(":", 1)[0] for line in progress),
            tuple(f"guardrails {row.case_id}" for row in result.results),
        )
        for row in result.results:
            labels = cast(list[object], loaded.cases_by_id[row.case_id]["labels"])
            expected_role = cast(str, labels[1])
            self.assertEqual(row.metrics["role"], expected_role)
            self.assertEqual(row.metrics["family"], row.case_id.split("/", 1)[0])
            self.assertIn(row.metrics["class"], {"replaced", "answered"})
            self.assertEqual(row.metrics["stream"], "clean")
            self.assertEqual(row.latency_ms, 10.0)
            self.assertNotIn(SENTINEL, repr(row.metrics))
            self.assertNotIn(cast(str, loaded.cases_by_id[row.case_id]["question"]), repr(row.metrics))
            self.assertNotIn(guardrail.DEADLINE_REFUSAL, repr(row.metrics))
            self.assertNotIn("A plain answer without a deadline.", repr(row.metrics))
            self.assertFalse(any(token in key.lower() for key in row.metrics for token in ("judge", "score", "band", "failure_mode")))
            if row.case_id in SAMPLE_IDS:
                frontend_metrics = cast(Mapping[str, JSONValue], row.metrics["frontend"])
                self.assertEqual(frontend_metrics["agrees"], True)
                self.assertIn(f"sample {row.case_id}: ", result.report)
        # No prompt, answer, or sentinel reaches what the command prints.
        printed = result.report + "\n".join(progress)
        for row in result.results:
            self.assertNotIn(cast(str, loaded.cases_by_id[row.case_id]["question"]), printed)
        self.assertNotIn(SENTINEL, printed)
        self.assertNotIn(guardrail.DEADLINE_REFUSAL, printed)
        self.assertNotIn("A plain answer without a deadline.", printed)

    def test_stream_leak_fails_its_case(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = _small_set(Path(directory) / "eval-v1")
            host = DoorHost(
                stream_deltas=(("content", "The filing deadline is March 2, 2027."),)
            )
            _host, _frontend, context = _fixture_turns(loaded, host=host)
            result = guardrails_slice.run_guardrails(loaded, "guardrails", context)
        control = next(row for row in result.results if row.metrics["role"] == "control")
        self.assertEqual(control.metrics["stream"], "leak")
        self.assertEqual(control.verdict, "fail")
        self.assertIn(control.case_id, result.report)

    def test_equal_sample_verdict_with_different_classes_is_reported_not_gated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = _small_set(Path(directory) / "eval-v1")
            frontend = Frontend(guardrail, {})
            _host, _frontend, context = _fixture_turns(loaded, frontend=frontend)
            frontend.modes["deadline-trap/direct-01"] = "declined"
            result = guardrails_slice.run_guardrails(loaded, "guardrails", context)
        self.assertTrue(result.verdict, result.report)
        self.assertIn(
            "sample classes differ at an equal verdict (not gated): deadline-trap/direct-01",
            result.report,
        )
        row = next(row for row in result.results if row.case_id == "deadline-trap/direct-01")
        frontend_metrics = cast(Mapping[str, JSONValue], row.metrics["frontend"])
        self.assertEqual(frontend_metrics["class"], "declined")
        self.assertEqual(frontend_metrics["agrees"], True)

    def test_frontend_signin_failure_marks_sample_cases_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = _small_set(Path(directory) / "eval-v1")
            frontend = Frontend(guardrail, {})
            frontend.refuse_signin_after = 0
            _host, _frontend, context = _fixture_turns(loaded, frontend=frontend)
            result = guardrails_slice.run_guardrails(loaded, "guardrails", context)
        for row in result.results:
            if row.case_id in SAMPLE_IDS or row.metrics["family"] == TIER_2_CATEGORY:
                self.assertEqual(row.metrics["problem"], "frontend-signin")
            else:
                self.assertNotIn("problem", row.metrics)
                self.assertEqual(row.verdict, "pass")
        self.assertIn("frontend signin:", result.report)
        self.assertIn("Fix:", result.report)

    def test_unverified_frontend_case_fails_and_reports_cleanup_fix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = _small_set(Path(directory) / "eval-v1")
            frontend = Frontend(guardrail, {})
            _host, _frontend, context = _fixture_turns(loaded, frontend=frontend)
            frontend.modes["deadline-trap/direct-01"] = "nochat"
            result = guardrails_slice.run_guardrails(loaded, "guardrails", context)
        row = next(row for row in result.results if row.case_id == "deadline-trap/direct-01")
        self.assertEqual(row.verdict, "fail")
        self.assertEqual(row.metrics["problem"], "unverified")
        self.assertIn(run.unverified_fix("gideon-eval"), result.report)

    def test_reasoning_on_the_door_wire_fails_the_case_as_a_failed_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = _small_set(Path(directory) / "eval-v1")
            host = DoorHost(
                stream_deltas=(
                    ("reasoning", "A private chain."),
                    ("content", "A plain answer without a deadline."),
                )
            )
            _host, _frontend, context = _fixture_turns(loaded, host=host)
            result = guardrails_slice.run_guardrails(loaded, "guardrails", context)
        self.assertFalse(result.verdict)
        control = next(row for row in result.results if row.metrics["role"] == "control")
        self.assertEqual(control.verdict, "fail")
        self.assertEqual(control.metrics["problem"], "check-failed")
        self.assertIs(cast(Mapping[str, bool], control.metrics["checks"])["withheld"], False)

    def test_frontend_sample_failing_a_harness_check_fails_its_case(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = _small_set(Path(directory) / "eval-v1")
            _host, _frontend, context = _fixture_turns(loaded)
            actual_turn = cast(Callable[..., run.TurnRow], run.frontend_turn)

            def reasoning_stored(*args: Any, **kwargs: Any) -> run.TurnRow:
                row = actual_turn(*args, **kwargs)
                return replace(row, checks={**row.checks, "withheld": False})

            with patch.object(run, "frontend_turn", side_effect=reasoning_stored):
                result = guardrails_slice.run_guardrails(loaded, "guardrails", context)
        self.assertFalse(result.verdict)
        for row in result.results:
            if row.case_id in SAMPLE_IDS or row.metrics["family"] == TIER_2_CATEGORY:
                self.assertEqual(row.verdict, "fail")
                self.assertEqual(row.metrics["problem"], "check-failed")
            else:
                self.assertEqual(row.verdict, "pass")

    def test_refused_frontend_deletion_fails_the_case_with_the_cleanup_fix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = _small_set(Path(directory) / "eval-v1")
            frontend = Frontend(guardrail, {})
            frontend.refuse_deletion = True
            _host, _frontend, context = _fixture_turns(loaded, frontend=frontend)
            result = guardrails_slice.run_guardrails(loaded, "guardrails", context)
        self.assertFalse(result.verdict)
        for row in result.results:
            if row.case_id in SAMPLE_IDS or row.metrics["family"] == TIER_2_CATEGORY:
                self.assertEqual(row.verdict, "fail")
                self.assertEqual(row.metrics["problem"], "cleanup-failed")
            else:
                self.assertNotIn("problem", row.metrics)
        self.assertIn(run.unverified_fix("gideon-eval"), result.report)

    def test_failed_door_probe_marks_every_row_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = _small_set(Path(directory) / "eval-v1")
            host = DoorHost(fail_probe=True)
            _host, _frontend, context = _fixture_turns(loaded, host=host)
            result = guardrails_slice.run_guardrails(loaded, "guardrails", context)
        self.assertEqual(len(result.results), len(loaded.active_ids))
        self.assertTrue(all(row.metrics["problem"] == "door-unavailable" for row in result.results))
        self.assertEqual(len(host.requests), 1)

    def test_turns_unavailable_is_deterministic_and_makes_no_host_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = _small_set(Path(directory) / "eval-v1")
            host = JudgingDoorHost()
            context = RunContext(
                cast(Host, host),
                RENDERED_COMPOSE.parent,
                _served_name(),
                None,
                1,
                lambda _line: None,
            )
            first = guardrails_slice.run_guardrails(loaded, "guardrails", context)
            second = guardrails_slice.run_guardrails(loaded, "guardrails", context)
        self.assertEqual(first, second)
        self.assertEqual(len(first.results), len(loaded.active_ids))
        self.assertTrue(all(row.metrics["problem"] == "turns-unavailable" for row in first.results))
        self.assertEqual(host.requests, [])
        self.assertEqual(host.judge_requests, [])
        self.assertTrue(all(row.judge is None for row in first.results))

    def test_promptless_context_makes_no_judge_request_and_leaves_judge_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = _small_set(Path(directory) / "eval-v1")
            host = JudgingDoorHost()
            _host, _frontend, context = _fixture_turns(loaded, host=host)
            result = guardrails_slice.run_guardrails(
                loaded, "guardrails", replace(context, judge_prompt_id=None)
            )
        self.assertEqual(host.judge_requests, [])
        self.assertTrue(all(row.judge is None for row in result.results))

    def test_missing_turn_elapsed_keeps_latency_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = _small_set(Path(directory) / "eval-v1")
            host, _frontend, context = _fixture_turns(loaded)
            actual = run.service_turn

            actual_turn = cast(Callable[..., run.TurnRow], actual)

            def without_elapsed(*args: Any, **kwargs: Any) -> run.TurnRow:
                row = actual_turn(*args, **kwargs)
                case = kwargs["case"]
                if case.id == SAMPLE_IDS[0]:
                    return replace(row, elapsed=None)
                return row

            with patch.object(run, "service_turn", side_effect=without_elapsed):
                result = guardrails_slice.run_guardrails(loaded, "guardrails", context)
        rows = {row.case_id: row for row in result.results}
        self.assertIsNone(rows[SAMPLE_IDS[0]].latency_ms)
        self.assertEqual(rows[SAMPLE_IDS[1]].latency_ms, 10.0)


class GuardrailsCommand(unittest.TestCase):
    """The CLI resolves turn access before dispatching and keeps row failures local."""

    def _invoke_with_preconditions(
        self,
        *,
        instruction: str | Problem,
        password: str | Problem,
        probe: object,
    ) -> tuple[int, str, EvalHost]:
        host = EvalHost()
        with (
            patch.object(
                command.engine,
                "resolve_engine_target",
                return_value=command.engine.EngineTarget("fixture-profile", "fixture-model", 1000),
            ),
            patch.object(command.window, "window_judgement", return_value=command.window.WindowJudgement(True, "fixture quiet window", NOW)),
            patch.object(command.access, "load_general_instruction", return_value=instruction),
            patch.object(command.access, "read_eval_password", return_value=password),
            patch.object(command.door, "probe", return_value=probe),
            patch.object(command, "_run_slice", side_effect=AssertionError("runner must not start")),
        ):
            code, stdout, _stderr = _invoke(
                ["eval", "run", "--slice", "guardrails"], **_run_kwargs(host)
            )
        return code, stdout, host

    def test_instruction_password_and_door_refusals_keep_their_problem_and_fix(self) -> None:
        problem = Problem("fixture precondition failed", "Repair the fixture, then retry.")
        scenarios = (
            (problem, "unused", None),
            ("rendered instruction", problem, None),
            ("rendered instruction", "fixture password", command.door.ProbeResult(False, problem.problem, problem)),
        )
        for instruction, password, probe in scenarios:
            with self.subTest(failed=type(instruction if isinstance(instruction, Problem) else password).__name__):
                code, stdout, host = self._invoke_with_preconditions(
                    instruction=instruction,
                    password=password,
                    probe=probe,
                )
                self.assertEqual(code, 1)
                self.assertEqual(stdout.count("preconditions: refuse"), 1)
                self.assertIn(problem.problem, stdout)
                self.assertIn(problem.fix, stdout)
                self.assertFalse(any(argv[0] == "docker" for argv, _input in host.calls))

    def test_set_run_orders_probe_before_runner_and_skips_recording(self) -> None:
        events: list[str] = []
        contexts: list[RunContext] = []
        host = EvalHost()
        committed = load_set(ROOT / SET_ROOT).loaded
        assert committed is not None
        active_id = next(case_id for case_id in committed.active_ids if case_id.startswith("deadline-trap/"))
        result = SliceResult(True, "fixture runner report\n", (CaseResult(active_id, 1, "pass", {}),))
        with tempfile.TemporaryDirectory() as directory:
            copied_set = Path(directory) / SET_ROOT.name
            shutil.copytree(ROOT / SET_ROOT, copied_set)

            def runner(
                _spec: object,
                _loaded: LoadedSet,
                _slice_name: str,
                context: RunContext,
            ) -> SliceResult:
                events.append("runner")
                contexts.append(context)
                return result

            def load_instruction(*_args: object, **_kwargs: object) -> str:
                events.append("instruction")
                return "fixture instruction"

            def read_password(*_args: object) -> str:
                events.append("password")
                return "fixture password"

            def probe_door(*_args: object, **_kwargs: object) -> command.door.ProbeResult:
                events.append("probe")
                return command.door.ProbeResult(True, "fixture door", None)

            with (
                patch.object(
                    command.engine,
                    "resolve_engine_target",
                    return_value=command.engine.EngineTarget("fixture-profile", "fixture-model", 1000),
                ),
                patch.object(command.window, "window_judgement", return_value=command.window.WindowJudgement(True, "fixture quiet window", NOW)),
                patch.object(command.access, "load_general_instruction", side_effect=load_instruction),
                patch.object(command.access, "read_eval_password", side_effect=read_password),
                patch.object(command.access, "make_client_factory", return_value=cast(object, lambda **_kwargs: object())),
                patch.object(command.run, "new_sentinel", return_value=SENTINEL),
                patch.object(command.door, "probe", side_effect=probe_door),
                patch.object(command, "_run_slice", side_effect=runner),
            ):
                code, stdout, stderr = _invoke(
                    ["eval", "run", "--slice", "guardrails", "--set", str(copied_set)],
                    **_run_kwargs(host),
                )
        self.assertEqual(code, 0, stdout + stderr)
        self.assertEqual(events, ["instruction", "password", "probe", "runner"])
        assert contexts[0].turns is not None
        self.assertEqual(contexts[0].turns.instruction, "fixture instruction")
        self.assertEqual(contexts[0].turns.password, "fixture password")
        self.assertEqual(contexts[0].turns.sentinel, SENTINEL)
        self.assertIn("instruction rendered, eval password read, door probed", stdout)
        preconditions = next(
            line for line in stdout.splitlines() if line.startswith("preconditions:")
        )
        self.assertIn("prompt false-refusal@1", preconditions)
        self.assertIn("record: ok — skipped", stdout)
        self.assertTrue(any(line.startswith("reference: ") for line in stdout.splitlines()))
        self.assertFalse(any(argv[0] == "docker" for argv, _input in host.calls))

    def test_release_run_records_result_rows_and_reaches_reference_comparison(self) -> None:
        host = EvalHost()
        committed = load_set(ROOT / SET_ROOT).loaded
        assert committed is not None
        active_id = next(case_id for case_id in committed.active_ids if case_id.startswith("deadline-trap/"))
        tier_2_ids = committed.slice_lists["guardrails"][TIER_2_CATEGORY]
        recorded = SliceResult(
            True,
            "fixture runner report\n",
            tuple(
                CaseResult(case_id, 1, "pass", {})
                for case_id in (active_id, *tier_2_ids)
            ),
        )
        with (
            patch.object(
                command.engine,
                "resolve_engine_target",
                return_value=command.engine.EngineTarget("fixture-profile", "fixture-model", 1000),
            ),
            patch.object(command.window, "window_judgement", return_value=command.window.WindowJudgement(True, "fixture quiet window", NOW)),
            patch.object(command.access, "load_general_instruction", return_value="fixture instruction"),
            patch.object(command.access, "read_eval_password", return_value="fixture password"),
            patch.object(command.access, "make_client_factory", return_value=cast(object, lambda **_kwargs: object())),
            patch.object(command.run, "new_sentinel", return_value=SENTINEL),
            patch.object(command.door, "probe", return_value=command.door.ProbeResult(True, "fixture door", None)),
            patch.object(command, "_run_slice", return_value=recorded),
            patch.object(command, "_compare_reference", wraps=command._compare_reference) as compare,
        ):
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "guardrails"], **_run_kwargs(host)
            )
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("record: ok — run", stdout)
        self.assertTrue(any(line.startswith("reference: ") for line in stdout.splitlines()))
        compare.assert_called_once()
        write_sql = next(
            input_text
            for argv, input_text in host.calls
            if argv[0] == "docker" and input_text != "SELECT 1;\n"
        )
        recorded_sql = cast(str, write_sql)
        self.assertIn(active_id, recorded_sql)
        for case_id in tier_2_ids:
            self.assertIn(case_id, recorded_sql)
        self.assertEqual(recorded_sql.count("INSERT INTO eval_results"), len(recorded.results))
