"""Project recorded chunks into vectors and maintain their embedding ledger."""

import itertools
import json
import logging
import math
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final, Protocol, cast

import httpx
import psycopg

import gideon.host.cas
from gideon.host.sysio import BytesHost, RealHost
from gideon.worker import caselaw, chunker, lexical, settings, staging, treatment

DENSE_TASK: Final = "gideon.worker.tasks.dense"
DENSE_QUEUE: Final = "index"
DENSE_LOCK_PREFIX: Final = "dense-"
DENSE_LOCK_SUFFIX: Final = ".dense.lock"
DENSE_FAILURE_REASONS: Final = frozenset({
    "invalid", "store", "embed", "database", "local", "busy",
    "config-mismatch", "chunk-too-long", "text-mismatch", "missing-text",
    "generation-mismatch",
})
DENSE_COUNT_KEYS: Final = (
    "dense_total", "dense_scope", "dense_present", "dense_copied",
    "dense_embedded", "dense_texts", "dense_tokens", "dense_requests",
    "dense_seconds", "dense_points", "dense_indexed", "dense_limit",
    "dense_hour_seconds", "dense_hour_tokens", "dense_hour_written",
    "dense_leveled",
)
SERVED_MODEL_PATTERN: Final = r"[A-Za-z0-9._-]{1,64}"
# exempt: the bound caps a job argument's vector width and its allocation.
DIMENSIONS_MAX: Final = 65_536
# exempt: two writers are the starting store-load setting, corrected by measurement.
BULK_WRITERS: Final = 2
# exempt: requests use the measured 32-text, 16384-token throughput batch.
EMBED_BATCH_TEXTS: Final = 32
EMBED_BATCH_TOKENS: Final = 16_384
# exempt: the server's measured input edge is 8191 tokens per text.
EMBED_TEXT_TOKEN_CAP: Final = 8_191
# exempt: bounded connection, read, write, and pool waits keep store failures finite.
HTTP_TIMEOUT: Final = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
# exempt: an embedding request gets the throughput probe's two-minute read bound.
EMBED_TIMEOUT: Final = httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=10.0)
# exempt: three increasing waits bound retries of temporary store pressure.
RETRY_WAITS: Final = (0.5, 1.0, 2.0)
# exempt: the waits outlast the embedding server's 300 s ready allowance, so a restart under its policy is absorbed.
EMBED_RETRY_WAITS: Final = (5.0, 30.0, 120.0, 300.0)
# exempt: one log and indexed-count callback per minute bound diagnostic overhead.
LOG_SECONDS: Final = 60.0
# exempt: the first-hour reading is one elapsed hour of the current attempt.
HOUR_SECONDS: Final = 3_600
# exempt: document batches bound metadata held while looking up chunk presence.
DOCUMENT_BATCH_SIZE: Final = 64
# exempt: presence lookups bound ids independently of a document's chunk count.
PRESENCE_BATCH: Final = 1_000
# exempt: retrieving vectors uses a smaller batch to bound resident memory.
RETRIEVE_BATCH: Final = 256
# exempt: five seconds between optimizer polls bound store traffic.
POLL_SECONDS: Final = 5.0
# exempt: a grey optimizer is triggered at most once a minute.
GREY_TRIGGER_SECONDS: Final = 60.0
# exempt: bulk loading raises the threshold in KB above any segment, avoiding zero's ambiguous readings.
FILL_INDEXING_THRESHOLD: Final = 100_000_000
# exempt: the normal threshold in KB is the store image's shipped default.
SHIPPED_INDEXING_THRESHOLD: Final = 10_000
# exempt: cosine, disk float16, RAM int8, and the graph's starting layout are the release's index config.
COLLECTION_CONFIG: Final[dict[str, Any]] = {
    "vectors": {"distance": "Cosine", "datatype": "float16", "on_disk": True},
    "hnsw_config": {"m": 16, "ef_construct": 100},
    "quantization_config": {"scalar": {"type": "int8", "always_ram": True}},
    "optimizers_config": {"indexing_threshold": SHIPPED_INDEXING_THRESHOLD},
}
PAYLOAD_INDEXES: Final = {
    "court": "keyword", "circuit": "keyword", "precedential": "keyword",
    "section_type": "keyword", "decided_date": "datetime",
    "dupe_cluster": "keyword", "boilerplate": "bool",
}
logger = logging.getLogger(__name__)


class DenseFailure(Exception):
    """A content-free refusal from the closed dense failure vocabulary."""

    def __init__(self, reason: str, *, error: str | None = None) -> None:
        if reason not in DENSE_FAILURE_REASONS:
            raise ValueError("unknown dense failure reason")
        self.reason, self.error = reason, error
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class DenseCounts:
    """Integer progress and first-hour figures for one dense attempt."""

    dense_total: int = 0
    dense_scope: int = 0
    dense_present: int = 0
    dense_copied: int = 0
    dense_embedded: int = 0
    dense_texts: int = 0
    dense_tokens: int = 0
    dense_requests: int = 0
    dense_seconds: int = 0
    dense_points: int = 0
    dense_indexed: int = 0
    dense_limit: int = 0
    dense_hour_seconds: int = 0
    dense_hour_tokens: int = 0
    dense_hour_written: int = 0
    dense_leveled: int = 0


@dataclass(frozen=True, slots=True)
class Point:
    """One deterministic point id, vector, and record-derived filter payload."""

    id: str
    vector: tuple[float, ...]
    payload: Mapping[str, str | bool]


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """A chunk text's vector home; the generation supplies its embedding space."""

    content_hash: str
    gen_id: str
    point_id: str


@dataclass(frozen=True, slots=True)
class EmbedReply:
    """Vectors in input order and the server's prompt-token count."""

    vectors: tuple[tuple[float, ...], ...]
    prompt_tokens: int


@dataclass(frozen=True, slots=True)
class TextGroup:
    """One representative text and every chunk sharing its content hash."""

    text: str
    chunks: tuple[chunker.NewChunk, ...]


class GenerationRecord(Protocol):
    """Read the immutable scope and commit dense state and ledger rows together."""

    def read(self, name: str) -> lexical.Generation | None: ...
    def resume(self, name: str) -> None: ...
    def fail(self, name: str, reason: str) -> None: ...
    def scope_total(self, collection: str, source: str, snapshot: date, courts: Sequence[str], pipeline: str, limit: int | None = None) -> int: ...
    def documents(self, collection: str, source: str, snapshot: date, courts: Sequence[str], limit: int | None = None) -> Iterator[lexical.Document]: ...
    def chunks(self, doc_id: str, pipeline: str) -> tuple[chunker.NewChunk, ...]: ...
    def opinion(self, doc_id: str) -> caselaw.NewOpinion: ...
    def keys_for(self, name: str, hashes: Sequence[str]) -> dict[str, lexical.TextKeys]: ...
    def ledger_rows(self, space: str, hashes: Sequence[str]) -> tuple[LedgerRow, ...]: ...
    def start(self, name: str, counts: DenseCounts) -> None: ...
    def progress(self, name: str, counts: DenseCounts, inserts: Sequence[LedgerRow] = (), rehomes: Sequence[LedgerRow] = ()) -> None: ...
    def finish(self, name: str, counts: DenseCounts, at: datetime, limited: bool) -> None: ...
    def close(self) -> None: ...


class VectorStore(Protocol):
    """Manage the collection, point presence, vector copies, and its optimizer."""

    def info(self, name: str) -> Mapping[str, Any] | None: ...
    def create(self, name: str, size: int) -> None: ...
    def ensure_indexes(self, name: str) -> None: ...
    def set_indexing(self, name: str, threshold: int) -> None: ...
    def present(self, name: str, ids: Sequence[str]) -> dict[str, lexical.TextKeys | None]: ...
    def retrieve(self, name: str, ids: Sequence[str]) -> dict[str, tuple[float, ...]]: ...
    def upsert(self, name: str, points: Sequence[Point]) -> None: ...
    def set_payload(self, name: str, ids: Sequence[str], payload: Mapping[str, str | bool]) -> None: ...
    def wait_green(self, name: str, progress: Callable[[int], None]) -> Mapping[str, Any]: ...
    def close(self) -> None: ...


class Embedder(Protocol):
    """Embed bare canonical slices under one served model and vector width."""

    def embed(self, texts: Sequence[str]) -> EmbedReply: ...
    def close(self) -> None: ...


class Pace(Protocol):
    """Wait before a writer submits an embedding request."""

    def wait_before(self) -> None: ...


class NoBackOff:
    """The shipped pacing policy permits each request immediately."""

    def wait_before(self) -> None:
        pass


def point_id(chunk_id: str) -> str:
    """Return the canonical UUID made from the chunk id's first 32 hex digits."""

    return str(uuid.UUID(hex=chunk_id[:32]))


def payload_for(
    chunk: chunker.NewChunk, opinion: caselaw.NewOpinion,
    geography: Mapping[str, treatment.Geography],
    keys: lexical.TextKeys,
) -> dict[str, str | bool]:
    """Project only the chunk id and record-derived filter keys."""

    payload: dict[str, str | bool] = {
        "chunk_id": chunk.chunk_id, "court": opinion.court,
        "decided_date": datetime.combine(opinion.decided_date, datetime.min.time(), UTC).isoformat(),
        "precedential": opinion.precedential, "section_type": chunk.section_type,
        "dupe_cluster": keys.dupe_cluster, "boilerplate": keys.boilerplate,
    }
    circuit = geography[opinion.court].circuit
    if circuit is not None:
        payload["circuit"] = circuit
    return payload


def group_by_hash(chunks_with_text: Sequence[tuple[chunker.NewChunk, str]]) -> tuple[TextGroup, ...]:
    """Keep the first representative of each hash in input order."""

    groups: dict[str, tuple[str, list[chunker.NewChunk]]] = {}
    for chunk, text in chunks_with_text:
        if chunk.content_hash not in groups:
            groups[chunk.content_hash] = (text, [])
        groups[chunk.content_hash][1].append(chunk)
    return tuple(TextGroup(text, tuple(chunks)) for text, chunks in groups.values())


def pack_requests(representatives: Sequence[TextGroup]) -> tuple[tuple[TextGroup, ...], ...]:
    """Pack texts greedily under both batch bounds, refusing an overlong text."""

    requests: list[tuple[TextGroup, ...]] = []
    batch: list[TextGroup] = []
    tokens = 0
    for representative in representatives:
        count = max(chunk.token_count for chunk in representative.chunks)
        if count > EMBED_TEXT_TOKEN_CAP:
            raise DenseFailure("chunk-too-long")
        if batch and (len(batch) >= EMBED_BATCH_TEXTS or tokens + count > EMBED_BATCH_TOKENS):
            requests.append(tuple(batch))
            batch, tokens = [], 0
        batch.append(representative)
        tokens += count
    if batch:
        requests.append(tuple(batch))
    return tuple(requests)


def _contains(reported: object, expected: Mapping[str, Any]) -> bool:
    if not isinstance(reported, Mapping):
        return False
    for key, value in expected.items():
        actual = reported.get(key)
        if isinstance(value, Mapping):
            if not _contains(actual, value):
                return False
        elif type(actual) is not type(value) or actual != value:
            return False
    return True


def config_matches(reported: Mapping[str, Any], dimensions: int) -> bool:
    """Compare the release's vector and graph keys while allowing store defaults."""

    return _contains(reported, {
        "params": {"vectors": {"size": dimensions, **COLLECTION_CONFIG["vectors"]}},
        "hnsw_config": COLLECTION_CONFIG["hnsw_config"],
        "quantization_config": COLLECTION_CONFIG["quantization_config"],
    })


DOCUMENTS_SQL: Final = lexical.DOCUMENTS_SQL + " LIMIT %s"
SCOPE_TOTAL_SQL: Final = (
    "SELECT COUNT(*) FROM chunks AS c JOIN (" + DOCUMENTS_SQL + ") AS scope "
    "ON scope.doc_id = c.doc_id WHERE c.pipeline = %s"
)
LEDGER_ROWS_SQL: Final = (
    "SELECT content_hash, gen_id, point_id FROM chunk_embeddings "
    "WHERE embedding_space = %s AND content_hash = ANY(%s)"
)
LEDGER_INSERT_SQL: Final = (
    "INSERT INTO chunk_embeddings (content_hash, embedding_space, gen_id, point_id) "
    "SELECT %s, embedding_space, gen_id, %s FROM index_generations WHERE gen_id = %s "
    "ON CONFLICT DO NOTHING"
)
LEDGER_REHOME_SQL: Final = (
    "UPDATE chunk_embeddings SET gen_id = %s, point_id = %s, embedded_at = now() "
    "WHERE content_hash = %s AND embedding_space = "
    "(SELECT embedding_space FROM index_generations WHERE gen_id = %s)"
)
START_SQL: Final = (
    "UPDATE index_generations SET counts = (counts - %s::text[]) || %s::jsonb WHERE gen_id = %s"
)
PROGRESS_SQL: Final = "UPDATE index_generations SET counts = counts || %s::jsonb WHERE gen_id = %s"
FINISH_SQL: Final = (
    "UPDATE index_generations SET counts = counts || %s::jsonb, dense_built_at = %s, "
    "built_at = CASE WHEN lexical_built_at IS NOT NULL THEN COALESCE(built_at, %s) ELSE NULL END, "
    "state = CASE WHEN state = 'building' AND lexical_built_at IS NOT NULL THEN 'built' ELSE state END "
    "WHERE gen_id = %s"
)


class PsycopgGenerations(lexical.PsycopgGenerations):
    """Reuse generation reads and connections, adding atomic dense ledger writes."""

    def scope_total(
        self, collection: str, source: str, snapshot: date, courts: Sequence[str],
        pipeline: str, limit: int | None = None,
    ) -> int:
        rows = self._run(SCOPE_TOTAL_SQL, (collection, source, snapshot, list(courts), limit, pipeline), read=True)
        if len(rows) != 1 or type(rows[0][0]) is not int or rows[0][0] < 0:
            raise DenseFailure("database")
        return rows[0][0]

    def documents(
        self, collection: str, source: str, snapshot: date, courts: Sequence[str],
        limit: int | None = None,
    ) -> Iterator[lexical.Document]:
        connection = self._connection(stream=True)
        try:
            with connection.cursor(name="dense_documents") as cursor:
                cursor.itersize = DOCUMENT_BATCH_SIZE
                cursor.execute(DOCUMENTS_SQL, (collection, source, snapshot, list(courts), limit))
                for row in cursor:
                    yield lexical.Document(*row)
            connection.commit()
        except psycopg.Error as exc:
            with suppress(psycopg.Error):
                connection.rollback()
            raise DenseFailure("database", error=type(exc).__name__) from exc

    def ledger_rows(self, space: str, hashes: Sequence[str]) -> tuple[LedgerRow, ...]:
        rows = self._run(LEDGER_ROWS_SQL, (space, list(hashes)), read=True)
        return tuple(LedgerRow(row[0], row[1], str(row[2])) for row in rows)

    def _write(
        self, statements: Sequence[tuple[str, tuple[object, ...]]],
    ) -> None:
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                for sql, parameters in statements:
                    cursor.execute(sql, parameters)
            connection.commit()
        except psycopg.Error as exc:
            with suppress(psycopg.Error):
                connection.rollback()
            raise DenseFailure("database", error=type(exc).__name__) from exc

    def start(self, name: str, counts: DenseCounts) -> None:
        self._run(START_SQL, (list(DENSE_COUNT_KEYS), json.dumps(asdict(counts)), name))

    # Dense progress replaces the lexical counter contract on the shared record.
    def progress(self, name: str, counts: DenseCounts, inserts: Sequence[LedgerRow] = (), rehomes: Sequence[LedgerRow] = ()) -> None:  # type: ignore[override]
        self._write([
            *((LEDGER_INSERT_SQL, (row.content_hash, row.point_id, row.gen_id)) for row in inserts),
            *((LEDGER_REHOME_SQL, (row.gen_id, row.point_id, row.content_hash, row.gen_id)) for row in rehomes),
            (PROGRESS_SQL, (json.dumps(asdict(counts)), name)),
        ])

    # Dense finish records a vector timestamp and limit instead of lexical bytes.
    def finish(self, name: str, counts: DenseCounts, at: datetime, limited: bool) -> None:  # type: ignore[override]
        if limited:
            self._run(PROGRESS_SQL, (json.dumps(asdict(counts)), name))
        else:
            self._run(FINISH_SQL, (json.dumps(asdict(counts)), at, at, name))


def _canonical_id(value: object) -> str:
    if not isinstance(value, str):
        raise DenseFailure("store", error="InvalidPointId")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise DenseFailure("store", error="InvalidPointId") from exc


def _vector(value: object, reason: str, *, dimensions: int | None = None) -> tuple[float, ...]:
    if not isinstance(value, list) or not 0 < len(value) <= DIMENSIONS_MAX or dimensions is not None and len(value) != dimensions:
        raise DenseFailure(reason, error="InvalidVector")
    try:
        if any(type(number) not in (int, float) or not math.isfinite(number) for number in value):
            raise DenseFailure(reason, error="InvalidVector")
        return tuple(float(number) for number in value)
    except OverflowError as exc:
        raise DenseFailure(reason, error="InvalidVector") from exc


class HttpxVectorStore:
    """Bounded Qdrant requests with endpoint-specific completion checks."""

    def __init__(
        self, worker_settings: settings.Settings | None = None, *,
        transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        try:
            configuration = worker_settings or settings.load_settings()
            self._client = httpx.Client(
                base_url=configuration.qdrant_url, headers={"api-key": settings.qdrant_key(configuration)},
                timeout=HTTP_TIMEOUT, follow_redirects=False, transport=transport,
            )
        except settings.WorkerSettingsError as exc:
            raise DenseFailure("local", error=type(exc).__name__) from exc
        self._sleep, self._monotonic = sleep, monotonic

    def _request(self, method: str, path: str, *, absent: bool = False, **kwargs: Any) -> httpx.Response:
        for attempt in range(len(RETRY_WAITS) + 1):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                raise DenseFailure("store", error=type(exc).__name__) from exc
            if response.status_code in {429, 503} and attempt < len(RETRY_WAITS):
                response.close()
                self._sleep(RETRY_WAITS[attempt])
                continue
            if not (200 <= response.status_code < 300 or absent and response.status_code == 404):
                raise DenseFailure("store", error=f"HttpStatus{response.status_code // 100}xx")
            return response
        raise AssertionError("bounded request loop ended without a response")

    @staticmethod
    def _result(response: httpx.Response) -> Any:
        try:
            value = response.json()
        except ValueError as exc:
            raise DenseFailure("store", error="InvalidReply") from exc
        if not isinstance(value, dict) or value.get("status") != "ok" or "result" not in value:
            raise DenseFailure("store", error="InvalidReply")
        return value["result"]

    def info(self, name: str) -> Mapping[str, Any] | None:
        response = self._request("GET", f"/collections/{name}", absent=True)
        if response.status_code == 404:
            return None
        result = self._result(response)
        if not isinstance(result, dict):
            raise DenseFailure("store", error="InvalidInfo")
        config = result.get("config")
        optimizer = result.get("optimizer_status")
        valid_optimizer = optimizer == "ok" or isinstance(optimizer, dict) and isinstance(optimizer.get("error"), str)
        if (
            result.get("status") not in ("green", "yellow", "grey", "red") or not valid_optimizer
            or any(type(result.get(key)) is not int or result[key] < 0 for key in ("points_count", "indexed_vectors_count"))
            or not isinstance(result.get("payload_schema"), dict)
            or not isinstance(config, dict)
        ):
            raise DenseFailure("store", error="InvalidInfo")
        try:
            vectors = config["params"]["vectors"]
            threshold = config["optimizer_config"]["indexing_threshold"]
            valid_config = (
                isinstance(vectors, dict) and type(vectors.get("size")) is int and vectors["size"] > 0
                and isinstance(vectors.get("distance"), str)
                and (vectors.get("datatype") is None or isinstance(vectors["datatype"], str))
                and (vectors.get("on_disk") is None or type(vectors["on_disk"]) is bool)
                and isinstance(config["hnsw_config"], dict)
                and (config.get("quantization_config") is None or isinstance(config["quantization_config"], dict))
                and type(threshold) is int and threshold >= 0
            )
        except (KeyError, TypeError):
            valid_config = False
        if not valid_config:
            raise DenseFailure("store", error="InvalidInfo")
        return result

    def create(self, name: str, size: int) -> None:
        result = self._result(self._request("PUT", f"/collections/{name}", json={
            **COLLECTION_CONFIG, "vectors": {"size": size, **COLLECTION_CONFIG["vectors"]},
        }))
        if result is not True:
            raise DenseFailure("store", error="UnconfirmedCreation")

    def ensure_indexes(self, name: str) -> None:
        for field, schema in PAYLOAD_INDEXES.items():
            result = self._result(self._request("PUT", f"/collections/{name}/index", params={"wait": "true"}, json={
                "field_name": field, "field_schema": schema,
            }))
            self._completed(result)

    def set_indexing(self, name: str, threshold: int) -> None:
        self._patch_optimizer(name, {"indexing_threshold": threshold})

    def _patch_optimizer(self, name: str, config: Mapping[str, int]) -> None:
        result = self._result(self._request("PATCH", f"/collections/{name}", json={"optimizers_config": config}))
        if result is not True:
            raise DenseFailure("store", error="UnconfirmedOptimizer")

    @staticmethod
    def _completed(result: object) -> None:
        if not isinstance(result, dict) or result.get("status") != "completed":
            raise DenseFailure("store", error="IncompleteOperation")

    def _points(
        self, name: str, ids: Sequence[str], *, vectors: bool,
    ) -> dict[str, tuple[float, ...] | lexical.TextKeys | None]:
        found: dict[str, tuple[float, ...] | lexical.TextKeys | None] = {}
        bound = RETRIEVE_BATCH if vectors else PRESENCE_BATCH
        for start in range(0, len(ids), bound):
            selected = [_canonical_id(identifier) for identifier in ids[start:start + bound]]
            response = self._request("POST", f"/collections/{name}/points", absent=vectors, json={
                "ids": selected, "with_payload": False if vectors else ["dupe_cluster", "boilerplate"], "with_vector": vectors,
            })
            if response.status_code == 404:
                return {}
            result = self._result(response)
            if not isinstance(result, list) or len(result) > len(selected):
                raise DenseFailure("store", error="InvalidPoints")
            seen: set[str] = set()
            for row in result:
                if not isinstance(row, dict):
                    raise DenseFailure("store", error="InvalidPoints")
                identifier = _canonical_id(row.get("id"))
                if identifier not in selected or identifier in seen:
                    raise DenseFailure("store", error="InvalidPoints")
                seen.add(identifier)
                if vectors:
                    found[identifier] = _vector(row.get("vector"), "store")
                else:
                    payload = row.get("payload", {})
                    if not isinstance(payload, dict):
                        raise DenseFailure("store", error="InvalidPoints")
                    cluster_id, boilerplate = payload.get("dupe_cluster"), payload.get("boilerplate")
                    found[identifier] = (
                        lexical.TextKeys(cluster_id, boilerplate)
                        if isinstance(cluster_id, str) and type(boilerplate) is bool else None
                    )
        return found

    def present(self, name: str, ids: Sequence[str]) -> dict[str, lexical.TextKeys | None]:
        return cast(dict[str, lexical.TextKeys | None], self._points(name, ids, vectors=False))

    def retrieve(self, name: str, ids: Sequence[str]) -> dict[str, tuple[float, ...]]:
        return cast(dict[str, tuple[float, ...]], self._points(name, ids, vectors=True))

    def upsert(self, name: str, points: Sequence[Point]) -> None:
        result = self._result(self._request("PUT", f"/collections/{name}/points", params={"wait": "true"}, json={
            "points": [asdict(point) for point in points],
        }))
        self._completed(result)

    def set_payload(self, name: str, ids: Sequence[str], payload: Mapping[str, str | bool]) -> None:
        for start in range(0, len(ids), PRESENCE_BATCH):
            selected = [_canonical_id(identifier) for identifier in ids[start:start + PRESENCE_BATCH]]
            result = self._result(self._request("POST", f"/collections/{name}/points/payload", params={"wait": "true"}, json={
                "payload": dict(payload), "points": selected,
            }))
            self._completed(result)

    def wait_green(self, name: str, progress: Callable[[int], None]) -> Mapping[str, Any]:
        last_progress = self._monotonic()
        last_trigger = last_progress - GREY_TRIGGER_SECONDS
        previous_green = False
        while True:
            info = self.info(name)
            if info is None or info["status"] == "red" or info["optimizer_status"] != "ok":
                raise DenseFailure("store", error="OptimizerFailure")
            now = self._monotonic()
            if now - last_progress >= LOG_SECONDS:
                progress(info["indexed_vectors_count"])
                last_progress = now
            if info["status"] == "grey" and now - last_trigger >= GREY_TRIGGER_SECONDS:
                self._patch_optimizer(name, {})
                last_trigger = now
            green = info["status"] == "green"
            if green and previous_green:
                return info
            previous_green = green
            self._sleep(POLL_SECONDS)

    def close(self) -> None:
        self._client.close()


class HttpxEmbedder:
    """Embed canonical slices with checked order, width, and token accounting."""

    def __init__(
        self, served_model: str, dimensions: int, worker_settings: settings.Settings | None = None, *,
        transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            not isinstance(served_model, str) or re.fullmatch(SERVED_MODEL_PATTERN, served_model) is None
            or type(dimensions) is not int or not 0 < dimensions <= DIMENSIONS_MAX
        ):
            raise DenseFailure("invalid")
        try:
            configuration = worker_settings or settings.load_settings()
            self._client = httpx.Client(
                base_url=configuration.embed_url, headers={"Authorization": f"Bearer {settings.embed_key(configuration)}"},
                timeout=EMBED_TIMEOUT, follow_redirects=False, transport=transport,
            )
        except settings.WorkerSettingsError as exc:
            raise DenseFailure("local", error=type(exc).__name__) from exc
        self._served_model, self._dimensions, self._sleep = served_model, dimensions, sleep

    def embed(self, texts: Sequence[str]) -> EmbedReply:
        if not texts:
            return EmbedReply((), 0)
        for attempt in range(len(EMBED_RETRY_WAITS) + 1):
            try:
                response = self._client.post("/v1/embeddings", json={"model": self._served_model, "input": list(texts)})
            except httpx.TransportError as exc:
                if attempt == len(EMBED_RETRY_WAITS):
                    raise DenseFailure("embed", error=type(exc).__name__) from exc
                self._sleep(EMBED_RETRY_WAITS[attempt])
                continue
            except httpx.HTTPError as exc:
                raise DenseFailure("embed", error=type(exc).__name__) from exc
            if response.status_code in {429, 503} and attempt < len(EMBED_RETRY_WAITS):
                response.close()
                self._sleep(EMBED_RETRY_WAITS[attempt])
                continue
            if not 200 <= response.status_code < 300:
                raise DenseFailure("embed", error=f"HttpStatus{response.status_code // 100}xx")
            return self._reply(response, len(texts))
        raise AssertionError("bounded embed loop ended without a response")

    def _reply(self, response: httpx.Response, count: int) -> EmbedReply:
        try:
            value = response.json()
            data, tokens = value["data"], value["usage"]["prompt_tokens"]
        except (ValueError, KeyError, TypeError) as exc:
            raise DenseFailure("embed", error="InvalidReply") from exc
        if not isinstance(data, list) or len(data) != count or type(tokens) is not int or tokens < 0:
            raise DenseFailure("embed", error="InvalidReply")
        vectors: list[tuple[float, ...]] = []
        for index, row in enumerate(data):
            if not isinstance(row, dict) or type(row.get("index")) is not int or row["index"] != index:
                raise DenseFailure("embed", error="InvalidOrder")
            vectors.append(_vector(row.get("embedding"), "embed", dimensions=self._dimensions))
        return EmbedReply(tuple(vectors), tokens)

    def close(self) -> None:
        self._client.close()


@dataclass(frozen=True, slots=True)
class _WriteOutcome:
    """One awaited request's ledger operations and counters."""

    inserts: tuple[LedgerRow, ...]
    rehomes: tuple[LedgerRow, ...]
    texts: int
    tokens: int
    points: int


@dataclass(slots=True)
class _Progress:
    """Main-thread counts, durable progress, and bounded log cadence."""

    name: str
    job: int
    record: GenerationRecord
    started: float
    monotonic: Callable[[], float]
    counts: DenseCounts
    last_log: float | None = None

    def elapsed(self, counts: DenseCounts) -> DenseCounts:
        return replace(counts, dense_seconds=round(max(0.0, self.monotonic() - self.started)))

    def commit(
        self, counts: DenseCounts, inserts: Sequence[LedgerRow] = (), rehomes: Sequence[LedgerRow] = (),
    ) -> None:
        now = self.monotonic()
        updated = replace(counts, dense_seconds=round(max(0.0, now - self.started)))
        if not updated.dense_hour_seconds and now - self.started >= HOUR_SECONDS:
            updated = replace(updated, dense_hour_seconds=updated.dense_seconds,
                              dense_hour_tokens=updated.dense_tokens,
                              dense_hour_written=updated.dense_copied + updated.dense_embedded)
        self.record.progress(self.name, updated, inserts, rehomes)
        self.counts = updated
        self.log()

    def log(self, *, final: bool = False) -> None:
        now = self.monotonic()
        previous = self.started if self.last_log is None else self.last_log
        if now - previous < LOG_SECONDS and not (final and self.last_log is None):
            return
        logger.info("job_id=%s generation=%s %s", self.job, self.name,
                    " ".join(f"{key}={value}" for key, value in asdict(self.counts).items()))
        self.last_log = now


def _parse_inputs(inputs: Mapping[str, object]) -> tuple[lexical._Inputs, str, int, int | None]:
    if not isinstance(inputs, Mapping):
        raise DenseFailure("invalid")
    values = dict(inputs)
    served_model = values.pop("served_model", None)
    dimensions = values.pop("dimensions", None)
    limit = values.pop("limit", None)
    if (
        not isinstance(served_model, str) or re.fullmatch(SERVED_MODEL_PATTERN, served_model) is None
        or type(dimensions) is not int or not 0 < dimensions <= DIMENSIONS_MAX
        or limit is not None and (type(limit) is not int or limit < 1)
    ):
        raise DenseFailure("invalid")
    try:
        parsed = lexical._parse_inputs(values)
    except lexical.LexicalFailure as exc:
        raise DenseFailure("invalid", error=exc.error) from exc
    return parsed, served_model, dimensions, cast(int | None, limit)


def _embed_request(
    name: str, groups: Sequence[TextGroup], existing: Mapping[str, LedgerRow],
    payloads: Mapping[str, Mapping[str, str | bool]], store: VectorStore, embedder: Embedder, pace: Pace,
) -> _WriteOutcome:
    pace.wait_before()
    try:
        reply = embedder.embed([group.text for group in groups])
    except httpx.HTTPError as exc:
        raise DenseFailure("embed", error=type(exc).__name__) from exc
    points: list[Point] = []
    inserts: list[LedgerRow] = []
    rehomes: list[LedgerRow] = []
    for group, vector in zip(groups, reply.vectors, strict=True):
        for chunk in group.chunks:
            points.append(Point(point_id(chunk.chunk_id), vector, payloads[chunk.chunk_id]))
        first = group.chunks[0]
        row = LedgerRow(first.content_hash, name, point_id(first.chunk_id))
        (rehomes if first.content_hash in existing else inserts).append(row)
    store.upsert(name, points)
    return _WriteOutcome(tuple(inserts), tuple(rehomes), len(groups), reply.prompt_tokens, len(points))


def _embed_batches(
    pool: ThreadPoolExecutor, name: str, batches: Sequence[tuple[TextGroup, ...]], existing: Mapping[str, LedgerRow],
    payloads: Mapping[str, Mapping[str, str | bool]], store: VectorStore, embedder: Embedder, pace: Pace,
    progress: _Progress, repairs: Sequence[LedgerRow], present: int, leveled: int,
) -> None:
    pending: dict[Future[_WriteOutcome], None] = {}
    remaining = iter(batches)
    failures: list[BaseException] = []
    failure_lock = threading.Lock()

    def write(batch: Sequence[TextGroup]) -> _WriteOutcome:
        try:
            return _embed_request(name, batch, existing, payloads, store, embedder, pace)
        except BaseException as exc:
            with failure_lock:
                if not failures:
                    failures.append(exc)
            raise

    def raise_failure() -> None:
        with failure_lock:
            if failures:
                raise failures[0]

    def submit() -> None:
        raise_failure()
        batch = next(remaining, None)
        if batch is not None:
            pending[pool.submit(write, batch)] = None

    try:
        for _index in range(BULK_WRITERS):
            submit()
        while pending:
            completed, _running = wait(pending, return_when=FIRST_COMPLETED)
            raise_failure()
            for future in tuple(pending):
                if future not in completed:
                    continue
                outcome = future.result()
                progress.commit(replace(
                    progress.counts,
                    dense_present=progress.counts.dense_present + present,
                    dense_leveled=progress.counts.dense_leveled + leveled,
                    dense_embedded=progress.counts.dense_embedded + outcome.points,
                    dense_texts=progress.counts.dense_texts + outcome.texts,
                    dense_tokens=progress.counts.dense_tokens + outcome.tokens,
                    dense_requests=progress.counts.dense_requests + 1,
                ), (*repairs, *outcome.inserts), outcome.rehomes)
                repairs, present, leveled = (), 0, 0
                del pending[future]
            raise_failure()
            for _index in range(BULK_WRITERS - len(pending)):
                submit()
    finally:
        # Cancel queued work and join running requests before either client closes.
        for future in pending:
            future.cancel()
        if pending:
            wait(pending)


def _fill(
    parsed: lexical._Inputs, limit: int | None, record: GenerationRecord, store: VectorStore,
    embedder: Embedder, pace: Pace, store_root: Path, host: BytesHost, progress: _Progress,
) -> None:
    source = parsed.snapshot[:-staging.DATE_SUFFIX_LENGTH]
    snapshot = date.fromisoformat(parsed.snapshot[-10:])
    stream = record.documents(parsed.collection, source, snapshot, tuple(sorted(parsed.courts)), limit)
    pool = ThreadPoolExecutor(max_workers=BULK_WRITERS)
    failed = False
    try:
        while documents := tuple(itertools.islice(stream, DOCUMENT_BATCH_SIZE)):
            chunks = {document.doc_id: record.chunks(document.doc_id, parsed.pipeline) for document in documents}
            all_chunks = [chunk for rows in chunks.values() for chunk in rows]
            ids = [point_id(chunk.chunk_id) for chunk in all_chunks]
            hashes = tuple(dict.fromkeys(chunk.content_hash for chunk in all_chunks))
            keys = record.keys_for(parsed.name, hashes) if hashes else {}
            present = store.present(parsed.name, ids) if ids else {}
            existing = {row.content_hash: row for row in record.ledger_rows(
                parsed.embedding_space, hashes,
            )}
            repairs: dict[str, LedgerRow] = {}
            missing: dict[str, list[chunker.NewChunk]] = {}
            present_count = 0
            levels: dict[lexical.TextKeys, list[str]] = {}
            for chunk in all_chunks:
                identifier = point_id(chunk.chunk_id)
                if identifier in present:
                    present_count += 1
                    current = keys[chunk.content_hash]
                    if present[identifier] != current:
                        levels.setdefault(current, []).append(identifier)
                    if chunk.content_hash not in existing:
                        repairs.setdefault(chunk.content_hash, LedgerRow(chunk.content_hash, parsed.name, identifier))
                else:
                    missing.setdefault(chunk.content_hash, []).append(chunk)
            homes: dict[str, list[LedgerRow]] = {}
            for content_hash in missing:
                if content_hash in existing:
                    row = existing[content_hash]
                    homes.setdefault(row.gen_id, []).append(row)
            copied: dict[str, tuple[float, ...]] = {}
            for home, rows in homes.items():
                vectors = store.retrieve(home, [row.point_id for row in rows])
                for row in rows:
                    vector = vectors.get(_canonical_id(row.point_id))
                    if vector is not None:
                        copied[row.content_hash] = vector
            payloads: dict[str, Mapping[str, str | bool]] = {}
            slices: list[tuple[chunker.NewChunk, str]] = []
            for document in documents:
                absent = [chunk for chunk in chunks[document.doc_id] if point_id(chunk.chunk_id) not in present]
                if not absent:
                    continue
                opinion = record.opinion(document.doc_id)
                for chunk in absent:
                    payloads[chunk.chunk_id] = payload_for(chunk, opinion, parsed.courts, keys[chunk.content_hash])
                fresh = [chunk for chunk in absent if chunk.content_hash not in copied]
                if fresh:
                    text = lexical.canonical_text(document, store_root, host)
                    slices.extend((chunk, lexical.slice_for(text, chunk)) for chunk in fresh)
            # Validate the entire batch before any embedding or point write.
            batches = pack_requests(group_by_hash(slices))
            leveled_count = 0
            for current, identifiers in levels.items():
                store.set_payload(parsed.name, identifiers, asdict(current))
                leveled_count += len(identifiers)
            if copied:
                points: list[Point] = []
                rehomes: list[LedgerRow] = []
                for content_hash, vector in copied.items():
                    selected = [Point(point_id(chunk.chunk_id), vector, payloads[chunk.chunk_id])
                                for chunk in missing[content_hash]]
                    points.extend(selected)
                    if existing[content_hash].gen_id != parsed.name:
                        rehomes.append(LedgerRow(content_hash, parsed.name, selected[0].id))
                store.upsert(parsed.name, points)
                progress.commit(replace(progress.counts,
                                        dense_present=progress.counts.dense_present + present_count,
                                        dense_leveled=progress.counts.dense_leveled + leveled_count,
                                        dense_copied=progress.counts.dense_copied + len(points)),
                                tuple(repairs.values()), tuple(rehomes))
                repairs, present_count, leveled_count = {}, 0, 0
            if batches:
                _embed_batches(pool, parsed.name, batches, existing, payloads, store, embedder, pace,
                               progress, tuple(repairs.values()), present_count, leveled_count)
            elif repairs or present_count or leveled_count:
                progress.commit(replace(progress.counts, dense_present=progress.counts.dense_present + present_count,
                                        dense_leveled=progress.counts.dense_leveled + leveled_count), tuple(repairs.values()))
    except BaseException:
        failed = True
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        close = getattr(stream, "close", None)
        if close is not None:
            if failed:
                with suppress(Exception):
                    close()
            else:
                close()


def build(
    inputs: Mapping[str, object], job: int, record: GenerationRecord, store: VectorStore,
    embedder: Embedder, pace: Pace, store_root: Path = gideon.host.cas.ROOT, *,
    work_root: Path = staging.WORK_ROOT, host: BytesHost | None = None,
    clock: Callable[[], datetime] = lexical._clock, monotonic: Callable[[], float] = time.monotonic,
) -> DenseCounts:
    """Fill, resume, or copy a generation's vectors and atomically record progress."""

    progress: _Progress | None = None
    try:
        try:
            parsed, _served_model, dimensions, limit = _parse_inputs(inputs)
            if type(job) is not int or job < 1:
                raise DenseFailure("invalid")
            progress = _Progress(parsed.name, job, record, monotonic(), monotonic, DenseCounts())
            generation = record.read(parsed.name)
            if generation is None or generation.gen_id != parsed.name or generation.inputs_hash != parsed.inputs_hash or (
                generation.collection, generation.pipeline, generation.embedding_space
            ) != (parsed.collection, parsed.pipeline, parsed.embedding_space):
                raise DenseFailure("generation-mismatch")
            if generation.state not in {"building", "built", "failed"}:
                raise DenseFailure("invalid")
            with lexical._generation_lock(work_root / f"{parsed.name}{DENSE_LOCK_SUFFIX}"):
                if generation.state == "failed":
                    record.resume(parsed.name)
                info = store.info(parsed.name)
                if info is None:
                    store.create(parsed.name, dimensions)
                elif not config_matches(info["config"], dimensions):
                    raise DenseFailure("config-mismatch")
                store.ensure_indexes(parsed.name)
                store.set_indexing(parsed.name, FILL_INDEXING_THRESHOLD)
                source = parsed.snapshot[:-staging.DATE_SUFFIX_LENGTH]
                snapshot = date.fromisoformat(parsed.snapshot[-10:])
                scope = (parsed.collection, source, snapshot, tuple(sorted(parsed.courts)), parsed.pipeline)
                progress.counts = DenseCounts(dense_total=record.scope_total(*scope),
                                             dense_scope=record.scope_total(*scope, limit), dense_limit=limit or 0)
                record.start(parsed.name, progress.counts)
                _fill(parsed, limit, record, store, embedder, pace, store_root, host or RealHost(), progress)
                store.set_indexing(parsed.name, SHIPPED_INDEXING_THRESHOLD)

                def indexed(count: int) -> None:
                    assert progress is not None
                    progress.commit(replace(progress.counts, dense_indexed=count))

                info = store.wait_green(parsed.name, indexed)
                counts = progress.elapsed(replace(progress.counts, dense_points=info["points_count"],
                                                  dense_indexed=info["indexed_vectors_count"]))
                if not counts.dense_hour_seconds and (limit is not None or monotonic() - progress.started >= HOUR_SECONDS):
                    counts = replace(counts, dense_hour_seconds=counts.dense_seconds,
                                     dense_hour_tokens=counts.dense_tokens,
                                     dense_hour_written=counts.dense_copied + counts.dense_embedded)
                record.finish(parsed.name, counts, clock(), limit is not None)
                progress.counts = counts
                try:
                    record.close()
                finally:
                    try:
                        store.close()
                    finally:
                        embedder.close()
        except lexical.LexicalFailure as exc:
            raise DenseFailure(exc.reason, error=exc.error) from exc
        except psycopg.Error as exc:
            raise DenseFailure("database", error=type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise DenseFailure("store", error=type(exc).__name__) from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DenseFailure("local", error=type(exc).__name__) from exc
    except BaseException as exc:
        if isinstance(exc, DenseFailure):
            name = inputs.get("name") if isinstance(inputs, Mapping) else None
            if isinstance(name, str) and re.fullmatch(lexical.GENERATION_PATTERN, name) is not None and len(name.encode()) < 255:
                if exc.reason != "busy":
                    with suppress(DenseFailure, lexical.LexicalFailure, psycopg.Error, OSError):
                        record.fail(name, exc.reason)
                if type(job) is int and job > 0:
                    if progress is None:
                        progress = _Progress(name, job, record, monotonic(), monotonic, DenseCounts())
                    progress.log(final=True)
        # Cleanup must not replace the first failure or outlive a writer request.
        for client in (record, store, embedder):
            with suppress(DenseFailure, lexical.LexicalFailure, psycopg.Error, httpx.HTTPError, OSError):
                client.close()
        raise
    else:
        assert progress is not None
        progress.log(final=True)
        return progress.counts
