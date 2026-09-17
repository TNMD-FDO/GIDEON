"""Backup-run stages over a dict-backed Host seam (spec §19.1)."""

import argparse
import contextlib
import io
import json
import os
import subprocess
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest import mock

from gideon.host import backup, backupset, pgbackrest, secrets, stack
from gideon.host.steps.site_dirs import AGE_IDENTITY_PATH, AGE_RECIPIENT_PATH
from gideon.host.sysio import Command, PathLike

RENDERED = "/etc/gideon/rendered"
SITE_PATH = "/etc/gideon/site.yaml"
CHECKOUT = "/work/GIDEON"
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
RECIPIENT = "age1" + "a" * 58
IDENTITY = "AGE-SECRET-KEY-1" + "A" * 58
GIDEON_IDS = backupset.AccountIds(999, 983)
OLD_LABEL = "20260901T120000Z"
BACKUP_INFO = {
    "backup": [
        {
            "label": "old-full",
            "type": "full",
            "timestamp": {"stop": (NOW - timedelta(days=3)).timestamp()},
        }
    ]
}
FRESH_INFO = {
    "backup": [
        {
            "label": "new-incr",
            "type": "incr",
            "timestamp": {"stop": NOW.timestamp()},
        }
    ]
}

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
    path: /backup
alerts:
  smtp:
    host: smtp.test
    from: gideon@test
  recipients: [csa@test]
"""


def result(
    argv: Sequence[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)


def stat_result(
    inode: int,
    *,
    size: int = 10,
    mtime: float = 100.0,
    mode: int = 0o644,
    uid: int = 1000,
    gid: int = 1000,
) -> os.stat_result:
    return os.stat_result(
        (0o100000 | mode, inode, 0, 1, uid, gid, size, mtime, mtime, mtime)
    )


def entry(path: str, *, sha256: str | None = None, size: int = 10, mtime: float = 100.0) -> backupset.Entry:
    return backupset.Entry(path, "f", size, 1000, 1000, 0o644, mtime, sha256)


def previous_manifest() -> backupset.Manifest:
    return backupset.Manifest(
        1,
        OLD_LABEL,
        backupset.Kind.NIGHTLY,
        NOW - timedelta(days=1, minutes=1),
        NOW - timedelta(days=1),
        "0.0.17",
        CHECKOUT,
        "old-commit",
        "gideon.test",
        None,
        "old-full",
        "full",
        NOW - timedelta(days=1),
        {"gideon": {}, "openwebui": {}},
        {
            "etc-gideon": (entry("unchanged", sha256="a" * 64),),
            "pgbackrest": (entry("old.dat", sha256="e" * 64),),
        },
        "b" * 64,
        (RECIPIENT,),
        backupset.LinkVerdict(1, 1),
        "f" * 64,
    )


class FakeHost:
    """A command queue plus file and stat dictionaries."""

    def __init__(
        self,
        *,
        files: Mapping[str, str] | None = None,
        names: Sequence[str] = (),
        commands: Mapping[tuple[str, ...], Sequence[subprocess.CompletedProcess[str]]] | None = None,
        stats: Mapping[str, os.stat_result] | None = None,
        euid: int = 0,
    ) -> None:
        self.files = dict(files or {})
        self.names = list(names)
        self.commands = {key: list(value) for key, value in (commands or {}).items()}
        self.stats = dict(stats or {})
        self.euid = euid
        self.calls: list[tuple[tuple[str, ...], str | None, float | None]] = []
        self.mkdir_calls: list[str] = []

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
        if command and command[0] == "mv" and len(command) == 3:
            source, destination = command[1:]
            for path in tuple(self.files):
                if path == source or path.startswith(source + "/"):
                    self.files[destination + path[len(source) :]] = self.files.pop(path)
        queue = self.commands.get(command)
        if queue:
            return queue.pop(0)
        prefix_queue = next(
            (value for key, value in self.commands.items() if command[: len(key)] == key and value),
            None,
        )
        if prefix_queue is not None:
            return prefix_queue.pop(0)
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
        self.files[os.fspath(path)] = text

    def exists(self, path: PathLike) -> bool:
        return os.fspath(path) in self.files

    def listdir(self, path: PathLike) -> list[str]:
        if os.fspath(path) == backupset.SETS_DIR:
            return list(self.names)
        return []

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del missing_ok
        self.files.pop(os.fspath(path), None)

    def stat(self, path: PathLike) -> os.stat_result:
        key = os.fspath(path)
        if key not in self.stats:
            raise FileNotFoundError(key)
        return self.stats[key]

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
        del mode, parents, exist_ok
        self.mkdir_calls.append(os.fspath(path))

    def geteuid(self) -> int:
        return self.euid


def _commands(*, old: backupset.Manifest | None = None) -> dict[tuple[str, ...], list[subprocess.CompletedProcess[str]]]:
    info_argv = tuple(pgbackrest.exec_argv(RENDERED, "info", "--output=json"))
    commands: dict[tuple[str, ...], list[subprocess.CompletedProcess[str]]] = {
        ("getent", "passwd", backupset.SERVICE_ACCOUNT): [
            result(
                ("getent", "passwd", backupset.SERVICE_ACCOUNT),
                stdout=(
                    f"{backupset.SERVICE_ACCOUNT}:x:{GIDEON_IDS.uid}:"
                    f"{GIDEON_IDS.gid}::/home/{backupset.SERVICE_ACCOUNT}:/bin/bash\n"
                ),
            )
        ],
        tuple(stack.exec_argv(RENDERED, "postgres", "pg_isready")): [result(())],
        info_argv: [result(info_argv, stdout=json.dumps(BACKUP_INFO)), result(info_argv, stdout=json.dumps(FRESH_INFO))],
        tuple(stack.exec_argv(RENDERED, "postgres", "psql", "-U", "gideon_audit", "-d", "gideon", "-v", "ON_ERROR_STOP=1", "--single-transaction", "-f", "-")): [result(())] * 3,
        tuple(pgbackrest.backup_argv(RENDERED, "full")): [result(())],
        tuple(pgbackrest.backup_argv(RENDERED, "incr")): [result(())],
        tuple(pgbackrest.exec_argv(RENDERED, "check")): [result(())],
        ("age-keygen", "-y", os.fspath(AGE_IDENTITY_PATH)): [
            result((), stdout="age1" + "c" * 58 + "\n")
        ],
        tuple(stack.exec_argv(RENDERED, "postgres", "psql", "-U", "postgres", "-d", "gideon", "-tA", "-f", "-")): [
            result((), stdout="public.users\ninvalid-name\n"),
            result((), stdout="public.users|3\n"),
        ],
        tuple(stack.exec_argv(RENDERED, "postgres", "psql", "-U", "postgres", "-d", "openwebui", "-tA", "-f", "-")): [
            result((), stdout="main.chats\n"),
            result((), stdout="main.chats|4\n"),
        ],
        ("sha256sum", os.path.join(backupset.partial_dir("20260902T120000Z"), backupset.TARBALL_NAME)): [
            result((), stdout="" + "c" * 64 + "  " + os.path.join(backupset.partial_dir("20260902T120000Z"), backupset.TARBALL_NAME) + "\n")
        ],
        ("git", "-c", f"safe.directory={CHECKOUT}", "-C", CHECKOUT, "rev-parse", "HEAD"): [result((), stdout="new-commit\n")],
        ("find", "/etc/gideon/secrets", "-type", "f", "-exec", "sha256sum", "{}", "+"): [
            result((), stdout=f"{'1' * 64}  /etc/gideon/secrets/webui_secret_key\n{'2' * 64}  /etc/gideon/secrets/ldap_bind_password\n")
        ],
        ("mv", backupset.partial_dir("20260902T120000Z"), backupset.set_dir("20260902T120000Z")): [result(())],
    }
    for inventory_root in backupset.inventory_roots(CHECKOUT):
        directory = (
            os.path.join(backupset.partial_dir("20260902T120000Z"), backupset.FILES_DIR, inventory_root.name)
            if inventory_root.snapshotted
            else inventory_root.source
        )
        if inventory_root.name == "etc-gideon":
            records = "d\t4096\t1000\t1000\t755\t100.0\t\0f\t10\t1000\t1000\t644\t100.0\tunchanged\0"
            file_name = "unchanged"
        elif inventory_root.name == "pgbackrest":
            records = (
                "d\t4096\t1000\t1000\t755\t100.0\t\0"
                "f\t10\t1000\t1000\t644\t100.0\tbackup.info\0"
                "f\t10\t1000\t1000\t644\t100.0\told.dat\0"
                "f\t10\t1000\t1000\t644\t101.0\tnew.dat\0"
            )
            file_name = ""
        else:
            records = "d\t4096\t1000\t1000\t755\t100.0\t\0f\t5\t1000\t1000\t644\t101.0\tfile.txt\0"
            file_name = "file.txt"
        find_argv: tuple[str, ...] = ("find", directory, "-printf", backupset.FIND_FORMAT)
        commands[find_argv] = [result(find_argv, stdout=records)]
        if inventory_root.snapshotted:
            hash_argv = ("find", directory, "-type", "f", "-exec", "sha256sum", "{}", "+")
            commands[hash_argv] = [
                result(hash_argv, stdout=f"{'a' * 64}  {directory}/{file_name}\n")
            ]
        else:
            repository_paths = sorted(
                os.path.join(directory, name)
                for name in ("backup.info", "old.dat", "new.dat")
                if old is None or name != "old.dat"
            )
            repository_hash_argv: tuple[str, ...] = ("sha256sum", "--", *repository_paths)
            commands[repository_hash_argv] = [
                result(
                    repository_hash_argv,
                    stdout="".join(f"{'d' * 64}  {path}\n" for path in repository_paths),
                )
            ]
    if old is not None:
        commands[tuple(pgbackrest.backup_argv(RENDERED, "incr"))] = [result(())]
    return commands


def _host(*, previous: bool = False, euid: int = 0) -> FakeHost:
    files = {
        SITE_PATH: SITE,
        f"{RENDERED}/compose.yaml": "services: {}\n",
        os.fspath(AGE_RECIPIENT_PATH): RECIPIENT + "\n",
        os.fspath(AGE_IDENTITY_PATH): IDENTITY + "\n",
    }
    names: list[str] = []
    stats: dict[str, os.stat_result] = {}
    old = previous_manifest() if previous else None
    if old is not None:
        names.append(OLD_LABEL)
        files[os.path.join(backupset.set_dir(OLD_LABEL), backupset.MANIFEST_NAME)] = old.to_json()
        stats[os.path.join(backupset.partial_dir("20260902T120000Z"), backupset.FILES_DIR, "etc-gideon", "unchanged")] = stat_result(7)
        stats[os.path.join(backupset.set_dir(OLD_LABEL), backupset.FILES_DIR, "etc-gideon", "unchanged")] = stat_result(7)
    for path in ("/usr/bin/bash", "/usr/bin/age", "/usr/bin/age-keygen", "/usr/bin/rsync"):
        files[path] = ""
    stats[os.fspath(AGE_IDENTITY_PATH)] = stat_result(
        9, mode=0o400, uid=0, gid=0
    )
    return FakeHost(files=files, names=names, commands=_commands(old=old), stats=stats, euid=euid)


class Preconditions(unittest.TestCase):
    def test_root_refusal(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = backup.run_backup_run(argparse.Namespace(), host=_host(euid=1000), now=NOW)
        self.assertEqual(code, 1)
        self.assertIn("root", err.getvalue())

    def test_missing_prerequisite_refusals_name_their_fixes(self) -> None:
        cases = (
            (SITE_PATH, "site file", "Correct the site file"),
            (f"{RENDERED}/compose.yaml", "rendered Compose", "gideon apply"),
            ("/usr/bin/rsync", "backup tool", "host provision --only host-tools"),
            ("/usr/bin/age-keygen", "backup tool", "host provision --only host-tools"),
            (os.fspath(AGE_RECIPIENT_PATH), "age recipient", "age-recipient"),
            (os.fspath(AGE_IDENTITY_PATH), "box identity", "age-identity"),
        )
        for path, problem, fix in cases:
            with self.subTest(path=path):
                host = _host()
                host.files.pop(path)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = backup.run_backup_run(argparse.Namespace(), host=host, now=NOW)
                self.assertEqual(code, 1)
                self.assertIn(problem, err.getvalue())
                self.assertIn(fix, err.getvalue())

    def test_identity_custody_refusals_name_the_step(self) -> None:
        for details in (stat_result(9, mode=0o644, uid=0, gid=0), stat_result(9, mode=0o400, uid=1000, gid=1000)):
            with self.subTest(details=details):
                host = _host()
                host.stats[os.fspath(AGE_IDENTITY_PATH)] = details
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = backup.run_backup_run(argparse.Namespace(), host=host, now=NOW)
                self.assertEqual(code, 1)
                self.assertIn("box identity is not root-only", err.getvalue())
                self.assertIn("host provision --only age-identity", err.getvalue())

    def test_identity_derivation_refusals_hide_child_stderr(self) -> None:
        command = ("age-keygen", "-y", os.fspath(AGE_IDENTITY_PATH))
        for child in (
            result(command, returncode=1, stderr="DERIVE-CHILD-ERROR"),
            result(command, stdout="not-an-age-recipient\n", stderr="DERIVE-CHILD-ERROR"),
        ):
            with self.subTest(child=child):
                host = _host()
                host.commands[command] = [child]
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = backup.run_backup_run(argparse.Namespace(), host=host, now=NOW)
                self.assertEqual(code, 1)
                self.assertIn("box identity cannot be read as an age identity", err.getvalue())
                self.assertIn("Delete /etc/gideon/backup_age_identity", err.getvalue())
                self.assertNotIn("DERIVE-CHILD-ERROR", err.getvalue())

    def test_postgres_info_and_audit_refusals(self) -> None:
        for command, fix_text in (
            (tuple(stack.exec_argv(RENDERED, "postgres", "pg_isready")), "gideon apply"),
            (tuple(pgbackrest.exec_argv(RENDERED, "info", "--output=json")), "gideon apply"),
            (tuple(stack.exec_argv(RENDERED, "postgres", "psql", "-U", "gideon_audit", "-d", "gideon", "-v", "ON_ERROR_STOP=1", "--single-transaction", "-f", "-")), "logs postgres"),
        ):
            with self.subTest(command=command):
                host = _host()
                host.commands[command] = [result(command, returncode=1)]
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = backup.run_backup_run(argparse.Namespace(), host=host, now=NOW)
                self.assertEqual(code, 1)
                self.assertIn(fix_text, err.getvalue())

    def test_malformed_recipient_refuses_with_provision_fix(self) -> None:
        host = _host()
        host.files[os.fspath(AGE_RECIPIENT_PATH)] = "not-an-age-recipient\n"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = backup.run_backup_run(argparse.Namespace(), host=host, now=NOW)
        self.assertEqual(code, 1)
        self.assertIn("age recipient is malformed", err.getvalue())
        self.assertIn("host provision --only age-recipient", err.getvalue())

    def test_label_requires_operator_grammar(self) -> None:
        host = _host()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = backup.run_backup_run(
                argparse.Namespace(label="20260902T120000Z", full=False),
                host=host,
                now=NOW,
            )
        self.assertEqual(code, 1)
        self.assertIn("operator label", err.getvalue())
        self.assertIn("pre-[A-Za-z0-9._-]+", err.getvalue())

    def test_nightly_collision_advances_one_second_and_partial_collides(self) -> None:
        complete = backupset.SetRef(
            "20260902T120000Z", "/sets/complete", NOW, True
        )
        partial = backupset.SetRef(
            "20260902T120001Z.partial", "/sets/partial", None, False
        )
        chosen = backup._choose_label(
            args=argparse.Namespace(label=None), now=NOW, sets=(complete, partial)
        )
        self.assertEqual(chosen, ("20260902T120002Z", backupset.Kind.NIGHTLY))


class BackupRun(unittest.TestCase):
    def test_full_run_orders_stages_and_writes_manifest(self) -> None:
        host = _host()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = backup.run_backup_run(
                argparse.Namespace(full=True, label=None),
                host=host,
                root=CHECKOUT,
                now=NOW,
            )
        self.assertEqual(code, 0)
        self.assertEqual(
            [line.split(":", 1)[0] for line in out.getvalue().splitlines()],
            ["intent", "files", "secrets", "postgres", "counts", "manifest", "prune", "applied"],
        )
        manifest_path = os.path.join(backupset.set_dir("20260902T120000Z"), backupset.MANIFEST_NAME)
        parsed = backupset.parse_manifest(host.files[manifest_path])
        self.assertIsInstance(parsed, backupset.Manifest)
        assert isinstance(parsed, backupset.Manifest)
        self.assertEqual(parsed.archive_through, NOW)
        self.assertEqual(parsed.commit, "new-commit")
        self.assertEqual(parsed.recipients, (RECIPIENT, "age1" + "c" * 58))
        self.assertEqual(parsed.gideon_ids, GIDEON_IDS)
        self.assertEqual(
            json.loads(host.files[manifest_path])["gideon_ids"],
            {"gid": GIDEON_IDS.gid, "uid": GIDEON_IDS.uid},
        )
        self.assertEqual(json.loads(host.files[manifest_path])["recipient"], RECIPIENT)
        self.assertEqual(json.loads(host.files[manifest_path])["recipients"], [RECIPIENT, "age1" + "c" * 58])
        self.assertEqual(
            parsed.secrets_fingerprint,
            secrets.secrets_fingerprint({"webui_secret_key": "1" * 64, "ldap_bind_password": "2" * 64}),
        )
        self.assertEqual(parsed.row_counts, {"gideon": {"public.users": 3}, "openwebui": {"main.chats": 4}})
        rsync = [call[0] for call in host.calls if call[0][0] == "rsync"]
        self.assertEqual(len(rsync), 4)
        etc = next(argv for argv in rsync if any("/etc-gideon/" in value for value in argv))
        self.assertIn("--exclude=secrets/", etc)
        self.assertNotIn("--link-dest=/data/backup-staging/sets/", " ".join(etc))
        bash = next(call for call in host.calls if call[0][:2] == ("bash", "-c"))
        self.assertIn("set -o pipefail;", bash[0][2])
        self.assertIn("age -r " + RECIPIENT + " -r age1" + "c" * 58, bash[0][2])
        self.assertIn("secrets: ok — secrets encrypted into the backup set for 2 recipients", out.getvalue())
        self.assertNotIn("secrets.tar.age", bash[0][2].split("tar -C", 1)[0])
        self.assertIn(("mv", backupset.partial_dir("20260902T120000Z"), backupset.set_dir("20260902T120000Z")), [call[0] for call in host.calls])
        expected_rsync = {
            (
                "rsync",
                "-a",
                "--exclude=secrets/",
                "--exclude=rendered/open-webui/env",
                "--exclude=rendered/searxng/env",
                "--exclude=no-gpu",
                "--exclude=build-box",
                "--exclude=backup_age_identity",
                "/etc/gideon/",
                f"{backupset.partial_dir('20260902T120000Z')}/files/etc-gideon/",
            ),
            (
                "rsync",
                "-a",
                *(f"--exclude={pattern}" for pattern in backupset.CHECKOUT_EXCLUSIONS),
                f"{CHECKOUT}/",
                f"{backupset.partial_dir('20260902T120000Z')}/files/checkout/",
            ),
            (
                "rsync",
                "-a",
                "/data/registry/",
                f"{backupset.partial_dir('20260902T120000Z')}/files/data-registry/",
            ),
            (
                "rsync",
                "-a",
                "/data/bulk/openwebui/",
                f"{backupset.partial_dir('20260902T120000Z')}/files/data-bulk-openwebui/",
            ),
        }
        self.assertEqual(set(rsync), expected_rsync)
        audit_inputs = [
            call[1]
            for call in host.calls
            if call[0][0] == "docker"
            and "gideon_audit" in call[0]
            and call[1] is not None
        ]
        self.assertEqual(len(audit_inputs), 3)
        self.assertIn('"phase": "intent"', audit_inputs[1] or "")
        self.assertIn('"phase": "applied"', audit_inputs[2] or "")
        psql_inputs = [
            call[1]
            for call in host.calls
            if call[0][0] == "docker"
            and "-U" in call[0]
            and "postgres" in call[0]
            and call[1] is not None
        ]
        self.assertIn(
            "SELECT schemaname||'.'||relname FROM pg_stat_user_tables "
            "ORDER BY schemaname, relname;\n",
            psql_inputs,
        )
        self.assertIn(
            "SELECT 'public.users', count(*) FROM public.users;\n", psql_inputs
        )
        self.assertNotIn("invalid-name", "".join(psql_inputs))

    def test_missing_or_malformed_gideon_account_refuses_before_intent(self) -> None:
        command = ("getent", "passwd", backupset.SERVICE_ACCOUNT)
        cases = (
            result(command, returncode=1),
            result(
                command,
                stdout=(
                    f"{backupset.SERVICE_ACCOUNT}:x:not-a-uid:983::/home/"
                    f"{backupset.SERVICE_ACCOUNT}:/bin/bash\n"
                ),
            ),
        )
        for response in cases:
            with self.subTest(response=response):
                host = _host()
                host.commands[command] = [response]
                out = io.StringIO()
                err = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = backup.run_backup_run(
                        argparse.Namespace(full=True, label=None),
                        host=host,
                        root=CHECKOUT,
                        now=NOW,
                    )
                self.assertEqual(code, 1)
                self.assertEqual(out.getvalue(), "")
                self.assertFalse(any(call[0][0] == "docker" and "gideon_audit" in call[0] for call in host.calls))
                self.assertIn(
                    f"the {backupset.SERVICE_ACCOUNT} service account is missing or malformed.",
                    err.getvalue(),
                )
                self.assertIn(
                    "Run sudo python3 -m gideon host provision, then retry.",
                    err.getvalue(),
                )

    def test_repository_carries_forward_only_unchanged_hashes(self) -> None:
        host = _host(previous=True)
        code = backup.run_backup_run(
            argparse.Namespace(full=True, label=None),
            host=host,
            root=CHECKOUT,
            now=NOW,
        )
        self.assertEqual(code, 0)
        manifest_path = os.path.join(backupset.set_dir("20260902T120000Z"), backupset.MANIFEST_NAME)
        parsed = backupset.parse_manifest(host.files[manifest_path])
        self.assertIsInstance(parsed, backupset.Manifest)
        assert isinstance(parsed, backupset.Manifest)
        repository = {item.path: item for item in parsed.inventory["pgbackrest"]}
        self.assertEqual(repository["old.dat"].sha256, "e" * 64)
        self.assertEqual(repository["backup.info"].sha256, "d" * 64)
        self.assertEqual(repository["new.dat"].sha256, "d" * 64)
        self.assertNotIn(
            ("sha256sum", "--", os.path.join(backupset.REPOSITORY_PATH, "old.dat")),
            [call[0] for call in host.calls],
        )

    def test_incremental_rule_and_link_dest(self) -> None:
        host = _host(previous=True)
        # The previous repository is useful to carry forward, while the one
        # unchanged snapshotted file proves --link-dest and the verdict.
        for path in (
            os.path.join(backupset.partial_dir("20260902T120000Z"), backupset.FILES_DIR, "etc-gideon", "unchanged"),
            os.path.join(backupset.set_dir(OLD_LABEL), backupset.FILES_DIR, "etc-gideon", "unchanged"),
        ):
            host.stats[path] = stat_result(7)
        # The default FakeHost reports all other commands successful; this
        # test focuses on the generated rsync and pgBackRest argv.
        code = backup.run_backup_run(
            argparse.Namespace(full=False, label=None),
            host=host,
            root=CHECKOUT,
            now=NOW,
        )
        self.assertEqual(code, 0)
        rsync = [call[0] for call in host.calls if call[0][0] == "rsync"]
        self.assertTrue(any("--link-dest=/data/backup-staging/sets/20260901T120000Z" in value for value in rsync[0]))
        self.assertTrue(any("--type=incr" in call[0] for call in host.calls if call[0][0] == "docker" and "pgbackrest" in call[0]))

    def test_labelled_postgres_backup_is_full_and_disables_expire_auto(self) -> None:
        host = _host()
        info = pgbackrest.InfoResult(
            infos=(
                pgbackrest.BackupInfo(
                    "old-full", "full", (NOW - timedelta(days=3)).timestamp()
                ),
            )
        )
        stage, backup_stage = backup._postgres_stage(
            host,
            RENDERED,
            info=info,
            full_requested=True,
            kind=backupset.Kind.LABELLED,
            now=NOW,
            now_was_supplied=True,
        )
        self.assertTrue(stage.ok)
        self.assertIsNotNone(backup_stage)
        backup_calls = [
            call[0]
            for call in host.calls
            if call[0][0] == "docker" and "pgbackrest" in call[0] and "backup" in call[0]
        ]
        self.assertEqual(
            backup_calls,
            [tuple(pgbackrest.backup_argv(RENDERED, "full", expire_auto=False))],
        )

    def test_backup_type_uses_seven_day_full_boundary(self) -> None:
        no_full = pgbackrest.InfoResult(infos=())
        three_days = pgbackrest.InfoResult(
            infos=(
                pgbackrest.BackupInfo(
                    "full-3d", "full", (NOW - timedelta(days=3)).timestamp()
                ),
            )
        )
        eight_days = pgbackrest.InfoResult(
            infos=(
                pgbackrest.BackupInfo(
                    "full-8d", "full", (NOW - timedelta(days=8)).timestamp()
                ),
            )
        )
        self.assertEqual(
            backup._backup_type(no_full.infos, full_requested=False, now=NOW), "full"
        )
        self.assertEqual(
            backup._backup_type(
                three_days.infos, full_requested=False, now=NOW
            ),
            "incr",
        )
        self.assertEqual(
            backup._backup_type(
                eight_days.infos, full_requested=False, now=NOW
            ),
            "full",
        )

    def test_hard_link_verdict_failure_stops_before_secrets(self) -> None:
        host = _host(previous=True)
        current_path = os.path.join(
            backupset.partial_dir("20260902T120000Z"),
            backupset.FILES_DIR,
            "etc-gideon",
            "unchanged",
        )
        host.stats[current_path] = stat_result(8)
        err = io.StringIO()
        with contextlib.redirect_stdout(err):
            code = backup.run_backup_run(
                argparse.Namespace(full=False, label=None),
                host=host,
                root=CHECKOUT,
                now=NOW,
            )
        self.assertEqual(code, 1)
        self.assertIn("no hard links to the previous set", err.getvalue())
        self.assertFalse(any(call[0][:2] == ("bash", "-c") for call in host.calls))

    def test_prune_removes_candidates_and_refuses_unsafe_paths(self) -> None:
        old = replace(
            previous_manifest(), finished=NOW - timedelta(days=8)
        )
        host = FakeHost(
            names=[OLD_LABEL],
            files={
                os.path.join(backupset.set_dir(OLD_LABEL), backupset.MANIFEST_NAME): old.to_json()
            },
        )
        stage, count = backup._prune_stage(
            host,
            now=NOW,
            local_days=7,
            final=backupset.set_dir("new"),
        )
        self.assertTrue(stage.ok)
        self.assertEqual(count, 1)
        self.assertIn(
            ("rm", "-rf", backupset.set_dir(OLD_LABEL)),
            [call[0] for call in host.calls],
        )

        with mock.patch.object(
            backup.backupset,
            "prune_candidates",
            return_value=("/tmp/not-a-set",),
        ):
            unsafe, unsafe_count = backup._prune_stage(
                host,
                now=NOW,
                local_days=7,
                final=backupset.set_dir("new"),
            )
        self.assertFalse(unsafe.ok)
        self.assertEqual(unsafe_count, 0)
        self.assertIn("outside /data/backup-staging/sets", unsafe.detail)

    def test_failed_tar_stops_before_pgbackrest(self) -> None:
        host = _host()
        bash_command = ("bash", "-c")
        # Identify the generated command after the run starts by making every
        # bash invocation fail in the fake.
        host.commands[bash_command] = [result(bash_command, returncode=1)]
        code = backup.run_backup_run(argparse.Namespace(full=True, label=None), host=host, root=CHECKOUT, now=NOW)
        self.assertEqual(code, 1)
        self.assertFalse(any(call[0][0] == "docker" and "pgbackrest" in call[0] and "backup" in call[0] for call in host.calls))
