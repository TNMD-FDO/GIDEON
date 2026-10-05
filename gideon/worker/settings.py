"""Environment and secret-file settings for the queue worker."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

DATABASE_HOST_ENV: Final = "GIDEON_WORKER_DB_HOST"
DATABASE_PORT_ENV: Final = "GIDEON_WORKER_DB_PORT"
DATABASE_NAME_ENV: Final = "GIDEON_WORKER_DB_NAME"
DATABASE_ROLE_ENV: Final = "GIDEON_WORKER_DB_ROLE"
PASSWORD_FILE_ENV: Final = "GIDEON_WORKER_PASSWORD_FILE"
CONCURRENCY_ENV: Final = "GIDEON_WORKER_CONCURRENCY"


class WorkerSettingsError(ValueError):
    """A safe, actionable refusal for worker settings or its secret file."""


@dataclass(frozen=True, slots=True)
class Settings:
    """The database coordinates, password path, and worker capacity."""

    database_host: str
    database_port: int
    database_name: str
    database_role: str
    password_file: Path
    concurrency: int


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise WorkerSettingsError(
            f"Environment variable {name} is missing or empty. "
            "Fix: rerun sudo python3 -m gideon apply."
        )
    return value


def _positive_integer(environ: Mapping[str, str], name: str) -> int:
    value = _required(environ, name)
    try:
        number = int(value)
    except ValueError as exc:
        raise WorkerSettingsError(
            f"Environment variable {name} must be a positive integer. "
            "Fix: rerun sudo python3 -m gideon apply."
        ) from exc
    if number < 1:
        raise WorkerSettingsError(
            f"Environment variable {name} must be a positive integer. "
            "Fix: rerun sudo python3 -m gideon apply."
        )
    return number


def read_password(path: Path) -> str:
    """Read the mounted password for each new database connection."""

    try:
        password = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise WorkerSettingsError(
            f"Secret file {path} is missing or empty. "
            "Fix: restore the worker password file and rerun apply."
        ) from exc
    if not password:
        raise WorkerSettingsError(
            f"Secret file {path} is missing or empty. "
            "Fix: restore the worker password file and rerun apply."
        )
    return password


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Validate the environment and mounted secret without retaining its value."""

    values = os.environ if environ is None else environ
    port = _positive_integer(values, DATABASE_PORT_ENV)
    if port > 65_535:
        raise WorkerSettingsError(
            f"Environment variable {DATABASE_PORT_ENV} must be a valid TCP port. "
            "Fix: rerun sudo python3 -m gideon apply."
        )
    password_file = Path(_required(values, PASSWORD_FILE_ENV))
    read_password(password_file)
    return Settings(
        database_host=_required(values, DATABASE_HOST_ENV),
        database_port=port,
        database_name=_required(values, DATABASE_NAME_ENV),
        database_role=_required(values, DATABASE_ROLE_ENV),
        password_file=password_file,
        concurrency=_positive_integer(values, CONCURRENCY_ENV),
    )


def connection_kwargs(settings: Settings) -> dict[str, str | int]:
    """Resolve credentials when a pool or listener opens a connection."""

    return {
        "host": settings.database_host,
        "port": settings.database_port,
        "dbname": settings.database_name,
        "user": settings.database_role,
        "password": read_password(settings.password_file),
    }
