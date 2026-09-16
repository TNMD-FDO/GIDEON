"""The two toolchain copies no install reads, held to requirements-dev.txt.

CI installs the development toolchain from the one file, so a ruff, mypy, or
pytest copy cannot drift: there is none. Two copies are read by nothing that
installs from the file — `pin-watch.yml`'s PyYAML install line and the Playwright
constant the browser mode checks on the box — and this module holds them equal
(workflow ticket 42). It states no version: every value is read from the tree.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from tools.exportboundary import in_export_tree
from tools.turns import chromium

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = Path("requirements-dev.txt")
PIN_WATCH = Path(".github/workflows/pin-watch.yml")
_PIN = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>\S+)$")
_INSTALL = re.compile(r"pip install PyYAML==(?P<version>\S+)")


def requirement(root: Path, name: str) -> str | None:
    """The exact version requirements-dev.txt pins for a package, or None."""

    for line in (root / REQUIREMENTS).read_text(encoding="utf-8").splitlines():
        match = _PIN.fullmatch(line.strip())
        if match is not None and match.group("name").casefold() == name.casefold():
            return match.group("version")
    return None


def pin_watch_pyyaml(root: Path) -> str | None:
    """The PyYAML version pin-watch.yml's install line names, or None."""

    match = _INSTALL.search((root / PIN_WATCH).read_text(encoding="utf-8"))
    return None if match is None else match.group("version")


class ToolchainCopies(unittest.TestCase):
    def test_pin_watch_installs_the_pinned_pyyaml(self) -> None:
        if in_export_tree(ROOT):
            self.skipTest("pin-watch.yml is excluded from the public export")
        pinned = requirement(ROOT, "PyYAML")
        self.assertIsNotNone(pinned, "Fix: add the PyYAML pin line to requirements-dev.txt")
        self.assertEqual(
            pin_watch_pyyaml(ROOT),
            pinned,
            "Fix: move pin-watch.yml's PyYAML install line with requirements-dev.txt",
        )

    def test_playwright_pin_equals_the_browser_constant(self) -> None:
        self.assertEqual(
            requirement(ROOT, "playwright"),
            chromium.PLAYWRIGHT_VERSION,
            "Fix: move requirements-dev.txt's playwright line and "
            "tools/turns/chromium.py's PLAYWRIGHT_VERSION together",
        )

    def test_readers_take_their_values_from_the_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / PIN_WATCH).parent.mkdir(parents=True)
            (root / PIN_WATCH).write_text("run: python -m pip install PyYAML==1000.0.1\n", encoding="utf-8")
            (root / REQUIREMENTS).write_text("PyYAML==1000.0.0\nplaywright==1000.0.0\n", encoding="utf-8")
            self.assertEqual(requirement(root, "pyyaml"), "1000.0.0")
            self.assertEqual(pin_watch_pyyaml(root), "1000.0.1")
            self.assertIsNone(requirement(root, "ruff"))


if __name__ == "__main__":
    unittest.main()
