"""GPU card record contracts over a dict-backed Host."""

import os
import subprocess
import unittest
from collections.abc import Mapping

import yaml  # type: ignore[import-untyped]

from gideon.host import gpus, report
from gideon.host.report import Problem
from gideon.host.sysio import Command, PathLike


class FakeHost:
    def __init__(
        self,
        *,
        files: Mapping[str, str] | None = None,
        read_fails: bool = False,
        write_fails: bool = False,
    ) -> None:
        self.files = dict(files or {})
        self.calls: list[tuple[str, object]] = []
        self.read_fails = read_fails
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
        return subprocess.CompletedProcess(list(argv), 127, "", "")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        self.calls.append(("read_text", key))
        if self.read_fails:
            raise OSError("read refused")
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
        raise FileNotFoundError(os.fspath(path))

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del path, missing_ok

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
        self.calls.append(("mkdir", (os.fspath(path), mode, parents, exist_ok)))

    def geteuid(self) -> int:
        return 0


class Record(unittest.TestCase):
    PATH = os.fspath(gpus.GPU_RECORD_PATH)
    CARDS = ("GPU-alpha", "GPU-beta")

    def test_absent_record_is_written_once_with_header_parent_and_mode(self) -> None:
        host = FakeHost()
        self.assertIsNone(gpus.read(host))
        self.assertEqual(gpus.bind(host, self.CARDS), self.CARDS)
        text = host.files[self.PATH]
        self.assertTrue(text.startswith("#"))
        self.assertIn("nvidia-smi -L", text)
        self.assertIn("models.lock gpu: index", text)
        self.assertIn("remove this file as root", text)
        self.assertEqual(yaml.safe_load(text), {"gpus": list(self.CARDS)})
        self.assertIn(
            ("mkdir", (os.fspath(gpus.GPU_RECORD_PATH.parent), 0o755, True, True)),
            host.calls,
        )
        writes = [value for name, value in host.calls if name == "write_text"]
        self.assertEqual(writes, [(self.PATH, text, 0o644)])

        self.assertEqual(gpus.bind(host, tuple(reversed(self.CARDS))), self.CARDS)
        self.assertEqual(
            [value for name, value in host.calls if name == "write_text"], writes
        )

    def test_write_leaves_an_existing_record_untouched(self) -> None:
        original = "gpus:\n  - GPU-edited\n"
        host = FakeHost(files={self.PATH: original})
        self.assertIsNone(gpus.write(host, self.CARDS))
        self.assertEqual(host.files[self.PATH], original)
        self.assertFalse(any(name == "write_text" for name, _ in host.calls))

    def test_recorded_order_ignores_an_added_card(self) -> None:
        host = FakeHost(files={self.PATH: "gpus:\n  - GPU-alpha\n  - GPU-beta\n"})
        self.assertEqual(gpus.read(host), self.CARDS)
        self.assertEqual(
            gpus.bind(host, ("GPU-extra", "GPU-beta", "GPU-alpha")), self.CARDS
        )
        self.assertFalse(any(name == "write_text" for name, _ in host.calls))

    def test_missing_cards_refuse_with_positions_uuids_and_re_record_step(self) -> None:
        host = FakeHost(files={self.PATH: "gpus:\n  - GPU-alpha\n  - GPU-beta\n"})
        result = gpus.bind(host, ("GPU-other",))
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        for fragment in (self.PATH, "nvidia-smi -L", "GPU index 0", "GPU-alpha", "GPU index 1", "GPU-beta"):
            self.assertIn(fragment, result.problem)
        for fragment in (self.PATH, "as root", "render --diff"):
            self.assertIn(fragment, result.fix)
        self.assertFalse(any(name == "write_text" for name, _ in host.calls))

    def test_no_present_card_and_no_record_writes_nothing(self) -> None:
        host = FakeHost()
        self.assertEqual(gpus.bind(host, ()), ())
        self.assertNotIn(self.PATH, host.files)
        self.assertFalse(any(name == "write_text" for name, _ in host.calls))

    def test_malformed_records_refuse_by_path(self) -> None:
        malformed = (
            "",
            "gpus: []\n",
            "gpus: [GPU-alpha, GPU-alpha]\n",
            "gpus: [GPU-alpha, 7]\n",
            "gpus: ['']\n",
            "gpus: [GPU-alpha]\nother: value\n",
            "gpus: [\n",
        )
        for text in malformed:
            with self.subTest(text=text):
                host = FakeHost(files={self.PATH: text})
                result = gpus.read(host)
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn(self.PATH, result.problem)
                self.assertIn("render --diff", result.fix)

    def test_unreadable_record_refuses_with_the_re_record_step(self) -> None:
        result = gpus.read(FakeHost(read_fails=True))
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn(self.PATH, result.problem)
        self.assertIn("read refused", result.problem)
        self.assertIn("render --diff", result.fix)

    def test_write_failure_names_manual_creation(self) -> None:
        host = FakeHost(write_fails=True)
        result = gpus.bind(host, self.CARDS)
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn(self.PATH, result.problem)
        self.assertIn("write refused", result.problem)
        self.assertIn("as root", result.fix)
        self.assertIn("0644", result.fix)
        self.assertNotIn(self.PATH, host.files)

    def test_re_record_fix_uses_the_current_command_form(self) -> None:
        host = FakeHost(files={self.PATH: "gpus: []\n"})
        try:
            report.set_installed_form(False)
            long_form = gpus.read(host)
            report.set_installed_form(True)
            installed_form = gpus.read(host)
        finally:
            report.set_installed_form(False)
        assert isinstance(long_form, Problem) and isinstance(installed_form, Problem)
        self.assertIn("sudo python3 -m gideon render --diff", long_form.fix)
        self.assertIn("gideon render --diff", installed_form.fix)
        self.assertNotIn("python3 -m", installed_form.fix)


if __name__ == "__main__":
    unittest.main()
