"""The startup settings for the GIDEON API service."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

_RERANK_URL_ENV = "GIDEON_API_RERANK_URL"
_RERANK_KEY_FILE_ENV = "GIDEON_API_RERANK_KEY_FILE"
# The rendered names are held to these by the render round-trip test.
_COURTS_SCOTUS_ENV = "GIDEON_API_COURTS_SCOTUS"
_COURTS_CIRCUIT_ENV = "GIDEON_API_COURTS_CIRCUIT"
_COURTS_DISTRICTS_ENV = "GIDEON_API_COURTS_DISTRICTS"
_COURTS_STATES_ENV = "GIDEON_API_COURTS_STATES"
_COURTS_FILE_ENV = "GIDEON_API_COURTS_FILE"
# The court map's ids: CourtListener's, mixed case in places (txctapp13A), hyphenated in fixtures.
_COURT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,31}")


@dataclass(frozen=True, slots=True)
class RecordCoordinates:
    """The Postgres address and the path to the reader's mounted password."""

    host: str
    port: int
    database_name: str
    role: str
    password_file: Path


@dataclass(frozen=True, slots=True)
class Settings:
    """The immutable runtime settings and credentials for one service process."""

    engine_url: str  # the engine's OpenAI base URL, ending in /v1
    engine_api_key: str = field(repr=False)  # value withheld from the repr
    api_key: str = field(repr=False)  # value withheld from the repr
    port: int
    source_header: str  # the forwarded header the trip's source is decided on
    chat_header: str  # the forwarded header holding the trip's chat id
    user_header: str  # the forwarded header holding the search turn's user id
    eval_identity: str  # the value under that header that reads as the eval identity
    instruction: str  # rendered file text, with surrounding whitespace stripped
    model_id: str  # the service's listed and accepted model id
    engine_model: str  # the engine's served name for upstream requests
    record: RecordCoordinates
    corpus_record: RecordCoordinates
    cas_root: Path
    qdrant_url: str
    qdrant_key_file: Path
    embed_url: str
    embed_key_file: Path
    opensearch_url: str
    opensearch_user: str
    opensearch_password_file: Path
    embed_model: str
    embed_repository: str
    embed_space: str
    embed_dimensions: int
    courts_scotus: tuple[str, ...]
    courts_circuit: tuple[str, ...]
    courts_districts: tuple[str, ...]
    courts_states: tuple[str, ...]
    courts_file: Path
    rerank_url: str | None = None
    rerank_key_file: Path | None = None


def _required_environment(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Environment variable {name} is missing or empty.")
    return value


def _read_file(path: Path, kind: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        raise ValueError(f"{kind} file {path} is missing or empty.") from None
    if not value:
        raise ValueError(f"{kind} file {path} is missing or empty.")
    return value


def _integer_environment(environ: Mapping[str, str], name: str) -> int:
    value = _required_environment(environ, name)
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer.") from exc


def _tcp_port(environ: Mapping[str, str], name: str) -> int:
    port = _integer_environment(environ, name)
    if not 1 <= port <= 65_535:
        raise ValueError(f"Environment variable {name} must be a valid TCP port.")
    return port


def _secret_file(environ: Mapping[str, str], name: str) -> Path:
    path = Path(_required_environment(environ, name))
    _read_file(path, "Secret")
    return path


def _http_url(environ: Mapping[str, str], name: str) -> str:
    value = _required_environment(environ, name)
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
        raise ValueError(f"Environment variable {name} must be an HTTP URL with a host and TCP port.")
    return value


def _record_coordinates(environ: Mapping[str, str], prefix: str) -> RecordCoordinates:
    host = _required_environment(environ, f"{prefix}_DB_HOST")
    port = _tcp_port(environ, f"{prefix}_DB_PORT")
    database_name = _required_environment(environ, f"{prefix}_DB_NAME")
    role = _required_environment(environ, f"{prefix}_DB_ROLE")
    password_file = _secret_file(environ, f"{prefix}_PASSWORD_FILE")
    return RecordCoordinates(host, port, database_name, role, password_file)


def _court_groups(environ: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    groups: dict[str, tuple[str, ...]] = {}
    seen: set[str] = set()
    for name in (_COURTS_SCOTUS_ENV, _COURTS_CIRCUIT_ENV, _COURTS_DISTRICTS_ENV, _COURTS_STATES_ENV):
        if name == _COURTS_STATES_ENV:
            raw = environ.get(name)
            if raw is None:
                raise ValueError(f"Environment variable {name} is missing.")
            value = raw.strip()
        else:
            value = _required_environment(environ, name)
        courts = tuple(value.split(",")) if value else ()
        if any(_COURT_ID.fullmatch(court) is None for court in courts):
            raise ValueError(f"Environment variable {name} must contain comma-separated court ids.")
        if name == _COURTS_CIRCUIT_ENV and len(courts) != 1:
            raise ValueError(f"Environment variable {name} must contain exactly one court id.")
        if seen.intersection(courts):
            raise ValueError(f"Environment variable {name} overlaps an earlier court group.")
        groups[name] = courts
        seen.update(courts)
    return groups


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Read the service environment, mounted secrets, and rendered instruction."""

    values = os.environ if environ is None else environ
    port = _tcp_port(values, "GIDEON_API_PORT")
    cas_root = Path(_required_environment(values, "GIDEON_API_CAS_ROOT"))
    if not cas_root.is_absolute():
        raise ValueError("Environment variable GIDEON_API_CAS_ROOT must be an absolute path.")
    dimensions_name = "GIDEON_API_EMBED_DIMENSIONS"
    embed_dimensions = _integer_environment(values, dimensions_name)
    if embed_dimensions <= 0:
        raise ValueError(f"Environment variable {dimensions_name} must be positive.")
    court_groups = _court_groups(values)
    courts_file = Path(_required_environment(values, _COURTS_FILE_ENV))
    _read_file(courts_file, "Court")
    rerank_url = None
    rerank_key_file = None
    if _RERANK_URL_ENV in values or _RERANK_KEY_FILE_ENV in values:
        _required_environment(values, _RERANK_URL_ENV)
        _required_environment(values, _RERANK_KEY_FILE_ENV)
        rerank_url = _http_url(values, _RERANK_URL_ENV)
        rerank_key_file = _secret_file(values, _RERANK_KEY_FILE_ENV)
    return Settings(
        engine_url=_required_environment(values, "GIDEON_ENGINE_URL").rstrip("/"),
        engine_api_key=_read_file(
            Path(_required_environment(values, "GIDEON_ENGINE_API_KEY_FILE")), "Secret",
        ),
        api_key=_read_file(Path(_required_environment(values, "GIDEON_API_KEY_FILE")), "Secret"),
        port=port,
        source_header=_required_environment(values, "GIDEON_SOURCE_HEADER"),
        chat_header=_required_environment(values, "GIDEON_CHAT_HEADER"),
        user_header=_required_environment(values, "GIDEON_USER_HEADER"),
        eval_identity=_required_environment(values, "GIDEON_EVAL_IDENTITY"),
        instruction=_read_file(
            Path(_required_environment(values, "GIDEON_INSTRUCTION_FILE")), "Instruction",
        ),
        model_id=_required_environment(values, "GIDEON_MODEL_ID"),
        engine_model=_required_environment(values, "GIDEON_ENGINE_MODEL"),
        record=_record_coordinates(values, "GIDEON_API"),
        corpus_record=_record_coordinates(values, "GIDEON_API_CORPUS"),
        cas_root=cas_root,
        qdrant_url=_http_url(values, "GIDEON_API_QDRANT_URL"),
        qdrant_key_file=_secret_file(values, "GIDEON_API_QDRANT_KEY_FILE"),
        embed_url=_http_url(values, "GIDEON_API_EMBED_URL"),
        embed_key_file=_secret_file(values, "GIDEON_API_EMBED_KEY_FILE"),
        opensearch_url=_http_url(values, "GIDEON_API_OPENSEARCH_URL"),
        opensearch_user=_required_environment(values, "GIDEON_API_OPENSEARCH_USER"),
        opensearch_password_file=_secret_file(values, "GIDEON_API_OPENSEARCH_PASSWORD_FILE"),
        embed_model=_required_environment(values, "GIDEON_API_EMBED_MODEL"),
        embed_repository=_required_environment(values, "GIDEON_API_EMBED_REPOSITORY"),
        embed_space=_required_environment(values, "GIDEON_API_EMBED_SPACE"),
        embed_dimensions=embed_dimensions,
        courts_scotus=court_groups[_COURTS_SCOTUS_ENV],
        courts_circuit=court_groups[_COURTS_CIRCUIT_ENV],
        courts_districts=court_groups[_COURTS_DISTRICTS_ENV],
        courts_states=court_groups[_COURTS_STATES_ENV],
        courts_file=courts_file,
        rerank_url=rerank_url,
        rerank_key_file=rerank_key_file,
    )
