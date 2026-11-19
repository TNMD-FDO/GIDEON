"""Record, withdraw, and show the office's maintenance window."""

import argparse
import os
import pwd
import re
import sys
import unicodedata
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Final, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gideon.host import announcements, backuplock, report, site, windowrecord
from gideon.host.report import Problem, StageResult
from gideon.host.stages import aware_now
from gideon.host.sysio import Host, LockingHost, PathLike, RealHost

_SET: Final = "window set"
_WITHDRAW: Final = "window withdraw"
_SHOW: Final = "window show"
_SITE_PATH: Final = "/etc/gideon/site.yaml"
_RENDERED_DIR: Final = "/etc/gideon/rendered"
_LOCAL_MINUTE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}")
# exempt: operational starting value. One working day is 24 weekday hours.
_WORKING_DAY: Final = timedelta(hours=24)
# exempt: operational starting value. The announcement's dates omit the year.
_START_LIMIT: Final = timedelta(days=90)
_NOTICE_WARNING: Final = (
    "Warning: announce a maintenance window at least one working day ahead. "
    "See docs/runbooks/install-upgrade.md §2."
)


def _refuse(command_path: str, problem: Problem) -> int:
    print(report.refusal(command_path, problem.problem, problem.fix), file=sys.stderr)
    return 1


def _fail_stage(name: str, problem: Problem) -> int:
    report.print_stage(StageResult(name, False, problem.problem, problem.fix))
    return 1


def _site_clock(
    io: Host, site_path: PathLike, now: datetime | None, command_path: str,
) -> tuple[datetime, ZoneInfo] | Problem:
    if io.geteuid() != 0:
        return Problem(
            "root is required.",
            f"Run {report.command(command_path, sudo=False)} as root, for example with sudo.",
        )
    started = aware_now(now)
    if started is None:
        return Problem("the clock must be timezone-aware.", "Supply a timezone-aware clock value.")
    loaded = site.load_site(Path(site_path), host=io)
    if loaded.errors or loaded.config is None:
        return Problem(site.render_errors(loaded.errors), "Correct the site file, then retry.")
    try:
        zone = ZoneInfo(loaded.config.office.timezone)
    except ZoneInfoNotFoundError:
        return Problem("the office timezone could not be loaded.", f"Install timezone data or correct office.timezone in {site_path}, then retry.")
    return started, zone


def _account(io: Host, actor: str | None, command_path: str) -> str | Problem:
    if actor is None:
        try:
            actor = os.environ.get("SUDO_USER") or pwd.getpwuid(io.geteuid()).pw_name
        except (KeyError, OSError):
            return Problem(
                "the invoking account could not be resolved.",
                f"Use a named Unix account to run {report.command(command_path)}.",
            )
    if not actor or any(unicodedata.category(char) in {"Cc", "Zl", "Zp"} for char in actor):
        return Problem(
            "the invoking account name is empty or contains a control character.",
            f"Run {report.command(command_path)} with a valid Unix account.",
        )
    return actor


def _parse_minute(text: str, flag: str, zone: ZoneInfo) -> datetime | Problem:
    invalid = Problem(
        f"{flag} must be an office time in YYYY-MM-DD HH:MM form.",
        f"Run {report.command(_SET)} with a valid {flag} without seconds or an offset.",
    )
    if not _LOCAL_MINUTE.fullmatch(text):
        return invalid
    try:
        local = datetime.fromisoformat(text)
        first = local.replace(tzinfo=zone, fold=0).astimezone(UTC)
        second = local.replace(tzinfo=zone, fold=1).astimezone(UTC)
        round_trip = first.astimezone(zone).replace(tzinfo=None)
    except (ValueError, OverflowError):
        return invalid

    fix = f"Run {report.command(_SET)} with an unambiguous office time for {flag}."
    if round_trip != local:
        return Problem(f"{flag} does not exist in office time because the clock changes.", fix)
    if first != second:
        return Problem(f"{flag} occurs twice in office time because the clock changes.", fix)
    return first


def _parse_span(
    start_text: str, end_text: str, *, now: datetime, zone: ZoneInfo,
) -> tuple[datetime, datetime] | Problem:
    start = _parse_minute(start_text, "--start", zone)
    if isinstance(start, Problem):
        return start
    end = _parse_minute(end_text, "--end", zone)
    if isinstance(end, Problem):
        return end
    if end <= start:
        return Problem("--end must be after --start.", f"Run {report.command(_SET)} with a later --end.")
    if end <= now:
        return Problem("--end must be after now.", f"Run {report.command(_SET)} with a future --end.")
    if start - now > _START_LIMIT:
        return Problem(
            f"--start is more than {_START_LIMIT.days} days ahead.",
            f"Run {report.command(_SET)} with --start within {_START_LIMIT.days} days.",
        )
    return start, end


def _short_notice(now: datetime, start: datetime, zone: ZoneInfo) -> bool:
    if start <= now:
        return True
    local_now = now.astimezone(zone).replace(tzinfo=None)
    local_start = start.astimezone(zone).replace(tzinfo=None)
    notice = timedelta()
    while local_now.date() < local_start.date() and notice < _WORKING_DAY:
        midnight = datetime.combine(local_now.date() + timedelta(days=1), time.min)
        if local_now.weekday() < 5:
            notice += midnight - local_now
        local_now = midnight
    if local_now.date() == local_start.date() and local_now.weekday() < 5:
        notice += local_start - local_now
    return notice < _WORKING_DAY


def _span_text(start: datetime, end: datetime, zone: ZoneInfo) -> str:
    return f"from {announcements.office_time(start, zone)} until {announcements.office_time(end, zone)}"


def _edit_record(
    io: Host, rendered_dir: PathLike, *, now: datetime, zone: ZoneInfo, actor: str,
    span: tuple[datetime, datetime] | None,
) -> int:
    newest = windowrecord.newest(io, rendered_dir)
    if isinstance(newest, Problem):
        return _fail_stage("window", newest)
    previous = windowrecord.standing_window(newest, now)
    if span is None and previous is None:
        return _fail_stage("window", Problem(
            "no maintenance window stands.", f"Run {report.command(_SET)} to record a maintenance window.",
        ))
    newest_key = newest.key if newest is not None else "none"
    report.print_stage(StageResult("window", True, f"office timezone {zone.key}, newest event {newest_key}", ""))

    key = str(uuid4())
    act: Literal["set", "withdraw"] = "withdraw" if span is None else "set"
    starts_at, ends_at = (None, None) if span is None else span
    problem = windowrecord.append(
        io, rendered_dir, key=key, act=act, actor=actor, starts_at=starts_at, ends_at=ends_at,
    )
    if problem is not None:
        return _fail_stage("record", problem)
    event = windowrecord.landed(io, rendered_dir, key)
    if isinstance(event, Problem):
        # The append committed, so a retry would record a second event.
        return _fail_stage("record", Problem(
            f"event {key} was written but could not be read back: {event.problem}",
            f"Run {report.command(_SHOW)} before recording or withdrawing again.",
        ))
    if event is None or (event.key, event.act) != (key, act):
        return _fail_stage("record", Problem(
            f"event {key} could not be confirmed.",
            f"Run {report.command(_SHOW)} to check the record before retrying.",
        ))

    if event.starts_at is None or event.ends_at is None:
        assert previous is not None
        detail = f"event {event.key} withdrew {previous.key}"
    else:
        detail = f"event {event.key} {_span_text(event.starts_at, event.ends_at, zone)}"
        if previous is not None:
            detail += f", moved {previous.key}"
        if _short_notice(now, event.starts_at, zone):
            detail += f". {_NOTICE_WARNING}"
    report.print_stage(StageResult("record", True, detail, ""))
    return 0


def _run_edit(
    io: LockingHost, rendered_dir: PathLike, site_path: PathLike, *, now: datetime | None,
    actor: str | None, times: tuple[str, str] | None,
) -> int:
    command_path = _WITHDRAW if times is None else _SET
    context = _site_clock(io, site_path, now, command_path)
    if isinstance(context, Problem):
        return _refuse(command_path, context)
    started, zone = context
    account = _account(io, actor, command_path)
    if isinstance(account, Problem):
        return _refuse(command_path, account)
    span = None if times is None else _parse_span(*times, now=started, zone=zone)
    if isinstance(span, Problem):
        return _fail_stage("window", span)

    claim = backuplock.claim(
        io, command=report.command_name(command_path), now=started, lock=backuplock.WINDOW_LOCK,
    )
    if claim.refusal is not None:
        return _refuse(command_path, Problem(claim.refusal.detail, claim.refusal.fix))
    if not claim.taken:
        return _refuse(command_path, Problem(
            "a maintenance window command in this process holds the lock.",
            f"Wait for it to finish, then run {report.command(command_path)} again.",
        ))
    try:
        return _edit_record(io, rendered_dir, now=started, zone=zone, actor=account, span=span)
    finally:
        backuplock.release_claim(io, claim, lock=backuplock.WINDOW_LOCK)


def run_window_set(
    args: argparse.Namespace, *, host: LockingHost | None = None, rendered_dir: PathLike = _RENDERED_DIR,
    site_path: PathLike = _SITE_PATH, now: datetime | None = None, actor: str | None = None,
) -> int:
    """Record a maintenance window or move the window that stands."""

    io = RealHost() if host is None else host
    return _run_edit(io, rendered_dir, site_path, now=now, actor=actor, times=(args.start, args.end))


def run_window_withdraw(
    args: argparse.Namespace, *, host: LockingHost | None = None, rendered_dir: PathLike = _RENDERED_DIR,
    site_path: PathLike = _SITE_PATH, now: datetime | None = None, actor: str | None = None,
) -> int:
    """Withdraw the standing maintenance window under the writer's lock."""

    io = RealHost() if host is None else host
    return _run_edit(io, rendered_dir, site_path, now=now, actor=actor, times=None)


def run_window_show(
    args: argparse.Namespace, *, host: Host | None = None, rendered_dir: PathLike = _RENDERED_DIR,
    site_path: PathLike = _SITE_PATH, now: datetime | None = None,
) -> int:
    """Read the standing window or print its release note sentence."""

    io = RealHost() if host is None else host
    context = _site_clock(io, site_path, now, _SHOW)
    if isinstance(context, Problem):
        return _refuse(_SHOW, context)
    started, zone = context
    newest = windowrecord.newest(io, rendered_dir)
    if isinstance(newest, Problem):
        return _refuse(_SHOW, newest)

    window = windowrecord.standing_window(newest, started)
    if args.note:
        print(announcements.note_line(window, zone))
    elif window is None:
        print("No maintenance window is recorded.")
    else:
        phase = windowrecord.phase(window, started).replace("-", " ")
        recorded_at = announcements.office_time(window.recorded_at, zone)
        print(
            f"Maintenance window {window.key} is {phase} "
            f"{_span_text(window.starts_at, window.ends_at, zone)}, "
            f"recorded by {window.actor} on {recorded_at}."
        )
    return 0
