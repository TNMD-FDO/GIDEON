"""Network and time-synchronization provisioning steps."""

import re
from pathlib import Path

from gideon.host.steps import (
    LIBVIRT_BRIDGE_CIDR,
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    apt_install,
    package_installed,
    site_required,
)

_UFW_TAG = "gideon-provision"
_AFTER_RULES = Path("/etc/ufw/after.rules")
_DOCKER_BEGIN = "# BEGIN gideon-provision docker-user"
_DOCKER_END = "# END gideon-provision docker-user"
_DOCKER_BLOCK = re.compile(
    rf"\n?{re.escape(_DOCKER_BEGIN)}\n.*?{re.escape(_DOCKER_END)}\n", re.DOTALL
)
# Packets arriving from a Docker bridge are container-originated (egress or
# inter-container) and must never meet the published-port rules; the default
# bridge is docker0 and every user-defined network is a br-<id> bridge.
_DOCKER_BRIDGES = ("docker0", "br-+")
# Docker-published ports and who may reach them: Caddy's 443 from the office
# LANs, and the registry's 5000 from the acceptance VM's bridge only.
_PUBLISHED_PORTS = (443, 5000)
_DOCKER_FIX = (
    "Rewrite the DOCKER-USER block in /etc/ufw/after.rules from site.lan_cidrs "
    "and reload ufw, then re-run provision."
)
_UFW_RULE = re.compile(
    r"^\[\s*(\d+)\]\s+(\d+)/tcp\s+ALLOW IN\s+(\S+).*#\s*gideon-provision\s*$"
)
_CHRONY_SOURCES = Path("/etc/chrony/sources.d/gideon.sources")
_CHRONY_FIX = "Configure chrony for the office domain controllers, then re-run provision."
_WAIT_ONLINE_DROPIN = Path(
    "/etc/systemd/system/systemd-networkd-wait-online.service.d/gideon.conf"
)
_WAIT_ONLINE_TEXT = (
    "[Service]\n"
    "ExecStart=\n"
    "ExecStart=/usr/lib/systemd/systemd-networkd-wait-online --any\n"
)
_WAIT_ONLINE_FIX = (
    f"Create {_WAIT_ONLINE_DROPIN} with the any-link wait command, then re-run provision."
)
_UFW_FIX = "Configure the tagged firewall rules from site.lan_cidrs, then re-run provision."


def _ufw_rules(
    context: ProvisionContext,
) -> tuple[bool, list[tuple[int, str, int]]] | None:
    result = context.host.run(["ufw", "status", "numbered"])
    if result.returncode != 0:
        return None
    active = any(line.strip() == "Status: active" for line in result.stdout.splitlines())
    rules: list[tuple[int, str, int]] = []
    for line in result.stdout.splitlines():
        if _UFW_TAG not in line:
            continue
        match = _UFW_RULE.match(line.strip())
        if match is None:
            return None
        rules.append((int(match.group(1)), match.group(3), int(match.group(2))))
    return active, rules


def _docker_user_rule(match: str, target: str) -> str:
    matched = f" {match}" if match else ""
    return f"-A DOCKER-USER{matched} -m comment --comment {_UFW_TAG} -j {target}"


def _docker_user_rules(lan_cidrs: list[str]) -> list[str]:
    """The DOCKER-USER rules that make ufw's allow-list true for Docker-published ports.

    Docker's own chains run before ufw's INPUT rules, so a port Compose
    publishes (Caddy's 443) is otherwise reachable from any routed network.
    Matching on the connection's original destination port needs no LAN
    interface name; anything arriving from a Docker bridge returns first, so
    container egress is judged by where it came from, never by its address.
    DOCKER-USER sits first in FORWARD, so every forwarded packet passes here —
    the acceptance VM's NAT egress to any HTTPS host included; the drops
    therefore name the Docker bridges the packet is forwarded INTO, which is
    what "published by Docker" means, and nothing else is judged.
    """

    port = "-p tcp -m conntrack --ctorigdstport {port} --ctdir ORIGINAL"
    rules = [_docker_user_rule("-m conntrack --ctstate RELATED,ESTABLISHED", "RETURN")]
    rules += [_docker_user_rule(f"-i {bridge}", "RETURN") for bridge in _DOCKER_BRIDGES]
    rules += [_docker_user_rule(f"-s {cidr} {port.format(port=443)}", "RETURN") for cidr in lan_cidrs]
    rules.append(_docker_user_rule(f"-s {LIBVIRT_BRIDGE_CIDR} {port.format(port=5000)}", "RETURN"))
    rules += [
        _docker_user_rule(f"-o {bridge} {port.format(port=published)}", "DROP")
        for published in _PUBLISHED_PORTS
        for bridge in _DOCKER_BRIDGES
    ]
    rules.append(_docker_user_rule("", "RETURN"))
    return rules


def _docker_user_block(lan_cidrs: list[str]) -> str:
    lines = [_DOCKER_BEGIN, "*filter", ":DOCKER-USER - [0:0]", *_docker_user_rules(lan_cidrs), "COMMIT", _DOCKER_END]
    return "\n".join(lines) + "\n"


def _current_block(text: str) -> str | None:
    match = _DOCKER_BLOCK.search(text)
    return None if match is None else match.group(0).lstrip("\n")


def _with_block(text: str, block: str) -> str:
    stripped = _DOCKER_BLOCK.sub("\n", text).rstrip("\n")
    return f"{stripped}\n\n{block}"


def _docker_user_loaded(context: ProvisionContext, expected_rules: int) -> bool:
    """True when the live chain's first rules are exactly the provision-owned ones.

    ufw loads after.rules with iptables-restore --noflush, under which a chain
    the file declares is flushed and rebuilt from the file (verified live on
    the reference box); demanding the owned rules at the chain head catches a
    foreign rule placed ahead of them by any other means.
    """

    result = context.host.run(["iptables", "-S", "DOCKER-USER"])
    if result.returncode != 0:
        return False
    rules = [line for line in result.stdout.splitlines() if line.startswith("-A ")]
    head = rules[:expected_rules]
    tagged = sum(f"--comment {_UFW_TAG}" in line for line in rules)
    return len(head) == expected_rules and all(
        f"--comment {_UFW_TAG}" in line for line in head
    ) and tagged == expected_rules


def _desired_rules(context: ProvisionContext) -> list[tuple[str, int]]:
    assert context.site is not None
    return sorted(
        (cidr, port)
        for cidr in context.site.lan_cidrs
        for port in (22, 443)
    )


class FirewallStep(Step):
    """Converge only the provision-owned UFW rule set."""

    name = "firewall"
    summary = "configure tagged UFW access for office LANs"
    needs_site = True

    def check(self, context: ProvisionContext) -> CheckResult:
        if context.site is None:
            return site_required()
        status = _ufw_rules(context)
        if status is None:
            return CheckResult(
                Disposition.UNFIXABLE,
                "ufw status could not be parsed",
                _UFW_FIX,
            )
        active, rules = status
        actual = sorted((cidr, port) for _, cidr, port in rules)
        desired = _desired_rules(context)
        if not active or actual != desired:
            return CheckResult(
                Disposition.DRIFT,
                f"tagged UFW rules are {actual!r}; desired {desired!r}",
                _UFW_FIX,
            )
        verbose = context.host.run(["ufw", "status", "verbose"])
        if verbose.returncode != 0 or "deny (incoming" not in verbose.stdout:
            return CheckResult(
                Disposition.DRIFT,
                "the UFW default incoming policy is not deny",
                _UFW_FIX,
            )
        block = _docker_user_block(context.site.lan_cidrs)
        try:
            after_rules = context.host.read_text(_AFTER_RULES)
        except FileNotFoundError:
            return CheckResult(Disposition.DRIFT, f"{_AFTER_RULES} is missing", _DOCKER_FIX)
        except (OSError, UnicodeError) as exc:
            return CheckResult(Disposition.UNFIXABLE, f"cannot read {_AFTER_RULES}: {exc}", _DOCKER_FIX)
        if _current_block(after_rules) != block:
            return CheckResult(
                Disposition.DRIFT,
                f"the DOCKER-USER block in {_AFTER_RULES} is missing or differs from site.lan_cidrs",
                _DOCKER_FIX,
            )
        if not _docker_user_loaded(context, len(_docker_user_rules(context.site.lan_cidrs))):
            return CheckResult(
                Disposition.DRIFT,
                "the DOCKER-USER chain does not carry the provision rules",
                _DOCKER_FIX,
            )
        return CheckResult(
            Disposition.CONVERGED,
            "tagged UFW rules and the DOCKER-USER block are current and active",
            "",
        )

    def apply(self, context: ProvisionContext) -> None:
        if context.site is None:
            raise RuntimeError("site file is required by firewall")
        status = _ufw_rules(context)
        if status is None:
            raise RuntimeError("refusing to mutate an unparsed UFW rule set")
        _, current = status
        desired = _desired_rules(context)
        needed = list(desired)
        stale_numbers: list[int] = []
        for number, cidr, port in sorted(current, reverse=True):
            rule = (cidr, port)
            if rule in needed:
                needed.remove(rule)
            else:
                stale_numbers.append(number)
        for number in sorted(stale_numbers, reverse=True):
            context.host.run(["ufw", "--force", "delete", str(number)], check=True)
        for cidr, port in needed:
            context.host.run(
                [
                    "ufw",
                    "allow",
                    "proto",
                    "tcp",
                    "from",
                    cidr,
                    "to",
                    "any",
                    "port",
                    str(port),
                    "comment",
                    _UFW_TAG,
                ],
                check=True,
            )
        block = _docker_user_block(context.site.lan_cidrs)
        try:
            after_rules = context.host.read_text(_AFTER_RULES)
        except FileNotFoundError:
            after_rules = ""
        if _current_block(after_rules) != block:
            context.host.write_text(_AFTER_RULES, _with_block(after_rules, block), mode=0o640)
        context.host.run(["ufw", "default", "deny", "incoming"], check=True)
        # Allow-22 rules have been added before enabling UFW, preserving SSH.
        context.host.run(["ufw", "--force", "enable"], check=True)
        # enable loads after.rules on activation; reload re-reads it when already active.
        context.host.run(["ufw", "reload"], check=True)


class WaitOnlineStep(Step):
    """Make network-online wait for any one managed link."""

    name = "wait-online"
    summary = "satisfy the boot-time network wait with any one link online"
    needs_site = False

    def check(self, context: ProvisionContext) -> CheckResult:
        try:
            current = context.host.read_text(_WAIT_ONLINE_DROPIN)
        except FileNotFoundError:
            return CheckResult(
                Disposition.DRIFT,
                f"{_WAIT_ONLINE_DROPIN} is missing",
                _WAIT_ONLINE_FIX,
            )
        except (OSError, UnicodeError) as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read {_WAIT_ONLINE_DROPIN}: {exc}",
                _WAIT_ONLINE_FIX,
            )
        if current != _WAIT_ONLINE_TEXT:
            return CheckResult(
                Disposition.DRIFT,
                f"{_WAIT_ONLINE_DROPIN} differs from the any-link wait configuration",
                _WAIT_ONLINE_FIX,
            )
        return CheckResult(
            Disposition.CONVERGED,
            f"{_WAIT_ONLINE_DROPIN} has the any-link wait configuration",
            "",
        )

    def apply(self, context: ProvisionContext) -> None:
        context.host.mkdir(
            _WAIT_ONLINE_DROPIN.parent,
            mode=0o755,
            parents=True,
            exist_ok=True,
        )
        context.host.write_text(_WAIT_ONLINE_DROPIN, _WAIT_ONLINE_TEXT)
        context.host.run(["systemctl", "daemon-reload"], check=True)
        context.host.run(
            ["systemctl", "reset-failed", "systemd-networkd-wait-online.service"],
            check=True,
        )


class TimeSyncStep(Step):
    """Install chrony and synchronize from the site's AD domain."""

    name = "time-sync"
    summary = "configure chrony from the office domain controller name"
    needs_site = True

    def check(self, context: ProvisionContext) -> CheckResult:
        if context.site is None:
            return site_required()
        if not package_installed(context, "chrony"):
            return CheckResult(Disposition.DRIFT, "chrony is not installed", _CHRONY_FIX)
        expected = f"pool {context.site.auth.ldap.host} iburst maxsources 3\n"
        if not context.host.exists(_CHRONY_SOURCES):
            return CheckResult(Disposition.DRIFT, f"{_CHRONY_SOURCES} is missing", _CHRONY_FIX)
        try:
            current = context.host.read_text(_CHRONY_SOURCES)
        except (OSError, UnicodeError) as exc:
            return CheckResult(Disposition.UNFIXABLE, f"cannot read {_CHRONY_SOURCES}: {exc}", _CHRONY_FIX)
        if current != expected:
            return CheckResult(Disposition.DRIFT, f"{_CHRONY_SOURCES} differs from the site domain", _CHRONY_FIX)
        enabled = context.host.run(["systemctl", "is-enabled", "chrony"])
        active = context.host.run(["systemctl", "is-active", "chrony"])
        if enabled.returncode != 0 or active.returncode != 0:
            return CheckResult(Disposition.DRIFT, "chrony is not enabled and active", _CHRONY_FIX)
        return CheckResult(Disposition.CONVERGED, "chrony is current and active", "")

    def apply(self, context: ProvisionContext) -> None:
        if context.site is None:
            raise RuntimeError("site file is required by time-sync")
        if not package_installed(context, "chrony"):
            apt_install(context, ["chrony"])
        expected = f"pool {context.site.auth.ldap.host} iburst maxsources 3\n"
        changed = True
        if context.host.exists(_CHRONY_SOURCES):
            try:
                changed = context.host.read_text(_CHRONY_SOURCES) != expected
            except (OSError, UnicodeError):
                changed = True
        context.host.mkdir(_CHRONY_SOURCES.parent, mode=0o755, parents=True, exist_ok=True)
        context.host.write_text(_CHRONY_SOURCES, expected)
        # Enable before reloading: chronyc can only talk to a running chronyd.
        context.host.run(["systemctl", "enable", "--now", "chrony"], check=True)
        if changed:
            context.host.run(["chronyc", "reload", "sources"], check=True)
