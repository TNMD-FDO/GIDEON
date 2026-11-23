"""Contracts for drafting expected answers from fictitious graded passages."""

import ast
import hashlib
import json
import sys
from pathlib import Path
from subprocess import TimeoutExpired
from unittest import TestCase
from unittest.mock import patch

from test_index_record import FakeHost
from test_judge import engine_output

from gideon.evaluation import judge, judgments, sampling, signoff_draft
from gideon.host import engine
from gideon.host.render.engine import GENERATOR

QUESTION = "What did the fictitious orchard board decide?"
PASSAGES = (
    ("Invented First Opinion", "The fictitious panel ordered a new hearing."),
    ("Invented Second Opinion", "The fictitious panel required written notice."),
)
ANSWER = "The imagined orchard board must provide notice and hold a new hearing."
CONTENT = json.dumps({"answer": ANSWER, "passages": [2, 1]})
RENDERED = "/tmp/fictitious-signoff-rendered"
MODEL = "fixture-signoff-model"
SECONDS = 7.25


def _draft(host: FakeHost, *, seed: int = sampling.DEFAULT_SEED) -> signoff_draft.Drafted | signoff_draft.DraftFailure:
    ticks = iter((100.0, 100.0 + SECONDS))
    return signoff_draft.draft(
        host, RENDERED, served_model_name=MODEL, question=QUESTION, passages=PASSAGES,
        seed=seed, monotonic=ticks.__next__,
    )


class SignoffPrompt(TestCase):
    """The versioned prompt and structured generator request for an expected-answer sign-off."""

    def test_prompt_id_and_pinned_digest(self) -> None:
        self.assertEqual(tuple(signoff_draft.PROMPTS), ("signoff-draft@1", "signoff-draft@2"))
        self.assertEqual(
            signoff_draft.PROMPT_DIGESTS,
            {key: judge.prompt_digest(prompt) for key, prompt in signoff_draft.PROMPTS.items()},
        )
        self.assertEqual(signoff_draft.PROMPT_ID, "signoff-draft@2")
        prompt = signoff_draft.PROMPTS[signoff_draft.PROMPT_ID]
        self.assertEqual(prompt.id, signoff_draft.PROMPT_ID)
        self.assertEqual(prompt.slots, ("question", "passages", "count"))

    def test_request_shape_and_valid_reply(self) -> None:
        host = FakeHost(stdout=engine_output(CONTENT, prompt_tokens=41, completion_tokens=19))
        with patch.object(engine, "call_model_server", wraps=engine.call_model_server) as call:
            result = _draft(host)
        self.assertEqual(call.call_args.args, (host, RENDERED, GENERATOR))
        self.assertEqual(call.call_args.kwargs["path"], "/v1/chat/completions")
        self.assertEqual(call.call_args.kwargs["max_time"], signoff_draft.DRAFT_TIMEOUT_SECONDS)
        (_, payload), = host.calls
        assert payload is not None
        prompt = signoff_draft.PROMPTS[signoff_draft.PROMPT_ID]
        shown = json.loads(json.dumps(prompt.schema))
        shown["properties"]["passages"]["items"] = {"type": "integer", "enum": [1, 2]}
        sent = json.loads(payload)
        self.assertEqual(sent, {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user_template.format(
                    question=QUESTION, passages="1. Invented First Opinion\n"
                    "The fictitious panel ordered a new hearing.\n\n2. Invented Second Opinion\n"
                    "The fictitious panel required written notice.", count=2,
                )},
            ],
            **sampling.THINKING_SAMPLING,
            "seed": sampling.DEFAULT_SEED,
            "stream": False,
            "max_tokens": signoff_draft.DRAFT_MAX_TOKENS,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": prompt.schema_name, "schema": shown},
            },
            "chat_template_kwargs": {"enable_thinking": True},
        })
        self.assertNotEqual(sent["temperature"], 0)
        self.assertIn("numbered 1 to 2", sent["messages"][1]["content"])
        self.assertEqual(result, signoff_draft.Drafted(ANSWER, (2, 1), 41, 19, SECONDS))
        self.assertNotIn(ANSWER, repr(result))

    def test_explicit_seed_reaches_the_wire(self) -> None:
        host = FakeHost(stdout=engine_output(CONTENT))
        seed = sampling.DEFAULT_SEED + 371
        _draft(host, seed=seed)
        (_, payload), = host.calls
        self.assertIsNotNone(payload)
        self.assertEqual(json.loads(payload or "")["seed"], seed)


    def test_frozen_proof_imports_the_sampling_names(self) -> None:
        self.assertIs(signoff_draft.THINKING_SAMPLING, sampling.THINKING_SAMPLING)
        self.assertEqual(signoff_draft.DEFAULT_SEED, sampling.DEFAULT_SEED)
        self.assertIs(signoff_draft.sampling_differences, sampling.sampling_differences)


class CandidateRanking(TestCase):
    """Primary grades define the eligible passages for an unsigned answer."""

    def test_floor_primary_and_every_sort_key(self) -> None:
        sha256 = hashlib.sha256(b"Imaginary canonical opinion").hexdigest()
        rows = (
            ("example:b", 4, 8, 3, "primary"),
            ("example:a", 1, 5, 2, "primary"),
            ("example:a", 4, 8, 3, "primary"),
            ("example:a", 2, 7, 3, "primary"),
            ("example:a", 2, 6, 3, "primary"),
            ("example:a", 0, 1, 3, "second"),
            ("example:a", 0, 2, 1, "primary"),
        )
        records = [
            judgments.Judgment("judgments-901", source, sha256, start, end, grade, "CHU-attorney-1", assessment)
            for source, start, end, grade, assessment in rows
        ]
        self.assertEqual(signoff_draft.candidates(records), tuple(
            signoff_draft.Candidate(source, sha256, start, end, grade)
            for source, start, end, grade, _ in (rows[index] for index in (4, 3, 2, 0, 1))
        ))

    def test_candidates_leaves_the_cap_to_the_caller(self) -> None:
        count = signoff_draft.CANDIDATE_LIMIT + 2
        sha256 = hashlib.sha256(b"Another imaginary canonical opinion").hexdigest()
        records = (
            judgments.Judgment("judgments-901", "example:a", sha256, start, start + 1, 2, "CHU-attorney-1", "primary")
            for start in reversed(range(count))
        )
        self.assertEqual(tuple(candidate.start for candidate in signoff_draft.candidates(records)), tuple(range(count)))


class ReplyFailures(TestCase):
    """Every failure carries a closed code and content-free request evidence."""

    def test_request_failures_use_injected_time(self) -> None:
        cases = (
            ("seam expiry", FakeHost(error=TimeoutExpired(["fictitious-curl"], 20)), "timed-out", "TimeoutExpired"),
            ("curl timeout with diagnostic", FakeHost(code=28, stderr="curl: (28) fictitious time limit"), "timed-out", "non-zero exit 28 (curl: (28) fictitious time limit)"),
            ("curl timeout without diagnostic", FakeHost(code=28), "timed-out", "non-zero exit 28"),
            ("cannot start", FakeHost(error=OSError(ANSWER)), "request-failed", "OSError"),
            ("different exit", FakeHost(code=7, stderr="fictitious transport error"), "request-failed", "non-zero exit 7 (fictitious transport error)"),
        )
        for label, host, code, problem in cases:
            with self.subTest(label=label):
                result = _draft(host)
                assert isinstance(result, signoff_draft.DraftFailure)
                self.assertEqual(result.code, code)
                self.assertEqual(result.elapsed_seconds, SECONDS)
                self.assertEqual(result.problem, f"{GENERATOR.words} request failed: {problem}")
                self.assertEqual((result.status, result.prompt_tokens, result.completion_tokens), (None, None, None))
                self.assertNotIn(ANSWER, repr(result))

    def test_http_error_keeps_status_with_or_without_body_problem(self) -> None:
        _, marker = engine_output(CONTENT, status=503).split("\n", 1)
        for body, has_problem in ((json.dumps({"error": ANSWER}), False), (ANSWER, True)):
            with self.subTest(has_problem=has_problem):
                result = _draft(FakeHost(stdout=f"{body}\n{marker}"))
                assert isinstance(result, signoff_draft.DraftFailure)
                self.assertEqual((result.code, result.status, result.elapsed_seconds), ("http-status", 503, SECONDS))
                if has_problem:
                    self.assertEqual(result.problem, f"{GENERATOR.words} response body is not valid JSON")
                else:
                    self.assertIsNone(result.problem)
                self.assertNotIn(ANSWER, repr(result))

    def test_reserve_exhaustion_keeps_completion_tokens(self) -> None:
        result = _draft(FakeHost(stdout=engine_output(
            None, finish_reason="length", completion_tokens=signoff_draft.DRAFT_MAX_TOKENS,
        )))
        assert isinstance(result, signoff_draft.DraftFailure)
        self.assertEqual(result.code, "truncated")
        self.assertEqual(result.completion_tokens, signoff_draft.DRAFT_MAX_TOKENS)
        self.assertEqual(result.elapsed_seconds, SECONDS)

    def test_schema_range_and_uniqueness_refuse_without_reply_text(self) -> None:
        malformed = (
            ("bad JSON", "off-schema", "{ " + ANSWER),
            ("extra key", "off-schema", json.dumps({"answer": ANSWER, "passages": [1], "extra": ANSWER})),
            ("empty answer", "off-schema", json.dumps({"answer": "", "passages": [1]})),
            ("too many picks", "off-schema", json.dumps({
                "answer": ANSWER, "passages": [1] * (signoff_draft.AUTHORITY_LIMIT + 1),
            })),
            ("below range", "out-of-range", json.dumps({"answer": ANSWER, "passages": [0]})),
            ("above range", "out-of-range", json.dumps({"answer": ANSWER, "passages": [len(PASSAGES) + 1]})),
            ("repeated", "repeated", json.dumps({"answer": ANSWER, "passages": [2, 2]})),
        )
        for label, code, content in malformed:
            with self.subTest(label=label):
                result = _draft(FakeHost(stdout=engine_output(content)))
                assert isinstance(result, signoff_draft.DraftFailure)
                self.assertEqual(result.code, code)
                self.assertNotIn(ANSWER, repr(result))


class Imports(TestCase):
    def test_imports_are_standard_library_or_gideon(self) -> None:
        tree = ast.parse(Path(signoff_draft.__file__).read_text(encoding="utf-8"))
        allowed = sys.stdlib_module_names | {"gideon"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for name in node.names:
                    self.assertIn(name.name.split(".")[0], allowed)
            if isinstance(node, ast.ImportFrom):
                self.assertIn((node.module or "").split(".")[0], allowed)
