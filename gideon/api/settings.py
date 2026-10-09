"""The ten startup settings for the GIDEON API service."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    """The immutable runtime settings and credentials for one service process."""

    engine_url: str  # the engine's OpenAI base URL, ending in /v1
    engine_api_key: str = field(repr=False)  # value withheld from the repr
    api_key: str = field(repr=False)  # value withheld from the repr
    port: int
    source_header: str  # the forwarded header the trip's source is decided on
    chat_header: str  # the forwarded header holding the trip's chat id
    eval_identity: str  # the value under that header that reads as the eval identity
    instruction: str  # rendered file text, with surrounding whitespace stripped
    model_id: str  # the service's listed and accepted model id
    engine_model: str  # the engine's served name for upstream requests


def _required_environment(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Environment variable {name} is missing or empty.")
    return value


def _read_file(environ: Mapping[str, str], variable: str, kind: str) -> str:
    path = Path(_required_environment(environ, variable))
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{kind} file {path} is missing or empty.") from exc
    if not value:
        raise ValueError(f"{kind} file {path} is missing or empty.")
    return value


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Read the service environment, mounted secrets, and rendered instruction."""

    values = os.environ if environ is None else environ
    port_text = _required_environment(values, "GIDEON_API_PORT")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("Environment variable GIDEON_API_PORT must be an integer.") from exc
    if not 1 <= port <= 65_535:
        raise ValueError("Environment variable GIDEON_API_PORT must be a valid TCP port.")
    return Settings(
        engine_url=_required_environment(values, "GIDEON_ENGINE_URL").rstrip("/"),
        engine_api_key=_read_file(values, "GIDEON_ENGINE_API_KEY_FILE", "Secret"),
        api_key=_read_file(values, "GIDEON_API_KEY_FILE", "Secret"),
        port=port,
        source_header=_required_environment(values, "GIDEON_SOURCE_HEADER"),
        chat_header=_required_environment(values, "GIDEON_CHAT_HEADER"),
        eval_identity=_required_environment(values, "GIDEON_EVAL_IDENTITY"),
        instruction=_read_file(values, "GIDEON_INSTRUCTION_FILE", "Instruction"),
        model_id=_required_environment(values, "GIDEON_MODEL_ID"),
        engine_model=_required_environment(values, "GIDEON_ENGINE_MODEL"),
    )
