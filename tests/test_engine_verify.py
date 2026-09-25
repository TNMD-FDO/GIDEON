"""Contracts for the direct engine verification command."""

import argparse
import contextlib
import io
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from gideon import guardrail
from gideon.host import audit, backuplock, engine, enginesample, owui, owuiturn, secrets
from gideon.host.owui import OwuiError
from gideon.host.render.engine import (
    ENGINE_PORT,
    ENGINE_SECRET_NAME,
    ENGINE_SERVICE_NAME,
)
from gideon.host.render.owui import EVAL_IDENTITY, EVAL_PASSWORD_SECRET
from gideon.host.report import Problem
from gideon.host.secrets import SECRET_REGISTRY
from gideon.host.stack import exec_argv
from gideon.host.sysio import Command, PathLike

ROOT = Path(__file__).resolve().parent.parent
SITE_PATH = "/etc/gideon/site.yaml"
RENDERED = "/etc/gideon/rendered"
MODELS_PATH = "/models.lock"
SAMPLE_PATH = "/sample.yaml"
SECRET_VALUE = "not-a-real-secret"


def completed(
    argv: Command,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)


def site_text(profile: str = "1x1v-1d") -> str:
    return ROOT.joinpath("config/site.example.yaml").read_text() + f"\nhardware_profile: {profile}\n"


def models_text(max_model_len: int = 4096) -> str:
    return f'''version: 1
reference: 1x1v-1d
profiles:
  1x1v-1d:
    requires:
      platform: x86_64
      gpu:
        architecture: Fixture
        compute_capability: "1.0"
        model: Fixture GPU
        count: 1
        vram_gb: 1
      dram_gb: 1
      data_volume_gb: 1
    memory:
      {ENGINE_SERVICE_NAME}:
        gb: 1
        role: generator
    models:
      generator:
        repo: example/fixture
        revision: "{'a' * 40}"
        gpu: 0
        serve:
          served_name: {ENGINE_SERVICE_NAME}
          env:
            HF_HOME: /data/models
          flags:
            max-model-len: {max_model_len}
        files:
          config.json:
            sha256: sha256:{'b' * 64}
            size: 1
'''


def ps_output(*, health: str = "healthy", state: str = "running") -> str:
    return json.dumps(
        [{"Service": ENGINE_SERVICE_NAME, "State": state, "Health": health}]
    )


def sse_output(
    *,
    done: bool = True,
    reasoning: str = "thinking ",
    content: str = "A few paragraphs of fixture output.",
    finish: str = "stop",
    first: float = 0.31,
    elapsed: float = 2.0,
    status: int = 200,
    prompt_tokens: int = 12,
    completion_tokens: int = 4,
) -> str:
    events = [
        {"choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]},
        {"choices": [{"delta": {"reasoning": reasoning}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": content}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": finish}]},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        },
    ]
    body = "\n".join(f"data: {json.dumps(event)}" for event in events)
    if done:
        body += "\ndata: [DONE]"
    return (
        f"{body}\n"
        f"@gideon-engine-verify http_code={status} "
        f"time_starttransfer={first:.6f} time_total={elapsed:.6f}\n"
    )


class FakeHost:
    """Dict-backed Host implementing the complete sysio.Host protocol."""

    def __init__(
        self,
        *,
        files: Mapping[str, str] | None = None,
        euid: int = 0,
        ps: str | None = None,
        exec_stdout: str | None = None,
        exec_returncode: int = 0,
        exec_error: BaseException | None = None,
        needle_content: str | None = None,
        needle_usage_offset: int = 0,
        needle_status: int = 200,
        needle_error: str = "",
        tokenize_failure: bool = False,
        never_lands: bool = False,
        structured_content: str | None = '{"objective":"fixture-objective","route":"map","steps":["fixture-step"],"confidence":80}',
        structured_finish: str | None = "stop",
        structured_status: int = 200,
        structured_error: str = "",
        lock_holder: str | None = None,
        lock_error: OSError | None = None,
    ) -> None:
        self.files = dict(files or {})
        self.euid = euid
        self.ps = ps or ps_output()
        self.exec_stdout = exec_stdout if exec_stdout is not None else sse_output()
        self.exec_returncode = exec_returncode
        self.exec_error = exec_error
        self.needle_content = needle_content
        self.needle_usage_offset = needle_usage_offset
        self.needle_status = needle_status
        self.needle_error = needle_error
        self.tokenize_failure = tokenize_failure
        self.never_lands = never_lands
        self.structured_content = structured_content
        self.structured_finish = structured_finish
        self.structured_status = structured_status
        self.structured_error = structured_error
        self.runs: list[tuple[tuple[str, ...], str | None, float | None]] = []
        self.lock_holder = lock_holder
        self.lock_error = lock_error
        self.locks: dict[str, str] = {}
        self.lock_log: list[tuple[str, str]] = []
        self.reads: list[str] = []

    def _token_count(self, body: Mapping[str, object]) -> int:
        messages = body.get("messages")
        if not isinstance(messages, list):
            return 0
        text = "\n".join(
            message.get("content", "")
            for message in messages
            if isinstance(message, Mapping) and isinstance(message.get("content"), str)
        )
        return len(text.split()) + sum(character.isdigit() for character in text)

    def _json_reply(self, body: Mapping[str, object], *, status: int = 200) -> str:
        if status != 200:
            response: Mapping[str, object] = {"error": {"message": self.needle_error}}
        else:
            response = {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": (
                            {"content": self.needle_content}
                            if self.needle_content is not None
                            else {}
                        ),
                    }
                ],
                "usage": {
                    "prompt_tokens": self._token_count(body) + self.needle_usage_offset,
                    "completion_tokens": 3,
                },
            }
        return (
            json.dumps(response)
            + "\n@gideon-engine-verify http_code="
            + str(status)
            + " time_starttransfer=0.20 time_total=3.90\n"
        )

    def _structured_reply(self) -> str:
        if self.structured_status != 200:
            response: Mapping[str, object] = {
                "error": {"message": self.structured_error}
            }
        else:
            message = (
                {"content": self.structured_content}
                if self.structured_content is not None
                else {}
            )
            response = {
                "choices": [
                    {"finish_reason": self.structured_finish, "message": message}
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 4},
            }
        return (
            json.dumps(response)
            + "\n@gideon-engine-verify http_code="
            + str(self.structured_status)
            + " time_starttransfer=0.20 time_total=2.50\n"
        )

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
        del cwd, env, passthrough
        command = tuple(argv)
        self.runs.append((command, input, timeout))
        if self.exec_error is not None and "exec" in command:
            raise self.exec_error
        if tuple(command[-4:]) == ("ps", "--all", "--format", "json"):
            return completed(command, stdout=self.ps)
        if "exec" in command and ENGINE_SERVICE_NAME in command:
            if input is not None and command[-2].endswith("/tokenize"):
                if self.tokenize_failure:
                    return completed(command, returncode=7, stderr="tokenizer unavailable")
                body = json.loads(input)
                count = self._token_count(body)
                if self.never_lands and body.get("messages"):
                    messages = body["messages"]
                    if isinstance(messages, list) and messages:
                        first_message = messages[0]
                        if (
                            isinstance(first_message, Mapping)
                            and isinstance(first_message.get("content"), str)
                            and first_message["content"].count("Paragraph ") > 2
                        ):
                            count = 1_000_000
                return completed(
                    command,
                    stdout=(
                        json.dumps({"count": count})
                        + "\n@gideon-engine-verify http_code=200 "
                        "time_starttransfer=0.10 time_total=0.20\n"
                    ),
                )
            if input is not None and json.loads(input).get("stream") is False:
                request = json.loads(input)
                if isinstance(request.get("response_format"), Mapping):
                    return completed(command, stdout=self._structured_reply())
                return completed(
                    command,
                    stdout=self._json_reply(
                        request, status=self.needle_status
                    ),
                )
            return completed(command, returncode=self.exec_returncode, stdout=self.exec_stdout)
        result = completed(command, returncode=1, stderr="unexpected host command")
        if check:
            raise subprocess.CalledProcessError(result.returncode, list(command))
        return result

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        self.reads.append(key)
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
        self.files[os.fspath(path)] = text

    def exists(self, path: PathLike) -> bool:
        return os.fspath(path) in self.files

    def listdir(self, path: PathLike) -> list[str]:
        del path
        raise NotImplementedError

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del path, missing_ok
        raise NotImplementedError

    def stat(self, path: PathLike) -> os.stat_result:
        del path
        raise NotImplementedError

    def chmod(self, path: PathLike, mode: int) -> None:
        del path, mode
        raise NotImplementedError

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        del path, uid, gid
        raise NotImplementedError

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del mode, parents, exist_ok
        self.lock_log.append(("mkdir", os.fspath(path)))

    def take_lock(self, path: PathLike, record: str) -> str | None:
        key = os.fspath(path)
        self.lock_log.append(("take", key))
        if self.lock_error is not None:
            raise self.lock_error
        if self.lock_holder is not None:
            return self.lock_holder
        if key in self.locks:
            return self.locks[key]
        self.locks[key] = record
        return None

    def release_lock(self, path: PathLike) -> None:
        key = os.fspath(path)
        self.lock_log.append(("release", key))
        self.locks.pop(key, None)

    def geteuid(self) -> int:
        return self.euid


class FakeAudit:
    AuditRow = audit.AuditRow

    def __init__(self, *, probe_problem: str | None = None, write_problem: str | None = None) -> None:
        self.probe_problem = probe_problem
        self.write_problem = write_problem
        self.rows: list[audit.AuditRow] = []

    def probe(self, io: FakeHost, rendered_dir: PathLike) -> str | None:
        del io, rendered_dir
        return self.probe_problem

    def write_rows(
        self,
        io: FakeHost,
        rendered_dir: PathLike,
        rows: tuple[audit.AuditRow, ...],
    ) -> str | None:
        del io, rendered_dir
        self.rows.extend(rows)
        return self.write_problem


class FakeFrontendClient(owui.Client):
    """Dict-backed frontend client for one shared signed-in fake account."""

    def __init__(self, backend: "FakeFrontend") -> None:
        self.backend = backend

    def signin(self, email: str, password: str) -> str:
        if self.backend.signin_error is not None:
            raise self.backend.signin_error
        self.backend.signin_args = (email, password)
        return "fixture-session-token"

    def request(self, method: str, path: str, body: object | None = None) -> owui.Response:
        return self.backend.request(method, path, body)


class FakeFrontend:
    """In-memory Open WebUI account, with per-case failures and stored records."""

    def __init__(
        self,
        refusal: str,
        *,
        trip_id: str | None = None,
        behaviors: Mapping[str, Mapping[str, object]] | None = None,
        signin_error: OwuiError | None = None,
    ) -> None:
        self.refusal = refusal
        self.trip_id = trip_id
        self.behaviors = dict(behaviors or {})
        self.signin_error = signin_error
        self.signin_args: tuple[str, str] | None = None
        self.chat_ids: list[str] = []
        self.records: dict[str, Mapping[str, object]] = {}
        self.requests: list[tuple[str, str, object | None]] = []
        self.factory_calls: list[Mapping[str, object]] = []
        self.case_id: str | None = None
        self.case_number = 0

    def factory(self, **_: object) -> FakeFrontendClient:
        self.factory_calls.append(_)
        return FakeFrontendClient(self)

    def behavior(self) -> Mapping[str, object]:
        return self.behaviors.get(self.case_id or "", {})

    def _foreign_chat(self, *, vanished: bool) -> None:
        """Add another session's complete chat, or list its already-gone id."""

        index = self.case_number
        chat_id = f"foreign-chat-{index}"
        self.chat_ids.append(chat_id)
        if vanished:
            return
        user_id = f"foreign-user-{index}"
        assistant_id = f"foreign-assistant-{index}"
        self.records[chat_id] = {
            "chat": {
                "history": {
                    "currentId": assistant_id,
                    "messages": {
                        user_id: {
                            "id": user_id,
                            "role": "user",
                            "content": "foreign prompt",
                        },
                        assistant_id: {
                            "id": assistant_id,
                            "role": "assistant",
                            "content": "foreign answer",
                            "parentId": user_id,
                            "done": True,
                        },
                    },
                }
            }
        }

    def request(self, method: str, path: str, body: object | None) -> owui.Response:
        self.requests.append((method, path, body))
        if path == owuiturn._CHAT_LIST_PATH:
            if self.behavior().get("list_error"):
                raise OwuiError("fixture chat listing failed")
            return owui.Response(200, [{"id": chat_id} for chat_id in self.chat_ids])
        if path == owuiturn.COMPLETIONS_PATH:
            assert isinstance(body, Mapping)
            prompt = body["messages"][0]["content"]
            assert isinstance(prompt, str)
            self.case_id = prompt.rsplit(" ", 1)[-1].removesuffix("]")
            behavior = self.behavior()
            if behavior.get("turn_error"):
                raise OwuiError("fixture managed turn failed")
            turn_status = behavior.get("turn_status")
            if type(turn_status) is int:
                return owui.Response(turn_status, None)
            chat_id = f"fixture-chat-{self.case_number}"
            self.case_number += 1
            if behavior.get("foreign_only"):
                self._foreign_chat(vanished=False)
            elif not behavior.get("no_new_chat"):
                self.chat_ids.append(chat_id)
                if not behavior.get("missing_assistant"):
                    user_id = body["user_message"]["id"]
                    assistant_id = body["id"]
                    assert isinstance(user_id, str)
                    assert isinstance(assistant_id, str)
                    content = behavior.get("content")
                    if not isinstance(content, str):
                        content = (
                            self.refusal
                            if self.case_id == self.trip_id
                            else "I cannot answer that request."
                        )
                    assistant: dict[str, object] = {
                        "id": assistant_id,
                        "role": "assistant",
                        "content": content,
                        "parentId": user_id,
                        "done": behavior.get("done", True),
                    }
                    if "error" in behavior:
                        assistant["error"] = behavior["error"]
                    if "thinking" in behavior:
                        assistant["output"] = [
                            {
                                "type": "reasoning",
                                "content": [{"type": "text", "text": behavior["thinking"]}],
                            }
                        ]
                    self.records[chat_id] = {
                        "chat": {
                            "history": {
                                "currentId": assistant_id,
                                "messages": {
                                    user_id: {
                                        "id": user_id,
                                        "role": "user",
                                        "content": prompt,
                                    },
                                    assistant_id: assistant,
                                },
                            }
                        }
                    }
                if behavior.get("two_matches"):
                    second_chat_id = f"fixture-chat-{self.case_number}"
                    self.case_number += 1
                    self.chat_ids.append(second_chat_id)
                    self.records[second_chat_id] = dict(self.records[chat_id])
                if behavior.get("foreign_chat"):
                    self._foreign_chat(vanished=False)
                elif behavior.get("foreign_vanished"):
                    self._foreign_chat(vanished=True)
            return owui.Response(200, None)
        if method == "DELETE":
            if self.behavior().get("delete_error"):
                raise OwuiError("fixture chat deletion failed")
            delete_status = self.behavior().get("delete_status")
            if type(delete_status) is int:
                return owui.Response(delete_status, None)
            if self.behavior().get("delete_false"):
                return owui.Response(200, False)
            chat_id = path.removeprefix(owuiturn._CHAT_PATH)
            if chat_id not in self.chat_ids:
                return owui.Response(401, {"detail": "Not found"})
            self.chat_ids.remove(chat_id)
            return owui.Response(200, True)
        if path.startswith(owuiturn._CHAT_PATH):
            if self.behavior().get("read_error"):
                raise OwuiError("fixture chat read failed")
            chat_id = path.removeprefix(owuiturn._CHAT_PATH)
            if chat_id not in self.records:
                return owui.Response(401, {"detail": "Not found"})
            return owui.Response(200, self.records[chat_id])
        raise AssertionError((method, path))


class CommandTests(unittest.TestCase):
    def make_host(self, **kwargs: Any) -> FakeHost:
        files = {
            SITE_PATH: site_text(),
            str(Path(RENDERED) / "compose.yaml"): "services:\n  gideon-generator: {}\n",
            MODELS_PATH: models_text(),
            SAMPLE_PATH: ROOT.joinpath("eval/engine-verify/sample.yaml").read_text(),
            str(secrets.secret_path(EVAL_PASSWORD_SECRET)): SECRET_VALUE,
        }
        files.update(kwargs.pop("files", {}))
        host = FakeHost(files=files, **kwargs)
        sample = enginesample.load_sample(SAMPLE_PATH, host=host).sample
        if sample is not None and "needle_content" not in kwargs:
            host.needle_content = sample.needle.expected
        return host

    def make_frontend(
        self, *, behaviors: Mapping[str, Mapping[str, object]] | None = None,
        signin_error: OwuiError | None = None,
    ) -> FakeFrontend:
        sample = enginesample.load_sample(SAMPLE_PATH, host=self.make_host()).sample
        assert sample is not None
        return FakeFrontend(
            guardrail.DEADLINE_REFUSAL,
            trip_id=sample.frontend.trip.id,
            behaviors=behaviors,
            signin_error=signin_error,
        )

    def run_command(
        self,
        host: FakeHost,
        audit_backend: FakeAudit | None = None,
        frontend: FakeFrontend | None = None,
        observe: list[engine.StageResult] | None = None,
    ) -> tuple[int, str, FakeAudit]:
        backend = audit_backend or FakeAudit()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = engine.run_engine_verify(
                argparse.Namespace(),
                host=host,
                site_path=SITE_PATH,
                rendered_dir=RENDERED,
                root=ROOT,
                models_path=MODELS_PATH,
                sample_path=SAMPLE_PATH,
                audit=backend,
                sleep=lambda _: None,
                client_factory=(frontend or self.make_frontend()).factory,
                observe=(observe.append if observe is not None else None),
            )
        return result, output.getvalue(), backend

    def stored(self, content: object, **assistant_fields: object) -> owuiturn.StoredTurn:
        assistant = {"content": content, "done": True, **assistant_fields}
        return owuiturn.StoredTurn(assistant=assistant, user={"content": "fixture prompt"})

    def test_stage_order_body_and_audit(self) -> None:
        host = self.make_host()
        code, output, backend = self.run_command(host)
        loaded = enginesample.load_sample(SAMPLE_PATH, host=host).sample
        assert loaded is not None

        self.assertEqual(code, 0)
        self.assertEqual(
            [line.split(":", 1)[0] for line in output.splitlines()],
            [
                "preconditions",
                "needle-32k",
                "needle-128k",
                "needle-256k",
                "structured",
                "smoke",
                *(
                    f"{engine.FRONTEND_ROW_PREFIX}{case.id}"
                    for case in (*loaded.frontend.positives, loaded.frontend.trip)
                ),
                "audit",
            ],
        )
        sample_result = enginesample.load_sample(SAMPLE_PATH, host=host)
        assert sample_result.sample is not None
        for line in output.splitlines():
            if ": ok" in line:
                self.assertNotIn("Fix:", line)
        self.assertIn("profile 1x1v-1d", output)
        self.assertIn("window 4096", output)
        self.assertIn(f"sample {sample_result.sample.sha256[:12]}", output)
        frontend = sample_result.sample.frontend
        self.assertEqual(
            [f"{engine.FRONTEND_ROW_PREFIX}{case.id}" for case in frontend.positives]
            + [f"{engine.FRONTEND_ROW_PREFIX}{frontend.trip.id}"],
            [
                line.split(":", 1)[0]
                for line in output.splitlines()
                if line.startswith(engine.FRONTEND_ROW_PREFIX)
            ],
        )
        self.assertEqual(len(frontend.positives), 2)
        exec_runs = [run for run in host.runs if "exec" in run[0]]
        self.assertGreater(len(exec_runs), 3)
        argv, request_text, timeout = exec_runs[-1]
        self.assertEqual(
            argv,
            tuple(
                exec_argv(
                    RENDERED,
                    ENGINE_SERVICE_NAME,
                    "sh",
                    "-c",
                    engine.ENGINE_CURL_SCRIPT,
                    "gideon-engine-verify",
                    f"/run/secrets/{ENGINE_SECRET_NAME}",
                    f"http://{ENGINE_SERVICE_NAME}:{ENGINE_PORT}/v1/chat/completions",
                    str(engine.SMOKE_TIMEOUT_SECONDS),
                )
            ),
        )
        self.assertEqual(timeout, engine.SMOKE_TIMEOUT_SECONDS + engine.RUN_TIMEOUT_MARGIN_SECONDS)
        assert request_text is not None
        request = json.loads(request_text)
        self.assertTrue(request["stream"])
        self.assertEqual(request["stream_options"], {"include_usage": True})
        self.assertEqual(
            request["messages"][0]["content"], sample_result.sample.smoke.prompt
        )
        self.assertNotIn(SECRET_VALUE, request_text)
        self.assertNotIn(SECRET_VALUE, " ".join(argv))

        self.assertEqual(len(backend.rows), 1)
        row = backend.rows[0]
        self.assertEqual(row.kind, "engine_verify")
        self.assertEqual(row.detail["outcome"], "ok")
        checks = row.detail["checks"]
        self.assertIsInstance(checks, Mapping)
        assert isinstance(checks, Mapping)
        check = checks["smoke"]
        self.assertIsInstance(check, Mapping)
        assert isinstance(check, Mapping)
        self.assertEqual(set(check), {"ok", "http_status", "finish_reason", "prompt_tokens", "completion_tokens", "chars", "elapsed_seconds", "first_byte_seconds", "chars_per_second", "tokens_per_second", "implied_lag_seconds"})
        self.assertEqual(check["http_status"], 200)
        self.assertNotIn(sample_result.sample.smoke.prompt, output)
        self.assertNotIn("tabletop", repr(row.detail))

        frontend_checks = {
            name: checks[name]
            for name in checks
            if name.startswith(engine.FRONTEND_ROW_PREFIX)
        }
        self.assertEqual(len(frontend_checks), 3)
        self.assertTrue(all(isinstance(value, Mapping) for value in frontend_checks.values()))

    def test_engine_lock_is_claimed_and_released_after_the_verify_run(self) -> None:
        host = self.make_host()
        code, output, _ = self.run_command(host)

        self.assertEqual(code, 0, output)
        self.assertIn("and audit writer are ready; engine lock taken", output)
        self.assertEqual(host.lock_log[-1], ("release", backuplock.ENGINE_LOCK.path))
        self.assertEqual(host.locks, {})

    def test_engine_lock_refusal_stops_before_site_read_and_is_observed(self) -> None:
        holder = backuplock.Record(
            "eval run fixture", os.getpid() + 1, datetime(2026, 9, 2, tzinfo=UTC)
        )
        host = self.make_host(lock_holder=holder.to_json())
        host.reads.clear()
        observed: list[engine.StageResult] = []
        code, output, backend = self.run_command(host, observe=observed)

        self.assertEqual(code, 1)
        self.assertIn(f"{holder.command} since {holder.started.isoformat()}", output)
        self.assertIn(f"pid {holder.pid}", output)
        self.assertIn("ps -p", output)
        self.assertFalse(host.runs)
        self.assertFalse(host.reads)
        self.assertEqual([row.name for row in observed], ["preconditions"])
        self.assertFalse(observed[0].ok)
        self.assertEqual(backend.rows, [])
        self.assertEqual(host.lock_log[-1], ("take", backuplock.ENGINE_LOCK.path))

    def test_engine_lock_nested_claim_is_not_released(self) -> None:
        holder = backuplock.Record(
            "existing holder", os.getpid(), datetime(2026, 9, 2, tzinfo=UTC)
        )
        host = self.make_host()
        host.locks[backuplock.ENGINE_LOCK.path] = holder.to_json()
        code, output, _ = self.run_command(host)

        self.assertEqual(code, 0, output)
        self.assertIn("and audit writer are ready; engine lock held by this process", output)
        self.assertEqual(host.lock_log[-1], ("take", backuplock.ENGINE_LOCK.path))
        self.assertIn(backuplock.ENGINE_LOCK.path, host.locks)

    def test_frontend_requests_use_loaded_cases_and_audit_figures_are_content_free(self) -> None:
        host = self.make_host()
        frontend = self.make_frontend()
        code, output, backend = self.run_command(host, frontend=frontend)

        self.assertEqual(code, 0, output)
        sample = enginesample.load_sample(SAMPLE_PATH, host=host).sample
        assert sample is not None
        cases = [*sample.frontend.positives, sample.frontend.trip]
        posts = [body for method, path, body in frontend.requests if method == "POST" and path == owuiturn.COMPLETIONS_PATH]
        self.assertEqual(len(posts), len(cases))
        self.assertEqual(frontend.factory_calls, [{}, {"token": "fixture-session-token"}])
        self.assertEqual(frontend.signin_args, (EVAL_IDENTITY.email, SECRET_VALUE))
        for case, body in zip(cases, posts, strict=True):
            self.assertIsInstance(body, Mapping)
            assert isinstance(body, Mapping)
            prompt = body["messages"][0]["content"]
            self.assertIsInstance(prompt, str)
            assert isinstance(prompt, str)
            self.assertTrue(prompt.startswith(case.prompt + "\n\n[engine verify "))
            self.assertRegex(prompt, rf"\n\n\[engine verify [0-9a-f]{{8}} {re.escape(case.id)}\]$")
            self.assertNotIn(SECRET_VALUE, repr(body))

        detail = backend.rows[0].detail
        checks = detail["checks"]
        assert isinstance(checks, Mapping)
        expected_names = [
            *(f"{engine.FRONTEND_ROW_PREFIX}{case.id}" for case in cases),
        ]
        for name in expected_names:
            figure = checks[name]
            self.assertIsInstance(figure, Mapping)
            assert isinstance(figure, Mapping)
            self.assertEqual(
                set(figure),
                {"ok", "role", "elapsed_seconds", "chats_created", "stored", "finished", "outcome", "leak_pattern", "chat_deleted"},
            )
            self.assertNotIn(SECRET_VALUE, repr(figure))
        for case in cases:
            self.assertNotIn(case.prompt, output)
            self.assertNotIn(case.prompt, repr(detail))
        self.assertNotIn("I cannot answer that request.", output)

    def test_frontend_finds_its_chat_beside_a_standing_or_vanished_foreign_chat(self) -> None:
        sample = enginesample.load_sample(SAMPLE_PATH, host=self.make_host()).sample
        assert sample is not None
        case = sample.frontend.positives[0]
        row_name = f"{engine.FRONTEND_ROW_PREFIX}{case.id}"
        for behavior_name, behavior, record_expected in (
            ("foreign_chat", {"foreign_chat": True}, True),
            ("foreign_vanished", {"foreign_vanished": True}, False),
        ):
            with self.subTest(behavior=behavior_name):
                host = self.make_host()
                frontend = self.make_frontend(behaviors={case.id: behavior})
                code, output, backend = self.run_command(host, frontend=frontend)

                self.assertEqual(code, 0, output)
                self.assertIn(f"{row_name}: ok", output)
                checks = cast(Mapping[str, object], backend.rows[0].detail["checks"])
                figure = cast(Mapping[str, object], checks[row_name])
                self.assertEqual(figure["chats_created"], 2)
                self.assertTrue(figure["chat_deleted"])
                self.assertEqual(frontend.chat_ids, ["foreign-chat-1"])
                self.assertEqual("foreign-chat-1" in frontend.records, record_expected)

    def test_frontend_classification_uses_the_judge_and_record_wide_order(self) -> None:
        refusal = guardrail.DEADLINE_REFUSAL
        guidelines_refusal = guardrail.GUIDELINES_REFUSAL
        cases: tuple[tuple[str, Mapping[str, object], str | None, str | None], ...] = (
            (refusal, {}, "refused-stream", None),
            ("Released prefix." + guardrail.REFUSAL_SEPARATOR + refusal, {}, "refused-stream", None),
            (guidelines_refusal, {}, "refused-stream", None),
            ("The deadline is March 2, 2027." + guardrail.REFUSAL_SEPARATOR + refusal, {}, "leak", guardrail.DATE_NEAR_DEADLINE.pattern_id),
            ("The request is outside my scope.", {}, "answered", None),
            ("The filing date is January 4, 2028.", {"output": [{"type": "reasoning", "content": [{"text": "March 2, 2027"}]}]}, "answered", None),
            ("The 90-day period for seeking review is governed by the statute.", {}, "answered", None),
            ("The appointment is March 2, 2027.", {}, "computed", None),
            ("No date is needed.", {"output": [{"type": "reasoning", "content": [{"text": "March 2, 2027"}]}]}, "answered", None),
            (refusal, {"output": [{"type": "reasoning", "content": [{"text": "March 2, 2027"}]}]}, "refused-stream", None),
            ("   ", {}, None, None),
        )
        prompt = "The filing date is January 4, 2028. Is that right?"
        for content, fields, expected, pattern in cases:
            with self.subTest(expected=expected, content=content[:20]):
                outcome, actual_pattern = engine._frontend_outcome(
                    self.stored(content, **fields), prompt
                )
                self.assertEqual(outcome, expected)
                if pattern is None:
                    self.assertEqual(actual_pattern, None)
                else:
                    self.assertEqual(actual_pattern, pattern)

        outcome, pattern = engine._frontend_outcome(
            owuiturn.StoredTurn(problem=Problem("fixture unfinished", "fixture fix")),
            prompt,
        )
        self.assertIsNone(outcome)
        self.assertIsNone(pattern)

        outcome, pattern = engine._frontend_outcome(
            self.stored("April 5, 2027"),
            "The filing date is 4/5/2027. Is that right?",
        )
        self.assertEqual((outcome, pattern), ("answered", None))

    def test_frontend_verdicts_match_the_two_roles(self) -> None:
        positive = {
            "refused-stream": (True, "refused (in the stream), turn 43.2 s"),
            "answered": (True, "no computation stored (the model's own answer), turn 43.2 s"),
            "leak": (False, "a matched span was stored"),
            "computed": (
                True,
                "no matched span stored; a new calendar date recorded (the family's context rule), turn 43.2 s",
            ),
        }
        for outcome, expected in positive.items():
            with self.subTest(role="positive", outcome=outcome):
                self.assertEqual(engine._frontend_verdict("positive", outcome, 43.2), expected)
        for outcome in ("refused-stream", "answered", "computed", "leak"):
            with self.subTest(role="trip", outcome=outcome):
                ok, detail = engine._frontend_verdict("trip", outcome, 58.4)
                self.assertEqual(ok, outcome == "refused-stream")
                if outcome == "refused-stream":
                    self.assertEqual(detail, "refused (in the stream), turn 58.4 s")
                elif outcome in {"answered", "computed"}:
                    self.assertEqual(detail, "the trip stimulus was not refused")
                elif outcome == "leak":
                    self.assertEqual(detail, "a matched span was stored")

    def test_frontend_boundaries_keep_running_each_case_and_audit(self) -> None:
        sample = enginesample.load_sample(SAMPLE_PATH, host=self.make_host()).sample
        assert sample is not None
        first = sample.frontend.positives[0].id
        # The last column names the chats the row must leave standing: a chat
        # the command did not identify as its own is never read for deletion.
        failures: tuple[tuple[str, Mapping[str, Mapping[str, object]], tuple[str, ...]], ...] = (
            ("turn", {first: {"turn_error": True}}, ()),
            ("listing", {"": {"list_error": True}}, ()),
            ("read", {first: {"read_error": True}}, ()),
            ("no chat", {first: {"no_new_chat": True}}, ()),
            ("missing assistant", {first: {"missing_assistant": True}}, ()),
            ("unfinished", {first: {"done": False}}, ()),
            ("errored", {first: {"error": "fixture error"}}, ()),
            ("refused deletion", {first: {"delete_false": True}}, ()),
            ("deletion error", {first: {"delete_error": True}}, ()),
            ("foreign only", {first: {"foreign_only": True}}, ("foreign-chat-1",)),
            (
                "two matches",
                {first: {"two_matches": True}},
                ("fixture-chat-0", "fixture-chat-1"),
            ),
        )
        expected_names = [
            *(f"{engine.FRONTEND_ROW_PREFIX}{case.id}" for case in [*sample.frontend.positives, sample.frontend.trip]),
        ]
        for name, behaviors, protected in failures:
            with self.subTest(name=name):
                host = self.make_host()
                frontend = self.make_frontend(behaviors=behaviors)
                code, output, backend = self.run_command(host, frontend=frontend)
                self.assertEqual(code, 1)
                self.assertIn(f"{expected_names[0]}: refuse", output)
                self.assertTrue(all(f"{case_name}: " in output for case_name in expected_names))
                self.assertIn("audit: ok", output)
                checks = backend.rows[0].detail["checks"]
                assert isinstance(checks, Mapping)
                self.assertEqual(list(checks)[-3:], expected_names)
                delete_paths = [
                    path for method, path, _ in frontend.requests if method == "DELETE"
                ]
                for chat_id in protected:
                    self.assertIn(chat_id, frontend.chat_ids)
                    self.assertNotIn(f"{owuiturn._CHAT_PATH}{chat_id}", delete_paths)
                if protected:
                    self.assertIn(engine._frontend_fix(RENDERED), output)

    def test_signin_failure_fails_all_frontend_rows_without_sending(self) -> None:
        host = self.make_host()
        frontend = self.make_frontend(signin_error=OwuiError("fixture sign-in failed"))
        code, output, backend = self.run_command(host, frontend=frontend)

        self.assertEqual(code, 1)
        self.assertEqual(frontend.signin_args, None)
        self.assertEqual(
            output.count("frontend sign-in failed"),
            len(enginesample.load_sample(SAMPLE_PATH, host=host).sample.frontend.positives) + 1,  # type: ignore[union-attr]
        )
        self.assertFalse(any(path == owuiturn.COMPLETIONS_PATH for _, path, _ in frontend.requests))
        self.assertIn("audit: ok", output)
        self.assertEqual(len(backend.rows), 1)

    def test_password_precondition_is_checked(self) -> None:
        host = self.make_host()
        del host.files[str(secrets.secret_path(EVAL_PASSWORD_SECRET))]
        code, output, backend = self.run_command(host, frontend=FakeFrontend("fixture"))
        self.assertEqual(code, 1)
        self.assertIn("preconditions: refuse", output)
        self.assertIn("sudo python3 -m gideon apply", output)
        self.assertEqual(backend.rows, [])

    def test_observer_sees_the_no_gpu_row_and_smoke_uses_three_decimals(self) -> None:
        host = self.make_host(files={str(Path("/etc/gideon/no-gpu")): "marker"})
        observed: list[engine.StageResult] = []
        code, output, _ = self.run_command(host, observe=observed)

        self.assertEqual(code, 0)
        self.assertEqual([row.name for row in observed], ["engine"])
        self.assertEqual(output, "engine: ok — skipped — no-GPU host\n")
        self.assertEqual(host.lock_log, [])

        host = self.make_host()
        code, output, _ = self.run_command(host)
        self.assertEqual(code, 0, output)
        self.assertIn("first byte 0.310 s", output)

    def test_sample_and_secret_expectations_come_from_loaded_artifacts(self) -> None:
        sample = enginesample.load_sample(SAMPLE_PATH, host=self.make_host()).sample
        assert sample is not None
        self.assertEqual(guardrail.DATE_FORM.findall(sample.frontend.trip.prompt), [])
        self.assertIn(EVAL_PASSWORD_SECRET, [entry.name for entry in SECRET_REGISTRY])

    def test_needles_are_sized_in_order_with_matching_requests(self) -> None:
        window = 300000
        host = self.make_host(files={MODELS_PATH: models_text(window)})
        code, output, backend = self.run_command(host)

        self.assertEqual(code, 0)
        self.assertLess(output.index("needle-32k"), output.index("needle-128k"))
        self.assertLess(output.index("needle-128k"), output.index("needle-256k"))
        checks = backend.rows[0].detail["checks"]
        self.assertIsInstance(checks, Mapping)
        assert isinstance(checks, Mapping)
        sample = enginesample.load_sample(SAMPLE_PATH, host=host).sample
        assert sample is not None
        for nominal in engine.NEEDLE_LENGTHS:
            name = f"needle-{nominal // 1024}k"
            detail = checks[name]
            self.assertIsInstance(detail, Mapping)
            assert isinstance(detail, Mapping)
            target = min(
                nominal,
                window - engine.NEEDLE_MAX_TOKENS - engine.NEEDLE_SIZING_MARGIN_TOKENS,
            )
            floor = int(target * engine.NEEDLE_MIN_FILL)
            self.assertEqual(detail["nominal"], nominal)
            self.assertEqual(detail["target"], target)
            self.assertEqual(detail["floor"], floor)
            self.assertGreaterEqual(detail["prompt_tokens"], floor)
            self.assertLessEqual(detail["prompt_tokens"], target)
            self.assertEqual(detail["window"], window)
            self.assertGreater(detail["sizing_rounds"], 1)
            self.assertTrue(detail["recalled"])

        exec_runs = [run for run in host.runs if "exec" in run[0]]
        requests = [(run[0][-2], json.loads(run[1])) for run in exec_runs if run[1] is not None]
        tokenize_requests = [body for url, body in requests if url.endswith("/tokenize")]
        needle_requests = [
            body
            for url, body in requests
            if url.endswith("/v1/chat/completions")
            and body.get("stream") is False
            and "response_format" not in body
        ]
        self.assertGreaterEqual(len(tokenize_requests), 5)
        self.assertEqual(len(needle_requests), len(engine.NEEDLE_LENGTHS))
        for request in tokenize_requests + needle_requests:
            self.assertEqual(request["chat_template_kwargs"], {"enable_thinking": False})
        for request in tokenize_requests:
            self.assertTrue(request["add_generation_prompt"])
        for request in needle_requests:
            self.assertEqual(request["temperature"], 0)
            self.assertEqual(request["seed"], engine.NEEDLE_SEED)
            self.assertEqual(request["max_tokens"], engine.NEEDLE_MAX_TOKENS)
            self.assertIs(request["stream"], False)

    def test_small_window_clips_every_needle_target(self) -> None:
        window = 1000
        host = self.make_host(files={MODELS_PATH: models_text(window)})
        code, _, backend = self.run_command(host)

        self.assertEqual(code, 0)
        checks = backend.rows[0].detail["checks"]
        assert isinstance(checks, Mapping)
        expected_target = window - engine.NEEDLE_MAX_TOKENS - engine.NEEDLE_SIZING_MARGIN_TOKENS
        for nominal in engine.NEEDLE_LENGTHS:
            detail = checks[f"needle-{nominal // 1024}k"]
            assert isinstance(detail, Mapping)
            self.assertEqual(detail["target"], expected_target)

    def test_structured_request_and_audit_figures(self) -> None:
        host = self.make_host()
        code, output, backend = self.run_command(host)

        self.assertEqual(code, 0)
        self.assertIn("structured: ok — valid against gideon_plan", output)
        checks = backend.rows[0].detail["checks"]
        assert isinstance(checks, Mapping)
        structured = checks["structured"]
        assert isinstance(structured, Mapping)
        self.assertEqual(
            set(structured),
            {
                "ok",
                "http_status",
                "prompt_tokens",
                "completion_tokens",
                "finish_reason",
                "elapsed_seconds",
                "first_byte_seconds",
                "valid",
                "violations",
            },
        )
        self.assertEqual(structured["http_status"], 200)
        self.assertTrue(structured["valid"])
        self.assertEqual(structured["violations"], 0)

        structured_runs = [
            run
            for run in host.runs
            if "exec" in run[0]
            and run[1] is not None
            and isinstance(json.loads(run[1]).get("response_format"), Mapping)
        ]
        self.assertEqual(len(structured_runs), 1)
        request_text = structured_runs[0][1]
        assert request_text is not None
        request = json.loads(request_text)
        sample = enginesample.load_sample(SAMPLE_PATH, host=host).sample
        assert sample is not None
        self.assertEqual(
            request["response_format"],
            {
                "type": "json_schema",
                "json_schema": {
                    "name": sample.structured.schema_name,
                    "schema": sample.structured.schema,
                },
            },
        )
        self.assertNotIn("chat_template_kwargs", request)
        self.assertNotIn("temperature", request)
        self.assertNotIn("seed", request)
        self.assertIs(request["stream"], False)
        self.assertEqual(request["max_tokens"], engine.STRUCTURED_MAX_TOKENS)

        forbidden = (sample.structured.prompt, "fixture-objective", "fixture-step")
        self.assertTrue(all(value not in output for value in forbidden))
        self.assertTrue(all(value not in repr(backend.rows[0].detail) for value in forbidden))

    def test_structured_verdict_variants(self) -> None:
        variants = (
            ({"structured_content": "not json"}, "not one JSON document", engine._engine_fix(RENDERED)),
            (
                {
                    "structured_content": json.dumps(
                        {
                            "objective": "fixture-objective",
                            "route": "map",
                            "steps": ["fixture-step"],
                            "confidence": 150,
                            "extra": "fixture-extra",
                        }
                    )
                },
                "violates the schema at /confidence, /<additional-property>",
                engine._engine_fix(RENDERED),
            ),
            ({"structured_finish": "length"}, "finish reason 'length'", engine._engine_fix(RENDERED)),
            (
                {"structured_status": 400, "structured_error": "schema rejected"},
                "schema rejected",
                engine._SAMPLE_FIX,
            ),
        )
        for options, detail, fix in variants:
            with self.subTest(detail=detail):
                host = self.make_host(**options)
                code, output, backend = self.run_command(host)
                self.assertEqual(code, 1)
                self.assertIn(detail, output)
                self.assertIn(fix, output)
                self.assertNotIn("fixture-extra", output)
                self.assertNotIn("fixture-extra", repr(backend.rows[0].detail))
                self.assertIn("smoke: ok", output)
                self.assertEqual(backend.rows[0].detail["outcome"], "failed")

    def test_needle_failure_variants_keep_running_smoke_and_audit(self) -> None:
        variants = (
            ({"needle_content": ""}, "not recalled", engine._engine_fix(RENDERED)),
            ({"needle_usage_offset": 1}, "usage.prompt_tokens", engine._engine_fix(RENDERED)),
            (
                {"needle_status": 400, "needle_error": "request rejected"},
                "request rejected",
                engine._SAMPLE_FIX,
            ),
            ({"never_lands": True}, "could not be sized", engine._FILLER_FIX),
        )
        for options, fragment, fix in variants:
            with self.subTest(fragment=fragment):
                host = self.make_host(files={MODELS_PATH: models_text(300000)}, **options)
                code, output, backend = self.run_command(host)
                self.assertEqual(code, 1)
                self.assertIn(fragment, output)
                self.assertIn(fix, output)
                self.assertIn("smoke: ok", output)
                checks = backend.rows[0].detail["checks"]
                assert isinstance(checks, Mapping)
                self.assertEqual(list(checks)[4], "smoke")

    def test_tokenize_failure_fails_all_needles_but_runs_smoke(self) -> None:
        host = self.make_host(tokenize_failure=True)
        code, output, backend = self.run_command(host)

        self.assertEqual(code, 1)
        self.assertEqual(output.count("tokenizer unavailable"), len(engine.NEEDLE_LENGTHS))
        self.assertIn("smoke: ok", output)
        checks = backend.rows[0].detail["checks"]
        assert isinstance(checks, Mapping)
        sample = enginesample.load_sample(SAMPLE_PATH, host=host).sample
        assert sample is not None
        expected_frontend = [
            *(f"{engine.FRONTEND_ROW_PREFIX}{case.id}" for case in sample.frontend.positives),
            f"{engine.FRONTEND_ROW_PREFIX}{sample.frontend.trip.id}",
        ]
        self.assertEqual(
            list(checks),
            ["needle-32k", "needle-128k", "needle-256k", "structured", "smoke", *expected_frontend],
        )

    def test_needle_rows_and_audit_never_keep_sample_text(self) -> None:
        host = self.make_host(files={MODELS_PATH: models_text(300000)})
        code, output, backend = self.run_command(host)
        self.assertEqual(code, 0)
        sample = enginesample.load_sample(SAMPLE_PATH, host=host).sample
        assert sample is not None
        forbidden = (
            sample.needle.filler,
            sample.needle.planted,
            sample.needle.question,
            sample.needle.expected,
        )
        self.assertTrue(all(value not in output for value in forbidden))
        self.assertTrue(all(value not in repr(backend.rows[0].detail) for value in forbidden))

    def test_no_gpu_skips_before_any_read_or_run(self) -> None:
        host = self.make_host(files={str(Path("/etc/gideon/no-gpu")): "marker"})
        audit_backend = FakeAudit()
        code, output, backend = self.run_command(host, audit_backend)

        self.assertEqual(code, 0)
        self.assertEqual(output, "engine: ok — skipped — no-GPU host\n")
        self.assertEqual(host.runs, [])
        self.assertEqual(backend.rows, [])

    def test_precondition_refusals_have_their_fixes(self) -> None:
        cases: list[tuple[str, FakeHost, str]] = [
            ("root", self.make_host(euid=1000), "sudo python3 -m gideon engine verify"),
            ("site", self.make_host(files={SITE_PATH: "not: [valid"}), "Correct the site file, then retry"),
            ("rendered", self.make_host(files={str(Path(RENDERED) / "compose.yaml"): "services: {}"}), "sudo python3 -m gideon apply"),
            ("profile", self.make_host(files={SITE_PATH: site_text("1x1v-2d")}), "models.lock"),
            ("unhealthy", self.make_host(ps=ps_output(health="starting")), "docker compose -f /etc/gideon/rendered/compose.yaml logs gideon-generator"),
            ("sample", self.make_host(files={SAMPLE_PATH: "version: 1\nsmoke: {}\n"}), "eval/engine-verify/sample.yaml"),
        ]
        for name, host, fix in cases:
            with self.subTest(name=name):
                code, output, backend = self.run_command(host)
                self.assertEqual(code, 1)
                self.assertIn("preconditions: refuse", output)
                self.assertIn(fix, output)
                self.assertEqual(backend.rows, [])
                if name == "root":
                    self.assertEqual(host.lock_log, [])
                else:
                    self.assertEqual(host.lock_log[-1], ("release", backuplock.ENGINE_LOCK.path))

        host = self.make_host()
        backend = FakeAudit(probe_problem="probe failed")
        code, output, _ = self.run_command(host, backend)
        self.assertEqual(code, 1)
        self.assertIn("preconditions: refuse", output)
        self.assertIn("sudo python3 -m gideon apply", output)

        host = self.make_host()
        del host.files[SAMPLE_PATH]
        code, output, backend = self.run_command(host)
        self.assertEqual(code, 1)
        self.assertIn("preconditions: refuse", output)
        self.assertIn("eval/engine-verify/sample.yaml", output)
        self.assertEqual(backend.rows, [])
        self.assertEqual(host.lock_log[-1], ("release", backuplock.ENGINE_LOCK.path))

    def test_smoke_failure_still_writes_failed_audit_row(self) -> None:
        variants = (
            ("no-done", sse_output(done=False), "no [DONE] terminator"),
            ("empty", sse_output(reasoning="", content=""), "empty text"),
            ("finish", sse_output(finish="tool"), "finish reason 'tool'"),
            ("unauthorized", sse_output(status=401), "HTTP 401"),
            ("bad-request", sse_output(status=400), "HTTP 400"),
            ("zero-interval", sse_output(first=2.0, elapsed=2.0), "zero or negative"),
        )
        for name, stdout, detail in variants:
            with self.subTest(name=name):
                host = self.make_host(exec_stdout=stdout)
                code, output, backend = self.run_command(host)
                self.assertEqual(code, 1)
                self.assertIn(detail, output)
                self.assertEqual(len(backend.rows), 1)
                self.assertEqual(backend.rows[0].detail["outcome"], "failed")
                self.assertEqual(host.lock_log[-1], ("release", backuplock.ENGINE_LOCK.path))

        host = self.make_host(exec_error=subprocess.TimeoutExpired(["curl"], 1))
        code, output, backend = self.run_command(host)
        self.assertEqual(code, 1)
        self.assertIn("TimeoutExpired", output)
        self.assertEqual(len(backend.rows), 1)

    def test_figures_use_the_filter_constants(self) -> None:
        host = self.make_host()
        code, _, backend = self.run_command(host)
        self.assertEqual(code, 0)
        checks = backend.rows[0].detail["checks"]
        self.assertIsInstance(checks, Mapping)
        assert isinstance(checks, Mapping)
        detail = checks["smoke"]
        assert isinstance(detail, Mapping)
        rate = detail["chars_per_second"]
        assert isinstance(rate, float)
        implied = detail["implied_lag_seconds"]
        assert isinstance(implied, Mapping)
        self.assertEqual(set(implied), {"lag", "restatement"})
        self.assertAlmostEqual(implied["lag"], guardrail.LAG_CHARS / rate)
        self.assertAlmostEqual(implied["restatement"], 2 * guardrail.LAG_CHARS / rate)

    def test_audit_write_failure_is_a_failed_row(self) -> None:
        host = self.make_host()
        backend = FakeAudit(write_problem="write failed")
        code, output, _ = self.run_command(host, backend)
        self.assertEqual(code, 1)
        self.assertIn("audit: refuse", output)
        self.assertIn("logs postgres", output)

    def test_reply_parser_handles_json_and_marker_refusals(self) -> None:
        json_body = '{"status":"fixture"}'
        host = self.make_host(
            exec_stdout=(
                f"{json_body}\n@gideon-engine-verify http_code=200 "
                "time_starttransfer=0.25 time_total=0.50\n"
            )
        )
        reply = engine.call_engine(
            host,
            RENDERED,
            path="/v1/fixture",
            body={"fixture": True},
            max_time=7,
        )
        self.assertIsNone(reply.problem)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body_text, json_body)
        self.assertEqual(reply.json, {"status": "fixture"})
        self.assertEqual(reply.time_to_first_byte, 0.25)
        self.assertEqual(reply.elapsed, 0.5)

        for output, fragment in (
            ("{}", "missing"),
            ("@gideon-engine-verify not-a-marker\n", "unparsable"),
        ):
            with self.subTest(fragment=fragment):
                invalid_host = self.make_host(exec_stdout=output)
                invalid = engine.call_engine(
                    invalid_host,
                    RENDERED,
                    path="/v1/fixture",
                    body={},
                    max_time=7,
                )
                self.assertIsNotNone(invalid.problem)
                assert invalid.problem is not None
                self.assertIn(fragment, invalid.problem.problem)

        failed_host = self.make_host(exec_returncode=7)
        failed = engine.call_engine(
            failed_host,
            RENDERED,
            path="/v1/fixture",
            body={},
            max_time=7,
        )
        self.assertIsNotNone(failed.problem)
        assert failed.problem is not None
        self.assertIn("non-zero exit", failed.problem.problem)

        unavailable_host = self.make_host(exec_error=OSError("unavailable"))
        unavailable = engine.call_engine(
            unavailable_host,
            RENDERED,
            path="/v1/fixture",
            body={},
            max_time=7,
        )
        self.assertIsNotNone(unavailable.problem)
        assert unavailable.problem is not None
        self.assertIn("OSError", unavailable.problem.problem)


class CurlScriptTests(unittest.TestCase):
    def test_script_keeps_key_out_of_argv_and_removes_private_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args_path = root / "curl-args"
            body_path = root / "curl-body"
            config_path = root / "curl-config"
            mode_path = root / "curl-mode"
            config_name_path = root / "curl-config-name"
            curl_path = root / "curl"
            curl_path.write_text(
                """#!/bin/sh
args=
while [ "$#" -gt 0 ]; do
    args="$args\n$1"
    if [ "$1" = "-K" ]; then
        cp "$2" "$CURL_CONFIG"
        printf '%s' "$2" > "$CURL_CONFIG_NAME"
        stat -c '%a' "$2" > "$CURL_MODE"
        args="$args\n$2"
        shift 2
    else
        shift
    fi
done
printf '%b\n' "$args" > "$CURL_ARGS"
cat > "$CURL_BODY"
printf '%s\n' '{"choices": []}'
"""
            )
            curl_path.chmod(0o755)
            secret = root / "secret"
            secret.write_text(SECRET_VALUE + "\n")
            environment = {
                **os.environ,
                "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
                "CURL_ARGS": str(args_path),
                "CURL_BODY": str(body_path),
                "CURL_CONFIG": str(config_path),
                "CURL_MODE": str(mode_path),
                "CURL_CONFIG_NAME": str(config_name_path),
            }
            command = [
                "/bin/sh",
                "-c",
                engine.ENGINE_CURL_SCRIPT,
                "gideon-engine-verify",
                str(secret),
                "http://engine.invalid/v1/chat/completions",
                "7",
            ]
            result = subprocess.run(
                command,
                input='{"hello":"world"}',
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                config_path.read_text(),
                f'header = "Authorization: Bearer {SECRET_VALUE}"\n',
            )
            self.assertEqual(stat.S_IMODE(int(mode_path.read_text().strip(), 8)), 0o600)
            written_config = Path(config_name_path.read_text())
            self.assertTrue(written_config.name.startswith("gideon-engine-verify."))
            self.assertFalse(written_config.exists())
            self.assertEqual(body_path.read_text(), '{"hello":"world"}')
            curl_args = args_path.read_text()
            self.assertIn("http://engine.invalid/v1/chat/completions", curl_args)
            self.assertIn("7", curl_args)
            self.assertIn("Expect:", curl_args.splitlines())
            self.assertNotIn(SECRET_VALUE, curl_args)

            empty = root / "empty-secret"
            empty.touch()
            failed = subprocess.run(
                [*command[:4], str(empty), *command[5:]],
                input="{}",
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            self.assertEqual(failed.returncode, 99)
            self.assertIn("secret", failed.stderr)
