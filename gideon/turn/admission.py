"""Validate research turn requests and admit the configured evaluation caller."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, cast

from gideon.api.judged import source_for_header
from gideon.guardrail.writer import EVAL_SOURCE
from gideon.turn.config import OverrideRefusal, RetrievalConfig, effective_config
from gideon.turn.states import RequestWord, State, TurnFailure

_EVAL_FIELDS: Final = ("stop_after", "evidence", "overrides")
_REQUEST_FIELDS: Final = frozenset(("message", "history", "kb_ids", "session_token", *_EVAL_FIELDS))
# exempt: a starting bound, corrected by the M1 reading.
_HISTORY_LIMIT: Final = 6


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """A request whose fields have passed shape validation."""

    message: str
    history: tuple[str, ...] | None
    kb_ids: tuple[str, ...] | None
    session_token: str | None
    stop_after: str | None
    evidence: tuple[str, ...] | None
    overrides: dict[str, object] | None


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


def _invalid_constant(_: str) -> None:
    raise TurnFailure(State.INVALID_REQUEST)


def _parse(body: bytes | bytearray) -> TurnRequest:
    try:
        fields = json.loads(body, parse_constant=_invalid_constant)
    except (ValueError, RecursionError):
        raise TurnFailure(State.INVALID_REQUEST) from None
    if not isinstance(fields, dict) or fields.keys() - _REQUEST_FIELDS:
        raise TurnFailure(State.INVALID_REQUEST)

    message = _string(fields, "message")
    if message is None or not message.strip():
        raise TurnFailure(State.INVALID_REQUEST, "message")
    history = _strings(fields, "history")
    if history is not None and len(history) > _HISTORY_LIMIT:
        raise TurnFailure(State.INVALID_REQUEST, "history")
    overrides = fields.get("overrides")
    if "overrides" in fields and not isinstance(overrides, dict):
        raise TurnFailure(State.INVALID_REQUEST, "overrides")
    return TurnRequest(
        message, history, _strings(fields, "kb_ids"), _string(fields, "session_token"),
        _string(fields, "stop_after"), _strings(fields, "evidence"), overrides,
    )


def admit(
    body: bytes | bytearray, source_values: Sequence[str], eval_identity: str,
) -> tuple[TurnRequest, RetrievalConfig]:
    """Validate the shape, the caller, the overrides, and the requested stage in order."""

    request = _parse(body)
    if source_for_header(source_values, eval_identity) != EVAL_SOURCE:
        for key in _EVAL_FIELDS:
            if getattr(request, key) is not None:
                raise TurnFailure(State.EVAL_ONLY_FIELD, key)
        raise TurnFailure(State.CALLER_NOT_ADMITTED)

    config = effective_config(request.overrides)
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
    return request, config
