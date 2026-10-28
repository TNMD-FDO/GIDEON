"""Copy pinned source snapshots, lockfiles, and cached model revisions to the archive."""

import json
import os
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

from gideon.host import (
    audit,
    backuplock,
    egress,
    images,
    models,
    nogpu,
    report,
    site,
    stack,
    weights,
)
from gideon.host.checks.network import (
    PROBE_RETRY_WAIT_SECONDS,
    PROBE_TIMEOUT_SECONDS,
    probe_verdict,
)
from gideon.host.corpus.lockfile import Lockfile, read_lockfile_directory
from gideon.host.render.worker import SNAPSHOTS_ROOT
from gideon.host.report import Problem, StageResult
from gideon.host.stages import aware_now
from gideon.host.steps import temporary_wgetrc, wget_argv_for_site
from gideon.host.sysio import (
    CompletedText,
    Host,
    LockingHost,
    PathLike,
    ReadBytesHost,
    RealHost,
)
from gideon.host.weights import MODELS_ROOT

ARCHIVE_REMOTE_CONFIG: Final = Path("/etc/gideon/archive-remote.conf")
ARCHIVE_EGRESS_GROUP: Final = "archive"
REMOTE_CORPUS_SNAPSHOTS: Final = "corpus/snapshots"
REMOTE_CORPUS_LOCKFILES: Final = "corpus/lockfiles"
REMOTE_MODELS: Final = "models"
AUDIT_KIND: Final = "archive_push"
_RENDERED_DIR: Final = Path("/etc/gideon/rendered")
_SITE_PATH: Final = Path("/etc/gideon/site.yaml")
# Exempt: operational child bound; the unit bounds the whole copy run.
_COMMAND_TIMEOUT: Final = 21600.0


class ArchiveHost(ReadBytesHost, LockingHost, Protocol):
    """The byte reads and lock operations used by an archive push."""


@dataclass(frozen=True, slots=True)
class ArchiveFile:
    """A pinned file's relative path, expected size, and local presence."""

    path: str
    size: int
    present: bool


@dataclass(frozen=True, slots=True)
class TransferRoot:
    """One local-to-remote directory mapping and its selected files."""

    name: str
    local_root: Path
    remote_root: str
    files: tuple[ArchiveFile, ...]
    follow_links: bool


@dataclass(frozen=True, slots=True)
class Inventory:
    """The transfer roots and the ids they cover or leave absent."""

    roots: tuple[TransferRoot, ...]
    lockfiles: tuple[str, ...]
    snapshots: tuple[str, ...]
    models: tuple[str, ...]
    absent_models: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CopyStats:
    """The files and bytes rclone transferred."""

    files: int
    bytes: int


@dataclass(frozen=True, slots=True)
class RemoteEntry:
    """One remote relative path and its byte size."""

    path: str
    size: int


@dataclass(frozen=True, slots=True)
class _Inputs:
    config: site.SiteConfig
    group: egress.EgressGroup
    lockfiles: tuple[Lockfile, ...]
    models_lock: models.ModelsLock
    remote: str


class _LoadError(Protocol):
    @property
    def problem(self) -> str: ...

    @property
    def fix(self) -> str: ...


def _retry_fix() -> str:
    return f"Correct the problem, then run {report.command('archive push')} again."


def _credential_fix() -> str:
    return (
        "Follow the re-authorisation steps in docs/runbooks/release-files.md §10, "
        f"then run {report.command('archive push')} again."
    )


def _load_failure(errors: Sequence[_LoadError]) -> StageResult | None:
    if not errors:
        return None
    return StageResult(
        "preconditions", False, "; ".join(error.problem for error in errors),
        "; ".join(dict.fromkeys(error.fix for error in errors))
        + f" Then run {report.command('archive push')} again.",
    )


def remote_path(remote: str, root: str, path: str = "") -> str:
    """Join a remote name, archive directory, and optional relative file path."""

    return f"{remote.rstrip(':')}:{root.rstrip('/')}" + (f"/{path}" if path else "")


def rclone_argv(*arguments: str, config_path: PathLike = ARCHIVE_REMOTE_CONFIG) -> list[str]:
    """Build every rclone call with the archive's one credential file."""

    return ["rclone", "--config", str(config_path), *arguments]


def rclone_environment(config: site.SiteConfig, host: Host) -> Mapping[str, str] | Problem:
    """Build the child's environment with proxy credentials outside argv."""

    proxy = images.proxy_environment(config, host)
    if not proxy.ok:
        return Problem(proxy.problem or "proxy authentication is unavailable", proxy.fix)
    return {**os.environ, **proxy.variables}


def _rclone(
    io: Host, config: site.SiteConfig, config_path: PathLike, *arguments: str,
) -> CompletedText | Problem:
    environment = rclone_environment(config, io)
    if isinstance(environment, Problem):
        return environment
    try:
        result = io.run(
            rclone_argv(*arguments, config_path=config_path), env=environment,
            timeout=_COMMAND_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"rclone {arguments[0]} could not run: {exc}", _retry_fix())
    if result.returncode != 0:
        diagnostic = report.command_detail(result)
        fix = _credential_fix() if any(
            word in diagnostic.lower() for word in ("oauth", "invalid_grant")
        ) else _retry_fix()
        if result.returncode in (126, 127):
            fix = f"Run {report.command('host provision --only archive-tools')}, then retry."
        return Problem(f"rclone {arguments[0]} failed: {diagnostic}", fix)
    return result


def parse_rclone_stats(stderr: str) -> CopyStats | None:
    """Read the final JSON stats, preserving exact file and byte counts."""

    latest = None
    for line in stderr.splitlines():
        try:
            document = json.loads(line)
        except ValueError:
            continue
        if not isinstance(document, dict) or not isinstance(document.get("stats"), dict):
            continue
        stats = document["stats"]
        files, byte_count = stats.get("transfers"), stats.get("bytes")
        if any(type(value) is not int or value < 0 for value in (files, byte_count)):
            return None
        latest = CopyStats(files, byte_count)
    return latest


def parse_remote_listing(stdout: str) -> tuple[RemoteEntry, ...]:
    """Read rclone lsf's path-and-size rows, refusing malformed listings."""

    entries: dict[str, RemoteEntry] = {}
    for line in stdout.splitlines():
        path, separator, size = line.rpartition(";")
        if not separator or not path or not size.isdecimal() or path in entries:
            raise ValueError(f"invalid remote listing row: {line!r}")
        entries[path] = RemoteEntry(path, int(size))
    return tuple(entries.values())


def _regular_file(io: Host, path: Path) -> bool:
    try:
        return stat.S_ISREG(io.stat(path).st_mode)
    except FileNotFoundError:
        return False


def _lockfile_files(io: Host, directory: Path, prefix: str = "") -> tuple[ArchiveFile, ...]:
    files: list[ArchiveFile] = []
    for name in sorted(io.listdir(directory / prefix)):
        relative = f"{prefix}/{name}" if prefix else name
        info = io.stat(directory / relative)
        if stat.S_ISDIR(info.st_mode):
            files.extend(_lockfile_files(io, directory, relative))
        elif stat.S_ISREG(info.st_mode):
            files.append(ArchiveFile(relative, info.st_size, True))
    return tuple(files)


def _model_pins(lock: models.ModelsLock) -> tuple[models.ModelPin | models.CandidatePin, ...]:
    return (*[pin for profile in lock.profiles for pin in profile.models], *lock.candidates)


def inventory(
    lockfiles: Sequence[Lockfile], models_lock: models.ModelsLock,
    lockfiles_root: PathLike, *, host: Host | None = None,
    snapshots_root: PathLike = SNAPSHOTS_ROOT, models_root: PathLike = MODELS_ROOT,
) -> Inventory:
    """Select pinned snapshot and model files and every regular lockfiles file."""

    io = host or RealHost()
    snapshots_dir, lockfiles_dir = Path(snapshots_root), Path(lockfiles_root)
    snapshot_names: set[str] = set()
    snapshot_files: dict[str, ArchiveFile] = {}
    for lockfile in lockfiles:
        for name, pin in lockfile.sources.items():
            directory = f"{name}-{pin.snapshot_date}"
            snapshot_names.add(directory)
            for entry in pin.entries:
                relative = f"{directory}/{entry.path}"
                selected = ArchiveFile(
                    relative, entry.size, _regular_file(io, snapshots_dir / relative),
                )
                if relative in snapshot_files and snapshot_files[relative] != selected:
                    raise ValueError(f"conflicting snapshot pins for {relative}")
                snapshot_files[relative] = selected
    roots = [
        TransferRoot(
            "snapshots", snapshots_dir, REMOTE_CORPUS_SNAPSHOTS,
            tuple(snapshot_files[path] for path in sorted(snapshot_files)), False,
        ),
        TransferRoot(
            "lockfiles", lockfiles_dir, REMOTE_CORPUS_LOCKFILES,
            _lockfile_files(io, lockfiles_dir), False,
        ),
    ]
    revisions: dict[tuple[str, str], dict[str, models.PinnedFile]] = {}
    for model_pin in _model_pins(models_lock):
        files = revisions.setdefault((model_pin.repo, model_pin.revision), {})
        for model_file in model_pin.files:
            if model_file.path in files and files[model_file.path] != model_file:
                raise ValueError(
                    f"conflicting model pins for {model_pin.repo}@{model_pin.revision}/{model_file.path}"
                )
            files[model_file.path] = model_file
    held_models: list[str] = []
    absent_models: list[str] = []
    for (repo, revision), files in sorted(revisions.items()):
        identity = f"{repo}@{revision}"
        model_directory = weights.repository_dir(repo, models_root) / "snapshots" / revision
        try:
            info = io.stat(model_directory)
        except FileNotFoundError:
            absent_models.append(identity)
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"model snapshot {identity} is not a directory")
        held_models.append(identity)
        roots.append(TransferRoot(
            identity, model_directory, f"{REMOTE_MODELS}/{repo}/{revision}",
            tuple(ArchiveFile(entry.path, entry.size, _regular_file(io, model_directory / entry.path))
                  for _, entry in sorted(files.items())), True,
        ))
    return Inventory(
        tuple(roots), tuple(lockfile.label for lockfile in lockfiles),
        tuple(sorted(snapshot_names)), tuple(held_models), tuple(absent_models),
    )


def _preconditions(
    io: ArchiveHost, checkout: Path, rendered_dir: PathLike, site_path: PathLike,
    config_path: PathLike,
) -> tuple[StageResult, _Inputs | None]:
    def failed(problem: str, fix: str) -> tuple[StageResult, None]:
        return StageResult("preconditions", False, problem, fix), None

    if not nogpu.is_build_box(io):
        return failed(
            "this host is not the build box",
            nogpu.BUILD_BOX_ONLY_FIX.replace(
                "host provision --build-box", report.command("host provision --build-box"),
            ),
        )
    try:
        info = io.stat(config_path)
    except OSError as exc:
        return failed(f"archive credential file is unavailable: {exc}", _credential_fix())
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
        return failed("the archive credential must be a root-owned regular file", _credential_fix())
    if stat.S_IMODE(info.st_mode) & ~0o600:
        return failed(
            "the archive credential has permissions wider than 0600",
            f"Run sudo chmod 0600 {config_path}. {_credential_fix()}",
        )
    if not io.exists("/usr/bin/rclone"):
        return failed(
            "archive tool is missing: /usr/bin/rclone",
            f"Run {report.command('host provision --only archive-tools')}, then retry.",
        )
    site_result = site.load_site(Path(site_path), host=io)
    issue = _load_failure(site_result.errors)
    if issue is not None:
        return issue, None
    config = site_result.config
    assert config is not None
    egress_result = egress.load_egress_allowlist(checkout / "config/egress.yaml", host=io)
    issue = _load_failure(egress_result.errors)
    if issue is not None:
        return issue, None
    assert egress_result.allowlist is not None
    group = egress_result.allowlist.group(ARCHIVE_EGRESS_GROUP)
    if group is None or not group.hosts:
        return failed(
            "the archive egress group is absent or empty",
            "Restore config/egress.yaml from the release checkout, then retry.",
        )
    models_result = models.load_models_lock(checkout / "models.lock", host=io)
    issue = _load_failure(models_result.errors)
    if issue is not None:
        return issue, None
    assert models_result.lock is not None
    directory = read_lockfile_directory(checkout / "corpus/lockfiles", host=io)
    issue = _load_failure(directory.errors)
    if issue is not None:
        return issue, None
    remotes = _rclone(io, config, config_path, "listremotes")
    if isinstance(remotes, Problem):
        return failed(remotes.problem, remotes.fix)
    names = tuple(line.strip() for line in remotes.stdout.splitlines() if line.strip())
    if len(names) != 1 or not names[0].endswith(":"):
        return failed("the archive credential must declare exactly one remote", _credential_fix())
    problem = audit.probe(io, rendered_dir)
    if problem is not None:
        return failed(
            f"audit writer is unavailable: {problem}", stack.logs_fix(rendered_dir, "postgres"),
        )
    return StageResult("preconditions", True, "root, archive lock, credential, and record are ready", ""), _Inputs(
        config, group, directory.lockfiles, models_result.lock, names[0],
    )


def _probe_stage(io: Host, inputs: _Inputs, sleep: Callable[[float], None]) -> StageResult:
    fix = (
        "Allow the listed hosts through the firewall or configure egress_proxy "
        "per docs/runbooks/release-files.md §5, then run "
        f"{report.command('archive push')} again."
    )

    def probe(entry: egress.EgressHost, argv: list[str]) -> str | None | Problem:
        tool_fix = f"Install or repair wget, then run {report.command('archive push')} again."
        try:
            result = io.run(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            return Problem(f"egress probe could not run: {exc}", tool_fix)
        if result.returncode in (126, 127):
            return Problem(report.command_detail(result), tool_fix)
        return probe_verdict(result.returncode, result.stderr, entry.expect)

    try:
        with temporary_wgetrc(io, command=report.command('archive push'), prefix="gideon-archive") as wgetrc:
            if not wgetrc.ok:
                return StageResult("probe", False, wgetrc.problem or "proxy auth unavailable", wgetrc.fix)
            misses: list[tuple[egress.EgressHost, list[str], str]] = []
            for entry in inputs.group.hosts:
                argv = wget_argv_for_site(inputs.config, "/dev/null", entry.probe_url)
                argv[1:1] = [
                    "--tries=1", "-S", f"--connect-timeout={PROBE_TIMEOUT_SECONDS}",
                    f"--read-timeout={PROBE_TIMEOUT_SECONDS}",
                ]
                argv = wgetrc.prefix(argv)
                verdict = probe(entry, argv)
                if isinstance(verdict, Problem):
                    return StageResult("probe", False, verdict.problem, verdict.fix)
                if verdict is not None:
                    misses.append((entry, argv, verdict))
            if any(verdict == "no response" for _, _, verdict in misses):
                sleep(PROBE_RETRY_WAIT_SECONDS)
            missed: list[str] = []
            recovered: list[str] = []
            for entry, argv, verdict in misses:
                if verdict == "no response":
                    second = probe(entry, argv)
                    if isinstance(second, Problem):
                        return StageResult("probe", False, second.problem, second.fix)
                    if second is None:
                        recovered.append(f"{entry.host} (first {verdict})")
                        continue
                    verdict += f", then {second}"
                missed.append(f"{entry.host} ({verdict})")
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult("probe", False, f"egress probe could not run: {exc}", fix)
    suffix = f"; on a second probe: {', '.join(recovered)}" if recovered else ""
    if missed:
        return StageResult("probe", False, f"unreachable hosts: {', '.join(missed)}{suffix}", fix)
    return StageResult("probe", True, f"all {len(inputs.group.hosts)} archive egress hosts responded{suffix}", "")


def _inventory_stage(
    io: Host, inputs: _Inputs, checkout: Path, snapshots_root: PathLike, models_root: PathLike,
) -> tuple[StageResult, Inventory | None]:
    try:
        selected = inventory(
            inputs.lockfiles, inputs.models_lock, checkout / "corpus/lockfiles", host=io,
            snapshots_root=snapshots_root, models_root=models_root,
        )
    except (OSError, ValueError) as exc:
        return StageResult(
            "inventory", False, f"archive inventory could not be read: {exc}",
            "Restore the release lockfiles and repair access to the source files. " + _retry_fix(),
        ), None
    files = tuple(file for root in selected.roots for file in root.files)
    detail = (
        f"{len(selected.lockfiles)} lockfiles, {len(selected.snapshots)} snapshots, "
        f"{len(selected.models)} model revisions; {sum(file.present for file in files)} present, "
        f"{sum(not file.present for file in files)} missing files; "
        f"absent models: {', '.join(selected.absent_models) or 'none'}"
    )
    return StageResult("inventory", True, detail, ""), selected


@contextmanager
def _temporary_text(io: Host, text: str) -> Iterator[Path]:
    path = Path(backuplock.LOCK_DIR) / f"archive-{uuid.uuid4().hex}.list"
    io.write_text(path, text, mode=0o600)
    try:
        yield path
    finally:
        io.unlink(path, missing_ok=True)


def _transfer_arguments(root: TransferRoot, remote: str, files_from: Path) -> list[str]:
    return [
        "--files-from", str(files_from), *(["--copy-links"] if root.follow_links else []),
        str(root.local_root), remote_path(remote, root.remote_root),
    ]


def _copy_stage(
    io: Host, inputs: _Inputs, config_path: PathLike, selected: Inventory,
) -> tuple[StageResult, CopyStats | None]:
    total = CopyStats(0, 0)
    for root in selected.roots:
        present = tuple(file.path for file in root.files if file.present)
        if not present:
            continue
        try:
            with _temporary_text(io, "".join(f"{path}\n" for path in present)) as files_from:
                # The account may upload but not edit a file's metadata, so a
                # file equal by hash keeps the archive's modification time.
                result = _rclone(
                    io, inputs.config, config_path, "copy", "--no-update-modtime",
                    "--stats-one-line", "-v", "--use-json-log",
                    *_transfer_arguments(root, inputs.remote, files_from),
                )
        except OSError as exc:
            result = Problem(f"copy file list could not be used: {exc}", _retry_fix())
        if isinstance(result, Problem):
            return StageResult("copy", False, f"{root.name}: {result.problem}", result.fix), None
        stats = parse_rclone_stats(result.stderr)
        if stats is None:
            return StageResult("copy", False, f"{root.name}: rclone transfer stats are unreadable", _retry_fix()), None
        total = CopyStats(total.files + stats.files, total.bytes + stats.bytes)
    return StageResult("copy", True, f"sent {total.files} files, {total.bytes} bytes", ""), total


def _missing_fix(root: TransferRoot, lock: models.ModelsLock) -> str:
    if root.remote_root == REMOTE_CORPUS_SNAPSHOTS:
        return (
            f"Make a new cut with {report.command('corpus cut')}, or copy the named files "
            "from another holder to the archive; follow docs/runbooks/release-files.md §10. "
            f"Then run {report.command('archive push')} again."
        )
    pin = next(pin for pin in _model_pins(lock) if f"{pin.repo}@{pin.revision}" == root.name)
    pull = "models pull" + (f" {pin.name}" if isinstance(pin, models.CandidatePin) else "")
    return f"Run {report.command(pull)}, then {report.command('archive push')} again."


def _check_stage(
    io: Host, inputs: _Inputs, config_path: PathLike, selected: Inventory,
) -> tuple[StageResult, int]:
    missing_checked = 0
    for root in selected.roots:
        present = tuple(file.path for file in root.files if file.present)
        missing = tuple(file for file in root.files if not file.present)
        try:
            if present:
                with (
                    _temporary_text(io, "".join(f"{path}\n" for path in present)) as files_from,
                    _temporary_text(io, "") as differences,
                ):
                    result = _rclone(
                        io, inputs.config, config_path, "check", "--one-way",
                        "--combined", str(differences),
                        *_transfer_arguments(root, inputs.remote, files_from),
                    )
                    if isinstance(result, Problem):
                        names = tuple(line[2:] for line in io.read_text(differences).splitlines()
                                      if line and not line.startswith("= "))[:10]
                        detail = f"{root.name}: {result.problem}"
                        if names:
                            detail += f"; differences: {', '.join(names)}"
                        return StageResult("check", False, detail, result.fix), missing_checked
            if missing:
                result = _rclone(
                    io, inputs.config, config_path, "lsf", "--format", "ps", "-R",
                    "--files-only", remote_path(inputs.remote, root.remote_root),
                )
                if isinstance(result, Problem):
                    return StageResult(
                        "check", False, f"{root.name}: {result.problem}",
                        result.fix + " " + _missing_fix(root, inputs.models_lock),
                    ), missing_checked
                sizes = {entry.path: entry.size for entry in parse_remote_listing(result.stdout)}
                bad = tuple(file.path for file in missing if sizes.get(file.path) != file.size)
                if bad:
                    return StageResult(
                        "check", False,
                        f"{root.name}: missing remotely or wrong size: {', '.join(bad[:10])}",
                        _missing_fix(root, inputs.models_lock),
                    ), missing_checked
                missing_checked += len(missing)
        except (OSError, ValueError) as exc:
            return StageResult("check", False, f"{root.name}: archive check failed: {exc}", _retry_fix()), missing_checked
    count = sum(len(root.files) for root in selected.roots)
    return StageResult("check", True, f"checked {count} files ({missing_checked} by remote size)", ""), missing_checked


def _audit_stage(
    io: Host, rendered_dir: PathLike, selected: Inventory, stats: CopyStats,
    missing_checked: int, started: datetime, finished: datetime,
) -> StageResult:
    files = tuple(file for root in selected.roots for file in root.files)
    row = audit.AuditRow(
        str(uuid.uuid4()), AUDIT_KIND, None, None, None, (),
        {
            "lockfiles": selected.lockfiles,
            "snapshots": selected.snapshots,
            "models": selected.models,
            "absent_models": selected.absent_models,
            "files_sent": stats.files,
            "bytes_sent": stats.bytes,
            "files_checked": len(files),
            "bytes_total": sum(file.size for file in files),
            "missing_checked": missing_checked,
            "duration_s": max(0.0, (finished - started).total_seconds()),
        },
    )
    problem = audit.write_rows(io, rendered_dir, (row,))
    if problem is not None:
        return StageResult(
            "audit", False, f"archive push audit write failed: {problem}",
            stack.logs_fix(rendered_dir, "postgres"),
        )
    return StageResult("audit", True, "archive push recorded", "")


def _print_stage(stage: StageResult) -> None:
    if stage.name == "preconditions" and not stage.ok:
        print(report.stage_line(stage), file=sys.stderr)
    else:
        report.print_stage(stage)


def run_archive_push(
    args: object, *, host: ArchiveHost | None = None,
    rendered_dir: PathLike = _RENDERED_DIR, site_path: PathLike = _SITE_PATH,
    root: PathLike | None = None, snapshots_root: PathLike = SNAPSHOTS_ROOT,
    models_root: PathLike = MODELS_ROOT, config_path: PathLike = ARCHIVE_REMOTE_CONFIG,
    now: datetime | None = None, sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Push and verify the build box's pinned sources, then record their coverage."""

    io = host or RealHost()
    if io.geteuid() != 0:
        _print_stage(StageResult("preconditions", False, "root privileges are required",
                                 f"Run {report.command('archive push')} as root, then retry."))
        return 1
    started = aware_now(now)
    if started is None:
        _print_stage(StageResult("preconditions", False, "the clock must be timezone-aware",
                                 "Use an aware UTC time, then retry."))
        return 1
    claim = backuplock.claim(
        io, command=report.command_name("archive push"), now=started, lock=backuplock.ARCHIVE_LOCK,
    )
    if claim.refusal is not None:
        _print_stage(claim.refusal)
        return 1
    if not claim.taken:
        _print_stage(StageResult(
            "preconditions", False, "an archive push is already running in this process",
            f"Wait for it to finish, then run {report.command('archive push')} again.",
        ))
        return 1
    try:
        checkout = Path(root) if root is not None else Path(__file__).parents[2]
        try:
            stage, inputs = _preconditions(io, checkout, rendered_dir, site_path, config_path)
        except OSError as exc:
            stage, inputs = StageResult(
                "preconditions", False, f"archive prerequisites could not be read: {exc}", _retry_fix(),
            ), None
        _print_stage(stage)
        if not stage.ok or inputs is None:
            return 1
        stage = _probe_stage(io, inputs, sleep)
        _print_stage(stage)
        if not stage.ok:
            return 1
        stage, selected = _inventory_stage(io, inputs, checkout, snapshots_root, models_root)
        _print_stage(stage)
        if not stage.ok or selected is None:
            return 1
        stage, stats = _copy_stage(io, inputs, config_path, selected)
        _print_stage(stage)
        if not stage.ok or stats is None:
            return 1
        stage, missing_checked = _check_stage(io, inputs, config_path, selected)
        _print_stage(stage)
        if not stage.ok:
            return 1
        finished = started if now is not None else datetime.now(UTC)
        stage = _audit_stage(io, rendered_dir, selected, stats, missing_checked, started, finished)
        _print_stage(stage)
        return int(not stage.ok)
    finally:
        backuplock.release_claim(io, claim, lock=backuplock.ARCHIVE_LOCK)
