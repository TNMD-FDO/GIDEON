"""Contracts for install prerequisites and matching steps in both documents."""

from __future__ import annotations

import re
import unittest
from dataclasses import dataclass
from pathlib import Path

from gideon.host.lock import load_host_lock
from gideon.host.models import load_models_lock

ROOT = Path(__file__).resolve().parent.parent
README = Path("README.md")
RUNBOOK = Path("docs/runbooks/install-upgrade.md")
_HEADING = re.compile(r"^## (?!#)")
_STEP = re.compile(r"^(\d+)\. ")
_LABEL = re.compile(r"^- \*\*(?P<label>[^*]+)\*\* (?P<text>.*)$")
_VERSION = re.compile(r"\b\d+\.\d+(?:\.\d+)?\b")
_GB = re.compile(r"\b(\d+) GB\b")
_NOUN = re.compile(r"^\s+(VRAM|RAM|data volume)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Finding:
    """One actionable problem at a document line."""

    path: Path
    line: int
    problem: str
    fix: str


@dataclass(frozen=True, slots=True)
class Expected:
    os_lts: str
    platform: str
    profile: str
    model: str
    count: int
    vram_gb: int
    dram_gb: int
    data_volume_gb: int


@dataclass(frozen=True, slots=True)
class Line:
    number: int
    text: str


@dataclass(frozen=True, slots=True)
class Step:
    number: int
    line: int
    commands: tuple[str, ...]


def _scope(path: Path, source: str) -> list[Line]:
    lines = [Line(number, text) for number, text in enumerate(source.splitlines(), 1)]
    opening = "## Install" if path == README else "## 1."
    start = next(
        (index for index, line in enumerate(lines) if line.text.startswith(opening)),
        None,
    )
    if start is None:
        return []
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if _HEADING.match(lines[index].text)
        ),
        len(lines),
    )
    return lines[start:end]


def _steps(scope: list[Line]) -> list[Step]:
    """Read top-level numbered items and their fenced command lines."""

    steps: list[Step] = []
    index = 0
    while index < len(scope):
        match = _STEP.match(scope[index].text)
        if match is None:
            index += 1
            continue
        start = scope[index]
        index += 1
        commands: list[str] = []
        in_fence = False
        fence_indent = 0
        while index < len(scope):
            line = scope[index].text
            if _STEP.match(line) or (line and not line[0].isspace()):
                break
            stripped = line.strip()
            if stripped.startswith("```"):
                if not in_fence:
                    fence_indent = len(line) - len(line.lstrip())
                in_fence = not in_fence
            elif in_fence:
                commands.append(
                    line[fence_indent:] if line.startswith(" " * fence_indent) else line
                )
            index += 1
        steps.append(Step(int(match.group(1)), start.number, tuple(commands)))
    return steps


def _items(scope: list[Line], start: int) -> dict[str, Line]:
    """Join wrapped bullets below one prerequisite heading."""

    items: dict[str, Line] = {}
    current_label: str | None = None
    current: Line | None = None
    for line in scope[start + 1 :]:
        if line.text.startswith("### ") or _STEP.match(line.text):
            break
        match = _LABEL.match(line.text)
        if match:
            if current_label is not None and current is not None:
                items[current_label] = current
            current_label = match.group("label")
            current = Line(line.number, match.group("text"))
        elif line.text and not line.text[0].isspace():
            break
        elif current is not None and line.text.strip():
            current = Line(current.number, f"{current.text} {line.text.strip()}")
    if current_label is not None and current is not None:
        items[current_label] = current
    return items


def rule_prerequisites(path: Path, source: str, expected: Expected) -> list[Finding]:
    """Hold each install list to the host and reference-profile requirements."""

    scope = _scope(path, source)
    if not scope:
        return [
            Finding(
                path,
                1,
                "install section is missing",
                f"Restore the install section in {path}.",
            )
        ]
    headings = [
        index for index, line in enumerate(scope) if line.text == "### Before you begin"
    ]
    steps = _steps(scope)
    findings: list[Finding] = []

    def add(line: int, problem: str, item: str, authority: str) -> None:
        findings.append(
            Finding(
                path,
                line,
                problem,
                f"Correct the {item} item in {path} from {authority}.",
            )
        )

    if len(headings) != 1:
        findings.append(
            Finding(
                path,
                scope[0].number,
                f"found {len(headings)} Before you begin lists; expected one",
                f"Keep one Before you begin list in {path} before step 1.",
            )
        )
        if not headings:
            return findings
    heading = headings[0]
    if steps and scope[heading].number > steps[0].line:
        findings.append(
            Finding(
                path,
                scope[heading].number,
                "Before you begin follows step 1",
                f"Move the Before you begin list in {path} before step 1.",
            )
        )
    items = _items(scope, heading)
    os_item = items.get("Operating system")
    hardware = items.get("Hardware")
    if os_item is None:
        add(
            scope[heading].number,
            "Operating system item is missing",
            "Operating system",
            "host.lock and models.lock",
        )
    else:
        versions = _VERSION.findall(os_item.text)
        if versions != [expected.os_lts]:
            add(
                os_item.number,
                f"Operating system version {versions!r} differs from host.lock",
                "Operating system",
                "host.lock os_lts",
            )
        if not re.search(rf"(?<!\w){re.escape(expected.platform)}(?!\w)", os_item.text):
            add(
                os_item.number,
                "Operating system architecture differs from the reference profile",
                "Operating system",
                "models.lock requires.platform",
            )
    if hardware is None:
        add(
            scope[heading].number, "Hardware item is missing", "Hardware", "models.lock"
        )
        return findings
    if f"`{expected.profile}`" not in hardware.text:
        add(
            hardware.number,
            "Hardware profile name differs from the reference profile",
            "Hardware",
            "models.lock reference",
        )
    if expected.model not in hardware.text:
        add(
            hardware.number,
            "GPU model differs from the reference profile",
            "Hardware",
            "models.lock requires.gpu.model",
        )
    if not re.search(rf"(?<!\d){expected.count}\s*×", hardware.text):
        add(
            hardware.number,
            "GPU count before × differs from the reference profile",
            "Hardware",
            "models.lock requires.gpu.count",
        )
    figures = list(_GB.finditer(hardware.text))
    if len(figures) != 3:
        add(
            hardware.number,
            f"Hardware has {len(figures)} GB figures; expected three",
            "Hardware",
            "models.lock requires",
        )
    values: dict[str, list[int]] = {"vram": [], "ram": [], "data volume": []}
    for figure in figures:
        noun_match = _NOUN.match(hardware.text[figure.end() :])
        if noun_match is None:
            add(
                hardware.number,
                f"{figure.group(0)} has no VRAM, RAM, or data volume noun",
                "Hardware",
                "models.lock requires",
            )
        else:
            values[noun_match.group(1).lower()].append(int(figure.group(1)))
    for noun, value, field in (
        ("vram", expected.vram_gb, "requires.gpu.vram_gb"),
        ("ram", expected.dram_gb, "requires.dram_gb"),
        ("data volume", expected.data_volume_gb, "requires.data_volume_gb"),
    ):
        if values[noun] != [value]:
            add(
                hardware.number,
                f"Hardware {noun} {values[noun]!r} differs from models.lock",
                "Hardware",
                f"models.lock {field}",
            )
    return findings


def rule_sequence(readme_source: str, runbook_source: str) -> list[Finding]:
    """Hold the two install sequences to the same steps and fenced commands."""

    readme_scope = _scope(README, readme_source)
    runbook_scope = _scope(RUNBOOK, runbook_source)
    readme_steps = _steps(readme_scope)
    runbook_steps = _steps(runbook_scope)
    findings: list[Finding] = []

    def fix(step: int) -> str:
        return (
            f"Correct step {step} in {README} and {RUNBOOK} together; "
            "neither file is authoritative."
        )

    for path, scope, steps in (
        (README, readme_scope, readme_steps),
        (RUNBOOK, runbook_scope, runbook_steps),
    ):
        if not steps:
            findings.append(
                Finding(
                    path,
                    scope[0].number if scope else 1,
                    "install scope has no steps",
                    fix(1),
                )
            )
        elif not any(step.commands for step in steps):
            findings.append(
                Finding(
                    path,
                    steps[0].line,
                    "no step has a fenced command",
                    fix(steps[0].number),
                )
            )

    if len(readme_steps) != len(runbook_steps):
        first_unpaired = min(len(readme_steps), len(runbook_steps))
        extra_steps = (
            readme_steps if len(readme_steps) > len(runbook_steps) else runbook_steps
        )
        path = README if extra_steps is readme_steps else RUNBOOK
        findings.append(
            Finding(
                path,
                extra_steps[first_unpaired].line,
                f"install step counts differ: {len(readme_steps)} and {len(runbook_steps)}",
                fix(first_unpaired + 1),
            )
        )
    for number, (readme_step, runbook_step) in enumerate(
        zip(readme_steps, runbook_steps, strict=False), 1
    ):
        if readme_step.commands != runbook_step.commands:
            findings.append(
                Finding(
                    RUNBOOK,
                    runbook_step.line,
                    f"step {number} fenced command lines differ",
                    fix(number),
                )
            )
    return findings


def render_findings(findings: list[Finding]) -> str:
    return "\n".join(
        f"{item.path}:{item.line}: {item.problem}. Fix: {item.fix}" for item in findings
    )


class CommittedTree(unittest.TestCase):
    def test_install_prerequisites(self) -> None:
        host_result = load_host_lock(ROOT / "host.lock")
        models_result = load_models_lock(ROOT / "models.lock")
        self.assertTrue(host_result.ok, host_result.errors)
        self.assertTrue(models_result.ok, models_result.errors)
        assert host_result.lock is not None
        assert models_result.lock is not None
        profile = models_result.lock.profile(models_result.lock.reference)
        self.assertIsNotNone(profile)
        assert profile is not None
        requires = profile.requires
        expected = Expected(
            host_result.lock.os_lts,
            requires.platform,
            profile.name,
            requires.gpu.model,
            requires.gpu.count,
            requires.gpu.vram_gb,
            requires.dram_gb,
            requires.data_volume_gb,
        )
        findings = []
        for path in (README, RUNBOOK):
            findings.extend(
                rule_prerequisites(
                    path, (ROOT / path).read_text(encoding="utf-8"), expected
                )
            )
        if findings:
            self.fail(render_findings(findings))

    def test_install_sequences(self) -> None:
        findings = rule_sequence(
            (ROOT / README).read_text(encoding="utf-8"),
            (ROOT / RUNBOOK).read_text(encoding="utf-8"),
        )
        if findings:
            self.fail(render_findings(findings))


class SeededText(unittest.TestCase):
    expected = Expected(
        "987.65", "example_64", "3x111v-777d", "Example Accelerator", 3, 111, 777, 9999
    )
    source = (
        "# Example\n\n## Install\n\n### Before you begin\n\n"
        "- **Operating system** — Ubuntu Server 987.65 on example_64.\n"
        "- **Hardware** — `3x111v-777d`: 3 × Example Accelerator, 111 GB VRAM per GPU,\n"
        "  777 GB RAM, 9999 GB data volume.\n\n"
        "1. First step:\n\n   ```bash\n   example command\n   ```\n\n## Later\n"
    )

    def check(self, source: str, problem: str) -> None:
        findings = rule_prerequisites(README, source, self.expected)
        self.assertTrue(
            any(problem in finding.problem for finding in findings),
            render_findings(findings),
        )
        for finding in findings:
            self.assertEqual(finding.path, README)
            self.assertGreater(finding.line, 0)
            self.assertIn(str(README), finding.fix)

    def check_sequence(
        self, readme_source: str, runbook_source: str, problem: str
    ) -> None:
        findings = rule_sequence(readme_source, runbook_source)
        self.assertTrue(
            any(problem in finding.problem for finding in findings),
            render_findings(findings),
        )
        for finding in findings:
            self.assertGreater(finding.line, 0)
            self.assertIn(str(finding.path), finding.fix)
            self.assertIn("step ", finding.fix)
            self.assertIn("neither file is authoritative", finding.fix)

    def runbook_source(self) -> str:
        return self.source.replace("## Install", "## 1. Example sequence")

    def test_valid_wrapped_list(self) -> None:
        self.assertEqual(rule_prerequisites(README, self.source, self.expected), [])
        self.assertEqual(
            _steps(_scope(README, self.source))[0].commands, ("example command",)
        )

    def test_missing_list(self) -> None:
        self.check(
            self.source.replace("### Before you begin", "### Later"),
            "Before you begin lists",
        )

    def test_list_after_step_one(self) -> None:
        before, after = self.source.split("### Before you begin\n\n", 1)
        self.check(
            before + "1. First step:\n\n" + "### Before you begin\n\n" + after,
            "follows step 1",
        )

    def test_wrong_os_version(self) -> None:
        self.check(self.source.replace("987.65", "987.66"), "Operating system version")

    def test_wrong_architecture(self) -> None:
        self.check(
            self.source.replace("example_64", "other_64"),
            "Operating system architecture",
        )

    def test_wrong_figure(self) -> None:
        self.check(self.source.replace("111 GB VRAM", "112 GB VRAM"), "Hardware vram")

    def test_ram_and_vram_swapped(self) -> None:
        source = self.source.replace("111 GB VRAM", "777 GB VRAM").replace(
            "777 GB RAM", "111 GB RAM"
        )
        self.check(source, "Hardware vram")
        self.check(source, "Hardware ram")

    def test_figure_without_noun(self) -> None:
        self.check(
            self.source.replace("111 GB VRAM", "111 GB"),
            "has no VRAM, RAM, or data volume noun",
        )

    def test_extra_gb_figure(self) -> None:
        self.check(
            self.source.replace(
                "9999 GB data volume", "9999 GB data volume, 7 GB cache"
            ),
            "GB figures",
        )

    def test_wrong_model(self) -> None:
        self.check(
            self.source.replace("Example Accelerator", "Other Accelerator"), "GPU model"
        )

    def test_wrong_count(self) -> None:
        self.check(self.source.replace("3 ×", "4 ×"), "GPU count")

    def test_matching_sequences(self) -> None:
        self.assertEqual(rule_sequence(self.source, self.runbook_source()), [])

    def test_unequal_step_counts(self) -> None:
        runbook = self.runbook_source().replace(
            "## Later",
            "2. Second step:\n\n   ```bash\n   example follow-up\n   ```\n\n## Later",
        )
        self.check_sequence(self.source, runbook, "step counts differ")

    def test_one_step_command_differs(self) -> None:
        runbook = self.runbook_source().replace(
            "example command", "example replacement"
        )
        self.check_sequence(self.source, runbook, "step 1 fenced command lines differ")
        runbook = self.runbook_source().replace("example command", "example command ")
        self.check_sequence(self.source, runbook, "step 1 fenced command lines differ")

    def test_empty_scope(self) -> None:
        readme = self.source.replace("## Install", "## Other")
        self.check_sequence(readme, self.runbook_source(), "install scope has no steps")

    def test_steps_without_commands(self) -> None:
        runbook = self.runbook_source().replace(
            "   ```bash\n   example command\n   ```", "   No command yet."
        )
        self.check_sequence(self.source, runbook, "no step has a fenced command")


if __name__ == "__main__":
    unittest.main()
