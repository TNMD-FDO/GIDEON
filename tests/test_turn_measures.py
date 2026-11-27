"""Turn observation rules and their Prometheus exposition bytes."""

import ast
import re
import sys
import unittest
from importlib.util import resolve_name
from itertools import product
from pathlib import Path
from typing import get_args

from gideon.guardrail import writer
from gideon.turn import events, measures, turnlog
from gideon.turn.rerank import Gate
from gideon.turn.states import State
from gideon.worker.metrics import CONTENT_TYPE
from tools.exportboundary import absent_from_export

ROOT = Path(__file__).resolve().parent.parent
SPEC = Path(".scratch/greenfield-spec/spec.md")


def _expected_histogram(
    metric: str, help_text: str, edges: tuple[float, ...], readings: dict[tuple[str, str], list[float]],
) -> list[str]:
    lines = [f"# HELP {metric} {help_text}", f"# TYPE {metric} histogram"]
    for source in writer.SOURCE_VOCABULARY:
        values = readings.get((metric, source), [])
        for edge in sorted(set(edges)):
            count = sum(value <= edge for value in values)
            lines.append(f'{metric}_bucket{{source="{source}",le="{float(edge)!r}"}} {count}')
        lines.extend((
            f'{metric}_bucket{{source="{source}",le="+Inf"}} {len(values)}',
            f'{metric}_sum{{source="{source}"}} {sum(values, 0.0)!r}',
            f'{metric}_count{{source="{source}"}} {len(values)}',
        ))
    return lines


def _expected_scrape(
    gate: Gate, readings: dict[tuple[str, str], list[float]], turns: dict[tuple[str, str, str], int],
) -> bytes:
    lines: list[str] = []
    for metric, help_text, tag, edges, kind, value in (
        (measures.RETRIEVAL_METRIC, measures.RETRIEVAL_HELP, "M1", measures.RETRIEVAL_BUCKETS, "histogram", None),
        (measures.THROUGH_RERANK_METRIC, measures.THROUGH_RERANK_HELP, "M2", measures.THROUGH_RERANK_BUCKETS, "histogram", None),
        (measures.RERANK_IN_FLIGHT_METRIC, measures.RERANK_IN_FLIGHT_HELP, "M3", (), "gauge", gate.in_flight),
        (measures.RERANK_WAITING_METRIC, measures.RERANK_WAITING_HELP, "M3", (), "gauge", gate.waiters),
        (measures.RERANK_QUEUE_TIMEOUTS_METRIC, measures.RERANK_QUEUE_TIMEOUTS_HELP, "M3", (), "counter", gate.timeouts),
        (measures.FIRST_PARAGRAPH_METRIC, measures.FIRST_PARAGRAPH_HELP, "M4", measures.ANSWER_BUCKETS, "histogram", None),
        (measures.COMPLETE_METRIC, measures.COMPLETE_HELP, "M5", measures.ANSWER_BUCKETS, "histogram", None),
    ):
        if not help_text.startswith(f"{tag} "):
            raise AssertionError(f"{metric} help must start with {tag}")
        if kind == "histogram":
            lines.extend(_expected_histogram(metric, help_text, edges, readings))
        else:
            lines.extend((f"# HELP {metric} {help_text}", f"# TYPE {metric} {kind}", f"{metric} {value}"))

    metric = measures.TURNS_METRIC
    lines.extend((f"# HELP {metric} {measures.TURNS_HELP}", f"# TYPE {metric} counter"))
    for source, outcome in product(writer.SOURCE_VOCABULARY, get_args(turnlog.Outcome)):
        for state in tuple(State) if outcome == "failed" else ("",):
            count = turns.get((source, outcome, state), 0)
            lines.append(f'{metric}{{source="{source}",outcome="{outcome}",state="{state}"}} {count}')
    return "\n".join([*lines, ""]).encode()


class TurnMeasuresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = Gate(2)
        self.collector = measures.TurnMeasures()

    def test_fresh_exposition_contains_the_whole_series_set_at_zero(self) -> None:
        self.assertEqual(measures.CONTENT_TYPE, CONTENT_TYPE)
        body = measures.format_metrics(self.collector, self.gate).encode()
        self.assertEqual(body, _expected_scrape(self.gate, {}, {}))

    def test_exposition_is_cumulative_and_reads_the_live_gate(self) -> None:
        eval_source = writer.EVAL_SOURCE
        user_source = writer.USER_SOURCE
        # Fictitious observations include zero, an exact edge, and overflow into +Inf.
        for milliseconds in (0, 250, 12_125):
            measures.observe_turn(self.collector, eval_source, "reply", None, {"retrieval": milliseconds}, None)
        measures.observe_turn(self.collector, user_source, "complete", None, {"first_text": 1234.567, "total": 3500}, 725)
        measures.observe_turn(self.collector, eval_source, "failed", State.RERANK_QUEUE_TIMEOUT, {"retrieval": 500}, None)
        self.gate.in_flight = 2
        self.gate.waiters = 3
        self.gate.timeouts = 1

        readings = {
            (measures.RETRIEVAL_METRIC, eval_source): [0.0, 0.25, 12.125, 0.5],
            (measures.THROUGH_RERANK_METRIC, user_source): [0.725],
            (measures.FIRST_PARAGRAPH_METRIC, user_source): [1.234567],
            (measures.COMPLETE_METRIC, user_source): [3.5],
        }
        turns = {
            (eval_source, "reply", ""): 3,
            (eval_source, "failed", State.RERANK_QUEUE_TIMEOUT.value): 1,
            (user_source, "complete", ""): 1,
        }
        body = measures.format_metrics(self.collector, self.gate).encode()
        self.assertEqual(body, _expected_scrape(self.gate, readings, turns))

    def test_every_outcome_with_and_without_each_figure(self) -> None:
        source = writer.EVAL_SOURCE
        terminal_outcomes = get_args(events.EndWord)
        for outcome, present in product(get_args(turnlog.Outcome), product((False, True), repeat=4)):
            with self.subTest(outcome=outcome, figures_present=present):
                retrieval, rerank, first_text, total = present
                collector = measures.TurnMeasures()
                timings = {
                    key: 1500
                    for key, reached in (("retrieval", retrieval), ("first_text", first_text), ("total", total))
                    if reached
                }
                measures.observe_turn(collector, source, outcome, State.INTERNAL_ERROR, timings, 1500 if rerank else None)
                readings = {
                    (metric, source): [1.5]
                    for metric, observed in (
                        (measures.RETRIEVAL_METRIC, retrieval),
                        (measures.THROUGH_RERANK_METRIC, rerank),
                        (measures.FIRST_PARAGRAPH_METRIC, first_text),
                        (measures.COMPLETE_METRIC, total and outcome in terminal_outcomes),
                    )
                    if observed
                }
                state = State.INTERNAL_ERROR.value if outcome == "failed" else ""
                self.assertEqual(
                    measures.format_metrics(collector, self.gate).encode(),
                    _expected_scrape(self.gate, readings, {(source, outcome, state): 1}),
                )

    def test_bad_timings_are_skipped_without_losing_the_turn_count(self) -> None:
        source = writer.EVAL_SOURCE
        for value in (None, "fictitious-timing", "1750", True, -3, -5e-324, float("nan"), float("inf"), 10**400):
            with self.subTest(value=value):
                collector = measures.TurnMeasures()
                timings = dict.fromkeys(("retrieval", "first_text", "total"), value)
                measures.observe_turn(collector, source, "complete", None, timings, value)
                self.assertEqual(
                    measures.format_metrics(collector, self.gate).encode(),
                    _expected_scrape(self.gate, {}, {(source, "complete", ""): 1}),
                )

    def test_values_outside_the_vocabularies_add_no_series(self) -> None:
        sentinel = 'fictitious-private-label\n"'
        source = writer.EVAL_SOURCE
        measures.observe_turn(self.collector, sentinel, "complete", None, {"retrieval": 1700}, 2100)
        measures.observe_turn(self.collector, source, sentinel, None, {}, None)
        measures.observe_turn(self.collector, source, "failed", sentinel, {}, None)
        measures.observe_turn(self.collector, source, "failed", None, {}, None)
        measures.observe_turn(self.collector, source, "reply", sentinel, {sentinel: 2300}, sentinel)
        body = measures.format_metrics(self.collector, self.gate)
        self.assertNotIn(sentinel, body)
        self.assertNotIn(sentinel, repr(self.collector))
        self.assertEqual(body.encode(), _expected_scrape(self.gate, {}, {(source, "reply", ""): 1}))

    def test_vocabulary_copies_match_their_owners(self) -> None:
        self.assertEqual(measures.SOURCE_VOCABULARY, writer.SOURCE_VOCABULARY)
        self.assertEqual(measures.OUTCOME_VOCABULARY, get_args(turnlog.Outcome))

    @unittest.skipIf(absent_from_export(SPEC, ROOT), "The build spec is omitted from the public export.")
    def test_buckets_include_the_appendix_b_bounds(self) -> None:
        _, _, appendix = (ROOT / SPEC).read_text(encoding="utf-8").partition("\n## Appendix B ")
        register = appendix.split("\n## ", 1)[0]
        bounds = dict(re.findall(r"^\| (M[12]) \| [^|\n]*≤ ([\d.]+) s\b", register, re.MULTILINE))
        self.assertIn(float(bounds["M1"]), measures.RETRIEVAL_BUCKETS)
        self.assertIn(float(bounds["M2"]), measures.THROUGH_RERANK_BUCKETS)

    def test_imports_are_standard_library_and_turn_states(self) -> None:
        tree = ast.parse((ROOT / "gideon/turn/measures.py").read_text(encoding="utf-8"))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                name = "." * node.level + (node.module or "")
                imports.add(resolve_name(name, "gideon.turn"))
        internal = {name for name in imports if name.partition(".")[0] not in sys.stdlib_module_names}
        self.assertEqual(internal, {"gideon.turn.states"})
