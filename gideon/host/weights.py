"""The weights tree: the hub-cache layout under /data/models and the pull record (§5.4, §2.4).

The layout helpers are pure; the pull record is the ``rollback.json``-shaped
journal (claim, current, previous, retiring) whose transitions every run of
``models pull`` walks in order.
"""

import os
import re
import stat as stat_module
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal, cast
from urllib.parse import quote, urlsplit

import yaml  # type: ignore[import-untyped]

from gideon.host.egress import EgressAllowlist, load_egress_allowlist
from gideon.host.egress import render_errors as render_egress_errors
from gideon.host.models import (
    GIGABYTE,
    HF_REPO,
    REVISION,
    ROLE_NAME,
    HardwareProfile,
    ModelPin,
    PinnedFile,
    load_models_lock,
    select_profile,
)
from gideon.host.models import (
    render_errors as render_models_errors,
)
from gideon.host.report import Problem, StageResult, print_stage, refusal
from gideon.host.site import SiteConfig, load_site
from gideon.host.site import render_errors as render_site_errors
from gideon.host.steps import Wgetrc, temporary_wgetrc, wget_argv_for_site
from gideon.host.sysio import CompletedText, Host, PathLike, RealHost

MODELS_ROOT: Final = Path("/data/models")
HUB_DIR_NAME: Final = "hub"
PULL_RECORD_PATH: Final = MODELS_ROOT / "gideon" / "pulls.yaml"

HF_RESOLVE_ROOT: Final = "https://huggingface.co"

# These are operational bounds for an unattended host command.  They are
# deliberately not product-tuning values; the egress probe carries the same
# stance for its fixed timeout.
WGET_TRIES: Final = 3
WGET_WAIT_SECONDS: Final = 5
WGET_CONNECT_TIMEOUT_SECONDS: Final = 30
WGET_READ_TIMEOUT_SECONDS: Final = 60

BLOB_MODE: Final = 0o444
DIRECTORY_MODE: Final = 0o755

_BARE_DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")
_RECORD_FIX: Final = (
    f"Repair or remove {PULL_RECORD_PATH} (a removed record forgets which sets "
    "are complete, so the next pull keeps every tree it finds), then re-run "
    "gideon models pull."
)
_BOOT_ID_PATH: Final = Path("/proc/sys/kernel/random/boot_id")


def bare_digest(digest: str) -> str:
    """Normalize a lock digest to lowercase bare hexadecimal."""

    value = digest.removeprefix("sha256:")
    if _BARE_DIGEST.fullmatch(value) is None:
        raise ValueError(f"invalid sha256 digest: {digest!r}")
    return value.lower()


def repository_folder(repo: str) -> str:
    """Return the Hugging Face hub folder name for ``owner/name``."""

    owner, separator, name = repo.partition("/")
    if not separator or not owner or not name:
        raise ValueError(f"invalid Hugging Face repository: {repo!r}")
    return f"models--{owner}--{name}"


def repository_dir(repo: str, root: PathLike = MODELS_ROOT) -> Path:
    return Path(root) / HUB_DIR_NAME / repository_folder(repo)


def blob_path(
    repo: str, digest: str, root: PathLike = MODELS_ROOT
) -> Path:
    return repository_dir(repo, root) / "blobs" / bare_digest(digest)


def snapshot_path(
    repo: str,
    revision: str,
    file_path: str,
    root: PathLike = MODELS_ROOT,
) -> Path:
    return repository_dir(repo, root) / "snapshots" / revision / Path(file_path)


def blob_link_target(file_path: str, digest: str) -> str:
    """Return the depth-aware relative target from a snapshot entry."""

    components = Path(file_path).parts
    if not components or any(component in ("", ".", "..") for component in components):
        raise ValueError(f"invalid model file path: {file_path!r}")
    return f"{'../' * (len(components) + 1)}blobs/{bare_digest(digest)}"


def refs_main_path(repo: str, root: PathLike = MODELS_ROOT) -> Path:
    return repository_dir(repo, root) / "refs" / "main"


def refs_main_content(revision: str) -> str:
    """The ref's content: the bare commit hash, no newline, as huggingface_hub writes and reads it."""

    return revision


def resolve_url(pin: ModelPin, file_path: str) -> str:
    """Build the pinned Hugging Face resolve URL for one file."""

    return (
        f"{HF_RESOLVE_ROOT}/{pin.repo}/resolve/{pin.revision}/"
        f"{quote(file_path, safe='/')}"
    )


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """The identity facts that distinguish a live process from a reused pid."""

    pid: int
    boot_id: str
    start_time: int


def _stat_start_time(text: str) -> int:
    """Read proc stat field 22, whose index is stable after the final ``)``."""

    closing = text.rfind(")")
    if closing < 0:
        raise ValueError("process stat has no closing command name")
    fields = text[closing + 1 :].split()
    # The first field after the command is field 3, so field 22 is offset 19.
    if len(fields) <= 19:
        raise ValueError("process stat has no start time field")
    try:
        return int(fields[19])
    except ValueError as exc:
        raise ValueError("process stat start time is not an integer") from exc


def process_identity(host: Host) -> ProcessIdentity:
    """Read this process's boot id and start ticks through ``Host``."""

    pid = os.getpid()
    boot_id = host.read_text(_BOOT_ID_PATH).strip()
    start_time = _stat_start_time(host.read_text(Path(f"/proc/{pid}/stat")))
    if not boot_id:
        raise ValueError("kernel boot id is empty")
    return ProcessIdentity(pid, boot_id, start_time)


def process_is_alive(host: Host, identity: ProcessIdentity) -> bool:
    """Judge liveness by boot id and start ticks, never by pid alone."""

    try:
        boot_id = host.read_text(_BOOT_ID_PATH).strip()
        if boot_id != identity.boot_id:
            return False
        start_time = _stat_start_time(
            host.read_text(Path(f"/proc/{identity.pid}/stat"))
        )
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        return False
    return start_time == identity.start_time


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    """One model repository revision named by a pull record."""

    role: str
    repo: str
    revision: str


type ModelSet = tuple[SnapshotRef, ...]


@dataclass(frozen=True, slots=True)
class PullEntry:
    """A claimed or completed pull of one profile's model set."""

    profile: str
    models: ModelSet
    started: str
    completed: str | None
    identity: ProcessIdentity


@dataclass(frozen=True, slots=True)
class PullRecord:
    """The four-slot journal for a model pull."""

    in_progress: PullEntry | None = None
    current: PullEntry | None = None
    previous: PullEntry | None = None
    retiring: ModelSet = ()


class _PullRecordLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate record fields."""


def _construct_mapping_without_duplicates(
    loader: _PullRecordLoader, node: Any, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.YAMLError(f"duplicate mapping key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_PullRecordLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_without_duplicates,
)


def snapshot_refs(models: Iterable[ModelPin]) -> ModelSet:
    """Make the record's model set from a lock profile's models in order."""

    return tuple(SnapshotRef(model.role, model.repo, model.revision) for model in models)


def new_pull_entry(
    profile: str,
    models: Iterable[ModelPin],
    identity: ProcessIdentity,
    started: str,
) -> PullEntry:
    return PullEntry(profile, snapshot_refs(models), started, None, identity)


def complete_pull_entry(entry: PullEntry, completed: str) -> PullEntry:
    return replace(entry, completed=completed)


def _entry_refs(entry: PullEntry | None) -> ModelSet:
    return () if entry is None else entry.models


def _unique(refs: Iterable[SnapshotRef]) -> ModelSet:
    return tuple(dict.fromkeys(refs))


def retiring_references(
    displaced: Iterable[SnapshotRef],
    named: Iterable[SnapshotRef],
    existing: Iterable[SnapshotRef] = (),
) -> ModelSet:
    """Apply the one retiring rule used by every record transition."""

    named_set = set(named)
    return _unique(
        ref for ref in (*existing, *displaced) if ref not in named_set
    )


def claim(
    record: PullRecord,
    entry: PullEntry,
    *,
    alive: bool = False,
) -> PullRecord | Problem:
    """Install a claim, journaling references displaced by a dead claim."""

    if record.in_progress is not None and alive:
        return Problem(
            f"a pull begun {record.in_progress.started} by pid "
            f"{record.in_progress.identity.pid} is running; wait for it",
            "Wait for the running models pull to finish, then re-run models pull.",
        )
    named = (*_entry_refs(record.current), *_entry_refs(record.previous), *entry.models)
    displaced = _entry_refs(record.in_progress)
    retiring = retiring_references(displaced, named, record.retiring)
    return replace(record, in_progress=entry, retiring=retiring)


def clear_retiring(record: PullRecord) -> PullRecord:
    return replace(record, retiring=())


def rotate(record: PullRecord, completed: str) -> PullRecord | Problem:
    """Promote the claim, completed at *completed*, and journal what leaves the window."""

    if record.in_progress is None:
        return Problem(
            "cannot rotate a pull record without an in-progress claim",
            "Re-run models pull so it can establish a claim first.",
        )
    new_current = complete_pull_entry(record.in_progress, completed)
    old_current = record.current
    old_previous = record.previous
    new_previous: PullEntry | None
    if old_current is not None and old_current.models != new_current.models:
        new_previous = old_current
        displaced = _entry_refs(old_previous)
    else:
        new_previous = old_previous
        displaced = ()
    named = (*_entry_refs(new_current), *_entry_refs(new_previous))
    retiring = retiring_references(displaced, named, record.retiring)
    return replace(
        record,
        current=new_current,
        previous=new_previous,
        retiring=retiring,
    )


def release(record: PullRecord) -> PullRecord:
    """Finish the sequence by clearing the claim and retiring journal."""

    return replace(record, in_progress=None, retiring=())


def _ref_document(ref: SnapshotRef) -> dict[str, object]:
    return {"role": ref.role, "repo": ref.repo, "revision": ref.revision}


def _entry_document(entry: PullEntry | None) -> dict[str, object] | None:
    if entry is None:
        return None
    return {
        "profile": entry.profile,
        "models": [_ref_document(ref) for ref in entry.models],
        "started": entry.started,
        "completed": entry.completed,
        "process": {
            "pid": entry.identity.pid,
            "boot_id": entry.identity.boot_id,
            "start_time": entry.identity.start_time,
        },
    }


def pull_record_document(record: PullRecord) -> dict[str, object]:
    return {
        "in_progress": _entry_document(record.in_progress),
        "current": _entry_document(record.current),
        "previous": _entry_document(record.previous),
        "retiring": [_ref_document(ref) for ref in record.retiring],
    }


def dump_pull_record(record: PullRecord) -> str:
    """Serialize the whole record as deterministic, ordinary YAML."""

    return yaml.safe_dump(
        pull_record_document(record), default_flow_style=False, sort_keys=False
    )


def _malformed(detail: str) -> Problem:
    return Problem(f"{PULL_RECORD_PATH} is malformed: {detail}", _RECORD_FIX)


def _mapping(value: object, field: str) -> Mapping[str, object] | Problem:
    if not isinstance(value, Mapping):
        return _malformed(f"{field} must be a mapping")
    return value


def _ref(value: object, field: str) -> SnapshotRef | Problem:
    mapping = _mapping(value, field)
    if isinstance(mapping, Problem):
        return mapping
    if set(mapping) != {"role", "repo", "revision"}:
        return _malformed(f"{field} must contain role, repo, and revision")
    role, repo, revision = mapping["role"], mapping["repo"], mapping["revision"]
    if not all(isinstance(item, str) and item for item in (role, repo, revision)):
        return _malformed(f"{field} has a non-string field")
    # The record's values name directories the prune removes whole: they are
    # held to the lock's own grammars, never interpolated as found.
    if ROLE_NAME.fullmatch(cast(str, role)) is None:
        return _malformed(f"{field}.role is not a model role name")
    if HF_REPO.fullmatch(cast(str, repo)) is None:
        return _malformed(f"{field}.repo is not a Hugging Face repository")
    if REVISION.fullmatch(cast(str, revision)) is None:
        return _malformed(f"{field}.revision is not a commit hash")
    return SnapshotRef(cast(str, role), cast(str, repo), cast(str, revision))


def _entry(
    value: object, field: str, *, require_completed: bool = False
) -> PullEntry | None | Problem:
    if value is None:
        return None
    mapping = _mapping(value, field)
    if isinstance(mapping, Problem):
        return mapping
    expected = {"profile", "models", "started", "completed", "process"}
    if set(mapping) != expected:
        return _malformed(f"{field} must contain exactly {', '.join(sorted(expected))}")
    profile, models_value = mapping["profile"], mapping["models"]
    started, completed = mapping["started"], mapping["completed"]
    if not isinstance(profile, str) or not profile:
        return _malformed(f"{field}.profile must be a non-empty string")
    if not isinstance(models_value, list):
        return _malformed(f"{field}.models must be a list")
    refs: list[SnapshotRef] = []
    for index, model_value in enumerate(models_value):
        ref = _ref(model_value, f"{field}.models[{index}]")
        if isinstance(ref, Problem):
            return ref
        refs.append(ref)
    if not isinstance(started, str) or not started:
        return _malformed(f"{field}.started must be a non-empty string")
    if completed is not None and (not isinstance(completed, str) or not completed):
        return _malformed(f"{field}.completed must be a string or null")
    if require_completed and completed is None:
        return _malformed(f"{field}.completed must be a completion time")
    process = _mapping(mapping["process"], f"{field}.process")
    if isinstance(process, Problem):
        return process
    if set(process) != {"pid", "boot_id", "start_time"}:
        return _malformed(f"{field}.process has the wrong fields")
    pid, boot_id, start_time = (
        process["pid"], process["boot_id"], process["start_time"]
    )
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return _malformed(f"{field}.process.pid must be a positive integer")
    if not isinstance(boot_id, str) or not boot_id:
        return _malformed(f"{field}.process.boot_id must be a non-empty string")
    if (
        not isinstance(start_time, int)
        or isinstance(start_time, bool)
        or start_time < 0
    ):
        return _malformed(f"{field}.process.start_time must be a non-negative integer")
    return PullEntry(
        profile,
        tuple(refs),
        started,
        completed,
        ProcessIdentity(pid, boot_id, start_time),
    )


def parse_pull_record(text: str) -> PullRecord | Problem:
    """Parse and validate every pull-record field without coercion."""

    try:
        document = yaml.load(text, Loader=_PullRecordLoader)
    except yaml.YAMLError as exc:
        return _malformed(f"YAML parse error: {exc}")
    mapping = _mapping(document, "record")
    if isinstance(mapping, Problem):
        return mapping
    expected = {"in_progress", "current", "previous", "retiring"}
    if set(mapping) != expected:
        return _malformed("record must contain exactly in_progress, current, previous, and retiring")
    entries: list[PullEntry | None] = []
    for field in ("in_progress", "current", "previous"):
        entry = _entry(
            mapping[field], field, require_completed=field in {"current", "previous"}
        )
        if isinstance(entry, Problem):
            return entry
        entries.append(entry)
    retiring_value = mapping["retiring"]
    if not isinstance(retiring_value, list):
        return _malformed("retiring must be a list")
    retiring: list[SnapshotRef] = []
    for index, value in enumerate(retiring_value):
        ref = _ref(value, f"retiring[{index}]")
        if isinstance(ref, Problem):
            return ref
        retiring.append(ref)
    return PullRecord(entries[0], entries[1], entries[2], tuple(retiring))


def load_pull_record(
    host: Host, path: PathLike = PULL_RECORD_PATH
) -> PullRecord | Problem:
    """Load the record through ``Host``; an absent record starts empty."""

    try:
        text = host.read_text(path)
    except FileNotFoundError:
        return PullRecord()
    except (OSError, UnicodeError) as exc:
        return Problem(f"cannot read {path}: {exc}", _RECORD_FIX)
    return parse_pull_record(text)


def save_pull_record(
    host: Host, record: PullRecord, path: PathLike = PULL_RECORD_PATH
) -> Problem | None:
    """Write the complete record through the Host's atomic write seam."""

    try:
        host.write_text(path, dump_pull_record(record))
    except OSError as exc:
        return Problem(f"cannot write {path}: {exc}", _RECORD_FIX)
    return None


class FileState(StrEnum):
    """The on-disk state of one lock-named snapshot file."""

    PRESENT = "present"
    ABSENT = "absent"
    PARTIAL = "partial"
    UNLINKED = "unlinked"
    MISMATCHED = "mismatched"
    FOREIGN = "foreign"


FileAction = Literal["verified", "fetched", "resumed", "refetched", "relinked"]
ModelKind = Literal["fetched", "present", "failed"]


@dataclass(frozen=True, slots=True)
class FileOutcome:
    """The observable result for one model file."""

    path: str
    action: FileAction
    bytes: int


@dataclass(frozen=True, slots=True)
class ModelOutcome:
    """The observable result for one model convergence."""

    role: str
    kind: ModelKind
    files: tuple[FileOutcome, ...]
    hosts: tuple[str, ...] = ()
    problem: str = ""
    fix: str = ""
    seconds: float = 0.0


_FOREIGN_FIX: Final = (
    "Remove {path}, then re-run gideon models pull; the engine loads every "
    "file in the snapshot."
)
_REPUBLISH_FIX: Final = (
    "The pinned upstream is gone or changed; report it to TNMD; the source "
    "archive is republished and models.lock repinned in the next patch release "
    "(§2.4)."
)
_WGET_FIX: Final = "Install wget with sudo apt-get install wget, then re-run gideon models pull."
_FREE_SPACE_FIX: Final = "Free space on /data, then re-run gideon models pull."
_TOOL_FIX: Final = (
    "Install coreutils (sha256sum, mv, ln, readlink, rm), then re-run "
    "gideon models pull."
)
_ROOT_FIX: Final = "Run sudo python3 -m gideon models pull."
_SITE_PATH: Final = Path("/etc/gideon/site.yaml")


def _format_gb(byte_count: int) -> str:
    return f"{byte_count / GIGABYTE:.1f} GB"


def _format_size(byte_count: int) -> str:
    """A file size for the per-file lines: GB, MB, or KB by magnitude, one decimal."""

    if byte_count >= GIGABYTE:
        return _format_gb(byte_count)
    if byte_count >= 1_000_000:
        return f"{byte_count / 1_000_000:.1f} MB"
    return f"{byte_count / 1_000:.1f} KB"


def _tool_failure(argv: Sequence[str], result: CompletedText) -> str:
    detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    return f"{argv[0]} failed: {detail}"


def _direct_entries(io: Host, directory: Path) -> tuple[str, ...]:
    try:
        return tuple(io.listdir(directory))
    except FileNotFoundError:
        return ()


def _snapshot_entries(io: Host, directory: Path, prefix: str = "") -> set[str]:
    entries: set[str] = set()
    current = directory / prefix
    for name in _direct_entries(io, current):
        relative = f"{prefix}/{name}" if prefix else name
        path = directory / relative
        try:
            is_directory = stat_module.S_ISDIR(io.stat(path).st_mode)
        except OSError:
            is_directory = False
        if is_directory:
            entries.update(_snapshot_entries(io, directory, relative))
        else:
            entries.add(relative)
    return entries


def _readlink(io: Host, path: Path) -> str | None | Problem:
    result = io.run(["readlink", str(path)])
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        return Problem(_tool_failure(["readlink", str(path)], result), _TOOL_FIX)
    target = result.stdout.strip()
    return target or None


def _verification_states(
    io: Host,
    pin: ModelPin,
    states: dict[str, FileState],
    root: PathLike,
) -> Problem | None:
    """Verify every reusable blob in one ``sha256sum -c -`` pass; a blob without an ``OK`` is mismatched.

    The tool's exit code is 1 whenever any line failed, so the verdict is the
    per-path stdout line; any other non-zero exit means the tool itself did not
    run, which is a refusal, never a re-download of every blob.
    """

    candidates = [
        model_file
        for model_file in pin.files
        if states.get(model_file.path) in (FileState.PRESENT, FileState.UNLINKED)
    ]
    if not candidates:
        return None
    lines = "".join(
        f"{bare_digest(model_file.sha256)}  {blob_path(pin.repo, model_file.sha256, root)}\n"
        for model_file in candidates
    )
    argv = ["sha256sum", "-c", "-"]
    result = io.run(argv, input=lines)
    if result.returncode not in (0, 1):
        return Problem(_tool_failure(argv, result), _TOOL_FIX)
    verified = {
        path
        for path, separator, verdict in (
            line.rpartition(": ") for line in result.stdout.splitlines()
        )
        if separator and verdict == "OK"
    }
    for model_file in candidates:
        if str(blob_path(pin.repo, model_file.sha256, root)) not in verified:
            states[model_file.path] = FileState.MISMATCHED
    return None


def _file_blob_size(io: Host, path: Path) -> int | None:
    if not io.exists(path):
        return None
    try:
        return io.stat(path).st_size
    except OSError:
        return None


def _partial_path(pin: ModelPin, model_file: PinnedFile, root: PathLike) -> Path:
    return blob_path(pin.repo, model_file.sha256, root).with_name(
        f"{bare_digest(model_file.sha256)}.partial"
    )


def classify_model(io: Host, pin: ModelPin, root: PathLike) -> dict[str, FileState] | Problem:
    """Classify every lock file and verify reusable blobs in one pass."""

    snapshot = repository_dir(pin.repo, root) / "snapshots" / pin.revision
    snapshot_entries = _snapshot_entries(io, snapshot) if io.exists(snapshot) else set()
    states: dict[str, FileState] = {}
    for relative in sorted(snapshot_entries - {model_file.path for model_file in pin.files}):
        states[relative] = FileState.FOREIGN

    for model_file in pin.files:
        blob = blob_path(pin.repo, model_file.sha256, root)
        if io.exists(_partial_path(pin, model_file, root)):
            states[model_file.path] = FileState.PARTIAL
            continue
        blob_size = _file_blob_size(io, blob)
        if blob_size is None:
            states[model_file.path] = FileState.ABSENT
            continue
        if blob_size != model_file.size:
            states[model_file.path] = FileState.MISMATCHED
            continue
        link = snapshot / model_file.path
        target = _readlink(io, link)
        if isinstance(target, Problem):
            return target
        states[model_file.path] = (
            FileState.PRESENT
            if target == blob_link_target(model_file.path, model_file.sha256)
            else FileState.UNLINKED
        )

    problem = _verification_states(io, pin, states, root)
    return states if problem is None else problem


def _df_available(io: Host) -> int | None:
    result = io.run(["df", "-B1", "--output=avail", "/data"])
    if result.returncode != 0:
        return None
    fields = [field for field in result.stdout.split() if field.isdigit()]
    return int(fields[-1]) if fields else None


def _shortfall(
    io: Host, pin: ModelPin, states: Mapping[str, FileState], root: PathLike, available: int
) -> int:
    """The bytes /data lacks for this model, walking the fetch order with its frees.

    A mismatched blob is removed just before its own fetch, so its bytes count
    as freed from that point; a partial's bytes are already on disk.
    """

    balance = available
    lowest = balance
    for model_file in pin.files:
        state = states.get(model_file.path)
        if state is FileState.MISMATCHED:
            balance += _file_blob_size(io, blob_path(pin.repo, model_file.sha256, root)) or 0
            balance -= model_file.size
        elif state is FileState.ABSENT:
            balance -= model_file.size
        elif state is FileState.PARTIAL:
            partial_size = _file_blob_size(io, _partial_path(pin, model_file, root)) or 0
            balance -= max(0, model_file.size - partial_size)
        lowest = min(lowest, balance)
    return max(0, -lowest)


def _model_outcome(
    pin: ModelPin,
    started: float,
    *,
    kind: ModelKind,
    files: Iterable[FileOutcome] = (),
    hosts: Iterable[str] = (),
    problem: str = "",
    fix: str = "",
    clock: Callable[[], float],
) -> ModelOutcome:
    return ModelOutcome(
        pin.role,
        kind,
        tuple(files),
        tuple(sorted(set(hosts))),
        problem,
        fix,
        clock() - started,
    )


def _response_hosts(stderr: str) -> tuple[str, ...]:
    """The hosts of every ``Location:`` header in ``wget -S`` output, in redirect order."""

    hosts: dict[str, None] = {}
    for raw in stderr.splitlines():
        line = raw.strip()  # wget -S indents every header line
        if line.lower().startswith("location:"):
            location = line.split(":", 1)[1].strip()
            parsed = urlsplit(location)
            if parsed.hostname:
                hosts[parsed.hostname] = None
    return tuple(hosts)


def _last_http_status(stderr: str) -> int | None:
    statuses = re.findall(r"HTTP/[\d.]+\s+(\d{3})", stderr)
    return int(statuses[-1]) if statuses else None


def _fetch_failure(
    result: CompletedText,
    url: str,
    hosts: Sequence[str],
) -> tuple[str, str]:
    """The problem and fix for a failed fetch, named after the last host answered."""

    status = _last_http_status(result.stderr)
    host = hosts[-1] if hosts else (urlsplit(url).hostname or "huggingface.co")
    if status in {401, 403, 404}:
        return (
            f"pinned URL returned HTTP {status} from {host}",
            _REPUBLISH_FIX,
        )
    return (
        f"could not fetch pinned URL from {host}"
        + (f" (HTTP {status})" if status is not None else ""),
        (
            f"Allow {host} (the install-upgrade group of config/egress.yaml) "
            "through the firewall or set egress_proxy in /etc/gideon/site.yaml, "
            "then re-run gideon models pull."
        ),
    )


def _digest_from_output(result: CompletedText) -> str | None:
    match = re.match(r"^([0-9a-fA-F]{64})\s+", result.stdout.strip())
    return match.group(1).lower() if match else None


def _link(io: Host, model_file: PinnedFile, link: Path) -> str | None:
    """Point the snapshot entry at its blob; the problem text when ``ln`` fails."""

    io.mkdir(link.parent, mode=DIRECTORY_MODE, parents=True, exist_ok=True)
    argv = ["ln", "-sfn", blob_link_target(model_file.path, model_file.sha256), str(link)]
    result = io.run(argv)
    return None if result.returncode == 0 else _tool_failure(argv, result)


def converge_model(
    io: Host,
    site: SiteConfig,
    pin: ModelPin,
    wgetrc: Wgetrc,
    *,
    root: PathLike,
    clock: Callable[[], float] = time.monotonic,
) -> ModelOutcome:
    """Converge one model's files into the pinned hub-cache snapshot."""

    started = clock()
    if not wgetrc.ok:
        return _model_outcome(
            pin,
            started,
            kind="failed",
            problem=wgetrc.problem or "proxy credentials unavailable",
            fix=wgetrc.fix,
            clock=clock,
        )

    states = classify_model(io, pin, root)
    if isinstance(states, Problem):
        return _model_outcome(
            pin, started, kind="failed", problem=states.problem, fix=states.fix, clock=clock
        )
    foreign = next(
        (path for path, state in states.items() if state is FileState.FOREIGN), None
    )
    if foreign is not None:
        return _model_outcome(
            pin,
            started,
            kind="failed",
            problem=f"snapshot contains foreign file {foreign}",
            fix=_FOREIGN_FIX.format(path=foreign),
            clock=clock,
        )

    repository = repository_dir(pin.repo, root)
    directories = {
        repository / "blobs",
        repository / "snapshots" / pin.revision,
        repository / "refs",
    }
    directories.update(
        snapshot_path(pin.repo, pin.revision, model_file.path, root).parent
        for model_file in pin.files
    )
    for directory in sorted(directories, key=str):
        io.mkdir(directory, mode=DIRECTORY_MODE, parents=True, exist_ok=True)

    available = _df_available(io)
    if available is None:
        return _model_outcome(
            pin,
            started,
            kind="failed",
            problem="could not measure free space on /data",
            fix=_FREE_SPACE_FIX,
            clock=clock,
        )
    shortfall = _shortfall(io, pin, states, root, available)
    if shortfall:
        return _model_outcome(
            pin,
            started,
            kind="failed",
            problem=f"/data is short by {_format_gb(shortfall)} for this model",
            fix=_FREE_SPACE_FIX,
            clock=clock,
        )

    outcomes: list[FileOutcome] = []
    hosts: set[str] = set()
    for model_file in pin.files:
        state = states[model_file.path]
        blob = blob_path(pin.repo, model_file.sha256, root)
        link = snapshot_path(pin.repo, pin.revision, model_file.path, root)
        if state is FileState.PRESENT:
            outcomes.append(FileOutcome(model_file.path, "verified", model_file.size))
            continue
        if state is FileState.UNLINKED:
            failure = _link(io, model_file, link)
            if failure is not None:
                return _model_outcome(
                    pin, started, kind="failed", files=outcomes, hosts=hosts,
                    problem=failure, fix=_TOOL_FIX, clock=clock,
                )
            print(f"  relinked {model_file.path}")
            outcomes.append(FileOutcome(model_file.path, "relinked", model_file.size))
            continue

        if state is FileState.MISMATCHED:
            io.unlink(blob, missing_ok=True)
            io.unlink(link, missing_ok=True)
        partial = _partial_path(pin, model_file, root)
        prior_bytes = _file_blob_size(io, partial) or 0
        if state is FileState.PARTIAL and prior_bytes > model_file.size:
            io.unlink(partial, missing_ok=True)
            return _model_outcome(
                pin,
                started,
                kind="failed",
                files=outcomes,
                hosts=hosts,
                problem=f"partial {model_file.path} is {prior_bytes} bytes; lock requires {model_file.size} bytes",
                fix=_REPUBLISH_FIX,
                clock=clock,
            )

        url = resolve_url(pin, model_file.path)
        argv = wget_argv_for_site(site, str(partial), url)
        argv[1:1] = [
            "-c",
            "-S",
            f"--tries={WGET_TRIES}",
            f"--waitretry={WGET_WAIT_SECONDS}",
            "--retry-connrefused",
            f"--connect-timeout={WGET_CONNECT_TIMEOUT_SECONDS}",
            f"--read-timeout={WGET_READ_TIMEOUT_SECONDS}",
        ]
        result = io.run(wgetrc.prefix(argv))
        response_hosts = _response_hosts(result.stderr)
        hosts.update(response_hosts)
        hosts.add(urlsplit(url).hostname or "huggingface.co")
        if result.returncode == 127:
            return _model_outcome(
                pin,
                started,
                kind="failed",
                files=outcomes,
                hosts=hosts,
                problem="wget is missing",
                fix=_WGET_FIX,
                clock=clock,
            )
        if result.returncode != 0:
            problem, fix = _fetch_failure(result, url, response_hosts)
            return _model_outcome(
                pin,
                started,
                kind="failed",
                files=outcomes,
                hosts=hosts,
                problem=problem,
                fix=fix,
                clock=clock,
            )

        try:
            fetched_size = io.stat(partial).st_size
        except OSError:
            fetched_size = -1
        if fetched_size != model_file.size:
            io.unlink(partial, missing_ok=True)
            return _model_outcome(
                pin,
                started,
                kind="failed",
                files=outcomes,
                hosts=hosts,
                problem=f"fetched {model_file.path} is {fetched_size} bytes; lock requires {model_file.size} bytes",
                fix=_REPUBLISH_FIX,
                clock=clock,
            )
        digest_result = io.run(["sha256sum", str(partial)])
        actual_digest = _digest_from_output(digest_result)
        expected_digest = bare_digest(model_file.sha256)
        if digest_result.returncode not in (0, 1):
            return _model_outcome(
                pin, started, kind="failed", files=outcomes, hosts=hosts,
                problem=_tool_failure(["sha256sum", str(partial)], digest_result),
                fix=_TOOL_FIX, clock=clock,
            )
        if digest_result.returncode != 0 or actual_digest != expected_digest:
            io.unlink(partial, missing_ok=True)
            actual = actual_digest or "unavailable"
            return _model_outcome(
                pin,
                started,
                kind="failed",
                files=outcomes,
                hosts=hosts,
                problem=f"fetched {model_file.path} has digest {actual}; lock requires {expected_digest}",
                fix=_REPUBLISH_FIX,
                clock=clock,
            )
        move = ["mv", "-f", str(partial), str(blob)]
        moved = io.run(move)
        if moved.returncode != 0:
            return _model_outcome(
                pin, started, kind="failed", files=outcomes, hosts=hosts,
                problem=_tool_failure(move, moved), fix=_TOOL_FIX, clock=clock,
            )
        io.chmod(blob, BLOB_MODE)
        failure = _link(io, model_file, link)
        if failure is not None:
            return _model_outcome(
                pin, started, kind="failed", files=outcomes, hosts=hosts,
                problem=failure, fix=_TOOL_FIX, clock=clock,
            )
        action: FileAction = "resumed" if state is FileState.PARTIAL else (
            "refetched" if state is FileState.MISMATCHED else "fetched"
        )
        size = _format_size(model_file.size)
        if action == "resumed":
            print(f"  resumed {model_file.path} from {_format_size(prior_bytes)} ({size})")
        elif action == "refetched":
            print(f"  refetched {model_file.path}: on-disk digest mismatched the lock ({size})")
        else:
            print(f"  fetched {model_file.path} ({size})")
        outcomes.append(FileOutcome(model_file.path, action, model_file.size))

    io.write_text(refs_main_path(pin.repo, root), refs_main_content(pin.revision))
    kind: ModelKind = (
        "fetched"
        if any(outcome.action in {"fetched", "resumed", "refetched"} for outcome in outcomes)
        else "present"
    )
    return _model_outcome(pin, started, kind=kind, files=outcomes, hosts=hosts, clock=clock)


def model_row(outcome: ModelOutcome) -> StageResult:
    """Render one model outcome as the shared operator-facing stage row."""

    if outcome.kind == "failed":
        return StageResult(outcome.role, False, outcome.problem, outcome.fix)
    verified = sum(
        file.action in {"verified", "relinked"} for file in outcome.files
    )
    if outcome.kind == "present":
        total_bytes = sum(file.bytes for file in outcome.files)
        detail = f"present: {verified} files verified, {_format_gb(total_bytes)}"
    else:
        fetched = sum(
            file.action in {"fetched", "resumed", "refetched"}
            for file in outcome.files
        )
        total_bytes = sum(
            file.bytes
            for file in outcome.files
            if file.action in {"fetched", "resumed", "refetched"}
        )
        via = ", ".join(outcome.hosts) or "none"
        detail = (
            f"fetched {fetched} file(s), {_format_gb(total_bytes)}; "
            f"{verified} present; via {via}; {outcome.seconds:.1f}s"
        )
    return StageResult(outcome.role, True, detail, "")


@dataclass(frozen=True, slots=True)
class PullOutcome:
    """The model results and refusal state for one complete pull attempt."""

    models: tuple[ModelOutcome, ...]
    uncovered: tuple[str, ...]
    ok: bool
    problem: str = ""
    fix: str = ""


def _contained(child: Path, parent: Path) -> bool:
    """True when *child* is lexically below *parent* — the guard before any ``rm -rf``."""

    return os.path.normpath(child).startswith(os.path.normpath(parent) + os.sep)


def _rm_tree(io: Host, path: Path) -> Problem | None:
    argv = ["rm", "-rf", str(path)]
    result = io.run(argv)
    return None if result.returncode == 0 else Problem(_tool_failure(argv, result), _TOOL_FIX)


def _snapshot_directories(io: Host, repository: Path) -> tuple[Path, ...] | Problem:
    snapshots = repository / "snapshots"
    try:
        names = io.listdir(snapshots)
    except FileNotFoundError:
        return ()
    except OSError as exc:
        return Problem(f"cannot list {snapshots}: {exc}", _TOOL_FIX)
    directories: list[Path] = []
    for name in names:
        path = snapshots / name
        try:
            is_directory = stat_module.S_ISDIR(io.stat(path).st_mode)
        except OSError as exc:
            return Problem(f"cannot inspect {path}: {exc}", _TOOL_FIX)
        if is_directory:
            directories.append(path)
    return tuple(directories)


def _reachable_blobs(io: Host, snapshots: Iterable[Path]) -> set[str] | Problem:
    reachable: set[str] = set()
    for snapshot in snapshots:
        for relative in _snapshot_entries(io, snapshot):
            link = snapshot / relative
            target = _readlink(io, link)
            if isinstance(target, Problem):
                return target
            if target is not None:
                reachable.add(os.path.normpath(link.parent / target))
    return reachable


def prune(
    io: Host, record: PullRecord, root: PathLike, *, keep: frozenset[str] = frozenset()
) -> list[str] | Problem:
    """Remove retiring snapshots, then blobs no snapshot on disk references.

    ``keep`` names bare digests never removed — the running claim's own files,
    so an interrupted download's ``.partial`` survives a prune and resumes.
    """

    lines: list[str] = []
    repositories = {
        repository_dir(reference.repo, root) for reference in record.retiring
    }
    for reference in _unique(record.retiring):
        snapshots_dir = repository_dir(reference.repo, root) / "snapshots"
        snapshot = snapshots_dir / reference.revision
        if not _contained(snapshot, snapshots_dir):
            return Problem(
                f"refusing to remove {snapshot}: outside {snapshots_dir}", _RECORD_FIX
            )
        if not io.exists(snapshot):
            continue
        failure = _rm_tree(io, snapshot)
        if failure is not None:
            return failure
        line = f"  removed {snapshot}"
        lines.append(line)
        print(line)

    for repository in sorted(repositories, key=str):
        snapshots = _snapshot_directories(io, repository)
        if isinstance(snapshots, Problem):
            return snapshots
        if not snapshots:
            failure = _rm_tree(io, repository)
            if failure is not None:
                return failure
            line = f"  removed {repository}"
            lines.append(line)
            print(line)
            continue
        reachable = _reachable_blobs(io, snapshots)
        if isinstance(reachable, Problem):
            return reachable
        blobs = repository / "blobs"
        try:
            blob_names = io.listdir(blobs)
        except FileNotFoundError:
            blob_names = []
        except OSError as exc:
            return Problem(f"cannot list {blobs}: {exc}", _TOOL_FIX)
        for name in blob_names:
            blob = blobs / name
            if name.removesuffix(".partial") in keep or str(blob) in reachable:
                continue
            try:
                io.unlink(blob)
            except OSError as exc:
                return Problem(f"cannot remove {blob}: {exc}", _TOOL_FIX)
            line = f"  removed {blob}"
            lines.append(line)
            print(line)
    return lines


def kept_lines(record: PullRecord) -> list[str]:
    """Return operator lines for previous snapshots still kept for rollback."""

    current = set(_entry_refs(record.current))
    return [
        f"  kept snapshot {reference.revision} of {reference.repo} "
        "(the previous set, for rollback)"
        for reference in _entry_refs(record.previous)
        if reference not in current
    ]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _pull_outcome(
    models: Iterable[ModelOutcome],
    uncovered: Iterable[str],
    *,
    ok: bool,
    problem: str = "",
    fix: str = "",
) -> PullOutcome:
    return PullOutcome(tuple(models), tuple(sorted(set(uncovered))), ok, problem, fix)


def _problem_outcome(
    problem: Problem, models: Iterable[ModelOutcome] = (), uncovered: Iterable[str] = ()
) -> PullOutcome:
    return _pull_outcome(models, uncovered, ok=False, problem=problem.problem, fix=problem.fix)


def pull_profile(
    io: Host,
    site: SiteConfig,
    profile: HardwareProfile,
    allowlist: EgressAllowlist,
    *,
    models_root: PathLike = MODELS_ROOT,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], str] = _now_iso,
) -> PullOutcome:
    """Run the five journalled steps for one selected hardware profile."""

    root = Path(models_root)
    record_path = root / "gideon" / "pulls.yaml"
    try:
        io.mkdir(root / "gideon", parents=True, exist_ok=True)
    except OSError as exc:
        return _problem_outcome(
            Problem(f"cannot create {root / 'gideon'}: {exc}", "Make /data writable, then re-run gideon models pull.")
        )
    loaded = load_pull_record(io, record_path)
    if isinstance(loaded, Problem):
        return _problem_outcome(loaded)
    record = loaded
    alive = (
        record.in_progress is not None
        and process_is_alive(io, record.in_progress.identity)
    )
    try:
        identity = process_identity(io)
    except (OSError, UnicodeError, ValueError) as exc:
        return _problem_outcome(
            Problem(f"cannot establish the pull process identity: {exc}", _TOOL_FIX)
        )
    claimed = claim(
        record,
        new_pull_entry(profile.name, profile.models, identity, now()),
        alive=alive,
    )
    if isinstance(claimed, Problem):
        return _problem_outcome(claimed)
    record = claimed
    saved = save_pull_record(io, record, record_path)
    if saved is not None:
        return _problem_outcome(saved)

    keep = frozenset(
        bare_digest(model_file.sha256) for pin in profile.models for model_file in pin.files
    )
    removed = prune(io, record, root, keep=keep)
    if isinstance(removed, Problem):
        return _problem_outcome(removed)
    record = clear_retiring(record)
    saved = save_pull_record(io, record, record_path)
    if saved is not None:
        return _problem_outcome(saved)
    for line in kept_lines(record):
        print(line)

    allowed_group = allowlist.group("install-upgrade")
    allowed = set() if allowed_group is None else {entry.host for entry in allowed_group.hosts}
    model_outcomes: list[ModelOutcome] = []
    uncovered: set[str] = set()
    with temporary_wgetrc(io, command="gideon models pull") as wgetrc:
        if not wgetrc.ok:
            return _problem_outcome(Problem(wgetrc.problem or "proxy credentials unavailable", wgetrc.fix))
        for pin in profile.models:
            outcome = converge_model(io, site, pin, wgetrc, root=root, clock=clock)
            model_outcomes.append(outcome)
            unknown = tuple(host for host in outcome.hosts if host not in allowed)
            if unknown:
                uncovered.update(unknown)
                for host in unknown:
                    print(
                        f"  uncovered host {host}: add it to the install-upgrade "
                        "group of config/egress.yaml"
                    )
            row = model_row(outcome)
            if unknown:
                row = replace(row, detail=f"{row.detail}; uncovered: {', '.join(unknown)}")
            print_stage(row)
            if outcome.kind == "failed":
                return _pull_outcome(
                    model_outcomes, uncovered, ok=False,
                    problem=outcome.problem, fix=outcome.fix,
                )

    rotated = rotate(record, now())
    if isinstance(rotated, Problem):
        return _problem_outcome(rotated, model_outcomes, uncovered)
    saved = save_pull_record(io, rotated, record_path)
    if saved is not None:
        return _problem_outcome(saved, model_outcomes, uncovered)
    removed = prune(io, rotated, root, keep=keep)
    if isinstance(removed, Problem):
        return _problem_outcome(removed, model_outcomes, uncovered)
    saved = save_pull_record(io, release(rotated), record_path)
    if saved is not None:
        return _problem_outcome(saved, model_outcomes, uncovered)
    return _pull_outcome(model_outcomes, uncovered, ok=True)


def run_models_pull(
    args: object,
    *,
    host: Host | None = None,
    site_path: PathLike = _SITE_PATH,
    models_path: PathLike | None = None,
    egress_path: PathLike | None = None,
    root: PathLike | None = None,
) -> int:
    """Load release inputs and run the selected profile's model pull."""

    del args
    io = host or RealHost()
    if io.geteuid() != 0:
        print(refusal("models pull", "root is required.", _ROOT_FIX), file=sys.stderr)
        return 1
    checkout = Path(__file__).parents[2] if root is None else Path(root)
    site_result = load_site(Path(site_path), host=io)
    if site_result.errors or site_result.config is None:
        print(render_site_errors(site_result.errors), file=sys.stderr)
        return 1
    actual_models = checkout / "models.lock" if models_path is None else models_path
    models_result = load_models_lock(actual_models, host=io)
    if models_result.errors or models_result.lock is None:
        print(render_models_errors(models_result.errors), file=sys.stderr)
        return 1
    profile = select_profile(models_result.lock, site_result.config.hardware_profile)
    if isinstance(profile, Problem):
        print(refusal("models pull", profile.problem, profile.fix), file=sys.stderr)
        return 1
    actual_egress = checkout / "config" / "egress.yaml" if egress_path is None else egress_path
    egress_result = load_egress_allowlist(actual_egress, host=io)
    if egress_result.errors or egress_result.allowlist is None:
        print(render_egress_errors(egress_result.errors), file=sys.stderr)
        return 1
    outcome = pull_profile(io, site_result.config, profile, egress_result.allowlist)
    # A failed model already printed its row; every other refusal has no row yet.
    if not outcome.ok and not any(model.kind == "failed" for model in outcome.models):
        print(refusal("models pull", outcome.problem, outcome.fix), file=sys.stderr)
    return int(not outcome.ok)
