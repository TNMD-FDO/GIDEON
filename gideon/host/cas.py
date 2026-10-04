"""The content-addressed store: immutable byte objects named by their SHA-256.

An object lives at ``<root>/<aa>/<bb>/<name>``, its name the 64 lowercase
hexadecimal characters of its digest, so the same bytes have one path and are
stored once.  A write is durable before its name is returned and never
replaces an existing object; a read hashes what it read and refuses a
mismatch.  Nothing here deletes, renames, or rewrites an object.

The root belongs to the service group with the setgid bit, so every entry
under it inherits the group; each shard directory and object is given its
mode explicitly, since a process umask would otherwise strip the group's
access.  Provision makes the root; the store never does.
"""

import errno
import hashlib
import re
import stat
from pathlib import Path
from typing import Final

from gideon.host.report import Problem
from gideon.host.sysio import BytesHost, PathLike

ROOT: Final = Path("/data/bulk/cas")
DIRECTORY_MODE: Final = 0o2770
OBJECT_MODE: Final = 0o440

_NAME: Final = re.compile(r"[0-9a-f]{64}")
_PROVISION_FIX: Final = (
    "Run sudo python3 -m gideon host provision --only disk-layout, then retry."
)
_SOURCE_FIX: Final = "Write the object's bytes again from their source."
_CORRUPT_FIX: Final = (
    "Move the object's path aside, then write its bytes again from their source."
)
_IO_FIX: Final = "Free space or repair the store root, then retry."


def object_path(name: str, root: PathLike = ROOT) -> Path:
    """The path of the object *name* under *root*; a malformed name is refused."""

    if _NAME.fullmatch(name) is None:
        # The value is never echoed: a caller's mistake could pass text here.
        raise ValueError(f"an object name of {len(name)} characters is malformed")
    return Path(root) / name[:2] / name[2:4] / name


def put(host: BytesHost, data: bytes, *, root: PathLike = ROOT) -> str | Problem:
    """Store *data* once and return its name, durable before it is returned."""

    root_path = Path(root)
    if problem := _root_problem(host, root_path):
        return problem
    name = hashlib.sha256(data).hexdigest()
    path = object_path(name, root_path)
    try:
        present = host.stat(path)
    except FileNotFoundError:
        if problem := _write(host, path, data):
            return problem
    except OSError as error:
        return _io_problem(error, path)
    else:
        if present.st_size != len(data):
            return Problem(
                f"object {path} differs in size from the bytes that name it",
                _CORRUPT_FIX,
            )
    # Flushed whoever made the object and however far they got, so a recorded
    # name never precedes its object's durability.
    for directory in (path.parent, path.parent.parent, root_path):
        try:
            host.sync_directory(directory)
        except OSError as error:
            return _io_problem(error, directory)
    return name


def get(host: BytesHost, name: str, *, root: PathLike = ROOT) -> bytes | Problem:
    """The bytes of the object *name*, or a refusal when they do not hash to it."""

    if _NAME.fullmatch(name) is None:
        return Problem(
            f"an object name of {len(name)} characters is malformed",
            "Pass the 64 characters put returned.",
        )
    root_path = Path(root)
    if problem := _root_problem(host, root_path):
        return problem
    path = object_path(name, root_path)
    try:
        data = host.read_bytes(path)
    except FileNotFoundError:
        return Problem(f"no object {name} at {path}", _SOURCE_FIX)
    except OSError as error:
        return _io_problem(error, path)
    if hashlib.sha256(data).hexdigest() != name:
        return Problem(f"object {path} does not hash to its name", _CORRUPT_FIX)
    return data


def verify(host: BytesHost, name: str, *, root: PathLike = ROOT) -> Problem | None:
    """Nothing when the object *name* is present and hashes to it, else the refusal."""

    result = get(host, name, root=root)
    return result if isinstance(result, Problem) else None


def _write(host: BytesHost, path: Path, data: bytes) -> Problem | None:
    """Hold both shard directories to the store's mode, then create the object."""

    for directory in (path.parent.parent, path.parent):
        try:
            host.mkdir(directory, mode=DIRECTORY_MODE, exist_ok=True)
            if stat.S_IMODE(host.stat(directory).st_mode) == DIRECTORY_MODE:
                continue
        except OSError as error:
            return _io_problem(error, directory)
        try:
            host.chmod(directory, DIRECTORY_MODE)
        except PermissionError:
            return Problem(
                f"shard directory {directory} is not at mode "
                f"{DIRECTORY_MODE:04o} and this process may not set it",
                f"Run sudo chmod {DIRECTORY_MODE:04o} {directory}, then retry.",
            )
        except OSError as error:
            return _io_problem(error, directory)
    try:
        # A concurrent writer of the same bytes winning the link is success.
        host.create_exclusive(path, data, mode=OBJECT_MODE)
    except OSError as error:
        return _io_problem(error, path)
    return None


def _root_problem(host: BytesHost, root: Path) -> Problem | None:
    try:
        state = host.stat(root)
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return Problem(
            f"store root {root} is absent or closed to this process", _PROVISION_FIX
        )
    except OSError as error:
        return _io_problem(error, root)
    if not stat.S_ISDIR(state.st_mode):
        return Problem(f"store root {root} is not a directory", _PROVISION_FIX)
    return None


def _io_problem(error: OSError, path: Path) -> Problem:
    """The error's kind and path, never its message."""

    kind = type(error).__name__
    if error.errno is not None:
        kind = errno.errorcode.get(error.errno, kind)
    return Problem(f"{kind} at {path}", _IO_FIX)
