"""``gideon uninstall``: remove the host state carrying GIDEON's ownership mark.

The ordered stages take down GIDEON's projects, units, firewall chain and its
tagged jump, the installed command, and its networks once nothing is attached;
no box-wide setting or shared prerequisite is reverted. ``--purge`` also
removes ``/etc/gideon``, GIDEON's ``/data`` directories, the stack's volumes,
and the drop-ins holding a GIDEON fact. Every stage reads before it acts, so
a re-run finds nothing left and exits 0.
"""

import argparse
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from gideon.host import backuplock, cotenants, stack
from gideon.host.render.compose import PROJECT_NAME
from gideon.host.report import StageResult, command_name, print_stage, refusal
from gideon.host.stages import run_stage
from gideon.host.steps import disk, network
from gideon.host.steps.accounts import SSHD_POLICY
from gideon.host.steps.command import COMMAND_PATH, INSTALL_HOME
from gideon.host.steps.docker import JOURNALD_DROP_IN
from gideon.host.steps.maintenance import AUTO_UPGRADES_DROP_IN
from gideon.host.steps.proxy import PROXY_CONF
from gideon.host.steps.services import REGISTRY_UNIT, RUNNER_SUDOERS
from gideon.host.steps.site_dirs import ETC_GIDEON
from gideon.host.sysio import LockingHost, PathLike, RealHost

_CHECKOUT: Final = Path(__file__).parents[2]
_RENDERED_DIR: Final = ETC_GIDEON / "rendered"
_PURGE_FLAG: Final = "--purge"
_COMPOSE_TIMEOUT: Final = 600.0
_BUILD_BOX_DATA_EXCLUSIONS: Final = ("registry", "acceptance")


@dataclass(frozen=True, slots=True)
class KeptDropIn:
    path: Path
    holds: str


@dataclass(frozen=True, slots=True)
class PurgedDropIn:
    path: Path
    reload: tuple[str, ...]


KEPT_DROP_INS: Final[tuple[KeptDropIn, ...]] = (
    KeptDropIn(JOURNALD_DROP_IN, "journald retention"),
    KeptDropIn(SSHD_POLICY, "key-only SSH login"),
    KeptDropIn(AUTO_UPGRADES_DROP_IN, "automatic upgrades"),
)
PURGED_DROP_INS: Final[tuple[PurgedDropIn, ...]] = (
    PurgedDropIn(PROXY_CONF, ()),
    PurgedDropIn(network.CHRONY_SOURCES, ("chronyc", "reload", "sources")),
    PurgedDropIn(network.WAIT_ONLINE_DROPIN, ("systemctl", "daemon-reload")),
)


@dataclass(frozen=True, slots=True)
class _Reading:
    docker: cotenants.DaemonState
    compose: bool
    registry_role: bool
    runner_role: bool
    purge: bool
    checkout: Path


def _form(purge: bool, checkout: PathLike) -> str:
    """The long form from the checkout: the run removes the installed command."""

    flag = f" {_PURGE_FLAG}" if purge else ""
    return f"sudo python3 -m gideon uninstall{flag} from {checkout}"


def _rerun(purge: bool, checkout: PathLike) -> str:
    return f"Run {_form(purge, checkout)}."


def _diagnostic(result: subprocess.CompletedProcess[str]) -> str:
    lines = (result.stderr or result.stdout).splitlines()
    return lines[0] if lines else "no diagnostic"


def _read(
    io: LockingHost,
    stage: str,
    argv: Sequence[str],
    what: str,
    fix: str,
    *,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str] | StageResult:
    try:
        result = io.run(argv, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult(stage, False, f"cannot read {what}: {exc}", fix)
    if result.returncode != 0:
        return StageResult(stage, False, f"cannot read {what}: {_diagnostic(result)}", fix)
    return result


def _disable_unit(io: LockingHost, stage: str, unit: str, fix: str) -> StageResult | None:
    try:
        result = io.run(["systemctl", "disable", "--now", unit])
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult(stage, False, f"cannot disable {unit}: {exc}", fix)
    if result.returncode == 0:
        return None
    state = _read(
        io, stage,
        ["systemctl", "show", "--property=LoadState", "--value", unit],
        f"{unit} load state", fix,
    )
    if not isinstance(state, StageResult) and state.stdout.strip() == "not-found":
        return None
    return StageResult(stage, False, f"cannot disable {unit}: {_diagnostic(result)}", fix)


def _preconditions(
    io: LockingHost,
    *,
    purge: bool,
    checkout: Path,
    backup_detail: str,
    engine_detail: str,
) -> tuple[StageResult, _Reading | None]:
    fix = _rerun(purge, checkout)
    try:
        listing = cotenants.running_containers(io)
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult("preconditions", False, f"cannot read Docker containers: {exc}", fix), None
    if listing.state is cotenants.DaemonState.NOT_ACTIVE:
        return StageResult(
            "preconditions", False,
            "Docker is installed but its daemon is not active; live-restored containers cannot be listed",
            f"Start Docker so its containers can be listed. {fix}",
        ), None
    if listing.state is cotenants.DaemonState.UNREADABLE:
        return StageResult(
            "preconditions", False,
            f"cannot read Docker containers: {listing.diagnostic}",
            f"Repair docker ps so its containers can be listed. {fix}",
        ), None

    compose = listing.state is cotenants.DaemonState.LISTED
    if compose:
        version = _read(
            io, "preconditions", ["docker", "compose", "version"],
            "Docker Compose", fix, timeout=_COMPOSE_TIMEOUT,
        )
        if isinstance(version, StageResult):
            return version, None
    try:
        registry_role = io.exists(REGISTRY_UNIT)
        runner_role = io.exists(RUNNER_SUDOERS)
    except OSError as exc:
        return StageResult("preconditions", False, f"cannot inspect build-box roles: {exc}", fix), None

    reading = _Reading(listing.state, compose, registry_role, runner_role, purge, checkout)
    tier = (
        "--purge: configuration, data, and volumes will be removed"
        if purge else "plain: configuration, data, and volumes stay"
    )
    roles = [name for name, found in (("registry", registry_role), ("runner", runner_role)) if found]
    role_detail = f"build-box roles kept: {', '.join(roles)}" if roles else "no build-box roles found"
    docker_detail = "Docker absent" if not compose else "Docker and Compose available"
    detail = f"{tier}; {docker_detail}; {role_detail}; {backup_detail}; {engine_detail}"
    return StageResult("preconditions", True, detail, ""), reading


def _attached(io: LockingHost, reading: _Reading) -> StageResult:
    if not reading.compose:
        return StageResult("attached", True, "Docker absent; no networks to inspect", "")
    fix = _rerun(reading.purge, reading.checkout)
    listed = _read(io, "attached", cotenants.network_ls_argv(), "Docker networks", fix)
    if isinstance(listed, StageResult):
        return listed
    details: list[str] = []
    networks = tuple(name for name in cotenants.parse_names(listed.stdout) if cotenants.marked_name(name))
    for name in networks:
        result = _read(io, "attached", cotenants.network_ps_argv(name), f"containers on {name}", fix)
        if isinstance(result, StageResult):
            return result
        foreign = cotenants.foreign(cotenants.parse_rows(result.stdout))
        if foreign:
            details.append(f"{name}: {cotenants.describe(foreign)} loses the engine when the stack stops")
    detail = "; ".join(details) if details else "no unmarked containers attached to GIDEON networks"
    return StageResult("attached", True, detail, "")


def _units(io: LockingHost, reading: _Reading) -> StageResult:
    fix = _rerun(reading.purge, reading.checkout)
    listed = _read(
        io, "units",
        ["systemctl", "list-unit-files", "--no-legend", "--plain", "gideon-*.service", "gideon-*.timer"],
        "GIDEON unit files", fix,
    )
    if isinstance(listed, StageResult):
        return listed
    found: list[str] = []
    for line in listed.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        unit = fields[0]
        if (
            cotenants.marked_name(unit)
            and unit.endswith((".service", ".timer"))
            and unit not in (REGISTRY_UNIT.name, network.DOCKER_USER_UNIT.name)
        ):
            found.append(unit)
    units = tuple(dict.fromkeys(found))
    for unit in units:
        failed = _disable_unit(io, "units", unit, f"Inspect journalctl -u {unit}. {fix}")
        if failed is not None:
            return failed
    reloaded = run_stage(io, "units", ["systemctl", "daemon-reload"], "systemd reloaded", fix)
    if not reloaded.ok:
        return reloaded
    detail = f"disabled {', '.join(units)}" if units else "no GIDEON units to disable"
    if reading.registry_role:
        detail += f"; kept {REGISTRY_UNIT.name} for the registry role"
    return StageResult("units", True, detail, "")


def _projects(io: LockingHost, reading: _Reading, rendered_dir: PathLike) -> StageResult:
    if not reading.compose:
        return StageResult("projects", True, "Docker absent; no Compose projects to stop", "")
    fix = _rerun(reading.purge, reading.checkout)
    listed = _read(
        io, "projects", cotenants.compose_projects_argv(),
        "Compose projects", fix, timeout=_COMPOSE_TIMEOUT,
    )
    if isinstance(listed, StageResult):
        return listed
    projects = cotenants.parse_projects(listed.stdout)
    if projects is None:
        return StageResult("projects", False, "Compose projects listing is malformed", fix)
    marked = tuple(dict.fromkeys(name for name in projects if cotenants.marked_name(name)))
    others = tuple(name for name in marked if name != PROJECT_NAME)
    ordered = (*others, *((PROJECT_NAME,) if PROJECT_NAME in marked else ()))
    for name in ordered:
        if name == PROJECT_NAME:
            compose_path = Path(rendered_dir) / "compose.yaml"
            try:
                rendered = io.exists(compose_path)
            except OSError as exc:
                return StageResult("projects", False, f"cannot inspect {compose_path}: {exc}", fix)
            flags = ("--remove-orphans", *(("--volumes",) if reading.purge else ()))
            argv = (
                stack.compose_argv(rendered_dir, "down", *flags)
                if rendered else stack.compose_project_argv(name, "down", *flags)
            )
        else:
            argv = stack.compose_project_argv(name, "down", "--volumes", "--remove-orphans")
        result = run_stage(
            io, "projects", argv, f"take down {name}",
            f"Run {shlex.join(argv)} by hand. {fix}", timeout=_COMPOSE_TIMEOUT,
        )
        if not result.ok:
            return result
    detail = f"stopped {', '.join(ordered)}" if ordered else "no GIDEON Compose projects found"
    if not reading.purge:
        return StageResult("projects", True, detail, "")

    # A project taken down by an earlier plain run is gone from Compose's
    # listing, which reads containers; its volumes are found by their label.
    volumes = _read(io, "projects", cotenants.volume_ls_argv(), "Docker volumes", fix)
    if isinstance(volumes, StageResult):
        return volumes
    marked_volumes = tuple(
        row.name for row in cotenants.parse_rows(volumes.stdout) if cotenants.is_marked(row)
    )
    for name in marked_volumes:
        argv = ["docker", "volume", "rm", name]
        result = run_stage(
            io, "projects", argv, f"remove volume {name}", f"Run {shlex.join(argv)} by hand. {fix}",
        )
        if not result.ok:
            return result
    if marked_volumes:
        detail += f"; removed volumes {', '.join(marked_volumes)}"
    else:
        detail += "; no GIDEON volumes left"
    return StageResult("projects", True, detail, "")


def _chain_rules(
    io: LockingHost, chain: str, reading: _Reading
) -> tuple[tuple[str, ...] | None, StageResult | None]:
    try:
        result = io.run(["iptables", "-w", "-S", chain])
    except (OSError, subprocess.SubprocessError) as exc:
        return None, StageResult("firewall", False, f"cannot read {chain}: {exc}", _rerun(reading.purge, reading.checkout))
    if result.returncode in (126, 127):
        return None, StageResult(
            "firewall", False, f"cannot run iptables for {chain}: {_diagnostic(result)}",
            f"Repair iptables. {_rerun(reading.purge, reading.checkout)}",
        )
    if result.returncode != 0:
        return None, None
    return tuple(line for line in result.stdout.splitlines() if line.startswith(f"-A {chain} ")), None


def _firewall(io: LockingHost, reading: _Reading) -> StageResult:
    fix = _rerun(reading.purge, reading.checkout)
    disabled = _disable_unit(io, "firewall", network.DOCKER_USER_UNIT.name, fix)
    if disabled is not None:
        return disabled
    try:
        io.unlink(network.DOCKER_USER_UNIT, missing_ok=True)
    except OSError as exc:
        return StageResult("firewall", False, f"cannot remove {network.DOCKER_USER_UNIT}: {exc}", fix)
    reloaded = run_stage(io, "firewall", ["systemctl", "daemon-reload"], "systemd reloaded", fix)
    if not reloaded.ok:
        return reloaded

    shared, failure = _chain_rules(io, network.SHARED_CHAIN, reading)
    if failure is not None:
        return failure
    positions = tuple(
        position for position, line in enumerate(shared or (), start=1) if network.tagged(line)
    )
    for position in reversed(positions):
        deleted = run_stage(
            io, "firewall", ["iptables", "-w", "-D", network.SHARED_CHAIN, str(position)],
            f"delete tagged jump {position} from {network.SHARED_CHAIN}", fix,
        )
        if not deleted.ok:
            return deleted

    block_removed = False
    try:
        after_rules = io.read_text(network.AFTER_RULES)
    except FileNotFoundError:
        after_rules = None
    except (OSError, UnicodeError) as exc:
        return StageResult("firewall", False, f"cannot read {network.AFTER_RULES}: {exc}", fix)
    if after_rules is not None:
        cleaned = network.without_block(after_rules)
        if cleaned != after_rules:
            try:
                io.write_text(network.AFTER_RULES, cleaned, mode=0o640)
            except OSError as exc:
                return StageResult("firewall", False, f"cannot write {network.AFTER_RULES}: {exc}", fix)
            block_removed = True

    ufw_note = "ufw reload not needed"
    if block_removed:
        active = network.ufw_active(io)
        if active:
            reloaded = run_stage(io, "firewall", ["ufw", "reload"], "ufw reloaded", fix)
            if not reloaded.ok:
                return reloaded
            ufw_note = "ufw reloaded"
        elif active is None:
            ufw_note = "ufw status could not be read; reload skipped"
        else:
            ufw_note = "ufw inactive; reload skipped"

    owned, failure = _chain_rules(io, network.CHAIN, reading)
    if failure is not None:
        return failure
    if owned is not None:
        for action in ("-F", "-X"):
            changed = run_stage(
                io, "firewall", ["iptables", "-w", action, network.CHAIN],
                f"remove GIDEON chain {network.CHAIN}", fix,
            )
            if not changed.ok:
                return changed

    status = _read(io, "firewall", ["ufw", "status", "numbered"], "tagged ufw rules", fix)
    if isinstance(status, StageResult):
        kept_detail = f"tagged ufw allow rules not counted ({status.detail})"
    else:
        kept = sum(network.UFW_TAG in line for line in status.stdout.splitlines())
        kept_detail = f"kept {kept} tagged ufw allow rule(s)"
    chain_detail = "chain removed" if owned is not None else "chain already absent"
    block_detail = "block removed" if block_removed else "block already absent"
    return StageResult(
        "firewall", True,
        f"removed {len(positions)} tagged jump(s); {chain_detail}; {block_detail}; {ufw_note}; "
        f"{kept_detail}, since the deny policy stays and they admit SSH under it",
        "",
    )


def _command(io: LockingHost, reading: _Reading) -> StageResult:
    fix = _rerun(reading.purge, reading.checkout)
    try:
        present = io.exists(COMMAND_PATH)
    except OSError as exc:
        return StageResult("command", False, f"cannot inspect {COMMAND_PATH}: {exc}", fix)
    if not present:
        return StageResult("command", True, f"{COMMAND_PATH} already absent", "")
    try:
        io.unlink(COMMAND_PATH, missing_ok=True)
    except OSError as exc:
        return StageResult("command", False, f"cannot remove {COMMAND_PATH}: {exc}", fix)
    return StageResult("command", True, f"removed {COMMAND_PATH}", "")


def _files(io: LockingHost, reading: _Reading) -> StageResult:
    fix = _rerun(reading.purge, reading.checkout)
    kept = "; ".join(f"{item.path} ({item.holds})" for item in KEPT_DROP_INS)
    if not reading.purge:
        facts = ", ".join(str(item.path) for item in PURGED_DROP_INS)
        return StageResult(
            "files", True,
            f"kept box-wide settings: {kept}; kept GIDEON files: {facts}. "
            f"To remove the GIDEON files, run {_form(True, reading.checkout)}.",
            "",
        )

    removed: list[str] = []
    for item in PURGED_DROP_INS:
        try:
            present = io.exists(item.path)
        except OSError as exc:
            return StageResult("files", False, f"cannot inspect {item.path}: {exc}", fix)
        if not present:
            continue
        try:
            io.unlink(item.path, missing_ok=True)
        except OSError as exc:
            return StageResult("files", False, f"cannot remove {item.path}: {exc}", fix)
        removed.append(str(item.path))
        if item.path == network.CHRONY_SOURCES:
            try:
                active = io.run(["systemctl", "is-active", "chrony"])
            except (OSError, subprocess.SubprocessError) as exc:
                return StageResult("files", False, f"cannot read chrony state: {exc}", fix)
            if active.returncode in (3, 4):
                continue
            if active.returncode != 0:
                return StageResult(
                    "files", False, f"cannot read chrony state: {_diagnostic(active)}", fix,
                )
            if active.stdout.strip() != "active":
                continue
        if item.reload:
            reloaded = run_stage(
                io, "files", item.reload, f"reload after removing {item.path}", fix,
            )
            if not reloaded.ok:
                return reloaded
    changed = f"removed {', '.join(removed)}" if removed else "GIDEON file drop-ins already absent"
    return StageResult("files", True, f"{changed}; kept box-wide settings: {kept}", "")


def _data(io: LockingHost, reading: _Reading) -> StageResult:
    directories = disk.data_directories()
    if not reading.purge:
        return StageResult(
            "data", True,
            f"kept {len(directories)} GIDEON directories under {disk.DATA_MOUNT}. "
            f"To remove them, run {_form(True, reading.checkout)}.",
            "",
        )

    fix = _rerun(True, reading.checkout)
    roles = dict(zip(_BUILD_BOX_DATA_EXCLUSIONS, (reading.registry_role, reading.runner_role), strict=True))
    removed: list[str] = []
    absent: list[str] = []
    kept: list[str] = []
    for name in directories:
        path = disk.DATA_MOUNT / name
        if roles.get(name, False):
            role = "registry" if name == "registry" else "runner"
            kept.append(f"{path} ({role} role)")
            continue
        try:
            present = io.exists(path)
        except OSError as exc:
            return StageResult("data", False, f"cannot inspect {path}: {exc}", fix)
        if not present:
            absent.append(str(path))
            continue
        result = run_stage(
            io, "data", ["rm", "-rf", "--one-file-system", str(path)],
            f"remove {path}", fix,
        )
        if not result.ok:
            return result
        removed.append(str(path))
    detail = f"removed {', '.join(removed)}" if removed else "no GIDEON data directories to remove"
    if absent:
        detail += f"; already absent: {', '.join(absent)}"
    if kept:
        detail += f"; kept for build-box roles: {', '.join(kept)}"
    detail += f"; {disk.DATA_MOUNT} mount and fstab entry kept"
    return StageResult("data", True, detail, "")


def _configuration(io: LockingHost, reading: _Reading) -> StageResult:
    if not reading.purge:
        return StageResult(
            "configuration", True,
            f"kept {ETC_GIDEON} with site configuration, secrets, rendered files, and box identity. "
            f"To remove it, run {_form(True, reading.checkout)}.",
            "",
        )
    fix = _rerun(True, reading.checkout)
    try:
        present = io.exists(ETC_GIDEON)
    except OSError as exc:
        return StageResult("configuration", False, f"cannot inspect {ETC_GIDEON}: {exc}", fix)
    if not present:
        return StageResult("configuration", True, f"{ETC_GIDEON} already absent", "")
    return run_stage(io, "configuration", ["rm", "-rf", str(ETC_GIDEON)], f"removed {ETC_GIDEON}", fix)


def _networks(io: LockingHost, reading: _Reading) -> StageResult:
    if not reading.compose:
        return StageResult("networks", True, "Docker absent; no networks to remove", "")
    fix = _rerun(reading.purge, reading.checkout)
    listed = _read(io, "networks", cotenants.network_ls_argv(), "Docker networks", fix)
    if isinstance(listed, StageResult):
        return listed
    removed: list[str] = []
    held: list[str] = []
    for name in cotenants.parse_names(listed.stdout):
        if not cotenants.marked_name(name):
            continue
        attached = _read(io, "networks", cotenants.network_ps_argv(name), f"containers on {name}", fix)
        if isinstance(attached, StageResult):
            return attached
        rows = cotenants.parse_rows(attached.stdout)
        if rows:
            groups: dict[str | None, list[str]] = {}
            for row in rows:
                groups.setdefault(row.project, []).append(row.name)
            held.append(f"{name}: {cotenants.describe(groups)}")
            continue
        result = run_stage(io, "networks", ["docker", "network", "rm", name], f"remove {name}", fix)
        if not result.ok:
            return result
        removed.append(name)
    if held:
        detail = f"networks still held by containers: {'; '.join(held)}"
        if removed:
            detail += f"; removed {', '.join(removed)}"
        return StageResult(
            "networks", False, detail,
            f"Announce to the attached containers' operator and wait until they have left the network. {fix}",
        )
    detail = f"removed {', '.join(removed)}" if removed else "no GIDEON networks to remove"
    return StageResult("networks", True, detail, "")


def _kept(reading: _Reading) -> str:
    detail = (
        f"{INSTALL_HOME} (remove by hand with rm -rf {INSTALL_HOME}), the gideon account, "
        "the packages GIDEON installed, Docker's daemon.json keys and containerd's file, "
        f"the {disk.DATA_MOUNT} mount and its fstab entry"
    )
    if not reading.purge:
        detail += (
            f", {ETC_GIDEON}, GIDEON's {disk.DATA_MOUNT} directories, "
            "the Caddy volumes, and the GIDEON file drop-ins"
            f" (to remove these, run {_form(True, reading.checkout)})"
        )
    detail += f". Every re-run is {_form(reading.purge, reading.checkout)}."
    return f"Kept: {detail}"


def run_uninstall(
    args: argparse.Namespace,
    *,
    host: LockingHost | None = None,
    rendered_dir: PathLike = _RENDERED_DIR,
    checkout: PathLike = _CHECKOUT,
) -> int:
    """Remove GIDEON's marked host state; exit 0 iff every stage is ok."""

    io = host or RealHost()
    root = Path(checkout)
    purge = bool(getattr(args, "purge", False))
    if io.geteuid() != 0:
        print(refusal("uninstall", "root is required.", _rerun(purge, root)), file=sys.stderr)
        return 1

    holder = command_name("uninstall")
    backup = backuplock.claim(io, command=holder, now=datetime.now(UTC), lock=backuplock.BACKUP_LOCK)
    engine: backuplock.Claim | None = None
    try:
        if backup.refusal is not None:
            print_stage(backup.refusal)
            return 1
        engine = backuplock.claim(io, command=holder, now=datetime.now(UTC))
        if engine.refusal is not None:
            print_stage(engine.refusal)
            return 1
        first, reading = _preconditions(
            io, purge=purge, checkout=root,
            backup_detail=backup.detail,
            engine_detail=engine.detail,
        )
        print_stage(first)
        if not first.ok or reading is None:
            return 1
        stages: tuple[Callable[[], StageResult], ...] = (
            lambda: _attached(io, reading),
            lambda: _units(io, reading),
            lambda: _projects(io, reading, rendered_dir),
            lambda: _firewall(io, reading),
            lambda: _command(io, reading),
            lambda: _files(io, reading),
            lambda: _data(io, reading),
            lambda: _configuration(io, reading),
            lambda: _networks(io, reading),
        )
        for stage in stages:
            row = stage()
            print_stage(row)
            if not row.ok:
                return 1
        print("GIDEON is removed from this box.")
        print(_kept(reading))
        return 0
    finally:
        if engine is not None:
            backuplock.release_claim(io, engine)
        backuplock.release_claim(io, backup, lock=backuplock.BACKUP_LOCK)
