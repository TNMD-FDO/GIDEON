"""Judge streamed Chat Completions payloads without owning transport I/O."""

import json
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from typing import Final

from gideon import guardrail

from . import progress, stamp
from .errors import error_body
from .progress import REASONING_KEYS
from .sse import DONE_EVENT

# A downstream choice is rebuilt from these keys, never scrubbed of the ones it
# withholds: a field the engine adds or renames at a pin bump — `reasoning`,
# `reasoning_content`, `logprobs`, token ids — is absent because it was never
# copied, not because a list of names caught it.
RELAYED_CHOICE_KEYS: Final[tuple[str, ...]] = ("index", "finish_reason")
# Status chunks use only these engine envelope fields. Rebuilding from names
# keeps an engine extra from riding the frontend's status event.
STATUS_ENVELOPE_KEYS: Final[tuple[str, ...]] = ("id", "object", "created", "model")
RELAYED_DELTA_KEYS: Final[tuple[str, ...]] = ("role", "content", "tool_calls")
RELAYED_MESSAGE_KEYS: Final[tuple[str, ...]] = ("role", "content", "tool_calls")
# A reasoning delta under any of the three names is withheld whole: nothing
# leaves under its key. These names decide and validate; the progress line is
# the seat's one sign that the model is reasoning. Relayed keys are named above.
TEXT_KEYS: Final[tuple[str, ...]] = (*REASONING_KEYS, "content")
# With no valid chunk seen, no envelope is minted: `object` and `choices` are
# all the pinned frontend and the door read of a chunk.
CHUNK_OBJECT: Final[str] = "chat.completion.chunk"
UNJUDGED_ERROR_CODE: Final[str] = "completion_unjudged"
UNJUDGED_ERROR: Final[dict[str, dict[str, object]]] = error_body(
    "The completion could not be judged.",
    error_type="server_error",
    code=UNJUDGED_ERROR_CODE,
)


def source_for_header(values: Sequence[str], eval_identity: str) -> str:
    """Return the content-free source word for one forwarded header."""

    if len(values) != 1:
        return guardrail.USER_SOURCE
    value = values[0].strip()
    identity = eval_identity.strip()
    if not value or not identity:
        return guardrail.USER_SOURCE
    return (
        guardrail.EVAL_SOURCE
        if value.casefold() == identity.casefold()
        else guardrail.USER_SOURCE
    )


def chat_id_for_header(values: Sequence[str]) -> str | None:
    """Return the one non-empty forwarded chat id, or ``None``."""

    if len(values) != 1:
        return None
    value = values[0].strip()
    return value or None


def stream_state_from_body(
    body: bytes, source: str, chat_id: str | None = None
) -> guardrail.StreamState:
    """Build the stream's request context from the caller's JSON body."""

    try:
        parsed = json.loads(body)
    except Exception:  # noqa: BLE001 - unreadable request bodies use the strict empty stash.
        return guardrail.StreamState({}, frozenset(), source=source, chat_id=chat_id)
    if not isinstance(parsed, dict):
        return guardrail.StreamState({}, frozenset(), source=source, chat_id=chat_id)

    model = parsed.get("model")
    branch = model if isinstance(model, str) else None
    messages = parsed.get("messages")
    if not isinstance(messages, list):
        return guardrail.StreamState(
            {}, frozenset(), branch=branch, source=source, chat_id=chat_id
        )
    try:
        supplied, contexts = guardrail.message_context(messages, len(messages))
    except Exception:  # noqa: BLE001 - malformed context uses the strict empty stash.
        supplied, contexts = {}, frozenset()
    return guardrail.StreamState(
        supplied, contexts, branch=branch, source=source, chat_id=chat_id
    )


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
                    (content,), content, state.supplied, state.confirmation
                )
                if isinstance(result, guardrail.Trip):
                    guardrail.record_stream_trip(state, result)
                    rebuilt_message["content"] = guardrail.refusal_for(result.family)
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


def _record_error_trip(state: guardrail.StreamState) -> None:
    guardrail.record_stream_trip(
        state,
        guardrail.Trip(guardrail.DEADLINE_FAMILY.name, guardrail.ERROR_PATTERN_ID),
    )


class StreamMechanics:
    """Judge one stream, send its progress line, and stamp its settled answer.

    The citation label is decided only after the finished answer settles at a
    finish chunk or an unfinished end.  A trip returns through the refusal
    path before that decision and never reaches the stamp.  The first reasoning
    delta under any reasoning name is withheld whole and opens the progress
    line, the seat's one sign of reasoning. Later deltas tick it, and the first
    answer or any end closes it with text built by ``progress``. Nothing leaves
    under a reasoning key.
    """

    def __init__(
        self,
        state: guardrail.StreamState,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.state = state
        # The exception class of a failure the mechanics caught themselves, for
        # the relay to log: a class name, never a payload's text.
        self.failure: str | None = None
        self._last_envelope: dict[str, object] | None = None
        self._error_seen = False
        self._ended = False
        # The line belongs to the stream mechanics, not the judge's answer
        # state: only the mechanics sees reasoning deltas and stream endings.
        self._clock = clock
        self._progress_start: float | None = None
        self._progress_next_due = progress.PERIOD_SECONDS
        self._progress_closed = False

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
            closing = self._close_progress()
            if closing is not None:
                payloads.insert(0, closing)
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
            # Relayed in the engine's own key order, less a top-level event.
            return [
                {key: value for key, value in event.items() if key != progress.STATUS_EVENT_KEY}
            ], False
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
        was_finished = self.state.finished
        checker = guardrail.StreamCheck(self.state, judge=guardrail.judge_rendered)
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
            self.state.finished = True
        progress_chunk: dict[str, object] | None = None
        if (
            self._tripped()
            or ("content" in texts and texts["content"] != "")
            or is_finished
        ):
            progress_chunk = self._close_progress()
        elif reasoning_keys:
            progress_chunk = (
                self._open_progress()
                if self._progress_start is None
                else self._tick_progress()
            )
        leading: list[dict[str, object] | str] = (
            [progress_chunk] if progress_chunk is not None else []
        )
        if self._tripped():
            self._ended = True
            return leading + self._trip_payloads(envelope), True
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
            return leading, False

        output_choice: dict[str, object] = {
            key: choice[key] for key in RELAYED_CHOICE_KEYS if key in choice
        }
        output_choice["delta"] = output_delta
        output = dict(envelope)
        output["choices"] = [output_choice]
        return [*leading, output], False

    def _process_error(
        self, event: Mapping[str, object]
    ) -> tuple[list[dict[str, object] | str], bool]:
        payloads = self._settle_tail(self._last_envelope)
        closing = self._close_progress()
        if closing is not None:
            payloads.insert(0, closing)
        if self._tripped():
            return payloads, True
        self._error_seen = True
        payloads.append(self._envelope(event))
        return payloads, False

    def _process_end_marker(self) -> tuple[list[dict[str, object] | str], bool]:
        payloads = self._settle_tail(self._last_envelope)
        closing = self._close_progress()
        if closing is not None:
            payloads.insert(0, closing)
        self._ended = True
        if self._tripped():
            return payloads, True
        payloads.append(DONE_EVENT)
        return payloads, False

    def _settle_tail(
        self, envelope: Mapping[str, object] | None
    ) -> list[dict[str, object] | str]:
        """Settle an unfinished answer, stamping only after its trip check passes."""

        if self.state.finished:
            return []
        checker = guardrail.StreamCheck(self.state, judge=guardrail.judge_rendered)
        tail = checker.finish()
        self.state.finished = True
        if self._tripped():
            return self._trip_payloads(envelope)
        tail += stamp.tail_for(self._finished_answer())
        if not tail:
            return []
        return [self._content_chunk(envelope, tail)]

    def _finished_answer(self) -> str:
        """Read the accumulated content text."""

        return self.state.content.text

    @staticmethod
    def _envelope(event: Mapping[str, object]) -> dict[str, object]:
        # The frontend replays a top-level event as its own; only progress
        # builds one, so an engine event cannot be copied into a reply.
        return {
            key: value
            for key, value in event.items()
            if key not in ("choices", progress.STATUS_EVENT_KEY)
        }

    def _status_chunk(self, description: str, *, done: bool) -> dict[str, object]:
        envelope = self._last_envelope
        output: dict[str, object] = (
            {"object": CHUNK_OBJECT}
            if envelope is None
            else {key: envelope[key] for key in STATUS_ENVELOPE_KEYS if key in envelope}
        )
        output["choices"] = []
        output[progress.STATUS_EVENT_KEY] = progress.build_status_event(
            description, done
        )
        return output

    def _elapsed_seconds(self) -> int:
        start = self._progress_start
        if start is None:
            return 0
        return max(0, int(self._clock() - start))

    def _open_progress(self) -> dict[str, object] | None:
        if self._progress_closed or self._progress_start is not None:
            return None
        self._progress_start = self._clock()
        return self._status_chunk(progress.opening_description(), done=False)

    def _tick_progress(self) -> dict[str, object] | None:
        if self._progress_closed or self._progress_start is None:
            return None
        elapsed = self._elapsed_seconds()
        if elapsed < self._progress_next_due:
            return None
        self._progress_next_due = (
            elapsed // progress.PERIOD_SECONDS + 1
        ) * progress.PERIOD_SECONDS
        return self._status_chunk(progress.running_description(elapsed), done=False)

    def _close_progress(self) -> dict[str, object] | None:
        if self._progress_closed:
            return None
        self._progress_closed = True
        if self._progress_start is None:
            return None
        return self._status_chunk(
            progress.closing_description(self._elapsed_seconds()), done=True
        )

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
        answer_released = self.state.content.released > 0
        trip = self.state.trip
        refusal = guardrail.refusal_for(trip.family if trip is not None else None)
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
        closing: dict[str, object] | None = None
        with suppress(Exception):
            closing = self._close_progress()
        if not self._tripped():
            _record_error_trip(self.state)
        self._ended = True
        payloads = self._trip_payloads(self._last_envelope)
        return ([closing] if closing is not None else []) + payloads, True

    def _tripped(self) -> bool:
        return self.state.trip is not None
