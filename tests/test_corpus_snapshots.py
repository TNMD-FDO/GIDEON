"""Shared corpus snapshot fetch and verification over fictitious files."""

import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import asdict, replace
from pathlib import Path

from gideon.host import fetch, report
from gideon.host.corpus import snapshots
from gideon.host.corpus.lockfile import SidecarEntry
from gideon.host.render.worker import KEPT_FORM, RECORD_SUFFIX
from gideon.host.sysio import Command, PathLike, RealHost

SOURCE = "example"
DATE = "2099-01-02"
RELATIVE = "objects/example.txt"
URL = "https://source.example.test/objects/example.txt"
DATA = b"fictitious snapshot"
FETCHED_AT = "2099-01-03T04:05:06+00:00"


def _record(data: bytes, *, fetched_at: str = FETCHED_AT) -> fetch.FetchRecord:
    return fetch.FetchRecord(
        state="whole", form=KEPT_FORM, host="source.example.test",
        path="/objects/example.txt", etag=None, last_modified=None,
        total=len(data), durable=len(data), size=len(data),
        sha256=hashlib.sha256(data).hexdigest(), fetched_at=fetched_at,
        job=1, seconds=0.1, resumes=0, schema=1,
    )


class HashHost(RealHost):
    """Read temporary files and answer only the hash subprocess."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[str]] = []

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, input, cwd, env, timeout, passthrough
        command = list(argv)
        self.calls.append(command)
        if len(command) == 2 and command[0] == "sha256sum":
            path = Path(command[1])
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            return subprocess.CompletedProcess(command, 0, f"{digest}  {path}\n", "")
        raise AssertionError(f"unexpected command: {command}")


class Fixes(unittest.TestCase):
    """The shared fixes name the caller's corpus command."""

    def test_retry_and_refetch_fixes_for_both_commands(self) -> None:
        path = Path("/snapshots/example-2099-01-02/objects/example.txt")
        for command_path in ("corpus cut", "corpus install"):
            with self.subTest(command_path=command_path):
                command = report.command(command_path)
                self.assertEqual(
                    snapshots.retry_fix("Restore access, then retry.", command_path=command_path),
                    f"Restore access. Then run {command} again.",
                )
                self.assertEqual(
                    snapshots.refetch_fix(path, command_path=command_path),
                    f"Remove {path} and {path}{RECORD_SUFFIX}, then run {command} "
                    "again to fetch it anew.",
                )


class Files(unittest.TestCase):
    """Snapshot records and sidecar pins are checked through the host seam."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.host = HashHost()
        destination = fetch.snapshot_destination(SOURCE, DATE, RELATIVE)
        self.path = self.root / destination
        self.record_path = self.root / f"{destination}{RECORD_SUFFIX}"
        self.path.parent.mkdir(parents=True)
        self.pin = (hashlib.sha256(DATA).hexdigest(), len(DATA))

    def _write(self, data: bytes = DATA, *, record: fetch.FetchRecord | None = None) -> None:
        self.path.write_bytes(data)
        self.record_path.write_text(json.dumps(asdict(record or _record(data))))

    def _verify(
        self, pin: tuple[str, int] | None = None, *,
        expected_paths: set[str] | None = None,
        fetched_paths: frozenset[str] = frozenset(),
    ) -> tuple[tuple[SidecarEntry, ...] | None, report.StageResult | None]:
        with contextlib.redirect_stdout(io.StringIO()):
            return snapshots.verify(
                self.host, self.root, SOURCE, DATE,
                ((RELATIVE, self.pin if pin is None else pin),),
                command_path="corpus install",
                expected_paths={RELATIVE} if expected_paths is None else expected_paths,
                fetched_paths=fetched_paths,
                repin_fix="Set mirror_url in corpus/lockfiles/corpus-2099-01-03.yaml or make a new cut.",
            )

    def test_matching_file_record_and_pin_returns_sidecar_entry(self) -> None:
        self._write()
        entries, issue = self._verify()
        self.assertIsNone(issue)
        self.assertEqual(entries, (SidecarEntry(RELATIVE, *self.pin),))
        self.assertEqual(self.host.calls, [["sha256sum", str(self.path)]])

    def test_file_disagreeing_with_record_refuses_with_removal(self) -> None:
        other = bytes([DATA[0] ^ 1]) + DATA[1:]
        self._write(other, record=_record(DATA))
        entries, issue = self._verify()
        self.assertIsNone(entries)
        assert issue is not None
        self.assertIn("file disagrees with its fetch record", issue.detail)
        self.assertIn(str(self.path), issue.detail)
        self.assertEqual(
            issue.fix, snapshots.refetch_fix(self.path, command_path="corpus install"),
        )

    def test_pin_disagreement_uses_removal_or_repin_then_removal(self) -> None:
        self._write()
        other = bytes([DATA[0] ^ 1]) + DATA[1:]
        wrong_pin = (hashlib.sha256(other).hexdigest(), len(other))
        for fetched in (frozenset(), frozenset({RELATIVE})):
            with self.subTest(fetched=fetched):
                entries, issue = self._verify(wrong_pin, fetched_paths=fetched)
                self.assertIsNone(entries)
                assert issue is not None
                self.assertIn("pinned sidecar", issue.detail)
                removal = snapshots.refetch_fix(self.path, command_path="corpus install")
                self.assertIn(f"remove {self.path} and {self.record_path}", issue.fix.lower())
                self.assertIn(report.command("corpus install"), issue.fix)
                if fetched:
                    self.assertIn("mirror_url in corpus/lockfiles/corpus-2099-01-03.yaml", issue.fix)
                    self.assertLess(issue.fix.index("mirror_url"), issue.fix.index(str(self.path)))
                else:
                    self.assertEqual(issue.fix, removal)

    def test_path_outside_expected_set_and_missing_expected_path_refuse(self) -> None:
        self._write()
        entries, issue = self._verify(expected_paths=set())
        self.assertIsNone(entries)
        assert issue is not None
        self.assertIn("pinned sidecar", issue.detail)
        self.assertEqual(
            issue.fix, snapshots.refetch_fix(self.path, command_path="corpus install"),
        )

        entries, issue = self._verify(expected_paths={RELATIVE, "objects/missing.txt"})
        self.assertIsNone(entries)
        assert issue is not None
        self.assertIn("no longer has the pinned file list", issue.detail)
        self.assertIn(report.command("corpus install"), issue.fix)

    def test_present_without_record_refuses_before_defer(self) -> None:
        self.path.write_bytes(DATA)
        result = snapshots.fetch_source(
            self.host, "/rendered", self.root, SOURCE, DATE, ((RELATIVE, URL),),
            sleep=lambda _seconds: None, monotonic=lambda: 0.0,
            command_path="corpus install",
        )
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn(str(self.path), result.problem)
        self.assertEqual(
            result.fix, snapshots.refetch_fix(self.path, command_path="corpus install"),
        )
        self.assertEqual(self.host.calls, [])

    def test_whole_record_is_kept_without_deferral(self) -> None:
        self._write()
        result = snapshots.fetch_source(
            self.host, "/rendered", self.root, SOURCE, DATE, ((RELATIVE, URL),),
            sleep=lambda _seconds: None, monotonic=lambda: 0.0,
            command_path="corpus cut",
        )
        self.assertIsInstance(result, snapshots.FetchedSource)
        assert isinstance(result, snapshots.FetchedSource)
        self.assertEqual(result.records[RELATIVE], _record(DATA))
        self.assertEqual(result.deferred, 0)
        self.assertEqual(result.deferred_paths, frozenset())
        self.assertEqual(self.host.calls, [])


class Summary(unittest.TestCase):
    """FetchedSource reports counts and the latest recorded fetch time."""

    def test_empty_singular_plural_and_latest_fetch(self) -> None:
        first = _record(DATA)
        later = replace(first, fetched_at="2099-01-03T03:05:06-02:00")
        empty = snapshots.FetchedSource({}, 0, frozenset())
        self.assertEqual(empty.summary(), "0 files, 0 bytes; no fetches deferred")
        self.assertIsNone(empty.latest_fetch())

        one = snapshots.FetchedSource({RELATIVE: first}, 1, frozenset({RELATIVE}))
        self.assertEqual(
            one.summary(), f"1 files, {first.size} bytes; 1 fetch deferred",
        )
        self.assertEqual(one.latest_fetch(), first.fetched_at)

        two = snapshots.FetchedSource(
            {RELATIVE: later, "objects/other.txt": first},
            2, frozenset({RELATIVE, "objects/other.txt"}),
        )
        self.assertEqual(two.summary(), f"2 files, {later.size + first.size} bytes; 2 fetches deferred")
        self.assertEqual(two.latest_fetch(), later.fetched_at)
