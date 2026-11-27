"""Exercise the rerank client, queue, and in-process turn route."""

import asyncio
import json
import tempfile
import time
import unittest
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

import httpx
from test_turn_endpoint import RETRIEVE, TurnEndpointFixture
from test_turn_legs import turn_fixture

from gideon.extraction import ExactObject
from gideon.turn import endpoint, rerank
from gideon.turn.config import RETRIEVAL_CONFIG
from gideon.turn.fusion import PoolEntry
from gideon.turn.instruction import RETRIEVAL_INSTRUCTION
from gideon.turn.passages import Passage
from gideon.turn.states import State, TurnFailure
from gideon.turn.turnlog import record_turn

KEY = "fictitious-rerank-key"
QUERY = "Fictitious Harbor Court query."
RERANK_TIMINGS = ("rerank_passages", "rerank_queue", "rerank")


def secret(root: Path) -> Path:
    path = root / "reranker"
    path.write_text(KEY + "\n", encoding="utf-8")
    return path


class TurnRerank(TurnEndpointFixture):
    def setUp(self) -> None:
        super().setUp()
        # These routes count exact selections; RerankSelection covers the reserved seats.
        controlling = turn_fixture("controlling-grouped.json")
        controlling["result"]["groups"] = []
        self.upstream_replies["controlling"] = controlling

    def test_eval_retrieve_ranks_by_indexed_scores_and_can_switch_rerank_off(self) -> None:
        citation = "444 U. S. 987"  # Fictitious reporter cite.
        message = f"Find the fictitious case at {citation}."
        start = message.index(citation)
        exact = ExactObject("case_cite", start, start + len(citation), citation)
        self.case_extractor = Mock(return_value=((exact, "444 U.S. 987"),))
        objects = turn_fixture("objects-msearch.json")
        self.upstream_replies[self.msearch_path] = objects
        pinned = [hit["_id"] for response in objects["responses"] for hit in response["hits"]["hits"]]
        key_file = secret(self.settings.embed_key_file.parent)
        settings = replace(self.settings, rerank_url="http://fixture-reranker", rerank_key_file=key_file)
        requests: list[httpx.Request] = []
        results: list[dict[str, object]] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={
                "id": "fixture-rerank-reply", "model": json.loads(request.content)["model"],
                "results": results, "usage": {"total_tokens": 23},
            })

        self.rerank_transport = httpx.MockTransport(respond)
        depth = 2
        payload = {**RETRIEVE, "message": message, "overrides": {"stage2_depth": depth}}
        off = self.request(settings=settings, payload={
            **payload, "overrides": {"stage2_depth": depth, "rerank.stage2": "off"},
        })
        self.assertEqual(off.status_code, 200, off.text)
        self.assertEqual(requests, [])
        self.assertNotIn(self.mget_path, self.requests)
        off_body = off.json()
        fused = off_body["pool"]
        self.assertEqual(off_body["provenance"]["rerank"], {
            "stage1": RETRIEVAL_CONFIG["rerank.stage1"], "stage2": "off", "depth": depth,
            "scored": 0, "pinned_scored": 0, "reserved_scored": 0,
        })
        self.assertEqual([off_body["timings_ms"][stage] for stage in RERANK_TIMINGS], [0.0] * len(RERANK_TIMINGS))
        fused_ids = [entry["chunk_id"] for entry in fused]
        self.assertEqual(fused_ids[:len(pinned)], pinned)
        self.assertEqual([entry["rank"] for entry in fused], list(range(1, len(fused) + 1)))
        for entry in fused:
            self.assertIsNone(entry["stage2_score"])
            self.assertEqual(entry["rank"], entry["pool_rank"])

        unpinned = fused_ids[len(pinned):]
        selected = [*pinned, *unpinned[:depth]]
        scores = dict(zip(selected, (0.0, 1.0, 0.5, 0.25, 0.75), strict=True))
        texts = {
            chunk_id: f"Fictitious passage {'very ' * length}long."
            for length, chunk_id in enumerate(reversed(selected), start=1)
        }
        self.upstream_replies[self.mget_path] = {
            "docs": [
                {**self.passage_doc, "_id": chunk_id, "_source": {"text": text}}
                for chunk_id, text in texts.items()
            ],
        }
        indexed = sorted(enumerate(texts.items()), key=lambda item: scores[item[1][0]], reverse=True)
        results = [
            {"document": {"text": text}, "index": index, "relevance_score": scores[chunk_id]}
            for index, (chunk_id, text) in indexed
        ]
        self.assertNotEqual([result["index"] for result in results], list(range(len(results))))

        on = self.request(settings=settings, payload=payload, private_values=(message, KEY))

        self.assertEqual(on.status_code, 200, on.text)
        request, = requests
        self.assertEqual((request.method, request.url.path), ("POST", "/v1/rerank"))
        self.assertEqual(request.headers["Authorization"], f"Bearer {KEY}")
        sent = json.loads(request.content)
        self.assertEqual(set(sent), {"model", "query", "documents", "instruction"})
        self.assertEqual(sent["query"], message)
        self.assertEqual(sent["instruction"], RETRIEVAL_INSTRUCTION)
        self.assertEqual(sent["documents"], list(texts.values()))
        read = json.loads(self.requests[self.mget_path].content)
        self.assertEqual([doc["_id"] for doc in read["docs"]], selected)

        expected_order = [*pinned, *reversed(unpinned[:depth]), *unpinned[depth:]]
        by_id = {entry["chunk_id"]: entry for entry in fused}
        body = on.json()
        self.assertEqual(body["pool"], [
            {**by_id[chunk_id], "stage2_score": scores.get(chunk_id), "rank": rank}
            for rank, chunk_id in enumerate(expected_order, start=1)
        ])
        self.assertEqual(body["provenance"]["rerank"], {
            "stage1": RETRIEVAL_CONFIG["rerank.stage1"], "stage2": RETRIEVAL_CONFIG["rerank.stage2"], "depth": depth,
            "scored": len(selected), "pinned_scored": len(pinned), "reserved_scored": 0,
        })
        timings = body["timings_ms"]
        rerank_ms = [timings[stage] for stage in RERANK_TIMINGS]
        self.assertEqual(rerank_ms, [1000.0] * len(RERANK_TIMINGS))
        self.assertEqual(timings["retrieval"], off_body["timings_ms"]["retrieval"])
        self.assertGreaterEqual(timings["total"], off_body["timings_ms"]["total"] + sum(rerank_ms))
        row = self.turn_row()
        self.assertEqual(json.loads(row["pool"]), body["pool"])
        self.assertEqual(json.loads(row["timings_ms"]), timings)
        self.assertEqual(json.loads(row["provenance"])["rerank"], body["provenance"]["rerank"])

    def test_empty_pool_has_zero_rerank_timings_and_sends_no_request(self) -> None:
        settings = replace(
            self.settings, rerank_url="http://fixture-reranker", rerank_key_file=secret(self.settings.embed_key_file.parent),
        )
        dense = turn_fixture("dense-grouped.json")
        lexical = turn_fixture("lexical-collapsed.json")
        dense["result"]["groups"] = []
        lexical["hits"].update({"hits": [], "total": {"value": 0, "relation": "eq"}, "max_score": None})
        self.upstream_replies.update({self.dense_path: dense, self.lexical_path: lexical})
        respond = Mock()
        self.rerank_transport = httpx.MockTransport(respond)

        response = self.request(settings=settings)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["pool"], [])
        self.assertEqual([response.json()["timings_ms"][stage] for stage in RERANK_TIMINGS], [0.0] * len(RERANK_TIMINGS))
        self.assertNotIn(self.mget_path, self.requests)
        respond.assert_not_called()

    def test_passage_text_is_absent_from_turn_outputs_for_valid_and_invalid_rerank_replies(self) -> None:
        sentinel = "Fictitious passage sentinel Violet Harbor Gate"
        self.passage_doc = {**self.passage_doc, "_source": {**self.passage_doc["_source"], "text": sentinel}}
        settings = replace(
            self.settings, rerank_url="http://fixture-reranker", rerank_key_file=secret(self.settings.embed_key_file.parent),
        )
        payload = {**RETRIEVE, "overrides": {"stage2_depth": 1}}
        fixture = turn_fixture("rerank.json")
        first = next(item for item in fixture["results"] if item["index"] == 0)
        result = {**first, "document": {**first["document"], "text": sentinel}}
        score = rerank.score

        async def observed_score(
            client: httpx.AsyncClient, key_file: Path | None, message: str, passages: Sequence[Passage],
        ) -> dict[str, float]:
            self.assertEqual([passage.text for passage in passages], [sentinel])
            self.assertNotIn(sentinel, repr(passages))
            try:
                scores = await score(client, key_file, message, passages)
            except TurnFailure as failure:
                self.assertNotIn(sentinel, repr(failure))
                raise
            self.assertNotIn(sentinel, repr(scores))
            return scores

        for valid, result_item in ((True, result), (False, {**result, "relevance_score": sentinel})):
            def respond(request: httpx.Request, item: dict[str, object] = result_item) -> httpx.Response:
                self.assertEqual(json.loads(request.content)["documents"], [sentinel])
                return httpx.Response(200, json={**fixture, "results": [item]})

            self.rerank_transport = httpx.MockTransport(respond)
            with (
                self.subTest(valid=valid), patch.object(rerank, "score", side_effect=observed_score) as scoring,
                patch.object(endpoint, "record_turn", wraps=record_turn) as recording,
            ):
                response = self.request(settings=settings, payload=payload, private_values=(sentinel,))
                scoring.assert_awaited_once()
                recording.assert_called_once()
                self.assertNotIn(sentinel, repr(recording.call_args.args[0]))
                if valid:
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(self.turn_row()["outcome"], "reply")
                else:
                    self.assert_refused(response, 502, "reranker_reply_invalid", "rerank", pinned=True)
                    row = self.assert_failed_row("reranker_reply_invalid", "rerank")
                    timings = json.loads(row["timings_ms"])
                    self.assertEqual([timings[stage] for stage in RERANK_TIMINGS], [1000.0] * len(RERANK_TIMINGS))
                    self.assertIsNone(row["pool"])

    def test_route_reads_passages_before_queueing_and_runs_waiter_after_release(self) -> None:
        key_file = secret(self.settings.embed_key_file.parent)
        settings = replace(self.settings, rerank_url="http://fixture-reranker", rerank_key_file=key_file)
        fixture = turn_fixture("rerank.json")
        seats = cast(int, RETRIEVAL_CONFIG["rerank.semaphore"])
        payload = {**RETRIEVE, "overrides": {"stage2_depth": len(fixture["results"]), "rerank.timeout_s": 1}}
        upstream = self.upstream

        async def run() -> None:
            reads: asyncio.Queue[None] = asyncio.Queue()
            calls: asyncio.Queue[asyncio.Event] = asyncio.Queue()

            def stores(request: httpx.Request) -> httpx.Response:
                self.requests.pop(self.store_key(request), None)
                reply = upstream(request)
                if request.url.path == self.mget_path:
                    reads.put_nowait(None)
                return reply

            async def respond(_: httpx.Request) -> httpx.Response:
                release = asyncio.Event()
                calls.put_nowait(release)
                await release.wait()
                return httpx.Response(200, json=fixture)

            self.rerank_transport = httpx.MockTransport(respond)
            with patch.object(self, "upstream", side_effect=stores):
                app = self.app(settings)
            async with (
                asyncio.timeout(5), app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture-api") as client,
                asyncio.TaskGroup() as tasks,
            ):
                gate: rerank.Gate = app.state.rerank_gate
                headers = self.headers(settings)
                holders = [tasks.create_task(client.post("/turn", json=payload, headers=headers)) for _ in range(seats)]
                releases = [await calls.get() for _ in holders]
                for _ in holders:
                    await reads.get()
                self.assertEqual((gate.in_flight, gate.waiters, gate.timeouts), (seats, 0, 0))

                expired = tasks.create_task(client.post("/turn", json=payload, headers=headers))
                await reads.get()
                self.assertEqual((gate.in_flight, gate.waiters, gate.timeouts), (seats, 1, 0))
                self.assertTrue(calls.empty())
                response = await expired
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), TurnFailure(State.RERANK_QUEUE_TIMEOUT, "rerank").to_body())
                self.assertEqual((gate.in_flight, gate.waiters, gate.timeouts), (seats, 0, 1))
                timings = json.loads(self.assert_failed_row("rerank_queue_timeout", "rerank")["timings_ms"])
                self.assertEqual(timings["rerank_passages"], 1000.0)
                self.assertEqual(timings["rerank_queue"], 1000.0)
                self.assertNotIn("rerank", timings)

                waiter = tasks.create_task(client.post("/turn", json=payload, headers=headers))
                await reads.get()
                self.assertEqual((gate.in_flight, gate.waiters, gate.timeouts), (seats, 1, 1))
                self.assertTrue(calls.empty())
                releases.pop().set()
                waiter_release = await calls.get()
                self.assertEqual((gate.in_flight, gate.waiters, gate.timeouts), (seats, 0, 1))
                for release in (*releases, waiter_release):
                    release.set()
                for turn in (*holders, waiter):
                    reply = await turn
                    self.assertEqual(reply.status_code, 200, reply.text)
                self.assertEqual((gate.in_flight, gate.waiters, gate.timeouts), (0, 0, 1))

        asyncio.run(run())


def pool_entry(chunk_id: str, rank: int, *, pinned: bool = False, controlling_rank: int | None = None) -> PoolEntry:
    return PoolEntry(
        chunk_id, rank, 0.0, (), chunk_id, False, "persuasive", rank=rank,
        pinned=pinned, object=0 if pinned else None, controlling_rank=controlling_rank,
    )


class RerankSelection(unittest.TestCase):
    def test_every_seat_is_scored_beside_the_depth_and_ordered_by_its_score(self) -> None:
        pool = (
            pool_entry("fixture-pin", 1, pinned=True),
            pool_entry("fixture-voted-a", 2),
            pool_entry("fixture-voted-seat", 3, controlling_rank=4),
            pool_entry("fixture-voted-b", 4),
            pool_entry("fixture-voted-c", 5),
            pool_entry("fixture-tail-seat", 6, controlling_rank=1),
        )
        selected = rerank.select(pool, 2)
        self.assertEqual([entry.chunk_id for entry in selected], [
            "fixture-pin", "fixture-voted-a", "fixture-voted-seat", "fixture-voted-b", "fixture-tail-seat",
        ])

        scores = {"fixture-pin": 0.1, "fixture-voted-a": 0.2, "fixture-voted-seat": 0.4,
                  "fixture-voted-b": 0.2, "fixture-tail-seat": 0.9}
        ordered = rerank.order(pool, scores)
        self.assertEqual([(entry.chunk_id, entry.rank) for entry in ordered], [
            ("fixture-pin", 1), ("fixture-tail-seat", 2), ("fixture-voted-seat", 3),
            ("fixture-voted-a", 4), ("fixture-voted-b", 5), ("fixture-voted-c", 6),
        ])
        self.assertEqual([entry.pool_rank for entry in ordered], [1, 6, 3, 2, 4, 5])
        self.assertIsNone(ordered[-1].stage2_score)


class RerankGate(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.seats = cast(int, RETRIEVAL_CONFIG["rerank.semaphore"])
        self.gate = rerank.Gate(self.seats)
        self.releases = [asyncio.Event() for _ in range(self.seats)]
        self.holders = [asyncio.create_task(self.hold(release)) for release in self.releases]
        await asyncio.sleep(0)
        self.assert_counts(self.seats, 0, 0)

    async def asyncTearDown(self) -> None:
        for release in self.releases:
            release.set()
        await asyncio.gather(*self.holders, return_exceptions=True)

    async def hold(self, release: asyncio.Event, *, clock: Callable[[], float] = time.monotonic) -> float:
        async with self.gate.seat(5, clock=clock) as waited:
            await release.wait()
        return waited

    def assert_counts(self, in_flight: int, waiters: int, timeouts: int) -> None:
        self.assertEqual((self.gate.in_flight, self.gate.waiters, self.gate.timeouts), (in_flight, waiters, timeouts))

    async def test_waiters_take_seats_in_arrival_order_and_receive_wait_seconds(self) -> None:
        done = asyncio.Event()
        done.set()
        ticks = (5.0, 5.25)
        first = asyncio.create_task(self.hold(done, clock=iter(ticks).__next__))
        later_release = asyncio.Event()
        later = asyncio.create_task(self.hold(later_release))
        await asyncio.sleep(0)
        self.assert_counts(self.seats, 2, 0)
        self.assertFalse(first.done())
        self.releases[0].set()
        self.assertEqual(await asyncio.wait_for(first, 1), ticks[1] - ticks[0])
        await asyncio.sleep(0)
        self.assert_counts(self.seats, 0, 0)
        later_release.set()
        await later
        self.assert_counts(self.seats - 1, 0, 0)

    async def test_queue_deadline_leaves_each_holder_in_flight(self) -> None:
        with self.assertRaises(TurnFailure) as raised:
            async with self.gate.seat(0.01):
                self.fail("A timed-out waiter took a seat.")
        self.assertEqual((raised.exception.state, raised.exception.word), (State.RERANK_QUEUE_TIMEOUT, "rerank"))
        self.assert_counts(self.seats, 0, 1)
        self.assertTrue(all(not holder.done() for holder in self.holders))

    async def test_the_wait_is_reported_once_however_it_ends(self) -> None:
        waits: list[float] = []
        with self.assertRaises(TurnFailure):
            async with self.gate.seat(0.01, clock=iter((1.0, 16.0)).__next__, on_wait=waits.append):
                self.fail("A timed-out waiter took a seat.")
        self.assertEqual(waits, [15.0])

        waiting = asyncio.create_task(self.enter(clock=iter((2.0, 2.5)).__next__, on_wait=waits.append))
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertEqual(waits, [15.0, 0.5])

        self.releases[0].set()
        seated = asyncio.create_task(self.enter(clock=iter((3.0, 3.25)).__next__, on_wait=waits.append))
        self.assertEqual(await asyncio.wait_for(seated, 1), 0.25)
        self.assertEqual(waits, [15.0, 0.5, 0.25])
        self.assert_counts(self.seats - 1, 0, 1)

    async def enter(self, *, clock: Callable[[], float], on_wait: Callable[[float], None]) -> float:
        async with self.gate.seat(5, clock=clock, on_wait=on_wait) as waited:
            return waited

    async def test_cancelling_waiter_restores_no_seat_and_cancelling_call_restores_one(self) -> None:
        abandoned = asyncio.create_task(self.hold(asyncio.Event()))
        await asyncio.sleep(0)
        self.assert_counts(self.seats, 1, 0)
        abandoned.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await abandoned
        self.assert_counts(self.seats, 0, 0)

        releases = [asyncio.Event(), asyncio.Event()]
        first, second = [asyncio.create_task(self.hold(release)) for release in releases]
        await asyncio.sleep(0)
        self.assert_counts(self.seats, 2, 0)
        self.holders[0].cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.holders[0]
        await asyncio.sleep(0)
        self.assert_counts(self.seats, 1, 0)
        releases[0].set()
        await asyncio.wait_for(first, 1)
        await asyncio.sleep(0)
        self.assert_counts(self.seats, 0, 0)
        releases[1].set()
        await second
        self.assert_counts(self.seats - 1, 0, 0)

    async def test_call_deadline_starts_after_the_wait_and_releases_one_seat(self) -> None:
        key_file = secret(Path(self.enterContext(tempfile.TemporaryDirectory())))
        entered = asyncio.Event()

        async def respond(_: httpx.Request) -> httpx.Response:
            entered.set()
            await asyncio.Future()
            self.fail("The call passed its deadline.")

        async with httpx.AsyncClient(
            base_url="http://fixture-reranker", transport=httpx.MockTransport(respond), timeout=None,
        ) as client:
            async def call() -> None:
                async with self.gate.seat(5):
                    await rerank.score(client, key_file, QUERY, (Passage("fixture-chunk", 1, "", "Fictitious passage."),))

            bound = 0.02
            with patch.object(rerank, "RERANK_TIMEOUT_SECONDS", bound):
                waiting = asyncio.create_task(call())
                await asyncio.sleep(2 * bound)
                self.assert_counts(self.seats, 1, 0)
                self.assertFalse(entered.is_set())
                self.assertFalse(waiting.done())
                self.releases[0].set()
                with self.assertRaises(TurnFailure) as raised:
                    await asyncio.wait_for(waiting, 1)
                self.assertEqual((raised.exception.state, raised.exception.word), (State.RETRIEVAL_TIMEOUT, "rerank"))
                self.assertTrue(entered.is_set())
            self.assert_counts(self.seats - 1, 0, 0)
            async with self.gate.seat(0):
                self.assert_counts(self.seats, 0, 0)
                with self.assertRaises(TurnFailure) as no_extra_seat:
                    async with self.gate.seat(0):
                        self.fail("An expired call released an extra seat.")
                self.assertEqual(no_extra_seat.exception.state, State.RERANK_QUEUE_TIMEOUT)
            self.assert_counts(self.seats - 1, 0, 1)


class RerankClient(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.key_file = secret(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.reply = turn_fixture("rerank.json")
        self.passages = tuple(
            Passage(f"fixture-chunk-{item['index']}", rank, "", item["document"]["text"])
            for rank, item in enumerate(self.reply["results"], start=1)
        )

    def client(self, respond: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url="http://fixture-reranker", transport=httpx.MockTransport(respond), timeout=None)

    async def assert_failure(
        self, respond: Callable[[httpx.Request], httpx.Response], state: State, status: int,
    ) -> None:
        async with self.client(respond) as client:
            with self.assertRaises(TurnFailure) as raised:
                await rerank.score(client, self.key_file, QUERY, self.passages)
        failure = raised.exception
        self.assertEqual((failure.state, failure.word, failure.status), (state, "rerank", status))

    async def test_each_call_reads_the_current_key_and_returns_scores_by_chunk_id(self) -> None:
        respond = Mock(side_effect=lambda _: httpx.Response(200, json=self.reply))
        rotated = "fictitious-rotated-rerank-key"
        expected = {f"fixture-chunk-{item['index']}": item["relevance_score"] for item in self.reply["results"]}
        async with self.client(respond) as client:
            self.assertEqual(await rerank.score(client, self.key_file, QUERY, self.passages), expected)
            self.key_file.write_text(rotated + "\n", encoding="utf-8")
            self.assertEqual(await rerank.score(client, self.key_file, QUERY, self.passages), expected)
        self.assertEqual([call.args[0].headers["Authorization"] for call in respond.call_args_list], [
            f"Bearer {KEY}", f"Bearer {rotated}",
        ])

    async def test_missing_empty_and_unreadable_keys_fail_before_calling(self) -> None:
        respond = Mock()
        self.key_file.unlink()
        await self.assert_failure(respond, State.RERANKER_UNAVAILABLE, 503)
        for contents in (b" \n", b"\xff"):
            with self.subTest(contents=contents):
                self.key_file.write_bytes(contents)
                await self.assert_failure(respond, State.RERANKER_UNAVAILABLE, 503)
        respond.assert_not_called()

    async def test_malformed_200s_refuse_the_entire_reply(self) -> None:
        first, second = self.reply["results"]
        malformed: dict[str, object] = {
            "short list": [first],
            "duplicate index": [first, {**second, "index": first["index"]}],
            "out of range index": [first, {**second, "index": len(self.passages)}],
            "boolean index": [first, {**second, "index": False}],
            "nonobject result": [first, None],
        }
        for value in (True, "0.5", None, -0.1, 1.1, float("nan"), float("inf")):
            malformed[f"score {value!r}"] = [first, {**second, "relevance_score": value}]
        bodies = {
            label: json.dumps({**self.reply, "results": results}) for label, results in malformed.items()
        }
        bodies["missing results"] = "{}"
        bodies["invalid JSON"] = "Fictitious non-JSON reranker response."
        for label, body in bodies.items():
            with self.subTest(label=label):
                await self.assert_failure(
                    Mock(return_value=httpx.Response(200, content=body)), State.RERANKER_REPLY_INVALID, 502,
                )

    async def test_window_error_at_either_level_and_other_statuses(self) -> None:
        window = turn_fixture("rerank-window.json")
        for body in (window, window["error"]):
            await self.assert_failure(Mock(return_value=httpx.Response(400, json=body)), State.PAIR_OUTSIDE_WINDOW, 400)
        for response in (
            httpx.Response(400, text="Fictitious non-JSON reranker error."),
            httpx.Response(400, json={"error": {"message": "Fictitious unrelated reranker error."}}),
            httpx.Response(401, json=window),
            httpx.Response(503, json=window),
            httpx.Response(307, headers={"location": "/v1/rerank"}),
        ):
            with self.subTest(status=response.status_code):
                await self.assert_failure(Mock(return_value=response), State.RERANKER_UNAVAILABLE, 503)

    async def test_transport_failures_are_unavailable(self) -> None:
        for error in (httpx.ConnectError, httpx.ReadError, httpx.ReadTimeout, TimeoutError):
            with self.subTest(error=error):
                await self.assert_failure(Mock(side_effect=error("Fictitious transport error.")), State.RERANKER_UNAVAILABLE, 503)
