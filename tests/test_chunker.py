"""Pure chunk packing over fictitious text and deterministic counters."""

import hashlib
import re
import unittest
from dataclasses import dataclass, replace

from gideon.worker import opiniontext, sections
from gideon.worker.chunker import NewChunk, chunk
from gideon.worker.pipeline import PIPELINE_VERSION, PIPELINES, Pipeline


@dataclass(frozen=True)
class Section:
    section_id: str
    section_type: str
    char_start: int
    char_end: int


_TOKEN = re.compile(r"\S+|\s+")


def words_and_separators(text: str) -> int:
    """Count each visible word and each whitespace run once."""

    return len(_TOKEN.findall(text))


def sizes(target: int, maximum: int, minimum: int = 1) -> Pipeline:
    return replace(
        PIPELINES[PIPELINE_VERSION], chunk_target=target,
        chunk_maximum=maximum, chunk_minimum=minimum,
    )


def one_section(text: str) -> tuple[Section, ...]:
    return (Section("section-one", "majority", 0, len(text)),)


class Chunking(unittest.TestCase):
    def check_tiles(self, text: str, rows: tuple[NewChunk, ...], sections: tuple[Section, ...]) -> None:
        self.assertEqual([row.ordinal for row in rows], list(range(len(rows))))
        for section in sections:
            section_rows = [row for row in rows if row.section_id == section.section_id]
            if not section_rows:
                self.assertFalse(text[section.char_start:section.char_end].strip())
                continue
            self.assertEqual(section_rows[0].char_start, section.char_start)
            self.assertEqual(section_rows[-1].char_end, section.char_end)
            self.assertEqual(
                [row.char_end for row in section_rows[:-1]],
                [row.char_start for row in section_rows[1:]],
            )
            for row in section_rows:
                self.assertLess(row.char_start, row.char_end)
                self.assertEqual(row.section_type, section.section_type)
                self.assertEqual(
                    row.content_hash,
                    hashlib.sha256(text[row.char_start:row.char_end].encode("utf-8")).hexdigest(),
                )

    def test_sections_tile_with_prefixes_separators_and_document_ordinals(self) -> None:
        text = "  Alpha beta.\n\nGamma delta.\n\nDissent one."
        split = text.index("Dissent")
        sections = (
            Section("majority", "majority", 0, split),
            Section("dissent", "dissent", split, len(text)),
        )
        pipeline = sizes(4, 8)
        rows = chunk("document", pipeline, text, sections, (), words_and_separators)
        self.assertGreaterEqual(len(rows), 3)
        self.check_tiles(text, rows, sections)
        self.assertEqual(rows[0].char_start, 0)
        self.assertTrue(text[rows[0].char_start:rows[0].char_end].startswith("  "))
        self.assertEqual([row.token_count for row in rows], [
            words_and_separators(text[row.char_start:row.char_end]) for row in rows
        ])
        self.assertTrue(all(row.token_count <= pipeline.chunk_maximum for row in rows))

    def test_ids_bind_document_pipeline_and_offsets_and_repeat(self) -> None:
        text = "Alpha beta.\n\nGamma delta."
        pipeline = sizes(3, 8)
        sections = one_section(text)
        rows = chunk("document", pipeline, text, sections, (), words_and_separators)
        self.assertEqual(rows, chunk("document", pipeline, text, sections, (), words_and_separators))
        first = rows[0]
        identity = f"document\n{pipeline.version}\n{first.char_start}\n{first.char_end}"
        self.assertEqual(first.chunk_id, hashlib.sha256(identity.encode()).hexdigest())
        other_document = chunk("other-document", pipeline, text, sections, (), words_and_separators)
        other_pipeline = chunk("document", replace(pipeline, version="9.9.9"), text,
                               sections, (), words_and_separators)
        self.assertEqual([row.char_start for row in rows], [row.char_start for row in other_document])
        self.assertEqual([row.char_end for row in rows], [row.char_end for row in other_pipeline])
        self.assertNotEqual(rows[0].chunk_id, other_document[0].chunk_id)
        self.assertNotEqual(rows[0].chunk_id, other_pipeline[0].chunk_id)

    def test_sentence_boundaries_split_a_long_paragraph(self) -> None:
        text = 'One two. Three four! "Five six?" Seven eight.'
        pipeline = sizes(4, 6)
        rows = chunk("document", pipeline, text, one_section(text), (), words_and_separators)
        self.check_tiles(text, rows, one_section(text))
        self.assertEqual([row.char_start for row in rows[1:]], [
            text.index("Three"), text.index('"Five'), text.index("Seven"),
        ])
        self.assertTrue(all(row.token_count <= pipeline.chunk_maximum for row in rows))

    def test_a_long_paragraph_packs_its_sentences_to_the_target(self) -> None:
        text = "One two. " * 5 + "One two."
        pipeline = sizes(8, 20)
        rows = chunk("document", pipeline, text, one_section(text), (), words_and_separators)
        self.check_tiles(text, rows, one_section(text))
        self.assertEqual([row.token_count for row in rows], [8, 8, 7])

    def test_sentence_too_long_splits_at_whitespace(self) -> None:
        text = "alpha beta gamma delta epsilon"
        pipeline = sizes(3, 4)
        rows = chunk("document", pipeline, text, one_section(text), (), words_and_separators)
        self.check_tiles(text, rows, one_section(text))
        self.assertGreater(len(rows), 1)
        self.assertTrue(all(row.token_count <= pipeline.chunk_maximum for row in rows))
        self.assertTrue(all(text[row.char_start].isalpha() for row in rows))

    def test_whitespace_free_run_splits_at_code_points(self) -> None:
        text = "abcdefghij"
        pipeline = sizes(2, 3)
        rows = chunk("document", pipeline, text, one_section(text), (), len)
        self.check_tiles(text, rows, one_section(text))
        self.assertEqual([row.token_count for row in rows], [3, 3, 3, 1])

    def test_citation_across_paragraph_boundary_fuses_units(self) -> None:
        text = "Alpha cites Case 123.\n\nId. Beta follows.\n\nLast paragraph."
        citation = (text.index("Case"), text.index("Id.") + len("Id."))
        pipeline = sizes(6, 12)
        rows = chunk("document", pipeline, text, one_section(text), (citation,), words_and_separators)
        self.check_tiles(text, rows, one_section(text))
        self.assertTrue(all(not (citation[0] < edge < citation[1])
                            for row in rows for edge in (row.char_start, row.char_end)))
        plain = chunk("document", pipeline, text, one_section(text), (), words_and_separators)
        self.assertTrue(any(citation[0] < row.char_end < citation[1] for row in plain))
        self.assertTrue(all(row.token_count <= pipeline.chunk_maximum for row in rows))

    def test_fused_overlong_paragraphs_resplit_without_cutting_citation(self) -> None:
        text = "a a a a a Case 123.\n\nId. B b b b b b. C c c."
        citation = (text.index("Case"), text.index("Id.") + len("Id."))
        pipeline = sizes(6, 9)
        rows = chunk("document", pipeline, text, one_section(text), (citation,), words_and_separators)
        self.check_tiles(text, rows, one_section(text))
        self.assertGreater(len(rows), 1)
        self.assertTrue(all(not (citation[0] < edge < citation[1])
                            for row in rows for edge in (row.char_start, row.char_end)))
        self.assertIn(text.index("B b"), [row.char_start for row in rows])
        self.assertTrue(all(row.token_count <= pipeline.chunk_maximum for row in rows))

    def test_sentence_boundary_inside_citation_is_illegal(self) -> None:
        text = "Intro text. Alpha v. Beta. Next sentence. Final sentence."
        citation = (text.index("Alpha"), text.index("Beta.") + len("Beta."))
        pipeline = sizes(4, 8)
        rows = chunk("document", pipeline, text, one_section(text), (citation,), words_and_separators)
        self.check_tiles(text, rows, one_section(text))
        self.assertTrue(all(not (citation[0] < edge < citation[1])
                            for row in rows for edge in (row.char_start, row.char_end)))

    def test_single_citation_over_maximum_refuses(self) -> None:
        text = "abcdefghij"
        pipeline = sizes(2, 3)
        with self.assertRaises(ValueError):
            chunk("document", pipeline, text, one_section(text), ((0, len(text)),), len)

    def test_separator_tips_a_unit_over_maximum_before_packing(self) -> None:
        text = "alpha beta\n\ngamma"
        pipeline = sizes(2, 3)
        rows = chunk("document", pipeline, text, one_section(text), (), words_and_separators)
        self.check_tiles(text, rows, one_section(text))
        self.assertGreater(len(rows), 1)
        self.assertTrue(all(row.token_count <= pipeline.chunk_maximum for row in rows))

    def test_trailing_fragment_merges_and_lone_short_section_stays(self) -> None:
        text = "alpha beta\n\ngamma"
        pipeline = sizes(3, 7, 3)
        rows = chunk("document", pipeline, text, one_section(text), (), words_and_separators)
        self.assertEqual(len(rows), 1)
        self.check_tiles(text, rows, one_section(text))
        short = chunk("document", pipeline, "tiny", one_section("tiny"), (), words_and_separators)
        self.assertEqual(len(short), 1)
        self.assertLess(short[0].token_count, pipeline.chunk_minimum)

    def test_minimum_packing_takes_next_unit_past_target_within_maximum(self) -> None:
        text = "A\n\nB"
        pipeline = sizes(2, 4, 3)
        rows = chunk("document", pipeline, text, one_section(text), (), words_and_separators)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].token_count, words_and_separators(text))

    def test_recount_gives_back_when_join_costs_extra(self) -> None:
        text = "alpha\n\nbeta"
        pipeline = sizes(4, 4)

        def joined_counter(value: str) -> int:
            return words_and_separators(value) + (3 if "\n\n" in value and value.endswith("beta") else 0)

        rows = chunk("document", pipeline, text, one_section(text), (), joined_counter)
        self.check_tiles(text, rows, one_section(text))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].char_start, text.index("beta"))
        self.assertEqual([row.token_count for row in rows], [2, 1])

    def test_a_nonblank_segmented_text_always_yields_a_chunk(self) -> None:
        for raw in ("x", "  Indented.\n\n", "One.\n\n\n\nTwo.", "é́"):
            parsed = opiniontext.canonical_text("plain_text", raw)
            rows = tuple(
                Section(f"section-{index}", row.section_type, row.char_start, row.char_end)
                for index, row in enumerate(sections.segment(
                    parsed, opinion_type="010combined", per_curiam=False, column="plain_text",
                ))
            )
            with self.subTest(raw=raw):
                chunks = chunk("document", sizes(512, 1024, 64), parsed.text, rows, (),
                               words_and_separators)
                self.assertTrue(chunks)
                self.check_tiles(parsed.text, chunks, rows)

    def test_whitespace_only_section_yields_no_row(self) -> None:
        text = "  \n\nAlpha."
        sections = (Section("blank", "headmatter", 0, 4),
                    Section("body", "majority", 4, len(text)))
        rows = chunk("document", sizes(3, 6), text, sections, (), words_and_separators)
        self.assertEqual([row.section_id for row in rows], ["body"])
        self.check_tiles(text, rows, sections)
        self.assertEqual(chunk("document", sizes(3, 6), " \n\n", one_section(" \n\n"),
                               (), words_and_separators), ())


if __name__ == "__main__":
    unittest.main()
