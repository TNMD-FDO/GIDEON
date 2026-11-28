"""The research turn's event payloads and wire bytes."""

import unittest
from typing import get_args

from gideon.turn import events
from gideon.turn.states import State, TurnFailure


class TurnEvents(unittest.TestCase):
    def test_answer_end_vocabulary(self) -> None:
        self.assertEqual(set(get_args(events.EndWord)), {"complete", "truncated", "tripped", "abstained"})

    def test_passage_carries_only_its_number_id_and_rank(self) -> None:
        self.assertEqual(
            events.passage(2, "fixture-chunk", 7),
            b'event: passage\ndata: {"number":2,"chunk_id":"fixture-chunk","pool_rank":7}\n\n',
        )

    def test_retrieve_phase_carries_the_retrieval_reply(self) -> None:
        retrieval = {"stage": "retrieve", "pool": [{"chunk_id": "fixture-chunk"}]}
        self.assertEqual(
            events.phase("retrieve", retrieval),
            b'event: phase\ndata: {"stage":"retrieve","retrieval":'
            b'{"stage":"retrieve","pool":[{"chunk_id":"fixture-chunk"}]}}\n\n',
        )

    def test_generate_phase_carries_the_attempt_number(self) -> None:
        self.assertEqual(
            events.phase("generate", 2),
            b'event: phase\ndata: {"stage":"generate","attempt":2}\n\n',
        )

    def test_text_carries_a_delta_on_one_data_line(self) -> None:
        self.assertEqual(
            events.text('Fictitious “quoted” text\r\nSecond line'),
            b'event: text\ndata: {"text":"Fictitious \\u201cquoted\\u201d text\\r\\nSecond line"}\n\n',
        )

    def test_answer_carries_the_displayed_text_end_and_timings(self) -> None:
        for end in ("complete", "truncated", "abstained"):
            with self.subTest(end=end):
                self.assertEqual(
                    events.answer("Fictitious answer", end, {"total": 2500.0}),
                    b'event: answer\ndata: {"text":"Fictitious answer","end":"'
                    + end.encode() + b'","timings_ms":{"total":2500.0}}\n\n',
                )

    def test_tripped_answer_carries_the_family(self) -> None:
        self.assertEqual(
            events.answer("Fictitious refusal", "tripped", {"total": 2500.0}, family="fixture-family"),
            b'event: answer\ndata: {"text":"Fictitious refusal","end":"tripped",'
            b'"family":"fixture-family","timings_ms":{"total":2500.0}}\n\n',
        )

    def test_failure_carries_the_fixed_refusal_body(self) -> None:
        self.assertEqual(
            events.failure(TurnFailure(State.GENERATOR_UNAVAILABLE, "generate")),
            b'event: failure\ndata: {"error":{"message":"The generator is unavailable.",'
            b'"type":"server_error","param":"generate","code":"generator_unavailable"}}\n\n',
        )
