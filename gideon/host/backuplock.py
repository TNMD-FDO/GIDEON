"""The shared lock for backup and restore operations.

The lock lives under ``/run`` because it is a root-only tmpfs that needs no
cleanup after a reboot, and outside the staging directory because restore's
swap moves that directory as a whole.
"""

import dataclasses
import datetime
import enum
import json
import os

from gideon.host import report, sysio

LOCK_DIR = "/run/gideon"
LOCK_PATH = "/run/gideon/backup.lock"


@dataclasses.dataclass(frozen=True, slots=True)
class Record:
    """The command, process, and instant holding the shared lock."""

    command: str
    pid: int
    started: datetime.datetime

    def to_json(self) -> str:
        """Serialize the holder record as one deterministic JSON line."""

        return (
            json.dumps(
                {
                    "command": self.command,
                    "pid": self.pid,
                    "started": self.started.isoformat(),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )


def parse(text: str) -> Record | None:
    """Parse a holder record, returning ``None`` for unreadable text."""

    try:
        document = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(document, dict):
        return None

    command = document.get("command")
    pid = document.get("pid")
    started = document.get("started")
    if (
        not isinstance(command, str)
        or not command
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or not isinstance(started, str)
    ):
        return None
    try:
        instant = datetime.datetime.fromisoformat(started)
    except ValueError:
        return None
    if instant.tzinfo is None or instant.utcoffset() is None:
        return None
    return Record(command, pid, instant)


class State(enum.Enum):
    """The result of trying to take the shared lock."""

    HELD = "held"
    NESTED = "nested"
    REFUSED = "refused"


@dataclasses.dataclass(frozen=True, slots=True)
class Outcome:
    """The lock state and any refusal to show the operator."""

    state: State
    problem: report.Problem | None = None


def take(
    io: sysio.LockingHost,
    *,
    command: str,
    now: datetime.datetime,
) -> Outcome:
    """Take the lock, pass a same-process holder, or describe its refusal."""

    io.mkdir(LOCK_DIR, mode=0o700, parents=True, exist_ok=True)
    record = Record(command, os.getpid(), now)
    holder_text = io.take_lock(LOCK_PATH, record.to_json())
    if holder_text is None:
        return Outcome(State.HELD)

    holder = parse(holder_text)
    if holder is not None and holder.pid == os.getpid():
        return Outcome(State.NESTED)
    if holder is None:
        # A holder between its flock and its write leaves the file empty.
        return Outcome(
            State.REFUSED,
            report.Problem(
                "the backup lock is held by another gideon command, its record unreadable.",
                "Wait for the running backup or restore to finish, then retry.",
            ),
        )
    return Outcome(
        State.REFUSED,
        report.Problem(
            f"the backup lock is held by {holder.command} since "
            f"{holder.started.isoformat()} (pid {holder.pid}).",
            f"Wait for it to finish — ps -p {holder.pid} says whether it still runs "
            "— then retry.",
        ),
    )


def release(io: sysio.LockingHost) -> None:
    """Release the shared lock held by this process."""

    io.release_lock(LOCK_PATH)
