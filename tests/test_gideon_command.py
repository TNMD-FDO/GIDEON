"""Run the provisioned command wrapper under a real POSIX shell."""

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from gideon.host.report import GIDEON_INSTALLED_COMMAND
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
        logged = (
            f"printf '%s\\n' \"${{{GIDEON_INSTALLED_COMMAND}+x}}\" "
            f"\"${{{GIDEON_INSTALLED_COMMAND}-}}\" \"$PWD\" \"$@\" > \"$GIDEON_LOG\"\n"
        )
        self._fake(fake_bin / "sudo", logged)
        self._fake(fake_bin / "python3", logged)
        elsewhere = root / "elsewhere"
        elsewhere.mkdir()
        env = os.environ.copy()
        env.pop(GIDEON_INSTALLED_COMMAND, None)
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
                ["", "", str(elsewhere), str(COMMAND_PATH), "two words", "--flag=with space"],
            )

    def test_root_runs_module_from_install_home_not_callers_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wrapper, home, elsewhere, env = self._fixture(Path(directory), uid="0")
            result = self._run(wrapper, elsewhere, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                Path(env["GIDEON_LOG"]).read_text().splitlines(),
                ["x", str(COMMAND_PATH), str(home), "-m", "gideon", "two words", "--flag=with space"],
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
                self.assertEqual(result.returncode, 2)
                self.assertIn(f"gideon: {home} holds no GIDEON checkout.", result.stderr)
                self.assertIn("Fix: clone the release tag", result.stderr)
                self.assertFalse(Path(env["GIDEON_LOG"]).exists())

    def test_unenterable_home_refuses_with_a_fix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wrapper, home, elsewhere, env = self._fixture(Path(directory), uid="0")
            text = command_text(home)
            if os.geteuid() == 0:
                self.assertIn("cannot enter", text)
                self.assertIn("exit 2", text.split("cannot enter", 1)[1].split("\n", 1)[0])
                return

            fake_id = Path(directory) / "fake-bin" / "id"
            self._fake(fake_id, f"chmod 000 {shlex.quote(str(home))} || exit 1\nprintf '0\\n'\n")
            try:
                result = self._run(wrapper, elsewhere, env)
            finally:
                home.chmod(0o755)
            self.assertEqual(result.returncode, 2)
            self.assertIn(f"gideon: cannot enter {home}.", result.stderr)
            self.assertIn(f"Fix: repair access to {home}, then re-run.", result.stderr)
            self.assertFalse(Path(env["GIDEON_LOG"]).exists())

    def test_release_wrapper_names_its_fixed_install_and_command_paths(self) -> None:
        text = command_text(INSTALL_HOME)
        self.assertIn("/opt/gideon", text)
        self.assertIn(str(COMMAND_PATH), text)
        self.assertNotIn("exit 1", text)


if __name__ == "__main__":
    unittest.main()
