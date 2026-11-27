"""Name-only citation stripping and ordered prior-turn messages."""

import importlib
import json
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from gideon.casecite.found import CITE_FORMS, CiteForm, CiteKind, FoundCitation
from gideon.turn import history
from gideon.turn.history import (
    CITATION_RULES,
    HistoryEntry,
    name_only,
    prior_turn_messages,
)
from gideon.turn.research import RESEARCH_CONFIG

FIXTURE = Path(__file__).with_name("fixtures") / "turn" / "legal-prompt.json"


def history_fixture() -> tuple[tuple[HistoryEntry, ...], str]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return tuple(HistoryEntry(**entry) for entry in fixture["history"]), fixture["name_only"]


def finding_at(
    text: str, fragment: str, form: CiteForm, pincite: str | None = None, *, kind: CiteKind = "case", after: int = 0,
) -> FoundCitation:
    start = text.index(fragment, after)
    return FoundCitation(start, start + len(fragment), form, kind, None, None, None, None, None, pincite)


def fixture_findings(text: str) -> tuple[FoundCitation, ...]:
    return (
        finding_at(text, "912 F.3d 386", "full", "397"),
        finding_at(text, "912 F.3d at 398", "short", "398"),
        finding_at(text, "Kestrel at 399", "reference", "at 399"),
        finding_at(text, "Id. at 400", "id", "at 400"),
        finding_at(text, "supra, at 401", "supra", "at 401"),
        finding_at(text, "supra.", "supra"),
        finding_at(text, "45 F.4th 100", "full", "112"),
        finding_at(text, "500 U.S. 1", "full", "5"),
        finding_at(text, "300 F.3d 10", "full"),
        finding_at(text, "12 F. App'x 300", "full"),
        finding_at(text, "371 U.S. 471", "full"),
        finding_at(text, "371 U.S. at ", "short"),
        finding_at(text, "18 U.S.C. § 3583", "full", "(e)(3)", kind="law"),
        finding_at(text, "371 U.S. at ", "short", after=text.index("Under Osprey")),
    )


class TurnHistory(unittest.TestCase):
    def test_rendered_fixture_uses_each_citation_form(self) -> None:
        entries, expected = history_fixture()
        text = entries[1].text
        findings = fixture_findings(text)
        self.assertEqual(CITATION_RULES.keys(), set(CITE_FORMS))
        self.assertEqual({finding.form for finding in findings}, set(CITE_FORMS))
        self.assertEqual(name_only(text, findings), expected)

    def test_prior_messages_keep_user_text_and_entry_order(self) -> None:
        entries, expected = history_fixture()
        user, assistant, follow_up = entries
        self.assertEqual(
            prior_turn_messages(entries, {1: fixture_findings(assistant.text)}),
            [
                {"role": "user", "content": user.text},
                {"role": "assistant", "content": expected},
                {"role": "user", "content": follow_up.text},
            ],
        )
        self.assertNotIn(assistant.text, repr(assistant))
        self.assertEqual(prior_turn_messages((), {}), [])

    def test_reference_without_a_pin_keeps_the_name(self) -> None:
        text = "The fictitious opinion cites Kestrel v. Lantern."
        finding = finding_at(text, "Kestrel v. Lantern", "reference")
        self.assertEqual(name_only(text, (finding,)), text)

    def test_attached_page_ranges_and_lists_are_removed(self) -> None:
        full = "371 U.S. 471"
        short = "371 U.S. at "
        cases: tuple[tuple[str, CiteForm, str], ...] = (
            (full, "full", ", 485–86 (1963) [6]."),
            (full, "full", ", 485–86, 489 (1963) [6]."),
            (short, "short", "485–86 (1963) [6]."),
            (short, "short", "485–86, 489, 491 (1963) [6]."),
            ("371 U.S. at 485", "short", "–86 (1963) [6]."),
        )
        for cite, form, suffix in cases:
            with self.subTest(cite=cite, suffix=suffix):
                text = f"The fictitious rule comes from Osprey, {cite}{suffix}"
                finding = finding_at(text, cite, form)
                self.assertEqual(name_only(text, (finding,)), "The fictitious rule comes from Osprey (1963).")

    def test_comma_suffix_stays_when_it_ends_in_a_prose_figure(self) -> None:
        for suffix in (
            ", 14 months elapsed.", ", 489, 14 months elapsed.", ", 14.5 months elapsed.",
            ", 14 (of 20) petitions were timely.", ", 14 (all filed in 2020) were timely.",
        ):
            with self.subTest(suffix=suffix):
                cite = "371 U.S. at "
                text = f"Under fictitious Osprey, {cite}485–86{suffix}"
                finding = finding_at(text, cite, "short")
                self.assertEqual(name_only(text, (finding,)), f"Under fictitious Osprey{suffix}")

    def test_full_span_requires_a_comma_before_its_pin(self) -> None:
        cite = "371 U.S. 471"
        text = f"After fictitious Osprey, {cite} (1963), 485 permits expired."
        finding = finding_at(text, cite, "full", "485")
        self.assertEqual(name_only(text, (finding,)), "After fictitious Osprey (1963), 485 permits expired.")

    def test_full_span_removes_the_reported_pin_before_a_prose_figure(self) -> None:
        cite = "371 U.S. 471"
        text = f"Under fictitious Osprey, {cite}, 485, 14 months elapsed."
        finding = finding_at(text, cite, "full", "485")
        self.assertEqual(name_only(text, (finding,)), "Under fictitious Osprey, 14 months elapsed.")

    def test_pages_before_a_year_parenthetical_are_removed_when_prose_follows(self) -> None:
        cite = "371 U.S. 471"
        for parenthetical in ("(1963)", "(6th Cir. 1963)", "(N.D. Ill. Mar. 3, 1963)"):
            with self.subTest(parenthetical=parenthetical):
                text = f"Fictitious Osprey, {cite}, 485–86, 489 {parenthetical} supports relief."
                finding = finding_at(text, cite, "full")
                self.assertEqual(name_only(text, (finding,)), f"Fictitious Osprey {parenthetical} supports relief.")

    def test_full_span_removes_a_reported_star_page_pin(self) -> None:
        cite = "2019 WL 123456"
        text = f"Fictitious Harbor, {cite}, at *5 (6th Cir. 2019) [1]."
        finding = finding_at(text, cite, "full", "at *5")
        self.assertEqual(name_only(text, (finding,)), "Fictitious Harbor (6th Cir. 2019).")

    def test_full_span_removes_the_reported_pin_before_prose(self) -> None:
        cite = "371 U.S. 471"
        for pincite, pages in (("485", "485"), ("485–86", "485–86")):
            with self.subTest(pincite=pincite):
                text = f"The discussion in fictitious Osprey, {cite}, {pages} supports relief."
                finding = finding_at(text, cite, "full", pincite)
                self.assertEqual(name_only(text, (finding,)), "The discussion in fictitious Osprey supports relief.")

    def test_invalid_findings_refuse_without_answer_text(self) -> None:
        text = "Fictitious answer sentinel mentions Osprey, 371 U.S. 471."
        finding = finding_at(text, "371 U.S. 471", "full")
        for findings, error in (
            ((replace(finding, end=len(text) + 1),), "outside the answer"),
            ((finding, replace(finding, start=finding.start + 1)), "overlapping citation"),
            ((replace(finding, form=cast(CiteForm, text)),), "unsupported citation form"),
        ):
            with self.subTest(error=error):
                with self.assertRaisesRegex(ValueError, error) as refused:
                    name_only(text, findings)
                self.assertNotIn(text, str(refused.exception))

    def test_invalid_role_refuses_without_entry_text(self) -> None:
        text = "Fictitious entry sentinel"
        with self.assertRaisesRegex(ValueError, "unsupported history role") as refused:
            prior_turn_messages((HistoryEntry(text, text),), {})
        self.assertNotIn(text, str(refused.exception))

    def test_unsupported_history_form_is_refused(self) -> None:
        with (
            patch.object(history, "RESEARCH_CONFIG", {**RESEARCH_CONFIG, "history_form": "fixture form"}),
            self.assertRaisesRegex(ValueError, "history_form"),
        ):
            prior_turn_messages((), {})


try:
    importlib.import_module("eyecite")
except ImportError:
    EYECITE_AVAILABLE = False
else:
    EYECITE_AVAILABLE = True


@unittest.skipUnless(EYECITE_AVAILABLE, "eyecite runs only inside the pinned image")
class EyeciteHistory(unittest.TestCase):
    def test_same_fixture_through_real_adapter(self) -> None:
        from gideon.casecite.adapter import find_citations

        entries, expected = history_fixture()
        text = entries[1].text
        self.assertEqual(name_only(text, find_citations(text)), expected)
