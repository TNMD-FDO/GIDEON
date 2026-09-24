"""The service citation stamp's seed, detection, and decision."""

import ast
import sys
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import yaml  # type: ignore[import-untyped]

from gideon.api import stamp

ROOT = Path(__file__).resolve().parent.parent
STAMP_PATH = ROOT / "gideon/api/stamp.py"
SEED_PATH = ROOT / "eval/seed/general/citation-stamp.yaml"
SITE_EXAMPLE = ROOT / "config/site.example.yaml"


def document() -> dict[str, object]:
    loaded = yaml.safe_load(SEED_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def cases() -> list[dict[str, object]]:
    loaded = document()["cases"]
    assert isinstance(loaded, list)
    return [case for case in loaded if isinstance(case, dict)]


def string_values(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for nested in value.values():
            yield from string_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from string_values(nested)
    elif isinstance(value, str):
        yield value


class Seed(unittest.TestCase):
    """The committed citation seed is the detection contract's fixture."""

    def test_family_version_and_both_kinds(self) -> None:
        loaded = document()
        self.assertEqual(loaded["family"], "citation")
        self.assertEqual(loaded["pattern_set_version"], 1)
        kinds = {case["kind"] for case in cases()}
        self.assertEqual(kinds, {"shaped", "free"})
        shaped = [case for case in cases() if case["kind"] == "shaped"]
        free = [case for case in cases() if case["kind"] == "free"]
        self.assertEqual(len(shaped), len(free))
        self.assertEqual(len({case["id"] for case in cases()}), len(cases()))
        for case in shaped:
            self.assertIn(
                case["pattern"], {pattern.pattern_id for pattern in stamp.PATTERNS}
            )
        for case in free:
            self.assertNotIn("pattern", case)

    def test_every_family_has_two_shaped_cases(self) -> None:
        shaped = [case for case in cases() if case["kind"] == "shaped"]
        for pattern in stamp.PATTERNS:
            with self.subTest(family=pattern.pattern_id):
                self.assertGreaterEqual(
                    sum(case.get("pattern") == pattern.pattern_id for case in shaped),
                    2,
                )

    def test_every_seed_answer_reports_or_declines_its_shape(self) -> None:
        for case in cases():
            answer = case["answer"]
            assert isinstance(answer, str)
            with self.subTest(case=case["id"]):
                if case["kind"] == "shaped":
                    self.assertEqual(stamp.detect(answer), case["pattern"])
                else:
                    self.assertIsNone(stamp.detect(answer))


class Detection(unittest.TestCase):
    """The bounded families cover their intended shapes and decline near misses."""

    def test_shapes_the_families_cover(self) -> None:
        for text, family in (
            ("466 U.S. 668", stamp.REPORTER_FAMILY_ID),
            ("140 S.Ct. 1204", stamp.REPORTER_FAMILY_ID),
            ("--- F.4th ---", stamp.REPORTER_FAMILY_ID),
            ("91 Fed. Reg. 2048", stamp.REPORTER_FAMILY_ID),
            ("18 U.S.C. § 3553(a)", stamp.CODE_FAMILY_ID),
            ("U.S.S.G. §2D1.1(c)(5)", stamp.CODE_FAMILY_ID),
            ("18 U.S.C. 3553", stamp.CODE_FAMILY_ID),
            ("28 C.F.R. § 2.20", stamp.CODE_FAMILY_ID),
            ("Tenn. Code Ann. § 40-35-501", stamp.CODE_FAMILY_ID),
            ("§§ 3553-3554", stamp.CODE_FAMILY_ID),
            ("§ 2255", stamp.CODE_FAMILY_ID),
            ("Fed. R. Crim. P.", stamp.RULE_FAMILY_ID),
            ("Fed. R. Crim. P. 32.1(b)", stamp.RULE_FAMILY_ID),
            ("Fed. R. Evid. 404(b)", stamp.RULE_FAMILY_ID),
            ("2024 WL 1234567", stamp.DATABASE_FAMILY_ID),
            ("2024 U.S. App. LEXIS 9876", stamp.DATABASE_FAMILY_ID),
        ):
            with self.subTest(text=text):
                self.assertEqual(stamp.detect(text), family)

    def test_shapes_outside_the_families(self) -> None:
        for text in (
            "Strickland v. Washington",
            "Section 3553(a) of Title 18",
            "section 2255 motion",
            "Rule 11",
            "the 2024 report, page 12",
            "9 a.m. to 3 p.m.",
            "version 2.3.1",
            "Form 1040, line 12",
            "$3,553 over 30 days",
            "United States v. Booker (2005)",
        ):
            with self.subTest(text=text):
                self.assertIsNone(stamp.detect(text))


class BoundsAndHygiene(unittest.TestCase):
    """The module stays deterministic, bounded, standard-library-only, and site-free."""

    def test_patterns_are_bounded_and_match_within_ceiling(self) -> None:
        adversarial = (
            "x" * (stamp.MAX_MATCH_CHARS * 2),
            "999999 U.S. ____________",
            "99999   U.S.C.A.   §§   999999A999.999999.999999.999999  (test)  (part)  (more)  (last)  (abcd)  (1234)",
            "99999 Tenn. Code Ann. §§ 999999A999-999999-999999-999999 (aaaa)(bbbb)(cccc)(dddd)(eeee)(ffff)",
            "Fed. R. Bankr. P. 999.99",
            "9999 U.S. Dist. LEXIS 999999999999",
        )
        for pattern in stamp.PATTERNS:
            with self.subTest(family=pattern.pattern_id):
                self.assertNotRegex(pattern.regex.pattern, r"(?:\*|\+|\{\d+,\})")
                for text in adversarial:
                    for match in pattern.regex.finditer(text):
                        self.assertLessEqual(len(match.group(0)), stamp.MAX_MATCH_CHARS)

    def test_module_imports_are_standard_library_only(self) -> None:
        tree = ast.parse(
            STAMP_PATH.read_text(encoding="utf-8"), filename=str(STAMP_PATH)
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(
                        alias.name.split(".")[0],
                        sys.stdlib_module_names,
                        f"stamp.py imports non-standard module {alias.name}; the turn harness runs it outside the service image",
                    )
            if isinstance(node, ast.ImportFrom):
                assert node.module is not None
                self.assertIn(
                    node.module.split(".")[0],
                    sys.stdlib_module_names,
                    f"stamp.py imports non-standard module {node.module}; the turn harness runs it outside the service image",
                )

    def test_label_matches_adr_and_contains_no_site_value(self) -> None:
        self.assertEqual(stamp.CITATION_STAMP, "General does not verify citations.")
        site = yaml.safe_load(SITE_EXAMPLE.read_text(encoding="utf-8"))
        assert isinstance(site, dict)
        for value in string_values(site):
            with self.subTest(value=value):
                self.assertNotIn(value, stamp.CITATION_STAMP)


class StampDecision(unittest.TestCase):
    """The service owes one fixed tail, or none, on every detection outcome."""

    def test_tail_is_owed_to_a_shaped_answer_and_not_a_free_one(self) -> None:
        self.assertEqual(
            stamp.tail_for("An invented cite is 3 F.2d 8."), stamp.STAMP_TAIL
        )
        self.assertEqual(stamp.tail_for("An ordinary fictional answer."), "")

    def test_already_stamped_answers_are_owed_nothing_under_any_whitespace(
        self,
    ) -> None:
        answer = "An invented cite is 3 F.2d 8."
        for whitespace in (" ", "  ", "\n", "\t", "\r\n", " \n\t "):
            with self.subTest(whitespace=repr(whitespace)):
                self.assertEqual(
                    stamp.tail_for(answer + whitespace + stamp.CITATION_STAMP),
                    "",
                )

    def test_detector_failure_fails_toward_the_label(self) -> None:
        with patch.object(stamp, "detect", side_effect=RuntimeError("broken detector")):
            self.assertEqual(
                stamp.tail_for("An ordinary fictional answer."), stamp.STAMP_TAIL
            )

    def test_detector_failure_over_a_stamped_answer_adds_nothing(self) -> None:
        with patch.object(stamp, "detect", side_effect=RuntimeError("broken detector")):
            answer = "An invented cite is 3 F.2d 8." + stamp.STAMP_TAIL
            self.assertEqual(stamp.tail_for(answer), "")

    def test_first_read_failure_rechecks_stamped_answer_before_failing_closed(
        self,
    ) -> None:
        with patch.object(
            stamp,
            "is_stamped",
            side_effect=(RuntimeError("first read"), True),
        ):
            self.assertEqual(
                stamp.tail_for("An invented cite is 3 F.2d 8." + stamp.STAMP_TAIL),
                "",
            )

    def test_both_stamped_reads_raising_still_owes_the_tail(self) -> None:
        with patch.object(
            stamp,
            "is_stamped",
            side_effect=(RuntimeError("first read"), RuntimeError("second read")),
        ):
            self.assertEqual(
                stamp.tail_for("An ordinary fictional answer."), stamp.STAMP_TAIL
            )

    def test_stamp_tail_is_separator_plus_label(self) -> None:
        self.assertEqual(stamp.STAMP_TAIL, stamp.STAMP_SEPARATOR + stamp.CITATION_STAMP)
