"""The one gate: environment, lint, types, then tests, stopping at the first failure.

``python3 -m tools.gate [--all] [--masked] [TEST_PATH ...]`` first compares the
running environment with ``requirements-dev.txt`` through
``tools/environment.py``, then runs ``ruff check .``, ``mypy gideon tests tools``,
and ``pytest -x`` over the test paths given (the whole suite when none), prints
one summary line, and exits with the first failing child's code. Cases marked
``slow`` are skipped unless ``--all`` is given, which CI's step does. Each tool
is looked up beside the running interpreter, then under the checkout's
``.venv/bin``, then on ``PATH``, so the same command serves CI, the dev seat,
and a worktree whose ``.venv`` is a link. The gate uses the standard library
and the sibling ``tools.mask`` only.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import tools.mask

ROOT = Path(__file__).resolve().parent.parent
TOOLS: tuple[str, ...] = ("ruff", "mypy", "pytest")
ARGUMENTS: dict[str, tuple[str, ...]] = {
    "ruff": ("check", "."),
    "mypy": ("gideon", "tests", "tools"),
    "pytest": ("-x",),
}
SLOW_MARK = "slow"
SKIP_SLOW: tuple[str, ...] = ("-m", f"not {SLOW_MARK}")
Runner = Callable[[Sequence[str]], int]


@dataclass(frozen=True, slots=True)
class Mask:
    """The three mask verbs the gate uses, as the gate calls them."""

    observe: Callable[[], tools.mask.Observation]
    probe: Callable[[Sequence[str], tools.mask.Observation], tools.mask.ProbeResult]
    wrap: Callable[[Sequence[str], Sequence[str]], Sequence[str]]


def _probe(
    command: Sequence[str], observation: tools.mask.Observation
) -> tools.mask.ProbeResult:
    return tools.mask.probe(command, observation=observation)


def _wrap(command: Sequence[str], paths: Sequence[str]) -> Sequence[str]:
    return tools.mask.wrap(command, paths)


REAL_MASK = Mask(tools.mask.observe, _probe, _wrap)


ONE_ENVIRONMENT_FIX = "Install the pinned gate tools into one environment, then re-run the gate."


@dataclass(frozen=True, slots=True)
class EnvironmentRefusal(Exception):
    """A gate refusal while resolving the comparison's interpreter."""

    problem: str
    fix: str


def resolve(tool: str, root: Path = ROOT) -> str:
    """Return the tool's executable: the interpreter's, the venv's, or PATH's."""

    for candidate in (Path(sys.executable).parent / tool, root / ".venv" / "bin" / tool):
        if candidate.is_file():
            return str(candidate)
    return shutil.which(tool) or tool


def environment_command(root: Path = ROOT) -> tuple[str, ...]:
    """Return the comparison command, refusing a split or incomplete tool set."""

    paths = tuple(Path(resolve(tool, root)) for tool in TOOLS)
    for tool, path in zip(TOOLS, paths, strict=True):
        if not path.is_absolute():
            raise EnvironmentRefusal(
                f"{tool} resolved to the bare name {path}, so its environment is unknown",
                ONE_ENVIRONMENT_FIX,
            )
    parents = {path.parent for path in paths}
    if len(parents) != 1:
        directories = ", ".join(str(path) for path in sorted(parents, key=str))
        raise EnvironmentRefusal(
            f"the gate tools resolve to more than one directory: {directories}",
            ONE_ENVIRONMENT_FIX,
        )
    directory = paths[0].parent
    interpreter = directory / "python"
    if not interpreter.is_file():
        interpreter = directory / "python3"
    if not interpreter.is_file():
        raise EnvironmentRefusal(
            f"no python or python3 interpreter is beside the gate tools in {directory}",
            ONE_ENVIRONMENT_FIX,
        )
    return (str(interpreter), "-P", str(root / "tools" / "environment.py"))


def commands(
    test_paths: Sequence[str],
    *,
    everything: bool = False,
    root: Path = ROOT,
    mask: Mask | None = None,
    paths: Sequence[str] = (),
) -> tuple[tuple[str, ...], ...]:
    """Return the three commands, optionally wrapping only pytest."""

    result: list[tuple[str, ...]] = []
    for tool in TOOLS:
        arguments = ARGUMENTS[tool]
        if tool == "pytest":
            marker = () if everything else SKIP_SLOW
            arguments = (*arguments, *marker, *test_paths)
        command = (resolve(tool, root), *arguments)
        if tool == "pytest" and mask is not None:
            command = tuple(mask.wrap(command, paths))
        result.append(command)
    return tuple(result)


def _run(command: Sequence[str]) -> int:
    return subprocess.run(list(command), cwd=ROOT, check=False).returncode


def _state(masked: bool, observation: tools.mask.Observation) -> str:
    if observation.state is tools.mask.ProbeState.INSIDE:
        return "masked by the caller"
    if not masked:
        return "unmasked"
    if observation.state is tools.mask.ProbeState.ABSENT:
        return "mask asked, no listed path on this host"
    return "masked"


def _refuse(trial: tools.mask.ProbeResult, started: float) -> int:
    """Print the mask's refusal, then the one red summary line, and return 1."""

    refusal = trial.refusal
    if refusal is None:
        raise RuntimeError("the mask refused without naming a problem")
    print(f"mask: {refusal.problem}. Fix: {refusal.fix}", file=sys.stderr)
    elapsed = time.monotonic() - started
    print(f"gate: red at mask (exit 1) after {elapsed:.1f}s (mask refused)", flush=True)
    return 1


def _environment_refuse(refusal: EnvironmentRefusal, started: float, state: str) -> int:
    """Print the gate-owned environment refusal and its red summary."""

    print(f"environment: {refusal.problem}. Fix: {refusal.fix}", file=sys.stderr)
    elapsed = time.monotonic() - started
    print(f"gate: red at environment (exit 1) after {elapsed:.1f}s ({state})", flush=True)
    return 1


def main(
    argv: Sequence[str] | None = None,
    runner: Runner = _run,
    mask: Mask = REAL_MASK,
) -> int:
    """Run the gate and return the first failing tool's exit code, else 0."""

    parser = argparse.ArgumentParser(
        prog="python3 -m tools.gate",
        description="environment, ruff, mypy, then pytest, stopping at the first failure.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=f"run the cases marked {SLOW_MARK} too (CI's form)",
    )
    parser.add_argument(
        "--masked",
        action="store_true",
        help="use the mask when listed host paths are present (the leaf's rule)",
    )
    parser.add_argument(
        "test_paths",
        nargs="*",
        metavar="TEST_PATH",
        help="test files or directories for pytest (default: the whole suite)",
    )
    options = parser.parse_args(argv)
    started = time.monotonic()
    observation = mask.observe()
    state = _state(options.masked, observation)
    try:
        comparison = environment_command()
    except EnvironmentRefusal as refusal:
        return _environment_refuse(refusal, started, state)
    code = runner(comparison)
    if code != 0:
        elapsed = time.monotonic() - started
        print(f"gate: red at environment (exit {code}) after {elapsed:.1f}s ({state})", flush=True)
        return code

    plan = commands(options.test_paths, everything=options.all)
    should_wrap = options.masked and observation.state is tools.mask.ProbeState.READY

    if should_wrap:
        trial = mask.probe(plan[2], observation)
        if trial.state is tools.mask.ProbeState.REFUSED:
            return _refuse(trial, started)

    for tool, command in zip(TOOLS[:2], plan[:2], strict=True):
        code = runner(command)
        if code != 0:
            elapsed = time.monotonic() - started
            print(f"gate: red at {tool} (exit {code}) after {elapsed:.1f}s ({state})", flush=True)
            return code

    if should_wrap:
        trial = mask.probe(plan[2], observation)
        if trial.state is tools.mask.ProbeState.REFUSED:
            return _refuse(trial, started)
        plan = commands(
            options.test_paths,
            everything=options.all,
            mask=mask,
            paths=observation.present,
        )

    code = runner(plan[2])
    if code != 0:
        elapsed = time.monotonic() - started
        tool = "mask" if should_wrap and code in tools.mask.MASK_CODES else "pytest"
        failure_state = "mask failed" if tool == "mask" else state
        print(f"gate: red at {tool} (exit {code}) after {elapsed:.1f}s ({failure_state})", flush=True)
        return code

    elapsed = time.monotonic() - started
    scope = "all cases" if options.all else f"{SLOW_MARK} cases skipped, --all runs them"
    print(
        f"gate: green in {elapsed:.1f}s (environment, {', '.join(TOOLS)}; {scope}; {state})",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
