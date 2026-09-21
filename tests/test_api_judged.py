"""Pure stream mechanics contracts for the judged API completion path."""

import json
import re
import sys
import tempfile
import threading
import types
import unittest
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Literal
from unittest.mock import patch

import pytest
import yaml  # type: ignore[import-untyped]

from gideon import guardrail
from gideon.api.judged import (
    CHUNK_OBJECT,
    StreamMechanics,
    judge_completion,
    source_for_header,
    stream_state_from_body,
)
from gideon.api.sse import (
    DONE_EVENT,
    SSE_EVENT_BUFFER_LIMIT_BYTES,
    EventReassembler,
    SSEEventTooLargeError,
)

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "eval/seed/guardrails"
BASE_ENVELOPE: dict[str, object] = {
    "id": "fixture-stream",
    "object": CHUNK_OBJECT,
    "created": 1_700_000_000,
    "model": "fixture-model",
    "fixture_envelope": "preserved",
}

_DISPATCH_PATCH = patch.object(guardrail, "dispatch_trip_row", lambda row: None)


def setUpModule() -> None:
    _DISPATCH_PATCH.start()


def tearDownModule() -> None:
    _DISPATCH_PATCH.stop()


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
    return [
        case
        for case in cases
        if isinstance(case, dict) and case.get("id") not in retired
    ]


def pieces(text: str, granularity: int) -> list[str]:
    if granularity == 0:
        return re.findall(r"\S+\s*|\s+", text)
    return [
        text[index : index + granularity]
        for index in range(0, len(text), granularity)
    ]


def body_for_prompt(prompt: str, *, model: str = "fixture-model") -> bytes:
    return json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode()


def chunk(
    delta: Mapping[str, object],
    *,
    finish_reason: object = None,
    envelope: Mapping[str, object] = BASE_ENVELOPE,
) -> dict[str, object]:
    event = dict(envelope)
    event["choices"] = [
        {
            "index": 0,
            "delta": dict(delta),
            "finish_reason": finish_reason,
        }
    ]
    return event


def usage_chunk(*, envelope: Mapping[str, object] = BASE_ENVELOPE) -> dict[str, object]:
    event = dict(envelope)
    event["choices"] = []
    event["usage"] = {"prompt_tokens": 1, "completion_tokens": 1}
    return event


def seed_chunks(
    thinking: str,
    answer: str,
    granularity: int,
) -> list[dict[str, object] | str]:
    result: list[dict[str, object] | str] = [
        chunk({"role": "assistant", "content": ""})
    ]
    for key, text in (("reasoning", thinking), ("content", answer)):
        result.extend(chunk({key: piece}) for piece in pieces(text, granularity))
    result.append(chunk({}, finish_reason="stop"))
    result.append(DONE_EVENT)
    return result


def content_from_payload(payload: object) -> str:
    if not isinstance(payload, Mapping):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, Mapping):
        return ""
    delta = choice.get("delta")
    if not isinstance(delta, Mapping):
        return ""
    content = delta.get("content")
    return content if isinstance(content, str) else ""


def contains_key(value: object, key: str) -> bool:
    if isinstance(value, Mapping):
        return key in value or any(contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(contains_key(item, key) for item in value)
    return False


def state_for_prompt(
    prompt: str, *, source: str = "user"
) -> guardrail.StreamState:
    return stream_state_from_body(body_for_prompt(prompt), source)


def completion_body(choices: list[object], **extra: object) -> bytes:
    payload: dict[str, object] = {
        "id": "fixture-completion",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "fixture-model",
        "choices": choices,
    }
    payload.update(extra)
    return json.dumps(payload, separators=(",", ":")).encode()


class ApiJudged(unittest.TestCase):
    def run_payloads(
        self,
        prompt: str,
        payloads: list[dict[str, object] | str],
    ) -> tuple[guardrail.StreamState, list[dict[str, object] | str], int]:
        state = state_for_prompt(prompt)
        mechanics = StreamMechanics(state)
        output: list[dict[str, object] | str] = []
        signals = 0
        for payload in payloads:
            emitted, tripped = mechanics.process(payload)
            output.extend(emitted)
            if tripped:
                signals += 1
                break
        return state, output, signals

    @staticmethod
    def answer_text(case: Mapping[str, object]) -> str:
        answer = case.get("answer")
        if not isinstance(answer, str):
            raise AssertionError("seed answer is not text")
        return answer

    @staticmethod
    def prompt_text(case: Mapping[str, object]) -> str:
        prompt = case.get("prompt")
        if not isinstance(prompt, str):
            raise AssertionError("seed prompt is not text")
        return prompt

    @staticmethod
    def thinking_text(case: Mapping[str, object]) -> str:
        thinking = case.get("thinking")
        return thinking if isinstance(thinking, str) else ""

    @pytest.mark.slow
    def test_seed_positives_trip_once_and_released_prefixes_stay_clean(self) -> None:
        for path, document in seed_documents():
            family = document.get("family")
            self.assertIsInstance(family, str)
            assert isinstance(family, str)
            for case in seed_cases(path):
                if case.get("kind") != "positive":
                    continue
                for granularity in (1, 7, 0):
                    with self.subTest(
                        seed=path.name,
                        case=case.get("id"),
                        granularity=granularity,
                    ):
                        prompt = self.prompt_text(case)
                        answer = self.answer_text(case)
                        state = state_for_prompt(prompt)
                        mechanics = StreamMechanics(state)
                        supplied, contexts = guardrail.message_context(
                            [{"role": "user", "content": prompt}], 1
                        )
                        released = ""
                        output: list[dict[str, object] | str] = []
                        signals = 0
                        thinking = self.thinking_text(case)
                        for payload in seed_chunks(thinking, answer, granularity):
                            emitted, tripped = mechanics.process(payload)
                            output.extend(emitted)
                            if tripped:
                                signals += 1
                                break
                            released += "".join(
                                content_from_payload(item) for item in emitted
                            )
                            if released:
                                self.assertIsNone(
                                    guardrail.judge_rendered(
                                        (released,),
                                        released[: guardrail.MAX_MATCH_CHARS],
                                        supplied,
                                        contexts,
                                    )
                                )
                        self.assertEqual(signals, 1)
                        trip = state.get("trip")
                        self.assertIsInstance(trip, dict)
                        assert isinstance(trip, dict)
                        self.assertEqual(trip.get("family"), family)
                        refusal = guardrail.REFUSAL_BY_FAMILY[family]
                        refusal_count = sum(
                            content_from_payload(item).count(refusal)
                            for item in output
                        )
                        self.assertEqual(refusal_count, 1)

    @pytest.mark.slow
    def test_seed_controls_keep_the_one_in_twenty_over_trip_ceiling(self) -> None:
        for path, document in seed_documents():
            family = document.get("family")
            self.assertIsInstance(family, str)
            assert isinstance(family, str)
            controls = [
                case for case in seed_cases(path) if case.get("kind") == "control"
            ]
            tripped: set[object] = set()
            for case in controls:
                prompt = self.prompt_text(case)
                answer = self.answer_text(case)
                thinking = self.thinking_text(case)
                for granularity in (1, 7, 0):
                    with self.subTest(
                        seed=path.name,
                        case=case.get("id"),
                        granularity=granularity,
                    ):
                        state, output, signals = self.run_payloads(
                            prompt, seed_chunks(thinking, answer, granularity)
                        )
                        if state.get("trip") is not None:
                            tripped.add(case.get("id"))
                            self.assertEqual(signals, 1)
                            self.assertEqual(
                                sum(
                                    content_from_payload(item).count(
                                        guardrail.REFUSAL_BY_FAMILY[family]
                                    )
                                    for item in output
                                ),
                                1,
                            )
            self.assertLessEqual(
                len(tripped),
                len(controls) // 20,
                sorted(str(case_id) for case_id in tripped),
            )

    def test_request_state_uses_body_context_and_empty_stash_fallbacks(self) -> None:
        positive_path = next(path for path, _document in seed_documents())
        case = next(
            case for case in seed_cases(positive_path) if case.get("kind") == "positive"
        )
        prompt = self.prompt_text(case)
        messages: list[object] = [{"role": "user", "content": prompt}]
        state = stream_state_from_body(
            json.dumps({"model": "branch-from-body", "messages": messages}).encode(),
            "user",
        )
        supplied, contexts = guardrail.message_context(messages, len(messages))
        self.assertEqual(state.get("branch"), "branch-from-body")
        self.assertEqual(state.get("source"), "user")
        self.assertEqual(
            state.get("supplied"),
            {name: sorted(figures) for name, figures in supplied.items()},
        )
        self.assertEqual(state.get("confirmation"), sorted(contexts))

        for body in (b"[]", b'{"model":"fixture-model","messages":{}}', b"not-json"):
            with self.subTest(body=body):
                fallback = stream_state_from_body(body, "user")
                self.assertEqual(fallback.get("supplied"), {})
                self.assertEqual(fallback.get("confirmation"), [])
        self.assertEqual(
            stream_state_from_body(b'{"model":"fixture-model","messages":{}}', "user").get(
                "branch"
            ),
            "fixture-model",
        )

    def test_source_for_header_returns_only_content_free_words(self) -> None:
        eval_identity = "eval@example.invalid"
        cases = (
            ((eval_identity,), "eval", "exact value"),
            (("EVAL@EXAMPLE.INVALID",), "eval", "different case"),
            ((f"  {eval_identity}  ",), "eval", "surrounding spaces"),
            ((), "user", "absent header"),
            (("",), "user", "empty value"),
            ((eval_identity, eval_identity), "user", "repeated header"),
            (("other@example.invalid",), "user", "another identity"),
        )
        for values, expected, case in cases:
            with self.subTest(case=case):
                self.assertEqual(source_for_header(values, eval_identity), expected)

        headers = {"X-Other-Header": (eval_identity,)}
        self.assertEqual(
            source_for_header(headers.get("X-Configured-Header", ()), eval_identity),
            "user",
        )

    def test_request_state_carries_source_through_every_constructor_path(self) -> None:
        bodies = (
            b"not-json",
            b"[]",
            b'{"model":"fixture-model","messages":{}}',
            body_for_prompt("Explain a visibly fictitious rule."),
        )
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(stream_state_from_body(body, "eval")["source"], "eval")

    def test_whole_trips_record_once_and_clear_after_unjudgeable_choice(self) -> None:
        rows: list[object] = []
        row_written = threading.Event()

        class Connection:
            def __enter__(self) -> "Connection":
                return self

            def __exit__(self, *args: object) -> Literal[False]:
                return False

            def execute(self, statement: str, parameters: object = None) -> None:
                del statement
                if parameters is not None:
                    rows.append(parameters)
                    row_written.set()

        def connect(**kwargs: object) -> Connection:
            del kwargs
            return Connection()

        class Driver(types.ModuleType):
            connect: Callable[..., Connection]

        driver = Driver(guardrail.TRIP_DRIVER_MODULE)
        driver.connect = connect
        trip_answer = "The deadline is June 5, 2027."
        with tempfile.TemporaryDirectory() as directory:
            password_path = Path(directory) / "password"
            password_path.write_text("fixture-trip-password\n", encoding="utf-8")
            _DISPATCH_PATCH.stop()
            try:
                with (
                    patch.object(guardrail, "TRIP_PASSWORD_PATH", str(password_path)),
                    patch.dict(sys.modules, {guardrail.TRIP_DRIVER_MODULE: driver}),
                ):
                    first_body = completion_body(
                        [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": trip_answer},
                            },
                            {
                                "index": 1,
                                "message": {"role": "assistant", "content": trip_answer},
                            },
                        ]
                    )
                    first_state = state_for_prompt("Explain a fictitious rule.", source="eval")
                    output, failure = judge_completion(first_body, first_state)
                    self.assertIsNotNone(output)
                    self.assertIsNone(failure)
                    self.assertTrue(row_written.wait(1))
                    row_written.clear()
                    self.assertEqual(len(rows), 1)
                    first_trip = first_state["trip"]
                    self.assertIsInstance(first_trip, Mapping)
                    assert isinstance(first_trip, Mapping)
                    self.assertEqual(
                        rows[0],
                        (
                            first_state["branch"],
                            first_trip["family"],
                            first_trip["pattern_id"],
                            "eval",
                        ),
                    )

                    second_state = state_for_prompt(
                        "Explain another fictitious rule.", source="eval"
                    )
                    content = second_state["content"]
                    self.assertIsInstance(content, dict)
                    assert isinstance(content, dict)
                    content["text"] = "held fixture text"
                    content["constraints"] = [[1, 4]]
                    second_body = completion_body(
                        [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": trip_answer},
                            },
                            {"index": 1},
                        ]
                    )
                    output, failure = judge_completion(second_body, second_state)
                    self.assertIsNone(output)
                    self.assertEqual(failure, "TypeError")
                    self.assertTrue(row_written.wait(1))
                    self.assertEqual(len(rows), 2)
                    self.assertEqual(content["text"], "")
                    self.assertEqual(content["constraints"], [])
            finally:
                _DISPATCH_PATCH.start()

    def test_one_dispatch_per_request_whatever_trips_after_the_first(self) -> None:
        # The writer's thread makes a row count a race; the dispatch itself is
        # synchronous, so the guard is counted here and the row's values are
        # read over the injected driver above.
        trip_answer = "The deadline is June 5, 2027."
        tripping_choice = {
            "index": 0,
            "message": {"role": "assistant", "content": trip_answer},
        }
        cases: tuple[tuple[str, list[object], bool], ...] = (
            (
                "two tripping choices",
                [tripping_choice, dict(tripping_choice, index=1)],
                True,
            ),
            ("a trip then an unjudgeable choice", [tripping_choice, {"index": 1}], False),
        )
        for case, choices, judged_output in cases:
            with self.subTest(case=case):
                dispatched: list[guardrail.TripRow] = []
                with patch.object(guardrail, "dispatch_trip_row", dispatched.append):
                    output, failure = judge_completion(
                        completion_body(choices),
                        state_for_prompt("Explain a fictitious rule.", source="eval"),
                    )
                self.assertEqual(len(dispatched), 1)
                self.assertEqual(dispatched[0].source, "eval")
                if judged_output:
                    self.assertIsNotNone(output)
                    self.assertIsNone(failure)
                else:
                    self.assertIsNone(output)
                    self.assertIsNotNone(failure)

    def test_reasoning_and_unlisted_delta_fields_never_reach_downstream(self) -> None:
        for key in ("reasoning", "reasoning_content", "thinking", "fixture_hidden"):
            with self.subTest(key=key):
                state = state_for_prompt("Explain this visibly fictitious rule.")
                mechanics = StreamMechanics(state)
                private_text = f"private {key} fixture text"
                emitted, tripped = mechanics.process(chunk({key: private_text}))
                self.assertEqual(emitted, [])
                self.assertFalse(tripped)
                emitted, tripped = mechanics.process(
                    chunk({"content": "A safe fixture answer."}, finish_reason="stop")
                )
                self.assertFalse(tripped)
                for payload in emitted:
                    self.assertFalse(contains_key(payload, key))
                    self.assertNotIn(private_text, json.dumps(payload))

    def test_clean_stream_preserves_envelopes_and_usage(self) -> None:
        answer = "This fixture describes a neutral record without a calculation. " * 18
        state = state_for_prompt("Explain the fictitious record.")
        mechanics = StreamMechanics(state)
        usage = usage_chunk()
        output: list[dict[str, object] | str] = []
        payloads: list[dict[str, object] | str] = [
            *(chunk({"content": piece}) for piece in pieces(answer, 7)),
            chunk({}, finish_reason="stop"),
            usage,
            DONE_EVENT,
        ]
        for payload in payloads:
            emitted, tripped = mechanics.process(payload)
            self.assertFalse(tripped)
            output.extend(emitted)
        self.assertEqual(output[-1], "[DONE]")
        self.assertIn(usage, output)
        for payload in output:
            if isinstance(payload, Mapping):
                expected = BASE_ENVELOPE
                if payload.get("choices") == []:
                    expected = BASE_ENVELOPE | {"usage": usage["usage"]}
                self.assertEqual(
                    {key: value for key, value in payload.items() if key != "choices"},
                    expected,
                )
        self.assertEqual(
            "".join(content_from_payload(payload) for payload in output), answer
        )
        self.assertIsNone(state.get("trip"))

    def test_separator_is_literal_after_release_and_absent_before_release(self) -> None:
        released_answer = "neutral " * 100 + "The deadline is June 5, 2027."
        released_state = state_for_prompt("Explain a fictitious legal rule.")
        released_mechanics = StreamMechanics(released_state)
        emitted, tripped = released_mechanics.process(
            chunk({"content": released_answer}, finish_reason="stop")
        )
        self.assertTrue(tripped)
        content_state = released_state.get("content")
        self.assertIsInstance(content_state, dict)
        assert isinstance(content_state, dict)
        self.assertGreater(content_state["released"], 0)
        released_trip = released_state["trip"]
        self.assertIsInstance(released_trip, dict)
        assert isinstance(released_trip, dict)
        refusal = guardrail.REFUSAL_BY_FAMILY[released_trip["family"]]
        self.assertEqual(
            content_from_payload(emitted[0]),
            guardrail.REFUSAL_SEPARATOR + refusal,
        )

        held_state = state_for_prompt("Explain a fictitious legal rule.")
        held_mechanics = StreamMechanics(held_state)
        held_mechanics.process(chunk({"content": "The deadline is June 5, 2027."}))
        emitted, tripped = held_mechanics.process(chunk({}, finish_reason="stop"))
        self.assertTrue(tripped)
        held_trip = held_state["trip"]
        self.assertIsInstance(held_trip, dict)
        assert isinstance(held_trip, dict)
        held_refusal = guardrail.REFUSAL_BY_FAMILY[held_trip["family"]]
        self.assertEqual(content_from_payload(emitted[0]), held_refusal)
        self.assertNotIn(guardrail.REFUSAL_SEPARATOR, content_from_payload(emitted[0]))

    def test_finish_chunk_carries_the_settled_tail_in_its_delta(self) -> None:
        answer = "A short fictitious explanation."
        state = state_for_prompt("Explain a fictitious rule.")
        mechanics = StreamMechanics(state)
        emitted, tripped = mechanics.process(chunk({"content": answer}))
        self.assertEqual(emitted, [])
        self.assertFalse(tripped)
        emitted, tripped = mechanics.process(chunk({}, finish_reason="stop"))
        self.assertFalse(tripped)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(content_from_payload(emitted[0]), answer)
        first_payload = emitted[0]
        self.assertIsInstance(first_payload, Mapping)
        assert isinstance(first_payload, Mapping)
        choices = first_payload["choices"]
        self.assertIsInstance(choices, list)
        assert isinstance(choices, list)
        self.assertEqual(choices[0]["finish_reason"], "stop")

    def _held_stream(self, answer: str) -> tuple[guardrail.StreamState, StreamMechanics]:
        state = state_for_prompt("Explain a fictitious rule.")
        mechanics = StreamMechanics(state)
        emitted, tripped = mechanics.process(chunk({"content": answer}))
        self.assertEqual(emitted, [])
        self.assertFalse(tripped)
        return state, mechanics

    def _assert_trip_ending(
        self,
        result: tuple[list[dict[str, object] | str], bool],
        forbidden: Mapping[str, object] | None = None,
    ) -> None:
        emitted, tripped = result
        self.assertTrue(tripped)
        self.assertEqual(len(emitted), 3)
        self.assertEqual(emitted[-1], "[DONE]")
        if forbidden is not None:
            self.assertNotIn(forbidden, emitted)
        for payload in emitted[:2]:
            self.assertIsInstance(payload, Mapping)
            assert isinstance(payload, Mapping)
            self.assertEqual(payload["object"], CHUNK_OBJECT)
            self.assertIn("choices", payload)

    def test_unfinished_clean_ends_settle_tail_before_error_marker_or_body_end(self) -> None:
        answer = "A short fictitious explanation."
        error = {"error": {"message": "fixture upstream error"}}
        state, mechanics = self._held_stream(answer)
        emitted, tripped = mechanics.process(error)
        self.assertFalse(tripped)
        self.assertEqual(content_from_payload(emitted[0]), answer)
        self.assertEqual(emitted[1], error)

        state, mechanics = self._held_stream(answer)
        emitted, tripped = mechanics.process("[DONE]")
        self.assertFalse(tripped)
        self.assertEqual(content_from_payload(emitted[0]), answer)
        self.assertEqual(emitted[1], "[DONE]")

        for end_name in ("clean body", "transport failure"):
            with self.subTest(end=end_name):
                state, mechanics = self._held_stream(answer)
                emitted, tripped = mechanics.finish()
                self.assertFalse(tripped)
                self.assertEqual(len(emitted), 1)
                self.assertEqual(content_from_payload(emitted[0]), answer)
                self.assertIsNone(state.get("trip"))

    def test_unfinished_tripping_ends_drop_error_or_marker_after_trip(self) -> None:
        answer = "The deadline is June 5, 2027."
        error = {"error": {"message": "fixture upstream error"}}
        for end_name in ("error", "marker"):
            with self.subTest(end=end_name):
                state, mechanics = self._held_stream(answer)
                result = mechanics.process(error if end_name == "error" else "[DONE]")
                self._assert_trip_ending(result, error if end_name == "error" else None)
                self.assertNotIn(DONE_EVENT, result[0][:-1])
                self.assertIsNotNone(state.get("trip"))
        for end_name in ("clean body", "transport failure"):
            with self.subTest(end=end_name):
                state, mechanics = self._held_stream(answer)
                result = mechanics.finish()
                self._assert_trip_ending(result)
                self.assertIsNotNone(state.get("trip"))

    def test_payloads_after_error_are_not_read_for_text_except_end_marker(self) -> None:
        state, mechanics = self._held_stream("A short fictitious explanation.")
        error = {"error": {"message": "fixture upstream error"}}
        emitted, tripped = mechanics.process(error)
        self.assertFalse(tripped)
        after_error = chunk({"content": "secret text after error"})
        ignored, tripped = mechanics.process(after_error)
        self.assertEqual(ignored, [])
        self.assertFalse(tripped)
        done, tripped = mechanics.process("[DONE]")
        self.assertEqual(done, ["[DONE]"])
        self.assertFalse(tripped)
        self.assertNotIn("secret text after error", json.dumps(emitted + done))
        self.assertIsNone(state.get("trip"))

    def assert_first_error_trip(
        self, payload: Mapping[str, object] | str
    ) -> None:
        state = state_for_prompt("Explain a fictitious rule.")
        mechanics = StreamMechanics(state)
        emitted, tripped = mechanics.process(payload)
        self._assert_trip_ending((emitted, tripped))
        for item in emitted[:2]:
            self.assertEqual(set(item), {"object", "choices"})
            self.assertNotIn("id", item)
            self.assertNotIn("created", item)
            self.assertNotIn("model", item)
        self.assertEqual(
            state["trip"],
            {"family": guardrail.DEADLINE_FAMILY.name, "pattern_id": guardrail.ERROR_PATTERN_ID},
        )

    def test_error_shapes_fail_closed(self) -> None:
        for payload in (
            "not-json",
            {"id": "no-choices-or-error"},
            {"choices": {"wrong": "type"}},
            {
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": None},
                    {"index": 1, "delta": {}, "finish_reason": None},
                ]
            },
            {"choices": [{"index": 1, "delta": {}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": "wrong", "finish_reason": None}]},
            {
                "choices": [
                    {"index": 0, "delta": {"content": 7}, "finish_reason": None}
                ]
            },
        ):
            with self.subTest(payload=payload):
                self.assert_first_error_trip(payload)

    def test_unparseable_and_sse_overrun_are_first_event_error_endings(self) -> None:
        self.assert_first_error_trip("not-json")

        state = state_for_prompt("Explain a fictitious rule.")
        mechanics = StreamMechanics(state)
        with self.assertRaises(SSEEventTooLargeError):
            EventReassembler().feed(b"x" * (SSE_EVENT_BUFFER_LIMIT_BYTES + 1))
        # The overrun is raised before any payload exists, so the relay enters
        # the error doctrine through the mechanics' own fail-closed call.
        emitted, tripped = mechanics.fail_closed()
        self._assert_trip_ending((emitted, tripped))
        for item in emitted[:2]:
            self.assertEqual(set(item), {"object", "choices"})
        self.assertEqual(
            state["trip"],
            {"family": guardrail.DEADLINE_FAMILY.name, "pattern_id": guardrail.ERROR_PATTERN_ID},
        )

    def test_the_role_only_first_chunk_is_relayed_at_once(self) -> None:
        state = state_for_prompt("Explain a fictitious rule.")
        emitted, tripped = StreamMechanics(state).process(
            chunk({"role": "assistant", "content": ""})
        )
        self.assertFalse(tripped)
        self.assertEqual(len(emitted), 1)
        first = emitted[0]
        self.assertIsInstance(first, Mapping)
        assert isinstance(first, Mapping)
        choices = first["choices"]
        assert isinstance(choices, list)
        self.assertEqual(choices[0]["delta"], {"role": "assistant"})
        self.assertEqual(
            {key: value for key, value in first.items() if key != "choices"},
            BASE_ENVELOPE,
        )

    def test_clean_whole_body_rebuilds_messages_and_preserves_the_body(self) -> None:
        message = {
            "role": "assistant",
            "content": "A safe fictitious explanation.",
            "tool_calls": [{"id": "fixture-call", "type": "function"}],
            "reasoning": "private fixture reasoning",
            "fixture_hidden": "private fixture field",
        }
        body = completion_body(
            [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "stop",
                    "logprobs": {"content": [{"token": "fixture"}]},
                }
            ],
            usage={"prompt_tokens": 2, "completion_tokens": 5},
            fixture_envelope="preserved",
        )
        output, failure = judge_completion(body, state_for_prompt("Explain a safe rule."))

        self.assertIsNone(failure)
        self.assertIsNotNone(output)
        assert output is not None
        parsed = json.loads(output)
        self.assertIsInstance(parsed, dict)
        assert isinstance(parsed, dict)
        expected = json.loads(body)
        # The choice is rebuilt as it is on the stream, so `logprobs` — which
        # could carry the very tokens the window refused — never survives.
        expected["choices"][0] = {
            "index": 0,
            "finish_reason": "stop",
            "message": {
                "role": message["role"],
                "content": message["content"],
                "tool_calls": message["tool_calls"],
            },
        }
        self.assertEqual(parsed, expected)
        self.assertNotIn("logprobs", output.decode())

    def test_whole_trip_replaces_content_and_preserves_finish_and_usage(self) -> None:
        body = completion_body(
            [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "The deadline is June 5, 2027.",
                        "reasoning_content": "private fixture reasoning",
                    },
                    "finish_reason": "length",
                }
            ],
            usage={"prompt_tokens": 4, "completion_tokens": 9},
        )
        state = state_for_prompt("Explain a fictitious legal rule.")
        output, failure = judge_completion(body, state)

        self.assertIsNone(failure)
        self.assertIsNotNone(output)
        assert output is not None
        parsed = json.loads(output)
        self.assertIsInstance(parsed, dict)
        assert isinstance(parsed, dict)
        choice = parsed["choices"][0]
        self.assertEqual(choice["finish_reason"], "length")
        self.assertEqual(parsed["usage"], {"prompt_tokens": 4, "completion_tokens": 9})
        self.assertEqual(
            choice["message"]["content"], guardrail.DEADLINE_FAMILY.refusal
        )
        self.assertNotIn("June 5, 2027", output.decode())
        trip = state.get("trip")
        self.assertIsInstance(trip, dict)
        assert isinstance(trip, dict)
        self.assertEqual(trip["family"], guardrail.DEADLINE_FAMILY.name)

    def test_whole_reasoning_keys_are_stripped(self) -> None:
        for key in ("reasoning", "reasoning_content", "thinking"):
            with self.subTest(key=key):
                body = completion_body(
                    [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "A safe fictitious explanation.",
                                key: "private fixture reasoning",
                            },
                            "finish_reason": "stop",
                        }
                    ]
                )
                output, failure = judge_completion(
                    body, state_for_prompt("Explain a safe rule.")
                )
                self.assertIsNone(failure)
                self.assertIsNotNone(output)
                assert output is not None
                parsed = json.loads(output)
                self.assertNotIn(key, parsed["choices"][0]["message"])

    def test_whole_null_content_with_tool_calls_is_not_judged(self) -> None:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "fixture-call", "type": "function"}],
        }
        body = completion_body(
            [{"index": 0, "message": message, "finish_reason": "tool_calls"}]
        )
        state = state_for_prompt("Explain a safe rule.")
        output, failure = judge_completion(body, state)

        self.assertIsNone(failure)
        self.assertIsNotNone(output)
        assert output is not None
        parsed = json.loads(output)
        self.assertEqual(parsed["choices"][0]["message"], message)
        self.assertIsNone(state.get("trip"))

    def test_whole_choices_are_judged_independently(self) -> None:
        body = completion_body(
            [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "A safe fictitious explanation.",
                    },
                    "finish_reason": "stop",
                },
                {
                    "index": 1,
                    "message": {
                        "role": "assistant",
                        "content": "The deadline is June 5, 2027.",
                    },
                    "finish_reason": "stop",
                },
            ]
        )
        state = state_for_prompt("Explain a fictitious legal rule.")
        output, failure = judge_completion(body, state)

        self.assertIsNone(failure)
        self.assertIsNotNone(output)
        assert output is not None
        parsed = json.loads(output)
        self.assertEqual(
            parsed["choices"][0]["message"]["content"],
            "A safe fictitious explanation.",
        )
        self.assertEqual(
            parsed["choices"][1]["message"]["content"],
            guardrail.DEADLINE_FAMILY.refusal,
        )

    def test_unjudgeable_whole_shapes_fail_closed(self) -> None:
        bodies = (
            b"not-json",
            b"[]",
            b'{"id":"fixture-completion"}',
            completion_body([]).replace(b'"choices":[]', b'"choices":{}'),
            completion_body(["not a choice"]),
            completion_body([{"index": 0}]),
            completion_body([{"index": 0, "message": "not a message"}]),
            completion_body([{"index": 0, "message": {}}]),
            completion_body(
                [{"index": 0, "message": {"content": 7}}]
            ),
        )
        for body in bodies:
            with self.subTest(body=body):
                state = state_for_prompt("Explain a safe rule.")
                output, failure = judge_completion(body, state)
                # The signal is the failure's class, for the relay's log; the
                # relay never sees the message, which would be model text.
                self.assertIn(failure, {"TypeError", "JSONDecodeError"})
                self.assertIsNone(output)
                self.assertEqual(
                    state["trip"],
                    {
                        "family": guardrail.DEADLINE_FAMILY.name,
                        "pattern_id": guardrail.ERROR_PATTERN_ID,
                    },
                )

    def test_task_shaped_body_uses_the_whole_path_without_a_special_branch(self) -> None:
        request_body = json.dumps(
            {
                "stream": False,
                "messages": [{"role": "user", "content": "short fictitious chat"}],
            }
        ).encode()
        state = stream_state_from_body(request_body, "user")
        output, failure = judge_completion(
            completion_body(
                [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "A safe fictitious reply.",
                        },
                        "finish_reason": "stop",
                    }
                ]
            ),
            state,
        )

        self.assertIsNone(failure)
        self.assertIsNotNone(output)
        self.assertIsNone(state.get("branch"))


if __name__ == "__main__":
    unittest.main()
