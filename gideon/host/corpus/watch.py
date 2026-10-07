"""Observe upstream corpus indexes and record whether a cut is due."""

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from gideon.host import backuplock, report, stack, worker
from gideon.host.corpus import record, resolve
from gideon.host.corpus.sources import SOURCES, SourceDefinition
from gideon.host.egress import load_egress_allowlist
from gideon.host.render.worker import SNAPSHOTS_ROOT, WORKER_SERVICE_NAME
from gideon.host.report import Problem, StageResult
from gideon.host.sysio import PathLike, RealHost


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
    host: resolve.CorpusHost,
    rendered_dir: PathLike,
    checkout: Path,
    sources: Sequence[SourceDefinition],
    clock: Callable[[], datetime],
) -> tuple[StageResult, backuplock.Claim | None, dict[str, str]]:
    if host.geteuid() != 0:
        return StageResult(
            "preconditions", False, "root privileges are required",
            f"Run {report.command('corpus watch')}, then retry.",
        ), None, {}
    declared = stack.declared_services(host, rendered_dir)
    if isinstance(declared, Problem):
        return StageResult(
            "preconditions", False, declared.problem,
            resolve.retry_fix(declared.fix, "corpus watch"),
        ), None, {}
    if WORKER_SERVICE_NAME not in declared:
        return StageResult(
            "preconditions", False, "rendered stack has no worker",
            f"Run {report.command('apply')}, then run {report.command('corpus watch')} again.",
        ), None, {}
    if not sources:
        return StageResult(
            "preconditions", False, "the corpus source registry is empty",
            "Add a corpus source in the release, then run "
            f"{report.command('corpus watch')} again.",
        ), None, {}
    loaded = load_egress_allowlist(checkout / "config/egress.yaml", host=host)
    if loaded.errors:
        detail = "; ".join(error.problem for error in loaded.errors)
        fix = "; ".join(dict.fromkeys(error.fix for error in loaded.errors))
        return StageResult(
            "preconditions", False, f"egress allowlist: {detail}",
            resolve.retry_fix(fix, "corpus watch"),
        ), None, {}
    allowlist = loaded.allowlist
    assert allowlist is not None
    group = allowlist.group("corpus")
    allowed = {item.host for item in group.hosts} if group is not None else set()
    urls: dict[str, str] = {}
    for source in sources:
        if source.name in urls:
            return StageResult(
                "preconditions", False, "source names repeat in the registry",
                "Correct the source definitions in the release, then run "
                f"{report.command('corpus watch')} again.",
            ), None, {}
        requests = source.index_documents()
        if not requests:
            return StageResult(
                "preconditions", False, f"source {source.name} has no index documents",
                "Correct the source's index documents in the release, then run "
                f"{report.command('corpus watch')} again.",
            ), None, {}
        for request in requests:
            try:
                parsed = urlsplit(request.url)
                scheme, hostname, port = parsed.scheme, parsed.hostname, parsed.port
            except ValueError:
                scheme, hostname, port = "", None, None
            if scheme != "https" or hostname is None or port not in (None, 443):
                return StageResult(
                    "preconditions", False,
                    f"source {source.name} index {request.name} has an invalid HTTPS URL",
                    "Correct the source's index URL in the release, then run "
                    f"{report.command('corpus watch')} again.",
                ), None, {}
            if hostname not in allowed:
                return StageResult(
                    "preconditions", False,
                    f"source {source.name} index host {hostname} is outside the corpus allowlist",
                    f"Add {hostname} to the corpus group in config/egress.yaml, "
                    f"run {report.command('apply')}, then run {report.command('corpus watch')} again.",
                ), None, {}
        urls[source.name] = requests[0].url
    claim = backuplock.claim(
        host, command=report.command_name("corpus watch"), now=clock(),
        lock=backuplock.CORPUS_LOCK,
    )
    if claim.refusal is not None:
        return StageResult(
            "preconditions", False, claim.refusal.detail,
            resolve.retry_fix(claim.refusal.fix, "corpus watch"),
        ), None, {}
    if not claim.taken:
        return StageResult(
            "preconditions", False, "a corpus command is already running in this process",
            f"Wait for it to finish, then run {report.command('corpus watch')} again.",
        ), None, {}
    return StageResult(
        "preconditions", True, "root, corpus lock, and release artifacts are ready", ""
    ), claim, urls


def _observe(
    host: resolve.CorpusHost,
    rendered_dir: PathLike,
    snapshots_root: PathLike,
    sources: Sequence[SourceDefinition],
    urls: dict[str, str],
    observed_at: str,
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> tuple[tuple[record.Observation, ...], bool]:
    health = worker.worker_preconditions(host, rendered_dir, command_path="corpus watch")
    observations: list[record.Observation] = []
    all_ok = True
    for source in sources:
        if health.ok:
            result = resolve.resolve_source(
                host, rendered_dir, snapshots_root, source, command_path="corpus watch",
                sleep=sleep, monotonic=monotonic,
            )
            if isinstance(result, resolve.ResolveFailure):
                reason = result.reason
                issue = result.problem
            else:
                observations.append(record.Observation(
                    source.name, observed_at, "observed", result.snapshot.snapshot_date,
                    None, urls[source.name], None,
                ))
                report.print_stage(StageResult(
                    "observe", True, f"{source.name}: {result.snapshot.snapshot_date}", ""
                ))
                continue
        else:
            reason = "worker-unavailable"
            issue = Problem(health.detail, resolve.retry_fix(health.fix, "corpus watch"))
        observations.append(record.Observation(
            source.name, observed_at, "unanswered", None, None, urls[source.name], reason,
        ))
        report.print_stage(StageResult(
            "observe", False, f"{source.name}: unanswered ({reason}); {issue.problem}", issue.fix
        ))
        all_ok = False
    return tuple(observations), all_ok


def _state_fix(rendered_dir: PathLike) -> str:
    return record.database_fix(rendered_dir, "corpus watch")


def _new_open_sources(
    observations: Sequence[record.Observation],
    states: Sequence[record.WatchState],
) -> tuple[str, ...]:
    # The record's parser has held every timestamp to an offset-bearing ISO form.
    by_source = {item.source: item for item in observations}
    new: list[str] = []
    for state in states:
        answer = state.newest_answered
        current = by_source.get(state.newest.source)
        if (
            not state.open or answer is None or current is None
            or current.outcome != "observed"
            or current.latest_label != answer.latest_label
            or state.first_seen is None
        ):
            continue
        if datetime.fromisoformat(state.first_seen) == datetime.fromisoformat(current.observed_at):
            new.append(current.source)
    return tuple(new)


def run_corpus_watch(
    args: argparse.Namespace,
    *,
    host: resolve.CorpusHost | None = None,
    rendered_dir: PathLike = "/etc/gideon/rendered",
    checkout: PathLike | None = None,
    snapshots_root: PathLike = SNAPSHOTS_ROOT,
    clock: Callable[[], datetime] = _utc_now,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    sources: Sequence[SourceDefinition] = SOURCES,
) -> int:
    """Observe every source and record the answered and unanswered rows."""

    del args
    io = host if host is not None else RealHost()
    root = Path(checkout) if checkout is not None else Path(__file__).parents[3]
    active_stage = "preconditions"
    claim: backuplock.Claim | None = None
    recorded: str | None = None
    try:
        stage, claim, urls = _preconditions(io, rendered_dir, root, sources, clock)
        if not stage.ok:
            return _refuse("preconditions", stage.detail, stage.fix)
        report.print_stage(stage)
        active_stage = "observe"
        observed_at = clock().astimezone(UTC).isoformat()
        observations, all_ok = _observe(
            io, rendered_dir, snapshots_root, sources, urls, observed_at,
            sleep=sleep, monotonic=monotonic,
        )
        active_stage = "record"
        issue = record.write_observations(
            io, rendered_dir, observations, command_path="corpus watch"
        )
        if issue is not None:
            return _refuse("record", issue.problem, issue.fix)
        states = record.read_watch_state(io, rendered_dir, command_path="corpus watch")
        if isinstance(states, Problem):
            return _refuse("record", states.problem, states.fix)
        returned = {state.newest.source for state in states}
        missing = {item.source for item in observations} - returned
        if missing:
            return _refuse(
                "record", f"watch state is missing sources: {', '.join(sorted(missing))}",
                _state_fix(rendered_dir),
            )
        new_sources = _new_open_sources(observations, states)
        open_count = sum(state.open for state in states)
        recorded = (
            f"{len(observations)} observations written, {len(new_sources)} new notices, "
            f"{open_count} open notices"
        )
        report.print_stage(StageResult("record", True, recorded, ""))
        for source in new_sources:
            print(
                f"Next: a cut is due for {source}: run {report.command('corpus cut')} "
                "in a window you choose."
            )
        return 0 if all_ok else 1
    except KeyboardInterrupt:
        if recorded is not None:
            detail = f"interrupted; record completed: {recorded}"
            fix = f"Run {report.command('corpus watch')} again if another observation is needed."
        elif active_stage == "record":
            detail = "interrupted; the record outcome is unknown"
            fix = (
                f"Inspect the record, then run {report.command('corpus watch')} again "
                "if another observation is needed."
            )
        else:
            detail = "interrupted; nothing was recorded"
            fix = f"Run {report.command('corpus watch')} again."
        return _refuse(active_stage, detail, fix)
    finally:
        if claim is not None:
            backuplock.release_claim(io, claim, lock=backuplock.CORPUS_LOCK)
