"""The exact-object contract and the extraction package's import boundary."""

import ast
import sys
import time
import unittest
from pathlib import Path

from gideon.extraction import (
    GRAMMAR_VERSION,
    KEYED_TYPES,
    OBJECT_TYPES,
    ExactObject,
    extract,
    key_violations,
    keys_equal,
    ordering_violations,
    span_violations,
)
from gideon.extraction.grammar import PATTERN_BUILDERS, PATTERN_REGISTRY, _candidates

ROOT = Path(__file__).resolve().parent.parent
EXTRACTION = ROOT / "gideon" / "extraction"


class Contract(unittest.TestCase):
    """The exact-object invariants from spec §11.2."""

    def test_source_slices_and_order_are_preserved(self) -> None:
        source = "[FICTIONAL] 18 USC 3663A(b)(1), then Rule 41."
        statute_start = source.index("18 USC")
        statute_end = source.index(",")
        rule_start = source.index("Rule")
        objects = (
            ExactObject(
                "statute",
                statute_start,
                statute_end,
                source[statute_start:statute_end],
                "/us/usc/t18/s3663A",
                "usc/titled-section@1",
                ("b", "1"),
            ),
            ExactObject(
                "bare_rule",
                rule_start,
                rule_start + len("Rule 41"),
                "Rule 41",
                pattern_id="rules/bare-number@1",
            ),
        )
        self.assertEqual(span_violations(source, objects), ())
        wrong_text = ExactObject("bare_section", statute_start, statute_end, "[FICTIONAL]")
        self.assertEqual(span_violations(source, (wrong_text,)), (wrong_text,))
        self.assertEqual(key_violations(objects), ())
        self.assertEqual(ordering_violations(objects), ())

    def test_keyed_types_require_keys_and_unkeyed_types_never_carry_them(self) -> None:
        missing_key = ExactObject("guideline", 0, 8, "[FICTIONAL]")
        bare_key = ExactObject("bare_section", 0, 8, "[FICTIONAL]", "not-allowed")
        self.assertEqual(key_violations((missing_key, bare_key)), (missing_key, bare_key))
        valid = tuple(
            ExactObject(
                object_type,
                0,
                8,
                "[FICTIONAL]",
                "/fictitious/key" if object_type in KEYED_TYPES else None,
            )
            for object_type in OBJECT_TYPES
        )
        self.assertEqual(key_violations(valid), ())

    def test_ordering_check_reports_an_overlap_and_out_of_order_object(self) -> None:
        first = ExactObject("bare_section", 2, 8, "CTIONAL")
        overlap = ExactObject("bare_rule", 6, 12, "AL] 18")
        out_of_order = ExactObject("caption", 1, 2, "F")
        self.assertEqual(ordering_violations((first, overlap)), (overlap,))
        self.assertEqual(ordering_violations((first, out_of_order)), (out_of_order,))

    def test_key_comparison_folds_case_only(self) -> None:
        self.assertTrue(keys_equal("/us/usc/t18/s3663a", "/US/USC/T18/S3663A"))
        self.assertFalse(keys_equal("/us/usc/t18/s3663a", "/us/usc/t18/s3663b"))
        self.assertTrue(keys_equal(None, None))
        self.assertFalse(keys_equal(None, "ussg/2B1.1"))


class Imports(unittest.TestCase):
    """Every import under the package is the standard library or the package (map §9)."""

    def test_every_import_is_standard_library_or_gideon(self) -> None:
        standard_library = set(sys.stdlib_module_names)
        for path in sorted(EXTRACTION.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        continue
                    names = (node.module or "",)
                else:
                    continue
                for name in names:
                    self.assertTrue(
                        name == "gideon.extraction"
                        or name.startswith("gideon.extraction.")
                        or name.split(".", 1)[0] in standard_library,
                        f"{path}: non-standard import {name}",
                    )


def _signature(objects: tuple[ExactObject, ...]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (obj.type, obj.start, obj.end, obj.key, obj.subsections, obj.pattern_id)
        for obj in objects
    )


UNIT_TEXTS: tuple[tuple[str, str], ...] = (
    (
        "titled",
        "[FICTIONAL TEST ONLY] 18 U.S. Code § 2511(2)(c).",
    ),
    (
        "list",
        "[FICTIONAL TEST ONLY] 18 U.S.C. §§ 922(g) and 924(c).",
    ),
    (
        "bare-markerless",
        "[FICTIONAL TEST ONLY] under 2254 and 3663A apply.",
    ),
    (
        "bare-subsections",
        "[FICTIONAL TEST ONLY] 922(g)(1) remains fictional.",
    ),
    (
        "nbsp",
        "[FICTIONAL TEST ONLY] 18 U.S.C.\u00a0§\u00a01028A.",
    ),
    (
        "line-break",
        "[FICTIONAL TEST ONLY] 28 U.S.C.\n2241.",
    ),
    (
        "uscaa",
        "[FICTIONAL TEST ONLY] 21 U.S.C.A. § 841(b)(1)(A).",
    ),
    (
        "curly-apostrophe",
        "[FICTIONAL TEST ONLY] defendant’s 18 USC 1470.",
    ),
    (
        "mixed-case-designators",
        "[FICTIONAL TEST ONLY] 18 USC 3553(a)(2)(A).",
    ),
    (
        "guideline-lower-case",
        "[FICTIONAL TEST ONLY] ussg §2b1.1(b)(1).",
    ),
    (
        "guideline-shape-only",
        "[FICTIONAL TEST ONLY] 2B1.1 remains fictional.",
    ),
    (
        "court-rule-short",
        "[FICTIONAL TEST ONLY] Fed.R.Cr.P. 32.1(b).",
    ),
    (
        "court-rule-long-order",
        "[FICTIONAL TEST ONLY] Rule 4 of the Federal Rules of Criminal Procedure.",
    ),
    (
        "bare-rule",
        "[FICTIONAL TEST ONLY] Rule 11(c)(1)(C).",
    ),
)


class Grammar(unittest.TestCase):
    """The first bounded grammar families and their exact-object results."""

    def test_version_and_titled_section_shape(self) -> None:
        self.assertEqual(GRAMMAR_VERSION, 2)
        source = "[FICTIONAL TEST ONLY] 18 U.S. Code § 2511(2)(c)."
        start = source.index("18 U.S.")
        end = source.index(").", start) + 1
        self.assertEqual(
            _signature(extract(source)),
            (
                (
                    "statute",
                    start,
                    end,
                    "/us/usc/t18/s2511",
                    ("2", "c"),
                    "usc/titled-section@1",
                ),
            ),
        )

    def test_list_member_shape_and_title_key(self) -> None:
        source = "[FICTIONAL TEST ONLY] 18 U.S.C. §§ 922(g) and 924(c)."
        title_start = source.index("18 U.S.C.")
        first_end = source.index(" and", title_start)
        second_start = source.index("924", first_end)
        second_end = source.index(".", second_start)
        self.assertEqual(
            _signature(extract(source)),
            (
                (
                    "statute",
                    title_start,
                    first_end,
                    "/us/usc/t18/s922",
                    ("g",),
                    "usc/titled-list-member@1",
                ),
                (
                    "statute",
                    second_start,
                    second_end,
                    "/us/usc/t18/s924",
                    ("c",),
                    "usc/titled-list-member@1",
                ),
            ),
        )

    def test_guideline_shape_and_uppercase_key(self) -> None:
        source = "[FICTIONAL TEST ONLY] ussg §2b1.1(b)(1)."
        start = source.index("ussg")
        end = source.index(").", start) + 1
        self.assertEqual(
            _signature(extract(source)),
            (
                (
                    "guideline",
                    start,
                    end,
                    "ussg/2B1.1",
                    ("b", "1"),
                    "ussg/id@1",
                ),
            ),
        )
        shape_only = "[FICTIONAL TEST ONLY] 2B1.1 remains fictional."
        self.assertEqual(extract(shape_only)[0].type, "guideline")
        self.assertNotIn("bare_section", tuple(obj.type for obj in extract(shape_only)))

    def test_rule_sets_bare_rule_and_long_order_shape(self) -> None:
        source = "[FICTIONAL TEST ONLY] Rule 4 of the Federal Rules of Criminal Procedure."
        start = source.index("Rule")
        end = source.index(".", start)
        self.assertEqual(
            _signature(extract(source)),
            (
                (
                    "court_rule",
                    start,
                    end,
                    "/us/usc/t18a/courtRules/Crim/rule4",
                    (),
                    "rules/set-and-number@1",
                ),
            ),
        )
        bare = "[FICTIONAL TEST ONLY] Rule 11(c)(1)(C)."
        bare_start = bare.index("Rule")
        bare_end = bare.index(".", bare_start)
        self.assertEqual(
            _signature(extract(bare)),
            (
                (
                    "bare_rule",
                    bare_start,
                    bare_end,
                    None,
                    ("c", "1", "C"),
                    "rules/bare-number@1",
                ),
            ),
        )

    def test_closed_rule_short_forms_and_paths(self) -> None:
        cases = (
            ("Fed. R. Crim. P. 32.1", "/us/usc/t18a/courtRules/Crim/rule32.1"),
            ("Fed.R.Crim.P. 32.1", "/us/usc/t18a/courtRules/Crim/rule32.1"),
            ("FRCrP 32.1", "/us/usc/t18a/courtRules/Crim/rule32.1"),
            ("F.R.Cr.P. 32.1", "/us/usc/t18a/courtRules/Crim/rule32.1"),
            ("Fed. R. Evid. 404", "/us/usc/t28a/courtRules/Evid/rule404"),
            ("FRE 404", "/us/usc/t28a/courtRules/Evid/rule404"),
            ("Fed. R. App. P. 4", "/us/usc/t28a/courtRules/App/rule4"),
            ("FRAP 4", "/us/usc/t28a/courtRules/App/rule4"),
            ("Fed. R. Civ. P. 12", "/us/usc/t28a/courtRules/Civil/rule12"),
            ("FRCP 12", "/us/usc/t28a/courtRules/Civil/rule12"),
            ("F.R.Civ.P. 12", "/us/usc/t28a/courtRules/Civil/rule12"),
            ("F.R.App.P. 4", "/us/usc/t28a/courtRules/App/rule4"),
            ("F.R.E. 404", "/us/usc/t28a/courtRules/Evid/rule404"),
            ("Federal Rule of Civil Procedure 12", "/us/usc/t28a/courtRules/Civil/rule12"),
            ("Federal Rules of Evidence 404", "/us/usc/t28a/courtRules/Evid/rule404"),
        )
        for case_id, (citation, key) in enumerate(cases):
            source = f"[FICTIONAL TEST ONLY] {citation}."
            with self.subTest(case_id):
                objects = extract(source)
                self.assertEqual(len(objects), 1)
                self.assertEqual(objects[0].type, "court_rule")
                self.assertEqual(objects[0].key, key)
                self.assertEqual(objects[0].pattern_id, "rules/set-and-number@1")

    def test_supreme_court_rule_forms_and_designators(self) -> None:
        cases = (
            ("Sup. Ct. R. 13", "rules/scotus/rule13", ()),
            ("Sup.Ct.R. 13", "rules/scotus/rule13", ()),
            ("sUp. cT. rUlE 13", "rules/scotus/rule13", ()),
            ("Supreme Court Rule 13", "rules/scotus/rule13", ()),
            (
                "Rule 10 of the Rules of the Supreme Court",
                "rules/scotus/rule10",
                (),
            ),
            ("Sup. Ct. R. 14.1(a)", "rules/scotus/rule14", ("1", "a")),
            (
                "Rule 15.6 of the Rules of the Supreme Court",
                "rules/scotus/rule15",
                ("6",),
            ),
        )
        for citation, key, subsections in cases:
            source = f"[FICTIONAL TEST ONLY] {citation}."
            start = source.index(citation)
            with self.subTest(citation):
                self.assertEqual(
                    _signature(extract(source)),
                    (
                        (
                            "scotus_rule",
                            start,
                            start + len(citation),
                            key,
                            subsections,
                            "rules/scotus-number@1",
                        ),
                    ),
                )

    def test_federal_and_supreme_rules_keep_their_types(self) -> None:
        cases = (
            (
                "[FICTIONAL TEST ONLY] Fed. R. Crim. P. 32.1 and Sup. Ct. R. 13.",
                (
                    ("court_rule", "Fed. R. Crim. P. 32.1", "/us/usc/t18a/courtRules/Crim/rule32.1"),
                    ("scotus_rule", "Sup. Ct. R. 13", "rules/scotus/rule13"),
                ),
            ),
            (
                "[FICTIONAL TEST ONLY] Supreme Court Rule 13 and Rule 4 of the Federal Rules of Criminal Procedure.",
                (
                    ("scotus_rule", "Supreme Court Rule 13", "rules/scotus/rule13"),
                    ("court_rule", "Rule 4 of the Federal Rules of Criminal Procedure", "/us/usc/t18a/courtRules/Crim/rule4"),
                ),
            ),
        )
        for source, expected in cases:
            with self.subTest(source):
                self.assertEqual(
                    tuple((obj.type, obj.text, obj.key) for obj in extract(source)),
                    expected,
                )

    def test_rule_number_alone_stays_a_bare_rule(self) -> None:
        source = "[FICTIONAL TEST ONLY] Rule 13 alone."
        start = source.index("Rule")
        self.assertEqual(
            _signature(extract(source)),
            (
                (
                    "bare_rule",
                    start,
                    start + len("Rule 13"),
                    None,
                    (),
                    "rules/bare-number@1",
                ),
            ),
        )

    def test_habeas_rule_forms_keys_and_subsections(self) -> None:
        cases = (
            (
                "Rule 6 of the Rules Governing Section 2254 Cases",
                "rules/2254/rule6",
                (),
            ),
            (
                "Rule 12(a) of the Rules Governing § 2255 Proceedings",
                "rules/2255/rule12",
                ("a",),
            ),
            ("§ 2254 Rule 6", "rules/2254/rule6", ()),
            ("Section 2255 Rule 8(c)", "rules/2255/rule8", ("c",)),
            ("Section 2255 Rule 8.1(c)", "rules/2255/rule8.1", ("c",)),
        )
        for citation, key, subsections in cases:
            source = f"[FICTIONAL TEST ONLY] {citation}."
            start = source.index(citation)
            with self.subTest(citation):
                self.assertEqual(
                    _signature(extract(source)),
                    (
                        (
                            "habeas_rule",
                            start,
                            start + len(citation),
                            key,
                            subsections,
                            "rules/habeas-set-and-number@1",
                        ),
                    ),
                )

    def test_habeas_and_federal_rules_keep_their_types(self) -> None:
        cases = (
            (
                "[FICTIONAL TEST ONLY] Rule 6 of the Rules Governing Section 2254 Cases and Fed. R. Crim. P. 41(b).",
                ("habeas_rule", "Rule 6 of the Rules Governing Section 2254 Cases"),
                ("court_rule", "Fed. R. Crim. P. 41(b)"),
            ),
            (
                "[FICTIONAL TEST ONLY] Fed. R. Crim. P. 41(b) and Rule 6 of the Rules Governing Section 2254 Cases.",
                ("court_rule", "Fed. R. Crim. P. 41(b)"),
                ("habeas_rule", "Rule 6 of the Rules Governing Section 2254 Cases"),
            ),
        )
        for source, first, second in cases:
            with self.subTest(source):
                self.assertEqual(
                    tuple((obj.type, obj.text) for obj in extract(source)),
                    (first, second),
                )

    def test_unnamed_habeas_rule_is_a_bare_rule(self) -> None:
        source = "[FICTIONAL TEST ONLY] Habeas Rule 6(a)."
        start = source.index("Habeas")
        self.assertEqual(
            _signature(extract(source)),
            (
                (
                    "bare_rule",
                    start,
                    start + len("Habeas Rule 6(a)"),
                    None,
                    ("a",),
                    "rules/habeas-unnamed-set@1",
                ),
            ),
        )

    def test_cfr_sections_lists_ranges_and_part_cites(self) -> None:
        cases = (
            ("21 C.F.R. § 1308.11(d)", "cfr/21/1308.11", ("d",)),
            ("28 CFR 2.20", "cfr/28/2.20", ()),
            ("17 C.F.R. § 240.10b-5", "cfr/17/240.10b-5", ()),
        )
        for citation, key, subsections in cases:
            source = f"[FICTIONAL TEST ONLY] {citation}."
            start = source.index(citation)
            with self.subTest(citation):
                self.assertEqual(
                    _signature(extract(source)),
                    (
                        (
                            "regulation",
                            start,
                            start + len(citation),
                            key,
                            subsections,
                            "cfr/titled-section@1",
                        ),
                    ),
                )

        list_source = "[FICTIONAL TEST ONLY] 28 C.F.R. §§ 523.42(c) and 523.44(d)."
        list_start = list_source.index("28 C.F.R.")
        second_start = list_source.index("523.44")
        self.assertEqual(
            _signature(extract(list_source)),
            (
                (
                    "regulation",
                    list_start,
                    list_source.index(" and", list_start),
                    "cfr/28/523.42",
                    ("c",),
                    "cfr/titled-list-member@1",
                ),
                (
                    "regulation",
                    second_start,
                    second_start + len("523.44(d)"),
                    "cfr/28/523.44",
                    ("d",),
                    "cfr/titled-list-member@1",
                ),
            ),
        )

        range_source = "[FICTIONAL TEST ONLY] 28 C.F.R. §§ 523.40-523.44."
        range_start = range_source.index("28 C.F.R.")
        range_right = range_source.index("523.44")
        self.assertEqual(
            _signature(extract(range_source)),
            (
                (
                    "regulation",
                    range_start,
                    range_right - 1,
                    "cfr/28/523.40",
                    (),
                    "cfr/titled-list-member@1",
                ),
                (
                    "regulation",
                    range_right,
                    range_right + len("523.44"),
                    "cfr/28/523.44",
                    (),
                    "cfr/titled-list-member@1",
                ),
            ),
        )
        self.assertEqual(extract("[FICTIONAL TEST ONLY] 28 C.F.R. pt. 2."), ())

    def test_bare_dotted_sections_require_a_marker(self) -> None:
        cases = (
            ("§ 1308.11", ()),
            ("§ 571.61(a)(2)", ("a", "2")),
        )
        for citation, subsections in cases:
            source = f"[FICTIONAL TEST ONLY] {citation}."
            start = source.index(citation)
            with self.subTest(citation):
                self.assertEqual(
                    _signature(extract(source)),
                    (
                        (
                            "bare_section",
                            start,
                            start + len(citation),
                            None,
                            subsections,
                            "usc/bare-section@2",
                        ),
                    ),
                )
        self.assertEqual(extract("[FICTIONAL TEST ONLY] 1308.11(a)."), ())
        guideline = "[FICTIONAL TEST ONLY] § 2B1.1(b)(1)."
        self.assertEqual(extract(guideline)[0].type, "guideline")

    def test_appendix_forms_use_the_table_and_decline_unknown_compilations(self) -> None:
        cases = (
            ("18 U.S.C. app. 3 § 6", "/us/usc/t18a/pl/96/456/s6", ()),
            ("18 U.S.C. app. 2, § 9(1)", "/us/usc/t18a/pl/91/538/s9", ("1",)),
            ("18 U.S.C. App. III § 4", "/us/usc/t18a/pl/96/456/s4", ()),
            ("18 U.S.C. App. II, § 9(1)", "/us/usc/t18a/pl/91/538/s9", ("1",)),
        )
        for citation, key, subsections in cases:
            source = f"[FICTIONAL TEST ONLY] {citation}."
            start = source.index(citation)
            with self.subTest(citation):
                self.assertEqual(
                    _signature(extract(source)),
                    (
                        (
                            "appendix_statute",
                            start,
                            start + len(citation),
                            key,
                            subsections,
                            "usc/appendix-section@1",
                        ),
                    ),
                )

        for citation in (
            "5 U.S.C. App. 3 § 6(a)(4)",
            "5 U.S.C. App. III, § 6(a)(4)",
        ):
            with self.subTest(citation):
                self.assertEqual(extract(f"[FICTIONAL TEST ONLY] {citation}."), ())

    def test_docket_forms_markers_and_declines(self) -> None:
        district_cases = (
            "3:21-cr-00123",
            "Case No. 3:21-cv-12345-ABCD-EF-GHIJ-2",
        )
        for citation in district_cases:
            source = f"[FICTIONAL TEST ONLY] {citation}."
            number = citation if citation[0].isdigit() else citation[citation.index("3:") :]
            start = source.index(number)
            with self.subTest(citation):
                self.assertEqual(
                    _signature(extract(source)),
                    (
                        (
                            "docket",
                            start,
                            start + len(number),
                            None,
                            (),
                            "docket/district@1",
                        ),
                    ),
                )

        for marker in ("No.", "Case No.", "Docket No.", "Dkt."):
            source = f"[FICTIONAL TEST ONLY] {marker} 21-5123."
            start = source.index("21-5123")
            with self.subTest(marker):
                self.assertEqual(
                    _signature(extract(source)),
                    (
                        (
                            "docket",
                            start,
                            start + len("21-5123"),
                            None,
                            (),
                            "docket/marked-number@1",
                        ),
                    ),
                )

        for source in (
            "[FICTIONAL TEST ONLY] Pub. L. No. 115-391.",
            "[FICTIONAL TEST ONLY] 21-5123.",
            "[FICTIONAL TEST ONLY] 39-17-417.",
        ):
            with self.subTest(source):
                self.assertEqual(extract(source), ())

    def test_bare_sections_have_subsections_or_cues_and_no_keys(self) -> None:
        source = "[FICTIONAL TEST ONLY] under 2254 and 3663A apply."
        first_start = source.index("2254")
        second_start = source.index("3663A")
        self.assertEqual(
            _signature(extract(source)),
            (
                (
                    "bare_section",
                    first_start,
                    first_start + 4,
                    None,
                    (),
                    "usc/bare-section@2",
                ),
                (
                    "bare_section",
                    second_start,
                    second_start + 5,
                    None,
                    (),
                    "usc/bare-section@2",
                ),
            ),
        )
        for _, unit_text in UNIT_TEXTS:
            for obj in extract(unit_text):
                if obj.type in {"bare_section", "bare_rule"}:
                    self.assertIsNone(obj.key)

    def test_hyphen_rule_four_corners(self) -> None:
        cases = (
            (
                "plural-range",
                "[FICTIONAL TEST ONLY] 18 U.S.C. §§ 3553-3554.",
                2,
                ("/us/usc/t18/s3553", "/us/usc/t18/s3554"),
            ),
            (
                "singular-hyphen",
                "[FICTIONAL TEST ONLY] 50 U.S.C. § 403-1.",
                1,
                ("/us/usc/t50/s403-1",),
            ),
            (
                "letter-hyphen",
                "[FICTIONAL TEST ONLY] 18 U.S.C. § 2000e-5.",
                1,
                ("/us/usc/t18/s2000e-5",),
            ),
            (
                "en-dash-range",
                "[FICTIONAL TEST ONLY] 18 U.S.C. §§ 3143–3145.",
                2,
                ("/us/usc/t18/s3143", "/us/usc/t18/s3145"),
            ),
            (
                "bare-plural-range",
                "[FICTIONAL TEST ONLY] §§ 2254-2255.",
                2,
                (None, None),
            ),
        )
        for case_id, source, count, keys in cases:
            with self.subTest(case_id):
                objects = extract(source)
                self.assertEqual(len(objects), count)
                self.assertEqual(tuple(obj.key for obj in objects), keys)
                self.assertEqual(
                    tuple(obj.text for obj in objects),
                    tuple(source[obj.start : obj.end] for obj in objects),
                )
                if count == 2:
                    self.assertNotIn("-", objects[0].text)
                    self.assertNotIn("–", objects[0].text)
                    self.assertNotIn("-", objects[1].text)
                    self.assertNotIn("–", objects[1].text)

    def test_spacing_code_forms_and_boundaries(self) -> None:
        for case_id, source in UNIT_TEXTS:
            with self.subTest(case_id):
                objects = extract(source)
                self.assertTrue(objects)
                self.assertEqual(tuple(obj.text for obj in objects), tuple(source[obj.start : obj.end] for obj in objects))
        start_text = "18 USC 3663A [FICTIONAL TEST ONLY]"
        end_text = "[FICTIONAL TEST ONLY] 18 USC 3663A"
        self.assertEqual(extract(start_text)[0].start, 0)
        self.assertEqual(extract(end_text)[0].end, len(end_text))

    def test_declines_and_no_object_text(self) -> None:
        cases = (
            "[FICTIONAL TEST ONLY] Tenn. Code Ann. § 39-17-417.",
            "[FICTIONAL TEST ONLY] 2024-2025 report, 30 days, and $95,000.",
        )
        for case_id, source in enumerate(cases):
            with self.subTest(case_id):
                self.assertEqual(extract(source), ())
        self.assertEqual(extract(""), ())

    def test_supreme_rule_declines_become_objects(self) -> None:
        cases = (
            (
                "[FICTIONAL TEST ONLY] Supreme Court Rule 13.",
                "Supreme Court Rule 13",
            ),
            ("[FICTIONAL TEST ONLY] Sup. Ct. R. 13.", "Sup. Ct. R. 13"),
        )
        for source, citation in cases:
            start = source.index(citation)
            with self.subTest(source):
                self.assertEqual(
                    _signature(extract(source)),
                    (
                        (
                            "scotus_rule",
                            start,
                            start + len(citation),
                            "rules/scotus/rule13",
                            (),
                            "rules/scotus-number@1",
                        ),
                    ),
                )

    def test_resolution_is_deterministic_and_contract_valid(self) -> None:
        for case_id, source in UNIT_TEXTS:
            with self.subTest(case_id):
                objects = extract(source)
                self.assertEqual(objects, extract(source))
                self.assertEqual(span_violations(source, objects), ())
                self.assertEqual(key_violations(objects), ())
                self.assertEqual(ordering_violations(objects), ())

    def test_pattern_ids_are_unique_versioned_and_bounded(self) -> None:
        ids = tuple(pattern.id for pattern in PATTERN_REGISTRY)
        self.assertEqual(len(ids), len(set(ids)))
        for pattern_id in ids:
            self.assertRegex(pattern_id, r"^[a-z0-9-]+/[a-z0-9-]+@[1-9][0-9]*$")
        for pattern in PATTERN_REGISTRY:
            self.assertNotRegex(pattern.expression.pattern, r"(?:\*|\+|\{[0-9]+,\})")

    def test_bounded_patterns_handle_long_adversarial_input(self) -> None:
        adversarial = "[FICTIONAL TEST ONLY] " + ("18 U.S.C. " * 1500) + "§§ 3553-3554"
        started = time.perf_counter()
        for pattern in PATTERN_REGISTRY:
            tuple(pattern.expression.finditer(adversarial))
        self.assertLess(time.perf_counter() - started, 1.0)

    def test_markerless_cue_residuals(self) -> None:
        self.assertEqual(extract("[FICTIONAL TEST ONLY] sentenced under 2019 rules."), ())
        self.assertEqual(extract("[FICTIONAL TEST ONLY] defendants under 21 years old."), ())
        source = "[FICTIONAL TEST ONLY] a 2254 court and Supreme Court Rule 13."
        self.assertEqual(
            _signature(extract(source)),
            (
                (
                    "bare_section",
                    source.index("2254"),
                    source.index("2254") + len("2254"),
                    None,
                    (),
                    "usc/bare-section@2",
                ),
                (
                    "scotus_rule",
                    source.index("Supreme Court Rule 13"),
                    source.index("Supreme Court Rule 13") + len("Supreme Court Rule 13"),
                    "rules/scotus/rule13",
                    (),
                    "rules/scotus-number@1",
                ),
            ),
        )

    def test_registry_builders_and_candidate_precedence_follow_registry(self) -> None:
        positions = {pattern.id: position for position, pattern in enumerate(PATTERN_REGISTRY)}
        self.assertEqual(set(positions), set(PATTERN_BUILDERS))
        sources = tuple(text for _, text in UNIT_TEXTS) + (
            "[FICTIONAL TEST ONLY] Sup. Ct. R. 13.",
        )
        for source in sources:
            for candidate in _candidates(source):
                pattern_id = candidate.objects[0].pattern_id
                if not isinstance(pattern_id, str):
                    self.fail("candidate object must carry its pattern id")
                self.assertEqual(candidate.precedence, positions[pattern_id])

    def test_the_habeas_rules_both_names_emit_objects(self) -> None:
        for name in ("Section 2254 Cases", "Section 2255 Proceedings", "§ 2255 Proceedings"):
            source = f"[FICTIONAL TEST ONLY] Rule 6 of the Rules Governing {name}."
            start = source.index("Rule")
            end = source.index(".", start)
            title = "2254" if "2254" in name else "2255"
            with self.subTest(name):
                self.assertEqual(
                    _signature(extract(source)),
                    (
                        (
                            "habeas_rule",
                            start,
                            end,
                            f"rules/{title}/rule6",
                            (),
                            "rules/habeas-set-and-number@1",
                        ),
                    ),
                )

    def test_review_round_one_spans(self) -> None:
        cases = (
            (
                "[FICTIONAL TEST ONLY] Guideline 2B1.1(b)(1) and guideline § 3E1.1.",
                (("guideline", "Guideline 2B1.1(b)(1)", "ussg/2B1.1"), ("guideline", "guideline § 3E1.1", "ussg/3E1.1")),
            ),
            (
                "[FICTIONAL TEST ONLY] 18 U.S.C. §§ 922(g), § 924(c).",
                (("statute", "18 U.S.C. §§ 922(g)", "/us/usc/t18/s922"), ("statute", "§ 924(c)", "/us/usc/t18/s924")),
            ),
            (
                "[FICTIONAL TEST ONLY] 18 U.S.C. §§ 922(g), 924(c), and 924(j).",
                (
                    ("statute", "18 U.S.C. §§ 922(g)", "/us/usc/t18/s922"),
                    ("statute", "924(c)", "/us/usc/t18/s924"),
                    ("statute", "924(j)", "/us/usc/t18/s924"),
                ),
            ),
        )
        for source, expected in cases:
            with self.subTest(expected[0][1]):
                self.assertEqual(tuple((obj.type, obj.text, obj.key) for obj in extract(source)), expected)
