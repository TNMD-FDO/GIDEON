"""The copies of a pinned version that no install reads, held to their source.

CI installs the development toolchain from the one file, so a ruff, mypy, or
pytest copy cannot drift: there is none. Two copies are read by nothing that
installs from the file — `pin-watch.yml`'s PyYAML install line and the Playwright
constant the browser mode checks on the box — and this module holds them equal to
`requirements-dev.txt`. The `gideon` image's build arguments run the other way:
`images.lock` is the source and `requirements-dev.txt` carries the copy, so mypy
and the unit suite see the service's imports, and each build argument is
matched to its requirement by normalized project name. The case-citation
packages but lxml and regex are the exception, held absent from the file: no
module the suite or mypy imports needs them, since the extraction leg runs
them inside the image; lxml serves the worker's opinion parser and regex is
in the token counter's dependency set.
Dependabot's pip ignore list names the same copies, so it proposes none of
them, and this module holds the list equal to them (`setuptools` beside, for
the packaging floor): six joined the image at `v0.3.148` without the list, and
Dependabot opened a red regex bump.
It states no version: every value is read from the tree.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from gideon.evaluation.turns import chromium
from gideon.host.images import BuiltImagePin, load_image_lock
from tools.exportboundary import in_export_tree

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = Path("requirements-dev.txt")
PIN_WATCH = Path(".github/workflows/pin-watch.yml")
DEPENDABOT = Path(".github/dependabot.yml")
IMAGE_ONLY_ARGUMENTS = frozenset({
    "EYECITE_VERSION",
    "REPORTERS_DB_VERSION",
    "COURTS_DB_VERSION",
    "PYAHOCORASICK_VERSION",
    "FAST_DIFF_MATCH_PATCH_VERSION",
})
_PIN = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>\S+)$")
_INSTALL = re.compile(r"pip install PyYAML==(?P<version>\S+)")


def normalize_project_name(name: str) -> str:
    """Normalize a package name as PyPI does for lock-to-requirement lookup."""

    return re.sub(r"[-_.]+", "-", name).casefold()


def requirement(root: Path, name: str) -> str | None:
    """The exact version requirements-dev.txt pins for a package, or None."""

    for line in (root / REQUIREMENTS).read_text(encoding="utf-8").splitlines():
        match = _PIN.fullmatch(line.strip())
        if match is not None and normalize_project_name(match.group("name")) == normalize_project_name(name):
            return match.group("version")
    return None


def pin_watch_pyyaml(root: Path) -> str | None:
    """The PyYAML version pin-watch.yml's install line names, or None."""

    match = _INSTALL.search((root / PIN_WATCH).read_text(encoding="utf-8"))
    return None if match is None else match.group("version")


def dependabot_pip_ignores(root: Path) -> frozenset[str]:
    """The normalized names dependabot.yml's pip entry ignores."""

    config = yaml.safe_load((root / DEPENDABOT).read_text(encoding="utf-8"))
    entry = next(update for update in config["updates"] if update["package-ecosystem"] == "pip")
    return frozenset(normalize_project_name(item["dependency-name"]) for item in entry.get("ignore", ()))


def gideon_pin() -> BuiltImagePin:
    """The gideon image's built pin from the committed images.lock."""

    result = load_image_lock(ROOT / "images.lock")
    assert result.errors == () and result.lock is not None, result.errors
    pin = next(pin for pin in result.lock.images if pin.name == "gideon")
    assert isinstance(pin, BuiltImagePin)
    return pin


def image_copies(pin: BuiltImagePin) -> frozenset[str]:
    """The normalized names requirements-dev.txt copies from the gideon image."""

    names = {
        normalize_project_name(argument.removesuffix("_VERSION").replace("_", "-"))
        for argument in pin.build_args
        if argument not in IMAGE_ONLY_ARGUMENTS
    }
    # One argument installs both driver wheels in the image.
    names.add(normalize_project_name("psycopg-binary"))
    return frozenset(names)


class ToolchainCopies(unittest.TestCase):
    def test_gideon_image_arguments_match_dev_copies_or_stay_image_only(self) -> None:
        pin = gideon_pin()
        self.assertTrue(
            pin.build_args.keys() >= IMAGE_ONLY_ARGUMENTS,
            "Fix: add all image-only arguments to images.lock",
        )
        for argument, version in pin.build_args.items():
            package = argument.removesuffix("_VERSION").replace("_", "-")
            with self.subTest(argument=argument):
                if argument in IMAGE_ONLY_ARGUMENTS:
                    self.assertIsNone(
                        requirement(ROOT, package),
                        f"Fix: remove requirements-dev.txt's image-only {package} line",
                    )
                    continue
                self.assertEqual(
                    requirement(ROOT, package),
                    version,
                    f"Fix: move requirements-dev.txt's {package} line with images.lock",
                )
        # One argument installs both driver wheels in the image.
        self.assertEqual(
            requirement(ROOT, "psycopg-binary"),
            pin.build_args["PSYCOPG_VERSION"],
            "Fix: move requirements-dev.txt's psycopg-binary line with PSYCOPG_VERSION",
        )

    def test_dependabot_ignores_exactly_the_image_copies(self) -> None:
        if in_export_tree(ROOT):
            self.skipTest("dependabot.yml is excluded from the public export")
        ignored = dependabot_pip_ignores(ROOT) - {"setuptools"}
        copies = image_copies(gideon_pin())
        self.assertEqual(
            sorted(copies - ignored),
            [],
            "Fix: add each image copy to dependabot.yml's pip ignore list",
        )
        self.assertEqual(
            sorted(ignored - copies),
            [],
            "Fix: remove each name from dependabot.yml's pip ignore list that the image no longer installs",
        )

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
            "gideon/evaluation/turns/chromium.py's PLAYWRIGHT_VERSION together",
        )

    def test_readers_take_their_values_from_the_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / PIN_WATCH).parent.mkdir(parents=True)
            (root / PIN_WATCH).write_text("run: python -m pip install PyYAML==1000.0.1\n", encoding="utf-8")
            (root / REQUIREMENTS).write_text("PyYAML==1000.0.0\nplaywright==1000.0.0\n", encoding="utf-8")
            (root / DEPENDABOT).write_text(
                "updates:\n"
                "  - package-ecosystem: github-actions\n"
                "  - package-ecosystem: pip\n"
                "    ignore:\n"
                "      - dependency-name: Typing_Extensions\n",
                encoding="utf-8",
            )
            self.assertEqual(requirement(root, "pyyaml"), "1000.0.0")
            self.assertEqual(pin_watch_pyyaml(root), "1000.0.1")
            self.assertIsNone(requirement(root, "ruff"))
            self.assertEqual(dependabot_pip_ignores(root), {"typing-extensions"})


if __name__ == "__main__":
    unittest.main()
