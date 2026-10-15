"""Read numeric samples from Prometheus on loopback with bounded requests."""

import http.client
import json
import math
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlencode

from gideon.host.render.services.prometheus import PROMETHEUS_LOOPBACK_ADDRESS

PROMETHEUS_QUERY_PATH: Final = "/api/v1/query"
PROMETHEUS_RANGE_QUERY_PATH: Final = "/api/v1/query_range"
PROMETHEUS_TIMEOUT_SECONDS: Final = 5
PROMETHEUS_SCRAPE_STEP_SECONDS: Final = 15
PROMETHEUS_RANGE_CHUNK_SECONDS: Final = 24 * 60 * 60
_UNREADABLE_RESULT = (
    f"Prometheus returned an unreadable result at {PROMETHEUS_LOOPBACK_ADDRESS}"
)
UNREADABLE_VALUE = (
    f"Prometheus returned an unreadable value at {PROMETHEUS_LOOPBACK_ADDRESS}"
)


@dataclass(frozen=True, slots=True)
class QueryResult[T]:
    """Samples or a content-free reason; an empty instant result has neither."""

    samples: T | None
    reason: str = ""


RangeReader = Callable[[str, int, int, int], QueryResult[dict[int, float]]]


def _request(
    path: str, parameters: dict[str, str | int], result_type: str
) -> QueryResult[list[object]]:
    address = PROMETHEUS_LOOPBACK_ADDRESS
    connection: http.client.HTTPConnection | None = None
    try:
        connection = http.client.HTTPConnection(
            address, timeout=PROMETHEUS_TIMEOUT_SECONDS
        )
        connection.request("GET", f"{path}?{urlencode(parameters)}")
        response = connection.getresponse()
        if response.status != 200:
            return QueryResult(
                None, f"Prometheus returned HTTP {response.status} at {address}"
            )
        body = response.read()
    except TimeoutError:
        return QueryResult(None, f"Prometheus timed out at {address}")
    except OSError:
        return QueryResult(None, f"Prometheus did not answer at {address}")
    except http.client.HTTPException:
        return QueryResult(
            None, f"Prometheus returned an unreadable reply at {address}"
        )
    finally:
        if connection is not None:
            with suppress(OSError):
                connection.close()

    try:
        document: object = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return QueryResult(None, f"Prometheus returned unreadable JSON at {address}")
    if not isinstance(document, dict) or not isinstance(document.get("status"), str):
        return QueryResult(None, _UNREADABLE_RESULT)
    if document["status"] != "success":
        return QueryResult(
            None, f"Prometheus returned a non-success status at {address}"
        )
    data = document.get("data")
    if not isinstance(data, dict) or data.get("resultType") != result_type:
        return QueryResult(None, _UNREADABLE_RESULT)
    result = data.get("result")
    if not isinstance(result, list):
        return QueryResult(None, _UNREADABLE_RESULT)
    return QueryResult(result)


def _number(sample: object, *, non_negative: bool = False) -> QueryResult[float]:
    if (
        not isinstance(sample, list)
        or len(sample) != 2
        or not isinstance(sample[1], str)
    ):
        return QueryResult(None, _UNREADABLE_RESULT)
    try:
        number = float(sample[1])
    except (ValueError, OverflowError):
        return QueryResult(None, UNREADABLE_VALUE)
    if not math.isfinite(number) or (non_negative and number < 0):
        return QueryResult(None, UNREADABLE_VALUE)
    return QueryResult(number)


def instant_query(expression: str) -> QueryResult[float]:
    """Read the first vector value, distinguishing an empty vector from failure."""

    result = _request(PROMETHEUS_QUERY_PATH, {"query": expression}, "vector")
    if not result.samples:
        return QueryResult(None, result.reason)
    sample = result.samples[0]
    if not isinstance(sample, dict):
        return QueryResult(None, _UNREADABLE_RESULT)
    return _number(sample.get("value"), non_negative=True)


def _range_samples(series: list[object]) -> QueryResult[dict[int, float]]:
    samples: dict[int, float] = {}
    for item in series:
        if not isinstance(item, dict) or not isinstance(item.get("values"), list):
            return QueryResult(None, _UNREADABLE_RESULT)
        for sample in item["values"]:
            number = _number(sample)
            if number.samples is None:
                return QueryResult(None, number.reason)
            stamp = sample[0]
            if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
                return QueryResult(None, _UNREADABLE_RESULT)
            if isinstance(stamp, float) and (
                not math.isfinite(stamp) or not stamp.is_integer()
            ):
                return QueryResult(None, _UNREADABLE_RESULT)
            key = int(stamp)
            total = samples.get(key, 0.0) + number.samples
            if not math.isfinite(total):
                return QueryResult(None, UNREADABLE_VALUE)
            samples[key] = total
    return QueryResult(samples)


def range_query(
    expression: str, start: int, end: int, step: int
) -> QueryResult[dict[int, float]]:
    """Sum matrix series by timestamp, limiting each request to at most a day."""

    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in (start, end, step)
    ):
        raise ValueError("Prometheus range boundaries and step must be whole seconds")
    if step <= 0 or end < start:
        raise ValueError(
            "Prometheus range needs a positive step and end at or after start"
        )
    samples: dict[int, float] = {}
    chunk = start
    chunk_span = (PROMETHEUS_RANGE_CHUNK_SECONDS // step) * step
    while chunk <= end:
        chunk_end = min(chunk + chunk_span, end)
        result = _request(
            PROMETHEUS_RANGE_QUERY_PATH,
            {"query": expression, "start": chunk, "end": chunk_end, "step": step},
            "matrix",
        )
        if result.samples is None:
            return QueryResult(None, result.reason)
        values = _range_samples(result.samples)
        if values.samples is None:
            return values
        samples.update(values.samples)
        chunk = chunk_end + step
    return QueryResult(dict(sorted(samples.items())))
