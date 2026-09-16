"""Contracts for the shared managed-turn recipe over a fake frontend (§1.5)."""

import unittest
from collections.abc import Mapping
from typing import Any, cast

from gideon.host import owui, owuiturn
from gideon.host.owui import OwuiError, Response
from gideon.host.report import Problem

PASSWORD = "fixture-password"
USER_ID = "fixture-user-id"
ASSISTANT_ID = "fixture-assistant-id"
CHAT_ID = "fixture-chat-id"


class FakeClient(owui.Client):
    """Dict-backed frontend client recording requests and sign-in arguments."""

    def __init__(self, responses: Mapping[tuple[str, str], object] | None = None) -> None:
        self.responses = dict(responses or {})
        self.requests: list[tuple[str, str, object | None]] = []
        self.signin_args: tuple[str, str] | None = None

    def request(self, method: str, path: str, body: object | None = None) -> Response:
        self.requests.append((method, path, body))
        response = self.responses[(method, path)]
        if isinstance(response, BaseException):
            raise response
        assert isinstance(response, Response)
        return response

    def signin(self, email: str, password: str) -> str:
        self.signin_args = (email, password)
        return "fixture-session-token"


def history(
    *,
    user_id: str = USER_ID,
    assistant_id: str = ASSISTANT_ID,
    assistant: Mapping[str, object] | None = None,
    current_id: str = ASSISTANT_ID,
) -> Response:
    assistant_message = {
        "id": assistant_id,
        "role": "assistant",
        "content": "fixture answer",
        "parentId": user_id,
        "done": True,
    }
    if assistant is not None:
        assistant_message.update(assistant)
    return Response(
        200,
        {
            "chat": {
                "history": {
                    "currentId": current_id,
                    "messages": {
                        user_id: {"id": user_id, "role": "user", "content": "fixture prompt"},
                        assistant_id: assistant_message,
                    },
                }
            }
        },
    )


class SigninTests(unittest.TestCase):
    def test_signin_uses_a_fresh_client_and_returns_its_token(self) -> None:
        clients: list[FakeClient] = []

        def factory(**_: Any) -> FakeClient:
            client = FakeClient()
            clients.append(client)
            return client

        token = owuiturn.signin(factory, "fixture@example.invalid", PASSWORD)

        self.assertEqual(token, "fixture-session-token")
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].signin_args, ("fixture@example.invalid", PASSWORD))


class ListingTests(unittest.TestCase):
    def test_chat_ids_accepts_the_frontend_list_shape(self) -> None:
        client = FakeClient(
            {("GET", owuiturn._CHAT_LIST_PATH): Response(200, [{"id": CHAT_ID}])}
        )

        self.assertEqual(owuiturn.chat_ids(client), frozenset({CHAT_ID}))

    def test_chat_ids_refuses_bad_shapes_and_http_failures(self) -> None:
        cases = (
            Response(200, {}),
            Response(200, ["not a chat"]),
            Response(200, [{"id": ""}]),
            Response(503, None),
            OwuiError("fixture frontend transport failed"),
        )
        for response in cases:
            with self.subTest(response=type(response).__name__):
                client = FakeClient({("GET", owuiturn._CHAT_LIST_PATH): response})
                with self.assertRaises(OwuiError) as raised:
                    owuiturn.chat_ids(client)
                self.assertTrue(raised.exception.fix)
                self.assertNotIn(PASSWORD, str(raised.exception))
                self.assertNotIn(PASSWORD, repr(raised.exception))


class ManagedTurnTests(unittest.TestCase):
    def test_managed_turn_sends_the_exact_new_chat_body_and_requires_null(self) -> None:
        client = FakeClient({("POST", owuiturn.COMPLETIONS_PATH): Response(200, None)})
        prompt = "fixture prompt"

        problem = owuiturn.managed_turn(
            client,
            model="fixture-model",
            prompt=prompt,
            user_id=USER_ID,
            assistant_id=ASSISTANT_ID,
            timestamp=123456,
        )

        self.assertIsNone(problem)
        self.assertEqual(
            client.requests,
            [
                (
                    "POST",
                    owuiturn.COMPLETIONS_PATH,
                    {
                        "model": "fixture-model",
                        "stream": True,
                        "messages": [{"role": "user", "content": prompt}],
                        "parent_id": None,
                        "user_message": {
                            "id": USER_ID,
                            "role": "user",
                            "content": prompt,
                            "parentId": None,
                            "childrenIds": [ASSISTANT_ID],
                            "timestamp": 123456,
                            "models": ["fixture-model"],
                        },
                        "id": ASSISTANT_ID,
                    },
                )
            ],
        )
        body = client.requests[0][2]
        self.assertIsInstance(body, dict)
        assert isinstance(body, dict)
        self.assertNotIn("session_id", body)
        self.assertNotIn("background_tasks", body)

    def test_managed_turn_refuses_non_null_http_and_transport_failures(self) -> None:
        cases = (
            (Response(200, {"unexpected": True}), "non-null response body"),
            (Response(502, None), "HTTP 502"),
            (OwuiError("fixture frontend transport failed"), "fixture frontend transport failed"),
        )
        for response, fragment in cases:
            with self.subTest(fragment=fragment):
                client = FakeClient({("POST", owuiturn.COMPLETIONS_PATH): response})
                problem = owuiturn.managed_turn(
                    client,
                    model="fixture-model",
                    prompt="fixture prompt",
                    user_id=USER_ID,
                    assistant_id=ASSISTANT_ID,
                    timestamp=123456,
                )
                self.assertIsInstance(problem, Problem)
                assert problem is not None
                self.assertIn(fragment, problem.problem)
                self.assertTrue(problem.fix)
                self.assertNotIn(PASSWORD, problem.problem)
                self.assertNotIn(PASSWORD, repr(problem))

    def test_managed_turn_carries_the_optional_feature_mapping(self) -> None:
        client = FakeClient({("POST", owuiturn.COMPLETIONS_PATH): Response(200, None)})

        problem = owuiturn.managed_turn(
            client,
            model="fixture-model",
            prompt="fixture prompt",
            user_id=USER_ID,
            assistant_id=ASSISTANT_ID,
            timestamp=123456,
            features={"web_search": True},
        )

        self.assertIsNone(problem)
        body = client.requests[0][2]
        self.assertIsInstance(body, dict)
        assert isinstance(body, dict)
        self.assertEqual(
            body,
            {
                "model": "fixture-model",
                "stream": True,
                "messages": [{"role": "user", "content": "fixture prompt"}],
                "parent_id": None,
                "user_message": {
                    "id": USER_ID,
                    "role": "user",
                    "content": "fixture prompt",
                    "parentId": None,
                    "childrenIds": [ASSISTANT_ID],
                    "timestamp": 123456,
                    "models": ["fixture-model"],
                },
                "id": ASSISTANT_ID,
                "features": {"web_search": True},
            },
        )


class FinderTests(unittest.TestCase):
    def client_for(
        self, listed: list[str], records: Mapping[str, object]
    ) -> FakeClient:
        responses: dict[tuple[str, str], object] = {
            ("GET", owuiturn._CHAT_LIST_PATH): Response(
                200, [{"id": chat_id} for chat_id in listed]
            )
        }
        for chat_id, response in records.items():
            responses[("GET", owuiturn._chat_path(chat_id))] = response
        return FakeClient(responses)

    def find(
        self,
        client: FakeClient,
        *,
        user_id: str = USER_ID,
        assistant_id: str = ASSISTANT_ID,
    ) -> owuiturn.TurnChat:
        return owuiturn.find_turn_chat(
            client,
            ids_before=frozenset({"fixture-old-chat"}),
            user_id=user_id,
            assistant_id=assistant_id,
        )

    def assert_refusal(self, found: owuiturn.TurnChat, *fragments: str) -> None:
        problem = found.refusal()
        self.assertIs(found.problem, problem)
        for fragment in fragments:
            self.assertIn(fragment, problem.problem)
        self.assertTrue(problem.fix)
        self.assertNotIn(PASSWORD, problem.problem)
        self.assertNotIn(PASSWORD, repr(problem))
        self.assertNotIn(PASSWORD, repr(found))

    def test_one_candidate_carrying_both_ids_is_returned(self) -> None:
        client = self.client_for([CHAT_ID], {CHAT_ID: history()})

        found = self.find(client)

        self.assertEqual(found.chat_id, CHAT_ID)
        self.assertEqual(found.candidates, 1)
        self.assertIsNone(found.problem)
        stored = cast(owuiturn.StoredTurn, found.stored)
        self.assertIsNone(stored.problem)
        self.assertEqual(stored.assistant["id"] if stored.assistant else None, ASSISTANT_ID)
        self.assertEqual(stored.user["id"] if stored.user else None, USER_ID)

    def test_every_candidate_is_read_and_foreign_ids_do_not_match(self) -> None:
        foreign_id = "fixture-foreign-chat"
        client = self.client_for(
            [CHAT_ID, foreign_id],
            {
                CHAT_ID: history(),
                foreign_id: history(
                    user_id="fixture-foreign-user",
                    assistant_id="fixture-foreign-assistant",
                ),
            },
        )

        found = self.find(client)

        self.assertEqual(found.chat_id, CHAT_ID)
        self.assertEqual(found.candidates, 2)
        self.assertEqual(
            [path for _method, path, _body in client.requests],
            [
                owuiturn._CHAT_LIST_PATH,
                owuiturn._chat_path(CHAT_ID),
                owuiturn._chat_path(foreign_id),
            ],
        )

    def test_not_found_candidate_is_skipped(self) -> None:
        for status, body in ((401, None), (404, {"detail": "other wording"})):
            with self.subTest(status=status):
                client = self.client_for(
                    [CHAT_ID], {CHAT_ID: Response(status, body)}
                )

                found = self.find(client)

                self.assertIsNone(found.chat_id)
                self.assertIsNone(found.stored)
                self.assertEqual(found.candidates, 1)
                self.assert_refusal(found, "1 new chats", "1 of them gone")

    def test_a_candidate_gone_since_the_listing_leaves_the_match_standing(self) -> None:
        # The overlap this ticket exists for: another session deleted the chat
        # it made between our listing and our read, and ours is still found.
        vanished_id = "fixture-foreign-chat"
        client = self.client_for(
            [CHAT_ID, vanished_id],
            {CHAT_ID: history(), vanished_id: Response(401, {"detail": "Not found"})},
        )

        found = self.find(client)

        self.assertEqual(found.chat_id, CHAT_ID)
        self.assertEqual(found.candidates, 2)
        self.assertIsNone(found.problem)

    def test_no_match_refusals_name_counts_fix_and_keep_credentials_out(self) -> None:
        foreign_id = "fixture-foreign-chat"
        cases: tuple[
            tuple[list[str], Mapping[str, object], tuple[str, str]], ...
        ] = (
            ([], {}, ("0 new chats", "0 of them gone")),
            (
                [foreign_id],
                {
                    foreign_id: history(
                        user_id="fixture-foreign-user",
                        assistant_id="fixture-foreign-assistant",
                    )
                },
                ("1 new chats", "0 of them gone"),
            ),
        )
        for listed, records, fragments in cases:
            with self.subTest(listed=listed):
                found = self.find(self.client_for(listed, records))
                self.assert_refusal(found, *fragments)

    def test_two_matching_candidates_are_refused(self) -> None:
        second_id = "fixture-chat-second"
        client = self.client_for(
            [CHAT_ID, second_id],
            {CHAT_ID: history(), second_id: history()},
        )

        found = self.find(client)

        self.assertIsNone(found.chat_id)
        self.assertIsNone(found.stored)
        self.assertEqual(found.candidates, 2)
        self.assert_refusal(found, "2 of 2 new chats")

    def test_non_not_found_candidate_error_is_raised(self) -> None:
        client = self.client_for([CHAT_ID], {CHAT_ID: Response(500, None)})

        with self.assertRaises(OwuiError) as raised:
            self.find(client)

        self.assertIn("HTTP 500", raised.exception.problem)
        self.assertTrue(raised.exception.fix)
        self.assertNotIn(PASSWORD, str(raised.exception))
        self.assertNotIn(PASSWORD, repr(raised.exception))

    def test_two_id_pairs_each_find_their_chat_in_the_same_listing(self) -> None:
        first_chat = "fixture-chat-first"
        second_chat = "fixture-chat-second"
        first_user = "fixture-user-first"
        first_assistant = "fixture-assistant-first"
        second_user = "fixture-user-second"
        second_assistant = "fixture-assistant-second"
        client = self.client_for(
            [first_chat, second_chat],
            {
                first_chat: history(
                    user_id=first_user, assistant_id=first_assistant
                ),
                second_chat: history(
                    user_id=second_user, assistant_id=second_assistant
                ),
            },
        )

        first = self.find(client, user_id=first_user, assistant_id=first_assistant)
        second = self.find(
            client, user_id=second_user, assistant_id=second_assistant
        )

        self.assertEqual(first.chat_id, first_chat)
        self.assertEqual(first.candidates, 2)
        self.assertEqual(second.chat_id, second_chat)
        self.assertEqual(second.candidates, 2)

    def test_a_record_without_the_assistant_id_is_not_the_turns_chat(self) -> None:
        found = self.find(
            self.client_for(
                [CHAT_ID], {CHAT_ID: history(assistant_id="fixture-other-assistant")}
            )
        )

        self.assertIsNone(found.chat_id)
        self.assertIsNone(found.stored)
        self.assert_refusal(found, "1 new chats", "0 of them gone")

    def test_placeholder_shapes_are_returned_through_stored_turn(self) -> None:
        # The placeholder is written when the chat is created, so an errored or
        # unfinished turn still identifies its chat — its StoredTurn says so.
        cases = (
            ("errored", history(assistant={"error": "fixture error"}), "carries an error"),
            ("unfinished", history(assistant={"done": False}), "is unfinished"),
            ("finished", history(), None),
        )
        for name, response, fragment in cases:
            with self.subTest(shape=name):
                found = self.find(self.client_for([CHAT_ID], {CHAT_ID: response}))

                self.assertEqual(found.chat_id, CHAT_ID)
                self.assertIsNone(found.problem)
                stored = cast(owuiturn.StoredTurn, found.stored)
                if fragment is None:
                    self.assertIsNone(stored.problem)
                else:
                    problem = cast(Problem, stored.problem)
                    self.assertIn(fragment, problem.problem)
                    self.assertTrue(problem.fix)
                    self.assertNotIn(PASSWORD, problem.problem)
                    self.assertNotIn(PASSWORD, repr(problem))

    def test_a_turn_chat_without_a_problem_still_answers_a_refusal(self) -> None:
        refusal = owuiturn.TurnChat(None, None, 0, None).refusal()

        self.assertTrue(refusal.problem)
        self.assertTrue(refusal.fix)


class StoredTurnTests(unittest.TestCase):
    def client_for(self, response: object) -> FakeClient:
        return FakeClient({("GET", f"{owuiturn._CHAT_PATH}{CHAT_ID}"): response})

    def test_stored_current_uses_current_and_parent_ids(self) -> None:
        stored = owuiturn.stored_current(self.client_for(history()), CHAT_ID)

        self.assertIsNone(stored.problem)
        self.assertEqual(stored.assistant["id"] if stored.assistant else None, ASSISTANT_ID)
        self.assertEqual(stored.user["id"] if stored.user else None, USER_ID)

    def test_stored_current_applies_finished_and_error_rules(self) -> None:
        for response, fragment in (
            (history(assistant={"error": "fixture error"}), "carries an error"),
            (history(assistant={"done": False}), "is unfinished"),
        ):
            with self.subTest(fragment=fragment):
                stored = owuiturn.stored_current(self.client_for(response), CHAT_ID)
                self.assertIsNotNone(stored.problem)
                assert stored.problem is not None
                self.assertIn(fragment, stored.problem.problem)
                self.assertTrue(stored.problem.fix)

    def test_stored_current_refuses_missing_current_or_parent_ids(self) -> None:
        for response in (
            history(current_id=""),
            Response(
                200,
                {
                    "chat": {
                        "history": {
                            "currentId": ASSISTANT_ID,
                            "messages": {
                                ASSISTANT_ID: {"parentId": "", "done": True}
                            },
                        }
                    }
                },
            ),
        ):
            with self.subTest(response=response):
                with self.assertRaises(OwuiError) as raised:
                    owuiturn.stored_current(self.client_for(response), CHAT_ID)
                self.assertTrue(raised.exception.fix)
                self.assertNotIn(PASSWORD, repr(raised.exception))


class DeletionTests(unittest.TestCase):
    def test_delete_chat_returns_none_only_for_confirmed_boolean_true(self) -> None:
        client = FakeClient({("DELETE", f"{owuiturn._CHAT_PATH}{CHAT_ID}"): Response(200, True)})

        self.assertIsNone(owuiturn.delete_chat(client, CHAT_ID))

    def test_delete_chat_reports_false_http_and_transport_failures_with_fixes(self) -> None:
        cases = (
            (Response(200, False), "confirm chat deletion"),
            (Response(500, None), "HTTP 500"),
            (OwuiError("fixture frontend transport failed"), "fixture frontend transport failed"),
        )
        for response, fragment in cases:
            with self.subTest(fragment=fragment):
                client = FakeClient({("DELETE", f"{owuiturn._CHAT_PATH}{CHAT_ID}"): response})
                problem = owuiturn.delete_chat(client, CHAT_ID)
                self.assertIsInstance(problem, Problem)
                assert problem is not None
                self.assertIn(fragment, problem.problem)
                self.assertTrue(problem.fix)
                self.assertNotIn(PASSWORD, problem.problem)
                self.assertNotIn(PASSWORD, repr(problem))
