"""Content-free audit rows built for conflicts-wall denials."""

import unittest
from uuid import UUID

from gideon.turn import turnlog
from gideon.turn.caller import Denial
from gideon.turn.denial import DENIAL_KIND, build_row

USER_ID = "fixture-user"
CHAT_ID = "fixture-chat"
KB_IDS = ("fixture-first", "fixture-second")
KB_NAME = "Fictitious knowledge base name"


class DenialRows(unittest.TestCase):
    def test_each_reason_builds_an_incident_with_a_fresh_run_id(self) -> None:
        cases: tuple[tuple[Denial, str, str | None, dict[str, str]], ...] = (
            (Denial("no_session", None, None, KB_IDS, ()), "eval", None, {}),
            (Denial("session_not_verified", None, "invalid", KB_IDS, ()), "user", CHAT_ID, {"session_reply": "invalid"}),
            (Denial("frontend_unavailable", None, "none", KB_IDS, ()), "user", CHAT_ID, {"session_reply": "none"}),
            (
                Denial("frontend_unavailable", USER_ID, None, KB_IDS, ((KB_IDS[1], 503),)),
                "user", CHAT_ID, {},
            ),
            (
                Denial("kb_not_readable", USER_ID, None, KB_IDS, ((KB_IDS[0], 401), (KB_IDS[1], 404))),
                "eval", CHAT_ID, {},
            ),
        )
        run_ids: set[UUID] = set()
        for denial, claim, chat_id, session_detail in cases:
            with self.subTest(reason=denial.reason, has_user=denial.user_id is not None):
                row = build_row(denial, claim, chat_id)
                self.assertEqual(row.run_id.version, 4)
                self.assertNotIn(row.run_id, run_ids)
                run_ids.add(row.run_id)
                expected_detail = {
                    "reason": denial.reason,
                    "result": "denied",
                    "claim": claim,
                    "failures": [{"kb_id": kb_id, "reply": reply} for kb_id, reply in denial.failures],
                    **session_detail,
                }
                self.assertEqual(row.detail, expected_detail)
                self.assertEqual(row.release, turnlog.product_version())
                self.assertEqual((row.user_id, row.chat_id, row.kb_ids), (denial.user_id, chat_id, KB_IDS))
                self.assertEqual((row.kind, row.log_reason), (DENIAL_KIND, denial.reason))

    def test_private_text_is_absent_from_the_built_row(self) -> None:
        denial = Denial("kb_not_readable", USER_ID, None, KB_IDS, ((KB_IDS[0], 401),))
        row = build_row(denial, "user", KB_NAME)
        self.assertIsNone(row.chat_id)
        self.assertNotIn(KB_NAME, repr(row))
