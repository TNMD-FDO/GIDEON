"""The release's two derived records: the release-note index and README's tag.

`bin/release-git` derives both from `gideon.__version__` and the release files
(slice-1 tickets 60 and 61, workflow tickets 38 and 42); this module holds the
committed tree to them, with each linked line targeting its release note, and
fires each index rule on a seeded tree.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from gideon import __version__
from tools.exportboundary import absent_from_export

ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = Path("CHANGELOG.md")
README_PATH = Path("README.md")
CHANGELOG_ROOT = Path("docs/2-changelog")
RELEASE_NOTES_ROOT = Path("docs/release-notes")
_CHANGELOG_NAME = re.compile(r"^w\d+_v(?P<version>\d+\.\d+\.\d+)\.md$")
_INDEX_LINE = re.compile(
    r"^- (?:\[v(?P<link_version>\d+\.\d+\.\d+)\]"
    r"\((?P<target>docs/release-notes/v"
    r"(?P<file_version>\d+\.\d+\.\d+)\.md)\)"
    r"|v(?P<text_version>\d+\.\d+\.\d+))"
    r" — (?P<date>\d{4}-\d{2}-\d{2}) — "
    r"(?P<phrase>\S(?:.*\S)?)$"
)
_TITLE = re.compile(
    r"^# Changelog - Week [^,]+, (?P<date>\d{2}-\d{2}-\d{4}), V\. "
    r"(?P<version>\d+\.\d+\.\d+)$"
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One actionable finding: the path, the line, the problem, the fix."""

    path: Path
    line: int
    problem: str
    fix: str


def _index_lines(root: Path) -> list[tuple[int, str]]:
    path = root / INDEX_PATH
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    return [
        (number, line)
        for number, line in enumerate(text.splitlines(), 1)
        if line.startswith("- ")
    ]


def _entries(root: Path) -> list[tuple[int, re.Match[str]]]:
    return [
        (number, match)
        for number, line in _index_lines(root)
        if (match := _INDEX_LINE.fullmatch(line)) is not None
    ]


def _release_files(root: Path) -> list[str]:
    directory = root / CHANGELOG_ROOT
    if not directory.is_dir():
        return []
    return [
        path.relative_to(root).as_posix()
        for path in sorted(directory.iterdir())
        if path.is_file() and _CHANGELOG_NAME.fullmatch(path.name) is not None
    ]


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _title_date(root: Path, target: str) -> date | None:
    lines = (root / target).read_text(encoding="utf-8").splitlines()
    match = _TITLE.fullmatch(lines[0]) if lines else None
    if match is None:
        return None
    return datetime.strptime(match.group("date"), "%d-%m-%Y").replace(tzinfo=UTC).date()


def index_findings(root: Path) -> list[Finding]:
    """The index over the release files: complete, well-shaped, ordered, dated."""

    index = INDEX_PATH
    found: list[Finding] = []
    entries = _entries(root)
    changelog_present = not absent_from_export(CHANGELOG_ROOT, root)
    changelog_by_version: dict[str, str] = {}
    if changelog_present:
        for target in _release_files(root):
            match = _CHANGELOG_NAME.fullmatch(Path(target).name)
            if match is not None:
                changelog_by_version[match.group("version")] = target
        seen: dict[str, list[int]] = {}
        for number, entry in entries:
            version = entry.group("file_version") or entry.group("text_version")
            assert version is not None
            seen.setdefault(version, []).append(number)
        for version, target in changelog_by_version.items():
            numbers = seen.get(version, [])
            if not numbers:
                found.append(Finding(index, 1, f"{target} has no index line", f"Add one fixed-shape line for {target}"))
            for number in numbers[1:]:
                found.append(Finding(index, number, f"{target} has a duplicate index line", f"Remove the duplicate line for {target}"))
    previous: tuple[int, ...] | None = None
    for number, line in _index_lines(root):
        match = _INDEX_LINE.fullmatch(line)
        if match is None:
            found.append(Finding(index, number, "index line does not match the fixed shape", "Write a dash, the tag linked to its release note from v0.2.0 or as text below it, an ISO date, and a non-empty phrase"))
            continue
        target = match.group("target")
        linked_version = match.group("file_version")
        text_version = match.group("text_version")
        version = linked_version or text_version
        assert version is not None
        if linked_version is not None and match.group("link_version") != linked_version:
            found.append(Finding(index, number, "linked tag version differs from its target filename", "Make the link text use the target filename's version"))
        current = _version_tuple(version)
        if previous is not None and current >= previous:
            found.append(Finding(index, number, "index lines are not newest first by version tuple", "Sort the release lines in descending version order"))
        previous = current
        if linked_version is not None:
            target = match.group("target")
            assert target is not None
            if not (root / target).is_file():
                found.append(Finding(index, number, f"index target {target} does not exist", f"Create the release note or remove its index line for {target}"))
                continue
            if current < (0, 2, 0):
                found.append(Finding(index, number, "linked index line is below the v0.2.0 release-note boundary", "Use the plain-text tag shape for releases before v0.2.0"))
        elif current >= (0, 2, 0):
            found.append(Finding(index, number, "plain-text index line is at or above the v0.2.0 release-note boundary", "Link the tag to docs/release-notes/v<version>.md"))
        if not changelog_present:
            continue
        changelog_target = changelog_by_version.get(version)
        if changelog_target is None:
            found.append(Finding(index, number, "no release file for this line", "Add the changelog file for the indexed version"))
            continue
        title_date = _title_date(root, changelog_target)
        if title_date is None:
            found.append(Finding(Path(changelog_target), 1, "release file has no title in the changelog shape", "Restore the title line with its release date"))
        elif title_date != date.fromisoformat(match.group("date")):
            found.append(Finding(index, number, "index date differs from the target title date", "Copy the date derived from the target file's title"))
    path = root / INDEX_PATH
    preamble = path.read_text(encoding="utf-8").split("\n- ", 1)[0] if path.is_file() else ""
    if "## Breaking" not in preamble or "upgrade" not in preamble:
        found.append(Finding(index, 1, "preamble does not name the Breaking section and upgrade command", "State that ## Breaking is printed by upgrade before a major refusal"))
    return found


def render(items: list[Finding]) -> str:
    """Render every finding as one actionable line."""

    return "\n".join(f"{item.path}:{item.line}: {item.problem}. Fix: {item.fix}" for item in items)


_PREAMBLE = "The release record.\n\nA major release has a ## Breaking section, and gideon upgrade reads it.\n"


def _version(offset: int = 0) -> str:
    return f"91.82.{73 - offset}"


def _release(root: Path, version: str, when: date) -> str:
    week = when.isocalendar().week
    path = root / CHANGELOG_ROOT / f"w{week}_v{version}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Changelog - Week {week}, {when:%d-%m-%Y}, V. {version}\n", encoding="utf-8")
    return path.relative_to(root).as_posix()


def _note(root: Path, version: str) -> str:
    path = root / RELEASE_NOTES_ROOT / f"v{version}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("synthetic release note\n", encoding="utf-8")
    return path.relative_to(root).as_posix()


def _line(version: str, target: str, when: date) -> str:
    return f"- [v{version}]({target}) — {when:%Y-%m-%d} — synthetic release"


def _index(root: Path, lines: list[str], preamble: str = _PREAMBLE) -> None:
    (root / INDEX_PATH).write_text(preamble + "\n".join(lines) + "\n", encoding="utf-8")


class ReleaseRecords(unittest.TestCase):
    """The committed tree is clean, and each index rule fires on a seeded tree."""

    def test_committed_index_is_derived_from_the_release_files(self) -> None:
        self.assertEqual(render(index_findings(ROOT)), "")

    def test_committed_readme_tag_equals_the_version(self) -> None:
        text = (ROOT / README_PATH).read_text(encoding="utf-8")
        self.assertEqual(text.count(f"this tree is `v{__version__}`"), 1)

    def test_each_index_rule_fires_on_a_seeded_tree(self) -> None:
        today = datetime.now(UTC).date()
        newest, older = _version(), _version(1)
        cases: list[tuple[str, list[str], str]] = [
            ("missing line", [], "has no index line"),
            ("duplicate line", ["same", "same"], "has a duplicate index line"),
            ("malformed line", ["- docs/2-changelog/nothing.md"], "does not match the fixed shape"),
            ("missing target", [_line(older, f"docs/release-notes/v{older}.md", today)], "does not exist"),
            ("link version", [_line(older, "newest", today)], "differs from its target filename"),
            ("order", ["older-line", "same"], "not newest first"),
            ("stale date", [_line(newest, "newest", today + timedelta(days=1))], "differs from the target title date"),
            ("preamble", ["same"], "does not name the Breaking section"),
        ]
        for name, lines, expected in cases:
            with self.subTest(rule=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _release(root, newest, today)
                if name == "order":
                    _release(root, older, today)
                note_target = _note(root, newest)
                older_note_target = _note(root, older) if name == "order" else ""
                filled = [
                    _line(newest, note_target, today) if line == "same"
                    else _line(older, older_note_target, today) if line == "older-line"
                    else line.replace("newest", note_target)
                    for line in lines
                ]
                _index(root, filled, "The release record.\n" if name == "preamble" else _PREAMBLE)
                rendered = render(index_findings(root))
                self.assertIn(expected, rendered, rendered)
                self.assertTrue(all(". Fix: " in line for line in rendered.splitlines()), rendered)

    def test_pre_0_2_0_text_shape_is_accepted(self) -> None:
        today = datetime.now(UTC).date()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _release(root, "0.1.9", today)
            _index(root, [f"- v0.1.9 — {today:%Y-%m-%d} — synthetic release"])
            self.assertEqual(index_findings(root), [])

    def test_linked_note_and_shape_boundaries_are_enforced(self) -> None:
        today = datetime.now(UTC).date()
        with self.subTest(case="linked note missing"), tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _release(root, "0.2.0", today)
            _index(root, [_line("0.2.0", "docs/release-notes/v0.2.0.md", today)])
            self.assertIn("index target docs/release-notes/v0.2.0.md does not exist", render(index_findings(root)))

        with self.subTest(case="linked line below boundary"), tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _release(root, "0.1.9", today)
            note = _note(root, "0.1.9")
            _index(root, [_line("0.1.9", note, today)])
            self.assertIn("linked index line is below", render(index_findings(root)))

        with self.subTest(case="text line at boundary"), tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _release(root, "0.2.0", today)
            _index(root, [f"- v0.2.0 — {today:%Y-%m-%d} — synthetic release"])
            self.assertIn("plain-text index line is at or above", render(index_findings(root)))

    def test_export_tree_skips_absent_changelog_reads(self) -> None:
        today = datetime.now(UTC).date()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note = _note(root, "0.2.0")
            _index(root, [_line("0.2.0", note, today)])
            self.assertTrue(absent_from_export(CHANGELOG_ROOT, root))
            self.assertEqual(index_findings(root), [])


if __name__ == "__main__":
    unittest.main()
