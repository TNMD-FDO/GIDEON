"""Generation record statements and parsing over a recording host."""

import json
import subprocess
import unittest
from collections.abc import Mapping
from dataclasses import asdict, replace

from gideon.host import caselaw, report, stack, worker
from gideon.host.corpus.record import database_fix
from gideon.host.index.generations import GenerationInputs, generation_name
from gideon.host.index.record import (
    BuildingOutcome,
    GenerationRow,
    PromotedRow,
    read_generation,
    read_generation_by_inputs,
    read_generations,
    read_manifest,
    read_manifest_rows,
    read_promoted,
    write_building,
    write_footprint,
)
from gideon.host.index.sample import SOURCE_ID_MAX_LENGTH
from gideon.host.render.worker import GENERATION_FAILURE_REASONS, PIPELINE_VERSION
from gideon.host.report import Problem
from gideon.host.sysio import Command, PathLike, RealHost

RENDERED = "/fictitious-rendered"
LABEL = "corpus-2099-01-03"
SPACE = "example-embedding-space"
NAME = generation_name("caselaw", LABEL, SPACE)


def inputs_for() -> GenerationInputs:
    return GenerationInputs(
        NAME, "caselaw", LABEL, "fictions-2099-01-02", ("court1",),
        {"court1": caselaw.CourtGeography("circuit", "ca6", None)},
        PIPELINE_VERSION, SPACE, "a" * 64,
    )


def row_for() -> GenerationRow:
    inputs = inputs_for()
    return GenerationRow(
        inputs.name, inputs.collection, inputs.label, (inputs.label,),
        inputs.pipeline, inputs.embedding_space, inputs.inputs_hash, "building", None,
        {"ready": 3, "indexed": 7}, 4096,
        "2099-01-03T12:00:00+00:00", None, None, None, None,
        "2099-01-03T11:00:00+00:00", clustered_at=None,
    )


def notice_for(outcome: str, name: str = NAME, state: str = "building") -> str:
    return "NOTICE:  INDEX_GENERATION:" + json.dumps({
        "outcome": outcome, "name": name, "state": state,
    }) + "\n"


class FakeHost(RealHost):
    """Return recorded answers without reading any file or external service."""

    def __init__(
        self, *, code: int = 0, stdout: str = "", stderr: str = "",
        error: OSError | subprocess.SubprocessError | None = None,
    ) -> None:
        super().__init__()
        self.code, self.stdout, self.stderr, self.error = code, stdout, stderr, error
        self.calls: list[tuple[list[str], str | None]] = []

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, cwd, timeout, passthrough
        assert env is None
        self.calls.append((list(argv), input))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(list(argv), self.code, self.stdout, self.stderr)

    def exists(self, path: PathLike) -> bool:
        return False

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        raise FileNotFoundError(str(path))


class Writer(unittest.TestCase):
    def test_created_outcome_and_one_json_payload_on_stdin(self) -> None:
        inputs = inputs_for()
        host = FakeHost(stderr=notice_for("created"))
        self.assertEqual(write_building(host, RENDERED, inputs), BuildingOutcome("created", NAME, "building"))
        self.assertEqual(len(host.calls), 1)
        argv, sql = host.calls[0]
        self.assertEqual(argv, worker.psql_argv(RENDERED))
        self.assertIn("gideon_worker", argv)
        assert sql is not None
        payload = json.loads(sql.split("\n", 1)[0].removeprefix("\\set v_payload '").removesuffix("'"))
        self.assertEqual(payload, {
            "name": inputs.name, "collection": inputs.collection, "label": inputs.label,
            "pipeline": inputs.pipeline, "embedding_space": inputs.embedding_space,
            "inputs_hash": inputs.inputs_hash,
        })
        for value in (inputs.name, inputs.label, inputs.inputs_hash, inputs.embedding_space):
            self.assertNotIn(value, " ".join(argv))
        self.assertEqual(sql.count("\\set "), 1)
        self.assertIn("BEGIN;", sql)
        self.assertIn("DO $generation$", sql)
        self.assertIn("current_setting('gideon.generation_payload')::jsonb", sql)
        self.assertIn("ARRAY[label]", sql)
        self.assertIn("payload->>'inputs_hash', 'building'", sql)
        self.assertIn("ON CONFLICT (collection, inputs_hash) DO NOTHING", sql)
        self.assertIn("FOR UPDATE", sql)
        self.assertIn("RAISE NOTICE 'INDEX_GENERATION:%'", sql)
        self.assertTrue(sql.endswith("COMMIT;\n"))

    def test_found_and_appended_labels_keep_the_original_name_and_state(self) -> None:
        for label in (LABEL, "corpus-2099-02-03"):
            for state in ("building", "failed", "built", "live", "retired"):
                with self.subTest(label=label, state=state):
                    inputs = replace(inputs_for(), label=label, name=generation_name("caselaw", label, SPACE))
                    host = FakeHost(stderr=notice_for("found", state=state))
                    self.assertEqual(write_building(host, RENDERED, inputs), BuildingOutcome("found", NAME, state))
                    sql = host.calls[0][1] or ""
                    self.assertIn("IF NOT label = ANY(generation.lockfiles) THEN", sql)
                    self.assertIn("SET lockfiles = array_append(lockfiles, label)", sql)
                    self.assertIn("WHERE gen_id = generation.gen_id", sql)
                    self.assertNotIn("SET state", sql)
                    self.assertNotIn("DELETE", sql)

    def test_malformed_or_missing_notices_refuse(self) -> None:
        for stderr in (
            "", "NOTICE: INDEX_GENERATION:{broken}\n",
            notice_for("unexpected"), notice_for("created", name="invalid"),
            notice_for("created", state="unknown"), notice_for("created") * 2,
            notice_for("created", name=generation_name("caselaw", "corpus-2099-02-03", SPACE)),
            'NOTICE: INDEX_GENERATION:{"outcome":"created","name":null,"state":"building"}\n',
        ):
            with self.subTest(stderr=stderr):
                result = write_building(FakeHost(stderr=stderr), RENDERED, inputs_for())
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("invalid outcome", result.problem)
                self.assertIn(stack.logs_fix(RENDERED, "postgres"), result.fix)

    def test_failed_exit_overrides_a_success_notice_and_hides_diagnostics(self) -> None:
        host = FakeHost(code=3, stderr=notice_for("created") + "private text sentinel")
        result = write_building(host, RENDERED, inputs_for())
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("exit 3", result.problem)
        self.assertNotIn("private text sentinel", result.problem + result.fix)


class Reader(unittest.TestCase):
    def test_reads_by_inputs_with_both_values_bound_on_stdin(self) -> None:
        row = row_for()
        host = FakeHost(stdout=json.dumps(asdict(row)) + "\n")
        self.assertEqual(read_generation_by_inputs(
            host, RENDERED, row.collection, row.inputs_hash, command_path="corpus install",
        ), row)
        self.assertEqual(len(host.calls), 1)
        argv, sql = host.calls[0]
        self.assertEqual(argv, worker.psql_argv(RENDERED))
        for value in (row.collection, row.inputs_hash):
            self.assertNotIn(value, " ".join(argv))
        assert sql is not None
        self.assertIn(worker.bind("v_collection", row.collection), sql)
        self.assertIn(worker.bind("v_inputs_hash", row.inputs_hash), sql)
        self.assertIn("row_to_json(generation)", sql)
        self.assertIn("WHERE collection = :'v_collection' AND inputs_hash = :'v_inputs_hash'", sql)

    def test_absent_row_by_inputs_is_none(self) -> None:
        row = row_for()
        self.assertIsNone(read_generation_by_inputs(
            FakeHost(stdout="\n"), RENDERED, row.collection, row.inputs_hash,
        ))

    def test_foreign_or_malformed_rows_by_inputs_refuse(self) -> None:
        row = row_for()
        for stdout in (
            "broken", "[]", json.dumps(asdict(row)) * 2,
            json.dumps({**asdict(row), "collection": "fictitious-collection"}),
            json.dumps({**asdict(row), "inputs_hash": "b" * 64}),
            json.dumps({**asdict(row), "state": "unknown"}),
        ):
            with self.subTest(stdout=stdout):
                result = read_generation_by_inputs(
                    FakeHost(stdout=stdout), RENDERED, row.collection, row.inputs_hash,
                    command_path="corpus install",
                )
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertEqual(result.problem, "generation record row is invalid")
                self.assertIn(stack.logs_fix(RENDERED, "postgres"), result.fix)
                self.assertIn(report.command("corpus install"), result.fix)

    def test_failed_read_by_inputs_overrides_valid_stdout(self) -> None:
        row = row_for()
        host = FakeHost(code=3, stdout=json.dumps(asdict(row)), stderr="private text sentinel")
        result = read_generation_by_inputs(
            host, RENDERED, row.collection, row.inputs_hash, command_path="corpus install",
        )
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("exit 3", result.problem)
        self.assertIn(report.command("corpus install"), result.fix)
        self.assertNotIn("private text sentinel", result.problem + result.fix)
        self.assertEqual(len(host.calls), 1)

    def test_reads_every_column_with_the_name_bound_on_stdin(self) -> None:
        row = row_for()
        host = FakeHost(stdout=json.dumps(asdict(row)) + "\n")
        self.assertEqual(read_generation(host, RENDERED, NAME), row)
        argv, sql = host.calls[0]
        self.assertEqual(argv, worker.psql_argv(RENDERED))
        self.assertNotIn(NAME, " ".join(argv))
        assert sql is not None
        self.assertIn(worker.bind("v_name", NAME), sql)
        self.assertIn("WHERE gen_id = :'v_name'", sql)
        self.assertIn("row_to_json(generation)", sql)

    def test_absent_row_is_none(self) -> None:
        self.assertIsNone(read_generation(FakeHost(stdout="\n"), RENDERED, NAME))

    def test_clustering_timestamp_is_independent_of_the_half_build_timestamps(self) -> None:
        for stamp in (None, "2099-01-03T11:30:00+00:00"):
            with self.subTest(clustered_at=stamp):
                row = replace(row_for(), clustered_at=stamp, lexical_built_at=None)
                self.assertEqual(
                    read_generation(FakeHost(stdout=json.dumps(asdict(row))), RENDERED, NAME), row,
                )

    def test_each_generation_failure_reason_parses(self) -> None:
        for reason in GENERATION_FAILURE_REASONS:
            with self.subTest(reason=reason):
                row = replace(row_for(), state="failed", failure_reason=reason)
                host = FakeHost(stdout=json.dumps(asdict(row)))
                self.assertEqual(read_generation(host, RENDERED, NAME), row)

    def test_failed_generation_and_completed_lifecycles_parse(self) -> None:
        row = row_for()
        stamp = row.lexical_built_at
        for variant in (
            replace(row, state="failed", failure_reason="store"),
            replace(row, state="built", dense_built_at=stamp, built_at=stamp),
            replace(row, state="live", dense_built_at=stamp, built_at=stamp, promoted_at=stamp),
            replace(row, state="retired", dense_built_at=stamp, built_at=stamp, promoted_at=stamp, retired_at=stamp),
        ):
            with self.subTest(state=variant.state):
                self.assertEqual(read_generation(FakeHost(stdout=json.dumps(asdict(variant))), RENDERED, NAME), variant)

    def test_malformed_rows_refuse_with_a_fix(self) -> None:
        row = asdict(row_for())
        variants = [
            {**row, "gen_id": "another-name"}, {**row, "state": "unknown"},
            {**row, "bytes": True}, {**row, "bytes": -1},
            {**row, "counts": {"indexed": True}}, {**row, "counts": {"indexed": -1}},
            {**row, "counts": []}, {**row, "lockfiles": []},
            {**row, "lockfiles": ["corpus-2099-04-01"]},
            {**row, "created_at": None}, {**row, "failure_reason": "store"},
            {**row, "clustered_at": True}, {**row, "clustered_at": 42},
            {**row, "clustered_at": []},
            {**row, "state": "failed"}, {**row, "state": "failed", "failure_reason": "unknown"},
            {**row, "built_at": row["lexical_built_at"]},
            {**row, "dense_built_at": row["lexical_built_at"]},
            {**row, "promoted_at": row["lexical_built_at"]},
            {**row, "state": "retired"}, {**row, "retired_at": row["lexical_built_at"]},
            {key: value for key, value in row.items() if key != "bytes"},
            {key: value for key, value in row.items() if key != "clustered_at"},
            {**row, "unexpected": 1},
        ]
        for stdout in ("broken", "[]", *(json.dumps(item) for item in variants), json.dumps(row) * 2):
            with self.subTest(stdout=stdout):
                result = read_generation(FakeHost(stdout=stdout), RENDERED, NAME)
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertEqual(result.problem, "generation record row is invalid")
                self.assertIn(stack.logs_fix(RENDERED, "postgres"), result.fix)


class GenerationsReader(unittest.TestCase):
    """List the record's generations without changing their order or state."""

    def test_rows_in_reader_order_and_one_unbound_statement_on_stdin(self) -> None:
        first = replace(row_for(), corpus_lockfile="corpus-2099-02-03",
                        lockfiles=("corpus-2099-02-03",),
                        gen_id=generation_name("caselaw", "corpus-2099-02-03", SPACE))
        rows = (first, row_for())
        host = FakeHost(stdout="".join(json.dumps(asdict(row)) + "\n" for row in rows))
        self.assertEqual(read_generations(host, RENDERED, command_path="index report"), rows)
        self.assertEqual(host.calls, [(worker.psql_argv(RENDERED),
            "SELECT row_to_json(generation) FROM public.index_generations AS generation "
            "ORDER BY collection, created_at, gen_id;\n")])
        argv, sql = host.calls[0]
        self.assertNotIn("SELECT", " ".join(argv))
        self.assertNotIn("\\set", sql or "")

    def test_empty_table_is_an_empty_tuple(self) -> None:
        self.assertEqual(read_generations(FakeHost(), RENDERED, command_path="index report"), ())

    def test_any_unreadable_or_invalid_line_refuses_the_whole_read(self) -> None:
        valid = json.dumps(asdict(row_for())) + "\n"
        for invalid in ("broken", "[]", "{}", "", json.dumps(asdict(replace(row_for(), bytes=-1)))):
            with self.subTest(invalid=invalid):
                host = FakeHost(stdout=valid + invalid + "\n")
                self.assertEqual(
                    read_generations(host, RENDERED, command_path="index report"),
                    Problem("generation record row is invalid", database_fix(RENDERED, "index report")),
                )
                self.assertEqual(len(host.calls), 1)

    def test_nonzero_exit_refuses_even_when_stdout_holds_a_valid_row(self) -> None:
        host = FakeHost(code=3, stdout=json.dumps(asdict(row_for())), stderr="private text sentinel")
        self.assertEqual(
            read_generations(host, RENDERED, command_path="index report"),
            Problem("generation record read failed (exit 3)", database_fix(RENDERED, "index report")),
        )
        self.assertEqual(len(host.calls), 1)

    def test_unavailable_command_refuses_with_a_fix_without_retry(self) -> None:
        for code, error in ((127, None), (0, FileNotFoundError()), (0, subprocess.SubprocessError())):
            with self.subTest(code=code, error=error):
                host = FakeHost(code=code, error=error)
                result = read_generations(host, RENDERED, command_path="index report")
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("Restore Docker Compose and psql", result.fix)
                self.assertIn(report.command("index report"), result.fix)
                self.assertEqual(len(host.calls), 1)


class PromotedReader(unittest.TestCase):
    def test_reads_an_empty_array_or_multiple_live_rows_on_stdin(self) -> None:
        rows = (
            PromotedRow(NAME, "caselaw", 4096),
            PromotedRow(generation_name("caselaw", "corpus-2099-02-03", SPACE), "caselaw", 8192),
        )
        for expected in ((), rows):
            with self.subTest(rows=expected):
                host = FakeHost(stdout=json.dumps([asdict(row) for row in expected]))
                self.assertEqual(read_promoted(host, RENDERED, command_path="preflight"), expected)
                self.assertEqual(len(host.calls), 1)
                argv, sql = host.calls[0]
                self.assertEqual(argv, worker.psql_argv(RENDERED))
                self.assertNotIn("SELECT", " ".join(argv))
                assert sql is not None
                self.assertIn("SELECT gen_id, collection, bytes FROM public.index_generations", sql)
                self.assertIn("WHERE state = 'live'", sql)
                self.assertIn("json_agg(promoted ORDER BY gen_id)", sql)
                self.assertIn("'[]'::json", sql)

    def test_malformed_array_or_row_refuses_the_whole_read(self) -> None:
        valid = asdict(PromotedRow(NAME, "caselaw", 4096))
        invalid_rows: tuple[object, ...] = (
            None, [], {}, {**valid, "bytes": True}, {**valid, "bytes": -1},
            {**valid, "bytes": "4096"}, {**valid, "collection": None},
            {**valid, "gen_id": 3}, {**valid, "gen_id": ""},
            {key: value for key, value in valid.items() if key != "bytes"},
            {**valid, "unexpected": 1},
        )
        for stdout in ("", "broken", "{}", "null", *(json.dumps([valid, row]) for row in invalid_rows)):
            with self.subTest(stdout=stdout):
                self.assertEqual(
                    read_promoted(FakeHost(stdout=stdout), RENDERED, command_path="preflight"),
                    Problem("promoted generation rows are invalid", database_fix(RENDERED, "preflight")),
                )

    def test_failed_exit_overrides_a_valid_array(self) -> None:
        host = FakeHost(code=3, stdout="[]", stderr="private text sentinel")
        self.assertEqual(
            read_promoted(host, RENDERED, command_path="preflight"),
            Problem("promoted generation read failed (exit 3)", database_fix(RENDERED, "preflight")),
        )
        self.assertEqual(len(host.calls), 1)


class FootprintWriter(unittest.TestCase):
    def test_measured_half_is_bound_and_both_halves_are_returned(self) -> None:
        expected = {"lexical_bytes": 4096, "dense_bytes": 8192, "bytes": 12288}
        host = FakeHost(stdout=json.dumps(expected))
        self.assertEqual(
            write_footprint(host, RENDERED, NAME, expected["dense_bytes"], command_path="index build"),
            expected,
        )
        self.assertEqual(len(host.calls), 1)
        argv, sql = host.calls[0]
        self.assertEqual(argv, worker.psql_argv(RENDERED))
        assert sql is not None
        self.assertIn(worker.bind("v_name", NAME), sql)
        self.assertIn(worker.bind("v_dense_bytes", str(expected["dense_bytes"])), sql)
        for value in (NAME, str(expected["dense_bytes"]), "UPDATE"):
            self.assertNotIn(value, " ".join(argv))
        self.assertIn("WHERE gen_id = :'v_name'", sql)
        self.assertIn("SET counts = counts || jsonb_build_object(", sql)
        self.assertIn("'dense_bytes', :'v_dense_bytes'::bigint", sql)
        self.assertIn("+ :'v_dense_bytes'::bigint", sql)
        self.assertIn("RETURNING counts, bytes", sql)
        self.assertIn("SELECT jsonb_build_object(", sql)
        self.assertTrue(sql.endswith(") FROM written;\n"))

    def test_older_rows_backfill_the_lexical_half_and_existing_zero_is_kept(self) -> None:
        for lexical in (0, row_for().bytes):
            with self.subTest(lexical=lexical):
                expected = {"lexical_bytes": lexical, "dense_bytes": 7, "bytes": lexical + 7}
                host = FakeHost(stdout=json.dumps(expected))
                self.assertEqual(write_footprint(host, RENDERED, NAME, 7, command_path="index build"), expected)
                sql = " ".join((host.calls[0][1] or "").split())
                # Presence distinguishes a measured zero from an older row's absent key.
                fallback = "CASE WHEN counts ? 'lexical_bytes' THEN (counts->>'lexical_bytes')::bigint ELSE bytes END"
                self.assertIn(f"'lexical_bytes', {fallback}", sql)
                self.assertIn(f"bytes = {fallback} + :'v_dense_bytes'::bigint", sql)

    def test_missing_malformed_or_inconsistent_results_refuse(self) -> None:
        valid = {"lexical_bytes": 3, "dense_bytes": 7, "bytes": 10}
        invalid: tuple[object, ...] = (
            [], None, {}, {**valid, "bytes": 11}, {**valid, "dense_bytes": 6, "bytes": 9},
            {**valid, "lexical_bytes": -1, "bytes": 6}, {**valid, "bytes": True},
            {**valid, "lexical_bytes": "3"}, {**valid, "unexpected": 1},
        )
        for stdout in ("", "broken", *(json.dumps(value) for value in invalid)):
            with self.subTest(stdout=stdout):
                self.assertEqual(
                    write_footprint(FakeHost(stdout=stdout), RENDERED, NAME, 7, command_path="index build"),
                    Problem("generation footprint write returned invalid byte counts", database_fix(RENDERED, "index build")),
                )

    def test_failed_exit_overrides_valid_byte_counts(self) -> None:
        host = FakeHost(code=3, stdout=json.dumps({"lexical_bytes": 3, "dense_bytes": 7, "bytes": 10}),
                        stderr="private text sentinel")
        self.assertEqual(
            write_footprint(host, RENDERED, NAME, 7, command_path="index build"),
            Problem("generation footprint write failed (exit 3)", database_fix(RENDERED, "index build")),
        )
        self.assertEqual(len(host.calls), 1)


class Commands(unittest.TestCase):
    def test_unavailable_tools_and_nonzero_reads_refuse_without_retry(self) -> None:
        for operation in (
            lambda host: read_generation(host, RENDERED, NAME),
            lambda host: read_generation_by_inputs(
                host, RENDERED, inputs_for().collection, inputs_for().inputs_hash,
            ),
            lambda host: write_building(host, RENDERED, inputs_for()),
            lambda host: read_promoted(host, RENDERED, command_path="index build"),
            lambda host: write_footprint(host, RENDERED, NAME, 7, command_path="index build"),
        ):
            for code, error in ((127, None), (0, FileNotFoundError()), (0, subprocess.SubprocessError())):
                with self.subTest(code=code, error=error):
                    host = FakeHost(code=code, error=error)
                    result = operation(host)
                    self.assertIsInstance(result, Problem)
                    assert isinstance(result, Problem)
                    self.assertIn("Restore Docker Compose and psql", result.fix)
                    self.assertIn(report.command("index build"), result.fix)
                    self.assertEqual(len(host.calls), 1)
        result = read_generation(FakeHost(code=1, stdout=json.dumps(asdict(row_for()))), RENDERED, NAME)
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("exit 1", result.problem)

    def test_fixes_follow_the_installed_command_form(self) -> None:
        report.set_installed_form(True)
        self.addCleanup(report.set_installed_form, False)
        result = read_generation(FakeHost(code=1), RENDERED, NAME)
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("gideon index build", result.fix)
        self.assertNotIn("python3", result.fix)


class ManifestReader(unittest.TestCase):
    def test_requested_ids_and_generation_are_bound_on_stdin_and_return_hashes(self) -> None:
        hashes = {"example:opinion:17": "b" * 64, "example:opinion:29": "c" * 64}
        host = FakeHost(stdout="\n".join(
            json.dumps({"source_id": identifier, "canonical_text_sha256": digest})
            for identifier, digest in hashes.items()
        ))

        self.assertEqual(read_manifest(host, RENDERED, NAME, tuple(hashes), "index build caselaw --sample"), hashes)

        self.assertEqual(len(host.calls), 1)
        argv, sql = host.calls[0]
        self.assertEqual(argv, worker.psql_argv(RENDERED))
        assert sql is not None
        self.assertIn(worker.bind("v_name", NAME), sql)
        self.assertIn(worker.bind("v_source_ids", json.dumps(list(hashes), separators=(",", ":"))), sql)
        for value in (NAME, *hashes, *hashes.values()):
            self.assertNotIn(value, " ".join(argv))

    def test_missing_requested_documents_are_omitted(self) -> None:
        self.assertEqual(read_manifest(FakeHost(), RENDERED, NAME, ("example:opinion:17",), "index build"), {})

    def test_invalid_manifest_rows_refuse_without_returning_partial_hashes(self) -> None:
        row = {"source_id": "example:opinion:17", "canonical_text_sha256": "b" * 64}
        variants: tuple[object, ...] = (
            {**row, "source_id": "example:opinion:29"},
            {**row, "source_id": "Invalid identifier"},
            {**row, "source_id": None},
            {**row, "canonical_text_sha256": "B" * 64},
            {**row, "canonical_text_sha256": "b" * 63},
            {**row, "canonical_text_sha256": None},
            {"source_id": row["source_id"]},
            {**row, "unexpected": "value"}, [], None,
        )
        for stdout in ("Invalid JSON", json.dumps(row) + "\n" + json.dumps(row), *(json.dumps(value) for value in variants)):
            with self.subTest(stdout=stdout):
                host = FakeHost(stdout=stdout)
                result = read_manifest(host, RENDERED, NAME, (str(row["source_id"]),), "index build caselaw --sample")
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertEqual(result.problem, "generation manifest row is invalid")
                self.assertIn(stack.logs_fix(RENDERED, "postgres"), result.fix)
                self.assertIn(report.command("index build caselaw --sample"), result.fix)
                self.assertEqual(len(host.calls), 1)

    def test_failed_read_and_unavailable_tools_refuse_without_retry(self) -> None:
        for code, error in ((3, None), (127, None), (0, FileNotFoundError()), (0, subprocess.SubprocessError())):
            with self.subTest(code=code, error=error):
                host = FakeHost(code=code, error=error, stderr="Private database diagnostics")
                result = read_manifest(host, RENDERED, NAME, ("example:opinion:17",), "index build caselaw --sample")
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                if code == 3:
                    self.assertIn("exit 3", result.problem)
                    self.assertIn(stack.logs_fix(RENDERED, "postgres"), result.fix)
                else:
                    self.assertIn("Restore Docker Compose and psql", result.fix)
                self.assertNotIn(host.stderr, result.problem + result.fix)
                self.assertIn(report.command("index build caselaw --sample"), result.fix)
                self.assertEqual(len(host.calls), 1)


class ManifestRowsReader(unittest.TestCase):
    """The whole manifest is a validated, ordered projection."""

    def test_reads_every_identity_with_generation_bound_on_stdin(self) -> None:
        rows = (
            {"source_id": "example:opinion:17", "canonical_text_sha256": "b" * 64, "doc_id": "e" * 64},
            {"source_id": "example:opinion:29", "canonical_text_sha256": "c" * 64, "doc_id": "f" * 64},
        )
        host = FakeHost(stdout="\n".join(json.dumps(row) for row in rows))
        self.assertEqual(read_manifest_rows(host, RENDERED, NAME, "tools.synthetic.draft"), tuple(
            (row["source_id"], row["canonical_text_sha256"], row["doc_id"]) for row in rows
        ))
        self.assertEqual(len(host.calls), 1)
        argv, sql = host.calls[0]
        self.assertEqual(argv, worker.psql_argv(RENDERED))
        assert sql is not None
        self.assertIn(worker.bind("v_name", NAME), sql)
        self.assertIn("SELECT source_id, canonical_text_sha256, doc_id FROM public.generation_documents", sql)
        self.assertIn("WHERE gen_id = :'v_name' ORDER BY source_id", sql)
        self.assertNotIn("v_source_ids", sql)
        self.assertNotIn(NAME, " ".join(argv))

    def test_empty_manifest_returns_no_rows(self) -> None:
        host = FakeHost()
        self.assertEqual(read_manifest_rows(host, RENDERED, NAME, "tools.synthetic.draft"), ())
        self.assertEqual(len(host.calls), 1)

    def test_malformed_rows_refuse_the_whole_read_without_partial_results(self) -> None:
        row = {"source_id": "example:opinion:29", "canonical_text_sha256": "c" * 64, "doc_id": "f" * 64}
        first = {**row, "source_id": "example:opinion:17"}
        invalid: tuple[object, ...] = (
            None, [], {},
            {**row, "source_id": ""}, {**row, "source_id": None},
            {**row, "source_id": "Invalid identifier"},
            {**row, "source_id": "x" * (SOURCE_ID_MAX_LENGTH + 1)},
            {**row, "canonical_text_sha256": "C" * 64},
            {**row, "canonical_text_sha256": "c" * 63},
            {**row, "canonical_text_sha256": None},
            {**row, "doc_id": "F" * 64}, {**row, "doc_id": "f" * 63},
            {**row, "doc_id": None}, {**row, "doc_id": 1},
            {**row, "unexpected": "Fictitious private diagnostic"},
            *({key: value for key, value in row.items() if key != missing} for missing in row),
        )
        for line in ("Invalid JSON", json.dumps(first), *(json.dumps(value) for value in invalid)):
            for prefix in ("", json.dumps(first) + "\n"):
                # A single valid row is the control; repeating its source id
                # is a malformed manifest just as it is for a filtered read.
                if not prefix and line == json.dumps(first):
                    continue
                with self.subTest(line=line, after_valid=bool(prefix)):
                    host = FakeHost(stdout=prefix + line)
                    result = read_manifest_rows(host, RENDERED, NAME, "tools.synthetic.draft")
                    self.assertEqual(result, Problem("generation manifest row is invalid", database_fix(RENDERED, "tools.synthetic.draft")))
                    self.assertEqual(len(host.calls), 1)

    def test_failed_read_and_unavailable_tools_refuse_even_with_valid_stdout(self) -> None:
        row = {"source_id": "example:opinion:17", "canonical_text_sha256": "b" * 64, "doc_id": "e" * 64}
        for code, error in ((3, None), (127, None), (0, FileNotFoundError()), (0, subprocess.SubprocessError())):
            with self.subTest(code=code, error=error):
                host = FakeHost(code=code, stdout=json.dumps(row), error=error, stderr="Fictitious private database diagnostic")
                result = read_manifest_rows(host, RENDERED, NAME, "tools.synthetic.draft")
                assert isinstance(result, Problem)
                if code == 3:
                    self.assertEqual(result.problem, "generation manifest read failed (exit 3)")
                    self.assertEqual(result.fix, database_fix(RENDERED, "tools.synthetic.draft"))
                else:
                    self.assertIn("Restore Docker Compose and psql", result.fix)
                self.assertIn(report.command("tools.synthetic.draft"), result.fix)
                self.assertNotIn(host.stderr, result.problem + result.fix)
                self.assertEqual(len(host.calls), 1)
