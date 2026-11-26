"""Embedding-space validation, request failures, and key rotation."""

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import httpx
from test_turn_legs import turn_fixture

from gideon.turn.embedder import (
    EmbeddedQuery,
    EmbeddingSpace,
    check_space,
    embed_query,
    listing_repository,
    parse_embedding,
)
from gideon.turn.states import TurnFailure

MESSAGE = "Fictitious embedding question"


class Embedder(unittest.TestCase):
    def setUp(self) -> None:
        self.listing = turn_fixture("embed-listing.json")
        self.embedding = turn_fixture("embedding.json")
        model = self.listing["data"][0]
        self.vector = tuple(self.embedding["data"][0]["embedding"])
        self.space = EmbeddingSpace(model["id"], model["root"], "fixture-embedding-space", len(self.vector))
        self.key_file = Path(self.enterContext(tempfile.TemporaryDirectory())) / "key"
        self.key_file.write_text("fixture-initial-key\n", encoding="utf-8")

    def test_space_check_names_each_mismatch(self) -> None:
        matching = EmbeddedQuery(self.space.repository, self.vector)
        cases = (
            ("generation", "fixture-other-space", matching),
            ("repository", self.space.space_id, replace(matching, repository="fixture-owner/other-model")),
            ("width", self.space.space_id, replace(matching, vector=self.vector[:-1])),
            ("listing", self.space.space_id, replace(matching, repository=None)),
        )
        self.assertIsNone(check_space(self.space, self.space.space_id, matching))
        for reason, space_id, query in cases:
            with self.subTest(reason=reason):
                self.assertEqual(check_space(self.space, space_id, query), reason)

    def test_listing_requires_the_served_name(self) -> None:
        self.assertEqual(listing_repository(self.listing, self.space.served_name), self.space.repository)
        self.assertIsNone(listing_repository(self.listing, "fixture-unlisted-model"))

    def test_maximum_context_refusal_is_distinct_from_an_unrelated_bad_request(self) -> None:
        context_error = "This fictitious model's maximum context length was exceeded."
        for message, state in (
            (context_error, "message_outside_window"),
            ("Fictitious upstream error", "embedder_unavailable"),
        ):
            with self.subTest(state=state):
                asyncio.run(self._assert_bad_request(message, state))

    async def _assert_bad_request(self, message: str, state: str) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/models":
                return httpx.Response(200, json=self.listing)
            return httpx.Response(400, json={"error": {"message": message}})

        async with httpx.AsyncClient(
            base_url="http://embedder.example.invalid", transport=httpx.MockTransport(respond),
        ) as client:
            with self.assertRaises(TurnFailure) as raised:
                await embed_query(client, self.key_file, self.space, MESSAGE)
        self.assertEqual(raised.exception.state, state)
        self.assertNotIn(message, str(raised.exception.to_body()))

    def test_non_finite_vector_components_refuse(self) -> None:
        self.assertEqual(parse_embedding(self.embedding), self.vector)
        for component in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(component=component):
                with self.assertRaises(TurnFailure) as raised:
                    parse_embedding({"data": [{"embedding": [self.vector[0], component]}]})
                self.assertEqual(raised.exception.state, "embedder_unavailable")

    def test_each_call_reads_the_key_for_both_requests(self) -> None:
        asyncio.run(self._rotate_key())

    async def _rotate_key(self) -> None:
        credentials: list[tuple[str, str]] = []
        replies = {"/v1/models": self.listing, "/v1/embeddings": self.embedding}

        def respond(request: httpx.Request) -> httpx.Response:
            credentials.append((request.url.path, request.headers["Authorization"]))
            return httpx.Response(200, json=replies[request.url.path])

        async with httpx.AsyncClient(
            base_url="http://embedder.example.invalid", transport=httpx.MockTransport(respond),
        ) as client:
            for key in ("fixture-first-key", "fixture-rotated-key"):
                with self.subTest(key=key):
                    credentials.clear()
                    self.key_file.write_text(f"{key}\n", encoding="utf-8")
                    result = await embed_query(client, self.key_file, self.space, MESSAGE)
                    self.assertEqual(result, EmbeddedQuery(self.space.repository, self.vector))
                    self.assertCountEqual(credentials, [(path, f"Bearer {key}") for path in replies])

    def test_missing_served_name_outranks_the_embedding_refusal(self) -> None:
        asyncio.run(self._missing_name())

    async def _missing_name(self) -> None:
        other = {**self.listing, "data": [{**self.listing["data"][0], "id": "fixture-other-model"}]}

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/models":
                return httpx.Response(200, json=other)
            return httpx.Response(404, json={"error": {"message": "fixture model not found"}})

        async with httpx.AsyncClient(
            base_url="http://embedder.example.invalid", transport=httpx.MockTransport(respond),
        ) as client:
            result = await embed_query(client, self.key_file, self.space, MESSAGE)
        self.assertEqual(result, EmbeddedQuery(None, ()))
        self.assertEqual(check_space(self.space, self.space.space_id, result), "listing")

    def test_a_failed_listing_is_reported_without_waiting_on_a_stalled_embedding(self) -> None:
        asyncio.run(self._failed_listing())

    async def _failed_listing(self) -> None:
        async def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/models":
                return httpx.Response(503)
            await asyncio.Future()
            raise AssertionError("unreachable")

        async with httpx.AsyncClient(
            base_url="http://embedder.example.invalid", transport=httpx.MockTransport(respond),
        ) as client:
            with self.assertRaises(TurnFailure) as raised:
                await embed_query(client, self.key_file, self.space, MESSAGE, timeout=30.0)
        self.assertEqual(raised.exception.state, "embedder_unavailable")

    def test_a_matching_listing_keeps_the_embedding_refusal(self) -> None:
        asyncio.run(self._matching_listing())

    async def _matching_listing(self) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/models":
                return httpx.Response(200, json=self.listing)
            return httpx.Response(503)

        async with httpx.AsyncClient(
            base_url="http://embedder.example.invalid", transport=httpx.MockTransport(respond),
        ) as client:
            with self.assertRaises(TurnFailure) as raised:
                await embed_query(client, self.key_file, self.space, MESSAGE)
        self.assertEqual(raised.exception.state, "embedder_unavailable")
