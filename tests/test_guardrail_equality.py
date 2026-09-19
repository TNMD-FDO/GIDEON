"""Hold ``gideon/guardrail.py`` statement-equal to the guardrail Function.

Parses both files and imports neither. Ticket 09 deletes this module with the
Function.
"""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "gideon/guardrail.py"
FUNCTION_PATH = ROOT / "compose/open-webui/functions/arithmetic_guardrail.py"
# The render's template names the Function from under compose/, so the held list
# searches for its path from functions/ on.
FUNCTION_NAME = "functions/arithmetic_guardrail.py"
_CARRY = "carry the edit to both files until ticket 09"

FUNCTION_ONLY = frozenset(
    {
        "asyncio",
        "STREAM_STATE_KEY",
        "SESSION_REFUSAL",
        "BRANCH_REFUSAL",
        "EVAL_IDENTITY_EMAIL",
        "TASK_ID_KEY",
        "REPLACEMENT_MESSAGE_ID",
        "SessionRefusal",
        "BranchRefusal",
        "_is_preset",
        "_TEXT_KEYS",
        "_REASONING_KEYS",
        "_refusal_chunk",
        "_cancel_current_task",
        "_is_generating_choice",
        "_clear_text",
        "_stream_tail",
        "_message_output_item",
        "_append_output_text",
        "_append_stream_tails",
        "_scrub_reasoning",
        "_trip_chunk",
        "_filter_chunk",
        "_existing_output_message_id",
        "replace_message",
        "Filter",
    }
)
# Every file naming the Function's file: its hook tests, the gate texts' and the
# stamp's readers, the render's template, and this module.
HELD_FUNCTION_USERS = (
    "gideon/guardrail.py",
    "gideon/host/render/owui.py",
    "tests/test_arithmetic_guardrail.py",
    "tests/test_citation_stamp.py",
    "tests/test_engine_verify.py",
    "tests/test_guardrail_equality.py",
    "tools/turns/classify.py",
)


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def bound_names(node: ast.stmt) -> frozenset[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return frozenset({node.name})
    if isinstance(node, ast.Import):
        return frozenset(alias.asname or alias.name.split(".")[0] for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return frozenset(alias.asname or alias.name for alias in node.names if alias.name != "*")
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names: set[str] = set()

        def collect(target: ast.AST) -> None:
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, (ast.Tuple, ast.List)):
                for item in target.elts:
                    collect(item)

        for target in targets:
            collect(target)
        return frozenset(names)
    return frozenset()


def statements(tree: ast.Module) -> list[ast.stmt]:
    body = tree.body
    if body and isinstance(body[0], ast.Expr):
        value = body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return body[1:]
    return body


def statement_label(node: ast.stmt) -> str:
    names = bound_names(node)
    return ", ".join(sorted(names)) or type(node).__name__


class GuardrailEquality(unittest.TestCase):
    def test_module_statements_equal_function_without_function_only_statements(self) -> None:
        function_statements = statements(parse(FUNCTION_PATH))
        module_statements = statements(parse(MODULE_PATH))
        expected = [
            node
            for node in function_statements
            if not bound_names(node) & FUNCTION_ONLY
        ]
        for actual, wanted in zip(module_statements, expected, strict=False):
            self.assertEqual(
                ast.dump(actual, include_attributes=False),
                ast.dump(wanted, include_attributes=False),
                f"{statement_label(actual)} at line {actual.lineno} of the module differs "
                f"from {statement_label(wanted)} at line {wanted.lineno} of the Function; "
                f"{_CARRY}",
            )
        self.assertEqual(
            len(module_statements),
            len(expected),
            f"the module has {len(module_statements)} top-level statements where the "
            f"Function has {len(expected)} outside the Function-only set; {_CARRY}",
        )

    def test_function_only_names_are_exact(self) -> None:
        function_names = frozenset(
            name for node in statements(parse(FUNCTION_PATH)) for name in bound_names(node)
        )
        module_names = frozenset(
            name for node in statements(parse(MODULE_PATH)) for name in bound_names(node)
        )
        self.assertEqual(function_names - module_names, FUNCTION_ONLY)
        self.assertEqual(module_names - function_names, frozenset())

    def test_no_top_level_name_is_bound_twice(self) -> None:
        for label, path in (("Function", FUNCTION_PATH), ("module", MODULE_PATH)):
            seen: set[str] = set()
            duplicates: set[str] = set()
            for node in statements(parse(path)):
                names = bound_names(node)
                duplicates.update(seen & names)
                seen.update(names)
            self.assertEqual(duplicates, set(), f"{label} binds names twice")

    def test_function_path_users_are_held(self) -> None:
        actual = tuple(sorted(
            path.relative_to(ROOT).as_posix()
            for directory in (ROOT / "tools", ROOT / "tests", ROOT / "gideon")
            for path in directory.rglob("*.py")
            if FUNCTION_NAME in path.read_text(encoding="utf-8")
        ))
        self.assertEqual(
            actual,
            HELD_FUNCTION_USERS,
            f"a file naming {FUNCTION_NAME} is a by-path loader; add it here consciously",
        )
