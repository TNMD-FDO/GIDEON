"""The branch gate's inlet rule, coupling, and frontend ordering tests."""

import ast
import importlib.util
import inspect
import re
import sys
import unittest
from pathlib import Path
from typing import Any

from gideon.host.render.owui import (
    EVAL_IDENTITY,
)

ROOT = Path(__file__).resolve().parent.parent
GATE_PATH = ROOT / "compose/open-webui/functions/branch_gate.py"

# These entry shapes mirror how the frontend merges preset and base records;
# all ids are visibly fictitious.
PRESET_ENTRY: dict[str, object] = {
    "id": "a-preset",
    "name": "a-preset",
    "info": {"base_model_id": "a-base"},
}
BASE_ENTRY: dict[str, object] = {
    "id": "a-base",
    "name": "a-base",
    "info": {"base_model_id": None},
}
META_ONLY_ENTRY: dict[str, object] = {"info": {"meta": {}}}
NO_INFO_ENTRY: dict[str, object] = {"id": "a-unrecorded", "name": "a-unrecorded"}


def load_filter(path: Path, module_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GATE: Any = load_filter(GATE_PATH, "branch_gate")


class BranchGate(unittest.TestCase):
    """The branch gate fails closed for users and exempts machine paths."""

    def setUp(self) -> None:
        self.body = {"messages": [{"role": "user", "content": object()}]}

    def test_user_on_every_non_preset_entry_is_refused(self) -> None:
        entries: tuple[object, ...] = (
            BASE_ENTRY,
            META_ONLY_ENTRY,
            NO_INFO_ENTRY,
            {"info": {"base_model_id": ""}},
            "a-base",
            None,
        )
        for model_entry in entries:
            with self.subTest(model_entry=model_entry), self.assertRaisesRegex(
                GATE.BranchRefusal, re.escape(GATE.BRANCH_REFUSAL)
            ):
                GATE.Filter().inlet(
                    self.body,
                    {"role": "user", "email": "person@example.invalid"},
                    model_entry,
                )

    def test_user_on_a_preset_entry_gets_the_body_unchanged(self) -> None:
        self.assertIs(
            GATE.Filter().inlet(
                self.body,
                {"role": "user", "email": "person@example.invalid"},
                PRESET_ENTRY,
            ),
            self.body,
        )

    def test_admin_and_eval_identity_pass_on_base_and_general(self) -> None:
        for model_entry in (BASE_ENTRY, PRESET_ENTRY):
            with self.subTest(model_entry=model_entry):
                self.assertIs(
                    GATE.Filter().inlet(
                        self.body,
                        {"role": "admin", "email": "admin@example.invalid"},
                        model_entry,
                    ),
                    self.body,
                )
                self.assertIs(
                    GATE.Filter().inlet(
                        self.body,
                        {"role": "user", "email": GATE.EVAL_IDENTITY_EMAIL},
                        model_entry,
                    ),
                    self.body,
                )

    def test_unusable_user_is_refused(self) -> None:
        for user in (None, object(), []):
            with self.subTest(user=user), self.assertRaisesRegex(
                GATE.BranchRefusal, re.escape(GATE.BRANCH_REFUSAL)
            ):
                GATE.Filter().inlet(self.body, user, PRESET_ENTRY)

    def test_eval_email_matches_render_identity(self) -> None:
        self.assertEqual(GATE.EVAL_IDENTITY_EMAIL, EVAL_IDENTITY.email)

    def test_branch_refusal_text_is_pinned(self) -> None:
        # A versioned product text, carried over from the guardrail unchanged.
        self.assertEqual(
            GATE.BRANCH_REFUSAL,
            "GIDEON answers only through one of its branches. Start a new chat and ask General.",
        )


class FileRules(unittest.TestCase):
    """The gate remains a narrow frontend Function with no optional hooks."""

    def test_function_file_is_stdlib_only_and_has_the_frontmatter(self) -> None:
        source = GATE_PATH.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], sys.stdlib_module_names)
            if isinstance(node, ast.ImportFrom):
                assert node.module is not None
                self.assertIn(node.module.split(".")[0], sys.stdlib_module_names)
        self.assertFalse(hasattr(GATE, "Valves"))
        self.assertFalse(hasattr(GATE.Filter, "Valves"))
        self.assertFalse(hasattr(GATE.Filter, "toggle"))
        self.assertFalse(hasattr(GATE.Filter, "stream"))
        self.assertFalse(hasattr(GATE.Filter, "outlet"))
        self.assertNotIn("__metadata__", inspect.signature(GATE.Filter.inlet).parameters)
        self.assertEqual(
            tuple(inspect.signature(GATE.Filter.inlet).parameters),
            ("self", "body", "__user__", "__model__"),
        )
        frontmatter = ast.get_docstring(tree) or ""
        self.assertIn("title:", frontmatter)
        self.assertNotIn("requirements:", frontmatter)
        for forbidden in ("from utils", "from apps", "from main", "from config"):
            self.assertNotIn(forbidden, source)
