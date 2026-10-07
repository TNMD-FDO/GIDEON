"""Read fresh corpus indexes through the worker and resolve source snapshots."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gideon.host import fetch, report, stack
from gideon.host.corpus.sources import SourceDefinition, SourceResolution
from gideon.host.render.worker import FRESH_FORM, RESOLVE_DIR, WORKER_SERVICE_NAME
from gideon.host.report import Problem
from gideon.host.sysio import LockingHost, PathLike, WritableBytesHost

# exempt: mechanics — fresh index reads have a short bound, unlike snapshot transfers.
RESOLVE_TIMEOUT_SECONDS = 60


class CorpusHost(WritableBytesHost, LockingHost, Protocol):
    """The host operations needed to clean fresh index directories."""

    def rmtree(self, path: PathLike) -> None: ...


@dataclass(frozen=True, slots=True)
class ResolveFailure:
    """A failed source resolution with its reason and operator fix."""

    reason: str
    problem: Problem


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    """A source resolution and the index bytes that selected it."""

    snapshot: SourceResolution
    index_bytes: Mapping[str, bytes]

    @classmethod
    def from_index(
        cls, source: SourceDefinition, index_bytes: Mapping[str, bytes], *, command_path: str
    ) -> "ResolvedSource | ResolveFailure":
        """Interpret fetched index bytes through their source definition."""

        snapshot = source.read_index(index_bytes)
        if isinstance(snapshot, Problem):
            return ResolveFailure(
                "unreadable-index",
                Problem(snapshot.problem, retry_fix(snapshot.fix, command_path)),
            )
        return cls(snapshot, index_bytes)


def retry_fix(fix: str, command_path: str) -> str:
    """End a fix with the calling command's re-run."""

    clean = fix.removesuffix("then retry.").rstrip(" ,—")
    if clean != fix:
        clean += "."
    return f"{clean} Then run {report.command(command_path)} again."


def wait_for_fetch(
    host: WritableBytesHost,
    rendered_dir: PathLike,
    snapshots_root: PathLike,
    job_id: int,
    destination: str,
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> fetch.FetchRecord | ResolveFailure:
    """Wait for a fresh index transfer under the resolve bound."""

    started = monotonic()
    while True:
        outcome = fetch.read_fetch(
            host, rendered_dir, job_id, destination=destination,
            snapshots_root=snapshots_root,
        )
        if isinstance(outcome, Problem):
            reason = "missing-index" if isinstance(outcome, fetch.MissingFetchRecord) else "local"
            return ResolveFailure(reason, outcome)
        if outcome.failure is not None:
            assert outcome.reason is not None
            return ResolveFailure(outcome.reason, outcome.failure)
        if outcome.record is not None:
            return outcome.record
        if monotonic() - started >= RESOLVE_TIMEOUT_SECONDS:
            return ResolveFailure(
                "timeout",
                Problem(
                    f"index fetch job {job_id} did not finish before the bound",
                    f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}.",
                ),
            )
        sleep(1.0)


def read_source_index(
    host: WritableBytesHost,
    rendered_dir: PathLike,
    snapshots_root: PathLike,
    source: SourceDefinition,
    *,
    command_path: str,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> ResolvedSource | ResolveFailure:
    """Fetch and interpret every index document for one source."""

    documents: dict[str, bytes] = {}
    for request in source.index_documents():
        try:
            destination = fetch.resolve_destination(f"{source.name}/{request.name}")
        except ValueError:
            return ResolveFailure(
                "local",
                Problem(
                    f"source {source.name} index path {request.name!r} is invalid",
                    retry_fix("Correct the source's index document name in the release.", command_path),
                ),
            )
        job = fetch.defer_fetch(
            host, rendered_dir, destination=destination, url=request.url, form=FRESH_FORM,
        )
        if isinstance(job, Problem):
            return ResolveFailure(
                "local", Problem(f"{request.name}: {job.problem}", retry_fix(job.fix, command_path))
            )
        outcome = wait_for_fetch(
            host, rendered_dir, snapshots_root, job, destination,
            sleep=sleep, monotonic=monotonic,
        )
        if isinstance(outcome, ResolveFailure):
            issue = outcome.problem
            return ResolveFailure(
                outcome.reason,
                Problem(f"{request.name}: {issue.problem}", retry_fix(issue.fix, command_path)),
            )
        documents[request.name] = host.read_bytes(Path(snapshots_root) / destination)
    return ResolvedSource.from_index(source, documents, command_path=command_path)


def resolve_source(
    host: CorpusHost,
    rendered_dir: PathLike,
    snapshots_root: PathLike,
    source: SourceDefinition,
    *,
    command_path: str,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> ResolvedSource | ResolveFailure:
    """Resolve one source and remove its fresh files on every outcome."""

    directory = Path(snapshots_root) / RESOLVE_DIR / source.name
    cleanup_issue: ResolveFailure | None = None
    try:
        try:
            result = read_source_index(
                host, rendered_dir, snapshots_root, source, command_path=command_path,
                sleep=sleep, monotonic=monotonic,
            )
        except OSError as exc:
            result = ResolveFailure(
                "local",
                Problem(
                    f"source {source.name} index file could not be read ({type(exc).__name__})",
                    retry_fix(f"Restore access to {directory}.", command_path),
                ),
            )
    finally:
        try:
            if host.exists(directory):
                host.rmtree(directory)
        except OSError as exc:
            cleanup_issue = ResolveFailure(
                "local",
                Problem(
                    f"source {source.name} resolve files could not be removed ({type(exc).__name__})",
                    retry_fix(f"Restore access to {directory}.", command_path),
                ),
            )
    return cleanup_issue if cleanup_issue is not None else result
