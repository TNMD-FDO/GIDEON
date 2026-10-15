"""Engine conditions require complete scrapes and whole observed counts."""

import ast
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from gideon.evaluation import conditions
from gideon.evaluation.conditions import (
    EngineConditions,
    describe,
    judge,
    read_engine_conditions,
)
from gideon.host import prometheus
from gideon.host.prometheus import PROMETHEUS_SCRAPE_STEP_SECONDS, QueryResult
from gideon.host.render.engine import ENGINE_JOB_NAME
from gideon.host.render.services.prometheus import PROMETHEUS_LOOPBACK_ADDRESS

ROOT = Path(__file__).resolve().parents[1]


class JudgeConditions(unittest.TestCase):
    def setUp(self) -> None:
        self.step = PROMETHEUS_SCRAPE_STEP_SECONDS
        self.start = 1001
        self.stamps = [self.start + index * self.step for index in range(4)]
        self.end = self.stamps[-1]
        self.up = dict.fromkeys(self.stamps, 1.0)
        self.running = dict(zip(self.stamps, (1.0, 7.0, 3.0, 2.0), strict=True))
        self.preemptions = dict(zip(self.stamps, (40.0, 42.0, 45.0, 45.0), strict=True))

    def read(self) -> EngineConditions:
        return judge(
            self.up,
            self.running,
            self.preemptions,
            start=self.start,
            end=self.end,
            step=self.step,
        )

    def assert_unknown(self, result: EngineConditions, reason: str) -> None:
        self.assertEqual(result, EngineConditions(None, None, reason))

    def test_maximum_and_differenced_rise(self) -> None:
        self.assertEqual(self.read(), EngineConditions(7, 5, ""))

    def test_counter_decrease_counts_the_value_since_restart(self) -> None:
        self.preemptions = dict(zip(self.stamps, (40.0, 42.0, 1.0, 4.0), strict=True))
        self.assertEqual(self.read(), EngineConditions(7, 6, ""))

    def test_missing_scrape_leaves_both_figures_unknown(self) -> None:
        for series in (self.up, self.running, self.preemptions):
            for stamp in self.stamps:
                with self.subTest(series=series, stamp=stamp):
                    value = series.pop(stamp)
                    self.assert_unknown(
                        self.read(), "engine not scraped at every step of the span"
                    )
                    series[stamp] = value

    def test_up_must_be_one_at_every_step(self) -> None:
        for value in (0.0, 2.0, -1.0, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.up[self.stamps[1]] = value
                self.assert_unknown(
                    self.read(), "engine job's up was not 1 at every step of the span"
                )

    def test_unreadable_count_in_either_series_leaves_both_unknown(self) -> None:
        for series in (self.running, self.preemptions):
            for value in (-1.0, 2.5, float("nan"), float("inf"), float("-inf")):
                with self.subTest(series=series, value=value):
                    previous = series[self.stamps[1]]
                    series[self.stamps[1]] = value
                    self.assert_unknown(
                        self.read(),
                        f"Prometheus returned an unreadable value at {PROMETHEUS_LOOPBACK_ADDRESS}",
                    )
                    series[self.stamps[1]] = previous

    def test_one_point_span_has_no_counter_rise(self) -> None:
        for end in (self.start, self.start + self.step - 1):
            with self.subTest(end=end):
                self.assertEqual(
                    judge(
                        {self.start: 1.0},
                        {self.start: 3.0},
                        {self.start: 40.0},
                        start=self.start,
                        end=end,
                        step=self.step,
                    ),
                    EngineConditions(3, 0, ""),
                )

    def test_zero_counts_are_known(self) -> None:
        self.running = dict.fromkeys(self.stamps, 0.0)
        self.preemptions = dict.fromkeys(self.stamps, 0.0)
        self.assertEqual(self.read(), EngineConditions(0, 0, ""))


class ReadConditions(unittest.TestCase):
    def test_expressions_select_the_engine_job_and_reduce_its_series(self) -> None:
        selector = f'{{job="{ENGINE_JOB_NAME}"}}'
        self.assertEqual(
            conditions.RUNNING_QUERY, f"max(vllm:num_requests_running{selector})"
        )
        self.assertEqual(
            conditions.PREEMPTIONS_QUERY, f"sum(vllm:num_preemptions_total{selector})"
        )
        self.assertEqual(conditions.UP_QUERY, f"min(up{selector})")

    def test_fractional_spans_share_normalized_query_and_judgement_bounds(self) -> None:
        base = datetime(2099, 1, 2, 3, 4, 5, tzinfo=UTC)
        started = base + timedelta(microseconds=800000)
        step = PROMETHEUS_SCRAPE_STEP_SECONDS
        expected_start = int(base.timestamp())
        expected_end = expected_start + step + 1
        calls: list[tuple[str, int, int, int]] = []

        def reader(
            expression: str, start: int, end: int, scrape_step: int
        ) -> QueryResult[dict[int, float]]:
            calls.append((expression, start, end, scrape_step))
            values: dict[int, float] = {}
            for index, stamp in enumerate(range(start, end + 1, scrape_step)):
                values[stamp] = {
                    conditions.UP_QUERY: 1.0,
                    conditions.RUNNING_QUERY: float(index + 1),
                    conditions.PREEMPTIONS_QUERY: 20.0 + 3 * index,
                }[expression]
            return QueryResult(values)

        for seconds in (step - 0.1, step + 0.1):
            with self.subTest(seconds=seconds):
                calls.clear()
                result = read_engine_conditions(
                    started, started + timedelta(seconds=seconds), range_reader=reader
                )
                self.assertEqual(result, EngineConditions(2, 3, ""))
                self.assertEqual(
                    calls,
                    [
                        (expression, expected_start, expected_end, step)
                        for expression in (
                            conditions.RUNNING_QUERY,
                            conditions.PREEMPTIONS_QUERY,
                            conditions.UP_QUERY,
                        )
                    ],
                )

    def test_default_reader_uses_the_shared_range_query(self) -> None:
        started = datetime(2099, 1, 2, tzinfo=UTC)
        start = int(started.timestamp())
        with mock.patch.object(
            prometheus, "range_query", return_value=QueryResult({start: 1.0})
        ) as reader:
            self.assertEqual(
                read_engine_conditions(started, started), EngineConditions(1, 0, "")
            )
        self.assertEqual(
            reader.call_args_list,
            [
                mock.call(expression, start, start, PROMETHEUS_SCRAPE_STEP_SECONDS)
                for expression in (
                    conditions.RUNNING_QUERY,
                    conditions.PREEMPTIONS_QUERY,
                    conditions.UP_QUERY,
                )
            ],
        )

    def test_first_failed_read_preserves_its_reason_and_stops(self) -> None:
        started = datetime(2099, 1, 2, tzinfo=UTC)
        start = int(started.timestamp())
        reasons = (
            f"Prometheus did not answer at {PROMETHEUS_LOOPBACK_ADDRESS}",
            f"Prometheus returned a non-success status at {PROMETHEUS_LOOPBACK_ADDRESS}",
            f"Prometheus returned unreadable JSON at {PROMETHEUS_LOOPBACK_ADDRESS}",
        )
        for failed_index, reason in enumerate(reasons):
            with self.subTest(failed_index=failed_index):
                results: list[QueryResult[dict[int, float]]] = [
                    QueryResult({start: 1.0}) for _ in range(failed_index)
                ]
                results.append(QueryResult(None, reason))
                reader = mock.Mock(side_effect=results)
                self.assertEqual(
                    read_engine_conditions(started, started, range_reader=reader),
                    EngineConditions(None, None, reason),
                )
                self.assertEqual(reader.call_count, failed_index + 1)

    def test_missing_engine_scrape_is_unknown_after_successful_reads(self) -> None:
        started = datetime(2099, 1, 2, tzinfo=UTC)
        reader = mock.Mock(return_value=QueryResult({}))
        result = read_engine_conditions(started, started, range_reader=reader)
        self.assertEqual(
            result,
            EngineConditions(
                None, None, "engine not scraped at every step of the span"
            ),
        )
        self.assertEqual(reader.call_count, 3)


class ConditionsDescription(unittest.TestCase):
    def test_known_and_unknown_phrasings(self) -> None:
        self.assertEqual(
            describe(EngineConditions(7, 3, "")),
            "engine in-flight max 7, preemptions 3 over the span",
        )
        self.assertEqual(
            describe(EngineConditions(None, None, "not asked")),
            "engine conditions unknown (not asked)",
        )


class ConditionsImports(unittest.TestCase):
    def test_imports_are_stdlib_or_host_and_job_is_not_a_literal(self) -> None:
        tree = ast.parse((ROOT / "gideon/evaluation/conditions.py").read_text())
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                imports.append(node.module or "")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotIn(f'job="{ENGINE_JOB_NAME}"', node.value)
                self.assertNotEqual(node.value, ENGINE_JOB_NAME)
        for name in imports:
            self.assertTrue(
                name.startswith("gideon.host")
                or name.split(".")[0] in sys.stdlib_module_names,
                name,
            )
