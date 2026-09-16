"""Contract checks for the public repository's filtered export boundary."""

from __future__ import annotations

import re
import subprocess
import tarfile
import tempfile
import unittest
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlsplit

from tools.exportboundary import EXCLUDED_PREFIXES, in_export_tree, is_excluded

ROOT = Path(__file__).resolve().parent.parent
ATTRIBUTES_PATH = Path(".gitattributes")
# The tracker's own tools and the skill-record mover, whose subject those paths are, and the list itself.
TEXT_RULE_EXEMPT = (
    "tools/tracker.py",
    "tools/cycles.py",
    "tools/pinwatch/skills.py",
    "tools/exportboundary.py",
)
_TEXT_ROOTS = (Path("gideon"), Path("compose"), Path("config"), Path("tools"))
_SKIP_DIRECTORIES = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
}


@dataclass(frozen=True, slots=True)
class Finding:
    """One actionable export-boundary finding."""

    path: Path
    line: int
    problem: str
    fix: str


def _finding(root: Path, path: Path, line: int, problem: str, fix: str) -> Finding:
    return Finding(path.relative_to(root), line, problem, fix)


def _attribute_entries(text: str) -> tuple[tuple[int, str], ...]:
    entries: list[tuple[int, str]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        fields = line.split()
        if "export-ignore" in fields and fields.index("export-ignore") > 0:
            entries.append((line_number, fields[0].rstrip("/")))
    return tuple(entries)


def rule_attributes(root: Path) -> list[Finding]:
    """Find differences between the boundary list and its attributes mirror."""

    path = root / ATTRIBUTES_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [
            _finding(
                root,
                path,
                1,
                ".gitattributes is missing",
                "Create .gitattributes with one export-ignore line per prefix.",
            )
        ]
    entries = _attribute_entries(text)
    mirrored = {prefix for _line, prefix in entries}
    findings: list[Finding] = []
    for prefix in EXCLUDED_PREFIXES:
        if prefix in mirrored:
            continue
        line = len(text.splitlines()) + 1
        rendered = prefix + (
            ""
            if prefix
            in {
                ".github/workflows/acceptance.yml",
                ".github/workflows/pin-watch.yml",
                ".github/dependabot.yml",
            }
            else "/"
        )
        findings.append(
            _finding(
                root,
                path,
                line,
                f"excluded prefix {prefix!r} has no export-ignore line",
                f"Add {rendered} export-ignore to .gitattributes.",
            )
        )
    allowed = set(EXCLUDED_PREFIXES)
    for line, prefix in entries:
        if prefix in allowed:
            continue
        findings.append(
            _finding(
                root,
                path,
                line,
                f"export-ignore line names unknown prefix {prefix!r}",
                "Remove the stray export-ignore line or add the prefix to tools/exportboundary.py.",
            )
        )
    return findings


def rule_archive(root: Path) -> list[Finding]:
    """Find excluded members emitted by Git's worktree-attribute archive."""

    command = ["git", "archive", "--worktree-attributes", "--format=tar", "HEAD"]
    result = subprocess.run(command, cwd=root, capture_output=True, check=False)
    if result.returncode != 0:
        return [
            _finding(
                root,
                root / ".git",
                0,
                f"git archive refused with exit code {result.returncode}",
                "Run git archive --worktree-attributes --format=tar HEAD from the repository root.",
            )
        ]
    findings: list[Finding] = []
    with tarfile.open(fileobj=BytesIO(result.stdout), mode="r:") as archive:
        for member in archive.getmembers():
            if is_excluded(member.name):
                findings.append(
                    _finding(
                        root,
                        root / member.name,
                        0,
                        f"excluded path {member.name!r} is present in the Git archive",
                        "Add the excluded prefix's export-ignore line to .gitattributes.",
                    )
                )
    return findings


def _text_files(root: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    candidates = [root / path for path in _TEXT_ROOTS]
    candidates.append(root / ".github/workflows/ci.yml")
    for candidate in candidates:
        if candidate.is_file():
            paths.append(candidate)
            continue
        if not candidate.is_dir():
            continue
        for path in candidate.rglob("*"):
            relative = path.relative_to(root)
            if (
                not path.is_file()
                or any(part in _SKIP_DIRECTORIES for part in relative.parts)
                or relative.is_relative_to(Path(".claude/worktrees"))
            ):
                continue
            paths.append(path)
    return tuple(sorted(paths))


def _text_pattern(prefix: str) -> re.Pattern[str]:
    if "/" not in prefix and not prefix.startswith("."):
        # A bare top-level name (`bin`) is a path only at a path's start: not
        # after a path character, so `/usr/bin` and `.venv/bin` are not it.
        return re.compile(
            r"(?<![A-Za-z0-9_.\\/'\"-])"
            + re.escape(prefix)
            + r"(?:/|(?![A-Za-z0-9_.-]))"
        )
    if prefix in {
        ".github/workflows/acceptance.yml",
        ".github/workflows/pin-watch.yml",
        ".github/dependabot.yml",
    }:
        return re.compile(re.escape(prefix) + r"(?![A-Za-z0-9_./-])")
    return re.compile(re.escape(prefix) + r"(?:/|(?![A-Za-z0-9_.-]))")


def rule_text(root: Path) -> list[Finding]:
    """Find excluded paths named by text in the scoped kept source files."""

    findings: list[Finding] = []
    exemptions = set(TEXT_RULE_EXEMPT)
    patterns = {prefix: _text_pattern(prefix) for prefix in EXCLUDED_PREFIXES}
    for path in _text_files(root):
        relative = path.relative_to(root).as_posix()
        if relative in exemptions:
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            for prefix, pattern in patterns.items():
                if pattern.search(line) is None:
                    continue
                findings.append(
                    _finding(
                        root,
                        path,
                        line_number,
                        f"text names excluded prefix {prefix!r}",
                        f"Remove or retarget the {prefix!r} reference.",
                    )
                )
    return findings


_INLINE_LINK = re.compile(
    r"\[[^\]\n]*\]\(\s*(?:<(?P<bracket>[^>\n]*)>|(?P<bare>[^)\s]+))"
)
_FENCE = re.compile(r"^\s*(?P<marker>`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(?P<ticks>`+).*?(?P=ticks)", re.DOTALL)


def _without_code(text: str) -> str:
    result: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    for line in text.splitlines(keepends=True):
        fence = _FENCE.match(line)
        if fence_character is not None:
            if (
                fence is not None
                and fence.group("marker")[0] == fence_character
                and len(fence.group("marker")) >= fence_length
            ):
                fence_character = None
                fence_length = 0
            result.append("\n" if line.endswith("\n") else "")
            continue
        if fence is not None:
            marker = fence.group("marker")
            fence_character = marker[0]
            fence_length = len(marker)
            result.append("\n" if line.endswith("\n") else "")
            continue
        result.append(line)
    # The budget test's stripper, keeping a span's newlines so line numbers hold.
    return _INLINE_CODE.sub(lambda match: "\n" * match.group(0).count("\n"), "".join(result))


def _markdown_files(root: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for path in root.rglob("*.md"):
        relative_path = path.relative_to(root)
        if (
            not path.is_file()
            or any(part in _SKIP_DIRECTORIES for part in relative_path.parts)
            or relative_path.is_relative_to(Path(".claude/worktrees"))
        ):
            continue
        relative = relative_path.as_posix()
        if is_excluded(relative):
            continue
        paths.append(path)
    return tuple(sorted(paths))


def rule_links(root: Path) -> list[Finding]:
    """Find inline Markdown links from kept documents into excluded paths."""

    findings: list[Finding] = []
    for path in _markdown_files(root):
        text = _without_code(path.read_text(encoding="utf-8"))
        for line_number, line in enumerate(text.splitlines(), 1):
            for match in _INLINE_LINK.finditer(line):
                target = match.group("bracket") or match.group("bare")
                parsed = urlsplit(target)
                if not target or target.startswith(("#", "//")) or parsed.scheme:
                    continue
                target_path = unquote(parsed.path)
                if not target_path:
                    continue
                resolved = (path.parent / target_path).resolve()
                try:
                    relative = resolved.relative_to(root.resolve()).as_posix()
                except ValueError:
                    continue
                if not is_excluded(relative):
                    continue
                findings.append(
                    _finding(
                        root,
                        path,
                        line_number,
                        f"inline link resolves to excluded path {relative!r}",
                        "Remove the link or replace it with backticked text kept by the export.",
                    )
                )
    return findings


def findings(root: Path) -> list[Finding]:
    """Return all boundary findings in rule order."""

    all_findings: list[Finding] = []
    all_findings.extend(rule_attributes(root))
    all_findings.extend(rule_archive(root))
    all_findings.extend(rule_text(root))
    all_findings.extend(rule_links(root))
    return all_findings


def render_findings(items: list[Finding]) -> str:
    """Render findings in the repository's one-line contract shape."""

    return "\n".join(
        f"{item.path}:{item.line}: {item.problem}. Fix: {item.fix}" for item in items
    )


def _git(root: Path, *arguments: str) -> None:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} failed: {result.stderr}")


class CommittedTree(unittest.TestCase):
    def test_committed_attributes(self) -> None:
        items = rule_attributes(ROOT)
        if items:
            self.fail(render_findings(items))

    def test_committed_archive(self) -> None:
        if in_export_tree(ROOT):
            self.skipTest("an exported tree has no Git history to archive")
        items = rule_archive(ROOT)
        if items:
            self.fail(render_findings(items))

    def test_committed_text(self) -> None:
        items = rule_text(ROOT)
        if items:
            self.fail(render_findings(items))

    def test_committed_links(self) -> None:
        items = rule_links(ROOT)
        if items:
            self.fail(render_findings(items))


class SeededTrees(unittest.TestCase):
    def test_attributes_reports_missing_and_stray_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ATTRIBUTES_PATH).write_text(
                "docs/1-plans/ export-ignore\nunknown export-ignore\n",
                encoding="utf-8",
            )
            items = rule_attributes(root)
        self.assertEqual(items[0].path, ATTRIBUTES_PATH)
        self.assertEqual(items[0].line, 3)
        self.assertIn("Add .scratch/ export-ignore", items[0].fix)
        self.assertEqual(items[-1].line, 2)
        self.assertIn("tools/exportboundary.py", items[-1].fix)

    def test_archive_reports_excluded_member(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".scratch").mkdir()
            (root / ".scratch" / "secret.md").write_text("secret\n", encoding="utf-8")
            (root / ".gitattributes").write_text("*.sh text eol=lf\n", encoding="utf-8")
            _git(root, "init", "-b", "main")
            _git(root, "config", "user.name", "Export Boundary Test")
            _git(root, "config", "user.email", "export-boundary@example.test")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "seed")
            items = rule_archive(root)
        self.assertTrue(items)
        self.assertEqual(items[0].path, Path(".scratch"))
        self.assertEqual(items[0].line, 0)

    def test_text_reports_excluded_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "gideon" / "source.py"
            path.parent.mkdir()
            path.write_text("docs/1-plans/F_example\nscratchpad\n", encoding="utf-8")
            items = rule_text(root)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].path, Path("gideon/source.py"))
        self.assertEqual(items[0].line, 1)
        self.assertIn("Remove or retarget", items[0].fix)

    def test_links_reports_excluded_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "docs" / "kept.md"
            path.parent.mkdir()
            path.write_text("[secret](../.scratch/secret.md)\n", encoding="utf-8")
            items = rule_links(root)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].path, Path("docs/kept.md"))
        self.assertEqual(items[0].line, 1)
        self.assertIn("backticked text", items[0].fix)


class HelperContracts(unittest.TestCase):
    def test_export_tree_detection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / EXCLUDED_PREFIXES[0]).mkdir()
            self.assertFalse(in_export_tree(root))
            empty = root / "empty"
            empty.mkdir()
            self.assertTrue(in_export_tree(empty))
        self.assertEqual(in_export_tree(ROOT), not (ROOT / ".scratch").exists())


if __name__ == "__main__":
    unittest.main()
