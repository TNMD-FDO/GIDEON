"""Paragraph numbering and checked-later section draft contracts."""

import ast
import json
import string
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from gideon.evaluation import judge, segmenter_draft
from gideon.host import engine
from gideon.host.render.engine import GENERATOR
from gideon.host.render.worker import SECTION_TYPES
from gideon.host.report import Problem
from gideon.host.sysio import Host
from gideon.worker import sections


def _reply(
    sections_value: object = None, *, finish: str = "stop", status: int = 200,
    problem: Problem | None = None, content: str | None = None,
) -> engine.EngineReply:
    if content is None:
        content = json.dumps({"sections": sections_value if sections_value is not None else [
            {"paragraph": 0, "type": "majority"},
        ]})
    document = {
        "choices": [{"finish_reason": finish, "message": {"content": content}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }
    return engine.EngineReply(status, json.dumps(document), document, (), True, None, 1.5, problem)


def _draft(reply: engine.EngineReply, text: str = "Fictitious opinion paragraph") -> segmenter_draft.Draft:
    with patch.object(engine, "call_model_server", return_value=reply):
        return segmenter_draft.draft(
            cast(Host, object()), "/tmp/fictitious-render", served_model_name="fixture-model", text=text,
        )


class Paragraphs(unittest.TestCase):
    """The host's paragraph offsets match the worker's split rule."""

    def test_table_matches_worker_for_blank_and_trailing_runs(self) -> None:
        fixtures = (
            "One paragraph",
            "\n\n  First paragraph\n\nSecond paragraph",
            "First\n\n\n\nSecond\n\n",
            "  First\nline  \n\n  Second  ",
            "\n\n\n\n",
            "",
        )
        for text in fixtures:
            with self.subTest(text_length=len(text)):
                table = segmenter_draft.paragraph_table(text)
                self.assertEqual(table.starts, tuple(item.start for item in sections._paragraphs(text)))
                self.assertEqual(table.length, len(text))
        self.assertGreater(segmenter_draft.paragraph_table(fixtures[1]).starts[0], 0)

    def test_empty_runs_refuse_without_engine_call(self) -> None:
        with patch.object(engine, "call_model_server") as call:
            result = segmenter_draft.draft(
                cast(Host, object()), "/tmp/fictitious-render", served_model_name="fixture", text="\n\n",
            )
        call.assert_not_called()
        self.assertEqual(result.failure.code if result.failure else None, "off-schema")
        self.assertEqual(result.windows, 0)


class Prompt(unittest.TestCase):
    """The versioned schema and request shape are pinned independently."""

    def test_registry_version_digest_and_vocabulary(self) -> None:
        self.assertEqual(tuple(segmenter_draft.PROMPTS), ("segmenter-label@1",))
        self.assertEqual(set(segmenter_draft.PROMPTS), set(segmenter_draft.PROMPT_DIGESTS))
        prompt = segmenter_draft.PROMPTS["segmenter-label@1"]
        self.assertIsInstance(prompt, judge.JudgePrompt)
        self.assertEqual(prompt.id, "segmenter-label@1")
        self.assertEqual(segmenter_draft.PROMPT_DIGESTS[prompt.id], judge.prompt_digest(prompt))
        self.assertNotEqual(
            segmenter_draft.PROMPT_DIGESTS[prompt.id],
            judge.prompt_digest(replace(prompt, system=prompt.system + " revised")),
        )
        fields = tuple(
            field for _, field, _, _ in string.Formatter().parse(prompt.user_template)
            if field is not None
        )
        self.assertEqual(fields, prompt.slots)
        properties = prompt.schema["properties"]
        assert isinstance(properties, dict)
        section_schema = properties["sections"]
        assert isinstance(section_schema, dict)
        self.assertEqual(section_schema["minItems"], 1)
        item = section_schema["items"]
        assert isinstance(item, dict)
        item_properties = item["properties"]
        assert isinstance(item_properties, dict)
        section_type = item_properties["type"]
        assert isinstance(section_type, dict)
        self.assertEqual(tuple(section_type["enum"]), SECTION_TYPES)
        self.assertEqual(item_properties["paragraph"], {"type": "integer"})

    def test_request_body_uses_generator_schema_and_thinking_off(self) -> None:
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def call(*args: object, **kwargs: object) -> engine.EngineReply:
            calls.append((args, kwargs))
            return _reply()

        with patch.object(engine, "call_model_server", side_effect=call):
            result = segmenter_draft.draft(
                cast(Host, object()), "/tmp/fictitious-render",
                served_model_name="fixture-model", text="  Fictitious paragraph",
            )
        self.assertIsNone(result.failure)
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertIs(args[2], GENERATOR)
        self.assertEqual(kwargs["path"], "/v1/chat/completions")
        self.assertEqual(kwargs["max_time"], segmenter_draft.DRAFT_TIMEOUT_SECONDS)
        body = kwargs["body"]
        assert isinstance(body, dict)
        self.assertEqual(body["model"], "fixture-model")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["max_tokens"], segmenter_draft.DRAFT_MAX_TOKENS)
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertIs(body["stream"], False)
        prompt = segmenter_draft.PROMPTS["segmenter-label@1"]
        self.assertEqual(body["response_format"], {
            "type": "json_schema",
            "json_schema": {"name": prompt.schema_name, "schema": prompt.schema},
        })
        messages = body["messages"]
        assert isinstance(messages, list)
        self.assertEqual(messages[0], {"role": "system", "content": prompt.system})
        self.assertIn("0000\tFictitious paragraph", messages[1]["content"])
        self.assertIn("Running type at the first paragraph: unknown", messages[1]["content"])


class Replies(unittest.TestCase):
    """Only validated transitions leave the model reply path."""

    def test_valid_reply_and_later_first_transition(self) -> None:
        text = "First paragraph\n\nSecond paragraph"
        valid = _draft(_reply([
            {"paragraph": 0, "type": "majority"},
            {"paragraph": 1, "type": "dissent"},
        ]), text)
        self.assertEqual(valid.transitions, (
            segmenter_draft.Transition(0, "majority"),
            segmenter_draft.Transition(1, "dissent"),
        ))
        self.assertFalse(valid.prepended)
        self.assertEqual((valid.prompt_tokens, valid.completion_tokens, valid.elapsed_seconds), (11, 7, 1.5))
        self.assertEqual(valid.windows, 1)
        later = _draft(_reply([{"paragraph": 1, "type": "dissent"}]), text)
        self.assertEqual(later.transitions, (
            segmenter_draft.Transition(0, "unknown"),
            segmenter_draft.Transition(1, "dissent"),
        ))
        self.assertTrue(later.prepended)

    def test_second_opinion_of_one_type_keeps_its_mark(self) -> None:
        text = "Opinion\n\nFirst dissent\n\nSecond dissent"
        result = _draft(_reply([
            {"paragraph": 0, "type": "majority"},
            {"paragraph": 1, "type": "dissent"},
            {"paragraph": 2, "type": "dissent"},
        ]), text)
        self.assertEqual(
            tuple(item.paragraph for item in result.transitions or ()), (0, 1, 2)
        )

    def test_closed_failure_codes_do_not_carry_reply_text(self) -> None:
        private = "Fictitious private opinion paragraph"
        cases = (
            ("off-schema", _reply(content="{" + private)),
            ("off-schema", _reply(content=json.dumps({"sections": [{"paragraph": 0, "type": private}]}))),
            ("out-of-range", _reply([{"paragraph": 5, "type": "majority"}])),
            ("not-increasing", _reply([
                {"paragraph": 1, "type": "majority"}, {"paragraph": 0, "type": "dissent"},
            ])),
            ("truncated", _reply(finish="length", content=private)),
            ("http-status", _reply(status=503, content=private)),
            ("request-failed", _reply(problem=Problem(private, private))),
        )
        for code, reply in cases:
            with self.subTest(code=code):
                result = _draft(reply, "First paragraph\n\nSecond paragraph")
                self.assertIsNone(result.transitions)
                assert result.failure is not None
                self.assertEqual(result.failure.code, code)
                self.assertNotIn(private, result.failure.detail)
                self.assertNotIn(private, repr(result))
                self.assertEqual(result.windows, 1)

    def test_windows_carry_running_type_and_offset_indices(self) -> None:
        paragraph = "F" * 50_000
        text = "\n\n".join((paragraph, paragraph, paragraph, paragraph))
        replies = (
            _reply([{"paragraph": 0, "type": "majority"}]),
            _reply([{"paragraph": 1, "type": "dissent"}]),
        )
        bodies: list[dict[str, object]] = []

        def call(*_args: object, **kwargs: object) -> engine.EngineReply:
            body = kwargs["body"]
            assert isinstance(body, dict)
            bodies.append(body)
            return replies[len(bodies) - 1]

        with patch.object(engine, "call_model_server", side_effect=call):
            result = segmenter_draft.draft(
                cast(Host, object()), "/tmp/fictitious-render", served_model_name="fixture", text=text,
            )
        self.assertEqual(result.windows, 2)
        self.assertEqual(result.transitions, (
            segmenter_draft.Transition(0, "majority"),
            segmenter_draft.Transition(3, "dissent"),
        ))
        # The second window's unmarked start continues the running type.
        self.assertFalse(result.prepended)
        self.assertEqual((result.prompt_tokens, result.completion_tokens, result.elapsed_seconds), (22, 14, 3.0))
        for body in bodies:
            messages = body["messages"]
            assert isinstance(messages, list)
            user = messages[1]["content"]
            self.assertEqual(user.count("0000\t"), 1)
            self.assertEqual(user.count("0001\t"), 1)
        first_messages = bodies[0]["messages"]
        second_messages = bodies[1]["messages"]
        assert isinstance(first_messages, list) and isinstance(second_messages, list)
        self.assertIn("Running type at the first paragraph: unknown", first_messages[1]["content"])
        self.assertIn("Running type at the first paragraph: majority", second_messages[1]["content"])


class Imports(unittest.TestCase):
    """The host draft module keeps worker imports out of its runtime boundary."""

    def test_module_imports_only_standard_library_and_gideon(self) -> None:
        path = Path(segmenter_draft.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                self.assertTrue(all(name.name.split(".")[0] in {"json", "collections", "dataclasses", "typing", "gideon"} for name in node.names))
            elif isinstance(node, ast.ImportFrom):
                self.assertIn((node.module or "").split(".")[0], {"collections", "dataclasses", "typing", "gideon"})
                self.assertFalse((node.module or "").startswith("gideon.worker"))


if __name__ == "__main__":
    unittest.main()
