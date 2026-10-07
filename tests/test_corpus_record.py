"""Corpus lockfile and upstream observation statements over a recording host."""

import json
import re
import subprocess
import unittest
from collections.abc import Mapping
from pathlib import Path

from gideon.host.corpus.lockfile import load_lockfile, render_errors
from gideon.host.corpus.record import (
    WATCH_STATE_SQL,
    CutRow,
    Observation,
    WatchState,
    read_cut,
    read_lockfiles,
    read_watch_state,
    write_cut,
    write_install,
    write_observations,
)
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
