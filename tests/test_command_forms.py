"""Account for literal product commands in package source strings."""

import argparse
import ast
import re
import unittest
from pathlib import Path

from gideon.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent
ENTRIES = {
    "gideon/evaluation/command.py": "front-door ticket 20: eval command fixes and retries",
    "gideon/evaluation/record.py": "front-door ticket 20: eval record fixes",
    "gideon/evaluation/turns/access.py": "front-door ticket 20: turn access checks",
    "gideon/evaluation/turns/door.py": "front-door ticket 20: turn door checks",
    "gideon/host/alerts.py": "front-door ticket 19: alerts test fixes",
    "gideon/host/apply.py": "front-door ticket 19: apply fixes and rows",
    "gideon/host/backup.py": "front-door ticket 18: backup run and push fixes",
    "gideon/host/backuproots.py": "front-door ticket 18: backup roots reader fixes",
    "gideon/host/backupset.py": "front-door ticket 18: backup set reader fixes",
    "gideon/host/cas.py": "front-door ticket 19: content-addressed store fixes",
    "gideon/host/drill.py": "front-door ticket 18: backup drill fixes",
    "gideon/host/engine.py": "front-door ticket 20: engine verify fixes",
    "gideon/host/enginesample.py": "front-door ticket 20: engine sample fixes",
    "gideon/host/grafana.py": "the test alert's summary names the command that sent it",
    "gideon/host/install.py": "front-door ticket 19: install fixes",
    "gideon/host/nogpu.py": "the mode marker's own text names the declaring command",
    "gideon/host/pgbackrest.py": "front-door ticket 18: pgBackRest reader fixes",
    "gideon/host/preflight.py": "front-door ticket 19: preflight fixes",
    "gideon/host/provision.py": "front-door ticket 19: provision fixes",
    "gideon/host/registry.py": "front-door ticket 19: registry mirror fixes",
    "gideon/host/render/command.py": "front-door ticket 19: render command fixes",
    "gideon/host/render/facts.py": "front-door ticket 19: render input fixes",
    "gideon/host/render/grafana.py": "the page email tells an operator on the box what to run",
    "gideon/host/render/systemd.py": "the unit's ExecStart runs the long form from its WorkingDirectory",
    "gideon/host/render/yamlout.py": "the rendered file's own text names its command",
    "gideon/host/report.py": "the renderer holds the command forms",
    "gideon/host/restore.py": "front-door ticket 18: restore fixes and next steps",
    "gideon/host/rotate.py": "front-door ticket 19: secrets rotation fixes",
    "gideon/host/secrets.py": "front-door ticket 19: secrets fixes",
    "gideon/host/steps/command.py": "the wrapper's own text and its fallback fix name the command",
    "gideon/host/steps/site_dirs.py": "front-door ticket 19: provision step fixes",
    "gideon/host/tls.py": "front-door ticket 19: TLS reload fixes",
    "gideon/host/upgrade.py": "front-door ticket 19: upgrade fixes and next steps",
    "gideon/host/users.py": "front-door ticket 19: users reconcile fixes",
    "gideon/host/weights.py": "front-door ticket 19: model pull fixes",
    "gideon/host/worker.py": "front-door ticket 19: worker verify fixes",
    "gideon/improvement/owuisnapshot.py": "front-door ticket 20: snapshot reader fixes",
    "gideon/improvement/packet.py": "front-door ticket 20: candidate packet fixes",
    "gideon/improvement/proposals.py": "front-door ticket 20: proposals report fixes",
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
