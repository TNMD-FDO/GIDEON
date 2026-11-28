"""The research turn's event vocabulary, payloads, and SSE encoding."""

import json
from collections.abc import Mapping
from typing import Literal, Protocol, overload

EventKind = Literal["passage", "phase", "text", "answer", "failure"]
EndWord = Literal["complete", "truncated", "tripped", "abstained"]


class _FailureBody(Protocol):
    def to_body(self) -> dict[str, dict[str, object]]: ...


def encode(kind: EventKind, payload: Mapping[str, object]) -> bytes:
    """Encode one event with compact JSON on a single data line."""

    data = json.dumps(dict(payload), separators=(",", ":"), allow_nan=False)
    return f"event: {kind}\ndata: {data}\n\n".encode()


def passage(number: int, chunk_id: str, pool_rank: int) -> bytes:
    return encode("passage", {"number": number, "chunk_id": chunk_id, "pool_rank": pool_rank})


@overload
def phase(stage: Literal["retrieve"], detail: Mapping[str, object]) -> bytes: ...


@overload
def phase(stage: Literal["generate"], detail: int) -> bytes: ...


def phase(stage: Literal["retrieve", "generate"], detail: Mapping[str, object] | int) -> bytes:
    if isinstance(detail, int):
        return encode("phase", {"stage": stage, "attempt": detail})
    return encode("phase", {"stage": stage, "retrieval": dict(detail)})


def text(released: str) -> bytes:
    return encode("text", {"text": released})


def answer(
    displayed: str, end: EndWord, timings_ms: Mapping[str, float | None], *, family: str | None = None,
) -> bytes:
    payload: dict[str, object] = {"text": displayed, "end": end}
    if family is not None:
        payload["family"] = family
    # Milliseconds, as the retrieval reply carries its own.
    payload["timings_ms"] = dict(timings_ms)
    return encode("answer", payload)


def failure(error: _FailureBody) -> bytes:
    return encode("failure", error.to_body())
