"""Validate the egress door's allowlist, listener, and parent proxy settings."""

import base64
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import unquote_to_bytes, urlsplit

HOSTS_ENV: Final = "GIDEON_EGRESS_HOSTS"
PORT_ENV: Final = "GIDEON_EGRESS_PORT"
PARENT_PROXY_ENV: Final = "GIDEON_EGRESS_PARENT_PROXY"

_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_FIX = "Fix: rerun sudo python3 -m gideon apply."


class EgressSettingsError(ValueError):
    """A content-free, actionable startup refusal."""


@dataclass(frozen=True, slots=True)
class ParentProxy:
    """A parent proxy's connection address and encoded Basic credential."""

    host: str
    port: int
    use_tls: bool
    authorization: str | None


@dataclass(frozen=True, slots=True)
class Settings:
    """The allowed DNS names, listener port, and optional parent proxy."""

    hosts: frozenset[str]
    port: int
    parent_proxy: ParentProxy | None


def normalize_host(value: str) -> str:
    """Fold one DNS name and refuse names outside the allowlist's syntax."""

    host = value.casefold().removesuffix(".")
    if (
        not host
        or len(host) > 253
        or any(not _HOST_LABEL.fullmatch(label) for label in host.split("."))
    ):
        raise ValueError("invalid DNS host")
    return host


def _port(value: str, name: str) -> int:
    if not value.isascii() or not value.isdecimal():
        raise EgressSettingsError(
            f"Environment variable {name} must be a valid TCP port. {_FIX}"
        )
    try:
        port = int(value)
    except ValueError as exc:
        raise EgressSettingsError(
            f"Environment variable {name} must be a valid TCP port. {_FIX}"
        ) from exc
    if not 1 <= port <= 65_535:
        raise EgressSettingsError(
            f"Environment variable {name} must be a valid TCP port. {_FIX}"
        )
    return port


def _parent(value: str) -> ParentProxy:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or parsed.hostname is None
        ):
            raise ValueError("invalid parent URL")
        host = parsed.hostname
        # An omitted port is the scheme's, as the renderer and every client read it.
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65_535:
            raise ValueError("invalid parent port")
        authorization = None
        if parsed.username is not None:
            if parsed.password is None:
                raise ValueError("incomplete parent user-info")
            user = unquote_to_bytes(parsed.username).decode("utf-8")
            password = unquote_to_bytes(parsed.password).decode("utf-8")
            if not user or not password or ":" in user:
                raise ValueError("invalid parent user-info")
            encoded = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
            authorization = f"Basic {encoded}"
        return ParentProxy(host, port, parsed.scheme == "https", authorization)
    except (ValueError, UnicodeError) as exc:
        raise EgressSettingsError(
            f"Environment variable {PARENT_PROXY_ENV} must be an HTTP(S) proxy "
            f"URL with a host. {_FIX}"
        ) from exc


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Read the rendered environment without including its values in refusals."""

    values = os.environ if environ is None else environ
    raw_hosts = values.get(HOSTS_ENV, "")
    if not raw_hosts.strip():
        raise EgressSettingsError(
            f"Environment variable {HOSTS_ENV} is missing or empty. {_FIX}"
        )
    try:
        hosts = frozenset(normalize_host(part.strip()) for part in raw_hosts.split(","))
    except ValueError as exc:
        raise EgressSettingsError(
            f"Environment variable {HOSTS_ENV} must contain DNS host names. {_FIX}"
        ) from exc
    raw_port = values.get(PORT_ENV, "").strip()
    if not raw_port:
        raise EgressSettingsError(
            f"Environment variable {PORT_ENV} is missing or empty. {_FIX}"
        )
    port = _port(raw_port, PORT_ENV)
    raw_parent = values.get(PARENT_PROXY_ENV, "").strip()
    return Settings(hosts, port, _parent(raw_parent) if raw_parent else None)
