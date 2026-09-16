"""Verified staging and off-box restore orchestration (§19.2)."""

import argparse
import os
import stat
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
    backupset,
    nogpu,
    pgbackrest,
    secrets,
    site,
    sshtarget,
    stack,
)
from gideon.host.render.compose import STORE_SERVICES
from gideon.host.render.pgbackrest import REPOSITORY_PATH
from gideon.host.report import (
    Problem,
    StageResult,
    command_detail,
    print_stage,
    refusal,
)
from gideon.host.site import SiteConfig
from gideon.host.stages import aware_now, run_stage, site_problem
from gideon.host.sysio import Host, PathLike, RealHost

_SITE_PATH: Final = "/etc/gideon/site.yaml"
_RENDERED_DIR: Final = "/etc/gideon/rendered"
_ROOT_FIX: Final = "Run sudo python3 -m gideon restore --from <staging|target>, then retry."
_APPLY_FIX: Final = "Run sudo python3 -m gideon apply, then retry."
_TARGET_FIX: Final = (
    "Authorize the backup key and check the target per the office-services "
    "runbook §3, then re-run restore."
)
_SET_FIX: Final = "Run sudo python3 -m gideon backup run, then retry."
_PUSH_FIX: Final = "Run sudo python3 -m gideon backup push, then retry."
_SELECT_FIX: Final = "Choose an earlier --at, or omit it for the latest state."
_STAGE_FIX: Final = "Run sudo python3 -m gideon apply, then retry."
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
_CHUNK_SIZE: Final = 200


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
) -> tuple[SiteConfig, str, datetime | None] | None:
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

    try:
        compose_path = Path(rendered_dir) / "compose.yaml"
        if not io.exists(compose_path):
            _refuse(
                f"rendered Compose file is missing: {compose_path}.",
                _APPLY_FIX,
            )
            return None
    except OSError as exc:
        _refuse(f"cannot inspect restore prerequisites: {exc}.", _APPLY_FIX)
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
    return config, source, at


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
            StageResult("select", False, f"cannot list backup sets: {exc}", _SET_FIX),
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
        return StageResult("select", False, "selected set has no manifest", _SET_FIX), None
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
                _PUSH_FIX,
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


_PARTIAL_FIX: Final = (
    "Start the whole stack with sudo python3 -m gideon apply, or stop it with "
    "docker compose -f /etc/gideon/rendered/compose.yaml down, then retry restore."
)


def _pre_restore_stage(
    io: Host,
    *,
    source: str,
    rendered_dir: PathLike,
    site_path: PathLike,
    root: PathLike | None,
    now: datetime,
) -> tuple[StageResult, str | None]:
    # The safety set needs Postgres; a stack whose other services are still
    # serving while Postgres is down would be replaced without one, so the
    # decision is made on every service, not on Postgres alone.
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
    if not _postgres_answers(io, rendered_dir):
        return (
            StageResult(
                "pre-restore",
                False,
                f"the stack is partially running ({', '.join(sorted(running))}) and "
                "Postgres does not answer, so the pre-restore set cannot be taken",
                _PARTIAL_FIX,
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
                _STAGE_FIX,
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
                    _STAGE_FIX,
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
        return StageResult("pre-restore", False, f"cannot refresh backup sets: {exc}", _SET_FIX)
    newest = next((ref for ref in sets if ref.complete and ref.manifest is not None), None)
    if newest is None or newest.manifest is None:
        return StageResult("pre-restore", False, "no complete backup set remains", _SET_FIX)
    if at > newest.manifest.archive_through:
        return StageResult(
            "pre-restore",
            False,
            "requested --at is after the newest local archive boundary "
            f"{newest.manifest.archive_through.isoformat()}",
            _SELECT_FIX,
        )
    return None


def _chunks(values: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(values[index : index + _CHUNK_SIZE])
        for index in range(0, len(values), _CHUNK_SIZE)
    )


# Owner groups are keyed by (uid, gid, is_link): a symlink is re-owned with
# ``chown -h`` so its referent — possibly outside the tree — is never touched,
# and it is never chmod-ed (a link has no mode of its own).
OwnerKey = tuple[int, int, bool]


def _run_grouped_chown(
    io: Host,
    paths_by_owner: Mapping[OwnerKey, Sequence[str]],
    *,
    stage: str,
) -> StageResult | None:
    for (uid, gid, is_link), paths in sorted(paths_by_owner.items()):
        for chunk in _chunks(tuple(paths)):
            result = run_stage(
                io,
                stage,
                ["chown", *(("-h",) if is_link else ()), f"{uid}:{gid}", "--", *chunk],
                f"re-owned {len(chunk)} path(s)",
                _REOWN_FIX,
            )
            if not result.ok:
                return result
    return None


def _run_grouped_chmod(
    io: Host,
    paths_by_mode: Mapping[int, Sequence[str]],
    *,
    stage: str,
) -> StageResult | None:
    for mode, paths in sorted(paths_by_mode.items()):
        for chunk in _chunks(tuple(paths)):
            result = run_stage(
                io,
                stage,
                ["chmod", f"{mode:04o}", "--", *chunk],
                f"applied mode {mode:04o} to {len(chunk)} path(s)",
                _REOWN_FIX,
            )
            if not result.ok:
                return result
    return None


def _entry_path(set_ref: backupset.SetRef, root_name: str, path: str) -> str:
    return os.path.join(
        set_ref.path,
        backupset.FILES_DIR,
        root_name,
        path,
    )


def _snapshotted_root_names(checkout: str) -> frozenset[str]:
    return frozenset(
        root.name
        for root in backupset.inventory_roots(checkout)
        if root.snapshotted
    )


_PHYSICAL_FIX: Final = (
    "The fetched or restored tree does not match its manifest; re-run "
    "sudo python3 -m gideon backup push --verify-all on the source box, "
    "then retry restore."
)


def _validate_physical(
    io: Host,
    *,
    stage: str,
    base: str,
    entries: Sequence[backupset.Entry],
    exact: bool = False,
) -> StageResult | None:
    """Refuse unless every inventoried path exists physically with its declared kind.

    ``find`` never follows symlinks, so a path beneath a link is absent from
    the listing and a link where the manifest records a file or directory is
    listed as a link: both refuse before any ownership or mode is applied,
    which is what keeps a corrupt tree from steering ``chown`` or ``chmod``
    at a referent outside it.
    """

    try:
        listed = io.run(["find", base, "-printf", backupset.FIND_FORMAT])
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult(stage, False, f"cannot list {base}: {exc}", _PHYSICAL_FIX)
    if listed.returncode != 0:
        return StageResult(stage, False, f"cannot list {base}: {command_detail(listed)}", _PHYSICAL_FIX)
    try:
        kinds = {entry.path: entry.kind for entry in backupset.parse_find_listing(listed.stdout)}
    except ValueError as exc:
        return StageResult(stage, False, f"listing of {base} is malformed: {exc}", _PHYSICAL_FIX)
    mismatched = sum(1 for entry in entries if kinds.get(entry.path) != entry.kind)
    if mismatched:
        return StageResult(
            stage,
            False,
            f"{mismatched} inventoried path(s) under {base} are not what the manifest "
            "declares (a link in place of a file or directory, or a path beneath a link)",
            _PHYSICAL_FIX,
        )
    if exact:
        # A snapshotted root is the manifest's set and nothing else: a path the
        # manifest never inventoried (an empty root's stray file included) is
        # refused before it can reach live state. The repository is never
        # judged this way — later backups legitimately add files there, and
        # pgBackRest's own verify covers it.
        inventoried = {entry.path for entry in entries}
        extraneous = sorted(
            path for path in kinds if path not in inventoried and path not in ("", backupset.ROOT_ENTRY)
        )
        if extraneous:
            return StageResult(
                stage,
                False,
                f"{len(extraneous)} path(s) under {base} are not in the manifest: {extraneous[0]}",
                _PHYSICAL_FIX,
            )
    return None


def _root_owned_metadata(
    io: Host,
    side: str,
    sets: Sequence[backupset.SetRef],
) -> StageResult | None:
    paths_by_owner: dict[OwnerKey, list[str]] = {}
    paths_by_mode: dict[int, list[str]] = {}
    root_owned: list[str] = [os.path.join(side, backupset.PUSH_RECORD_NAME)]
    for set_ref in sets:
        if not set_ref.complete or set_ref.manifest is None:
            continue
        manifest = set_ref.manifest
        snapshotted = _snapshotted_root_names(manifest.checkout)
        for root_name, entries in manifest.inventory.items():
            if root_name not in snapshotted:
                continue
            failure = _validate_physical(
                io,
                stage="fetch",
                base=_entry_path(set_ref, root_name, ""),
                entries=entries,
            )
            if failure is not None:
                return failure
            for entry in entries:
                path = _entry_path(set_ref, root_name, entry.path)
                is_link = entry.kind == "l"
                paths_by_owner.setdefault((entry.uid, entry.gid, is_link), []).append(path)
                if not is_link:
                    paths_by_mode.setdefault(entry.mode, []).append(path)
        root_owned.extend(
            (
                os.path.join(set_ref.path, backupset.MANIFEST_NAME),
                os.path.join(set_ref.path, backupset.TARBALL_NAME),
            )
        )

    owner_result = _run_grouped_chown(io, paths_by_owner, stage="fetch")
    if owner_result is not None:
        return owner_result
    mode_result = _run_grouped_chmod(io, paths_by_mode, stage="fetch")
    if mode_result is not None:
        return mode_result

    # The skeleton around the sets is nobody's inventory: the staging root is
    # provision's (gideon), everything from sets/ down to each set's files/
    # is root's, and each root directory's own owner is its manifest "." entry.
    skeleton: list[str] = [os.path.join(side, "sets")]
    for set_ref in sets:
        if set_ref.complete:
            skeleton.extend((set_ref.path, os.path.join(set_ref.path, backupset.FILES_DIR)))
    owner_result = _run_grouped_chown(
        io,
        {(0, 0, False): root_owned + skeleton},
        stage="fetch",
    )
    if owner_result is not None:
        return owner_result
    staging_root = run_stage(
        io,
        "fetch",
        ["chown", "gideon:gideon", "--", side],
        "re-owned the staging root",
        _REOWN_FIX,
    )
    if not staging_root.ok:
        return staging_root
    skeleton_modes = _run_grouped_chmod(io, {0o755: [side, *skeleton]}, stage="fetch")
    if skeleton_modes is not None:
        return skeleton_modes
    paths_by_root_mode: dict[int, list[str]] = {
        0o644: [
            path
            for path in root_owned
            if not path.endswith(backupset.TARBALL_NAME)
        ],
        0o600: [
            path for path in root_owned if path.endswith(backupset.TARBALL_NAME)
        ],
    }
    return _run_grouped_chmod(io, paths_by_root_mode, stage="fetch")


def _repository_mode_overrides(
    io: Host,
    side: str,
    newest: backupset.SetRef,
) -> StageResult | None:
    if newest.manifest is None:
        return StageResult("fetch", False, "newest fetched set has no manifest", _SET_FIX)
    repository_entries = newest.manifest.inventory.get("pgbackrest", ())
    failure = _validate_physical(
        io,
        stage="fetch",
        base=os.path.join(side, "pgbackrest"),
        entries=repository_entries,
    )
    if failure is not None:
        return failure
    paths_by_mode: dict[int, list[str]] = {}
    for entry in repository_entries:
        default = 0o750 if entry.kind == "d" else 0o640 if entry.kind == "f" else None
        if default is None or entry.mode == default:
            continue
        paths_by_mode.setdefault(entry.mode, []).append(
            os.path.join(side, "pgbackrest", entry.path)
        )
    return _run_grouped_chmod(io, paths_by_mode, stage="fetch")


def _fetch_stage(
    io: Host,
    config: SiteConfig,
    *,
    rendered_dir: PathLike,
    snapshot: backupset.RemoteSnapshot,
    at: datetime | None,
    operation_label: str,
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
        _STAGE_FIX,
    )
    if not rsync.ok:
        return rsync, None

    identity, identity_failure = pgbackrest.container_identity(io, rendered_dir)
    if identity_failure is not None or identity is None:
        if identity_failure is None:
            return StageResult(
                "fetch", False, "postgres identity lookup failed", _REOWN_FIX
            ), None
        return (
            StageResult(
                "fetch",
                False,
                identity_failure.problem or "postgres identity lookup failed",
                identity_failure.fix or _REOWN_FIX,
            ),
            None,
        )
    uid, gid = identity
    repository = os.path.join(side, "pgbackrest")
    for argv, detail in (
        (["chown", "-R", "-h", f"{uid}:{gid}", repository], f"re-owned {repository}"),
        (
            ["find", repository, "-type", "d", "-exec", "chmod", "0750", "{}", "+"],
            f"applied pgBackRest directory modes under {repository}",
        ),
        (
            ["find", repository, "-type", "f", "-exec", "chmod", "0640", "{}", "+"],
            f"applied pgBackRest file modes under {repository}",
        ),
    ):
        result = run_stage(io, "fetch", argv, detail, _REOWN_FIX)
        if not result.ok:
            return result, None

    try:
        sets = backupset.list_sets(io, staging=side)
    except OSError as exc:
        return StageResult("fetch", False, f"cannot list fetched sets: {exc}", _SET_FIX), None
    newest = next((ref for ref in sets if ref.complete and ref.manifest is not None), None)
    if newest is None:
        return StageResult("fetch", False, "fetched snapshot has no complete backup set", _SET_FIX), None
    repository_modes = _repository_mode_overrides(io, side, newest)
    if repository_modes is not None and not repository_modes.ok:
        return repository_modes, None

    reowned = sum(
        len(entry)
        for ref in sets
        if ref.complete and ref.manifest is not None
        for root_name, entry in ref.manifest.inventory.items()
        if root_name in _snapshotted_root_names(ref.manifest.checkout)
    )
    root_metadata = _root_owned_metadata(io, side, sets)
    if root_metadata is not None:
        return root_metadata, None

    selected = backupset.select_set(sets, at=at)
    if isinstance(selected, Problem):
        return StageResult("fetch", False, selected.problem, selected.fix), None
    replaced = f"{backupset.STAGING}.replaced-{operation_label}"
    return (
        StageResult(
            "fetch",
            True,
            f"fetched {snapshot.label}; selected set {selected.label}; "
            f"re-owned {reowned} path(s)",
            "",
        ),
        _Fetched(side, replaced, selected, reowned),
    )


def _verify_file_root(
    io: Host,
    root_name: str,
    entries: Sequence[backupset.Entry],
    base: str,
) -> StageResult | None:
    lines = "".join(
        f"{entry.sha256}  {entry.path}\n"
        for entry in sorted(entries, key=lambda item: item.path)
        if entry.kind == "f" and entry.sha256 is not None
    )
    if any(
        entry.kind == "f" and entry.sha256 is None for entry in entries
    ):
        return StageResult(
            "verify",
            False,
            f"{root_name} contains a file without an inventory hash",
            _VERIFY_FIX,
        )
    if not lines:
        # A root with no files has no hash to check — the exact walk above has
        # already refused any stray path; sha256sum -c refuses an empty list
        # ("no properly formatted checksum lines found"), which is what
        # /data/registry is on a no-GPU host (the acceptance VM's first
        # rollback found this).
        return None
    try:
        result = io.run(
            ["sha256sum", "-c", "-"],
            input=lines,
            cwd=base,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult("verify", False, f"checksum verification failed for {root_name}: {exc}", _VERIFY_FIX)
    okay, failed = sshtarget.parse_check_output(result.stdout)
    del okay
    if failed:
        return StageResult(
            "verify",
            False,
            f"checksum verification failed for {len(failed)} path(s) in {root_name}",
            _VERIFY_FIX,
        )
    if result.returncode != 0:
        return StageResult(
            "verify",
            False,
            f"checksum verification failed for {root_name}: {command_detail(result)}",
            _VERIFY_FIX,
        )
    return None


def _verify_stage(
    io: Host,
    rendered_dir: PathLike,
    *,
    selected: backupset.SetRef,
    source: str,
) -> StageResult:
    if selected.manifest is None:
        return StageResult("verify", False, "selected set has no manifest", _SET_FIX)
    manifest = selected.manifest
    roots = backupset.inventory_roots(manifest.checkout)
    repository = (
        REPOSITORY_PATH
        if source == "staging"
        else os.path.join(
            os.path.dirname(os.path.dirname(selected.path)),
            "pgbackrest",
        )
    )
    for root in roots:
        entries: Sequence[backupset.Entry] = manifest.inventory.get(root.name, ())
        if root.snapshotted:
            base = os.path.join(selected.path, backupset.FILES_DIR, root.name)
            walked = _validate_physical(io, stage="verify", base=base, entries=entries, exact=True)
            if walked is not None:
                return walked
        else:
            base = repository
            # A later backup has legitimately rewritten the info files since
            # this set was made; pgBackRest's verify below proves them.
            entries = tuple(
                entry
                for entry in entries
                if backupset.MUTABLE_REPOSITORY_FILE.fullmatch(entry.path.rsplit("/", 1)[-1]) is None
            )
        failure = _verify_file_root(
            io,
            root.name,
            entries,
            base,
        )
        if failure is not None:
            return failure

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

    repository_arg = None if source == "staging" else repository
    problem = pgbackrest.verify(
        io,
        pgbackrest.verify_argv(rendered_dir, repository=repository_arg),
    )
    if problem is not None:
        return StageResult("verify", False, problem, _VERIFY_FIX)
    return StageResult("verify", True, "backup set hashes and pgBackRest verification passed", "")


def _stop_stage(io: Host, rendered_dir: PathLike, *, build_box: bool) -> StageResult:
    stopped = run_stage(
        io,
        "stop",
        stack.compose_argv(rendered_dir, "down"),
        "stopped the GIDEON Compose project",
        _STAGE_FIX,
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
        return StageResult("swap", False, f"unsafe replacement path: {replaced}", _STAGE_FIX)
    first = run_stage(
        io,
        "swap",
        ["mv", backupset.STAGING, replaced],
        f"moved live staging to {replaced}",
        _STAGE_FIX,
    )
    if not first.ok:
        return first
    second = run_stage(
        io,
        "swap",
        ["mv", side, backupset.STAGING],
        "installed the fetched staging directory",
        _STAGE_FIX,
    )
    if second.ok:
        return second
    # Never leave the box without a staging directory: put the live one back.
    rollback = run_stage(
        io,
        "swap",
        ["mv", replaced, backupset.STAGING],
        "moved the live staging directory back",
        _STAGE_FIX,
    )
    if rollback.ok:
        return StageResult(
            "swap",
            False,
            f"{second.detail}; the live staging directory was moved back and the "
            f"fetched copy stays in {side}",
            _STAGE_FIX,
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
) -> tuple[StageResult, tuple[str, ...], int]:
    if selected.manifest is None:
        return StageResult("files", False, "selected set has no manifest", _SET_FIX), (), 0
    roots = backupset.inventory_roots(selected.manifest.checkout)
    restored: list[str] = []
    paths_by_owner: dict[OwnerKey, list[str]] = {}
    paths_by_mode: dict[int, list[str]] = {}
    for root in roots:
        if not root.restore_in_place:
            continue
        source = os.path.join(selected.path, backupset.FILES_DIR, root.name)
        root_entries = selected.manifest.inventory.get(root.name, ())
        # rsync applies the source directory's own attributes to the live
        # directory; a set that records no "." entry for the root (one made
        # before the entry existed) must not hand the copy's owner to it, so
        # the live directory's owner and mode are taken now and put back.
        keep: tuple[int, int, int] | None = None
        if not any(entry.path == backupset.ROOT_ENTRY for entry in root_entries):
            try:
                details = io.stat(root.source)
                keep = (details.st_uid, details.st_gid, stat.S_IMODE(details.st_mode))
            except OSError:
                keep = None
        argv = ["rsync", "-a", "--delete"]
        argv.extend(f"--exclude={pattern}" for pattern in root.exclusions)
        argv.extend(
            [source.rstrip("/") + "/", root.source.rstrip("/") + "/"]
        )
        result = run_stage(
            io,
            "files",
            argv,
            f"restored {root.name}",
            _STAGE_FIX,
        )
        if not result.ok:
            return result, tuple(restored), 0
        restored.append(root.name)
        if keep is not None:
            uid, gid, mode = keep
            paths_by_owner.setdefault((uid, gid, False), []).append(root.source)
            paths_by_mode.setdefault(mode, []).append(root.source)
        failure = _validate_physical(
            io, stage="files", base=root.source, entries=root_entries
        )
        if failure is not None:
            return failure, tuple(restored), 0
        for entry in root_entries:
            path = os.path.join(root.source, entry.path)
            is_link = entry.kind == "l"
            paths_by_owner.setdefault((entry.uid, entry.gid, is_link), []).append(path)
            if not is_link:
                paths_by_mode.setdefault(entry.mode, []).append(path)

    owner_result = _run_grouped_chown(io, paths_by_owner, stage="files")
    if owner_result is not None:
        return owner_result, tuple(restored), 0
    mode_result = _run_grouped_chmod(io, paths_by_mode, stage="files")
    if mode_result is not None:
        return mode_result, tuple(restored), 0
    checkout_copy = os.path.join(selected.path, backupset.FILES_DIR, "checkout")
    return (
        StageResult(
            "files",
            True,
            f"restored {', '.join(restored)}; the checkout copy stays in {checkout_copy} "
            "(clone the tag the manifest names)",
            "",
        ),
        tuple(restored),
        sum(len(paths) for paths in paths_by_owner.values()),
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
                    "postgres", False, f"backup {label} records no archive start", _SET_FIX
                )
            return record.timeline
    return StageResult(
        "postgres",
        False,
        f"the repository holds no backup {label} for the selected set",
        _SET_FIX,
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
        _STAGE_FIX,
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
        _STAGE_FIX,
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
            return StageResult("stores", False, f"unsafe replacement path: {replaced}", _STAGE_FIX)
        removed = run_stage(
            io,
            "stores",
            ["rm", "-rf", replaced],
            f"removed replaced staging directory {replaced}",
            _STAGE_FIX,
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
    io: Host, selected: backupset.SetRef, *, build_box: bool
) -> tuple[StageResult, tuple[str, ...]]:
    """State the terminal state and the two next steps.

    Whether the secrets on disk are the restored cluster's is decided by the
    set's keyed fingerprint against the live secret directory, never by a
    file's presence: apply regenerates the files on a rebuilt box.
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
    lines += (
        (
            "Next: sudo python3 -m gideon apply, then "
            "sudo python3 -m gideon backup run --full"
        ),
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
    host: Host | None = None,
    rendered_dir: PathLike = _RENDERED_DIR,
    site_path: PathLike = _SITE_PATH,
    root: PathLike | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Verify a selected backup source before replacing live state."""

    io = host or RealHost()
    if io.geteuid() != 0:
        return _refuse("root is required.", _ROOT_FIX)
    effective_now = aware_now(now)
    if effective_now is None:
        return _refuse(
            "restore's clock value must be timezone-aware.",
            "Use an aware UTC time, then retry.",
        )
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
    config, source, at = prerequisites

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
        fetch_result, fetched = _fetch_stage(
            io,
            config,
            rendered_dir=rendered_dir,
            snapshot=selection.snapshot,
            at=at,
            operation_label=operation_label,
        )
        print_stage(fetch_result)
        if not fetch_result.ok or fetched is None:
            return 1
        selected = fetched.selected
    else:
        print_stage(StageResult("fetch", True, "skipped (staging source)", ""))
    assert selected is not None

    verify_result = _verify_stage(
        io,
        rendered_dir,
        selected=selected,
        source=source,
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
    else:
        print_stage(StageResult("swap", True, "skipped (staging source)", ""))

    files_result, roots_restored, file_reowned = _restore_files_stage(
        io,
        selected=selected,
    )
    print_stage(files_result)
    if not files_result.ok:
        return 1

    reowned = file_reowned + (fetched.reowned if fetched is not None else 0)
    if selected.manifest is None:
        print_stage(StageResult("postgres", False, "selected set has no manifest", _SET_FIX))
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
        reowned=reowned,
        replaced=fetched.replaced if fetched is not None else None,
        run_id=str(uuid.uuid4()),
        build_box=build_box,
    )
    print_stage(stores_result)
    if not stores_result.ok:
        return 1

    next_result, next_lines = _next_stage(io, selected, build_box=build_box)
    print_stage(next_result)
    if next_result.ok:
        for line in next_lines:
            print(line)
    return int(not next_result.ok)
