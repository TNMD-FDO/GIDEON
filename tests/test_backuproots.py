"""Backup root behavior and command boundaries over a recording host seam."""

import ast
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest import mock

from gideon.host import backuproots, backupset, cas, pgbackrest, stack
from gideon.host.report import Problem
from gideon.host.sysio import Command, Host, PathLike, RealHost

ROOT = "example-root"
SOURCE = "/example/source"
PARTIAL = "/example/staging/sets/current.partial"
PREVIOUS = "/example/staging/sets/previous"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
ROOT_NAMES = frozenset(root.name for root in backupset.inventory_roots("/example/checkout"))


def root_boundary_findings(source: str, module: str) -> list[str]:
    """Find root names, kind reads, and layout constants in command syntax."""

    findings: list[str] = []
    for node in ast.walk(ast.parse(source, filename=module)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(node.value == name or node.value.startswith(name + "/") for name in ROOT_NAMES):
                findings.append(f"{module}:{node.lineno}: root-name literal {node.value!r}")
        elif isinstance(node, ast.Attribute):
            if node.attr in {"snapshotted", "restore_in_place"} and isinstance(node.ctx, ast.Load):
                findings.append(f"{module}:{node.lineno}: root policy read .{node.attr}")
            if node.attr in {"FILES_DIR", "REPOSITORY_PATH"}:
                findings.append(f"{module}:{node.lineno}: layout reference .{node.attr}")
        elif isinstance(node, ast.Name) and node.id in {"FILES_DIR", "REPOSITORY_PATH"}:
            findings.append(f"{module}:{node.lineno}: layout reference {node.id}")
    return findings


def completed(
    argv: Sequence[str], *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)


def entry(path: str, *, size: int = 7, mtime: float = 100.0, sha256: str | None = None) -> backupset.Entry:
    return backupset.Entry(path, "f", size, 1000, 1000, 0o644, mtime, sha256)


def listing(*entries: backupset.Entry) -> str:
    records = ["d\t4096\t1000\t1000\t755\t100.0\t\0"]
    records.extend(
        f"f\t{item.size}\t{item.uid}\t{item.gid}\t{item.mode:o}\t{item.mtime}\t{item.path}\0"
        for item in entries
    )
    return "".join(records)


def previous(*entries: backupset.Entry) -> backupset.SetRef:
    manifest = backupset.Manifest(
        1,
        "pre-example",
        backupset.Kind.LABELLED,
        NOW,
        NOW,
        "example-release",
        "/example/checkout",
        "example-commit",
        "example.test",
        None,
        "example-backup",
        "full",
        NOW,
        {},
        {ROOT: entries},
        "a" * 64,
        ("example-recipient",),
        backupset.LinkVerdict(0, 0),
        "b" * 64,
    )
    return backupset.SetRef("pre-example", PREVIOUS, NOW, True, manifest)


def manifest_with(inventory: Mapping[str, tuple[backupset.Entry, ...]]) -> backupset.Manifest:
    ref = previous()
    assert ref.manifest is not None
    return replace(ref.manifest, inventory=inventory)


def later_ref(
    label: str,
    inventory: Mapping[str, tuple[backupset.Entry, ...]],
    *,
    ids: backupset.AccountIds | None = None,
    listings: Mapping[str, backupset.ListingPin] | None = None,
) -> backupset.SetRef:
    value = replace(
        manifest_with(inventory), label=label, gideon_ids=ids,
        listings={} if listings is None else listings,
    )
    return backupset.SetRef(label, f"/example/staging/sets/{label}", NOW, True, value)


class FakeHost:
    """Return scripted command results and record calls in their actual order."""

    def __init__(
        self,
        responses: Sequence[subprocess.CompletedProcess[str] | OSError] = (),
        stats: Mapping[str, os.stat_result] | None = None,
        existing: Sequence[str] = (),
        files: Mapping[str, str] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.stats = dict(stats or {})
        self.existing = set(existing)
        self.files = dict(files or {})
        self.calls: list[tuple[str, object, object]] = []

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        self.calls.append(("read_text", key, {}))
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def exists(self, path: PathLike) -> bool:
        self.calls.append(("exists", os.fspath(path), {}))
        return os.fspath(path) in self.existing

    def run(self, argv: Command, **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = tuple(argv)
        self.calls.append(("run", command, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected command: {command}")
        response = self.responses.pop(0)
        if isinstance(response, OSError):
            raise response
        if tuple(response.args) != command:
            raise AssertionError(f"expected {response.args}, got {command}")
        return response

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        self.calls.append(("mkdir", os.fspath(path), {"mode": mode, "parents": parents, "exist_ok": exist_ok}))

    def stat(self, path: PathLike) -> os.stat_result:
        self.calls.append(("stat", os.fspath(path), {}))
        if os.fspath(path) in self.stats:
            return self.stats[os.fspath(path)]
        raise FileNotFoundError(os.fspath(path))

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        self.calls.append(("unlink", os.fspath(path), {"missing_ok": missing_ok}))


def bound(host: FakeHost, kind: backupset.RootKind, old: backupset.SetRef | None = None) -> backuproots.TakingRoot:
    row = backupset.InventoryRoot(ROOT, SOURCE, ("cache/",), kind, True)
    (root,) = backuproots.taking(cast(Host, host), (row,), PARTIAL, old)
    return root


class ClaimGroups(unittest.TestCase):
    def test_two_sets_and_owners_merge_before_chunks_and_skip_link_mode(self) -> None:
        first = backuproots.Claims()
        second = backuproots.Claims()
        first_map = backupset.OwnerMap(backupset.AccountIds(1000, 1000), backupset.AccountIds(1001, 1001))
        second_map = backupset.OwnerMap(backupset.AccountIds(2000, 2000), backupset.AccountIds(2001, 2001))
        first_paths = tuple(f"/first/file-{index:03}" for index in range(199))
        second_paths = ("/second/file-000", "/second/file-001")
        link_path = "/second/link"
        for path in first_paths:
            first.claim(path, entry(path), first_map)
        for path in second_paths:
            second.claim(path, entry(path), first_map)
        second.claim(
            link_path,
            backupset.Entry("link", "l", 1, 2000, 2000, 0o777, 100.0, None),
            second_map,
        )
        first.merge(second)
        first_chunk = (*first_paths, second_paths[0])
        last_chunk = (second_paths[1],)
        commands = (
            ("chown", "1001:1001", "--", *first_chunk),
            ("chown", "1001:1001", "--", *last_chunk),
            ("chown", "-h", "2001:2001", "--", link_path),
            ("chmod", "0644", "--", *first_chunk),
            ("chmod", "0644", "--", *last_chunk),
        )
        host = FakeHost(tuple(completed(argv) for argv in commands))

        self.assertEqual((first.entries_claimed, first.gideon_owned, first.owner_paths), (202, 202, 202))
        self.assertIsNone(first.apply(cast(Host, host), "fetch"))
        self.assertEqual(host.calls, [("run", argv, {"input": None, "timeout": None}) for argv in commands])
        self.assertEqual(host.responses, [])

    def test_grouped_commands_refuse_with_their_phrase_and_empty_fix(self) -> None:
        for operation in ("chown", "chmod"):
            with self.subTest(operation=operation):
                claims = backuproots.Claims()
                if operation == "chown":
                    claims.add_owner("/example/file", 1, 2)
                    argv = ("chown", "1:2", "--", "/example/file")
                    detail = "re-owned 1 path(s)"
                else:
                    claims.add_mode("/example/file", 0o640)
                    argv = ("chmod", "0640", "--", "/example/file")
                    detail = "applied mode 0640 to 1 path(s)"
                host = FakeHost((completed(argv, returncode=1, stderr="denied"),))
                self.assertEqual(claims.apply(cast(Host, host), "fetch"), Problem(f"{detail}: denied", ""))
                self.assertEqual(host.calls, [("run", argv, {"input": None, "timeout": None})])


class Take(unittest.TestCase):
    def test_snapshot_copies_and_returns_previous_file_pairs(self) -> None:
        old = previous(entry("nested/file"), entry("other"))
        destination = os.path.join(backupset.files_dir(PARTIAL), ROOT)
        old_copy = os.path.join(backupset.files_dir(PREVIOUS), ROOT)
        argv = (
            "rsync", "-a", "--exclude=cache/", f"--link-dest={old_copy}/",
            SOURCE + "/", destination + "/",
        )
        host = FakeHost((completed(argv),))

        outcome = bound(host, backupset.RootKind.SNAPSHOT, old).take()

        self.assertEqual(
            outcome,
            backuproots.Taken(
                True,
                {
                    f"{ROOT}/nested/file": (
                        os.path.join(destination, "nested/file"),
                        os.path.join(old_copy, "nested/file"),
                    ),
                    f"{ROOT}/other": (
                        os.path.join(destination, "other"),
                        os.path.join(old_copy, "other"),
                    ),
                },
            ),
        )
        self.assertEqual(
            host.calls,
            [
                ("mkdir", destination, {"mode": 0o750, "parents": True, "exist_ok": True}),
                ("run", argv, {}),
            ],
        )
        self.assertEqual(host.responses, [])

    def test_repository_take_makes_no_copy(self) -> None:
        host = FakeHost()
        self.assertEqual(bound(host, backupset.RootKind.REPOSITORY).take(), backuproots.Taken(False, {}))
        self.assertEqual(host.calls, [])

    def test_snapshot_refuses_a_failed_rsync(self) -> None:
        destination = os.path.join(backupset.files_dir(PARTIAL), ROOT)
        argv = ("rsync", "-a", "--exclude=cache/", SOURCE + "/", destination + "/")
        host = FakeHost((completed(argv, returncode=23, stderr="rsync: denied\n"),))
        self.assertEqual(
            bound(host, backupset.RootKind.SNAPSHOT).take(),
            Problem(f"rsync failed for {ROOT}: rsync: denied", ""),
        )
        self.assertEqual(
            host.calls,
            [
                ("mkdir", destination, {"mode": 0o750, "parents": True, "exist_ok": True}),
                ("run", argv, {}),
            ],
        )

    def test_snapshot_refuses_a_raised_seam_error(self) -> None:
        destination = os.path.join(backupset.files_dir(PARTIAL), ROOT)
        host = FakeHost((OSError("disk unavailable"),))
        self.assertEqual(
            bound(host, backupset.RootKind.SNAPSHOT).take(),
            Problem("file snapshot failed: disk unavailable", ""),
        )
        self.assertEqual(
            host.calls,
            [
                ("mkdir", destination, {"mode": 0o750, "parents": True, "exist_ok": True}),
                ("run", ("rsync", "-a", "--exclude=cache/", SOURCE + "/", destination + "/"), {}),
            ],
        )


class StoreTaking(unittest.TestCase):
    """A store is copied before and after the archive boundary, and its listing pinned."""

    def setUp(self) -> None:
        self.copy = os.path.join(backupset.files_dir(PARTIAL), ROOT)
        self.listing_path = backupset.listing_path(PARTIAL, ROOT)
        self.copy_argv = (
            "rsync", "-rtp", "--stats", "--exclude=cache/", SOURCE + "/", self.copy + "/",
        )

    def test_store_take_link_dest_depends_on_previous_pin_and_object_count(self) -> None:
        pin = backupset.ListingPin(f"{ROOT}.listing", 0, "a" * 64, 0, 0)
        old = previous()
        assert old.manifest is not None
        pinned = replace(old, manifest=replace(old.manifest, listings={ROOT: pin}))
        counted = replace(old, manifest=replace(old.manifest, listings={ROOT: replace(pin, objects=1)}))
        old_copy = os.path.join(backupset.files_dir(PREVIOUS), ROOT)
        linked_argv = self.copy_argv[:-2] + (f"--link-dest={old_copy}/",) + self.copy_argv[-2:]
        link_check = ("find", self.copy, "-type", "f", "-links", "+1", "-print", "-quit")
        cases = (
            (None, self.copy_argv, False),
            (old, self.copy_argv, False),
            (pinned, linked_argv, False),
            (counted, linked_argv, True),
        )
        for prior, argv, check_links in cases:
            with self.subTest(prior=prior, check_links=check_links):
                responses = [completed(argv, stdout="Number of regular files transferred: 1\n")]
                if check_links:
                    responses.append(completed(link_check, stdout=f"{self.copy}/linked\n"))
                host = FakeHost(responses, existing=(SOURCE,))
                self.assertEqual(bound(host, backupset.RootKind.STORE, prior).take(), backuproots.Taken(True, {}))
                expected: list[tuple[str, object, object]] = [
                    ("exists", SOURCE, {}),
                    ("mkdir", self.copy, {"mode": 0o750, "parents": True, "exist_ok": True}),
                    ("run", argv, {"cwd": None}),
                ]
                if check_links:
                    expected.append(("run", link_check, {"cwd": None}))
                self.assertEqual(host.calls, expected)
                self.assertEqual(host.responses, [])

    def test_complete_reports_counts_and_only_user_written_snapshots_act(self) -> None:
        host = FakeHost((completed(self.copy_argv, stdout="Number of regular files transferred: 1,234\n"),), existing=(SOURCE,))
        self.assertEqual(bound(host, backupset.RootKind.STORE).complete(), backuproots.Completed(True, 1234, f"{ROOT}: 1234 object(s)"))
        self.assertEqual(host.calls, [
            ("exists", SOURCE, {}),
            ("mkdir", self.copy, {"mode": 0o750, "parents": True, "exist_ok": True}),
            ("run", self.copy_argv, {"cwd": None}),
        ])
        for kind in (backupset.RootKind.SNAPSHOT, backupset.RootKind.REPOSITORY):
            with self.subTest(kind=kind):
                idle = FakeHost()
                self.assertEqual(bound(idle, kind).complete(), backuproots.Completed(False, 0, ""))
                self.assertEqual(idle.calls, [])
        for prior in (None, previous()):
            with self.subTest(previous=prior):
                link_dest = (
                    (f"--link-dest={backupset.files_dir(PREVIOUS)}/{ROOT}/",)
                    if prior is not None else ()
                )
                argv = (
                    "rsync", "-a", "--exclude=cache/", *link_dest, "--stats",
                    SOURCE + "/", self.copy + "/",
                )
                row = backupset.InventoryRoot(
                    ROOT, SOURCE, ("cache/",), backupset.RootKind.SNAPSHOT, True, True
                )
                copied = FakeHost((completed(argv, stdout="Number of regular files transferred: 3\n"),))
                (snapshot,) = backuproots.taking(cast(Host, copied), (row,), PARTIAL, prior)
                self.assertEqual(snapshot.complete(), backuproots.Completed(True, 3, f"{ROOT}: 3 file(s)"))
                self.assertEqual(copied.calls, [("run", argv, {"cwd": None})])
                self.assertEqual(copied.responses, [])

                for response in (completed(argv, returncode=23, stderr="disk full"), OSError("rsync unavailable")):
                    with self.subTest(response=response):
                        failed_snapshot = FakeHost((response,))
                        (snapshot,) = backuproots.taking(cast(Host, failed_snapshot), (row,), PARTIAL, prior)
                        outcome = snapshot.complete()
                        self.assertIsInstance(outcome, Problem)
                        assert isinstance(outcome, Problem)
                        self.assertIn("file snapshot", outcome.problem)
                        self.assertEqual(outcome.fix, "")
                        self.assertEqual(failed_snapshot.calls, [("run", argv, {"cwd": None})])
        failed = FakeHost((completed(self.copy_argv, returncode=23, stderr="disk full"),), existing=(SOURCE,))
        self.assertEqual(
            bound(failed, backupset.RootKind.STORE).complete(),
            Problem(f"store copy for {ROOT} failed: disk full", ""),
        )

    def test_store_take_refusals_keep_their_detail_and_fix(self) -> None:
        absent = FakeHost()
        self.assertEqual(
            bound(absent, backupset.RootKind.STORE).take(),
            Problem(f"content-addressed store root is missing: {SOURCE}", "Run sudo python3 -m gideon host provision --only disk-layout, then retry."),
        )
        for response, detail in (
            (completed(self.copy_argv, returncode=23, stderr="denied"), "denied"),
            (OSError("rsync unavailable"), "rsync unavailable"),
            (completed(self.copy_argv, stdout="statistics absent"), "reported no transferred file count"),
        ):
            with self.subTest(response=response):
                host = FakeHost((response,), existing=(SOURCE,))
                problem = bound(host, backupset.RootKind.STORE).take()
                self.assertIsInstance(problem, Problem)
                assert isinstance(problem, Problem)
                self.assertIn(detail, problem.problem)
                self.assertEqual(problem.fix, "")

        pin = backupset.ListingPin(f"{ROOT}.listing", 0, "a" * 64, 1, 7)
        old = previous()
        assert old.manifest is not None
        pinned = replace(old, manifest=replace(old.manifest, listings={ROOT: pin}))
        old_copy = os.path.join(backupset.files_dir(PREVIOUS), ROOT)
        argv = self.copy_argv[:-2] + (f"--link-dest={old_copy}/",) + self.copy_argv[-2:]
        link_check = ("find", self.copy, "-type", "f", "-links", "+1", "-print", "-quit")
        host = FakeHost((completed(argv, stdout="Number of regular files transferred: 0\n"), completed(link_check)), existing=(SOURCE,))
        self.assertEqual(bound(host, backupset.RootKind.STORE, pinned).take(), Problem(backuproots.LINK_PROBLEM, backuproots.LINK_FIX))
        self.assertEqual(host.calls[-1], ("run", link_check, {"cwd": None}))
        failed_link = FakeHost((completed(argv, stdout="Number of regular files transferred: 0\n"), completed(link_check, returncode=1, stderr="find denied")), existing=(SOURCE,))
        self.assertEqual(
            bound(failed_link, backupset.RootKind.STORE, pinned).take(),
            Problem(f"store link check for {ROOT} failed: find denied", ""),
        )

    def test_store_inventory_writes_sorted_listing_and_reads_pin(self) -> None:
        regex = r"\./\([0-9a-f]\{2\}\)/\([0-9a-f]\{2\}\)/\1\2[0-9a-f]\{60\}"
        stray_argv = (
            "find", ".", "-regextype", "posix-basic", "-mindepth", "1",
            "!", "(", "-type", "d", "-regex", r"\./[0-9a-f]\{2\}", ")",
            "!", "(", "-type", "d", "-regex", r"\./[0-9a-f]\{2\}/[0-9a-f]\{2\}", ")",
            "!", "(", "-type", "f", "-regex", regex, ")", "-print", "-quit",
        )
        find_argv = ("find", ".", "-regextype", "posix-basic", "-type", "f", "-regex", regex, "-fprintf", self.listing_path, r"%f\t%s\n")
        sort_argv = ("env", "LC_ALL=C", "sort", "-o", self.listing_path, self.listing_path)
        sha_argv = ("sha256sum", self.listing_path)
        awk_argv = ("awk", "-F", "\t", '{n++; b+=$2} END {printf "%.0f %.0f\\n", n, b}', self.listing_path)
        digest = "c" * 64
        responses = (
            completed(stray_argv), completed(find_argv), completed(sort_argv),
            completed(sha_argv, stdout=f"{digest}  {self.listing_path}\n"),
            completed(awk_argv, stdout="2 17\n"),
        )
        stats = {self.listing_path: os.stat_result((0, 0, 0, 0, 0, 0, 136, 0, 0, 0))}
        host = FakeHost(responses, stats=stats)
        self.assertEqual(
            bound(host, backupset.RootKind.STORE).inventory(),
            backuproots.Inventoried((), 17, backupset.ListingPin(f"{ROOT}.listing", 136, digest, 2, 17)),
        )
        self.assertEqual(host.calls, [
            ("run", stray_argv, {"cwd": self.copy}),
            ("run", find_argv, {"cwd": self.copy}),
            ("run", sort_argv, {"cwd": None}),
            ("run", sha_argv, {"cwd": None}),
            ("run", awk_argv, {"cwd": None}),
            ("stat", self.listing_path, {}),
        ])
        self.assertEqual(host.responses, [])

        offender = "./aa/bb/not-an-object"
        stray = FakeHost((completed(stray_argv, stdout=offender + "\n"),))
        self.assertEqual(
            bound(stray, backupset.RootKind.STORE).inventory(),
            Problem(f"{SOURCE}/aa/bb/not-an-object is not an object in its own shard directories", "Move the named path out of the live content-addressed store, then re-run backup run."),
        )
        malformed = FakeHost(responses[:-1] + (completed(awk_argv, stdout="bad count\n"),), stats=stats)
        problem = bound(malformed, backupset.RootKind.STORE).inventory()
        self.assertIsInstance(problem, Problem)
        assert isinstance(problem, Problem)
        self.assertIn("pinning", problem.problem)
        self.assertEqual(problem.fix, "")
        failures = (
            (0, stray_argv, "store shape check"),
            (1, find_argv, "store listing"),
            (2, sort_argv, "store listing sort"),
            (3, sha_argv, "hashing"),
            (4, awk_argv, "counting"),
        )
        for index, argv, phrase in failures:
            with self.subTest(tool=argv[0]):
                refused = list(responses[:index])
                refused.append(completed(argv, returncode=1, stderr="tool denied"))
                failed = FakeHost(refused, stats=stats)
                outcome = bound(failed, backupset.RootKind.STORE).inventory()
                self.assertIsInstance(outcome, Problem)
                assert isinstance(outcome, Problem)
                self.assertIn(phrase, outcome.problem)
                self.assertIn("tool denied", outcome.problem)
                self.assertEqual(outcome.fix, "")
                self.assertEqual(failed.responses, [])


class StoreRealTools(unittest.TestCase):
    """Real tools take and complete snapshot and store roots."""

    @unittest.skipUnless(shutil.which("rsync"), "rsync is required for the real snapshot case")
    def test_user_written_snapshot_second_pass_adds_and_rewrites_without_deleting(self) -> None:
        """A second pass keeps the first copy's files and counts changed files."""

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            partial = base / "sets" / "current.partial"
            row = backupset.InventoryRoot(ROOT, str(source), (), backupset.RootKind.SNAPSHOT, True, True)
            (root,) = backuproots.taking(RealHost(), (row,), str(partial), None)
            changed = source / "changed.txt"
            removed = source / "removed.txt"
            changed.write_bytes(b"first")
            removed.write_bytes(b"keep in the set")
            self.assertEqual(root.take(), backuproots.Taken(True, {}))

            changed.write_bytes(b"rewritten after the first pass")
            (source / "arrived.txt").write_bytes(b"new after the first pass")
            removed.unlink()
            self.assertEqual(root.complete(), backuproots.Completed(True, 2, f"{ROOT}: 2 file(s)"))

            copy = Path(backupset.files_dir(partial)) / ROOT
            self.assertEqual((copy / changed.name).read_bytes(), changed.read_bytes())
            self.assertEqual((copy / "arrived.txt").read_bytes(), b"new after the first pass")
            self.assertEqual((copy / removed.name).read_bytes(), b"keep in the set")

    @unittest.skipUnless(shutil.which("rsync"), "rsync is required for the real snapshot overlay case")
    def test_snapshot_overlay_keeps_selected_only_and_recovers_intermediate_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            live = base / "live"
            live.mkdir()
            selected_path = base / "staging" / "sets" / "selected"
            middle_path = base / "staging" / "sets" / "middle"
            newest_path = base / "staging" / "sets" / "newest"

            def copy_with(path: Path, files: Mapping[str, bytes]) -> tuple[backupset.Entry, ...]:
                copy = Path(backupset.files_dir(path)) / ROOT
                copy.mkdir(parents=True)
                entries: list[backupset.Entry] = []
                for name, data in files.items():
                    file = copy / name
                    file.write_bytes(data)
                    details = file.stat()
                    entries.append(backupset.Entry(
                        name, "f", len(data), details.st_uid, details.st_gid,
                        stat.S_IMODE(details.st_mode), details.st_mtime,
                        hashlib.sha256(data).hexdigest(),
                    ))
                return tuple(entries)

            selected_entries = copy_with(selected_path, {
                "shared": b"partial", "selected-only": b"selected bytes",
            })
            middle_entries = copy_with(middle_path, {
                "shared": b"whole uploaded file", "middle-only": b"intermediate bytes",
            })
            newest_entries = copy_with(newest_path, {"newest-only": b"newest bytes"})
            selected = replace(
                previous(), path=str(selected_path),
                manifest=manifest_with({ROOT: selected_entries}),
            )
            middle = replace(later_ref("middle", {ROOT: middle_entries}), path=str(middle_path))
            newest = replace(later_ref("newest", {ROOT: newest_entries}), path=str(newest_path))
            row = backupset.InventoryRoot(ROOT, str(live), (), backupset.RootKind.SNAPSHOT, True, True)
            (root,) = backuproots.held(RealHost(), (row,), selected, (middle, newest))

            self.assertIsNone(root.verify())
            outcome = root.put_back(backupset.AccountIds(os.getuid(), os.getgid()))
            self.assertIsInstance(outcome, backuproots.PutBack)
            assert isinstance(outcome, backuproots.PutBack)
            self.assertEqual(outcome.clauses, (f"{ROOT}: added or rewrote 3 file(s) (with middle, newest)",))
            self.assertEqual(outcome.added, {ROOT: 3})
            self.assertEqual((live / "shared").read_bytes(), b"whole uploaded file")
            self.assertEqual((live / "selected-only").read_bytes(), b"selected bytes")
            self.assertEqual((live / "middle-only").read_bytes(), b"intermediate bytes")
            self.assertEqual((live / "newest-only").read_bytes(), b"newest bytes")

    @unittest.skipUnless(shutil.which("rsync"), "rsync is required for the real store-copy case")
    def test_two_passes_include_a_later_object_and_pin_the_listing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            partial = Path(temporary) / "sets" / "current.partial"
            source.mkdir()
            row = backupset.InventoryRoot(ROOT, str(source), (".*",), backupset.RootKind.STORE, False)
            (root,) = backuproots.taking(RealHost(), (row,), str(partial), None)

            def put(data: bytes) -> str:
                name = hashlib.sha256(data).hexdigest()
                path = cas.object_path(name, source)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                return name

            first = put(b"first object")
            writer_temporary = cas.object_path(first, source).parent / ".writer-temp"
            writer_temporary.write_bytes(b"uncommitted")
            self.assertEqual(root.take(), backuproots.Taken(True, {}))
            second = put(b"second object")
            self.assertEqual(root.complete(), backuproots.Completed(True, 1, f"{ROOT}: 1 object(s)"))
            outcome = root.inventory()
            self.assertIsInstance(outcome, backuproots.Inventoried)
            assert isinstance(outcome, backuproots.Inventoried)
            self.assertEqual(outcome.entries, ())
            self.assertEqual(outcome.set_bytes, len(b"first object") + len(b"second object"))
            assert outcome.pin is not None
            listing_path = Path(backupset.listing_path(partial, ROOT))
            content = listing_path.read_text(encoding="utf-8")
            self.assertEqual(content, "".join(f"{name}\t{size}\n" for name, size in sorted(((first, len(b"first object")), (second, len(b"second object"))))))
            self.assertEqual(outcome.pin, backupset.ListingPin(listing_path.name, len(content.encode()), hashlib.sha256(content.encode()).hexdigest(), 2, outcome.set_bytes))
            for name in (first, second):
                self.assertEqual(cas.object_path(name, source).read_bytes(), cas.object_path(name, partial / "files" / ROOT).read_bytes())
            self.assertFalse((partial / "files" / ROOT / first[:2] / first[2:4] / writer_temporary.name).exists())

    @unittest.skipUnless(shutil.which("rsync"), "rsync is required for the real store put-back case")
    def test_put_back_adds_only_intact_missing_objects_and_keeps_live_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "live"
            source.mkdir()
            source.chmod(cas.DIRECTORY_MODE)
            set_path = base / "staging" / "sets" / "current"
            copy = Path(backupset.files_dir(set_path)) / ROOT
            copy.mkdir(parents=True)

            def put(directory: Path, data: bytes, *, name: str | None = None) -> str:
                object_name = name or hashlib.sha256(data).hexdigest()
                object_path = cas.object_path(object_name, directory)
                object_path.parent.mkdir(parents=True, exist_ok=True)
                object_path.write_bytes(data)
                return object_name

            held_name = put(copy, b"held in the set")
            put(source, b"different live bytes", name=held_name)
            damaged_name = put(copy, b"damaged copy", name=hashlib.sha256(b"original bytes").hexdigest())
            added_name = put(copy, b"new object")
            live_only = put(source, b"live only")
            names_and_sizes = sorted(
                ((held_name, len(b"held in the set")), (damaged_name, len(b"damaged copy")),
                 (added_name, len(b"new object")))
            )
            content = "".join(f"{name}\t{size}\n" for name, size in names_and_sizes)
            listing_path = Path(backupset.listing_path(set_path, ROOT))
            listing_path.write_text(content, encoding="utf-8")
            pin = backupset.ListingPin(
                listing_path.name, len(content.encode()), hashlib.sha256(content.encode()).hexdigest(),
                len(names_and_sizes), sum(size for _, size in names_and_sizes),
            )
            manifest = replace(manifest_with({}), listings={ROOT: pin})
            ref = backupset.SetRef("current", str(set_path), NOW, True, manifest)
            row = backupset.InventoryRoot(ROOT, str(source), (), backupset.RootKind.STORE, False)
            with mock.patch.object(backupset, "STAGING", str(base / "staging")):
                (root,) = backuproots.held(RealHost(), (row,), ref)
                self.assertIsNone(root.verify())
                outcome = root.put_back(backupset.AccountIds(2000, 2000))
            self.assertIsInstance(outcome, backuproots.PutBack)
            assert isinstance(outcome, backuproots.PutBack)
            self.assertEqual(outcome.counts, {ROOT: backuproots.StoreCounts(1, 1)})
            self.assertEqual(outcome.clauses, (f"{ROOT}: added 1 object(s), 1 left out",))
            self.assertIn(damaged_name, outcome.closing_lines[0])
            self.assertEqual(cas.object_path(held_name, source).read_bytes(), b"different live bytes")
            self.assertEqual(cas.object_path(live_only, source).read_bytes(), b"live only")
            self.assertFalse(cas.object_path(damaged_name, source).exists())
            added_path = cas.object_path(added_name, source)
            self.assertEqual(added_path.read_bytes(), b"new object")
            self.assertEqual(stat.S_IMODE(added_path.stat().st_mode), cas.OBJECT_MODE)
            self.assertEqual(stat.S_IMODE(added_path.parent.stat().st_mode), cas.DIRECTORY_MODE)
            self.assertFalse((base / f"staging.live-{ROOT}.listing").exists())

    @unittest.skipUnless(shutil.which("rsync"), "rsync is required for the real store put-back case")
    def test_later_copy_repairs_a_damaged_object_and_adds_its_own_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "live"
            source.mkdir()
            source.chmod(cas.DIRECTORY_MODE)
            selected_path = base / "staging" / "sets" / "selected"
            later_path = base / "staging" / "sets" / "later"

            def put(directory: Path, data: bytes, *, name: str | None = None) -> str:
                object_name = name or hashlib.sha256(data).hexdigest()
                path = cas.object_path(object_name, directory)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                return object_name

            selected_copy = Path(backupset.files_dir(selected_path)) / ROOT
            later_copy = Path(backupset.files_dir(later_path)) / ROOT
            selected_good = put(selected_copy, b"selected object")
            repaired = put(selected_copy, b"damaged object", name=hashlib.sha256(b"whole object").hexdigest())
            put(later_copy, b"whole object")
            later_only = put(later_copy, b"later object")
            live_only = put(source, b"live object")

            def pin_for(set_path: Path, sizes: Mapping[str, int]) -> backupset.ListingPin:
                content = "".join(f"{name}\t{size}\n" for name, size in sorted(sizes.items()))
                listing_path = Path(backupset.listing_path(set_path, ROOT))
                listing_path.write_text(content, encoding="utf-8")
                return backupset.ListingPin(
                    listing_path.name, len(content.encode()), hashlib.sha256(content.encode()).hexdigest(),
                    len(sizes), sum(sizes.values()),
                )

            selected_pin = pin_for(selected_path, {selected_good: len(b"selected object"), repaired: len(b"damaged object")})
            later_pin = pin_for(later_path, {repaired: len(b"whole object"), later_only: len(b"later object")})
            selected = backupset.SetRef(
                "selected", str(selected_path), NOW, True,
                replace(manifest_with({}), listings={ROOT: selected_pin}),
            )
            later = backupset.SetRef(
                "later", str(later_path), NOW, True,
                replace(manifest_with({}), listings={ROOT: later_pin}),
            )
            row = backupset.InventoryRoot(ROOT, str(source), (), backupset.RootKind.STORE, False)
            with mock.patch.object(backupset, "STAGING", str(base / "staging")):
                (root,) = backuproots.held(RealHost(), (row,), selected, (later,))
                self.assertIsNone(root.verify())
                outcome = root.put_back(backupset.AccountIds(2000, 2000))
            self.assertIsInstance(outcome, backuproots.PutBack)
            assert isinstance(outcome, backuproots.PutBack)
            self.assertEqual(outcome.counts, {ROOT: backuproots.StoreCounts(3, 0)})
            self.assertEqual(outcome.clauses, (f"{ROOT}: added 3 object(s), 0 left out (with later)",))
            self.assertEqual(outcome.closing_lines, ())
            for name, expected in ((selected_good, b"selected object"), (repaired, b"whole object"),
                                   (later_only, b"later object"), (live_only, b"live object")):
                self.assertEqual(cas.object_path(name, source).read_bytes(), expected)
            for name in (selected_good, repaired, later_only):
                path = cas.object_path(name, source)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), cas.OBJECT_MODE)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), cas.DIRECTORY_MODE)


class Inventory(unittest.TestCase):
    def test_snapshot_lists_then_hashes_sorted_entries_and_counts_files(self) -> None:
        directory = os.path.join(backupset.files_dir(PARTIAL), ROOT)
        find_argv = ("find", directory, "-printf", backupset.FIND_FORMAT)
        hash_argv = ("find", directory, "-type", "f", "-exec", "sha256sum", "{}", "+")
        first = entry("z-last", size=11)
        second = entry("a-first", size=5)
        host = FakeHost(
            (
                completed(find_argv, stdout=listing(first, second)),
                completed(hash_argv, stdout=f"{'c' * 64}  {directory}/z-last\n{'d' * 64}  {directory}/a-first\n"),
            )
        )

        outcome = bound(host, backupset.RootKind.SNAPSHOT).inventory()

        self.assertEqual(
            outcome,
            backuproots.Inventoried(
                (
                    backupset.Entry(".", "d", 4096, 1000, 1000, 0o755, 100.0, None),
                    replace(second, sha256="d" * 64),
                    replace(first, sha256="c" * 64),
                ),
                first.size + second.size,
            ),
        )
        self.assertEqual(host.calls, [("run", find_argv, {}), ("run", hash_argv, {})])
        self.assertEqual(host.responses, [])

    def test_repository_carries_unchanged_hashes_and_hashes_mutable_files(self) -> None:
        old_hash = "e" * 64
        old = previous(entry("old.dat", sha256=old_hash), entry("backup.info", sha256="f" * 64))
        find_argv = ("find", SOURCE, "-printf", backupset.FIND_FORMAT)
        hash_argv = ("sha256sum", "--", os.path.join(SOURCE, "backup.info"), os.path.join(SOURCE, "new.dat"))
        host = FakeHost(
            (
                completed(find_argv, stdout=listing(entry("new.dat"), entry("old.dat"), entry("backup.info"))),
                completed(
                    hash_argv,
                    stdout=f"{'a' * 64}  {SOURCE}/backup.info\n{'b' * 64}  {SOURCE}/new.dat\n",
                ),
            )
        )

        outcome = bound(host, backupset.RootKind.REPOSITORY, old).inventory()

        self.assertEqual(
            outcome,
            backuproots.Inventoried(
                (
                    backupset.Entry(".", "d", 4096, 1000, 1000, 0o755, 100.0, None),
                    entry("backup.info", sha256="a" * 64),
                    entry("new.dat", sha256="b" * 64),
                    entry("old.dat", sha256=old_hash),
                ),
                0,
            ),
        )
        self.assertEqual(host.calls, [("run", find_argv, {}), ("run", hash_argv, {})])

    def test_repository_hashes_in_chunks_of_two_hundred(self) -> None:
        paths = tuple(f"file-{number:03}" for number in range(201))
        entries = tuple(entry(path) for path in paths)
        find_argv = ("find", SOURCE, "-printf", backupset.FIND_FORMAT)
        first_argv = ("sha256sum", "--", *(os.path.join(SOURCE, path) for path in paths[:200]))
        last_argv = ("sha256sum", "--", os.path.join(SOURCE, paths[200]))
        host = FakeHost(
            (
                completed(find_argv, stdout=listing(*entries)),
                completed(first_argv, stdout="".join(f"{'a' * 64}  {SOURCE}/{path}\n" for path in paths[:200])),
                completed(last_argv, stdout=f"{'a' * 64}  {SOURCE}/{paths[200]}\n"),
            )
        )

        outcome = bound(host, backupset.RootKind.REPOSITORY).inventory()

        self.assertIsInstance(outcome, backuproots.Inventoried)
        assert isinstance(outcome, backuproots.Inventoried)
        self.assertEqual(outcome.set_bytes, 0)
        self.assertEqual(tuple(item.path for item in outcome.entries[1:]), paths)
        self.assertTrue(all(item.sha256 == "a" * 64 for item in outcome.entries[1:]))
        self.assertEqual(
            host.calls,
            [("run", find_argv, {}), ("run", first_argv, {}), ("run", last_argv, {})],
        )

    def test_snapshot_and_repository_return_exact_listing_and_hash_refusals(self) -> None:
        for kind in (backupset.RootKind.SNAPSHOT, backupset.RootKind.REPOSITORY):
            directory = os.path.join(backupset.files_dir(PARTIAL), ROOT) if kind is backupset.RootKind.SNAPSHOT else SOURCE
            find_argv = ("find", directory, "-printf", backupset.FIND_FORMAT)
            hash_argv = (
                ("find", directory, "-type", "f", "-exec", "sha256sum", "{}", "+")
                if kind is backupset.RootKind.SNAPSHOT
                else ("sha256sum", "--", os.path.join(directory, "file"))
            )
            cases: tuple[tuple[str, tuple[subprocess.CompletedProcess[str] | OSError, ...], str, tuple[tuple[str, ...], ...]], ...] = (
                ("find status", (completed(find_argv, returncode=1, stderr="denied"),), f"find failed for {directory}: denied", (find_argv,)),
                ("find raised", (OSError("unavailable"),), f"find failed for {directory}: unavailable", (find_argv,)),
                ("find parse", (completed(find_argv, stdout="bad\0"),), f"find listing was malformed for {directory}: find listing record 1 has the wrong field count", (find_argv,)),
                ("hash status", (completed(find_argv, stdout=listing(entry("file"))), completed(hash_argv, returncode=1, stderr="denied")), f"hashing failed for {directory}: denied", (find_argv, hash_argv)),
                ("hash raised", (completed(find_argv, stdout=listing(entry("file"))), OSError("unavailable")), f"hashing failed for {directory}: unavailable", (find_argv, hash_argv)),
                ("hash parse", (completed(find_argv, stdout=listing(entry("file"))), completed(hash_argv, stdout="bad\n")), f"hash listing was malformed for {directory}: sha256sum line 1 is malformed", (find_argv, hash_argv)),
            )
            for label, responses, detail, expected_argv in cases:
                with self.subTest(kind=kind, case=label):
                    host = FakeHost(responses)
                    self.assertEqual(bound(host, kind).inventory(), Problem(detail, ""))
                    self.assertEqual(host.calls, [("run", argv, {}) for argv in expected_argv])
                    self.assertEqual(host.responses, [])

    def test_merge_hashes_keeps_its_own_fix(self) -> None:
        directory = os.path.join(backupset.files_dir(PARTIAL), ROOT)
        find_argv = ("find", directory, "-printf", backupset.FIND_FORMAT)
        hash_argv = ("find", directory, "-type", "f", "-exec", "sha256sum", "{}", "+")
        host = FakeHost((completed(find_argv, stdout=listing(entry("file"))), completed(hash_argv)))

        outcome = bound(host, backupset.RootKind.SNAPSHOT).inventory()
        expected = backupset.merge_hashes((entry("file"),), {})

        self.assertIsInstance(expected, Problem)
        self.assertEqual(outcome, expected)
        assert isinstance(outcome, Problem)
        self.assertTrue(outcome.fix)
        self.assertEqual(host.calls, [("run", find_argv, {}), ("run", hash_argv, {})])


class Check(unittest.TestCase):
    def test_snapshot_samples_one_percent_and_verify_all_keeps_manifest_order(self) -> None:
        paths = tuple(f"file-{number:03}" for number in range(201))
        recorded = tuple(entry(path, sha256="a" * 64) for path in reversed(paths))
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.SNAPSHOT, True)
        (root,) = backuproots.pushed(cast(Host, FakeHost()), (row,), manifest_with({ROOT: recorded}), "pre-example")
        place = f"sets/pre-example/{backupset.FILES_DIR}/{ROOT}"

        self.assertEqual(
            root.check(verify_all=False),
            backuproots.Checked(
                (),
                tuple((f"{place}/{path}", "a" * 64) for path in (paths[0], paths[100], paths[200])),
            ),
        )
        self.assertEqual(
            root.check(verify_all=True),
            backuproots.Checked((), tuple((f"{place}/{path}", "a" * 64) for path in reversed(paths))),
        )

    def test_repository_adds_mandatory_files_before_the_sample(self) -> None:
        source = os.path.join(backupset.STAGING, "example-repository")
        row = backupset.InventoryRoot(ROOT, source, (), backupset.RootKind.REPOSITORY, False)
        special = (
            f"backup/{pgbackrest.STANZA}/backup.info",
            f"archive/{pgbackrest.STANZA}/archive.info",
            f"backup/{pgbackrest.STANZA}/example-backup/backup.manifest",
        )
        recorded = tuple(entry(path, sha256=character * 64) for path, character in zip(special, "abc", strict=True))
        (root,) = backuproots.pushed(cast(Host, FakeHost()), (row,), manifest_with({ROOT: recorded}), "pre-example")
        place = os.path.relpath(source, backupset.STAGING)

        self.assertEqual(
            root.check(verify_all=False),
            backuproots.Checked(
                tuple((f"{place}/{path}", character * 64) for path, character in zip(special, "abc", strict=True)),
                ((f"{place}/{special[1]}", "b" * 64),),
            ),
        )
        self.assertEqual(
            root.check(verify_all=True),
            backuproots.Checked(
                tuple((f"{place}/{path}", character * 64) for path, character in zip(special, "abc", strict=True)),
                tuple((f"{place}/{path}", character * 64) for path, character in zip(special, "abc", strict=True)),
            ),
        )

    def test_repository_refuses_each_missing_mandatory_file(self) -> None:
        source = os.path.join(backupset.STAGING, "example-repository")
        row = backupset.InventoryRoot(ROOT, source, (), backupset.RootKind.REPOSITORY, False)
        special = (
            f"backup/{pgbackrest.STANZA}/backup.info",
            f"archive/{pgbackrest.STANZA}/archive.info",
            f"backup/{pgbackrest.STANZA}/example-backup/backup.manifest",
        )
        for missing in special:
            with self.subTest(missing=missing):
                recorded = tuple(entry(path, sha256="a" * 64) for path in special if path != missing)
                (root,) = backuproots.pushed(cast(Host, FakeHost()), (row,), manifest_with({ROOT: recorded}), "pre-example")
                self.assertEqual(
                    root.check(verify_all=False),
                    Problem(f"the set's repository inventory lacks {missing}", ""),
                )

    def test_unknown_name_uses_snapshot_layout_and_missing_snapshot_is_empty(self) -> None:
        known = backupset.InventoryRoot("known", SOURCE, (), backupset.RootKind.SNAPSHOT, True)
        recorded = entry("file", sha256="a" * 64)
        roots = backuproots.pushed(
            cast(Host, FakeHost()), (known,), manifest_with({"unknown": (recorded,)}), "pre-example"
        )

        self.assertEqual(tuple(root.name for root in roots), ("known", "unknown"))
        self.assertEqual(roots[0].check(verify_all=False), backuproots.Checked((), ()))
        self.assertEqual(
            roots[1].check(verify_all=False),
            backuproots.Checked(
                (), ((f"sets/pre-example/{backupset.FILES_DIR}/unknown/file", "a" * 64),)
            ),
        )

    def test_missing_repository_refuses_its_first_mandatory_file(self) -> None:
        source = os.path.join(backupset.STAGING, "example-repository")
        row = backupset.InventoryRoot(ROOT, source, (), backupset.RootKind.REPOSITORY, False)
        (root,) = backuproots.pushed(cast(Host, FakeHost()), (row,), manifest_with({}), "pre-example")

        self.assertEqual(
            root.check(verify_all=False),
            Problem(f"the set's repository inventory lacks backup/{pgbackrest.STANZA}/backup.info", ""),
        )


class StoreCheck(unittest.TestCase):
    """A pushed store contributes its listing and a bounded object sample."""

    def test_listing_is_mandatory_and_sample_uses_every_hundredth_line(self) -> None:
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.STORE, False)
        lines = tuple(f"{index:064x}\t7\n" for index in range(201))
        pin = backupset.ListingPin(f"{ROOT}.listing", len("".join(lines)), "c" * 64, len(lines), 7 * len(lines))
        value = replace(manifest_with({}), listings={ROOT: pin})
        listing_path = os.path.join(backupset.set_dir("pre-example"), pin.file)
        awk_argv = ("awk", "NR % 100 == 1", listing_path)
        sample = "".join(lines[index] for index in (0, 100, 200))
        host = FakeHost((completed(awk_argv, stdout=sample),), files={listing_path: "".join(lines)})
        (root,) = backuproots.pushed(cast(Host, host), (row,), value, "pre-example")

        place = f"sets/pre-example/{backupset.FILES_DIR}/{ROOT}"
        mandatory = ((f"sets/pre-example/{pin.file}", pin.sha256),)
        expected_sample = tuple(
            (f"{place}/{name[:2]}/{name[2:4]}/{name}", name)
            for name in (f"{index:064x}" for index in (0, 100, 200))
        )
        self.assertEqual(root.check(verify_all=False), backuproots.Checked(mandatory, expected_sample))
        self.assertEqual(host.calls, [("run", awk_argv, {"cwd": None})])
        self.assertEqual(host.responses, [])

        all_sample = tuple(
            (f"{place}/{name[:2]}/{name[2:4]}/{name}", name)
            for name in (f"{index:064x}" for index in range(201))
        )
        self.assertEqual(root.check(verify_all=True), backuproots.Checked(mandatory, all_sample))
        self.assertEqual(host.calls[-1], ("read_text", listing_path, {}))

    def test_store_without_pin_checks_nothing_and_arrival_uses_pin_bytes(self) -> None:
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.STORE, False)
        host = FakeHost()
        (unlisted,) = backuproots.pushed(cast(Host, host), (row,), manifest_with({}), "pre-example")
        self.assertEqual(unlisted.check(verify_all=False), backuproots.Checked((), ()))
        self.assertEqual(unlisted.check(verify_all=True), backuproots.Checked((), ()))
        self.assertEqual(unlisted.arrived(None), 0)
        self.assertEqual(host.calls, [])

        pin = backupset.ListingPin(f"{ROOT}.listing", 10, "a" * 64, 2, 100)
        current = replace(manifest_with({}), listings={ROOT: pin})
        (root,) = backuproots.pushed(cast(Host, host), (row,), current, "pre-example")
        self.assertEqual(root.arrived(None), pin.bytes)
        for previous_bytes, expected in ((0, 100), (40, 60), (100, 0), (120, 0)):
            with self.subTest(previous_bytes=previous_bytes):
                older = replace(current, listings={ROOT: replace(pin, bytes=previous_bytes)})
                self.assertEqual(root.arrived(older), expected)
        self.assertEqual(root.arrived(manifest_with({})), pin.bytes)
        for kind in (backupset.RootKind.SNAPSHOT, backupset.RootKind.REPOSITORY):
            with self.subTest(kind=kind):
                source = SOURCE if kind is backupset.RootKind.SNAPSHOT else os.path.join(backupset.STAGING, ROOT)
                other_row = backupset.InventoryRoot(ROOT, source, (), kind, False)
                (other,) = backuproots.pushed(cast(Host, host), (other_row,), current, "pre-example")
                self.assertEqual(other.arrived(None), 0)
                self.assertEqual(other.arrived(current), 0)

    def test_listing_tool_failure_and_malformed_line_refuse(self) -> None:
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.STORE, False)
        pin = backupset.ListingPin(f"{ROOT}.listing", 0, "a" * 64, 0, 0)
        value = replace(manifest_with({}), listings={ROOT: pin})
        listing_path = os.path.join(backupset.set_dir("pre-example"), pin.file)
        awk_argv = ("awk", "NR % 100 == 1", listing_path)
        failed = FakeHost((completed(awk_argv, returncode=1, stderr="denied"),))
        (root,) = backuproots.pushed(cast(Host, failed), (row,), value, "pre-example")
        self.assertEqual(root.check(verify_all=False), Problem(f"store listing sample for {ROOT} failed: denied", ""))
        malformed = FakeHost((completed(awk_argv, stdout="../bad\t7\n"),))
        (root,) = backuproots.pushed(cast(Host, malformed), (row,), value, "pre-example")
        self.assertEqual(root.check(verify_all=False), Problem(f"store listing sample line 1 is malformed for {ROOT}", ""))


class Fetch(unittest.TestCase):
    def test_arrival_does_nothing_for_snapshot_then_reowns_repository(self) -> None:
        side = f"{backupset.STAGING}.fetch-example"
        rendered = "/example/rendered"
        source = os.path.join(backupset.STAGING, "example-repository")
        rows = (
            backupset.InventoryRoot("snapshot", SOURCE, (), backupset.RootKind.SNAPSHOT, True),
            backupset.InventoryRoot(ROOT, source, (), backupset.RootKind.REPOSITORY, False),
        )
        repository = os.path.join(side, os.path.relpath(source, backupset.STAGING))
        uid_argv = tuple(stack.exec_argv(rendered, "postgres", "id", "-u", "postgres"))
        gid_argv = tuple(stack.exec_argv(rendered, "postgres", "id", "-g", "postgres"))
        commands = (
            ("chown", "-R", "-h", "31:32", repository),
            ("find", repository, "-type", "d", "-exec", "chmod", "0750", "{}", "+"),
            ("find", repository, "-type", "f", "-exec", "chmod", "0640", "{}", "+"),
        )
        host = FakeHost(
            (completed(uid_argv, stdout="31\n"), completed(gid_argv, stdout="32\n"),
             *(completed(argv) for argv in commands))
        )
        roots = backuproots.landed(cast(Host, host), rows, side)

        self.assertEqual(tuple(root.name for root in roots), ("snapshot", ROOT))
        self.assertIsNone(roots[0].arrive(rendered))
        self.assertIsNone(roots[1].arrive(rendered))
        self.assertEqual(
            host.calls,
            [("run", uid_argv, {"timeout": 60.0}), ("run", gid_argv, {"timeout": 60.0}),
             *(("run", argv, {"input": None, "timeout": None}) for argv in commands)],
        )

    def test_arrival_preserves_identity_lookup_problem_and_fix(self) -> None:
        side = f"{backupset.STAGING}.fetch-example"
        rendered = "/example/rendered"
        source = os.path.join(backupset.STAGING, "example-repository")
        row = backupset.InventoryRoot(ROOT, source, (), backupset.RootKind.REPOSITORY, False)
        exec_argv = tuple(stack.exec_argv(rendered, "postgres", "id", "-u", "postgres"))
        fallback_argv = tuple(stack.compose_argv(rendered, "run", "--rm", "--no-deps", "postgres", "id", "-u", "postgres"))
        host = FakeHost((completed(exec_argv, returncode=1, stderr="unavailable"), completed(fallback_argv, returncode=1, stderr="denied")))
        (root,) = backuproots.landed(cast(Host, host), (row,), side)

        self.assertEqual(
            root.arrive(rendered),
            Problem("pgBackRest one-off postgres -u lookup failed: denied", stack.logs_fix(rendered, "postgres")),
        )
        self.assertEqual(host.calls, [("run", exec_argv, {"timeout": 60.0}), ("run", fallback_argv, {"timeout": 60.0})])

    def test_arrival_uses_empty_fix_when_identity_has_no_diagnostic(self) -> None:
        side = f"{backupset.STAGING}.fetch-example"
        source = os.path.join(backupset.STAGING, "example-repository")
        row = backupset.InventoryRoot(ROOT, source, (), backupset.RootKind.REPOSITORY, False)
        host = FakeHost()
        (root,) = backuproots.landed(cast(Host, host), (row,), side)

        with mock.patch.object(pgbackrest, "container_identity", return_value=(None, None)):
            self.assertEqual(root.arrive("/example/rendered"), Problem("postgres identity lookup failed", ""))
        self.assertEqual(host.calls, [])

    def test_arrival_reports_each_failed_mode_or_owner_command(self) -> None:
        side = f"{backupset.STAGING}.fetch-example"
        rendered = "/example/rendered"
        source = os.path.join(backupset.STAGING, "example-repository")
        row = backupset.InventoryRoot(ROOT, source, (), backupset.RootKind.REPOSITORY, False)
        repository = os.path.join(side, os.path.relpath(source, backupset.STAGING))
        uid_argv = tuple(stack.exec_argv(rendered, "postgres", "id", "-u", "postgres"))
        gid_argv = tuple(stack.exec_argv(rendered, "postgres", "id", "-g", "postgres"))
        commands = (
            (("chown", "-R", "-h", "31:32", repository), f"re-owned {repository}"),
            (("find", repository, "-type", "d", "-exec", "chmod", "0750", "{}", "+"), f"applied pgBackRest directory modes under {repository}"),
            (("find", repository, "-type", "f", "-exec", "chmod", "0640", "{}", "+"), f"applied pgBackRest file modes under {repository}"),
        )
        for index, (argv, detail) in enumerate(commands):
            with self.subTest(command=argv[0], index=index):
                responses = [completed(uid_argv, stdout="31\n"), completed(gid_argv, stdout="32\n")]
                responses.extend(completed(prior) for prior, _ in commands[:index])
                responses.append(completed(argv, returncode=1, stderr="denied"))
                host = FakeHost(responses)
                (root,) = backuproots.landed(cast(Host, host), (row,), side)
                self.assertEqual(root.arrive(rendered), Problem(f"{detail}: denied", ""))
                self.assertEqual(
                    host.calls,
                    [("run", uid_argv, {"timeout": 60.0}), ("run", gid_argv, {"timeout": 60.0}),
                     *(("run", prior, {"input": None, "timeout": None}) for prior, _ in commands[:index + 1])],
                )

    def test_fetched_snapshot_walks_with_trailing_slash_and_maps_claims(self) -> None:
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.SNAPSHOT, True)
        root_entry = backupset.Entry(".", "d", 4096, 1000, 1000, 0o755, 100.0, None)
        file_entry = entry("file")
        manifest = replace(
            manifest_with({ROOT: (root_entry, file_entry)}),
            gideon_ids=backupset.AccountIds(1000, 1000),
        )
        ref = backupset.SetRef("pre-example", PREVIOUS, NOW, True, manifest)
        base = os.path.join(backupset.files_dir(PREVIOUS), ROOT, "")
        find_argv = ("find", base, "-printf", backupset.FIND_FORMAT)
        host = FakeHost((completed(find_argv, stdout=listing(file_entry)),))
        (root,) = backuproots.fetched(cast(Host, host), (row,), "/example/side", (ref,))

        outcome = root.reown(backupset.AccountIds(2000, 2000))

        self.assertIsInstance(outcome, backuproots.Claims)
        assert isinstance(outcome, backuproots.Claims)
        self.assertEqual((outcome.entries_claimed, outcome.gideon_owned, outcome.owner_paths), (2, 2, 2))
        self.assertEqual(outcome.owners, {(2000, 2000, False): [os.path.join(base, "."), os.path.join(base, "file")]})
        self.assertEqual(outcome.modes, {0o755: [os.path.join(base, ".")], 0o644: [os.path.join(base, "file")]})
        self.assertEqual(host.calls, [("run", find_argv, {})])

    def test_fetched_repository_walks_then_applies_only_mode_overrides(self) -> None:
        side = f"{backupset.STAGING}.fetch-example"
        source = os.path.join(backupset.STAGING, "example-repository")
        row = backupset.InventoryRoot(ROOT, source, (), backupset.RootKind.REPOSITORY, False)
        root_entry = backupset.Entry(".", "d", 4096, 1000, 1000, 0o700, 100.0, None)
        file_entry = entry("file", sha256="a" * 64)
        file_entry = replace(file_entry, mode=0o600)
        manifest = manifest_with({ROOT: (root_entry, file_entry)})
        ref = backupset.SetRef("pre-example", PREVIOUS, NOW, True, manifest)
        base = os.path.join(side, os.path.relpath(source, backupset.STAGING))
        find_argv = ("find", base, "-printf", backupset.FIND_FORMAT)
        mode_file = ("chmod", "0600", "--", os.path.join(base, "file"))
        mode_root = ("chmod", "0700", "--", os.path.join(base, "."))
        host = FakeHost((completed(find_argv, stdout=listing(file_entry)), completed(mode_file), completed(mode_root)))
        (root,) = backuproots.fetched(cast(Host, host), (row,), side, (ref,))

        outcome = root.reown(backupset.AccountIds(2000, 2000))

        self.assertIsInstance(outcome, backuproots.Claims)
        assert isinstance(outcome, backuproots.Claims)
        self.assertEqual((outcome.entries_claimed, outcome.gideon_owned, outcome.owner_paths), (0, 0, 0))
        self.assertEqual(
            host.calls,
            [("run", find_argv, {}), ("run", mode_file, {"input": None, "timeout": None}),
             ("run", mode_root, {"input": None, "timeout": None})],
        )

    def test_fetched_repository_mode_refusal_has_empty_fix(self) -> None:
        side = f"{backupset.STAGING}.fetch-example"
        source = os.path.join(backupset.STAGING, "example-repository")
        row = backupset.InventoryRoot(ROOT, source, (), backupset.RootKind.REPOSITORY, False)
        root_entry = backupset.Entry(".", "d", 4096, 1000, 1000, 0o700, 100.0, None)
        ref = backupset.SetRef("pre-example", PREVIOUS, NOW, True, manifest_with({ROOT: (root_entry,)}))
        base = os.path.join(side, os.path.relpath(source, backupset.STAGING))
        find_argv = ("find", base, "-printf", backupset.FIND_FORMAT)
        mode_argv = ("chmod", "0700", "--", os.path.join(base, "."))
        host = FakeHost((completed(find_argv, stdout=listing()), completed(mode_argv, returncode=1, stderr="denied")))
        (root,) = backuproots.fetched(cast(Host, host), (row,), side, (ref,))

        self.assertEqual(
            root.reown(backupset.AccountIds(2000, 2000)),
            Problem("applied mode 0700 to 1 path(s): denied", ""),
        )
        self.assertEqual(host.calls, [("run", find_argv, {}), ("run", mode_argv, {"input": None, "timeout": None})])

    def test_fetched_binds_repository_first_then_each_sets_manifest_order(self) -> None:
        side = f"{backupset.STAGING}.fetch-example"
        source = os.path.join(backupset.STAGING, "example-repository")
        rows = (
            backupset.InventoryRoot("second", SOURCE, (), backupset.RootKind.SNAPSHOT, True),
            backupset.InventoryRoot(ROOT, source, (), backupset.RootKind.REPOSITORY, False),
            backupset.InventoryRoot("first", SOURCE, (), backupset.RootKind.SNAPSHOT, True),
        )
        newest_manifest = manifest_with({"first": (entry("a"),), "second": (entry("b"),), ROOT: ()})
        older_manifest = manifest_with({"second": (entry("c"),), "unknown": (entry("d"),)})
        newest = backupset.SetRef("pre-example", PREVIOUS, NOW, True, newest_manifest)
        older = backupset.SetRef("pre-older", "/example/staging/sets/older", NOW, True, older_manifest)
        host = FakeHost()

        roots = backuproots.fetched(cast(Host, host), rows, side, (newest, older))

        self.assertEqual(tuple(root.name for root in roots), (ROOT, "first", "second", "second"))
        self.assertEqual(
            tuple(root.base for root in roots),
            (
                os.path.join(side, os.path.relpath(source, backupset.STAGING)),
                os.path.join(backupset.files_dir(PREVIOUS), "first", ""),
                os.path.join(backupset.files_dir(PREVIOUS), "second", ""),
                os.path.join(backupset.files_dir(older.path), "second", ""),
            ),
        )
        self.assertEqual(host.calls, [])

    def test_fetched_unknown_name_is_skipped_and_missing_repository_walks_empty(self) -> None:
        side = f"{backupset.STAGING}.fetch-example"
        source = os.path.join(backupset.STAGING, "example-repository")
        rows = (
            backupset.InventoryRoot("known", SOURCE, (), backupset.RootKind.SNAPSHOT, True),
            backupset.InventoryRoot(ROOT, source, (), backupset.RootKind.REPOSITORY, False),
        )
        manifest = manifest_with({"unknown": (entry("file"),)})
        ref = backupset.SetRef("pre-example", PREVIOUS, NOW, True, manifest)
        base = os.path.join(side, os.path.relpath(source, backupset.STAGING))
        find_argv = ("find", base, "-printf", backupset.FIND_FORMAT)
        host = FakeHost((completed(find_argv, stdout=listing()),))
        roots = backuproots.fetched(cast(Host, host), rows, side, (ref,))

        self.assertEqual(tuple(root.name for root in roots), (ROOT,))
        outcome = roots[0].reown(backupset.AccountIds(2000, 2000))
        self.assertIsInstance(outcome, backuproots.Claims)
        assert isinstance(outcome, backuproots.Claims)
        self.assertEqual((outcome.entries_claimed, outcome.owner_paths), (0, 0))
        self.assertEqual(host.calls, [("run", find_argv, {})])

    def test_fetched_physical_mismatch_keeps_its_own_fix(self) -> None:
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.SNAPSHOT, True)
        manifest = manifest_with({ROOT: (entry("missing"),)})
        ref = backupset.SetRef("pre-example", PREVIOUS, NOW, True, manifest)
        base = os.path.join(backupset.files_dir(PREVIOUS), ROOT, "")
        find_argv = ("find", base, "-printf", backupset.FIND_FORMAT)
        host = FakeHost((completed(find_argv, stdout=listing()),))
        (root,) = backuproots.fetched(cast(Host, host), (row,), "/example/side", (ref,))

        outcome = root.reown(backupset.AccountIds(2000, 2000))

        self.assertIsInstance(outcome, Problem)
        assert isinstance(outcome, Problem)
        self.assertEqual(
            outcome.problem,
            f"1 inventoried path(s) under {base} are not what the manifest declares "
            "(a link in place of a file or directory, or a path beneath a link)",
        )
        self.assertIn("backup push --verify-all", outcome.fix)
        self.assertEqual(host.calls, [("run", find_argv, {})])


class HeldRoots(unittest.TestCase):
    def test_supplies_requires_a_store_pin_or_user_written_in_place_inventory(self) -> None:
        store = backupset.InventoryRoot("store", SOURCE, (), backupset.RootKind.STORE, False)
        snapshot = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.SNAPSHOT, True, True)
        pin = backupset.ListingPin("store.listing", 0, "a" * 64, 0, 0)
        empty = manifest_with({})

        self.assertTrue(backuproots.supplies(replace(empty, listings={store.name: pin}), (store, snapshot)))
        self.assertTrue(backuproots.supplies(manifest_with({ROOT: ()}), (store, snapshot)))
        self.assertFalse(backuproots.supplies(empty, (store, snapshot)))
        self.assertFalse(backuproots.supplies(manifest_with({ROOT: ()}), (store, replace(snapshot, user_written=False))))
        self.assertFalse(backuproots.supplies(manifest_with({ROOT: ()}), (store, replace(snapshot, restore_in_place=False))))

    def test_held_binds_all_later_sets_to_snapshot_and_newest_listing_to_store(self) -> None:
        snapshot = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.SNAPSHOT, True, True)
        store = backupset.InventoryRoot("store", SOURCE, (), backupset.RootKind.STORE, False)
        pin = backupset.ListingPin("store.listing", 0, "a" * 64, 0, 0)
        selected = replace(previous(), manifest=manifest_with({ROOT: ()}))
        first = later_ref("later-one", {ROOT: ()}, listings={store.name: pin})
        second = later_ref("later-two", {ROOT: ()}, listings={store.name: pin})
        uploads_only = later_ref("later-three", {ROOT: ()})

        snapshot_root, store_root = backuproots.held(
            cast(Host, FakeHost()), (snapshot, store), selected, (first, second, uploads_only)
        )
        self.assertIsInstance(snapshot_root, backuproots._HeldSnapshot)
        self.assertIsInstance(store_root, backuproots._HeldStore)
        assert isinstance(snapshot_root, backuproots._HeldSnapshot)
        assert isinstance(store_root, backuproots._HeldStore)
        self.assertEqual(snapshot_root.later, (first, second, uploads_only))
        self.assertIs(store_root.later, second)

        (store_root,) = backuproots.held(cast(Host, FakeHost()), (store,), selected, (uploads_only,))
        assert isinstance(store_root, backuproots._HeldStore)
        self.assertIsNone(store_root.later)

    def test_snapshot_verify_walks_exact_copy_then_checks_hashes(self) -> None:
        file = entry("nested/file", sha256="a" * 64)
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.SNAPSHOT, True)
        ref = replace(previous(), manifest=manifest_with({ROOT: (file,)}))
        copy = os.path.join(backupset.files_dir(ref.path), ROOT)
        find_argv = ("find", copy, "-printf", backupset.FIND_FORMAT)
        hash_argv = ("sha256sum", "-c", "-")
        host = FakeHost((completed(find_argv, stdout=listing(file)), completed(hash_argv, stdout="nested/file: OK\n")))
        (root,) = backuproots.held(cast(Host, host), (row,), ref)

        self.assertEqual((root.name, root.source, root.copy, root.entries), (ROOT, SOURCE, copy, (file,)))
        self.assertIsNone(root.verify())
        self.assertEqual(
            host.calls,
            [
                ("run", find_argv, {}),
                ("run", hash_argv, {"input": f"{'a' * 64}  nested/file\n", "cwd": copy}),
            ],
        )
        self.assertIsNone(root.prove("/example/rendered"))
        self.assertEqual(len(host.calls), 2)

    def test_snapshot_verify_walks_and_hashes_each_later_copy(self) -> None:
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.SNAPSHOT, True, True)
        selected_file = entry("selected", sha256="a" * 64)
        first_file = entry("first", sha256="b" * 64)
        second_file = entry("second", sha256="c" * 64)
        selected = replace(previous(), manifest=manifest_with({ROOT: (selected_file,)}))
        first = later_ref("later-one", {ROOT: (first_file,)})
        second = later_ref("later-two", {ROOT: (second_file,)})
        hash_argv = ("sha256sum", "-c", "-")
        copies = (
            (selected, selected_file), (first, first_file), (second, second_file),
        )
        responses: list[subprocess.CompletedProcess[str]] = []
        calls: list[tuple[str, object, object]] = []
        for ref, file in copies:
            copy = os.path.join(backupset.files_dir(ref.path), ROOT)
            find_argv = ("find", copy, "-printf", backupset.FIND_FORMAT)
            responses.extend((
                completed(find_argv, stdout=listing(file)),
                completed(hash_argv, stdout=f"{file.path}: OK\n"),
            ))
            calls.extend((
                ("run", find_argv, {}),
                ("run", hash_argv, {"input": f"{file.sha256}  {file.path}\n", "cwd": copy}),
            ))
        host = FakeHost(responses)
        (root,) = backuproots.held(cast(Host, host), (row,), selected, (first, second))
        self.assertIsNone(root.verify())
        self.assertEqual(host.calls, calls)
        self.assertEqual(host.responses, [])

        failed = FakeHost((*responses[:-1], completed(hash_argv, returncode=1, stdout="second: FAILED\n")))
        (root,) = backuproots.held(cast(Host, failed), (row,), selected, (first, second))
        self.assertEqual(
            root.verify(), Problem(f"checksum verification failed for 1 path(s) in {ROOT}", "")
        )
        self.assertEqual(failed.calls, calls)

    def test_snapshot_verify_refuses_a_stray_path_with_walk_fix(self) -> None:
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.SNAPSHOT, True)
        ref = replace(previous(), manifest=manifest_with({ROOT: ()}))
        copy = os.path.join(backupset.files_dir(ref.path), ROOT)
        find_argv = ("find", copy, "-printf", backupset.FIND_FORMAT)
        host = FakeHost((completed(find_argv, stdout=listing(entry("stray"))),))
        (root,) = backuproots.held(cast(Host, host), (row,), ref)

        problem = root.verify()

        self.assertEqual(
            problem,
            Problem(
                f"1 path(s) under {copy} are not in the manifest: stray",
                "The fetched or restored tree does not match its manifest; re-run "
                "sudo python3 -m gideon backup push --verify-all on the source box, "
                "then retry restore.",
            ),
        )
        self.assertEqual(host.calls, [("run", find_argv, {})])

    def test_snapshot_verify_refuses_find_and_missing_hash(self) -> None:
        file = entry("file")
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.SNAPSHOT, True)
        ref = replace(previous(), manifest=manifest_with({ROOT: (file,)}))
        copy = os.path.join(backupset.files_dir(ref.path), ROOT)
        find_argv = ("find", copy, "-printf", backupset.FIND_FORMAT)
        for response, detail in (
            (OSError("unavailable"), f"cannot list {copy}: unavailable"),
            (completed(find_argv, returncode=1, stderr="denied"), f"cannot list {copy}: denied"),
            (
                completed(find_argv, stdout="malformed\0"),
                f"listing of {copy} is malformed: find listing record 1 has the wrong field count",
            ),
        ):
            with self.subTest(detail=detail):
                host = FakeHost((response,))
                (root,) = backuproots.held(cast(Host, host), (row,), ref)
                problem = root.verify()
                self.assertIsInstance(problem, Problem)
                assert isinstance(problem, Problem)
                self.assertEqual(problem.problem, detail)
                self.assertIn("backup push --verify-all", problem.fix)
                self.assertEqual(host.calls, [("run", find_argv, {})])
        host = FakeHost((completed(find_argv, stdout=listing(file)),))
        (root,) = backuproots.held(cast(Host, host), (row,), ref)
        self.assertEqual(root.verify(), Problem(f"{ROOT} contains a file without an inventory hash", ""))
        self.assertEqual(host.calls, [("run", find_argv, {})])

    def test_snapshot_verify_prefers_failed_lines_before_exit_status(self) -> None:
        file = entry("file", sha256="a" * 64)
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.SNAPSHOT, True)
        ref = replace(previous(), manifest=manifest_with({ROOT: (file,)}))
        copy = os.path.join(backupset.files_dir(ref.path), ROOT)
        find_argv = ("find", copy, "-printf", backupset.FIND_FORMAT)
        hash_argv = ("sha256sum", "-c", "-")
        for hash_result, expected in (
            (completed(hash_argv, returncode=1, stdout="file: FAILED open or read\n", stderr="denied"),
             f"checksum verification failed for 1 path(s) in {ROOT}"),
            (completed(hash_argv, returncode=1, stderr="denied"),
             f"checksum verification failed for {ROOT}: denied"),
            (OSError("unavailable"), f"checksum verification failed for {ROOT}: unavailable"),
        ):
            with self.subTest(expected=expected):
                host = FakeHost((completed(find_argv, stdout=listing(file)), hash_result))
                (root,) = backuproots.held(cast(Host, host), (row,), ref)
                self.assertEqual(root.verify(), Problem(expected, ""))
                self.assertEqual(
                    host.calls,
                    [
                        ("run", find_argv, {}),
                        ("run", hash_argv, {"input": f"{'a' * 64}  file\n", "cwd": copy}),
                    ],
                )

    def test_repository_place_and_proof_use_the_same_relative_path(self) -> None:
        source = os.path.join(backupset.STAGING, "example-repository")
        row = backupset.InventoryRoot(ROOT, source, (), backupset.RootKind.REPOSITORY, False)
        file = entry("archive/file", sha256="a" * 64)
        mutable = entry("archive.info", sha256="b" * 64)
        manifest = manifest_with({ROOT: (file, mutable)})
        live = backupset.SetRef("pre-example", backupset.set_dir("pre-example"), NOW, True, manifest)
        side = f"{backupset.STAGING}.fetch-example"
        fetched = replace(live, path=os.path.join(side, os.path.relpath(live.path, backupset.STAGING)))
        hash_argv = ("sha256sum", "-c", "-")
        for ref, copy, repository in (
            (live, source, None),
            (fetched, os.path.join(side, os.path.relpath(source, backupset.STAGING)),
             os.path.join(side, os.path.relpath(source, backupset.STAGING))),
        ):
            with self.subTest(path=ref.path):
                verify_argv = tuple(pgbackrest.verify_argv("/example/rendered", repository=repository))
                host = FakeHost((completed(hash_argv, stdout="archive/file: OK\n"), completed(verify_argv)))
                (root,) = backuproots.held(cast(Host, host), (row,), ref)
                self.assertEqual((root.source, root.copy), (source, copy))
                self.assertIsNone(root.verify())
                self.assertIsNone(root.prove("/example/rendered"))
                self.assertEqual(root.put_back(backupset.AccountIds(2000, 2000)), backuproots.PutBack((), (), backuproots.Claims()))
                self.assertEqual(
                    host.calls,
                    [
                        ("run", hash_argv, {"input": f"{'a' * 64}  archive/file\n", "cwd": copy}),
                        ("run", verify_argv, {"timeout": 3600.0}),
                    ],
                )

    def test_repository_proof_refuses_with_stage_fix_left_empty(self) -> None:
        source = os.path.join(backupset.STAGING, "example-repository")
        row = backupset.InventoryRoot(ROOT, source, (), backupset.RootKind.REPOSITORY, False)
        ref = replace(previous(), path=backupset.set_dir("pre-example"), manifest=manifest_with({ROOT: ()}))
        verify_argv = tuple(pgbackrest.verify_argv("/example/rendered"))
        for response, expected in (
            (completed(verify_argv, returncode=1, stderr="denied"), "pgBackRest verify failed: denied"),
            (OSError("unavailable"), "pgBackRest verify failed: unavailable"),
        ):
            with self.subTest(expected=expected):
                host = FakeHost((response,))
                (root,) = backuproots.held(cast(Host, host), (row,), ref)
                self.assertIsNone(root.verify())
                self.assertEqual(root.prove("/example/rendered"), Problem(expected, ""))
                self.assertEqual(host.calls, [("run", verify_argv, {"timeout": 3600.0})])

    def test_repository_verify_refuses_missing_hash_without_a_walk(self) -> None:
        source = os.path.join(backupset.STAGING, "example-repository")
        row = backupset.InventoryRoot(ROOT, source, (), backupset.RootKind.REPOSITORY, False)
        ref = replace(
            previous(),
            path=backupset.set_dir("pre-example"),
            manifest=manifest_with({ROOT: (entry("archive/file"),)}),
        )
        host = FakeHost()
        (root,) = backuproots.held(cast(Host, host), (row,), ref)

        self.assertEqual(root.verify(), Problem(f"{ROOT} contains a file without an inventory hash", ""))
        self.assertEqual(host.calls, [])

    def test_snapshot_put_back_preserves_live_directory_then_claims_entries(self) -> None:
        file = entry("file")
        manifest = replace(manifest_with({ROOT: (file,)}), gideon_ids=backupset.AccountIds(1000, 1000))
        ref = replace(previous(), manifest=manifest)
        row = backupset.InventoryRoot(ROOT, SOURCE, ("cache/",), backupset.RootKind.SNAPSHOT, True)
        copy = os.path.join(backupset.files_dir(ref.path), ROOT)
        rsync_argv = ("rsync", "-a", "--delete", "--exclude=cache/", copy + "/", SOURCE + "/")
        find_argv = ("find", SOURCE, "-printf", backupset.FIND_FORMAT)
        details = os.stat_result((stat.S_IFDIR | 0o750, 0, 0, 0, 3000, 3001, 0, 0, 0, 0))
        host = FakeHost(
            (completed(rsync_argv), completed(find_argv, stdout=listing(file))),
            {SOURCE: details},
        )
        (root,) = backuproots.held(cast(Host, host), (row,), ref)

        result = root.put_back(backupset.AccountIds(2000, 2001))

        self.assertIsInstance(result, backuproots.PutBack)
        assert isinstance(result, backuproots.PutBack)
        self.assertEqual((result.restored, result.clauses), ((ROOT,), ()))
        self.assertEqual(result.claims.owners, {(3000, 3001, False): [SOURCE], (2000, 2001, False): [os.path.join(SOURCE, "file")]})
        self.assertEqual(result.claims.modes, {0o750: [SOURCE], 0o644: [os.path.join(SOURCE, "file")]})
        self.assertEqual((result.claims.entries_claimed, result.claims.gideon_owned, result.claims.owner_paths), (1, 1, 2))
        self.assertEqual(
            host.calls,
            [
                ("stat", SOURCE, {}),
                ("run", rsync_argv, {"input": None, "timeout": None}),
                ("run", find_argv, {}),
            ],
        )

    def test_snapshot_put_back_overlays_applying_later_sets_in_order(self) -> None:
        row = backupset.InventoryRoot(ROOT, SOURCE, ("cache/",), backupset.RootKind.SNAPSHOT, True, True)
        selected_file = replace(entry("same"), uid=1001, gid=1001)
        first_file = replace(entry("same"), uid=3001, gid=3001)
        middle_file = replace(entry("middle"), uid=3000, gid=3000)
        newest_file = replace(entry("same"), uid=4001, gid=4001)
        root_entry = backupset.Entry(backupset.ROOT_ENTRY, "d", 4096, 4000, 4000, 0o750, 100.0, None)
        selected = replace(
            previous(), manifest=replace(
                manifest_with({ROOT: (selected_file,)}), gideon_ids=backupset.AccountIds(1000, 1000),
            ),
        )
        first = later_ref(
            "later-one", {ROOT: (first_file, middle_file)}, ids=backupset.AccountIds(3000, 3000),
        )
        skipped = later_ref("later-empty", {})
        newest = later_ref(
            "later-two", {ROOT: (root_entry, newest_file)}, ids=backupset.AccountIds(4000, 4000),
        )
        selected_copy = os.path.join(backupset.files_dir(selected.path), ROOT)
        first_copy = os.path.join(backupset.files_dir(first.path), ROOT)
        newest_copy = os.path.join(backupset.files_dir(newest.path), ROOT)
        put_back_argv = (
            "rsync", "-a", "--delete", "--exclude=cache/", selected_copy + "/", SOURCE + "/",
        )
        first_argv = ("rsync", "-a", "--stats", "--exclude=cache/", first_copy + "/", SOURCE + "/")
        newest_argv = ("rsync", "-a", "--stats", "--exclude=cache/", newest_copy + "/", SOURCE + "/")
        find_argv = ("find", SOURCE, "-printf", backupset.FIND_FORMAT)
        details = os.stat_result((stat.S_IFDIR | 0o755, 0, 0, 0, 5000, 5001, 0, 0, 0, 0))
        host = FakeHost((
            completed(put_back_argv),
            completed(find_argv, stdout=listing(selected_file)),
            completed(first_argv, stdout="Number of regular files transferred: 1\n"),
            completed(newest_argv, stdout="Number of regular files transferred: 2\n"),
        ), {SOURCE: details})
        (root,) = backuproots.held(cast(Host, host), (row,), selected, (first, skipped, newest))

        outcome = root.put_back(backupset.AccountIds(2000, 2000))

        self.assertIsInstance(outcome, backuproots.PutBack)
        assert isinstance(outcome, backuproots.PutBack)
        self.assertEqual(outcome.restored, (ROOT,))
        self.assertEqual(outcome.clauses, (f"{ROOT}: added or rewrote 3 file(s) (with later-one, later-two)",))
        self.assertEqual(outcome.added, {ROOT: 3})
        self.assertEqual(outcome.claims.owners, {
            (4001, 4001, False): [os.path.join(SOURCE, "same")],
            (2000, 2000, False): [os.path.join(SOURCE, "middle"), os.path.join(SOURCE, ".")],
        })
        self.assertEqual(outcome.claims.modes, {
            0o644: [os.path.join(SOURCE, "same"), os.path.join(SOURCE, "middle")],
            0o750: [os.path.join(SOURCE, ".")],
        })
        self.assertEqual((outcome.claims.entries_claimed, outcome.claims.gideon_owned), (3, 2))
        self.assertEqual(host.calls, [
            ("stat", SOURCE, {}),
            ("run", put_back_argv, {"input": None, "timeout": None}),
            ("run", find_argv, {}),
            ("run", first_argv, {"cwd": None}),
            ("run", newest_argv, {"cwd": None}),
        ])
        self.assertEqual(host.responses, [])

    def test_snapshot_overlay_reports_zero_and_non_user_written_skips_it(self) -> None:
        later = later_ref("later-one", {ROOT: ()})
        selected = replace(previous(), manifest=manifest_with({ROOT: ()}))
        copy = os.path.join(backupset.files_dir(selected.path), ROOT)
        later_copy = os.path.join(backupset.files_dir(later.path), ROOT)
        put_back_argv = ("rsync", "-a", "--delete", copy + "/", SOURCE + "/")
        overlay_argv = ("rsync", "-a", "--stats", later_copy + "/", SOURCE + "/")
        find_argv = ("find", SOURCE, "-printf", backupset.FIND_FORMAT)
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.SNAPSHOT, True, True)
        host = FakeHost((
            completed(put_back_argv), completed(find_argv, stdout=listing()),
            completed(overlay_argv, stdout="Number of regular files transferred: 0\n"),
        ))
        (root,) = backuproots.held(cast(Host, host), (row,), selected, (later,))
        outcome = root.put_back(backupset.AccountIds(2000, 2000))
        self.assertIsInstance(outcome, backuproots.PutBack)
        assert isinstance(outcome, backuproots.PutBack)
        self.assertEqual(outcome.clauses, (f"{ROOT}: added or rewrote 0 file(s) (with later-one)",))
        self.assertEqual(outcome.added, {ROOT: 0})
        self.assertEqual([call[1] for call in host.calls if call[0] == "run"], [put_back_argv, find_argv, overlay_argv])

        copy_find_argv = ("find", copy, "-printf", backupset.FIND_FORMAT)
        idle = FakeHost((
            completed(copy_find_argv, stdout=listing()),
            completed(put_back_argv),
            completed(find_argv, stdout=listing()),
        ))
        (root,) = backuproots.held(cast(Host, idle), (replace(row, user_written=False),), selected, (later,))
        self.assertIsNone(root.verify())
        outcome = root.put_back(backupset.AccountIds(2000, 2000))
        self.assertIsInstance(outcome, backuproots.PutBack)
        assert isinstance(outcome, backuproots.PutBack)
        self.assertEqual((outcome.clauses, outcome.added), ((), {}))
        self.assertEqual(
            [call[1] for call in idle.calls if call[0] == "run"],
            [copy_find_argv, put_back_argv, find_argv],
        )

    def test_snapshot_put_back_refuses_rsync_and_walk(self) -> None:
        file = entry("file")
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.SNAPSHOT, True)
        ref = replace(previous(), manifest=manifest_with({ROOT: (file,)}))
        copy = os.path.join(backupset.files_dir(ref.path), ROOT)
        rsync_argv = ("rsync", "-a", "--delete", copy + "/", SOURCE + "/")
        find_argv = ("find", SOURCE, "-printf", backupset.FIND_FORMAT)
        for response, expected in (
            (completed(rsync_argv, returncode=1, stderr="denied"), f"restored {ROOT}: denied"),
            (OSError("unavailable"), f"restored {ROOT}: unavailable"),
        ):
            with self.subTest(expected=expected):
                host = FakeHost((response,))
                (root,) = backuproots.held(cast(Host, host), (row,), ref)
                self.assertEqual(root.put_back(backupset.AccountIds(2000, 2000)), Problem(expected, ""))
                self.assertEqual(host.calls, [("stat", SOURCE, {}), ("run", rsync_argv, {"input": None, "timeout": None})])
        host = FakeHost((completed(rsync_argv), completed(find_argv, stdout=listing())))
        (root,) = backuproots.held(cast(Host, host), (row,), ref)
        problem = root.put_back(backupset.AccountIds(2000, 2000))
        self.assertIsInstance(problem, Problem)
        assert isinstance(problem, Problem)
        self.assertEqual(
            problem.problem,
            f"1 inventoried path(s) under {SOURCE} are not what the manifest declares "
            "(a link in place of a file or directory, or a path beneath a link)",
        )
        self.assertIn("backup push --verify-all", problem.fix)
        self.assertEqual(
            host.calls,
            [("stat", SOURCE, {}), ("run", rsync_argv, {"input": None, "timeout": None}), ("run", find_argv, {})],
        )

    def test_snapshot_put_back_uses_recorded_root_without_stat(self) -> None:
        recorded_root = backupset.Entry(
            backupset.ROOT_ENTRY,
            "d",
            4096,
            1000,
            1000,
            0o755,
            100.0,
            None,
        )
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.SNAPSHOT, True)
        ref = replace(previous(), manifest=manifest_with({ROOT: (recorded_root,)}))
        copy = os.path.join(backupset.files_dir(ref.path), ROOT)
        rsync_argv = ("rsync", "-a", "--delete", copy + "/", SOURCE + "/")
        find_argv = ("find", SOURCE, "-printf", backupset.FIND_FORMAT)
        host = FakeHost((completed(rsync_argv), completed(find_argv, stdout=listing())))
        (root,) = backuproots.held(cast(Host, host), (row,), ref)

        result = root.put_back(backupset.AccountIds(2000, 2000))

        self.assertIsInstance(result, backuproots.PutBack)
        assert isinstance(result, backuproots.PutBack)
        self.assertEqual(result.claims.owners, {(1000, 1000, False): [os.path.join(SOURCE, backupset.ROOT_ENTRY)]})
        self.assertEqual(
            host.calls,
            [("run", rsync_argv, {"input": None, "timeout": None}), ("run", find_argv, {})],
        )

    def test_held_skips_unknown_names_and_missing_snapshot_walks_empty(self) -> None:
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.SNAPSHOT, True)
        manifest = manifest_with({"unknown": (entry("file"),)})
        ref = replace(previous(), manifest=manifest)
        copy = os.path.join(backupset.files_dir(ref.path), ROOT)
        find_argv = ("find", copy, "-printf", backupset.FIND_FORMAT)
        host = FakeHost((completed(find_argv, stdout=listing()),))
        roots = backuproots.held(cast(Host, host), (row,), ref)

        self.assertEqual(tuple(root.name for root in roots), (ROOT,))
        self.assertEqual(roots[0].entries, ())
        self.assertIsNone(roots[0].verify())
        self.assertEqual(host.calls, [("run", find_argv, {})])

    def test_held_missing_repository_hashes_nothing_but_still_proves(self) -> None:
        source = os.path.join(backupset.STAGING, "example-repository")
        row = backupset.InventoryRoot(ROOT, source, (), backupset.RootKind.REPOSITORY, False)
        ref = replace(previous(), path=backupset.set_dir("pre-example"), manifest=manifest_with({"unknown": (entry("file"),)}))
        verify_argv = tuple(pgbackrest.verify_argv("/example/rendered"))
        host = FakeHost((completed(verify_argv),))
        (root,) = backuproots.held(cast(Host, host), (row,), ref)

        self.assertEqual(root.entries, ())
        self.assertIsNone(root.verify())
        self.assertIsNone(root.prove("/example/rendered"))
        self.assertEqual(host.calls, [("run", verify_argv, {"timeout": 3600.0})])

    def test_kept_copy_clause_and_carrying_lookup(self) -> None:
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.SNAPSHOT, False)
        ref = replace(previous(), manifest=manifest_with({ROOT: ()}))
        host = FakeHost()
        roots = backuproots.held(cast(Host, host), (row,), ref)
        copy = os.path.join(backupset.files_dir(ref.path), ROOT)

        self.assertIs(backuproots.carrying(roots, SOURCE), roots[0])
        self.assertEqual(
            roots[0].put_back(backupset.AccountIds(2000, 2000)),
            backuproots.PutBack(
                (),
                (f"the {ROOT} copy stays in {copy} (clone the tag the manifest names)",),
                backuproots.Claims(),
            ),
        )
        with self.assertRaisesRegex(LookupError, "/absent"):
            backuproots.carrying(roots, "/absent")
        self.assertEqual(host.calls, [])

    def test_held_uses_registry_order_and_requires_a_manifest(self) -> None:
        source = os.path.join(backupset.STAGING, "example-repository")
        rows = (
            backupset.InventoryRoot(ROOT, source, (), backupset.RootKind.REPOSITORY, False),
            backupset.InventoryRoot("other", SOURCE, (), backupset.RootKind.SNAPSHOT, False),
        )
        ref = replace(previous(), manifest=manifest_with({"other": (), "unknown": (entry("file"),)}))
        host = FakeHost()

        self.assertEqual(tuple(root.name for root in backuproots.held(cast(Host, host), rows, ref)), (ROOT, "other"))
        with self.assertRaisesRegex(ValueError, "held set has no manifest"):
            backuproots.held(cast(Host, host), rows, replace(ref, manifest=None))
        self.assertEqual(host.calls, [])


class StoreRestore(unittest.TestCase):
    def _bound(self, host: FakeHost, pin: backupset.ListingPin | None) -> backuproots.HeldRoot:
        manifest = replace(manifest_with({}), listings={ROOT: pin} if pin is not None else {})
        ref = replace(previous(), manifest=manifest)
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.STORE, False)
        (root,) = backuproots.held(cast(Host, host), (row,), ref)
        return root

    def test_landed_store_is_bound_and_does_nothing(self) -> None:
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.STORE, False)
        host = FakeHost()
        (root,) = backuproots.landed(cast(Host, host), (row,), "/example/side")
        self.assertEqual(root.name, ROOT)
        self.assertIsNone(root.arrive("/example/rendered"))
        self.assertEqual(host.calls, [])

    def test_fetched_store_reowns_copy_once_and_claims_listing(self) -> None:
        pin = backupset.ListingPin(f"{ROOT}.listing", 4, "a" * 64, 1, 9)
        manifest = replace(manifest_with({}), listings={ROOT: pin})
        ref = replace(previous(), manifest=manifest)
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.STORE, False)
        copy = os.path.join(backupset.files_dir(ref.path), ROOT)
        argv = ("chown", "-R", "-h", "0:0", copy)
        host = FakeHost((completed(argv),))
        (root,) = backuproots.fetched(cast(Host, host), (row,), "/example/side", (ref,))

        claims = root.reown(backupset.AccountIds(2000, 2000))

        self.assertIsInstance(claims, backuproots.Claims)
        assert isinstance(claims, backuproots.Claims)
        listing_path = os.path.join(ref.path, pin.file)
        self.assertEqual(claims.owners, {(0, 0, False): [listing_path]})
        self.assertEqual(claims.modes, {0o644: [listing_path]})
        self.assertEqual(host.calls, [("run", argv, {"cwd": None})])
        for response in (completed(argv, returncode=1, stderr="denied"), OSError("unavailable")):
            with self.subTest(response=response):
                failed = FakeHost((response,))
                (bound_root,) = backuproots.fetched(cast(Host, failed), (row,), "/example/side", (ref,))
                problem = bound_root.reown(backupset.AccountIds(2000, 2000))
                self.assertIsInstance(problem, Problem)
                assert isinstance(problem, Problem)
                self.assertEqual(problem.fix, "")
                self.assertEqual(failed.calls, [("run", argv, {"cwd": None})])

    def test_fetched_ignores_store_without_pin_or_registry_row(self) -> None:
        pin = backupset.ListingPin(f"{ROOT}.listing", 0, "a" * 64, 0, 0)
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.STORE, False)
        unpinned = replace(previous(), manifest=manifest_with({}))
        unknown = replace(previous(), manifest=replace(manifest_with({}), listings={"unknown": pin}))
        host = FakeHost()
        self.assertEqual(backuproots.fetched(cast(Host, host), (row,), "/side", (unpinned, unknown)), ())
        self.assertEqual(host.calls, [])

    def test_held_store_verifies_listing_pin_and_refuses_mismatch(self) -> None:
        content = f"{'a' * 64}\t5\n"
        pin = backupset.ListingPin(f"{ROOT}.listing", len(content), hashlib.sha256(content.encode()).hexdigest(), 1, 5)
        listing_path = os.path.join(PREVIOUS, pin.file)
        argv = ("sha256sum", listing_path)
        stats = {listing_path: cast(os.stat_result, mock.Mock(st_size=pin.size))}
        host = FakeHost((completed(argv, stdout=f"{pin.sha256}  {listing_path}\n"),), stats=stats)
        root = self._bound(host, pin)
        self.assertIsNone(root.verify())
        self.assertIsNone(root.prove("/rendered"))
        self.assertEqual(host.calls, [("stat", listing_path, {}), ("run", argv, {"cwd": None})])
        for size, digest, response in (
            (pin.size + 1, pin.sha256, None),
            (pin.size, "b" * 64, None),
            (pin.size, pin.sha256, completed(argv, returncode=1, stderr="denied")),
        ):
            with self.subTest(size=size, digest=digest, response=response):
                failed = FakeHost(
                    (response or completed(argv, stdout=f"{digest}  {listing_path}\n"),),
                    stats={listing_path: cast(os.stat_result, mock.Mock(st_size=size))},
                )
                problem = self._bound(failed, pin).verify()
                self.assertIsInstance(problem, Problem)
                assert isinstance(problem, Problem)
                self.assertIn("backup push --verify-all", problem.fix)
        absent = FakeHost()
        problem = self._bound(absent, pin).verify()
        self.assertIsInstance(problem, Problem)
        assert isinstance(problem, Problem)
        self.assertIn("backup push --verify-all", problem.fix)

    def test_held_store_without_pin_runs_nothing_and_keeps_live_store(self) -> None:
        host = FakeHost()
        root = self._bound(host, None)
        self.assertIsNone(root.verify())
        self.assertIsNone(root.prove("/rendered"))
        self.assertEqual(
            root.put_back(backupset.AccountIds(2000, 2000)),
            backuproots.PutBack((), (f"{ROOT}: the set predates this root; the live store is left as it is",), backuproots.Claims()),
        )
        self.assertEqual(host.calls, [])

    def test_held_store_absent_live_root_has_provision_fix(self) -> None:
        pin = backupset.ListingPin(f"{ROOT}.listing", 0, "a" * 64, 0, 0)
        host = FakeHost()
        problem = self._bound(host, pin).put_back(backupset.AccountIds(2000, 2000))
        self.assertIsInstance(problem, Problem)
        assert isinstance(problem, Problem)
        self.assertIn(SOURCE, problem.problem)
        self.assertIn("host provision --only disk-layout", problem.fix)
        self.assertEqual(host.calls, [("exists", SOURCE, {})])

    def test_held_store_put_back_runs_listing_join_check_and_transfer_in_order(self) -> None:
        name = "a" * 64
        damaged = "b" * 64
        pin = backupset.ListingPin(f"{ROOT}.listing", 0, "c" * 64, 2, 12)
        set_listing = os.path.join(PREVIOUS, pin.file)
        live_listing = f"{backupset.STAGING}.live-{ROOT}.listing"
        copy = os.path.join(backupset.files_dir(PREVIOUS), ROOT)
        find_argv = (
            "find", ".", "-regextype", "posix-basic", "-type", "f", "-regex",
            r"\./\([0-9a-f]\{2\}\)/\([0-9a-f]\{2\}\)/\1\2[0-9a-f]\{60\}",
            "-fprintf", live_listing, r"%f\t%s\n",
        )
        sort_argv = ("env", "LC_ALL=C", "sort", "-o", live_listing, live_listing)
        join_argv = ("env", "LC_ALL=C", "join", "-t", "\t", "-j", "1", "-v", "1", set_listing, live_listing)
        hash_argv = ("sha256sum", "-c", "-")
        transfer_argv = (
            "rsync", "-rtp", "--stats", f"--chmod=D{cas.DIRECTORY_MODE:o},F{cas.OBJECT_MODE:o}",
            "--ignore-existing", "--files-from=-", copy + "/", SOURCE + "/",
        )
        host = FakeHost(
            (
                completed(find_argv), completed(sort_argv),
                completed(join_argv, stdout=f"{name}\t5\n{damaged}\t7\n"),
                completed(hash_argv, returncode=1, stdout=f"aa/aa/{name}: OK\nbb/bb/{damaged}: FAILED\n"),
                completed(transfer_argv, stdout="Number of regular files transferred: 1\n"),
            ),
            existing=(SOURCE,),
        )
        outcome = self._bound(host, pin).put_back(backupset.AccountIds(2000, 2000))

        self.assertIsInstance(outcome, backuproots.PutBack)
        assert isinstance(outcome, backuproots.PutBack)
        self.assertEqual(outcome.counts, {ROOT: backuproots.StoreCounts(1, 1)})
        self.assertEqual(outcome.clauses, (f"{ROOT}: added 1 object(s), 1 left out",))
        self.assertIn(damaged, outcome.closing_lines[0])
        self.assertEqual(
            host.calls,
            [
                ("exists", SOURCE, {}),
                ("unlink", live_listing, {"missing_ok": True}),
                ("run", find_argv, {"cwd": SOURCE}),
                ("run", sort_argv, {"cwd": None}),
                ("run", join_argv, {"cwd": None}),
                ("unlink", live_listing, {"missing_ok": True}),
                ("run", hash_argv, {"input": f"{name}  aa/aa/{name}\n{damaged}  bb/bb/{damaged}\n", "cwd": copy}),
                ("run", transfer_argv, {"cwd": None, "input": f"aa/aa/{name}\n"}),
            ],
        )
        self.assertEqual(host.responses, [])

    def test_later_store_copy_supplies_a_damaged_and_a_new_object(self) -> None:
        first = "a" * 64
        repaired = "b" * 64
        later_only = "c" * 64
        pin = backupset.ListingPin(f"{ROOT}.listing", 0, "d" * 64, 2, 12)
        later_path = "/example/staging/sets/later"
        later_pin = backupset.ListingPin(f"{ROOT}.listing", 0, "e" * 64, 3, 18)
        later_manifest = replace(manifest_with({}), listings={ROOT: later_pin})
        later = backupset.SetRef("later", later_path, NOW, True, later_manifest)
        selected_manifest = replace(manifest_with({}), listings={ROOT: pin})
        selected = replace(previous(), manifest=selected_manifest)
        row = backupset.InventoryRoot(ROOT, SOURCE, (), backupset.RootKind.STORE, False)
        live_listing = f"{backupset.STAGING}.live-{ROOT}.listing"
        find_argv = tuple(backuproots._store_listing_argv(live_listing))
        sort_argv = ("env", "LC_ALL=C", "sort", "-o", live_listing, live_listing)
        hash_argv = ("sha256sum", "-c", "-")
        first_copy = os.path.join(backupset.files_dir(PREVIOUS), ROOT)
        later_copy = os.path.join(backupset.files_dir(later_path), ROOT)

        def join_argv(set_path: str) -> tuple[str, ...]:
            return ("env", "LC_ALL=C", "join", "-t", "\t", "-j", "1", "-v", "1",
                    os.path.join(set_path, pin.file), live_listing)

        def transfer_argv(copy: str) -> tuple[str, ...]:
            return ("rsync", "-rtp", "--stats", f"--chmod=D{cas.DIRECTORY_MODE:o},F{cas.OBJECT_MODE:o}",
                    "--ignore-existing", "--files-from=-", copy + "/", SOURCE + "/")

        host = FakeHost((
            completed(find_argv), completed(sort_argv),
            completed(join_argv(PREVIOUS), stdout=f"{first}\t5\n{repaired}\t7\n"),
            completed(hash_argv, returncode=1, stdout=f"aa/aa/{first}: OK\nbb/bb/{repaired}: FAILED\n"),
            completed(transfer_argv(first_copy), stdout="Number of regular files transferred: 1\n"),
            completed(find_argv), completed(sort_argv),
            completed(join_argv(later_path), stdout=f"{repaired}\t7\n{later_only}\t6\n"),
            completed(hash_argv, stdout=f"bb/bb/{repaired}: OK\ncc/cc/{later_only}: OK\n"),
            completed(transfer_argv(later_copy), stdout="Number of regular files transferred: 2\n"),
        ), existing=(SOURCE,))
        (root,) = backuproots.held(cast(Host, host), (row,), selected, (later,))
        outcome = root.put_back(backupset.AccountIds(2000, 2000))
        self.assertIsInstance(outcome, backuproots.PutBack)
        assert isinstance(outcome, backuproots.PutBack)
        self.assertEqual(outcome.counts, {ROOT: backuproots.StoreCounts(3, 0)})
        self.assertEqual(outcome.clauses, (f"{ROOT}: added 3 object(s), 0 left out (with later)",))
        self.assertEqual(outcome.closing_lines, ())
        self.assertEqual([call[1] for call in host.calls if call[0] == "run"], [
            find_argv, sort_argv, join_argv(PREVIOUS), hash_argv, transfer_argv(first_copy),
            find_argv, sort_argv, join_argv(later_path), hash_argv, transfer_argv(later_copy),
        ])
        self.assertEqual(host.responses, [])

    def test_held_store_put_back_refuses_failed_tools_with_empty_fix(self) -> None:
        pin = backupset.ListingPin(f"{ROOT}.listing", 0, "c" * 64, 1, 5)
        live_listing = f"{backupset.STAGING}.live-{ROOT}.listing"
        listing = os.path.join(PREVIOUS, pin.file)
        find_argv = tuple(backuproots._store_listing_argv(live_listing))
        sort_argv = ("env", "LC_ALL=C", "sort", "-o", live_listing, live_listing)
        join_argv = ("env", "LC_ALL=C", "join", "-t", "\t", "-j", "1", "-v", "1", listing, live_listing)
        for commands, phrase in (
            ((completed(find_argv, returncode=1, stderr="denied"),), "live store listing"),
            ((completed(find_argv), completed(sort_argv, returncode=1, stderr="denied")), "live store listing sort"),
            ((completed(find_argv), completed(sort_argv), completed(join_argv, returncode=1, stderr="denied")), "store listing join"),
        ):
            with self.subTest(phrase=phrase):
                host = FakeHost(commands, existing=(SOURCE,))
                problem = self._bound(host, pin).put_back(backupset.AccountIds(2000, 2000))
                self.assertIsInstance(problem, Problem)
                assert isinstance(problem, Problem)
                self.assertIn(phrase, problem.problem)
                self.assertIn("denied", problem.problem)
                self.assertEqual(problem.fix, "")
                self.assertEqual(host.calls[-1], ("unlink", live_listing, {"missing_ok": True}))

        name = "a" * 64
        hash_argv = ("sha256sum", "-c", "-")
        host = FakeHost(
            (completed(find_argv), completed(sort_argv), completed(join_argv, stdout=f"{name}\t5\n"),
             completed(hash_argv, returncode=1, stderr="unavailable")),
            existing=(SOURCE,),
        )
        problem = self._bound(host, pin).put_back(backupset.AccountIds(2000, 2000))
        self.assertIsInstance(problem, Problem)
        assert isinstance(problem, Problem)
        self.assertIn("store object check", problem.problem)
        self.assertEqual(problem.fix, "")
        # The scratch listing is gone once the join has read it, before any object is hashed.
        self.assertEqual(host.calls[-2], ("unlink", live_listing, {"missing_ok": True}))
        self.assertEqual(host.calls[-1][1], hash_argv)

    def test_scratch_listing_that_cannot_be_removed_after_the_join_does_not_refuse(self) -> None:
        pin = backupset.ListingPin(f"{ROOT}.listing", 0, "c" * 64, 0, 0)
        live_listing = f"{backupset.STAGING}.live-{ROOT}.listing"
        listing = os.path.join(PREVIOUS, pin.file)
        find_argv = tuple(backuproots._store_listing_argv(live_listing))
        sort_argv = ("env", "LC_ALL=C", "sort", "-o", live_listing, live_listing)
        join_argv = ("env", "LC_ALL=C", "join", "-t", "\t", "-j", "1", "-v", "1", listing, live_listing)
        host = FakeHost((completed(find_argv), completed(sort_argv), completed(join_argv)), existing=(SOURCE,))
        removals = iter((None, PermissionError(live_listing)))

        def unlink(path: PathLike, *, missing_ok: bool = False) -> None:
            host.calls.append(("unlink", os.fspath(path), {"missing_ok": missing_ok}))
            failure = next(removals)
            if failure is not None:
                raise failure

        with mock.patch.object(host, "unlink", unlink):
            outcome = self._bound(host, pin).put_back(backupset.AccountIds(2000, 2000))
        self.assertEqual(
            outcome,
            backuproots.PutBack(
                (), (f"{ROOT}: added 0 object(s), {backuproots.NOTHING_LEFT_OUT_DETAIL}",),
                backuproots.Claims(), {ROOT: backuproots.StoreCounts(0, 0)}, (),
            ),
        )


class CommandBoundary(unittest.TestCase):
    def test_commands_keep_root_decisions_in_bound_roots(self) -> None:
        repository = Path(__file__).resolve().parent.parent
        for name in ("backup.py", "restore.py", "drill.py"):
            module = Path("gideon/host") / name
            with self.subTest(module=str(module)):
                source = (repository / module).read_text(encoding="utf-8")
                self.assertEqual(root_boundary_findings(source, str(module)), [])

    def test_every_rule_refuses_inline_command_source(self) -> None:
        name = next(iter(sorted(ROOT_NAMES)))
        examples = (
            (f"value = {name!r}\n", f"root-name literal {name!r}"),
            (f"value = {name + '/child'!r}\n", f"root-name literal {name + '/child'!r}"),
            (f'value = f"{name}/{{child}}"\n', f"root-name literal {name + '/'!r}"),
            ("value = root.snapshotted\n", "root policy read .snapshotted"),
            ("value = root.restore_in_place\n", "root policy read .restore_in_place"),
            ("value = FILES_DIR\n", "layout reference FILES_DIR"),
            ("value = backupset.REPOSITORY_PATH\n", "layout reference .REPOSITORY_PATH"),
        )
        for source, finding in examples:
            with self.subTest(source=source):
                self.assertEqual(root_boundary_findings(source, "inline.py"), [f"inline.py:1: {finding}"])


if __name__ == "__main__":
    unittest.main()
