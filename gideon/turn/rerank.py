"""Select fused passages, queue and score them with the reranker, then order the pool."""

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from http import HTTPStatus
from itertools import islice
from pathlib import Path
from typing import Final

import httpx

from gideon.turn import embedder
from gideon.turn.fusion import PoolEntry
from gideon.turn.instruction import RETRIEVAL_INSTRUCTION
from gideon.turn.passages import Passage
from gideon.turn.states import State, TurnFailure

RERANK_SERVED_NAME: Final = "gideon-rerank-2"
# exempt: a starting bound, corrected by the M2 reading.
RERANK_TIMEOUT_SECONDS: Final = 10.0


class Gate:
    """Bound in-flight reranker calls and admit waiting turns in arrival order."""

    def __init__(self, seats: int) -> None:
        self._seats = asyncio.BoundedSemaphore(seats)
        self.in_flight = 0
        self.waiters = 0
        self.timeouts = 0

    @asynccontextmanager
    async def seat(
        self, timeout: float, *, clock: Callable[[], float] = time.monotonic,
        on_wait: Callable[[float], None] | None = None,
    ) -> AsyncIterator[float]:
        """Wait for one seat and hold it through the call, yielding the wait in seconds.

        ``on_wait`` receives the wait however it ends: seated, timed out, or cancelled.
        """

        started = clock()
        self.waiters += 1
        try:
            async with asyncio.timeout(timeout):
                await self._seats.acquire()
        except TimeoutError:
            self.timeouts += 1
            raise TurnFailure(State.RERANK_QUEUE_TIMEOUT, "rerank") from None
        finally:
            self.waiters -= 1
            waited = clock() - started
            if on_wait is not None:
                on_wait(waited)

        self.in_flight += 1
        try:
            yield waited
        finally:
            self.in_flight -= 1
            self._seats.release()


def _seated(entry: PoolEntry) -> bool:
    return entry.pinned or entry.reserved is not None


def select(entries: Sequence[PoolEntry], depth: int) -> tuple[PoolEntry, ...]:
    """Select every seated entry and the first ``depth`` others, in fused order.

    A pin or a reserved seat is scored whatever its place, so a seat the voting
    legs never returned still reaches the scored set.
    """

    chosen = {entry.chunk_id for entry in islice((entry for entry in entries if not _seated(entry)), depth)}
    return tuple(entry for entry in entries if _seated(entry) or entry.chunk_id in chosen)


def _parse_reply(reply: object, count: int) -> dict[int, float]:
    results = reply.get("results") if isinstance(reply, dict) else None
    if not isinstance(results, list) or len(results) != count:
        raise TurnFailure(State.RERANKER_REPLY_INVALID, "rerank")

    scores: dict[int, float] = {}
    for result in results:
        if not isinstance(result, dict):
            raise TurnFailure(State.RERANKER_REPLY_INVALID, "rerank")
        index, value = result.get("index"), result.get("relevance_score")
        if type(index) is not int or not 0 <= index < count or index in scores:
            raise TurnFailure(State.RERANKER_REPLY_INVALID, "rerank")
        if (type(value) is not int and type(value) is not float) or not 0 <= value <= 1:
            raise TurnFailure(State.RERANKER_REPLY_INVALID, "rerank")
        scores[index] = float(value)
    return scores


async def score(
    client: httpx.AsyncClient, key_file: Path | None, message: str, passages: Sequence[Passage],
) -> dict[str, float]:
    """Score the whole passages in length order and return scores by chunk id."""

    if not passages:
        return {}

    ordered = sorted(passages, key=lambda passage: len(passage.text))
    try:
        key = key_file.read_text(encoding="utf-8").strip() if key_file is not None else ""
    except (OSError, UnicodeError):
        raise TurnFailure(State.RERANKER_UNAVAILABLE, "rerank") from None
    if not key:
        raise TurnFailure(State.RERANKER_UNAVAILABLE, "rerank")

    body = {
        "model": RERANK_SERVED_NAME,
        "query": message,
        "documents": [passage.text for passage in ordered],
        "instruction": RETRIEVAL_INSTRUCTION,
    }
    deadline = asyncio.timeout(RERANK_TIMEOUT_SECONDS)
    try:
        async with deadline:
            response = await client.post("/v1/rerank", headers={"Authorization": f"Bearer {key}"}, json=body)
    except (OSError, UnicodeError, httpx.HTTPError):
        state = State.RETRIEVAL_TIMEOUT if deadline.expired() else State.RERANKER_UNAVAILABLE
        raise TurnFailure(state, "rerank") from None

    ok = response.status_code == HTTPStatus.OK
    if not ok and response.status_code != HTTPStatus.BAD_REQUEST:
        raise TurnFailure(State.RERANKER_UNAVAILABLE, "rerank")
    try:
        reply = response.json()
    except (ValueError, RecursionError):
        state = State.RERANKER_REPLY_INVALID if ok else State.RERANKER_UNAVAILABLE
        raise TurnFailure(state, "rerank") from None
    if not ok:
        state = State.PAIR_OUTSIDE_WINDOW if embedder.outside_window(reply) else State.RERANKER_UNAVAILABLE
        raise TurnFailure(state, "rerank")

    scores = _parse_reply(reply, len(ordered))
    return {passage.chunk_id: scores[index] for index, passage in enumerate(ordered)}


def order(entries: Sequence[PoolEntry], scores: Mapping[str, float]) -> tuple[PoolEntry, ...]:
    """Rank pins first, then scored unpinned hits, then the unscored tail."""

    def priority(entry: PoolEntry) -> tuple[int, float, int]:
        if entry.pinned:
            return 0, 0.0, entry.pool_rank
        value = scores.get(entry.chunk_id)
        if value is not None:
            return 1, -value, entry.pool_rank
        return 2, 0.0, entry.pool_rank

    return tuple(
        replace(entry, stage2_score=scores.get(entry.chunk_id), rank=rank)
        for rank, entry in enumerate(sorted(entries, key=priority), start=1)
    )
