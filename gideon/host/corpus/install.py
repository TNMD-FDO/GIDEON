"""Fetch and verify the files pinned by a committed corpus lockfile."""

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from gideon.host import backuplock, report, stack
from gideon.host.corpus import artifacts, record, snapshots
from gideon.host.corpus.lockfile import (
    LABEL,
    Lockfile,
    check_courts,
    check_egress_hosts,
)
from gideon.host.corpus.sources import SOURCES, SourceDefinition
from gideon.host.render.worker import RESOLVE_DIR, SNAPSHOTS_ROOT
from gideon.host.report import Problem, StageResult
from gideon.host.sysio import LockingHost, PathLike, RealHost, WritableBytesHost

COMMAND_PATH = "corpus install"


class CorpusHost(WritableBytesHost, LockingHost, Protocol):
    """The host operations needed for a locked corpus install."""

    def listdir(self, path: PathLike) -> list[str]: ...

    def rmtree(self, path: PathLike) -> None: ...


def _retry_fix(fix: str) -> str:
    return snapshots.retry_fix(fix, command_path=COMMAND_PATH)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _refuse(name: str, detail: str, fix: str) -> int:
    stage = StageResult(name, False, detail, fix)
    if name == "preconditions":
        print(report.stage_line(stage), file=sys.stderr)
    else:
        report.print_stage(stage)
    return 1


def _preconditions(
    args: argparse.Namespace,
    host: CorpusHost,
    rendered_dir: PathLike,
    checkout: Path,
    registry: Sequence[SourceDefinition],
    clock: Callable[[], datetime],
) -> tuple[StageResult, Lockfile | None, backuplock.Claim | None]:
    label = args.label
    if LABEL.fullmatch(label) is None:
        return StageResult(
            "preconditions", False, f"lockfile label {label!r} has the wrong form",
            f"Use a label in the form corpus-YYYY-MM-DD, then run {report.command(COMMAND_PATH)} again.",
        ), None, None
    if host.geteuid() != 0:
        return StageResult(
            "preconditions", False, "root privileges are required",
            f"Run {report.command(COMMAND_PATH)}, then retry.",
        ), None, None
    claim = backuplock.claim(
        host, command=report.command_name(COMMAND_PATH), now=clock(),
        lock=backuplock.CORPUS_LOCK,
    )
    if claim.refusal is not None:
        return StageResult(
            "preconditions", False, claim.refusal.detail, _retry_fix(claim.refusal.fix),
        ), None, None
    if not claim.taken:
        return StageResult(
            "preconditions", False, "a corpus command is already running in this process",
            f"Wait for it to finish, then run {report.command(COMMAND_PATH)} again.",
        ), None, None
    try:
        stage, lockfile = _artifact_preconditions(host, rendered_dir, checkout, registry, label)
    except BaseException:
        backuplock.release_claim(host, claim, lock=backuplock.CORPUS_LOCK)
        raise
    return stage, lockfile, claim


def _artifact_preconditions(
    host: WritableBytesHost,
    rendered_dir: PathLike,
    checkout: Path,
    registry: Sequence[SourceDefinition],
    label: str,
) -> tuple[StageResult, Lockfile | None]:
    loaded, issue = artifacts.load_artifacts(
        host, rendered_dir, checkout, registry, command_path=COMMAND_PATH,
    )
    if issue is not None:
        return issue, None
    assert loaded is not None
    lockfile = next((item for item in loaded.directory.lockfiles if item.label == label), None)
    if lockfile is None:
        labels = ", ".join(item.label for item in loaded.directory.lockfiles) or "none"
        return StageResult(
            "preconditions", False, f"lockfile label {label} is not committed",
            f"Use a committed label from corpus/lockfiles/: {labels}; then run "
            f"{report.command(COMMAND_PATH)} again.",
        ), None
    issue = artifacts.artifact_problem(
        check_courts(lockfile, loaded.court_map), label, command_path=COMMAND_PATH,
    )
    if issue is not None:
        return issue, None
    issue = artifacts.artifact_problem(
        check_egress_hosts(lockfile, loaded.allowlist), label, command_path=COMMAND_PATH,
    )
    if issue is not None:
        return issue, None
    row = record.read_cut(host, rendered_dir, label, command_path=COMMAND_PATH)
    if isinstance(row, Problem):
        return StageResult("preconditions", False, row.problem, row.fix), None
    if row is not None and row.state == "superseded":
        newest = loaded.directory.newest
        assert newest is not None
        return StageResult(
            "preconditions", False, f"lockfile label {label} is superseded",
            f"Run {report.command(f'{COMMAND_PATH} {newest.label}')} instead.",
        ), None
    snapshots_text = ", ".join(
        f"{name} {pin.snapshot_date}" for name, pin in lockfile.sources.items()
    )
    state = row.state if row is not None else "not yet recorded"
    return StageResult(
        "preconditions", True, f"{label}: {snapshots_text}; {state}", "",
    ), lockfile


def _run_install_stages(
    host: CorpusHost,
    rendered_dir: PathLike,
    snapshots_root: PathLike,
    lockfile: Lockfile,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    active_stage: list[str],
) -> int:
    active_stage[0] = "fetch"
    fetched: dict[str, snapshots.FetchedSource] = {}
    for name, pin in lockfile.sources.items():
        base = (pin.mirror_url or pin.base_url).rstrip("/") + "/"
        result = snapshots.fetch_source(
            host, rendered_dir, snapshots_root, name, pin.snapshot_date,
            tuple((entry.path, base + entry.path) for entry in pin.entries),
            sleep=sleep, monotonic=monotonic, command_path=COMMAND_PATH,
        )
        if isinstance(result, Problem):
            return _refuse("fetch", f"{name}: {result.problem}", result.fix)
        fetched[name] = result
        report.print_stage(StageResult("fetch", True, f"{name}: {result.summary()}", ""))
    active_stage[0] = "verify"
    verified_count = 0
    for name, pin in lockfile.sources.items():
        entries, issue = snapshots.verify(
            host, snapshots_root, name, pin.snapshot_date,
            tuple((entry.path, (entry.sha256, entry.size)) for entry in pin.entries),
            command_path=COMMAND_PATH,
            expected_paths={entry.path for entry in pin.entries},
            fetched_paths=fetched[name].deferred_paths,
            repin_fix=(
                f"Set mirror_url for {name} in corpus/lockfiles/{lockfile.label}.yaml "
                "to a source serving the pinned bytes, or make a new cut."
            ),
        )
        if issue is not None:
            return _refuse("verify", issue.detail, issue.fix)
        assert entries is not None
        verified_count += len(entries)
    report.print_stage(StageResult(
        "verify", True, f"{verified_count} files match fetch records and pinned sidecars", "",
    ))
    active_stage[0] = "record"
    verified_at = clock()
    if verified_at.tzinfo is None:
        return _refuse("record", "install clock has no timezone",
                       f"Use a UTC clock, then run {report.command(COMMAND_PATH)} again.")
    fetched_at: dict[str, str] = {}
    for name, result in fetched.items():
        latest = result.latest_fetch()
        if latest is None:
            return _refuse(
                "record", f"source {name} has no fetch records",
                _retry_fix("Restore its snapshot files."),
            )
        fetched_at[name] = latest
    write_issue = record.write_install(
        host, rendered_dir, lockfile, fetched_at, verified_at.astimezone(UTC).isoformat(),
        command_path=COMMAND_PATH,
    )
    if write_issue is not None:
        return _refuse("record", write_issue.problem, write_issue.fix)
    row = record.read_cut(host, rendered_dir, lockfile.label, command_path=COMMAND_PATH)
    if isinstance(row, Problem):
        return _refuse("record", row.problem, row.fix)
    if row is None:
        return _refuse(
            "record", f"lockfile {lockfile.label} was not found after writing",
            _retry_fix(f"Run {stack.logs_fix(rendered_dir, 'postgres')} to inspect the record."),
        )
    report.print_stage(StageResult("record", True, f"{row.label}: {row.state}", ""))
    active_stage[0] = "retain"
    return _retain(host, rendered_dir, snapshots_root)


def _retain(host: CorpusHost, rendered_dir: PathLike, snapshots_root: PathLike) -> int:
    rows = record.read_lockfiles(host, rendered_dir, command_path=COMMAND_PATH)
    if isinstance(rows, Problem):
        return _refuse("retain", rows.problem, rows.fix)
    previous: record.CutRow | None = None
    latest: datetime | None = None
    for row in rows:
        if row.state != "superseded" or row.installed_at is None:
            continue
        try:
            installed_at = datetime.fromisoformat(row.installed_at.replace("Z", "+00:00"))
        except ValueError:
            return _refuse(
                "retain", f"lockfile {row.label} has an invalid installed_at",
                _retry_fix(f"Inspect the record with {stack.logs_fix(rendered_dir, 'postgres')}."),
            )
        if installed_at.tzinfo is None:
            return _refuse(
                "retain", f"lockfile {row.label} has an installed_at without a timezone",
                _retry_fix(f"Inspect the record with {stack.logs_fix(rendered_dir, 'postgres')}."),
            )
        if latest is None or installed_at > latest:
            latest, previous = installed_at, row

    live = {"cut", "installing", "installed"}
    kept_labels = tuple(sorted(
        row.label for row in rows if row.state in live or row == previous
    ))
    bindings: dict[str, list[record.CutRow]] = {}
    for row in rows:
        for binding in row.sources:
            name = f"{binding.source}-{binding.snapshot_date}"
            bindings.setdefault(name, []).append(row)
    try:
        names = host.listdir(snapshots_root)
    except OSError as exc:
        return _refuse(
            "retain", f"snapshots root {snapshots_root} could not be listed ({type(exc).__name__})",
            f"Run {report.command('host provision')}, then run {report.command(COMMAND_PATH)} again.",
        )
    kept = removed = unpinned = 0
    for name in sorted(names):
        if name == RESOLVE_DIR:
            continue
        pinned = bindings.get(name)
        if pinned is None:
            unpinned += 1
        elif any(row.state in live or row == previous for row in pinned):
            kept += 1
        else:
            directory = Path(snapshots_root) / name
            try:
                host.rmtree(directory)
            except OSError as exc:
                return _refuse(
                    "retain", f"could not remove {directory} ({type(exc).__name__})",
                    _retry_fix(f"Restore access to {directory}."),
                )
            removed += 1
            report.print_stage(StageResult("retain", True, f"removed {directory}", ""))
    labels = ", ".join(kept_labels) or "none"
    report.print_stage(StageResult(
        "retain", True,
        f"{kept} kept ({labels}), {removed} removed, {unpinned} unpinned", "",
    ))
    return 0


def run_corpus_install(
    args: argparse.Namespace,
    *,
    host: CorpusHost | None = None,
    rendered_dir: PathLike = "/etc/gideon/rendered",
    checkout: PathLike | None = None,
    snapshots_root: PathLike = SNAPSHOTS_ROOT,
    clock: Callable[[], datetime] = _utc_now,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    sources: Sequence[SourceDefinition] = SOURCES,
) -> int:
    """Run the install's preconditions, fetch, verify, record, and retain stages."""
    io = host if host is not None else RealHost()
    root = Path(checkout) if checkout is not None else Path(__file__).parents[3]
    active_stage = ["preconditions"]
    lock_claim: backuplock.Claim | None = None
    try:
        stage, lockfile, lock_claim = _preconditions(args, io, rendered_dir, root, sources, clock)
        if not stage.ok:
            return _refuse("preconditions", stage.detail, stage.fix)
        assert lockfile is not None
        report.print_stage(stage)
        return _run_install_stages(
            io, rendered_dir, snapshots_root, lockfile, clock, sleep, monotonic, active_stage,
        )
    except KeyboardInterrupt:
        if active_stage[0] == "fetch":
            return _refuse(
                "fetch", "interrupted; downloads continue in the worker",
                f"Run {report.command(COMMAND_PATH)} again to rejoin them.",
            )
        if active_stage[0] == "retain":
            return _refuse(
                "retain", "interrupted; a directory removed in part is removed whole by the next run",
                f"Run {report.command(COMMAND_PATH)} again to finish retention.",
            )
        return _refuse(
            active_stage[0], "interrupted; nothing was recorded",
            f"Run {report.command(COMMAND_PATH)} again to start again.",
        )
    finally:
        if lock_claim is not None:
            backuplock.release_claim(io, lock_claim, lock=backuplock.CORPUS_LOCK)
