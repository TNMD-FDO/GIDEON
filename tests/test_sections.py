"""Section spans and types over fictitious opinion text."""

import unittest

from gideon.worker import opiniontext, sections


class Segment(unittest.TestCase):
    def test_public_paragraphs_keep_the_blank_line_rule_within_bounds(self) -> None:
        text = "  Intro.\n\n  Body.\n\nTail."
        first = sections.paragraphs(text, 0, text.index("Tail."))
        self.assertEqual([(row.start, row.end, row.line) for row in first], [
            (2, text.index("\n\n"), "Intro."),
            (text.index("Body."), text.index("\n\nTail."), "Body."),
        ])
        self.assertEqual(sections.paragraphs(text, text.index("Tail."), len(text))[0].line, "Tail.")

    def parsed_sections(
        self, column: str, raw: str, *, opinion_type: str = "010combined",
        per_curiam: bool = False,
    ) -> tuple[opiniontext.Parsed, tuple[sections.Section, ...]]:
        parsed = opiniontext.walk(column, raw)
        result = sections.segment(
            parsed, opinion_type=opinion_type, per_curiam=per_curiam, column=column,
        )
        self.assertTrue(result)
        self.assertEqual(result[0].char_start, 0)
        self.assertEqual(result[-1].char_end, len(parsed.text))
        self.assertEqual([section.char_start for section in result],
                         sorted(section.char_start for section in result))
        for index, section in enumerate(result):
            self.assertLess(section.char_start, section.char_end)
            if index:
                self.assertEqual(result[index - 1].char_end, section.char_start)
        return parsed, result

    def kinds(self, result: tuple[sections.Section, ...]) -> list[tuple[str, str]]:
        return [(section.section_type, section.typed_by) for section in result]

    def test_vocabulary(self) -> None:
        self.assertEqual(sections.SECTION_TYPES, (
            "syllabus", "headmatter", "majority", "plurality", "per_curiam",
            "concurrence", "dissent", "concurrence_dissent", "footnote", "appendix",
            "order", "unknown",
        ))
        self.assertEqual(sections.TYPED_BY, ("row", "flag", "element", "line", "markup", "none"))

    def test_row_types_and_unmapped_codes(self) -> None:
        cases = (
            ("015unamimous", "majority"), ("020lead", "majority"),
            ("025plurality", "plurality"), ("030concurrence", "concurrence"),
            ("035concurrenceinpart", "concurrence_dissent"),
            ("040dissent", "dissent"),
        )
        self.assertEqual(tuple(sections.ROW_TYPES.items()), cases)
        for code, expected in cases:
            with self.subTest(code=code):
                _, result = self.parsed_sections("plain_text", "Fictional reasoning.", opinion_type=code)
                self.assertEqual(self.kinds(result), [(expected, "row")])
        for code in ("010combined", "100trialcourt", "050addendum", "unexpected"):
            with self.subTest(code=code):
                _, result = self.parsed_sections("plain_text", "Fictional reasoning.", opinion_type=code)
                self.assertEqual(self.kinds(result), [("unknown", "none")])

    def test_flag_element_and_disagreement(self) -> None:
        _, flagged = self.parsed_sections("plain_text", "Fictional reasoning.", per_curiam=True)
        self.assertEqual(self.kinds(flagged), [("per_curiam", "flag")])
        element_cases = (
            ("majority", "majority"), ("unanimous", "majority"),
            ("plurality", "plurality"), ("concurrence", "concurrence"),
            ("concurring-in-part-and-dissenting-in-part", "concurrence_dissent"),
            ("dissent", "dissent"),
        )
        self.assertEqual(tuple(sections.ELEMENT_TYPES.items()), element_cases)
        for label, expected in element_cases:
            with self.subTest(label=label):
                _, result = self.parsed_sections(
                    "xml_harvard", f'<opinion type="{label}"><p>Fictional reasoning.</p></opinion>',
                )
                self.assertEqual(self.kinds(result), [(expected, "element")])
        _, unmapped = self.parsed_sections(
            "xml_harvard", '<opinion type="remittitur"><p>Fictional reasoning.</p></opinion>',
        )
        self.assertEqual(self.kinds(unmapped), [("unknown", "none")])
        _, conflict = self.parsed_sections(
            "xml_harvard", '<opinion type="majority"><p>Fictional reasoning.</p></opinion>',
            opinion_type="040dissent",
        )
        self.assertEqual(self.kinds(conflict), [("unknown", "none")])
        _, agreed = self.parsed_sections(
            "xml_harvard", '<opinion type="unanimous"><p>Fictional reasoning.</p></opinion>',
            opinion_type="020lead",
        )
        self.assertEqual(self.kinds(agreed), [("majority", "row")])
        _, row_over_flag = self.parsed_sections(
            "plain_text", "Fictional reasoning.", opinion_type="040dissent", per_curiam=True,
        )
        self.assertEqual(self.kinds(row_over_flag), [("dissent", "row")])
        _, flag_over_element = self.parsed_sections(
            "xml_harvard", '<opinion type="dissent"><p>Fictional reasoning.</p></opinion>',
            per_curiam=True,
        )
        self.assertEqual(self.kinds(flag_over_element), [("per_curiam", "flag")])
        _, conflict_over_flag = self.parsed_sections(
            "xml_harvard", '<opinion type="majority"><p>Fictional reasoning.</p></opinion>',
            opinion_type="040dissent", per_curiam=True,
        )
        self.assertEqual(self.kinds(conflict_over_flag), [("unknown", "none")])

    def test_author_line_shapes_and_disposition_precedence(self) -> None:
        cases = (
            ("Example, C. J.", "unknown", "none"),
            ("Sample, J., dissenting.", "dissent", "line"),
            ("Example, J., with whom Sample, J., joins, concurring in part and dissenting in part.",
             "concurrence_dissent", "line"),
            ("SAMPLE, Chief Judge.", "unknown", "none"),
            ("EXAMPLE, Circuit Judge, dissenting.", "dissent", "line"),
            ("FICTION, J., concurring in the judgment.", "concurrence", "line"),
            ("JUSTICE FICTION delivered the opinion of the Court.", "majority", "line"),
            ("Mr. Justice Example delivered the opinion of the Court.", "majority", "line"),
            ("JUSTICE FICTION, concurring.", "concurrence", "line"),
            ("CHIEF JUSTICE SAMPLE announced the judgment of the Court and delivered the "
             "opinion of the Court with respect to Parts I and II", "majority", "line"),
            ("CHIEF JUSTICE SAMPLE announced the judgment of the Court and delivered an "
             "opinion.", "plurality", "line"),
            ("PER CURIAM:", "per_curiam", "line"),
            ("EXAMPLE, J., dissenting in part.", "concurrence_dissent", "line"),
            ("EXAMPLE, J., concurring in part and dissenting in part, per curiam.",
             "per_curiam", "line"),
            ("MR. JUSTICE EXAMPLE, whom MR. JUSTICE SAMPLE joins, dissenting.", "dissent", "line"),
            ("MR. JUSTICE EXAMPLE, with whom THE CHIEF JUSTICE concurred, dissenting.",
             "dissent", "line"),
            ("MR. JUSTICE EXAMPLE, after making the foregoing statement, delivered the opinion "
             "of the court.", "majority", "line"),
            ("JUSTICE EXAMPLE, with whom JUSTICE SAMPLE concurred, concurring in the judgment.",
             "concurrence", "line"),
            ("EXAMPLE, J., dissenting in part, concurring in part.", "concurrence_dissent", "line"),
        )
        for line, kind, typed_by in cases:
            for column in ("plain_text", "html"):
                with self.subTest(line=line, column=column):
                    raw = line if column == "plain_text" else f"<p>{line}</p>"
                    _, result = self.parsed_sections(column, raw)
                    self.assertEqual(self.kinds(result), [(kind, typed_by)])

    def test_byline_prefixes_type_only_the_prefix(self) -> None:
        cases = (
            ("EXAMPLE, Circuit Judge. The first fictional sentence mentions a dissent.",
             "unknown", "none"),
            ("PER CURIAM. The first fictional sentence mentions a dissent.", "per_curiam", "line"),
            ("PER CURIAM: The first fictional sentence mentions a dissent.", "per_curiam", "line"),
            ("PER CURIAM; The first fictional sentence mentions a dissent.", "per_curiam", "line"),
            ("PER CURIAM:The first fictional sentence mentions a dissent.", "per_curiam", "line"),
            ("EXAMPLE, Circuit Judge, dissenting. The first fictional sentence concurs.",
             "dissent", "line"),
            ("JUSTICE FICTION delivered the opinion of the Court. The dissent disagrees.",
             "majority", "line"),
            ("JUSTICE FICTION delivered the opinion of the Court with respect to Parts I and II "
             "and an opinion with respect to Part III. The dissent disagrees.", "majority", "line"),
            ("CHIEF JUSTICE SAMPLE announced the judgment of the Court. The dissent disagrees.",
             "plurality", "line"),
            ("EXAMPLE, Circuit Judge, for the court. The dissent disagrees.", "majority", "line"),
            ("JUSTICE FICTION delivered the opinion of the Court. "
             "EXAMPLE, J., wrote fictional reasoning. The dissent disagrees.", "majority", "line"),
        )
        for line, kind, typed_by in cases:
            for column in ("plain_text", "html"):
                with self.subTest(line=line, column=column):
                    raw = (f"Opening text.\n\n{line}\n\nClosing text." if column == "plain_text"
                           else f"<p>Opening text.</p><p>{line}</p><p>Closing text.</p>")
                    parsed, result = self.parsed_sections(column, raw)
                    self.assertEqual(self.kinds(result), [("unknown", "none"), (kind, typed_by)])
                    self.assertEqual(result[1].char_start, parsed.text.index(line))

    def test_bare_byline_prefix_continues_the_running_type(self) -> None:
        line = "EXAMPLE, Circuit Judge. The first fictional sentence mentions a dissent."
        _, result = self.parsed_sections(
            "plain_text", f"CONCURRENCE\n\nFictional reasoning.\n\n{line}",
        )
        self.assertEqual(self.kinds(result), [("concurrence", "line"), ("concurrence", "line")])

    def test_a_bare_byline_compares_the_whole_opening_judge_name(self) -> None:
        for first, bare, expected in (
            ("JUSTICE EXAMPLE delivered the opinion of the Court.",
             "JUSTICE SAMPLE:", ("unknown", "none")),
            ("JUSTICE EXAMPLE delivered the opinion of the Court.",
             "JUSTICE EXAMPLE:", ("majority", "line")),
            ("Mr. Justice EXAMPLE delivered the opinion of the Court.",
             "EXAMPLE, J.", ("majority", "line")),
            ("EXAMPLE, Circuit Judge, delivered the opinion of the court.",
             "R. GUY EXAMPLE, Circuit Judge.", ("unknown", "none")),
        ):
            for column in ("plain_text", "html"):
                with self.subTest(first=first, bare=bare, column=column):
                    raw = (f"{first}\n\nFictional court reasoning.\n\n{bare}\n\n"
                           "More fictional reasoning." if column == "plain_text" else
                           f"<p>{first}</p><p>Fictional court reasoning.</p><p>{bare}</p>"
                           "<p>More fictional reasoning.</p>")
                    parsed, result = self.parsed_sections(column, raw)
                    self.assertEqual(self.kinds(result), [("majority", "line"), expected])
                    self.assertEqual(result[1].char_start, parsed.text.index(bare))

    def test_a_disposition_after_a_different_judges_bare_line_opens_dissent(self) -> None:
        first = "JUSTICE EXAMPLE delivered the opinion of the Court."
        bare = "JUSTICE SAMPLE:"
        dissent = "JUSTICE FICTION, dissenting."
        for column in ("plain_text", "html"):
            with self.subTest(column=column):
                raw = (f"{first}\n\nFictional court reasoning.\n\n{bare}\n\n"
                       f"Fictional separate reasoning.\n\n{dissent}\n\nFictional dissent."
                       if column == "plain_text" else
                       f"<p>{first}</p><p>Fictional court reasoning.</p><p>{bare}</p>"
                       f"<p>Fictional separate reasoning.</p><p>{dissent}</p><p>Fictional dissent.</p>")
                parsed, result = self.parsed_sections(column, raw)
                self.assertEqual(self.kinds(result), [
                    ("majority", "line"), ("unknown", "none"), ("dissent", "line"),
                ])
                self.assertEqual([section.char_start for section in result], [
                    0, parsed.text.index(bare), parsed.text.index(dissent),
                ])

    def test_a_different_judges_bare_line_spends_the_panel_arming(self) -> None:
        first = "Mr. Justice EXAMPLE delivered the opinion of the Court:"
        armed = "Before we consider the instructions, the judge's charge is read whole."
        bares = ("Mr. Justice SAMPLE:", "Mr. Justice FICTION stated that he dissented from the "
                 "opinion of the Court, delivered by Mr. Justice Example.")
        for column in ("plain_text", "html"):
            with self.subTest(column=column):
                paragraphs = (first, armed, "Fictional court reasoning.", bares[0],
                              "Fictional concurrence.", bares[1], "Fictional dissent.")
                raw = ("\n\n".join(paragraphs) if column == "plain_text"
                       else "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs))
                parsed, result = self.parsed_sections(column, raw)
                self.assertEqual(self.kinds(result), [
                    ("majority", "line"), ("unknown", "none"), ("unknown", "none"),
                ])
                self.assertEqual([section.char_start for section in result], [
                    0, *(parsed.text.index(bare) for bare in bares),
                ])

    def test_a_different_judges_bare_line_continues_a_typed_or_per_curiam_opinion(self) -> None:
        bare = "JUSTICE SAMPLE:"
        for first, row_type, per_curiam, expected in (
            ("JUSTICE EXAMPLE delivered the opinion of the Court.",
             "020lead", False, ("majority", "row")),
            ("JUSTICE EXAMPLE delivered the opinion of the Court.",
             "010combined", True, ("per_curiam", "flag")),
            ("PER CURIAM.", "010combined", False, ("per_curiam", "line")),
        ):
            for column in ("plain_text", "html"):
                with self.subTest(first=first, row_type=row_type, per_curiam=per_curiam, column=column):
                    raw = (f"{first}\n\nFictional court reasoning.\n\n{bare}\n\n"
                           "More fictional reasoning." if column == "plain_text" else
                           f"<p>{first}</p><p>Fictional court reasoning.</p><p>{bare}</p>"
                           "<p>More fictional reasoning.</p>")
                    parsed, result = self.parsed_sections(
                        column, raw, opinion_type=row_type, per_curiam=per_curiam,
                    )
                    self.assertEqual(self.kinds(result), [expected, expected])
                    self.assertEqual(result[1].char_start, parsed.text.index(bare))

    def test_a_later_court_byline_changes_the_opening_judge(self) -> None:
        first = "JUSTICE EXAMPLE delivered the opinion of the Court."
        second = "JUSTICE SAMPLE delivered the opinion of the Court."
        bare = "SAMPLE, J."
        for column in ("plain_text", "html"):
            with self.subTest(column=column):
                raw = (f"{first}\n\nFictional court reasoning.\n\n{second}\n\n"
                       f"More fictional court reasoning.\n\n{bare}\n\nFictional continuation."
                       if column == "plain_text" else
                       f"<p>{first}</p><p>Fictional court reasoning.</p><p>{second}</p>"
                       f"<p>More fictional court reasoning.</p><p>{bare}</p><p>Fictional continuation.</p>")
                parsed, result = self.parsed_sections(column, raw)
                self.assertEqual(self.kinds(result), [
                    ("majority", "line"), ("majority", "line"), ("majority", "line"),
                ])
                self.assertEqual([section.char_start for section in result], [
                    0, parsed.text.index(second), parsed.text.index(bare),
                ])

    def test_a_bare_byline_after_a_dissent_heading_continues_dissent(self) -> None:
        first = "JUSTICE EXAMPLE delivered the opinion of the Court."
        bare = "JUSTICE SAMPLE:"
        for column in ("plain_text", "html"):
            with self.subTest(column=column):
                raw = (f"{first}\n\nFictional court reasoning.\n\nDISSENT\n\n{bare}\n\n"
                       "Fictional dissent." if column == "plain_text" else
                       f"<p>{first}</p><p>Fictional court reasoning.</p><h2>DISSENT</h2>"
                       f"<p>{bare}</p><p>Fictional dissent.</p>")
                parsed, result = self.parsed_sections(column, raw)
                self.assertEqual(self.kinds(result), [
                    ("majority", "line"), ("dissent", "line"), ("dissent", "line"),
                ])
                self.assertEqual([section.char_start for section in result], [
                    0, parsed.text.index("DISSENT"), parsed.text.index(bare),
                ])

    def test_circuit_footnote_resumption_keeps_the_opening_judge(self) -> None:
        first = "JUSTICE EXAMPLE delivered the opinion of the Court."
        bare = "JUSTICE SAMPLE:"
        resumed = "Fictional court reasoning on the second page."
        header = "No. 00-0000 Example v. Sample Page"
        raw = "\n\n".join((
            f"{header} 1", first, "Fictional court reasoning.", "1", "A fictional footnote.",
            f"{header} 2", resumed, bare, "Fictional separate reasoning.",
        ))
        parsed, result = self.parsed_sections("plain_text", raw)
        self.assertEqual(self.kinds(result), [
            ("unknown", "none"), ("majority", "line"), ("footnote", "line"),
            ("majority", "line"), ("unknown", "none"),
        ])
        self.assertEqual(result[2].label, "1")
        self.assertEqual([section.char_start for section in result], [
            0, parsed.text.index(first), parsed.text.index("\n\n1\n\n") + 2,
            parsed.text.index(resumed), parsed.text.index(bare),
        ])

    def test_markup_footnote_resumption_keeps_the_opening_judge(self) -> None:
        first = "JUSTICE EXAMPLE delivered the opinion of the Court."
        bare = "JUSTICE SAMPLE:"
        resumed = "The fictional court reasoning resumes."
        raw = (
            f'<p>{first}</p><p>Fictional court reasoning.</p>'
            '<div class="footnote"><p>[1] A fictional footnote.</p></div>'
            f'<p>{resumed}</p><p>{bare}</p><p>Fictional separate reasoning.</p>'
        )
        parsed, result = self.parsed_sections("html", raw)
        self.assertEqual(self.kinds(result), [
            ("majority", "line"), ("footnote", "markup"),
            ("majority", "line"), ("unknown", "none"),
        ])
        self.assertEqual([section.char_start for section in result], [
            0, parsed.text.index("[1] A fictional footnote."),
            parsed.text.index(resumed), parsed.text.index(bare),
        ])

    def test_panel_line_arms_the_first_bare_byline(self) -> None:
        byline = "EXAMPLE, Circuit Judge. The first fictional sentence."
        raw = "\n\n".join((
            "Fictional opening text.",
            "Before: EXAMPLE, SAMPLE, and FICTION, Circuit Judges.",
            byline, "More fictional reasoning.",
        ))
        parsed, result = self.parsed_sections("plain_text", raw)
        self.assertEqual(self.kinds(result), [("unknown", "none"), ("majority", "line")])
        self.assertEqual(result[0].char_end, parsed.text.index(byline))
        self.assertEqual(result[1].char_start, parsed.text.index(byline))

    def test_later_bare_bylines_revert_and_do_not_reopen_the_court_opinion(self) -> None:
        bylines = (
            "EXAMPLE, Circuit Judge. The first fictional sentence.",
            "SAMPLE, Circuit Judge.", "FICTION, Circuit Judge.",
        )
        raw = "\n\n".join((
            "Fictional opening text.",
            "Before: EXAMPLE, SAMPLE, and FICTION, Circuit Judges.",
            bylines[0], "More fictional reasoning.", bylines[1],
            "A second fictional body.", bylines[2], "A third fictional body.",
        ))
        parsed, result = self.parsed_sections("plain_text", raw)
        self.assertEqual(self.kinds(result), [
            ("unknown", "none"), ("majority", "line"),
            ("unknown", "none"), ("unknown", "none"),
        ])
        self.assertEqual([section.char_start for section in result], [
            0, *(parsed.text.index(byline) for byline in bylines),
        ])

    def test_bare_byline_without_a_panel_line_stays_unknown(self) -> None:
        byline = "EXAMPLE, Circuit Judge. The first fictional sentence."
        parsed, result = self.parsed_sections(
            "plain_text", f"Fictional opening text.\n\n{byline}\n\nMore fictional reasoning.",
        )
        self.assertEqual(self.kinds(result), [("unknown", "none"), ("unknown", "none")])
        self.assertEqual(result[1].char_start, parsed.text.index(byline))

    def test_before_prose_does_not_arm_a_bare_byline(self) -> None:
        byline = "EXAMPLE, Circuit Judge. The first fictional sentence."
        raw = "\n\n".join((
            "Fictional opening text.", "Before the trial began, the parties met.",
            byline, "More fictional reasoning.",
        ))
        parsed, result = self.parsed_sections("plain_text", raw)
        self.assertEqual(self.kinds(result), [("unknown", "none"), ("unknown", "none")])
        self.assertEqual(result[1].char_start, parsed.text.index(byline))

    def test_circuit_footnote_resumption_preserves_the_bare_opening(self) -> None:
        first = "EXAMPLE, Circuit Judge. The first fictional sentence."
        second = "SAMPLE, Circuit Judge."
        resumed = "Fictional body on the second page."
        header = "No. 00-0000 Example v. Sample Page"
        raw = "\n\n".join((
            f"{header} 1", "Fictional opening text.",
            "Before: EXAMPLE, SAMPLE, and FICTION, Circuit Judges.",
            first, "More fictional reasoning.", "1", "A fictional footnote.",
            f"{header} 2", resumed, second, "A second fictional body.",
        ))
        parsed, result = self.parsed_sections("plain_text", raw)
        self.assertEqual(self.kinds(result), [
            ("unknown", "none"), ("majority", "line"), ("footnote", "line"),
            ("majority", "line"), ("unknown", "none"),
        ])
        self.assertEqual(result[2].label, "1")
        self.assertEqual([section.char_start for section in result], [
            0, parsed.text.index(first), parsed.text.index("\n\n1\n\n") + 2,
            parsed.text.index(resumed), parsed.text.index(second),
        ])

    def test_markup_footnote_preserves_the_bare_opening(self) -> None:
        first = "EXAMPLE, Circuit Judge. The first fictional sentence."
        second = "SAMPLE, Circuit Judge."
        resumed = "The fictional body resumes."
        raw = (
            '<p>Fictional opening text.</p>'
            '<p>Before: EXAMPLE, SAMPLE, and FICTION, Circuit Judges.</p>'
            f'<p>{first}</p><p>More fictional reasoning.</p>'
            '<div class="footnote"><p>[1] A fictional footnote.</p></div>'
            f'<p>{resumed}</p><p>{second}</p><p>A second fictional body.</p>'
        )
        parsed, result = self.parsed_sections("html", raw)
        self.assertEqual(self.kinds(result), [
            ("unknown", "none"), ("majority", "line"), ("footnote", "markup"),
            ("majority", "line"), ("unknown", "none"),
        ])
        self.assertEqual([section.char_start for section in result], [
            0, parsed.text.index(first), parsed.text.index("[1] A fictional footnote."),
            parsed.text.index(resumed), parsed.text.index(second),
        ])

    def test_opinion_heading_clears_the_bare_opening(self) -> None:
        first = "EXAMPLE, Circuit Judge. The first fictional sentence."
        second = "SAMPLE, Circuit Judge."
        raw = "\n\n".join((
            "Fictional opening text.",
            "Before: EXAMPLE, SAMPLE, and FICTION, Circuit Judges.",
            first, "More fictional reasoning.", "OPINION", second,
            "A second fictional body.",
        ))
        parsed, result = self.parsed_sections("plain_text", raw)
        self.assertEqual(self.kinds(result), [
            ("unknown", "none"), ("majority", "line"),
            ("majority", "line"), ("majority", "line"),
        ])
        self.assertEqual([section.char_start for section in result], [
            0, parsed.text.index(first), parsed.text.index("OPINION"), parsed.text.index(second),
        ])

    def test_armed_bare_bylines_continue_under_each_typed_base(self) -> None:
        panel = "Before: EXAMPLE, SAMPLE, and FICTION, Circuit Judges."
        byline = "EXAMPLE, Circuit Judge. The first fictional sentence."
        raw = f"{panel}\n\nFictional opening text.\n\n{byline}\n\nMore fictional reasoning."
        for row_type, base in sections.ROW_TYPES.items():
            with self.subTest(row_type=row_type):
                parsed, result = self.parsed_sections("plain_text", raw, opinion_type=row_type)
                self.assertEqual(self.kinds(result), [(base, "row"), (base, "row")])
                self.assertEqual(result[1].char_start, parsed.text.index(byline))
        parsed, flagged = self.parsed_sections("plain_text", raw, per_curiam=True)
        self.assertEqual(self.kinds(flagged), [("per_curiam", "flag"), ("per_curiam", "flag")])
        self.assertEqual(flagged[1].char_start, parsed.text.index(byline))
        for label, base in sections.ELEMENT_TYPES.items():
            with self.subTest(element=label):
                parsed, result = self.parsed_sections(
                    "xml_harvard", f'<opinion type="{label}"><p>{panel}</p>'
                    f'<p>Fictional opening text.</p><p>{byline}</p>'
                    '<p>More fictional reasoning.</p></opinion>',
                )
                self.assertEqual(self.kinds(result), [(base, "element"), (base, "element")])
                self.assertEqual(result[1].char_start, parsed.text.index(byline))

    def test_prelims_closing_byline_cues_the_body_after_the_block(self) -> None:
        for closing, footnote, expected in (
            ("EXAMPLE, Circuit Judge.", False, ("majority", "line")),
            ("EXAMPLE, Circuit Judge, dissenting.", False, ("dissent", "line")),
            ("Fictional introductory prose.", False, ("unknown", "none")),
            ("EXAMPLE, Circuit Judge.", True, ("majority", "line")),
        ):
            with self.subTest(closing=closing, footnote=footnote):
                raw = (
                    '<div class="prelims">'
                    '<p>Before: EXAMPLE, SAMPLE, and FICTION, Circuit Judges.</p>'
                    f'<p>{closing}</p></div>'
                )
                expected_kinds = [("headmatter", "markup")]
                if footnote:
                    raw += '<div class="footnote"><p>[1] A fictional footnote.</p></div>'
                    expected_kinds.append(("footnote", "markup"))
                raw += '<p>Fictional body reasoning.</p>'
                parsed, result = self.parsed_sections("html", raw)
                self.assertEqual(self.kinds(result), [*expected_kinds, expected])
                self.assertEqual(result[-1].char_start, parsed.text.index("Fictional body reasoning."))
                self.assertIn(closing, parsed.text[result[0].char_start:result[0].char_end])

    def test_prelims_closing_participation_line_cues_nothing(self) -> None:
        byline = "SAMPLE, J."
        raw = (
            '<div class="prelims">'
            '<p>Before: EXAMPLE, SAMPLE, and FICTION, Circuit Judges.</p>'
            '<p>SAMPLE, J., with whom EXAMPLE, J., joined.</p></div>'
            f'<p>{byline}</p><p>Fictional body reasoning.</p>'
        )
        parsed, result = self.parsed_sections("html", raw)
        self.assertEqual(self.kinds(result)[0], ("headmatter", "markup"))
        self.assertEqual(self.kinds(result)[-1], ("majority", "line"))
        self.assertEqual(result[-1].char_start, parsed.text.index(byline + "\n"))

    def test_a_head_matter_block_after_the_body_is_unknown(self) -> None:
        raw = (
            '<opinion type="majority"><headnotes><p>Fictional headnote.</p></headnotes>'
            '<author>EXAMPLE, J.</author><p>Fictional reasoning.</p>'
            "<headnotes><p>Fictional reporter's note.</p><p>SAMPLE, J., dissenting.</p></headnotes>"
            '<p>Affirmed.</p></opinion>'
        )
        parsed, result = self.parsed_sections("xml_harvard", raw)
        self.assertEqual(self.kinds(result), [
            ("headmatter", "markup"), ("majority", "element"),
            ("unknown", "none"), ("majority", "element"),
        ])
        self.assertEqual(result[1].char_start, parsed.text.index("EXAMPLE, J."))
        self.assertEqual(result[2].char_start, parsed.text.index("Fictional reporter's note."))
        self.assertEqual(result[3].char_start, parsed.text.index("Affirmed."))

    def test_cues_and_bare_bylines_under_each_base(self) -> None:
        for row_type, base, typed_by in (
            ("010combined", "unknown", "none"),
            ("020lead", "majority", "row"),
            ("025plurality", "plurality", "row"),
            ("030concurrence", "concurrence", "row"),
            ("035concurrenceinpart", "concurrence_dissent", "row"),
            ("040dissent", "dissent", "row"),
        ):
            with self.subTest(row_type=row_type):
                _, result = self.parsed_sections(
                    "plain_text", "Fictional text.\n\nEXAMPLE, J.\n\nMore text.",
                    opinion_type=row_type,
                )
                self.assertEqual(self.kinds(result), [(base, typed_by), (base, typed_by)])
        _, flagged = self.parsed_sections(
            "plain_text", "Fictional text.\n\nEXAMPLE, J.\n\nMore text.", per_curiam=True,
        )
        self.assertEqual(self.kinds(flagged), [("per_curiam", "flag"), ("per_curiam", "flag")])
        _, separate = self.parsed_sections(
            "plain_text", "Fictional text.\n\nEXAMPLE, J., dissenting.\n\nMore text.",
            opinion_type="020lead",
        )
        self.assertEqual(self.kinds(separate), [("majority", "row"), ("dissent", "line")])
        for row_type, expected in (
            ("010combined", ("majority", "line")),
            ("020lead", ("majority", "row")),
            ("025plurality", ("plurality", "row")),
            ("040dissent", ("unknown", "none")),
        ):
            with self.subTest(row_type=row_type):
                _, result = self.parsed_sections(
                    "plain_text", "Fictional text.\n\nOPINION\n\nMore text.",
                    opinion_type=row_type,
                )
                self.assertEqual(self.kinds(result)[-1], expected)
        _, flagged_heading = self.parsed_sections(
            "plain_text", "Fictional text.\n\nOPINION\n\nMore text.", per_curiam=True,
        )
        self.assertEqual(self.kinds(flagged_heading)[-1], ("per_curiam", "flag"))

    def test_qualified_opinion_headings_against_running_bases(self) -> None:
        for heading in ("AMENDED OPINION", "SUPPLEMENTAL OPINION", "CORRECTED OPINION"):
            for row_type, base, expected in (
                ("010combined", ("unknown", "none"), ("majority", "line")),
                ("040dissent", ("dissent", "row"), ("unknown", "none")),
                ("020lead", ("majority", "row"), ("majority", "row")),
            ):
                with self.subTest(heading=heading, row_type=row_type):
                    parsed, result = self.parsed_sections(
                        "plain_text", f"Fictional opening text.\n\n{heading}\n\nMore fictional reasoning.",
                        opinion_type=row_type,
                    )
                    self.assertEqual(self.kinds(result), [base, expected])
                    self.assertEqual(result[1].char_start, parsed.text.index(heading))

    def test_longer_qualified_opinion_heading_is_unmatched(self) -> None:
        for row_type, expected in (
            ("010combined", ("unknown", "none")),
            ("040dissent", ("dissent", "row")),
            ("020lead", ("majority", "row")),
        ):
            with self.subTest(row_type=row_type):
                _, result = self.parsed_sections(
                    "plain_text", "Fictional opening text.\n\nAMENDED OPINION ON REHEARING"
                    "\n\nMore fictional reasoning.", opinion_type=row_type,
                )
                self.assertEqual(self.kinds(result), [expected])

    def test_heading_cues_against_running_bases(self) -> None:
        separate = (
            ("DISSENT", "dissent"),
            ("CONCURRENCE", "concurrence"),
            ("CONCURRING IN PART AND DISSENTING IN PART", "concurrence_dissent"),
        )
        for row_type in ("010combined", "020lead", "040dissent"):
            for heading, kind in separate:
                with self.subTest(row_type=row_type, heading=heading):
                    _, result = self.parsed_sections(
                        "plain_text", f"Opening text.\n\n{heading}\n\nMore text.",
                        opinion_type=row_type,
                    )
                    self.assertEqual(self.kinds(result)[-1], (kind, "line"))
        for heading, kind in (("SYLLABUS", "syllabus"), ("APPENDIX", "appendix"),
                              ("ORDER", "order")):
            with self.subTest(heading=heading):
                _, result = self.parsed_sections(
                    "plain_text", f"Opening text.\n\n{heading}\n\nMore text.",
                    opinion_type="040dissent",
                )
                self.assertEqual(self.kinds(result)[-1], (kind, "line"))
        for row_type, expected in (
            ("010combined", ("per_curiam", "line")),
            ("020lead", ("majority", "row")),
            ("040dissent", ("unknown", "none")),
        ):
            with self.subTest(row_type=row_type):
                _, result = self.parsed_sections(
                    "plain_text", "Opening text.\n\nPER CURIAM\n\nMore text.",
                    opinion_type=row_type,
                )
                self.assertEqual(self.kinds(result)[-1], expected)
        _, author_conflict = self.parsed_sections(
            "plain_text", "Opening text.\n\nJUSTICE EXAMPLE delivered the opinion "
            "of the Court.\n\nMore text.", opinion_type="040dissent",
        )
        self.assertEqual(self.kinds(author_conflict)[-1], ("unknown", "none"))
        _, second_cue = self.parsed_sections(
            "plain_text", "Opening text.\n\nOPINION\n\nJUSTICE EXAMPLE delivered the "
            "opinion of the Court.\n\nMore text.", opinion_type="040dissent",
        )
        self.assertNotIn("majority", [kind for kind, _ in self.kinds(second_cue)])

    def test_prose_opening_with_a_judge_is_not_a_byline(self) -> None:
        for prose in (
            "Justice Example wrote a dissent in Fiction.",
            "Justice Example, in a dissent, argued the search was unreasonable.",
            "Example, J., wrote separately to dissent from the remand.",
            "Justice Example, dissenting in that case, argued the search was unreasonable.",
            "Justice Example, dissenting.The fictional sentence continues without whitespace.",
            "Justice Example, as Justice Sample noted, dissenting from the remand would be wrong.",
            "Justice Example, after making a long and fictional statement of the facts of this "
            "case at some length, delivered the opinion of the court.",
            "Justice Example and Justice Sample concurred in the judgment of the court only "
            "on the ground first stated.",
            "Justices Example, Sample, and Fiction dissented.",
            "Justice Example dissented in Fiction, arguing the search was unreasonable.",
            "Justice Example, Jr., wrote separately.",
        ):
            for column in ("plain_text", "html"):
                with self.subTest(prose=prose, column=column):
                    raw = (f"Opening text.\n\n{prose}\n\nMore." if column == "plain_text"
                           else f"<p>Opening text.</p><p>{prose}</p><p>More.</p>")
                    _, result = self.parsed_sections(column, raw, opinion_type="020lead")
                    self.assertEqual(self.kinds(result), [("majority", "row")])

    def test_a_wrapped_joiners_clause_still_reads(self) -> None:
        line = ("JUSTICE EXAMPLE, with whom JUSTICE SAMPLE and\n"
                "JUSTICE FICTION join, dissenting.")
        for column in ("plain_text", "html"):
            with self.subTest(column=column):
                raw = (f"Opening text.\n\nOPINION\n\nReasoning.\n\n{line}\n\nDissent text."
                       if column == "plain_text" else
                       f"<p>Opening text.</p><h2>OPINION</h2><p>Reasoning.</p><p>{line}</p>"
                       "<p>Dissent text.</p>")
                _, result = self.parsed_sections(column, raw)
                self.assertEqual(self.kinds(result), [
                    ("unknown", "none"), ("majority", "line"), ("dissent", "line"),
                ])

    def test_separate_opinion_byline_shapes_open_at_their_paragraph(self) -> None:
        first = "JUSTICE FICTION delivered the opinion of the Court."
        for line, kind in (
            ("JUSTICE EXAMPLE dissented.", "dissent"),
            ("JUSTICE EXAMPLE dissents.", "dissent"),
            ("JUSTICE EXAMPLE concurred.", "concurrence"),
            ("JUSTICE EXAMPLE, with whom concurred the CHIEF JUSTICE and Mr. Justice SAMPLE, "
             "dissenting.", "dissent"),
            ("MR. CHIEF JUSTICE EXAMPLE, with whom concurred MR. JUSTICE SAMPLE, dissenting.",
             "dissent"),
            ("MR. JUSTICE EXAMPLE, concurring specially:", "concurrence"),
            ("Mr. Justice EXAMPLE and Mr. Justice SAMPLE dissenting. "
             "For dissenting opinion, see 000 U.S. 000.", "dissent"),
            ("R. GUY EXAMPLE, JR., Circuit Judge, dissenting.", "dissent"),
            ("EXAMPLE, Sr., Circuit Judge, concurring.", "concurrence"),
            ("EXAMPLE, Circuit Judge (dissenting).", "dissent"),
            ("SAMPLE, Circuit Judge (concurring).", "concurrence"),
        ):
            for column in ("plain_text", "html"):
                with self.subTest(line=line, column=column):
                    raw = (f"{first}\n\nFictional court reasoning.\n\n{line}\n\n"
                           "Fictional separate reasoning." if column == "plain_text" else
                           f"<p>{first}</p><p>Fictional court reasoning.</p><p>{line}</p>"
                           "<p>Fictional separate reasoning.</p>")
                    parsed, result = self.parsed_sections(column, raw)
                    self.assertEqual(self.kinds(result), [("majority", "line"), (kind, "line")])
                    self.assertEqual(result[1].char_start, parsed.text.index(line))

    def test_parenthesised_dispositions_follow_a_panel_and_bare_court_byline(self) -> None:
        panel = "Before EXAMPLE, SAMPLE, and FICTION, Circuit Judges."
        court = "SAMPLE, Circuit Judge."
        concurrence = "FICTION, Circuit Judge (concurring)."
        dissent = "EXAMPLE, Circuit Judge (dissenting)."
        for column in ("plain_text", "html"):
            with self.subTest(column=column):
                raw = (f"{panel}\n\n{court}\n\nFictional court reasoning.\n\n{concurrence}\n\n"
                       f"Fictional concurrence.\n\n{dissent}\n\nFictional dissent."
                       if column == "plain_text" else
                       f"<p>{panel}</p><p>{court}</p><p>Fictional court reasoning.</p>"
                       f"<p>{concurrence}</p><p>Fictional concurrence.</p>"
                       f"<p>{dissent}</p><p>Fictional dissent.</p>")
                parsed, result = self.parsed_sections(column, raw)
                self.assertEqual(self.kinds(result), [
                    ("unknown", "none"), ("majority", "line"),
                    ("concurrence", "line"), ("dissent", "line"),
                ])
                self.assertEqual([section.char_start for section in result], [
                    0, parsed.text.index(court), parsed.text.index(concurrence), parsed.text.index(dissent),
                ])

    def test_a_typed_court_opinion_resumes_its_type_after_a_syllabus(self) -> None:
        for row_type, per_curiam, expected in (
            ("025plurality", False, ("plurality", "row")),
            ("010combined", True, ("per_curiam", "flag")),
            ("010combined", False, ("majority", "line")),
        ):
            with self.subTest(row_type=row_type, per_curiam=per_curiam):
                _, result = self.parsed_sections(
                    "plain_text", "Syllabus\n\nFictional summary.\n\nOPINION\n\nReasoning.",
                    opinion_type=row_type, per_curiam=per_curiam,
                )
                self.assertEqual(self.kinds(result), [("syllabus", "line"), expected])

    def test_an_opening_starts_at_its_text_after_extra_blank_lines(self) -> None:
        parsed, result = self.parsed_sections(
            "plain_text", "Opening text.\n\n\n\nEXAMPLE, J., dissenting.\n\nMore text.",
        )
        self.assertEqual(result[-1].char_start, parsed.text.index("EXAMPLE"))

    def test_participation_lines_are_headmatter(self) -> None:
        cases = (
            "SAMPLE, C. J., delivered the opinion of the court, in which EXAMPLE, J., "
            "joined. FICTION, J., delivered a separate dissenting opinion.",
            "SAMPLE, C. J., delivered the opinion of the court, in which EXAMPLE, J., "
            "concurred. FICTION, J., delivered a separate dissenting opinion.",
            "SAMPLE, J., with whom EXAMPLE, J., joined.",
            "SAMPLE, J., with whom EXAMPLE, J., joins.",
            "JUSTICE SAMPLE, with whom JUSTICE EXAMPLE and JUSTICE FICTION join.",
            "SAMPLE, C. J., delivered the opinion of the court, in which EXAMPLE, J., joined.",
            "SAMPLE, J., delivered the opinion of the Court. FICTION, J., filed a dissenting opinion.",
            "SAMPLE, J., delivered the opinion of the Court. FICTION, J., joined.",
            "SAMPLE, J., delivered the opinion of the Court. FICTION, J., took no part.",
            "SAMPLE, J., delivered the opinion of the Court. FICTION, J., concurring.",
        )
        for line in cases:
            for column in ("plain_text", "html"):
                with self.subTest(line=line, column=column):
                    raw = (f"Opening text.\n\n{line}\n\nClosing text." if column == "plain_text"
                           else f"<p>Opening text.</p><p>{line}</p><p>Closing text.</p>")
                    parsed, result = self.parsed_sections(column, raw)
                    self.assertEqual(self.kinds(result), [
                        ("unknown", "none"), ("headmatter", "line"), ("unknown", "none"),
                    ])
                    self.assertEqual(result[1].char_start, parsed.text.index(line))

    def test_every_heading_and_unmatched_centered_line(self) -> None:
        heading_cases = (
            ("opinion", "majority"), ("opinion of the court", "majority"),
            ("majority opinion", "majority"), ("dissent", "dissent"),
            ("dissenting opinion", "dissent"), ("concurrence", "concurrence"),
            ("concurring opinion", "concurrence"),
            ("concurring in part and dissenting in part", "concurrence_dissent"),
            ("dissenting in part and concurring in part", "concurrence_dissent"),
            ("per curiam", "per_curiam"), ("syllabus", "syllabus"),
            ("appendix", "appendix"),
            ("appendix to opinion of the court", "appendix"),
            ("order", "order"), ("notes", "notes"),
        )
        self.assertEqual(tuple(sections.HEADING_CUES.items()), heading_cases)
        for heading, cue in heading_cases:
            with self.subTest(heading=heading):
                _, result = self.parsed_sections("plain_text", heading.upper())
                expected = "unknown" if cue == "notes" else cue
                typed_by = "none" if cue == "notes" else "line"
                self.assertEqual(self.kinds(result), [(expected, typed_by)])
        for heading in ("APPENDIX A", "APPENDIX 2", "APPENDIX TO OPINION OF THE COURT"):
            with self.subTest(heading=heading):
                _, result = self.parsed_sections("plain_text", f"___ {heading} ---")
                self.assertEqual(self.kinds(result), [("appendix", "line")])
        _, unmatched = self.parsed_sections("html_columbia", "<center>State of Example</center>")
        self.assertEqual(self.kinds(unmatched), [("unknown", "none")])

    def test_long_paragraph_reads_only_a_bounded_byline_prefix(self) -> None:
        long_line = "Fictional argument " * sections.LINE_MAX_CHARS
        _, long_result = self.parsed_sections("plain_text", long_line)
        self.assertEqual(self.kinds(long_result), [("unknown", "none")])
        for prefix, kind in (
            ("EXAMPLE, J., dissenting.", "dissent"),
            ("JUSTICE EXAMPLE, concurring in part and dissenting in part.", "concurrence_dissent"),
            ("JUSTICE EXAMPLE delivered the opinion of the Court.", "majority"),
        ):
            for column in ("plain_text", "html"):
                with self.subTest(prefix=prefix, column=column):
                    line = prefix + " The dissent disagrees. " + long_line
                    raw = (f"Opening text.\n\n{line}" if column == "plain_text"
                           else f"<p>Opening text.</p><p>{line}</p>")
                    parsed, result = self.parsed_sections(column, raw)
                    self.assertEqual(self.kinds(result), [("unknown", "none"), (kind, "line")])
                    self.assertEqual(result[1].char_start, parsed.text.index(prefix))
        _, long_prefix = self.parsed_sections(
            "plain_text", "JUSTICE EXAMPLE delivered the opinion of the Court with " + long_line,
        )
        self.assertEqual(self.kinds(long_prefix), [("unknown", "none")])

    def test_quoted_or_footnoted_bylines_are_unread(self) -> None:
        _, quoted = self.parsed_sections(
            "xml_harvard", '<opinion><blockquote><p>EXAMPLE, J., dissenting.</p></blockquote>'
            '<p>Fictional reasoning.</p></opinion>',
        )
        self.assertEqual(self.kinds(quoted), [("unknown", "none")])
        _, footnoted = self.parsed_sections(
            "xml_harvard", '<opinion><footnote label="1"><p>EXAMPLE, J., dissenting.</p>'
            '</footnote><p>Fictional reasoning.</p></opinion>',
        )
        self.assertEqual(self.kinds(footnoted), [("footnote", "markup"), ("unknown", "none")])

    def test_slip_caption_syllabus_opinions_and_footnotes(self) -> None:
        court = "SUPREME COURT OF THE UNITED STATES"
        summary = "A fictional summary begins here."
        participation = ("SAMPLE, J., delivered the opinion of the Court, in which EXAMPLE, J., "
                         "joined. FICTION, J., filed a dissenting opinion.")
        opinion_head = "Cite as: 999 U. S. 100 (2099)"
        byline = ("JUSTICE SAMPLE delivered the opinion of the Court. "
                  + "Fictional reasoning. " * sections.LINE_MAX_CHARS)
        divider = "—— 1 First fictional footnote."
        second_note = "2 Second fictional footnote."
        resumed = "The fictional body resumes here."
        separate_head = "Cite as: 999 U. S. 100 (2099)\nOpinion of EXAMPLE, J."
        separate_byline = ("JUSTICE EXAMPLE, with whom JUSTICE FICTION joins, "
                           "concurring in part and dissenting in part.")
        raw = "\n\n".join((
            "(Slip Opinion) 1", "Syllabus", "NOTE: Fictional editorial notice.", court,
            "EXAMPLE v. FICTION", "No. 99-1000. Argued January 1, 2099—Decided February 2, 2099*",
            summary, "Held: The fictional result follows.", participation,
            opinion_head, "Opinion of the Court", "NOTICE: Fictional publication notice.", court,
            "EXAMPLE v. FICTION", "[February 2, 2099]", byline, "More fictional reasoning.",
            "NOTES", divider, "The first fictional footnote continues.", second_note,
            "Cite as: 999 U. S. 100 (2099) 2\nOpinion of the Court", resumed,
            separate_head, "NOTICE: Another fictional notice.", court, "EXAMPLE v. FICTION",
            "[February 2, 2099]", separate_byline, "Fictional separate reasoning.",
        ))
        parsed, result = self.parsed_sections("plain_text", raw)
        self.assertEqual(self.kinds(result), [
            ("headmatter", "line"), ("syllabus", "line"), ("headmatter", "line"),
            ("majority", "line"), ("footnote", "line"), ("footnote", "line"),
            ("majority", "line"), ("headmatter", "line"), ("concurrence_dissent", "line"),
        ])
        self.assertEqual([section.char_start for section in result], [
            0, parsed.text.index(summary), parsed.text.index(opinion_head), parsed.text.index(byline),
            parsed.text.index(divider), parsed.text.index(second_note), parsed.text.index(resumed),
            parsed.text.index(separate_head), parsed.text.index(separate_byline),
        ])
        self.assertEqual([section.label for section in result], [
            None, None, None, None, "1", "2", None, None, None,
        ])
        self.assertIn(participation, parsed.text[result[1].char_start:result[1].char_end])
        self.assertIn("The first fictional footnote continues.",
                      parsed.text[result[4].char_start:result[4].char_end])
        for section in result:
            self.assertIsNone(section.ref_offset)
            self.assertIsNone(section.parent)

    def test_participation_inside_a_slip_syllabus_continues_it(self) -> None:
        line = "SAMPLE, J., with whom EXAMPLE, J., joined."
        raw = ("(Slip Opinion)\nSyllabus\n\nSUPREME COURT OF THE UNITED STATES\n\n"
               "EXAMPLE v. FICTION\n\nNo. 99-1000. Decided February 2, 2099\n\n"
               f"Fictional summary.\n\n{line}\n\nMore fictional summary.")
        parsed, result = self.parsed_sections("plain_text", raw)
        self.assertEqual(self.kinds(result), [("headmatter", "line"), ("syllabus", "line")])
        self.assertEqual(result[1].char_start, parsed.text.index("Fictional summary."))
        self.assertIn(line, parsed.text[result[1].char_start:result[1].char_end])

    def test_a_cue_supersedes_the_resume_after_participation(self) -> None:
        _, result = self.parsed_sections(
            "plain_text", "OPINION\n\nFictional reasoning.\n\n"
            "SAMPLE, J., with whom EXAMPLE, J., joined.\n\nDISSENT\n\nFictional dissent.",
        )
        self.assertEqual(self.kinds(result), [
            ("majority", "line"), ("headmatter", "line"), ("dissent", "line"),
        ])

    def test_a_slip_divider_before_the_page_head_is_body(self) -> None:
        raw = ("Fictional reasoning.\n\n—— 1 Earlier fictional body.\n\n"
               "1 Still fictional body.\n\nCite as: 999 U. S. 100 (2099)\nOpinion of the Court\n\n"
               "More fictional reasoning.\n\n—— 2 Actual fictional footnote.")
        parsed, result = self.parsed_sections("plain_text", raw, opinion_type="020lead")
        self.assertEqual(self.kinds(result), [("majority", "row"), ("footnote", "line")])
        self.assertEqual(result[1].char_start, parsed.text.index("—— 2"))
        self.assertEqual(result[1].label, "2")

    def test_slip_furniture_without_a_page_head_is_body(self) -> None:
        raw = ("SUPREME COURT OF THE UNITED STATES\n\nEXAMPLE v. FICTION\n\n"
               "[February 2, 2099]\n\nNOTE: Fictional notice.\n\n——\n\n"
               "1 Fictional body.\n\n* More fictional body.")
        _, result = self.parsed_sections("plain_text", raw, opinion_type="020lead")
        self.assertEqual(self.kinds(result), [("majority", "row")])

    def test_participation_without_a_page_head_keeps_its_headmatter_span(self) -> None:
        _, result = self.parsed_sections(
            "plain_text", "Syllabus\n\nFictional summary.\n\n"
            "SAMPLE, J., with whom EXAMPLE, J., joined.\n\nMore fictional summary.",
        )
        self.assertEqual(self.kinds(result), [
            ("syllabus", "line"), ("headmatter", "line"), ("syllabus", "line"),
        ])

    def test_slip_page_grammar_is_plain_text_only(self) -> None:
        _, result = self.parsed_sections(
            "html", "<p>(Slip Opinion)</p><p>SUPREME COURT OF THE UNITED STATES</p>"
            "<p>EXAMPLE v. FICTION</p><p>—— 1 Fictional body.</p>", opinion_type="020lead",
        )
        self.assertEqual(self.kinds(result), [("majority", "row")])

    def test_slip_running_heads_continue_the_opinion(self) -> None:
        for head in ("Syllabus", "Opinion of the Court", "PER CURIAM",
                     "Opinion of EXAMPLE, J.", "EXAMPLE, J., dissenting"):
            for merged in (False, True):
                with self.subTest(head=head, merged=merged):
                    separator = "\n" if merged else "\n\n"
                    _, result = self.parsed_sections(
                        "plain_text", "Fictional reasoning.\n\n"
                        f"Cite as: 999 U. S. 100 (2099){separator}{head}\n\nMore reasoning.",
                        opinion_type="020lead",
                    )
                    self.assertEqual(self.kinds(result), [("majority", "row")])

    def test_a_running_head_merged_with_body_is_read_as_body(self) -> None:
        _, result = self.parsed_sections(
            "plain_text", "Fictional reasoning.\n\nCite as: 999 U. S. 100 (2099)\n\n"
            "Opinion of the Court\nFictional body continues.", opinion_type="040dissent",
        )
        self.assertEqual(self.kinds(result), [("dissent", "row")])

    def test_held_closes_a_syllabus_caption_without_a_date(self) -> None:
        raw = ("(Slip Opinion)\n\nSyllabus\n\nSUPREME COURT OF THE UNITED STATES\n\n"
               "EXAMPLE v. FICTION\n\nNo. 99-1000.\n\nHeld: A fictional result.\n\n"
               "Fictional summary continues.")
        parsed, result = self.parsed_sections("plain_text", raw)
        self.assertEqual(self.kinds(result), [("headmatter", "line"), ("syllabus", "line")])
        self.assertEqual(result[1].char_start, parsed.text.index("Held:"))

    def test_a_per_curiam_byline_closes_a_slip_caption_with_or_without_a_date(self) -> None:
        for date in ("", "[February 2, 2099]\n\n"):
            with self.subTest(date=date):
                raw = ("(Slip Opinion)\nPER CURIAM\n\nNOTICE: Fictional notice.\n\n"
                       "SUPREME COURT OF THE UNITED STATES\n\nEXAMPLE v. FICTION\n\n"
                       f"{date}PER CURIAM. Fictional reasoning.")
                parsed, result = self.parsed_sections("plain_text", raw)
                self.assertEqual(self.kinds(result), [("headmatter", "line"), ("per_curiam", "line")])
                self.assertEqual(result[1].char_start, parsed.text.index("PER CURIAM. Fictional"))

    def test_slip_footnote_labels_and_an_unlabelled_continuation_at_the_end(self) -> None:
        raw = ("Cite as: 999 U. S. 100 (2099)\nOpinion of the Court\n\n"
               "1 Fictional body list.\n\n——\n\nContinued fictional footnote.\n\n"
               "* Fictional star footnote.\n\n† Fictional dagger footnote.\n\n"
               "‡ Fictional double dagger footnote.\n\n999 Fictional numbered footnote.\n\n"
               "EXAMPLE, J., dissenting.")
        parsed, result = self.parsed_sections("plain_text", raw, opinion_type="020lead")
        self.assertEqual(self.kinds(result), [("majority", "row")] + [("footnote", "line")] * 5)
        self.assertEqual([section.label for section in result[1:]], [None, "*", "†", "‡", "999"])
        self.assertEqual(result[1].char_start, parsed.text.index("Continued fictional footnote."))
        self.assertEqual(result[-1].char_end, len(parsed.text))

    def test_a_caption_reaches_back_only_over_its_own_page_head(self) -> None:
        raw = ("Cite as: 999 U. S. 100 (2099)\nOpinion of the Court\n\nFictional reasoning.\n\n"
               "SUPREME COURT OF THE UNITED STATES\n\nEXAMPLE v. FICTION\n\n[February 2, 2099]\n\n"
               "JUSTICE EXAMPLE, dissenting.\n\nFictional dissent.")
        parsed, result = self.parsed_sections("plain_text", raw, opinion_type="020lead")
        self.assertEqual(self.kinds(result), [
            ("majority", "row"), ("headmatter", "line"), ("dissent", "line"),
        ])
        self.assertEqual(result[1].char_start, parsed.text.index("SUPREME COURT"))

    def test_circuit_headers_bare_number_footnotes_and_body_resumption(self) -> None:
        for header in (
            "No. 99-1000 EXAMPLE v. FICTION Page",
            "Nos. 99-1000/99-1001 EXAMPLE v. FICTION Page",
            "Nos. 99-1000, 99-1001 Page",
        ):
            with self.subTest(header=header):
                raw = "\n\n".join((
                    f"{header} 2", "Fictional body on the second page.",
                    "1", "First fictional footnote.", "A fictional continuation.",
                    "2", "Second fictional footnote.", f"{header} 3",
                    "Fictional body on the third page.",
                ))
                parsed, result = self.parsed_sections("plain_text", raw)
                self.assertEqual(self.kinds(result), [
                    ("unknown", "none"), ("footnote", "line"),
                    ("footnote", "line"), ("unknown", "none"),
                ])
                self.assertEqual([section.label for section in result], [None, "1", "2", None])
                self.assertEqual([section.char_start for section in result], [
                    0, parsed.text.index("\n\n1\n\n") + 2,
                    parsed.text.index("\n\n2\n\n") + 2,
                    parsed.text.index("Fictional body on the third page."),
                ])
                self.assertIn("A fictional continuation.",
                              parsed.text[result[1].char_start:result[1].char_end])
                for section in result:
                    self.assertIsNone(section.ref_offset)
                    self.assertIsNone(section.parent)

    def test_circuit_bare_numbers_before_a_header_are_body(self) -> None:
        raw = ("Fictional first-page body.\n\n1\n\nFirst fictional untyped note.\n\n"
               "Page 2\n\n2\n\nMore fictional body.\n\nNo. 99-1000 Page 3\n\n"
               "Fictional third-page body.\n\n3\n\nA fictional typed footnote.")
        parsed, result = self.parsed_sections("plain_text", raw)
        self.assertEqual(self.kinds(result), [("unknown", "none"), ("footnote", "line")])
        self.assertEqual(result[1].label, "3")
        self.assertEqual(result[1].char_start, parsed.text.index("\n\n3\n\n") + 2)

    def test_circuit_header_like_text_does_not_arm_the_grammar(self) -> None:
        for header in (
            "Page 2", "No. 99-1000", "No. 99-1000 Page 2 trailing prose.",
            "No. 99-1000 Page 2\nFictional body in the same paragraph.",
            "No. 9-1000 Page 2", "No. 99-100 Page 2",
        ):
            with self.subTest(header=header):
                _, result = self.parsed_sections(
                    "plain_text", f"Fictional body.\n\n{header}\n\n1\n\nFictional note.",
                )
                self.assertEqual(self.kinds(result), [("unknown", "none")])

    def test_circuit_footnote_openers_are_only_bare_numbers(self) -> None:
        raw = ("No. 99-1000 Page 2\n\n1 Fictional body list.\n\n1234\n\n"
               "Fictional body continues.\n\n999\n\nFictional footnote.\n\n"
               "2 Fictional continuation of that footnote.")
        parsed, result = self.parsed_sections("plain_text", raw)
        self.assertEqual(self.kinds(result), [("unknown", "none"), ("footnote", "line")])
        self.assertEqual(result[1].label, "999")
        self.assertEqual(result[1].char_start, parsed.text.index("\n\n999\n\n") + 2)

    def test_circuit_page_grammar_is_plain_text_only(self) -> None:
        _, result = self.parsed_sections(
            "html", "<p>No. 99-1000 Page 2</p><p>Fictional body.</p>"
            "<p>1</p><p>Fictional note.</p>",
        )
        self.assertEqual(self.kinds(result), [("unknown", "none")])

    def test_a_date_inside_a_caption_paragraph_ends_the_syllabus_caption(self) -> None:
        raw = ("(Slip Opinion)\nSyllabus\n\nSUPREME COURT OF THE UNITED STATES\n\n"
               "EXAMPLE v. FICTION\nNo. 99-1000. Decided February 2, 2099\n\n"
               "Fictional summary.")
        parsed, result = self.parsed_sections("plain_text", raw)
        self.assertEqual(self.kinds(result), [("headmatter", "line"), ("syllabus", "line")])
        self.assertEqual(result[1].char_start, parsed.text.index("Fictional summary."))

    def test_a_date_merged_with_a_larger_summary_opens_the_syllabus_at_its_paragraph(self) -> None:
        summary = ("No. 99-1000. Decided February 2, 2099\n"
                   "A fictional summary supplies the larger part of this paragraph, "
                   "describing only an invented dispute between Example and Fiction.")
        raw = ("(Slip Opinion)\nSyllabus\n\nSUPREME COURT OF THE UNITED STATES\n\n"
               f"EXAMPLE v. FICTION\n\n{summary}\n\nMore fictional summary.")
        parsed, result = self.parsed_sections("plain_text", raw)
        self.assertEqual(self.kinds(result), [("headmatter", "line"), ("syllabus", "line")])
        self.assertEqual(result[1].char_start, parsed.text.index(summary))

    def test_an_opinion_body_resumes_after_a_caption_date_without_a_byline(self) -> None:
        raw = ("Cite as: 999 U. S. 100 (2099)\nOpinion of the Court\n\n"
               "SUPREME COURT OF THE UNITED STATES\n\nEXAMPLE v. FICTION\n\n"
               "[February 2, 2099]\n\nFictional reasoning without an author line.")
        parsed, result = self.parsed_sections("plain_text", raw, opinion_type="020lead")
        self.assertEqual(self.kinds(result), [("headmatter", "line"), ("majority", "row")])
        self.assertEqual(result[1].char_start, parsed.text.index("Fictional reasoning"))

    def test_a_page_head_closes_an_undated_caption(self) -> None:
        raw = ("Cite as: 999 U. S. 100 (2099)\nOpinion of the Court\n\n"
               "SUPREME COURT OF THE UNITED STATES\n\nEXAMPLE v. FICTION\n\n"
               "Cite as: 999 U. S. 100 (2099) 2\nOpinion of the Court\n\nFictional reasoning.")
        parsed, result = self.parsed_sections("plain_text", raw, opinion_type="020lead")
        self.assertEqual(self.kinds(result), [("headmatter", "line"), ("majority", "row")])
        self.assertEqual(result[1].char_start, parsed.text.index("Fictional reasoning."))

    def test_even_slip_heads_close_footnotes_and_resume_the_body(self) -> None:
        for head in (
            "2 EXAMPLE v. FICTION\n\nOpinion of the Court",
            "2 EXAMPLE v.\nFICTION\nOpinion of the Court",
            "2 EXAMPLE v. FICTION Opinion of the Court",
        ):
            with self.subTest(head=head):
                raw = ("Cite as: 999 U. S. 100 (2099)\nOpinion of the Court\n\n"
                       f"Fictional reasoning.\n\n—— 1 Fictional footnote.\n\n{head}\n\n"
                       "Resumed fictional reasoning.")
                parsed, result = self.parsed_sections("plain_text", raw, opinion_type="020lead")
                self.assertEqual(self.kinds(result), [
                    ("majority", "row"), ("footnote", "line"), ("majority", "row"),
                ])
                self.assertEqual(result[1].label, "1")
                self.assertEqual(result[2].char_start, parsed.text.index("Resumed fictional reasoning."))

    def test_a_citation_tail_at_a_page_top_is_not_an_even_head(self) -> None:
        raw = ("Cite as: 999 U. S. 100 (2099)\nOpinion of the Court\n\nFictional reasoning.\n\n"
               "999 U. S. 100, 105 (2099).\n\nJUSTICE EXAMPLE, concurring.\n\n"
               "Fictional separate reasoning.")
        parsed, result = self.parsed_sections("plain_text", raw, opinion_type="020lead")
        self.assertEqual(self.kinds(result), [("majority", "row"), ("concurrence", "line")])
        self.assertEqual(result[1].char_start, parsed.text.index("JUSTICE EXAMPLE"))
        self.assertIn("999 U. S. 100, 105 (2099).", parsed.text[result[0].char_start:result[0].char_end])

    def test_a_rule_inside_a_larger_body_paragraph_owes_the_next_footnote(self) -> None:
        mixed = ("Fictional body reasoning fills most of this paragraph with an invented argument.\n"
                 "——\n1 Brief note.")
        raw = ("Cite as: 999 U. S. 100 (2099)\nOpinion of the Court\n\n"
               f"Fictional opening.\n\n{mixed}\n\n2 Following fictional footnote.")
        parsed, result = self.parsed_sections("plain_text", raw, opinion_type="020lead")
        self.assertEqual(self.kinds(result), [("majority", "row"), ("footnote", "line")])
        self.assertEqual(result[1].char_start, parsed.text.index("2 Following fictional footnote."))
        self.assertEqual(result[1].label, "2")
        self.assertIn(mixed, parsed.text[result[0].char_start:result[0].char_end])

    def test_a_rule_inside_a_larger_footnote_paragraph_opens_at_the_paragraph(self) -> None:
        mixed = ("Brief body.\n——\n1 A fictional footnote fills most of this paragraph "
                 "with details about an invented argument.")
        raw = ("Cite as: 999 U. S. 100 (2099)\nOpinion of the Court\n\n"
               f"Fictional opening.\n\n{mixed}\n\nFictional note continues.")
        parsed, result = self.parsed_sections("plain_text", raw, opinion_type="020lead")
        self.assertEqual(self.kinds(result), [("majority", "row"), ("footnote", "line")])
        self.assertEqual(result[1].char_start, parsed.text.index(mixed))
        self.assertEqual(result[1].label, "1")

    def test_a_circuit_number_inside_a_larger_footnote_paragraph_opens_at_the_paragraph(self) -> None:
        mixed = ("Brief body.\n1\nA fictional footnote supplies most of this paragraph "
                 "with details about an invented argument.")
        parsed, result = self.parsed_sections(
            "plain_text", f"No. 99-1000 Page 2\n\nFictional opening.\n\n{mixed}",
        )
        self.assertEqual(self.kinds(result), [("unknown", "none"), ("footnote", "line")])
        self.assertEqual(result[1].char_start, parsed.text.index(mixed))
        self.assertEqual(result[1].label, "1")

    def test_a_circuit_number_on_the_first_line_opens_its_footnote(self) -> None:
        note = "1\nA fictional footnote follows its number in the same paragraph."
        parsed, result = self.parsed_sections(
            "plain_text", f"No. 99-1000 Page 2\n\nFictional opening.\n\n{note}",
        )
        self.assertEqual(self.kinds(result), [("unknown", "none"), ("footnote", "line")])
        self.assertEqual(result[1].char_start, parsed.text.index(note))
        self.assertEqual(result[1].label, "1")

    def test_a_standalone_dissent_slip_types_its_front_block_from_offset_zero(self) -> None:
        raw = ("(Slip Opinion)\nEXAMPLE, J., dissenting\n\nNOTICE: Fictional notice.\n\n"
               "SUPREME COURT OF THE UNITED STATES\n\nEXAMPLE v. FICTION\n\n"
               "[February 2, 2099]\n\nFictional dissent reasoning.")
        _, result = self.parsed_sections("plain_text", raw)
        self.assertEqual(self.kinds(result), [("dissent", "line")])
        self.assertEqual(result[0].char_start, 0)

    def test_a_statement_slip_keeps_its_front_block_unknown(self) -> None:
        raw = ("(Slip Opinion)\nStatement of EXAMPLE, J.\n\nNOTICE: Fictional notice.\n\n"
               "SUPREME COURT OF THE UNITED STATES\n\nEXAMPLE v. FICTION\n\n"
               "[February 2, 2099]\n\nFictional statement reasoning.")
        _, result = self.parsed_sections("plain_text", raw)
        self.assertEqual(self.kinds(result), [("unknown", "none")])
        self.assertEqual(result[0].char_start, 0)

    def test_a_concurrence_running_head_switches_the_body_despite_an_unreadable_byline(self) -> None:
        raw = ("Cite as: 999 U. S. 100 (2099)\nOpinion of the Court\n\nFictional majority.\n\n"
               "Cite as: 999 U. S. 100 (2099) 3\nSAMPLE, J., concurring.\n\n"
               "SAMPLE [Justice], concurring.\n\nFictional concurrence.")
        parsed, result = self.parsed_sections("plain_text", raw, opinion_type="020lead")
        self.assertEqual(self.kinds(result), [("majority", "row"), ("concurrence", "line")])
        self.assertEqual(result[1].char_start, parsed.text.index("SAMPLE [Justice]"))

    def test_a_byline_after_a_closing_formula_opens_at_its_paragraph(self) -> None:
        for closing in ("It is so ordered.", "So ordered."):
            with self.subTest(closing=closing):
                paragraph = f"{closing} JUSTICE EXAMPLE, dissenting. Fictional separate reasoning."
                parsed, result = self.parsed_sections(
                    "plain_text", f"Fictional majority.\n\n{paragraph}", opinion_type="020lead",
                )
                self.assertEqual(self.kinds(result), [("majority", "row"), ("dissent", "line")])
                self.assertEqual(result[1].char_start, parsed.text.index(paragraph))

    def test_a_disposition_hyphenated_across_a_line_break_reads_whole(self) -> None:
        for byline, kind in (
            ("JUSTICE EXAMPLE, dis-\nsenting.", "dissent"),
            ("JUSTICE EXAMPLE deliv-\nered the opinion of the Court.", "majority"),
        ):
            with self.subTest(byline=byline):
                parsed, result = self.parsed_sections(
                    "plain_text", f"Fictional opening.\n\n{byline} Fictional reasoning.",
                )
                self.assertEqual(self.kinds(result), [("unknown", "none"), (kind, "line")])
                self.assertEqual(result[1].char_start, parsed.text.index(byline))

    def test_a_starred_period_ends_the_byline_before_body_dispositions(self) -> None:
        raw = ("Fictional majority.\n\nJUSTICE EXAMPLE, dissenting.* "
               "The fictional concurrence discusses an invented argument.")
        parsed, result = self.parsed_sections("plain_text", raw, opinion_type="020lead")
        self.assertEqual(self.kinds(result), [("majority", "row"), ("dissent", "line")])
        self.assertEqual(result[1].char_start, parsed.text.index("JUSTICE EXAMPLE"))

    def test_a_qualified_joiner_still_opens_the_separate_opinion(self) -> None:
        raw = ("Fictional majority.\n\nJUSTICE EXAMPLE, with whom JUSTICE SAMPLE joins "
               "except as to Part III, dissenting. Fictional separate reasoning.")
        parsed, result = self.parsed_sections("plain_text", raw, opinion_type="020lead")
        self.assertEqual(self.kinds(result), [("majority", "row"), ("dissent", "line")])
        self.assertEqual(result[1].char_start, parsed.text.index("JUSTICE EXAMPLE"))

    def test_delivery_of_the_courts_opinion_precedes_announcement_of_the_judgment(self) -> None:
        for delivery, kind in (
            ("the opinion of the Court with respect to Parts I and II", "majority"),
            ("an opinion with respect to Parts I and II", "plurality"),
        ):
            with self.subTest(delivery=delivery):
                raw = ("Fictional opening.\n\nCHIEF JUSTICE EXAMPLE announced the judgment "
                       f"of the Court and delivered {delivery}. Fictional reasoning.")
                parsed, result = self.parsed_sections("plain_text", raw)
                self.assertEqual(self.kinds(result), [("unknown", "none"), (kind, "line")])
                self.assertEqual(result[1].char_start, parsed.text.index("CHIEF JUSTICE EXAMPLE"))

    def test_sixth_circuit_cap_combined_and_slip_opinion_shapes(self) -> None:
        sixth = (
            "<div><p>SAMPLE, C. J., delivered the opinion of the court, in which "
            "EXAMPLE, J., joined. FICTION, J., delivered a separate dissenting opinion.</p>"
            "<h2>OPINION</h2><p>SAMPLE, Chief Judge.</p><p>Fictional reasoning.</p>"
            "<h2>DISSENT</h2><p>FICTION, Circuit Judge, dissenting.</p></div>"
        )
        _, result = self.parsed_sections("html", sixth)
        self.assertEqual(self.kinds(result), [
            ("headmatter", "line"), ("majority", "line"), ("majority", "line"),
            ("dissent", "line"), ("dissent", "line"),
        ])
        _, cap = self.parsed_sections(
            "xml_harvard", '<opinion type="majority"><p>Opening text.</p>'
            '<author>EXAMPLE, C. J.</author><p>Fictional reasoning.</p></opinion>',
        )
        self.assertEqual(self.kinds(cap), [("majority", "element"), ("majority", "element")])
        _, slip = self.parsed_sections(
            "plain_text", "Syllabus\n\nFictional summary.\n\nJUSTICE EXAMPLE delivered the "
            "opinion of the Court.\n\nFictional reasoning.\n\nJUSTICE SAMPLE, concurring."
            "\n\nJUSTICE FICTION, dissenting.",
        )
        self.assertEqual(self.kinds(slip), [
            ("syllabus", "line"), ("majority", "line"),
            ("concurrence", "line"), ("dissent", "line"),
        ])

    def test_lawbox_notes_and_continued_footnote_body(self) -> None:
        raw = (
            "<div><center><h1>Example v. Fiction</h1></center></div>"
            "<p>Fictional reasoning<sup>[1]</sup>.</p><h2>NOTES</h2>"
            "<p>[1] First note.</p><p>Continued note.</p>"
            "<p>[2] Second note.</p>"
        )
        parsed, result = self.parsed_sections("html_lawbox", raw)
        self.assertEqual(self.kinds(result), [
            ("headmatter", "markup"), ("unknown", "none"),
            ("footnote", "markup"), ("footnote", "markup"),
        ])
        self.assertEqual([section.label for section in result[2:]], ["[1]", "[2]"])
        self.assertIn("Continued note.", parsed.text[result[2].char_start:result[2].char_end])
        self.assertEqual(result[2].ref_offset, parsed.text.index("Fictional reasoning") + len("Fictional reasoning"))
        self.assertEqual(result[2].parent, 1)
        self.assertIsNone(result[3].ref_offset)

    def test_backlink_labels_repeated_marks_orphan_and_dissent_parent(self) -> None:
        for column, raw in (
            (
                "html_columbia",
                '<div><p>Body<sup><a href="#fn">1</a></sup>.</p>'
                '<footnote_body><sup><a href="#back">1</a></sup><p>Note.</p></footnote_body></div>',
            ),
            (
                "html_anon_2020",
                '<div class="opinion"><p>Body<sup><a href="#fn">1</a></sup>.</p></div>'
                '<div class="footnotes"><li><div><sup><a href="#back">1</a></sup>'
                '<p>Note.</p></div></li></div>',
            ),
        ):
            with self.subTest(column=column):
                parsed, result = self.parsed_sections(column, raw)
                footnote = next(section for section in result if section.section_type == "footnote")
                self.assertEqual(footnote.label, "1")
                self.assertEqual(footnote.ref_offset, parsed.text.index("Body") + len("Body"))
                self.assertEqual(footnote.parent, 0)
        parsed, repeated = self.parsed_sections(
            "xml_harvard", '<opinion><p>First<footnotemark>1</footnotemark>.</p>'
            '<footnote label="1"><p>First note.</p></footnote>'
            '<p>Second<footnotemark>1</footnotemark>.</p>'
            '<footnote label="1"><p>Second note.</p></footnote>'
            '<footnote label="orphan"><p>Orphan note.</p></footnote></opinion>',
        )
        notes = [section for section in repeated if section.section_type == "footnote"]
        self.assertEqual([note.ref_offset for note in notes], [
            parsed.text.index("First") + len("First"),
            parsed.text.index("Second") + len("Second"), None,
        ])
        self.assertIsNone(notes[2].parent)
        dissent_parsed, dissent = self.parsed_sections(
            "xml_harvard", '<opinion><p>Opening text.</p>'
            '<author>EXAMPLE, J., dissenting.</author>'
            '<p>Dissent text<footnotemark>2</footnotemark>.</p>'
            '<footnote label="2"><p>Dissent note.</p></footnote></opinion>',
            opinion_type="020lead",
        )
        note = next(section for section in dissent if section.section_type == "footnote")
        self.assertEqual(note.ref_offset, dissent_parsed.text.index("Dissent text") + len("Dissent text"))
        self.assertIsNotNone(note.parent)
        if note.parent is not None:
            self.assertEqual(dissent[note.parent].section_type, "dissent")

    def test_whole_unknown_html_and_plain_text_with_structural_types(self) -> None:
        _, html_result = self.parsed_sections(
            "html", '<div class="prelims">Fictional panel.</div>'
            '<p>Unclassified reasoning<sup>[1]</sup>.</p>'
            '<div class="footnote"><p>[1] Note.</p></div>',
        )
        self.assertEqual(self.kinds(html_result), [
            ("headmatter", "markup"), ("unknown", "none"), ("footnote", "markup"),
        ])
        _, plain_result = self.parsed_sections("plain_text", "Unclassified reasoning.")
        self.assertEqual(self.kinds(plain_result), [("unknown", "none")])


if __name__ == "__main__":
    unittest.main()
