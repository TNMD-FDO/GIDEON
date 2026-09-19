"""The citation stamp's contract: the Filter over its seed and the pinned payload shape."""

import ast
import copy
import importlib.util
import inspect
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml  # type: ignore[import-untyped]

from gideon import guardrail

ROOT = Path(__file__).resolve().parent.parent
FILTER_PATH = ROOT / "compose/open-webui/functions/citation_stamp.py"
SEED_PATH = ROOT / "eval/seed/general/citation-stamp.yaml"
FUNCTION_PATH = ROOT / "compose/open-webui/functions/arithmetic_guardrail.py"


def load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


FILTER: Any = load_module(FILTER_PATH, "citation_stamp")
FUNCTION: Any = load_module(FUNCTION_PATH, "citation_stamp_guardrail")
TAIL: str = FILTER.STAMP_SEPARATOR + FILTER.CITATION_STAMP


def document() -> dict[str, object]:
    loaded = yaml.safe_load(SEED_PATH.read_text())
    assert isinstance(loaded, dict)
    return loaded


def cases() -> list[dict[str, object]]:
    loaded = document()["cases"]
    assert isinstance(loaded, list)
    return [case for case in loaded if isinstance(case, dict)]


def body(answer: str, *, output: bool = True, thinking: str = "") -> dict[str, object]:
    """A body in the pinned outlet payload's shape: a user turn, then the assistant's answer."""

    items: list[dict[str, object]] = []
    if thinking:
        items.append({"type": "reasoning", "content": [{"type": "output_text", "text": thinking}]})
    if output:
        items.append(
            {
                "type": "message",
                "id": "msg_1234567890abcdef12345678",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": answer}],
            }
        )
    message: dict[str, object] = {"id": "a1", "role": "assistant", "content": answer}
    if output or thinking:
        message["output"] = items
    return {
        "model": "gideon-general",
        "messages": [{"id": "u1", "role": "user", "content": "an invented prompt"}, message],
        "filter_ids": [],
        "chat_id": "chat-id",
        "session_id": "session-id",
        "id": "a1",
    }


def assistant(payload: dict[str, object]) -> dict[str, object]:
    messages = payload["messages"]
    assert isinstance(messages, list)
    message = messages[-1]
    assert isinstance(message, dict)
    return message


def message_item_text(message: dict[str, object]) -> str:
    output = message["output"]
    assert isinstance(output, list)
    item = next(item for item in output if item.get("type") == "message")
    parts = item["content"]
    assert isinstance(parts, list)
    return "".join(str(part["text"]) for part in parts)


class Seed(unittest.TestCase):
    def test_family_version_and_both_kinds(self) -> None:
        loaded = document()
        self.assertEqual(loaded["family"], "citation")
        self.assertEqual(loaded["pattern_set_version"], 1)
        kinds = {case["kind"] for case in cases()}
        self.assertEqual(kinds, {"shaped", "free"})
        shaped = [case for case in cases() if case["kind"] == "shaped"]
        free = [case for case in cases() if case["kind"] == "free"]
        self.assertEqual(len(shaped), len(free))
        self.assertEqual(len({case["id"] for case in cases()}), len(cases()))
        for case in shaped:
            self.assertIn(case["pattern"], {pattern.pattern_id for pattern in FILTER.PATTERNS})
        for case in free:
            self.assertNotIn("pattern", case)

    def test_every_family_has_two_shaped_cases(self) -> None:
        shaped = [case for case in cases() if case["kind"] == "shaped"]
        for pattern in FILTER.PATTERNS:
            with self.subTest(family=pattern.pattern_id):
                self.assertGreaterEqual(sum(case.get("pattern") == pattern.pattern_id for case in shaped), 2)


class Outlet(unittest.TestCase):
    def test_shaped_cases_stamp_once_in_both_representations(self) -> None:
        for case in cases():
            if case["kind"] != "shaped":
                continue
            with self.subTest(case=case["id"]):
                answer = str(case["answer"])
                payload = body(answer, thinking="a fictional thought with no citation")
                returned = FILTER.Filter().outlet(payload)
                message = assistant(payload)
                self.assertIs(returned, payload)
                self.assertEqual(message["content"], answer + TAIL)
                self.assertEqual(message_item_text(message), answer + TAIL)
                self.assertEqual(str(message["content"]).count(FILTER.CITATION_STAMP), 1)
                self.assertEqual(FILTER.detect(answer), case["pattern"])

    def test_free_cases_are_deep_equal(self) -> None:
        for case in cases():
            if case["kind"] != "free":
                continue
            with self.subTest(case=case["id"]):
                payload = body(str(case["answer"]), thinking="a thought that mentions no authority")
                before = copy.deepcopy(payload)
                returned = FILTER.Filter().outlet(payload)
                self.assertIs(returned, payload)
                self.assertEqual(payload, before)
                self.assertIsNone(FILTER.detect(str(case["answer"])))

    def test_reasoning_items_are_not_scanned(self) -> None:
        payload = body("An ordinary fictional answer.", thinking="A hidden thought mentions 2026 WL 123456.")
        before = copy.deepcopy(payload)
        returned = FILTER.Filter().outlet(payload)
        self.assertIs(returned, payload)
        self.assertEqual(payload, before)

    def test_already_stamped_body_is_untouched(self) -> None:
        payload = body("An invented cite is 3 F.2d 8." + TAIL)
        before = copy.deepcopy(payload)
        returned = FILTER.Filter().outlet(payload)
        self.assertIs(returned, payload)
        self.assertEqual(payload, before)

    def test_a_second_pass_never_doubles_the_stamp(self) -> None:
        payload = body("An invented cite is 3 F.2d 8.")
        FILTER.Filter().outlet(payload)
        after_first = copy.deepcopy(payload)
        FILTER.Filter().outlet(payload)
        self.assertEqual(payload, after_first)

    def test_same_object_on_all_paths(self) -> None:
        values: list[object] = [None, {}, {"messages": []}, {"messages": [{"role": "user"}]}]
        for value in values:
            with self.subTest(value=value):
                self.assertIs(FILTER.Filter().outlet(value), value)

    def test_malformed_body_matrix_is_untouched(self) -> None:
        values: list[object] = [
            {"messages": None},
            {"messages": ["not a message"]},
            {"messages": [{"role": "user", "content": "1 U.S. 2"}]},
            {"messages": [{"role": "assistant", "content": 42}]},
            {"messages": [{"role": "assistant", "output": "not a list"}]},
            {"messages": [{"role": "assistant", "output": [{"type": "reasoning", "content": []}]}]},
        ]
        for value in values:
            with self.subTest(value=value):
                before = copy.deepcopy(value)
                self.assertIs(FILTER.Filter().outlet(value), value)
                self.assertEqual(value, before)

    def test_content_only_message_is_stamped_in_content_alone(self) -> None:
        payload = body("The fictional rule is Fed. R. Civ. P.", output=False)
        returned = FILTER.Filter().outlet(payload)
        message = assistant(payload)
        self.assertIs(returned, payload)
        self.assertEqual(message["content"], "The fictional rule is Fed. R. Civ. P." + TAIL)
        self.assertNotIn("output", message)

    def test_empty_content_uses_the_output_fallback_and_keeps_the_answer(self) -> None:
        answer = "The fictional cite is 2026 WL 555000."
        payload = body(answer, thinking="no citation in thought")
        message = assistant(payload)
        message["content"] = ""
        returned = FILTER.Filter().outlet(payload)
        self.assertIs(returned, payload)
        self.assertEqual(message["content"], answer + TAIL)
        self.assertEqual(message_item_text(message), answer + TAIL)

    def test_output_without_a_message_item_gains_one_carrying_the_whole_answer(self) -> None:
        answer = "The invented statute is 18 U.S.C. § 4123."
        payload = body(answer, output=False, thinking="a thought with no citation")
        message = assistant(payload)
        returned = FILTER.Filter().outlet(payload)
        self.assertIs(returned, payload)
        self.assertEqual(message["content"], answer + TAIL)
        output = message["output"]
        assert isinstance(output, list)
        self.assertEqual([item["type"] for item in output], ["reasoning", "message"])
        self.assertEqual(message_item_text(message), answer + TAIL)
        self.assertEqual(output[-1]["id"], FILTER.STAMP_MESSAGE_ID)
        self.assertEqual(output[-1]["status"], "completed")

    def test_internal_error_stamps_once(self) -> None:
        payload = body("An ordinary fictional answer.")
        with patch.object(FILTER, "detect", side_effect=RuntimeError("broken detector")):
            returned = FILTER.Filter().outlet(payload)
        message = assistant(payload)
        self.assertIs(returned, payload)
        self.assertEqual(message["content"], "An ordinary fictional answer." + TAIL)
        self.assertEqual(str(message["content"]).count(FILTER.CITATION_STAMP), 1)

    def test_internal_error_over_a_stamped_answer_adds_nothing(self) -> None:
        payload = body("An invented cite is 3 F.2d 8." + TAIL)
        before = copy.deepcopy(payload)
        with patch.object(FILTER, "detect", side_effect=RuntimeError("broken detector")):
            returned = FILTER.Filter().outlet(payload)
        self.assertIs(returned, payload)
        self.assertEqual(payload, before)


class Detection(unittest.TestCase):
    def test_shapes_the_families_cover(self) -> None:
        for text, family in (
            ("466 U.S. 668", FILTER.REPORTER_FAMILY_ID),
            ("140 S.Ct. 1204", FILTER.REPORTER_FAMILY_ID),
            ("--- F.4th ---", FILTER.REPORTER_FAMILY_ID),
            ("91 Fed. Reg. 2048", FILTER.REPORTER_FAMILY_ID),
            ("18 U.S.C. § 3553(a)", FILTER.CODE_FAMILY_ID),
            ("U.S.S.G. §2D1.1(c)(5)", FILTER.CODE_FAMILY_ID),
            ("18 U.S.C. 3553", FILTER.CODE_FAMILY_ID),
            ("28 C.F.R. § 2.20", FILTER.CODE_FAMILY_ID),
            ("Tenn. Code Ann. § 40-35-501", FILTER.CODE_FAMILY_ID),
            ("§§ 3553-3554", FILTER.CODE_FAMILY_ID),
            ("§ 2255", FILTER.CODE_FAMILY_ID),
            ("Fed. R. Crim. P.", FILTER.RULE_FAMILY_ID),
            ("Fed. R. Crim. P. 32.1(b)", FILTER.RULE_FAMILY_ID),
            ("Fed. R. Evid. 404(b)", FILTER.RULE_FAMILY_ID),
            ("2024 WL 1234567", FILTER.DATABASE_FAMILY_ID),
            ("2024 U.S. App. LEXIS 9876", FILTER.DATABASE_FAMILY_ID),
        ):
            with self.subTest(text=text):
                self.assertEqual(FILTER.detect(text), family)

    def test_shapes_outside_the_families(self) -> None:
        for text in (
            "Strickland v. Washington",
            "Section 3553(a) of Title 18",
            "section 2255 motion",
            "Rule 11",
            "the 2024 report, page 12",
            "9 a.m. to 3 p.m.",
            "version 2.3.1",
            "Form 1040, line 12",
            "$3,553 over 30 days",
            "United States v. Booker (2005)",
        ):
            with self.subTest(text=text):
                self.assertIsNone(FILTER.detect(text))


class BoundsAndHygiene(unittest.TestCase):
    def test_patterns_are_bounded_and_match_within_ceiling(self) -> None:
        adversarial = (
            "x" * (FILTER.MAX_MATCH_CHARS * 2),
            "999999 U.S. ____________",
            "99999   U.S.C.A.   §§   999999A999.999999.999999.999999  (test)  (part)  (more)  (last)  (abcd)  (1234)",
            "99999 Tenn. Code Ann. §§ 999999A999-999999-999999-999999 (aaaa)(bbbb)(cccc)(dddd)(eeee)(ffff)",
            "Fed. R. Bankr. P. 999.99",
            "9999 U.S. Dist. LEXIS 999999999999",
        )
        for pattern in FILTER.PATTERNS:
            with self.subTest(family=pattern.pattern_id):
                self.assertNotRegex(pattern.regex.pattern, r"(?:\*|\+|\{\d+,\})")
                for text in adversarial:
                    for match in pattern.regex.finditer(text):
                        self.assertLessEqual(len(match.group(0)), FILTER.MAX_MATCH_CHARS)

    def test_hygiene_and_signature(self) -> None:
        source = FILTER_PATH.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], sys.stdlib_module_names)
            if isinstance(node, ast.ImportFrom):
                assert node.module is not None
                self.assertIn(node.module.split(".")[0], sys.stdlib_module_names)
        self.assertFalse(hasattr(FILTER, "Valves"))
        self.assertFalse(hasattr(FILTER.Filter, "toggle"))
        self.assertFalse(hasattr(FILTER.Filter, "inlet"))
        self.assertFalse(hasattr(FILTER.Filter, "stream"))
        frontmatter = ast.get_docstring(tree) or ""
        self.assertIn("title: GIDEON citation stamp", frontmatter)
        self.assertIn("version: 2", frontmatter)
        self.assertNotIn("requirements:", frontmatter)
        for forbidden in ("from utils", "from apps", "from main", "from config"):
            self.assertNotIn(forbidden, source)
        self.assertEqual(
            FILTER.CITATION_STAMP,
            "General does not verify citations.",
        )
        self.assertEqual(tuple(inspect.signature(FILTER.Filter.outlet).parameters), ("self", "body"))

    def test_refusals_and_the_stamp_are_not_citations(self) -> None:
        for text in (guardrail.DEADLINE_REFUSAL, FUNCTION.SESSION_REFUSAL, FILTER.CITATION_STAMP):
            with self.subTest(text=text[:40]):
                self.assertIsNone(FILTER.detect(text))
