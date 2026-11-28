"""The pure request, claim, and settlement steps for research turns, in their refusal order."""

import json
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields

from gideon.turn.admission import (
    _GENERATION_NAME_LIMIT,
    _KB_IDS_LIMIT,
    TurnRequest,
    claim,
    parse,
    settle,
)
from gideon.turn.config import RETRIEVAL_CONFIG
from gideon.turn.states import TurnFailure

IDENTITY = "eval@example.invalid"
MESSAGE = {"message": "Fictitious research question"}


def _request(value: object) -> TurnRequest:
    return parse(json.dumps(value).encode())


class Admission(unittest.TestCase):
    @contextmanager
    def assert_refused(self, state: str, word: str | None = None) -> Iterator[None]:
        with self.assertRaises(TurnFailure) as raised:
            yield
        self.assertEqual((raised.exception.state, raised.exception.word), (state, word))

    def test_parse_refuses_malformed_json_non_object_and_unknown_keys(self) -> None:
        for body in (b'{"message":', json.dumps([MESSAGE]).encode(), b'{"fixture_unknown":"retrieve"}'):
            with self.subTest(body=body), self.assert_refused("invalid_request"):
                parse(body)

    def test_each_invalid_field_shape_is_refused_during_parse_even_with_an_eval_field(self) -> None:
        invalid_fields: dict[str, tuple[object, ...]] = {
            "message": (None, [], "  "),
            "message_id": ("Fictitious free text", 42, None),
            "history": ("Fictitious history", [False], ["Fictitious history"] * 7),
            "kb_ids": ("fixture-kb", [False], [""], ["fixture/kb"], ["fixture KB"], ["x" * 129]),
            "session_token": (None, []),
            "stop_after": (None, {}),
            "evidence": ("fixture-evidence", [False]),
            "overrides": (None, []),
            "generation": (None, [], 42, "", "x" * (_GENERATION_NAME_LIMIT + 1)),
        }
        for key, invalid_values in invalid_fields.items():
            for value in invalid_values:
                with self.subTest(key=key, value=value), self.assert_refused("invalid_request", key):
                    _request({**MESSAGE, "stop_after": "retrieve", key: value})
        with self.assert_refused("invalid_request", "message"):
            _request({"stop_after": "retrieve"})

    def test_parse_folds_knowledge_base_ids_in_first_order_and_bounds_the_distinct_count(self) -> None:
        ids = [f"fixture-kb_{index}" for index in range(_KB_IDS_LIMIT)]
        repeated = [ids[0], ids[1], ids[0], *ids[2:], ids[1]]
        request = _request({**MESSAGE, "kb_ids": repeated})
        self.assertEqual(request.kb_ids, tuple(ids))
        with self.assert_refused("invalid_request", "kb_ids"):
            _request({**MESSAGE, "kb_ids": [*repeated, "fixture-extra_kb"]})

    def test_claim_refuses_the_first_eval_field_for_a_user(self) -> None:
        body: dict[str, object] = {
            **MESSAGE, "overrides": {"fixture_unknown": True}, "evidence": [], "stop_after": "",
            "generation": "fixture-generation",
        }
        sources = ("reader@example.invalid",)
        for field in ("stop_after", "evidence", "overrides", "generation"):
            with self.subTest(field=field):
                for value in ({**MESSAGE, field: body[field]}, body):
                    request = _request(value)
                    with self.assert_refused("eval_only_field", field):
                        claim(request, sources, IDENTITY)
            del body[field]
        self.assertEqual(claim(_request(body), sources, IDENTITY), "user")
        request = _request({**body, "message_id": "fixture-message_42"})
        self.assertEqual(claim(request, sources, IDENTITY), "user")

    def test_parse_keeps_a_generation_name_at_the_length_bound(self) -> None:
        for name in ("?", "Fictitious generation".ljust(_GENERATION_NAME_LIMIT, "x")):
            with self.subTest(length=len(name)):
                request = _request({**MESSAGE, "generation": name})
                self.assertEqual(request.generation, name)
                self.assertEqual(claim(request, (IDENTITY,), IDENTITY), "eval")

    def test_claim_uses_a_single_matching_source_header(self) -> None:
        request = _request({**MESSAGE, "kb_ids": ["fixture-kb"]})
        self.assertEqual(claim(request, (f" {IDENTITY.upper()} ",), IDENTITY), "eval")
        for values in ((), ("",), (IDENTITY, IDENTITY), ("reader@example.invalid",)):
            with self.subTest(values=values):
                self.assertEqual(claim(request, values, IDENTITY), "user")

    def test_settle_checks_overrides_stage_and_evidence_before_knowledge_bases(self) -> None:
        body = {**MESSAGE, "evidence": [], "kb_ids": ["fixture-kb"]}
        for request_fields, state, word in (
            ({**body, "overrides": {"fixture_unknown": True}}, "override_not_allowed", "overrides"),
            ({**body, "overrides": {"pool_cap": 0}}, "override_invalid", "pool_cap"),
            ({**body, "stop_after": "generate"}, "stage_not_built", "stop_after"),
            ({**body, "stop_after": "retrieve"}, "stage_not_built", "evidence"),
            (body, "stage_not_built", "evidence"),
            ({**MESSAGE, "kb_ids": body["kb_ids"]}, "kb_ids_not_built", "kb_ids"),
            ({**MESSAGE, "kb_ids": body["kb_ids"], "stop_after": "retrieve"}, "kb_ids_not_built", "kb_ids"),
        ):
            with self.subTest(request_fields=request_fields):
                request = _request(request_fields)
                self.assertEqual(claim(request, (IDENTITY,), IDENTITY), "eval")
                with self.assert_refused(state, word):
                    settle(request, reranker=True)

    def test_eval_request_without_a_stop_stage_settles_to_the_release_config(self) -> None:
        request = _request(MESSAGE)
        self.assertIsNone(request.stop_after)
        self.assertIsNone(request.message_id)
        self.assertIsNone(request.generation)
        self.assertEqual(request.message, MESSAGE["message"])
        self.assertEqual(claim(request, (IDENTITY,), IDENTITY), "eval")
        self.assertEqual(settle(request, reranker=True), RETRIEVAL_CONFIG)

    def test_eval_request_keeps_valid_fields_and_uses_the_release_config(self) -> None:
        history = ["Fictitious earlier question"] * 6
        body = {
            **MESSAGE, "history": history, "kb_ids": [], "session_token": "fixture-session", "stop_after": "retrieve",
            "message_id": "fixture-message_42",
        }
        request = _request(body)
        self.assertEqual(request.message, MESSAGE["message"])
        self.assertEqual(request.message_id, body["message_id"])
        self.assertEqual(request.history, tuple(history))
        self.assertEqual(request.kb_ids, ())
        self.assertEqual(request.session_token, body["session_token"])
        self.assertEqual(request.stop_after, body["stop_after"])
        self.assertEqual(claim(request, (IDENTITY,), IDENTITY), "eval")
        self.assertEqual(settle(request, reranker=True), RETRIEVAL_CONFIG)

    def test_stage2_override_requires_a_reranker(self) -> None:
        request = _request({**MESSAGE, "overrides": {"rerank.stage2": "on"}})
        self.assertEqual(settle(request, reranker=True)["rerank.stage2"], "on")
        with self.assert_refused("override_invalid", "rerank.stage2"):
            settle(request, reranker=False)
        self.assertEqual(settle(_request(MESSAGE), reranker=False)["rerank.stage2"], "off")

    def test_repr_has_no_fields_and_the_copy_has_no_token(self) -> None:
        token = "fixture.session.token"
        request = _request({
            **MESSAGE, "history": ["Fictitious earlier question"], "kb_ids": ["fixture-kb"],
            "session_token": token, "stop_after": "retrieve", "evidence": ["fixture-evidence"],
            "overrides": {"pool_cap": 5}, "message_id": "fixture-message_42",
            "generation": "fixture-generation",
        })
        stripped = request.without_token()
        self.assertIsNot(stripped, request)
        self.assertIsNone(stripped.session_token)
        self.assertEqual(request.session_token, token)
        for field in fields(request):
            with self.subTest(field=field.name):
                value = getattr(request, field.name)
                for rendered in (repr(request), str(request), repr(stripped), str(stripped)):
                    self.assertNotIn(field.name, rendered)
                    self.assertNotIn(str(value), rendered)
                if field.name != "session_token":
                    self.assertEqual(getattr(stripped, field.name), value)
