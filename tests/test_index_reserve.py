"""Reserve lines and chunk projections over fictitious figures and a recording host."""

import json
import subprocess
import unittest
from fractions import Fraction

from test_index_record import RENDERED, FakeHost

from gideon.host import worker
from gideon.host.corpus.record import database_fix
from gideon.host.index.guards import (
    CHUNK_PROJECTION_SQL,
    ChunkProjection,
    judge_guard,
    read_chunk_projection,
)
from gideon.host.index.reserve import (
    ALERT_RESERVE_MULTIPLIER,
    BUILD_RESERVE_RATIO,
    DATA_DF_ARGV,
    describe,
    judge,
    parse_size_and_available,
    read_free_bytes,
)
from gideon.host.models import ChunkGuards
from gideon.host.report import Problem

COMMAND_PATH = "index build caselaw --lockfile corpus-2099-01-03"


class Reserve(unittest.TestCase):
    def test_lines_round_up_to_whole_bytes_and_clear_at_equality(self) -> None:
        self.assertEqual(BUILD_RESERVE_RATIO, Fraction(5, 4))
        self.assertEqual(ALERT_RESERVE_MULTIPLIER, 2)
        for promoted, build_line, alert_line in (
            (0, 0, 0), (1, 2, 2), (3, 4, 6), (4, 5, 8),
            (8_000_000_000, 10_000_000_000, 16_000_000_000),
            (40_000_000_000_000_003, 50_000_000_000_000_004, 80_000_000_000_000_006),
        ):
            for free in {max(0, build_line - 1), build_line, alert_line - 1, alert_line, alert_line + 1}:
                if free < 0:
                    continue
                with self.subTest(promoted=promoted, free=free):
                    verdict = judge(free, promoted)
                    self.assertEqual(verdict.free_bytes, free)
                    self.assertEqual(verdict.promoted_bytes, promoted)
                    self.assertEqual(verdict.build_line, build_line)
                    self.assertEqual(verdict.alert_line, alert_line)
                    self.assertEqual(verdict.build_cleared, free >= build_line)
                    self.assertEqual(verdict.alert_cleared, free >= alert_line)

    def test_description_names_raw_bytes_gigabytes_and_both_clearances(self) -> None:
        self.assertEqual(describe(judge(12_000_000_000, 8_000_000_000)),
            "/data free 12000000000 bytes (12.0 GB); promoted 8000000000 bytes (8.0 GB); "
            "build line 10000000000 bytes (10.0 GB), cleared; "
            "alert line 16000000000 bytes (16.0 GB), not cleared")
        self.assertIn("build line 10000000000 bytes (10.0 GB), not cleared", describe(judge(9_000_000_000, 8_000_000_000)))
        self.assertIn("alert line 16000000000 bytes (16.0 GB), cleared", describe(judge(16_000_000_000, 8_000_000_000)))

    def test_df_success_and_parser_shapes(self) -> None:
        host = FakeHost(stdout="       1B-blocks       Avail\n 17000000000 9000000000\n\n")
        self.assertEqual(read_free_bytes(host), (17_000_000_000, 9_000_000_000))
        self.assertEqual(host.calls, [(list(DATA_DF_ARGV), None)])
        self.assertEqual(parse_size_and_available("Size Avail\n0 0\n"), (0, 0))
        for output in ("", "Size Avail\n", "3", "1 2 3", "-1 2", "3 -1", "3.5 2"):
            with self.subTest(output=output):
                self.assertIsNone(parse_size_and_available(output))
                result = read_free_bytes(FakeHost(stdout=output))
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("Ensure /data is mounted and readable", result.fix)

    def test_failed_or_unavailable_df_refuses_even_with_valid_stdout(self) -> None:
        for code, error in (
            (1, None), (127, None), (0, FileNotFoundError()),
            (0, subprocess.TimeoutExpired("df", 1)),
        ):
            with self.subTest(code=code, error=error):
                host = FakeHost(code=code, stdout="10 9\n", stderr="private text sentinel", error=error)
                result = read_free_bytes(host)
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("Ensure /data is mounted and readable", result.fix)
                self.assertNotIn("private text sentinel", result.problem + result.fix)
                self.assertEqual(len(host.calls), 1)


def projection_from(host: FakeHost) -> ChunkProjection | Problem:
    return read_chunk_projection(
        host, RENDERED, source="fictitious-opinions", snapshot_date="2099-01-02",
        courts=("court1", "court2"), pipeline="9.8.7", command_path=COMMAND_PATH,
    )


class ProjectionReader(unittest.TestCase):
    def test_scope_is_bound_on_stdin_and_fractional_projection_rounds_up(self) -> None:
        host = FakeHost(stdout=json.dumps({"ready": 13, "chunked": 4, "chunks": 7}))
        self.assertEqual(projection_from(host), ChunkProjection(13, 4, 7, 23))
        self.assertEqual(len(host.calls), 1)
        argv, sql = host.calls[0]
        self.assertEqual(argv, worker.psql_argv(RENDERED))
        assert sql is not None
        for key, value in (
            ("v_source", "fictitious-opinions"), ("v_snapshot_date", "2099-01-02"),
            ("v_courts", "{court1,court2}"), ("v_pipeline", "9.8.7"),
        ):
            self.assertIn(worker.bind(key, value), sql)
            self.assertNotIn(value, " ".join(argv))
        self.assertTrue(sql.endswith(CHUNK_PROJECTION_SQL))
        self.assertIn("d.source = :'v_source'", sql)
        self.assertIn("d.source_snapshot = :'v_snapshot_date'::date", sql)
        self.assertIn("o.court = ANY(:'v_courts'::text[])", sql)
        self.assertIn("d.status = 'ready'", sql)
        self.assertIn("JOIN public.opinions AS o ON o.doc_id = d.doc_id", sql)
        self.assertIn("JOIN ready_documents AS d ON d.doc_id = c.doc_id", sql)
        self.assertIn("c.pipeline = :'v_pipeline'", sql)
        self.assertIn("count(DISTINCT doc_id) FROM selected_chunks", sql)

    def test_no_chunked_documents_projects_zero(self) -> None:
        for ready in (0, 100):
            with self.subTest(ready=ready):
                host = FakeHost(stdout=json.dumps({"ready": ready, "chunked": 0, "chunks": 0}))
                self.assertEqual(projection_from(host), ChunkProjection(ready, 0, 0, 0))

    def test_each_count_requires_a_nonnegative_whole_number(self) -> None:
        counts = {"ready": 13, "chunked": 4, "chunks": 7}
        invalid: list[object] = [None, [], {}, {**counts, "unexpected": 1}]
        for key in counts:
            invalid.append({name: value for name, value in counts.items() if name != key})
            for value in (True, -1, "3", 1.5, None):
                variant: dict[str, object] = {**counts, key: value}
                invalid.append(variant)
        for stdout in ("", "broken", *(json.dumps(value) for value in invalid)):
            with self.subTest(stdout=stdout):
                self.assertEqual(
                    projection_from(FakeHost(stdout=stdout)),
                    Problem("chunk projection counts are invalid", database_fix(RENDERED, COMMAND_PATH)),
                )

    def test_array_delimiters_and_other_invalid_court_ids_refuse_before_io(self) -> None:
        for court in ("court1,court2", "court1}", 'court"1', "court'1", "Court1", ""):
            with self.subTest(court=court):
                host = FakeHost()
                result = read_chunk_projection(
                    host, RENDERED, source="fictitious-opinions", snapshot_date="2099-01-02",
                    courts=(court,), pipeline="9.8.7", command_path=COMMAND_PATH,
                )
                self.assertIsInstance(result, Problem)
                self.assertEqual(host.calls, [])

    def test_failed_or_unavailable_psql_refuses_without_retry(self) -> None:
        for code, error in ((3, None), (127, None), (0, FileNotFoundError()), (0, subprocess.SubprocessError())):
            with self.subTest(code=code, error=error):
                host = FakeHost(code=code, stdout='{"ready":13,"chunked":4,"chunks":7}',
                                stderr="private text sentinel", error=error)
                result = projection_from(host)
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertEqual(result.fix, database_fix(RENDERED, COMMAND_PATH))
                self.assertNotIn("private text sentinel", result.problem + result.fix)
                self.assertEqual(len(host.calls), 1)


class Guard(unittest.TestCase):
    def test_skips_and_both_verdicts_include_the_one_percent_and_refusal_edges(self) -> None:
        guards = ChunkGuards(5, 3, 1300)
        for counts, kind, reason in (
            ({"ready": 0, "chunked": 0, "chunks": 0}, "skipped", "no ready documents"),
            ({"ready": 101, "chunked": 1, "chunks": 9999999}, "skipped", "fewer than 1 %"),
            ({"ready": 100, "chunked": 0, "chunks": 0}, "skipped", "fewer than 1 %"),
            ({"ready": 100, "chunked": 1, "chunks": 30000}, "cleared", "within"),
            ({"ready": 100, "chunked": 1, "chunks": 30001}, "refused", "exceed"),
            ({"ready": 13, "chunked": 4, "chunks": 7}, "cleared", "within"),
        ):
            with self.subTest(counts=counts):
                projection = projection_from(FakeHost(stdout=json.dumps(counts)))
                assert isinstance(projection, ChunkProjection)
                verdict = judge_guard(projection, guards)
                self.assertEqual(verdict.kind, kind)
                self.assertIn(reason, verdict.reason)
                self.assertEqual(verdict.projection, projection)
                self.assertEqual(verdict.refusal, guards.build_refusal_chunks)
                self.assertEqual(verdict.ceiling, guards.serving_ceiling_chunks)
