"""The evaluation recorder: one transaction, bound values, and host constants."""

import os
import subprocess
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime

import gideon
from gideon.evaluation import record
from gideon.host import audit, stores
from gideon.host.stack import exec_argv
from gideon.host.sysio import Command, PathLike

RENDERED = "/etc/gideon/rendered"
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
PSQL = tuple(
    exec_argv(
        RENDERED,
        "postgres",
        "psql",
        "-U",
        "gideon_eval",
        "-d",
        "gideon",
        "-v",
        "ON_ERROR_STOP=1",
        "--single-transaction",
        "-f",
        "-",
    )
)


class FakeHost:
    def __init__(self, rc: int = 0) -> None:
        self.rc = rc
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

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
        self.calls.append((tuple(argv), input))
        return subprocess.CompletedProcess(list(argv), self.rc, "", "diagnostic with row values")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        raise NotImplementedError

    def write_text(self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644) -> None:
        raise NotImplementedError

    def exists(self, path: PathLike) -> bool:
        raise NotImplementedError

    def listdir(self, path: PathLike) -> list[str]:
        raise NotImplementedError

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        raise NotImplementedError

    def stat(self, path: PathLike) -> os.stat_result:
        raise NotImplementedError

    def chmod(self, path: PathLike, mode: int) -> None:
        raise NotImplementedError

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        raise NotImplementedError

    def mkdir(self, path: PathLike, *, mode: int = 0o755, parents: bool = False, exist_ok: bool = False) -> None:
        raise NotImplementedError

    def geteuid(self) -> int:
        return 0


def run_row(**overrides: object) -> record.RunRow:
    values: dict[str, object] = {
        "run_id": "11111111-2222-4333-8444-555555555555",
        "started_at": NOW,
        "finished_at": datetime(2026, 9, 19, 12, 0, 1, tzinfo=UTC),
        "product_version": gideon.__version__,
        "corpus_lockfile": None,
        "eval_set_version": "eval-v1",
        "hardware_profile": "fixture-profile",
        "stack": "production",
        "generation_id": None,
        "kind": "manual",
        "slice": "extraction",
        "overrides": {"z": 2, "a": 1},
        "repeats": 1,
        "git_sha": "a" * 40,
        "git_dirty": False,
        "set_digest": "b" * 64,
        "verdict": "pass",
    }
    values.update(overrides)
    return record.RunRow(**values)  # type: ignore[arg-type]


def result_row(case_id: str, **overrides: object) -> record.ResultRow:
    values: dict[str, object] = {
        "run_id": "11111111-2222-4333-8444-555555555555",
        "run_started_at": NOW,
        "case_id": case_id,
        "repeat": 1,
        "verdict": "pass",
        "metrics": {"zeta": {"misses": 0}, "alpha": {"hits": 1}},
        "judge": None,
        "provenance_ref": None,
        "latency_ms": 1.25,
    }
    values.update(overrides)
    return record.ResultRow(**values)  # type: ignore[arg-type]


class Constants(unittest.TestCase):
    def test_writer_constants_follow_the_host_contract(self) -> None:
        self.assertEqual(record.EVAL_ROLE, "gideon_eval")
        self.assertEqual(record.EVAL_ROLE, next(spec.name for spec in stores.ROLE_SPECS if spec.name == record.EVAL_ROLE))
        self.assertEqual(record.EVAL_DATABASE, stores._SCHEMA_DATABASE)
        self.assertEqual(record.EVAL_DATABASE, audit._AUDIT_DATABASE)
        self.assertEqual(record.POSTGRES_SERVICE, stores._POSTGRES_SERVICE)
        self.assertEqual(record.POSTGRES_SERVICE, audit._POSTGRES_SERVICE)


class WriteRows(unittest.TestCase):
    def test_batch_binds_values_and_starts_with_both_partition_calls(self) -> None:
        host = FakeHost()
        self.assertIsNone(record.write_rows(host, RENDERED, run_row(), (result_row("case-z"), result_row("case-a"))))
        self.assertEqual(len(host.calls), 1)
        argv, sql = host.calls[0]
        self.assertEqual(argv, PSQL)
        assert sql is not None
        self.assertTrue(sql.startswith("\\set run_started_at '2026-09-19T12:00:00+00:00'\n"))
        first = sql.index("SELECT eval_runs_ensure_partition")
        second = sql.index("SELECT eval_results_ensure_partition")
        insert = sql.index("INSERT INTO eval_runs")
        result_a = sql.index("\\set result_0_case_id 'case-a'")
        result_z = sql.index("\\set result_1_case_id 'case-z'")
        self.assertLess(first, second)
        self.assertLess(second, insert)
        self.assertLess(result_a, result_z)
        self.assertIn("\\set overrides '{\"a\": 1, \"z\": 2}'", sql)
        self.assertIn("\\set result_0_metrics '{\"alpha\": {\"hits\": 1}, \"zeta\": {\"misses\": 0}}'", sql)
        self.assertIn("::jsonb", sql)
        self.assertEqual(sql.count("INSERT INTO eval_results"), 2)
        for value in ("11111111-2222-4333-8444-555555555555", "case-a", "case-z"):
            self.assertNotIn(value, " ".join(argv))
        self.assertIn("-v", argv)
        self.assertIn("ON_ERROR_STOP=1", argv)
        self.assertIn("--single-transaction", argv)

    def test_line_breaks_and_nul_are_refused_before_host_io(self) -> None:
        for value in ("bad\nvalue", "bad\rvalue", "bad\x00value"):
            with self.subTest(value=repr(value)):
                host = FakeHost()
                problem = record.write_rows(host, RENDERED, run_row(run_id=value), ())
                self.assertIn("CR, LF, or NUL", problem or "")
                self.assertEqual(host.calls, [])

    def test_failed_write_reports_only_the_exit_status(self) -> None:
        problem = record.write_rows(FakeHost(rc=1), RENDERED, run_row(), ())
        self.assertEqual(problem, "eval writer failed: exit 1")
        self.assertNotIn("diagnostic with row values", problem or "")

    def test_probe_uses_the_evaluation_role_and_selects_one(self) -> None:
        host = FakeHost()
        self.assertIsNone(record.probe(host, RENDERED))
        self.assertEqual(host.calls[0], (PSQL, "SELECT 1;\n"))


if __name__ == "__main__":
    unittest.main()
