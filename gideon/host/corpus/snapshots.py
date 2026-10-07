"""Fetch and verify dated corpus snapshot files through the host seam."""

import re
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from gideon.host import fetch, report
from gideon.host.corpus import resolve
from gideon.host.corpus.lockfile import SidecarEntry
from gideon.host.render.worker import KEPT_FORM, RECORD_SUFFIX
from gideon.host.report import Problem, StageResult
from gideon.host.sysio import PathLike, WritableBytesHost

# exempt: mechanics — progress while snapshot transfers continue without a bound.
HEARTBEAT_SECONDS = 60


@dataclass(frozen=True, slots=True)
class FetchedSource:
    """Whole file records and the number of transfers deferred for a source."""

    records: Mapping[str, fetch.FetchRecord]
    deferred: int
    deferred_paths: frozenset[str]

    def summary(self) -> str:
        """The fetch row's counts: files, bytes, and transfers deferred."""
        size = sum(item.size for item in self.records.values())
        action = (
            "no fetches deferred" if self.deferred == 0
            else f"{self.deferred} {'fetch' if self.deferred == 1 else 'fetches'} deferred"
        )
        return f"{len(self.records)} files, {size} bytes; {action}"

    def latest_fetch(self) -> str | None:
        """The latest record time among the source's files, if it has any."""
        times = [datetime.fromisoformat(item.fetched_at) for item in self.records.values()]
        return max(times).isoformat() if times else None


def retry_fix(fix: str, *, command_path: str) -> str:
    return resolve.retry_fix(fix, command_path)


def refetch_fix(path: Path, *, command_path: str) -> str:
    # The worker never fetches over a whole record, so the record goes with the file.
    return (
        f"Remove {path} and {path}{RECORD_SUFFIX}, then run "
        f"{report.command(command_path)} again to fetch it anew."
    )


def fetch_source(
    host: WritableBytesHost,
    rendered_dir: PathLike,
    snapshots_root: PathLike,
    source_name: str,
    snapshot_date: str,
    entries: Sequence[tuple[str, str]],
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    command_path: str,
) -> FetchedSource | Problem:
    """Keep whole records, defer absent files, and wait for pending transfers."""
    records: dict[str, fetch.FetchRecord] = {}
    pending: dict[str, tuple[int, str]] = {}
    for path, url in entries:
        try:
            destination = fetch.snapshot_destination(source_name, snapshot_date, path)
        except ValueError:
            return Problem(
                f"source {source_name} snapshot path {path!r} is invalid",
                retry_fix("Correct the source's index reader in the release.", command_path=command_path),
            )
        target_path = Path(snapshots_root) / destination
        existing = fetch.read_record(host, destination, snapshots_root=snapshots_root)
        if isinstance(existing, Problem):
            if existing.problem == "fetch file does not match its record":
                return Problem(
                    f"{target_path}: {existing.problem}",
                    refetch_fix(target_path, command_path=command_path),
                )
            return Problem(f"{path}: {existing.problem}",
                           retry_fix(existing.fix, command_path=command_path))
        if existing is not None:
            records[path] = existing
            continue
        try:
            present = host.exists(target_path)
        except OSError as exc:
            return Problem(
                f"{target_path}: file presence could not be checked ({type(exc).__name__})",
                retry_fix(f"Restore access to {target_path}.", command_path=command_path),
            )
        if present:
            return Problem(
                f"{target_path}: file is present without a whole fetch record",
                refetch_fix(target_path, command_path=command_path),
            )
        job = fetch.defer_fetch(
            host, rendered_dir, destination=destination, url=url, form=KEPT_FORM,
        )
        if isinstance(job, Problem):
            return Problem(f"{path}: {job.problem}",
                           retry_fix(job.fix, command_path=command_path))
        pending[path] = (job, destination)
    deferred_paths = frozenset(pending)
    deferred = len(deferred_paths)
    heartbeat_at = monotonic()
    while pending:
        for path, (job, destination) in tuple(pending.items()):
            outcome = fetch.read_fetch(
                host, rendered_dir, job, destination=destination,
                snapshots_root=snapshots_root,
            )
            if isinstance(outcome, Problem):
                return Problem(f"{path}: {outcome.problem}",
                               retry_fix(outcome.fix, command_path=command_path))
            if outcome.failure is not None:
                return Problem(f"{path}: {outcome.failure.problem}",
                               retry_fix(outcome.failure.fix, command_path=command_path))
            if outcome.record is not None:
                records[path] = outcome.record
                del pending[path]
                report.print_stage(StageResult(
                    "fetch", True, f"{path}: {outcome.record.size} bytes", "",
                ))
        if not pending:
            break
        now = monotonic()
        if now - heartbeat_at >= HEARTBEAT_SECONDS:
            report.print_stage(StageResult(
                "fetch", True,
                f"{len(records)}/{len(entries)} files, "
                f"{sum(item.size for item in records.values())} bytes", "",
            ))
            heartbeat_at = now
        sleep(1.0)
    return FetchedSource(records, deferred, deferred_paths)


def verify(
    host: WritableBytesHost,
    snapshots_root: PathLike,
    source_name: str,
    snapshot_date: str,
    entries: Sequence[tuple[str, tuple[str, int] | None]],
    *,
    command_path: str,
    expected_paths: Set[str] | None = None,
    fetched_paths: Set[str] = frozenset(),
    repin_fix: str = "Set mirror_url in the lockfile to a source serving the pinned bytes, or make a new cut.",
) -> tuple[tuple[SidecarEntry, ...] | None, StageResult | None]:
    """Compare files with their records and any supplied sidecar pins."""
    verified: list[SidecarEntry] = []
    seen: set[str] = set()
    for relative_path, pin in entries:
        try:
            destination = fetch.snapshot_destination(source_name, snapshot_date, relative_path)
        except ValueError:
            return None, StageResult("verify", False, f"invalid snapshot path {relative_path!r}",
                                     "Correct the source's index reader in the release, then run "
                                     f"{report.command(command_path)} again.")
        path = Path(snapshots_root) / destination
        fix = refetch_fix(path, command_path=command_path)
        if relative_path in seen:
            return None, StageResult("verify", False, f"repeated snapshot path {relative_path}", fix)
        seen.add(relative_path)
        record = fetch.read_record(host, destination, snapshots_root=snapshots_root)
        if isinstance(record, Problem):
            return None, StageResult("verify", False, f"{path}: {record.problem}", fix)
        if record is None:
            return None, StageResult("verify", False, f"{path}: whole fetch record is missing", fix)
        try:
            info = host.stat(path)
            result = host.run(("sha256sum", str(path)))
        except (OSError, subprocess.SubprocessError) as exc:
            return None, StageResult(
                "verify", False, f"{path}: hash tool could not run ({type(exc).__name__})",
                f"Restore sha256sum, then run {report.command(command_path)} again.",
            )
        if result.returncode != 0:
            return None, StageResult(
                "verify", False, f"{path}: sha256sum failed (exit {result.returncode})",
                f"Restore sha256sum and access to this file, then run {report.command(command_path)} again.",
            )
        match = re.fullmatch(r"([0-9a-f]{64})  " + re.escape(str(path)) + r"\n?", result.stdout)
        if match is None or not stat.S_ISREG(info.st_mode):
            return None, StageResult("verify", False, f"{path}: invalid hash result or file type", fix)
        digest = match.group(1)
        size = info.st_size
        if (digest, size) != (record.sha256, record.size):
            return None, StageResult("verify", False, f"{path}: file disagrees with its fetch record", fix)
        if ((expected_paths is not None and relative_path not in expected_paths)
            or (pin is not None and pin != (digest, size))):
            if pin is not None and relative_path in fetched_paths:
                fix = f"{repin_fix.rstrip('.')}. Then {fix[0].lower()}{fix[1:]}"
            return None, StageResult("verify", False, f"{path}: file disagrees with its pinned sidecar", fix)
        verified.append(SidecarEntry(relative_path, digest, size))
        report.print_stage(StageResult("verify", True, f"{path}: {size} bytes, sha256 {digest}", ""))
    if expected_paths is not None and expected_paths != seen:
        return None, StageResult(
            "verify", False, f"source {source_name} no longer has the pinned file list",
            f"Restore the snapshot files, then run {report.command(command_path)} again.",
        )
    return tuple(sorted(verified, key=lambda item: item.path)), None
