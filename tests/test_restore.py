"""Restore orchestration contracts over a dict-backed host seam."""

import argparse
import contextlib
import hashlib
import io
import os
import subprocess
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

from gideon.host import backuplock, backupset, nogpu, restore, secrets, site, sshtarget
from gideon.host.steps.site_dirs import AGE_RECIPIENT_PATH
from gideon.host.sysio import Command, PathLike

RENDERED = "/etc/gideon/rendered"
SITE_PATH = "/etc/gideon/site.yaml"
LOCAL_SET = "20260902T110000Z"
REMOTE_SNAPSHOT = "20260902T120000Z"
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
RECIPIENT = "age1" + "a" * 58
SECOND_RECIPIENT = "age1" + "c" * 58
DEFAULT_GIDEON_IDS = backupset.AccountIds(999, 983)
TARBALL_BYTES = b"tarball"
TARBALL_SHA256 = hashlib.sha256(TARBALL_BYTES).hexdigest()
SELECT_FIX = "Choose an earlier --at, or omit it for the latest state."
SECRET_DIGESTS = {"webui_secret_key": "1" * 64, "ldap_bind_password": "2" * 64}
_fingerprint = secrets.secrets_fingerprint(SECRET_DIGESTS)
assert _fingerprint is not None
FINGERPRINT: str = _fingerprint

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


def completed(
    argv: Sequence[str], stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)


def entry(
    path: str,
    *,
    digest: str = "a" * 64,
    uid: int = 1000,
    gid: int = 1000,
    mode: int = 0o644,
) -> backupset.Entry:
    return backupset.Entry(path, "f", 7, uid, gid, mode, 100.0, digest)


def manifest(*, finished: datetime = NOW - timedelta(hours=1)) -> backupset.Manifest:
    return backupset.Manifest(
        1,
        LOCAL_SET,
        backupset.Kind.NIGHTLY,
        finished - timedelta(minutes=10),
        finished,
        "0.0.18",
        "/work/GIDEON",
        "commit",
        "gideon.test",
        None,
        "backup-label",
        "full",
        finished,
        {"gideon": {"public.users": 2}, "openwebui": {}},
        {
            "etc-gideon": (entry("config.yaml"),),
            "checkout": (entry("gideon/__init__.py"),),
            "data-registry": (entry("image.blob"),),
            "data-bulk-openwebui": (entry("uploads/data.bin"),),
            "pgbackrest": (
                entry("backup/gideon/backup.info", mode=0o640),
                entry("archive/gideon/archive.info", digest="b" * 64, mode=0o640),
                entry(
                    "backup/gideon/backup-label/backup.manifest",
                    digest="c" * 64,
                    mode=0o640,
                ),
            ),
        },
        TARBALL_SHA256,
        (RECIPIENT, SECOND_RECIPIENT),
        backupset.LinkVerdict(1, 1),
        FINGERPRINT,
    )


class FakeHost:
    """A recording Host with independent local and fetched set listings."""

    def __init__(
        self,
        *,
        running: bool = False,
        target: bool = True,
        source: str = "staging",
        names: Sequence[str] = (LOCAL_SET,),
        side_names: Sequence[str] = (LOCAL_SET,),
        remote_listing: str = "",
        remote_record: backupset.PushRecord | None = None,
        remote_records: Mapping[str, backupset.PushRecord] | None = None,
        fail_checksum: bool = False,
        fail_pgbackrest: bool = False,
        info_has_label: bool = True,
        fail_identity_exec: bool = False,
        secrets_present: bool = False,
        partial: bool = False,
        sets_absent: bool = False,
        fail_swap: bool = False,
        manifest_value: backupset.Manifest | None = None,
        physical_escape: bool = False,
        extra_physical_file: tuple[str, str] | None = None,
        registry_active: bool = False,
        gideon_ids: backupset.AccountIds = DEFAULT_GIDEON_IDS,
        fail_gideon_ids: bool = False,
    ) -> None:
        value = manifest_value or manifest()
        self.manifest_value = value
        self.partial = partial
        self.fail_swap = fail_swap
        self.physical_escape = physical_escape
        self.extra_physical_file = extra_physical_file
        self.registry_active = registry_active
        self.gideon_ids = gideon_ids
        self.fail_gideon_ids = fail_gideon_ids
        self.running = running
        self.target = target
        self.source = source
        self.names = list(names)
        self.side_names = list(side_names)
        self.sets_absent = sets_absent
        self.remote_listing = remote_listing
        self.remote_record = remote_record or backupset.PushRecord(
            REMOTE_SNAPSHOT, NOW, LOCAL_SET, value.archive_through
        )
        self.remote_records = dict(remote_records or {})
        self.fail_checksum = fail_checksum
        self.fail_pgbackrest = fail_pgbackrest
        self.info_has_label = info_has_label
        self.fail_identity_exec = fail_identity_exec
        self.calls: list[tuple[tuple[str, ...], str | None, str | None, float | None]] = []
        self.files: dict[str, str] = {
            SITE_PATH: SITE,
            os.fspath(AGE_RECIPIENT_PATH): RECIPIENT + "\n",
            os.path.join(RENDERED, "compose.yaml"): "services: {}\n",
            "/usr/bin/bash": "",
            "/usr/bin/age": "",
            "/usr/bin/rsync": "",
            os.path.join(
                backupset.set_dir(LOCAL_SET), backupset.MANIFEST_NAME
            ): value.to_json(),
        }
        self.secrets_present = secrets_present
        self.locks: dict[str, str] = {}

    def _root_for(self, base: str) -> str:
        """Which inventory root a find base names: a set's files/<root>, a live source, or the repository."""

        if "/files/" in base:
            return base.rsplit("/files/", 1)[1].split("/", 1)[0]
        for root in backupset.inventory_roots(self.manifest_value.checkout):
            if base == root.source:
                return root.name
        if base.endswith("/pgbackrest"):
            return "pgbackrest"
        return ""

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
        del check, env
        command = tuple(argv)
        self.calls.append((command, input, None if cwd is None else os.fspath(cwd), timeout))
        if command == ("getent", "passwd", backupset.SERVICE_ACCOUNT):
            if self.fail_gideon_ids:
                return completed(command, returncode=1)
            return completed(
                command,
                stdout=(
                    f"{backupset.SERVICE_ACCOUNT}:x:{self.gideon_ids.uid}:"
                    f"{self.gideon_ids.gid}::/home/{backupset.SERVICE_ACCOUNT}:/bin/bash\n"
                ),
            )
        if command == ("systemctl", "is-active", "gideon-registry"):
            return completed(command, returncode=0 if self.registry_active else 3)
        if "exec" in command and "pg_isready" in command:
            return completed(command, returncode=0 if self.running and not self.partial else 1)
        if command[:2] == ("mv", f"{backupset.STAGING}.fetch-{REMOTE_SNAPSHOT}") and self.fail_swap:
            return completed(command, returncode=1)
        if command[:2] == ("docker", "compose") and "up" in command:
            self.running = True
        if command[:2] == ("docker", "compose") and command[-1] == "down":
            self.running = False
        if "exec" in command and "id" in command:
            if self.fail_identity_exec:
                return completed(command, returncode=1)
            if command[-2:] == ("-u", "postgres"):
                return completed(command, stdout="1000\n")
            if command[-2:] == ("-g", "postgres"):
                return completed(command, stdout="1001\n")
        if command and command[0] == "ssh":
            if command[-1] == "true":
                return completed(command, returncode=int(not self.target))
            if "for directory in */" in command[-1]:
                return completed(command, stdout=self.remote_listing)
            if len(command) >= 2 and command[-2] == "cat":
                record = self.remote_record
                for name, candidate in self.remote_records.items():
                    if f"/{name}/push.json" in command[-1]:
                        record = candidate
                        break
                return completed(command, stdout=record.to_json())
        if command and command[0] == "rsync":
            return completed(command)
        if command[0] == "find" and command[-2:] == ("-printf", backupset.FIND_FORMAT):
            # The physical tree under one base: that root's inventoried entries
            # with their declared kinds, unless the tree has been made to escape.
            base = str(command[1])
            root_name = self._root_for(base)
            records = "d\t4096\t0\t0\t755\t100.0\t\0"
            for item in self.manifest_value.inventory.get(root_name, ()):
                if self.physical_escape and item.path.startswith("escape/"):
                    continue
                records += f"{item.kind}\t{item.size}\t{item.uid}\t{item.gid}\t{item.mode:o}\t{item.mtime}\t{item.path}\0"
            if self.physical_escape:
                records += "l\t4\t0\t0\t777\t100.0\tescape\0"
            if self.extra_physical_file is not None and self.extra_physical_file[0] == root_name:
                records += f"f\t3\t0\t0\t644\t100.0\t{self.extra_physical_file[1]}\0"
            return completed(command, stdout=records)
        if command[:2] == ("find", "/etc/gideon/secrets"):
            digests = SECRET_DIGESTS if self.secrets_present else {"webui_secret_key": "9" * 64}
            return completed(
                command,
                stdout="".join(f"{digest}  /etc/gideon/secrets/{name}\n" for name, digest in digests.items()),
            )
        if command[:2] == ("sha256sum", "-c"):
            if not (input or ""):
                # The real tool refuses an empty list.
                return completed(command, returncode=1, stderr="sha256sum: 'standard input': no properly formatted checksum lines found\n")
            if self.fail_checksum:
                return completed(command, returncode=1, stdout="config.yaml: FAILED\n")
            output = "".join(
                line.split("  ", 1)[1].rstrip("\n") + ": OK\n"
                for line in (input or "").splitlines(True)
            )
            return completed(command, stdout=output)
        if command and command[0] == "sha256sum":
            return completed(command, stdout=f"{TARBALL_SHA256}  {command[-1]}\n")
        if command and command[0] == "pgbackrest":
            return completed(command, returncode=int(self.fail_pgbackrest))
        if "pgbackrest" in command and "verify" in command:
            return completed(command, returncode=int(self.fail_pgbackrest))
        if "pgbackrest" in command and "info" in command and "--output=json" in command:
            # The repository now in place: the set's backup on timeline 2.
            labels = [self.manifest_value.pgbackrest_label] if self.info_has_label else []
            backups = ",".join(
                f'{{"label":"{label}","type":"full","timestamp":{{"stop":100}},'
                f'"archive":{{"start":"000000020000000000000005","stop":"000000020000000000000007"}}}}'
                for label in labels
            )
            return completed(command, stdout=f'[{{"name":"gideon","backup":[{backups}]}}]')
        if "run" in command and "id" in command:
            flag = "-u" if "-u" in command else "-g"
            return completed(command, stdout=("1000" if flag == "-u" else "1001") + "\n")
        if len(command) >= 2 and command[-2:] == ("--format", "json"):
            if self.partial:
                return completed(command, stdout='[{"Service":"open-webui","State":"running","Health":"healthy"}]')
            if not self.running:
                return completed(command, stdout="[]")
            return completed(
                command,
                stdout='[{"Service":"postgres","State":"running","Health":"healthy"}]',
            )
        return completed(command)

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        if ".fetch-" in key and key.endswith(
            f"/sets/{LOCAL_SET}/{backupset.MANIFEST_NAME}"
        ):
            return self.files[
                os.path.join(
                    backupset.set_dir(LOCAL_SET), backupset.MANIFEST_NAME
                )
            ]
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
        self.files[os.fspath(path)] = text

    def exists(self, path: PathLike) -> bool:
        return os.fspath(path) in self.files

    def listdir(self, path: PathLike) -> list[str]:
        key = os.fspath(path)
        if key == backupset.SETS_DIR:
            if self.sets_absent:
                raise FileNotFoundError(key)
            return list(self.names)
        if key == f"{backupset.STAGING}.fetch-{REMOTE_SNAPSHOT}/sets":
            return list(self.side_names)
        if key.endswith("/sets") and ".fetch-" in key:
            return list(self.side_names)
        raise FileNotFoundError(key)

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del missing_ok
        self.files.pop(os.fspath(path), None)

    def stat(self, path: PathLike) -> os.stat_result:
        if os.fspath(path) == "/data/registry":
            return os.stat_result((0o40755, 1, 0, 1, 999, 983, 0, 0, 0, 0))
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
        return 0


def make_host(**kwargs: Any) -> FakeHost:
    return FakeHost(**kwargs)


class RestoreContracts(unittest.TestCase):
    def test_refuses_while_a_foreign_holder_has_the_lock(self) -> None:
        fake = make_host()
        holder = backuplock.Record("backup push", os.getpid() + 1, NOW)
        stored = holder.to_json()
        fake.locks[backuplock.LOCK_PATH] = stored
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at=None),
                host=fake,
                now=NOW,
            )
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn(holder.command, err.getvalue())
        self.assertIn(holder.started.isoformat(), err.getvalue())
        self.assertEqual(fake.calls, [])
        self.assertEqual(fake.locks[backuplock.LOCK_PATH], stored)

    def test_staging_restore_verifies_before_down_and_prints_next_steps(self) -> None:
        fake = make_host()
        fake.files[os.fspath(nogpu.BUILD_BOX_PATH)] = "declared\n"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at=None),
                host=fake,
                now=NOW,
            )
        self.assertEqual(code, 0)
        self.assertEqual(fake.manifest_value.recipients, (RECIPIENT, SECOND_RECIPIENT))
        names = [
            line.split(":", 1)[0]
            for line in out.getvalue().splitlines()
            if line.startswith(("select:", "pre-restore:", "fetch:", "verify:", "stop:", "swap:", "files:", "postgres:", "stores:", "next:"))
        ]
        self.assertEqual(
            names,
            ["select", "pre-restore", "fetch", "verify", "stop", "swap", "files", "postgres", "stores", "next"],
        )
        down_index = next(index for index, call in enumerate(fake.calls) if call[0][-1] == "down")
        verify_index = next(index for index, call in enumerate(fake.calls) if "verify" in call[0])
        self.assertLess(verify_index, down_index)
        self.assertIn("age -d -i <identity file kept off-box>", out.getvalue())
        self.assertIn("not the set's", out.getvalue())
        self.assertIn("Next: sudo python3 -m gideon host provision", out.getvalue())
        self.assertIn("stop: ok — stopped the host registry", out.getvalue())
        self.assertIn(
            "stores: ok — frontend and ingress down; the store tier and the host registry running",
            out.getvalue(),
        )
        self.assertIn(
            "next: ok — frontend and ingress down; the store tier and the host registry running",
            out.getvalue(),
        )
        self.assertIn(("systemctl", "stop", "gideon-registry"), [call[0] for call in fake.calls])
        self.assertIn(("systemctl", "start", "gideon-registry"), [call[0] for call in fake.calls])

    def test_undeclared_host_skips_host_registry_and_names_build_box(self) -> None:
        fake = make_host()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at=None),
                host=fake,
                now=NOW,
            )

        self.assertEqual(code, 0, out.getvalue())
        calls = [call[0] for call in fake.calls]
        self.assertNotIn(("systemctl", "stop", "gideon-registry"), calls)
        self.assertNotIn(("systemctl", "start", "gideon-registry"), calls)
        output = out.getvalue()
        self.assertIn(
            "stop: ok — stopped the GIDEON Compose project; no host registry: not the build box",
            output,
        )
        self.assertIn(
            "stores: ok — frontend and ingress down; the store tier running; no host registry: not the build box",
            output,
        )
        self.assertIn(
            "next: ok — frontend and ingress down; the store tier running; no host registry: not the build box",
            output,
        )

    def test_no_gpu_host_also_skips_host_registry_and_names_build_box(self) -> None:
        fake = make_host()
        fake.files[os.fspath(nogpu.NO_GPU_PATH)] = "declared\n"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at=None),
                host=fake,
                now=NOW,
            )

        self.assertEqual(code, 0, out.getvalue())
        calls = [call[0] for call in fake.calls]
        self.assertNotIn(("systemctl", "stop", "gideon-registry"), calls)
        self.assertNotIn(("systemctl", "start", "gideon-registry"), calls)
        output = out.getvalue()
        self.assertIn(
            "stop: ok — stopped the GIDEON Compose project; no host registry: not the build box",
            output,
        )
        self.assertIn(
            "stores: ok — frontend and ingress down; the store tier running; no host registry: not the build box",
            output,
        )
        self.assertIn(
            "next: ok — frontend and ingress down; the store tier running; no host registry: not the build box",
            output,
        )

    def test_active_registry_on_an_undeclared_host_refuses_before_any_stage(self) -> None:
        fake = make_host(registry_active=True)
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at=None),
                host=fake,
                now=NOW,
            )

        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn(
            "the host registry is active, but this host is not declared the build box.",
            err.getvalue(),
        )
        self.assertIn(
            "Stop and disable gideon-registry (a former build box keeps its unit until removed by hand), or declare this host with host provision --build-box, then retry.",
            err.getvalue(),
        )
        calls = [call[0] for call in fake.calls]
        self.assertFalse(any(command[-1] == "down" for command in calls))
        self.assertNotIn(("systemctl", "stop", "gideon-registry"), calls)
        self.assertFalse(any(command[0] == "mv" for command in calls))

    def test_restore_excludes_the_build_box_marker_from_etc_gideon(self) -> None:
        fake = make_host()
        with contextlib.redirect_stdout(io.StringIO()):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at=None),
                host=fake,
                now=NOW,
            )

        self.assertEqual(code, 0)
        etc_rsync = next(
            call[0]
            for call in fake.calls
            if call[0][0] == "rsync" and call[0][-1] == "/etc/gideon/"
        )
        self.assertIn("--exclude=build-box", etc_rsync)

    def test_target_fetch_reowns_whole_side_and_swaps_after_verify(self) -> None:
        fake = make_host(
            source="target",
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
            side_names=(LOCAL_SET,),
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="target", at=None),
                host=fake,
                now=NOW,
            )
        self.assertEqual(code, 0)
        fetch = next(call[0] for call in fake.calls if call[0][0] == "rsync")
        self.assertEqual(
            fetch,
            (
                "rsync",
                "-aH",
                "--delete",
                "-e",
                sshtarget.rsync_ssh_option(),
                "gideon-backup@nas.test:/backup/snapshots/20260902T120000Z/",
                "/data/backup-staging.fetch-20260902T120000Z/",
            ),
        )
        self.assertTrue(
            any(call[0][:4] == ("chown", "-R", "-h", "1000:1001") for call in fake.calls)
        )
        self.assertTrue(
            any("/etc/gideon/config.yaml" in call[0] for call in fake.calls if call[0][0] == "chown")
        )
        self.assertTrue(
            any(
                call[0][:2]
                == ("find", "/data/backup-staging.fetch-20260902T120000Z/pgbackrest")
                for call in fake.calls
            )
        )
        self.assertTrue(any(call[0][:2] == ("mv", backupset.STAGING) for call in fake.calls))
        side = f"{backupset.STAGING}.fetch-{REMOTE_SNAPSHOT}"
        chowns = [call[0] for call in fake.calls if call[0][0] == "chown"]
        self.assertTrue(
            any(
                argv[1] == "1000:1000" and "/etc/gideon/config.yaml" in argv
                for argv in chowns
            )
        )
        self.assertIn(("chown", "gideon:gideon", "--", side), chowns)
        skeleton = [argv for argv in chowns if argv[1] == "0:0" and f"{side}/sets" in argv]
        self.assertTrue(skeleton, chowns)
        self.assertIn(f"{side}/sets/{LOCAL_SET}/files", skeleton[0])
        self.assertIn("frontend and ingress down", out.getvalue())

    def test_recorded_ids_are_mapped_in_fetch_and_files_for_target_and_staging(self) -> None:
        value = manifest()
        recorded = backupset.AccountIds(999, 983)
        additions = (
            entry("mapped-both", uid=999, gid=983),
            entry("mapped-uid", uid=999, gid=0),
            entry("mapped-gid", uid=0, gid=983),
            backupset.Entry("mapped-link", "l", 1, 999, 983, 0o777, 100.0, None),
        )
        value = replace(
            value,
            gideon_ids=recorded,
            inventory={
                **value.inventory,
                "etc-gideon": (*value.inventory["etc-gideon"], *additions),
            },
        )
        host_ids = backupset.AccountIds(998, 997)
        for source in ("target", "staging"):
            with self.subTest(source=source):
                fake = make_host(
                    source=source,
                    manifest_value=value,
                    gideon_ids=host_ids,
                    remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
                    side_names=(LOCAL_SET,),
                )
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = restore.run_restore(
                        argparse.Namespace(source=source, at=None),
                        host=fake,
                        now=NOW,
                    )
                self.assertEqual(code, 0, out.getvalue())
                output = out.getvalue()
                self.assertIn(
                    f"4 {restore.REOWN_MAPPED_DETAIL} {host_ids.uid}:{host_ids.gid}",
                    output,
                )
                chowns = [call[0] for call in fake.calls if call[0][0] == "chown"]
                self.assertIn(
                    ("chown", "998:997", "--", "/etc/gideon/mapped-both"),
                    chowns,
                )
                self.assertIn(
                    ("chown", "998:0", "--", "/etc/gideon/mapped-uid"),
                    chowns,
                )
                self.assertIn(
                    ("chown", "0:997", "--", "/etc/gideon/mapped-gid"),
                    chowns,
                )
                self.assertIn(
                    ("chown", "-h", "998:997", "--", "/etc/gideon/mapped-link"),
                    chowns,
                )

    def test_no_record_rows_and_next_line_name_host_provision(self) -> None:
        fake = make_host(
            source="target",
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
            side_names=(LOCAL_SET,),
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="target", at=None),
                host=fake,
                now=NOW,
            )
        self.assertEqual(code, 0, out.getvalue())
        output = out.getvalue()
        self.assertIn(
            f"re-owned 4 path(s), re-owned by their recorded ids "
            f"(1 set(s) with {restore.REOWN_NO_RECORD_DETAIL})",
            output,
        )
        self.assertIn(
            f"; re-owned by the recorded ids ({restore.REOWN_NO_RECORD_DETAIL})",
            output,
        )
        self.assertIn("Next: sudo python3 -m gideon host provision", output)
        self.assertIn("then sudo python3 -m gideon apply", output)

    def test_missing_gideon_account_refuses_before_select(self) -> None:
        fake = make_host(fail_gideon_ids=True)
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at=None),
                host=fake,
                now=NOW,
            )
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertFalse(any(call[0][0] == "docker" for call in fake.calls))
        self.assertIn(
            f"the {backupset.SERVICE_ACCOUNT} service account is missing or malformed.",
            err.getvalue(),
        )
        self.assertIn(
            "Run sudo python3 -m gideon host provision, then retry.",
            err.getvalue(),
        )

    def test_at_uses_the_oldest_remote_boundary_that_covers_it(self) -> None:
        early = backupset.PushRecord(
            "20260901T120000Z",
            NOW - timedelta(days=1),
            LOCAL_SET,
            NOW - timedelta(days=2),
        )
        late = backupset.PushRecord(
            REMOTE_SNAPSHOT,
            NOW,
            LOCAL_SET,
            NOW - timedelta(hours=1),
        )
        fake = make_host(
            source="target",
            remote_listing="20260901T120000Z\t1\n20260902T120000Z\t1\n",
            remote_record=late,
            remote_records={"20260901T120000Z": early, REMOTE_SNAPSHOT: late},
        )
        # The fake returns one record for every cat; exercise the pure choice
        # directly for the differing coverage boundaries.
        config = site.load_site(SITE_PATH, host=fake).config
        assert config is not None
        choice = restore._select_target(
            fake,
            config,
            NOW - timedelta(hours=2),
        )
        self.assertTrue(choice[0].ok)
        assert choice[1] is not None
        assert choice[1].snapshot is not None
        self.assertEqual(choice[1].snapshot.label, REMOTE_SNAPSHOT)
        self.assertEqual(early.newest_set, LOCAL_SET)

    def test_pre_restore_runs_only_when_postgres_is_ready_and_pushes_target(self) -> None:
        for source in ("staging", "target"):
            with self.subTest(source=source):
                fake = make_host(
                    running=True,
                    source=source,
                    remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
                    side_names=(LOCAL_SET,),
                )
                calls: list[tuple[str, object]] = []

                def record_run(
                    *args: object,
                    _calls: list[tuple[str, object]] = calls,
                    **kwargs: object,
                ) -> int:
                    del args
                    _calls.append(("run", kwargs))
                    return 0

                def record_push(
                    *args: object,
                    _calls: list[tuple[str, object]] = calls,
                    **kwargs: object,
                ) -> int:
                    del args
                    _calls.append(("push", kwargs))
                    return 0

                with (
                    patch.object(
                        restore.backup,
                        "run_backup_run",
                        side_effect=record_run,
                    ),
                    patch.object(
                        restore.backup,
                        "run_backup_push",
                        side_effect=record_push,
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    code = restore.run_restore(
                        argparse.Namespace(source=source, at=None),
                        host=fake,
                        now=NOW,
                    )
                self.assertEqual(code, 0)
                self.assertEqual([kind for kind, _ in calls], ["run", "push"] if source == "target" else ["run"])
                run_kwargs = calls[0][1]
                assert isinstance(run_kwargs, dict)
                self.assertTrue(run_kwargs["pre_restore"])
                self.assertEqual(run_kwargs["now"], NOW)

    def test_pre_restore_nested_runs_keep_the_restore_lock(self) -> None:
        for source in ("staging", "target"):
            with self.subTest(source=source):
                fake = make_host(
                    running=True,
                    source=source,
                    remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
                    side_names=(LOCAL_SET,),
                )
                outer_records: list[str] = []
                nested_returns: list[str] = []
                original_take_lock = fake.take_lock

                def observe_lock(
                    path: PathLike,
                    record: str,
                    *,
                    _original_take_lock: Any = original_take_lock,
                    _outer_records: list[str] = outer_records,
                    _nested_returns: list[str] = nested_returns,
                ) -> str | None:
                    result = _original_take_lock(path, record)
                    if result is None:
                        _outer_records.append(record)
                    else:
                        _nested_returns.append(result)
                    return result

                def nested_run(
                    *args: object,
                    _fake: FakeHost = fake,
                    _outer_records: list[str] = outer_records,
                    **kwargs: object,
                ) -> int:
                    del args, kwargs
                    outcome = backuplock.take(_fake, command="backup run", now=NOW)
                    self.assertEqual(outcome.state, backuplock.State.NESTED)
                    self.assertEqual(
                        _fake.locks[backuplock.LOCK_PATH], _outer_records[0]
                    )
                    return 0

                def nested_push(
                    *args: object,
                    _fake: FakeHost = fake,
                    _outer_records: list[str] = outer_records,
                    **kwargs: object,
                ) -> int:
                    del args, kwargs
                    outcome = backuplock.take(_fake, command="backup push", now=NOW)
                    self.assertEqual(outcome.state, backuplock.State.NESTED)
                    self.assertEqual(
                        _fake.locks[backuplock.LOCK_PATH], _outer_records[0]
                    )
                    return 0

                with (
                    patch.object(fake, "take_lock", side_effect=observe_lock),
                    patch.object(restore.backup, "run_backup_run", side_effect=nested_run),
                    patch.object(restore.backup, "run_backup_push", side_effect=nested_push),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    code = restore.run_restore(
                        argparse.Namespace(source=source, at=None),
                        host=fake,
                        now=NOW,
                    )

                self.assertEqual(code, 0)
                self.assertEqual(len(outer_records), 1)
                record = backuplock.parse(outer_records[0])
                self.assertIsNotNone(record)
                assert record is not None
                self.assertEqual(record.command, "restore")
                self.assertEqual(record.started, NOW)
                self.assertEqual(
                    len(nested_returns), 2 if source == "target" else 1
                )
                self.assertTrue(all(value == outer_records[0] for value in nested_returns))
                self.assertNotIn(backuplock.LOCK_PATH, fake.locks)

    def test_fresh_target_skips_pre_restore_for_absent_and_empty_sets(self) -> None:
        for sets_absent in (True, False):
            with self.subTest(sets_absent=sets_absent):
                fake = make_host(
                    running=True,
                    source="target",
                    names=(),
                    sets_absent=sets_absent,
                    remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
                )
                out = io.StringIO()
                with (
                    patch.object(
                        restore.backup,
                        "run_backup_run",
                        side_effect=AssertionError("fresh stack must not take a set"),
                    ),
                    patch.object(
                        restore.backup,
                        "run_backup_push",
                        side_effect=AssertionError("fresh stack must not push a set"),
                    ),
                    contextlib.redirect_stdout(out),
                ):
                    code = restore.run_restore(
                        argparse.Namespace(source="target", at=None),
                        host=fake,
                        now=NOW,
                    )

                self.assertEqual(code, 0, out.getvalue())
                output = out.getvalue()
                self.assertIn(
                    f"pre-restore: ok — {restore.FRESH_STACK_SKIPPED_DETAIL}",
                    output,
                )
                self.assertIn("fetch: ok", output)
                audit_sql = "\n".join(call[1] or "" for call in fake.calls)
                self.assertIn('"pre_restore_label": null', audit_sql)

    def test_incomplete_staging_refuses_before_backup_or_down(self) -> None:
        partial_label = LOCAL_SET + backupset.PARTIAL_SUFFIX
        cases = ((partial_label, None), (LOCAL_SET, "{not a manifest"))
        for label, malformed_manifest in cases:
            with self.subTest(label=label):
                fake = make_host(
                    running=True,
                    source="target",
                    names=(label,),
                    remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
                )
                if malformed_manifest is not None:
                    fake.files[
                        os.path.join(
                            backupset.set_dir(LOCAL_SET), backupset.MANIFEST_NAME
                        )
                    ] = malformed_manifest
                out = io.StringIO()
                with (
                    patch.object(
                        restore.backup,
                        "run_backup_run",
                        side_effect=AssertionError("incomplete staging must not take a set"),
                    ),
                    patch.object(
                        restore.backup,
                        "run_backup_push",
                        side_effect=AssertionError("incomplete staging must not push a set"),
                    ),
                    contextlib.redirect_stdout(out),
                ):
                    code = restore.run_restore(
                        argparse.Namespace(source="target", at=None),
                        host=fake,
                        now=NOW,
                    )

                self.assertEqual(code, 1)
                output = out.getvalue()
                pre_restore = next(
                    line for line in output.splitlines() if line.startswith("pre-restore:")
                )
                self.assertIn(
                    f"no complete backup set in staging, only 1 incomplete entry: {label}",
                    pre_restore,
                )
                self.assertNotIn(backupset.SETS_DIR, pre_restore.split(" Fix:", 1)[0])
                self.assertIn(
                    "Run sudo python3 -m gideon backup run to complete a set",
                    output,
                )
                self.assertIn(
                    "stop the stack with docker compose -f /etc/gideon/rendered/compose.yaml down",
                    output,
                )
                self.assertFalse(
                    any(
                        call[0][:2] == ("docker", "compose")
                        and call[0][-1] == "down"
                        for call in fake.calls
                    )
                )

    def test_pre_restore_push_failure_refuses_before_down(self) -> None:
        fake = make_host(
            running=True,
            source="target",
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
        )
        out = io.StringIO()
        with (
            patch.object(restore.backup, "run_backup_run", return_value=0),
            patch.object(restore.backup, "run_backup_push", return_value=1),
            contextlib.redirect_stdout(out),
        ):
            code = restore.run_restore(
                argparse.Namespace(source="target", at=None),
                host=fake,
                now=NOW,
            )

        self.assertEqual(code, 1)
        self.assertIn("pre-restore push failed", out.getvalue())
        self.assertFalse(any(call[0][-1] == "down" for call in fake.calls))
        self.assertNotIn(backuplock.LOCK_PATH, fake.locks)

    def test_partly_running_fresh_stack_skips_pre_restore(self) -> None:
        fake = make_host(
            running=True,
            partial=True,
            source="target",
            names=(),
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
        )
        out = io.StringIO()
        with (
            patch.object(
                restore.backup,
                "run_backup_run",
                side_effect=AssertionError("fresh stack must not take a set"),
            ),
            patch.object(
                restore.backup,
                "run_backup_push",
                side_effect=AssertionError("fresh stack must not push a set"),
            ),
            contextlib.redirect_stdout(out),
        ):
            restore.run_restore(
                argparse.Namespace(source="target", at=None),
                host=fake,
                now=NOW,
            )

        # The partial refusal protects a safety set a fresh stack cannot have,
        # so the skip wins and the later stop stage runs down itself.
        output = out.getvalue()
        self.assertIn(
            f"pre-restore: ok — {restore.FRESH_STACK_SKIPPED_DETAIL}",
            output,
        )
        self.assertNotIn("partially running", output)

    def test_partly_running_incomplete_staging_refuses_as_partial(self) -> None:
        fake = make_host(
            running=True,
            partial=True,
            source="target",
            names=(LOCAL_SET + backupset.PARTIAL_SUFFIX,),
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="target", at=None),
                host=fake,
                now=NOW,
            )

        self.assertEqual(code, 1)
        output = out.getvalue()
        self.assertIn("partially running (open-webui)", output)
        self.assertNotIn("no complete backup set in staging", output)

    def test_stopped_fresh_stack_keeps_the_existing_skip_row(self) -> None:
        fake = make_host(
            running=False,
            source="target",
            names=(),
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="target", at=None),
                host=fake,
                now=NOW,
            )

        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("pre-restore: ok — skipped (stack not running)", out.getvalue())

    def test_unlistable_staging_refuses_with_the_backup_run_fix(self) -> None:
        fake = make_host(
            running=True,
            source="target",
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
        )
        listable = fake.listdir

        def deny_sets(path: PathLike) -> list[str]:
            if os.fspath(path) == backupset.SETS_DIR:
                raise PermissionError("permission denied")
            return listable(path)

        out = io.StringIO()
        with (
            patch.object(fake, "listdir", side_effect=deny_sets),
            contextlib.redirect_stdout(out),
        ):
            code = restore.run_restore(
                argparse.Namespace(source="target", at=None),
                host=fake,
                now=NOW,
            )

        self.assertEqual(code, 1)
        output = out.getvalue()
        self.assertIn("cannot list backup sets: permission denied", output)
        self.assertIn("Run sudo python3 -m gideon backup run, then retry.", output)

    def test_staging_at_after_refreshed_archive_boundary_refuses_before_down(self) -> None:
        fake = make_host()
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at="2026-09-02T11:30Z"),
                host=fake,
                now=NOW,
            )
        self.assertEqual(code, 1)
        self.assertIn("newest local archive boundary", out.getvalue())
        self.assertFalse(any(call[0][-1] == "down" for call in fake.calls))

    def test_at_reaches_pgbackrest_as_an_explicit_time_target(self) -> None:
        fake = make_host()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at="2026-09-02T11:00Z"),
                host=fake,
                now=NOW,
            )
        self.assertEqual(code, 0)
        restore_call = next(
            call[0]
            for call in fake.calls
            if call[0][0:2] == ("docker", "compose")
            and "pgbackrest" in call[0]
            and "restore" in call[0]
        )
        self.assertIn("--type=time", restore_call)
        self.assertIn("--target=2026-09-02 11:00:00+00:00", restore_call)
        self.assertIn("--target-action=promote", restore_call)
        # Every set restore names its own backup and that backup's timeline:
        # auto-selection and the current timeline both go wrong after a fork.
        self.assertIn(f"--set={manifest().pgbackrest_label}", restore_call)
        self.assertIn("--target-timeline=2", restore_call)

    def test_target_at_after_newest_remote_boundary_refuses_in_select(self) -> None:
        fake = make_host(
            source="target",
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
            remote_record=backupset.PushRecord(
                REMOTE_SNAPSHOT,
                NOW,
                LOCAL_SET,
                NOW - timedelta(hours=1),
            ),
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="target", at="2026-09-02T11:30Z"),
                host=fake,
                now=NOW,
            )
        self.assertEqual(code, 1)
        self.assertIn("newest remote archive boundary", out.getvalue())
        self.assertIn(SELECT_FIX, out.getvalue())

    def test_verify_skips_the_repository_info_files_and_names_the_root(self) -> None:
        fake = make_host()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(restore.run_restore(argparse.Namespace(source="staging", at=None), host=fake, now=NOW), 0)
        repository_checks = [
            call[1] or ""
            for call in fake.calls
            if call[0][:2] == ("sha256sum", "-c") and call[2] == "/data/backup-staging/pgbackrest"
        ]
        self.assertEqual(len(repository_checks), 1)
        self.assertNotIn("backup.info", repository_checks[0])
        self.assertNotIn("archive.info", repository_checks[0])
        self.assertIn("backup/gideon/backup-label/backup.manifest", repository_checks[0])
        failing = make_host(fail_checksum=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            restore.run_restore(argparse.Namespace(source="staging", at=None), host=failing, now=NOW)
        self.assertIn("path(s) in etc-gideon", out.getvalue())

    def test_verify_and_pgbackrest_refusals_happen_before_down(self) -> None:
        for failure in ("checksum", "pgbackrest"):
            with self.subTest(failure=failure):
                fake = make_host(
                    fail_checksum=failure == "checksum",
                    fail_pgbackrest=failure == "pgbackrest",
                )
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = restore.run_restore(
                        argparse.Namespace(source="staging", at=None),
                        host=fake,
                        now=NOW,
                    )
                self.assertEqual(code, 1)
                self.assertFalse(any(call[0][-1] == "down" for call in fake.calls))
                self.assertIn("Fix:", out.getvalue())

    def test_fetch_falls_back_to_one_off_identity_lookup(self) -> None:
        fake = make_host(
            source="target",
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
            side_names=(LOCAL_SET,),
            fail_identity_exec=True,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            code = restore.run_restore(
                argparse.Namespace(source="target", at=None),
                host=fake,
                now=NOW,
            )
        self.assertEqual(code, 0)
        identity_calls = [
            call[0]
            for call in fake.calls
            if call[0][:3] == ("docker", "compose", "--project-directory")
            and "run" in call[0]
            and "id" in call[0]
        ]
        self.assertEqual(len(identity_calls), 2)

    def test_partial_stack_refuses_before_anything_stops(self) -> None:
        fake = make_host(running=True, partial=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(argparse.Namespace(source="staging", at=None), host=fake, now=NOW)
        self.assertEqual(code, 1)
        self.assertIn("partially running (open-webui)", out.getvalue())
        self.assertIn("gideon apply", out.getvalue())
        self.assertFalse(any(call[0][-1] == "down" for call in fake.calls))

    def test_swap_failure_moves_the_live_staging_back(self) -> None:
        fake = make_host(source="target", remote_listing=f"{REMOTE_SNAPSHOT}\t1\n", fail_swap=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(argparse.Namespace(source="target", at=None), host=fake, now=NOW)
        self.assertEqual(code, 1)
        moves = [call[0] for call in fake.calls if call[0][0] == "mv"]
        self.assertEqual(moves[-1], ("mv", f"{backupset.STAGING}.replaced-{REMOTE_SNAPSHOT}", backupset.STAGING))
        self.assertIn("moved back", out.getvalue())
        self.assertFalse(any("pgbackrest" in call[0] and "restore" in call[0] for call in fake.calls))

    def test_symlink_entries_are_reowned_without_dereferencing(self) -> None:
        value = manifest()
        link = backupset.Entry("link-to-elsewhere", "l", 1, 1000, 1000, 0o777, 100.0, None)
        with_link = replace(value, inventory={**value.inventory, "etc-gideon": (*value.inventory["etc-gideon"], link)})
        fake = make_host(manifest_value=with_link)
        with contextlib.redirect_stdout(io.StringIO()):
            code = restore.run_restore(argparse.Namespace(source="staging", at=None), host=fake, now=NOW)
        self.assertEqual(code, 0)
        chowns = [call[0] for call in fake.calls if call[0][0] == "chown"]
        self.assertIn(("chown", "-h", "1000:1000", "--", "/etc/gideon/link-to-elsewhere"), chowns)
        chmods = [call[0] for call in fake.calls if call[0][0] == "chmod"]
        self.assertFalse(any("/etc/gideon/link-to-elsewhere" in argv for argv in chmods))

    def test_a_path_beneath_a_link_refuses_before_any_metadata_is_applied(self) -> None:
        value = manifest()
        with_escape = replace(
            value,
            inventory={
                **value.inventory,
                "etc-gideon": (*value.inventory["etc-gideon"], entry("escape/passwd")),
            },
        )
        for source, listing in (("staging", ""), ("target", f"{REMOTE_SNAPSHOT}\t1\n")):
            with self.subTest(source=source):
                fake = make_host(source=source, remote_listing=listing, manifest_value=with_escape, physical_escape=True)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = restore.run_restore(argparse.Namespace(source=source, at=None), host=fake, now=NOW)
                self.assertEqual(code, 1)
                self.assertIn("beneath a link", out.getvalue())
                self.assertFalse(any("escape/passwd" in call[0] for call in fake.calls if call[0][0] in ("chown", "chmod")))

    def test_a_root_without_a_dot_entry_keeps_the_live_directory_owner(self) -> None:
        fake = make_host()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(restore.run_restore(argparse.Namespace(source="staging", at=None), host=fake, now=NOW), 0)
        chowns = [call[0] for call in fake.calls if call[0][0] == "chown"]
        self.assertIn(("chown", "999:983", "--", "/data/registry"), chowns)
        chmods = [call[0] for call in fake.calls if call[0][0] == "chmod"]
        self.assertIn(("chmod", "0755", "--", "/data/registry"), chmods)

        value = manifest()
        dotted = replace(
            value,
            inventory={
                **value.inventory,
                "data-registry": (
                    backupset.Entry(backupset.ROOT_ENTRY, "d", 4096, 999, 983, 0o755, 100.0, None),
                    *value.inventory["data-registry"],
                ),
            },
        )
        fake = make_host(manifest_value=dotted)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(restore.run_restore(argparse.Namespace(source="staging", at=None), host=fake, now=NOW), 0)
        chowns = [call[0] for call in fake.calls if call[0][0] == "chown"]
        self.assertNotIn(("chown", "999:983", "--", "/data/registry"), chowns)
        self.assertIn(("chown", "999:983", "--", "/data/registry/."), chowns)

    def test_a_path_the_manifest_never_inventoried_refuses_at_verify_before_stop(self) -> None:
        """A snapshotted root is the manifest's set and nothing else, an empty root included."""

        value = manifest()
        empty_registry = replace(value, inventory={**value.inventory, "data-registry": ()})
        fake = make_host(manifest_value=empty_registry, extra_physical_file=("data-registry", "stray.blob"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(argparse.Namespace(source="staging", at=None), host=fake, now=NOW)
        self.assertEqual(code, 1)
        self.assertIn("verify: refuse — 1 path(s) under", out.getvalue())
        self.assertIn("are not in the manifest: stray.blob", out.getvalue())
        self.assertFalse(any(call[0][-1] == "down" for call in fake.calls))

    def test_a_root_with_no_files_is_verified_without_a_checksum_call(self) -> None:
        """/data/registry is empty on a no-GPU host; sha256sum -c refuses an empty list."""

        value = manifest()
        empty_registry = replace(value, inventory={**value.inventory, "data-registry": ()})
        fake = make_host(manifest_value=empty_registry)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(argparse.Namespace(source="staging", at=None), host=fake, now=NOW)
        self.assertEqual(code, 0, out.getvalue())
        registry_checks = [
            call for call in fake.calls
            if call[0][:2] == ("sha256sum", "-c") and str(call[2]).endswith("/data-registry")
        ]
        self.assertEqual(registry_checks, [])

    def test_refusals_and_secret_kept_line(self) -> None:
        for fake, expected in (
            (make_host(), "--from staging"),
            (make_host(target=False, source="target"), "office-services runbook"),
            (make_host(secrets_present=True), "secrets on disk are the set's"),
        ):
            if expected == "--from staging":
                args = argparse.Namespace(source=None, at=None)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = restore.run_restore(args, host=fake, now=NOW)
                self.assertEqual(code, 1)
                self.assertIn(expected, err.getvalue())
            elif expected == "office-services runbook":
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = restore.run_restore(
                        argparse.Namespace(source="target", at=None),
                        host=fake,
                        now=NOW,
                    )
                self.assertEqual(code, 1)
                self.assertIn(expected, err.getvalue())
            else:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = restore.run_restore(
                        argparse.Namespace(source="staging", at=None),
                        host=fake,
                        now=NOW,
                    )
                self.assertEqual(code, 0)
                self.assertIn(expected, out.getvalue())


if __name__ == "__main__":
    unittest.main()


class RestoreBySetLabel(unittest.TestCase):
    """``--set <label>`` (ADR-0005 rollback): the named set, to its own archive boundary."""

    def run_restore(self, fake: FakeHost, **args: Any) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        namespace = argparse.Namespace(source="staging", at=None, set=None)
        for key, value in args.items():
            setattr(namespace, key, value)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = restore.run_restore(namespace, host=fake, now=NOW)
        return code, out.getvalue(), err.getvalue()

    def test_set_selects_the_named_set_and_targets_its_archive_boundary(self) -> None:
        fake = make_host()
        code, out, _ = self.run_restore(fake, set=LOCAL_SET)
        self.assertEqual(code, 0, out)
        boundary = manifest().archive_through
        self.assertIn(
            f"select: ok — source=staging; set={LOCAL_SET}; archive_through={boundary.isoformat()}",
            out,
        )
        self.assertNotIn("after the newest local archive boundary", out)
        restore_call = next(
            call[0]
            for call in fake.calls
            if call[0][0:2] == ("docker", "compose") and "pgbackrest" in call[0] and "restore" in call[0]
        )
        self.assertIn("--type=time", restore_call)
        self.assertIn(f"--target={backupset.pgbackrest_target(boundary)}", restore_call)
        self.assertIn("--target-action=promote", restore_call)
        # The set's own backup, never auto-selected: pgBackRest's rule (stop strictly
        # before the target, at second granularity) would skip it for the older one.
        self.assertIn(f"--set={manifest().pgbackrest_label}", restore_call)
        self.assertIn("--target-timeline=2", restore_call)
        self.assertIn("Next: sudo python3 -m gideon host provision", out)

    def test_target_restore_recovers_the_set_on_its_own_timeline_to_its_boundary(self) -> None:
        """--from target with no --at: the newest snapshot's set, its backup, its timeline, its boundary."""

        fake = make_host(source="target", remote_listing=f"{REMOTE_SNAPSHOT}\t1\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(argparse.Namespace(source="target", at=None), host=fake, now=NOW)
        self.assertEqual(code, 0, out.getvalue())
        restore_call = next(
            call[0]
            for call in fake.calls
            if call[0][0:2] == ("docker", "compose") and "pgbackrest" in call[0] and "restore" in call[0]
        )
        self.assertIn(f"--set={manifest().pgbackrest_label}", restore_call)
        self.assertIn("--target-timeline=2", restore_call)
        self.assertIn("--type=time", restore_call)
        self.assertIn(f"--target={backupset.pgbackrest_target(manifest().archive_through)}", restore_call)
        self.assertIn("--target-action=promote", restore_call)

    def test_a_set_whose_backup_the_repository_lacks_refuses_at_postgres(self) -> None:
        fake = make_host(info_has_label=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(argparse.Namespace(source="staging", at=None), host=fake, now=NOW)
        self.assertEqual(code, 1)
        self.assertIn(f"postgres: refuse — the repository holds no backup {manifest().pgbackrest_label}", out.getvalue())
        self.assertFalse(any("restore" in call[0] and "pgbackrest" in call[0] and "--delta" in call[0] for call in fake.calls))

    def test_set_is_refused_with_at_or_the_target_source(self) -> None:
        for args in ({"set": LOCAL_SET, "at": "2026-09-02T11:00Z"}, {"set": LOCAL_SET, "source": "target"}):
            with self.subTest(args=args):
                fake = make_host()
                code, out, err = self.run_restore(fake, **args)
                self.assertEqual(code, 1)
                self.assertIn(
                    "gideon restore: --set cannot be combined with --at or --from target. "
                    "Fix: Use --at for a point in time; use --from target for the off-box copy.",
                    err,
                )
                self.assertEqual(out, "")
                self.assertEqual(fake.calls, [])

    def test_an_unknown_or_partial_label_refuses_in_select_listing_the_complete_sets(self) -> None:
        for label in ("pre-missing", f"pre-v9.9.9{backupset.PARTIAL_SUFFIX}"):
            with self.subTest(label=label):
                fake = make_host(names=(LOCAL_SET, f"pre-v9.9.9{backupset.PARTIAL_SUFFIX}"))
                code, out, _ = self.run_restore(fake, set=label)
                self.assertEqual(code, 1)
                self.assertIn(
                    f"select: refuse — No complete backup set is named {label}; the complete sets are: {LOCAL_SET}. "
                    "Fix: Choose one of them, then retry sudo python3 -m gideon restore --from staging --set <label>.",
                    out,
                )
                self.assertFalse(any(call[0][-1] == "down" for call in fake.calls))
