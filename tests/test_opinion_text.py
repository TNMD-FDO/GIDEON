"""Canonical opinion text over fictitious examples of each source column."""

import unittest

from gideon.worker import opiniontext


class OpinionText(unittest.TestCase):
    def assert_parsed(
        self, column: str, raw: str, expected: str,
        markers: tuple[opiniontext.Marker, ...],
    ) -> None:
        parsed = opiniontext.canonical_text(column, raw)
        self.assertEqual(parsed.text, expected)
        self.assertEqual(parsed.markers, markers)

    def test_source_order_skips_blank_values_and_derived_markup(self) -> None:
        self.assertEqual(opiniontext.TEXT_SOURCES, (
            "xml_harvard", "html_columbia", "html_lawbox", "html_anon_2020",
            "html", "plain_text",
        ))
        row = {column: f"<{column}>" for column in opiniontext.TEXT_SOURCES}
        row["html_with_citations"] = "<p>derived text</p>"
        for chosen in opiniontext.TEXT_SOURCES:
            with self.subTest(chosen=chosen):
                self.assertEqual(opiniontext.choose(row), (chosen, row[chosen]))
                row[chosen] = " \t\n "
        self.assertIsNone(opiniontext.choose(row))
        self.assertIsNone(opiniontext.choose({"html_with_citations": "derived only"}))
        self.assertEqual(opiniontext.choose({"xml_harvard": " ", "html": "  text  "}),
                         ("html", "  text  "))

    def test_raw_harvard_opinion_keeps_body_and_drops_three_marker_kinds(self) -> None:
        raw = (
            "<opinion><author>Judge É</author><p>record"
            '<page-number label="999">*349</page-number>criminal'
            "<footnotemark>1</footnotemark> won."
            "<bracketnum>2. catchline</bracketnum></p>"
            '<footnote label="1"><p>Note body.</p></footnote></opinion>'
        )
        expected = "Judge É\n\nrecordcriminal won.\n\nNote body."
        self.assert_parsed("xml_harvard", raw, expected, (
            opiniontext.Marker("page", "349", expected.index("record") + len("record")),
            opiniontext.Marker("footnote-mark", "1", expected.index("recordcriminal") + len("recordcriminal")),
            opiniontext.Marker("editorial", "2. catchline", expected.index(" won.") + len(" won.")),
        ))

    def test_rewritten_harvard_prolog_padded_page_and_backlink(self) -> None:
        raw = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<opinion><p>One<span class="star-pagination" label="7"> *8 </span>Two'
            '<a class="page-label" data-label="70" href="#p70">**72</a>Three'
            '<a class="footnote" href="#fn">1</a>.</p>'
            '<div class="footnotes"><div class="footnote" label="1">'
            '<a class="footnote" href="#back">↩</a><p>Note.</p></div></div></opinion>'
        )
        expected = "OneTwoThree.\n\nNote."
        self.assert_parsed("xml_harvard", raw, expected, (
            opiniontext.Marker("page", "8", len("One")),
            opiniontext.Marker("page", "72", len("OneTwo")),
            opiniontext.Marker("footnote-mark", "1", len("OneTwoThree")),
            opiniontext.Marker("footnote-mark", "↩", expected.index("Note.")),
        ))

    def test_columbia_blocks_heading_page_and_footnote_body(self) -> None:
        raw = (
            "<div><p>We <strong>{¶ 8}</strong> hold.</p>"
            "<p><center>State heading</center></p>"
            '<p>Then<span class="star-pagination">*Page 142</span> proceed'
            '<sup><a href="#fn1">1</a></sup>.</p>'
            '<div class="footnote"><sup><a href="#back">↩</a></sup>'
            '<p>Foot body.</p></div></div>'
        )
        expected = "We {¶ 8} hold.\n\nState heading\n\nThen proceed.\n\nFoot body."
        self.assert_parsed("html_columbia", raw, expected, (
            opiniontext.Marker("page", "142", expected.index("Then") + len("Then")),
            opiniontext.Marker("footnote-mark", "1", expected.index("proceed") + len("proceed")),
            opiniontext.Marker("footnote-mark", "↩", expected.index("Foot body.")),
        ))

    def test_lawbox_header_pre_and_notes_remain_in_order(self) -> None:
        raw = (
            "<div><center>10 Example 20</center><center>"
            "<h1>Fiction v. Example<br>Appeal</h1></center></div>"
            '<p>We<span class="star-pagination">*837</span>agree<sup>[1]</sup>.</p>'
            "<h2>NOTES</h2><p>[1] Note text.</p>"
            "<pre>  col  one\n  two  three  </pre>"
        )
        expected = (
            "10 Example 20\n\nFiction v. Example\nAppeal\n\nWeagree."
            "\n\nNOTES\n\n[1] Note text.\n\n  col  one\n  two  three  "
        )
        self.assert_parsed("html_lawbox", raw, expected, (
            opiniontext.Marker("page", "837", expected.index("We") + len("We")),
            opiniontext.Marker("footnote-mark", "[1]", expected.index("Weagree") + len("Weagree")),
        ))

    def test_anonymous_html_uses_printed_page_instead_of_scheme_number(self) -> None:
        raw = (
            '<div class="courtcasedochead"><casename>Fiction Name</casename></div>'
            '<div class="opinion"><bodytext><p>We'
            '<span class="star-pagination" number="23" pagescheme="Fiction">*242 </span>'
            '<span normalizedcite="Example"><content>hold</content></span>'
            '<sup><a href="#fn">1</a></sup>.</p></bodytext></div>'
            '<div class="footnotes"><h4>Footnotes</h4><ul><li><div id="fn">'
            '<bodytext><p>1. Note<a href="#back">↩</a> continues.</p></bodytext>'
            "</div></li></ul></div>"
        )
        expected = "Fiction Name\n\nWehold.\n\nFootnotes\n\n1. Note continues."
        self.assert_parsed("html_anon_2020", raw, expected, (
            opiniontext.Marker("page", "242", expected.index("Wehold") + len("We")),
            opiniontext.Marker("footnote-mark", "1", expected.index("Wehold") + len("Wehold")),
            opiniontext.Marker("footnote-mark", "↩", expected.index("1. Note") + len("1. Note")),
        ))

    def test_generic_html_drops_navigation_and_keeps_word_footnote_body(self) -> None:
        raw = (
            "<html><body><nav>Navigation only</nav><p>Begin"
            '<a class="footnote" href="#fn">1</a><span class="num">6</span>'
            ' end<span class="ldml-pagenumber">*45</span>.</p>'
            '<div class="footnotes"><div class="footnote">'
            '<a href="#back">↩</a><p>Foot.</p></div></div>'
            '<p class="MsoFootnoteText">Word'
            '<span class="MsoFootnoteReference">2</span> note.</p>'
            '<img src="data:image/png;base64,fiction"><script>skip</script>'
            "<style>skip</style></body></html>"
        )
        expected = "Begin end.\n\nFoot.\n\nWord note."
        self.assert_parsed("html", raw, expected, (
            opiniontext.Marker("footnote-mark", "1", len("Begin")),
            opiniontext.Marker("editorial", "6", len("Begin")),
            opiniontext.Marker("page", "45", expected.index(" end") + len(" end")),
            opiniontext.Marker("footnote-mark", "↩", expected.index("Foot.")),
            opiniontext.Marker("footnote-mark", "2", expected.index("Word") + len("Word")),
        ))

    def test_plain_text_changes_only_line_endings_form_feed_and_nfc(self) -> None:
        raw = "  HEAD\r\ncafe\u0301\f*12\rend  "
        self.assert_parsed("plain_text", raw, "  HEAD\ncafé\n*12\nend  ", ())

    def test_pretty_printed_inline_xml_matches_compact_xml(self) -> None:
        compact = "<opinion><p>we <em>affirm</em> today</p></opinion>"
        pretty = "<opinion><p>we\n  <em>\n   affirm</em> today</p></opinion>"
        self.assert_parsed("xml_harvard", compact, "we affirm today", ())
        self.assert_parsed("xml_harvard", pretty, "we affirm today", ())

    def test_glued_page_marker_offset_follows_nfc_prefix(self) -> None:
        raw = (
            "<opinion><p>cafe\u0301<page-number label=\"3\">*9</page-number>"
            "teria</p></opinion>"
        )
        self.assert_parsed("xml_harvard", raw, "caféteria", (
            opiniontext.Marker("page", "9", len("café")),
        ))

    def test_footnotereference_label_is_dropped_without_losing_its_tail(self) -> None:
        raw = (
            "<div><p>Word<footnotereference anchoridref='fn'>"
            "<label>1</label></footnotereference> follows.</p></div>"
        )
        self.assert_parsed("html_anon_2020", raw, "Word follows.", (
            opiniontext.Marker("footnote-mark", "1", len("Word")),
        ))

    def test_unknown_elements_remain_text_but_comments_and_processing_do_not(self) -> None:
        raw = (
            "<opinion><p>Start<!--comment--><?ignore hidden?>"
            "<unknown><emphasis> kept</emphasis></unknown> end</p></opinion>"
        )
        self.assert_parsed("xml_harvard", raw, "Start kept end", ())

    def test_empty_and_unparseable_have_separate_reasons(self) -> None:
        for column, raw, reason in (
            ("plain_text", " \r\n\f ", "empty"),
            ("xml_harvard", "<opinion><page-number>*1</page-number></opinion>", "empty"),
            ("html", "<p><script>only script</script></p>", "empty"),
            ("xml_harvard", "<", "unparseable"),
        ):
            with self.subTest(column=column, raw=raw):
                with self.assertRaises(opiniontext.OpinionTextFailure) as raised:
                    opiniontext.canonical_text(column, raw)
                self.assertEqual(raised.exception.reason, reason)


if __name__ == "__main__":
    unittest.main()
