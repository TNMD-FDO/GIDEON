"""Every research note names the pins it was verified against (workflow ticket 05).

The tripwire: each ``docs/research/*.md`` opens with the front matter
``tools/pinwatch/notes.py`` parses, names only ids the derived vocabulary knows
(the pin watch's registry, ``models.lock``'s reference profile, and
``requirements-dev.txt``), and carries a version per item; a count floor keeps
a glob mistake from passing an empty scan. The vocabulary is derived from the
committed records, never restated (slice-0 ticket 13's lock-value rule), and
the parser's negatives use visibly fictitious names and versions.
"""

import unittest
from pathlib import Path

from gideon.host.images import load_image_lock
from gideon.host.lock import load_host_lock
from gideon.host.models import load_models_lock
from tools.pinwatch import notes
from tools.pinwatch.pins import pin_registry
from tools.pinwatch.skills import parse_provenance

ROOT = Path(__file__).resolve().parent.parent
# Today's committed note count is a floor so a glob mistake cannot empty the scan.
MINIMUM_NOTE_COUNT = 13


def committed_vocabulary() -> notes.NoteVocabulary:
    image_result = load_image_lock(ROOT / "images.lock")
    host_result = load_host_lock(ROOT / "host.lock")
    models_result = load_models_lock(ROOT / "models.lock")
    if not image_result.ok or image_result.lock is None:
        raise AssertionError(image_result.errors)
    if not host_result.ok or host_result.lock is None:
        raise AssertionError(host_result.errors)
    if not models_result.ok or models_result.lock is None:
        raise AssertionError(models_result.errors)
    provenance = parse_provenance(
        (ROOT / "docs" / "agents" / "tooling.md").read_text(encoding="utf-8")
    )
    pins = pin_registry(
        image_result.lock,
        host_result.lock,
        models_result.lock,
        provenance,
    )
    return notes.build_vocabulary(
        pins,
        (ROOT / "requirements-dev.txt").read_text(encoding="utf-8"),
    )


class ResearchNoteContracts(unittest.TestCase):
    """Hold committed notes to the front-matter and vocabulary contract."""

    def test_committed_notes_parse_against_derived_vocabulary(self) -> None:
        """Every committed note has non-empty versions and known pin ids."""

        paths = sorted((ROOT / "docs" / "research").glob("*.md"))
        self.assertGreaterEqual(len(paths), MINIMUM_NOTE_COUNT)
        vocabulary = committed_vocabulary()
        all_bindings: list[notes.NoteBinding] = []
        for path in paths:
            note = path.relative_to(ROOT).as_posix()
            bindings = notes.parse_front_matter(
                path.read_text(encoding="utf-8"), note
            )
            self.assertTrue(bindings)
            for binding in bindings:
                with self.subTest(note=note, pin=binding.pin):
                    self.assertTrue(binding.version.strip())
                    self.assertIsNotNone(vocabulary.namespace(binding.pin))
            all_bindings.extend(bindings)
        notes.validate_bindings(tuple(all_bindings), vocabulary)

    def test_invalid_front_matter_reports_a_fix(self) -> None:
        """Each malformed front-matter shape refuses with a corrective fix."""

        cases = {
            "no block": "# Example\n",
            "never closed": "---\nverified_against:\n  - pin: example\n    version: 9000.0.0\n# Example\n",
            "empty list": "---\nverified_against: []\n---\n# Example\n",
            "missing version": "---\nverified_against:\n  - pin: example\n---\n# Example\n",
            "unknown item key": "---\nverified_against:\n  - pin: example\n    version: 9000.0.0\n    extra: value\n---\n# Example\n",
            "wrong top-level key": "---\nother: value\n---\n# Example\n",
            "duplicate top-level key": "---\nverified_against:\n  - pin: example\n    version: 9000.0.0\nverified_against: []\n---\n# Example\n",
            "duplicate item key": "---\nverified_against:\n  - pin: example\n    pin: another-example\n    version: 9000.0.0\n---\n# Example\n",
        }
        for label, text in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(notes.NotesError) as raised:
                    notes.parse_front_matter(text, "docs/research/example.md")
                self.assertIn("Fix:", str(raised.exception))

    def test_unknown_pin_names_all_vocabulary_sources_in_the_fix(self) -> None:
        """An unknown binding identifies every source used to derive ids."""

        vocabulary = committed_vocabulary()
        binding = notes.NoteBinding(
            "docs/research/example.md", "example.unknown", "9000.0.0"
        )
        with self.assertRaises(notes.NotesError) as raised:
            notes.validate_bindings((binding,), vocabulary)
        message = str(raised.exception)
        self.assertIn("docs/research/example.md", message)
        self.assertIn("example.unknown", message)
        for source in (
            "images.lock",
            "host.lock",
            "models.lock",
            "requirements-dev.txt",
        ):
            with self.subTest(source=source):
                self.assertIn(source, message)
        self.assertIn("Fix:", message)


if __name__ == "__main__":
    unittest.main()
