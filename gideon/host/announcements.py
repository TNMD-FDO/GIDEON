"""Fixed announcements and the set that currently stands."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Final

from gideon.host import backuplock, windowrecord
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike
from gideon.host.windowrecord import Window

LEVELS: Final = ("info", "warning", "error", "success")
# An id is printed in rows and refusals, so it is held to this grammar.
ID_PATTERN: Final = re.compile(r"[a-z0-9-]+")
_WINDOW_AHEAD: Final = "GIDEON will be unavailable for maintenance from {start} until {end}."
_WINDOW_UNDER_WAY: Final = "GIDEON is under maintenance and may be unavailable until {end}."
_COMMAND_UNAVAILABLE: Final = "GIDEON is unavailable right now."
_COMMAND_WITH_END: Final = "GIDEON is unavailable right now, and maintenance may continue until {end}."
WINDOW_NOTE: Final = "The next maintenance window is from {start} until {end}."
NO_WINDOW_NOTE: Final = "No maintenance window is announced."

WEEKDAYS: Final = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)
MONTHS: Final = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


@dataclass(frozen=True, slots=True)
class Announcement:
    """A fixed plain-text notice with a level and dismissal policy."""

    id: str
    """Use a new id each time an announcement is raised.

    A browser remembers dismissals by id alone and prunes them only at a later
    dismissal.
    """

    level: str
    text: str
    dismissible: bool

    def __post_init__(self) -> None:
        if not ID_PATTERN.fullmatch(self.id):
            raise ValueError(
                f"Announcement {self.id!r} needs a non-empty id of lowercase letters, "
                "digits, and hyphens."
            )
        if self.level not in LEVELS:
            raise ValueError(f"Announcement {self.id!r} needs a level from {', '.join(LEVELS)}.")
        if not self.text:
            raise ValueError(f"Announcement {self.id!r} needs non-empty text.")
        if self.text.splitlines() != [self.text]:
            raise ValueError(f"Announcement {self.id!r} needs text on one line.")
        if any(character in self.text for character in "$\\`*_<[~"):
            raise ValueError(f"Announcement {self.id!r} needs text free of Markdown punctuation.")
        if re.match(r"[#-]|\d+\.", self.text.lstrip()):
            raise ValueError(
                f"Announcement {self.id!r} needs text that starts without a heading or list marker."
            )
        if "://" in self.text:
            raise ValueError(f"Announcement {self.id!r} needs text free of URLs.")


def office_time(instant: datetime, timezone: tzinfo) -> str:
    """Render an instant in office time with fixed English names."""

    local = instant.astimezone(timezone)
    period = "AM" if local.hour < 12 else "PM"
    return (
        f"{WEEKDAYS[local.weekday()]}, {MONTHS[local.month - 1]} {local.day} "
        f"at {local.hour % 12 or 12}:{local.minute:02d} {period}"
    )


def note_line(window: Window | None, timezone: tzinfo) -> str:
    """Return the release note's maintenance window sentence."""

    text = NO_WINDOW_NOTE if window is None else WINDOW_NOTE.format(
        start=office_time(window.starts_at, timezone), end=office_time(window.ends_at, timezone),
    )
    return Announcement("window-note", "info", text, dismissible=False).text


def window_announcements(
    window: Window | None, now: datetime, timezone: tzinfo,
) -> tuple[Announcement, ...]:
    """Return the announcement for a standing window in its current phase."""

    if window is None:
        return ()
    phase = windowrecord.phase(window, now)
    identifier = f"window-{window.key}-{phase}"
    end = office_time(window.ends_at, timezone)
    if phase == "ahead":
        text = _WINDOW_AHEAD.format(start=office_time(window.starts_at, timezone), end=end)
        return (Announcement(identifier, "info", text, dismissible=True),)
    return (Announcement(identifier, "warning", _WINDOW_UNDER_WAY.format(end=end), dismissible=False),)


def command_holder(io: Host) -> backuplock.Holding | Problem:
    """Read the primary announcement lock, falling back to the nested lock if free."""

    try:
        holding = backuplock.held_by(io, lock=backuplock.UNAVAILABLE_LOCK)
        if holding.held:
            return holding
        return backuplock.held_by(io, lock=backuplock.UNAVAILABLE_NESTED_LOCK)
    except (OSError, UnicodeError):
        return Problem(
            "the unavailability announcement locks could not be read.",
            f"Restore access to {backuplock.LOCK_DIR} and {backuplock.LOCK_TABLE}, then retry.",
        )


def command_announcement(
    record: backuplock.Record, window: Window | None, now: datetime, timezone: tzinfo,
) -> Announcement:
    """Render the running command's warning for the current window phase."""

    started = record.started.astimezone(UTC).strftime("%Y%m%dt%H%M%S%fz")
    text = _COMMAND_UNAVAILABLE
    if window is not None and windowrecord.phase(window, now) == "under-way":
        text = _COMMAND_WITH_END.format(end=office_time(window.ends_at, timezone))
    return Announcement(f"unavailable-{started}-{record.pid}", "warning", text, dismissible=False)


def standing(
    io: Host, rendered_dir: PathLike, *, holder: backuplock.Holding | Problem,
    now: datetime, timezone: tzinfo, timeout: float | None = None,
) -> tuple[Announcement, ...] | Problem:
    """Combine the supplied command holder with the newest window event."""

    if isinstance(holder, Problem):
        return holder
    event = windowrecord.newest(io, rendered_dir, timeout=timeout)
    if isinstance(event, Problem):
        return event
    window = windowrecord.standing_window(event, now)
    if holder.record is None:
        return window_announcements(window, now, timezone)
    command = command_announcement(holder.record, window, now, timezone)
    if window is not None and windowrecord.phase(window, now) == "under-way":
        return (command,)
    return (command, *window_announcements(window, now, timezone))
