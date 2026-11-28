"""Collect research turn measures and write their Prometheus exposition.

Observations and scrapes run synchronously on the single uvicorn process's
event loop, so the collector needs no lock. Each process has its own counts.
"""

from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import accumulate
from math import isfinite
from typing import Final, Protocol

from .states import State

CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"

# Copies of guardrail.writer.SOURCE_VOCABULARY and turnlog.Outcome.
SOURCE_VOCABULARY: Final = ("user", "eval")
OUTCOME_VOCABULARY: Final = ("reply", "complete", "truncated", "tripped", "abstained", "failed", "disconnected")

RETRIEVAL_METRIC: Final = "gideon_turn_retrieval_seconds"
THROUGH_RERANK_METRIC: Final = "gideon_turn_through_rerank_seconds"
RERANK_IN_FLIGHT_METRIC: Final = "gideon_turn_rerank_in_flight"
RERANK_WAITING_METRIC: Final = "gideon_turn_rerank_waiting"
RERANK_QUEUE_TIMEOUTS_METRIC: Final = "gideon_turn_rerank_queue_timeouts_total"
FIRST_PARAGRAPH_METRIC: Final = "gideon_turn_first_paragraph_seconds"
COMPLETE_METRIC: Final = "gideon_turn_complete_seconds"
TURNS_METRIC: Final = "gideon_turn_turns_total"

RETRIEVAL_HELP: Final = "M1 Retrieval latency for stores and fusion in seconds."
THROUGH_RERANK_HELP: Final = "M2 Retrieval service latency through rerank in seconds."
RERANK_IN_FLIGHT_HELP: Final = "M3 Rerank seats in use."
RERANK_WAITING_HELP: Final = "M3 Turns waiting for a rerank seat."
RERANK_QUEUE_TIMEOUTS_HELP: Final = "M3 Turns that timed out waiting for a rerank seat."
FIRST_PARAGRAPH_HELP: Final = "M4 Time to the first paragraph in seconds."
COMPLETE_HELP: Final = "M5 Time to the complete answer in seconds."
TURNS_HELP: Final = "Turns observed by source, outcome, and failure state."

# M1 and M2 include their bounds. Other edges are exempt: histogram resolution.
RETRIEVAL_BUCKETS: Final = (0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.5, 5.0, 10.0)
THROUGH_RERANK_BUCKETS: Final = (0.25, 0.5, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 20.0, 30.0)
# M4 and M5. Exempt: histogram resolution.
ANSWER_BUCKETS: Final = (1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0, 180.0, 300.0, 600.0)

_HISTOGRAMS: Final[dict[str, tuple[str, tuple[float, ...]]]] = {
    RETRIEVAL_METRIC: (RETRIEVAL_HELP, RETRIEVAL_BUCKETS),
    THROUGH_RERANK_METRIC: (THROUGH_RERANK_HELP, THROUGH_RERANK_BUCKETS),
    FIRST_PARAGRAPH_METRIC: (FIRST_PARAGRAPH_HELP, ANSWER_BUCKETS),
    COMPLETE_METRIC: (COMPLETE_HELP, ANSWER_BUCKETS),
}


class _GateMetrics(Protocol):
    in_flight: int
    waiters: int
    timeouts: int


@dataclass(slots=True)
class _Histogram:
    buckets: list[int]
    total: float = 0.0


class TurnMeasures:
    """Keep the service's counts and sums for the fixed set of turn series."""

    def __init__(self) -> None:
        self._histograms = {
            (metric, source): _Histogram([0] * (len(edges) + 1))
            for metric, (_, edges) in _HISTOGRAMS.items()
            for source in SOURCE_VOCABULARY
        }
        failed_states = tuple(state.value for state in State)
        self._turns = {
            (source, outcome, state): 0
            for source in SOURCE_VOCABULARY
            for outcome in OUTCOME_VOCABULARY
            for state in (failed_states if outcome == "failed" else ("",))
        }


def _seconds(milliseconds: object) -> float | None:
    if isinstance(milliseconds, bool) or not isinstance(milliseconds, (int, float)) or milliseconds < 0:
        return None
    try:
        seconds = milliseconds / 1000
    except OverflowError:
        return None
    return seconds if isfinite(seconds) else None


def observe_turn(
    measures: TurnMeasures,
    source: object,
    outcome: object,
    failure_state: object,
    timings_ms: Mapping[str, object],
    through_rerank_ms: object,
) -> None:
    """Count an admitted turn and record the figures for the stages it reached."""

    if not isinstance(source, str) or source not in SOURCE_VOCABULARY:
        return

    state = failure_state if outcome == "failed" else ""
    if isinstance(outcome, str) and isinstance(state, str):
        turn = (source, outcome, state)
        if turn in measures._turns:
            measures._turns[turn] += 1

    for metric, milliseconds in (
        (RETRIEVAL_METRIC, timings_ms.get("retrieval")),
        (THROUGH_RERANK_METRIC, through_rerank_ms),
        (FIRST_PARAGRAPH_METRIC, timings_ms.get("first_text")),
        (COMPLETE_METRIC, timings_ms.get("total") if outcome in ("complete", "truncated", "tripped") else None),
    ):
        seconds = _seconds(milliseconds)
        if seconds is not None:
            histogram = measures._histograms[metric, source]
            _, edges = _HISTOGRAMS[metric]
            histogram.buckets[bisect_left(edges, seconds)] += 1
            histogram.total += seconds


def _format_histogram(lines: list[str], measures: TurnMeasures, metric: str) -> None:
    help_text, edges = _HISTOGRAMS[metric]
    lines.extend((f"# HELP {metric} {help_text}", f"# TYPE {metric} histogram"))
    for source in SOURCE_VOCABULARY:
        histogram = measures._histograms[metric, source]
        labels = f'source="{source}"'
        for edge, count in zip((*edges, "+Inf"), accumulate(histogram.buckets), strict=True):
            lines.append(f'{metric}_bucket{{{labels},le="{edge}"}} {count}')
        lines.extend((
            f"{metric}_sum{{{labels}}} {histogram.total!r}",
            f"{metric}_count{{{labels}}} {sum(histogram.buckets)}",
        ))


def format_metrics(measures: TurnMeasures, gate: _GateMetrics) -> str:
    """Format the turn collector and the live gate as Prometheus text."""

    lines: list[str] = []
    _format_histogram(lines, measures, RETRIEVAL_METRIC)
    _format_histogram(lines, measures, THROUGH_RERANK_METRIC)
    for metric, help_text, metric_type, value in (
        (RERANK_IN_FLIGHT_METRIC, RERANK_IN_FLIGHT_HELP, "gauge", gate.in_flight),
        (RERANK_WAITING_METRIC, RERANK_WAITING_HELP, "gauge", gate.waiters),
        (RERANK_QUEUE_TIMEOUTS_METRIC, RERANK_QUEUE_TIMEOUTS_HELP, "counter", gate.timeouts),
    ):
        lines.extend((f"# HELP {metric} {help_text}", f"# TYPE {metric} {metric_type}", f"{metric} {value}"))
    _format_histogram(lines, measures, FIRST_PARAGRAPH_METRIC)
    _format_histogram(lines, measures, COMPLETE_METRIC)
    lines.extend((f"# HELP {TURNS_METRIC} {TURNS_HELP}", f"# TYPE {TURNS_METRIC} counter"))
    for (source, outcome, state), count in measures._turns.items():
        lines.append(f'{TURNS_METRIC}{{source="{source}",outcome="{outcome}",state="{state}"}} {count}')
    return "\n".join(lines) + "\n"
