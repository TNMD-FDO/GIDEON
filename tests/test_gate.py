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
from unittest import mock

from tools import gate, mask

ROOT = Path(__file__).resolve().parent.parent


class RecordingRunner:
    """A runner that records every command and answers from a script of codes."""

    def __init__(self, codes: Sequence[int]) -> None:
        self.codes = list(codes)
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str]) -> int:
        self.commands.append(tuple(command))
        return self.codes[len(self.commands) - 1]


class RecordingMask:
    """A mask seam that records observation, probes, and wrapping."""

    def __init__(
        self,
        observation: mask.Observation,
        probes: Sequence[mask.ProbeResult] = (),
    ) -> None:
        self.observation = observation
        self.probes = list(probes)
        self.observe_calls = 0
        self.probe_commands: list[tuple[str, ...]] = []
        self.probe_observations: list[mask.Observation] = []
        self.wrap_calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def observe(self) -> mask.Observation:
        self.observe_calls += 1
        return self.observation

    def probe(
        self, command: Sequence[str], observation: mask.Observation
    ) -> mask.ProbeResult:
        self.probe_commands.append(tuple(command))
        self.probe_observations.append(observation)
        return self.probes[len(self.probe_commands) - 1]

    def wrap(self, command: Sequence[str], paths: Sequence[str]) -> Sequence[str]:
        self.wrap_calls.append((tuple(command), tuple(paths)))
        return ("wrapped", *command)


def observation(state: mask.ProbeState, present: Sequence[str] = ()) -> mask.Observation:
    return mask.Observation(state, tuple(present), ())


def probe_result(
    state: mask.ProbeState,
    observed: mask.Observation,
    refusal: mask.Refusal | None = None,
) -> mask.ProbeResult:
    return mask.ProbeResult(state, observed, refusal=refusal, trial_code=0)


def fake_mask(
    state: mask.ProbeState = mask.ProbeState.ABSENT,
    present: Sequence[str] = (),
    probes: Sequence[mask.ProbeResult] = (),
) -> RecordingMask:
    return RecordingMask(observation(state, present), probes)


def mask_seam(fake: RecordingMask) -> gate.Mask:
    return gate.Mask(fake.observe, fake.probe, fake.wrap)


def run_gate(
    argv: Sequence[str],
    runner: RecordingRunner,
    fake: RecordingMask | None = None,
) -> tuple[int, str]:
    if fake is None:
        fake = fake_mask()
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = gate.main(argv, runner=runner, mask=mask_seam(fake))
    return code, stdout.getvalue()


def run_gate_with_stderr(
    argv: Sequence[str], runner: RecordingRunner, fake: RecordingMask
) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = gate.main(argv, runner=runner, mask=mask_seam(fake))
    return code, stdout.getvalue(), stderr.getvalue()


class Gate(unittest.TestCase):
    def test_green_runs_the_three_tools_in_order_and_prints_one_line(self) -> None:
        runner = RecordingRunner([0, 0, 0, 0])
        code, output = run_gate([], runner)
        self.assertEqual(code, 0)
        self.assertIn(Path(runner.commands[0][0]).name, {"python", "python3"})
        self.assertEqual(runner.commands[0][1], "-P")
        self.assertTrue(runner.commands[0][2].endswith("tools/environment.py"))
        self.assertEqual(
            [Path(command[0]).name for command in runner.commands[1:]], list(gate.TOOLS)
        )
        self.assertEqual(runner.commands[1][1:], ("check", "."))
        self.assertEqual(runner.commands[2][1:], ("gideon", "tests", "tools"))
        self.assertEqual(runner.commands[3][1:], ("-x", "-m", "not slow"))
        lines = output.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(
            lines[0],
            r"^gate: green in \d+\.\d+s \(environment, ruff, mypy, pytest; slow cases skipped, --all runs them; unmasked\)$",
        )

    def test_test_paths_pass_through_to_pytest_alone(self) -> None:
        runner = RecordingRunner([0, 0, 0, 0])
        code, _output = run_gate(["tests/test_gate.py", "tests/test_tracker.py"], runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.commands[1][1:], ("check", "."))
        self.assertEqual(runner.commands[2][1:], ("gideon", "tests", "tools"))
        self.assertEqual(
            runner.commands[3][1:],
            ("-x", "-m", "not slow", "tests/test_gate.py", "tests/test_tracker.py"),
        )

    def test_all_drops_the_slow_filter_and_says_so(self) -> None:
        runner = RecordingRunner([0, 0, 0, 0])
        code, output = run_gate(["--all", "tests/test_gate.py"], runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.commands[3][1:], ("-x", "tests/test_gate.py"))
        self.assertRegex(
            output.splitlines()[0],
            r"^gate: green in \d+\.\d+s \(environment, ruff, mypy, pytest; all cases; unmasked\)$",
        )

    def test_red_tool_stops_the_gate_with_its_exit_code(self) -> None:
        runner = RecordingRunner([0, 0, 2, 0])
        code, output = run_gate([], runner)
        self.assertEqual(code, 2)
        self.assertEqual(len(runner.commands), 3)
        lines = output.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^gate: red at mypy \(exit 2\) after \d+\.\d+s \(unmasked\)$")

    def test_present_paths_without_flag_observe_only_and_stay_unmasked(self) -> None:
        present = ("/etc/gideon", "/data")
        fake = fake_mask(mask.ProbeState.READY, present)
        runner = RecordingRunner([0, 0, 0, 0])
        code, output = run_gate([], runner, fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.observe_calls, 1)
        self.assertEqual(fake.probe_commands, [])
        self.assertEqual(fake.wrap_calls, [])
        self.assertEqual(runner.commands[1][1:], ("check", "."))
        self.assertEqual(runner.commands[2][1:], ("gideon", "tests", "tools"))
        self.assertEqual(runner.commands[3][1:], ("-x", "-m", "not slow"))
        self.assertIn("; unmasked)", output)

    def test_masked_wraps_pytest_alone_and_trials_before_each_phase(self) -> None:
        present = ("/etc/gideon", "/data")
        observed = observation(mask.ProbeState.READY, present)
        fake = fake_mask(
            mask.ProbeState.READY,
            present,
            (probe_result(mask.ProbeState.READY, observed),) * 2,
        )
        runner = RecordingRunner([0, 0, 0, 0])
        code, output = run_gate(["--masked", "tests/test_gate.py"], runner, fake)
        self.assertEqual(code, 0)
        self.assertIn("; masked)", output)
        self.assertEqual(len(fake.probe_commands), 2)
        self.assertEqual(fake.probe_commands[0], fake.probe_commands[1])
        self.assertEqual(fake.wrap_calls, [(fake.probe_commands[1], present)])
        self.assertEqual(runner.commands[1][1:], ("check", "."))
        self.assertEqual(runner.commands[2][1:], ("gideon", "tests", "tools"))
        self.assertEqual(
            runner.commands[3],
            ("wrapped", *fake.probe_commands[1]),
        )

    def test_refusing_probe_runs_no_tool_and_reports_mask(self) -> None:
        observed = observation(mask.ProbeState.READY, ("/data",))
        refusal = mask.Refusal("the probe refused", "repair the mask")
        fake = fake_mask(
            mask.ProbeState.READY,
            ("/data",),
            (probe_result(mask.ProbeState.REFUSED, observed, refusal),),
        )
        runner = RecordingRunner([0])
        code, output, error = run_gate_with_stderr(["--masked"], runner, fake)
        self.assertEqual(code, 1)
        self.assertEqual(len(runner.commands), 1)
        self.assertEqual(len(fake.probe_commands), 1)
        self.assertEqual(refusal.problem, "the probe refused")
        self.assertEqual(refusal.fix, "repair the mask")
        self.assertEqual(error, "mask: the probe refused. Fix: repair the mask\n")
        self.assertRegex(output, r"^gate: red at mask \(exit 1\) after \d+\.\d+s \(mask refused\)$")

    def test_second_trial_refusal_runs_no_pytest(self) -> None:
        observed = observation(mask.ProbeState.READY, ("/data",))
        refusal = mask.Refusal("the credential expired", "log in again")
        fake = fake_mask(
            mask.ProbeState.READY,
            ("/data",),
            (
                probe_result(mask.ProbeState.READY, observed),
                probe_result(mask.ProbeState.REFUSED, observed, refusal),
            ),
        )
        runner = RecordingRunner([0, 0, 0])
        code, output, error = run_gate_with_stderr(["--masked"], runner, fake)
        self.assertEqual(code, 1)
        self.assertEqual(len(runner.commands), 3)
        self.assertEqual(refusal.problem, "the credential expired")
        self.assertEqual(refusal.fix, "log in again")
        self.assertEqual(error, "mask: the credential expired. Fix: log in again\n")
        self.assertRegex(output, r"^gate: red at mask \(exit 1\) after \d+\.\d+s \(mask refused\)$")

    def test_mask_exit_codes_are_reported_as_mask_failures(self) -> None:
        observed = observation(mask.ProbeState.READY, ("/data",))
        for code in (min(mask.MASK_CODES), max(mask.MASK_CODES)):
            with self.subTest(code=code):
                fake = fake_mask(
                    mask.ProbeState.READY,
                    ("/data",),
                    (probe_result(mask.ProbeState.READY, observed),) * 2,
                )
                runner = RecordingRunner([0, 0, 0, code])
                actual, output = run_gate(["--masked"], runner, fake)
                self.assertEqual(actual, code)
                self.assertRegex(
                    output,
                    rf"^gate: red at mask \(exit {code}\) after \d+\.\d+s \(mask failed\)$",
                )

    def test_wrapped_pytest_exit_one_is_reported_as_pytest(self) -> None:
        observed = observation(mask.ProbeState.READY, ("/data",))
        fake = fake_mask(
            mask.ProbeState.READY,
            ("/data",),
            (probe_result(mask.ProbeState.READY, observed),) * 2,
        )
        runner = RecordingRunner([0, 0, 0, 1])
        code, output = run_gate(["--masked"], runner, fake)
        self.assertEqual(code, 1)
        self.assertRegex(output, r"^gate: red at pytest \(exit 1\) after \d+\.\d+s \(masked\)$")

    def test_inside_mask_with_or_without_flag_does_not_probe_or_wrap(self) -> None:
        for argv in ([], ["--masked"]):
            with self.subTest(argv=argv):
                fake = fake_mask(mask.ProbeState.INSIDE, ("/data",))
                runner = RecordingRunner([0, 0, 0, 0])
                code, output = run_gate(argv, runner, fake)
                self.assertEqual(code, 0)
                self.assertEqual(fake.probe_commands, [])
                self.assertEqual(fake.wrap_calls, [])
                self.assertIn("; masked by the caller)", output)

    def test_masked_with_no_paths_says_it_proceeds_unmasked(self) -> None:
        fake = fake_mask(mask.ProbeState.ABSENT)
        runner = RecordingRunner([0, 0, 0, 0])
        code, output = run_gate(["--masked"], runner, fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.probe_commands, [])
        self.assertEqual(fake.wrap_calls, [])
        self.assertIn("; mask asked, no listed path on this host)", output)

    def test_environment_red_stops_before_tools_and_the_first_mask_trial(self) -> None:
        fake = fake_mask(mask.ProbeState.READY, ("/data",))
        runner = RecordingRunner([23])
        code, output, error = run_gate_with_stderr(["--masked"], runner, fake)
        self.assertEqual(code, 23)
        self.assertEqual(len(runner.commands), 1)
        self.assertIn(Path(runner.commands[0][0]).name, {"python", "python3"})
        self.assertEqual(fake.probe_commands, [])
        self.assertEqual(fake.wrap_calls, [])
        self.assertEqual(error, "")
        self.assertRegex(
            output,
            r"^gate: red at environment \(exit 23\) after \d+\.\d+s \(masked\)$",
        )

    def test_environment_command_uses_shared_tool_directory_and_python_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / "venv" / "bin"
            tools.mkdir(parents=True)
            for tool in gate.TOOLS:
                (tools / tool).write_text("", encoding="utf-8")
            (tools / "python").write_text("", encoding="utf-8")
            resolved = {tool: str(tools / tool) for tool in gate.TOOLS}
            with mock.patch.object(
                gate,
                "resolve",
                side_effect=lambda tool, _root: resolved[tool],
            ):
                command = gate.environment_command(root)
        self.assertEqual(
            command,
            (
                str(tools / "python"),
                "-P",
                str(root / "tools" / "environment.py"),
            ),
        )

    def test_environment_resolution_refusals_stop_before_runner_and_mask(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            cases = {
                "two directories": {
                    "ruff": str(first / "ruff"),
                    "mypy": str(second / "mypy"),
                    "pytest": str(second / "pytest"),
                },
                "bare name": {
                    "ruff": "ruff",
                    "mypy": str(first / "mypy"),
                    "pytest": str(first / "pytest"),
                },
                "no interpreter": {
                    tool: str(first / tool) for tool in gate.TOOLS
                },
            }
            for label, resolved in cases.items():
                with self.subTest(label=label):
                    fake = fake_mask(mask.ProbeState.READY, ("/data",))
                    runner = RecordingRunner([])
                    with mock.patch.object(
                        gate,
                        "resolve",
                        side_effect=lambda tool, _root, resolved=resolved: resolved[tool],
                    ):
                        code, output, error = run_gate_with_stderr(
                            ["--masked"], runner, fake
                        )
                    self.assertEqual(code, 1)
                    self.assertEqual(runner.commands, [])
                    self.assertEqual(fake.probe_commands, [])
                    self.assertEqual(fake.wrap_calls, [])
                    self.assertTrue(error.startswith("environment: "))
                    self.assertIn("Fix:", error)
                    self.assertRegex(
                        output,
                        r"^gate: red at environment \(exit 1\) after \d+\.\d+s \(masked\)$",
                    )

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
                    target if target == "tools.mask" else target.partition(".")[0],
                    {*sys.stdlib_module_names, "tools.mask"},
                    f"{path}:{node.lineno} imports {target}",
                )


if __name__ == "__main__":
    unittest.main()
