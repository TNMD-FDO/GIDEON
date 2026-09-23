"""Service-door client, relay, and harness completion contracts."""

import ast
import contextlib
import errno
import http.server
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from urllib.parse import quote

import yaml  # type: ignore[import-untyped]

from gideon import guardrail
from gideon.evaluation.turns import classify, door, doorclient, session
from gideon.evaluation.turns import run as run_module
from gideon.evaluation.turns.cases import Case
from gideon.host import models, secrets, site
from gideon.host.render.api import (
    API_SECRET_NAME,
    API_USER_EMAIL_HEADER,
    API_USER_NAME_HEADER,
    API_USER_ROLE_HEADER,
)
from gideon.host.render.ci import CI_ROOT, CI_SECRET_NAMES
from gideon.host.render.owui import EVAL_IDENTITY
from gideon.host.report import Problem
from gideon.host.sysio import Command, Host, PathLike
from tools.turns import cli

ROOT = Path(__file__).resolve().parents[1]
SITE_PATH = Path("/etc/gideon/site.yaml")
# The site the fakes serve at SITE_PATH, and so the one an assertion reads: the
# committed example, never the box's own file, which no other tree has.
SITE_SOURCE = ROOT / "config/site.example.yaml"
RENDERED_COMPOSE = Path("/etc/gideon/rendered/compose.yaml")
FIXED_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
KEY_VALUE = "planted-api-key"


def _seed_case(identifier: str) -> tuple[str, str]:
    """Load a seed prompt and answer so the guardrail remains the authority."""

    document = cast(
        dict[str, object],
        yaml.safe_load(
            (ROOT / "eval/seed/guardrails/deadline-trap.yaml").read_text(
                encoding="utf-8"
            )
        ),
    )
    values = document["cases"]
    assert isinstance(values, list)
    retired = {
        value["supersedes"]
        for value in values
        if isinstance(value, dict) and isinstance(value.get("supersedes"), str)
    }
    for value in values:
        if (
            isinstance(value, dict)
            and value.get("id") == identifier
            and identifier not in retired
        ):
            prompt = value.get("prompt")
            answer = value.get("answer")
            if isinstance(prompt, str) and isinstance(answer, str):
                return prompt, answer
    raise AssertionError(f"active seed case {identifier!r} is unavailable")


def _served_name() -> str:
    """Derive the generator's served name from the committed lock and site."""

    loaded_site = site.load_site(SITE_SOURCE)
    assert loaded_site.config is not None, loaded_site.errors
    loaded_models = models.load_models_lock(ROOT / "models.lock")
    assert loaded_models.lock is not None, loaded_models.errors
    profile = models.select_profile(loaded_models.lock, loaded_site.config.hardware_profile)
    assert not isinstance(profile, Problem)
    generator = profile.model("generator")
    assert generator is not None
    return generator.serve.served_name


class _LoopbackServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _LoopbackHandler)
        self.requests: list[dict[str, object]] = []
        self.release = threading.Event()
        self.block_response = False


class _LoopbackHandler(http.server.BaseHTTPRequestHandler):
    server: _LoopbackServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.server.requests.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
        self._reply({"data": [{"id": _served_name()}]})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append(
            {
                "method": "POST",
                "path": self.path,
                "headers": dict(self.headers),
                "body": json.loads(body),
            }
        )
        if self.server.block_response:
            self.server.release.wait(1.0)
            return
        if cast(dict[str, object], json.loads(body)).get("stream"):
            self._stream_reply()
            return
        self._reply({"choices": [{"message": {"content": "whole completion"}}]})

    def _stream_reply(self) -> None:
        payloads = [
            json.dumps({"choices": [{"delta": {"content": "streamed"}}]}),
            json.dumps({"choices": [{"delta": {"content": " completion"}}]}),
        ]
        encoded = "".join(f"data: {payload}\n\n" for payload in payloads)
        encoded += "data: [DONE]\n\n"
        data = encoded.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError):
            self.wfile.write(data)

    def _reply(self, body: object) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError):
            self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def _run_client(
    arguments: list[str], request: Mapping[str, object]
) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        patch.object(sys, "stdin", io.StringIO(json.dumps(request))),
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        code = doorclient.main(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


class ClientRefusals(unittest.TestCase):
    """Key-file refusals happen before any network operation."""

    def test_missing_and_empty_key_files_refuse_without_an_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            empty = Path(directory) / "empty"
            empty.write_text("\r\n", encoding="utf-8")
            for key_path in (missing, empty):
                with self.subTest(key_path=key_path.name):
                    code, stdout, stderr = _run_client(
                        [str(key_path), "http://127.0.0.1:1/v1/models", "2"],
                        {"headers": {}},
                    )
                    self.assertEqual(code, doorclient.KEY_FILE_EXIT_CODE)
                    self.assertEqual(stdout, "")
                    self.assertEqual(stderr.strip(), doorclient.KEY_FILE_REFUSAL)


class ClientOverLoopback(unittest.TestCase):
    """The standard-library client emits only the bounded wire envelope."""

    server: _LoopbackServer
    thread: threading.Thread

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.server = _LoopbackServer()
        except OSError as exc:
            if exc.errno in {errno.EPERM, errno.EACCES}:
                raise unittest.SkipTest(f"loopback sockets unavailable: {exc}") from exc
            raise
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2.0)
        if cls.thread.is_alive():
            raise AssertionError("loopback server did not stop within its bound")

    def setUp(self) -> None:
        self.server.requests.clear()
        self.server.release.clear()
        self.server.block_response = False

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_port}{path}"

    def test_whole_post_carries_key_content_type_headers_and_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "api-key"
            key_path.write_text(f"{KEY_VALUE}\r\n", encoding="utf-8")
            request = {
                "headers": {
                    API_USER_NAME_HEADER: "quoted-name",
                    API_USER_EMAIL_HEADER: "eval@example.invalid",
                    API_USER_ROLE_HEADER: "user",
                },
                "body": {"model": "served", "messages": []},
            }
            code, stdout, stderr = _run_client(
                [str(key_path), self.url("/v1/chat/completions"), "2"], request
            )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        records = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual([record["kind"] for record in records], ["head", "body", "elapsed"])
        self.assertEqual(records[0], {"kind": "head", "status": 200, "media_type": "application/json"})
        self.assertEqual(json.loads(cast(str, records[1]["text"])), {"choices": [{"message": {"content": "whole completion"}}]})
        self.assertIsInstance(records[2]["seconds"], float)

        received = self.server.requests[-1]
        self.assertEqual(received["method"], "POST")
        headers = cast(dict[str, str], received["headers"])
        self.assertEqual(headers["Authorization"], f"Bearer {KEY_VALUE}")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers[API_USER_NAME_HEADER], "quoted-name")
        self.assertEqual(headers[API_USER_EMAIL_HEADER], "eval@example.invalid")
        self.assertEqual(headers[API_USER_ROLE_HEADER], "user")
        self.assertEqual(received["body"], request["body"])

    def test_get_is_keyed_and_has_the_same_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "api-key"
            key_path.write_text(KEY_VALUE, encoding="utf-8")
            code, stdout, stderr = _run_client(
                [str(key_path), self.url("/v1/models"), "2"], {"headers": {}}
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        records = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual([record["kind"] for record in records], ["head", "body", "elapsed"])
        self.assertEqual(self.server.requests[-1]["method"], "GET")
        self.assertEqual(
            cast(dict[str, str], self.server.requests[-1]["headers"])["Authorization"],
            f"Bearer {KEY_VALUE}",
        )

    def test_stream_post_carries_data_offsets_and_end_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "api-key"
            key_path.write_text(KEY_VALUE, encoding="utf-8")
            code, stdout, stderr = _run_client(
                [str(key_path), self.url("/v1/chat/completions"), "2"],
                {"headers": {}, "body": {"stream": True}},
            )
        self.assertEqual(code, 0, stderr)
        records = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(
            [record["kind"] for record in records],
            ["head", "data", "data", "end", "elapsed"],
        )
        self.assertEqual(records[0]["media_type"], "text/event-stream")
        self.assertTrue(records[1]["payload"].startswith('{"choices"'))
        self.assertLessEqual(records[1]["offset"], records[2]["offset"])
        self.assertEqual(records[3], {"kind": "end", "done": True})

    def test_refused_connection_is_named_by_exception_without_request_text(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "api-key"
            key_path.write_text(KEY_VALUE, encoding="utf-8")
            code, stdout, stderr = _run_client(
                [str(key_path), f"http://127.0.0.1:{port}/v1/models", "1"],
                {"headers": {}, "body": {"secret": "request text"}},
            )
        self.assertEqual(code, doorclient.FAILURE_EXIT_CODE)
        self.assertEqual(stderr, "")
        records = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(records, [{"kind": "failure", "exception": records[0]["exception"]}])
        self.assertNotIn(KEY_VALUE, stdout)
        self.assertNotIn("request text", stdout)

    def test_deadline_failure_names_only_its_exception_class(self) -> None:
        self.server.block_response = True
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "api-key"
            key_path.write_text(KEY_VALUE, encoding="utf-8")
            code, stdout, stderr = _run_client(
                [str(key_path), self.url("/v1/chat/completions"), "0.05"],
                {"headers": {}, "body": {"secret": "response text"}},
            )
        self.server.release.set()
        self.assertEqual(code, doorclient.FAILURE_EXIT_CODE)
        self.assertEqual(stderr, "")
        records = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(set(records[-1]), {"kind", "exception"})
        self.assertEqual(records[-1]["kind"], "failure")
        self.assertIsInstance(records[-1]["exception"], str)
        self.assertNotIn("response text", stdout)


class FakeHost:
    """A dict-backed host whose door exec computes a response from stdin."""

    def __init__(
        self,
        *,
        answer: str = "A plain answer.",
        fail_probe: bool = False,
        stream_deltas: Sequence[tuple[str, str]] | None = None,
        stream_end: bool = True,
        stream_error: bool = False,
        ignores_stream: bool = False,
        always_streams: bool = False,
        completion_status: int = 200,
    ) -> None:
        self.files: dict[str, str] = {
            str(SITE_PATH): (ROOT / "config/site.example.yaml").read_text(encoding="utf-8"),
            str(ROOT / "models.lock"): (ROOT / "models.lock").read_text(encoding="utf-8"),
            str(RENDERED_COMPOSE): "rendered",
        }
        self.directories: dict[str, list[str]] = {}
        self.requests: list[dict[str, object]] = []
        self.exec_argv: list[tuple[str, ...]] = []
        self.exec_inputs: list[str] = []
        self.answer = answer
        self.answers: dict[str, str] = {}
        self.fail_probe = fail_probe
        self.stream_deltas = tuple(stream_deltas) if stream_deltas is not None else None
        self.stream_end = stream_end
        self.stream_error = stream_error
        self.ignores_stream = ignores_stream
        self.always_streams = always_streams
        self.completion_status = completion_status
        self.euid = 0

    def run(
        self,
        argv: Command,
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, cwd, env, timeout, passthrough
        command = tuple(str(value) for value in argv)
        if input is None:
            return subprocess.CompletedProcess(command, 0, "", "")
        self.exec_argv.append(command)
        self.exec_inputs.append(input)
        request = cast(dict[str, object], json.loads(input))
        self.requests.append(request)
        body = request.get("body")
        if body is None:
            if self.fail_probe:
                return subprocess.CompletedProcess(command, 7, "", "")
            response: object = {"data": [{"id": _served_name()}]}
        else:
            body_mapping = cast(dict[str, object], body)
            messages = cast(list[dict[str, str]], body_mapping["messages"])
            prompt = messages[-1]["content"]
            answer = next(
                (value for identifier, value in self.answers.items() if identifier in prompt),
                self.answer,
            )
            streamed = bool(body_mapping.get("stream")) or self.always_streams
            if streamed and not self.ignores_stream:
                events: list[str] = []
                if self.stream_error:
                    events.append(json.dumps({"error": {"message": "relay secret"}}))
                else:
                    deltas = self.stream_deltas
                    if deltas is None:
                        deltas = (("content", answer),)
                    for kind, text in deltas:
                        events.append(json.dumps({"choices": [{"delta": {kind: text}}]}))
                lines = [
                    json.dumps(
                        {"kind": "head", "status": 200, "media_type": "text/event-stream"}
                    )
                ]
                lines.extend(
                    json.dumps({"kind": "data", "offset": 0.01 * (index + 1), "payload": payload})
                    for index, payload in enumerate(events)
                )
                if self.stream_end:
                    lines.append(json.dumps({"kind": "end", "done": True}))
                lines.append(json.dumps({"kind": "elapsed", "seconds": 0.01}))
                envelope = "\n".join(lines) + "\n"
                return subprocess.CompletedProcess(command, 0, envelope, "")
            response = {"choices": [{"message": {"content": answer}}]}
        envelope = "\n".join(
            (
                json.dumps(
                    {
                        "kind": "head",
                        "status": 200 if body is None else self.completion_status,
                        "media_type": "application/json",
                    }
                ),
                json.dumps({"kind": "body", "text": json.dumps(response)}),
                json.dumps({"kind": "elapsed", "seconds": 0.01}),
            )
        ) + "\n"
        return subprocess.CompletedProcess(command, 0, envelope, "")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(
        self,
        path: PathLike,
        text: str,
        *,
        encoding: str = "utf-8",
        mode: int = 0o644,
    ) -> None:
        del encoding, mode
        key = os.fspath(path)
        self.files[key] = text
        parent = str(Path(key).parent)
        self.directories.setdefault(parent, [])
        if Path(key).name not in self.directories[parent]:
            self.directories[parent].append(Path(key).name)

    def exists(self, path: PathLike) -> bool:
        key = os.fspath(path)
        return key in self.files or key in self.directories

    def listdir(self, path: PathLike) -> list[str]:
        return list(self.directories.get(os.fspath(path), []))

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del missing_ok
        self.files.pop(os.fspath(path), None)

    def stat(self, path: PathLike) -> os.stat_result:
        del path
        return os.stat_result((0o100644, 0, 0, 1, 0, 0, 0, 0, 0, 0))

    def chmod(self, path: PathLike, mode: int) -> None:
        del path, mode

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        del path, uid, gid

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del mode, parents, exist_ok
        self.directories.setdefault(os.fspath(path), [])

    def geteuid(self) -> int:
        return self.euid


def _case_file(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(yaml.safe_dump({"cases": records}, sort_keys=False), encoding="utf-8")


def _run_service(
    host: FakeHost,
    cases_path: Path,
    *,
    arguments: Sequence[str] = ("--service", "--no-instruction"),
    output: Path | None = None,
) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    argv = [str(cases_path), *arguments]
    if output is not None:
        argv.extend(("--out", str(output)))
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(
            argv,
            host=cast(Any, host),
            now=lambda: FIXED_NOW,
            checkout=ROOT,
            site_path=SITE_PATH,
        )
    return code, stdout.getvalue(), stderr.getvalue()


class ServiceDoor(unittest.TestCase):
    """The whole door has the same class and safe reporting shape as a stored turn."""

    def test_whole_rows_body_and_classes_use_the_tagged_user_message(self) -> None:
        supplied_prompt, supplied_answer = _seed_case("control-27")
        expected = classify.classify(
            guardrail,
            {"content": supplied_answer, "output": []},
            {"role": "user", "content": supplied_prompt},
        )
        self.assertNotEqual(expected.kind, "leak")
        with tempfile.TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            output = Path(directory) / "out"
            _case_file(
                cases_path,
                [
                    {"id": "plain", "prompt": "Explain the general rule.", "expect": "answered"},
                    {"id": "control-27", "prompt": supplied_prompt, "expect": "recorded"},
                ],
            )
            host = FakeHost(answer=supplied_answer)
            host.answer = "A plain answer."
            host.answers["control-27"] = supplied_answer
            code, stdout, stderr = _run_service(host, cases_path, output=output)

        self.assertEqual(code, 0, stderr)
        self.assertIn("preconditions: ok", stdout)
        self.assertIn("door: ok", stdout)
        self.assertIn("plain: ok — answered", stdout)
        self.assertIn(f"control-27: ok — {expected.kind}", stdout)
        self.assertIn("summary: ok", stdout)
        self.assertIn("record: ok", stdout)
        self.assertNotIn("signin:", stdout)
        self.assertNotIn("cleanup:", stdout)
        self.assertEqual(len(host.exec_inputs), 3)

        requests = host.requests
        self.assertIsNone(requests[0]["body"])
        for request in requests[1:]:
            body = cast(dict[str, object], request["body"])
            messages = cast(list[dict[str, str]], body["messages"])
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["role"], "user")
            self.assertNotIn("system", {message["role"] for message in messages})
            self.assertIn("[turn harness ", messages[0]["content"])

        served_name = _served_name()
        for request in requests[1:]:
            body = cast(dict[str, object], request["body"])
            self.assertEqual(body["model"], served_name)
            self.assertFalse(body["stream"])

        output_prefix = str(output) + "/"
        persisted = [
            text for path, text in host.files.items() if path.startswith(output_prefix)
        ]
        combined = stdout + "\n".join(persisted)
        for value in (
            KEY_VALUE,
            API_USER_NAME_HEADER,
            API_USER_EMAIL_HEADER,
            API_USER_ROLE_HEADER,
            quote(EVAL_IDENTITY.username, safe=""),
            EVAL_IDENTITY.email,
            EVAL_IDENTITY.role,
        ):
            self.assertNotIn(value, combined)
        self.assertNotIn(f"/run/secrets/{API_SECRET_NAME}", combined)

        run_record = json.loads(host.files[str(output / "run.json")])
        self.assertTrue(run_record["arguments"]["service"])
        self.assertFalse(run_record["arguments"]["instruction"])

    def test_service_turn_structured_facts_exist_with_and_without_output(self) -> None:
        prompt, answer = _seed_case("direct-01")
        case = Case("direct-01", prompt, "refused", kind="positive")

        class FixedDoorDriver(run_module.ServiceTurnDriver):
            def __init__(self, reply: door.DoorReply) -> None:
                super().__init__(
                    cast(Host, FakeHost()),
                    RENDERED_COMPOSE.parent,
                    model="served",
                    instruction=None,
                    stream=True,
                )
                self.reply = reply

            def turn(
                self,
                case: Case,
                prompt: str,
                *,
                now: Callable[[], datetime],
                monotonic: Callable[[], float],
                ids_before: frozenset[str],
                row_name: str,
            ) -> run_module.TurnOutcome:
                del case, prompt, now, monotonic, ids_before, row_name
                return run_module.TurnOutcome(
                    chat_id=None,
                    assistant=None,
                    user=None,
                    elapsed=0.25,
                    problem=None,
                    extras=self.reply,
                    started=10.0,
                )

        for output_enabled in (False, True):
            with self.subTest(output=output_enabled), tempfile.TemporaryDirectory() as directory:
                reply = door.DoorReply(
                    status=200,
                    media_type=door.EVENT_STREAM_MEDIA_TYPE,
                    body_text="",
                    events=(door.DoorEvent(0.01, (("content", answer),)),),
                    done=True,
                    elapsed=0.25,
                    problem=None,
                )
                spec = run_module.RunSpec(
                    cases=Path(directory) / "cases.yaml",
                    repeat=1,
                    stream=True,
                    out=Path(directory) / "out" if output_enabled else None,
                    force=False,
                    dry_run=False,
                    sentinel="turn-access-test",
                    service=True,
                )
                row = run_module.service_turn(
                    spec,
                    driver=FixedDoorDriver(reply),
                    guardrail=guardrail,
                    case=case,
                    session_number=1,
                    row_name=case.id,
                    now=lambda: FIXED_NOW,
                    monotonic=lambda: 10.0,
                )
                tagged_prompt = session.prompt_text(case.id, spec.sentinel, prompt)
                expected_class = classify.classify(
                    guardrail,
                    {"content": answer, "output": []},
                    {"role": "user", "content": tagged_prompt},
                )
                expected_stream = classify.stream_verdict(
                    guardrail,
                    (("content", answer),),
                    tagged_prompt,
                )

                self.assertEqual(row.elapsed, 0.25)
                self.assertTrue(row.checks)
                self.assertTrue(all(isinstance(value, bool) for value in row.checks.values()))
                self.assertEqual(row.pattern_id, expected_class.pattern_id)
                self.assertFalse(expected_stream.clean)
                self.assertEqual(row.stream_pattern_id, expected_stream.pattern_id)
                self.assertEqual(row.stream_offset, expected_stream.offset)
                self.assertEqual(row.record is not None, output_enabled)
                facts = (
                    row.elapsed,
                    row.checks,
                    row.pattern_id,
                    row.stream_pattern_id,
                    row.stream_offset,
                )
                self.assertNotIn(prompt, repr(facts))
                self.assertNotIn(answer, repr(facts))

    def test_ci_door_uses_ci_render_and_secret_contract(self) -> None:
        original_directory = secrets.current_directory()
        self.addCleanup(secrets.select_directory, original_directory)
        record: dict[str, object]
        with tempfile.TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            _case_file(cases_path, [{"id": "one", "prompt": "plain", "expect": "answered"}])
            host = FakeHost()
            host.files[f"{CI_ROOT}/compose.yaml"] = "ci-rendered"
            output = Path(directory) / "out"
            loaded: dict[str, object] = {}

            def load_inputs(*args: object, **kwargs: object) -> tuple[object, str, str, str]:
                del args
                loaded.update(kwargs)
                return object(), "", "", ""

            with (
                patch.object(cli.access.render_command, "load_render_inputs", side_effect=load_inputs),
                patch.object(cli.access, "general_texts", return_value=type("Texts", (), {"system_prompt": "instruction"})()),
            ):
                code, stdout, stderr = _run_service(
                    host,
                    cases_path,
                    arguments=("--stack", "ci", "--service"),
                    output=output,
                )
            record = cast(dict[str, object], json.loads(host.files[str(output / "run.json")]))

        self.assertEqual(code, 0, stderr)
        self.assertEqual(loaded["secret_names"], CI_SECRET_NAMES)
        self.assertEqual(host.exec_argv[0][2], "--project-directory")
        self.assertEqual(host.exec_argv[0][3], CI_ROOT)
        self.assertIn("summary: ok — stack: ci;", stdout)
        arguments = cast(dict[str, object], record["arguments"])
        self.assertEqual(arguments["stack"], "ci")

    def test_exec_argv_contains_only_the_mounted_key_path_and_door_failure_is_a_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            _case_file(cases_path, [{"id": "one", "prompt": "plain", "expect": "answered"}])
            host = FakeHost(fail_probe=True)
            code, stdout, stderr = _run_service(host, cases_path)

        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertIn("door: refuse", stdout)
        self.assertNotIn("signin:", stdout)
        self.assertNotIn("cleanup:", stdout)
        self.assertEqual(len(host.exec_argv), 1)
        command_text = "\0".join(host.exec_argv[0])
        self.assertIn(f"/run/secrets/{API_SECRET_NAME}", command_text)
        self.assertNotIn(KEY_VALUE, command_text)
        self.assertNotIn(KEY_VALUE, stdout)

    def test_service_guard_counts_stream_as_one_call_per_case(self) -> None:
        # The door makes one call per case whichever way the answer is
        # delivered, so the two runs are refused at the same count.
        for streamed in (False, True):
            with self.subTest(stream=streamed), tempfile.TemporaryDirectory() as directory:
                cases_path = Path(directory) / "cases.yaml"
                _case_file(cases_path, [{"id": "one", "prompt": "plain", "expect": "answered"}])
                host = FakeHost()
                arguments = ["--service", "--no-instruction", "--repeat", "3", "--concurrent", "3"]
                if streamed:
                    arguments.insert(2, "--stream")
                code, stdout, stderr = _run_service(
                    host, cases_path, arguments=tuple(arguments)
                )
                self.assertEqual(code, 1)
                self.assertEqual(stderr, "")
                self.assertIn("9 turns (3 sessions) during office hours", stdout)
                self.assertEqual(host.exec_inputs, [])

    def test_streamed_row_keeps_ordered_deltas_and_first_offset_in_record(self) -> None:
        deltas = (("content", "A plain "), ("content", "answer."))
        with tempfile.TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            output = Path(directory) / "out"
            _case_file(cases_path, [{"id": "plain", "prompt": "plain", "expect": "answered"}])
            host = FakeHost(stream_deltas=deltas)
            code, stdout, stderr = _run_service(
                host,
                cases_path,
                arguments=("--service", "--no-instruction", "--stream"),
                output=output,
            )

        self.assertEqual(code, 0, stderr)
        self.assertIn("plain: ok — answered; block absent; expect answered", stdout)
        self.assertIn("stream clean", stdout)
        self.assertIn("summary: ok", stdout)
        row = json.loads(host.files[str(output / "plain.json")])
        self.assertEqual(row["stream"]["deltas"], [list(delta) for delta in deltas])
        self.assertEqual(row["stream"]["first_offset"], 0.01)
        self.assertEqual(row["stream"]["verdict"], {"clean": True, "pattern_id": None, "offset": None})
        self.assertEqual(row["answer"], "A plain answer.")

    def test_streamed_door_turns_are_the_cases_not_twice_the_cases(self) -> None:
        # A frontend --stream row is a managed turn plus a raw replay, two
        # calls; the door's is the case's one call, delivered streamed, so the
        # summary must not count it twice (found on the box at ticket 05).
        with tempfile.TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            output = Path(directory) / "out"
            _case_file(
                cases_path,
                [
                    {"id": "one", "prompt": "plain", "expect": "answered"},
                    {"id": "two", "prompt": "plain", "expect": "answered"},
                ],
            )
            host = FakeHost(stream_deltas=(("content", "A plain answer."),))
            code, stdout, stderr = _run_service(
                host,
                cases_path,
                arguments=("--service", "--no-instruction", "--stream"),
                output=output,
            )

        self.assertEqual(code, 0, stderr)
        self.assertIn("summary: ok — 2 turns;", stdout)
        self.assertIn("stream: 2 clean, 0 leak, 0 error", stdout)
        run_record = json.loads(host.files[str(output / "run.json")])
        self.assertEqual(run_record["summary"]["turns"], 2)
        # One exec per case: the probe, then one call each.
        self.assertEqual(len(host.exec_inputs), 3)


    def test_a_streamed_request_answered_whole_is_a_failed_turn(self) -> None:
        # The relay lets the engine's answer decide the shape, so a --stream
        # run can be answered whole. Judging its empty delta list would report
        # "stream clean" with no released prefix ever examined.
        with tempfile.TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            _case_file(cases_path, [{"id": "plain", "prompt": "plain", "expect": "answered"}])
            host = FakeHost(ignores_stream=True)
            code, stdout, stderr = _run_service(
                host,
                cases_path,
                arguments=("--service", "--no-instruction", "--stream"),
            )
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertIn("door answered a streamed request with a whole response", stdout)
        self.assertNotIn("stream clean", stdout)

    def test_a_whole_request_answered_as_a_stream_is_a_failed_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            _case_file(cases_path, [{"id": "plain", "prompt": "plain", "expect": "answered"}])
            host = FakeHost(always_streams=True)
            code, stdout, stderr = _run_service(host, cases_path)
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertIn("door answered a whole request with a streamed response", stdout)

    def test_a_case_row_carries_the_doors_own_fix_not_the_records(self) -> None:
        # A refused key is repaired by rotating it, never by reading a record
        # that a run without --out never wrote.
        with tempfile.TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            _case_file(cases_path, [{"id": "plain", "prompt": "plain", "expect": "answered"}])
            host = FakeHost(completion_status=401)
            code, stdout, stderr = _run_service(host, cases_path)
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        # The probe passed, so the failure is the case row's, not the door's.
        self.assertIn("door: ok", stdout)
        self.assertIn("plain: refuse", stdout)
        self.assertIn("door returned HTTP 401", stdout)
        self.assertIn(f"secrets rotate {API_SECRET_NAME}", stdout)
        self.assertNotIn("Read the record", stdout)


    def test_prefix_then_guardrail_refusal_is_the_stored_replacement_class(self) -> None:
        refusal = str(guardrail.REFUSALS[0])
        prefix = "A harmless released preface."
        answer = f"{prefix}{guardrail.REFUSAL_SEPARATOR}{refusal}"
        expected = classify.classify(
            guardrail,
            {"content": answer, "output": []},
            {"role": "user", "content": "plain"},
        )
        with tempfile.TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            _case_file(cases_path, [{"id": "replacement", "prompt": "plain", "expect": "refused"}])
            host = FakeHost(stream_deltas=(("content", prefix), ("content", f"{guardrail.REFUSAL_SEPARATOR}{refusal}")))
            code, stdout, stderr = _run_service(
                host,
                cases_path,
                arguments=("--service", "--no-instruction", "--stream"),
            )
        self.assertEqual(code, 0, stderr)
        self.assertIn(f"replacement: ok — {expected.kind}", stdout)

    @staticmethod
    def _service_arguments(streamed: bool) -> tuple[str, ...]:
        arguments = ("--service", "--no-instruction")
        return (*arguments, "--stream") if streamed else arguments

    def test_refusal_without_released_prefix_is_the_stored_replacement_class(self) -> None:
        refusal = str(guardrail.REFUSALS[0])
        self.assertNotIn(guardrail.REFUSAL_SEPARATOR, refusal)
        expected = classify.classify(
            guardrail,
            {"content": refusal, "output": []},
            {"role": "user", "content": "plain"},
        )
        stream_expected = classify.stream_verdict(
            guardrail,
            (("content", refusal),),
            "plain",
        )
        self.assertTrue(stream_expected.clean)
        for streamed in (False, True):
            with self.subTest(stream=streamed), tempfile.TemporaryDirectory() as directory:
                cases_path = Path(directory) / "cases.yaml"
                _case_file(
                    cases_path,
                    [{"id": "replacement-alone", "prompt": "plain", "expect": "refused"}],
                )
                host = (
                    FakeHost(stream_deltas=(("content", refusal),))
                    if streamed
                    else FakeHost(answer=refusal)
                )
                code, stdout, stderr = _run_service(
                    host, cases_path, arguments=self._service_arguments(streamed)
                )
                self.assertEqual(code, 0, stderr)
                self.assertIn(f"replacement-alone: ok — {expected.kind}", stdout)
                if streamed:
                    self.assertIn("stream clean", stdout)

    def test_content_only_stream_and_whole_body_pass_the_withheld_check(self) -> None:
        answer = "A plain answer."
        for streamed in (False, True):
            with self.subTest(stream=streamed), tempfile.TemporaryDirectory() as directory:
                cases_path = Path(directory) / "cases.yaml"
                _case_file(
                    cases_path,
                    [{"id": "content-only", "prompt": "plain", "expect": "answered"}],
                )
                host = (
                    FakeHost(stream_deltas=(("content", answer),))
                    if streamed
                    else FakeHost(answer=answer)
                )
                code, stdout, stderr = _run_service(
                    host, cases_path, arguments=self._service_arguments(streamed)
                )
                self.assertEqual(code, 0, stderr)
                self.assertIn("withheld ok", stdout)
                self.assertNotIn("withheld red", stdout)

    def test_stream_leak_fails_the_row_under_the_shared_policy(self) -> None:
        prompt, answer = _seed_case("direct-01")
        expected = classify.stream_verdict(
            guardrail,
            (("content", answer),),
            session.prompt_text("direct-01", "test", prompt),
        )
        self.assertFalse(expected.clean)
        with tempfile.TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            _case_file(cases_path, [{"id": "direct-01", "prompt": prompt, "expect": "answered"}])
            host = FakeHost(stream_deltas=(("content", answer),))
            code, stdout, stderr = _run_service(
                host,
                cases_path,
                arguments=("--service", "--no-instruction", "--stream"),
            )
        self.assertEqual(code, 1)
        self.assertIn(
            f"stream leak@{expected.pattern_id} at {expected.offset} chars", stdout
        )
        self.assertIn("summary: refuse", stdout)

    # This is the check's own tripwire, no longer today's state: it plants a
    # fake wire carrying reasoning and proves the door turns red if it leaks.
    def test_reasoning_on_the_wire_fails_the_withheld_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            output = Path(directory) / "out"
            _case_file(cases_path, [{"id": "reasoning", "prompt": "plain", "expect": "answered"}])
            host = FakeHost(
                stream_deltas=(("reasoning", "A private chain."), ("content", "A plain answer."))
            )
            code, stdout, stderr = _run_service(
                host,
                cases_path,
                arguments=("--service", "--no-instruction", "--stream"),
                output=output,
            )
        self.assertEqual(code, 1)
        self.assertIn("reasoning: refuse", stdout)
        self.assertIn("withheld failed", stdout)
        row = json.loads(host.files[str(output / "reasoning.json")])
        self.assertTrue(row["verdict"]["reasoning_stored"])

    def test_stream_without_end_marker_is_a_problem_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            _case_file(cases_path, [{"id": "plain", "prompt": "plain", "expect": "answered"}])
            host = FakeHost(stream_deltas=(("content", "A plain answer."),), stream_end=False)
            code, stdout, stderr = _run_service(
                host,
                cases_path,
                arguments=("--service", "--no-instruction", "--stream"),
            )
        self.assertEqual(code, 1)
        self.assertIn("door stream ended without its end marker", stdout)
        self.assertNotIn("signin:", stdout)
        self.assertNotIn("cleanup:", stdout)

    def test_relay_error_event_is_a_content_free_problem_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            output = Path(directory) / "out"
            _case_file(cases_path, [{"id": "plain", "prompt": "plain", "expect": "answered"}])
            host = FakeHost(stream_error=True)
            code, stdout, stderr = _run_service(
                host,
                cases_path,
                arguments=("--service", "--no-instruction", "--stream"),
                output=output,
            )
        self.assertEqual(code, 1)
        self.assertIn("the stream carried an error", stdout)
        self.assertNotIn("relay secret", stdout)
        row = json.loads(host.files[str(output / "plain.json")])
        self.assertEqual(row["answer"], None)
        self.assertNotIn("relay secret", json.dumps(row))


class DoorBody(unittest.TestCase):
    """General's instruction is the adapter's own, sent as the system message."""

    def test_instruction_rides_as_the_system_message_and_one_flag_drops_it(self) -> None:
        instructed = door.completion_body(
            served_name="served-x",
            prompt="tagged prompt",
            instruction="You are General.",
            stream=False,
        )
        self.assertEqual(
            instructed["messages"],
            [
                {"role": "system", "content": "You are General."},
                {"role": "user", "content": "tagged prompt"},
            ],
        )
        self.assertEqual(instructed["model"], "served-x")
        self.assertIs(instructed["stream"], False)

        bare = door.completion_body(
            served_name="served-x",
            prompt="tagged prompt",
            instruction=None,
            stream=True,
        )
        self.assertEqual(bare["messages"], [{"role": "user", "content": "tagged prompt"}])
        self.assertIs(bare["stream"], True)

    def test_the_driver_sends_the_instruction_it_was_built_with(self) -> None:
        host = FakeHost()
        driver = run_module.ServiceTurnDriver(
            cast(Any, host),
            "/etc/gideon/rendered",
            model=_served_name(),
            instruction="You are General.",
        )
        driver.turn(
            Case(id="one", prompt="plain", expect="answered"),
            "tagged prompt [turn harness abcd1234 one]",
            now=lambda: FIXED_NOW,
            monotonic=time.monotonic,
            ids_before=frozenset(),
            row_name="one",
        )
        body = cast(dict[str, object], host.requests[-1]["body"])
        messages = cast(list[dict[str, str]], body["messages"])
        self.assertEqual(messages[0], {"role": "system", "content": "You are General."})
        self.assertEqual(messages[1]["content"], "tagged prompt [turn harness abcd1234 one]")

    def test_service_refusals_happen_before_any_exec(self) -> None:
        early_refusals = (
            (("--service", "--browser"), "--browser"),
            (("--service", "--probe-inlet"), "--probe-inlet"),
            (("--service", "--trust-ca"), "--trust-ca"),
            (("--service", "--unfiltered"), "--unfiltered"),
            (("--no-instruction",), "--service or --unfiltered"),
        )
        for arguments, expected in early_refusals:
            with self.subTest(arguments=arguments):
                early_stdout = io.StringIO()
                with contextlib.redirect_stdout(early_stdout):
                    code = cli.main(["missing.yaml", *arguments])
                self.assertEqual(code, 1)
                self.assertIn("preconditions: refuse", early_stdout.getvalue())
                self.assertIn(expected, early_stdout.getvalue())
                self.assertIn("Fix:", early_stdout.getvalue())

        with tempfile.TemporaryDirectory() as directory:
            searched_path = Path(directory) / "searched.yaml"
            _case_file(
                searched_path,
                [{"id": "searched", "prompt": "plain", "expect": "answered", "search": True}],
            )
            sources_path = Path(directory) / "sources.yaml"
            _case_file(
                sources_path,
                [{"id": "sources", "prompt": "plain", "expect": "answered", "sources": "present"}],
            )
            for cases_path in (searched_path, sources_path):
                with self.subTest(cases_path=cases_path.name):
                    host = FakeHost()
                    code, stdout, stderr = _run_service(host, cases_path)
                    self.assertEqual(code, 1)
                    self.assertEqual(stderr, "")
                    self.assertIn("preconditions: refuse", stdout)
                    self.assertEqual(host.exec_inputs, [])


class ImportBoundary(unittest.TestCase):
    """The copied client remains standard-library-only, and the tripwire itself works."""

    @staticmethod
    def external_imports(source: str) -> set[str]:
        tree = ast.parse(source)
        targets: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets.update(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                targets.add(node.module.partition(".")[0])
        return targets - set(sys.stdlib_module_names)

    def test_client_has_no_external_import_and_planted_violation_is_caught(self) -> None:
        source = (ROOT / "gideon/evaluation/turns/doorclient.py").read_text(encoding="utf-8")
        self.assertEqual(self.external_imports(source), set())
        self.assertEqual(self.external_imports("import yaml\n"), {"yaml"})
