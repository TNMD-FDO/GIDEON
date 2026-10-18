"""Network checks for the host preflight."""

import ipaddress
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from gideon.host import cotenants
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
PROBE_RETRY_WAIT_SECONDS = 5
"""Fixed wait before re-probing hosts that gave no response; operational, like the timeout."""
DOCKER_NETWORK_TIMEOUT_SECONDS = 10
"""Bound on each Docker network read; the client has no request timeout."""
GETENT_TIMEOUT_SECONDS = 5
"""Bound on each host lookup; a resolver with a dead upstream retries long."""

NETWORK_LS_ARGV = ("docker", "network", "ls", "--format", "{{.ID}}")


def network_inspect_argv(ids: tuple[str, ...]) -> tuple[str, ...]:
    """Inspect the listed network ids in one read, one tab-separated line each."""

    template = (
        '{{.Name}}\t{{with index .Labels "com.docker.compose.project"}}'
        '{{.}}{{end}}\t{{range .IPAM.Config}}{{.Subnet}} {{end}}'
    )
    return ("docker", "network", "inspect", "--format", template, *ids)

_EGRESS_FIX = (
    "Allow the listed hosts through the firewall or configure egress_proxy "
    "per docs/runbooks/release-files.md §5, then re-run preflight."
)
_PORT_FIX = (
    "Stop the process or container, or publish the container on another port, "
    "then re-run preflight."
)
_DOCKER_FIX = (
    "Repair docker ps so the container publishing the port can be listed, "
    "then re-run preflight."
)
_NETWORK_COVER_FIX = (
    "Wait for the named project to end, set docker_address_pool clear of the "
    "office's networks per docs/runbooks/install-upgrade.md §1 and re-run "
    "provision, or agree another subnet with the network's owner per "
    "docs/runbooks/install-upgrade.md §10; then re-run preflight."
)
_NETWORK_LS_FIX = (
    "Repair docker network ls so the daemon's networks can be read, "
    "then re-run preflight."
)
_NETWORK_INSPECT_FIX = (
    "Repair docker network inspect so the daemon's networks can be read, "
    "then re-run preflight."
)
_HOSTNAME_FIX = (
    "Create the site hostname's DNS record per "
    "docs/runbooks/office-services-setup.md §2, then re-run preflight."
)
_NTP_FIX = (
    "Configure chrony against the office domain controllers, then re-run "
    "preflight."
)
_PROCESS_RE = re.compile(r'users:\(\("([^"]+)"')


@dataclass(frozen=True, slots=True)
class NetworkRow:
    """A Docker network's identity and IPAM subnets."""

    name: str
    project: str | None
    subnets: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]


def parse_network_rows(stdout: str) -> tuple[NetworkRow, ...] | None:
    """Read inspect's tab-separated rows, refusing malformed output."""

    rows: list[NetworkRow] = []
    for line in stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 3 or not fields[0]:
            return None
        try:
            subnets = tuple(ipaddress.ip_network(value) for value in fields[2].split())
        except ValueError:
            return None
        rows.append(NetworkRow(fields[0], fields[1] or None, subnets))
    return tuple(rows)


_Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def _addresses(context: PreflightContext, host: str) -> tuple[_Address, ...]:
    """Every address ``getent ahosts`` gives, none when it fails or stalls."""

    try:
        result = context.host.run(
            ("getent", "ahosts", host), timeout=GETENT_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        return ()
    if result.returncode != 0:
        return ()
    addresses: dict[_Address, None] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        try:
            addresses[ipaddress.ip_address(fields[0])] = None
        except ValueError:
            continue
    return tuple(addresses)


def _resolved_hosts(
    context: PreflightContext,
) -> list[tuple[str, tuple[_Address, ...]]]:
    """Each judged site key with its host's addresses, every host resolved once."""

    site = context.site
    hosts = [
        ("hostname", site.hostname),
        ("auth.ldap.host", site.auth.ldap.host),
        ("backup.target.host", site.backup.target.host),
        ("alerts.smtp.host", site.alerts.smtp.host),
    ]
    if site.egress_proxy:
        try:
            proxy_host = urlsplit(site.egress_proxy).hostname
        except ValueError:
            proxy_host = None
        if proxy_host:
            hosts.append(("egress_proxy", proxy_host))
    registry = parse_registry(site.registry)
    if registry is not None:
        registry_host = urlsplit(f"//{registry.authority}").hostname
        if registry_host:
            hosts.append(("registry", registry_host))

    cache: dict[str, tuple[_Address, ...]] = {}
    for _, host in hosts:
        if host not in cache:
            cache[host] = _addresses(context, host)
    return [(key, cache[host]) for key, host in hosts]


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

# The one verdict a second probe can change: nothing answered at all.
_NO_RESPONSE = "no response"


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
    return _NO_RESPONSE


class EgressCheck(PreflightCheck):
    """Probe the pre-install egress destinations.

    A host that gave no response is probed once more, after one fixed wait,
    with the same argv, so a transient miss does not refuse the run; a host
    that answered with a status is judged on that one answer.
    """

    name = "egress"
    summary = "probe required host-provisioning and install-upgrade egress"

    def __init__(self, sleep: Callable[[float], None] = time.sleep) -> None:
        self._sleep = sleep

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

            def probe(entry: EgressHost, argv: list[str]) -> str | None:
                result = context.host.run(argv)
                return _probe_verdict(result.returncode, result.stderr, entry.expect)

            first_misses: list[tuple[EgressHost, list[str], str]] = []
            for entry in entries:
                argv = wget_argv(provision_context, "/dev/null", entry.probe_url)
                argv[1:1] = [
                    "--tries=1",
                    "-S",
                    f"--connect-timeout={PROBE_TIMEOUT_SECONDS}",
                    f"--read-timeout={PROBE_TIMEOUT_SECONDS}",
                ]
                argv = wgetrc.prefix(argv)
                verdict = probe(entry, argv)
                if verdict is not None:
                    first_misses.append((entry, argv, verdict))

            if any(verdict == _NO_RESPONSE for _, _, verdict in first_misses):
                self._sleep(PROBE_RETRY_WAIT_SECONDS)
            missed: list[str] = []
            recovered: list[str] = []
            for entry, argv, verdict in first_misses:
                if verdict != _NO_RESPONSE:
                    missed.append(f"{entry.host} ({verdict})")
                    continue
                second = probe(entry, argv)
                if second is None:
                    recovered.append(f"{entry.host} (first {verdict})")
                else:
                    missed.append(f"{entry.host} ({verdict}, then {second})")
            second_probe = (
                f"; on a second probe: {', '.join(recovered)}" if recovered else ""
            )
            if missed:
                return CheckReport(
                    Severity.REFUSE,
                    f"unreachable hosts: {', '.join(missed)}{second_probe}",
                    _EGRESS_FIX,
                )
            return CheckReport(
                Severity.PASS,
                f"all {len(entries)} required egress hosts responded{second_probe}",
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


class PortsCheck(PreflightCheck):
    """Ensure the required ports are free or held by GIDEON containers."""

    name = "ports"
    summary = "check ports 443, 5000, and 9090 are free or held by GIDEON"

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
        needs_port_fix = False
        needs_docker_fix = False
        for port, actual in holders.items():
            if not actual:
                details[port] = f"{port} is free"
                continue
            for holder in dict.fromkeys(actual):
                if holder == "docker-proxy":
                    listing = context.host.run(cotenants.publish_ps_argv(port))
                    if listing.returncode != 0:
                        unexpected.append(
                            f"port {port} held by docker-proxy, and docker ps could not "
                            "list the container publishing it"
                        )
                        needs_docker_fix = True
                        continue
                    rows = cotenants.parse_rows(listing.stdout)
                    if not rows:
                        unexpected.append(
                            f"port {port} held by docker-proxy, and no running container publishes it"
                        )
                        needs_docker_fix = True
                        continue
                    unmarked = cotenants.foreign(rows)
                    if unmarked:
                        unexpected.append(f"port {port} held by {cotenants.describe(unmarked)}")
                        needs_port_fix = True
                        continue
                    details[port] = "; ".join(
                        f"{port} held by container {row.name} "
                        f"({'no Compose project' if row.project is None else f'project {row.project}'})"
                        for row in rows
                    )
                else:
                    unexpected.append(f"port {port} held by {holder}")
                    needs_port_fix = True
        if unexpected:
            fixes = []
            if needs_port_fix:
                fixes.append(_PORT_FIX)
            if needs_docker_fix:
                fixes.append(_DOCKER_FIX)
            return CheckReport(Severity.REFUSE, "; ".join(unexpected), " ".join(fixes))
        detail = ", ".join(details[port] for port in sorted(details))
        return CheckReport(Severity.PASS, detail)


class DockerNetworksCheck(PreflightCheck):
    """Find Docker subnets that cover a site host or LAN range."""

    name = "docker-networks"
    summary = "check no Docker network's subnet covers a site host or LAN range"

    def run(self, context: PreflightContext) -> CheckReport:
        try:
            listing = context.host.run(
                NETWORK_LS_ARGV, timeout=DOCKER_NETWORK_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired:
            return CheckReport(
                Severity.REFUSE, "docker network ls timed out", _NETWORK_LS_FIX
            )
        if listing.returncode != 0:
            return CheckReport(
                Severity.REFUSE, "could not list Docker networks", _NETWORK_LS_FIX
            )

        ids = tuple(listing.stdout.split())
        rows: tuple[NetworkRow, ...] = ()
        if ids:
            try:
                inspected = context.host.run(
                    network_inspect_argv(ids), timeout=DOCKER_NETWORK_TIMEOUT_SECONDS
                )
            except subprocess.TimeoutExpired:
                return CheckReport(
                    Severity.REFUSE,
                    "docker network inspect timed out",
                    _NETWORK_INSPECT_FIX,
                )
            if inspected.returncode != 0:
                return CheckReport(
                    Severity.REFUSE,
                    "could not inspect Docker networks",
                    _NETWORK_INSPECT_FIX,
                )
            parsed = parse_network_rows(inspected.stdout)
            if parsed is None or len(parsed) != len(ids):
                return CheckReport(
                    Severity.REFUSE,
                    "could not read Docker network inspect output",
                    _NETWORK_INSPECT_FIX,
                )
            rows = parsed

        hosts = _resolved_hosts(context)
        lan_ranges = tuple(ipaddress.ip_network(cidr) for cidr in context.site.lan_cidrs)
        covering: list[str] = []
        for row in rows:
            keys = [
                key
                for key, addresses in hosts
                if any(address in subnet for address in addresses for subnet in row.subnets)
            ]
            keys.extend(
                f"lan_cidrs[{index}]"
                for index, lan_range in enumerate(lan_ranges)
                if any(
                    lan_range.version == subnet.version and lan_range.overlaps(subnet)
                    for subnet in row.subnets
                )
            )
            if keys:
                project = (
                    "no Compose project"
                    if row.project is None
                    else f"project {row.project}"
                )
                covering.append(
                    f"network {row.name} ({project}) covers {', '.join(keys)}"
                )
        if covering:
            return CheckReport(
                Severity.REFUSE, "; ".join(covering), _NETWORK_COVER_FIX
            )
        return CheckReport(
            Severity.PASS,
            f"judged {len(rows)} Docker networks, {len(hosts)} site hosts, "
            f"and {len(lan_ranges)} LAN ranges",
        )


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
