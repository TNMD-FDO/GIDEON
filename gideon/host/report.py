"""Operator-facing report and refusal text shared by the host commands."""

import subprocess
from dataclasses import dataclass


def one_line(value: object) -> str:
    """Collapse *value* to a single line so report rows stay one row each."""

    return " ".join(str(value).splitlines())


def refusal(command: str, problem: object, fix: str) -> str:
    """The one refusal shape: the command, what is wrong, and the fix last."""

    return f"gideon {command}: {one_line(problem)} Fix: {fix}"


def command_detail(result: subprocess.CompletedProcess[str]) -> str:
    """What a failed command said, stderr first, for a report row's detail.

    Both streams when both spoke: a Compose one-off puts its own progress on
    stderr, and the tool it ran may have put the real error on stdout.
    """

    stderr = one_line(result.stderr).strip()
    stdout = one_line(result.stdout).strip()
    if stderr and stdout:
        return f"{stderr} | {stdout}"
    return stderr or stdout or "command failed"


@dataclass(frozen=True, slots=True)
class Problem:
    """What is wrong and the command that fixes it, in refusal()'s two halves."""

    problem: str
    fix: str


@dataclass(frozen=True, slots=True)
class StageResult:
    """The operator-facing result of one ordered host-command stage."""

    name: str
    ok: bool
    detail: str
    fix: str


def stage_line(result: StageResult) -> str:
    """One stage row in the shared operator-facing shape: ``name: ok|refuse — detail [Fix: …]``."""

    outcome = "ok" if result.ok else "refuse"
    line = f"{result.name}: {outcome} — {one_line(result.detail)}"
    if result.fix:
        line += f" Fix: {one_line(result.fix)}"
    return line


def print_stage(result: StageResult) -> None:
    """Print one stage row with the shared operator-facing shape."""

    print(stage_line(result))
