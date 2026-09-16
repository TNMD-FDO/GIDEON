"""Checks judged against the release artifacts: the locks now, courts.yaml when it ships."""

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from subprocess import CompletedProcess

from gideon.host import nogpu
from gideon.host.checks import (
    CheckReport,
    PreflightCheck,
    PreflightContext,
    Severity,
    format_gb,
)
from gideon.host.models import GIGABYTE, select_profile
from gideon.host.report import Problem
from gideon.host.steps import ProvisionContext, package_version

_JURISDICTION_INERT = "courts.yaml and the corpus lockfile ship with slices 2–3"
_JURISDICTION_FIX = "Correct the jurisdiction identifiers using the §3.6 step 0 checklist, then re-run preflight."
_DRIVER_FIX = "Install the lock-pinned NVIDIA driver, then re-run preflight."
_KERNEL_FIX = "Reconcile the running kernel with the release evidence, then re-run preflight."
_PROFILE_FIX = (
    "Provide the hardware the profile requires, or set hardware_profile in "
    "/etc/gideon/site.yaml to a profile this host satisfies, then re-run preflight."
)
_FACT_FIX = "Restore uname and /proc/meminfo on the host, then re-run preflight."
_MEBIBYTE = 2**20
_KIBIBYTE = 1024
_VERSION = re.compile(r"\d+(?:\.\d+){0,3}")


class JurisdictionCheck(PreflightCheck):
    """Validate site court ids when corpus court data is supplied.

    *court_map* is courts.yaml's shape (a state id → the court ids it expands
    to) and *courts* the installed lockfile's flat ``courts[]`` set — the
    interim injection seams until both artifacts land with slices 2–3.
    A state id absent from the map is an unknown id (refuse); a known state
    whose expansion is not fully installed is the §1.5 warn row.
    """

    name = "jurisdiction"
    summary = "validate configured jurisdiction against corpus court data"

    def __init__(
        self,
        court_map: Mapping[str, Collection[str]] | None = None,
        courts: Collection[str] | None = None,
    ) -> None:
        self.court_map = court_map
        self.courts = courts

    def run(self, context: PreflightContext) -> CheckReport:
        if self.court_map is None or self.courts is None:
            return CheckReport(Severity.INERT, _JURISDICTION_INERT)
        ids = set(self.courts)
        jurisdiction = context.site.jurisdiction
        missing: list[str] = []
        if jurisdiction.circuit not in ids:
            missing.append(f"circuit {jurisdiction.circuit!r}")
        missing.extend(
            f"district {district!r}"
            for district in jurisdiction.districts
            if district not in ids
        )
        missing.extend(
            f"state {state!r}"
            for state in jurisdiction.states
            if state not in self.court_map
        )
        if missing:
            return CheckReport(
                Severity.REFUSE,
                "unknown jurisdiction id(s): " + ", ".join(missing),
                _JURISDICTION_FIX,
            )
        pending = [
            state
            for state in jurisdiction.states
            if not ids.issuperset(self.court_map[state])
        ]
        if pending:
            rendered = ", ".join(repr(state) for state in pending)
            return CheckReport(
                Severity.WARN,
                f"states[] courts not yet in the corpus: {rendered}; "
                "the state tier is inert until a derived cut adds them",
                "Follow the derived-cut runbook section (§8.7) when state courts are wanted.",
            )
        return CheckReport(Severity.PASS, "configured jurisdiction ids are all present")


@dataclass(frozen=True, slots=True)
class _GpuFacts:
    """Facts for one GPU reported by the host probes."""

    model: str
    compute_capability: str
    vram_bytes: int
    architecture: str


def _gpu_facts(output: str) -> list[_GpuFacts] | None:
    facts: list[_GpuFacts] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        columns = [column.strip() for column in line.split(",")]
        if len(columns) != 3 or not columns[0] or not columns[1]:
            return None
        match = re.fullmatch(r"([0-9]+)\s+MiB", columns[2])
        if match is None:
            return None
        facts.append(
            _GpuFacts(
                model=columns[0],
                compute_capability=columns[1],
                vram_bytes=int(match.group(1)) * _MEBIBYTE,
                architecture="",
            )
        )
    return facts


def _architectures(output: str) -> list[str]:
    architectures: list[str] = []
    for line in output.splitlines():
        match = re.match(r"^\s*Product Architecture\s*:\s*(\S.*?)\s*$", line)
        if match is not None:
            architectures.append(match.group(1))
    return architectures


def _mem_total_kb(output: str) -> int | None:
    for line in output.splitlines():
        match = re.fullmatch(r"\s*MemTotal:\s*([0-9]+)\s+kB\s*", line)
        if match is not None:
            return int(match.group(1))
    return None


class HardwareProfileCheck(PreflightCheck):
    """Compare live host facts with the selected profile's requirements."""

    name = "hardware-profile"
    summary = "check the configured hardware profile and live GPU facts"

    def run(self, context: PreflightContext) -> CheckReport:
        profile = select_profile(context.models, context.site.hardware_profile)
        if isinstance(profile, Problem):
            return CheckReport(Severity.REFUSE, profile.problem, profile.fix)
        if context.no_gpu:
            return CheckReport(
                Severity.PASS,
                "skipped: no-GPU host; platform, GPU facts, and DRAM are not judged",
            )
        non_reference = context.site.hardware_profile != context.models.reference
        platform_result = context.host.run(["uname", "-m"])
        platform_lines = [line.strip() for line in platform_result.stdout.splitlines() if line.strip()]
        if platform_result.returncode != 0 or len(platform_lines) != 1:
            return CheckReport(
                Severity.REFUSE,
                _unreadable_detail("uname -m", platform_result.stderr),
                _FACT_FIX,
            )
        platform = platform_lines[0]

        result = context.host.run(
            [
                "nvidia-smi",
                "--query-gpu=name,compute_cap,memory.total",
                "--format=csv,noheader",
            ]
        )
        if result.returncode != 0:
            return _nvidia_failure(result)
        facts = _gpu_facts(result.stdout)
        if facts is None:
            return CheckReport(
                Severity.REFUSE,
                "nvidia-smi returned unreadable GPU facts",
                nogpu.GPU_DRIVER_FIX,
            )

        architecture_result = context.host.run(["nvidia-smi", "-q"])
        if architecture_result.returncode != 0:
            return _nvidia_failure(architecture_result)
        architectures = _architectures(architecture_result.stdout)
        if len(architectures) != len(facts):
            return CheckReport(
                Severity.REFUSE,
                f"nvidia-smi -q returned {len(architectures)} architecture line(s) for {len(facts)} GPU(s)",
                nogpu.GPU_DRIVER_FIX,
            )
        facts = [
            _GpuFacts(fact.model, fact.compute_capability, fact.vram_bytes, architecture)
            for fact, architecture in zip(facts, architectures, strict=True)
        ]

        try:
            meminfo = context.host.read_text("/proc/meminfo")
        except OSError as exc:
            return CheckReport(
                Severity.REFUSE,
                f"could not read /proc/meminfo: {exc}",
                _FACT_FIX,
            )
        dram_kb = _mem_total_kb(meminfo)
        if dram_kb is None:
            return CheckReport(
                Severity.REFUSE,
                "MemTotal is missing or unreadable in /proc/meminfo",
                _FACT_FIX,
            )

        required = profile.requires
        shortfalls: list[str] = []
        if platform != required.platform:
            shortfalls.append(f"platform is {platform}, profile requires {required.platform}")
        if len(facts) != required.gpu.count:
            shortfalls.append(f"GPU count is {len(facts)}, profile requires {required.gpu.count}")
        for index, fact in enumerate(facts, 1):
            if fact.model != required.gpu.model:
                shortfalls.append(
                    f"GPU {index} model is {fact.model}, profile requires {required.gpu.model}"
                )
            if fact.compute_capability != required.gpu.compute_capability:
                shortfalls.append(
                    f"GPU {index} compute capability is {fact.compute_capability}, "
                    f"profile requires {required.gpu.compute_capability}"
                )
            if fact.architecture != required.gpu.architecture:
                shortfalls.append(
                    f"GPU {index} architecture is {fact.architecture} (per nvidia-smi -q), "
                    f"profile requires {required.gpu.architecture}"
                )
            if fact.vram_bytes < required.gpu.vram_gb * GIGABYTE:
                vram_mib = fact.vram_bytes // _MEBIBYTE
                shortfalls.append(
                    f"GPU {index} VRAM is {vram_mib} MiB ({format_gb(fact.vram_bytes)}), "
                    f"profile requires {required.gpu.vram_gb} GB"
                )
        dram_bytes = dram_kb * _KIBIBYTE
        if dram_bytes < required.dram_gb * GIGABYTE:
            shortfalls.append(
                f"DRAM is {dram_kb} kB ({format_gb(dram_bytes)}), "
                f"profile requires {required.dram_gb} GB"
            )
        if shortfalls:
            prefix = f"profile {profile.name!r}: " if non_reference else ""
            return CheckReport(Severity.REFUSE, prefix + "; ".join(shortfalls), _PROFILE_FIX)

        detail = _matched_detail(platform, facts, dram_bytes)
        if non_reference:
            return CheckReport(
                Severity.WARN,
                f"hardware profile is {context.site.hardware_profile!r}, not "
                f"{context.models.reference!r}; its requirements are met: {detail}",
                "Review the non-reference hardware profile against models.lock, then re-run preflight.",
            )
        return CheckReport(Severity.PASS, f"profile {profile.name!r} requirements are met: {detail}")


def _unreadable_detail(command: str, stderr: str) -> str:
    """Describe a failed or empty host fact probe without hiding its output."""

    detail = stderr.strip()
    return f"{command} could not determine the host fact" + (f": {detail}" if detail else "")


def _nvidia_failure(result: CompletedProcess[str]) -> CheckReport:
    """Render the shared driver refusal for either NVIDIA probe."""

    returncode = result.returncode
    stderr = result.stderr.strip()
    if returncode == 127:
        detail = "nvidia-smi is not available"
    else:
        detail = "nvidia-smi failed"
        if stderr:
            detail += f": {stderr}"
        else:
            detail += f" with exit code {returncode}"
    return CheckReport(Severity.REFUSE, detail, nogpu.GPU_DRIVER_FIX)


def _matched_detail(platform: str, facts: list[_GpuFacts], dram_bytes: int) -> str:
    """Render the facts that matched a selected profile (every GPU matched, so the first speaks for all)."""

    first = facts[0]
    return (
        f"GPU count {len(facts)}, model {first.model}; "
        f"architecture {first.architecture} (per nvidia-smi -q); "
        f"compute capability {first.compute_capability}; "
        f"per-GPU VRAM {format_gb(first.vram_bytes)}; "
        f"DRAM {format_gb(dram_bytes)}; platform {platform}"
    )


def _version_key(value: str) -> tuple[int, ...]:
    match = _VERSION.search(value)
    if match is None:
        return ()
    return tuple(int(part) for part in match.group(0).split("."))


class DriverTestedCheck(PreflightCheck):
    """Compare the installed driver version with release evidence."""

    name = "driver-tested"
    summary = "compare the installed driver with the tested lock pin"

    def run(self, context: PreflightContext) -> CheckReport:
        if context.no_gpu:
            return CheckReport(Severity.PASS, "skipped: no-GPU host")
        tested = context.lock.driver.tested
        if tested is None:
            return CheckReport(Severity.PASS, "no tested driver version recorded")
        actual = package_version(
            ProvisionContext(context.host, context.lock, context.site),
            context.lock.driver.package,
        )
        if actual is None:
            return CheckReport(Severity.REFUSE, "the locked NVIDIA driver is not installed", _DRIVER_FIX)
        if _version_key(actual) > _version_key(tested):
            return CheckReport(
                Severity.WARN,
                f"installed driver {actual} is above tested {tested}",
                "Review the driver change against the tested release evidence, then re-run preflight.",
            )
        if _version_key(actual) == _version_key(tested):
            return CheckReport(Severity.PASS, f"installed driver matches tested {tested}")
        return CheckReport(
            Severity.WARN,
            f"installed driver {actual} is below tested {tested}",
            "Install the tested driver version or record updated release evidence, then re-run preflight.",
        )


class OsKernelCheck(PreflightCheck):
    """Compare the running kernel with optional release evidence."""

    name = "os-kernel"
    summary = "compare the running kernel with tested release evidence"

    def run(self, context: PreflightContext) -> CheckReport:
        result = context.host.run(["uname", "-r"])
        running = result.stdout.strip() if result.returncode == 0 else ""
        if not running:
            return CheckReport(
                Severity.REFUSE,
                "could not determine the running kernel",
                _KERNEL_FIX,
            )
        tested = context.lock.kernel_tested
        if tested is None:
            return CheckReport(Severity.PASS, "no tested kernel recorded")
        if running == tested:
            return CheckReport(Severity.PASS, f"running kernel matches tested {tested}")
        return CheckReport(
            Severity.WARN,
            f"running kernel {running} differs from tested {tested}",
            _KERNEL_FIX,
        )
