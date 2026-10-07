"""Account for literal product commands in package source strings."""

import argparse
import ast
import re
import unittest
from pathlib import Path

from gideon.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent
ENTRIES = {
    "gideon/egress/__main__.py": "slice-3 ticket 80: egress service start fixes",
    "gideon/egress/settings.py": "slice-3 ticket 80: egress service settings fixes",
    "gideon/host/alerts.py": "front-door ticket 29: alerts test fixes",
    "gideon/host/cas.py": "front-door ticket 29: content-addressed store fixes",
    "gideon/host/grafana.py": "the test alert's summary names the command that sent it",
    "gideon/host/nogpu.py": "the mode marker's own text names the declaring command",
    "gideon/host/preflight.py": "front-door ticket 29: preflight fixes",
    "gideon/host/provision.py": "front-door ticket 29: provision fixes",
    "gideon/host/registry.py": "front-door ticket 29: registry mirror fixes",
    "gideon/host/render/grafana.py": "the page email tells an operator on the box what to run",
    "gideon/host/render/systemd.py": "the unit's ExecStart runs the long form from its WorkingDirectory",
    "gideon/host/render/yamlout.py": "the rendered file's own text names its command",
    "gideon/host/report.py": "the renderer holds the command forms",
    "gideon/host/steps/command.py": "the wrapper's own text and its fallback fix name the command",
    "gideon/host/steps/site_dirs.py": "front-door ticket 29: provision step fixes",
    "gideon/host/tls.py": "front-door ticket 29: TLS reload fixes",
    "gideon/host/users.py": "front-door ticket 29: users reconcile fixes",
    "gideon/worker/settings.py": "front-door ticket 29: worker settings fixes",
}


def _literal_strings(tree: ast.AST) -> tuple[str, ...]:
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return tuple(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    )


class _ImportTimeCommands(ast.NodeVisitor):
    def __init__(self) -> None:
        self.lines: list[int] = []

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "command"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "report"
        ):
            self.lines.append(node.lineno)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for expression in (
            *node.decorator_list,
            *node.args.defaults,
            *node.args.kw_defaults,
        ):
            if expression is not None:
                self.visit(expression)
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            if argument.annotation is not None:
                self.visit(argument.annotation)
        if node.args.vararg is not None and node.args.vararg.annotation is not None:
            self.visit(node.args.vararg.annotation)
        if node.args.kwarg is not None and node.args.kwarg.annotation is not None:
            self.visit(node.args.kwarg.annotation)
        if node.returns is not None:
            self.visit(node.returns)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for expression in (*node.args.defaults, *node.args.kw_defaults):
            if expression is not None:
                self.visit(expression)


class CommandForms(unittest.TestCase):
    def test_import_time_check_covers_class_bodies_and_defaults(self) -> None:
        tree = ast.parse(
            "fix = report.command('apply')\n"
            "class Example:\n"
            "    fix = report.command('render')\n"
            "    def method(self, fix=report.command('install')):\n"
            "        return report.command('upgrade')\n"
            "def deferred():\n"
            "    return report.command('models pull')\n"
            "name = report.command_name('apply')\n"
        )
        visitor = _ImportTimeCommands()
        visitor.visit(tree)
        self.assertEqual(visitor.lines, [1, 3, 4])

    def test_literal_command_modules_have_current_entries(self) -> None:
        subparsers = [
            action
            for action in build_parser()._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        self.assertEqual(len(subparsers), 1)
        words = sorted(subparsers[0].choices, key=len, reverse=True)
        long_form = re.compile(r"\bpython(?:3)? -m gideon\b")
        bare_form = re.compile(r"\bgideon (?:" + "|".join(map(re.escape, words)) + r")\b")

        found: set[str] = set()
        for path in sorted((ROOT / "gideon").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if any(
                long_form.search(value) or bare_form.search(value)
                for value in _literal_strings(tree)
            ):
                found.add(path.relative_to(ROOT).as_posix())

        missing = found - ENTRIES.keys()
        stale = ENTRIES.keys() - found
        findings = [
            f"{path}: render the command through the report module or add an entry naming the ticket"
            for path in sorted(missing)
        ]
        findings.extend(
            f"{path}: delete the entry once the module names no literal command"
            for path in sorted(stale)
        )
        self.assertFalse(findings, "\n".join(findings))

    def test_command_renderer_is_not_called_at_import(self) -> None:
        findings: list[str] = []
        for path in sorted((ROOT / "gideon").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            visitor = _ImportTimeCommands()
            visitor.visit(tree)
            findings.extend(
                f"{path.relative_to(ROOT).as_posix()}:{line}: build the text in a function"
                for line in visitor.lines
            )
        self.assertFalse(findings, "\n".join(findings))


if __name__ == "__main__":
    unittest.main()
