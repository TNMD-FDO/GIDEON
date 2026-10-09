"""Pure citation edge construction and the image-only adapter integration."""

import hashlib
import importlib
import unittest
from dataclasses import dataclass

from gideon.casecite.found import EDGE_PATTERN_ID, CiteForm, CiteKind, FoundCitation
from gideon.extraction.contract import (
    KEYED_TYPES,
    OBJECT_TYPES,
    ExactObject,
    ObjectType,
    combine_extractions,
)
from gideon.worker.citations import CITE_TYPES, NewCitation, edges

FIXTURE_TEXT = (
    "[FICTIONAL TEST OPINION] 18 U.S.C. § 922(g)(1). "
    "Id. § 924(e)(1). 466 U.S. 668. Id."
)
STATUTE_KEY = "/us/usc/t18/s922"
GRAMMAR_PATTERN = "fictional-grammar@1"
DOC_ID = "fictional-document"


@dataclass(frozen=True)
class Section:
    section_id: str
    section_type: str
    char_start: int
    char_end: int


def whole(text: str, section_type: str = "majority") -> tuple[Section, ...]:
    return (Section("fictional-section", section_type, 0, len(text)),)


def object_at(
    text: str, fragment: str, kind: ObjectType, key: str | None = None,
) -> ExactObject:
    start = text.index(fragment)
    return ExactObject(kind, start, start + len(fragment), fragment, key, GRAMMAR_PATTERN)


def found_at(
    text: str, fragment: str, form: CiteForm, kind: CiteKind, resource: int | None = None,
    *, first_page: str | None = None, reporter_cite: str | None = None,
    pincite: str | None = None, occurrence: int = 0,
) -> FoundCitation:
    start = text.index(fragment)
    for _ in range(occurrence):
        start = text.index(fragment, start + len(fragment))
    volume = reporter = None
    if first_page is not None and reporter_cite is not None:
        volume, reporter, _ = reporter_cite.split(maxsplit=2)
    return FoundCitation(
        start, start + len(fragment), form, kind, resource, volume, reporter,
        first_page, reporter_cite, pincite,
    )


class Builder(unittest.TestCase):
    def test_closed_types_and_every_grammar_type(self) -> None:
        grammar_types = set(OBJECT_TYPES) - {"docket", "caption", "case_cite"}
        self.assertEqual(set(CITE_TYPES), grammar_types | {
            "case_cite", "law_cite", "journal_cite", "unknown",
        })
        fragments = tuple(f"object{i}" for i in range(len(OBJECT_TYPES)))
        text = " ".join(fragments)
        objects = tuple(
            object_at(
                text, fragment, kind,
                f"fictional-key/{kind}" if kind in KEYED_TYPES else None,
            )
            for fragment, kind in zip(fragments, OBJECT_TYPES, strict=True)
        )
        rows = edges(DOC_ID, text, whole(text), (), objects, lambda _key: None)
        self.assertEqual(tuple(row.cite_type for row in rows), tuple(
            kind for kind in OBJECT_TYPES if kind in grammar_types
        ))
        for row in rows:
            self.assertEqual(row.cite_form, "full")
            self.assertEqual(row.pattern_id, GRAMMAR_PATTERN)
            self.assertEqual(row.key is not None, row.cite_type in KEYED_TYPES)

    def test_overlap_restates_contract_precedence(self) -> None:
        cases = (
            ((10, 20), ((12, 17), (8, 22), (18, 25), (10, 15), (20, 23))),
            (None, ((2, 7), (5, 9), (10, 13), (2, 10))),
            ((20, 29), ((30, 38), (0, 8))),
        )
        text = "x" * 40
        for first_span, second_spans in cases:
            with self.subTest(first=first_span, second=second_spans):
                grammar = () if first_span is None else (
                    ExactObject("statute", *first_span, text[slice(*first_span)],
                                STATUTE_KEY, GRAMMAR_PATTERN),
                )
                found = tuple(
                    FoundCitation(start, end, "full", "case", None,
                                  None, None, None, None, None)
                    for start, end in second_spans
                )
                contract = combine_extractions(grammar, tuple(
                    ExactObject("case_cite", cite.start, cite.end,
                                text[cite.start:cite.end]) for cite in found
                ))
                rows = edges(DOC_ID, text, whole(text), found, grammar,
                             lambda _key: None)
                self.assertEqual(
                    tuple((row.char_start, row.char_end) for row in rows),
                    tuple((obj.start, obj.end) for obj in contract),
                )

    def test_kinds_forms_resolution_and_unresolved_rows(self) -> None:
        fragments = (
            "466 U.S. 668", "466 U.S. at 690", "Id.", "supra", "Smith at 691",
            "466 U.S. at 692", "Tenn. Code", "Law Id.", "Journal 2",
            "orphan Id.", "orphan supra", "orphan reference",
        )
        text = " | ".join(fragments)
        found = (
            found_at(text, fragments[0], "full", "case", 0,
                     first_page="668", reporter_cite=fragments[0]),
            found_at(text, fragments[1], "short", "case", 0,
                     first_page="668", reporter_cite=fragments[0], pincite="690"),
            found_at(text, fragments[2], "id", "case", 0,
                     first_page="668", reporter_cite=fragments[0]),
            found_at(text, fragments[3], "supra", "case", 0,
                     first_page="668", reporter_cite=fragments[0]),
            found_at(text, fragments[4], "reference", "case", 0,
                     first_page="668", reporter_cite=fragments[0]),
            found_at(text, fragments[5], "short", "case", None,
                     reporter_cite=fragments[5], pincite="692"),
            found_at(text, fragments[6], "full", "law", 6),
            found_at(text, fragments[7], "id", "law", 6),
            found_at(text, fragments[8], "full", "journal", 8),
            found_at(text, fragments[9], "id", "unknown"),
            found_at(text, fragments[10], "supra", "unknown"),
            found_at(text, fragments[11], "reference", "unknown"),
        )
        seen: list[tuple[str, str, str]] = []

        def resolve(key: tuple[str, str, str]) -> int | None:
            seen.append(key)
            return 77

        rows = edges(DOC_ID, text, whole(text), found, (), resolve)
        self.assertTrue(all(isinstance(row, NewCitation) for row in rows))
        self.assertEqual(tuple(row.cite_type for row in rows), (
            "case_cite", "case_cite", "case_cite", "case_cite", "case_cite",
            "case_cite", "law_cite", "law_cite", "journal_cite",
            "unknown", "unknown", "unknown",
        ))
        self.assertEqual(tuple(row.cite_form for row in rows), tuple(
            cite.form for cite in found
        ))
        self.assertEqual(tuple(row.to_cluster for row in rows),
                         (77, 77, 77, 77, 77, None, None, None, None, None, None, None))
        self.assertEqual(seen, [("466", "U.S.", "668")] * 5)
        self.assertEqual(rows[5].reporter_cite, fragments[5])
        self.assertTrue(all(row.pattern_id == EDGE_PATTERN_ID for row in rows))

    def test_unresolved_short_never_uses_its_pin_page_as_a_key(self) -> None:
        text = "466 U.S. at 690. Id. Smith at 691."
        found = (
            FoundCitation(0, len("466 U.S. at 690"), "short", "case", None,
                          None, None, None, "466 U.S. at 690", "690"),
            found_at(text, "Id.", "id", "unknown"),
            found_at(text, "Smith at 691", "reference", "unknown"),
        )
        seen: list[tuple[str, str, str]] = []

        def resolve(key: tuple[str, str, str]) -> int | None:
            seen.append(key)
            return {("466", "U.S.", "690"): 88}.get(key)

        rows = edges(DOC_ID, text, whole(text), found, (), resolve)
        self.assertEqual(tuple(row.to_cluster for row in rows), (None, None, None))
        self.assertEqual(seen, [])
        self.assertEqual(tuple(row.raw_cite for row in rows),
                         ("466 U.S. at 690", "Id.", "Smith at 691"))

    def test_barrier_chains_then_clears_at_a_full_cite(self) -> None:
        text = "1 U.S. 2. Rule 11. Id. Id. 2 U.S. 3. Id."
        rule = object_at(text, "Rule 11", "court_rule", "fictional-rule/11")
        found = (
            found_at(text, "1 U.S. 2", "full", "case", 0,
                     first_page="2", reporter_cite="1 U.S. 2"),
            found_at(text, "Id.", "id", "case", 0,
                     first_page="2", reporter_cite="1 U.S. 2"),
            found_at(text, "Id.", "id", "case", 0,
                     first_page="2", reporter_cite="1 U.S. 2", occurrence=1),
            found_at(text, "2 U.S. 3", "full", "case", 3,
                     first_page="3", reporter_cite="2 U.S. 3"),
            found_at(text, "Id.", "id", "case", 3,
                     first_page="3", reporter_cite="2 U.S. 3", occurrence=2),
        )
        seen: list[tuple[str, str, str]] = []

        def resolve(key: tuple[str, str, str]) -> int | None:
            seen.append(key)
            return {("1", "U.S.", "2"): 12, ("2", "U.S.", "3"): 13}[key]

        rows = edges(DOC_ID, text, whole(text), found, (rule,), resolve)
        self.assertEqual(tuple(row.cite_type for row in rows), (
            "case_cite", "court_rule", "court_rule", "court_rule",
            "case_cite", "case_cite",
        ))
        self.assertEqual(tuple(row.key for row in rows[2:4]), (rule.key, rule.key))
        self.assertEqual(tuple(row.to_cluster for row in rows),
                         (12, None, None, None, 13, 13))
        self.assertEqual(seen, [("1", "U.S.", "2"),
                                ("2", "U.S.", "3"), ("2", "U.S.", "3")])

    def test_statute_overlap_sets_barrier_for_bare_id(self) -> None:
        text = "18 U.S.C. § 922(g)(1). Id."
        statute = object_at(text, "18 U.S.C. § 922(g)(1)", "statute", STATUTE_KEY)
        found = (
            found_at(text, "18 U.S.C. § 922(g)(1)", "full", "law", 0),
            found_at(text, "Id.", "id", "law", 0),
        )
        rows = edges(DOC_ID, text, whole(text), found, (statute,),
                     lambda _key: self.fail("non-case row queried the resolver"))
        self.assertEqual(tuple(row.cite_type for row in rows), ("statute", "statute"))
        self.assertEqual(tuple(row.key for row in rows), (STATUTE_KEY, STATUTE_KEY))
        self.assertEqual(rows[1].cite_form, "id")

    def test_designator_gaps_and_pin_do_not_copy_a_statute_key(self) -> None:
        for gap, bare_key in ((" ", None), (", ", None), (" at ", None),
                              (" The court turned to ", STATUTE_KEY)):
            text = f"18 U.S.C. § 922(g)(1). Id.{gap}§ 924(e)(1)"
            statute = object_at(text, "18 U.S.C. § 922(g)(1)", "statute", STATUTE_KEY)
            bare = object_at(text, "§ 924(e)(1)", "bare_section")
            found = (found_at(text, "Id.", "id", "law", 0),)
            rows = edges(DOC_ID, text, whole(text), found, (statute, bare),
                         lambda _key: None)
            with self.subTest(gap=gap):
                self.assertEqual(tuple(row.cite_type for row in rows),
                                 ("statute", "statute", "bare_section"))
                self.assertEqual(rows[1].key, bare_key)
                self.assertIsNone(rows[2].key)

        text = "18 U.S.C. § 922(g)(1). Id. § 924(e)(1)"
        statute = object_at(text, "18 U.S.C. § 922(g)(1)", "statute", STATUTE_KEY)
        bare = object_at(text, "§ 924(e)(1)", "bare_section")
        folded = found_at(text, "Id. § 924(e)(1)", "id", "law", 0)
        rows = edges(DOC_ID, text, whole(text), (folded,), (statute, bare),
                     lambda _key: None)
        self.assertEqual(tuple(row.cite_type for row in rows),
                         ("statute", "bare_section"))

        text = "18 U.S.C. § 922(g)(1). Id. at 3"
        statute = object_at(text, "18 U.S.C. § 922(g)(1)", "statute", STATUTE_KEY)
        pinned = found_at(text, "Id. at 3", "id", "law", 0, pincite="at 3")
        rows = edges(DOC_ID, text, whole(text), (pinned,), (statute,),
                     lambda _key: None)
        self.assertEqual((rows[1].cite_type, rows[1].key, rows[1].pincite),
                         ("statute", None, "at 3"))

    def test_ambiguous_key_sections_identity_and_raw_slices(self) -> None:
        text = "1 U.S. 2. 2 U.S. 3."
        boundary = text.index("2 U.S. 3") + 3
        sections = (
            Section("body", "majority", 0, boundary),
            Section("note", "footnote", boundary, len(text)),
        )
        first = found_at(text, "1 U.S. 2", "full", "case", 0,
                         first_page="2", reporter_cite="1 U.S. 2")
        second = found_at(text, "2 U.S. 3", "full", "case", 1,
                          first_page="3", reporter_cite="2 U.S. 3")
        rows = edges(DOC_ID, text, sections, (second, first), (), lambda _key: None)
        self.assertEqual(tuple(row.section_id for row in rows), ("body", "body"))
        self.assertEqual(tuple(row.to_cluster for row in rows), (None, None))
        self.assertEqual(tuple(row.ordinal for row in rows), (0, 1))
        self.assertEqual(tuple(row.raw_cite for row in rows),
                         tuple(text[row.char_start:row.char_end] for row in rows))
        self.assertEqual(tuple(row.citation_id for row in rows), tuple(
            hashlib.sha256(
                f"{DOC_ID}\n{row.char_start}\n{row.char_end}".encode()
            ).hexdigest() for row in rows
        ))
        other = edges("another-document", text, sections, (first,), (),
                      lambda _key: None)
        self.assertNotEqual(rows[0].citation_id, other[0].citation_id)

        footnote = found_at(text, "3.", "reference", "unknown")
        footnote_rows = edges(DOC_ID, text, sections, (footnote,), (),
                              lambda _key: None)
        self.assertEqual((footnote_rows[0].section_id, footnote_rows[0].section_type),
                         ("note", "footnote"))
        with self.assertRaises(ValueError):
            edges(DOC_ID, text, (), (), (), lambda _key: None)

    def test_same_fixture_with_hand_built_findings(self) -> None:
        text = FIXTURE_TEXT
        statute = object_at(text, "18 U.S.C. § 922(g)(1)", "statute", STATUTE_KEY)
        bare = object_at(text, "§ 924(e)(1)", "bare_section")
        found = (
            found_at(text, "18 U.S.C. § 922(g)(1)", "full", "law", 0),
            found_at(text, "Id.", "id", "law", 0),
            found_at(text, "466 U.S. 668", "full", "case", 2,
                     first_page="668", reporter_cite="466 U.S. 668"),
            found_at(text, "Id.", "id", "case", 2,
                     first_page="668", reporter_cite="466 U.S. 668", occurrence=1),
        )
        rows = edges(DOC_ID, text, whole(text), found, (statute, bare),
                     lambda key: 77 if key == ("466", "U.S.", "668") else None)
        self.assertEqual(tuple(row.cite_type for row in rows), (
            "statute", "statute", "bare_section", "case_cite", "case_cite",
        ))
        self.assertIsNone(rows[1].key)
        self.assertEqual(tuple(row.to_cluster for row in rows[-2:]), (77, 77))


try:
    importlib.import_module("eyecite")
except ImportError:
    EYECITE_AVAILABLE = False
else:
    EYECITE_AVAILABLE = True


@unittest.skipUnless(EYECITE_AVAILABLE, "eyecite runs only inside the pinned image")
class EyeciteBuilder(unittest.TestCase):
    def test_same_fixture_through_real_adapter(self) -> None:
        from gideon.casecite.adapter import find_citations

        text = FIXTURE_TEXT
        statute = object_at(text, "18 U.S.C. § 922(g)(1)", "statute", STATUTE_KEY)
        bare = object_at(text, "§ 924(e)(1)", "bare_section")
        found = find_citations(text)
        rows = edges(DOC_ID, text, whole(text), found, (statute, bare),
                     lambda key: 77 if key == ("466", "U.S.", "668") else None)

        def overlapping(fragment: str, occurrence: int = 0) -> tuple[NewCitation, ...]:
            start = text.index(fragment)
            for _ in range(occurrence):
                start = text.index(fragment, start + len(fragment))
            end = start + len(fragment)
            return tuple(row for row in rows if row.char_start < end and start < row.char_end)

        self.assertEqual(overlapping("18 U.S.C. § 922(g)(1)")[0].cite_type, "statute")
        self.assertTrue(all(row.key is None for row in overlapping("§ 924(e)(1)")))
        self.assertEqual(overlapping("466 U.S. 668")[0].to_cluster, 77)
        self.assertEqual(overlapping("Id.", 1)[0].to_cluster, 77)
        self.assertTrue(all(text[row.char_start:row.char_end] == row.raw_cite for row in rows))
