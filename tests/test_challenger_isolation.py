"""Keep the challenger off ordinary run paths, as tests/test_service_import_boundary.py does for gideon/api."""

import ast
import importlib.util
import unittest
from pathlib import Path

from test_evaluation_run import (
    ROOT,
    ChallengerHost,
    _invoke_engine_command,
    _run_kwargs,
)

from gideon.evaluation import challenger, slices
from gideon.host import nogpu
from gideon.host.sysio import PathLike

_SOURCE_FOLDERS = ("gideon", "tools", "compose/open-webui/functions")


def _sources() -> tuple[Path, ...]:
    # A folder that moved would shrink the walk silently, so its absence is a failure.
    missing = [folder for folder in _SOURCE_FOLDERS if not (ROOT / folder).is_dir()]
    if missing:
        raise AssertionError(f"source folders missing: {missing}")
    return tuple(
        sorted(
            path
            for folder in _SOURCE_FOLDERS
            for path in (ROOT / folder).rglob("*.py")
            if "__pycache__" not in path.parts
        )
    )


class NoChallengerRead(ChallengerHost):
    def __init__(self, *, build_box: bool = False) -> None:
        super().__init__(document="", build_box=build_box)

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        name = str(path)
        self.reads.append(name)
        if name == str(ROOT / challenger.CHALLENGER_PATH):
            raise AssertionError("ordinary run read the challenger")
        return super().read_text(path, encoding=encoding)


class Boundary(unittest.TestCase):
    def test_only_command_imports_loader(self) -> None:
        importers: set[str] = set()
        for path in _sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    uses = any(alias.name == "gideon.evaluation.challenger" for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    package_parts = path.relative_to(ROOT).with_suffix("").parts
                    package = ".".join(package_parts if path.name == "__init__.py" else package_parts[:-1])
                    module = (
                        importlib.util.resolve_name("." * node.level + (node.module or ""), package)
                        if node.level else node.module
                    )
                    uses = module == "gideon.evaluation.challenger" or (
                        module == "gideon.evaluation"
                        and any(alias.name == "challenger" for alias in node.names)
                    )
                else:
                    continue
                if uses:
                    importers.add(path.relative_to(ROOT).as_posix())
        self.assertEqual(importers, {"gideon/evaluation/command.py"})

    def test_only_loader_names_the_committed_path(self) -> None:
        named = {
            path.relative_to(ROOT).as_posix()
            for path in _sources()
            if challenger.CHALLENGER_PATH.as_posix() in path.read_text(encoding="utf-8")
        }
        self.assertEqual(named, {"gideon/evaluation/challenger.py"})
        for folder in ("compose", "images"):
            for path in (ROOT / folder).rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts:
                    self.assertNotIn(challenger.CHALLENGER_PATH.as_posix(), path.read_text(encoding="utf-8", errors="replace"), str(path))

    def test_committed_candidate_is_not_a_slice_prompt(self) -> None:
        result = challenger.load_challenger(ROOT / challenger.CHALLENGER_PATH)
        self.assertTrue(result.ok)
        assert result.config is not None and result.config.challenger is not None
        candidate = result.config.challenger.challenger
        self.assertNotIn(candidate, {spec.judge_prompt for spec in slices.SLICE_RUNNERS.values()})

    def test_ordinary_judging_and_nonjudging_runs_never_read_file(self) -> None:
        for slice_name in ("judge-triples", "extraction"):
            with self.subTest(slice=slice_name):
                host = NoChallengerRead()
                code, stdout, stderr, _observed = _invoke_engine_command(
                    host, slice_name=slice_name, stack_name="production", kind="manual"
                )
                self.assertEqual(code, 0, stdout + stderr)
                self.assertIn("gate:", stdout)
                self.assertNotIn(str(ROOT / challenger.CHALLENGER_PATH), host.reads)

    def test_bounding_refusals_touch_only_the_build_box_marker(self) -> None:
        from test_evaluation_run import _invoke

        cases = ((NoChallengerRead(build_box=True), ["--stack", "production"]),
                 (NoChallengerRead(), ["--stack", "ci"]))
        for host, flags in cases:
            with self.subTest(flags=flags):
                code, _stdout, _stderr = _invoke(
                    ["eval", "run", "--challenger", *flags], **_run_kwargs(host)
                )
                self.assertEqual(code, 1)
                self.assertFalse(host.reads)
                self.assertFalse(host.calls)
                expected = [] if flags[-1] == "production" else [str(nogpu.BUILD_BOX_PATH)]
                self.assertEqual(host.exists_calls, expected)


if __name__ == "__main__":
    unittest.main()
