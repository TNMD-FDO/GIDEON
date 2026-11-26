"""Exercise retrieval and the judged answer through the API's turn route."""

import asyncio
import json
import logging
import sys
import tempfile
import unittest
import warnings
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from functools import partial
from itertools import count
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send
from test_api_completions import ASGISession, FixtureStream, GatedStream
from test_lock_coupling import MODELS_LOCK_PATH
from test_turn_answer import ENGINE_ERROR, payload, recorded_answer
from test_turn_generation import LiveRow, record_with_rows
from test_turn_generator import generator_pin
from test_turn_legs import FIXTURES, turn_fixture
from test_turn_objectleg import invalid_msearch_replies

from gideon import guardrail
from gideon.api.app import create_app
from gideon.api.progress import REASONING_KEYS
from gideon.api.settings import RecordCoordinates, Settings
from gideon.api.sse import DONE_EVENT, SSE_EVENT_BUFFER_LIMIT_BYTES, EventReassembler
from gideon.extraction import ExactObject, extract
from gideon.host.models import load_models_lock
from gideon.turn import lexical, objectleg, objects
from gideon.turn.answer import LEGAL_BRANCH
from gideon.turn.config import RETRIEVAL_CONFIG, config_hash
from gideon.turn.generation import TURN_COLLECTION
from gideon.turn.generator import GENERATOR_SAMPLING, RESEARCH_MAX_TOKENS
from gideon.turn.objects import CaseExtractor
from gideon.turn.prompt import messages

MESSAGE = "Fictitious turn sentinel Cobalt Widget Q7"
FULL_TURN: dict[str, object] = {"message": MESSAGE}
RETRIEVE = {"message": MESSAGE, "stop_after": "retrieve"}
MODELS_ROUTE = "/v1/models"
COMPLETIONS_ROUTE = "/v1/chat/completions"
CHAT_ID = "fixture-research-chat"
DEADLINE_TEXT = "The deadline is April 7, 2034."
NEUTRAL_SENTENCE = "The fictitious harbor stayed open. "
OPEN_TEXT = NEUTRAL_SENTENCE * (guardrail.LAG_CHARS // len(NEUTRAL_SENTENCE) + 1)


def sse(data: str) -> bytes:
    return f"data: {data}\n\n".encode()


def completion(stream: httpx.AsyncByteStream, *, status: int = 200, content_type: str = "text/event-stream") -> httpx.Response:
    return httpx.Response(status, headers={"content-type": content_type}, stream=stream)


class WaitingStream(FixtureStream):
    """Hold an open engine response with a checkpoint in its close."""

    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.waiting = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body
        self.waiting.set()
        await asyncio.Future()

    async def aclose(self) -> None:
        await asyncio.sleep(0)
        await super().aclose()


class TurnEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        listing = turn_fixture("embed-listing.json")
        embedding = turn_fixture("embedding.json")
        dense = turn_fixture("dense-grouped.json")
        lexical = turn_fixture("lexical-collapsed.json")
        self.lexical_hits = lexical["hits"]["hits"]
        self.groups = dense["result"]["groups"]
        self.generation = self.lexical_hits[0]["_index"]
        self.lockfile, embedding_space = self.generation.split("__")[1:]
        self.dense_path = f"/collections/{self.generation}/points/query/groups"
        self.lexical_path = f"/{self.generation}/_search"
        self.msearch_path = f"/{self.generation}/_msearch"
        self.mget_path = f"/{self.generation}/_mget"
        self.passage_doc = turn_fixture("passages-mget.json")["docs"][0]
        self.generator_listing = turn_fixture("generator-listing.json")
        self.capture = (FIXTURES / "generator-stream.sse").read_bytes()
        self.generator_stream = FixtureStream(self.capture)
        self.listing_reply: object = self.generator_listing
        self.completions: list[httpx.Response | Exception] = [
            completion(self.generator_stream, content_type="text/event-stream; charset=utf-8"),
        ]
        self.upstream_replies: dict[str, object] = {
            MODELS_ROUTE: listing,
            "/v1/embeddings": embedding,
            self.dense_path: dense,
            self.lexical_path: lexical,
        }
        self.requests: dict[str, httpx.Request] = {}
        self.engine_requests: list[httpx.Request] = []
        self.case_extractor: CaseExtractor | None = lambda _: ()
        self.trips: list[guardrail.TripRow] = []
        self.enterContext(patch.object(guardrail.writer, "dispatch_trip_row", self.trips.append))
        self.live_rows: tuple[LiveRow, ...] = ((self.generation, TURN_COLLECTION, embedding_space, [self.lockfile]),)
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ("record", "dense", "embedder", "lexical"):
            (root / name).write_text(f"fixture-{name}-secret\n", encoding="utf-8")
        record = RecordCoordinates("fixture-record", 5432, "fixture-db", "fixture-reader", root / "record")
        model = listing["data"][0]
        self.settings = Settings(
            engine_url="http://fixture-engine/v1", engine_api_key="fixture-engine-key", api_key="fixture-api-key",
            port=8000, source_header="X-Fixture-Source", chat_header="X-Fixture-Chat", user_header="X-Fixture-User",
            eval_identity="eval@example.invalid", instruction="Fictitious General instruction.",
            model_id="fixture-general", engine_model=self.generator_listing["data"][0]["id"], record=record, corpus_record=record,
            cas_root=root / "cas", qdrant_url="http://fixture-qdrant:7001", qdrant_key_file=root / "dense",
            embed_url="http://fixture-embedding:7002", embed_key_file=root / "embedder",
            opensearch_url="http://fixture-opensearch:7003", opensearch_user="fixture-turn-reader",
            opensearch_password_file=root / "lexical", embed_model=model["id"], embed_repository=model["root"],
            embed_space=embedding_space, embed_dimensions=len(embedding["data"][0]["embedding"]),
        )

    def upstream(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.assertNotIn(path, self.requests)
        self.requests[path] = request
        if path == self.mget_path:
            docs = [
                {**self.passage_doc, "_index": self.generation, "_id": item["_id"]}
                for item in json.loads(request.content)["docs"]
            ]
            return httpx.Response(200, json={"docs": list(reversed(docs))})
        reply = self.upstream_replies[path]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply)

    def engine_upstream(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.engine_requests.append(request)
        reply: object
        if path == COMPLETIONS_ROUTE:
            attempt = self.engine_paths().count(COMPLETIONS_ROUTE)
            reply = self.completions[attempt - 1]
        else:
            self.assertEqual(path, MODELS_ROUTE)
            reply = self.listing_reply
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply)

    def engine_paths(self) -> list[str]:
        return [request.url.path for request in self.engine_requests]

    def app(self, settings: Settings, rows: Sequence[LiveRow] | None = None) -> Starlette:
        self.requests.clear()
        self.engine_requests.clear()
        self.trips.clear()
        self.connection, self.cursor = record_with_rows(self.live_rows if rows is None else rows)
        self.connect = AsyncMock(return_value=self.connection)
        return create_app(
            settings, transport=httpx.MockTransport(self.engine_upstream), embed_transport=httpx.MockTransport(self.upstream),
            qdrant_transport=httpx.MockTransport(self.upstream), opensearch_transport=httpx.MockTransport(self.upstream),
            record_connect=self.connect, case_extractor=self.case_extractor, turn_clock=count().__next__,
        )

    def headers(self, settings: Settings, *, eval_caller: bool = True) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.api_key}",
            settings.source_header: settings.eval_identity if eval_caller else "reader@example.invalid",
            settings.chat_header: CHAT_ID,
        }

    def request(
        self, *, settings: Settings | None = None, rows: Sequence[LiveRow] | None = None,
        payload: dict[str, object] | None = None, eval_caller: bool = True,
        on_send: Callable[[Message], None] | None = None, private_values: tuple[str, ...] = (),
    ) -> httpx.Response:
        configuration = self.settings if settings is None else settings
        app = self.app(configuration, rows)

        async def observe(scope: Scope, receive: Receive, send: Send) -> None:
            async def send_observed(message: Message) -> None:
                if on_send is not None:
                    on_send(message)
                await send(message)

            await app(scope, receive, send_observed)

        async def run() -> httpx.Response:
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=observe), base_url="http://fixture-api",
            ) as client:
                return await client.post(
                    "/turn", json=RETRIEVE if payload is None else payload,
                    headers=self.headers(configuration, eval_caller=eval_caller),
                )

        with self.assertLogs(level="DEBUG") as captured:
            response = asyncio.run(run())
        self.turn_logs = [record for record in captured.records if record.name == "gideon.turn"]
        for value in (MESSAGE, *private_values):
            self.assertNotIn(value, response.text)
            for record in captured.records:
                self.assertNotIn(value, record.getMessage())
                self.assertNotIn(value, repr(vars(record)))
        return response

    def assert_refused(
        self, response: httpx.Response, status: int, state: str, word: str | None = None, *, pinned: bool | None = False,
    ) -> None:
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.headers["content-type"], "application/json")
        body = response.json()
        self.assertEqual(set(body), {"error"})
        self.assertEqual((body["error"]["code"], body["error"]["param"]), (state, word))
        line, = self.turn_logs
        self.assertIn(f"state={state}", line.getMessage())
        self.assertIn(f"word={word or '-'}", line.getMessage())
        generations = ("-", self.generation) if pinned is None else (self.generation if pinned else "-",)
        self.assertIn(line.getMessage().partition(" generation=")[2].partition(" ")[0], generations)
        self.assertIsNone(line.exc_info)

    def test_eval_retrieve_returns_the_fused_pool_and_provenance(self) -> None:
        response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.requests.keys(), self.upstream_replies.keys())
        self.assertEqual(self.engine_requests, [])
        self.assertEqual(self.requests["/v1/models"].method, "GET")
        self.assertEqual(self.requests["/v1/embeddings"].method, "POST")
        self.assertEqual(self.requests[self.dense_path].method, "POST")
        self.assertEqual(self.requests[self.lexical_path].method, "POST")
        self.connect.assert_awaited_once()
        self.cursor.execute.assert_awaited_once()
        self.connection.close.assert_awaited_once()

        body = response.json()
        self.assertEqual(body["stage"], "retrieve")
        self.assertEqual(body["timings_ms"]["objects"], 0)
        self.assertGreater(body["timings_ms"]["extraction"], 0)
        self.assertEqual(body["generation"], {
            "gen_id": self.generation, "collection": TURN_COLLECTION, "lockfiles": [self.lockfile],
            "embedding_space": self.settings.embed_space,
        })
        self.assertEqual(body["retrieval_config_hash"], config_hash(RETRIEVAL_CONFIG))
        pool = body["pool"]
        groups = self.groups
        lexical_hits = self.lexical_hits
        shared = groups[0]["hits"][0]["payload"]["chunk_id"]
        duplicate = groups[1]["hits"][0]["payload"]["chunk_id"]
        boilerplate = groups[2]["hits"][0]["payload"]["chunk_id"]
        self.assertEqual([entry["chunk_id"] for entry in pool[:2]], [shared, duplicate])
        self.assertEqual({entry["chunk_id"] for entry in pool[2:]}, {boilerplate, lexical_hits[2]["_id"]})
        self.assertEqual(pool[0]["leg_ranks"], {"dense": 1, "lexical": 1})
        self.assertEqual(pool[1]["leg_ranks"], {"dense": 2})
        self.assertTrue(all(entry["pinned"] is False and entry["object"] is None for entry in pool))
        self.assertEqual(len({entry["dupe_cluster"] for entry in pool}), len(pool))
        self.assertEqual(
            body["provenance"], {
                "plan_mode": RETRIEVAL_CONFIG["query_plan"],
                "sub_queries": [{"source": "message"}],
                "objects": [],
                "out_of_corpus_objects": [],
                "filters": {
                    "floor": {"collections": [TURN_COLLECTION], "kb_ids": []},
                    "user_stated_scope": None,
                    "narrowing": None,
                },
                "legs": {
                    "dense": {"store": "qdrant", "limit": RETRIEVAL_CONFIG["leg_limit"], "returned": len(groups)},
                    "lexical": {
                        "store": "opensearch", "limit": RETRIEVAL_CONFIG["leg_limit"], "returned": len(lexical_hits),
                    },
                    "objects": {
                        "store": "opensearch", "limit": RETRIEVAL_CONFIG["reservations.exact_object"],
                        "returned": 0, "searches": 0,
                    },
                },
                "tools": [],
                "fusion": {"union": len(groups) + len(lexical_hits) - 1, "after_collapse": len(pool),
                           "weighted": 1, "cut": 0, "pinned": 0, "pinned_cut": 0},
            },
        )

    def test_eval_reporter_cite_pins_the_held_chunk_at_rank_one(self) -> None:
        citation = "444 U. S. 987"  # Fictitious reporter cite.
        exact = ExactObject("case_cite", 0, len(citation), citation)
        adapter = Mock(return_value=((exact, "444 U.S. 987"),))
        self.case_extractor = adapter

        reply = turn_fixture("objects-msearch.json")
        held = reply["responses"][0]
        empty_citing = turn_fixture("objects-msearch-empty.json")["responses"][1]
        self.upstream_replies[self.msearch_path] = {**reply, "responses": [held, empty_citing]}
        held_chunk = held["hits"]["hits"][0]["_id"]
        self.assertNotIn(held_chunk, {row["_id"] for row in self.lexical_hits})
        self.assertNotIn(held_chunk, {hit["payload"]["chunk_id"] for group in self.groups for hit in group["hits"]})

        response = self.request(payload={**RETRIEVE, "message": citation})

        self.assertEqual(response.status_code, 200, response.text)
        adapter.assert_called_once_with(citation)
        self.assertEqual(self.requests[self.msearch_path].method, "POST")
        body = response.json()
        first = body["pool"][0]
        self.assertEqual(first["chunk_id"], held_chunk)
        self.assertEqual(first["pool_rank"], 1)
        self.assertIs(first["pinned"], True)
        self.assertEqual(first["object"], 0)
        self.assertEqual(body["provenance"]["out_of_corpus_objects"], [])
        self.assertIs(body["provenance"]["objects"][0]["out_of_corpus"], False)

    def test_unmatched_keyed_object_is_unresolved_and_seats_nothing(self) -> None:
        message = "Fictitious statute lookup for 18 U.S.C. § 9999"
        (obj,) = extract(message)
        assert obj.key is not None
        empty = turn_fixture("objects-msearch-empty.json")
        self.upstream_replies[self.msearch_path] = {**empty, "responses": empty["responses"][:1]}
        payload: dict[str, object] = {**RETRIEVE, "message": message}
        private_values = (obj.text, obj.key)

        response = self.request(payload=payload, private_values=private_values)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        provenance = body["provenance"]
        self.assertEqual(provenance["objects"], [{
            "index": 0, "type": "statute", "start": obj.start, "end": obj.end,
            "pattern_id": obj.pattern_id, "fields": ["cites"], "leg": 0, "hits": {"matched": 0},
            "seats": 0, "out_of_corpus": True,
        }])
        self.assertEqual(provenance["out_of_corpus_objects"], [{
            "index": 0, "type": "statute", "resolved": "unresolved", "citing_hits": 0,
        }])
        self.assertEqual(provenance["fusion"]["pinned"], 0)
        self.assertTrue(all(entry["object"] is None and entry["pinned"] is False for entry in body["pool"]))
        self.assertGreater(body["timings_ms"]["objects"], 0)

        with patch.dict(self.upstream_replies, {self.msearch_path: httpx.ConnectError(message)}):
            refused = self.request(payload=payload, private_values=private_values)
        self.assert_refused(refused, 503, "store_unavailable", "objects", pinned=True)

    def test_citing_hits_seat_an_unresolved_citation_and_a_shared_leg_counts_once(self) -> None:
        citation, indexed = "444 U. S. 987", "444 U.S. 987"  # Fictitious reporter cite.
        message = f"{citation}; {citation}"
        citations = tuple(
            ExactObject("case_cite", start, start + len(citation), citation, pattern_id="fixture/cite@1")
            for start in (0, message.rindex(citation))
        )
        self.case_extractor = Mock(return_value=tuple((obj, indexed) for obj in citations))
        reply = turn_fixture("objects-msearch.json")
        empty_held = turn_fixture("objects-msearch-empty.json")["responses"][0]
        citing = reply["responses"][1]
        responses = [empty_held, citing]
        self.upstream_replies[self.msearch_path] = {**reply, "responses": responses}
        citing_rows = citing["hits"]["hits"]
        cap = 1

        response = self.request(
            payload={**RETRIEVE, "message": message, "overrides": {"pool_cap": cap}},
            private_values=(citation, indexed),
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        provenance = body["provenance"]
        self.assertEqual(provenance["objects"], [
            {
                "index": index, "type": obj.type, "start": obj.start, "end": obj.end,
                "pattern_id": obj.pattern_id, "fields": ["caption", "cites"], "leg": 0,
                "hits": {"held": 0, "citing": len(citing_rows)},
                "seats": cap if index == 0 else 0, "out_of_corpus": True,
            }
            for index, obj in enumerate(citations)
        ])
        self.assertEqual(provenance["out_of_corpus_objects"], [
            {"index": index, "type": obj.type, "resolved": "unresolved", "citing_hits": len(citing_rows)}
            for index, obj in enumerate(citations)
        ])
        self.assertEqual(provenance["legs"]["objects"], {
            "store": "opensearch", "limit": RETRIEVAL_CONFIG["reservations.exact_object"],
            "returned": len(citing_rows), "searches": len(responses),
        })
        self.assertEqual(provenance["fusion"]["pinned"], cap)
        self.assertEqual(provenance["fusion"]["pinned_cut"], len(citing_rows) - cap)

        # The shared leg is seated once: a second occurrence takes no reservation of its own.
        one_seat = self.request(
            payload={**RETRIEVE, "message": message, "overrides": {"reservations.exact_object": 1}},
            private_values=(citation, indexed),
        )
        self.assertEqual(one_seat.status_code, 200, one_seat.text)
        seated = one_seat.json()
        self.assertGreater(len(citing_rows), 1)
        self.assertEqual([obj["seats"] for obj in seated["provenance"]["objects"]], [1, 0])
        self.assertEqual(seated["provenance"]["fusion"]["pinned"], 1)
        self.assertEqual([entry["object"] for entry in seated["pool"] if entry["pinned"]], [0])
        (first,) = body["pool"]
        self.assertEqual(first["chunk_id"], citing_rows[0]["_id"])
        self.assertEqual(first["pool_rank"], 1)
        self.assertIs(first["pinned"], True)
        self.assertEqual(first["object"], 0)

    def test_missing_adapter_logs_one_startup_warning_and_refuses_extraction(self) -> None:
        self.case_extractor = None
        with (
            patch.object(objects, "load_case_extractor", return_value=None) as load,
            self.assertLogs("gideon.api.startup", level="WARNING") as startup,
        ):
            response = self.request()
        load.assert_called_once_with()
        warning, = startup.records
        self.assertEqual(warning.getMessage(), "Could not load gideon.casecite.adapter")
        self.assert_refused(response, 503, "extraction_unavailable", "extraction", pinned=None)
        self.assertEqual(self.requests, {})

    def test_adapter_exception_and_contract_violation_refuse_extraction(self) -> None:
        citation = "444 U. S. 987"  # Fictitious reporter cite.
        invalid = ExactObject("case_cite", 0, len(citation), citation, key="fixture/invalid-case-key")
        for kind, adapter in (
            ("exception", Mock(side_effect=RuntimeError(citation))),
            ("contract", Mock(return_value=((invalid, None),))),
        ):
            with self.subTest(kind=kind):
                self.case_extractor = adapter
                response = self.request(payload={**RETRIEVE, "message": citation}, private_values=(citation,))
                adapter.assert_called_once_with(citation)
                self.assert_refused(response, 500, "extraction_failed", "extraction", pinned=None)
                self.assertEqual(self.requests, {})

    def test_expired_extraction_bound_refuses_the_turn(self) -> None:
        adapter = Mock(return_value=())
        self.case_extractor = adapter
        with patch.object(objects, "EXTRACTION_TIMEOUT_SECONDS", 0):
            response = self.request()
        self.assert_refused(response, 504, "retrieval_timeout", "extraction", pinned=None)
        adapter.assert_not_called()
        self.assertEqual(self.requests, {})

    def test_object_search_refusals_drop_citation_diagnostics(self) -> None:
        citation = "444 U. S. 987"  # Fictitious reporter cite.
        obj = ExactObject("case_cite", 0, len(citation), citation)

        def emit_diagnostics(_: str) -> tuple[tuple[ExactObject, str | None], ...]:
            warnings.warn_explicit(citation, UserWarning, "fictional.py", 1, module="eyecite.fixture")
            logging.getLogger("eyecite.fixture").error("Fictitious citation diagnostic %s", citation)
            return ((obj, None),)

        adapter = Mock(side_effect=emit_diagnostics)
        module = ModuleType("gideon.casecite.adapter")
        module.__dict__["extract_indexed_case_cites"] = adapter
        self.case_extractor = None
        caught = self.enterContext(warnings.catch_warnings(record=True))
        warnings.simplefilter("always")
        for name in objects.CITATION_LOGGERS:
            logger = logging.getLogger(name)
            self.enterContext(patch.object(logger, "handlers", logger.handlers.copy()))
            self.enterContext(patch.object(logger, "propagate", logger.propagate))
        self.enterContext(patch.dict(sys.modules, {module.__name__: module}))

        reply = turn_fixture("objects-msearch.json")
        self.upstream_replies[self.msearch_path] = reply
        payload: dict[str, object] = {**RETRIEVE, "message": citation}
        response = self.request(payload=payload, private_values=(citation,))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["provenance"]["objects"][0]["type"], "case_cite")

        invalid = invalid_msearch_replies(reply)
        for label, bad_reply in invalid.items():
            with self.subTest(reply=label), patch.dict(self.upstream_replies, {self.msearch_path: bad_reply}):
                response = self.request(payload=payload, private_values=(citation,))
                self.assert_refused(response, 502, "store_reply_invalid", "objects", pinned=True)

        for path, word in ((self.msearch_path, "objects"), (self.lexical_path, "lexical")):
            with self.subTest(leg=word), patch.dict(self.upstream_replies, {path: httpx.Response(503, text=citation)}):
                response = self.request(payload=payload, private_values=(citation,))
                self.assert_refused(response, 503, "store_unavailable", word, pinned=True)

        lexical_reply = {**turn_fixture("lexical-collapsed.json"), "timed_out": True}
        with patch.dict(self.upstream_replies, {self.lexical_path: lexical_reply}):
            response = self.request(payload=payload, private_values=(citation,))
        self.assert_refused(response, 502, "store_reply_invalid", "lexical", pinned=True)
        self.assertEqual(adapter.call_count, len(invalid) + 4)
        self.assertEqual(caught, [])

    def test_object_and_lexical_request_deadlines_name_the_expired_leg(self) -> None:
        citation = "444 U. S. 987"  # Fictitious reporter cite.
        obj = ExactObject("case_cite", 0, len(citation), citation)
        self.case_extractor = Mock(return_value=((obj, None),))
        self.upstream_replies[self.msearch_path] = turn_fixture("objects-msearch.json")
        respond = self.upstream
        cancelled = []
        for leg, path, word in (
            (objectleg, self.msearch_path, "objects"), (lexical, self.lexical_path, "lexical"),
        ):
            async def stall(request: httpx.Request, stall_path: str = path) -> httpx.Response:
                if request.url.path == stall_path:
                    try:
                        await asyncio.Future()
                    finally:
                        cancelled.append(stall_path)
                    raise AssertionError("Fictitious store unexpectedly resumed.")
                return respond(request)

            with (
                self.subTest(leg=word),
                patch.object(self, "upstream", stall),
                patch.object(leg, "search", partial(leg.search, timeout=0)),
            ):
                response = self.request(payload={**RETRIEVE, "message": citation}, private_values=(citation,))
                self.assert_refused(response, 504, "retrieval_timeout", word, pinned=True)
        self.assertEqual(cancelled, [self.msearch_path, self.lexical_path])

    def test_eval_pool_cap_updates_pool_provenance_and_config_hash(self) -> None:
        cap = 2
        response = self.request(payload={**RETRIEVE, "overrides": {"pool_cap": cap}})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        expected = [group["hits"][0]["payload"]["chunk_id"] for group in self.groups[:cap]]
        self.assertEqual([entry["chunk_id"] for entry in body["pool"]], expected)
        self.assertEqual(body["retrieval_config_hash"], config_hash({**RETRIEVAL_CONFIG, "pool_cap": cap}))
        clusters = {group["id"] for group in self.groups} | {
            row["_source"]["dupe_cluster"] for row in self.lexical_hits
        }
        self.assertEqual(body["provenance"]["fusion"]["after_collapse"], len(clusters))
        self.assertEqual(body["provenance"]["fusion"]["cut"], len(clusters) - cap)

    def test_space_mismatches_name_the_reason_without_a_dense_request(self) -> None:
        cases: tuple[tuple[str, Settings, dict[str, object]], ...] = (
            ("generation", replace(self.settings, embed_space="fixture-other-space"), {}),
            ("repository", replace(self.settings, embed_repository="fixture-owner/other-model"), {}),
            ("width", replace(self.settings, embed_dimensions=self.settings.embed_dimensions + 1), {}),
            ("listing", self.settings, {"/v1/models": {"data": []}}),
        )
        for reason, settings, replies in cases:
            with self.subTest(reason=reason), patch.dict(self.upstream_replies, replies):
                self.assert_refused(self.request(settings=settings), 503, "embedder_outside_space", reason, pinned=True)
                self.assertNotIn(httpx.URL(settings.qdrant_url).host, {request.url.host for request in self.requests.values()})

    def test_missing_or_ambiguous_generation_refuses_without_a_pool(self) -> None:
        rows: tuple[LiveRow, ...] = (*self.live_rows, (
            "fixture-second-generation", TURN_COLLECTION, self.settings.embed_space, [self.lockfile],
        ))
        for state, live_rows in (("no_live_generation", ()), ("ambiguous_generation", rows)):
            with self.subTest(state=state):
                self.assert_refused(self.request(rows=live_rows), 503, state)
                self.assertEqual(self.requests, {})

    def test_caller_refusal_never_reaches_the_record_stores_or_generator(self) -> None:
        self.assert_refused(self.request(payload={"message": MESSAGE}, eval_caller=False), 403, "caller_not_admitted")
        self.assertEqual(self.requests, {})
        self.assertEqual(self.engine_requests, [])
        self.connect.assert_not_awaited()

    def test_unexpected_exception_returns_a_fixed_error_and_logs_only_the_exception_class(self) -> None:
        self.upstream_replies["/v1/embeddings"] = RuntimeError(MESSAGE)
        self.assert_refused(self.request(), 500, "internal_error", pinned=True)
        self.assertIn("exception=RuntimeError", self.turn_logs[0].getMessage())

    def read_events(self, response: httpx.Response) -> list[tuple[str, dict[str, Any]]]:
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/event-stream")
        frames = response.content.split(b"\n\n")
        self.assertEqual(frames.pop(), b"")
        emitted = []
        for frame in frames:
            kind, data = frame.split(b"\n")
            self.assertTrue(kind.startswith(b"event: ") and data.startswith(b"data: "))
            emitted.append((kind.removeprefix(b"event: ").decode(), json.loads(data.removeprefix(b"data: "))))
        return emitted

    def test_full_turn_streams_passages_phases_text_and_the_complete_answer_in_order(self) -> None:
        retrieval = self.request()
        pool = retrieval.json()["pool"]
        response = self.request(payload={"message": MESSAGE})
        emitted = self.read_events(response)
        source_count = len(pool)
        self.assertGreater(source_count, 0)
        self.assertEqual(emitted[:source_count], [
            ("passage", {"number": number, "chunk_id": entry["chunk_id"], "pool_rank": entry["pool_rank"]})
            for number, entry in enumerate(pool, start=1)
        ])
        retrieval_phase = emitted[source_count][1]["retrieval"]
        expected_retrieval = retrieval.json()
        # Extraction and record reads can finish in either order.
        for stage in ("extraction", "record"):
            expected_retrieval["timings_ms"][stage] = retrieval_phase["timings_ms"][stage]
        self.assertEqual(emitted[source_count:source_count + 2], [
            ("phase", {"stage": "retrieve", "retrieval": expected_retrieval}),
            ("phase", {"stage": "generate", "attempt": 1}),
        ])
        self.assertEqual(
            json.dumps(retrieval_phase, ensure_ascii=False, separators=(",", ":")).encode(),
            JSONResponse(expected_retrieval).body,
        )

        payloads = [line.removeprefix(b"data: ").decode() for line in self.capture.splitlines() if line.startswith(b"data: ")]
        expected, finish_reason = recorded_answer(payloads)
        self.assertEqual(finish_reason, "stop")
        text_events = emitted[source_count + 2:-1]
        self.assertTrue(text_events)
        self.assertTrue(all(kind == "text" and body.keys() == {"text"} for kind, body in text_events))
        self.assertEqual("".join(body["text"] for _, body in text_events), expected)
        kind, terminal = emitted[-1]
        self.assertEqual(kind, "answer")
        timings = terminal["timings_ms"]
        self.assertEqual(terminal, {"text": expected, "end": "complete", "timings_ms": timings})
        self.assertEqual(timings, {
            **retrieval_phase["timings_ms"],
            "passages": 1000, "first_text": retrieval_phase["timings_ms"]["total"] + 4000,
            "generation": 2000, "total": retrieval_phase["timings_ms"]["total"] + 5000,
        })

        hidden = "".join(
            choice["delta"].get(key) or ""
            for item in payloads if item != DONE_EVENT
            for choice in json.loads(item)["choices"] for key in REASONING_KEYS
        )
        self.assertTrue(hidden)
        self.assertNotIn(json.dumps(hidden)[1:-1].encode(), response.content)
        for key in REASONING_KEYS:
            self.assertNotIn(f'"{key}":'.encode(), response.content)
        self.assertTrue(self.generator_stream.closed)
        self.assertEqual(self.turn_logs, [])
        self.assertEqual(self.engine_paths(), [MODELS_ROUTE, COMPLETIONS_ROUTE])
        listing_request, request = self.engine_requests
        self.assertEqual(listing_request.method, "GET")
        self.assertEqual((request.method, request.headers["content-type"]), ("POST", "application/json"))
        self.assertEqual(json.loads(self.requests[self.mget_path].content), {
            "docs": [{"_id": entry["chunk_id"], "_source": ["text", "caption"]} for entry in pool],
        })
        source = self.passage_doc["_source"]
        sampling = GENERATOR_SAMPLING[self.generator_listing["data"][0]["root"]]
        self.assertEqual(json.loads(request.content), {
            "model": self.settings.engine_model,
            "messages": messages(MESSAGE, [(source["caption"], source["text"])] * source_count),
            "max_tokens": RESEARCH_MAX_TOKENS,
            **sampling.fields,
            "stream": True,
        })

    def test_failed_or_malformed_generator_listings_are_json_refusals(self) -> None:
        cases: tuple[tuple[str, object], ...] = (
            ("unreachable", httpx.ConnectError("Fictitious generator connection failure")),
            ("non-200", httpx.Response(503, json=self.generator_listing)),
            ("not JSON", httpx.Response(200, content=b"Fictitious HTML")),
            ("non-JSON constant", httpx.Response(200, content=b'{"data":[],"fixture":NaN}')),
            ("nonobject", []),
            ("no data", {}),
            ("nonlist data", {"data": {}}),
            ("nonobject entry", {"data": [None]}),
            ("missing id", {"data": [{"root": "example/fixture-model"}]}),
            ("nonstring id", {"data": [{"id": 1}]}),
        )
        for label, listing in cases:
            with self.subTest(label=label), patch.object(self, "listing_reply", listing):
                response = self.request(payload={"message": MESSAGE})
                self.assert_refused(response, 503, "generator_unavailable", "generate", pinned=True)
                self.assertEqual(self.engine_paths(), [MODELS_ROUTE])

    def test_generator_listing_without_one_supported_repository_is_a_json_refusal(self) -> None:
        entry = self.generator_listing["data"][0]
        lock = load_models_lock(MODELS_LOCK_PATH).lock
        assert lock is not None
        trial = generator_pin(lock.profile("1x32v-120d"))
        self.assertNotIn(trial.repo, GENERATOR_SAMPLING)
        cases = (
            ("unlisted served name", [{**entry, "id": "fixture-unlisted-model"}]),
            ("duplicate served name", [entry, entry]),
            ("no repository", [{"id": self.settings.engine_model}]),
            ("nonstring repository", [{**entry, "root": []}]),
            ("outside release", [{**entry, "root": "example/fixture-model"}]),
        )
        for label, entries in cases:
            with self.subTest(label=label), patch.object(self, "listing_reply", {"data": entries}):
                response = self.request(payload={"message": MESSAGE})
                self.assert_refused(response, 503, "generator_outside_release", "generate", pinned=True)
                self.assertEqual(self.engine_paths(), [MODELS_ROUTE])

        trial_entry = {**entry, "id": trial.serve.served_name, "root": trial.repo}
        with patch.object(self, "listing_reply", {"data": [trial_entry]}):
            settings = replace(self.settings, engine_model=trial.serve.served_name)
            response = self.request(settings=settings, payload={"message": MESSAGE})
            self.assert_refused(response, 503, "generator_outside_release", "generate", pinned=True)
            self.assertEqual(self.engine_paths(), [MODELS_ROUTE])

    def assert_stream_failure(self, emitted: list[tuple[str, dict[str, Any]]], state: str, *, word: str | None = "generate") -> None:
        kind, body = emitted[-1]
        self.assertEqual(kind, "failure")
        self.assertEqual(set(body), {"error"})
        self.assertEqual((body["error"]["code"], body["error"]["param"]), (state, word))
        self.assertEqual([kind for kind, _ in emitted if kind in {"answer", "failure"}], ["failure"])
        line, = self.turn_logs
        self.assertIn(f"state={state} word={word or '-'} generation={self.generation}", line.getMessage())
        self.assertIsNone(line.exc_info)
        self.assertEqual(self.trips, [])

    def assert_trip(self, emitted: list[tuple[str, dict[str, Any]]], expected: guardrail.Trip) -> None:
        self.assertEqual([kind for kind, _ in emitted if kind in {"answer", "failure"}], ["answer"])
        text = "".join(body["text"] for kind, body in emitted if kind == "text")
        kind, answer = emitted[-1]
        self.assertEqual(kind, "answer")
        self.assertEqual((answer["end"], answer["family"], answer["text"]), ("tripped", expected.family, text))
        self.assertEqual(self.trips, [guardrail.TripRow(LEGAL_BRANCH, expected.family, expected.pattern_id, guardrail.EVAL_SOURCE, CHAT_ID)])

    def test_trip_closes_the_engine_before_sending_the_refusal_and_records_one_row(self) -> None:
        stream = FixtureStream(sse(payload({"content": OPEN_TEXT})) + sse(payload({"content": DEADLINE_TEXT}, finish_reason="stop")))
        self.completions = [completion(stream)]
        sent: list[tuple[Message, bool]] = []
        response = self.request(payload=FULL_TURN, on_send=lambda message: sent.append((message, stream.closed)))
        emitted = self.read_events(response)
        trip = guardrail.judge_rendered((DEADLINE_TEXT,), DEADLINE_TEXT)
        assert isinstance(trip, guardrail.Trip)
        self.assert_trip(emitted, trip)
        refusal = guardrail.REFUSAL_SEPARATOR + guardrail.refusal_for(trip.family)
        self.assertEqual(emitted[-2], ("text", {"text": refusal}))
        self.assertEqual(self.turn_logs, [])
        self.assertNotIn(DEADLINE_TEXT, response.text)

        start, *bodies, final = sent
        self.assertEqual(start[0]["type"], "http.response.start")
        self.assertEqual(final[0], {"type": "http.response.body", "body": b"", "more_body": False})
        self.assertTrue(all(message["type"] == "http.response.body" and message["more_body"] for message, _ in bodies))
        self.assertEqual(len(bodies), len(emitted))
        self.assertTrue(bodies[-2][1] and bodies[-1][1])
        self.assertTrue(any(kind == "text" and not closed for (kind, _), (_, closed) in zip(emitted, bodies, strict=True)))

    def test_length_without_text_retries_once_with_thinking_off_and_a_fresh_window(self) -> None:
        first = FixtureStream((FIXTURES / "generator-truncated.sse").read_bytes())
        second = FixtureStream((FIXTURES / "generator-thinking-off.sse").read_bytes())
        self.completions = [completion(first), completion(second)]
        retry_closed: list[bool] = []

        def observe(message: Message) -> None:
            if b'"stage":"generate","attempt":2' in message.get("body", b""):
                retry_closed.append(first.closed)

        emitted = self.read_events(self.request(payload=FULL_TURN, on_send=observe))
        self.assertEqual(retry_closed, [True])
        self.assertTrue(second.closed)
        self.assertEqual(self.engine_paths(), [MODELS_ROUTE, COMPLETIONS_ROUTE, COMPLETIONS_ROUTE])
        first_request, retry_request = (json.loads(request.content) for request in self.engine_requests[1:])
        self.assertNotIn("chat_template_kwargs", first_request)
        self.assertEqual(retry_request, {**first_request, "chat_template_kwargs": {"enable_thinking": False}})
        phases = [body for kind, body in emitted if kind == "phase" and body["stage"] == "generate"]
        self.assertEqual([body["attempt"] for body in phases], [1, 2])
        retry_index = emitted.index(("phase", phases[-1]))
        self.assertNotIn("text", [kind for kind, _ in emitted[:retry_index]])
        expected, finish = recorded_answer(EventReassembler().feed(second.body))
        self.assertTrue(0 < len(expected) < guardrail.LAG_CHARS)
        self.assertEqual(finish, "stop")
        self.assertEqual("".join(body["text"] for kind, body in emitted if kind == "text"), expected)
        self.assertEqual([kind for kind, _ in emitted if kind in {"answer", "failure"}], ["answer"])
        self.assertEqual((emitted[-1][1]["text"], emitted[-1][1]["end"]), (expected, "complete"))
        self.assertEqual(self.turn_logs, [])
        self.assertEqual(self.trips, [])

    def test_empty_stop_fails_on_either_attempt(self) -> None:
        truncated = (FIXTURES / "generator-truncated.sse").read_bytes()
        empty_stop = sse(payload({}, finish_reason="stop"))
        for attempts, bodies in ((1, (empty_stop,)), (2, (truncated, empty_stop))):
            with self.subTest(attempts=attempts):
                streams = [FixtureStream(body) for body in bodies]
                self.completions = [completion(stream) for stream in streams]
                emitted = self.read_events(self.request(payload=FULL_TURN))
                self.assert_stream_failure(emitted, "generation_empty")
                self.assertNotIn("text", [kind for kind, _ in emitted])
                self.assertEqual(self.engine_paths(), [MODELS_ROUTE, *([COMPLETIONS_ROUTE] * attempts)])
                self.assertTrue(all(stream.closed for stream in streams))

    def test_engine_error_and_transport_error_settle_the_tail_or_emit_its_trip(self) -> None:
        trip = guardrail.judge_rendered((DEADLINE_TEXT,), DEADLINE_TEXT)
        assert isinstance(trip, guardrail.Trip)
        for engine_error in (True, False):
            for tail in ("A safe fictional explanation.", DEADLINE_TEXT):
                with self.subTest(engine_error=engine_error, tripping_tail=tail == DEADLINE_TEXT):
                    body = sse(payload({"content": OPEN_TEXT})) + sse(payload({"content": tail}))
                    if engine_error:
                        stream: FixtureStream | GatedStream = FixtureStream(body + sse(ENGINE_ERROR))
                    else:
                        stream = GatedStream((body,), failure=httpx.ReadError("Fictitious lost connection"))
                        stream.release(0)
                    self.completions = [completion(stream)]
                    response = self.request(payload=FULL_TURN)
                    emitted = self.read_events(response)
                    self.assertTrue(stream.closed)
                    text = "".join(data["text"] for kind, data in emitted if kind == "text")
                    self.assertNotIn("Fictitious generator diagnostics", response.text)
                    if tail == DEADLINE_TEXT:
                        self.assert_trip(emitted, trip)
                        self.assertNotIn(DEADLINE_TEXT, text)
                        self.assertTrue(text.endswith(guardrail.REFUSAL_SEPARATOR + guardrail.refusal_for(trip.family)))
                        self.assertEqual(self.turn_logs, [])
                    else:
                        self.assert_stream_failure(emitted, "generator_unavailable")
                        self.assertEqual(text, OPEN_TEXT + tail)
                        self.assertEqual(emitted[-2][0], "text")

    def test_completion_failures_before_stream_bytes(self) -> None:
        unread = FixtureStream(b"Fictitious upstream error body")
        broken = FixtureStream(b"", failure=httpx.ReadError("Fictitious read error"))
        cases = (
            ("connect", httpx.ConnectError("Fictitious connection error"), "generator_unavailable"),
            ("HTTP status", completion(unread, status=503), "generator_unavailable"),
            ("content type", completion(FixtureStream(b"{}"), content_type="application/json"), "generator_reply_invalid"),
            ("first read", completion(broken), "generator_unavailable"),
        )
        for label, reply, state in cases:
            with self.subTest(label=label):
                self.completions = [reply]
                emitted = self.read_events(self.request(payload=FULL_TURN))
                self.assert_stream_failure(emitted, state)
                self.assertNotIn("text", [kind for kind, _ in emitted])
                if isinstance(reply, httpx.Response):
                    self.assertTrue(reply.is_closed)

    def test_unfinished_stream_settles_text_before_invalid_reply_failure(self) -> None:
        text = "A short fictitious answer."
        for ending in (b"", b'data: {"choices":', sse(DONE_EVENT)):
            with self.subTest(ending=ending):
                stream = FixtureStream(sse(payload({"content": text})) + ending)
                self.completions = [completion(stream)]
                emitted = self.read_events(self.request(payload=FULL_TURN))
                self.assert_stream_failure(emitted, "generator_reply_invalid")
                self.assertEqual(emitted[-2], ("text", {"text": text}))
                self.assertTrue(stream.closed)

    def test_unreadable_payload_and_reassembler_failures_trip_and_log_the_class_alone(self) -> None:
        cases = (
            (sse(payload({"content": 7})), "TypeError"),
            (b"data: \xff\n\n", "UnicodeDecodeError"),
            (b"x" * (SSE_EVENT_BUFFER_LIMIT_BYTES + 1), "SSEEventTooLargeError"),
        )
        expected = guardrail.Trip(guardrail.DEADLINE_FAMILY.name, guardrail.ERROR_PATTERN_ID)
        for chunk, error in cases:
            with self.subTest(error=error):
                stream = FixtureStream(chunk)
                self.completions = [completion(stream)]
                emitted = self.read_events(self.request(payload=FULL_TURN))
                self.assert_trip(emitted, expected)
                line, = self.turn_logs
                self.assertEqual(line.getMessage(), f"judged stream failed: {error}")
                self.assertIsNone(line.exc_info)
                self.assertTrue(stream.closed)

    def test_unexpected_exception_after_start_sends_internal_error(self) -> None:
        self.completions = [RuntimeError(MESSAGE)]
        emitted = self.read_events(self.request(payload=FULL_TURN))
        self.assert_stream_failure(emitted, "internal_error", word=None)
        self.assertIn("exception=RuntimeError", self.turn_logs[0].getMessage())

    def test_hang_up_cancels_the_stream_and_waits_for_shielded_close(self) -> None:
        async def run(spec_version: str) -> None:
            stream = WaitingStream(sse(payload({"content": OPEN_TEXT})))
            self.completions = [completion(stream)]
            async with ASGISession(
                self.app(self.settings), "POST", "/turn", json.dumps(FULL_TURN).encode(),
                self.headers(self.settings), spec_version,
            ) as session:
                start = await session.next_message()
                self.assertEqual(start["type"], "http.response.start")
                while True:
                    message = await session.next_message()
                    self.assertTrue(message.get("more_body"))
                    if message["body"].startswith(b"event: text\n"):
                        break
                await asyncio.wait_for(stream.waiting.wait(), timeout=1)
                session.disconnect()
                await session.wait_for_application()
                self.assertTrue(stream.closed)
                self.assertTrue(session.outgoing.empty())

        for spec_version in ("2.0", "2.4"):
            with self.subTest(spec_version=spec_version):
                asyncio.run(run(spec_version))
                self.assertEqual(self.trips, [])
