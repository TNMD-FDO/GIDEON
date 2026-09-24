"""Network checks for the host preflight."""

import re

from gideon.host.checks import (
    CheckReport,
    PreflightCheck,
    PreflightContext,
    Severity,
)
from gideon.host.egress import EgressHost
from gideon.host.images import parse_registry
from gideon.host.steps import ProvisionContext, temporary_wgetrc, wget_argv

PROBE_TIMEOUT_SECONDS = 10
"""Fixed wget connect/read timeout; operational, not a tuned product value."""

_EGRESS_FIX = (
    "Allow the listed hosts through the firewall or configure egress_proxy "
    "per docs/runbooks/release-files.md §5, then re-run preflight."
)
_PORT_FIX = "Stop or move the squatting process, then re-run preflight."
_HOSTNAME_FIX = (
    "Create the site hostname's DNS record per "
    "docs/runbooks/office-services-setup.md §2, then re-run preflight."
)
_NTP_FIX = (
    "Configure chrony against the office domain controllers, then re-run "
    "preflight."
)
_PROCESS_RE = re.compile(r'users:\(\("([^"]+)"')


def _egress_hosts(context: PreflightContext) -> list[EgressHost] | None:
    """The pre-install probe list, or ``None`` when the registry key is unusable.

    The allowlist loader already guarantees every group exists, so the only
    unresolvable input here is the site ``registry`` value.
    """

    hosts: list[EgressHost] = []
    seen: set[str] = set()
    for group_name in ("host-provisioning", "install-upgrade"):
        group = context.egress.group(group_name)
        if group is not None:
            for entry in group.hosts:
                if entry.host not in seen:
                    hosts.append(entry)
                    seen.add(entry.host)

    registry = parse_registry(context.site.registry)
    if registry is None:
        return None
    authority = registry.authority
    if authority not in seen:
        hosts.append(EgressHost(authority, f"{registry.scheme}://{authority}/v2/"))
    return hosts


# wget -S writes each response's status line to stderr.
_HTTP_STATUS = re.compile(r"HTTP/[\d.]+\s+(\d{3})")

# On wget exit 8 (the server answered with an error) these statuses still
# prove the origin is reachable: 401 is an auth-gated registry endpoint,
# 404 a probe path the real origin does not serve. Anything else — proxy
# block pages, 407, 5xx — is a miss and named, unless the allowlist entry
# declares the status expected for that host (signed-URL CDNs answer 403
# to every unsigned probe — their healthy behaviour).
_REACHABLE_ERROR_STATUSES = frozenset({401, 404})


def _probe_verdict(
    returncode: int, stderr: str, expect: tuple[int, ...] = ()
) -> str | None:
    """``None`` when the origin answered, else a short miss description."""

    if returncode == 0:
        return None
    if returncode == 6:
        # wget exits 6 (authentication failure) on a 401 — the server
        # demanded credentials, which is proof it answered.
        return None
    if returncode == 8:
        statuses = _HTTP_STATUS.findall(stderr)
        if statuses and (
            int(statuses[-1]) in _REACHABLE_ERROR_STATUSES
            or int(statuses[-1]) in expect
        ):
            return None
        return f"HTTP {statuses[-1]}" if statuses else "server error"
    return "no response"


class EgressCheck(PreflightCheck):
    """Probe the pre-install egress destinations."""

    name = "egress"
    summary = "probe required host-provisioning and install-upgrade egress"

    def run(self, context: PreflightContext) -> CheckReport:
        entries = _egress_hosts(context)
        if entries is None:
            return CheckReport(
                Severity.REFUSE,
                f"the registry key {context.site.registry!r} has no usable host",
                "Correct the registry key in /etc/gideon/site.yaml, then re-run preflight.",
            )

        with temporary_wgetrc(context.host, command="preflight", prefix="gideon-preflight") as wgetrc:
            if not wgetrc.ok:
                return CheckReport(
                    Severity.REFUSE,
                    wgetrc.problem or "proxy auth unavailable",
                    wgetrc.fix,
                )
            provision_context = ProvisionContext(
                host=context.host,
                lock=context.lock,
                site=context.site,
            )
            missed: list[str] = []
            for entry in entries:
                argv = wget_argv(provision_context, "/dev/null", entry.probe_url)
                argv[1:1] = [
                    "--tries=1",
                    "-S",
                    f"--connect-timeout={PROBE_TIMEOUT_SECONDS}",
                    f"--read-timeout={PROBE_TIMEOUT_SECONDS}",
                ]
                argv = wgetrc.prefix(argv)
                result = context.host.run(argv)
                verdict = _probe_verdict(
                    result.returncode, result.stderr, entry.expect
                )
                if verdict is not None:
                    missed.append(f"{entry.host} ({verdict})")
            if missed:
                return CheckReport(
                    Severity.REFUSE,
                    f"unreachable hosts: {', '.join(missed)}",
                    _EGRESS_FIX,
                )
            return CheckReport(
                Severity.PASS,
                f"all {len(entries)} required egress hosts responded",
            )


def _port(line: str) -> int | None:
    fields = line.split()
    if len(fields) < 4:
        return None
    try:
        return int(fields[3].rsplit(":", 1)[1].rstrip("]"))
    except (IndexError, ValueError):
        return None


def _holder(line: str) -> str:
    match = _PROCESS_RE.search(line)
    if match is not None:
        return match.group(1)
    fields = line.split()
    return fields[-1] if len(fields) > 5 else "unknown process"


def _docker_owner(context: PreflightContext, port: int) -> str | None:
    """The single container publishing *port*, or ``None`` when unresolvable."""

    result = context.host.run(
        ["docker", "ps", "--filter", f"publish={port}", "--format", "{{.Names}}"]
    )
    if result.returncode != 0:
        return None
    names = result.stdout.split()
    return names[0] if len(names) == 1 else None


def _expected_container(port: int, owner: str | None) -> bool:
    # 5000 belongs to provision's registry container by exact name; 443's
    # Caddy container name is not pinned yet, so match on the service.
    if owner is None:
        return False
    if port == 5000:
        return owner == "gideon-registry"
    if port == 9090:
        return "prometheus" in owner
    return "caddy" in owner


class PortsCheck(PreflightCheck):
    """Ensure ingress ports are free or owned by expected services."""

    name = "ports"
    summary = "check ports 443, 5000, and 9090 for expected owners"

    def run(self, context: PreflightContext) -> CheckReport:
        result = context.host.run(["ss", "-ltnp"])
        if result.returncode != 0:
            return CheckReport(
                Severity.REFUSE,
                "could not inspect listening TCP ports",
                "Install ss or repair the host networking tools, then re-run preflight.",
            )
        holders: dict[int, list[str]] = {443: [], 5000: [], 9090: []}
        for line in result.stdout.splitlines():
            port = _port(line)
            if port in holders:
                holders[port].append(_holder(line))
        unexpected: list[str] = []
        details: dict[int, str] = {}
        for port, actual in holders.items():
            if not actual:
                details[port] = f"{port} is free"
                continue
            for holder in set(actual):
                if port == 443 and holder == "caddy":
                    details[port] = f"{port} held by caddy"
                elif holder == "docker-proxy":
                    owner = _docker_owner(context, port)
                    if _expected_container(port, owner):
                        details[port] = f"{port} held by container {owner}"
                    else:
                        unexpected.append(
                            f"port {port} held by container "
                            f"{owner or '(unresolvable)'}"
                        )
                else:
                    unexpected.append(f"port {port} held by {holder}")
        if unexpected:
            return CheckReport(Severity.REFUSE, "; ".join(unexpected), _PORT_FIX)
        detail = ", ".join(details[port] for port in sorted(details))
        return CheckReport(Severity.PASS, detail)


class HostnameCheck(PreflightCheck):
    """Check that the configured site hostname resolves."""

    name = "hostname"
    summary = "resolve the configured site hostname"

    def run(self, context: PreflightContext) -> CheckReport:
        hostname = context.site.hostname
        result = context.host.run(["getent", "hosts", hostname])
        if result.returncode == 0 and result.stdout.strip():
            return CheckReport(Severity.PASS, f"{hostname} resolves")
        return CheckReport(Severity.REFUSE, f"{hostname} does not resolve", _HOSTNAME_FIX)


class NtpCheck(PreflightCheck):
    """Warn when the host clock is not synchronized."""

    name = "ntp"
    summary = "check live NTP synchronization"

    def run(self, context: PreflightContext) -> CheckReport:
        result = context.host.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"]
        )
        if result.returncode == 0 and result.stdout.strip().lower() == "yes":
            return CheckReport(Severity.PASS, "NTP is synchronized")
        return CheckReport(Severity.WARN, "NTP is not synchronized", _NTP_FIX)
