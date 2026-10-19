"""Corpus lockfile, stage count, and observation statements over a recording host."""

import json
import re
import subprocess
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from gideon.host import report, stack, staging, worker
from gideon.host.corpus.lockfile import load_lockfile, render_errors
from gideon.host.corpus.record import (
    WATCH_STATE_SQL,
    CutRow,
    InstalledOutcome,
    Observation,
    StageCounts,
    WatchState,
    copy_lockfile,
    database_fix,
    read_cut,
    read_lockfiles,
    read_stage_counts,
    read_watch_state,
    write_cut,
    write_derived_cut,
    write_install,
    write_installed,
    write_observations,
    write_stage_counts,
)
from gideon.host.corpus.sources import SourceDefinition
from gideon.host.render.worker import STAGE_TABLES
from gideon.host.report import Problem
from gideon.host.sysio import Command, PathLike, RealHost
from gideon.host.worker import psql_argv

FIXTURE = Path(__file__).parent / "fixtures/corpus/corpus-2099-01-03.yaml"
RENDERED = "/rendered"
KNOWN = {
    "example": SourceDefinition(
        "example", "https://example.invalid/archive/", True, ("ca6",),
        lambda: (), lambda _: Problem("Fictitious source has no index reader", "Use the fixture."),
    ),
}


class FakeHost(RealHost):
    """Return one psql answer and record its argv and stdin."""

    def __init__(
        self, *, code: int = 0, stdout: str = "", stderr: str = "",
        error: OSError | subprocess.SubprocessError | None = None,
    ) -> None:
        super().__init__()
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        self.error = error
        self.calls: list[tuple[list[str], str | None]] = []

    def run(
        self,
        argv: Command,
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, cwd, env, timeout, passthrough
        command = list(argv)
        self.calls.append((command, input))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(command, self.code, self.stdout, self.stderr)


class Writer(unittest.TestCase):
    """The writer binds data and refuses a conflicting existing record."""

    def setUp(self) -> None:
        loaded = load_lockfile(FIXTURE, known_sources=KNOWN)
        self.assertTrue(loaded.ok, render_errors(loaded.errors))
        assert loaded.lockfile is not None
        self.lockfile = loaded.lockfile

    def test_one_transaction_has_every_value_on_stdin(self) -> None:
        host = FakeHost()
        result = write_cut(
            host, RENDERED, self.lockfile,
            {"example": self.lockfile.cut_at}, self.lockfile.cut_at,
            command_path="corpus cut",
        )
        self.assertIsNone(result)
        self.assertEqual(len(host.calls), 1)
        argv, sql = host.calls[0]
        self.assertEqual(argv, psql_argv(RENDERED))
        self.assertIn("ON_ERROR_STOP=1", argv)
        self.assertNotIn(self.lockfile.label, " ".join(argv))
        self.assertNotIn(self.lockfile.sources["example"].sidecar_sha256, " ".join(argv))
        assert sql is not None
        self.assertIn("\\set v_payload '", sql)
        self.assertIn(self.lockfile.label, sql)
        self.assertIn(self.lockfile.sources["example"].sidecar_sha256, sql)
        self.assertIn("BEGIN;", sql)
        self.assertIn("DO $cut$", sql)
        self.assertIn("CORPUS_LOCKFILE_CONFLICT", sql)
        self.assertIn("CORPUS_SNAPSHOT_CONFLICT", sql)
        self.assertIn("public.corpus_lockfiles", sql)
        self.assertIn("public.source_snapshots", sql)
        self.assertIn("public.lockfile_sources", sql)
        self.assertTrue(sql.endswith("COMMIT;\n"))
        payload_match = re.search(r"\\set v_payload '([^']*)'", sql)
        assert payload_match is not None
        self.assertNotIn("install", json.loads(payload_match.group(1)))

    def test_install_uses_the_same_transaction_with_state_move_on_stdin(self) -> None:
        host = FakeHost()
        result = write_install(
            host, RENDERED, self.lockfile,
            {"example": self.lockfile.cut_at}, self.lockfile.cut_at,
            command_path="corpus install",
        )
        self.assertIsNone(result)
        self.assertEqual(len(host.calls), 1)
        argv, sql = host.calls[0]
        self.assertEqual(argv, psql_argv(RENDERED))
        self.assertNotIn(self.lockfile.label, " ".join(argv))
        self.assertNotIn(self.lockfile.sources["example"].sidecar_sha256, " ".join(argv))
        assert sql is not None
        payload_match = re.search(r"\\set v_payload '([^']*)'", sql)
        assert payload_match is not None
        payload = json.loads(payload_match.group(1))
        self.assertIs(payload["install"], True)
        self.assertEqual(payload["label"], self.lockfile.label)
        self.assertEqual(payload["sources"][0]["fetched_at"], self.lockfile.cut_at)
        self.assertIn("DO $cut$", sql)
        self.assertIn("CORPUS_LOCKFILE_SUPERSEDED:%", sql)
        self.assertIn("SET state = 'installing'", sql)
        self.assertIn("WHERE label = cut_label AND state = 'cut'", sql)
        self.assertTrue(sql.endswith("COMMIT;\n"))

    def test_superseded_write_refuses_with_install_fix(self) -> None:
        host = FakeHost(
            code=3,
            stderr=f"ERROR: CORPUS_LOCKFILE_SUPERSEDED:{self.lockfile.label}\n",
        )
        result = write_install(
            host, RENDERED, self.lockfile,
            {"example": self.lockfile.cut_at}, self.lockfile.cut_at,
            command_path="corpus install",
        )
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn(self.lockfile.label, result.problem)
        self.assertIn("superseded", result.problem)
        self.assertIn("corpus install", result.fix)

    def test_missing_fetch_time_refuses_before_database(self) -> None:
        host = FakeHost()
        result = write_cut(host, RENDERED, self.lockfile, {}, self.lockfile.cut_at,
                           command_path="corpus cut")
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("example", result.problem)
        self.assertIn("corpus cut", result.fix)
        self.assertEqual(host.calls, [])

    def test_snapshot_conflict_names_snapshot_and_no_deletion(self) -> None:
        pin = self.lockfile.sources["example"]
        host = FakeHost(code=3, stderr=f"ERROR: CORPUS_SNAPSHOT_CONFLICT:example:{pin.snapshot_date}\n")
        result = write_cut(host, RENDERED, self.lockfile,
                           {"example": self.lockfile.cut_at}, self.lockfile.cut_at,
                           command_path="corpus cut")
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn(f"example {pin.snapshot_date}", result.problem)
        self.assertNotIn("delete", result.fix.lower())
        self.assertIn("corpus cut", result.fix)

    def test_label_conflict_and_tool_failure_refuse(self) -> None:
        conflict = FakeHost(code=3, stderr=f"ERROR: CORPUS_LOCKFILE_CONFLICT:{self.lockfile.label}\n")
        result = write_cut(conflict, RENDERED, self.lockfile,
                           {"example": self.lockfile.cut_at}, self.lockfile.cut_at,
                           command_path="corpus cut")
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn(self.lockfile.label, result.problem)
        self.assertNotIn("delete", result.fix.lower())

        unavailable = FakeHost(code=127, stderr="psql unavailable")
        result = write_cut(unavailable, RENDERED, self.lockfile,
                           {"example": self.lockfile.cut_at}, self.lockfile.cut_at,
                           command_path="corpus cut")
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("exit 127", result.problem)
        self.assertIn("corpus cut", result.fix)


class InstalledWriter(unittest.TestCase):
    """The installed move binds its data and parses one transaction's outcome."""

    def setUp(self) -> None:
        loaded = load_lockfile(FIXTURE, known_sources=KNOWN)
        self.assertTrue(loaded.ok, render_errors(loaded.errors))
        assert loaded.lockfile is not None
        self.lockfile = loaded.lockfile
        self.outcome = {
            "label": self.lockfile.label, "state": "installed",
            "installed_at": self.lockfile.cut_at,
            "superseded": ["corpus-2098-11-01", "corpus-2098-12-01"],
        }

    def notice(self, value: object) -> str:
        return "NOTICE: CORPUS_INSTALLED:" + json.dumps(value) + "\n"

    def test_one_transaction_moves_states_and_returns_sorted_labels(self) -> None:
        host = FakeHost(stderr=self.notice(self.outcome))
        result = write_installed(
            host, RENDERED, self.lockfile.label, self.lockfile.cut_at,
            command_path="corpus install",
        )
        self.assertEqual(result, InstalledOutcome(
            self.lockfile.label, "installed", self.lockfile.cut_at,
            tuple(self.outcome["superseded"]),
        ))
        self.assertEqual(len(host.calls), 1)
        argv, sql = host.calls[0]
        self.assertEqual(argv, psql_argv(RENDERED))
        for value in (self.lockfile.label, self.lockfile.cut_at):
            self.assertNotIn(value, " ".join(argv))
        assert sql is not None
        payload = json.loads(sql.split("\n", 1)[0].removeprefix("\\set v_payload '").removesuffix("'"))
        self.assertEqual(payload, {
            "label": self.lockfile.label, "installed_at": self.lockfile.cut_at,
        })
        self.assertEqual(sql.count("\\set "), 1)
        self.assertIn("BEGIN;", sql)
        self.assertIn("DO $installed$", sql)
        self.assertIn("current_setting('gideon.installed_payload')::jsonb", sql)
        self.assertIn("WHERE label = cut_label FOR UPDATE", sql)
        for tag in ("MISSING", "SUPERSEDED", "NOT_INSTALLING"):
            self.assertIn(f"CORPUS_LOCKFILE_{tag}:%", sql)
        self.assertIn("IF installed.state = 'installing' THEN", sql)
        self.assertIn("SET state = 'installed', installed_at = (payload->>'installed_at')::timestamptz", sql)
        move = sql.split("WITH moved AS (", 1)[1].split("RAISE NOTICE", 1)[0]
        self.assertIn("SET state = 'superseded'", move)
        self.assertIn("WHERE label < cut_label AND state IN ('installing', 'installed')", move)
        self.assertIn("jsonb_agg(label ORDER BY label)", move)
        self.assertNotIn("installed_at", move)
        self.assertNotIn("DELETE", sql)
        self.assertIn("RAISE NOTICE 'CORPUS_INSTALLED:%'", sql)
        self.assertTrue(sql.endswith("COMMIT;\n"))

    def test_already_installed_outcome_keeps_its_timestamp(self) -> None:
        requested_at = "2099-02-03T12:00:00+00:00"
        host = FakeHost(stderr=self.notice({**self.outcome, "superseded": []}))
        result = write_installed(
            host, RENDERED, self.lockfile.label, requested_at, command_path="corpus install",
        )
        self.assertEqual(result, InstalledOutcome(
            self.lockfile.label, "installed", self.lockfile.cut_at, (),
        ))
        sql = host.calls[0][1] or ""
        installing_branch = sql.split("IF installed.state = 'installing' THEN", 1)[1].split("END IF;", 1)[0]
        self.assertIn("installed_at =", installing_branch)
        self.assertEqual(sql.count("installed_at ="), 1)

    def test_tagged_failures_have_the_install_fix(self) -> None:
        for tag, detail in (
            ("MISSING", "is not recorded"), ("NOT_INSTALLING", "is not installing"),
            ("SUPERSEDED", "is superseded"),
        ):
            with self.subTest(tag=tag):
                host = FakeHost(code=3, stderr=f"ERROR: CORPUS_LOCKFILE_{tag}:{self.lockfile.label}\n")
                result = write_installed(
                    host, RENDERED, self.lockfile.label, self.lockfile.cut_at,
                    command_path="corpus install",
                )
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertEqual(result.problem, f"lockfile label {self.lockfile.label} {detail}")
                fix = "with the newest committed label." if tag == "SUPERSEDED" else "again."
                self.assertEqual(result.fix, f"Run {report.command('corpus install')} {fix}")

    def test_invalid_or_missing_notices_refuse(self) -> None:
        for stderr in (
            "", "NOTICE: CORPUS_INSTALLED:{broken}\n", self.notice([]),
            self.notice(self.outcome) * 2,
            *(self.notice({**self.outcome, **change}) for change in (
                {"label": "corpus-2099-02-03"}, {"state": "installing"},
                {"installed_at": None}, {"installed_at": "invalid"},
                {"superseded": None}, {"superseded": [None]},
                {"superseded": [self.lockfile.label]}, {"superseded": ["invalid"]},
                {"superseded": ["corpus-2098-12-01", "corpus-2098-11-01"]},
                {"superseded": ["corpus-2098-12-01"] * 2}, {"unexpected": 1},
            )),
            self.notice({key: value for key, value in self.outcome.items() if key != "state"}),
        ):
            with self.subTest(stderr=stderr):
                result = write_installed(
                    FakeHost(stderr=stderr), RENDERED, self.lockfile.label, self.lockfile.cut_at,
                    command_path="corpus install",
                )
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("invalid outcome", result.problem)
                self.assertIn(stack.logs_fix(RENDERED, "postgres"), result.fix)
                self.assertIn(report.command("corpus install"), result.fix)

    def test_command_failures_override_notices_without_retry(self) -> None:
        for code, error in ((3, None), (127, None), (0, FileNotFoundError()), (0, subprocess.SubprocessError())):
            with self.subTest(code=code, error=error):
                host = FakeHost(code=code, error=error,
                                stderr=self.notice(self.outcome) + "private text sentinel")
                result = write_installed(
                    host, RENDERED, self.lockfile.label, self.lockfile.cut_at,
                    command_path="corpus install",
                )
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn(report.command("corpus install"), result.fix)
                self.assertNotIn("private text sentinel", result.problem + result.fix)
                if code == 3:
                    self.assertIn("exit 3", result.problem)
                    self.assertIn(stack.logs_fix(RENDERED, "postgres"), result.fix)
                else:
                    self.assertIn("Restore Docker Compose and psql", result.fix)
                self.assertEqual(len(host.calls), 1)

    def test_tagged_fix_uses_the_installed_command_form(self) -> None:
        report.set_installed_form(True)
        self.addCleanup(report.set_installed_form, False)
        host = FakeHost(code=3, stderr=f"ERROR: CORPUS_LOCKFILE_MISSING:{self.lockfile.label}\n")
        result = write_installed(
            host, RENDERED, self.lockfile.label, self.lockfile.cut_at,
            command_path="corpus install",
        )
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertEqual(result.fix, "Run gideon corpus install again.")


class DerivedWriter(unittest.TestCase):
    """Derived records reuse rows without advancing snapshot verification."""

    def setUp(self) -> None:
        fixture = FIXTURE.parent / "corpus-2099-01-07.yaml"
        loaded = load_lockfile(fixture, known_sources=KNOWN)
        self.assertTrue(loaded.ok, render_errors(loaded.errors))
        assert loaded.lockfile is not None
        self.lockfile = loaded.lockfile

    def test_one_transaction_binds_payload_and_only_reads_snapshots(self) -> None:
        host = FakeHost()
        self.assertIsNone(write_derived_cut(host, RENDERED, self.lockfile, command_path="corpus cut"))
        self.assertEqual(len(host.calls), 1)
        argv, sql = host.calls[0]
        self.assertEqual(argv, psql_argv(RENDERED))
        self.assertNotIn(self.lockfile.label, " ".join(argv))
        self.assertNotIn(self.lockfile.base or "", " ".join(argv))
        self.assertNotIn(self.lockfile.sources["example"].sidecar_sha256, " ".join(argv))
        assert sql is not None
        self.assertIn("\\set v_payload '", sql)
        self.assertIn("BEGIN;", sql)
        self.assertIn("DO $cut$", sql)
        self.assertTrue(sql.endswith("COMMIT;\n"))
        payload = json.loads(sql.split("\\set v_payload '", 1)[1].split("'", 1)[0])
        self.assertEqual(payload["base"], self.lockfile.base)
        self.assertEqual(payload["label"], self.lockfile.label)
        self.assertEqual(payload["sources"][0]["sidecar_sha256"],
                         self.lockfile.sources["example"].sidecar_sha256)
        self.assertNotIn("verified_at", payload)
        self.assertNotIn("fetched_at", payload["sources"][0])
        self.assertNotIn("verified_at", sql)
        self.assertNotIn("fetched_at", sql)
        self.assertIn("CORPUS_SNAPSHOT_MISSING", sql)
        self.assertIn("CORPUS_BASE_MISSING", sql)
        self.assertNotIn("UPDATE public.source_snapshots", sql)
        self.assertNotIn("INSERT INTO public.source_snapshots", sql)
        self.assertEqual(sql.count("IF label_exists THEN"), 1)

    def test_full_lockfile_omits_base_check(self) -> None:
        host = FakeHost()
        self.assertIsNone(write_derived_cut(host, RENDERED, replace(self.lockfile, base=None),
                                           command_path="corpus cut"))
        sql = host.calls[0][1]
        assert sql is not None
        self.assertNotIn("CORPUS_BASE_MISSING", sql)

    def test_missing_snapshot_and_base_name_install_fix(self) -> None:
        pin = self.lockfile.sources["example"]
        for marker, detail in (
            (f"CORPUS_SNAPSHOT_MISSING:example:{pin.snapshot_date}",
             f"example {pin.snapshot_date}"),
            (f"CORPUS_BASE_MISSING:{self.lockfile.base}", self.lockfile.base),
        ):
            with self.subTest(marker=marker):
                host = FakeHost(code=3, stderr=f"ERROR: {marker}\n")
                result = write_derived_cut(host, RENDERED, self.lockfile, command_path="corpus cut")
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn(detail, result.problem)
                self.assertIn(f"corpus install {self.lockfile.base}", result.fix)
                self.assertIn("corpus cut", result.fix)

    def test_conflicts_and_command_failure(self) -> None:
        pin = self.lockfile.sources["example"]
        for marker, detail in (
            (f"CORPUS_SNAPSHOT_CONFLICT:example:{pin.snapshot_date}",
             f"example {pin.snapshot_date}"),
            (f"CORPUS_LOCKFILE_CONFLICT:{self.lockfile.label}", self.lockfile.label),
        ):
            with self.subTest(marker=marker):
                result = write_derived_cut(FakeHost(code=3, stderr=f"ERROR: {marker}\n"),
                                           RENDERED, self.lockfile, command_path="corpus cut")
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn(detail, result.problem)
                self.assertIn("Restore the matching lockfile", result.fix)
        result = write_derived_cut(FakeHost(code=4, stderr="connection failed"),
                                   RENDERED, self.lockfile, command_path="corpus cut")
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("exit 4", result.problem)
        self.assertIn("corpus cut", result.fix)


class StageCountsWriter(unittest.TestCase):
    """A stage's count payload is bound once and write outcomes refuse conflicts."""

    def setUp(self) -> None:
        self.record = staging.StageRecord(
            label="corpus-2099-01-03", source="example", snapshot="example-2099-01-02",
            courts=["ca6", "ca9"], inputs={}, records={},
            counts={
                "ca6": {"dockets": 0, "opinion-clusters": 2, "citations": 3, "opinions": 4},
                "ca9": {"dockets": 5, "opinion-clusters": 6, "citations": 7, "opinions": 8},
            },
            job=1, seconds=0.5, staged_at="2099-01-03T04:05:06+00:00", schema=1,
        )

    def write(self, host: FakeHost) -> int | Problem:
        return write_stage_counts(
            host, RENDERED, self.record.label, self.record.source, self.record,
            command_path="corpus install",
        )

    def test_one_transaction_binds_all_values_on_stdin_and_parses_inserted_count(self) -> None:
        host = FakeHost(stdout=json.dumps({"inserted": len(self.record.courts)}) + "\n")
        self.assertEqual(self.write(host), len(self.record.courts))
        self.assertEqual(len(host.calls), 1)
        argv, sql = host.calls[0]
        self.assertEqual(argv, psql_argv(RENDERED))
        for value in (self.record.label, self.record.source, self.record.staged_at, *self.record.courts):
            self.assertNotIn(value, " ".join(argv))
        assert sql is not None
        match = re.search(r"\\set v_payload '([^']*)'", sql)
        assert match is not None
        self.assertEqual(json.loads(match.group(1)), {
            "label": self.record.label,
            "source": self.record.source,
            "staged_at": self.record.staged_at,
            "courts": [
                {"court": court, **{table.replace("-", "_"): self.record.counts[court][table]
                                    for table in STAGE_TABLES[1:]}}
                for court in self.record.courts
            ],
        })
        self.assertEqual(sql.count("BEGIN;"), 1)
        self.assertEqual(sql.count("COMMIT;"), 1)
        self.assertIn("\\set QUIET on", sql)
        self.assertIn("DO $stage_counts$", sql)
        self.assertIn("IS DISTINCT FROM", sql)
        self.assertIn("STAGE_COUNTS_CONFLICT:%", sql)
        self.assertIn("INSERT INTO public.stage_counts", sql)
        self.assertNotIn("UPDATE public.stage_counts", sql)
        self.assertNotIn("DELETE", sql)
        self.assertTrue(sql.rstrip().endswith("SELECT json_build_object('inserted', :'v_inserted'::integer);"))

    def test_repeat_and_partial_insert_counts_are_returned(self) -> None:
        for inserted in (0, 1):
            with self.subTest(inserted=inserted):
                self.assertEqual(self.write(FakeHost(stdout=json.dumps({"inserted": inserted}))), inserted)

    def test_conflict_names_court_and_requires_new_cut(self) -> None:
        court = self.record.courts[1]
        host = FakeHost(code=3, stderr=f"ERROR: STAGE_COUNTS_CONFLICT:{court}\n")
        result = self.write(host)
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertEqual(result.problem, f"stage counts for {court} differ from the recorded ones")
        self.assertIn("recorded once", result.fix)
        self.assertIn(report.command("corpus cut"), result.fix)
        self.assertIn(report.command("corpus install"), result.fix)
        self.assertNotIn("delete", result.fix.lower())
        self.assertNotIn("remove", result.fix.lower())

    def test_database_failure_refuses_even_with_valid_stdout(self) -> None:
        host = FakeHost(code=3, stdout='{"inserted": 0}', stderr="foreign key violation")
        result = self.write(host)
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("exit 3", result.problem)
        self.assertEqual(result.fix, database_fix(RENDERED, "corpus install"))

    def test_invalid_write_outcome_refuses(self) -> None:
        for value in ("", "not json", "null", "[]", "{}", '{"inserted": true}',
                      '{"inserted": -1}', '{"inserted": "1"}', '{"inserted": 1.5}',
                      json.dumps({"inserted": len(self.record.courts) + 1}),
                      '{"inserted": 0, "extra": 1}'):
            with self.subTest(value=value):
                result = self.write(FakeHost(stdout=value))
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("invalid outcome", result.problem)
                self.assertEqual(result.fix, database_fix(RENDERED, "corpus install"))

    def test_unavailable_tool_refuses_with_restore_fix(self) -> None:
        for host in (FakeHost(code=127), FakeHost(error=FileNotFoundError("docker")),
                     FakeHost(error=subprocess.TimeoutExpired("docker", 1))):
            with self.subTest(code=host.code, error=host.error):
                result = self.write(host)
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("Restore Docker Compose and psql", result.fix)
                self.assertIn("corpus install", result.fix)
                self.assertEqual(len(host.calls), 1)


class StageCountsReader(unittest.TestCase):
    """Recorded court counts are grouped by source and malformed rows refuse."""

    def setUp(self) -> None:
        self.row = {
            "label": "corpus-2099-01-03", "source": "example", "court": "ca6",
            "dockets": 0, "opinion_clusters": 2, "citations": 3, "opinions": 4,
            "staged_at": "2099-01-03T04:05:06+00:00",
        }
        self.directory = "/production-rendered"

    def read(self, host: FakeHost) -> dict[str, dict[str, StageCounts]] | Problem:
        return read_stage_counts(
            host, self.directory, str(self.row["label"]), command_path="index build",
        )

    def test_rows_are_grouped_and_read_from_the_named_directory(self) -> None:
        rows = [self.row, {**self.row, "court": "ca9", "opinions": 2**32},
                {**self.row, "source": "other-example"}]
        host = FakeHost(stdout=json.dumps(rows) + "\n")
        self.assertEqual(self.read(host), {
            "example": {
                "ca6": StageCounts(0, 2, 3, 4, str(self.row["staged_at"])),
                "ca9": StageCounts(0, 2, 3, 2**32, str(self.row["staged_at"])),
            },
            "other-example": {"ca6": StageCounts(0, 2, 3, 4, str(self.row["staged_at"]))},
        })
        self.assertEqual(len(host.calls), 1)
        argv, sql = host.calls[0]
        self.assertEqual(argv, psql_argv(self.directory))
        self.assertNotIn(str(self.row["label"]), " ".join(argv))
        assert sql is not None
        self.assertIn(worker.bind("v_label", str(self.row["label"])), sql)
        self.assertIn("FROM public.stage_counts s WHERE label = :'v_label'", sql)
        self.assertIn("ORDER BY source, court", sql)

    def test_absent_label_is_an_empty_mapping(self) -> None:
        for stdout in ("", "\n", "null\n", "[]\n"):
            with self.subTest(stdout=stdout):
                self.assertEqual(self.read(FakeHost(stdout=stdout)), {})

    def test_malformed_rows_refuse_with_database_fix(self) -> None:
        invalid = ["not json", "{}", "0", '[null]', json.dumps([self.row, self.row])]
        for field in self.row:
            invalid.append(json.dumps([{key: value for key, value in self.row.items() if key != field}]))
        for field in ("dockets", "opinion_clusters", "citations", "opinions"):
            invalid.extend(json.dumps([{**self.row, field: value}]) for value in (-1, True, 1.5, "2", None))
        for field, values in (
            ("label", ("corpus-2099-01-04", None)),
            ("source", ("", "Example!", None)),
            ("court", ("", "CA6", "x" * 33, None)),
            ("staged_at", ("2099-01-03T04:05:06", "not a timestamp", None)),
        ):
            invalid.extend(json.dumps([{**self.row, field: value}]) for value in values)
        invalid.append(json.dumps([{**self.row, "extra": 1}]))
        for stdout in invalid:
            with self.subTest(stdout=stdout):
                result = self.read(FakeHost(stdout=stdout))
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("invalid", result.problem)
                self.assertEqual(result.fix, database_fix(self.directory, "index build"))

    def test_failed_read_refuses_even_with_valid_stdout(self) -> None:
        result = self.read(FakeHost(code=4, stdout=json.dumps([self.row])))
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("exit 4", result.problem)
        self.assertEqual(result.fix, database_fix(self.directory, "index build"))

    def test_tool_failure_refuses_without_retry(self) -> None:
        for host in (FakeHost(code=127), FakeHost(error=OSError("docker unavailable"))):
            with self.subTest(code=host.code, error=host.error):
                result = self.read(host)
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("Restore Docker Compose and psql", result.fix)
                self.assertIn("index build", result.fix)
                self.assertEqual(len(host.calls), 1)


class Reader(unittest.TestCase):
    """The reader returns a row and ordered source bindings or a refusal."""

    def test_row_missing_and_parsed(self) -> None:
        label = "corpus-2099-01-03"
        missing = FakeHost()
        self.assertIsNone(read_cut(missing, RENDERED, label, command_path="corpus cut"))
        self.assertEqual(missing.calls[0][0], psql_argv(RENDERED))
        self.assertIn("\\set v_label", missing.calls[0][1] or "")
        self.assertNotIn(label, " ".join(missing.calls[0][0]))

        row = {
            "label": label,
            "schema": 1,
            "pipeline": "0.0.0",
            "cut_at": "2099-01-03T04:05:06+00:00",
            "reason": "tranche",
            "base": None,
            "installed_at": None,
            "state": "cut",
            "sources": [{"source": "example", "snapshot_date": "2099-01-02"}],
        }
        host = FakeHost(stdout=json.dumps(row) + "\n")
        result = read_cut(host, RENDERED, label, command_path="corpus cut")
        self.assertIsInstance(result, CutRow)
        assert isinstance(result, CutRow)
        self.assertEqual(result.label, label)
        self.assertEqual([(item.source, item.snapshot_date) for item in result.sources],
                         [("example", "2099-01-02")])

    def test_invalid_row_and_failed_command_refuse(self) -> None:
        for host in (FakeHost(stdout="not json\n"), FakeHost(code=127, stderr="psql unavailable")):
            with self.subTest(code=host.code):
                result = read_cut(host, RENDERED, "corpus-2099-01-03",
                                  command_path="corpus cut")
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("corpus cut", result.fix)

    def test_all_rows_are_read_in_label_order(self) -> None:
        first = {
            "label": "corpus-2099-01-03", "schema": 1, "pipeline": "0.0.0",
            "cut_at": "2099-01-03T04:05:06+00:00", "reason": "tranche",
            "base": None, "installed_at": None, "state": "installing",
            "sources": [{"source": "example", "snapshot_date": "2099-01-02"}],
        }
        second = {**first, "label": "corpus-2099-01-04", "state": "installed"}
        host = FakeHost(stdout=json.dumps(first) + "\n" + json.dumps(second) + "\n")
        result = read_lockfiles(host, RENDERED, command_path="corpus install")
        self.assertIsInstance(result, tuple)
        assert isinstance(result, tuple)
        self.assertEqual(tuple(row.label for row in result), (first["label"], second["label"]))
        self.assertEqual(tuple(row.state for row in result), ("installing", "installed"))
        self.assertEqual(tuple(row.sources[0].source for row in result), ("example", "example"))
        argv, sql = host.calls[0]
        self.assertEqual(argv, psql_argv(RENDERED))
        self.assertNotIn("v_label", sql or "")
        self.assertIn("ORDER BY c.label", sql or "")

    def test_all_rows_empty_and_invalid_line(self) -> None:
        self.assertEqual(read_lockfiles(FakeHost(), RENDERED, command_path="corpus install"), ())
        first = {
            "label": "corpus-2099-01-03", "schema": 1, "pipeline": "0.0.0",
            "cut_at": "2099-01-03T04:05:06+00:00", "reason": "tranche",
            "base": None, "installed_at": None, "state": "cut",
            "sources": [{"source": "example", "snapshot_date": "2099-01-02"}],
        }
        invalid = FakeHost(stdout=json.dumps(first) + "\nnot json\n")
        result = read_lockfiles(invalid, RENDERED, command_path="corpus install")
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("invalid", result.problem)
        self.assertIn("corpus install", result.fix)

def watch_row() -> dict[str, object]:
    """One visibly fictitious source after an unanswered run."""

    return {
        "source": "example",
        "newest_at": "2099-01-05T04:05:06+00:00",
        "newest_outcome": "unanswered",
        "newest_latest_label": None,
        "newest_effective_date": None,
        "newest_url": "https://example.test/listing",
        "newest_detail": "transport",
        "answered_at": "2099-01-03T04:05:06+00:00",
        "answered_outcome": "observed",
        "answered_latest_label": "2099-01-02",
        "answered_effective_date": None,
        "answered_url": "https://example.test/listing",
        "answered_detail": None,
        "first_seen": "2099-01-01T04:05:06+00:00",
        "open": True,
        "pinned_label": "corpus-2098-12-31",
        "pinned_date": "2098-12-30",
        "unanswered_since": "2099-01-04T04:05:06+00:00",
    }


class ObservationWriter(unittest.TestCase):
    def test_one_transaction_inserts_only_with_payload_on_stdin(self) -> None:
        host = FakeHost()
        observation = Observation(
            "example", "2099-01-05T04:05:06+00:00", "unanswered",
            None, None, "https://example.test/listing", "transport",
        )
        self.assertIsNone(write_observations(host, RENDERED, (observation,)))
        self.assertEqual(len(host.calls), 1)
        argv, sql = host.calls[0]
        self.assertEqual(argv, psql_argv(RENDERED))
        self.assertNotIn(observation.source, " ".join(argv))
        self.assertNotIn(observation.url, " ".join(argv))
        assert sql is not None
        self.assertIn("\\set v_payload '", sql)
        self.assertIn(observation.observed_at, sql)
        self.assertIn(observation.url, sql)
        self.assertIn("BEGIN;", sql)
        self.assertEqual(sql.count("INSERT INTO public.upstream_observations"), 1)
        self.assertNotIn("UPDATE public.upstream_observations", sql)
        self.assertNotIn("DELETE FROM public.upstream_observations", sql)
        self.assertTrue(sql.endswith("COMMIT;\n"))

    def test_failed_write_names_exit_and_calling_command(self) -> None:
        host = FakeHost(code=3)
        result = write_observations(host, RENDERED, (), command_path="corpus watch")
        assert isinstance(result, Problem)
        self.assertIn("exit 3", result.problem)
        self.assertIn("corpus watch", result.fix)


class ObservationReader(unittest.TestCase):
    def test_state_keeps_newest_and_last_answer_separate(self) -> None:
        host = FakeHost(stdout=json.dumps(watch_row()) + "\n")
        result = read_watch_state(host, RENDERED)
        self.assertIsInstance(result, tuple)
        assert isinstance(result, tuple)
        self.assertEqual(len(result), 1)
        state = result[0]
        self.assertIsInstance(state, WatchState)
        self.assertEqual(state.newest.outcome, "unanswered")
        self.assertEqual(state.newest.detail, "transport")
        assert state.newest_answered is not None
        self.assertEqual(state.newest_answered.latest_label, "2099-01-02")
        self.assertEqual(state.first_seen, "2099-01-01T04:05:06+00:00")
        self.assertTrue(state.open)
        self.assertEqual((state.pinned_label, state.pinned_date),
                         ("corpus-2098-12-31", "2098-12-30"))
        self.assertEqual(state.unanswered_since, "2099-01-04T04:05:06+00:00")
        argv, sql = host.calls[0]
        self.assertEqual(argv, psql_argv(RENDERED))
        assert sql is not None
        self.assertIn(WATCH_STATE_SQL, sql)
        self.assertEqual(sql.count("row_to_json(state)"), 1)
        self.assertNotIn("example", " ".join(argv))
        self.assertIn("SELECT DISTINCT ON (source)", WATCH_STATE_SQL)
        self.assertIn("WHERE outcome = 'observed'", WATCH_STATE_SQL)
        self.assertIn("seen.latest_label = a.latest_label", WATCH_STATE_SQL)
        self.assertIn("FROM public.lockfile_sources AS b", WATCH_STATE_SQL)
        self.assertIn("u.observed_at > a.observed_at", WATCH_STATE_SQL)

    def test_unanswered_only_source_and_invalid_rows(self) -> None:
        row = watch_row()
        for field in ("answered_at", "answered_outcome", "answered_latest_label",
                      "answered_effective_date", "answered_url", "answered_detail"):
            row[field] = None
        row.update(first_seen=None, open=False, pinned_label=None, pinned_date=None)
        result = read_watch_state(FakeHost(stdout=json.dumps(row) + "\n"), RENDERED)
        assert isinstance(result, tuple)
        self.assertIsNone(result[0].newest_answered)
        self.assertEqual(result[0].unanswered_since, row["unanswered_since"])

        bad = dict(row, open=True)
        malformed = dict(row, newest_outcome=["unanswered"])
        for stdout in ("not json\n", json.dumps(bad) + "\n", json.dumps(malformed) + "\n"):
            with self.subTest(stdout=stdout):
                refusal = read_watch_state(FakeHost(stdout=stdout), RENDERED)
                assert isinstance(refusal, Problem)
                self.assertIn("row is invalid", refusal.problem)
                self.assertIn("corpus watch", refusal.fix)

    def test_failed_read_and_unavailable_tool_refuse(self) -> None:
        for code in (3, 127):
            with self.subTest(code=code):
                result = read_watch_state(FakeHost(code=code), RENDERED)
                assert isinstance(result, Problem)
                self.assertIn(f"exit {code}", result.problem)
                self.assertIn("corpus watch", result.fix)


COPY_SOURCE = "/fictitious-production-record"
COPY_TARGET = "/fictitious-sibling-record"


def copy_rows() -> list[dict[str, object]]:
    """Return fictitious source records with a full snapshot and a derived label."""
    loaded = load_lockfile(FIXTURE, known_sources=KNOWN)
    assert loaded.lockfile is not None
    lockfile = loaded.lockfile
    pin = lockfile.sources["example"]
    base: dict[str, object] = {
        "label": lockfile.label, "schema": lockfile.schema, "pipeline": lockfile.pipeline,
        "cut_at": lockfile.cut_at, "reason": lockfile.reason, "base": None,
        "installed_at": None, "state": "cut",
        "sources": [{
            "source": "example", "snapshot_date": pin.snapshot_date,
            "base_url": pin.base_url, "mirror_url": pin.mirror_url,
            "sidecar_sha256": pin.sidecar_sha256,
            "fetched_at": lockfile.cut_at, "verified_at": lockfile.cut_at,
        }],
    }
    return [base, {
        **base, "label": "corpus-2099-01-04", "base": base["label"],
        "cut_at": "2099-01-04T04:05:06+00:00",
    }]


class CopyHost(FakeHost):
    """Serve source rows and retain sibling rows across copy attempts."""

    def __init__(self) -> None:
        super().__init__()
        self.rows = copy_rows()
        self.target_rows: dict[str, dict[str, object]] = {}
        self.read_code = 0
        self.write_code = 0
        self.write_error = ""

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, cwd, env, timeout, passthrough
        command = list(argv)
        self.calls.append((command, input))
        assert input is not None
        if command == psql_argv(COPY_SOURCE):
            return subprocess.CompletedProcess(
                command, self.read_code, "\n".join(json.dumps(row) for row in self.rows), "",
            )
        assert command == psql_argv(COPY_TARGET)
        if self.write_code:
            return subprocess.CompletedProcess(command, self.write_code, "", self.write_error)
        payload = json.loads(input.split("\\set v_payload '", 1)[1].split("'", 1)[0])
        label = payload["label"]
        old = self.target_rows.get(label)
        if old is not None:
            comparisons = re.findall(r"AND [^\n]*payload->>'([a-z_]+)'", input)
            if any(old[field] != payload[field] for field in comparisons):
                return subprocess.CompletedProcess(command, 3, "", f"CORPUS_LOCKFILE_CONFLICT:{label}")
        elif payload["base"] is not None and payload["base"] not in self.target_rows:
            return subprocess.CompletedProcess(command, 3, "", "Base is not recorded")
        else:
            self.target_rows[label] = payload
        return subprocess.CompletedProcess(command, 0, "", "")


class LockfileCopy(unittest.TestCase):
    command_path = "index build caselaw --sample"
    conflict_fix = "Remove the divergent sibling record and rebuild the sample."

    def test_reads_and_writes_are_stdin_only_and_copy_the_base_before_its_child(self) -> None:
        host = CopyHost()
        label = str(host.rows[-1]["label"])

        self.assertIsNone(copy_lockfile(
            host, COPY_SOURCE, COPY_TARGET, label, self.command_path,
            conflict_fix=self.conflict_fix,
        ))

        self.assertEqual(len(host.calls), 1 + len(host.rows))
        argv, sql = host.calls[0]
        self.assertEqual(argv, psql_argv(COPY_SOURCE))
        assert sql is not None
        self.assertIn(worker.bind("v_label", label), sql)
        self.assertIn("WITH RECURSIVE", sql)
        self.assertIn("ORDER BY chain.depth DESC", sql)
        payloads: list[object] = []
        for argv, sql in host.calls[1:]:
            self.assertEqual(argv, psql_argv(COPY_TARGET))
            assert sql is not None
            payloads.append(json.loads(sql.split("\\set v_payload '", 1)[1].split("'", 1)[0]))
            self.assertIn("BEGIN;", sql)
            self.assertTrue(sql.endswith("COMMIT;\n"))
        self.assertEqual(payloads, host.rows)
        self.assertEqual(list(host.target_rows), [row["label"] for row in host.rows])
        for argv, _ in host.calls:
            for row in host.rows:
                self.assertNotIn(str(row["label"]), " ".join(argv))
                self.assertNotIn(str(row["cut_at"]), " ".join(argv))
                sources = row["sources"]
                assert isinstance(sources, list)
                for pin in sources:
                    self.assertNotIn(pin["sidecar_sha256"], " ".join(argv))
                    self.assertNotIn(pin["base_url"], " ".join(argv))

    def test_absent_label_refuses_before_writes_with_a_corpus_install_fix(self) -> None:
        host = CopyHost()
        label = str(host.rows[-1]["label"])
        host.rows = []

        result = copy_lockfile(host, COPY_SOURCE, COPY_TARGET, label, self.command_path, conflict_fix=self.conflict_fix)

        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn(label, result.problem)
        self.assertIn(f"{report.command('corpus install')} {label}", result.fix)
        self.assertIn(report.command(self.command_path), result.fix)
        self.assertEqual(len(host.calls), 1)
        self.assertEqual(host.target_rows, {})

    def test_both_conflicts_keep_the_callers_recovery_fix(self) -> None:
        for marker in ("CORPUS_LOCKFILE_CONFLICT:corpus-2099-01-03", "CORPUS_SNAPSHOT_CONFLICT:example:2099-01-02"):
            with self.subTest(marker=marker):
                host = CopyHost()
                host.write_code, host.write_error = 3, marker
                label = str(host.rows[-1]["label"])

                result = copy_lockfile(host, COPY_SOURCE, COPY_TARGET, label, self.command_path, conflict_fix=self.conflict_fix)

                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("differs", result.problem)
                self.assertEqual(result.fix, self.conflict_fix)
                self.assertEqual(len(host.calls), 2)
                self.assertEqual(host.target_rows, {})

    def test_recopy_ignores_moved_source_state_and_installation_time(self) -> None:
        host = CopyHost()
        label = str(host.rows[-1]["label"])
        self.assertIsNone(copy_lockfile(host, COPY_SOURCE, COPY_TARGET, label, self.command_path, conflict_fix=self.conflict_fix))
        original = json.loads(json.dumps(host.target_rows))
        host.rows = [{
            **row, "state": "superseded", "installed_at": "2099-01-05T04:05:06+00:00",
        } for row in host.rows]
        host.calls.clear()

        self.assertIsNone(copy_lockfile(host, COPY_SOURCE, COPY_TARGET, label, self.command_path, conflict_fix=self.conflict_fix))

        self.assertEqual(host.target_rows, original)
        for _, sql in host.calls[1:]:
            assert sql is not None
            payload = json.loads(sql.split("\\set v_payload '", 1)[1].split("'", 1)[0])
            self.assertEqual(payload["state"], "superseded")
            self.assertEqual(payload["installed_at"], "2099-01-05T04:05:06+00:00")
            self.assertNotIn("UPDATE public.corpus_lockfiles", sql)
        self.assertEqual(len(host.calls), 1 + len(host.rows))

    def test_recopy_still_refuses_an_immutable_cut_change(self) -> None:
        for change in ({"pipeline": "1000.0.0"}, {"cut_at": "2099-01-06T04:05:06+00:00"}):
            with self.subTest(change=change):
                host = CopyHost()
                label = str(host.rows[-1]["label"])
                self.assertIsNone(copy_lockfile(host, COPY_SOURCE, COPY_TARGET, label, self.command_path, conflict_fix=self.conflict_fix))
                original = json.loads(json.dumps(host.target_rows))
                host.rows[0].update(change)
                host.calls.clear()

                result = copy_lockfile(host, COPY_SOURCE, COPY_TARGET, label, self.command_path, conflict_fix=self.conflict_fix)

                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("differs", result.problem)
                self.assertEqual(result.fix, self.conflict_fix)
                self.assertEqual(host.target_rows, original)
                self.assertEqual(len(host.calls), 2)

    def test_missing_or_malformed_base_chains_refuse_before_any_write(self) -> None:
        for variant in ("child-only", "reversed", "duplicate", "invalid-json-shape"):
            with self.subTest(variant=variant):
                host = CopyHost()
                label = str(host.rows[-1]["label"])
                if variant == "child-only":
                    host.rows = host.rows[1:]
                elif variant == "reversed":
                    host.rows.reverse()
                elif variant == "duplicate":
                    host.rows.append(host.rows[-1])
                else:
                    host.rows[0] = {"label": label}

                result = copy_lockfile(host, COPY_SOURCE, COPY_TARGET, label, self.command_path, conflict_fix=self.conflict_fix)

                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertEqual(result.problem, "corpus record row is invalid")
                self.assertIn(stack.logs_fix(COPY_SOURCE, "postgres"), result.fix)
                self.assertEqual(len(host.calls), 1)
                self.assertEqual(host.target_rows, {})

    def test_failed_read_refuses_without_copying_or_retrying(self) -> None:
        host = CopyHost()
        host.read_code = 3
        label = str(host.rows[-1]["label"])

        result = copy_lockfile(host, COPY_SOURCE, COPY_TARGET, label, self.command_path, conflict_fix=self.conflict_fix)

        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("exit 3", result.problem)
        self.assertIn(stack.logs_fix(COPY_SOURCE, "postgres"), result.fix)
        self.assertIn(report.command(self.command_path), result.fix)
        self.assertEqual(len(host.calls), 1)
        self.assertEqual(host.target_rows, {})
