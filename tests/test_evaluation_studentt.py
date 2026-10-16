"""Published critical values and symmetry hold the Student's t arithmetic."""

import ast
import unittest
from pathlib import Path

from gideon.evaluation import studentt


class StudentT(unittest.TestCase):
    """The decision's two-sided critical values need only the standard library."""

    def test_two_sided_95_percent_quantiles_match_the_table(self) -> None:
        # NIST/SEMATECH e-Handbook 1.3.6.7.2, column 0.975.
        # The requested 151-df reading supplements the table; 10,000 df
        # reads as its infinite-df (normal) limit at three decimals.
        for df, expected in (
            (1, 12.706),
            (2, 4.303),
            (3, 3.182),
            (5, 2.571),
            (10, 2.228),
            (19, 2.093),
            (20, 2.086),
            (30, 2.042),
            (60, 2.000),
            (120, 1.980),
            (151, 1.976),
            (10_000, 1.960),
        ):
            with self.subTest(df=df):
                self.assertAlmostEqual(
                    studentt.two_sided_quantile(0.95, df), expected, places=3
                )

    def test_cdf_at_zero_is_one_half(self) -> None:
        for df in (1, 2, 3, 5, 10, 19, 20, 30, 60, 120, 151, 10_000):
            with self.subTest(df=df):
                self.assertEqual(studentt.cdf(0.0, df), 0.5)

    def test_cdf_is_symmetric(self) -> None:
        for df in (1, 2, 3, 5, 10, 19, 20, 30, 60, 120, 151, 10_000):
            for t in (0.1, 1.0, 2.0, 12.706):
                with self.subTest(df=df, t=t):
                    self.assertAlmostEqual(
                        studentt.cdf(-t, df), 1 - studentt.cdf(t, df), places=14
                    )

    def test_quantile_encloses_the_requested_probability(self) -> None:
        for df in (1, 2, 19, 151):
            for confidence in (0.5, 0.9, 0.95, 0.99):
                with self.subTest(df=df, confidence=confidence):
                    t = studentt.two_sided_quantile(confidence, df)
                    self.assertGreater(t, 0)
                    self.assertAlmostEqual(
                        studentt.cdf(t, df) - studentt.cdf(-t, df),
                        confidence,
                        places=11,
                    )

    def test_degrees_of_freedom_below_one_raise(self) -> None:
        for df in (0, -1, -20):
            with self.subTest(df=df):
                with self.assertRaises(ValueError):
                    studentt.cdf(0.0, df)
                with self.assertRaises(ValueError):
                    studentt.two_sided_quantile(0.95, df)

    def test_confidence_outside_the_open_unit_interval_raises(self) -> None:
        for confidence in (-0.1, 0.0, 1.0, 1.1, float("nan")):
            with self.subTest(confidence=confidence), self.assertRaises(ValueError):
                studentt.two_sided_quantile(confidence, 19)

    def test_imports_are_math_and_typing_alone(self) -> None:
        tree = ast.parse(Path(studentt.__file__).read_text(encoding="utf-8"))
        imports: set[str | None] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module)
                self.assertEqual(node.level, 0)
        self.assertEqual(imports, {"math", "typing"})
