"""The named figure derived from a seed positive's canned answer, and its rendered forms."""

import ast
import re
import sys
import unittest
from pathlib import Path

from gideon.evaluation.turns import figures
from gideon.guardrail.grammar import DATE_FORM

ROOT = Path(__file__).resolve().parents[1]


class Rendering(unittest.TestCase):
    def test_key_shapes_render_each_form(self) -> None:
        cases = (
            ("level-7", ("7", "seven")),
            ("7-point", ("7 points", "7 criminal history points", "seven points")),
            (
                "23-day",
                ("23-day", "23 day", "twenty-three-day", "twenty-three day", "twenty three-day"),
            ),
            ("8-month", ("8-month", "8 month", "eight-month", "eight month", "eight months")),
            ("8-year", ("8 years", "eight-year")),
            ("57-71", ("57 months", "seventy-one months")),
            ("360-life", ("360 months", "three hundred sixty")),
        )
        for key, examples in cases:
            with self.subTest(key=key):
                alternatives = figures._render(key)
                expected_count = 2 if key == "57-71" else 1
                self.assertEqual(len(alternatives), expected_count)
                for index, example in enumerate(examples):
                    with self.subTest(example=example):
                        source = alternatives[index] if key == "57-71" else alternatives[0]
                        self.assertIsNotNone(re.search(source, example))

    def test_category_forms(self) -> None:
        roman, clause = figures._render("category-IV")
        for example in ("Category IV", "IV"):
            with self.subTest(roman=example):
                self.assertIsNotNone(re.search(roman, example))
        self.assertIsNone(re.search(roman, "IVth"))
        for example in (
            "category is 4",
            "category: four",
            "Criminal History Category of 4",
        ):
            with self.subTest(clause=example):
                self.assertIsNotNone(re.search(clause, example))
        self.assertIsNone(re.search(clause, "category" + " " * 41 + "4"))

    def test_category_one_avoids_the_pronoun(self) -> None:
        roman, _ = figures._render("category-I")
        self.assertIsNotNone(re.search(roman, "Category I"))
        self.assertIsNone(re.search(roman, "I"))

    def test_qualified_day_count(self) -> None:
        source = figures._render("30-day")[0]
        for example in ("30 more days", "thirty calendar days", "30-business day"):
            with self.subTest(count=example):
                self.assertIsNotNone(re.search(source, example))

    def test_bold_marks_around_a_join_still_match(self) -> None:
        examples = {
            "23-day": ("**23** days", "23 **days**", "**23**-day", "23 **calendar** days"),
            "7-point": ("**7** points", "**seven** criminal history points"),
            "category-I": ("**Category** I", "Category **I**"),
        }
        for key, forms in examples.items():
            source = figures._render(key)[0]
            for form in forms:
                with self.subTest(key=key, form=form):
                    self.assertIsNotNone(re.search(source, form))

    def test_category_pair_renders_nothing(self) -> None:
        self.assertEqual(figures._render("20/III"), ())

    def test_date_forms_are_read_by_guardrail_grammar(self) -> None:
        source, = figures._render("2032-08-29")
        for example in (
            "August 29, 2032",
            "29th of August, 2032",
            "08/29/32",
            "8/29/2032",
            "2032-08-29",
        ):
            with self.subTest(date=example):
                self.assertIsNotNone(re.search(source, example))
                self.assertIsNotNone(DATE_FORM.search(example))

    def test_month_year_and_season_year_forms(self) -> None:
        month_year, = figures._render("2032-08")
        for example in ("August 2032", "August of 2032", "08/2032"):
            with self.subTest(month_year=example):
                self.assertIsNotNone(re.search(month_year, example))

        season_year, = figures._render("2032-winter")
        for example in ("winter 2032", "the winter of 2032", "winter-2032"):
            with self.subTest(season_year=example):
                self.assertIsNotNone(re.search(season_year, example))

    def test_numeral_forms(self) -> None:
        examples = {
            "7": ("7", "seven"),
            "21": ("21", "twenty-one", "twenty one"),
            "99": ("99", "ninety-nine", "ninety nine"),
            "100": ("100", "one hundred"),
            "360": ("360", "three hundred and sixty", "three-hundred-sixty"),
            "412": ("412", "four hundred and twelve", "four-hundred-and-twelve"),
            "1000": ("1000",),
        }
        for digits, forms in examples.items():
            with self.subTest(number=digits):
                source, = figures._render(f"level-{digits}")
                for form in forms:
                    with self.subTest(form=form):
                        self.assertIsNotNone(re.search(source, form))
                if digits == "1000":
                    self.assertIsNone(re.search(source, "one thousand"))


class Derivation(unittest.TestCase):
    def test_prompt_subtraction_and_alternative_drop(self) -> None:
        answer = "Offense level 7 and category III."
        derived = figures.derive("guidelines", "My offense level is 7.", answer)
        self.assertEqual(tuple(key for key, _ in derived), ("category-III",))

        roman_omitted = figures.derive("guidelines", "The numeral III appears.", "category III")
        self.assertEqual(tuple(key for key, _ in roman_omitted), ("category-III",))
        self.assertEqual(len(roman_omitted[0][1]), 1)
        self.assertIsNotNone(re.search(roman_omitted[0][1][0], "category 3"))

        self.assertEqual(figures.derive("guidelines", "A bare 7 appears.", "offense level 7"), ())

    def test_merge_orders_sources_and_collapses_duplicates(self) -> None:
        derived = (("first", ("derived-a", "shared")), ("second", ("derived-b", "shared")))
        self.assertEqual(
            figures.merge(("written", "shared"), derived),
            ("written", "shared", "derived-a", "derived-b"),
        )

    def test_unknown_family_and_empty_answer_derive_nothing(self) -> None:
        self.assertEqual(figures.derive("unlisted", "prompt", "answer"), ())
        self.assertEqual(figures.derive("deadline", "prompt", ""), ())

    def test_derivation_is_deterministic(self) -> None:
        arguments = ("guidelines", "Where does he fall?", "Seven points put him in category IV.")
        self.assertEqual(figures.derive(*arguments), figures.derive(*arguments))

    def test_module_imports_are_standard_library_or_guardrail(self) -> None:
        module_path = ROOT / "gideon/evaluation/turns/figures.py"
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules.append(node.module)
        for module in modules:
            root = module.split(".", 1)[0]
            with self.subTest(module=module):
                self.assertTrue(
                    root in sys.stdlib_module_names or module.startswith("gideon.guardrail"),
                    module,
                )
