"""The evaluation recorder: one transaction, bound values, and host constants."""

import json
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
READER_PSQL = tuple(
    exec_argv(
        RENDERED,
        record.POSTGRES_SERVICE,
        "psql",
        "-U",
        record.METRICS_ROLE,
        "-d",
        record.EVAL_DATABASE,
        "-v",
        "ON_ERROR_STOP=1",
        "-tA",
        "-f",
        "-",
    )
)


class FakeHost:
    def __init__(self, rc: int = 0, stdout: str = "") -> None:
        self.rc = rc
        self.stdout = stdout
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
        return subprocess.CompletedProcess(list(argv), self.rc, self.stdout, "diagnostic with row values")

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
        "eval_set_version": "eval-v-fictitious-test",
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


def read_psql_set(line: str, name: str) -> str:
    """Read one psql single-quoted ``\\set`` value without using production code."""

    prefix = f"\\set {name} '"
    assert line.startswith(prefix)
    encoded = line[len(prefix) : -1]
    value: list[str] = []
    index = 0
    while index < len(encoded):
        character = encoded[index]
        if character == "\\":
            if index + 1 >= len(encoded):
                raise AssertionError("unterminated psql escape")
            value.append(encoded[index + 1])
            index += 2
        elif character == "'" and index + 1 < len(encoded) and encoded[index + 1] == "'":
            value.append("'")
            index += 2
        else:
            value.append(character)
            index += 1
    return "".join(value)


class Constants(unittest.TestCase):
    def test_writer_constants_follow_the_host_contract(self) -> None:
        self.assertEqual(record.EVAL_ROLE, "gideon_eval")
        self.assertEqual(record.EVAL_ROLE, next(spec.name for spec in stores.ROLE_SPECS if spec.name == record.EVAL_ROLE))
        self.assertEqual(record.EVAL_DATABASE, stores._SCHEMA_DATABASE)
        self.assertEqual(record.EVAL_DATABASE, audit._AUDIT_DATABASE)
        self.assertEqual(record.POSTGRES_SERVICE, stores._POSTGRES_SERVICE)
        self.assertEqual(record.POSTGRES_SERVICE, audit._POSTGRES_SERVICE)

    def test_reader_role_follows_the_metrics_read_role(self) -> None:
        self.assertEqual(record.METRICS_ROLE, "gideon_ro_metrics")
        self.assertEqual(
            record.METRICS_ROLE,
            next(spec.name for spec in stores.ROLE_SPECS if spec.name == record.METRICS_ROLE),
        )


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
                host = FakeHost()
                problem = record.write_rows(
                    host,
                    RENDERED,
                    run_row(),
                    (result_row("case", judge={"reason": value}),),
                )
                self.assertIn("CR, LF, or NUL", problem or "")
                self.assertEqual(host.calls, [])

    def test_judge_json_binding_round_trips_through_psql_escaping(self) -> None:
        judge = {"answer": 'a "quote", a \\slash, an apostrophe\'s §'}
        host = FakeHost()
        self.assertIsNone(record.write_rows(host, RENDERED, run_row(), (result_row("case", judge=judge),)))
        sql = host.calls[0][1]
        assert sql is not None
        line = next(line for line in sql.splitlines() if line.startswith("\\set result_0_judge "))
        encoded = read_psql_set(line, "result_0_judge")
        expected = json.dumps(judge, sort_keys=True, ensure_ascii=True)
        self.assertEqual(encoded, expected)
        self.assertEqual(json.loads(encoded), judge)

    def test_judge_json_binding_sorts_keys(self) -> None:
        judge = {"zeta": "last", "alpha": "first"}
        host = FakeHost()
        self.assertIsNone(record.write_rows(host, RENDERED, run_row(), (result_row("case", judge=judge),)))
        sql = host.calls[0][1]
        assert sql is not None
        line = next(line for line in sql.splitlines() if line.startswith("\\set result_0_judge "))
        self.assertEqual(
            read_psql_set(line, "result_0_judge"),
            json.dumps(judge, sort_keys=True, ensure_ascii=True),
        )

    def test_result_without_latency_binds_sql_null(self) -> None:
        host = FakeHost()
        self.assertIsNone(
            record.write_rows(host, RENDERED, run_row(), (result_row("case", latency_ms=None),))
        )
        sql = host.calls[0][1]
        assert sql is not None
        result_insert = sql[sql.index("INSERT INTO eval_results") :]
        # latency_ms is the last column, so a row without one ends in NULL and
        # binds no latency variable of its own.
        self.assertTrue(result_insert.rstrip().endswith("NULL);"))
        self.assertNotIn("result_0_latency_ms", sql)

    def test_failed_write_reports_only_the_exit_status(self) -> None:
        problem = record.write_rows(FakeHost(rc=1), RENDERED, run_row(), ())
        self.assertEqual(problem, "eval writer failed: exit 1")
        self.assertNotIn("diagnostic with row values", problem or "")

    def test_probe_uses_the_evaluation_role_and_selects_one(self) -> None:
        host = FakeHost()
        self.assertIsNone(record.probe(host, RENDERED))
        self.assertEqual(host.calls[0], (PSQL, "SELECT 1;\n"))


class ReadRows(unittest.TestCase):
    def test_read_uses_the_reader_argv_and_binds_the_run_id(self) -> None:
        run_id = "11111111-2222-4333-8444-555555555555"
        document = {
            "run": {
                "run_id": run_id,
                "product_version": gideon.__version__,
                "corpus_lockfile": None,
                "eval_set_version": "eval-v-fictitious-test",
                "hardware_profile": "fixture-profile",
                "slice": "extraction",
                "overrides": {},
                "repeats": 1,
                "git_sha": "a" * 40,
                "git_dirty": False,
                "set_digest": "b" * 64,
                "verdict": "pass",
            },
            "results": [{"case_id": "extraction-001", "repeat": 1, "verdict": "pass"}],
        }
        host = FakeHost(stdout=json.dumps(document))

        loaded, problem = record.read_run(host, RENDERED, run_id)

        self.assertIsNone(problem)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.run_id, run_id)
        self.assertEqual(loaded.results, (("extraction-001", 1, "pass"),))
        argv, sql = host.calls[0]
        self.assertEqual(argv, READER_PSQL)
        assert sql is not None
        self.assertIn(f"\\set run_id '{run_id}'", sql)
        self.assertIn("WHERE run_id = :'run_id'::uuid", sql)
        self.assertEqual(sql.count(run_id), 1)
        self.assertNotIn("metrics", sql)
        self.assertNotIn("judge", sql)

    def test_non_uuid_is_refused_before_reader_io(self) -> None:
        host = FakeHost()
        loaded, problem = record.read_run(host, RENDERED, "not-a-uuid")
        self.assertIsNone(loaded)
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertIn("not a UUID", problem.problem)
        self.assertEqual(host.calls, [])


if __name__ == "__main__":
    unittest.main()
