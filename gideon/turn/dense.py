"""Query the pinned vector collection and decode its ranked chunk identifiers."""

import asyncio
import time
from collections.abc import Callable, Sequence, Set
from http import HTTPStatus
from pathlib import Path
from typing import Final

import httpx

from gideon.turn.fusion import LegHit
from gideon.turn.provenance import RequestSpan
from gideon.turn.states import LegWord, State, TurnFailure
from gideon.turn.tier import PRECEDENTIAL_VALUES

# exempt: a starting bound, corrected by the M1 reading.
DENSE_TIMEOUT_SECONDS: Final = 2.0


def query_body(
    vector: Sequence[float], limit: int, *, collapse: bool, courts: Set[str] | None = None,
) -> dict[str, object]:
    """Build the plain or grouped vector query for one leg."""

    body: dict[str, object] = {
        "query": list(vector),
        "limit": limit,
        "with_payload": ["chunk_id", "dupe_cluster", "boilerplate", "court", "precedential"],
        "with_vector": False,
    }
    if collapse:
        body.update(group_by="dupe_cluster", group_size=1)
    if courts is not None:
        body["filter"] = {"must": [{"key": "court", "match": {"any": sorted(courts)}}]}
    return body


def _group_hit(group: object, *, leg: LegWord) -> object:
    if not isinstance(group, dict) or not isinstance(group.get("id"), str):
        raise TurnFailure(State.STORE_REPLY_INVALID, leg)
    hits = group.get("hits")
    if not isinstance(hits, list) or len(hits) != 1:
        raise TurnFailure(State.STORE_REPLY_INVALID, leg)
    return hits[0]


def _hit(row: object, rank: int, *, leg: LegWord) -> LegHit:
    payload = row.get("payload") if isinstance(row, dict) else None
    if not isinstance(payload, dict):
        raise TurnFailure(State.STORE_REPLY_INVALID, leg)
    chunk_id, cluster, boilerplate = (payload.get(key) for key in ("chunk_id", "dupe_cluster", "boilerplate"))
    court, precedential = payload.get("court"), payload.get("precedential")
    if not (
        isinstance(chunk_id, str) and chunk_id
        and isinstance(cluster, str) and cluster
        and isinstance(boilerplate, bool)
        and isinstance(court, str) and court
        and precedential in PRECEDENTIAL_VALUES
    ):
        raise TurnFailure(State.STORE_REPLY_INVALID, leg)
    return LegHit(chunk_id, rank, cluster, boilerplate, court, precedential)


def parse_reply(reply: object, *, collapse: bool, leg: LegWord = "dense") -> tuple[LegHit, ...]:
    """Decode hits in store order, requiring a complete successful reply."""

    if not isinstance(reply, dict) or reply.get("status") != "ok":
        raise TurnFailure(State.STORE_REPLY_INVALID, leg)
    result = reply.get("result")
    rows = result.get("groups" if collapse else "points") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise TurnFailure(State.STORE_REPLY_INVALID, leg)
    return tuple(
        _hit(_group_hit(row, leg=leg) if collapse else row, rank, leg=leg)
        for rank, row in enumerate(rows, start=1)
    )


async def search(
    client: httpx.AsyncClient, key_file: Path, collection: str, vector: Sequence[float], limit: int, *,
    collapse: bool, courts: Set[str] | None = None, leg: LegWord = "dense",
    clock: Callable[[], float] = time.monotonic, timeout: float = DENSE_TIMEOUT_SECONDS,
) -> tuple[tuple[LegHit, ...], RequestSpan]:
    """Send one request with the mounted key and return its hits and request span."""

    path = f"/collections/{collection}/points/query"
    if collapse:
        path += "/groups"
    body = query_body(vector, limit, collapse=collapse, courts=courts)
    try:
        key = key_file.read_text(encoding="utf-8").strip()
        if not key:
            raise TurnFailure(State.STORE_UNAVAILABLE, leg)
        async with asyncio.timeout(timeout):
            started = clock()
            response = await client.post(path, headers={"api-key": key}, json=body)
            span = RequestSpan(started, clock())
    except (TimeoutError, httpx.TimeoutException):
        raise TurnFailure(State.RETRIEVAL_TIMEOUT, leg) from None
    except (OSError, UnicodeError, httpx.RequestError):
        raise TurnFailure(State.STORE_UNAVAILABLE, leg) from None
    if response.status_code != HTTPStatus.OK:
        raise TurnFailure(State.STORE_UNAVAILABLE, leg)
    try:
        reply = response.json()
    except ValueError:
        raise TurnFailure(State.STORE_REPLY_INVALID, leg) from None
    return parse_reply(reply, collapse=collapse, leg=leg), span
