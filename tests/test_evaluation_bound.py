"""The reported rule-of-three bound describes clean runs and never failures."""

import ast
import sys
import unittest
from pathlib import Path

from gideon.evaluation import bound

# Fictitious clean-run counts and hand-calculated percentages, with rounding
# confined to the displayed figure.
CLEAN_RUNS: tuple[tuple[int, float | None, str | None], ...] = (
    (0, None, None),
    (1, 100.0, "100.0"),
    (2, 100.0, "100.0"),
    (3, 100.0, "100.0"),
    (6, 50.0, "50.0"),
    (39, 100 / 13, "7.7"),
    (40, 7.5, "7.5"),
    (298, 150 / 149, "1.0"),
)


class EvaluationBound(unittest.TestCase):
    def test_percentages_are_capped_and_unrounded(self) -> None:
        for count, expected, _ in CLEAN_RUNS:
            with self.subTest(count=count):
                actual = bound.upper_bound_percent(count)
                if expected is None:
                    self.assertIsNone(actual)
                else:
                    assert actual is not None
                    self.assertAlmostEqual(actual, expected)

    def test_passing_text_states_the_count_and_rounded_bound(self) -> None:
        rule = f"{bound.RULE_NUMERATOR}/n at {bound.ONE_SIDED_LEVEL_PERCENT} %"
        for count, _, displayed in CLEAN_RUNS:
            with self.subTest(count=count):
                cases = f"{count} {'case' if count == 1 else 'cases'}"
                expected = (
                    f"{cases}, no bound"
                    if displayed is None
                    else f"{cases}, true failure rate ≤ {displayed} % ({rule})"
                )
                self.assertEqual(bound.bound_text(count, True), expected)

    def test_the_texts_read_as_stated(self) -> None:
        self.assertEqual(
            bound.bound_text(39, True),
            "39 cases, true failure rate ≤ 7.7 % (3/n at 95 %)",
        )
        self.assertEqual(
            bound.bound_text(1, True),
            "1 case, true failure rate ≤ 100.0 % (3/n at 95 %)",
        )
        self.assertEqual(
            bound.bound_text(39, False),
            "39 cases, failed: no bound (3/n at 95 % is a clean run's)",
        )

    def test_failing_text_never_states_a_bound(self) -> None:
        rule = f"{bound.RULE_NUMERATOR}/n at {bound.ONE_SIDED_LEVEL_PERCENT} %"
        for count, _, _ in CLEAN_RUNS:
            with self.subTest(count=count):
                cases = f"{count} {'case' if count == 1 else 'cases'}"
                expected = (
                    f"{cases}, no bound"
                    if count == 0
                    else f"{cases}, failed: no bound ({rule} is a clean run's)"
                )
                self.assertEqual(bound.bound_text(count, False), expected)

    def test_imports_are_standard_library_only(self) -> None:
        tree = ast.parse(Path(bound.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], sys.stdlib_module_names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                self.assertIsNotNone(node.module)
                assert node.module is not None
                self.assertIn(node.module.split(".")[0], sys.stdlib_module_names)
