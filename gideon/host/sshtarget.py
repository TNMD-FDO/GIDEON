"""SSH and remote-check argv helpers for the backup target."""

import hashlib
import shlex
from pathlib import Path
from typing import Final

from gideon.host.site import SiteConfig

BACKUP_KEY: Final = Path("/etc/gideon/secrets/backup_ssh_key")
BACKUP_KNOWN_HOSTS: Final = Path("/etc/gideon/backup_known_hosts")
SSH_TIMEOUT_SECONDS: Final = 10


def _ssh_options() -> list[str]:
    """The identity, host-key, and bound options every target connection uses."""

    return [
        "-i",
        str(BACKUP_KEY),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={BACKUP_KNOWN_HOSTS}",
        "-o",
        f"ConnectTimeout={SSH_TIMEOUT_SECONDS}",
    ]


def ssh_argv(site: SiteConfig, *remote: str) -> list[str]:
    """Build the bounded, non-interactive SSH command for *site*'s target."""

    target = site.backup.target
    return ["ssh", *_ssh_options(), f"{target.user}@{target.host}", *remote]


def rsync_ssh_option() -> str:
    """Return the one shell word rsync's ``-e`` takes: ssh with the same options."""

    return shlex.join(["ssh", *_ssh_options()])


def remote_script(script: str) -> str:
    """Make a ``sh -c`` command safe to pass as one SSH remote word."""

    return f"sh -c {shlex.quote(script)}"


def remote_check_argv(site: SiteConfig, directory: str) -> list[str]:
    """Build the remote checksum verifier, whose list arrives on stdin."""

    script = f"cd {shlex.quote(directory)} && sha256sum -c -"
    return ssh_argv(site, remote_script(script))


def parse_check_output(stdout: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split ``sha256sum -c`` output into verified and failed paths.

    A missing file reports ``FAILED open or read`` rather than ``FAILED``;
    both count as failures.
    """

    okay: list[str] = []
    failed: list[str] = []
    for line in stdout.splitlines():
        if line.endswith(": OK"):
            okay.append(line[: -len(": OK")])
        elif ": FAILED" in line:
            failed.append(line[: line.index(": FAILED")])
    return tuple(okay), tuple(failed)


BACKUP_PROBE_SHA256: Final = hashlib.sha256(b"gideon\n").hexdigest()

