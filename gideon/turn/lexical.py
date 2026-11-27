"""Search the pinned lexical index and decode its ranked chunk identifiers."""

import asyncio
import time
from collections.abc import Callable, Sequence
from http import HTTPStatus
from pathlib import Path
from typing import Final

import httpx

from gideon.turn.fusion import LegHit
from gideon.turn.provenance import RequestSpan
from gideon.turn.states import LegWord, State, TurnFailure
from gideon.turn.tier import PRECEDENTIAL_VALUES

# exempt: a starting bound, corrected by the M1 reading.
LEXICAL_TIMEOUT_SECONDS: Final = 2.0


def court_filter(courts: Sequence[str]) -> dict[str, dict[str, list[str]]]:
    """Build the OpenSearch terms filter shared by lexical and object searches."""

    return {"terms": {"court": sorted(courts)}}


def query_body(
    message: str, limit: int, *, collapse: bool, courts: Sequence[str] | None = None,
) -> dict[str, object]:
    """Build the lexical match query with the requested court filter and cluster collapse.

    The court filter is a ``post_filter``: it returns the hits and scores a filter inside the
    query's ``bool`` returns, collapse included, while the match keeps its top-k skipping, which
    an in-query filter over most of the index costs about six times over.
    """

    body: dict[str, object] = {
        "query": {"match": {"text": message}},
        "size": limit,
        "_source": ["dupe_cluster", "boilerplate", "court", "precedential"],
    }
    if courts is not None:
        body["post_filter"] = court_filter(courts)
    if collapse:
        body["collapse"] = {"field": "dupe_cluster"}
    return body


def _hit(row: object, rank: int, *, leg: LegWord = "lexical") -> LegHit:
    if not isinstance(row, dict) or not isinstance(source := row.get("_source"), dict):
        raise TurnFailure(State.STORE_REPLY_INVALID, leg)
    chunk_id, cluster, boilerplate = row.get("_id"), source.get("dupe_cluster"), source.get("boilerplate")
    court, precedential = source.get("court"), source.get("precedential")
    if not (
        isinstance(chunk_id, str) and chunk_id
        and isinstance(cluster, str) and cluster
        and isinstance(boilerplate, bool)
        and isinstance(court, str) and court
        and precedential in PRECEDENTIAL_VALUES
    ):
        raise TurnFailure(State.STORE_REPLY_INVALID, leg)
    return LegHit(chunk_id, rank, cluster, boilerplate, court, precedential)


def parse_reply(reply: object, *, leg: LegWord = "lexical") -> tuple[LegHit, ...]:
    """Decode hits in store order after checking all shards completed."""

    if not isinstance(reply, dict) or reply.get("timed_out") is not False:
        raise TurnFailure(State.STORE_REPLY_INVALID, leg)
    shards = reply.get("_shards")
    failed = shards.get("failed") if isinstance(shards, dict) else None
    if type(failed) is not int or failed != 0:
        raise TurnFailure(State.STORE_REPLY_INVALID, leg)
    hits = reply.get("hits")
    rows = hits.get("hits") if isinstance(hits, dict) else None
    if not isinstance(rows, list):
        raise TurnFailure(State.STORE_REPLY_INVALID, leg)
    return tuple(_hit(row, rank, leg=leg) for rank, row in enumerate(rows, start=1))


async def search(
    client: httpx.AsyncClient, user: str, password_file: Path, index: str, message: str, limit: int, *,
    collapse: bool, courts: Sequence[str] | None = None,
    clock: Callable[[], float] = time.monotonic, timeout: float = LEXICAL_TIMEOUT_SECONDS,
) -> tuple[tuple[LegHit, ...], RequestSpan]:
    """Send one request with the mounted password and return its hits and request span."""

    body = query_body(message, limit, collapse=collapse, courts=courts)
    try:
        password = password_file.read_text(encoding="utf-8").strip()
        if not password:
            raise TurnFailure(State.STORE_UNAVAILABLE, "lexical")
        async with asyncio.timeout(timeout):
            started = clock()
            response = await client.post(f"/{index}/_search", auth=(user, password), json=body)
            span = RequestSpan(started, clock())
    except (TimeoutError, httpx.TimeoutException):
        raise TurnFailure(State.RETRIEVAL_TIMEOUT, "lexical") from None
    except (OSError, UnicodeError, httpx.RequestError):
        raise TurnFailure(State.STORE_UNAVAILABLE, "lexical") from None
    if response.status_code != HTTPStatus.OK:
        raise TurnFailure(State.STORE_UNAVAILABLE, "lexical")
    try:
        reply = response.json()
    except ValueError:
        raise TurnFailure(State.STORE_REPLY_INVALID, "lexical") from None
    return parse_reply(reply), span
