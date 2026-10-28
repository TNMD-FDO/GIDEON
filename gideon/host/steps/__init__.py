"""Provisioning step contracts and the ordered step registry."""

import base64
import binascii
import hashlib
import re
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from gideon.host.images import LIBVIRT_BRIDGE_CIDR as LIBVIRT_BRIDGE_CIDR
from gideon.host.lock import HostLock
from gideon.host.models import ModelsLock
from gideon.host.secrets import SecretReadResult, read_secret
from gideon.host.site import SiteConfig
from gideon.host.sysio import Host

# The one fix text for every step blocked on the absent site file.
SITE_MISSING_FIX = (
    "Write /etc/gideon/site.yaml from config/site.example.yaml, then re-run provision."
)

# The operator's fix when a present shared package is below its required floor.
PREREQUISITE_FLOOR_FIX = (
    "Upgrade {package} to at least {floor} in an announced maintenance window "
    "(docs/runbooks/install-upgrade.md §9), then re-run provision."
)
# The operator's fix when apt would change a package beyond the absent set.
APT_REHEARSAL_FIX = (
    "Upgrade or remove the named packages yourself, in an announced maintenance "
    "window when containers are running (docs/runbooks/install-upgrade.md §9), "
    "then re-run provision."
)
BOX_WIDE_SHORTFALL_FIX = (
    "Agree the named setting with the box's other operators and change it yourself "
    "by docs/runbooks/install-upgrade.md §10, in an announced maintenance window "
    "where that section says so, then re-run provision."
)


@dataclass(frozen=True, slots=True)
class BoxWideSetting:
    """A host setting shared by every application on the box."""

    name: str


def box_wide_shortfall(
    setting: BoxWideSetting, found: str, needed: str, source: str | None = None
) -> str:
    """Describe a moved setting that falls short of the required value."""

    detail = f"{setting.name} is {found!r}, GIDEON needs {needed!r}"
    if source is not None:
        detail += f", set by {source}"
    return detail


def stderr_first_line(stderr: str) -> str:
    """The first line of a failed reader's diagnostic, for its refusal."""

    return stderr.splitlines()[0] if stderr.strip() else "no diagnostic"


def wget_argv_for_site(
    site: SiteConfig | None, output: str, url: str
) -> list[str]:
    """Build the shared wget argv for a site and destination."""

    argv = ["wget"]
    if site is not None and site.egress_proxy:
        proxy = site.egress_proxy
        argv += ["-e", f"https_proxy={proxy}", "-e", f"http_proxy={proxy}"]
    return argv + ["-qO", output, url]


def wget_argv(context: "ProvisionContext", output: str, url: str) -> list[str]:
    """A wget command that honours the site egress proxy when one is set.

    apt reads the egress-proxy step's drop-in; wget does not, so the proxy
    rides as ``-e`` options where the Host seam (and its fakes) can see it.
    """

    return wget_argv_for_site(context.site, output, url)


@dataclass(frozen=True, slots=True)
class Wgetrc:
    """The optional temporary wget credentials file and any read refusal."""

    path: Path | None = None
    problem: str | None = None
    fix: str = ""

    @property
    def ok(self) -> bool:
        return self.problem is None

    def prefix(self, argv: Sequence[str]) -> list[str]:
        """Prefix *argv* with the environment assignment, when configured."""

        if self.path is None:
            return list(argv)
        return ["env", f"WGETRC={self.path}", *argv]


_PROXY_AUTH_PATH = Path("/etc/gideon/secrets/proxy_auth")
_PROXY_AUTH_FIX = (
    "Correct the proxy credentials in /etc/gideon/secrets/proxy_auth, "
    "then re-run {command}."
)


def _wgetrc_text(auth: SecretReadResult) -> str | None:
    if auth.value is None:
        return None
    user, separator, password = auth.value.partition(":")
    if not separator or not user or not password:
        return None
    return f"proxy_user = {user}\nproxy_password = {password}\n"


@contextmanager
def temporary_wgetrc(host: Host, *, command: str, prefix: str = "gideon") -> Iterator[Wgetrc]:
    """Yield the proxy credentials as a temporary WGETRC file, removed on every exit path.

    ``command`` names the caller in the refusal's fix (``re-run preflight``).
    """

    if not host.exists(_PROXY_AUTH_PATH):
        yield Wgetrc()
        return

    auth = read_secret(host, "proxy_auth")
    if not auth.ok:
        yield Wgetrc(problem=auth.problem, fix=auth.fix)
        return
    contents = _wgetrc_text(auth)
    if contents is None:
        if auth.value is not None and ":" not in auth.value:
            yield Wgetrc(
                problem=f"Secret file {_PROXY_AUTH_PATH} must contain user:password.",
                fix=_PROXY_AUTH_FIX.format(command=command),
            )
            return
        yield Wgetrc(
            problem="proxy_auth must contain a non-empty user:password pair",
            fix=_PROXY_AUTH_FIX.format(command=command),
        )
        return

    path = Path(f"/run/{prefix}-{uuid.uuid4().hex}.wgetrc")
    host.write_text(path, contents, mode=0o600)
    try:
        yield Wgetrc(path=path)
    finally:
        host.unlink(path, missing_ok=True)


def fetch_file(
    context: "ProvisionContext", url: str, dest: str,
    *, verify: Callable[[str], None] | None = None,
) -> None:
    """Download *url* to *dest* via a .partial temporary and an atomic move.

    wget -O truncates its target immediately, so a failed download must never
    write to *dest* directly — a later run would see the stub and skip it.
    An optional verifier reads the partial before promotion; a refusal removes it.
    """

    partial = f"{dest}.partial"
    context.host.run(wget_argv(context, partial, url), check=True)
    if verify is not None:
        try:
            verify(partial)
        except Exception:
            context.host.unlink(partial, missing_ok=True)
            raise
    context.host.run(["mv", "-f", partial, dest], check=True)


_SHA256_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+")


def checksum_matches(context: "ProvisionContext", path: str, expected: str) -> bool:
    """True when sha256sum of *path* equals *expected*."""

    result = context.host.run(["sha256sum", path])
    if result.returncode != 0:
        return False
    match = _SHA256_LINE.match(result.stdout.strip())
    return match is not None and match.group(1).lower() == expected.lower()


def openpgp_fingerprint(text: str) -> str:
    """Read the primary v4 fingerprint from an armored OpenPGP public key."""

    lines = iter(text.splitlines())
    if not any(line.strip() == "-----BEGIN PGP PUBLIC KEY BLOCK-----" for line in lines):
        raise ValueError("no armored public key block")
    for line in lines:
        if not line.strip():
            break
        if ":" not in line:
            raise ValueError("invalid armor header")
    else:
        raise ValueError("missing armor body")
    encoded = []
    checksum = None
    for line in lines:
        line = line.strip()
        if line == "-----END PGP PUBLIC KEY BLOCK-----":
            break
        if line.startswith("="):
            checksum = line[1:]
            if next(lines, "").strip() != "-----END PGP PUBLIC KEY BLOCK-----":
                raise ValueError("missing armor end")
            break
        encoded.append(line)
    else:
        raise ValueError("missing armor end")
    # apt trusts every key the file holds, so a second block is never ignored.
    if any(line.strip() for line in lines):
        raise ValueError("text after the armor block")
    try:
        data = base64.b64decode("".join(encoded), validate=True)
        expected_crc = base64.b64decode(checksum, validate=True) if checksum is not None else None
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid armor base64") from exc
    if expected_crc is not None:
        crc = 0xB704CE
        for octet in data:
            crc ^= octet << 16
            for _ in range(8):
                crc <<= 1
                if crc & 0x1000000:
                    crc ^= 0x1864CFB
        if expected_crc != crc.to_bytes(3, "big"):
            raise ValueError("armor checksum mismatch")
    packets = list(_openpgp_packets(data))
    if not packets:
        raise ValueError("no openpgp packets")
    (tag, body), *rest = packets
    if tag != _PUBLIC_KEY_TAG:
        raise ValueError("first packet is not a public key")
    if not body:
        raise ValueError("empty public key packet")
    if body[0] != 4:
        raise ValueError(f"key version {body[0]} is not v4")
    if len(body) > 0xFFFF:
        raise ValueError("public key packet is too long")
    # Every packet is read: a key appended after the pinned one would be trusted too.
    for tag, _ in rest:
        if tag == _PUBLIC_KEY_TAG:
            raise ValueError("more than one primary key")
        if tag not in _KEY_COMPANION_TAGS:
            raise ValueError(f"unexpected packet tag {tag}")
    return hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body).hexdigest().upper()


_PUBLIC_KEY_TAG = 6
# Signature, trust, user ID, public subkey, and user attribute: what an exported
# public key carries beside its primary. A subkey is trusted only through the
# primary's binding signature, so it needs no pin of its own.
_KEY_COMPANION_TAGS = frozenset({2, 12, 13, 14, 17})


def _openpgp_packets(data: bytes) -> Iterator[tuple[int, bytes]]:
    """Split binary OpenPGP data into (tag, body) pairs, old and new headers alike."""

    offset = 0
    while offset < len(data):
        header = data[offset]
        if not header & 0x80:
            raise ValueError("invalid packet header")
        start = offset + 1
        if header & 0x40:
            tag = header & 0x3F
            if len(data) < start + 1:
                raise ValueError("truncated packet length")
            first = data[start]
            if first < 192:
                length, start = first, start + 1
            elif first < 224:
                if len(data) < start + 2:
                    raise ValueError("truncated packet length")
                length, start = ((first - 192) << 8) + data[start + 1] + 192, start + 2
            elif first == 255:
                if len(data) < start + 5:
                    raise ValueError("truncated packet length")
                length, start = int.from_bytes(data[start + 1:start + 5], "big"), start + 5
            else:
                raise ValueError("partial packet length")
        else:
            tag = (header >> 2) & 0x0F
            length_type = header & 0x03
            if length_type == 3:
                raise ValueError("indeterminate packet length")
            size = 1 << length_type
            if len(data) < start + size:
                raise ValueError("truncated packet length")
            length, start = int.from_bytes(data[start:start + size], "big"), start + size
        if start + length > len(data):
            raise ValueError("truncated packet body")
        yield tag, data[start:start + length]
        offset = start + length


_APT_UPGRADE = re.compile(r"^Inst\s+(\S+)\s+\[([^]]+)\]\s+\((\S+)")
_APT_REMOVAL = re.compile(r"^Remv\s+(\S+)")


def _rehearsal_changes(stdout: str) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Read package upgrades and removals from an apt install rehearsal."""

    upgrades = []
    removals = []
    for line in stdout.splitlines():
        upgrade = _APT_UPGRADE.match(line)
        if upgrade is not None:
            upgrades.append((upgrade.group(1), upgrade.group(2), upgrade.group(3)))
        removal = _APT_REMOVAL.match(line)
        if removal is not None:
            removals.append(removal.group(1))
    return upgrades, removals


def _rehearsal_refusal(
    absent: Sequence[str], upgrades: Sequence[tuple[str, str, str]], removals: Sequence[str]
) -> str | None:
    """Describe every package apt would upgrade, reinstall, or remove."""

    clauses = [f"upgrade {name} {old} → {new}" for name, old, new in upgrades]
    clauses.extend(f"remove {name}" for name in removals)
    if not clauses:
        return None
    return f"installing {', '.join(absent)} would {'; '.join(clauses)}"


def apt_install(context: "ProvisionContext", packages: Sequence[str]) -> None:
    """Install the absent packages among *packages*, and never move a present one.

    A present package is never named, since ``apt-get install`` moves an
    installed package to its candidate, and Docker's upgrade restarts every
    container on the box; the dpkg state word decides presence, so a held
    package counts. With nothing absent no apt command runs. Otherwise the
    lists are refreshed first — a fresh cloud image ships empty ones and a
    box provisioned weeks apart stale ones — and apt's dry run is read before
    the install: any upgrade or reinstall of a present package, named or
    pulled in by the resolver, or any removal, is a refusal naming each.
    """

    absent = [package for package in packages if package_version(context, package) is None]
    if not absent:
        return
    context.host.run(["apt-get", "update"], check=True)
    rehearsal = context.host.run(["apt-get", "-s", "install", "-y", *absent], check=True)
    upgrades, removals = _rehearsal_changes(rehearsal.stdout)
    refusal = _rehearsal_refusal(absent, upgrades, removals)
    if refusal is not None:
        raise StepFailure(refusal, APT_REHEARSAL_FIX)
    context.host.run(["apt-get", "install", "-y", *absent], check=True)


def site_required() -> "CheckResult":
    """The check result for a site-dependent step run without a site file."""

    return CheckResult(
        Disposition.PENDING_INPUT,
        "site file is required by this step",
        SITE_MISSING_FIX,
    )


def package_version(context: "ProvisionContext", package: str) -> str | None:
    """The installed version of *package*, or None when it is not installed.

    dpkg's Status is "<selection> <flag> <state>"; a held package reports
    "hold ok installed", so only the state word decides installed-ness —
    demanding the install selection would blind the probe to every held
    package, including the driver package provision intentionally holds.
    """

    result = context.host.run(
        ["dpkg-query", "-W", "-f=${Status} ${Version}\\n", package]
    )
    if result.returncode != 0:
        return None
    fields = result.stdout.split()
    if len(fields) < 4 or fields[2] != "installed":
        return None
    return fields[-1]


def package_installed(context: "ProvisionContext", package: str) -> bool:
    return package_version(context, package) is not None


@dataclass(frozen=True, slots=True)
class PasswdEntry:
    """One getent passwd record."""

    uid: int
    gid: int
    home: str
    shell: str


def passwd_entry(context: "ProvisionContext", account: str) -> "PasswdEntry | None":
    """The passwd record for *account*, or None when absent or malformed."""

    result = context.host.run(["getent", "passwd", account])
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        fields = line.split(":")
        if len(fields) >= 7 and fields[0] == account:
            try:
                return PasswdEntry(int(fields[2]), int(fields[3]), fields[5], fields[6])
            except ValueError:
                return None
    return None


class Disposition(Enum):
    """The result of checking one step's target state."""

    CONVERGED = "converged"
    DRIFT = "drift"
    UNFIXABLE = "unfixable"
    PENDING_INPUT = "pending-input"
    REBOOT_REQUIRED = "reboot-required"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """A check result with the detail and corrective action for an operator."""

    disposition: Disposition
    detail: str
    fix: str
    halts_run: bool = False


class StepFailure(Exception):
    """An apply refusal carrying the operator fix for its failed row."""

    detail: str
    fix: str

    def __init__(self, detail: str, fix: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.fix = fix


@dataclass(frozen=True, slots=True)
class ProvisionContext:
    """Inputs shared by every provisioning step.

    ``models`` supplies floors a step may judge. ``no_gpu`` is the run's
    effective mode, including a --no-gpu --dry-run before its marker exists.
    """

    host: Host
    lock: HostLock
    site: SiteConfig | None
    models: ModelsLock | None = None
    no_gpu: bool = False
    disruption_acknowledged: bool = False


class Step:
    """Base class for one check/apply unit in host provisioning.

    ``gpu_host_only`` and ``build_box_only`` restrict where a step may run.
    """

    name: str = ""
    summary: str = ""
    needs_site: bool = False
    gpu_host_only: bool = False
    # The kvm, registry, gh-runner, and archive-tools roles run only on the declared build box.
    build_box_only: bool = False
    requires: tuple[str, ...] = ()
    settings: tuple[BoxWideSetting, ...] = ()

    def check(self, context: ProvisionContext) -> CheckResult:
        raise NotImplementedError

    def apply(self, context: ProvisionContext) -> str | None:
        raise NotImplementedError


# Keeping the registry as instances lets the runner remain independent of
# construction details and makes the list directly usable by --list.
STEPS: list[Step] = []


def _registered_steps() -> tuple[Step, ...]:
    from gideon.host.steps.accounts import CsaAccountsStep, ServiceUserStep
    from gideon.host.steps.command import GideonCommandStep
    from gideon.host.steps.disk import DiskLayoutStep
    from gideon.host.steps.docker import DockerEngineStep
    from gideon.host.steps.maintenance import UnattendedUpgradesStep
    from gideon.host.steps.network import FirewallStep, TimeSyncStep, WaitOnlineStep
    from gideon.host.steps.nvidia import NvidiaDriverStep, NvidiaToolkitStep
    from gideon.host.steps.platform import PlatformStep
    from gideon.host.steps.proxy import EgressProxyStep
    from gideon.host.steps.services import GhRunnerStep, KvmStep, RegistryStep
    from gideon.host.steps.site_dirs import (
        AgeIdentityStep,
        AgeRecipientStep,
        BackupKeypairStep,
        SecretsDirsStep,
    )
    from gideon.host.steps.timezone import TimezoneStep
    from gideon.host.steps.tools import ArchiveToolsStep, HostToolsStep

    return (
        PlatformStep(),
        WaitOnlineStep(),
        EgressProxyStep(),
        HostToolsStep(),
        GideonCommandStep(),
        ServiceUserStep(),
        CsaAccountsStep(),
        DiskLayoutStep(),
        NvidiaDriverStep(),
        NvidiaToolkitStep(),
        DockerEngineStep(),
        FirewallStep(),
        TimeSyncStep(),
        TimezoneStep(),
        UnattendedUpgradesStep(),
        SecretsDirsStep(),
        BackupKeypairStep(),
        AgeRecipientStep(),
        AgeIdentityStep(),
        KvmStep(),
        RegistryStep(),
        GhRunnerStep(),
        ArchiveToolsStep(),
    )


STEPS.extend(_registered_steps())
