"""Retrieve candidates and stream a judged answer for an admitted research turn."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
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
from gideon.turn import (
    dense,
    events,
    lexical,
    objectleg,
    objects,
    passages,
    prompt,
    rerank,
    scope,
)
from gideon.turn.admission import admit
from gideon.turn.answer import AnswerMechanics, AnswerStep, stream_state
from gideon.turn.config import RetrievalConfig, config_hash
from gideon.turn.embedder import (
    EmbeddingSpace,
    check_space,
    embed_query,
    listing_repository,
)
from gideon.turn.fusion import FusionResult, LegHit, PinnedHit, fuse
from gideon.turn.generation import (
    TURN_COLLECTION,
    Connect,
    PinnedGeneration,
    read_generation,
)
from gideon.turn.generator import GeneratorSampling, request_body, sampling_for
from gideon.turn.provenance import (
    ControllingLegRun,
    LegRun,
    RequestSpan,
    TurnTimings,
    reply_body,
)
from gideon.turn.scope import LockfileOutsideRelease
from gideon.turn.states import LegWord, State, TurnFailure
from gideon.turn.tier import Jurisdiction, authority_tier
from gideon.turn.turnlog import AttemptCounters, TurnLog, record_turn

_LOG = logging.getLogger("gideon.turn")
# exempt: a starting bound, corrected by the M1 reading.
_BODY_LIMIT_BYTES: Final = 256 * 1024


@dataclass(frozen=True, slots=True)
class TurnClients:
    """The keyless HTTP clients held for the service lifespan."""

    embedder: httpx.AsyncClient
    dense: httpx.AsyncClient
    lexical: httpx.AsyncClient
    reranker: httpx.AsyncClient | None = None


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


async def _generator_sampling(engine: EngineClient, served_name: str) -> tuple[GeneratorSampling, str]:
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
    if repository is not None and (sampling := sampling_for(repository)) is not None:
        return sampling, repository
    raise TurnFailure(State.GENERATOR_OUTSIDE_RELEASE, "generate")


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
        self._jurisdiction = Jurisdiction(
            settings.courts_scotus, settings.courts_circuit, settings.courts_districts, settings.courts_states,
        )
        try:
            self._court_file = scope.read_court_file(settings.courts_file)
        except scope.CourtFileError as exc:
            raise scope.CourtFileError(f"{settings.courts_file}: {exc}") from None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        settings = self._settings
        started = self._clock()
        refusal: TurnFailure | None
        try:
            body = await _read_body(request)
            source_values = request.headers.getlist(settings.source_header)
            turn, config = admit(body, source_values, settings.eval_identity, reranker=settings.rerank_url is not None)
        except ClientDisconnect:
            return
        except TurnFailure as error:
            refusal = error
            _log_failure(refusal, None)
        except Exception as error:  # noqa: BLE001 - unexpected failures disclose the exception class alone.
            refusal = _internal_failure(error, None)
        else:
            refusal = None
        if refusal is not None:
            # A request refused at admission ran no turn and writes no row.
            await JSONResponse(refusal.to_body(), status_code=refusal.status)(scope, receive, send)
            return

        turn_log = TurnLog(
            source=judged.source_for_header(source_values, settings.eval_identity),
            chat_id=judged.id_for_header(request.headers.getlist(settings.chat_header)),
            message_id=turn.message_id, stop_after=turn.stop_after, retrieval_config_hash=config_hash(config),
            overrides=turn.overrides, plan_mode=config["query_plan"],
        )

        async def send_message(message: Message) -> None:
            try:
                await send(message)
            except OSError:
                raise ClientDisconnect() from None

        async def work() -> None:
            generation: PinnedGeneration | None = None
            stream = None
            with suppress(ClientDisconnect):
                try:
                    async def pin_generation() -> float:
                        nonlocal generation
                        record_started = self._clock()
                        generation = await self._read_generation(settings.record)
                        record_seconds = self._clock() - record_started
                        turn_log.timings_ms["record"] = record_seconds * 1000
                        turn_log.generation_id = generation.gen_id
                        turn_log.corpus_lockfiles = generation.lockfiles
                        turn_log.embedding_space = generation.embedding_space
                        return record_seconds

                    case_extractor: objects.CaseExtractor | None = request.app.state.case_extractor

                    async def extract_message() -> tuple[tuple[objects.TurnObject, ...], float]:
                        extraction_started = self._clock()
                        extracted = await objects.extract_objects(turn.message, case_extractor)
                        extraction_seconds = self._clock() - extraction_started
                        turn_log.timings_ms["extraction"] = extraction_seconds * 1000
                        return extracted, extraction_seconds

                    extraction_task = asyncio.create_task(extract_message())
                    record_task = asyncio.create_task(pin_generation())
                    try:
                        (extracted, extraction_seconds), record_seconds = await asyncio.gather(extraction_task, record_task)
                    finally:
                        with anyio.CancelScope(shield=True):
                            await _cancel_pending(extraction_task, record_task)

                    assert generation is not None
                    named = self._court_file.match(turn.message, tuple(obj.exact for obj in extracted))
                    try:
                        stated_scope = self._court_file.resolve(named, generation.lockfiles)
                    except LockfileOutsideRelease:
                        raise TurnFailure(State.LOCKFILE_OUTSIDE_RELEASE) from None
                    clients: TurnClients = request.app.state.turn_clients
                    retrieval, fusion = await self._retrieve(
                        clients, generation, turn.message, extracted, config, started, record_seconds,
                        extraction_seconds, turn_log, rerank_gate=request.app.state.rerank_gate,
                        stated_scope=stated_scope,
                    )
                    if turn.stop_after == "retrieve":
                        response = JSONResponse(retrieval)
                    else:
                        stream = await self._prepare_answer(
                            request, clients, generation, turn.message, fusion, retrieval, started, send_message, turn_log,
                        )
                except ClientDisconnect:
                    raise
                except TurnFailure as error:
                    failure = error
                    _log_failure(failure, generation)
                except Exception as error:  # noqa: BLE001 - unexpected failures disclose the exception class alone.
                    failure = _internal_failure(error, generation)
                else:
                    if stream is not None:
                        await stream()
                    else:
                        await response(scope, receive, send_message)
                        turn_log.outcome = "reply"
                    return

                # A known failure stays the row's outcome even if the caller leaves during its send.
                turn_log.failure_state = failure.state
                turn_log.failure_word = failure.word
                turn_log.outcome = "failed"
                await JSONResponse(failure.to_body(), status_code=failure.status)(scope, receive, send_message)

        try:
            await run_with_disconnect(work, receive)
        finally:
            record_turn(turn_log, elapsed_ms=lambda: (self._clock() - started) * 1000)

    async def _retrieve(
        self, clients: TurnClients, generation: PinnedGeneration, message: str,
        extracted: tuple[objects.TurnObject, ...], config: RetrievalConfig, started: float, record_seconds: float,
        extraction_seconds: float, turn_log: TurnLog, *, rerank_gate: rerank.Gate,
        stated_scope: scope.StatedScope | None,
    ) -> tuple[dict[str, object], FusionResult]:
        settings = self._settings
        limit = config["leg_limit"]
        reservation = config["reservations.exact_object"]
        controlling_reservation = config["reservations.controlling"]
        courts = stated_scope.courts if stated_scope is not None else None
        controlling_courts = self._jurisdiction.controlling_courts
        if courts is not None:
            controlling_courts = controlling_courts.intersection(courts)
        collapse = config["collapse"] == "on"

        async def measure_leg(
            leg: LegWord, search: Callable[[], Awaitable[tuple[tuple[LegHit, ...], RequestSpan]]],
        ) -> tuple[tuple[LegHit, ...], RequestSpan]:
            hits, span = await search()
            turn_log.timings_ms[leg] = span.seconds * 1000
            return hits, span

        lexical_task = asyncio.create_task(measure_leg(
            "lexical", lambda: lexical.search(
                clients.lexical, settings.opensearch_user, settings.opensearch_password_file,
                generation.stores.lexical_index, message, limit, collapse=collapse, courts=courts, clock=self._clock,
            ),
        ))
        async def measure_objects() -> objectleg.ObjectLegResult:
            result = await objectleg.search(
                clients.lexical, settings.opensearch_user, settings.opensearch_password_file,
                generation.stores.lexical_index, message, extracted, reservation,
                collapse=collapse, courts=courts, clock=self._clock,
            )
            if result.span is not None:
                turn_log.timings_ms["objects"] = result.span.seconds * 1000
            return result

        objects_task = asyncio.create_task(measure_objects())
        store_tasks: list[asyncio.Task[Any]] = [lexical_task, objects_task]
        try:
            # The embedder and the space check are awaited first, so their refusal wins over a store's.
            embedding_started = self._clock()
            query = await embed_query(clients.embedder, settings.embed_key_file, self._space, message)
            embedding_seconds = self._clock() - embedding_started
            turn_log.timings_ms["embedding"] = embedding_seconds * 1000
            reason = check_space(self._space, generation.embedding_space, query)
            if reason is not None:
                raise TurnFailure(State.EMBEDDER_OUTSIDE_SPACE, reason)

            dense_task = asyncio.create_task(measure_leg(
                "dense", lambda: dense.search(
                    clients.dense, settings.qdrant_key_file, generation.stores.dense_collection,
                    query.vector, limit, collapse=collapse,
                    courts=frozenset(courts) if courts is not None else None, clock=self._clock,
                ),
            ))

            async def measure_controlling() -> tuple[tuple[LegHit, ...], RequestSpan | None]:
                if courts is not None and not controlling_courts:
                    return (), None
                return await measure_leg(
                    "controlling", lambda: dense.search(
                        clients.dense, settings.qdrant_key_file, generation.stores.dense_collection,
                        query.vector, controlling_reservation, collapse=collapse, courts=controlling_courts,
                        leg="controlling", clock=self._clock,
                    ),
                )

            controlling_task = asyncio.create_task(measure_controlling())
            store_tasks.extend((dense_task, controlling_task))
            (dense_hits, dense_span), (lexical_hits, lexical_span), object_leg, (controlling_hits, controlling_span) = (
                await asyncio.gather(dense_task, lexical_task, objects_task, controlling_task)
            )
        finally:
            with anyio.CancelScope(shield=True):
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
            controlling_hits=controlling_hits, controlling_reservation=controlling_reservation,
            tier_for=lambda hit: authority_tier(TURN_COLLECTION, hit.court, hit.precedential, self._jurisdiction),
        )
        fusion_seconds = self._clock() - fusion_started
        turn_log.timings_ms["fusion"] = fusion_seconds * 1000

        ranked = fusion
        rerank_passages = rerank_queue = rerank_seconds = 0.0
        if config["rerank.stage2"] == "on":
            # Admission and the lifespan both use the configured reranker URL.
            reranker = cast(httpx.AsyncClient, clients.reranker)
            selected = rerank.select(fusion.entries, config["stage2_depth"])
            if selected:
                passages_started = self._clock()
                try:
                    documents = await passages.fetch(
                        clients.lexical, settings.opensearch_user, settings.opensearch_password_file,
                        generation.stores.lexical_index, selected, word="rerank",
                    )
                finally:
                    rerank_passages = self._clock() - passages_started
                    turn_log.timings_ms["rerank_passages"] = rerank_passages * 1000

                def record_queue(seconds: float) -> None:
                    turn_log.timings_ms["rerank_queue"] = seconds * 1000

                async with rerank_gate.seat(
                    config["rerank.timeout_s"], clock=self._clock, on_wait=record_queue,
                ) as rerank_queue:
                    call_started = self._clock()
                    try:
                        scores = await rerank.score(reranker, settings.rerank_key_file, message, documents)
                    finally:
                        rerank_seconds = self._clock() - call_started
                        turn_log.timings_ms["rerank"] = rerank_seconds * 1000
                ranked = replace(fusion, entries=rerank.order(fusion.entries, scores))

        finished = self._clock()
        body = reply_body(
            generation, ranked, config=config,
            legs={
                "dense": LegRun("qdrant", limit, len(dense_hits)),
                "lexical": LegRun("opensearch", limit, len(lexical_hits)),
                "controlling": ControllingLegRun(
                    "qdrant", controlling_reservation, len(controlling_hits), tuple(sorted(controlling_courts)),
                ),
            },
            objects=extracted, object_leg=object_leg, stated_scope=stated_scope,
            retrieval_config_hash=turn_log.retrieval_config_hash,
            timings=TurnTimings(
                record=record_seconds, extraction=extraction_seconds, embedding=embedding_seconds,
                dense=dense_span, lexical=lexical_span, objects=object_leg.span, controlling=controlling_span,
                fusion=fusion_seconds, rerank_passages=rerank_passages, rerank_queue=rerank_queue,
                rerank=rerank_seconds, total=finished - started,
            ),
        )
        # A copy: later stages add their timings here, never to the reply's own mapping.
        turn_log.timings_ms = dict(cast(dict[str, float | None], body["timings_ms"]))
        turn_log.provenance = cast(dict[str, object], body["provenance"])
        turn_log.pool = cast(list[dict[str, object]], body["pool"])
        # The tracer's passage cut uses the fused order.
        return body, fusion

    async def _prepare_answer(
        self, request: Request, clients: TurnClients, generation: PinnedGeneration, question: str,
        fusion: FusionResult, retrieval: dict[str, object], started: float, send: Send, turn_log: TurnLog,
    ) -> Callable[[], Awaitable[None]]:
        settings = self._settings
        engine: EngineClient = request.app.state.engine
        entries = passages.cut(fusion)

        async def fetch_passages() -> tuple[passages.Passage, ...]:
            fetching = self._clock()
            selected = await passages.fetch(
                clients.lexical, settings.opensearch_user, settings.opensearch_password_file,
                generation.stores.lexical_index, entries,
            )
            turn_log.timings_ms["passages"] = (self._clock() - fetching) * 1000
            return selected

        fetching = asyncio.create_task(fetch_passages())
        listing = asyncio.create_task(_generator_sampling(engine, settings.engine_model))
        try:
            selected, (sampling, repository) = await asyncio.gather(fetching, listing)
        finally:
            with anyio.CancelScope(shield=True):
                await _cancel_pending(fetching, listing)

        turn_log.evidence = selected
        turn_log.generator_repository = repository
        messages = prompt.messages(question, [(passage.caption, passage.text) for passage in selected])

        def make_attempt(thinking_off: bool) -> tuple[bytes, AnswerMechanics]:
            body = request_body(settings.engine_model, messages, sampling, thinking_off=thinking_off)
            mechanics = AnswerMechanics(stream_state(question, turn_log.source, turn_log.chat_id))
            return json.dumps(body, separators=(",", ":")).encode(), mechanics

        async def work() -> None:
            await self._generate(engine, generation, make_attempt, selected, retrieval, turn_log, started, send)

        return work

    async def _generate(
        self, engine: EngineClient, generation: PinnedGeneration, make_attempt: Callable[[bool], tuple[bytes, AnswerMechanics]],
        selected: tuple[passages.Passage, ...], retrieval: dict[str, object], turn_log: TurnLog, started: float,
        send: Send,
    ) -> None:
        async def send_event(event: bytes) -> None:
            await send({"type": "http.response.body", "body": event, "more_body": True})

        async def send_text(deltas: tuple[bytes, ...]) -> None:
            for delta in deltas:
                await send_event(delta)
                if "first_text" not in turn_log.timings_ms:
                    turn_log.timings_ms["first_text"] = (self._clock() - started) * 1000

        await send({
            "type": "http.response.start", "status": HTTPStatus.OK,
            "headers": [(b"content-type", EVENT_STREAM_MEDIA_TYPE.encode())],
        })
        failure: TurnFailure | None = None
        try:
            for number, passage in enumerate(selected, start=1):
                await send_event(events.passage(number, passage.chunk_id, passage.pool_rank))
            await send_event(events.phase("retrieve", retrieval))
            await send_event(events.phase("generate", 1))
            generating = self._clock()

            for attempt in (1, 2):
                thinking_off = attempt == 2
                body, mechanics = make_attempt(thinking_off)
                finish_reason: str | None = None
                try:
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
                        finish_reason = step.finish_reason
                    finally:
                        with anyio.CancelScope(shield=True):
                            await upstream.aclose()
                finally:
                    usage = mechanics.usage
                    turn_log.engine_counters.append(AttemptCounters(
                        thinking=not thinking_off,
                        finish_reason=finish_reason,
                        prompt_tokens=usage.prompt_tokens if usage is not None else None,
                        completion_tokens=usage.completion_tokens if usage is not None else None,
                        reasoning_tokens=usage.reasoning_tokens if usage is not None else None,
                    ))

                if mechanics.failure is not None:
                    _LOG.warning("judged stream failed: %s", mechanics.failure)
                await send_text(step.events)
                if step.outcome in ("finished", "tripped"):
                    finished = self._clock()
                    generation_ms = (finished - generating) * 1000
                    terminal_timings = {
                        **turn_log.timings_ms, "first_text": turn_log.timings_ms.get("first_text"),
                        "generation": generation_ms, "total": (finished - started) * 1000,
                    }
                    turn_log.timings_ms["generation"] = generation_ms
                    terminal = step.answer_event(terminal_timings)
                    assert terminal is not None and step.end is not None
                    break
                if failure_state is not None:
                    raise TurnFailure(failure_state, "generate")
                if attempt == 1 and step.outcome == "empty" and step.finish_reason == "length":
                    await send_event(events.phase("generate", 2))
                    continue
                raise TurnFailure(State.GENERATION_EMPTY, "generate")
        except ClientDisconnect:
            raise
        except TurnFailure as error:
            failure = error
            _log_failure(failure, generation)
        except Exception as error:  # noqa: BLE001 - the stream discloses the exception class alone in the log.
            failure = _internal_failure(error, generation)
        else:
            await send_event(cast(bytes, terminal))
            turn_log.timings_ms = dict(terminal_timings)
            turn_log.outcome = step.end
            if step.end == "tripped":
                turn_log.trip_family = step.family

        if failure is not None:
            turn_log.failure_state = failure.state
            turn_log.failure_word = failure.word
            turn_log.outcome = "failed"
            await send_event(events.failure(failure))
        await send({"type": "http.response.body", "body": b"", "more_body": False})
