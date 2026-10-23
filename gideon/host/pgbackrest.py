"""pgBackRest commands, convergence, and backup, archive, and history reads."""

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from gideon.host import report, stack
from gideon.host.render.pgbackrest import REPOSITORY_PATH, STANZA
from gideon.host.report import command_detail
from gideon.host.sysio import CompletedText, Host, PathLike

SIDE_REPOSITORY_PATH: Final = "/data/backup-staging-side"
_POSTGRES_SERVICE: Final = "postgres"
_POSTGRES_USER: Final = "postgres"
_REPOSITORY_MODE: Final = 0o750
_COMMAND_TIMEOUT: Final = 3600.0
_INFO_TIMEOUT: Final = 60.0


def _stanza_mismatch_fix() -> str:
    return (
        f"Run {report.command('restore --from staging')} to restore the cluster "
        "this repository belongs to, or move /data/backup-staging/pgbackrest aside, "
        "then re-run apply."
    )


@dataclass(frozen=True, slots=True)
class PgBackRestResult:
    """The result of one pgBackRest or repository convergence operation."""

    problem: str | None = None
    fix: str = ""

    @property
    def ok(self) -> bool:
        return self.problem is None


@dataclass(frozen=True, slots=True)
class BackupInfo:
    """The fields of one completed backup needed by the host commands."""

    label: str
    type: str
    timestamp_stop: float
    # The timeline the backup was taken on: the first eight hex digits of its
    # archive start segment. A restore recovers along it (see restore.py).
    timeline: int | None = None
    lsn_stop: str | None = None


@dataclass(frozen=True, slots=True)
class InfoResult(PgBackRestResult):
    """Parsed pgBackRest info, or its refusal."""

    infos: tuple[BackupInfo, ...] = ()
    archive_id: str | None = None
    archive_max: str | None = None


@dataclass(frozen=True, slots=True)
class HistoryResult(PgBackRestResult):
    """The latest repository timeline and its ancestor switch positions."""

    latest: int = 1
    chain: tuple[tuple[int, int], ...] = ()


def exec_argv(rendered_dir: PathLike, *pgbackrest_args: str) -> list[str]:
    """Build a pgBackRest command against the running Postgres service."""

    return stack.exec_argv(
        rendered_dir,
        _POSTGRES_SERVICE,
        "pgbackrest",
        f"--stanza={STANZA}",
        *pgbackrest_args,
        user=_POSTGRES_USER,
    )


def run_argv(
    project_dir: PathLike,
    *pgbackrest_args: str,
    repository: PathLike | None = None,
) -> list[str]:
    """Build a one-off pgBackRest container command."""

    mount: list[str] = []
    repository_arg: list[str] = []
    if repository is not None:
        mount = ["-v", f"{repository}:{SIDE_REPOSITORY_PATH}"]
        repository_arg = [f"--repo1-path={SIDE_REPOSITORY_PATH}"]
    return stack.compose_argv(
        project_dir,
        "run",
        "--rm",
        "--no-deps",
        "-u",
        _POSTGRES_USER,
        *mount,
        _POSTGRES_SERVICE,
        "pgbackrest",
        f"--stanza={STANZA}",
        *repository_arg,
        *pgbackrest_args,
    )


def verify_argv(
    project_dir: PathLike, repository: PathLike | None = None
) -> list[str]:
    """Build the repository verification command for a Compose project."""

    return run_argv(project_dir, "verify", repository=repository)


def backup_argv(
    rendered_dir: PathLike,
    backup_type: str,
    *,
    expire_auto: bool = True,
) -> list[str]:
    """Build a full or incremental backup command."""

    if backup_type not in {"full", "incr"}:
        raise ValueError(
            f"Unsupported pgBackRest backup type: {backup_type}. "
            "Use full or incr, then re-run backup."
        )
    args = ["backup", f"--type={backup_type}"]
    if not expire_auto:
        args.append("--no-expire-auto")
    return exec_argv(rendered_dir, *args)


def _logs_fix(rendered_dir: PathLike) -> str:
    return stack.logs_fix(rendered_dir, _POSTGRES_SERVICE)


def _command_result(
    host: Host,
    argv: Sequence[str],
    *,
    action: str,
    rendered_dir: PathLike,
    timeout: float = _COMMAND_TIMEOUT,
) -> CompletedText | PgBackRestResult:
    try:
        result = host.run(argv, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return PgBackRestResult(
            problem=f"pgBackRest {action} failed: {exc}",
            fix=_logs_fix(rendered_dir),
        )
    if result.returncode != 0:
        return PgBackRestResult(
            problem=f"pgBackRest {action} failed: {command_detail(result)}",
            fix=_logs_fix(rendered_dir),
        )
    return result


def _container_identity_value(result: CompletedText) -> int | None:
    value = result.stdout.strip()
    return int(value) if value.isdecimal() else None


def container_identity(
    host: Host, rendered_dir: PathLike
) -> tuple[tuple[int, int] | None, PgBackRestResult | None]:
    """Resolve the Postgres container ids, falling back to a one-off run."""

    values: dict[str, int] = {}
    failed = False
    for flag in ("-u", "-g"):
        result = _command_result(
            host,
            stack.exec_argv(
                rendered_dir,
                _POSTGRES_SERVICE,
                "id",
                flag,
                _POSTGRES_USER,
            ),
            action=f"container postgres {flag} lookup",
            rendered_dir=rendered_dir,
            timeout=60.0,
        )
        if isinstance(result, PgBackRestResult):
            failed = True
            break
        value = _container_identity_value(result)
        if value is None:
            failed = True
            break
        values[flag] = value
    if not failed:
        return (values["-u"], values["-g"]), None

    values = {}
    for flag in ("-u", "-g"):
        result = _command_result(
            host,
            stack.compose_argv(
                rendered_dir,
                "run",
                "--rm",
                "--no-deps",
                _POSTGRES_SERVICE,
                "id",
                flag,
                _POSTGRES_USER,
            ),
            action=f"one-off postgres {flag} lookup",
            rendered_dir=rendered_dir,
            timeout=60.0,
        )
        if isinstance(result, PgBackRestResult):
            return None, result
        value = _container_identity_value(result)
        if value is None:
            return None, PgBackRestResult(
                problem=(
                    f"pgBackRest one-off postgres {flag} lookup returned "
                    f"an invalid id: {result.stdout.strip() or '(empty)'}"
                ),
                fix=_logs_fix(rendered_dir),
            )
        values[flag] = value
    return (values["-u"], values["-g"]), None


def ensure_repository(host: Host, rendered_dir: PathLike) -> PgBackRestResult:
    """Create and own the host repository using the container's postgres ids."""

    identity, failure = container_identity(host, rendered_dir)
    if failure is not None or identity is None:
        return failure or PgBackRestResult(
            problem="pgBackRest container identity lookup failed.",
            fix=_logs_fix(rendered_dir),
        )
    uid, gid = identity
    try:
        host.mkdir(
            REPOSITORY_PATH,
            mode=_REPOSITORY_MODE,
            parents=True,
            exist_ok=True,
        )
        host.chmod(REPOSITORY_PATH, _REPOSITORY_MODE)
        host.chown(REPOSITORY_PATH, uid, gid)
    except OSError as exc:
        return PgBackRestResult(
            problem=f"Could not prepare pgBackRest repository {REPOSITORY_PATH}: {exc}.",
            fix=_logs_fix(rendered_dir),
        )
    return PgBackRestResult()


def ensure_stanza(host: Host, rendered_dir: PathLike) -> PgBackRestResult:
    """Create the stanza, refusing a repository belonging to another cluster."""

    result = _command_result(
        host,
        exec_argv(rendered_dir, "stanza-create"),
        action="stanza-create",
        rendered_dir=rendered_dir,
    )
    if isinstance(result, PgBackRestResult) and result.problem is not None:
        detail = result.problem.casefold()
        if "already exists" in detail or "already present" in detail:
            return PgBackRestResult()
        if "do not match" in detail:
            return PgBackRestResult(result.problem, _stanza_mismatch_fix())
    return result if isinstance(result, PgBackRestResult) else PgBackRestResult()


def check(host: Host, rendered_dir: PathLike) -> PgBackRestResult:
    """Run pgBackRest's end-to-end archive check."""

    result = _command_result(
        host,
        exec_argv(rendered_dir, "check"),
        action="check",
        rendered_dir=rendered_dir,
    )
    return result if isinstance(result, PgBackRestResult) else PgBackRestResult()


def verify(host: Host, argv: Sequence[str]) -> str | None:
    """Run a prepared pgBackRest verify command and return its problem."""

    try:
        result = host.run(argv, timeout=_COMMAND_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"pgBackRest verify failed: {exc}"
    if result.returncode == 0:
        return None
    return f"pgBackRest verify failed: {command_detail(result)}"


def _info_records(value: object, field: str) -> tuple[Mapping[str, object], ...]:
    records: list[Mapping[str, object]] = []

    def walk(node: object) -> None:
        if isinstance(node, Mapping):
            entries = node.get(field)
            if isinstance(entries, list):
                records.extend(
                    entry for entry in entries if isinstance(entry, Mapping)
                )
            for key, child in node.items():
                if key != field:
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return tuple(records)


def _archive_database_id(entry: Mapping[str, object]) -> int:
    database = entry.get("database")
    value = database.get("id") if isinstance(database, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("archive database id is missing")
    return value


def info(host: Host, rendered_dir: PathLike, *, running: bool = True) -> InfoResult:
    """Read and parse pgBackRest's JSON backup inventory.

    Through the running Postgres service, or — with the stack stopped, as a
    restore's Postgres stage finds it — a one-off container over the repository.
    """

    argv = (
        exec_argv(rendered_dir, "info", "--output=json")
        if running
        else run_argv(rendered_dir, "info", "--output=json")
    )
    try:
        result = host.run(argv, timeout=_INFO_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return InfoResult(
            problem=f"pgBackRest info failed: {exc}", fix=_logs_fix(rendered_dir)
        )
    if result.returncode != 0:
        return InfoResult(
            problem=f"pgBackRest info failed: {command_detail(result)}",
            fix=_logs_fix(rendered_dir),
        )
    try:
        document = json.loads(result.stdout)
        parsed: list[BackupInfo] = []
        for entry in _info_records(document, "backup"):
            label = entry.get("label")
            backup_type = entry.get("type")
            timestamp = entry.get("timestamp")
            stop = timestamp.get("stop") if isinstance(timestamp, Mapping) else None
            if not isinstance(label, str) or not isinstance(backup_type, str):
                raise TypeError("backup label or type is missing")
            if isinstance(stop, bool) or not isinstance(stop, (int, float, str)):
                raise TypeError("backup stop timestamp is missing")
            archive = entry.get("archive")
            start = archive.get("start") if isinstance(archive, Mapping) else None
            timeline = int(start[:8], 16) if isinstance(start, str) and len(start) >= 8 else None
            lsn = entry.get("lsn")
            lsn_stop = lsn.get("stop") if isinstance(lsn, Mapping) else None
            if lsn_stop is not None and not isinstance(lsn_stop, str):
                raise TypeError("backup stop LSN is not a string")
            parsed.append(BackupInfo(label, backup_type, float(stop), timeline, lsn_stop))
        # After a stanza upgrade the list holds one entry per cluster; the
        # newest database id is the cluster a restore replays.
        archives = sorted(_info_records(document, "archive"), key=_archive_database_id)
        archive_id = archives[-1].get("id") if archives else None
        archive_max = archives[-1].get("max") if archives else None
        if archive_id is not None and not isinstance(archive_id, str):
            raise TypeError("archive id is not a string")
        if archive_max is not None and not isinstance(archive_max, str):
            raise TypeError("archive maximum is not a string")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return InfoResult(
            problem=f"pgBackRest info returned malformed JSON: {exc}.",
            fix=_logs_fix(rendered_dir),
        )
    return InfoResult(infos=tuple(parsed), archive_id=archive_id, archive_max=archive_max)


def lsn_value(lsn: str) -> int:
    """Convert a PostgreSQL X/Y WAL position to its byte offset."""

    if re.fullmatch(r"[0-9A-Fa-f]{1,8}/[0-9A-Fa-f]{1,8}", lsn) is None:
        raise ValueError("invalid WAL position")
    log, offset = lsn.split("/")
    return (int(log, 16) << 32) + int(offset, 16)


def segments_per_log(wal_segment_size_bytes: int) -> int:
    """Return the number of WAL segments in a 2**32-byte log."""

    if wal_segment_size_bytes <= 0 or (1 << 32) % wal_segment_size_bytes:
        raise ValueError("WAL segment size must be a positive divisor of 2**32")
    return (1 << 32) // wal_segment_size_bytes


def segment_ordinal(name: str, segments_per_log: int) -> int:
    """Number a WAL segment across log rollovers, independent of timeline."""

    if re.fullmatch(r"[0-9A-Fa-f]{24}", name) is None:
        raise ValueError("invalid WAL segment name")
    return int(name[8:16], 16) * segments_per_log + int(name[16:24], 16)


def reaches_latest(
    backup: BackupInfo, latest: int, chain: tuple[tuple[int, int], ...]
) -> bool:
    """Whether a backup can follow the history to the latest timeline."""

    if backup.lsn_stop is None or backup.timeline is None:
        return False
    stop = lsn_value(backup.lsn_stop)
    return backup.timeline == latest or any(
        backup.timeline == timeline and stop <= switch for timeline, switch in chain
    )


def history(host: Host, rendered_dir: PathLike, archive_id: str) -> HistoryResult:
    """Read the latest timeline's history through one-off repository commands."""

    path = f"archive/{STANZA}/{archive_id}"
    result = _command_result(
        host,
        run_argv(rendered_dir, "repo-ls", path, "--output=json"),
        action="history listing",
        rendered_dir=rendered_dir,
        timeout=_INFO_TIMEOUT,
    )
    if isinstance(result, PgBackRestResult):
        return HistoryResult(problem=result.problem, fix=result.fix)
    try:
        document = json.loads(result.stdout)
        if not isinstance(document, dict):
            raise TypeError("history listing is not an object")
        files = {
            int(name[:8], 16): name
            for name in document
            if re.fullmatch(r"[0-9A-Fa-f]{8}\.history", name)
        }
    except (TypeError, ValueError) as exc:
        return HistoryResult(
            problem=f"pgBackRest history listing returned malformed JSON: {exc}.",
            fix=_logs_fix(rendered_dir),
        )
    if not files:
        return HistoryResult()
    latest = max(files)
    result = _command_result(
        host,
        run_argv(rendered_dir, "repo-get", f"{path}/{files[latest]}"),
        action="history read",
        rendered_dir=rendered_dir,
        timeout=_INFO_TIMEOUT,
    )
    if isinstance(result, PgBackRestResult):
        return HistoryResult(problem=result.problem, fix=result.fix)
    chain: list[tuple[int, int]] = []
    try:
        for line in result.stdout.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            timeline_text, lsn, _reason = line.split(None, 2)
            timeline = int(timeline_text)
            if not (0 < timeline < latest) or (chain and timeline <= chain[-1][0]):
                raise ValueError("ancestor timelines are out of order")
            chain.append((timeline, lsn_value(lsn)))
        if not chain:
            raise ValueError("history has no ancestor timeline")
    except ValueError as exc:
        return HistoryResult(
            problem=f"pgBackRest history returned malformed content: {exc}.",
            fix=_logs_fix(rendered_dir),
        )
    return HistoryResult(latest=latest, chain=tuple(chain))


def newest_full(infos: Sequence[BackupInfo]) -> BackupInfo | None:
    """Return the newest full backup, if the repository has one."""

    fulls = (entry for entry in infos if entry.type == "full")
    return max(fulls, key=lambda entry: entry.timestamp_stop, default=None)
