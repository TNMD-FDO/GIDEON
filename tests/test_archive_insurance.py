"""The source archive is written by its command and read by no install path.

Only the host CLI imports the archive module. Install paths name neither
its credential nor rclone; only the archive and its provision step name
rclone. Only the archive service starts the push among rendered units.
"""

import ast
import re
import unittest
from collections.abc import Mapping
from pathlib import Path

from test_render import EXAMPLE, SECOND
from test_render import inputs as render_inputs

from gideon.host import archive
from gideon.host.render import render_all
from gideon.host.render.systemd import ArchivePushServiceArtifact

ROOT = Path(__file__).resolve().parent.parent
CLI = Path("gideon/host/cli.py")
ARCHIVE = Path("gideon/host/archive.py")
TOOLS = Path("gideon/host/steps/tools.py")
INSTALL_PATHS = (
    Path("gideon/host/install.py"),
    Path("gideon/host/upgrade.py"),
    Path("gideon/host/apply.py"),
    Path("gideon/host/weights.py"),
    Path("gideon/host/corpus/install.py"),
    Path("gideon/host/corpus/snapshots.py"),
)


def archive_imports(path: Path, text: str) -> list[int]:
    """Find archive imports at any depth, resolving relative imports too."""

    lines = []
    for node in ast.walk(ast.parse(text, filename=str(path))):
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            parts = list(path.parent.parts)
            if node.level:
                parts = parts[: len(parts) - node.level + 1]
                base = ".".join(parts + (node.module or "").split(".")).rstrip(".")
            else:
                base = node.module or ""
            targets = [base, *(f"{base}.{alias.name}" for alias in node.names)]
        else:
            continue
        if any(
            name == "gideon.host.archive" or name.startswith("gideon.host.archive.")
            for name in targets
        ):
            lines.append(node.lineno)
    return lines


def importer_findings(sources: Mapping[Path, str]) -> list[str]:
    return [
        f"{path}:{line}: archive import outside the host CLI; keep the push in its command."
        for path, text in sources.items()
        if path != CLI
        for line in archive_imports(path, text)
    ]


def install_findings(sources: Mapping[Path, str]) -> list[str]:
    problems = []
    for path in INSTALL_PATHS:
        text = sources.get(path, "")
        for line in archive_imports(path, text):
            problems.append(f"{path}:{line}: archive import; remove the install dependency.")
        for number, line_text in enumerate(text.splitlines(), 1):
            if "rclone" in line_text.lower() or archive.ARCHIVE_REMOTE_CONFIG.name in line_text:
                problems.append(
                    f"{path}:{number}: archive tool or credential; remove the install dependency."
                )
    return problems


def tool_findings(sources: Mapping[Path, str]) -> list[str]:
    return [
        f"{path}:{number}: rclone outside archive or its tools step; use the archive command."
        for path, text in sources.items()
        if path not in {ARCHIVE, TOOLS}
        for number, line in enumerate(text.splitlines(), 1)
        if "rclone" in line.lower()
    ]


def push_starts(text: str) -> bool:
    """Recognise a push in ExecStart, including continued command lines."""

    joined = re.sub(r"\\\n\s*", " ", text)
    return any(
        key.strip() == "ExecStart" and re.search(r"\barchive\s+push\b", value)
        for line in joined.splitlines()
        for key, _, value in (line.partition("="),)
    )


def unit_findings(units: Mapping[str, str]) -> list[str]:
    return [
        f"{path}: archive push in another unit; keep it in the archive service."
        for path, text in units.items()
        if Path(path).name != Path(ArchivePushServiceArtifact.relative_path).name and push_starts(text)
    ]


def render_units(site_path: Path, *, build_box: bool, no_gpu: bool) -> dict[str, str]:
    """Render one host kind's units through the render tests' inputs."""

    return {
        rendered.relative_path: rendered.content
        for rendered in render_all(render_inputs(site_path, build_box=build_box, no_gpu=no_gpu)).files
        if rendered.relative_path.startswith("systemd/")
    }


class ArchiveBoundary(unittest.TestCase):
    sources: dict[Path, str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.sources = {
            path.relative_to(ROOT): path.read_text()
            for path in sorted((ROOT / "gideon").rglob("*.py"))
        }

    def test_only_host_cli_imports_archive(self) -> None:
        self.assertEqual(
            {path for path, text in self.sources.items() if archive_imports(path, text)},
            {CLI},
        )
        self.assertEqual(importer_findings(self.sources), [])

    def test_import_rule_fires_on_seeded_absolute_relative_and_deferred_imports(self) -> None:
        for path, text in (
            (Path("gideon/host/fictitious.py"), "import gideon.host.archive as writer"),
            (Path("gideon/host/fictitious.py"), "from gideon.host import archive"),
            (Path("gideon/host/fictitious.py"), "from . import archive as writer"),
            (Path("gideon/host/fictitious.py"), "def run():\n    from .archive import AUDIT_KIND"),
            (Path("gideon/host/corpus/fictitious.py"), "from .. import archive"),
            (Path("gideon/host/corpus/__init__.py"), "from ..archive import AUDIT_KIND"),
        ):
            with self.subTest(path=path, text=text):
                findings = importer_findings({path: text})
                self.assertEqual(len(findings), 1)
                self.assertIn(str(path), findings[0])
                self.assertIn("keep the push in its command", findings[0])
                self.assertEqual(importer_findings({CLI: text}), [])
        self.assertEqual(importer_findings({TOOLS: 'message = "import gideon.host.archive"'}), [])

    def test_install_paths_have_no_archive_dependency(self) -> None:
        self.assertTrue(all(path in self.sources for path in INSTALL_PATHS))
        self.assertEqual(install_findings(self.sources), [])

    def test_install_rule_fires_on_each_seeded_dependency(self) -> None:
        for path in INSTALL_PATHS:
            for text in (
                "from gideon.host.archive import run_archive_push",
                f"credential = {str(archive.ARCHIVE_REMOTE_CONFIG)!r}",
                f"credential = {archive.ARCHIVE_REMOTE_CONFIG.name!r}",
                'command = ["rclone", "copy"]',
            ):
                with self.subTest(path=path, text=text):
                    findings = install_findings({path: text})
                    self.assertEqual(len(findings), 1)
                    self.assertIn(str(path), findings[0])
                    self.assertIn("remove the install dependency", findings[0])
            self.assertEqual(install_findings({path: 'command = ["fictitious-tool"]'}), [])

    def test_only_archive_and_tools_name_rclone(self) -> None:
        self.assertEqual(
            {path for path, text in self.sources.items() if "rclone" in text.lower()},
            {ARCHIVE, TOOLS},
        )
        self.assertEqual(tool_findings(self.sources), [])

    def test_tool_rule_fires_on_seeded_name(self) -> None:
        path = Path("gideon/host/fictitious.py")
        for text in ('command = ["rclone", "copy"]', "# run rclone here", "def run_rclone(): pass"):
            with self.subTest(text=text):
                findings = tool_findings({path: text})
                self.assertEqual(len(findings), 1)
                self.assertIn(str(path), findings[0])
                self.assertIn("use the archive command", findings[0])
                self.assertEqual(tool_findings({ARCHIVE: text, TOOLS: text}), [])

    def test_only_archive_service_starts_push_on_all_host_kinds(self) -> None:
        for name, site_path, build_box, no_gpu in (
            ("build-box", EXAMPLE, True, False),
            ("office", SECOND, False, False),
            ("no-gpu", EXAMPLE, False, True),
        ):
            with self.subTest(host=name):
                units = render_units(site_path, build_box=build_box, no_gpu=no_gpu)
                self.assertTrue(units)
                self.assertEqual(unit_findings(units), [])
                self.assertEqual(
                    {Path(path).name for path, text in units.items() if push_starts(text)},
                    {Path(ArchivePushServiceArtifact.relative_path).name} if build_box else set(),
                )

    def test_unit_rule_fires_on_seeded_exec_start(self) -> None:
        path = "systemd/fictitious.service"
        for text in (
            "[Service]\nExecStart=/usr/bin/gideon archive push\n",
            "[Service]\nExecStart=-/usr/bin/python3 -m gideon archive push\n",
            "[Service]\nExecStart=/usr/bin/gideon archive \\\n    push\n",
        ):
            with self.subTest(text=text):
                findings = unit_findings({path: text})
                self.assertEqual(len(findings), 1)
                self.assertIn(path, findings[0])
                self.assertIn("keep it in the archive service", findings[0])
                self.assertEqual(unit_findings({Path(ArchivePushServiceArtifact.relative_path).name: text}), [])
        self.assertEqual(unit_findings({path: (
            "# ExecStart=/usr/bin/gideon archive push\n"
            "ExecStart=/usr/bin/gideon status\n"
            "Description=archive push\n"
            "ExecStartPost=/usr/bin/gideon archive push\n"
        )}), [])
