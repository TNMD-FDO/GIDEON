"""Chat retention selection, shared-upload protection, audit ordering, and refusals."""

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import unittest
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from gideon.host import owuiturn, report, retention, site
from gideon.host.owui import Client, OwuiError, Response
from gideon.host.render.owui import BREAK_GLASS, FEEDBACK_LIST_ROUTE
from gideon.host.stack import exec_argv
from gideon.host.sysio import Command, PathLike

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config/site.example.yaml"
SITE = "/etc/gideon/site.yaml"
RENDERED = "/etc/gideon/rendered"
PASSWORD_PATH = "/etc/gideon/secrets/gideon_admin_password"
PASSWORD = "fictitious-retention-password"
TOKEN = "fictitious-retention-session-token"
NOW = 2_000_000_000.875
DAY_SECONDS = 24 * 60 * 60
TITLE = "Fictitious private chat title"
FILENAME = "fictitious-private-upload.txt"
MESSAGE = "Fictitious private message body"
COMMENT = "Fictitious private feedback comment"
KB_NAME = "Fictitious private knowledge name"
ADMIN = "fixture-admin"
ALICE = "fixture-alice"
BOB = "fixture-bob"
AUDIT_PSQL = tuple(exec_argv(
    RENDERED, "postgres", "psql", "-U", "gideon_audit", "-d", "gideon",
    "-v", "ON_ERROR_STOP=1", "--single-transaction", "-f", "-",
))


def files(interval: str = "7d") -> dict[str, str]:
    return {
        SITE: EXAMPLE.read_text() + f"\nretention:\n  chats: {interval}\n",
        PASSWORD_PATH: PASSWORD + "\n",
    }


class FakeHost:
    def __init__(self, events: list[tuple[str, str]], file_contents: Mapping[str, str]) -> None:
        self.events = events
        self.files = dict(file_contents)
        self.euid = 0
        self.unreadable: set[str] = set()
        self.probe_rc = 0
        self.fail_writes: set[int] = set()
        self.writes = 0
        self.audit_sql: list[str] = []
        self.committed_sql: list[str] = []
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Command, *, check: bool = False, input: str | None = None,
            cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
            timeout: float | None = None, passthrough: bool = False) -> subprocess.CompletedProcess[str]:
        command = tuple(argv)
        self.calls.append(command)
        if command != AUDIT_PSQL:
            raise AssertionError(f"Unexpected host command: {command}")
        sql = input or ""
        self.audit_sql.append(sql)
        self.events.append(("SQL", sql))
        if sql == "SELECT 1;\n":
            rc = self.probe_rc
        else:
            rc = 2 if self.writes in self.fail_writes else 0
            self.writes += 1
        if rc == 0:
            self.committed_sql.append(sql)
        return subprocess.CompletedProcess(list(command), rc, "", "fictitious SQL diagnostic")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        key = os.fspath(path)
        if key in self.unreadable:
            raise PermissionError("fictitious permission denied")
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def exists(self, path: PathLike) -> bool:
        return os.fspath(path) in self.files

    def geteuid(self) -> int:
        return self.euid

    def write_text(self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644) -> None:
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


class Frontend:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        self.events = events
        self.requests: list[tuple[str, str]] = []
        self.clients: list[FakeClient] = []
        self.users = [
            {"id": ADMIN, "email": BREAK_GLASS.email.upper(), "name": BREAK_GLASS.username, "role": "admin"},
            {"id": ALICE, "email": "alice@example.org", "name": "Alice", "role": "user"},
            {"id": BOB, "email": "bob@example.org", "name": "Bob", "role": "pending"},
        ]
        self.chats: dict[str, dict[str, Any]] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.knowledge: dict[str, list[str]] = {}
        self.feedback: dict[str, dict[str, Any]] = {}
        self.overrides: dict[tuple[str, str], Response | Exception] = {}
        self.file_delete_status: dict[str, int] = {}
        self.ready = True
        self.signins = 0

    def add_chat(self, chat_id: str, updated_at: object, *, owner: str = ALICE,
                 file_ids: tuple[str, ...] = (), archived: bool = False,
                 pinned: bool = False, folder_id: str | None = None) -> None:
        attachments = [{"type": "file", "id": identifier} for identifier in file_ids]
        self.chats[chat_id] = {
            "id": chat_id, "user_id": owner, "updated_at": updated_at,
            "archived": archived, "pinned": pinned, "folder_id": folder_id,
            "title": TITLE,
            "chat": {
                "title": TITLE, "files": attachments,
                "history": {"messages": {"fixture-message": {
                    "content": MESSAGE, "files": attachments,
                }}},
            },
        }
        for identifier in file_ids:
            self.uploads.setdefault(identifier, {
                "id": identifier, "user_id": owner, "filename": FILENAME,
                "data": {"content": MESSAGE},
            })

    def add_feedback(self, feedback_id: str, updated_at: int, *, owner: str = ALICE,
                     chat_id: object = "fixture-unrelated-chat") -> None:
        self.feedback[feedback_id] = {
            "id": feedback_id, "user_id": owner, "created_at": updated_at,
            "updated_at": updated_at, "meta": {"chat_id": chat_id},
            "data": {"comment": COMMENT}, "snapshot": {"chat": {"title": TITLE}},
            "user": {"name": "Fictitious rater", "email": "rater@example.org"},
        }

    def handle(self, method: str, path: str, body: object | None, credential: str | None) -> Response:
        self.requests.append((method, path))
        self.events.append((method, path))
        override = self.overrides.get((method, path))
        if isinstance(override, Exception):
            raise override
        if override is not None:
            return override
        if (method, path) == ("GET", "/ready"):
            return Response(200, {"status": self.ready})
        if (method, path) == ("POST", "/api/v1/auths/signin"):
            assert credential is None
            assert body == {"email": BREAK_GLASS.email, "password": PASSWORD}
            self.signins += 1
            return Response(200, {"token": TOKEN})
        assert credential == TOKEN
        route = urlsplit(path).path
        page = int(parse_qs(urlsplit(path).query).get("page", ["1"])[0])
        if (method, route) == ("GET", "/api/v1/users/all"):
            return Response(200, {"users": self.users, "total": len(self.users)})
        if method == "GET" and route.startswith("/api/v1/chats/list/user/"):
            owner = route.rsplit("/", 1)[1]
            chats = [item for item in self.chats.values() if item["user_id"] == owner]
            chats.sort(key=lambda item: item["id"])
            return Response(200, [
                {key: item[key] for key in ("id", "title", "updated_at")}
                for item in chats[(page - 1) * 60:page * 60]
            ])
        if method == "GET" and route == "/api/v1/knowledge/":
            items = [{"id": key, "name": KB_NAME, "user_id": BOB} for key in self.knowledge]
            return Response(200, {"items": items[(page - 1) * 30:page * 30], "total": len(items)})
        if method == "GET" and route.startswith("/api/v1/knowledge/") and route.endswith("/files"):
            identifiers = self.knowledge[route.split("/")[-2]]
            return Response(200, {
                "items": [{"id": identifier} for identifier in identifiers[(page - 1) * 30:page * 30]],
                "total": len(identifiers),
            })
        if (method, route) == ("GET", FEEDBACK_LIST_ROUTE):
            items = list(self.feedback.values())
            return Response(200, {"items": items[(page - 1) * 30:page * 30], "total": len(items)})
        if route.startswith("/api/v1/chats/"):
            records = self.chats
        elif route.startswith("/api/v1/files/"):
            records = self.uploads
        elif route.startswith("/api/v1/evaluations/feedback/"):
            records = self.feedback
        else:
            raise AssertionError(f"Unexpected frontend request: {method} {path}")
        identifier = route.rsplit("/", 1)[1]
        if identifier not in records:
            return Response(404, None)
        if method == "GET":
            return Response(200, records[identifier])
        if method != "DELETE":
            raise AssertionError(f"Unexpected frontend method: {method}")
        del records[identifier]
        if route.startswith("/api/v1/files/"):
            status = self.file_delete_status.get(identifier, 200)
            return Response(status, {"message": "File deleted"} if status == 200 else {"detail": MESSAGE})
        return Response(200, True)


class FakeClient(Client):
    def __init__(self, frontend: Frontend, *, api_key: str | None = None, token: str | None = None) -> None:
        super().__init__("http://frontend.example.org", api_key=api_key, token=token)
        self.frontend = frontend

    def request(self, method: str, path: str, body: object | None = None) -> Response:
        assert self._api_key is None
        return self.frontend.handle(method, path, body, self._token)


def audit_batches(host: FakeHost) -> list[list[dict[str, Any]]]:
    """Read the committed rows from the audit writer's psql stdin."""
    batches: list[list[dict[str, Any]]] = []
    for sql in host.committed_sql:
        values: dict[str, str] = {}
        rows = []
        for line in sql.splitlines():
            if line.startswith("\\set "):
                _, key, literal = line.split(" ", 2)
                values[key] = literal[1:-1].replace("\\\\", "\\").replace("''", "'")
            elif line.startswith("INSERT INTO audit_log "):
                rows.append({
                    "run_id": values["v_run_id"], "kind": values["v_kind"],
                    "actor_user_id": values.get("v_actor"), "user_id": values.get("v_user"),
                    "chat_id": values.get("v_chat"), "kb_ids": values["v_kb"],
                    "detail": json.loads(values["v_detail"]),
                })
                values = {}
        if rows:
            batches.append(rows)
    return batches


class SweepCase(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(report.set_form_from_environment, os.environ.copy())
        report.set_installed_form(False)
        self.reset_case()

    def reset_case(self, interval: str = "7d") -> None:
        self.events: list[tuple[str, str]] = []
        self.host = FakeHost(self.events, files(interval))
        self.front = Frontend(self.events)
        self.stages: list[report.StageResult] = []
        self.sleeps: list[float] = []
        self.boundary = int(NOW) - 7 * DAY_SECONDS

    def sweep(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        print_stage = report.print_stage

        def factory(*, api_key: str | None = None, token: str | None = None) -> Client:
            client = FakeClient(self.front, api_key=api_key, token=token)
            self.front.clients.append(client)
            return client

        def observe(stage: report.StageResult) -> None:
            self.stages.append(stage)
            print_stage(stage)

        with (
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err),
            patch.object(report, "print_stage", side_effect=observe),
        ):
            code = retention.run_retention_sweep(
                argparse.Namespace(), host=self.host, client_factory=factory,
                sleep=self.sleeps.append, clock=lambda: NOW,
            )
        for credential in (PASSWORD, TOKEN):
            for text in (
                out.getvalue(), err.getvalue(), repr(self.stages), repr(self.front.clients),
                repr(self.host.calls), "\n".join(self.host.audit_sql), repr(audit_batches(self.host)),
            ):
                self.assertNotIn(credential, text)
        return code, out.getvalue(), err.getvalue()

    def stage(self, name: str) -> report.StageResult:
        return next(stage for stage in self.stages if stage.name == name)

    def counts(self, name: str) -> dict[str, int]:
        return {key: int(value) for key, value in re.findall(r"([\w-]+)=(\d+)", self.stage(name).detail)}

    def rows(self, kind: str) -> list[dict[str, Any]]:
        return [row for batch in audit_batches(self.host) for row in batch if row["kind"] == kind]

    def assert_success(self) -> str:
        previous_signins = self.front.signins
        code, out, err = self.sweep()
        self.assertEqual((code, err), (0, ""), out)
        self.assertTrue(all(stage.ok for stage in self.stages), out)
        self.assertEqual([stage.name for stage in self.stages], ["signin", "chats", "files", "feedback", "audit"])
        self.assertEqual(self.stage("signin").detail, "signed in as the break-glass administrator")
        self.assertEqual(self.front.signins, previous_signins + 1)
        return out


class Selection(SweepCase):
    def test_interval_keys_and_integer_cutoffs_cover_the_site_grammar(self) -> None:
        self.assertEqual(set(retention.RETENTION_DAYS), set(site._RETENTION_VALUES))
        for interval in site._RETENTION_VALUES:
            with self.subTest(interval=interval):
                days = 365 if interval == "1y" else int(interval[:-1])
                self.assertEqual(retention.RETENTION_DAYS[interval], days)
                boundary = retention.cutoff(NOW, interval)
                self.assertIs(type(boundary), int)
                self.assertEqual(boundary, int(NOW) - days * DAY_SECONDS)

    def test_unknown_interval_refuses_before_any_request(self) -> None:
        with self.assertRaises(ValueError):
            retention.cutoff(NOW, "fictitious-interval")
        self.reset_case("fictitious-interval")
        code, out, err = self.sweep()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("retention.chats", err)
        self.assertIn("Correct the site file", err)
        self.assertEqual(self.front.requests, [])
        self.assertEqual(self.host.calls, [])

    def test_one_second_older_is_swept_equal_and_younger_are_kept(self) -> None:
        self.front.add_chat("fixture-older", self.boundary - 1)
        self.front.add_chat("fixture-equal", self.boundary)
        self.front.add_chat("fixture-younger", self.boundary + 1)
        self.assert_success()
        self.assertEqual(set(self.front.chats), {"fixture-equal", "fixture-younger"})
        self.assertEqual(self.counts("chats"), {
            "accounts": len(self.front.users), "read": 3, "candidates": 1,
            "swept": 1, "vanished": 0, "resumed": 0, "failed": 0,
        })
        intent = self.rows("retention_sweep")[0]["detail"]
        self.assertIs(type(intent["cutoff"]), int)
        self.assertEqual(intent["cutoff"], self.boundary)

    def test_one_year_sweeps_only_beyond_365_days(self) -> None:
        self.reset_case("1y")
        boundary = int(NOW) - 365 * DAY_SECONDS
        self.front.add_chat("fixture-older", boundary - 1)
        self.front.add_chat("fixture-equal", boundary)
        self.front.add_chat("fixture-younger", boundary + 1)
        self.assert_success()
        self.assertEqual(set(self.front.chats), {"fixture-equal", "fixture-younger"})
        self.assertEqual(self.rows("retention_sweep")[0]["detail"]["interval"], "1y")

    def test_archived_pinned_folder_and_every_account_are_subject_to_retention(self) -> None:
        for chat_id, options in (
            ("fixture-archived", {"archived": True}),
            ("fixture-pinned", {"pinned": True}),
            ("fixture-folder", {"folder_id": "fixture-folder-id"}),
            ("fixture-admin-chat", {"owner": ADMIN}),
            ("fixture-pending-chat", {"owner": BOB}),
        ):
            self.front.add_chat(chat_id, self.boundary - 1, **options)
        self.front.add_chat("fixture-control", int(NOW))
        self.assert_success()
        self.assertEqual(set(self.front.chats), {"fixture-control"})
        self.assertEqual(self.counts("chats")["swept"], 5)
        self.assertEqual({row["user_id"] for row in self.rows("chat_swept")}, {ADMIN, ALICE, BOB})

    def test_full_chat_page_and_shorter_page_are_read_before_any_delete(self) -> None:
        for index in range(62):
            self.front.add_chat(f"fixture-page-{index:03}", self.boundary - 1)
        self.assert_success()
        self.assertEqual(self.front.chats, {})
        paths = [("GET", f"/api/v1/chats/list/user/{ALICE}?page={page}") for page in (1, 2)]
        for request in paths:
            self.assertEqual(self.front.requests.count(request), 1)
        self.assertNotIn(("GET", f"/api/v1/chats/list/user/{ALICE}?page=3"), self.front.requests)
        first_delete = next(index for index, (method, _) in enumerate(self.events) if method == "DELETE")
        self.assertTrue(all(self.events.index(request) < first_delete for request in paths))
        self.assertEqual(self.counts("chats")["candidates"], 62)
        self.assertEqual(self.counts("chats")["read"], 62)

    def test_vanished_candidate_is_skipped_for_each_not_found_status(self) -> None:
        for status in owuiturn.NOT_FOUND_STATUSES:
            with self.subTest(status=status):
                self.reset_case()
                self.front.add_chat("fixture-vanished", self.boundary - 1)
                self.front.overrides[("GET", "/api/v1/chats/fixture-vanished")] = Response(status, None)
                self.assert_success()
                self.assertEqual(self.counts("chats")["vanished"], 1)
                self.assertEqual(self.counts("chats")["swept"], 0)
                self.assertFalse(any(method == "DELETE" for method, _ in self.front.requests))
                self.assertEqual(self.rows("chat_swept"), [])

    def test_unconfirmed_or_failed_chat_delete_does_not_stop_later_work(self) -> None:
        for response in (Response(200, False), Response(503, None), OwuiError("fictitious failure")):
            with self.subTest(response=response):
                self.reset_case()
                self.front.add_chat("fixture-a-refused", self.boundary - 1)
                self.front.add_chat("fixture-b-swept", self.boundary - 1, file_ids=("fixture-upload",))
                self.front.add_feedback("fixture-rating", self.boundary - 1)
                self.front.overrides[("DELETE", "/api/v1/chats/fixture-a-refused")] = response
                code, out, err = self.sweep()
                self.assertEqual((code, err), (1, ""), out)
                self.assertFalse(self.stage("chats").ok)
                self.assertIn("logs open-webui", self.stage("chats").fix)
                self.assertEqual(self.counts("chats")["failed"], 1)
                self.assertEqual(self.counts("chats")["swept"], 1)
                self.assertEqual(set(self.front.chats), {"fixture-a-refused"})
                self.assertEqual(self.front.uploads, {})
                self.assertEqual(self.front.feedback, {})
                self.assertTrue(self.stage("audit").ok)

    def test_chat_resumed_since_the_listing_is_kept_and_protects_its_files(self) -> None:
        self.front.add_chat("fixture-resumed", self.boundary - 1, file_ids=("fixture-shared",))
        self.front.add_chat("fixture-expired", self.boundary - 1, file_ids=("fixture-shared",))
        resumed = dict(self.front.chats["fixture-resumed"], updated_at=int(NOW))
        self.front.overrides[("GET", "/api/v1/chats/fixture-resumed")] = Response(200, resumed)
        self.assert_success()
        self.assertEqual(set(self.front.chats), {"fixture-resumed"})
        self.assertNotIn(("DELETE", "/api/v1/chats/fixture-resumed"), self.front.requests)
        self.assertIn("fixture-shared", self.front.uploads)
        self.assertEqual(self.counts("chats")["resumed"], 1)
        self.assertEqual(self.counts("chats")["swept"], 1)
        self.assertEqual(self.rows("chat_swept")[0]["detail"]["files"], [
            {"id": "fixture-shared", "outcome": "kept", "reason": "still-named"},
        ])

    def test_unread_references_leave_the_owners_chats_for_the_next_run(self) -> None:
        knowledge_route = ("GET", "/api/v1/knowledge/?page=1")
        for failing in (
            ("GET", "/api/v1/chats/fixture-unread-candidate"),
            ("GET", "/api/v1/chats/fixture-young"),
            knowledge_route,
        ):
            with self.subTest(failing=failing):
                self.reset_case()
                self.front.add_chat("fixture-named", self.boundary - 1, file_ids=("fixture-upload",))
                self.front.add_chat("fixture-young", int(NOW))
                if failing[1].endswith("unread-candidate"):
                    self.front.add_chat("fixture-unread-candidate", self.boundary - 1)
                self.front.add_chat("fixture-bob-chat", self.boundary - 1, owner=BOB)
                self.front.overrides[failing] = OwuiError("fictitious failure")
                code, out, err = self.sweep()
                self.assertEqual((code, err), (1, ""), out)
                self.assertFalse(self.stage("chats").ok)
                self.assertIn("fixture-named", self.front.chats)
                self.assertIn("fixture-upload", self.front.uploads)
                self.assertNotIn(("DELETE", "/api/v1/chats/fixture-named"), self.front.requests)
                # Another account's chat naming no upload is still swept.
                self.assertNotIn("fixture-bob-chat", self.front.chats)
                self.assertEqual([row["chat_id"] for row in self.rows("chat_swept")], ["fixture-bob-chat"])

    def test_invalid_chat_timestamp_refuses_selection_without_deleting(self) -> None:
        for value in (None, True, "fictitious timestamp"):
            with self.subTest(value=value):
                self.reset_case()
                self.front.add_chat("fixture-invalid", value)
                code, out, err = self.sweep()
                self.assertEqual((code, err), (1, ""), out)
                self.assertFalse(self.stage("chats").ok)
                self.assertIn("logs open-webui", self.stage("chats").fix)
                self.assertFalse(any(method == "DELETE" for method, _ in self.front.requests))


class Uploads(SweepCase):
    def test_only_nonempty_file_ids_from_both_locations_are_deleted_once(self) -> None:
        self.front.add_chat("fixture-chat", self.boundary - 1, file_ids=("fixture-top", "fixture-message"))
        record = self.front.chats["fixture-chat"]["chat"]
        ignored = [
            {"type": kind, "id": f"fixture-{kind}"} for kind in ("image", "chat", "note")
        ] + [{"type": "file", "id": ""}, {"type": "file", "id": None}, {"type": "file", "id": 42}]
        record["files"] = [{"type": "file", "id": "fixture-top"}, *ignored]
        record["history"]["messages"]["fixture-message"]["files"] = [
            {"type": "file", "id": "fixture-message"},
            {"type": "file", "id": "fixture-top"}, *ignored,
        ]
        self.assert_success()
        self.assertEqual(self.front.uploads, {})
        deleted = [path for method, path in self.front.requests if method == "DELETE"]
        self.assertEqual(deleted, [
            "/api/v1/chats/fixture-chat", "/api/v1/files/fixture-message", "/api/v1/files/fixture-top",
        ])
        self.assertEqual(self.counts("files"), {"named": 2, "deleted": 2, "gone": 0, "kept": 0, "failed": 0})
        self.assertEqual(self.rows("chat_swept")[0]["detail"]["files"], [
            {"id": "fixture-message", "outcome": "deleted"}, {"id": "fixture-top", "outcome": "deleted"},
        ])

    def test_another_accounts_upload_is_kept_with_its_reason(self) -> None:
        self.front.add_chat("fixture-chat", self.boundary - 1, file_ids=("fixture-foreign",))
        self.front.uploads["fixture-foreign"]["user_id"] = BOB
        self.assert_success()
        self.assertIn("fixture-foreign", self.front.uploads)
        self.assertNotIn(("DELETE", "/api/v1/files/fixture-foreign"), self.front.requests)
        self.assertEqual(self.counts("files")["kept"], 1)
        self.assertEqual(self.rows("chat_swept")[0]["detail"]["files"], [
            {"id": "fixture-foreign", "outcome": "kept", "reason": "other-owner"},
        ])

    def test_young_chat_protects_shared_files_and_is_read_once(self) -> None:
        identifiers = ("fixture-shared-a", "fixture-shared-b")
        self.front.add_chat("fixture-expired", self.boundary - 1, file_ids=identifiers)
        self.front.add_chat("fixture-young", int(NOW), file_ids=identifiers)
        self.assert_success()
        self.assertEqual(set(self.front.chats), {"fixture-young"})
        self.assertEqual(set(self.front.uploads), set(identifiers))
        self.assertEqual(self.front.requests.count(("GET", "/api/v1/chats/fixture-young")), 1)
        self.assertEqual(self.counts("files")["kept"], len(identifiers))
        self.assertEqual(self.rows("chat_swept")[0]["detail"]["files"], [
            {"id": identifier, "outcome": "kept", "reason": "still-named"} for identifier in identifiers
        ])

    def test_shared_file_waits_for_every_expired_chat_deletion(self) -> None:
        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed):
                self.reset_case()
                for chat_id in ("fixture-first", "fixture-second"):
                    self.front.add_chat(chat_id, self.boundary - 1, file_ids=("fixture-shared",))
                if not confirmed:
                    self.front.overrides[("DELETE", "/api/v1/chats/fixture-second")] = Response(200, False)
                code, out, err = self.sweep()
                self.assertEqual((code, err), (0 if confirmed else 1, ""), out)
                file_read = self.front.requests.index(("GET", "/api/v1/files/fixture-shared"))
                for chat_id in ("fixture-first", "fixture-second"):
                    self.assertLess(self.front.requests.index(("DELETE", f"/api/v1/chats/{chat_id}")), file_read)
                    self.assertEqual(self.front.requests.count(("GET", f"/api/v1/chats/{chat_id}")), 1)
                if confirmed:
                    self.assertEqual(self.front.chats, {})
                    self.assertNotIn("fixture-shared", self.front.uploads)
                    self.assertEqual(self.front.requests.count(("DELETE", "/api/v1/files/fixture-shared")), 1)
                    self.assertEqual(self.counts("files")["deleted"], 1)
                    expected = {"id": "fixture-shared", "outcome": "deleted"}
                else:
                    self.assertEqual(set(self.front.chats), {"fixture-second"})
                    self.assertIn("fixture-shared", self.front.uploads)
                    self.assertNotIn(("DELETE", "/api/v1/files/fixture-shared"), self.front.requests)
                    self.assertEqual(self.counts("files")["kept"], 1)
                    expected = {"id": "fixture-shared", "outcome": "kept", "reason": "still-named"}
                for row in self.rows("chat_swept"):
                    self.assertEqual(row["detail"]["files"], [expected])

    def test_knowledge_files_are_read_page_by_page_once_and_kept(self) -> None:
        identifiers = [f"fixture-kb-file-{index:02}" for index in range(31)]
        self.front.knowledge = {"fixture-kb": identifiers}
        self.front.add_chat("fixture-first", self.boundary - 1, file_ids=(identifiers[-1],))
        self.front.add_chat("fixture-second", self.boundary - 1, file_ids=(identifiers[0],))
        self.assert_success()
        self.assertEqual(self.counts("files")["kept"], 2)
        for page in (1, 2, 3):
            self.assertEqual(self.front.requests.count(("GET", f"/api/v1/knowledge/fixture-kb/files?page={page}")), 1)
        self.assertFalse(any("directory_id" in path for _, path in self.front.requests))
        for row in self.rows("chat_swept"):
            self.assertEqual(row["detail"]["files"][0]["outcome"], "kept")
            self.assertEqual(row["detail"]["files"][0]["reason"], "knowledge")
        first_delete = next(index for index, (method, _) in enumerate(self.front.requests) if method == "DELETE")
        self.assertLess(self.front.requests.index(("GET", "/api/v1/knowledge/fixture-kb/files?page=3")), first_delete)

    def test_a_missing_upload_is_gone_without_a_delete(self) -> None:
        self.front.add_chat("fixture-chat", self.boundary - 1, file_ids=("fixture-gone",))
        del self.front.uploads["fixture-gone"]
        self.assert_success()
        self.assertEqual(self.counts("files")["gone"], 1)
        self.assertNotIn(("DELETE", "/api/v1/files/fixture-gone"), self.front.requests)
        self.assertEqual(self.rows("chat_swept")[0]["detail"]["files"], [
            {"id": "fixture-gone", "outcome": "gone"},
        ])

    def test_file_delete_400_fails_the_row_but_keeps_the_chat_swept(self) -> None:
        self.front.add_chat("fixture-chat", self.boundary - 1, file_ids=("fixture-failed", "fixture-next"))
        self.front.file_delete_status["fixture-failed"] = 400
        code, out, err = self.sweep()
        self.assertEqual((code, err), (1, ""), out)
        self.assertEqual(self.front.chats, {})
        self.assertEqual(self.front.uploads, {})
        self.assertEqual(self.counts("chats")["swept"], 1)
        self.assertEqual(self.counts("files"), {"named": 2, "deleted": 1, "gone": 0, "kept": 0, "failed": 1})
        self.assertFalse(self.stage("files").ok)
        self.assertIn("logs open-webui", self.stage("files").fix)
        self.assertEqual(self.rows("chat_swept")[0]["detail"]["files"], [
            {"id": "fixture-failed", "outcome": "failed"}, {"id": "fixture-next", "outcome": "deleted"},
        ])


class Feedback(SweepCase):
    def test_every_feedback_page_is_read_before_the_first_delete(self) -> None:
        for index in range(32):
            self.front.add_feedback(f"fixture-rating-{index:02}", self.boundary - 1)
        self.assert_success()
        self.assertEqual(self.front.feedback, {})
        self.assertEqual(self.counts("feedback")["read"], 32)
        self.assertEqual(self.counts("feedback")["swept-by-age"], 32)
        self.assertEqual(len(self.rows("feedback_swept")), 32)
        first_delete = next(index for index, (method, _) in enumerate(self.front.requests) if method == "DELETE")
        for page in (1, 2, 3):
            request = ("GET", f"{FEEDBACK_LIST_ROUTE}?page={page}")
            self.assertEqual(self.front.requests.count(request), 1)
            self.assertLess(self.front.requests.index(request), first_delete)

    def test_age_and_swept_chat_select_feedback_independently(self) -> None:
        self.front.add_chat("fixture-swept", self.boundary - 1)
        self.front.add_chat("fixture-control", int(NOW))
        self.front.add_feedback("fixture-old-rating", self.boundary - 1, chat_id="fixture-control")
        self.front.add_feedback("fixture-equal-rating", self.boundary, chat_id="fixture-control")
        self.front.add_feedback("fixture-young-rating", self.boundary + 1, chat_id="fixture-control")
        self.front.add_feedback("fixture-linked-rating", int(NOW), owner=BOB, chat_id="fixture-swept")
        self.assert_success()
        self.assertEqual(set(self.front.feedback), {"fixture-equal-rating", "fixture-young-rating"})
        self.assertEqual(self.counts("feedback"), {"read": 4, "swept-by-age": 1, "swept-with-chat": 1, "failed": 0})
        aged = self.rows("feedback_swept")
        self.assertEqual(len(aged), 1)
        self.assertEqual(aged[0]["user_id"], ALICE)
        self.assertIsNone(aged[0]["chat_id"])
        self.assertEqual(aged[0]["detail"], {"feedback_id": "fixture-old-rating", "chat_id": "fixture-control"})
        self.assertEqual(self.rows("chat_swept")[0]["detail"]["feedback"], ["fixture-linked-rating"])
        self.assertEqual(self.rows("retention_sweep")[0]["detail"]["feedback_candidates"], 2)

    def test_age_alone_drops_non_id_chat_references(self) -> None:
        for chat_id in (None, 42, {"text": MESSAGE}, MESSAGE):
            with self.subTest(chat_id=chat_id):
                self.reset_case()
                self.front.add_feedback("fixture-rating", self.boundary - 1, chat_id=chat_id)
                self.assert_success()
                self.assertEqual(self.rows("feedback_swept")[0]["detail"], {"feedback_id": "fixture-rating", "chat_id": None})
                self.assertNotIn(MESSAGE, "\n".join(self.host.audit_sql))

    def test_feedback_deletion_requires_true_and_continues_after_failure(self) -> None:
        for response in (Response(200, False), Response(500, None), OwuiError("fictitious failure")):
            with self.subTest(response=response):
                self.reset_case()
                for feedback_id in ("fixture-failed-rating", "fixture-next-rating"):
                    self.front.add_feedback(feedback_id, self.boundary - 1)
                self.front.overrides[("DELETE", "/api/v1/evaluations/feedback/fixture-failed-rating")] = response
                code, out, err = self.sweep()
                self.assertEqual((code, err), (1, ""), out)
                self.assertEqual(set(self.front.feedback), {"fixture-failed-rating"})
                self.assertEqual(self.counts("feedback")["failed"], 1)
                self.assertEqual(self.counts("feedback")["swept-by-age"], 1)
                self.assertIn("logs open-webui", self.stage("feedback").fix)
                self.assertEqual(len(self.rows("feedback_swept")), 1)


class Audit(SweepCase):
    def test_listings_and_intent_precede_deletes_and_applied_rows_batch_by_account(self) -> None:
        self.front.add_chat("fixture-alice-first", self.boundary - 1, file_ids=("fixture-upload",))
        self.front.add_chat("fixture-alice-second", self.boundary - 1)
        self.front.add_chat("fixture-bob-chat", self.boundary - 1, owner=BOB)
        self.front.add_chat("fixture-control", int(NOW))
        self.front.knowledge = {"fixture-kb": []}
        self.front.add_feedback("fixture-age-rating", self.boundary - 1, chat_id="fixture-control")
        self.front.add_feedback("fixture-linked-rating", int(NOW), owner=BOB, chat_id="fixture-alice-first")
        out = self.assert_success()
        expected_listings = [
            ("GET", "/api/v1/users/all"),
            *(("GET", f"/api/v1/chats/list/user/{user['id']}?page=1") for user in self.front.users),
            ("GET", "/api/v1/knowledge/?page=1"), ("GET", "/api/v1/knowledge/?page=2"),
            ("GET", "/api/v1/knowledge/fixture-kb/files?page=1"),
            ("GET", f"{FEEDBACK_LIST_ROUTE}?page=1"), ("GET", f"{FEEDBACK_LIST_ROUTE}?page=2"),
        ]
        first_delete = next(index for index, (method, _) in enumerate(self.events) if method == "DELETE")
        intent_event = next(index for index, (method, text) in enumerate(self.events) if method == "SQL" and "retention_sweep" in text)
        for request in expected_listings:
            self.assertEqual(self.front.requests.count(request), 1)
            self.assertLess(self.events.index(request), intent_event)
        self.assertLess(intent_event, first_delete)
        self.assertEqual(self.host.audit_sql[0], "SELECT 1;\n")
        batches = audit_batches(self.host)
        self.assertEqual([[row["kind"] for row in batch] for batch in batches], [
            ["retention_sweep"], ["chat_swept", "chat_swept", "feedback_swept"], ["chat_swept"],
        ])
        intent = batches[0][0]
        self.assertIsNone(intent["user_id"])
        self.assertIsNone(intent["chat_id"])
        self.assertEqual(intent["detail"], {
            "interval": "7d", "cutoff": self.boundary, "accounts": len(self.front.users),
            "chats_read": 4, "candidates": 3, "feedback_read": 2, "feedback_candidates": 2,
        })
        self.assertEqual({row["user_id"] for row in batches[1]}, {ALICE})
        self.assertEqual({row["user_id"] for row in batches[2]}, {BOB})
        self.assertEqual({row["chat_id"] for row in self.rows("chat_swept")}, {
            "fixture-alice-first", "fixture-alice-second", "fixture-bob-chat",
        })
        all_rows = [row for batch in batches for row in batch]
        self.assertEqual({row["actor_user_id"] for row in all_rows}, {ADMIN})
        self.assertEqual({row["kb_ids"] for row in all_rows}, {"{}"})
        run_ids = {row["run_id"] for row in all_rows}
        self.assertEqual(len(run_ids), 1)
        uuid.UUID(next(iter(run_ids)))
        for row in self.rows("chat_swept"):
            self.assertEqual(set(row["detail"]), {"files", "feedback"})
            for upload in row["detail"]["files"]:
                self.assertEqual(set(upload), {"id", "outcome"})
        self.assertEqual(set(self.rows("feedback_swept")[0]["detail"]), {"feedback_id", "chat_id"})
        self.assertEqual(self.counts("audit"), {"written": len(all_rows)})
        alice_batch = next(index for index, (method, text) in enumerate(self.events) if method == "SQL" and "fixture-alice-first" in text)
        alice_last_delete = self.events.index(("DELETE", "/api/v1/evaluations/feedback/fixture-linked-rating"))
        self.assertLess(alice_last_delete, alice_batch)
        self.assertLess(alice_batch, self.events.index(("DELETE", "/api/v1/chats/fixture-bob-chat")))
        for forbidden in (TITLE, FILENAME, MESSAGE, COMMENT, KB_NAME, "rater@example.org"):
            self.assertNotIn(forbidden, out)
            self.assertNotIn(forbidden, "\n".join(self.host.audit_sql))
        for identifier in (ADMIN, ALICE, BOB, "fixture-alice-first", "fixture-upload", "fixture-age-rating"):
            self.assertNotIn(identifier, out)

    def test_failed_intent_writes_no_applied_rows_and_deletes_nothing(self) -> None:
        self.front.add_chat("fixture-chat", self.boundary - 1, file_ids=("fixture-upload",))
        self.front.add_feedback("fixture-rating", self.boundary - 1, chat_id="fixture-chat")
        self.host.fail_writes = {0}
        code, out, err = self.sweep()
        self.assertEqual((code, err), (1, ""), out)
        self.assertFalse(any(method == "DELETE" for method, _ in self.front.requests))
        self.assertIn("fixture-chat", self.front.chats)
        self.assertIn("fixture-upload", self.front.uploads)
        self.assertIn("fixture-rating", self.front.feedback)
        self.assertEqual(self.counts("chats")["swept"], 0)
        self.assertEqual(self.counts("files")["deleted"], 0)
        self.assertEqual(self.counts("feedback")["swept-by-age"], 0)
        self.assertEqual(self.counts("feedback")["swept-with-chat"], 0)
        self.assertEqual(self.counts("audit"), {"written": 0})
        self.assertFalse(self.stage("audit").ok)
        self.assertIn("logs postgres", self.stage("audit").fix)
        self.assertEqual(audit_batches(self.host), [])
        self.assertEqual(self.host.writes, 1)

    def test_failed_applied_batch_does_not_stop_the_next_account(self) -> None:
        self.front.add_chat("fixture-alice-chat", self.boundary - 1)
        self.front.add_chat("fixture-bob-chat", self.boundary - 1, owner=BOB)
        self.host.fail_writes = {1}
        code, out, err = self.sweep()
        self.assertEqual((code, err), (1, ""), out)
        self.assertEqual(self.front.chats, {})
        self.assertFalse(self.stage("audit").ok)
        self.assertIn("logs postgres", self.stage("audit").fix)
        self.assertEqual([row["chat_id"] for row in self.rows("chat_swept")], ["fixture-bob-chat"])
        self.assertEqual(self.counts("audit"), {"written": 2})

    def test_repeated_empty_sweep_records_only_an_intent(self) -> None:
        for _ in range(2):
            self.stages.clear()
            self.assert_success()
        self.assertFalse(any(method == "DELETE" for method, _ in self.front.requests))
        batches = audit_batches(self.host)
        self.assertEqual(len(batches), 2)
        for batch in batches:
            self.assertEqual(len(batch), 1)
            self.assertEqual(batch[0]["kind"], "retention_sweep")
            self.assertEqual(batch[0]["detail"]["candidates"], 0)


class Refusals(SweepCase):
    def test_pre_run_refusals_and_fixes_in_both_command_forms(self) -> None:
        for installed in (False, True):
            for failure in ("root", "site", "missing-password", "unreadable-password", "ready", "audit"):
                with self.subTest(installed=installed, failure=failure):
                    self.reset_case()
                    report.set_installed_form(installed)
                    root_command = "gideon" if installed else "python3 -m gideon"
                    apply_command = "gideon" if installed else "sudo python3 -m gideon"
                    if failure == "root":
                        self.host.euid = 1000
                        expected = f"root is required. Fix: Run {root_command} retention sweep as root, for example with sudo."
                    elif failure == "site":
                        self.host.files[SITE] = "hostname: [unterminated"
                        expected = "Correct the site file, then retry."
                    elif failure == "missing-password":
                        del self.host.files[PASSWORD_PATH]
                        expected = f"the break-glass password is missing. Fix: Run {apply_command} apply, then retry"
                    elif failure == "unreadable-password":
                        self.host.unreadable.add(PASSWORD_PATH)
                        expected = "Correct the secret file, then retry."
                    elif failure == "ready":
                        self.front.ready = False
                        expected = "Open WebUI is not ready."
                    else:
                        self.host.probe_rc = 2
                        expected = "audit writer is unavailable"
                    code, out, err = self.sweep()
                    self.assertEqual((code, out), (1, ""), err)
                    self.assertTrue(err.startswith("gideon retention sweep: "), err)
                    self.assertIn(expected, err)
                    self.assertIn("Fix:", err)
                    if failure in ("root", "missing-password"):
                        self.assertEqual(err, f"gideon retention sweep: {expected}\n")
                    if failure in ("site", "unreadable-password"):
                        self.assertIn("site" if failure == "site" else PASSWORD_PATH, err)
                    if failure in ("ready", "audit"):
                        self.assertIn("logs open-webui" if failure == "ready" else "logs postgres", err)
                    if installed:
                        self.assertNotIn("python3 -m gideon", err)
                    self.assertEqual(self.front.signins, 0)
                    self.assertFalse(any(method == "DELETE" for method, _ in self.front.requests))
                    if failure not in ("ready", "audit"):
                        self.assertEqual(self.front.requests, [])
                        self.assertEqual(self.host.calls, [])
                    elif failure == "ready":
                        self.assertTrue(self.sleeps)
                        self.assertTrue(all(request == ("GET", "/ready") for request in self.front.requests))
                        self.assertEqual(self.host.calls, [])
                    else:
                        self.assertEqual(self.host.audit_sql, ["SELECT 1;\n"])

    def test_signin_failure_is_a_row_without_any_authenticated_read(self) -> None:
        for response in (Response(401, {"detail": PASSWORD + TOKEN}), OwuiError(PASSWORD + TOKEN)):
            with self.subTest(response=type(response).__name__):
                self.reset_case()
                self.front.overrides[("POST", "/api/v1/auths/signin")] = response
                code, out, err = self.sweep()
                self.assertEqual((code, err), (1, ""), out)
                self.assertEqual([stage.name for stage in self.stages], ["signin"])
                self.assertFalse(self.stage("signin").ok)
                self.assertIn("logs open-webui", self.stage("signin").fix)
                self.assertEqual(self.front.requests, [("GET", "/ready"), ("POST", "/api/v1/auths/signin")])
                self.assertEqual(self.host.audit_sql, ["SELECT 1;\n"])

    def test_response_and_client_reprs_do_not_expose_credentials(self) -> None:
        self.assert_success()
        for credential in (PASSWORD, TOKEN):
            self.assertNotIn(credential, repr(Response(200, {"password": PASSWORD, "token": TOKEN})))
            self.assertNotIn(credential, repr(self.front.clients))
