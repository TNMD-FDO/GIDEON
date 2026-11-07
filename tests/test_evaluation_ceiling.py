"""Exact binomial tails and the false-refusal ceiling's boundary rules."""

import ast
import sys
import unittest
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

from gideon.evaluation import ceiling, guardrails_slice


class BinomialCeiling(unittest.TestCase):
    def test_small_tails_are_hand_computed_exact_fractions(self) -> None:
        # Three fair trials have masses 1, 3, 3, 1 over 8; two trials at
        # one third have masses 4, 4, 1 over 9, reversed at two thirds.
        cases = (
            ((1, 2), 3, -1, Fraction(1)),
            ((1, 2), 3, 0, Fraction(7, 8)),
            ((1, 2), 3, 1, Fraction(1, 2)),
            ((1, 2), 3, 2, Fraction(1, 8)),
            ((1, 2), 3, 3, Fraction(0)),
            ((1, 2), 3, 4, Fraction(0)),
            ((1, 3), 2, 0, Fraction(5, 9)),
            ((1, 3), 2, 1, Fraction(1, 9)),
            ((1, 3), 2, 2, Fraction(0)),
            ((2, 3), 2, 0, Fraction(8, 9)),
            ((2, 3), 2, 1, Fraction(4, 9)),
        )
        for rate, trials, count, expected in cases:
            with self.subTest(rate=rate, trials=trials, count=count):
                actual = ceiling.tail_above(rate, trials, count)
                self.assertIsInstance(actual, Fraction)
                self.assertEqual(actual, expected)

    def test_committed_family_ceilings_are_the_smallest_admissible_counts(self) -> None:
        for family, trials, expected in (
            ("deadline-trap", 63, 21),
            ("guidelines-range", 42, 21),
            ("sentence-credit", 47, 15),
        ):
            with self.subTest(family=family):
                rate = guardrails_slice.FALSE_REFUSAL_RATES[family]
                actual = ceiling.upper_count(rate, trials)
                self.assertEqual(actual, expected)
                self.assertLessEqual(ceiling.tail_above(rate, trials, actual), Fraction(1, 100))
                self.assertGreater(ceiling.tail_above(rate, trials, actual - 1), Fraction(1, 100))

    def test_a_tail_equal_to_the_remainder_is_allowed(self) -> None:
        self.assertEqual(ceiling.tail_above((1, 100), 1, 0), Fraction(1, 100))
        self.assertEqual(ceiling.upper_count((1, 100), 1), 0)
        self.assertEqual(ceiling.upper_count((1, 99), 1), 1)

    def test_zero_trials_give_zero(self) -> None:
        for rate in (*guardrails_slice.FALSE_REFUSAL_RATES.values(), (0, 1), (1, 1)):
            with self.subTest(rate=rate):
                self.assertEqual(ceiling.upper_count(rate, 0), 0)
                self.assertEqual(ceiling.tail_above(rate, 0, 0), Fraction(0))
                self.assertEqual(ceiling.tail_above(rate, 0, -1), Fraction(1))

    def test_zero_and_unit_rates_give_the_endpoint_counts(self) -> None:
        for trials in (0, 1, 2, 20, 63):
            with self.subTest(trials=trials):
                self.assertEqual(ceiling.upper_count((0, 5), trials), 0)
                self.assertEqual(ceiling.tail_above((0, 5), trials, 0), Fraction(0))
                self.assertEqual(ceiling.upper_count((5, 5), trials), trials)
                self.assertEqual(ceiling.tail_above((5, 5), trials, trials), Fraction(0))
                self.assertEqual(ceiling.tail_above((5, 5), trials, trials - 1), Fraction(1))

    def test_ceilings_are_bounded_and_non_decreasing_in_trials(self) -> None:
        for rate in (
            *guardrails_slice.FALSE_REFUSAL_RATES.values(),
            (0, 1), (1, 1), (1, 100), (1, 2), (99, 100),
        ):
            previous = 0
            for trials in range(81):
                with self.subTest(rate=rate, trials=trials):
                    actual = ceiling.upper_count(rate, trials)
                    self.assertGreaterEqual(actual, previous)
                    self.assertLessEqual(actual, trials)
                    previous = actual

    def test_malformed_rates_raise_value_error_in_both_functions(self) -> None:
        malformed: tuple[object, ...] = (
            None, 1, "1/2", [1, 2], {}, (), (1,), (1, 2, 3),
            (-1, 2), (1, 0), (0, 0), (1, -2), (3, 2),
            (0.0, 2), (1, 2.0), ("1", 2), (1, "2"),
            (False, 2), (1, True), (None, 2), (1, None),
        )
        for rate in malformed:
            for trials in (0, 2):
                with self.subTest(rate=rate, trials=trials):
                    with self.assertRaises(ValueError):
                        ceiling.upper_count(cast(Any, rate), trials)
                    with self.assertRaises(ValueError):
                        ceiling.tail_above(cast(Any, rate), trials, trials)

    def test_negative_or_non_integer_trials_raise_value_error_in_both_functions(self) -> None:
        for trials in (-1, -20, 0.0, 1.5, "3", None, True, False, Fraction(3)):
            with self.subTest(trials=trials):
                with self.assertRaises(ValueError):
                    ceiling.upper_count((1, 2), cast(Any, trials))
                with self.assertRaises(ValueError):
                    ceiling.tail_above((1, 2), cast(Any, trials), 0)

    def test_imports_are_standard_library_only(self) -> None:
        tree = ast.parse(Path(ceiling.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], sys.stdlib_module_names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                self.assertIsNotNone(node.module)
                assert node.module is not None
                self.assertIn(node.module.split(".")[0], sys.stdlib_module_names)
