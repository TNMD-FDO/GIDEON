"""Verified staging and off-box restore orchestration.

Each entry holds the backup lock (``backuplock.py``) for its whole run, so a
backup, a push, and a restore never overlap; ``backuplock.py`` owns the rules.
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
    nogpu,
    pgbackrest,
    report,
    secrets,
    site,
    sshtarget,
    stack,
)
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


def _account_fix() -> str:
    return f"Run {report.command('host provision')}, then retry."


def _push_fix() -> str:
    return f"Run {report.command('backup push')}, then retry."


def _stage_fix() -> str:
    return f"Run {report.command('apply')}, then retry."


@dataclass(frozen=True, slots=True)
class _Selection:
    source: str
    at: datetime | None
    snapshot: backupset.RemoteSnapshot | None
    record: backupset.PushRecord | None
    set_ref: backupset.SetRef | None


@dataclass(frozen=True, slots=True)
class _Fetched:
    side: str
    replaced: str
    selected: backupset.SetRef
    reowned: int


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
) -> tuple[SiteConfig, str, datetime | None, backupset.AccountIds] | None:
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
    return config, source, at, gideon_ids


def _selection_detail(selection: _Selection) -> str:
    snapshot = selection.snapshot.label if selection.snapshot is not None else "-"
    set_label = (
        selection.set_ref.label
        if selection.set_ref is not None
        else "chosen after fetch"
    )
    target_time = selection.at.isoformat() if selection.at is not None else "latest"
    return (
        f"source={selection.source}; snapshot={snapshot}; set={set_label}; "
        f"at={target_time}"
    )


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
    return StageResult("select", True, _selection_detail(selected), ""), selected


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


def _fetch_stage(
    io: Host,
    config: SiteConfig,
    *,
    rendered_dir: PathLike,
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

    for landed_root in backuproots.landed(io, roots, side):
        problem = landed_root.arrive(rendered_dir)
        if problem is not None:
            return StageResult("fetch", False, problem.problem, problem.fix or _REOWN_FIX), None

    try:
        sets = backupset.list_sets(io, staging=side)
    except OSError as exc:
        return StageResult("fetch", False, f"cannot list fetched sets: {exc}", _set_fix()), None
    newest = next((ref for ref in sets if ref.complete and ref.manifest is not None), None)
    if newest is None:
        return StageResult("fetch", False, "fetched snapshot has no complete backup set", _set_fix()), None
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

    selected = backupset.select_set(sets, at=at)
    if isinstance(selected, Problem):
        return StageResult("fetch", False, selected.problem, selected.fix), None
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
) -> StageResult:
    if selected.manifest is None:
        return StageResult("verify", False, "selected set has no manifest", _set_fix())
    manifest = selected.manifest
    roots = backuproots.held(
        io,
        backupset.inventory_roots(manifest.checkout),
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
    return StageResult("verify", True, "backup set hashes and pgBackRest verification passed", "")


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
) -> tuple[StageResult, tuple[str, ...], int, Mapping[str, backuproots.StoreCounts], Mapping[str, int], tuple[str, ...]]:
    if selected.manifest is None:
        return StageResult("files", False, "selected set has no manifest", _set_fix()), (), 0, {}, {}, ()
    roots = backuproots.held(
        io,
        backupset.inventory_roots(selected.manifest.checkout),
        selected,
        later,
    )
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
) -> StageResult:
    """Restore the selected set's own backup, along its own timeline, to its target.

    Three rehearsals taught the three parts. The backup is named (``--set``):
    left to pgBackRest, the backup is auto-selected as the newest that stopped
    strictly before the target at second granularity, which after a rollback
    is one on a diverged timeline. The timeline is named
    (``--target-timeline``): pgBackRest's default follows the cluster's
    current timeline, which after two promotions had forked before the
    backup's own LSN and could not reach it. The target is named
    (``--type=time``): ``--at`` when given, else the set's archive boundary,
    so the restore ends where the set's record says it does.
    """

    timeline = _backup_timeline(io, rendered_dir, backup_label)
    if isinstance(timeline, StageResult):
        return timeline
    target = at if at is not None else boundary
    args = [
        "restore",
        "--delta",
        f"--set={backup_label}",
        f"--target-timeline={timeline}",
        "--type=time",
        f"--target={backupset.pgbackrest_target(target)}",
        "--target-action=promote",
    ]
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
) -> StageResult:
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
            return started
    up = run_stage(
        io,
        "stores",
        stack.compose_argv(rendered_dir, "up", "-d", *STORE_SERVICES),
        "started the store tier",
        _stage_fix(),
    )
    if not up.ok:
        return up
    ready, detail, service = apply.wait_for_services(
        io,
        rendered_dir,
        STORE_SERVICES,
        sleep,
        exact=False,
        require_healthy=True,
    )
    if not ready:
        return StageResult(
            "stores",
            False,
            detail,
            stack.logs_fix(rendered_dir, service),
        )

    replaced_removed = False
    if replaced is not None:
        if not replaced.startswith(backupset.STAGING + ".replaced-"):
            return StageResult("stores", False, f"unsafe replacement path: {replaced}", _stage_fix())
        removed = run_stage(
            io,
            "stores",
            ["rm", "-rf", replaced],
            f"removed replaced staging directory {replaced}",
            _stage_fix(),
        )
        if not removed.ok:
            return removed
        replaced_removed = True

    detail_row: Mapping[str, object] = {
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
    }
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
        )
    return StageResult(
        "stores",
        True,
        (
            "frontend and ingress down; the store tier running; no host registry: "
            "not the build box"
            if not build_box
            else "frontend and ingress down; the store tier and the host registry running"
        ),
        "",
    )


def _next_stage(
    io: Host,
    selected: backupset.SetRef,
    *,
    build_box: bool,
    has_gideon_ids: bool,
    closing_lines: tuple[str, ...],
) -> tuple[StageResult, tuple[str, ...]]:
    """State the terminal state and the two next steps.

    Whether the secrets on disk are the restored cluster's is decided by the
    set's keyed fingerprint against the live secret directory, never by a
    file's presence: apply regenerates the files on a rebuilt box.

    A set that records no ``gideon`` ids was re-owned by the ids it holds,
    so its next steps name ``host provision`` — which re-owns the managed
    ``/data`` directories — before the apply.
    """

    tarball = os.path.join(selected.path, backupset.TARBALL_NAME)
    kept = (
        selected.manifest is not None
        and secrets.fingerprint(io) == selected.manifest.secrets_fingerprint
    )
    lines: tuple[str, ...]
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
    if has_gideon_ids:
        lines += (
            f"Next: {report.command('apply')}, then "
            f"{report.command('backup run --full')}",
        )
    else:
        # This set was re-owned by the ids it records, so the managed /data
        # directories still need provision's own re-own before the apply.
        lines += (
            f"Next: {report.command('host provision')} (the first provision's "
            "mode flags), because this set records no gideon ids, then "
            f"{report.command('apply')}, then "
            f"{report.command('backup run --full')}",
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
    try:
        return _restore_body(
            args,
            io=io,
            rendered_dir=rendered_dir,
            site_path=site_path,
            root=root,
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
    root: PathLike | None,
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
    config, source, at, gideon_ids = prerequisites

    if source == "staging":
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
    )
    if pre_result.ok and source == "staging" and at is not None:
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
        checkout = Path(__file__).parents[2] if root is None else Path(root)
        roots = backupset.inventory_roots(os.fspath(checkout))
        fetch_result, fetched = _fetch_stage(
            io,
            config,
            rendered_dir=rendered_dir,
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

    files_result, roots_restored, file_reowned, store_counts, added, closing_lines = _restore_files_stage(
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
    )
    print_stage(postgres_result)
    if not postgres_result.ok:
        return 1

    stores_result = _stores_stage(
        io,
        rendered_dir,
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
    )
    print_stage(stores_result)
    if not stores_result.ok:
        return 1

    next_result, next_lines = _next_stage(
        io,
        selected,
        build_box=build_box,
        has_gideon_ids=selected.manifest.gideon_ids is not None,
        closing_lines=closing_lines,
    )
    print_stage(next_result)
    if next_result.ok:
        for line in next_lines:
            print(line)
    return int(not next_result.ok)
