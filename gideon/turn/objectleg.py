"""Search the pinned lexical index for the turn's exact objects."""

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from itertools import islice
from pathlib import Path
from typing import Final

import httpx

from gideon.turn import lexical
from gideon.turn.fusion import LegHit
from gideon.turn.objects import ObjectSearch, TurnObject, object_leg
from gideon.turn.provenance import RequestSpan
from gideon.turn.states import State, TurnFailure

# exempt: a starting bound, corrected by the M1 reading.
OBJECT_LEG_TIMEOUT_SECONDS: Final = 2.0
# exempt: a starting bound on the encoded request, since every search carries the whole message.
OBJECT_REQUEST_MAX_BYTES: Final = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One object search and its hits in store order."""

    search: ObjectSearch
    hits: tuple[LegHit, ...]


@dataclass(frozen=True, slots=True)
class ObjectLegResult:
    """Hits by object and the measured work of the shared request.

    ``owners`` maps each object with a leg to the first object in message order
    whose leg equals it, the one that seats the shared leg's hits.
    """

    by_object: dict[int, tuple[SearchResult, ...]]
    span: RequestSpan | None
    limit: int
    returned: int
    searches: int
    owners: dict[int, int]


def _filter(search: ObjectSearch) -> dict[str, object]:
    clauses: list[dict[str, object]]
    if search.kind == "term":
        clauses = [
            {"term": {search.field: {"value": value, "case_insensitive": True}}}
            for value in search.values
        ]
    else:
        clauses = [{"match_phrase": {search.field: value}} for value in search.values]
    if len(clauses) == 1:
        return clauses[0]
    return {"bool": {"should": clauses}}


def query_body(
    searches: Sequence[ObjectSearch], message: str, reservation: int, *, collapse: bool,
    courts: Sequence[str] | None = None, max_bytes: int = OBJECT_REQUEST_MAX_BYTES,
) -> bytes:
    """Encode the searches as NDJSON for the generation's multi-search endpoint.

    A request past ``max_bytes`` is refused as an invalid message before it grows further.
    """

    court_filters = [] if courts is None else [lexical.court_filter(courts)]
    encoded = bytearray()
    for search in searches:
        body = lexical.query_body(message, reservation, collapse=collapse)
        body["query"] = {"bool": {"filter": [_filter(search), *court_filters], "should": [body["query"]]}}
        body["sort"] = [{"_score": "desc"}, {"doc_id": "asc"}, {"ordinal": "asc"}]
        line = b"{}\n" + json.dumps(body, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(encoded) + len(line) > max_bytes:
            raise TurnFailure(State.INVALID_REQUEST, "message")
        encoded.extend(line)
    return bytes(encoded)


def parse_reply(reply: object, searches: Sequence[ObjectSearch]) -> tuple[SearchResult, ...]:
    """Decode a complete multi-search reply in request order."""

    items = reply.get("responses") if isinstance(reply, dict) else None
    if not isinstance(items, list) or len(items) != len(searches):
        raise TurnFailure(State.STORE_REPLY_INVALID, "objects")

    results: list[SearchResult] = []
    for search, item in zip(searches, items, strict=True):
        if not isinstance(item, dict) or "error" in item:
            raise TurnFailure(State.STORE_REPLY_INVALID, "objects")
        status = item.get("status")
        if type(status) is not int or status != HTTPStatus.OK:
            raise TurnFailure(State.STORE_REPLY_INVALID, "objects")
        results.append(SearchResult(search, lexical.parse_reply(item, leg="objects")))
    return tuple(results)


async def search(
    client: httpx.AsyncClient, user: str, password_file: Path, index: str, message: str,
    objects: Sequence[TurnObject], reservation: int, *, collapse: bool, courts: Sequence[str] | None = None,
    clock: Callable[[], float] = time.monotonic, timeout: float = OBJECT_LEG_TIMEOUT_SECONDS,
) -> ObjectLegResult:
    """Send each distinct leg once and return the ordered searches for each object."""

    legs = {
        obj.index: leg for obj in objects
        if (leg := object_leg(obj.exact, obj.indexed_spelling, message))
    }
    if not legs:
        return ObjectLegResult({}, None, reservation, returned=0, searches=0, owners={})

    first_object: dict[tuple[ObjectSearch, ...], int] = {}
    for position, leg in legs.items():
        first_object.setdefault(leg, position)
    owners = {position: first_object[leg] for position, leg in legs.items()}
    unique_legs = dict.fromkeys(legs.values())
    searches = tuple(search for leg in unique_legs for search in leg)
    body = query_body(searches, message, reservation, collapse=collapse, courts=courts)
    try:
        password = password_file.read_text(encoding="utf-8").strip()
        if not password:
            raise TurnFailure(State.STORE_UNAVAILABLE, "objects")
        async with asyncio.timeout(timeout):
            started = clock()
            response = await client.post(
                f"/{index}/_msearch", auth=(user, password), content=body,
                headers={"Content-Type": "application/x-ndjson"},
            )
            span = RequestSpan(started, clock())
    except (TimeoutError, httpx.TimeoutException):
        raise TurnFailure(State.RETRIEVAL_TIMEOUT, "objects") from None
    except (OSError, UnicodeError, httpx.RequestError):
        raise TurnFailure(State.STORE_UNAVAILABLE, "objects") from None
    if response.status_code != HTTPStatus.OK:
        raise TurnFailure(State.STORE_UNAVAILABLE, "objects")
    try:
        reply = response.json()
    except ValueError:
        raise TurnFailure(State.STORE_REPLY_INVALID, "objects") from None

    results = parse_reply(reply, searches)
    ordered = iter(results)
    by_leg = {leg: tuple(islice(ordered, len(leg))) for leg in unique_legs}
    return ObjectLegResult(
        {index: by_leg[leg] for index, leg in legs.items()}, span, reservation,
        returned=sum(len(result.hits) for result in results), searches=len(results), owners=owners,
    )
