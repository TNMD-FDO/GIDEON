"""Query the pinned vector collection and decode its ranked chunk identifiers."""

import asyncio
import time
from collections.abc import Callable, Sequence
from http import HTTPStatus
from pathlib import Path
from typing import Final

import httpx

from gideon.turn.fusion import LegHit
from gideon.turn.provenance import RequestSpan
from gideon.turn.states import State, TurnFailure

# exempt: a starting bound, corrected by the M1 reading.
DENSE_TIMEOUT_SECONDS: Final = 2.0


def query_body(vector: Sequence[float], limit: int, *, collapse: bool) -> dict[str, object]:
    """Build the plain or grouped vector query for one leg."""

    body: dict[str, object] = {
        "query": list(vector),
        "limit": limit,
        "with_payload": ["chunk_id", "dupe_cluster", "boilerplate"],
        "with_vector": False,
    }
    if collapse:
        body.update(group_by="dupe_cluster", group_size=1)
    return body


def _group_hit(group: object) -> object:
    if not isinstance(group, dict) or not isinstance(group.get("id"), str):
        raise TurnFailure(State.STORE_REPLY_INVALID, "dense")
    hits = group.get("hits")
    if not isinstance(hits, list) or len(hits) != 1:
        raise TurnFailure(State.STORE_REPLY_INVALID, "dense")
    return hits[0]


def _hit(row: object, rank: int) -> LegHit:
    payload = row.get("payload") if isinstance(row, dict) else None
    if not isinstance(payload, dict):
        raise TurnFailure(State.STORE_REPLY_INVALID, "dense")
    chunk_id, cluster, boilerplate = (payload.get(key) for key in ("chunk_id", "dupe_cluster", "boilerplate"))
    if not (
        isinstance(chunk_id, str) and chunk_id
        and isinstance(cluster, str) and cluster
        and isinstance(boilerplate, bool)
    ):
        raise TurnFailure(State.STORE_REPLY_INVALID, "dense")
    return LegHit(chunk_id, rank, cluster, boilerplate)


def parse_reply(reply: object, *, collapse: bool) -> tuple[LegHit, ...]:
    """Decode hits in store order, requiring a complete successful reply."""

    if not isinstance(reply, dict) or reply.get("status") != "ok":
        raise TurnFailure(State.STORE_REPLY_INVALID, "dense")
    result = reply.get("result")
    rows = result.get("groups" if collapse else "points") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise TurnFailure(State.STORE_REPLY_INVALID, "dense")
    return tuple(_hit(_group_hit(row) if collapse else row, rank) for rank, row in enumerate(rows, start=1))


async def search(
    client: httpx.AsyncClient, key_file: Path, collection: str, vector: Sequence[float], limit: int, *,
    collapse: bool, clock: Callable[[], float] = time.monotonic, timeout: float = DENSE_TIMEOUT_SECONDS,
) -> tuple[tuple[LegHit, ...], RequestSpan]:
    """Send one request with the mounted key and return its hits and request span."""

    path = f"/collections/{collection}/points/query"
    if collapse:
        path += "/groups"
    body = query_body(vector, limit, collapse=collapse)
    try:
        key = key_file.read_text(encoding="utf-8").strip()
        if not key:
            raise TurnFailure(State.STORE_UNAVAILABLE, "dense")
        async with asyncio.timeout(timeout):
            started = clock()
            response = await client.post(path, headers={"api-key": key}, json=body)
            span = RequestSpan(started, clock())
    except (TimeoutError, httpx.TimeoutException):
        raise TurnFailure(State.RETRIEVAL_TIMEOUT, "dense") from None
    except (OSError, UnicodeError, httpx.RequestError):
        raise TurnFailure(State.STORE_UNAVAILABLE, "dense") from None
    if response.status_code != HTTPStatus.OK:
        raise TurnFailure(State.STORE_UNAVAILABLE, "dense")
    try:
        reply = response.json()
    except ValueError:
        raise TurnFailure(State.STORE_REPLY_INVALID, "dense") from None
    return parse_reply(reply, collapse=collapse), span
