"""The run's shared state: what every stage module reads and the runner owns.

A leaf module, so ``image``, ``seed``, ``vm``, and ``run`` import it at module
level without a cycle.
"""

import ipaddress
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from gideon.host import backupset, images
from gideon.host.lock import HostLock
from gideon.host.report import StageResult
from gideon.host.site import SiteConfig
from gideon.host.sysio import Host
from tools.pinwatch.fetch import Fetcher

# Every disk, seed, key, and mirror of a run lives under its own directory
# here, on /data because the full restore writes about four copies of the
# box's set (the registry's images) into the VM's data disk and the root
# filesystem cannot hold them; the domain guard and the leftover sweep act on
# nothing outside it.
HARNESS_ROOT: Final = Path("/data/acceptance")
DEFAULT_VM_NAME: Final = "gideon-acceptance"
DEFAULT_RESTORE_VM_NAME: Final = "gideon-acceptance-restore"
DEFAULT_SITE_PATH: Final = Path("/etc/gideon/site.yaml")
# The first usable address on libvirt's default bridge is where both the
# provisioned registry and this run's SMTP sink are reachable from the VM.
ACCEPTANCE_REGISTRY_AUTHORITY: Final = (
    f"{next(iter(ipaddress.ip_network(images.LIBVIRT_BRIDGE_CIDR).hosts()))}:5000"
)


@dataclass(frozen=True, slots=True)
class RunSpec:
    """The immutable paths and operator choices for one acceptance run."""

    ref: str
    resolved_commit: str
    vm_name: str
    run_dir: Path
    out: Path
    keep: bool
    until: str | None
    # The tag the VM clones: the ref itself when it is a tag, else the
    # mirror-only acceptance-<sha12> tag the image stage makes.
    clone_ref: str = ""
    full_restore: bool = False


StageCallable = Callable[["HarnessContext"], StageResult]


class SinkMessageLike(Protocol):
    """The facts the runner needs from a stored SMTP message."""

    user: str
    tls: bool


class SinkLike(Protocol):
    """The small lifecycle and observation surface of an SMTP sink."""

    messages: Sequence[SinkMessageLike]
    port: int

    def start(self) -> None: ...

    def stop(self) -> None: ...


SinkFactory = Callable[..., SinkLike]


@dataclass(frozen=True, slots=True)
class ServiceMaterial:
    """Secrets and certificates prepared for the receiving VM."""

    site_text: str
    ca_bundle_text: str
    vm_certificate_text: str
    vm_key_text: str
    ldap_bind_password: str
    smtp_password: str
    smtp_user: str


@dataclass(frozen=True, slots=True)
class Stage:
    """One named stage in the acceptance sequence."""

    name: str
    callable: StageCallable
    fix: str
    key: str | None = None

    @property
    def identifier(self) -> str:
        """The CLI identifier; the second provision entry is ``provision-2``."""

        return self.key or self.name


@dataclass(slots=True)
class HarnessContext:
    """Mutable run state shared by the ordered stage callables."""

    host: Host
    fetcher: Fetcher
    checkout: Path
    site_path: Path
    template_path: Path
    spec: RunSpec
    lock: HostLock | None = None
    site: SiteConfig | None = None
    # The site rendered for the acceptance VM, distinct from the box's site.
    vm_site: SiteConfig | None = None
    run_id: str = ""
    address: str | None = None
    domain_defined: bool = False
    run_dir_created: bool = False
    # The 1-based position of the running stage in the table: the transcript
    # prefix, stamped by the runner before each call.
    stage_index: int = 0
    sleep: Callable[[float], None] = time.sleep
    # A monotonic clock for the wait loops' deadlines; tests advance a fake.
    clock: Callable[[], float] = time.monotonic
    sink_factory: SinkFactory | None = None
    sink: SinkLike | None = None
    services: ServiceMaterial | None = None
    ufw_rule: tuple[str, ...] | None = None
    install_url: str | None = None
    base_version: str | None = None
    rc_tag: str | None = None
    transcripts: list[Path] = field(default_factory=list)
    restore_set: backupset.SetRef | None = None
    snapshot_label: str | None = None
    box_recipient: str | None = None
    services_listing: str | None = None
    applied_record: Mapping[str, object] | None = None


def authenticated_messages(ctx: "HarnessContext") -> tuple[SinkMessageLike, ...]:
    """The sink's messages that arrived authenticated as the run's user over TLS.

    What preflight's SMTP check and Grafana's delivery must both produce
    (ticket 07's item (d), live for the first time).
    """

    material = ctx.services
    if material is None or ctx.sink is None:
        return ()
    return tuple(
        message
        for message in ctx.sink.messages
        if message.user == material.smtp_user and message.tls
    )


def checkout_git(checkout: Path, *arguments: str) -> list[str]:
    """A read-only git command over the box's checkout, run as root.

    The checkout belongs to a CSA account (the owner rule of the runbook), and
    git refuses a repository owned by someone else unless it is named safe;
    naming this one directory is narrower than running as its owner, and the
    harness never writes to it. The harness's own mirror under the run
    directory is root's and needs nothing.
    """

    return ["git", "-c", f"safe.directory={checkout}", *arguments]
