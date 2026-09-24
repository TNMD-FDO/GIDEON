"""The pinned Open WebUI's rated turns: the one module that knows its feedback row.

It runs one statement as the database superuser over the Postgres container's
socket and projects, per rating, its ids, its time, and from the row's chat
snapshot the rated message and its parent alone.  A comment, a tag, the
rater, and every other message of the chat are never selected, so they never
cross the socket; psql's diagnostics are never quoted, since they can quote a
row.
"""

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from gideon.evaluation import record
from gideon.host import stack
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike
from gideon.improvement import owuifeedback
from gideon.improvement.feedback import FeedbackRecord
from gideon.improvement.snapshot import RatedTurn, SnapshotReading, SnapshotSource

POSTGRES_SERVICE: Final[str] = record.POSTGRES_SERVICE
SUPERUSER: Final[str] = "postgres"
FRONTEND_DATABASE: Final[str] = "openwebui"
FEEDBACK_TABLE: Final[str] = "feedback"
START_VARIABLE: Final[str] = "snapshot_start"
END_VARIABLE: Final[str] = "snapshot_end"
_STACK_FIX: Final[str] = "Check the stack with sudo python3 -m gideon status, then retry."
_READ_FIX: Final[str] = (
    "Run sudo python3 -m gideon eval candidates --out <dir> as root with the stack up, then retry."
)


def statement(start: int, end: int) -> str:
    """Build the one-row-per-rating query for the requested time window."""

    for name, value in (("start", start), ("end", end)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    # The snapshot holds the whole chat object, whose own ``chat`` field keeps
    # the message map by id; the rated message names its question by parentId.
    return f"""\\set {START_VARIABLE} {start}
\\set {END_VARIABLE} {end}
WITH selected AS (
    SELECT
        id,
        created_at,
        data->'rating' AS rating,
        data->>'model_id' AS model_id,
        meta->>'chat_id' AS chat_id,
        meta->>'message_id' AS message_id,
        snapshot->'chat'->'chat'->'history'->'messages' AS messages
    FROM {FEEDBACK_TABLE}
    WHERE type = 'rating'
      AND created_at >= :{START_VARIABLE}
      AND created_at < :{END_VARIABLE}
), rated AS (
    SELECT
        id,
        created_at,
        rating,
        model_id,
        chat_id,
        message_id,
        messages,
        messages->message_id AS rated_message
    FROM selected
)
SELECT json_build_object(
    'id', id,
    'created_at', created_at,
    'rating', rating,
    'model_id', model_id,
    'chat_id', chat_id,
    'message_id', message_id,
    'answer', rated_message->>'content',
    'answer_role', rated_message->>'role',
    'question', messages->(rated_message->>'parentId')->>'content'
)::text
FROM rated
ORDER BY created_at, id;"""


def argv(rendered_dir: PathLike) -> list[str]:
    """Build the local socket command that reads the frontend database."""

    return stack.exec_argv(
        rendered_dir,
        POSTGRES_SERVICE,
        "psql",
        "-U",
        SUPERUSER,
        "-d",
        FRONTEND_DATABASE,
        "-v",
        "ON_ERROR_STOP=1",
        "-tA",
        "-f",
        "-",
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _decode_line(line: str) -> RatedTurn | None:
    try:
        row = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(row, Mapping):
        return None
    rating_value = row.get("rating")
    rating = owuifeedback.decode_rating(rating_value)
    created_at = row.get("created_at")
    feedback_id = row.get("id")
    chat_id = row.get("chat_id")
    message_id = row.get("message_id")
    model_id = row.get("model_id")
    if (
        rating is None
        or isinstance(created_at, bool)
        or not isinstance(created_at, int)
        or not isinstance(feedback_id, str)
        or not feedback_id
        or not isinstance(chat_id, str)
        or not chat_id
        or not isinstance(message_id, str)
        or not message_id
        or not isinstance(model_id, str)
        or not model_id
    ):
        return None
    return RatedTurn(
        FeedbackRecord(rating, chat_id, message_id, model_id, created_at),
        feedback_id,
        _text(row.get("question")),
        _text(row.get("answer")),
        _text(row.get("answer_role")),
    )


def decode_lines(lines: Sequence[str]) -> SnapshotReading:
    """Decode JSON output rows, counting rows that do not form a rating."""

    rows = tuple(line for line in lines if line.strip())
    turns = tuple(turn for line in rows if (turn := _decode_line(line)) is not None)
    return SnapshotReading(turns, len(rows) - len(turns))


def read(
    host: Host, rendered_dir: PathLike, start: int, end: int
) -> SnapshotReading | Problem:
    """Run one bounded database read and return only the projected fields."""

    sql = statement(start, end)
    try:
        result = host.run(argv(rendered_dir), input=sql)
    except (OSError, subprocess.SubprocessError):
        return Problem("snapshot reader command could not run", _STACK_FIX)
    if result.returncode != 0:
        return Problem(f"snapshot reader failed with exit code {result.returncode}", _READ_FIX)
    return decode_lines(result.stdout.splitlines())


@dataclass(frozen=True, slots=True)
class _BoundSource:
    host: Host
    rendered_dir: PathLike

    def read(self, start: int, end: int) -> SnapshotReading | Problem:
        return read(self.host, self.rendered_dir, start, end)


def source(host: Host, rendered_dir: PathLike) -> SnapshotSource:
    """Bind the database read; no command runs until its read method is called."""

    return _BoundSource(host, rendered_dir)
