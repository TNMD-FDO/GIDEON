"""Contract checks for the map, leaves, CLAUDE.md, and agent documents.

The estimator counts characters in the same categories as the repository's
token-counting script. Linefeeds are record separators, not punctuation; a
UTF-8 character count is intended, so a byte-oriented awk implementation can
vary for multibyte text. Reference-style links are not scanned: this contract
covers inline links only, which is every link the map, the leaves, and the glossary use.
The four documentation classes read their caps and warnings from the one
machine-readable line in docs/ARCHI-rules.md.
An excluded leaf under docs/archi/ is exempt from the map's index when a _Leaf_
link in CONTEXT.md routes to it; a §2 row that indexes one is a finding.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from tools.exportboundary import is_excluded

ROOT = Path(__file__).resolve().parent.parent
MAP_PATH = Path("docs/ARCHI.md")
LEAVES_PATH = Path("docs/archi")
GLOSSARY_PATH = Path("CONTEXT.md")
RULES_PATH = Path("docs/ARCHI-rules.md")
CLAUDE_PATH = Path("CLAUDE.md")
AGENTS_PATH = Path("docs/agents")
_BUDGET_CLASSES = ("map", "leaf", "CLAUDE.md", "agent document")
_INTEGER = r"(?:\d+|\d{1,3}(?:,\d{3})+)"
_BUDGET_CLAUSE = re.compile(
    rf"^(?P<name>map|leaf|CLAUDE\.md|agent document) cap "
    rf"(?P<cap>{_INTEGER}) warning (?P<warning>{_INTEGER})$"
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One actionable architecture-documentation finding."""

    path: Path
    problem: str
    fix: str


@dataclass(frozen=True, slots=True)
class BudgetValue:
    """A documentation class's cap and warning."""

    cap: int
    warning: int


class BudgetRecordError(ValueError):
    """The rules document does not contain a readable budget record."""


def _budgets_section(text: str) -> tuple[str, ...]:
    lines = text.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line == "## Budgets and warnings"),
        None,
    )
    if start is None:
        return ()
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("## ")
        ),
        len(lines),
    )
    return tuple(lines[start:end])


def _read_budgets(text: str) -> dict[str, BudgetValue]:
    candidates = [
        line for line in _budgets_section(text) if line.startswith("Budgets:")
    ]
    if len(candidates) != 1:
        raise BudgetRecordError(
            "docs/ARCHI-rules.md must contain exactly one Budgets line",
        )
    clauses = candidates[0][len("Budgets:") :].strip().split(";")
    if len(clauses) != len(_BUDGET_CLASSES):
        raise BudgetRecordError(
            "the Budgets line in docs/ARCHI-rules.md must contain four clauses",
        )
    budgets: dict[str, BudgetValue] = {}
    for expected_name, clause in zip(_BUDGET_CLASSES, clauses, strict=True):
        match = _BUDGET_CLAUSE.fullmatch(clause.strip())
        if match is None or match.group("name") != expected_name:
            raise BudgetRecordError(
                "the Budgets line in docs/ARCHI-rules.md has an invalid clause",
            )
        budgets[expected_name] = BudgetValue(
            cap=int(match.group("cap").replace(",", "")),
            warning=int(match.group("warning").replace(",", "")),
        )
    return budgets


_BUDGET_FIX = "Restore the one-line shape of the Budgets line in docs/ARCHI-rules.md."


def _character_counts(text: str) -> tuple[int, int, int, int, int]:
    letters = 0
    digits = 0
    whitespace = 0
    other = 0
    linefeeds = 0
    for character in text:
        if character == "\n":
            linefeeds += 1
        elif ("A" <= character <= "Z") or ("a" <= character <= "z"):
            letters += 1
        elif "0" <= character <= "9":
            digits += 1
        elif character in " \t":
            whitespace += 1
        else:
            other += 1
    return letters, digits, whitespace, other, linefeeds


def estimate_tokens(text: str) -> int:
    """Estimate tokens using character categories and awk-style records."""

    letters, digits, whitespace, other, linefeeds = _character_counts(text)
    records = linefeeds + int(bool(text) and not text.endswith("\n"))
    estimate = (
        letters / 4.8 + digits / 2.5 + whitespace / 6.0 + other / 2.8 + records * 0.75
    )
    return int(estimate + 0.5)


def _relative(root: Path, path: Path) -> Path:
    return path.relative_to(root)


def _finding(root: Path, path: Path, problem: str, fix: str) -> Finding:
    return Finding(_relative(root, path), problem, fix)


def rule_budget(root: Path) -> list[Finding]:
    """Find documentation files whose estimates exceed their budgets."""

    findings: list[Finding] = []
    rules_path = root / RULES_PATH
    try:
        budgets = _read_budgets(rules_path.read_text(encoding="utf-8"))
    except OSError as error:
        return [
            _finding(
                root,
                rules_path,
                f"could not read {RULES_PATH}: {error}",
                _BUDGET_FIX,
            )
        ]
    except BudgetRecordError as error:
        return [_finding(root, rules_path, str(error), _BUDGET_FIX)]

    for name, budget in budgets.items():
        if budget.warning >= budget.cap:
            findings.append(
                _finding(
                    root,
                    rules_path,
                    f"{name} warning {budget.warning} is not below cap {budget.cap}",
                    _BUDGET_FIX,
                )
            )

    map_path = root / MAP_PATH
    if not map_path.is_file():
        findings.append(
            _finding(
                root,
                map_path,
                "architecture map is missing",
                "Create docs/ARCHI.md and add its architecture map sections.",
            )
        )
    else:
        map_budget = budgets["map"]
        map_tokens = estimate_tokens(map_path.read_text(encoding="utf-8"))
        if map_tokens >= map_budget.cap:
            findings.append(
                _finding(
                    root,
                    map_path,
                    f"estimated token count {map_tokens} reaches map cap {map_budget.cap}",
                    "Run TRIP-compact docs/ARCHI.md or split docs/ARCHI.md by mechanism per docs/ARCHI-rules.md.",
                )
            )

    archi_path = root / LEAVES_PATH
    if archi_path.is_dir():
        leaf_budget = budgets["leaf"]
        for leaf_path in sorted(archi_path.glob("*.md")):
            leaf_tokens = estimate_tokens(leaf_path.read_text(encoding="utf-8"))
            if leaf_tokens >= leaf_budget.cap:
                findings.append(
                    _finding(
                        root,
                        leaf_path,
                        f"estimated token count {leaf_tokens} reaches leaf cap {leaf_budget.cap}",
                        f"Run TRIP-compact {_relative(root, leaf_path)} or split {_relative(root, leaf_path)} by mechanism per docs/ARCHI-rules.md.",
                    )
                )
    claude_path = root / CLAUDE_PATH
    if claude_path.is_file():
        claude_budget = budgets["CLAUDE.md"]
        claude_tokens = estimate_tokens(claude_path.read_text(encoding="utf-8"))
        if claude_tokens >= claude_budget.cap:
            findings.append(
                _finding(
                    root,
                    claude_path,
                    f"estimated token count {claude_tokens} reaches CLAUDE.md cap {claude_budget.cap}",
                    "Run TRIP-compact CLAUDE.md with its loss ledger or move a section's detail to a document under docs/agents/ per docs/ARCHI-rules.md.",
                )
            )
    if (root / AGENTS_PATH).is_dir():
        agent_budget = budgets["agent document"]
        for agent_path in sorted((root / AGENTS_PATH).glob("*.md")):
            agent_tokens = estimate_tokens(agent_path.read_text(encoding="utf-8"))
            if agent_tokens >= agent_budget.cap:
                relative = _relative(root, agent_path)
                findings.append(
                    _finding(
                        root,
                        agent_path,
                        f"estimated token count {agent_tokens} reaches agent document cap {agent_budget.cap}",
                        f"Run TRIP-compact {relative} or split {relative} by mechanism per docs/ARCHI-rules.md.",
                    )
                )
    return findings


def _section_two(text: str) -> str:
    lines = text.splitlines(keepends=True)
    start = next(
        (index for index, line in enumerate(lines) if re.match(r"^## 2\.", line)),
        None,
    )
    if start is None:
        return ""
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("## ")
        ),
        len(lines),
    )
    return "".join(lines[start:end])


_INLINE_LINK = re.compile(
    r"\[[^\]\n]*\]\(\s*(?:<(?P<bracket>[^>\n]*)>|(?P<bare>[^)\s]+))"
)
_FENCE = re.compile(r"^\s*(?P<marker>`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(?P<ticks>`+).*?(?P=ticks)", re.DOTALL)


def _without_fences(text: str) -> str:
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
    return "".join(result)


def _without_code(text: str) -> str:
    return _INLINE_CODE.sub(
        lambda match: "\n" * match.group(0).count("\n"), _without_fences(text)
    )


def _link_targets(text: str) -> tuple[str, ...]:
    return tuple(target for _, target in _links(text))


def _links(text: str) -> tuple[tuple[int, str], ...]:
    """Return every inline link outside code as (line number, target)."""

    stripped = _without_code(text)
    return tuple(
        (stripped.count("\n", 0, match.start()) + 1, match.group("bracket") or match.group("bare"))
        for match in _INLINE_LINK.finditer(stripped)
    )


_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(?P<text>.*?)(?:[ \t]+#+)?[ \t]*$")
_HEADING_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _slug(heading: str) -> str:
    """The renderer's anchor for a heading: ticks and link syntax dropped, lowercased,
    punctuation other than hyphens and underscores removed, each space a hyphen."""

    text = _HEADING_LINK.sub(r"\1", heading.replace("`", "")).lower()
    return re.sub(r"[^\w\s-]", "", text).replace(" ", "-")


def _anchors(text: str) -> set[str]:
    anchors: set[str] = set()
    for line in _without_fences(text).splitlines():
        heading = _HEADING.match(line)
        if heading is None:
            continue
        slug = _slug(heading.group("text"))
        candidate, repeat = slug, 0
        while candidate in anchors:
            repeat += 1
            candidate = f"{slug}-{repeat}"
        anchors.add(candidate)
    return anchors


def _archi_files(root: Path) -> tuple[Path, ...]:
    archi_path = root / LEAVES_PATH
    if not archi_path.is_dir():
        return ()
    return tuple(sorted(path for path in archi_path.rglob("*") if path.is_file()))


def _indexed_leaves(root: Path, section: str) -> set[Path]:
    archi_path = (root / LEAVES_PATH).resolve()
    indexed: set[Path] = set()
    for target in _link_targets(section):
        target_path = target.split("#", 1)[0]
        if not target_path.endswith(".md"):
            continue
        resolved = (root / MAP_PATH).parent.joinpath(unquote(target_path)).resolve()
        try:
            resolved.relative_to(archi_path)
        except ValueError:
            continue
        indexed.add(resolved)
    return indexed


def _context_routed_leaves(root: Path) -> set[Path] | None:
    context_path = root / GLOSSARY_PATH
    if not context_path.is_file():
        return None
    archi_path = (root / LEAVES_PATH).resolve()
    routed: set[Path] = set()
    for target in _link_targets(context_path.read_text(encoding="utf-8")):
        target_path = target.split("#", 1)[0]
        if not target_path.endswith(".md"):
            continue
        resolved = (context_path.parent / unquote(target_path)).resolve()
        try:
            resolved.relative_to(archi_path)
        except ValueError:
            continue
        routed.add(resolved)
    return routed


def rule_index(root: Path) -> list[Finding]:
    """Find leaves and §2 rows that are absent from the other side.

    An excluded leaf is exempt when a CONTEXT.md link routes to it, and a §2 row
    that indexes an excluded leaf is a finding on the map.
    """

    map_path = root / MAP_PATH
    if not map_path.is_file():
        return []
    archi_path = root / LEAVES_PATH
    if not archi_path.is_dir():
        return [
            _finding(
                root,
                archi_path,
                "docs/archi/ does not exist",
                "Create docs/archi/ and add one indexed leaf row to docs/ARCHI.md §2 for each subsystem.",
            )
        ]

    indexed = _indexed_leaves(root, _section_two(map_path.read_text(encoding="utf-8")))
    context_routed = _context_routed_leaves(root)
    files = set(_archi_files(root))
    findings: list[Finding] = []
    non_md_files = {path for path in files if path.suffix != ".md"}
    for path in sorted(non_md_files):
        findings.append(
            _finding(
                root,
                path,
                "file under docs/archi/ is not a Markdown leaf",
                f"Remove {_relative(root, path)} or rename it to a .md leaf and add its row to docs/ARCHI.md §2.",
            )
        )

    md_files = {path for path in files if path.suffix == ".md"}
    excluded_md_files = {
        path
        for path in md_files
        if is_excluded(_relative(root, path).as_posix())
    }
    kept_md_files = md_files - excluded_md_files
    for path in sorted(indexed - md_files):
        findings.append(
            _finding(
                root,
                path,
                "§2 indexes a leaf file that does not exist",
                f"Create {_relative(root, path)} or remove its row from docs/ARCHI.md §2.",
            )
        )
    for path in sorted(indexed & excluded_md_files):
        relative = _relative(root, path)
        findings.append(
            _finding(
                root,
                map_path,
                f"§2 indexes excluded leaf {relative}",
                f"Remove {relative}'s row from docs/ARCHI.md §2.",
            )
        )
    for path in sorted(kept_md_files - indexed):
        findings.append(
            _finding(
                root,
                path,
                "leaf file is not indexed from docs/ARCHI.md §2",
                f"Add {_relative(root, path)}'s row to docs/ARCHI.md §2.",
            )
        )
    if context_routed is not None:
        for path in sorted(excluded_md_files - context_routed):
            relative = _relative(root, path)
            findings.append(
                _finding(
                    root,
                    path,
                    "excluded leaf is not routed from CONTEXT.md",
                    f"Add a _Leaf_ link through /domain-modeling in CONTEXT.md to {relative}.",
                )
            )
    return findings


def _files_to_scan(root: Path) -> tuple[Path, ...]:
    map_path = root / MAP_PATH
    paths: list[Path] = [map_path] if map_path.is_file() else []
    paths.extend(path for path in _archi_files(root) if path.suffix == ".md")
    paths.extend(
        path
        for path in (root / GLOSSARY_PATH, root / RULES_PATH, root / CLAUDE_PATH)
        if path.is_file()
    )
    agents_path = root / AGENTS_PATH
    if agents_path.is_dir():
        paths.extend(sorted(agents_path.glob("*.md")))
    return tuple(paths)


def rule_links(root: Path) -> list[Finding]:
    """Find inline links whose file, or whose anchor's heading, does not exist.

    A link into an excluded export prefix is the export boundary test's, so it
    is skipped here, except into this test's own documents (`CLAUDE.md`,
    `CONTEXT.md`, `docs/agents/`), which leave with the export yet are checked
    wherever they are; the renderer's own anchor ids are not modelled.
    """

    findings: list[Finding] = []
    top = root.resolve()
    own = (GLOSSARY_PATH, CLAUDE_PATH, AGENTS_PATH)
    for path in _files_to_scan(root):
        text = path.read_text(encoding="utf-8")
        for line, target in _links(text):
            if urlsplit(target).scheme:
                continue
            target_path, _, anchor = target.partition("#")
            resolved = (
                path.parent.joinpath(unquote(target_path)).resolve() if target_path else path.resolve()
            )
            if resolved.is_relative_to(top):
                relative = resolved.relative_to(top)
                if is_excluded(relative.as_posix()) and not any(
                    relative.is_relative_to(path) for path in own
                ):
                    continue
            where = f"line {line}: inline link target {target!r}"
            if not resolved.exists():
                problem = f"{where} does not resolve"
                fix = f"Retarget the link in {_relative(root, path)} to an existing file or directory."
            elif anchor and resolved.is_file() and resolved.suffix == ".md":
                headings = text if resolved == path.resolve() else resolved.read_text(encoding="utf-8")
                if unquote(anchor) in _anchors(headings):
                    continue
                problem = f"{where} names no heading of {_relative(root, resolved)}"
                fix = f"Point the anchor in {_relative(root, path)} at a heading's slug, or drop it."
            else:
                continue
            findings.append(_finding(root, path, problem, fix))
    return findings


def findings(root: Path) -> list[Finding]:
    """Return all architecture findings in rule order."""

    all_findings: list[Finding] = []
    all_findings.extend(rule_budget(root))
    all_findings.extend(rule_index(root))
    all_findings.extend(rule_links(root))
    return all_findings


def render_findings(items: list[Finding]) -> str:
    """Render every finding as one actionable line."""

    return "\n".join(f"{item.path}: {item.problem}. Fix: {item.fix}" for item in items)


class Estimator(unittest.TestCase):
    def test_hand_computed_value_and_category_partition(self) -> None:
        text = "AB12 \t!é\ncd3"
        counts = _character_counts(text)
        self.assertEqual(counts, (4, 3, 2, 2, 1))
        self.assertEqual(sum(counts), len(text))
        self.assertEqual(estimate_tokens(text), 5)


def _seed_rules(root: Path) -> dict[str, BudgetValue]:
    rules_path = root / RULES_PATH
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(
        "## Budgets and warnings\n\n"
        "A fictitious rules document for the fixtures.\n\n"
        "Budgets: map cap 100 warning 50; leaf cap 80 warning 40; "
        "CLAUDE.md cap 60 warning 30; agent document cap 80 warning 40\n",
        encoding="utf-8",
    )
    return _read_budgets(rules_path.read_text(encoding="utf-8"))


class Budget(unittest.TestCase):
    def test_map_over_cap_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            budgets = _seed_rules(root)
            map_path = root / MAP_PATH
            map_path.parent.mkdir(parents=True, exist_ok=True)
            map_path.write_text("a" * (budgets["map"].cap * 5), encoding="utf-8")
            items = rule_budget(root)
            self.assertTrue(any(item.path == MAP_PATH for item in items))

    def test_leaf_over_cap_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            budgets = _seed_rules(root)
            leaf_path = root / LEAVES_PATH / "large.md"
            leaf_path.parent.mkdir(parents=True)
            leaf_path.write_text("a" * (budgets["leaf"].cap * 5), encoding="utf-8")
            (root / MAP_PATH).parent.mkdir(parents=True, exist_ok=True)
            (root / MAP_PATH).write_text("map", encoding="utf-8")
            items = rule_budget(root)
            self.assertTrue(
                any(item.path == LEAVES_PATH / "large.md" for item in items)
            )

    def test_claude_over_cap_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            budgets = _seed_rules(root)
            claude_path = root / CLAUDE_PATH
            claude_path.write_text(
                "a" * (budgets["CLAUDE.md"].cap * 5), encoding="utf-8"
            )
            self.assertTrue(
                any(item.path == CLAUDE_PATH for item in rule_budget(root))
            )

    def test_agent_document_over_cap_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            budgets = _seed_rules(root)
            agent_path = root / AGENTS_PATH / "large.md"
            agent_path.parent.mkdir(parents=True)
            agent_path.write_text(
                "a" * (budgets["agent document"].cap * 5), encoding="utf-8"
            )
            self.assertTrue(
                any(item.path == AGENTS_PATH / "large.md" for item in rule_budget(root))
            )

    def test_missing_or_malformed_budgets_line_is_one_finding(self) -> None:
        malformed = (
            "## Budgets and warnings\n",
            "## Budgets and warnings\n\nBudgets: map cap 100 warning 50\n",
            (
                "## Budgets and warnings\n\n"
                "Budgets: map cap 100 warning 50; leaf cap 80 warning 40; "
                "CLAUDE.md cap 60 warning 30; agent document cap 80 warning 40\n"
                "Budgets: map cap 100 warning 50; leaf cap 80 warning 40; "
                "CLAUDE.md cap 60 warning 30; agent document cap 80 warning 40\n"
            ),
            (
                "## Budgets and warnings\n\n"
                "Budgets: map cap 1,0 warning 50; leaf cap 80 warning 40; "
                "CLAUDE.md cap 60 warning 30; agent document cap 80 warning 40\n"
            ),
            (
                "## Budgets and warnings\n\n## Routing\n\n"
                "Budgets: map cap 100 warning 50; leaf cap 80 warning 40; "
                "CLAUDE.md cap 60 warning 30; agent document cap 80 warning 40\n"
            ),
        )
        for text in malformed:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _seed_rules(root)
                (root / RULES_PATH).write_text(text, encoding="utf-8")
                items = rule_budget(root)
                self.assertEqual([item.path for item in items], [RULES_PATH])
                self.assertIn("one-line shape", render_findings(items))

    def test_warning_at_cap_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            budgets = _seed_rules(root)
            rules_path = root / RULES_PATH
            rules_path.write_text(
                rules_path.read_text(encoding="utf-8").replace(
                    "warning 50", f"warning {budgets['map'].cap}"
                ),
                encoding="utf-8",
            )
            items = rule_budget(root)
            self.assertTrue(any("not below cap" in item.problem for item in items))


class Index(unittest.TestCase):
    def _seed_index(
        self,
        root: Path,
        *,
        map_links: str = "",
        context_links: str | None = None,
        leaf_name: str = "workflow-tools.md",
    ) -> None:
        map_path = root / MAP_PATH
        map_path.parent.mkdir(parents=True)
        map_path.write_text(f"## 2. Index\n{map_links}## 3. Next\n", encoding="utf-8")
        leaf_path = root / LEAVES_PATH / leaf_name
        leaf_path.parent.mkdir(parents=True)
        leaf_path.write_text("leaf", encoding="utf-8")
        if context_links is not None:
            (root / GLOSSARY_PATH).write_text(context_links, encoding="utf-8")

    def test_excluded_leaf_routed_by_glossary_is_exempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._seed_index(
                root,
                context_links="_Leaf_: [workflow](docs/archi/workflow-tools.md)\n",
            )
            self.assertEqual(rule_index(root), [])

    def test_excluded_leaf_without_glossary_route_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._seed_index(root, context_links="")
            items = rule_index(root)
            self.assertEqual([item.path for item in items], [LEAVES_PATH / "workflow-tools.md"])
            self.assertIn("_Leaf_", items[0].fix)
            self.assertIn("CONTEXT.md", items[0].fix)

    def test_map_row_for_excluded_leaf_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._seed_index(
                root,
                map_links="- [workflow](archi/workflow-tools.md)\n",
                context_links="_Leaf_: [workflow](docs/archi/workflow-tools.md)\n",
            )
            items = rule_index(root)
            self.assertEqual([item.path for item in items], [MAP_PATH])
            self.assertIn("excluded leaf", items[0].problem)
            self.assertIn("Remove", items[0].fix)

    def test_kept_leaf_without_map_row_remains_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._seed_index(root, leaf_name="present.md")
            items = rule_index(root)
            self.assertEqual([item.path for item in items], [LEAVES_PATH / "present.md"])
            self.assertIn("not indexed", items[0].problem)

    def test_rows_leaves_and_file_suffixes_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path = root / MAP_PATH
            map_path.parent.mkdir(parents=True)
            map_path.write_text(
                "## 1. Reading\n\n## 2. Index\n"
                "- [present](archi/present.md)\n"
                "- [missing](archi/missing.md)\n\n"
                "## 3. Next\n",
                encoding="utf-8",
            )
            archi_path = root / LEAVES_PATH
            archi_path.mkdir(parents=True)
            (archi_path / "present.md").write_text("leaf", encoding="utf-8")
            (archi_path / "orphan.md").write_text("leaf", encoding="utf-8")
            (archi_path / "notes.txt").write_text("not a leaf", encoding="utf-8")
            items = rule_index(root)
            problems = {item.problem for item in items}
            self.assertTrue(any("does not exist" in problem for problem in problems))
            self.assertTrue(any("not indexed" in problem for problem in problems))
            self.assertTrue(any("not a Markdown" in problem for problem in problems))


class Links(unittest.TestCase):
    def test_broken_links_are_found_and_code_fragments_and_directories_resolve(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path = root / MAP_PATH
            map_path.parent.mkdir(parents=True)
            map_path.write_text(
                "## 2. Index\n"
                "[leaf](archi/leaf.md) [broken](missing.md) [fragment](#2-index) [directory](.)\n"
                "`[ignored](missing-from-code.md)`\n"
                "```text\n[also ignored](missing-from-fence.md)\n```\n",
                encoding="utf-8",
            )
            leaf_path = root / LEAVES_PATH / "leaf.md"
            leaf_path.parent.mkdir(parents=True)
            (root / "README.md").write_text("root", encoding="utf-8")
            leaf_path.write_text(
                "# Leaf\n"
                "[broken](missing-leaf.md) [root](../../README.md) [directory](..) [fragment](#leaf)\n",
                encoding="utf-8",
            )
            items = rule_links(root)
            self.assertEqual(
                [item.path for item in items], [MAP_PATH, LEAVES_PATH / "leaf.md"]
            )
            self.assertNotIn("missing-from-code.md", render_findings(items))
            self.assertNotIn("missing-from-fence.md", render_findings(items))

    def test_anchors_resolve_by_slug_in_agent_documents_rules_and_claude_md(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / AGENTS_PATH).mkdir(parents=True)
            (root / ".scratch").mkdir()
            (root / CLAUDE_PATH).write_text(
                "## The `tool.md` rule — its two halves\n"
                "## Twice\n## Twice\n"
                "[self](#the-toolmd-rule--its-two-halves) [twin](#twice-1) "
                "[rules](docs/ARCHI-rules.md#present) [excluded](.scratch/gone.md)\n"
                "\n[gone](#twice-2)\n",
                encoding="utf-8",
            )
            (root / RULES_PATH).write_text(
                "# Present\n```text\n## Fenced\n```\n[fenced](#fenced) [dir](agents#present)\n",
                encoding="utf-8",
            )
            (root / AGENTS_PATH / "page.md").write_text(
                "[missing](gone.md#any) [bad](../ARCHI-rules.md#absent) [good](../ARCHI-rules.md#present)\n",
                encoding="utf-8",
            )
            items = rule_links(root)
            self.assertEqual(
                [(item.path, item.problem) for item in items],
                [
                    (
                        RULES_PATH,
                        "line 5: inline link target '#fenced' names no heading of docs/ARCHI-rules.md",
                    ),
                    (
                        CLAUDE_PATH,
                        "line 6: inline link target '#twice-2' names no heading of CLAUDE.md",
                    ),
                    (
                        AGENTS_PATH / "page.md",
                        "line 1: inline link target 'gone.md#any' does not resolve",
                    ),
                    (
                        AGENTS_PATH / "page.md",
                        "line 1: inline link target '../ARCHI-rules.md#absent' names no heading "
                        "of docs/ARCHI-rules.md",
                    ),
                ],
            )
            self.assertTrue(all(item.fix.endswith(".") for item in items))

    def test_glossary_links_resolve_from_repo_root_without_a_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            budgets = _seed_rules(root)
            glossary_path = root / GLOSSARY_PATH
            glossary_path.write_text(
                "**Present**: definition\n"
                "_Leaf_: [`docs/archi/present.md`](docs/archi/present.md)\n\n"
                "**Missing**: definition\n"
                "_Leaf_: [`docs/archi/missing.md`](docs/archi/missing.md)\n",
                encoding="utf-8",
            )
            present_path = root / "docs/archi/present.md"
            present_path.parent.mkdir(parents=True)
            present_path.write_text("leaf", encoding="utf-8")
            (root / MAP_PATH).write_text("map", encoding="utf-8")

            items = rule_links(root)
            self.assertEqual([item.path for item in items], [GLOSSARY_PATH])
            self.assertEqual(
                render_findings(items),
                "CONTEXT.md: line 5: inline link target 'docs/archi/missing.md' does not resolve. "
                "Fix: Retarget the link in CONTEXT.md to an existing file or directory.",
            )

            glossary_path.write_text(
                "a" * (budgets["map"].cap * 5), encoding="utf-8"
            )
            self.assertEqual(rule_budget(root), [])


class CommittedTree(unittest.TestCase):
    def test_committed_tree_is_clean(self) -> None:
        items = findings(ROOT)
        if items:
            self.fail(render_findings(items))


if __name__ == "__main__":
    unittest.main()
