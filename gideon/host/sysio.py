"""The injectable system-I/O seam for bare-host operations.

Only this module is allowed to call :mod:`subprocess` or perform filesystem
operations for the host path.  Provisioning code can therefore be exercised
against a recording or replaying fixture without requiring root or a Linux
host.
"""

import contextlib
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


class RealHost:
    """The production implementation of :class:`Host`."""

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
            # No host command is interactive (§1.9, §3.6), so a child never gets
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
