"""Snapshot, recovery, and count clients for the two derived stores."""

import json
import re
import subprocess
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from gideon.host import report, secrets, stack
from gideon.host.render.opensearch import (
    OPENSEARCH_PASSWORD_SECRET_NAME,
    OPENSEARCH_REPOSITORY,
    OPENSEARCH_REST_PORT,
    OPENSEARCH_SERVICE_NAME,
    OPENSEARCH_SNAPSHOTS_MOUNT,
    OPENSEARCH_SNAPSHOTS_ROOT,
    OPENSEARCH_USER,
)
from gideon.host.render.qdrant import (
    QDRANT_REST_PORT,
    QDRANT_SECRET_NAME,
    QDRANT_SERVICE_NAME,
    QDRANT_SNAPSHOTS_MOUNT,
    QDRANT_SNAPSHOTS_ROOT,
    QDRANT_SNAPSHOTS_TEMP_DIR,
)
from gideon.host.sysio import Host, PathLike

_QDRANT_TCP_SCRIPT: Final = (
    'exec 3<>"/dev/tcp/127.0.0.1/$1" || exit 7; cat >&3; cat <&3'
)
_QDRANT_TCP_LABEL: Final = "qdrant-snapshot-request"
_CURL_STATUS_TRAILER: Final = "gideon-status:"
_PLAIN_NAME: Final = re.compile(r"[A-Za-z0-9._-]+\Z")
_HTTP_STATUS: Final = re.compile(r"HTTP/1\.\d ([1-5]\d\d)(?: .*)?\Z")
_CHECKSUM: Final = re.compile(r"[0-9a-f]{64}\Z")
_CURL_TRAILER: Final = re.compile(r"\ngideon-status:(\d{3})\n\Z")
_DIAGNOSTIC_LIMIT: Final = 200
# exempt: a bound, not a threshold; listings and deletes should answer promptly.
STORE_LIST_WAIT_SECONDS: Final = 60
# exempt: a bound, not a threshold; a large store may need hours to snapshot or recover.
STORE_SNAPSHOT_WAIT_SECONDS: Final = 4 * 60 * 60
# exempt: a bound, not a threshold; container execution needs room to return its reply.
STORE_REQUEST_MARGIN_SECONDS: Final = 10
# exempt: a rate, not a threshold; the first full run measures this starting value.
OPENSEARCH_SNAPSHOT_RATE: Final = "200mb"
OPENSEARCH_SNAPSHOT_PREFIX: Final = "gideon-"
INDEXES_INCREMENTAL_DETAIL: Final = (
    "skipped (incremental run; the stores snapshot on a full run)"
)
INDEXES_PRE_RESTORE_DETAIL: Final = (
    "skipped (pre-restore run; the live snapshot directories ride as they stand)"
)
_NO_SNAPSHOT_DETAIL: Final = "no snapshot in the set"
_RECOVERY_CLAUSES: Final = {
    QDRANT_SERVICE_NAME: "qdrant: {count} collection(s) recovered in {seconds} s",
    OPENSEARCH_SERVICE_NAME: "opensearch: {count} index(es) restored in {seconds} s",
}


@dataclass(frozen=True, slots=True)
class StoreSnapshot:
    """One store's snapshot names and measured work."""

    store: str
    names: tuple[str, ...]
    seconds: float
    bytes: int | None


@dataclass(frozen=True, slots=True)
class StoreRecovery:
    """One store's recovered names, duration, and absence in the set."""

    store: str
    names: tuple[str, ...]
    seconds: float
    skipped: bool


@dataclass(frozen=True, slots=True)
class _Reply:
    status: int
    data: object


def _problem(rendered_dir: PathLike, store: str, detail: str) -> report.Problem:
    return report.Problem(detail, stack.logs_fix(rendered_dir, store))


def _provision_fix() -> str:
    return f"Run {report.command('host provision --only disk-layout')}, then retry."


def _diagnostic(value: object, credential: str = "") -> str:
    line = report.one_line(value)
    if credential:
        line = line.replace(json.dumps(credential)[1:-1], "[redacted]")
        line = line.replace(credential, "[redacted]")
    return line[:_DIAGNOSTIC_LIMIT]


def store_secret(io: Host, name: str) -> str | report.Problem:
    read = secrets.read_secret(io, name)
    if read.problem is not None:
        fix = (
            f"Run {report.command('apply')}, then retry." if read.missing else read.fix
        )
        return report.Problem(read.problem, fix)
    if not read.value or not read.value.strip():
        path = secrets.secret_path(name)
        return report.Problem(
            f"Secret file is empty: {path}.",
            "Correct the secret file, then retry.",
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in read.value):
        path = secrets.secret_path(name)
        return report.Problem(
            f"Secret file contains an invalid character: {path}.",
            "Correct the secret file, then retry.",
        )
    return read.value


def _plain_name(
    name: object, rendered_dir: PathLike, store: str
) -> str | report.Problem:
    if (
        not isinstance(name, str)
        or name in {".", ".."}
        or _PLAIN_NAME.fullmatch(name) is None
    ):
        return _problem(rendered_dir, store, f"{store} returned an unsafe name")
    return name


def _qdrant_error(data: object) -> str:
    if isinstance(data, Mapping):
        status = data.get("status")
        if isinstance(status, Mapping) and isinstance(status.get("error"), str):
            return status["error"]
    return ""


def _opensearch_error(data: object) -> tuple[str, bool]:
    if not isinstance(data, Mapping):
        return "", False
    error = data.get("error")
    if isinstance(error, str):
        return error, False
    parts: list[str] = []
    verification = False
    while isinstance(error, Mapping):
        kind = error.get("type")
        reason = error.get("reason")
        if kind == "repository_verification_exception":
            verification = True
        if isinstance(kind, str) or isinstance(reason, str):
            parts.append(
                ": ".join(str(part) for part in (kind, reason) if isinstance(part, str))
            )
        error = error.get("caused_by")
    return " <- ".join(parts), verification


def _parse_qdrant_reply(raw: str, rendered_dir: PathLike) -> _Reply | report.Problem:
    head, separator, body = raw.partition("\r\n\r\n")
    if not separator:
        # The host's text subprocess normalizes CRLF when it captures stdout.
        head, separator, body = raw.partition("\n\n")
    if not separator:
        return _problem(
            rendered_dir, QDRANT_SERVICE_NAME, "qdrant reply has no header boundary"
        )
    lines = head.split("\r\n" if "\r\n" in head else "\n")
    match = _HTTP_STATUS.fullmatch(lines[0])
    if match is None:
        return _problem(
            rendered_dir, QDRANT_SERVICE_NAME, "qdrant reply has an invalid status line"
        )
    lengths = [
        line.partition(":")[2].strip()
        for line in lines[1:]
        if line.lower().startswith("content-length:")
    ]
    if lengths:
        if len(lengths) != 1 or not lengths[0].isdigit():
            return _problem(
                rendered_dir,
                QDRANT_SERVICE_NAME,
                "qdrant reply has an invalid content length",
            )
        if int(lengths[0]) != len(body.encode("utf-8")):
            return _problem(
                rendered_dir,
                QDRANT_SERVICE_NAME,
                "qdrant reply is truncated or has an invalid content length",
            )
    try:
        data: object = json.loads(body)
    except ValueError:
        return _problem(rendered_dir, QDRANT_SERVICE_NAME, "qdrant reply is not JSON")
    return _Reply(int(match.group(1)), data)


def _parse_opensearch_reply(
    raw: str, rendered_dir: PathLike
) -> _Reply | report.Problem:
    match = _CURL_TRAILER.search(raw)
    if match is None:
        return _problem(
            rendered_dir,
            OPENSEARCH_SERVICE_NAME,
            "opensearch reply has no status trailer",
        )
    try:
        data: object = json.loads(raw[: match.start()])
    except ValueError:
        return _problem(
            rendered_dir, OPENSEARCH_SERVICE_NAME, "opensearch reply is not JSON"
        )
    return _Reply(int(match.group(1)), data)


def _run_request(
    io: Host,
    rendered_dir: PathLike,
    store: str,
    method: str,
    path: str,
    argv: Sequence[str],
    request: str,
    credential: str,
    bound: int,
) -> _Reply | report.Problem:
    call = f"{store} {method} {path}"
    try:
        result = io.run(
            argv, input=request, timeout=bound + STORE_REQUEST_MARGIN_SECONDS
        )
    except subprocess.TimeoutExpired:
        return report.Timeout(f"{call} timed out", stack.logs_fix(rendered_dir, store))
    except OSError:
        return _problem(rendered_dir, store, f"{call} could not run")
    except UnicodeError:
        return _problem(rendered_dir, store, f"{call} returned unreadable text")
    if result.returncode != 0:
        lines = [line for line in result.stderr.splitlines() if line.strip()]
        diagnostic = _diagnostic(lines[-1], credential) if lines else ""
        detail = f"{call} failed (exit {result.returncode})"
        if diagnostic:
            detail += f": {diagnostic}"
        return _problem(rendered_dir, store, detail)
    return (
        _parse_qdrant_reply(result.stdout, rendered_dir)
        if store == QDRANT_SERVICE_NAME
        else _parse_opensearch_reply(result.stdout, rendered_dir)
    )


def qdrant_request(
    io: Host,
    rendered_dir: PathLike,
    key: str,
    method: str,
    path: str,
    *,
    body: object | None = None,
    bound: int = STORE_LIST_WAIT_SECONDS,
    accepted_statuses: frozenset[int] = frozenset({200}),
) -> _Reply | report.Problem:
    payload = "" if body is None else json.dumps(body, separators=(",", ":"))
    headers = [
        f"{method} {path} HTTP/1.0",
        f"api-key: {key}",
        f"Content-Length: {len(payload.encode('utf-8'))}",
        "Connection: close",
    ]
    if body is not None:
        headers.append("Content-Type: application/json")
    request = "\r\n".join(headers) + "\r\n\r\n" + payload
    argv = stack.exec_argv(
        rendered_dir,
        QDRANT_SERVICE_NAME,
        "bash",
        "-c",
        _QDRANT_TCP_SCRIPT,
        _QDRANT_TCP_LABEL,
        str(QDRANT_REST_PORT),
    )
    reply = _run_request(
        io, rendered_dir, QDRANT_SERVICE_NAME, method, path, argv, request, key, bound
    )
    if isinstance(reply, report.Problem):
        return reply
    if reply.status not in accepted_statuses:
        message = _diagnostic(_qdrant_error(reply.data), key)
        return _problem(
            rendered_dir,
            QDRANT_SERVICE_NAME,
            f"qdrant {method} {path} returned HTTP {reply.status}"
            + (f": {message}" if message else ""),
        )
    return reply


def _curl_quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def opensearch_request(
    io: Host,
    rendered_dir: PathLike,
    password: str,
    method: str,
    path: str,
    *,
    body: object | None = None,
    bound: int = STORE_LIST_WAIT_SECONDS,
    accepted_statuses: frozenset[int] = frozenset({200}),
) -> _Reply | report.Problem:
    lines = [
        f"user = {_curl_quoted(f'{OPENSEARCH_USER}:{password}')}",
        f"url = {_curl_quoted(f'http://127.0.0.1:{OPENSEARCH_REST_PORT}{path}')}",
        f"request = {_curl_quoted(method)}",
        f"max-time = {bound}",
        f"write-out = {_curl_quoted(f'\\n{_CURL_STATUS_TRAILER}%{{http_code}}\\n')}",
    ]
    if body is not None:
        lines.append(f"header = {_curl_quoted('Content-Type: application/json')}")
        lines.append(f"data = {_curl_quoted(json.dumps(body, separators=(',', ':')))}")
    request = "\n".join(lines) + "\n"
    argv = stack.exec_argv(
        rendered_dir, OPENSEARCH_SERVICE_NAME, "curl", "-s", "-S", "-K", "-"
    )
    reply = _run_request(
        io,
        rendered_dir,
        OPENSEARCH_SERVICE_NAME,
        method,
        path,
        argv,
        request,
        password,
        bound,
    )
    if isinstance(reply, report.Problem):
        return reply
    if reply.status not in accepted_statuses:
        message, verification = _opensearch_error(reply.data)
        message = _diagnostic(message, password)
        detail = f"opensearch {method} {path} returned HTTP {reply.status}" + (
            f": {message}" if message else ""
        )
        if verification:
            return report.Problem(
                detail,
                _provision_fix(),
            )
        return _problem(rendered_dir, OPENSEARCH_SERVICE_NAME, detail)
    return reply


def opensearch_document_count(
    io: Host, rendered_dir: PathLike, password: str, name: str,
) -> int | None | report.Problem:
    """Read an index's document count, or None when the index is absent."""

    checked = _plain_name(name, rendered_dir, OPENSEARCH_SERVICE_NAME)
    if isinstance(checked, report.Problem):
        return checked
    reply = opensearch_request(
        io, rendered_dir, password, "GET", f"/{checked}/_count",
        accepted_statuses=frozenset({200, 404}),
    )
    if isinstance(reply, report.Problem):
        return reply
    if reply.status == 404:
        return None
    count = reply.data.get("count") if isinstance(reply.data, Mapping) else None
    if type(count) is not int or count < 0:
        return _problem(
            rendered_dir, OPENSEARCH_SERVICE_NAME,
            "opensearch returned an invalid document count",
        )
    return count


def qdrant_collection_names(
    io: Host, rendered_dir: PathLike, key: str,
) -> tuple[str, ...] | report.Problem:
    """Read the store's collection names in sorted order."""

    reply = qdrant_request(io, rendered_dir, key, "GET", "/collections")
    if isinstance(reply, report.Problem):
        return reply
    return _qdrant_collections(reply.data, rendered_dir)


def qdrant_point_count(
    io: Host, rendered_dir: PathLike, key: str, name: str,
) -> int | None | report.Problem:
    """Read a collection's exact point count, or None when it is absent."""

    checked = _plain_name(name, rendered_dir, QDRANT_SERVICE_NAME)
    if isinstance(checked, report.Problem):
        return checked
    reply = qdrant_request(
        io, rendered_dir, key, "POST", f"/collections/{checked}/points/count",
        body={"exact": True}, accepted_statuses=frozenset({200, 404}),
    )
    if isinstance(reply, report.Problem):
        return reply
    if reply.status == 404:
        return None
    result = reply.data.get("result") if isinstance(reply.data, Mapping) else None
    count = result.get("count") if isinstance(result, Mapping) else None
    if type(count) is not int or count < 0:
        return _problem(
            rendered_dir, QDRANT_SERVICE_NAME,
            "qdrant returned an invalid point count",
        )
    return count


def _qdrant_collections(
    data: object, rendered_dir: PathLike
) -> tuple[str, ...] | report.Problem:
    if not isinstance(data, Mapping):
        return _problem(
            rendered_dir, QDRANT_SERVICE_NAME, "qdrant collection listing has no result"
        )
    result = data.get("result")
    if not isinstance(result, Mapping) or not isinstance(
        result.get("collections"), list
    ):
        return _problem(
            rendered_dir,
            QDRANT_SERVICE_NAME,
            "qdrant collection listing has no collections",
        )
    names: list[str] = []
    for entry in result["collections"]:
        name = _plain_name(
            entry.get("name") if isinstance(entry, Mapping) else None,
            rendered_dir,
            QDRANT_SERVICE_NAME,
        )
        if isinstance(name, report.Problem):
            return name
        names.append(name)
    return tuple(sorted(set(names)))


def _qdrant_snapshot_entry(
    data: object, rendered_dir: PathLike
) -> tuple[str, int] | report.Problem:
    result = data.get("result") if isinstance(data, Mapping) else None
    if not isinstance(result, Mapping):
        return _problem(
            rendered_dir, QDRANT_SERVICE_NAME, "qdrant snapshot reply has no result"
        )
    name = _plain_name(result.get("name"), rendered_dir, QDRANT_SERVICE_NAME)
    if isinstance(name, report.Problem):
        return name
    size = result.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return _problem(
            rendered_dir, QDRANT_SERVICE_NAME, "qdrant snapshot reply has no valid size"
        )
    return name, size


def _qdrant_snapshot_names(
    data: object, rendered_dir: PathLike
) -> tuple[str, ...] | report.Problem:
    result = data.get("result") if isinstance(data, Mapping) else None
    if not isinstance(result, list):
        return _problem(
            rendered_dir, QDRANT_SERVICE_NAME, "qdrant snapshot listing has no result"
        )
    names: list[str] = []
    for entry in result:
        name = _plain_name(
            entry.get("name") if isinstance(entry, Mapping) else None,
            rendered_dir,
            QDRANT_SERVICE_NAME,
        )
        if isinstance(name, report.Problem):
            return name
        names.append(name)
    return tuple(sorted(set(names)))


def _stale_qdrant_dirs(
    io: Host,
    rendered_dir: PathLike,
    qdrant_root: PathLike,
    collections: tuple[str, ...],
) -> report.Problem | None:
    root = Path(qdrant_root)
    # NUL keeps a directory with an embedded newline from becoming two names.
    argv = [
        "find",
        str(root),
        "-mindepth",
        "1",
        "-maxdepth",
        "1",
        "-type",
        "d",
        "-printf",
        "%f\\0",
    ]
    try:
        result = io.run(argv)
    except OSError:
        return report.Problem(f"could not list {root}", _provision_fix())
    if result.returncode != 0:
        return report.Problem(
            f"could not list {root} (exit {result.returncode})", _provision_fix()
        )
    if result.stdout and not result.stdout.endswith("\0"):
        return _problem(
            rendered_dir,
            QDRANT_SERVICE_NAME,
            "qdrant snapshot directory listing is incomplete",
        )
    stale: list[str] = []
    for candidate in result.stdout.split("\0")[:-1]:
        name = _plain_name(candidate, rendered_dir, QDRANT_SERVICE_NAME)
        if isinstance(name, report.Problem):
            return name
        if name not in collections and name != QDRANT_SNAPSHOTS_TEMP_DIR:
            stale.append(name)
    if not stale:
        return None
    remove = [
        "rm",
        "-rf",
        "--one-file-system",
        "--",
        *(str(root / name) for name in sorted(set(stale))),
    ]
    try:
        result = io.run(remove)
    except OSError:
        return _problem(
            rendered_dir,
            QDRANT_SERVICE_NAME,
            "could not remove stale qdrant snapshot directories",
        )
    if result.returncode != 0:
        return _problem(
            rendered_dir,
            QDRANT_SERVICE_NAME,
            f"could not remove stale qdrant snapshot directories (exit {result.returncode})",
        )
    return None


def _snapshot_qdrant(
    io: Host, rendered_dir: PathLike, qdrant_root: PathLike, clock: Callable[[], float]
) -> StoreSnapshot | report.Problem:
    key = store_secret(io, QDRANT_SECRET_NAME)
    if isinstance(key, report.Problem):
        return key
    started = clock()
    reply = qdrant_request(io, rendered_dir, key, "GET", "/collections")
    if isinstance(reply, report.Problem):
        return reply
    collections = _qdrant_collections(reply.data, rendered_dir)
    if isinstance(collections, report.Problem):
        return collections
    total_bytes = 0
    for collection in collections:
        path = f"/collections/{collection}/snapshots"
        created = qdrant_request(
            io,
            rendered_dir,
            key,
            "POST",
            path + "?wait=true",
            bound=STORE_SNAPSHOT_WAIT_SECONDS,
        )
        if isinstance(created, report.Problem):
            return created
        entry = _qdrant_snapshot_entry(created.data, rendered_dir)
        if isinstance(entry, report.Problem):
            return entry
        name, size = entry
        total_bytes += size
        listing = qdrant_request(io, rendered_dir, key, "GET", path)
        if isinstance(listing, report.Problem):
            return listing
        names = _qdrant_snapshot_names(listing.data, rendered_dir)
        if isinstance(names, report.Problem):
            return names
        if name not in names:
            return _problem(
                rendered_dir,
                QDRANT_SERVICE_NAME,
                f"qdrant snapshot {name} was not listed after creation",
            )
        for older in names:
            if older == name:
                continue
            deleted = qdrant_request(
                io, rendered_dir, key, "DELETE", f"{path}/{older}?wait=true"
            )
            if isinstance(deleted, report.Problem):
                return deleted
            if (
                not isinstance(deleted.data, Mapping)
                or deleted.data.get("result") is not True
            ):
                return _problem(
                    rendered_dir,
                    QDRANT_SERVICE_NAME,
                    f"qdrant did not delete snapshot {older}",
                )
    stale = _stale_qdrant_dirs(io, rendered_dir, qdrant_root, collections)
    if stale is not None:
        return stale
    return StoreSnapshot(
        QDRANT_SERVICE_NAME, collections, clock() - started, total_bytes
    )


def _opensearch_snapshot_names(
    data: object, rendered_dir: PathLike
) -> tuple[str, ...] | report.Problem:
    snapshots = data.get("snapshots") if isinstance(data, Mapping) else None
    if not isinstance(snapshots, list):
        return _problem(
            rendered_dir,
            OPENSEARCH_SERVICE_NAME,
            "opensearch snapshot listing has no snapshots",
        )
    names: list[str] = []
    for entry in snapshots:
        name = _plain_name(
            entry.get("snapshot") if isinstance(entry, Mapping) else None,
            rendered_dir,
            OPENSEARCH_SERVICE_NAME,
        )
        if isinstance(name, report.Problem):
            return name
        names.append(name)
    return tuple(sorted(set(names)))


def _register_repository(
    io: Host, rendered_dir: PathLike, password: str
) -> report.Problem | None:
    path = f"/_snapshot/{OPENSEARCH_REPOSITORY}"
    registered = opensearch_request(
        io,
        rendered_dir,
        password,
        "PUT",
        path,
        body={
            "type": "fs",
            "settings": {
                "location": OPENSEARCH_SNAPSHOTS_MOUNT,
                "compress": False,
                "max_snapshot_bytes_per_sec": OPENSEARCH_SNAPSHOT_RATE,
            },
        },
    )
    if isinstance(registered, report.Problem):
        return registered
    if not isinstance(registered.data, Mapping) or registered.data.get("acknowledged") is not True:
        return _problem(
            rendered_dir,
            OPENSEARCH_SERVICE_NAME,
            "opensearch did not register the snapshot repository",
        )
    return None


def _snapshot_opensearch(
    io: Host, rendered_dir: PathLike, now: datetime, clock: Callable[[], float]
) -> StoreSnapshot | report.Problem:
    password = store_secret(io, OPENSEARCH_PASSWORD_SECRET_NAME)
    if isinstance(password, report.Problem):
        return password
    started = clock()
    repository_path = f"/_snapshot/{OPENSEARCH_REPOSITORY}"
    registration = _register_repository(io, rendered_dir, password)
    if registration is not None:
        return registration
    listed = opensearch_request(
        io,
        rendered_dir,
        password,
        "GET",
        "/_cat/indices/*,-.*?format=json&expand_wildcards=open,hidden",
    )
    if isinstance(listed, report.Problem):
        return listed
    if not isinstance(listed.data, list):
        return _problem(
            rendered_dir,
            OPENSEARCH_SERVICE_NAME,
            "opensearch index listing is not a list",
        )
    names: list[str] = []
    for entry in listed.data:
        name = _plain_name(
            entry.get("index") if isinstance(entry, Mapping) else None,
            rendered_dir,
            OPENSEARCH_SERVICE_NAME,
        )
        if isinstance(name, report.Problem):
            return name
        if name.startswith("."):
            return _problem(
                rendered_dir,
                OPENSEARCH_SERVICE_NAME,
                "opensearch returned a dot-named index",
            )
        names.append(name)
    indices = tuple(sorted(set(names)))
    created_name: str | None = None
    if indices:
        if now.tzinfo is None or now.utcoffset() is None:
            return _problem(
                rendered_dir, OPENSEARCH_SERVICE_NAME, "snapshot time has no UTC offset"
            )
        created_name = OPENSEARCH_SNAPSHOT_PREFIX + now.astimezone(UTC).strftime(
            "%Y%m%dt%H%M%Sz"
        )
        created = opensearch_request(
            io,
            rendered_dir,
            password,
            "PUT",
            f"{repository_path}/{created_name}?wait_for_completion=true",
            body={"indices": "*,-.*", "include_global_state": False, "partial": False},
            bound=STORE_SNAPSHOT_WAIT_SECONDS,
        )
        if isinstance(created, report.Problem):
            return created
        snapshot = (
            created.data.get("snapshot") if isinstance(created.data, Mapping) else None
        )
        if not isinstance(snapshot, Mapping):
            return _problem(
                rendered_dir,
                OPENSEARCH_SERVICE_NAME,
                "opensearch snapshot reply has no snapshot",
            )
        state = snapshot.get("state")
        shards = snapshot.get("shards")
        failed = shards.get("failed") if isinstance(shards, Mapping) else None
        if (
            state != "SUCCESS"
            or not isinstance(failed, int)
            or isinstance(failed, bool)
            or failed != 0
        ):
            return _problem(
                rendered_dir,
                OPENSEARCH_SERVICE_NAME,
                f"opensearch snapshot state {state}, failed shards {failed}",
            )
        result_indices = snapshot.get("indices")
        if not isinstance(result_indices, list):
            return _problem(
                rendered_dir,
                OPENSEARCH_SERVICE_NAME,
                "opensearch snapshot reply has no indices",
            )
        verified: list[str] = []
        for candidate in result_indices:
            name = _plain_name(candidate, rendered_dir, OPENSEARCH_SERVICE_NAME)
            if isinstance(name, report.Problem):
                return name
            if name.startswith("."):
                return _problem(
                    rendered_dir,
                    OPENSEARCH_SERVICE_NAME,
                    "opensearch snapshotted a dot-named index",
                )
            verified.append(name)
        if tuple(sorted(set(verified))) != indices:
            return _problem(
                rendered_dir,
                OPENSEARCH_SERVICE_NAME,
                "opensearch snapshot indices differ from the listing",
            )
    listing = opensearch_request(
        io, rendered_dir, password, "GET", f"{repository_path}/_all"
    )
    if isinstance(listing, report.Problem):
        return listing
    snapshots = _opensearch_snapshot_names(listing.data, rendered_dir)
    if isinstance(snapshots, report.Problem):
        return snapshots
    if created_name is not None and created_name not in snapshots:
        return _problem(
            rendered_dir,
            OPENSEARCH_SERVICE_NAME,
            "opensearch snapshot was not listed after creation",
        )
    for older in snapshots:
        if older == created_name:
            continue
        deleted = opensearch_request(
            io, rendered_dir, password, "DELETE", f"{repository_path}/{older}"
        )
        if isinstance(deleted, report.Problem):
            return deleted
        if (
            not isinstance(deleted.data, Mapping)
            or deleted.data.get("acknowledged") is not True
        ):
            return _problem(
                rendered_dir,
                OPENSEARCH_SERVICE_NAME,
                f"opensearch did not delete snapshot {older}",
            )
    return StoreSnapshot(OPENSEARCH_SERVICE_NAME, indices, clock() - started, None)


def snapshot_all(
    io: Host,
    rendered_dir: PathLike,
    *,
    now: datetime,
    qdrant_root: PathLike = QDRANT_SNAPSHOTS_ROOT,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[StoreSnapshot, StoreSnapshot] | report.Problem:
    """Snapshot each derived store and retire its older snapshots."""

    qdrant = _snapshot_qdrant(io, rendered_dir, qdrant_root, clock)
    if isinstance(qdrant, report.Problem):
        return qdrant
    opensearch = _snapshot_opensearch(io, rendered_dir, now, clock)
    if isinstance(opensearch, report.Problem):
        return opensearch
    return qdrant, opensearch


def snapshot_detail(results: Sequence[StoreSnapshot]) -> str:
    """Format the two stores' counts and elapsed seconds for a stage row."""

    return "; ".join(
        f"{result.store}: {len(result.names)} {'collection(s)' if result.store == QDRANT_SERVICE_NAME else 'index(es)'} in {result.seconds:.1f} s"
        for result in results
    )


def snapshot_audit(results: Sequence[StoreSnapshot]) -> Mapping[str, object]:
    """Return JSON-ready snapshot facts keyed by store."""

    return {
        result.store: {
            "names": list(result.names),
            "seconds": result.seconds,
            "bytes": result.bytes,
        }
        for result in results
    }


def _recover_qdrant(
    io: Host, rendered_dir: PathLike, root: PathLike, clock: Callable[[], float]
) -> StoreRecovery | report.Problem:
    started = clock()
    directory = Path(root)
    argv = [
        "find", str(directory), "-mindepth", "2", "-maxdepth", "2", "-type", "f",
        "-name", "*.snapshot", "-printf", "%P\\0",
    ]
    try:
        found = io.run(argv)
    except (OSError, subprocess.SubprocessError):
        return report.Problem(f"could not list {directory}", _provision_fix())
    if found.returncode != 0:
        return report.Problem(
            f"could not list {directory} (exit {found.returncode})", _provision_fix()
        )
    if found.stdout and not found.stdout.endswith("\0"):
        return _problem(rendered_dir, QDRANT_SERVICE_NAME, "qdrant snapshot listing is incomplete")
    newest: dict[str, str] = {}
    for relative in found.stdout.split("\0")[:-1]:
        parts = relative.split("/")
        if len(parts) != 2:
            return _problem(rendered_dir, QDRANT_SERVICE_NAME, "qdrant snapshot path is malformed")
        collection, file = parts
        if collection == QDRANT_SNAPSHOTS_TEMP_DIR:
            continue
        for name in (collection, file):
            valid = _plain_name(name, rendered_dir, QDRANT_SERVICE_NAME)
            if isinstance(valid, report.Problem):
                return valid
        if not file.endswith(".snapshot"):
            return _problem(rendered_dir, QDRANT_SERVICE_NAME, "qdrant snapshot file is malformed")
        if collection not in newest or file > newest[collection]:
            newest[collection] = file
    if not newest:
        return StoreRecovery(QDRANT_SERVICE_NAME, (), clock() - started, True)
    key = store_secret(io, QDRANT_SECRET_NAME)
    if isinstance(key, report.Problem):
        return key
    for collection, file in sorted(newest.items()):
        body = {
            "location": f"file://{QDRANT_SNAPSHOTS_MOUNT}/{collection}/{file}",
            "priority": "snapshot",
        }
        try:
            checksum = io.read_text(directory / collection / f"{file}.checksum")
        except FileNotFoundError:
            checksum = ""
        except (OSError, UnicodeError):
            return _problem(
                rendered_dir, QDRANT_SERVICE_NAME,
                f"qdrant checksum for {collection}/{file} cannot be read",
            )
        if _CHECKSUM.fullmatch(checksum) is not None:
            body["checksum"] = checksum
        reply = qdrant_request(
            io, rendered_dir, key, "PUT",
            f"/collections/{collection}/snapshots/recover?wait=true",
            body=body, bound=STORE_SNAPSHOT_WAIT_SECONDS,
        )
        if isinstance(reply, report.Problem):
            return reply
        if not isinstance(reply.data, Mapping) or reply.data.get("result") is not True:
            return _problem(
                rendered_dir, QDRANT_SERVICE_NAME,
                f"qdrant did not recover collection {collection}",
            )
    return StoreRecovery(QDRANT_SERVICE_NAME, tuple(sorted(newest)), clock() - started, False)


def _recovery_snapshots(
    data: object, rendered_dir: PathLike
) -> tuple[tuple[int, str, tuple[str, ...]], ...] | report.Problem:
    snapshots = data.get("snapshots") if isinstance(data, Mapping) else None
    if not isinstance(snapshots, list):
        return _problem(rendered_dir, OPENSEARCH_SERVICE_NAME, "opensearch snapshot listing has no snapshots")
    parsed: list[tuple[int, str, tuple[str, ...]]] = []
    for entry in snapshots:
        if not isinstance(entry, Mapping):
            return _problem(rendered_dir, OPENSEARCH_SERVICE_NAME, "opensearch snapshot listing is malformed")
        name = _plain_name(entry.get("snapshot"), rendered_dir, OPENSEARCH_SERVICE_NAME)
        if isinstance(name, report.Problem):
            return name
        started = entry.get("start_time_in_millis")
        indices = entry.get("indices")
        if (
            not isinstance(started, int) or isinstance(started, bool) or started < 0
            or not isinstance(indices, list)
        ):
            return _problem(rendered_dir, OPENSEARCH_SERVICE_NAME, "opensearch snapshot listing is malformed")
        # A killed or failed full run can leave a newer unusable snapshot
        # beside the last good one; recovery reads only a whole snapshot, so
        # no live index is deleted for one it could not restore.
        shards = entry.get("shards")
        failed = shards.get("failed") if isinstance(shards, Mapping) else None
        if entry.get("state") != "SUCCESS" or failed != 0 or isinstance(failed, bool):
            continue
        names: list[str] = []
        for candidate in indices:
            index = _plain_name(candidate, rendered_dir, OPENSEARCH_SERVICE_NAME)
            if isinstance(index, report.Problem):
                return index
            if index.startswith("."):
                return _problem(rendered_dir, OPENSEARCH_SERVICE_NAME, "opensearch snapshot names a dot index")
            names.append(index)
        parsed.append((started, name, tuple(sorted(set(names)))))
    return tuple(parsed)


def _recover_opensearch(
    io: Host, rendered_dir: PathLike, clock: Callable[[], float]
) -> StoreRecovery | report.Problem:
    password = store_secret(io, OPENSEARCH_PASSWORD_SECRET_NAME)
    if isinstance(password, report.Problem):
        return password
    started = clock()
    repository = f"/_snapshot/{OPENSEARCH_REPOSITORY}"
    removed = opensearch_request(
        io, rendered_dir, password, "DELETE", repository,
        accepted_statuses=frozenset({200, 404}),
    )
    if isinstance(removed, report.Problem):
        return removed
    registration = _register_repository(io, rendered_dir, password)
    if registration is not None:
        return registration
    listed = opensearch_request(io, rendered_dir, password, "GET", f"{repository}/_all")
    if isinstance(listed, report.Problem):
        return listed
    snapshots = _recovery_snapshots(listed.data, rendered_dir)
    if isinstance(snapshots, report.Problem):
        return snapshots
    if not snapshots:
        return StoreRecovery(OPENSEARCH_SERVICE_NAME, (), clock() - started, True)
    _, name, indices = max(snapshots, key=lambda item: item[0])
    if not indices:
        return StoreRecovery(OPENSEARCH_SERVICE_NAME, (), clock() - started, True)
    for index in indices:
        deleted = opensearch_request(
            io, rendered_dir, password, "DELETE", f"/{index}",
            accepted_statuses=frozenset({200, 404}),
        )
        if isinstance(deleted, report.Problem):
            return deleted
    restored = opensearch_request(
        io, rendered_dir, password, "POST",
        f"{repository}/{name}/_restore?wait_for_completion=true",
        body={"indices": ",".join(indices), "include_global_state": False},
        bound=STORE_SNAPSHOT_WAIT_SECONDS,
    )
    if isinstance(restored, report.Problem):
        return restored
    snapshot = restored.data.get("snapshot") if isinstance(restored.data, Mapping) else None
    shards = snapshot.get("shards") if isinstance(snapshot, Mapping) else None
    failed = shards.get("failed") if isinstance(shards, Mapping) else None
    if not isinstance(failed, int) or isinstance(failed, bool) or failed != 0:
        return _problem(
            rendered_dir, OPENSEARCH_SERVICE_NAME,
            f"opensearch restore failed shards {failed}",
        )
    return StoreRecovery(OPENSEARCH_SERVICE_NAME, indices, clock() - started, False)


def recover_all(
    io: Host,
    rendered_dir: PathLike,
    *,
    present_sources: Collection[str],
    qdrant_root: PathLike = QDRANT_SNAPSHOTS_ROOT,
    opensearch_root: PathLike = OPENSEARCH_SNAPSHOTS_ROOT,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[StoreRecovery, StoreRecovery] | report.Problem:
    """Recover the present derived roots after their copies have been put back."""

    qdrant = (
        _recover_qdrant(io, rendered_dir, qdrant_root, clock)
        if str(qdrant_root) in present_sources
        else StoreRecovery(QDRANT_SERVICE_NAME, (), 0.0, True)
    )
    if isinstance(qdrant, report.Problem):
        return qdrant
    opensearch = (
        _recover_opensearch(io, rendered_dir, clock)
        if str(opensearch_root) in present_sources
        else StoreRecovery(OPENSEARCH_SERVICE_NAME, (), 0.0, True)
    )
    if isinstance(opensearch, report.Problem):
        return opensearch
    return qdrant, opensearch


def no_snapshot_clause(store: str) -> str:
    """Describe a store with no snapshot in the selected set."""

    return f"{store}: {_NO_SNAPSHOT_DETAIL}"


def recovery_clause(result: StoreRecovery) -> str:
    """Describe one store's recovery for the restore row."""

    if result.skipped:
        return no_snapshot_clause(result.store)
    return _RECOVERY_CLAUSES[result.store].format(
        count=len(result.names), seconds=f"{result.seconds:.1f}"
    )


def recovery_clause_patterns() -> tuple[re.Pattern[str], re.Pattern[str]]:
    """Match the two recovered clauses from their formatting templates."""

    def pattern(store: str) -> re.Pattern[str]:
        escaped = re.escape(_RECOVERY_CLAUSES[store])
        expression = escaped.replace(r"\{count\}", r"\d+").replace(r"\{seconds\}", r"\d+\.\d")
        return re.compile(expression)

    return pattern(QDRANT_SERVICE_NAME), pattern(OPENSEARCH_SERVICE_NAME)


def recovery_audit(results: Sequence[StoreRecovery]) -> Mapping[str, object]:
    """Return JSON-ready recovered names and seconds keyed by store."""

    return {
        result.store: (
            {"skipped": _NO_SNAPSHOT_DETAIL}
            if result.skipped else {"names": list(result.names), "seconds": result.seconds}
        )
        for result in results
    }
