"""The mask: one command run where the box's own paths read as empty.

``python3 -m tools.mask -- <command>``, or the file by path over any tree,
runs *command* inside a private mount and network namespace in which every
listed path — ``/etc/gideon``, the rendered tree, ``/data``, the journal, the
Docker socket — is an empty read-only mount or the null device, and loopback
is the only interface. It observes before it acts: inside a mask, or on a host
carrying none of the listed paths, the command runs as it stands. ``tools/gate.py``
imports the verbs for its ``--masked`` flag; the export tree's check runs this
file by path, so the tree under test needs to know nothing of it. Standard
library only.
"""

from __future__ import annotations

import argparse
import enum
import os
import pwd
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
ENVIRONMENT_ALLOWLIST: Final[tuple[str, ...]] = (
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "USER",
    "LOGNAME",
)
ROOT_STAGE: Final[str] = r"""set -u

fail() {
    printf '%s\n' "mask: setup failed at $1" >&2
    exit 71
}

setup() {
    step=$1
    shift
    if "$@" >/dev/null 2>&1; then
        return 0
    fi
    fail "$step"
}

uid=$1
gid=$2
groups=$3
environment_count=$4
path_count=$5
shift 5

home_set=0
path_set=0
lang_set=0
lc_all_set=0
lc_ctype_set=0
term_set=0
tmpdir_set=0
user_set=0
logname_set=0
home=
path=
lang=
lc_all=
lc_ctype=
term=
tmpdir=
user=
logname=

i=0
while [ "$i" -lt "$environment_count" ]; do
    case "$1" in
        HOME=*) home=${1#HOME=}; home_set=1 ;;
        PATH=*) path=${1#PATH=}; path_set=1 ;;
        LANG=*) lang=${1#LANG=}; lang_set=1 ;;
        LC_ALL=*) lc_all=${1#LC_ALL=}; lc_all_set=1 ;;
        LC_CTYPE=*) lc_ctype=${1#LC_CTYPE=}; lc_ctype_set=1 ;;
        TERM=*) term=${1#TERM=}; term_set=1 ;;
        TMPDIR=*) tmpdir=${1#TMPDIR=}; tmpdir_set=1 ;;
        USER=*) user=${1#USER=}; user_set=1 ;;
        LOGNAME=*) logname=${1#LOGNAME=}; logname_set=1 ;;
    esac
    shift
    i=$((i + 1))
done

setup / mount --make-rprivate /
i=0
while [ "$i" -lt "$path_count" ]; do
    if [ -d "$1" ]; then
        setup "$1" mount -t tmpfs -o ro tmpfs "$1"
    else
        setup "$1" mount --bind /dev/null "$1"
    fi
    shift
    i=$((i + 1))
done
setup loopback ip link set lo up

trial_drop() {
    set --
    if [ "$logname_set" -eq 1 ]; then set -- "LOGNAME=$logname" "$@"; fi
    if [ "$user_set" -eq 1 ]; then set -- "USER=$user" "$@"; fi
    if [ "$tmpdir_set" -eq 1 ]; then set -- "TMPDIR=$tmpdir" "$@"; fi
    if [ "$term_set" -eq 1 ]; then set -- "TERM=$term" "$@"; fi
    if [ "$lc_ctype_set" -eq 1 ]; then set -- "LC_CTYPE=$lc_ctype" "$@"; fi
    if [ "$lc_all_set" -eq 1 ]; then set -- "LC_ALL=$lc_all" "$@"; fi
    if [ "$lang_set" -eq 1 ]; then set -- "LANG=$lang" "$@"; fi
    if [ "$path_set" -eq 1 ]; then set -- "PATH=$path" "$@"; fi
    if [ "$home_set" -eq 1 ]; then set -- "HOME=$home" "$@"; fi
    set -- "$@" true
    setpriv --reuid "$uid" --regid "$gid" --groups "$groups" \
        --no-new-privs env -i "$@" >/dev/null 2>&1
}

if ! trial_drop; then
    fail drop
fi

if [ "$logname_set" -eq 1 ]; then set -- "LOGNAME=$logname" "$@"; fi
if [ "$user_set" -eq 1 ]; then set -- "USER=$user" "$@"; fi
if [ "$tmpdir_set" -eq 1 ]; then set -- "TMPDIR=$tmpdir" "$@"; fi
if [ "$term_set" -eq 1 ]; then set -- "TERM=$term" "$@"; fi
if [ "$lc_ctype_set" -eq 1 ]; then set -- "LC_CTYPE=$lc_ctype" "$@"; fi
if [ "$lc_all_set" -eq 1 ]; then set -- "LC_ALL=$lc_all" "$@"; fi
if [ "$lang_set" -eq 1 ]; then set -- "LANG=$lang" "$@"; fi
if [ "$path_set" -eq 1 ]; then set -- "PATH=$path" "$@"; fi
if [ "$home_set" -eq 1 ]; then set -- "HOME=$home" "$@"; fi
exec setpriv --reuid "$uid" --regid "$gid" --groups "$groups" \
    --no-new-privs env -i "$@"
"""

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


def _run(command: Sequence[str]) -> int:
    try:
        return subprocess.run(list(command), check=False).returncode
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
    effective_gid: Callable[[], int] = os.getegid
    supplementary_groups: Callable[[], Sequence[int]] = os.getgroups
    password_entry: Callable[[int], pwd.struct_passwd] = pwd.getpwuid
    environment: Callable[[], Mapping[str, str]] = _environment
    which: Callable[[str], str | None] = shutil.which
    working_directory: Callable[[], str] = os.getcwd
    run: Callable[[Sequence[str]], int] = _run


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


def _environment_values(facts: Facts) -> tuple[str, ...]:
    calling = facts.environment()
    entry = facts.password_entry(facts.effective_uid())
    fallback = {
        "HOME": entry.pw_dir,
        "USER": entry.pw_name,
        "LOGNAME": entry.pw_name,
    }
    return tuple(
        f"{name}={calling[name] if name in calling else fallback[name]}"
        for name in ENVIRONMENT_ALLOWLIST
        if name in calling or name in fallback
    )


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
    unsafe = _unsafe_refusal(command, facts)
    if unsafe is not None:
        return ProbeResult(ProbeState.REFUSED, observed, unsafe)

    trial = wrap(("true",), paths=observed.present, facts=facts)
    code = facts.run(trial)
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
) -> tuple[str, ...]:
    """Build the one argv that runs *command* inside the private mask."""

    if paths is None:
        paths = observe(facts).present
    uid = facts.effective_uid()
    gid = facts.effective_gid()
    groups = ",".join(str(group) for group in facts.supplementary_groups())
    environment = _environment_values(facts)
    return (
        "sudo",
        "-n",
        "unshare",
        "--mount",
        "--net",
        "--",
        "sh",
        "-c",
        ROOT_STAGE,
        "mask",
        str(uid),
        str(gid),
        groups,
        str(len(environment)),
        str(len(paths)),
        *environment,
        *paths,
        *command,
    )


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
        return facts.run(command)

    result = probe(command, facts=facts, observation=observed)
    if result.refusal is not None:
        _print_refusal(result.refusal)
        return 1
    print(_state_line(observed), flush=True)
    return facts.run(wrap(command, paths=observed.present, facts=facts))


if __name__ == "__main__":
    sys.exit(main())
