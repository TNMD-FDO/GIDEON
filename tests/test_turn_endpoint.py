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
from uuid import UUID

import anyio
import httpx
from psycopg import OperationalError
from psycopg.types.json import Jsonb
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send
from test_api_completions import ASGISession, FixtureStream, GatedStream
from test_lock_coupling import MODELS_LOCK_PATH
from test_turn_answer import ENGINE_ERROR, payload, recorded_answer, with_usage
from test_turn_frontend import KB_ID, KNOWLEDGE_ROUTE, SESSION_ROUTE, USER_ID
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
from gideon.turn import (
    auditlog,
    denial,
    endpoint,
    lexical,
    measures,
    notice,
    objectleg,
    objects,
    provenance,
    turnaudit,
    turnlog,
)
from gideon.turn.answer import LEGAL_BRANCH
from gideon.turn.caller import Caller, Floor
from gideon.turn.config import (
    RETRIEVAL_CONFIG,
    OverrideRefusal,
    config_hash,
    effective_config,
)
from gideon.turn.generation import TURN_COLLECTION, read_generation
from gideon.turn.generator import GENERATOR_SAMPLING
from gideon.turn.objects import CaseExtractor
from gideon.turn.prompt import THIN_SUPPORT, TRACER_SYSTEM, messages
from gideon.turn.research import RESEARCH_CONFIG

MESSAGE = "Fictitious turn sentinel Cobalt Widget Q7"
FULL_TURN: dict[str, object] = {"message": MESSAGE}
RETRIEVE: dict[str, object] = {"message": MESSAGE, "stop_after": "retrieve"}
MODELS_ROUTE = "/v1/models"
COMPLETIONS_ROUTE = "/v1/chat/completions"
CHAT_ID = "fixture-research-chat"
DEADLINE_TEXT = "The deadline is April 7, 2034."
NEUTRAL_SENTENCE = "The fictitious harbor stayed open. "
OPEN_TEXT = NEUTRAL_SENTENCE * (guardrail.LAG_CHARS // len(NEUTRAL_SENTENCE) + 1)


async def scrape_metrics(client: httpx.AsyncClient) -> dict[str, float]:
    response = await client.get("/metrics")
    response.raise_for_status()
    samples: dict[str, float] = {}
    for line in response.text.splitlines():
        if line and not line.startswith("#"):
            series, value = line.rsplit(maxsplit=1)
            samples[series] = float(value)
    return samples


def sse(data: str) -> bytes:
    return f"data: {data}\n\n".encode()


def with_finishing_usage(capture: bytes, usage: dict[str, object]) -> bytes:
    *opening, finishing, done = EventReassembler().feed(capture)
    assert done == DONE_EVENT and json.loads(finishing)["choices"][0]["finish_reason"] is not None
    return b"".join(sse(item) for item in (*opening, with_usage(finishing, usage), done))


def completion(stream: httpx.AsyncByteStream, *, status: int = 200, content_type: str = "text/event-stream") -> httpx.Response:
    return httpx.Response(status, headers={"content-type": content_type}, stream=stream)


def session_reply(token: str) -> httpx.Response:
    return httpx.Response(
        200, json={"id": USER_ID, "role": "user", "token": token},
        headers={"Set-Cookie": f"token={token}; Path=/; HttpOnly; SameSite=Strict"},
    )


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


class TurnEndpointFixture(unittest.TestCase):
    def setUp(self) -> None:
        listing = turn_fixture("embed-listing.json")
        embedding = turn_fixture("embedding.json")
        dense = turn_fixture("dense-grouped.json")
        controlling = turn_fixture("controlling-grouped.json")
        lexical = turn_fixture("lexical-collapsed.json")
        self.lexical_hits = lexical["hits"]["hits"]
        self.groups = dense["result"]["groups"]
        self.controlling_groups = controlling["result"]["groups"]
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
            "controlling": controlling,
            self.lexical_path: lexical,
        }
        self.requests: dict[str, httpx.Request] = {}
        self.engine_requests: list[httpx.Request] = []
        self.frontend_requests: list[httpx.Request] = []
        self.frontend_replies: dict[str, httpx.Response | Exception] = {}
        self.rerank_transport: httpx.AsyncBaseTransport | None = None
        self.case_extractor: CaseExtractor | None = lambda _: ()
        self.trips: list[guardrail.TripRow] = []
        self.enterContext(patch.object(guardrail.writer, "dispatch_trip_row", self.trips.append))
        self.dispatch = self.enterContext(patch.object(guardrail, "dispatch_event_row"))
        self.live_rows: tuple[LiveRow, ...] = ((self.generation, TURN_COLLECTION, embedding_space, [self.lockfile]),)
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ("record", "dense", "embedder", "lexical", "audit"):
            (root / name).write_text(f"fixture-{name}-secret\n", encoding="utf-8")
        self.enterContext(patch.object(guardrail.writer, "TRIP_PASSWORD_PATH", str(root / "audit")))
        self.audit_connection = Mock(execute=AsyncMock(), close=AsyncMock())
        self.audit_connect = AsyncMock(return_value=self.audit_connection)
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
            courts_scotus=("scotus",), courts_circuit=("ca11",), courts_districts=("alnd",), courts_states=("fx-sup-al",),
            courts_file=FIXTURES / "courts.json",
        )

    def store_key(self, request: httpx.Request) -> str:
        path = request.url.path
        # The mock keeps the dense limit fixed and allows the controlling reservation to vary.
        if (
            path in (self.dense_path, self.dense_path.removesuffix("/groups"))
            and json.loads(request.content)["limit"] != RETRIEVAL_CONFIG["leg_limit"]
        ):
            return "controlling"
        return path

    def upstream(self, request: httpx.Request) -> httpx.Response:
        key = self.store_key(request)
        self.assertNotIn(key, self.requests)
        self.requests[key] = request
        if key == self.mget_path and key not in self.upstream_replies:
            docs = [
                {**self.passage_doc, "_index": self.generation, "_id": item["_id"]}
                for item in json.loads(request.content)["docs"]
            ]
            return httpx.Response(200, json={"docs": list(reversed(docs))})
        reply = self.upstream_replies[key]
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

    def frontend_upstream(self, request: httpx.Request) -> httpx.Response:
        self.frontend_requests.append(request)
        self.assertEqual(request.method, "GET")
        reply = self.frontend_replies.get(request.url.path)
        if reply is None:
            self.fail("Unexpected frontend request.")
        if isinstance(reply, Exception):
            raise reply
        return reply

    def app(self, settings: Settings, rows: Sequence[LiveRow] | None = None) -> Starlette:
        self.requests.clear()
        self.engine_requests.clear()
        self.frontend_requests.clear()
        self.trips.clear()
        self.dispatch.reset_mock()
        self.audit_connection.reset_mock()
        self.audit_connect.reset_mock()
        self.clock = count()
        self.connection, self.cursor = record_with_rows(self.live_rows if rows is None else rows)
        self.connect = AsyncMock(return_value=self.connection)
        return create_app(
            settings, transport=httpx.MockTransport(self.engine_upstream), embed_transport=httpx.MockTransport(self.upstream),
            qdrant_transport=httpx.MockTransport(self.upstream), opensearch_transport=httpx.MockTransport(self.upstream),
            rerank_transport=self.rerank_transport, frontend_transport=httpx.MockTransport(self.frontend_upstream),
            record_connect=self.connect, audit_connect=self.audit_connect,
            case_extractor=self.case_extractor, turn_clock=self.clock.__next__,
        )

    def headers(self, settings: Settings, *, eval_caller: bool = True) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.api_key}",
            settings.source_header: settings.eval_identity if eval_caller else "reader@example.invalid",
            settings.chat_header: CHAT_ID,
        }

    def request(
        self, *, settings: Settings | None = None, rows: Sequence[LiveRow] | None = None,
        payload: dict[str, object] | bytes | None = None, eval_caller: bool = True, chat_id: str = CHAT_ID,
        on_send: Callable[[Message], None] | None = None, private_values: tuple[str, ...] = (), scrape: bool = False,
    ) -> httpx.Response:
        configuration = self.settings if settings is None else settings
        app = self.app(configuration, rows)

        async def observe(scope: Scope, receive: Receive, send: Send) -> None:
            async def send_observed(message: Message) -> None:
                if on_send is not None and scope["path"] == "/turn":
                    on_send(message)
                await send(message)

            await app(scope, receive, send_observed)

        async def run() -> httpx.Response:
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=observe), base_url="http://fixture-api",
            ) as client:
                headers = self.headers(configuration, eval_caller=eval_caller)
                headers[configuration.chat_header] = chat_id
                if scrape:
                    self.metrics_before = await scrape_metrics(client)
                if isinstance(payload, bytes):
                    response = await client.post("/turn", content=payload, headers=headers)
                else:
                    response = await client.post("/turn", json=RETRIEVE if payload is None else payload, headers=headers)
                if scrape:
                    self.metrics_after = await scrape_metrics(client)
                clients: endpoint.TurnClients = app.state.turn_clients
                self.frontend_cookies = tuple(clients.frontend.cookies.jar)
                return response

        with self.assertLogs(level="DEBUG") as captured:
            response = asyncio.run(run())
        self.turn_logs = [record for record in captured.records if record.name == "gideon.turn"]
        for value in (MESSAGE, *private_values):
            self.assertNotIn(value, response.text)
            for record in captured.records:
                self.assertNotIn(value, record.getMessage())
                self.assertNotIn(value, repr(vars(record)))
            for dispatched in self.dispatch.call_args_list:
                self.assertNotIn(value, repr(dispatched.args))
        return response

    def assert_refused(
        self, response: httpx.Response, status: int, state: str, word: str | None = None, *, pinned: bool | None = False,
        incident_error: str | None = None,
    ) -> None:
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.headers["content-type"], "application/json")
        body = response.json()
        self.assertEqual(set(body), {"error"})
        self.assertEqual((body["error"]["code"], body["error"]["param"]), (state, word))
        if incident_error is None:
            line, = self.turn_logs
        else:
            line, error = self.turn_logs
            self.assertEqual(line.levelno, logging.WARNING)
            self.assertEqual((error.levelno, error.getMessage()), (logging.ERROR, incident_error))
            self.assertIsNone(error.exc_info)
        self.assertIn(f"state={state}", line.getMessage())
        self.assertIn(f"word={word or '-'}", line.getMessage())
        generations = ("-", self.generation) if pinned is None else (self.generation if pinned else "-",)
        self.assertIn(line.getMessage().partition(" generation=")[2].partition(" ")[0], generations)
        self.assertIsNone(line.exc_info)

    def audit_row(self) -> dict[str, Any]:
        self.audit_connect.assert_awaited_once()
        self.audit_connection.close.assert_awaited_once()
        partition, insert = self.audit_connection.execute.await_args_list
        query, values = insert.args
        self.assertTrue(query.startswith(f"INSERT INTO {auditlog.AUDIT_LOG_TABLE} "))
        row = dict(zip(auditlog.AUDIT_LOG_COLUMNS, values, strict=True))
        self.assertEqual(partition.args, (f"SELECT {auditlog.AUDIT_LOG_PARTITION_FUNCTION}(%s)", (row["at"],)))
        self.assertIsInstance(row["detail"], Jsonb)
        row["detail"] = row["detail"].obj
        self.assertNotIn(MESSAGE, repr(row))
        return row

    def turn_row(self) -> dict[str, Any]:
        self.dispatch.assert_called_once()
        self.assertNotIn(MESSAGE, repr(self.dispatch.call_args.args))
        _, _, columns, values = self.dispatch.call_args.args
        return dict(zip(columns, values, strict=True))

    def assert_research_audit(self, turn: dict[str, Any], *, sent_count: int = 0) -> dict[str, Any]:
        row = self.audit_row()
        self.assertEqual((row["kind"], row["chat_id"], row["release"]), (
            turnaudit.TURN_KIND, turn["chat_id"], turn["product_version"],
        ))
        evidence = json.loads(turn["evidence"]) if turn["evidence"] is not None else []
        self.assertGreaterEqual(len(evidence), sent_count)
        self.assertEqual(row["detail"], {
            "result": "allowed",
            "claim": turn["source"],
            "branch": LEGAL_BRANCH,
            "route": "retrieve" if turn["stop_after"] == "retrieve" else "answer",
            "outcome": turn["outcome"],
            "failure_state": turn["failure_state"],
            "message_id": turn["message_id"],
            "plan_mode": turn["plan_mode"],
            "corpus_lockfiles": json.loads(turn["corpus_lockfiles"]) if turn["corpus_lockfiles"] is not None else None,
            "returned": [{key: hit[key] for key in ("chunk_id", "doc_id")} for hit in evidence[:sent_count]],
            "support_level": turn["support_level"],
            "gate_counts": None,
            "latency_ms": json.loads(turn["timings_ms"])["total"],
        })
        return row

    def assert_failed_row(self, state: str, word: str | None = None, *, sent_count: int = 0) -> dict[str, Any]:
        row = self.turn_row()
        self.assertEqual((row["outcome"], row["failure_state"], row["failure_word"]), ("failed", state, word))
        self.assert_research_audit(row, sent_count=sent_count)
        return row

    def retrieve_session(self) -> ASGISession:
        return ASGISession(
            self.app(self.settings), "POST", "/turn", json.dumps(RETRIEVE).encode(), self.headers(self.settings), "2.4",
        )

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


class TurnEndpoint(TurnEndpointFixture):
    def assert_turn_metrics(
        self, row: dict[str, Any], readings: Sequence[tuple[str, tuple[float, ...], float]],
    ) -> None:
        source = f'source="{row["source"]}"'
        outcome = row["outcome"]
        state = row["failure_state"] or ""
        expected = self.metrics_before.copy()
        expected[f'{measures.TURNS_METRIC}{{{source},outcome="{outcome}",state="{state}"}}'] += 1
        for metric, edges, milliseconds in readings:
            seconds = milliseconds / 1000
            expected[f"{metric}_count{{{source}}}"] += 1
            expected[f"{metric}_sum{{{source}}}"] += seconds
            expected[f'{metric}_bucket{{{source},le="+Inf"}}'] += 1
            for edge in edges:
                if seconds <= edge:
                    expected[f'{metric}_bucket{{{source},le="{edge}"}}'] += 1
        self.assertEqual(self.metrics_after, expected)

    def test_retrieve_measures_match_the_row_with_rerank_on_and_off(self) -> None:
        def score(request: httpx.Request) -> httpx.Response:
            documents = json.loads(request.content)["documents"]
            return httpx.Response(200, json={"results": [
                {"index": index, "relevance_score": 0.5} for index, _ in enumerate(documents)
            ]})

        self.rerank_transport = httpx.MockTransport(score)
        key_file = self.settings.embed_key_file.with_name("reranker")
        key_file.write_text("fictitious-turn-rerank-key", encoding="utf-8")
        settings = replace(self.settings, rerank_url="http://fixture-reranker", rerank_key_file=key_file)
        for stage in ("on", "off"):
            with self.subTest(stage=stage):
                payload = {**RETRIEVE, "overrides": {"rerank.stage2": stage}}
                response = self.request(settings=settings, payload=payload, scrape=True)
                self.assertEqual(response.status_code, 200, response.text)
                row = self.turn_row()
                self.assertEqual(row["outcome"], "reply")
                timings = json.loads(row["timings_ms"])
                self.assertEqual(timings, response.json()["timings_ms"])

                readings: list[tuple[str, tuple[float, ...], float]] = [
                    (measures.RETRIEVAL_METRIC, measures.RETRIEVAL_BUCKETS, timings["retrieval"]),
                ]
                if stage == "on":
                    readings.append((measures.THROUGH_RERANK_METRIC, measures.THROUGH_RERANK_BUCKETS, timings["total"]))
                self.assert_turn_metrics(row, readings)

    def test_eval_retrieve_returns_the_fused_pool_and_provenance(self) -> None:
        reranker = Mock()
        self.rerank_transport = httpx.MockTransport(reranker)
        response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.requests.keys(), self.upstream_replies.keys() | {self.mget_path})
        reranker.assert_not_called()
        self.assertEqual(self.engine_requests, [])
        self.assertEqual(self.requests["/v1/models"].method, "GET")
        self.assertEqual(self.requests["/v1/embeddings"].method, "POST")
        self.assertEqual(self.requests[self.dense_path].method, "POST")
        self.assertEqual(self.requests[self.lexical_path].method, "POST")
        controlling_request = self.requests["controlling"]
        self.assertEqual((controlling_request.method, controlling_request.url.path), ("POST", self.dense_path))
        self.assertEqual(controlling_request.headers["api-key"], self.requests[self.dense_path].headers["api-key"])
        courts = sorted({
            *self.settings.courts_scotus, *self.settings.courts_circuit,
            *self.settings.courts_districts, *self.settings.courts_states,
        })
        dense_body = json.loads(self.requests[self.dense_path].content)
        self.assertNotIn("filter", dense_body)
        self.assertEqual(json.loads(controlling_request.content), {
            **dense_body, "limit": RETRIEVAL_CONFIG["reservations.controlling"],
            "filter": {"must": [{"key": "court", "match": {"any": courts}}]},
        })
        self.connect.assert_awaited_once()
        self.cursor.execute.assert_awaited_once()
        self.connection.close.assert_awaited_once()

        body = response.json()
        self.assertEqual(body["stage"], "retrieve")
        self.assertEqual(body["timings_ms"]["objects"], 0)
        self.assertEqual(body["timings_ms"]["passages"], 1000.0)
        for stage in ("rerank_passages", "rerank_queue", "rerank"):
            self.assertEqual(body["timings_ms"][stage], 0.0)
        self.assertGreater(body["timings_ms"]["extraction"], 0)
        self.assertGreater(body["timings_ms"]["controlling"], 0)
        self.assertEqual(body["generation"], {
            "gen_id": self.generation, "collection": TURN_COLLECTION, "lockfiles": [self.lockfile],
            "embedding_space": self.settings.embed_space,
        })
        config = effective_config(reranker=False)
        assert not isinstance(config, OverrideRefusal)
        self.assertEqual(body["retrieval_config_hash"], config_hash(config))
        row = self.turn_row()
        self.assertEqual(row["outcome"], "reply")
        audit = self.assert_research_audit(row, sent_count=len(body["final_set"]))
        self.assertIsNone(audit["user_id"])
        self.assertEqual(audit["kb_ids"], [])
        self.assertEqual(row["retrieval_config_hash"], body["retrieval_config_hash"])
        self.assertEqual(json.loads(row["timings_ms"]), body["timings_ms"])
        self.assertEqual(json.loads(row["pool"]), body["pool"])
        provenance_without_objects = dict(body["provenance"])
        objects = provenance_without_objects.pop("out_of_corpus_objects")
        self.assertEqual(json.loads(row["provenance"]), provenance_without_objects)
        self.assertEqual(json.loads(row["out_of_corpus_objects"]), objects)
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
        self.assertEqual({entry["chunk_id"]: entry["authority_tier"] for entry in pool}, {
            shared: "scotus", duplicate: "circuit_published", boilerplate: "circuit_unpublished",
            lexical_hits[2]["_id"]: "persuasive",
        })
        seat_ranks = {
            group["hits"][0]["payload"]["chunk_id"]: rank
            for rank, group in enumerate(self.controlling_groups, start=1)
        }
        self.assertEqual({entry["chunk_id"]: entry["controlling_rank"] for entry in pool if entry["reserved"]}, seat_ranks)
        for entry in pool:
            rank = seat_ranks.get(entry["chunk_id"])
            self.assertEqual((entry["reserved"], entry["controlling_rank"]), ("controlling" if rank is not None else None, rank))
        self.assertTrue(all(entry["pinned"] is False and entry["object"] is None for entry in pool))
        self.assertTrue(all(entry["stage2_score"] is None and entry["rank"] == entry["pool_rank"] for entry in pool))
        self.assertEqual(len({entry["dupe_cluster"] for entry in pool}), len(pool))
        source = self.passage_doc["_source"]
        expected = [entry for entry in pool if entry["chunk_id"] in (shared, duplicate, boilerplate)]
        self.assertEqual(body["final_set"], [
            {
                **{key: entry[key] for key in ("chunk_id", "pool_rank", "rank", "stage2_score", "authority_tier", "object")},
                "number": number, "seat": "controlling" if entry["chunk_id"] == boilerplate else "score",
                "doc_id": source["doc_id"], "tokens": source["token_count"],
            }
            for number, entry in enumerate(expected, start=1)
        ])
        self.assertEqual(json.loads(row["evidence"]), body["final_set"])
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
                    "controlling": {
                        "store": "qdrant", "limit": RETRIEVAL_CONFIG["reservations.controlling"],
                        "returned": len(self.controlling_groups), "courts": courts,
                    },
                    "objects": {
                        "store": "opensearch", "limit": RETRIEVAL_CONFIG["reservations.exact_object"],
                        "returned": 0, "searches": 0,
                    },
                },
                "tools": [],
                "fusion": {
                    "union": len(groups) + len(lexical_hits) - 1, "after_collapse": len(pool),
                    "weighted": 1, "cut": 0, "pinned": 0, "pinned_cut": 0,
                    "reserved": len(seat_ranks), "reserved_only": 0, "reserved_promoted": 0,
                    "reserved_inherited": 0, "reserved_cut": 0,
                },
                "rerank": {
                    "stage1": RETRIEVAL_CONFIG["rerank.stage1"], "stage2": "off",
                    "depth": RETRIEVAL_CONFIG["stage2_depth"], "scored": 0, "pinned_scored": 0, "reserved_scored": 0,
                },
                "final_set": {
                    "k": config["final_k"], "mode": config["hierarchy.mode"], "ordered_by": "rank",
                    "reserved_controlling": config["final_reserved_controlling"],
                    "per_document_cap": config["per_document_cap"], "budget_tokens": config["context_budget_tokens"],
                    "candidates": len(pool), "size": len(expected), "pinned": 0, "controlling_seated": 1,
                    "by_tier": {entry["authority_tier"]: 1 for entry in expected},
                    "tokens": len(expected) * source["token_count"], "document_capped": 0, "exempt_documents": 0,
                    "budget_dropped": 0, "objects_unseated": [],
                    "floor": config["abstention.floor"], "floor_dropped": 0, "best_under_floor": None,
                },
                "support": {
                    "level": "ok", "count": len(expected), "thin_below": config["abstention.thin_below"],
                    "floor": config["abstention.floor"], "dropped": 0, "best_under_floor": None,
                    "question": {"kind": "law", "decided_by": "default"},
                },
            },
        )

    def test_service_without_a_reranker_refuses_on_override_before_retrieval(self) -> None:
        reranker = Mock()
        self.rerank_transport = httpx.MockTransport(reranker)
        response = self.request(payload={**RETRIEVE, "overrides": {"rerank.stage2": "on"}})

        self.assert_refused(response, 400, "override_invalid", "rerank.stage2")
        self.assertEqual(self.requests, {})
        reranker.assert_not_called()
        self.connect.assert_not_awaited()
        self.dispatch.assert_not_called()

    def test_eval_retrieve_filters_each_leg_only_when_the_user_names_a_scope(self) -> None:
        unscoped_message = "Fictitious appeal under 18 U.S.C. § 9999"
        exact, = extract(unscoped_message)
        empty = turn_fixture("objects-msearch-empty.json")
        self.upstream_replies[self.msearch_path] = {**empty, "responses": empty["responses"][:1]}
        baseline = self.request(payload={**RETRIEVE, "message": unscoped_message})
        self.assertEqual(baseline.status_code, 200, baseline.text)
        expected = baseline.json()
        self.assertIsNone(expected["provenance"]["filters"]["user_stated_scope"])

        home_courts = sorted({
            *self.settings.courts_scotus, *self.settings.courts_circuit,
            *self.settings.courts_districts, *self.settings.courts_states,
        })
        lexical_body = {
            "query": {"match": {"text": unscoped_message}}, "size": RETRIEVAL_CONFIG["leg_limit"],
            "_source": list(self.lexical_hits[0]["_source"]), "collapse": {"field": "dupe_cluster"},
        }
        object_clause = {"term": {"cites": {"value": exact.key, "case_insensitive": True}}}
        object_body = {
            **lexical_body, "size": RETRIEVAL_CONFIG["reservations.exact_object"],
            "query": {"bool": {"filter": [object_clause], "should": [lexical_body["query"]]}},
            "sort": [{"_score": "desc"}, {"doc_id": "asc"}, {"ordinal": "asc"}],
        }
        dense_body = {
            "query": turn_fixture("embedding.json")["data"][0]["embedding"], "limit": RETRIEVAL_CONFIG["leg_limit"],
            "with_payload": list(self.groups[0]["hits"][0]["payload"]), "with_vector": False,
            "group_by": "dupe_cluster", "group_size": 1,
        }
        controlling_body = {
            **dense_body, "limit": RETRIEVAL_CONFIG["reservations.controlling"],
            "filter": {"must": [{"key": "court", "match": {"any": home_courts}}]},
        }
        for path, body in ((self.lexical_path, lexical_body), (self.dense_path, dense_body), ("controlling", controlling_body)):
            self.assertEqual(self.requests[path].content, json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
        self.assertEqual(
            self.requests[self.msearch_path].content, b"{}\n" + json.dumps(object_body, separators=(",", ":")).encode() + b"\n",
        )

        home_name, absent_name = "Eleventh Circuit", "First Circuit"
        message = f"{unscoped_message} in the {home_name} or the {absent_name}?"
        home, = self.settings.courts_circuit
        absent = self.lexical_hits[2]["_source"]["court"]
        home_start, absent_start = message.index(home_name), message.index(absent_name)
        response = self.request(payload={**RETRIEVE, "message": message}, private_values=(home_name, absent_name))
        self.assertEqual(response.status_code, 200, response.text)
        courts = sorted((home, absent, *self.settings.courts_districts, *self.settings.courts_scotus))
        intersection = sorted(set(home_courts).intersection(courts))
        match = {"match": {"text": message}}
        terms = {"terms": {"court": courts}}
        self.assertEqual(json.loads(self.requests[self.lexical_path].content), {
            **lexical_body, "query": match, "post_filter": terms,
        })
        self.assertEqual(list(map(json.loads, self.requests[self.msearch_path].content.splitlines())), [{}, {
            **object_body, "query": {"bool": {"filter": [object_clause, terms], "should": [match]}},
        }])
        self.assertEqual(json.loads(self.requests[self.dense_path].content), {
            **dense_body, "filter": {"must": [{"key": "court", "match": {"any": courts}}]},
        })
        self.assertEqual(json.loads(self.requests["controlling"].content), {
            **controlling_body, "filter": {"must": [{"key": "court", "match": {"any": intersection}}]},
        })

        expected["provenance"]["filters"]["user_stated_scope"] = {
            "named": [
                {"court": home, "start": home_start, "end": home_start + len(home_name)},
                {"court": absent, "start": absent_start, "end": absent_start + len(absent_name)},
            ],
            "courts": courts,
            "out_of_corpus": [absent],
        }
        expected["provenance"]["legs"]["controlling"]["courts"] = intersection
        missing = expected["provenance"]["out_of_corpus_objects"]
        missing[0]["resolved"] = "unresolved_in_scope"
        missing.append({"type": "court", "resolved": absent})
        self.assertEqual(response.json(), expected)

    def test_absent_circuit_is_filtered_and_projected_into_the_turn_row(self) -> None:
        court_name = "First Circuit"
        message = f"How would the {court_name} hear a fictitious appeal?"
        court_file = turn_fixture("courts.json")
        absent, = (
            identifier for identifier, court in court_file["courts"].items()
            if court["name"] == f"Court of Appeals for the {court_name}"
        )
        self.assertNotIn(absent, court_file["lockfiles"][self.lockfile])
        supreme = list(self.settings.courts_scotus)
        for path, fixture in (
            (self.dense_path, "dense-grouped.json"), ("controlling", "controlling-grouped.json"),
        ):
            reply = turn_fixture(fixture)
            reply["result"]["groups"] = [
                group for group in reply["result"]["groups"] if group["hits"][0]["payload"]["court"] in supreme
            ]
            self.upstream_replies[path] = reply
        lexical_reply = turn_fixture("lexical-collapsed.json")
        lexical_reply["hits"]["hits"] = [row for row in self.lexical_hits if row["_source"]["court"] in supreme]
        self.upstream_replies[self.lexical_path] = lexical_reply

        with patch.object(provenance, "retrieval_provenance", wraps=provenance.retrieval_provenance) as build:
            response = self.request(payload={**RETRIEVE, "message": message}, private_values=(court_name, message))
        self.assertEqual(response.status_code, 200, response.text)
        build.assert_called_once()
        self.assertNotIn(court_name, repr(build.call_args.kwargs["stated_scope"]))

        start = message.index(court_name)
        reply_provenance = response.json()["provenance"]
        self.assertEqual(reply_provenance["filters"]["user_stated_scope"], {
            "named": [{"court": absent, "start": start, "end": start + len(court_name)}],
            "courts": sorted((absent, *supreme)),
            "out_of_corpus": [absent],
        })
        self.assertEqual(reply_provenance["legs"]["controlling"]["courts"], supreme)
        self.assertEqual(json.loads(self.requests["controlling"].content)["filter"], {
            "must": [{"key": "court", "match": {"any": supreme}}],
        })
        row = self.turn_row()
        missing = [{"type": "court", "resolved": absent}]
        self.assertEqual(reply_provenance.pop("out_of_corpus_objects"), missing)
        self.assertEqual(json.loads(row["provenance"]), reply_provenance)
        self.assertEqual(json.loads(row["out_of_corpus_objects"]), missing)

    def test_empty_scope_intersection_skips_the_controlling_request(self) -> None:
        settings = replace(self.settings, courts_scotus=(), courts_circuit=(), courts_districts=())
        response = self.request(settings=settings, payload={
            **RETRIEVE, "message": "Fictitious appeal in the Eleventh Circuit or First Circuit?",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.requests.keys(), (self.upstream_replies.keys() - {"controlling"}) | {self.mget_path})
        reply = response.json()
        self.assertEqual(reply["provenance"]["legs"]["controlling"], {
            "store": "qdrant", "limit": RETRIEVAL_CONFIG["reservations.controlling"], "returned": 0, "courts": [],
        })
        self.assertEqual(reply["timings_ms"]["controlling"], 0)

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
        self.assertEqual(first["authority_tier"], "scotus")
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
        row = self.turn_row()
        self.assertEqual(json.loads(row["out_of_corpus_objects"]), [{"type": "statute", "resolved": "unresolved"}])
        self.assertEqual(json.loads(row["provenance"])["objects"], provenance["objects"])
        self.assertEqual(json.loads(row["pool"]), body["pool"])
        self.assertEqual(json.loads(row["timings_ms"]), body["timings_ms"])

        with patch.dict(self.upstream_replies, {self.msearch_path: httpx.ConnectError(message)}):
            refused = self.request(payload=payload, private_values=private_values)
        self.assert_refused(refused, 503, "store_unavailable", "objects", pinned=True)
        timings = json.loads(self.assert_failed_row("store_unavailable", "objects")["timings_ms"])
        self.assertTrue({"record", "extraction", "embedding"} <= timings.keys())

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

    def test_reporter_cite_with_only_citing_hits_distinguishes_a_scoped_miss(self) -> None:
        citation = "444 U. S. 987"  # Fictitious reporter cite.
        exact = ExactObject("case_cite", 0, len(citation), citation)
        self.case_extractor = Mock(return_value=((exact, None),))
        reply = turn_fixture("objects-msearch.json")
        empty_held = turn_fixture("objects-msearch-empty.json")["responses"][0]
        citing = reply["responses"][1]
        kept = [row for row in citing["hits"]["hits"] if row["_source"]["court"] in self.settings.courts_circuit]
        citing["hits"]["hits"] = kept
        citing["hits"]["total"]["value"] = len(kept)
        self.assertTrue(kept)
        self.upstream_replies[self.msearch_path] = {**reply, "responses": [empty_held, citing]}

        for suffix, resolved in (("", "unresolved"), (" under the Eleventh Circuit", "unresolved_in_scope")):
            with self.subTest(resolved=resolved):
                response = self.request(
                    payload={**RETRIEVE, "message": f"{citation} and a fictitious appeal{suffix}"},
                    private_values=(citation,),
                )
                self.assertEqual(response.status_code, 200, response.text)
                reply_body = response.json()
                (entry,) = reply_body["provenance"]["objects"]
                self.assertIs(entry["out_of_corpus"], True)
                self.assertEqual(entry["seats"], len(kept))
                self.assertEqual(reply_body["provenance"]["out_of_corpus_objects"], [
                    {"type": "case_cite", "index": 0, "resolved": resolved, "citing_hits": len(kept)},
                ])
                self.assertEqual([hit["chunk_id"] for hit in reply_body["pool"] if hit["pinned"]], [row["_id"] for row in kept])

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
        cap = 3
        reservation = len(self.controlling_groups)
        overrides = {"pool_cap": cap, "reservations.controlling": reservation}
        lexical = turn_fixture("lexical-collapsed.json")
        # Put the out-of-circuit hit first in the lexical leg so it takes the unmarked place.
        *home_hits, outsider = lexical["hits"]["hits"]
        lexical["hits"]["hits"] = [outsider, *home_hits]
        with patch.dict(self.upstream_replies, {self.lexical_path: lexical}):
            uncapped = self.request()
            self.assertEqual(uncapped.status_code, 200, uncapped.text)
            original = uncapped.json()
            response = self.request(payload={**RETRIEVE, "overrides": overrides})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertLess(cap, len(original["pool"]))
        seats = {
            group["hits"][0]["payload"]["chunk_id"]: rank
            for rank, group in enumerate(self.controlling_groups, start=1)
        }
        kept = {*seats, outsider["_id"]}
        self.assertFalse(seats.keys() <= {entry["chunk_id"] for entry in original["pool"][:cap]})
        self.assertEqual([entry["chunk_id"] for entry in body["pool"]], [
            entry["chunk_id"] for entry in original["pool"] if entry["chunk_id"] in kept
        ])
        self.assertEqual({
            entry["chunk_id"]: entry["controlling_rank"] for entry in body["pool"] if entry["reserved"] == "controlling"
        }, seats)
        (persuasive,) = [entry for entry in body["pool"] if entry["chunk_id"] == outsider["_id"]]
        self.assertEqual((persuasive["authority_tier"], persuasive["reserved"], persuasive["controlling_rank"]),
                         ("persuasive", None, None))
        self.assertEqual(json.loads(self.requests["controlling"].content)["limit"], reservation)
        self.assertEqual(body["provenance"]["legs"]["controlling"]["limit"], reservation)
        config = effective_config(overrides, reranker=False)
        assert not isinstance(config, OverrideRefusal)
        self.assertEqual(body["retrieval_config_hash"], config_hash(config))
        row = self.turn_row()
        self.assertEqual(json.loads(row["pool"]), body["pool"])
        self.assertEqual(row["retrieval_config_hash"], body["retrieval_config_hash"])
        self.assertNotEqual(row["retrieval_config_hash"], original["retrieval_config_hash"])
        self.assertEqual(json.loads(row["overrides"]), overrides)
        clusters = {group["id"] for group in self.groups} | {
            row["_source"]["dupe_cluster"] for row in self.lexical_hits
        }
        self.assertEqual(body["provenance"]["fusion"]["after_collapse"], len(clusters))
        self.assertEqual(body["provenance"]["fusion"]["cut"], len(clusters) - cap)
        self.assertEqual(body["provenance"]["fusion"]["reserved_cut"], 0)

    def test_both_vector_requests_follow_the_collapse_override(self) -> None:
        path = self.dense_path.removesuffix("/groups")
        replies = {
            key: {"status": "ok", "result": {"points": [group["hits"][0] for group in groups]}}
            for key, groups in ((path, self.groups), ("controlling", self.controlling_groups))
        }
        with patch.dict(self.upstream_replies, replies):
            response = self.request(payload={**RETRIEVE, "overrides": {"collapse": "off"}})
        self.assertEqual(response.status_code, 200, response.text)
        for key in (path, "controlling"):
            request = self.requests[key]
            self.assertEqual(request.url.path, path)
            self.assertTrue({"group_by", "group_size"}.isdisjoint(json.loads(request.content)))

    def test_space_mismatches_name_the_reason_without_a_dense_request(self) -> None:
        cases: tuple[tuple[str, Settings, dict[str, object]], ...] = (
            ("generation", replace(self.settings, embed_space="fixture-other-space"), {}),
            ("repository", replace(self.settings, embed_repository="fixture-owner/other-model"), {}),
            ("width", replace(self.settings, embed_dimensions=self.settings.embed_dimensions + 1), {}),
            ("listing", self.settings, {"/v1/models": {"data": []}}),
        )
        for reason, settings, replies in cases:
            with self.subTest(reason=reason), patch.dict(self.upstream_replies, replies):
                payload = {**RETRIEVE, "generation": self.generation} if reason == "generation" else RETRIEVE
                response = self.request(settings=settings, payload=payload)
                self.assert_refused(response, 503, "embedder_outside_space", reason, pinned=True)
                timings = json.loads(self.assert_failed_row("embedder_outside_space", reason)["timings_ms"])
                self.assertGreater(timings["record"], 0)
                self.assertGreater(timings["embedding"], 0)
                self.assertNotIn("dense", timings)
                self.assertNotIn("controlling", timings)
                self.assertNotIn("fusion", timings)
                self.assertNotIn(httpx.URL(settings.qdrant_url).host, {request.url.host for request in self.requests.values()})

    def test_named_generation_is_kept_in_the_reply_and_turn_row(self) -> None:
        name, collection, space, lockfiles = self.live_rows[0]
        response = self.request(payload={**RETRIEVE, "generation": name})
        self.assertEqual(response.status_code, 200, response.text)
        self.cursor.execute.assert_awaited_once()
        self.assertEqual(self.cursor.execute.await_args.args[1:], ((name,),))
        self.assertEqual(response.json()["generation"], {
            "gen_id": name, "collection": collection, "lockfiles": lockfiles, "embedding_space": space,
        })
        row = self.turn_row()
        self.assertEqual((row["generation_id"], json.loads(row["corpus_lockfiles"]), row["embedding_space"]), (
            name, lockfiles, space,
        ))

    def test_missing_or_ambiguous_generation_refuses_without_a_pool(self) -> None:
        rows: tuple[LiveRow, ...] = (*self.live_rows, (
            "fixture-second-generation", TURN_COLLECTION, self.settings.embed_space, [self.lockfile],
        ))
        for state, live_rows in (("no_live_generation", ()), ("ambiguous_generation", rows)):
            with self.subTest(state=state):
                self.assert_refused(self.request(rows=live_rows), 503, state)
                self.assert_failed_row(state)
                self.assertEqual(self.requests, {})

    def test_lockfile_outside_release_refuses_only_the_scoped_turn(self) -> None:
        missing = "corpus-2099-04-13"
        rows: tuple[LiveRow, ...] = ((self.generation, TURN_COLLECTION, self.settings.embed_space, [missing]),)
        court = "Eleventh Circuit"
        scoped = {**RETRIEVE, "message": f"A fictitious appeal in the {court}"}
        for body in (scoped, {**scoped, "generation": self.generation}):
            with self.subTest(named="generation" in body):
                response = self.request(rows=rows, private_values=(court,), payload=body)
                self.assert_refused(response, 503, "lockfile_outside_release", pinned=True)
                self.assertEqual(
                    response.json()["error"]["message"], "The generation's corpus lockfile is outside the release.",
                )
                self.assertEqual(self.requests, {})
                row = self.assert_failed_row("lockfile_outside_release")
                self.assertEqual(row["generation_id"], self.generation)
                self.assertIsNone(row["provenance"])
                self.assertIsNone(row["pool"])

        response = self.request(rows=rows)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["generation"]["lockfiles"], [missing])
        self.assertIsNone(response.json()["provenance"]["filters"]["user_stated_scope"])
        self.assertEqual(self.turn_row()["outcome"], "reply")

    def test_record_read_and_deadline_refusals_write_failed_rows(self) -> None:
        missing_password = replace(self.settings.record, password_file=self.settings.record.password_file.with_suffix(".absent"))
        settings = replace(self.settings, record=missing_password)
        self.assert_refused(self.request(settings=settings), 503, "record_unavailable")
        self.assert_failed_row("record_unavailable")

        async def pending_read(_: str) -> None:
            await asyncio.Future()

        connection, cursor = record_with_rows(self.live_rows)
        cursor.execute.side_effect = pending_read
        with (
            patch.object(endpoint, "read_generation", partial(read_generation, timeout=0)),
            patch(f"{__name__}.record_with_rows", return_value=(connection, cursor)),
        ):
            self.assert_refused(self.request(), 504, "retrieval_timeout", "record")
        self.assert_failed_row("retrieval_timeout", "record")

    def test_embedder_refusals_write_failed_rows(self) -> None:
        cases: tuple[tuple[object, int, str], ...] = (
            ({}, 503, "embedder_unavailable"),
            (httpx.Response(400, json={"error": {"message": "Fictitious maximum context length error"}}),
             400, "message_outside_window"),
        )
        for reply, status, state in cases:
            with self.subTest(state=state), patch.dict(self.upstream_replies, {"/v1/embeddings": reply}):
                self.assert_refused(self.request(), status, state, pinned=True)
                self.assert_failed_row(state)

    def test_each_store_refusal_writes_one_failed_row(self) -> None:
        for word, path, body in (
            ("dense", self.dense_path, RETRIEVE), ("lexical", self.lexical_path, RETRIEVE),
            ("controlling", "controlling", RETRIEVE), ("passages", self.mget_path, RETRIEVE),
            ("passages", self.mget_path, FULL_TURN),
        ):
            for reply, status, state in (
                (httpx.ConnectError("Fictitious store connection failure"), 503, "store_unavailable"),
                ({}, 502, "store_reply_invalid"),
                (httpx.ReadTimeout("Fictitious store read timeout"), 504, "retrieval_timeout"),
            ):
                with self.subTest(word=word, state=state), patch.dict(self.upstream_replies, {path: reply}):
                    self.assert_refused(self.request(payload=body), status, state, word, pinned=True)
                    row = self.assert_failed_row(state, word)
                    if word == "passages":
                        self.assertEqual(self.engine_requests, [])
                        self.assertIsNone(row["evidence"])
                        self.assertEqual(json.loads(row["timings_ms"])["passages"], 1000.0)
                    if word == "controlling":
                        self.assertIsNone(row["pool"])
                        self.assertNotIn("controlling", json.loads(row["timings_ms"]))

    def test_either_vector_leg_failure_cancels_its_pending_peer(self) -> None:
        upstream = self.upstream
        for failed, other, word in (
            ("controlling", self.dense_path, "controlling"), (self.dense_path, "controlling", "dense"),
        ):
            started = asyncio.Event()
            cancelled: list[str] = []

            async def fail_while_peer_is_pending(
                request: httpx.Request, failed: str = failed, other: str = other,
                started: asyncio.Event = started, cancelled: list[str] = cancelled,
            ) -> httpx.Response:
                key = self.store_key(request)
                if key == other:
                    started.set()
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        cancelled.append(key)
                        raise
                    self.fail("Fictitious store unexpectedly resumed.")
                if key == failed:
                    await started.wait()
                    return httpx.Response(503)
                return upstream(request)

            with self.subTest(leg=word), patch.object(self, "upstream", fail_while_peer_is_pending):
                self.assert_refused(self.request(), 503, "store_unavailable", word, pinned=True)
                self.assertEqual(cancelled, [other])

    def assert_no_turn_work(self) -> None:
        self.assertEqual(self.requests, {})
        self.assertEqual(self.engine_requests, [])
        self.connect.assert_not_called()
        self.dispatch.assert_not_called()

    def test_unverified_sessions_never_reach_the_record_stores_or_generator(self) -> None:
        for token, reaches_frontend, status, state, word in (
            (None, False, 403, "caller_not_admitted", None),
            ("sk-fictitious-key", False, 401, "session_not_verified", "session_token"),
            ("fixture.session.forged", True, 401, "session_not_verified", "session_token"),
            ("fixture.session.expired", True, 401, "session_not_verified", "session_token"),
        ):
            with self.subTest(token=token):
                self.frontend_replies = {SESSION_ROUTE: httpx.Response(401)} if reaches_frontend else {}
                body = FULL_TURN if token is None else {**FULL_TURN, "session_token": token}
                private = () if token is None else (token,)
                response = self.request(payload=body, eval_caller=False, private_values=private)
                self.assert_refused(response, status, state, word)
                expected = [[f"Bearer {token}"]] if reaches_frontend else []
                self.assertEqual(
                    [request.headers.get_list("Authorization") for request in self.frontend_requests], expected,
                )
                self.assert_no_turn_work()
                self.audit_connect.assert_not_called()

    def test_frontend_failures_refuse_admission_before_turn_work(self) -> None:
        token = "fixture.session.unavailable"
        for reply in (httpx.Response(502), httpx.ConnectError(token)):
            with self.subTest(reply=type(reply).__name__):
                self.frontend_replies[SESSION_ROUTE] = reply
                response = self.request(
                    payload={**FULL_TURN, "session_token": token}, eval_caller=False, private_values=(token,),
                )
                self.assert_refused(response, 503, "frontend_unavailable", "admission")
                self.assertEqual(len(self.frontend_requests), 1)
                self.assert_no_turn_work()
                self.audit_connect.assert_not_called()

    def test_knowledge_refusals_write_one_audit_row_before_the_response(self) -> None:
        token = "fixture.session.knowledge-refusal"
        path = KNOWLEDGE_ROUTE + KB_ID
        order: list[str] = []

        async def execute(query: str, values: tuple[object, ...]) -> None:
            if query.startswith(f"INSERT INTO {auditlog.AUDIT_LOG_TABLE} "):
                await asyncio.sleep(0)
                order.append("insert completed")

        self.audit_connection.execute.side_effect = execute
        for reply, status, state, word, reason, recorded in (
            (httpx.Response(401), 403, "kb_access_denied", "kb_ids", "kb_not_readable", 401),
            (httpx.ConnectError(token), 503, "frontend_unavailable", "admission", "frontend_unavailable", "none"),
            (httpx.Response(503), 503, "frontend_unavailable", "admission", "frontend_unavailable", 503),
        ):
            with self.subTest(reply=recorded):
                order.clear()
                self.frontend_replies = {SESSION_ROUTE: session_reply(token), path: reply}
                response = self.request(
                    payload={**FULL_TURN, "session_token": token, "kb_ids": [KB_ID]},
                    eval_caller=False, on_send=lambda message: order.append(message["type"]), private_values=(token,),
                )
                self.assert_refused(response, status, state, word)
                self.assertEqual(self.turn_logs[0].levelno, logging.WARNING)
                self.assertEqual(order, ["insert completed", "http.response.start", "http.response.body"])
                self.assertEqual([request.url.path for request in self.frontend_requests], [SESSION_ROUTE, path])
                row = self.audit_row()
                self.assertEqual((row["kind"], row["user_id"], row["chat_id"], row["kb_ids"]), (
                    denial.DENIAL_KIND, USER_ID, CHAT_ID, [KB_ID],
                ))
                self.assertEqual(row["detail"], {
                    "reason": reason, "result": "denied", "claim": "user",
                    "failures": [{"kb_id": KB_ID, "reply": recorded}],
                })
                self.assert_no_turn_work()
                self.assertNotIn(token, repr(row))
                self.assertNotIn(token, repr(response.headers))
                self.assertEqual(self.frontend_cookies, ())

    def test_unconfirmed_audit_writes_log_one_error_and_preserve_the_refusal(self) -> None:
        token = "fixture.session.audit-refusal"
        path = KNOWLEDGE_ROUTE + KB_ID
        body = {**FULL_TURN, "session_token": token, "kb_ids": [KB_ID]}
        self.frontend_replies = {SESSION_ROUTE: session_reply(token), path: httpx.Response(401)}
        kept = self.request(payload=body, eval_caller=False, private_values=(token,))
        self.assert_refused(kept, 403, "kb_access_denied", "kb_ids")
        run_ids = {self.audit_row()["run_id"]}

        for outcome in ("not_kept", "unknown"):
            with self.subTest(outcome=outcome):
                self.frontend_replies = {SESSION_ROUTE: session_reply(token), path: httpx.Response(401)}
                self.audit_connect.side_effect = OperationalError(token) if outcome == "not_kept" else None
                self.audit_connection.execute.side_effect = [None, OperationalError(token)] if outcome == "unknown" else None
                response = self.request(payload=body, eval_caller=False, private_values=(token,))
                _, error = self.turn_logs
                run_id = UUID(error.getMessage().rpartition(" run_id=")[2])
                self.assertEqual(run_id.version, 4)
                self.assertNotIn(run_id, run_ids)
                run_ids.add(run_id)
                self.assert_refused(
                    response, 403, "kb_access_denied", "kb_ids",
                    incident_error=f"state={denial.DENIAL_KIND} reason=kb_not_readable outcome={outcome} run_id={run_id}",
                )
                self.assertEqual((response.status_code, response.content), (kept.status_code, kept.content))
                self.audit_connect.assert_awaited_once()
                if outcome == "unknown":
                    row = self.audit_row()
                    self.assertEqual(row["run_id"], run_id)
                    self.assertNotIn(token, repr(row))
                else:
                    self.audit_connection.execute.assert_not_called()
                self.assert_no_turn_work()
                self.assertNotIn(token, repr(response.headers))
                self.assertEqual(self.frontend_cookies, ())

    def test_verified_user_is_admitted_and_the_echoed_token_is_discarded(self) -> None:
        token = "fixture.session.verified"
        self.frontend_replies[SESSION_ROUTE] = session_reply(token)
        with patch.object(objects, "extract_objects", AsyncMock(return_value=())):
            response = self.request(
                payload={**FULL_TURN, "session_token": token}, eval_caller=False, private_values=(token,),
            )
        emitted = self.read_events(response)
        self.assertTrue(any(kind == "answer" for kind, _ in emitted))
        self.connect.assert_awaited_once()
        request, = self.frontend_requests
        self.assertEqual(request.headers.get_list("Authorization"), [f"Bearer {token}"])
        row = self.turn_row()
        self.assertEqual(row["source"], "user")
        audit = self.assert_research_audit(row, sent_count=sum(kind == "passage" for kind, _ in emitted))
        self.assertEqual((audit["user_id"], audit["kb_ids"]), (USER_ID, []))
        self.assertNotIn(token, repr(audit))
        self.assertNotIn(token, repr(row))
        self.assertNotIn(token, repr(response.headers))
        self.assertEqual(self.frontend_cookies, ())

    def test_audit_uses_the_admitted_callers_grants_and_verified_user(self) -> None:
        grants = frozenset({"fixture-granted-z", "fixture-granted-a"})
        caller = Caller("eval", USER_ID, Floor(frozenset({TURN_COLLECTION}), grants))
        with patch.object(endpoint, "admit_caller", return_value=caller):
            response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        row = self.assert_research_audit(self.turn_row(), sent_count=len(response.json()["final_set"]))
        self.assertEqual((row["user_id"], row["kb_ids"]), (caller.user_id, sorted(grants)))
        self.assertEqual(row["detail"]["claim"], caller.kind)

    def test_eval_admission_without_knowledge_bases_skips_the_frontend(self) -> None:
        with patch.object(objects, "extract_objects", AsyncMock(return_value=())):
            response = self.request(payload={**RETRIEVE, "session_token": "sk-fictitious-unread"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.frontend_requests, [])
        self.assertEqual(self.turn_row()["source"], "eval")

    def test_admission_refusals_never_write_a_turn_row(self) -> None:
        cases: tuple[tuple[dict[str, object] | bytes, bool, int, str, str | None], ...] = (
            (b'{"message":', True, 400, "invalid_request", None),
            ({**FULL_TURN, "message_id": "Fictitious free text"}, True, 400, "invalid_request", "message_id"),
            (RETRIEVE, False, 403, "eval_only_field", "stop_after"),
            ({**FULL_TURN, "evidence": []}, False, 403, "eval_only_field", "evidence"),
            ({**FULL_TURN, "overrides": {}}, False, 403, "eval_only_field", "overrides"),
            ({**FULL_TURN, "overrides": {"fixture-unknown": True}}, True, 400, "override_not_allowed", "overrides"),
            ({**FULL_TURN, "overrides": {"pool_cap": 0}}, True, 400, "override_invalid", "pool_cap"),
            ({**FULL_TURN, "stop_after": "generate"}, True, 501, "stage_not_built", "stop_after"),
            ({**FULL_TURN, "evidence": []}, True, 501, "stage_not_built", "evidence"),
            ({**FULL_TURN, "kb_ids": ["fixture-kb"]}, True, 403, "caller_not_admitted", None),
            ({**FULL_TURN, "kb_ids": ["fixture-kb"]}, False, 403, "caller_not_admitted", None),
        )
        for body, eval_caller, status, state, word in cases:
            with self.subTest(state=state, word=word):
                response = self.request(payload=body, eval_caller=eval_caller, scrape=True)
                self.assert_refused(response, status, state, word)
                self.dispatch.assert_not_called()
                self.connect.assert_not_awaited()
                self.assertEqual(self.metrics_before, self.metrics_after)
                self.assertEqual(self.frontend_requests, [])
                if state == "caller_not_admitted":
                    row = self.audit_row()
                    self.assertEqual(row["kind"], denial.DENIAL_KIND)
                    self.assertIsNone(row["user_id"])
                    self.assertEqual(row["detail"], {
                        "reason": "no_session", "result": "denied",
                        "claim": "eval" if eval_caller else "user", "failures": [],
                    })
                else:
                    self.audit_connect.assert_not_called()

    def test_verified_knowledge_bases_are_refused_without_an_audit_row(self) -> None:
        token = "fixture.session.knowledge-permitted"
        path = KNOWLEDGE_ROUTE + KB_ID
        self.frontend_replies = {SESSION_ROUTE: session_reply(token), path: httpx.Response(200, json={"id": KB_ID})}
        response = self.request(
            payload={**FULL_TURN, "session_token": token, "kb_ids": [KB_ID]}, eval_caller=False, private_values=(token,),
        )
        self.assert_refused(response, 501, "kb_ids_not_built", "kb_ids")
        self.assertEqual([request.url.path for request in self.frontend_requests], [SESSION_ROUTE, path])
        self.assert_no_turn_work()
        self.audit_connect.assert_not_called()

    def test_unexpected_exception_returns_a_fixed_error_and_logs_only_the_exception_class(self) -> None:
        self.upstream_replies["/v1/embeddings"] = RuntimeError(MESSAGE)
        self.assert_refused(self.request(), 500, "internal_error", pinned=True)
        row = self.assert_failed_row("internal_error")
        self.assertNotIn(MESSAGE, repr(row))
        self.assertIn("exception=RuntimeError", self.turn_logs[0].getMessage())

    def test_retrieval_row_keeps_shaped_ids_and_discards_a_free_text_header(self) -> None:
        message_id = "fixture-research-message_17"
        for chat_id, expected in ((CHAT_ID, CHAT_ID), ("Fictitious free text", None)):
            with self.subTest(chat_id=chat_id):
                response = self.request(payload={**RETRIEVE, "message_id": message_id}, chat_id=chat_id)
                self.assertEqual(response.status_code, 200)
                row = self.turn_row()
                self.assertEqual((row["chat_id"], row["message_id"]), (expected, message_id))
                audit = self.assert_research_audit(row, sent_count=len(response.json()["final_set"]))
                self.assertEqual(audit["chat_id"], expected)

    def test_unresolved_provenance_entry_is_projected_in_the_row(self) -> None:
        entry = {"index": 0, "type": "reporter", "resolved": "unresolved", "hits": 1}

        def with_unresolved(**kwargs: Any) -> dict[str, object]:
            return {**build_provenance(**kwargs), "out_of_corpus_objects": [entry]}

        build_provenance = provenance.retrieval_provenance
        with patch.object(provenance, "retrieval_provenance", side_effect=with_unresolved):
            response = self.request()
        self.assertEqual(response.json()["provenance"]["out_of_corpus_objects"], [entry])
        row = self.turn_row()
        self.assertNotIn("out_of_corpus_objects", json.loads(row["provenance"]))
        self.assertEqual(json.loads(row["out_of_corpus_objects"]), [{"type": entry["type"], "resolved": entry["resolved"]}])

    def test_dispatch_failure_leaves_retrieval_response_bytes_unchanged(self) -> None:
        baseline = self.request()
        self.assertEqual(baseline.status_code, 200)
        self.dispatch.side_effect = RuntimeError("Fictitious dispatch failure")
        response = self.request()
        self.dispatch.assert_called_once()
        self.assertEqual((response.status_code, response.content), (baseline.status_code, baseline.content))
        self.assert_research_audit(self.turn_row(), sent_count=len(response.json()["final_set"]))

    def test_unconfirmed_turn_audit_leaves_one_error_and_the_same_reply(self) -> None:
        baseline = self.request()
        self.assertEqual(baseline.status_code, 200, baseline.text)
        for outcome in ("not_kept", "unknown"):
            with self.subTest(outcome=outcome):
                self.audit_connect.side_effect = OperationalError(MESSAGE) if outcome == "not_kept" else None
                self.audit_connection.execute.side_effect = [None, OperationalError(MESSAGE)] if outcome == "unknown" else None
                response = self.request()
                self.assertEqual((response.status_code, response.content), (baseline.status_code, baseline.content))
                self.assertEqual(self.turn_row()["outcome"], "reply")
                run_id = self.assert_turn_audit_error("reply", outcome)
                self.audit_connect.assert_awaited_once()
                if outcome == "unknown":
                    self.assertEqual(self.audit_row()["run_id"], run_id)
                else:
                    self.audit_connection.execute.assert_not_called()

    def test_unbuildable_turn_audit_leaves_one_error_and_the_same_reply(self) -> None:
        baseline = self.request()
        self.assertEqual(baseline.status_code, 200, baseline.text)
        for module, function in ((turnlog, "kept_values"), (turnaudit, "build_row")):
            with self.subTest(function=function), patch.object(module, function, side_effect=RuntimeError(MESSAGE)):
                response = self.request()
                self.assertEqual((response.status_code, response.content), (baseline.status_code, baseline.content))
                self.assert_turn_audit_error("reply", "not_built")
                self.audit_connect.assert_not_called()
                if module is turnaudit:
                    self.assertEqual(self.turn_row()["outcome"], "reply")
                else:
                    self.dispatch.assert_not_called()

    def assert_turn_audit_error(self, reason: str, outcome: str) -> UUID:
        error, = self.turn_logs
        run_id = UUID(error.getMessage().rpartition(" run_id=")[2])
        self.assertEqual(run_id.version, 4)
        self.assertEqual(error.levelno, logging.ERROR)
        self.assertIsNone(error.exc_info)
        self.assertEqual(
            error.getMessage(), f"state={turnaudit.TURN_KIND} reason={reason} outcome={outcome} run_id={run_id}",
        )
        return run_id

    def test_unbuildable_audit_preserves_the_turn_exception(self) -> None:
        failure = RuntimeError("Fictitious turn exception")
        with (
            patch.object(endpoint, "run_with_disconnect", side_effect=failure),
            patch.object(turnaudit, "build_row", side_effect=ValueError("Fictitious builder exception")),
            self.assertRaises(RuntimeError) as raised,
        ):
            self.request()
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.turn_row()["outcome"], "disconnected")
        self.audit_connect.assert_not_called()

    def test_observation_failure_leaves_the_retrieval_response_and_row_unchanged(self) -> None:
        baseline = self.request()
        self.assertEqual(baseline.status_code, 200)
        baseline_row = self.turn_row()
        sentinel = "Fictitious observation failure detail"

        def fail_observation(*_args: object) -> None:
            self.dispatch.assert_called_once()
            raise RuntimeError(sentinel)

        with patch.object(measures, "observe_turn", side_effect=fail_observation) as observe:
            response = self.request(private_values=(sentinel,))

        observe.assert_called_once()
        self.assertEqual((response.status_code, response.content), (baseline.status_code, baseline.content))
        self.assertEqual(self.turn_row(), baseline_row)
        line, = self.turn_logs
        self.assertEqual(line.getMessage(), "Turn metrics observation failed: RuntimeError")
        self.assertIsNone(line.exc_info)

    def test_failure_after_fusion_keeps_the_same_retrieval_timing_as_a_reply(self) -> None:
        baseline = self.request()
        self.assertEqual(baseline.status_code, 200)
        retrieval_ms = baseline.json()["timings_ms"]["retrieval"]

        self.upstream_replies[self.mget_path] = {"docs": []}
        response = self.request(scrape=True)
        self.assert_refused(response, 502, "store_reply_invalid", "passages", pinned=True)
        row = self.assert_failed_row("store_reply_invalid", "passages")
        self.assertEqual(json.loads(row["timings_ms"])["retrieval"], retrieval_ms)

        source = f'source="{row["source"]}"'
        failed = f'{measures.TURNS_METRIC}{{{source},outcome="failed",state="{row["failure_state"]}"}}'
        retrieval_sum = f"{measures.RETRIEVAL_METRIC}_sum{{{source}}}"
        self.assertEqual(self.metrics_after[failed] - self.metrics_before[failed], 1)
        self.assertEqual(self.metrics_after[retrieval_sum] - self.metrics_before[retrieval_sum], retrieval_ms / 1000)

    def test_disconnect_during_retrieval_cancels_the_pending_store_and_writes_one_row(self) -> None:
        async def run() -> None:
            pending = asyncio.Event()
            cancelled = asyncio.Event()
            upstream = self.upstream

            async def wait_in_store(request: httpx.Request) -> httpx.Response:
                if self.store_key(request) != self.dense_path:
                    return upstream(request)
                pending.set()
                try:
                    return await asyncio.Future()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            with patch.object(self, "upstream", wait_in_store):
                async with self.retrieve_session() as session:
                    await asyncio.wait_for(pending.wait(), timeout=1)
                    session.disconnect()
                    await session.wait_for_application()
                    self.assertTrue(cancelled.is_set())
                    self.assertTrue(session.outgoing.empty())

        asyncio.run(run())
        row = self.turn_row()
        self.assertEqual(row["outcome"], "disconnected")
        self.assert_research_audit(row)
        timings = json.loads(row["timings_ms"])
        self.assertTrue({"record", "embedding", "lexical", "total"} <= timings.keys())
        self.assertNotIn("dense", timings)

    def test_disconnect_after_the_last_reply_byte_keeps_reply_and_its_total(self) -> None:
        async def run() -> None:
            session = self.retrieve_session()
            last_byte = asyncio.Event()
            delivered = asyncio.Event()
            sent: list[Message] = []
            clock_after_send: list[int] = []

            async def receive() -> Message:
                if not session.incoming.empty():
                    return session.incoming.get_nowait()
                await last_byte.wait()
                delivered.set()
                return {"type": "http.disconnect"}

            async def send(message: Message) -> None:
                sent.append(message)
                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    clock_after_send.append(next(self.clock))
                    last_byte.set()
                    # The transport has accepted the last byte. Let its send return even if the watch sees EOF.
                    with anyio.CancelScope(shield=True):
                        await delivered.wait()
                    self.audit_connect.assert_not_called()

            async with session.app.router.lifespan_context(session.app):
                await asyncio.wait_for(session.app(session.scope, receive, send), timeout=1)

            self.assertTrue(delivered.is_set())
            start, final = sent
            self.assertEqual(start["status"], 200)
            body = json.loads(final["body"])
            body_timings = body["timings_ms"]
            observed_tick, = clock_after_send
            self.assertGreater(observed_tick * 1000, body_timings["total"])
            row = self.turn_row()
            self.assertEqual(row["outcome"], "reply")
            self.assertEqual(json.loads(row["timings_ms"]), body_timings)
            self.assert_research_audit(row, sent_count=len(body["final_set"]))

        asyncio.run(run())

    def test_audit_connect_follows_the_last_body_send(self) -> None:
        async def run(body: dict[str, object], outcome: str) -> None:
            session = ASGISession(
                self.app(self.settings), "POST", "/turn", json.dumps(body).encode(), self.headers(self.settings), "2.4",
            )
            order: list[tuple[str, bool]] = []

            async def send(message: Message) -> None:
                await asyncio.sleep(0)
                order.append((message["type"], message.get("more_body", False)))

            def connect(**_: object) -> Mock:
                order.append(("audit_connect", False))
                return self.audit_connection

            self.audit_connect.side_effect = connect
            async with session.app.router.lifespan_context(session.app):
                await asyncio.wait_for(session.app(session.scope, session.incoming.get, send), timeout=1)

            self.assertEqual(order[0], ("http.response.start", False))
            self.assertEqual(order[-2:], [("http.response.body", False), ("audit_connect", False)])
            streaming_bodies = order[1:-2]
            self.assertEqual(bool(streaming_bodies), outcome == "complete")
            self.assertTrue(all(message == ("http.response.body", True) for message in streaming_bodies))
            self.assertEqual(self.turn_row()["outcome"], outcome)
            self.assertEqual(self.audit_row()["detail"]["outcome"], outcome)

        for body, outcome in ((FULL_TURN, "complete"), (RETRIEVE, "reply")):
            with self.subTest(outcome=outcome):
                asyncio.run(run(body, outcome))

    def test_retrieve_send_disconnect_keeps_an_audit_row_with_no_returned_hits(self) -> None:
        row, audit = self.assert_json_send_disconnect("disconnected")
        self.assertEqual(row["stop_after"], "retrieve")
        self.assertTrue(json.loads(row["evidence"]))
        self.assertEqual(audit["detail"]["returned"], [])

    def test_failure_body_send_disconnect_keeps_the_known_failure(self) -> None:
        with patch.dict(self.upstream_replies, {self.dense_path: {}}):
            row, _ = self.assert_json_send_disconnect("failed")
        self.assertEqual((row["failure_state"], row["failure_word"]), ("store_reply_invalid", "dense"))

    def assert_json_send_disconnect(self, outcome: str) -> tuple[dict[str, Any], dict[str, Any]]:
        async def run() -> None:
            session = self.retrieve_session()
            watching = asyncio.Event()
            failed_sends: list[Message] = []

            async def receive() -> Message:
                if session.incoming.empty():
                    watching.set()
                return await session.incoming.get()

            async def send(message: Message) -> None:
                if message["type"] == "http.response.body":
                    await watching.wait()
                    failed_sends.append(message)
                    raise OSError("Fictitious closed transport")

            async with session.app.router.lifespan_context(session.app):
                await asyncio.wait_for(session.app(session.scope, receive, send), timeout=1)
            self.assertEqual(len(failed_sends), 1)

        asyncio.run(run())
        row = self.turn_row()
        self.assertEqual(row["outcome"], outcome)
        return row, self.assert_research_audit(row)

    def test_release_final_set_uses_top_scored_unpinned_hits_with_one_read_per_turn(self) -> None:
        config = effective_config(reranker=True)
        assert not isinstance(config, OverrideRefusal)
        limit = config["final_k"]
        candidate_count = limit + 3
        self.assertLessEqual(candidate_count, config["stage2_depth"])
        lexical_reply = turn_fixture("lexical-collapsed.json")
        template = lexical_reply["hits"]["hits"][-1]
        chunk_ids = [f"fixture-final-chunk-{i}" for i in range(candidate_count)]
        lexical_reply["hits"]["hits"] = [
            {**template, "_id": chunk_id, "_source": {**template["_source"], "dupe_cluster": chunk_id},
             "fields": {"dupe_cluster": [chunk_id]}}
            for chunk_id in chunk_ids
        ]
        lexical_reply["hits"]["total"]["value"] = candidate_count
        self.upstream_replies[self.lexical_path] = lexical_reply
        for path, name in ((self.dense_path, "dense-grouped.json"), ("controlling", "controlling-grouped.json")):
            vector_reply = turn_fixture(name)
            vector_reply["result"]["groups"] = []
            self.upstream_replies[path] = vector_reply

        texts = {chunk_id: f"Fictitious final set passage for {chunk_id}." for chunk_id in chunk_ids}
        document_text = "Fictitious full opinion body beyond the candidate passages."
        source = self.passage_doc["_source"]
        private_values = (source["caption"], document_text, *texts.values())
        floor = config["abstention.floor"]
        scores = {
            chunk_id: floor + (1 - floor) * i / (candidate_count + 1) for i, chunk_id in enumerate(chunk_ids, start=1)
        }
        scores_by_text = {text: scores[chunk_id] for chunk_id, text in texts.items()}
        self.upstream_replies[self.mget_path] = {"docs": [
            {**self.passage_doc, "_id": chunk_id, "_source": {
                **source, "text": texts[chunk_id], "doc_id": f"opinion-{chunk_id}", "document_text": document_text,
            }}
            for chunk_id in reversed(chunk_ids)
        ]}

        def score(request: httpx.Request) -> httpx.Response:
            sent = json.loads(request.content)
            self.assertCountEqual(sent["documents"], texts.values())
            return httpx.Response(200, json={"results": [
                {"index": index, "relevance_score": scores_by_text[text]}
                for index, text in reversed(list(enumerate(sent["documents"])))
            ]})

        self.rerank_transport = httpx.MockTransport(score)
        key_file = self.settings.embed_key_file.with_name("reranker")
        key_file.write_text("fictitious-final-set-rerank-key", encoding="utf-8")
        settings = replace(self.settings, rerank_url="http://fixture-reranker", rerank_key_file=key_file)
        with patch.object(self, "upstream", wraps=self.upstream) as stores:
            response = self.request(settings=settings, private_values=private_values)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(sum(call.args[0].url.path == self.mget_path for call in stores.call_args_list), 1)
            retrieve_row = self.turn_row()
            stores.reset_mock()
            full = self.request(settings=settings, payload=FULL_TURN, private_values=private_values, scrape=True)
            self.assertEqual(sum(call.args[0].url.path == self.mget_path for call in stores.call_args_list), 1)

        retrieval = response.json()
        self.assertEqual(retrieval["retrieval_config_hash"], config_hash(config))
        self.assertEqual(len(retrieval["pool"]), candidate_count)
        self.assertTrue(all(not entry["pinned"] and entry["stage2_score"] is not None for entry in retrieval["pool"]))
        expected_ids = list(reversed(chunk_ids))[:limit]
        final_set = retrieval["final_set"]
        self.assertEqual([hit["chunk_id"] for hit in final_set], expected_ids)
        self.assertEqual([hit["stage2_score"] for hit in final_set], [scores[chunk_id] for chunk_id in expected_ids])
        self.assertEqual([hit["seat"] for hit in final_set], ["score"] * limit)
        self.assertEqual(retrieval["timings_ms"]["passages"], 0.0)
        self.assertEqual(retrieval["timings_ms"]["rerank_passages"], 1000.0)
        summary = retrieval["provenance"]["final_set"]
        self.assertEqual(summary, {
            "k": limit, "mode": config["hierarchy.mode"], "ordered_by": "weighted",
            "reserved_controlling": config["final_reserved_controlling"],
            "per_document_cap": config["per_document_cap"], "budget_tokens": config["context_budget_tokens"],
            "candidates": candidate_count, "size": limit, "pinned": 0, "controlling_seated": 0,
            "by_tier": {"persuasive": limit}, "tokens": limit * source["token_count"],
            "document_capped": 0, "exempt_documents": 0, "budget_dropped": 0, "objects_unseated": [],
            "floor": floor, "floor_dropped": 0, "best_under_floor": None,
        })
        self.assertEqual(retrieve_row["stop_after"], "retrieve")
        self.assertEqual(json.loads(retrieve_row["evidence"]), final_set)
        self.assertEqual(json.loads(retrieve_row["provenance"])["final_set"], summary)

        emitted = self.read_events(full)
        self.assertEqual([data for kind, data in emitted if kind == "passage"], [
            {key: hit[key] for key in ("number", "chunk_id", "pool_rank")} for hit in final_set
        ])
        kind, phase = emitted[limit]
        self.assertEqual((kind, phase["stage"]), ("phase", "retrieve"))
        self.assertEqual(phase["retrieval"]["final_set"], final_set)
        full_row = self.assert_answer_row(emitted, "complete")
        self.assertEqual(json.loads(full_row["evidence"]), final_set)
        self.assertEqual(json.loads(full_row["provenance"])["final_set"], summary)
        timings = json.loads(full_row["timings_ms"])
        through_rerank_ms = phase["retrieval"]["timings_ms"]["total"]
        self.assertLess(through_rerank_ms, timings["total"])
        self.assert_turn_metrics(full_row, (
            (measures.RETRIEVAL_METRIC, measures.RETRIEVAL_BUCKETS, timings["retrieval"]),
            (measures.THROUGH_RERANK_METRIC, measures.THROUGH_RERANK_BUCKETS, through_rerank_ms),
            (measures.FIRST_PARAGRAPH_METRIC, measures.ANSWER_BUCKETS, timings["first_text"]),
            (measures.COMPLETE_METRIC, measures.ANSWER_BUCKETS, timings["total"]),
        ))

    def reranker_settings(self, respond: Callable[[httpx.Request], httpx.Response]) -> Settings:
        self.rerank_transport = httpx.MockTransport(respond)
        key_file = self.settings.embed_key_file.with_name("reranker")
        key_file.write_text("fictitious-under-floor-rerank-key", encoding="utf-8")
        return replace(self.settings, rerank_url="http://fixture-reranker", rerank_key_file=key_file)

    def empty_retrieval_legs(self) -> None:
        for path, fixture in ((self.dense_path, "dense-grouped.json"), ("controlling", "controlling-grouped.json")):
            reply = turn_fixture(fixture)
            reply["result"]["groups"] = []
            self.upstream_replies[path] = reply
        lexical_reply = turn_fixture("lexical-collapsed.json")
        lexical_reply["hits"].update({"hits": [], "total": {"value": 0, "relation": "eq"}, "max_score": None})
        self.upstream_replies[self.lexical_path] = lexical_reply

    def test_retrieve_below_the_floor_keeps_the_ranked_pool_and_records_no_support(self) -> None:
        fixture = turn_fixture("rerank-under-floor.json")
        reranker = Mock(return_value=httpx.Response(200, json=fixture))
        settings = self.reranker_settings(reranker)

        response = self.request(settings=settings, payload=RETRIEVE)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/json")
        reranker.assert_called_once()
        self.assertEqual(self.engine_requests, [])
        body = response.json()
        self.assertEqual(body["support_level"], "none")
        self.assertEqual(body["final_set"], [])
        candidates = [doc["_id"] for doc in json.loads(self.requests[self.mget_path].content)["docs"]]
        scores = [result["relevance_score"] for result in fixture["results"]]
        self.assertCountEqual([entry["chunk_id"] for entry in body["pool"]], candidates)
        self.assertEqual([entry["stage2_score"] for entry in body["pool"]], sorted(scores, reverse=True))
        floor = RETRIEVAL_CONFIG["abstention.floor"]
        self.assertLess(max(scores), floor)
        support = {
            "level": "none", "count": 0, "thin_below": RETRIEVAL_CONFIG["abstention.thin_below"],
            "floor": floor, "dropped": len(scores), "best_under_floor": max(scores),
            "question": {"kind": "law", "decided_by": "default"},
        }
        self.assertEqual(body["provenance"]["support"], support)
        row = self.turn_row()
        self.assertEqual((row["outcome"], row["support_level"]), ("reply", "none"))
        self.assertEqual(json.loads(row["evidence"]), [])
        self.assertEqual(json.loads(row["pool"]), body["pool"])
        self.assertEqual(json.loads(row["provenance"])["support"], support)
        self.assertEqual(json.loads(row["misses"]), [{
            "kind": "abstention", "floor": floor, "best_score": max(scores),
            "chunk_ids": [entry["chunk_id"] for entry in body["pool"]],
        }])

    def test_abstention_streams_only_retrieval_and_notice_and_keeps_the_row_pool(self) -> None:
        fixture = turn_fixture("rerank-under-floor.json")
        settings = self.reranker_settings(lambda _: httpx.Response(200, json=fixture))
        source = self.passage_doc["_source"]
        assert settings.rerank_key_file is not None
        private_values = (source["caption"], source["text"], settings.rerank_key_file.read_text(encoding="utf-8"))
        sent: list[Message] = []

        def fail_on_engine_request(_: httpx.Request) -> httpx.Response:
            self.fail("An abstaining turn reached the generator")

        with patch.object(self, "engine_upstream", side_effect=fail_on_engine_request) as engine:
            retrieved = self.request(settings=settings, payload=RETRIEVE, private_values=private_values)
            response = self.request(settings=settings, payload=FULL_TURN, on_send=sent.append, private_values=private_values)
        engine.assert_not_called()
        self.assertEqual(retrieved.status_code, 200, retrieved.text)
        retrieval = retrieved.json()
        self.assertTrue(retrieval["pool"])
        self.assertEqual(retrieval["final_set"], [])
        self.assertEqual(retrieval["support_level"], "none")
        emitted = self.read_events(response)
        self.assertEqual([kind for kind, _ in emitted], ["phase", "answer"])
        # The ranked pool stays, as an answering turn's does; only passages and the final set are empty.
        self.assertEqual(emitted[0][1], {"stage": "retrieve", "retrieval": retrieval})
        answer = emitted[1][1]
        total = answer["timings_ms"]["total"]
        self.assertGreater(total, retrieval["timings_ms"]["total"])
        self.assertEqual(answer, {
            "text": notice.abstention_notice((self.lockfile,), ()),
            "end": "abstained", "timings_ms": {**retrieval["timings_ms"], "total": total},
        })
        self.assertEqual(sent[-1], {"type": "http.response.body", "body": b"", "more_body": False})

        row = self.turn_row()
        self.assertEqual((row["outcome"], row["support_level"]), ("abstained", "none"))
        audit = self.assert_research_audit(row)
        self.assertEqual((audit["detail"]["outcome"], audit["detail"]["support_level"], audit["detail"]["returned"]), ("abstained", "none", []))
        self.assertEqual(json.loads(row["pool"]), retrieval["pool"])
        self.assertEqual(json.loads(row["evidence"]), [])
        self.assertEqual(json.loads(row["timings_ms"]), answer["timings_ms"])
        self.assertEqual(json.loads(row["misses"]), [{
            "kind": "abstention", "floor": RETRIEVAL_CONFIG["abstention.floor"],
            "best_score": max(result["relevance_score"] for result in fixture["results"]),
            "chunk_ids": [entry["chunk_id"] for entry in retrieval["pool"]],
        }])
        self.assertIsNone(row["engine_counters"])
        self.assertIsNone(row["generator_repository"])

    def test_scoped_abstention_names_the_absent_court_from_the_court_file(self) -> None:
        self.empty_retrieval_legs()
        typed_court = "FiRsT CiR."
        message = f"{MESSAGE} in the {typed_court}"
        court_file = turn_fixture("courts.json")
        court_id = "ca1"
        court_name = court_file["courts"][court_id]["name"]
        self.assertNotIn(court_id, court_file["lockfiles"][self.lockfile])

        response = self.request(payload={"message": message}, private_values=(message, typed_court))

        (phase_kind, phase), (answer_kind, answer) = self.read_events(response)
        self.assertEqual((phase_kind, answer_kind, answer["end"]), ("phase", "answer", "abstained"))
        self.assertEqual(phase["retrieval"]["provenance"]["filters"]["user_stated_scope"]["out_of_corpus"], [court_id])
        self.assertEqual(answer["text"], notice.abstention_notice((self.lockfile,), (court_name,)))
        self.assertNotIn(court_name, repr(self.turn_row()))

    def test_reporter_pin_answers_even_at_the_maximum_floor(self) -> None:
        citation = "444 U. S. 987"  # Fictitious reporter cite.
        exact = ExactObject("case_cite", 0, len(citation), citation)
        self.case_extractor = Mock(return_value=((exact, "444 U.S. 987"),))
        reply = turn_fixture("objects-msearch.json")
        held = reply["responses"][0]
        empty_citing = turn_fixture("objects-msearch-empty.json")["responses"][1]
        self.upstream_replies[self.msearch_path] = {**reply, "responses": [held, empty_citing]}
        held_chunk = held["hits"]["hits"][0]["_id"]
        score = turn_fixture("rerank-under-floor.json")["results"][0]["relevance_score"]

        def score_candidates(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"results": [
                {"index": index, "relevance_score": score}
                for index, _ in enumerate(json.loads(request.content)["documents"])
            ]})

        settings = self.reranker_settings(score_candidates)
        response = self.request(settings=settings, payload={"message": citation, "overrides": {"abstention.floor": 1}})

        emitted = self.read_events(response)
        self.assertEqual(emitted[0][0], "passage")
        self.assertEqual(emitted[0][1]["chunk_id"], held_chunk)
        self.assertEqual((emitted[-1][0], emitted[-1][1]["end"]), ("answer", "complete"))
        self.assertEqual(self.engine_paths(), [MODELS_ROUTE, COMPLETIONS_ROUTE])
        row = self.turn_row()
        self.assertEqual(row["outcome"], "complete")
        pin, = json.loads(row["evidence"])
        self.assertEqual((pin["chunk_id"], pin["seat"], pin["stage2_score"]), (held_chunk, "pin", score))
        self.assertIsNone(row["misses"])

    def test_abstention_send_disconnect_keeps_the_decided_outcome(self) -> None:
        fixture = turn_fixture("rerank-under-floor.json")
        settings = self.reranker_settings(lambda _: httpx.Response(200, json=fixture))

        async def run() -> None:
            session = ASGISession(
                self.app(settings), "POST", "/turn", json.dumps(FULL_TURN).encode(), self.headers(settings), "2.4",
            )
            watching = asyncio.Event()
            failed_sends: list[Message] = []

            async def receive() -> Message:
                if session.incoming.empty():
                    watching.set()
                return await session.incoming.get()

            async def send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    await watching.wait()
                    failed_sends.append(message)
                    raise OSError("Fictitious closed transport")

            async with session.app.router.lifespan_context(session.app):
                await asyncio.wait_for(session.app(session.scope, receive, send), timeout=1)

            failed_start, = failed_sends
            self.assertEqual(failed_start["status"], 200)

        asyncio.run(run())
        row = self.turn_row()
        self.assertEqual((row["outcome"], row["support_level"]), ("abstained", "none"))

    def test_empty_pool_abstains_with_a_scoreless_miss(self) -> None:
        self.empty_retrieval_legs()
        config = effective_config(reranker=False)
        assert not isinstance(config, OverrideRefusal)

        response = self.request(payload=FULL_TURN)

        (phase_kind, phase), (answer_kind, answer) = self.read_events(response)
        self.assertEqual((phase_kind, answer_kind, answer["end"]), ("phase", "answer", "abstained"))
        self.assertEqual((phase["retrieval"]["pool"], phase["retrieval"]["final_set"]), ([], []))
        self.assertEqual(self.engine_requests, [])
        row = self.turn_row()
        self.assertEqual(row["outcome"], "abstained")
        self.assertEqual(json.loads(row["pool"]), [])
        self.assertEqual(json.loads(row["misses"]), [{
            "kind": "abstention", "floor": config["abstention.floor"], "best_score": None, "chunk_ids": [],
        }])

    def test_thin_turn_prompts_with_the_thin_instruction_and_keeps_the_above_floor_hit(self) -> None:
        fixture = turn_fixture("rerank.json")
        self.empty_retrieval_legs()
        dense_reply = turn_fixture("dense-grouped.json")
        dense_reply["result"]["groups"] = self.groups[:len(fixture["results"])]
        self.upstream_replies[self.dense_path] = dense_reply
        reranker = Mock(return_value=httpx.Response(200, json=fixture))

        response = self.request(settings=self.reranker_settings(reranker), payload=FULL_TURN)

        reranker.assert_called_once()
        emitted = self.read_events(response)
        retrieval = next(data["retrieval"] for kind, data in emitted if kind == "phase" and data["stage"] == "retrieve")
        scores = [entry["relevance_score"] for entry in fixture["results"]]
        self.assertEqual(retrieval["support_level"], "thin")
        self.assertEqual([entry["stage2_score"] for entry in retrieval["pool"]], sorted(scores, reverse=True))
        hit, = retrieval["final_set"]
        self.assertEqual(hit["stage2_score"], max(scores))
        self.assertEqual([data["chunk_id"] for kind, data in emitted if kind == "passage"], [hit["chunk_id"]])
        self.assertEqual((emitted[-1][0], emitted[-1][1]["end"]), ("answer", "complete"))
        (request,) = [request for request in self.engine_requests if request.url.path == COMPLETIONS_ROUTE]
        system = json.loads(request.content)["messages"][0]
        self.assertEqual(system, {"role": "system", "content": f"{TRACER_SYSTEM}\n\n{THIN_SUPPORT}"})

    def test_full_turn_streams_passages_phases_text_and_the_complete_answer_in_order(self) -> None:
        source = self.passage_doc["_source"]
        private_values = (source["caption"], source["text"])
        retrieval = self.request(private_values=private_values)
        final_set = retrieval.json()["final_set"]
        usage = {"prompt_tokens": 48, "completion_tokens": 23, "completion_tokens_details": {"reasoning_tokens": 5}}
        self.generator_stream = FixtureStream(with_finishing_usage(self.capture, usage))
        self.completions = [completion(self.generator_stream, content_type="text/event-stream; charset=utf-8")]
        clock_after_answer: list[int] = []

        def observe(message: Message) -> None:
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                self.audit_connect.assert_not_called()
                clock_after_answer.append(next(self.clock))

        response = self.request(payload=FULL_TURN, on_send=observe, private_values=private_values)
        emitted = self.read_events(response)
        source_count = len(final_set)
        self.assertGreater(source_count, 0)
        self.assertEqual(emitted[:source_count], [
            ("passage", {key: hit[key] for key in ("number", "chunk_id", "pool_rank")}) for hit in final_set
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
            "first_text": retrieval_phase["timings_ms"]["total"] + 2_000,
            "generation": 2000, "total": retrieval_phase["timings_ms"]["total"] + 3_000,
        })
        tick, = clock_after_answer
        self.assertGreater(tick * 1000, timings["total"])
        row = self.assert_answer_row(emitted, "complete")
        self.assertEqual(json.loads(row["pool"]), retrieval_phase["pool"])
        self.assertEqual(json.loads(row["evidence"]), final_set)
        self.assertEqual(row["generator_repository"], self.generator_listing["data"][0]["root"])
        self.assertEqual(json.loads(row["engine_counters"]), [{
            "thinking": True, "finish_reason": finish_reason,
            "prompt_tokens": 48, "completion_tokens": 23, "reasoning_tokens": 5,
        }])

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
            "docs": [
                {"_id": entry["chunk_id"], "_source": ["text", "caption", "doc_id", "token_count"]}
                for entry in retrieval_phase["pool"]
            ],
        })
        sampling = GENERATOR_SAMPLING[self.generator_listing["data"][0]["root"]]
        generation_request = json.loads(request.content)
        self.assertEqual(retrieval_phase["support_level"], "ok")
        self.assertEqual(generation_request["messages"][0], {"role": "system", "content": TRACER_SYSTEM})
        self.assertEqual(generation_request, {
            "model": self.settings.engine_model,
            "messages": messages(MESSAGE, [(source["caption"], source["text"])] * source_count, support_level="ok"),
            "max_tokens": RESEARCH_CONFIG["max_tokens"],
            **sampling.fields,
            "stream": True,
            "stream_options": {"include_usage": True, "continuous_usage_stats": True},
        })

    def test_length_with_visible_text_writes_one_truncated_row(self) -> None:
        text = "The fictitious harbor stayed open."
        stream = FixtureStream(sse(payload({"content": text}, finish_reason="length")))
        self.completions = [completion(stream)]
        emitted = self.read_events(self.request(payload=FULL_TURN))
        self.assertEqual(emitted[-1][1]["text"], text)
        self.assert_answer_row(emitted, "truncated")
        self.assertTrue(stream.closed)
        self.assertEqual(self.engine_paths(), [MODELS_ROUTE, COMPLETIONS_ROUTE])

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
                self.assert_failed_row("generator_unavailable", "generate")
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
                self.assert_failed_row("generator_outside_release", "generate")
                self.assertEqual(self.engine_paths(), [MODELS_ROUTE])

        trial_entry = {**entry, "id": trial.serve.served_name, "root": trial.repo}
        with patch.object(self, "listing_reply", {"data": [trial_entry]}):
            settings = replace(self.settings, engine_model=trial.serve.served_name)
            response = self.request(settings=settings, payload={"message": MESSAGE})
            self.assert_refused(response, 503, "generator_outside_release", "generate", pinned=True)
            self.assert_failed_row("generator_outside_release", "generate")
            self.assertEqual(self.engine_paths(), [MODELS_ROUTE])

    def assert_answer_row(self, emitted: list[tuple[str, dict[str, Any]]], end: str) -> dict[str, Any]:
        kind, body = emitted[-1]
        self.assertEqual((kind, body["end"]), ("answer", end))
        row = self.turn_row()
        self.assertEqual((row["outcome"], row["trip_family"]), (end, body.get("family")))
        self.assertEqual(json.loads(row["timings_ms"]), body["timings_ms"])
        self.assert_research_audit(row, sent_count=sum(kind == "passage" for kind, _ in emitted))
        return row

    def assert_stream_failure(
        self, emitted: list[tuple[str, dict[str, Any]]], state: str, *, word: str | None = "generate",
    ) -> dict[str, Any]:
        kind, body = emitted[-1]
        self.assertEqual(kind, "failure")
        self.assertEqual(set(body), {"error"})
        self.assertEqual((body["error"]["code"], body["error"]["param"]), (state, word))
        self.assertEqual([kind for kind, _ in emitted if kind in {"answer", "failure"}], ["failure"])
        line, = self.turn_logs
        self.assertIn(f"state={state} word={word or '-'} generation={self.generation}", line.getMessage())
        self.assertIsNone(line.exc_info)
        self.assertEqual(self.trips, [])
        return self.assert_failed_row(state, word, sent_count=sum(kind == "passage" for kind, _ in emitted))

    def assert_trip(self, emitted: list[tuple[str, dict[str, Any]]], expected: guardrail.Trip) -> dict[str, Any]:
        self.assertEqual([kind for kind, _ in emitted if kind in {"answer", "failure"}], ["answer"])
        text = "".join(body["text"] for kind, body in emitted if kind == "text")
        kind, answer = emitted[-1]
        self.assertEqual(kind, "answer")
        self.assertEqual((answer["end"], answer["family"], answer["text"]), ("tripped", expected.family, text))
        self.assertEqual(self.trips, [guardrail.TripRow(LEGAL_BRANCH, expected.family, expected.pattern_id, guardrail.EVAL_SOURCE, CHAT_ID)])
        return self.assert_answer_row(emitted, "tripped")

    def test_trip_closes_the_engine_before_sending_the_refusal_and_records_one_row(self) -> None:
        stream = FixtureStream(sse(payload({"content": OPEN_TEXT})) + sse(payload({"content": DEADLINE_TEXT}, finish_reason="stop")))
        self.completions = [completion(stream)]
        sent: list[tuple[Message, bool]] = []
        response = self.request(payload=FULL_TURN, on_send=lambda message: sent.append((message, stream.closed)), scrape=True)
        emitted = self.read_events(response)
        trip = guardrail.judge_rendered((DEADLINE_TEXT,), DEADLINE_TEXT)
        assert isinstance(trip, guardrail.Trip)
        row = self.assert_trip(emitted, trip)
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
        timings = json.loads(row["timings_ms"])
        self.assert_turn_metrics(row, (
            (measures.RETRIEVAL_METRIC, measures.RETRIEVAL_BUCKETS, timings["retrieval"]),
            (measures.FIRST_PARAGRAPH_METRIC, measures.ANSWER_BUCKETS, timings["first_text"]),
            (measures.COMPLETE_METRIC, measures.ANSWER_BUCKETS, timings["total"]),
        ))

    def test_length_without_text_retries_once_with_thinking_off_and_a_fresh_window(self) -> None:
        first = FixtureStream(with_finishing_usage(
            (FIXTURES / "generator-truncated.sse").read_bytes(),
            {"prompt_tokens": 51, "completion_tokens": 44, "completion_tokens_details": {"reasoning_tokens": 39}},
        ))
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
        row = self.assert_answer_row(emitted, "complete")
        self.assertEqual(json.loads(row["engine_counters"]), [
            {"thinking": True, "finish_reason": "length", "prompt_tokens": 51, "completion_tokens": 44, "reasoning_tokens": 39},
            {"thinking": False, "finish_reason": finish, "prompt_tokens": None, "completion_tokens": None, "reasoning_tokens": None},
        ])
        self.assertEqual(self.turn_logs, [])
        self.assertEqual(self.trips, [])

    def test_empty_stop_fails_on_either_attempt(self) -> None:
        truncated = (FIXTURES / "generator-truncated.sse").read_bytes()
        empty_stop = sse(payload({}, finish_reason="stop"))
        for attempts, bodies in ((1, (empty_stop,)), (2, (truncated, empty_stop))):
            with self.subTest(attempts=attempts):
                streams = [FixtureStream(body) for body in bodies]
                self.completions = [completion(stream) for stream in streams]
                emitted = self.read_events(self.request(payload=FULL_TURN, scrape=True))
                row = self.assert_stream_failure(emitted, "generation_empty")
                self.assertNotIn("text", [kind for kind, _ in emitted])
                self.assertEqual(self.engine_paths(), [MODELS_ROUTE, *([COMPLETIONS_ROUTE] * attempts)])
                self.assertTrue(all(stream.closed for stream in streams))
                timings = json.loads(row["timings_ms"])
                self.assert_turn_metrics(row, ((measures.RETRIEVAL_METRIC, measures.RETRIEVAL_BUCKETS, timings["retrieval"]),))

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
                emitted = self.read_events(self.request(payload=FULL_TURN, scrape=True))
                row = self.assert_stream_failure(emitted, "generator_reply_invalid")
                self.assertEqual(emitted[-2], ("text", {"text": text}))
                self.assertTrue(stream.closed)
                timings = json.loads(row["timings_ms"])
                self.assert_turn_metrics(row, (
                    (measures.RETRIEVAL_METRIC, measures.RETRIEVAL_BUCKETS, timings["retrieval"]),
                    (measures.FIRST_PARAGRAPH_METRIC, measures.ANSWER_BUCKETS, timings["first_text"]),
                ))

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

    def test_failed_answer_send_writes_disconnected_while_receive_is_pending(self) -> None:
        async def run() -> None:
            session = ASGISession(
                self.app(self.settings), "POST", "/turn", json.dumps(FULL_TURN).encode(), self.headers(self.settings), "2.4",
            )
            watching = asyncio.Event()
            failed_sends: list[Message] = []

            async def receive() -> Message:
                if session.incoming.empty():
                    watching.set()
                return await session.incoming.get()

            async def send(message: Message) -> None:
                if message.get("body", b"").startswith(b"event: answer\n"):
                    await watching.wait()
                    failed_sends.append(message)
                    raise OSError("Fictitious closed transport")

            async with session.app.router.lifespan_context(session.app):
                await asyncio.wait_for(session.app(session.scope, receive, send), timeout=1)

            self.assertEqual(len(failed_sends), 1)
            self.assertTrue(self.generator_stream.closed)

        asyncio.run(run())
        self.assertEqual(self.turn_row()["outcome"], "disconnected")

    def test_hang_up_during_a_passage_send_keeps_only_the_delivered_prefix(self) -> None:
        async def run(hang_up_at: int) -> None:
            session = ASGISession(
                self.app(self.settings), "POST", "/turn", json.dumps(FULL_TURN).encode(), self.headers(self.settings), "2.4",
            )
            attempted: list[str] = []

            async def send(message: Message) -> None:
                event = message.get("body", b"")
                if event.startswith(b"event: passage\n"):
                    data = event.splitlines()[1].removeprefix(b"data: ")
                    attempted.append(json.loads(data)["chunk_id"])
                    if len(attempted) == hang_up_at:
                        session.disconnect()
                        await asyncio.Future()

            async with session.app.router.lifespan_context(session.app):
                await asyncio.wait_for(session.app(session.scope, session.incoming.get, send), timeout=1)
            row = self.turn_row()
            self.assertEqual(row["outcome"], "disconnected")
            evidence = json.loads(row["evidence"])
            self.assertEqual(len(attempted), hang_up_at)
            self.assertEqual(attempted, [hit["chunk_id"] for hit in evidence[:hang_up_at]])
            self.assert_research_audit(row, sent_count=hang_up_at - 1)

        for hang_up_at in (1, 2):
            with self.subTest(hang_up_at=hang_up_at):
                asyncio.run(run(hang_up_at))

    def test_hang_up_cancels_the_stream_and_waits_for_shielded_close(self) -> None:
        async def run(spec_version: str) -> None:
            stream = WaitingStream(sse(with_usage(
                payload({"content": OPEN_TEXT}), {"prompt_tokens": 48, "completion_tokens": 16},
            )))
            self.completions = [completion(stream)]
            session = ASGISession(
                self.app(self.settings), "POST", "/turn", json.dumps(FULL_TURN).encode(),
                self.headers(self.settings), spec_version,
            )
            async with session, httpx.AsyncClient(
                transport=httpx.ASGITransport(app=session.app), base_url="http://fixture-api",
            ) as client:
                self.metrics_before = await scrape_metrics(client)
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
                self.metrics_after = await scrape_metrics(client)
                self.assertTrue(stream.closed)
                self.assertTrue(session.outgoing.empty())
                row = self.turn_row()
                self.assertEqual(row["outcome"], "disconnected")
                self.assertEqual(json.loads(row["engine_counters"]), [{
                    "thinking": True, "finish_reason": None,
                    "prompt_tokens": 48, "completion_tokens": 16, "reasoning_tokens": None,
                }])
                timings = json.loads(row["timings_ms"])
                self.assert_turn_metrics(row, (
                    (measures.RETRIEVAL_METRIC, measures.RETRIEVAL_BUCKETS, timings["retrieval"]),
                    (measures.FIRST_PARAGRAPH_METRIC, measures.ANSWER_BUCKETS, timings["first_text"]),
                ))

        for spec_version in ("2.0", "2.4"):
            with self.subTest(spec_version=spec_version):
                asyncio.run(run(spec_version))
                self.assertEqual(self.trips, [])
