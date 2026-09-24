"""Provisioning step contracts and the ordered step registry."""

import re
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from gideon.host.images import LIBVIRT_BRIDGE_CIDR as LIBVIRT_BRIDGE_CIDR
from gideon.host.lock import HostLock
from gideon.host.secrets import SecretReadResult, read_secret
from gideon.host.site import SiteConfig
from gideon.host.sysio import Host

# The one fix text for every step blocked on the absent site file.
SITE_MISSING_FIX = (
    "Write /etc/gideon/site.yaml from config/site.example.yaml, then re-run provision."
)


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


def fetch_file(context: "ProvisionContext", url: str, dest: str) -> None:
    """Download *url* to *dest* via a .partial temporary and an atomic move.

    wget -O truncates its target immediately, so a failed download must never
    write to *dest* directly — a later run would see the stub and skip it.
    """

    partial = f"{dest}.partial"
    context.host.run(wget_argv(context, partial, url), check=True)
    context.host.run(["mv", "-f", partial, dest], check=True)


_SHA256_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+")


def checksum_matches(context: "ProvisionContext", path: str, expected: str) -> bool:
    """True when sha256sum of *path* equals *expected*."""

    result = context.host.run(["sha256sum", path])
    if result.returncode != 0:
        return False
    match = _SHA256_LINE.match(result.stdout.strip())
    return match is not None and match.group(1).lower() == expected.lower()


def apt_install(context: "ProvisionContext", packages: Sequence[str]) -> None:
    """Refresh the package lists, then install *packages*.

    A fresh cloud image ships empty apt lists (the acceptance VM found this:
    ``skopeo`` and ``age`` were "unable to locate" until Docker's step had
    updated), and a box provisioned weeks apart has stale ones; every install
    therefore updates first, a few seconds each.
    """

    context.host.run(["apt-get", "update"], check=True)
    context.host.run(["apt-get", "install", "-y", *packages], check=True)


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
    """Inputs shared by every provisioning step."""

    host: Host
    lock: HostLock
    site: SiteConfig | None


class Step:
    """Base class for one check/apply unit in host provisioning.

    ``gpu_host_only`` and ``build_box_only`` restrict where a step may run.
    """

    name: str = ""
    summary: str = ""
    needs_site: bool = False
    gpu_host_only: bool = False
    # The kvm, registry, and gh-runner roles run only on the declared build box.
    build_box_only: bool = False
    requires: tuple[str, ...] = ()

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
    from gideon.host.steps.tools import HostToolsStep

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
    )


STEPS.extend(_registered_steps())
