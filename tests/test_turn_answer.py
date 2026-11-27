"""The lag window's output and terminal outcomes over generator streams."""

import json
import unittest
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from test_api_judged import seed_cases, seed_chunks, seed_documents

from gideon import guardrail
from gideon.api.progress import REASONING_KEYS
from gideon.api.sse import DONE_EVENT, EventReassembler, SSEEventTooLargeError
from gideon.turn.answer import (
    LEGAL_BRANCH,
    AnswerMechanics,
    AnswerStep,
    TokenUsage,
    stream_state,
)
from gideon.turn.prompt import messages

FIXTURES = Path(__file__).with_name("fixtures") / "turn"
QUESTION = "What did the fictitious harbor board decide?"
ENGINE_ERROR = '{"error":{"message":"Fictitious generator diagnostics"}}'


def payload(delta: dict[str, object], *, finish_reason: str | None = None) -> str:
    return json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]})


def with_usage(item: str, usage: object) -> str:
    return json.dumps({**json.loads(item), "usage": usage})


def event_data(encoded: bytes, kind: str) -> dict[str, Any]:
    name, data, empty = encoded.splitlines()
    assert name == f"event: {kind}".encode() and data.startswith(b"data: ") and not empty
    return json.loads(data.removeprefix(b"data: "))


def recorded_answer(payloads: Iterable[str]) -> tuple[str, str]:
    choices = [choice for item in payloads if item != DONE_EVENT for choice in json.loads(item)["choices"]]
    return "".join(choice["delta"].get("content") or "" for choice in choices), choices[-1]["finish_reason"]


class TurnAnswer(unittest.TestCase):
    def setUp(self) -> None:
        self.trips: list[guardrail.TripRow] = []
        self.enterContext(patch.object(guardrail.writer, "dispatch_trip_row", self.trips.append))

    def consume(self, mechanics: AnswerMechanics, payloads: Iterable[str]) -> tuple[AnswerStep, list[bytes]]:
        terminal = None
        released: list[bytes] = []
        for item in payloads:
            step = mechanics.process(item)
            if terminal is not None:
                self.assertEqual(step, AnswerStep())
            released.extend(step.events)
            if step.outcome != "open":
                terminal = step
        assert terminal is not None, "The generator stream must reach a terminal outcome."
        return terminal, released

    def assert_answer(
        self, step: AnswerStep, released: Iterable[bytes], text: str, end: str, *, family: str | None = None,
    ) -> None:
        timings = {"total": 2500.0}
        final = step.answer_event(timings)
        assert final is not None
        expected: dict[str, object] = {"text": text, "end": end, "timings_ms": timings}
        if family is not None:
            expected["family"] = family
        self.assertEqual(event_data(final, "answer"), expected)
        self.assertEqual("".join(event_data(event, "text")["text"] for event in released), text)

    def assert_tripped(self, step: AnswerStep, released: Sequence[bytes], family: str) -> str:
        self.assertEqual((step.outcome, step.end, step.family), ("tripped", "tripped", family))
        prefix = "".join(event_data(event, "text")["text"] for event in released[:-1])
        refusal = guardrail.refusal_for(family)
        if prefix:
            refusal = guardrail.REFUSAL_SEPARATOR + refusal
        self.assertEqual(event_data(released[-1], "text"), {"text": refusal})
        self.assert_answer(step, released, prefix + refusal, "tripped", family=family)
        return prefix

    def test_recorded_streams_reassemble_at_every_byte_split(self) -> None:
        for name in ("generator-stream.sse", "generator-truncated.sse", "generator-thinking-off.sse"):
            with self.subTest(name=name):
                raw = (FIXTURES / name).read_bytes()
                expected = [line.removeprefix(b"data: ").decode() for line in raw.splitlines() if line.startswith(b"data: ")]
                self.assertEqual(expected[-1], DONE_EVENT)
                for split in range(len(raw) + 1):
                    reassembler = EventReassembler()
                    actual = reassembler.feed(raw[:split]) + reassembler.feed(raw[split:])
                    if actual != expected or reassembler.has_pending_bytes():
                        self.fail(f"{name} did not reassemble at offset {split}.")

                # The mechanics sees the same payload sequence at every split.
                terminal, released = self.consume(AnswerMechanics(stream_state(QUESTION, guardrail.EVAL_SOURCE)), actual)
                text, finish_reason = recorded_answer(expected)
                self.assertEqual(terminal.finish_reason, finish_reason)
                if text:
                    self.assertEqual(terminal.outcome, "finished")
                    self.assert_answer(terminal, released, text, "complete")
                else:
                    self.assertEqual((terminal.outcome, terminal.finish_reason), ("empty", "length"))
                    self.assertEqual(released, [])
                    self.assertIsNone(terminal.answer_event({}))

    def test_each_reasoning_name_is_withheld_and_empty_choices_are_dropped(self) -> None:
        for key in REASONING_KEYS:
            with self.subTest(key=key):
                mechanics = AnswerMechanics(stream_state(QUESTION, guardrail.EVAL_SOURCE))
                reasoning = "Fictitious private reasoning"
                self.assertEqual(mechanics.process(payload({key: reasoning})), AnswerStep())
                self.assertEqual(mechanics.process('{"choices":[],"usage":{"completion_tokens":2}}'), AnswerStep())
                text = "The fictitious harbor stayed open."
                step = mechanics.process(payload({key: reasoning, "content": text}, finish_reason="stop"))
                self.assert_answer(step, step.events, text, "complete")
                for name in REASONING_KEYS:
                    self.assertNotIn(name.encode(), b"".join(step.events) + (step.answer_event({}) or b""))
                for representation in (repr(step), repr(mechanics)):
                    self.assertNotIn(reasoning, representation)
                    self.assertNotIn(text, representation)

    def test_finishing_chunk_records_usage_with_optional_reasoning(self) -> None:
        text = "The fictitious harbor stayed open."
        for details, reasoning_tokens in (({}, None), ({"reasoning_tokens": 233}, 233), ({"reasoning_tokens": True}, None)):
            with self.subTest(details=details):
                mechanics = AnswerMechanics(stream_state(QUESTION, guardrail.EVAL_SOURCE))
                usage: dict[str, object] = {"prompt_tokens": 4127, "completion_tokens": 831}
                if details:
                    usage["completion_tokens_details"] = details
                step = mechanics.process(with_usage(payload({"content": text}, finish_reason="stop"), usage))
                self.assert_answer(step, step.events, text, "complete")
                self.assertEqual(mechanics.usage, TokenUsage(4127, 831, reasoning_tokens))
                for value in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "4127", "831", "233"):
                    self.assertNotIn(value, repr(mechanics))

    def test_usage_only_chunk_records_counts_and_invalid_usage_preserves_them(self) -> None:
        mechanics = AnswerMechanics(stream_state(QUESTION, guardrail.EVAL_SOURCE))
        self.assertEqual(mechanics.process(payload({})), AnswerStep())
        self.assertIsNone(mechanics.usage)

        usage = {"prompt_tokens": 48, "completion_tokens": 19, "completion_tokens_details": {"reasoning_tokens": 12}}
        self.assertEqual(mechanics.process(with_usage('{"choices":[]}', usage)), AnswerStep())
        last = TokenUsage(48, 19, 12)
        self.assertEqual(mechanics.usage, last)

        for invalid in (
            None, "fictitious usage", {**usage, "prompt_tokens": -1}, {**usage, "completion_tokens": True},
            {**usage, "prompt_tokens": "48"}, {"prompt_tokens": 48},
        ):
            with self.subTest(usage=invalid):
                self.assertEqual(mechanics.process(with_usage(payload({}), invalid)), AnswerStep())
                self.assertEqual(mechanics.usage, last)
                self.assertIsNone(mechanics.failure)
                self.assertEqual(self.trips, [])

        text = "The fictitious harbor stayed open."
        step = mechanics.process(payload({"content": text}, finish_reason="stop"))
        self.assert_answer(step, step.events, text, "complete")
        self.assertEqual(mechanics.usage, last)

    def test_tripping_chunk_keeps_its_usage(self) -> None:
        mechanics = AnswerMechanics(stream_state(QUESTION, guardrail.EVAL_SOURCE))
        self.assertEqual(mechanics.process(with_usage(payload({}), {"prompt_tokens": 48, "completion_tokens": 1})), AnswerStep())
        step = mechanics.process(with_usage(
            payload({"content": "The deadline is April 7, 2034."}, finish_reason="stop"),
            {"prompt_tokens": 48, "completion_tokens": 23, "completion_tokens_details": {"reasoning_tokens": 5}},
        ))
        self.assert_tripped(step, step.events, guardrail.DEADLINE_FAMILY.name)
        self.assertEqual(step.finish_reason, "stop")
        self.assertEqual(mechanics.usage, TokenUsage(48, 23, 5))

    def test_retry_mechanics_starts_without_usage_and_keeps_its_own_counts(self) -> None:
        first = AnswerMechanics(stream_state(QUESTION, guardrail.EVAL_SOURCE))
        cut = first.process(with_usage(
            payload({}, finish_reason="length"),
            {"prompt_tokens": 48, "completion_tokens": 23, "completion_tokens_details": {"reasoning_tokens": 21}},
        ))
        self.assertEqual((cut.outcome, cut.finish_reason, cut.events), ("empty", "length", ()))
        self.assertEqual(first.usage, TokenUsage(48, 23, 21))

        retry = AnswerMechanics(stream_state(QUESTION, guardrail.EVAL_SOURCE))
        self.assertIsNone(retry.usage)
        text = "The fictitious harbor stayed open."
        finish = retry.process(with_usage(
            payload({"content": text}, finish_reason="stop"), {"prompt_tokens": 51, "completion_tokens": 8},
        ))
        self.assert_answer(finish, finish.events, text, "complete")
        self.assertEqual(retry.usage, TokenUsage(51, 8, None))
        self.assertEqual(first.usage, TokenUsage(48, 23, 21))

    def test_length_with_content_ends_truncated_after_releasing_its_tail(self) -> None:
        mechanics = AnswerMechanics(stream_state(QUESTION, guardrail.EVAL_SOURCE))
        sentence = "The fictitious harbor stayed open. "
        prefix = sentence * (guardrail.LAG_CHARS // len(sentence) + 1)
        tail = "The record ends here."
        opening = mechanics.process(payload({"content": prefix}))
        self.assertEqual(opening.outcome, "open")
        self.assertTrue(opening.events)
        finish = mechanics.process(payload({"content": tail}, finish_reason="length"))
        self.assertEqual((finish.outcome, finish.finish_reason), ("finished", "length"))
        self.assert_answer(finish, opening.events + finish.events, prefix + tail, "truncated")

    def test_a_blank_answer_at_length_finishes_empty_and_sends_nothing(self) -> None:
        for finish_reason in ("length", "stop"):
            with self.subTest(finish_reason=finish_reason):
                mechanics = AnswerMechanics(stream_state(QUESTION, guardrail.EVAL_SOURCE))
                self.assertEqual(mechanics.process(payload({"content": "\n\n"})), AnswerStep())
                step = mechanics.process(payload({"content": " "}, finish_reason=finish_reason))
                self.assertEqual((step.outcome, step.finish_reason, step.events), ("empty", finish_reason, ()))
                self.assertIsNone(step.answer_event({}))

    def test_leading_whitespace_rides_with_the_first_visible_text(self) -> None:
        mechanics = AnswerMechanics(stream_state(QUESTION, guardrail.EVAL_SOURCE))
        self.assertEqual(mechanics.process(payload({"content": "\n\n"})), AnswerStep())
        step = mechanics.process(payload({"content": "The fictitious harbor stayed open."}, finish_reason="stop"))
        self.assertEqual(step.outcome, "finished")
        self.assertEqual(len(step.events), 1)
        self.assert_answer(step, step.events, "\n\nThe fictitious harbor stayed open.", "complete")

    def test_a_trip_after_held_whitespace_sends_the_refusal_alone(self) -> None:
        mechanics = AnswerMechanics(stream_state(QUESTION, guardrail.EVAL_SOURCE))
        self.assertEqual(mechanics.process(payload({"content": "\n\n"})), AnswerStep())
        step = mechanics.fail_closed()
        refusal = guardrail.refusal_for(guardrail.DEADLINE_FAMILY.name)
        self.assertEqual([event_data(event, "text") for event in step.events], [{"text": refusal}])
        self.assert_answer(step, step.events, refusal, "tripped", family=guardrail.DEADLINE_FAMILY.name)

    def test_settle_releases_held_text_once_without_an_answer_event(self) -> None:
        for end in ("eof", DONE_EVENT):
            with self.subTest(end=end):
                mechanics = AnswerMechanics(stream_state(QUESTION, guardrail.EVAL_SOURCE))
                text = "An unfinished fictitious answer."
                self.assertEqual(mechanics.process(payload({"content": text})), AnswerStep())
                settled = mechanics.settle() if end == "eof" else mechanics.process(end)
                released, = settled.events
                self.assertEqual(event_data(released, "text"), {"text": text})
                self.assertIsNone(settled.answer_event({}))
                self.assertEqual(mechanics.settle(), AnswerStep())

    def test_attempts_get_fresh_windows(self) -> None:
        first = stream_state(QUESTION, guardrail.EVAL_SOURCE)
        cut = (FIXTURES / "generator-truncated.sse").read_bytes()
        terminal, released = self.consume(AnswerMechanics(first), EventReassembler().feed(cut))
        self.assertEqual((terminal.outcome, terminal.finish_reason, released), ("empty", "length", []))
        self.assertTrue(first.content.finished)

        second = stream_state(QUESTION, guardrail.EVAL_SOURCE)
        self.assertIsNot(second.content, first.content)
        self.assertFalse(second.content.finished)
        raw = (FIXTURES / "generator-thinking-off.sse").read_bytes()
        reassembler = EventReassembler()
        retry = [item for byte in raw for item in reassembler.feed(bytes((byte,)))]
        terminal, released = self.consume(AnswerMechanics(second), retry)
        text, finish_reason = recorded_answer(retry)
        self.assertEqual((terminal.outcome, terminal.finish_reason), ("finished", finish_reason))
        self.assertTrue(text)
        self.assert_answer(terminal, released, text, "complete")

    def test_stream_context_uses_the_question_alone(self) -> None:
        question = "Did the fictitious harbor reopen on May 4, 2034?"
        passage = "The fictitious harbor closed on April 7, 2034."
        user_message = messages(question, (("", passage),))[-1]["content"]
        state = stream_state(question, guardrail.EVAL_SOURCE, "fixture-chat")
        self.assertEqual((state.branch, state.source, state.chat_id), (LEGAL_BRANCH, guardrail.EVAL_SOURCE, "fixture-chat"))
        supplied = state.supplied[guardrail.DEADLINE_FAMILY.name]
        self.assertTrue(supplied)
        self.assertEqual(supplied, guardrail.normalized_dates(question))
        self.assertTrue(guardrail.normalized_dates(passage))
        self.assertTrue(supplied.isdisjoint(guardrail.normalized_dates(passage)))
        self.assertNotEqual(supplied, guardrail.normalized_dates(user_message))

    @pytest.mark.slow
    def test_seed_outcomes_and_trip_rows_at_each_granularity(self) -> None:
        """Positives trip once; a control trips at most at the shared judge's one-in-twenty ceiling."""

        for path, document in seed_documents():
            family = document["family"]
            assert isinstance(family, str)
            controls = 0
            tripped_controls: set[object] = set()
            for case in seed_cases(path):
                question, answer = case["prompt"], case["answer"]
                assert isinstance(question, str) and isinstance(answer, str)
                thinking = case.get("thinking", "")
                assert isinstance(thinking, str)
                positive = case["kind"] == "positive"
                controls += case["kind"] == "control"
                for granularity in (1, 7, 0):
                    with self.subTest(seed=path.name, case=case["id"], granularity=granularity):
                        self.trips.clear()
                        state = stream_state(question, guardrail.EVAL_SOURCE, "fixture-chat")
                        mechanics = AnswerMechanics(state)
                        chunks = (
                            chunk if isinstance(chunk, str) else json.dumps(chunk)
                            for chunk in seed_chunks(thinking, answer, granularity)
                        )
                        step, emitted = self.consume(mechanics, chunks)
                        self.assertIsNone(mechanics.failure)
                        if positive or step.outcome == "tripped":
                            if not positive:
                                tripped_controls.add(case["id"])
                            prefix = self.assert_tripped(step, emitted, family)
                            self.assertTrue(answer.startswith(prefix))
                            assert state.trip is not None
                            self.assertEqual(self.trips, [guardrail.TripRow(
                                LEGAL_BRANCH, family, state.trip.pattern_id, guardrail.EVAL_SOURCE, "fixture-chat",
                            )])
                        else:
                            self.assertEqual(step.outcome, "finished")
                            self.assert_answer(step, emitted, answer, "complete")
                            self.assertEqual(self.trips, [])
            with self.subTest(seed=path.name, ceiling="controls"):
                self.assertLessEqual(len(tripped_controls), controls // 20, sorted(map(str, tripped_controls)))

    def test_engine_error_releases_a_clean_tail_without_a_trip(self) -> None:
        state = stream_state(QUESTION, guardrail.EVAL_SOURCE)
        mechanics = AnswerMechanics(state)
        text = "A short answer about a fictitious harbor."
        self.assertEqual(mechanics.process(payload({"content": text})), AnswerStep())
        step = mechanics.process(ENGINE_ERROR)
        self.assertEqual(step.outcome, "engine-error")
        released, = step.events
        self.assertEqual(event_data(released, "text"), {"text": text})
        self.assertIsNone(step.answer_event({}))
        self.assertIsNone(mechanics.failure)
        self.assertEqual(mechanics.process(payload({"content": "Text after the engine failed."})), AnswerStep())
        self.assertEqual(self.trips, [])

    def test_tripping_tail_takes_precedence_over_an_engine_error_or_stream_end(self) -> None:
        sentence = "The fictitious harbor stayed open. "
        text = sentence * (guardrail.LAG_CHARS // len(sentence) + 1)
        tail = "The deadline is April 7, 2034."
        for end in (ENGINE_ERROR, DONE_EVENT, None):
            with self.subTest(end=end):
                self.trips.clear()
                state = stream_state(QUESTION, guardrail.EVAL_SOURCE)
                mechanics = AnswerMechanics(state)
                opening = mechanics.process(payload({"content": text}))
                self.assertTrue(opening.events)
                held = mechanics.process(payload({"content": tail}))
                self.assertEqual(held.outcome, "open")
                step = mechanics.settle() if end is None else mechanics.process(end)
                prefix = self.assert_tripped(step, opening.events + held.events + step.events, guardrail.DEADLINE_FAMILY.name)
                self.assertTrue(prefix and text.startswith(prefix))
                self.assertIsNone(mechanics.failure)
                self.assertEqual(len(self.trips), 1)

    def test_malformed_chunk_fails_closed_with_one_content_free_error_trip(self) -> None:
        state = stream_state(QUESTION, guardrail.EVAL_SOURCE, "fixture-chat")
        mechanics = AnswerMechanics(state)
        private = "Fictitious private payload"
        step = mechanics.process(payload({"content": {"private": private}}))
        self.assert_tripped(step, step.events, guardrail.DEADLINE_FAMILY.name)
        self.assertEqual(mechanics.failure, "TypeError")
        self.assertEqual(state.trip, guardrail.Trip(guardrail.DEADLINE_FAMILY.name, guardrail.ERROR_PATTERN_ID))
        self.assertEqual(mechanics.fail_closed(), AnswerStep())
        self.assertEqual(mechanics.settle(), AnswerStep())
        self.assertEqual(self.trips, [guardrail.TripRow(
            LEGAL_BRANCH, guardrail.DEADLINE_FAMILY.name, guardrail.ERROR_PATTERN_ID, guardrail.EVAL_SOURCE, "fixture-chat",
        )])
        for representation in (repr(step), repr(mechanics), repr(self.trips), repr(step.events)):
            self.assertNotIn(private, representation)

    def test_settlement_exception_holds_unsent_deltas_out_of_the_refusal_answer(self) -> None:
        sentence = "The fictitious harbor stayed open. "
        text = sentence * (guardrail.LAG_CHARS // len(sentence) + 1)
        for with_finish in (False, True):
            with self.subTest(with_finish=with_finish):
                self.trips.clear()
                state = stream_state(QUESTION, guardrail.EVAL_SOURCE)
                mechanics = AnswerMechanics(state)
                with patch.object(guardrail.StreamCheck, "finish", side_effect=ValueError("Fictitious failure")):
                    if with_finish:
                        step = mechanics.process(payload({"content": text}, finish_reason="stop"))
                    else:
                        self.assertEqual(mechanics.process(payload({"content": sentence})), AnswerStep())
                        step = mechanics.settle()
                self.assertEqual(self.assert_tripped(step, step.events, guardrail.DEADLINE_FAMILY.name), "")
                self.assertEqual(mechanics.failure, "ValueError")
                self.assertEqual([row.pattern_id for row in self.trips], [guardrail.ERROR_PATTERN_ID])

    def test_external_failure_fails_closed_and_preserves_an_existing_trip(self) -> None:
        for recorded in (None, guardrail.Trip(guardrail.GUIDELINES_FAMILY.name, "fixture-pattern")):
            with self.subTest(recorded=recorded):
                self.trips.clear()
                state = stream_state(QUESTION, guardrail.EVAL_SOURCE)
                if recorded is not None:
                    guardrail.record_stream_trip(state, recorded)
                mechanics = AnswerMechanics(state)
                error = SSEEventTooLargeError("Fictitious reassembly error")
                step = mechanics.fail_closed(error)
                expected = recorded or guardrail.Trip(guardrail.DEADLINE_FAMILY.name, guardrail.ERROR_PATTERN_ID)
                self.assert_tripped(step, step.events, expected.family)
                self.assertEqual(state.trip, expected)
                self.assertEqual(mechanics.failure, type(error).__name__)
                self.assertEqual([row.pattern_id for row in self.trips], [expected.pattern_id])

    def test_date_supplied_only_by_a_passage_still_trips_the_answer(self) -> None:
        question = "When is the fictional filing due?"
        passage = "The deadline is April 7, 2034."
        self.assertFalse(guardrail.normalized_dates(question))
        self.assertTrue(guardrail.normalized_dates(messages(question, (("", passage),))[-1]["content"]))
        state = stream_state(question, guardrail.EVAL_SOURCE)
        mechanics = AnswerMechanics(state)
        step = mechanics.process(payload({"content": passage}, finish_reason="stop"))
        self.assertEqual(self.assert_tripped(step, step.events, guardrail.DEADLINE_FAMILY.name), "")
        self.assertEqual(len(self.trips), 1)
