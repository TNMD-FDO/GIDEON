"""Contracts for the content-free guardrail review section."""

import re
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from gideon.guardrail import writer
from gideon.host.report import Problem
from gideon.host.sysio import Host
from gideon.improvement import ratings, trips
from gideon.improvement.feedback import FeedbackReading, FeedbackRecord
from gideon.improvement.sections import READ_FIX, Context, SectionReport
from gideon.improvement.triggers import TriggerRegistry

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2099, 4, 15, 12, tzinfo=UTC).timestamp()
TRIP_LINES = (
    "fictional-family|fictional-pattern-a|fictional-chat-a|2",
    "fictional-family|fictional-pattern-b||4",
)
TRIPS = (
    trips.TripCount("fictional-family", "fictional-pattern-a", "fictional-chat-a", 2),
    trips.TripCount("fictional-family", "fictional-pattern-b", "fictional-chat-a", 1),
    trips.TripCount("fictional-family", "fictional-pattern-c", "fictional-chat-b", 3),
    trips.TripCount("fictional-family", "fictional-pattern-d", None, 4),
    trips.TripCount("fictional-family", "fictional-pattern-e", "fictional-chat-e", 1),
)


def _record(
    rating: Literal["up", "down"], chat_id: str, message_id: str, created_at: int
) -> FeedbackRecord:
    return FeedbackRecord(
        rating,
        chat_id,
        message_id,
        "fictional-model",
        created_at,
    )


def _reading() -> FeedbackReading:
    cutoff = NOW - ratings.FEEDBACK_WINDOW_DAYS * 24 * 60 * 60
    return FeedbackReading(
        (
            _record("down", "fictional-chat-a", "fictional-message-b", int(NOW - 10)),
            _record("down", "fictional-chat-c", "fictional-message-c", int(NOW - 2)),
            _record("down", "fictional-chat-a", "fictional-message-a", int(NOW - 20)),
            _record("down", "fictional-chat-a", "fictional-message-old", int(cutoff - 1)),
            _record("up", "fictional-chat-a", "fictional-message-up", int(NOW - 1)),
        ),
        skipped=2,
    )


def _context(
    query: Callable[[str], object],
    feedback: Callable[[], object],
    *,
    now: float = NOW,
) -> Context:
    return Context(
        host=cast(Host, object()),
        checkout_root=ROOT,
        rendered_dir=Path("/tmp/fictional-rendered"),
        registry=cast(TriggerRegistry, None),
        build_box=False,
        query=cast(Callable[[str], tuple[str, ...] | Problem], query),
        feedback=cast(Callable[[], FeedbackReading | Problem], feedback),
        now=lambda: now,
    )


class GuardrailTrips(unittest.TestCase):
    def test_statement_uses_fixed_utc_cutoff_and_writer_constants(self) -> None:
        fixed_clock = datetime.fromtimestamp(NOW, UTC)
        cutoff = (fixed_clock - timedelta(days=ratings.FEEDBACK_WINDOW_DAYS)).strftime(
            ratings.TIME_FORMAT
        )
        query = trips.statement(NOW)

        self.assertEqual(query.count(cutoff), 1)
        self.assertIn(writer.GUARDRAIL_TRIPS_TABLE, query)
        self.assertIn(f"source = '{writer.SOURCE_VOCABULARY[0]}'", query)
        self.assertIn("GROUP BY family, pattern_id, chat_id", query)
        self.assertIn("ORDER BY family, pattern_id, chat_id", query)

    def test_parser_reads_group_columns_and_empty_chat_id(self) -> None:
        parsed = trips.parse_rows(TRIP_LINES)

        self.assertEqual(
            parsed,
            (
                trips.TripCount(
                    "fictional-family", "fictional-pattern-a", "fictional-chat-a", 2
                ),
                trips.TripCount("fictional-family", "fictional-pattern-b", None, 4),
            ),
        )

    def test_parser_counts_malformed_rows_without_quoting_them(self) -> None:
        malformed = "FICTIONAL_READER_SENTINEL|not-a-row"
        result = trips.parse_rows(
            (TRIP_LINES[0], malformed, "fictional-family|fictional-pattern|fictional-chat|x")
        )

        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertEqual(result.problem, trips.PARSE_PROBLEM.format(count=2))
        self.assertEqual(result.fix, READ_FIX)
        self.assertNotIn(malformed, result.problem)
        self.assertNotIn(malformed, result.fix)

    def test_review_joins_recent_down_ratings_by_chat_id(self) -> None:
        result = trips.review(TRIPS, _reading(), NOW)

        self.assertEqual(
            result.patterns,
            (
                trips.PatternTrips("fictional-pattern-a", 2, 2),
                trips.PatternTrips("fictional-pattern-b", 1, 2),
                trips.PatternTrips("fictional-pattern-c", 3, 0),
                trips.PatternTrips("fictional-pattern-d", 4, 0),
                trips.PatternTrips("fictional-pattern-e", 1, 0),
            ),
        )
        self.assertEqual(
            result.reports,
            (
                trips.Report(
                    "fictional-chat-a",
                    "fictional-message-a",
                    int(NOW - 20),
                    ("fictional-pattern-a", "fictional-pattern-b"),
                ),
                trips.Report(
                    "fictional-chat-a",
                    "fictional-message-b",
                    int(NOW - 10),
                    ("fictional-pattern-a", "fictional-pattern-b"),
                ),
            ),
        )
        self.assertEqual((result.trips, result.elsewhere, result.unkeyed), (11, 1, 4))

    def test_section_renders_ordered_content_free_rows(self) -> None:
        context = _context(lambda _sql: TRIP_LINES, _reading)
        rendered = trips.TRIPS_SECTION.render(context)

        self.assertIsInstance(rendered, SectionReport)
        assert isinstance(rendered, SectionReport)
        self.assertEqual(trips.TRIPS_SECTION.name, "guardrail")
        self.assertEqual(trips.TRIPS_SECTION.scope, "office")
        self.assertEqual(
            rendered.detail,
            f"6 trips in the last {ratings.FEEDBACK_WINDOW_DAYS} days, "
            "2 rated down in tripped chats, "
            "1 rated down in other chats, 4 trips without a chat id, 2 unreadable",
        )
        self.assertEqual(tuple(row.name for row in rendered.rows), (
            "fictional-pattern-a",
            "fictional-pattern-b",
            "fictional-chat-a",
            "fictional-chat-a",
        ))
        self.assertEqual(
            tuple(row.state for row in rendered.rows),
            ("rated", "not fired", "rated", "rated"),
        )

    def test_review_rows_have_expected_names_details_and_states(self) -> None:
        query_rows = tuple(
            f"{item.family}|{item.pattern_id}|{item.chat_id or ''}|{item.trips}"
            for item in TRIPS
        )
        reading = _reading()
        context = _context(lambda _sql: query_rows, lambda: reading)
        rendered = trips.TRIPS_SECTION.render(context)

        self.assertIsInstance(rendered, SectionReport)
        assert isinstance(rendered, SectionReport)
        self.assertEqual(
            tuple((row.name, row.state, row.detail) for row in rendered.rows),
            (
                ("fictional-pattern-a", "rated", "2 trips, 2 rated down"),
                ("fictional-pattern-b", "rated", "1 trips, 2 rated down"),
                ("fictional-pattern-c", "not fired", "3 trips, 0 rated down"),
                ("fictional-pattern-d", "not fired", "4 trips, 0 rated down"),
                ("fictional-pattern-e", "not fired", "1 trips, 0 rated down"),
                (
                    "fictional-chat-a",
                    "rated",
                    "message fictional-message-a, down "
                    f"{datetime.fromtimestamp(NOW - 20, UTC).strftime(ratings.TIME_FORMAT)}, "
                    "fictional-pattern-a, fictional-pattern-b",
                ),
                (
                    "fictional-chat-a",
                    "rated",
                    "message fictional-message-b, down "
                    f"{datetime.fromtimestamp(NOW - 10, UTC).strftime(ratings.TIME_FORMAT)}, "
                    "fictional-pattern-a, fictional-pattern-b",
                ),
            ),
        )
        self.assertEqual(
            {row.state for row in rendered.rows},
            {ratings.RATED_STATE, trips.NOT_FIRED_STATE},
        )

    def test_rows_and_statement_never_include_reading_text(self) -> None:
        rows = tuple(
            f"{item.family}|{item.pattern_id}|{item.chat_id or ''}|{item.trips}"
            for item in TRIPS
        )
        reading = FeedbackReading(
            (
                _record(
                    "down",
                    "fictional-chat-a",
                    "fictional-reading-sentinel-4d8a",
                    int(NOW - 3),
                ),
            ),
            0,
        )
        context = _context(lambda _sql: rows, lambda: reading)
        rendered = trips.TRIPS_SECTION.render(context)
        self.assertIsInstance(rendered, SectionReport)
        assert isinstance(rendered, SectionReport)

        name_pattern = re.compile(r"[A-Za-z0-9_./@-]+")
        pattern_detail = re.compile(r"[0-9]+ trips, [0-9]+ rated down")
        report_detail = re.compile(
            r"message [A-Za-z0-9_-]+, down [0-9]{4}-[0-9]{2}-[0-9]{2}T"
            r"[0-9]{2}:[0-9]{2}:[0-9]{2}Z, [A-Za-z0-9_./@-]+(?:, [A-Za-z0-9_./@-]+)*"
        )
        header_detail = re.compile(
            r"[0-9]+ trips in the last [0-9]+ days, [0-9]+ rated down in tripped chats, "
            r"[0-9]+ rated down in other chats, [0-9]+ trips without a chat id"
        )
        self.assertIsNotNone(header_detail.fullmatch(rendered.detail))
        for row in rendered.rows:
            self.assertIsNotNone(name_pattern.fullmatch(row.name), row.name)
            detail = report_detail if row.name.startswith("fictional-chat") else pattern_detail
            self.assertIsNotNone(detail.fullmatch(row.detail), row.detail)
        query = trips.statement(NOW)
        self.assertNotIn("fictional-reading-sentinel-4d8a", query)
        self.assertNotIn("fictional-chat-a", query)

    def test_section_returns_each_read_problem_and_skips_feedback_after_query_failure(self) -> None:
        query_problem = Problem("metrics reader failed with exit code 1", "Fix the fictional reader.")
        query_calls = 0
        feedback_calls = 0

        def refused_query(_sql: str) -> tuple[str, ...] | Problem:
            nonlocal query_calls
            query_calls += 1
            return query_problem

        def feedback_after_query_refusal() -> FeedbackReading | Problem:
            nonlocal feedback_calls
            feedback_calls += 1
            return _reading()

        result = trips.TRIPS_SECTION.render(
            _context(refused_query, feedback_after_query_refusal)
        )
        self.assertIs(result, query_problem)
        self.assertEqual(query_calls, 1)
        self.assertEqual(feedback_calls, 0)

        feedback_problem = Problem("frontend is unavailable", "Fix the fictional frontend.")
        query_calls = 0
        feedback_calls = 0

        def successful_query(_sql: str) -> tuple[str, ...]:
            nonlocal query_calls
            query_calls += 1
            return ()

        def refused_feedback() -> FeedbackReading | Problem:
            nonlocal feedback_calls
            feedback_calls += 1
            return feedback_problem

        result = trips.TRIPS_SECTION.render(
            _context(successful_query, refused_feedback)
        )
        self.assertIs(result, feedback_problem)
        self.assertEqual(query_calls, 1)
        self.assertEqual(feedback_calls, 1)


if __name__ == "__main__":
    unittest.main()
