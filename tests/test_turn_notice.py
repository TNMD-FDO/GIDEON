"""The fixed abstention notice and its no-green-state assertion."""

import ast
import sys
import unittest
from pathlib import Path

from test_host_import_boundary import absolute_targets

from gideon.turn import notice


def assert_no_green_state(case: unittest.TestCase, text: str) -> None:
    """Assert no check mark, green, good law, valid, or current, so no rendered text implies a source is safe."""

    case.assertNotRegex(text, r"[\u2713\u2714\u2611\u2705]")
    case.assertNotRegex(text, r"(?i)\b(?:green|good\s+law|valid|current)\b")


class TurnNotice(unittest.TestCase):
    def test_notice_paragraphs_follow_the_supplied_labels_and_courts(self) -> None:
        for lockfiles, courts, expected in (
            (
                ("corpus-2099-01-01",), (),
                "Legal found nothing in GIDEON's corpus that supports an answer to this question.\n\n"
                "Searched: case law as of corpus-2099-01-01.\n\n"
                "Rephrasing may help. A question about a source GIDEON does not hold cannot be answered here.",
            ),
            (
                ("corpus-2099-01-02", "corpus-2099-01-01"), (),
                "Legal found nothing in GIDEON's corpus that supports an answer to this question.\n\n"
                "Searched: case law as of corpus-2099-01-02, corpus-2099-01-01.\n\n"
                "Rephrasing may help. A question about a source GIDEON does not hold cannot be answered here.",
            ),
            (
                ("corpus-2099-01-01",), ("Western Court of Example",),
                "Legal found nothing in GIDEON's corpus that supports an answer to this question.\n\n"
                "Searched: case law as of corpus-2099-01-01.\n\n"
                "The court you named, Western Court of Example, is outside GIDEON's corpus.\n\n"
                "Rephrasing may help. A question about a source GIDEON does not hold cannot be answered here.",
            ),
            (
                ("corpus-2099-01-01",), ("Western Court of Example", "Eastern Court of Example"),
                "Legal found nothing in GIDEON's corpus that supports an answer to this question.\n\n"
                "Searched: case law as of corpus-2099-01-01.\n\n"
                "The court you named, Western Court of Example, is outside GIDEON's corpus.\n\n"
                "The court you named, Eastern Court of Example, is outside GIDEON's corpus.\n\n"
                "Rephrasing may help. A question about a source GIDEON does not hold cannot be answered here.",
            ),
        ):
            with self.subTest(lockfiles=lockfiles, courts=courts):
                text = notice.abstention_notice(lockfiles, courts)
                self.assertEqual(text, expected)
                self.assertNotRegex(text, r"(?i)@|\b(?:contact|call|e-?mail|phone|chat|general|assistant)\b")
                self.assertNotRegex(text, r"(?<!\d)(?:\+?1[ .-]?)?(?:\(\d{3}\)[ .-]?|\d{3}[ .-]?)?\d{3}[ .-]?\d{4}(?!\d)")
                assert_no_green_state(self, text)

    def test_notice_requires_a_lockfile_label(self) -> None:
        with self.assertRaises(ValueError):
            notice.abstention_notice((), ())

    def test_notice_imports_only_the_standard_library(self) -> None:
        assert notice.__file__ is not None
        path = Path(notice.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for target in absolute_targets(node, path):
                    with self.subTest(line=node.lineno, target=target):
                        self.assertIn(target.partition(".")[0], sys.stdlib_module_names)
