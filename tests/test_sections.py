"""Section spans and types over fictitious opinion text."""

import unittest

from gideon.worker import opiniontext, sections


class Segment(unittest.TestCase):
    def parsed_sections(
        self, column: str, raw: str, *, opinion_type: str = "010combined",
        per_curiam: bool = False,
    ) -> tuple[opiniontext.Parsed, tuple[sections.Section, ...]]:
        parsed = opiniontext.canonical_text(column, raw)
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
             "opinion of the Court with respect to Parts I and II", "plurality", "line"),
            ("PER CURIAM:", "per_curiam", "line"),
            ("EXAMPLE, J., dissenting in part.", "concurrence_dissent", "line"),
            ("EXAMPLE, J., concurring in part and dissenting in part, per curiam.",
             "per_curiam", "line"),
        )
        for line, kind, typed_by in cases:
            with self.subTest(line=line):
                _, result = self.parsed_sections("plain_text", line)
                self.assertEqual(self.kinds(result), [(kind, typed_by)])

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
        ):
            with self.subTest(prose=prose):
                _, result = self.parsed_sections(
                    "plain_text", f"Opening text.\n\n{prose}\n\nMore.", opinion_type="020lead",
                )
                self.assertEqual(self.kinds(result), [("majority", "row")])

    def test_a_wrapped_joiners_clause_still_reads(self) -> None:
        _, result = self.parsed_sections(
            "plain_text", "Opening text.\n\nOPINION\n\nReasoning.\n\nJUSTICE EXAMPLE, with whom "
            "JUSTICE SAMPLE and\nJUSTICE FICTION join, dissenting.\n\nDissent text.",
        )
        self.assertEqual(self.kinds(result)[-1], ("dissent", "line"))

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
            "SAMPLE, J., with whom EXAMPLE, J., joined.",
        )
        for line in cases:
            with self.subTest(line=line):
                parsed, result = self.parsed_sections(
                    "plain_text", f"Opening text.\n\n{line}\n\nClosing text.",
                )
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

    def test_long_line_and_quoted_or_footnoted_bylines_are_unread(self) -> None:
        long_line = "EXAMPLE, J., dissenting. " + "argument " * sections.LINE_MAX_CHARS
        _, long_result = self.parsed_sections("plain_text", long_line)
        self.assertEqual(self.kinds(long_result), [("unknown", "none")])
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
