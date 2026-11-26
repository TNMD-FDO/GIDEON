"""Research requests, strict retrieval replies, and the eval-only probe."""

import ast
import json
import subprocess
import unittest
from collections.abc import Mapping
from http import HTTPStatus
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from urllib.parse import urlsplit

from test_evaluation_baselines import assert_import_boundary
from test_turns_door import FakeHost

from gideon.evaluation import baselines, research_door
from gideon.evaluation.turns import doorclient
from gideon.host import report, stack
from gideon.host.render.api import (
    API_SECRET_NAME,
    API_SERVICE_NAME,
    API_SOURCE_HEADER,
    api_base_url,
)
from gideon.host.render.owui import EVAL_IDENTITY
from gideon.turn.config import RETRIEVAL_CONFIG
from gideon.turn.states import State, TurnFailure

FIXTURES = Path(__file__).with_name("fixtures") / "eval" / "turn-pool"
RENDERED = Path("/fictitious/research-rendered")
MESSAGE = "Fictitious confidential message for a retrieval turn"


def turn_pool_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def turn_pool_reply(overrides: Mapping[str, object] | None = None) -> dict[str, Any]:
    """Use the recorded ranking in a reply for the current retrieval config."""

    recorded = turn_pool_fixture("reply.json")
    digest = baselines.expected_config_hash(overrides)
    assert isinstance(digest, str)
    return {
        **recorded,
        "retrieval_config_hash": digest,
        "pool": [{**entry, "pinned": False, "object": None} for entry in recorded["pool"]],
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

    def test_send_uses_the_eval_identity_and_includes_only_supplied_overrides(self) -> None:
        options: tuple[Mapping[str, object] | None, ...] = (None, {}, baselines.BASELINES["off"])
        for overrides in options:
            fixture = turn_pool_reply(overrides)
            with self.subTest(overrides=overrides), patch.object(self.host, "run", return_value=envelope(fixture, HTTPStatus.OK)) as run:
                result = research_door.send(self.host, RENDERED, MESSAGE, overrides)
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
                self.assertEqual(request["body"], expected)
                self.assertNotIn(MESSAGE, repr(argv))
                self.assertNotIn(MESSAGE, repr(result))
                self.assertIsInstance(result, research_door.Reply)

    def test_recorded_reply_uses_pool_rank_order_and_allows_an_empty_pool(self) -> None:
        fixture = turn_pool_fixture("reply.json")
        by_rank = sorted(fixture["pool"], key=lambda entry: entry["pool_rank"])
        expected = research_door.Reply(
            fixture["generation"]["gen_id"], tuple(fixture["generation"]["lockfiles"]),
            fixture["retrieval_config_hash"], tuple(entry["chunk_id"] for entry in by_rank),
            fixture["timings_ms"]["total"],
        )
        self.assertEqual(self.send({**fixture, "pool": list(reversed(by_rank))}), expected)
        reply = self.send({**fixture, "pool": []})
        assert isinstance(reply, research_door.Reply)
        self.assertEqual(reply.chunk_ids, ())

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
            "duplicate rank": {"pool": [first, {**second, "pool_rank": first["pool_rank"]}, *tail]},
            "boolean rank": {"pool": [{**first, "pool_rank": True}, second, *tail]},
            "rank gap": {"pool": [second, *tail]},
            "missing total": {"timings_ms": {}},
            "boolean total": {"timings_ms": {"total": True}},
            "negative total": {"timings_ms": {"total": -1}},
            "infinite total": {"timings_ms": {"total": float("inf")}},
        }
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

    def test_imports_stay_within_the_door_turn_states_and_host_seams(self) -> None:
        assert_import_boundary(self, research_door, {
            "gideon.evaluation.turns", "gideon.evaluation.turns.door", "gideon.turn.states", "gideon.host",
            "gideon.host.render.api", "gideon.host.render.engine", "gideon.host.render.worker",
            "gideon.host.report", "gideon.host.stack", "gideon.host.staging", "gideon.host.sysio",
        })
