"""Deterministic chunk sketches, clustering, and temporary signature storage."""

import fcntl
import hashlib
import io
import json
import logging
import pickle
import struct
import tempfile
import unittest
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import FrozenInstanceError, asdict, astuple, fields, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

import psycopg
from test_worker_caselaw import _CapturedConnection
from test_worker_lexical import StreamConnection, configuration

from gideon.host import cas
from gideon.host.sysio import RealHost
from gideon.worker import chunker, dupes, lexical, logs, treatment
from gideon.worker.pipeline import PIPELINE_VERSION

TEXT = "α🙂 FICTITIOUS_TEXT_SENTINEL. A fictitious opinion ending."


def packed(values: list[int]) -> bytes:
    return struct.pack(f"<{dupes.SIGNATURE_SLOTS}I", *values)


class Values(unittest.TestCase):
    def test_counts_and_keys_are_frozen_and_counts_follow_the_shared_order(self) -> None:
        counts = dupes.DupesCounts()
        self.assertEqual(tuple(field.name for field in fields(counts)), dupes.DUPES_COUNT_KEYS)
        self.assertTrue(all(type(value) is int and value == 0 for value in asdict(counts).values()))
        keys = lexical.TextKeys("a" * 64, False)
        self.assertEqual(asdict(keys), {"dupe_cluster": "a" * 64, "boilerplate": False})
        for value, field in ((counts, "cluster_seconds"), (keys, "boilerplate")):
            with self.subTest(field=field), self.assertRaises(FrozenInstanceError):
                setattr(value, field, 1)

    def test_failure_vocabulary_and_process_serialization(self) -> None:
        for reason in dupes.DUPES_FAILURE_REASONS:
            with self.subTest(reason=reason):
                failure = pickle.loads(pickle.dumps(dupes.DupesFailure(reason, error="OSError")))
                self.assertEqual((failure.reason, failure.error, str(failure)), (reason, "OSError", reason))
        with self.assertRaises(ValueError):
            dupes.DupesFailure("fictitious-unknown-reason")

    def test_release_constants_are_consistent(self) -> None:
        self.assertEqual(dupes.LSH_BANDS * dupes.LSH_ROWS, dupes.SIGNATURE_SLOTS)
        self.assertEqual(dupes.SIGNATURE_BYTES, struct.calcsize(f"<{dupes.SIGNATURE_SLOTS}I"))
        self.assertEqual(dupes.AGREEMENT_SLOTS, 103)
        self.assertEqual(dupes.TASKS_IN_FLIGHT, 2 * dupes.CLUSTER_PROCESSES)
        self.assertEqual(tuple(dupes.COURT_LEVEL_RANK), treatment.LEVELS)


class Sketches(unittest.TestCase):
    def test_shingles_normalize_case_whitespace_and_short_texts(self) -> None:
        for text, expected in (
            ("", b""), (" \n\t ", b""), ("ONE\tTwo\nTHREE", b"one two three"),
            ("α🙂 FiCTITIOUS", "α🙂 fictitious".encode()),
            (" ".join(f"word{i}" for i in range(dupes.SHINGLE_WORDS)),
             " ".join(f"word{i}" for i in range(dupes.SHINGLE_WORDS)).encode()),
        ):
            with self.subTest(text=text):
                self.assertEqual(list(dupes.shingles(text)), [expected])
        words = [f"fictitious{i}" for i in range(dupes.SHINGLE_WORDS + 2)]
        self.assertEqual(list(dupes.shingles(" ".join(words))), [
            " ".join(words[start:start + dupes.SHINGLE_WORDS]).encode() for start in range(3)
        ])

    def test_signature_length_determinism_and_normalization(self) -> None:
        for text in ("", TEXT, " ".join(f"fictitious{i}" for i in range(400))):
            with self.subTest(text=text[:20]):
                sig = dupes.signature(text)
                self.assertEqual(len(sig), dupes.SIGNATURE_BYTES)
                self.assertEqual(sig, dupes.signature(text))
                self.assertEqual(sig, dupes.signature("\t".join(text.upper().split())))
                self.assertEqual(dupes.agreement(sig, sig), dupes.SIGNATURE_SLOTS)

    def test_hash_byte_order_minimum_densification_and_slot_packing(self) -> None:
        # Three visibly constructed digests exercise collisions and forward wrap.
        digests = ((2 << 57) | (1 << 40) | 11, (2 << 57) | 7, (125 << 57) | (1 << 35) | 3)
        replies = [Mock(digest=Mock(return_value=value.to_bytes(8, "big"))) for value in digests]
        words = [f"fiction{i}" for i in range(dupes.SHINGLE_WORDS + 2)]
        text = " ".join(words)
        with patch.object(dupes.hashlib, "blake2b", side_effect=replies) as hash_call:
            sig = dupes.signature(text)
        self.assertEqual(sig, packed([7] * 3 + [3] * 123 + [7] * 2))
        self.assertEqual([call.args[0] for call in hash_call.call_args_list], list(dupes.shingles(text)))
        self.assertTrue(all(call.kwargs == {"digest_size": 8} for call in hash_call.call_args_list))

    def test_agreement_estimates_exact_shingle_jaccard_for_near_and_far_texts(self) -> None:
        words = [f"fictitiousword{i}" for i in range(400)]
        original = " ".join(words)
        changed = words.copy()
        changed[200] = "fictitiousreplacement"
        far = " ".join(f"differentfiction{i}" for i in range(len(words)))
        for other in (" ".join(changed), far):
            with self.subTest(other=other[:25]):
                left, right = set(dupes.shingles(original)), set(dupes.shingles(other))
                exact = len(left & right) / len(left | right)
                estimate = dupes.agreement(dupes.signature(original), dupes.signature(other)) / dupes.SIGNATURE_SLOTS
                self.assertAlmostEqual(estimate, exact, delta=0.08)

    def test_band_keys_are_contiguous_slot_slices(self) -> None:
        sig = packed(list(range(dupes.SIGNATURE_SLOTS)))
        for band in range(dupes.LSH_BANDS):
            with self.subTest(band=band):
                start = band * dupes.LSH_ROWS
                self.assertEqual(dupes.band_key(sig, band), struct.pack(
                    f"<{dupes.LSH_ROWS}I", *range(start, start + dupes.LSH_ROWS),
                ))
        for band in (-1, dupes.LSH_BANDS):
            with self.subTest(band=band), self.assertRaises(ValueError):
                dupes.band_key(sig, band)


class CanonicalMembers(unittest.TestCase):
    def test_canonical_order_is_rank_then_date_then_index(self) -> None:
        early, late = date(2099, 1, 1), date(2099, 2, 1)
        maximum = (1 << 32) - 1
        for first, second in zip(treatment.LEVELS, treatment.LEVELS[1:], strict=False):
            with self.subTest(level=first):
                self.assertLess(dupes.canonical_key(first, late, maximum), dupes.canonical_key(second, early, 0))
        self.assertLess(dupes.canonical_key("scotus", early, maximum), dupes.canonical_key("scotus", late, 0))
        self.assertLess(dupes.canonical_key("scotus", early, 0), dupes.canonical_key("scotus", early, 1))
        key = dupes.canonical_key("circuit", early, maximum)
        self.assertEqual(key >> 64, dupes.COURT_LEVEL_RANK["circuit"])
        self.assertEqual((key >> 32) & maximum, early.toordinal())
        self.assertEqual(key & maximum, maximum)
        for index in (-1, 1 << 32):
            with self.subTest(index=index), self.assertRaises(ValueError):
                dupes.canonical_key("scotus", early, index)

    def test_canonical_hash_is_selected_by_key_and_keeps_singletons(self) -> None:
        hashes = [character * 64 for character in "abcde"]
        roots, keys = [0, 0, 2, 0, 2], [10, 3, 8, 7, 12]
        self.assertEqual(dupes.canonical_of(roots, keys, hashes), [hashes[1], hashes[1], hashes[2], hashes[1], hashes[2]])
        self.assertEqual(dupes.canonical_of([0, 1], [5, 1], hashes[:2]), hashes[:2])
        self.assertEqual(dupes.canonical_of([], [], []), [])


class FileStore(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "fictitious.sigs"
        self.store = dupes.FileSignatures()
        self.addCleanup(self.store.close, True)

    def test_round_trip_permissions_out_of_order_writes_bands_and_removal(self) -> None:
        sigs = [dupes.signature(text) for text in ("fiction one", "fiction two", TEXT)]
        self.store.open(self.path, len(sigs))
        self.assertEqual(self.path.stat().st_size, len(sigs) * dupes.SIGNATURE_BYTES)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        for index in reversed(range(len(sigs))):
            self.store.put(index, sigs[index])
        for index, sig in enumerate(sigs):
            self.assertEqual(self.store.get(index), sig)
        for band in range(dupes.LSH_BANDS):
            self.assertEqual(list(self.store.band(band)), [(dupes.band_key(sig, band), index) for index, sig in enumerate(sigs)])
            self.assertEqual(self.store.get(1), sigs[1])
        self.store.close(remove=False)
        self.assertTrue(self.path.exists())
        self.store.close(remove=True)
        self.assertFalse(self.path.exists())

    def test_open_truncates_a_partial_file_and_resets_permissions(self) -> None:
        self.path.write_bytes(b"fictitious partial signature" * dupes.SIGNATURE_BYTES)
        self.path.chmod(0o644)
        self.store.open(self.path, 1)
        self.assertEqual(self.path.stat().st_size, dupes.SIGNATURE_BYTES)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.get(0), bytes(dupes.SIGNATURE_BYTES))
        self.store.open(self.path, 0)
        self.assertEqual(self.path.stat().st_size, 0)
        self.assertEqual(list(self.store.band(0)), [])

    def test_open_failure_after_creation_removes_its_owned_file(self) -> None:
        with patch.object(dupes.os, "ftruncate", side_effect=OSError("fictitious truncate failure")), self.assertRaises(OSError):
            self.store.open(self.path, 1)
        self.assertFalse(self.path.exists())

    def test_close_failure_still_removes_its_owned_file(self) -> None:
        self.store.open(self.path, 1)
        close_descriptor = dupes.os.close

        def close_with_failure(descriptor: int) -> None:
            close_descriptor(descriptor)
            raise OSError("fictitious descriptor close failure")

        with patch.object(dupes.os, "close", side_effect=close_with_failure), self.assertRaises(OSError):
            self.store.close(remove=True)
        self.assertFalse(self.path.exists())

    def test_wrong_sizes_out_of_range_indexes_and_incomplete_reads_refuse(self) -> None:
        self.store.open(self.path, 1)
        with self.assertRaises(ValueError):
            self.store.put(0, b"short")
        for index in (-1, 1):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.store.get(index)
        self.path.write_bytes(b"short")
        with self.assertRaises(OSError):
            self.store.get(0)
        with self.assertRaises(OSError):
            list(self.store.band(0))

    def test_verified_bucket_rejected_candidate_and_transitive_union(self) -> None:
        slots, rows = dupes.SIGNATURE_SLOTS, dupes.LSH_ROWS
        sigs = [
            packed([0] * slots),
            packed([1] * (2 * rows) + [0] * (slots - 2 * rows)),
            packed([1] * (4 * rows) + [0] * (slots - 4 * rows)),
            packed([0] * rows + [2] * (slots - rows)),
        ]
        self.assertGreaterEqual(dupes.agreement(sigs[0], sigs[1]), dupes.AGREEMENT_SLOTS)
        self.assertGreaterEqual(dupes.agreement(sigs[1], sigs[2]), dupes.AGREEMENT_SLOTS)
        self.assertLess(dupes.agreement(sigs[0], sigs[2]), dupes.AGREEMENT_SLOTS)
        self.assertEqual(dupes.band_key(sigs[0], 0), dupes.band_key(sigs[3], 0))
        self.store.open(self.path, len(sigs))
        for index, sig in enumerate(sigs):
            self.store.put(index, sig)
        bands: list[int] = []
        self.assertEqual(dupes.cluster(self.store, len(sigs), on_band=bands.append), [0, 0, 0, 3])
        self.assertEqual(bands, list(range(dupes.LSH_BANDS)))

    def test_empty_scope_and_per_band_refusal(self) -> None:
        self.store.open(self.path, 0)
        self.assertEqual(dupes.cluster(self.store, 0), [])
        calls: list[int] = []

        def check_budget(band: int) -> None:
            calls.append(band)
            if band == 2:
                raise dupes.DupesFailure("cluster-budget")

        with self.assertRaises(dupes.DupesFailure) as raised:
            dupes.cluster(self.store, 0, on_band=check_budget)
        self.assertEqual(raised.exception.reason, "cluster-budget")
        self.assertEqual(calls, [0, 1, 2])

    def test_verification_accepts_the_exact_agreement_floor(self) -> None:
        slots, floor = dupes.SIGNATURE_SLOTS, dupes.AGREEMENT_SLOTS
        sigs = [
            packed([0] * slots),
            packed([0] * floor + [1] * (slots - floor)),
            packed([0] * (floor - 1) + [2] * (slots - floor + 1)),
        ]
        self.store.open(self.path, len(sigs))
        for index, sig in enumerate(sigs):
            self.store.put(index, sig)
        self.assertEqual(dupes.cluster(self.store, len(sigs)), [0, 0, 2])


class SketchTask(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "cas"
        self.root.mkdir()
        digest = cas.put(RealHost(), TEXT.encode(), root=self.root)
        assert isinstance(digest, str)
        self.task = dupes.SketchTask(digest, tuple(
            (index, start, end, hashlib.sha256(TEXT[start:end].encode()).hexdigest())
            for index, start, end in ((9, 0, 2), (3, 3, len(TEXT)))
        ))

    def test_sketch_uses_code_point_spans_and_keeps_the_assigned_indexes(self) -> None:
        self.assertEqual(dupes.sketch(self.task, self.root), tuple(
            (index, dupes.signature(TEXT[start:end])) for index, start, end, _ in self.task.chunks
        ))
        with self.assertRaises(FrozenInstanceError):
            self.task.canonical_text_sha256 = "a" * 64  # type: ignore[misc]

    def test_hash_or_span_mismatch_crosses_as_a_clustering_failure(self) -> None:
        for chunk in ((9, 0, 2, "f" * 64), (9, -1, 2, "f" * 64), (9, 0, len(TEXT) + 1, "f" * 64)):
            with self.subTest(chunk=chunk), self.assertRaises(dupes.DupesFailure) as raised:
                dupes.sketch(replace(self.task, chunks=(chunk,)), self.root)
            self.assertEqual(raised.exception.reason, "text-mismatch")

    def test_missing_and_corrupt_canonical_objects_keep_their_reasons(self) -> None:
        path = cas.object_path(self.task.canonical_text_sha256, self.root)
        path.unlink()
        with self.assertRaises(dupes.DupesFailure) as raised:
            dupes.sketch(self.task, self.root)
        self.assertEqual(raised.exception.reason, "missing-text")
        path.write_bytes(b"fictitious corrupt canonical object")
        with self.assertRaises(dupes.DupesFailure) as raised:
            dupes.sketch(self.task, self.root)
        self.assertEqual(raised.exception.reason, "text-mismatch")

    def test_other_errors_cross_with_only_the_class_name(self) -> None:
        with patch.object(dupes, "signature", side_effect=RuntimeError(TEXT)), self.assertRaises(dupes.DupesFailure) as raised:
            dupes.sketch(self.task, self.root)
        failure = raised.exception
        self.assertEqual((failure.reason, failure.error), ("local", "RuntimeError"))
        self.assertNotIn(TEXT, str(failure))


NOW = datetime(2099, 1, 3, 4, 5, 6, tzinfo=UTC)
NAME = "caselaw__corpus-2099-01-03__example-space"
GEOGRAPHY = {
    "scotus": treatment.Geography("scotus", None, None),
    "ca6": treatment.Geography("circuit", "ca6", None),
}


class FixtureRecord:
    def __init__(self, documents: Sequence[dupes.Document], chunks: Sequence[chunker.NewChunk]) -> None:
        self.document_rows = tuple(documents)
        self.chunk_rows = tuple(chunks)
        self.generation: lexical.Generation | None = lexical.Generation(NAME, "caselaw", "e" * 64, PIPELINE_VERSION, "example-space", "built")
        self.clustered_at: datetime | None = NOW
        self.counts = dupes.DupesCounts()
        self.events: list[str] = []
        self.progress_rows: list[Mapping[str, int]] = []
        self.batch_reads: list[tuple[str, ...]] = []
        self.mapping: dict[str, str] = {}
        self.clusters: dict[str, tuple[int, int, bool]] = {}
        self.members: dict[str, str] = {}
        self.scope_reads: list[tuple[object, ...]] = []
        self.written_scope: tuple[object, ...] | None = None
        self.timestamps: list[tuple[str, datetime | None]] = []
        self.closed = False
        self.failure: str | None = None

    def read(self, name: str) -> lexical.Generation | None:
        self.events.append("read")
        assert name == NAME
        return self.generation

    def resume(self, name: str) -> None:
        self.events.append("resume")
        assert self.generation is not None
        self.generation = replace(self.generation, state="building")

    def scope(self, collection: str, source: str, snapshot: date, courts: Sequence[str], pipeline: str) -> tuple[int, int]:
        self.scope_reads.append((collection, source, snapshot, tuple(courts), pipeline))
        return len(self.chunk_rows), len({chunk.content_hash for chunk in self.chunk_rows})

    def standing(self, name: str) -> tuple[datetime | None, int, int]:
        return self.clustered_at, self.counts.cluster_chunks, self.counts.cluster_texts

    def start(self, name: str, counts: dupes.DupesCounts) -> None:
        self.events.append("start")
        self.counts = counts
        self.clustered_at = None
        self.timestamps.append(("start", self.clustered_at))

    def progress(self, name: str, counts: Mapping[str, int]) -> None:
        self.progress_rows.append(dict(counts))
        self.counts = replace(self.counts, **counts)

    def documents_with_opinions(self, collection: str, source: str, snapshot: date, courts: Sequence[str]) -> Iterator[dupes.Document]:
        self.events.append("documents")
        yield from self.document_rows

    def chunks_for(self, doc_ids: Sequence[str], pipeline: str) -> tuple[chunker.NewChunk, ...]:
        self.batch_reads.append(tuple(doc_ids))
        assert pipeline == PIPELINE_VERSION
        selected = set(doc_ids)
        return tuple(chunk for chunk in self.chunk_rows if chunk.doc_id in selected)

    def write(
        self, name: str, mapping: Mapping[str, str], counts: dupes.DupesCounts,
        scope: tuple[str, str, date, Sequence[str], str], *,
        remaining: Callable[[], float], stamp: Callable[[], tuple[int, datetime]],
    ) -> dupes.DupesCounts:
        remaining()
        assert name == NAME and self.clustered_at is None
        self.mapping = dict(mapping)
        self.written_scope = scope
        grouped: dict[str, list[chunker.NewChunk]] = {}
        for chunk in self.chunk_rows:
            grouped.setdefault(mapping[chunk.content_hash], []).append(chunk)
        self.clusters = {
            key: (len(chunks), len({chunk.doc_id for chunk in chunks}),
                  len({chunk.doc_id for chunk in chunks}) >= dupes.BOILERPLATE_OPINIONS)
            for key, chunks in grouped.items() if len(chunks) >= 2
        }
        self.members = {
            content_hash: cluster for content_hash, cluster in mapping.items()
            if content_hash != cluster and cluster in self.clusters
        }
        self.counts = replace(
            counts, cluster_clusters=len(self.clusters), cluster_members=len(self.members),
            cluster_grouped=sum(chunks for chunks, _, _ in self.clusters.values()),
            cluster_boilerplate=sum(flagged for _, _, flagged in self.clusters.values()),
            cluster_flagged=sum(chunks for chunks, _, flagged in self.clusters.values() if flagged),
            cluster_reused=0,
        )
        seconds, at = stamp()
        # A write the budget rolls back records no event, as its transaction leaves no row.
        self.events.append("write")
        self.counts = replace(self.counts, cluster_seconds=seconds)
        self.clustered_at = at
        self.timestamps.append(("write", self.clustered_at))
        return self.counts

    def fail(self, name: str, reason: str) -> None:
        self.failure = reason
        if self.generation is not None:
            self.generation = replace(self.generation, state="failed")

    def close(self) -> None:
        self.closed = True


class InProcessSketcher:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.tasks: list[dupes.SketchTask] = []
        self.closed = False

    def submit(self, task: dupes.SketchTask) -> Future[tuple[tuple[int, bytes], ...]]:
        self.tasks.append(task)
        result: Future[tuple[tuple[int, bytes], ...]] = Future()
        try:
            result.set_result(dupes.sketch(task, self.root))
        except dupes.DupesFailure as failure:
            result.set_exception(failure)
        return result

    def close(self) -> None:
        self.closed = True


class CheckedSignatures(dupes.FileSignatures):
    def __init__(self, record: FixtureRecord) -> None:
        super().__init__()
        self.record = record
        self.written: list[int] = []
        self.opened: list[tuple[Path, int]] = []

    def open(self, path: Path, count: int) -> None:
        self.opened.append((path, count))
        super().open(path, count)

    def put(self, index: int, sig: bytes) -> None:
        assert self.record.clustered_at is None
        assert "start" in self.record.events and "write" not in self.record.events
        self.written.append(index)
        super().put(index, sig)


class ClusteringJob(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "cas"
        self.root.mkdir()
        self.work = Path(temporary.name) / "work"
        self.work.mkdir()
        self.documents: list[dupes.Document] = []
        self.chunks: list[chunker.NewChunk] = []
        self.groups: list[list[str]] = []
        for group, size in enumerate((30, 3, 26, 1)):
            hashes: list[str] = []
            for member in range(size):
                if group < 2:
                    words = [f"fictitiousgroup{group}word{index}" for index in range(400)]
                    words[150] = f"fictitiousgroup{group}variant{member}"
                    text = " ".join(words)
                else:
                    text = f"FICTITIOUS independent short text group {group}"
                digest = cas.put(RealHost(), text.encode(), root=self.root)
                assert isinstance(digest, str)
                content_hash = hashlib.sha256(text.encode()).hexdigest()
                hashes.append(content_hash)
                document_id = f"{len(self.documents) + 1:064x}"
                court = "scotus" if group == 0 and member in (10, 17) else "ca6"
                decided = date(2090, 1, 1) if member == 17 else date(2091, 1, 1) if court == "scotus" else date(2080, 1, member % 28 + 1)
                self.documents.append(dupes.Document(document_id, digest, court, decided))
                self.chunks.append(chunker.NewChunk(
                    document_id, document_id, PIPELINE_VERSION, 0, "b" * 64,
                    "majority", 0, len(text), content_hash, len(text.split()),
                ))
            self.groups.append(hashes)
        self.inputs: dict[str, object] = {
            "name": NAME, "collection": "caselaw", "label": "corpus-2099-01-03",
            "snapshot": "fictions-2099-01-02", "courts": {court: asdict(value) for court, value in GEOGRAPHY.items()},
            "pipeline": PIPELINE_VERSION, "embedding_space": "example-space", "inputs_hash": "e" * 64,
        }
        self.reset_clients()

    def reset_clients(self) -> None:
        self.record = FixtureRecord(self.documents, self.chunks)
        self.signatures = CheckedSignatures(self.record)
        self.sketcher = InProcessSketcher(self.root)

    def attempt(self, *, job: int = 17, monotonic: Callable[[], float] = lambda: 0.0) -> dupes.DupesCounts:
        return dupes.build(
            self.inputs, job, self.record, self.signatures, self.sketcher, self.root,
            work_root=self.work, clock=lambda: NOW, monotonic=monotonic,
        )

    def refusal(self, reason: str, *, job: int = 17, monotonic: Callable[[], float] = lambda: 0.0) -> dupes.DupesFailure:
        with self.assertRaises(dupes.DupesFailure) as raised:
            self.attempt(job=job, monotonic=monotonic)
        self.assertEqual(raised.exception.reason, reason)
        self.assertEqual(self.record.failure, reason)
        self.assertNotIn("write", self.record.events)
        self.assertTrue(self.record.closed and self.sketcher.closed)
        self.assertFalse((self.work / f"{NAME}.sigs").exists())
        return raised.exception

    def run_build(self) -> tuple[dupes.DupesCounts, FixtureRecord, CheckedSignatures, InProcessSketcher]:
        record = FixtureRecord(self.documents, self.chunks)
        signatures = CheckedSignatures(record)
        sketcher = InProcessSketcher(self.root)
        ticks = iter(float(index) for index in range(1000))
        with patch.object(dupes, "DOCUMENT_BATCH_SIZE", 7):
            counts = dupes.build(
                self.inputs, 17, record, signatures, sketcher, self.root,
                work_root=self.work, clock=lambda: NOW, monotonic=lambda: next(ticks),
            )
        return counts, record, signatures, sketcher

    def test_cluster_fixture_names_flags_exact_copies_and_singletons(self) -> None:
        counts, record, signatures, sketcher = self.run_build()
        canonical = self.groups[0][17]
        three = self.groups[1][0]
        copied = self.groups[2][0]
        singleton = self.groups[3][0]
        expected = {
            **dict.fromkeys(self.groups[0], canonical), **dict.fromkeys(self.groups[1], three),
            copied: copied, singleton: singleton,
        }
        self.assertEqual(record.mapping, expected)
        self.assertEqual(record.clusters, {canonical: (30, 30, True), three: (3, 3, False), copied: (26, 26, True)})
        self.assertEqual(record.members, {content_hash: cluster for content_hash, cluster in expected.items() if content_hash != cluster})
        self.assertNotIn(copied, record.members)
        self.assertNotIn(singleton, record.clusters)
        self.assertEqual(
            (counts.cluster_chunks, counts.cluster_texts, counts.cluster_documents),
            (len(self.chunks), len(expected), len(self.documents)),
        )
        self.assertEqual((counts.cluster_clusters, counts.cluster_members, counts.cluster_boilerplate, counts.cluster_flagged), (3, 31, 2, 56))
        self.assertEqual(counts.cluster_grouped, 59)
        self.assertEqual(counts.cluster_reused, 0)
        self.assertEqual(record.timestamps, [("start", None), ("write", NOW)])
        self.assertEqual(record.clustered_at, NOW)
        self.assertEqual(sorted(signatures.written), list(range(len(expected))))
        self.assertEqual(sum(len(task.chunks) for task in sketcher.tasks), len(expected))
        self.assertEqual(len(record.batch_reads), (len(self.documents) + 6) // 7)
        self.assertTrue(all(len(batch) <= 7 for batch in record.batch_reads))
        self.assertEqual(record.written_scope, ("caselaw", "fictions", date(2099, 1, 2), tuple(sorted(GEOGRAPHY)), PIPELINE_VERSION))
        self.assertEqual(record.scope_reads, [record.written_scope])
        self.assertIsNone(record.failure)
        self.assertTrue(record.closed and sketcher.closed)
        self.assertFalse((self.work / f"{NAME}.sigs").exists())

    def test_two_full_runs_have_identical_mapping_rows_and_counts(self) -> None:
        first, first_record, _, _ = self.run_build()
        second, second_record, _, _ = self.run_build()
        self.assertEqual(first_record.mapping, second_record.mapping)
        self.assertEqual(first_record.clusters, second_record.clusters)
        self.assertEqual(first_record.members, second_record.members)
        self.assertEqual(first, second)
        self.assertEqual(first_record.events.count("write"), 1)
        self.assertEqual(second_record.events.count("write"), 1)
        self.assertFalse((self.work / f"{NAME}.sigs").exists())

    def test_equal_scope_reuses_with_only_a_progress_merge(self) -> None:
        self.record.counts = dupes.DupesCounts(
            cluster_chunks=len(self.chunks), cluster_texts=len(set(sum(self.groups, []))),
            cluster_documents=len(self.documents), cluster_clusters=3, cluster_members=31,
            cluster_grouped=59, cluster_boilerplate=2, cluster_flagged=56,
        )
        standing = self.record.counts
        ticks = iter((10.0, 12.0))
        counts = self.attempt(monotonic=lambda: next(ticks))
        self.assertEqual(counts.cluster_reused, 1)
        self.assertEqual((counts.cluster_chunks, counts.cluster_texts), (standing.cluster_chunks, standing.cluster_texts))
        # The standing clustering's measured seconds survive the reuse.
        self.assertEqual(self.record.counts, replace(standing, cluster_reused=1))
        self.assertEqual(self.record.progress_rows, [{"cluster_reused": 1}])
        self.assertEqual(self.record.events, ["read"])
        self.assertEqual(self.signatures.opened, [])
        self.assertEqual(self.signatures.written, [])
        self.assertEqual(self.sketcher.tasks, [])
        self.assertEqual(self.record.batch_reads, [])
        self.assertEqual(self.record.clustered_at, NOW)
        self.assertTrue(self.record.closed and self.sketcher.closed)
        self.assertFalse((self.work / f"{NAME}.sigs").exists())

    def test_reused_finish_removes_an_orphan_signature_file_without_opening_it(self) -> None:
        self.record.counts = dupes.DupesCounts(
            cluster_chunks=len(self.chunks), cluster_texts=len({chunk.content_hash for chunk in self.chunks}),
        )
        path = self.work / f"{NAME}.sigs"
        path.write_bytes(b"fictitious interrupted cleanup")
        self.assertEqual(self.attempt().cluster_reused, 1)
        self.assertEqual(self.signatures.opened, [])
        self.assertFalse(path.exists())

    def test_growth_in_either_scope_count_reclusters(self) -> None:
        chunks, texts = self.record.scope("caselaw", "fictions", NOW.date(), tuple(GEOGRAPHY), PIPELINE_VERSION)
        for earlier_chunks, earlier_texts in ((chunks - 1, texts), (chunks, texts - 1)):
            with self.subTest(chunks=earlier_chunks, texts=earlier_texts):
                self.reset_clients()
                self.record.counts = dupes.DupesCounts(cluster_chunks=earlier_chunks, cluster_texts=earlier_texts)
                counts = self.attempt()
                self.assertEqual((counts.cluster_chunks, counts.cluster_texts, counts.cluster_reused), (chunks, texts, 0))
                self.assertEqual(self.record.events.count("start"), 1)
                self.assertEqual(self.record.events.count("write"), 1)
                self.assertTrue(self.signatures.opened and self.sketcher.tasks)
                self.assertFalse((self.work / f"{NAME}.sigs").exists())

    def test_failed_reclustering_keeps_new_scope_and_retry_does_not_reuse(self) -> None:
        chunks = len(self.chunks)
        texts = len({chunk.content_hash for chunk in self.chunks})
        self.record.counts = dupes.DupesCounts(cluster_chunks=chunks - 1, cluster_texts=texts - 1)
        with patch.object(self.signatures, "put", side_effect=OSError("fictitious put failure")):
            self.refusal("local")
        self.assertIsNone(self.record.clustered_at)
        self.assertEqual((self.record.counts.cluster_chunks, self.record.counts.cluster_texts), (chunks, texts))
        assert self.record.generation is not None
        self.assertEqual(self.record.generation.state, "failed")
        self.record.closed = False
        self.signatures = CheckedSignatures(self.record)
        self.sketcher = InProcessSketcher(self.root)
        counts = self.attempt()
        self.assertEqual(counts.cluster_reused, 0)
        self.assertEqual(self.record.events.count("start"), 2)
        self.assertEqual(self.record.events.count("write"), 1)
        self.assertIn("resume", self.record.events)
        self.assertTrue(self.signatures.opened and self.sketcher.tasks)
        self.assertEqual(self.record.timestamps, [("start", None), ("start", None), ("write", NOW)])
        self.assertFalse((self.work / f"{NAME}.sigs").exists())

    def test_budget_failure_at_a_document_batch_a_band_and_the_write(self) -> None:
        past_budget = float(dupes.CLUSTER_BUDGET_SECONDS + 1)
        for stage, batch_size, times in (
            ("batch", 7, (0.0, 0.0, 0.0, 0.0, past_budget)),
            ("band", len(self.documents), (0.0, 0.0, 0.0, 0.0, 0.0, past_budget)),
            ("write", len(self.documents), (0.0,) * (5 + dupes.LSH_BANDS + 1) + (past_budget,)),
        ):
            with self.subTest(stage=stage):
                self.reset_clients()
                ticks = iter(times)
                with patch.object(dupes, "DOCUMENT_BATCH_SIZE", batch_size):
                    self.refusal("cluster-budget", monotonic=ticks.__next__)
                self.assertIsNone(self.record.clustered_at)
                assert self.record.generation is not None
                self.assertEqual(self.record.generation.state, "failed")
                self.assertEqual(len(self.record.batch_reads), 1)
                self.assertEqual(self.signatures.opened, [(self.work / f"{NAME}.sigs", self.record.counts.cluster_texts)])
                if stage != "batch":
                    self.assertEqual(len(self.signatures.written), self.record.counts.cluster_texts)
                self.assertFalse((self.work / f"{NAME}.sigs").exists())

    def test_invalid_inputs_and_job_ids_refuse_before_start(self) -> None:
        original = self.inputs.copy()
        changes: tuple[dict[str, object], ...] = (
            {"collection": "fictitious-unknown"}, {"label": "corpus-2099-02-30"},
            {"snapshot": "fictions-2099-02-30"}, {"pipeline": "1000.0.0"},
            {"embedding_space": "Example"}, {"inputs_hash": "A" * 64}, {"courts": {}},
            {"courts": {"scotus": {"level": "fictitious-unknown", "circuit": None, "state": None}}},
            {"extra": "FICTITIOUS_ARGUMENT_SENTINEL"},
        )
        for change in changes:
            with self.subTest(change=change):
                self.reset_clients()
                self.inputs = {**original, **change}
                self.refusal("invalid")
                self.assertEqual(self.signatures.opened, [])
                self.assertEqual(self.sketcher.tasks, [])
                self.assertNotIn("start", self.record.events)
        self.inputs = original
        for job in (0, -1, True, "17", None):
            with self.subTest(job=job):
                self.reset_clients()
                self.refusal("invalid", job=cast(int, job))
                self.assertEqual(self.record.events, [])
        for name in ("INVALID", NAME + "x" * 255):
            with self.subTest(name=name):
                self.reset_clients()
                self.inputs = {**original, "name": name}
                with self.assertRaises(dupes.DupesFailure) as raised:
                    self.attempt()
                self.assertEqual(raised.exception.reason, "invalid")
                self.assertIsNone(self.record.failure)
                self.assertTrue(self.record.closed and self.sketcher.closed)
                self.assertFalse((self.work / f"{NAME}.sigs").exists())

    def test_absent_generation_and_every_identity_field_mismatch_refuse(self) -> None:
        original = self.record.generation
        assert original is not None
        for generation in (
            None, replace(original, gen_id=NAME + "-different"), replace(original, collection="fictitious"),
            replace(original, inputs_hash="f" * 64), replace(original, pipeline="1000.0.0"),
            replace(original, embedding_space="fictitious-space"),
        ):
            with self.subTest(generation=generation):
                self.reset_clients()
                self.record.generation = generation
                self.refusal("generation-mismatch")
                self.assertNotIn("start", self.record.events)
                self.assertEqual(self.signatures.opened, [])
                self.assertEqual(self.sketcher.tasks, [])

    def test_live_and_retired_refuse_and_a_failed_row_resumes_before_start(self) -> None:
        original = self.record.generation
        assert original is not None
        for state in ("live", "retired"):
            with self.subTest(state=state):
                self.reset_clients()
                self.record.generation = replace(original, state=state)
                self.refusal("invalid")
                self.assertNotIn("start", self.record.events)
        self.reset_clients()
        self.record.generation = replace(original, state="failed")
        counts = self.attempt()
        self.assertEqual(self.record.events[:3], ["read", "resume", "start"])
        self.assertEqual(counts.cluster_reused, 0)
        self.assertEqual(self.record.clustered_at, NOW)
        self.assertIsNone(self.record.failure)
        self.assertFalse((self.work / f"{NAME}.sigs").exists())

    def test_busy_leaves_the_lock_holders_generation_and_signature_file_untouched(self) -> None:
        generation, counts, at = self.record.generation, self.record.counts, self.record.clustered_at
        lock = self.work / f"{NAME}{dupes.DUPES_LOCK_SUFFIX}"
        signatures = self.work / f"{NAME}.sigs"
        signatures.write_bytes(b"fictitious active signature file")
        with lock.open("w") as descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(dupes.DupesFailure) as raised:
                self.attempt()
        self.assertEqual(raised.exception.reason, "busy")
        self.assertIsNone(self.record.failure)
        self.assertEqual((self.record.generation, self.record.counts, self.record.clustered_at), (generation, counts, at))
        self.assertEqual(self.record.events, ["read"])
        self.assertEqual(self.signatures.opened, [])
        self.assertEqual(self.sketcher.tasks, [])
        self.assertEqual(signatures.read_bytes(), b"fictitious active signature file")
        self.assertTrue(self.record.closed and self.sketcher.closed)

    def test_record_errors_become_database_with_only_the_error_class(self) -> None:
        for operation, error in (
            ("read", lexical.LexicalFailure("database", error="OperationalError")),
            ("chunks_for", psycopg.OperationalError("FICTITIOUS_RECORD_SENTINEL")),
        ):
            with self.subTest(operation=operation):
                self.reset_clients()
                with patch.object(self.record, operation, side_effect=error):
                    failure = self.refusal("database")
                self.assertEqual(failure.error, "OperationalError")
                self.assertNotIn("FICTITIOUS_RECORD_SENTINEL", str(failure))

    def test_signature_write_oserror_becomes_local_and_removes_the_open_file(self) -> None:
        with patch.object(self.signatures, "put", side_effect=OSError("FICTITIOUS_LOCAL_SENTINEL")):
            failure = self.refusal("local")
        self.assertEqual(failure.error, "OSError")
        self.assertTrue(self.signatures.opened)
        self.assertIsNone(self.record.clustered_at)
        self.assertNotIn("FICTITIOUS_LOCAL_SENTINEL", str(failure))

    def test_missing_and_mismatched_canonical_text_and_chunk_fail_through_futures(self) -> None:
        path = cas.object_path(self.documents[0].canonical_text_sha256, self.root)
        original = path.read_bytes()
        for failure in ("missing-text", "text-mismatch"):
            with self.subTest(reason=failure):
                self.reset_clients()
                if failure == "missing-text":
                    path.unlink()
                else:
                    path.write_bytes(b"FICTITIOUS_CORRUPT_TEXT_SENTINEL")
                try:
                    self.refusal(failure)
                    self.assertEqual(len(self.sketcher.tasks), 1)
                    self.assertTrue(self.signatures.opened)
                finally:
                    path.write_bytes(original)
        self.reset_clients()
        self.record.chunk_rows = (replace(self.chunks[0], content_hash="f" * 64), *self.chunks[1:])
        self.refusal("text-mismatch")

    def test_real_spawned_sketch_failure_keeps_its_reason(self) -> None:
        sketcher = dupes.ProcessSketcher(self.root)
        try:
            future = sketcher.submit(dupes.SketchTask("f" * 64, ((0, 0, len(TEXT), "e" * 64),)))
            with self.assertRaises(dupes.DupesFailure) as raised:
                future.result(timeout=10)
            self.assertEqual(raised.exception.reason, "missing-text")
        finally:
            sketcher.close()
        self.assertFalse((self.work / f"{NAME}.sigs").exists())

    def test_cleanup_failures_preserve_the_first_sketch_failure_and_remove_the_file(self) -> None:
        path = cas.object_path(self.documents[0].canonical_text_sha256, self.root)
        path.unlink()
        signature_close = self.signatures.close
        sketcher_close = self.sketcher.close
        record_close = self.record.close
        record_fail = self.record.fail

        def close_signatures(remove: bool) -> None:
            signature_close(remove)
            if remove:
                raise OSError("FICTITIOUS_SIGNATURE_CLEANUP_SENTINEL")

        def close_sketcher() -> None:
            sketcher_close()
            raise lexical.LexicalFailure("local")

        def close_record() -> None:
            record_close()
            raise psycopg.OperationalError("FICTITIOUS_RECORD_CLEANUP_SENTINEL")

        def fail_record(name: str, reason: str) -> None:
            record_fail(name, reason)
            raise lexical.LexicalFailure("database")

        with (
            patch.object(self.signatures, "close", side_effect=close_signatures),
            patch.object(self.sketcher, "close", side_effect=close_sketcher),
            patch.object(self.record, "close", side_effect=close_record),
            patch.object(self.record, "fail", side_effect=fail_record),
        ):
            self.refusal("missing-text")

    def test_success_cleanup_keeps_its_first_failure_when_later_clients_also_fail(self) -> None:
        signature_close = self.signatures.close
        sketcher_close = self.sketcher.close
        record_close = self.record.close

        def close_signatures(remove: bool) -> None:
            signature_close(remove)
            if remove:
                raise dupes.DupesFailure("local", error="FirstCleanup")

        def close_sketcher() -> None:
            sketcher_close()
            raise dupes.DupesFailure("database", error="LaterCleanup")

        def close_record() -> None:
            record_close()
            raise OSError("FICTITIOUS_LATEST_CLEANUP_SENTINEL")

        with (
            patch.object(self.signatures, "close", side_effect=close_signatures),
            patch.object(self.sketcher, "close", side_effect=close_sketcher),
            patch.object(self.record, "close", side_effect=close_record),
            self.assertRaises(dupes.DupesFailure) as raised,
        ):
            self.attempt()
        self.assertEqual((raised.exception.reason, raised.exception.error), ("local", "FirstCleanup"))
        self.assertTrue(self.record.closed and self.sketcher.closed)
        self.assertFalse((self.work / f"{NAME}.sigs").exists())

    def test_progress_bands_finish_and_failure_logs_are_content_free_through_the_real_handler(self) -> None:
        text_digest = cas.put(RealHost(), TEXT.encode(), root=self.root)
        assert isinstance(text_digest, str)
        self.record.document_rows = (dupes.Document("f" * 64, text_digest, "scotus", NOW.date()),)
        self.record.chunk_rows = (chunker.NewChunk(
            "f" * 64, "f" * 64, PIPELINE_VERSION, 0, "b" * 64, "majority", 0, len(TEXT), text_digest, len(TEXT.split()),
        ),)
        output = io.StringIO()
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        ticks = iter((0.0, 0.0, float(dupes.PROGRESS_SECONDS), float(dupes.PROGRESS_SECONDS),
                      *[float(dupes.PROGRESS_SECONDS)] * (dupes.LSH_BANDS + 3)))
        try:
            logs.install_handler(output)
            counts = self.attempt(monotonic=lambda: next(ticks))
            self.assertEqual(counts.cluster_documents, 1)
            self.assertEqual(len(self.record.progress_rows), 1)
            self.reset_clients()
            with patch.object(self.record, "chunks_for", side_effect=OSError(TEXT + str(self.root) + text_digest)):
                self.refusal("local")
        finally:
            root.handlers[:] = handlers
            root.setLevel(level)
        emitted = output.getvalue()
        for sentinel in ("FICTITIOUS_TEXT_SENTINEL", TEXT, text_digest, str(self.root), str(self.work)):
            self.assertNotIn(sentinel, emitted)
        for field in ("job_id=17", f"generation={NAME}", "documents=1", "texts=1",
                      "bands=0", f"bands={dupes.LSH_BANDS}", f"seconds={dupes.PROGRESS_SECONDS}"):
            self.assertIn(field, emitted)


class MappingConnection(_CapturedConnection):
    """Capture COPY rows beside ordinary statements and transaction boundaries."""

    def __init__(self) -> None:
        super().__init__()
        self.copied: list[tuple[str, str]] = []
        self.fail_copy = False

    def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> None:
        super().execute(statement, parameters)

    def copy(self, statement: str) -> "MappingConnection":
        self.execute(statement)
        return self

    def write_row(self, row: tuple[str, str]) -> None:
        if self.fail_copy:
            raise psycopg.OperationalError("fictitious private COPY diagnostic")
        assert self.commits == 0
        self.copied.append(row)

    def fetchone(self) -> tuple[object, ...]:
        assert self.commits == 0
        return self.rows[0]


class RecordAdapter(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        password = Path(temporary.name) / "password"
        password.write_text("fictitious-password")
        self.writer, self.stream = MappingConnection(), StreamConnection()
        connections = iter((self.writer, self.stream))
        self.record = dupes.PsycopgGenerations(
            connect=lambda **_kwargs: next(connections), worker_settings=configuration(password),
        )
        self.addCleanup(self.record.close)
        self.scope = ("caselaw", "fictions", NOW.date(), ("scotus", "ca6"), PIPELINE_VERSION)

    def test_atomic_write_statement_order_copy_rows_and_timestamp(self) -> None:
        mapping = {"a" * 64: "b" * 64, "b" * 64: "b" * 64, "c" * 64: "c" * 64}
        initial = dupes.DupesCounts(cluster_chunks=9, cluster_texts=len(mapping), cluster_documents=7, cluster_reused=1)
        self.writer.rows = [(2, 1, 1, 6, 8)]
        merged = self.record.write(NAME, mapping, initial, self.scope, remaining=lambda: 2.5, stamp=lambda: (41, NOW))
        self.assertEqual(self.writer.copied, list(mapping.items()))
        bounds = [index for index, (sql, _) in enumerate(self.writer.statements) if sql == dupes.TIMEOUT_SQL]
        self.assertEqual(bounds, list(range(0, 18, 2)))
        self.assertTrue(all(self.writer.statements[index][1] == ("2500ms",) for index in bounds))
        self.writer.statements = [row for row in self.writer.statements if row[0] != dupes.TIMEOUT_SQL]
        self.assertEqual([sql for sql, _ in self.writer.statements], [
            dupes.MAPPING_SQL, dupes.COPY_SQL, "ANALYZE dupe_mapping",
            "DELETE FROM dupe_members WHERE gen_id = %s", "DELETE FROM dupe_clusters WHERE gen_id = %s",
            dupes.CLUSTERS_SQL, dupes.MEMBERS_SQL, dupes.COUNTS_SQL, dupes.FINISH_SQL,
        ])
        clusters, members, boilerplate, flagged, grouped = self.writer.rows[0]
        assert isinstance(clusters, int) and isinstance(members, int)
        assert isinstance(boilerplate, int) and isinstance(flagged, int)
        assert isinstance(grouped, int)
        self.assertEqual(merged, replace(initial, cluster_clusters=clusters, cluster_members=members,
                                         cluster_grouped=grouped, cluster_boilerplate=boilerplate,
                                         cluster_flagged=flagged, cluster_seconds=41, cluster_reused=0))
        self.assertEqual((self.writer.commits, self.writer.rollbacks), (1, 0))
        self.assertEqual(self.writer.statements[3][1], (NAME,))
        self.assertEqual(self.writer.statements[4][1], (NAME,))
        self.assertEqual(self.writer.statements[5][1], (
            NAME, dupes.BOILERPLATE_OPINIONS, "caselaw", "fictions", NOW.date(), ["scotus", "ca6"], PIPELINE_VERSION,
        ))
        self.assertEqual(self.writer.statements[6][1], (NAME, NAME))
        self.assertEqual(self.writer.statements[7][1], (NAME, NAME))
        payload, at, name = self.writer.statements[-1][1]
        assert isinstance(payload, str)
        self.assertEqual(json.loads(payload), asdict(merged))
        self.assertEqual((at, name), (NOW, NAME))
        for sql, clause in (
            (dupes.MAPPING_SQL, "ON COMMIT DROP"),
            (dupes.CLUSTERS_SQL, "GROUP BY m.cluster_id HAVING COUNT(*) >= 2"),
            (dupes.CLUSTERS_SQL, "COUNT(DISTINCT c.doc_id) >= %s"),
            (dupes.MEMBERS_SQL, "WHERE m.cluster_id <> m.content_hash"),
            (dupes.MEMBERS_SQL, "k.gen_id = %s AND k.cluster_id = m.cluster_id"),
            (dupes.FINISH_SQL, "counts = counts || %s::jsonb, clustered_at = %s"),
            (dupes.COUNTS_SQL, "COALESCE(SUM(chunk_count), 0)"),
        ):
            with self.subTest(clause=clause):
                self.assertIn(clause, sql)

    def test_write_errors_roll_back_without_committing_or_exposing_diagnostics(self) -> None:
        for failure in ("fail_execute", "fail_copy", "fail_commit"):
            with self.subTest(failure=failure):
                writer = MappingConnection()
                writer.rows = [(0, 0, 0, 0, 0)]
                setattr(writer, failure, True)
                with patch.object(self.record, "_connection", return_value=writer), self.assertRaises(dupes.DupesFailure) as raised:
                    self.record.write(NAME, {"a" * 64: "a" * 64}, dupes.DupesCounts(), self.scope,
                                      remaining=lambda: 60.0, stamp=lambda: (1, NOW))
                self.assertEqual((raised.exception.reason, raised.exception.error), ("database", "OperationalError"))
                self.assertNotIn("diagnostic", str(raised.exception))
                self.assertEqual((writer.commits, writer.rollbacks), (0, 1))

    def test_write_past_the_budget_rolls_back_as_the_budget_failure(self) -> None:
        def expired() -> float:
            raise dupes.DupesFailure("cluster-budget")

        class Cancelling(MappingConnection):
            def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> None:
                super().execute(statement, parameters)
                if statement == dupes.CLUSTERS_SQL:
                    raise psycopg.errors.QueryCanceled("fictitious statement timeout")

        for case in ("remaining", "stamp", "cancelled"):
            with self.subTest(case=case):
                writer = Cancelling() if case == "cancelled" else MappingConnection()
                writer.rows = [(0, 0, 0, 0, 0)]
                remaining = expired if case == "remaining" else (lambda: 60.0)

                def stamp(case: str = case) -> tuple[int, datetime]:
                    if case == "stamp":
                        raise dupes.DupesFailure("cluster-budget")
                    return 1, NOW

                with patch.object(self.record, "_connection", return_value=writer), self.assertRaises(dupes.DupesFailure) as raised:
                    self.record.write(NAME, {"a" * 64: "a" * 64}, dupes.DupesCounts(), self.scope,
                                      remaining=remaining, stamp=stamp)
                self.assertEqual(raised.exception.reason, "cluster-budget")
                self.assertEqual((writer.commits, writer.rollbacks), (0, 1))
                self.assertNotIn(dupes.FINISH_SQL, [sql for sql, _ in writer.statements])

    def test_scope_chunk_reads_start_and_partial_progress_are_bound(self) -> None:
        self.writer.rows = [(9, 4)]
        self.assertEqual(self.record.scope(*self.scope), tuple(self.writer.rows[0]))
        self.assertEqual(self.writer.statements[-1], (
            dupes.SCOPE_SQL, ("caselaw", "fictions", NOW.date(), ["scotus", "ca6"], PIPELINE_VERSION),
        ))
        self.assertIn("COUNT(*), COUNT(DISTINCT c.content_hash)", dupes.SCOPE_SQL)
        self.assertIn(lexical.DOCUMENTS_SQL, dupes.SCOPE_SQL)
        self.writer.rows = [(NOW, 9, 4)]
        self.assertEqual(self.record.standing(NAME), tuple(self.writer.rows[0]))
        chunk = chunker.NewChunk("a" * 64, "b" * 64, PIPELINE_VERSION, 0, "c" * 64, "majority", 0, 5, "d" * 64, 1)
        self.writer.rows = [astuple(chunk)]
        self.assertEqual(self.record.chunks_for((chunk.doc_id,), chunk.pipeline), (chunk,))
        self.assertEqual(self.writer.statements[-1], (dupes.CHUNKS_SQL, ([chunk.doc_id], chunk.pipeline)))
        self.assertIn("doc_id = ANY(%s) AND pipeline = %s ORDER BY doc_id, ordinal", dupes.CHUNKS_SQL)
        counts = dupes.DupesCounts(cluster_chunks=9, cluster_texts=4)
        self.record.start(NAME, counts)
        reset, payload, name = self.writer.statements[-1][1]
        assert isinstance(payload, str)
        self.assertEqual((reset, json.loads(payload), name), (list(dupes.DUPES_COUNT_KEYS), asdict(counts), NAME))
        self.assertIn("clustered_at = NULL", dupes.START_SQL)
        self.record.progress(NAME, {"cluster_reused": 1, "cluster_seconds": 2})
        payload, name = self.writer.statements[-1][1]
        assert isinstance(payload, str)
        self.assertEqual((json.loads(payload), name), ({"cluster_reused": 1, "cluster_seconds": 2}, NAME))

    def test_documents_statement_selects_exactly_the_document_fields(self) -> None:
        selected = dupes.DOCUMENTS_SQL.split(" FROM ", 1)[0].removeprefix("SELECT ").split(", ")
        self.assertEqual([column.split(".")[1] for column in selected], [field.name for field in fields(dupes.Document)])

    def test_documents_stream_without_writer_commits_closing_the_cursor(self) -> None:
        self.record.start(NAME, dupes.DupesCounts())
        self.stream.rows = [("a" * 64, "b" * 64, "scotus", NOW.date()), ("c" * 64, "d" * 64, "ca6", NOW.date())]
        documents = self.record.documents_with_opinions(*self.scope[:4])
        first = next(documents)
        self.assertEqual(astuple(first), self.stream.rows[0])
        self.record.progress(NAME, {"cluster_documents": 1})
        self.assertEqual(self.stream.commits, 0)
        self.assertEqual(self.writer.commits, 2)
        self.assertEqual(astuple(next(documents)), self.stream.rows[1])
        with self.assertRaises(StopIteration):
            next(documents)
        self.assertEqual(self.stream.commits, 1)
        self.assertEqual(self.stream.names, ["dupes_documents"])
        self.assertEqual(self.stream.itersize, dupes.DOCUMENT_BATCH_SIZE)
        self.assertEqual(self.stream.statements, [(dupes.DOCUMENTS_SQL, ("caselaw", "fictions", NOW.date(), ["scotus", "ca6"]))])
        self.record.close()
        self.assertTrue(self.writer.closed and self.stream.closed)
