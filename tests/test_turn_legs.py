"""Request shapes, reply validation, and timing for the retrieval stores."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Literal

import httpx

from gideon.turn import dense, lexical
from gideon.turn.fusion import LegHit
from gideon.turn.provenance import RequestSpan, TurnTimings
from gideon.turn.states import TurnFailure

FIXTURES = Path(__file__).with_name("fixtures") / "turn"
VECTOR = [0.2, 0.4]
MESSAGE = "Fictitious lexical search"
LIMIT = 11
INDEX = "fixture-generation"
type Leg = Literal["dense", "lexical"]


def turn_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Legs(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = Path(self.enterContext(tempfile.TemporaryDirectory())) / "secret"
        self.secret.write_text("fixture-store-secret", encoding="utf-8")

    def search(
        self, leg: Leg, transport: httpx.MockTransport, *, collapse: bool = True,
        ticks: tuple[float, float] = (0, 1), timeout: float = 2,
    ) -> tuple[tuple[LegHit, ...], RequestSpan]:
        async def run() -> tuple[tuple[LegHit, ...], RequestSpan]:
            clock = iter(ticks).__next__
            async with httpx.AsyncClient(base_url="http://store.example.invalid", transport=transport) as client:
                if leg == "dense":
                    return await dense.search(
                        client, self.secret, INDEX, VECTOR, LIMIT, collapse=collapse, clock=clock, timeout=timeout,
                    )
                return await lexical.search(
                    client, "fixture-reader", self.secret, INDEX, MESSAGE, LIMIT,
                    collapse=collapse, clock=clock, timeout=timeout,
                )
        return asyncio.run(run())

    def reply_transport(self, reply: object, status: int = 200) -> httpx.MockTransport:
        return httpx.MockTransport(lambda _: httpx.Response(status, json=reply))

    def test_vector_request_and_reply_with_and_without_collapse(self) -> None:
        grouped = turn_fixture("dense-grouped.json")
        points = [group["hits"][0] for group in grouped["result"]["groups"]]
        plain = {**grouped, "result": {"points": points}}
        requests: list[httpx.Request] = []
        for collapse, reply, route in ((True, grouped, "/groups"), (False, plain, "")):
            with self.subTest(collapse=collapse):
                requests.clear()

                def respond(request: httpx.Request, reply: object = reply) -> httpx.Response:
                    requests.append(request)
                    return httpx.Response(200, json=reply)

                hits, _ = self.search("dense", httpx.MockTransport(respond), collapse=collapse)
                request, = requests
                self.assertEqual((request.method, request.url.path), ("POST", f"/collections/{INDEX}/points/query{route}"))
                self.assertEqual(request.headers["api-key"], self.secret.read_text(encoding="utf-8"))
                self.assertEqual(json.loads(request.content), {
                    "query": VECTOR, "limit": LIMIT, "with_payload": ["chunk_id", "dupe_cluster", "boilerplate"],
                    "with_vector": False, **({"group_by": "dupe_cluster", "group_size": 1} if collapse else {}),
                })
                self.assertEqual(hits, tuple(
                    LegHit(point["payload"]["chunk_id"], rank, point["payload"]["dupe_cluster"], point["payload"]["boilerplate"])
                    for rank, point in enumerate(points, start=1)
                ))

    def test_lexical_request_and_reply_with_and_without_collapse(self) -> None:
        reply = turn_fixture("lexical-collapsed.json")
        rows = reply["hits"]["hits"]
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=reply)

        for collapse in (True, False):
            with self.subTest(collapse=collapse):
                requests.clear()
                hits, _ = self.search("lexical", httpx.MockTransport(respond), collapse=collapse)
                request, = requests
                self.assertEqual((request.method, request.url.path), ("POST", f"/{INDEX}/_search"))
                self.assertEqual(json.loads(request.content), {
                    "query": {"match": {"text": MESSAGE}}, "size": LIMIT, "_source": ["dupe_cluster", "boilerplate"],
                    **({"collapse": {"field": "dupe_cluster"}} if collapse else {}),
                })
                self.assertEqual(hits, tuple(
                    LegHit(row["_id"], rank, row["_source"]["dupe_cluster"], row["_source"]["boilerplate"])
                    for rank, row in enumerate(rows, start=1)
                ))

    def test_incomplete_or_off_shape_successes_refuse_even_with_hits(self) -> None:
        vector = turn_fixture("dense-grouped.json")
        text = turn_fixture("lexical-collapsed.json")
        shards = text["_shards"]
        cases: tuple[tuple[Leg, object], ...] = (
            ("dense", {**vector, "result": {"groups": [False]}}),
            ("dense", {**vector, "status": "fixture-incomplete"}),
            ("lexical", {**text, "hits": {"hits": [False]}}),
            ("lexical", {**text, "timed_out": True}),
            ("lexical", {**text, "_shards": {**shards, "total": shards["total"] + 1, "failed": 1}}),
        )
        for leg, reply in cases:
            with self.subTest(leg=leg, reply=reply):
                with self.assertRaises(TurnFailure) as raised:
                    self.search(leg, self.reply_transport(reply))
                self.assertEqual((raised.exception.state, raised.exception.word), ("store_reply_invalid", leg))

    def test_non_ok_response_and_deadline_have_distinct_states_for_each_store(self) -> None:
        cancelled: list[str] = []

        async def stall(request: httpx.Request) -> httpx.Response:
            try:
                await asyncio.Future()
            finally:
                cancelled.append(request.url.path)
            raise AssertionError("Fictitious store unexpectedly replied")

        for leg in ("dense", "lexical"):
            with self.subTest(leg=leg):
                with self.assertRaises(TurnFailure) as unavailable:
                    self.search(leg, self.reply_transport({"detail": "Fictitious backend diagnostics"}, 503))
                self.assertEqual((unavailable.exception.state, unavailable.exception.word), ("store_unavailable", leg))
                with self.assertRaises(TurnFailure) as expired:
                    self.search(leg, httpx.MockTransport(stall), timeout=0)
                self.assertEqual((expired.exception.state, expired.exception.word), ("retrieval_timeout", leg))
        self.assertEqual(cancelled, [f"/collections/{INDEX}/points/query/groups", f"/{INDEX}/_search"])

    def test_retrieval_timing_counts_store_overlap_once_and_excludes_disjoint_gaps(self) -> None:
        vector = self.reply_transport(turn_fixture("dense-grouped.json"))
        text = self.reply_transport(turn_fixture("lexical-collapsed.json"))
        for dense_ticks, lexical_ticks, objects_span, expected in (
            ((5, 9), (3, 7), None, 6250),
            ((8, 10), (3, 6), None, 5250),
            ((8, 10), (3, 6), RequestSpan(5, 9), 7250),
            ((7, 9), (3, 10), RequestSpan(5, 11), 8250),
            ((8, 10), (3, 6), RequestSpan(12, 13), 6250),
            ((4, 5), (9, 10), RequestSpan(1, 8), 8250),
        ):
            with self.subTest(dense=dense_ticks, lexical=lexical_ticks, objects=objects_span):
                _, dense_span = self.search("dense", vector, ticks=dense_ticks)
                _, lexical_span = self.search("lexical", text, ticks=lexical_ticks)
                metrics = TurnTimings(
                    record=1, extraction=0.75, embedding=5,
                    dense=dense_span, lexical=lexical_span, objects=objects_span, fusion=0.25, total=12,
                ).to_milliseconds()
                self.assertEqual(metrics["dense"], (dense_ticks[1] - dense_ticks[0]) * 1000)
                self.assertEqual(metrics["lexical"], (lexical_ticks[1] - lexical_ticks[0]) * 1000)
                self.assertEqual(metrics["objects"], 0 if objects_span is None else objects_span.seconds * 1000)
                self.assertEqual(metrics["extraction"], 750)
                self.assertEqual(metrics["retrieval"], expected)
