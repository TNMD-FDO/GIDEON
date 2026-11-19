"""Synchronize Open WebUI's banners with the current announcements."""

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gideon.host import (
    announcements,
    backuplock,
    owui,
    report,
    secrets,
    site,
    stack,
    tls,
)
from gideon.host.report import Problem, StageResult
from gideon.host.stages import aware_now
from gideon.host.sysio import Host, LockingHost, PathLike, RealHost

_COMMAND: Final = "announcements sync"
_RENDERED_DIR: Final = "/etc/gideon/rendered"
_SITE_PATH: Final = "/etc/gideon/site.yaml"
# exempt: operational starting value. Limit how long a stalled store or frontend can delay rollback.
_HOOK_TIMEOUT: Final = 2.0
# exempt: operational starting value. Allow a changed holder or competing write to settle.
_SYNC_ROUNDS: Final = 3


@dataclass(frozen=True, slots=True)
class SyncResult:
    """The standing ids, verified holder, and banner changes from the final round."""

    ids: tuple[str, ...]
    holder: backuplock.Holding
    changes: owui.BannerSync

    @property
    def detail(self) -> str:
        changes = self.changes
        detail = [
            f"{act}: {', '.join(ids)}"
            for act, ids in (("added", changes.added), ("removed", changes.removed), ("replaced", changes.replaced))
            if ids
        ]
        return "; ".join(detail) or "banner list matches"


@dataclass(frozen=True, slots=True)
class _BannerProblem(Problem):
    """Sync failed after reading a standing set the command can report."""

    ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RaiseHandle:
    """The lock this raise owns and its command's announcement id."""

    lock: backuplock.Lock | None = None
    id: str = ""


def _refuse(problem: Problem) -> int:
    print(report.refusal(_COMMAND, problem.problem, problem.fix), file=sys.stderr)
    return 1


def _sync_round(
    io: Host,
    rendered_dir: PathLike,
    desired: tuple[announcements.Announcement, ...],
    make_client: Callable[..., owui.Client],
) -> owui.BannerSync | Problem | None:
    """Sync once, returning None when the posted list changed before read-back."""

    key = secrets.read_secret(io, "gideon_admin_api_key")
    if key.missing:
        return Problem("the break-glass API key is missing.", f"Run {report.command('apply')}, then retry.")
    if not key.ok or key.value is None:
        return Problem(
            key.problem or "the break-glass API key is unavailable.",
            key.fix or "Correct the secret file, then retry.",
        )
    if not key.value:
        return Problem(
            "the break-glass API key is empty.",
            f"Run {report.command('secrets rotate gideon_admin_api_key')}, then retry.",
        )

    fix = f"Run {stack.logs_fix(rendered_dir, 'open-webui')}, then retry."
    try:
        return owui.sync_banners(make_client(api_key=key.value), desired)
    except owui.BannerReadbackMismatch:
        return None
    except owui.OwuiError as exc:
        return Problem(exc.problem, fix)
    except (OSError, ValueError):
        return Problem("Open WebUI banner request failed.", fix)


def sync(
    io: Host,
    rendered_dir: PathLike,
    *,
    hostname: str,
    timezone: tzinfo,
    now: datetime,
    client_factory: Callable[..., owui.Client] | None = None,
    timeout: float | None = None,
) -> SyncResult | Problem:
    """Reconcile the banner list and verify the holder after each round's read-back."""

    make_client = client_factory
    if make_client is None:
        if timeout is None:
            make_client = owui.ingress_client_factory(hostname, ca_path=tls.CA_PATH)
        else:
            make_client = owui.ingress_client_factory(hostname, ca_path=tls.CA_PATH, timeout=timeout)

    ids: tuple[str, ...] = ()
    for _ in range(_SYNC_ROUNDS):
        holder = announcements.command_holder(io)
        desired = announcements.standing(io, rendered_dir, holder=holder, now=now, timezone=timezone, timeout=timeout)
        if isinstance(desired, Problem):
            return desired
        ids = tuple(item.id for item in desired)
        changes = _sync_round(io, rendered_dir, desired, make_client)
        if isinstance(changes, Problem):
            return _BannerProblem(changes.problem, changes.fix, ids)
        current = announcements.command_holder(io)
        if isinstance(current, Problem):
            return current
        if changes is not None and current == holder:
            return SyncResult(ids, current, changes)

    return _BannerProblem(
        f"the banner list did not converge after {_SYNC_ROUNDS} rounds.",
        "The next announcements sync firing will retry.",
        ids,
    )


def raise_announcement(
    io: LockingHost,
    rendered_dir: PathLike,
    *,
    command: str,
    hostname: str,
    timezone: tzinfo,
    now: datetime,
    client_factory: Callable[..., owui.Client] | None = None,
) -> tuple[RaiseHandle, StageResult]:
    """Hold the command's announcement lock and try to put its banner up."""

    lock = backuplock.UNAVAILABLE_LOCK
    try:
        outcome = backuplock.take(io, lock=lock, command=command, now=now)
        if outcome.state is backuplock.State.REFUSED:
            lock = backuplock.UNAVAILABLE_NESTED_LOCK
            outcome = backuplock.take(io, lock=lock, command=command, now=now)
    except OSError:
        return RaiseHandle(), StageResult("announce", True, f"the {lock.noun} lock could not be taken", "")
    if outcome.state is not backuplock.State.HELD:
        return RaiseHandle(), StageResult("announce", True, "an announcement already stands", "")

    record = backuplock.Record(command, os.getpid(), now)
    identifier = announcements.command_announcement(record, None, now, timezone).id
    handle = RaiseHandle(lock, identifier)
    try:
        result = sync(
            io, rendered_dir, hostname=hostname, timezone=timezone, now=now,
            client_factory=client_factory, timeout=_HOOK_TIMEOUT,
        )
    except BaseException:
        # An interrupt after the post leaves no handle with the caller, so the
        # raise takes its own banner down before it lets the interrupt go on.
        clear_announcement(
            io, rendered_dir, handle, hostname=hostname, timezone=timezone, now=now,
            client_factory=client_factory,
        )
        raise
    if isinstance(result, Problem):
        detail = f"lock held, announcement not shown yet: {result.problem}"
    elif identifier in result.ids:
        detail = f"raised {identifier}"
    elif result.holder.record is not None:
        detail = f"announcement stands under {result.holder.record.command}"
    else:
        detail = "lock held, announcement not shown yet"
    return handle, StageResult("announce", True, detail, "")


def clear_announcement(
    io: LockingHost,
    rendered_dir: PathLike,
    handle: RaiseHandle,
    *,
    hostname: str,
    timezone: tzinfo,
    now: datetime,
    client_factory: Callable[..., owui.Client] | None = None,
) -> StageResult | None:
    """Release this raise's lock and try to reconcile the remaining banners."""

    if handle.lock is None:
        return None
    try:
        backuplock.release(io, lock=handle.lock)
    except OSError:
        return StageResult(
            "clear", True, "the lock could not be released. The banner is left to the next announcements sync firing.", "",
        )
    result = sync(
        io, rendered_dir, hostname=hostname, timezone=timezone, now=now,
        client_factory=client_factory, timeout=_HOOK_TIMEOUT,
    )
    if isinstance(result, Problem):
        detail = f"lock released, banner left to the next announcements sync firing: {result.problem}"
    elif result.holder.record is not None:
        detail = f"announcement still stands under {result.holder.record.command}"
    else:
        detail = f"cleared {handle.id}"
    return StageResult("clear", True, detail, "")


def run_announcements_sync(
    args: object,
    *,
    host: Host | None = None,
    rendered_dir: PathLike = _RENDERED_DIR,
    site_path: PathLike = _SITE_PATH,
    client_factory: Callable[..., owui.Client] | None = None,
    now: datetime | None = None,
) -> int:
    """Read the standing set and reconcile the frontend's banner list."""

    io = RealHost() if host is None else host
    if io.geteuid() != 0:
        return _refuse(Problem(
            "root is required.",
            f"Run {report.command(_COMMAND, sudo=False)} as root, for example with sudo.",
        ))
    loaded = site.load_site(Path(site_path), host=io)
    if loaded.errors or loaded.config is None:
        return _refuse(Problem(site.render_errors(loaded.errors), "Correct the site file, then retry."))
    config = loaded.config
    try:
        timezone = ZoneInfo(config.office.timezone)
    except ZoneInfoNotFoundError:
        return _refuse(Problem(
            "the office timezone could not be loaded.",
            f"Install timezone data or correct office.timezone in {site_path}, then retry.",
        ))
    instant = aware_now(now)
    if instant is None:
        return _refuse(Problem("the clock must be timezone-aware.", "Supply a timezone-aware clock value."))

    result = sync(
        io, rendered_dir, hostname=config.hostname, timezone=timezone, now=instant, client_factory=client_factory,
    )
    if isinstance(result, Problem) and not isinstance(result, _BannerProblem):
        report.print_stage(StageResult("standing", False, result.problem, result.fix))
        return 1
    report.print_stage(StageResult("standing", True, ", ".join(result.ids) or "none", ""))

    if isinstance(result, _BannerProblem):
        report.print_stage(StageResult("banners", False, result.problem, result.fix))
        return 1
    report.print_stage(StageResult("banners", True, result.detail, ""))
    return 0
