"""Verified staging and off-box restore orchestration.

Each entry holds the backup lock (``backuplock.py``) for its whole run, so a
backup, a push, and a restore never overlap; ``backuplock.py`` owns the rules.
Store recovery and levelling run with the worker down, so no job races a demotion.
"""

import argparse
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from gideon.host import (
    apply,
    audit,
    backup,
    backuplock,
    backuproots,
    backupset,
    derivedstores,
    images,
    nogpu,
    pgbackrest,
    report,
    secrets,
    site,
    sshtarget,
    stack,
    stores,
)
from gideon.host.index import level
from gideon.host.render.command import render_to_disk
from gideon.host.render.compose import STORE_SERVICES
from gideon.host.report import (
    Problem,
    StageResult,
    command_detail,
    print_stage,
    refusal,
)
from gideon.host.site import SiteConfig
from gideon.host.stages import aware_now, run_stage, site_problem
from gideon.host.sysio import Host, LockingHost, PathLike, RealHost

_SITE_PATH: Final = "/etc/gideon/site.yaml"
_RENDERED_DIR: Final = "/etc/gideon/rendered"
# The pre-restore row a fresh stack reads; the acceptance harness requires it.
FRESH_STACK_SKIPPED_DETAIL: Final = "skipped (fresh stack has no backup set in staging)"
# The two ownership phrases the fetch and files rows carry; the acceptance
# harness requires the mapped one on the files row.
REOWN_MAPPED_DETAIL: Final = "mapped to gideon"
REOWN_NO_RECORD_DETAIL: Final = "no gideon ids recorded"
_TARGET_FIX: Final = (
    "Authorize the backup key and check the target per "
    "docs/runbooks/office-services-setup.md §3, then re-run restore."
)
_SELECT_FIX: Final = "Choose an earlier --at, or omit it for the latest state."
_VERIFY_FIX: Final = "Repair the backup set, then retry restore."
_REOWN_FIX: Final = "Repair ownership of the backup staging directory, then retry restore."
_REGISTRY_FIX: Final = "Repair the host registry service, then retry restore."
_REGISTRY_TARGET_FIX: Final = "Correct registry in /etc/gideon/site.yaml, then retry restore."
_REGISTRY_SEED_PROBE_INTERVAL_SECONDS: Final = 2.0
REGISTRY_SEED_WAIT_SECONDS: Final = 30.0
_UNDECLARED_REGISTRY_PROBLEM: Final = (
    "the host registry is active, but this host is not declared the build box."
)
_UNDECLARED_REGISTRY_FIX: Final = (
    "Stop and disable gideon-registry (a former build box keeps its unit until "
    "removed by hand), or declare this host with host provision --build-box, then retry."
)
_RESTORE_TIMEOUT: Final = 18000.0



def _root_fix() -> str:
    return f"Run {report.command('restore --from <staging|target>')}, then retry."


def _apply_fix() -> str:
    return f"Run {report.command('apply')}, then retry."


def _set_fix() -> str:
    return f"Run {report.command('backup run')}, then retry."


def _archive_fallback_fix() -> str:
    return (
        f"Run {report.command('restore --from staging')}, then "
        f"{report.command('apply')}, then {report.command('backup run --full')}."
    )


def _archive_running_fix() -> str:
    return (
        f"Use {report.command('restore --from staging --at <instant>')} "
        "(or omit --at) while Postgres answers. For a cluster that does not answer, "
        "stop the stack with docker compose -f /etc/gideon/rendered/compose.yaml "
        f"down, then run {report.command('restore --from staging --archive-end')}."
    )


def _account_fix() -> str:
    return f"Run {report.command('host provision')}, then retry."


def _push_fix() -> str:
    return f"Run {report.command('backup push')}, then retry."


def _stage_fix() -> str:
    return f"Run {report.command('apply')}, then retry."


def _docker_fix() -> str:
    return f"Run {report.command('host provision --only docker-engine')}, then retry restore."


def _build_fix() -> str:
    return (
        "Build and push the built images with python3 -m tools.imagebuild <name> "
        "per docs/runbooks/built-images.md §2, then retry restore."
    )


def _seed_tag_fix(release: str) -> str:
    return (
        f"Check out tag v{release}, run {report.command('apply')} to render that tag's "
        "Compose file (a registry refusal naming the built images is expected), "
        "then retry restore; see docs/runbooks/backup-restore.md §5."
    )


@dataclass(frozen=True, slots=True)
class _Selection:
    source: str
    at: datetime | None
    snapshot: backupset.RemoteSnapshot | None
    record: backupset.PushRecord | None
    set_ref: backupset.SetRef | None
    archive_end: str | None = None


@dataclass(frozen=True, slots=True)
class _Fetched:
    side: str
    replaced: str
    selected: backupset.SetRef
    reowned: int


@dataclass(frozen=True, slots=True)
class _Reach:
    """The replay's position against the archive end selected before restore."""

    instant: datetime | None
    lsn: str
    segment: str
    archive_end: str
    short_by: int
    past: bool


def _reach_detail(reach: _Reach) -> str:
    instant = reach.instant.isoformat() if reach.instant is not None else "no commit replayed"
    detail = (
        f"{instant}; lsn={reach.lsn}; segment={reach.segment}; "
        f"archive newest segment={reach.archive_end}"
    )
    if reach.short_by:
        detail += (
            f"; short by {reach.short_by} segment(s); "
            "see docs/runbooks/backup-restore.md §4"
        )
    elif reach.past:
        detail += "; past the archive's newest segment"
    else:
        detail += "; archive end reached"
    return detail


def _refuse(problem: str, fix: str) -> int:
    print(refusal("restore", problem, fix), file=sys.stderr)
    return 1


def _preconditions(
    io: Host,
    args: object,
    *,
    build_box: bool,
    rendered_dir: PathLike,
    site_path: PathLike,
) -> tuple[SiteConfig, str, datetime | None, backupset.AccountIds, bool] | None:
    loaded = site.load_site(Path(site_path), host=io)
    if loaded.errors or loaded.config is None:
        _refuse(
            site_problem(loaded) or "the site file is invalid.",
            "Correct the site file, then retry.",
        )
        return None
    config = loaded.config
    source = getattr(args, "source", None)
    if source not in {"staging", "target"}:
        _refuse(
            "restore source must be staging or target.",
            "Use --from staging or --from target, then retry.",
        )
        return None

    archive_end = getattr(args, "archive_end", False)
    if archive_end and source == "target":
        _refuse(
            "--archive-end requires the local archive and cannot use --from target.",
            f"Use {report.command('restore --from staging --archive-end')}, then retry.",
        )
        return None

    at_text = getattr(args, "at", None)
    set_label = getattr(args, "set", None)
    if set_label is not None and (at_text is not None or source == "target"):
        _refuse(
            "--set cannot be combined with --at or --from target.",
            "Use --at for a point in time; use --from target for the off-box copy.",
        )
        return None

    gideon_ids = backupset.gideon_account_ids(io)
    if gideon_ids is None:
        _refuse(
            f"the {backupset.SERVICE_ACCOUNT} service account is missing or malformed.",
            _account_fix(),
        )
        return None

    try:
        compose_path = Path(rendered_dir) / "compose.yaml"
        if not io.exists(compose_path):
            _refuse(
                f"rendered Compose file is missing: {compose_path}.",
                _apply_fix(),
            )
            return None
    except OSError as exc:
        _refuse(f"cannot inspect restore prerequisites: {exc}.", _apply_fix())
        return None

    at: datetime | None = None
    if at_text is not None:
        parsed = backupset.parse_at(at_text, config.office.timezone)
        if isinstance(parsed, Problem):
            _refuse(parsed.problem, parsed.fix)
            return None
        at = parsed

    if source == "target":
        try:
            reachable = io.run(sshtarget.ssh_argv(config, "true"))
        except (OSError, subprocess.SubprocessError) as exc:
            _refuse(f"backup target SSH probe failed: {exc}.", _TARGET_FIX)
            return None
        if reachable.returncode != 0:
            _refuse(
                f"backup target SSH probe failed: {command_detail(reachable)}",
                _TARGET_FIX,
            )
            return None
    # A former build box keeps its registry unit; the files stage replaces
    # /data/registry, so a live registry on an undeclared host refuses here.
    if not build_box:
        try:
            active = io.run(["systemctl", "is-active", "gideon-registry"])
        except (OSError, subprocess.SubprocessError):
            active = None
        if active is not None and active.returncode == 0:
            _refuse(_UNDECLARED_REGISTRY_PROBLEM, _UNDECLARED_REGISTRY_FIX)
            return None
    return config, source, at, gideon_ids, archive_end


def _selection_detail(selection: _Selection, unfinished: Sequence[str] = ()) -> str:
    snapshot = selection.snapshot.label if selection.snapshot is not None else "-"
    set_label = (
        selection.set_ref.label
        if selection.set_ref is not None
        else "chosen after fetch"
    )
    target_time = selection.at.isoformat() if selection.at is not None else "latest"
    detail = (
        f"source={selection.source}; snapshot={snapshot}; set={set_label}; "
        f"at={target_time}"
    )
    if selection.record is not None:
        detail += f"; archive_through={selection.record.archive_through.isoformat()}"
    if unfinished:
        detail += (
            f"; {len(unfinished)} newer unfinished snapshot(s): "
            + ", ".join(unfinished)
        )
    return detail


def _newer_unfinished(
    listed: Sequence[backupset.RemoteSnapshot], selected: str
) -> tuple[str, ...]:
    # A newer push left unfinished is never selectable, so a target restore
    # can go back further than the newest push; the row names that gap.
    newer: list[str] = []
    for snapshot in listed:
        label = snapshot.label.removesuffix(backupset.PARTIAL_SUFFIX)
        if (
            not snapshot.complete
            and backupset.LABEL.fullmatch(label) is not None
            and label > selected
        ):
            newer.append(snapshot.label)
    return tuple(sorted(newer))


def _select_staging(
    io: Host, at: datetime | None, label: str | None = None
) -> tuple[StageResult, _Selection | None]:
    try:
        sets = backupset.list_sets(io)
    except OSError as exc:
        return (
            StageResult("select", False, f"cannot list backup sets: {exc}", _set_fix()),
            None,
        )
    selected = (
        backupset.select_set(sets, at=at)
        if label is None
        else backupset.select_set_by_label(sets, label)
    )
    if isinstance(selected, Problem):
        return StageResult("select", False, selected.problem, selected.fix), None
    if label is None:
        choice = _Selection("staging", at, None, None, selected)
        return StageResult("select", True, _selection_detail(choice), ""), choice
    # A set named by label is restored to its own archive boundary: the proven
    # bound, so the cluster comes back as exactly the state the set describes.
    if selected.manifest is None:
        return StageResult("select", False, "selected set has no manifest", _set_fix()), None
    boundary = selected.manifest.archive_through
    choice = _Selection("staging", boundary, None, None, selected)
    detail = f"source=staging; set={selected.label}; archive_through={boundary.isoformat()}"
    return StageResult("select", True, detail, ""), choice


def _select_archive_end(
    io: Host, rendered_dir: PathLike
) -> tuple[StageResult, _Selection | None]:
    try:
        sets = backupset.list_sets(io)
    except OSError as exc:
        return StageResult("select", False, f"cannot list backup sets: {exc}", _set_fix()), None
    complete = tuple(ref for ref in sets if ref.complete and ref.manifest is not None)
    if not complete:
        return StageResult("select", False, "no complete backup set is available", _set_fix()), None

    found = pgbackrest.info(io, rendered_dir, running=False)
    if not found.ok:
        return StageResult("select", False, found.problem or "pgBackRest info failed", found.fix), None
    if not found.archive_max:
        return StageResult("select", False, "the local archive has no archived WAL segment", _set_fix()), None
    if found.archive_id is None:
        return StageResult(
            "select", False, "pgBackRest info records no archive id",
            stack.logs_fix(rendered_dir, "postgres"),
        ), None
    history = pgbackrest.history(io, rendered_dir, found.archive_id)
    if not history.ok:
        return StageResult("select", False, history.problem or "pgBackRest history failed", history.fix), None

    backups = {entry.label: entry for entry in found.infos}
    passed: list[str] = []
    for ref in complete:
        assert ref.manifest is not None
        label = ref.manifest.pgbackrest_label
        entry = backups.get(label)
        if entry is None:
            passed.append(f"set {ref.label}: the repository holds no backup {label}")
            continue
        if entry.lsn_stop is None:
            passed.append(f"set {ref.label}: backup {label} records no stop LSN")
            continue
        try:
            reaches = pgbackrest.reaches_latest(entry, history.latest, history.chain)
        except ValueError as exc:
            return StageResult(
                "select", False, f"backup {label} records a malformed stop LSN: {exc}",
                stack.logs_fix(rendered_dir, "postgres"),
            ), None
        if not reaches:
            passed.append(
                f"set {ref.label}: backup {label} on timeline {entry.timeline} "
                f"cannot reach timeline {history.latest}"
            )
            continue
        choice = _Selection("staging", None, None, None, ref, found.archive_max)
        detail = (
            f"source=staging; set={ref.label}; backup={label}; timeline={entry.timeline}; "
            f"latest_timeline={history.latest}; archive_end={found.archive_max}"
        )
        if passed:
            detail += "; passed over: " + "; ".join(passed)
        return StageResult("select", True, detail, ""), choice

    newest = complete[0]
    assert newest.manifest is not None
    newest_backup = backups.get(newest.manifest.pgbackrest_label)
    timeline = newest_backup.timeline if newest_backup is not None else None
    detail = (
        f"no complete backup set reaches latest timeline {history.latest}; "
        f"newest set={newest.label}; timeline={timeline}; newest local archive boundary="
        f"{newest.manifest.archive_through.isoformat()}; archive_end={found.archive_max}; "
        "passed over: " + "; ".join(passed)
    )
    return StageResult("select", False, detail, _archive_fallback_fix()), None


def _select_target(
    io: Host,
    config: SiteConfig,
    at: datetime | None,
) -> tuple[StageResult, _Selection | None]:
    listed = backup.list_remote_snapshots(io, config)
    if isinstance(listed, StageResult):
        return listed, None
    complete = tuple(snapshot for snapshot in listed if snapshot.complete)
    if not complete:
        return (
            StageResult(
                "select",
                False,
                "no complete remote backup snapshot is available",
                _push_fix(),
            ),
            None,
        )

    records: list[tuple[backupset.RemoteSnapshot, backupset.PushRecord]] = []
    for snapshot in complete:
        record = backup.read_push_record(io, config, snapshot.label)
        if isinstance(record, Problem):
            return (
                StageResult(
                    "select",
                    False,
                    record.problem,
                    record.fix,
                ),
                None,
            )
        records.append((snapshot, record))

    if at is None:
        snapshot, record = max(records, key=lambda pair: pair[0].label)
    else:
        eligible = [pair for pair in records if pair[1].archive_through >= at]
        if not eligible:
            newest_snapshot, newest_record = max(
                records, key=lambda pair: pair[0].label
            )
            del newest_snapshot
            return (
                StageResult(
                    "select",
                    False,
                    "requested --at is after the newest remote archive boundary "
                    f"{newest_record.archive_through.isoformat()}",
                    _SELECT_FIX,
                ),
                None,
            )
        snapshot, record = min(eligible, key=lambda pair: pair[0].label)
    selected = _Selection("target", at, snapshot, record, None)
    detail = _selection_detail(selected, _newer_unfinished(listed, snapshot.label))
    return StageResult("select", True, detail, ""), selected


def _postgres_answers(io: Host, rendered_dir: PathLike) -> bool:
    try:
        result = io.run(
            stack.exec_argv(rendered_dir, "postgres", "pg_isready")
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _partial_fix() -> str:
    return (
        f"Start the whole stack with {report.command('apply')}, or stop it with "
        "docker compose -f /etc/gideon/rendered/compose.yaml down, then retry restore."
    )


def _incomplete_staging_fix() -> str:
    return (
        f"Run {report.command('backup run')} to complete a set, or on a rebuilt box "
        "stop the stack with docker compose -f /etc/gideon/rendered/compose.yaml down, "
        "then retry restore."
    )


def _pre_restore_stage(
    io: LockingHost,
    *,
    source: str,
    rendered_dir: PathLike,
    site_path: PathLike,
    root: PathLike | None,
    now: datetime,
    archive_end: bool = False,
) -> tuple[StageResult, str | None]:
    # The order is running, fresh, partial, incomplete, take, push: a fresh
    # stack has nothing a safety set would protect, while a staging of
    # incomplete entries alone is damage, not freshness. The safety set needs
    # Postgres; a stack whose other services are still serving while Postgres
    # is down would be replaced without one, so the running decision is made
    # on every service, not on Postgres alone.
    running = stack.running_services(io, rendered_dir)
    if running is None:
        return (
            StageResult(
                "pre-restore",
                False,
                "cannot determine which services are running",
                stack.logs_fix(rendered_dir, "postgres"),
            ),
            None,
        )
    if not running:
        result = StageResult(
            "pre-restore", True, "skipped (stack not running)", ""
        )
        return result, None
    try:
        sets = backupset.list_sets(io)
    except OSError as exc:
        return (
            StageResult("pre-restore", False, f"cannot list backup sets: {exc}", _set_fix()),
            None,
        )
    if not sets:
        return StageResult("pre-restore", True, FRESH_STACK_SKIPPED_DETAIL, ""), None
    if not _postgres_answers(io, rendered_dir):
        return (
            StageResult(
                "pre-restore",
                False,
                f"the stack is partially running ({', '.join(sorted(running))}) and "
                "Postgres does not answer, so the pre-restore set cannot be taken",
                _partial_fix(),
            ),
            None,
        )

    if archive_end:
        return StageResult(
            "pre-restore", False,
            "the stack is running and Postgres answers; --archive-end is for a cluster that does not answer",
            _archive_running_fix(),
        ), None

    if not any(ref.complete for ref in sets):
        incomplete = tuple(ref.label for ref in sets if not ref.complete)
        count = len(incomplete)
        entry_word = "entry" if count == 1 else "entries"
        return (
            StageResult(
                "pre-restore",
                False,
                "no complete backup set in staging, only "
                f"{count} incomplete {entry_word}: {', '.join(incomplete)}",
                _incomplete_staging_fix(),
            ),
            None,
        )

    pre_label = backupset.pre_restore_label(now)
    pre_run = backup.run_backup_run(
        argparse.Namespace(full=True, label=None),
        host=io,
        rendered_dir=rendered_dir,
        site_path=site_path,
        root=root,
        now=now,
        pre_restore=True,
    )
    if pre_run != 0:
        return (
            StageResult(
                "pre-restore",
                False,
                f"pre-restore backup failed for {pre_label}",
                _stage_fix(),
            ),
            pre_label,
        )
    if source == "target":
        pushed = backup.run_backup_push(
            argparse.Namespace(verify_all=False),
            host=io,
            rendered_dir=rendered_dir,
            site_path=site_path,
            now=now,
        )
        if pushed != 0:
            return (
                StageResult(
                    "pre-restore",
                    False,
                    f"pre-restore push failed for {pre_label}",
                    _stage_fix(),
                ),
                pre_label,
            )
    detail = f"created {pre_label}"
    if source == "target":
        detail += " and pushed it"
    return StageResult("pre-restore", True, detail, ""), pre_label


def _staging_bound(
    io: Host,
    *,
    at: datetime,
) -> StageResult | None:
    try:
        sets = backupset.list_sets(io)
    except OSError as exc:
        return StageResult("pre-restore", False, f"cannot refresh backup sets: {exc}", _set_fix())
    newest = next((ref for ref in sets if ref.complete and ref.manifest is not None), None)
    if newest is None or newest.manifest is None:
        return StageResult("pre-restore", False, "no complete backup set remains", _set_fix())
    if at > newest.manifest.archive_through:
        return StageResult(
            "pre-restore",
            False,
            "requested --at is after the newest local archive boundary "
            f"{newest.manifest.archive_through.isoformat()}",
            _SELECT_FIX,
        )
    return None


def _later_sets(
    io: Host,
    selected: backupset.SetRef,
    *,
    at: datetime | None,
    staging: PathLike,
) -> tuple[backupset.SetRef, ...] | StageResult:
    """Find the complete sets after the selected one a bound root can draw on, oldest first."""

    if (
        at is None
        or selected.manifest is None
        or at <= selected.manifest.archive_through
        or selected.finished is None
    ):
        return ()
    roots = backupset.inventory_roots(selected.manifest.checkout)
    try:
        sets = backupset.list_sets(io, staging=staging)
    except OSError as exc:
        return StageResult("verify", False, f"cannot list backup sets: {exc}", _set_fix())
    return tuple(
        ref for ref in reversed(sets)
        if ref.complete and ref.manifest is not None
        and ref.finished is not None and ref.finished > selected.finished
        and backuproots.supplies(ref.manifest, roots)
    )


def _seed_registry(
    io: Host,
    config: SiteConfig,
    *,
    rendered_dir: PathLike,
    selected: backupset.SetRef,
    roots: tuple[backupset.InventoryRoot, ...],
    sleep: Callable[[float], None],
) -> str | StageResult:
    """Ensure the rendered Postgres image is served before the ownership lookup."""

    image = stack.service_image(io, rendered_dir, "postgres")
    if isinstance(image, Problem):
        return StageResult("fetch", False, image.problem, image.fix)
    target = images.parse_registry(config.registry)
    if target is None:
        return StageResult(
            "fetch", False, f"registry is not usable: {config.registry}", _REGISTRY_TARGET_FIX
        )
    environment: Mapping[str, str] | None = None
    if config.egress_proxy:
        proxy = images.proxy_environment(config, io)
        if not proxy.ok:
            return StageResult("fetch", False, proxy.problem or "proxy is unusable", proxy.fix)
        environment = proxy.variables
    argv = images.manifest_inspect_argv(target, image)

    def probe() -> bool | StageResult:
        try:
            result = io.run(argv, env=environment)
        except (OSError, subprocess.SubprocessError) as exc:
            return StageResult(
                "fetch", False, f"Docker manifest probe could not run: {exc}", _docker_fix()
            )
        if result.returncode == 127:
            return StageResult("fetch", False, "Docker manifest probe is unavailable", _docker_fix())
        return result.returncode == 0

    present = probe()
    if isinstance(present, StageResult):
        return present
    if present:
        return "the host registry holds the Postgres image"

    root = backuproots.registry_root(roots)
    manifest = selected.manifest
    if root is None or manifest is None or not any(
        entry.kind == "f" for entry in manifest.inventory.get(root.name, ())
    ):
        return StageResult(
            "fetch", False, f"set {selected.label} carries no registry copy", _build_fix()
        )
    held_root = backuproots.held(io, (root,), selected, ())[0]
    problem = held_root.verify()
    if problem is not None:
        return StageResult("fetch", False, problem.problem, problem.fix or _VERIFY_FIX)
    copied = run_stage(
        io,
        "fetch",
        ["rsync", "-a", held_root.copy.rstrip("/") + "/", root.source.rstrip("/") + "/"],
        f"seeded the host registry from set {selected.label}'s {root.name} copy",
        _REGISTRY_FIX,
    )
    if not copied.ok:
        return copied
    restarted = run_stage(
        io,
        "fetch",
        ["systemctl", "restart", "gideon-registry"],
        "restarted the host registry",
        _REGISTRY_FIX,
    )
    if not restarted.ok:
        return restarted

    waited = 0.0
    while True:
        present = probe()
        if isinstance(present, StageResult):
            return present
        if present:
            return (
                f"seeded the host registry from set {selected.label}'s {root.name} copy "
                "and restarted it"
            )
        if waited >= REGISTRY_SEED_WAIT_SECONDS:
            return StageResult(
                "fetch",
                False,
                "the host registry lacks the Postgres image after the seed from "
                f"set {selected.label} (release {manifest.release})",
                _seed_tag_fix(manifest.release),
            )
        interval = min(_REGISTRY_SEED_PROBE_INTERVAL_SECONDS, REGISTRY_SEED_WAIT_SECONDS - waited)
        sleep(interval)
        waited += interval


def _fetch_stage(
    io: Host,
    config: SiteConfig,
    *,
    rendered_dir: PathLike,
    build_box: bool,
    sleep: Callable[[float], None],
    snapshot: backupset.RemoteSnapshot,
    at: datetime | None,
    operation_label: str,
    gideon_ids: backupset.AccountIds,
    roots: tuple[backupset.InventoryRoot, ...],
) -> tuple[StageResult, _Fetched | None]:
    side = f"{backupset.STAGING}.fetch-{operation_label}"
    remote_root = config.backup.target.path.rstrip("/") or "/"
    source = (
        f"{config.backup.target.user}@{config.backup.target.host}:"
        f"{remote_root}/{snapshot.label}/"
    )
    rsync = run_stage(
        io,
        "fetch",
        [
            "rsync",
            "-aH",
            "--delete",
            "-e",
            sshtarget.rsync_ssh_option(),
            source,
            side.rstrip("/") + "/",
        ],
        f"fetched remote snapshot {snapshot.label} into {side}",
        _stage_fix(),
    )
    if not rsync.ok:
        return rsync, None

    try:
        sets = backupset.list_sets(io, staging=side)
    except OSError as exc:
        return StageResult("fetch", False, f"cannot list fetched sets: {exc}", _set_fix()), None
    newest = next((ref for ref in sets if ref.complete and ref.manifest is not None), None)
    if newest is None:
        return StageResult("fetch", False, "fetched snapshot has no complete backup set", _set_fix()), None
    selected = backupset.select_set(sets, at=at)
    if isinstance(selected, Problem):
        return StageResult("fetch", False, selected.problem, selected.fix), None
    registry_clause = ""
    if build_box:
        seeded = _seed_registry(
            io, config, rendered_dir=rendered_dir, selected=selected, roots=roots, sleep=sleep
        )
        if isinstance(seeded, StageResult):
            return seeded, None
        registry_clause = seeded + "; "

    for landed_root in backuproots.landed(io, roots, side):
        problem = landed_root.arrive(rendered_dir)
        if problem is not None:
            return StageResult("fetch", False, problem.problem, problem.fix or _REOWN_FIX), None

    claims = backuproots.Claims()
    for fetched_root in backuproots.fetched(io, roots, side, sets):
        outcome = fetched_root.reown(gideon_ids)
        if isinstance(outcome, Problem):
            return StageResult("fetch", False, outcome.problem, outcome.fix or _REOWN_FIX), None
        claims.merge(outcome)
    problem = claims.apply(io, "fetch")
    if problem is not None:
        return StageResult("fetch", False, problem.problem, problem.fix or _REOWN_FIX), None

    no_record_sets = 0
    root_owned: list[str] = [os.path.join(side, backupset.PUSH_RECORD_NAME)]
    for ref in sets:
        if not ref.complete or ref.manifest is None:
            continue
        owner_map = backupset.OwnerMap(ref.manifest.gideon_ids, gideon_ids)
        if owner_map.is_identity:
            no_record_sets += 1
        root_owned.extend(
            (
                os.path.join(ref.path, backupset.MANIFEST_NAME),
                os.path.join(ref.path, backupset.TARBALL_NAME),
            )
        )

    # The skeleton around the sets is nobody's inventory: the staging root is
    # provision's (gideon), everything from sets/ down to each set's files/
    # is root's, and each root directory's own owner is its manifest "." entry.
    skeleton = [backupset.sets_dir(side)]
    for ref in sets:
        if ref.complete:
            skeleton.extend((ref.path, backupset.files_dir(ref.path)))
    owner_claims = backuproots.Claims()
    for path in (*root_owned, *skeleton):
        owner_claims.add_owner(path, 0, 0)
    problem = owner_claims.apply(io, "fetch")
    if problem is not None:
        return StageResult("fetch", False, problem.problem, problem.fix or _REOWN_FIX), None
    staging_root = run_stage(
        io,
        "fetch",
        ["chown", "gideon:gideon", "--", side],
        "re-owned the staging root",
        _REOWN_FIX,
    )
    if not staging_root.ok:
        return staging_root, None
    directory_claims = backuproots.Claims()
    for path in (side, *skeleton):
        directory_claims.add_mode(path, 0o755)
    problem = directory_claims.apply(io, "fetch")
    if problem is not None:
        return StageResult("fetch", False, problem.problem, problem.fix or _REOWN_FIX), None
    file_claims = backuproots.Claims()
    for path in root_owned:
        file_claims.add_mode(path, 0o600 if path.endswith(backupset.TARBALL_NAME) else 0o644)
    problem = file_claims.apply(io, "fetch")
    if problem is not None:
        return StageResult("fetch", False, problem.problem, problem.fix or _REOWN_FIX), None

    replaced = f"{backupset.STAGING}.replaced-{operation_label}"
    ownership = (
        f"{claims.gideon_owned} {REOWN_MAPPED_DETAIL} {gideon_ids.uid}:{gideon_ids.gid}"
        if no_record_sets == 0
        else (
            f"re-owned by their recorded ids "
            f"({no_record_sets} set(s) with {REOWN_NO_RECORD_DETAIL})"
        )
    )
    return (
        StageResult(
            "fetch",
            True,
            f"fetched {snapshot.label}; selected set {selected.label}; "
            f"{registry_clause}"
            f"re-owned {claims.entries_claimed} path(s), {ownership}",
            "",
        ),
        _Fetched(side, replaced, selected, claims.entries_claimed),
    )


def _verify_stage(
    io: Host,
    rendered_dir: PathLike,
    *,
    selected: backupset.SetRef,
    later: tuple[backupset.SetRef, ...] = (),
    repository_only: bool = False,
) -> StageResult:
    if selected.manifest is None:
        return StageResult("verify", False, "selected set has no manifest", _set_fix())
    manifest = selected.manifest
    inventory_roots = backupset.inventory_roots(manifest.checkout)
    if repository_only:
        inventory_roots = tuple(
            root for root in inventory_roots if root.kind is backupset.RootKind.REPOSITORY
        )
    roots = backuproots.held(
        io,
        inventory_roots,
        selected,
        later,
    )
    for root in roots:
        problem = root.verify()
        if problem is not None:
            return StageResult(
                "verify",
                False,
                problem.problem,
                problem.fix or _VERIFY_FIX,
            )

    if not repository_only:
        tarball = os.path.join(selected.path, backupset.TARBALL_NAME)
        try:
            hashed = io.run(["sha256sum", tarball])
        except (OSError, subprocess.SubprocessError) as exc:
            return StageResult("verify", False, f"tarball checksum failed: {exc}", _VERIFY_FIX)
        if hashed.returncode != 0:
            return StageResult(
                "verify",
                False,
                f"tarball checksum failed: {command_detail(hashed)}",
                _VERIFY_FIX,
            )
        try:
            hashes = backupset.parse_sha256sum(hashed.stdout)
        except ValueError as exc:
            return StageResult("verify", False, f"tarball checksum was malformed: {exc}", _VERIFY_FIX)
        if hashes.get(tarball, "").casefold() != manifest.tarball_sha256.casefold():
            return StageResult("verify", False, "tarball checksum does not match the manifest", _VERIFY_FIX)

    for root in roots:
        problem = root.prove(rendered_dir)
        if problem is not None:
            return StageResult(
                "verify",
                False,
                problem.problem,
                problem.fix or _VERIFY_FIX,
            )
    detail = (
        "repository hashes and pgBackRest verification passed; "
        "tarball not checked, every other root left in place"
        if repository_only
        else "backup set hashes and pgBackRest verification passed"
    )
    return StageResult("verify", True, detail, "")


def _stop_stage(io: Host, rendered_dir: PathLike, *, build_box: bool) -> StageResult:
    stopped = run_stage(
        io,
        "stop",
        stack.compose_argv(rendered_dir, "down"),
        "stopped the GIDEON Compose project",
        _stage_fix(),
    )
    if not stopped.ok:
        return stopped
    if not build_box:
        return StageResult(
            "stop",
            True,
            "stopped the GIDEON Compose project; no host registry: not the build box",
            "",
        )
    return run_stage(
        io,
        "stop",
        ["systemctl", "stop", "gideon-registry"],
        "stopped the host registry",
        _REGISTRY_FIX,
    )


def _swap_stage(io: Host, side: str, replaced: str) -> StageResult:
    if not replaced.startswith(backupset.STAGING + ".replaced-"):
        return StageResult("swap", False, f"unsafe replacement path: {replaced}", _stage_fix())
    first = run_stage(
        io,
        "swap",
        ["mv", backupset.STAGING, replaced],
        f"moved live staging to {replaced}",
        _stage_fix(),
    )
    if not first.ok:
        return first
    second = run_stage(
        io,
        "swap",
        ["mv", side, backupset.STAGING],
        "installed the fetched staging directory",
        _stage_fix(),
    )
    if second.ok:
        return second
    # Never leave the box without a staging directory: put the live one back.
    rollback = run_stage(
        io,
        "swap",
        ["mv", replaced, backupset.STAGING],
        "moved the live staging directory back",
        _stage_fix(),
    )
    if rollback.ok:
        return StageResult(
            "swap",
            False,
            f"{second.detail}; the live staging directory was moved back and the "
            f"fetched copy stays in {side}",
            _stage_fix(),
        )
    return StageResult(
        "swap",
        False,
        f"{second.detail}; moving the live staging directory back also failed — "
        f"the live copy is at {replaced} and the fetched copy at {side}",
        f"Move {replaced} back to {backupset.STAGING} by hand, then retry restore.",
    )


def _restore_files_stage(
    io: Host,
    *,
    selected: backupset.SetRef,
    gideon_ids: backupset.AccountIds,
    later: tuple[backupset.SetRef, ...] = (),
) -> tuple[StageResult, tuple[str, ...], int, Mapping[str, backuproots.StoreCounts], Mapping[str, int], tuple[str, ...], frozenset[str]]:
    if selected.manifest is None:
        return StageResult("files", False, "selected set has no manifest", _set_fix()), (), 0, {}, {}, (), frozenset()
    roots = backuproots.held(
        io,
        backupset.inventory_roots(selected.manifest.checkout),
        selected,
        later,
    )
    present_sources = frozenset(root.source for root in roots if root.present)
    restored: list[str] = []
    clauses: list[str] = []
    counts: dict[str, backuproots.StoreCounts] = {}
    added: dict[str, int] = {}
    closing_lines: list[str] = []
    claims = backuproots.Claims()
    for root in roots:
        outcome = root.put_back(gideon_ids)
        if isinstance(outcome, Problem):
            return (
                StageResult(
                    "files",
                    False,
                    outcome.problem,
                    outcome.fix or _stage_fix(),
                ),
                tuple(restored),
                0,
                counts,
                added,
                tuple(closing_lines),
                present_sources,
            )
        restored.extend(outcome.restored)
        clauses.extend(outcome.clauses)
        counts.update(outcome.counts)
        added.update(outcome.added)
        closing_lines.extend(outcome.closing_lines)
        claims.merge(outcome.claims)
    problem = claims.apply(io, "files")
    if problem is not None:
        return (
            StageResult(
                "files",
                False,
                problem.problem,
                problem.fix or _REOWN_FIX,
            ),
            tuple(restored),
            0,
            counts,
            added,
            tuple(closing_lines),
            present_sources,
        )
    owner_map = backupset.OwnerMap(selected.manifest.gideon_ids, gideon_ids)
    ownership = (
        f"re-owned by the recorded ids ({REOWN_NO_RECORD_DETAIL})"
        if owner_map.is_identity
        else f"{claims.gideon_owned} {REOWN_MAPPED_DETAIL} {gideon_ids.uid}:{gideon_ids.gid}"
    )
    detail = "; ".join((f"restored {', '.join(restored)}", *clauses, ownership))
    return (
        StageResult(
            "files",
            True,
            detail,
            "",
        ),
        tuple(restored),
        claims.owner_paths,
        counts,
        added,
        tuple(closing_lines),
        present_sources,
    )


def _backup_timeline(io: Host, rendered_dir: PathLike, label: str) -> int | StageResult:
    """The timeline the set's backup was taken on, from the repository now in place."""

    # The stack is stopped here: the one-off container reads the repository.
    found = pgbackrest.info(io, rendered_dir, running=False)
    if not found.ok:
        return StageResult("postgres", False, found.problem or "pgBackRest info failed", found.fix)
    for record in found.infos:
        if record.label == label:
            if record.timeline is None:
                return StageResult(
                    "postgres", False, f"backup {label} records no archive start", _set_fix()
                )
            return record.timeline
    return StageResult(
        "postgres",
        False,
        f"the repository holds no backup {label} for the selected set",
        _set_fix(),
    )


def _postgres_stage(
    io: Host,
    rendered_dir: PathLike,
    *,
    at: datetime | None,
    boundary: datetime,
    backup_label: str,
    archive_end: bool = False,
) -> StageResult:
    """Restore the selected backup to its target or to the archive's end.

    Three rehearsals taught the three parts. The backup is named (``--set``):
    left to pgBackRest, the backup is auto-selected as the newest that stopped
    strictly before the target at second granularity, which after a rollback
    is one on a diverged timeline. The timeline is named
    (``--target-timeline``): pgBackRest's default follows the cluster's
    current timeline, which after two promotions had forked before the
    backup's own LSN and could not reach it. The target is named
    (``--type=time``): ``--at`` when given, else the set's archive boundary,
    so the restore ends where the set's record says it does.

    The fourth lesson is a damaged cluster's archive end: ``--type=default``
    follows ``--target-timeline=latest`` with no recovery target or target
    action, so recovery ends when the archive runs out rather than failing
    on a time target no archived commit passes.
    """

    args = ["restore", "--delta", f"--set={backup_label}"]
    if archive_end:
        args.extend(("--type=default", "--target-timeline=latest"))
    else:
        timeline = _backup_timeline(io, rendered_dir, backup_label)
        if isinstance(timeline, StageResult):
            return timeline
        target = at if at is not None else boundary
        args.extend((
            f"--target-timeline={timeline}",
            "--type=time",
            f"--target={backupset.pgbackrest_target(target)}",
            "--target-action=promote",
        ))
    return run_stage(
        io,
        "postgres",
        pgbackrest.run_argv(rendered_dir, *args),
        "restored Postgres from pgBackRest",
        _stage_fix(),
        timeout=_RESTORE_TIMEOUT,
    )


def _stores_stage(
    io: Host,
    rendered_dir: PathLike,
    *,
    site_path: PathLike,
    lock_path: PathLike,
    images_path: PathLike,
    models_path: PathLike,
    root: Path,
    sleep: Callable[[float], None],
    source: str,
    selected: backupset.SetRef,
    snapshot: backupset.RemoteSnapshot | None,
    at: datetime | None,
    pre_restore_label: str | None,
    roots_restored: Sequence[str],
    store_counts: Mapping[str, backuproots.StoreCounts],
    later_labels: Sequence[str],
    added: Mapping[str, int],
    reowned: int,
    replaced: str | None,
    run_id: str,
    build_box: bool,
    present_sources: frozenset[str],
    archive_end: str | None = None,
    reach_wait: float = stores.REPLAY_WAIT_SECONDS,
) -> tuple[StageResult, level.LevelReport | None, _Reach | None]:
    # Only the build box runs the registry step, so only it has a unit to start.
    if build_box:
        started = run_stage(
            io,
            "stores",
            ["systemctl", "start", "gideon-registry"],
            "started the host registry",
            _REGISTRY_FIX,
        )
        if not started.ok:
            return started, None, None
    # The restored render is the making host's: a store started from it would
    # reserve that host's memory, so the tier starts from this host's render.
    try:
        outcome = render_to_disk(
            io,
            rendered_dir=rendered_dir,
            site_path=site_path,
            lock_path=lock_path,
            images_path=images_path,
            models_path=models_path,
            root=root,
            diff=False,
            command="restore",
        )
    except Exception as exc:  # noqa: BLE001  # command boundary must not traceback
        return StageResult(
            "stores", False,
            f"render for this host failed: {type(exc).__name__}: {exc}",
            apply.RENDER_FIX,
        ), None, None
    if outcome.exit_code != 0 or outcome.rendered is None or outcome.inputs is None:
        return StageResult("stores", False, "render inputs were refused", apply.RENDER_FIX), None, None
    up = run_stage(
        io,
        "stores",
        stack.compose_argv(rendered_dir, "up", "-d", *STORE_SERVICES),
        "started the store tier",
        _stage_fix(),
    )
    if not up.ok:
        return up, None, None
    ready, detail, service = apply.wait_for_store_tier(io, rendered_dir, sleep)
    if not ready:
        return StageResult(
            "stores",
            False,
            detail,
            stack.logs_fix(rendered_dir, service),
        ), None, None

    reach: _Reach | None = None
    if archive_end is not None:
        replayed = stores.replay_reach(io, rendered_dir, sleep=sleep, wait_seconds=reach_wait)
        if isinstance(replayed, Problem):
            return StageResult("stores", False, replayed.problem, replayed.fix), None, None
        try:
            per_log = pgbackrest.segments_per_log(replayed.segment_size)
            difference = (
                pgbackrest.segment_ordinal(archive_end, per_log)
                - pgbackrest.segment_ordinal(replayed.segment, per_log)
            )
        except ValueError as exc:
            return StageResult(
                "stores", False, f"cannot compare replay with the archive end: {exc}",
                stack.logs_fix(rendered_dir, "postgres"),
            ), None, None
        reach = _Reach(
            replayed.instant, replayed.lsn, replayed.segment, archive_end,
            max(0, difference), difference < 0,
        )

    # The replaced staging directory is the local fallback while the indexes
    # are put back and levelled, so it goes only after both succeed. The worker
    # is down throughout, so no job races the demotions.
    recovered: tuple[derivedstores.StoreRecovery, ...] | Problem = ()
    if archive_end is None:
        recovered = derivedstores.recover_all(
            io, rendered_dir, present_sources=present_sources
        )
    if isinstance(recovered, Problem):
        return StageResult("stores", False, recovered.problem, recovered.fix), None, reach
    level_report = level.level_all(io, rendered_dir, recovered=recovered, command_path="restore")
    if isinstance(level_report, Problem):
        return StageResult("stores", False, level_report.problem, level_report.fix), None, reach

    replaced_removed = False
    if replaced is not None:
        if not replaced.startswith(backupset.STAGING + ".replaced-"):
            return StageResult("stores", False, f"unsafe replacement path: {replaced}", _stage_fix()), level_report, reach
        removed = run_stage(
            io,
            "stores",
            ["rm", "-rf", replaced],
            f"removed replaced staging directory {replaced}",
            _stage_fix(),
        )
        if not removed.ok:
            return removed, level_report, reach
        replaced_removed = True

    detail_row: dict[str, object] = {
        "source": source,
        "set_label": selected.label,
        "snapshot_label": snapshot.label if snapshot is not None else None,
        "at": at.isoformat() if at is not None else None,
        "pre_restore_label": pre_restore_label,
        "roots_restored": tuple(roots_restored),
        "store": {name: {"added": value.added, "left_out": value.left_out} for name, value in store_counts.items()},
        "later_labels": list(later_labels),
        "added": dict(added),
        "reowned": reowned,
        "replaced_removed": replaced_removed,
        "indexes": derivedstores.recovery_audit(recovered),
        "level": level.level_audit(level_report),
    }
    if reach is not None:
        detail_row.update({
            "reached": reach.instant.isoformat() if reach.instant is not None else None,
            "reached_lsn": reach.lsn,
            "reached_segment": reach.segment,
            "archive_end": reach.archive_end,
            "archive_end_reached": reach.short_by == 0,
        })
    row = audit.AuditRow(
        run_id,
        "restore",
        None,
        None,
        None,
        (),
        detail_row,
    )
    problem = audit.write_rows(io, rendered_dir, (row,))
    if problem is not None:
        return StageResult(
            "stores",
            False,
            f"restore audit write failed: {problem}",
            stack.logs_fix(rendered_dir, "postgres"),
        ), level_report, reach
    clauses = "; ".join((
        *(derivedstores.recovery_clause(result) for result in recovered),
        *level.level_clauses(level_report),
    ))
    if reach is not None:
        clauses = f"reached {_reach_detail(reach)}" + (f"; {clauses}" if clauses else "")
    return StageResult(
        "stores",
        True,
        (
            f"frontend and ingress down; the store tier running; {clauses}; "
            "no host registry: not the build box"
            if not build_box
            else f"frontend and ingress down; the store tier running; {clauses}; "
            "the host registry running"
        ),
        "",
    ), level_report, reach


def _next_stage(
    io: Host,
    selected: backupset.SetRef,
    *,
    build_box: bool,
    has_gideon_ids: bool,
    closing_lines: tuple[str, ...],
    level_lines: tuple[str, ...] = (),
    reach: _Reach | None = None,
) -> tuple[StageResult, tuple[str, ...]]:
    """State the terminal state and the next steps, including index resumes.

    Outside an archive-end replay, whether the secrets on disk are the
    restored cluster's is decided by the set's keyed fingerprint against
    the live secret directory, never by a file's presence: apply regenerates
    the files on a rebuilt box. An archive-end replay keeps the live secrets.

    A set that records no ``gideon`` ids was re-owned by the ids it holds,
    so its next steps name ``host provision`` — which re-owns the managed
    ``/data`` directories — before the apply.
    """

    lines: tuple[str, ...]
    if reach is not None:
        lines = (
            "Secrets: the secrets on disk are the live box's; the replayed record "
            "carries every rotation up to the archive end, so nothing is decrypted.",
            f"Reached: {_reach_detail(reach)}",
        )
    else:
        tarball = os.path.join(selected.path, backupset.TARBALL_NAME)
        kept = (
            selected.manifest is not None
            and secrets.fingerprint(io) == selected.manifest.secrets_fingerprint
        )
        if kept:
            lines = (
                f"Secrets: the secrets on disk are the set's; the tarball is at {tarball}",
            )
        else:
            lines = (
                (
                    "Secrets: the secrets on disk are not the set's (a rebuilt box): decrypt "
                    "the tarball with the office's age identity: age -d -i "
                    f"<identity file kept off-box> {tarball} | tar -x -C /etc/gideon"
                ),
            )
    lines += closing_lines
    next_commands = ", then ".join((
        report.command("apply"), *level_lines, report.command("backup run --full"),
    ))
    if has_gideon_ids:
        lines += (f"Next: {next_commands}",)
    else:
        # This set was re-owned by the ids it records, so the managed /data
        # directories still need provision's own re-own before the apply.
        lines += (
            f"Next: {report.command('host provision')} (the first provision's "
            "mode flags), because this set records no gideon ids, then "
            f"{next_commands}",
        )
    return (
        StageResult(
            "next",
            True,
            (
                "frontend and ingress down; the store tier running; no host registry: "
                "not the build box"
                if not build_box
                else "frontend and ingress down; the store tier and the host registry running"
            ),
            "",
        ),
        lines,
    )


def run_restore(
    args: object,
    *,
    host: LockingHost | None = None,
    rendered_dir: PathLike = _RENDERED_DIR,
    site_path: PathLike = _SITE_PATH,
    lock_path: PathLike | None = None,
    images_path: PathLike | None = None,
    models_path: PathLike | None = None,
    root: PathLike | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Verify a selected backup source before replacing live state."""

    io = host or RealHost()
    if io.geteuid() != 0:
        return _refuse("root is required.", _root_fix())
    effective_now = aware_now(now)
    if effective_now is None:
        return _refuse(
            "restore's clock value must be timezone-aware.",
            "Use an aware UTC time, then retry.",
        )
    outcome = backuplock.take(io, command="restore", now=effective_now)
    if outcome.problem is not None:
        return _refuse(outcome.problem.problem, outcome.problem.fix)
    checkout = Path(__file__).parents[2] if root is None else Path(root)
    actual_lock = checkout / "host.lock" if lock_path is None else lock_path
    actual_images = checkout / "images.lock" if images_path is None else images_path
    actual_models = checkout / "models.lock" if models_path is None else models_path
    try:
        return _restore_body(
            args,
            io=io,
            rendered_dir=rendered_dir,
            site_path=site_path,
            lock_path=actual_lock,
            images_path=actual_images,
            models_path=actual_models,
            root=checkout,
            effective_now=effective_now,
            sleep=sleep,
        )
    finally:
        # A nested run is its holder's; the holder releases.
        if outcome.state is backuplock.State.HELD:
            backuplock.release(io)


def _restore_body(
    args: object,
    *,
    io: LockingHost,
    rendered_dir: PathLike,
    site_path: PathLike,
    lock_path: PathLike,
    images_path: PathLike,
    models_path: PathLike,
    root: Path,
    effective_now: datetime,
    sleep: Callable[[float], None],
) -> int:
    build_box = nogpu.is_build_box(io)
    prerequisites = _preconditions(
        io,
        args,
        build_box=build_box,
        rendered_dir=rendered_dir,
        site_path=site_path,
    )
    if prerequisites is None:
        return 1
    config, source, at, gideon_ids, archive_end = prerequisites

    if archive_end:
        select_result, selection = _select_archive_end(io, rendered_dir)
    elif source == "staging":
        select_result, selection = _select_staging(io, at, label=getattr(args, "set", None))
    else:
        select_result, selection = _select_target(io, config, at)
    print_stage(select_result)
    if not select_result.ok or selection is None:
        return 1
    # --at as parsed, or the named set's archive boundary: the Postgres target.
    restore_at = selection.at

    pre_result, pre_restore_label = _pre_restore_stage(
        io,
        source=source,
        rendered_dir=rendered_dir,
        site_path=site_path,
        root=root,
        now=effective_now,
        archive_end=archive_end,
    )
    if pre_result.ok and source == "staging" and at is not None and not archive_end:
        bound_failure = _staging_bound(io, at=at)
        if bound_failure is not None:
            pre_result = bound_failure
    print_stage(pre_result)
    if not pre_result.ok:
        return 1

    operation_label = backupset.nightly_label(effective_now)
    fetched: _Fetched | None = None
    selected = selection.set_ref
    if source == "target":
        assert selection.snapshot is not None
        roots = backupset.inventory_roots(os.fspath(root))
        fetch_result, fetched = _fetch_stage(
            io,
            config,
            rendered_dir=rendered_dir,
            build_box=build_box,
            sleep=sleep,
            snapshot=selection.snapshot,
            at=at,
            operation_label=operation_label,
            gideon_ids=gideon_ids,
            roots=roots,
        )
        print_stage(fetch_result)
        if not fetch_result.ok or fetched is None:
            return 1
        selected = fetched.selected
    else:
        print_stage(StageResult("fetch", True, "skipped (staging source)", ""))
    assert selected is not None

    later = _later_sets(
        io, selected, at=at,
        staging=fetched.side if fetched is not None else backupset.STAGING,
    )
    if isinstance(later, StageResult):
        print_stage(later)
        return 1

    verify_result = _verify_stage(
        io,
        rendered_dir,
        selected=selected,
        later=later,
        repository_only=archive_end,
    )
    print_stage(verify_result)
    if not verify_result.ok:
        return 1

    stop_result = _stop_stage(io, rendered_dir, build_box=build_box)
    print_stage(stop_result)
    if not stop_result.ok:
        return 1

    if source == "target":
        assert fetched is not None
        swap_result = _swap_stage(io, fetched.side, fetched.replaced)
        print_stage(swap_result)
        if not swap_result.ok:
            return 1
        selected = backupset.SetRef(
            selected.label,
            backupset.set_dir(selected.label),
            selected.finished,
            selected.complete,
            selected.manifest,
        )
        later = tuple(
            backupset.SetRef(
                ref.label,
                backupset.set_dir(ref.label),
                ref.finished,
                ref.complete,
                ref.manifest,
            )
            for ref in later
        )
    else:
        print_stage(StageResult("swap", True, "skipped (staging source)", ""))

    if archive_end:
        files_result = StageResult("files", True, "skipped (every root left in place)", "")
        roots_restored: tuple[str, ...] = ()
        file_reowned = 0
        store_counts: Mapping[str, backuproots.StoreCounts] = {}
        added: Mapping[str, int] = {}
        closing_lines: tuple[str, ...] = ()
        present_sources: frozenset[str] = frozenset()
    else:
        files_result, roots_restored, file_reowned, store_counts, added, closing_lines, present_sources = _restore_files_stage(
            io,
            selected=selected,
            gideon_ids=gideon_ids,
            later=later,
        )
    print_stage(files_result)
    if not files_result.ok:
        return 1

    reowned = file_reowned + (fetched.reowned if fetched is not None else 0)
    if selected.manifest is None:
        print_stage(StageResult("postgres", False, "selected set has no manifest", _set_fix()))
        return 1
    postgres_result = _postgres_stage(
        io,
        rendered_dir,
        at=restore_at,
        boundary=selected.manifest.archive_through,
        backup_label=selected.manifest.pgbackrest_label,
        archive_end=archive_end,
    )
    print_stage(postgres_result)
    if not postgres_result.ok:
        return 1

    stores_result, level_report, reach = _stores_stage(
        io,
        rendered_dir,
        site_path=site_path,
        lock_path=lock_path,
        images_path=images_path,
        models_path=models_path,
        root=root,
        sleep=sleep,
        source=source,
        selected=selected,
        snapshot=selection.snapshot,
        at=restore_at,
        pre_restore_label=pre_restore_label,
        roots_restored=roots_restored,
        store_counts=store_counts,
        later_labels=tuple(ref.label for ref in later),
        added=added,
        reowned=reowned,
        replaced=fetched.replaced if fetched is not None else None,
        run_id=str(uuid.uuid4()),
        build_box=build_box,
        present_sources=present_sources,
        archive_end=selection.archive_end,
        reach_wait=stores.REPLAY_WAIT_SECONDS,
    )
    print_stage(stores_result)
    if not stores_result.ok:
        return 1
    assert level_report is not None

    next_result, next_lines = _next_stage(
        io,
        selected,
        build_box=build_box,
        has_gideon_ids=selected.manifest.gideon_ids is not None,
        closing_lines=closing_lines,
        level_lines=level.next_lines(level_report),
        reach=reach,
    )
    print_stage(next_result)
    if next_result.ok:
        for line in next_lines:
            print(line)
    return int(not next_result.ok)
