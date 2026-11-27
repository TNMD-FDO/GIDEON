"""The passage multi-get request, metadata, and fixed refusals."""

import asyncio
import base64
import json
import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import httpx

from gideon.turn import passages
from gideon.turn.fusion import PoolEntry
from gideon.turn.states import State, TurnFailure

FIXTURE = Path(__file__).with_name("fixtures") / "turn" / "passages-mget.json"
READER = "fixture-reader"
PASSWORD = "fictitious-store-password"


def pool_entry(chunk_id: str, rank: int) -> PoolEntry:
    return PoolEntry(chunk_id, rank, 1.0, (("lexical", rank),), chunk_id, False, "persuasive", rank=rank)


class PassageRecord(unittest.TestCase):
    def test_passage_is_frozen_and_repr_excludes_caption_and_text(self) -> None:
        passage = passages.Passage(
            "fixture-chunk", 4, "Invented v. Caption", "An invented passage.", doc_id="fixture-opinion", token_count=4,
        )
        with self.assertRaises(FrozenInstanceError):
            passage.text = "A replacement passage."  # type: ignore[misc]
        self.assertNotIn(passage.caption, repr(passage))
        self.assertNotIn(passage.text, repr(passage))


class PassageFetch(unittest.TestCase):
    def setUp(self) -> None:
        self.docs = json.loads(FIXTURE.read_text(encoding="utf-8"))["docs"]
        self.index = self.docs[0]["_index"]
        self.entries = tuple(pool_entry(doc["_id"], rank) for rank, doc in enumerate(self.docs, start=1))
        self.password_file = Path(self.enterContext(tempfile.TemporaryDirectory())) / "secret"
        self.password_file.write_text(PASSWORD + "\n", encoding="utf-8")

    def read(self, transport: httpx.MockTransport, entries: Sequence[PoolEntry], *, timeout: float = 2) -> tuple[passages.Passage, ...]:
        async def run() -> tuple[passages.Passage, ...]:
            async with httpx.AsyncClient(base_url="http://store.example.invalid", transport=transport) as client:
                return await passages.fetch(client, READER, self.password_file, self.index, entries, timeout=timeout)

        return asyncio.run(run())

    def test_request_uses_current_password_and_returns_passages_in_supplied_order(self) -> None:
        docs = self.docs[:-1]
        del docs[1]["_source"]["caption"]
        entries = tuple(replace(entry, pool_rank=entry.pool_rank + 4) for entry in self.entries[:-1])
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"docs": list(reversed(docs))})

        transport = httpx.MockTransport(respond)
        next_password = "rotated-fictitious-store-password"

        async def read_twice() -> tuple[tuple[passages.Passage, ...], tuple[passages.Passage, ...]]:
            async with httpx.AsyncClient(base_url="http://store.example.invalid", transport=transport) as client:
                first = await passages.fetch(client, READER, self.password_file, self.index, entries)
                self.password_file.write_text(next_password, encoding="utf-8")
                second = await passages.fetch(client, READER, self.password_file, self.index, entries)
                return first, second

        expected = tuple(
            passages.Passage(
                entry.chunk_id, entry.pool_rank, doc["_source"].get("caption", ""), doc["_source"]["text"],
                doc_id=doc["_source"]["doc_id"], token_count=doc["_source"]["token_count"],
            )
            for entry, doc in zip(entries, docs, strict=True)
        )
        self.assertEqual(asyncio.run(read_twice()), (expected, expected))
        for request, password in zip(requests, (PASSWORD, next_password), strict=True):
            self.assertEqual((request.method, request.url.path), ("POST", f"/{self.index}/_mget"))
            self.assertEqual(request.headers["Authorization"], "Basic " + base64.b64encode(f"{READER}:{password}".encode()).decode())
            self.assertEqual(json.loads(request.content), {
                "docs": [{"_id": entry.chunk_id, "_source": ["text", "caption", "doc_id", "token_count"]} for entry in entries],
            })

    def test_empty_selection_reads_no_password_and_makes_no_request(self) -> None:
        self.password_file.unlink()

        def respond(_: httpx.Request) -> httpx.Response:
            self.fail("An empty selection must not reach the store.")

        self.assertEqual(self.read(httpx.MockTransport(respond), ()), ())

    def test_absent_id_in_the_recorded_reply_refuses_the_whole_read(self) -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"docs": self.docs}))
        with self.assertRaises(TurnFailure) as raised:
            self.read(transport, self.entries)
        self.assertEqual((raised.exception.state, raised.exception.word), (State.STORE_REPLY_INVALID, "passages"))

    def test_malformed_multi_get_refusals(self) -> None:
        first, second = self.docs[:2]
        no_found = {key: value for key, value in second.items() if key != "found"}
        source = second["_source"]
        bad_fields: tuple[tuple[str, object], ...] = (
            ("text", ""), ("text", 1), ("caption", []), ("doc_id", ""), ("doc_id", 1),
            ("token_count", 0), ("token_count", -1), ("token_count", True), ("token_count", 2.0), ("token_count", "2"),
        )
        cases: tuple[tuple[str, object], ...] = (
            ("nonobject body", []),
            ("no docs", {}),
            ("nonlist docs", {"docs": {}}),
            ("missing item", {"docs": [first]}),
            ("extra item", {"docs": [first, second, self.docs[2]]}),
            ("nonobject item", {"docs": [first, None]}),
            ("unknown id", {"docs": [first, {**second, "_id": "unrequested-fixture"}]}),
            ("nonstring id", {"docs": [first, {**second, "_id": []}]}),
            ("duplicate id", {"docs": [first, first]}),
            ("item error", {"docs": [first, {**second, "error": {"reason": "Fictitious lookup failure"}}]}),
            ("missing found", {"docs": [first, no_found]}),
            ("nonboolean found", {"docs": [first, {**second, "found": 1}]}),
            ("nonobject source", {"docs": [first, {**second, "_source": None}]}),
            *(
                (f"missing {field}", {"docs": [first, {**second, "_source": {k: v for k, v in source.items() if k != field}}]})
                for field in ("text", "doc_id", "token_count")
            ),
            *(
                (f"{field}={value!r}", {"docs": [first, {**second, "_source": {**source, field: value}}]})
                for field, value in bad_fields
            ),
        )
        for label, reply in cases:
            with self.subTest(label=label):
                transport = httpx.MockTransport(lambda _, reply=reply: httpx.Response(200, json=reply))
                with self.assertRaises(TurnFailure) as raised:
                    self.read(transport, self.entries[:2])
                self.assertEqual((raised.exception.state, raised.exception.word), (State.STORE_REPLY_INVALID, "passages"))

    def test_non_json_reply_and_unsuccessful_status(self) -> None:
        for status, state in ((200, State.STORE_REPLY_INVALID), (503, State.STORE_UNAVAILABLE)):
            with self.subTest(status=status):
                response = httpx.Response(status, text="Fictitious store diagnostics")
                transport = httpx.MockTransport(lambda _, response=response: response)
                with self.assertRaises(TurnFailure) as raised:
                    self.read(transport, self.entries[:1])
                self.assertEqual((raised.exception.state, raised.exception.word), (state, "passages"))
                self.assertNotIn("Fictitious store diagnostics", str(raised.exception.to_body()))

    def test_transport_failures_use_the_passages_word(self) -> None:
        for error, state in ((httpx.ConnectError, State.STORE_UNAVAILABLE), (httpx.ReadTimeout, State.RETRIEVAL_TIMEOUT)):
            with self.subTest(error=error):
                def respond(request: httpx.Request, error: type[httpx.RequestError] = error) -> httpx.Response:
                    raise error("Fictitious transport failure", request=request)

                with self.assertRaises(TurnFailure) as raised:
                    self.read(httpx.MockTransport(respond), self.entries[:1])
                self.assertEqual((raised.exception.state, raised.exception.word), (state, "passages"))

    def test_request_deadline_cancels_the_multi_get(self) -> None:
        cancelled = False

        async def stalled(_: httpx.Request) -> httpx.Response:
            nonlocal cancelled
            try:
                return await asyncio.Future()
            finally:
                cancelled = True

        with self.assertRaises(TurnFailure) as raised:
            self.read(httpx.MockTransport(stalled), self.entries[:1], timeout=0)
        self.assertEqual((raised.exception.state, raised.exception.word), (State.RETRIEVAL_TIMEOUT, "passages"))
        self.assertTrue(cancelled)
