"""Host facts gathered through the bare-host system-I/O seam: the GPU record, the service gid."""

import re
from dataclasses import dataclass

from gideon.host import gpus, nogpu, report
from gideon.host.secrets import SERVICE_GROUP_PROBLEM, service_group_gid
from gideon.host.sysio import Host


@dataclass(frozen=True, slots=True)
class HostFacts:
    """Facts read from the host and supplied to the pure render core."""

    gpu_uuids: tuple[str, ...]  # Recorded cards by lock position; first use takes today's order.
    service_gid: int


@dataclass(frozen=True, slots=True)
class FactsError:
    """A refusal encountered while gathering host facts."""

    problem: str
    fix: str


_GPU_UUID = re.compile(r"\(UUID:\s*([^\)\s]+)\)")


def _service_group_fix() -> str:
    return f"Run {report.command('host provision --only service-user')}, then re-run render."


def gather_facts(host: Host, *, no_gpu: bool = False) -> HostFacts | FactsError:
    """Read present cards and bind GPU UUIDs to recorded lock positions.

    A no-GPU host (the marker) has no UUIDs and is never probed; a GPU host
    without a working driver refuses naming both ways out. First use records
    today's order after the other host facts have been checked.
    """

    if no_gpu:
        present: tuple[str, ...] = ()
    else:
        result = host.run(["nvidia-smi", "-L"])
        if result.returncode != 0:
            if result.returncode == 127:
                problem = "nvidia-smi is not available"
            else:
                detail = result.stderr.strip() or f"exit {result.returncode}"
                problem = f"nvidia-smi -L failed: {detail}"
            return FactsError(problem=problem, fix=nogpu.GPU_DRIVER_FIX)

        found: list[str] = []
        for line in result.stdout.splitlines():
            match = _GPU_UUID.search(line)
            if match is not None:
                found.append(match.group(1))
        present = tuple(found)
    service_gid = service_group_gid(host)
    if service_gid is None:
        return FactsError(problem=SERVICE_GROUP_PROBLEM, fix=_service_group_fix())
    if no_gpu:
        return HostFacts(gpu_uuids=(), service_gid=service_gid)
    bound = gpus.bind(host, present)
    if isinstance(bound, report.Problem):
        return FactsError(problem=bound.problem, fix=bound.fix)
    return HostFacts(gpu_uuids=bound, service_gid=service_gid)
