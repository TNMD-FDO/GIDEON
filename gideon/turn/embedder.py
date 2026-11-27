"""Read the served embedder's repository and embed a query in the release space."""

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Final

import httpx

from gideon.turn.instruction import instructed
from gideon.turn.states import ReasonWord, State, TurnFailure

# exempt: a starting bound, corrected by the M1 reading.
EMBED_TIMEOUT_SECONDS: Final = 5.0


@dataclass(frozen=True, slots=True)
class EmbeddingSpace:
    """The model and vector identity expected by the release."""

    served_name: str
    repository: str
    space_id: str
    width: int


@dataclass(frozen=True, slots=True)
class EmbeddedQuery:
    """The repository found in the listing and the query's vector."""

    repository: str | None
    vector: tuple[float, ...]


def check_space(space: EmbeddingSpace, generation_space: str, query: EmbeddedQuery) -> ReasonWord | None:
    """Return the first reason the generation and served embedder cannot be used together."""

    if generation_space != space.space_id:
        return "generation"
    if query.repository is None:
        return "listing"
    if query.repository != space.repository:
        return "repository"
    if len(query.vector) != space.width:
        return "width"
    return None


def listing_repository(reply: object, served_name: str) -> str | None:
    """Find the repository under the sole listing entry for the served name."""

    entries = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(entries, list):
        raise TurnFailure(State.EMBEDDER_UNAVAILABLE)
    matches = [entry for entry in entries if isinstance(entry, dict) and entry.get("id") == served_name]
    if len(matches) != 1:
        return None
    root = matches[0].get("root")
    return root if isinstance(root, str) and root.strip() else None


def parse_embedding(reply: object) -> tuple[float, ...]:
    """Decode exactly one nonempty vector of finite numbers."""

    items = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise TurnFailure(State.EMBEDDER_UNAVAILABLE)
    components = items[0].get("embedding")
    if not isinstance(components, list) or not components:
        raise TurnFailure(State.EMBEDDER_UNAVAILABLE)
    return _finite_vector(components)


def _finite_vector(components: Sequence[object]) -> tuple[float, ...]:
    vector: list[float] = []
    for value in components:
        if type(value) is not int and type(value) is not float:
            raise TurnFailure(State.EMBEDDER_UNAVAILABLE)
        try:
            number = float(value)
        except OverflowError:
            raise TurnFailure(State.EMBEDDER_UNAVAILABLE) from None
        if not math.isfinite(number):
            raise TurnFailure(State.EMBEDDER_UNAVAILABLE)
        vector.append(number)
    return tuple(vector)


def _json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        raise TurnFailure(State.EMBEDDER_UNAVAILABLE) from None


def outside_window(reply: object) -> bool:
    """Read a pooling server's 400 body as the context-window refusal, the embedder's and the reranker's."""

    if not isinstance(reply, dict):
        return False
    error = reply.get("error", reply)
    message = error.get("message") if isinstance(error, dict) else None
    return isinstance(message, str) and "maximum context length" in message.casefold()


async def _list_repository(client: httpx.AsyncClient, headers: dict[str, str], served_name: str) -> str | None:
    response = await client.get("/v1/models", headers=headers)
    if response.status_code != HTTPStatus.OK:
        raise TurnFailure(State.EMBEDDER_UNAVAILABLE)
    return listing_repository(_json(response), served_name)


async def _embed(
    client: httpx.AsyncClient, headers: dict[str, str], space: EmbeddingSpace, message: str,
) -> tuple[float, ...]:
    response = await client.post("/v1/embeddings", headers=headers, json={
        "model": space.served_name,
        "input": [instructed(message)],
        "dimensions": space.width,
    })
    if response.status_code == HTTPStatus.BAD_REQUEST and outside_window(_json(response)):
        raise TurnFailure(State.MESSAGE_OUTSIDE_WINDOW)
    if response.status_code != HTTPStatus.OK:
        raise TurnFailure(State.EMBEDDER_UNAVAILABLE)
    return parse_embedding(_json(response))


async def embed_query(
    client: httpx.AsyncClient, key_file: Path, space: EmbeddingSpace, message: str, *,
    timeout: float = EMBED_TIMEOUT_SECONDS,
) -> EmbeddedQuery:
    """Read the mounted key once and issue the listing and embedding requests together."""

    try:
        key = key_file.read_text(encoding="utf-8").strip()
        if not key:
            raise TurnFailure(State.EMBEDDER_UNAVAILABLE)
        headers = {"Authorization": f"Bearer {key}"}
        listing = asyncio.create_task(_list_repository(client, headers, space.served_name))
        embedding = asyncio.create_task(_embed(client, headers, space, message))
        try:
            async with asyncio.timeout(timeout):
                repository = await listing
                # A listing that shows another model decides the turn, whatever the embedding
                # answers, so its request is cancelled rather than awaited.
                if repository != space.repository:
                    return EmbeddedQuery(repository, ())
                vector = await embedding
        finally:
            for task in (listing, embedding):
                if not task.done():
                    task.cancel()
            await asyncio.gather(listing, embedding, return_exceptions=True)
    except (TimeoutError, httpx.TimeoutException):
        raise TurnFailure(State.RETRIEVAL_TIMEOUT, "embedding") from None
    except (OSError, UnicodeError, httpx.RequestError):
        raise TurnFailure(State.EMBEDDER_UNAVAILABLE) from None
    return EmbeddedQuery(repository, vector)
