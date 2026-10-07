"""Contracts shared by the host preflight checks."""

import re
from dataclasses import dataclass
from enum import Enum

from gideon.host.corpus.lockfile import Lockfile
from gideon.host.courts import CourtMap
from gideon.host.egress import EgressAllowlist
from gideon.host.lock import HostLock
from gideon.host.models import GIGABYTE, ModelsLock
from gideon.host.secrets import SecretReadResult as SecretReadResult
from gideon.host.secrets import read_secret
from gideon.host.site import SiteConfig
from gideon.host.sysio import Host

read_secret_file = read_secret


def format_gb(byte_count: int) -> str:
    """Render a byte count as decimal gigabytes with one decimal place."""

    return f"{byte_count / GIGABYTE:.1f} GB"


def meminfo_kb(output: str, field: str) -> int | None:
    """Read a /proc/meminfo field in kB with the grammar shared by both checks."""

    for line in output.splitlines():
        match = re.fullmatch(rf"\s*{re.escape(field)}:\s*([0-9]+)\s+kB\s*", line)
        if match is not None:
            return int(match.group(1))
    return None


class Severity(Enum):
    """The operator-facing severity; ``INERT`` is a judgment whose data has not shipped."""

    PASS = "pass"
    WARN = "warn"
    REFUSE = "refuse"
    INERT = "inert"


@dataclass(frozen=True, slots=True)
class CheckReport:
    """A check result with optional corrective action."""

    severity: Severity
    detail: str
    fix: str = ""


@dataclass(frozen=True, slots=True)
class PreflightContext:
    """Inputs shared by every install-time preflight check."""

    host: Host
    lock: HostLock
    models: ModelsLock
    site: SiteConfig
    egress: EgressAllowlist
    courts: CourtMap
    no_gpu: bool = False
    build_box: bool = False
    lockfile: Lockfile | None = None


class PreflightCheck:
    """Base class for one install-time preflight check."""

    name: str = ""
    summary: str = ""

    def run(self, context: PreflightContext) -> CheckReport:
        raise NotImplementedError


# Checks are registered as instances so the runner remains independent of
# construction details and the list is directly injectable in tests.
CHECKS: list[PreflightCheck] = []


def _registered_checks() -> tuple[PreflightCheck, ...]:
    from gideon.host.checks.artifacts import (
        DriverTestedCheck,
        HardwareProfileCheck,
        JurisdictionCheck,
        OsKernelCheck,
    )
    from gideon.host.checks.capacity import DataVolumeCheck, HostMemoryCheck
    from gideon.host.checks.network import (
        EgressCheck,
        HostnameCheck,
        NtpCheck,
        PortsCheck,
    )
    from gideon.host.checks.services import BackupSshCheck, LdapCheck, SmtpCheck

    return (
        EgressCheck(),
        PortsCheck(),
        HostnameCheck(),
        NtpCheck(),
        LdapCheck(),
        BackupSshCheck(),
        SmtpCheck(),
        DataVolumeCheck(),
        HostMemoryCheck(),
        JurisdictionCheck(),
        HardwareProfileCheck(),
        DriverTestedCheck(),
        OsKernelCheck(),
    )


CHECKS.extend(_registered_checks())
