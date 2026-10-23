"""Postgres roles, databases, and forward-only migration convergence.

Every statement reaches the server through ``docker compose exec`` over the
container's unix socket, which the official image trusts for every role, so no
password ever travels in argv or an environment.  SQL goes on stdin only.
"""

import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from gideon.host import pgbackrest, report, stack
from gideon.host.secrets import read_secret
from gideon.host.sysio import CompletedText, Host, PathLike

_POSTGRES_SERVICE: Final = "postgres"
_SUPERUSER: Final = "postgres"
_ADMIN_DATABASE: Final = "postgres"
_SCHEMA_ROLE: Final = "gideon"
_SCHEMA_DATABASE: Final = "gideon"
REPLAY_WAIT_SECONDS: Final = 900.0
_REPLAY_POLL_SECONDS: Final = 2.0
# The version is spliced into SQL as a literal, so the accepted names are the
# characters a version can safely be.
_MIGRATION_NAME = re.compile(r"^[0-9]{4}_[a-z0-9_]+\.sql$")


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """One application role, its release-managed password file, and the predefined roles it holds."""

    name: str
    secret_name: str
    # Granted by the superuser on every converge (idempotent): a migration
    # runs as gideon, which may not grant a predefined role.
    memberships: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DatabaseSpec:
    """One application database and its owner role."""

    name: str
    owner: str


ROLE_SPECS: Final[tuple[RoleSpec, ...]] = (
    RoleSpec("openwebui", "postgres_openwebui_password"),
    RoleSpec("gideon", "postgres_gideon_password"),
    RoleSpec("gideon_audit", "postgres_gideon_audit_password"),
    RoleSpec("gideon_ro_metrics", "postgres_gideon_ro_metrics_password", ("pg_monitor",)),
    RoleSpec("gideon_eval", "postgres_gideon_eval_password"),
    RoleSpec("gideon_worker", "postgres_gideon_worker_password"),
)
DATABASE_SPECS: Final[tuple[DatabaseSpec, ...]] = (
    DatabaseSpec("openwebui", "openwebui"),
    DatabaseSpec("gideon", "gideon"),
)


@dataclass(frozen=True, slots=True)
class ConvergeReport:
    """The changes made by one store convergence pass, or its refusal."""

    created_roles: tuple[str, ...] = ()
    created_databases: tuple[str, ...] = ()
    applied_migrations: tuple[str, ...] = ()
    problem: str | None = None
    fix: str = ""

    @property
    def ok(self) -> bool:
        return self.problem is None


@dataclass(frozen=True, slots=True)
class ReplayReach:
    """The last replayed commit, WAL position, and segment after recovery."""

    instant: datetime | None
    lsn: str
    segment: str
    segment_size: int


def _logs_fix(rendered_dir: PathLike) -> str:
    return stack.logs_fix(rendered_dir, _POSTGRES_SERVICE)


def _migration_fix(path: Path) -> str:
    return f"Correct migration {path}, then retry."


def _psql(
    io: Host,
    rendered_dir: PathLike,
    *,
    role: str,
    database: str,
    flags: tuple[str, ...],
    sql: str,
) -> CompletedText:
    argv = stack.exec_argv(
        rendered_dir, _POSTGRES_SERVICE, "psql", "-U", role, "-d", database, *flags, "-f", "-"
    )
    return io.run(argv, input=sql)


def _query(
    io: Host,
    rendered_dir: PathLike,
    *,
    sql: str,
    action: str,
    role: str = _SUPERUSER,
    database: str = _ADMIN_DATABASE,
) -> tuple[str, ...] | str:
    """One-column query results as stripped lines, or the problem text."""

    try:
        result = _psql(io, rendered_dir, role=role, database=database, flags=("-tA",), sql=sql)
    except OSError as exc:
        return f"psql {action} failed: {exc}"
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        return f"psql {action} failed: {detail}"
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def _replay_query(io: Host, rendered_dir: PathLike, sql: str) -> str | report.Problem:
    """Read replay state, keeping empty columns for SQL nulls."""

    try:
        result = _psql(
            io,
            rendered_dir,
            role=_SUPERUSER,
            database=_ADMIN_DATABASE,
            flags=("-tA", "-F", "\t", "-v", "ON_ERROR_STOP=1"),
            sql=sql,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return report.Problem(f"psql replay query failed: {exc}", _logs_fix(rendered_dir))
    if result.returncode != 0:
        return report.Problem(
            f"psql replay query failed: {report.command_detail(result)}",
            _logs_fix(rendered_dir),
        )
    return result.stdout


def replay_reach(
    io: Host,
    rendered_dir: PathLike,
    *,
    sleep: Callable[[float], None] = time.sleep,
    wait_seconds: float = REPLAY_WAIT_SECONDS,
) -> ReplayReach | report.Problem:
    """Wait for recovery to end, then read its reach before any restart.

    Postgres answers during replay, and ``pg_walfile_name`` cannot run in
    recovery, so the reach is read only once recovery has ended; the replay
    functions turn null again at a restart. A null commit instant is a valid
    reach: no commit was replayed.
    """

    remaining = wait_seconds
    while True:
        recovery = _replay_query(io, rendered_dir, "SELECT pg_is_in_recovery();\n")
        if isinstance(recovery, report.Problem):
            return recovery
        state = recovery.strip()
        if state == "f":
            break
        if state != "t":
            return report.Problem(
                "Postgres returned a malformed recovery state.", _logs_fix(rendered_dir)
            )
        if remaining <= 0:
            return report.Timeout(
                f"Postgres is still in recovery after waiting {wait_seconds:g} seconds.",
                _logs_fix(rendered_dir),
            )
        interval = min(_REPLAY_POLL_SECONDS, remaining)
        sleep(interval)
        remaining -= interval

    # The replay position is the end of the last record, which after a WAL
    # switch is the next segment's first byte, and pg_walfile_name names the
    # segment holding the byte it is given: the segment is the last byte's.
    answer = _replay_query(
        io,
        rendered_dir,
        "SELECT pg_last_xact_replay_timestamp(), pg_last_wal_replay_lsn(), "
        "pg_walfile_name(pg_last_wal_replay_lsn() - 1), "
        "(SELECT setting FROM pg_settings WHERE name = 'wal_segment_size');\n",
    )
    if isinstance(answer, report.Problem):
        return answer
    try:
        instant_text, lsn, segment, size_text = (
            column.strip() for column in answer.rstrip("\r\n").split("\t")
        )
        if not lsn:
            return report.Problem(
                "Postgres has no replay position; the server did not recover.",
                _logs_fix(rendered_dir),
            )
        pgbackrest.lsn_value(lsn)
        if not size_text.isdecimal():
            raise ValueError("WAL segment size is not a byte count")
        segment_size = int(size_text)
        pgbackrest.segment_ordinal(segment, pgbackrest.segments_per_log(segment_size))
        instant = datetime.fromisoformat(instant_text) if instant_text else None
        if instant is not None:
            if instant.tzinfo is None or instant.utcoffset() is None:
                raise ValueError("replay instant has no timezone")
            instant = instant.astimezone(UTC)
    except ValueError as exc:
        return report.Problem(
            f"Postgres returned a malformed replay reach: {exc}.",
            _logs_fix(rendered_dir),
        )
    return ReplayReach(instant, lsn, segment, segment_size)


def _statement(
    io: Host,
    rendered_dir: PathLike,
    *,
    role: str,
    database: str,
    sql: str,
    action: str,
    sensitive: bool = False,
) -> str | None:
    """Run one autocommit statement; the problem text, or None on success.

    A ``sensitive`` statement carries a secret, and psql's diagnostics can
    quote the failing line, so only the exit status is reported for it.
    """

    try:
        result = _psql(
            io, rendered_dir, role=role, database=database, flags=("-v", "ON_ERROR_STOP=1"), sql=sql
        )
    except OSError as exc:
        return f"psql {action} failed: {exc}"
    if result.returncode != 0:
        if sensitive:
            return f"psql {action} failed: exit {result.returncode}"
        detail = result.stderr.strip() or f"exit {result.returncode}"
        return f"psql {action} failed: {detail}"
    return None


def _create_role_sql(role: str, password: str) -> str:
    """CREATE ROLE with its password bound through a psql variable, never interpolated."""

    if any(character in password for character in "\r\n\x00"):
        raise ValueError("the password contains CR, LF, or NUL")
    escaped = password.replace("'", "''")
    return f"\\set pw '{escaped}'\nCREATE ROLE {role} LOGIN PASSWORD :'pw';\n"


def _migration_files(io: Host, root: Path) -> tuple[Path, ...] | str:
    directory = root / "migrations"
    try:
        names = io.listdir(directory)
    except OSError as exc:
        return f"Migration directory is unreadable: {directory} ({exc})."
    return tuple(
        sorted((directory / name for name in names if _MIGRATION_NAME.fullmatch(name)), key=lambda path: path.name)
    )


def _migration_sql(path: Path, source: str) -> str:
    """The migration text followed by its version row, so both commit or neither."""

    separator = "" if source.endswith("\n") else "\n"
    return f"{source}{separator}INSERT INTO schema_migrations (version) VALUES ('{path.stem}');\n"


def _run_migrations(io: Host, rendered_dir: PathLike, root: Path, report: ConvergeReport) -> ConvergeReport:
    # The runner owns its bookkeeping: no migration ever has to create the
    # table the runner reads.
    schema_error = _statement(
        io,
        rendered_dir,
        role=_SCHEMA_ROLE,
        database=_SCHEMA_DATABASE,
        sql=(
            "CREATE TABLE IF NOT EXISTS schema_migrations("
            "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());\n"
        ),
        action="schema_migrations statement",
    )
    if schema_error is not None:
        return replace(report, problem=schema_error, fix=_logs_fix(rendered_dir))

    applied = _query(
        io,
        rendered_dir,
        sql="SELECT version FROM schema_migrations ORDER BY version;\n",
        action="migration version query",
        role=_SCHEMA_ROLE,
        database=_SCHEMA_DATABASE,
    )
    if isinstance(applied, str):
        return replace(report, problem=applied, fix=_logs_fix(rendered_dir))

    files = _migration_files(io, root)
    if isinstance(files, str):
        return replace(report, problem=files, fix="Restore the release checkout's migrations directory, then retry.")
    for path in files:
        if path.stem in applied:
            continue
        try:
            source = io.read_text(path)
        except (OSError, UnicodeError) as exc:
            return replace(report, problem=f"Migration is unreadable: {path} ({exc}).", fix=_migration_fix(path))
        try:
            result = _psql(
                io,
                rendered_dir,
                role=_SCHEMA_ROLE,
                database=_SCHEMA_DATABASE,
                flags=("-v", "ON_ERROR_STOP=1", "--single-transaction"),
                sql=_migration_sql(path, source),
            )
        except OSError as exc:
            return replace(report, problem=f"Migration failed: {path} ({exc}).", fix=_migration_fix(path))
        if result.returncode != 0:
            detail = result.stderr.strip() or f"exit {result.returncode}"
            return replace(report, problem=f"Migration failed: {path} ({detail}).", fix=_migration_fix(path))
        report = replace(report, applied_migrations=report.applied_migrations + (path.stem,))
    return report


def converge(io: Host, rendered_dir: PathLike, root: PathLike | None = None) -> ConvergeReport:
    """Create absent roles and databases, then apply pending migrations in order.

    Existing roles are never altered (a password rotation is an explicit CSA
    action); ``CREATE DATABASE`` cannot share a transaction, so every
    creation is its own autocommit statement.
    """

    checkout = Path(__file__).parents[2] if root is None else Path(root)
    report = ConvergeReport()

    roles = _query(io, rendered_dir, sql="SELECT rolname FROM pg_roles ORDER BY rolname;\n", action="role query")
    if isinstance(roles, str):
        return replace(report, problem=roles, fix=_logs_fix(rendered_dir))
    for spec in ROLE_SPECS:
        if spec.name in roles:
            continue
        secret = read_secret(io, spec.secret_name)
        if not secret.ok or secret.value is None:
            return replace(report, problem=secret.problem or f"Secret is unavailable: {spec.secret_name}.", fix=secret.fix)
        try:
            sql = _create_role_sql(spec.name, secret.value)
        except ValueError as exc:
            return replace(
                report,
                problem=f"Unable to create role {spec.name}: {exc}.",
                fix=f"Correct /etc/gideon/secrets/{spec.secret_name}, then retry.",
            )
        error = _statement(
            io,
            rendered_dir,
            role=_SUPERUSER,
            database=_ADMIN_DATABASE,
            sql=sql,
            action=f"CREATE ROLE {spec.name}",
            sensitive=True,
        )
        if error is not None:
            return replace(report, problem=error, fix=_logs_fix(rendered_dir))
        report = replace(report, created_roles=report.created_roles + (spec.name,))

    # Predefined-role memberships are re-granted every run: GRANT of a held
    # membership is a no-op, and only the superuser may grant them.
    for spec in ROLE_SPECS:
        for membership in spec.memberships:
            error = _statement(
                io,
                rendered_dir,
                role=_SUPERUSER,
                database=_ADMIN_DATABASE,
                sql=f"GRANT {membership} TO {spec.name};\n",
                action=f"GRANT {membership} TO {spec.name}",
            )
            if error is not None:
                return replace(report, problem=error, fix=_logs_fix(rendered_dir))

    databases = _query(
        io, rendered_dir, sql="SELECT datname FROM pg_database ORDER BY datname;\n", action="database query"
    )
    if isinstance(databases, str):
        return replace(report, problem=databases, fix=_logs_fix(rendered_dir))
    for database in DATABASE_SPECS:
        if database.name in databases:
            continue
        error = _statement(
            io,
            rendered_dir,
            role=_SUPERUSER,
            database=_ADMIN_DATABASE,
            sql=f"CREATE DATABASE {database.name} OWNER {database.owner};\n",
            action=f"CREATE DATABASE {database.name}",
        )
        if error is not None:
            return replace(report, problem=error, fix=_logs_fix(rendered_dir))
        report = replace(report, created_databases=report.created_databases + (database.name,))

    return _run_migrations(io, rendered_dir, checkout, report)
