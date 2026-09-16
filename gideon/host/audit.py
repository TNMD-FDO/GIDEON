"""Append-only audit transport for §19.4.

Audit rows contain ids and identifiers only, never document or query text.
The writer therefore sends its SQL through the Postgres container's stdin
seam and never places row values in argv or an environment.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import gideon
from gideon.host import stack
from gideon.host.sysio import Host, PathLike

_AUDIT_ROLE: Final[str] = "gideon_audit"
_AUDIT_DATABASE: Final[str] = "gideon"
_POSTGRES_SERVICE: Final[str] = "postgres"
# One batch is one transaction: a snapshot is written whole or not at all.
_PSQL_FLAGS: Final[tuple[str, ...]] = ("-v", "ON_ERROR_STOP=1", "--single-transaction", "-f", "-")


@dataclass(frozen=True, slots=True)
class AuditRow:
    """One §19.4 row whose fields are identifiers rather than content."""

    run_id: str
    kind: str
    actor_user_id: str | None
    user_id: str | None
    chat_id: str | None
    kb_ids: tuple[str, ...]
    detail: Mapping[str, object]


def _argv(rendered_dir: PathLike) -> list[str]:
    return stack.exec_argv(
        rendered_dir,
        _POSTGRES_SERVICE,
        "psql",
        "-U",
        _AUDIT_ROLE,
        "-d",
        _AUDIT_DATABASE,
        *_PSQL_FLAGS,
    )


def _has_forbidden_text(value: object) -> bool:
    if isinstance(value, str):
        return any(character in value for character in "\r\n\x00")
    if isinstance(value, Mapping):
        return any(
            _has_forbidden_text(key) or _has_forbidden_text(item)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return any(_has_forbidden_text(item) for item in value)
    return False


def _escape(value: str) -> str:
    return value.replace("'", "''")


def _set_variable(name: str, value: str) -> str:
    return f"\\set {name} '{_escape(value)}'"


def _array_literal(values: tuple[str, ...]) -> str:
    # Every element is quoted, so commas and braces in an identifier remain
    # data.  Backslashes and double quotes are the two array-string escapes.
    escaped = (
        '"'
        + value.replace("\\", "\\\\").replace('"', '\\"')
        + '"'
        for value in values
    )
    return "{" + ",".join(escaped) + "}"


def _sql_value(variable: str, value: str | None, lines: list[str]) -> str:
    if value is None:
        return "NULL"
    lines.append(_set_variable(variable, value))
    return f":'{variable}'"


def _row_sql(row: AuditRow) -> str:
    lines: list[str] = []
    if _has_forbidden_text(row.run_id):
        raise ValueError("run_id contains CR, LF, or NUL")
    if _has_forbidden_text(row.kind):
        raise ValueError("kind contains CR, LF, or NUL")
    if _has_forbidden_text(row.actor_user_id):
        raise ValueError("actor_user_id contains CR, LF, or NUL")
    if _has_forbidden_text(row.user_id):
        raise ValueError("user_id contains CR, LF, or NUL")
    if _has_forbidden_text(row.chat_id):
        raise ValueError("chat_id contains CR, LF, or NUL")
    if _has_forbidden_text(row.kb_ids):
        raise ValueError("kb_ids contains CR, LF, or NUL")
    if _has_forbidden_text(row.detail):
        raise ValueError("detail contains CR, LF, or NUL")

    lines.append(_set_variable("v_run_id", row.run_id))
    lines.append(_set_variable("v_kind", row.kind))
    actor = _sql_value("v_actor", row.actor_user_id, lines)
    user = _sql_value("v_user", row.user_id, lines)
    chat = _sql_value("v_chat", row.chat_id, lines)
    lines.append(_set_variable("v_kb", _array_literal(row.kb_ids)))
    lines.append(_set_variable("v_release", gideon.__version__))
    try:
        detail = json.dumps(dict(row.detail), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("detail is not JSON-serializable") from exc
    if _has_forbidden_text(detail):
        raise ValueError("detail contains CR, LF, or NUL")
    lines.append(_set_variable("v_detail", detail))
    lines.append(
        "INSERT INTO audit_log "
        "(run_id, kind, actor_user_id, user_id, chat_id, kb_ids, release, detail) "
        f"VALUES (:'v_run_id'::uuid, :'v_kind', {actor}, {user}, {chat}, "
        ":'v_kb'::text[], :'v_release', :'v_detail'::jsonb);"
    )
    return "\n".join(lines)


def _run(io: Host, rendered_dir: PathLike, sql: str) -> str | None:
    try:
        result = io.run(_argv(rendered_dir), input=sql)
    except OSError as exc:
        return f"audit writer failed: {exc}"
    if result.returncode != 0:
        # psql may quote the SQL line containing a row value in stderr.  The
        # exit status is sufficient and keeps identifiers out of diagnostics.
        return f"audit writer failed: exit {result.returncode}"
    return None


def write_rows(io: Host, rendered_dir: PathLike, rows: Sequence[AuditRow]) -> str | None:
    """Insert *rows* after ensuring the current audit partition exists."""

    lines = ["SELECT audit_log_ensure_partition(now());"]
    try:
        lines.extend(_row_sql(row) for row in rows)
    except ValueError as exc:
        return f"audit row refused: {exc}"
    return _run(io, rendered_dir, "\n".join(lines) + "\n")


def probe(io: Host, rendered_dir: PathLike) -> str | None:
    """Verify that the append-only audit role can reach its database."""

    return _run(io, rendered_dir, "SELECT 1;\n")
