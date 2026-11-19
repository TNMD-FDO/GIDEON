"""The announcement sync command over a fake record and frontend."""

import argparse
import ast
import contextlib
import io
import os
import subprocess
import unittest
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_announcements import window_query_argv
from test_owui import ADMIN_KEY, Frontend, banner_row, factory_for
from test_owui import FakeHost as FrontendHost
from test_windowrecord import END, EVENT, KEY, RENDERED, START, reply

from gideon.host import (
    announcements,
    announcesync,
    backuplock,
    owui,
    report,
    secrets,
    site,
    stack,
    tls,
)
from gideon.host.render.owui import BANNERS_ROUTE
from gideon.host.sysio import Command, PathLike

SITE_PATH = "/fictitious-site/site.yaml"
SITE_TEXT = (Path(__file__).parent / "fixtures/site/second-office.yaml").read_text(encoding="utf-8")
SITE_CONFIG = site.load_site_text(SITE_TEXT).config
assert SITE_CONFIG is not None
HOSTNAME = SITE_CONFIG.hostname
ZONE = ZoneInfo(SITE_CONFIG.office.timezone)
KEY_PATH = os.fspath(secrets.secret_path("gideon_admin_api_key"))
FRONTEND_FIX = f"Run {stack.logs_fix(RENDERED, 'open-webui')}, then retry."
PRIVATE = "Fictitious text posted by hand."
BANNER_ID = f"window-{KEY}-ahead"
PARENT = backuplock.Record("fixture upgrade", os.getpid() + 1, EVENT.at)
CHILD = backuplock.Record("fixture apply", os.getpid() + 2, START)
LOCK_PATHS = (backuplock.UNAVAILABLE_LOCK.path, backuplock.UNAVAILABLE_NESTED_LOCK.path)


def command_banner(record: backuplock.Record) -> announcements.Announcement:
    return announcements.command_announcement(record, None, START, ZONE)


class FakeHost(FrontendHost):
    def __init__(self) -> None:
        super().__init__({SITE_PATH: SITE_TEXT, KEY_PATH: ADMIN_KEY + "\n"})
        self.uid = 0
        self.reads = 0
        self.timeouts: list[float | None] = []
        self.reply = reply()
        self.record_exit = 0
        self.record_stderr = ""
        self.record_times_out = False
        self.held_files: dict[str, str] = {}
        self.takes: list[str] = []
        self.releases: list[str] = []
        self.take_errors: dict[str, OSError] = {}

    def geteuid(self) -> int:
        return self.uid

    def stat(self, path: PathLike) -> os.stat_result:
        key = os.fspath(path)
        if key in LOCK_PATHS:
            if key not in self.held_files:
                raise FileNotFoundError(key)
            return os.stat_result((0, LOCK_PATHS.index(key) + 1, 0, 1, 0, 0, 0, 0, 0, 0))
        return super().stat(path)

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        key = os.fspath(path)
        if key == backuplock.LOCK_TABLE:
            return "\n".join(
                f"{inode}: FLOCK ADVISORY WRITE 1 00:00:{inode} 0 EOF"
                for inode, lock_path in enumerate(LOCK_PATHS, 1) if lock_path in self.held_files
            )
        if key in self.held_files:
            return self.held_files[key]
        return super().read_text(path, encoding=encoding)

    def mkdir(self, path: PathLike, *, mode: int = 0o755, parents: bool = False, exist_ok: bool = False) -> None:
        assert os.fspath(path) == backuplock.LOCK_DIR

    def take_lock(self, path: PathLike, record: str) -> str | None:
        key = os.fspath(path)
        assert key in LOCK_PATHS
        self.takes.append(key)
        if key in self.take_errors:
            raise self.take_errors[key]
        if key in self.held_files:
            return self.held_files[key]
        self.held_files[key] = record
        return None

    def release_lock(self, path: PathLike) -> None:
        key = os.fspath(path)
        self.releases.append(key)
        del self.held_files[key]

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        assert tuple(argv) == window_query_argv(RENDERED)
        assert input is not None and "FROM public.maintenance_window_events" in input
        self.reads += 1
        self.timeouts.append(timeout)
        if self.record_times_out:
            assert timeout is not None
            raise subprocess.TimeoutExpired(argv, timeout)
        return subprocess.CompletedProcess(list(argv), self.record_exit, self.reply, self.record_stderr)


class SyncFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeHost()
        self.frontend = Frontend()
        self.frontend.keys["u-admin"] = ADMIN_KEY


class Sync(SyncFixture):
    def run_sync(self, now: datetime = EVENT.at) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(owui, "ingress_client_factory", return_value=factory_for(self.frontend)) as ingress,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = announcesync.run_announcements_sync(
                argparse.Namespace(), host=self.host, rendered_dir=RENDERED, site_path=SITE_PATH, now=now,
            )
        if ingress.called:
            ingress.assert_called_once_with(HOSTNAME, ca_path=tls.CA_PATH)
        self.assertEqual(self.host.modes, {})
        self.assertNotIn(ADMIN_KEY, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def test_clean_list_is_read_once(self) -> None:
        self.host.reply = ""
        self.assertEqual(self.run_sync(), (0, "standing: ok — none\nbanners: ok — banner list matches\n", ""))
        self.assertEqual(self.host.reads, 1)
        self.assertEqual(self.host.timeouts, [None])
        self.assertEqual(self.frontend.calls, [("GET", BANNERS_ROUTE)])

    def test_sync_adds_replaces_and_removes_banners_and_prints_only_ids(self) -> None:
        self.frontend.banners = [{"id": "moon-cafe", "content": PRIVATE}]
        code, out, err = self.run_sync()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"standing: ok — {BANNER_ID}\nbanners: ok — added: {BANNER_ID}; removed: moon-cafe\n")
        start, end = announcements.office_time(START, ZONE), announcements.office_time(END, ZONE)
        text = f"GIDEON will be unavailable for maintenance from {start} until {end}."
        banner = announcements.Announcement(BANNER_ID, "info", text, dismissible=True)
        self.assertEqual(self.frontend.banners, [{"title": None, **banner_row(banner)}])
        self.assertEqual(self.frontend.calls, [("GET", BANNERS_ROUTE), ("POST", BANNERS_ROUTE), ("GET", BANNERS_ROUTE)])
        self.frontend.calls.clear()
        self.frontend.banners[0]["content"] = PRIVATE
        code, replaced, err = self.run_sync()
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"banners: ok — replaced: {BANNER_ID}", replaced)
        self.assertEqual(self.frontend.banners[0]["content"], text)
        self.frontend.calls.clear()
        code, clean, err = self.run_sync()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("banners: ok — banner list matches", clean)
        self.assertEqual(self.frontend.calls, [("GET", BANNERS_ROUTE)])
        code, expired, err = self.run_sync(now=END)
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"standing: ok — none\nbanners: ok — removed: {BANNER_ID}", expired)
        self.assertEqual(self.frontend.banners, [])
        for output in (out, replaced, clean, expired):
            self.assertNotIn(PRIVATE, output)
            self.assertNotIn(text, output)

    def test_unreadable_record_leaves_frontend_alone(self) -> None:
        banner: dict[str, object] = {"id": "moon-cafe", "content": PRIVATE}
        self.frontend.banners = [banner]
        for exit_status, stderr, expected in (
            (0, "", "reader returned an unreadable event"),
            (3, "psql:<stdin>:5: ERROR:  42P01\n", "event table is missing"),
        ):
            with self.subTest(exit_status=exit_status), patch.object(report, "_installed_form", True):
                self.host.reply, self.host.record_exit, self.host.record_stderr = PRIVATE, exit_status, stderr
                code, out, err = self.run_sync()
                self.assertEqual((code, err), (1, ""))
                [row] = out.splitlines()
                self.assertTrue(row.startswith("standing: refuse"))
                self.assertIn(expected, row)
                self.assertIn("Fix: Run gideon apply, then retry.", row)
                self.assertNotIn(PRIVATE, row)
                self.assertEqual(self.frontend.calls, [])
                self.assertEqual(self.frontend.banners, [banner])

    def test_missing_and_empty_key_have_fixes_and_never_contact_frontend(self) -> None:
        del self.host.files[KEY_PATH]
        with patch.object(report, "_installed_form", True):
            self.assertEqual(self.run_sync(), (
                1, f"standing: ok — {BANNER_ID}\nbanners: refuse — the break-glass API key is missing. "
                "Fix: Run gideon apply, then retry.\n", "",
            ))
            self.assertNotIn(KEY_PATH, self.host.files)
            self.host.files[KEY_PATH] = "\n"
            self.assertEqual(self.run_sync(), (
                1, f"standing: ok — {BANNER_ID}\nbanners: refuse — the break-glass API key is empty. "
                "Fix: Run gideon secrets rotate gideon_admin_api_key, then retry.\n", "",
            ))
        self.assertEqual(self.frontend.calls, [])

    def test_unreachable_or_not_ready_frontend_refuses_with_logs_fix(self) -> None:
        for error in (OSError(PRIVATE), ValueError(PRIVATE), None):
            with self.subTest(error=error), patch.object(
                self.frontend, "handle", side_effect=error, return_value=owui.Response(503, {"detail": PRIVATE}),
            ) as handle:
                code, out, err = self.run_sync()
                self.assertEqual((code, err), (1, ""))
                standing, banners = out.splitlines()
                self.assertEqual(standing, f"standing: ok — {BANNER_ID}")
                self.assertTrue(banners.startswith("banners: refuse"))
                self.assertIn("HTTP 503" if error is None else "banner request failed", banners)
                self.assertIn(f"Fix: {FRONTEND_FIX}", banners)
                self.assertNotIn(PRIVATE, banners)
                handle.assert_called_once_with("GET", BANNERS_ROUTE, None, ADMIN_KEY)

    def test_read_back_mismatch_refuses_without_posted_text(self) -> None:
        self.frontend.swallow_banner_set = True
        self.frontend.banners = [{"id": PRIVATE, "content": PRIVATE}]
        with patch.object(announcements, "command_holder", wraps=announcements.command_holder) as read_holder:
            code, out, err = self.run_sync()
        self.assertEqual((code, err), (1, ""))
        standing, banners = out.splitlines()
        self.assertEqual(standing, f"standing: ok — {BANNER_ID}")
        self.assertIn("banners: refuse — the banner list did not converge", banners)
        self.assertIn("The next announcements sync firing will retry", banners)
        self.assertNotIn(PRIVATE, banners)
        self.assertEqual(self.host.reads, announcesync._SYNC_ROUNDS)
        self.assertEqual(read_holder.call_count, announcesync._SYNC_ROUNDS * 2)
        self.assertEqual(self.frontend.calls.count(("POST", BANNERS_ROUTE)), announcesync._SYNC_ROUNDS)

    def test_root_and_site_refusals_use_the_house_shape(self) -> None:
        self.host.uid = 1000
        for installed, expected in ((True, "gideon"), (False, "python3 -m gideon")):
            with self.subTest(installed=installed), patch.object(report, "_installed_form", installed):
                self.assertEqual(self.run_sync(), (
                    1, "", "gideon announcements sync: root is required. "
                    f"Fix: Run {expected} announcements sync as root, for example with sudo.\n",
                ))
        self.host.uid = 0
        del self.host.files[SITE_PATH]
        loaded = site.load_site(SITE_PATH, host=self.host)
        self.assertEqual(self.run_sync(), (
            1, "", report.refusal("announcements sync", site.render_errors(loaded.errors), "Correct the site file, then retry.") + "\n",
        ))
        self.assertEqual((self.host.reads, self.frontend.calls), (0, []))

    def test_clear_between_holder_read_and_post_removes_the_stale_banner(self) -> None:
        self.host.reply = ""
        self.host.held_files[backuplock.UNAVAILABLE_LOCK.path] = PARENT.to_json()
        handle = self.frontend.handle
        posts: list[object] = []

        def clear_before_post(method: str, path: str, body: object, credential: str | None) -> owui.Response:
            if method == "POST":
                if not posts:
                    self.host.release_lock(backuplock.UNAVAILABLE_LOCK.path)
                posts.append(body)
            return handle(method, path, body, credential)

        with patch.object(self.frontend, "handle", side_effect=clear_before_post):
            code, out, err = self.run_sync()
        banner = command_banner(PARENT)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"standing: ok — none\nbanners: ok — removed: {banner.id}\n")
        self.assertEqual(posts, [{"banners": [banner_row(banner)]}, {"banners": []}])
        self.assertEqual(self.frontend.banners, [])
        self.assertEqual(self.host.reads, 2)

    def test_raise_between_holder_read_and_post_restores_the_banner(self) -> None:
        self.host.reply = ""
        banner = command_banner(PARENT)
        run = self.host.run

        def raise_while_reading_window(
            argv: Command, *, input: str | None, timeout: float | None,
        ) -> subprocess.CompletedProcess[str]:
            if self.host.reads == 0:
                self.host.held_files[backuplock.UNAVAILABLE_LOCK.path] = PARENT.to_json()
                self.frontend.banners = [banner_row(banner)]
            return run(argv, input=input, timeout=timeout)

        with patch.object(self.host, "run", side_effect=raise_while_reading_window):
            code, out, err = self.run_sync()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"standing: ok — {banner.id}\nbanners: ok — added: {banner.id}\n")
        self.assertEqual(self.frontend.banners, [{"title": None, **banner_row(banner)}])
        self.assertEqual(self.host.reads, 2)
        self.assertEqual(self.frontend.calls.count(("POST", BANNERS_ROUTE)), 2)

    def test_raise_and_clear_within_one_round_posts_only_the_snapshot(self) -> None:
        self.host.reply = ""
        run, handle = self.host.run, self.frontend.handle
        posts: list[object] = []

        def raise_while_reading_window(
            argv: Command, *, input: str | None, timeout: float | None,
        ) -> subprocess.CompletedProcess[str]:
            self.host.held_files[backuplock.UNAVAILABLE_LOCK.path] = PARENT.to_json()
            self.frontend.banners = [banner_row(command_banner(PARENT))]
            return run(argv, input=input, timeout=timeout)

        def clear_before_post(method: str, path: str, body: object, credential: str | None) -> owui.Response:
            if method == "POST":
                posts.append(body)
                self.host.release_lock(backuplock.UNAVAILABLE_LOCK.path)
                self.frontend.banners = []
            return handle(method, path, body, credential)

        with (
            patch.object(self.host, "run", side_effect=raise_while_reading_window),
            patch.object(self.frontend, "handle", side_effect=clear_before_post),
        ):
            code, out, err = self.run_sync()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("standing: ok — none", out)
        self.assertEqual(posts, [{"banners": []}])
        self.assertEqual(self.frontend.banners, [])
        self.assertEqual(self.host.reads, 1)

    def test_holder_changing_every_round_exhausts_the_bound(self) -> None:
        self.host.reply = ""
        self.host.held_files[backuplock.UNAVAILABLE_LOCK.path] = PARENT.to_json()
        handle = self.frontend.handle

        def change_holder_after_post(method: str, path: str, body: object, credential: str | None) -> owui.Response:
            response = handle(method, path, body, credential)
            if method == "POST":
                record = replace(PARENT, started=PARENT.started + timedelta(microseconds=self.host.reads))
                self.host.held_files[backuplock.UNAVAILABLE_LOCK.path] = record.to_json()
            return response

        with patch.object(self.frontend, "handle", side_effect=change_holder_after_post):
            code, out, err = self.run_sync()
        self.assertEqual((code, err), (1, ""))
        self.assertIn("banners: refuse — the banner list did not converge", out)
        self.assertIn("The next announcements sync firing will retry", out)
        self.assertEqual(self.host.reads, announcesync._SYNC_ROUNDS)
        self.assertEqual(self.frontend.calls.count(("POST", BANNERS_ROUTE)), announcesync._SYNC_ROUNDS)

    def test_competing_post_retries_with_a_fresh_window_and_holder(self) -> None:
        banner = command_banner(PARENT)
        handle = self.frontend.handle
        for holder_changed in (False, True):
            with self.subTest(holder_changed=holder_changed):
                self.host.reply = ""
                self.host.held_files[backuplock.UNAVAILABLE_LOCK.path] = PARENT.to_json()
                self.host.reads = 0
                self.frontend.banners = []
                self.frontend.calls.clear()

                def compete_after_post(
                    method: str, path: str, body: object, credential: str | None,
                    *, holder_changed: bool = holder_changed,
                ) -> owui.Response:
                    response = handle(method, path, body, credential)
                    if method == "POST" and self.host.reads == 1:
                        self.frontend.banners = []
                        if holder_changed:
                            self.host.release_lock(backuplock.UNAVAILABLE_LOCK.path)
                        else:
                            self.host.reply = reply()
                    return response

                with patch.object(self.frontend, "handle", side_effect=compete_after_post):
                    code, out, err = self.run_sync()
                self.assertEqual((code, err), (0, ""))
                ids = [] if holder_changed else [banner.id, BANNER_ID]
                self.assertIn(f"standing: ok — {', '.join(ids) or 'none'}\n", out)
                self.assertEqual([row["id"] for row in self.frontend.banners], ids)
                self.assertEqual(self.host.reads, 2)
                self.assertEqual(self.frontend.calls.count(("POST", BANNERS_ROUTE)), 1 if holder_changed else 2)

    def test_post_sync_holder_read_failure_refuses_without_retrying(self) -> None:
        problem = report.Problem("fictitious lock table read failure", "Correct the lock table.")
        with patch.object(announcements, "command_holder", side_effect=[backuplock.Holding(False, None), problem]):
            code, out, err = self.run_sync()
        self.assertEqual((code, out, err), (
            1, "standing: refuse — fictitious lock table read failure Fix: Correct the lock table.\n", "",
        ))
        self.assertEqual(self.host.reads, 1)


class Hooks(SyncFixture):
    def setUp(self) -> None:
        super().setUp()
        self.host.reply = ""

    def raise_banner(self) -> tuple[announcesync.RaiseHandle, report.StageResult]:
        return announcesync.raise_announcement(
            self.host, RENDERED, command=CHILD.command, hostname=HOSTNAME, timezone=ZONE,
            now=START, client_factory=factory_for(self.frontend),
        )

    def clear_banner(self, handle: announcesync.RaiseHandle) -> report.StageResult | None:
        return announcesync.clear_announcement(
            self.host, RENDERED, handle, hostname=HOSTNAME, timezone=ZONE,
            now=START, client_factory=factory_for(self.frontend),
        )

    def test_raise_takes_primary_and_clear_releases_it_and_removes_the_banner(self) -> None:
        handle, raised = self.raise_banner()
        self.assertEqual(self.host.takes, [backuplock.UNAVAILABLE_LOCK.path])
        record = backuplock.parse(self.host.held_files[backuplock.UNAVAILABLE_LOCK.path])
        self.assertEqual(record, backuplock.Record(CHILD.command, os.getpid(), START))
        assert record is not None
        banner = command_banner(record)
        self.assertEqual(raised, report.StageResult("announce", True, f"raised {banner.id}", ""))
        self.assertEqual(self.frontend.banners, [{"title": None, **banner_row(banner)}])

        self.assertEqual(self.clear_banner(handle), report.StageResult("clear", True, f"cleared {banner.id}", ""))
        self.assertEqual(self.frontend.banners, [])
        self.assertEqual(self.host.releases, [backuplock.UNAVAILABLE_LOCK.path])
        self.assertEqual(self.host.held_files, {})

    def test_interrupt_during_the_raise_read_back_takes_its_banner_down(self) -> None:
        handle = self.frontend.handle
        posted = False

        def interrupt_read_back(method: str, path: str, body: object, credential: str | None) -> owui.Response:
            nonlocal posted
            if posted and method == "GET" and self.host.held_files:
                raise KeyboardInterrupt
            response = handle(method, path, body, credential)
            posted = posted or method == "POST"
            return response

        with (
            patch.object(self.frontend, "handle", side_effect=interrupt_read_back),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.raise_banner()

        self.assertTrue(posted)
        self.assertEqual(self.host.releases, [backuplock.UNAVAILABLE_LOCK.path])
        self.assertEqual(self.host.held_files, {})
        self.assertEqual(self.frontend.banners, [])

    def test_nested_raise_follows_parent_and_clear_releases_only_nested(self) -> None:
        self.host.held_files[backuplock.UNAVAILABLE_LOCK.path] = PARENT.to_json()
        banner = command_banner(PARENT)
        handle, raised = self.raise_banner()
        self.assertEqual(self.host.takes, list(LOCK_PATHS))
        self.assertEqual(raised, report.StageResult("announce", True, f"announcement stands under {PARENT.command}", ""))
        self.assertEqual(self.frontend.banners, [{"title": None, **banner_row(banner)}])

        self.assertEqual(self.clear_banner(handle), report.StageResult(
            "clear", True, f"announcement still stands under {PARENT.command}", "",
        ))
        self.assertEqual(self.host.releases, [backuplock.UNAVAILABLE_NESTED_LOCK.path])
        self.assertEqual(self.host.held_files, {backuplock.UNAVAILABLE_LOCK.path: PARENT.to_json()})
        self.assertEqual(self.frontend.banners, [{"title": None, **banner_row(banner)}])

    def test_both_locks_held_yields_ok_and_no_clear(self) -> None:
        self.host.held_files.update(zip(LOCK_PATHS, (PARENT.to_json(), CHILD.to_json()), strict=True))
        original = dict(self.host.held_files)
        handle, row = self.raise_banner()
        self.assertEqual(self.host.takes, list(LOCK_PATHS))
        self.assertEqual(row, report.StageResult("announce", True, "an announcement already stands", ""))
        self.assertIsNone(self.clear_banner(handle))
        self.assertEqual(self.host.held_files, original)
        self.assertEqual(self.host.releases, [])
        self.assertEqual(self.host.reads, 0)
        self.assertEqual(self.frontend.calls, [])

    def test_take_error_yields_ok_and_no_clear(self) -> None:
        for lock in (backuplock.UNAVAILABLE_LOCK, backuplock.UNAVAILABLE_NESTED_LOCK):
            with self.subTest(lock=lock):
                self.host.take_errors = {lock.path: OSError("fictitious lock failure")}
                if lock == backuplock.UNAVAILABLE_NESTED_LOCK:
                    self.host.held_files[backuplock.UNAVAILABLE_LOCK.path] = PARENT.to_json()
                handle, row = self.raise_banner()
                self.assertEqual(row, report.StageResult(
                    "announce", True, f"the {lock.noun} lock could not be taken", "",
                ))
                self.assertIsNone(self.clear_banner(handle))
                self.assertEqual(self.host.releases, [])
                self.assertEqual(self.host.reads, 0)
                self.assertEqual(self.frontend.calls, [])

    def test_expired_window_read_leaves_raise_ok_and_holds_the_lock(self) -> None:
        self.host.record_times_out = True
        handle, row = self.raise_banner()
        self.assertEqual(row, report.StageResult(
            "announce", True, "lock held, announcement not shown yet: maintenance window reader failed: command could not run", "",
        ))
        self.assertIn(backuplock.UNAVAILABLE_LOCK.path, self.host.held_files)
        self.assertEqual(self.host.timeouts, [announcesync._HOOK_TIMEOUT])
        self.assertEqual(self.frontend.calls, [])
        self.host.record_times_out = False
        self.assertIsNotNone(self.clear_banner(handle))
        self.assertEqual(self.host.held_files, {})

    def test_expired_frontend_request_leaves_clear_ok_and_defers_to_timer(self) -> None:
        handle, _ = self.raise_banner()
        problem = "fictitious request timed out"
        # The production factory is built over the system store, so no box CA file is read.
        with (
            patch.object(tls, "CA_PATH", None),
            patch.object(owui.Client, "banners", autospec=True, side_effect=owui.OwuiTimeout(problem)) as banners,
        ):
            row = announcesync.clear_announcement(
                self.host, RENDERED, handle, hostname=HOSTNAME, timezone=ZONE, now=START,
            )
        self.assertEqual(row, report.StageResult(
            "clear", True, f"lock released, banner left to the next announcements sync firing: {problem}", "",
        ))
        banners.assert_called_once()
        (client,) = banners.call_args.args
        self.assertEqual(client._timeout, announcesync._HOOK_TIMEOUT)
        self.assertEqual(self.host.timeouts, [announcesync._HOOK_TIMEOUT] * 2)
        self.assertEqual(self.host.held_files, {})
        self.assertEqual(self.host.releases, [backuplock.UNAVAILABLE_LOCK.path])
        self.assertEqual(len(self.frontend.banners), 1)


class ModuleShape(unittest.TestCase):
    def test_sync_path_has_no_function_level_imports(self) -> None:
        host = Path(__file__).resolve().parents[1] / "gideon" / "host"
        for name in ("announcesync", "announcements", "windowrecord", "owui", "secrets", "audit", "stack", "tls", "backuplock"):
            path = host / f"{name}.py"
            with self.subTest(module=name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                nested = [
                    node.lineno for node in ast.walk(tree)
                    if isinstance(node, (ast.Import, ast.ImportFrom)) and node not in tree.body
                ]
                self.assertEqual(nested, [], "Upgrade clears the announcement after checkout. Load imports at module level.")
