"""Canonical opinion text over fictitious examples of each source column."""

import re
import unicodedata
import unittest
from string import ascii_lowercase, ascii_uppercase, digits

from gideon.worker import opiniontext

FICTIONAL_OPINION = (
    "The defendant in this fictional appeal asks us to set aside an order about "
    "a search of a locked cabinet in the town library. The officer had a warrant "
    "for the front desk, but the cabinet was in another room and belonged to "
    "a visitor. No witness said that the visitor had given permission to open "
    "it. The town argues that the key was on the desk and therefore the cabinet "
    "was within the warrant. We disagree because the location of a key does "
    "not change the place that a warrant describes. The photograph marked "
    "exhibit7 shows the separate room beside the library cafe\u0301. It does not "
    "show any connection between the cabinet and the desk. We return the "
    "matter to the fictional trial court so that it may consider whether "
    "the remaining evidence supports the charge without the papers taken "
    "from the cabinet. The parties may address that question on remand."
)

FICTIONAL_ORDERS = "\n".join(
    f"Docket 24-{1000 + index}. Elara Finch v. Beryl Vale. Certiorari denied."
    for index in range(20)
)
FICTIONAL_CITES = "; ".join(
    f"{700 + index} Fict. App. Rep. {100 + index}, {200 + index} ({1980 + index})"
    for index in range(40)
)
FICTIONAL_HEADMATTER = (
    "Fictional Supreme Court\nElara Finch v. Beryl Vale\nOctober 12, 2099\n"
    + "\n".join(
        f"Docket 24-{1000 + index}. Rowan Lark, Senior Counsel, Petitioner. "
        "Briar Wren, Deputy Counsel, Respondent."
        for index in range(12)
    )
)


def cipher_text(text: str) -> str:
    """Model the glyph cipher observed in ca6 plain_text with a fixed code-point offset."""

    return text.translate(str.maketrans(
        ascii_lowercase + ascii_uppercase,
        "".join(chr(ord(letter) - 29) for letter in ascii_lowercase + ascii_uppercase),
        digits,
    ))


def _source(column: str, text: str) -> str:
    if column == "plain_text":
        return text
    if column == "xml_harvard":
        return f"<opinion><p>{text}</p></opinion>"
    return f"<p>{text}</p>"


class OpinionText(unittest.TestCase):
    def assert_parsed(
        self, column: str, raw: str, expected: str,
        markers: tuple[opiniontext.Marker, ...],
    ) -> opiniontext.Parsed:
        parsed = opiniontext.canonical_text(column, raw)
        self.assertEqual((parsed.text, parsed.markers), (expected, markers))
        return parsed

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
            opiniontext.Marker("page", "72", len("OneTwo"), (("stars", "2"),)),
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
        parsed = self.assert_parsed("html_lawbox", raw, expected, (
            opiniontext.Marker("page", "837", expected.index("We") + len("We")),
            opiniontext.Marker("footnote-mark", "[1]", expected.index("Weagree") + len("Weagree")),
        ))
        self.assertEqual(parsed.blocks[0], opiniontext.Block(
            "headmatter", "header", expected.index("10 Example"),
            expected.index("Appeal") + len("Appeal"),
        ))
        self.assertEqual(tuple(block for block in parsed.blocks if block.kind == "headmatter"),
                         (parsed.blocks[0],))

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
            opiniontext.Marker(
                "page", "242", expected.index("Wehold") + len("We"),
                (("number", "23"), ("pagescheme", "Fiction")),
            ),
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

    def test_page_scheme_attributes_survive_normalization_and_deferred_marker(self) -> None:
        raw = (
            '<opinion><p>cafe\u0301<page-number citation-index="1">*9</page-number>'
            'teria</p><p><page-number citation-index="2">**10</page-number>Next</p>'
            '</opinion>'
        )
        self.assert_parsed("xml_harvard", raw, "caféteria\n\nNext", (
            opiniontext.Marker("page", "9", len("café"), (("citation_index", "1"),)),
            opiniontext.Marker(
                "page", "10", len("caféteria\n\n"),
                (("citation_index", "2"), ("stars", "2")),
            ),
        ))
        for column in ("html", "xml_harvard"):
            with self.subTest(column=column):
                self.assert_parsed(
                    column,
                    '<p>One<a class="page-label" data-citation-index="2">**72</a>Two</p>',
                    "OneTwo", (opiniontext.Marker(
                        "page", "72", len("One"), (("citation_index", "2"), ("stars", "2")),
                    ),),
                )

    def test_harvard_pagemap_block_keeps_structural_blocks_and_text(self) -> None:
        raw = (
            '<opinion><author pgmap="1 2">Judge <page-number>*2</page-number>A</author>'
            '<blockquote pgmap="3 4"><p>Quote</p></blockquote>'
            '<p pgmap="5 6">Body</p></opinion>'
        )
        parsed = self.assert_parsed("xml_harvard", raw, "Judge A\n\nQuote\n\nBody", (
            opiniontext.Marker("page", "2", len("Judge ")),
        ))
        self.assertEqual(parsed.blocks, (
            opiniontext.Block("opinion", "", 0, len(parsed.text)),
            opiniontext.Block("author", "", 0, len("Judge A")),
            opiniontext.Block("pagemap", "1 2", 0, len("Judge A")),
            opiniontext.Block("quote", "", parsed.text.index("Quote"),
                              parsed.text.index("Quote") + len("Quote")),
            opiniontext.Block("pagemap", "3 4", parsed.text.index("Quote"),
                              parsed.text.index("Quote") + len("Quote")),
            opiniontext.Block("pagemap", "5 6", parsed.text.index("Body"),
                              parsed.text.index("Body") + len("Body")),
        ))
        without_raw = raw.replace(' pgmap="1 2"', '').replace(' pgmap="3 4"', '')
        without_raw = without_raw.replace(' pgmap="5 6"', '')
        without = opiniontext.canonical_text("xml_harvard", without_raw)
        self.assertEqual((without.text, without.markers), (parsed.text, parsed.markers))
        self.assertEqual(
            without.blocks,
            tuple(block for block in parsed.blocks if block.kind != "pagemap"),
        )

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
            ("html", "<p> \n\t </p>", "empty"),
            ("xml_harvard", "<", "unparseable"),
            ("plain_text", cipher_text(FICTIONAL_OPINION), "illegible"),
        ):
            with self.subTest(column=column, raw=raw):
                with self.assertRaises(opiniontext.OpinionTextFailure) as raised:
                    opiniontext.canonical_text(column, raw)
                self.assertEqual(raised.exception.reason, reason)


class Legibility(unittest.TestCase):
    def assert_low_share_passes(self, text: str) -> None:
        self.assertGreaterEqual(len(re.findall(r"[A-Za-z]+", text)),
                                opiniontext.LEGIBILITY_WORD_FLOOR)
        share = opiniontext.legibility(text)
        self.assertIsNotNone(share)
        assert share is not None
        self.assertLess(share, opiniontext.LEGIBILITY_SHARE_FLOOR)
        mark = opiniontext.legibility_mark(text)
        if mark is not None:
            self.assertLess(mark, opiniontext.LEGIBILITY_MARK_FLOOR)
        for column in opiniontext.TEXT_SOURCES:
            with self.subTest(column=column):
                parsed = opiniontext.canonical_text(column, _source(column, text))
                expected = text if column == "plain_text" else " ".join(text.split())
                self.assertEqual(parsed.text, unicodedata.normalize("NFC", expected))

    def test_cipher_uses_one_code_point_offset_and_drops_original_digits(self) -> None:
        self.assertEqual(cipher_text("the The MNVW xyz 0123456789"), "WKH 7KH 019: [\\] ")

    def test_plain_opinion_passes_and_its_cipher_fails_in_every_source(self) -> None:
        cipher = cipher_text(FICTIONAL_OPINION)
        self.assertGreaterEqual(len(re.findall(r"[A-Za-z]+", cipher)),
                                opiniontext.LEGIBILITY_WORD_FLOOR)
        self.assertNotEqual(cipher, FICTIONAL_OPINION)
        self.assertRegex(cipher, r"[0-9]+")
        mark = opiniontext.legibility_mark(cipher)
        self.assertIsNotNone(mark)
        assert mark is not None
        self.assertGreaterEqual(mark, opiniontext.LEGIBILITY_MARK_FLOOR)
        cipher_share = opiniontext.legibility(cipher)
        self.assertIsNotNone(cipher_share)
        assert cipher_share is not None
        self.assertLess(cipher_share, opiniontext.LEGIBILITY_SHARE_FLOOR)
        for column in opiniontext.TEXT_SOURCES:
            with self.subTest(column=column):
                parsed = opiniontext.canonical_text(column, _source(column, FICTIONAL_OPINION))
                self.assertEqual(parsed.text, unicodedata.normalize("NFC", FICTIONAL_OPINION))
                share = opiniontext.legibility(parsed.text)
                self.assertIsNotNone(share)
                assert share is not None
                self.assertGreaterEqual(share, opiniontext.LEGIBILITY_SHARE_FLOOR)
                with self.assertRaises(opiniontext.OpinionTextFailure) as raised:
                    opiniontext.canonical_text(column, _source(column, cipher))
                self.assertEqual(raised.exception.reason, "illegible")

    def test_fictitious_lists_pass_in_every_source(self) -> None:
        for name, text in (
            ("orders", FICTIONAL_ORDERS),
            ("cites", FICTIONAL_CITES),
            ("headmatter", FICTIONAL_HEADMATTER),
        ):
            with self.subTest(name=name):
                self.assert_low_share_passes(text)

    def test_list_with_a_few_digit_before_capital_forms_passes(self) -> None:
        text = FICTIONAL_ORDERS + "\n12A345. Motion denied. 2255A. Application dismissed."
        mark = opiniontext.legibility_mark(text)
        self.assertIsNotNone(mark)
        assert mark is not None
        self.assertGreater(mark, 0)
        self.assertLess(mark, opiniontext.LEGIBILITY_MARK_FLOOR)
        self.assert_low_share_passes(text)

    def test_low_share_text_without_digits_passes(self) -> None:
        text = " ".join(["Elara Finch Beryl Vale Rowan Lark Briar Wren"] * 20)
        self.assertNotRegex(text, r"[0-9]")
        self.assertIsNone(opiniontext.legibility_mark(text))
        self.assert_low_share_passes(text)

    def test_non_english_prose_passes(self) -> None:
        paragraph = (
            "Durante esta audiencia ficticia, varios vecinos describieron una biblioteca "
            "cerrada cuyas ventanas permanecieron cubiertas durante toda aquella noche. "
            "Ninguna persona entregó permiso para registrar esos muebles privados. "
            "Nuestro tribunal devuelve este expediente porque las pruebas disponibles "
            "resultan insuficientes para sostener semejante medida judicial."
        )
        self.assert_low_share_passes(" ".join([paragraph] * 4))

    def test_mark_counts_maximal_ascii_digit_runs_and_immediate_ascii_capitals(self) -> None:
        for text, expected in (
            ("", None),
            ("Elara Finch", None),
            ("１２A ١٢A", None),
            ("123A", 1.0),
            ("123A45B", 1.0),
            ("123A 45b 67 A 89, 90É 12\nA 34", 1 / 7),
            ("12 34. 56, 78\nA", 0.0),
        ):
            with self.subTest(text=text):
                self.assertEqual(opiniontext.legibility_mark(text), expected)

    def test_verdict_fields_for_cipher_list_and_short_text(self) -> None:
        cipher = cipher_text(FICTIONAL_OPINION)
        for text, share, mark, reason in (
            (cipher, opiniontext.legibility(cipher), opiniontext.legibility_mark(cipher),
             "illegible"),
            (FICTIONAL_CITES, 0.0, 0.0, None),
            ("7KH", None, 1.0, None),
        ):
            with self.subTest(text=text):
                verdict = opiniontext.legibility_verdict(text)
                self.assertEqual((verdict.share, verdict.mark, verdict.reason),
                                 (share, mark, reason))

    def test_verdict_requires_both_signals_at_their_floors(self) -> None:
        words = ["Finch"] * opiniontext.LEGIBILITY_WORD_FLOOR
        at_mark_floor = " ".join(words + ["12A"] + ["34"] * 9)
        below_mark_floor = at_mark_floor + " 56"
        at_share_floor = " ".join(
            ["the"] * 6 + ["Finch"] * 113 + ["12B"],
        )
        self.assertEqual(opiniontext.legibility_mark(at_mark_floor),
                         opiniontext.LEGIBILITY_MARK_FLOOR)
        self.assertEqual(opiniontext.legibility(at_share_floor),
                         opiniontext.LEGIBILITY_SHARE_FLOOR)
        self.assertEqual(opiniontext.legibility_verdict(at_mark_floor).reason, "illegible")
        self.assertIsNone(opiniontext.legibility_verdict(below_mark_floor).reason)
        self.assertIsNone(opiniontext.legibility_verdict(at_share_floor).reason)

    def test_cipher_below_word_floor_is_unjudged_in_every_source(self) -> None:
        words = re.findall(r"[A-Za-z]+", cipher_text(FICTIONAL_OPINION))
        short = " ".join(words[:opiniontext.LEGIBILITY_WORD_FLOOR - 1])
        self.assertEqual(len(short.split()), opiniontext.LEGIBILITY_WORD_FLOOR - 1)
        for column in opiniontext.TEXT_SOURCES:
            with self.subTest(column=column):
                parsed = opiniontext.canonical_text(column, _source(column, short))
                self.assertEqual(parsed.text, short)
                self.assertIsNone(opiniontext.legibility(parsed.text))

    def test_capitals_heading_and_latin_pass_at_word_floor(self) -> None:
        heading = "OPINION"
        latin = "res ipsa loquitur"
        prose = FICTIONAL_OPINION.split()[:
            opiniontext.LEGIBILITY_WORD_FLOOR - len(heading.split()) - len(latin.split())
        ]
        raw = f"{heading}\n\n{' '.join(prose)} {latin}."
        self.assertEqual(len(re.findall(r"[A-Za-z]+", raw)), opiniontext.LEGIBILITY_WORD_FLOOR)
        parsed = opiniontext.canonical_text("plain_text", raw)
        share = opiniontext.legibility(parsed.text)
        self.assertIsNotNone(share)
        assert share is not None
        self.assertGreaterEqual(share, opiniontext.LEGIBILITY_SHARE_FLOOR)
        self.assertEqual(opiniontext.legibility(parsed.text.upper()), share)

    def test_walk_keeps_cipher_text_and_structure_in_every_source(self) -> None:
        cipher = cipher_text(FICTIONAL_OPINION)
        expected = unicodedata.normalize("NFC", cipher)
        for column in opiniontext.TEXT_SOURCES:
            with self.subTest(column=column):
                parsed = opiniontext.walk(column, _source(column, cipher))
                blocks = ((opiniontext.Block("opinion", "", 0, len(expected)),)
                          if column == "xml_harvard" else ())
                self.assertEqual(parsed, opiniontext.Parsed(expected, (), blocks))

    def test_walk_still_rejects_empty_and_unparseable_sources(self) -> None:
        for column, raw, reason in (
            ("plain_text", " \r\n\f ", "empty"),
            ("xml_harvard", "<opinion><p> </p></opinion>", "empty"),
            ("html", "<p> \n\t </p>", "empty"),
            ("xml_harvard", "<", "unparseable"),
        ):
            with self.subTest(column=column, reason=reason):
                with self.assertRaises(opiniontext.OpinionTextFailure) as raised:
                    opiniontext.walk(column, raw)
                self.assertEqual(raised.exception.reason, reason)


class Blocks(unittest.TestCase):
    def assert_blocks(
        self, column: str, raw: str,
        expected: tuple[tuple[opiniontext.BlockKind, str, str, str], ...],
    ) -> None:
        parsed = opiniontext.canonical_text(column, raw)
        self.assertEqual(parsed.blocks, tuple(
            opiniontext.Block(kind, label, parsed.text.index(first),
                              parsed.text.index(last) + len(last))
            for kind, label, first, last in expected
        ))

    def test_element_blocks_by_column_and_nested_spans(self) -> None:
        cases: tuple[
            tuple[str, str, tuple[tuple[opiniontext.BlockKind, str, str, str], ...]], ...
        ] = (
            (
                "xml_harvard",
                '<opinion type="MAJORITY"><judges>Panel</judges><author>Judge</author>'
                '<p>Body</p><footnote label="1"><blockquote><p>Note</p>'
                '</blockquote></footnote></opinion>',
                (("opinion", "majority", "Panel", "Note"),
                 ("headmatter", "judges", "Panel", "Panel"),
                 ("author", "", "Judge", "Judge"),
                 ("footnote", "1", "Note", "Note"),
                 ("quote", "", "Note", "Note")),
            ),
            (
                "html_columbia",
                '<div><h3>Head</h3><blockquote><p>Quote</p></blockquote>'
                '<footnote_body>Note</footnote_body></div>',
                (("heading", "", "Head", "Head"), ("quote", "", "Quote", "Quote"),
                 ("footnote", "", "Note", "Note")),
            ),
            (
                "html_lawbox",
                '<div><center><h1>Caption</h1></center></div><p>Body</p>',
                (("headmatter", "header", "Caption", "Caption"),
                 ("heading", "", "Caption", "Caption"),
                 ("heading", "", "Caption", "Caption")),
            ),
            (
                "html_anon_2020",
                '<div class="courtcasedochead">Header</div>'
                '<div class="opinion" opiniontype="dissent">'
                '<div class="caseopinionby">Judge</div><p>Body</p></div>'
                '<div class="footnotes"><li><p>Note</p></li></div>',
                (("headmatter", "", "Header", "Header"),
                 ("opinion", "dissent", "Judge", "Body"),
                 ("author", "", "Judge", "Judge"),
                 ("footnote", "", "Note", "Note")),
            ),
            (
                "html",
                '<div class="prelims">Header</div><center>Heading</center>'
                '<blockquote><p>Quote</p></blockquote>'
                '<p class="MsoFootnoteText">Note</p>',
                (("headmatter", "", "Header", "Header"),
                 ("heading", "", "Heading", "Heading"),
                 ("quote", "", "Quote", "Quote"),
                 ("footnote", "", "Note", "Note")),
            ),
            ("plain_text", "Header\n\nBody", ()),
        )
        for column, raw, expected in cases:
            with self.subTest(column=column):
                self.assert_blocks(column, raw, expected)

    def test_empty_block_and_dropped_locator_before_nfc_adjusted_start(self) -> None:
        raw = (
            '<div><h3></h3><p>cafe\u0301'
            '<span class="star-pagination">*Page 5</span></p>'
            '<blockquote><p>Quoted</p></blockquote></div>'
        )
        parsed = opiniontext.canonical_text("html_columbia", raw)
        self.assertEqual(parsed.text, "café\n\nQuoted")
        self.assertEqual(parsed.markers, (opiniontext.Marker("page", "5", len("café")),))
        self.assertEqual(parsed.blocks, (
            opiniontext.Block("quote", "", parsed.text.index("Quoted"),
                              parsed.text.index("Quoted") + len("Quoted")),
        ))

    def test_root_div_is_not_a_lawbox_header(self) -> None:
        self.assert_blocks(
            "html_lawbox", "<div><h1>Caption</h1><p>Body</p></div>",
            (("heading", "", "Caption", "Caption"),),
        )

    def test_inline_headmatter_uses_its_own_text_boundaries(self) -> None:
        self.assert_blocks(
            "html_anon_2020", "<p>A <counselor>Counsel</counselor> B</p>",
            (("headmatter", "", "Counsel", "Counsel"),),
        )

    def test_other_element_table_entries(self) -> None:
        cases: tuple[
            tuple[str, str, tuple[tuple[opiniontext.BlockKind, str, str, str], ...]], ...
        ] = (
            (
                "xml_harvard",
                '<opinion><attorneys>Counsel</attorneys><div class="footnotes">'
                '<div class="footnote" label="2"><p>Note</p></div></div></opinion>',
                (("opinion", "", "Counsel", "Note"),
                 ("headmatter", "attorneys", "Counsel", "Counsel"),
                 ("footnote", "2", "Note", "Note")),
            ),
            (
                "html_columbia",
                '<div><center>Head</center><div class="footnote">Note</div></div>',
                (("heading", "", "Head", "Head"),
                 ("footnote", "", "Note", "Note")),
            ),
            (
                "html_anon_2020",
                '<div class="judges">Panel</div><counselor>Counsel</counselor>'
                '<h>Head</h><excerpt><p>Quote</p></excerpt>',
                (("headmatter", "", "Panel", "Panel"),
                 ("headmatter", "", "Counsel", "Counsel"),
                 ("heading", "", "Head", "Head"),
                 ("quote", "", "Quote", "Quote")),
            ),
            (
                "html",
                '<p class="case_cite">Citation</p><p class="date">Date</p>'
                '<div class="footnote"><p>Note</p></div>',
                (("headmatter", "", "Citation", "Citation"),
                 ("headmatter", "", "Date", "Date"),
                 ("footnote", "", "Note", "Note")),
            ),
        )
        for column, raw, expected in cases:
            with self.subTest(column=column):
                self.assert_blocks(column, raw, expected)


if __name__ == "__main__":
    unittest.main()
