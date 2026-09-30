"""The exported model-upgrade runbook's shape and executable command syntax."""

from __future__ import annotations

import contextlib
import io
import re
import shlex
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from gideon.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = Path("docs/runbooks/model-upgrade.md")
BUILT_IMAGES = Path("docs/runbooks/built-images.md")
_HEADING = re.compile(r"^## (?P<number>[0-9]+)\. (?P<title>.+)$", re.MULTILINE)
_LABEL = re.compile(r"^(Who|Command|Failure):(?P<value>.*)$")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_TOOL = re.compile(r"\bpython3\s+(?:-[A-Za-z]+\s+)*-m\s+(tools(?:\.[A-Za-z_]\w*)+)")
_STAND_INS = {
    "<run id>": "example-run",
    "<tag>": "v9.9.9",
    "<repo>": "example/model",
    "<revision>": "a" * 40,
}


@dataclass(frozen=True, slots=True)
class Finding:
    """One runbook problem at its source line."""

    path: Path
    line: int
    problem: str


def _runbook_text(root: Path) -> str | None:
    try:
        return (root / RUNBOOK).read_text(encoding="utf-8")
    except OSError:
        return None


def rule_headings(root: Path) -> list[Finding]:
    """Require exactly thirteen consecutive numbered sections."""

    source = _runbook_text(root)
    if source is None:
        return [Finding(RUNBOOK, 1, "runbook is missing or unreadable")]
    numbers = [int(match.group("number")) for match in _HEADING.finditer(source)]
    if numbers != list(range(1, 14)):
        return [Finding(RUNBOOK, 1, f"numbered headings are {numbers}; expected 1 through 13")]
    return []


def rule_step_labels(root: Path) -> list[Finding]:
    """Require the three nonempty opening labels in every step section."""

    source = _runbook_text(root)
    if source is None:
        return [Finding(RUNBOOK, 1, "runbook is missing or unreadable")]
    headings = list(_HEADING.finditer(source))
    findings: list[Finding] = []
    for index, heading in enumerate(headings):
        number = int(heading.group("number"))
        if number not in range(3, 12):
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(source)
        body = source[heading.end() : end]
        lines = body.lstrip("\n").splitlines()
        labels = [_LABEL.fullmatch(line) for line in lines[:3]]
        if len(labels) != 3 or any(match is None for match in labels):
            findings.append(Finding(RUNBOOK, source.count("\n", 0, heading.start()) + 1,
                                    f"section {number} must open with Who, Command, Failure"))
            continue
        actual = [match.group(1) for match in labels if match is not None]
        if actual != ["Who", "Command", "Failure"] or any(
            not match.group("value").strip().rstrip("\\").strip()
            for match in labels if match is not None
        ):
            findings.append(Finding(RUNBOOK, source.count("\n", 0, heading.start()) + 1,
                                    f"section {number} has missing, empty, or reordered step labels"))
    return findings


def _code_lines(source: str) -> list[tuple[int, str]]:
    """Read inline code and shell fences while retaining source line numbers."""

    snippets: list[tuple[int, str]] = []
    in_fence = False
    for number, line in enumerate(source.splitlines(), 1):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            snippets.append((number, line.strip()))
        else:
            snippets.extend((number, match.group(1)) for match in _CODE_SPAN.finditer(line))
    return snippets


def _cli_args(snippet: str) -> list[str] | None:
    for placeholder, stand_in in _STAND_INS.items():
        snippet = snippet.replace(placeholder, stand_in)
    try:
        words = shlex.split(snippet)
    except ValueError:
        return None
    if words and words[0] == "sudo":
        words = words[1:]
    if words and words[0] == "gideon":
        return words[1:]
    if not words or words[0] not in {"python3", "/usr/bin/python3"}:
        return None
    words = words[1:]
    if "-m" not in words:
        return None
    module_at = words.index("-m")
    if any(not word.startswith("-") for word in words[:module_at]):
        return None
    if len(words) <= module_at + 1 or words[module_at + 1] != "gideon":
        return None
    return words[module_at + 2 :]


def rule_cli_commands(root: Path) -> list[Finding]:
    """Parse each product command without invoking its handler."""

    source = _runbook_text(root)
    if source is None:
        return [Finding(RUNBOOK, 1, "runbook is missing or unreadable")]
    parser = build_parser()
    findings: list[Finding] = []
    for line, snippet in _code_lines(source):
        args = _cli_args(snippet)
        if args is None:
            continue
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr):
                parser.parse_args(args)
        except SystemExit as exc:
            if exc.code != 0:
                detail = stderr.getvalue().splitlines()[-1] if stderr.getvalue() else "usage error"
                findings.append(Finding(RUNBOOK, line, f"CLI usage error in {snippet!r}: {detail}"))
    return findings


def rule_tool_modules(root: Path) -> list[Finding]:
    """Require every named repository tool to have an executable module."""

    source = _runbook_text(root)
    if source is None:
        return [Finding(RUNBOOK, 1, "runbook is missing or unreadable")]
    findings: list[Finding] = []
    for line, content in enumerate(source.splitlines(), 1):
        for match in _TOOL.finditer(content):
            relative = Path(*match.group(1).split("."))
            if not (root / relative.with_suffix(".py")).is_file() and not (
                root / relative / "__main__.py"
            ).is_file():
                findings.append(Finding(RUNBOOK, line, f"tool module {match.group(1)} does not exist"))
    return findings


def rule_built_images_pointer(root: Path) -> list[Finding]:
    """Require the image runbook to direct model proposals here."""

    try:
        text = (root / BUILT_IMAGES).read_text(encoding="utf-8")
    except OSError:
        return [Finding(BUILT_IMAGES, 1, "built-images runbook is missing or unreadable")]
    if RUNBOOK.as_posix() not in text:
        return [Finding(BUILT_IMAGES, 1, "model-upgrade runbook pointer is missing")]
    return []


def render_findings(findings: list[Finding]) -> str:
    """Name each finding's source line for an actionable failure."""

    return "\n".join(f"{item.path}:{item.line}: {item.problem}" for item in findings)


def _seed(root: Path) -> None:
    """Create a minimal, visibly fictitious valid runbook tree."""

    path = root / RUNBOOK
    path.parent.mkdir(parents=True)
    sections = []
    for number in range(1, 14):
        opening = (
            "Who: Example operator.\\\nCommand: No command yet.\\\nFailure: Example refusal.\n"
            if 3 <= number <= 11 else "Example text.\n"
        )
        sections.append(f"## {number}. Example section\n\n{opening}\n")
    path.write_text("# Example runbook\n\n" + "".join(sections), encoding="utf-8")
    (root / BUILT_IMAGES).write_text(f"See {RUNBOOK.as_posix()}.\n", encoding="utf-8")


class CommittedTree(unittest.TestCase):
    def test_runbook_contract(self) -> None:
        findings = [
            *rule_headings(ROOT),
            *rule_step_labels(ROOT),
            *rule_cli_commands(ROOT),
            *rule_tool_modules(ROOT),
            *rule_built_images_pointer(ROOT),
        ]
        if findings:
            self.fail(render_findings(findings))

    def test_commands_are_found(self) -> None:
        # A floor, so an extraction that silently finds nothing cannot pass the parser rule.
        source = _runbook_text(ROOT)
        assert source is not None
        parsed = [snippet for _, snippet in _code_lines(source) if _cli_args(snippet) is not None]
        self.assertGreaterEqual(len(parsed), 8)


class SeededTrees(unittest.TestCase):
    def test_missing_heading_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            path = root / RUNBOOK
            path.write_text(path.read_text(encoding="utf-8").replace("## 7.", "## 8."), encoding="utf-8")
            findings = rule_headings(root)
        self.assertEqual(len(findings), 1)
        self.assertIn("expected 1 through 13", findings[0].problem)

    def test_empty_step_label_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            path = root / RUNBOOK
            path.write_text(path.read_text(encoding="utf-8").replace("Command: No command yet.", "Command: ", 1), encoding="utf-8")
            findings = rule_step_labels(root)
        self.assertEqual(len(findings), 1)
        self.assertIn("section 3", findings[0].problem)

    def test_unknown_cli_flag_names_its_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            path = root / RUNBOOK
            expected_line = len(path.read_text(encoding="utf-8").splitlines()) + 1
            with path.open("a", encoding="utf-8") as stream:
                stream.write("`sudo python3 -B -m gideon apply --imaginary-option`\n")
            findings = rule_cli_commands(root)
        self.assertEqual(len(findings), 1)
        self.assertIn("imaginary-option", findings[0].problem)
        self.assertEqual(findings[0].line, expected_line)

    def test_missing_tool_module_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            path = root / RUNBOOK
            with path.open("a", encoding="utf-8") as stream:
                stream.write("`python3 -m tools.example_missing`\n")
            findings = rule_tool_modules(root)
        self.assertEqual(len(findings), 1)
        self.assertIn("tools.example_missing", findings[0].problem)

    def test_missing_image_pointer_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            (root / BUILT_IMAGES).write_text("# Example image guide\n", encoding="utf-8")
            findings = rule_built_images_pointer(root)
        self.assertEqual(len(findings), 1)
        self.assertIn("pointer is missing", findings[0].problem)


if __name__ == "__main__":
    unittest.main()
