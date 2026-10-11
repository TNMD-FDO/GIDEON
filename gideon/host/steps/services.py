"""KVM, local registry, and GitHub Actions runner host services."""

import json
import os
import re
import stat
from dataclasses import dataclass
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
REGISTRY_UNIT = Path("/etc/systemd/system/gideon-registry.service")
_RUNNER_USER = "gh-runner"
_RUNNER_HOME = Path("/home/gh-runner")


@dataclass(frozen=True, slots=True)
class RunnerInstance:
    """One registered runner and the files owned by its installation."""

    name: str
    directory: Path
    labels: tuple[str, ...]

    def archive(self, version: str) -> Path:
        return self.directory / f"actions-runner-linux-x64-{version}.tar.gz"

    @property
    def manifest(self) -> Path:
        return self.directory / "bin/Runner.Listener.deps.json"

    @property
    def settings_file(self) -> Path:
        return self.directory / ".runner"

    @property
    def migrated_settings(self) -> Path:
        return self.directory / ".runner_migrated"

    @property
    def service_file(self) -> Path:
        return self.directory / ".service"


RUNNER_INSTANCES = (
    RunnerInstance("gideon", Path("/opt/gh-runner"), ("self-hosted", "linux", "x64", "gpu", "dl385-gen11")),
    RunnerInstance("gideon-checks", Path("/opt/gh-runner-checks"), ("self-hosted", "linux", "x64", "checks")),
)
_RUNNER_TOKEN = Path("/etc/gideon/secrets/gh_runner_token")
RUNNER_SUDOERS = Path("/etc/sudoers.d/gideon-acceptance")
_RUNNER_SUDOERS_CANDIDATE = Path("/etc/sudoers.d/gideon-acceptance.candidate")
_RUNNER_PACKAGE = "python3-venv"
# The runner matches the ACTIONS_RUNNER_INPUT_ prefix case-insensitively and
# looks the remainder up verbatim as the argument name; the lower-case suffix
# is therefore the exact form.
_RUNNER_TOKEN_VARIABLE = "ACTIONS_RUNNER_INPUT_token"
_RUNNER_URL = "https://github.com/TNMD-FDO"  # The product's GitHub organization.
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
_RUNNER_PACKAGE_FIX = "Run apt-get install -y python3-venv, then re-run provision."
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


def _runner_settings_fix(instance: RunnerInstance) -> str:
    return (
        f"Run {instance.directory / 'config.sh'} remove --local as gh-runner, place a fresh "
        "registration token, then re-run provision."
    )


def _runner_manifest_fix(instance: RunnerInstance) -> str:
    return (
        f"Inspect {instance.manifest} and run "
        f"{instance.directory / 'bin/Runner.Listener'} --version, then re-run provision."
    )


def _runner_dir_fix(instance: RunnerInstance) -> str:
    return f"Create {instance.directory} for gh-runner, then re-run provision."


def _runner_ownership_fix(instance: RunnerInstance) -> str:
    return f"Set {instance.directory} ownership to gh-runner, then re-run provision."


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
        if not context.host.exists(REGISTRY_UNIT):
            return CheckResult(Disposition.DRIFT, f"{REGISTRY_UNIT} is missing", _REGISTRY_FIX)
        try:
            current = context.host.read_text(REGISTRY_UNIT)
        except (OSError, UnicodeError) as exc:
            return CheckResult(Disposition.UNFIXABLE, f"cannot read {REGISTRY_UNIT}: {exc}", _REGISTRY_FIX)
        if current != expected:
            return CheckResult(Disposition.DRIFT, f"{REGISTRY_UNIT} differs from the pinned registry", _REGISTRY_FIX)
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
        if context.host.exists(REGISTRY_UNIT):
            try:
                changed = context.host.read_text(REGISTRY_UNIT) != expected
            except (OSError, UnicodeError):
                changed = True
        context.host.write_text(REGISTRY_UNIT, expected)
        if changed:
            context.host.run(["systemctl", "daemon-reload"], check=True)
            if context.host.exists(REGISTRY_UNIT):
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


def _runner_ready(context: ProvisionContext, instance: RunnerInstance, uid: int, gid: int) -> CheckResult | None:
    if not context.host.exists(instance.directory):
        return CheckResult(Disposition.DRIFT, f"{instance.directory} is missing", _runner_dir_fix(instance))
    try:
        details = context.host.stat(instance.directory)
    except OSError as exc:
        return CheckResult(Disposition.UNFIXABLE, f"cannot stat {instance.directory}: {exc}", _runner_dir_fix(instance))
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != uid or details.st_gid != gid:
        return CheckResult(Disposition.DRIFT, f"{instance.directory} is not owned by gh-runner", _runner_ownership_fix(instance))
    archive = instance.archive(context.lock.gh_runner.version)
    if not context.host.exists(archive):
        return CheckResult(Disposition.DRIFT, f"{archive} is missing", _RUNNER_DOWNLOAD_FIX)
    if not checksum_matches(context, str(archive), context.lock.gh_runner.sha256):
        return CheckResult(Disposition.DRIFT, f"{archive} has a checksum mismatch", _RUNNER_CHECKSUM_FIX)
    if not context.host.exists(instance.directory / "config.sh"):
        return CheckResult(Disposition.DRIFT, f"{instance.directory} runner release is not extracted", _RUNNER_EXTRACT_FIX)
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


def _runner_busy(context: ProvisionContext, instance: RunnerInstance) -> bool | CheckResult:
    """Whether this instance's worker process is running under gh-runner."""

    pattern = rf"^{re.escape(str(instance.directory / 'bin/Runner.Worker'))}( |$)"
    result = context.host.run(["pgrep", "-u", _RUNNER_USER, "-f", pattern])
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return CheckResult(
        Disposition.UNFIXABLE, "cannot tell whether a job is running", _RUNNER_PROCPS_FIX
    )


def _runner_installed_version(context: ProvisionContext, instance: RunnerInstance) -> str | CheckResult:
    """The installed listener's version, from the .NET dependency manifest it ships.

    The manifest names the assembly ``Runner.Listener/<version>`` under its
    targets; reading it is a file read, never an execution of the runner.
    """

    if not context.host.exists(instance.manifest):
        return CheckResult(
            Disposition.DRIFT, f"{instance.manifest} is missing", _RUNNER_EXTRACT_FIX
        )
    try:
        document = json.loads(context.host.read_text(instance.manifest))
    except (OSError, UnicodeError, ValueError) as exc:
        return CheckResult(
            Disposition.UNFIXABLE, f"cannot read {instance.manifest}: {exc}", _runner_manifest_fix(instance)
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
        f"{instance.manifest} names no Runner.Listener version",
        _runner_manifest_fix(instance),
    )


def _runner_settings(context: ProvisionContext, instance: RunnerInstance) -> list[dict[str, object]] | CheckResult:
    """Every runner settings file present, parsed (the runner writes a UTF-8 BOM)."""

    settings: list[dict[str, object]] = []
    for path in (instance.settings_file, instance.migrated_settings):
        if not context.host.exists(path):
            continue
        try:
            document = json.loads(context.host.read_text(path).removeprefix("\ufeff"))
        except (OSError, UnicodeError, ValueError) as exc:
            return CheckResult(
                Disposition.UNFIXABLE, f"{path} is not valid runner settings JSON: {exc}", _runner_settings_fix(instance)
            )
        if not isinstance(document, dict):
            return CheckResult(
                Disposition.UNFIXABLE, f"{path} is not a runner settings object", _runner_settings_fix(instance)
            )
        settings.append(document)
    return settings


def _runner_updates_disabled(context: ProvisionContext, instance: RunnerInstance) -> bool | CheckResult:
    """True iff every settings file present carries the flag --disableupdate writes.

    RunnerSettings.DisableUpdate is omitted when false, so an absent key means
    updates are on.
    """

    settings = _runner_settings(context, instance)
    if isinstance(settings, CheckResult):
        return settings
    return bool(settings) and all(document.get("disableUpdate") is True for document in settings)


def _runner_release_stale(context: ProvisionContext, instance: RunnerInstance) -> bool:
    """Whether the extracted release is missing or is not the pinned version."""

    if not context.host.exists(instance.directory / "config.sh"):
        return True
    version = _runner_installed_version(context, instance)
    if isinstance(version, CheckResult):
        if version.disposition is Disposition.UNFIXABLE:
            raise StepFailure(version.detail, version.fix)
        return True
    return version != context.lock.gh_runner.version


def _refuse_when_busy(context: ProvisionContext, instance: RunnerInstance) -> None:
    """Never stop a runner with a job in flight."""

    busy = _runner_busy(context, instance)
    if isinstance(busy, CheckResult):
        raise StepFailure(busy.detail, busy.fix)
    if busy:
        raise StepFailure(f"a job is running on {instance.name}", _RUNNER_WAIT_FIX)


def _refresh_service_wrapper(context: ProvisionContext, instance: RunnerInstance) -> None:
    """Copy a new release's runsvc.sh over the one the unit runs, as svc.sh install did."""

    try:
        current = context.host.read_text(instance.directory / "runsvc.sh")
        incoming = context.host.read_text(instance.directory / "bin/runsvc.sh")
    except OSError:
        current, incoming = "", "unreadable"
    if current != incoming:
        context.host.run(["cp", "./bin/runsvc.sh", "./runsvc.sh"], cwd=instance.directory, check=True)


def _remove_superseded_archives(context: ProvisionContext, instance: RunnerInstance, archive: Path) -> None:
    for name in sorted(context.host.listdir(instance.directory)):
        if (
            name.startswith("actions-runner-linux-x64-")
            and name.endswith(".tar.gz")
            and name != archive.name
        ):
            context.host.unlink(instance.directory / name)


def _register(context: ProvisionContext, instance: RunnerInstance, token: str) -> None:
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
            "--name",
            instance.name,
            "--labels",
            ",".join(instance.labels),
        ],
        cwd=instance.directory,
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


class GhRunnerStep(Step):
    """Converge two organization runner instances under one user and sudoers file.

    A job on one instance never holds the other's release or registration.
    Both share the gh-runner account and its sudoers rules for the acceptance
    harness, push smoke, and masked root stage.
    """

    name = "gh-runner"
    summary = "install and register the pinned GitHub Actions runner instances"
    build_box_only = True
    requires = ("docker-engine", "disk-layout")

    @staticmethod
    def _sudoers_text() -> str:
        return (
            f"{_RUNNER_USER} ALL=(root) NOPASSWD: "
            "/usr/bin/python3 -m tools.acceptance *\n"
            f"{_RUNNER_USER} ALL=(root) NOPASSWD: "
            "/usr/bin/python3 -B -m tools.cistack smoke\n"
            f"{_RUNNER_USER} ALL=(root) NOPASSWD: "
            "/usr/bin/unshare --mount --net -- sh -c *\n"
        )

    def _sudoers_check(self, context: ProvisionContext) -> CheckResult | None:
        if not context.host.exists(RUNNER_SUDOERS):
            return CheckResult(
                Disposition.DRIFT,
                f"{RUNNER_SUDOERS} is missing",
                _RUNNER_SUDOERS_FIX,
            )
        try:
            current = context.host.read_text(RUNNER_SUDOERS)
        except (OSError, UnicodeError) as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read {RUNNER_SUDOERS}: {exc}",
                _RUNNER_SUDOERS_FIX,
            )
        if current != self._sudoers_text():
            return CheckResult(
                Disposition.DRIFT,
                f"{RUNNER_SUDOERS} differs from the runner's rules",
                _RUNNER_SUDOERS_FIX,
            )
        try:
            details = context.host.stat(RUNNER_SUDOERS)
        except OSError as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot stat {RUNNER_SUDOERS}: {exc}",
                _RUNNER_SUDOERS_FIX,
            )
        mode = stat.S_IMODE(details.st_mode)
        if mode != 0o440:
            return CheckResult(
                Disposition.DRIFT,
                f"{RUNNER_SUDOERS} has mode {mode:04o}, expected 0440",
                _RUNNER_SUDOERS_FIX,
            )
        if details.st_uid != 0 or details.st_gid != 0:
            return CheckResult(
                Disposition.DRIFT,
                f"{RUNNER_SUDOERS} is owned by {details.st_uid}:{details.st_gid}, expected root:root",
                _RUNNER_SUDOERS_FIX,
            )
        return None

    def _check_instance(
        self,
        context: ProvisionContext,
        instance: RunnerInstance,
        uid: int,
        gid: int,
        token: str | None | CheckResult,
    ) -> CheckResult:
        ready = _runner_ready(context, instance, uid, gid)
        if ready is not None:
            return ready
        version = _runner_installed_version(context, instance)
        if isinstance(version, CheckResult):
            return version
        if version != context.lock.gh_runner.version:
            return CheckResult(
                Disposition.DRIFT,
                f"installed GitHub runner version {version} differs from "
                f"lock version {context.lock.gh_runner.version}",
                _RUNNER_VERSION_FIX,
            )
        if not context.host.exists(instance.settings_file):
            if isinstance(token, CheckResult):
                return token
            if token is None:
                return CheckResult(Disposition.PENDING_INPUT, "installed, unregistered", _RUNNER_FIX)
            return CheckResult(
                Disposition.DRIFT,
                "installed, unregistered; registration token is available",
                _RUNNER_REGISTER_FIX,
            )
        updates_disabled = _runner_updates_disabled(context, instance)
        if isinstance(updates_disabled, CheckResult):
            return updates_disabled
        if not updates_disabled:
            if isinstance(token, CheckResult):
                return token
            if token is None:
                return CheckResult(
                    Disposition.PENDING_INPUT, "registered with automatic updates on", _RUNNER_FIX
                )
            busy = _runner_busy(context, instance)
            if isinstance(busy, CheckResult):
                return busy
            if busy:
                return CheckResult(
                    Disposition.PENDING_INPUT,
                    f"registered with automatic updates on; a job is running on {instance.name}",
                    _RUNNER_WAIT_FIX,
                )
            return CheckResult(
                Disposition.DRIFT,
                "registered with automatic updates on; registration token is available",
                _RUNNER_REREGISTER_FIX,
            )

        if not context.host.exists(instance.service_file):
            return CheckResult(
                Disposition.DRIFT,
                "the GitHub runner service is not installed",
                _RUNNER_SERVICE_FIX,
            )
        try:
            unit = context.host.read_text(instance.service_file).strip()
        except (OSError, UnicodeError) as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read {instance.service_file}: {exc}",
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
        return CheckResult(
            Disposition.CONVERGED,
            "converged",
            "",
        )

    def check(self, context: ProvisionContext) -> CheckResult:
        account = passwd_entry(context, _RUNNER_USER)
        if account is None:
            return CheckResult(Disposition.DRIFT, "gh-runner does not exist", _RUNNER_CREATE_FIX)
        uid, gid, home = account.uid, account.gid, account.home
        if uid >= 1000 or not home:
            return CheckResult(Disposition.UNFIXABLE, "gh-runner is not a system user with a home", _RUNNER_USER_FIX)
        if not _runner_in_docker_group(context):
            return CheckResult(Disposition.DRIFT, "gh-runner is not in the docker group", _RUNNER_GROUP_FIX)
        if not package_installed(context, _RUNNER_PACKAGE):
            return CheckResult(Disposition.DRIFT, f"{_RUNNER_PACKAGE} is not installed", _RUNNER_PACKAGE_FIX)

        token = _runner_token(context)
        findings = [
            (instance.name, self._check_instance(context, instance, uid, gid, token))
            for instance in RUNNER_INSTANCES
        ]
        sudoers = self._sudoers_check(context)
        if sudoers is not None:
            findings.append(("sudoers", sudoers))
        if all(finding.disposition is Disposition.CONVERGED for _, finding in findings):
            names = " and ".join(instance.name for instance in RUNNER_INSTANCES)
            return CheckResult(
                Disposition.CONVERGED,
                f"runners {names} registered with updates disabled and active",
                "",
            )
        detail = "; ".join(f"{name}: {finding.detail}" for name, finding in findings)
        for disposition in (Disposition.DRIFT, Disposition.UNFIXABLE, Disposition.PENDING_INPUT):
            for _, finding in findings:
                if finding.disposition is disposition:
                    return CheckResult(disposition, detail, finding.fix)
        raise AssertionError("unexpected runner finding")

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
        if not package_installed(context, _RUNNER_PACKAGE):
            apt_install(context, [_RUNNER_PACKAGE])
        self._ensure_sudoers(context)
        token = _runner_token(context)
        failures: list[tuple[str, StepFailure]] = []
        registered_any = False
        registration_refused = False
        replaced_any = False
        for instance in RUNNER_INSTANCES:
            try:
                if account is not None and self._check_instance(
                    context, instance, account.uid, account.gid, token
                ).disposition is Disposition.CONVERGED:
                    continue
                replaced, registered = self._apply_instance(context, instance, token)
                replaced_any |= replaced
                registered_any |= registered
            except StepFailure as exc:
                failures.append((instance.name, exc))
                if exc.fix == _RUNNER_FRESH_TOKEN_FIX:
                    registration_refused = True
        if registered_any and not registration_refused:
            context.host.unlink(_RUNNER_TOKEN, missing_ok=True)
        if failures:
            detail = "; ".join(f"{name}: {failure.detail}" for name, failure in failures)
            raise StepFailure(detail, failures[0][1].fix)
        return _RUNNER_REREGISTERED_ANNOUNCEMENT if replaced_any else None

    def _apply_instance(
        self,
        context: ProvisionContext,
        instance: RunnerInstance,
        token: str | None | CheckResult,
    ) -> tuple[bool, bool]:
        context.host.mkdir(instance.directory, mode=0o755, parents=True, exist_ok=True)
        archive = instance.archive(context.lock.gh_runner.version)
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
                raise StepFailure(f"{archive} has a checksum mismatch after download", _RUNNER_CHECKSUM_FIX)

        service_installed = context.host.exists(instance.service_file)
        registered = context.host.exists(instance.settings_file)
        reregister = False
        if registered:
            updates_disabled = _runner_updates_disabled(context, instance)
            if isinstance(updates_disabled, CheckResult):
                raise StepFailure(updates_disabled.detail, updates_disabled.fix)
            reregister = not updates_disabled
        if (reregister or not registered) and isinstance(token, CheckResult):
            raise StepFailure(token.detail, token.fix)

        if archive_changed or _runner_release_stale(context, instance):
            if service_installed:
                _refuse_when_busy(context, instance)
                context.host.run(["./svc.sh", "stop"], cwd=instance.directory, check=True)
            context.host.run(["tar", "-xzf", str(archive)], cwd=instance.directory, check=True)
            if service_installed:
                _refresh_service_wrapper(context, instance)
            _remove_superseded_archives(context, instance, archive)
        context.host.run(["chown", "-R", f"{_RUNNER_USER}:{_RUNNER_USER}", str(instance.directory)], check=True)

        replaced = reregister and isinstance(token, str)
        if replaced:
            _refuse_when_busy(context, instance)
            if service_installed:
                context.host.run(["./svc.sh", "stop"], cwd=instance.directory, check=True)
                context.host.run(["./svc.sh", "uninstall"], cwd=instance.directory, check=True)
                service_installed = False
            context.host.run(
                ["runuser", "-u", _RUNNER_USER, "--", "./config.sh", "remove", "--local"],
                cwd=instance.directory,
                check=True,
            )
            registered = False
        registered_now = False
        if not registered and isinstance(token, str):
            _register(context, instance, token)
            registered = True
            registered_now = True
        if not registered:
            return False, False
        # svc.sh records its unit name in .service; a second install refuses.
        if not service_installed:
            context.host.run(["./svc.sh", "install", _RUNNER_USER], cwd=instance.directory, check=True)
        # svc.sh start never enables the unit; a unit someone disabled is enabled here.
        unit = context.host.read_text(instance.service_file).strip()
        context.host.run(["systemctl", "enable", unit], check=True)
        context.host.run(["./svc.sh", "start"], cwd=instance.directory, check=True)
        return replaced, registered_now

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
                str(RUNNER_SUDOERS),
            ],
            check=True,
        )
        context.host.chmod(RUNNER_SUDOERS, 0o440)
        context.host.chown(RUNNER_SUDOERS, 0, 0)
