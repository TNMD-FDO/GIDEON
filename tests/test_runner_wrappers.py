"""Run the provision-owned smoke wrapper under a real POSIX shell."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from gideon.host.steps.services import SMOKE_CHECKOUT, smoke_wrapper_text


@unittest.skipUnless(shutil.which("/bin/sh"), "POSIX shell is unavailable")
class SmokeWrapperTests(unittest.TestCase):
    def _fixture(
        self, root: Path, *, checkout: bool = True
    ) -> tuple[Path, Path, Path, dict[str, str]]:
        home = root / "runner's checkout"
        if checkout:
            entry = home / "tools/cistack/__main__.py"
            entry.parent.mkdir(parents=True)
            entry.write_text("# fictitious checkout\n")
        wrapper = root / "smoke-wrapper"
        wrapper.write_text(smoke_wrapper_text(home))
        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        python = fake_bin / "python3"
        python.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@" > "$GIDEON_SMOKE_LOG"\n')
        python.chmod(0o755)
        elsewhere = root / "elsewhere"
        elsewhere.mkdir()
        environment = {
            **os.environ,
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "GIDEON_SMOKE_LOG": str(root / "invocation.log"),
        }
        return wrapper, home, elsewhere, environment

    def test_smoke_runs_from_the_checkout_with_bytecode_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wrapper, home, elsewhere, environment = self._fixture(Path(directory))
            result = subprocess.run(
                ["/bin/sh", str(wrapper)], cwd=elsewhere, env=environment,
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(
                Path(environment["GIDEON_SMOKE_LOG"]).read_text().splitlines(),
                [str(home), "-B", "-m", "tools.cistack", "smoke"],
            )

    def test_argument_refuses_before_any_tool_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wrapper, _home, elsewhere, environment = self._fixture(Path(directory))
            result = subprocess.run(
                ["/bin/sh", str(wrapper), "extra"], cwd=elsewhere, env=environment,
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertRegex(result.stderr, r"^gideon-ci-smoke: takes no argument\. Fix: [^\n]+\n$")
            self.assertFalse(Path(environment["GIDEON_SMOKE_LOG"]).exists())

    def test_missing_checkout_refuses_with_a_fix_and_no_tool_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wrapper, home, elsewhere, environment = self._fixture(Path(directory), checkout=False)
            result = subprocess.run(
                ["/bin/sh", str(wrapper)], cwd=elsewhere, env=environment,
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertIn(str(home), result.stderr)
            self.assertIn("Fix: a push to main checks it out there", result.stderr)
            self.assertIn("sudo python3 -B -m tools.cistack smoke from any checkout", result.stderr)
            self.assertEqual(len(result.stderr.splitlines()), 1)
            self.assertFalse(Path(environment["GIDEON_SMOKE_LOG"]).exists())

    def test_release_text_names_the_runner_checkout_and_disables_bytecode(self) -> None:
        text = smoke_wrapper_text()
        self.assertEqual(str(SMOKE_CHECKOUT), "/opt/gh-runner/_work/GIDEON-dev/GIDEON-dev")
        self.assertIn(str(SMOKE_CHECKOUT), text)
        self.assertIn("exec python3 -B -m tools.cistack smoke\n", text)


if __name__ == "__main__":
    unittest.main()
