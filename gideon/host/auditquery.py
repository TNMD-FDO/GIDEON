"""Read the office's audit log by knowledge base, user, or chat."""

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gideon.guardrail.writer import bounded_id
from gideon.host import audit, report, site, stack
from gideon.host.render.worker import (
    METRICS_ROLE,
    WORKER_DATABASE_HOST,
    WORKER_DATABASE_NAME,
)
from gideon.host.report import Problem
from gideon.host.stages import site_problem
from gideon.host.sysio import Host, PathLike, RealHost

_COMMAND: Final = "audit query"
_SITE_PATH: Final = "/etc/gideon/site.yaml"
_RENDERED_DIR: Final = "/etc/gideon/rendered"
_LOCAL_TIME: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?: [0-9]{2}:[0-9]{2})?")


def _refuse(problem: Problem) -> int:
    print(report.refusal(_COMMAND, problem.problem, problem.fix), file=sys.stderr)
    return 1


def _parse_bound(text: str | None, flag: str, zone: ZoneInfo) -> datetime | None | Problem:
    if text is None:
        return None
    form = "YYYY-MM-DD or YYYY-MM-DD HH:MM"
    invalid = Problem(
        f"{flag} must be an office time in {form} form.",
        f"Run {report.command(_COMMAND)} with {flag} in {form} form.",
    )
    if not _LOCAL_TIME.fullmatch(text):
        return invalid
    try:
        local = datetime.fromisoformat(text)
        if flag == "--until" and " " not in text:
            local += timedelta(days=1)
        first = local.replace(tzinfo=zone, fold=0).astimezone(UTC)
        second = local.replace(tzinfo=zone, fold=1).astimezone(UTC)
        round_trip = first.astimezone(zone).replace(tzinfo=None)
    except (ValueError, OverflowError):
        return invalid

    fix = f"Run {report.command(_COMMAND)} with an unambiguous office time for {flag}."
    if round_trip != local:
        return Problem(f"{flag} does not exist in office time because the clock changes.", fix)
    if first != second:
        return Problem(f"{flag} occurs twice in office time because the clock changes.", fix)
    return first


def _statement(args: argparse.Namespace, zone: ZoneInfo) -> str | Problem:
    bindings: list[str] = []
    predicates: list[str] = []
    for name, value, predicate in (
        ("kb", args.kb, ":'v_kb' = ANY(kb_ids)"),
        ("user", args.user, "user_id = :'v_user'"),
        ("chat", args.chat, "chat_id = :'v_chat'"),
    ):
        if value is None:
            continue
        if bounded_id(value) is None:
            return Problem(
                f"--{name} must be a valid id.",
                f"Run {report.command(_COMMAND)} with a valid --{name} id.",
            )
        bindings.append(audit.set_variable(f"v_{name}", value))
        predicates.append(predicate)
    if not predicates:
        return Problem(
            "at least one of --kb, --user, or --chat is required.",
            f"Run {report.command(_COMMAND)} with a knowledge-base, user, or chat id.",
        )

    since = _parse_bound(args.since, "--since", zone)
    if isinstance(since, Problem):
        return since
    until = _parse_bound(args.until, "--until", zone)
    if isinstance(until, Problem):
        return until
    if since is not None and until is not None and until < since:
        return Problem(
            "--until is before --since.",
            f"Run {report.command(_COMMAND)} with a later --until or an earlier --since.",
        )
    for name, bound, operator in (("since", since, ">="), ("until", until, "<")):
        if bound is not None:
            bindings.append(audit.set_variable(f"v_{name}", bound.isoformat()))
            predicates.append(f"at {operator} :'v_{name}'::timestamptz")

    return "\n".join((
        *bindings,
        "SELECT row_to_json(entry)",
        "FROM public.audit_log AS entry",
        "WHERE " + " AND ".join(predicates),
        "ORDER BY at, id;",
        "",
    ))


def _reader_problem(detail: str) -> Problem:
    return Problem(detail, f"Check the Postgres service with {report.command('status')}, then retry.")


def _read_rows(io: Host, rendered_dir: PathLike, sql: str) -> list[str] | Problem:
    argv = stack.exec_argv(
        rendered_dir, WORKER_DATABASE_HOST, "psql", "-U", METRICS_ROLE,
        "-d", WORKER_DATABASE_NAME, "-X", "-v", "ON_ERROR_STOP=1",
        "-v", "VERBOSITY=sqlstate", "-tA", "-f", "-",
    )
    try:
        result = io.run(argv, input=sql)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return _reader_problem("audit log reader failed: command could not run.")
    if result.returncode != 0:
        if "42P01" in result.stderr.split():  # undefined_table
            return Problem("the audit log table is missing.", f"Run {report.command('apply')}, then retry.")
        return _reader_problem(f"audit log reader failed: exit {result.returncode}.")
    return [line for line in result.stdout.split("\n") if line.strip()]


def _field(value: object) -> str:
    if value is not None and not isinstance(value, (str, int, float)):
        raise ValueError("expected an audit field")
    return "-" if value is None else report.one_line(value)


def _text_row(row: dict[str, object], zone: ZoneInfo) -> str:
    at, kind, kb_ids = row["at"], row["kind"], row["kb_ids"]
    if not isinstance(at, str) or not isinstance(kind, str) or not isinstance(kb_ids, list):
        raise ValueError("expected an audit row")
    instant = datetime.fromisoformat(at)
    if instant.utcoffset() is None:
        raise ValueError("expected an aware audit time")
    knowledge = ",".join(kb_ids) or "-"
    line = (
        f"{instant.astimezone(zone).isoformat()} {report.one_line(kind)} "
        f"user={_field(row['user_id'])} chat={_field(row['chat_id'])} kb={report.one_line(knowledge)}"
    )
    if kind not in {"research_turn", "conflicts_wall_denial"}:
        return line
    detail = row["detail"]
    if not isinstance(detail, dict):
        raise ValueError("expected audit details")
    line += f" result={_field(detail.get('result'))}"
    if kind == "conflicts_wall_denial":
        return f"{line} reason={_field(detail.get('reason'))}"
    returned = detail.get("returned", [])
    documents = {hit["doc_id"] for hit in returned if hit["doc_id"] is not None}
    chunks = {hit["chunk_id"] for hit in returned if hit["chunk_id"] is not None}
    return (
        f"{line} outcome={_field(detail.get('outcome'))} route={_field(detail.get('route'))} "
        f"support_level={_field(detail.get('support_level'))} "
        f"documents={len(documents)} chunks={len(chunks)} latency_ms={_field(detail.get('latency_ms'))}"
    )


def _format_rows(raw: list[str], zone: ZoneInfo, *, as_json: bool) -> str | Problem:
    try:
        rows = [json.loads(line) for line in raw]
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError("expected audit objects")
        if as_json:
            # Keep the record's JSON values intact, including decimal precision.
            return "[" + ",".join(raw) + "]"
        if not rows:
            return "No audit rows matched."
        count = f"{len(rows)} audit {'row' if len(rows) == 1 else 'rows'}."
        return "\n".join([*(_text_row(row, zone) for row in rows), count])
    except (KeyError, OverflowError, RecursionError, TypeError, ValueError):
        return _reader_problem("audit log reader returned an unreadable row.")


def run_audit_query(
    args: argparse.Namespace, *, host: Host | None = None, rendered_dir: PathLike | None = None,
    site_path: PathLike | None = None,
) -> int:
    """Read and print the audit rows matching the supplied selectors and bounds."""

    io = RealHost() if host is None else host
    if io.geteuid() != 0:
        return _refuse(Problem(
            "root is required.",
            f"Run {report.command(_COMMAND, sudo=False)} as root, for example with sudo.",
        ))
    path = Path(_SITE_PATH if site_path is None else site_path)
    loaded = site.load_site(path, host=io)
    if loaded.errors or loaded.config is None:
        return _refuse(Problem(
            site_problem(loaded) or "the site file is invalid.",
            f"Correct the site file, then run {report.command(_COMMAND)} again.",
        ))
    try:
        zone = ZoneInfo(loaded.config.office.timezone)
    except ZoneInfoNotFoundError:
        return _refuse(Problem(
            "the office timezone could not be loaded.",
            f"Install timezone data or correct office.timezone in {path}, then run {report.command(_COMMAND)} again.",
        ))

    sql = _statement(args, zone)
    if isinstance(sql, Problem):
        return _refuse(sql)
    rows = _read_rows(io, _RENDERED_DIR if rendered_dir is None else rendered_dir, sql)
    if isinstance(rows, Problem):
        return _refuse(rows)
    output = _format_rows(rows, zone, as_json=args.json)
    if isinstance(output, Problem):
        return _refuse(output)
    print(output)
    return 0
