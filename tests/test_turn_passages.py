"""The passage cut, multi-get request, and fixed refusals."""

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
from gideon.turn.fusion import LegHit, PoolEntry, fuse
from gideon.turn.states import State, TurnFailure

FIXTURE = Path(__file__).with_name("fixtures") / "turn" / "passages-mget.json"
READER = "fixture-reader"
PASSWORD = "fictitious-store-password"


def pool_entry(chunk_id: str, rank: int) -> PoolEntry:
    return PoolEntry(chunk_id, rank, 1.0, (("lexical", rank),), chunk_id, False, "persuasive", rank=rank)


class PassageCut(unittest.TestCase):
    def test_cut_takes_the_first_twenty_in_pool_order(self) -> None:
        hits = [
            LegHit(f"fixture-{rank}", rank, f"cluster-{rank}", False, "fixture-court", "unknown")
            for rank in range(25, 0, -1)
        ]
        fusion = fuse(
            {"lexical": hits}, tier_for=lambda _: "persuasive",
            rrf_k=40, boilerplate_weight=0.5, collapse=True, pool_cap=len(hits),
        )
        self.assertEqual(passages.cut(fusion), fusion.entries[:20])

    def test_passage_is_frozen_and_repr_excludes_caption_and_text(self) -> None:
        passage = passages.Passage("fixture-chunk", 4, "Invented v. Caption", "An invented passage.")
        with self.assertRaises(FrozenInstanceError):
            passage.text = "A replacement passage."  # type: ignore[misc]
        self.assertEqual(repr(passage), "Passage(chunk_id='fixture-chunk', pool_rank=4)")


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

    def test_request_uses_current_password_and_returns_passages_in_cut_order(self) -> None:
        docs = self.docs[:-1]
        docs[1]["_source"] = {"text": "Another invented passage without a caption."}
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
            passages.Passage(entry.chunk_id, entry.pool_rank, doc["_source"].get("caption", ""), doc["_source"]["text"])
            for entry, doc in zip(entries, docs, strict=True)
        )
        self.assertEqual(asyncio.run(read_twice()), (expected, expected))
        for request, password in zip(requests, (PASSWORD, next_password), strict=True):
            self.assertEqual((request.method, request.url.path), ("POST", f"/{self.index}/_mget"))
            self.assertEqual(request.headers["Authorization"], "Basic " + base64.b64encode(f"{READER}:{password}".encode()).decode())
            self.assertEqual(json.loads(request.content), {
                "docs": [{"_id": entry.chunk_id, "_source": ["text", "caption"]} for entry in entries],
            })

    def test_empty_cut_reads_no_password_and_makes_no_request(self) -> None:
        self.password_file.unlink()

        def respond(_: httpx.Request) -> httpx.Response:
            self.fail("An empty cut must not reach the store.")

        self.assertEqual(self.read(httpx.MockTransport(respond), ()), ())

    def test_absent_id_in_the_recorded_reply_refuses_the_whole_cut(self) -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"docs": self.docs}))
        with self.assertRaises(TurnFailure) as raised:
            self.read(transport, self.entries)
        self.assertEqual((raised.exception.state, raised.exception.word), (State.STORE_REPLY_INVALID, "passages"))

    def test_malformed_multi_get_refusals(self) -> None:
        first, second = self.docs[:2]
        no_found = {key: value for key, value in second.items() if key != "found"}
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
            ("missing text", {"docs": [first, {**second, "_source": {}}]}),
            ("empty text", {"docs": [first, {**second, "_source": {"text": ""}}]}),
            ("nonstring text", {"docs": [first, {**second, "_source": {"text": 1}}]}),
            ("nonstring caption", {"docs": [first, {**second, "_source": {"text": "Invented text.", "caption": []}}]}),
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
