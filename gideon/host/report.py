"""Operator-facing report and refusal text shared by the host commands."""

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

GIDEON_INSTALLED_COMMAND = "GIDEON_INSTALLED_COMMAND"
_installed_form = False


def set_installed_form(installed: bool) -> None:
    """Set the command form used for fixes in this run."""

    global _installed_form
    _installed_form = installed


def set_form_from_environment(environment: Mapping[str, str]) -> None:
    """Read whether the installed command started this run."""

    set_installed_form(GIDEON_INSTALLED_COMMAND in environment)


def command(path: str, *, sudo: bool = True) -> str:
    """Render a command path as the run's command form.

    ``sudo=False`` is for a line whose own words say how to gain root: its
    long form drops ``sudo``.  The installed form needs no ``sudo`` either way.
    """

    if _installed_form:
        return f"gideon {path}"
    prefix = "sudo python3 -m gideon" if sudo else "python3 -m gideon"
    return f"{prefix} {path}"


def command_name(path: str) -> str:
    """Name a command alike in both forms, for a refusal's prefix or a lock's holder label.

    A name says what is running; a fix says what to type, through command().
    """

    return f"gideon {path}"


def one_line(value: object) -> str:
    """Collapse *value* to a single line so report rows stay one row each."""

    return " ".join(str(value).splitlines())


def refusal(command: str, problem: object, fix: str) -> str:
    """The one refusal shape: the command, what is wrong, and the fix last."""

    return f"{command_name(command)}: {one_line(problem)} Fix: {fix}"


def failure_lines(fix: str, problem: str | None = None) -> tuple[str, ...]:
    """Return a report's problem line when present, then its fix line."""

    if problem is None:
        return (f"Fix: {fix}",)
    return (problem, f"Fix: {fix}")


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
class Timeout(Problem):
    """A problem that is a bound's expiry.

    The caller's own time bound ended the request with the answer still
    coming.  It prints as any problem and is typed so a caller can tell the
    bound from a failure.
    """


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
