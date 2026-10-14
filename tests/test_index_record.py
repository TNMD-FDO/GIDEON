"""Generation record statements and parsing over a recording host."""

import json
import subprocess
import unittest
from collections.abc import Mapping
from dataclasses import asdict, replace

from gideon.host import caselaw, report, stack, worker
from gideon.host.index.generations import GenerationInputs, generation_name
from gideon.host.index.record import (
    BuildingOutcome,
    GenerationRow,
    read_generation,
    write_building,
)
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
        "2099-01-03T11:00:00+00:00",
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


class Commands(unittest.TestCase):
    def test_unavailable_tools_and_nonzero_reads_refuse_without_retry(self) -> None:
        for operation in (
            lambda host: read_generation(host, RENDERED, NAME),
            lambda host: write_building(host, RENDERED, inputs_for()),
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
