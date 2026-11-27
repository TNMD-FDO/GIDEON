"""Read generator deltas and emit text released by the research turn's lag window."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from gideon import guardrail
from gideon.api.progress import REASONING_KEYS
from gideon.api.sse import DONE_EVENT
from gideon.turn import events
from gideon.turn.events import EndWord

# The branch identifies Legal chats in the shared guardrail record.
LEGAL_BRANCH: Final = "gideon-legal"

Outcome = Literal["open", "tripped", "finished", "empty", "engine-error"]


@dataclass(frozen=True, slots=True)
class AnswerStep:
    """Released events and the outcome of processing one generator payload."""

    events: tuple[bytes, ...] = field(default=(), repr=False)
    outcome: Outcome = "open"
    finish_reason: str | None = field(default=None, repr=False)
    end: EndWord | None = None
    family: str | None = None
    _displayed: str = field(default="", repr=False)

    def answer_event(self, timings_ms: Mapping[str, float | None]) -> bytes | None:
        """Encode the terminal answer after the sender measures the released text."""

        if self.end is None:
            return None
        return events.answer(self._displayed, self.end, timings_ms, family=self.family)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token counts reported by the generator for an attempt so far."""

    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int | None


def stream_state(question: str, source: str, chat_id: str | None = None) -> guardrail.StreamState:
    """Build a fresh lag window context from the question's supplied figures."""

    user_messages: list[object] = [{"role": "user", "content": question}]
    supplied, confirmation = guardrail.message_context(user_messages, len(user_messages))
    return guardrail.StreamState(supplied, confirmation, branch=LEGAL_BRANCH, source=source, chat_id=chat_id)


class AnswerMechanics:
    """Judge and accumulate the displayed answer for one generator attempt."""

    def __init__(self, state: guardrail.StreamState) -> None:
        self._check = guardrail.StreamCheck(state, judge=guardrail.judge_rendered)
        self._released: list[str] = []
        # Whitespace the window released before any visible answer text: held back, so an
        # attempt whose answer is blank finishes empty and the retry never follows sent text.
        self._held = ""
        self._usage: TokenUsage | None = None
        self.failure: str | None = None

    def __repr__(self) -> str:
        state = self._check.state
        return f"AnswerMechanics(released_deltas={len(self._released)}, finished={state.finished}, tripped={state.trip is not None})"

    @property
    def usage(self) -> TokenUsage | None:
        """The last well-formed usage from this attempt, if any."""

        return self._usage

    def process(self, payload: str) -> AnswerStep:
        """Process one reassembled payload and release any text the judge approved."""

        state = self._check.state
        if state.finished:
            return AnswerStep()
        try:
            if state.trip is not None:
                return self._trip_step(state.trip)
            if payload == DONE_EVENT:
                return self._finish()
            chunk = json.loads(payload)
            if isinstance(chunk, dict):
                self._read_usage(chunk.get("usage"))
                if "error" in chunk and "choices" not in chunk:
                    return self._finish(unfinished="engine-error")
            return self._process_chunk(chunk)
        except Exception as error:  # noqa: BLE001 - unjudgeable payloads fail closed with the exception class alone.
            return self.fail_closed(error)

    def _read_usage(self, usage: object) -> None:
        if not isinstance(usage, dict):
            return
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if (
            type(prompt_tokens) is not int or prompt_tokens < 0
            or type(completion_tokens) is not int or completion_tokens < 0
        ):
            return
        details = usage.get("completion_tokens_details")
        reasoning_tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
        self._usage = TokenUsage(
            prompt_tokens, completion_tokens,
            reasoning_tokens if type(reasoning_tokens) is int and reasoning_tokens >= 0 else None,
        )

    def _process_chunk(self, chunk: object) -> AnswerStep:
        choices = chunk.get("choices") if isinstance(chunk, dict) else None
        if not isinstance(choices, list):
            raise TypeError("Stream choices must be a list.")
        if not choices:
            return AnswerStep()
        if len(choices) != 1 or not isinstance(choice := choices[0], dict):
            raise TypeError("Stream choices must contain one object.")
        # An absent index names the only choice there is.
        index = choice.get("index", 0)
        if type(index) is not int or index != 0:
            raise TypeError("Stream choice index must be zero.")
        delta = choice.get("delta")
        if delta is None:
            delta = {}
        if not isinstance(delta, dict):
            raise TypeError("Stream delta must be an object.")
        # Reasoning fields are read only for shape. Only content enters the window.
        for key in REASONING_KEYS:
            reasoning = delta.get(key)
            if reasoning is not None and not isinstance(reasoning, str):
                raise TypeError("Stream reasoning must be text or null.")
        content, finish_reason = delta.get("content"), choice.get("finish_reason")
        if content is not None and not isinstance(content, str):
            raise TypeError("Stream content must be text or null.")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise TypeError("Stream finish reason must be text or null.")

        released = self._check.append(content) if content else ""
        if (trip := self._check.state.trip) is not None:
            return self._trip_step(trip, released, finish_reason=finish_reason)
        if finish_reason is not None:
            return self._finish(released, finish_reason=finish_reason)
        return AnswerStep(events=self._answer(released))

    def settle(self) -> AnswerStep:
        """Release the judged tail when the stream ends without a finish reason."""

        if self._check.state.finished:
            return AnswerStep()
        try:
            return self._finish()
        except Exception as error:  # noqa: BLE001 - an unjudgeable tail fails closed.
            return self.fail_closed(error)

    def fail_closed(self, error: Exception | None = None) -> AnswerStep:
        """End an unjudgeable stream with a recorded trip and its refusal."""

        state = self._check.state
        if state.finished:
            return AnswerStep()
        if error is not None:
            self.failure = type(error).__name__
        trip = state.trip
        if trip is None:
            trip = guardrail.Trip(guardrail.DEADLINE_FAMILY.name, guardrail.ERROR_PATTERN_ID)
            guardrail.record_stream_trip(state, trip)
        return self._trip_step(trip)

    def _finish(
        self, released: str = "", *, finish_reason: str | None = None,
        unfinished: Literal["open", "engine-error"] = "open",
    ) -> AnswerStep:
        tail = self._check.finish()
        if (trip := self._check.state.trip) is not None:
            return self._trip_step(trip, released, finish_reason=finish_reason)
        emitted = self._answer(released, tail)
        if finish_reason is None:
            step = AnswerStep(events=emitted, outcome=unfinished)
        elif not self._released:
            step = AnswerStep(outcome="empty", finish_reason=finish_reason)
        else:
            step = AnswerStep(
                events=emitted, outcome="finished", finish_reason=finish_reason,
                end="truncated" if finish_reason == "length" else "complete", _displayed="".join(self._released),
            )
        self._check.state.finished = True
        return step

    def _trip_step(self, trip: guardrail.Trip, released: str = "", *, finish_reason: str | None = None) -> AnswerStep:
        prefix = self._answer(released)
        refusal = guardrail.refusal_for(trip.family)
        # Held whitespace is no released answer, so a refusal after it stands alone.
        if self._released:
            refusal = guardrail.REFUSAL_SEPARATOR + refusal
        emitted = prefix + self._release(refusal)
        self._check.state.finished = True
        return AnswerStep(
            events=emitted, outcome="tripped", finish_reason=finish_reason, end="tripped", family=trip.family,
            _displayed="".join(self._released),
        )

    def _answer(self, *deltas: str) -> tuple[bytes, ...]:
        """Release answer text, holding leading whitespace until visible text follows it."""

        emitted: tuple[bytes, ...] = ()
        for delta in deltas:
            if not self._released:
                self._held += delta
                if not self._held.strip():
                    continue
                delta, self._held = self._held, ""
            emitted += self._release(delta)
        return emitted

    def _release(self, *deltas: str) -> tuple[bytes, ...]:
        released = tuple(delta for delta in deltas if delta)
        emitted = tuple(events.text(delta) for delta in released)
        self._released.extend(released)
        return emitted
