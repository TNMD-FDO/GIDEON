"""Network and time-synchronization provisioning steps."""

import re
from pathlib import Path

from gideon.host.cotenants import guard
from gideon.host.steps import (
    BOX_WIDE_SHORTFALL_FIX,
    LIBVIRT_BRIDGE_CIDR,
    BoxWideSetting,
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    StepFailure,
    apt_install,
    box_wide_shortfall,
    package_installed,
    site_required,
)
from gideon.host.sysio import Host

UFW_TAG = "gideon-provision"
AFTER_RULES = Path("/etc/ufw/after.rules")
CHAIN = "gideon-docker-user"
SHARED_CHAIN = "DOCKER-USER"
_FIREWALL_BEGIN = "# GIDEON BEGIN provision:firewall"
_FIREWALL_END = "# GIDEON END provision:firewall"
_LEGACY_BEGIN = "# BEGIN gideon-provision docker-user"
_LEGACY_END = "# END gideon-provision docker-user"
_FIREWALL_BLOCK = re.compile(
    rf"\n?{re.escape(_FIREWALL_BEGIN)}\n.*?{re.escape(_FIREWALL_END)}\n", re.DOTALL
)
_LEGACY_BLOCK = re.compile(
    rf"\n?{re.escape(_LEGACY_BEGIN)}\n.*?{re.escape(_LEGACY_END)}\n", re.DOTALL
)
# The one rule GIDEON places in the shared chain, as `iptables -S` prints it.
_JUMP_SPEC = f"-m comment --comment {UFW_TAG} -j {CHAIN}"
_JUMP = f"-A {SHARED_CHAIN} {_JUMP_SPEC}"
DOCKER_USER_UNIT = Path("/etc/systemd/system/gideon-docker-user.service")
DOCKER_USER_SERVICE = DOCKER_USER_UNIT.stem
# ufw loads after.rules at boot before Docker has created the shared chain, so
# the jump is installed by this unit between the two: the chain is created only
# when absent (Docker keeps a present one and its rules), and the jump inserted
# only when none is present, so a re-run never moves or repeats it.
_DOCKER_USER_UNIT_TEXT = (
    "[Unit]\n"
    f"Description=GIDEON firewall jump from {SHARED_CHAIN} into {CHAIN}\n"
    "After=ufw.service\n"
    "Before=docker.service\n"
    "\n"
    "[Service]\n"
    "Type=oneshot\n"
    f"ExecStart=-/usr/sbin/iptables -w -N {SHARED_CHAIN}\n"
    f'ExecStart=/bin/sh -c "/usr/sbin/iptables -w -C {SHARED_CHAIN} {_JUMP_SPEC}'
    f' || /usr/sbin/iptables -w -I {SHARED_CHAIN} 1 {_JUMP_SPEC}"\n'
    "\n"
    "[Install]\n"
    "WantedBy=multi-user.target\n"
)
# Packets arriving from a Docker bridge are container-originated (egress or
# inter-container) and must never meet the published-port rules; the default
# bridge is docker0 and every user-defined network is a br-<id> bridge.
_DOCKER_BRIDGES = ("docker0", "br-+")
# Docker-published ports and who may reach them: Caddy's 443 from the office
# LANs, and the registry's 5000 from the acceptance VM's bridge only.
_PUBLISHED_PORTS = (443, 5000)
_DOCKER_FIX = (
    "Rewrite GIDEON's firewall block in /etc/ufw/after.rules from site.lan_cidrs, "
    "reload ufw, and start gideon-docker-user.service, then re-run provision."
)
_UFW_RULE = re.compile(
    r"^\[\s*(\d+)\]\s+(\d+)/tcp\s+ALLOW IN\s+(\S+).*#\s*gideon-provision\s*$"
)
CHRONY_SOURCES = Path("/etc/chrony/sources.d/gideon.sources")
_CHRONY_FIX = "Configure chrony for the office domain controllers, then re-run provision."
WAIT_ONLINE_DROPIN = Path(
    "/etc/systemd/system/systemd-networkd-wait-online.service.d/gideon.conf"
)
_WAIT_ONLINE_TEXT = (
    "[Service]\n"
    "ExecStart=\n"
    "ExecStart=/usr/lib/systemd/systemd-networkd-wait-online --any\n"
)
_WAIT_ONLINE_FIX = (
    f"Create {WAIT_ONLINE_DROPIN} with the any-link wait command, then re-run provision."
)
_UFW_FIX = "Configure the tagged firewall rules from site.lan_cidrs, then re-run provision."
_INCOMING_POLICY = re.compile(r"Default:\s*(\w+)\s*\(incoming\)")
_UFW_READ_FIX = "Repair ufw so its status can be read, then re-run provision."


def _incoming_policy(context: ProvisionContext) -> str | None:
    try:
        result = context.host.run(["ufw", "status", "verbose"])
    except OSError:
        return None
    if result.returncode != 0:
        return None
    match = _INCOMING_POLICY.search(result.stdout)
    return match.group(1).lower() if match is not None else None


def _policy_refusal(context: ProvisionContext, setting: BoxWideSetting) -> CheckResult | None:
    policy = _incoming_policy(context)
    if policy is None:
        return CheckResult(
            Disposition.UNFIXABLE,
            "ufw status verbose could not report the default incoming policy",
            _UFW_READ_FIX,
        )
    if policy in {"deny", "reject"}:
        return None
    needed = "deny (incoming)" if policy == "allow" else "deny or reject (incoming)"
    return CheckResult(
        Disposition.UNFIXABLE,
        box_wide_shortfall(setting, f"{policy} (incoming)", needed),
        BOX_WIDE_SHORTFALL_FIX,
    )


def _status_active(stdout: str) -> bool:
    return any(line.strip() == "Status: active" for line in stdout.splitlines())


def ufw_active(host: Host) -> bool | None:
    """Read whether ufw is active, or None when its status cannot be read."""

    try:
        result = host.run(["ufw", "status"])
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return _status_active(result.stdout)


def _ufw_rules(
    context: ProvisionContext,
) -> tuple[bool, list[tuple[int, str, int]]] | None:
    try:
        result = context.host.run(["ufw", "status", "numbered"])
    except OSError:
        return None
    if result.returncode != 0:
        return None
    active = _status_active(result.stdout)
    rules: list[tuple[int, str, int]] = []
    for line in result.stdout.splitlines():
        if UFW_TAG not in line:
            continue
        match = _UFW_RULE.match(line.strip())
        if match is None:
            return None
        rules.append((int(match.group(1)), match.group(3), int(match.group(2))))
    return active, rules


def _docker_user_rule(chain: str, match: str, target: str) -> str:
    matched = f" {match}" if match else ""
    return f"-A {chain}{matched} -m comment --comment {UFW_TAG} -j {target}"


def _docker_user_rules(lan_cidrs: list[str]) -> list[str]:
    """Rules reached through DOCKER-USER that protect Docker-published ports.

    Docker's own chains run before ufw's INPUT rules, so a port Compose
    publishes (Caddy's 443) is otherwise reachable from any routed network.
    Matching on the connection's original destination port needs no LAN
    interface name; anything arriving from a Docker bridge returns first, so
    container egress is judged by where it came from, never by its address.
    DOCKER-USER sits first in FORWARD and jumps into GIDEON's chain. The drops
    name the Docker bridges the packet is forwarded INTO, so the acceptance
    VM's NAT egress to any HTTPS host is not judged. The final RETURN resumes
    DOCKER-USER at the rule after the jump.
    """

    port = "-p tcp -m conntrack --ctorigdstport {port} --ctdir ORIGINAL"
    rules = [_docker_user_rule(CHAIN, "-m conntrack --ctstate RELATED,ESTABLISHED", "RETURN")]
    rules += [_docker_user_rule(CHAIN, f"-i {bridge}", "RETURN") for bridge in _DOCKER_BRIDGES]
    rules += [_docker_user_rule(CHAIN, f"-s {cidr} {port.format(port=443)}", "RETURN") for cidr in lan_cidrs]
    rules.append(_docker_user_rule(CHAIN, f"-s {LIBVIRT_BRIDGE_CIDR} {port.format(port=5000)}", "RETURN"))
    rules += [
        _docker_user_rule(CHAIN, f"-o {bridge} {port.format(port=published)}", "DROP")
        for published in _PUBLISHED_PORTS
        for bridge in _DOCKER_BRIDGES
    ]
    rules.append(_docker_user_rule(CHAIN, "", "RETURN"))
    return rules


def _docker_user_block(lan_cidrs: list[str]) -> str:
    """GIDEON's marked block, declaring its own chain and never the shared one.

    ufw loads after.rules with iptables-restore --noflush, under which a chain
    the file declares is flushed and rebuilt from the file (verified live on
    the reference box): right for GIDEON's chain, and the reason DOCKER-USER,
    which other applications add to, is never declared here.
    """

    lines = [
        _FIREWALL_BEGIN,
        "*filter",
        f":{CHAIN} - [0:0]",
        *_docker_user_rules(lan_cidrs),
        "COMMIT",
        _FIREWALL_END,
    ]
    return "\n".join(lines) + "\n"


def _current_block(text: str) -> tuple[str, bool] | None:
    legacy = _LEGACY_BLOCK.search(text)
    if legacy is not None:
        return legacy.group(0).lstrip("\n"), True
    current = _FIREWALL_BLOCK.search(text)
    return None if current is None else (current.group(0).lstrip("\n"), False)


def _with_block(text: str, block: str) -> str:
    stripped = without_block(text).rstrip("\n")
    return f"{stripped}\n\n{block}"


def without_block(text: str) -> str:
    """Remove either GIDEON firewall marker block, preserving other lines."""

    stripped = _FIREWALL_BLOCK.sub("\n", text)
    stripped = _LEGACY_BLOCK.sub("\n", stripped).rstrip("\n")
    return f"{stripped}\n" if stripped else ""


def _iptables_rules(context: ProvisionContext, chain: str) -> list[str] | None:
    try:
        result = context.host.run(["iptables", "-w", "-S", chain])
    except OSError:
        return None
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.startswith("-A ")]


def tagged(line: str) -> bool:
    tokens = line.split()
    return any(
        first == "--comment" and second == UFW_TAG
        for first, second in zip(tokens, tokens[1:], strict=False)
    )


def _desired_rules(context: ProvisionContext) -> list[tuple[str, int]]:
    assert context.site is not None
    return sorted(
        (cidr, port)
        for cidr in context.site.lan_cidrs
        for port in (22, 443)
    )


class FirewallStep(Step):
    """Provision tagged UFW rules and GIDEON's Docker firewall chain."""

    name = "firewall"
    summary = "configure tagged UFW access for office LANs"
    needs_site = True
    settings = (BoxWideSetting("firewall default policy"),)

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
        if active:
            refusal = _policy_refusal(context, self.settings[0])
            if refusal is not None:
                return refusal
        else:
            return CheckResult(
                Disposition.DRIFT,
                "ufw is inactive, the installed default; GIDEON enables it with deny incoming and its tagged rules",
                _UFW_FIX,
            )
        actual = sorted((cidr, port) for _, cidr, port in rules)
        desired = _desired_rules(context)
        if actual != desired:
            return CheckResult(
                Disposition.DRIFT,
                f"tagged UFW rules are {actual!r}; desired {desired!r}",
                _UFW_FIX,
            )
        block = _docker_user_block(context.site.lan_cidrs)
        try:
            after_rules = context.host.read_text(AFTER_RULES)
        except FileNotFoundError:
            return CheckResult(
                Disposition.DRIFT,
                f"GIDEON's firewall block in {AFTER_RULES} is missing",
                _DOCKER_FIX,
            )
        except (OSError, UnicodeError) as exc:
            return CheckResult(Disposition.UNFIXABLE, f"cannot read {AFTER_RULES}: {exc}", _DOCKER_FIX)
        current_block = _current_block(after_rules)
        if current_block is None:
            return CheckResult(
                Disposition.DRIFT,
                f"GIDEON's firewall block in {AFTER_RULES} is missing",
                _DOCKER_FIX,
            )
        if current_block[1]:
            return CheckResult(
                Disposition.DRIFT,
                f"GIDEON's firewall block in {AFTER_RULES} stands under the previous markers",
                _DOCKER_FIX,
            )
        if current_block[0] != block:
            return CheckResult(
                Disposition.DRIFT,
                f"GIDEON's firewall block in {AFTER_RULES} differs from site.lan_cidrs",
                _DOCKER_FIX,
            )
        chain_rules = _iptables_rules(context, CHAIN)
        if chain_rules is None:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read GIDEON's firewall chain {CHAIN}",
                _DOCKER_FIX,
            )
        if chain_rules != _docker_user_rules(context.site.lan_cidrs):
            return CheckResult(
                Disposition.DRIFT,
                f"GIDEON's firewall chain {CHAIN} does not carry its rules",
                _DOCKER_FIX,
            )
        shared_rules = _iptables_rules(context, SHARED_CHAIN)
        if shared_rules is None:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read {SHARED_CHAIN}",
                _DOCKER_FIX,
            )
        jumps = sum(line == _JUMP for line in shared_rules)
        if jumps == 0:
            return CheckResult(
                Disposition.DRIFT,
                f"the jump from {SHARED_CHAIN} into {CHAIN} is missing",
                _DOCKER_FIX,
            )
        if jumps > 1:
            return CheckResult(
                Disposition.DRIFT,
                f"the jump from {SHARED_CHAIN} into {CHAIN} is present more than once",
                _DOCKER_FIX,
            )
        if any(tagged(line) and line != _JUMP for line in shared_rules):
            return CheckResult(
                Disposition.DRIFT,
                f"GIDEON's previous rules remain in {SHARED_CHAIN}",
                _DOCKER_FIX,
            )
        try:
            unit_text = context.host.read_text(DOCKER_USER_UNIT)
        except FileNotFoundError:
            return CheckResult(
                Disposition.DRIFT,
                f"{DOCKER_USER_UNIT} is missing",
                _DOCKER_FIX,
            )
        except (OSError, UnicodeError) as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read {DOCKER_USER_UNIT}: {exc}",
                _DOCKER_FIX,
            )
        if unit_text != _DOCKER_USER_UNIT_TEXT:
            return CheckResult(
                Disposition.DRIFT,
                f"{DOCKER_USER_UNIT} differs from GIDEON's firewall jump unit",
                _DOCKER_FIX,
            )
        try:
            enabled = context.host.run(["systemctl", "is-enabled", DOCKER_USER_SERVICE])
        except OSError as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read whether {DOCKER_USER_UNIT} is enabled: {exc}",
                _DOCKER_FIX,
            )
        if enabled.returncode != 0:
            return CheckResult(
                Disposition.DRIFT,
                f"{DOCKER_USER_UNIT} is not enabled",
                _DOCKER_FIX,
            )
        return CheckResult(
            Disposition.CONVERGED,
            "tagged UFW rules, GIDEON's firewall chain, its jump, and its unit are current",
            "",
        )

    def apply(self, context: ProvisionContext) -> None:
        if context.site is None:
            raise RuntimeError("site file is required by firewall")
        status = _ufw_rules(context)
        if status is None:
            raise StepFailure("ufw status could not be parsed", _UFW_FIX)
        active, current = status
        if active:
            refusal = _policy_refusal(context, self.settings[0])
            if refusal is not None:
                raise StepFailure(refusal.detail, refusal.fix)
        else:
            guard(context, "enabling ufw")
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
                    UFW_TAG,
                ],
                check=True,
            )
        block = _docker_user_block(context.site.lan_cidrs)
        try:
            after_rules = context.host.read_text(AFTER_RULES)
        except FileNotFoundError:
            after_rules = ""
        except (OSError, UnicodeError) as exc:
            raise StepFailure(f"cannot read {AFTER_RULES}: {exc}", _DOCKER_FIX) from exc
        if _current_block(after_rules) != (block, False):
            context.host.write_text(AFTER_RULES, _with_block(after_rules, block), mode=0o640)
        if not active:
            context.host.run(["ufw", "default", "deny", "incoming"], check=True)
            # Allow-22 rules have been added before enabling UFW, preserving SSH.
            context.host.run(["ufw", "--force", "enable"], check=True)
        # enable loads after.rules on activation; reload re-reads it when already active.
        context.host.run(["ufw", "reload"], check=True)
        try:
            unit_text = context.host.read_text(DOCKER_USER_UNIT)
        except FileNotFoundError:
            unit_text = None
        except (OSError, UnicodeError) as exc:
            raise StepFailure(f"cannot read {DOCKER_USER_UNIT}: {exc}", _DOCKER_FIX) from exc
        if unit_text != _DOCKER_USER_UNIT_TEXT:
            context.host.write_text(DOCKER_USER_UNIT, _DOCKER_USER_UNIT_TEXT, mode=0o644)
            context.host.run(["systemctl", "daemon-reload"], check=True)
        context.host.run(["systemctl", "enable", DOCKER_USER_SERVICE], check=True)
        # The unit is oneshot without RemainAfterExit, so start re-runs it and
        # restores a missing jump; the prune follows, so the previous rules keep
        # dropping until the jump is in.
        context.host.run(["systemctl", "start", DOCKER_USER_SERVICE], check=True)
        # By position, from the highest down: a delete by specification removes
        # the first identical rule, which for a duplicate jump is the kept one.
        result =context.host.run(["iptables", "-w", "-S", SHARED_CHAIN], check=True)
        shared_rules = [line for line in result.stdout.splitlines() if line.startswith("-A ")]
        keep_jump = False
        stale_positions: list[int] = []
        for position, line in enumerate(shared_rules, start=1):
            if line == _JUMP and not keep_jump:
                keep_jump = True
            elif tagged(line):
                stale_positions.append(position)
        for position in reversed(stale_positions):
            context.host.run(["iptables", "-w", "-D", SHARED_CHAIN, str(position)], check=True)


class WaitOnlineStep(Step):
    """Make network-online wait for any one managed link."""

    name = "wait-online"
    summary = "satisfy the boot-time network wait with any one link online"
    needs_site = False

    def check(self, context: ProvisionContext) -> CheckResult:
        try:
            current = context.host.read_text(WAIT_ONLINE_DROPIN)
        except FileNotFoundError:
            return CheckResult(
                Disposition.DRIFT,
                f"{WAIT_ONLINE_DROPIN} is missing",
                _WAIT_ONLINE_FIX,
            )
        except (OSError, UnicodeError) as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read {WAIT_ONLINE_DROPIN}: {exc}",
                _WAIT_ONLINE_FIX,
            )
        if current != _WAIT_ONLINE_TEXT:
            return CheckResult(
                Disposition.DRIFT,
                f"{WAIT_ONLINE_DROPIN} differs from the any-link wait configuration",
                _WAIT_ONLINE_FIX,
            )
        return CheckResult(
            Disposition.CONVERGED,
            f"{WAIT_ONLINE_DROPIN} has the any-link wait configuration",
            "",
        )

    def apply(self, context: ProvisionContext) -> None:
        context.host.mkdir(
            WAIT_ONLINE_DROPIN.parent,
            mode=0o755,
            parents=True,
            exist_ok=True,
        )
        context.host.write_text(WAIT_ONLINE_DROPIN, _WAIT_ONLINE_TEXT)
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
        if not context.host.exists(CHRONY_SOURCES):
            return CheckResult(Disposition.DRIFT, f"{CHRONY_SOURCES} is missing", _CHRONY_FIX)
        try:
            current = context.host.read_text(CHRONY_SOURCES)
        except (OSError, UnicodeError) as exc:
            return CheckResult(Disposition.UNFIXABLE, f"cannot read {CHRONY_SOURCES}: {exc}", _CHRONY_FIX)
        if current != expected:
            return CheckResult(Disposition.DRIFT, f"{CHRONY_SOURCES} differs from the site domain", _CHRONY_FIX)
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
        if context.host.exists(CHRONY_SOURCES):
            try:
                changed = context.host.read_text(CHRONY_SOURCES) != expected
            except (OSError, UnicodeError):
                changed = True
        context.host.mkdir(CHRONY_SOURCES.parent, mode=0o755, parents=True, exist_ok=True)
        context.host.write_text(CHRONY_SOURCES, expected)
        # Enable before reloading: chronyc can only talk to a running chronyd.
        context.host.run(["systemctl", "enable", "--now", "chrony"], check=True)
        if changed:
            context.host.run(["chronyc", "reload", "sources"], check=True)
