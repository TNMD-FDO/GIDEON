"""The guardrail's lag window: the typed ``StreamState``, ``StreamCheck``, text
in and released text out, and ``record_stream_trip``, the one recorder the
service calls for a trip on any path.

Built over every module before it; General's service feeds it a stream.
"""

import contextlib
from collections.abc import Callable, Mapping
from dataclasses import KW_ONLY, dataclass, field

from gideon.guardrail.families import Trip
from gideon.guardrail.grammar import (
    LAG_CHARS,
    MAX_MATCH_CHARS,
    RESTATEMENT_LOOKAHEAD_CHARS,
)
from gideon.guardrail.judge import Constraint, judge_floor
from gideon.guardrail.writer import USER_SOURCE, record_trip


@dataclass(slots=True, repr=False, eq=False)
class TextState:
    """Accumulated text and release positions for one stream."""

    text: str = ""
    released: int = 0
    decided: int = 0
    constraints: list[Constraint] = field(default_factory=list)
    finished: bool = False

    def __repr__(self) -> str:
        status = "finished" if self.finished else "open"
        return f"{len(self.text)}/{self.released}/{self.decided}/{status}"

    __str__ = __repr__


@dataclass(slots=True, repr=False, eq=False)
class StreamState:
    """The request's own state in General's service for its scope.

    Supplied figures and confirmation context guide the judge; branch, source,
    and chat id feed the trip row. Content, trip, and finished track release.
    No character of the stream or of the user's dates may reach a log.
    """

    supplied: Mapping[str, frozenset[str]]
    confirmation: frozenset[str]
    _: KW_ONLY
    branch: str | None = None
    source: str = USER_SOURCE
    chat_id: str | None = None
    content: TextState = field(default_factory=TextState)
    trip: Trip | None = None
    finished: bool = False

    def __repr__(self) -> str:
        return (
            f"StreamState(content={self.content!r}, tripped={self.trip is not None}, "
            f"finished={self.finished})"
        )

    __str__ = __repr__


def record_stream_trip(state: StreamState, trip: Trip) -> None:
    """Record the stream's trip once; the texts are discarded from here on."""

    if state.trip is not None:
        return
    state.trip = trip
    # Trip recording cannot affect the refusal; its writer stays silent on any
    # failure so a logging problem never changes the judged response.
    with contextlib.suppress(Exception):
        record_trip(trip, state.branch, state.source, state.chat_id)
    state.content.text = ""
    state.content.constraints = []


Judge = Callable[..., "Trip | tuple[Constraint, ...] | None"]


class StreamCheck:
    """The bounded release of one streamed text: judge the window, release all but the tail.

    The mechanism knows no family — the judge is a parameter —
    so another bounded-regex check (the citation stamp) can copy
    it.  A released hit always carries the context that exempted it: the lag
    point never lands inside a constraint the judge reported.
    """

    def __init__(self, state: StreamState, *, judge: Judge) -> None:
        self.state = state
        self._judge_function = judge

    def _judge(
        self, text: str, decided: int, *, prefix: bool
    ) -> Trip | tuple[Constraint, ...] | None:
        floor = judge_floor(decided)
        opening = text[:MAX_MATCH_CHARS]
        result = self._judge_function(
            (text[floor:],),
            opening,
            self.state.supplied,
            self.state.confirmation,
            prefix=prefix,
            since=max(0, decided - floor),
        )
        if isinstance(result, tuple):
            return tuple(
                Constraint(item.start + floor, item.end + floor) for item in result
            )
        return result

    def append(self, delta: str) -> str:
        """Append generated text and return what may now be released."""

        if self.state.trip is not None:
            return ""
        self.state.content.text += delta
        return self.judge()

    def judge(self) -> str:
        """Judge the bounded window and return the text that may now be released."""

        if self.state.trip is not None:
            return ""
        entry = self.state.content
        text, released, decided = entry.text, entry.released, entry.decided
        result = self._judge(text, decided, prefix=True)
        if isinstance(result, Trip):
            record_stream_trip(self.state, result)
            return ""
        fresh = tuple(result) if isinstance(result, tuple) else ()
        # Constraints persist until release has passed them; a decided hit is
        # not re-judged, but its context still travels with it.
        active = {(item.start, item.end): item for item in entry.constraints}
        for item in fresh:
            active[(item.start, item.end)] = item
        constraints = [item for item in active.values() if item.end > released]
        entry.decided = max(decided, len(text) - RESTATEMENT_LOOKAHEAD_CHARS)
        target = max(0, len(text) - LAG_CHARS)
        moved = True
        while moved:
            moved = False
            for item in constraints:
                if item.start < target < item.end:
                    target = max(released, item.start)
                    moved = True
        target = max(released, min(target, len(text)))
        entry.constraints = [item for item in constraints if item.end > target]
        entry.released = target
        return text[released:target]

    def finish(self) -> str:
        """Judge the complete text in ordinary mode and return its held tail."""

        if self.state.trip is not None:
            return ""
        entry = self.state.content
        text, released, decided = entry.text, entry.released, entry.decided
        if entry.finished:
            return ""
        result = self._judge(text, decided, prefix=False)
        if isinstance(result, Trip):
            record_stream_trip(self.state, result)
            return ""
        entry.released = len(text)
        entry.decided = len(text)
        entry.constraints = []
        entry.finished = True
        return text[released:]
