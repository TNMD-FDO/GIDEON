"""Environment and secret-file settings for the queue worker."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

DATABASE_HOST_ENV: Final = "GIDEON_WORKER_DB_HOST"
DATABASE_PORT_ENV: Final = "GIDEON_WORKER_DB_PORT"
DATABASE_NAME_ENV: Final = "GIDEON_WORKER_DB_NAME"
DATABASE_ROLE_ENV: Final = "GIDEON_WORKER_DB_ROLE"
PASSWORD_FILE_ENV: Final = "GIDEON_WORKER_PASSWORD_FILE"
CONCURRENCY_ENV: Final = "GIDEON_WORKER_CONCURRENCY"
OPENSEARCH_URL_ENV: Final = "GIDEON_WORKER_OPENSEARCH_URL"
OPENSEARCH_USER_ENV: Final = "GIDEON_WORKER_OPENSEARCH_USER"
OPENSEARCH_PASSWORD_FILE_ENV: Final = "GIDEON_WORKER_OPENSEARCH_PASSWORD_FILE"
QDRANT_URL_ENV: Final = "GIDEON_WORKER_QDRANT_URL"
QDRANT_KEY_FILE_ENV: Final = "GIDEON_WORKER_QDRANT_KEY_FILE"
EMBED_URL_ENV: Final = "GIDEON_WORKER_EMBED_URL"
EMBED_KEY_FILE_ENV: Final = "GIDEON_WORKER_EMBED_KEY_FILE"


class WorkerSettingsError(ValueError):
    """A safe, actionable refusal for worker settings or its secret file."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Database, index store, and embedding server coordinates and secret paths."""

    database_host: str
    database_port: int
    database_name: str
    database_role: str
    password_file: Path
    concurrency: int
    opensearch_url: str
    opensearch_user: str
    opensearch_password_file: Path
    qdrant_url: str
    qdrant_key_file: Path
    embed_url: str
    embed_key_file: Path


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
    """Read a mounted credential when validating or constructing a client."""

    try:
        password = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise WorkerSettingsError(
            f"Secret file {path} is missing or empty. "
            "Fix: restore the worker secret file and rerun apply."
        ) from exc
    if not password:
        raise WorkerSettingsError(
            f"Secret file {path} is missing or empty. "
            "Fix: restore the worker secret file and rerun apply."
        )
    return password


def _http_url(environ: Mapping[str, str], name: str) -> str:
    value = _required(environ, name)
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "http" and bool(parsed.hostname)
            and parsed.port is not None and 1 <= parsed.port <= 65_535
            and parsed.username is None and parsed.password is None
            and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
            and not any(character.isspace() for character in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise WorkerSettingsError(
            f"Environment variable {name} must be an HTTP URL with a host and TCP port. "
            "Fix: rerun sudo python3 -m gideon apply."
        )
    return value


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Validate the environment and mounted secrets without retaining their values."""

    values = os.environ if environ is None else environ
    port = _positive_integer(values, DATABASE_PORT_ENV)
    if port > 65_535:
        raise WorkerSettingsError(
            f"Environment variable {DATABASE_PORT_ENV} must be a valid TCP port. "
            "Fix: rerun sudo python3 -m gideon apply."
        )
    password_file = Path(_required(values, PASSWORD_FILE_ENV))
    read_password(password_file)
    opensearch_url = _http_url(values, OPENSEARCH_URL_ENV)
    opensearch_user = _required(values, OPENSEARCH_USER_ENV)
    opensearch_password_file = Path(_required(values, OPENSEARCH_PASSWORD_FILE_ENV))
    read_password(opensearch_password_file)
    qdrant_url = _http_url(values, QDRANT_URL_ENV)
    qdrant_key_file = Path(_required(values, QDRANT_KEY_FILE_ENV))
    read_password(qdrant_key_file)
    embed_url = _http_url(values, EMBED_URL_ENV)
    embed_key_file = Path(_required(values, EMBED_KEY_FILE_ENV))
    read_password(embed_key_file)
    return Settings(
        database_host=_required(values, DATABASE_HOST_ENV),
        database_port=port,
        database_name=_required(values, DATABASE_NAME_ENV),
        database_role=_required(values, DATABASE_ROLE_ENV),
        password_file=password_file,
        concurrency=_positive_integer(values, CONCURRENCY_ENV),
        opensearch_url=opensearch_url,
        opensearch_user=opensearch_user,
        opensearch_password_file=opensearch_password_file,
        qdrant_url=qdrant_url,
        qdrant_key_file=qdrant_key_file,
        embed_url=embed_url,
        embed_key_file=embed_key_file,
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


def opensearch_auth(settings: Settings) -> tuple[str, str]:
    """Resolve the mounted store credential for each new lexical client."""

    return settings.opensearch_user, read_password(settings.opensearch_password_file)


def qdrant_key(settings: Settings) -> str:
    """Resolve the mounted key for each new vector store client."""

    return read_password(settings.qdrant_key_file)


def embed_key(settings: Settings) -> str:
    """Resolve the mounted key for each new embedding client."""

    return read_password(settings.embed_key_file)
