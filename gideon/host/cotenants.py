"""Read Docker ownership marks and guard shared host changes.

GIDEON knows a co-tenant only as a running container without its ownership
mark; the readers ask Docker for names and Compose project labels and nothing
else, so no environment, mount, or command reaches a row.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from gideon.host.report import command
from gideon.host.sysio import Host

if TYPE_CHECKING:
    from gideon.host.steps import ProvisionContext


# GIDEON's Compose projects use this prefix; its unlabeled registry container
# is named gideon-registry.
OWNERSHIP_PREFIX = "gideon"
ACKNOWLEDGE_DISRUPTION_FLAG = "--acknowledge-disruption"
DOCKER_VERSION_ARGV = ("docker", "--version")
DOCKER_ACTIVE_ARGV = ("systemctl", "is-active", "docker")
_DOCKER_PS_FORMAT = '{{.Names}}\t{{.Label "com.docker.compose.project"}}'
DOCKER_PS_ARGV = (
    "docker",
    "ps",
    "--format",
    _DOCKER_PS_FORMAT,
)

_WINDOW_FIX = (
    "Announce a maintenance window to every project sharing the daemon "
    "(docs/runbooks/install-upgrade.md §9), then re-run {command} "
    f"{ACKNOWLEDGE_DISRUPTION_FLAG}."
)
_NOT_ACTIVE_FIX = (
    "Start Docker so the running containers can be listed, then re-run {command}, "
    "or announce a maintenance window to every project sharing the daemon "
    "(docs/runbooks/install-upgrade.md §9) and re-run {command} "
    f"{ACKNOWLEDGE_DISRUPTION_FLAG}."
)
_UNREADABLE_FIX = (
    "Repair docker ps so the running containers can be listed, then re-run {command}."
)


@dataclass(frozen=True, slots=True)
class ContainerRow:
    """A running container's name and optional Compose project."""

    name: str
    project: str | None


class DaemonState(Enum):
    """How far the running-container read could proceed."""

    ABSENT = "absent"
    NOT_ACTIVE = "not-active"
    UNREADABLE = "unreadable"
    LISTED = "listed"


@dataclass(frozen=True, slots=True)
class ContainerListing:
    """The daemon state, its running rows, and any read diagnostic."""

    state: DaemonState
    rows: tuple[ContainerRow, ...] = ()
    diagnostic: str = ""


def publish_ps_argv(port: int) -> tuple[str, ...]:
    """The read of the running containers publishing one port, in the box-wide read's columns.

    Running alone, since a stopped container publishes nothing.
    """

    return ("docker", "ps", "--filter", f"publish={port}", "--format", _DOCKER_PS_FORMAT)


def network_ps_argv(network: str) -> tuple[str, ...]:
    """The read of one network's running containers, in the box-wide read's columns.

    Running alone, since a stopped container holds no endpoint on the network.
    """

    return ("docker", "ps", "--filter", f"network={network}", "--format", _DOCKER_PS_FORMAT)


def compose_projects_argv() -> tuple[str, ...]:
    """Read every Compose project name from the daemon."""

    return ("docker", "compose", "ls", "--all", "--format", "json")


def parse_projects(stdout: str) -> tuple[str, ...] | None:
    """Return project names, or None when Compose's JSON listing is malformed."""

    try:
        listing = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(listing, list):
        return None
    names: list[str] = []
    for row in listing:
        if not isinstance(row, dict) or not isinstance(row.get("Name"), str) or not row["Name"]:
            return None
        names.append(row["Name"])
    return tuple(names)


def network_ls_argv() -> tuple[str, ...]:
    """Read network names from the daemon."""

    return ("docker", "network", "ls", "--format", "{{.Name}}")


def volume_ls_argv() -> tuple[str, ...]:
    """Read every volume's name and Compose project label, in the container rows' columns.

    Volumes outlive their project's containers, so a project gone from Compose's
    own listing still names its volumes by this label.
    """

    return (
        "docker", "volume", "ls", "--format",
        '{{.Name}}\t{{.Label "com.docker.compose.project"}}',
    )


def parse_names(stdout: str) -> tuple[str, ...]:
    """Return one network name per non-empty output line."""

    return tuple(name for line in stdout.splitlines() if (name := line.strip()))


def parse_rows(stdout: str) -> tuple[ContainerRow, ...]:
    """Return valid name and project rows, skipping malformed Docker lines."""

    rows: list[ContainerRow] = []
    for line in stdout.splitlines():
        name, separator, project = line.partition("\t")
        if separator and name:
            rows.append(ContainerRow(name, project or None))
    return tuple(rows)


def running_containers(host: Host) -> ContainerListing:
    """Read only names and Compose project labels from running containers."""

    version = host.run(DOCKER_VERSION_ARGV)
    if version.returncode != 0:
        return ContainerListing(DaemonState.ABSENT)

    active = host.run(DOCKER_ACTIVE_ARGV)
    if active.returncode != 0 or active.stdout.strip() != "active":
        return ContainerListing(DaemonState.NOT_ACTIVE)

    result = host.run(DOCKER_PS_ARGV)
    if result.returncode != 0:
        diagnostic = (result.stderr or result.stdout).splitlines()
        return ContainerListing(
            DaemonState.UNREADABLE,
            diagnostic=diagnostic[0] if diagnostic else "no diagnostic",
        )

    return ContainerListing(DaemonState.LISTED, parse_rows(result.stdout))


def marked_name(name: str) -> bool:
    """Whether a name carries GIDEON's ownership prefix."""

    return name.startswith(OWNERSHIP_PREFIX)


def is_marked(row: ContainerRow) -> bool:
    """Whether a row carries GIDEON's mark: its project begins with the prefix,
    or, with no project label, its name does."""

    if row.project is not None:
        return marked_name(row.project)
    return marked_name(row.name)


def foreign(rows: Sequence[ContainerRow]) -> dict[str | None, list[str]]:
    """Group unmarked running containers by project in first-seen order."""

    groups: dict[str | None, list[str]] = {}
    for row in rows:
        if is_marked(row):
            continue
        groups.setdefault(row.project, []).append(row.name)
    return groups


def describe(groups: Mapping[str | None, Sequence[str]]) -> str:
    """Render project groups as one operator-facing line."""

    return "; ".join(
        f"{'no Compose project' if project is None else f'project {project}'} "
        f"({', '.join(names)})"
        for project, names in groups.items()
    )


def guard(context: ProvisionContext, mutation: str) -> None:
    """Refuse a shared mutation when running co-tenants have not been acknowledged."""

    if context.disruption_acknowledged:
        return

    # Import after the step contracts have loaded: step modules import this guard.
    from gideon.host.steps import StepFailure

    listing = running_containers(context.host)
    if listing.state is DaemonState.ABSENT:
        return
    provision = command("host provision")
    if listing.state is DaemonState.NOT_ACTIVE:
        raise StepFailure(
            "Docker is installed but its daemon is not active, so running containers "
            "that live-restore may be keeping up cannot be listed",
            _NOT_ACTIVE_FIX.format(command=provision),
        )
    if listing.state is DaemonState.UNREADABLE:
        raise StepFailure(
            f"docker ps could not list running containers: {listing.diagnostic}",
            _UNREADABLE_FIX.format(command=provision),
        )

    groups = foreign(listing.rows)
    if groups:
        raise StepFailure(
            f"{mutation} reaches every container on the box, and containers outside "
            f"GIDEON's ownership mark are running: {describe(groups)}",
            _WINDOW_FIX.format(command=provision),
        )
