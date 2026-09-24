"""The append-only audit writer: ids only, SQL on stdin, values bound through psql variables."""

import os
import subprocess
import unittest
from collections.abc import Mapping

import gideon
from gideon.host.audit import AuditRow, probe, write_rows
from gideon.host.stack import exec_argv
from gideon.host.sysio import Command, PathLike

RENDERED = "/etc/gideon/rendered"
PSQL = tuple(exec_argv(RENDERED, "postgres", "psql", "-U", "gideon_audit", "-d", "gideon", "-v", "ON_ERROR_STOP=1", "--single-transaction", "-f", "-"))


class FakeHost:
    def __init__(self, rc: int = 0) -> None:
        self.rc = rc
        self.calls: list[tuple[tuple[str, ...], str | None, Mapping[str, str] | None]] = []

    def run(self, argv: Command, *, check: bool = False, input: str | None = None, cwd: PathLike | None = None, env: Mapping[str, str] | None = None, timeout: float | None = None, passthrough: bool = False) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(argv), input, env))
        return subprocess.CompletedProcess(list(argv), self.rc, "", "ERROR: near 'secret-looking-value'" if self.rc else "")

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


def row(**overrides: object) -> AuditRow:
    base: dict[str, object] = {
        "run_id": "11111111-2222-4333-8444-555555555555",
        "kind": "reconcile.role.intent",
        "actor_user_id": "u-admin",
        "user_id": "u-42",
        "chat_id": None,
        "kb_ids": (),
        "detail": {"from": "user", "to": "admin", "reason": "in GIDEON-Admins"},
    }
    base.update(overrides)
    return AuditRow(**base)  # type: ignore[arg-type]


class WriteRows(unittest.TestCase):
    def test_sql_travels_on_stdin_as_the_audit_role_with_bound_variables(self) -> None:
        host = FakeHost()
        self.assertIsNone(write_rows(host, RENDERED, [row(), row(kind="membership_snapshot", user_id=None, kb_ids=("kb-1", 'kb "2"'))]))
        self.assertEqual(len(host.calls), 1)
        argv, sql, env = host.calls[0]
        self.assertEqual(argv, PSQL)
        self.assertIsNone(env)
        assert sql is not None
        self.assertTrue(sql.startswith("SELECT audit_log_ensure_partition(now());\n"))
        self.assertIn("\\set v_kind 'reconcile.role.intent'", sql)
        self.assertIn("\\set v_user 'u-42'", sql)
        self.assertIn(f"\\set v_release '{gideon.__version__}'", sql)
        self.assertIn('\\set v_detail \'{"from": "user", "reason": "in GIDEON-Admins", "to": "admin"}\'', sql)
        self.assertIn("VALUES (:'v_run_id'::uuid, :'v_kind', :'v_actor', :'v_user', NULL, :'v_kb'::text[], :'v_release', :'v_detail'::jsonb);", sql)
        self.assertIn("VALUES (:'v_run_id'::uuid, :'v_kind', :'v_actor', NULL, NULL,", sql)
        self.assertIn('\\set v_kb \'{"kb-1","kb \\"2\\""}\'', sql)
        self.assertEqual(sql.count("INSERT INTO audit_log"), 2)
        for value in ("u-42", "u-admin", "kb-1"):
            self.assertNotIn(value, " ".join(argv))

    def test_single_quotes_are_doubled_and_line_breaks_refuse(self) -> None:
        host = FakeHost()
        self.assertIsNone(write_rows(host, RENDERED, [row(detail={"reason": "it's fine"})]))
        assert host.calls[0][1] is not None
        self.assertIn("it''s fine", host.calls[0][1])
        problem = write_rows(host, RENDERED, [row(user_id="u\n1")])
        self.assertIsNotNone(problem)
        self.assertIn("CR, LF, or NUL", problem or "")
        self.assertEqual(len(host.calls), 1)

    def test_failure_reports_the_exit_status_never_the_diagnostic(self) -> None:
        problem = write_rows(FakeHost(rc=1), RENDERED, [row()])
        self.assertEqual(problem, "audit writer failed: exit 1")
        self.assertNotIn("secret-looking-value", problem or "")

    def test_probe_runs_a_select_as_the_audit_role(self) -> None:
        host = FakeHost()
        self.assertIsNone(probe(host, RENDERED))
        self.assertEqual(host.calls[0][:2], (PSQL, "SELECT 1;\n"))
        self.assertIsNotNone(probe(FakeHost(rc=2), RENDERED))
