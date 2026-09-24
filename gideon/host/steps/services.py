"""KVM, local registry, and GitHub Actions runner host services."""

import json
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

from gideon.host.lock import HostLock
from gideon.host.report import command_detail
from gideon.host.steps import (
    LIBVIRT_BRIDGE_CIDR,
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    StepFailure,
    apt_install,
    checksum_matches,
    package_installed,
    passwd_entry,
    wget_argv,
)

_IMAGE_DIR = Path("/var/lib/libvirt/images")
_LIBVIRT_NETWORK = "default"
_BRIDGE_CIDR = LIBVIRT_BRIDGE_CIDR
_REGISTRY_TAG = "gideon-registry"
_REGISTRY_UNIT = Path("/etc/systemd/system/gideon-registry.service")
_RUNNER_USER = "gh-runner"
_RUNNER_HOME = Path("/home/gh-runner")
_RUNNER_DIR = Path("/opt/gh-runner")
_RUNNER_MANIFEST = _RUNNER_DIR / "bin/Runner.Listener.deps.json"
_RUNNER_SETTINGS_FILE = _RUNNER_DIR / ".runner"
# The runner's server-refreshed copy of its settings; the listener loads it first.
_RUNNER_MIGRATED_SETTINGS = _RUNNER_DIR / ".runner_migrated"
_RUNNER_SERVICE = _RUNNER_DIR / ".service"
_RUNNER_TOKEN = Path("/etc/gideon/secrets/gh_runner_token")
_RUNNER_SUDOERS = Path("/etc/sudoers.d/gideon-acceptance")
_RUNNER_SUDOERS_CANDIDATE = Path("/etc/sudoers.d/gideon-acceptance.candidate")
# The runner matches the ACTIONS_RUNNER_INPUT_ prefix case-insensitively and
# looks the remainder up verbatim as the argument name; the lower-case suffix
# is therefore the exact form.
_RUNNER_TOKEN_VARIABLE = "ACTIONS_RUNNER_INPUT_token"
_RUNNER_URL = "https://github.com/TNMD-FDO"  # The product's GitHub organization.
_RUNNER_LABELS = "self-hosted,linux,x64,gpu,dl385-gen11"  # The build box's runner labels.
_KVM_FIX = "Install and repair the libvirt KVM prerequisites, then re-run provision."
_IMAGE_FIX = "Download the lock-pinned acceptance VM image, then re-run provision."
_REGISTRY_FIX = "Repair the provision-owned registry service, then re-run provision."
_RUNNER_FIX = (
    "Get a fresh registration token from the organization's Settings → Actions → "
    "Runners page; it lives for one hour. Place it in "
    "/etc/gideon/secrets/gh_runner_token, then re-run provision."
)
_RUNNER_WAIT_FIX = "Wait for the running job to finish, then re-run provision."
_RUNNER_PROCPS_FIX = "Install procps so job state can be checked, then re-run provision."
_RUNNER_FRESH_TOKEN_FIX = (
    "Place a fresh registration token (they expire after one hour) in "
    "/etc/gideon/secrets/gh_runner_token, then re-run provision."
)
_RUNNER_SETTINGS_FIX = (
    "Run /opt/gh-runner/config.sh remove --local as gh-runner, place a fresh "
    "registration token, then re-run provision."
)
_RUNNER_MANIFEST_FIX = (
    "Inspect /opt/gh-runner/bin/Runner.Listener.deps.json and run "
    "/opt/gh-runner/bin/Runner.Listener --version, then re-run provision."
)
_RUNNER_EXTRACT_FIX = "Extract the lock-pinned GitHub runner, then re-run provision."
_RUNNER_VERSION_FIX = "Install the lock-pinned GitHub runner, then re-run provision."
_RUNNER_REGISTER_FIX = "Register the GitHub runner, then re-run provision."
_RUNNER_REREGISTER_FIX = (
    "Re-register the runner with updates disabled, then re-run provision."
)
_RUNNER_SERVICE_FIX = "Install and start the GitHub runner service, then re-run provision."
_RUNNER_SUDOERS_FIX = (
    "Repair /etc/sudoers.d/gideon-acceptance, then re-run provision."
)
_RUNNER_CREATE_FIX = "Create the gh-runner system user, then re-run provision."
_RUNNER_USER_FIX = (
    "Repair gh-runner as a system user with a home directory, then re-run provision."
)
_RUNNER_GROUP_FIX = "Add gh-runner to the docker group, then re-run provision."
_RUNNER_DIR_FIX = "Create /opt/gh-runner for gh-runner, then re-run provision."
_RUNNER_OWNERSHIP_FIX = (
    "Set /opt/gh-runner ownership to gh-runner, then re-run provision."
)
_RUNNER_DOWNLOAD_FIX = "Download the lock-pinned GitHub runner, then re-run provision."
_RUNNER_CHECKSUM_FIX = (
    "Re-download the lock-pinned GitHub runner, then re-run provision."
)
_RUNNER_REREGISTERED_ANNOUNCEMENT = (
    "The runner is registered with automatic updates disabled; from now on a new "
    "runner release arrives as the pin watch's host.gh_runner pull request — merge "
    "it, bring the checkout to the merged commit, and re-run host provision within "
    "30 days of the release."
)


# qemu-system-x86, not the qemu-kvm virtual package: dpkg-query only sees real
# packages, and a virtual name would never read as installed. The last three are
# the acceptance harness's tools (virt-customize/virt-resize, guestfish,
# cloud-localds), part of the build box's target state.
_KVM_PACKAGES = (
    "qemu-system-x86",
    "libvirt-daemon-system",
    "guestfs-tools",
    "libguestfs-tools",
    "cloud-image-utils",
)


def acceptance_image_path(lock: HostLock) -> Path:
    """Return the local path for the lock-pinned acceptance image."""

    name = Path(urlsplit(lock.acceptance_vm_image.url).path).name
    return _IMAGE_DIR / name


def _network_state(context: ProvisionContext) -> tuple[bool, bool] | None:
    result = context.host.run(["virsh", "net-info", _LIBVIRT_NETWORK])
    if result.returncode != 0:
        return None
    active = False
    autostart = False
    for line in result.stdout.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "Active":
            active = value.strip().lower() == "yes"
        elif key.strip() == "Autostart":
            autostart = value.strip().lower() == "yes"
    return active, autostart


class KvmStep(Step):
    """Converge libvirt's default network and the pinned VM image."""

    name = "kvm"
    summary = "install libvirt and verify the pinned acceptance VM image"
    build_box_only = True

    def check(self, context: ProvisionContext) -> CheckResult:
        for package in _KVM_PACKAGES:
            if not package_installed(context, package):
                return CheckResult(Disposition.DRIFT, f"{package} is not installed", _KVM_FIX)
        enabled = context.host.run(["systemctl", "is-enabled", "libvirtd"])
        active = context.host.run(["systemctl", "is-active", "libvirtd"])
        if enabled.returncode != 0 or active.returncode != 0:
            return CheckResult(Disposition.DRIFT, "libvirtd is not enabled and active", _KVM_FIX)
        network = _network_state(context)
        if network is None or network != (True, True):
            return CheckResult(Disposition.DRIFT, "libvirt default network is not active and autostarted", _KVM_FIX)
        image = acceptance_image_path(context.lock)
        if not context.host.exists(image):
            return CheckResult(Disposition.DRIFT, f"{image} is missing", _IMAGE_FIX)
        if not checksum_matches(context, str(image), context.lock.acceptance_vm_image.sha256):
            return CheckResult(Disposition.DRIFT, f"{image} has a checksum mismatch", _IMAGE_FIX)
        return CheckResult(Disposition.CONVERGED, "KVM and the acceptance VM image are current", "")

    def apply(self, context: ProvisionContext) -> None:
        missing = [
            package
            for package in _KVM_PACKAGES
            if not package_installed(context, package)
        ]
        if missing:
            apt_install(context, missing)
        context.host.run(["systemctl", "enable", "--now", "libvirtd"], check=True)
        network = _network_state(context)
        if network is None:
            # An undefined default network is re-created from libvirt's own
            # shipped definition, then inspected again.
            context.host.run(
                ["virsh", "net-define", "/usr/share/libvirt/networks/default.xml"],
                check=True,
            )
            network = _network_state(context)
        if network is None:
            raise RuntimeError("cannot inspect the libvirt default network")
        if not network[0]:
            context.host.run(["virsh", "net-start", _LIBVIRT_NETWORK], check=True)
        if not network[1]:
            context.host.run(["virsh", "net-autostart", _LIBVIRT_NETWORK], check=True)

        image = acceptance_image_path(context.lock)
        if not context.host.exists(image) or not checksum_matches(
            context, str(image), context.lock.acceptance_vm_image.sha256
        ):
            context.host.unlink(image, missing_ok=True)
            context.host.mkdir(_IMAGE_DIR, mode=0o755, parents=True, exist_ok=True)
            context.host.run(
                wget_argv(context, str(image), context.lock.acceptance_vm_image.url),
                check=True,
            )
            if not checksum_matches(context, str(image), context.lock.acceptance_vm_image.sha256):
                raise RuntimeError("acceptance VM image checksum mismatch after download")


def _registry_unit_text(image: str) -> str:
    return (
        "[Unit]\n"
        "Description=GIDEON local container registry\n"
        "After=docker.service\n"
        "Requires=docker.service\n\n"
        "[Service]\n"
        "Restart=always\n"
        # rm -f the leftover container first: a named docker run cannot start
        # over a previous instance, and Restart=always would crash-loop on it.
        "ExecStartPre=-/usr/bin/docker rm -f gideon-registry\n"
        f"ExecStart=/usr/bin/docker run --rm --name gideon-registry --publish 127.0.0.1:5000:5000 --publish 192.168.122.1:5000:5000 --volume /data/registry:/var/lib/registry {image}\n"
        "ExecStop=/usr/bin/docker stop --time 10 gideon-registry\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _registry_rule(context: ProvisionContext) -> bool:
    # ufw show added lists configured rules even while ufw is inactive
    # (status numbered prints nothing then); activation belongs to the
    # firewall step, this step only owns its rule's presence.
    result = context.host.run(["ufw", "show", "added"])
    if result.returncode != 0:
        return False
    return any(
        "port 5000" in line and _BRIDGE_CIDR in line and _REGISTRY_TAG in line
        for line in result.stdout.splitlines()
    )


class RegistryStep(Step):
    """Run the pinned registry on localhost and libvirt's bridge address."""

    name = "registry"
    summary = "run the pinned local registry on the libvirt bridge"
    build_box_only = True
    requires = ("docker-engine", "disk-layout", "kvm")

    def check(self, context: ProvisionContext) -> CheckResult:
        expected = _registry_unit_text(context.lock.registry_image)
        if not context.host.exists(_REGISTRY_UNIT):
            return CheckResult(Disposition.DRIFT, f"{_REGISTRY_UNIT} is missing", _REGISTRY_FIX)
        try:
            current = context.host.read_text(_REGISTRY_UNIT)
        except (OSError, UnicodeError) as exc:
            return CheckResult(Disposition.UNFIXABLE, f"cannot read {_REGISTRY_UNIT}: {exc}", _REGISTRY_FIX)
        if current != expected:
            return CheckResult(Disposition.DRIFT, f"{_REGISTRY_UNIT} differs from the pinned registry", _REGISTRY_FIX)
        enabled = context.host.run(["systemctl", "is-enabled", "gideon-registry"])
        active = context.host.run(["systemctl", "is-active", "gideon-registry"])
        if enabled.returncode != 0 or active.returncode != 0:
            return CheckResult(Disposition.DRIFT, "gideon-registry is not enabled and active", _REGISTRY_FIX)
        if not _registry_rule(context):
            return CheckResult(Disposition.DRIFT, "the gideon-registry bridge firewall rule is missing", _REGISTRY_FIX)
        return CheckResult(Disposition.CONVERGED, "gideon-registry is current and active", "")

    def apply(self, context: ProvisionContext) -> None:
        image = context.lock.registry_image
        context.host.run(["docker", "pull", image], check=True)
        if not _registry_rule(context):
            context.host.run(
                [
                    "ufw",
                    "allow",
                    "proto",
                    "tcp",
                    "from",
                    _BRIDGE_CIDR,
                    "to",
                    "any",
                    "port",
                    "5000",
                    "comment",
                    _REGISTRY_TAG,
                ],
                check=True,
            )
        expected = _registry_unit_text(image)
        changed = True
        if context.host.exists(_REGISTRY_UNIT):
            try:
                changed = context.host.read_text(_REGISTRY_UNIT) != expected
            except (OSError, UnicodeError):
                changed = True
        context.host.write_text(_REGISTRY_UNIT, expected)
        if changed:
            context.host.run(["systemctl", "daemon-reload"], check=True)
            if context.host.exists(_REGISTRY_UNIT):
                context.host.run(["systemctl", "restart", "gideon-registry"], check=True)
        context.host.run(["systemctl", "enable", "--now", "gideon-registry"], check=True)


def _runner_in_docker_group(context: ProvisionContext) -> bool:
    result = context.host.run(["getent", "group", "docker"])
    if result.returncode != 0:
        return False
    return any(
        len(fields) >= 4
        and fields[0] == "docker"
        and _RUNNER_USER in fields[3].split(",")
        for fields in (line.split(":") for line in result.stdout.splitlines())
    )


def _runner_archive(context: ProvisionContext) -> Path:
    return _RUNNER_DIR / f"actions-runner-linux-x64-{context.lock.gh_runner.version}.tar.gz"


def _runner_ready(context: ProvisionContext, uid: int, gid: int) -> CheckResult | None:
    if not context.host.exists(_RUNNER_DIR):
        return CheckResult(Disposition.DRIFT, f"{_RUNNER_DIR} is missing", _RUNNER_DIR_FIX)
    try:
        details = context.host.stat(_RUNNER_DIR)
    except OSError as exc:
        return CheckResult(Disposition.UNFIXABLE, f"cannot stat {_RUNNER_DIR}: {exc}", _RUNNER_FIX)
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != uid or details.st_gid != gid:
        return CheckResult(Disposition.DRIFT, f"{_RUNNER_DIR} is not owned by gh-runner", _RUNNER_OWNERSHIP_FIX)
    archive = _runner_archive(context)
    if not context.host.exists(archive):
        return CheckResult(Disposition.DRIFT, f"{archive} is missing", _RUNNER_DOWNLOAD_FIX)
    if not checksum_matches(context, str(archive), context.lock.gh_runner.sha256):
        return CheckResult(Disposition.DRIFT, f"{archive} has a checksum mismatch", _RUNNER_CHECKSUM_FIX)
    if not context.host.exists(_RUNNER_DIR / "config.sh"):
        return CheckResult(Disposition.DRIFT, "the GitHub runner release is not extracted", _RUNNER_EXTRACT_FIX)
    return None


def _runner_token(context: ProvisionContext) -> str | None | CheckResult:
    """The registration token on disk: None when absent or empty."""

    if not context.host.exists(_RUNNER_TOKEN):
        return None
    try:
        token = context.host.read_text(_RUNNER_TOKEN).strip()
    except (OSError, UnicodeError) as exc:
        return CheckResult(Disposition.UNFIXABLE, f"cannot read {_RUNNER_TOKEN}: {exc}", _RUNNER_FIX)
    return token or None


def _runner_busy(context: ProvisionContext) -> bool | CheckResult:
    """Whether a job is running: the worker process exists under gh-runner."""

    result = context.host.run(["pgrep", "-u", _RUNNER_USER, "-x", "Runner.Worker"])
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return CheckResult(
        Disposition.UNFIXABLE, "cannot tell whether a job is running", _RUNNER_PROCPS_FIX
    )


def _runner_installed_version(context: ProvisionContext) -> str | CheckResult:
    """The installed listener's version, from the .NET dependency manifest it ships.

    The manifest names the assembly ``Runner.Listener/<version>`` under its
    targets; reading it is a file read, never an execution of the runner.
    """

    if not context.host.exists(_RUNNER_MANIFEST):
        return CheckResult(
            Disposition.DRIFT, "the installed runner's manifest is missing", _RUNNER_EXTRACT_FIX
        )
    try:
        document = json.loads(context.host.read_text(_RUNNER_MANIFEST))
    except (OSError, UnicodeError, ValueError) as exc:
        return CheckResult(
            Disposition.UNFIXABLE, f"cannot read {_RUNNER_MANIFEST}: {exc}", _RUNNER_MANIFEST_FIX
        )
    targets = document.get("targets") if isinstance(document, dict) else None
    for target in (targets.values() if isinstance(targets, dict) else ()):
        if not isinstance(target, dict):
            continue
        for name in target:
            if isinstance(name, str) and name.startswith("Runner.Listener/"):
                version = name.removeprefix("Runner.Listener/")
                if version:
                    return version
    return CheckResult(
        Disposition.UNFIXABLE,
        f"{_RUNNER_MANIFEST} names no Runner.Listener version",
        _RUNNER_MANIFEST_FIX,
    )


def _runner_settings(context: ProvisionContext) -> list[dict[str, object]] | CheckResult:
    """Every runner settings file present, parsed (the runner writes a UTF-8 BOM)."""

    settings: list[dict[str, object]] = []
    for path in (_RUNNER_SETTINGS_FILE, _RUNNER_MIGRATED_SETTINGS):
        if not context.host.exists(path):
            continue
        try:
            document = json.loads(context.host.read_text(path).removeprefix("\ufeff"))
        except (OSError, UnicodeError, ValueError) as exc:
            return CheckResult(
                Disposition.UNFIXABLE, f"{path} is not valid runner settings JSON: {exc}", _RUNNER_SETTINGS_FIX
            )
        if not isinstance(document, dict):
            return CheckResult(
                Disposition.UNFIXABLE, f"{path} is not a runner settings object", _RUNNER_SETTINGS_FIX
            )
        settings.append(document)
    return settings


def _runner_updates_disabled(settings: list[dict[str, object]]) -> bool:
    """True iff every settings file present carries the flag --disableupdate writes.

    RunnerSettings.DisableUpdate is omitted when false, so an absent key means
    updates are on.
    """

    return bool(settings) and all(document.get("disableUpdate") is True for document in settings)


def _runner_release_stale(context: ProvisionContext) -> bool:
    """Whether the extracted release is missing or is not the pinned version."""

    if not context.host.exists(_RUNNER_DIR / "config.sh"):
        return True
    version = _runner_installed_version(context)
    if isinstance(version, CheckResult):
        if version.disposition is Disposition.UNFIXABLE:
            raise StepFailure(version.detail, version.fix)
        return True
    return version != context.lock.gh_runner.version


def _refuse_when_busy(context: ProvisionContext) -> None:
    """Never stop a runner with a job in flight."""

    busy = _runner_busy(context)
    if isinstance(busy, CheckResult):
        raise StepFailure(busy.detail, busy.fix)
    if busy:
        raise StepFailure("a job is running on the runner", _RUNNER_WAIT_FIX)


def _refresh_service_wrapper(context: ProvisionContext) -> None:
    """Copy a new release's runsvc.sh over the one the unit runs, as svc.sh install did."""

    try:
        current = context.host.read_text(_RUNNER_DIR / "runsvc.sh")
        incoming = context.host.read_text(_RUNNER_DIR / "bin/runsvc.sh")
    except OSError:
        current, incoming = "", "unreadable"
    if current != incoming:
        context.host.run(["cp", "./bin/runsvc.sh", "./runsvc.sh"], cwd=_RUNNER_DIR, check=True)


def _remove_superseded_archives(context: ProvisionContext, archive: Path) -> None:
    for name in sorted(context.host.listdir(_RUNNER_DIR)):
        if (
            name.startswith("actions-runner-linux-x64-")
            and name.endswith(".tar.gz")
            and name != archive.name
        ):
            context.host.unlink(_RUNNER_DIR / name)


def _register(context: ProvisionContext, token: str) -> None:
    """Register the runner, taking over the organization's record of the same name.

    The token rides in the child's environment, never argv; the environment is
    the inherited one plus that variable, since Host.run passes it whole.
    """

    config = context.host.run(
        [
            "runuser",
            "-u",
            _RUNNER_USER,
            "--",
            "./config.sh",
            "--unattended",
            "--replace",
            "--disableupdate",
            "--url",
            _RUNNER_URL,
            "--labels",
            _RUNNER_LABELS,
        ],
        cwd=_RUNNER_DIR,
        env={**os.environ, _RUNNER_TOKEN_VARIABLE: token},
    )
    if config.returncode != 0:
        lines = [line.strip() for line in config.stderr.splitlines() if line.strip()] or [
            line.strip() for line in config.stdout.splitlines() if line.strip()
        ]
        diagnostic = lines[-1] if lines else f"exit code {config.returncode}"
        raise StepFailure(
            f"config.sh refused the registration: {diagnostic}", _RUNNER_FRESH_TOKEN_FIX
        )
    # A registration token is single-purpose and lives an hour: consumed.
    context.host.unlink(_RUNNER_TOKEN, missing_ok=True)


class GhRunnerStep(Step):
    """Install and optionally register the organization GitHub runner.

    The runner is already root-equivalent through the docker group and never
    runs pull-request code. Its sudoers line names one module of the checkout
    it runs from; hardening waits for the public repository flip.
    """

    name = "gh-runner"
    summary = "install and register the pinned GitHub Actions runner"
    build_box_only = True
    requires = ("docker-engine", "disk-layout")

    @staticmethod
    def _sudoers_text() -> str:
        return (
            f"{_RUNNER_USER} ALL=(root) NOPASSWD: "
            "/usr/bin/python3 -m tools.acceptance *\n"
        )

    def _sudoers_check(self, context: ProvisionContext) -> CheckResult | None:
        if not context.host.exists(_RUNNER_SUDOERS):
            return CheckResult(
                Disposition.DRIFT,
                f"{_RUNNER_SUDOERS} is missing",
                _RUNNER_SUDOERS_FIX,
            )
        try:
            current = context.host.read_text(_RUNNER_SUDOERS)
        except (OSError, UnicodeError) as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read {_RUNNER_SUDOERS}: {exc}",
                _RUNNER_SUDOERS_FIX,
            )
        if current != self._sudoers_text():
            return CheckResult(
                Disposition.DRIFT,
                f"{_RUNNER_SUDOERS} differs from the acceptance rule",
                _RUNNER_SUDOERS_FIX,
            )
        try:
            details = context.host.stat(_RUNNER_SUDOERS)
        except OSError as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot stat {_RUNNER_SUDOERS}: {exc}",
                _RUNNER_SUDOERS_FIX,
            )
        mode = stat.S_IMODE(details.st_mode)
        if mode != 0o440:
            return CheckResult(
                Disposition.DRIFT,
                f"{_RUNNER_SUDOERS} has mode {mode:04o}, expected 0440",
                _RUNNER_SUDOERS_FIX,
            )
        if details.st_uid != 0 or details.st_gid != 0:
            return CheckResult(
                Disposition.DRIFT,
                f"{_RUNNER_SUDOERS} is owned by {details.st_uid}:{details.st_gid}, expected root:root",
                _RUNNER_SUDOERS_FIX,
            )
        return None

    def check(self, context: ProvisionContext) -> CheckResult:
        account = passwd_entry(context, _RUNNER_USER)
        if account is None:
            return CheckResult(Disposition.DRIFT, "gh-runner does not exist", _RUNNER_CREATE_FIX)
        uid, gid, home = account.uid, account.gid, account.home
        if uid >= 1000 or not home:
            return CheckResult(Disposition.UNFIXABLE, "gh-runner is not a system user with a home", _RUNNER_USER_FIX)
        if not _runner_in_docker_group(context):
            return CheckResult(Disposition.DRIFT, "gh-runner is not in the docker group", _RUNNER_GROUP_FIX)
        ready = _runner_ready(context, uid, gid)
        if ready is not None:
            return ready
        version = _runner_installed_version(context)
        if isinstance(version, CheckResult):
            return version
        if version != context.lock.gh_runner.version:
            return CheckResult(
                Disposition.DRIFT,
                f"installed GitHub runner version {version} differs from "
                f"lock version {context.lock.gh_runner.version}",
                _RUNNER_VERSION_FIX,
            )
        if not context.host.exists(_RUNNER_SETTINGS_FILE):
            token = _runner_token(context)
            if isinstance(token, CheckResult):
                return token
            if token is None:
                return CheckResult(Disposition.PENDING_INPUT, "installed, unregistered", _RUNNER_FIX)
            return CheckResult(
                Disposition.DRIFT,
                "installed, unregistered; registration token is available",
                _RUNNER_REGISTER_FIX,
            )
        settings = _runner_settings(context)
        if isinstance(settings, CheckResult):
            return settings
        if not _runner_updates_disabled(settings):
            token = _runner_token(context)
            if isinstance(token, CheckResult):
                return token
            if token is None:
                return CheckResult(
                    Disposition.PENDING_INPUT, "registered with automatic updates on", _RUNNER_FIX
                )
            busy = _runner_busy(context)
            if isinstance(busy, CheckResult):
                return busy
            if busy:
                return CheckResult(
                    Disposition.PENDING_INPUT,
                    "registered with automatic updates on; a job is running",
                    _RUNNER_WAIT_FIX,
                )
            return CheckResult(
                Disposition.DRIFT,
                "registered with automatic updates on; registration token is available",
                _RUNNER_REREGISTER_FIX,
            )

        if not context.host.exists(_RUNNER_SERVICE):
            return CheckResult(
                Disposition.DRIFT,
                "the GitHub runner service is not installed",
                _RUNNER_SERVICE_FIX,
            )
        try:
            unit = context.host.read_text(_RUNNER_SERVICE).strip()
        except (OSError, UnicodeError) as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read {_RUNNER_SERVICE}: {exc}",
                _RUNNER_SERVICE_FIX,
            )
        enabled = context.host.run(["systemctl", "is-enabled", unit])
        active = context.host.run(["systemctl", "is-active", unit])
        if enabled.returncode != 0 or active.returncode != 0:
            return CheckResult(
                Disposition.DRIFT,
                "the GitHub runner service is not enabled and active",
                _RUNNER_SERVICE_FIX,
            )
        sudoers = self._sudoers_check(context)
        if sudoers is not None:
            return sudoers
        return CheckResult(
            Disposition.CONVERGED,
            "GitHub runner is registered with updates disabled and active",
            "",
        )

    def apply(self, context: ProvisionContext) -> str | None:
        account = passwd_entry(context, _RUNNER_USER)
        if account is None:
            context.host.run(
                [
                    "useradd",
                    "--system",
                    "--create-home",
                    "--home-dir",
                    str(_RUNNER_HOME),
                    "--shell",
                    "/usr/sbin/nologin",
                    _RUNNER_USER,
                ],
                check=True,
            )
        if not _runner_in_docker_group(context):
            context.host.run(["usermod", "-aG", "docker", _RUNNER_USER], check=True)
        context.host.mkdir(_RUNNER_DIR, mode=0o755, parents=True, exist_ok=True)
        archive = _runner_archive(context)
        archive_changed = False
        if not context.host.exists(archive) or not checksum_matches(
            context, str(archive), context.lock.gh_runner.sha256
        ):
            archive_changed = True
            context.host.unlink(archive, missing_ok=True)
            url = (
                "https://github.com/actions/runner/releases/download/"
                f"v{context.lock.gh_runner.version}/{archive.name}"
            )
            context.host.run(wget_argv(context, str(archive), url), check=True)
            if not checksum_matches(context, str(archive), context.lock.gh_runner.sha256):
                raise RuntimeError("GitHub runner checksum mismatch after download")

        service_installed = context.host.exists(_RUNNER_SERVICE)
        registered = context.host.exists(_RUNNER_SETTINGS_FILE)
        reregister = False
        if registered:
            settings = _runner_settings(context)
            if isinstance(settings, CheckResult):
                raise StepFailure(settings.detail, settings.fix)
            reregister = not _runner_updates_disabled(settings)
        # The token is an optional manual input, resolved before anything stops:
        # without one the release still moves and the service still restarts, and
        # the re-check reports pending-input, never an offline runner.
        token: str | None = None
        if reregister or not registered:
            token_on_disk = _runner_token(context)
            if isinstance(token_on_disk, CheckResult):
                raise StepFailure(token_on_disk.detail, token_on_disk.fix)
            token = token_on_disk

        if archive_changed or _runner_release_stale(context):
            if service_installed:
                _refuse_when_busy(context)
                context.host.run(["./svc.sh", "stop"], cwd=_RUNNER_DIR, check=True)
            context.host.run(["tar", "-xzf", str(archive)], cwd=_RUNNER_DIR, check=True)
            if service_installed:
                _refresh_service_wrapper(context)
            _remove_superseded_archives(context, archive)
        context.host.run(["chown", "-R", f"{_RUNNER_USER}:{_RUNNER_USER}", str(_RUNNER_DIR)], check=True)
        # The sudoers line is host state independent of registration: an
        # installed, unregistered runner still gets it.
        self._ensure_sudoers(context)

        replaced = reregister and token is not None
        if replaced:
            _refuse_when_busy(context)
            if service_installed:
                context.host.run(["./svc.sh", "stop"], cwd=_RUNNER_DIR, check=True)
                context.host.run(["./svc.sh", "uninstall"], cwd=_RUNNER_DIR, check=True)
                service_installed = False
            context.host.run(
                ["runuser", "-u", _RUNNER_USER, "--", "./config.sh", "remove", "--local"],
                cwd=_RUNNER_DIR,
                check=True,
            )
            registered = False
        if not registered and token is not None:
            _register(context, token)
            registered = True
        if not registered:
            # Installed, unregistered, no token: nothing to run yet.
            return None
        # svc.sh records its unit name in .service; a second install refuses.
        if not service_installed:
            context.host.run(["./svc.sh", "install", _RUNNER_USER], cwd=_RUNNER_DIR, check=True)
        # svc.sh start never enables the unit; a unit someone disabled is enabled here.
        unit = context.host.read_text(_RUNNER_SERVICE).strip()
        context.host.run(["systemctl", "enable", unit], check=True)
        context.host.run(["./svc.sh", "start"], cwd=_RUNNER_DIR, check=True)
        return _RUNNER_REREGISTERED_ANNOUNCEMENT if replaced else None

    def _ensure_sudoers(self, context: ProvisionContext) -> None:
        """Validate the acceptance runner's sudoers rule as a candidate, then promote it."""

        if self._sudoers_check(context) is None:
            return
        expected = self._sudoers_text()
        context.host.write_text(_RUNNER_SUDOERS_CANDIDATE, expected, mode=0o440)
        candidate = context.host.run(
            ["visudo", "-c", "-f", str(_RUNNER_SUDOERS_CANDIDATE)]
        )
        if candidate.returncode != 0:
            context.host.unlink(_RUNNER_SUDOERS_CANDIDATE, missing_ok=True)
            diagnostic = command_detail(candidate)
            raise StepFailure(
                f"{_RUNNER_SUDOERS_CANDIDATE} was rejected by visudo: {diagnostic}",
                _RUNNER_SUDOERS_FIX,
            )
        context.host.run(
            [
                "mv",
                "-f",
                str(_RUNNER_SUDOERS_CANDIDATE),
                str(_RUNNER_SUDOERS),
            ],
            check=True,
        )
        context.host.chmod(_RUNNER_SUDOERS, 0o440)
        context.host.chown(_RUNNER_SUDOERS, 0, 0)
