"""Ownership hand-back helpers for root-run repository tooling."""

import os
from pathlib import Path

from gideon.host.sysio import Host


def sudo_ids() -> tuple[int, int] | None:
    uid_text = os.environ.get("SUDO_UID")
    gid_text = os.environ.get("SUDO_GID")
    if uid_text is None or gid_text is None:
        return None
    try:
        return int(uid_text), int(gid_text)
    except ValueError:
        return None


def restore_ownership(
    host: Host, out: Path, owner: tuple[int, int], *, checkout: Path
) -> None:
    """chown --out, its mail directory, the regular files in each, and the
    checkout's bytecode caches with theirs; nothing deeper."""

    uid, gid = owner
    for directory in (out, out / "mail"):
        if not host.exists(directory):
            continue
        host.chown(directory, uid, gid)
        for name in host.listdir(directory):
            path = directory / name
            if path != out / "mail":
                host.chown(path, uid, gid)
    restore_bytecode_ownership(host, owner, checkout=checkout)


def restore_bytecode_ownership(
    host: Host, owner: tuple[int, int], *, checkout: Path
) -> None:
    """Return the checkout's direct bytecode-cache contents to *owner*."""

    uid, gid = owner
    for directory in bytecode_directories(host, checkout):
        if not host.exists(directory):
            continue
        host.chown(directory, uid, gid)
        for name in host.listdir(directory):
            host.chown(directory / name, uid, gid)


def bytecode_directories(host: Host, checkout: Path) -> list[Path]:
    """Every ``__pycache__`` under the checkout's two importable trees.

    Root runs the harness from a CSA's checkout or the runner's workspace, and
    Python caches the package files it imports before the entry point can say
    otherwise. Left root-owned, the caches broke the runner's next checkout
    (git clean cannot unlink them) after the v0.1.0 tag run; they are the run's
    own by-product, so they go back with the transcripts.
    """

    listed = host.run(
        [
            "find",
            str(checkout / "tools"),
            str(checkout / "gideon"),
            "-type",
            "d",
            "-name",
            "__pycache__",
        ]
    )
    if listed.returncode != 0:
        return []
    return [Path(line) for line in listed.stdout.splitlines() if line]
