"""Research audit rows from the turn's kept values and the admitted caller's identifiers."""

import unittest
from types import SimpleNamespace
from typing import get_args
from unittest.mock import patch
from uuid import UUID

from test_turn_endpoint import FULL_TURN, TurnEndpointFixture

from gideon.turn import objects, turnaudit, turnlog
from gideon.turn.answer import LEGAL_BRANCH
from gideon.turn.finalset import FinalHit
from gideon.turn.states import State


class TurnAudit(unittest.TestCase):
    def setUp(self) -> None:
        self.turn = turnlog.TurnLog(
            source="user", chat_id="fixture-chat", message_id="fixture-message",
            stop_after=None, retrieval_config_hash="fixture-config-hash", overrides=None,
            plan_mode="off", corpus_lockfiles=("fixture-corpus",),
            outcome="complete", timings_ms={"total": 12.5},
        )

    def test_detail_contains_the_kept_values_and_sorted_grants(self) -> None:
        values = turnlog.kept_values(self.turn, elapsed_ms=lambda: 17.0)
        values["support_level"] = "thin"
        row = turnaudit.build_row(
            values, sent_count=0, claim="user", branch=LEGAL_BRANCH,
            user_id="fixture-user", kb_ids={"fixture-z", "fixture-a"},
        )
        self.assertEqual(row.run_id.version, 4)
        self.assertEqual(row.kind, turnaudit.TURN_KIND)
        self.assertEqual(row.release, turnlog.product_version())
        self.assertEqual((row.user_id, row.chat_id, row.kb_ids), ("fixture-user", self.turn.chat_id, ("fixture-a", "fixture-z")))
        self.assertEqual(row.detail, {
            "result": "allowed",
            "claim": "user",
            "branch": LEGAL_BRANCH,
            "route": "answer",
            "outcome": self.turn.outcome,
            "failure_state": None,
            "message_id": self.turn.message_id,
            "plan_mode": self.turn.plan_mode,
            "corpus_lockfiles": list(self.turn.corpus_lockfiles or ()),
            "returned": [],
            "support_level": values["support_level"],
            "gate_counts": None,
            "latency_ms": self.turn.timings_ms["total"],
        })

    def test_each_outcome_keeps_one_row_for_the_eval_claim(self) -> None:
        self.turn.source = "eval"
        self.turn.failure_state = State.STORE_UNAVAILABLE
        run_ids: set[UUID] = set()
        for outcome in (*get_args(turnlog.Outcome), None):
            with self.subTest(outcome=outcome):
                self.turn.outcome = outcome
                self.turn.stop_after = "retrieve" if outcome == "reply" else None
                values = turnlog.kept_values(self.turn, elapsed_ms=lambda: 17.0)
                row = turnaudit.build_row(
                    values, sent_count=0, claim="eval", branch=LEGAL_BRANCH,
                    user_id=None, kb_ids=(),
                )
                expected = outcome or "disconnected"
                self.assertEqual(row.detail["outcome"], expected)
                self.assertEqual(row.log_reason, expected)
                self.assertEqual(row.detail["route"], "retrieve" if outcome == "reply" else "answer")
                self.assertEqual(row.detail["failure_state"], State.STORE_UNAVAILABLE.value if outcome == "failed" else None)
                self.assertEqual(row.detail["claim"], self.turn.source)
                self.assertIsNone(row.user_id)
                self.assertNotIn(row.run_id, run_ids)
                run_ids.add(row.run_id)

    def test_only_the_sent_prefix_of_evidence_is_returned(self) -> None:
        self.turn.evidence = [
            FinalHit(number, f"fixture-chunk-{number}", number, number, None, "state_home",
                     "score", None, f"fixture-doc-{number % 2}", 3)
            for number in range(1, 4)
        ]
        values = turnlog.kept_values(self.turn, elapsed_ms=lambda: 17.0)
        expected = [{"chunk_id": hit.chunk_id, "doc_id": hit.doc_id} for hit in self.turn.evidence]
        for sent_count in (0, 1, 2, len(expected)):
            with self.subTest(sent_count=sent_count):
                row = turnaudit.build_row(
                    values, sent_count=sent_count, claim="user", branch=LEGAL_BRANCH,
                    user_id="fixture-user", kb_ids=(),
                )
                self.assertEqual(row.detail["returned"], expected[:sent_count])

    def test_audit_row_has_no_message_passage_or_token_text(self) -> None:
        message = "Fictitious question about a violet harbor"
        passage = "Fictitious passage about a yellow ship"
        token = "Fictitious session token"
        self.turn.message_id = message
        hit = FinalHit(1, "fixture-chunk", 1, 1, None, "state_home", "score", None, "fixture-doc", 3)
        self.turn.evidence = [SimpleNamespace(**hit.to_wire(), text=passage)]
        self.turn.engine_counters = [turnlog.AttemptCounters(True, finish_reason=token)]
        values = turnlog.kept_values(self.turn, elapsed_ms=lambda: 17.0)
        row = turnaudit.build_row(
            values, sent_count=1, claim="user", branch=LEGAL_BRANCH,
            user_id="fixture-user", kb_ids=(),
        )
        for private in (message, passage, token):
            with self.subTest(private=private):
                self.assertNotIn(private, repr(row))
        self.assertEqual(row.detail["returned"], [{"chunk_id": hit.chunk_id, "doc_id": hit.doc_id}])


class GateCounts(TurnEndpointFixture):
    def test_a_full_turn_leaves_gate_report_null_until_counts_are_projected(self) -> None:
        with patch.object(objects, "extract_objects", return_value=()):
            response = self.request(payload=FULL_TURN)
        self.assertEqual(response.status_code, 200, response.text)
        row = self.turn_row()
        self.assertEqual(row["outcome"], "complete")
        self.assertIsNone(row["gate_report"], "Project gate_report into the audit row's gate_counts in slice-4 ticket 18.")
