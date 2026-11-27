"""The Legal instruction, evidence headings, provenance, and message order."""

import json
import unittest
from collections.abc import Sequence
from dataclasses import replace
from typing import cast
from unittest.mock import patch

from test_turn_history import FIXTURE, fixture_findings

from gideon.extraction.contract import OBJECT_TYPES
from gideon.turn import research
from gideon.turn.history import HistoryEntry
from gideon.turn.prompt import (
    ARITHMETIC_REFUSAL,
    CLOSED_BOOK,
    OBJECT_LABELS,
    SECTION_LABELS,
    SENTENCE_KINDS,
    SOURCE_TAGS,
    THIN_SUPPORT,
    TIER_LABELS,
    TRACER_SYSTEM,
    EvidenceHit,
    OutOfCorpusCourt,
    OutOfCorpusObject,
    PromptProvenance,
    legal_messages,
    legal_system,
    messages,
)
from gideon.turn.tier import AUTHORITY_TIERS, AuthorityTier
from gideon.worker.sections import SECTION_TYPES


class LegalPrompt(unittest.TestCase):
    def setUp(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.history = tuple(HistoryEntry(**entry) for entry in fixture["history"])
        self.hits = tuple(EvidenceHit(**hit) for hit in fixture["hits"])
        self.question: str = fixture["question"]
        self.name_only: str = fixture["name_only"]
        self.recorded: list[dict[str, str]] = fixture["messages"]
        provenance = fixture["provenance"]
        self.provenance = PromptProvenance(
            lockfiles=tuple(provenance["lockfiles"]),
            plan_mode=provenance["plan_mode"],
            filter_lines=tuple(provenance["filter_lines"]),
            out_of_corpus_objects=tuple(OutOfCorpusObject(**obj) for obj in provenance["out_of_corpus_objects"]),
            out_of_corpus_courts=tuple(OutOfCorpusCourt(**court) for court in provenance["out_of_corpus_courts"]),
        )

    def user_content(self, hits: Sequence[EvidenceHit], provenance: PromptProvenance) -> str:
        return legal_messages(self.question, hits, provenance, support_level="ok")[-1]["content"]

    def test_recorded_messages_keep_history_and_evidence_in_order(self) -> None:
        user, answer, follow_up = self.history
        findings = fixture_findings(answer.text)
        built = legal_messages(
            self.question, self.hits, self.provenance, support_level="ok",
            history=self.history, history_findings={1: findings},
        )
        self.assertEqual(built, self.recorded)
        self.assertEqual(built[0], {"role": "system", "content": legal_system("ok")})
        self.assertEqual(built[1:-1], [
            {"role": "user", "content": user.text},
            {"role": "assistant", "content": self.name_only},
            {"role": "user", "content": follow_up.text},
        ])
        self.assertNotIn(answer.text[findings[0].start:findings[0].end], built[2]["content"])
        self.assertEqual(built[-1]["role"], "user")
        evidence, provenance, question = built[-1]["content"].rsplit("\n\n", 2)
        heading, *passages = evidence.split("\n\n")
        self.assertEqual(heading, "Evidence")
        for hit, passage in zip(self.hits, passages, strict=True):
            header, text = passage.split("\n", 1)
            self.assertTrue(header.startswith(f"[SRC:{hit.number}] {hit.case_name}"))
            self.assertEqual(text, hit.text)
        provenance_lines = provenance.splitlines()
        self.assertEqual(provenance_lines[0], "Retrieval provenance")
        self.assertIn(", ".join(self.provenance.lockfiles), provenance_lines[1])
        self.assertIn("The question was searched as written.", provenance_lines[2])
        self.assertEqual(question, f"Question\n{self.question}")

    def test_thin_instruction_follows_the_common_system_prefix(self) -> None:
        common = "\n\n".join((CLOSED_BOOK, SOURCE_TAGS, SENTENCE_KINDS, ARITHMETIC_REFUSAL))
        self.assertEqual(legal_system("ok"), common)
        self.assertEqual(legal_system("thin"), f"{common}\n\n{THIN_SUPPORT}")
        with self.assertRaisesRegex(ValueError, "support level"):
            legal_system("none")

    def test_every_worker_section_and_authority_tier_has_a_heading_label(self) -> None:
        self.assertEqual(SECTION_LABELS.keys(), set(SECTION_TYPES))
        self.assertEqual(TIER_LABELS.keys(), set(AUTHORITY_TIERS))
        base = self.hits[0]
        for section in SECTION_TYPES:
            with self.subTest(section=section):
                hit = replace(base, section_type=section)
                header = self.user_content((hit,), self.provenance).splitlines()[2]
                self.assertTrue(header.endswith(f" | {SECTION_LABELS[section]}"))
        for tier in AUTHORITY_TIERS:
            with self.subTest(tier=tier):
                hit = replace(base, authority_tier=cast(AuthorityTier, tier))
                header = self.user_content((hit,), self.provenance).splitlines()[2]
                self.assertIn(f" | {TIER_LABELS[tier]} | ", header)

    def test_name_only_heading_and_optional_metadata_leave_passage_text_whole(self) -> None:
        text = "Fictitious Harbor Caption, 777 F.3d 901, No. 99-901\nA passage can contain [SRC:99]."
        base = replace(self.hits[0], text=text)
        for name, court, year, heading_name in (
            (base.case_name, base.court, base.year, f"{base.case_name} ({base.court} {base.year})"),
            (base.case_name, base.court, None, f"{base.case_name} ({base.court})"),
            (base.case_name, None, base.year, f"{base.case_name} ({base.year})"),
            (None, None, None, "Case name unavailable"),
        ):
            with self.subTest(name=name, court=court, year=year):
                hit = replace(base, case_name=name, court=court, year=year)
                content = self.user_content((hit,), self.provenance)
                header, body = content.split("\n\n")[1].split("\n", 1)
                self.assertEqual(
                    header, f"[SRC:{hit.number}] {heading_name} | {TIER_LABELS[hit.authority_tier]} | "
                    f"{SECTION_LABELS[hit.section_type]}",
                )
                self.assertNotIn("Fictitious Harbor Caption", header)
                self.assertNotIn("777 F.3d 901", header)
                self.assertNotIn("No. 99-901", header)
                self.assertEqual(body, text)

    def test_provenance_stands_with_empty_evidence_and_no_missing_objects(self) -> None:
        filters = ("Scope includes the fictitious harbor courts.", "The district court filter was applied.")
        provenance = replace(
            self.provenance, filter_lines=filters, out_of_corpus_objects=(), out_of_corpus_courts=(),
        )
        built = legal_messages(self.question, (), provenance, support_level="ok")
        self.assertEqual([message["role"] for message in built], ["system", "user"])
        block, question = built[-1]["content"].split("\n\n")
        title, scope, plan_mode, *filter_lines = block.splitlines()
        self.assertEqual(title, "Retrieval provenance")
        self.assertIn(", ".join(provenance.lockfiles), scope)
        self.assertIn("mode was off", plan_mode)
        self.assertEqual(tuple(filter_lines), filters)
        self.assertEqual(question, f"Question\n{self.question}")

    def test_object_wording_follows_type_resolution_and_citing_evidence(self) -> None:
        self.assertEqual(OBJECT_LABELS.keys(), set(OBJECT_TYPES))
        for object_type in OBJECT_TYPES:
            for resolved, phrase in (
                ("unresolved", "is not in GIDEON's corpus"),
                ("unresolved_in_scope", "was not found in the courts searched"),
            ):
                for cites in (True, False):
                    with self.subTest(object_type=object_type, resolved=resolved, cites=cites):
                        entry = OutOfCorpusObject(0, object_type, resolved, cites)
                        provenance = replace(self.provenance, out_of_corpus_objects=(entry,), out_of_corpus_courts=())
                        block, _ = self.user_content((), provenance).split("\n\n")
                        line = block.splitlines()[-1]
                        self.assertTrue(line.startswith(f"The {OBJECT_LABELS[object_type]}"))
                        self.assertIn(f"object {entry.index + 1}", line)
                        self.assertIn(phrase, line)
                        self.assertEqual("GIDEON's corpus" in line, resolved == "unresolved")
                        self.assertEqual("passage in the evidence cites it" in line, cites)

    def test_named_court_follows_object_lines_and_uses_its_display_name(self) -> None:
        court, = self.provenance.out_of_corpus_courts
        filter_line = "The fictitious named courts were searched."
        provenance = replace(self.provenance, filter_lines=(filter_line,))
        _, block, _ = self.user_content(self.hits, provenance).rsplit("\n\n", 2)
        lines = block.splitlines()
        self.assertEqual(lines[3], filter_line)
        for line, entry in zip(lines[4:-1], provenance.out_of_corpus_objects, strict=True):
            self.assertIn(f"object {entry.index + 1}", line)
        self.assertEqual(lines[-1], f"The court you named, {court.name}, is outside GIDEON's corpus.")
        self.assertNotIn(court.court_id, lines[-1])
        self.assertNotIn("777 U.S. 901", block)
        self.assertNotIn("§ 79-42-900", block)

    def test_invalid_inputs_raise_without_prompt_text(self) -> None:
        sentinel = "Fictitious private prompt text"
        hit = replace(self.hits[0], text=sentinel)
        self.assertNotIn(sentinel, repr(hit))
        entry = self.provenance.out_of_corpus_objects[0]
        for hits, provenance, error in (
            ((replace(hit, authority_tier=cast(AuthorityTier, sentinel)),), self.provenance, "authority tier"),
            ((replace(hit, section_type=sentinel),), self.provenance, "section type"),
            ((replace(hit, number=2),), self.provenance, "evidence number"),
            ((), replace(self.provenance, plan_mode=sentinel), "plan mode"),
            ((), replace(self.provenance, out_of_corpus_objects=(replace(entry, object_type=sentinel),)), "object type"),
            ((), replace(self.provenance, out_of_corpus_objects=(replace(entry, resolved=sentinel),)), "resolved word"),
        ):
            with self.subTest(error=error):
                with self.assertRaisesRegex(ValueError, error) as refused:
                    legal_messages(sentinel, hits, provenance, support_level="ok")
                self.assertNotIn(sentinel, str(refused.exception))

    def test_unsupported_evidence_order_is_refused(self) -> None:
        with (
            patch.object(research, "RESEARCH_CONFIG", {**research.RESEARCH_CONFIG, "evidence_order": "fixture order"}),
            self.assertRaisesRegex(ValueError, "evidence_order"),
        ):
            self.user_content(self.hits, self.provenance)

    def test_tracer_keeps_its_system_instruction(self) -> None:
        self.assertEqual(
            messages(self.question, ())[0],
            {"role": "system", "content": TRACER_SYSTEM},
        )
