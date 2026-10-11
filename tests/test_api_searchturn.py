"""Search-query detection, bounded event rows, and the service import boundary."""

import ast
import json
import sys
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import yaml  # type: ignore[import-untyped]

from gideon import guardrail
from gideon.api import searchturn

ROOT = Path(__file__).resolve().parent.parent
SEARCHTURN_PATH = ROOT / "gideon/api/searchturn.py"
SITE_EXAMPLE = ROOT / "config/site.example.yaml"


def _body(messages: object) -> bytes:
    return json.dumps({"messages": messages}).encode("utf-8")


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for nested in value.values():
            yield from _strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _strings(nested)
    elif isinstance(value, str):
        yield value


class SearchQueryTell(unittest.TestCase):
    def test_first_user_text_identifies_only_the_query_task(self) -> None:
        query = searchturn.SEARCH_QUERY_TELL + "\nFictitious chat history follows."
        cases = (
            ([("user", query)], True),
            ([("user", "Fictitious chat.\n" + query)], False),
            ([("system", "Fictitious instruction."), ("user", query)], True),
            ([("user", "### Task:\nGenerate a fictional title.")], False),
            ([("user", "Fictitious chat."), ("user", query)], False),
        )
        for entries, expected in cases:
            with self.subTest(entries=entries):
                messages = [{"role": role, "content": text} for role, text in entries]
                self.assertIs(searchturn.is_search_query_call(_body(messages)), expected)

    def test_parts_list_uses_its_first_text_part(self) -> None:
        query = searchturn.SEARCH_QUERY_TELL + "\nFictitious chat history follows."
        parts = [
            {"type": "image_url", "image_url": "https://example.invalid/image"},
            {"type": "text", "text": query},
            {"type": "text", "text": "Fictitious later text."},
        ]
        messages = [{"role": "user", "content": parts}]
        self.assertTrue(searchturn.is_search_query_call(_body(messages)))
        parts[1]["text"] = "Fictitious first text."
        self.assertFalse(searchturn.is_search_query_call(_body(messages)))

    def test_unreadable_and_non_task_bodies_are_false_without_raising(self) -> None:
        bodies = (
            b"not-json",
            b"\xff",
            b"[]",
            b"null",
            b"{}",
            _body({"role": "user", "content": searchturn.SEARCH_QUERY_TELL}),
            _body(None),
            _body(["not a message", {"role": "assistant", "content": searchturn.SEARCH_QUERY_TELL}]),
            _body([{"role": "user", "content": None}]),
        )
        for body in bodies:
            with self.subTest(body=body):
                self.assertFalse(searchturn.is_search_query_call(body))


class SearchTurnWriter(unittest.TestCase):
    def test_dispatch_keeps_only_bounded_ids_and_source_words(self) -> None:
        maximum = int(guardrail.TRIP_CHAT_ID_PATTERN.pattern.split(",")[-1].rstrip("}"))
        accepted_user = "fictitious-user_42"
        accepted_chat = "fictitious-chat_73"
        cases = (
            (accepted_user, accepted_chat, guardrail.EVAL_SOURCE, (accepted_user, accepted_chat, guardrail.EVAL_SOURCE)),
            ("x" * (maximum + 1), "free text", "unknown", (None, None, guardrail.USER_SOURCE)),
            (None, None, guardrail.USER_SOURCE, (None, None, guardrail.USER_SOURCE)),
        )
        calls: list[tuple[object, ...]] = []
        with patch.object(guardrail, "dispatch_event_row", side_effect=lambda *args: calls.append(args)):
            for user_id, chat_id, source, expected in cases:
                searchturn.record_search_turn(user_id, chat_id, source)
                self.assertEqual(
                    calls[-1],
                    (
                        searchturn.SEARCH_TURNS_TABLE,
                        searchturn.SEARCH_TURNS_PARTITION_FUNCTION,
                        searchturn.SEARCH_TURNS_COLUMNS,
                        expected,
                    ),
                )
        self.assertEqual(len(calls), len(cases))

    def test_recording_failures_do_not_escape(self) -> None:
        with patch.object(guardrail, "dispatch_event_row", side_effect=RuntimeError("fixture failure")):
            searchturn.record_search_turn("fictitious-user", None, "user")
        with patch.object(guardrail, "bounded_id", side_effect=RuntimeError("fixture failure")):
            searchturn.record_search_turn("fictitious-user", None, "user")


class Hygiene(unittest.TestCase):
    def test_module_imports_only_standard_library_and_guardrail(self) -> None:
        tree = ast.parse(SEARCHTURN_PATH.read_text(encoding="utf-8"), filename=str(SEARCHTURN_PATH))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], sys.stdlib_module_names)
            elif isinstance(node, ast.ImportFrom):
                self.assertIsNotNone(node.module)
                assert node.module is not None
                self.assertTrue(
                    node.module.split(".")[0] in sys.stdlib_module_names
                    or node.module == "gideon",
                    f"searchturn.py imports an unsupported module: {node.module}",
                )

    def test_tell_contains_no_example_site_value(self) -> None:
        site = yaml.safe_load(SITE_EXAMPLE.read_text(encoding="utf-8"))
        for value in _strings(site):
            with self.subTest(value=value):
                self.assertNotIn(value, searchturn.SEARCH_QUERY_TELL)
