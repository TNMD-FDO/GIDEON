"""The one gate: lint, types, then tests, stopping at the first failure.

``python3 -m tools.gate [--all] [TEST_PATH ...]`` runs ``ruff check .``,
then ``mypy gideon tests tools``, then ``pytest -x`` over the test paths
given (the whole suite when none), prints one summary line, and exits with
the first failing tool's code. Cases marked ``slow`` are skipped unless
``--all`` is given, which CI's step does. Each tool is looked up beside the
running interpreter, then under the checkout's ``.venv/bin``, then on
``PATH``, so the same command serves CI, the dev seat, and a worktree whose
``.venv`` is a link. Standard library only.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

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


def resolve(tool: str, root: Path = ROOT) -> str:
    """Return the tool's executable: the interpreter's, the venv's, or PATH's."""

    for candidate in (Path(sys.executable).parent / tool, root / ".venv" / "bin" / tool):
        if candidate.is_file():
            return str(candidate)
    return shutil.which(tool) or tool


def commands(
    test_paths: Sequence[str], *, everything: bool = False, root: Path = ROOT
) -> tuple[tuple[str, ...], ...]:
    """Return the three commands in gate order, the test paths on pytest's."""

    result: list[tuple[str, ...]] = []
    for tool in TOOLS:
        arguments = ARGUMENTS[tool]
        if tool == "pytest":
            marker = () if everything else SKIP_SLOW
            arguments = (*arguments, *marker, *test_paths)
        result.append((resolve(tool, root), *arguments))
    return tuple(result)


def _run(command: Sequence[str]) -> int:
    return subprocess.run(list(command), cwd=ROOT, check=False).returncode


def main(argv: Sequence[str] | None = None, runner: Runner = _run) -> int:
    """Run the gate and return the first failing tool's exit code, else 0."""

    parser = argparse.ArgumentParser(
        prog="python3 -m tools.gate",
        description="ruff, mypy, then pytest, stopping at the first failure.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=f"run the cases marked {SLOW_MARK} too (CI's form)",
    )
    parser.add_argument(
        "test_paths",
        nargs="*",
        metavar="TEST_PATH",
        help="test files or directories for pytest (default: the whole suite)",
    )
    options = parser.parse_args(argv)
    started = time.monotonic()
    plan = commands(options.test_paths, everything=options.all)
    for tool, command in zip(TOOLS, plan, strict=True):
        code = runner(command)
        if code != 0:
            elapsed = time.monotonic() - started
            print(f"gate: red at {tool} (exit {code}) after {elapsed:.1f}s", flush=True)
            return code
    elapsed = time.monotonic() - started
    scope = "all cases" if options.all else f"{SLOW_MARK} cases skipped, --all runs them"
    print(f"gate: green in {elapsed:.1f}s ({', '.join(TOOLS)}; {scope})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
