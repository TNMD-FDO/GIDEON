"""Locks the provisioning seam (spec §§1.5, 2.3; bootstrap brief Phase B).

``python3 -m gideon host provision`` runs on a bare Ubuntu Server install
where only the standard library and python3-yaml exist — never PyPI. Two
rules, checked against the AST so nothing has to be imported to be caught:

- The bare-host entry chain (``gideon/__init__.py``, ``gideon/__main__.py``,
  ``gideon/cli.py``) may import, **at module level**, only the standard
  library, ``yaml``, and other files in the checked set. Function-level
  imports are exempt there so commands outside the host subtree can grow
  heavier dependencies later without dragging them onto the bare-host path.
- ``gideon/host/`` is self-contained: **every** import, at any depth,
  resolves to the standard library, ``yaml``, or the checked set itself.
- Shared package modules (currently ``gideon/guardrail/``) obey the
  module-level rule: their imports resolve to the standard library, ``yaml``,
  or the checked set itself.
"""

import ast
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "gideon"

ENTRY_CHAIN = frozenset(
    {PACKAGE / "__init__.py", PACKAGE / "__main__.py", PACKAGE / "cli.py"}
)
SHARED_MODULES = frozenset(
    {
        PACKAGE / "guardrail" / "__init__.py",
        PACKAGE / "guardrail" / "grammar.py",
        PACKAGE / "guardrail" / "families.py",
        PACKAGE / "guardrail" / "judge.py",
        PACKAGE / "guardrail" / "writer.py",
        PACKAGE / "guardrail" / "window.py",
    }
)
ALLOWED_EXTERNAL = frozenset(sys.stdlib_module_names) | {"yaml"}


def host_files() -> frozenset[Path]:
    return frozenset((PACKAGE / "host").rglob("*.py"))


def module_name(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def resolve_internal(dotted: str) -> Path | None:
    """The file a gideon-internal dotted name lives in, or None."""
    rel = REPO_ROOT.joinpath(*dotted.split("."))
    for candidate in (rel / "__init__.py", rel.with_suffix(".py")):
        if candidate.is_file():
            return candidate
    return None


def iter_imports(tree: ast.Module, *, module_level_only: bool):
    """Import nodes, optionally skipping those deferred inside functions.

    Class-body imports run at module import time, so classes are always
    descended into; only function bodies defer execution.
    """

    def walk(node: ast.AST):
        for child in ast.iter_child_nodes(node):
            if module_level_only and isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                yield child
            yield from walk(child)

    yield from walk(tree)


def absolute_targets(node: ast.stmt, importer: Path) -> list[str]:
    """The absolute dotted names an import statement pulls in.

    For ``from X import Y`` where Y is a submodule of X, both ``X`` and
    ``X.Y`` are returned; the checker ignores names that resolve to no file
    (attribute imports). Relative imports resolve against the importer's
    package.
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    assert isinstance(node, ast.ImportFrom)
    if node.level == 0:
        base = node.module or ""
    else:
        parts = module_name(importer).split(".")
        if importer.name != "__init__.py":
            parts = parts[:-1]
        parts = parts[: len(parts) - (node.level - 1)]
        if node.module:
            parts += node.module.split(".")
        base = ".".join(parts)
    return [base] + [f"{base}.{alias.name}" for alias in node.names]


def violations(files: frozenset[Path], *, module_level_only: bool) -> list[str]:
    allowed_files = ENTRY_CHAIN | host_files() | SHARED_MODULES
    problems = []
    for path in sorted(files):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in iter_imports(tree, module_level_only=module_level_only):
            for dotted in absolute_targets(node, path):
                top = dotted.partition(".")[0]
                where = f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
                if top != "gideon":
                    if top not in ALLOWED_EXTERNAL:
                        problems.append(
                            f"{where}: `{dotted}` is neither stdlib nor yaml"
                        )
                    continue
                target = resolve_internal(dotted)
                if target is None:
                    continue  # an attribute import; its module was also yielded
                if target not in allowed_files:
                    problems.append(
                        f"{where}: `{dotted}` is outside the bare-host set"
                    )
    return problems


class ImportBoundary(unittest.TestCase):
    def test_checked_files_exist(self) -> None:
        # Guard against a rename quietly emptying the checked set.
        for path in ENTRY_CHAIN:
            self.assertTrue(path.is_file(), f"missing from entry chain: {path}")
        for path in SHARED_MODULES:
            self.assertTrue(path.is_file(), f"missing from shared modules: {path}")
        self.assertTrue(host_files(), "gideon/host/ has no Python files")

    def test_entry_chain_module_level_imports(self) -> None:
        self.assertEqual(violations(ENTRY_CHAIN, module_level_only=True), [])

    def test_host_subtree_is_self_contained(self) -> None:
        self.assertEqual(violations(host_files(), module_level_only=False), [])

    def test_shared_modules_are_module_level_self_contained(self) -> None:
        self.assertEqual(violations(SHARED_MODULES, module_level_only=True), [])
