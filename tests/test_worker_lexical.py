"""Lexical serving copies, resumable builds, and their record and HTTP adapters."""

import base64
import fcntl
import hashlib
import io
import json
import logging
import tempfile
import unittest
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, astuple, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast
from unittest.mock import patch

import httpx
import psycopg
from test_worker_caselaw import _CapturedConnection

from gideon.host import cas
from gideon.host.sysio import PathLike, RealHost
from gideon.worker import (
    caselaw,
    chunker,
    citations,
    lexical,
    logs,
    settings,
    treatment,
)
from gideon.worker.pipeline import PIPELINE_VERSION

TEXT = "α🙂FICTITIOUS_TEXT_SENTINEL: 18 U.S.C. § 99999. Fictitious ending."
NOW = datetime(2099, 1, 3, 4, 5, 6, tzinfo=UTC)
NAME = "caselaw__corpus-2099-01-03__example-space"
GEO = {"court1": treatment.Geography("circuit", "ca6", None)}


def inputs_for() -> dict[str, object]:
    return {
        "name": NAME, "collection": "caselaw", "label": "corpus-2099-01-03",
        "snapshot": "fictions-2099-01-02", "courts": {key: asdict(value) for key, value in GEO.items()},
        "pipeline": PIPELINE_VERSION, "embedding_space": "example-space", "inputs_hash": "e" * 64,
    }


def chunk_for(start: int = 2, end: int = len(TEXT), ordinal: int = 0) -> chunker.NewChunk:
    return chunker.NewChunk(
        str(ordinal + 1) * 64, "a" * 64, PIPELINE_VERSION, ordinal, "b" * 64,
        "majority", start, end, hashlib.sha256(TEXT[start:end].encode()).hexdigest(), 17,
    )


def opinion_for() -> caselaw.NewOpinion:
    return caselaw.NewOpinion(
        31, "a" * 64, "court1", 41, "FICTITIOUS_CAPTION_SENTINEL\n99-A", NOW.date(),
        False, "published", "Published", ("7 Fiction 11", "8 Fiction 12"), "020",
    )


def citation_for(start: int, end: int, reporter: str | None, key: str | None, ordinal: int = 0) -> citations.NewCitation:
    return citations.NewCitation(
        "c" * 64, "a" * 64, ordinal, start, end, "b" * 64, "majority", "case_cite", "full",
        "FICTITIOUS_RAW_CITE_SENTINEL", reporter, None, key, None, "fictional-pattern",
    )


class Projection(unittest.TestCase):
    def test_slice_uses_code_points_and_refuses_hash_or_span_disagreement(self) -> None:
        chunk = chunk_for()
        self.assertEqual(lexical.slice_for(TEXT, chunk), TEXT[chunk.char_start:chunk.char_end])
        self.assertNotEqual(chunk.char_start, len(TEXT[:chunk.char_start].encode()))
        for variant in (replace(chunk, content_hash="f" * 64), replace(chunk, char_start=-1),
                        replace(chunk, char_end=len(TEXT) + 1), replace(chunk, char_end=chunk.char_start)):
            with self.subTest(chunk=variant), self.assertRaises(lexical.LexicalFailure) as raised:
                lexical.slice_for(TEXT, variant)
            self.assertEqual(raised.exception.reason, "text-mismatch")

    def test_cites_are_contained_deduplicated_and_ordered_with_reporter_preferred(self) -> None:
        chunk = chunk_for(10, 40)
        rows = (
            citation_for(30, 35, None, "fictional-statute", 5),
            citation_for(10, 20, "7 Fiction 11", "ignored-key", 1),
            citation_for(22, 25, "7 Fiction 11", None, 3),
            citation_for(8, 12, "outside-left", None),
            citation_for(38, 42, "outside-right", None),
            citation_for(25, 30, None, None, 4),
        )
        self.assertEqual(lexical.cites_for(chunk, rows), (rows[1].reporter_cite, rows[0].key))

    def test_caption_and_document_come_only_from_the_record(self) -> None:
        opinion = opinion_for()
        chunk = chunk_for()
        text_slice = lexical.slice_for(TEXT, chunk)
        document = lexical.serving_document(chunk, text_slice, opinion, (), GEO)
        self.assertEqual(document.caption, " ".join((
            *opinion.reporter_cites, " ".join((opinion.docket or "").split()),
            opinion.court, opinion.decided_date.isoformat(),
        )))
        self.assertNotIn("\n", document.caption)
        self.assertEqual(document.text, text_slice)
        self.assertEqual(document.circuit, GEO[opinion.court].circuit)
        self.assertEqual(document.decided_date, opinion.decided_date.isoformat())
        self.assertEqual(document.opinion_id, opinion.opinion_id)
        self.assertEqual(document.cluster_id, opinion.cluster_id)
        self.assertEqual(document.char_start, chunk.char_start)
        self.assertEqual(document.content_hash, chunk.content_hash)
        self.assertEqual(set(asdict(document)), set(lexical.INDEX_MAPPING["properties"]) - {"dupe_cluster", "boilerplate"})
        self.assertEqual(lexical.caption_for(replace(opinion, docket=None, reporter_cites=())),
                         f"{opinion.court} {opinion.decided_date.isoformat()}")

    def test_analyzer_preserves_every_citation_form_with_escaped_padding(self) -> None:
        analysis = lexical.INDEX_SETTINGS["analysis"]
        self.assertEqual(analysis["analyzer"]["citation"], {
            "type": "custom", "char_filter": ["citation_forms"], "tokenizer": "standard", "filter": ["lowercase"],
        })
        mappings = analysis["char_filter"]["citation_forms"]
        self.assertEqual(mappings["type"], "mapping")
        self.assertEqual({form for form, _token in lexical.CITATION_FORMS}, {
            "§", "§§", "U.S.C.", "C.F.R.", "U.S.S.G.",
            "Fed. R. Crim. P.", "Fed. R. Civ. P.", "Fed. R. App. P.", "Fed. R. Evid.",
        })
        self.assertEqual(mappings["mappings"], [rf"{form} => \u0020{token}\u0020" for form, token in lexical.CITATION_FORMS])
        for _form, token in lexical.CITATION_FORMS:
            self.assertTrue(token.isascii() and token.isalnum() and token.islower())
        self.assertEqual(lexical.INDEX_MAPPING["dynamic"], "strict")
        self.assertEqual(lexical.INDEX_MAPPING["_meta"]["mapping_version"], lexical.LEXICAL_MAPPING_VERSION)
        self.assertNotIn("number_of_replicas", lexical.INDEX_SETTINGS)


class FakeRecord:
    def __init__(self, digest: str) -> None:
        self.generation: lexical.Generation | None = lexical.Generation(NAME, "caselaw", "e" * 64, PIPELINE_VERSION, "example-space", "building")
        self.document_rows = [lexical.Document("a" * 64, digest), lexical.Document("d" * 64, digest)]
        self.chunk_rows = {"a" * 64: (chunk_for(),), "d" * 64: ()}
        self.events: list[str] = []
        self.failure: str | None = None
        self.progress_rows: list[lexical.BuildCounts] = []
        self.finished: tuple[lexical.BuildCounts, int, datetime] | None = None
        self.scope: tuple[object, ...] | None = None
        self.fail_at: str | None = None
        self.resumed = False
        self.closed = False
        self.opinion_reads = 0

    def read(self, name: str) -> lexical.Generation | None:
        self.events.append("read")
        if self.fail_at == "read":
            raise psycopg.OperationalError("FICTITIOUS_EXCEPTION_SENTINEL")
        return self.generation

    def resume(self, name: str) -> None:
        self.events.append("resume")
        assert self.generation is not None
        self.generation = replace(self.generation, state="building")
        self.resumed = True

    def progress(self, name: str, counts: lexical.BuildCounts) -> None:
        self.progress_rows.append(counts)

    def finish(self, name: str, counts: lexical.BuildCounts, size: int, at: datetime) -> None:
        self.events.append("finish")
        if self.fail_at == "finish":
            raise lexical.LexicalFailure("database", error="FICTITIOUS_EXCEPTION_SENTINEL")
        self.finished = counts, size, at

    def fail(self, name: str, reason: str) -> None:
        self.failure = reason
        if self.generation is not None:
            self.generation = replace(self.generation, state="failed")

    def documents(self, collection: str, source: str, snapshot: date, courts: Sequence[str]) -> Iterator[lexical.Document]:
        self.scope = collection, source, snapshot, tuple(courts)
        yield from self.document_rows

    def chunks(self, doc_id: str, pipeline: str) -> tuple[chunker.NewChunk, ...]:
        assert pipeline == PIPELINE_VERSION
        return self.chunk_rows[doc_id]

    def citations(self, doc_id: str) -> tuple[citations.NewCitation, ...]:
        return (citation_for(10, 20, "FICTITIOUS_CITE_SENTINEL", None),)

    def opinion(self, doc_id: str) -> caselaw.NewOpinion:
        self.opinion_reads += 1
        return opinion_for()

    def close(self) -> None:
        self.closed = True


class FakeStore:
    def __init__(self) -> None:
        self.version: int | None = None
        self.rows: dict[str, lexical.ServingDocument] = {}
        self.writes = 0
        self.conflicts: set[str] = set()
        self.fail_at: str | None = None
        self.closed = False
        self.refreshes = 0

    def _check(self, operation: str) -> None:
        if self.fail_at == operation:
            raise lexical.LexicalFailure("store", error="FICTITIOUS_EXCEPTION_SENTINEL")

    def mapping_version(self, name: str) -> int | None:
        self._check("mapping")
        return self.version

    def create(self, name: str) -> None:
        self._check("create")
        self.version = lexical.LEXICAL_MAPPING_VERSION

    def present(self, name: str, ids: Sequence[str]) -> set[str]:
        self._check("present")
        return set(ids) & self.rows.keys()

    def bulk_create(self, name: str, documents: Sequence[tuple[str, lexical.ServingDocument]]) -> tuple[int, int]:
        self._check("bulk")
        self.writes += len(documents)
        for identifier, document in documents:
            self.rows[identifier] = document
        present = len({identifier for identifier, _document in documents} & self.conflicts)
        return len(documents) - present, present

    def refresh(self, name: str) -> None:
        self._check("refresh")
        self.refreshes += 1

    def bytes(self, name: str) -> int:
        self._check("bytes")
        return sum(len(document.text.encode()) for document in self.rows.values())

    def close(self) -> None:
        self.closed = True


class ReadHost(RealHost):
    def __init__(self) -> None:
        super().__init__()
        self.reads: list[Path] = []

    def read_bytes(self, path: PathLike) -> bytes:
        self.reads.append(Path(path))
        return super().read_bytes(path)


class Build(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store_root = self.root / "cas"
        self.work = self.root / "work"
        self.store_root.mkdir()
        self.work.mkdir()
        self.host = ReadHost()
        digest = cas.put(self.host, TEXT.encode(), root=self.store_root)
        assert isinstance(digest, str)
        self.record, self.store = FakeRecord(digest), FakeStore()
        self.input = inputs_for()

    def run_build(self, *, monotonic: Callable[[], float] = lambda: 0.0) -> lexical.BuildCounts:
        return lexical.build(self.input, 17, self.record, self.store, self.store_root,
                             work_root=self.work, host=self.host, clock=lambda: NOW, monotonic=monotonic)

    def refusal(self, reason: str) -> None:
        with self.assertRaises(lexical.LexicalFailure) as raised:
            self.run_build()
        self.assertEqual(raised.exception.reason, reason)
        self.assertEqual(self.record.failure, reason)
        self.assertTrue(self.record.closed)
        self.assertTrue(self.store.closed)

    def test_first_run_writes_slices_second_run_reads_no_text_or_metadata(self) -> None:
        counts = self.run_build()
        self.assertEqual(asdict(counts), {"ready": 2, "chunked": 1, "unchunked": 1, "indexed": 1, "present": 0, "seconds": 0})
        self.assertEqual(self.record.scope, ("caselaw", "fictions", date(2099, 1, 2), tuple(GEO)))
        self.assertEqual(self.store.rows[chunk_for().chunk_id].text, lexical.slice_for(TEXT, chunk_for()))
        self.assertEqual(self.record.finished, (counts, self.store.bytes(NAME), NOW))
        self.assertEqual(len(self.host.reads), 1)
        self.host.reads.clear()
        reads, writes = self.record.opinion_reads, self.store.writes
        again = self.run_build()
        self.assertEqual((again.indexed, again.present), (0, 1))
        self.assertEqual(self.host.reads, [])
        self.assertEqual((self.record.opinion_reads, self.store.writes), (reads, writes))

    def test_failed_resume_and_built_refill_keep_the_required_states(self) -> None:
        assert self.record.generation is not None
        self.record.generation = replace(self.record.generation, state="failed")
        self.run_build()
        self.assertTrue(self.record.resumed)
        self.assertEqual(self.record.generation.state, "building")
        self.record.generation = replace(self.record.generation, state="built")
        self.store.rows.clear()
        self.store.version = None
        self.run_build()
        self.assertEqual(self.record.generation.state, "built")
        self.assertEqual(len(self.store.rows), 1)

    def test_bulk_conflict_is_counted_present_and_empty_scope_finishes(self) -> None:
        self.store.conflicts.add(chunk_for().chunk_id)
        counts = self.run_build()
        self.assertEqual((counts.indexed, counts.present), (0, 1))
        self.record.document_rows.clear()
        self.assertEqual(self.run_build(), lexical.BuildCounts())

    def test_document_batches_continue_after_progress_and_read_each_missing_text_once(self) -> None:
        digest = self.record.document_rows[0].canonical_text_sha256
        self.record.document_rows = [
            lexical.Document(f"{number:064x}", digest)
            for number in range(lexical.DOCUMENT_BATCH_SIZE + 1)
        ]
        self.record.chunk_rows = {
            document.doc_id: (replace(chunk_for(), doc_id=document.doc_id, chunk_id=f"{number + 100:064x}"),)
            for number, document in enumerate(self.record.document_rows)
        }
        ticks = iter((0.0, lexical.PROGRESS_SECONDS, lexical.PROGRESS_SECONDS * 2, lexical.PROGRESS_SECONDS * 2 + 1))
        with patch.object(self.store, "present", wraps=self.store.present) as presence, patch.object(self.store, "bulk_create", wraps=self.store.bulk_create) as bulk:
            counts = self.run_build(monotonic=lambda: next(ticks))
        self.assertEqual((presence.call_count, bulk.call_count), (2, 2))
        self.assertEqual(counts.ready, len(self.record.document_rows))
        self.assertEqual(counts.indexed, len(self.record.document_rows))
        self.assertEqual(len(self.host.reads), len(self.record.document_rows))
        self.assertEqual([row.ready for row in self.record.progress_rows], [lexical.DOCUMENT_BATCH_SIZE, counts.ready])

    def test_invalid_states_and_identity_mismatch_refuse_before_store_writes(self) -> None:
        original = self.record.generation
        assert original is not None
        for state in ("live", "retired", "unknown"):
            with self.subTest(state=state):
                self.record.generation = replace(original, state=state)
                self.refusal("invalid")
        for row in (None, replace(original, inputs_hash="f" * 64), replace(original, pipeline="1000.0.0")):
            with self.subTest(row=row):
                self.record.generation = row
                self.refusal("generation-mismatch")
        self.assertEqual(self.store.writes, 0)

    def test_each_argument_grammar_refuses_with_a_safe_reason(self) -> None:
        changes: tuple[dict[str, object], ...] = (
            {"collection": "unknown"}, {"label": "corpus-2099-02-30"}, {"snapshot": "fictions-2099-02-30"},
            {"pipeline": "1000.0.0"}, {"embedding_space": "Example"}, {"inputs_hash": "A" * 64},
            {"courts": {}}, {"courts": {"Court1": asdict(GEO["court1"])}},
            {"courts": {"court1": {"level": "unknown", "circuit": None, "state": None}}},
            {"courts": {"court1": {"level": "circuit", "circuit": "unknown", "state": None}}},
            {"courts": {"court1": {"level": "circuit", "circuit": "ca6", "state": "XX"}}},
            {"extra": "FICTITIOUS_PRIVATE_ARGUMENT"},
        )
        for change in changes:
            with self.subTest(change=change):
                self.input = {**inputs_for(), **change}
                self.refusal("invalid")
        self.assertEqual(self.store.writes, 0)
        for name in ("INVALID", NAME + "x" * 255):
            self.input = {**inputs_for(), "name": name}
            with self.assertRaises(lexical.LexicalFailure) as raised:
                self.run_build()
            self.assertEqual(raised.exception.reason, "invalid")

    def test_mapping_and_each_store_operation_failure_records_its_reason(self) -> None:
        self.store.version = lexical.LEXICAL_MAPPING_VERSION + 1
        self.refusal("mapping-mismatch")
        for operation in ("mapping", "create", "present", "bulk", "refresh", "bytes"):
            with self.subTest(operation=operation):
                self.store.version = None
                self.store.rows.clear()
                self.store.fail_at = operation
                self.refusal("store")
        self.store.fail_at = None
        self.record.fail_at = "read"
        self.refusal("database")

    def test_missing_canonical_text_corrupt_object_and_mismatched_slice(self) -> None:
        path = cas.object_path(self.record.document_rows[0].canonical_text_sha256, self.store_root)
        original = path.read_bytes()
        path.unlink()
        self.refusal("missing-text")
        path.write_bytes(b"fictitious corrupt bytes")
        self.refusal("text-mismatch")
        path.write_bytes(original)
        self.record.chunk_rows["a" * 64] = (replace(chunk_for(), content_hash="f" * 64),)
        self.refusal("text-mismatch")

    def test_lock_is_under_work_root_busy_when_held_and_safe_against_symlinks(self) -> None:
        path = self.work / f"{NAME}{lexical.LOCK_SUFFIX}"
        with path.open("w") as file:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(lexical.LexicalFailure) as raised:
                self.run_build()
            self.assertEqual(raised.exception.reason, "busy")
            # The job holding the lock owns the row, so a busy job leaves it.
            self.assertIsNone(self.record.failure)
            assert self.record.generation is not None
            self.assertEqual(self.record.generation.state, "building")
        path.unlink()
        path.symlink_to(self.root / "outside")
        self.refusal("local")
        self.assertFalse((self.root / "outside").exists())

    def test_progress_and_real_log_handler_never_emit_text_cites_captions_or_paths(self) -> None:
        output = io.StringIO()
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        ticks = iter((0.0, lexical.PROGRESS_SECONDS, lexical.PROGRESS_SECONDS + 1))
        try:
            logs.install_handler(output)
            counts = self.run_build(monotonic=lambda: next(ticks))
        finally:
            root.handlers[:] = handlers
            root.setLevel(level)
        self.assertEqual(len(self.record.progress_rows), 1)
        self.assertEqual(set(asdict(counts)), {"ready", "chunked", "unchunked", "indexed", "present", "seconds"})
        for sentinel in ("FICTITIOUS_TEXT_SENTINEL", "FICTITIOUS_CITE_SENTINEL", "FICTITIOUS_CAPTION_SENTINEL", str(self.root)):
            self.assertNotIn(sentinel, output.getvalue())
        self.assertIn("job_id=17", output.getvalue())
        self.assertIn(f"generation={NAME}", output.getvalue())
        self.assertIn(f"seconds={round(lexical.PROGRESS_SECONDS)}", output.getvalue())

    def test_failed_build_logs_through_the_real_handler_without_exception_content(self) -> None:
        output = io.StringIO()
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        self.record.fail_at = "finish"
        try:
            logs.install_handler(output)
            self.refusal("database")
        finally:
            root.handlers[:] = handlers
            root.setLevel(level)
        self.assertIn(f"generation={NAME}", output.getvalue())
        for sentinel in ("FICTITIOUS_EXCEPTION_SENTINEL", "FICTITIOUS_TEXT_SENTINEL", "FICTITIOUS_CITE_SENTINEL", "FICTITIOUS_CAPTION_SENTINEL", str(self.root)):
            self.assertNotIn(sentinel, output.getvalue())

    def test_local_failure_and_cleanup_do_not_replace_original_reason(self) -> None:
        self.work.rmdir()
        self.refusal("local")
        self.work.mkdir()
        self.store.fail_at = "bulk"
        with patch.object(self.record, "fail", side_effect=lexical.LexicalFailure("database")), patch.object(self.store, "close", side_effect=OSError("private cleanup")), self.assertRaises(lexical.LexicalFailure) as raised:
            self.run_build()
        self.assertEqual(raised.exception.reason, "store")


def configuration(password_file: Path) -> settings.Settings:
    return settings.Settings("db.example.test", 5432, "example", "example-worker", password_file, 2,
                             "http://store.example.test:9200", "example-user", password_file)


class StreamConnection(_CapturedConnection):
    def __init__(self) -> None:
        super().__init__()
        self.names: list[str] = []
        self.itersize = 0
        self.cursor_closed = False

    def cursor(self, name: str | None = None) -> "StreamConnection":
        if name is not None:
            self.names.append(name)
        return self

    def __iter__(self) -> Iterator[tuple[object, ...]]:
        for row in self.rows:
            if self.commits:
                raise AssertionError("stream transaction committed before the cursor was consumed")
            yield row

    def __exit__(self, *_args: object) -> None:
        self.cursor_closed = True


class RecordAdapter(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        password = Path(temporary.name) / "password"
        password.write_text("fictitious-password")
        self.writer, self.stream = _CapturedConnection(), StreamConnection()
        self.connections = iter((self.writer, self.stream))
        self.calls: list[dict[str, object]] = []

        def connect(**kwargs: object):
            self.calls.append(kwargs)
            return next(self.connections)

        self.record = lexical.PsycopgGenerations(connect=connect, worker_settings=configuration(password))
        self.addCleanup(self.record.close)

    def test_progress_finish_and_failure_commit_on_the_other_connection(self) -> None:
        generation = lexical.Generation(NAME, "caselaw", "e" * 64, PIPELINE_VERSION, "example-space", "built")
        self.writer.rows = [astuple(generation)]
        self.assertEqual(self.record.read(NAME), generation)
        self.stream.rows = [("a" * 64, "b" * 64), ("c" * 64, "d" * 64)]
        stream = self.record.documents("caselaw", "fictions", NOW.date(), ("court1",))
        self.assertEqual(next(stream), lexical.Document(*cast(tuple[str, str], self.stream.rows[0])))
        counts = lexical.BuildCounts(2, 1, 1, 4, 3, 7)
        self.record.progress(NAME, counts)
        self.record.finish(NAME, counts, 4096, NOW)
        self.record.fail(NAME, "store")
        self.record.resume(NAME)
        self.assertEqual(self.stream.commits, 0)
        self.assertEqual(self.writer.commits, 5)
        self.assertEqual(next(stream), lexical.Document(*cast(tuple[str, str], self.stream.rows[1])))
        with self.assertRaises(StopIteration):
            next(stream)
        self.assertEqual(self.stream.commits, 1)
        self.assertTrue(self.stream.cursor_closed)
        self.assertEqual(self.stream.names, ["lexical_documents"])
        self.assertEqual(self.stream.itersize, lexical.DOCUMENT_BATCH_SIZE)
        self.assertEqual(self.stream.statements, [(lexical.DOCUMENTS_SQL, ("caselaw", "fictions", NOW.date(), ["court1"]))])
        self.assertEqual(json.loads(cast(str, self.writer.statements[1][1][0])), asdict(counts))
        self.assertEqual(self.writer.statements[2][1][1:], (4096, NOW, NOW, NAME))
        self.assertNotIn("SET state", lexical.FINISH_SQL)
        self.assertIn("dense_built_at IS NOT NULL", lexical.FINISH_SQL)
        self.assertIn("AND state = 'failed'", lexical.RESUME_SQL)
        self.assertIn("d.superseded_by IS NULL", lexical.DOCUMENTS_SQL)
        self.record.close()
        self.assertTrue(self.stream.closed and self.writer.closed)
        self.assertEqual(len(self.calls), 2)

    def test_chunk_citation_and_opinion_projections_and_scope_are_bound(self) -> None:
        chunk, citation, opinion = chunk_for(), citation_for(10, 20, "7 Fiction 11", None), opinion_for()
        for value, operation in ((chunk, lambda: self.record.chunks(chunk.doc_id, chunk.pipeline)),
                                 (citation, lambda: self.record.citations(citation.doc_id))):
            self.writer.rows = [astuple(value)]
            self.assertEqual(operation(), (value,))
        self.writer.rows = [astuple(opinion)]
        self.assertEqual(self.record.opinion(opinion.doc_id), opinion)
        self.assertEqual(self.writer.statements[0], (lexical.CHUNKS_SQL, (chunk.doc_id, chunk.pipeline)))
        self.assertEqual(self.writer.statements[1], (caselaw.READ_CITATIONS_SQL, (citation.doc_id,)))
        self.assertEqual(self.writer.statements[2], (lexical.OPINION_SQL, (opinion.doc_id,)))

    def test_database_failure_rolls_back_and_exposes_only_the_class(self) -> None:
        self.writer.fail_execute = True
        with self.assertRaises(lexical.LexicalFailure) as raised:
            self.record.progress(NAME, lexical.BuildCounts())
        self.assertEqual((raised.exception.reason, raised.exception.error), ("database", "OperationalError"))
        self.assertNotIn("private database detail", str(raised.exception))
        self.assertEqual(self.writer.rollbacks, 1)


class StoreAdapter(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.password = Path(temporary.name) / "password"
        self.password.write_text("fictitious-store-password")
        self.configuration = configuration(self.password)
        self.requests: list[httpx.Request] = []
        self.waits: list[float] = []

    def store(self, respond: Callable[[httpx.Request], httpx.Response]) -> lexical.HttpxStore:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return respond(request)

        store = lexical.HttpxStore(self.configuration, transport=httpx.MockTransport(handler), sleep=self.waits.append)
        self.addCleanup(store.close)
        return store

    def response(self, value: object = None, *, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json=value)

        return respond

    def test_requests_use_file_auth_mapping_and_source_free_presence(self) -> None:
        document = lexical.serving_document(chunk_for(), lexical.slice_for(TEXT, chunk_for()), opinion_for(), (), GEO)

        def respond(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "GET" and path.endswith("/_mapping"):
                return httpx.Response(404)
            if path.endswith("/_mget"):
                ids = json.loads(request.content)["ids"]
                return httpx.Response(200, json={"docs": [{"_id": identifier, "found": False} for identifier in ids]})
            if path.endswith("/_bulk"):
                identifier = json.loads(request.content.splitlines()[0])["create"]["_id"]
                return httpx.Response(200, json={"items": [{"create": {"_id": identifier, "status": 201}}]})
            if path.endswith(("/_refresh", "/_flush")):
                return httpx.Response(200, json={"_shards": {"failed": 0}})
            if path.endswith("/_stats/store"):
                return httpx.Response(200, json={"_shards": {"failed": 0}, "indices": {NAME: {"total": {"store": {"size_in_bytes": 4096}}}}})
            return httpx.Response(200, json={"acknowledged": True})

        store = self.store(respond)
        self.assertIsNone(store.mapping_version(NAME))
        store.create(NAME)
        self.assertEqual(store.present(NAME, (chunk_for().chunk_id,)), set())
        self.assertEqual(store.bulk_create(NAME, ((chunk_for().chunk_id, document),)), (1, 0))
        store.refresh(NAME)
        self.assertEqual(store.bytes(NAME), 4096)
        self.assertEqual([(request.method, request.url.path) for request in self.requests], [
            ("GET", f"/{NAME}/_mapping"), ("PUT", f"/{NAME}"), ("POST", f"/{NAME}/_mget"),
            ("POST", f"/{NAME}/_bulk"), ("PUT", f"/{NAME}/_settings"),
            ("POST", f"/{NAME}/_refresh"), ("POST", f"/{NAME}/_flush"),
            ("GET", f"/{NAME}/_stats/store"),
        ])
        self.assertEqual(json.loads(self.requests[1].content), {"settings": lexical.INDEX_SETTINGS, "mappings": lexical.INDEX_MAPPING})
        self.assertEqual(self.requests[2].url.params["_source"], "false")
        payload = self.requests[3].content
        self.assertTrue(payload.endswith(b"\n"))
        self.assertEqual(self.requests[3].headers["Content-Type"], "application/x-ndjson")
        self.assertEqual(json.loads(payload.splitlines()[1]), json.loads(json.dumps(asdict(document))))
        self.assertEqual(json.loads(self.requests[4].content), lexical.FINISH_SETTINGS)
        self.assertEqual(self.requests[0].extensions["timeout"], lexical.HTTP_TIMEOUT.as_dict())
        for request in self.requests:
            auth = request.headers["Authorization"].removeprefix("Basic ")
            self.assertEqual(base64.b64decode(auth).decode(), f"{self.configuration.opensearch_user}:{self.password.read_text()}")
            self.assertIsNone(request.url.userinfo or None)

    def test_new_client_reads_rotated_password_and_existing_mapping_version(self) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={NAME: {"mappings": {"_meta": {"mapping_version": lexical.LEXICAL_MAPPING_VERSION}}}})

        first = self.store(respond)
        self.assertEqual(first.mapping_version(NAME), lexical.LEXICAL_MAPPING_VERSION)
        first_auth = self.requests[-1].headers["Authorization"]
        self.password.write_text("fictitious-rotated-password")
        self.assertEqual(self.store(respond).mapping_version(NAME), lexical.LEXICAL_MAPPING_VERSION)
        self.assertNotEqual(self.requests[-1].headers["Authorization"], first_auth)
        self.assertEqual(base64.b64decode(self.requests[-1].headers["Authorization"].split()[1]).decode(),
                         f"{self.configuration.opensearch_user}:{self.password.read_text()}")

    def test_http_pressure_is_retried_bounded_and_other_statuses_refuse(self) -> None:
        replies = iter((429, 503, 200))
        store = self.store(lambda request: httpx.Response(next(replies), json={"acknowledged": True}))
        store.create(NAME)
        self.assertEqual(self.waits, list(lexical.RETRY_WAITS[:2]))
        exhausted = self.store(lambda request: httpx.Response(429))
        before = len(self.requests)
        with self.assertRaises(lexical.LexicalFailure) as raised:
            exhausted.create(NAME)
        self.assertEqual(raised.exception.reason, "store")
        self.assertEqual(len(self.requests) - before, len(lexical.RETRY_WAITS) + 1)
        for status in (301, 400, 401, 500):
            with self.subTest(status=status):
                rejected = self.store(self.response(status=status))
                before = len(self.requests)
                with self.assertRaises(lexical.LexicalFailure):
                    rejected.create(NAME)
                self.assertEqual(len(self.requests) - before, 1)

    def test_bulk_conflicts_and_item_pressure_preserve_successes(self) -> None:
        document = lexical.serving_document(chunk_for(), TEXT, opinion_for(), (), GEO)
        identifiers = ("1" * 64, "2" * 64, "3" * 64)

        def respond(request: httpx.Request) -> httpx.Response:
            ids = [json.loads(line)["create"]["_id"] for line in request.content.splitlines()[::2]]
            statuses = (201, 409, 429) if len(ids) == 3 else (201,)
            return httpx.Response(200, json={"errors": True, "items": [
                {"create": {"_id": identifier, "status": status}} for identifier, status in zip(ids, statuses, strict=True)
            ]})

        self.assertEqual(self.store(respond).bulk_create(NAME, [(identifier, document) for identifier in identifiers]), (2, 1))
        retry_ids = [json.loads(line)["create"]["_id"] for line in self.requests[1].content.splitlines()[::2]]
        self.assertEqual(retry_ids, [identifiers[-1]])
        self.assertEqual(self.waits, [lexical.RETRY_WAITS[0]])

    def test_every_other_bulk_item_error_is_raised(self) -> None:
        document = lexical.serving_document(chunk_for(), TEXT, opinion_for(), (), GEO)
        for status in (400, 401, 404, 500):
            with self.subTest(status=status):
                def respond(request: httpx.Request, status=status) -> httpx.Response:
                    return httpx.Response(200, json={"errors": False, "items": [
                        {"create": {"_id": "1" * 64, "status": 201}},
                        {"create": {"_id": "2" * 64, "status": status, "error": "FICTITIOUS_PRIVATE_ERROR"}},
                    ]})

                with self.assertRaises(lexical.LexicalFailure) as raised:
                    self.store(respond).bulk_create(NAME, [("1" * 64, document), ("2" * 64, document)])
                self.assertEqual(raised.exception.reason, "store")
                self.assertNotIn("FICTITIOUS_PRIVATE_ERROR", str(raised.exception))

    def test_bulk_item_pressure_is_bounded_and_requests_obey_the_byte_bound(self) -> None:
        document = lexical.serving_document(chunk_for(), TEXT, opinion_for(), (), GEO)

        def busy(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": [{"create": {"_id": "1" * 64, "status": 503}}]})

        with self.assertRaises(lexical.LexicalFailure):
            self.store(busy).bulk_create(NAME, [("1" * 64, document)])
        self.assertEqual(len(self.requests), len(lexical.RETRY_WAITS) + 1)
        self.requests.clear()

        def created(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": [{"create": {"_id": json.loads(request.content.splitlines()[0])["create"]["_id"], "status": 201}}]})

        first = ("1" * 64, document)
        bound = len(lexical._bulk_lines(first))
        with patch.object(lexical, "BULK_BYTES", bound):
            self.assertEqual(self.store(created).bulk_create(NAME, [first, ("2" * 64, document)]), (2, 0))
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(len(request.content) <= bound for request in self.requests))

    def test_presence_is_bounded_and_item_errors_cannot_mean_absent(self) -> None:
        ids = [f"{number:064x}" for number in range(lexical.MGET_BATCH_SIZE + 1)]

        def found(request: httpx.Request) -> httpx.Response:
            selected = json.loads(request.content)["ids"]
            self.assertLessEqual(len(selected), lexical.MGET_BATCH_SIZE)
            return httpx.Response(200, json={"docs": [{"_id": identifier, "found": True} for identifier in selected]})

        self.assertEqual(self.store(found).present(NAME, ids), set(ids))
        self.assertEqual(len(self.requests), 2)
        for row in ({"_id": ids[0], "error": "FICTITIOUS_PRIVATE_ERROR"}, {"_id": ids[0], "found": "false"}, {"_id": "wrong", "found": False}):
            with self.subTest(row=row), self.assertRaises(lexical.LexicalFailure):
                self.store(self.response({"docs": [row]})).present(NAME, ids[:1])

    def test_malformed_mapping_stats_refresh_and_transport_failures_refuse(self) -> None:
        mapping_replies: tuple[dict[str, object], ...] = ({NAME: {"mappings": {}}}, {NAME: {"mappings": {"_meta": {"mapping_version": True}}}})
        for reply in mapping_replies:
            with self.subTest(reply=reply), self.assertRaises(lexical.LexicalFailure) as raised:
                self.store(self.response(reply)).mapping_version(NAME)
            self.assertEqual(raised.exception.reason, "mapping-mismatch")
        stats_replies: tuple[dict[str, object], ...] = (
            {"_shards": {"failed": 1}}, {"_shards": {"failed": 0}, "indices": {}},
            {"_shards": {"failed": 0}, "indices": {NAME: {"total": {"store": {"size_in_bytes": -1}}}}},
        )
        for reply in stats_replies:
            with self.subTest(reply=reply), self.assertRaises(lexical.LexicalFailure):
                self.store(self.response(reply)).bytes(NAME)
        with self.assertRaises(lexical.LexicalFailure):
            self.store(lambda request: httpx.Response(200, text="FICTITIOUS_INVALID_REPLY")).create(NAME)

        def disconnected(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("FICTITIOUS_PRIVATE_ERROR", request=request)

        with self.assertRaises(lexical.LexicalFailure) as raised:
            self.store(disconnected).create(NAME)
        self.assertEqual((raised.exception.reason, raised.exception.error), ("store", "ConnectError"))
        self.assertNotIn("FICTITIOUS_PRIVATE_ERROR", str(raised.exception))
