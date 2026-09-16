"""No-GPU host-state marker contracts from the acceptance plan."""

import os
import subprocess
import unittest
from collections.abc import Mapping
from pathlib import Path

from gideon.host.nogpu import (
    BUILD_BOX_DECLARED_FIX,
    BUILD_BOX_DECLARED_PROBLEM,
    BUILD_BOX_PATH,
    GPU_DRIVER_FIX,
    NO_GPU_DECLARED_FIX,
    NO_GPU_DECLARED_PROBLEM,
    NO_GPU_PATH,
    NVIDIA_DEVICE_FIX,
    NVIDIA_DEVICE_PROBLEM,
    build_box_declaration_problem,
    declaration_problem,
    declare,
    declare_build_box,
    has_nvidia_device,
    is_build_box,
    is_no_gpu_host,
)
from gideon.host.report import Problem
from gideon.host.sysio import Command, PathLike


class FakeHost:
    def __init__(
        self,
        *,
        files: Mapping[str, str] | None = None,
        write_fails: bool = False,
    ) -> None:
        self.files = dict(files or {})
        self.calls: list[tuple[str, object]] = []
        self.write_fails = write_fails

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
        del check, input, cwd, env, timeout, passthrough
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        self.calls.append(("read_text", key))
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
        if self.write_fails:
            raise OSError("write refused")
        key = os.fspath(path)
        self.calls.append(("write_text", (key, text, mode)))
        self.files[key] = text

    def exists(self, path: PathLike) -> bool:
        key = os.fspath(path)
        self.calls.append(("exists", key))
        return key in self.files

    def listdir(self, path: PathLike) -> list[str]:
        root = Path(path)
        entries = {
            candidate.parent.name
            for name in self.files
            if (candidate := Path(name)).parent.parent == root
        }
        if not entries and not any(Path(name).parent == root for name in self.files):
            raise FileNotFoundError(os.fspath(path))
        return sorted(entries)

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del path, missing_ok

    def stat(self, path: PathLike) -> os.stat_result:
        raise FileNotFoundError(os.fspath(path))

    def chmod(self, path: PathLike, mode: int) -> None:
        del path, mode

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        self.calls.append(("chown", (os.fspath(path), uid, gid)))

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        self.calls.append(("mkdir", (os.fspath(path), mode, parents, exist_ok)))

    def geteuid(self) -> int:
        return 0


class Marker(unittest.TestCase):
    def test_marker_is_read_and_declared_absent_only(self) -> None:
        host = FakeHost()
        self.assertFalse(is_no_gpu_host(host))
        self.assertIsNone(declare(host))
        self.assertTrue(is_no_gpu_host(host))
        self.assertIn("--no-gpu", host.files[os.fspath(NO_GPU_PATH)])
        self.assertIn(
            ("mkdir", ("/etc/gideon", 0o755, True, True)),
            host.calls,
        )
        writes = [call[1] for call in host.calls if call[0] == "write_text"]
        self.assertEqual(len(writes), 1)
        written = writes[0]
        assert isinstance(written, tuple)
        self.assertEqual((written[0], written[2]), ("/etc/gideon/no-gpu", 0o644))

        # A second declaration is a no-op: the marker is never rewritten.
        host.files[os.fspath(NO_GPU_PATH)] = "edited by hand"
        self.assertIsNone(declare(host))
        self.assertEqual(
            len([call for call in host.calls if call[0] == "write_text"]), 1
        )
        self.assertEqual(host.files[os.fspath(NO_GPU_PATH)], "edited by hand")

    def test_build_box_marker_is_read_and_declared_absent_only(self) -> None:
        host = FakeHost()
        self.assertFalse(is_build_box(host))
        self.assertIsNone(declare_build_box(host))
        self.assertTrue(is_build_box(host))
        self.assertIn("--build-box", host.files[os.fspath(BUILD_BOX_PATH)])
        self.assertIn(
            ("mkdir", ("/etc/gideon", 0o755, True, True)),
            host.calls,
        )
        writes = [call[1] for call in host.calls if call[0] == "write_text"]
        self.assertEqual(len(writes), 1)
        written = writes[0]
        assert isinstance(written, tuple)
        self.assertEqual((written[0], written[2]), (os.fspath(BUILD_BOX_PATH), 0o644))

        host.files[os.fspath(BUILD_BOX_PATH)] = "edited by hand"
        self.assertIsNone(declare_build_box(host))
        self.assertEqual(
            len([call for call in host.calls if call[0] == "write_text"]), 1
        )
        self.assertEqual(host.files[os.fspath(BUILD_BOX_PATH)], "edited by hand")

    def test_build_box_marker_write_failure_names_the_marker(self) -> None:
        host = FakeHost(write_fails=True)
        problem = declare_build_box(host)
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertIn(os.fspath(BUILD_BOX_PATH), problem.problem)

    def test_build_box_declaration_refuses_a_declared_no_gpu_host(self) -> None:
        host = FakeHost(files={os.fspath(NO_GPU_PATH): ""})
        expected = Problem(NO_GPU_DECLARED_PROBLEM, NO_GPU_DECLARED_FIX)
        self.assertEqual(build_box_declaration_problem(host), expected)
        self.assertEqual(declare_build_box(host), expected)
        self.assertNotIn(os.fspath(BUILD_BOX_PATH), host.files)

    def test_build_box_declaration_does_not_probe_for_nvidia_devices(self) -> None:
        vendor = "/sys/bus/pci/devices/0000:01:00.0/vendor"
        host = FakeHost(files={vendor: "0x10de\n"})
        self.assertIsNone(build_box_declaration_problem(host))
        self.assertIsNone(declare_build_box(host))
        self.assertNotIn(("read_text", vendor), host.calls)

    def test_a_marker_pair_reads_as_a_no_gpu_host_and_refuses_both_declarations(self) -> None:
        host = FakeHost(files={os.fspath(NO_GPU_PATH): "", os.fspath(BUILD_BOX_PATH): ""})
        self.assertTrue(is_no_gpu_host(host))
        self.assertFalse(is_build_box(host))
        self.assertEqual(
            build_box_declaration_problem(host),
            Problem(NO_GPU_DECLARED_PROBLEM, NO_GPU_DECLARED_FIX),
        )
        self.assertEqual(
            declaration_problem(host),
            Problem(BUILD_BOX_DECLARED_PROBLEM, BUILD_BOX_DECLARED_FIX),
        )

    def test_no_gpu_declaration_refuses_a_declared_build_box(self) -> None:
        host = FakeHost(files={os.fspath(BUILD_BOX_PATH): ""})
        self.assertEqual(
            declaration_problem(host),
            Problem(BUILD_BOX_DECLARED_PROBLEM, BUILD_BOX_DECLARED_FIX),
        )
        self.assertIn(os.fspath(BUILD_BOX_PATH), BUILD_BOX_DECLARED_FIX)

    def test_declaration_refuses_an_nvidia_pci_vendor(self) -> None:
        vendor = "/sys/bus/pci/devices/0000:01:00.0/vendor"
        host = FakeHost(files={vendor: "0x10de\n"})
        self.assertTrue(has_nvidia_device(host))
        self.assertEqual(
            declaration_problem(host), Problem(NVIDIA_DEVICE_PROBLEM, NVIDIA_DEVICE_FIX)
        )
        self.assertEqual(declare(host), Problem(NVIDIA_DEVICE_PROBLEM, NVIDIA_DEVICE_FIX))
        self.assertNotIn(os.fspath(NO_GPU_PATH), host.files)
        self.assertFalse(any(call[0] == "write_text" for call in host.calls))

    def test_a_declared_host_is_not_re_probed(self) -> None:
        """The marker wins over sysfs: a declared host stays declared."""

        vendor = "/sys/bus/pci/devices/0000:01:00.0/vendor"
        host = FakeHost(files={vendor: "0x10de\n", os.fspath(NO_GPU_PATH): ""})
        self.assertIsNone(declaration_problem(host))
        self.assertIsNone(declare(host))

    def test_no_sysfs_means_no_device(self) -> None:
        host = FakeHost(files={"/sys/bus/pci/devices/0000:00:1f.0/vendor": "0x8086\n"})
        self.assertFalse(has_nvidia_device(host))
        self.assertFalse(has_nvidia_device(FakeHost()))

    def test_fix_texts_name_the_ways_out(self) -> None:
        """The declaration refusal names the driver step; the render refusal names both fixes."""

        self.assertIn("host provision --only nvidia-driver", NVIDIA_DEVICE_FIX)
        self.assertIn("host provision --only nvidia-driver", GPU_DRIVER_FIX)
        self.assertIn("host provision --no-gpu", GPU_DRIVER_FIX)


if __name__ == "__main__":
    unittest.main()
