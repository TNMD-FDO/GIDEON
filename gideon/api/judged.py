"""Judge streamed Chat Completions payloads without owning transport I/O."""

import json
from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import Final

from gideon import guardrail

from . import stamp
from .sse import DONE_EVENT

# A downstream choice is rebuilt from these keys, never scrubbed of the ones it
# withholds: a field the engine adds or renames at a pin bump — `reasoning`,
# `reasoning_content`, `logprobs`, token ids — is absent because it was never
# copied, not because a list of names caught it.
RELAYED_CHOICE_KEYS: Final[tuple[str, ...]] = ("index", "finish_reason")
RELAYED_DELTA_KEYS: Final[tuple[str, ...]] = ("role", "content", "tool_calls")
RELAYED_MESSAGE_KEYS: Final[tuple[str, ...]] = ("role", "content", "tool_calls")
# The text a delta may carry, and the names the pinned engine has used for its
# reasoning. These are read to decide and to validate, never to scrub: what is
# relayed is what the lists above name.
TEXT_KEYS: Final[tuple[str, ...]] = ("reasoning", "reasoning_content", "thinking", "content")
REASONING_KEYS: Final[tuple[str, ...]] = ("reasoning", "reasoning_content", "thinking")
# With no valid chunk seen, no envelope is minted: `object` and `choices` are
# all the pinned frontend and the door read of a chunk.
CHUNK_OBJECT: Final[str] = "chat.completion.chunk"
UNJUDGED_ERROR_CODE: Final[str] = "completion_unjudged"
UNJUDGED_ERROR: Final[dict[str, object]] = {
    "error": {
        "message": "The completion could not be judged.",
        "type": "server_error",
        "param": None,
        "code": UNJUDGED_ERROR_CODE,
    }
}


def source_for_header(values: Sequence[str], eval_identity: str) -> str:
    """Return the content-free source word for one forwarded header."""

    if len(values) != 1:
        return "user"
    value = values[0].strip()
    identity = eval_identity.strip()
    if not value or not identity:
        return "user"
    return "eval" if value.casefold() == identity.casefold() else "user"


def stream_state_from_body(body: bytes, source: str) -> guardrail.StreamState:
    """Build the stream's request context from the caller's JSON body."""

    try:
        parsed = json.loads(body)
    except Exception:  # noqa: BLE001 - unreadable request bodies use the strict empty stash.
        return guardrail.StreamState({}, frozenset(), source=source)
    if not isinstance(parsed, dict):
        return guardrail.StreamState({}, frozenset(), source=source)

    model = parsed.get("model")
    branch = model if isinstance(model, str) else None
    messages = parsed.get("messages")
    if not isinstance(messages, list):
        return guardrail.StreamState({}, frozenset(), branch=branch, source=source)
    try:
        supplied, contexts = guardrail.message_context(messages, len(messages))
    except Exception:  # noqa: BLE001 - malformed context uses the strict empty stash.
        supplied, contexts = {}, frozenset()
    return guardrail.StreamState(supplied, contexts, branch=branch, source=source)


def judge_completion(
    body: bytes, state: guardrail.StreamState
) -> tuple[bytes | None, str | None]:
    """Judge and rebuild a complete Chat Completions response body.

    The second return value is ``None`` when the body was judged, and
    otherwise the failure's exception class, which the relay logs: the
    judgement itself never logs, since everything it holds is model text.  A
    valid body is compactly re-serialized even when no choice trips, so every
    message passes through the same withholding boundary and an untripped text
    receives the citation stamp's tail.
    """

    try:
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            raise TypeError("completion body is not an object")
        choices = parsed.get("choices")
        if not isinstance(choices, list):
            raise TypeError("completion choices are not a list")
        supplied, contexts = _state_context(state)
        rebuilt_choices: list[dict[str, object]] = []
        for choice_value in choices:
            if not isinstance(choice_value, Mapping):
                raise TypeError("completion choice is not a mapping")
            message_value = choice_value.get("message")
            if not isinstance(message_value, Mapping):
                raise TypeError("completion message is not a mapping")
            if "content" not in message_value:
                raise TypeError("completion message has no content")
            content = message_value["content"]
            if content is not None and not isinstance(content, str):
                raise TypeError("completion content is not text or null")

            rebuilt_message = {
                key: message_value[key]
                for key in RELAYED_MESSAGE_KEYS
                if key in message_value
            }
            if isinstance(content, str):
                result = guardrail.judge_rendered(
                    (content,), content, supplied, contexts
                )
                if isinstance(result, guardrail.Trip):
                    _record_trip(state, result)
                    rebuilt_message["content"] = _refusal_for(result.family)
                else:
                    rebuilt_message["content"] = content + stamp.tail_for(content)
            # The choice is rebuilt here as it is on the stream: a `logprobs`
            # or a token id the engine adds beside the message cannot carry
            # text the window refused.
            rebuilt_choice: dict[str, object] = {
                key: choice_value[key]
                for key in RELAYED_CHOICE_KEYS
                if key in choice_value
            }
            rebuilt_choice["message"] = rebuilt_message
            rebuilt_choices.append(rebuilt_choice)
        output = dict(parsed)
        output["choices"] = rebuilt_choices
        return json.dumps(output, separators=(",", ":")).encode("utf-8"), None
    except Exception as exc:  # noqa: BLE001 - unjudgeable bodies fail closed.
        _record_error_trip(state)
        return None, type(exc).__name__


def _state_context(
    state: guardrail.StreamState,
) -> tuple[Mapping[str, frozenset[str]], frozenset[str]]:
    supplied_value = state.get("supplied")
    confirmation_value = state.get("confirmation")
    if not isinstance(supplied_value, Mapping) or not isinstance(
        confirmation_value, list
    ):
        raise TypeError("invalid completion context")
    supplied: dict[str, frozenset[str]] = {}
    for name, figures in supplied_value.items():
        if not isinstance(name, str) or not isinstance(figures, list):
            raise TypeError("invalid completion stash")
        if not all(isinstance(figure, str) for figure in figures):
            raise TypeError("invalid completion stash")
        supplied[name] = frozenset(figures)
    if not all(isinstance(name, str) for name in confirmation_value):
        raise TypeError("invalid completion context")
    return supplied, frozenset(confirmation_value)


def _refusal_for(family: object) -> str:
    """The tripped family's refusal; the deadline family's when none is named."""

    if not isinstance(family, str):
        return guardrail.DEADLINE_FAMILY.refusal
    return guardrail.REFUSAL_BY_FAMILY.get(family, guardrail.DEADLINE_FAMILY.refusal)


def _record_trip(state: guardrail.StreamState, trip: guardrail.Trip) -> None:
    if state.get("trip") is not None:
        return
    state["trip"] = {"family": trip.family, "pattern_id": trip.pattern_id}
    branch = state.get("branch")
    source = state.get("source")
    with suppress(Exception):
        guardrail.record_trip(
            trip,
            branch if isinstance(branch, str) else None,
            source if isinstance(source, str) else "user",
        )


def _record_error_trip(state: guardrail.StreamState) -> None:
    content = state.get("content")
    if isinstance(content, dict):
        content["text"] = ""
        content["constraints"] = []
    _record_trip(
        state,
        guardrail.Trip(guardrail.DEADLINE_FAMILY.name, guardrail.ERROR_PATTERN_ID),
    )


class StreamMechanics:
    """Judge one stream and stamp its answer when the window's tail settles.

    The citation label is decided only after the finished answer settles at a
    finish chunk or an unfinished end.  A trip returns through the refusal
    path before that decision and never reaches the stamp.
    """

    def __init__(self, state: guardrail.StreamState) -> None:
        self.state = state
        # The exception class of a failure the mechanics caught themselves, for
        # the relay to log: a class name, never a payload's text.
        self.failure: str | None = None
        self._last_envelope: dict[str, object] | None = None
        self._error_seen = False
        self._ended = False

    def process(
        self, payload: Mapping[str, object] | str
    ) -> tuple[list[dict[str, object] | str], bool]:
        """Process one parsed chunk or end marker and return payloads and trip state."""

        if self._ended:
            return [], self._tripped()
        if self._tripped():
            return [], True
        if self._error_seen and payload != DONE_EVENT:
            return [], False
        try:
            if payload == DONE_EVENT:
                return self._process_end_marker()
            event = self._parse_payload(payload)
            if "error" in event and "choices" not in event:
                return self._process_error(event)
            return self._process_chunk(event)
        except Exception as exc:  # noqa: BLE001 - mechanics fail closed without payload text.
            return self._error_trip(type(exc).__name__)

    def finish(self) -> tuple[list[dict[str, object] | str], bool]:
        """Settle held text when the upstream ends without an announcing payload."""

        if self._ended:
            return [], self._tripped()
        if self._tripped():
            return [], True
        try:
            payloads = self._settle_tail(self._last_envelope)
            self._ended = True
            if self._tripped():
                return payloads, True
            return payloads, False
        except Exception as exc:  # noqa: BLE001 - mechanics fail closed without payload text.
            return self._error_trip(type(exc).__name__)

    def fail_closed(self) -> tuple[list[dict[str, object] | str], bool]:
        """End the stream as a trip does for a failure outside ``process``.

        The reassembler's overrun is the caller's one such failure, so the
        error doctrine keeps a single home here rather than a second one in
        the relay.
        """

        if self._ended:
            return [], self._tripped()
        return self._error_trip()

    def _parse_payload(self, payload: Mapping[str, object] | str) -> dict[str, object]:
        if isinstance(payload, str):
            parsed = json.loads(payload)
            if not isinstance(parsed, dict):
                raise TypeError("stream payload is not an object")
            return parsed
        return dict(payload)

    def _process_chunk(
        self, event: Mapping[str, object]
    ) -> tuple[list[dict[str, object] | str], bool]:
        """Process one chunk, stamping only an untripped finished answer."""

        choices = event.get("choices")
        if not isinstance(choices, list):
            raise TypeError("stream choices are not a list")
        envelope = self._envelope(event)
        if not choices:
            self._last_envelope = envelope
            return [dict(event)], False
        if len(choices) != 1 or not isinstance(choices[0], Mapping):
            raise TypeError("stream choices are not one mapping")
        choice = choices[0]
        # One window judges one answer: an index the engine states must be zero,
        # and an absent one names the only choice there is.
        index = choice.get("index", 0)
        if not isinstance(index, int) or isinstance(index, bool) or index != 0:
            raise TypeError("stream choice index is not zero")
        delta_value = choice.get("delta", {})
        if delta_value is None:
            delta: Mapping[str, object] = {}
        elif isinstance(delta_value, Mapping):
            delta = delta_value
        else:
            raise TypeError("stream delta is not a mapping")
        texts = {key: delta[key] for key in TEXT_KEYS if key in delta}
        if any(not isinstance(value, str) for value in texts.values()):
            raise TypeError("stream text is not a string")
        reasoning_keys = [key for key in REASONING_KEYS if key in texts]
        if len(reasoning_keys) > 1:
            raise TypeError("stream has ambiguous reasoning")

        self._last_envelope = envelope
        was_finished = self.state.get("finished") is True
        checker = guardrail.StreamCheck(self.state, "content", judge=guardrail.judge_rendered)
        released = ""
        if "content" in texts:
            content_text = texts["content"]
            if not isinstance(content_text, str):
                raise TypeError("stream content is not a string")
            released = checker.append(content_text)
        finish_reason = choice.get("finish_reason")
        is_finished = finish_reason is not None
        if is_finished:
            released += checker.finish()
            self.state["finished"] = True
        if self._tripped():
            self._ended = True
            return self._trip_payloads(envelope), True
        if is_finished and not was_finished:
            released += stamp.tail_for(self._finished_answer())

        output_delta: dict[str, object] = {
            key: delta[key] for key in RELAYED_DELTA_KEYS if key in delta
        }
        if "content" in output_delta:
            del output_delta["content"]
        if released:
            output_delta["content"] = released
        if not output_delta and not is_finished:
            return [], False

        output_choice: dict[str, object] = {
            key: choice[key] for key in RELAYED_CHOICE_KEYS if key in choice
        }
        output_choice["delta"] = output_delta
        output = dict(envelope)
        output["choices"] = [output_choice]
        return [output], False

    def _process_error(
        self, event: Mapping[str, object]
    ) -> tuple[list[dict[str, object] | str], bool]:
        payloads = self._settle_tail(self._last_envelope)
        if self._tripped():
            return payloads, True
        self._error_seen = True
        payloads.append(dict(event))
        return payloads, False

    def _process_end_marker(self) -> tuple[list[dict[str, object] | str], bool]:
        payloads = self._settle_tail(self._last_envelope)
        self._ended = True
        if self._tripped():
            return payloads, True
        payloads.append(DONE_EVENT)
        return payloads, False

    def _settle_tail(
        self, envelope: Mapping[str, object] | None
    ) -> list[dict[str, object] | str]:
        """Settle an unfinished answer, stamping only after its trip check passes."""

        if self.state.get("finished") is True:
            return []
        checker = guardrail.StreamCheck(self.state, "content", judge=guardrail.judge_rendered)
        tail = checker.finish()
        self.state["finished"] = True
        if self._tripped():
            return self._trip_payloads(envelope)
        tail += stamp.tail_for(self._finished_answer())
        if not tail:
            return []
        return [self._content_chunk(envelope, tail)]

    def _finished_answer(self) -> str:
        """Read the accumulated content text, or empty text for an unreadable state."""

        content = self.state.get("content")
        if not isinstance(content, dict):
            return ""
        text = content.get("text")
        return text if isinstance(text, str) else ""

    @staticmethod
    def _envelope(event: Mapping[str, object]) -> dict[str, object]:
        return {key: value for key, value in event.items() if key != "choices"}

    @staticmethod
    def _base(envelope: Mapping[str, object] | None) -> dict[str, object]:
        """The envelope a rebuilt chunk carries: the engine's, or nothing minted."""

        return {"object": CHUNK_OBJECT} if envelope is None else dict(envelope)

    @classmethod
    def _content_chunk(
        cls, envelope: Mapping[str, object] | None, content: str
    ) -> dict[str, object]:
        output = cls._base(envelope)
        output["choices"] = [
            {"index": 0, "delta": {"content": content}, "finish_reason": None}
        ]
        return output

    def _trip_payloads(
        self, envelope: Mapping[str, object] | None
    ) -> list[dict[str, object] | str]:
        content = self.state.get("content")
        released = content.get("released") if isinstance(content, dict) else None
        answer_released = isinstance(released, int) and released > 0
        trip = self.state.get("trip")
        refusal = _refusal_for(trip.get("family") if isinstance(trip, dict) else None)
        if answer_released:
            refusal = guardrail.REFUSAL_SEPARATOR + refusal
        base = self._base(envelope)
        refusal_chunk = dict(base)
        refusal_chunk["choices"] = [
            {"index": 0, "delta": {"content": refusal}, "finish_reason": None}
        ]
        finish_chunk = dict(base)
        finish_chunk["choices"] = [
            {"index": 0, "delta": {}, "finish_reason": "stop"}
        ]
        return [refusal_chunk, finish_chunk, DONE_EVENT]

    def _error_trip(
        self, failure: str | None = None
    ) -> tuple[list[dict[str, object] | str], bool]:
        if failure is not None and self.failure is None:
            self.failure = failure
        if not self._tripped():
            _record_error_trip(self.state)
        self._ended = True
        return self._trip_payloads(self._last_envelope), True

    def _tripped(self) -> bool:
        return self.state.get("trip") is not None
