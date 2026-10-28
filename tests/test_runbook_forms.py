"""Hold runbook command spellings to the places where each can be used."""

from __future__ import annotations

import re
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from tools.exportboundary import absent_from_export

ROOT = Path(__file__).resolve().parent.parent
RUNBOOKS = Path("docs/runbooks")
INSTALL = "install-upgrade.md"
CARD = "start-here.md"

# Each phrase identifies the line whose long form is needed at that checkout.
LONG_FORM_ENTRIES = (
    ("built-images.md", "python3 -m gideon registry mirror", "the proposal branch carries the image pin being mirrored"),
    ("built-images.md", "sudo python3 -m gideon eval run --slice extraction", "the rebuilt proposal image is checked before merge"),
    ("model-upgrade.md", "Command: `sudo python3 -B -m gideon render --diff`, then `sudo python3 -B -m gideon apply` from the proposal checkout.", "the proposal checkout must render and apply its own candidate"),
    ("model-upgrade.md", "sudo python3 -B -m gideon engine verify", "the proposal checkout supplies the candidate engine"),
    ("model-upgrade.md", "sudo python3 -B -m gideon eval run --slice guardrails --decision", "the decision runs against the proposal checkout"),
    ("model-upgrade.md", "sudo python3 -B -m gideon eval run --slice general-smoke", "the smoke run sits beside the proposal decision"),
    ("model-upgrade.md", "From the proposal checkout at the tag, `sudo python3 -B -m gideon render --diff`", "the tagged proposal is rendered before the installed release moves"),
    ("backup-restore.md", "sudo python3 -m gideon host provision` — the `host-tools`", "the first provision installs the command and prints the identity"),
    ("backup-restore.md", "sudo python3 -m gideon host provision` — a **new**", "the rebuilt box has not yet installed the command"),
    ("install-upgrade.md", "sudo python3 -m gideon host provision --no-gpu", "the first provision declares the no-GPU mode"),
    ("install-upgrade.md", "Run sudo python3 -m gideon backup drill", "the install script prints its phase fix in the long form"),
    ("install-upgrade.md", "# On a first-provision box, use instead: sudo python3 -m gideon host provision", "the first provision has no installed command"),
    ("install-upgrade.md", "sudo python3 -m gideon uninstall", "the first removal run took the installed command away"),
    ("ci-runner.md", "sudo python3 -B -m gideon host provision --only gh-runner` from a tree at", "the repair runs from a checkout at main"),
)

UPGRADE_SCRIPT_ENTRIES = (
    (INSTALL, "sudo ./upgrade.sh <tag>` from the checkout", "the checkout script is the same upgrade command"),
)

_LONG_FORM = re.compile(r"(?<![\w/])(?:/usr/bin/)?python3(?:[ \t]+-[^\s`]+)*[ \t\n]+-m[ \t\n]+gideon(?![\w-])")
_UPGRADE_SCRIPT = re.compile(r"upgrade\.sh")
_HEADING = re.compile(r"^## [0-9]+\.")


@dataclass(frozen=True, slots=True)
class Finding:
    """A runbook problem with its source and repair."""

    path: Path
    line: int
    problem: str
    fix: str


def render_findings(findings: list[Finding]) -> str:
    """Render one source location, problem, and fix per line."""

    return "\n".join(f"{item.path}:{item.line}: {item.problem}; Fix: {item.fix}" for item in findings)


def _read_runbooks(root: Path) -> tuple[dict[str, list[str]], list[Finding]]:
    runbooks: dict[str, list[str]] = {}
    findings: list[Finding] = []
    for path in sorted((root / RUNBOOKS).glob("*.md")):
        relative = RUNBOOKS / path.name
        try:
            runbooks[path.name] = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            findings.append(Finding(relative, 1, "runbook is unreadable", "restore a readable runbook"))
    return runbooks, findings


def _matches(lines: list[str], pattern: re.Pattern[str], *, joined: bool = False) -> set[int]:
    found: set[int] = set()
    for index, line in enumerate(lines):
        window = line + ("\n" + lines[index + 1] if joined and index + 1 < len(lines) else "")
        if any(match.start() < len(line) for match in pattern.finditer(window)):
            found.add(index + 1)
    return found


def _section(lines: list[str], number: int) -> tuple[int, int] | None:
    heading = re.compile(rf"^## {number}\.(?:[ \t]|$)")
    start = next((index for index, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        return None
    end = next((index for index in range(start + 1, len(lines)) if _HEADING.match(lines[index])), len(lines))
    return start, end


def _named_lines(
    root: Path, runbooks: dict[str, list[str]], matches: dict[str, set[int]], entries: tuple[tuple[str, str, str], ...]
) -> tuple[set[tuple[str, int]], list[Finding]]:
    kept: set[tuple[str, int]] = set()
    findings: list[Finding] = []
    for name, phrase, _reason in entries:
        if absent_from_export(RUNBOOKS / name, root):
            continue
        candidates = [
            number
            for number, line in enumerate(runbooks.get(name, []), 1)
            if phrase in line and number in matches.get(name, set())
        ]
        if len(candidates) == 1:
            kept.add((name, candidates[0]))
        else:
            problem = "stale entry" if not candidates else "ambiguous entry"
            findings.append(Finding(RUNBOOKS / name, candidates[0] if candidates else 1, f"{problem} {phrase!r}", "name exactly one matching command line and give its reason"))
    return kept, findings


def rule_long_form(root: Path, kept: tuple[tuple[str, str, str], ...] = LONG_FORM_ENTRIES) -> list[Finding]:
    """Find long-form commands outside the first provision and named checkout runs."""

    runbooks, findings = _read_runbooks(root)
    matches = {name: _matches(lines, _LONG_FORM, joined=True) for name, lines in runbooks.items()}
    allowed, named_findings = _named_lines(root, runbooks, matches, kept)
    findings.extend(named_findings)

    install = runbooks.get(INSTALL, [])
    first = _section(install, 1)
    step_three = next((index for index in range(first[0] + 1, len(install)) if install[index].startswith("3. ")), None) if first else None
    if first is None or step_three is None:
        findings.append(Finding(RUNBOOKS / INSTALL, 1, "first-provision region moved", "restore the first section and its step 3 boundary"))
    elif not any(number <= step_three for number in matches[INSTALL]):
        findings.append(Finding(RUNBOOKS / INSTALL, first[0] + 1, "first-provision region has no long form", "restore a long-form command or update the reader"))
    else:
        allowed.update((INSTALL, number) for number in matches[INSTALL] if number <= step_three)

    card = runbooks.get(CARD, [])
    first_heading = next((index for index, line in enumerate(card) if _HEADING.match(line)), None)
    if first_heading is None:
        findings.append(Finding(RUNBOOKS / CARD, 1, "card head region moved", "restore the first numbered section"))
    elif not any(number <= first_heading for number in matches[CARD]):
        findings.append(Finding(RUNBOOKS / CARD, 1, "card head has no long form", "restore a long-form command or update the reader"))
    else:
        allowed.update((CARD, number) for number in matches[CARD] if number <= first_heading)

    for name, numbers in matches.items():
        for number in sorted(numbers):
            if (name, number) not in allowed:
                findings.append(Finding(RUNBOOKS / name, number, "long-form command outside a kept line", "type gideon … or name this line with its reason"))
    return findings


def rule_upgrade_script(root: Path, kept: tuple[tuple[str, str, str], ...] = UPGRADE_SCRIPT_ENTRIES) -> list[Finding]:
    """Find script invocations outside records and the named equivalence."""

    runbooks, findings = _read_runbooks(root)
    matches = {name: _matches(lines, _UPGRADE_SCRIPT) for name, lines in runbooks.items()}
    allowed, named_findings = _named_lines(root, runbooks, matches, kept)
    findings.extend(named_findings)
    install = runbooks.get(INSTALL, [])
    first = _section(install, 1)
    if first is None:
        findings.append(Finding(RUNBOOKS / INSTALL, 1, "intro region moved", "restore the first numbered section"))
    else:
        allowed.update((INSTALL, number) for number in matches[INSTALL] if number <= first[0])
    for number in (5, 6):
        bounds = _section(install, number)
        if bounds is None:
            if number == 6:
                findings.append(Finding(RUNBOOKS / INSTALL, 1, "harness region moved", "restore section 6"))
            continue
        start, end = bounds
        region = {line for line in matches[INSTALL] if start + 1 <= line <= end}
        allowed.update((INSTALL, line) for line in region)
        if number == 6 and not region:
            findings.append(Finding(RUNBOOKS / INSTALL, start + 1, "harness region has no upgrade script", "restore its script record or update the reader"))
    for name, numbers in matches.items():
        for number in sorted(numbers):
            if (name, number) not in allowed:
                findings.append(Finding(RUNBOOKS / name, number, "upgrade script outside a kept line", "type gideon upgrade <tag>"))
    return findings


def _seed(root: Path) -> None:
    """Write fictitious runbooks with both permitted and disallowed lines."""

    runbooks = root / RUNBOOKS
    runbooks.mkdir(parents=True)
    (runbooks / "example.md").write_text("# Example\n`/usr/bin/python3 -B -m gideon status` in a proposal checkout.\n", encoding="utf-8")
    (runbooks / INSTALL).write_text(
        "# Install\n`./upgrade.sh` is a wrapper.\n## 6. Harness\n`./upgrade.sh <tag>` was run.\n"
        "## 1. Setup\n2. Provision\n```bash\nsudo python3 -m gideon host provision\n```\n"
        "3. Write site\n```bash\nsudo python3 -m gideon apply\n```\n"
        "## 2. Upgrade\nUse gideon upgrade.\n",
        encoding="utf-8",
    )
    (runbooks / CARD).write_text("# Card\nBefore provision: `sudo python3 -m gideon status`.\n## 1. Use\n- `sudo python3 -m gideon status`\n", encoding="utf-8")


class CommittedTree(unittest.TestCase):
    def test_runbook_forms(self) -> None:
        findings = [*rule_long_form(ROOT), *rule_upgrade_script(ROOT)]
        if findings:
            self.fail(render_findings(findings))


class SeededTrees(unittest.TestCase):
    def test_regions_and_named_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            findings = rule_long_form(root, (("example.md", "/usr/bin/python3 -B -m gideon status", "a proposal checkout"),))
        self.assertEqual([(item.path.name, item.line) for item in findings], [(INSTALL, 12), (CARD, 4)])
        self.assertTrue(all("Fix: " in line for line in render_findings(findings).splitlines()))

    def test_stale_and_ambiguous_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            example = root / RUNBOOKS / "example.md"
            with example.open("a", encoding="utf-8") as stream:
                stream.write("`/usr/bin/python3 -B -m gideon status` again.\n")
            findings = rule_long_form(root, (("example.md", "missing phrase", "a reason"), ("example.md", "/usr/bin/python3 -B -m gideon status", "a reason")))
        self.assertTrue(any("stale entry" in item.problem for item in findings))
        self.assertTrue(any("ambiguous entry" in item.problem for item in findings))

    def test_entry_for_a_runbook_the_export_omits(self) -> None:
        # An export tree lacks the omitted runbooks, so their entries are skipped there; a kept runbook's absence stays stale.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            omitted = (("ci-runner.md", "sudo python3 -m gideon host provision", "a runner checkout"),)
            long_form = rule_long_form(root, (*omitted, ("example.md", "/usr/bin/python3 -B -m gideon status", "a proposal checkout")))
            upgrade = rule_upgrade_script(root, (("ci-runner.md", "./upgrade.sh <tag>", "a runner checkout"),))
            kept_absent = rule_long_form(root, (("missing.md", "sudo python3 -m gideon status", "a reason"),))
        self.assertFalse(any(item.path.name == "ci-runner.md" for item in [*long_form, *upgrade]))
        self.assertTrue(any(item.path.name == "missing.md" and "stale entry" in item.problem for item in kept_absent))

    def test_empty_region_and_wrapped_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            install = root / RUNBOOKS / INSTALL
            install.write_text(install.read_text(encoding="utf-8").replace("sudo python3 -m gideon host provision", "gideon host provision"), encoding="utf-8")
            example = root / RUNBOOKS / "example.md"
            with example.open("a", encoding="utf-8") as stream:
                stream.write("`python3 -m\ngideon backup run`\n")
            findings = rule_long_form(root, (("example.md", "/usr/bin/python3 -B -m gideon status", "a proposal checkout"),))
        self.assertTrue(any("first-provision region has no long form" in item.problem for item in findings))
        self.assertTrue(any(item.path.name == "example.md" and item.line == 3 for item in findings))

    def test_upgrade_script_regions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            install = root / RUNBOOKS / INSTALL
            install.write_text(install.read_text(encoding="utf-8").replace("Use gideon upgrade.", "Use `./upgrade.sh <tag>`."), encoding="utf-8")
            findings = rule_upgrade_script(root, ())
        self.assertEqual([(item.path.name, item.line) for item in findings], [(INSTALL, 15)])
        self.assertIn("gideon upgrade <tag>", findings[0].fix)

    def test_named_upgrade_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            install = root / RUNBOOKS / INSTALL
            install.write_text(install.read_text(encoding="utf-8").replace("Use gideon upgrade.", "Use `sudo ./upgrade.sh <tag>` from this example checkout."), encoding="utf-8")
            runner = root / RUNBOOKS / "example-runner.md"
            runner.write_text("# Example runner\n`./upgrade.sh <tag>` from this example runner.\n", encoding="utf-8")
            findings = rule_upgrade_script(root, ((INSTALL, "sudo ./upgrade.sh <tag>` from this example checkout", "an equivalent checkout command"), ("example-runner.md", "./upgrade.sh <tag>` from this example runner", "the runner's checkout")))
        self.assertEqual(findings, [])

    def test_empty_harness_region(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root)
            install = root / RUNBOOKS / INSTALL
            install.write_text(install.read_text(encoding="utf-8").replace("`./upgrade.sh <tag>` was run.", "A run was recorded."), encoding="utf-8")
            findings = rule_upgrade_script(root, ())
        self.assertTrue(any("harness region has no upgrade script" in item.problem for item in findings))


if __name__ == "__main__":
    unittest.main()
