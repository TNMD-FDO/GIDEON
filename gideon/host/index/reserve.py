"""Measure data-volume free space and judge the generation reserve lines."""

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from typing import Final

from gideon.host.report import Problem
from gideon.host.sysio import Host

# exempt: starting reserve ratios, corrected from measured generation footprints.
BUILD_RESERVE_RATIO: Final = Fraction(5, 4)
ALERT_RESERVE_MULTIPLIER: Final = 2
RUNBOOK_ROW: Final = "the Data volume low row of docs/runbooks/observability.md §4"
DATA_DF_ARGV: Final[tuple[str, ...]] = ("df", "-B1", "--output=size,avail", "/data")


@dataclass(frozen=True, slots=True)
class ReserveVerdict:
    """Free and promoted bytes, their reserve lines, and each clearance."""

    free_bytes: int
    promoted_bytes: int
    build_line: int
    alert_line: int
    build_cleared: bool
    alert_cleared: bool


@dataclass(frozen=True, slots=True)
class PromotedReading:
    """Promoted bytes, or the reason they could not be read."""

    bytes: int | None
    reason: str = ""


PromotedReader = Callable[[Host], PromotedReading]


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


def read_free_bytes(host: Host) -> tuple[int, int] | Problem:
    """Read the size and available bytes of the mounted data volume."""

    fix = "Ensure /data is mounted and readable."
    try:
        result = host.run(DATA_DF_ARGV)
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"data-volume measurement could not run ({type(exc).__name__})", fix)
    if result.returncode != 0:
        return Problem(f"data-volume measurement failed (exit {result.returncode})", fix)
    measurements = parse_size_and_available(result.stdout)
    if measurements is None:
        return Problem("could not measure /data's size and free space", fix)
    return measurements


def judge(free: int, promoted: int) -> ReserveVerdict:
    """Judge both reserve lines using whole-byte integer arithmetic."""

    numerator = promoted * BUILD_RESERVE_RATIO.numerator
    denominator = BUILD_RESERVE_RATIO.denominator
    build_line = (numerator + denominator - 1) // denominator
    alert_line = promoted * ALERT_RESERVE_MULTIPLIER
    return ReserveVerdict(
        free, promoted, build_line, alert_line, free >= build_line, free >= alert_line,
    )


def describe(verdict: ReserveVerdict) -> str:
    """Describe the shared figures with raw bytes and decimal gigabytes."""

    # The checks registry imports the reserve reader through its capacity check.
    from gideon.host.checks import format_gb

    def figure(value: int) -> str:
        return f"{value} bytes ({format_gb(value)})"

    return (
        f"/data free {figure(verdict.free_bytes)}; promoted {figure(verdict.promoted_bytes)}; "
        f"build line {figure(verdict.build_line)}, "
        f"{'cleared' if verdict.build_cleared else 'not cleared'}; "
        f"alert line {figure(verdict.alert_line)}, "
        f"{'cleared' if verdict.alert_cleared else 'not cleared'}"
    )
