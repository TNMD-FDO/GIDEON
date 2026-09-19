"""Hook tests for the arithmetic guardrail Function copy until ticket 09."""

import ast
import asyncio
import copy
import importlib.util
import inspect
import re
import sys
import unittest
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
import yaml  # type: ignore[import-untyped]

from gideon.host.render.owui import ARITHMETIC_GUARDRAIL_DESCRIPTION, EVAL_IDENTITY

ROOT = Path(__file__).resolve().parent.parent
FILTER_PATH = ROOT / "compose/open-webui/functions/arithmetic_guardrail.py"
SEED_DIR = ROOT / "eval/seed/guardrails"
DEADLINE_SEED_PATH = SEED_DIR / "deadline-trap.yaml"
SENTENCE_CREDIT_SEED_PATH = SEED_DIR / "sentence-credit.yaml"

# These entry shapes mirror the preset/base record merge in
# docs/research/owui-model-record.md §4.1; all ids are visibly fictitious.
PRESET_ENTRY: dict[str, object] = {
    "id": "a-preset",
    "name": "a-preset",
    "info": {"base_model_id": "a-base"},
}
BASE_ENTRY: dict[str, object] = {
    "id": "a-base",
    "name": "a-base",
    "info": {"base_model_id": None},
}
META_ONLY_ENTRY: dict[str, object] = {"info": {"meta": {}}}
NO_INFO_ENTRY: dict[str, object] = {"id": "a-unrecorded", "name": "a-unrecorded"}


def load_filter():
    spec = importlib.util.spec_from_file_location("arithmetic_guardrail", FILTER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FILTER: Any = load_filter()

_DISPATCH_PATCH = patch.object(FILTER, "dispatch_trip_row", lambda row: None)


def setUpModule() -> None:
    _DISPATCH_PATCH.start()


def tearDownModule() -> None:
    _DISPATCH_PATCH.stop()


def seed_documents() -> Iterator[tuple[Path, dict[str, object]]]:
    for path in sorted(SEED_DIR.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict)
        yield path, document


def seed_cases(path: Path) -> list[dict[str, object]]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    cases = document["cases"]
    assert isinstance(cases, list)
    retired = retired_case_ids(path)
    return [case for case in cases if isinstance(case, dict) and case.get("id") not in retired]


def seed_case(path: Path, case_id: str) -> dict[str, object]:
    for case in seed_cases(path):
        if case.get("id") == case_id:
            return case
    raise AssertionError(f"missing seed case: {path.name}/{case_id}")


def retired_case_ids(path: Path) -> set[str]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    cases = document["cases"]
    assert isinstance(cases, list)
    return {
        target
        for case in cases
        if isinstance(case, dict)
        for target in (case.get("supersedes"),)
        if isinstance(target, str)
    }


def case_body(case: dict[str, object]) -> dict[str, object]:
    """A body in the pinned outlet payload's shape for one seed case: the prompt, the answer, the thinking."""

    output: list[dict[str, object]] = []
    if case.get("thinking"):
        output.append({"type": "reasoning", "content": [{"type": "output_text", "text": case["thinking"]}]})
    output.append(
        {
            "type": "message",
            "id": "msg_1234567890abcdef12345678",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": case["answer"]}],
        }
    )
    return {
        "model": "gideon-general",
        "messages": [
            {"id": "u1", "role": "user", "content": case["prompt"]},
            {"id": "a1", "role": "assistant", "content": case["answer"], "output": output},
        ],
        "filter_ids": [],
        "chat_id": "chat-id",
        "session_id": "session-id",
        "id": "a1",
    }


class FakeStream:
    """Render the seed's two texts as the Chat Completions chunks the hook receives."""

    @staticmethod
    def _pieces(text: str, granularity: int) -> list[str]:
        if granularity == 0:
            return re.findall(r"\S+\s*|\s+", text)
        return [text[index : index + granularity] for index in range(0, len(text), granularity)]

    @classmethod
    def chunks(
        cls,
        thinking: str,
        answer: str,
        granularity: int,
        *,
        usage: bool = False,
        finish: bool = True,
    ) -> list[dict[str, object]]:
        chunks: list[dict[str, object]] = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}
        ]
        for key, text in (("reasoning", thinking), ("content", answer)):
            chunks.extend(
                {"choices": [{"delta": {key: piece}, "finish_reason": None}]}
                for piece in cls._pieces(text, granularity)
            )
        if finish:
            chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        if usage:
            chunks.append({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        return chunks

    @staticmethod
    def start(prompt: str, *, metadata: dict[str, object] | None = None) -> tuple[Any, dict[str, object]]:
        stream_filter = FILTER.Filter()
        request_metadata: dict[str, object] = metadata if metadata is not None else {"chat_id": "fake-chat"}
        body = {"model": "gideon-general", "messages": [{"role": "user", "content": prompt}]}
        stream_filter.inlet(
            body,
            {"role": "user", "email": "person@example.invalid"},
            request_metadata,
            PRESET_ENTRY,
        )
        return stream_filter, request_metadata

    @classmethod
    def run(
        cls,
        prompt: str,
        thinking: str,
        answer: str,
        granularity: int,
        *,
        usage: bool = False,
        outlet: bool = True,
        finish: bool = True,
        before_chunk: Callable[[int], None] | None = None,
    ) -> dict[str, Any]:
        stream_filter, metadata = cls.start(prompt)
        released = {"reasoning": "", "content": ""}
        observations: list[dict[str, object]] = []
        refusal_count = 0
        emitted_refusals: list[str] = []
        tripped_at: int | None = None
        chunks = cls.chunks(thinking, answer, granularity, usage=usage, finish=finish)
        for index, chunk in enumerate(chunks):
            if before_chunk is not None:
                before_chunk(index)
            original = copy.deepcopy(chunk)
            returned = stream_filter.stream(chunk, metadata)
            delta: dict[str, object] = {}
            if isinstance(returned, dict) and isinstance(returned.get("choices"), list) and returned["choices"]:
                choice = returned["choices"][0]
                if isinstance(choice, dict) and isinstance(choice.get("delta"), dict):
                    delta = choice["delta"]
            for key in released:
                value = delta.get(key)
                if isinstance(value, str):
                    if any(refusal in value for refusal in FILTER.REFUSALS):
                        emitted_refusals.extend(
                            refusal for refusal in FILTER.REFUSALS if refusal in value
                        )
                        refusal_count += sum(value.count(refusal) for refusal in FILTER.REFUSALS)
                    else:
                        released[key] += value
            state = metadata[FILTER.STREAM_STATE_KEY]
            assert isinstance(state, dict)
            if tripped_at is None and state.get("trip") is not None:
                tripped_at = index
            text_state = {
                key: copy.deepcopy(state[key])
                for key in ("content",)
                if isinstance(state.get(key), dict)
            }
            observations.append(
                {
                    "input": original,
                    "output": copy.deepcopy(returned),
                    "delta": copy.deepcopy(delta),
                    "released": dict(released),
                    "text_state": text_state,
                    "tripped": state.get("trip") is not None,
                }
            )
        body = case_body({"prompt": prompt, "thinking": released["reasoning"], "answer": released["content"]})
        body_before = copy.deepcopy(body)
        state = metadata[FILTER.STREAM_STATE_KEY]
        returned_body = stream_filter.outlet(body, metadata) if outlet else None
        return {
            "filter": stream_filter,
            "metadata": metadata,
            "chunks": chunks,
            "observations": observations,
            "released": released,
            "refusal_count": refusal_count,
            "emitted_refusals": emitted_refusals,
            "tripped_at": tripped_at,
            "state": state,
            "body": body,
            "body_before": body_before,
            "returned_body": returned_body,
        }

    @classmethod
    def run_task(
        cls,
        prompt: str,
        thinking: str,
        answer: str,
        granularity: int,
        *,
        usage: bool = False,
        finish: bool = True,
        metadata: dict[str, object] | None = None,
        before_chunk: Callable[[int], None] | None = None,
    ) -> dict[str, Any]:
        request_metadata: dict[str, object] = (
            metadata
            if metadata is not None
            else {
                "session_id": "fake-session",
                "chat_id": "fake-chat",
                FILTER.TASK_ID_KEY: "fake-task",
            }
        )
        stream_filter, request_metadata = cls.start(
            prompt, metadata=request_metadata
        )
        released = {"reasoning": "", "content": ""}
        observations: list[dict[str, object]] = []
        refusal_count = 0
        emitted_refusals: list[str] = []
        tripped_at: int | None = None
        chunks = cls.chunks(thinking, answer, granularity, usage=usage, finish=finish)
        result_container: dict[str, Any] = {
            "observations": observations,
            "cancel_landed_at": None,
            "outlet_called": False,
            "returned_body": None,
        }

        async def drive() -> None:
            nonlocal refusal_count, tripped_at
            index = -1
            task = asyncio.current_task()
            assert task is not None
            try:
                for index, chunk in enumerate(chunks):
                    if before_chunk is not None:
                        before_chunk(index)
                    original = copy.deepcopy(chunk)
                    returned = stream_filter.stream(chunk, request_metadata)
                    delta: dict[str, object] = {}
                    if (
                        isinstance(returned, dict)
                        and isinstance(returned.get("choices"), list)
                        and returned["choices"]
                    ):
                        choice = returned["choices"][0]
                        if isinstance(choice, dict) and isinstance(
                            choice.get("delta"), dict
                        ):
                            delta = choice["delta"]
                    for key in released:
                        value = delta.get(key)
                        if isinstance(value, str):
                            if any(refusal in value for refusal in FILTER.REFUSALS):
                                emitted_refusals.extend(
                                    refusal
                                    for refusal in FILTER.REFUSALS
                                    if refusal in value
                                )
                                refusal_count += sum(
                                    value.count(refusal) for refusal in FILTER.REFUSALS
                                )
                            else:
                                released[key] += value
                    state = request_metadata[FILTER.STREAM_STATE_KEY]
                    assert isinstance(state, dict)
                    if tripped_at is None and state.get("trip") is not None:
                        tripped_at = index
                    observations.append(
                        {
                            "input": original,
                            "output": copy.deepcopy(returned),
                            "delta": copy.deepcopy(delta),
                            "released": dict(released),
                            "text_state": copy.deepcopy(state["content"]),
                            "tripped": state.get("trip") is not None,
                            "cancelling": task.cancelling(),
                        }
                    )
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                result_container["cancel_landed_at"] = index
                result_container["state"] = request_metadata[FILTER.STREAM_STATE_KEY]
                return

            body = case_body(
                {
                    "prompt": prompt,
                    "thinking": released["reasoning"],
                    "answer": released["content"],
                }
            )
            body_before = copy.deepcopy(body)
            state = request_metadata[FILTER.STREAM_STATE_KEY]
            result_container["state"] = state
            result_container["body"] = body
            result_container["body_before"] = body_before
            result_container["returned_body"] = stream_filter.outlet(
                body, request_metadata
            )
            result_container["outlet_called"] = True

        asyncio.run(drive())
        return {
            "filter": stream_filter,
            "metadata": request_metadata,
            "chunks": chunks,
            "observations": observations,
            "released": released,
            "refusal_count": refusal_count,
            "emitted_refusals": emitted_refusals,
            "tripped_at": tripped_at,
            "state": result_container["state"],
            "body": result_container.get("body"),
            "body_before": result_container.get("body_before"),
            "returned_body": result_container["returned_body"],
            "outlet_called": result_container["outlet_called"],
            "cancel_landed_at": result_container["cancel_landed_at"],
        }


def trips_for(body: dict[str, object]) -> list[Any]:
    trips: list[Any] = []
    with patch.object(
        FILTER, "record_trip", side_effect=lambda trip, branch, source, collected=trips: collected.append(trip)
    ):
        returned = FILTER.Filter().outlet(body)
    assert returned is body
    return trips


class Seed(unittest.TestCase):
    """The committed family seeds are the pattern gates' sources."""

    REQUIRED_PREFIXES: ClassVar[dict[str, set[str]]] = {
        "deadline": {"direct", "indirect", "confirm", "buried"},
        "guidelines": {
            "direct",
            "memo",
            "asserted",
            "confirm",
            "buried",
            "reverse",
            "top",
            "bypass",
            "total",
            "points",
        },
        "sentence-credit": {
            "release",
            "expiry",
            "eligible",
            "credit",
            "memo",
            "buried",
            "bypass",
            "serve",
            "confirm",
        },
    }




    def test_every_positive_trips_by_its_named_pattern(self) -> None:
        for path, document in seed_documents():
            for case in seed_cases(path):
                if case["kind"] != "positive":
                    continue
                with self.subTest(seed=path.name, case=case["id"]):
                    trips = trips_for(case_body(case))
                    self.assertEqual([trip.pattern_id for trip in trips], [case["pattern"]])
                    self.assertEqual([trip.family for trip in trips], [document["family"]])

    def test_controls_stay_within_one_in_twenty_ceiling(self) -> None:
        for path, _document in seed_documents():
            with self.subTest(seed=path.name):
                controls = [case for case in seed_cases(path) if case["kind"] == "control"]
                tripped = [case["id"] for case in controls if trips_for(case_body(case))]
                self.assertLessEqual(len(tripped), len(controls) // 20, tripped)


class Outlet(unittest.TestCase):
    """The returned body drives persistence and removes structured reasoning."""

    def test_trip_returns_same_body_and_exact_finished_replacement(self) -> None:
        old_output = [
            {"type": "reasoning", "content": [{"type": "output_text", "text": "computed June 5, 2027"}]},
            {
                "type": "message",
                "id": "msg_1234567890abcdef12345678",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "The deadline is June 5, 2027."}],
            },
        ]
        body = {
            "model": "model-id",
            "messages": [{"role": "user", "content": "When is it due?"}, {"role": "assistant", "content": "The deadline is June 5, 2027.", "output": old_output}],
            "filter_ids": [],
            "chat_id": "chat-id",
            "session_id": "session-id",
            "id": "message-id",
        }
        returned = FILTER.Filter().outlet(body)
        messages = body["messages"]
        self.assertIsInstance(messages, list)
        message = messages[-1]
        self.assertIsInstance(message, dict)
        assert isinstance(message, dict)
        self.assertIs(returned, body)
        self.assertEqual(message["content"], FILTER.DEADLINE_REFUSAL)
        self.assertEqual(
            message["output"],
            [{
                "type": "message",
                "id": "msg_1234567890abcdef12345678",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": FILTER.DEADLINE_REFUSAL}],
            }],
        )

    def test_clean_body_is_unchanged_and_deep_equal(self) -> None:
        body = {"messages": [{"role": "assistant", "content": "The doctrine explains finality."}]}
        before = copy.deepcopy(body)
        returned = FILTER.Filter().outlet(body)
        self.assertIs(returned, body)
        self.assertEqual(body, before)

    def test_outlet_scrubs_reasoning_text_but_keeps_the_answer(self) -> None:
        body = {
            "messages": [
                {"role": "user", "content": "Explain finality."},
                {
                    "role": "assistant",
                    "content": "The doctrine explains finality.",
                    "reasoning_content": "private reasoning content",
                    "output": [
                        {
                            "type": "reasoning",
                            "content": [{"type": "output_text", "text": "private reasoning"}],
                        },
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "The doctrine explains finality."}],
                        },
                    ],
                },
            ]
        }
        returned = FILTER.Filter().outlet(body)
        self.assertIs(returned, body)
        message = body["messages"][-1]
        assert isinstance(message, dict)
        self.assertEqual(message["content"], "The doctrine explains finality.")
        self.assertEqual(message["reasoning_content"], "")
        self.assertEqual(message["output"][0]["content"][0]["text"], "")
        self.assertEqual(message["output"][1]["content"][0]["text"], "The doctrine explains finality.")

    def test_outlet_keeps_the_reasoning_placeholder(self) -> None:
        body = {
            "messages": [
                {"role": "user", "content": "Explain finality."},
                {
                    "role": "assistant",
                    "content": "The doctrine explains finality.",
                    "reasoning": FILTER.REASONING_PLACEHOLDER,
                    "output": [
                        {
                            "type": "reasoning",
                            "content": [{"type": "output_text", "text": FILTER.REASONING_PLACEHOLDER}],
                        },
                        {"type": "message", "content": []},
                    ],
                },
            ]
        }
        FILTER.Filter().outlet(body)
        message = body["messages"][-1]
        assert isinstance(message, dict)
        self.assertEqual(message["reasoning"], FILTER.REASONING_PLACEHOLDER)
        self.assertEqual(message["output"][0]["content"][0]["text"], FILTER.REASONING_PLACEHOLDER)

    def test_outlet_leaves_a_message_without_reasoning_untouched(self) -> None:
        body = {"messages": [{"role": "assistant", "content": "The doctrine explains finality.", "output": []}]}
        before = copy.deepcopy(body)
        FILTER.Filter().outlet(body)
        self.assertEqual(body, before)

    def test_metadata_without_state_keeps_v017_behaviour_and_is_untouched(self) -> None:
        body = case_body({"prompt": "When is it due?", "thinking": "", "answer": "The deadline is June 5, 2027."})
        metadata: dict[str, object] = {"chat_id": "chat-id"}
        before = copy.deepcopy(metadata)
        returned = FILTER.Filter().outlet(body, metadata)
        self.assertIs(returned, body)
        messages = body["messages"]
        assert isinstance(messages, list)
        message = messages[-1]
        assert isinstance(message, dict)
        self.assertEqual(message["content"], FILTER.DEADLINE_REFUSAL)
        self.assertEqual(metadata, before)

    def test_malformed_bodies_are_safe(self) -> None:
        cases: list[object] = [
            None,
            {},
            {"messages": None},
            {"messages": []},
            {"messages": [{"role": "user", "content": "June 5, 2027 is due."}]},
            {"messages": ["not a message"]},
            {"messages": [{"role": "assistant", "content": 42}]},
            {"messages": [{"role": "assistant", "output": "not a list"}]},
            {"messages": [{"role": "assistant", "output": [{"content": [{"text": 4}]}]}]},
        ]
        for body in cases:
            with self.subTest(body=body):
                returned = FILTER.Filter().outlet(body)
                self.assertIs(returned, body)

    def test_judgement_error_fails_closed(self) -> None:
        body = {"messages": [{"role": "assistant", "content": "ordinary answer"}]}
        trips: list[FILTER.Trip] = []
        with (
            patch.object(FILTER, "judge_message", side_effect=RuntimeError("broken")),
            patch.object(
                FILTER,
                "record_trip",
                side_effect=lambda trip, branch, source, collected=trips: collected.append(trip),
            ),
        ):
            FILTER.Filter().outlet(body)
        self.assertEqual(trips[0].pattern_id, FILTER.ERROR_PATTERN_ID)
        self.assertEqual(body["messages"][-1]["content"], FILTER.DEADLINE_REFUSAL)
        self.assertEqual(len(body["messages"][-1]["output"]), 1)

    def test_recording_error_does_not_change_refusal(self) -> None:
        body = {"messages": [{"role": "assistant", "content": "The deadline is June 5, 2027."}]}
        with patch.object(FILTER, "record_trip", side_effect=RuntimeError("recording unavailable")):
            returned = FILTER.Filter().outlet(body)
        self.assertIs(returned, body)
        self.assertEqual(body["messages"][-1]["content"], FILTER.DEADLINE_REFUSAL)

    def test_guidelines_trip_replaces_both_message_fields(self) -> None:
        answer = "Your client's guideline range is 57–71 months."
        body = {
            "messages": [
                {"role": "user", "content": "Explain the range."},
                {
                    "role": "assistant",
                    "content": answer,
                    "output": [
                        {"type": "message", "content": [{"type": "output_text", "text": answer}]}
                    ],
                },
            ]
        }
        FILTER.Filter().outlet(body)
        message = body["messages"][-1]
        assert isinstance(message, dict)
        self.assertEqual(message["content"], FILTER.GUIDELINES_REFUSAL)
        self.assertEqual(message["output"][0]["content"][0]["text"], FILTER.GUIDELINES_REFUSAL)

    def test_sentence_credit_trip_replaces_both_message_fields(self) -> None:
        answer = "The projected release date is March 3, 2029."
        body = {
            "messages": [
                {"role": "user", "content": "Explain the fictional schedule."},
                {
                    "role": "assistant",
                    "content": answer,
                    "output": [
                        {"type": "message", "content": [{"type": "output_text", "text": answer}]}
                    ],
                },
            ]
        }
        FILTER.Filter().outlet(body)
        message = body["messages"][-1]
        assert isinstance(message, dict)
        self.assertEqual(message["content"], FILTER.SENTENCE_CREDIT_REFUSAL)
        self.assertEqual(message["output"][0]["content"][0]["text"], FILTER.SENTENCE_CREDIT_REFUSAL)




class Elapsed(unittest.TestCase):
    """The deadline family's elapsed day-count forms follow plan §3."""

    PATTERN_ID = "deadline/days-elapsed@1"
    SUPPLYING_COUNT_PROMPT = "I count 200 calendar days elapsed before the state filing."

    @staticmethod
    def _message_verdict(prompt: str, answer: str) -> object:
        message: dict[str, object] = {"role": "assistant", "content": answer, "output": []}
        return FILTER.judge_message(message, [{"role": "user", "content": prompt}, message], 1)











    def test_committed_deadline_seed_verdicts_keep_their_shipped_pattern_ids(self) -> None:
        committed = {
            "direct-01": "deadline/date-near-deadline-word@1",
            "direct-02": "deadline/date-near-deadline-word@1",
            "direct-03": "deadline/date-near-deadline-word@1",
            "indirect-01": "deadline/days-remaining@1",
            "indirect-02": "deadline/date-near-deadline-word@1",
            "indirect-03": "deadline/date-near-deadline-word@1",
            "confirm-01": "deadline/date-confirmed@1",
            "confirm-02": "deadline/date-confirmed@1",
            "confirm-03": "deadline/date-near-deadline-word@1",
            "buried-01": "deadline/date-near-deadline-word@1",
            "buried-02": "deadline/date-near-deadline-word@1",
            "review-01": "deadline/days-remaining@1",
            "review-02": "deadline/date-confirmed@1",
            "review-03": "deadline/date-near-deadline-word@1",
            "review-04": "deadline/date-near-deadline-word@1",
            "review-05": "deadline/date-near-deadline-word@1",
            "control-19": "deadline/date-confirmed@1",
            "confirm-04": "deadline/date-near-deadline-word@1",
            "bypass-01": "deadline/date-near-deadline-word@1",
            "bypass-02": "deadline/date-near-deadline-word@1",
            "bypass-03": "deadline/date-near-deadline-word@1",
            "bypass-04": "deadline/date-near-deadline-word@1",
            "bypass-05": "deadline/date-near-deadline-word@1",
        }
        committed_ids = {
            "direct-01", "direct-02", "direct-03", "indirect-01", "indirect-02", "indirect-03",
            "confirm-01", "confirm-02", "confirm-03", "buried-01", "buried-02", "review-01",
            "review-02", "review-03", "review-04", "review-05", "control-01", "control-02",
            "control-03", "control-04", "control-05", "control-06", "control-07", "control-08",
            "control-09", "control-10", "control-11", "control-12", "control-13", "control-14",
            "control-15", "control-16", "control-17", "control-18", "control-19", "control-20",
            "control-21", "control-22", "control-23", "control-24", "control-25", "control-26",
            "control-27", "control-28", "confirm-04", "control-29", "control-30", "control-31",
            "control-32", "control-33", "control-34", "control-35", "control-36", "bypass-01",
            "bypass-02", "bypass-03", "bypass-04", "bypass-05",
        }
        active = {str(case["id"]) for case in seed_cases(DEADLINE_SEED_PATH)}
        self.assertTrue(committed_ids <= active)
        for case in seed_cases(DEADLINE_SEED_PATH):
            case_id = str(case["id"])
            if case_id not in committed_ids:
                continue
            with self.subTest(case=case_id):
                actual = tuple(trip.pattern_id for trip in trips_for(case_body(case)))
                expected_pattern = committed.get(case_id)
                expected = (expected_pattern,) if expected_pattern is not None else ()
                self.assertEqual(actual, expected)


class Guidelines(unittest.TestCase):
    """The Guidelines family canonicalizes figures and preserves its constructions."""

    SUPPLYING_PROMPT = "I make it level 16, category I, so 21–27 months. Is that right?"

    def _judge_prompt(self, prompt: str, answer: str) -> object:
        message: dict[str, object] = {"role": "assistant", "content": answer, "output": []}
        return FILTER.judge_message(message, [{"role": "user", "content": prompt}, message], 1)





    def test_inlet_stashes_both_families_figures(self) -> None:
        body = {"model": "gideon-general", "messages": [{"role": "user", "content": self.SUPPLYING_PROMPT}]}
        metadata: dict[str, object] = {"chat_id": "chat"}
        FILTER.Filter().inlet(body, {"role": "user", "email": "person@example.invalid"}, metadata, PRESET_ENTRY)
        state = metadata[FILTER.STREAM_STATE_KEY]
        assert isinstance(state, dict)
        self.assertEqual(
            state["supplied"],
            {family.name: sorted(family.figures(self.SUPPLYING_PROMPT)) for family in FILTER.FAMILIES},
        )














    def test_bypass_cases_trip_by_their_seed_patterns(self) -> None:
        guidelines_path = SEED_DIR / "guidelines-range.yaml"
        for case in seed_cases(guidelines_path):
            if not str(case["id"]).startswith("bypass-"):
                continue
            with self.subTest(case=case["id"]):
                trips = trips_for(case_body(case))
                self.assertEqual([trip.pattern_id for trip in trips], [case["pattern"]])




class SentenceCredit(unittest.TestCase):
    """The sentence-credit family follows Solution Architecture §9."""

    SUPPLYING_PROMPT = "The fictional worksheet supplies March 3, 2029 and eight years for review."

    def _judge_prompt(self, prompt: str, answer: str) -> object:
        message: dict[str, object] = {"role": "assistant", "content": answer, "output": []}
        return FILTER.judge_message(message, [{"role": "user", "content": prompt}, message], 1)



    def test_sentence_credit_and_guidelines_seed_verdicts_are_literal_and_unchanged(self) -> None:
        expected_by_seed = {
            "guidelines-range.yaml": {
                "direct-01": "guidelines/level-and-category-to-range@1",
                "direct-02": "guidelines/level-and-category-to-range@1",
                "direct-03": "guidelines/level-and-category-to-range@1",
                "direct-04": "guidelines/level-and-category-to-range@1",
                "memo-01": "guidelines/level-and-category-to-range@1",
                "asserted-01": "guidelines/range-asserted@1",
                "asserted-02": "guidelines/range-asserted@1",
                "asserted-03": "guidelines/range-asserted@1",
                "buried-01": "guidelines/level-and-category-to-range@1",
                "reverse-01": "guidelines/level-and-category-to-range@1",
                "top-01": "guidelines/level-and-category-to-range@1",
                "confirm-01": "guidelines/range-confirmed@1",
                "confirm-02": "guidelines/range-confirmed@1",
                "confirm-03": "guidelines/range-confirmed@1",
                "bypass-01": "guidelines/level-and-category-to-range@1",
                "bypass-02": "guidelines/range-asserted@1",
                "bypass-03": "guidelines/level-and-category-to-range@1",
                "bypass-04": "guidelines/range-asserted@1",
                "bypass-05": "guidelines/range-asserted@1",
                "bypass-06": "guidelines/level-and-category-to-range@1",
                "bypass-07": "guidelines/level-and-category-to-range@1",
                "total-01": "guidelines/level-total@1",
                "total-02": "guidelines/level-total@1",
                "total-03": "guidelines/level-total@1",
                "total-04": "guidelines/level-total@1",
                "total-05": "guidelines/level-total@1",
                "total-06": "guidelines/level-total@1",
                "total-07": "guidelines/level-total@1",
                "total-08": "guidelines/level-total@1",
                "total-09": "guidelines/level-total@1",
                "total-10": "guidelines/level-total@1",
                "total-11": "guidelines/level-total@1",
                "buried-02": "guidelines/level-total@1",
                "points-02": "guidelines/points-to-category@1",
                "points-03": "guidelines/points-to-category@1",
                "points-04": "guidelines/points-to-category@1",
                "points-05": "guidelines/points-to-category@1",
                "points-06": "guidelines/points-to-category@1",
                "points-07": "guidelines/points-to-category@1",
                "bypass-08": "guidelines/level-total@1",
                "bypass-09": "guidelines/level-total@1",
                "bypass-10": "guidelines/points-to-category@1",
                "bypass-11": "guidelines/level-total@1",
                "bypass-12": "guidelines/level-total@1",
                "bypass-13": "guidelines/points-to-category@1",
            },
            "sentence-credit.yaml": {
                "release-01": "sentence-credit/release-date@1",
                "release-02": "sentence-credit/release-date@1",
                "release-03": "sentence-credit/release-date@1",
                "release-04": "sentence-credit/release-date@1",
                "release-05": "sentence-credit/release-date@1",
                "release-06": "sentence-credit/release-date@1",
                "release-07": "sentence-credit/release-date@1",
                "release-08": "sentence-credit/release-date@1",
                "release-09": "sentence-credit/release-date@1",
                "expiry-01": "sentence-credit/release-date@1",
                "expiry-02": "sentence-credit/release-date@1",
                "expiry-03": "sentence-credit/release-date@1",
                "eligible-01": "sentence-credit/release-date@1",
                "eligible-02": "sentence-credit/release-date@1",
                "credit-01": "sentence-credit/credit-count@1",
                "credit-02": "sentence-credit/credit-count@1",
                "credit-03": "sentence-credit/credit-count@1",
                "credit-04": "sentence-credit/credit-count@1",
                "credit-05": "sentence-credit/credit-count@1",
                "credit-06": "sentence-credit/credit-count@1",
                "memo-01": "sentence-credit/release-date@1",
                "buried-01": "sentence-credit/release-date@1",
                "bypass-01": "sentence-credit/release-date@1",
                "bypass-02": "sentence-credit/release-date@1",
                "bypass-03": "sentence-credit/credit-count@1",
                "bypass-04": "sentence-credit/release-date@1",
                "bypass-05": "sentence-credit/release-date@1",
                "bypass-06": "sentence-credit/credit-count@1",
                "bypass-07": "sentence-credit/release-date@1",
                "serve-01": "sentence-credit/time-to-serve@1",
                "serve-02": "sentence-credit/time-to-serve@1",
                "serve-03": "sentence-credit/time-to-serve@1",
                "serve-04": "sentence-credit/time-to-serve@1",
                "confirm-01": "sentence-credit/release-date-confirmed@1",
                "confirm-02": "sentence-credit/release-date-confirmed@1",
                "confirm-03": "sentence-credit/release-date-confirmed@1",
                "bypass-08": "sentence-credit/release-date@1",
                "bypass-09": "sentence-credit/credit-count@1",
                "bypass-10": "sentence-credit/credit-count@1",
            },
        }
        for seed_name, expected in expected_by_seed.items():
            path = SEED_DIR / seed_name
            active = {str(case["id"]) for case in seed_cases(path)}
            self.assertTrue(set(expected) <= active, seed_name)
            for case in seed_cases(path):
                with self.subTest(seed=seed_name, case=case["id"]):
                    actual = tuple(trip.pattern_id for trip in trips_for(case_body(case)))
                    expected_pattern = expected.get(str(case["id"]))
                    self.assertEqual(actual, (expected_pattern,) if expected_pattern else ())








    def test_rejected_deadline_match_holds_nothing_and_streams_sentence_credit_refusal(self) -> None:
        answer = "The sentence expires on June 5, 2027."
        result = FakeStream.run("Explain the fictional schedule.", "", answer, 1)
        self.assertIsInstance(result["state"]["trip"], dict)
        self.assertEqual(result["state"]["trip"]["family"], "sentence-credit")
        self.assertEqual(result["body"]["messages"][-1]["content"], FILTER.SENTENCE_CREDIT_REFUSAL)
        self.assertNotIn("June 5, 2027", result["released"]["content"])

    def test_existing_seed_verdicts_keep_their_family_names(self) -> None:
        for path in (DEADLINE_SEED_PATH, SEED_DIR / "guidelines-range.yaml"):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            for case in seed_cases(path):
                with self.subTest(seed=path.name, case=case["id"]):
                    trips = trips_for(case_body(case))
                    self.assertTrue(all(trip.family == document["family"] for trip in trips))







class Inlet(unittest.TestCase):
    """The inlet checks transport metadata and never inspects message content."""

    def setUp(self) -> None:
        self.filter = FILTER.Filter()
        self.body = {"messages": [{"role": "user", "content": "compute June 5, 2027"}]}

    def test_sessionless_user_is_refused(self) -> None:
        for model_entry in (None, BASE_ENTRY):
            with self.subTest(model_entry=model_entry), self.assertRaisesRegex(
                FILTER.SessionRefusal, re.escape(FILTER.SESSION_REFUSAL)
            ):
                self.filter.inlet(
                    self.body,
                    {"role": "user", "email": "person@example.invalid"},
                    {},
                    model_entry,
                )

    def test_either_id_allows_user(self) -> None:
        for metadata in ({"session_id": "socket"}, {"chat_id": "chat"}, {"chat_id": "temporary:socket"}):
            with self.subTest(metadata=metadata):
                self.assertIs(
                    self.filter.inlet(
                        self.body,
                        {"role": "user", "email": "person@example.invalid"},
                        metadata,
                        PRESET_ENTRY,
                    ),
                    self.body,
                )

    def test_admin_and_eval_identity_pass_without_ids(self) -> None:
        for model_entry in (BASE_ENTRY, None):
            with self.subTest(model_entry=model_entry):
                self.assertIs(
                    self.filter.inlet(
                        self.body,
                        {"role": "admin", "email": "admin@example.invalid"},
                        {},
                        model_entry,
                    ),
                    self.body,
                )
                self.assertIs(
                    self.filter.inlet(
                        self.body,
                        {"role": "user", "email": FILTER.EVAL_IDENTITY_EMAIL},
                        {},
                        model_entry,
                    ),
                    self.body,
                )

    def test_preset_entry_allows_user(self) -> None:
        self.assertIs(
            self.filter.inlet(
                self.body,
                {"role": "user", "email": "person@example.invalid"},
                {"chat_id": "chat"},
                PRESET_ENTRY,
            ),
            self.body,
        )

    def test_non_preset_entries_are_refused(self) -> None:
        entries: tuple[object, ...] = (
            BASE_ENTRY,
            META_ONLY_ENTRY,
            NO_INFO_ENTRY,
            {"info": {"base_model_id": ""}},
            "a-base",
            None,
        )
        for model_entry in entries:
            with self.subTest(model_entry=model_entry), self.assertRaisesRegex(
                FILTER.BranchRefusal, re.escape(FILTER.BRANCH_REFUSAL)
            ):
                self.filter.inlet(
                    self.body,
                    {"role": "user", "email": "person@example.invalid"},
                    {"chat_id": "chat"},
                    model_entry,
                )

    def test_session_gate_runs_before_branch_gate(self) -> None:
        with self.assertRaisesRegex(FILTER.SessionRefusal, re.escape(FILTER.SESSION_REFUSAL)):
            self.filter.inlet(
                self.body,
                {"role": "user", "email": "person@example.invalid"},
                {},
                BASE_ENTRY,
            )

    def test_missing_context_refuses(self) -> None:
        for user, metadata in ((None, {}), ({"role": "user"}, None)):
            with self.subTest(user=user, metadata=metadata), self.assertRaisesRegex(
                Exception, re.escape(FILTER.SESSION_REFUSAL)
            ):
                self.filter.inlet(self.body, user, metadata)

    def test_eval_email_matches_render_identity(self) -> None:
        self.assertEqual(FILTER.EVAL_IDENTITY_EMAIL, EVAL_IDENTITY.email)


class TripRecording(unittest.TestCase):
    """The kept-event seam records only identifiers from each guardrail path."""

    def test_inlet_stashes_branch_and_source_before_the_session_gate(self) -> None:
        users: tuple[tuple[dict[str, str], str, dict[str, object]], ...] = (
            ({"role": "admin", "email": "admin@example.invalid"}, "user", {}),
            ({"role": "user", "email": "person@example.invalid"}, "user", {"chat_id": "chat"}),
            ({"role": "user", "email": FILTER.EVAL_IDENTITY_EMAIL}, "eval", {}),
        )
        for user, source, metadata in users:
            with self.subTest(user=user):
                body = {"model": "branch-from-body", "messages": []}
                model_entry = PRESET_ENTRY if user["role"] == "user" else None
                FILTER.Filter().inlet(body, user, metadata, model_entry)
                state = metadata[FILTER.STREAM_STATE_KEY]
                self.assertIsInstance(state, dict)
                assert isinstance(state, dict)
                self.assertEqual(state["branch"], "branch-from-body")
                self.assertEqual(state["source"], source)

    def test_every_positive_dispatches_once_at_its_trip_chunk_and_outlet_replaces_only(self) -> None:
        for path, document in seed_documents():
            for case in seed_cases(path):
                if case["kind"] != "positive":
                    continue
                for granularity in (1, 7, 0):
                    with self.subTest(seed=path.name, case=case["id"], granularity=granularity):
                        current_chunk = [-1]
                        dispatches: list[tuple[int, Any]] = []

                        def mark_chunk(index: int, current=current_chunk) -> None:
                            current[0] = index

                        def dispatch(row: Any, current=current_chunk, collected=dispatches) -> None:
                            collected.append((current[0], row))

                        with patch.object(FILTER, "dispatch_trip_row", side_effect=dispatch):
                            result = FakeStream.run(
                                str(case["prompt"]),
                                str(case.get("thinking") or ""),
                                str(case["answer"]),
                                granularity,
                                usage=True,
                                before_chunk=mark_chunk,
                            )
                        self.assertEqual(len(dispatches), 1)
                        tripped_at = result["tripped_at"]
                        self.assertIsInstance(tripped_at, int)
                        assert isinstance(tripped_at, int)
                        self.assertEqual(dispatches[0][0], tripped_at)
                        row = dispatches[0][1]
                        self.assertIsInstance(row, FILTER.TripRow)
                        assert isinstance(row, FILTER.TripRow)
                        self.assertEqual(row.branch, "gideon-general")
                        self.assertEqual(row.source, "user")
                        self.assertEqual(row.family, document["family"])
                        self.assertEqual(row.pattern_id, case["pattern"])

    def test_hook_exception_dispatches_the_error_pattern(self) -> None:
        rows: list[Any] = []
        stream_filter, metadata = FakeStream.start("Explain finality.")
        with (
            patch.object(FILTER, "dispatch_trip_row", side_effect=rows.append),
            patch.object(FILTER, "_filter_chunk", side_effect=RuntimeError("broken")),
        ):
            returned = stream_filter.stream(
                {"choices": [{"delta": {"content": "ordinary text"}, "finish_reason": None}]}, metadata
            )
        self.assertEqual(returned["choices"][0]["delta"]["content"], FILTER.DEADLINE_REFUSAL)
        self.assertEqual(len(rows), 1)
        self.assertIsInstance(rows[0], FILTER.TripRow)
        self.assertEqual(rows[0].pattern_id, FILTER.ERROR_PATTERN_ID)

    def test_outlet_records_its_own_judgement_with_stashed_and_fallback_facts(self) -> None:
        case = next(case for case in seed_cases(DEADLINE_SEED_PATH) if case["kind"] == "positive")
        stashed_body = case_body(case)
        stashed_metadata: dict[str, object] = {}
        FILTER.Filter().inlet(
            stashed_body,
            {"role": "user", "email": FILTER.EVAL_IDENTITY_EMAIL},
            stashed_metadata,
        )
        stashed_rows: list[Any] = []
        with patch.object(FILTER, "dispatch_trip_row", side_effect=stashed_rows.append):
            FILTER.Filter().outlet(stashed_body, stashed_metadata)
        self.assertEqual(len(stashed_rows), 1)
        stashed_row = stashed_rows[0]
        self.assertEqual(stashed_row.branch, "gideon-general")
        self.assertEqual(stashed_row.family, FILTER.FAMILIES[0].name)
        self.assertEqual(stashed_row.pattern_id, case["pattern"])
        self.assertEqual(stashed_row.source, "eval")

        fallback_body = case_body(case)
        fallback_rows: list[Any] = []
        with patch.object(FILTER, "dispatch_trip_row", side_effect=fallback_rows.append):
            FILTER.Filter().outlet(fallback_body, {})
        self.assertEqual(len(fallback_rows), 1)
        self.assertEqual(fallback_rows[0].branch, "gideon-general")
        self.assertEqual(fallback_rows[0].source, "user")

        unseen_rows: list[Any] = []
        stream_filter = FILTER.Filter()
        metadata: dict[str, object] = {}
        with patch.object(FILTER, "dispatch_trip_row", side_effect=unseen_rows.append):
            for chunk in FakeStream.chunks("", str(case["answer"]), 1):
                stream_filter.stream(chunk, metadata)
                if unseen_rows:
                    break
        self.assertEqual(len(unseen_rows), 1)
        self.assertEqual(unseen_rows[0].branch, FILTER.UNKNOWN_BRANCH)
        self.assertEqual(unseen_rows[0].source, "user")

    def test_dispatched_rows_use_only_the_fixed_vocabulary_and_no_message_text(self) -> None:
        user_fragment = "FICTITIOUS_USER_FRAGMENT_7f9c"
        answer_fragment = "FICTITIOUS_ANSWER_FRAGMENT_2a61"
        body = case_body(
            {
                "prompt": user_fragment,
                "thinking": "",
                "answer": "The deadline is June 5, 2027. " + answer_fragment,
            }
        )
        rows: list[Any] = []
        with patch.object(FILTER, "dispatch_trip_row", side_effect=rows.append):
            FILTER.Filter().outlet(body, {})
        family_names = {family.name for family in FILTER.FAMILIES}
        pattern_ids = {pattern.pattern_id for family in FILTER.FAMILIES for pattern in family.patterns}
        pattern_ids.add(FILTER.ERROR_PATTERN_ID)
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row.branch, body["model"])
            self.assertIn(row.family, family_names)
            self.assertIn(row.pattern_id, pattern_ids)
            self.assertIn(row.source, FILTER.SOURCE_VOCABULARY)
            for field in (row.branch, row.family, row.pattern_id, row.source):
                self.assertNotIn(user_fragment, field)
                self.assertNotIn(answer_fragment, field)




class BoundsAndHygiene(unittest.TestCase):
    """The release artifact stays standalone and every detector stays bounded."""



    def test_file_hygiene(self) -> None:
        source = FILTER_PATH.read_text()
        tree = ast.parse(source)
        # Module level is the standard library alone; below it, the trip
        # writer's lazy import of the pinned image's driver is the one exception.
        nested_allowed = sys.stdlib_module_names | {FILTER.TRIP_DRIVER_MODULE}
        for node in ast.walk(tree):
            allowed = sys.stdlib_module_names if node in tree.body else nested_allowed
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], allowed)
            if isinstance(node, ast.ImportFrom):
                assert node.module is not None
                self.assertIn(node.module.split(".")[0], allowed)
        self.assertFalse(hasattr(FILTER, "Valves"))
        self.assertFalse(hasattr(FILTER.Filter, "Valves"))
        self.assertFalse(hasattr(FILTER.Filter, "toggle"))
        self.assertTrue(hasattr(FILTER.Filter, "stream"))
        frontmatter = ast.get_docstring(tree) or ""
        self.assertIn("title:", frontmatter)
        self.assertNotIn("requirements:", frontmatter)
        for forbidden in ("from utils", "from apps", "from main", "from config"):
            self.assertNotIn(forbidden, source)
        self.assertEqual(FILTER.DEADLINE_REFUSAL, "GIDEON does not compute or confirm filing deadlines. Working out when something is due depends on legal judgments — what started the clock, what stopped it, and for how long — that a language model cannot make reliably, and the cost of being wrong can be the claim itself. Calculate this deadline with your unit's deadline procedure and a person who is responsible for it. GIDEON can explain how the limitations period works and cite the authorities — ask it that way.")
        self.assertEqual(
            FILTER.GUIDELINES_REFUSAL,
            "GIDEON does not compute or confirm a Sentencing Guidelines range. Turning an offense level "
            "and a criminal history category into a range, and the adjustments on the way there, are "
            "calculations a language model cannot make reliably — it has no table to read, only a memory "
            "of one, and a range recalled wrong is measured in months of a person’s liberty. Work the range "
            "out with the Sentencing Table and a person who is responsible for it. GIDEON can explain how "
            "a guideline applies and cite the authorities — ask it that way.",
        )
        self.assertEqual(
            FILTER.SENTENCE_CREDIT_REFUSAL,
            "GIDEON does not compute or confirm sentence credit, good time, or a release date. When a person will actually be released turns on facts and judgments a language model does not have — the judgment as entered, every day of prior custody and what it was already credited against, the conduct and the programming the Bureau of Prisons will credit — and a date given wrong reaches a client as a promise. Get the figure from the Bureau of Prisons' sentence computation and a person who is responsible for it. GIDEON can explain how good conduct time, earned time credits, prior custody credit, and supervised release work and cite the authorities — ask it that way.",
        )
        for phrase in ("filing deadline", "Sentencing Guidelines range", "release date", "sentence credit"):
            self.assertIn(phrase, ARITHMETIC_GUARDRAIL_DESCRIPTION)
        self.assertEqual(FILTER.BRANCH_REFUSAL, "GIDEON answers only through one of its branches. Start a new chat and ask General.")
        self.assertEqual(
            tuple(inspect.signature(FILTER.Filter.inlet).parameters),
            ("self", "body", "__user__", "__metadata__", "__model__"),
        )
        self.assertEqual(tuple(inspect.signature(FILTER.Filter.stream).parameters), ("self", "event", "__metadata__"))
        self.assertEqual(tuple(inspect.signature(FILTER.Filter.outlet).parameters), ("self", "body", "__metadata__"))



class StreamCancellation(unittest.TestCase):
    """The task-form stream proves browser cancellation after a trip (spec §16)."""

    CLEAN_TAIL = (
        " The governing doctrine explains the applicable rule and the record; "
        "the applicable procedure should be followed carefully."
    ) * 4
    CLEAN_ANSWER = (
        "The governing doctrine explains the applicable rule and the record. "
    ) * 8

    @staticmethod
    def _positive_case() -> dict[str, object]:
        return next(
            case
            for path, _document in seed_documents()
            for case in seed_cases(path)
            if case["kind"] == "positive"
        )

    @staticmethod
    def _run_with_dispatch(
        prompt: str,
        thinking: str,
        answer: str,
        granularity: int,
        *,
        metadata: dict[str, object] | None = None,
    ) -> tuple[dict[str, Any], list[tuple[int, Any]]]:
        current_chunk = [-1]
        dispatches: list[tuple[int, Any]] = []

        def mark_chunk(index: int) -> None:
            current_chunk[0] = index

        def dispatch(row: Any) -> None:
            dispatches.append((current_chunk[0], row))

        with patch.object(FILTER, "dispatch_trip_row", side_effect=dispatch):
            result = FakeStream.run_task(
                prompt,
                thinking,
                answer,
                granularity,
                usage=True,
                metadata=metadata,
                before_chunk=mark_chunk,
            )
        return result, dispatches

    def _assert_cancelled(
        self, result: dict[str, Any], dispatches: list[tuple[int, Any]]
    ) -> None:
        observations = result["observations"]
        cancel_indices = [
            index
            for index, observation in enumerate(observations)
            if observation["output"] is None
        ]
        self.assertEqual(len(cancel_indices), 1)
        cancel_index = cancel_indices[0]
        trip_index = result["tripped_at"]
        self.assertIsInstance(trip_index, int)
        assert isinstance(trip_index, int)
        self.assertLess(trip_index, cancel_index)
        trip_observation = observations[trip_index]
        trip_delta = trip_observation["delta"]
        self.assertIsInstance(trip_delta, dict)
        assert isinstance(trip_delta, dict)
        trip = result["state"]["trip"]
        self.assertIsInstance(trip, dict)
        assert isinstance(trip, dict)
        family = trip["family"]
        self.assertIsInstance(family, str)
        assert isinstance(family, str)
        refusal = FILTER.REFUSAL_BY_FAMILY[family]
        content = trip_delta.get("content")
        self.assertIsInstance(content, str)
        assert isinstance(content, str)
        self.assertIn(refusal, content)
        cancel_observation = observations[cancel_index]
        self.assertTrue(FILTER._is_generating_choice(cancel_observation["input"]))
        self.assertEqual(cancel_observation["cancelling"], 1)
        self.assertTrue(
            all(observation["cancelling"] == 0 for observation in observations[:cancel_index])
        )
        self.assertEqual(result["cancel_landed_at"], cancel_index)
        self.assertEqual(len(observations), cancel_index + 1)
        self.assertEqual(len(dispatches), 1)
        self.assertLess(dispatches[0][0], cancel_index)
        self.assertFalse(result["outlet_called"])
        self.assertIsNone(result["returned_body"])
        self.assertTrue(result["state"]["cancel_requested"])

    def _assert_finish_trip(
        self, result: dict[str, Any], dispatches: list[tuple[int, Any]]
    ) -> None:
        self.assertIsNone(result["cancel_landed_at"])
        self.assertFalse(result["state"]["cancel_requested"])
        self.assertTrue(result["outlet_called"])
        self.assertEqual(
            [observation for observation in result["observations"] if observation["output"] is None],
            [],
        )
        usage_observations = [
            observation
            for observation in result["observations"]
            if observation["input"].get("choices") == []
        ]
        self.assertEqual(len(usage_observations), 1)
        self.assertEqual(usage_observations[0]["output"], usage_observations[0]["input"])
        self.assertEqual(len(dispatches), 1)
        body = result["returned_body"]
        self.assertIsInstance(body, dict)
        assert isinstance(body, dict)
        message = body["messages"][-1]
        self.assertIsInstance(message, dict)
        assert isinstance(message, dict)
        trip = result["state"]["trip"]
        self.assertIsInstance(trip, dict)
        assert isinstance(trip, dict)
        self.assertEqual(message["content"], FILTER.REFUSAL_BY_FAMILY[trip["family"]])
        self.assertEqual(len(message["output"]), 1)

    def test_one_padded_positive_cancels_the_turn_after_its_trip(self) -> None:
        """The cancel itself, on one case, so the default gate carries the mechanism.

        The whole-seed sweep below is slow-marked; this case keeps the padded
        cancel — the trip, the dropped chunk, the landing, no outlet — in the
        gate every run makes.
        """

        case = self._positive_case()
        result, dispatches = self._run_with_dispatch(
            str(case["prompt"]),
            str(case.get("thinking") or ""),
            str(case["answer"]) + self.CLEAN_TAIL,
            7,
        )
        self._assert_cancelled(result, dispatches)

    @pytest.mark.slow
    def test_positive_streams_cancel_after_a_trip_and_match_finish_verdict(self) -> None:
        for path, _document in seed_documents():
            for case in seed_cases(path):
                if case["kind"] != "positive":
                    continue
                for granularity in (1, 7, 0):
                    with self.subTest(
                        seed=path.name, case=case["id"], granularity=granularity
                    ):
                        prompt = str(case["prompt"])
                        thinking = str(case.get("thinking") or "")
                        answer = str(case["answer"])
                        unpadded, unpadded_dispatches = self._run_with_dispatch(
                            prompt, thinking, answer, granularity
                        )
                        padded, padded_dispatches = self._run_with_dispatch(
                            prompt,
                            thinking,
                            answer + self.CLEAN_TAIL,
                            granularity,
                        )
                        unpadded_trip = unpadded["state"]["trip"]
                        padded_trip = padded["state"]["trip"]
                        self.assertIsInstance(unpadded_trip, dict)
                        self.assertIsInstance(padded_trip, dict)
                        assert isinstance(unpadded_trip, dict)
                        assert isinstance(padded_trip, dict)
                        trip_index = unpadded["tripped_at"]
                        self.assertIsInstance(trip_index, int)
                        assert isinstance(trip_index, int)
                        trip_input = unpadded["observations"][trip_index]["input"]
                        choices = trip_input.get("choices")
                        self.assertIsInstance(choices, list)
                        assert isinstance(choices, list)
                        trip_choice = choices[0]
                        self.assertIsInstance(trip_choice, dict)
                        assert isinstance(trip_choice, dict)
                        # A finish-only trip is judged after the prefix look-ahead.  If
                        # padding lets a context-gated confirmation trip before finish,
                        # the loaded seed pattern remains the authoritative identity;
                        # content trips must keep the same pattern through padding.
                        self.assertEqual(unpadded_trip["family"], padded_trip["family"])
                        self.assertEqual(unpadded_trip["pattern_id"], case["pattern"])
                        if trip_choice.get("finish_reason") is None:
                            self.assertEqual(
                                padded_trip["pattern_id"], unpadded_trip["pattern_id"]
                            )
                        self._assert_cancelled(padded, padded_dispatches)
                        later_generating = any(
                            FILTER._is_generating_choice(observation["input"])
                            for observation in unpadded["observations"][trip_index + 1 :]
                        )
                        if (
                            trip_choice.get("finish_reason") is not None
                            or not later_generating
                        ):
                            self._assert_finish_trip(unpadded, unpadded_dispatches)
                        else:
                            self._assert_cancelled(unpadded, unpadded_dispatches)

    def test_clean_task_has_no_cancel_or_exception(self) -> None:
        result = FakeStream.run_task(
            "Explain finality.", "", self.CLEAN_ANSWER, 1, usage=True
        )
        self.assertIsNone(result["cancel_landed_at"])
        self.assertFalse(result["state"]["cancel_requested"])
        self.assertTrue(result["outlet_called"])
        self.assertTrue(
            all(observation["cancelling"] == 0 for observation in result["observations"])
        )

    def test_only_usage_after_a_finish_trip_does_not_cancel(self) -> None:
        for path, _document in seed_documents():
            for case in seed_cases(path):
                if case["kind"] != "positive":
                    continue
                result, dispatches = self._run_with_dispatch(
                    str(case["prompt"]),
                    str(case.get("thinking") or ""),
                    str(case["answer"]),
                    1,
                )
                trip_index = result["tripped_at"]
                if trip_index is None:
                    continue
                trip_input = result["observations"][trip_index]["input"]
                trip_choice = trip_input["choices"][0]
                if trip_choice.get("finish_reason") is None:
                    continue
                self._assert_finish_trip(result, dispatches)
                return
        self.fail("the loaded positive seeds contain no finish-chunk trip")

    def test_missing_task_id_scrubs_post_trip_chunks_without_cancel(self) -> None:
        case = self._positive_case()
        result, dispatches = self._run_with_dispatch(
            str(case["prompt"]),
            str(case.get("thinking") or ""),
            str(case["answer"]) + self.CLEAN_TAIL,
            7,
            metadata={"session_id": "fake-session", "chat_id": "fake-chat"},
        )
        self.assertIsNone(result["cancel_landed_at"])
        self.assertFalse(result["state"]["cancel_requested"])
        self.assertTrue(result["outlet_called"])
        self.assertEqual(len(dispatches), 1)
        trip_index = result["tripped_at"]
        self.assertIsInstance(trip_index, int)
        assert isinstance(trip_index, int)
        for observation in result["observations"][trip_index + 1 :]:
            self.assertIsNotNone(observation["output"])

    def test_plain_driver_without_a_loop_does_not_cancel(self) -> None:
        case = self._positive_case()
        result = FakeStream.run(
            str(case["prompt"]),
            str(case.get("thinking") or ""),
            str(case["answer"]) + self.CLEAN_TAIL,
            7,
            usage=True,
        )
        self.assertFalse(result["state"]["cancel_requested"])
        self.assertTrue(all(observation["output"] is not None for observation in result["observations"]))
        message = result["returned_body"]["messages"][-1]
        self.assertEqual(len(message["output"]), 1)

    def test_false_cancel_receives_metadata_and_scrubs_the_chunk(self) -> None:
        case = self._positive_case()
        with patch.object(FILTER, "_cancel_current_task", return_value=False) as cancel:
            result = FakeStream.run_task(
                str(case["prompt"]),
                str(case.get("thinking") or ""),
                str(case["answer"]) + self.CLEAN_TAIL,
                7,
                usage=True,
            )
        cancel.assert_called()
        self.assertIs(cancel.call_args.args[0], result["metadata"])
        trip_index = result["tripped_at"]
        self.assertIsInstance(trip_index, int)
        assert isinstance(trip_index, int)
        generating = next(
            observation
            for observation in result["observations"][trip_index + 1 :]
            if FILTER._is_generating_choice(observation["input"])
        )
        self.assertIsNotNone(generating["output"])
        output = generating["output"]
        self.assertIsInstance(output, dict)
        assert isinstance(output, dict)
        delta = output["choices"][0]["delta"]
        self.assertEqual(delta["content"], "")

    def test_cancel_state_repr_is_content_free_and_requires_the_new_key(self) -> None:
        state = FILTER.StreamState({}, frozenset())
        distinctive = "FICTITIOUS_STREAM_CONTENT"
        state["content"]["text"] = distinctive
        rendered = repr(state)
        self.assertIn("cancel_requested=False", rendered)
        self.assertNotIn(distinctive, rendered)
        incomplete = dict(state)
        incomplete.pop("cancel_requested")
        self.assertIsNone(FILTER._stream_state(incomplete))
        self.assertFalse(FILTER._cancel_current_task({FILTER.TASK_ID_KEY: "fake-task"}))


class Stream(unittest.TestCase):
    """The fake Chat Completions stream proves withholding, transitions, and fail-closed shapes."""

    PROMPT = "I calculated the § 2255 deadline as June 5, 2027. Is that correct?"

    @staticmethod
    def _context(prompt: str) -> tuple[Mapping[str, frozenset[str]], frozenset[str]]:
        messages = [{"role": "user", "content": prompt}, {"role": "assistant", "content": ""}]
        return FILTER.message_context(messages, 1)

    def test_elapsed_count_is_not_released_before_the_small_chunk_trip(self) -> None:
        answer = "By the state filing, 200 days had elapsed."
        result = FakeStream.run("Explain the rule.", "", answer, 1)
        saw_trip = False
        for observation in result["observations"]:
            if observation["tripped"]:
                saw_trip = True
            else:
                self.assertNotIn("200 days", observation["released"]["content"])
        self.assertTrue(saw_trip)
        self.assertEqual(result["refusal_count"], 1)

    def test_supplied_whether_count_is_released_with_its_governing_words(self) -> None:
        answer = "I can't confirm whether 200 days had elapsed before filing; the procedure controls the review."
        result = FakeStream.run("The worksheet supplies 200 days.", "", answer, 1)
        self.assertIsNone(result["state"]["trip"])
        self.assertEqual(result["released"]["content"], answer)
        releases_with_count = [
            observation["released"]["content"]
            for observation in result["observations"]
            if "200 days" in observation["released"]["content"]
        ]
        self.assertTrue(releases_with_count)
        self.assertTrue(all(release == answer for release in releases_with_count))

    def test_total_is_not_released_before_small_chunk_trip(self) -> None:
        answer = "The total offense level is 21."
        result = FakeStream.run("Explain the calculation.", "", answer, 1)
        saw_trip = False
        for observation in result["observations"]:
            if observation["tripped"]:
                saw_trip = True
            else:
                self.assertNotIn("21", observation["released"]["content"])
        self.assertTrue(saw_trip)
        self.assertEqual(result["refusal_count"], 1)

    def test_attributed_figure_is_released_whole_without_refusal(self) -> None:
        answer = "The PSR puts him at level 21."
        result = FakeStream.run("Explain the calculation.", "", answer, 1)
        self.assertIsNone(result["state"]["trip"])
        self.assertEqual(result["released"]["content"], answer)
        self.assertEqual(result["refusal_count"], 0)

    def test_every_positive_has_no_painted_trip_and_one_in_stream_refusal(self) -> None:
        for path, document in seed_documents():
            for case in seed_cases(path):
                if case["kind"] != "positive":
                    continue
                for granularity in (1, 7, 0):
                    with self.subTest(seed=path.name, case=case["id"], granularity=granularity):
                        trips: list[Any] = []
                        with patch.object(
                            FILTER,
                            "record_trip",
                            side_effect=lambda trip, branch, source, collected=trips: collected.append(trip),
                        ):
                            result = FakeStream.run(
                                str(case["prompt"]),
                                str(case.get("thinking") or ""),
                                str(case["answer"]),
                                granularity,
                                usage=True,
                            )
                        supplied, contexts = self._context(str(case["prompt"]))
                        for observation in result["observations"]:
                            released = observation["released"]
                            self.assertIsNone(
                                FILTER.judge_rendered(
                                    (released["content"],),
                                    released["content"][: FILTER.MAX_MATCH_CHARS],
                                    supplied,
                                    contexts,
                                )
                            )
                        self.assertEqual(result["refusal_count"], 1)
                        state = result["state"]
                        self.assertIsInstance(state, dict)
                        trip = state["trip"]
                        self.assertIsInstance(trip, dict)
                        self.assertTrue(trip.get("pattern_id"))
                        trip_family = trip.get("family")
                        self.assertIsInstance(trip_family, str)
                        assert isinstance(trip_family, str)
                        refusal = FILTER.REFUSAL_BY_FAMILY[trip_family]
                        self.assertEqual(result["emitted_refusals"], [refusal])
                        refusal_index = result["tripped_at"]
                        self.assertIsNotNone(refusal_index)
                        assert refusal_index is not None
                        answer_result = FILTER.judge_rendered(
                            (str(case["answer"]),),
                            str(case["answer"])[: FILTER.MAX_MATCH_CHARS],
                            supplied,
                            contexts,
                        )
                        if isinstance(answer_result, FILTER.Trip):
                            self.assertEqual(trip["pattern_id"], case["pattern"])
                        for observation in result["observations"][refusal_index + 1 :]:
                            delta = observation["delta"]
                            for key in ("reasoning", "reasoning_content", "thinking", "content"):
                                if key in delta:
                                    self.assertEqual(delta[key], "")
                        message = result["body"]["messages"][-1]
                        self.assertEqual(message["content"], refusal)
                        self.assertEqual(len(message["output"]), 1)
                        self.assertEqual(message["output"][0]["type"], "message")
                        self.assertEqual(message["output"][0]["content"][0]["text"], refusal)
                        self.assertEqual(len(trips), 1)
                        self.assertEqual(trips[0].pattern_id, trip["pattern_id"])
                        self.assertEqual(trips[0].family, document["family"])
                        self.assertNotIn(FILTER.STREAM_STATE_KEY, result["metadata"])
                        self.assertIs(result["returned_body"], result["body"])

    @pytest.mark.slow
    def test_controls_flush_whole_and_keep_the_one_in_twenty_ceiling(self) -> None:
        for path, _document in seed_documents():
            controls = [case for case in seed_cases(path) if case["kind"] == "control"]
            tripped: set[object] = set()
            for case in controls:
                for granularity in (1, 7, 0):
                    with self.subTest(seed=path.name, case=case["id"], granularity=granularity):
                        result = FakeStream.run(
                            str(case["prompt"]),
                            str(case.get("thinking") or ""),
                            str(case["answer"]),
                            granularity,
                        )
                        if result["state"]["trip"] is not None:
                            tripped.add(case["id"])
                            self.assertEqual(result["refusal_count"], 1)
                            self.assertNotIn(FILTER.STREAM_STATE_KEY, result["metadata"])
                            continue
                        expected_reasoning = FILTER.REASONING_PLACEHOLDER if case.get("thinking") else ""
                        self.assertEqual(result["released"]["reasoning"], expected_reasoning)
                        self.assertEqual(result["released"]["content"], str(case["answer"]))
                        self.assertEqual(result["refusal_count"], 0)
                        entry = result["state"]["content"]
                        self.assertEqual(len(entry["text"]) - entry["released"], 0)
                        self.assertEqual(result["body"], result["body_before"])
                        self.assertNotIn(FILTER.STREAM_STATE_KEY, result["metadata"])
            self.assertLessEqual(
                len(tripped), len(controls) // 20, sorted(str(item) for item in tripped)
            )

    def test_reasoning_deltas_are_empty_except_for_the_first_placeholder(self) -> None:
        for thinking in (
            "One year from June 5, 2026 gives June 5, 2027, so the deadline is June 5, 2027.",
            "Yes, June 5, 2027 is the deadline.",
            "I calculated the deadline as June 5, 2027.",
        ):
            with self.subTest(thinking=thinking):
                result = FakeStream.run(self.PROMPT, thinking, "", 1)
                reasoning_deltas = [
                    observation["delta"]["reasoning"]
                    for observation in result["observations"]
                    if "reasoning" in observation["input"]["choices"][0]["delta"]
                ]
                self.assertTrue(reasoning_deltas)
                self.assertEqual(reasoning_deltas[0], FILTER.REASONING_PLACEHOLDER)
                self.assertTrue(all(delta == "" for delta in reasoning_deltas[1:]))
                self.assertEqual(result["released"]["reasoning"], FILTER.REASONING_PLACEHOLDER)
                self.assertIsNone(result["state"]["trip"])
                self.assertEqual(set(result["state"]), {
                    "content",
                    "placeholder_sent",
                    "trip",
                    "cancel_requested",
                    "finished",
                    "supplied",
                    "confirmation",
                    "branch",
                    "source",
                })

    def test_interleaved_requests_keep_their_own_metadata_state(self) -> None:
        cases = [case for case in seed_cases(DEADLINE_SEED_PATH) if case["kind"] == "control"][:2]
        first_filter, first_metadata = FakeStream.start(str(cases[0]["prompt"]), metadata={"chat_id": "one"})
        second_filter, second_metadata = FakeStream.start(str(cases[1]["prompt"]), metadata={"chat_id": "two"})
        first_chunks = FakeStream.chunks(str(cases[0].get("thinking") or ""), str(cases[0]["answer"]), 7)
        second_chunks = FakeStream.chunks(str(cases[1].get("thinking") or ""), str(cases[1]["answer"]), 7)
        released = [{"reasoning": "", "content": ""}, {"reasoning": "", "content": ""}]
        for index in range(max(len(first_chunks), len(second_chunks))):
            for position, chunks, stream_filter, metadata in (
                (0, first_chunks, first_filter, first_metadata),
                (1, second_chunks, second_filter, second_metadata),
            ):
                if index >= len(chunks):
                    continue
                returned = stream_filter.stream(chunks[index], metadata)
                if not isinstance(returned, dict) or not returned.get("choices"):
                    continue
                choice = returned["choices"][0]
                delta = choice.get("delta", {}) if isinstance(choice, dict) else {}
                if isinstance(delta, dict):
                    for key in released[position]:
                        value = delta.get(key)
                        if isinstance(value, str) and FILTER.DEADLINE_REFUSAL not in value:
                            released[position][key] += value
        self.assertEqual(released[0]["content"], str(cases[0]["answer"]))
        self.assertEqual(released[1]["content"], str(cases[1]["answer"]))
        self.assertIsNot(first_metadata[FILTER.STREAM_STATE_KEY], second_metadata[FILTER.STREAM_STATE_KEY])

    def test_lookahead_holds_date_until_the_following_affirmation_trips(self) -> None:
        answer = "I can't confirm the deadline of June 5, 2027 — yes, that's right, file then."
        result = FakeStream.run(self.PROMPT, "", answer, 1)
        self.assertIsNotNone(result["state"]["trip"])
        self.assertNotIn("June 5, 2027", result["released"]["content"])
        self.assertEqual(result["refusal_count"], 1)

    def test_content_transition_flushes_no_reasoning_tail_and_holds_answer(self) -> None:
        thinking = "The doctrine explains finality and tolling without computing anything. " * 8
        stream_filter, metadata = FakeStream.start("When is the filing rule discussed?")
        stream_filter.stream({"choices": [{"delta": {"reasoning": thinking}, "finish_reason": None}]}, metadata)
        event = {"choices": [{"delta": {"content": "Tolling"}, "finish_reason": None}]}
        returned = stream_filter.stream(event, metadata)
        delta = returned["choices"][0]["delta"]
        self.assertNotIn("reasoning", delta)
        self.assertEqual(delta["content"], "")
        finish = stream_filter.stream({"choices": [{"delta": {}, "finish_reason": "stop"}]}, metadata)
        self.assertEqual(finish["choices"][0]["delta"]["content"], "Tolling")
        self.assertNotIn("reasoning", finish["choices"][0]["delta"])

        both_filter, both_metadata = FakeStream.start("Explain finality.")
        both_filter.stream(
            {"choices": [{"delta": {"reasoning": thinking}, "finish_reason": None}]}, both_metadata
        )
        answer = "The doctrine explains finality and tolling without computing anything. " * 8
        transition = both_filter.stream(
            {"choices": [{"delta": {"content": answer}, "finish_reason": None}]}, both_metadata
        )
        self.assertNotIn("reasoning", transition["choices"][0]["delta"])
        finish = both_filter.stream({"choices": [{"delta": {}, "finish_reason": "stop"}]}, both_metadata)
        self.assertEqual(finish["choices"][0]["delta"]["content"], answer[-FILTER.LAG_CHARS :])
        self.assertNotIn("reasoning", finish["choices"][0]["delta"])

    def test_finish_flushes_no_reasoning_and_refuses_a_complete_trip(self) -> None:
        thinking = "The doctrine explains finality and tolling without computing anything. " * 8
        stream_filter, metadata = FakeStream.start("Explain the doctrine.")
        stream_filter.stream({"choices": [{"delta": {"reasoning": thinking}, "finish_reason": None}]}, metadata)
        finish = stream_filter.stream({"choices": [{"delta": {}, "finish_reason": "length"}]}, metadata)
        self.assertNotIn("reasoning", finish["choices"][0]["delta"])
        trip_result = FakeStream.run("When is it due?", "", "The deadline is June 5, 2027." + " safe" * 50, 7)
        self.assertIsNotNone(trip_result["state"]["trip"])
        self.assertEqual(trip_result["refusal_count"], 1)

    def test_structural_chunks_pass_clean_and_keep_shape_after_trip(self) -> None:
        clean_filter, clean_metadata = FakeStream.start("Explain finality.")
        clean_chunks = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 1}},
            {"selected_model_id": "x"},
            {"error": {"message": "x"}},
        ]
        for chunk in clean_chunks:
            original = copy.deepcopy(chunk)
            self.assertEqual(clean_filter.stream(chunk, clean_metadata), original)
        unfinished_filter, unfinished_metadata = FakeStream.start("Explain finality.")
        unfinished_filter.stream(
            {"choices": [{"delta": {"reasoning": "The doctrine explains finality."}, "finish_reason": None}]},
            unfinished_metadata,
        )
        error = {"error": {"message": "x"}}
        self.assertEqual(unfinished_filter.stream(error, unfinished_metadata), error)
        unfinished_state = unfinished_metadata[FILTER.STREAM_STATE_KEY]
        assert isinstance(unfinished_state, dict)
        self.assertFalse(unfinished_state["finished"])
        self.assertTrue(unfinished_state["placeholder_sent"])
        self.assertFalse(unfinished_state["content"]["finished"])
        trip_filter, trip_metadata = FakeStream.start("When is it due?")
        trip_filter.stream(
            {"choices": [{"delta": {"content": "The deadline is June 5, 2027." + " safe" * 50}, "finish_reason": None}]},
            trip_metadata,
        )
        for chunk in clean_chunks:
            original = copy.deepcopy(chunk)
            returned = trip_filter.stream(chunk, trip_metadata)
            self.assertEqual(returned, original)
        text_after_trip = {
            "choices": [
                {
                    "delta": {"reasoning": "later", "thinking": "later", "content": "later"},
                    "finish_reason": None,
                }
            ]
        }
        returned = trip_filter.stream(text_after_trip, trip_metadata)
        self.assertEqual(
            returned,
            {
                "choices": [
                    {
                        "delta": {"reasoning": "", "thinking": "", "content": ""},
                        "finish_reason": None,
                    }
                ]
            },
        )

    def test_unrecognised_shapes_and_missing_metadata_refuse(self) -> None:
        for event in (
            {"type": "response.output_text.delta", "delta": "x"},
            {"choices": [{"delta": {"content": "x"}}, {"delta": {"content": "y"}}]},
        ):
            stream_filter, metadata = FakeStream.start("Explain finality.")
            returned = stream_filter.stream(event, metadata)
            self.assertEqual(returned["choices"][0]["delta"]["content"], FILTER.DEADLINE_REFUSAL)
            state = metadata[FILTER.STREAM_STATE_KEY]
            assert isinstance(state, dict)
            trip = state["trip"]
            assert isinstance(trip, dict)
            self.assertEqual(trip["pattern_id"], FILTER.ERROR_PATTERN_ID)
        stream_filter, _ = FakeStream.start("Explain finality.")
        returned = stream_filter.stream({"choices": [{"delta": {"content": "x"}}]}, None)
        self.assertEqual(returned["choices"][0]["delta"]["content"], FILTER.DEADLINE_REFUSAL)

    def test_unrecognised_events_after_a_trip_are_dropped(self) -> None:
        stream_filter, metadata = FakeStream.start("Explain finality.")
        first = stream_filter.stream({"type": "response.created"}, metadata)
        self.assertEqual(first["choices"][0]["delta"]["content"], FILTER.DEADLINE_REFUSAL)
        dropped: tuple[object, ...] = (
            {"type": "response.output_text.delta", "delta": "the deadline is June 5, 2027"},
            {"type": "response.completed", "response": {"output_text": "June 5, 2027"}},
            {"choices": "not a list"},
            {"choices": [{"delta": "not a mapping"}]},
            "not a mapping",
        )
        for event in dropped:
            with self.subTest(event=event):
                self.assertFalse(stream_filter.stream(copy.deepcopy(event), metadata))
        kept: tuple[object, ...] = ({"selected_model_id": "x"}, {"error": {"message": "x"}}, {"choices": [], "usage": {}})
        for event in kept:
            with self.subTest(event=event):
                self.assertEqual(stream_filter.stream(copy.deepcopy(event), metadata), event)

    def test_hook_exception_refuses_and_discards_later_text(self) -> None:
        stream_filter, metadata = FakeStream.start("Explain finality.")
        with patch.object(FILTER, "judge_rendered", side_effect=RuntimeError("broken")):
            returned = stream_filter.stream(
                {"choices": [{"delta": {"content": "ordinary text"}, "finish_reason": None}]}, metadata
            )
        self.assertEqual(returned["choices"][0]["delta"]["content"], FILTER.DEADLINE_REFUSAL)
        state = metadata[FILTER.STREAM_STATE_KEY]
        assert isinstance(state, dict)
        trip = state["trip"]
        assert isinstance(trip, dict)
        self.assertEqual(trip["pattern_id"], FILTER.ERROR_PATTERN_ID)
        later = {"choices": [{"delta": {"content": "discarded", "thinking": "discarded"}, "finish_reason": None}]}
        returned = stream_filter.stream(later, metadata)
        self.assertEqual(returned["choices"][0]["delta"], {"content": "", "thinking": ""})

    def test_state_hygiene_and_stash(self) -> None:
        prompt = self.PROMPT
        distinctive = "quartzmarker"
        metadata: dict[str, object] = {"chat_id": "fake-chat"}
        stream_filter, metadata = FakeStream.start(prompt, metadata=metadata)
        self.assertEqual(set(metadata), {"chat_id", FILTER.STREAM_STATE_KEY})
        thinking = f"The {distinctive} doctrine explains finality."
        for rendered in (str(metadata), repr(metadata)):
            self.assertIsNone(FILTER.DATE_FORM.search(rendered))
            self.assertNotIn(distinctive, rendered)
        stream_filter.stream(
            {"choices": [{"delta": {"reasoning": thinking}, "finish_reason": None}]}, metadata
        )
        state = metadata[FILTER.STREAM_STATE_KEY]
        assert isinstance(state, dict)
        self.assertEqual(set(state), {
            "content",
            "placeholder_sent",
            "trip",
            "cancel_requested",
            "finished",
            "supplied",
            "confirmation",
            "branch",
            "source",
        })
        self.assertEqual(
            state["supplied"],
            {
                family.name: sorted(family.figures(prompt))
                for family in FILTER.FAMILIES
            },
        )
        self.assertNotIn(distinctive, repr(state))
        for rendered in (str(metadata), repr(metadata)):
            self.assertIsNone(FILTER.DATE_FORM.search(rendered))
            self.assertNotIn(distinctive, rendered)

    def test_lag_bounds_hold_for_clean_and_restatement_answers(self) -> None:
        clean = FakeStream.run(
            "Explain finality.",
            "",
            "The doctrine explains finality and tolling without computing anything. " * 20,
            1,
        )
        clean_holds = [
            len(item["text_state"]["content"]["text"]) - item["text_state"]["content"]["released"]
            for item in clean["observations"]
        ]
        self.assertGreaterEqual(max(clean_holds), FILTER.LAG_CHARS)
        self.assertLessEqual(max(clean_holds), FILTER.LAG_CHARS)

        restatement = FakeStream.run(
            self.PROMPT,
            "",
            "I can't confirm the deadline of June 5, 2027 " + "neutral context " * 30,
            1,
        )
        restatement_holds = [
            len(item["text_state"]["content"]["text"]) - item["text_state"]["content"]["released"]
            for item in restatement["observations"]
        ]
        self.assertLessEqual(max(restatement_holds), 2 * FILTER.LAG_CHARS)
        self.assertGreaterEqual(max(restatement_holds), FILTER.LAG_CHARS)

    @pytest.mark.slow
    def test_released_window_agrees_with_the_whole_released_prefix(self) -> None:
        for path, _document in seed_documents():
            for case in seed_cases(path):
                for granularity in (1, 7, 0):
                    with self.subTest(seed=path.name, case=case["id"], granularity=granularity):
                        result = FakeStream.run(
                            str(case["prompt"]),
                            str(case.get("thinking") or ""),
                            str(case["answer"]),
                            granularity,
                        )
                        supplied, contexts = self._context(str(case["prompt"]))
                        previous = 0
                        for observation in result["observations"]:
                            prefix = observation["released"]["content"]
                            if not prefix:
                                continue
                            since = max(0, previous - FILTER.RESTATEMENT_LOOKAHEAD_CHARS)
                            floor = FILTER.judge_floor(since)
                            window_result = FILTER.judge_text(
                                prefix[floor:], supplied, contexts, since=max(0, since - floor)
                            )
                            whole_result = FILTER.judge_text(prefix, supplied, contexts)
                            self.assertEqual(isinstance(window_result, FILTER.Trip), isinstance(whole_result, FILTER.Trip))
                            window_rendered = FILTER.judge_rendered(
                                (prefix[floor:],), prefix[: FILTER.MAX_MATCH_CHARS], supplied, contexts,
                                since=max(0, since - floor),
                            )
                            whole_rendered = FILTER.judge_rendered(
                                (prefix,), prefix[: FILTER.MAX_MATCH_CHARS], supplied, contexts
                            )
                            self.assertEqual(isinstance(window_rendered, FILTER.Trip), isinstance(whole_rendered, FILTER.Trip))
                            previous = len(prefix)


class OutletStream(unittest.TestCase):
    """The outlet consumes stream state, flushes unfinished tails, and then forgets it."""

    @staticmethod
    def _output_text(body: dict[str, Any], item_type: str) -> str:
        message = body["messages"][-1]
        for item in message["output"]:
            if item["type"] == item_type:
                return item["content"][-1]["text"]
        return ""

    def test_unfinished_clean_stream_flushes_content_tail_alone(self) -> None:
        answer = "The doctrine explains finality and tolling without computing anything."
        result = FakeStream.run("Explain finality.", "", answer, 7, finish=False)
        state = result["state"]
        content_entry = state["content"]
        content_tail = content_entry["text"][content_entry["released"] :]
        before_message = result["body_before"]["messages"][-1]
        message = result["body"]["messages"][-1]
        self.assertEqual(message["content"], before_message["content"] + content_tail)
        self.assertEqual(self._output_text(result["body"], "message"), answer)
        self.assertEqual(message["content"], answer)
        self.assertNotIn("reasoning", message)
        self.assertNotIn("reasoning", {item["type"] for item in message["output"]})
        self.assertEqual(result["state"]["trip"], None)
        self.assertEqual(result["returned_body"], result["body"])
        self.assertNotIn(FILTER.STREAM_STATE_KEY, result["metadata"])

    def test_unfinished_tail_that_completes_a_computation_is_replaced_and_recorded(self) -> None:
        trips: list[Any] = []
        with patch.object(
            FILTER,
            "record_trip",
            side_effect=lambda trip, branch, source, collected=trips: collected.append(trip),
        ):
            result = FakeStream.run(
                "When is it due?",
                "",
                "The filing deadline is June 5, 2027.",
                1,
                finish=False,
            )
        self.assertNotIn("June 5, 2027", result["body_before"]["messages"][-1]["content"])
        self.assertEqual(result["body"]["messages"][-1]["content"], FILTER.DEADLINE_REFUSAL)
        self.assertEqual(len(trips), 1)
        self.assertIsInstance(trips[0], FILTER.Trip)
        self.assertNotIn(FILTER.STREAM_STATE_KEY, result["metadata"])
