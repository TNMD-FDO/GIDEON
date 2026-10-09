"""Fetch, verify, record, stage, ingest, measure, and retain a corpus lockfile."""

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from gideon.host import agreement, backuplock, caselaw, report, stack, staging
from gideon.host.corpus import artifacts, record, snapshots
from gideon.host.corpus.lockfile import (
    LABEL,
    Lockfile,
    check_courts,
    check_egress_hosts,
)
from gideon.host.corpus.sources import SOURCES, SourceDefinition, data_file
from gideon.host.courts import CourtMap
from gideon.host.render.worker import (
    AGREEMENT_TABLE,
    CITE_FORMS,
    CITE_TYPES,
    NO_STATE_REASONS,
    PATTERN_SET_ID,
    PRECEDENTIAL_VALUES,
    QUALIFIERS,
    RESOLVE_DIR,
    SECTION_TYPES,
    SNAPSHOTS_ROOT,
    STAGE_TABLES,
    TEXT_SOURCES,
    TREATMENT_SIGNALS,
    TREATMENT_STATES,
    WORK_ROOT,
    WORKER_SERVICE_NAME,
)
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
) -> tuple[StageResult, Lockfile | None, CourtMap | None, backuplock.Claim | None]:
    label = args.label
    if LABEL.fullmatch(label) is None:
        return StageResult(
            "preconditions", False, f"lockfile label {label!r} has the wrong form",
            f"Use a label in the form corpus-YYYY-MM-DD, then run {report.command(COMMAND_PATH)} again.",
        ), None, None, None
    if host.geteuid() != 0:
        return StageResult(
            "preconditions", False, "root privileges are required",
            f"Run {report.command(COMMAND_PATH)}, then retry.",
        ), None, None, None
    claim = backuplock.claim(
        host, command=report.command_name(COMMAND_PATH), now=clock(),
        lock=backuplock.CORPUS_LOCK,
    )
    if claim.refusal is not None:
        return StageResult(
            "preconditions", False, claim.refusal.detail, _retry_fix(claim.refusal.fix),
        ), None, None, None
    if not claim.taken:
        return StageResult(
            "preconditions", False, "a corpus command is already running in this process",
            f"Wait for it to finish, then run {report.command(COMMAND_PATH)} again.",
        ), None, None, None
    try:
        stage, lockfile, court_map = _artifact_preconditions(
            host, rendered_dir, checkout, registry, label,
        )
    except BaseException:
        backuplock.release_claim(host, claim, lock=backuplock.CORPUS_LOCK)
        raise
    return stage, lockfile, court_map, claim


def _artifact_preconditions(
    host: WritableBytesHost,
    rendered_dir: PathLike,
    checkout: Path,
    registry: Sequence[SourceDefinition],
    label: str,
) -> tuple[StageResult, Lockfile | None, CourtMap | None]:
    loaded, issue = artifacts.load_artifacts(
        host, rendered_dir, checkout, registry, command_path=COMMAND_PATH,
    )
    if issue is not None:
        return issue, None, None
    assert loaded is not None
    lockfile = next((item for item in loaded.directory.lockfiles if item.label == label), None)
    if lockfile is None:
        labels = ", ".join(item.label for item in loaded.directory.lockfiles) or "none"
        return StageResult(
            "preconditions", False, f"lockfile label {label} is not committed",
            f"Use a committed label from corpus/lockfiles/: {labels}; then run "
            f"{report.command(COMMAND_PATH)} again.",
        ), None, None
    issue = artifacts.artifact_problem(
        check_courts(lockfile, loaded.court_map), label, command_path=COMMAND_PATH,
    )
    if issue is not None:
        return issue, None, None
    issue = artifacts.artifact_problem(
        check_egress_hosts(lockfile, loaded.allowlist), label, command_path=COMMAND_PATH,
    )
    if issue is not None:
        return issue, None, None
    row = record.read_cut(host, rendered_dir, label, command_path=COMMAND_PATH)
    if isinstance(row, Problem):
        return StageResult("preconditions", False, row.problem, row.fix), None, None
    if row is not None and row.state == "superseded":
        newest = loaded.directory.newest
        assert newest is not None
        return StageResult(
            "preconditions", False, f"lockfile label {label} is superseded",
            f"Run {report.command(f'{COMMAND_PATH} {newest.label}')} instead.",
        ), None, None
    base_text = ""
    if lockfile.base is not None:
        base_row = record.read_cut(host, rendered_dir, lockfile.base, command_path=COMMAND_PATH)
        if isinstance(base_row, Problem):
            return StageResult("preconditions", False, base_row.problem, base_row.fix), None, None
        if base_row is None:
            # The record's base column is a foreign key, so a derived lockfile
            # follows its base onto a box, and the refusal comes before any fetch.
            return StageResult(
                "preconditions", False,
                f"base {lockfile.base} of lockfile {label} is not recorded on this box",
                f"Run {report.command(f'{COMMAND_PATH} {lockfile.base}')}, then run "
                f"{report.command(f'{COMMAND_PATH} {label}')} again.",
            ), None, None
        base_text = f"; base {lockfile.base} {base_row.state}"
    snapshots_text = ", ".join(
        f"{name} {pin.snapshot_date}" for name, pin in lockfile.sources.items()
    )
    state = row.state if row is not None else "not yet recorded"
    return StageResult(
        "preconditions", True, f"{label}: {snapshots_text}; {state}{base_text}", "",
    ), lockfile, loaded.court_map


def _run_install_stages(
    host: CorpusHost,
    rendered_dir: PathLike,
    snapshots_root: PathLike,
    work_root: PathLike,
    lockfile: Lockfile,
    court_map: CourtMap,
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
    active_stage[0] = "stage"
    staged = _stage(host, rendered_dir, work_root, lockfile, sleep, monotonic)
    if staged:
        return staged
    active_stage[0] = "ingest"
    ingested = _ingest(host, rendered_dir, work_root, lockfile, court_map, sleep, monotonic)
    if ingested:
        return ingested
    active_stage[0] = "agreement"
    measured = _agreement(host, rendered_dir, work_root, lockfile, sleep, monotonic)
    if measured:
        return measured
    active_stage[0] = "retain"
    return _retain(host, rendered_dir, snapshots_root)


def _stage(
    host: CorpusHost, rendered_dir: PathLike, work_root: PathLike,
    lockfile: Lockfile, sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> int:
    """Keep matching work, or defer and wait for each source carrying courts."""

    for name, pin in lockfile.sources.items():
        if pin.courts is None:
            continue
        snapshot = f"{name}-{pin.snapshot_date}"
        entries = {entry.path: entry for entry in pin.entries}
        inputs: dict[str, dict[str, str]] = {}
        for table in STAGE_TABLES:
            path = data_file(table, pin.snapshot_date)
            entry = entries.get(path)
            if entry is None:
                return _refuse(
                    "stage", f"{name}: pinned stage input {path} is missing",
                    f"Run {report.command('corpus cut')} to pin {path}, then run "
                    f"{report.command(COMMAND_PATH)} with the new label.",
                )
            inputs[table] = {"path": path, "sha256": entry.sha256}
        courts = list(pin.courts)
        existing = staging.read_record(
            host, lockfile.label, name, work_root=work_root, command_path=COMMAND_PATH,
        )
        if isinstance(existing, Problem):
            return _refuse("stage", f"{name}: {existing.problem}", existing.fix)
        if existing is not None:
            if not _stage_matches(existing, snapshot, courts, inputs):
                return _refuse("stage", f"{name}: stage record disagrees with the lockfile",
                               _stage_removal_fix(lockfile.label, name, work_root))
            _court_stage_rows(existing)
            report.print_stage(StageResult("stage", True, f"{name}: complete; nothing deferred", ""))
            continue
        job = staging.defer_stage(
            host, rendered_dir, label=lockfile.label, snapshot=snapshot,
            courts=courts, inputs=inputs, command_path=COMMAND_PATH,
        )
        if isinstance(job, Problem):
            return _refuse("stage", f"{name}: {job.problem}", job.fix)
        heartbeat_at = monotonic()
        started = heartbeat_at
        while True:
            outcome = staging.read_stage(
                host, rendered_dir, job, label=lockfile.label, snapshot=snapshot,
                work_root=work_root, command_path=COMMAND_PATH,
            )
            if isinstance(outcome, Problem):
                return _refuse("stage", f"{name}: {outcome.problem}", outcome.fix)
            if outcome.failure is not None:
                return _refuse("stage", f"{name}: {outcome.failure.problem}", outcome.failure.fix)
            if outcome.record is not None:
                if not _stage_matches(outcome.record, snapshot, courts, inputs):
                    return _refuse("stage", f"{name}: stage record disagrees with the lockfile",
                                   _stage_removal_fix(lockfile.label, name, work_root))
                _court_stage_rows(outcome.record)
                read_count = sum(outcome.record.records.values())
                report.print_stage(StageResult(
                    "stage", True,
                    f"{name}: {len(courts)} courts staged, {read_count} records read, "
                    f"{round(outcome.record.seconds)} seconds", "",
                ))
                break
            now = monotonic()
            if now - heartbeat_at >= snapshots.HEARTBEAT_SECONDS:
                report.print_stage(StageResult(
                    "stage", True, f"{name}: staging, {round(now - started)} seconds", "",
                ))
                heartbeat_at = now
            sleep(1.0)
    return 0


def _stage_matches(
    record: staging.StageRecord, snapshot: str, courts: list[str],
    inputs: dict[str, dict[str, str]],
) -> bool:
    return (
        record.snapshot == snapshot and record.courts == courts
        and all(
            record.inputs[table]["path"] == inputs[table]["path"]
            and record.inputs[table]["sha256"] == inputs[table]["sha256"]
            for table in STAGE_TABLES
        )
    )


def _stage_removal_fix(label: str, source: str, work_root: PathLike) -> str:
    directory = staging.work_directory(label, source, work_root=work_root)
    return f"Remove {directory}, then run {report.command(COMMAND_PATH)} again."


def _court_stage_rows(record: staging.StageRecord) -> None:
    for court in record.courts:
        counts = record.counts[court]
        report.print_stage(StageResult(
            "stage", True,
            f"{court}: dockets {counts['dockets']}, "
            f"opinion-clusters {counts['opinion-clusters']}, "
            f"citations {counts['citations']}, opinions {counts['opinions']}", "",
        ))


def _percent(part: int, whole: int) -> int:
    return round(100 * part / whole) if whole else 0


def _ingest(
    host: CorpusHost, rendered_dir: PathLike, work_root: PathLike,
    lockfile: Lockfile, court_map: CourtMap, sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> int:
    """Defer every court, wait for the jobs, and compare their recorded counts."""

    started = heartbeat_at = monotonic()
    courts: list[tuple[str, str, str, int]] = []
    pending: dict[int, tuple[str, str, str]] = {}
    for name, pin in lockfile.sources.items():
        if pin.courts is None:
            continue
        stage_record = staging.read_record(
            host, lockfile.label, name, work_root=work_root, command_path=COMMAND_PATH,
        )
        if isinstance(stage_record, Problem):
            return _refuse("ingest", f"{name}: {stage_record.problem}", stage_record.fix)
        if stage_record is None:
            return _refuse(
                "ingest", f"{name}: stage record is missing",
                _stage_removal_fix(lockfile.label, name, work_root),
            )
        if stage_record.courts != list(pin.courts):
            return _refuse("ingest", f"{name}: stage record disagrees with the lockfile",
                           _stage_removal_fix(lockfile.label, name, work_root))
        geography = caselaw.court_geography(court_map, pin.courts)
        if isinstance(geography, Problem):
            return _refuse("ingest", f"{name}: {geography.problem}", geography.fix)
        snapshot = f"{name}-{pin.snapshot_date}"
        for court in pin.courts:
            job = caselaw.defer_caselaw(
                host, rendered_dir, label=lockfile.label, snapshot=snapshot,
                court=court, courts=geography, command_path=COMMAND_PATH,
            )
            if isinstance(job, Problem):
                return _refuse("ingest", f"{court}: {job.problem}", job.fix)
            courts.append((name, pin.snapshot_date, court, stage_record.counts[court]["opinions"]))
            pending[job] = (name, pin.snapshot_date, court)

    while pending:
        for job, (source, snapshot_date, court) in tuple(pending.items()):
            outcome = caselaw.read_caselaw(
                host, rendered_dir, job, label=lockfile.label,
                snapshot=f"{source}-{snapshot_date}", court=court,
                work_root=work_root, command_path=COMMAND_PATH,
            )
            if isinstance(outcome, Problem):
                return _refuse("ingest", f"{court}: {outcome.problem}", outcome.fix)
            if outcome.failure is not None:
                return _refuse("ingest", f"{court}: {outcome.failure.problem}", outcome.failure.fix)
            if outcome.done:
                del pending[job]
        if not pending:
            break
        sleep(1.0)
        now = monotonic()
        if now - heartbeat_at >= snapshots.HEARTBEAT_SECONDS:
            report.print_stage(StageResult(
                "ingest", True,
                f"caselaw: ingesting, {len(courts)} courts, {round(now - started)} seconds", "",
            ))
            heartbeat_at = now

    total = ready = failed = 0
    for source, snapshot_date, court, expected in courts:
        counts = caselaw.read_counts(
            host, rendered_dir, source=source, snapshot_date=snapshot_date,
            court=court, command_path=COMMAND_PATH,
        )
        if isinstance(counts, Problem):
            return _refuse("ingest", f"{court}: {counts.problem}", counts.fix)
        detail = (
            f"{court}: {counts.opinions} opinions; ready {counts.by_status.get('ready', 0)}, "
            f"failed {counts.by_status.get('failed', 0)} ("
            + ", ".join(
                f"{reason} {counts.by_failure_reason.get(reason, 0)}"
                for reason in ("no-text", "unparseable", "empty", "interrupted")
            ) + "); "
            + ", ".join(
                f"{source_name} {counts.by_text_source.get(source_name, 0)}"
                for source_name in TEXT_SOURCES
            ) + "; "
            + ", ".join(
                f"{value} {counts.by_precedential.get(value, 0)}"
                for value in PRECEDENTIAL_VALUES
            )
        )
        report.print_stage(StageResult("ingest", True, detail, ""))
        if counts.opinions < expected:
            return _refuse(
                "ingest", f"{court}: {counts.opinions} opinions recorded; {expected} staged",
                f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, then run "
                f"{report.command(COMMAND_PATH)} again.",
            )
        section_counts = caselaw.read_section_counts(
            host, rendered_dir, source=source, snapshot_date=snapshot_date,
            court=court, command_path=COMMAND_PATH,
        )
        if isinstance(section_counts, Problem):
            return _refuse("ingest", f"{court}: {section_counts.problem}", section_counts.fix)
        if section_counts.sectioned < section_counts.ready:
            return _refuse(
                "ingest",
                f"{court}: {section_counts.sectioned} of {section_counts.ready} ready documents have sections",
                f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, then run "
                f"{report.command(COMMAND_PATH)} again.",
            )
        total_chars = sum(section_counts.chars_by_type.values())
        section_detail = ", ".join(
            f"{section_type} "
            f"{_percent(section_counts.chars_by_type.get(section_type, 0), total_chars)} % "
            f"({section_counts.sections_by_type.get(section_type, 0)})"
            for section_type in SECTION_TYPES
        )
        report.print_stage(StageResult(
            "ingest", True,
            f"{court} sections: {section_counts.sectioned} of {section_counts.ready} "
            f"ready documents; {section_detail}", "",
        ))
        anchor_counts = caselaw.read_anchor_counts(
            host, rendered_dir, source=source, snapshot_date=snapshot_date,
            court=court, command_path=COMMAND_PATH,
        )
        if isinstance(anchor_counts, Problem):
            return _refuse("ingest", f"{court}: {anchor_counts.problem}", anchor_counts.fix)
        if anchor_counts.anchored < anchor_counts.ready:
            return _refuse(
                "ingest",
                f"{court}: {anchor_counts.anchored} of {anchor_counts.ready} ready documents anchored",
                f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, then run "
                f"{report.command(COMMAND_PATH)} again.",
            )
        anchor_detail = "; ".join(
            f"{source_name} {coverage.documents} documents, "
            f"{_percent(coverage.with_anchors, coverage.documents)} % with a page, "
            f"{_percent(coverage.anchored_chars, coverage.chars)} % of characters"
            for source_name in TEXT_SOURCES
            if (coverage := anchor_counts.by_text_source.get(source_name)) is not None
        )
        report.print_stage(StageResult(
            "ingest", True,
            f"{court} anchors: {anchor_counts.anchored} of {anchor_counts.ready} "
            f"ready documents anchored" + (f"; {anchor_detail}" if anchor_detail else ""), "",
        ))
        citation_counts = caselaw.read_citation_counts(
            host, rendered_dir, source=source, snapshot_date=snapshot_date,
            court=court, command_path=COMMAND_PATH,
        )
        if isinstance(citation_counts, Problem):
            return _refuse("ingest", f"{court}: {citation_counts.problem}", citation_counts.fix)
        if citation_counts.cited < citation_counts.ready:
            return _refuse(
                "ingest",
                f"{court}: {citation_counts.cited} of {citation_counts.ready} ready documents have citations parsed",
                f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, then run "
                f"{report.command(COMMAND_PATH)} again.",
            )
        edge_rate = citation_counts.edges / citation_counts.ready if citation_counts.ready else 0
        resolved_share = round(100 * citation_counts.case_resolved / citation_counts.case_rows) if citation_counts.case_rows else 0
        case_forms = ", ".join(
            f"{form} {citation_counts.by_form.get(form, 0)}" for form in CITE_FORMS
        )
        other_types = ", ".join(
            f"{kind} {citation_counts.by_type.get(kind, 0)}"
            for kind in CITE_TYPES if kind != "case_cite"
        )
        report.print_stage(StageResult(
            "ingest", True,
            f"{court} citations: {citation_counts.cited} of {citation_counts.ready} "
            f"ready documents; {citation_counts.edges} edges, {edge_rate:.1f} per document; "
            f"case_cite {citation_counts.case_rows} ({resolved_share} % resolved; {case_forms}); "
            f"{other_types}", "",
        ))
        treatment_counts = caselaw.read_treatment_counts(
            host, rendered_dir, source=source, snapshot_date=snapshot_date,
            court=court, command_path=COMMAND_PATH,
        )
        if isinstance(treatment_counts, Problem):
            return _refuse("ingest", f"{court}: {treatment_counts.problem}", treatment_counts.fix)
        if treatment_counts.treated < treatment_counts.ready:
            return _refuse(
                "ingest",
                f"{court}: {treatment_counts.treated} of {treatment_counts.ready} ready documents treated under {PATTERN_SET_ID}",
                f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, then run "
                f"{report.command(COMMAND_PATH)} again.",
            )
        treatment_detail = "; ".join((
            ", ".join(
                f"{signal} {treatment_counts.by_signal.get(signal, 0)}"
                for signal in TREATMENT_SIGNALS
            ),
            ", ".join(
                f"{outcome} {treatment_counts.by_outcome.get(outcome, 0)}"
                for outcome in (*TREATMENT_STATES, *NO_STATE_REASONS)
            ),
            ", ".join(
                f"{qualifier} {treatment_counts.by_qualifier.get(qualifier, 0)}"
                for qualifier in QUALIFIERS
            ),
        ))
        report.print_stage(StageResult(
            "ingest", True,
            f"{court} treatment: {treatment_counts.treated} of {treatment_counts.ready} "
            f"ready documents under {PATTERN_SET_ID}; "
            f"{treatment_counts.signalled} signalled edges; {treatment_detail}", "",
        ))
        total += counts.opinions
        ready += counts.by_status.get("ready", 0)
        failed += counts.by_status.get("failed", 0)
    report.print_stage(StageResult(
        "ingest", True,
        f"caselaw: {len(courts)} courts, {total} opinions, {ready} ready, "
        f"{failed} failed, {round(monotonic() - started)} seconds", "",
    ))
    return 0


def _agreement(
    host: CorpusHost, rendered_dir: PathLike, work_root: PathLike,
    lockfile: Lockfile, sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> int:
    """Defer all court comparisons, wait once, and report their figures."""

    started = heartbeat_at = monotonic()
    courts: list[tuple[str, int]] = []
    pending: dict[int, tuple[str, str]] = {}
    records: dict[int, agreement.AgreementRecord] = {}
    for name, pin in lockfile.sources.items():
        if pin.courts is None:
            continue
        stage_record = staging.read_record(
            host, lockfile.label, name, work_root=work_root, command_path=COMMAND_PATH,
        )
        if isinstance(stage_record, Problem):
            return _refuse("agreement", f"{name}: {stage_record.problem}", stage_record.fix)
        if stage_record is None:
            return _refuse(
                "agreement", f"{name}: stage record is missing",
                _stage_removal_fix(lockfile.label, name, work_root),
            )
        if stage_record.courts != list(pin.courts):
            return _refuse(
                "agreement", f"{name}: stage record disagrees with the lockfile",
                _stage_removal_fix(lockfile.label, name, work_root),
            )
        path = data_file(AGREEMENT_TABLE, pin.snapshot_date)
        entry = next((item for item in pin.entries if item.path == path), None)
        if entry is None:
            return _refuse(
                "agreement", f"{name}: pinned agreement input {path} is missing",
                f"Run {report.command('corpus cut')} to pin {path}, then run "
                f"{report.command(COMMAND_PATH)} with the new label.",
            )
        snapshot = f"{name}-{pin.snapshot_date}"
        for court in pin.courts:
            job = agreement.defer_agreement(
                host, rendered_dir, label=lockfile.label, snapshot=snapshot,
                court=court, input={"path": path, "sha256": entry.sha256},
                command_path=COMMAND_PATH,
            )
            if isinstance(job, Problem):
                return _refuse("agreement", f"{court}: {job.problem}", job.fix)
            courts.append((court, job))
            pending[job] = (snapshot, court)

    while pending:
        for job, (snapshot, court) in tuple(pending.items()):
            outcome = agreement.read_agreement(
                host, rendered_dir, job, label=lockfile.label, snapshot=snapshot,
                court=court, work_root=work_root, command_path=COMMAND_PATH,
            )
            if isinstance(outcome, Problem):
                return _refuse("agreement", f"{court}: {outcome.problem}", outcome.fix)
            if outcome.failure is not None:
                return _refuse("agreement", f"{court}: {outcome.failure.problem}", outcome.failure.fix)
            if outcome.done:
                assert outcome.record is not None
                records[job] = outcome.record
                del pending[job]
        if not pending:
            break
        sleep(1.0)
        now = monotonic()
        if now - heartbeat_at >= snapshots.HEARTBEAT_SECONDS:
            report.print_stage(StageResult(
                "agreement", True,
                f"caselaw: measuring, {len(courts)} courts, {round(now - started)} seconds", "",
            ))
            heartbeat_at = now

    for court, job in courts:
        figure = records[job]
        report.print_stage(StageResult(
            "agreement", True,
            f"{court} agreement: {figure.documents} documents; "
            f"{figure.gideon_pairs} GIDEON pairs, {figure.map_pairs} map pairs, "
            f"{figure.map_outside} map rows beyond the label; agreed {figure.agreed} "
            f"({figure.gideon_share()} % of GIDEON's, {figure.map_share()} % of the map's); "
            f"map-only {figure.map_only_seen} seen unresolved, {figure.map_only_missed} missed", "",
        ))
    report.print_stage(StageResult(
        "agreement", True,
        f"caselaw: {len(courts)} courts measured, {round(monotonic() - started)} seconds", "",
    ))
    return 0


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
    work_root: PathLike = WORK_ROOT,
    clock: Callable[[], datetime] = _utc_now,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    sources: Sequence[SourceDefinition] = SOURCES,
) -> int:
    """Run preconditions, fetch, verify, record, stage, ingest, agreement, and retain."""
    io = host if host is not None else RealHost()
    root = Path(checkout) if checkout is not None else Path(__file__).parents[3]
    active_stage = ["preconditions"]
    lock_claim: backuplock.Claim | None = None
    try:
        stage, lockfile, court_map, lock_claim = _preconditions(
            args, io, rendered_dir, root, sources, clock,
        )
        if not stage.ok:
            return _refuse("preconditions", stage.detail, stage.fix)
        assert lockfile is not None and court_map is not None
        report.print_stage(stage)
        return _run_install_stages(
            io, rendered_dir, snapshots_root, work_root, lockfile, court_map,
            clock, sleep, monotonic, active_stage,
        )
    except KeyboardInterrupt:
        if active_stage[0] == "fetch":
            return _refuse(
                "fetch", "interrupted; downloads continue in the worker",
                f"Run {report.command(COMMAND_PATH)} again to rejoin them.",
            )
        if active_stage[0] == "stage":
            return _refuse(
                "stage", "interrupted; the stage continues in the worker",
                f"Run {report.command(COMMAND_PATH)} again to rejoin it.",
            )
        if active_stage[0] == "ingest":
            return _refuse(
                "ingest", "interrupted; the ingest continues in the worker",
                f"Run {report.command(COMMAND_PATH)} again to rejoin it.",
            )
        if active_stage[0] == "agreement":
            return _refuse(
                "agreement", "interrupted; agreement jobs continue in the worker",
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
