"""Command forms in backup and restore fixes and next steps."""

import argparse
import contextlib
import io
import os
import unittest
from dataclasses import replace
from unittest.mock import patch

import test_backup
import test_backup_push
import test_backupset
import test_drill
import test_restore

from gideon import cli
from gideon.host import (
    backup,
    backuproots,
    backupset,
    drill,
    pgbackrest,
    report,
    restore,
)
from gideon.host.report import Problem
from gideon.host.sysio import RealHost


class BackupForms(unittest.TestCase):
    """A fix uses the form selected for the current CLI run."""

    def setUp(self) -> None:
        self.addCleanup(report.set_form_from_environment, os.environ.copy())

    def test_sentences_keep_their_long_form_words(self) -> None:
        def problem_fix(value: backupset.SetRef | Problem) -> str:
            assert isinstance(value, Problem)
            return value.fix

        def next_line(has_gideon_ids: bool) -> str:
            selected = backupset.SetRef(
                test_restore.LOCAL_SET,
                backupset.set_dir(test_restore.LOCAL_SET),
                test_restore.manifest().finished,
                True,
                test_restore.manifest(),
            )
            _, lines = restore._next_stage(
                test_restore.make_host(),
                selected,
                build_box=False,
                has_gideon_ids=has_gideon_ids,
                closing_lines=(),
            )
            return lines[-1]

        cases = (
            ("rehash", backupset._rehash_fix, "Re-run sudo python3 -m gideon backup run to regenerate the inventory hashes."),
            ("no set", lambda: problem_fix(backupset.select_set(backupset.list_sets(test_backupset.FakeHost()))), "Run sudo python3 -m gideon backup run, then retry."),
            ("unknown label", lambda: problem_fix(backupset.select_set_by_label(backupset.list_sets(test_backupset.FakeHost()), "pre-missing")), "Choose one of them, then retry sudo python3 -m gideon restore --from staging --set <label>."),
            ("root", restore._root_fix, "Run sudo python3 -m gideon restore --from <staging|target>, then retry."),
            ("apply", restore._apply_fix, "Run sudo python3 -m gideon apply, then retry."),
            ("set", restore._set_fix, "Run sudo python3 -m gideon backup run, then retry."),
            ("account", restore._account_fix, "Run sudo python3 -m gideon host provision, then retry."),
            ("push", restore._push_fix, "Run sudo python3 -m gideon backup push, then retry."),
            ("stage", restore._stage_fix, "Run sudo python3 -m gideon apply, then retry."),
            ("partial", restore._partial_fix, "Start the whole stack with sudo python3 -m gideon apply, or stop it with docker compose -f /etc/gideon/rendered/compose.yaml down, then retry restore."),
            ("incomplete staging", restore._incomplete_staging_fix, "Run sudo python3 -m gideon backup run to complete a set, or on a rebuilt box stop the stack with docker compose -f /etc/gideon/rendered/compose.yaml down, then retry restore."),
            ("next with ids", lambda: next_line(True), "Next: sudo python3 -m gideon apply, then sudo python3 -m gideon backup run --full"),
            ("next without ids", lambda: next_line(False), "Next: sudo python3 -m gideon host provision (the first provision's mode flags), because this set records no gideon ids, then sudo python3 -m gideon apply, then sudo python3 -m gideon backup run --full"),
            ("backup root", backup._root_fix, "Run sudo python3 -m gideon backup run as root, then retry."),
            ("backup apply", backup._apply_fix, "Run sudo python3 -m gideon apply, then retry."),
            ("backup tools", backup._tools_fix, "Run sudo python3 -m gideon host provision --only host-tools, then retry."),
            ("backup recipient", backup._recipient_fix, "Run sudo python3 -m gideon host provision --only age-recipient, then retry."),
            ("backup identity", backup._identity_fix, "Run sudo python3 -m gideon host provision --only age-identity, then retry."),
            ("backup account", backup._account_fix, "Run sudo python3 -m gideon host provision, then retry."),
            ("backup stage", backup._stage_fix, "Run sudo python3 -m gideon apply, then retry."),
            ("push root", backup._push_root_fix, "Run sudo python3 -m gideon backup push as root, then retry."),
            ("push set", backup._push_set_fix, "Run sudo python3 -m gideon backup run, then retry."),
            ("push tools", backup._push_tools_fix, "Run sudo python3 -m gideon host provision --only host-tools, then retry."),
            ("push stage", backup._push_stage_fix, "Run sudo python3 -m gideon backup push, then retry."),
            ("push check", backup._push_check_fix, "Re-run sudo python3 -m gideon backup push --verify-all; if it fails again, the target's copy is corrupt — check the target's disk per docs/runbooks/office-services-setup.md §3."),
            ("drill root", drill._root_fix, "Run sudo python3 -m gideon backup drill as root, then retry."),
            ("drill apply", drill._apply_fix, "Run sudo python3 -m gideon apply, then retry."),
            ("drill tools", drill._tools_fix, "Run sudo python3 -m gideon host provision --only host-tools, then retry."),
            ("drill set", drill._set_fix, "Run sudo python3 -m gideon backup run, then retry."),
            ("drill age", drill._age_fix, "Run sudo python3 -m gideon backup run, then retry."),
            ("physical", backuproots._physical_fix, "The fetched or restored tree does not match its manifest; re-run sudo python3 -m gideon backup push --verify-all on the source box, then retry restore."),
            ("stanza mismatch", pgbackrest._stanza_mismatch_fix, "Run sudo python3 -m gideon restore --from staging to restore the cluster this repository belongs to, or move /data/backup-staging/pgbackrest aside, then re-run apply."),
        )
        for installed in (False, True):
            report.set_installed_form(installed)
            for name, render, long_sentence in cases:
                with self.subTest(installed=installed, sentence=name):
                    expected = (
                        long_sentence.replace("sudo python3 -m gideon", "gideon")
                        if installed else long_sentence
                    )
                    self.assertEqual(render(), expected)

    def test_restore_entry_reads_both_forms_before_box_access(self) -> None:
        # The installed wrapper re-executes as root; its non-root refusal is
        # reachable only through this in-process entry test.
        with (
            patch.object(RealHost, "geteuid", return_value=1000) as geteuid,
            patch.object(RealHost, "read_text", side_effect=AssertionError("box read")) as read_text,
            patch.object(RealHost, "exists", side_effect=AssertionError("box exists")) as exists,
            patch.object(RealHost, "listdir", side_effect=AssertionError("box list")) as listdir,
            patch.object(RealHost, "run", side_effect=AssertionError("box command")) as run,
        ):
            for installed in (True, False):
                with self.subTest(installed=installed):
                    environment = (
                        {report.GIDEON_INSTALLED_COMMAND: "/fictitious/gideon"}
                        if installed else {}
                    )
                    out, err = io.StringIO(), io.StringIO()
                    with (
                        patch.dict(os.environ, environment, clear=True),
                        contextlib.redirect_stdout(out),
                        contextlib.redirect_stderr(err),
                    ):
                        code = cli.main(["restore", "--from", "staging"])
                    command = "gideon" if installed else "sudo python3 -m gideon"
                    self.assertEqual(code, 1)
                    self.assertEqual(out.getvalue(), "")
                    self.assertEqual(err.getvalue(), f"gideon restore: root is required. Fix: Run {command} restore --from <staging|target>, then retry.\n")
            self.assertEqual(geteuid.call_count, 2)
            read_text.assert_not_called()
            exists.assert_not_called()
            listdir.assert_not_called()
            run.assert_not_called()

    def test_backup_entries_read_both_forms_before_box_access(self) -> None:
        # The installed wrapper re-executes as root, so these non-root
        # refusals are reachable in the installed form only in-process.
        cases = (
            (["backup", "run"], "backup run"),
            (["backup", "push"], "backup push"),
            (["backup", "drill"], "backup drill"),
        )
        with (
            patch.object(RealHost, "geteuid", return_value=1000) as geteuid,
            patch.object(RealHost, "read_text", side_effect=AssertionError("box read")) as read_text,
            patch.object(RealHost, "exists", side_effect=AssertionError("box exists")) as exists,
            patch.object(RealHost, "listdir", side_effect=AssertionError("box list")) as listdir,
            patch.object(RealHost, "run", side_effect=AssertionError("box command")) as run,
        ):
            for installed in (True, False):
                environment = (
                    {report.GIDEON_INSTALLED_COMMAND: "/fictitious/gideon"}
                    if installed else {}
                )
                command = "gideon" if installed else "sudo python3 -m gideon"
                for argv, path in cases:
                    with self.subTest(installed=installed, path=path):
                        out, err = io.StringIO(), io.StringIO()
                        with (
                            patch.dict(os.environ, environment, clear=True),
                            contextlib.redirect_stdout(out),
                            contextlib.redirect_stderr(err),
                        ):
                            code = cli.main(argv)
                        self.assertEqual(code, 1)
                        self.assertEqual(out.getvalue(), "")
                        self.assertEqual(
                            err.getvalue(),
                            f"gideon {path}: root is required. Fix: Run {command} {path} as root, then retry.\n",
                        )
            self.assertEqual(geteuid.call_count, 6)
            read_text.assert_not_called()
            exists.assert_not_called()
            listdir.assert_not_called()
            run.assert_not_called()

    def test_backup_run_push_and_drill_rows_follow_the_form(self) -> None:
        for installed in (False, True):
            report.set_installed_form(installed)
            command = "gideon" if installed else "sudo python3 -m gideon"

            with self.subTest(installed=installed, row="backup precondition"):
                backup_host = test_backup._host()
                backup_host.files.pop(f"{test_backup.RENDERED}/compose.yaml")
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = backup.run_backup_run(
                        argparse.Namespace(), host=backup_host, now=test_backup.NOW
                    )
                self.assertEqual(code, 1)
                self.assertEqual(out.getvalue(), "")
                self.assertEqual(
                    err.getvalue(),
                    f"gideon backup run: rendered Compose file is missing: {test_backup.RENDERED}/compose.yaml. Fix: Run {command} apply, then retry.\n",
                )

            with self.subTest(installed=installed, row="push off-box check"):
                push_host = test_backup_push.host(check_output="pgbackrest/file: FAILED\n")
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = backup.run_backup_push(
                        argparse.Namespace(verify_all=False),
                        host=push_host,
                        now=test_backup_push.NOW,
                    )
                self.assertEqual(code, 1)
                check_row = next(
                    line for line in out.getvalue().splitlines() if line.startswith("check:")
                )
                self.assertIn("check: refuse —", check_row)
                self.assertIn(
                    f"Fix: Re-run {command} backup push --verify-all; if it fails again, the target's copy is corrupt — check the target's disk per docs/runbooks/office-services-setup.md §3.",
                    check_row,
                )
                if installed:
                    self.assertNotIn("python3 -m gideon", out.getvalue())
                else:
                    self.assertIn("sudo python3 -m gideon", out.getvalue())

            with self.subTest(installed=installed, row="drill missing set"):
                drill_host = test_drill.FakeHost()
                drill_host.files.pop(
                    os.path.join(
                        backupset.set_dir(test_drill.SET_LABEL), backupset.MANIFEST_NAME
                    )
                )
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = test_drill.run(drill_host)
                self.assertEqual(code, 1)
                self.assertEqual(out.getvalue(), "")
                self.assertEqual(
                    err.getvalue(),
                    f"gideon backup drill: no complete local backup set is available. Fix: Run {command} backup run, then retry.\n",
                )

    def test_select_refusal_and_both_next_lines_follow_the_form(self) -> None:
        for installed in (False, True):
            report.set_installed_form(installed)
            command = "gideon" if installed else "sudo python3 -m gideon"
            with self.subTest(installed=installed, row="select"):
                fake = test_restore.make_host()
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = restore.run_restore(
                        argparse.Namespace(source="staging", at=None, set="pre-missing"),
                        host=fake,
                        now=test_restore.NOW,
                    )
                self.assertEqual(code, 1)
                self.assertEqual(
                    out.getvalue(),
                    f"select: refuse — No complete backup set is named pre-missing; the complete sets are: {test_restore.LOCAL_SET}. Fix: Choose one of them, then retry {command} restore --from staging --set <label>.\n",
                )
                self.assertFalse(any(call[0][-1] == "down" for call in fake.calls))
            for has_ids in (True, False):
                with self.subTest(installed=installed, has_ids=has_ids):
                    value = test_restore.manifest()
                    if has_ids:
                        value = replace(value, gideon_ids=test_restore.DEFAULT_GIDEON_IDS)
                    fake = test_restore.make_host(manifest_value=value)
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        code = restore.run_restore(
                            argparse.Namespace(source="staging", at=None),
                            host=fake,
                            now=test_restore.NOW,
                        )
                    self.assertEqual(code, 0, out.getvalue())
                    next_lines = [line for line in out.getvalue().splitlines() if line.startswith("Next:")]
                    expected = (
                        f"Next: {command} apply, then {command} backup run --full"
                        if has_ids else
                        f"Next: {command} host provision (the first provision's mode flags), because this set records no gideon ids, then {command} apply, then {command} backup run --full"
                    )
                    self.assertEqual(next_lines, [expected])
                    if installed:
                        self.assertNotIn("python3 -m gideon", out.getvalue())
                    else:
                        self.assertIn("sudo python3 -m gideon", out.getvalue())
