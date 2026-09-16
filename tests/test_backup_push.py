"""Backup-push stages over the injectable Host seam (spec §19.1, ADR-0026)."""

import argparse
import contextlib
import io
import os
import subprocess
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from gideon.host import backup, backupset, site, sshtarget, stack
from gideon.host.steps.site_dirs import AGE_RECIPIENT_PATH
from gideon.host.sysio import Command, PathLike

RENDERED = "/etc/gideon/rendered"
SITE_PATH = "/etc/gideon/site.yaml"
LOCAL_SET = "20260902T110000Z"
PUSH_LABEL = "20260902T120000Z"
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
RECIPIENT = "age1" + "a" * 58

SITE = """\
office:
  name: Test Office
  short_name: TEST
  timezone: Etc/UTC
hostname: gideon.test
lan_cidrs: [192.0.2.0/24]
jurisdiction:
  circuit: ca6
  districts: [tnmd]
  states: [TN]
auth:
  ldap:
    host: ad.test
backup:
  target:
    host: nas.test
    path: /backup/snapshots
alerts:
  smtp:
    host: smtp.test
    from: gideon@test
  recipients: [csa@test]
"""


def result(
    argv: Sequence[str], *, returncode: int = 0, stdout: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), returncode, stdout, "")


def entry(path: str, *, digest: str = "a" * 64) -> backupset.Entry:
    return backupset.Entry(path, "f", 10, 1000, 1000, 0o644, 100.0, digest)


def manifest(*, file_count: int = 1) -> backupset.Manifest:
    repository: tuple[backupset.Entry, ...] = (
        entry("backup/gideon/backup.info", digest="b" * 64),
        entry("archive/gideon/archive.info", digest="c" * 64),
        entry(
            "backup/gideon/new-full/backup.manifest", digest="d" * 64
        ),
    )
    if file_count > 1:
        repository += tuple(
            entry(f"blob-{index:03d}", digest=f"{index + 1:064x}")
            for index in range(file_count)
        )
    return backupset.Manifest(
        1,
        LOCAL_SET,
        backupset.Kind.NIGHTLY,
        NOW - timedelta(hours=2),
        NOW - timedelta(hours=1),
        "0.0.18",
        "/work/GIDEON",
        "commit",
        "gideon.test",
        None,
        "new-full",
        "full",
        NOW - timedelta(hours=1),
        {"gideon": {"public.users": 3}, "openwebui": {}},
        {
            "etc-gideon": (entry("config.yaml"),),
            "checkout": (entry("gideon/__init__.py"),),
            "data-registry": (entry("image.blob"),),
            "data-bulk-openwebui": (entry("uploads/data.bin"),),
            "pgbackrest": repository,
        },
        "e" * 64,
        (RECIPIENT,),
        backupset.LinkVerdict(1, 1),
        "f" * 64,
    )


class FakeHost:
    """A dict-backed Host that records argv, stdin, and writes."""

    def __init__(
        self,
        *,
        files: Mapping[str, str],
        names: Sequence[str] = (),
        remote_listing: str = "",
        remote_manifest: str | None = None,
        stats: str = "Total file size: 100 bytes\nTotal transferred file size: 10 bytes\n",
        check_output: str | None = None,
        euid: int = 0,
        fail_audit: bool = False,
        fail_target: bool = False,
    ) -> None:
        self.files = dict(files)
        self.names = list(names)
        self.remote_listing = remote_listing
        self.remote_manifest = remote_manifest or manifest().to_json()
        self.stats = stats
        self.check_output = check_output
        self.euid = euid
        self.fail_audit = fail_audit
        self.fail_target = fail_target
        self.calls: list[tuple[tuple[str, ...], str | None, float | None]] = []
        self.writes: list[str] = []

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
        del check, cwd, env
        command = tuple(argv)
        self.calls.append((command, input, timeout))
        if command and command[0] == "docker" and "gideon_audit" in command:
            return result(command, returncode=int(self.fail_audit))
        if command and command[0] == "rsync":
            return result(command, stdout=self.stats)
        if command and command[0] == "ssh":
            if command[-1] == "true":
                return result(command, returncode=int(self.fail_target))
            remote_word = command[-1]
            if "for directory in */" in remote_word:
                return result(command, stdout=self.remote_listing)
            if len(command) >= 2 and command[-2] == "cat":
                return result(command, stdout=self.remote_manifest or "")
            if "sha256sum -c -" in remote_word:
                output = self.check_output
                if output is None:
                    output = "".join(
                        line.split("  ", 1)[1].rstrip("\n") + ": OK\n"
                        for line in (input or "").splitlines(True)
                    )
                return result(
                    command,
                    returncode=int(": FAILED" in output),
                    stdout=output,
                )
            return result(command)
        return result(command)

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(
        self,
        path: PathLike,
        text: str,
        *,
        encoding: str = "utf-8",
        mode: int = 0o644,
    ) -> None:
        del encoding, mode
        key = os.fspath(path)
        self.files[key] = text
        self.writes.append(key)

    def exists(self, path: PathLike) -> bool:
        return os.fspath(path) in self.files

    def listdir(self, path: PathLike) -> list[str]:
        if os.fspath(path) == backupset.SETS_DIR:
            return list(self.names)
        raise FileNotFoundError(os.fspath(path))

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del missing_ok
        self.files.pop(os.fspath(path), None)

    def stat(self, path: PathLike) -> os.stat_result:
        raise FileNotFoundError(os.fspath(path))

    def chmod(self, path: PathLike, mode: int) -> None:
        del path, mode

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        del path, uid, gid

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del path, mode, parents, exist_ok

    def geteuid(self) -> int:
        return self.euid


def host(
    *,
    names: Sequence[str] = (LOCAL_SET,),
    remote_listing: str = "",
    remote_manifest: str | None = None,
    stats: str | None = None,
    check_output: str | None = None,
    euid: int = 0,
    fail_audit: bool = False,
    fail_target: bool = False,
    local_set_manifest: backupset.Manifest | None = None,
) -> FakeHost:
    value = local_set_manifest or manifest()
    files = {
        SITE_PATH: SITE,
        os.fspath(AGE_RECIPIENT_PATH): RECIPIENT + "\n",
        "/usr/bin/rsync": "",
        os.path.join(
            backupset.set_dir(value.label), backupset.MANIFEST_NAME
        ): value.to_json(),
    }
    return FakeHost(
        files=files,
        names=names,
        remote_listing=remote_listing,
        remote_manifest=remote_manifest,
        stats=stats
        or "Total file size: 100 bytes\nTotal transferred file size: 10 bytes\n",
        check_output=check_output,
        euid=euid,
        fail_audit=fail_audit,
        fail_target=fail_target,
    )


class Preconditions(unittest.TestCase):
    def test_refusals_name_their_fixes(self) -> None:
        cases = (
            ("root", host(euid=1000), "backup push as root"),
            ("site", host(), "Correct the site file"),
            ("rsync", host(), "host provision --only host-tools"),
            ("local", host(), "backup run"),
            ("audit", host(fail_audit=True), "logs postgres"),
            ("target", host(fail_target=True), "office-services runbook §3"),
        )
        cases[1][1].files.pop(SITE_PATH)
        cases[2][1].files.pop("/usr/bin/rsync")
        cases[3][1].files.pop(
            os.path.join(backupset.set_dir(LOCAL_SET), backupset.MANIFEST_NAME)
        )
        for name, fake, expected in cases:
            with self.subTest(name=name):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = backup.run_backup_push(
                        argparse.Namespace(verify_all=False), host=fake, now=NOW
                    )
                self.assertEqual(code, 1)
                self.assertIn(expected, err.getvalue())


class PushContracts(unittest.TestCase):
    def test_listing_parser_and_script(self) -> None:
        parsed = backup._parse_remote_listing(
            "20260901T120000Z\t1\n"
            "20260901T110000Z.partial\t1\n"
            "not-a-label\t1\n"
            "20260901T100000Z\t0\n"
        )
        self.assertIsInstance(parsed, tuple)
        assert isinstance(parsed, tuple)
        self.assertEqual(
            [(item.label, item.complete) for item in parsed],
            [
                ("20260901T120000Z", True),
                ("20260901T110000Z.partial", False),
                ("not-a-label", False),
                ("20260901T100000Z", False),
            ],
        )
        fake = host()
        loaded_site = site.load_site(SITE_PATH, host=fake)
        assert loaded_site.config is not None
        script = backup._remote_listing_script(loaded_site.config)
        self.assertIn("cd /backup/snapshots", script)
        self.assertIn("push.json", script)
        self.assertIn("printf", script)

    def test_push_record_rsync_finalize_and_check(self) -> None:
        fake = host()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = backup.run_backup_push(
                argparse.Namespace(verify_all=False), host=fake, now=NOW
            )
        self.assertEqual(code, 0)
        self.assertEqual(
            [line.split(":", 1)[0] for line in out.getvalue().splitlines()],
            ["record", "list", "push", "finalize", "prune", "check", "audit"],
        )
        record_path = os.path.join(backupset.STAGING, backupset.PUSH_RECORD_NAME)
        parsed = backupset.parse_push_record(fake.files[record_path])
        self.assertIsInstance(parsed, backupset.PushRecord)
        assert isinstance(parsed, backupset.PushRecord)
        self.assertEqual(parsed.label, PUSH_LABEL)
        self.assertEqual(parsed.newest_set, LOCAL_SET)
        rsync = next(call[0] for call in fake.calls if call[0][0] == "rsync")
        self.assertEqual(
            rsync,
            (
                "rsync",
                "-aH",
                "--no-owner",
                "--no-group",
                "--delete",
                "--stats",
                "-e",
                sshtarget.rsync_ssh_option(),
                "/data/backup-staging/",
                "gideon-backup@nas.test:/backup/snapshots/20260902T120000Z.partial/",
            ),
        )
        finalize = [
            call[0]
            for call in fake.calls
            if call[0][0] == "ssh" and "mv --" in call[0][-1]
        ]
        self.assertEqual(len(finalize), 1)
        check_call = next(
            call for call in fake.calls if call[0][0] == "ssh" and "sha256sum -c -" in call[0][-1]
        )
        expected = check_call[1]
        self.assertIsNotNone(expected)
        assert expected is not None
        self.assertIn("sets/20260902T110000Z/manifest.json", expected)
        self.assertIn("push.json", expected)
        self.assertIn("sets/20260902T110000Z/secrets.tar.age", expected)
        self.assertIn("pgbackrest/backup/gideon/backup.info", expected)
        self.assertIn("pgbackrest/archive/gideon/archive.info", expected)
        self.assertIn("pgbackrest/backup/gideon/new-full/backup.manifest", expected)
        self.assertNotIn(expected, " ".join(check_call[0]))
        audit_inputs = [
            call[1]
            for call in fake.calls
            if call[0] == tuple(
                stack.exec_argv(
                    RENDERED,
                    "postgres",
                    "psql",
                    "-U",
                    "gideon_audit",
                    "-d",
                    "gideon",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "--single-transaction",
                    "-f",
                    "-",
                )
            )
            and call[1] is not None
        ]
        self.assertTrue(any("backup_push" in value for value in audit_inputs if value))
        self.assertTrue(any('"verify_all": false' in value for value in audit_inputs if value))

    def test_link_dest_and_full_copy_failure_only_follow_a_previous_snapshot(self) -> None:
        listing = "20260901T120000Z\t1\n"
        failed = host(
            remote_listing=listing,
            stats="Total file size: 1,000 bytes\nTotal transferred file size: 901 bytes\n",
        )
        # The local set is present, while the remote listing makes this a second push.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = backup.run_backup_push(
                argparse.Namespace(verify_all=False), host=failed, now=NOW
            )
        self.assertEqual(code, 1)
        push_row = next(line for line in out.getvalue().splitlines() if line.startswith("push:"))
        self.assertIn("901", push_row)
        self.assertIn("1000", push_row)
        self.assertIn("one filesystem", push_row)
        rsync = next(call[0] for call in failed.calls if call[0][0] == "rsync")
        self.assertIn("--link-dest=../20260901T120000Z", rsync)
        self.assertFalse(any(call[0][0] == "ssh" and "mv --" in call[0][-1] for call in failed.calls))

        first = host(
            remote_listing="",
            stats="Total file size: 1,000 bytes\nTotal transferred file size: 901 bytes\n",
        )
        self.assertEqual(
            backup.run_backup_push(
                argparse.Namespace(verify_all=False), host=first, now=NOW
            ),
            0,
        )

    def test_prune_names_keep_fresh_and_current_snapshots(self) -> None:
        old = backupset.RemoteSnapshot("20260801T120000Z", None, True)
        stale = backupset.RemoteSnapshot("20260901T110000Z.partial", None, False)
        fresh = backupset.RemoteSnapshot("20260902T110000Z.partial", None, False)
        current = backupset.RemoteSnapshot(PUSH_LABEL, None, True)
        self.assertEqual(
            backup._push_prune_names(
                (old, stale, fresh, current),
                now=NOW,
                remote_days=30,
                current_label=PUSH_LABEL,
            ),
            ("20260801T120000Z", "20260901T110000Z.partial"),
        )
        fake = host()
        loaded = site.load_site(SITE_PATH, host=fake)
        assert loaded.config is not None
        self.assertIsNone(
            backup._safe_remote_snapshot_path(loaded.config, PUSH_LABEL, PUSH_LABEL)
        )
        self.assertIsNone(
            backup._safe_remote_snapshot_path(loaded.config, "/tmp/evil", "other")
        )
        stage, removed = backup._push_prune_stage(
            fake,
            loaded.config,
            snapshots=(old, stale, fresh, current),
            now=NOW,
            current_label=PUSH_LABEL,
        )
        self.assertTrue(stage.ok)
        self.assertEqual(removed, 2)
        removed_commands = [
            call[0][-1]
            for call in fake.calls
            if call[0][0] == "ssh" and "rm -rf --" in call[0][-1]
        ]
        self.assertEqual(len(removed_commands), 2)
        self.assertTrue(all("20260902T110000Z" not in command for command in removed_commands))

    def test_failed_checksum_row_names_only_the_count(self) -> None:
        fake = host(check_output="pgbackrest/file: FAILED\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = backup.run_backup_push(
                argparse.Namespace(verify_all=False), host=fake, now=NOW
            )
        self.assertEqual(code, 1)
        check_row = next(line for line in out.getvalue().splitlines() if line.startswith("check:"))
        self.assertIn("1 path(s)", check_row)
        self.assertNotIn("pgbackrest/file", check_row)

    def test_verify_all_sends_every_inventory_file(self) -> None:
        value = manifest(file_count=500)
        fake = host(local_set_manifest=value)
        local_set = backupset.SetRef(
            LOCAL_SET,
            backupset.set_dir(LOCAL_SET),
            value.finished,
            True,
            value,
        )
        expected = backup._push_check_input(
            fake,
            local_set=local_set,
            remote_manifest=value,
            record_text=backupset.PushRecord(
                PUSH_LABEL, NOW, LOCAL_SET, value.archive_through
            ).to_json(),
            verify_all=True,
        )
        self.assertIsInstance(expected, str)
        assert isinstance(expected, str)
        blob_lines = [line for line in expected.splitlines() if "pgbackrest/blob-" in line]
        self.assertEqual(len(blob_lines), 500)

    def test_check_fails_when_the_inventory_lacks_a_mandatory_repository_file(self) -> None:
        value = manifest()
        thin = replace(value, inventory={**value.inventory, "pgbackrest": tuple(item for item in value.inventory["pgbackrest"] if not item.path.endswith("archive.info"))})
        local_set = backupset.SetRef(LOCAL_SET, backupset.set_dir(LOCAL_SET), thin.finished, True, thin)
        outcome = backup._push_check_input(
            host(),
            local_set=local_set,
            remote_manifest=thin,
            record_text="{}",
            verify_all=False,
        )
        self.assertIsInstance(outcome, backup.StageResult)
        assert isinstance(outcome, backup.StageResult)
        self.assertFalse(outcome.ok)
        self.assertIn("archive/gideon/archive.info", outcome.detail)

    def test_rsync_stats_strip_commas(self) -> None:
        parsed = backup._parse_rsync_stats(
            "Total file size: 1,234,567 bytes\n"
            "Total transferred file size: 987,654 bytes\n"
        )
        self.assertEqual(parsed, backup._PushStats(1_234_567, 987_654))


if __name__ == "__main__":
    unittest.main()
