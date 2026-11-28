"""Parse research turn requests, decide the caller's claim, and settle retrieval."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final, Self, cast

from gideon.api.judged import source_for_header
from gideon.guardrail.writer import EVAL_SOURCE, bounded_id
from gideon.turn.config import OverrideRefusal, RetrievalConfig, effective_config
from gideon.turn.states import RequestWord, State, TurnFailure

_EVAL_FIELDS: Final = ("stop_after", "evidence", "overrides", "generation")
_REQUEST_FIELDS: Final = frozenset(("message", "message_id", "history", "kb_ids", "session_token", *_EVAL_FIELDS))
# exempt: a starting bound, corrected by the M1 reading.
_HISTORY_LIMIT: Final = 6
# exempt: a starting bound, corrected by the M1 reading.
_KB_IDS_LIMIT: Final = 16
# The record's ASCII generation names fit in fewer than 255 bytes.
_GENERATION_NAME_LIMIT: Final = 254


@dataclass(frozen=True, repr=False, slots=True)
class TurnRequest:
    """A request whose fields have passed shape validation."""

    message: str
    history: tuple[str, ...] | None
    kb_ids: tuple[str, ...] | None
    session_token: str | None
    stop_after: str | None
    evidence: tuple[str, ...] | None
    overrides: dict[str, object] | None
    message_id: str | None = None
    generation: str | None = None

    def without_token(self) -> Self:
        """Return a copy with the session token removed."""
        return replace(self, session_token=None)


def _string(fields: dict[str, object], key: RequestWord) -> str | None:
    if key not in fields:
        return None
    value = fields[key]
    if not isinstance(value, str):
        raise TurnFailure(State.INVALID_REQUEST, key)
    return value


def _strings(fields: dict[str, object], key: RequestWord) -> tuple[str, ...] | None:
    if key not in fields:
        return None
    value = fields[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TurnFailure(State.INVALID_REQUEST, key)
    return tuple(value)


def _kb_ids(fields: dict[str, object]) -> tuple[str, ...] | None:
    values = _strings(fields, "kb_ids")
    if values is None:
        return None
    ids = tuple(dict.fromkeys(values))
    if len(ids) > _KB_IDS_LIMIT or any(bounded_id(value) is None for value in ids):
        raise TurnFailure(State.INVALID_REQUEST, "kb_ids")
    return ids


def _invalid_constant(_: str) -> None:
    raise TurnFailure(State.INVALID_REQUEST)


def parse(body: bytes | bytearray) -> TurnRequest:
    """Validate the shape of the request before deciding its claim."""
    try:
        fields = json.loads(body, parse_constant=_invalid_constant)
    except (ValueError, RecursionError):
        raise TurnFailure(State.INVALID_REQUEST) from None
    if not isinstance(fields, dict) or fields.keys() - _REQUEST_FIELDS:
        raise TurnFailure(State.INVALID_REQUEST)

    message = _string(fields, "message")
    if message is None or not message.strip():
        raise TurnFailure(State.INVALID_REQUEST, "message")
    message_id = _string(fields, "message_id")
    if message_id is not None and bounded_id(message_id) is None:
        raise TurnFailure(State.INVALID_REQUEST, "message_id")
    history = _strings(fields, "history")
    if history is not None and len(history) > _HISTORY_LIMIT:
        raise TurnFailure(State.INVALID_REQUEST, "history")
    overrides = fields.get("overrides")
    if "overrides" in fields and not isinstance(overrides, dict):
        raise TurnFailure(State.INVALID_REQUEST, "overrides")
    generation = _string(fields, "generation")
    if generation is not None and (not generation or len(generation) > _GENERATION_NAME_LIMIT):
        raise TurnFailure(State.INVALID_REQUEST, "generation")
    return TurnRequest(
        message, history, _kb_ids(fields), _string(fields, "session_token"),
        _string(fields, "stop_after"), _strings(fields, "evidence"), overrides,
        message_id=message_id, generation=generation,
    )


def claim(request: TurnRequest, source_values: Sequence[str], eval_identity: str) -> str:
    """Decide the claim and enforce the eval-only fields."""
    source = source_for_header(source_values, eval_identity)
    if source != EVAL_SOURCE:
        for key in _EVAL_FIELDS:
            if getattr(request, key) is not None:
                raise TurnFailure(State.EVAL_ONLY_FIELD, key)
    return source


def settle(request: TurnRequest, *, reranker: bool) -> RetrievalConfig:
    """Validate the overrides and requested stage after caller admission."""
    config = effective_config(request.overrides, reranker=reranker)
    if isinstance(config, OverrideRefusal):
        if config.state == State.OVERRIDE_NOT_ALLOWED:
            raise TurnFailure(State.OVERRIDE_NOT_ALLOWED, "overrides")
        # The config validator only reports an invalid value under an allowed key.
        raise TurnFailure(State.OVERRIDE_INVALID, cast(RequestWord, config.key))
    if request.stop_after not in (None, "retrieve"):
        raise TurnFailure(State.STAGE_NOT_BUILT, "stop_after")
    if request.evidence is not None:
        raise TurnFailure(State.STAGE_NOT_BUILT, "evidence")
    if request.kb_ids:
        raise TurnFailure(State.KB_IDS_NOT_BUILT, "kb_ids")
    return config
