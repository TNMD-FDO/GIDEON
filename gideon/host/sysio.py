"""The injectable system-I/O seam for bare-host operations.

Only this module is allowed to call :mod:`subprocess` or perform filesystem
operations for the host path.  Provisioning code can therefore be exercised
against a recording or replaying fixture without requiring root or a Linux
host.  The seam also owns the one advisory lock shared by the ordered backup
and restore commands.
"""

import contextlib
import fcntl
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

type PathLike = str | os.PathLike[str]
type Command = Sequence[str]
type CompletedText = subprocess.CompletedProcess[str]


class Host(Protocol):
    """The system operations used by the bare-host implementation."""

    def run(
        self,
        argv: Command,
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> CompletedText: ...

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str: ...

    def write_text(
        self,
        path: PathLike,
        text: str,
        *,
        encoding: str = "utf-8",
        mode: int = 0o644,
    ) -> None: ...

    def exists(self, path: PathLike) -> bool: ...

    def listdir(self, path: PathLike) -> list[str]: ...

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None: ...

    def stat(self, path: PathLike) -> os.stat_result: ...

    def chmod(self, path: PathLike, mode: int) -> None: ...

    def chown(self, path: PathLike, uid: int, gid: int) -> None: ...

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None: ...

    def geteuid(self) -> int: ...


class LockingHost(Host, Protocol):
    """The host operations plus the shared advisory lock pair."""

    def take_lock(self, path: PathLike, record: str) -> str | None:
        """Return ``None`` when *path* is taken, else the holder's record."""

    def release_lock(self, path: PathLike) -> None:
        """Release *path* by closing its descriptor; never unlink the file."""


class RealHost:
    """The production implementation of :class:`Host` and :class:`LockingHost`."""

    def __init__(self) -> None:
        self._lock_descriptors: dict[str, int] = {}

    def run(
        self,
        argv: Command,
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> CompletedText:
        if passthrough:
            # The child writes to the same descriptors; anything this process has
            # buffered must land first or the rows interleave out of order.
            sys.stdout.flush()
            sys.stderr.flush()
        try:
            # No host command is interactive, so a child never gets
            # the terminal as stdin: with no tty on any descriptor a nested sudo
            # allocates no pty, and a docker compose exec reads nothing — the two
            # that, together, stopped an upgrade's readiness probe with SIGTTIN.
            completed = subprocess.run(
                list(argv),
                check=check,
                input=input,
                stdin=subprocess.DEVNULL if input is None else None,
                capture_output=not passthrough,
                cwd=cwd,
                env=env,
                shell=False,
                text=True,
                timeout=timeout,
            )
            if passthrough:
                return subprocess.CompletedProcess(
                    completed.args, completed.returncode, "", ""
                )
            return completed
        except FileNotFoundError:
            # An absent binary is a normal pre-converge state (docker, nvidia-ctk
            # before their steps run); degrade to the shell's 127, never a crash.
            result: CompletedText = subprocess.CompletedProcess(
                list(argv), 127, "", f"{argv[0]}: command not found"
            )
            if check:
                raise subprocess.CalledProcessError(
                    result.returncode, list(argv), result.stdout, result.stderr
                ) from None
            return result

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        return Path(path).read_text(encoding=encoding)

    def write_text(
        self,
        path: PathLike,
        text: str,
        *,
        encoding: str = "utf-8",
        mode: int = 0o644,
    ) -> None:
        """Write *text* through a same-directory temporary and rename."""

        target = Path(path)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, mode)
            with os.fdopen(descriptor, "w", encoding=encoding) as handle:
                descriptor = -1
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except BaseException:
            if descriptor != -1:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
            raise

    def exists(self, path: PathLike) -> bool:
        return Path(path).exists()

    def listdir(self, path: PathLike) -> list[str]:
        return os.listdir(os.fspath(path))

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        Path(path).unlink(missing_ok=missing_ok)

    def stat(self, path: PathLike) -> os.stat_result:
        return os.stat(path)

    def chmod(self, path: PathLike, mode: int) -> None:
        os.chmod(path, mode)

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        os.chown(path, uid, gid)

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        Path(path).mkdir(mode=mode, parents=parents, exist_ok=exist_ok)

    def geteuid(self) -> int:
        return os.geteuid()

    def take_lock(self, path: PathLike, record: str) -> str | None:
        # Root's alone: any process that can open the file can take the flock.
        # os.open's descriptor is non-inheritable, so no child outlives the
        # holder with the lock.
        key = os.fspath(path)
        descriptor = os.open(key, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                return _read_descriptor(descriptor)
            finally:
                os.close(descriptor)
        try:
            os.ftruncate(descriptor, 0)
            os.write(descriptor, record.encode())
        except BaseException:
            os.close(descriptor)
            raise
        self._lock_descriptors[key] = descriptor
        return None

    def release_lock(self, path: PathLike) -> None:
        descriptor = self._lock_descriptors.pop(os.fspath(path), None)
        if descriptor is not None:
            os.close(descriptor)


def _read_descriptor(descriptor: int) -> str:
    """The text behind *descriptor* from offset zero; empty when unreadable."""

    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 4096):
            chunks.append(chunk)
        return b"".join(chunks).decode()
    except (OSError, UnicodeDecodeError):
        return ""
