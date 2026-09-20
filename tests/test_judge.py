"""The reference-guided judge over a stub engine (spec §18.4)."""

import ast
import json
import subprocess
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from gideon.evaluation import judge, record, results
from gideon.host import engine, stack
from gideon.host.render.engine import (
    ENGINE_PORT,
    ENGINE_SECRET_NAME,
    ENGINE_SERVICE_NAME,
)
from gideon.host.sysio import Command, PathLike

RENDERED = "/rendered"
PROMPT = judge.PROMPT_REGISTRY["synthesis@1"]


def engine_output(
    content: str | None,
    *,
    status: int = 200,
    finish_reason: str | None = "stop",
    prompt_tokens: int = 17,
    completion_tokens: int = 9,
    elapsed: float = 2.5,
) -> str:
    response = {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {} if content is None else {"content": content},
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }
    return (
        json.dumps(response)
        + f"\n@gideon-engine-verify http_code={status} "
        f"time_starttransfer=0.20 time_total={elapsed:.2f}\n"
    )


class StubHost:
    """Host seam returning one canned non-streaming engine response."""

    def __init__(self, stdout: str, *, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.argv: tuple[str, ...] | None = None
        self.input: str | None = None
        self.timeout: float | None = None

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
        del check, cwd, env, passthrough
        self.argv = tuple(argv)
        self.input = input
        self.timeout = timeout
        return subprocess.CompletedProcess(self.argv, self.returncode, self.stdout, "")


def valid_content(
    *,
    score: int = 3,
    reason: str = "The answer is supported.",
    modes: tuple[str, ...] = (),
) -> str:
    """One on-schema verdict document. Shared with the runner and tripwire tests."""

    return json.dumps(
        {
            "score": score,
            "reason": reason,
            "failure_modes": list(modes),
        }
    )


def run_grade(host: StubHost) -> judge.Grading:
    return judge.grade(
        cast(Any, host),
        RENDERED,
        served_model_name="fixture-model",
        prompt=PROMPT,
        question="What is the answer?",
        reference="The reference answer.",
        candidate="The candidate answer.",
    )


class Request(unittest.TestCase):
    """The request preserves the prompt contract and the secret boundary."""

    def test_request_contains_prompt_schema_slots_and_thinking_flag(self) -> None:
        host = StubHost(engine_output(valid_content()))
        grading = run_grade(host)
        self.assertIsNotNone(grading.verdict)
        assert host.input is not None
        body = json.loads(host.input)
        self.assertEqual(body["model"], "fixture-model")
        self.assertEqual(body["temperature"], 0)
        self.assertIs(body["stream"], False)
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": True})
        self.assertNotIn("reasoning_effort", body)
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertEqual(body["response_format"]["json_schema"]["name"], PROMPT.schema_name)
        self.assertEqual(body["response_format"]["json_schema"]["schema"], PROMPT.schema)
        messages = body["messages"]
        self.assertEqual(messages[0], {"role": "system", "content": PROMPT.system})
        user = messages[1]["content"]
        self.assertIn("<question>\nWhat is the answer?", user)
        self.assertIn("<reference-answer>\nThe reference answer.", user)
        self.assertIn("<candidate-answer>\nThe candidate answer.", user)

    def test_exec_argv_names_the_secret_path_and_nothing_more(self) -> None:
        host = StubHost(engine_output(valid_content()))
        run_grade(host)
        expected = tuple(
            stack.exec_argv(
                RENDERED,
                ENGINE_SERVICE_NAME,
                "sh",
                "-c",
                engine.ENGINE_CURL_SCRIPT,
                "gideon-engine-verify",
                f"/run/secrets/{ENGINE_SECRET_NAME}",
                f"http://{ENGINE_SERVICE_NAME}:{ENGINE_PORT}/v1/chat/completions",
                str(judge.JUDGE_TIMEOUT_SECONDS),
            )
        )
        # The whole argv is pinned, so the secret's path is the only thing about
        # the key that can appear: no element holds a value read from the file.
        self.assertEqual(host.argv, expected)

    def test_module_ast_has_no_secret_import_or_read(self) -> None:
        tree = ast.parse(Path(judge.__file__).read_text())
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            f"{node.module}.{alias.name}"
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if node.module is not None
        )
        self.assertNotIn("gideon.host.secrets", imported)
        self.assertFalse(any(name.endswith(".read_secret") for name in imported))
        calls = [
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        self.assertNotIn("read_secret", calls)


class Failures(unittest.TestCase):
    """Every engine or verdict refusal is a scoreless, content-free grading."""

    def test_every_engine_failure_has_code_and_no_candidate_text(self) -> None:
        candidate = "candidate text that must never enter a failure detail"
        cases = (
            ("request-failed", StubHost("", returncode=7)),
            ("http-status", StubHost(engine_output(valid_content(), status=503))),
            ("finish-reason", StubHost(engine_output(valid_content(), finish_reason="length"))),
            ("no-content", StubHost(engine_output(None))),
            # The plan's named risk: the whole reserve spent inside the reasoning
            # block returns no content AND finish reason 'length'. That is an
            # unfinished reply, never a missing field.
            (
                "finish-reason",
                StubHost(engine_output(None, finish_reason="length")),
            ),
            ("invalid-json", StubHost(engine_output("{} trailing"))),
            (
                "schema-violation",
                StubHost(engine_output(json.dumps({"score": 9, "reason": "x", "failure_modes": []}))),
            ),
            (
                "empty-reason",
                StubHost(engine_output(valid_content(reason=" \n\t"))),
            ),
        )
        for expected_code, host in cases:
            with self.subTest(code=expected_code):
                grading = judge.grade(
                    cast(Any, host),
                    RENDERED,
                    served_model_name="fixture-model",
                    prompt=PROMPT,
                    question="question",
                    reference="reference",
                    candidate=candidate,
                )
                self.assertIsNone(grading.verdict)
                self.assertIsNotNone(grading.failure)
                assert grading.failure is not None
                self.assertEqual(grading.failure.code, expected_code)
                self.assertNotIn(candidate, grading.failure.detail)
                self.assertNotIn("score", judge.render_judge(grading))

    def test_schema_failure_names_key_or_index_without_reply_text(self) -> None:
        content = json.dumps(
            {"score": 3, "reason": "fine", "failure_modes": ["not-a-mode"]}
        )
        grading = run_grade(StubHost(engine_output(content)))
        assert grading.failure is not None
        self.assertIn("/failure_modes/0", grading.failure.detail)
        self.assertNotIn("not-a-mode", grading.failure.detail)


class Normalisation(unittest.TestCase):
    """Verdict text and modes are normalised only after schema validation."""

    def test_reason_whitespace_and_repeated_modes_collapse_in_order(self) -> None:
        content = valid_content(
            reason="  first\n\tsecond   third  ",
            modes=("off-question", "unsupported-claim", "off-question"),
        )
        grading = run_grade(StubHost(engine_output(content)))
        self.assertEqual(
            grading.verdict,
            judge.Verdict(
                3,
                "first second third",
                ("off-question", "unsupported-claim"),
            ),
        )
        field = judge.render_judge(grading, band=(2, 3))
        self.assertEqual(field["in_band"], True)

    def test_in_band_reads_the_whole_inclusive_range(self) -> None:
        """A band is a low and a high, so its interior counts as inside."""

        for score, band, inside in (
            (2, (1, 3), True),
            (1, (1, 3), True),
            (3, (1, 3), True),
            (0, (1, 3), False),
            (3, (0, 2), False),
        ):
            with self.subTest(score=score, band=band):
                grading = judge.Grading(
                    PROMPT.id, verdict=judge.Verdict(score, "a reason", ())
                )
                field = judge.render_judge(grading, band=band)
                self.assertEqual(field["band"], [band[0], band[1]])
                self.assertEqual(field["in_band"], inside)

    def test_unfinished_reply_names_the_token_reserve(self) -> None:
        grading = run_grade(StubHost(engine_output(None, finish_reason="length")))
        assert grading.failure is not None
        self.assertEqual(grading.failure.code, "finish-reason")
        self.assertIn(str(judge.JUDGE_MAX_TOKENS), grading.failure.detail)
        self.assertIn("token reserve", grading.failure.detail)

    def test_result_reprs_redact_the_reason(self) -> None:
        """A judge mapping reaches two dataclasses whose generated reprs would
        print the reason that Verdict redacts."""

        reason = "Distinctive fixture reason that no repr may print."
        grading = run_grade(StubHost(engine_output(valid_content(reason=reason))))
        field = judge.render_judge(grading, band=(3, 3))
        self.assertIn(reason, json.dumps(field))
        case_judge = cast(Mapping[str, results.JSONValue], field)
        case = results.CaseResult("judge-001", 1, "pass", {}, judge=case_judge, latency_ms=1.0)
        row = record.ResultRow(
            run_id="11111111-2222-4333-8444-555555555555",
            run_started_at=datetime(2026, 9, 19, tzinfo=UTC),
            case_id="judge-001",
            repeat=1,
            verdict="pass",
            metrics={},
            judge=field,
            provenance_ref=None,
            latency_ms=1.0,
        )
        slice_result = results.SliceResult(True, "report", (case,))
        for value in (case, row, slice_result):
            with self.subTest(value=type(value).__name__):
                self.assertNotIn(reason, repr(value))
                self.assertIn("<redacted>", repr(value))

    def test_grading_repr_redacts_the_reason(self) -> None:
        reason = "private model explanation"
        grading = run_grade(StubHost(engine_output(valid_content(reason=reason))))
        self.assertNotIn(reason, repr(grading))
        self.assertIn("<redacted>", repr(grading))


class Registry(unittest.TestCase):
    """Prompt ids and their pinned content digests are versioned artifacts."""

    def test_ids_are_unique_versioned_and_digest_pinned(self) -> None:
        ids = tuple(judge.PROMPT_REGISTRY)
        self.assertEqual(len(ids), len(set(ids)))
        for prompt_id in ids:
            with self.subTest(prompt_id=prompt_id):
                name, separator, version = prompt_id.rpartition("@")
                self.assertTrue(name)
                self.assertEqual(separator, "@")
                self.assertTrue(version.isdigit())
                self.assertGreater(int(version), 0)
                self.assertEqual(judge.PROMPT_DIGESTS[prompt_id], judge.prompt_digest(judge.PROMPT_REGISTRY[prompt_id]))


if __name__ == "__main__":
    unittest.main()
