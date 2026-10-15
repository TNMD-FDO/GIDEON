"""Install corpus lockfiles through worker jobs and changed collection builds.

Fetch, verify, record, stage, ingest, overruled, and agreement lead to collections,
installed, clean, and retain.
"""

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from gideon.host import (
    agreement,
    backuplock,
    caselaw,
    fetch,
    models,
    overruled,
    report,
    stack,
    staging,
)
from gideon.host.corpus import artifacts, record, snapshots
from gideon.host.corpus.lockfile import (
    LABEL,
    Lockfile,
    check_courts,
    check_egress_hosts,
)
from gideon.host.corpus.sources import SOURCES, SourceDefinition, data_file
from gideon.host.courts import CourtMap
from gideon.host.index import build, buildreport, dense, generations
from gideon.host.index import record as indexrecord
from gideon.host.render.worker import (
    AGREEMENT_TABLE,
    CITE_FORMS,
    CITE_TYPES,
    NO_STATE_REASONS,
    OVERRULED_COURT,
    OVERRULED_SOURCE,
    PATTERN_SET_ID,
    PIPELINE_CHUNK_SIZES,
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
    site_path: PathLike,
) -> tuple[StageResult, Lockfile | None, CourtMap | None, tuple[dense.DenseInputs, ...], backuplock.Claim | None]:
    """Claim the corpus lock and validate the lockfile and generation build inputs."""

    label = args.label
    if LABEL.fullmatch(label) is None:
        return StageResult(
            "preconditions", False, f"lockfile label {label!r} has the wrong form",
            f"Use a label in the form corpus-YYYY-MM-DD, then run {report.command(COMMAND_PATH)} again.",
        ), None, None, (), None
    if host.geteuid() != 0:
        return StageResult(
            "preconditions", False, "root privileges are required",
            f"Run {report.command(COMMAND_PATH)}, then retry.",
        ), None, None, (), None
    claim = backuplock.claim(
        host, command=report.command_name(COMMAND_PATH), now=clock(),
        lock=backuplock.CORPUS_LOCK,
    )
    if claim.refusal is not None:
        return StageResult(
            "preconditions", False, claim.refusal.detail, _retry_fix(claim.refusal.fix),
        ), None, None, (), None
    if not claim.taken:
        return StageResult(
            "preconditions", False, "a corpus command is already running in this process",
            f"Wait for it to finish, then run {report.command(COMMAND_PATH)} again.",
        ), None, None, (), None
    try:
        stage, lockfile, court_map, inputs_by_collection = _artifact_preconditions(
            host, rendered_dir, checkout, registry, label, site_path,
        )
    except BaseException:
        backuplock.release_claim(host, claim, lock=backuplock.CORPUS_LOCK)
        raise
    return stage, lockfile, court_map, inputs_by_collection, claim


def _build_refusal(problem: Problem) -> StageResult:
    """Refuse a shared build check, its fix naming this command once."""

    # The profile's refusal names no command; the other checks already name it.
    fix = problem.fix if report.command(COMMAND_PATH) in problem.fix else _retry_fix(problem.fix)
    return StageResult("preconditions", False, problem.problem, fix)


def _artifact_preconditions(
    host: CorpusHost,
    rendered_dir: PathLike,
    checkout: Path,
    registry: Sequence[SourceDefinition],
    label: str,
    site_path: PathLike,
) -> tuple[StageResult, Lockfile | None, CourtMap | None, tuple[dense.DenseInputs, ...]]:
    """Read the committed artifacts, corpus record, services, and embedding target."""

    loaded, issue = artifacts.load_artifacts(
        host, rendered_dir, checkout, registry, command_path=COMMAND_PATH,
    )
    if issue is not None:
        return issue, None, None, ()
    assert loaded is not None
    lockfile = next((item for item in loaded.directory.lockfiles if item.label == label), None)
    if lockfile is None:
        labels = ", ".join(item.label for item in loaded.directory.lockfiles) or "none"
        return StageResult(
            "preconditions", False, f"lockfile label {label} is not committed",
            f"Use a committed label from corpus/lockfiles/: {labels}; then run "
            f"{report.command(COMMAND_PATH)} again.",
        ), None, None, ()
    if lockfile.pipeline not in PIPELINE_CHUNK_SIZES:
        versions = ", ".join(sorted(PIPELINE_CHUNK_SIZES))
        return StageResult(
            "preconditions", False,
            f"lockfile {label} pipeline {lockfile.pipeline} is not registered",
            f"Use a registered pipeline ({versions}) from a new "
            f"{report.command('corpus cut')}, then run {report.command(COMMAND_PATH)} "
            "with its new label.",
        ), None, None, ()
    issue = artifacts.artifact_problem(
        check_courts(lockfile, loaded.court_map), label, command_path=COMMAND_PATH,
    )
    if issue is not None:
        return issue, None, None, ()
    issue = artifacts.artifact_problem(
        check_egress_hosts(lockfile, loaded.allowlist), label, command_path=COMMAND_PATH,
    )
    if issue is not None:
        return issue, None, None, ()
    row = record.read_cut(host, rendered_dir, label, command_path=COMMAND_PATH)
    if isinstance(row, Problem):
        return StageResult("preconditions", False, row.problem, row.fix), None, None, ()
    if row is not None and row.state == "superseded":
        newest = loaded.directory.newest
        assert newest is not None
        return StageResult(
            "preconditions", False, f"lockfile label {label} is superseded",
            f"Run {report.command(f'{COMMAND_PATH} {newest.label}')} instead.",
        ), None, None, ()
    base_text = ""
    if lockfile.base is not None:
        base_row = record.read_cut(host, rendered_dir, lockfile.base, command_path=COMMAND_PATH)
        if isinstance(base_row, Problem):
            return StageResult("preconditions", False, base_row.problem, base_row.fix), None, None, ()
        if base_row is None:
            # The record's base column is a foreign key, so a derived lockfile
            # follows its base onto a box, and the refusal comes before any fetch.
            return StageResult(
                "preconditions", False,
                f"base {lockfile.base} of lockfile {label} is not recorded on this box",
                f"Run {report.command(f'{COMMAND_PATH} {lockfile.base}')}, then run "
                f"{report.command(f'{COMMAND_PATH} {label}')} again.",
            ), None, None, ()
        base_text = f"; base {lockfile.base} {base_row.state}"
    problem = build.check_services(host, rendered_dir, COMMAND_PATH)
    if problem is not None:
        return _build_refusal(problem), None, None, ()
    target = build.embed_target(host, checkout, site_path, COMMAND_PATH)
    if isinstance(target, Problem):
        return _build_refusal(target), None, None, ()
    inputs_by_collection: list[dense.DenseInputs] = []
    for collection in generations.COLLECTIONS:
        inputs = build.collection_inputs(loaded, lockfile, collection, registry, target, COMMAND_PATH)
        if isinstance(inputs, Problem):
            return _build_refusal(inputs), None, None, ()
        inputs_by_collection.append(inputs)
    snapshots_text = ", ".join(
        f"{name} {pin.snapshot_date}" for name, pin in lockfile.sources.items()
    )
    state = row.state if row is not None else "not yet recorded"
    return StageResult(
        "preconditions", True,
        f"{label}: {snapshots_text}; {state}{base_text}; embedding space {target.space_id}", "",
    ), lockfile, loaded.court_map, tuple(inputs_by_collection)


def _run_install_stages(
    host: CorpusHost,
    rendered_dir: PathLike,
    checkout: Path,
    snapshots_root: PathLike,
    work_root: PathLike,
    lockfile: Lockfile,
    court_map: CourtMap,
    inputs_by_collection: tuple[dense.DenseInputs, ...],
    chunk_guards: models.ChunkGuards,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    active_stage: list[str],
) -> int:
    """Run the worker stages, changed generation builds, installed move, and cleanup."""

    active_stage[0] = "fetch"
    fetched: dict[str, snapshots.FetchedSource] = {}
    for name, pin in lockfile.sources.items():
        if pin.captured:
            entry = pin.entries[0]
            companion = checkout / "corpus/lockfiles" / lockfile.label / f"{name}.{entry.path}"
            try:
                data = host.read_bytes(companion)
            except OSError as exc:
                return _refuse(
                    "fetch", f"{name}: committed copy {companion} is unreadable ({type(exc).__name__})",
                    _retry_fix("Restore the lockfile and its companion files from the release checkout."),
                )
            target = Path(snapshots_root) / fetch.snapshot_destination(name, pin.snapshot_date, entry.path)
            result = snapshots.seed_source(
                host, snapshots_root, name, pin.snapshot_date, entry.path, data,
                url=pin.base_url, now=clock(), command_path=COMMAND_PATH,
                other_bytes_fix=snapshots.refetch_fix(target, command_path=COMMAND_PATH),
            )
        else:
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
    installed = row.state == "installed"
    for name, run in (
        ("stage", lambda: _stage(host, rendered_dir, work_root, lockfile, sleep, monotonic)),
        ("ingest", lambda: _ingest(host, rendered_dir, work_root, lockfile, court_map, sleep, monotonic)),
        ("overruled", lambda: _overruled(host, rendered_dir, work_root, lockfile, sleep, monotonic)),
        ("agreement", lambda: _agreement(host, rendered_dir, work_root, lockfile, sleep, monotonic)),
    ):
        active_stage[0] = name
        if installed:
            report.print_stage(StageResult(name, True, f"{lockfile.label} is installed; nothing to do", ""))
        else:
            code = run()
            if code:
                return code
    active_stage[0] = "collections"
    names = _collections(
        host, rendered_dir, work_root, lockfile, inputs_by_collection, chunk_guards, sleep,
        monotonic, clock, active_stage,
    )
    if isinstance(names, int):
        return names
    active_stage[0] = "installed"
    rows = _installed(host, rendered_dir, lockfile, names, clock, installed=installed)
    if isinstance(rows, int):
        return rows
    active_stage[0] = "clean"
    code = _clean(host, work_root, lockfile.label)
    if code:
        return code
    active_stage[0] = "retain"
    code = _retain(host, rendered_dir, snapshots_root)
    if code:
        return code
    for generation in rows:
        if generation.state == "built":
            print(f"Next: run {report.command(f'index promote {generation.gen_id}')} to serve it.")
    return 0


def _collections(
    host: CorpusHost, rendered_dir: PathLike, work_root: PathLike, lockfile: Lockfile,
    inputs_by_collection: tuple[dense.DenseInputs, ...], chunk_guards: models.ChunkGuards,
    sleep: Callable[[float], None], monotonic: Callable[[], float],
    clock: Callable[[], datetime], active_stage: list[str],
) -> tuple[str, ...] | int:
    """Report changed inputs, record each generation, and build or finish the changed ones."""

    verdicts: list[tuple[dense.DenseInputs, bool, bool]] = []
    for inputs in inputs_by_collection:
        row = indexrecord.read_generation_by_inputs(
            host, rendered_dir, inputs.collection, inputs.inputs_hash, command_path=COMMAND_PATH,
        )
        if isinstance(row, Problem):
            return _refuse("collections", row.problem, row.fix)
        if row is not None and row.state == "retired":
            return _refuse(
                "collections", f"{row.gen_id}: generation is retired",
                "A retired generation is not rebuilt in place: make a new lockfile with "
                f"{report.command('corpus cut')} and build it with {report.command(COMMAND_PATH)}, "
                f"or remove the retired generation with {report.command('index gc')} once its hold ends.",
            )
        needs_build = row is None or row.state in {"building", "failed"}
        # A built generation's stages after built leave no mark but the report row they end in;
        # a report written before the treatment stage existed carries no treatment object.
        unfinished = False
        if row is not None and row.state == "built":
            newest = buildreport.read_report(host, rendered_dir, row.gen_id, command_path=COMMAND_PATH)
            if isinstance(newest, Problem):
                return _refuse("collections", newest.problem, newest.fix)
            unfinished = newest is None or newest.failed_stage is not None or newest.treatment is None
        verdict = "needs build" if needs_build else "unchanged"
        generation = inputs.name if row is None else f"{row.gen_id} {row.state}"
        if row is not None and row.failure_reason is not None:
            generation += f": {row.failure_reason}"
        if unfinished:
            generation += ", its treatment and report to finish"
        report.print_stage(StageResult(
            "collections", True,
            f"{inputs.collection}: {verdict}; generation {generation}, inputs hash {inputs.inputs_hash}", "",
        ))
        verdicts.append((inputs, needs_build, unfinished))
    to_finish = sum(unfinished for _, _, unfinished in verdicts)
    report.print_stage(StageResult(
        "collections", True,
        f"{sum(needs_build for _, needs_build, _ in verdicts)} of {len(verdicts)} need a build"
        + (f", {to_finish} to finish" if to_finish else ""), "",
    ))
    names: list[str] = []
    for inputs, needs_build, unfinished in verdicts:
        active_stage[0] = "generation"
        started_at = monotonic()
        outcome = indexrecord.write_building(host, rendered_dir, inputs, command_path=COMMAND_PATH)
        if isinstance(outcome, Problem):
            return _refuse("generation", outcome.problem, outcome.fix)
        report.print_stage(StageResult(
            "generation", True, f"{outcome.name}: {outcome.state}, {outcome.outcome}", "",
        ))
        names.append(outcome.name)
        if needs_build:
            result = build.build_generation(
                host, rendered_dir, replace(inputs, name=outcome.name), COMMAND_PATH,
                created=outcome.outcome == "created", started_at=started_at, sleep=sleep,
                monotonic=monotonic, clock=clock, active_stage=active_stage,
                treatment_snapshot=build.treatment_snapshot(lockfile), chunk_guards=chunk_guards,
                work_root=work_root,
            )
            if result:
                return result
        elif unfinished:
            result = build.finish_generation(
                host, rendered_dir, replace(inputs, name=outcome.name), COMMAND_PATH,
                started_at=started_at, monotonic=monotonic, active_stage=active_stage,
                treatment_snapshot=build.treatment_snapshot(lockfile), work_root=work_root,
            )
            if result:
                return result
    return tuple(names)


def _installed(
    host: CorpusHost, rendered_dir: PathLike, lockfile: Lockfile,
    names: tuple[str, ...], clock: Callable[[], datetime], *, installed: bool = False,
) -> tuple[indexrecord.GenerationRow, ...] | int:
    """Require finished generations, then record the installed label and superseded rows."""

    rows: list[indexrecord.GenerationRow] = []
    for name in names:
        row = indexrecord.read_generation(host, rendered_dir, name, command_path=COMMAND_PATH)
        if isinstance(row, Problem):
            return _refuse("installed", row.problem, row.fix)
        if row is None or row.state not in {"built", "live"}:
            state = "missing" if row is None else row.state
            return _refuse(
                "installed", f"generation {name} is {state}",
                f"Run {report.command(COMMAND_PATH)} again.",
            )
        rows.append(row)
    installed_at = clock()
    if installed_at.tzinfo is None:
        return _refuse("installed", "install clock has no timezone",
                       f"Use a UTC clock, then run {report.command(COMMAND_PATH)} again.")
    outcome = record.write_installed(
        host, rendered_dir, lockfile.label, installed_at.astimezone(UTC).isoformat(),
        command_path=COMMAND_PATH,
    )
    if isinstance(outcome, Problem):
        return _refuse("installed", outcome.problem, outcome.fix)
    detail = "installed (kept)" if installed else f"installed at {outcome.installed_at}"
    superseded = f"superseded {', '.join(outcome.superseded)}" if outcome.superseded else "none superseded"
    report.print_stage(StageResult("installed", True, f"{outcome.label}: {detail}; {superseded}", ""))
    return tuple(rows)


def _clean(host: CorpusHost, work_root: PathLike, label: str) -> int:
    """Remove a label's whole work directory, including an interrupted removal."""

    directory = Path(work_root) / label
    try:
        names = host.listdir(work_root)
    except OSError as exc:
        return _refuse(
            "clean", f"work root {work_root} could not be listed ({type(exc).__name__})",
            _retry_fix(f"Restore access to {work_root}."),
        )
    if label not in names:
        report.print_stage(StageResult("clean", True, f"{directory} is absent; nothing removed", ""))
        return 0
    try:
        host.rmtree(directory)
    except OSError as exc:
        return _refuse(
            "clean", f"could not remove {directory} ({type(exc).__name__})",
            _retry_fix(f"Restore access to {directory}."),
        )
    report.print_stage(StageResult("clean", True, f"removed {directory}", ""))
    return 0


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


def _token_figure(value: int | float | None) -> str:
    return "-" if value is None else f"{value:g}"


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
                court=court, courts=geography, pipeline=lockfile.pipeline,
                command_path=COMMAND_PATH,
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
        chunk_counts = caselaw.read_chunk_counts(
            host, rendered_dir, source=source, snapshot_date=snapshot_date,
            court=court, pipeline=lockfile.pipeline, command_path=COMMAND_PATH,
        )
        if isinstance(chunk_counts, Problem):
            return _refuse("ingest", f"{court}: {chunk_counts.problem}", chunk_counts.fix)
        if chunk_counts.chunked < chunk_counts.ready:
            return _refuse(
                "ingest",
                f"{court}: {chunk_counts.chunked} of {chunk_counts.ready} ready documents have chunks under {lockfile.pipeline}",
                f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, then run "
                f"{report.command(COMMAND_PATH)} again.",
            )
        if chunk_counts.over_maximum:
            return _refuse(
                "ingest",
                f"{court}: {chunk_counts.over_maximum} chunks exceed pipeline {lockfile.pipeline}'s maximum",
                f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}, then run "
                f"{report.command(COMMAND_PATH)} again after the fix.",
            )
        target, maximum, _minimum = PIPELINE_CHUNK_SIZES[lockfile.pipeline]
        per_document = chunk_counts.chunks / chunk_counts.ready if chunk_counts.ready else 0
        chunk_detail = ", ".join(
            f"{section_type} {chunk_counts.by_section_type.get(section_type, 0)}"
            for section_type in SECTION_TYPES
        )
        report.print_stage(StageResult(
            "ingest", True,
            f"{court} chunks: {chunk_counts.chunked} of {chunk_counts.ready} ready documents "
            f"under {lockfile.pipeline}; {chunk_counts.chunks} chunks, "
            f"{per_document:.1f} per document; tokens minimum "
            f"{_token_figure(chunk_counts.token_minimum)}, median "
            f"{_token_figure(chunk_counts.token_median)}, p95 "
            f"{_token_figure(chunk_counts.token_p95)}, maximum "
            f"{_token_figure(chunk_counts.token_maximum)} "
            f"(target {target}, maximum {maximum}); "
            f"over maximum {chunk_counts.over_maximum}, "
            f"under minimum {chunk_counts.under_minimum}; {chunk_detail}", "",
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


def _overruled(
    host: CorpusHost, rendered_dir: PathLike, work_root: PathLike,
    lockfile: Lockfile, sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> int:
    """Replay the captured list job and report its recorded resolution counts."""

    pin = lockfile.sources.get(OVERRULED_SOURCE)
    if pin is None:
        return 0
    caselaw_pin = next(((name, item) for name, item in lockfile.sources.items() if item.courts is not None), None)
    if caselaw_pin is None or OVERRULED_COURT not in (caselaw_pin[1].courts or ()):
        return _refuse(
            "overruled", "the known-overruled list is Supreme Court only and "
            f"{OVERRULED_COURT} is not among the lockfile's courts",
            f"Run {report.command(f'corpus cut --base {lockfile.label} --add-courts {OVERRULED_COURT}')}, "
            f"then run {report.command(COMMAND_PATH)} with the new label.",
        )
    name, case_pin = caselaw_pin
    caselaw_snapshot = f"{name}-{case_pin.snapshot_date}"
    stage_record = staging.read_record(
        host, lockfile.label, name, work_root=work_root, command_path=COMMAND_PATH,
    )
    if isinstance(stage_record, Problem):
        return _refuse("overruled", f"{name}: {stage_record.problem}", stage_record.fix)
    if stage_record is None:
        return _refuse(
            "overruled", f"{name}: stage record is missing",
            _stage_removal_fix(lockfile.label, name, work_root),
        )
    if stage_record.courts != list(case_pin.courts or ()) or stage_record.snapshot != caselaw_snapshot:
        return _refuse(
            "overruled", f"{name}: stage record disagrees with the lockfile",
            _stage_removal_fix(lockfile.label, name, work_root),
        )
    started = heartbeat_at = monotonic()
    snapshot = f"{OVERRULED_SOURCE}-{pin.snapshot_date}"
    entry = pin.entries[0]
    job = overruled.defer_overruled(
        host, rendered_dir, label=lockfile.label, snapshot=snapshot,
        caselaw_snapshot=caselaw_snapshot, court=OVERRULED_COURT,
        input={"path": entry.path, "sha256": entry.sha256}, command_path=COMMAND_PATH,
    )
    if isinstance(job, Problem):
        return _refuse("overruled", f"{OVERRULED_SOURCE}: {job.problem}", job.fix)
    while True:
        outcome = overruled.read_overruled(
            host, rendered_dir, job, label=lockfile.label, snapshot=snapshot,
            work_root=work_root, command_path=COMMAND_PATH,
        )
        if isinstance(outcome, Problem):
            return _refuse("overruled", f"{OVERRULED_SOURCE}: {outcome.problem}", outcome.fix)
        if outcome.failure is not None:
            return _refuse("overruled", f"{OVERRULED_SOURCE}: {outcome.failure.problem}", outcome.failure.fix)
        if outcome.done:
            break
        sleep(1.0)
        now = monotonic()
        if now - heartbeat_at >= snapshots.HEARTBEAT_SECONDS:
            report.print_stage(StageResult(
                "overruled", True, f"{OVERRULED_SOURCE}: resolving, {round(now - started)} seconds", "",
            ))
            heartbeat_at = now
    counts = overruled.read_overruled_counts(
        host, rendered_dir, source=OVERRULED_SOURCE, snapshot_date=pin.snapshot_date,
        caselaw_source=name, caselaw_snapshot_date=case_pin.snapshot_date,
        court=OVERRULED_COURT, command_path=COMMAND_PATH,
    )
    if isinstance(counts, Problem):
        return _refuse("overruled", f"{OVERRULED_SOURCE}: {counts.problem}", counts.fix)
    if counts.rows == 0:
        return _refuse(
            "overruled", f"{OVERRULED_SOURCE}: job succeeded but the record holds zero rows",
            _retry_fix(f"Run {stack.logs_fix(rendered_dir, WORKER_SERVICE_NAME)}."),
        )
    ruled, ruling = counts.overruled, counts.overruling
    cite, named = ruled.get("cite", 0), ruled.get("name", 0)
    ambiguous, absent = ruled.get("ambiguous", 0), ruled.get("absent", 0)
    ruling_cite, docket = ruling.get("cite", 0), ruling.get("docket", 0)
    unresolved = ruling.get("ambiguous", 0) + ruling.get("absent", 0)
    report.print_stage(StageResult(
        "overruled", True,
        f"{OVERRULED_SOURCE}: {counts.list_rows} table rows, {counts.rows} listed decisions; "
        f"overruled resolved {cite + named} (cite {cite}, name {named}), "
        f"unresolved {ambiguous + absent} (ambiguous {ambiguous}, absent {absent}); "
        f"overruling resolved {ruling_cite + docket} (cite {ruling_cite}, docket {docket}), "
        f"unresolved {unresolved}", "",
    ))
    report.print_stage(StageResult(
        "overruled", True,
        f"{OVERRULED_SOURCE}: {counts.rows} rows over {counts.clusters} clusters, "
        f"{round(monotonic() - started)} seconds", "",
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
    site_path: PathLike = "/etc/gideon/site.yaml",
    snapshots_root: PathLike = SNAPSHOTS_ROOT,
    work_root: PathLike = WORK_ROOT,
    clock: Callable[[], datetime] = _utc_now,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    sources: Sequence[SourceDefinition] = SOURCES,
) -> int:
    """Install a lockfile, build changed generations, clean its work, and retain snapshots."""
    io = host if host is not None else RealHost()
    root = Path(checkout) if checkout is not None else Path(__file__).parents[3]
    active_stage = ["preconditions"]
    lock_claim: backuplock.Claim | None = None
    try:
        stage, lockfile, court_map, inputs_by_collection, lock_claim = _preconditions(
            args, io, rendered_dir, root, sources, clock, site_path,
        )
        if not stage.ok:
            return _refuse("preconditions", stage.detail, stage.fix)
        assert lockfile is not None and court_map is not None
        chunk_guards = build.chunk_guards(io, root, site_path, COMMAND_PATH)
        if isinstance(chunk_guards, Problem):
            refusal = _build_refusal(chunk_guards)
            return _refuse("preconditions", refusal.detail, refusal.fix)
        report.print_stage(stage)
        return _run_install_stages(
            io, rendered_dir, root, snapshots_root, work_root, lockfile, court_map,
            inputs_by_collection, chunk_guards, clock, sleep, monotonic, active_stage,
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
        if active_stage[0] == "overruled":
            return _refuse(
                "overruled", "interrupted; the overruled job continues in the worker",
                f"Run {report.command(COMMAND_PATH)} again to rejoin it.",
            )
        if active_stage[0] == "agreement":
            return _refuse(
                "agreement", "interrupted; agreement jobs continue in the worker",
                f"Run {report.command(COMMAND_PATH)} again to rejoin them.",
            )
        if active_stage[0] == "collections":
            return _refuse(
                "collections", "interrupted; the verdicts stand",
                f"Run {report.command(COMMAND_PATH)} again.",
            )
        if active_stage[0] in {"generation", "guard", "reserve", "counts", "built"}:
            return _refuse(
                active_stage[0], "interrupted; the row stands",
                f"Run {report.command(COMMAND_PATH)} again.",
            )
        if active_stage[0] == "cluster":
            return _refuse(
                "cluster", "interrupted; the clustering continues in the worker",
                f"Run {report.command(COMMAND_PATH)} again to rejoin it.",
            )
        if active_stage[0] == "treatment":
            return _refuse(
                "treatment", "interrupted; the generation's states are replaced whole by the next run",
                f"Run {report.command(COMMAND_PATH)} again.",
            )
        if active_stage[0] == "report":
            return _refuse(
                "report", "interrupted; the report row may stand",
                f"Run {report.command(COMMAND_PATH)} again.",
            )
        if active_stage[0] == "build":
            return _refuse(
                "build", "interrupted; the build continues in the worker",
                f"Run {report.command(COMMAND_PATH)} again to rejoin it.",
            )
        if active_stage[0] == "dense":
            return _refuse(
                "dense", "interrupted; the embed continues in the worker",
                f"Run {report.command(COMMAND_PATH)} again to rejoin it.",
            )
        if active_stage[0] == "installed":
            return _refuse(
                "installed", "interrupted; the installed move is one transaction",
                f"Run {report.command(COMMAND_PATH)} again.",
            )
        if active_stage[0] == "clean":
            return _refuse(
                "clean", "interrupted; a directory removed in part is removed whole by the next run",
                f"Run {report.command(COMMAND_PATH)} again to finish cleanup.",
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
