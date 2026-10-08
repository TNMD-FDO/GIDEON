"""Refuse a box-wide host change while another application's containers run.

GIDEON knows a co-tenant only as a running container without its ownership
mark; the reader asks Docker for each container's name and Compose project
label and nothing else, so no environment, mount, or command reaches a row.
"""

from __future__ import annotations

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
DOCKER_PS_ARGV = (
    "docker",
    "ps",
    "--format",
    '{{.Names}}\t{{.Label "com.docker.compose.project"}}',
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

    rows: list[ContainerRow] = []
    for line in result.stdout.splitlines():
        name, separator, project = line.partition("\t")
        if separator and name:
            rows.append(ContainerRow(name, project or None))
    return ContainerListing(DaemonState.LISTED, tuple(rows))


def foreign(rows: Sequence[ContainerRow]) -> dict[str | None, list[str]]:
    """Group unmarked running containers by project in first-seen order."""

    groups: dict[str | None, list[str]] = {}
    for row in rows:
        if row.project is not None:
            if row.project.startswith(OWNERSHIP_PREFIX):
                continue
        elif row.name.startswith(OWNERSHIP_PREFIX):
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
