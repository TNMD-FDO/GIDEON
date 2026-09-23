"""Capacity checks for the host preflight.

The data-volume floor is the profile's size minimum (ADR-0017). Free bytes
are reported, not judged, until §7.6's reserve rule lands with its consumer.
"""

from typing import Final

from gideon.host.checks import (
    CheckReport,
    PreflightCheck,
    PreflightContext,
    Severity,
    format_gb,
)
from gideon.host.models import GIGABYTE, select_profile
from gideon.host.report import Problem

_DATA_FIX = (
    "Provide a data volume of at least {floor} GB at /data (§1.4), or set "
    "hardware_profile in /etc/gideon/site.yaml to a profile this host satisfies, "
    "then re-run preflight."
)
DATA_DF_ARGV: Final[tuple[str, ...]] = ("df", "-B1", "--output=size,avail", "/data")


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
