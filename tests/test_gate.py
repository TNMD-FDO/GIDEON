"""The gate in-process: order, the stop at the first red tool, the paths, one line."""

from __future__ import annotations

import ast
import contextlib
import io
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from tools import gate

ROOT = Path(__file__).resolve().parent.parent


class RecordingRunner:
    """A runner that records every command and answers from a script of codes."""

    def __init__(self, codes: Sequence[int]) -> None:
        self.codes = list(codes)
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str]) -> int:
        self.commands.append(tuple(command))
        return self.codes[len(self.commands) - 1]


def run_gate(argv: Sequence[str], runner: RecordingRunner) -> tuple[int, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = gate.main(argv, runner=runner)
    return code, stdout.getvalue()


class Gate(unittest.TestCase):
    def test_green_runs_the_three_tools_in_order_and_prints_one_line(self) -> None:
        runner = RecordingRunner([0, 0, 0])
        code, output = run_gate([], runner)
        self.assertEqual(code, 0)
        self.assertEqual([Path(command[0]).name for command in runner.commands], list(gate.TOOLS))
        self.assertEqual(runner.commands[0][1:], ("check", "."))
        self.assertEqual(runner.commands[1][1:], ("gideon", "tests", "tools"))
        self.assertEqual(runner.commands[2][1:], ("-x", "-m", "not slow"))
        lines = output.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(
            lines[0],
            r"^gate: green in \d+\.\d+s \(ruff, mypy, pytest; slow cases skipped, --all runs them\)$",
        )

    def test_test_paths_pass_through_to_pytest_alone(self) -> None:
        runner = RecordingRunner([0, 0, 0])
        code, _output = run_gate(["tests/test_gate.py", "tests/test_tracker.py"], runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.commands[0][1:], ("check", "."))
        self.assertEqual(runner.commands[1][1:], ("gideon", "tests", "tools"))
        self.assertEqual(
            runner.commands[2][1:],
            ("-x", "-m", "not slow", "tests/test_gate.py", "tests/test_tracker.py"),
        )

    def test_all_drops_the_slow_filter_and_says_so(self) -> None:
        runner = RecordingRunner([0, 0, 0])
        code, output = run_gate(["--all", "tests/test_gate.py"], runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.commands[2][1:], ("-x", "tests/test_gate.py"))
        self.assertRegex(
            output.splitlines()[0], r"^gate: green in \d+\.\d+s \(ruff, mypy, pytest; all cases\)$"
        )

    def test_red_tool_stops_the_gate_with_its_exit_code(self) -> None:
        runner = RecordingRunner([0, 2, 0])
        code, output = run_gate([], runner)
        self.assertEqual(code, 2)
        self.assertEqual(len(runner.commands), 2)
        lines = output.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^gate: red at mypy \(exit 2\) after \d+\.\d+s$")

    def test_resolve_falls_back_to_the_venv_then_the_bare_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            venv_tool = root / ".venv" / "bin" / "example-gate-tool"
            venv_tool.parent.mkdir(parents=True)
            venv_tool.write_text("#!/bin/sh\n", encoding="utf-8")
            self.assertEqual(gate.resolve("example-gate-tool", root), str(venv_tool))
            self.assertEqual(gate.resolve("example-absent-tool", root), "example-absent-tool")

    def test_gate_imports_only_standard_library_modules(self) -> None:
        path = ROOT / "tools" / "gate.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                targets = [node.module or ""]
            else:
                continue
            for target in targets:
                self.assertIn(
                    target.partition(".")[0],
                    sys.stdlib_module_names,
                    f"{path}:{node.lineno} imports {target}",
                )


if __name__ == "__main__":
    unittest.main()
