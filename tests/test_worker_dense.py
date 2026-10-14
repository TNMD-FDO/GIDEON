"""Dense projections, transactional embedding records, and checked HTTP adapters."""

import copy
import fcntl
import hashlib
import io
import json
import logging
import math
import tempfile
import threading
import unittest
import uuid
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, astuple, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import httpx
import psycopg
from test_worker_caselaw import _CapturedConnection
from test_worker_lexical import (
    GEO,
    NAME,
    NOW,
    TEXT,
    StreamConnection,
    chunk_for,
    configuration,
    inputs_for,
    opinion_for,
)

from gideon.host import cas
from gideon.worker import chunker, dense, lexical, logs, treatment

WIDTH = 3
SERVED = "fictitious-embedding-model"


def info_for(status: str = "green", indexed: int = 7) -> dict[str, Any]:
    return {
        "status": status, "optimizer_status": "ok", "points_count": 9,
        "indexed_vectors_count": indexed,
        "config": {
            "params": {"vectors": {"size": WIDTH, **dense.COLLECTION_CONFIG["vectors"]}},
            "hnsw_config": {**dense.COLLECTION_CONFIG["hnsw_config"], "full_scan_threshold": 17},
            "quantization_config": {
                "scalar": {**dense.COLLECTION_CONFIG["quantization_config"]["scalar"], "quantile": 0.9},
            },
            "optimizer_config": {"indexing_threshold": dense.SHIPPED_INDEXING_THRESHOLD},
        },
        "payload_schema": {},
    }


def response(result: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=json.dumps({"status": "ok", "result": result}))


def answer(value: object, status: int = 200, *, envelope: bool = True) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        if envelope:
            return response(value, status)
        return httpx.Response(status, content=json.dumps(value))

    return respond


class Projections(unittest.TestCase):
    def test_point_id_is_stable_canonical_and_uses_only_the_first_half(self) -> None:
        chunk = chunk_for()
        expected = str(uuid.UUID(hex=chunk.chunk_id[:32]))
        self.assertEqual(dense.point_id(chunk.chunk_id), expected)
        self.assertEqual(dense.point_id(chunk.chunk_id[:32] + "f" * 32), expected)
        other = replace(chunk, chunk_id="abcdef01" * 8)
        self.assertEqual(dense.point_id(other.chunk_id), str(uuid.UUID(hex=other.chunk_id[:32])))
        self.assertNotEqual(dense.point_id(other.chunk_id), expected)
        self.assertEqual(str(uuid.UUID(expected)), expected)

    def test_payload_has_only_record_filter_keys_and_omits_an_absent_circuit(self) -> None:
        chunk, opinion = chunk_for(), opinion_for()
        payload = dense.payload_for(chunk, opinion, GEO)
        self.assertEqual(payload, {
            "chunk_id": chunk.chunk_id, "court": opinion.court,
            "circuit": GEO[opinion.court].circuit,
            "decided_date": datetime.combine(opinion.decided_date, datetime.min.time(), UTC).isoformat(),
            "precedential": opinion.precedential, "section_type": chunk.section_type,
        })
        geography = {opinion.court: treatment.Geography("state", None, "fictional-state")}
        self.assertEqual(dense.payload_for(chunk, opinion, geography), {key: value for key, value in payload.items() if key != "circuit"})
        for sentinel in (TEXT, opinion.docket, *opinion.reporter_cites):
            self.assertNotIn(sentinel or "absent-caption", str(payload))

    def test_grouping_preserves_representatives_and_all_duplicate_chunks(self) -> None:
        first = chunk_for()
        duplicate = replace(first, chunk_id="2" * 64, doc_id="d" * 64)
        different = chunk_for(3)
        groups = dense.group_by_hash(((first, TEXT[2:]), (different, TEXT[3:]), (duplicate, TEXT[2:])))
        self.assertEqual(groups, (
            dense.TextGroup(TEXT[2:], (first, duplicate)), dense.TextGroup(TEXT[3:], (different,)),
        ))
        self.assertEqual(dense.group_by_hash(()), ())
        self.assertEqual(lexical.slice_for(TEXT, first), groups[0].text)

    def test_packing_keeps_order_and_obeys_both_bounds(self) -> None:
        groups = tuple(dense.TextGroup(f"fictitious-text-{index}", (replace(chunk_for(), token_count=1),))
                       for index in range(dense.EMBED_BATCH_TEXTS + 1))
        packed = dense.pack_requests(groups)
        self.assertEqual(packed, (groups[:dense.EMBED_BATCH_TEXTS], groups[dense.EMBED_BATCH_TEXTS:]))
        token_groups = tuple(dense.TextGroup(f"fictitious-text-{index}", (replace(chunk_for(), token_count=count),))
                             for index, count in enumerate((4, 6, 1)))
        with patch.object(dense, "EMBED_BATCH_TOKENS", 10):
            self.assertEqual(dense.pack_requests(token_groups), (token_groups[:2], token_groups[2:]))
        self.assertEqual(dense.pack_requests(()), ())
        at_cap = dense.TextGroup("fictitious-long-text", (replace(chunk_for(), token_count=dense.EMBED_TEXT_TOKEN_CAP),))
        self.assertEqual(dense.pack_requests((at_cap,)), ((at_cap,),))
        over_cap = replace(at_cap, chunks=(replace(at_cap.chunks[0], token_count=dense.EMBED_TEXT_TOKEN_CAP + 1),))
        with self.assertRaises(dense.DenseFailure) as raised:
            dense.pack_requests((*groups, over_cap))
        self.assertEqual(raised.exception.reason, "chunk-too-long")

    def test_config_compare_checks_only_release_keys(self) -> None:
        config = info_for()["config"]
        self.assertTrue(dense.config_matches(config, WIDTH))
        config["optimizer_config"]["indexing_threshold"] = dense.FILL_INDEXING_THRESHOLD
        self.assertTrue(dense.config_matches(config, WIDTH))
        for path, value in (
            (("params", "vectors", "size"), WIDTH + 1),
            (("params", "vectors", "distance"), "Euclid"),
            (("params", "vectors", "datatype"), "float32"),
            (("params", "vectors", "on_disk"), False),
            (("hnsw_config", "m"), dense.COLLECTION_CONFIG["hnsw_config"]["m"] + 1),
            (("hnsw_config", "ef_construct"), 1),
            (("quantization_config", "scalar", "type"), "int4"),
            (("quantization_config", "scalar", "always_ram"), False),
        ):
            changed = copy.deepcopy(config)
            target = changed
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path):
                self.assertFalse(dense.config_matches(changed, WIDTH))
        self.assertFalse(dense.config_matches({}, WIDTH))
        config["params"]["vectors"]["on_disk"] = 1
        self.assertFalse(dense.config_matches(config, WIDTH))

    def test_counts_fields_and_failures_follow_the_closed_contract(self) -> None:
        self.assertEqual(tuple(field.name for field in fields(dense.DenseCounts)), dense.DENSE_COUNT_KEYS)
        self.assertTrue(all(type(value) is int for value in asdict(dense.DenseCounts()).values()))
        for reason in dense.DENSE_FAILURE_REASONS:
            failure = dense.DenseFailure(reason, error="FICTITIOUS_PRIVATE_DETAIL")
            self.assertEqual(failure.reason, reason)
            self.assertEqual(str(failure), reason)
        with self.assertRaises(ValueError):
            dense.DenseFailure("fictitious-unknown-reason")
        dense.NoBackOff().wait_before()


class LedgerConnection(_CapturedConnection):
    def __init__(self) -> None:
        super().__init__()
        self.fail_statement: str | None = None

    def execute(self, statement: str, parameters: tuple[object, ...]) -> None:
        super().execute(statement, parameters)
        if statement == self.fail_statement:
            raise psycopg.OperationalError("FICTITIOUS_PRIVATE_DETAIL")


class RecordAdapter(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        password = Path(temporary.name) / "password"
        password.write_text("fictitious-secret", encoding="utf-8")
        self.writer, self.stream = LedgerConnection(), StreamConnection()
        connections = iter((self.writer, self.stream))
        self.calls: list[dict[str, object]] = []

        def connect(**kwargs: object):
            self.calls.append(kwargs)
            return next(connections)

        self.record = dense.PsycopgGenerations(connect=connect, worker_settings=configuration(password))
        self.addCleanup(self.record.close)
        chunk = chunk_for()
        self.row = dense.LedgerRow(chunk.content_hash, NAME, dense.point_id(chunk.chunk_id))

    def test_record_inherits_the_existing_reads_state_and_cleanup(self) -> None:
        for name in ("read", "resume", "fail", "chunks", "opinion", "close"):
            with self.subTest(name=name):
                self.assertIs(getattr(dense.PsycopgGenerations, name), getattr(lexical.PsycopgGenerations, name))
        generation = lexical.Generation(NAME, "caselaw", "e" * 64, "1.2.3", "fictitious-space", "building")
        self.writer.rows = [astuple(generation)]
        self.assertEqual(self.record.read(NAME), generation)
        self.writer.rows = [astuple(chunk_for())]
        self.assertEqual(self.record.chunks(chunk_for().doc_id, chunk_for().pipeline), (chunk_for(),))
        self.writer.rows = [astuple(opinion_for())]
        self.assertEqual(self.record.opinion(opinion_for().doc_id), opinion_for())
        self.record.resume(NAME)
        self.record.fail(NAME, "embed")
        self.assertEqual(self.writer.statements[-1], (lexical.FAIL_SQL, ("embed", NAME)))

    def test_scope_count_and_documents_bind_the_same_document_limit_before_chunks(self) -> None:
        for limit in (None, 3):
            with self.subTest(limit=limit):
                self.writer.rows = [(19,)]
                self.assertEqual(self.record.scope_total("caselaw", "fictions", NOW.date(), ("court1",), "1.2.3", limit), 19)
                self.assertEqual(self.writer.statements[-1], (dense.SCOPE_TOTAL_SQL, ("caselaw", "fictions", NOW.date(), ["court1"], limit, "1.2.3")))
                self.assertEqual(dense.SCOPE_TOTAL_SQL.count("COUNT("), 1)
                self.assertIn("JOIN (" + lexical.DOCUMENTS_SQL + " LIMIT %s) AS scope", dense.SCOPE_TOTAL_SQL)
                self.assertTrue(dense.SCOPE_TOTAL_SQL.endswith("WHERE c.pipeline = %s"))
        self.stream.rows = [("a" * 64, "b" * 64)]
        self.assertEqual(list(self.record.documents("caselaw", "fictions", NOW.date(), ("court1",), 3)), [lexical.Document("a" * 64, "b" * 64)])
        self.assertEqual(self.stream.statements, [(dense.DOCUMENTS_SQL, ("caselaw", "fictions", NOW.date(), ["court1"], 3))])
        self.assertEqual(self.stream.names, ["dense_documents"])
        self.assertEqual(self.stream.itersize, dense.DOCUMENT_BATCH_SIZE)

    def test_ledger_read_insert_and_rehome_bind_space_hash_and_canonical_uuid(self) -> None:
        self.writer.rows = [(self.row.content_hash, self.row.gen_id, uuid.UUID(self.row.point_id))]
        self.assertEqual(self.record.ledger_rows("fictitious-space", (self.row.content_hash,)), (self.row,))
        self.assertEqual(self.writer.statements[-1], (dense.LEDGER_ROWS_SQL, ("fictitious-space", [self.row.content_hash])))
        self.assertIn("embedding_space = %s AND content_hash = ANY(%s)", dense.LEDGER_ROWS_SQL)
        self.record.progress(NAME, dense.DenseCounts(), (self.row,), (self.row,))
        self.assertEqual(self.writer.statements[-3], (dense.LEDGER_INSERT_SQL, (self.row.content_hash, self.row.point_id, self.row.gen_id)))
        self.assertIn("ON CONFLICT DO NOTHING", dense.LEDGER_INSERT_SQL)
        self.assertIn("embedding_space, gen_id", dense.LEDGER_INSERT_SQL)
        self.assertEqual(self.writer.statements[-2], (dense.LEDGER_REHOME_SQL, (self.row.gen_id, self.row.point_id, self.row.content_hash, self.row.gen_id)))
        self.assertIn("embedded_at = now()", dense.LEDGER_REHOME_SQL)
        self.assertIn("WHERE content_hash = %s AND embedding_space =", dense.LEDGER_REHOME_SQL)
        self.assertEqual(self.writer.statements[-1][0], dense.PROGRESS_SQL)
        self.assertEqual(self.writer.commits, 2)

    def test_start_clears_all_dense_keys_and_keeps_the_lexical_counts(self) -> None:
        counts = dense.DenseCounts(dense_total=19, dense_scope=7, dense_limit=3)
        self.record.start(NAME, counts)
        sql, parameters = self.writer.statements[-1]
        self.assertEqual(sql, dense.START_SQL)
        self.assertIn("(counts - %s::text[]) || %s::jsonb", sql)
        self.assertEqual(parameters[0], list(dense.DENSE_COUNT_KEYS))
        payload = json.loads(cast(str, parameters[1]))
        self.assertEqual(payload, asdict(counts))
        self.assertEqual(payload["dense_hour_seconds"], 0)
        self.assertEqual(asdict(dense.DenseCounts())["dense_limit"], 0)
        self.assertEqual(parameters[-1], NAME)

    def test_progress_commits_ledger_and_counts_once_without_committing_the_stream(self) -> None:
        self.record.start(NAME, dense.DenseCounts())
        self.stream.rows = [("a" * 64, "b" * 64), ("c" * 64, "d" * 64)]
        documents = self.record.documents("caselaw", "fictions", NOW.date(), ("court1",))
        next(documents)
        baseline = self.writer.commits
        counts = dense.DenseCounts(dense_embedded=7, dense_tokens=91)
        self.record.progress(NAME, counts, (self.row, replace(self.row, content_hash="c" * 64)))
        self.assertEqual(self.writer.commits, baseline + 1)
        self.assertEqual(self.stream.commits, 0)
        self.assertEqual([sql for sql, _parameters in self.writer.statements[-3:]], [dense.LEDGER_INSERT_SQL, dense.LEDGER_INSERT_SQL, dense.PROGRESS_SQL])
        self.assertEqual(json.loads(cast(str, self.writer.statements[-1][1][0])), asdict(counts))
        self.assertEqual(len(list(documents)), 1)
        self.assertEqual(self.stream.commits, 1)
        self.assertEqual(len(self.calls), 2)

    def test_progress_rehomes_in_the_same_transaction_as_inserts_and_counts(self) -> None:
        copied = replace(self.row, content_hash="f" * 64)
        self.record.progress(NAME, dense.DenseCounts(dense_copied=1), (self.row,), (copied,))
        self.assertEqual([sql for sql, _parameters in self.writer.statements], [
            dense.LEDGER_INSERT_SQL, dense.LEDGER_REHOME_SQL, dense.PROGRESS_SQL,
        ])
        self.assertEqual(self.writer.commits, 1)
        self.writer.fail_statement = dense.PROGRESS_SQL
        with self.assertRaises(dense.DenseFailure):
            self.record.progress(NAME, dense.DenseCounts(), (self.row,), (copied,))
        self.assertEqual(self.writer.commits, 1)
        self.assertEqual(self.writer.rollbacks, 1)

    def test_limited_finish_merges_only_counts_and_full_finish_completes_both_halves(self) -> None:
        counts = dense.DenseCounts(dense_limit=3)
        self.record.finish(NAME, counts, NOW, True)
        self.assertEqual(self.writer.statements[-1], (dense.PROGRESS_SQL, (json.dumps(asdict(counts)), NAME)))
        unlimited = replace(counts, dense_limit=0)
        self.record.finish(NAME, unlimited, NOW, False)
        self.assertEqual(self.writer.statements[-1], (dense.FINISH_SQL, (json.dumps(asdict(unlimited)), NOW, NOW, NAME)))
        self.assertIn("lexical_built_at IS NOT NULL THEN COALESCE(built_at, %s) ELSE NULL", dense.FINISH_SQL)
        self.assertIn("state = 'building' AND lexical_built_at IS NOT NULL THEN 'built' ELSE state", dense.FINISH_SQL)
        self.assertNotIn("bytes =", dense.FINISH_SQL)

    def test_failure_after_ledger_insert_rolls_back_without_committing_counts(self) -> None:
        self.writer.fail_statement = dense.PROGRESS_SQL
        with self.assertRaises(dense.DenseFailure) as raised:
            self.record.progress(NAME, dense.DenseCounts(), (self.row,))
        self.assertEqual((raised.exception.reason, raised.exception.error), ("database", "OperationalError"))
        self.assertEqual(self.writer.commits, 0)
        self.assertEqual(self.writer.rollbacks, 1)
        self.assertEqual([sql for sql, _parameters in self.writer.statements], [dense.LEDGER_INSERT_SQL, dense.PROGRESS_SQL])
        self.assertNotIn("FICTITIOUS_PRIVATE_DETAIL", str(raised.exception))

    def test_commit_failure_and_reused_database_failure_are_safe(self) -> None:
        self.writer.fail_commit = True
        with self.assertRaises(dense.DenseFailure):
            self.record.progress(NAME, dense.DenseCounts(), (self.row,))
        self.assertEqual(self.writer.rollbacks, 1)
        self.writer.fail_execute = True
        with self.assertRaises(lexical.LexicalFailure) as raised:
            self.record.read(NAME)
        self.assertEqual(raised.exception.reason, "database")


class HttpAdapters(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        password = Path(temporary.name) / "password"
        password.write_text("fictitious-database-password", encoding="utf-8")
        self.vector_key = Path(temporary.name) / "vector-key"
        self.embed_key = Path(temporary.name) / "embed-key"
        self.vector_key.write_text("fictitious-vector-key", encoding="utf-8")
        self.embed_key.write_text("fictitious-embed-key", encoding="utf-8")
        self.configuration = replace(configuration(password), qdrant_key_file=self.vector_key, embed_key_file=self.embed_key)
        self.requests: list[httpx.Request] = []
        self.waits: list[float] = []
        self.clock = 0.0

    def pause(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.clock += seconds

    def transport(self, handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
        def respond(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        return httpx.MockTransport(respond)

    def store(self, handler: Callable[[httpx.Request], httpx.Response]) -> dense.HttpxVectorStore:
        store = dense.HttpxVectorStore(self.configuration, transport=self.transport(handler), sleep=self.pause, monotonic=lambda: self.clock)
        self.addCleanup(store.close)
        return store

    def embedder(self, handler: Callable[[httpx.Request], httpx.Response]) -> dense.HttpxEmbedder:
        embedder = dense.HttpxEmbedder(SERVED, WIDTH, self.configuration, transport=self.transport(handler), sleep=self.pause)
        self.addCleanup(embedder.close)
        return embedder

    def test_store_requests_auth_config_optimizer_and_awaited_operations(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return response(info_for())
            if request.url.path.endswith(("/index", "/points")):
                return response({"status": "completed"})
            return response(True)

        store = self.store(handler)
        self.assertEqual(store.info(NAME), info_for())
        store.create(NAME, WIDTH)
        self.assertEqual(json.loads(self.requests[-1].content), {**dense.COLLECTION_CONFIG, "vectors": {"size": WIDTH, **dense.COLLECTION_CONFIG["vectors"]}})
        store.ensure_indexes(NAME)
        indexes = self.requests[2:]
        self.assertEqual([json.loads(request.content) for request in indexes], [
            {"field_name": name, "field_schema": schema} for name, schema in dense.PAYLOAD_INDEXES.items()
        ])
        self.assertTrue(all(request.url.params["wait"] == "true" for request in indexes))
        store.set_indexing(NAME, dense.FILL_INDEXING_THRESHOLD)
        self.assertEqual(self.requests[-1].method, "PATCH")
        self.assertEqual(json.loads(self.requests[-1].content), {"optimizers_config": {"indexing_threshold": dense.FILL_INDEXING_THRESHOLD}})
        point = dense.Point(dense.point_id(chunk_for().chunk_id), (0.1, 0.2, 0.3), dense.payload_for(chunk_for(), opinion_for(), GEO))
        store.upsert(NAME, (point,))
        self.assertEqual(self.requests[-1].url.params["wait"], "true")
        self.assertEqual(json.loads(self.requests[-1].content), json.loads(json.dumps({"points": [asdict(point)]})))
        for request in self.requests:
            self.assertEqual(request.headers["api-key"], self.vector_key.read_text())
            self.assertEqual(request.url.host, "vectors.example.test")
            self.assertEqual(request.extensions["timeout"], dense.HTTP_TIMEOUT.as_dict())
            self.assertNotIn("Authorization", request.headers)

    def test_new_clients_reread_their_own_keys_and_missing_keys_fail_local(self) -> None:
        first_store = self.store(lambda request: response(None, 404))
        first_store.info(NAME)
        first_key = self.requests[-1].headers["api-key"]
        self.vector_key.write_text("fictitious-rotated-vector-key", encoding="utf-8")
        self.store(lambda request: response(None, 404)).info(NAME)
        self.assertNotEqual(first_key, self.requests[-1].headers["api-key"])
        self.assertEqual(self.requests[-1].headers["api-key"], self.vector_key.read_text())
        self.embed_key.write_text("fictitious-rotated-embed-key", encoding="utf-8")
        self.embedder(lambda request: self.embedding_reply()).embed((TEXT,))
        self.assertEqual(self.requests[-1].headers["Authorization"], f"Bearer {self.embed_key.read_text()}")
        for path, operation in ((self.vector_key, lambda: dense.HttpxVectorStore(self.configuration)),
                                (self.embed_key, lambda: dense.HttpxEmbedder(SERVED, WIDTH, self.configuration))):
            path.unlink()
            with self.assertRaises(dense.DenseFailure) as raised:
                operation()
            self.assertEqual(raised.exception.reason, "local")
            path.write_text("fictitious-restored-key", encoding="utf-8")

    def test_missing_collection_is_absent_for_info_and_retrieve_but_not_presence(self) -> None:
        store = self.store(lambda request: httpx.Response(404, text="FICTITIOUS_PRIVATE_DETAIL"))
        identifier = dense.point_id(chunk_for().chunk_id)
        self.assertIsNone(store.info(NAME))
        self.assertEqual(store.retrieve(NAME, (identifier,)), {})
        with self.assertRaises(dense.DenseFailure) as raised:
            store.present(NAME, (identifier,))
        self.assertEqual((raised.exception.reason, raised.exception.error), ("store", "HttpStatus4xx"))

    def test_point_lookups_are_bounded_unordered_and_canonical(self) -> None:
        identifiers = [str(uuid.UUID(int=index + 1)) for index in range(dense.PRESENCE_BATCH + 1)]

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.assertIs(body["with_payload"], False)
            bound = dense.RETRIEVE_BATCH if body["with_vector"] else dense.PRESENCE_BATCH
            self.assertLessEqual(len(body["ids"]), bound)
            return response([{"id": identifier.upper(), "vector": [0.1, 0.2, 0.3]} for identifier in reversed(body["ids"])])

        store = self.store(handler)
        self.assertEqual(store.present(NAME, identifiers), set(identifiers))
        self.assertEqual(len(self.requests), math.ceil(len(identifiers) / dense.PRESENCE_BATCH))
        self.requests.clear()
        vectors = store.retrieve(NAME, identifiers)
        self.assertEqual(vectors, dict.fromkeys(identifiers, (0.1, 0.2, 0.3)))
        self.assertEqual(len(self.requests), math.ceil(len(identifiers) / dense.RETRIEVE_BATCH))
        self.requests.clear()
        self.assertEqual(store.present(NAME, ()), set())
        self.assertEqual(store.retrieve(NAME, ()), {})
        self.assertFalse(self.requests)

    def test_missing_points_are_omitted_and_dropped_home_discards_partial_results(self) -> None:
        identifiers = [str(uuid.UUID(int=index + 1)) for index in range(dense.RETRIEVE_BATCH + 1)]
        store = self.store(lambda request: response([]))
        self.assertEqual(store.present(NAME, identifiers), set())
        self.assertEqual(store.retrieve(NAME, identifiers), {})
        replies = iter((response([{"id": identifiers[0], "vector": [0.1, 0.2, 0.3]}]), httpx.Response(404)))
        self.assertEqual(self.store(lambda request: next(replies)).retrieve(NAME, identifiers), {})

    def test_invalid_points_and_vectors_refuse(self) -> None:
        identifier = dense.point_id(chunk_for().chunk_id)
        results: tuple[object, ...] = (
            None, {}, [None], [{"id": 17}], [{"id": "invalid"}],
            [{"id": str(uuid.UUID(int=29))}], [{"id": identifier}] * 2,
        )
        for result in results:
            with self.subTest(result=result), self.assertRaises(dense.DenseFailure):
                self.store(answer(result)).present(NAME, (identifier,))
        vectors: tuple[object, ...] = (None, [], {}, [True], ["private"], [float("inf")], [float("nan")])
        for vector in vectors:
            with self.subTest(vector=vector), self.assertRaises(dense.DenseFailure):
                self.store(answer([{"id": identifier, "vector": vector}])).retrieve(NAME, (identifier,))

    def test_store_retries_only_pressure_and_exposes_status_classes(self) -> None:
        statuses = iter((429, 503, 200))
        self.store(lambda request: response(True, next(statuses))).create(NAME, WIDTH)
        self.assertEqual(self.waits, list(dense.RETRY_WAITS[:2]))
        for status in (429, 503, 301, 400, 401, 500):
            self.requests.clear()
            with self.subTest(status=status), self.assertRaises(dense.DenseFailure) as raised:
                self.store(answer("FICTITIOUS_PRIVATE_DETAIL", status, envelope=False)).create(NAME, WIDTH)
            self.assertEqual(raised.exception.error, f"HttpStatus{status // 100}xx")
            self.assertEqual(len(self.requests), len(dense.RETRY_WAITS) + 1 if status in (429, 503) else 1)
            self.assertNotIn("FICTITIOUS_PRIVATE_DETAIL", str(raised.exception))
        self.requests.clear()

        def disconnected(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("FICTITIOUS_PRIVATE_DETAIL", request=request)

        with self.assertRaises(dense.DenseFailure) as raised:
            self.store(disconnected).create(NAME, WIDTH)
        self.assertEqual((raised.exception.reason, raised.exception.error), ("store", "ConnectError"))
        self.assertEqual(len(self.requests), 1)

    def test_store_operation_shapes_refuse_false_unawaited_or_invalid_replies(self) -> None:
        for result in (False, 1, None, {"status": "completed"}):
            store = self.store(answer(result))
            with self.subTest(result=result), self.assertRaises(dense.DenseFailure):
                store.create(NAME, WIDTH)
            with self.subTest(result=result), self.assertRaises(dense.DenseFailure):
                store.set_indexing(NAME, 17)
        for result in (True, None, {}, {"status": "acknowledged"}, {"status": "failed"}):
            store = self.store(answer(result))
            with self.subTest(result=result), self.assertRaises(dense.DenseFailure):
                store.ensure_indexes(NAME)
            with self.subTest(result=result), self.assertRaises(dense.DenseFailure):
                store.upsert(NAME, ())
        values: tuple[object, ...] = ([], {"status": "error", "result": True}, {"status": "ok"})
        for value in values:
            with self.subTest(value=value), self.assertRaises(dense.DenseFailure):
                self.store(answer(value, envelope=False)).create(NAME, WIDTH)
        with self.assertRaises(dense.DenseFailure):
            self.store(lambda request: httpx.Response(200, text="FICTITIOUS_INVALID_REPLY")).create(NAME, WIDTH)

    def test_info_checks_counts_config_schema_and_optimizer_shapes(self) -> None:
        for key, value in (
            ("status", "unknown"), ("optimizer_status", None),
            ("points_count", True), ("points_count", -1), ("indexed_vectors_count", "9"),
            ("config", []), ("config", {}), ("payload_schema", []),
        ):
            result = {**info_for(), key: value}
            with self.subTest(key=key, value=value), self.assertRaises(dense.DenseFailure):
                self.store(answer(result)).info(NAME)
        for key in info_for():
            result = {k: v for k, v in info_for().items() if k != key}
            with self.subTest(missing=key), self.assertRaises(dense.DenseFailure):
                self.store(answer(result)).info(NAME)

    def test_valid_default_config_is_reported_for_the_build_to_compare(self) -> None:
        result = info_for()
        result["config"]["params"]["vectors"]["datatype"] = None
        result["config"]["params"]["vectors"]["on_disk"] = None
        result["config"]["quantization_config"] = None
        info = self.store(answer(result)).info(NAME)
        self.assertEqual(info, result)
        assert info is not None
        self.assertFalse(dense.config_matches(info["config"], WIDTH))

    def test_wait_green_ignores_a_transient_green_and_requires_consecutive_polls(self) -> None:
        states = iter(("green", "yellow", "green", "green"))
        store = self.store(lambda request: response(info_for(next(states))))
        progress: list[int] = []
        self.assertEqual(store.wait_green(NAME, progress.append)["status"], "green")
        self.assertEqual(self.waits, [dense.POLL_SECONDS] * 3)
        self.assertFalse(progress)

    def test_wait_green_triggers_grey_once_a_minute_and_reports_indexed_progress(self) -> None:
        triggers: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "PATCH":
                triggers.append(self.clock)
                self.assertEqual(json.loads(request.content), {"optimizers_config": {}})
                return response(True)
            return response(info_for("grey" if self.clock < dense.LOG_SECONDS * 2 else "green", int(self.clock)))

        progress: list[int] = []
        result = self.store(handler).wait_green(NAME, progress.append)
        self.assertEqual(triggers, [0.0, dense.GREY_TRIGGER_SECONDS])
        self.assertEqual(progress, [int(dense.LOG_SECONDS), int(dense.LOG_SECONDS * 2)])
        self.assertEqual(result["status"], "green")
        for info in (None, info_for("red"), {**info_for(), "optimizer_status": {"error": "FICTITIOUS_PRIVATE_DETAIL"}}):
            with self.subTest(info=info), self.assertRaises(dense.DenseFailure):
                self.store(answer(info, 404 if info is None else 200)).wait_green(NAME, progress.append)

    def embedding_reply(self) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}], "usage": {"prompt_tokens": 17}})

    def test_embed_body_auth_timeout_order_width_and_tokens(self) -> None:
        vectors = ((0.1, 0.2, 0.3), (0.4, 0.5, 0.6))

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"index": index, "embedding": vector} for index, vector in enumerate(vectors)], "usage": {"prompt_tokens": 31}})

        embedder = self.embedder(handler)
        texts = (TEXT, "FICTITIOUS_SECOND_TEXT")
        self.assertEqual(embedder.embed(texts), dense.EmbedReply(vectors, 31))
        request = self.requests[-1]
        self.assertEqual(request.url.path, "/v1/embeddings")
        self.assertEqual(request.url.host, "embed.example.test")
        self.assertEqual(json.loads(request.content), {"model": SERVED, "input": list(texts)})
        self.assertEqual(request.headers["Authorization"], f"Bearer {self.embed_key.read_text()}")
        self.assertNotIn("api-key", request.headers)
        self.assertEqual(request.extensions["timeout"], dense.EMBED_TIMEOUT.as_dict())
        self.assertEqual(embedder.embed(()), dense.EmbedReply((), 0))
        self.assertEqual(len(self.requests), 1)

    def test_embed_retries_transport_and_pressure_but_never_other_statuses(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ReadTimeout("FICTITIOUS_PRIVATE_DETAIL", request=request)
            if attempts == 2:
                return httpx.Response(503)
            return self.embedding_reply()

        self.embedder(handler).embed((TEXT,))
        self.assertEqual(self.waits, list(dense.EMBED_RETRY_WAITS[:2]))
        for status in (429, 503, 301, 400, 401, 500):
            self.requests.clear()
            with self.subTest(status=status), self.assertRaises(dense.DenseFailure) as raised:
                self.embedder(answer("FICTITIOUS_PRIVATE_DETAIL", status, envelope=False)).embed((TEXT,))
            self.assertEqual((raised.exception.reason, raised.exception.error), ("embed", f"HttpStatus{status // 100}xx"))
            self.assertEqual(len(self.requests), len(dense.EMBED_RETRY_WAITS) + 1 if status in (429, 503) else 1)
        self.requests.clear()

        def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("FICTITIOUS_PRIVATE_DETAIL", request=request)

        with self.assertRaises(dense.DenseFailure) as raised:
            self.embedder(broken).embed((TEXT,))
        self.assertEqual(raised.exception.error, "ConnectError")
        self.assertEqual(len(self.requests), len(dense.EMBED_RETRY_WAITS) + 1)

    def test_embed_reply_refuses_bad_order_counts_dimensions_tokens_and_nonfinite_values(self) -> None:
        valid = {"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}], "usage": {"prompt_tokens": 17}}
        variants = (
            [], {}, {"data": [], "usage": valid["usage"]}, {"data": valid["data"]},
            {**valid, "usage": {"prompt_tokens": True}}, {**valid, "usage": {"prompt_tokens": -1}},
            {**valid, "data": [{"index": True, "embedding": [0.1, 0.2, 0.3]}]},
            {**valid, "data": [{"index": 1, "embedding": [0.1, 0.2, 0.3]}]},
            {**valid, "data": [None]}, {**valid, "data": [{"index": 0}]},
            *({**valid, "data": [{"index": 0, "embedding": vector}]} for vector in (
                [], [0.1], [0.1] * (WIDTH + 1), [True, 0.2, 0.3], ["private", 0.2, 0.3],
                [float("nan"), 0.2, 0.3], [float("inf"), 0.2, 0.3],
            )),
        )
        for value in variants:
            with self.subTest(value=value), self.assertRaises(dense.DenseFailure) as raised:
                self.embedder(answer(value, envelope=False)).embed((TEXT,))
            self.assertEqual(raised.exception.reason, "embed")
        with self.assertRaises(dense.DenseFailure):
            self.embedder(lambda request: httpx.Response(200, text="FICTITIOUS_INVALID_REPLY")).embed((TEXT,))
        swapped = {"data": [{"index": 1, "embedding": [0.1, 0.2, 0.3]}, {"index": 0, "embedding": [0.4, 0.5, 0.6]}], "usage": {"prompt_tokens": 31}}
        with self.assertRaises(dense.DenseFailure) as raised:
            self.embedder(answer(swapped, envelope=False)).embed((TEXT, "FICTITIOUS_SECOND_TEXT"))
        self.assertEqual(raised.exception.error, "InvalidOrder")

    def test_embed_coordinates_refuse_before_opening_a_client(self) -> None:
        for served, width in (("invalid model", WIDTH), ("", WIDTH), ("x" * 65, WIDTH),
                              (SERVED, 0), (SERVED, True), (SERVED, dense.DIMENSIONS_MAX + 1)):
            with self.subTest(served=served, width=width), self.assertRaises(dense.DenseFailure) as raised:
                dense.HttpxEmbedder(served, width, self.configuration)
            self.assertEqual(raised.exception.reason, "invalid")

    def test_http_and_failure_details_never_reach_the_worker_handler(self) -> None:
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        output = io.StringIO()
        try:
            logs.install_handler(output)
            root.setLevel(logging.INFO)
            with self.assertRaises(dense.DenseFailure) as raised:
                self.embedder(lambda request: httpx.Response(400, text="FICTITIOUS_PRIVATE_DETAIL")).embed((TEXT,))
            logging.getLogger("gideon.worker.dense").error("dense failure %s", raised.exception)
            for sentinel in (TEXT, "FICTITIOUS_PRIVATE_DETAIL", self.embed_key.read_text(), self.vector_key.read_text()):
                self.assertNotIn(sentinel, output.getvalue())
            self.assertIn("dense failure embed", output.getvalue())
        finally:
            root.handlers[:] = handlers
            root.setLevel(level)


def dense_inputs() -> dict[str, object]:
    return {**inputs_for(), "served_model": SERVED, "dimensions": WIDTH}


class FakeVectorStore:
    """Retain acknowledged points across client instances and attempted builds."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.collections: dict[str, dict[str, dense.Point]] = {}
        self.config = info_for()["config"]
        self.lock = threading.Lock()
        self.closed = False
        self.info_failure: Exception | None = None
        self.upsert_failure: Exception | None = None
        self.indexed_callback: Callable[[Callable[[int], None]], None] | None = None
        self.retrievals: list[tuple[str, tuple[str, ...]]] = []
        self.thresholds: list[int] = []

    def info(self, name: str) -> dict[str, Any] | None:
        self.events.append("info")
        if self.info_failure is not None:
            raise self.info_failure
        if name not in self.collections:
            return None
        return {**info_for(), "config": self.config, "points_count": len(self.collections[name]),
                "indexed_vectors_count": len(self.collections[name])}

    def create(self, name: str, size: int) -> None:
        self.events.append("create")
        self.collections[name] = {}
        self.config["params"]["vectors"]["size"] = size

    def ensure_indexes(self, name: str) -> None:
        self.events.append("indexes")

    def set_indexing(self, name: str, threshold: int) -> None:
        self.events.append("pause" if threshold == dense.FILL_INDEXING_THRESHOLD else "restore")
        self.thresholds.append(threshold)

    def present(self, name: str, ids: Sequence[str]) -> set[str]:
        self.events.append("present")
        return set(ids) & self.collections[name].keys()

    def retrieve(self, name: str, ids: Sequence[str]) -> dict[str, tuple[float, ...]]:
        self.retrievals.append((name, tuple(ids)))
        return {identifier: self.collections[name][identifier].vector for identifier in ids
                if identifier in self.collections.get(name, {})}

    def upsert(self, name: str, points: Sequence[dense.Point]) -> None:
        if self.upsert_failure is not None:
            raise self.upsert_failure
        with self.lock:
            self.collections[name].update((point.id, point) for point in points)
            self.events.append("upsert-ack")

    def wait_green(self, name: str, progress: Callable[[int], None]) -> dict[str, Any]:
        self.events.append("wait-green")
        if self.indexed_callback is not None:
            self.indexed_callback(progress)
        info = self.info(name)
        assert info is not None
        return info

    def close(self) -> None:
        self.closed = True
        self.events.append("store-close")


class FakeDenseRecord:
    """Assert record access stays on the build thread and commits follow point acks."""

    def __init__(self, digest: str, store: FakeVectorStore, events: list[str]) -> None:
        values = inputs_for()
        self.generation: lexical.Generation | None = lexical.Generation(
            NAME, "caselaw", cast(str, values["inputs_hash"]), chunk_for().pipeline,
            cast(str, values["embedding_space"]), "building",
        )
        self.store, self.events = store, events
        self.document_rows = [lexical.Document("a" * 64, digest), lexical.Document("d" * 64, digest)]
        self.chunk_rows = {"a" * 64: (chunk_for(),), "d" * 64: ()}
        self.ledger: dict[str, dense.LedgerRow] = {}
        self.counts = dense.DenseCounts()
        self.commits: list[tuple[dense.DenseCounts, tuple[dense.LedgerRow, ...], tuple[dense.LedgerRow, ...]]] = []
        self.scopes: list[int | None] = []
        self.limits: list[int | None] = []
        self.owner: int | None = None
        self.failure: str | None = None
        self.read_failure: Exception | None = None
        self.progress_failure: BaseException | None = None
        self.closed = False
        self.lexical_built_at: datetime | None = NOW
        self.dense_built_at: datetime | None = None
        self.built_at: datetime | None = None
        self.finished: tuple[dense.DenseCounts, datetime, bool] | None = None
        self.opinion_reads: list[str] = []

    def _main(self) -> None:
        assert threading.get_ident() == self.owner, "record touched by a writer thread"

    def read(self, name: str) -> lexical.Generation | None:
        self.owner = threading.get_ident()
        if self.read_failure is not None:
            raise self.read_failure
        return self.generation

    def resume(self, name: str) -> None:
        self._main()
        self.events.append("resume")
        assert self.generation is not None
        self.generation = replace(self.generation, state="building")
        self.failure = None

    def scope_total(self, collection: str, source: str, snapshot: object, courts: Sequence[str], pipeline: str, limit: int | None = None) -> int:
        self._main()
        self.scopes.append(limit)
        return sum(len(self.chunk_rows[document.doc_id]) for document in self.document_rows[:limit])

    def documents(self, collection: str, source: str, snapshot: object, courts: Sequence[str], limit: int | None = None) -> Iterator[lexical.Document]:
        self._main()
        self.limits.append(limit)
        yield from self.document_rows[:limit]

    def chunks(self, doc_id: str, pipeline: str) -> tuple[chunker.NewChunk, ...]:
        self._main()
        return self.chunk_rows[doc_id]

    def opinion(self, doc_id: str):
        self._main()
        self.opinion_reads.append(doc_id)
        return replace(opinion_for(), doc_id=doc_id)

    def ledger_rows(self, space: str, hashes: Sequence[str]) -> tuple[dense.LedgerRow, ...]:
        self._main()
        return tuple(self.ledger[content_hash] for content_hash in hashes if content_hash in self.ledger)

    def start(self, name: str, counts: dense.DenseCounts) -> None:
        self._main()
        self.events.append("start")
        self.counts = counts

    def progress(self, name: str, counts: dense.DenseCounts, inserts: Sequence[dense.LedgerRow] = (), rehomes: Sequence[dense.LedgerRow] = ()) -> None:
        self._main()
        if self.progress_failure is not None:
            raise self.progress_failure
        for row in (*inserts, *rehomes):
            assert row.point_id in self.store.collections[row.gen_id], "ledger committed before an acknowledged point"
        for row in inserts:
            self.ledger.setdefault(row.content_hash, row)
        for row in rehomes:
            assert row.content_hash in self.ledger
            self.ledger[row.content_hash] = row
        self.counts = counts
        self.commits.append((counts, tuple(inserts), tuple(rehomes)))
        self.events.append("commit")

    def finish(self, name: str, counts: dense.DenseCounts, at: datetime, limited: bool) -> None:
        self._main()
        self.events.append("finish")
        self.counts, self.finished = counts, (counts, at, limited)
        assert self.generation is not None
        if not limited:
            self.dense_built_at = at
            if self.lexical_built_at is not None:
                self.built_at = self.built_at or at
                if self.generation.state == "building":
                    self.generation = replace(self.generation, state="built")

    def fail(self, name: str, reason: str) -> None:
        self.failure = reason
        self.events.append("fail")
        if self.generation is not None:
            self.generation = replace(self.generation, state="failed")

    def close(self) -> None:
        self.closed = True
        self.events.append("record-close")


class FakeEmbedder:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[tuple[str, ...]] = []
        self.failure: Exception | None = None
        self.before_reply: Callable[[], None] | None = None
        self.closed = False

    def embed(self, texts: Sequence[str]) -> dense.EmbedReply:
        self.calls.append(tuple(texts))
        self.events.append("embed")
        if self.before_reply is not None:
            self.before_reply()
        if self.failure is not None:
            raise self.failure
        return dense.EmbedReply(tuple((0.1, 0.2, 0.3) for _text in texts), len(texts) * 7)

    def close(self) -> None:
        self.closed = True
        self.events.append("embed-close")


class FakePace:
    def __init__(self) -> None:
        self.threads: list[int] = []

    def wait_before(self) -> None:
        self.threads.append(threading.get_ident())


class Build(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store_root, self.work_root = self.root / "cas", self.root / "work"
        self.store_root.mkdir()
        self.work_root.mkdir()
        self.digest = self.save_text(TEXT)
        self.events: list[str] = []
        self.store = FakeVectorStore(self.events)
        self.record = FakeDenseRecord(self.digest, self.store, self.events)
        self.embedder = FakeEmbedder(self.events)
        self.pace = FakePace()
        self.seconds = 0.0

    def save_text(self, text: str) -> str:
        digest = hashlib.sha256(text.encode()).hexdigest()
        path = cas.object_path(digest, self.store_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return digest

    def run_build(self, values: dict[str, object] | None = None) -> dense.DenseCounts:
        return dense.build(values or dense_inputs(), 77, self.record, self.store, self.embedder, self.pace,
                           self.store_root, work_root=self.work_root, clock=lambda: NOW, monotonic=lambda: self.seconds)

    def distinct_chunks(self, count: int) -> None:
        self.record.document_rows = []
        self.record.chunk_rows = {}
        for index in range(count):
            text = f"α🙂 FICTITIOUS UNIQUE TEXT {index}"
            doc_id = f"{index + 1:064x}"
            digest = self.save_text(text)
            self.record.document_rows.append(lexical.Document(doc_id, digest))
            self.record.chunk_rows[doc_id] = (replace(chunk_for(), doc_id=doc_id, chunk_id=f"{index + 1:032x}" + "a" * 32,
                                                    char_end=len(text), content_hash=hashlib.sha256(text[2:].encode()).hexdigest()),)

    def test_first_run_creates_pauses_fills_commits_restores_and_finishes(self) -> None:
        counts = self.run_build()
        self.assertEqual((counts.dense_total, counts.dense_scope, counts.dense_embedded, counts.dense_texts, counts.dense_tokens, counts.dense_requests), (1, 1, 1, 1, 7, 1))
        self.assertEqual((counts.dense_points, counts.dense_indexed, counts.dense_limit), (1, 1, 0))
        self.assertEqual(self.store.thresholds, [dense.FILL_INDEXING_THRESHOLD, dense.SHIPPED_INDEXING_THRESHOLD])
        self.assertEqual(self.record.scopes, [None, None])
        self.assertEqual(self.record.limits, [None])
        for first, second in (("create", "indexes"), ("pause", "embed"), ("upsert-ack", "commit"),
                              ("commit", "restore"), ("restore", "wait-green"), ("wait-green", "finish")):
            self.assertLess(self.events.index(first), self.events.index(second))
        chunk = chunk_for()
        identifier = dense.point_id(chunk.chunk_id)
        point = self.store.collections[NAME][identifier]
        self.assertEqual(point.payload, dense.payload_for(chunk, opinion_for(), GEO))
        self.assertEqual(self.record.ledger[chunk.content_hash], dense.LedgerRow(chunk.content_hash, NAME, identifier))
        self.assertEqual(self.record.finished, (counts, NOW, False))
        self.assertEqual((self.record.dense_built_at, self.record.built_at), (NOW, NOW))
        assert self.record.generation is not None
        self.assertEqual(self.record.generation.state, "built")
        self.assertTrue(self.record.closed and self.store.closed and self.embedder.closed)
        self.assertEqual(len(self.pace.threads), counts.dense_requests)
        self.assertNotIn(threading.get_ident(), self.pace.threads)

    def test_two_writers_bound_submissions_until_main_thread_commits(self) -> None:
        self.distinct_chunks(dense.EMBED_BATCH_TEXTS * dense.BULK_WRITERS + 1)
        barrier = threading.Barrier(dense.BULK_WRITERS)
        calls = 0
        guard = threading.Lock()

        def meet() -> None:
            nonlocal calls
            with guard:
                calls += 1
                first_wave = calls <= dense.BULK_WRITERS
            if first_wave:
                barrier.wait(timeout=2)

        self.embedder.before_reply = meet
        record = self.record
        submitted: list[int] = []

        class BoundedExecutor(ThreadPoolExecutor):
            def submit(self, fn, /, *args, **kwargs):
                assert len(submitted) + 1 - record.counts.dense_requests <= dense.BULK_WRITERS
                submitted.append(len(submitted))
                return super().submit(fn, *args, **kwargs)

        with patch.object(dense, "ThreadPoolExecutor", BoundedExecutor):
            counts = self.run_build()
        self.assertEqual(counts.dense_requests, len(submitted))
        self.assertEqual(counts.dense_embedded, len(self.record.document_rows))
        self.assertEqual(len(set(self.pace.threads[:dense.BULK_WRITERS])), dense.BULK_WRITERS)

    def test_present_chunks_skip_text_and_requests_and_repair_a_missing_ledger(self) -> None:
        chunk = chunk_for()
        identifier = dense.point_id(chunk.chunk_id)
        self.store.collections[NAME] = {identifier: dense.Point(identifier, (0.1, 0.2, 0.3), {})}
        cas.object_path(self.digest, self.store_root).unlink()
        counts = self.run_build()
        self.assertEqual((counts.dense_present, counts.dense_embedded, counts.dense_copied, counts.dense_requests), (1, 0, 0, 0))
        self.assertEqual(self.record.ledger[chunk.content_hash].point_id, identifier)
        self.assertFalse(self.embedder.calls)
        self.assertFalse(self.record.opinion_reads)
        self.assertNotIn("create", self.events)
        self.assertNotIn("upsert-ack", self.events)

    def test_kill_after_upsert_before_commit_is_repaired_without_reembedding(self) -> None:
        class Killed(BaseException):
            pass

        self.record.progress_failure = Killed()
        with self.assertRaises(Killed):
            self.run_build()
        self.assertEqual(len(self.store.collections[NAME]), 1)
        self.assertFalse(self.record.ledger)
        self.record.progress_failure = None
        self.embedder.calls.clear()
        counts = self.run_build()
        self.assertEqual((counts.dense_present, counts.dense_requests), (1, 0))
        self.assertEqual(len(self.record.ledger), 1)
        self.assertFalse(self.embedder.calls)

    def test_copy_from_another_home_rehomes_once_and_never_reads_text(self) -> None:
        chunk = chunk_for()
        duplicate = replace(chunk, chunk_id="2" * 64)
        self.record.chunk_rows[chunk.doc_id] = (chunk, duplicate)
        home = "caselaw__corpus-2098-01-01__example-space"
        identifier = dense.point_id("f" * 64)
        self.store.collections[home] = {identifier: dense.Point(identifier, (0.7, 0.8, 0.9), {})}
        self.record.ledger[chunk.content_hash] = dense.LedgerRow(chunk.content_hash, home, identifier)
        cas.object_path(self.digest, self.store_root).unlink()
        counts = self.run_build()
        self.assertEqual((counts.dense_copied, counts.dense_embedded, counts.dense_texts), (2, 0, 0))
        self.assertEqual(self.store.retrievals, [(home, (identifier,))])
        self.assertEqual(self.record.ledger[chunk.content_hash], dense.LedgerRow(chunk.content_hash, NAME, dense.point_id(chunk.chunk_id)))
        self.assertEqual(len(self.record.commits[0][2]), 1)
        self.assertFalse(self.embedder.calls)

    def test_dropped_and_wiped_homes_embed_and_rehome_the_existing_row(self) -> None:
        chunk = chunk_for()
        for home in ("caselaw__corpus-2098-01-01__example-space", NAME):
            with self.subTest(home=home):
                self.store.collections.clear()
                self.record.ledger[chunk.content_hash] = dense.LedgerRow(chunk.content_hash, home, dense.point_id("f" * 64))
                self.record.commits.clear()
                counts = self.run_build()
                self.assertEqual((counts.dense_embedded, counts.dense_copied), (1, 0))
                self.assertEqual(self.record.commits[0][1], ())
                self.assertEqual(self.record.commits[0][2], (dense.LedgerRow(chunk.content_hash, NAME, dense.point_id(chunk.chunk_id)),))

    def test_same_hash_embeds_one_text_and_reads_each_document_once(self) -> None:
        chunk = chunk_for()
        second = replace(chunk, doc_id="d" * 64, chunk_id="2" * 64)
        self.record.chunk_rows[second.doc_id] = (second, replace(second, chunk_id="3" * 64))
        with patch.object(lexical, "canonical_text", wraps=lexical.canonical_text) as read:
            counts = self.run_build()
        self.assertEqual((counts.dense_embedded, counts.dense_texts, counts.dense_requests), (3, 1, 1))
        self.assertEqual(self.embedder.calls, [(TEXT[chunk.char_start:chunk.char_end],)])
        self.assertEqual(read.call_count, 2)
        self.assertEqual(len(self.record.ledger), 1)

    def test_limit_and_short_hour_reading_leave_the_dense_half_unbuilt(self) -> None:
        self.distinct_chunks(3)
        self.embedder.before_reply = lambda: setattr(self, "seconds", 17.0)
        counts = self.run_build({**dense_inputs(), "limit": 2})
        self.assertEqual((counts.dense_total, counts.dense_scope, counts.dense_limit, counts.dense_embedded), (3, 2, 2, 2))
        self.assertEqual(self.record.scopes, [None, 2])
        self.assertEqual(self.record.limits, [2])
        self.assertEqual((counts.dense_hour_seconds, counts.dense_hour_tokens, counts.dense_hour_written), (17, counts.dense_tokens, counts.dense_embedded))
        self.assertEqual(self.record.finished, (counts, NOW, True))
        self.assertIsNone(self.record.dense_built_at)
        self.assertIsNone(self.record.built_at)

    def test_hour_triple_is_written_once_and_optimizer_progress_keeps_it(self) -> None:
        self.embedder.before_reply = lambda: setattr(self, "seconds", float(dense.HOUR_SECONDS))

        def indexed(callback: Callable[[int], None]) -> None:
            self.seconds += dense.LOG_SECONDS
            callback(1)

        self.store.indexed_callback = indexed
        counts = self.run_build()
        hour = self.record.commits[0][0]
        self.assertEqual(hour.dense_hour_seconds, dense.HOUR_SECONDS)
        self.assertEqual(hour.dense_hour_tokens, hour.dense_tokens)
        self.assertEqual(hour.dense_hour_written, hour.dense_copied + hour.dense_embedded)
        self.assertEqual((counts.dense_hour_seconds, counts.dense_hour_tokens, counts.dense_hour_written),
                         (hour.dense_hour_seconds, hour.dense_hour_tokens, hour.dense_hour_written))
        self.assertEqual(self.record.commits[-1][0].dense_indexed, 1)

    def test_failed_row_resumes_and_a_rerun_clears_stale_attempt_figures(self) -> None:
        assert self.record.generation is not None
        self.record.generation = replace(self.record.generation, state="failed")
        self.record.counts = dense.DenseCounts(dense_limit=19, dense_hour_seconds=3700, dense_tokens=91)
        counts = self.run_build()
        self.assertIn("resume", self.events)
        self.assertEqual(counts.dense_limit, 0)
        self.assertEqual(counts.dense_hour_seconds, 0)
        self.assertEqual(counts.dense_tokens, 7)
        before = len(self.embedder.calls)
        again = self.run_build()
        self.assertEqual((again.dense_present, again.dense_embedded, again.dense_tokens), (1, 0, 0))
        self.assertEqual(len(self.embedder.calls), before)

    def test_empty_generation_finishes_and_dense_first_waits_for_lexical(self) -> None:
        self.record.chunk_rows = dict.fromkeys(self.record.chunk_rows, ())
        self.record.lexical_built_at = None
        counts = self.run_build()
        self.assertEqual((counts.dense_scope, counts.dense_points, counts.dense_requests), (0, 0, 0))
        self.assertEqual(self.record.dense_built_at, NOW)
        self.assertIsNone(self.record.built_at)
        assert self.record.generation is not None
        self.assertEqual(self.record.generation.state, "building")

    def test_invalid_dense_and_lexical_arguments_refuse_before_reading_scope(self) -> None:
        for key, value in (
            ("served_model", "private invalid model"), ("served_model", None),
            ("dimensions", True), ("dimensions", 0), ("dimensions", dense.DIMENSIONS_MAX + 1),
            ("limit", True), ("limit", 0), ("limit", -1), ("limit", "3"),
            ("pipeline", "unregistered"), ("unexpected", 1),
        ):
            with self.subTest(key=key), self.assertRaises(dense.DenseFailure) as raised:
                self.run_build({**dense_inputs(), key: value})
            self.assertEqual(raised.exception.reason, "invalid")
            self.assertEqual(self.record.failure, "invalid")
        self.assertFalse(self.record.scopes)
        with self.assertRaises(dense.DenseFailure) as raised:
            dense.build(dense_inputs(), True, self.record, self.store, self.embedder, self.pace, self.store_root, work_root=self.work_root)
        self.assertEqual(raised.exception.reason, "invalid")

    def test_generation_mismatches_and_terminal_states_refuse(self) -> None:
        generation = self.record.generation
        assert generation is not None
        variants = (None, replace(generation, gen_id="other"), replace(generation, inputs_hash="f" * 64),
                    replace(generation, collection="other"), replace(generation, pipeline="9.9.9"),
                    replace(generation, embedding_space="other"))
        for variant in variants:
            self.record.generation = variant
            with self.subTest(variant=variant), self.assertRaises(dense.DenseFailure) as raised:
                self.run_build()
            self.assertEqual(raised.exception.reason, "generation-mismatch")
        for state in ("live", "retired"):
            self.record.generation = replace(generation, state=state)
            with self.subTest(state=state), self.assertRaises(dense.DenseFailure) as raised:
                self.run_build()
            self.assertEqual(raised.exception.reason, "invalid")
        self.assertNotIn("create", self.events)

    def test_busy_lock_leaves_the_generation_and_ledger_untouched(self) -> None:
        generation = self.record.generation
        path = self.work_root / f"{NAME}{dense.DENSE_LOCK_SUFFIX}"
        with path.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(dense.DenseFailure) as raised:
                self.run_build()
        self.assertEqual(raised.exception.reason, "busy")
        self.assertEqual(self.record.generation, generation)
        self.assertIsNone(self.record.failure)
        self.assertFalse(self.record.ledger)

    def test_config_mismatch_never_writes_the_collection(self) -> None:
        self.store.collections[NAME] = {}
        self.store.config["params"]["vectors"]["size"] = WIDTH + 1
        with self.assertRaises(dense.DenseFailure) as raised:
            self.run_build()
        self.assertEqual(raised.exception.reason, "config-mismatch")
        self.assertEqual(self.events[:2], ["info", "fail"])
        self.assertFalse(self.store.thresholds)
        self.assertNotIn("indexes", self.events)

    def test_overlong_batch_sends_no_embed_or_point_write(self) -> None:
        chunk = chunk_for()
        self.record.chunk_rows[chunk.doc_id] = (chunk, replace(chunk, chunk_id="2" * 64, token_count=dense.EMBED_TEXT_TOKEN_CAP + 1))
        with self.assertRaises(dense.DenseFailure) as raised:
            self.run_build()
        self.assertEqual(raised.exception.reason, "chunk-too-long")
        self.assertFalse(self.embedder.calls)
        self.assertNotIn("upsert-ack", self.events)

    def test_overlong_fresh_text_prevents_the_same_batch_copy_from_writing(self) -> None:
        chunk = chunk_for()
        overlong = replace(chunk_for(3), chunk_id="2" * 64, token_count=dense.EMBED_TEXT_TOKEN_CAP + 1)
        self.record.chunk_rows[chunk.doc_id] = (chunk, overlong)
        home = "caselaw__corpus-2098-01-01__example-space"
        identifier = dense.point_id("f" * 64)
        self.store.collections[home] = {identifier: dense.Point(identifier, (0.7, 0.8, 0.9), {})}
        self.record.ledger[chunk.content_hash] = dense.LedgerRow(chunk.content_hash, home, identifier)
        with self.assertRaises(dense.DenseFailure) as raised:
            self.run_build()
        self.assertEqual(raised.exception.reason, "chunk-too-long")
        self.assertFalse(self.embedder.calls)
        self.assertNotIn("upsert-ack", self.events)
        self.assertFalse(self.store.collections[NAME])

    def test_text_failures_database_local_embed_and_store_reasons_are_recorded(self) -> None:
        path = cas.object_path(self.digest, self.store_root)
        for body, reason in ((None, "missing-text"), (b"wrong-text", "text-mismatch")):
            if body is None:
                path.unlink()
            else:
                path.write_bytes(body)
            with self.subTest(reason=reason), self.assertRaises(dense.DenseFailure) as raised:
                self.run_build()
            self.assertEqual((raised.exception.reason, self.record.failure), (reason, reason))
        path.write_text(TEXT, encoding="utf-8")
        self.record.read_failure = lexical.LexicalFailure("database", error="OperationalError")
        with self.assertRaises(dense.DenseFailure) as raised:
            self.run_build()
        self.assertEqual((raised.exception.reason, raised.exception.error), ("database", "OperationalError"))
        self.record.read_failure = None
        for client, attribute, reason in ((self.embedder, "failure", "embed"), (self.store, "upsert_failure", "store")):
            setattr(client, attribute, dense.DenseFailure(reason))
            with self.subTest(reason=reason), self.assertRaises(dense.DenseFailure) as raised:
                self.run_build()
            self.assertEqual((raised.exception.reason, self.record.failure), (reason, reason))
            setattr(client, attribute, None)
        self.work_root = self.root / "missing-work-root"
        with self.assertRaises(dense.DenseFailure) as raised:
            self.run_build()
        self.assertEqual((raised.exception.reason, self.record.failure), ("local", "local"))

    def test_failed_writer_joins_running_work_before_cleanup_and_keeps_first_failure(self) -> None:
        self.distinct_chunks(dense.EMBED_BATCH_TEXTS * 3)
        barrier = threading.Barrier(dense.BULK_WRITERS)
        calls = 0
        guard = threading.Lock()
        failure = dense.DenseFailure("embed", error="FICTITIOUS_PRIVATE_DETAIL")

        def reply() -> None:
            nonlocal calls
            with guard:
                calls += 1
                selected = calls
            barrier.wait(timeout=2)
            if selected == 1:
                raise failure

        self.embedder.before_reply = reply
        with self.assertRaises(dense.DenseFailure) as raised:
            self.run_build()
        self.assertIs(raised.exception, failure)
        self.assertEqual(len(self.embedder.calls), dense.BULK_WRITERS)
        if "upsert-ack" in self.events:
            self.assertLess(self.events.index("upsert-ack"), self.events.index("store-close"))
        self.assertTrue(self.record.closed and self.store.closed and self.embedder.closed)

    def test_cleanup_and_failure_recording_cannot_replace_the_original_failure(self) -> None:
        failure = dense.DenseFailure("embed")
        self.embedder.failure = failure
        with (
            patch.object(self.record, "fail", side_effect=lexical.LexicalFailure("database")),
            patch.object(self.store, "close", side_effect=OSError("private cleanup")),
            self.assertRaises(dense.DenseFailure) as raised,
        ):
            self.run_build()
        self.assertIs(raised.exception, failure)
        self.assertTrue(self.record.closed and self.embedder.closed)

    def test_build_logs_ids_and_counts_without_slices_and_respects_the_interval(self) -> None:
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        output = io.StringIO()
        self.embedder.before_reply = lambda: setattr(self, "seconds", float(dense.LOG_SECONDS))
        try:
            logs.install_handler(output)
            root.setLevel(logging.INFO)
            counts = self.run_build()
        finally:
            root.handlers[:] = handlers
            root.setLevel(level)
        self.assertIn("job_id=77", output.getvalue())
        self.assertIn(NAME, output.getvalue())
        self.assertIn(f"dense_tokens={counts.dense_tokens}", output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertNotIn(TEXT[2:], output.getvalue())
        self.assertNotIn(opinion_for().docket or "absent-caption", output.getvalue())
