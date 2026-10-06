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
    "gideon/host/alerts.py": "front-door ticket 19: alerts test fixes",
    "gideon/host/apply.py": "front-door ticket 19: apply fixes and rows",
    "gideon/host/cas.py": "front-door ticket 19: content-addressed store fixes",
    "gideon/host/grafana.py": "the test alert's summary names the command that sent it",
    "gideon/host/install.py": "front-door ticket 19: install fixes",
    "gideon/host/nogpu.py": "the mode marker's own text names the declaring command",
    "gideon/host/preflight.py": "front-door ticket 19: preflight fixes",
    "gideon/host/provision.py": "front-door ticket 19: provision fixes",
    "gideon/host/registry.py": "front-door ticket 19: registry mirror fixes",
    "gideon/host/render/command.py": "front-door ticket 19: render command fixes",
    "gideon/host/render/facts.py": "front-door ticket 19: render input fixes",
    "gideon/host/render/grafana.py": "the page email tells an operator on the box what to run",
    "gideon/host/render/systemd.py": "the unit's ExecStart runs the long form from its WorkingDirectory",
    "gideon/host/render/yamlout.py": "the rendered file's own text names its command",
    "gideon/host/report.py": "the renderer holds the command forms",
    "gideon/host/rotate.py": "front-door ticket 19: secrets rotation fixes",
    "gideon/host/secrets.py": "front-door ticket 19: secrets fixes",
    "gideon/host/steps/command.py": "the wrapper's own text and its fallback fix name the command",
    "gideon/host/steps/site_dirs.py": "front-door ticket 19: provision step fixes",
    "gideon/host/tls.py": "front-door ticket 19: TLS reload fixes",
    "gideon/host/upgrade.py": "front-door ticket 19: upgrade fixes and next steps",
    "gideon/host/users.py": "front-door ticket 19: users reconcile fixes",
    "gideon/host/weights.py": "front-door ticket 19: model pull fixes",
    "gideon/host/worker.py": "front-door ticket 19: worker verify fixes",
    "gideon/worker/settings.py": "front-door ticket 19: worker settings fixes",
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


class CommandForms(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
