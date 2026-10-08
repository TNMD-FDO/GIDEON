"""Verify local corpus snapshots and write their committed lockfiles."""

import argparse
import difflib
import hashlib
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from gideon.host import backuplock, report, stack
from gideon.host.corpus import artifacts, record, resolve, snapshots
from gideon.host.corpus.lockfile import (
    LABEL,
    PIPELINE_VERSION,
    SCHEMA_VERSION,
    IndexDocument,
    Lockfile,
    LockfileDirectoryResult,
    SidecarEntry,
    SourcePin,
    check_courts,
    check_egress_hosts,
    load_lockfile,
    read_lockfile_directory,
    render_lockfile,
    render_sidecar,
    same_state,
)
from gideon.host.corpus.sources import SOURCES, SourceDefinition
from gideon.host.courts import CourtMap
from gideon.host.render.worker import SNAPSHOTS_ROOT
from gideon.host.report import Problem, StageResult
from gideon.host.sysio import PathLike, RealHost, WritableBytesHost


@dataclass(frozen=True, slots=True)
class DerivedRequest:
    """A validated base and the court ids to add to it."""

    base: Lockfile
    court_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LockfileCandidate:
    """A cut's lockfile with the exact companion bytes to write."""

    lockfile: Lockfile
    index_bytes: Mapping[str, Mapping[str, bytes]]
    sidecar_bytes: Mapping[str, bytes]
    written_detail: str


def _retry_fix(fix: str) -> str:
    return snapshots.retry_fix(fix, command_path="corpus cut")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _refuse(name: str, detail: str, fix: str) -> int:
    stage = StageResult(name, False, detail, fix)
    if name == "preconditions":
        print(report.stage_line(stage), file=sys.stderr)
    else:
        report.print_stage(stage)
    return 1


def _artifact_preconditions(
    host: WritableBytesHost,
    rendered_dir: PathLike,
    checkout: Path,
    registry: Sequence[SourceDefinition],
    *, check_worker: bool,
) -> tuple[StageResult, LockfileDirectoryResult | None, CourtMap | None]:
    loaded, issue = artifacts.load_artifacts(
        host, rendered_dir, checkout, registry, command_path="corpus cut",
        check_worker=check_worker,
    )
    if issue is not None:
        return issue, None, None
    assert loaded is not None
    directory = loaded.directory
    court_map = loaded.court_map
    corpus_group = loaded.allowlist.group("corpus")
    allowed = {item.host for item in corpus_group.hosts} if corpus_group else set()
    for lockfile in directory.lockfiles:
        issue = artifacts.artifact_problem(
            check_courts(lockfile, court_map), lockfile.label, command_path="corpus cut",
        )
        if issue is not None:
            return issue, None, None
        issue = artifacts.artifact_problem(
            check_egress_hosts(lockfile, loaded.allowlist), lockfile.label,
            command_path="corpus cut",
        )
        if issue is not None:
            return issue, None, None
    known = {source.name: source.carries_courts for source in registry}
    if len(known) != len(registry):
        return StageResult(
            "preconditions", False, "source names repeat in the registry",
            "Correct the source definitions in the release, then run "
            f"{report.command('corpus cut')} again.",
        ), None, None
    for source in registry:
        try:
            parsed = urlsplit(source.base_url)
            scheme = parsed.scheme
            source_host = parsed.hostname
            port = parsed.port
        except ValueError:
            scheme, source_host, port = "", None, None
        if scheme != "https" or source_host is None or port not in (None, 443):
            return StageResult(
                "preconditions", False, f"source {source.name} has an invalid HTTPS base URL",
                "Correct the source URL in the release, then run "
                f"{report.command('corpus cut')} again.",
            ), None, None
        if source_host not in allowed:
            return StageResult(
                "preconditions", False,
                f"source {source.name} host {source_host} is outside the corpus allowlist",
                f"Add {source_host} to the corpus group in config/egress.yaml, "
                f"run {report.command('apply')}, then retry.",
            ), None, None
        if source.carries_courts and directory.newest is None:
            if not source.first_courts or tuple(sorted(set(source.first_courts))) != source.first_courts:
                return StageResult(
                    "preconditions", False, f"source {source.name} first courts are invalid",
                    "Correct the sorted court ids in the release, then run "
                    f"{report.command('corpus cut')} again.",
                ), None, None
            missing = court_map.unresolved(source.first_courts)
            if missing:
                return StageResult(
                    "preconditions", False,
                    f"source {source.name} has unknown court ids: {', '.join(missing)}",
                    "Correct the court ids in the release, then run "
                    f"{report.command('corpus cut')} again.",
                ), None, None
    detail = ("root, worker, and release artifacts are ready" if check_worker
              else "root and release artifacts are ready")
    return StageResult("preconditions", True, detail, ""), directory, court_map


def _derived_preconditions(
    host: resolve.CorpusHost,
    rendered_dir: PathLike,
    registry: Sequence[SourceDefinition],
    directory: LockfileDirectoryResult,
    court_map: CourtMap,
    base_label: str,
    added_courts: str,
) -> tuple[StageResult, DerivedRequest | None]:
    command = report.command("corpus cut")
    if LABEL.fullmatch(base_label) is None:
        return StageResult(
            "preconditions", False, "expected a lockfile label such as corpus-2026-10-07",
            f"Choose a label under corpus/lockfiles/, then run {command} again.",
        ), None
    base = next((item for item in directory.lockfiles if item.label == base_label), None)
    if base is None:
        return StageResult(
            "preconditions", False, f"no committed lockfile is labelled {base_label}",
            f"Choose a label under corpus/lockfiles/, then run {command} again.",
        ), None
    court_ids = tuple(item.strip() for item in added_courts.split(","))
    if "" in court_ids:
        return StageResult(
            "preconditions", False, "--add-courts has an empty court id",
            f"Name court ids from courts.yaml, then run {command} again.",
        ), None
    seen: set[str] = set()
    for court_id in court_ids:
        if court_id in seen:
            return StageResult(
                "preconditions", False, f"court id {court_id} is repeated",
                f"Name each court id once, then run {command} again.",
            ), None
        seen.add(court_id)
    for court_id in court_ids:
        if court_map.court(court_id) is None:
            nearest = difflib.get_close_matches(court_id, court_map.courts, n=1, cutoff=0.0)
            suggestion = f"; nearest id is {nearest[0]}" if nearest else ""
            return StageResult(
                "preconditions", False, f"unknown court id {court_id}{suggestion}",
                f"Look up court ids in courts.yaml as described in "
                f"docs/runbooks/release-files.md §11, then run {command} again.",
            ), None
    court_sources = tuple(source for source in registry if source.carries_courts)
    for court_id in court_ids:
        for source in court_sources:
            if court_id in (base.sources[source.name].courts or ()):
                return StageResult(
                    "preconditions", False,
                    f"court id {court_id} is already in base {base_label}",
                    f"Choose a court absent from {base_label}, then run {command} again.",
                ), None
    if not court_sources:
        return StageResult(
            "preconditions", False, "no source in the registry carries courts",
            f"Correct the source registry in the release, then run {command} again.",
        ), None
    row = record.read_cut(host, rendered_dir, base_label, command_path="corpus cut")
    if isinstance(row, Problem):
        return StageResult("preconditions", False, row.problem, row.fix), None
    if row is None:
        return StageResult(
            "preconditions", False, f"base {base_label} is not recorded on this box",
            f"Run {report.command('corpus install')} {base_label}, then run {command} again.",
        ), None
    try:
        same_time = datetime.fromisoformat(row.cut_at.replace("Z", "+00:00")) == datetime.fromisoformat(
            base.cut_at.replace("Z", "+00:00")
        )
    except ValueError:
        same_time = False
    bindings = tuple(sorted((name, pin.snapshot_date) for name, pin in base.sources.items()))
    recorded_bindings = tuple(sorted((item.source, item.snapshot_date) for item in row.sources))
    if not (
        row.label == base.label and row.schema == base.schema
        and row.pipeline == base.pipeline and same_time
        and row.reason == base.reason and row.base == base.base
        and recorded_bindings == bindings
    ):
        return StageResult(
            "preconditions", False, f"base {base_label}'s record disagrees with its lockfile",
            f"Restore the matching lockfile from the release checkout, then run {command} again.",
        ), None
    return StageResult(
        "preconditions", True, "root, release artifacts, and the base's record are ready", ""
    ), DerivedRequest(base, court_ids)


def _preconditions(
    args: argparse.Namespace,
    host: resolve.CorpusHost,
    rendered_dir: PathLike,
    checkout: Path,
    registry: Sequence[SourceDefinition],
    clock: Callable[[], datetime],
) -> tuple[StageResult, LockfileDirectoryResult | None, DerivedRequest | None,
           backuplock.Claim | None]:
    base_label = getattr(args, "base", None)
    added_courts = getattr(args, "add_courts", None)
    if (base_label is None) != (added_courts is None):
        given, missing = (
            ("--add-courts", "--base") if base_label is None else ("--base", "--add-courts")
        )
        return StageResult(
            "preconditions", False, f"{given} was given without {missing}",
            f"Run {report.command('corpus cut')} --base <label> --add-courts <ids>.",
        ), None, None, None
    if host.geteuid() != 0:
        return StageResult(
            "preconditions", False, "root privileges are required",
            f"Run {report.command('corpus cut')}, then retry.",
        ), None, None, None
    claim = backuplock.claim(
        host, command=report.command_name("corpus cut"), now=clock(),
        lock=backuplock.CORPUS_LOCK,
    )
    if claim.refusal is not None:
        return StageResult(
            "preconditions", False, claim.refusal.detail,
            _retry_fix(claim.refusal.fix),
        ), None, None, None
    if not claim.taken:
        return StageResult(
            "preconditions", False, "a corpus cut is already running in this process",
            f"Wait for it to finish, then run {report.command('corpus cut')} again.",
        ), None, None, None
    try:
        stage, directory, court_map = _artifact_preconditions(
            host, rendered_dir, checkout, registry, check_worker=base_label is None,
        )
        request: DerivedRequest | None = None
        if stage.ok and base_label is not None:
            assert directory is not None and court_map is not None and added_courts is not None
            stage, request = _derived_preconditions(
                host, rendered_dir, registry, directory, court_map,
                base_label, added_courts,
            )
    except BaseException:
        backuplock.release_claim(host, claim, lock=backuplock.CORPUS_LOCK)
        raise
    return stage, directory, request, claim


def _cut_reason(pins: Mapping[str, SourcePin], newest: Lockfile | None) -> str:
    if newest is None or any(
        pin.courts != (newest.sources[name].courts if name in newest.sources else None)
        for name, pin in pins.items()
    ):
        return "tranche"
    caselaw = pins.get("caselaw")
    previous_caselaw = newest.sources.get("caselaw")
    if caselaw is not None and (
        previous_caselaw is None or caselaw.snapshot_date != previous_caselaw.snapshot_date
    ):
        return "quarterly"
    if newest.pipeline != PIPELINE_VERSION:
        return "pipeline"
    return "instrument"


def _lockfile_owner(host: WritableBytesHost, checkout: Path, directory: Path) -> tuple[int, int]:
    corpus = directory.parent
    if not host.exists(corpus):
        checkout_owner = host.stat(checkout)
        host.mkdir(corpus, parents=True, exist_ok=True)
        host.chown(corpus, checkout_owner.st_uid, checkout_owner.st_gid)
    if not host.exists(directory):
        corpus_owner = host.stat(corpus)
        host.mkdir(directory, parents=True, exist_ok=True)
        host.chown(directory, corpus_owner.st_uid, corpus_owner.st_gid)
    owner = host.stat(directory)
    return owner.st_uid, owner.st_gid


def _candidate_time(now: datetime) -> tuple[str, str] | StageResult:
    if now.tzinfo is None:
        return StageResult("lockfile", False, "cut clock has no timezone",
                           f"Use a UTC clock, then run {report.command('corpus cut')} again.")
    timestamp = now.astimezone(UTC)
    return f"corpus-{timestamp.date().isoformat()}", timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _full_candidate(
    host: resolve.CorpusHost,
    checkout: Path,
    registry: Sequence[SourceDefinition],
    resolved: Mapping[str, resolve.ResolvedSource],
    verified: Mapping[str, tuple[SidecarEntry, ...]],
    newest: Lockfile | None,
    now: datetime,
) -> LockfileCandidate | StageResult:
    time_fields = _candidate_time(now)
    if isinstance(time_fields, StageResult):
        return time_fields
    label, cut_at = time_fields
    directory = checkout / "corpus/lockfiles"
    pins: dict[str, SourcePin] = {}
    index_bytes: dict[str, Mapping[str, bytes]] = {}
    sidecar_bytes: dict[str, bytes] = {}
    for source in registry:
        selected = resolved[source.name]
        snapshot = selected.snapshot
        previous = newest.sources.get(source.name) if newest is not None else None
        same_snapshot = previous is not None and previous.snapshot_date == snapshot.snapshot_date
        if same_snapshot:
            assert previous is not None and newest is not None
            indexes = previous.index
            mirror_url = previous.mirror_url
            # A kept snapshot keeps the index documents first committed with it,
            # so their bytes come from that lockfile, never from this cut's read.
            kept: dict[str, bytes] = {}
            for document in indexes:
                kept_path = directory / newest.label / f"{source.name}.{document.name}"
                try:
                    kept[document.name] = host.read_bytes(kept_path)
                except OSError as exc:
                    return StageResult(
                        "lockfile", False,
                        f"{kept_path} could not be read ({type(exc).__name__})",
                        _retry_fix(f"Restore {kept_path} from the release checkout."),
                    )
            source_index_bytes: Mapping[str, bytes] = kept
        else:
            documents: list[IndexDocument] = []
            for request in source.index_documents():
                data = selected.index_bytes.get(request.name)
                if data is None:
                    return StageResult("lockfile", False,
                                       f"source {source.name} index {request.name} is missing",
                                       f"Run {report.command('corpus cut')} again after fetching the index.")
                documents.append(IndexDocument(request.name, request.url,
                                               hashlib.sha256(data).hexdigest(), len(data)))
            indexes = tuple(documents)
            mirror_url = None
            source_index_bytes = selected.index_bytes
        entries = verified[source.name]
        sidecar = render_sidecar(entries).encode("utf-8")
        courts = (
            (previous.courts if previous is not None else source.first_courts)
            if source.carries_courts else None
        )
        pins[source.name] = SourcePin(
            snapshot.snapshot_date, source.base_url, mirror_url,
            hashlib.sha256(sidecar).hexdigest(), len(entries),
            sum(entry.size for entry in entries), indexes, entries, courts,
        )
        index_bytes[source.name] = source_index_bytes
        sidecar_bytes[source.name] = sidecar
    candidate = Lockfile(
        SCHEMA_VERSION, label, PIPELINE_VERSION,
        cut_at, _cut_reason(pins, newest), pins,
    )
    return LockfileCandidate(candidate, index_bytes, sidecar_bytes, f"wrote {label}")


def _derived_candidate(
    host: resolve.CorpusHost,
    checkout: Path,
    registry: Sequence[SourceDefinition],
    request: DerivedRequest,
    now: datetime,
) -> LockfileCandidate | StageResult:
    time_fields = _candidate_time(now)
    if isinstance(time_fields, StageResult):
        return time_fields
    label, cut_at = time_fields
    base = request.base
    companion = checkout / "corpus/lockfiles" / base.label
    pins: dict[str, SourcePin] = {}
    index_bytes: dict[str, Mapping[str, bytes]] = {}
    sidecar_bytes: dict[str, bytes] = {}
    for source in registry:
        pin = base.sources[source.name]
        courts = (tuple(sorted(set(pin.courts or ()) | set(request.court_ids)))
                  if source.carries_courts else None)
        pins[source.name] = SourcePin(
            pin.snapshot_date, pin.base_url, pin.mirror_url, pin.sidecar_sha256,
            pin.files, pin.bytes, pin.index, pin.entries, courts,
        )
        documents: dict[str, bytes] = {}
        for document in pin.index:
            path = companion / f"{source.name}.{document.name}"
            try:
                documents[document.name] = host.read_bytes(path)
            except OSError as exc:
                return StageResult(
                    "lockfile", False, f"{path} could not be read ({type(exc).__name__})",
                    _retry_fix(f"Restore {path} from the release checkout."),
                )
        index_bytes[source.name] = documents
        path = companion / f"{source.name}.sha256"
        try:
            sidecar_bytes[source.name] = host.read_bytes(path)
        except OSError as exc:
            return StageResult(
                "lockfile", False, f"{path} could not be read ({type(exc).__name__})",
                _retry_fix(f"Restore {path} from the release checkout."),
            )
    candidate = Lockfile(
        SCHEMA_VERSION, label, PIPELINE_VERSION, cut_at, "tranche", pins, base.label,
    )
    added = ",".join(request.court_ids)
    return LockfileCandidate(
        candidate, index_bytes, sidecar_bytes,
        f"wrote {label} from {base.label}, courts + {added}",
    )


def _write_lockfile(
    host: resolve.CorpusHost,
    rendered_dir: PathLike,
    checkout: Path,
    registry: Sequence[SourceDefinition],
    newest: Lockfile | None,
    built: LockfileCandidate,
) -> StageResult:
    candidate = built.lockfile
    label = candidate.label
    pins = candidate.sources
    directory = checkout / "corpus/lockfiles"
    companion = directory / label
    if newest is not None and same_state(candidate, newest):
        return StageResult("lockfile", True, f"unchanged; {newest.label} already pins this state", "")
    later_fix = f"Run {report.command('corpus cut')} on a later UTC date."
    if host.exists(directory / f"{label}.yaml"):
        return StageResult(
            "lockfile", False, f"label {label} already has a lockfile",
            later_fix,
        )
    recorded = record.read_cut(host, rendered_dir, label, command_path="corpus cut")
    if isinstance(recorded, Problem):
        return StageResult("lockfile", False, recorded.problem, recorded.fix)
    if recorded is not None:
        return StageResult("lockfile", False, f"label {label} is already recorded", later_fix)
    try:
        uid, gid = _lockfile_owner(host, checkout, directory)
        if host.exists(companion):
            host.rmtree(companion)
        host.mkdir(companion)
        host.chown(companion, uid, gid)
        for source in registry:
            for document in pins[source.name].index:
                document_path = companion / f"{source.name}.{document.name}"
                host.write_bytes(document_path, built.index_bytes[source.name][document.name])
                host.chown(document_path, uid, gid)
        for source in registry:
            sidecar_path = companion / f"{source.name}.sha256"
            host.write_bytes(sidecar_path, built.sidecar_bytes[source.name])
            host.chown(sidecar_path, uid, gid)
        path = directory / f"{label}.yaml"
        host.write_text(path, render_lockfile(candidate))
        host.chown(path, uid, gid)
    except OSError as exc:
        return StageResult(
            "lockfile", False, f"lockfile {label} could not be written ({type(exc).__name__})",
            f"Fix access to {directory}, then run {report.command('corpus cut')} again.",
        )
    loaded = load_lockfile(path, known_sources={source.name: source.carries_courts for source in registry}, host=host)
    if not loaded.ok:
        return StageResult(
            "lockfile", False, f"written lockfile {label} does not load",
            "Restore the lockfile files from the release checkout, then run "
            f"{report.command('corpus cut')} again.",
        )
    return StageResult("lockfile", True, built.written_detail, "")


def _run_cut_stages(
    io: resolve.CorpusHost,
    rendered_dir: PathLike,
    root: Path,
    snapshots_root: PathLike,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    sources: Sequence[SourceDefinition],
    newest: Lockfile | None,
    active_stage: list[str],
) -> int:
    active_stage[0] = "resolve"
    resolved: dict[str, resolve.ResolvedSource] = {}
    for source in sources:
        result = resolve.resolve_source(
            io, rendered_dir, snapshots_root, source, command_path="corpus cut",
            sleep=sleep, monotonic=monotonic,
        )
        if isinstance(result, resolve.ResolveFailure):
            return _refuse(
                "resolve", f"{source.name}: {result.problem.problem}", result.problem.fix
            )
        resolved[source.name] = result
        report.print_stage(StageResult(
            "resolve", True,
            f"{source.name}: {result.snapshot.snapshot_date}, {len(result.snapshot.entries)} files", "",
        ))
    active_stage[0] = "fetch"
    fetched: dict[str, snapshots.FetchedSource] = {}
    for source in sources:
        fetch_result = snapshots.fetch_source(
            io, rendered_dir, snapshots_root, source.name,
            resolved[source.name].snapshot.snapshot_date,
            tuple((entry.path, entry.url) for entry in resolved[source.name].snapshot.entries),
            sleep=sleep, monotonic=monotonic, command_path="corpus cut",
        )
        if isinstance(fetch_result, Problem):
            return _refuse("fetch", f"{source.name}: {fetch_result.problem}", fetch_result.fix)
        fetched[source.name] = fetch_result
        report.print_stage(StageResult(
            "fetch", True, f"{source.name}: {fetch_result.summary()}", "",
        ))
    active_stage[0] = "verify"
    verified: dict[str, tuple[SidecarEntry, ...]] = {}
    for source in sources:
        snapshot = resolved[source.name].snapshot
        previous = newest.sources.get(source.name) if newest is not None else None
        old_entries = (
            {entry.path: entry for entry in previous.entries}
            if previous is not None and previous.snapshot_date == snapshot.snapshot_date else None
        )
        entries, verify_issue = snapshots.verify(
            io, snapshots_root, source.name, snapshot.snapshot_date,
            tuple((entry.path, (old_entries[entry.path].sha256, old_entries[entry.path].size)
                   if old_entries is not None and entry.path in old_entries else None)
                  for entry in snapshot.entries),
            expected_paths=set(old_entries) if old_entries is not None else None,
            command_path="corpus cut",
        )
        if verify_issue is not None:
            return _refuse("verify", verify_issue.detail, verify_issue.fix)
        assert entries is not None
        verified[source.name] = entries
    report.print_stage(StageResult(
        "verify", True, f"{sum(len(entries) for entries in verified.values())} files match fetch records", "",
    ))
    verified_at = clock()
    if verified_at.tzinfo is None:
        return _refuse("record", "cut clock has no timezone",
                       f"Use a UTC clock, then run {report.command('corpus cut')} again.")
    active_stage[0] = "lockfile"
    built = _full_candidate(io, root, sources, resolved, verified, newest, clock())
    if isinstance(built, StageResult):
        return _refuse("lockfile", built.detail, built.fix)
    stage = _write_lockfile(io, rendered_dir, root, sources, newest, built)
    if not stage.ok:
        return _refuse("lockfile", stage.detail, stage.fix)
    report.print_stage(stage)
    active_stage[0] = "record"
    fetched_at: dict[str, str] = {}
    for source in sources:
        latest = fetched[source.name].latest_fetch()
        if latest is None:
            return _refuse(
                "record", f"source {source.name} has no fetch records",
                _retry_fix("Restore its snapshot files."),
            )
        fetched_at[source.name] = latest
    return _record_stage(
        io, rendered_dir, root, sources, stage,
        lambda lockfile: record.write_cut(
            io, rendered_dir, lockfile, fetched_at,
            verified_at.astimezone(UTC).isoformat(), command_path="corpus cut",
        ),
    )


def _record_stage(
    io: resolve.CorpusHost,
    rendered_dir: PathLike,
    root: Path,
    sources: Sequence[SourceDefinition],
    stage: StageResult,
    write: Callable[[Lockfile], Problem | None],
) -> int:
    directory = read_lockfile_directory(
        root / "corpus/lockfiles",
        known_sources={source.name: source.carries_courts for source in sources}, host=io,
    )
    if directory.errors or directory.newest is None:
        return _refuse(
            "record", "written corpus lockfile could not be loaded",
            _retry_fix("Restore the lockfile and its companion files from the release checkout."),
        )
    lockfile = directory.newest
    issue = write(lockfile)
    if issue is not None:
        return _refuse("record", issue.problem, issue.fix)
    row = record.read_cut(io, rendered_dir, lockfile.label, command_path="corpus cut")
    if isinstance(row, Problem):
        return _refuse("record", row.problem, row.fix)
    if row is None:
        return _refuse(
            "record", f"lockfile {lockfile.label} was not found after writing",
            _retry_fix(f"Run {stack.logs_fix(rendered_dir, 'postgres')} to inspect the record."),
        )
    base_detail = f", base {row.base}" if row.base is not None else ""
    report.print_stage(StageResult("record", True, f"{row.label}: {row.state}{base_detail}", ""))
    if stage.detail.startswith("wrote "):
        print("Next: commit the new lockfile and companion files.")
    return 0


def _run_derived_stages(
    io: resolve.CorpusHost,
    rendered_dir: PathLike,
    root: Path,
    clock: Callable[[], datetime],
    sources: Sequence[SourceDefinition],
    newest: Lockfile | None,
    request: DerivedRequest,
    active_stage: list[str],
) -> int:
    active_stage[0] = "lockfile"
    built = _derived_candidate(io, root, sources, request, clock())
    if isinstance(built, StageResult):
        return _refuse("lockfile", built.detail, built.fix)
    stage = _write_lockfile(io, rendered_dir, root, sources, newest, built)
    if not stage.ok:
        return _refuse("lockfile", stage.detail, stage.fix)
    report.print_stage(stage)
    active_stage[0] = "record"
    return _record_stage(
        io, rendered_dir, root, sources, stage,
        lambda lockfile: record.write_derived_cut(
            io, rendered_dir, lockfile, command_path="corpus cut",
        ),
    )


def run_corpus_cut(
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
    """Run the corpus cut stages in order, releasing its lock on every exit."""
    io = host if host is not None else RealHost()
    root = Path(checkout) if checkout is not None else Path(__file__).parents[3]
    active_stage = ["preconditions"]
    lock_claim: backuplock.Claim | None = None
    try:
        stage, directory, request, lock_claim = _preconditions(
            args, io, rendered_dir, root, sources, clock,
        )
        if not stage.ok:
            return _refuse("preconditions", stage.detail, stage.fix)
        report.print_stage(stage)
        assert directory is not None
        if request is not None:
            return _run_derived_stages(
                io, rendered_dir, root, clock, sources, directory.newest, request, active_stage,
            )
        return _run_cut_stages(
            io, rendered_dir, root, snapshots_root, clock, sleep, monotonic,
            sources, directory.newest, active_stage,
        )
    except KeyboardInterrupt:
        if active_stage[0] == "fetch":
            return _refuse(
                "fetch", "interrupted; downloads continue in the worker",
                f"Run {report.command('corpus cut')} again to rejoin them.",
            )
        return _refuse(
            active_stage[0], "interrupted; nothing was recorded",
            f"Run {report.command('corpus cut')} again to start again.",
        )
    finally:
        if lock_claim is not None:
            backuplock.release_claim(io, lock_claim, lock=backuplock.CORPUS_LOCK)
