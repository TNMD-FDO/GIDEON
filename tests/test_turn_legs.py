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
from gideon.turn.states import State, TurnFailure

FIXTURES = Path(__file__).with_name("fixtures") / "turn"
VECTOR = [0.2, 0.4]
MESSAGE = "Fictitious lexical search"
LIMIT = 11
INDEX = "fixture-generation"
type Leg = Literal["dense", "lexical", "controlling"]


def turn_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def invalid_court_metadata(fields: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "missing court": {key: value for key, value in fields.items() if key != "court"},
        "missing precedential": {key: value for key, value in fields.items() if key != "precedential"},
        "empty court": {**fields, "court": ""},
        "outside vocabulary": {**fields, "precedential": "fixture-unrecognized-status"},
    }


class Legs(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = Path(self.enterContext(tempfile.TemporaryDirectory())) / "secret"
        self.secret.write_text("fixture-store-secret", encoding="utf-8")
        self.vector_reply = turn_fixture("dense-grouped.json")
        self.controlling_courts = frozenset(
            group["hits"][0]["payload"]["court"] for group in self.vector_reply["result"]["groups"]
        ) | {"fx-state13A"}

    def search(
        self, leg: Leg, transport: httpx.MockTransport, *, collapse: bool = True, courts: tuple[str, ...] | None = None,
        ticks: tuple[float, float] = (0, 1), timeout: float = 2,
    ) -> tuple[tuple[LegHit, ...], RequestSpan]:
        async def run() -> tuple[tuple[LegHit, ...], RequestSpan]:
            clock = iter(ticks).__next__
            async with httpx.AsyncClient(base_url="http://store.example.invalid", transport=transport) as client:
                if leg == "lexical":
                    return await lexical.search(
                        client, "fixture-reader", self.secret, INDEX, MESSAGE, LIMIT,
                        collapse=collapse, courts=courts, clock=clock, timeout=timeout,
                    )
                court_set = frozenset(courts) if courts is not None else None
                return await dense.search(
                    client, self.secret, INDEX, VECTOR, LIMIT, collapse=collapse, leg=leg, clock=clock, timeout=timeout,
                    courts=self.controlling_courts if leg == "controlling" else court_set,
                )
        return asyncio.run(run())

    def reply_transport(self, reply: object, status: int = 200) -> httpx.MockTransport:
        return httpx.MockTransport(lambda _: httpx.Response(status, json=reply))

    def test_vector_request_and_reply_with_and_without_collapse(self) -> None:
        grouped = self.vector_reply
        points = [group["hits"][0] for group in grouped["result"]["groups"]]
        plain = {**grouped, "result": {"points": points}}
        requests: list[httpx.Request] = []
        scoped = tuple(sorted(self.controlling_courts, reverse=True))
        cases: tuple[tuple[Leg, tuple[str, ...] | None], ...] = (
            ("dense", None), ("dense", scoped), ("controlling", None),
        )
        for leg, courts in cases:
            for collapse, reply, route in ((True, grouped, "/groups"), (False, plain, "")):
                with self.subTest(leg=leg, courts=courts, collapse=collapse):
                    requests.clear()

                    def respond(request: httpx.Request, reply: object = reply) -> httpx.Response:
                        requests.append(request)
                        return httpx.Response(200, json=reply)

                    hits, _ = self.search(leg, httpx.MockTransport(respond), collapse=collapse, courts=courts)
                    request, = requests
                    self.assertEqual((request.method, request.url.path), ("POST", f"/collections/{INDEX}/points/query{route}"))
                    self.assertEqual(request.headers["api-key"], self.secret.read_text(encoding="utf-8"))
                    expected: dict[str, object] = {
                        "query": VECTOR, "limit": LIMIT, "with_payload": list(points[0]["payload"]),
                        "with_vector": False, **({"group_by": "dupe_cluster", "group_size": 1} if collapse else {}),
                    }
                    filtered = self.controlling_courts if leg == "controlling" else courts
                    if filtered is not None:
                        expected["filter"] = {"must": [{"key": "court", "match": {"any": sorted(filtered)}}]}
                    self.assertEqual(json.loads(request.content), expected)
                    self.assertEqual(hits, tuple(
                        LegHit(rank=rank, **point["payload"])
                        for rank, point in enumerate(points, start=1)
                    ))

    def test_empty_group_list_is_valid_for_either_vector_leg(self) -> None:
        reply = {**self.vector_reply, "result": {"groups": []}}
        for leg in ("dense", "controlling"):
            with self.subTest(leg=leg):
                hits, span = self.search(leg, self.reply_transport(reply))
                self.assertEqual(hits, ())
                self.assertEqual(span, RequestSpan(0, 1))

    def test_lexical_request_and_reply_with_and_without_collapse(self) -> None:
        reply = turn_fixture("lexical-collapsed.json")
        rows = reply["hits"]["hits"]
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=reply)

        for collapse in (True, False):
            for courts in (None, ("fx-court-b", "fx-court-a")):
                with self.subTest(collapse=collapse, courts=courts):
                    requests.clear()
                    hits, _ = self.search("lexical", httpx.MockTransport(respond), collapse=collapse, courts=courts)
                    request, = requests
                    self.assertEqual((request.method, request.url.path), ("POST", f"/{INDEX}/_search"))
                    expected = {
                        "query": {"match": {"text": MESSAGE}},
                        "size": LIMIT, "_source": list(rows[0]["_source"]),
                        **({} if courts is None else {"post_filter": {"terms": {"court": sorted(courts)}}}),
                        **({"collapse": {"field": "dupe_cluster"}} if collapse else {}),
                    }
                    self.assertEqual(request.content, json.dumps(expected, separators=(",", ":")).encode())
                    self.assertEqual(hits, tuple(
                        LegHit(row["_id"], rank, **row["_source"])
                        for rank, row in enumerate(rows, start=1)
                    ))

    def test_each_leg_refuses_missing_or_invalid_court_metadata(self) -> None:
        vector = self.vector_reply
        text = turn_fixture("lexical-collapsed.json")
        point = vector["result"]["groups"][0]["hits"][0]
        cases: tuple[tuple[Leg, dict[str, Any], dict[str, Any], str], ...] = (
            ("dense", vector, point, "payload"),
            ("controlling", vector, point, "payload"),
            ("lexical", text, text["hits"]["hits"][0], "_source"),
        )
        for leg, reply, hit, field in cases:
            original_fields = hit[field]
            for label, bad_fields in invalid_court_metadata(original_fields).items():
                with self.subTest(leg=leg, label=label):
                    hit[field] = bad_fields
                    with self.assertRaises(TurnFailure) as raised:
                        self.search(leg, self.reply_transport(reply))
                    self.assertEqual((raised.exception.state, raised.exception.word), ("store_reply_invalid", leg))
            hit[field] = original_fields

    def test_incomplete_or_off_shape_successes_refuse_even_with_hits(self) -> None:
        vector = self.vector_reply
        text = turn_fixture("lexical-collapsed.json")
        shards = text["_shards"]
        group = vector["result"]["groups"][0]
        vector_failures = (
            {**vector, "status": "fixture-incomplete"},
            {**vector, "result": {}},
            {**vector, "result": {"groups": [False]}},
            {**vector, "result": {"groups": [{**group, "hits": []}]}},
            {**vector, "result": {"groups": [{**group, "hits": [{"payload": None}]}]}},
        )
        cases: tuple[tuple[Leg, object], ...] = (
            *(("dense", reply) for reply in vector_failures),
            *(("controlling", reply) for reply in vector_failures),
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

        for leg in ("dense", "lexical", "controlling"):
            with self.subTest(leg=leg):
                with self.assertRaises(TurnFailure) as unavailable:
                    self.search(leg, self.reply_transport({"detail": "Fictitious backend diagnostics"}, 503))
                self.assertEqual((unavailable.exception.state, unavailable.exception.word), ("store_unavailable", leg))
                with self.assertRaises(TurnFailure) as expired:
                    self.search(leg, httpx.MockTransport(stall), timeout=0)
                self.assertEqual((expired.exception.state, expired.exception.word), ("retrieval_timeout", leg))
        vector_path = f"/collections/{INDEX}/points/query/groups"
        self.assertEqual(cancelled, [vector_path, f"/{INDEX}/_search", vector_path])

    def test_vector_legs_refuse_transport_errors_timeouts_and_invalid_json(self) -> None:
        for leg in ("dense", "controlling"):
            for error, state in (
                (httpx.ConnectError, State.STORE_UNAVAILABLE),
                (httpx.ReadTimeout, State.RETRIEVAL_TIMEOUT),
                (None, State.STORE_REPLY_INVALID),
            ):
                with self.subTest(leg=leg, error=error):
                    def respond(request: httpx.Request, error: type[httpx.RequestError] | None = error) -> httpx.Response:
                        if error is not None:
                            raise error("Fictitious diagnostics", request=request)
                        return httpx.Response(200, content="Fictitious invalid JSON")

                    with self.assertRaises(TurnFailure) as raised:
                        self.search(leg, httpx.MockTransport(respond))
                    self.assertEqual((raised.exception.state, raised.exception.word), (state, leg))

    def test_vector_legs_refuse_missing_blank_or_unreadable_keys(self) -> None:
        def unexpected(_: httpx.Request) -> httpx.Response:
            self.fail("The store should not receive an unauthenticated request.")

        for contents in (b" \n", b"\xff", None):
            if contents is None:
                self.secret.unlink()
            else:
                self.secret.write_bytes(contents)
            for leg in ("dense", "controlling"):
                with self.subTest(leg=leg, contents=contents):
                    with self.assertRaises(TurnFailure) as raised:
                        self.search(leg, httpx.MockTransport(unexpected))
                    self.assertEqual((raised.exception.state, raised.exception.word), (State.STORE_UNAVAILABLE, leg))

    def test_retrieval_timing_counts_store_overlap_once_and_excludes_disjoint_gaps(self) -> None:
        vector = self.reply_transport(self.vector_reply)
        text = self.reply_transport(turn_fixture("lexical-collapsed.json"))
        rerank_timings = {"rerank_passages": 0.25, "rerank_queue": 1.5, "rerank": 0.5}
        total = 12 + sum(rerank_timings.values())
        for dense_ticks, lexical_ticks, controlling_ticks, objects_span, expected in (
            ((5, 9), (3, 7), (6, 8), None, 6250),
            ((8, 10), (3, 6), (5, 9), None, 7250),
            ((8, 10), (3, 6), (11, 12), RequestSpan(5, 9), 8250),
            ((7, 9), (3, 10), (9, 12), RequestSpan(5, 11), 9250),
            ((8, 10), (3, 6), (9, 11), RequestSpan(12, 13), 7250),
            ((4, 5), (9, 10), (2, 3), RequestSpan(1, 8), 8250),
        ):
            with self.subTest(dense=dense_ticks, lexical=lexical_ticks, controlling=controlling_ticks, objects=objects_span):
                _, dense_span = self.search("dense", vector, ticks=dense_ticks)
                _, lexical_span = self.search("lexical", text, ticks=lexical_ticks)
                _, controlling_span = self.search("controlling", vector, ticks=controlling_ticks)
                metrics = TurnTimings(
                    record=1, extraction=0.75, embedding=5,
                    dense=dense_span, lexical=lexical_span, controlling=controlling_span, objects=objects_span,
                    fusion=0.25, rerank_passages=rerank_timings["rerank_passages"],
                    rerank_queue=rerank_timings["rerank_queue"], rerank=rerank_timings["rerank"], total=total,
                ).to_milliseconds()
                self.assertEqual(metrics["dense"], (dense_ticks[1] - dense_ticks[0]) * 1000)
                self.assertEqual(metrics["lexical"], (lexical_ticks[1] - lexical_ticks[0]) * 1000)
                self.assertEqual(metrics["controlling"], (controlling_ticks[1] - controlling_ticks[0]) * 1000)
                self.assertEqual(metrics["objects"], 0 if objects_span is None else objects_span.seconds * 1000)
                self.assertEqual(metrics["extraction"], 750)
                self.assertEqual(metrics["retrieval"], expected)
                self.assertEqual(metrics["total"], total * 1000)
                for stage, seconds in rerank_timings.items():
                    self.assertEqual(metrics[stage], seconds * 1000)
