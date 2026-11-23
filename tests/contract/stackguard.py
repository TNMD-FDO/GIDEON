"""Serialize the throwaway contract stacks and clear a dead run's leftover.

The two contract modules bring up Compose projects that publish the same
loopback port. Each takes one exclusive ``flock`` on a file under
``/run/lock`` before its first ``up`` and holds it through its teardown; the
runner's user and a hand run under ``sudo`` share it, since the file is
opened read-only. The kernel drops the lock when the last process holding
its descriptor exits, and every Compose child of a stack inherits that
descriptor, so a held lock is a live run and a free one means any project
still standing is a dead run's. Holding the lock, the guard takes down both
projects by the names their compose files declare and says which it cleared.

Run by hand from a checkout's root, ``sudo python3 -B
tests/contract/stackguard.py`` takes the same lock and runs the same clear.
"""

import fcntl
import os
import shlex
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, TextIO

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = (
    ROOT / "tests/contract/compose.yaml",
    ROOT / "tests/contract/sentinel/compose.yaml",
)
LOCK_PATH = Path("/run/lock/gideon-ci-contract.lock")
LOCK_TABLE = Path("/proc/locks")
DEFAULT_WAIT = 900
POLL_INTERVAL = 5
WAIT_VARIABLE = "GIDEON_CONTRACT_LOCK_WAIT"
PROJECT_VARIABLE = "COMPOSE_PROJECT_NAME"
HAND_COMMAND = "sudo python3 -B tests/contract/stackguard.py"

# A flock belongs to its open file description, so a second take in the
# holding process would wait on itself; the held paths answer it at once.
_held_paths: set[Path] = set()


def _one_line(text: str) -> str:
    return " ".join(text.splitlines()).strip()


class StackGuardRefusal(Exception):
    """A guard failure with the action that lets the caller retry."""

    def __init__(self, problem: str, fix: str) -> None:
        super().__init__(_one_line(f"{problem} Fix: {fix}"))


class LockHandle:
    """Hold the lock and expose its descriptor for Compose children."""

    def __init__(self, fd: int, path: Path) -> None:
        self.fd = fd
        self._path = path
        self._released = False

    def release(self) -> None:
        """Close this process's descriptor while any child keeps its own."""
        if self._released:
            return
        self._released = True
        try:
            os.close(self.fd)
        finally:
            _held_paths.discard(self._path)


def project_names(*, compose_files: tuple[Path, ...] = COMPOSE_FILES) -> tuple[str, ...]:
    """Read each contract project's name from its Compose file."""
    names: list[str] = []
    for path in compose_files:
        fix = f"Restore a Compose file with a distinct top-level name at {path}."
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise StackGuardRefusal(
                f"Cannot read the Compose project name in {path}: {exc}.", fix,
            ) from exc
        name = document.get("name") if isinstance(document, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise StackGuardRefusal(f"{path} needs a non-empty top-level name string.", fix)
        if name in names:
            raise StackGuardRefusal(f"{path} repeats the Compose project name {name}.", fix)
        names.append(name)
    return tuple(names)


def refuse_project_override() -> None:
    """Refuse a Compose project name the clear would never list.

    The stacks' Compose children inherit the environment, so an override would
    run a stack under a name the next run's clear cannot see.
    """
    value = os.environ.get(PROJECT_VARIABLE)
    if value is not None:
        raise StackGuardRefusal(
            f"{PROJECT_VARIABLE} is set to {value!r}; the contract stacks run under their "
            "compose files' names so the next run can clear a dead run's leftover.",
            f"Unset {PROJECT_VARIABLE} and run again; the contract lock already "
            "keeps two runs apart.",
        )


def lock_wait() -> int:
    """Read the lock wait bound in whole seconds."""
    value = os.environ.get(WAIT_VARIABLE)
    if value is None:
        return DEFAULT_WAIT
    try:
        seconds = int(value)
    except ValueError:
        pass
    else:
        if seconds >= 0:
            return seconds
    raise StackGuardRefusal(
        f"Invalid {WAIT_VARIABLE} value {value!r}.",
        f"Set {WAIT_VARIABLE} to a non-negative integer number of seconds or unset it.",
    )


def _open_lock(path: Path) -> tuple[int, os.stat_result]:
    # Non-blocking open also lets fstat reject a FIFO without waiting for a writer.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    quoted_path = shlex.quote(str(path))
    fd = None
    try:
        while True:
            try:
                fd = os.open(path, flags)
                break
            except FileNotFoundError:
                # Sticky-directory protections require a plain open for existing files.
                try:
                    fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o644)
                except FileExistsError:
                    continue
                os.fchmod(fd, 0o644)
                break
        info = os.fstat(fd)
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        raise StackGuardRefusal(
            f"Cannot open contract lock {path}: {exc}.",
            f"Restore access to the regular lock file with sudo chmod 644 {quoted_path}.",
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise StackGuardRefusal(
            f"Contract lock {path} is not a regular file.",
            f"Restore a regular lock file at {path} with mode 0644.",
        )
    return fd, info


def _holder_pid(info: os.stat_result, table_path: Path) -> int | None:
    identity = os.major(info.st_dev), os.minor(info.st_dev), info.st_ino
    try:
        table = table_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in table.splitlines():
        fields = line.split()
        if len(fields) != 8 or fields[1] != "FLOCK":
            continue
        device = fields[5].split(":")
        if len(device) != 3:
            continue
        try:
            major, minor, inode = int(device[0], 16), int(device[1], 16), int(device[2], 10)
            pid = int(fields[4], 10)
        except ValueError:
            continue
        if (major, minor, inode) == identity and pid > 0:
            return pid
    return None


def _busy_lock(path: Path, info: os.stat_result, table_path: Path) -> tuple[str, str]:
    problem = f"Contract lock {path} is busy"
    inspect = f"Run sudo fuser -v {shlex.quote(str(path))} to identify what still has it open."
    pid = _holder_pid(info, table_path)
    if pid is None:
        return f"{problem} and its holder could not be identified", inspect

    process = Path("/proc") / str(pid)
    try:
        cmdline = (process / "cmdline").read_bytes().decode(errors="replace")
    except OSError:
        try:
            process.stat()
        except FileNotFoundError:
            return (
                f"{problem}. The process that took it (pid {pid}) has exited "
                "and something it started still holds it",
                inspect,
            )
        except OSError:
            pass
        cmdline = ""
    cmdline = _one_line(cmdline.replace("\0", " "))
    holder = f"pid {pid} ({cmdline})" if cmdline else f"pid {pid}"
    fix = f"Let pid {pid} finish or stop it if it hangs, then run the module again."
    return f"{problem} and is held by {holder}", fix


def _take_lock(
    path: Path,
    seconds: int,
    *,
    table_path: Path,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    stream: TextIO,
) -> LockHandle:
    fd, info = _open_lock(path)
    try:
        started = clock()
        waiting_reported = False
        while True:
            at_deadline = clock() - started >= seconds
            if path in _held_paths:
                raise StackGuardRefusal(
                    f"This process already holds contract lock {path}.",
                    "Release the existing handle before taking the lock again.",
                )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            except OSError as exc:
                raise StackGuardRefusal(
                    f"Cannot take contract lock {path}: {exc}.",
                    f"Restore flock support for the filesystem holding {path}, then retry.",
                ) from exc
            else:
                _held_paths.add(path)
                return LockHandle(fd, path)

            if at_deadline or not waiting_reported:
                problem, fix = _busy_lock(path, info, table_path)
                if not waiting_reported:
                    line = f"{problem}. Waiting up to {seconds} seconds. {fix}"
                    print(_one_line(line), file=stream, flush=True)
                    waiting_reported = True
                if at_deadline:
                    raise StackGuardRefusal(f"{problem}. Waited {seconds} seconds.", fix)
            elapsed = clock() - started
            if elapsed + POLL_INTERVAL <= seconds:
                sleep(POLL_INTERVAL)
            elif elapsed < seconds:
                sleep(seconds - elapsed)
    except BaseException:
        os.close(fd)
        raise


class Runner(Protocol):
    """Run a Docker argument vector, optionally with the lock descriptor."""

    def __call__(
        self, argv: list[str], /, *, pass_fds: tuple[int, ...] = ()
    ) -> subprocess.CompletedProcess[str]: ...


def _run(argv: list[str], *, pass_fds: tuple[int, ...] = ()) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd="/", capture_output=True, text=True, pass_fds=pass_fds)


def _docker_detail(result: subprocess.CompletedProcess[str]) -> str:
    return " | ".join(text.strip() for text in (result.stderr, result.stdout) if text and text.strip())


def _checked_run(
    runner: Runner, argv: list[str], problem: str, fix: str, *, fd: int | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = runner(argv) if fd is None else runner(argv, pass_fds=(fd,))
    except OSError as exc:
        raise StackGuardRefusal(f"{problem}: {exc}.", fix) from exc
    if result.returncode != 0:
        detail = _docker_detail(result) or f"Docker exited {result.returncode}"
        raise StackGuardRefusal(f"{problem}: {detail}.", fix)
    return result


def _list_resources(name: str, runner: Runner) -> list[str]:
    label = f"label=com.docker.compose.project={name}"
    listings = (
        ("containers", ["docker", "ps", "-a", "-q", "--filter", label]),
        ("networks", ["docker", "network", "ls", "-q", "--filter", label]),
        ("volumes", ["docker", "volume", "ls", "-q", "--filter", label]),
    )
    found = []
    fix = "Restore Docker access and use sudo for a hand run."
    for kind, argv in listings:
        problem = f"Cannot list Docker {kind} for Compose project {name}"
        result = _checked_run(runner, argv, problem, fix)
        if result.stdout.strip():
            found.append(kind)
    return found


def _clear_projects(names: tuple[str, ...], handle: LockHandle, runner: Runner) -> list[str]:
    cleared = []
    fix = f"Resolve the Docker problem, then run {HAND_COMMAND} from the checkout root."
    for name in names:
        if not _list_resources(name, runner):
            continue
        argv = ["docker", "compose", "-p", name, "down", "-v", "--remove-orphans"]
        result = _checked_run(runner, argv, f"Cannot clear Compose project {name}", fix, fd=handle.fd)
        remaining = _list_resources(name, runner)
        if remaining:
            problem = f"Compose project {name} still has Docker {', '.join(remaining)} after cleanup."
            detail = _docker_detail(result)
            raise StackGuardRefusal(f"{problem} {detail}" if detail else problem, fix)
        cleared.append(name)
    return cleared


def guard(
    *,
    runner: Runner = _run,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    lock_path: Path = LOCK_PATH,
    table_path: Path = LOCK_TABLE,
    stream: TextIO | None = None,
) -> LockHandle:
    """Take the lock, clear the two projects, and return the held lock."""
    if stream is None:
        stream = sys.stderr
    path = Path(os.path.abspath(lock_path))
    refuse_project_override()
    seconds = lock_wait()
    handle = _take_lock(
        path,
        seconds,
        table_path=table_path,
        clock=clock,
        sleep=sleep,
        stream=stream,
    )
    try:
        cleared = _clear_projects(project_names(), handle, runner)
        outcome = f"cleared {', '.join(cleared)}" if cleared else "neither project was present"
        print(_one_line(f"Contract lock {path} taken. {outcome}."), file=stream, flush=True)
    except BaseException:
        handle.release()
        raise
    return handle


def main(
    *,
    runner: Runner = _run,
    lock_path: Path = LOCK_PATH,
    stream: TextIO | None = None,
) -> int:
    """Run the guarded clear by hand and report its result on standard error."""
    if stream is None:
        stream = sys.stderr
    try:
        handle = guard(runner=runner, lock_path=lock_path, stream=stream)
    except StackGuardRefusal as exc:
        print(exc, file=stream, flush=True)
        return 1
    handle.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
