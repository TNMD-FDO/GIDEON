"""Cut the fused pool and read passage text from the pinned lexical index."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Final

import httpx

from gideon.turn.fusion import FusionResult, PoolEntry
from gideon.turn.states import StageWord, State, TurnFailure

# E25 starts with this pool depth. The final set replaces the cut.
TRACER_CUT: Final = 20
# exempt: a starting bound, corrected by the M1 reading.
PASSAGES_TIMEOUT_SECONDS: Final = 2.0


@dataclass(frozen=True, slots=True)
class Passage:
    """One ranked chunk and its text from the lexical serving copy."""

    chunk_id: str
    pool_rank: int
    caption: str = field(repr=False)
    text: str = field(repr=False)


def cut(fusion: FusionResult) -> tuple[PoolEntry, ...]:
    """Keep the head of the pool in its existing rank order."""

    return fusion.entries[:TRACER_CUT]


def _parse_reply(reply: object, entries: Sequence[PoolEntry], word: StageWord) -> tuple[Passage, ...]:
    docs = reply.get("docs") if isinstance(reply, dict) else None
    if not isinstance(docs, list) or len(docs) != len(entries):
        raise TurnFailure(State.STORE_REPLY_INVALID, word)

    ranks = {entry.chunk_id: entry.pool_rank for entry in entries}
    passages: dict[str, Passage] = {}
    for doc in docs:
        if not isinstance(doc, dict) or "error" in doc or doc.get("found") is not True:
            raise TurnFailure(State.STORE_REPLY_INVALID, word)
        chunk_id, source = doc.get("_id"), doc.get("_source")
        if (
            not isinstance(chunk_id, str) or chunk_id not in ranks or chunk_id in passages
            or not isinstance(source, dict)
        ):
            raise TurnFailure(State.STORE_REPLY_INVALID, word)
        text = source.get("text")
        # Older serving copies can lack captions because they predate the case name field.
        caption = source.get("caption", "")
        if not isinstance(text, str) or not text or not isinstance(caption, str):
            raise TurnFailure(State.STORE_REPLY_INVALID, word)
        passages[chunk_id] = Passage(chunk_id, ranks[chunk_id], caption, text)

    return tuple(passages[entry.chunk_id] for entry in entries)


async def fetch(
    client: httpx.AsyncClient, user: str, password_file: Path, index: str, entries: Sequence[PoolEntry],
    *, word: StageWord = "passages", timeout: float = PASSAGES_TIMEOUT_SECONDS,
) -> tuple[Passage, ...]:
    """Fetch the selected entries in one multi-get and return passages in the supplied order."""

    if not entries:
        return ()

    body = {"docs": [{"_id": entry.chunk_id, "_source": ["text", "caption"]} for entry in entries]}
    try:
        password = password_file.read_text(encoding="utf-8").strip()
        if not password:
            raise TurnFailure(State.STORE_UNAVAILABLE, word)
        async with asyncio.timeout(timeout):
            response = await client.post(f"/{index}/_mget", auth=(user, password), json=body)
    except (TimeoutError, httpx.TimeoutException):
        raise TurnFailure(State.RETRIEVAL_TIMEOUT, word) from None
    except (OSError, UnicodeError, httpx.RequestError):
        raise TurnFailure(State.STORE_UNAVAILABLE, word) from None

    if response.status_code != HTTPStatus.OK:
        raise TurnFailure(State.STORE_UNAVAILABLE, word)
    try:
        reply = response.json()
    except (ValueError, RecursionError):
        raise TurnFailure(State.STORE_REPLY_INVALID, word) from None
    return _parse_reply(reply, entries, word)
