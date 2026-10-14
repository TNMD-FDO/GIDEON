"""Write resumable lexical serving copies from recorded chunks and stored text."""

import fcntl
import hashlib
import itertools
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol, cast

import httpx
import psycopg

import gideon.host.cas
from gideon.host.report import Problem
from gideon.host.sysio import BytesHost, RealHost
from gideon.worker import (
    caselaw,
    chunker,
    citations,
    fetch,
    settings,
    staging,
    treatment,
)

LEXICAL_TASK: Final = "gideon.worker.tasks.lexical"
LEXICAL_QUEUE: Final = "index"
LEXICAL_FAILURE_REASONS: Final = frozenset({
    "invalid", "store", "database", "local", "busy", "mapping-mismatch",
    "text-mismatch", "missing-text", "generation-mismatch",
})
GENERATION_STATES: Final = ("building", "built", "live", "retired", "failed")
COLLECTION_NAMES: Final = ("caselaw",)
GENERATION_PATTERN: Final = r"(caselaw)__corpus-[0-9]{4}-[0-9]{2}-[0-9]{2}__[a-z0-9]+(?:-[a-z0-9]+)*"
LEXICAL_MAPPING_VERSION: Final = 1
LOCK_SUFFIX: Final = ".lexical.lock"
# exempt: document batches bound metadata held while looking up chunk presence.
DOCUMENT_BATCH_SIZE: Final = 64
# exempt: the id lookup is bounded independently of a document's chunk count.
MGET_BATCH_SIZE: Final = 1000
# exempt: bulk requests carry a few mebibytes of serving copies at most.
BULK_BYTES: Final = 4 * 1024 * 1024
# exempt: bounded connection, read, write, and pool waits keep store failures finite.
HTTP_TIMEOUT: Final = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
# exempt: three increasing waits bound retries of temporary store pressure.
RETRY_WAITS: Final = (0.5, 1.0, 2.0)
# exempt: one progress write and log per minute bound diagnostic overhead.
PROGRESS_SECONDS: Final = 60.0
# exempt: one shard is the starting layout for a single store node.
SHARD_COUNT: Final = 1
# exempt: a build disables periodic refresh and uses asynchronous translog sync.
BUILD_REFRESH: Final = "-1"
BUILD_DURABILITY: Final = "async"
# exempt: the normal refresh and translog defaults are restored after the build.
FINISH_SETTINGS: Final = {"index": {"refresh_interval": "1s", "translog.durability": "request"}}
# exempt: a keyword bound above any citation's length.
CITE_IGNORE_ABOVE: Final = 2048

CITATION_FORMS: Final = (
    ("§§", "sections"), ("§", "section"),
    ("U.S.C.", "usc"), ("C.F.R.", "cfr"), ("U.S.S.G.", "ussg"),
    ("Fed. R. Crim. P.", "fedrcrimp"), ("Fed. R. Civ. P.", "fedrcivp"),
    ("Fed. R. App. P.", "fedrappp"), ("Fed. R. Evid.", "fedrevid"),
)
INDEX_SETTINGS: Final[dict[str, Any]] = {
    "number_of_shards": SHARD_COUNT,
    "refresh_interval": BUILD_REFRESH,
    "translog.durability": BUILD_DURABILITY,
    "analysis": {
        "char_filter": {"citation_forms": {
            # Literal escapes survive the mapping parser's trimming of each side.
            "type": "mapping", "mappings": [rf"{form} => \u0020{token}\u0020" for form, token in CITATION_FORMS],
        }},
        "analyzer": {"citation": {
            "type": "custom", "char_filter": ["citation_forms"],
            "tokenizer": "standard", "filter": ["lowercase"],
        }},
    },
}
INDEX_MAPPING: Final[dict[str, Any]] = {
    "dynamic": "strict", "_meta": {"mapping_version": LEXICAL_MAPPING_VERSION},
    "properties": {
        "text": {"type": "text", "analyzer": "citation"},
        "cites": {"type": "keyword", "ignore_above": CITE_IGNORE_ABOVE},
        "caption": {"type": "text", "analyzer": "citation"},
        **{key: {"type": "keyword"} for key in (
            "court", "circuit", "precedential", "section_type", "dupe_cluster",
            "doc_id", "section_id", "content_hash",
        )},
        "decided_date": {"type": "date"}, "boilerplate": {"type": "boolean"},
        **{key: {"type": "integer"} for key in ("ordinal", "char_start", "char_end", "token_count")},
        **{key: {"type": "long"} for key in ("opinion_id", "cluster_id")},
    },
}

logger = logging.getLogger(__name__)


class LexicalFailure(Exception):
    """A content-free job refusal from the closed lexical failure vocabulary."""

    def __init__(self, reason: str, *, error: str | None = None) -> None:
        if reason not in LEXICAL_FAILURE_REASONS:
            raise ValueError("unknown lexical failure reason")
        self.reason, self.error = reason, error
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class Generation:
    """The recorded identity and state required by a lexical writer."""

    gen_id: str
    collection: str
    inputs_hash: str
    pipeline: str
    embedding_space: str
    state: str


@dataclass(frozen=True, slots=True)
class Document:
    """A ready document's identity and verified canonical text object name."""

    doc_id: str
    canonical_text_sha256: str


@dataclass(frozen=True, slots=True)
class BuildCounts:
    """The diagnostic counts read by the host's progress and finish rows."""

    ready: int = 0
    chunked: int = 0
    unchunked: int = 0
    indexed: int = 0
    present: int = 0
    seconds: int = 0
    leveled: int = 0


@dataclass(frozen=True, slots=True)
class TextKeys:
    """A text's canonical cluster hash and boilerplate flag."""

    dupe_cluster: str
    boilerplate: bool


@dataclass(frozen=True, slots=True)
class ServingDocument:
    """A chunk's serving copy and the record-derived fields in its mapping."""

    text: str
    cites: tuple[str, ...]
    caption: str
    court: str
    circuit: str | None
    decided_date: str
    precedential: str
    section_type: str
    doc_id: str
    section_id: str
    content_hash: str
    ordinal: int
    char_start: int
    char_end: int
    token_count: int
    opinion_id: int
    cluster_id: int
    dupe_cluster: str
    boilerplate: bool


class GenerationRecord(Protocol):
    """Read immutable scope metadata and commit the generation's build state."""

    def read(self, name: str) -> Generation | None: ...
    def resume(self, name: str) -> None: ...
    def progress(self, name: str, counts: BuildCounts) -> None: ...
    def finish(self, name: str, counts: BuildCounts, size: int, at: datetime) -> None: ...
    def fail(self, name: str, reason: str) -> None: ...
    def documents(self, collection: str, source: str, snapshot: date, courts: Sequence[str]) -> Iterator[Document]: ...
    def chunks(self, doc_id: str, pipeline: str) -> tuple[chunker.NewChunk, ...]: ...
    def citations(self, doc_id: str) -> tuple[citations.NewCitation, ...]: ...
    def opinion(self, doc_id: str) -> caselaw.NewOpinion: ...
    def keys_for(self, name: str, hashes: Sequence[str]) -> dict[str, TextKeys]: ...
    def close(self) -> None: ...


class LexicalStore(Protocol):
    """Create a mapped index, test chunk presence, and write serving copies."""

    def mapping_version(self, name: str) -> int | None: ...
    def create(self, name: str) -> None: ...
    def present(self, name: str, ids: Sequence[str]) -> dict[str, TextKeys | None]: ...
    def bulk_create(self, name: str, documents: Sequence[tuple[str, ServingDocument]]) -> tuple[int, int]: ...
    def bulk_update(self, name: str, updates: Sequence[tuple[str, TextKeys]]) -> int: ...
    def refresh(self, name: str) -> None: ...
    def bytes(self, name: str) -> int: ...
    def close(self) -> None: ...


def slice_for(text: str, chunk: chunker.NewChunk) -> str:
    """Take and hash-check the canonical code-point span of a chunk."""

    if not 0 <= chunk.char_start < chunk.char_end <= len(text):
        raise LexicalFailure("text-mismatch")
    sliced = text[chunk.char_start:chunk.char_end]
    if hashlib.sha256(sliced.encode()).hexdigest() != chunk.content_hash:
        raise LexicalFailure("text-mismatch")
    return sliced


def cites_for(chunk: chunker.NewChunk, rows: Sequence[citations.NewCitation]) -> tuple[str, ...]:
    """Deduplicate the citation keys contained in a chunk in span order."""

    values: dict[str, None] = {}
    for row in sorted(rows, key=lambda item: (item.char_start, item.char_end, item.ordinal)):
        if chunk.char_start <= row.char_start and row.char_end <= chunk.char_end:
            value = row.reporter_cite or row.key
            if value:
                values[value] = None
    return tuple(values)


def caption_for(opinion: caselaw.NewOpinion) -> str:
    """Join the recorded reporter cites, docket, court, and decided date."""

    parts = (*opinion.reporter_cites, opinion.docket, opinion.court, opinion.decided_date.isoformat())
    return " ".join(" ".join(part.split()) for part in parts if part)


def serving_document(
    chunk: chunker.NewChunk, text_slice: str, opinion: caselaw.NewOpinion,
    rows: Sequence[citations.NewCitation], geography: Mapping[str, treatment.Geography],
    keys: TextKeys,
) -> ServingDocument:
    """Project one chunk and its recorded opinion into the declared mapping."""

    return ServingDocument(
        text_slice, cites_for(chunk, rows), caption_for(opinion), opinion.court,
        geography[opinion.court].circuit, opinion.decided_date.isoformat(), opinion.precedential,
        chunk.section_type, chunk.doc_id, chunk.section_id, chunk.content_hash,
        chunk.ordinal, chunk.char_start, chunk.char_end, chunk.token_count,
        opinion.opinion_id, opinion.cluster_id, keys.dupe_cluster, keys.boilerplate,
    )


READ_SQL: Final = "SELECT gen_id, collection, inputs_hash, pipeline, embedding_space, state FROM index_generations WHERE gen_id = %s"
RESUME_SQL: Final = "UPDATE index_generations SET state = 'building', failure_reason = NULL WHERE gen_id = %s AND state = 'failed'"
PROGRESS_SQL: Final = "UPDATE index_generations SET counts = counts || %s::jsonb WHERE gen_id = %s"
FINISH_SQL: Final = (
    "UPDATE index_generations SET counts = counts || %s::jsonb, bytes = %s, lexical_built_at = %s, "
    "built_at = CASE WHEN dense_built_at IS NOT NULL THEN COALESCE(built_at, %s) ELSE NULL END, "
    "state = CASE WHEN state = 'building' AND dense_built_at IS NOT NULL THEN 'built' ELSE state END "
    "WHERE gen_id = %s"
)
FAIL_SQL: Final = (
    "UPDATE index_generations SET state = 'failed', failure_reason = %s "
    "WHERE gen_id = %s AND state IN ('building', 'built', 'failed')"
)
DOCUMENTS_SQL: Final = (
    "SELECT d.doc_id, d.canonical_text_sha256 FROM documents AS d "
    "JOIN opinions AS o ON o.doc_id = d.doc_id "
    "WHERE d.profile = %s AND d.source = %s AND d.source_snapshot = %s "
    "AND o.court = ANY(%s) AND d.status = 'ready' AND d.superseded_by IS NULL ORDER BY d.doc_id"
)
CHUNKS_SQL: Final = (
    "SELECT chunk_id, doc_id, pipeline, ordinal, section_id, section_type, char_start, char_end, "
    "content_hash, token_count FROM chunks WHERE doc_id = %s AND pipeline = %s ORDER BY ordinal"
)
OPINION_SQL: Final = (
    "SELECT opinion_id, doc_id, court, cluster_id, docket, decided_date, decided_date_is_approximate, "
    "precedential, precedential_raw, reporter_cites, opinion_type FROM opinions WHERE doc_id = %s"
)
KEYS_SQL: Final = (
    "SELECT h.content_hash, COALESCE(m.cluster_id, h.content_hash), COALESCE(k.boilerplate, false) "
    "FROM unnest(%s::text[]) AS h(content_hash) "
    "LEFT JOIN dupe_members AS m ON m.gen_id = %s AND m.content_hash = h.content_hash "
    "LEFT JOIN dupe_clusters AS k ON k.gen_id = %s "
    "AND k.cluster_id = COALESCE(m.cluster_id, h.content_hash)"
)


class PsycopgGenerations:
    """Stream on one transaction and commit record operations on another."""

    def __init__(self, *, connect: Callable[..., Any] = psycopg.connect, worker_settings: settings.Settings | None = None) -> None:
        self._connect, self._settings = connect, worker_settings
        self._stream: Any | None = None
        self._writer: Any | None = None

    def _connection(self, *, stream: bool = False) -> Any:
        connection = self._stream if stream else self._writer
        if connection is None:
            try:
                configuration = self._settings or settings.load_settings()
                connection = self._connect(**settings.connection_kwargs(configuration), autocommit=False)
            except psycopg.Error as exc:
                raise LexicalFailure("database", error=type(exc).__name__) from exc
            except settings.WorkerSettingsError as exc:
                raise LexicalFailure("local", error=type(exc).__name__) from exc
            if stream:
                self._stream = connection
            else:
                self._writer = connection
        return connection

    def _run(self, sql: str, parameters: tuple[object, ...], *, read: bool = False) -> list[tuple[Any, ...]]:
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, parameters)
                rows = cursor.fetchall() if read else []
            connection.commit()
            return cast(list[tuple[Any, ...]], rows)
        except psycopg.Error as exc:
            with suppress(psycopg.Error):
                connection.rollback()
            raise LexicalFailure("database", error=type(exc).__name__) from exc

    def read(self, name: str) -> Generation | None:
        rows = self._run(READ_SQL, (name,), read=True)
        return Generation(*rows[0]) if rows else None

    def resume(self, name: str) -> None:
        self._run(RESUME_SQL, (name,))

    def progress(self, name: str, counts: BuildCounts) -> None:
        self._run(PROGRESS_SQL, (json.dumps(asdict(counts)), name))

    def finish(self, name: str, counts: BuildCounts, size: int, at: datetime) -> None:
        self._run(FINISH_SQL, (json.dumps(asdict(counts)), size, at, at, name))

    def fail(self, name: str, reason: str) -> None:
        self._run(FAIL_SQL, (reason, name))

    def documents(self, collection: str, source: str, snapshot: date, courts: Sequence[str]) -> Iterator[Document]:
        connection = self._connection(stream=True)
        try:
            with connection.cursor(name="lexical_documents") as cursor:
                cursor.itersize = DOCUMENT_BATCH_SIZE
                cursor.execute(DOCUMENTS_SQL, (collection, source, snapshot, list(courts)))
                for row in cursor:
                    yield Document(*row)
            connection.commit()
        except psycopg.Error as exc:
            with suppress(psycopg.Error):
                connection.rollback()
            raise LexicalFailure("database", error=type(exc).__name__) from exc

    def chunks(self, doc_id: str, pipeline: str) -> tuple[chunker.NewChunk, ...]:
        return tuple(chunker.NewChunk(*row) for row in self._run(CHUNKS_SQL, (doc_id, pipeline), read=True))

    def citations(self, doc_id: str) -> tuple[citations.NewCitation, ...]:
        return tuple(citations.NewCitation(*row) for row in self._run(caselaw.READ_CITATIONS_SQL, (doc_id,), read=True))

    def opinion(self, doc_id: str) -> caselaw.NewOpinion:
        rows = self._run(OPINION_SQL, (doc_id,), read=True)
        if len(rows) != 1:
            raise LexicalFailure("database")
        return caselaw.NewOpinion(*(*rows[0][:9], tuple(rows[0][9]), rows[0][10]))

    def keys_for(self, name: str, hashes: Sequence[str]) -> dict[str, TextKeys]:
        rows = self._run(KEYS_SQL, (list(hashes), name, name), read=True)
        keys: dict[str, TextKeys] = {}
        for content_hash, cluster_id, boilerplate in rows:
            if not isinstance(content_hash, str) or not isinstance(cluster_id, str) or type(boilerplate) is not bool:
                raise LexicalFailure("database")
            keys[content_hash] = TextKeys(cluster_id, boilerplate)
        if set(keys) != set(hashes):
            raise LexicalFailure("database")
        return keys

    def close(self) -> None:
        first_error: psycopg.Error | None = None
        for attribute in ("_stream", "_writer"):
            connection = getattr(self, attribute)
            if connection is not None:
                try:
                    connection.close()
                except psycopg.Error as exc:
                    first_error = first_error or exc
                finally:
                    setattr(self, attribute, None)
        if first_error is not None:
            raise LexicalFailure("database", error=type(first_error).__name__) from first_error


def _bulk_lines(
    action: Literal["create", "update"], item: tuple[str, ServingDocument | TextKeys],
) -> bytes:
    identifier, document = item
    body = asdict(document)
    if action == "update":
        body = {"doc": body}
    return (
        json.dumps({action: {"_id": identifier}}, separators=(",", ":")) + "\n"
        + json.dumps(body, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


class HttpxStore:
    """Bounded authenticated requests and checked item replies for one index."""

    def __init__(
        self, worker_settings: settings.Settings | None = None, *,
        transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        try:
            configuration = worker_settings or settings.load_settings()
            self._client = httpx.Client(
                base_url=configuration.opensearch_url, auth=settings.opensearch_auth(configuration),
                timeout=HTTP_TIMEOUT, follow_redirects=False, transport=transport,
            )
        except settings.WorkerSettingsError as exc:
            raise LexicalFailure("local", error=type(exc).__name__) from exc
        self._sleep = sleep

    def _request(self, method: str, path: str, *, absent: bool = False, **kwargs: Any) -> httpx.Response:
        for attempt in range(len(RETRY_WAITS) + 1):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                raise LexicalFailure("store", error=type(exc).__name__) from exc
            if response.status_code in {429, 503} and attempt < len(RETRY_WAITS):
                response.close()
                self._sleep(RETRY_WAITS[attempt])
                continue
            if not (200 <= response.status_code < 300 or absent and response.status_code == 404):
                raise LexicalFailure("store", error="HttpStatus")
            return response
        raise AssertionError("bounded request loop ended without a response")

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError as exc:
            raise LexicalFailure("store", error="InvalidReply") from exc
        if not isinstance(value, dict):
            raise LexicalFailure("store", error="InvalidReply")
        return value

    def mapping_version(self, name: str) -> int | None:
        response = self._request("GET", f"/{name}/_mapping", absent=True)
        if response.status_code == 404:
            return None
        try:
            version = self._json(response)[name]["mappings"]["_meta"]["mapping_version"]
        except (KeyError, TypeError) as exc:
            raise LexicalFailure("mapping-mismatch") from exc
        if type(version) is not int:
            raise LexicalFailure("mapping-mismatch")
        return version

    def create(self, name: str) -> None:
        value = self._json(self._request("PUT", f"/{name}", json={"settings": INDEX_SETTINGS, "mappings": INDEX_MAPPING}))
        if value.get("acknowledged") is not True:
            raise LexicalFailure("store", error="Unacknowledged")

    def present(self, name: str, ids: Sequence[str]) -> dict[str, TextKeys | None]:
        present: dict[str, TextKeys | None] = {}
        for start in range(0, len(ids), MGET_BATCH_SIZE):
            selected = list(ids[start:start + MGET_BATCH_SIZE])
            value = self._json(self._request("POST", f"/{name}/_mget", params={"_source_includes": "dupe_cluster,boilerplate"}, json={"ids": selected}))
            rows = value.get("docs")
            if not isinstance(rows, list) or len(rows) != len(selected):
                raise LexicalFailure("store", error="InvalidPresence")
            for identifier, row in zip(selected, rows, strict=True):
                if not isinstance(row, dict) or row.get("_id") != identifier or "error" in row or type(row.get("found")) is not bool:
                    raise LexicalFailure("store", error="InvalidPresence")
                if row["found"]:
                    source = row.get("_source", {})
                    if not isinstance(source, dict):
                        raise LexicalFailure("store", error="InvalidPresence")
                    cluster_id, boilerplate = source.get("dupe_cluster"), source.get("boilerplate")
                    present[identifier] = (
                        TextKeys(cluster_id, boilerplate)
                        if isinstance(cluster_id, str) and type(boilerplate) is bool else None
                    )
        return present

    def _send_bulk(
        self, name: str, action: Literal["create", "update"], documents: Sequence[tuple[str, bytes]],
    ) -> tuple[int, int]:
        indexed = present = 0
        pending = list(documents)
        for attempt in range(len(RETRY_WAITS) + 1):
            payload = b"".join(lines for _, lines in pending)
            value = self._json(self._request(
                "POST", f"/{name}/_bulk", content=payload,
                headers={"Content-Type": "application/x-ndjson"},
            ))
            items = value.get("items")
            if not isinstance(items, list) or len(items) != len(pending):
                raise LexicalFailure("store", error="InvalidBulkReply")
            retry: list[tuple[str, bytes]] = []
            failed = False
            for document, item in zip(pending, items, strict=True):
                reply = item.get(action) if isinstance(item, dict) else None
                if not isinstance(reply, dict) or reply.get("_id") != document[0]:
                    failed = True
                    continue
                status = reply.get("status")
                if type(status) is not int:
                    failed = True
                elif "error" not in reply and (
                    action == "create" and status == 201
                    or action == "update" and status == 200 and reply.get("result") in ("updated", "noop")
                ):
                    indexed += 1
                elif action == "create" and status == 409:
                    present += 1
                elif status in {429, 503}:
                    retry.append(document)
                else:
                    failed = True
            if failed:
                raise LexicalFailure("store", error="BulkItemError")
            if not retry:
                return indexed, present
            if attempt == len(RETRY_WAITS):
                raise LexicalFailure("store", error="BulkRetriesExhausted")
            self._sleep(RETRY_WAITS[attempt])
            pending = retry
        raise AssertionError("bounded bulk loop ended without an outcome")

    def _bulk(
        self, name: str, action: Literal["create", "update"], documents: Sequence[tuple[str, ServingDocument | TextKeys]],
    ) -> tuple[int, int]:
        indexed = present = size = 0
        batch: list[tuple[str, bytes]] = []
        for item in documents:
            lines = _bulk_lines(action, item)
            length = len(lines)
            if length > BULK_BYTES:
                raise LexicalFailure("store", error="BulkDocumentTooLarge")
            if batch and size + length > BULK_BYTES:
                created, found = self._send_bulk(name, action, batch)
                indexed, present = indexed + created, present + found
                batch, size = [], 0
            batch.append((item[0], lines))
            size += length
        if batch:
            created, found = self._send_bulk(name, action, batch)
            indexed, present = indexed + created, present + found
        return indexed, present

    def bulk_create(self, name: str, documents: Sequence[tuple[str, ServingDocument]]) -> tuple[int, int]:
        return self._bulk(name, "create", documents)

    def bulk_update(self, name: str, updates: Sequence[tuple[str, TextKeys]]) -> int:
        leveled, _present = self._bulk(name, "update", updates)
        return leveled

    def refresh(self, name: str) -> None:
        value = self._json(self._request("PUT", f"/{name}/_settings", json=FINISH_SETTINGS))
        if value.get("acknowledged") is not True:
            raise LexicalFailure("store", error="Unacknowledged")
        self._check_shards(self._json(self._request("POST", f"/{name}/_refresh")))
        # A flush commits the build's segments, so a store restart replays no translog.
        self._check_shards(self._json(self._request("POST", f"/{name}/_flush")))

    @staticmethod
    def _check_shards(value: dict[str, Any]) -> None:
        shards = value.get("_shards")
        if not isinstance(shards, dict) or type(shards.get("failed")) is not int or shards["failed"] != 0:
            raise LexicalFailure("store", error="ShardFailure")

    def bytes(self, name: str) -> int:
        value = self._json(self._request("GET", f"/{name}/_stats/store"))
        self._check_shards(value)
        try:
            size = value["indices"][name]["total"]["store"]["size_in_bytes"]
        except (KeyError, TypeError) as exc:
            raise LexicalFailure("store", error="InvalidStoreStats") from exc
        if type(size) is not int or size < 0:
            raise LexicalFailure("store", error="InvalidStoreStats")
        return size

    def close(self) -> None:
        self._client.close()


@dataclass(frozen=True, slots=True)
class _Inputs:
    name: str
    collection: str
    label: str
    snapshot: str
    courts: Mapping[str, treatment.Geography]
    pipeline: str
    embedding_space: str
    inputs_hash: str


def _parse_inputs(inputs: Mapping[str, object]) -> _Inputs:
    if not isinstance(inputs, Mapping) or set(inputs) != {
        "name", "collection", "label", "snapshot", "courts", "pipeline", "embedding_space", "inputs_hash",
    } or not all(isinstance(inputs[key], str) for key in inputs if key != "courts"):
        raise LexicalFailure("invalid")
    name, collection, space, digest = (cast(str, inputs[key]) for key in ("name", "collection", "embedding_space", "inputs_hash"))
    courts = inputs["courts"]
    if (
        re.fullmatch(GENERATION_PATTERN, name) is None or len(name.encode()) >= 255
        or collection not in COLLECTION_NAMES or name.split("__")[0] != collection
        or name.split("__")[2] != space or re.fullmatch(staging.DIGEST_PATTERN, digest) is None
        or not isinstance(courts, Mapping) or not courts
    ):
        raise LexicalFailure("invalid")
    try:
        geography = caselaw.validate_arguments(
            inputs["label"], inputs["snapshot"], next(iter(courts)), None,
            cast(Mapping[str, Mapping[str, object]], courts), inputs["pipeline"],
        )
    except caselaw.CaselawFailure as exc:
        raise LexicalFailure("invalid") from exc
    return _Inputs(name, collection, cast(str, inputs["label"]), cast(str, inputs["snapshot"]), geography,
                   cast(str, inputs["pipeline"]), space, digest)


@contextmanager
def _generation_lock(path: Path) -> Iterator[None]:
    descriptor = os.open(path, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, fetch.PARTIAL_MODE)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LexicalFailure("busy") from exc
        os.fchmod(descriptor, fetch.PARTIAL_MODE)
        yield
    finally:
        os.close(descriptor)


def _log(job: int, name: str, counts: BuildCounts) -> None:
    logger.info(
        "job_id=%s generation=%s ready=%s chunked=%s unchunked=%s indexed=%s present=%s seconds=%s",
        job, name, counts.ready, counts.chunked, counts.unchunked, counts.indexed, counts.present, counts.seconds,
    )


def canonical_text(document: Document, store_root: Path, host: BytesHost) -> str:
    """Read and decode one verified canonical object, with safe failure reasons."""

    path = gideon.host.cas.object_path(document.canonical_text_sha256, store_root)
    data = gideon.host.cas.get(host, document.canonical_text_sha256, root=store_root)
    if isinstance(data, Problem):
        raise LexicalFailure("text-mismatch" if host.exists(path) else "missing-text")
    try:
        return data.decode("utf-8")
    except UnicodeError as exc:
        raise LexicalFailure("text-mismatch") from exc


def _fill(
    inputs: _Inputs, job: int, record: GenerationRecord, store: LexicalStore,
    store_root: Path, host: BytesHost, monotonic: Callable[[], float], started: float,
) -> BuildCounts:
    counts = BuildCounts()
    progress_at = started
    source = inputs.snapshot[:-staging.DATE_SUFFIX_LENGTH]
    snapshot = date.fromisoformat(inputs.snapshot[-10:])
    stream = record.documents(inputs.collection, source, snapshot, tuple(sorted(inputs.courts)))
    try:
        while batch := tuple(itertools.islice(stream, DOCUMENT_BATCH_SIZE)):
            chunks = {document.doc_id: record.chunks(document.doc_id, inputs.pipeline) for document in batch}
            ids = [chunk.chunk_id for rows in chunks.values() for chunk in rows]
            hashes = list(dict.fromkeys(chunk.content_hash for rows in chunks.values() for chunk in rows))
            keys = record.keys_for(inputs.name, hashes) if hashes else {}
            present = store.present(inputs.name, ids) if ids else {}
            counts = replace(counts, present=counts.present + len(present))
            updates = [(chunk.chunk_id, keys[chunk.content_hash]) for rows in chunks.values() for chunk in rows
                       if chunk.chunk_id in present and present[chunk.chunk_id] != keys[chunk.content_hash]]
            if updates:
                counts = replace(counts, leveled=counts.leveled + store.bulk_update(inputs.name, updates))
            serving: list[tuple[str, ServingDocument]] = []
            for document in batch:
                rows = chunks[document.doc_id]
                counts = replace(counts, ready=counts.ready + 1,
                                 chunked=counts.chunked + bool(rows), unchunked=counts.unchunked + (not rows))
                missing = [chunk for chunk in rows if chunk.chunk_id not in present]
                if not missing:
                    continue
                text = canonical_text(document, store_root, host)
                opinion, citation_rows = record.opinion(document.doc_id), record.citations(document.doc_id)
                for chunk in missing:
                    serving.append((chunk.chunk_id, serving_document(chunk, slice_for(text, chunk), opinion, citation_rows, inputs.courts,
                                                                    keys[chunk.content_hash])))
            if serving:
                indexed, found = store.bulk_create(inputs.name, serving)
                counts = replace(counts, indexed=counts.indexed + indexed, present=counts.present + found)
            now = monotonic()
            counts = replace(counts, seconds=round(max(0.0, now - started)))
            if now - progress_at >= PROGRESS_SECONDS:
                record.progress(inputs.name, counts)
                _log(job, inputs.name, counts)
                progress_at = now
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()
    return counts


def _clock() -> datetime:
    return datetime.now(UTC)


def build(
    inputs: Mapping[str, object], job: int, record: GenerationRecord, store: LexicalStore,
    store_root: Path = gideon.host.cas.ROOT, *, work_root: Path = staging.WORK_ROOT,
    host: BytesHost | None = None, clock: Callable[[], datetime] = _clock,
    monotonic: Callable[[], float] = time.monotonic,
) -> BuildCounts:
    """Write missing chunks under a generation lock and record its outcome."""

    parsed: _Inputs | None = None
    counts = BuildCounts()
    try:
        try:
            parsed = _parse_inputs(inputs)
            if type(job) is not int or job < 1:
                raise LexicalFailure("invalid")
            started = monotonic()
            generation = record.read(parsed.name)
            if generation is None or generation.inputs_hash != parsed.inputs_hash or (
                generation.collection, generation.pipeline, generation.embedding_space
            ) != (parsed.collection, parsed.pipeline, parsed.embedding_space):
                raise LexicalFailure("generation-mismatch")
            if generation.state not in {"building", "built", "failed"}:
                raise LexicalFailure("invalid")
            with _generation_lock(work_root / f"{parsed.name}{LOCK_SUFFIX}"):
                if generation.state == "failed":
                    record.resume(parsed.name)
                version = store.mapping_version(parsed.name)
                if version is None:
                    store.create(parsed.name)
                elif version != LEXICAL_MAPPING_VERSION:
                    raise LexicalFailure("mapping-mismatch")
                counts = _fill(parsed, job, record, store, store_root, host or RealHost(), monotonic, started)
                store.refresh(parsed.name)
                size = store.bytes(parsed.name)
                counts = replace(counts, seconds=round(max(0.0, monotonic() - started)))
                record.finish(parsed.name, counts, size, clock())
                try:
                    record.close()
                finally:
                    store.close()
        except psycopg.Error as exc:
            raise LexicalFailure("database", error=type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise LexicalFailure("store", error=type(exc).__name__) from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LexicalFailure("local", error=type(exc).__name__) from exc
    except BaseException as exc:
        if isinstance(exc, LexicalFailure):
            name = inputs.get("name") if isinstance(inputs, Mapping) else None
            if isinstance(name, str) and re.fullmatch(GENERATION_PATTERN, name) is not None and len(name.encode()) < 255:
                # A busy job's generation belongs to the job holding its lock.
                if exc.reason != "busy":
                    with suppress(LexicalFailure, psycopg.Error, OSError):
                        record.fail(name, exc.reason)
                if type(job) is int and job > 0:
                    _log(job, name, counts)
        # Cleanup must not replace the job's first failure.
        with suppress(LexicalFailure, psycopg.Error, httpx.HTTPError, OSError):
            record.close()
        with suppress(LexicalFailure, httpx.HTTPError, OSError):
            store.close()
        raise
    else:
        assert parsed is not None
        _log(job, parsed.name, counts)
        return counts
