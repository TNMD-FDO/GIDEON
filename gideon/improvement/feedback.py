"""The content-free feedback seam: a user's rating of one answer, by id.

A record carries five fields and never text (ADR-0049, §19.4): no comment, no
tag, no message, no chat.  This module names no frontend, route, or key; a
frontend swap keeps it and the section and replaces only the adapter.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from gideon.host.report import Problem

type Rating = Literal["up", "down"]


@dataclass(frozen=True, slots=True)
class FeedbackRecord:
    """One rating: its direction, its three ids, and its epoch-second time.

    It never gains a field that carries text — a comment, a tag, a reason, a
    rater, a message, or a chat.
    """

    rating: Rating
    chat_id: str
    message_id: str
    model_id: str
    created_at: int


@dataclass(frozen=True, slots=True)
class FeedbackReading:
    """The records read and the count of items that were not a readable rating."""

    records: tuple[FeedbackRecord, ...]
    skipped: int


class FeedbackSource(Protocol):
    """One frontend's feedback, read as records or refused with a fix."""

    def read(self) -> FeedbackReading | Problem: ...
