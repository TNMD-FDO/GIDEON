"""Capacity checks for the host preflight.

The data-volume floor comes from the profile's size minimum. Free bytes are
reported, not judged, until the reserve rule lands with its consumer.

The host-memory floor is an agreement between the operators of the
applications sharing the box: a fifth of the host, a release-constant fraction
a test holds to the agreed figure. Its readings are /proc/meminfo now and the
node exporter's fourteen-day low when Prometheus answers; either below the
floor warns and nothing refuses, since the limits are re-weighed by people.
"""

import http.client
import json
import math
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from fractions import Fraction
from typing import Final
from urllib.parse import urlencode

from gideon.host.checks import (
    CheckReport,
    PreflightCheck,
    PreflightContext,
    Severity,
    format_gb,
    meminfo_kb,
)
from gideon.host.models import GIGABYTE, select_profile
from gideon.host.render.services.prometheus import PROMETHEUS_LOOPBACK_ADDRESS
from gideon.host.report import Problem

_DATA_FIX = (
    "Provide a data volume of at least {floor} GB at /data, or set "
    "hardware_profile in /etc/gideon/site.yaml to a profile this host satisfies, "
    "then re-run preflight."
)
DATA_DF_ARGV: Final[tuple[str, ...]] = ("df", "-B1", "--output=size,avail", "/data")
HOST_MEMORY_FLOOR_FRACTION: Final = Fraction(1, 5)
MEMORY_LOW_RANGE: Final = "14d"
MEMORY_AVAILABLE_SERIES: Final = "node_memory_MemAvailable_bytes"
MEMORY_LOW_QUERY: Final = f"min(min_over_time({MEMORY_AVAILABLE_SERIES}[{MEMORY_LOW_RANGE}]))"
PROMETHEUS_QUERY_PATH: Final = "/api/v1/query"
PROMETHEUS_TIMEOUT_SECONDS: Final = 5
_MEMINFO_FIX = "Restore /proc/meminfo on the host, then re-run preflight."
_HOST_MEMORY_FIX = (
    "Read each container's working set against its limit on the Overview's "
    "container-memory panel, and the co-tenant's own; re-weigh the applications' "
    "memory limits before installing or upgrading; then re-run preflight."
)
_KIBIBYTE = 1024


@dataclass(frozen=True, slots=True)
class LowReading:
    """A Prometheus low in bytes, or the reason it could not be read."""

    bytes: int | None
    reason: str = ""


LowReader = Callable[[], LowReading]


def read_prometheus_low() -> LowReading:
    """Read the fourteen-day minimum directly from Prometheus on loopback."""

    address = PROMETHEUS_LOOPBACK_ADDRESS
    connection: http.client.HTTPConnection | None = None
    try:
        connection = http.client.HTTPConnection(address, timeout=PROMETHEUS_TIMEOUT_SECONDS)
        connection.request("GET", f"{PROMETHEUS_QUERY_PATH}?{urlencode({'query': MEMORY_LOW_QUERY})}")
        response = connection.getresponse()
        if response.status != 200:
            return LowReading(None, f"Prometheus returned HTTP {response.status} at {address}")
        body = response.read()
    except TimeoutError:
        return LowReading(None, f"Prometheus timed out at {address}")
    except OSError:
        return LowReading(None, f"Prometheus did not answer at {address}")
    except http.client.HTTPException:
        return LowReading(None, f"Prometheus returned an unreadable reply at {address}")
    finally:
        if connection is not None:
            with suppress(OSError):
                connection.close()

    try:
        document: object = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return LowReading(None, f"Prometheus returned unreadable JSON at {address}")
    if not isinstance(document, dict) or not isinstance(document.get("status"), str):
        return LowReading(None, f"Prometheus returned an unreadable result at {address}")
    if document["status"] != "success":
        return LowReading(None, f"Prometheus returned a non-success status at {address}")
    data = document.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "vector":
        return LowReading(None, f"Prometheus returned an unreadable result at {address}")
    result = data.get("result")
    if not isinstance(result, list):
        return LowReading(None, f"Prometheus returned an unreadable result at {address}")
    if not result:
        return LowReading(None, f"no fourteen-day low on Prometheus at {address} yet")
    sample = result[0]
    if not isinstance(sample, dict):
        return LowReading(None, f"Prometheus returned an unreadable result at {address}")
    value = sample.get("value")
    if not isinstance(value, list) or len(value) != 2 or not isinstance(value[1], str):
        return LowReading(None, f"Prometheus returned an unreadable result at {address}")
    try:
        number = float(value[1])
    except (ValueError, OverflowError):
        return LowReading(None, f"Prometheus returned an unreadable value at {address}")
    if not math.isfinite(number) or number < 0:
        return LowReading(None, f"Prometheus returned an unreadable value at {address}")
    return LowReading(int(number))


def parse_size_and_available(output: str) -> tuple[int, int] | None:
    """Parse the size and available byte counts from ``df`` output."""

    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    fields = lines[-1].split()
    if len(fields) != 2:
        return None
    try:
        size, available = (int(field) for field in fields)
    except ValueError:
        return None
    if size < 0 or available < 0:
        return None
    return size, available


class DataVolumeCheck(PreflightCheck):
    """Measure ``/data``'s size against the selected profile's minimum.

    The free-byte figure is informational; the size floor is the only capacity
    judgment until the measured reserve rule arrives with its consumer.
    """

    name = "data-volume"
    summary = "check /data's size against the profile's data-volume floor"

    def run(self, context: PreflightContext) -> CheckReport:
        profile = select_profile(context.models, context.site.hardware_profile)
        if isinstance(profile, Problem):
            return CheckReport(Severity.REFUSE, profile.problem, profile.fix)

        result = context.host.run(DATA_DF_ARGV)
        measurements = parse_size_and_available(result.stdout) if result.returncode == 0 else None
        if measurements is None:
            return CheckReport(
                Severity.REFUSE,
                "could not measure /data's size and free space",
                "Ensure /data is mounted and readable, then re-run preflight.",
            )

        size, available = measurements
        size_detail = format_gb(size)
        free_detail = format_gb(available)
        floor = profile.requires.data_volume_gb
        if context.no_gpu:
            return CheckReport(
                Severity.PASS,
                f"skipped: no-GPU host; /data size {size_detail}, {free_detail} free; "
                "data-volume floor is not judged",
            )

        required = floor * GIGABYTE
        if size < required:
            return CheckReport(
                Severity.REFUSE,
                f"/data is {size} bytes ({size_detail}), profile requires a {floor} GB data volume",
                _DATA_FIX.format(floor=floor),
            )
        return CheckReport(
            Severity.PASS,
            f"/data size {size_detail}, {free_detail} free; profile requires a {floor} GB data volume",
        )


class HostMemoryCheck(PreflightCheck):
    """Warn when available host memory falls below its agreed floor."""

    name = "host-memory"
    summary = "check the host's available memory against a fifth of the host, now and at its fourteen-day low"

    def __init__(self, reader: LowReader = read_prometheus_low) -> None:
        self.reader = reader

    def run(self, context: PreflightContext) -> CheckReport:
        try:
            meminfo = context.host.read_text("/proc/meminfo")
        except OSError as exc:
            return CheckReport(
                Severity.WARN,
                f"could not read /proc/meminfo: {exc}",
                _MEMINFO_FIX,
            )

        total_kb = meminfo_kb(meminfo, "MemTotal")
        if total_kb is None:
            return CheckReport(
                Severity.WARN,
                "MemTotal is missing or unreadable in /proc/meminfo",
                _MEMINFO_FIX,
            )
        available_kb = meminfo_kb(meminfo, "MemAvailable")
        if available_kb is None:
            return CheckReport(
                Severity.WARN,
                "MemAvailable is missing or unreadable in /proc/meminfo",
                _MEMINFO_FIX,
            )

        total_bytes = total_kb * _KIBIBYTE
        available_bytes = available_kb * _KIBIBYTE
        floor_bytes = total_bytes * HOST_MEMORY_FLOOR_FRACTION
        low = self.reader()
        now_detail = format_gb(available_bytes)
        floor_detail = format_gb(int(floor_bytes))
        total_detail = format_gb(total_bytes)
        now_below = available_bytes < floor_bytes
        low_below = low.bytes is not None and low.bytes < floor_bytes
        if now_below or low_below:
            low_detail = (
                f" and {format_gb(low.bytes)} at its fourteen-day low on Prometheus"
                if low.bytes is not None
                else f" (fourteen-day low not read: {low.reason})"
            )
            below = (
                "both readings are"
                if now_below and low_below
                else "the current reading is" if now_below else "the fourteen-day low is"
            )
            return CheckReport(
                Severity.WARN,
                f"available memory is {now_detail} now{low_detail}; {below} below "
                f"the floor of {floor_detail} (a fifth of the host's {total_detail})",
                _HOST_MEMORY_FIX,
            )
        low_detail = (
            f", {format_gb(low.bytes)} at its fourteen-day low on Prometheus"
            if low.bytes is not None
            else f" (fourteen-day low not read: {low.reason})"
        )
        return CheckReport(
            Severity.PASS,
            f"available memory {now_detail} now{low_detail}; floor {floor_detail} "
            f"(a fifth of {total_detail})",
        )
