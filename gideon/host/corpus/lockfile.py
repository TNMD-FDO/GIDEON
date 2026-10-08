"""Model, load, and render committed corpus lockfiles."""

import difflib
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, cast
from urllib.parse import urlsplit

import yaml  # type: ignore[import-untyped]

from gideon.host.corpus.sources import SOURCES
from gideon.host.courts import CourtMap
from gideon.host.egress import EgressAllowlist
from gideon.host.sysio import PathLike, ReadBytesHost, RealHost

SCHEMA_VERSION: Final = 1
READABLE_SCHEMAS: Final = (SCHEMA_VERSION,)
PIPELINE_VERSION: Final = "0.0.0"
REASONS: Final = ("quarterly", "instrument", "tranche", "pipeline")
LABEL: Final = re.compile(r"^corpus-(\d{4}-\d{2}-\d{2})$")
SIDECAR_LINE: Final = re.compile(r"^(\S+)  ([0-9a-f]{64})  (0|[1-9][0-9]*)$")
_DIGEST: Final = re.compile(r"^[0-9a-f]{64}$")
_VERSION: Final = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_TIMESTAMP: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_FIX: Final = "Restore the lockfile and its companion files from the release checkout."
_HEADER: Final = "# Written by the corpus cut command; edit mirror_url alone.\n"
_ROOT_KEYS: Final = ("schema", "label", "pipeline", "cut_at", "reason", "base", "sources")
_SOURCE_KEYS: Final = (
    "snapshot_date", "base_url", "mirror_url", "sidecar_sha256", "files",
    "bytes", "index", "courts",
)
_INDEX_KEYS: Final = ("name", "url", "sha256", "size")


@dataclass(frozen=True, slots=True)
class SidecarEntry:
    """One object pinned by a source sidecar."""

    path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class IndexDocument:
    """The fields pinned for one committed index document."""

    name: str
    url: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class SourcePin:
    """A source snapshot and the files that define it."""

    snapshot_date: str
    base_url: str
    mirror_url: str | None
    sidecar_sha256: str
    files: int
    bytes: int
    index: tuple[IndexDocument, ...]
    entries: tuple[SidecarEntry, ...]
    courts: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class Lockfile:
    """One immutable corpus cut in source registry order."""

    schema: int
    label: str
    pipeline: str
    cut_at: str
    reason: str
    sources: Mapping[str, SourcePin]
    base: str | None = None

    @property
    def courts(self) -> tuple[str, ...]:
        """Sorted court ids carried by all source pins."""
        return tuple(sorted({court for pin in self.sources.values() for court in pin.courts or ()}))


@dataclass(frozen=True, slots=True)
class LockfileError:
    """A lockfile refusal with its corrective action."""

    key_path: str | None
    problem: str
    fix: str = _FIX


@dataclass(frozen=True, slots=True)
class LockfileLoadResult:
    """A loaded lockfile or all errors found while reading it."""

    lockfile: Lockfile | None = None
    errors: tuple[LockfileError, ...] = ()

    @property
    def ok(self) -> bool:
        return self.lockfile is not None and not self.errors


@dataclass(frozen=True, slots=True)
class LockfileDirectoryResult:
    """All completed lockfiles in a directory, newest last."""

    lockfiles: tuple[Lockfile, ...] = ()
    errors: tuple[LockfileError, ...] = ()

    @property
    def newest(self) -> Lockfile | None:
        return self.lockfiles[-1] if self.lockfiles else None


class _DuplicateKeyError(yaml.YAMLError):
    pass


class LockfileLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses repeated mapping keys."""


LockfileLoader.yaml_implicit_resolvers = {
    initial: list(resolvers)
    for initial, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _mapping_without_duplicates(
    loader: LockfileLoader, node: Any, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise _DuplicateKeyError(
                f"duplicate mapping key {key!r} at line {key_node.start_mark.line + 1}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


LockfileLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_without_duplicates
)


def _error(path: str | None, detail: str, *, fix: str = _FIX) -> LockfileError:
    return LockfileError(path, f"Invalid value for '{path}': {detail}." if path else detail, fix)


def _unknown(value: Mapping[object, object], valid: Sequence[str], prefix: str,
             errors: list[LockfileError]) -> None:
    for key in value:
        if not isinstance(key, str) or key not in valid:
            path = f"{prefix}.{key}" if prefix else str(key)
            nearest = difflib.get_close_matches(str(key), valid, n=1, cutoff=0.0)[0]
            errors.append(_error(path, f"unknown key; nearest valid key is '{nearest}'"))


def _required(value: Mapping[object, object], key: str, path: str,
              errors: list[LockfileError]) -> object:
    if key not in value:
        errors.append(_error(path, "missing required key"))
        return None
    return value[key]


def _string(value: object, path: str, errors: list[LockfileError]) -> str | None:
    if not isinstance(value, str) or not value:
        errors.append(_error(path, f"expected a non-empty string (got {value!r})"))
        return None
    return value


def _date(value: object, path: str, errors: list[LockfileError]) -> str | None:
    item = _string(value, path, errors)
    if item is not None:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item):
                raise ValueError
            date.fromisoformat(item)
        except ValueError:
            errors.append(_error(path, "expected a valid YYYY-MM-DD date"))
            return None
    return item


def _digest(value: object, path: str, errors: list[LockfileError]) -> str | None:
    item = _string(value, path, errors)
    if item is not None and not _DIGEST.fullmatch(item):
        errors.append(_error(path, "expected a lowercase sha256 digest"))
        return None
    return item


def _size(value: object, path: str, errors: list[LockfileError]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        errors.append(_error(path, f"expected a non-negative integer (got {value!r})"))
        return None
    return value


def _url(value: object, path: str, errors: list[LockfileError]) -> str | None:
    item = _string(value, path, errors)
    if item is not None:
        try:
            parsed = urlsplit(item)
            valid = (parsed.scheme == "https" and bool(parsed.hostname)
                     and parsed.username is None and parsed.password is None
                     and not parsed.fragment and not any(ch.isspace() for ch in item)
                     and parsed.port in (None, 443))
        except ValueError:
            valid = False
        if not valid:
            errors.append(_error(path, "expected an HTTPS URL with a host and no port but 443"))
            return None
    return item


def _relative_path(value: str) -> bool:
    return (not value.startswith("/") and not value.endswith("/")
            and all(part not in ("", ".", "..") for part in value.split("/"))
            and not any(ch in value for ch in "\\?#")
            and all(ord(ch) >= 33 for ch in value))


def _read_bytes(path: Path, host: ReadBytesHost, errors: list[LockfileError]) -> bytes | None:
    try:
        return host.read_bytes(path)
    except (FileNotFoundError, PermissionError, OSError) as exc:
        errors.append(_error(str(path), f"file is missing or unreadable ({exc})"))
        return None


def _sidecar(data: bytes, path: str, errors: list[LockfileError]) -> tuple[SidecarEntry, ...]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        errors.append(_error(path, "sidecar is not valid UTF-8"))
        return ()
    if not text.endswith("\n"):
        errors.append(_error(path, "sidecar must end with a newline"))
    entries: list[SidecarEntry] = []
    seen: set[str] = set()
    for number, line in enumerate(text.splitlines(), 1):
        match = SIDECAR_LINE.fullmatch(line)
        item_path = f"{path}:{number}"
        if match is None or not _relative_path(match.group(1)):
            errors.append(_error(item_path, "invalid sidecar line; use path, two spaces, sha256, two spaces, size"))
            continue
        name, digest, size = match.groups()
        if name in seen:
            errors.append(_error(item_path, f"repeated path {name!r}"))
        if entries and name <= entries[-1].path:
            errors.append(_error(item_path, "paths must be sorted and unique"))
        seen.add(name)
        entries.append(SidecarEntry(name, digest, int(size)))
    if not entries:
        errors.append(_error(path, "sidecar must pin at least one file"))
    return tuple(entries)


def _index(value: object, prefix: str, source_dir: Path, source: str,
           host: ReadBytesHost, errors: list[LockfileError]) -> tuple[IndexDocument, ...]:
    if not isinstance(value, list) or not value:
        errors.append(_error(prefix, "expected a non-empty list"))
        return ()
    result: list[IndexDocument] = []
    seen: set[str] = set()
    for number, raw in enumerate(value):
        path = f"{prefix}[{number}]"
        if not isinstance(raw, Mapping):
            errors.append(_error(path, "expected a mapping"))
            continue
        _unknown(raw, _INDEX_KEYS, path, errors)
        for key in _INDEX_KEYS:
            _required(raw, key, f"{path}.{key}", errors)
        name = _string(raw["name"], f"{path}.name", errors) if "name" in raw else None
        url = _url(raw["url"], f"{path}.url", errors) if "url" in raw else None
        digest = _digest(raw["sha256"], f"{path}.sha256", errors) if "sha256" in raw else None
        size = _size(raw["size"], f"{path}.size", errors) if "size" in raw else None
        if name is not None:
            if not _NAME.fullmatch(name) or name in (".", ".."):
                errors.append(_error(f"{path}.name", "expected a safe file name"))
            elif name in seen:
                errors.append(_error(f"{path}.name", "index names must be unique"))
            else:
                seen.add(name)
                data = _read_bytes(source_dir / f"{source}.{name}", host, errors)
                if data is not None:
                    if digest is not None and hashlib.sha256(data).hexdigest() != digest:
                        errors.append(_error(f"{path}.sha256", "index document digest differs from its pin"))
                    if size is not None and len(data) != size:
                        errors.append(_error(f"{path}.size", "index document size differs from its pin"))
        if name is not None and url is not None and digest is not None and size is not None:
            result.append(IndexDocument(name, url, digest, size))
    return tuple(result)


def _source(raw: object, name: str, carries_courts: bool, directory: Path,
            host: ReadBytesHost, errors: list[LockfileError]) -> SourcePin | None:
    prefix = f"sources.{name}"
    if not isinstance(raw, Mapping):
        errors.append(_error(prefix, "expected a mapping"))
        return None
    _unknown(raw, _SOURCE_KEYS, prefix, errors)
    for key in _SOURCE_KEYS[:-1]:
        if key not in raw:
            _required(raw, key, f"{prefix}.{key}", errors)
    snapshot = _date(raw["snapshot_date"], f"{prefix}.snapshot_date", errors) if "snapshot_date" in raw else None
    base_url = _url(raw["base_url"], f"{prefix}.base_url", errors) if "base_url" in raw else None
    mirror = raw.get("mirror_url")
    mirror_url = _url(mirror, f"{prefix}.mirror_url", errors) if mirror is not None else None
    digest = _digest(raw["sidecar_sha256"], f"{prefix}.sidecar_sha256", errors) if "sidecar_sha256" in raw else None
    files = _size(raw["files"], f"{prefix}.files", errors) if "files" in raw else None
    size = _size(raw["bytes"], f"{prefix}.bytes", errors) if "bytes" in raw else None
    index = _index(raw.get("index"), f"{prefix}.index", directory, name, host, errors) if "index" in raw else ()
    courts: tuple[str, ...] | None = None
    if carries_courts:
        if "courts" not in raw:
            errors.append(_error(f"{prefix}.courts", "missing required key"))
        elif not isinstance(raw["courts"], list) or not raw["courts"]:
            errors.append(_error(f"{prefix}.courts", "expected a non-empty list"))
        else:
            values = raw["courts"]
            for number, court in enumerate(values):
                if not isinstance(court, str) or not court:
                    errors.append(_error(f"{prefix}.courts[{number}]", "expected a court id"))
            if all(isinstance(court, str) and court for court in values):
                if values != sorted(set(values)):
                    errors.append(_error(f"{prefix}.courts", "court ids must be sorted and unique"))
                courts = tuple(values)
    elif "courts" in raw:
        errors.append(_error(f"{prefix}.courts", "source does not carry courts"))
    sidecar_path = directory / f"{name}.sha256"
    data = _read_bytes(sidecar_path, host, errors)
    entries: tuple[SidecarEntry, ...] = ()
    if data is not None:
        entries = _sidecar(data, str(sidecar_path), errors)
        if digest is not None and hashlib.sha256(data).hexdigest() != digest:
            errors.append(_error(f"{prefix}.sidecar_sha256", "sidecar digest differs from its pin"))
        if files is not None and len(entries) != files:
            errors.append(_error(f"{prefix}.files", "file count differs from sidecar"))
        if size is not None and sum(entry.size for entry in entries) != size:
            errors.append(_error(f"{prefix}.bytes", "byte total differs from sidecar"))
    if any(value is None for value in (snapshot, base_url, digest, files, size)):
        return None
    return SourcePin(cast(str, snapshot), cast(str, base_url), mirror_url,
                     cast(str, digest), cast(int, files), cast(int, size),
                     index, entries, courts)


def load_lockfile(path: PathLike, *, known_sources: Mapping[str, bool] | None = None,
                  host: ReadBytesHost | None = None) -> LockfileLoadResult:
    """Read a lockfile and all its companion files, collecting refusals."""
    if known_sources is None:
        known_sources = {source.name: source.carries_courts for source in SOURCES}
    io = host or RealHost()
    file_path = Path(path)
    errors: list[LockfileError] = []
    data = _read_bytes(file_path, io, errors)
    if data is None:
        return LockfileLoadResult(errors=tuple(errors))
    try:
        document = yaml.load(data.decode("utf-8"), Loader=LockfileLoader)
    except UnicodeDecodeError:
        return LockfileLoadResult(errors=(_error(str(path), "lockfile is not valid UTF-8"),))
    except _DuplicateKeyError as exc:
        return LockfileLoadResult(errors=(_error(None, f"lockfile has a {exc}"),))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = getattr(mark, "line", None)
        location = f" at line {line + 1}" if isinstance(line, int) else ""
        return LockfileLoadResult(errors=(_error(None, f"lockfile has a YAML parse error{location}: {exc}"),))
    if not isinstance(document, Mapping):
        return LockfileLoadResult(errors=(_error(None, "lockfile root must be a mapping"),))
    _unknown(document, _ROOT_KEYS, "", errors)
    for key in ("schema", "label", "pipeline", "cut_at", "reason", "sources"):
        if key not in document:
            _required(document, key, key, errors)
    schema = document.get("schema")
    if schema is not None and (isinstance(schema, bool) or not isinstance(schema, int)
                               or schema not in READABLE_SCHEMAS):
        errors.append(_error("schema", f"unsupported version {schema!r}",
                             fix="Upgrade GIDEON to a release that reads this lockfile schema."))
    label = _string(document["label"], "label", errors) if "label" in document else None
    if label is not None:
        match = LABEL.fullmatch(label)
        if match is None:
            errors.append(_error("label", "expected corpus-YYYY-MM-DD"))
        else:
            _date(match.group(1), "label", errors)
        if file_path.stem != label:
            errors.append(_error("label", f"does not match file stem {file_path.stem!r}"))
    pipeline = _string(document["pipeline"], "pipeline", errors) if "pipeline" in document else None
    if pipeline is not None and not _VERSION.fullmatch(pipeline):
        errors.append(_error("pipeline", "expected a three-part version"))
    cut_at = _string(document["cut_at"], "cut_at", errors) if "cut_at" in document else None
    if cut_at is not None:
        try:
            if not _TIMESTAMP.fullmatch(cut_at):
                raise ValueError
            datetime.fromisoformat(cut_at.replace("Z", "+00:00"))
        except ValueError:
            errors.append(_error("cut_at", "expected a valid UTC timestamp to the second"))
    reason = _string(document["reason"], "reason", errors) if "reason" in document else None
    if reason is not None and reason not in REASONS:
        errors.append(_error("reason", f"expected one of {', '.join(REASONS)}"))
    base: str | None = None
    if "base" in document:
        base = _string(document["base"], "base", errors)
        if base is not None:
            match = LABEL.fullmatch(base)
            if match is None:
                errors.append(_error("base", "expected a lockfile label"))
            else:
                _date(match.group(1), "base", errors)
    pins: dict[str, SourcePin] = {}
    sources = document.get("sources")
    if not isinstance(sources, Mapping):
        errors.append(_error("sources", "expected a mapping"))
    else:
        if not sources:
            errors.append(_error("sources", "expected at least one source"))
        for name in known_sources:
            if name not in sources:
                errors.append(_error(f"sources.{name}", "missing required source"))
        for name, raw in sources.items():
            if not isinstance(name, str) or name not in known_sources:
                errors.append(_error(f"sources.{name}", "unknown source"))
                continue
            pin = _source(raw, name, known_sources[name], file_path.with_suffix(""), io, errors)
            if pin is not None:
                pins[name] = pin
        if tuple(sources) != tuple(known_sources):
            errors.append(_error("sources", "sources must follow registry order"))
    if errors:
        return LockfileLoadResult(errors=tuple(errors))
    return LockfileLoadResult(Lockfile(cast(int, schema), cast(str, label),
                                       cast(str, pipeline), cast(str, cut_at),
                                       cast(str, reason), pins, base))


def render_sidecar(entries: Sequence[SidecarEntry]) -> str:
    """Render sorted sidecar entries with one trailing newline."""
    return "".join(
        f"{entry.path}  {entry.sha256}  {entry.size}\n"
        for entry in sorted(entries, key=lambda entry: entry.path)
    )


def render_lockfile(lockfile: Lockfile) -> str:
    """Render a lockfile in its canonical YAML key order."""
    document: dict[str, object] = {
        "schema": lockfile.schema,
        "label": lockfile.label,
        "pipeline": lockfile.pipeline,
        "cut_at": lockfile.cut_at,
        "reason": lockfile.reason,
    }
    if lockfile.base is not None:
        document["base"] = lockfile.base
    sources: dict[str, object] = {}
    for name, pin in lockfile.sources.items():
        source: dict[str, object] = {
            "snapshot_date": pin.snapshot_date,
            "base_url": pin.base_url,
            "mirror_url": pin.mirror_url,
            "sidecar_sha256": pin.sidecar_sha256,
            "files": pin.files,
            "bytes": pin.bytes,
            "index": [
                {"name": item.name, "url": item.url,
                 "sha256": item.sha256, "size": item.size}
                for item in pin.index
            ],
        }
        if pin.courts is not None:
            source["courts"] = list(pin.courts)
        sources[name] = source
    document["sources"] = sources
    return _HEADER + yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


def same_state(left: Lockfile, right: Lockfile) -> bool:
    """Compare the state a cut pins, excluding its label and index bytes."""
    if left.pipeline != right.pipeline or tuple(left.sources) != tuple(right.sources):
        return False
    return all(
        (a.snapshot_date, a.base_url, a.mirror_url, a.sidecar_sha256, a.courts)
        == (b.snapshot_date, b.base_url, b.mirror_url, b.sidecar_sha256, b.courts)
        for a, b in ((left.sources[name], right.sources[name]) for name in left.sources)
    )


def check_courts(lockfile: Lockfile, court_map: CourtMap) -> tuple[LockfileError, ...]:
    """Refuse court ids absent from an already loaded court map."""
    return tuple(
        _error(f"sources.{name}.courts", f"unknown court id {court!r}")
        for name, pin in lockfile.sources.items()
        for court in pin.courts or ()
        if court_map.court(court) is None
    )


def check_egress_hosts(lockfile: Lockfile, allowlist: EgressAllowlist) -> tuple[LockfileError, ...]:
    """Refuse source URL hosts absent from the loaded corpus allowlist."""
    group = allowlist.group("corpus")
    allowed = {item.host for item in group.hosts} if group is not None else set()
    return tuple(
        _error(f"sources.{name}.{field}", f"host {urlsplit(url).hostname!r} is not in the corpus allowlist",
               fix="Add the host to the corpus group in config/egress.yaml and cut again.")
        for name, pin in lockfile.sources.items()
        for field, url in (("base_url", pin.base_url), ("mirror_url", pin.mirror_url))
        if url is not None and urlsplit(url).hostname not in allowed
    )


def read_lockfile_directory(path: PathLike, *, known_sources: Mapping[str, bool] | None = None,
                            host: ReadBytesHost | None = None) -> LockfileDirectoryResult:
    """Load every YAML lockfile; unfinished companion directories are ignored."""
    io = host or RealHost()
    directory = Path(path)
    if not io.exists(directory):
        return LockfileDirectoryResult()
    try:
        names = io.listdir(directory)
    except OSError as exc:
        return LockfileDirectoryResult(errors=(_error(str(directory), f"directory is unreadable ({exc})"),))
    lockfiles: list[Lockfile] = []
    errors: list[LockfileError] = []
    for name in sorted(item for item in names if item.endswith(".yaml")):
        result = load_lockfile(directory / name, known_sources=known_sources, host=io)
        if result.lockfile is not None:
            lockfiles.append(result.lockfile)
        errors.extend(result.errors)
    labels = {lockfile.label for lockfile in lockfiles}
    for lockfile in lockfiles:
        if lockfile.base is None:
            continue
        key_path = str(directory / f"{lockfile.label}.yaml")
        if lockfile.base not in labels:
            errors.append(_error(key_path, f"base {lockfile.base} is not a lockfile in this directory"))
        if lockfile.base[len("corpus-"):] >= lockfile.label[len("corpus-"):]:
            errors.append(_error(key_path, f"base {lockfile.base} is not earlier than this lockfile"))
    return LockfileDirectoryResult(tuple(lockfiles), tuple(errors))


def render_errors(errors: Sequence[LockfileError]) -> str:
    """Render one refusal and its fix per line."""
    return "\n".join(
        f"{' '.join(error.problem.splitlines())} Fix: {error.fix}" for error in errors
    )
