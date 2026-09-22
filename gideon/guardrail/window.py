"""The guardrail's lag window: ``StreamState`` and ``StreamCheck``, text in and
released text out, a trip recorded through the writer.

Built over every module before it; General's service feeds it a stream.
"""

import contextlib
from collections.abc import Callable, Mapping

from gideon.guardrail.families import Trip
from gideon.guardrail.grammar import (
    LAG_CHARS,
    MAX_MATCH_CHARS,
    RESTATEMENT_LOOKAHEAD_CHARS,
)
from gideon.guardrail.judge import Constraint, judge_floor
from gideon.guardrail.writer import record_trip


def _new_text_state() -> dict[str, object]:
    return {
        "text": "",
        "released": 0,
        "decided": 0,
        "constraints": [],
        "finished": False,
    }


# The per-request state remains on request metadata until the request ends.
class StreamState(dict[str, object]):
    """The per-request stream state, kept on ``__metadata__`` under STREAM_STATE_KEY.

    A dict subclass, so the frontend's metadata stays a mapping tree, whose
    ``repr`` and ``str`` are content-free: the pinned frontend formats the
    whole request with ``%s`` into a DEBUG log line, and no character of the
    stream or of the user's dates may reach a log (spec §19.4).  The content
    entry holds its accumulated string, released length, decided length, and
    release constraints; the state also holds the placeholder and finished
    flags, the trip, and the inlet stash.
    """

    def __init__(
        self,
        supplied: Mapping[str, frozenset[str]],
        confirmation: frozenset[str],
        *,
        branch: str | None = None,
        source: str = "user",
    ) -> None:
        super().__init__(
            {
                "content": _new_text_state(),
                "placeholder_sent": False,
                "trip": None,
                "finished": False,
                "supplied": {
                    name: sorted(figures) for name, figures in supplied.items()
                },
                "confirmation": sorted(confirmation),
                "branch": branch,
                "source": source,
            }
        )

    def __repr__(self) -> str:
        entry = self.get("content")
        if isinstance(entry, dict):
            text = entry.get("text")
            length = len(text) if isinstance(text, str) else 0
            state = "finished" if entry.get("finished") else "open"
            content = f"{length}/{entry.get('released')}/{entry.get('decided')}/{state}"
        else:
            content = "invalid"
        return (
            f"StreamState(content={content}, placeholder_sent={self.get('placeholder_sent')}, "
            f"tripped={self.get('trip') is not None}, "
            f"finished={self.get('finished')})"
        )

    __str__ = __repr__


def _stream_state(value: object) -> dict[str, object] | None:
    """The value as the stream's state, or ``None`` when it is not one."""

    keys = (
        "content",
        "placeholder_sent",
        "trip",
        "finished",
        "supplied",
        "confirmation",
        "branch",
        "source",
    )
    if isinstance(value, dict) and all(key in value for key in keys):
        return value
    return None


def _stream_entry(state: dict[str, object], field: str) -> dict[str, object]:
    entry = state.get(field)
    if not isinstance(entry, dict):
        raise TypeError("invalid stream text state")
    return entry


def _record_stream_trip(state: dict[str, object], trip: Trip) -> None:
    """Record the stream's trip once; the texts are discarded from here on."""

    if state.get("trip") is not None:
        return
    state["trip"] = {"family": trip.family, "pattern_id": trip.pattern_id}
    branch = state.get("branch")
    source = state.get("source")
    # Trip recording cannot affect the refusal; its writer stays silent on any
    # failure so a logging problem never changes the judged response.
    with contextlib.suppress(Exception):
        record_trip(
            trip,
            branch if isinstance(branch, str) else None,
            source if isinstance(source, str) else "user",
        )
    entry = _stream_entry(state, "content")
    entry["text"] = ""
    entry["constraints"] = []


Judge = Callable[..., "Trip | tuple[Constraint, ...] | None"]


class StreamCheck:
    """The bounded release of one streamed text: judge the window, release all but the tail.

    The mechanism knows no family — the judge and the field are parameters —
    so another bounded-regex check (the citation stamp, ticket 14) can copy
    it.  A released hit always carries the context that exempted it: the lag
    point never lands inside a constraint the judge reported.
    """

    def __init__(self, state: dict[str, object], field: str, *, judge: Judge) -> None:
        self.state = state
        self.field = field
        self._judge_function = judge

    def _supplied(self) -> dict[str, frozenset[str]]:
        supplied = self.state.get("supplied")
        if not isinstance(supplied, dict):
            raise TypeError("invalid stream stash")
        result: dict[str, frozenset[str]] = {}
        for name, figures in supplied.items():
            if not isinstance(name, str) or not isinstance(figures, list) or not all(
                isinstance(figure, str) for figure in figures
            ):
                raise TypeError("invalid stream stash")
            result[name] = frozenset(figures)
        return result

    def _confirmation(self) -> frozenset[str]:
        confirmation = self.state.get("confirmation")
        if not isinstance(confirmation, list) or not all(
            isinstance(name, str) for name in confirmation
        ):
            raise TypeError("invalid stream confirmation")
        return frozenset(confirmation)

    def _entry(self) -> tuple[dict[str, object], str, int, int]:
        entry = _stream_entry(self.state, self.field)
        text, released, decided = (
            entry.get("text"),
            entry.get("released"),
            entry.get("decided"),
        )
        if (
            not isinstance(text, str)
            or not isinstance(released, int)
            or not isinstance(decided, int)
        ):
            raise TypeError("invalid stream text state")
        return entry, text, released, decided

    @staticmethod
    def _constraints(entry: dict[str, object]) -> list[Constraint]:
        stored = entry.get("constraints")
        if not isinstance(stored, list):
            raise TypeError("invalid stream constraints")
        return [Constraint(int(item[0]), int(item[1])) for item in stored]

    def _judge(
        self, text: str, decided: int, *, prefix: bool
    ) -> Trip | tuple[Constraint, ...] | None:
        floor = judge_floor(decided)
        opening = text[:MAX_MATCH_CHARS]
        result = self._judge_function(
            (text[floor:],),
            opening,
            self._supplied(),
            self._confirmation(),
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

        if self.state.get("trip") is not None:
            return ""
        entry, text, _, _ = self._entry()
        entry["text"] = text + delta
        return self.judge()

    def judge(self) -> str:
        """Judge the bounded window and return the text that may now be released."""

        if self.state.get("trip") is not None:
            return ""
        entry, text, released, decided = self._entry()
        result = self._judge(text, decided, prefix=True)
        if isinstance(result, Trip):
            _record_stream_trip(self.state, result)
            return ""
        fresh = tuple(result) if isinstance(result, tuple) else ()
        # Constraints persist until release has passed them; a decided hit is
        # not re-judged, but its context still travels with it.
        active = {(item.start, item.end): item for item in self._constraints(entry)}
        for item in fresh:
            active[(item.start, item.end)] = item
        constraints = [item for item in active.values() if item.end > released]
        entry["decided"] = max(decided, len(text) - RESTATEMENT_LOOKAHEAD_CHARS)
        target = max(0, len(text) - LAG_CHARS)
        moved = True
        while moved:
            moved = False
            for item in constraints:
                if item.start < target < item.end:
                    target = max(released, item.start)
                    moved = True
        target = max(released, min(target, len(text)))
        entry["constraints"] = [
            [item.start, item.end] for item in constraints if item.end > target
        ]
        entry["released"] = target
        return text[released:target]

    def finish(self) -> str:
        """Judge the complete text in ordinary mode and return its held tail."""

        if self.state.get("trip") is not None:
            return ""
        entry, text, released, decided = self._entry()
        if entry.get("finished"):
            return ""
        result = self._judge(text, decided, prefix=False)
        if isinstance(result, Trip):
            _record_stream_trip(self.state, result)
            return ""
        entry["released"] = len(text)
        entry["decided"] = len(text)
        entry["constraints"] = []
        entry["finished"] = True
        return text[released:]
