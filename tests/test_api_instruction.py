"""Contracts for General's service-owned completion instruction."""

import ast
import json
import sys
import unittest
from pathlib import Path

from gideon.api.instruction import INSTRUCTION_JOINER, instruct_completion_body

INSTRUCTION = "Fictitious General instruction."


class InstructionBody(unittest.TestCase):
    """The service mirrors the frontend's leading system-message splice."""

    def test_leading_system_text_is_joined_and_other_fields_are_preserved(self) -> None:
        messages = [
            {"role": "system", "content": "Personal direction.", "name": "personal"},
            {"role": "user", "content": "Fixture prompt."},
        ]
        original = {
            "model": "fixture-model",
            "messages": messages,
            "stream": False,
        }
        body = json.dumps(original, indent=2).encode()

        instructed = instruct_completion_body(body, INSTRUCTION)

        expected = {
            **original,
            "messages": [
                {
                    "role": "system",
                    "content": INSTRUCTION + INSTRUCTION_JOINER + "Personal direction.",
                    "name": "personal",
                },
                messages[1],
            ],
        }
        self.assertEqual(instructed, json.dumps(expected, separators=(",", ":")).encode())
        self.assertEqual(INSTRUCTION_JOINER, "\n")

    def test_new_system_message_precedes_every_other_first_shape(self) -> None:
        first_messages: tuple[object | None, ...] = (
            None,
            {"role": "user", "content": "Fixture prompt."},
            {"role": "system", "content": [{"type": "text", "text": "Personal"}]},
            {"role": "system", "content": None},
            {"role": "system", "content": 7},
        )
        for first in first_messages:
            with self.subTest(first=first):
                messages = [] if first is None else [first]
                body = json.dumps({"messages": messages, "temperature": 0.2}).encode()

                instructed = json.loads(instruct_completion_body(body, INSTRUCTION))

                self.assertEqual(instructed["messages"], [
                    {"role": "system", "content": INSTRUCTION}, *messages
                ])
                self.assertEqual(instructed["temperature"], 0.2)

    def test_unreadable_or_unshaped_bodies_remain_byte_equal(self) -> None:
        bodies = (
            b"not JSON",
            b'["a JSON array"]',
            b'{ "model": "fixture-model" }',
            b'{ "messages": "not a list" }',
        )
        for body in bodies:
            with self.subTest(body=body):
                self.assertIs(instruct_completion_body(body, INSTRUCTION), body)

    def test_module_imports_only_the_standard_library(self) -> None:
        source = Path(__file__).resolve().parents[1] / "gideon/api/instruction.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
        for node in imports:
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                names = [node.module or ""] if node.level == 0 else [""]
            self.assertTrue(all(name.split(".")[0] in sys.stdlib_module_names for name in names))
