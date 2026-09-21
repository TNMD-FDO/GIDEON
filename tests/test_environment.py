"""Contract tests for the pinned development-environment comparison."""

from __future__ import annotations

import ast
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterable, Sequence
from pathlib import Path
from unittest import mock

from tools import environment

ROOT = Path(__file__).resolve().parent.parent
FICTIONAL_VERSION = "1000.0.0"


def write_pins(root: Path, *lines: str) -> None:
    """Write a temporary checkout's visibly fictitious pin file."""

    root.mkdir(parents=True, exist_ok=True)
    (root / environment.PIN_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


def installed(
    name: str,
    version: str = FICTIONAL_VERSION,
    requires: Iterable[str] = (),
) -> environment.InstalledDistribution:
    """Build one installed-distribution reading for the injected seam."""

    return environment.InstalledDistribution(name, version, tuple(requires))


def run_comparison(
    root: Path,
    *,
    packages: Sequence[environment.InstalledDistribution] = (),
    pip_result: tuple[int, str] = (0, ""),
    argv: Sequence[str] = (),
) -> tuple[int, str, str, list[str]]:
    """Run the module over injected readings and capture both streams."""

    distribution_calls: list[str] = []
    pip_calls: list[str] = []

    def distributions() -> Sequence[environment.InstalledDistribution]:
        distribution_calls.append("read")
        return packages

    def pip_check() -> tuple[int, str]:
        pip_calls.append("check")
        return pip_result

    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = environment.main(
            argv,
            root=root,
            distributions=distributions,
            pip_check=pip_check,
        )
    return code, stdout.getvalue(), stderr.getvalue(), distribution_calls + pip_calls


class PinInput(unittest.TestCase):
    """Exact pin-file grammar and its input-error boundary."""

    def forbidden(self) -> Sequence[environment.InstalledDistribution]:
        raise AssertionError("the environment seam was read")

    def forbidden_check(self) -> tuple[int, str]:
        raise AssertionError("the pip-check seam was read")

    def test_pin_grammar_skips_comments_and_accepts_surrounding_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_pins(root, "", "  # comment", "  Fiction_Pkg==1000.0.0  ")
            code, stdout, stderr, calls = run_comparison(
                root, packages=(installed("fiction-pkg"),)
            )
        self.assertEqual(code, 0)
        self.assertIn("equal to requirements-dev.txt", stdout)
        self.assertEqual(stderr, "")
        self.assertEqual(calls, ["read"])

    def test_missing_pin_file_exits_two_before_reading_either_seam(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = environment.main(
                    [],
                    root=root,
                    distributions=self.forbidden,
                    pip_check=self.forbidden_check,
                )
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("cannot read requirements-dev.txt", stderr.getvalue())
        self.assertIn("Fix: Repair requirements-dev.txt", stderr.getvalue())

    def test_malformed_pin_exits_two_with_line_number_and_repair_fix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_pins(root, "fictional==1000.0.0", "fictional>=1000.0.0")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = environment.main(
                    [],
                    root=root,
                    distributions=self.forbidden,
                    pip_check=self.forbidden_check,
                )
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("line 2", stderr.getvalue())
        self.assertIn("expected an exact name==version pin", stderr.getvalue())
        self.assertIn("Fix: Repair requirements-dev.txt", stderr.getvalue())
        self.assertNotIn("Build a fresh venv", stderr.getvalue())

    def test_duplicate_normalized_pin_exits_two_with_duplicate_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_pins(root, "Fiction_Pkg==1000.0.0", "fiction-pkg==1000.0.1")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = environment.main(
                    [],
                    root=root,
                    distributions=self.forbidden,
                    pip_check=self.forbidden_check,
                )
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("line 2: package fiction-pkg is pinned more than once", stderr.getvalue())
        self.assertIn("Fix: Repair requirements-dev.txt", stderr.getvalue())

    def test_pins_only_does_not_touch_either_seam_on_valid_or_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_pins(root, "fictional==1000.0.0")
            code, stdout, stderr, calls = run_comparison(
                root,
                argv=("--pins-only",),
                packages=(installed("fictional"),),
            )
            self.assertEqual(code, 0)
            self.assertIn("valid exact pins", stdout)
            self.assertEqual(stderr, "")
            self.assertEqual(calls, [])

            write_pins(root, "fictional>=1000.0.0")
            code, stdout, stderr, calls = run_comparison(
                root,
                argv=("--pins-only",),
                packages=(installed("fictional"),),
            )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("Fix: Repair requirements-dev.txt", stderr)
        self.assertEqual(calls, [])

    def test_names_are_normalized_on_both_sides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_pins(root, "Fictional_Pkg==1000.0.0")
            code, stdout, stderr, _calls = run_comparison(
                root, packages=(installed("fictional.pkg"),)
            )
        self.assertEqual(code, 0)
        self.assertIn("equal to requirements-dev.txt", stdout)
        self.assertEqual(stderr, "")


class Comparison(unittest.TestCase):
    """Dependency closure, installer packages, and collected findings."""

    def test_extra_marker_is_not_followed_but_other_markers_are(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_pins(root, "root-package==1000.0.0")
            packages = (
                installed(
                    "root-package",
                    requires=(
                        'extra-child>=1000; extra == "test"',
                        'platform-child>=1000; python_version >= "3.12"',
                    ),
                ),
                installed("extra-child"),
                installed("platform-child"),
            )
            code, _stdout, stderr, _calls = run_comparison(root, packages=packages)
        self.assertEqual(code, 1)
        self.assertIn("package extra-child==1000.0.0", stderr)
        self.assertIn("outside the pins' dependency closure", stderr)
        self.assertNotIn("platform-child", stderr)

    def test_closure_walks_to_a_fixed_point_through_normalized_requirement_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_pins(root, "root-package==1000.0.0")
            packages = (
                installed("root-package", requires=("Child_One>=1000",)),
                installed("child.one", requires=("CHILD-TWO",)),
                installed("child-two", requires=("child_three; python_version >= '3.12'",)),
                installed("child-three"),
            )
            code, stdout, stderr, _calls = run_comparison(root, packages=packages)
        self.assertEqual(code, 0, stderr)
        self.assertIn("equal to requirements-dev.txt", stdout)
        self.assertEqual(stderr, "")

    def test_installer_distributions_are_allowed_by_the_named_constant(self) -> None:
        self.assertEqual(
            environment.INSTALLER_PACKAGES,
            frozenset({"pip", "setuptools", "wheel"}),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_pins(root, "root-package==1000.0.0")
            packages = (
                installed("root-package"),
                installed("pip"),
                installed("setuptools"),
                installed("wheel"),
            )
            code, stdout, stderr, calls = run_comparison(root, packages=packages)
        self.assertEqual(code, 0)
        self.assertIn("pip check passed", stdout)
        self.assertEqual(stderr, "")
        self.assertEqual(calls, ["read", "check"])

    def test_all_five_finding_kinds_are_collected_with_pip_lines_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_pins(
                root,
                "root-package==1000.0.0",
                "missing-package==1000.0.0",
                "wrong-package==1000.0.0",
                "duplicate-package==1000.0.0",
            )
            pip_lines = (
                "root-package has requirement missing-transitive>=1000, but you have missing-transitive 1.0.",
                "root-package has requirement incompatible-transitive==1000.0.0, but you have incompatible-transitive 999.0.0.",
            )
            packages = (
                installed("root-package"),
                installed("wrong-package", "999.0.0"),
                installed("duplicate-package"),
                installed("duplicate-package"),
                installed("outside-package"),
                installed("pip"),
            )
            code, stdout, stderr, calls = run_comparison(
                root,
                packages=packages,
                pip_result=(1, "\n".join(pip_lines)),
            )
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(calls, ["read", "check"])
        self.assertIn("package outside-package==1000.0.0", stderr)
        self.assertIn("package missing-package==1000.0.0 is not installed", stderr)
        self.assertIn("package wrong-package is installed at 999.0.0, expected 1000.0.0", stderr)
        self.assertIn("package duplicate-package is installed more than once", stderr)
        for line in pip_lines:
            self.assertIn(f"environment: {line}", stderr)
        self.assertEqual(stderr.count("environment: Fix:"), 1)
        self.assertTrue(stderr.splitlines()[-1].startswith("environment: Fix: "))

    def test_environment_without_pip_is_equal_and_does_not_run_the_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_pins(root, "root-package==1000.0.0")
            code, stdout, stderr, calls = run_comparison(
                root,
                packages=(installed("root-package"),),
            )
        self.assertEqual(code, 0)
        self.assertIn("pip check did not run", stdout)
        self.assertEqual(stderr, "")
        self.assertEqual(calls, ["read"])

    def test_empty_pip_check_output_names_the_exit_without_attributing_a_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_pins(root, "root-package==1000.0.0")
            code, stdout, stderr, _calls = run_comparison(
                root,
                packages=(installed("root-package"), installed("pip")),
                pip_result=(7, ""),
            )
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("pip check exited 7 without naming a package", stderr)
        self.assertNotIn("package pip:", stderr)


class Fixes(unittest.TestCase):
    """The environment repair names the linked or owned interpreter prefix."""

    def mismatch(self, root: Path) -> tuple[int, str]:
        write_pins(root, "root-package==1000.0.0")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = environment.main(
                [],
                root=root,
                distributions=lambda: (installed("outside-package"),),
                pip_check=lambda: (0, ""),
            )
        return code, stderr.getvalue()

    def test_owned_prefix_uses_fresh_venv_fix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            prefix = Path(directory) / "venv"
            prefix.mkdir()
            with mock.patch.object(environment.sys, "prefix", str(prefix)):
                code, stderr = self.mismatch(root)
        self.assertEqual(code, 1)
        self.assertIn(environment.OWN_FIX, stderr)
        self.assertNotIn(environment.LINK_FIX, stderr)

    def test_symlinked_prefix_uses_link_fix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            target = Path(directory) / "primary-venv"
            target.mkdir()
            prefix = Path(directory) / "linked-venv"
            prefix.symlink_to(target, target_is_directory=True)
            with mock.patch.object(environment.sys, "prefix", str(prefix)):
                code, stderr = self.mismatch(root)
        self.assertEqual(code, 1)
        self.assertIn(environment.LINK_FIX, stderr)
        self.assertNotIn(environment.OWN_FIX, stderr)

    def test_environment_difference_is_exit_one_while_pin_input_is_exit_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_pins(root, "root-package>=1000.0.0")
            code, _stdout, _stderr, _calls = run_comparison(root)
            self.assertEqual(code, 2)
            write_pins(root, "root-package==1000.0.0")
            code, _stdout, _stderr, _calls = run_comparison(
                root, packages=(installed("different-package"),)
            )
        self.assertEqual(code, 1)


class Boundary(unittest.TestCase):
    """The module remains runnable by path and standard-library only."""

    def test_by_path_under_p_reads_a_temporary_pin_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_pins(root, "fictional-package==1000.0.0")
            result = subprocess.run(
                (
                    sys.executable,
                    "-P",
                    str(ROOT / "tools" / "environment.py"),
                    "--pins-only",
                ),
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("valid exact pins", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_module_imports_only_standard_library_modules(self) -> None:
        source = (ROOT / "tools" / "environment.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        standard_library = set(sys.stdlib_module_names)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.module == "__future__":
                    continue
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                self.assertIn(module.split(".", 1)[0], standard_library, module)


if __name__ == "__main__":
    unittest.main()
