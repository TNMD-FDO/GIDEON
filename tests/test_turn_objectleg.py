"""Exact-object multi-search requests, replies, and refusals."""

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx
from test_turn_legs import invalid_court_metadata, turn_fixture

from gideon.extraction import ExactObject
from gideon.turn import lexical, objectleg
from gideon.turn.fusion import LegHit
from gideon.turn.objects import TurnObject, object_leg
from gideon.turn.provenance import RequestSpan
from gideon.turn.states import State, TurnFailure

TYPED = "444 U. S. 987"
INDEXED = "444 U.S. 987"
MESSAGE = f"{TYPED}; {TYPED}; {INDEXED}"
INDEX = "fixture-generation"
USER = "fixture-reader"
RESERVATION = 3


def invalid_msearch_replies(reply: dict[str, Any]) -> dict[str, dict[str, object]]:
    """Corrupt a complete reply so the client must refuse the whole search."""

    held, citing = reply["responses"]
    invalid_items = {
        "short": [held],
        "long": [held, citing, held],
        "error": [held, {**citing, "error": "fixture diagnostic"}],
        "status": [held, {**citing, "status": 503}],
        "missing status": [held, {key: value for key, value in citing.items() if key != "status"}],
        "timeout": [held, {**citing, "timed_out": True}],
        "shard": [held, {**citing, "_shards": {**citing["_shards"], "failed": 1}}],
        "hit": [held, {**citing, "hits": {"hits": [False]}}],
    }
    return {label: {**reply, "responses": items} for label, items in invalid_items.items()}


class ObjectLeg(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = Path(self.enterContext(tempfile.TemporaryDirectory())) / "secret"
        self.secret.write_text("fixture-store-secret", encoding="utf-8")
        self.cite = TurnObject(0, ExactObject("case_cite", 0, len(TYPED), TYPED), INDEXED)
        self.searches = object_leg(self.cite.exact, INDEXED, MESSAGE)
        self.reply = turn_fixture("objects-msearch.json")

    def search(
        self, transport: httpx.MockTransport, *, objects: tuple[TurnObject, ...] | None = None,
        timeout: float = objectleg.OBJECT_LEG_TIMEOUT_SECONDS,
    ) -> objectleg.ObjectLegResult:
        async def run() -> objectleg.ObjectLegResult:
            async with httpx.AsyncClient(base_url="http://store.example.invalid", transport=transport) as client:
                return await objectleg.search(
                    client, USER, self.secret, INDEX, MESSAGE, (self.cite,) if objects is None else objects,
                    RESERVATION, collapse=True, clock=iter((4.0, 5.5)).__next__, timeout=timeout,
                )
        return asyncio.run(run())

    def test_body_sends_each_spelling_as_a_filter_and_the_message_as_optional(self) -> None:
        for collapse, indexed in ((True, INDEXED), (False, None)):
            with self.subTest(collapse=collapse):
                searches = object_leg(self.cite.exact, indexed, MESSAGE)
                body = objectleg.query_body(searches, MESSAGE, RESERVATION, collapse=collapse)
                self.assertTrue(body.endswith(b"\n"))
                head_held, held, head_citing, citing = map(json.loads, body.splitlines())
                self.assertEqual((head_held, head_citing), ({}, {}))
                self.assertEqual(objectleg.query_body(searches, MESSAGE, RESERVATION, collapse=collapse, courts=None), body)
                for search, clauses in (
                    (held, [{"match_phrase": {"caption": value}} for value in searches[0].values]),
                    (citing, [
                        {"term": {"cites": {"value": value, "case_insensitive": True}}}
                        for value in searches[1].values
                    ]),
                ):
                    clause = {"bool": {"should": clauses}} if indexed else clauses[0]
                    self.assertEqual(search, {
                        "query": {"bool": {"filter": [clause], "should": [{"match": {"text": MESSAGE}}]}},
                        "sort": [{"_score": "desc"}, {"doc_id": "asc"}, {"ordinal": "asc"}],
                        "size": RESERVATION,
                        "_source": lexical.query_body(MESSAGE, RESERVATION, collapse=collapse)["_source"],
                        **({"collapse": {"field": "dupe_cluster"}} if collapse else {}),
                    })
                courts = ("fx-court-b", "fx-court-a")
                scoped = objectleg.query_body(searches, MESSAGE, RESERVATION, collapse=collapse, courts=courts)
                for search in (held, citing):
                    search["query"]["bool"]["filter"].append({"terms": {"court": sorted(courts)}})
                self.assertEqual(list(map(json.loads, scoped.splitlines())), [head_held, held, head_citing, citing])

    def test_a_request_past_its_byte_bound_is_refused_before_it_grows(self) -> None:
        whole = objectleg.query_body(self.searches, MESSAGE, RESERVATION, collapse=True)
        self.assertEqual(
            objectleg.query_body(self.searches, MESSAGE, RESERVATION, collapse=True, max_bytes=len(whole)), whole,
        )
        with self.assertRaises(TurnFailure) as raised:
            objectleg.query_body(self.searches, MESSAGE, RESERVATION, collapse=True, max_bytes=len(whole) - 1)
        self.assertEqual((raised.exception.state, raised.exception.word), (State.INVALID_REQUEST, "message"))
        self.assertNotIn(TYPED, json.dumps(raised.exception.to_body()))

    def test_parser_keeps_zero_score_and_store_order_for_each_search(self) -> None:
        for reply in (self.reply, turn_fixture("objects-msearch-empty.json")):
            with self.subTest(reply=reply):
                results = objectleg.parse_reply(reply, self.searches)
                self.assertEqual(tuple(result.search.name for result in results), ("held", "citing"))
                self.assertEqual(tuple(result.hits for result in results), tuple(
                    tuple(
                        LegHit(row["_id"], rank, **row["_source"])
                        for rank, row in enumerate(item["hits"]["hits"], start=1)
                    )
                    for item in reply["responses"]
                ))

    def test_parser_refuses_missing_or_invalid_court_metadata_as_objects(self) -> None:
        row = self.reply["responses"][0]["hits"]["hits"][0]
        for label, bad_fields in invalid_court_metadata(row["_source"]).items():
            with self.subTest(label=label):
                row["_source"] = bad_fields
                with self.assertRaises(TurnFailure) as raised:
                    objectleg.parse_reply(self.reply, self.searches)
                self.assertEqual((raised.exception.state, raised.exception.word), (State.STORE_REPLY_INVALID, "objects"))

    def test_client_shares_equal_legs_and_reads_the_password_each_time(self) -> None:
        requests: list[httpx.Request] = []
        repeated_start = MESSAGE.index(TYPED, len(TYPED))
        repeated = TurnObject(
            1, ExactObject("case_cite", repeated_start, repeated_start + len(TYPED), TYPED), INDEXED,
        )
        distinct = TurnObject(
            2, ExactObject("case_cite", MESSAGE.index(INDEXED), len(MESSAGE), INDEXED), INDEXED,
        )
        empty = turn_fixture("objects-msearch-empty.json")

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={
                **self.reply, "responses": [*self.reply["responses"], *empty["responses"]],
            })

        for secret in ("fixture-store-secret", "fixture-replacement-secret"):
            with self.subTest(secret=secret):
                self.secret.write_text(f"{secret}\n", encoding="utf-8")
                result = self.search(httpx.MockTransport(respond), objects=(self.cite, repeated, distinct))
                hits = result.by_object
                request, = requests
                requests.clear()
                self.assertEqual((request.method, request.url.path), ("POST", f"/{INDEX}/_msearch"))
                self.assertEqual(request.headers["content-type"], "application/x-ndjson")
                self.assertEqual(request.headers["authorization"], "Basic " + base64.b64encode(
                    f"{USER}:{secret}".encode(),
                ).decode())
                self.assertEqual(len(request.content.splitlines()), 2 * (len(self.searches) + len(empty["responses"])))
                self.assertEqual(result.span, RequestSpan(4.0, 5.5))
                self.assertEqual(result.limit, RESERVATION)
                self.assertEqual(result.searches, len(self.reply["responses"]) + len(empty["responses"]))
                self.assertEqual(result.returned, sum(len(item["hits"]["hits"]) for item in self.reply["responses"]))
                self.assertEqual(tuple(hits), (0, 1, 2))
                self.assertEqual(hits[0], objectleg.parse_reply(self.reply, self.searches))
                self.assertEqual(hits[1], hits[0])
                self.assertEqual(hits[2], objectleg.parse_reply(empty, object_leg(distinct.exact, INDEXED, MESSAGE)))

    def test_no_leg_needs_no_password_or_request(self) -> None:
        self.secret.unlink()
        start = MESSAGE.index(TYPED)
        unsupported = TurnObject(0, ExactObject("caption", start, start + len(TYPED), TYPED), None)

        def unexpected(_: httpx.Request) -> httpx.Response:
            self.fail("The store should not receive a request.")

        for objects in ((), (unsupported,)):
            with self.subTest(objects=objects):
                self.assertEqual(
                    self.search(httpx.MockTransport(unexpected), objects=objects),
                    objectleg.ObjectLegResult({}, None, RESERVATION, returned=0, searches=0, owners={}),
                )

    def test_invalid_items_refuse_an_otherwise_successful_reply(self) -> None:
        for label, reply in invalid_msearch_replies(self.reply).items():
            with self.subTest(label=label):
                with self.assertRaises(TurnFailure) as invalid:
                    self.search(httpx.MockTransport(lambda _, body=reply: httpx.Response(200, json=body)))
                self.assertEqual((invalid.exception.state, invalid.exception.word), (State.STORE_REPLY_INVALID, "objects"))

    def test_client_maps_non_ok_json_transport_and_deadline_failures(self) -> None:
        def unavailable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("fixture diagnostic", request=request)

        def read_timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("fixture diagnostic", request=request)

        async def stalled(_: httpx.Request) -> httpx.Response:
            await asyncio.Future()
            self.fail("The fixture request should have timed out.")

        for handler, state, timeout in (
            (lambda _: httpx.Response(503), State.STORE_UNAVAILABLE, 2),
            (lambda _: httpx.Response(200, content="fixture invalid JSON"), State.STORE_REPLY_INVALID, 2),
            (unavailable, State.STORE_UNAVAILABLE, 2),
            (read_timeout, State.RETRIEVAL_TIMEOUT, 2),
            (stalled, State.RETRIEVAL_TIMEOUT, 0),
        ):
            with self.subTest(handler=handler):
                with self.assertRaises(TurnFailure) as failure:
                    self.search(httpx.MockTransport(handler), timeout=timeout)
                self.assertEqual((failure.exception.state, failure.exception.word), (state, "objects"))

    def test_client_refuses_missing_or_blank_password(self) -> None:
        def unexpected(_: httpx.Request) -> httpx.Response:
            self.fail("The store should not receive an unauthenticated request.")

        self.secret.write_text(" \n", encoding="utf-8")
        for exists in (True, False):
            with self.subTest(exists=exists):
                if not exists:
                    self.secret.unlink()
                with self.assertRaises(TurnFailure) as failure:
                    self.search(httpx.MockTransport(unexpected))
                self.assertEqual((failure.exception.state, failure.exception.word), (State.STORE_UNAVAILABLE, "objects"))
