"""Corpus cut database statements over a recording host."""

import json
import subprocess
import unittest
from collections.abc import Mapping
from pathlib import Path

from gideon.host.corpus.lockfile import load_lockfile, render_errors
from gideon.host.corpus.record import CutRow, read_cut, write_cut
from gideon.host.report import Problem
from gideon.host.sysio import Command, PathLike, RealHost
from gideon.host.worker import psql_argv

FIXTURE = Path(__file__).parent / "fixtures/corpus/corpus-2099-01-03.yaml"
RENDERED = "/rendered"


class FakeHost(RealHost):
    """Return one psql answer and record its argv and stdin."""

    def __init__(self, *, code: int = 0, stdout: str = "", stderr: str = "") -> None:
        super().__init__()
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
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
        return subprocess.CompletedProcess(command, self.code, self.stdout, self.stderr)


class Writer(unittest.TestCase):
    """The writer binds data and refuses a conflicting existing record."""

    def setUp(self) -> None:
        loaded = load_lockfile(FIXTURE, known_sources={"example": True})
        self.assertTrue(loaded.ok, render_errors(loaded.errors))
        assert loaded.lockfile is not None
        self.lockfile = loaded.lockfile

    def test_one_transaction_has_every_value_on_stdin(self) -> None:
        host = FakeHost()
        result = write_cut(
            host, RENDERED, self.lockfile,
            {"example": self.lockfile.cut_at}, self.lockfile.cut_at,
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

    def test_missing_fetch_time_refuses_before_database(self) -> None:
        host = FakeHost()
        result = write_cut(host, RENDERED, self.lockfile, {}, self.lockfile.cut_at)
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("example", result.problem)
        self.assertIn("corpus cut", result.fix)
        self.assertEqual(host.calls, [])

    def test_snapshot_conflict_names_snapshot_and_no_deletion(self) -> None:
        pin = self.lockfile.sources["example"]
        host = FakeHost(code=3, stderr=f"ERROR: CORPUS_SNAPSHOT_CONFLICT:example:{pin.snapshot_date}\n")
        result = write_cut(host, RENDERED, self.lockfile,
                           {"example": self.lockfile.cut_at}, self.lockfile.cut_at)
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn(f"example {pin.snapshot_date}", result.problem)
        self.assertNotIn("delete", result.fix.lower())
        self.assertIn("corpus cut", result.fix)

    def test_label_conflict_and_tool_failure_refuse(self) -> None:
        conflict = FakeHost(code=3, stderr=f"ERROR: CORPUS_LOCKFILE_CONFLICT:{self.lockfile.label}\n")
        result = write_cut(conflict, RENDERED, self.lockfile,
                           {"example": self.lockfile.cut_at}, self.lockfile.cut_at)
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn(self.lockfile.label, result.problem)
        self.assertNotIn("delete", result.fix.lower())

        unavailable = FakeHost(code=127, stderr="psql unavailable")
        result = write_cut(unavailable, RENDERED, self.lockfile,
                           {"example": self.lockfile.cut_at}, self.lockfile.cut_at)
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("exit 127", result.problem)
        self.assertIn("corpus cut", result.fix)


class Reader(unittest.TestCase):
    """The reader returns a row and ordered source bindings or a refusal."""

    def test_row_missing_and_parsed(self) -> None:
        label = "corpus-2099-01-03"
        missing = FakeHost()
        self.assertIsNone(read_cut(missing, RENDERED, label))
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
        result = read_cut(host, RENDERED, label)
        self.assertIsInstance(result, CutRow)
        assert isinstance(result, CutRow)
        self.assertEqual(result.label, label)
        self.assertEqual([(item.source, item.snapshot_date) for item in result.sources],
                         [("example", "2099-01-02")])

    def test_invalid_row_and_failed_command_refuse(self) -> None:
        for host in (FakeHost(stdout="not json\n"), FakeHost(code=127, stderr="psql unavailable")):
            with self.subTest(code=host.code):
                result = read_cut(host, RENDERED, "corpus-2099-01-03")
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("corpus cut", result.fix)
