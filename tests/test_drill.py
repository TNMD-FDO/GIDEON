"""Backup-drill orchestration contracts over a dict-backed host seam."""

import argparse
import base64
import contextlib
import hashlib
import io
import os
import subprocess
import unittest
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import yaml  # type: ignore[import-untyped]

from gideon.host import backupset, drill, owui, stack
from gideon.host.render import ARTIFACTS
from gideon.host.render.drill import DRILL_ROOT
from gideon.host.sysio import Command, PathLike

ROOT = Path(__file__).resolve().parent.parent
RENDERED = "/etc/gideon/rendered"
SITE_PATH = "/etc/gideon/site.yaml"
SET_LABEL = "20260902T110000Z"
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
TARBALL = b"drill tarball"
TARBALL_SHA256 = hashlib.sha256(TARBALL).hexdigest()
RECIPIENT = "age1" + "a" * 58
AGE_HEADER = (
    b"age-encryption.org/v1\n"
    b"-> X25519 abcdef\n"
    b"wrapped-key\n"
    b"--- mac\n"
    b"ciphertext"
)
AGE_HEADER_TWO = (
    b"age-encryption.org/v1\n"
    b"-> X25519 abcdef\n"
    b"wrapped-key\n"
    b"-> X25519 ghijkl\n"
    b"wrapped-key\n"
    b"--- mac\n"
    b"ciphertext"
)

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

TEMPLATE_PATHS = tuple(
    dict.fromkeys(
        path
        for artifact in ARTIFACTS
        for path in artifact.template_paths
    )
)


def completed(
    argv: Sequence[str], *, returncode: int = 0, stdout: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), returncode, stdout, "")


def entry(path: str, *, digest: str = "a" * 64) -> backupset.Entry:
    return backupset.Entry(path, "f", 10, 1000, 1000, 0o644, 100.0, digest)


def make_manifest() -> backupset.Manifest:
    return backupset.Manifest(
        1,
        SET_LABEL,
        backupset.Kind.NIGHTLY,
        NOW - timedelta(hours=2),
        NOW - timedelta(hours=1),
        "0.0.18",
        os.fspath(ROOT),
        "commit",
        "gideon.test",
        None,
        "20260902-110000F",
        "full",
        NOW - timedelta(hours=1),
        {
            "gideon": {"public.users": 2},
            "openwebui": {"public.files": 1},
        },
        {
            "etc-gideon": (entry("config.yaml"),),
            "checkout": (entry("gideon/__init__.py"),),
            "data-registry": (entry("image.blob"),),
            "data-bulk-openwebui": (entry("uploads/data.bin"),),
            "pgbackrest": (entry("backup.info"),),
        },
        TARBALL_SHA256,
        (RECIPIENT, "age1" + "b" * 58),
        backupset.LinkVerdict(1, 1),
        "b" * 64,
    )


class FakeClient:
    def __init__(self, ready: bool) -> None:
        self.ready = ready

    def request(self, method: str, path: str) -> owui.Response:
        del method, path
        return owui.Response(200, {"status": self.ready})


class FakeHost:
    """A recording Host with files and command outcomes in dictionaries."""

    def __init__(
        self,
        *,
        manifest: backupset.Manifest | None = None,
        euid: int = 0,
        leftover: bool = False,
        fail_audit: bool = False,
        fail_tarball: bool = False,
        fail_age_probe: bool = False,
        fail_verify: bool = False,
        fail_postgres: bool = False,
        fail_counts: bool = False,
    ) -> None:
        self.manifest = manifest or make_manifest()
        self.euid = euid
        self.fail_audit = fail_audit
        self.fail_tarball = fail_tarball
        self.fail_age_probe = fail_age_probe
        self.fail_verify = fail_verify
        self.fail_postgres = fail_postgres
        self.fail_counts = fail_counts
        self.calls: list[
            tuple[tuple[str, ...], str | None, str | None, float | None]
        ] = []
        self.writes: list[tuple[str, str, int]] = []
        self.mkdir_calls: list[tuple[str, int]] = []
        self.chown_calls: list[tuple[str, int, int]] = []
        self.files: dict[str, str] = {
            SITE_PATH: SITE,
            os.path.join(RENDERED, "compose.yaml"): "services: {}\n",
            "/usr/bin/bash": "",
            os.path.join(
                backupset.set_dir(SET_LABEL), backupset.MANIFEST_NAME
            ): self.manifest.to_json(),
        }
        if leftover:
            self.files[os.path.join(DRILL_ROOT, "compose.yaml")] = "old\n"
        for relative in ("host.lock", "images.lock", "models.lock"):
            path = ROOT / relative
            self.files[os.fspath(path)] = path.read_text(encoding="utf-8")
        for relative in TEMPLATE_PATHS:
            path = ROOT / "compose" / relative
            self.files[os.fspath(path)] = path.read_text(encoding="utf-8")

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
        self.calls.append(
            (command, input, None if cwd is None else os.fspath(cwd), timeout)
        )
        if command and command[0] == "docker" and "gideon_audit" in command:
            return completed(command, returncode=int(self.fail_audit))
        if "id" in command:
            value = "1000" if "-u" in command else "1001"
            return completed(command, stdout=value + "\n")
        if command and command[0] == "sha256sum":
            if len(command) > 1 and command[1] == "-c":
                return completed(command)
            path = command[-1]
            digest = "0" * 64 if self.fail_tarball else TARBALL_SHA256
            return completed(command, stdout=f"{digest}  {path}\n")
        if command[:2] == ("bash", "-c"):
            return completed(
                command,
                returncode=int(self.fail_age_probe),
                stdout="" if self.fail_age_probe else base64.b64encode(AGE_HEADER_TWO).decode(),
            )
        if "pgbackrest" in command and "verify" in command:
            return completed(command, returncode=int(self.fail_verify))
        if "pgbackrest" in command and "restore" in command:
            return completed(command, returncode=int(self.fail_postgres))
        if command and command[0] == "docker" and "ps" in command:
            return completed(
                command,
                stdout='[{"Service":"postgres","State":"running","Health":"healthy"}]',
            )
        if "psql" in command and "-d" in command:
            database = command[command.index("-d") + 1]
            if "gideon_audit" in command:
                return completed(command)
            if self.fail_counts and database == "gideon":
                return completed(command, stdout="public.users|3\n")
            output = "public.users|2\n" if database == "gideon" else "public.files|1\n"
            return completed(command, stdout=output)
        return completed(command)

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
        del encoding
        key = os.fspath(path)
        self.files[key] = text
        self.writes.append((key, text, mode))

    def exists(self, path: PathLike) -> bool:
        return os.fspath(path) in self.files

    def listdir(self, path: PathLike) -> list[str]:
        if os.fspath(path) == backupset.SETS_DIR:
            return [SET_LABEL]
        raise FileNotFoundError(os.fspath(path))

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del missing_ok
        self.files.pop(os.fspath(path), None)

    def stat(self, path: PathLike) -> os.stat_result:
        raise FileNotFoundError(os.fspath(path))

    def chmod(self, path: PathLike, mode: int) -> None:
        del path, mode

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        self.chown_calls.append((os.fspath(path), uid, gid))

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del parents, exist_ok
        self.mkdir_calls.append((os.fspath(path), mode))

    def geteuid(self) -> int:
        return self.euid


def run(
    host: FakeHost,
    *,
    ready: bool = True,
    now: datetime = NOW,
) -> int:
    return drill.run_backup_drill(
        argparse.Namespace(),
        host=host,
        root=ROOT,
        now=now,
        sleep=lambda seconds: None,
        client_factory=lambda: cast(owui.Client, FakeClient(ready)),
    )


class Header(unittest.TestCase):
    def test_age_header_shapes(self) -> None:
        self.assertIsNone(drill.parse_age_header(AGE_HEADER, 1))
        self.assertIsNone(drill.parse_age_header(AGE_HEADER_TWO, 2))
        one_against_two = drill.parse_age_header(AGE_HEADER, 2)
        self.assertIsNotNone(one_against_two)
        assert one_against_two is not None
        self.assertIn("1 X25519", one_against_two.problem)
        self.assertIn("manifest names 2", one_against_two.problem)
        two_against_one = drill.parse_age_header(AGE_HEADER_TWO, 1)
        self.assertIsNotNone(two_against_one)
        assert two_against_one is not None
        self.assertIn("2 X25519", two_against_one.problem)
        self.assertIn("manifest names 1", two_against_one.problem)
        self.assertIsNotNone(drill.parse_age_header(b"wrong\n-> X25519 x\n---", 1))
        self.assertIsNotNone(drill.parse_age_header(b"age-encryption.org/v1\n---", 1))
        self.assertIsNotNone(
            drill.parse_age_header(b"age-encryption.org/v1\n-> X25519 x\nbody", 1)
        )


class Preconditions(unittest.TestCase):
    def test_refusals_name_their_fixes(self) -> None:
        cases = (
            ("root", FakeHost(euid=1000), "backup drill as root"),
            ("site", FakeHost(), "Create /etc/gideon/site.yaml"),
            ("compose", FakeHost(), "gideon apply"),
            ("bash", FakeHost(), "host provision --only host-tools"),
            ("set", FakeHost(), "backup run"),
            ("audit", FakeHost(), "logs postgres"),
        )
        cases[1][1].files.pop(SITE_PATH)
        cases[2][1].files.pop(os.path.join(RENDERED, "compose.yaml"))
        cases[3][1].files.pop("/usr/bin/bash")
        cases[4][1].files.pop(
            os.path.join(backupset.set_dir(SET_LABEL), backupset.MANIFEST_NAME)
        )
        cases[5][1].fail_audit = True
        for name, host, expected in cases:
            with self.subTest(name=name):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = run(host)
                self.assertEqual(code, 1)
                self.assertIn(expected, err.getvalue())


class DrillContracts(unittest.TestCase):
    def test_document_prepare_identity_upload_and_teardown(self) -> None:
        host = FakeHost(leftover=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run(host), 0)
        document = yaml.safe_load(host.files[os.path.join(DRILL_ROOT, "compose.yaml")])
        self.assertEqual(document["name"], "gideon-drill")
        self.assertNotIn("command", document["services"]["postgres"])
        self.assertTrue(
            any(
                value.startswith(
                    "/data/backup-staging/pgbackrest:"
                    "/data/backup-staging/pgbackrest:ro"
                )
                for value in document["services"]["postgres"]["volumes"]
            )
        )
        self.assertEqual(document["services"]["open-webui"]["ports"], ["127.0.0.1:18090:8080"])
        self.assertIn((os.path.join(DRILL_ROOT, "postgres"), 0o750), host.mkdir_calls)
        self.assertIn((os.path.join(DRILL_ROOT, "openwebui"), 0o755), host.mkdir_calls)
        self.assertIn((os.path.join(DRILL_ROOT, "postgres"), 1000, 1001), host.chown_calls)
        self.assertIn(
            (
                "rsync",
                "-a",
                f"{backupset.set_dir(SET_LABEL)}/files/data-bulk-openwebui/",
                "/data/drill/openwebui/",
            ),
            [call[0] for call in host.calls],
        )
        commands = [call[0] for call in host.calls]
        self.assertEqual(sum(command[0:2] == ("docker", "compose") and "down" in command for command in commands), 2)
        for name in ("postgres", "openwebui", "compose.yaml"):
            self.assertIn(("rm", "-rf", f"{DRILL_ROOT}/{name}"), commands)
        self.assertIn("cas-walk: ok — inert", out.getvalue())
        self.assertIn("retrieval-read: ok — inert", out.getvalue())

    def test_tarball_bash_probe_and_verify_restore_argv(self) -> None:
        host = FakeHost()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run(host), 0)
        bash = next(call[0] for call in host.calls if call[0][:2] == ("bash", "-c"))
        self.assertIn("set -o pipefail", bash[2])
        self.assertIn("base64 -w0", bash[2])
        self.assertIn("secrets.tar.age", bash[2])
        self.assertIn("tarball hash and age header passed for 2 recipient stanza(s)", out.getvalue())
        self.assertNotIn("tar -", bash[2])
        verify = next(
            call[0] for call in host.calls if "pgbackrest" in call[0] and "verify" in call[0]
        )
        self.assertEqual(
            verify,
            tuple(
                stack.compose_argv(
                    DRILL_ROOT,
                    "run",
                    "--rm",
                    "--no-deps",
                    "-u",
                    "postgres",
                    "postgres",
                    "pgbackrest",
                    "--stanza=gideon",
                    "verify",
                )
            ),
        )
        restore = next(
            call[0] for call in host.calls if "pgbackrest" in call[0] and "restore" in call[0]
        )
        self.assertIn("--set=20260902-110000F", restore)
        self.assertIn("--type=immediate", restore)
        self.assertIn("--archive-mode=off", restore)

    def test_tarball_and_verify_fail_before_postgres_and_teardown_runs(self) -> None:
        for failure in ("tarball", "verify"):
            host = FakeHost(
                fail_tarball=failure == "tarball",
                fail_verify=failure == "verify",
            )
            self.assertEqual(run(host), 1)
            commands = [call[0] for call in host.calls]
            self.assertFalse(any("restore" in command for command in commands))
            self.assertTrue(any(command[0:2] == ("docker", "compose") and "down" in command for command in commands))

    def test_counts_mismatch_names_only_the_table_and_frontend_can_fail(self) -> None:
        host = FakeHost(fail_counts=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run(host), 1)
        self.assertIn("public.users", out.getvalue())
        self.assertNotIn("public.files", out.getvalue().split("counts:", 1)[-1].split("teardown:", 1)[0])

        host = FakeHost()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run(host, ready=False), 1)
        self.assertIn("frontend: refuse", out.getvalue())
        self.assertIn("logs open-webui", out.getvalue())

    def test_audit_contains_checks_and_result(self) -> None:
        host = FakeHost()
        self.assertEqual(run(host), 0)
        audit_inputs = [call[1] or "" for call in host.calls if "gideon_audit" in call[0]]
        self.assertTrue(any('backup_drill' in value and '"result": "pass"' in value for value in audit_inputs))


if __name__ == "__main__":
    unittest.main()
