"""Run the provisioned command wrapper under a real POSIX shell."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from gideon.host.steps.command import COMMAND_PATH, INSTALL_HOME, command_text


@unittest.skipUnless(shutil.which("/bin/sh"), "POSIX shell is unavailable")
class GideonCommandWrapperTests(unittest.TestCase):
    def _fixture(self, root: Path, *, uid: str = "1000", checkout: bool = True) -> tuple[Path, Path, Path, dict[str, str]]:
        home = root / "installed release"
        if checkout:
            entry = home / "gideon" / "__main__.py"
            entry.parent.mkdir(parents=True)
            entry.write_text("# fictitious checkout\n")
        wrapper = root / "gideon-wrapper"
        wrapper.write_text(command_text(home))
        wrapper.chmod(0o755)
        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        log = root / "invocation.log"
        self._fake(fake_bin / "id", "printf '%s\\n' \"$FAKE_UID\"\n")
        self._fake(fake_bin / "sudo", "printf '%s\\n' \"$PWD\" \"$@\" > \"$GIDEON_LOG\"\n")
        self._fake(fake_bin / "python3", "printf '%s\\n' \"$PWD\" \"$@\" > \"$GIDEON_LOG\"\n")
        elsewhere = root / "elsewhere"
        elsewhere.mkdir()
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{fake_bin}{os.pathsep}/usr/bin:/bin",
                "FAKE_UID": uid,
                "GIDEON_LOG": str(log),
            }
        )
        return wrapper, home, elsewhere, env

    @staticmethod
    def _fake(path: Path, body: str) -> None:
        path.write_text(f"#!/bin/sh\n{body}")
        path.chmod(0o755)

    def _run(self, wrapper: Path, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(wrapper), "two words", "--flag=with space"],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_non_root_reexecutes_installed_absolute_command_with_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wrapper, _, elsewhere, env = self._fixture(Path(directory))
            result = self._run(wrapper, elsewhere, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                Path(env["GIDEON_LOG"]).read_text().splitlines(),
                [str(elsewhere), str(COMMAND_PATH), "two words", "--flag=with space"],
            )

    def test_root_runs_module_from_install_home_not_callers_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wrapper, home, elsewhere, env = self._fixture(Path(directory), uid="0")
            result = self._run(wrapper, elsewhere, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                Path(env["GIDEON_LOG"]).read_text().splitlines(),
                [str(home), "-m", "gideon", "two words", "--flag=with space"],
            )

    def test_missing_checkout_refuses_before_running_a_tool(self) -> None:
        for checkout in (False, True):
            with self.subTest(checkout=checkout), tempfile.TemporaryDirectory() as directory:
                wrapper, home, elsewhere, env = self._fixture(
                    Path(directory), checkout=checkout
                )
                if checkout:
                    (home / "gideon" / "__main__.py").unlink()
                result = self._run(wrapper, elsewhere, env)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"gideon: {home} holds no GIDEON checkout.", result.stderr)
                self.assertIn("Fix: clone the release tag", result.stderr)
                self.assertFalse(Path(env["GIDEON_LOG"]).exists())

    def test_release_wrapper_names_its_fixed_install_and_command_paths(self) -> None:
        text = command_text(INSTALL_HOME)
        self.assertIn("/opt/gideon", text)
        self.assertIn(str(COMMAND_PATH), text)


if __name__ == "__main__":
    unittest.main()
