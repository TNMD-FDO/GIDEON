"""The mounted API package's import boundary and inert parent initializer."""

import ast
import sys
import unittest
from collections.abc import Iterator
from pathlib import Path

from gideon.host.render.api import API_SOURCES

REPO_ROOT = Path(__file__).resolve().parent.parent
PARENT_INITIALIZER = REPO_ROOT / "gideon/__init__.py"
ALLOWED_DEPENDENCIES = frozenset({"starlette", "uvicorn", "httpx", "anyio"})
_FIX = (
    "Keep service imports at module level and limited to the standard library, "
    "the image dependencies, or the declared service package."
)


def service_files() -> frozenset[Path]:
    """Return the Python files under the declared mounted service sources."""

    files: set[Path] = set()
    for source in API_SOURCES:
        path = REPO_ROOT / source
        if path.is_dir():
            files.update(
                candidate
                for candidate in path.rglob("*.py")
                if "__pycache__" not in candidate.parts
            )
        elif path.suffix == ".py":
            files.add(path)
    return frozenset(files)


def module_name(path: Path) -> str:
    """Return the dotted module name represented by a repository path."""

    relative = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def package_name(path: Path) -> str:
    """Return the importing package for a repository module path."""

    name = module_name(path)
    return name if path.name == "__init__.py" else name.rpartition(".")[0]


def _declared_modules() -> frozenset[str]:
    return frozenset(module_name(path) for path in service_files())


DECLARED_MODULES = _declared_modules()


def _imports(tree: ast.Module) -> Iterator[tuple[ast.Import | ast.ImportFrom, bool]]:
    """Yield imports and whether they execute at module import time."""

    def visit(node: ast.AST, module_level: bool) -> Iterator[tuple[ast.Import | ast.ImportFrom, bool]]:
        for child in ast.iter_child_nodes(node):
            child_level = module_level and not isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)
            )
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                yield child, child_level
            yield from visit(child, child_level)

    yield from visit(tree, True)


def _relative_base(node: ast.ImportFrom, path: Path) -> str:
    if node.level == 0:
        return node.module or ""
    parts = package_name(path).split(".") if package_name(path) else []
    remove = node.level - 1
    if remove > len(parts):
        return ""
    parts = parts[: len(parts) - remove]
    if node.module:
        parts.extend(node.module.split("."))
    return ".".join(parts)


def _location(path: Path, node: ast.AST) -> str:
    return f"{path.relative_to(REPO_ROOT)}:{getattr(node, 'lineno', 0)}"


def _error(path: Path, node: ast.AST, reason: str) -> str:
    return f"{_location(path, node)}: {reason} Fix: {_FIX}"


def _is_declared(dotted: str) -> bool:
    return dotted in DECLARED_MODULES


def _internal_import_error(
    path: Path,
    node: ast.Import | ast.ImportFrom,
    dotted: str,
    module_level: bool,
) -> str | None:
    if dotted == "gideon" or (dotted.startswith("gideon.") and not _is_declared(dotted)):
        return _error(
            path,
            node,
            f"`{dotted}` is outside the declared service package or is a bare "
            "gideon package name.",
        )
    if not module_level:
        return _error(path, node, f"`{dotted}` is a deferred import inside gideon.")
    return None


def import_violations(path: Path, source: str) -> list[str]:
    """Return import-boundary violations for one path and source text."""

    tree = ast.parse(source, filename=str(path))
    problems: list[str] = []
    for node, module_level in _imports(tree):
        if isinstance(node, ast.Import):
            targets = [(alias.name, alias.name) for alias in node.names]
        else:
            base = _relative_base(node, path)
            targets = [(base, base)] if base else []
            if base == "gideon" and node.level:
                problems.append(_error(path, node, "a relative import reaches the bare gideon package."))
                continue
            if base == "gideon" and node.level == 0:
                targets = [
                    (f"{base}.{alias.name}", f"{base}.{alias.name}")
                    for alias in node.names
                ]
            elif base.startswith("gideon"):
                targets = [(base, base)]
        for dotted, display in targets:
            top = dotted.partition(".")[0]
            if top != "gideon":
                if top not in sys.stdlib_module_names and top not in ALLOWED_DEPENDENCIES:
                    problems.append(
                        _error(
                            path,
                            node,
                            f"`{display}` is neither stdlib nor an allowed image dependency.",
                        )
                    )
                continue
            violation = _internal_import_error(path, node, dotted, module_level)
            if violation is not None:
                problems.append(violation)
    return problems


def initializer_violations(path: Path, source: str) -> list[str]:
    """Return violations of the inert parent-package initializer contract."""

    tree = ast.parse(source, filename=str(path))
    body = list(tree.body)
    valid_docstring = bool(
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    )
    valid_version = bool(
        len(body) == 2
        and isinstance(body[1], ast.Assign)
        and len(body[1].targets) == 1
        and isinstance(body[1].targets[0], ast.Name)
        and body[1].targets[0].id == "__version__"
        and isinstance(body[1].value, ast.Constant)
        and isinstance(body[1].value.value, str)
    )
    if valid_docstring and valid_version:
        return []
    line = body[-1].lineno if body else 1
    return [
        f"{path.relative_to(REPO_ROOT)}:{line}: gideon/__init__.py must contain only "
        "a module docstring and one string __version__ assignment. Fix: remove "
        "the added statement from the parent initializer."
    ]


class ServiceImportBoundary(unittest.TestCase):
    def test_declared_service_imports_and_parent_initializer(self) -> None:
        self.assertTrue(service_files())
        for path in sorted(service_files()):
            with self.subTest(path=path):
                self.assertEqual(import_violations(path, path.read_text()), [])
        self.assertEqual(
            initializer_violations(PARENT_INITIALIZER, PARENT_INITIALIZER.read_text()), []
        )

    def test_unlisted_import_at_module_and_function_scope_is_rejected(self) -> None:
        path = REPO_ROOT / "gideon/api/app.py"
        for source in ("import not_in_the_image\n", "def deferred() -> None:\n    import not_in_the_image\n"):
            with self.subTest(source=source):
                errors = import_violations(path, source)
                self.assertTrue(errors)
                self.assertIn("app.py", errors[0])
                self.assertIn("Fix:", errors[0])

    def test_deferred_gideon_import_is_rejected(self) -> None:
        path = REPO_ROOT / "gideon/api/app.py"
        errors = import_violations(path, "def deferred() -> None:\n    from .settings import Settings\n")
        self.assertTrue(errors)
        self.assertIn("deferred import", errors[0])
        self.assertIn(":2:", errors[0])

    def test_bare_package_import_is_rejected(self) -> None:
        path = REPO_ROOT / "gideon/api/app.py"
        errors = import_violations(path, "from gideon import host\n")
        self.assertTrue(errors)
        self.assertIn("bare gideon package", errors[0])

    def test_deferred_dependency_import_is_allowed(self) -> None:
        path = REPO_ROOT / "gideon/api/app.py"
        self.assertEqual(
            import_violations(path, "def deferred() -> None:\n    import httpx\n"), []
        )

    def test_parent_initializer_rejects_an_added_statement(self) -> None:
        source = PARENT_INITIALIZER.read_text() + "\nanswer = 1\n"
        errors = initializer_violations(PARENT_INITIALIZER, source)
        self.assertTrue(errors)
        self.assertIn("initializer", errors[0])
        self.assertIn("Fix:", errors[0])


if __name__ == "__main__":
    unittest.main()
