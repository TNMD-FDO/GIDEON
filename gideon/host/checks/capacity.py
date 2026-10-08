"""Capacity checks for the host preflight.

The data-volume floor comes from the profile's size minimum. Free bytes are
reported, not judged, until the reserve rule lands with its consumer.

The host-memory floor is an agreement between the operators of the
applications sharing the box: a fifth of the host, a release-constant fraction
a test holds to the agreed figure. Its readings are /proc/meminfo now and the
node exporter's fourteen-day low when Prometheus answers; either below the
floor warns and nothing refuses, since the limits are re-weighed by people.

The GPU memory check uses the engine's card budget. GIDEON's running holdings
count as available to its replacement servers; other holdings are measured
from processes and containers. The co-tenant reserve is held by a lock test,
not by this live check.
"""

import http.client
import json
import math
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from fractions import Fraction
from typing import Final
from urllib.parse import urlencode

from gideon.host import cotenants, gpus, nogpu, report
from gideon.host.checks import (
    MEBIBYTE,
    CheckReport,
    PreflightCheck,
    PreflightContext,
    Severity,
    format_gb,
    meminfo_kb,
    nvidia_failure,
)
from gideon.host.models import GIGABYTE, HardwareProfile, select_profile
from gideon.host.render.services.prometheus import PROMETHEUS_LOOPBACK_ADDRESS
from gideon.host.report import Problem
from gideon.host.sysio import Host

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
GPU_MEMORY_CARD_ARGV: Final = (
    "nvidia-smi", "--query-gpu=uuid,memory.total,memory.free", "--format=csv,noheader,nounits"
)
GPU_MEMORY_PROCESS_ARGV: Final = (
    "nvidia-smi", "--query-compute-apps=gpu_uuid,pid,used_memory", "--format=csv,noheader,nounits"
)
GPU_MEMORY_CONTAINERS_ARGV: Final = (
    "docker", "ps", "--no-trunc", "--format",
    '{{.ID}}\t{{.Names}}\t{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.service"}}',
)
GPU_MEMORY_FLAG: Final = "gpu-memory-utilization"
_CGROUP_CONTAINER = re.compile(r"/docker-([0-9a-f]{64})\.scope(?:/|$)")
_GPU_MEMORY_LOCK_FIX = (
    "Correct the model's serve.flags.gpu-memory-utilization in models.lock "
    "using docs/runbooks/release-files.md §4, then re-run preflight."
)
_GPU_MEMORY_FIX = (
    "Have the other application release memory on the short card, or re-agree "
    "that card's shared line in the box ledger with its operator; a server's "
    "budget is a release constant in models.lock, never changed on the server; "
    "then re-run {command}."
)


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


@dataclass(frozen=True, slots=True)
class GpuCard:
    """A card's driver position, UUID, and memory in MiB."""

    position: int
    uuid: str
    total_mib: int
    free_mib: int


@dataclass(frozen=True, slots=True)
class GpuProcess:
    """A compute process's card, PID, and memory in MiB."""

    uuid: str
    pid: int
    used_mib: int


@dataclass(frozen=True, slots=True)
class GpuContainer:
    """A running container's name and Compose labels."""

    name: str
    project: str | None
    service: str | None


@dataclass(frozen=True, slots=True)
class GpuServer:
    """A model server's card position and memory fraction."""

    role: str
    position: int
    fraction: Fraction


@dataclass(frozen=True, slots=True)
class GpuBudget:
    """A server's rounded budget on a measured card."""

    role: str
    fraction: Fraction
    mib: int


@dataclass(frozen=True, slots=True)
class GpuMemoryResult:
    """One card's measured holdings and server budgets."""

    position: int
    uuid: str
    total_mib: int
    free_mib: int
    gideon_by_service: tuple[tuple[str, int], ...]
    others_count: int
    others_mib: int
    budgets: tuple[GpuBudget, ...]

    @property
    def fits(self) -> bool:
        """Whether the servers fit beside GIDEON's current holdings."""

        need = sum(budget.mib for budget in self.budgets)
        return need <= self.free_mib + sum(mib for _, mib in self.gideon_by_service)


def gpu_servers(profile: HardwareProfile) -> tuple[GpuServer, ...] | CheckReport:
    """Read each model server's release-owned GPU memory fraction."""

    servers: list[GpuServer] = []
    for model in profile.models:
        value = model.serve.flags.get(GPU_MEMORY_FLAG)
        path = f"models.lock profiles.{profile.name}.models.{model.role}.serve.flags.{GPU_MEMORY_FLAG}"
        try:
            if value is None or isinstance(value, bool):
                raise ValueError
            fraction = Fraction(str(value))
        except (ValueError, ZeroDivisionError):
            return CheckReport(Severity.REFUSE, f"{path} is missing or invalid", _GPU_MEMORY_LOCK_FIX)
        if not 0 < fraction <= 1:
            return CheckReport(Severity.REFUSE, f"{path} must be greater than 0 and at most 1", _GPU_MEMORY_LOCK_FIX)
        servers.append(GpuServer(model.role, model.gpu, fraction))
    return tuple(servers)


def parse_gpu_cards(output: str) -> tuple[GpuCard, ...] | None:
    """Read card memory rows in the driver's position order."""

    cards: list[GpuCard] = []
    seen: set[str] = set()
    for line in output.splitlines():
        if not line.strip():
            continue
        columns = [column.strip() for column in line.split(",")]
        if len(columns) != 3 or not columns[0] or columns[0] in seen:
            return None
        try:
            total, free = int(columns[1]), int(columns[2])
        except ValueError:
            return None
        if total <= 0 or not 0 <= free <= total:
            return None
        cards.append(GpuCard(len(cards), columns[0], total, free))
        seen.add(columns[0])
    return tuple(cards) if cards else None


def parse_gpu_processes(output: str) -> tuple[GpuProcess, ...] | None:
    """Read the driver's per-process memory rows."""

    processes: list[GpuProcess] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        columns = [column.strip() for column in line.split(",")]
        if len(columns) != 3 or not columns[0]:
            return None
        try:
            pid, used = int(columns[1]), int(columns[2])
        except ValueError:
            return None
        if pid <= 0 or used < 0:
            return None
        processes.append(GpuProcess(columns[0], pid, used))
    return tuple(processes)


def parse_gpu_containers(output: str) -> dict[str, GpuContainer] | None:
    """Read container identities and the two Compose ownership labels."""

    containers: dict[str, GpuContainer] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        columns = line.split("\t")
        if len(columns) != 4 or not re.fullmatch(r"[0-9a-f]{64}", columns[0]) or not columns[1]:
            return None
        container_id, name, project, service = columns
        if container_id in containers:
            return None
        containers[container_id] = GpuContainer(name, project or None, service or None)
    return containers


def attribute_gpu_processes(
    host: Host, processes: tuple[GpuProcess, ...], containers: dict[str, GpuContainer]
) -> tuple[tuple[GpuProcess, str | None], ...]:
    """Attribute a process only when its cgroup names an owned container."""

    attributed: list[tuple[GpuProcess, str | None]] = []
    for process in processes:
        service = None
        try:
            cgroup = host.read_text(f"/proc/{process.pid}/cgroup")
        except (OSError, UnicodeError):
            cgroup = ""
        for line in cgroup.splitlines():
            match = _CGROUP_CONTAINER.search(line)
            if match is None:
                continue
            container = containers.get(match.group(1))
            if container is not None and cotenants.is_marked(
                cotenants.ContainerRow(container.name, container.project)
            ):
                service = container.service or container.name
            break
        attributed.append((process, service))
    return tuple(attributed)


def judge_gpu_memory(
    cards: tuple[GpuCard, ...],
    resolved: tuple[str, ...],
    servers: tuple[GpuServer, ...],
    attributed: tuple[tuple[GpuProcess, str | None], ...],
) -> tuple[GpuMemoryResult, ...]:
    """Compare each card's rounded server budgets with its available memory."""

    by_uuid = {card.uuid: card for card in cards}
    results: list[GpuMemoryResult] = []
    for position, uuid in enumerate(resolved):
        placed = [server for server in servers if server.position == position]
        if not placed:
            continue
        card = by_uuid[uuid]
        budgets = tuple(
            GpuBudget(server.role, server.fraction, math.ceil(card.total_mib * server.fraction))
            for server in placed
        )
        own: dict[str, int] = {}
        others_count = others_mib = 0
        for process, service in attributed:
            if process.uuid != uuid:
                continue
            if service is None:
                others_count += 1
                others_mib += process.used_mib
            else:
                own[service] = own.get(service, 0) + process.used_mib
        results.append(
            GpuMemoryResult(
                position, uuid, card.total_mib, card.free_mib,
                tuple(sorted(own.items())), others_count, others_mib, budgets,
            )
        )
    return tuple(results)


def read_gpu_memory(
    host: Host, cards: tuple[GpuCard, ...], resolved: tuple[str, ...], servers: tuple[GpuServer, ...]
) -> tuple[tuple[GpuMemoryResult, ...], str] | CheckReport:
    """Read live holdings and judge the cards carrying model servers."""

    process_result = host.run(GPU_MEMORY_PROCESS_ARGV)
    if process_result.returncode != 0:
        return nvidia_failure(process_result)
    processes = parse_gpu_processes(process_result.stdout)
    if processes is None:
        return CheckReport(
            Severity.REFUSE, "nvidia-smi returned unreadable per-process memory", nogpu.GPU_DRIVER_FIX
        )
    listing = host.run(GPU_MEMORY_CONTAINERS_ARGV)
    containers = parse_gpu_containers(listing.stdout) if listing.returncode == 0 else None
    if containers is None:
        if listing.returncode == 0:
            listing_detail = "docker ps returned an unreadable listing; judged on free memory alone"
        else:
            diagnostic = listing.stderr.strip() or f"exit code {listing.returncode}"
            listing_detail = f"docker ps failed ({diagnostic}); judged on free memory alone"
        attributed: tuple[tuple[GpuProcess, str | None], ...] = tuple(
            (process, None) for process in processes
        )
    else:
        attributed = attribute_gpu_processes(host, processes, containers)
        listing_detail = ""
    return judge_gpu_memory(cards, resolved, servers, attributed), listing_detail


def gpu_memory_detail(card: GpuMemoryResult) -> str:
    """Render one judged card's memory, holders, and budgets."""

    own = ", ".join(f"{service} {mib} MiB" for service, mib in card.gideon_by_service) or "none"
    budgets = ", ".join(
        f"{budget.role} {budget.mib} MiB ({float(budget.fraction):g} of card)"
        for budget in card.budgets
    )
    return (
        f"GPU index {card.position} UUID {card.uuid}: {card.free_mib} of {card.total_mib} MiB free "
        f"({format_gb(card.free_mib * MEBIBYTE)} of {format_gb(card.total_mib * MEBIBYTE)}), "
        f"GIDEON holding {own}, others {card.others_count} process(es), {card.others_mib} MiB, "
        f"budget {budgets}"
    )


class GpuMemoryCheck(PreflightCheck):
    """Judge model server memory budgets against live card availability."""

    name = "gpu-memory"
    summary = "check free GPU memory before model servers start"

    def run(self, context: PreflightContext) -> CheckReport:
        profile = select_profile(context.models, context.site.hardware_profile)
        if isinstance(profile, Problem):
            return CheckReport(Severity.REFUSE, profile.problem, profile.fix)
        if context.no_gpu:
            return CheckReport(Severity.PASS, "skipped: no-GPU host")
        servers = gpu_servers(profile)
        if isinstance(servers, CheckReport):
            return servers
        card_result = context.host.run(GPU_MEMORY_CARD_ARGV)
        if card_result.returncode != 0:
            return nvidia_failure(card_result)
        cards = parse_gpu_cards(card_result.stdout)
        if cards is None:
            return CheckReport(
                Severity.REFUSE, "nvidia-smi returned unreadable GPU memory", nogpu.GPU_DRIVER_FIX
            )
        recorded = gpus.read(context.host)
        if isinstance(recorded, Problem):
            return CheckReport(Severity.REFUSE, recorded.problem, recorded.fix)
        resolved = gpus.resolve(recorded, tuple(card.uuid for card in cards))
        if isinstance(resolved, Problem):
            return CheckReport(Severity.REFUSE, resolved.problem, resolved.fix)
        for server in servers:
            if server.position >= len(resolved):
                return CheckReport(
                    Severity.REFUSE,
                    f"profile {profile.name} assigns {server.role} GPU index {server.position}, "
                    f"but the GPU record names {len(resolved)} card(s)",
                    gpus.re_record_fix(),
                )
        reading = read_gpu_memory(context.host, cards, resolved, servers)
        if isinstance(reading, CheckReport):
            return reading
        judged, listing_detail = reading
        ordered = sorted(judged, key=lambda card: (card.fits, card.position))
        detail = "; ".join(gpu_memory_detail(card) for card in ordered)
        if recorded is None:
            detail = "unrecorded; today's order, which the first render records; " + detail
        if listing_detail:
            detail += "; " + listing_detail
        if any(not card.fits for card in judged):
            return CheckReport(
                Severity.REFUSE, detail,
                _GPU_MEMORY_FIX.format(command=report.command("preflight")),
            )
        return CheckReport(Severity.PASS, detail)
