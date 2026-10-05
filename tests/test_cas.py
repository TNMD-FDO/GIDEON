"""Contracts for the content-addressed store over fake and real host I/O."""

import errno
import hashlib
import os
import stat
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from gideon.host import cas
from gideon.host.report import Problem
from gideon.host.sysio import Command, CompletedText, PathLike, RealHost


@dataclass
class File:
    data: bytes
    mode: int
    inode: int
    uid: int
    gid: int


@dataclass
class Directory:
    mode: int
    uid: int
    gid: int


@dataclass(frozen=True)
class Call:
    operation: str
    path: Path
    mode: int | None = None


class FakeBytesHost:
    """A dict-backed byte seam with the real exclusive-create flush contract."""

    def __init__(self, root: Path, *, with_root: bool = True) -> None:
        self.files: dict[Path, File] = {}
        self.directories: dict[Path, Directory] = {}
        if with_root:
            self.directories[root] = Directory(cas.DIRECTORY_MODE, 1000, 1000)
        self.calls: list[Call] = []
        self.errors: dict[tuple[str, Path], OSError] = {}
        self.lose_create: set[Path] = set()
        self.next_inode = 1

    def _record(self, operation: str, path: PathLike, mode: int | None = None) -> Path:
        target = Path(path)
        self.calls.append(Call(operation, target, mode))
        if error := self.errors.get((operation, target)):
            raise error
        return target

    def _new_file(self, path: Path, data: bytes, mode: int) -> None:
        parent = self.directories[path.parent]
        self.files[path] = File(data, mode, self.next_inode, 10001, parent.gid)
        self.next_inode += 1

    def read_bytes(self, path: PathLike) -> bytes:
        target = self._record("read_bytes", path)
        try:
            return self.files[target].data
        except KeyError:
            raise FileNotFoundError(target) from None

    def create_exclusive(self, path: PathLike, data: bytes, *, mode: int) -> bool:
        target = self._record("create_exclusive", path, mode)
        if target not in self.files:
            self._new_file(target, data, mode)
            created = target not in self.lose_create
        else:
            created = False
        self.sync_directory(target.parent)
        return created

    def sync_directory(self, path: PathLike) -> None:
        target = self._record("sync_directory", path)
        if target not in self.directories:
            raise FileNotFoundError(target)

    def stat(self, path: PathLike) -> os.stat_result:
        target = self._record("stat", path)
        if directory := self.directories.get(target):
            return os.stat_result(
                (
                    stat.S_IFDIR | directory.mode,
                    0,
                    0,
                    1,
                    directory.uid,
                    directory.gid,
                    0,
                    0,
                    0,
                    0,
                )
            )
        if file := self.files.get(target):
            return os.stat_result(
                (
                    stat.S_IFREG | file.mode,
                    file.inode,
                    0,
                    1,
                    file.uid,
                    file.gid,
                    len(file.data),
                    0,
                    0,
                    0,
                )
            )
        raise FileNotFoundError(target)

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        target = self._record("mkdir", path, mode)
        if target in self.directories:
            if not exist_ok:
                raise FileExistsError(target)
            return
        if target in self.files:
            raise FileExistsError(target)
        if parents:
            raise AssertionError("unexpected recursive mkdir")
        parent = self.directories.get(target.parent)
        if parent is None:
            raise FileNotFoundError(target.parent)
        inherited_setgid = stat.S_ISGID if parent.mode & stat.S_ISGID else 0
        self.directories[target] = Directory(
            (mode & ~0o022) | inherited_setgid,
            10001,
            parent.gid if inherited_setgid else 10001,
        )

    def chmod(self, path: PathLike, mode: int) -> None:
        target = self._record("chmod", path, mode)
        if target in self.files:
            raise AssertionError("an object was chmodded")
        try:
            self.directories[target].mode = mode
        except KeyError:
            raise FileNotFoundError(target) from None

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
    ) -> CompletedText:
        del argv, check, input, cwd, env, timeout, passthrough
        raise AssertionError("unexpected run")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        self._record("read_text", path)
        del encoding
        raise AssertionError("unexpected read_text")

    def write_text(
        self,
        path: PathLike,
        text: str,
        *,
        encoding: str = "utf-8",
        mode: int = 0o644,
    ) -> None:
        self._record("write_text", path, mode)
        del text, encoding
        raise AssertionError("unexpected write_text")

    def exists(self, path: PathLike) -> bool:
        target = self._record("exists", path)
        return target in self.files or target in self.directories

    def listdir(self, path: PathLike) -> list[str]:
        self._record("listdir", path)
        raise AssertionError("unexpected listdir")

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        self._record("unlink", path)
        del missing_ok
        raise AssertionError("unexpected unlink")

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        self._record("chown", path)
        del uid, gid
        raise AssertionError("unexpected chown")

    def geteuid(self) -> int:
        raise AssertionError("unexpected geteuid")


class ObjectPath(unittest.TestCase):
    """The object-name grammar and the path it builds."""

    def test_layout_and_invalid_name(self) -> None:
        root = Path("/fake/cas")
        name = hashlib.sha256(b"path example").hexdigest()
        self.assertEqual(
            cas.object_path(name, root), root / name[:2] / name[2:4] / name
        )
        invalid = "../../a-private-value"
        with self.assertRaises(ValueError) as caught:
            cas.object_path(invalid, root)
        self.assertIn(str(len(invalid)), str(caught.exception))
        self.assertNotIn(invalid, str(caught.exception))


class FakeStore(unittest.TestCase):
    """Write-once and refusal behavior over the byte seam."""

    def setUp(self) -> None:
        self.root = Path("/fake/cas")
        self.host = FakeBytesHost(self.root)

    def put_name(self, data: bytes) -> str:
        result = cas.put(self.host, data, root=self.root)
        self.assertIsInstance(result, str)
        assert isinstance(result, str)
        return result

    def assert_no_object_mutation(self, path: Path) -> None:
        self.assertFalse(
            any(
                call.operation in {"unlink", "write_text"}
                for call in self.host.calls
            )
        )
        self.assertFalse(
            any(
                call.operation == "chmod" and call.path == path
                for call in self.host.calls
            )
        )

    def test_second_put_stores_one_object_without_writing_again(self) -> None:
        data = b"same bytes"
        name = self.put_name(data)
        path = cas.object_path(name, self.root)
        before = self.host.files[path]
        self.host.calls.clear()

        self.assertEqual(cas.put(self.host, data, root=self.root), name)
        self.assertIs(self.host.files[path], before)
        self.assertEqual(len(self.host.files), 1)
        self.assertFalse(
            {"create_exclusive", "mkdir", "chmod"}
            & {call.operation for call in self.host.calls}
        )
        self.assertEqual(
            [call.path for call in self.host.calls if call.operation == "stat"],
            [self.root, path],
        )
        self.assertEqual(
            [
                call.path
                for call in self.host.calls
                if call.operation == "sync_directory"
            ],
            [path.parent, path.parent.parent, self.root],
        )
        self.assert_no_object_mutation(path)

    def test_existing_corrupt_objects_are_never_modified(self) -> None:
        data = b"intended bytes"
        name = hashlib.sha256(data).hexdigest()
        path = cas.object_path(name, self.root)
        self.host.directories[path.parent.parent] = Directory(
            cas.DIRECTORY_MODE, 10001, 1000
        )
        self.host.directories[path.parent] = Directory(cas.DIRECTORY_MODE, 10001, 1000)
        for wrong in (b"X" * len(data), b"different length"):
            with self.subTest(wrong=wrong):
                self.host.files[path] = File(wrong, cas.OBJECT_MODE, 10, 10001, 1000)
                before = self.host.files[path]
                self.host.calls.clear()
                result = cas.put(self.host, data, root=self.root)
                if len(wrong) != len(data):
                    self.assertIsInstance(result, Problem)
                    assert isinstance(result, Problem)
                    self.assertIn("differs in size", result.problem)
                else:
                    self.assertEqual(result, name)
                    self.assertEqual(len(self.host.files[path].data), len(data))
                self.assertIs(self.host.files[path], before)
                self.assertFalse(
                    any(
                        call.operation == "create_exclusive" for call in self.host.calls
                    )
                )
                self.assert_no_object_mutation(path)

    def test_get_refuses_hash_mismatch_absence_and_invalid_name(self) -> None:
        data = b"named bytes"
        name = self.put_name(data)
        path = cas.object_path(name, self.root)
        self.host.files[path].data = b"wrong bytes"
        mismatch = cas.get(self.host, name, root=self.root)
        self.assertIsInstance(mismatch, Problem)
        assert isinstance(mismatch, Problem)
        self.assertIn("does not hash", mismatch.problem)
        self.assertIn(str(path), mismatch.problem)
        self.assertIn("source", mismatch.fix)
        self.assertIn("Move the object's path aside", mismatch.fix)
        self.assertIn("restore --from staging --set <label>", mismatch.fix)
        self.assertIn("whole system to a backup set that holds it", mismatch.fix)

        absent_name = hashlib.sha256(b"not stored").hexdigest()
        absent = cas.get(self.host, absent_name, root=self.root)
        self.assertIsInstance(absent, Problem)
        assert isinstance(absent, Problem)
        self.assertIn("no object", absent.problem)
        self.assertIn("source", absent.fix)
        self.assertIn("restore --from staging --set <label>", absent.fix)
        self.assertIn("whole system to a backup set that holds it", absent.fix)

        invalid = "../private matter text"
        self.host.calls.clear()
        refused = cas.get(self.host, invalid, root=self.root)
        self.assertIsInstance(refused, Problem)
        assert isinstance(refused, Problem)
        self.assertNotIn(invalid, refused.problem)
        self.assertNotIn(invalid, refused.fix)
        self.assertIn(str(len(invalid)), refused.problem)
        self.assertEqual(self.host.calls, [])

    def test_missing_root_names_provision_and_does_not_create_it(self) -> None:
        host = FakeBytesHost(self.root, with_root=False)
        result = cas.put(host, b"bytes", root=self.root)
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("host provision --only disk-layout", result.fix)
        self.assertEqual(host.directories, {})
        self.assertEqual([call.operation for call in host.calls], ["stat"])
        name = hashlib.sha256(b"bytes").hexdigest()
        read = cas.get(host, name, root=self.root)
        self.assertIsInstance(read, Problem)
        assert isinstance(read, Problem)
        self.assertIn("host provision --only disk-layout", read.fix)

    def test_new_and_found_shards_are_held_to_mode(self) -> None:
        for existing_mode in (None, 0o2750, cas.DIRECTORY_MODE):
            with self.subTest(existing_mode=existing_mode):
                host = FakeBytesHost(self.root)
                data = f"shard {existing_mode}".encode()
                name = hashlib.sha256(data).hexdigest()
                path = cas.object_path(name, self.root)
                if existing_mode is not None:
                    host.directories[path.parent.parent] = Directory(
                        existing_mode, 10001, 1000
                    )
                    host.directories[path.parent] = Directory(
                        existing_mode, 10001, 1000
                    )
                result = cas.put(host, data, root=self.root)
                self.assertEqual(result, name)
                for directory in (path.parent.parent, path.parent):
                    self.assertEqual(
                        host.directories[directory].mode, cas.DIRECTORY_MODE
                    )
                    self.assertEqual(host.directories[directory].gid, 1000)
                    chmod_calls = [
                        call
                        for call in host.calls
                        if call.operation == "chmod" and call.path == directory
                    ]
                    self.assertEqual(
                        len(chmod_calls),
                        0 if existing_mode == cas.DIRECTORY_MODE else 1,
                    )
                    if chmod_calls:
                        self.assertEqual(chmod_calls[0].mode, cas.DIRECTORY_MODE)

    def test_link_before_failed_flush_is_recovered_by_next_put(self) -> None:
        data = b"linked before flush"
        name = hashlib.sha256(data).hexdigest()
        path = cas.object_path(name, self.root)
        self.host.errors[("sync_directory", path.parent)] = OSError(
            errno.ENOSPC, "private detail"
        )
        failed = cas.put(self.host, data, root=self.root)
        self.assertIsInstance(failed, Problem)
        assert isinstance(failed, Problem)
        self.assertIn("ENOSPC", failed.problem)
        self.assertIn(str(path.parent), failed.problem)
        self.assertNotIn("private detail", failed.problem)
        self.assertIn(path, self.host.files)

        del self.host.errors[("sync_directory", path.parent)]
        self.host.calls.clear()
        self.assertEqual(cas.put(self.host, data, root=self.root), name)
        self.assertEqual(
            [
                call.path
                for call in self.host.calls
                if call.operation == "sync_directory"
            ],
            [path.parent, path.parent.parent, self.root],
        )
        self.assertFalse(
            any(call.operation == "create_exclusive" for call in self.host.calls)
        )

    def test_incomplete_shard_mode_is_repaired_or_refused(self) -> None:
        data = b"mode repair"
        name = hashlib.sha256(data).hexdigest()
        path = cas.object_path(name, self.root)
        self.host.directories[path.parent.parent] = Directory(0o2750, 10001, 1000)
        self.host.directories[path.parent] = Directory(0o2750, 10001, 1000)
        self.host.errors[("chmod", path.parent.parent)] = PermissionError(
            errno.EPERM, "denied"
        )
        refused = cas.put(self.host, data, root=self.root)
        self.assertIsInstance(refused, Problem)
        assert isinstance(refused, Problem)
        self.assertIn(str(path.parent.parent), refused.problem)
        self.assertIn(
            f"sudo chmod {cas.DIRECTORY_MODE:04o} {path.parent.parent}", refused.fix
        )
        self.assertNotIn(path, self.host.files)

        del self.host.errors[("chmod", path.parent.parent)]
        self.host.calls.clear()
        self.assertEqual(cas.put(self.host, data, root=self.root), name)
        self.assertEqual(
            self.host.directories[path.parent.parent].mode, cas.DIRECTORY_MODE
        )
        self.assertEqual(self.host.directories[path.parent].mode, cas.DIRECTORY_MODE)

    def test_shard_errors_outside_chmod_are_io_refusals(self) -> None:
        data = b"shard error"
        path = cas.object_path(hashlib.sha256(data).hexdigest(), self.root)
        for operation in ("mkdir", "stat"):
            with self.subTest(operation=operation):
                host = FakeBytesHost(self.root)
                host.errors[(operation, path.parent.parent)] = PermissionError(
                    errno.EACCES, "denied"
                )
                result = cas.put(host, data, root=self.root)
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("EACCES", result.problem)
                self.assertNotIn("sudo chmod", result.fix)
                self.assertNotIn(path, host.files)

    def test_all_name_returning_paths_flush_in_order(self) -> None:
        for scenario in ("created", "present", "race_lost"):
            with self.subTest(scenario=scenario):
                host = FakeBytesHost(self.root)
                data = scenario.encode()
                name = hashlib.sha256(data).hexdigest()
                path = cas.object_path(name, self.root)
                if scenario == "present":
                    host.directories[path.parent.parent] = Directory(
                        cas.DIRECTORY_MODE, 10001, 1000
                    )
                    host.directories[path.parent] = Directory(
                        cas.DIRECTORY_MODE, 10001, 1000
                    )
                    host._new_file(path, data, cas.OBJECT_MODE)
                if scenario == "race_lost":
                    host.lose_create.add(path)
                self.assertEqual(cas.put(host, data, root=self.root), name)
                syncs = [
                    call.path
                    for call in host.calls
                    if call.operation == "sync_directory"
                ]
                expected = [path.parent, path.parent.parent, self.root]
                if scenario != "present":
                    expected.insert(0, path.parent)
                self.assertEqual(syncs, expected)
                create_calls = [
                    call for call in host.calls if call.operation == "create_exclusive"
                ]
                self.assertEqual(len(create_calls), 0 if scenario == "present" else 1)
                self.assertEqual(host.files[path].data, data)
                self.assertFalse(
                    any(
                        call.operation in {"unlink", "write_text"}
                        for call in host.calls
                    )
                )
                self.assertFalse(
                    any(
                        call.operation == "chmod" and call.path == path
                        for call in host.calls
                    )
                )

    def test_verify_and_zero_byte_object(self) -> None:
        name = self.put_name(b"")
        path = cas.object_path(name, self.root)
        self.assertEqual(self.host.files[path].data, b"")
        self.assertEqual(cas.get(self.host, name, root=self.root), b"")
        self.assertIsNone(cas.verify(self.host, name, root=self.root))
        self.host.files[path].data = b"damaged"
        self.assertIsInstance(cas.verify(self.host, name, root=self.root), Problem)


class RealStore(unittest.TestCase):
    """The durable byte seam and the object modes on a temporary root."""

    def test_create_exclusive_preserves_existing_object_and_syncs_both_times(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            target = parent / "object"
            host = RealHost()
            with mock.patch.object(
                host, "sync_directory", wraps=host.sync_directory
            ) as sync:
                self.assertTrue(
                    host.create_exclusive(target, b"first", mode=cas.OBJECT_MODE)
                )
                before = target.stat()
                self.assertFalse(
                    host.create_exclusive(target, b"second", mode=cas.OBJECT_MODE)
                )
            after = target.stat()
            self.assertEqual(
                [call.args for call in sync.call_args_list], [(parent,), (parent,)]
            )
            self.assertEqual(
                (after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns)
            )
            self.assertEqual(host.read_bytes(target), b"first")
            self.assertEqual(stat.S_IMODE(after.st_mode), cas.OBJECT_MODE)
            self.assertEqual(list(parent.iterdir()), [target])
            self.assertFalse(
                any(path.name.startswith(".") for path in parent.iterdir())
            )

    def test_sync_directory_raises_for_missing_directory(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaises(FileNotFoundError),
        ):
            RealHost().sync_directory(Path(directory) / "missing")

    def test_store_round_trip_and_modes_under_umask(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cas"
            host = RealHost()
            data = b"real store bytes"
            previous_umask = os.umask(0o022)
            try:
                root.mkdir(mode=cas.DIRECTORY_MODE)
                root.chmod(cas.DIRECTORY_MODE)
                result = cas.put(host, data, root=root)
            finally:
                os.umask(previous_umask)
            self.assertIsInstance(result, str)
            assert isinstance(result, str)
            path = cas.object_path(result, root)
            self.assertEqual(host.read_bytes(path), data)
            self.assertEqual(cas.get(host, result, root=root), data)
            self.assertIsNone(cas.verify(host, result, root=root))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), cas.OBJECT_MODE)
            self.assertEqual(
                stat.S_IMODE(path.parent.stat().st_mode), cas.DIRECTORY_MODE
            )
            self.assertEqual(
                stat.S_IMODE(path.parent.parent.stat().st_mode), cas.DIRECTORY_MODE
            )
