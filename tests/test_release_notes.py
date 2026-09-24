"""Hold release notes to the versioned template contract."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

import gideon
from gideon.host.upgrade import Version
from tools.exportboundary import absent_from_export
from tools.pinwatch import bumped

ROOT = Path(__file__).resolve().parent.parent
RELEASE_NOTES = ROOT / "docs" / "release-notes"
TEMPLATE = RELEASE_NOTES / "TEMPLATE.md"
# One entry per template version; a later version freezes the earlier list here so
# a note written under it stays judged by its own template.
KNOWN_TEMPLATE_HEADINGS: dict[int, tuple[str, ...]] = {
    1: ("## What is new", "## Coverage", "## Next maintenance window"),
    2: (
        "## What is new",
        "## What was bumped",
        "## Coverage",
        "## Next maintenance window",
    ),
    3: (
        "## What is new",
        "## What was bumped",
        "## Coverage",
        "## Next maintenance window",
    ),
}
# The practice rule is the sentence every pre-launch note carries verbatim.
PRACTICE_RULE = (
    "Nothing from General goes into a court filing unchecked, and until go-live "
    "there is no Legal chat to check it in."
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HEADING_RE = re.compile(r"^#{1,6}\s+.+$", re.MULTILINE)
_H2_RE = re.compile(r"^##\s+.+$", re.MULTILINE)
_TEMPLATE_RE = re.compile(r"^Template:\s*(\d+)\s*$", re.MULTILINE)
_PLACEHOLDER_RE = re.compile(r"<[^>\n]+>")
_MINIMUM_NOTE_VERSION = Version.parse("0.2.0")


def _without_comments(text: str) -> str:
    return _COMMENT_RE.sub("", text)


def _headings(text: str) -> list[str]:
    return [match.group(0) for match in _H2_RE.finditer(_without_comments(text))]


def _template_version(text: str) -> int | None:
    match = _TEMPLATE_RE.search(text)
    return None if match is None else int(match.group(1))


def _section_text(text: str, heading: str) -> str:
    visible = _without_comments(text)
    match = re.search(
        rf"^{re.escape(heading)}\s*$", visible, flags=re.MULTILINE
    )
    if match is None:
        return ""
    following = visible[match.end() :]
    next_heading = _HEADING_RE.search(following)
    body = following if next_heading is None else following[: next_heading.start()]
    return body.strip()


def _finding(path: Path, problem: str, fix: str) -> str:
    return f"{path}: {problem}. Fix: {fix}"


def template_findings(path: Path = TEMPLATE) -> list[str]:
    """Return template-contract findings, each ending with its fix."""

    findings: list[str] = []
    newest = max(KNOWN_TEMPLATE_HEADINGS)
    if not path.is_file():
        return [
            _finding(
                path,
                "release-note template is missing",
                f"write the version {newest} template at {TEMPLATE}",
            )
        ]

    text = path.read_text(encoding="utf-8")
    version = _template_version(text)
    if version is None:
        findings.append(
            _finding(
                path,
                "template does not declare its version",
                f"copy the Template: {newest} metadata line into {TEMPLATE}",
            )
        )
    elif version != newest:
        findings.append(
            _finding(
                path,
                f"template declares unknown or stale version {version}",
                f"update {TEMPLATE} to the newest known template version {newest}",
            )
        )

    visible = _without_comments(text)
    titles = re.findall(r"^#\s+(.+)$", visible, flags=re.MULTILINE)
    expected_title = "GIDEON v<x.y.z>"
    if titles != [expected_title]:
        findings.append(
            _finding(
                path,
                f"template title must be # {expected_title}",
                f"restore the tag title in {TEMPLATE}",
            )
        )

    headings = _headings(text)
    expected_headings = KNOWN_TEMPLATE_HEADINGS[newest]
    if tuple(headings) != expected_headings:
        findings.append(
            _finding(
                path,
                f"required headings are {expected_headings}, found {tuple(headings)}",
                f"restore the version {newest} headings in {TEMPLATE}",
            )
        )

    if not any(PRACTICE_RULE in comment for comment in _COMMENT_RE.findall(text)):
        findings.append(
            _finding(
                path,
                "the optional Rules of use comment is missing the practice rule",
                f"restore the verbatim practice rule in {TEMPLATE}",
            )
        )
    return findings


def note_findings(
    path: Path, template_headings: dict[int, tuple[str, ...]] = KNOWN_TEMPLATE_HEADINGS
) -> list[str]:
    """Return conformance findings for one release note."""

    findings: list[str] = []
    text = path.read_text(encoding="utf-8")
    try:
        version = Version.from_tag(path.stem)
    except (TypeError, ValueError):
        findings.append(
            _finding(
                path,
                "file name is not a SemVer release tag",
                f"copy {TEMPLATE} to docs/release-notes/v<x.y.z>.md and fill it in",
            )
        )
        version = None

    visible = _without_comments(text)
    titles = re.findall(r"^#\s+(.+)$", visible, flags=re.MULTILINE)
    if version is not None and titles != [f"GIDEON v{version}"]:
        findings.append(
            _finding(
                path,
                f"title must be # GIDEON v{version}",
                f"fix the title in {path}",
            )
        )
    elif version is None and not titles:
        findings.append(
            _finding(
                path,
                "release note has no title",
                f"copy the tag title from {TEMPLATE} into {path}",
            )
        )

    note_template_version = _template_version(text)
    if note_template_version not in template_headings:
        findings.append(
            _finding(
                path,
                "note does not name a known template version",
                f"copy a known Template: line from {TEMPLATE} into {path}",
            )
        )
    else:
        required = template_headings[note_template_version]
        headings = _headings(text)
        required_in_note = [heading for heading in headings if heading in required]
        if required_in_note != list(required):
            findings.append(
                _finding(
                    path,
                    f"required headings are missing or out of order: {required}",
                    f"add the required headings from {TEMPLATE} to {path} in order",
                )
            )
        headings_to_check = list(required)
        if note_template_version == 3 and "## Breaking" in headings:
            headings_to_check.append("## Breaking")
        for heading in headings_to_check:
            if not _section_text(text, heading):
                findings.append(
                    _finding(
                        path,
                        f"section {heading} has no text",
                        f"write the section text beneath {heading} in {path}",
                    )
                )
        if note_template_version >= 2:
            section_lines = _section_text(text, "## What was bumped").splitlines()
            is_none = (
                len(section_lines) == 1
                and bumped.NONE_LINE.fullmatch(section_lines[0]) is not None
            )
            is_moved = (
                len(section_lines) >= 2
                and bumped.SINCE_LINE.fullmatch(section_lines[0]) is not None
                and all(
                    bumped.MOVED_LINE.fullmatch(line) is not None
                    for line in section_lines[1:]
                )
            )
            if not (is_none or is_moved):
                findings.append(
                    _finding(
                        path,
                        "section ## What was bumped does not match the pin-watch generator grammar",
                        f"paste the complete output of python3 -m tools.pinwatch.bumped into {path}",
                    )
                )

        if note_template_version == 3:
            breaking_heading = "## Breaking"
            if breaking_heading in headings and "## What is new" in headings:
                breaking_index = headings.index(breaking_heading)
                new_index = headings.index("## What is new")
                if breaking_index != new_index + 1:
                    findings.append(
                        _finding(
                            path,
                            "section ## Breaking must immediately follow ## What is new",
                            f"move ## Breaking directly after ## What is new in {path}, following {TEMPLATE}",
                        )
                    )
            if (
                version is not None
                and version.major >= 1
                and version.minor == 0
                and version.patch == 0
                and not version.prerelease
                and breaking_heading not in headings
            ):
                findings.append(
                    _finding(
                        path,
                        "a major release note must carry section ## Breaking",
                        f"add section ## Breaking from {TEMPLATE} to {path}",
                    )
                )

    if "<!--" in text or "-->" in text:
        findings.append(
            _finding(
                path,
                "authoring HTML comments remain in the note",
                f"remove the HTML comments from {path}",
            )
        )
    if _PLACEHOLDER_RE.search(text) is not None:
        findings.append(
            _finding(
                path,
                "a template placeholder remains in the note",
                f"replace every placeholder copied from {TEMPLATE} in {path}",
            )
        )

    rules_heading = "## Rules of use"
    if rules_heading in _headings(text) and PRACTICE_RULE not in _section_text(
        text, rules_heading
    ):
        findings.append(
            _finding(
                path,
                "Rules of use does not carry the verbatim practice rule",
                f"copy the practice rule from {TEMPLATE} into {path}",
            )
        )
    return findings


def existence_findings(root: Path, current_version: str) -> list[str]:
    """Return the missing-note finding for a current version from 0.2.0 onward."""

    current = Version.parse(current_version)
    if current < _MINIMUM_NOTE_VERSION:
        return []
    expected = root / f"v{current}.md"
    if expected.is_file():
        return []
    return [
        _finding(
            expected,
            f"release note for {current} is missing",
            f"copy {TEMPLATE} to {expected} and fill it in",
        )
    ]


def _good_note() -> str:
    return (
        "# GIDEON v0.2.0\n\n"
        "Template: 1\n\n"
        "## What is new\n\nA user-visible change.\n\n"
        "## Coverage\n\nGeneral only; the Legal chat arrives at go-live on SCOTUS and the Sixth Circuit.\n\n"
        "## Next maintenance window\n\nNo window is announced.\n"
    )


def _good_note_v2() -> str:
    return (
        "# GIDEON v0.2.0\n\n"
        "Template: 2\n\n"
        "## What is new\n\nA user-visible change.\n\n"
        "## What was bumped\n\nNo pin moved since v0.1.37.\n\n"
        "## Coverage\n\nGeneral only; the Legal chat arrives at go-live on SCOTUS and the Sixth Circuit.\n\n"
        "## Next maintenance window\n\nNo window is announced.\n"
    )


# Template 3 fixture with the optional Breaking section in its required position.
def _good_note_v3() -> str:
    return (
        "# GIDEON v0.2.0\n\n"
        "Template: 3\n\n"
        "## What is new\n\nA user-visible change.\n\n"
        "## Breaking\n\nThere is no contract change for the office.\n\n"
        "## What was bumped\n\nNo pin moved since v0.1.37.\n\n"
        "## Coverage\n\nGeneral only; the Legal chat arrives at go-live on SCOTUS and the Sixth Circuit.\n\n"
        "## Next maintenance window\n\nNo window is announced.\n"
    )


class ReleaseNoteTests(unittest.TestCase):
    """Test the release-note artifact and its durable contract."""

    def test_template_contract(self) -> None:
        """The committed template declares version 3 and exactly its required sections."""

        if absent_from_export(TEMPLATE.relative_to(ROOT), ROOT):
            self.skipTest("the release-note template is excluded from the public export")
        self.assertEqual(template_findings(), [])

    def test_each_committed_note_conforms(self) -> None:
        """Every committed note follows the template version it names."""

        for path in sorted(RELEASE_NOTES.glob("v*.md")):
            with self.subTest(note=path.name):
                self.assertEqual(note_findings(path), [])

    def test_current_release_has_its_note(self) -> None:
        """From v0.2.0 every tag ships a note for the version the package declares."""

        self.assertEqual(existence_findings(RELEASE_NOTES, gideon.__version__), [])

    def test_broken_shapes_are_reported_in_isolated_fixtures(self) -> None:
        """Each malformed note shape is refused with the finding that names its repair."""

        good = _good_note()
        cases: tuple[tuple[str, str, str], ...] = (
            ("bad-name.md", good, "file name is not a SemVer release tag"),
            (
                "v0.2.1.md",
                good.replace("# GIDEON v0.2.0", "# GIDEON v0.2.1").replace("## Coverage", "## Missing"),
                "required headings are missing or out of order",
            ),
            (
                "v0.2.2.md",
                good.replace("Template: 1", "Template: 2"),
                "required headings are missing or out of order",
            ),
            ("v0.2.3.md", good.replace("# GIDEON v0.2.0", "# GIDEON v9.9.9"), "title must be # GIDEON v0.2.3"),
            (
                "v0.2.4.md",
                good.replace("A user-visible change.", "<!-- authoring note -->"),
                "authoring HTML comments remain",
            ),
            ("v0.2.5.md", good.replace("A user-visible change.", "<…>"), "a template placeholder remains"),
            (
                "v0.2.6.md",
                good.replace("## Next maintenance window\n\nNo window is announced.", "## Next maintenance window\n\n"),
                "section ## Next maintenance window has no text",
            ),
            (
                "v0.2.7.md",
                good.replace("## Coverage\n\n", "## Coverage\n\n## What is new\n\n"),
                "required headings are missing or out of order",
            ),
            (
                "v0.2.8.md",
                good.replace(
                    "## Next maintenance window\n\nNo window is announced.",
                    "## Rules of use\n\nNot the rule.\n\n## Next maintenance window\n\nNo window is announced.",
                ),
                "Rules of use does not carry the verbatim practice rule",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good_path = root / "v0.2.0.md"
            good_path.write_text(good, encoding="utf-8")
            self.assertEqual(note_findings(good_path), [])
            good_v2_path = root / "v0.2.10.md"
            good_v2 = _good_note_v2().replace("# GIDEON v0.2.0", "# GIDEON v0.2.10")
            good_v2_path.write_text(good_v2, encoding="utf-8")
            self.assertEqual(note_findings(good_v2_path), [])
            good_v3_path = root / "v0.2.12.md"
            good_v3 = _good_note_v3().replace("# GIDEON v0.2.0", "# GIDEON v0.2.12")
            good_v3_path.write_text(good_v3, encoding="utf-8")
            self.assertEqual(note_findings(good_v3_path), [])
            major_without_breaking = root / "v1.0.0.md"
            major_without_breaking.write_text(
                good_v3.replace("# GIDEON v0.2.12", "# GIDEON v1.0.0").replace(
                    "## Breaking\n\nThere is no contract change for the office.\n\n", ""
                ),
                encoding="utf-8",
            )
            major_findings = note_findings(major_without_breaking)
            self.assertTrue(
                any("major release note must carry section ## Breaking" in finding for finding in major_findings),
                major_findings,
            )
            wrong_position = root / "v0.2.13.md"
            wrong_position.write_text(
                good_v3.replace("# GIDEON v0.2.12", "# GIDEON v0.2.13").replace(
                    "## Breaking\n\nThere is no contract change for the office.\n\n", ""
                ).replace(
                    "## Coverage", "## Breaking\n\nThere is no contract change for the office.\n\n## Coverage"
                ),
                encoding="utf-8",
            )
            position_findings = note_findings(wrong_position)
            self.assertTrue(
                any("section ## Breaking must immediately follow ## What is new" in finding for finding in position_findings),
                position_findings,
            )
            template_2_major = root / "v2.0.0.md"
            template_2_major.write_text(
                _good_note_v2().replace("# GIDEON v0.2.0", "# GIDEON v2.0.0"),
                encoding="utf-8",
            )
            self.assertEqual(note_findings(template_2_major), [])
            prose_v2_path = root / "v0.2.11.md"
            prose_v2 = good_v2.replace(
                "No pin moved since v0.1.37.", "A prose summary of the changes."
            )
            prose_v2_path.write_text(prose_v2, encoding="utf-8")
            prose_findings = note_findings(prose_v2_path)
            self.assertTrue(
                any("What was bumped does not match" in finding for finding in prose_findings),
                prose_findings,
            )
            for finding in prose_findings:
                self.assertIn(". Fix: ", finding)
            with_rule = good_path.with_name("v0.2.9.md")
            with_rule.write_text(
                good.replace("# GIDEON v0.2.0", "# GIDEON v0.2.9") + f"\n## Rules of use\n\n{PRACTICE_RULE}\n",
                encoding="utf-8",
            )
            self.assertEqual(note_findings(with_rule), [])
            for name, contents, problem in cases:
                with self.subTest(note=name):
                    path = root / name
                    path.write_text(contents, encoding="utf-8")
                    findings = note_findings(path)
                    self.assertTrue(any(problem in finding for finding in findings), findings)
                    for finding in findings:
                        self.assertIn(". Fix: ", finding)

    def test_existence_rule_starts_at_0_2_0(self) -> None:
        """A note is required for the current version from 0.2.0 on, never before."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(existence_findings(root, "0.1.2"), [])
            findings = existence_findings(root, "0.2.0")
            expected = root / "v0.2.0.md"
            self.assertEqual(len(findings), 1, findings)
            self.assertIn(str(TEMPLATE), findings[0])
            self.assertIn(str(expected), findings[0])
            expected.write_text(_good_note(), encoding="utf-8")
            self.assertEqual(existence_findings(root, "0.2.0"), [])
            self.assertEqual(len(existence_findings(root, "0.2.1")), 1)


if __name__ == "__main__":
    unittest.main()
