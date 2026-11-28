"""Research requests, checked retrieval and full-turn replies, and the eval-only probe."""

import ast
import json
import subprocess
import unittest
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from itertools import product
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from urllib.parse import urlsplit

from test_evaluation_baselines import assert_import_boundary
from test_turns_door import FakeHost

from gideon.evaluation import baselines, research_door
from gideon.evaluation.turns import door, doorclient
from gideon.host import report, stack
from gideon.host.render.api import (
    API_SECRET_NAME,
    API_SERVICE_NAME,
    API_SOURCE_HEADER,
    api_base_url,
)
from gideon.host.render.owui import EVAL_IDENTITY
from gideon.turn import events
from gideon.turn.config import RETRIEVAL_CONFIG
from gideon.turn.finalset import SUPPORT_LEVELS
from gideon.turn.states import State, TurnFailure

FIXTURES = Path(__file__).with_name("fixtures") / "eval" / "turn-pool"
RENDERED = Path("/fictitious/research-rendered")
MESSAGE = "Fictitious confidential message for a retrieval turn"


def turn_pool_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def turn_pool_reply(overrides: Mapping[str, object] | None = None, *, reranker: bool = True) -> dict[str, Any]:
    """Use the recorded ranking in a reply for the current retrieval config."""

    recorded = turn_pool_fixture("reply.json")
    digest = baselines.expected_config_hash(overrides, reranker=reranker)
    assert isinstance(digest, str)
    return {
        **recorded,
        "retrieval_config_hash": digest,
        "support_level": "ok",
        "pool": [
            {**entry, "rank": entry["pool_rank"], "stage2_score": None, "pinned": False, "object": None}
            for entry in recorded["pool"]
        ],
    }


def envelope(body: object, status: HTTPStatus) -> subprocess.CompletedProcess[str]:
    records = (
        {"kind": "head", "status": status, "media_type": "application/json"},
        {"kind": "body", "text": body if isinstance(body, str) else json.dumps(body)},
        {"kind": "elapsed", "seconds": 0.1},
    )
    return subprocess.CompletedProcess(("fixture",), 0, "\n".join(map(json.dumps, records)), "")


class ResearchDoor(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeHost()

    def send(self, body: object, status: HTTPStatus = HTTPStatus.OK) -> research_door.Reply | research_door.Failure:
        with patch.object(self.host, "run", return_value=envelope(body, status)) as run:
            result = research_door.send(self.host, RENDERED, MESSAGE)
        run.assert_called_once()
        return result

    def test_send_uses_the_eval_identity_and_includes_only_supplied_options(self) -> None:
        options: tuple[Mapping[str, object] | None, ...] = (None, {}, baselines.BASELINES["off"])
        name = turn_pool_fixture("reply.json")["generation"]["gen_id"]
        for overrides, generation in product(options, (None, name)):
            fixture = turn_pool_reply(overrides)
            with (
                self.subTest(overrides=overrides, generation=generation),
                patch.object(self.host, "run", return_value=envelope(fixture, HTTPStatus.OK)) as run,
            ):
                result = research_door.send(self.host, RENDERED, MESSAGE, overrides, generation=generation)
                run.assert_called_once()
                argv = run.call_args.args[0]
                target = urlsplit(argv[-2])
                api = urlsplit(api_base_url())
                self.assertEqual((target.hostname, target.port, target.path), (
                    API_SERVICE_NAME, api.port, research_door.TURN_PATH,
                ))
                self.assertIsNotNone(api.port)
                self.assertEqual(float(argv[-1]), research_door.TURN_TIMEOUT_SECONDS)
                request = json.loads(run.call_args.kwargs["input"])
                self.assertEqual(request["headers"][API_SOURCE_HEADER], EVAL_IDENTITY.email)
                expected: dict[str, object] = {"message": MESSAGE, "stop_after": fixture["stage"]}
                if overrides:
                    expected["overrides"] = dict(overrides)
                if generation is not None:
                    expected["generation"] = generation
                self.assertEqual(request["body"], expected)
                self.assertNotIn(MESSAGE, repr(argv))
                self.assertNotIn(MESSAGE, repr(result))
                self.assertIsInstance(result, research_door.Reply)

    def test_reply_uses_reranked_order_and_allows_an_empty_pool(self) -> None:
        fixture = turn_pool_reply()
        fused = sorted(fixture["pool"], key=lambda entry: entry["pool_rank"])
        ranked = [
            {**entry, "rank": rank} for rank, entry in enumerate(reversed(fused), start=1)
        ]
        expected = research_door.Reply(
            fixture["generation"]["gen_id"], tuple(fixture["generation"]["lockfiles"]),
            fixture["retrieval_config_hash"], tuple(entry["chunk_id"] for entry in ranked),
            fixture["timings_ms"]["total"], fixture["support_level"],
        )
        self.assertEqual(self.send({**fixture, "pool": ranked[1:] + ranked[:1]}), expected)
        reply = self.send({**fixture, "pool": []})
        assert isinstance(reply, research_door.Reply)
        self.assertEqual(reply.chunk_ids, ())

    def test_retrieval_reply_requires_a_support_level_from_the_shared_vocabulary(self) -> None:
        fixture = turn_pool_reply()
        for level in SUPPORT_LEVELS:
            with self.subTest(level=level):
                reply = self.send({**fixture, "support_level": level})
                assert isinstance(reply, research_door.Reply)
                self.assertEqual(reply.support_level, level)

        fixture.pop("support_level")
        expected = research_door.Failure("turn-reply-invalid")
        self.assertEqual(self.send(fixture), expected)
        invalid_levels: tuple[object, ...] = (None, False, 3, [], {}, MESSAGE, "THIN")
        for invalid in invalid_levels:
            with self.subTest(level=invalid):
                self.assertEqual(self.send({**fixture, "support_level": invalid}), expected)

    def test_success_rejects_invalid_stage_generation_hash_pool_and_latency(self) -> None:
        fixture = turn_pool_reply()
        first, second, *tail = fixture["pool"]
        generation = fixture["generation"]
        cases: dict[str, dict[str, object]] = {
            "stage": {"stage": "generate"},
            "generation id": {"generation": {**generation, "gen_id": MESSAGE}},
            "no lockfiles": {"generation": {**generation, "lockfiles": []}},
            "bad lockfile": {"generation": {**generation, "lockfiles": [MESSAGE]}},
            "hash": {"retrieval_config_hash": MESSAGE},
            "pool shape": {"pool": {}},
            "pool entry shape": {"pool": [MESSAGE]},
            "chunk id": {"pool": [{**first, "chunk_id": MESSAGE}, second, *tail]},
            "duplicate chunk": {"pool": [first, {**second, "chunk_id": first["chunk_id"]}, *tail]},
            "missing total": {"timings_ms": {}},
            "boolean total": {"timings_ms": {"total": True}},
            "negative total": {"timings_ms": {"total": -1}},
            "infinite total": {"timings_ms": {"total": float("inf")}},
        }
        for key in ("rank", "pool_rank"):
            cases[f"missing {key}"] = {"pool": [{name: value for name, value in first.items() if name != key}, second, *tail]}
            for value in (None, "1", True, 1.0, 0, len(fixture["pool"]) + 1, second[key]):
                cases[f"invalid {key} {value!r}"] = {"pool": [{**first, key: value}, second, *tail]}
        for name, changed in cases.items():
            with self.subTest(name=name):
                failure = self.send({**fixture, **changed})
                self.assertEqual(failure, research_door.Failure("turn-reply-invalid"))
                self.assertNotIn(MESSAGE, repr(failure))
        self.assertEqual(self.send(MESSAGE), research_door.Failure("turn-reply-invalid"))

    def test_http_failure_keeps_only_a_known_state(self) -> None:
        refused = TurnFailure(State.OVERRIDE_INVALID, "overrides")
        body = refused.to_body()
        body["error"]["message"] = MESSAGE
        failure = self.send(body, refused.status)
        self.assertEqual(failure, research_door.Failure("turn-failed", refused.state))
        self.assertNotIn(MESSAGE, repr(failure))
        invalid_bodies: tuple[object, ...] = ({"error": {"code": MESSAGE}}, {"error": {"code": []}}, MESSAGE)
        for body_value in invalid_bodies:
            with self.subTest(body=body_value):
                failure = self.send(body_value, refused.status)
                self.assertEqual(failure, research_door.Failure("turn-failed", "unknown"))
                self.assertNotIn(MESSAGE, repr(failure))

    def test_both_client_and_outer_subprocess_timeouts_are_turn_cuts(self) -> None:
        inner = subprocess.CompletedProcess(
            ("fixture",), doorclient.FAILURE_EXIT_CODE,
            json.dumps({"kind": "failure", "exception": "TimeoutError"}), "",
        )
        outer = subprocess.TimeoutExpired("fixture", research_door.TURN_TIMEOUT_SECONDS, output=MESSAGE)
        with patch.object(self.host, "run", side_effect=(inner, outer)) as run:
            for source in ("client", "subprocess"):
                with self.subTest(source=source):
                    result = research_door.send(self.host, RENDERED, MESSAGE, None)
                    self.assertEqual(result, research_door.Failure("turn-cut"))
                    self.assertNotIn(MESSAGE, repr(result))
        self.assertEqual(run.call_count, 2)

    def test_probe_sends_an_unlisted_override_and_requires_its_refusal(self) -> None:
        self.assertNotIn(research_door.PROBE_KEY, RETRIEVAL_CONFIG)
        refused = TurnFailure(State.OVERRIDE_NOT_ALLOWED, "overrides")
        with patch.object(self.host, "run", return_value=envelope(refused.to_body(), refused.status)) as run:
            self.assertIsNone(research_door.probe(self.host, RENDERED))
        run.assert_called_once()
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(request["headers"][API_SOURCE_HEADER], EVAL_IDENTITY.email)
        self.assertTrue(request["body"].pop("message").strip())
        self.assertEqual(request["body"], {"stop_after": "retrieve", "overrides": {research_door.PROBE_KEY: True}})

    def test_probe_refusals_name_the_fix_for_the_response(self) -> None:
        cases = (
            (HTTPStatus.NOT_FOUND, report.command("upgrade <tag>")),
            (HTTPStatus.UNAUTHORIZED, report.command(f"secrets rotate {API_SECRET_NAME}")),
            (HTTPStatus.FORBIDDEN, report.command("render --diff")),
            (HTTPStatus.BAD_REQUEST, stack.logs_fix(RENDERED, API_SERVICE_NAME)),
            (HTTPStatus.OK, stack.logs_fix(RENDERED, API_SERVICE_NAME)),
        )
        for status, fix in cases:
            with self.subTest(status=status), patch.object(self.host, "run", return_value=envelope(MESSAGE, status)):
                problem = research_door.probe(self.host, RENDERED)
                assert problem is not None
                self.assertIn(fix, problem.fix)
                self.assertNotIn(MESSAGE, repr(problem))

    def test_sibling_probe_refusals_name_the_siblings_re_render_never_production(self) -> None:
        for status in (HTTPStatus.NOT_FOUND, HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            with self.subTest(status=status), patch.object(self.host, "run", return_value=envelope(MESSAGE, status)):
                problem = research_door.probe(self.host, RENDERED, sibling=True)
                assert problem is not None
                self.assertIn(research_door.SIBLING_FIX_COMMAND, problem.fix)
                for production_fix in ("upgrade", "secrets rotate", "apply"):
                    self.assertNotIn(report.command(production_fix), problem.fix)
        with patch.object(self.host, "run", return_value=envelope(MESSAGE, HTTPStatus.OK)):
            problem = research_door.probe(self.host, RENDERED, sibling=True)
        assert problem is not None
        self.assertIn(stack.logs_fix(RENDERED, API_SERVICE_NAME), problem.fix)

    def test_turn_path_holds_to_the_apps_registered_post_route(self) -> None:
        path = Path(__file__).resolve().parents[1] / "gideon" / "api" / "app.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        route = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Route"
            and len(node.args) >= 2 and isinstance(node.args[1], ast.Call)
            and isinstance(node.args[1].func, ast.Name) and node.args[1].func.id == "TurnEndpoint"
        )
        address = cast(ast.Name, route.args[0]).id
        value = next(
            node.value for node in tree.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == address for target in node.targets)
        )
        self.assertEqual(research_door.TURN_PATH, ast.literal_eval(value))
        self.assertIn("POST", ast.literal_eval(next(keyword.value for keyword in route.keywords if keyword.arg == "methods")))

    def test_imports_stay_within_the_door_shared_turn_modules_and_host_seams(self) -> None:
        assert_import_boundary(self, research_door, {
            "gideon.evaluation.turns", "gideon.evaluation.turns.door", "gideon.host",
            "gideon.host.render.api", "gideon.host.render.engine", "gideon.host.render.worker",
            "gideon.host.report", "gideon.host.stack", "gideon.host.staging", "gideon.host.sysio",
            "gideon.turn", "gideon.turn.events", "gideon.turn.finalset", "gideon.turn.states",
        })


def stream_envelope(frames: Sequence[bytes], elapsed: float) -> subprocess.CompletedProcess[str]:
    """Put service-encoded event frames into the client's stdout envelope."""

    records: list[dict[str, object]] = [
        {"kind": "head", "status": HTTPStatus.OK, "media_type": door.EVENT_STREAM_MEDIA_TYPE},
    ]
    for number, frame in enumerate(frames, start=1):
        name, payload, _ = frame.decode("utf-8").splitlines()
        records.append({
            "kind": "event", "name": name.removeprefix("event: "),
            "offset": elapsed * number / (len(frames) + 1),
            "payload": payload.removeprefix("data: "),
        })
    records.extend(({"kind": "end", "done": False}, {"kind": "elapsed", "seconds": elapsed}))
    return subprocess.CompletedProcess(("fixture",), 0, "\n".join(map(json.dumps, records)), "")


class FullResearchTurn(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeHost()
        self.retrieval = turn_pool_reply()
        fused = sorted(self.retrieval["pool"], key=lambda entry: entry["pool_rank"])
        self.ranked_pool = [{**entry, "rank": rank} for rank, entry in enumerate(reversed(fused), start=1)]
        self.selected = (fused[-1], fused[0])
        self.passages = tuple(
            events.passage(number, entry["chunk_id"], entry["pool_rank"])
            for number, entry in enumerate(self.selected, start=1)
        )
        self.retrieval["pool"] = self.ranked_pool[1:] + self.ranked_pool[:1]
        self.retrieve = events.phase("retrieve", self.retrieval)
        self.generate = events.phase("generate", 1)
        self.prefix = (*self.passages, self.retrieve, self.generate)
        self.answer = "Fictitious confidential terminal answer."
        self.delta = "Fictitious confidential streamed delta."
        self.timings = {"total": 1234.5, "generation": 11.0, "first_text": None}
        self.elapsed = 2.5

    def send(
        self, response: subprocess.CompletedProcess[str],
    ) -> research_door.FullReply | research_door.Failure:
        with patch.object(self.host, "run", return_value=response) as run:
            result = research_door.send_full(self.host, RENDERED, MESSAGE)
        run.assert_called_once()
        return result

    def assert_failure(
        self, result: research_door.FullReply | research_door.Failure, expected: research_door.Failure,
    ) -> None:
        self.assertEqual(result, expected)
        for sentinel in (MESSAGE, self.answer, self.delta):
            self.assertNotIn(sentinel, repr(result))

    def test_full_send_includes_only_supplied_options(self) -> None:
        response = stream_envelope(
            (*self.prefix, events.answer(self.answer, "complete", self.timings)), self.elapsed,
        )
        options: tuple[Mapping[str, object] | None, ...] = (None, {}, baselines.BASELINES["off"])
        name = self.retrieval["generation"]["gen_id"]
        for message_id in (None, "fictitious-message-123"):
            for overrides, generation in product(options, (None, name)):
                with (
                    self.subTest(message_id=message_id, overrides=overrides, generation=generation),
                    patch.object(self.host, "run", return_value=response) as run,
                ):
                    result = research_door.send_full(
                        self.host, RENDERED, MESSAGE, overrides, message_id=message_id, generation=generation,
                    )
                    self.assertIsInstance(result, research_door.FullReply)
                    run.assert_called_once()
                    argv = run.call_args.args[0]
                    target = urlsplit(argv[-2])
                    self.assertEqual((target.hostname, target.port, target.path), (
                        API_SERVICE_NAME, urlsplit(api_base_url()).port, research_door.TURN_PATH,
                    ))
                    self.assertEqual(float(argv[-1]), research_door.FULL_TURN_TIMEOUT_SECONDS)
                    request = json.loads(run.call_args.kwargs["input"])
                    self.assertEqual(request["headers"][API_SOURCE_HEADER], EVAL_IDENTITY.email)
                    expected: dict[str, object] = {"message": MESSAGE}
                    if message_id is not None:
                        expected["message_id"] = message_id
                    if overrides:
                        expected["overrides"] = dict(overrides)
                    if generation is not None:
                        expected["generation"] = generation
                    self.assertEqual(request["body"], expected)
                    self.assertNotIn(MESSAGE, repr(argv))

    def test_stream_returns_ranked_pool_ordered_evidence_and_terminal_answer(self) -> None:
        ends: tuple[events.EndWord, ...] = ("complete", "truncated", "tripped")
        for end in ends:
            family = "fictitious-family" if end == "tripped" else None
            for attempts in (1, 2):
                with self.subTest(end=end, attempts=attempts):
                    retry = (events.phase("generate", 2),) if attempts == 2 else ()
                    frames = (
                        *self.prefix, *retry, events.text(self.delta), events.text(self.delta),
                        events.answer(self.answer, end, self.timings, family=family),
                    )
                    reply = self.send(stream_envelope(frames, self.elapsed))
                    assert isinstance(reply, research_door.FullReply)
                    self.assertEqual(reply.retrieval, research_door.Reply(
                        self.retrieval["generation"]["gen_id"],
                        tuple(self.retrieval["generation"]["lockfiles"]),
                        self.retrieval["retrieval_config_hash"],
                        tuple(entry["chunk_id"] for entry in self.ranked_pool),
                        self.retrieval["timings_ms"]["total"],
                        self.retrieval["support_level"],
                    ))
                    self.assertEqual(reply.evidence, tuple(entry["chunk_id"] for entry in self.selected))
                    self.assertEqual(reply.answer, self.answer)
                    self.assertEqual(reply.end, end)
                    self.assertEqual(reply.family, family)
                    self.assertEqual(reply.attempts, attempts)
                    self.assertEqual(reply.text_events, 2)
                    self.assertEqual(reply.timings_ms, self.timings)
                    self.assertEqual(reply.latency_ms, self.timings["total"])
                    self.assertEqual(reply.elapsed, self.elapsed)
                    for sentinel in (MESSAGE, self.answer, self.delta):
                        self.assertNotIn(sentinel, repr(reply))

    def test_abstained_stream_returns_the_notice_with_zero_evidence_and_attempts(self) -> None:
        retrieve = events.phase("retrieve", {**self.retrieval, "support_level": "none"})
        timings = {"total": self.timings["total"]}
        answer = events.answer(self.answer, "abstained", timings)

        reply = self.send(stream_envelope((retrieve, answer), self.elapsed))

        assert isinstance(reply, research_door.FullReply)
        self.assertEqual(reply.retrieval.support_level, "none")
        # The ranked pool is kept for recall; no passage reached the turn's evidence.
        self.assertEqual(reply.retrieval.chunk_ids, tuple(entry["chunk_id"] for entry in self.ranked_pool))
        self.assertEqual(reply.evidence, ())
        self.assertEqual((reply.attempts, reply.text_events), (0, 0))
        self.assertEqual((reply.end, reply.answer, reply.family), ("abstained", self.answer, None))
        self.assertEqual((reply.timings_ms, reply.latency_ms, reply.elapsed), (timings, timings["total"], self.elapsed))
        self.assertNotIn(self.answer, repr(reply))

    def test_malformed_abstention_streams_return_the_invalid_reply_code(self) -> None:
        unsupported = {**self.retrieval, "support_level": "none"}
        retrieve = events.phase("retrieve", unsupported)
        abstained = events.answer(self.answer, "abstained", {"total": self.timings["total"]})
        complete = events.answer(self.answer, "complete", self.timings)
        frames = {
            "passage before retrieve": (self.passages[0], retrieve, abstained),
            "generate phase": (retrieve, self.generate, abstained),
            "text event": (retrieve, events.text(self.delta), abstained),
            "complete at none": (retrieve, self.generate, complete),
            "complete with no attempt": (self.retrieve, complete),
            "abstained at ok": (events.phase("retrieve", {**unsupported, "support_level": "ok"}), abstained),
            "abstained at thin": (events.phase("retrieve", {**unsupported, "support_level": "thin"}), abstained),
        }
        for label, stream in frames.items():
            with self.subTest(label=label):
                self.assert_failure(self.send(stream_envelope(stream, self.elapsed)), research_door.Failure("turn-reply-invalid"))

    def test_malformed_streams_return_only_the_invalid_reply_code(self) -> None:
        terminal = events.answer(self.answer, "complete", self.timings)
        rank = self.selected[0]["pool_rank"]
        chunk_id = self.selected[0]["chunk_id"]
        frames = {
            "no terminal": (*self.prefix, events.text(self.delta)),
            "two terminals": (*self.prefix, terminal, terminal),
            "text after terminal": (*self.prefix, terminal, events.text(self.delta)),
            "number gap": (events.passage(2, chunk_id, rank), self.retrieve, self.generate, terminal),
            "wrong rank": (events.passage(1, chunk_id, 1), self.retrieve, self.generate, terminal),
            "rank outside pool": (
                events.passage(1, chunk_id, len(self.ranked_pool) + 1), self.retrieve, self.generate, terminal,
            ),
            "bad digest": (events.passage(1, self.delta, rank), self.retrieve, self.generate, terminal),
            "passage after retrieve": (self.retrieve, *self.passages, self.generate, terminal),
            "text before generate": (self.retrieve, events.text(self.delta), self.generate, terminal),
            "bad text": (*self.prefix, events.encode("text", {"text": False}), terminal),
            "bad retrieval body": (
                events.phase("retrieve", {**self.retrieval, "retrieval_config_hash": self.delta}),
                self.generate, terminal,
            ),
            "missing retrieve": (*self.passages, self.generate, terminal),
            "repeated retrieve": (*self.passages, self.retrieve, self.retrieve, self.generate, terminal),
            "missing generate": (*self.passages, self.retrieve, terminal),
            "skipped attempt": (self.retrieve, events.phase("generate", 2), terminal),
            "repeated attempt": (*self.prefix, self.generate, terminal),
            "third attempt": (*self.prefix, events.phase("generate", 2), events.phase("generate", 3), terminal),
            "unknown event": (
                *self.prefix, events.encode(cast(events.EventKind, "fictitious"), {"text": self.delta}), terminal,
            ),
            "malformed failure": (*self.prefix, events.encode("failure", {"error": self.delta})),
        }
        expected = research_door.Failure("turn-reply-invalid")
        for label, stream in frames.items():
            with self.subTest(label=label):
                self.assert_failure(self.send(stream_envelope(stream, self.elapsed)), expected)
        self.assert_failure(self.send(envelope(self.answer, HTTPStatus.OK)), expected)

    def test_rejects_malformed_answer_fields(self) -> None:
        body = {"text": self.answer, "end": "complete", "timings_ms": self.timings}
        invalid: tuple[Mapping[str, object], ...] = (
            {"text": False}, {"end": self.delta}, {"family": self.delta}, {"end": "tripped"},
            {"end": "tripped", "family": False}, {"timings_ms": {}}, {"timings_ms": self.delta},
            {"timings_ms": {"total": -1}}, {"timings_ms": {"total": True}},
            {"timings_ms": {"total": 10**400}},
            {"timings_ms": {**self.timings, "generation": self.delta}},
            {"timings_ms": {**self.timings, "generation": True}},
        )
        for changed in invalid:
            with self.subTest(changed=changed):
                terminal = events.encode("answer", {**body, **changed})
                result = self.send(stream_envelope((*self.prefix, terminal), self.elapsed))
                self.assert_failure(result, research_door.Failure("turn-reply-invalid"))

    def test_failure_event_returns_only_the_checked_state(self) -> None:
        refused = TurnFailure(State.GENERATOR_UNAVAILABLE, "generate")
        body = refused.to_body()
        body["error"]["message"] = self.answer
        cases = (
            (refused.state.value, research_door.Failure("turn-failed", refused.state)),
            (self.delta, research_door.Failure("turn-failed", "unknown")),
        )
        # The service's catch-all can fail a turn before its phases are sent.
        prefixes = {
            "after text": (*self.prefix, events.text(self.delta)),
            "after passages": self.passages,
            "first event": (),
        }
        for code, expected in cases:
            for label, prefix in prefixes.items():
                with self.subTest(state=expected.state, at=label):
                    failure = events.encode("failure", {"error": {**body["error"], "code": code}})
                    result = self.send(stream_envelope((*prefix, failure), self.elapsed))
                    self.assert_failure(result, expected)

    def test_pre_stream_refusals_keep_only_known_states(self) -> None:
        for state in State:
            with self.subTest(state=state):
                refused = TurnFailure(state)
                body = refused.to_body()
                body["error"]["message"] = self.answer
                self.assert_failure(
                    self.send(envelope(body, refused.status)), research_door.Failure("turn-failed", state),
                )
        for status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND):
            with self.subTest(status=status):
                self.assert_failure(
                    self.send(envelope({"error": {"code": self.delta}}, status)),
                    research_door.Failure("turn-failed", "unknown"),
                )

    def test_client_errors_and_both_timeouts_use_fixed_failure_codes(self) -> None:
        cases = (
            ("OSError", research_door.Failure("turn-failed")),
            ("TimeoutError", research_door.Failure("turn-cut")),
        )
        for exception, expected in cases:
            with self.subTest(exception=exception):
                inner = subprocess.CompletedProcess(
                    ("fixture",), doorclient.FAILURE_EXIT_CODE,
                    json.dumps({"kind": "failure", "exception": exception}), self.delta,
                )
                self.assert_failure(self.send(inner), expected)

        outer = subprocess.TimeoutExpired("fixture", research_door.FULL_TURN_TIMEOUT_SECONDS, output=self.delta)
        with patch.object(self.host, "run", side_effect=outer) as run:
            result = research_door.send_full(self.host, RENDERED, MESSAGE)
        run.assert_called_once()
        self.assert_failure(result, research_door.Failure("turn-cut"))

    def test_full_turn_bound_exceeds_the_service_completion_read_bound(self) -> None:
        path = Path(__file__).resolve().parents[1] / "gideon" / "api" / "upstream.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        value = next(
            node.value for node in tree.body if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name) and node.target.id == "UPSTREAM_COMPLETION_READ_TIMEOUT_SECONDS"
        )
        assert value is not None
        self.assertGreater(research_door.FULL_TURN_TIMEOUT_SECONDS, ast.literal_eval(value))
