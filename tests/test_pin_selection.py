"""Hold the hosted pin selection complete and its promised checks selected."""

import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

from tools import pinselection
from tools.exportboundary import absent_from_export

ROOT = Path(__file__).resolve().parent.parent
WATCHED = (
    "host.lock",
    "images.lock",
    "models.lock",
    "skills-lock.json",
    "requirements-dev.txt",
    "fixtures/render",
    "regenerate_render_fixtures",
    ".github/workflows",
    "ci.yml",
    "pin-watch.yml",
)
NEVER_EXCLUDED = {
    "test_images_lock": "a built pin whose inputs_digest no longer matches stays red",
    "test_toolchain_pins": "a PyYAML or Playwright bump stays red until its copy moves",
    "test_models_lock": "the embedding space revision held to the embed pin",
    "test_render": "the committed render fixtures held to their regeneration",
    "test_lock_coupling": "no test embeds a value the watch can move",
    "test_skills_vendored": "the vendored skills held to their lock",
}


def selection_findings(
    texts: Mapping[str, str],
    selected: tuple[str, ...],
    excluded: Mapping[str, str],
    root: Path,
) -> list[str]:
    """Name every misplaced module and the edit that repairs its list entry."""

    findings: list[str] = []
    selected_stems = set(selected)
    listed = selected_stems | excluded.keys()
    watched = {
        stem for stem, text in texts.items() if any(name in text for name in WATCHED)
    }
    for stem in sorted(watched - listed):
        findings.append(
            f"{stem}: unplaced watched module. Fix: add it to SELECTED or to "
            "EXCLUDED with its reason in tools/pinselection.py."
        )
    for stem in sorted(selected_stems & excluded.keys()):
        findings.append(
            f"{stem}: listed in both selections. Fix: remove the stale entry from "
            "EXCLUDED in tools/pinselection.py if selected, otherwise from SELECTED."
        )
    for stem in sorted(listed - watched):
        if stem not in texts and absent_from_export(Path("tests") / f"{stem}.py", root):
            continue
        problem = "module is missing" if stem not in texts else "names no watched name"
        findings.append(
            f"{stem}: {problem}. Fix: remove the stale entry from "
            "tools/pinselection.py."
        )
    for stem, reason in sorted(excluded.items()):
        if not reason.strip():
            findings.append(
                f"{stem}: empty exclusion reason. Fix: add its non-empty reason to "
                "EXCLUDED in tools/pinselection.py."
            )
    for stem, promise in NEVER_EXCLUDED.items():
        if stem not in selected_stems:
            findings.append(
                f"{stem}: must stay selected so {promise}. Fix: add it to SELECTED "
                "and remove it from EXCLUDED in tools/pinselection.py."
            )
    return findings


class PinSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.texts = dict.fromkeys(NEVER_EXCLUDED, "# host.lock\n")
        self.selected = tuple(NEVER_EXCLUDED)

    def test_checked_in_selection_is_complete(self) -> None:
        texts = {
            path.stem: path.read_text(encoding="utf-8")
            for path in (ROOT / "tests").glob("test_*.py")
        }
        findings = selection_findings(texts, pinselection.SELECTED, pinselection.EXCLUDED, ROOT)
        self.assertEqual(findings, [], "\n".join(findings))
        self.assertEqual(
            pinselection.SELECTED,
            tuple(sorted(set(pinselection.SELECTED))),
            "Fix: sort SELECTED and remove duplicate stems in tools/pinselection.py.",
        )

    def test_checked_in_gate_paths_are_the_selected_modules_present(self) -> None:
        expected = tuple(
            ROOT / "tests" / f"{stem}.py"
            for stem in pinselection.SELECTED
            if not absent_from_export(Path("tests") / f"{stem}.py", ROOT)
        )
        paths = pinselection.selected_paths(ROOT)
        self.assertEqual(paths, expected)
        self.assertTrue(paths, "Fix: select at least one module in tools/pinselection.py.")
        for path in paths:
            self.assertTrue(path.is_file(), f"{path.stem}: selected but missing. Fix: remove it from SELECTED.")

    def test_seeded_selection_can_be_clean(self) -> None:
        self.texts["test_unrelated"] = "# An unrelated fictitious module.\n"
        self.texts["test_command"] = "# images.lock\n"
        self.assertEqual(
            selection_findings(
                self.texts, self.selected,
                {"test_command": "Reads a pin as a fictitious command's input."}, self.root,
            ),
            [],
        )

    def test_each_watched_name_requires_placement(self) -> None:
        for name in WATCHED:
            with self.subTest(name=name):
                texts = {**self.texts, "test_unplaced": f"# A fictitious reference: {name}\n"}
                self.assertEqual(
                    selection_findings(texts, self.selected, {}, self.root),
                    ["test_unplaced: unplaced watched module. Fix: add it to SELECTED or to "
                     "EXCLUDED with its reason in tools/pinselection.py."],
                )

    def test_seeded_overlap_requires_removing_one_entry(self) -> None:
        self.texts["test_overlap"] = "# host.lock\n"
        self.assertEqual(
            selection_findings(
                self.texts, (*self.selected, "test_overlap"),
                {"test_overlap": "A fictitious command reads the pin."}, self.root,
            ),
            ["test_overlap: listed in both selections. Fix: remove the stale entry from "
             "EXCLUDED in tools/pinselection.py if selected, otherwise from SELECTED."],
        )

    def test_seeded_stale_entries_are_rejected_in_both_lists(self) -> None:
        for placement in ("selected", "excluded"):
            for present in (False, True):
                with self.subTest(placement=placement, present=present):
                    texts = dict(self.texts)
                    if present:
                        texts["test_stale"] = "# No relevant reference.\n"
                    selected = self.selected
                    excluded = {}
                    if placement == "selected":
                        selected = (*selected, "test_stale")
                    else:
                        excluded["test_stale"] = "A fictitious exclusion."
                    problem = "names no watched name" if present else "module is missing"
                    self.assertEqual(
                        selection_findings(texts, selected, excluded, self.root),
                        [f"test_stale: {problem}. Fix: remove the stale entry from "
                         "tools/pinselection.py."],
                    )

    def test_export_omission_excuses_only_an_absent_module(self) -> None:
        for present in (False, True):
            with self.subTest(present=present):
                texts = dict(self.texts)
                if present:
                    texts["test_venv_build"] = "# No relevant reference.\n"
                findings = selection_findings(
                    texts, (*self.selected, "test_venv_build"), {}, self.root,
                )
                self.assertEqual(
                    findings,
                    ["test_venv_build: names no watched name. Fix: remove the stale entry "
                     "from tools/pinselection.py."] if present else [],
                )

    def test_seeded_blank_exclusion_reason_is_rejected(self) -> None:
        self.texts["test_command"] = "# models.lock\n"
        for reason in ("", " \n\t"):
            with self.subTest(reason=reason):
                self.assertEqual(
                    selection_findings(
                        self.texts, self.selected, {"test_command": reason}, self.root,
                    ),
                    ["test_command: empty exclusion reason. Fix: add its non-empty reason "
                     "to EXCLUDED in tools/pinselection.py."],
                )

    def test_each_promised_module_must_stay_selected(self) -> None:
        for stem, promise in NEVER_EXCLUDED.items():
            for placement in ("unlisted", "excluded"):
                with self.subTest(stem=stem, placement=placement):
                    selected = tuple(name for name in self.selected if name != stem)
                    excluded = {stem: "A fictitious exclusion."} if placement == "excluded" else {}
                    findings = selection_findings(self.texts, selected, excluded, self.root)
                    self.assertIn(
                        f"{stem}: must stay selected so {promise}. Fix: add it to SELECTED "
                        "and remove it from EXCLUDED in tools/pinselection.py.",
                        findings,
                    )

    def test_selected_paths_keep_order_and_missing_kept_modules(self) -> None:
        stems = ("test_kept_existing", "test_skills_vendored", "test_kept_missing")
        tests = self.root / "tests"
        tests.mkdir()
        existing = tests / f"{stems[0]}.py"
        existing.write_text("# A fictitious module.\n", encoding="utf-8")
        omitted = tests / f"{stems[1]}.py"
        missing = tests / f"{stems[2]}.py"
        with patch.object(pinselection, "SELECTED", stems):
            self.assertEqual(pinselection.selected_paths(self.root), (existing, missing))
            omitted.write_text("# A fictitious module.\n", encoding="utf-8")
            self.assertEqual(pinselection.selected_paths(self.root), (existing, omitted, missing))
