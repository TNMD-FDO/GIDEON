"""Backup-drill orchestration contracts over a dict-backed host seam."""

import argparse
import base64
import contextlib
import hashlib
import io
import os
import subprocess
import unittest
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest import mock

import yaml  # type: ignore[import-untyped]
from test_render import artifact_source_files

from gideon.host import backupset, cas, drill, owui, stack
from gideon.host.render import ARTIFACTS
from gideon.host.render.compose import POSTGRES_SHM_SIZE_BYTES
from gideon.host.render.drill import DRILL_ROOT
from gideon.host.report import StageResult
from gideon.host.sysio import Command, Host, PathLike

ROOT = Path(__file__).resolve().parent.parent
RENDERED = "/etc/gideon/rendered"
SITE_PATH = "/etc/gideon/site.yaml"
SET_LABEL = "20260902T110000Z"
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
TARBALL = b"drill tarball"
TARBALL_SHA256 = hashlib.sha256(TARBALL).hexdigest()
CANONICAL_TEXT = "fictitious café Ω canonical text"
STORE_OBJECTS = {hashlib.sha256(data).hexdigest(): data for data in (b"fictitious source", CANONICAL_TEXT.encode())}
STORE_LISTING = "".join(f"{name}\t{len(data)}\n" for name, data in sorted(STORE_OBJECTS.items()))
STORE_PIN = backupset.ListingPin(
    "data-bulk-cas.listing", len(STORE_LISTING), hashlib.sha256(STORE_LISTING.encode()).hexdigest(),
    len(STORE_OBJECTS), sum(map(len, STORE_OBJECTS.values())),
)
DOCUMENT_ID = hashlib.sha256(b"fictitious document").hexdigest()
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
        listings={"data-bulk-cas": STORE_PIN},
    )


STORE_COPY = backupset.store_copy(backupset.STAGING, backupset.set_dir(SET_LABEL), make_manifest(), "data-bulk-cas")


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
        fail_inventory: bool = False,
        planted_object: bool = False,
        fail_listing_pin: bool = False,
    ) -> None:
        self.manifest = manifest or make_manifest()
        self.euid = euid
        self.fail_audit = fail_audit
        self.fail_tarball = fail_tarball
        self.fail_age_probe = fail_age_probe
        self.fail_verify = fail_verify
        self.fail_postgres = fail_postgres
        self.fail_counts = fail_counts
        self.fail_inventory = fail_inventory
        self.counts = {database: dict(counts) for database, counts in self.manifest.row_counts.items()}
        self.tables = {database: set(counts) for database, counts in self.counts.items()}
        self.count_output: dict[str, str] = {}
        self.chunk: tuple[str, ...] | None = (
            hashlib.sha256(b"fictitious chunk").hexdigest(), DOCUMENT_ID,
            hashlib.sha256(CANONICAL_TEXT.encode()).hexdigest(), "11", "17",
            hashlib.sha256(CANONICAL_TEXT[11:17].encode()).hexdigest(), "3",
        )
        self.fail_listing_pin = fail_listing_pin
        self.documents = [(DOCUMENT_ID, *STORE_OBJECTS)]
        self.store_copy = backupset.store_copy(backupset.STAGING, backupset.set_dir(SET_LABEL), self.manifest, "data-bulk-cas")
        self.object_bytes = {os.fspath(cas.object_path(name, self.store_copy)): data for name, data in STORE_OBJECTS.items()}
        if planted_object:
            planted_path = os.fspath(cas.object_path(sorted(STORE_OBJECTS)[0], self.store_copy))
            self.object_bytes[planted_path] = b"x" * len(self.object_bytes[planted_path])
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
        self.files[os.path.join(backupset.set_dir(SET_LABEL), STORE_PIN.file)] = STORE_LISTING
        if leftover:
            self.files[os.path.join(DRILL_ROOT, "compose.yaml")] = "old\n"
        for relative in ("host.lock", "images.lock", "models.lock"):
            path = ROOT / relative
            self.files[os.fspath(path)] = path.read_text(encoding="utf-8")
        for relative in TEMPLATE_PATHS:
            path = ROOT / "compose" / relative
            self.files[os.fspath(path)] = path.read_text(encoding="utf-8")
        self.files.update(artifact_source_files())

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
        if command[:2] == ("find", "."):
            if "-fprintf" in command:
                assert cwd is not None
                path = command[command.index("-fprintf") + 1]
                self.files[path] = "".join(
                    f"{os.path.basename(name)}\t{len(data)}\n"
                    for name, data in self.object_bytes.items()
                    if name.startswith(os.fspath(cwd) + "/")
                )
            return completed(command)
        if command[:3] == ("env", "LC_ALL=C", "sort"):
            path = command[-1]
            self.files[path] = "".join(line + "\n" for line in sorted(self.files[path].splitlines()))
            return completed(command)
        if command and command[0] == "awk":
            lines = self.files[command[-1]].splitlines()
            total = sum(int(line.split("\t")[1]) for line in lines)
            return completed(command, stdout=f"{len(lines)} {total}\n")
        if command and command[0] == "sha256sum":
            if len(command) > 1 and command[1] == "-c":
                assert input is not None and cwd is not None
                check_lines: list[str] = []
                failed = False
                for line in input.splitlines():
                    name, path = line.split("  ", 1)
                    data = self.read_bytes(os.path.join(os.fspath(cwd), path))
                    okay = hashlib.sha256(data).hexdigest() == name
                    failed |= not okay
                    check_lines.append(f"{path}: {'OK' if okay else 'FAILED'}\n")
                return completed(command, returncode=int(failed), stdout="".join(check_lines))
            path = command[-1]
            if path.endswith(".listing"):
                digest = hashlib.sha256(self.files[path].encode()).hexdigest()
                if self.fail_listing_pin:
                    digest = hashlib.sha256(b"fictitious mismatched listing").hexdigest()
                return completed(command, stdout=f"{digest}  {path}\n")
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
            if input is not None and "pg_stat_user_tables" in input:
                assert "ON_ERROR_STOP=1" in command
                assert input == "SELECT schemaname||'.'||relname FROM pg_stat_user_tables ORDER BY schemaname, relname;\n"
                return completed(command, returncode=int(self.fail_inventory), stdout="".join(table + "\n" for table in sorted(self.tables[database])))
            if input is not None and "count(*)" in input:
                assert "ON_ERROR_STOP=1" in command
                requested = [line.split("'")[1] for line in input.splitlines()]
                output = self.count_output.get(database, "".join(f"{table}|{self.counts[database][table]}\n" for table in requested))
                return completed(command, returncode=int(self.fail_counts), stdout=output)
            if input is not None and "FROM chunks" in input:
                assert "ON_ERROR_STOP=1" in command
                assert "JOIN documents d ON d.doc_id = c.doc_id" in input
                assert "ORDER BY c.chunk_id LIMIT 1" in input
                return completed(command, stdout="" if self.chunk is None else "|".join(self.chunk) + "\n")
            if input is not None and "FROM documents" in input:
                assert "ON_ERROR_STOP=1" in command
                assert "coalesce(sha256, '')" in input
                assert "coalesce(canonical_text_sha256, '')" in input
                assert "WHERE sha256 IS NOT NULL OR canonical_text_sha256 IS NOT NULL" in input
                return completed(command, stdout="".join("|".join(row) + "\n" for row in self.documents))
            raise AssertionError(f"unexpected psql statement: {input}")
        return completed(command)

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def read_bytes(self, path: PathLike) -> bytes:
        key = os.fspath(path)
        if key not in self.object_bytes:
            raise FileNotFoundError(key)
        return self.object_bytes[key]

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
        key = os.fspath(path)
        if key == self.store_copy:
            return os.stat_result((0o40700, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        if key in self.files:
            return os.stat_result((0o100644, 0, 0, 1, 0, 0, len(self.files[key].encode()), 0, 0, 0))
        if key in self.object_bytes:
            return os.stat_result((0o100440, 0, 0, 1, 0, 0, len(self.object_bytes[key]), 0, 0, 0))
        raise FileNotFoundError(key)

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
    now: datetime | None = NOW,
    clock: Callable[[], float] = lambda: 0.0,
) -> int:
    return drill.run_backup_drill(
        argparse.Namespace(),
        host=host,
        root=ROOT,
        now=now,
        clock=clock,
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


class StoreChecks(unittest.TestCase):
    def _observe(
        self, host: FakeHost, *, now: datetime | None = NOW, clock: Callable[[], float] = lambda: 0.0
    ) -> tuple[int, str, Mapping[str, object]]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(drill.audit, "write_rows", return_value=None) as writer:
            code = run(host, now=now, clock=clock)
        return code, out.getvalue(), writer.call_args.args[2][0].detail

    def test_walk_row_and_audit_hold_objects_documents_and_zero_seconds_with_supplied_now(self) -> None:
        host = FakeHost()
        code, output, detail = self._observe(host, clock=mock.Mock(side_effect=AssertionError("clock must not run")))
        self.assertEqual(code, 0)
        self.assertIn(
            f"cas-walk: ok — walked {STORE_PIN.objects} object(s) ({STORE_PIN.bytes} bytes), "
            f"every object hashes to its name; {len(host.documents)} document row(s) name objects the set holds; 0.0 s",
            output,
        )
        self.assertEqual(detail["objects"], STORE_PIN.objects)
        self.assertEqual(detail["seconds"], {"cas_walk": 0.0, "retrieval_read": 0.0})
        self.assertEqual(cast(Mapping[str, str], detail["checks"])["cas_walk"], "pass")
        self.assertEqual(cast(Mapping[str, str], detail["checks"])["retrieval_read"], "pass")
        self.assertIn((os.path.join(DRILL_ROOT, "walk"), 0o700), host.mkdir_calls)
        self.assertTrue(any(call[0] == ("sha256sum", "-c", "-") for call in host.calls))
        for data in STORE_OBJECTS.values():
            self.assertNotIn(data.decode(), output)
            self.assertNotIn(data.decode(), str(detail))

    def test_monotonic_seconds_reach_row_and_audit(self) -> None:
        clock = mock.Mock(side_effect=(20.0, 21.25, 25.0, 25.75))
        code, output, detail = self._observe(FakeHost(), now=None, clock=clock)
        self.assertEqual(code, 0)
        self.assertIn("; 1.2 s", output)
        self.assertIn("; 0.8 s", output)
        self.assertEqual(detail["seconds"], {"cas_walk": 1.25, "retrieval_read": 0.75})
        self.assertEqual(clock.call_count, 4)

    def test_read_row_uses_code_point_offsets_and_audit_contains_no_text(self) -> None:
        host = FakeHost()
        assert host.chunk is not None
        chunk_id, doc_id, name, start, end, _, tokens = host.chunk
        content = CANONICAL_TEXT[int(start):int(end)]
        self.assertNotEqual(len(content), len(content.encode()))
        code, output, detail = self._observe(host)
        self.assertEqual(code, 0)
        self.assertIn(
            f"retrieval-read: ok — read chunk {chunk_id} of document {doc_id}: {len(content)} character(s), "
            f"{tokens} token(s), from object {name}; 0.0 s",
            output,
        )
        self.assertNotIn(content, output)
        self.assertNotIn(content, str(detail))
        self.assertFalse(any(content in text for _, text, _ in host.writes))

    def test_no_chunk_passes_with_nothing_to_read(self) -> None:
        host = FakeHost()
        host.chunk = None
        code, output, detail = self._observe(host)
        self.assertEqual(code, 0)
        self.assertIn("retrieval-read: ok — the restored record holds no chunk; nothing to read\n", output)
        self.assertEqual(cast(Mapping[str, str], detail["checks"])["retrieval_read"], "pass")

    def test_wrong_content_hash_and_empty_slice_fail_with_read_fix(self) -> None:
        for empty in (False, True):
            with self.subTest(empty=empty):
                host = FakeHost()
                assert host.chunk is not None
                fields = list(host.chunk)
                if empty:
                    fields[3:5] = (str(len(CANONICAL_TEXT)), str(len(CANONICAL_TEXT) + 1))
                else:
                    fields[5] = hashlib.sha256(b"fictitious different slice").hexdigest()
                host.chunk = tuple(fields)
                code, output, detail = self._observe(host)
                self.assertEqual(code, 1)
                self.assertIn(f"chunk {fields[0]} text does not hash to its content hash", output)
                self.assertIn("Fix: Run sudo python3 -m gideon backup run, then retry.", output)
                self.assertEqual(cast(Mapping[str, str], detail["checks"])["retrieval_read"], "failed")
                self.assertIn("teardown: ok", output)

    def test_store_read_refusal_uses_its_problem_and_the_read_fix(self) -> None:
        host = FakeHost()
        assert host.chunk is not None
        name = host.chunk[2]
        path = os.fspath(cas.object_path(name, STORE_COPY))
        ordinary_read = host.read_bytes
        reads = 0

        def read_bytes(object_path: PathLike) -> bytes:
            nonlocal reads
            if os.fspath(object_path) == path:
                reads += 1
                if reads > 1:
                    raise FileNotFoundError(path)
            return ordinary_read(object_path)

        with mock.patch.object(host, "read_bytes", side_effect=read_bytes):
            code, output, detail = self._observe(host)
        self.assertEqual(code, 1)
        self.assertIn(f"no object {name} at {path}", output)
        self.assertIn("Fix: Run sudo python3 -m gideon backup run, then retry.", output)
        self.assertEqual(cast(Mapping[str, str], detail["checks"])["retrieval_read"], "failed")

    def test_non_utf8_object_fails_without_echoing_its_bytes(self) -> None:
        host = FakeHost()
        assert host.chunk is not None
        data = b"fictitious invalid UTF-8: \xff"
        name = hashlib.sha256(data).hexdigest()
        host.object_bytes[os.fspath(cas.object_path(name, STORE_COPY))] = data
        content = "".join(
            f"{os.path.basename(path)}\t{len(value)}\n" for path, value in sorted(host.object_bytes.items())
        )
        pin = replace(STORE_PIN, size=len(content), sha256=hashlib.sha256(content.encode()).hexdigest(),
                      objects=len(host.object_bytes), bytes=sum(map(len, host.object_bytes.values())))
        host.manifest = replace(host.manifest, listings={"data-bulk-cas": pin})
        host.files[os.path.join(backupset.set_dir(SET_LABEL), backupset.MANIFEST_NAME)] = host.manifest.to_json()
        host.files[os.path.join(backupset.set_dir(SET_LABEL), pin.file)] = content
        fields = list(host.chunk)
        fields[2] = name
        host.chunk = tuple(fields)
        host.documents = [(DOCUMENT_ID, next(iter(STORE_OBJECTS)), name)]
        code, output, detail = self._observe(host)
        self.assertEqual(code, 1)
        self.assertIn(f"object {name} is not UTF-8", output)
        self.assertNotIn("fictitious invalid UTF-8", output)
        self.assertNotIn("fictitious invalid UTF-8", str(detail))

    def test_chunk_row_grammar_refuses_malformed_rows_without_echoing_text(self) -> None:
        host = FakeHost()
        assert host.chunk is not None
        cases: list[tuple[str, ...]] = [host.chunk[:-1], (*host.chunk, "extra")]
        for index, value in (
            (0, "fictitious bad id"), (1, DOCUMENT_ID.upper()), (2, ""), (5, "fictitious bad hash"),
            (3, "-1"), (4, "11"), (6, "0"), (6, "1.5"),
        ):
            fields = list(host.chunk)
            fields[index] = value
            cases.append(tuple(fields))
        for row in cases:
            with self.subTest(row=row):
                host = FakeHost()
                host.chunk = row
                code, output, _ = self._observe(host)
                self.assertEqual(code, 1)
                self.assertIn("chunk query returned a malformed row", output)
                self.assertNotIn("fictitious bad", output)

    def test_planted_object_fails_naming_it_and_teardown_runs(self) -> None:
        host = FakeHost(planted_object=True)
        code, output, detail = self._observe(host)
        self.assertEqual(code, 1)
        self.assertIn(f"1 object(s) do not hash to their names: {sorted(STORE_OBJECTS)[0]}", output)
        self.assertEqual(detail["result"], "failed")
        self.assertEqual(detail["objects"], STORE_PIN.objects)
        self.assertEqual(cast(Mapping[str, str], detail["checks"])["cas_walk"], "failed")
        self.assertEqual(cast(Mapping[str, str], detail["checks"])["retrieval_read"], "skipped")
        self.assertFalse(any("FROM documents" in (call[1] or "") for call in host.calls))
        self.assertIn(("rm", "-rf", os.path.join(DRILL_ROOT, "walk")), [call[0] for call in host.calls])
        self.assertIn("teardown: ok", output)
        self.assertIn("Remove the named object's path from the set's copy", output)
        self.assertIn("backup run", output)

    def test_listing_pin_failure_precedes_object_hashing(self) -> None:
        host = FakeHost(fail_listing_pin=True)
        code, output, detail = self._observe(host)
        self.assertEqual(code, 1)
        self.assertIn("does not match its manifest pin", output)
        self.assertFalse(any(call[0] == ("sha256sum", "-c", "-") for call in host.calls))
        self.assertNotIn("objects", detail)
        self.assertIn("teardown: ok", output)

    def test_staging_copy_walk_ignores_extra_objects_and_names_a_failing_listed_one(self) -> None:
        value = replace(make_manifest(), version=backupset.MANIFEST_VERSION)
        extra = hashlib.sha256(b"fictitious object pinned by a different set").hexdigest()
        for failed in (False, True):
            with self.subTest(failed=failed):
                host = FakeHost(manifest=value, planted_object=failed)
                host.object_bytes[os.fspath(cas.object_path(extra, host.store_copy))] = b"fictitious damaged unlisted bytes"
                code, output, detail = self._observe(host)
                self.assertEqual(code, int(failed), output)
                self.assertEqual(detail["objects"], STORE_PIN.objects)
                self.assertNotIn(extra, output)
                self.assertFalse(any("-fprintf" in argv for argv, _, _, _ in host.calls))
                hashes = [call for call in host.calls if call[0] == ("sha256sum", "-c", "-") and call[2] == host.store_copy]
                self.assertEqual(hashes, [(
                    ("sha256sum", "-c", "-"),
                    "".join(f"{name}  {name[:2]}/{name[2:4]}/{name}\n" for name in sorted(STORE_OBJECTS)),
                    host.store_copy, None,
                )])
                if failed:
                    self.assertIn(f"1 object(s) do not hash to their names: {sorted(STORE_OBJECTS)[0]}", output)
                    self.assertEqual(cast(Mapping[str, str], detail["checks"])["retrieval_read"], "skipped")
                else:
                    self.assertEqual(cast(Mapping[str, str], detail["checks"])["retrieval_read"], "pass")
                self.assertIn("teardown: ok", output)

    def test_missing_object_counts_document_rows_and_names_at_most_ten_ids(self) -> None:
        host = FakeHost()
        absent = hashlib.sha256(b"fictitious absent object").hexdigest()
        ids = [hashlib.sha256(f"fictitious missing document {i}".encode()).hexdigest() for i in range(12)]
        host.documents = [(doc_id, absent, absent) for doc_id in ids]
        code, output, detail = self._observe(host)
        self.assertEqual(code, 1)
        self.assertIn(f"{len(ids)} document row(s) name objects the set lacks: {', '.join(ids[:10])}", output)
        for doc_id in ids[10:]:
            self.assertNotIn(doc_id, output)
        self.assertEqual(detail["objects"], STORE_PIN.objects)

    def test_no_listing_pin_refuses_with_backup_run_fix(self) -> None:
        host = FakeHost(manifest=replace(make_manifest(), listings={}))
        code, output, detail = self._observe(host)
        self.assertEqual(code, 1)
        self.assertIn("the set predates the store listing", output)
        self.assertIn("Fix: Run sudo python3 -m gideon backup run, then retry.", output)
        self.assertNotIn("Remove the named object's path", output)
        self.assertNotIn("objects", detail)
        self.assertFalse(any(call[0] == ("sha256sum", "-c", "-") for call in host.calls))

    def test_earlier_failure_skips_walk_and_omits_objects(self) -> None:
        host = FakeHost(fail_counts=True)
        clock = mock.Mock(side_effect=AssertionError("skipped walk must not use clock"))
        code, output, detail = self._observe(host, now=None, clock=clock)
        self.assertEqual(code, 1)
        self.assertNotIn("cas-walk:", output)
        self.assertEqual(cast(Mapping[str, str], detail["checks"])["cas_walk"], "skipped")
        self.assertNotIn("objects", detail)
        self.assertEqual(detail["seconds"], {"cas_walk": 0.0, "retrieval_read": 0.0})

    def test_empty_reference_names_pass_and_malformed_document_rows_refuse_without_echoing_text(self) -> None:
        host = FakeHost()
        host.documents = [(DOCUMENT_ID, "", next(iter(STORE_OBJECTS)))]
        self.assertEqual(self._observe(host)[0], 0)
        for row in (
            ("fictitious malformed document", "", ""),
            (DOCUMENT_ID, "fictitious malformed name", ""),
            (DOCUMENT_ID, "", "fictitious malformed name"),
            (DOCUMENT_ID, "", "", "extra field"),
        ):
            with self.subTest(row=row):
                host = FakeHost()
                host.documents = [row]
                code, output, _ = self._observe(host)
                self.assertEqual(code, 1)
                self.assertIn("document object query returned a malformed line", output)
                self.assertNotIn("fictitious malformed", output)


class Counts(unittest.TestCase):
    def _stage(self, host: FakeHost) -> StageResult:
        return drill._counts_stage(cast(Host, host), manifest=host.manifest)

    def test_matching_counts_include_the_manifest_table_count(self) -> None:
        host = FakeHost()
        result = self._stage(host)
        tables = sum(map(len, host.manifest.row_counts.values()))
        self.assertEqual(result, StageResult("counts", True, f"restored row counts match the manifest ({tables} tables)", ""))

    def test_positive_and_negative_drift_pass_in_name_order_across_databases(self) -> None:
        manifest = replace(make_manifest(), row_counts={"gideon": {"public.users": 4}, "openwebui": {"public.files": 1}})
        host = FakeHost(manifest=manifest)
        host.counts["gideon"]["public.users"] = 2
        host.counts["openwebui"]["public.files"] = 3
        result = self._stage(host)
        self.assertTrue(result.ok)
        self.assertEqual(result.detail, "2 table(s) drift from the manifest: public.files +2, public.users -2 (the writer's rows since the boundary)")

    def test_absent_and_emptied_tables_each_pass_with_their_clause(self) -> None:
        host = FakeHost()
        host.tables["gideon"].clear()
        result = self._stage(host)
        self.assertTrue(result.ok)
        self.assertEqual(result.detail, "absent from the restored database: public.users")
        self.assertFalse(any(call[1] and "count(*)" in call[1] and "public.users" in call[1] for call in host.calls))
        host = FakeHost()
        host.counts["gideon"]["public.users"] = 0
        result = self._stage(host)
        self.assertTrue(result.ok)
        self.assertEqual(result.detail, "restored empty: public.users")

    def test_drift_absent_and_empty_clauses_keep_their_order(self) -> None:
        manifest = replace(make_manifest(), row_counts={"gideon": {"public.z_drift": 1, "public.a_absent": 1}, "openwebui": {"public.b_empty": 1}})
        host = FakeHost(manifest=manifest)
        host.tables["gideon"].remove("public.a_absent")
        host.counts["gideon"]["public.z_drift"] = 2
        host.counts["openwebui"]["public.b_empty"] = 0
        result = self._stage(host)
        self.assertTrue(result.ok)
        self.assertEqual(result.detail, "1 table(s) drift from the manifest: public.z_drift +1 (the writer's rows since the boundary); absent from the restored database: public.a_absent; restored empty: public.b_empty")

    def test_short_malformed_unrequested_and_duplicate_count_results_refuse(self) -> None:
        for output in ("", "public.users|-1\n", "public.users|1|extra\n", "public.other|1\n", "public.users|2\npublic.users|2\n"):
            with self.subTest(output=output):
                host = FakeHost()
                host.count_output["gideon"] = output
                result = self._stage(host)
                self.assertFalse(result.ok)
                self.assertEqual(result.fix, drill._COUNTS_FIX)

    def test_listing_and_count_failures_are_refusals(self) -> None:
        for inventory in (True, False):
            with self.subTest(inventory=inventory):
                host = FakeHost(fail_inventory=inventory, fail_counts=not inventory)
                result = self._stage(host)
                self.assertFalse(result.ok)
                self.assertIn("table listing failed" if inventory else "row count query failed", result.detail)
                self.assertEqual(result.fix, drill._COUNTS_FIX)
                self.assertEqual(len(host.calls), 1 if inventory else 2)

    def test_listing_and_count_exceptions_are_refusals_and_unlisted_names_are_skipped(self) -> None:
        for inventory in (True, False):
            with self.subTest(inventory=inventory):
                host = FakeHost()
                responses: list[subprocess.CompletedProcess[str] | OSError] = [OSError("fictitious query failure")]
                if not inventory:
                    responses.insert(0, completed((), stdout="".join(table + "\n" for table in sorted(host.tables["gideon"]))))
                with mock.patch.object(host, "run", side_effect=responses):
                    result = self._stage(host)
                self.assertFalse(result.ok)
                self.assertEqual(result.fix, drill._COUNTS_FIX)
        host = FakeHost()
        host.tables["gideon"] = {"fictitious unlisted table"}
        result = self._stage(host)
        self.assertTrue(result.ok)
        self.assertEqual(result.detail, "absent from the restored database: public.users")


class DrillContracts(unittest.TestCase):
    def test_document_prepare_identity_upload_and_teardown(self) -> None:
        host = FakeHost(leftover=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run(host), 0)
        document = yaml.safe_load(host.files[os.path.join(DRILL_ROOT, "compose.yaml")])
        self.assertEqual(document["name"], "gideon-drill")
        self.assertNotIn("command", document["services"]["postgres"])
        self.assertEqual(document["services"]["postgres"]["shm_size"], POSTGRES_SHM_SIZE_BYTES)
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
        for name in ("postgres", "openwebui", "compose.yaml", "walk"):
            self.assertIn(("rm", "-rf", f"{DRILL_ROOT}/{name}"), commands)
        self.assertIn("cas-walk: ok — walked", out.getvalue())
        self.assertIn("retrieval-read: ok — read chunk", out.getvalue())

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

    def test_counts_drift_names_only_the_table_and_frontend_can_fail(self) -> None:
        host = FakeHost()
        host.counts["gideon"]["public.users"] += 1
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run(host), 0)
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
