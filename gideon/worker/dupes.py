"""Sketch chunk texts and derive deterministic near-duplicate components."""

import hashlib
import itertools
import json
import logging
import math
import multiprocessing
import os
import re
import struct
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from functools import partial
from pathlib import Path
from threading import Lock
from typing import Final, Protocol, cast

import psycopg

import gideon.host.cas
from gideon.host.sysio import RealHost
from gideon.worker import chunker, lexical, staging, treatment

DUPES_TASK: Final = "gideon.worker.tasks.dupes"
DUPES_QUEUE: Final = "index"
DUPES_LOCK_PREFIX: Final = "dupes-"
DUPES_LOCK_SUFFIX: Final = ".dupes.lock"
DUPES_FAILURE_REASONS: Final = frozenset({
    "invalid", "database", "local", "busy", "text-mismatch", "missing-text",
    "generation-mismatch", "cluster-budget",
})
DUPES_COUNT_KEYS: Final = (
    "cluster_chunks", "cluster_texts", "cluster_documents", "cluster_clusters",
    "cluster_members", "cluster_grouped", "cluster_boilerplate", "cluster_flagged",
    "cluster_seconds", "cluster_reused",
)
# exempt: one hour is the clustering's starting budget; past it the clustering turns incremental.
CLUSTER_BUDGET_SECONDS: Final = 3_600
# E13: eight-word shingles are a starting value for near-duplicate detection.
SHINGLE_WORDS: Final = 8
# E13: 128 signature slots are a starting value for the similarity estimate.
SIGNATURE_SLOTS: Final = 128
# E13: 0.8 Jaccard similarity is the starting near-duplicate threshold.
JACCARD_THRESHOLD: Final = 0.8
AGREEMENT_SLOTS: Final = math.ceil(JACCARD_THRESHOLD * SIGNATURE_SLOTS)
# E13: twenty-five opinions are the starting boilerplate threshold.
BOILERPLATE_OPINIONS: Final = 25
# exempt: sixteen bands of eight slots put candidate recall below the verified threshold.
LSH_BANDS: Final = 16
LSH_ROWS: Final = 8
# exempt: each slot stores the hash minimum's low thirty-two bits in four bytes.
SIGNATURE_BYTES: Final = SIGNATURE_SLOTS * 4
# exempt: a band pass reads two mebibytes of signatures per call.
READ_SIGNATURES: Final = 4096
# exempt: eight sketch processes are a starting share of the box's shared cores.
CLUSTER_PROCESSES: Final = 8
# exempt: two tasks per process bound queued document metadata.
TASKS_IN_FLIGHT: Final = 2 * CLUSTER_PROCESSES
# exempt: document batches bound metadata held while streaming the scope.
DOCUMENT_BATCH_SIZE: Final = 64
# exempt: one progress write and log per minute bound diagnostic overhead.
PROGRESS_SECONDS: Final = 60
# E13: the court levels' standing order is the starting canonical preference.
COURT_LEVEL_RANK: Final = {level: rank for rank, level in enumerate(treatment.LEVELS)}
logger = logging.getLogger(__name__)


class DupesFailure(Exception):
    """A content-free refusal from the closed clustering failure vocabulary."""

    def __init__(self, reason: str, *, error: str | None = None) -> None:
        if reason not in DUPES_FAILURE_REASONS:
            raise ValueError("unknown clustering failure reason")
        self.reason, self.error = reason, error
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class DupesCounts:
    """Integer scope, clustering, and progress figures for one attempt."""

    cluster_chunks: int = 0
    cluster_texts: int = 0
    cluster_documents: int = 0
    cluster_clusters: int = 0
    cluster_members: int = 0
    cluster_grouped: int = 0
    cluster_boilerplate: int = 0
    cluster_flagged: int = 0
    cluster_seconds: int = 0
    cluster_reused: int = 0


@dataclass(frozen=True, slots=True)
class Document:
    """A ready document's canonical object, court, and decision date."""

    doc_id: str
    canonical_text_sha256: str
    court: str
    decided_date: date


class GenerationRecord(Protocol):
    """Read a generation's scope and atomically record its derived clustering."""

    def read(self, name: str) -> lexical.Generation | None: ...
    def resume(self, name: str) -> None: ...
    def fail(self, name: str, reason: str) -> None: ...
    def close(self) -> None: ...
    def scope(self, collection: str, source: str, snapshot: date, courts: Sequence[str], pipeline: str) -> tuple[int, int]: ...
    def documents_with_opinions(self, collection: str, source: str, snapshot: date, courts: Sequence[str]) -> Iterator[Document]: ...
    def chunks_for(self, doc_ids: Sequence[str], pipeline: str) -> tuple[chunker.NewChunk, ...]: ...
    def standing(self, name: str) -> tuple[datetime | None, int | None, int | None]: ...
    def start(self, name: str, counts: DupesCounts) -> None: ...
    def progress(self, name: str, counts: Mapping[str, int]) -> None: ...
    def write(
        self, name: str, mapping: Mapping[str, str], counts: DupesCounts,
        scope: tuple[str, str, date, Sequence[str], str], *,
        remaining: Callable[[], float], stamp: Callable[[], tuple[int, datetime]],
    ) -> DupesCounts: ...


class SignatureStore(Protocol):
    """Fixed-width signatures addressed by text index and streamed by band."""

    def open(self, path: Path, count: int) -> None: ...
    def put(self, index: int, sig: bytes) -> None: ...
    def get(self, index: int) -> bytes: ...
    def band(self, band: int) -> Iterator[tuple[bytes, int]]: ...
    def close(self, remove: bool) -> None: ...


def shingles(text: str) -> Iterator[bytes]:
    """Yield normalized word shingles, including one for a short or empty text."""

    words = text.lower().split()
    if len(words) <= SHINGLE_WORDS:
        yield " ".join(words).encode()
    else:
        for start in range(len(words) - SHINGLE_WORDS + 1):
            yield " ".join(words[start:start + SHINGLE_WORDS]).encode()


def signature(text: str) -> bytes:
    """Keep one hash minimum per slot, densify forward, and pack aligned slots."""

    bins: list[int | None] = [None] * SIGNATURE_SLOTS
    for shingle in shingles(text):
        digest = int.from_bytes(hashlib.blake2b(shingle, digest_size=8).digest(), "big")
        index, value = digest >> 57, digest & ((1 << 57) - 1)
        minimum = bins[index]
        if minimum is None or value < minimum:
            bins[index] = value
    # Walking backward from a filled slot carries each empty slot its next filled slot forward.
    filled = next(index for index, minimum in enumerate(bins) if minimum is not None)
    carry = 0
    for offset in range(SIGNATURE_SLOTS):
        index = (filled - offset) % SIGNATURE_SLOTS
        minimum = bins[index]
        if minimum is None:
            bins[index] = carry
        else:
            carry = minimum
    return struct.pack(f"<{SIGNATURE_SLOTS}I", *(cast(int, minimum) & ((1 << 32) - 1) for minimum in bins))


def agreement(a: bytes, b: bytes) -> int:
    """Count agreeing unsigned signature slots."""

    return sum(left == right for left, right in zip(
        struct.unpack(f"<{SIGNATURE_SLOTS}I", a),
        struct.unpack(f"<{SIGNATURE_SLOTS}I", b), strict=True,
    ))


def band_key(sig: bytes, band: int) -> bytes:
    """Return one contiguous band of signature slots."""

    if not 0 <= band < LSH_BANDS:
        raise ValueError("signature band is out of range")
    start = band * LSH_ROWS * 4
    return sig[start:start + LSH_ROWS * 4]


def canonical_key(level: str, decided_date: date, document_index: int) -> int:
    """Order members by court rank, decision date, and document stream index."""

    if not 0 <= document_index < 1 << 32:
        raise ValueError("document index is outside thirty-two bits")
    return (COURT_LEVEL_RANK[level] << 64) | (decided_date.toordinal() << 32) | document_index


def cluster(
    reader: SignatureStore, count: int, *, on_band: Callable[[int], None] | None = None,
) -> list[int]:
    """Verify band candidates against their first member and unite components."""

    roots = list(range(count))

    def find(index: int) -> int:
        while roots[index] != index:
            roots[index] = roots[roots[index]]
            index = roots[index]
        return index

    for band in range(LSH_BANDS):
        if on_band is not None:
            on_band(band)
        pairs = sorted(reader.band(band))
        for _, bucket in itertools.groupby(pairs, key=lambda pair: pair[0]):
            first: int | None = None
            first_sig: bytes | None = None
            for _, index in bucket:
                if first is None:
                    first = index
                    continue
                if first_sig is None:
                    first_sig = reader.get(first)
                if agreement(first_sig, reader.get(index)) >= AGREEMENT_SLOTS:
                    left, right = find(first), find(index)
                    roots[max(left, right)] = min(left, right)
    return [find(index) for index in range(count)]


def canonical_of(roots: Sequence[int], keys: Sequence[int], hashes: Sequence[str]) -> list[str]:
    """Map every text index to the minimum-key member's hash in its component."""

    canonical: dict[int, tuple[int, str]] = {}
    for root, key, content_hash in zip(roots, keys, hashes, strict=True):
        if root not in canonical or key < canonical[root][0]:
            canonical[root] = (key, content_hash)
    return [canonical[root][1] for root in roots]


class FileSignatures:
    """A private fixed-width work file with positional access and band streams."""

    def __init__(self) -> None:
        self._descriptor: int | None = None
        self._path: Path | None = None
        self._count = 0

    def open(self, path: Path, count: int) -> None:
        """Create or truncate the signature file to its scope's size."""

        if count < 0:
            raise ValueError("signature count is negative")
        self.close(remove=False)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        self._descriptor, self._path, self._count = descriptor, path, count
        try:
            os.fchmod(descriptor, 0o600)
            os.ftruncate(descriptor, count * SIGNATURE_BYTES)
        except OSError:
            with suppress(OSError):
                self.close(remove=True)
            raise

    def _fd(self) -> int:
        if self._descriptor is None:
            raise ValueError("signature file is closed")
        return self._descriptor

    def _offset(self, index: int) -> int:
        if not 0 <= index < self._count:
            raise ValueError("signature index is out of range")
        return index * SIGNATURE_BYTES

    def put(self, index: int, sig: bytes) -> None:
        """Write one complete signature at its assigned text index."""

        if len(sig) != SIGNATURE_BYTES:
            raise ValueError("signature has the wrong size")
        if os.pwrite(self._fd(), sig, self._offset(index)) != SIGNATURE_BYTES:
            raise OSError("signature write was incomplete")

    def get(self, index: int) -> bytes:
        """Read one complete signature without moving the stream position."""

        sig = os.pread(self._fd(), SIGNATURE_BYTES, self._offset(index))
        if len(sig) != SIGNATURE_BYTES:
            raise OSError("signature read was incomplete")
        return sig

    def band(self, band: int) -> Iterator[tuple[bytes, int]]:
        """Read signatures in one buffered sequential pass for the given band."""

        if not 0 <= band < LSH_BANDS:
            raise ValueError("signature band is out of range")
        start = band * LSH_ROWS * 4
        with os.fdopen(os.dup(self._fd()), "rb") as stream:
            stream.seek(0)
            for first in range(0, self._count, READ_SIGNATURES):
                size = min(READ_SIGNATURES, self._count - first) * SIGNATURE_BYTES
                block = stream.read(size)
                if len(block) != size:
                    raise OSError("signature stream was incomplete")
                for offset in range(0, len(block), SIGNATURE_BYTES):
                    yield block[offset + start:offset + start + LSH_ROWS * 4], first + offset // SIGNATURE_BYTES

    def close(self, remove: bool) -> None:
        """Close the work file and optionally remove it, including after a prior close."""

        descriptor, self._descriptor = self._descriptor, None
        first_error: OSError | None = None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                first_error = exc
        if remove and self._path is not None:
            try:
                self._path.unlink(missing_ok=True)
            except OSError as exc:
                first_error = first_error or exc
            else:
                self._path = None
        if first_error is not None:
            raise first_error


@dataclass(frozen=True, slots=True)
class SketchTask:
    """One canonical object and its first-seen indexed chunk spans and hashes."""

    canonical_text_sha256: str
    chunks: tuple[tuple[int, int, int, str], ...]


def sketch(task: SketchTask, store_root: Path) -> tuple[tuple[int, bytes], ...]:
    """Read verified canonical text and sketch each verified chunk span."""

    try:
        text = lexical.canonical_text(lexical.Document("", task.canonical_text_sha256, "", 0), store_root, RealHost())
        result: list[tuple[int, bytes]] = []
        for index, start, end, content_hash in task.chunks:
            chunk = chunker.NewChunk("", "", "", index, "", "", start, end, content_hash, 0)
            result.append((index, signature(lexical.slice_for(text, chunk))))
        return tuple(result)
    except lexical.LexicalFailure as exc:
        raise DupesFailure(exc.reason, error=exc.error) from exc
    except Exception as exc:
        raise DupesFailure("local", error=type(exc).__name__) from exc


SCOPE_SQL: Final = (
    "SELECT COUNT(*), COUNT(DISTINCT c.content_hash) FROM chunks AS c JOIN ("
    + lexical.DOCUMENTS_SQL + ") AS scope ON scope.doc_id = c.doc_id WHERE c.pipeline = %s"
)
DOCUMENTS_SQL: Final = lexical.DOCUMENTS_SQL.replace(
    "SELECT d.doc_id, d.canonical_text_sha256, d.source, o.opinion_id",
    "SELECT d.doc_id, d.canonical_text_sha256, o.court, o.decided_date",
)
CHUNKS_SQL: Final = lexical.CHUNKS_SQL.replace(
    "doc_id = %s", "doc_id = ANY(%s)",
).replace("ORDER BY ordinal", "ORDER BY doc_id, ordinal")
STANDING_SQL: Final = (
    "SELECT clustered_at, (counts->>'cluster_chunks')::bigint, "
    "(counts->>'cluster_texts')::bigint FROM index_generations WHERE gen_id = %s"
)
START_SQL: Final = (
    "UPDATE index_generations SET counts = (counts - %s::text[]) || %s::jsonb, "
    "clustered_at = NULL WHERE gen_id = %s"
)
PROGRESS_SQL: Final = "UPDATE index_generations SET counts = counts || %s::jsonb WHERE gen_id = %s"
MAPPING_SQL: Final = (
    "CREATE TEMPORARY TABLE dupe_mapping (content_hash text PRIMARY KEY, "
    "cluster_id text NOT NULL) ON COMMIT DROP"
)
COPY_SQL: Final = "COPY dupe_mapping (content_hash, cluster_id) FROM STDIN"
CLUSTERS_SQL: Final = (
    "INSERT INTO dupe_clusters (gen_id, cluster_id, chunk_count, opinion_count, boilerplate) "
    "SELECT %s, m.cluster_id, COUNT(*), COUNT(DISTINCT c.doc_id), "
    "COUNT(DISTINCT c.doc_id) >= %s FROM chunks AS c JOIN ("
    + lexical.DOCUMENTS_SQL + ") AS scope ON scope.doc_id = c.doc_id "
    "JOIN dupe_mapping AS m ON m.content_hash = c.content_hash WHERE c.pipeline = %s "
    "GROUP BY m.cluster_id HAVING COUNT(*) >= 2"
)
MEMBERS_SQL: Final = (
    "INSERT INTO dupe_members (gen_id, content_hash, cluster_id) "
    "SELECT %s, m.content_hash, m.cluster_id FROM dupe_mapping AS m JOIN dupe_clusters AS k "
    "ON k.gen_id = %s AND k.cluster_id = m.cluster_id WHERE m.cluster_id <> m.content_hash"
)
COUNTS_SQL: Final = (
    "SELECT COUNT(*), (SELECT COUNT(*) FROM dupe_members WHERE gen_id = %s), "
    "COUNT(*) FILTER (WHERE boilerplate), COALESCE(SUM(chunk_count) FILTER (WHERE boilerplate), 0), "
    "COALESCE(SUM(chunk_count), 0) "
    "FROM dupe_clusters WHERE gen_id = %s"
)
TIMEOUT_SQL: Final = "SELECT set_config('statement_timeout', %s, true)"
FINISH_SQL: Final = (
    "UPDATE index_generations SET counts = counts || %s::jsonb, clustered_at = %s WHERE gen_id = %s"
)


class PsycopgGenerations(lexical.PsycopgGenerations):
    """Stream immutable chunk metadata and replace one generation's clustering."""

    def scope(
        self, collection: str, source: str, snapshot: date, courts: Sequence[str], pipeline: str,
    ) -> tuple[int, int]:
        rows = self._run(SCOPE_SQL, (collection, source, snapshot, list(courts), pipeline), read=True)
        if len(rows) != 1:
            raise DupesFailure("database")
        return int(rows[0][0]), int(rows[0][1])

    def documents_with_opinions(
        self, collection: str, source: str, snapshot: date, courts: Sequence[str],
    ) -> Iterator[Document]:
        connection = self._connection(corpus=True)
        try:
            with connection.cursor(name="dupes_documents") as cursor:
                cursor.itersize = DOCUMENT_BATCH_SIZE
                cursor.execute(DOCUMENTS_SQL, (collection, source, snapshot, list(courts)))
                for row in cursor:
                    yield Document(*row)
            connection.commit()
        except psycopg.Error as exc:
            with suppress(psycopg.Error):
                connection.rollback()
            raise DupesFailure("database", error=type(exc).__name__) from exc

    def chunks_for(self, doc_ids: Sequence[str], pipeline: str) -> tuple[chunker.NewChunk, ...]:
        return tuple(chunker.NewChunk(*row) for row in self._run(CHUNKS_SQL, (list(doc_ids), pipeline), read=True))

    def standing(self, name: str) -> tuple[datetime | None, int | None, int | None]:
        rows = self._run(STANDING_SQL, (name,), read=True)
        if len(rows) != 1:
            raise DupesFailure("database")
        at, chunks, texts = rows[0]
        return at, int(chunks) if chunks is not None else None, int(texts) if texts is not None else None

    def start(self, name: str, counts: DupesCounts) -> None:
        self._run(START_SQL, (list(DUPES_COUNT_KEYS), json.dumps(asdict(counts)), name))

    # Clustering progress merges a chosen subset of its keys, so a reuse leaves the rest standing.
    def progress(self, name: str, counts: Mapping[str, int]) -> None:  # type: ignore[override]
        self._run(PROGRESS_SQL, (json.dumps(dict(counts)), name))

    def write(
        self, name: str, mapping: Mapping[str, str], counts: DupesCounts,
        scope: tuple[str, str, date, Sequence[str], str], *,
        remaining: Callable[[], float], stamp: Callable[[], tuple[int, datetime]],
    ) -> DupesCounts:
        connection = self._connection()
        try:
            with connection.cursor() as cursor:

                def bound() -> None:
                    # Every statement is cancelled at the budget's end, so a slow write rolls back.
                    cursor.execute(TIMEOUT_SQL, (f"{max(1, math.ceil(remaining() * 1000))}ms",))

                bound()
                cursor.execute(MAPPING_SQL)
                bound()
                with cursor.copy(COPY_SQL) as copy:
                    for row in mapping.items():
                        copy.write_row(row)
                collection, source, snapshot, courts, pipeline = scope
                for sql, parameters in (
                    ("ANALYZE dupe_mapping", ()),
                    ("DELETE FROM dupe_members WHERE gen_id = %s", (name,)),
                    ("DELETE FROM dupe_clusters WHERE gen_id = %s", (name,)),
                    (CLUSTERS_SQL, (name, BOILERPLATE_OPINIONS, collection, source, snapshot, list(courts), pipeline)),
                    (MEMBERS_SQL, (name, name)),
                    (COUNTS_SQL, (name, name)),
                ):
                    bound()
                    cursor.execute(sql, parameters or None)
                clusters, members, boilerplate, flagged, grouped = cursor.fetchone()
                seconds, at = stamp()
                merged = replace(counts, cluster_clusters=int(clusters), cluster_members=int(members),
                                 cluster_grouped=int(grouped), cluster_boilerplate=int(boilerplate),
                                 cluster_flagged=int(flagged), cluster_seconds=seconds, cluster_reused=0)
                bound()
                cursor.execute(FINISH_SQL, (json.dumps(asdict(merged)), at, name))
            connection.commit()
            return merged
        except DupesFailure:
            with suppress(psycopg.Error):
                connection.rollback()
            raise
        except psycopg.errors.QueryCanceled as exc:
            with suppress(psycopg.Error):
                connection.rollback()
            raise DupesFailure("cluster-budget", error=type(exc).__name__) from exc
        except psycopg.Error as exc:
            with suppress(psycopg.Error):
                connection.rollback()
            raise DupesFailure("database", error=type(exc).__name__) from exc


class Sketcher(Protocol):
    """Submit document sketches and join or cancel outstanding process work."""

    def submit(self, task: SketchTask) -> Future[tuple[tuple[int, bytes], ...]]: ...
    def close(self) -> None: ...


class ProcessSketcher:
    """Lazily sketch verified chunk spans in a bounded spawned process pool."""

    def __init__(self, store_root: Path) -> None:
        self._store_root = store_root
        self._pool: ProcessPoolExecutor | None = None

    def submit(self, task: SketchTask) -> Future[tuple[tuple[int, bytes], ...]]:
        if self._pool is None:
            self._pool = ProcessPoolExecutor(max_workers=CLUSTER_PROCESSES, mp_context=multiprocessing.get_context("spawn"))
        return self._pool.submit(partial(sketch, store_root=self._store_root), task)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(cancel_futures=True)
            self._pool = None


def _log(job: int, name: str, counts: DupesCounts, bands: int = 0) -> None:
    logger.info(
        "job_id=%s generation=%s documents=%s texts=%s bands=%s seconds=%s",
        job, name, counts.cluster_documents, counts.cluster_texts, bands, counts.cluster_seconds,
    )


def _sketch_scope(
    parsed: lexical._Inputs, job: int, record: GenerationRecord, signatures: SignatureStore,
    sketcher: Sketcher, counts: DupesCounts, started: float, monotonic: Callable[[], float],
    scope: tuple[str, str, date, Sequence[str], str],
) -> tuple[DupesCounts, list[int], list[str]]:
    indexes: dict[str, int] = {}
    keys: list[int] = []
    pending: dict[Future[tuple[tuple[int, bytes], ...]], None] = {}
    failures: list[BaseException] = []
    failure_lock = Lock()

    def completed(future: Future[tuple[tuple[int, bytes], ...]]) -> None:
        if not future.cancelled():
            failure = future.exception()
            if failure is not None:
                with failure_lock:
                    if not failures:
                        failures.append(failure)

    def raise_failure() -> None:
        with failure_lock:
            if failures:
                raise failures[0]

    def drain() -> None:
        done, _running = wait(pending, return_when=FIRST_COMPLETED)
        raise_failure()
        for future in tuple(pending):
            if future in done:
                for index, sig in future.result():
                    if not 0 <= index < counts.cluster_texts:
                        raise DupesFailure("database")
                    signatures.put(index, sig)
                del pending[future]

    progress_at = started
    stream = record.documents_with_opinions(*scope[:4])
    try:
        while batch := tuple(itertools.islice(stream, DOCUMENT_BATCH_SIZE)):
            elapsed = max(0.0, monotonic() - started)
            if elapsed > CLUSTER_BUDGET_SECONDS:
                raise DupesFailure("cluster-budget")
            chunks: dict[str, list[chunker.NewChunk]] = {}
            for chunk in record.chunks_for([document.doc_id for document in batch], parsed.pipeline):
                chunks.setdefault(chunk.doc_id, []).append(chunk)
            for offset, document in enumerate(batch):
                key = canonical_key(parsed.courts[document.court].level, document.decided_date,
                                    counts.cluster_documents + offset)
                new: list[tuple[int, int, int, str]] = []
                for chunk in sorted(chunks.get(document.doc_id, ()), key=lambda row: row.ordinal):
                    index = indexes.get(chunk.content_hash)
                    if index is None:
                        index = len(indexes)
                        if index >= counts.cluster_texts:
                            raise DupesFailure("database")
                        indexes[chunk.content_hash] = index
                        keys.append(key)
                        new.append((index, chunk.char_start, chunk.char_end, chunk.content_hash))
                    else:
                        keys[index] = min(keys[index], key)
                if new:
                    while len(pending) >= TASKS_IN_FLIGHT:
                        drain()
                    raise_failure()
                    future = sketcher.submit(SketchTask(document.canonical_text_sha256, tuple(new)))
                    pending[future] = None
                    future.add_done_callback(completed)
            counts = replace(counts, cluster_documents=counts.cluster_documents + len(batch),
                             cluster_seconds=round(max(0.0, monotonic() - started)))
            now = monotonic()
            if now - progress_at >= PROGRESS_SECONDS:
                record.progress(parsed.name, asdict(counts))
                _log(job, parsed.name, counts)
                progress_at = now
        while pending:
            drain()
        raise_failure()
        if len(indexes) != counts.cluster_texts:
            raise DupesFailure("database")
    except BaseException:
        with suppress(DupesFailure, lexical.LexicalFailure, psycopg.Error, OSError, ValueError, KeyError, TypeError):
            if hasattr(stream, "close"):
                stream.close()
        raise
    else:
        if hasattr(stream, "close"):
            stream.close()
    finally:
        for future in pending:
            future.cancel()
    return counts, keys, list(indexes)


def build(
    inputs: Mapping[str, object], job: int, record: GenerationRecord, signatures: SignatureStore,
    sketcher: Sketcher, store_root: Path = gideon.host.cas.ROOT, *,
    work_root: Path = staging.WORK_ROOT, clock: Callable[[], datetime] = lexical._clock,
    monotonic: Callable[[], float] = time.monotonic,
) -> DupesCounts:
    """Cluster a verified generation scope under its lock and record one atomic result."""

    counts = DupesCounts()
    try:
        try:
            parsed = lexical._parse_inputs(inputs)
            if type(job) is not int or job < 1:
                raise DupesFailure("invalid")
            generation = record.read(parsed.name)
            if generation is None or generation.gen_id != parsed.name or generation.inputs_hash != parsed.inputs_hash or (
                generation.collection, generation.pipeline, generation.embedding_space
            ) != (parsed.collection, parsed.pipeline, parsed.embedding_space):
                raise DupesFailure("generation-mismatch")
            if generation.state not in {"building", "built", "failed"}:
                raise DupesFailure("invalid")
            with lexical._generation_lock(work_root / f"{parsed.name}{DUPES_LOCK_SUFFIX}"):
                signature_path = work_root / f"{parsed.name}.sigs"
                started = monotonic()
                if generation.state == "failed":
                    record.resume(parsed.name)
                source = parsed.snapshot[:-staging.DATE_SUFFIX_LENGTH]
                snapshot = date.fromisoformat(parsed.snapshot[-10:])
                scope = (parsed.collection, source, snapshot, tuple(sorted(parsed.courts)), parsed.pipeline)
                chunks, texts = record.scope(*scope)
                counts = DupesCounts(cluster_chunks=chunks, cluster_texts=texts)
                at, standing_chunks, standing_texts = record.standing(parsed.name)
                if at is not None and (standing_chunks, standing_texts) == (chunks, texts):
                    # The standing clustering's measured seconds stay its record; only the flag moves.
                    counts = replace(counts, cluster_reused=1)
                    record.progress(parsed.name, {"cluster_reused": 1})
                else:
                    record.start(parsed.name, counts)
                    signatures.open(signature_path, texts)
                    counts, keys, hashes = _sketch_scope(parsed, job, record, signatures, sketcher, counts, started, monotonic, scope)

                    def remaining() -> float:
                        left = CLUSTER_BUDGET_SECONDS - max(0.0, monotonic() - started)
                        if left <= 0:
                            raise DupesFailure("cluster-budget")
                        return left

                    def stamp() -> tuple[int, datetime]:
                        return round(CLUSTER_BUDGET_SECONDS - remaining()), clock()

                    def band_progress(band: int) -> None:
                        nonlocal counts
                        counts = replace(counts, cluster_seconds=round(CLUSTER_BUDGET_SECONDS - remaining()))
                        _log(job, parsed.name, counts, band)

                    roots = cluster(signatures, texts, on_band=band_progress)
                    band_progress(LSH_BANDS)
                    mapping = dict(zip(hashes, canonical_of(roots, keys, hashes), strict=True))
                    counts = record.write(parsed.name, mapping, counts, scope, remaining=remaining, stamp=stamp)
                cleanup_error: Exception | None = None
                for close in (
                    partial(signatures.close, remove=True),
                    partial(signature_path.unlink, missing_ok=True),
                    sketcher.close, record.close,
                ):
                    try:
                        close()
                    except Exception as exc:  # noqa: BLE001 - close every resource and preserve the first error.
                        cleanup_error = cleanup_error or exc
                if cleanup_error is not None:
                    raise cleanup_error
        except lexical.LexicalFailure as exc:
            raise DupesFailure(exc.reason, error=exc.error) from exc
        except psycopg.Error as exc:
            raise DupesFailure("database", error=type(exc).__name__) from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DupesFailure("local", error=type(exc).__name__) from exc
    except BaseException as exc:
        if isinstance(exc, DupesFailure):
            name = inputs.get("name") if isinstance(inputs, Mapping) else None
            if isinstance(name, str) and re.fullmatch(lexical.GENERATION_PATTERN, name) is not None and len(name.encode()) < 255:
                if exc.reason != "busy":
                    with suppress(DupesFailure, lexical.LexicalFailure, psycopg.Error, OSError, ValueError, KeyError, TypeError):
                        record.fail(name, exc.reason)
                if type(job) is int and job > 0:
                    _log(job, name, counts)
        # Cleanup preserves the job's first failure and joins any running sketches.
        with suppress(DupesFailure, lexical.LexicalFailure, psycopg.Error, OSError, ValueError, KeyError, TypeError):
            signatures.close(remove=True)
        for client in (sketcher, record):
            with suppress(DupesFailure, lexical.LexicalFailure, psycopg.Error, OSError, ValueError, KeyError, TypeError):
                client.close()
        raise
    else:
        _log(job, parsed.name, counts, LSH_BANDS if not counts.cluster_reused else 0)
        return counts
