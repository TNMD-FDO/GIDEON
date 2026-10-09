"""Pure treatment patterns over fictitious opinions and court geography."""

import hashlib
import unittest
from dataclasses import dataclass

from gideon.host.courts import CIRCUITS, STATE_CODES
from gideon.host.courts import LEVELS as HOST_LEVELS
from gideon.worker.citations import NewCitation
from gideon.worker.treatment import (
    CIRCUIT_PATTERN,
    LEVELS,
    PATTERN_SET_ID,
    STATE_PATTERN,
    Geography,
    binds,
    effective_section,
    parse_geography,
    sentence,
    signals,
)

DOC_ID = "fictional-document"
SAME_CIRCUIT = Geography("circuit", "ca6", None)


@dataclass(frozen=True)
class Section:
    section_id: str
    section_type: str
    char_start: int
    char_end: int
    parent_section_id: str | None = None


def citation(
    text: str, fragment: str = "Bricker, 1 U.S. 2", *,
    section: Section | None = None, kind: str = "case_cite",
    cluster: int | None = 1, ordinal: int = 0, occurrence: int = 0,
) -> NewCitation:
    start = text.index(fragment)
    for _ in range(occurrence):
        start = text.index(fragment, start + len(fragment))
    section = section or Section("body", "majority", 0, len(text))
    return NewCitation(
        f"fictional-citation-{ordinal}", DOC_ID, ordinal, start,
        start + len(fragment), section.section_id, section.section_type,
        kind, "full", fragment, fragment, None, None, cluster, "fictional-edges",
    )


def findings(
    text: str, rows: tuple[NewCitation, ...] | None = None,
    sections: tuple[Section, ...] | None = None,
    citing: Geography = SAME_CIRCUIT,
    cited: Geography | None = SAME_CIRCUIT,
) -> tuple:
    sections = sections or (Section("body", "majority", 0, len(text)),)
    rows = rows if rows is not None else (citation(text, section=sections[0]),)
    return signals(
        DOC_ID, text, sections, rows, citing,
        lambda cluster: "fictional-court" if cluster == 1 else None,
        lambda court: cited if court == "fictional-court" else None,
    )


class SentenceRule(unittest.TestCase):
    def test_paragraph_and_soft_newlines(self) -> None:
        for gap in ("\n", "\n\n"):
            text = f"We have not{gap}overruled Bricker, 1 U.S. 2."
            result = findings(text)
            self.assertEqual(len(result), 0 if gap == "\n" else 1)
        text = "We have not\noverruled Bricker, 1 U.S. 2."
        self.assertEqual(findings(text), ())

    def test_terminators_quotes_and_citation_periods(self) -> None:
        for terminator in (". ", "? ", "! ", ".\" ", "!) "):
            text = f"We overruled a different matter{terminator}Bricker, 1 U.S. 2 remains."
            self.assertEqual(findings(text), (), terminator)
        text = "We overruled Bricker, 1 U.S. 2. Other reasons followed."
        row = citation(text)
        start, end = sentence(text, row.char_start, row.char_end,
                              Section("body", "majority", 0, len(text)), (row,))
        self.assertIn("overruled", text[start:end])
        self.assertNotIn("Other reasons", text[start:end])

    def test_abbreviations_and_initial(self) -> None:
        for prefix in ("v.", "Id.", "id.", "e.g.", "i.e.", "cf.",
                       "et al.", "Inc.", "Co.", "Corp.", "Ltd.", "No.",
                       "Nos.", "Mr.", "Mrs.", "Ms.", "Dr.", "Jr.", "Sr.",
                       "St.", "Ct.", "Cir.", "Dist.", "App.", "Div.",
                       "Supp.", "Fed.", "Crim.", "Civ.", "Ann.", "Stat.",
                       "Sec.", "Art.", "Ch.", "Pt.", "Vol.", "J."):
            text = f"We overruled {prefix} Bricker, 1 U.S. 2."
            self.assertEqual(len(findings(text)), 1, prefix)
        self.assertEqual(findings("Id. at 5. We overrule it. Bricker, 1 U.S. 2."), ())
        text = "The court said no. We overruled Bricker, 1 U.S. 2."
        row = citation(text)
        start, _ = sentence(text, row.char_start, row.char_end,
                            Section("body", "majority", 0, len(text)), (row,))
        self.assertEqual(start, text.index("We overruled"))

    def test_window_edges_and_section_boundary(self) -> None:
        text = "overruled " + "x" * 310 + " Bricker, 1 U.S. 2"
        self.assertEqual(findings(text), ())
        text = "Bricker, 1 U.S. 2 " + "x" * 310 + " overruled"
        self.assertEqual(findings(text), ())
        for text in ("overruled Bricker, 1 U.S. 2", "Bricker, 1 U.S. 2 overruled"):
            row = citation(text)
            self.assertEqual(sentence(text, row.char_start, row.char_end,
                                      Section("body", "majority", 0, len(text)),
                                      (row,)), (0, len(text)))
        text = "We overruled another matter. Bricker, 1 U.S. 2"
        cut = text.index("Bricker")
        body = Section("body", "majority", 0, cut)
        note = Section("note", "footnote", cut, len(text), "body")
        row = citation(text, section=note)
        self.assertEqual(sentence(text, row.char_start, row.char_end, note, (row,))[0], cut)
        self.assertEqual(findings(text, (row,), (body, note)), ())


class PatternRule(unittest.TestCase):
    def test_inflections_and_participles(self) -> None:
        forms = {
            "overruled": ("overrule", "overrules", "overruled", "overruling"),
            "abrogated": ("abrogate", "abrogates", "abrogated", "abrogating"),
            "superseded": ("supersede", "supersedes", "superseded", "superseding"),
            "disapproved": ("disapprove", "disapproves", "disapproved", "disapproving"),
            "reversed": ("reversed",),
            "vacated": ("vacated",),
        }
        for signal, variants in forms.items():
            for verb in variants:
                text = f"We {verb} Bricker, 1 U.S. 2."
                with self.subTest(verb=verb):
                    self.assertEqual(findings(text)[0].treatment_signal, signal)
        for verb in ("reverse", "reverses", "vacate", "vacates", "overruleable"):
            self.assertEqual(findings(f"We {verb} Bricker, 1 U.S. 2."), ())

    def test_exclusions_and_surviving_occurrence(self) -> None:
        phrases = (
            "declined to overrule", "did not overrule", "has not been overruled",
            "whether Bricker, 1 U.S. 2 was overruled", "would overrule",
            "urges us to overrule", "need not overrule", "never overruled",
            "nor overruled", "neither overruled", "no case overruled",
            "should overrule", "could overrule", "might overrule",
            "may overrule", "must overrule", "asks us to overrule",
        )
        for phrase in phrases:
            text = phrase if "Bricker" in phrase else f"We {phrase} Bricker, 1 U.S. 2."
            with self.subTest(phrase=phrase):
                self.assertEqual(findings(text), ())
        text = "We did not overrule one case, but overruled Bricker, 1 U.S. 2."
        self.assertEqual(findings(text)[0].char_start, text.rindex("overruled"))

    def test_direction_qualifier_order_and_one_row(self) -> None:
        bricker = "Bricker, 1 U.S. 2"
        hall = "Hall, 3 U.S. 4"
        for phrase, qualifier in (("overruled by", "none"),
                                  ("overruled in part by", "in_part"),
                                  ("abrogated on other grounds by", "on_other_grounds"),
                                  ("disapproved in", "none")):
            text = f"{bricker} was {phrase} {hall}."
            rows = (citation(text, bricker), citation(text, hall, ordinal=1))
            result = findings(text, rows)
            with self.subTest(phrase=phrase):
                self.assertEqual([item.citation_id for item in result], [rows[0].citation_id])
                self.assertEqual(result[0].qualifier, qualifier)
        text = f"In {hall}, we overruled {bricker}."
        rows = (citation(text, hall), citation(text, bricker, ordinal=1))
        self.assertEqual(len(findings(text, rows)), 2)
        text = f"We overruled {bricker} in part and on other grounds."
        self.assertEqual(findings(text)[0].qualifier, "in_part")
        text = f"We overruled {bricker} on other grounds and in part."
        self.assertEqual(findings(text)[0].qualifier, "on_other_grounds")
        text = f"We overruled in part {bricker}."
        self.assertEqual(findings(text)[0].qualifier, "in_part")
        text = f"{bricker} was overruled on\nother grounds by {hall}."
        rows = (citation(text, bricker), citation(text, hall, ordinal=1))
        self.assertEqual(len(findings(text, rows)), 1)
        self.assertEqual(findings(text, rows)[0].qualifier, "on_other_grounds")
        for text in (f"We overruled and abrogated {bricker}.",
                     f"We overruled and overruled {bricker}."):
            result = findings(text)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].char_start, text.index("overruled"))
        self.assertEqual(findings(f"{bricker} was superseded by statute." )[0].state,
                         "negative")


class StandingAndBinding(unittest.TestCase):
    def test_section_types_and_footnote_parent(self) -> None:
        for kind in ("majority", "per_curiam", "plurality", "syllabus", "headmatter",
                     "concurrence", "dissent", "concurrence_dissent", "appendix",
                     "order", "unknown"):
            text = "Bricker, 1 U.S. 2 is overruled."
            section = Section("body", kind, 0, len(text))
            row = citation(text, section=section)
            result = findings(text, (row,), (section,))[0]
            self.assertEqual(result.effective_section, kind)
            self.assertEqual(result.state == "negative", kind in {"majority", "per_curiam"})
            self.assertEqual(result.no_state_reason,
                             None if kind in {"majority", "per_curiam"} else "non_holding")
        for parent_kind in ("majority", "dissent", None):
            text = "Bricker, 1 U.S. 2 is overruled."
            parent = Section("parent", parent_kind or "majority", 0, len(text))
            note = Section("note", "footnote", 0, len(text),
                           "parent" if parent_kind else None)
            row = citation(text, section=note)
            result = findings(text, (row,), (parent, note))[0]
            self.assertEqual(effective_section(row, {"parent": parent, "note": note}),
                             parent_kind or "footnote")
            self.assertEqual(result.no_state_reason,
                             None if parent_kind == "majority" else "non_holding")

    def test_binding_court_geography(self) -> None:
        supreme = Geography("scotus", None, None)
        circuit = Geography("circuit", "ca6", None)
        district = Geography("district", "ca6", None)
        other_circuit = Geography("circuit", "ca5", None)
        state = Geography("state_supreme", "ca6", "IL")
        appellate = Geography("state_appellate", "ca6", "IL")
        for cited in (supreme, circuit, district, other_circuit, state, appellate):
            self.assertTrue(binds(supreme, cited))
        for cited, expected in ((circuit, True), (district, True),
                                (other_circuit, False), (state, False)):
            self.assertEqual(binds(circuit, cited), expected)
        self.assertTrue(binds(state, appellate))
        self.assertTrue(binds(state, state))
        self.assertFalse(binds(state, district))
        for citing in (district, appellate, Geography("other", None, None)):
            self.assertFalse(binds(citing, circuit))

    def test_geography_grammar_and_levels(self) -> None:
        self.assertEqual(LEVELS, HOST_LEVELS)
        self.assertEqual(
            {first + second for first in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
             for second in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
             if STATE_PATTERN.fullmatch(first + second)},
            set(STATE_CODES),
        )
        candidates = {f"ca{number}" for number in range(100)} | {
            f"ca{letters}" for letters in ("dc", "fc", "aa", "zz")
        }
        self.assertEqual(
            {item for item in candidates if CIRCUIT_PATTERN.fullmatch(item)}, set(CIRCUITS),
        )
        self.assertEqual(parse_geography({"level": "circuit", "circuit": "ca6",
                                          "state": None}), Geography("circuit", "ca6", None))
        for value in (None, {}, {"level": "circuit"},
                      {"level": "imaginary", "circuit": None, "state": None},
                      {"level": "circuit", "circuit": "ca12", "state": None},
                      {"level": "circuit", "circuit": None, "state": "zz"},
                      {"level": "circuit", "circuit": None, "state": "ZZ"},
                      {"level": "circuit", "circuit": 6, "state": None}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_geography(value)


class SignalRows(unittest.TestCase):
    def test_states_reasons_identity_offsets_and_order(self) -> None:
        texts = (
            "Bricker, 1 U.S. 2 is overruled.",
            "Bricker, 1 U.S. 2 is abrogated.",
            "Bricker, 1 U.S. 2 is overruled.",
            "Bricker, 1 U.S. 2 was reversed.",
            "Bricker, 1 U.S. 2 is overruled.",
        )
        cases = (
            (Geography("circuit", "ca6", None), "majority", 1, "negative", None),
            (Geography("circuit", "ca5", None), "majority", 1, "caution", None),
            (Geography("circuit", "ca6", None), "dissent", 1, None, "non_holding"),
            (Geography("circuit", "ca6", None), "majority", 1, None,
             "lineage_unverified"),
            (Geography("circuit", "ca6", None), "majority", None, None, "unresolved"),
        )
        for text, (citing, kind, cluster, state, reason) in zip(texts, cases, strict=True):
            section = Section("body", kind, 0, len(text))
            row = citation(text, section=section, cluster=cluster)
            result = findings(text, (row,), (section,), citing)[0]
            self.assertEqual((result.state, result.no_state_reason), (state, reason))
            self.assertEqual(text[result.char_start:result.char_end],
                             text[result.char_start:result.char_end].lower())
            self.assertIn(text[result.char_start:result.char_end],
                          ("overruled", "abrogated", "reversed"))
            self.assertEqual(result.signal_id, hashlib.sha256(
                f"{row.citation_id}\npattern\n{PATTERN_SET_ID}".encode()
            ).hexdigest())
            self.assertEqual(result.citation_id, row.citation_id)
            self.assertEqual(result.doc_id, DOC_ID)
        text = "We overruled Bricker, 1 U.S. 2 and Hall, 3 U.S. 4."
        rows = (citation(text, "Bricker, 1 U.S. 2", ordinal=1),
                citation(text, "Hall, 3 U.S. 4", ordinal=0))
        result = findings(text, rows)
        self.assertEqual(tuple(item.citation_id for item in result),
                         (rows[1].citation_id, rows[0].citation_id))
        self.assertEqual(len(result), 2)
        self.assertEqual(findings(text, (citation(text, kind="law_cite"),)), ())
        with self.assertRaises(ValueError):
            findings(text, (citation(text),), (Section("elsewhere", "majority", 0,
                                                  len(text)),))
