"""Contracts for the operator card's commands, pointers, and discoverability."""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import shlex
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from gideon.cli import SITUATIONS, build_parser, start_screen

ROOT = Path(__file__).resolve().parent.parent
CARD = Path("docs/runbooks/start-here.md")
README = Path("README.md")
WORD_CAP = 800
MINIMUM_COMMANDS = 8
MINIMUM_ITEMS = 2
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_REFERENCE = re.compile(
    r"docs/runbooks/(?P<name>[A-Za-z0-9_-]+\.md)`?[ \t]*"
    r"§(?P<section>[0-9]+[a-z]?)(?![A-Za-z0-9]|\.[0-9])"
)
_HEADING = re.compile(r"^##[ \t]+(?P<section>[0-9]+[a-z]?)\.(?:[ \t]+|$)", re.MULTILINE)
_LINK_TARGET = re.compile(r"\]\((?P<target>[^)]+)\)")
_STAND_INS = {
    "<tag>": "v9.9.9",
    "<label>": "example-set",
    "<YYYY-MM>": "2026-09",
    "<dir>": "/root/example-packet",
}


@dataclass(frozen=True, slots=True)
class Finding:
    """One card problem at its source line."""

    path: Path
    line: int
    problem: str


def _card_text(root: Path) -> str | None:
    try:
        return (root / CARD).read_text(encoding="utf-8")
    except OSError:
        return None


def _code_lines(source: str) -> list[tuple[int, str]]:
    """Read inline code and shell fences with their source lines."""

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


def _commands(source: str) -> list[tuple[int, str, list[str]]]:
    return [
        (line, snippet, args)
        for line, snippet in _code_lines(source)
        if (args := _cli_args(snippet)) is not None
    ]


def _parse(parser: argparse.ArgumentParser, args: list[str]) -> tuple[argparse.Namespace | None, str]:
    """Parse one argv quietly: the namespace, or the usage error's last line (empty for help)."""

    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            return parser.parse_args(args), ""
    except SystemExit as exc:
        if exc.code == 0:
            return None, ""
        return None, stderr.getvalue().splitlines()[-1] if stderr.getvalue() else "usage error"


def rule_commands(root: Path) -> list[Finding]:
    """Parse each complete product command without invoking its handler."""

    source = _card_text(root)
    if source is None:
        return [Finding(CARD, 1, "card is missing or unreadable")]
    commands = _commands(source)
    findings: list[Finding] = []
    parser = build_parser()
    for line, snippet, args in commands:
        parsed, error = _parse(parser, args)
        if error:
            findings.append(Finding(CARD, line, f"CLI usage error in {snippet!r}: {error}"))
        elif parsed is not None and getattr(parsed, "landing", None):
            findings.append(Finding(CARD, line, f"card names stub command {parsed.command_path}"))
    if len(commands) < MINIMUM_COMMANDS:
        findings.append(Finding(CARD, 1, f"found {len(commands)} commands; expected at least {MINIMUM_COMMANDS}"))
    return findings


def rule_pointers(root: Path) -> list[Finding]:
    """Resolve every numbered-section pointer on an item bullet."""

    source = _card_text(root)
    if source is None:
        return [Finding(CARD, 1, "card is missing or unreadable")]
    findings: list[Finding] = []
    in_section = False
    items = 0
    for number, line in enumerate(source.splitlines(), 1):
        if line.startswith("## "):
            in_section = True
        if not in_section or not line.startswith("- "):
            continue
        items += 1
        references = list(_REFERENCE.finditer(line))
        if not references:
            findings.append(Finding(CARD, number, "item has no runbook-section pointer"))
            continue
        for match in references:
            name = match.group("name")
            section = match.group("section")
            target = root / CARD.parent / name
            if not target.is_file():
                findings.append(Finding(CARD, number, f"runbook {name} is missing"))
            else:
                try:
                    headings = {heading.group("section") for heading in _HEADING.finditer(target.read_text(encoding="utf-8"))}
                except (OSError, UnicodeDecodeError):
                    findings.append(Finding(CARD, number, f"runbook {name} is unreadable"))
                else:
                    if section not in headings:
                        findings.append(Finding(CARD, number, f"{name} has no heading {section}"))
            if match.start() > 0 and line[match.start() - 1] == "[":
                link = _LINK_TARGET.match(line, match.end())
                if link is None or link.group("target") != name:
                    findings.append(Finding(CARD, number, f"link for {name} must target {name}"))
    if items < MINIMUM_ITEMS:
        findings.append(Finding(CARD, 1, f"found {items} items; expected at least {MINIMUM_ITEMS}"))
    return findings


def rule_start_screen(root: Path) -> list[Finding]:
    """Keep every start-screen command path on the card."""

    source = _card_text(root)
    if source is None:
        return [Finding(CARD, 1, "card is missing or unreadable")]
    parser = build_parser()
    paths: set[str] = set()
    for _line, _snippet, args in _commands(source):
        parsed, _error = _parse(parser, args)
        if parsed is not None and getattr(parsed, "command_path", None) and not getattr(parsed, "landing", None):
            paths.add(parsed.command_path)
    expected = {path for situation in SITUATIONS for path, _hint in situation.entries}
    return [Finding(CARD, 1, f"start-screen command {path} is absent") for path in sorted(expected - paths)]


def rule_size(root: Path) -> list[Finding]:
    """Hold the card to one page of text."""

    source = _card_text(root)
    if source is None:
        return [Finding(CARD, 1, "card is missing or unreadable")]
    count = 0
    for number, line in enumerate(source.splitlines(), 1):
        count += len(line.split())
        if count > WORD_CAP:
            return [Finding(CARD, number, f"card exceeds {WORD_CAP} words")]
    return []


def rule_root_line(root: Path) -> list[Finding]:
    """Keep the root and sudo policy clear on the card."""

    source = _card_text(root)
    if source is None:
        return [Finding(CARD, 1, "card is missing or unreadable")]
    collapsed = " ".join(source.split())
    missing = [phrase for phrase in ("runs as root", "office's policy") if phrase not in collapsed]
    return [Finding(CARD, 1, f"card lacks {phrase!r}") for phrase in missing]


def rule_links(root: Path) -> list[Finding]:
    """Keep the card discoverable from the documentation and start screen."""

    try:
        readme = (root / README).read_text(encoding="utf-8")
    except OSError:
        readme = ""
    findings: list[Finding] = []
    if CARD.as_posix() not in readme:
        findings.append(Finding(README, 1, "README does not name the card"))
    if root == ROOT and CARD.as_posix() not in start_screen():
        cli_source = (root / "gideon/cli.py").read_text(encoding="utf-8")
        line = next((number for number, text in enumerate(cli_source.splitlines(), 1) if "_EVERYTHING_ELSE" in text), 1)
        findings.append(Finding(Path("gideon/cli.py"), line, "start screen does not name the card"))
    return findings


def render_findings(findings: list[Finding]) -> str:
    """Render each source location and problem."""

    return "\n".join(f"{item.path}:{item.line}: {item.problem}" for item in findings)


def _seed(root: Path) -> None:
    """Create two numbered card sections and a fictitious runbook."""

    runbooks = root / CARD.parent
    runbooks.mkdir(parents=True)
    (runbooks / "example.md").write_text(
        "# Example runbook\n\n## 1. First example\n\nExample step.\n\n## 2. Second example\n\nExample step.\n",
        encoding="utf-8",
    )
    examples = [
        f"gideon {path} {hint}".strip()
        for situation in SITUATIONS
        for path, hint in situation.entries
    ]
    examples = [
        example.replace("staging|target", "staging").replace("<tag>", "v9.9.9").replace("<dir>", "/root/example-packet")
        for example in examples
    ]
    halfway = len(examples) // 2
    first = ", ".join(f"`{example}`" for example in examples[:halfway])
    second = ", ".join(f"`{example}`" for example in examples[halfway:])
    (root / CARD).write_text(
        "# Example card\n\nEvery command runs as root; sudo follows the office's policy.\n\n"
        "## 1. First example\n\n"
        f"- Example commands: {first}. [docs/runbooks/example.md §1](example.md)\n\n"
        "## 2. Second example\n\n"
        f"- Example commands: {second}. [docs/runbooks/example.md §2](example.md)\n",
        encoding="utf-8",
    )
    (root / README).write_text(f"Start: {CARD.as_posix()}\n", encoding="utf-8")


def _line_of(root: Path, needle: str) -> int:
    return next(number for number, line in enumerate((root / CARD).read_text(encoding="utf-8").splitlines(), 1) if needle in line)


class CommittedTree(unittest.TestCase):
    def test_card_contract(self) -> None:
        findings = [
            *rule_commands(ROOT),
            *rule_pointers(ROOT),
            *rule_start_screen(ROOT),
            *rule_size(ROOT),
            *rule_root_line(ROOT),
            *rule_links(ROOT),
        ]
        if findings:
            self.fail(render_findings(findings))


class SeededTrees(unittest.TestCase):
    def test_unknown_command_names_its_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            with (root / CARD).open("a", encoding="utf-8") as stream:
                stream.write("\n```bash\ngideon example-missing\n```\n")
            line = _line_of(root, "gideon example-missing")
            findings = rule_commands(root)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].line, line)
        self.assertIn("usage error", findings[0].problem)

    def test_stub_command_names_its_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            card = root / CARD
            card.write_text(card.read_text(encoding="utf-8").replace("`gideon status`", "`gideon corpus install example-lock`", 1), encoding="utf-8")
            line = _line_of(root, "gideon corpus install")
            findings = rule_commands(root)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].line, line)
        self.assertIn("stub command", findings[0].problem)

    def test_command_floor_names_its_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            card = root / CARD
            card.write_text(re.sub(r"`gideon [^`]+`", "Example command", card.read_text(encoding="utf-8")), encoding="utf-8")
            findings = rule_commands(root)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].line, 1)
        self.assertIn("expected at least", findings[0].problem)

    def test_renamed_heading_names_item_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            runbook = root / CARD.parent / "example.md"
            runbook.write_text(runbook.read_text(encoding="utf-8").replace("## 2.", "## 3."), encoding="utf-8")
            line = _line_of(root, "§2")
            findings = rule_pointers(root)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].line, line)
        self.assertIn("no heading 2", findings[0].problem)

    def test_item_without_pointer_names_its_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            card = root / CARD
            card.write_text(card.read_text(encoding="utf-8").replace("[docs/runbooks/example.md §2](example.md)", "Example guide", 1), encoding="utf-8")
            line = _line_of(root, "Example guide")
            findings = rule_pointers(root)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].line, line)
        self.assertIn("no runbook-section pointer", findings[0].problem)

    def test_wrong_link_target_names_item_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            card = root / CARD
            card.write_text(card.read_text(encoding="utf-8").replace("§2](example.md)", "§2](other.md)", 1), encoding="utf-8")
            line = _line_of(root, "other.md")
            findings = rule_pointers(root)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].line, line)
        self.assertIn("must target example.md", findings[0].problem)

    def test_item_floor_names_its_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            card = root / CARD
            card.write_text(card.read_text(encoding="utf-8").replace("- Example commands:", "Example commands:"), encoding="utf-8")
            findings = rule_pointers(root)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].line, 1)
        self.assertIn("expected at least", findings[0].problem)

    def test_missing_start_screen_command_names_its_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            card = root / CARD
            card.write_text(card.read_text(encoding="utf-8").replace("`gideon status`", "", 1), encoding="utf-8")
            findings = rule_start_screen(root)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].line, 1)
        self.assertIn("status is absent", findings[0].problem)

    def test_size_cap_names_crossing_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            card = root / CARD
            with card.open("a", encoding="utf-8") as stream:
                stream.write("\n" + "example " * WORD_CAP + "\n")
            line = len(card.read_text(encoding="utf-8").splitlines())
            findings = rule_size(root)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].line, line)
        self.assertIn("exceeds", findings[0].problem)

    def test_missing_root_line_names_its_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            card = root / CARD
            card.write_text(card.read_text(encoding="utf-8").replace("runs as root", "runs as example"), encoding="utf-8")
            findings = rule_root_line(root)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].line, 1)
        self.assertIn("runs as root", findings[0].problem)

    def test_missing_readme_link_names_its_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            (root / README).write_text("# Example project\n", encoding="utf-8")
            findings = rule_links(root)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, README)
        self.assertEqual(findings[0].line, 1)


if __name__ == "__main__":
    unittest.main()
