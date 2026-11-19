"""Append and read maintenance window events and derive their current phase."""

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import gideon
from gideon.host import audit, report, stack
from gideon.host.render.worker import (
    METRICS_ROLE,
    WORKER_DATABASE_HOST,
    WORKER_DATABASE_NAME,
)
from gideon.host.sysio import Host, PathLike


@dataclass(frozen=True, slots=True)
class WindowEvent:
    """One event in the maintenance window record."""

    id: int
    key: str
    at: datetime
    act: str
    starts_at: datetime | None
    ends_at: datetime | None
    actor: str


@dataclass(frozen=True, slots=True)
class Window:
    """The span and provenance of a standing maintenance window."""

    key: str
    starts_at: datetime
    ends_at: datetime
    recorded_at: datetime
    actor: str


def append(
    io: Host,
    rendered_dir: PathLike,
    *,
    key: str,
    act: Literal["set", "withdraw"],
    actor: str,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> report.Problem | None:
    """Append one event as the audit role, ensuring its partition in the same transaction."""

    if any(character in key + act + actor for character in "\r\n\x00"):
        return report.Problem(
            "maintenance window identifiers contain a control character.",
            "Use identifiers without control characters, then retry.",
        )
    if any(instant is not None and instant.utcoffset() is None for instant in (starts_at, ends_at)):
        return report.Problem(
            "maintenance window times require a timezone.",
            "Supply timezone-aware maintenance window times, then retry.",
        )

    sql = "\n".join((
        "SELECT public.maintenance_window_events_ensure_partition(now());",
        audit.set_variable("v_key", key),
        audit.set_variable("v_act", act),
        audit.set_variable("v_starts_at", starts_at.isoformat() if starts_at is not None else ""),
        audit.set_variable("v_ends_at", ends_at.isoformat() if ends_at is not None else ""),
        audit.set_variable("v_actor", actor),
        audit.set_variable("v_release", gideon.__version__),
        """INSERT INTO public.maintenance_window_events (key, act, starts_at, ends_at, actor, release)
VALUES (:'v_key'::uuid, :'v_act', NULLIF(:'v_starts_at', '')::timestamptz,
        NULLIF(:'v_ends_at', '')::timestamptz, :'v_actor', :'v_release');
""",
    ))
    fix = f"Run {stack.logs_fix(rendered_dir, WORKER_DATABASE_HOST)}, then retry."
    try:
        result = io.run(audit.audit_argv(rendered_dir), input=sql)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return report.Problem("maintenance window writer failed: command could not run", fix)
    if result.returncode != 0:
        return report.Problem(f"maintenance window writer failed: exit {result.returncode}", fix)
    return None


def _read_instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("expected an instant")
    instant = datetime.fromisoformat(value)
    if instant.utcoffset() is None:
        raise ValueError("expected an aware instant")
    return instant.astimezone(UTC)


def _read_row(reply: str) -> WindowEvent:
    row = json.loads(reply)
    if not isinstance(row, dict):
        raise ValueError("expected an event object")
    event_id, key, act, actor = row["id"], row["key"], row["act"], row["actor"]
    if (
        type(event_id) is not int or event_id < 1
        or not isinstance(key, str) or not isinstance(actor, str)
    ):
        raise ValueError("invalid event identifiers")
    UUID(key)
    at = _read_instant(row["at"])
    starts_at = None if row["starts_at"] is None else _read_instant(row["starts_at"])
    ends_at = None if row["ends_at"] is None else _read_instant(row["ends_at"])
    if act == "set":
        if starts_at is None or ends_at is None or starts_at >= ends_at:
            raise ValueError("invalid set span")
    elif act != "withdraw" or starts_at is not None or ends_at is not None:
        raise ValueError("invalid event act or span")
    return WindowEvent(event_id, key, at, act, starts_at, ends_at, actor)


def _read_event(
    io: Host, rendered_dir: PathLike, *, key: str | None = None, timeout: float | None = None,
) -> WindowEvent | None | report.Problem:
    fix = f"Run {report.command('apply')}, then retry."
    binding = ""
    where = ""
    if key is not None:
        try:
            UUID(key)
        except ValueError:
            return report.Problem("maintenance window key is invalid.", "Use the event's UUID, then retry.")
        binding = audit.set_variable("v_key", key) + "\n"
        where = "WHERE key = :'v_key'::uuid\n"

    sql = f"""{binding}SELECT row_to_json(event) FROM (
    SELECT id, key, at, act, starts_at, ends_at, actor
    FROM public.maintenance_window_events
    {where}ORDER BY id DESC LIMIT 1
) AS event;
"""
    argv = stack.exec_argv(
        rendered_dir, WORKER_DATABASE_HOST, "psql", "-U", METRICS_ROLE,
        "-d", WORKER_DATABASE_NAME, "-X", "-v", "ON_ERROR_STOP=1",
        "-v", "VERBOSITY=sqlstate", "-tA", "-f", "-",
    )
    try:
        result = io.run(argv, input=sql, timeout=timeout)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return report.Problem("maintenance window reader failed: command could not run", fix)
    if result.returncode != 0:
        if "42P01" in result.stderr.split():  # undefined_table
            return report.Problem("the maintenance window event table is missing.", fix)
        return report.Problem(f"maintenance window reader failed: exit {result.returncode}", fix)
    if not result.stdout.strip():
        return None
    try:
        return _read_row(result.stdout)
    except (ValueError, KeyError, OverflowError):
        return report.Problem("maintenance window reader returned an unreadable event.", fix)


def newest(
    io: Host, rendered_dir: PathLike, *, timeout: float | None = None,
) -> WindowEvent | None | report.Problem:
    """Read the newest event by id as the metrics role, with an optional timeout."""

    return _read_event(io, rendered_dir, timeout=timeout)


def landed(io: Host, rendered_dir: PathLike, key: str) -> WindowEvent | None | report.Problem:
    """Read back the event with the supplied key as the metrics role."""

    return _read_event(io, rendered_dir, key=key)


def standing_window(event: WindowEvent | None, now: datetime) -> Window | None:
    """Return the window from a set event whose end is strictly after now."""

    if event is None or event.act != "set" or event.starts_at is None or event.ends_at is None:
        return None
    if event.ends_at.astimezone(UTC) <= now.astimezone(UTC):
        return None
    return Window(event.key, event.starts_at, event.ends_at, event.at, event.actor)


def phase(window: Window, now: datetime) -> Literal["ahead", "under-way"]:
    """Return the standing window's phase, with its start counted as under way."""

    if now.astimezone(UTC) < window.starts_at.astimezone(UTC):
        return "ahead"
    return "under-way"
