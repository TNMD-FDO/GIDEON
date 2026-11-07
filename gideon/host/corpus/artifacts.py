"""Load the worker and release artifacts needed by corpus commands."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from gideon.host import report, worker
from gideon.host.corpus.lockfile import (
    DEFAULT_LINE_WORD,
    Lockfile,
    LockfileDirectoryResult,
    LockfileError,
    read_lockfile_directory,
)
from gideon.host.corpus.sources import SourceDefinition
from gideon.host.courts import CourtMap, CourtsError, load_court_map
from gideon.host.egress import EgressAllowlist, EgressError, load_egress_allowlist
from gideon.host.report import Problem, StageResult
from gideon.host.sysio import PathLike, WritableBytesHost


@dataclass(frozen=True, slots=True)
class CorpusArtifacts:
    """Loaded court map, allowlist, and complete lockfile directory."""

    court_map: CourtMap
    allowlist: EgressAllowlist
    directory: LockfileDirectoryResult


def line_problem(
    directory: LockfileDirectoryResult, lockfile: Lockfile, line: str | None,
    *, command_path: str,
) -> Problem | None:
    """Judge a label's line; *command_path* is the command before its label."""
    if lockfile.line == line:
        return None
    newest = directory.newest_on(line)
    if newest is None:
        fix = (
            f"Commit a corpus lockfile on line {line or DEFAULT_LINE_WORD!r}, "
            f"then run {report.command(command_path)} with its label"
        )
    else:
        fix = f"Run {report.command(f'{command_path} {newest.label}')}"
    return Problem(
        f"Corpus lockfile {lockfile.label} is on line {lockfile.line or DEFAULT_LINE_WORD!r}; "
        f"the profile's line is {line or DEFAULT_LINE_WORD!r}.",
        f"{fix}, or set hardware_profile in /etc/gideon/site.yaml to a profile on line "
        f"{lockfile.line or DEFAULT_LINE_WORD!r} and run "
        f"{report.command(f'{command_path} {lockfile.label}')} again.",
    )


def artifact_problem(
    errors: Sequence[CourtsError | EgressError | LockfileError], artifact: str,
    *, command_path: str,
) -> StageResult | None:
    if not errors:
        return None
    problems = "; ".join(error.problem for error in errors)
    fixes = "; ".join(dict.fromkeys(error.fix for error in errors))
    return StageResult(
        "preconditions", False, f"{artifact}: {problems}",
        f"{fixes} Then run {report.command(command_path)} again.",
    )


def load_artifacts(
    host: WritableBytesHost,
    rendered_dir: PathLike,
    checkout: Path,
    registry: Sequence[SourceDefinition],
    *, command_path: str, check_worker: bool = True,
) -> tuple[CorpusArtifacts | None, StageResult | None]:
    """Load all committed lockfiles and their shared dependencies."""
    if check_worker:
        worker_stage = worker.worker_preconditions(
            host, rendered_dir, command_path=command_path,
        )
        if not worker_stage.ok:
            return None, worker_stage

    courts_result = load_court_map(checkout / "courts.yaml", host=host)
    issue = artifact_problem(courts_result.errors, "court map", command_path=command_path)
    if issue is not None:
        return None, issue
    court_map = courts_result.court_map
    assert court_map is not None

    egress_result = load_egress_allowlist(checkout / "config/egress.yaml", host=host)
    issue = artifact_problem(egress_result.errors, "egress allowlist", command_path=command_path)
    if issue is not None:
        return None, issue
    allowlist = egress_result.allowlist
    assert allowlist is not None

    directory = read_lockfile_directory(
        checkout / "corpus/lockfiles",
        known_sources={source.name: source for source in registry}, host=host,
    )
    issue = artifact_problem(directory.errors, "corpus lockfiles", command_path=command_path)
    if issue is not None:
        return None, issue
    return CorpusArtifacts(court_map, allowlist, directory), None
