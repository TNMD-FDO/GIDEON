"""Retrieve candidates and stream a judged answer for an admitted research turn."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from http import HTTPStatus
from typing import Any, Final, cast

import anyio
import httpx
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

from gideon.api import judged
from gideon.api.relay import run_with_disconnect
from gideon.api.settings import Settings
from gideon.api.sse import (
    DONE_EVENT,
    EVENT_STREAM_MEDIA_TYPE,
    EventReassembler,
    is_event_stream,
)
from gideon.api.upstream import EngineClient
from gideon.turn import dense, events, lexical, objectleg, objects, passages, prompt
from gideon.turn.admission import admit
from gideon.turn.answer import AnswerMechanics, AnswerStep, stream_state
from gideon.turn.config import RetrievalConfig, config_hash
from gideon.turn.embedder import (
    EmbeddingSpace,
    check_space,
    embed_query,
    listing_repository,
)
from gideon.turn.fusion import FusionResult, PinnedHit, fuse
from gideon.turn.generation import Connect, PinnedGeneration, read_generation
from gideon.turn.generator import GeneratorSampling, request_body, sampling_for
from gideon.turn.provenance import LegRun, TurnTimings, reply_body
from gideon.turn.states import State, TurnFailure

_LOG = logging.getLogger("gideon.turn")
# exempt: a starting bound, corrected by the M1 reading.
_BODY_LIMIT_BYTES: Final = 256 * 1024


@dataclass(frozen=True, slots=True)
class TurnClients:
    """The keyless HTTP clients held for the service lifespan."""

    embedder: httpx.AsyncClient
    dense: httpx.AsyncClient
    lexical: httpx.AsyncClient


async def _cancel_pending(*tasks: asyncio.Task[Any]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _read_body(request: Request) -> bytearray:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _BODY_LIMIT_BYTES:
            raise TurnFailure(State.INVALID_REQUEST)
        body.extend(chunk)
    return body


async def _generator_sampling(engine: EngineClient, served_name: str) -> GeneratorSampling:
    listing = await engine.list_models()
    if listing is None or listing.status_code != HTTPStatus.OK:
        raise TurnFailure(State.GENERATOR_UNAVAILABLE, "generate")
    try:
        # Reject non-JSON numeric constants such as NaN.
        reply = json.loads(listing.content, parse_constant=int)
        repository = listing_repository(reply, served_name)
    except (ValueError, RecursionError, TurnFailure):
        raise TurnFailure(State.GENERATOR_UNAVAILABLE, "generate") from None
    if any(
        not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"].strip()
        for entry in reply["data"]
    ):
        raise TurnFailure(State.GENERATOR_UNAVAILABLE, "generate")
    sampling = sampling_for(repository) if repository is not None else None
    if sampling is None:
        raise TurnFailure(State.GENERATOR_OUTSIDE_RELEASE, "generate")
    return sampling


def _log_failure(failure: TurnFailure, generation: PinnedGeneration | None) -> None:
    _LOG.warning(
        "state=%s word=%s generation=%s", failure.state, failure.word or "-",
        generation.gen_id if generation is not None else "-",
    )


def _internal_failure(error: Exception, generation: PinnedGeneration | None) -> TurnFailure:
    failure = TurnFailure(State.INTERNAL_ERROR)
    _LOG.error(
        "state=%s word=- generation=%s exception=%s", failure.state,
        generation.gen_id if generation is not None else "-", type(error).__name__,
    )
    return failure


async def _read_attempt(
    upstream: httpx.Response, mechanics: AnswerMechanics, send_text: Callable[[tuple[bytes, ...]], Awaitable[None]],
) -> tuple[AnswerStep, State | None]:
    """Send open steps and return the terminal step for upstream cleanup before delivery."""

    reassembler = EventReassembler()
    incoming = upstream.aiter_raw()
    while True:
        try:
            chunk = await anext(incoming)
        except StopAsyncIteration:
            # EOF before a finish is invalid, including an incomplete last event.
            return mechanics.settle(), State.GENERATOR_REPLY_INVALID
        except httpx.HTTPError:
            return mechanics.settle(), State.GENERATOR_UNAVAILABLE
        try:
            payloads = reassembler.feed(chunk)
        except Exception as error:  # noqa: BLE001 - unreadable events use the mechanics' fail-closed path.
            return mechanics.fail_closed(error), None
        for payload in payloads:
            step = mechanics.process(payload)
            if step.outcome == "engine-error":
                return step, State.GENERATOR_UNAVAILABLE
            if step.outcome != "open":
                return step, None
            if payload == DONE_EVENT:
                return step, State.GENERATOR_REPLY_INVALID
            await send_text(step.events)


class TurnEndpoint:
    """Serve retrieval and generation as an ASGI endpoint instance."""

    def __init__(
        self, settings: Settings, *, connect: Connect | None = None, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._read_generation = read_generation if connect is None else partial(read_generation, connect=connect)
        self._clock = clock
        self._space = EmbeddingSpace(
            settings.embed_model, settings.embed_repository, settings.embed_space, settings.embed_dimensions,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        generation: PinnedGeneration | None = None
        work = None
        try:
            started = self._clock()
            turn, config = admit(
                await _read_body(request), request.headers.getlist(self._settings.source_header),
                self._settings.eval_identity,
            )

            async def pin_generation() -> float:
                nonlocal generation
                record_started = self._clock()
                generation = await self._read_generation(self._settings.record)
                return self._clock() - record_started

            case_extractor: objects.CaseExtractor | None = request.app.state.case_extractor

            async def extract_message() -> tuple[tuple[objects.TurnObject, ...], float]:
                extraction_started = self._clock()
                extracted = await objects.extract_objects(turn.message, case_extractor)
                return extracted, self._clock() - extraction_started

            extraction_task = asyncio.create_task(extract_message())
            record_task = asyncio.create_task(pin_generation())
            try:
                (extracted, extraction_seconds), record_seconds = await asyncio.gather(extraction_task, record_task)
            finally:
                await _cancel_pending(extraction_task, record_task)

            assert generation is not None
            clients: TurnClients = request.app.state.turn_clients
            body, fusion = await self._retrieve(
                clients, generation, turn.message, extracted, config, started, record_seconds, extraction_seconds,
            )
            if turn.stop_after == "retrieve":
                response = JSONResponse(body)
            else:
                work = await self._prepare_answer(request, clients, generation, turn.message, fusion, body, started, send)
        except ClientDisconnect:
            return
        except TurnFailure as failure:
            _log_failure(failure, generation)
            response = JSONResponse(failure.to_body(), status_code=failure.status)
        except Exception as error:  # noqa: BLE001 - unexpected failures disclose the exception class alone.
            internal_failure = _internal_failure(error, generation)
            response = JSONResponse(internal_failure.to_body(), status_code=internal_failure.status)
        if work is not None:
            await run_with_disconnect(work, receive)
        else:
            await response(scope, receive, send)

    async def _retrieve(
        self, clients: TurnClients, generation: PinnedGeneration, message: str,
        extracted: tuple[objects.TurnObject, ...], config: RetrievalConfig, started: float, record_seconds: float,
        extraction_seconds: float,
    ) -> tuple[dict[str, object], FusionResult]:
        settings = self._settings
        limit = config["leg_limit"]
        reservation = config["reservations.exact_object"]
        collapse = config["collapse"] == "on"

        lexical_task = asyncio.create_task(lexical.search(
            clients.lexical, settings.opensearch_user, settings.opensearch_password_file,
            generation.stores.lexical_index, message, limit, collapse=collapse, clock=self._clock,
        ))
        objects_task = asyncio.create_task(objectleg.search(
            clients.lexical, settings.opensearch_user, settings.opensearch_password_file,
            generation.stores.lexical_index, message, extracted, reservation, collapse=collapse, clock=self._clock,
        ))
        store_tasks: list[asyncio.Task[Any]] = [lexical_task, objects_task]
        try:
            # The embedder and the space check are awaited first, so their refusal wins over a store's.
            embedding_started = self._clock()
            query = await embed_query(clients.embedder, settings.embed_key_file, self._space, message)
            embedding_seconds = self._clock() - embedding_started
            reason = check_space(self._space, generation.embedding_space, query)
            if reason is not None:
                raise TurnFailure(State.EMBEDDER_OUTSIDE_SPACE, reason)

            dense_task = asyncio.create_task(dense.search(
                clients.dense, settings.qdrant_key_file, generation.stores.dense_collection,
                query.vector, limit, collapse=collapse, clock=self._clock,
            ))
            store_tasks.append(dense_task)
            (dense_hits, dense_span), (lexical_hits, lexical_span), object_leg = await asyncio.gather(
                dense_task, lexical_task, objects_task,
            )
        finally:
            await _cancel_pending(*store_tasks)

        fusion_started = self._clock()
        # A leg two objects share is seated once, by the first object that names it.
        pins = tuple(
            PinnedHit(hit, obj.index)
            for obj in extracted
            if object_leg.owners.get(obj.index) == obj.index
            for result in object_leg.by_object[obj.index]
            for hit in result.hits
        )
        fusion = fuse(
            {"dense": dense_hits, "lexical": lexical_hits}, rrf_k=config["rrf.k"],
            boilerplate_weight=config["boilerplate_weight"], collapse=collapse, pool_cap=config["pool_cap"],
            pins=pins, reservation=reservation,
        )
        finished = self._clock()
        body = reply_body(
            generation, fusion, plan_mode=config["query_plan"],
            legs={
                "dense": LegRun("qdrant", limit, len(dense_hits)),
                "lexical": LegRun("opensearch", limit, len(lexical_hits)),
            },
            objects=extracted, object_leg=object_leg,
            retrieval_config_hash=config_hash(config),
            timings=TurnTimings(
                record=record_seconds, extraction=extraction_seconds, embedding=embedding_seconds,
                dense=dense_span, lexical=lexical_span, objects=object_leg.span,
                fusion=finished - fusion_started, total=finished - started,
            ),
        )
        return body, fusion

    async def _prepare_answer(
        self, request: Request, clients: TurnClients, generation: PinnedGeneration, question: str,
        fusion: FusionResult, retrieval: dict[str, object], started: float, send: Send,
    ) -> Callable[[], Awaitable[None]]:
        settings = self._settings
        engine: EngineClient = request.app.state.engine
        entries = passages.cut(fusion)

        async def fetch_passages() -> tuple[tuple[passages.Passage, ...], float]:
            fetching = self._clock()
            selected = await passages.fetch(
                clients.lexical, settings.opensearch_user, settings.opensearch_password_file,
                generation.stores.lexical_index, entries,
            )
            return selected, self._clock() - fetching

        fetching = asyncio.create_task(fetch_passages())
        listing = asyncio.create_task(_generator_sampling(engine, settings.engine_model))
        try:
            (selected, passages_seconds), sampling = await asyncio.gather(fetching, listing)
        finally:
            for task in (fetching, listing):
                if not task.done():
                    task.cancel()
            await asyncio.gather(fetching, listing, return_exceptions=True)

        messages = prompt.messages(question, [(passage.caption, passage.text) for passage in selected])
        source = judged.source_for_header(request.headers.getlist(settings.source_header), settings.eval_identity)
        chat_id = judged.id_for_header(request.headers.getlist(settings.chat_header))
        timings = dict(cast(dict[str, float], retrieval["timings_ms"]))
        timings["passages"] = passages_seconds * 1000

        def make_attempt(thinking_off: bool) -> tuple[bytes, AnswerMechanics]:
            body = request_body(settings.engine_model, messages, sampling, thinking_off=thinking_off)
            mechanics = AnswerMechanics(stream_state(question, source, chat_id))
            return json.dumps(body, separators=(",", ":")).encode(), mechanics

        async def work() -> None:
            async def send_message(message: Message) -> None:
                try:
                    await send(message)
                except OSError:
                    raise ClientDisconnect() from None

            try:
                await self._generate(engine, generation, make_attempt, selected, retrieval, timings, started, send_message)
            except ClientDisconnect:
                return

        return work

    async def _generate(
        self, engine: EngineClient, generation: PinnedGeneration, make_attempt: Callable[[bool], tuple[bytes, AnswerMechanics]],
        selected: tuple[passages.Passage, ...], retrieval: dict[str, object], timings: dict[str, float], started: float,
        send: Send,
    ) -> None:
        first_text_ms: float | None = None

        async def send_event(event: bytes) -> None:
            await send({"type": "http.response.body", "body": event, "more_body": True})

        async def send_text(deltas: tuple[bytes, ...]) -> None:
            nonlocal first_text_ms
            for delta in deltas:
                await send_event(delta)
                if first_text_ms is None:
                    first_text_ms = (self._clock() - started) * 1000

        await send({
            "type": "http.response.start", "status": HTTPStatus.OK,
            "headers": [(b"content-type", EVENT_STREAM_MEDIA_TYPE.encode())],
        })
        try:
            for number, passage in enumerate(selected, start=1):
                await send_event(events.passage(number, passage.chunk_id, passage.pool_rank))
            await send_event(events.phase("retrieve", retrieval))
            await send_event(events.phase("generate", 1))
            generating = self._clock()

            for attempt in (1, 2):
                body, mechanics = make_attempt(attempt == 2)
                try:
                    upstream = await engine.completion(body, "application/json")
                except httpx.HTTPError:
                    raise TurnFailure(State.GENERATOR_UNAVAILABLE, "generate") from None
                if upstream is None:
                    raise TurnFailure(State.GENERATOR_UNAVAILABLE, "generate")
                try:
                    if upstream.status_code != HTTPStatus.OK:
                        raise TurnFailure(State.GENERATOR_UNAVAILABLE, "generate")
                    if not is_event_stream(upstream.headers.get("content-type")):
                        raise TurnFailure(State.GENERATOR_REPLY_INVALID, "generate")
                    step, failure_state = await _read_attempt(upstream, mechanics, send_text)
                finally:
                    with anyio.CancelScope(shield=True):
                        await upstream.aclose()

                if mechanics.failure is not None:
                    _LOG.warning("judged stream failed: %s", mechanics.failure)
                await send_text(step.events)
                if step.outcome in ("finished", "tripped"):
                    finished = self._clock()
                    terminal = step.answer_event({
                        **timings, "first_text": first_text_ms,
                        "generation": (finished - generating) * 1000, "total": (finished - started) * 1000,
                    })
                    assert terminal is not None
                    await send_event(terminal)
                    break
                if failure_state is not None:
                    raise TurnFailure(failure_state, "generate")
                if attempt == 1 and step.outcome == "empty" and step.finish_reason == "length":
                    await send_event(events.phase("generate", 2))
                    continue
                raise TurnFailure(State.GENERATION_EMPTY, "generate")
        except ClientDisconnect:
            raise
        except TurnFailure as failure:
            _log_failure(failure, generation)
            await send_event(events.failure(failure))
        except Exception as error:  # noqa: BLE001 - the stream discloses the exception class alone in the log.
            await send_event(events.failure(_internal_failure(error, generation)))
        await send({"type": "http.response.body", "body": b"", "more_body": False})
