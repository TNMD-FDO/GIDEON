"""Reporter-page anchoring over fictitious canonical opinions."""

import unittest

from gideon.worker import anchors, opiniontext, sections


class ReporterPages(unittest.TestCase):
    def parsed_sections(
        self, column: str, raw: str, *, opinion_type: str = "020lead",
    ) -> tuple[opiniontext.Parsed, tuple[sections.Section, ...]]:
        parsed = opiniontext.canonical_text(column, raw)
        segmented = sections.segment(
            parsed, opinion_type=opinion_type, per_curiam=False, column=column,
        )
        return parsed, segmented

    def check(
        self, parsed: opiniontext.Parsed, segmented: tuple[sections.Section, ...],
    ) -> anchors.Anchoring:
        result = anchors.anchor(parsed, segmented)
        rows = result.anchors
        self.assertEqual(rows, tuple(sorted(
            rows, key=lambda row: (row.char_start, dict(row.attrs)["scheme"]),
        )))
        by_scheme: dict[str, list[anchors.Anchor]] = {}
        for row in rows:
            self.assertEqual(row.kind, "reporter_page")
            self.assertLessEqual(0, row.char_start)
            self.assertLess(row.char_start, row.char_end)
            self.assertLessEqual(row.char_end, len(parsed.text))
            by_scheme.setdefault(dict(row.attrs)["scheme"], []).append(row)
        for scheme_rows in by_scheme.values():
            for left, right in zip(scheme_rows, scheme_rows[1:], strict=False):
                self.assertLessEqual(left.char_end, right.char_start)

        body: list[tuple[int, int]] = []
        regions = [body]
        for section in segmented:
            span = (section.char_start, section.char_end)
            if section.section_type == "footnote":
                regions.append([span])
            elif body and body[-1][1] == span[0]:
                body[-1] = (body[-1][0], span[1])
            else:
                body.append(span)
        self.assertTrue(all(
            any(start <= row.char_start and row.char_end <= end
                for spans in regions for start, end in spans)
            for row in rows
        ))
        for spans in regions:
            region_markers = [
                marker for marker in parsed.markers if marker.kind == "page"
                and any(start <= marker.offset < end for start, end in spans)
            ]
            first_by_scheme: dict[str, int] = {}
            for marker in region_markers:
                attrs = dict(marker.attrs)
                scheme = next(
                    (attrs[name] for name in ("pagescheme", "citation_index", "stars")
                     if name in attrs), "1",
                )
                first_by_scheme.setdefault(scheme, marker.offset)
            for start, end in spans:
                span_rows = [
                    row for row in rows if start <= row.char_start and row.char_end <= end
                ]
                for scheme in set(first_by_scheme) | {dict(row.attrs)["scheme"] for row in span_rows}:
                    matching = [row for row in span_rows if dict(row.attrs)["scheme"] == scheme]
                    first = first_by_scheme.get(scheme)
                    if first is None or first >= end:
                        self.assertEqual(matching, [])
                        continue
                    expected_start = max(start, first)
                    self.assertTrue(matching)
                    self.assertEqual(matching[0].char_start, expected_start)
                    self.assertEqual(matching[-1].char_end, end)
                    for left, right in zip(matching, matching[1:], strict=False):
                        self.assertEqual(left.char_end, right.char_start)
        return result

    def test_vocabulary_and_real_paginated_body_tiles_after_first_marker(self) -> None:
        self.assertEqual(anchors.ANCHOR_KINDS, (
            "reporter_page", "pdf_page", "bates", "tr_page", "tr_line",
            "uslm_id", "guideline_id",
        ))
        parsed, segmented = self.parsed_sections(
            "xml_harvard", "<opinion><p>Before<page-number>*8</page-number>After"
            "<page-number>*9</page-number>End</p></opinion>",
        )
        result = self.check(parsed, segmented)
        self.assertEqual(parsed.text, "BeforeAfterEnd")
        self.assertEqual(result.anchors, (
            anchors.Anchor("reporter_page", "8", len("Before"), len("BeforeAfter"),
                           (("scheme", "1"),)),
            anchors.Anchor("reporter_page", "9", len("BeforeAfter"), len(parsed.text),
                           (("scheme", "1"),)),
        ))
        self.assertEqual(anchors.intersecting(result.anchors, 0, len("Before")), ())
        boundary = len("BeforeAfter")
        self.assertEqual(anchors.intersecting(result.anchors, boundary - 1, boundary),
                         result.anchors[:1])
        self.assertEqual(anchors.intersecting(result.anchors, boundary, boundary + 1),
                         result.anchors[1:])
        self.assertEqual(anchors.intersecting(result.anchors, boundary, boundary), ())

    def test_real_no_marker_has_no_anchors_or_pgmap_counts(self) -> None:
        parsed, segmented = self.parsed_sections("plain_text", "A fictitious one-page order.")
        self.assertEqual(self.check(parsed, segmented), anchors.Anchoring((), 0, 0))

    def test_real_footnotes_have_own_pages_and_body_reopens_for_dissent(self) -> None:
        raw = (
            '<case><opinion type="majority"><p>Body<page-number>*8</page-number>more</p>'
            '<footnote label="1"><p>Unpaged note.</p></footnote>'
            '<footnote label="2"><p>Intro<page-number>*9</page-number>Ending</p></footnote>'
            '</opinion><opinion type="dissent"><p>Dissent text.</p></opinion></case>'
        )
        parsed, segmented = self.parsed_sections("xml_harvard", raw, opinion_type="010combined")
        self.assertIn("footnote", [section.section_type for section in segmented])
        self.assertIn("dissent", [section.section_type for section in segmented])
        result = self.check(parsed, segmented)
        body_page = [row for row in result.anchors if row.label == "8"]
        footnote_page = [row for row in result.anchors if row.label == "9"]
        self.assertEqual(len(body_page), 2)
        self.assertEqual(len(footnote_page), 1)
        self.assertEqual(parsed.text[body_page[0].char_start:body_page[0].char_end], "more\n\n")
        self.assertEqual(parsed.text[body_page[1].char_start:body_page[1].char_end],
                         "Dissent text.")
        self.assertEqual(parsed.text[footnote_page[0].char_start:footnote_page[0].char_end],
                         "Ending\n\n")
        for word in ("Unpaged note.", "Intro"):
            start = parsed.text.index(word)
            self.assertEqual(anchors.intersecting(result.anchors, start, start + len(word)), ())

    def test_real_marker_at_paragraph_end_and_deferred_to_footnote_start(self) -> None:
        raw = (
            '<opinion><p>Alpha<page-number>*8</page-number></p>'
            '<footnote label="1"><p><page-number>*9</page-number>Foot</p></footnote>'
            '<p>Beta</p></opinion>'
        )
        parsed, segmented = self.parsed_sections("xml_harvard", raw)
        result = self.check(parsed, segmented)
        self.assertEqual(parsed.markers[0].offset, len("Alpha"))
        self.assertEqual(parsed.markers[1].offset, parsed.text.index("Foot"))
        self.assertEqual([(row.label, parsed.text[row.char_start:row.char_end])
                          for row in result.anchors], [
            ("8", "\n\n"), ("9", "Foot\n\n"), ("8", "Beta"),
        ])

    def test_real_same_offset_turn_discards_zero_length_page(self) -> None:
        parsed, segmented = self.parsed_sections(
            "xml_harvard", '<opinion><p>A<page-number>*8</page-number>'
            '<page-number>*9</page-number>B</p></opinion>',
        )
        result = self.check(parsed, segmented)
        self.assertEqual(result.anchors, (
            anchors.Anchor("reporter_page", "9", 1, 2, (("scheme", "1"),)),
        ))

    def test_real_parallel_stars_tile_independently_and_intersect_together(self) -> None:
        parsed, segmented = self.parsed_sections(
            "xml_harvard", '<opinion><p>One<page-number>*8</page-number>Two'
            '<page-number>**72</page-number>Three</p></opinion>',
        )
        result = self.check(parsed, segmented)
        self.assertEqual(result.anchors, (
            anchors.Anchor("reporter_page", "8", len("One"), len(parsed.text),
                           (("scheme", "1"),)),
            anchors.Anchor("reporter_page", "72", len("OneTwo"), len(parsed.text),
                           (("stars", "2"), ("scheme", "2"))),
        ))
        self.assertEqual(anchors.intersecting(result.anchors, parsed.text.index("Three"),
                                               len(parsed.text)), result.anchors)

    def test_real_pagescheme_and_citation_index_are_source_scheme_keys(self) -> None:
        for column, raw, expected_scheme, expected_attrs in (
            (
                "html_anon_2020",
                '<div class="opinion"><p>A<span class="star-pagination" number="23" '
                'pagescheme="Fiction">*242</span>B</p></div>',
                "Fiction", (("number", "23"), ("pagescheme", "Fiction"),
                            ("scheme", "Fiction")),
            ),
            (
                "xml_harvard",
                '<opinion><p>A<page-number citation-index="2">*72</page-number>B</p></opinion>',
                "2", (("citation_index", "2"), ("scheme", "2")),
            ),
        ):
            with self.subTest(column=column):
                parsed, segmented = self.parsed_sections(column, raw)
                result = self.check(parsed, segmented)
                self.assertEqual(result.anchors[0].attrs, expected_attrs)
                self.assertEqual(dict(result.anchors[0].attrs)["scheme"], expected_scheme)

    def test_real_text_end_marker_opens_no_row(self) -> None:
        parsed, segmented = self.parsed_sections(
            "xml_harvard", '<opinion><p>Word<page-number>*9</page-number></p></opinion>',
        )
        self.assertEqual(parsed.markers[0].offset, len(parsed.text))
        self.assertEqual(self.check(parsed, segmented).anchors, ())

    def test_real_pgmap_strict_containment_and_cross_check(self) -> None:
        raw = (
            '<opinion><p pgmap="1 2">'
            '<page-number citation-index="1">*7</page-number>A'
            '<page-number citation-index="1">*8</page-number>B'
            '</p><p pgmap="3 4">C<page-number citation-index="1">*9</page-number>D'
            '</p></opinion>'
        )
        parsed, segmented = self.parsed_sections("xml_harvard", raw)
        agreeing = self.check(parsed, segmented)
        self.assertEqual((agreeing.pgmap_checked, agreeing.pgmap_disagreeing), (2, 0))
        self.assertNotIn("pgmap", dict(agreeing.anchors[0].attrs))
        self.assertEqual(dict(agreeing.anchors[1].attrs)["pgmap"], "1 2")
        self.assertEqual(dict(agreeing.anchors[2].attrs)["pgmap"], "3 4")

        changed = opiniontext.canonical_text("xml_harvard", raw.replace('pgmap="1 2"', 'pgmap="1 2 3"'))
        disagreeing = self.check(changed, segmented)
        self.assertEqual((disagreeing.pgmap_checked, disagreeing.pgmap_disagreeing), (2, 1))
        self.assertEqual(
            [(row.label, row.char_start, row.char_end) for row in agreeing.anchors],
            [(row.label, row.char_start, row.char_end) for row in disagreeing.anchors],
        )

    def test_hand_built_marker_at_footnote_boundary_belongs_to_footnote(self) -> None:
        # A marker at an exact section boundary is easiest to state in canonical offsets.
        parsed = opiniontext.Parsed("BodyFootTail", (
            opiniontext.Marker("page", "8", 2),
            opiniontext.Marker("page", "9", 4),
        ), ())
        segmented = (
            sections.Section("majority", "row", 0, 4, None, None, None),
            sections.Section("footnote", "markup", 4, 8, None, None, None),
            sections.Section("dissent", "row", 8, 12, None, None, None),
        )
        result = self.check(parsed, segmented)
        self.assertEqual([(row.label, row.char_start, row.char_end) for row in result.anchors],
                         [("8", 2, 4), ("9", 4, 8), ("8", 8, 12)])


if __name__ == "__main__":
    unittest.main()
