"""Tests for the local Markdown tracker board and its lint rules."""

import ast
import os
import re
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from tools import tracker
from tools.exportboundary import in_export_tree

ROOT = Path(__file__).resolve().parents[1]
STATUSES = "needs-triage, needs-info, ready-for-human, ready-for-agent, claimed"
CLOSING_STATUSES = "resolved, wontfix"
VOCABULARY_LINE = f"Statuses: {STATUSES}; closed: {CLOSING_STATUSES}"
BASE_DATE = date(2026, 9, 8)


class Fixture:
    """A synthetic checkout with only fictitious issue data."""

    def __init__(self, root: Path) -> None:
        self.root = root
        agents = root / "docs" / "agents"
        agents.mkdir(parents=True)
        (agents / "issue-tracker.md").write_text(
            "# Fixture tracker\n\n" + VOCABULARY_LINE + "\n", encoding="utf-8"
        )
        (agents / "triage-labels.md").write_text(
            "| Label in skills | Label in tracker |\n| --- | --- |\n"
            "| needs-triage | needs-triage |\n",
            encoding="utf-8",
        )

    def add(
        self,
        effort: str,
        number: int,
        title: str,
        status: str | None,
        blockers: str | None = None,
        *,
        tag: str | None = None,
        bold: bool = True,
    ) -> Path:
        path = self.root / ".scratch" / effort / "issues" / f"{number:02d}-{effort}-fixture.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        heading = f"# {number:02d}{':' if bold else ' —'} {title}\n"
        lines = [heading, ""]
        if blockers is not None:
            key = "**Blocked by:**" if bold else "Blocked by:"
            lines.extend([f"{key} {blockers}", ""])
        if tag is not None:
            key = "**Tag:**" if bold else "Tag:"
            lines.extend([f"{key} {tag}", ""])
        if status is not None:
            key = "**Status:**" if bold else "Status:"
            lines.extend([f"{key} {status}", ""])
        path.write_text("\n".join(lines), encoding="utf-8")
        return path.relative_to(self.root)


def make_facts(
    paths: tuple[Path, ...], *, uncommitted: tuple[Path, ...] = ()
) -> tracker.GitFacts:
    return tracker.GitFacts(
        tuple((path, BASE_DATE - timedelta(days=index + 1)) for index, path in enumerate(paths)),
        frozenset(uncommitted),
    )


def build_fixture_board(
    fixture: Fixture,
    facts: tracker.GitFacts,
    *,
    today: date = BASE_DATE,
) -> tracker.Board:
    return tracker.build_board(
        fixture.root,
        today,
        facts_reader=lambda _root: facts,
    )


def run_cli(arguments: list[str], root: Path) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = tracker.main(arguments, root=root, today=BASE_DATE)
    return code, stdout.getvalue(), stderr.getvalue()


class Parsing(TestCase):
    """The parser accepts both ticket header shapes without reading bodies."""

    def test_header_shapes_title_forms_and_missing_status(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            first = fixture.add("alpha", 1, "Bold title", "resolved", "none", bold=True)
            second = fixture.add("beta", 2, "Plain title", "ready-for-agent", None, bold=False)
            third = fixture.add("gamma", 3, "Missing status", None, "none", bold=True)
            facts = make_facts((first, second, third))
            board = build_fixture_board(fixture, facts)
            tickets = {ticket.path: ticket for ticket in board.tickets}
            self.assertEqual(tickets[first].title, "Bold title")
            self.assertEqual(tickets[second].title, "Plain title")
            self.assertEqual(tickets[second].status, "ready-for-agent")
            self.assertEqual(tickets[third].status, "missing")
            finding = next(item for item in board.findings if item.path == third)
            self.assertGreaterEqual(finding.line, 0)
            self.assertIn("canonical status", finding.fix)


class BlockerGrammar(TestCase):
    """The blocker line has one deliberately small, sentence-bounded grammar."""

    def test_all_blocker_forms(self) -> None:
        cases: tuple[tuple[str | None, tuple[tracker.Reference, ...], bool, bool], ...] = (
            ("none", (), False, False),
            ("nothing", (), False, False),
            ("None", (), False, False),
            ("—", (), False, False),
            ("- no blockers", (), False, False),
            (None, (), False, False),
            ("12 (reason mentions 13–15)", (tracker.Reference("alpha", 12),), False, False),
            ("12, 13", (tracker.Reference("alpha", 12), tracker.Reference("alpha", 13)), False, False),
            ("beta ticket 12", (tracker.Reference("beta", 12),), False, False),
            (
                "beta tickets 12 and 13",
                (tracker.Reference("beta", 12), tracker.Reference("beta", 13)),
                False,
                False,
            ),
            ("ticket 12", (tracker.Reference("alpha", 12),), False, False),
            (
                "ticket 12–14",
                tuple(tracker.Reference("alpha", number) for number in (12, 13, 14)),
                False,
                False,
            ),
            ("ticket 12. Explanation starts with Ticket 13", (tracker.Reference("alpha", 12),), False, False),
            ("v0.1.12, 1.0, and 2026-09-05", (), True, False),
            ("slice 4", (), True, False),
            ("12; slice 4", (tracker.Reference("alpha", 12),), True, False),
            ("none for this ticket; beta ticket 12", (), False, False),
            ("fired", (), False, True),
            ("fired; beta ticket 12", (tracker.Reference("beta", 12),), False, True),
            ("Fired (…) ", (), False, True),
            ("none (fired …)", (), False, False),
            ("(fired)", (), False, False),
        )
        for value, expected, external, fired in cases:
            with self.subTest(value=value):
                actual, actual_external, actual_fired = tracker.parse_blockers(value, "alpha")
                self.assertEqual(actual, expected)
                self.assertEqual(actual_external, external)
                self.assertEqual(actual_fired, fired)


class Sections(TestCase):
    """Derived sections preserve dependency, status, effort, and age rules."""

    def test_critical_path_closure_order_and_dropped_references(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            closed = fixture.add("alpha", 1, "Closed", "resolved", "none")
            root = fixture.add("alpha", 2, "Root", "ready-for-agent", "none")
            dependent = fixture.add("alpha", 3, "Dependent", "ready-for-agent", "02")
            release = fixture.add(
                "alpha", 9, "Release", "ready-for-agent", "03, 01, 99", tag="v9.9.9"
            )
            facts = make_facts((closed, root, dependent, release))
            board = build_fixture_board(fixture, facts)
            self.assertEqual(board.next_tag, "v9.9.9")
            self.assertEqual([ticket.number for ticket in board.critical_path], [2, 3])
            self.assertNotIn(9, [ticket.number for ticket in board.critical_path])
            self.assertEqual(board.release_ticket, next(ticket for ticket in board.tickets if ticket.number == 9))
            self.assertTrue(any("alpha ticket 01" in item.problem for item in board.findings))
            self.assertTrue(any("alpha ticket 99" in item.problem for item in board.findings))

    def test_release_ticket_is_never_a_row_even_through_a_cycle(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            blocker = fixture.add("alpha", 1, "Blocker", "ready-for-agent", "09")
            release = fixture.add("alpha", 9, "Release", "ready-for-agent", "01, 09", tag="v9.9.9")
            board = build_fixture_board(fixture, make_facts((blocker, release)))
            self.assertEqual([ticket.number for ticket in board.critical_path], [1])
            self.assertTrue(any("blocking cycle" in item.problem for item in board.findings))
            rendered = tracker.render_markdown(board)
            self.assertIn("## Critical path to v9.9.9 — alpha ticket 09", rendered)
            self.assertNotIn("| alpha 09 | Release |", rendered)

    def test_sections_qualify_effort_and_hold_threshold_boundary(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            paths = (
                fixture.add("alpha", 1, "Closed", "resolved", "none"),
                fixture.add("alpha", 2, "Frontier", "ready-for-agent", "none"),
                fixture.add("alpha", 3, "Decision old", "ready-for-human", "none"),
                fixture.add("alpha", 4, "Decision new", "ready-for-human", "none"),
                fixture.add("alpha", 5, "At boundary", "needs-triage", "none"),
                fixture.add("alpha", 6, "Past boundary", "needs-triage", "none"),
                fixture.add("beta", 1, "External frontier", "ready-for-agent", "slice 4", bold=False),
            )
            dates = {
                paths[0]: BASE_DATE,
                paths[1]: BASE_DATE,
                paths[2]: BASE_DATE - timedelta(days=3),
                paths[3]: BASE_DATE - timedelta(days=1),
                paths[4]: BASE_DATE - timedelta(days=tracker.TRIAGE_THRESHOLD_DAYS),
                paths[5]: BASE_DATE - timedelta(days=tracker.TRIAGE_THRESHOLD_DAYS + 1),
                paths[6]: BASE_DATE,
            }
            facts = tracker.GitFacts(tuple(dates.items()), frozenset())
            board = build_fixture_board(fixture, facts)
            self.assertEqual([ticket.number for ticket in board.frontier], [2])
            self.assertEqual([ticket.number for ticket in board.decisions_owed], [3, 4])
            self.assertEqual([ticket.number for ticket in board.triage_owed], [6])
            self.assertEqual(board.efforts_read, 2)
            self.assertEqual(board.efforts_open, 2)
            rendered = tracker.render_markdown(board)
            self.assertIn(f"## Triage owed (past {tracker.TRIAGE_THRESHOLD_DAYS} days)", rendered)
            self.assertIn("| number | title | age (days) |", rendered)
            self.assertIn("| alpha 02 | Frontier | 0 |", rendered)
            self.assertIn("| 01 | External frontier |", rendered)
            self.assertIn("external", rendered)

    def test_unknown_status_is_open_and_sorts_after_vocabulary(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            first = fixture.add("alpha", 1, "Unknown", "invented", "none")
            second = fixture.add("alpha", 2, "Known", "claimed", "none")
            facts = make_facts((first, second))
            board = build_fixture_board(fixture, facts)
            self.assertEqual(board.open_tickets, 2)
            listing = board.listing[0][1]
            self.assertEqual([ticket.number for ticket in listing], [2, 1])
            self.assertTrue(any(item.path == first for item in board.findings))

    def test_listing_groups_efforts_and_uses_document_status_order(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            paths = (
                fixture.add("zeta", 1, "Claimed", "claimed", "none"),
                fixture.add("zeta", 2, "Triage", "needs-triage", "none"),
                fixture.add("zeta", 3, "Agent", "ready-for-agent", "none"),
                fixture.add("alpha", 1, "Human", "ready-for-human", "none"),
            )
            board = build_fixture_board(fixture, make_facts(paths))
            self.assertEqual([effort for effort, _ in board.listing], ["alpha", "zeta"])
            self.assertEqual(
                [ticket.status for ticket in board.listing[1][1]],
                ["needs-triage", "ready-for-agent", "claimed"],
            )

    def test_waiting_standing_tickets_are_separate_and_not_offered(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            ordinary = fixture.add("alpha", 10, "Ordinary", "ready-for-agent", "none")
            waiting = (
                fixture.add("standing", 1, "Triage", "needs-triage", "upstream event"),
                fixture.add("standing", 2, "Info", "needs-info", "reporter event"),
                fixture.add("standing", 3, "Human", "ready-for-human", "calendar event"),
                fixture.add("standing", 4, "Agent", "ready-for-agent", "release event"),
                fixture.add("standing", 5, "Claimed", "claimed", "person outside project"),
            )
            blocked = fixture.add(
                "alpha", 11, "Blocked ordinary", "ready-for-agent", "ticket 10; upstream event"
            )
            board = build_fixture_board(fixture, make_facts((ordinary, *waiting, blocked)))

            self.assertEqual([ticket.number for ticket in board.standing], [1, 2, 3, 4, 5])
            self.assertEqual(board.standing_open, 5)
            self.assertEqual(board.frontier, (next(ticket for ticket in board.tickets if ticket.path == ordinary),))
            self.assertEqual(board.decisions_owed, ())
            self.assertEqual(board.triage_owed, ())
            self.assertEqual([effort for effort, _ in board.listing], ["alpha"])
            self.assertEqual(board.open_tickets, 2)
            self.assertEqual(board.efforts_open, 1)
            self.assertEqual(
                tracker.summary_line(board),
                "2 open across 1 efforts and 5 standing (7 tickets read in 2 efforts).",
            )
            rendered = tracker.render_markdown(board)
            self.assertIn("## Standing (5)", rendered)
            self.assertIn("| 01 | Triage | needs-triage | external |", rendered)
            self.assertIn("| 05 | Claimed | claimed | external |", rendered)
            self.assertIn("| 11 | Blocked ordinary | ready-for-agent | alpha 10, external |", rendered)
            self.assertNotIn("### standing (", rendered.lower())

    def test_fired_standing_tickets_use_ordinary_sections(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            ordinary = fixture.add("alpha", 9, "Open dependency", "ready-for-agent", "none")
            agent = fixture.add("standing", 1, "Agent", "ready-for-agent", "fired")
            human = fixture.add("standing", 2, "Human", "ready-for-human", "fired")
            triage = fixture.add("standing", 3, "Triage", "needs-triage", "fired")
            referenced = fixture.add("standing", 4, "Referenced", "ready-for-agent", "fired; alpha ticket 09")
            dates = {
                ordinary: BASE_DATE,
                agent: BASE_DATE,
                human: BASE_DATE,
                triage: BASE_DATE - timedelta(days=tracker.TRIAGE_THRESHOLD_DAYS + 1),
                referenced: BASE_DATE,
            }
            board = build_fixture_board(fixture, tracker.GitFacts(tuple(dates.items()), frozenset()))

            self.assertEqual(
                [(ticket.effort, ticket.number) for ticket in board.frontier],
                [("alpha", 9), ("standing", 1)],
            )
            self.assertEqual([(ticket.effort, ticket.number) for ticket in board.decisions_owed], [("standing", 2)])
            self.assertEqual([(ticket.effort, ticket.number) for ticket in board.triage_owed], [("standing", 3)])
            self.assertNotIn(referenced, [ticket.path for ticket in board.frontier])
            self.assertEqual([ticket.number for ticket in board.standing], [1, 2, 3, 4])
            rendered = tracker.render_markdown(board)
            self.assertIn("## Standing (4)", rendered)
            self.assertIn("| 01 | Agent | ready-for-agent | fired |", rendered)
            self.assertIn("| 04 | Referenced | ready-for-agent | alpha 09, fired |", rendered)

    def test_waiting_standing_ticket_is_on_a_release_critical_path_without_a_mark(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            standing = fixture.add("standing", 1, "External dependency", "ready-for-agent", "upstream release")
            release = fixture.add(
                "alpha", 9, "Release", "ready-for-agent", "standing ticket 01", tag="v0.2.0"
            )
            board = build_fixture_board(fixture, make_facts((standing, release)))

            self.assertEqual([(ticket.effort, ticket.number) for ticket in board.critical_path], [("standing", 1)])
            self.assertEqual(board.frontier, ())
            rendered = tracker.render_markdown(board)
            self.assertIn("| standing 01 | External dependency | ready-for-agent | external |", rendered)
            self.assertNotIn("| * standing 01 |", rendered)


class Lint(TestCase):
    """Each lint rule and both shape guards produce a finding with path, line, and fix."""

    def test_rules_and_shape_guards_include_path_line_and_fix(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            missing = fixture.add("alpha", 1, "Missing", "ready-for-agent", "99")
            unknown = fixture.add("alpha", 2, "Unknown", "invented", "none")
            closed_ref = fixture.add("alpha", 3, "Closed ref", "ready-for-agent", "04")
            fixture.add("alpha", 4, "Closed", "resolved", "99")
            claimed = fixture.add("alpha", 5, "Claimed", "claimed", "none")
            cycle_a = fixture.add("alpha", 6, "Cycle A", "ready-for-agent", "07")
            cycle_b = fixture.add("alpha", 7, "Cycle B", "ready-for-agent", "06")
            closed_line = fixture.add("alpha", 8, "Closed history", "resolved", "99")
            bad_name = fixture.root / ".scratch" / "alpha" / "issues" / "bad-name.md"
            bad_name.write_text("Status: resolved\n", encoding="utf-8")
            facts = make_facts(
                (missing, unknown, closed_ref, claimed, cycle_a, cycle_b, closed_line),
                uncommitted=(claimed,),
            )
            board = build_fixture_board(fixture, facts)
            findings = board.findings
            self.assertTrue(any(item.path == missing and "does not name" in item.problem for item in findings))
            self.assertTrue(any(item.path == unknown and "outside the vocabulary" in item.problem for item in findings))
            self.assertTrue(any(item.path == closed_ref and "resolved" in item.problem for item in findings))
            self.assertTrue(any(item.path == claimed and "modified or untracked" in item.problem for item in findings))
            self.assertTrue(any(item.path == bad_name.relative_to(fixture.root) for item in findings))
            self.assertTrue(any("blocking cycle" in item.problem for item in findings))
            self.assertFalse(any(item.path == closed_line and "blocker" in item.problem for item in findings))
            for finding in findings:
                self.assertTrue(finding.path.parts)
                self.assertGreaterEqual(finding.line, 0)
                self.assertTrue(finding.fix)

    def test_standing_state_lint_has_three_shapes_and_ignores_closed_tickets(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            target = fixture.add("alpha", 10, "Target", "ready-for-agent", "none")
            neither_none = fixture.add("standing", 1, "Neither none", "ready-for-agent", "none")
            neither_reference = fixture.add("standing", 2, "Neither reference", "ready-for-agent", "alpha ticket 10")
            both = fixture.add("standing", 3, "Both", "ready-for-agent", "fired; external event")
            claimed_waiting = fixture.add("standing", 4, "Claimed waiting", "claimed", "external event")
            fired_agent = fixture.add("standing", 5, "Fired agent", "ready-for-agent", "fired")
            claimed_fired = fixture.add("standing", 6, "Claimed fired", "claimed", "fired")
            resolved = fixture.add("standing", 7, "Resolved history", "resolved", "none")
            board = build_fixture_board(
                fixture,
                make_facts(
                    (target, neither_none, neither_reference, both, claimed_waiting, fired_agent, claimed_fired, resolved)
                ),
            )
            findings = {item.path: item for item in board.findings if item.path != target}

            self.assertEqual(findings[neither_none].problem, "standing ticket names no event in its Blocked by line")
            self.assertEqual(findings[neither_reference].problem, "standing ticket names no event in its Blocked by line")
            self.assertEqual(findings[both].problem, "standing ticket is both fired and waiting")
            self.assertEqual(
                findings[claimed_waiting].problem,
                "claimed standing ticket still names its event as a blocker",
            )
            self.assertEqual(findings[neither_none].line, 4)
            self.assertIn("Name the event no session can cause", findings[neither_none].fix)
            self.assertEqual(
                findings[both].fix,
                "Keep fired and move the event into the parenthesis, or drop fired while the event stands",
            )
            self.assertEqual(findings[claimed_waiting].fix, "Write fired with the event in the parenthesis, or unclaim the ticket")
            self.assertNotIn(fired_agent, findings)
            self.assertNotIn(claimed_fired, findings)
            self.assertNotIn(resolved, findings)

    def test_now_sections_fail_closed_for_malformed_standing_states(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            malformed = (
                fixture.add("standing", 1, "Agent neither", "ready-for-agent", "none"),
                fixture.add("standing", 2, "Agent both", "ready-for-agent", "fired; external event"),
                fixture.add("standing", 3, "Human neither", "ready-for-human", "none"),
                fixture.add("standing", 4, "Human both", "ready-for-human", "fired; external event"),
                fixture.add("standing", 5, "Triage neither", "needs-triage", "none"),
                fixture.add("standing", 6, "Triage both", "needs-triage", "fired; external event"),
                fixture.add("standing", 7, "Agent fired", "ready-for-agent", "fired"),
                fixture.add("standing", 8, "Human fired", "ready-for-human", "fired"),
                fixture.add("standing", 9, "Triage fired", "needs-triage", "fired"),
            )
            dates = {
                path: BASE_DATE - timedelta(days=tracker.TRIAGE_THRESHOLD_DAYS + 1)
                if index >= 4
                else BASE_DATE
                for index, path in enumerate(malformed)
            }
            board = build_fixture_board(fixture, tracker.GitFacts(tuple(dates.items()), frozenset()))

            self.assertEqual([ticket.number for ticket in board.frontier], [7])
            self.assertEqual([ticket.number for ticket in board.decisions_owed], [8])
            self.assertEqual([ticket.number for ticket in board.triage_owed], [9])
            self.assertEqual([ticket.number for ticket in board.standing], list(range(1, 10)))
            offered = [ticket.path for ticket in board.frontier + board.decisions_owed + board.triage_owed]
            for path in malformed[:6]:
                self.assertNotIn(path, offered)

    def test_duplicate_numbers_find_later_file_and_resolve_references_to_first(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            first = fixture.add("alpha", 1, "First", "resolved", "none")
            duplicate = fixture.root / ".scratch" / "alpha" / "issues" / "01-second.md"
            duplicate.write_text("# 01: Second\n\nBlocked by: none\n\nStatus: ready-for-agent\n", encoding="utf-8")
            reference = fixture.add("alpha", 2, "Reference", "ready-for-agent", "ticket 01")
            board = build_fixture_board(fixture, make_facts((first, duplicate.relative_to(fixture.root), reference)))

            finding = next(item for item in board.findings if item.path == duplicate.relative_to(fixture.root))
            self.assertEqual(finding.line, 1)
            self.assertIn(str(first), finding.problem)
            self.assertEqual(
                finding.fix,
                "Renumber one of the two files to the next free number in the effort and record its former identity in the first line of its body",
            )
            rendered = tracker.render_markdown(board)
            self.assertIn("| 02 | Reference | ready-for-agent | ~~alpha 01~~ |", rendered)

        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            alpha = fixture.add("alpha", 1, "Alpha", "ready-for-agent", "none")
            beta = fixture.add("beta", 1, "Beta", "ready-for-agent", "none")
            board = build_fixture_board(fixture, make_facts((alpha, beta)))
            self.assertFalse(any("duplicates" in item.problem for item in board.findings))


class RenderingAndCLI(TestCase):
    """The in-process command writes Markdown and keeps refusal behavior explicit."""

    def test_markdown_is_byte_stable_and_strict_only_changes_exit_code(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            path = fixture.add("alpha", 1, "Stable", "ready-for-agent", "none")
            facts = make_facts((path,))
            board = build_fixture_board(fixture, facts)
            first = tracker.render_markdown(board)
            second = tracker.render_markdown(build_fixture_board(fixture, facts))
            self.assertEqual(first, second)
            self.assertIn(tracker.NO_RELEASE_TICKET, first)
            code, stdout, stderr = run_cli(
                ["--root", str(fixture.root), "--today", BASE_DATE.isoformat(), "--strict"],
                fixture.root,
            )
            self.assertEqual(code, 0)
            self.assertIn(tracker.summary_line(board), stdout)
            self.assertEqual(stderr, "")
            self.assertIn("## Standing (0)\nnone", tracker.render_markdown(board))

            bad_path = fixture.add("alpha", 2, "Bad", "invented", "none")
            code, stdout, stderr = run_cli(
                ["--root", str(fixture.root), "--today", BASE_DATE.isoformat(), "--strict"],
                fixture.root,
            )
            self.assertEqual(code, 1)
            self.assertIn("outside the vocabulary", stdout)
            strict_board = build_fixture_board(fixture, make_facts((path, bad_path)))
            self.assertIn(tracker.summary_line(strict_board), stdout)
            self.assertEqual(stderr, "")

    def test_markdown_with_standing_rows_is_byte_stable(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            ordinary = fixture.add("alpha", 1, "Ordinary", "ready-for-agent", "none")
            standing = fixture.add("standing", 2, "Standing", "ready-for-agent", "external event")
            facts = make_facts((ordinary, standing))
            first = tracker.render_markdown(build_fixture_board(fixture, facts))
            second = tracker.render_markdown(build_fixture_board(fixture, facts))

            self.assertEqual(first, second)
            self.assertIn("## Standing (1)", first)
            self.assertIn("| 02 | Standing | ready-for-agent | external |", first)

    def test_html_is_stable_self_contained_escaped_and_linked(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            dependency = fixture.add("alpha", 1, "Dependency", "ready-for-agent", "none")
            frontier = fixture.add("gamma", 2, "Frontier", "ready-for-agent", "none")
            decision = fixture.add("beta", 2, "Decision", "ready-for-human", "none")
            triage = fixture.add("gamma", 3, "Triage", "needs-triage", "none")
            release = fixture.add(
                "beta",
                4,
                "Release & <edge>",
                "ready-for-agent",
                "alpha ticket 01",
                tag="v0.2.0",
            )
            standing = fixture.add("standing", 5, "Standing", "ready-for-agent", "external event")
            paths = (dependency, frontier, decision, triage, release, standing)
            facts = tracker.GitFacts(
                tuple((path, BASE_DATE - timedelta(days=10 + index)) for index, path in enumerate(paths)),
                frozenset(),
            )
            first_board = build_fixture_board(fixture, facts)
            second_board = build_fixture_board(fixture, facts)
            first = tracker.render_html(first_board)
            second = tracker.render_html(second_board)

            self.assertEqual(first, second)
            self.assertIn(":root {", first)
            self.assertIn("@media (prefers-color-scheme: dark)", first)
            # Four cross-effort sections with rows, one table per effort, and standing.
            self.assertEqual(first.count("<table>"), 4 + len(first_board.listing) + 1)
            self.assertEqual(first.count("<h3>"), len(first_board.listing))
            for heading in (
                "Lint findings",
                "Critical path to v0.2.0",
                "Frontier",
                "Decisions owed",
                "Triage owed (past 7 days)",
                "Open by effort",
                "Standing (1)",
            ):
                self.assertIn(f"<h2>{heading}", first)
            self.assertIn("Release &amp; &lt;edge&gt;", first)
            self.assertNotIn("Release & <edge>", first)
            section_bodies = dict(
                re.findall(r"<section>\n<h2>([^<]+)</h2>(.*?)</section>", first, re.DOTALL)
            )
            self.assertEqual(section_bodies["Frontier"].count("<th>"), 3)
            self.assertNotIn("data-status", section_bodies["Frontier"])
            for heading in (
                "Critical path to v0.2.0 — beta ticket 04",
                "Decisions owed",
                "Triage owed (past 7 days)",
            ):
                self.assertEqual(section_bodies[heading].count("<th>"), 5)
                self.assertEqual(section_bodies[heading].count("data-status"), 1)
            for path in paths:
                href = path.relative_to(Path(".scratch")).as_posix()
                self.assertIn(f'href="{href}"', first)
            lowered = first.lower()
            for forbidden in ("<script", "<link", "src=", "http"):
                self.assertNotIn(forbidden, lowered)
            self.assertIn(tracker.CRITICAL_MARK_NOTE, first)

    def test_main_writes_both_files_and_page_refusal_writes_neither(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            fixture.add("alpha", 1, "Valid", "ready-for-agent", "none")
            code, stdout, stderr = run_cli(
                ["--root", str(fixture.root), "--today", BASE_DATE.isoformat()],
                fixture.root,
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("1 open across 1 efforts", stdout)
            self.assertTrue((fixture.root / tracker.BOARD_PATH).is_file())
            self.assertTrue((fixture.root / tracker.PAGE_PATH).is_file())

        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            fixture.add("alpha", 1, "Valid", "ready-for-agent", "none")
            page_path = fixture.root / tracker.PAGE_PATH
            page_path.mkdir()
            code, stdout, stderr = run_cli([], fixture.root)
            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertTrue(stderr.startswith("python3 -m tools.tracker: "))
            self.assertIn("cannot write the page", stderr)
            self.assertFalse((fixture.root / tracker.BOARD_PATH).is_file())
            self.assertFalse(page_path.is_file())

    def test_check_writes_neither_output_and_preserves_strict_behavior(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            valid_path = fixture.add("alpha", 1, "Valid", "ready-for-agent", "none")
            board_path = fixture.root / tracker.BOARD_PATH
            page_path = fixture.root / tracker.PAGE_PATH
            board_path.write_bytes(b"sentinel board\n")

            code, stdout, stderr = run_cli(["--check"], fixture.root)

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn(
                tracker.summary_line(build_fixture_board(fixture, make_facts((valid_path,)))),
                stdout,
            )
            self.assertEqual(board_path.read_bytes(), b"sentinel board\n")
            self.assertFalse(page_path.exists())

            bad_path = fixture.add("alpha", 2, "Invented", "invented", "none")
            code, stdout, stderr = run_cli(["--check", "--strict"], fixture.root)

            self.assertEqual(code, 1)
            self.assertEqual(stderr, "")
            self.assertIn("is outside the vocabulary", stdout)
            self.assertIn(
                tracker.summary_line(
                    build_fixture_board(fixture, make_facts((valid_path, bad_path)))
                ),
                stdout,
            )
            self.assertEqual(board_path.read_bytes(), b"sentinel board\n")
            self.assertFalse(page_path.exists())

    def test_three_refusals_write_nothing_and_print_one_refusal_shape(self) -> None:
        cases: tuple[tuple[str, str], ...] = (
            ("no effort", "no effort with an issues/ directory"),
            ("missing vocabulary", "no single machine-readable Statuses"),
            ("unwritable board", "cannot write the board"),
        )
        for name, expected_problem in cases:
            with self.subTest(name=name), TemporaryDirectory() as directory:
                root = Path(directory)
                if name == "no effort":
                    (root / "docs" / "agents").mkdir(parents=True)
                    (root / "docs" / "agents" / "issue-tracker.md").write_text(
                        VOCABULARY_LINE + "\n", encoding="utf-8"
                    )
                else:
                    fixture = Fixture(root)
                    if name == "missing vocabulary":
                        (root / "docs" / "agents" / "issue-tracker.md").write_text(
                            "# malformed\n", encoding="utf-8"
                        )
                    else:
                        fixture.add("alpha", 1, "Valid", "ready-for-agent", "none")
                        (root / ".scratch" / "BOARD.md").mkdir()
                code, stdout, stderr = run_cli([], root)
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertTrue(stderr.startswith("python3 -m tools.tracker: "))
                self.assertIn(expected_problem, stderr)
                self.assertRegex(stderr, r" Fix: .+\n$")
                self.assertFalse((root / ".scratch" / "BOARD.md").is_file())


class GitFactsReader(TestCase):
    """Git dates follow renames and all git failures produce absent facts."""

    def test_real_repository_add_modify_rename(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            issue_dir = root / ".scratch" / "alpha" / "issues"
            issue_dir.mkdir(parents=True)
            old = issue_dir / "01-old.md"
            modified = issue_dir / "02-modified.md"
            old.write_text("# 01: Old\nStatus: resolved\n", encoding="utf-8")
            modified.write_text("# 02: Modified\nStatus: resolved\n", encoding="utf-8")
            git(root, "init", "-q")
            git(root, "config", "user.name", "Fixture Author")
            git(root, "config", "user.email", "fixture@example.invalid")
            git(root, "add", ".scratch")
            commit(root, "initial", "2026-01-02T12:00:00+0000")
            modified.write_text("# 02: Modified\nStatus: resolved\nchanged\n", encoding="utf-8")
            git(root, "mv", str(old.relative_to(root)), str((issue_dir / "01-renamed.md").relative_to(root)))
            git(root, "commit", "-m", "rename", "--no-verify")
            facts = tracker.read_git_facts(root)
            self.assertIsNotNone(facts)
            assert facts is not None
            self.assertEqual(
                facts.opened(issue_dir.relative_to(root) / "01-renamed.md"), date(2026, 1, 2)
            )
            self.assertNotIn(issue_dir.relative_to(root) / "01-old.md", dict(facts.opened_dates))
            self.assertIn(modified.relative_to(root), facts.uncommitted_paths)

    def test_plain_directory_has_absent_facts(self) -> None:
        with TemporaryDirectory() as directory:
            self.assertIsNone(tracker.read_git_facts(Path(directory)))


def git(root: Path, *arguments: str) -> None:
    result = subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr)


def commit(root: Path, message: str, timestamp: str) -> None:
    environment = {
        "GIT_AUTHOR_DATE": timestamp,
        "GIT_COMMITTER_DATE": timestamp,
    }
    result = subprocess.run(
        ["git", "commit", "-m", message, "--no-verify"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env={**dict(os.environ), **environment},
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)


class DocumentsAndBoundary(TestCase):
    """The tool's source and its two document couplings stay aligned."""

    def test_real_tracker_and_status_documents(self) -> None:
        if in_export_tree(ROOT):
            self.skipTest("the real tracker is excluded from the public export")
        vocabulary = tracker.read_vocabulary(ROOT)
        self.assertEqual(len(vocabulary.open_statuses), 5)
        self.assertEqual(len(vocabulary.closed_statuses), 2)
        board = tracker.build_board(ROOT)
        self.assertEqual(board.vocabulary, vocabulary)
        self.assertGreater(board.total_tickets, 0)
        # The standing directory holds at least the two tickets moved at v0.1.33, every one
        # waiting; the standing rules and the shared-number rule are silent over the real tree.
        # Rule 4 is not asserted: a cycle's own claim is uncommitted in its worktree until release.
        self.assertGreaterEqual(len(board.standing), 2)
        self.assertTrue(all(ticket.external_blocker and not ticket.fired for ticket in board.standing))
        self.assertFalse(any("standing ticket" in item.problem for item in board.findings))
        self.assertFalse(any("duplicates" in item.problem for item in board.findings))
        board_path = ROOT / tracker.BOARD_PATH
        before = board_path.read_bytes()
        code, stdout, stderr = run_cli(["--check"], ROOT)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn(tracker.summary_line(board), stdout)
        self.assertEqual(before, board_path.read_bytes())
        text = (ROOT / "docs" / "agents" / "triage-labels.md").read_text(encoding="utf-8")
        right_hand = {
            cells[1].strip().strip("`")
            for line in text.splitlines()
            if line.startswith("|")
            for cells in [line.strip().strip("|").split("|")]
            if len(cells) == 3 and cells[1].strip().startswith("`")
        }
        self.assertEqual(len(right_hand), 5)
        self.assertTrue(right_hand.issubset(set(vocabulary.open_statuses + vocabulary.closed_statuses)))
        self.assertIn("issue-tracker.md", text)
        self.assertIn("Vocabulary section", text)

    def test_threshold_is_derived_from_the_board_bullet(self) -> None:
        text = (ROOT / "docs" / "agents" / "issue-tracker.md").read_text(encoding="utf-8")
        match = re.search(r"triage-owed section lists .*? past (\d+) days", text)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(tracker.TRIAGE_THRESHOLD_DAYS, int(match.group(1)))

        standing_match = re.search(
            r"The directory is `(?P<path>\.scratch/[^`]+/)`; `tests/test_tracker\.py` holds the tool's constant",
            text,
        )
        self.assertIsNotNone(standing_match)
        assert standing_match is not None
        self.assertEqual(tracker.STANDING_EFFORT, Path(standing_match.group("path")).parts[1])

    def test_tracker_imports_only_standard_library_modules(self) -> None:
        path = ROOT / "tools" / "tracker.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                targets = [node.module or ""]
            else:
                continue
            for target in targets:
                self.assertIn(
                    target.partition(".")[0],
                    sys.stdlib_module_names,
                    f"{path}:{node.lineno} imports {target}",
                )
