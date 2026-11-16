"""The named figure derived from a seed positive's canned answer, and its rendered forms."""

import ast
import re
import sys
import unittest
from pathlib import Path

from gideon.evaluation.turns import figures
from gideon.guardrail.grammar import DATE_FORM, GUIDELINES_CATEGORY_SOURCE

ROOT = Path(__file__).resolve().parents[1]


class Rendering(unittest.TestCase):
    def test_key_shapes_have_fixed_labels_in_form_order(self) -> None:
        labels_by_key = {
            "level-18": ("noun", "total", "placing", "chained", "shorthand"),
            "category-I": ("noun", "link", "shorthand"),
            "30-37": ("lower", "upper"),
            "30-30": ("lower", "upper"),
            "360-life": ("form",),
            "7-point": ("form",),
            "23-day": ("form",),
            "8-month": ("form",),
            "8-year": ("form",),
            "2032-08-29": ("form",),
            "2032-08": ("form",),
            "2032-winter": ("form",),
            "20/III": (),
            "unknown": (),
        }
        for key, expected in labels_by_key.items():
            with self.subTest(key=key):
                self.assertEqual(tuple(label for label, _ in figures._render(key)), expected)

    def test_key_shapes_render_each_form(self) -> None:
        cases = (
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
                        _, source = alternatives[index] if key == "57-71" else alternatives[0]
                        self.assertIsNotNone(re.search(source, example))

    def test_level_forms(self) -> None:
        examples = {
            "level-18": (
                "level 18",
                "offense level of 18",
                "OL 18",
                "TOL 18",
                "18/II",
                "eighteen/II",
                "level eighteen",
                "the total offense level is therefore 18",
                "total offense level at 18",
                "puts him at level 18",
                "Offense level 14, plus 4 for the firearm, for a total of 18.",
                "**Total offense level → 18**",
            ),
            "level-14": ("base offense level 14",),
            "level-21": (
                "Offense level twenty, plus one, for a total of twenty-one.",
                "Offense level twenty one, plus zero, for a total of twenty one.",
                "**Total offense level at 21**,",
                "Offense level 20, plus 2 for the firearm, plus 2 for role, "
                "minus 3, for a total of 21.",
                "Offense level 20, plus 1, for a total of **21**.",
            ),
        }
        for key, forms in examples.items():
            alternatives = figures._render(key)
            for form in forms:
                with self.subTest(key=key, form=form):
                    self.assertTrue(any(re.search(source, form) for _, source in alternatives))

    def test_level_forms_exclude_unrelated_numbers(self) -> None:
        examples = {
            "level-18": (
                "18 U.S.C. § 922(g)",
                "18",
                "eighteen",
                "the base offense level under 18 U.S.C. § 922(g) is 14.",
                "a level 20 defendant faces 18 months",
                "offense level 180",
                "offense level eighteenfold",
            ),
            "level-21": (
                "At a high level, nine exhibits plus twelve exhibits, for a total of 21.",
                "Offense level 20, plus 2, for a total of 21 months.",
                "Offense level 20. Plus 1, for a total of 21.",
                "Offense level 20, plus 1, for a total of 21 under the guideline.",
            ),
        }
        for key, forms in examples.items():
            alternatives = figures._render(key)
            for form in forms:
                with self.subTest(key=key, form=form):
                    self.assertFalse(any(re.search(source, form) for _, source in alternatives))

    def test_category_forms(self) -> None:
        examples = {
            "category-I": (
                (
                    "criminal history category I,", "CHC I", "category 1", "category one",
                    "category I yields",
                ),
                ("category is I", "category would be 1", "category: 1"),
                ("16/I", "16 / I"),
            ),
            "category-III": (
                ("Criminal History Category of 3", "CHC III", "category three"),
                ("category is III", "category: 3", "category would be III"),
                ("20/III", "20 / III"),
            ),
        }
        for key, forms in examples.items():
            alternatives = figures._render(key)
            self.assertEqual(len(alternatives), len(forms))
            for (_, source), texts in zip(alternatives, forms, strict=True):
                for text in texts:
                    with self.subTest(key=key, text=text):
                        self.assertIsNotNone(re.search(source, text))

    def test_category_forms_exclude_unrelated_numbers(self) -> None:
        examples = {
            "category-I": (
                "I", "your category is one of the inputs", "one or two prior sentences",
                "category 10", "category 1st", "category onefold", "category II",
                "category i", "category is i", "16/i", "category" + " " * 41 + "1",
            ),
            "category-II": ("Part II", "Title II", "II. Argument", "category III"),
            "category-III": (
                "III", "category is now 3", "category would be about III",
                "category: the number 3", "category is three", "category: three",
                "category isIII", "category would be3", "120/III", "category 30",
            ),
        }
        for key, texts in examples.items():
            alternatives = figures._render(key)
            for text in texts:
                with self.subTest(key=key, text=text):
                    self.assertFalse(any(re.search(source, text) for _, source in alternatives))

    def test_category_one_avoids_the_pronoun(self) -> None:
        alternatives = figures._render("category-I")
        words = (
            "am", "can", "cannot", "could", "did", "do", "had", "have", "may", "might",
            "must", "shall", "should", "was", "will", "would",
            "can't", "couldn't", "didn't", "don't", "hadn't", "haven't", "mustn't",
            "shan't", "shouldn't", "wasn't", "won't", "wouldn't",
        )
        for prefix in ("category ", "category is ", "category would be ", "category: ", "16/"):
            for apostrophe in ("'", "’"):
                for word in words:
                    text = prefix + "I " + word.replace("'", apostrophe)
                    with self.subTest(text=text):
                        self.assertFalse(any(re.search(source, text) for _, source in alternatives))
                for ending in ("d", "m", "ll", "ve"):
                    text = prefix + "I" + apostrophe + ending
                    with self.subTest(text=text):
                        self.assertFalse(any(re.search(source, text) for _, source in alternatives))

    def test_category_one_keeps_third_person_and_other_words(self) -> None:
        alternatives = figures._render("category-I")
        for prefix in ("category ", "category is ", "category would be ", "category: ", "16/"):
            for word in ("is", "has", "does", "yields", "candy", "cannotary"):
                text = prefix + "I " + word
                with self.subTest(text=text):
                    self.assertTrue(any(re.search(source, text) for _, source in alternatives))

    def test_category_nouns_match_the_guardrail_grammar_in_both_directions(self) -> None:
        nouns = (
            "category", "criminal history category", "criminal-history category",
            "CHC", "crim. hist. cat.", "crim hist cat", "crim. hist cat.",
            "subcategory", "criminalhistorycategory", "criminal  history  category",
            "CHC of", "crim. hist. cat. of", "part", "title",
        )
        for noun in nouns:
            for space in (" ", "\t", "\n", "\u00a0", "\u202f", "", "  ", "-"):
                for of in ("", f"{space}of"):
                    for roman, digit in zip(
                        ("I", "II", "III", "IV", "V", "VI"), "123456", strict=True
                    ):
                        _, source = figures._render(f"category-{roman}")[0]
                        for numeral in (roman, digit):
                            text = noun.replace(" ", space) + of + space + numeral
                            with self.subTest(text=text, key=roman):
                                grammar_match = re.fullmatch(GUIDELINES_CATEGORY_SOURCE, text, re.I)
                                figure_match = re.fullmatch(source, text)
                                if grammar_match is not None:
                                    self.assertIsNotNone(figure_match)
                                if figure_match is not None:
                                    self.assertIsNotNone(grammar_match)

    def test_qualified_day_count(self) -> None:
        _, source = figures._render("30-day")[0]
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
            _, source = figures._render(key)[0]
            for form in forms:
                with self.subTest(key=key, form=form):
                    self.assertIsNotNone(re.search(source, form))

        _, (_, link), (_, shorthand) = figures._render("category-I")
        self.assertIsNotNone(re.search(link, "**Category** is **I**"))
        self.assertIsNotNone(re.search(link, "category **would** **be** **I**"))
        self.assertIsNotNone(re.search(shorthand, "16 / **I**"))
        for text in ("Category **I** can't", "category is **I** can't", "16/**I** can't"):
            with self.subTest(text=text):
                self.assertFalse(any(
                    re.search(source, text) for _, source in figures._render("category-I")
                ))

    def test_category_pair_renders_nothing(self) -> None:
        self.assertEqual(figures._render("20/III"), ())

    def test_date_forms_are_read_by_guardrail_grammar(self) -> None:
        (_, source), = figures._render("2032-08-29")
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
        (_, month_year), = figures._render("2032-08")
        for example in ("August 2032", "August of 2032", "08/2032"):
            with self.subTest(month_year=example):
                self.assertIsNotNone(re.search(month_year, example))

        (_, season_year), = figures._render("2032-winter")
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
                (_, source), = figures._render(f"{digits}-life")
                for form in forms:
                    with self.subTest(form=form):
                        self.assertIsNotNone(re.search(source, form))
                if digits == "1000":
                    self.assertIsNone(re.search(source, "one thousand"))


class Derivation(unittest.TestCase):
    def test_prompt_subtraction_and_alternative_drop(self) -> None:
        answer = "Offense level 7 and category III."
        derived = figures.derive("guidelines", "With offense level 7.", answer)
        self.assertEqual(tuple(key for key, _ in derived), ("category-III",))

        prompt = "The numeral III appears."
        derived = figures.derive("guidelines", prompt, "category III")
        self.assertEqual(tuple(key for key, _ in derived), ("category-III",))
        self.assertEqual(derived[0][1], figures._render("category-III"))
        self.assertFalse(any(re.search(source, prompt) for _, source in derived[0][1]))

        prompt = "The category is III."
        derived = figures.derive("guidelines", prompt, "category III")
        noun, _, shorthand = figures._render("category-III")
        self.assertEqual(derived, (("category-III", (noun, shorthand)),))
        self.assertEqual(tuple(label for label, _ in derived[0][1]), ("noun", "shorthand"))

        prompt = "A bare 7 appears."
        derived = figures.derive("guidelines", prompt, "offense level 7")
        self.assertEqual(tuple(key for key, _ in derived), ("level-7",))
        self.assertTrue(any(re.search(source, "level seven") for _, source in derived[0][1]))
        self.assertFalse(any(re.search(source, prompt) for _, source in derived[0][1]))

        answer = "The range is 30-37 months."
        self.assertEqual(tuple(key for key, _ in figures.derive("guidelines", "", answer)), ("30-37",))
        self.assertEqual(figures.derive("guidelines", "Exhibits 30 and 37 are attached.", answer), ())

    def test_merge_orders_sources_and_collapses_duplicates(self) -> None:
        derived = (
            ("first", (("noun", "derived-a"), ("shorthand", "shared"))),
            ("second", (("lower", "derived-b"), ("upper", "shared"))),
        )
        self.assertEqual(
            figures.merge(("written", "shared"), derived),
            ("written", "shared", "derived-a", "derived-b"),
        )
        repeated_bound = figures._render("30-30")
        self.assertEqual(repeated_bound[0][1], repeated_bound[1][1])
        self.assertEqual(figures.merge((), (("30-30", repeated_bound),)), (repeated_bound[0][1],))

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
