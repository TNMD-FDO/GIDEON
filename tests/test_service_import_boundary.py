"""Mounted service import boundaries and the inert parent initializer."""

import ast
import sys
import unittest
from collections.abc import Iterator
from pathlib import Path

from gideon import guardrail
from gideon.host.render.api import API_SERVICE_NAME
from gideon.host.render.services import declared_sources

REPO_ROOT = Path(__file__).resolve().parent.parent
PARENT_INITIALIZER = REPO_ROOT / "gideon/__init__.py"
ALLOWED_DEPENDENCIES = frozenset(
    {"starlette", "uvicorn", "httpx", "anyio", "procrastinate", "lxml",
     guardrail.TRIP_DRIVER_MODULE}
)
_FIX = (
    "Keep service imports at module level and limited to the standard library, "
    "the image dependencies, or the service's declared sources."
)


def service_files(sources: tuple[str, ...]) -> frozenset[Path]:
    """Return Python files under one service's declared sources."""

    files: set[Path] = set()
    for source in sources:
        path = REPO_ROOT / source
        if path.is_dir():
            files.update(
                candidate
                for candidate in path.rglob("*.py")
                if "__pycache__" not in candidate.parts
            )
        elif path.suffix == ".py" and path.is_file():
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


def declared_modules(sources: tuple[str, ...]) -> frozenset[str]:
    """Return module names from one service's declared files."""

    return frozenset(module_name(path) for path in service_files(sources))


def api_modules() -> frozenset[str]:
    """Return gideon-api's declared modules, the planted cases' set."""

    return declared_modules(declared_sources()[API_SERVICE_NAME])


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


def _internal_import_error(
    path: Path,
    node: ast.Import | ast.ImportFrom,
    dotted: str,
    module_level: bool,
    modules: frozenset[str],
) -> str | None:
    if dotted == "gideon" or (dotted.startswith("gideon.") and dotted not in modules):
        return _error(
            path,
            node,
            f"`{dotted}` is outside the service's declared sources or is a bare "
            "gideon package name.",
        )
    if not module_level:
        return _error(path, node, f"`{dotted}` is a deferred import inside gideon.")
    return None


def import_violations(path: Path, source: str, modules: frozenset[str]) -> list[str]:
    """Return import violations against one service's declared modules."""

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
            violation = _internal_import_error(path, node, dotted, module_level, modules)
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
    def test_each_declaring_service_imports_only_its_sources(self) -> None:
        for service, sources in declared_sources().items():
            files = service_files(sources)
            with self.subTest(service=service):
                self.assertTrue(files)
            modules = declared_modules(sources)
            for path in sorted(files):
                with self.subTest(service=service, path=path):
                    self.assertEqual(import_violations(path, path.read_text(), modules), [])

    def test_parent_initializer_contains_only_version_and_docstring(self) -> None:
        self.assertEqual(
            initializer_violations(PARENT_INITIALIZER, PARENT_INITIALIZER.read_text()), []
        )

    def test_unlisted_import_at_module_and_function_scope_is_rejected(self) -> None:
        path = REPO_ROOT / "gideon/api/app.py"
        modules = api_modules()
        for source in ("import not_in_the_image\n", "def deferred() -> None:\n    import not_in_the_image\n"):
            with self.subTest(source=source):
                errors = import_violations(path, source, modules)
                self.assertTrue(errors)
                self.assertIn("app.py", errors[0])
                self.assertIn("Fix:", errors[0])

    def test_deferred_gideon_import_is_rejected(self) -> None:
        path = REPO_ROOT / "gideon/api/app.py"
        modules = api_modules()
        errors = import_violations(path, "def deferred() -> None:\n    from .settings import Settings\n", modules)
        self.assertTrue(errors)
        self.assertIn("deferred import", errors[0])
        self.assertIn(":2:", errors[0])

    def test_bare_package_import_is_rejected(self) -> None:
        path = REPO_ROOT / "gideon/api/app.py"
        modules = api_modules()
        errors = import_violations(path, "from gideon import host\n", modules)
        self.assertTrue(errors)
        self.assertIn("bare gideon package", errors[0])

    def test_deferred_dependency_import_is_allowed(self) -> None:
        path = REPO_ROOT / "gideon/api/app.py"
        modules = api_modules()
        self.assertEqual(
            import_violations(path, "def deferred() -> None:\n    import httpx\n", modules), []
        )

    def test_trip_driver_is_an_ordinary_image_dependency(self) -> None:
        path = REPO_ROOT / "gideon/api/app.py"
        source = f"import {guardrail.TRIP_DRIVER_MODULE}\n"
        modules = api_modules()
        self.assertEqual(import_violations(path, source, modules), [])

    def test_shared_judge_import_requires_its_own_declared_source(self) -> None:
        path = REPO_ROOT / "gideon/api/app.py"
        sources = declared_sources()[API_SERVICE_NAME]
        source = "from gideon.guardrail import judge\n"
        self.assertEqual(import_violations(path, source, declared_modules(sources)), [])
        api_only = ("gideon/api",)
        errors = import_violations(path, source, declared_modules(api_only))
        self.assertTrue(errors)
        self.assertIn("gideon.guardrail", errors[0])
        self.assertIn("service's declared sources", errors[0])

    def test_parent_initializer_rejects_an_added_statement(self) -> None:
        source = PARENT_INITIALIZER.read_text() + "\nanswer = 1\n"
        errors = initializer_violations(PARENT_INITIALIZER, source)
        self.assertTrue(errors)
        self.assertIn("initializer", errors[0])
        self.assertIn("Fix:", errors[0])


if __name__ == "__main__":
    unittest.main()
