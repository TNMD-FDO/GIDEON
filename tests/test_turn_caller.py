"""The turn's caller rule, its fail-closed re-check, and the floor, over a fake frontend adapter."""

import asyncio
import unittest
from dataclasses import replace
from typing import cast
from unittest.mock import AsyncMock, Mock, call, patch

from gideon.turn import frontend
from gideon.turn.caller import (
    Caller,
    CallerRefusal,
    Denial,
    DenialReason,
    Floor,
    admit_caller,
)
from gideon.turn.generation import TURN_COLLECTION
from gideon.turn.states import State

TOKEN = "fixture.session.secret"
BAD_TOKEN = "sk-fictitious-key"
USER_ID = "fixture-user"
KB_IDS = ("fixture-kb",)


class CallerRule(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = Mock()
        self.session = AsyncMock(return_value=(USER_ID, 200))
        self.knowledge = AsyncMock(return_value=(True, 200))
        self.enterContext(patch.object(frontend, "read_session", self.session))
        self.enterContext(patch.object(frontend, "read_knowledge", self.knowledge))

    async def test_claim_token_and_named_ids_decide_admission(self) -> None:
        collections = frozenset({TURN_COLLECTION})
        empty_floor = Floor(collections, frozenset())
        named_floor = Floor(collections, frozenset(KB_IDS))
        eval_caller = Caller("eval", None, empty_floor)
        verified_user = Caller("user", USER_ID, empty_floor)
        user_with_grant = Caller("user", USER_ID, named_floor)
        eval_with_grant = Caller("eval", USER_ID, named_floor)
        no_token = (State.CALLER_NOT_ADMITTED, None, None)
        malformed = (State.SESSION_NOT_VERIFIED, "session_token", None)
        no_token_with_kb = (State.CALLER_NOT_ADMITTED, None, Denial("no_session", None, None, KB_IDS, ()))
        malformed_with_kb = (
            State.SESSION_NOT_VERIFIED, "session_token", Denial("session_not_verified", None, "invalid", KB_IDS, ()),
        )
        for claim, token, named, expected in (
            ("eval", None, (), eval_caller),
            ("eval", BAD_TOKEN, (), eval_caller),
            ("eval", TOKEN, (), eval_caller),
            ("eval", None, KB_IDS, no_token_with_kb),
            ("eval", BAD_TOKEN, KB_IDS, malformed_with_kb),
            ("eval", TOKEN, KB_IDS, eval_with_grant),
            ("user", None, (), no_token),
            ("user", BAD_TOKEN, (), malformed),
            ("user", TOKEN, (), verified_user),
            ("user", None, KB_IDS, no_token_with_kb),
            ("user", BAD_TOKEN, KB_IDS, malformed_with_kb),
            ("user", TOKEN, KB_IDS, user_with_grant),
        ):
            with self.subTest(claim=claim, token=token, named=named):
                self.session.reset_mock()
                self.knowledge.reset_mock()
                if isinstance(expected, Caller):
                    result = await admit_caller(claim, token, named, self.client)
                    self.assertEqual(result, expected)
                else:
                    with self.assertRaises(CallerRefusal) as raised:
                        await admit_caller(claim, token, named, self.client)
                    failure = raised.exception
                    self.assertEqual((failure.state, failure.word, failure.denial), expected)

                if isinstance(expected, Caller) and expected.user_id is not None:
                    self.session.assert_awaited_once_with(self.client, token)
                else:
                    self.session.assert_not_called()
                if expected in (user_with_grant, eval_with_grant):
                    self.knowledge.assert_awaited_once_with(self.client, token, KB_IDS[0])
                else:
                    self.knowledge.assert_not_called()

    async def test_failed_session_reply_is_kept_only_for_a_request_naming_knowledge(self) -> None:
        cases: tuple[tuple[frontend.ReplyWord, DenialReason, State, str], ...] = (
            (401, "session_not_verified", State.SESSION_NOT_VERIFIED, "session_token"),
            ("invalid", "session_not_verified", State.SESSION_NOT_VERIFIED, "session_token"),
            (503, "frontend_unavailable", State.FRONTEND_UNAVAILABLE, "admission"),
            ("none", "frontend_unavailable", State.FRONTEND_UNAVAILABLE, "admission"),
        )
        for reply, reason, state, word in cases:
            for named in ((), KB_IDS):
                with self.subTest(reply=reply, named=named):
                    self.session.return_value = (None, reply)
                    with self.assertRaises(CallerRefusal) as raised:
                        await admit_caller("user", TOKEN, named, self.client)
                    error = raised.exception
                    self.assertEqual((error.state, error.word), (state, word))
                    self.assertEqual(error.denial, Denial(reason, None, reply, named, ()) if named else None)
                    self.knowledge.assert_not_called()

    async def test_all_reads_run_concurrently_and_failures_keep_named_order(self) -> None:
        first, granted, last = "fixture-first", "fixture-granted", "fixture-last"
        named = (first, granted, last)
        later_answered = asyncio.Event()

        async def reply(_client: object, _token: str, kb_id: str) -> tuple[bool, frontend.ReplyWord]:
            if kb_id == first:
                await later_answered.wait()
                return False, 401
            if kb_id == last:
                later_answered.set()
                return False, 404
            return True, 200

        self.knowledge.side_effect = reply
        with self.assertRaises(CallerRefusal) as raised:
            await admit_caller("user", TOKEN, named, self.client)
        self.assertEqual(self.knowledge.await_count, len(named))
        self.knowledge.assert_has_awaits([call(self.client, TOKEN, kb_id) for kb_id in named], any_order=True)
        error = raised.exception
        self.assertEqual((error.state, error.status, error.word), (State.KB_ACCESS_DENIED, 403, "kb_ids"))
        self.assertEqual(error.denial, Denial("kb_not_readable", USER_ID, None, named, ((first, 401), (last, 404))))
        self.assertNotIn(first, repr(error.to_body()))
        self.assertNotIn("kb_not_readable", repr(error.to_body()))

    async def test_unavailable_knowledge_reply_takes_priority_over_a_denial(self) -> None:
        forbidden, offline = "fixture-forbidden", "fixture-offline"
        named = (forbidden, offline)
        unavailable_replies: tuple[frontend.ReplyWord, ...] = (503, "none")
        for unavailable in unavailable_replies:
            with self.subTest(unavailable=unavailable):
                self.knowledge.side_effect = [(False, 401), (False, unavailable)]
                with self.assertRaises(CallerRefusal) as raised:
                    await admit_caller("user", TOKEN, named, self.client)
                error = raised.exception
                self.assertEqual((error.state, error.word), (State.FRONTEND_UNAVAILABLE, "admission"))
                self.assertEqual(
                    error.denial, Denial("frontend_unavailable", USER_ID, None, named, ((forbidden, 401), (offline, unavailable))),
                )

    async def test_knowledge_deadline_keeps_each_answered_failure_and_marks_unanswered_ids(self) -> None:
        granted, slow, offline = "fixture-granted", "fixture-slow", "fixture-offline"
        named = (granted, slow, offline)
        finished = asyncio.Event()

        async def reply(_client: object, _token: str, kb_id: str) -> tuple[bool, frontend.ReplyWord]:
            if kb_id == slow:
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    finished.set()
                    return False, 404  # A result arriving during cancellation missed the deadline.
            return (True, 200) if kb_id == granted else (False, 503)

        self.knowledge.side_effect = reply
        with self.assertRaises(CallerRefusal) as raised:
            await admit_caller("eval", TOKEN, named, self.client, timeout=0.05)
        self.session.assert_awaited_once_with(self.client, TOKEN)
        self.assertEqual(self.knowledge.await_count, len(named))
        self.knowledge.assert_has_awaits([call(self.client, TOKEN, kb_id) for kb_id in named], any_order=True)
        self.assertTrue(finished.is_set())
        error = raised.exception
        self.assertEqual((error.state, error.word), (State.FRONTEND_UNAVAILABLE, "admission"))
        self.assertEqual(error.denial, Denial("frontend_unavailable", USER_ID, None, named, ((slow, "none"), (offline, 503))))

    async def test_session_deadline_refuses_before_any_knowledge_read(self) -> None:
        async def stalled(_client: object, _token: str) -> None:
            await asyncio.Future()

        async def late(_client: object, _token: str) -> tuple[str, int]:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                return USER_ID, 200
            raise AssertionError("The stalled session unexpectedly resumed.")

        for answer in (stalled, late):
            self.session.side_effect = answer
            for named in ((), KB_IDS):
                with self.subTest(answer=answer.__name__, named=named):
                    with self.assertRaises(CallerRefusal) as raised:
                        await admit_caller("user", TOKEN, named, self.client, timeout=0)
                    error = raised.exception
                    self.assertEqual((error.state, error.word), (State.FRONTEND_UNAVAILABLE, "admission"))
                    self.assertEqual(error.denial, Denial("frontend_unavailable", None, "none", named, ()) if named else None)
                    self.knowledge.assert_not_called()

    async def test_admission_values_keep_grants_and_leave_the_token_out_of_reprs(self) -> None:
        named = (*KB_IDS, "fixture-second-kb")
        result = await admit_caller("user", TOKEN, named, self.client)
        self.assertEqual(result.floor.collections, frozenset({TURN_COLLECTION}))
        self.assertEqual(result.floor.granted, frozenset(named))

        self.knowledge.return_value = (False, 401)
        with self.assertRaises(CallerRefusal) as raised:
            await admit_caller("user", TOKEN, named, self.client)
        error = raised.exception
        self.assertIsNotNone(error.denial)
        for value in (result, result.floor, error.denial, error):
            self.assertNotIn(TOKEN, repr(value))


class DenialVocabulary(unittest.TestCase):
    def test_outside_reason_and_reply_words_are_refused(self) -> None:
        valid = Denial("kb_not_readable", USER_ID, None, KB_IDS, ((KB_IDS[0], 401),))
        with self.assertRaisesRegex(ValueError, "Unknown denial reason"):
            replace(valid, reason=cast(DenialReason, TOKEN))
        for value in (TOKEN, True, 99, 1000, 401.0):
            with self.subTest(value=value):
                reply = cast(frontend.ReplyWord, value)
                with self.assertRaisesRegex(ValueError, "Invalid session reply word"):
                    replace(valid, session_reply=reply)
                with self.assertRaisesRegex(ValueError, "Invalid knowledge-base reply word"):
                    replace(valid, failures=((KB_IDS[0], reply),))
