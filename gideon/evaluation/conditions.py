"""Read and reduce the engine's scrape samples over an evaluation run's span."""

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from gideon.host import prometheus
from gideon.host.prometheus import PROMETHEUS_SCRAPE_STEP_SECONDS, RangeReader
from gideon.host.render.engine import ENGINE_JOB_NAME

RUNNING_SERIES: Final = "vllm:num_requests_running"
PREEMPTIONS_SERIES: Final = "vllm:num_preemptions_total"
UP_SERIES: Final = "up"
_JOB_SELECTOR: Final = f'{{job="{ENGINE_JOB_NAME}"}}'
RUNNING_QUERY: Final = f"max({RUNNING_SERIES}{_JOB_SELECTOR})"
PREEMPTIONS_QUERY: Final = f"sum({PREEMPTIONS_SERIES}{_JOB_SELECTOR})"
UP_QUERY: Final = f"min({UP_SERIES}{_JOB_SELECTOR})"
_KNOWN_DETAIL: Final = "engine in-flight max {n}, preemptions {m} over the span"
_UNKNOWN_DETAIL: Final = "engine conditions unknown ({reason})"


@dataclass(frozen=True, slots=True)
class EngineConditions:
    """The two observed counts, or the reason both are unknown."""

    in_flight_max: int | None
    preemptions: int | None
    reason: str


ConditionsReader = Callable[[datetime, datetime], EngineConditions]


def judge(
    up: Mapping[int, float],
    running: Mapping[int, float],
    preemptions: Mapping[int, float],
    *,
    start: int,
    end: int,
    step: int,
) -> EngineConditions:
    """Reduce a complete scrape grid; a counter decrease starts a new count."""

    if step <= 0 or end < start:
        raise ValueError("Engine conditions need a positive step and an ordered span")
    stamps = range(start, end + 1, step)
    if any(
        stamp not in series for series in (up, running, preemptions) for stamp in stamps
    ):
        return EngineConditions(
            None, None, "engine not scraped at every step of the span"
        )
    if any(up[stamp] != 1 for stamp in stamps):
        return EngineConditions(
            None, None, "engine job's up was not 1 at every step of the span"
        )
    for series in (running, preemptions):
        for stamp in stamps:
            value = series[stamp]
            if not math.isfinite(value) or value < 0 or not value.is_integer():
                return EngineConditions(None, None, prometheus.UNREADABLE_VALUE)
    maximum = max(int(running[stamp]) for stamp in stamps)
    rise = 0
    previous: int | None = None
    for stamp in stamps:
        value = int(preemptions[stamp])
        if previous is not None:
            rise += value - previous if value >= previous else value
        previous = value
    return EngineConditions(maximum, rise, "")


def read_engine_conditions(
    started: datetime,
    finished: datetime,
    *,
    range_reader: RangeReader | None = None,
) -> EngineConditions:
    """Query and judge one normalized span so fractional seconds share a grid."""

    reader = prometheus.range_query if range_reader is None else range_reader
    start = math.floor(started.timestamp())
    end = math.ceil(finished.timestamp())
    samples: dict[str, dict[int, float]] = {}
    for expression in (RUNNING_QUERY, PREEMPTIONS_QUERY, UP_QUERY):
        result = reader(expression, start, end, PROMETHEUS_SCRAPE_STEP_SECONDS)
        if result.samples is None:
            return EngineConditions(None, None, result.reason)
        samples[expression] = result.samples
    return judge(
        samples[UP_QUERY],
        samples[RUNNING_QUERY],
        samples[PREEMPTIONS_QUERY],
        start=start,
        end=end,
        step=PROMETHEUS_SCRAPE_STEP_SECONDS,
    )


def describe(conditions: EngineConditions) -> str:
    """Describe both counts or the reason they could not be measured."""

    if conditions.in_flight_max is None or conditions.preemptions is None:
        return _UNKNOWN_DETAIL.format(reason=conditions.reason)
    return _KNOWN_DETAIL.format(n=conditions.in_flight_max, m=conditions.preemptions)
