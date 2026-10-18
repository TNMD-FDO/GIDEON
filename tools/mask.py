"""The mask: one command run where the box's own paths read as empty.

``python3 -m tools.mask -- <command>``, or the file by path over any tree,
runs *command* inside a private mount and network namespace in which every
listed path — ``/etc/gideon``, the rendered tree, ``/data``, the journal, the
Docker socket — is an empty read-only mount or the null device, and loopback
is the only interface. It observes before it acts: inside a mask, or on a host
carrying none of the listed paths, the command runs as it stands. ``tools/gate.py``
imports the verbs for its ``--masked`` flag; the export tree's check runs this
file by path, so the tree under test needs to know nothing of it. Standard
library only. The root stages belong to the provision-owned wrapper outside
all checkouts; command words travel on stdin and are read only after the
privilege drop. A masked command receives no stdin of its own.
"""

from __future__ import annotations

import argparse
import enum
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

MASK_PATHS: Final[tuple[str, ...]] = (
    "/etc/gideon",
    "/opt/gideon",
    "/run/gideon",
    "/data",
    "/var/log/journal",
    "/run/log/journal",
    "/run/docker.sock",
    "/var/run/docker.sock",
)
WRAPPER_PATH: Final[str] = "/usr/local/sbin/gideon-mask"

SETUP_FAILURE_CODE: Final = 71
MASK_CODES: Final[frozenset[int]] = frozenset((SETUP_FAILURE_CODE, 125, 126, 127))
REQUIRED_TOOLS: Final[tuple[str, ...]] = (
    "sudo",
    "unshare",
    "setpriv",
    "mount",
    "ip",
    "sh",
)


class ProbeState(enum.Enum):
    """The outcome of observing or probing the mask boundary."""

    INSIDE = "inside"
    ABSENT = "absent"
    READY = "ready"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class Wrapped:
    """The fixed wrapper command and the command words it receives on stdin."""

    argv: tuple[str, ...]
    stdin: str


@dataclass(frozen=True, slots=True)
class Refusal:
    """A mask refusal's problem and its repair, kept separate for rendering."""

    problem: str
    fix: str


@dataclass(frozen=True, slots=True)
class Observation:
    """The listed paths observed by the current process."""

    state: ProbeState
    present: tuple[str, ...]
    masked: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The result of checking whether a command can enter the mask."""

    state: ProbeState
    observation: Observation
    refusal: Refusal | None = None
    trial_code: int | None = None


def _device(path: str) -> int:
    return os.stat(path).st_dev


def _environment() -> Mapping[str, str]:
    return os.environ


def _run(command: Sequence[str], stdin: str | None) -> int:
    try:
        return subprocess.run(list(command), input=stdin, text=True, check=False).returncode
    except OSError:
        return 127


@dataclass(frozen=True, slots=True)
class Facts:
    """Filesystem, process, and command facts used by the mask verbs."""

    exists: Callable[[str], bool] = os.path.exists
    is_directory: Callable[[str], bool] = os.path.isdir
    is_mount: Callable[[str], bool] = os.path.ismount
    list_directory: Callable[[str], Sequence[str]] = os.listdir
    device: Callable[[str], int] = _device
    realpath: Callable[[str], str] = os.path.realpath
    effective_uid: Callable[[], int] = os.geteuid
    environment: Callable[[], Mapping[str, str]] = _environment
    which: Callable[[str], str | None] = shutil.which
    working_directory: Callable[[], str] = os.getcwd
    run: Callable[[Sequence[str], str | None], int] = _run


FACTS: Final = Facts()


def _resolved_paths(facts: Facts) -> tuple[str, ...]:
    resolved: list[str] = []
    seen: set[str] = set()
    for path in MASK_PATHS:
        target = facts.realpath(path)
        if target not in seen:
            seen.add(target)
            resolved.append(target)
    return tuple(resolved)


def _is_masked(path: str, facts: Facts) -> bool:
    try:
        if facts.is_directory(path):
            return facts.is_mount(path) and not facts.list_directory(path)
        return facts.device(path) == facts.device("/dev/null")
    except OSError:
        return False


def observe(facts: Facts = FACTS) -> Observation:
    """Observe whether the current process is already inside the mask."""

    present = tuple(path for path in _resolved_paths(facts) if facts.exists(path))
    masked = tuple(path for path in present if _is_masked(path, facts))
    if not present:
        state = ProbeState.ABSENT
    elif len(masked) == len(present):
        state = ProbeState.INSIDE
    else:
        state = ProbeState.READY
    return Observation(state=state, present=present, masked=masked)


def _under_mask(path: str, facts: Facts) -> bool:
    candidate = facts.realpath(path)
    for listed in _resolved_paths(facts):
        try:
            if os.path.commonpath((candidate, listed)) == listed:
                return True
        except ValueError:
            continue
    return False


def _unsafe_refusal(command: Sequence[str], facts: Facts) -> Refusal | None:
    calling = facts.environment()
    if _under_mask(facts.working_directory(), facts):
        return Refusal(
            "the working directory is under a listed mask path",
            "Run from a checkout outside the listed paths (never /opt/gideon), then re-run.",
        )
    resolved = facts.which(command[0]) if command else None
    if resolved is not None and _under_mask(resolved, facts):
        return Refusal(
            "the command executable is under a listed mask path",
            "Run an executable from a checkout outside the listed paths (never /opt/gideon), then re-run.",
        )
    tmpdir = calling.get("TMPDIR")
    if tmpdir is not None and _under_mask(tmpdir, facts):
        return Refusal(
            "TMPDIR is under a listed mask path",
            "Set TMPDIR outside the listed paths (never /opt/gideon), then re-run.",
        )
    return None


def _missing_tool(facts: Facts) -> Refusal | None:
    for tool in REQUIRED_TOOLS:
        if facts.which(tool) is None:
            return Refusal(
                f"required mask tool is missing: {tool}",
                "Install the Ubuntu packages providing the mask tools (sudo, util-linux, iproute2, and dash), then re-run.",
            )
    return None


def probe(
    command: Sequence[str],
    facts: Facts = FACTS,
    observation: Observation | None = None,
) -> ProbeResult:
    """Probe the namespaces and drop before a command is allowed to run."""

    observed = observation or observe(facts=facts)
    if observed.state in (ProbeState.INSIDE, ProbeState.ABSENT):
        return ProbeResult(state=observed.state, observation=observed)

    if facts.effective_uid() == 0:
        refusal = Refusal(
            "the mask was invoked as root",
            "Run the mask as the invoking user, without sudo; root belongs only inside its namespace.",
        )
        return ProbeResult(ProbeState.REFUSED, observed, refusal)
    missing = _missing_tool(facts)
    if missing is not None:
        return ProbeResult(ProbeState.REFUSED, observed, missing)
    if not facts.exists(WRAPPER_PATH):
        refusal = Refusal(
            f"the mask wrapper {WRAPPER_PATH} is absent",
            "Run sudo python3 -m gideon host provision --only gh-runner from a checkout at main, then re-run.",
        )
        return ProbeResult(ProbeState.REFUSED, observed, refusal)
    if any("\n" in word for word in command):
        refusal = Refusal(
            "a command word contains a newline",
            "Run a command whose words contain no newline, then re-run.",
        )
        return ProbeResult(ProbeState.REFUSED, observed, refusal)
    unsafe = _unsafe_refusal(command, facts)
    if unsafe is not None:
        return ProbeResult(ProbeState.REFUSED, observed, unsafe)

    trial = wrap(("true",), paths=observed.present, facts=facts)
    code = facts.run(trial.argv, trial.stdin)
    if code != 0:
        refusal = Refusal(
            "the mask setup trial failed",
            "Refresh non-interactive sudo with sudo -v, then re-run; if this account has no sudo on a host carrying a listed path, use the hosted checks run.",
        )
        return ProbeResult(ProbeState.REFUSED, observed, refusal, code)
    return ProbeResult(ProbeState.READY, observed, trial_code=code)


def wrap(
    command: Sequence[str],
    paths: Sequence[str] | None = None,
    facts: Facts = FACTS,
) -> Wrapped:
    """Build the fixed wrapper argv and one-word-per-line command stdin.

    Paths and facts retain the caller seam's shape; the provision-owned wrapper
    carries its own paths and reads the invoking identity itself.
    """

    del paths, facts
    return Wrapped(("sudo", "-n", WRAPPER_PATH), "".join(word + "\n" for word in command))


def _state_line(observation: Observation) -> str:
    if observation.state is ProbeState.INSIDE:
        return "mask: already inside a mask"
    if observation.state is ProbeState.ABSENT:
        return "mask: no listed path is on this host"
    return (
        f"mask: {len(observation.present)} listed paths hidden; "
        "network namespace isolated with loopback up"
    )


def _print_refusal(refusal: Refusal) -> None:
    print(f"mask: {refusal.problem}. Fix: {refusal.fix}", file=sys.stderr)


def main(argv: Sequence[str] | None = None, facts: Facts = FACTS) -> int:
    """Observe, probe, wrap, and run one command, returning its exit code."""

    parser = argparse.ArgumentParser(
        prog="python3 -m tools.mask",
        description="run a command with the box's known paths masked",
    )
    parser.add_argument(
        "command",
        nargs="+",
        metavar="COMMAND",
        help="command and arguments; use -- before a command beginning with '-'",
    )
    options = parser.parse_args(argv)
    command = tuple(options.command)
    observed = observe(facts=facts)
    if observed.state in (ProbeState.INSIDE, ProbeState.ABSENT):
        print(_state_line(observed), flush=True)
        return facts.run(command, None)

    result = probe(command, facts=facts, observation=observed)
    if result.refusal is not None:
        _print_refusal(result.refusal)
        return 1
    print(_state_line(observed), flush=True)
    wrapped = wrap(command, paths=observed.present, facts=facts)
    return facts.run(wrapped.argv, wrapped.stdin)


if __name__ == "__main__":
    sys.exit(main())
