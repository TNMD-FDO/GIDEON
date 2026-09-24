"""The rated-turn seam: a rating with the question and answer the user rated.

It is the one seam in the package that carries text, read by the candidate
packet alone and never by a report section, and it names no frontend, table,
or key, so a frontend swap replaces only the adapter.  A turn's repr withholds
its texts.
"""

from dataclasses import dataclass
from typing import Protocol

from gideon.host.report import Problem
from gideon.improvement.feedback import FeedbackRecord


@dataclass(frozen=True, slots=True)
class RatedTurn:
    """A rating and its two associated texts, withheld in representations."""

    record: FeedbackRecord
    feedback_id: str
    question: str | None
    answer: str | None
    answer_role: str | None

    def __repr__(self) -> str:
        return (
            f"RatedTurn(record={self.record!r}, feedback_id={self.feedback_id!r}, "
            "question=<withheld>, answer=<withheld>, answer_role=<withheld>)"
        )


@dataclass(frozen=True, slots=True)
class SnapshotReading:
    """The turns read and the count of rows that were not readable ratings."""

    turns: tuple[RatedTurn, ...]
    skipped: int


class SnapshotSource(Protocol):
    """Read rated turns in a half-open epoch-second window."""

    def read(self, start: int, end: int) -> SnapshotReading | Problem: ...
