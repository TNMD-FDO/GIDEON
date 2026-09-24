"""users reconcile: the membership truth table, the audit protocol, and the refusals (spec §4.1, §19.4)."""

import argparse
import contextlib
import io
import os
import subprocess
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gideon.host.ldap import ldapsearch_argv
from gideon.host.owui import Client, Response
from gideon.host.render.owui import BREAK_GLASS, EVAL_IDENTITY
from gideon.host.stack import exec_argv
from gideon.host.sysio import Command, PathLike
from gideon.host.users import run_reconcile

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config/site.example.yaml"
SITE = "/etc/gideon/site.yaml"
RENDERED = "/etc/gideon/rendered"
ADMIN_KEY = "sk-admin"
BASE = "DC=example,DC=org"
USERS_DN = f"CN=GIDEON-Users,CN=Users,{BASE}"
ADMINS_DN = f"CN=GIDEON-Admins,CN=Users,{BASE}"
AUDIT_PSQL = tuple(exec_argv(RENDERED, "postgres", "psql", "-U", "gideon_audit", "-d", "gideon", "-v", "ON_ERROR_STOP=1", "--single-transaction", "-f", "-"))


def search(group_dn: str) -> tuple[str, ...]:
    return tuple(ldapsearch_argv("example.org", 636, "svc-gideon-ldap", BASE, f"(&(objectClass=user)(memberOf={group_dn}))", ("sAMAccountName", "userPrincipalName")))


def ldif(*members: tuple[str, str | None]) -> str:
    records = []
    for account, upn in members:
        lines = [f"dn: CN={account},CN=Users,{BASE}", f"sAMAccountName: {account}"]
        if upn:
            lines.append(f"userPrincipalName: {upn}")
        records.append("\n".join(lines))
    return "\n\n".join(records) + "\n"


class FakeHost:
    def __init__(self, commands: Mapping[tuple[str, ...], subprocess.CompletedProcess[str]], files: Mapping[str, str], *, euid: int = 0, audit_rc: int = 0) -> None:
        self.commands = dict(commands)
        self.files = dict(files)
        self.euid = euid
        self.audit_rc = audit_rc
        self.audit_sql: list[str] = []
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Command, *, check: bool = False, input: str | None = None, cwd: PathLike | None = None, env: Mapping[str, str] | None = None, timeout: float | None = None, passthrough: bool = False) -> subprocess.CompletedProcess[str]:
        command = tuple(argv)
        self.calls.append(command)
        if command == AUDIT_PSQL:
            self.audit_sql.append(input or "")
            return subprocess.CompletedProcess(list(command), self.audit_rc, "", "")
        return self.commands.get(command, subprocess.CompletedProcess(list(command), 127, "", "not found"))

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        key = os.fspath(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644) -> None:
        raise NotImplementedError

    def exists(self, path: PathLike) -> bool:
        return os.fspath(path) in self.files

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
        return self.euid


class Frontend:
    def __init__(self, users: list[dict[str, Any]], knowledge: list[dict[str, Any]] | None = None) -> None:
        self.users = users
        self.knowledge = knowledge or []
        self.role_updates: list[tuple[str, str]] = []
        self.ready = True
        self.fail_update_for: str | None = None

    def handle(self, method: str, path: str, body: object | None, credential: str | None) -> Response:
        route = path.split("?")[0]
        if route == "/ready":
            return Response(200, {"status": self.ready})
        if credential != ADMIN_KEY:
            return Response(401, {"detail": "unauthorized"})
        if route == "/api/v1/users/all":
            return Response(200, {"users": self.users, "total": len(self.users)})
        if route.startswith("/api/v1/users/") and route.endswith("/update"):
            assert isinstance(body, Mapping)
            user_id = route.split("/")[4]
            if user_id == self.fail_update_for:
                return Response(500, {"detail": "boom"})
            user = next(user for user in self.users if user["id"] == user_id)
            user["role"] = body["role"]
            self.role_updates.append((user_id, body["role"]))
            return Response(200, user)
        if route == "/api/v1/knowledge/":
            page = int(path.partition("page=")[2] or 1)
            return Response(200, {"items": self.knowledge if page == 1 else [], "total": len(self.knowledge)})
        return Response(200, "<!doctype html>")


class FakeClient(Client):
    def __init__(self, frontend: Frontend, *, api_key: str | None = None, token: str | None = None) -> None:
        super().__init__("http://fake", api_key=api_key, token=token)
        self.frontend = frontend

    def request(self, method: str, path: str, body: object | None = None) -> Response:
        return self.frontend.handle(method, path, body, self._api_key if self._api_key is not None else self._token)


def user(user_id: str, email: str, role: str, *, name: str | None = None) -> dict[str, Any]:
    return {"id": user_id, "email": email, "name": name or user_id, "role": role}


def frontend() -> Frontend:
    return Frontend(
        [
            user("u-admin", BREAK_GLASS.email, "admin", name=BREAK_GLASS.username),
            user("u-eval", EVAL_IDENTITY.email, "user", name=EVAL_IDENTITY.username),
            user("u-alice", "alice@example.org", "user"),          # in admins → promote
            user("u-bob", "bob@example.org", "admin"),             # left admins → demote
            user("u-carol", "carol@example.org", "user"),          # left users → pending
            user("u-dave", "dave@example.org", "pending"),         # re-added → user
            user("u-erin", "Erin@EXAMPLE.ORG", "user"),            # steady, case-insensitive UPN join
            user("u-frank", "frank@example.org", "user"),          # no UPN → pending
        ],
        knowledge=[{"id": "kb-1", "name": "Matter", "user_id": "u-carol"}, {"id": "kb-2", "name": "Ok", "user_id": "u-erin"}, {"id": "kb-3", "name": "Gone", "user_id": "u-ghost"}, {"id": "kb-4", "name": "Restored", "user_id": "u-dave"}],
    )


def directory() -> dict[tuple[str, ...], subprocess.CompletedProcess[str]]:
    users_group = ldif(("alice", "alice@example.org"), ("bob", "bob@example.org"), ("dave", "dave@example.org"), ("erin", "erin@example.org"), ("frank", None), ("newbie", "newbie@example.org"))
    admins_group = ldif(("alice", "alice@example.org"), ("zed", "zed@example.org"))
    return {
        search(USERS_DN): subprocess.CompletedProcess(list(search(USERS_DN)), 0, users_group, ""),
        search(ADMINS_DN): subprocess.CompletedProcess(list(search(ADMINS_DN)), 0, admins_group, ""),
    }


def files(*, admin_key: bool = True) -> dict[str, str]:
    result = {SITE: EXAMPLE.read_text()}
    if admin_key:
        result["/etc/gideon/secrets/gideon_admin_api_key"] = ADMIN_KEY + "\n"
    return result


def reconcile(host: FakeHost, front: Frontend, *, now: bool) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()

    def factory(*, api_key: str | None = None, token: str | None = None) -> Client:
        return FakeClient(front, api_key=api_key, token=token)

    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = run_reconcile(argparse.Namespace(now=now), host=host, client_factory=factory, sleep=lambda _: None)
    return code, out.getvalue(), err.getvalue()


class Report(unittest.TestCase):
    def test_bare_run_reports_with_would_and_changes_nothing(self) -> None:
        host = FakeHost(directory(), files())
        front = frontend()
        code, out, err = reconcile(host, front, now=False)
        self.assertEqual((code, err), (0, ""), out)
        lines = out.splitlines()
        self.assertTrue(all(line.startswith("would ") for line in lines), lines)
        self.assertIn("would u-alice: user → admin (in GIDEON-Admins)", lines)
        self.assertIn("would u-bob: admin → user (not in GIDEON-Admins)", lines)
        self.assertIn("would u-carol: user → pending (not in GIDEON-Users)", lines)
        self.assertIn("would u-dave: pending → user (in GIDEON-Users)", lines)
        self.assertIn("would u-frank: user → pending (not in GIDEON-Users)", lines)
        self.assertIn("would inconsistency: zed is in GIDEON-Admins but not GIDEON-Users", lines)
        self.assertIn("would inconsistency: frank has no userPrincipalName", lines)
        self.assertIn("would orphaned knowledge base kb-1: owner u-carol is pending", lines)
        self.assertIn("would orphaned knowledge base kb-3: owner u-ghost is missing", lines)
        self.assertFalse(any("kb-4" in line for line in lines), "a restored owner is not an orphan")
        self.assertIn("would snapshot: 2 groups recorded", lines)
        self.assertIn("would Summary: 5 correction(s), 2 orphaned knowledge base(s), 2 group(s) snapshotted.", lines)
        for absent in ("u-admin", "u-eval", "u-erin", "kb-2"):
            self.assertFalse(any(absent in line for line in lines), absent)
        self.assertEqual(front.role_updates, [])
        self.assertEqual(host.audit_sql, [])
        self.assertNotIn(ADMIN_KEY, out)


class Enforce(unittest.TestCase):
    def test_now_probes_snapshots_then_intents_updates_and_applied_rows_in_order(self) -> None:
        host = FakeHost(directory(), files())
        front = frontend()
        code, out, err = reconcile(host, front, now=True)
        self.assertEqual((code, err), (0, ""), out)
        self.assertFalse(any(line.startswith("would ") for line in out.splitlines()))
        self.assertEqual(front.role_updates, [("u-alice", "admin"), ("u-bob", "user"), ("u-carol", "pending"), ("u-dave", "user"), ("u-frank", "pending")])
        lines = out.splitlines()
        self.assertEqual(lines[0], "snapshot: 2 groups recorded")
        self.assertEqual(lines[-1], "Summary: 5 correction(s), 2 orphaned knowledge base(s), 2 group(s) snapshotted.")
        self.assertLess(lines.index("u-alice: user → admin (in GIDEON-Admins)"), lines.index("orphaned knowledge base kb-1: owner u-carol is pending"))
        self.assertEqual(host.audit_sql[0], "SELECT 1;\n")
        snapshot = host.audit_sql[1]
        self.assertEqual(snapshot.count("membership_snapshot"), 2)
        self.assertIn('"account": "frank", "upn": null, "user_id": null', snapshot)
        self.assertIn('"account": "newbie", "upn": "newbie@example.org", "user_id": null', snapshot)
        kinds = [("intent" if "reconcile.role.intent" in sql else "applied") for sql in host.audit_sql[2:]]
        self.assertEqual(kinds, ["intent", "applied"] * 5)
        self.assertIn("\\set v_user 'u-alice'", host.audit_sql[2])
        self.assertIn('"to": "admin"', host.audit_sql[2])
        self.assertIn("\\set v_actor 'u-admin'", host.audit_sql[2])
        run_ids = {line for sql in host.audit_sql[1:] for line in sql.splitlines() if line.startswith("\\set v_run_id")}
        self.assertEqual(len(run_ids), 1)
        joined = "\n".join(host.audit_sql)
        self.assertIn("alice@example.org", joined)
        for text in ("Matter", ADMIN_KEY):
            self.assertNotIn(text, joined)

    def test_failed_update_stops_after_its_intent_row(self) -> None:
        host = FakeHost(directory(), files())
        front = frontend()
        front.fail_update_for = "u-bob"
        code, _, err = reconcile(host, front, now=True)
        self.assertEqual(code, 1)
        self.assertIn("role update failed for u-bob", err)
        self.assertEqual(front.role_updates, [("u-alice", "admin")])
        self.assertIn("u-alice: user → admin", _)
        self.assertNotIn("u-bob:", _)
        self.assertNotIn("Summary:", _)
        intents = [sql for sql in host.audit_sql if "reconcile.role.intent" in sql]
        applied = [sql for sql in host.audit_sql if "reconcile.role.applied" in sql]
        self.assertEqual(len(intents), 2)
        self.assertEqual(len(applied), 1)

    def test_audit_probe_failure_refuses_before_any_update(self) -> None:
        host = FakeHost(directory(), files(), audit_rc=2)
        front = frontend()
        code, _, err = reconcile(host, front, now=True)
        self.assertEqual(code, 1)
        self.assertIn("audit writer is unavailable", err)
        self.assertIn("logs postgres", err)
        self.assertEqual(front.role_updates, [])
        self.assertEqual(host.audit_sql, ["SELECT 1;\n"])


class Refusals(unittest.TestCase):
    def test_root_required(self) -> None:
        code, _, err = reconcile(FakeHost(directory(), files(), euid=1000), frontend(), now=False)
        self.assertEqual(code, 1)
        self.assertIn("sudo", err)

    def test_missing_admin_key_names_apply(self) -> None:
        code, _, err = reconcile(FakeHost(directory(), files(admin_key=False)), frontend(), now=False)
        self.assertEqual(code, 1)
        self.assertIn("gideon apply", err)

    def test_directory_failure_names_the_leaf_and_the_checklist(self) -> None:
        commands = directory()
        commands[search(ADMINS_DN)] = subprocess.CompletedProcess(list(search(ADMINS_DN)), 49, "", "invalid credentials")
        host = FakeHost(commands, files())
        front = frontend()
        code, _, err = reconcile(host, front, now=True)
        self.assertEqual(code, 1)
        self.assertIn("auth.ldap.admins_group", err)
        self.assertIn("docs/runbooks/office-services-setup.md §1", err)
        self.assertEqual(front.role_updates, [])

    def test_frontend_not_ready_refuses_with_logs(self) -> None:
        front = frontend()
        front.ready = False
        code, _, err = reconcile(FakeHost(directory(), files()), front, now=False)
        self.assertEqual(code, 1)
        self.assertIn("logs open-webui", err)

    def test_password_never_reaches_argv(self) -> None:
        host = FakeHost(directory(), files())
        reconcile(host, frontend(), now=False)
        for call in host.calls:
            if call and call[0] == "env":
                self.assertIn("-y", call)
                self.assertIn("/etc/gideon/secrets/ldap_bind_password", call)
