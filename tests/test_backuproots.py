"""Backup root behavior and command boundaries over a recording host seam."""

import ast
import os
import stat
import subprocess
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest import mock

from gideon.host import backuproots, backupset, pgbackrest, stack
from gideon.host.report import Problem
from gideon.host.sysio import Command, Host, PathLike

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


class FakeHost:
    """Return scripted command results and record calls in their actual order."""

    def __init__(
        self,
        responses: Sequence[subprocess.CompletedProcess[str] | OSError] = (),
        stats: Mapping[str, os.stat_result] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.stats = dict(stats or {})
        self.calls: list[tuple[str, object, object]] = []

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

    def test_each_kind_returns_exact_listing_and_hash_refusals(self) -> None:
        for kind in backupset.RootKind:
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
        (root,) = backuproots.pushed((row,), manifest_with({ROOT: recorded}), "pre-example")
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
        (root,) = backuproots.pushed((row,), manifest_with({ROOT: recorded}), "pre-example")
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
                (root,) = backuproots.pushed((row,), manifest_with({ROOT: recorded}), "pre-example")
                self.assertEqual(
                    root.check(verify_all=False),
                    Problem(f"the set's repository inventory lacks {missing}", ""),
                )

    def test_unknown_name_uses_snapshot_layout_and_missing_snapshot_is_empty(self) -> None:
        known = backupset.InventoryRoot("known", SOURCE, (), backupset.RootKind.SNAPSHOT, True)
        recorded = entry("file", sha256="a" * 64)
        roots = backuproots.pushed(
            (known,), manifest_with({"unknown": (recorded,)}), "pre-example"
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
        (root,) = backuproots.pushed((row,), manifest_with({}), "pre-example")

        self.assertEqual(
            root.check(verify_all=False),
            Problem(f"the set's repository inventory lacks backup/{pgbackrest.STANZA}/backup.info", ""),
        )


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
