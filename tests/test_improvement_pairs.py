"""Contracts for the content-free challenger section and its paired figures."""

import re
import unittest
from collections.abc import Mapping
from dataclasses import replace
from typing import cast
from unittest.mock import patch
from uuid import UUID

from test_improvement_proposals import RENDERED, ROOT, ReadOnlyHost, _reader_argv

from gideon.evaluation import challenger, decision, evalset
from gideon.evaluation.results import JSONValue
from gideon.host.report import Problem
from gideon.host.sysio import Host
from gideon.improvement import pairs
from gideon.improvement.feedback import FeedbackReading
from gideon.improvement.sections import Context, Row, SectionReport, read_fix, read_rows
from gideon.improvement.triggers import TriggerRegistry

CHALLENGER_FILE = ROOT / challenger.CHALLENGER_PATH
CHALLENGER_TEXT = CHALLENGER_FILE.read_text(encoding="utf-8")
LOADED = challenger.load_challenger(CHALLENGER_FILE)
assert LOADED.config is not None and LOADED.config.challenger is not None
ENTRY = LOADED.config.challenger
START = "2099-01-02T03:04:05Z"
PRODUCT_VERSION = "1000.0.0"
SET_VERSION = "eval-v1000"
DIGEST = "a" * 64


def _id(number: int) -> str:
    return str(UUID(int=number))


def _pair(number: int = 1, *, partner: bool = True) -> pairs.PairRow:
    return pairs.PairRow(
        _id(number),
        START,
        "nightly",
        PRODUCT_VERSION,
        SET_VERSION,
        DIGEST,
        1,
        False,
        ENTRY.release,
        _id(number + 100) if partner else None,
        False if partner else None,
        ENTRY.challenger if partner else None,
    )


def _pair_line(pair: pairs.PairRow) -> str:
    return "|".join(
        (
            pair.release_id,
            pair.start,
            pair.kind,
            pair.product_version,
            pair.set_version,
            pair.set_digest,
            str(pair.repeats),
            "t" if pair.release_partial else "f",
            pair.release_value,
            pair.partner_id or "",
            ""
            if pair.partner_partial is None
            else "t"
            if pair.partner_partial
            else "f",
            pair.partner_value or "",
        )
    )


def _result(
    run_id: str, case_id: str, in_band: bool | None, *, repeat: int = 1
) -> pairs.ResultRow:
    return pairs.ResultRow(run_id, case_id, repeat, in_band, in_band is None, 3, 2)


def _result_line(row: pairs.ResultRow) -> str:
    return "|".join(
        (
            row.run_id,
            row.case_id,
            str(row.repeat),
            "" if row.in_band is None else "true" if row.in_band else "false",
            "t" if row.failed else "f",
            "" if row.prompt_tokens is None else str(row.prompt_tokens),
            "" if row.completion_tokens is None else str(row.completion_tokens),
        )
    )


def _side_results(
    pair: pairs.PairRow,
    release: tuple[bool | None, ...],
    candidate: tuple[bool | None, ...],
) -> tuple[pairs.ResultRow, ...]:
    assert pair.partner_id is not None
    return tuple(
        _result(run_id, f"judge-{index:03d}", flag)
        for run_id, flags in ((pair.release_id, release), (pair.partner_id, candidate))
        for index, flag in enumerate(flags, 1)
    )


def _context(
    host: ReadOnlyHost,
    *,
    pair_lines: tuple[str, ...] = (),
    result_lines: tuple[str, ...] = (),
) -> Context:
    def query(sql: str) -> tuple[str, ...] | Problem:
        host.run_stdout = "\n".join(
            pair_lines if "FROM eval_runs" in sql else result_lines
        )
        return read_rows(cast(Host, host), RENDERED, sql)

    return Context(
        host=cast(Host, host),
        checkout_root=ROOT,
        rendered_dir=RENDERED,
        registry=cast(TriggerRegistry, None),
        build_box=True,
        query=query,
        feedback=lambda: FeedbackReading((), 0),
        now=lambda: 0.0,
    )


def _host(text: str | None = CHALLENGER_TEXT) -> ReadOnlyHost:
    return ReadOnlyHost(files={} if text is None else {str(CHALLENGER_FILE): text})


class StatementsAndParsers(unittest.TestCase):
    def test_statements_use_validated_identifiers_and_select_only_figures(self) -> None:
        pair_sql = pairs.pairs_statement(ENTRY.name)
        self.assertEqual(pair_sql.count(f"= '{ENTRY.name}'"), 2)
        for field in (
            challenger.OVERRIDE_KEY,
            challenger.NAME_FIELD,
            challenger.SIDE_FIELD,
            challenger.VALUE_FIELD,
            challenger.PAIRS_FIELD,
            challenger.RELEASE_SIDE,
            challenger.CHALLENGER_SIDE,
        ):
            self.assertIn(f"'{field}'", pair_sql)
        self.assertIn("LEFT JOIN LATERAL", pair_sql)
        self.assertIn("LIMIT 1", pair_sql)
        self.assertIn("AT TIME ZONE 'UTC'", pair_sql)
        self.assertIn("ORDER BY release_run.started_at DESC", pair_sql)

        result_sql = pairs.results_statement((_id(2), _id(1), _id(1)))
        self.assertEqual(result_sql.count(f"'{_id(1)}'::uuid"), 1)
        self.assertEqual(result_sql.count(f"'{_id(2)}'::uuid"), 1)
        self.assertIn("judge->>'in_band'", result_sql)
        self.assertIn("judge ? 'failed'", result_sql)
        self.assertIn("metrics->>'prompt_tokens'", result_sql)
        self.assertIn("metrics->>'completion_tokens'", result_sql)
        self.assertIn("ORDER BY run_id, case_id, repeat", result_sql)
        for sql in (pair_sql, result_sql):
            for forbidden in (
                "reason",
                "prompt",
                "detail",
                "score",
                "band",
                "failure_mode",
            ):
                self.assertIsNone(re.search(rf"\b{forbidden}\b", sql), sql)
            self.assertIsNone(re.search(r"\bjudge\b(?!\s*(?:->|\?))", sql), sql)
        with self.assertRaises(ValueError):
            pairs.pairs_statement("fictional'; DROP TABLE eval_runs; --")
        with self.assertRaises(ValueError):
            pairs.results_statement((_id(1), "fictional-not-a-uuid"))

    def test_pair_parser_accepts_empty_partner_and_refuses_each_unreadable_kind(
        self,
    ) -> None:
        pair = _pair()
        self.assertEqual(
            pairs.parse_pairs((_pair_line(pair), _pair_line(_pair(partner=False)))),
            (pair, _pair(partner=False)),
        )
        valid = _pair_line(pair).split("|")
        changes = (
            (0, "FICTIONAL_BAD_UUID"),
            (1, "FICTIONAL_BAD_TIME"),
            (2, "FICTIONAL_BAD_KIND"),
            (3, "FICTIONAL BAD VERSION"),
            (4, "FICTIONAL BAD VERSION"),
            (5, "FICTIONAL_BAD_DIGEST"),
            (6, "0"),
            (7, "FICTIONAL_BAD_BOOL"),
            (8, "FICTIONAL BAD VALUE"),
            (9, "FICTIONAL_BAD_UUID"),
            (10, "FICTIONAL_BAD_BOOL"),
            (11, "FICTIONAL BAD VALUE"),
        )
        malformed: list[str] = ["FICTIONAL_READER_SENTINEL"]
        for column, value in changes:
            altered = valid.copy()
            altered[column] = value
            malformed.append("|".join(altered))
        result = pairs.parse_pairs((_pair_line(pair), *malformed))
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertEqual(
            result.problem, pairs.PARSE_PAIRS_PROBLEM.format(count=len(malformed))
        )
        self.assertEqual(result.fix, read_fix())
        self.assertNotIn("FICTIONAL_", result.problem + result.fix)

    def test_result_parser_accepts_failed_and_null_tokens_and_counts_bad_rows(
        self,
    ) -> None:
        good = _result(_id(1), "judge-001", True)
        failed = pairs.ResultRow(_id(1), "judge-002", 1, None, True, None, None)
        self.assertEqual(
            pairs.parse_results((_result_line(good), _result_line(failed))),
            (good, failed),
        )
        valid = _result_line(good).split("|")
        changes = (
            (0, "FICTIONAL_BAD_UUID"),
            (1, "FICTIONAL BAD CASE"),
            (2, "0"),
            (3, "FICTIONAL_BAD_BOOLEAN"),
            (4, "FICTIONAL_BAD_BOOL"),
            (5, "FICTIONAL_BAD_TOKENS"),
            (6, "FICTIONAL_BAD_TOKENS"),
        )
        malformed: list[str] = ["FICTIONAL_READER_SENTINEL"]
        for column, value in changes:
            altered = valid.copy()
            altered[column] = value
            malformed.append("|".join(altered))
        missing_band = valid.copy()
        missing_band[3] = ""
        malformed.append("|".join(missing_band))
        result = pairs.parse_results((_result_line(good), *malformed))
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertEqual(
            result.problem, pairs.PARSE_RESULTS_PROBLEM.format(count=len(malformed))
        )
        self.assertEqual(result.fix, read_fix())
        self.assertNotIn("FICTIONAL_", result.problem + result.fix)


class PairFigures(unittest.TestCase):
    def test_selection_keeps_newest_five_complete_same_version_nights(self) -> None:
        newest = replace(_pair(1, partner=False), release_partial=True)
        partial_partner = replace(_pair(2), partner_partial=True)
        partial_release = replace(_pair(3), release_partial=True)
        older_version = replace(_pair(4), product_version="999.0.0")
        older_set = replace(_pair(5), set_version="eval-v999")
        complete = tuple(_pair(number) for number in range(6, 13))
        selected = pairs.select_pairs(
            (
                newest,
                partial_partner,
                partial_release,
                older_version,
                older_set,
                *complete,
            )
        )
        self.assertIs(selected.newest, newest)
        self.assertEqual(selected.pooled, complete[: pairs.POOLED_NIGHTS])
        self.assertEqual(pairs.select_pairs(()), pairs.Selection(None, ()))

    def test_night_counts_failed_grading_without_a_defined_measurement(self) -> None:
        run_id = _id(1)
        rows = (
            pairs.ResultRow(run_id, "judge-001", 1, True, False, 3, 2),
            pairs.ResultRow(run_id, "judge-001", 2, True, False, 4, 3),
            pairs.ResultRow(run_id, "judge-002", 1, True, False, 5, 4),
            pairs.ResultRow(run_id, "judge-002", 2, None, True, None, None),
        )
        self.assertEqual(
            pairs.night_figures(rows, 2), pairs.NightFigures(2, 1, 4, 1, 12, 9)
        )

    def test_pooled_statistic_uses_case_values_and_never_calls_metric_reader(
        self,
    ) -> None:
        pair = _pair()

        def forbidden_reader(_metrics: Mapping[str, JSONValue]) -> float | None:
            raise AssertionError("metric reader was called")

        metric = decision.DecisionMetric(pairs.FIGURE_NAME, True, forbidden_reader)
        with patch.object(pairs, "IN_BAND_METRIC", metric):
            for release, candidate, verdict, paired in (
                ((False, False, False), (True, True, True), decision.WINS, 3),
                ((True, True, True), (False, False, False), decision.LOSES, 3),
                ((False, True), (True, False), decision.UNDECIDED, 2),
                ((False,), (True,), decision.UNDECIDED, 1),
            ):
                with self.subTest(verdict=verdict, paired=paired):
                    outcome = pairs.pooled_decision(
                        (pair,),
                        _side_results(pair, release, candidate),
                        against=pair.release_value,
                    )
                    self.assertEqual(outcome.verdict, verdict)
                    self.assertEqual(outcome.paired, paired)
                    self.assertEqual(outcome.clusters, paired)
                    self.assertEqual(
                        (outcome.candidate_only, outcome.comparand_only), (0, 0)
                    )
                    self.assertEqual(
                        outcome.mean_difference,
                        1.0
                        if verdict == decision.WINS
                        else -1.0
                        if verdict == decision.LOSES
                        else 0.0
                        if paired == 2
                        else 1.0,
                    )
                    self.assertEqual(
                        (outcome.requested_repeats, outcome.completed_repeats),
                        (pairs.POOLED_NIGHTS, 1),
                    )
                    self.assertTrue(outcome.digest_equal)
                    self.assertIn(outcome.verdict, decision.describe(outcome))

            undefined = _side_results(pair, (False, False), (True, None))
            outcome = pairs.pooled_decision(
                (pair,), undefined, against=pair.release_value
            )
            self.assertEqual((outcome.paired, outcome.comparand_only), (1, 1))
            different = replace(_pair(2), set_digest="b" * 64)
            outcome = pairs.pooled_decision(
                (pair, different),
                undefined + _side_results(different, (False,), (True,)),
                against=pair.release_value,
            )
            self.assertFalse(outcome.digest_equal)
            self.assertEqual(outcome.completed_repeats, 2)

    def test_committed_judge_cases_each_form_their_own_cluster(self) -> None:
        result = evalset.load_set(ROOT / evalset.SET_ROOT)
        self.assertTrue(result.ok, result.findings)
        assert result.loaded is not None
        case_ids = result.loaded.slices["judge-triples"]
        self.assertTrue(case_ids)
        for case_id in case_ids:
            with self.subTest(case_id=case_id):
                self.assertEqual(
                    result.loaded.cases_by_id[case_id]["cluster_id"], case_id
                )


class SectionOverHost(unittest.TestCase):
    def test_missing_and_malformed_file_refuse_with_loader_fix_before_query(
        self,
    ) -> None:
        for text in (None, "version: 1\nchallenger: []\n"):
            with self.subTest(text=text):
                host = _host(text)
                finding = challenger.load_challenger(
                    CHALLENGER_FILE, host=cast(Host, host)
                ).findings[0]
                report = pairs.CHALLENGER_SECTION.render(_context(host))
                self.assertIsInstance(report, Problem)
                assert isinstance(report, Problem)
                self.assertIn("1 findings", report.problem)
                self.assertIn(finding.key_path, report.problem)
                self.assertIn(finding.problem, report.problem)
                self.assertEqual(report.fix, finding.fix)
                self.assertEqual(host.calls, [])

    def test_none_set_is_one_skipped_row_without_a_query(self) -> None:
        host = _host("version: 1\nchallenger: null\n")
        report = pairs.CHALLENGER_SECTION.render(_context(host))
        self.assertEqual(
            report, SectionReport("none set", (Row("newest", "skipped", "none set"),))
        )
        assert isinstance(report, SectionReport)
        self._assert_content_free(report)
        self.assertEqual(host.calls, [])
        self.assertFalse(host.write_attempted)

    def test_no_pair_and_partnerless_release_are_measurable_only_after_pairing(
        self,
    ) -> None:
        host = _host()
        report = pairs.CHALLENGER_SECTION.render(_context(host))
        self.assertIsInstance(report, SectionReport)
        assert isinstance(report, SectionReport)
        self.assertIn(ENTRY.name, report.detail)
        self.assertIn(ENTRY.subject, report.detail)
        self.assertIn(ENTRY.release, report.detail)
        self.assertIn(ENTRY.challenger, report.detail)
        self.assertEqual(
            tuple(row.state for row in report.rows),
            ("not yet measurable", "not yet measurable"),
        )
        self.assertIn("no pair recorded", report.rows[0].detail)
        self.assertEqual(report.rows[1].detail, f"0 of {pairs.POOLED_NIGHTS} nights")
        self.assertEqual(
            host.calls, [(_reader_argv(), pairs.pairs_statement(ENTRY.name))]
        )
        self._assert_content_free(report)

        newest = replace(_pair(partner=False), release_partial=True)
        host = _host()
        report = pairs.CHALLENGER_SECTION.render(
            _context(host, pair_lines=(_pair_line(newest),))
        )
        self.assertIsInstance(report, SectionReport)
        assert isinstance(report, SectionReport)
        self.assertEqual(
            tuple(row.state for row in report.rows),
            ("not yet measurable", "not yet measurable"),
        )
        self.assertIn(newest.release_id, report.rows[0].detail)
        self.assertIn(newest.release_value, report.rows[0].detail)
        self.assertIn(newest.kind, report.rows[0].detail)
        self.assertIn(newest.start, report.rows[0].detail)
        self.assertIn("no challenger row pairs it, partial", report.rows[0].detail)
        self.assertEqual(len(host.calls), 1)
        self.assertEqual(
            host.calls[0], (_reader_argv(), pairs.pairs_statement(ENTRY.name))
        )
        self._assert_content_free(report, newest)

    def test_recorded_pair_reads_both_statements_and_reports_figures_only(self) -> None:
        pair = _pair()
        results = _side_results(pair, (False, False, False), (True, True, True))
        host = _host()
        report = pairs.CHALLENGER_SECTION.render(
            _context(
                host,
                pair_lines=(_pair_line(pair),),
                result_lines=tuple(_result_line(row) for row in results),
            )
        )
        self.assertIsInstance(report, SectionReport)
        assert isinstance(report, SectionReport)
        self.assertEqual(
            host.calls,
            [
                (_reader_argv(), pairs.pairs_statement(ENTRY.name)),
                (
                    _reader_argv(),
                    pairs.results_statement(
                        (pair.release_id, cast(str, pair.partner_id))
                    ),
                ),
            ],
        )
        self.assertEqual(
            tuple(row.name for row in report.rows), (pairs.NEWEST_ROW, pairs.POOLED_ROW)
        )
        self.assertEqual(
            tuple(row.state for row in report.rows), ("not fired", "fired")
        )
        newest, pooled = report.rows
        assert pair.partner_id is not None
        self.assertLess(
            newest.detail.index(pair.release_id), newest.detail.index(pair.partner_id)
        )
        self.assertIn(
            f"release {pair.release_id}, value {ENTRY.release}, in band 0 of 3 cases",
            newest.detail,
        )
        self.assertIn(
            f"challenger {pair.partner_id}, value {ENTRY.challenger}, in band 3 of 3 cases",
            newest.detail,
        )
        self.assertIn(
            "gradings 3, failed 0, prompt tokens 9, completion tokens 6", newest.detail
        )
        self.assertIn(f"kind {pair.kind}, start {pair.start}", newest.detail)
        self.assertIn(
            f"in_band over 1 of {pairs.POOLED_NIGHTS} nights: ", pooled.detail
        )
        self.assertIn(decision.WINS, pooled.detail)
        self.assertFalse(host.write_attempted)
        self._assert_content_free(report, pair)

    def test_pooled_row_states_cover_loss_tie_and_insufficient_cases(self) -> None:
        pair = _pair()
        for release, candidate, state, verdict in (
            ((True, True, True), (False, False, False), "not fired", decision.LOSES),
            ((False, True), (True, False), "not fired", decision.UNDECIDED),
            ((False,), (True,), "not yet measurable", decision.UNDECIDED),
        ):
            with self.subTest(state=state, verdict=verdict, cases=len(release)):
                results = _side_results(pair, release, candidate)
                host = _host()
                report = pairs.CHALLENGER_SECTION.render(
                    _context(
                        host,
                        pair_lines=(_pair_line(pair),),
                        result_lines=tuple(_result_line(row) for row in results),
                    )
                )
                self.assertIsInstance(report, SectionReport)
                assert isinstance(report, SectionReport)
                self.assertEqual(report.rows[1].state, state)
                self.assertIn(verdict, report.rows[1].detail)
                self._assert_content_free(report, pair)

    def _assert_content_free(
        self, report: SectionReport, pair: pairs.PairRow | None = None
    ) -> None:
        allowed = {ENTRY.name, ENTRY.subject, ENTRY.release, ENTRY.challenger}
        if pair is not None:
            allowed.update(
                value
                for value in (pair.release_id, pair.partner_id, pair.kind, pair.start)
                if value is not None
            )
        text = "\n".join(
            (report.detail, *(row.name + " " + row.detail for row in report.rows))
        )
        for value in allowed:
            if value is not None:
                text = text.replace(value, "")
        self.assertIsNone(re.search(r"\b(?:reason|detail|score|failure_mode)\b", text))
        self.assertNotIn("FICTIONAL_READER_SENTINEL", text)
        self.assertRegex(text, r"^[A-Za-z0-9_ .,;:+%\[\]()/−\n-]*$")
        labels = {
            "release",
            "challenger",
            "nights",
            "pooled",
            "newest",
            "none",
            "set",
            "skipped",
            "no",
            "pair",
            "recorded",
            "row",
            "pairs",
            "it",
            "partial",
            "value",
            "kind",
            "start",
            "in",
            "band",
            "of",
            "cases",
            "gradings",
            "failed",
            "prompt",
            "tokens",
            "completion",
            "in_band",
            "over",
            "vs",
            "paired",
            "clusters",
            "mean",
            "SE",
            "unclustered",
            "wins",
            "loses",
            "undecided",
            "candidate",
            "comparand",
            "only",
            "n",
            "a",
            "nothing",
            "digest",
            "differs",
        }
        self.assertLessEqual(set(re.findall(r"[A-Za-z][A-Za-z_]*", text)), labels)


if __name__ == "__main__":
    unittest.main()
