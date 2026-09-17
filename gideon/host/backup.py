"""The local backup-run command and its ordered host-side stages (§19.1)."""

import hashlib
import os
import re
import shlex
import stat
import subprocess
import sys
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import gideon
from gideon.host import audit, backupset, pgbackrest, secrets, site, sshtarget, stack
from gideon.host.report import (
    Problem,
    StageResult,
    command_detail,
    print_stage,
    refusal,
)
from gideon.host.site import SiteConfig
from gideon.host.stages import aware_now, psql_argv, site_problem, table_parts
from gideon.host.steps.site_dirs import (
    AGE_IDENTITY_FIX,
    AGE_IDENTITY_MODE,
    AGE_IDENTITY_PATH,
    AGE_RECIPIENT,
    AGE_RECIPIENT_PATH,
)
from gideon.host.sysio import Host, PathLike, RealHost

_SITE_PATH: Final = "/etc/gideon/site.yaml"
_RENDERED_DIR: Final = "/etc/gideon/rendered"
_ROOT_FIX: Final = "Run sudo python3 -m gideon backup run as root, then retry."
_APPLY_FIX: Final = "Run sudo python3 -m gideon apply, then retry."
_TOOLS_FIX: Final = (
    "Run sudo python3 -m gideon host provision --only host-tools, then retry."
)
_RECIPIENT_FIX: Final = (
    "Run sudo python3 -m gideon host provision --only age-recipient, then retry."
)
_IDENTITY_FIX: Final = (
    "Run sudo python3 -m gideon host provision --only age-identity, then retry."
)
_STAGE_FIX: Final = "Run sudo python3 -m gideon apply, then retry."
_LINK_FIX: Final = (
    "Confirm /data/backup-staging is one filesystem (§3.7), then re-run backup run"
)
_PRUNE_FIX: Final = "Repair the staging directory, then re-run backup run."
_SECRET_STAGE_FIX: Final = (
    "Correct /etc/gideon/secrets, /etc/gideon/backup_age_recipient, and "
    "/etc/gideon/backup_age_identity, then re-run backup run."
)
# Exempt operational constant: the bound on one pgBackRest backup, within the
# timer unit's six-hour window for the run and the push together.
_COMMAND_TIMEOUT: Final = 18000.0
_PUSH_ROOT_FIX: Final = "Run sudo python3 -m gideon backup push as root, then retry."
_PUSH_SET_FIX: Final = "Run sudo python3 -m gideon backup run, then retry."
_PUSH_TOOLS_FIX: Final = (
    "Run sudo python3 -m gideon host provision --only host-tools, then retry."
)
_PUSH_TARGET_FIX: Final = (
    "Authorize the backup key and check the target per the office-services "
    "runbook §3, then re-run backup push."
)
_PUSH_STAGE_FIX: Final = "Run sudo python3 -m gideon backup push, then retry."
_PUSH_FULL_COPY_FIX: Final = (
    "Keep every snapshot under one path on one filesystem on the target (§3.7), "
    "then re-run backup push."
)
_PUSH_CHECK_FIX: Final = (
    "Re-run sudo python3 -m gideon backup push --verify-all; if it fails again, "
    "the target's copy is corrupt — check the target's disk per §3.7."
)
_RSYNC_STAT_PATTERN = re.compile(
    r"^\s*Total (file size|transferred file size):\s*([0-9][0-9,]*) bytes\s*$"
)


@dataclass(frozen=True, slots=True)
class _SecretsStage:
    tarball_sha256: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _PostgresStage:
    label: str
    backup_type: str
    archive_through: datetime


def _refuse(problem: str, fix: str, *, command: str = "backup run") -> int:
    print(refusal(command, problem, fix), file=sys.stderr)
    return 1


def _preconditions(
    io: Host,
    *,
    rendered_dir: PathLike,
    site_path: PathLike,
) -> tuple[SiteConfig, tuple[str, ...], pgbackrest.InfoResult] | None:
    loaded = site.load_site(Path(site_path), host=io)
    if loaded.errors or loaded.config is None:
        _refuse(
            site_problem(loaded) or "the site file is invalid.",
            "Correct the site file, then retry.",
        )
        return None
    config = loaded.config

    try:
        compose_path = Path(rendered_dir) / "compose.yaml"
        if not io.exists(compose_path):
            _refuse(
                f"rendered Compose file is missing: {compose_path}.", _APPLY_FIX
            )
            return None
        missing_tools = tuple(
            path
            for path in (
                "/usr/bin/bash",
                "/usr/bin/age",
                "/usr/bin/age-keygen",
                "/usr/bin/rsync",
            )
            if not io.exists(path)
        )
    except OSError as exc:
        _refuse(f"cannot inspect backup prerequisites: {exc}.", _APPLY_FIX)
        return None
    if missing_tools:
        _refuse(
            "backup tool(s) are missing: " + ", ".join(missing_tools) + ".",
            _TOOLS_FIX,
        )
        return None

    try:
        recipient = io.read_text(AGE_RECIPIENT_PATH).strip()
    except FileNotFoundError:
        _refuse(
            f"age recipient is missing: {AGE_RECIPIENT_PATH}.", _RECIPIENT_FIX
        )
        return None
    except (OSError, UnicodeError) as exc:
        _refuse(
            f"age recipient is unreadable: {AGE_RECIPIENT_PATH} ({exc}).",
            _RECIPIENT_FIX,
        )
        return None
    if AGE_RECIPIENT.fullmatch(recipient) is None:
        _refuse(
            f"age recipient is malformed: {AGE_RECIPIENT_PATH}.", _RECIPIENT_FIX
        )
        return None

    try:
        if not io.exists(AGE_IDENTITY_PATH):
            _refuse(
                f"box identity is missing: {AGE_IDENTITY_PATH}.", _IDENTITY_FIX
            )
            return None
        identity_stat = io.stat(AGE_IDENTITY_PATH)
    except OSError as exc:
        _refuse(
            f"box identity cannot be inspected: {AGE_IDENTITY_PATH} ({exc}).",
            _IDENTITY_FIX,
        )
        return None
    identity_mode = stat.S_IMODE(identity_stat.st_mode)
    if identity_mode != AGE_IDENTITY_MODE or identity_stat.st_uid != 0 or identity_stat.st_gid != 0:
        _refuse(
            f"box identity is not root-only: {AGE_IDENTITY_PATH} "
            f"(mode {identity_mode:04o}, owner {identity_stat.st_uid}:{identity_stat.st_gid}).",
            _IDENTITY_FIX,
        )
        return None

    # The identity reaches the child by path; its streams are never echoed,
    # since a decoder's diagnostic may quote its input.
    derive_argv = ["age-keygen", "-y", os.fspath(AGE_IDENTITY_PATH)]
    try:
        derived = io.run(derive_argv)
    except (OSError, subprocess.SubprocessError) as exc:
        exit_code = getattr(exc, "returncode", "unknown")
        _refuse(
            f"box identity cannot be read as an age identity: {AGE_IDENTITY_PATH} "
            f"(exit {exit_code}).",
            AGE_IDENTITY_FIX,
        )
        return None
    if (
        derived.returncode != 0
        or AGE_RECIPIENT.fullmatch(derived.stdout.strip()) is None
    ):
        _refuse(
            f"box identity cannot be read as an age identity: {AGE_IDENTITY_PATH} "
            f"(exit {derived.returncode}).",
            AGE_IDENTITY_FIX,
        )
        return None
    box_recipient = derived.stdout.strip()

    ready_argv = stack.exec_argv(rendered_dir, "postgres", "pg_isready")
    try:
        ready = io.run(ready_argv)
    except (OSError, subprocess.SubprocessError) as exc:
        _refuse(f"Postgres readiness probe failed: {exc}.", _APPLY_FIX)
        return None
    if ready.returncode != 0:
        _refuse(
            f"Postgres is not ready: {command_detail(ready)}.", _APPLY_FIX
        )
        return None

    info = pgbackrest.info(io, rendered_dir)
    if not info.ok:
        _refuse(info.problem or "pgBackRest info failed.", _APPLY_FIX)
        return None

    audit_problem = audit.probe(io, rendered_dir)
    if audit_problem is not None:
        _refuse(
            f"audit writer is unavailable: {audit_problem}",
            stack.logs_fix(rendered_dir, "postgres"),
        )
        return None
    return config, (recipient, box_recipient), info


def _existing_labels(sets: Sequence[backupset.SetRef]) -> frozenset[str]:
    return frozenset(
        ref.label.removesuffix(backupset.PARTIAL_SUFFIX)
        for ref in sets
    )


def _choose_label(
    *,
    args: object,
    now: datetime,
    sets: tuple[backupset.SetRef, ...],
    pre_restore: bool = False,
) -> tuple[str, backupset.Kind] | None:
    if pre_restore:
        return backupset.pre_restore_label(now), backupset.Kind.PRE_RESTORE
    supplied = getattr(args, "label", None)
    if supplied is not None:
        problem = backupset.validate_label(supplied)
        if problem is not None:
            _refuse(problem.problem, problem.fix)
            return None
        assert isinstance(supplied, str)
        kind = backupset.kind_of(supplied)
        if kind is not backupset.Kind.LABELLED:
            _refuse(
                "--label must be an operator label, not a nightly or pre-restore label.",
                "Use an operator label matching pre-[A-Za-z0-9._-]+, then retry.",
            )
            return None
        if supplied in _existing_labels(sets):
            _refuse(
                f"backup label already exists: {supplied}.",
                "Choose a new --label, then retry.",
            )
            return None
        return supplied, kind

    candidate_time = now
    labels = _existing_labels(sets)
    while True:
        label = backupset.nightly_label(candidate_time)
        if label not in labels:
            return label, backupset.Kind.NIGHTLY
        candidate_time += timedelta(seconds=1)


def _run_failure(
    name: str,
    action: str,
    result: subprocess.CompletedProcess[str],
    fix: str,
) -> StageResult:
    return StageResult(name, False, f"{action}: {command_detail(result)}", fix)


def _intent_stage(
    io: Host,
    rendered_dir: PathLike,
    *,
    run_id: str,
    label: str,
    kind: backupset.Kind,
    previous_label: str | None,
) -> StageResult:
    row = audit.AuditRow(
        run_id,
        "backup_run",
        None,
        None,
        None,
        (),
        {
            "phase": "intent",
            "label": label,
            "kind": kind.value,
            "previous_label": previous_label,
        },
    )
    problem = audit.write_rows(io, rendered_dir, (row,))
    if problem is not None:
        return StageResult(
            "intent",
            False,
            f"audit intent write failed: {problem}",
            stack.logs_fix(rendered_dir, "postgres"),
        )
    return StageResult(
        "intent",
        True,
        f"backup_run intent recorded for {label}",
        "",
    )


def _sample_at_most(paths: Sequence[str], limit: int) -> tuple[str, ...]:
    ordered = tuple(sorted(paths))
    if len(ordered) <= limit:
        return backupset.sample_paths(ordered, percent=100, floor=1)
    step = (len(ordered) + limit - 1) // limit
    return ordered[::step]


def _link_verdict(
    io: Host,
    previous: backupset.SetRef,
    roots: Sequence[backupset.InventoryRoot],
    partial: str,
) -> backupset.LinkVerdict:
    """Compare inodes for a sample of files the previous set also holds.

    The sample is drawn before anything is stat-ed, so a registry of many
    thousand blobs costs at most a hundred pairs of stat calls; a file gone
    from either tree is skipped rather than sampled.
    """

    if previous.manifest is None:
        return backupset.LinkVerdict(0, 0)
    candidates = [
        (root.name, entry.path)
        for root in roots
        if root.snapshotted
        for entry in previous.manifest.inventory.get(root.name, ())
        if entry.kind == "f"
    ]
    keyed = {f"{root_name}/{path}": (root_name, path) for root_name, path in candidates}
    sampled = 0
    linked = 0
    for key in _sample_at_most(tuple(keyed), 100):
        root_name, path = keyed[key]
        current_path = os.path.join(partial, backupset.FILES_DIR, root_name, path)
        previous_path = os.path.join(previous.path, backupset.FILES_DIR, root_name, path)
        try:
            current_stat = io.stat(current_path)
            previous_stat = io.stat(previous_path)
        except OSError:
            continue
        sampled += 1
        if current_stat.st_ino == previous_stat.st_ino:
            linked += 1
    return backupset.LinkVerdict(sampled, linked)


def _files_stage(
    io: Host,
    *,
    previous: backupset.SetRef | None,
    roots: tuple[backupset.InventoryRoot, ...],
    partial: str,
) -> tuple[StageResult, backupset.LinkVerdict]:
    try:
        for root in roots:
            if not root.snapshotted:
                continue
            destination = os.path.join(partial, backupset.FILES_DIR, root.name)
            io.mkdir(destination, mode=0o750, parents=True, exist_ok=True)
            argv = ["rsync", "-a"]
            argv.extend(f"--exclude={pattern}" for pattern in root.exclusions)
            if previous is not None:
                link_dest = os.path.join(
                    previous.path, backupset.FILES_DIR, root.name
                )
                argv.append(f"--link-dest={link_dest}/")
            argv.extend(
                [
                    root.source.rstrip("/") + "/",
                    destination.rstrip("/") + "/",
                ]
            )
            result = io.run(argv)
            if result.returncode != 0:
                return (
                    _run_failure(
                        "files",
                        f"rsync failed for {root.name}",
                        result,
                        _STAGE_FIX,
                    ),
                    backupset.LinkVerdict(0, 0),
                )
    except (OSError, subprocess.SubprocessError) as exc:
        return (
            StageResult("files", False, f"file snapshot failed: {exc}", _STAGE_FIX),
            backupset.LinkVerdict(0, 0),
        )

    verdict = (
        _link_verdict(io, previous, roots, partial)
        if previous is not None
        else backupset.LinkVerdict(0, 0)
    )
    if verdict.sampled > 0 and verdict.linked == 0:
        return (
            StageResult(
                "files",
                False,
                "no hard links to the previous set; a full copy would hide a retention overrun",
                _LINK_FIX,
            ),
            verdict,
        )
    return (
        StageResult(
            "files",
            True,
            f"snapshotted {sum(root.snapshotted for root in roots)} root(s); "
            f"linked {verdict.linked} of {verdict.sampled} sampled",
            "",
        ),
        verdict,
    )


def _secrets_stage(
    io: Host,
    *,
    recipients: Sequence[str],
    partial: str,
) -> tuple[StageResult, _SecretsStage | None]:
    tarball = os.path.join(partial, backupset.TARBALL_NAME)
    recipient_args = " ".join(
        f"-r {shlex.quote(recipient)}" for recipient in recipients
    )
    script = (
        "set -o pipefail; tar -C /etc/gideon -cf - secrets | age "
        + recipient_args
        + " -o "
        + shlex.quote(tarball)
    )
    try:
        encrypted = io.run(["bash", "-c", script])
    except (OSError, subprocess.SubprocessError) as exc:
        return (
            StageResult("secrets", False, f"secret archive failed: {exc}", _SECRET_STAGE_FIX),
            None,
        )
    if encrypted.returncode != 0:
        return (
            _run_failure("secrets", "secret archive failed", encrypted, _SECRET_STAGE_FIX),
            None,
        )

    try:
        hashed = io.run(["sha256sum", tarball])
    except (OSError, subprocess.SubprocessError) as exc:
        return (
            StageResult("secrets", False, f"tarball hash failed: {exc}", _SECRET_STAGE_FIX),
            None,
        )
    if hashed.returncode != 0:
        return (
            _run_failure("secrets", "tarball hash failed", hashed, _SECRET_STAGE_FIX),
            None,
        )
    try:
        hashes = backupset.parse_sha256sum(hashed.stdout)
    except ValueError as exc:
        return (
            StageResult("secrets", False, f"tarball hash was malformed: {exc}", _SECRET_STAGE_FIX),
            None,
        )
    digest = hashes.get(tarball)
    if digest is None:
        return (
            StageResult(
                "secrets",
                False,
                f"tarball hash did not name {tarball}",
                _SECRET_STAGE_FIX,
            ),
            None,
        )
    fingerprint = secrets.fingerprint(io)
    if fingerprint is None:
        return (
            StageResult(
                "secrets",
                False,
                "the secrets directory has no webui_secret_key to fingerprint",
                _APPLY_FIX,
            ),
            None,
        )
    return (
        StageResult(
            "secrets",
            True,
            f"secrets encrypted into the backup set for {len(recipients)} recipients",
            "",
        ),
        _SecretsStage(digest, fingerprint),
    )


def _backup_type(
    infos: Sequence[pgbackrest.BackupInfo], *, full_requested: bool, now: datetime
) -> str:
    if full_requested:
        return "full"
    newest = pgbackrest.newest_full(infos)
    if newest is None:
        return "full"
    try:
        finished = datetime.fromtimestamp(newest.timestamp_stop, UTC)
    except (OverflowError, OSError, ValueError):
        return "full"
    return "full" if now - finished > timedelta(days=7) else "incr"


def _postgres_stage(
    io: Host,
    rendered_dir: PathLike,
    *,
    info: pgbackrest.InfoResult,
    full_requested: bool,
    kind: backupset.Kind,
    now: datetime,
    now_was_supplied: bool,
) -> tuple[StageResult, _PostgresStage | None]:
    backup_type = _backup_type(info.infos, full_requested=full_requested, now=now)
    try:
        backup_result = io.run(
            pgbackrest.backup_argv(
                rendered_dir,
                backup_type,
                expire_auto=kind is backupset.Kind.NIGHTLY,
            ),
            timeout=_COMMAND_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return (
            StageResult("postgres", False, f"pgBackRest backup failed: {exc}", _STAGE_FIX),
            None,
        )
    if backup_result.returncode != 0:
        return (
            _run_failure("postgres", "pgBackRest backup failed", backup_result, _STAGE_FIX),
            None,
        )

    archive_start = now if now_was_supplied else datetime.now(UTC)
    checked = pgbackrest.check(io, rendered_dir)
    if not checked.ok:
        return (
            StageResult(
                "postgres",
                False,
                checked.problem or "pgBackRest archive check failed.",
                checked.fix or _STAGE_FIX,
            ),
            None,
        )
    fresh = pgbackrest.info(io, rendered_dir)
    if not fresh.ok or not fresh.infos:
        return (
            StageResult(
                "postgres",
                False,
                fresh.problem or "pgBackRest info returned no backup after backup.",
                fresh.fix or _STAGE_FIX,
            ),
            None,
        )
    newest = max(fresh.infos, key=lambda entry: entry.timestamp_stop)
    return (
        StageResult(
            "postgres",
            True,
            f"pgBackRest {newest.type} backup {newest.label}; archive through {archive_start.isoformat()}",
            "",
        ),
        _PostgresStage(newest.label, newest.type, archive_start),
    )


def _counts_stage(
    io: Host,
    rendered_dir: PathLike,
) -> tuple[StageResult, Mapping[str, Mapping[str, int]] | None]:
    counts: dict[str, Mapping[str, int]] = {}
    listing_sql = (
        "SELECT schemaname||'.'||relname FROM pg_stat_user_tables "
        "ORDER BY schemaname, relname;\n"
    )
    for database in ("gideon", "openwebui"):
        argv = psql_argv(rendered_dir, database)
        try:
            listing = io.run(argv, input=listing_sql)
        except (OSError, subprocess.SubprocessError) as exc:
            return (
                StageResult("counts", False, f"table listing failed for {database}: {exc}", _STAGE_FIX),
                None,
            )
        if listing.returncode != 0:
            return (
                _run_failure("counts", f"table listing failed for {database}", listing, _STAGE_FIX),
                None,
            )
        tables = tuple(
            line.strip()
            for line in listing.stdout.splitlines()
            if line.strip() and table_parts(line.strip()) is not None
        )
        count_sql = "".join(
            f"SELECT '{table}', count(*) FROM {table};\n" for table in tables
        )
        try:
            counted = io.run(argv, input=count_sql)
        except (OSError, subprocess.SubprocessError) as exc:
            return (
                StageResult("counts", False, f"table counts failed for {database}: {exc}", _STAGE_FIX),
                None,
            )
        if counted.returncode != 0:
            return (
                _run_failure("counts", f"table counts failed for {database}", counted, _STAGE_FIX),
                None,
            )
        wanted = set(tables)
        parsed: dict[str, int] = {}
        for line in counted.stdout.splitlines():
            if not line.strip():
                continue
            name, separator, value = line.partition("|")
            name = name.strip()
            value = value.strip()
            if not separator or name not in wanted:
                continue
            try:
                number = int(value, 10)
            except ValueError:
                return (
                    StageResult("counts", False, f"invalid row count for {database}.{name}", _STAGE_FIX),
                    None,
                )
            if number < 0:
                return (
                    StageResult("counts", False, f"invalid row count for {database}.{name}", _STAGE_FIX),
                    None,
                )
            parsed[name] = number
        if set(parsed) != wanted:
            missing = sorted(wanted - set(parsed))
            return (
                StageResult(
                    "counts",
                    False,
                    f"row counts were missing for {database}: {', '.join(missing)}",
                    _STAGE_FIX,
                ),
                None,
            )
        counts[database] = {name: parsed[name] for name in tables}
    return StageResult("counts", True, "row counts recorded for gideon and openwebui", ""), counts


def _relative_hashes(directory: str, hashes: Mapping[str, str]) -> Mapping[str, str]:
    prefix = directory.rstrip("/") + "/"
    return {
        path.removeprefix(prefix) if path.startswith(prefix) else path: digest
        for path, digest in hashes.items()
    }


def _find_entries(io: Host, directory: str) -> tuple[backupset.Entry, ...] | StageResult:
    try:
        found = io.run(["find", directory, "-printf", backupset.FIND_FORMAT])
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult("manifest", False, f"find failed for {directory}: {exc}", _STAGE_FIX)
    if found.returncode != 0:
        return _run_failure("manifest", f"find failed for {directory}", found, _STAGE_FIX)
    try:
        return tuple(sorted(backupset.parse_find_listing(found.stdout), key=lambda entry: entry.path))
    except ValueError as exc:
        return StageResult("manifest", False, f"find listing was malformed for {directory}: {exc}", _STAGE_FIX)


def _hash_entries(
    io: Host,
    directory: str,
    entries: tuple[backupset.Entry, ...],
    *,
    repository: bool,
    previous_entries: Mapping[str, backupset.Entry],
) -> tuple[backupset.Entry, ...] | StageResult:
    carried = tuple(
        backupset.carry_forward(previous_entries, entry) if repository else entry
        for entry in entries
    )
    paths = tuple(
        os.path.join(directory, entry.path)
        for entry in carried
        if entry.kind == "f" and entry.sha256 is None
    )
    hashes: dict[str, str] = {
        entry.path: entry.sha256
        for entry in carried
        if entry.kind == "f" and entry.sha256 is not None
    }
    commands: Iterable[list[str]]
    if repository:
        all_paths = paths
        commands = (
            ["sha256sum", "--", *all_paths[start : start + 200]]
            for start in range(0, len(all_paths), 200)
        )
    else:
        commands = iter(
            [["find", directory, "-type", "f", "-exec", "sha256sum", "{}", "+"]]
        )
    for argv in commands:
        try:
            result = io.run(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            return StageResult("manifest", False, f"hashing failed for {directory}: {exc}", _STAGE_FIX)
        if result.returncode != 0:
            return _run_failure("manifest", f"hashing failed for {directory}", result, _STAGE_FIX)
        try:
            parsed = backupset.parse_sha256sum(result.stdout)
        except ValueError as exc:
            return StageResult("manifest", False, f"hash listing was malformed for {directory}: {exc}", _STAGE_FIX)
        hashes.update(_relative_hashes(directory, parsed))
    merged = backupset.merge_hashes(carried, hashes)
    if isinstance(merged, backupset.Problem):
        return StageResult("manifest", False, merged.problem, merged.fix)
    return tuple(sorted(merged, key=lambda entry: entry.path))


def _commit_for(io: Host, checkout: str) -> str:
    # Root reads a checkout a CSA account owns; without the safe.directory grant
    # git refuses it as dubious ownership and the record would say "unknown".
    try:
        result = io.run(
            ["git", "-c", f"safe.directory={checkout}", "-C", checkout, "rev-parse", "HEAD"]
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0 or not result.stdout.strip():
        return "unknown"
    return result.stdout.strip()


def _manifest_stage(
    io: Host,
    *,
    partial: str,
    final: str,
    roots: tuple[backupset.InventoryRoot, ...],
    previous: backupset.SetRef | None,
    started: datetime,
    finished: datetime,
    label: str,
    kind: backupset.Kind,
    config: SiteConfig,
    pg: _PostgresStage,
    row_counts: Mapping[str, Mapping[str, int]],
    tarball_sha256: str,
    recipients: Sequence[str],
    links: backupset.LinkVerdict,
    checkout: str,
    secrets_fingerprint: str,
) -> tuple[StageResult, backupset.Manifest | None]:
    inventory: dict[str, tuple[backupset.Entry, ...]] = {}
    for root in roots:
        directory = (
            os.path.join(partial, backupset.FILES_DIR, root.name)
            if root.snapshotted
            else root.source
        )
        entries = _find_entries(io, directory)
        if isinstance(entries, StageResult):
            return entries, None
        previous_entries_value: Sequence[backupset.Entry] = ()
        if previous is not None and previous.manifest is not None:
            previous_entries_value = previous.manifest.inventory.get(root.name, ())
        previous_entries: Mapping[str, backupset.Entry] = {
            entry.path: entry for entry in previous_entries_value
        }
        hashed = _hash_entries(
            io,
            directory,
            entries,
            repository=not root.snapshotted,
            previous_entries=previous_entries,
        )
        if isinstance(hashed, StageResult):
            return hashed, None
        inventory[root.name] = hashed

    commit = _commit_for(io, checkout)
    manifest = backupset.Manifest(
        1,
        label,
        kind,
        started,
        finished,
        gideon.__version__,
        checkout,
        commit,
        config.hostname,
        previous.label if previous is not None else None,
        pg.label,
        pg.backup_type,
        pg.archive_through,
        row_counts,
        inventory,
        tarball_sha256,
        tuple(recipients),
        links,
        secrets_fingerprint,
    )
    manifest_path = os.path.join(partial, backupset.MANIFEST_NAME)
    try:
        io.write_text(manifest_path, manifest.to_json())
        moved = io.run(["mv", partial, final])
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult("manifest", False, f"manifest finalization failed: {exc}", _STAGE_FIX), None
    if moved.returncode != 0:
        return _run_failure("manifest", "manifest finalization failed", moved, _STAGE_FIX), None
    return StageResult("manifest", True, f"manifest written and set renamed to {label}", ""), manifest


def _safe_set_path(path: str, new_set: str) -> bool:
    root = os.path.normpath(backupset.SETS_DIR)
    normalized = os.path.normpath(path)
    return normalized != os.path.normpath(new_set) and normalized.startswith(root + os.sep)


def _prune_stage(
    io: Host,
    *,
    now: datetime,
    local_days: int,
    final: str,
) -> tuple[StageResult, int]:
    try:
        sets = backupset.list_sets(io)
    except OSError as exc:
        return StageResult("prune", False, f"cannot list backup sets: {exc}", _PRUNE_FIX), 0
    partial_mtimes: dict[str, float] = {}
    for ref in sets:
        if not ref.label.endswith(backupset.PARTIAL_SUFFIX):
            continue
        try:
            partial_mtimes[ref.label] = float(io.stat(ref.path).st_mtime)
        except OSError:
            continue
    candidates = backupset.prune_candidates(
        sets,
        partial_mtimes,
        now,
        local_days,
        staging=backupset.STAGING,
    )
    for candidate in candidates:
        if not _safe_set_path(candidate, final):
            return (
                StageResult(
                    "prune",
                    False,
                    f"refusing to remove a path outside {backupset.SETS_DIR}: {candidate}",
                    _PRUNE_FIX,
                ),
                0,
            )
        try:
            removed = io.run(["rm", "-rf", candidate])
        except (OSError, subprocess.SubprocessError) as exc:
            return StageResult("prune", False, f"prune failed for {candidate}: {exc}", _PRUNE_FIX), 0
        if removed.returncode != 0:
            return _run_failure("prune", f"prune failed for {candidate}", removed, _PRUNE_FIX), 0
    return StageResult("prune", True, f"pruned {len(candidates)} old set(s)", ""), len(candidates)


def _applied_stage(
    io: Host,
    rendered_dir: PathLike,
    *,
    run_id: str,
    label: str,
    kind: backupset.Kind,
    pg: _PostgresStage,
    started: datetime,
    finished: datetime,
    inventory: Mapping[str, Sequence[backupset.Entry]],
    links: backupset.LinkVerdict,
    pruned: int,
) -> StageResult:
    files = {
        name: sum(entry.kind == "f" for entry in entries)
        for name, entries in inventory.items()
    }
    set_bytes = sum(
        entry.size
        for name, entries in inventory.items()
        if name != "pgbackrest"
        for entry in entries
        if entry.kind == "f"
    )
    duration = max(0.0, (finished - started).total_seconds())
    row = audit.AuditRow(
        run_id,
        "backup_run",
        None,
        None,
        None,
        (),
        {
            "phase": "applied",
            "label": label,
            "kind": kind.value,
            "pgbackrest_label": pg.label,
            "pgbackrest_type": pg.backup_type,
            "archive_through": pg.archive_through.isoformat(),
            "duration_s": duration,
            "set_bytes": set_bytes,
            "files": files,
            "hard_links": {"sampled": links.sampled, "linked": links.linked},
            "pruned": pruned,
        },
    )
    problem = audit.write_rows(io, rendered_dir, (row,))
    if problem is not None:
        return StageResult(
            "applied",
            False,
            f"audit applied write failed: {problem}",
            stack.logs_fix(rendered_dir, "postgres"),
        )
    return StageResult(
        "applied",
        True,
        f"backup run {label} applied ({set_bytes} bytes, {links.linked} of {links.sampled} links)",
        "",
    )


def run_backup_run(
    args: object,
    *,
    host: Host | None = None,
    rendered_dir: PathLike = _RENDERED_DIR,
    site_path: PathLike = _SITE_PATH,
    root: PathLike | None = None,
    now: datetime | None = None,
    pre_restore: bool = False,
) -> int:
    """Run the eight ordered stages that create one local backup set."""

    io = host or RealHost()
    if io.geteuid() != 0:
        return _refuse("root is required.", _ROOT_FIX)
    started = aware_now(now)
    if started is None:
        return _refuse(
            "backup run's clock value must be timezone-aware.",
            "Use an aware UTC time, then retry.",
        )
    now_was_supplied = now is not None
    prerequisites = _preconditions(io, rendered_dir=rendered_dir, site_path=site_path)
    if prerequisites is None:
        return 1
    config, recipients, info = prerequisites

    try:
        sets = backupset.list_sets(io)
    except OSError as exc:
        return _refuse(f"cannot list backup sets: {exc}.", _STAGE_FIX)
    chosen = _choose_label(
        args=args, now=started, sets=sets, pre_restore=pre_restore
    )
    if chosen is None:
        return 1
    label, kind = chosen
    previous = next((ref for ref in sets if ref.complete), None)
    checkout = Path(__file__).parents[2] if root is None else Path(root)
    checkout_text = os.fspath(checkout)
    partial = backupset.partial_dir(label)
    final = backupset.set_dir(label)
    run_id = str(uuid.uuid4())

    intent = _intent_stage(
        io,
        rendered_dir,
        run_id=run_id,
        label=label,
        kind=kind,
        previous_label=previous.label if previous is not None else None,
    )
    print_stage(intent)
    if not intent.ok:
        return 1

    roots = backupset.inventory_roots(checkout_text)
    files, links = _files_stage(
        io, previous=previous, roots=roots, partial=partial
    )
    print_stage(files)
    if not files.ok:
        return 1

    secrets_result, secrets_stage = _secrets_stage(
        io, recipients=recipients, partial=partial
    )
    print_stage(secrets_result)
    if not secrets_result.ok or secrets_stage is None:
        return 1

    full_requested = (
        pre_restore
        or bool(getattr(args, "full", False))
        or getattr(args, "label", None) is not None
    )
    postgres_result, pg = _postgres_stage(
        io,
        rendered_dir,
        info=info,
        full_requested=full_requested,
        kind=kind,
        now=started,
        now_was_supplied=now_was_supplied,
    )
    print_stage(postgres_result)
    if not postgres_result.ok or pg is None:
        return 1

    counts_result, row_counts = _counts_stage(io, rendered_dir)
    print_stage(counts_result)
    if not counts_result.ok or row_counts is None:
        return 1

    finished = started if now_was_supplied else datetime.now(UTC)
    manifest_result, manifest = _manifest_stage(
        io,
        partial=partial,
        final=final,
        roots=roots,
        previous=previous,
        started=started,
        finished=finished,
        label=label,
        kind=kind,
        config=config,
        pg=pg,
        row_counts=row_counts,
        tarball_sha256=secrets_stage.tarball_sha256,
        recipients=recipients,
        links=links,
        checkout=checkout_text,
        secrets_fingerprint=secrets_stage.fingerprint,
    )
    print_stage(manifest_result)
    if not manifest_result.ok or manifest is None:
        return 1

    prune_result, pruned = _prune_stage(
        io,
        now=finished,
        local_days=config.backup.local_days,
        final=final,
    )
    print_stage(prune_result)
    if not prune_result.ok:
        return 1

    applied = _applied_stage(
        io,
        rendered_dir,
        run_id=run_id,
        label=label,
        kind=kind,
        pg=pg,
        started=started,
        finished=finished,
        inventory=manifest.inventory,
        links=links,
        pruned=pruned,
    )
    print_stage(applied)
    return int(not applied.ok)


@dataclass(frozen=True, slots=True)
class PushStats:
    total_bytes: int
    transferred_bytes: int


@dataclass(frozen=True, slots=True)
class _PushCheck:
    verified: int
    failed: int


def _push_remote_root(config: SiteConfig) -> str:
    path = config.backup.target.path.rstrip("/")
    return path or "/"


def _push_remote_path(config: SiteConfig, name: str) -> str:
    return os.path.join(_push_remote_root(config), name)


def _push_preconditions(
    io: Host,
    *,
    site_path: PathLike,
    rendered_dir: PathLike,
) -> tuple[SiteConfig, backupset.SetRef] | None:
    loaded = site.load_site(Path(site_path), host=io)
    if loaded.errors or loaded.config is None:
        _refuse(
            site_problem(loaded) or "the site file is invalid.",
            "Correct the site file, then retry.",
            command="backup push",
        )
        return None
    config = loaded.config

    try:
        has_rsync = io.exists("/usr/bin/rsync")
    except OSError as exc:
        _refuse(
            f"cannot inspect /usr/bin/rsync: {exc}.",
            _PUSH_TOOLS_FIX,
            command="backup push",
        )
        return None
    if not has_rsync:
        _refuse(
            "backup tool is missing: /usr/bin/rsync.",
            _PUSH_TOOLS_FIX,
            command="backup push",
        )
        return None

    try:
        sets = backupset.list_sets(io)
    except OSError as exc:
        _refuse(
            f"cannot list local backup sets: {exc}.",
            _PUSH_SET_FIX,
            command="backup push",
        )
        return None
    newest = next(
        (ref for ref in sets if ref.complete and ref.manifest is not None), None
    )
    if newest is None:
        _refuse(
            "no complete local backup set is available.",
            _PUSH_SET_FIX,
            command="backup push",
        )
        return None

    audit_problem = audit.probe(io, rendered_dir)
    if audit_problem is not None:
        _refuse(
            f"audit writer is unavailable: {audit_problem}",
            stack.logs_fix(rendered_dir, "postgres"),
            command="backup push",
        )
        return None

    try:
        reachable = io.run(sshtarget.ssh_argv(config, "true"))
    except (OSError, subprocess.SubprocessError) as exc:
        _refuse(
            f"backup target SSH probe failed: {exc}.",
            _PUSH_TARGET_FIX,
            command="backup push",
        )
        return None
    if reachable.returncode != 0:
        _refuse(
            f"backup target SSH probe failed: {command_detail(reachable)}.",
            _PUSH_TARGET_FIX,
            command="backup push",
        )
        return None
    return config, newest


def _push_record_stage(
    io: Host,
    *,
    label: str,
    pushed_at: datetime,
    local_set: backupset.SetRef,
) -> tuple[StageResult, backupset.PushRecord | None, str | None]:
    manifest = local_set.manifest
    if manifest is None:
        return (
            StageResult(
                "record",
                False,
                f"local set {local_set.label} has no parsed manifest",
                _PUSH_SET_FIX,
            ),
            None,
            None,
        )
    record = backupset.PushRecord(
        label,
        pushed_at,
        local_set.label,
        manifest.archive_through,
    )
    text = record.to_json()
    try:
        io.write_text(
            os.path.join(backupset.STAGING, backupset.PUSH_RECORD_NAME), text
        )
    except (OSError, UnicodeError) as exc:
        return (
            StageResult(
                "record",
                False,
                f"push record could not be written: {exc}",
                _PUSH_STAGE_FIX,
            ),
            None,
            None,
        )
    return (
        StageResult(
            "record",
            True,
            f"push record written for {local_set.label}",
            "",
        ),
        record,
        text,
    )


def _remote_listing_script(config: SiteConfig) -> str:
    target_path = shlex.quote(_push_remote_root(config))
    return (
        "cd "
        + target_path
        + " && for directory in */; do "
        + '[ -d "$directory" ] || continue; '
        + 'name=${directory%/}; '
        + 'if [ -f "$directory/push.json" ]; then '
        + 'printf "%s\\t1\\n" "$name"; '
        + 'else printf "%s\\t0\\n" "$name"; fi; '
        + "done"
    )


def _parse_remote_listing(
    stdout: str,
) -> tuple[backupset.RemoteSnapshot, ...] | StageResult:
    snapshots: list[backupset.RemoteSnapshot] = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        name, separator, flag = line.partition("\t")
        if not separator or not name or flag not in {"0", "1"}:
            return StageResult(
                "list",
                False,
                f"remote snapshot listing line {line_number} is malformed",
                _PUSH_STAGE_FIX,
            )
        complete = (
            flag == "1"
            and not name.endswith(backupset.PARTIAL_SUFFIX)
            and backupset.LABEL.fullmatch(name) is not None
        )
        snapshots.append(backupset.RemoteSnapshot(name, None, complete))
    return tuple(snapshots)


def list_remote_snapshots(
    io: Host, config: SiteConfig
) -> tuple[backupset.RemoteSnapshot, ...] | StageResult:
    """List the target's snapshot directories through the SSH seam."""

    try:
        result = io.run(
            sshtarget.ssh_argv(
                config,
                sshtarget.remote_script(_remote_listing_script(config)),
            )
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult(
            "list",
            False,
            f"remote snapshot listing failed: {exc}",
            _PUSH_STAGE_FIX,
        )
    if result.returncode != 0:
        return _run_failure(
            "list",
            "remote snapshot listing failed",
            result,
            _PUSH_STAGE_FIX,
        )
    return _parse_remote_listing(result.stdout)


def read_push_record(
    io: Host, config: SiteConfig, snapshot_name: str
) -> backupset.PushRecord | Problem:
    """Read and parse one target snapshot's coverage record."""

    path = _push_remote_path(
        config, f"{snapshot_name}/{backupset.PUSH_RECORD_NAME}"
    )
    try:
        result = io.run(
            sshtarget.ssh_argv(config, "cat", shlex.quote(path))
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(
            f"Remote push record read failed for {snapshot_name}: {exc}.",
            _PUSH_STAGE_FIX,
        )
    if result.returncode != 0:
        return Problem(
            f"Remote push record read failed for {snapshot_name}: "
            f"{command_detail(result)}.",
            _PUSH_STAGE_FIX,
        )
    return backupset.parse_push_record(result.stdout)


def _push_list_stage(
    io: Host,
    config: SiteConfig,
) -> tuple[StageResult, tuple[backupset.RemoteSnapshot, ...], backupset.RemoteSnapshot | None]:
    parsed = list_remote_snapshots(io, config)
    if isinstance(parsed, StageResult):
        return parsed, (), None
    complete = tuple(snapshot for snapshot in parsed if snapshot.complete)
    newest = max(complete, key=lambda snapshot: snapshot.label, default=None)
    return (
        StageResult(
            "list",
            True,
            f"found {len(complete)} complete remote snapshot(s)",
            "",
        ),
        parsed,
        newest,
    )


def parse_rsync_stats(stdout: str) -> PushStats | None:
    values: dict[str, int] = {}
    for line in stdout.splitlines():
        match = _RSYNC_STAT_PATTERN.match(line)
        if match is not None:
            values[match.group(1)] = int(match.group(2).replace(",", ""), 10)
    if set(values) != {"file size", "transferred file size"}:
        return None
    return PushStats(values["file size"], values["transferred file size"])


def _push_stage(
    io: Host,
    config: SiteConfig,
    *,
    label: str,
    previous: backupset.RemoteSnapshot | None,
) -> tuple[StageResult, PushStats | None]:
    argv = [
        "rsync",
        "-aH",
        "--no-owner",
        "--no-group",
        "--delete",
        "--stats",
        "-e",
        sshtarget.rsync_ssh_option(),
    ]
    if previous is not None:
        argv.append(f"--link-dest=../{previous.label}")
    remote_partial = _push_remote_path(config, f"{label}{backupset.PARTIAL_SUFFIX}")
    argv.extend(
        [
            backupset.STAGING.rstrip("/") + "/",
            f"{config.backup.target.user}@{config.backup.target.host}:{remote_partial}/",
        ]
    )
    try:
        result = io.run(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        return (
            StageResult("push", False, f"rsync push failed: {exc}", _PUSH_STAGE_FIX),
            None,
        )
    if result.returncode != 0:
        return _run_failure("push", "rsync push failed", result, _PUSH_STAGE_FIX), None
    stats = parse_rsync_stats(result.stdout)
    if stats is None:
        return (
            StageResult(
                "push",
                False,
                "rsync --stats output was missing total or transferred file size",
                _PUSH_STAGE_FIX,
            ),
            None,
        )
    if previous is not None and stats.transferred_bytes > 0.9 * stats.total_bytes:
        return (
            StageResult(
                "push",
                False,
                f"rsync transferred {stats.transferred_bytes} of {stats.total_bytes} bytes",
                _PUSH_FULL_COPY_FIX,
            ),
            stats,
        )
    return (
        StageResult(
            "push",
            True,
            f"transferred {stats.transferred_bytes} of {stats.total_bytes} bytes",
            "",
        ),
        stats,
    )


def _remote_command_stage(
    io: Host,
    config: SiteConfig,
    *,
    name: str,
    detail: str,
    script: str,
    fix: str = _PUSH_STAGE_FIX,
) -> StageResult:
    try:
        result = io.run(sshtarget.ssh_argv(config, sshtarget.remote_script(script)))
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult(name, False, f"{detail}: {exc}", fix)
    if result.returncode != 0:
        return _run_failure(name, detail, result, fix)
    return StageResult(name, True, detail, "")


def _push_prune_timestamp(name: str) -> datetime | None:
    label = name.removesuffix(backupset.PARTIAL_SUFFIX)
    if backupset.kind_of(label) is not backupset.Kind.NIGHTLY:
        return None
    try:
        return datetime.strptime(label, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _push_prune_names(
    snapshots: Sequence[backupset.RemoteSnapshot],
    *,
    now: datetime,
    remote_days: int,
    current_label: str,
) -> tuple[str, ...]:
    complete_cutoff = now - timedelta(days=remote_days)
    partial_cutoff = now - timedelta(days=1)
    names: list[str] = []
    for snapshot in snapshots:
        timestamp = _push_prune_timestamp(snapshot.label)
        if timestamp is None:
            continue
        if snapshot.label.removesuffix(backupset.PARTIAL_SUFFIX) == current_label:
            continue
        if (
            snapshot.complete and timestamp < complete_cutoff
        ) or (
            snapshot.label.endswith(backupset.PARTIAL_SUFFIX)
            and timestamp < partial_cutoff
        ):
            names.append(snapshot.label)
    return tuple(names)


def _safe_remote_snapshot_path(
    config: SiteConfig,
    name: str,
    current_label: str,
) -> str | None:
    root = os.path.normpath(_push_remote_root(config))
    path = os.path.normpath(_push_remote_path(config, name))
    if root == os.sep:
        inside = path.startswith(os.sep) and path != root
    else:
        inside = path.startswith(root + os.sep)
    if not inside or name.removesuffix(backupset.PARTIAL_SUFFIX) == current_label:
        return None
    return path


def _push_prune_stage(
    io: Host,
    config: SiteConfig,
    *,
    snapshots: Sequence[backupset.RemoteSnapshot],
    now: datetime,
    current_label: str,
) -> tuple[StageResult, int]:
    names = _push_prune_names(
        snapshots,
        now=now,
        remote_days=config.backup.remote_days,
        current_label=current_label,
    )
    removed = 0
    for name in names:
        path = _safe_remote_snapshot_path(config, name, current_label)
        if path is None:
            return (
                StageResult(
                    "prune",
                    False,
                    f"refusing to remove an unsafe target snapshot: {name}",
                    _PUSH_STAGE_FIX,
                ),
                removed,
            )
        result = _remote_command_stage(
            io,
            config,
            name="prune",
            detail=f"removed remote snapshot {name}",
            script=f"rm -rf -- {shlex.quote(path)}",
        )
        if not result.ok:
            return result, removed
        removed += 1
    return StageResult("prune", True, f"pruned {removed} remote snapshot(s)", ""), removed


def _add_check_hash(
    lines: list[str], seen: set[str], relative: str, digest: str
) -> None:
    if relative in seen:
        return
    seen.add(relative)
    lines.append(f"{digest}  {relative}\n")


def _push_check_input(
    io: Host,
    *,
    local_set: backupset.SetRef,
    remote_manifest: backupset.Manifest,
    record_text: str,
    verify_all: bool,
) -> str | StageResult:
    local_manifest_path = os.path.join(
        backupset.set_dir(local_set.label), backupset.MANIFEST_NAME
    )
    try:
        local_manifest_text = io.read_text(local_manifest_path)
    except (OSError, UnicodeError) as exc:
        return StageResult(
            "check",
            False,
            f"local manifest could not be read: {exc}",
            _PUSH_CHECK_FIX,
        )

    lines: list[str] = []
    seen: set[str] = set()
    _add_check_hash(
        lines,
        seen,
        f"sets/{local_set.label}/{backupset.MANIFEST_NAME}",
        hashlib.sha256(local_manifest_text.encode("utf-8")).hexdigest(),
    )
    _add_check_hash(
        lines,
        seen,
        backupset.PUSH_RECORD_NAME,
        hashlib.sha256(record_text.encode("utf-8")).hexdigest(),
    )
    _add_check_hash(
        lines,
        seen,
        f"sets/{local_set.label}/{backupset.TARBALL_NAME}",
        remote_manifest.tarball_sha256,
    )

    repository_entries = {
        entry.path: entry
        for entry in remote_manifest.inventory.get("pgbackrest", ())
        if entry.kind == "f" and entry.sha256 is not None
    }
    # pgBackRest keeps its info files below the stanza directories; every one
    # of these is what a restore reads first, so the set must inventory them.
    special_paths = (
        f"backup/{pgbackrest.STANZA}/backup.info",
        f"archive/{pgbackrest.STANZA}/archive.info",
        f"backup/{pgbackrest.STANZA}/{remote_manifest.pgbackrest_label}/backup.manifest",
    )
    for path in special_paths:
        entry = repository_entries.get(path)
        if entry is None or entry.sha256 is None:
            return StageResult(
                "check",
                False,
                f"the set's repository inventory lacks {path}",
                _PUSH_SET_FIX,
            )
        _add_check_hash(lines, seen, f"pgbackrest/{path}", entry.sha256)

    for root_name in sorted(remote_manifest.inventory):
        file_entries = tuple(
            entry
            for entry in remote_manifest.inventory[root_name]
            if entry.kind == "f" and entry.sha256 is not None
        )
        paths = tuple(entry.path for entry in file_entries)
        selected = (
            paths
            if verify_all
            else backupset.sample_paths(paths, percent=1, floor=1)
        )
        entries_by_path = {entry.path: entry for entry in file_entries}
        for path in selected:
            entry = entries_by_path[path]
            relative = (
                f"pgbackrest/{path}"
                if root_name == "pgbackrest"
                else f"sets/{local_set.label}/{backupset.FILES_DIR}/{root_name}/{path}"
            )
            assert entry.sha256 is not None
            _add_check_hash(lines, seen, relative, entry.sha256)
    return "".join(lines)


def _push_check_stage(
    io: Host,
    config: SiteConfig,
    *,
    local_set: backupset.SetRef,
    remote_label: str,
    record_text: str,
    verify_all: bool,
) -> tuple[StageResult, _PushCheck]:
    remote_manifest_path = _push_remote_path(
        config,
        f"{remote_label}/sets/{local_set.label}/{backupset.MANIFEST_NAME}",
    )
    try:
        fetched = io.run(
            sshtarget.ssh_argv(config, "cat", shlex.quote(remote_manifest_path))
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return (
            StageResult("check", False, f"remote manifest read failed: {exc}", _PUSH_CHECK_FIX),
            _PushCheck(0, 0),
        )
    if fetched.returncode != 0:
        return (
            _run_failure("check", "remote manifest read failed", fetched, _PUSH_CHECK_FIX),
            _PushCheck(0, 0),
        )
    try:
        parsed = backupset.parse_manifest(fetched.stdout)
    except (TypeError, ValueError) as exc:
        return (
            StageResult(
                "check",
                False,
                f"remote manifest could not be parsed: {exc}",
                _PUSH_CHECK_FIX,
            ),
            _PushCheck(0, 0),
        )
    if isinstance(parsed, backupset.Problem):
        return (
            StageResult("check", False, parsed.problem, _PUSH_CHECK_FIX),
            _PushCheck(0, 0),
        )
    expected = _push_check_input(
        io,
        local_set=local_set,
        remote_manifest=parsed,
        record_text=record_text,
        verify_all=verify_all,
    )
    if isinstance(expected, StageResult):
        return expected, _PushCheck(0, 0)
    try:
        checked = io.run(
            sshtarget.remote_check_argv(
                config, _push_remote_path(config, remote_label)
            ),
            input=expected,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return (
            StageResult("check", False, f"remote checksum check failed: {exc}", _PUSH_CHECK_FIX),
            _PushCheck(0, 0),
        )
    okay, failed = sshtarget.parse_check_output(checked.stdout)
    report = _PushCheck(len(okay), len(failed))
    if failed:
        return (
            StageResult(
                "check",
                False,
                f"remote checksum verification failed for {len(failed)} path(s)",
                _PUSH_CHECK_FIX,
            ),
            report,
        )
    if checked.returncode != 0:
        return (
            _run_failure("check", "remote checksum check failed", checked, _PUSH_CHECK_FIX),
            report,
        )
    return (
        StageResult("check", True, f"verified {len(okay)} path(s) on the target", ""),
        report,
    )


def _push_audit_stage(
    io: Host,
    rendered_dir: PathLike,
    *,
    run_id: str,
    label: str,
    newest_set: str,
    started: datetime,
    finished: datetime,
    stats: PushStats,
    checked: _PushCheck,
    pruned: int,
    verify_all: bool,
) -> StageResult:
    row = audit.AuditRow(
        run_id,
        "backup_push",
        None,
        None,
        None,
        (),
        {
            "label": label,
            "newest_set": newest_set,
            "duration_s": max(0.0, (finished - started).total_seconds()),
            "transferred_bytes": stats.transferred_bytes,
            "total_bytes": stats.total_bytes,
            "verified": checked.verified,
            "failed": checked.failed,
            "pruned": pruned,
            "verify_all": verify_all,
        },
    )
    problem = audit.write_rows(io, rendered_dir, (row,))
    if problem is not None:
        return StageResult(
            "audit",
            False,
            f"backup push audit write failed: {problem}",
            stack.logs_fix(rendered_dir, "postgres"),
        )
    return StageResult(
        "audit",
        True,
        f"backup push {label} recorded ({checked.verified} verified, {pruned} pruned)",
        "",
    )


def run_backup_push(
    args: object,
    *,
    host: Host | None = None,
    rendered_dir: PathLike = _RENDERED_DIR,
    site_path: PathLike = _SITE_PATH,
    now: datetime | None = None,
) -> int:
    """Push the complete local staging tree to the configured backup target."""

    io = host or RealHost()
    if io.geteuid() != 0:
        return _refuse("root is required.", _PUSH_ROOT_FIX, command="backup push")
    started = aware_now(now)
    if started is None:
        return _refuse(
            "backup push's clock value must be timezone-aware.",
            "Use an aware UTC time, then retry.",
            command="backup push",
        )
    now_was_supplied = now is not None
    prerequisites = _push_preconditions(
        io, site_path=site_path, rendered_dir=rendered_dir
    )
    if prerequisites is None:
        return 1
    config, local_set = prerequisites
    label = backupset.nightly_label(started)
    run_id = str(uuid.uuid4())
    verify_all = bool(getattr(args, "verify_all", False))

    record_result, record, record_text = _push_record_stage(
        io,
        label=label,
        pushed_at=started,
        local_set=local_set,
    )
    print_stage(record_result)
    if not record_result.ok or record is None or record_text is None:
        return 1

    list_result, snapshots, previous = _push_list_stage(io, config)
    print_stage(list_result)
    if not list_result.ok:
        return 1

    push_result, stats = _push_stage(
        io, config, label=label, previous=previous
    )
    print_stage(push_result)
    if not push_result.ok or stats is None:
        return 1

    remote_partial = _push_remote_path(
        config, f"{label}{backupset.PARTIAL_SUFFIX}"
    )
    remote_final = _push_remote_path(config, label)
    finalize = _remote_command_stage(
        io,
        config,
        name="finalize",
        detail=f"renamed remote snapshot {label}",
        script=f"mv -- {shlex.quote(remote_partial)} {shlex.quote(remote_final)}",
    )
    print_stage(finalize)
    if not finalize.ok:
        return 1

    prune_result, pruned = _push_prune_stage(
        io,
        config,
        snapshots=snapshots,
        now=started,
        current_label=label,
    )
    print_stage(prune_result)
    if not prune_result.ok:
        return 1

    check_result, checked = _push_check_stage(
        io,
        config,
        local_set=local_set,
        remote_label=label,
        record_text=record_text,
        verify_all=verify_all,
    )
    print_stage(check_result)
    if not check_result.ok:
        return 1

    finished = started if now_was_supplied else datetime.now(UTC)
    audit_result = _push_audit_stage(
        io,
        rendered_dir,
        run_id=run_id,
        label=label,
        newest_set=local_set.label,
        started=started,
        finished=finished,
        stats=stats,
        checked=checked,
        pruned=pruned,
        verify_all=verify_all,
    )
    print_stage(audit_result)
    return int(not audit_result.ok)
