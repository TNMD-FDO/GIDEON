"""Backup-push stages over the injectable Host seam."""

import argparse
import contextlib
import io
import json
import os
import shlex
import subprocess
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest import mock

from gideon.host import backup, backuplock, backupset, site, sshtarget, stack
from gideon.host.steps.site_dirs import AGE_RECIPIENT_PATH
from gideon.host.sysio import Command, PathLike

RENDERED = "/etc/gideon/rendered"
SITE_PATH = "/etc/gideon/site.yaml"
LOCAL_SET = "20260902T110000Z"
PUSH_LABEL = "20260902T120000Z"
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
RECIPIENT = "age1" + "a" * 58
OLD_SNAPSHOTS = ("20260701T120000Z", "20260702T120000Z", "20260703T120000Z")
OLD_LISTING = "".join(f"{name}\t1\n" for name in OLD_SNAPSHOTS)

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


class Clock:
    """A monotonic clock advanced only by the fake host's operations."""

    def __init__(self, seconds: float = 1000.0) -> None:
        self.seconds = seconds

    def __call__(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> None:
        self.seconds += seconds


class FakeHost:
    """A dict-backed Host that records argv, stdin, and writes."""

    def __init__(
        self,
        *,
        files: Mapping[str, str],
        names: Sequence[str] = (),
        remote_listing: str = "",
        remote_manifest: str | None = None,
        remote_record: str | None = None,
        stats: str = "Total file size: 100 bytes\nTotal transferred file size: 10 bytes\n",
        check_output: str | None = None,
        euid: int = 0,
        fail_audit: bool = False,
        fail_target: bool = False,
        clock: Clock | None = None,
        environ: Mapping[str, str] | None = None,
        systemctl_output: str | None = "0\n",
        systemctl_returncode: int = 0,
        removal_seconds: Mapping[str, float] | None = None,
        removal_exits: Mapping[str, int] | None = None,
        transfer_seconds: float = 0.0,
    ) -> None:
        self.files = dict(files)
        self.names = list(names)
        self.remote_listing = remote_listing
        self.remote_manifest = remote_manifest or manifest().to_json()
        self.remote_record = remote_record
        self.stats = stats
        self.check_output = check_output
        self.euid = euid
        self.fail_audit = fail_audit
        self.fail_target = fail_target
        self.clock = clock or Clock()
        self.environ = dict(environ or {})
        self.systemctl_output = systemctl_output
        self.systemctl_returncode = systemctl_returncode
        self.removal_seconds = dict(removal_seconds or {})
        self.removal_exits = dict(removal_exits or {})
        self.transfer_seconds = transfer_seconds
        self.calls: list[tuple[tuple[str, ...], str | None, float | None]] = []
        self.writes: list[str] = []
        self.locks: dict[str, str] = {}

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
        if command and command[0] == "systemctl":
            if self.systemctl_output is None:
                raise FileNotFoundError("systemctl")
            return result(
                command, returncode=self.systemctl_returncode, stdout=self.systemctl_output
            )
        if command and command[0] == "docker" and "gideon_audit" in command:
            return result(command, returncode=int(self.fail_audit))
        if command and command[0] == "rsync":
            self.clock.advance(self.transfer_seconds)
            return result(command, stdout=self.stats)
        if command and command[0] == "awk":
            if len(command) != 3 or command[1] != "NR % 100 == 1":
                return result(command, returncode=1)
            listing = self.files.get(command[2])
            if listing is None:
                return result(command, returncode=1)
            return result(command, stdout="".join(line for number, line in enumerate(listing.splitlines(keepends=True), start=1) if number % 100 == 1))
        if command and command[0] == "ssh":
            if command[-1] == "true":
                return result(command, returncode=int(self.fail_target))
            remote_word = command[-1]
            if "rm -rf --" in remote_word:
                script = shlex.split(shlex.split(remote_word)[2])
                name = os.path.basename(script[script.index("rm") + 3])
                original = os.path.basename(script[2]) if script[0] == "mv" else name
                self.clock.advance(self.removal_seconds.get(name, 0.0))
                returncode = self.removal_exits.get(name, 0)
                remaining: list[str] = []
                for line in self.remote_listing.splitlines():
                    label, _, flag = line.partition("\t")
                    if label in {original, name}:
                        if returncode == 0:
                            continue
                        label = name
                    remaining.append(f"{label}\t{flag}\n")
                self.remote_listing = "".join(remaining)
                return result(command, returncode=returncode)
            if "for directory in */" in remote_word:
                return result(command, stdout=self.remote_listing)
            if len(command) >= 2 and command[-2] == "cat":
                if command[-1].endswith("/push.json"):
                    return result(command, stdout=self.remote_record or "", returncode=int(self.remote_record is None))
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

    def take_lock(self, path: PathLike, record: str) -> str | None:
        key = os.fspath(path)
        if key in self.locks:
            return self.locks[key]
        self.locks[key] = record
        return None

    def release_lock(self, path: PathLike) -> None:
        self.locks.pop(os.fspath(path), None)

    def geteuid(self) -> int:
        return self.euid


def host(
    *,
    names: Sequence[str] = (LOCAL_SET,),
    remote_listing: str = "",
    remote_manifest: str | None = None,
    remote_record: str | None = None,
    listing_text: str | None = None,
    previous_set_manifest: backupset.Manifest | None = None,
    stats: str | None = None,
    check_output: str | None = None,
    euid: int = 0,
    fail_audit: bool = False,
    fail_target: bool = False,
    local_set_manifest: backupset.Manifest | None = None,
    clock: Clock | None = None,
    environ: Mapping[str, str] | None = None,
    cgroup: str | None = "0::/system.slice/gideon-backup.service\n",
    systemctl_output: str | None = "0\n",
    systemctl_returncode: int = 0,
    removal_seconds: Mapping[str, float] | None = None,
    removal_exits: Mapping[str, int] | None = None,
    transfer_seconds: float = 0.0,
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
    if cgroup is not None:
        files["/proc/self/cgroup"] = cgroup
    if listing_text is not None:
        for pin in value.listings.values():
            files[os.path.join(backupset.set_dir(value.label), pin.file)] = listing_text
    if previous_set_manifest is not None:
        files[os.path.join(backupset.set_dir(previous_set_manifest.label), backupset.MANIFEST_NAME)] = previous_set_manifest.to_json()
    if remote_record is None and remote_listing:
        previous_label = remote_listing.split("\t", 1)[0]
        if backupset.validate_label(previous_label) is None:
            remote_record = backupset.PushRecord(previous_label, NOW - timedelta(days=1), value.label, value.archive_through).to_json()
    return FakeHost(
        files=files,
        names=names,
        remote_listing=remote_listing,
        remote_manifest=remote_manifest,
        remote_record=remote_record,
        stats=stats
        or "Total file size: 100 bytes\nTotal transferred file size: 10 bytes\n",
        check_output=check_output,
        euid=euid,
        fail_audit=fail_audit,
        fail_target=fail_target,
        clock=clock,
        environ=environ,
        systemctl_output=systemctl_output,
        systemctl_returncode=systemctl_returncode,
        removal_seconds=removal_seconds,
        removal_exits=removal_exits,
        transfer_seconds=transfer_seconds,
    )


def push(fake: FakeHost) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = backup.run_backup_push(
            argparse.Namespace(verify_all=False), host=fake, now=NOW,
            clock=fake.clock, environ=fake.environ,
        )
    return code, out.getvalue()


def audit_details(fake: FakeHost, kind: str) -> list[dict[str, object]]:
    details = []
    for _, request, _ in fake.calls:
        if request is None or f"\\set v_kind '{kind}'" not in request:
            continue
        line = next(line for line in request.splitlines() if line.startswith("\\set v_detail '"))
        details.append(json.loads(line.removeprefix("\\set v_detail '").removesuffix("'")))
    return details


def removal_scripts(fake: FakeHost) -> list[list[str]]:
    return [
        shlex.split(shlex.split(command[-1])[2])
        for command, _, _ in fake.calls
        if command[0] == "ssh" and "rm -rf --" in command[-1]
    ]


class Preconditions(unittest.TestCase):
    def test_refusals_name_their_fixes(self) -> None:
        cases = (
            ("root", host(euid=1000), "backup push as root"),
            ("site", host(), "Correct the site file"),
            ("rsync", host(), "host provision --only host-tools"),
            ("local", host(), "backup run"),
            ("audit", host(fail_audit=True), "logs postgres"),
            ("target", host(fail_target=True), "docs/runbooks/office-services-setup.md §3"),
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
                        argparse.Namespace(verify_all=False), host=fake, now=NOW,
                        clock=fake.clock, environ=fake.environ,
                    )
                self.assertEqual(code, 1)
                self.assertIn(expected, err.getvalue())


class PushContracts(unittest.TestCase):
    def test_refuses_while_a_foreign_holder_has_the_lock(self) -> None:
        fake = host()
        holder = backuplock.Record("restore", os.getpid() + 1, NOW)
        stored = holder.to_json()
        fake.locks[backuplock.BACKUP_LOCK.path] = stored
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = backup.run_backup_push(
                argparse.Namespace(verify_all=False), host=fake, now=NOW,
                clock=fake.clock, environ=fake.environ,
            )
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn(holder.command, err.getvalue())
        self.assertIn(holder.started.isoformat(), err.getvalue())
        self.assertEqual(fake.calls, [])
        self.assertEqual(fake.locks[backuplock.BACKUP_LOCK.path], stored)

    def test_listing_parser_and_script(self) -> None:
        parsed = backup._parse_remote_listing(
            "20260901T120000Z\t1\n"
            "20260901T110000Z.partial\t1\n"
            "20260901T100000Z.removing\t1\n"
            "20260901T100000Z.partial.removing\t1\n"
            "pre-example.removing\t1\n"
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
                ("20260901T100000Z.removing", False),
                ("20260901T100000Z.partial.removing", False),
                ("pre-example.removing", False),
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
        records: list[str] = []
        original_take_lock = fake.take_lock

        def capture_lock(path: PathLike, record: str) -> str | None:
            records.append(record)
            return original_take_lock(path, record)

        with (
            mock.patch.object(fake, "take_lock", side_effect=capture_lock),
            contextlib.redirect_stdout(out),
        ):
            code = backup.run_backup_push(
                argparse.Namespace(verify_all=False), host=fake, now=NOW,
                clock=fake.clock, environ=fake.environ,
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(records), 1)
        record = backuplock.parse(records[0])
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.command, "backup push")
        self.assertEqual(record.started, NOW)
        self.assertNotIn(backuplock.BACKUP_LOCK.path, fake.locks)
        self.assertEqual(
            [line.split(":", 1)[0] for line in out.getvalue().splitlines()],
            ["record", "list", "push", "check", "finalize", "audit", "prune"],
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
        manifest_call = next(
            call for call in fake.calls
            if call[0][0] == "ssh" and call[0][-2] == "cat"
        )
        self.assertEqual(
            manifest_call[0][-1],
            f"/backup/snapshots/{PUSH_LABEL}.partial/sets/{LOCAL_SET}/manifest.json",
        )
        check_call = next(
            call for call in fake.calls if call[0][0] == "ssh" and "sha256sum -c -" in call[0][-1]
        )
        expected = check_call[1]
        self.assertIsNotNone(expected)
        assert expected is not None
        self.assertIn(f"cd /backup/snapshots/{PUSH_LABEL}.partial &&", check_call[0][-1])
        finalize_index = next(i for i, call in enumerate(fake.calls) if call[0] == finalize[0])
        self.assertLess(fake.calls.index(manifest_call), finalize_index)
        self.assertLess(fake.calls.index(check_call), finalize_index)
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
        self.assertNotIn("pruned", audit_details(fake, "backup_push")[0])

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
                argparse.Namespace(verify_all=False), host=failed, now=NOW,
                clock=failed.clock, environ=failed.environ,
            )
        self.assertEqual(code, 1)
        push_row = next(line for line in out.getvalue().splitlines() if line.startswith("push:"))
        self.assertIn("901", push_row)
        self.assertIn("1000", push_row)
        self.assertIn("one filesystem", push_row)
        self.assertIn(
            "Fix: Keep every snapshot under one path on one filesystem on the target "
            "(docs/runbooks/office-services-setup.md §3), then re-run backup push.",
            push_row,
        )
        rsync = next(call[0] for call in failed.calls if call[0][0] == "rsync")
        self.assertIn("--link-dest=../20260901T120000Z", rsync)
        self.assertFalse(any(call[0][0] == "ssh" and "mv --" in call[0][-1] for call in failed.calls))

        first = host(
            remote_listing="",
            stats="Total file size: 1,000 bytes\nTotal transferred file size: 901 bytes\n",
        )
        self.assertEqual(
            backup.run_backup_push(
                argparse.Namespace(verify_all=False), host=first, now=NOW,
                clock=first.clock, environ=first.environ,
            ),
            0,
        )

    def test_pinned_listing_is_mandatory_and_objects_are_sampled_from_local_listing(self) -> None:
        store = next(root for root in backupset.inventory_roots("/work/GIDEON") if root.kind is backupset.RootKind.STORE)
        listing = "".join(f"{index:064x}\t7\n" for index in range(201))
        pin = backupset.ListingPin(f"{store.name}.listing", len(listing), "9" * 64, 201, 201 * 7)
        for version, place in ((1, f"sets/{LOCAL_SET}/files/{store.name}"), (2, store.name)):
            with self.subTest(version=version):
                value = replace(manifest(), version=version, listings={store.name: pin})
                fake = host(local_set_manifest=value, remote_manifest=value.to_json(), listing_text=listing)
                local_set = backupset.SetRef(LOCAL_SET, backupset.set_dir(LOCAL_SET), value.finished, True, value)
                record_text = backupset.PushRecord(PUSH_LABEL, NOW, LOCAL_SET, value.archive_through).to_json()
                sample = backup._push_check_input(
                    fake, local_set=local_set, remote_manifest=value, record_text=record_text, verify_all=False
                )
                self.assertIsInstance(sample, str)
                assert isinstance(sample, str)
                self.assertIn(f"{pin.sha256}  sets/{LOCAL_SET}/{pin.file}\n", sample)
                for index in (0, 100, 200):
                    name = f"{index:064x}"
                    self.assertIn(f"{name}  {place}/{name[:2]}/{name[2:4]}/{name}\n", sample)
                self.assertEqual(sum(f"  {place}/" in line for line in sample.splitlines()), 3)
                self.assertEqual(fake.calls, [(
                    ("awk", "NR % 100 == 1", backupset.listing_path(backupset.set_dir(LOCAL_SET), store.name)), None, None,
                )])

                fake.calls.clear()
                all_lines = backup._push_check_input(
                    fake, local_set=local_set, remote_manifest=value, record_text=record_text, verify_all=True
                )
                self.assertIsInstance(all_lines, str)
                assert isinstance(all_lines, str)
                self.assertEqual(sum(f"  {place}/" in line for line in all_lines.splitlines()), 201)
                self.assertEqual(fake.calls, [])

        previous = manifest()
        without_pin = host(local_set_manifest=previous)
        ref = backupset.SetRef(LOCAL_SET, backupset.set_dir(LOCAL_SET), previous.finished, True, previous)
        unchanged = backup._push_check_input(
            without_pin, local_set=ref, remote_manifest=previous, record_text=record_text, verify_all=False
        )
        self.assertIsInstance(unchanged, str)
        assert isinstance(unchanged, str)
        self.assertNotIn(f"sets/{LOCAL_SET}/{pin.file}", unchanged)
        self.assertEqual(without_pin.calls, [])

    def test_full_copy_guard_subtracts_only_bytes_new_since_previous_set(self) -> None:
        store = next(root for root in backupset.inventory_roots("/work/GIDEON") if root.kind is backupset.RootKind.STORE)
        name = "a" * 64
        listing = f"{name}\t400\n"
        pin = backupset.ListingPin(f"{store.name}.listing", len(listing), "b" * 64, 1, 400)
        value = replace(manifest(), listings={store.name: pin})
        older_label = "20260901T110000Z"
        remote_label = "20260901T120000Z"
        remote_listing = f"{remote_label}\t1\n"
        record = backupset.PushRecord(remote_label, NOW - timedelta(days=1), older_label, value.archive_through).to_json()
        transfer = "Total file size: 1,000 bytes\nTotal transferred file size: 901 bytes\n"
        cases = (
            ("empty prior pin", 1, 1, replace(pin, objects=0, bytes=0), True, record, 0),
            ("equal pins", 1, 1, pin, True, record, 1),
            ("prior set gone", 1, 1, pin, False, record, 0),
            # An unreadable record names no prior set: the store counts whole and the push proceeds.
            ("record unreadable", 1, 1, pin, True, "{", 0),
            ("first staging copy", 2, 1, pin, True, record, 0),
            ("staging copy already pushed", 2, 2, pin, True, record, 1),
            ("old-form after new-form", 1, 2, pin, True, record, 1),
            ("new-form empty prior pin", 2, 2, replace(pin, objects=0, bytes=0), True, record, 0),
        )
        for case, version, previous_version, prior_pin, staged, record_text, expected_code in cases:
            with self.subTest(case=case):
                current = replace(value, version=version)
                previous_set = replace(
                    current, version=previous_version,
                    label=older_label,
                    started=current.started - timedelta(days=1),
                    finished=current.finished - timedelta(days=1),
                    listings={store.name: prior_pin},
                )
                fake = host(
                    names=(LOCAL_SET, older_label) if staged else (LOCAL_SET,),
                    remote_listing=remote_listing,
                    remote_manifest=current.to_json(),
                    remote_record=record_text,
                    stats=transfer,
                    local_set_manifest=current,
                    previous_set_manifest=previous_set if staged else None,
                    listing_text=listing,
                )
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = backup.run_backup_push(
                        argparse.Namespace(verify_all=False), host=fake, now=NOW,
                        clock=fake.clock, environ=fake.environ,
                    )
                self.assertEqual(code, expected_code)
                push_row = next(line for line in out.getvalue().splitlines() if line.startswith("push:"))
                if expected_code:
                    self.assertIn("901 of 1000 bytes", push_row)
                    self.assertIn("one filesystem", push_row)
                else:
                    self.assertIn("push: ok", push_row)
                    self.assertNotIn("one filesystem", push_row)

    def test_prune_names_keep_fresh_and_current_snapshots(self) -> None:
        old = backupset.RemoteSnapshot("20260801T120000Z", None, True)
        stale = backupset.RemoteSnapshot("20260901T110000Z.partial", None, False)
        fresh = backupset.RemoteSnapshot("20260902T110000Z.partial", None, False)
        current = backupset.RemoteSnapshot(PUSH_LABEL, None, True)
        removing = backupset.RemoteSnapshot("20260902T113000Z.partial.removing", None, False)
        labelled = backupset.RemoteSnapshot("pre-example.removing", None, False)
        current_names = tuple(
            backupset.RemoteSnapshot(f"{PUSH_LABEL}{suffix}", None, False)
            for suffix in (".partial", ".removing", ".partial.removing")
        )
        snapshots = (old, stale, fresh, current, removing, labelled, *current_names)
        self.assertEqual(
            backup._push_prune_names(
                snapshots,
                now=NOW,
                remote_days=30,
                current_label=PUSH_LABEL,
            ),
            (removing.label, labelled.label, old.label, stale.label),
        )
        fake = host()
        loaded = site.load_site(SITE_PATH, host=fake)
        assert loaded.config is not None
        for name in (PUSH_LABEL, *(snapshot.label for snapshot in current_names)):
            with self.subTest(current=name):
                self.assertIsNone(
                    backup._safe_remote_snapshot_path(loaded.config, name, PUSH_LABEL)
                )
        self.assertIsNone(
            backup._safe_remote_snapshot_path(loaded.config, "/tmp/evil", "other")
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            stage = backup._push_prune_stage(
                fake,
                loaded.config,
                snapshots=snapshots,
                now=NOW,
                current_label=PUSH_LABEL,
                deadline=backup._PruneDeadline(fake.clock() + 10, "this run", "run"),
                clock=fake.clock,
                rendered_dir=RENDERED,
                run_id="00000000-0000-0000-0000-000000000001",
                now_was_supplied=True,
            )
        self.assertTrue(stage.ok)
        self.assertEqual(audit_details(fake, "backup_prune")[0]["removed"], 4)
        removed_commands = [
            call[0][-1]
            for call in fake.calls
            if call[0][0] == "ssh" and "rm -rf --" in call[0][-1]
        ]
        self.assertEqual(len(removed_commands), 4)
        scripts = removal_scripts(fake)
        self.assertEqual(
            [os.path.basename(script[script.index("rm") + 3]) for script in scripts],
            [removing.label, labelled.label, f"{old.label}.removing", f"{stale.label}.removing"],
        )
        self.assertTrue(all("mv" not in script for script in scripts[:2]))
        for script, snapshot in zip(scripts[2:], (old, stale), strict=True):
            with self.subTest(renamed=snapshot.label):
                path = f"/backup/snapshots/{snapshot.label}"
                self.assertEqual(script[:5], ["mv", "--", path, f"{path}.removing", "&&"])
                self.assertEqual(script[-4:], ["rm", "-rf", "--", f"{path}.removing"])
        self.assertTrue(all(int(script[script.index("timeout") + 2]) >= 1 for script in scripts))
        self.assertTrue(all("in 0.0 s" in line for line in out.getvalue().splitlines()))
        self.assertEqual(audit_details(fake, "backup_prune")[0]["duration_s"], 0.0)
        self.assertTrue(all("20260902T110000Z" not in command for command in removed_commands))

    def test_failed_checksum_row_names_only_the_count(self) -> None:
        fake = host(check_output="pgbackrest/file: FAILED\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = backup.run_backup_push(
                argparse.Namespace(verify_all=False), host=fake, now=NOW,
                clock=fake.clock, environ=fake.environ,
            )
        self.assertEqual(code, 1)
        self.assertNotIn(backuplock.BACKUP_LOCK.path, fake.locks)
        check_row = next(line for line in out.getvalue().splitlines() if line.startswith("check:"))
        self.assertIn("1 path(s)", check_row)
        self.assertNotIn("pgbackrest/file", check_row)
        self.assertIn(
            "Fix: Re-run sudo python3 -m gideon backup push --verify-all; if it fails "
            "again, the target's copy is corrupt — check the target's disk per "
            "docs/runbooks/office-services-setup.md §3.",
            check_row,
        )
        self.assertFalse(any(call[0][0] == "ssh" and "mv --" in call[0][-1] for call in fake.calls))
        self.assertEqual(audit_details(fake, "backup_push"), [])
        self.assertEqual(audit_details(fake, "backup_prune"), [])

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
        parsed = backup.parse_rsync_stats(
            "Total file size: 1,234,567 bytes\n"
            "Total transferred file size: 987,654 bytes\n"
        )
        self.assertEqual(parsed, backup.PushStats(1_234_567, 987_654))


class PruneContracts(unittest.TestCase):
    """The off-box prune reports each removal and ends inside its budget."""

    def test_push_audit_precedes_removal_and_deadline_leaves_the_rest(self) -> None:
        budget = backup.BACKUP_TIMEOUT_START_SECONDS - backup._PRUNE_MARGIN_SECONDS
        fake = host(
            remote_listing=OLD_LISTING,
            removal_seconds={f"{OLD_SNAPSHOTS[0]}.removing": budget + 1},
        )
        code, output = push(fake)
        self.assertEqual(code, 0)
        self.assertEqual(len(removal_scripts(fake)), 1)
        push_index = next(
            i for i, (_, request, _) in enumerate(fake.calls)
            if request and "\\set v_kind 'backup_push'" in request
        )
        removal_index = next(
            i for i, (argv, _, _) in enumerate(fake.calls)
            if argv[0] == "ssh" and "rm -rf --" in argv[-1]
        )
        prune_index = next(
            i for i, (_, request, _) in enumerate(fake.calls)
            if request and "\\set v_kind 'backup_prune'" in request
        )
        self.assertLess(push_index, removal_index)
        self.assertLess(removal_index, prune_index)
        self.assertIn(f"removed remote snapshot {OLD_SNAPSHOTS[0]} in 0.0 s", output)
        self.assertTrue(output.splitlines()[-1].startswith("prune: ok"))
        self.assertIn("pruned 1 remote snapshot(s), 2 left, deadline reached", output)
        self.assertEqual(audit_details(fake, "backup_prune"), [{
            "label": PUSH_LABEL, "removed": 1, "left": 2,
            "deadline_reached": True, "budget": "run", "duration_s": 0.0,
        }])
        self.assertNotIn(backuplock.BACKUP_LOCK.path, fake.locks)

    def test_timeout_leaves_a_removing_snapshot_and_the_next_push_resumes_it_first(self) -> None:
        for returncode in (124, 137):
            with self.subTest(returncode=returncode):
                removing_name = f"{OLD_SNAPSHOTS[0]}.removing"
                first = host(remote_listing=OLD_LISTING, removal_exits={removing_name: returncode})
                code, output = push(first)
                self.assertEqual(code, 0)
                self.assertEqual(len(removal_scripts(first)), 1)
                self.assertIn(f"left remote snapshot {OLD_SNAPSHOTS[0]} at the deadline", output)
                self.assertIn("pruned 0 remote snapshot(s), 3 left, deadline reached", output)
                self.assertTrue(output.splitlines()[-1].startswith("prune: ok"))
                first_detail = audit_details(first, "backup_prune")[0]
                self.assertEqual(first_detail["removed"], 0)
                self.assertEqual(first_detail["left"], 3)
                self.assertTrue(first_detail["deadline_reached"])
                self.assertIn(f"{removing_name}\t1\n", first.remote_listing)

                # Put untouched names ahead of the interrupted one in the next listing.
                second = host(remote_listing="\n".join(reversed(first.remote_listing.splitlines())) + "\n")
                code, output = push(second)
                self.assertEqual(code, 0)
                scripts = removal_scripts(second)
                self.assertEqual(len(scripts), 3)
                self.assertNotIn("mv", scripts[0])
                self.assertEqual(scripts[0][-1], f"/backup/snapshots/{removing_name}")
                self.assertTrue(all("mv" in script for script in scripts[1:]))
                self.assertIn(f"removed remote snapshot {removing_name} in 0.0 s", output)
                self.assertIn("pruned 3 remote snapshot(s), 0 left", output)
                self.assertFalse(audit_details(second, "backup_prune")[0]["deadline_reached"])
                self.assertEqual(second.remote_listing, "")

    def test_budget_source_and_seconds_follow_the_unit_start_or_fallback(self) -> None:
        budget = backup.BACKUP_TIMEOUT_START_SECONDS - backup._PRUNE_MARGIN_SECONDS
        for unit in ("gideon-backup.service", "gideon-backup-verify.service"):
            with self.subTest(unit=unit):
                clock = Clock()
                elapsed = 30
                fake = host(
                    remote_listing=OLD_LISTING,
                    clock=clock,
                    environ={"INVOCATION_ID": "fictitious"},
                    cgroup=f"0::/system.slice/outer.service/{unit}/payload\n",
                    systemctl_output=f"{int((clock() - elapsed) * 1e6)}\n",
                )
                code, output = push(fake)
                self.assertEqual(code, 0)
                self.assertIn(f"(budget from {unit})", output.splitlines()[-1])
                self.assertEqual(audit_details(fake, "backup_prune")[0]["budget"], "unit")
                self.assertEqual(
                    [argv for argv, _, _ in fake.calls if argv[0] == "systemctl"],
                    [("systemctl", "show", "-p", "InactiveExitTimestampMonotonic", "--value", unit)],
                )
                for script in removal_scripts(fake):
                    self.assertEqual(int(script[script.index("timeout") + 2]), int(budget - elapsed))

        for name, cgroup, answer, returncode, invocation in (
            ("hand run", None, "0\n", 0, False),
            ("missing cgroup", None, "0\n", 0, True),
            ("malformed cgroup", "invalid\n", "0\n", 0, True),
            ("no service", "0::/user.slice\n", "0\n", 0, True),
            ("failed systemctl", "0::/gideon-backup.service\n", "0\n", 1, True),
            ("missing systemctl", "0::/gideon-backup.service\n", None, 0, True),
            ("empty timestamp", "0::/gideon-backup.service\n", "", 0, True),
            ("negative timestamp", "0::/gideon-backup.service\n", "-1\n", 0, True),
            ("malformed timestamp", "0::/gideon-backup.service\n", "unknown\n", 0, True),
            ("multiple timestamps", "0::/gideon-backup.service\n", "1\n2\n", 0, True),
        ):
            with self.subTest(fallback=name):
                fake = host(
                    remote_listing=OLD_LISTING,
                    environ={"INVOCATION_ID": "fictitious"} if invocation else {},
                    cgroup=cgroup,
                    systemctl_output=answer,
                    systemctl_returncode=returncode,
                )
                code, output = push(fake)
                self.assertEqual(code, 0)
                source = "this run (unit start unavailable)" if invocation else "this run"
                self.assertIn(f"(budget from {source})", output.splitlines()[-1])
                self.assertEqual(audit_details(fake, "backup_prune")[0]["budget"], "run")
                scripts = removal_scripts(fake)
                self.assertEqual(len(scripts), len(OLD_SNAPSHOTS))
                for script in scripts:
                    self.assertEqual(int(script[script.index("timeout") + 2]), int(budget))
                if not invocation or not cgroup or ".service" not in cgroup:
                    self.assertFalse(any(argv[0] == "systemctl" for argv, _, _ in fake.calls))

    def test_transfer_consumes_the_fallback_budget_before_pruning(self) -> None:
        budget = backup.BACKUP_TIMEOUT_START_SECONDS - backup._PRUNE_MARGIN_SECONDS
        fake = host(
            remote_listing=OLD_LISTING,
            environ={"INVOCATION_ID": "fictitious"},
            systemctl_returncode=1,
            transfer_seconds=budget + 1,
        )
        code, output = push(fake)
        self.assertEqual(code, 0)
        self.assertEqual(removal_scripts(fake), [])
        self.assertEqual(len(audit_details(fake, "backup_push")), 1)
        self.assertIn("pruned 0 remote snapshot(s), 3 left, deadline reached", output)
        self.assertIn("budget from this run (unit start unavailable)", output)
        self.assertEqual(audit_details(fake, "backup_prune"), [{
            "label": PUSH_LABEL, "removed": 0, "left": 3,
            "deadline_reached": True, "budget": "run", "duration_s": 0.0,
        }])

    def test_removal_requires_a_whole_second_and_never_uses_timeout_zero(self) -> None:
        budget = backup.BACKUP_TIMEOUT_START_SECONDS - backup._PRUNE_MARGIN_SECONDS
        for remaining in (0.5, 1.0, 1.9):
            with self.subTest(remaining=remaining):
                fake = host(
                    remote_listing=f"{OLD_SNAPSHOTS[0]}\t1\n",
                    transfer_seconds=budget - remaining,
                )
                code, output = push(fake)
                self.assertEqual(code, 0)
                scripts = removal_scripts(fake)
                self.assertEqual(len(scripts), int(remaining >= 1))
                for script in scripts:
                    timeout_index = script.index("timeout")
                    self.assertEqual(script[timeout_index + 1], "--kill-after=30")
                    self.assertEqual(int(script[timeout_index + 2]), int(remaining))
                    self.assertGreaterEqual(int(script[timeout_index + 2]), 1)
                detail = audit_details(fake, "backup_prune")[0]
                self.assertEqual(detail["deadline_reached"], remaining < 1)
                self.assertEqual(detail["removed"], int(remaining >= 1))
                self.assertEqual(detail["left"], int(remaining < 1))
                if remaining < 1:
                    self.assertIn("deadline reached", output)

    def test_failed_removal_refuses_after_the_push_row_was_written(self) -> None:
        for returncode in (1, 127):
            with self.subTest(returncode=returncode):
                fake = host(
                    remote_listing=OLD_LISTING,
                    removal_exits={f"{OLD_SNAPSHOTS[0]}.removing": returncode},
                )
                code, output = push(fake)
                self.assertEqual(code, 1)
                self.assertEqual(len(removal_scripts(fake)), 1)
                self.assertEqual(len(audit_details(fake, "backup_push")), 1)
                self.assertEqual(audit_details(fake, "backup_prune"), [])
                closing = output.splitlines()[-1]
                self.assertTrue(closing.startswith("prune: refuse"))
                self.assertIn("Fix:", closing)
                self.assertIn("backup push", closing)
                self.assertNotIn(backuplock.BACKUP_LOCK.path, fake.locks)

    def test_prune_measures_seconds_when_now_was_not_supplied(self) -> None:
        name = OLD_SNAPSHOTS[0]
        fake = host(removal_seconds={f"{name}.removing": 2.5})
        loaded = site.load_site(SITE_PATH, host=fake)
        assert loaded.config is not None
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            stage = backup._push_prune_stage(
                fake, loaded.config,
                snapshots=(backupset.RemoteSnapshot(name, None, True),),
                now=NOW,
                current_label=PUSH_LABEL,
                deadline=backup._PruneDeadline(fake.clock() + 10, "this run", "run"),
                clock=fake.clock,
                rendered_dir=RENDERED,
                run_id="00000000-0000-0000-0000-000000000001",
                now_was_supplied=False,
            )
        self.assertTrue(stage.ok)
        self.assertIn(f"removed remote snapshot {name} in 2.5 s", out.getvalue())
        self.assertEqual(audit_details(fake, "backup_prune")[0]["duration_s"], 2.5)


if __name__ == "__main__":
    unittest.main()
