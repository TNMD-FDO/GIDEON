"""Request validation and caller admission for research turns."""

import json
import unittest

from gideon.turn.admission import admit
from gideon.turn.config import RETRIEVAL_CONFIG
from gideon.turn.states import TurnFailure

IDENTITY = "eval@example.invalid"
MESSAGE = {"message": "Fictitious research question"}


class Admission(unittest.TestCase):
    def assert_refused(
        self, value: object, state: str, word: str | None = None, *, source: tuple[str, ...] = (), reranker: bool = True,
    ) -> None:
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        with self.assertRaises(TurnFailure) as raised:
            admit(body, source, IDENTITY, reranker=reranker)
        self.assertEqual((raised.exception.state, raised.exception.word), (state, word))

    def test_malformed_json_non_object_and_unknown_key_precede_caller_refusal(self) -> None:
        for value in (b'{"message":', [MESSAGE], {**MESSAGE, "fixture_unknown": "retrieve"}):
            with self.subTest(value=value):
                self.assert_refused(value, "invalid_request")

    def test_each_invalid_field_shape_precedes_eval_field_refusal(self) -> None:
        invalid_fields: dict[str, tuple[object, ...]] = {
            "message": (None, [], "  "),
            "message_id": ("Fictitious free text", 42, None),
            "history": ("Fictitious history", [False], ["Fictitious history"] * 7),
            "kb_ids": ("fixture-kb", [False]),
            "session_token": (None, []),
            "stop_after": (None, {}),
            "evidence": ("fixture-evidence", [False]),
            "overrides": (None, []),
        }
        for key, invalid_values in invalid_fields.items():
            for value in invalid_values:
                with self.subTest(key=key, value=value):
                    self.assert_refused({**MESSAGE, "stop_after": "retrieve", key: value}, "invalid_request", key)
        self.assert_refused({"stop_after": "retrieve"}, "invalid_request", "message")

    def test_non_eval_caller_is_refused_by_field_presence_in_contract_order(self) -> None:
        body: dict[str, object] = {**MESSAGE, "overrides": {"fixture_unknown": True}, "evidence": [], "stop_after": ""}
        for field in ("stop_after", "evidence", "overrides"):
            with self.subTest(field=field):
                self.assert_refused({**MESSAGE, field: body[field]}, "eval_only_field", field, source=("reader@example.invalid",))
                self.assert_refused(body, "eval_only_field", field, source=("reader@example.invalid",))
            del body[field]
        self.assert_refused(body, "caller_not_admitted", source=("reader@example.invalid",))
        self.assert_refused(
            {**body, "message_id": "fixture-message_42"}, "caller_not_admitted", source=("reader@example.invalid",),
        )

    def test_eval_override_validation_precedes_stage_and_knowledge_base_refusals(self) -> None:
        body = {**MESSAGE, "evidence": [], "kb_ids": ["fixture-kb"]}
        self.assert_refused(
            {**body, "overrides": {"fixture_unknown": True}}, "override_not_allowed", "overrides", source=(IDENTITY,),
        )
        self.assert_refused(
            {**body, "overrides": {"pool_cap": 0}}, "override_invalid", "pool_cap", source=(IDENTITY,),
        )
        self.assert_refused(body, "stage_not_built", "evidence", source=(IDENTITY,))
        self.assert_refused({**body, "stop_after": "generate"}, "stage_not_built", "stop_after", source=(IDENTITY,))
        self.assert_refused({**body, "stop_after": "retrieve"}, "stage_not_built", "evidence", source=(IDENTITY,))
        for fields in ({}, {"stop_after": "retrieve"}):
            with self.subTest(fields=fields):
                self.assert_refused(
                    {**MESSAGE, **fields, "kb_ids": body["kb_ids"]},
                    "kb_ids_not_built", "kb_ids", source=(IDENTITY,),
                )

    def test_eval_request_without_a_stop_stage_is_admitted(self) -> None:
        request, config = admit(json.dumps(MESSAGE).encode(), (IDENTITY,), IDENTITY, reranker=True)
        self.assertIsNone(request.stop_after)
        self.assertIsNone(request.message_id)
        self.assertEqual(request.message, MESSAGE["message"])
        self.assertEqual(config, RETRIEVAL_CONFIG)

    def test_eval_request_keeps_valid_fields_and_uses_the_release_config(self) -> None:
        history = ["Fictitious earlier question"] * 6
        body = {
            **MESSAGE, "history": history, "kb_ids": [], "session_token": "fixture-session", "stop_after": "retrieve",
            "message_id": "fixture-message_42",
        }
        request, config = admit(json.dumps(body).encode(), (f" {IDENTITY.upper()} ",), IDENTITY, reranker=True)
        self.assertEqual(request.message, MESSAGE["message"])
        self.assertEqual(request.message_id, body["message_id"])
        self.assertEqual(request.history, tuple(history))
        self.assertEqual(request.kb_ids, ())
        self.assertEqual(request.session_token, body["session_token"])
        self.assertEqual(request.stop_after, body["stop_after"])
        self.assertEqual(config, RETRIEVAL_CONFIG)

    def test_stage2_override_requires_a_reranker(self) -> None:
        body = {**MESSAGE, "overrides": {"rerank.stage2": "on"}}
        _, config = admit(json.dumps(body).encode(), (IDENTITY,), IDENTITY, reranker=True)
        self.assertEqual(config["rerank.stage2"], "on")
        self.assert_refused(body, "override_invalid", "rerank.stage2", source=(IDENTITY,), reranker=False)

        _, config = admit(json.dumps(MESSAGE).encode(), (IDENTITY,), IDENTITY, reranker=False)
        self.assertEqual(config["rerank.stage2"], "off")
