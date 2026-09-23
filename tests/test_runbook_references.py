"""Every runbook-section reference in product and runbook text resolves.

A reference is ``docs/runbooks/<name>.md §<n>``, the file optionally in
backticks: the form the refusals, the alert templates, and the pin watch's
bump bodies share. It resolves when the runbook holds a ``## <n>.`` heading.
A section that product text names keeps its number, so a runbook appends a
section and never renumbers. A reference into a runbook the export omits is
skipped only where that runbook is absent; a kept runbook's absence is a
finding everywhere.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from tools.exportboundary import absent_from_export, is_excluded

ROOT = Path(__file__).resolve().parent.parent
_TEXT_ROOTS = (
    Path("gideon"),
    Path("tools"),
    Path("compose"),
    Path("config"),
    Path("docs/runbooks"),
)
_TEXT_FILES = (Path("README.md"),)
_SKIP_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
}
_REFERENCE = re.compile(
    r"docs/runbooks/(?P<name>[A-Za-z0-9_-]+\.md)`?[ \t]*"
    r"§(?P<section>[0-9]+[a-z]?)(?![A-Za-z0-9]|\.[0-9])"
)
# The floor counts distinct (runbook, section) pairs, so a regex mistake cannot pass an empty scan.
MINIMUM_REFERENCE_COUNT = 8


@dataclass(frozen=True, slots=True)
class Finding:
    """One actionable runbook-reference finding."""

    path: Path
    line: int
    problem: str
    fix: str


def _finding(root: Path, path: Path, line: int, problem: str, fix: str) -> Finding:
    return Finding(path.relative_to(root), line, problem, fix)


def _text_files(root: Path) -> tuple[Path, ...]:
    paths: set[Path] = set()
    for relative in (*_TEXT_ROOTS, *_TEXT_FILES):
        candidate = root / relative
        if candidate.is_file():
            candidates: list[Path] = [candidate]
        elif candidate.is_dir():
            candidates = list(candidate.rglob("*"))
        else:
            continue
        for path in candidates:
            if path.is_symlink() or not path.is_file():
                continue
            relative_path = path.relative_to(root)
            if any(part in _SKIP_DIRECTORIES for part in relative_path.parts):
                continue
            if is_excluded(relative_path.as_posix()):
                continue
            paths.add(path)
    return tuple(sorted(paths))


def rule_references(root: Path) -> list[Finding]:
    """Find missing runbooks or numbered headings named by scanned text."""

    findings: list[Finding] = []
    references: set[tuple[str, str]] = set()
    for source in _text_files(root):
        try:
            text = source.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            findings.append(
                _finding(
                    root,
                    source,
                    1,
                    f"cannot read text file: {exc}",
                    "Make the scanned text file readable, then run the check again.",
                )
            )
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            for match in _REFERENCE.finditer(line):
                runbook_path = Path("docs/runbooks") / match.group("name")
                section = match.group("section")
                references.add((runbook_path.as_posix(), section))
                target = root / runbook_path
                if absent_from_export(runbook_path, root):
                    continue
                if not target.is_file():
                    findings.append(
                        _finding(
                            root,
                            source,
                            line_number,
                            f"runbook reference names missing {runbook_path.as_posix()}",
                            f"Add {runbook_path.as_posix()} or correct the reference.",
                        )
                    )
                    continue
                try:
                    headings = target.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeDecodeError) as exc:
                    findings.append(
                        _finding(
                            root,
                            source,
                            line_number,
                            f"runbook {runbook_path.as_posix()} cannot be read: {exc}",
                            f"Make {runbook_path.as_posix()} readable UTF-8 text.",
                        )
                    )
                    continue
                heading = re.compile(rf"^##[ \t]+{re.escape(section)}\.(?:[ \t]+|$)")
                if not any(heading.match(candidate) for candidate in headings):
                    findings.append(
                        _finding(
                            root,
                            source,
                            line_number,
                            f"{runbook_path.as_posix()} has no numbered heading {section}",
                            f"Add a ## {section}. heading to {runbook_path.as_posix()} or correct the reference.",
                        )
                    )

    if len(references) < MINIMUM_REFERENCE_COUNT:
        findings.append(
            _finding(
                root,
                root / "README.md",
                1,
                f"found {len(references)} distinct runbook-section references, fewer than the required {MINIMUM_REFERENCE_COUNT}",
                "Keep the source scan finding runbook-section references in product and runbook text.",
            )
        )
    return findings


def render_findings(items: list[Finding]) -> str:
    """Render findings as one actionable line each."""

    return "\n".join(
        f"{item.path}:{item.line}: {item.problem}. Fix: {item.fix}" for item in items
    )


def _seed_references(root: Path, count: int = MINIMUM_REFERENCE_COUNT) -> Path:
    runbooks = root / "docs" / "runbooks"
    runbooks.mkdir(parents=True)
    headings = "".join(f"## {number}. Section\n" for number in range(1, count + 1))
    (runbooks / "guide.md").write_text(headings + "## 1a. More\n", encoding="utf-8")
    source = root / "gideon" / "references.txt"
    source.parent.mkdir(parents=True)
    lines = "".join(f"docs/runbooks/guide.md §{number}\n" for number in range(1, count + 1))
    source.write_text(lines, encoding="utf-8")
    return source


class CommittedTree(unittest.TestCase):
    def test_committed_references_resolve(self) -> None:
        items = rule_references(ROOT)
        if items:
            self.fail(render_findings(items))


class SeededTrees(unittest.TestCase):
    def test_resolving_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed_references(root)
            items = rule_references(root)
        self.assertEqual(items, [])

    def test_missing_runbook_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _seed_references(root)
            with source.open("a", encoding="utf-8") as stream:
                stream.write("docs/runbooks/missing.md §1\n")
            items = rule_references(root)
        self.assertEqual(len(items), 1)
        self.assertIn("missing docs/runbooks/missing.md", items[0].problem)

    def test_missing_numbered_heading_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _seed_references(root)
            with source.open("a", encoding="utf-8") as stream:
                stream.write(f"docs/runbooks/guide.md §{MINIMUM_REFERENCE_COUNT + 1}\n")
            items = rule_references(root)
        self.assertEqual(len(items), 1)
        self.assertIn(f"has no numbered heading {MINIMUM_REFERENCE_COUNT + 1}", items[0].problem)

    def test_backticked_reference_and_lettered_heading_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _seed_references(root)
            with source.open("a", encoding="utf-8") as stream:
                stream.write("See `docs/runbooks/guide.md` §1a.\n")
            items = rule_references(root)
        self.assertEqual(items, [])

    def test_reference_floor_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed_references(root, MINIMUM_REFERENCE_COUNT - 1)
            items = rule_references(root)
        self.assertEqual(len(items), 1)
        self.assertIn("fewer than the required", items[0].problem)

    def test_repeated_reference_counts_once_toward_the_floor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _seed_references(root, 1)
            source.write_text("docs/runbooks/guide.md §1\n" * MINIMUM_REFERENCE_COUNT, encoding="utf-8")
            items = rule_references(root)
        self.assertEqual(len(items), 1)
        self.assertIn("found 1 distinct", items[0].problem)

    def test_excluded_runbook_resolves_when_present(self) -> None:
        reference = "docs/runbooks/pin-watch-app-setup.md"
        self.assertTrue(is_excluded(reference))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _seed_references(root)
            (root / reference).write_text("## 5. Setup\n", encoding="utf-8")
            with source.open("a", encoding="utf-8") as stream:
                stream.write(f"{reference} §5\n")
            items = rule_references(root)
        self.assertEqual(items, [])

    def test_absent_excluded_runbook_is_skipped(self) -> None:
        reference = "docs/runbooks/pin-watch-app-setup.md"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _seed_references(root)
            with source.open("a", encoding="utf-8") as stream:
                stream.write(f"{reference} §5\n")
            self.assertTrue(absent_from_export(reference, root))
            items = rule_references(root)
        self.assertEqual(items, [])

    def test_present_excluded_runbook_without_section_is_reported(self) -> None:
        reference = "docs/runbooks/pin-watch-app-setup.md"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _seed_references(root)
            (root / reference).write_text("## 4. Other\n", encoding="utf-8")
            with source.open("a", encoding="utf-8") as stream:
                stream.write(f"{reference} §5\n")
            items = rule_references(root)
        self.assertEqual(len(items), 1)
        self.assertIn("has no numbered heading 5", items[0].problem)


if __name__ == "__main__":
    unittest.main()
