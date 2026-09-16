"""pgBackRest command construction and host-side convergence."""

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from gideon.host import stack
from gideon.host.render.pgbackrest import REPOSITORY_PATH, STANZA
from gideon.host.report import command_detail
from gideon.host.sysio import CompletedText, Host, PathLike

SIDE_REPOSITORY_PATH: Final = "/data/backup-staging-side"
_POSTGRES_SERVICE: Final = "postgres"
_POSTGRES_USER: Final = "postgres"
_REPOSITORY_MODE: Final = 0o750
_COMMAND_TIMEOUT: Final = 3600.0
_INFO_TIMEOUT: Final = 60.0


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


@dataclass(frozen=True, slots=True)
class InfoResult(PgBackRestResult):
    """Parsed pgBackRest info, or its refusal."""

    infos: tuple[BackupInfo, ...] = ()


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


_STANZA_MISMATCH_FIX: Final = (
    "Run sudo python3 -m gideon restore --from staging to restore the cluster "
    "this repository belongs to, or move /data/backup-staging/pgbackrest aside, "
    "then re-run apply."
)


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
            return PgBackRestResult(result.problem, _STANZA_MISMATCH_FIX)
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


def _backup_records(value: object) -> tuple[Mapping[str, object], ...]:
    records: list[Mapping[str, object]] = []

    def walk(node: object) -> None:
        if isinstance(node, Mapping):
            backups = node.get("backup")
            if isinstance(backups, list):
                records.extend(
                    entry for entry in backups if isinstance(entry, Mapping)
                )
            for key, child in node.items():
                if key != "backup":
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return tuple(records)


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
        for entry in _backup_records(document):
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
            parsed.append(BackupInfo(label, backup_type, float(stop), timeline))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return InfoResult(
            problem=f"pgBackRest info returned malformed JSON: {exc}.",
            fix=_logs_fix(rendered_dir),
        )
    return InfoResult(infos=tuple(parsed))


def newest_full(infos: Sequence[BackupInfo]) -> BackupInfo | None:
    """Return the newest full backup, if the repository has one."""

    fulls = (entry for entry in infos if entry.type == "full")
    return max(fulls, key=lambda entry: entry.timestamp_stop, default=None)
