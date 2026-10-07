"""Host-step truth tables, including the post-reinstall baseline.

Beyond the leaf's entry: `gideon-command`'s text and mode, containerd
restarted before Docker when its file changed, `age-identity` repairing
custody in place, and the Docker, Compose, toolkit, and driver refusals
each read before any write.
"""

import contextlib
import io
import json
import os
import pwd
import re
import stat
import subprocess
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from gideon.host import dockerdaemon
from gideon.host.lock import load_host_lock
from gideon.host.provision import run_provision
from gideon.host.render import worker
from gideon.host.render.opensearch import (
    OPENSEARCH_DATA_ROOT,
    OPENSEARCH_GID,
    OPENSEARCH_UID,
)
from gideon.host.render.qdrant import QDRANT_DATA_ROOT
from gideon.host.site import SiteConfig, load_site
from gideon.host.steps import (
    SITE_MISSING_FIX,
    STEPS,
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    StepFailure,
    apt_install,
    package_version,
)
from gideon.host.steps.accounts import CsaAccountsStep, ServiceUserStep
from gideon.host.steps.command import (
    COMMAND_ANNOUNCEMENT,
    COMMAND_FIX,
    COMMAND_MODE,
    COMMAND_PATH,
    INSTALL_HOME,
    GideonCommandStep,
    command_text,
)
from gideon.host.steps.disk import DiskLayoutStep
from gideon.host.steps.docker import (
    _ARCHITECTURE,
    _COMPONENT,
    _CONTAINERD_CONFIG,
    _CONTAINERD_DEFAULT_ROOT,
    _CONTAINERD_DUMP,
    _CONTAINERD_ROOT,
    _CONTAINERD_STORE_PATHS,
    _CONTAINERD_TEXT,
    _CONTAINERD_VERIFY,
    _DAEMON,
    _JOURNALD,
    _JOURNALD_CAT,
    _JOURNALD_TEXT,
    _KEY_URL,
    _KEYRING,
    _PACKAGES,
    _REPO,
    _REPOSITORY,
    _SOURCE,
    _SUITE,
    DockerEngineStep,
)
from gideon.host.steps.maintenance import _DROP_IN, _PERIODIC, UnattendedUpgradesStep
from gideon.host.steps.network import (
    _DOCKER_USER_UNIT,
    _DOCKER_USER_UNIT_TEXT,
    _JUMP,
    _WAIT_ONLINE_DROPIN,
    _WAIT_ONLINE_FIX,
    _WAIT_ONLINE_TEXT,
    FirewallStep,
    TimeSyncStep,
    WaitOnlineStep,
    _docker_user_block,
    _docker_user_rules,
)
from gideon.host.steps.nvidia import (
    _DEB_TMP,
    _DRIVER_PACKAGES,
    _KERNEL_RELEASE,
    _KEYRING_FILE,
    _LEGACY_KEYRING,
    _LEGACY_SOURCE,
    _LOADED_TAINT,
    _LOADED_VERSION,
    _MODINFO_LICENSE,
    _MODINFO_VERSION,
    _TOOLKIT_POLICY,
    NvidiaDriverStep,
    NvidiaToolkitStep,
)
from gideon.host.steps.platform import PlatformStep
from gideon.host.steps.proxy import EgressProxyStep
from gideon.host.steps.services import (
    _RUNNER_FRESH_TOKEN_FIX,
    _RUNNER_REREGISTERED_ANNOUNCEMENT,
    _RUNNER_TOKEN_VARIABLE,
    _RUNNER_WAIT_FIX,
    GhRunnerStep,
    KvmStep,
    RegistryStep,
    acceptance_image_path,
)
from gideon.host.steps.site_dirs import (
    AGE_IDENTITY_FIX,
    AGE_IDENTITY_LINE,
    AGE_IDENTITY_MODE,
    AGE_IDENTITY_PATH,
    AGE_RECIPIENT_PATH,
    AgeIdentityStep,
    AgeRecipientStep,
    BackupKeypairStep,
    SecretsDirsStep,
)
from gideon.host.steps.timezone import TimezoneStep
from gideon.host.steps.tools import HostToolsStep
from gideon.host.sysio import Command, PathLike
from tools.exportboundary import absent_from_export
from tools.mask import Facts, wrap

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "host.lock"
EXAMPLE = ROOT / "config/site.example.yaml"
BASELINE = ROOT / "tests/fixtures/host/baseline-post-reinstall"


class FakeHost:
    """An in-memory Host with command and file operations in one state model."""

    def __init__(
        self,
        *,
        commands: Mapping[tuple[str, ...], subprocess.CompletedProcess[str]] | None = None,
        files: Mapping[str, str] | None = None,
        stats: Mapping[str, os.stat_result] | None = None,
    ) -> None:
        self.commands = dict(commands or {})
        self.files = dict(files or {})
        self.stats = dict(stats or {})
        self.calls: list[tuple[str, object]] = []
        self.write_modes: list[tuple[str, int]] = []
        self.runs: list[
            tuple[tuple[str, ...], PathLike | None, Mapping[str, str] | None]
        ] = []

    def run(
        self,
        argv: Command,
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del input, timeout
        command = tuple(argv)
        self.calls.append(("run", (command, check)))
        self.runs.append((command, cwd, dict(env) if env is not None else None))
        result = self.commands.get(
            command, subprocess.CompletedProcess(list(command), 1, "", "not found")
        )
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, list(command), result.stdout, result.stderr
            )
        return result

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        self.calls.append(("read_text", key))
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(
        self,
        path: PathLike,
        text: str,
        *,
        encoding: str = "utf-8",
        mode: int = 0o644,
    ) -> None:
        del encoding
        key = os.fspath(path)
        self.calls.append(("write_text", (key, text)))
        self.write_modes.append((key, mode))
        self.files[key] = text

    def exists(self, path: PathLike) -> bool:
        key = os.fspath(path)
        self.calls.append(("exists", key))
        return key in self.files or key in self.stats

    def listdir(self, path: PathLike) -> list[str]:
        key = os.fspath(path)
        self.calls.append(("listdir", key))
        if key in self.files:
            raise NotADirectoryError(f"{key} is not a directory")
        return [Path(name).name for name in self.files if Path(name).parent == Path(key)]

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        key = os.fspath(path)
        self.calls.append(("unlink", key))
        if key not in self.files:
            if missing_ok:
                return
            raise FileNotFoundError(key)
        del self.files[key]

    def stat(self, path: PathLike) -> os.stat_result:
        key = os.fspath(path)
        self.calls.append(("stat", key))
        if key not in self.stats:
            raise FileNotFoundError(key)
        return self.stats[key]

    def chmod(self, path: PathLike, mode: int) -> None:
        self.calls.append(("chmod", (os.fspath(path), mode)))

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        self.calls.append(("chown", (os.fspath(path), uid, gid)))

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        self.calls.append(
            ("mkdir", (os.fspath(path), mode, parents, exist_ok))
        )

    def geteuid(self) -> int:
        return 0


def assert_no_host_mutation(
    test: unittest.TestCase, host: FakeHost, read_commands: set[tuple[str, ...]]
) -> None:
    mutations = [
        call
        for call in host.calls
        if call[0] in {"write_text", "unlink", "mkdir"}
        or (
            call[0] == "run"
            and isinstance(call[1], tuple)
            and (call[1][0] not in read_commands or call[1][1] is True)
        )
    ]
    test.assertEqual(mutations, [])


class PermissionErrorHost(FakeHost):
    """A fake host whose configured store directory cannot be listed."""

    def __init__(
        self,
        blocked_path: PathLike,
        *,
        commands: Mapping[tuple[str, ...], subprocess.CompletedProcess[str]] | None = None,
        files: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(commands=commands, files=files)
        self.blocked_path = os.fspath(blocked_path)

    def listdir(self, path: PathLike) -> list[str]:
        if os.fspath(path) == self.blocked_path:
            raise PermissionError("permission denied")
        return super().listdir(path)


def completed(command: Sequence[str], stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(command), 0, stdout, "")


def context(host: FakeHost, *, site: SiteConfig | None = None) -> ProvisionContext:
    lock_result = load_host_lock(LOCK)
    assert lock_result.lock is not None
    if site is None:
        site_result = load_site(EXAMPLE)
        assert site_result.config is not None
        site = site_result.config
    assert isinstance(site, SiteConfig)
    return ProvisionContext(host, lock_result.lock, site)


# The committed pins every fake below must agree with: derived once from the
# loaded lock, never typed.
HOST_LOCK = context(FakeHost()).lock

CUDA_REPOSITORY = (
    "https://developer.download.nvidia.com/compute/cuda/repos/"
    f"{HOST_LOCK.driver.repo}"
)
CUDA_SOURCE = (
    "/etc/apt/sources.list.d/"
    f"cuda-{HOST_LOCK.driver.repo.replace('/', '-')}.list"
)
CUDA_ENTRY = (
    f"deb [signed-by={_KEYRING_FILE}] {CUDA_REPOSITORY}/ /\n"
)
TOOLKIT_PACKAGE = "nvidia-container-toolkit"


def nvidia_driver_host(
    *,
    loaded_version: str | None = None,
    disk_version: str | None = None,
    loaded_closed: bool = False,
    disk_open: bool = True,
    packages: Mapping[str, str] | None = None,
    held: bool = False,
    files: Mapping[str, str] | None = None,
    commands: Mapping[tuple[str, ...], subprocess.CompletedProcess[str]] | None = None,
) -> FakeHost:
    """Build module, package, and apt readings through one fake Host."""

    installed = dict(packages or {})
    module_files = {_KERNEL_RELEASE.as_posix(): "0.0.0-fictitious\n", **(files or {})}
    lsmod = ("lsmod",)
    version_probe = _MODINFO_VERSION
    license_probe = _MODINFO_LICENSE
    package_names = sorted(name for name in installed if name.startswith("nvidia-"))
    listing = "".join(
        f"{name} install ok installed {installed[name]}\n" for name in package_names
    )
    responses: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {
        lsmod: completed(lsmod, "nvidia 1 0\n" if loaded_version else ""),
        version_probe: subprocess.CompletedProcess(
            list(version_probe), 0 if disk_version else 1,
            f"{disk_version}\n" if disk_version else "", "" if disk_version else "not found",
        ),
        license_probe: subprocess.CompletedProcess(
            list(license_probe), 0 if disk_version else 1,
            "Dual MIT/GPL\n" if disk_version and disk_open else "NVIDIA\n" if disk_version else "",
            "" if disk_version else "not found",
        ),
        _DRIVER_PACKAGES: subprocess.CompletedProcess(
            list(_DRIVER_PACKAGES), 0 if package_names else 1,
            listing, "" if package_names else "no packages found matching nvidia-*",
        ),
        ("apt-mark", "showhold"): completed(
            ("apt-mark", "showhold"),
            f"{HOST_LOCK.driver.package}\n" if held else "",
        ),
    }
    if loaded_version is not None:
        module_files.update(
            {
                "/proc/driver/nvidia": "",
                _LOADED_VERSION.as_posix(): f"{loaded_version}\n",
                _LOADED_TAINT.as_posix(): "P\n" if loaded_closed else "G\n",
            }
        )
    for name, version in installed.items():
        query, response = package_result(name, version)
        responses[query] = response
    responses.update(commands or {})
    return FakeHost(files=module_files, commands=responses)


def nvidia_install_commands(*packages: str) -> dict[tuple[str, ...], subprocess.CompletedProcess[str]]:
    """The keyring deb and absent-package apt commands the recipe invokes."""

    deb = str(_DEB_TMP)
    url = f"{CUDA_REPOSITORY}/{HOST_LOCK.driver.keyring_deb}"
    wget = ("wget", "-qO", f"{deb}.partial", url)
    move = ("mv", "-f", f"{deb}.partial", deb)
    sha = ("sha256sum", deb)
    dpkg = ("dpkg", "-i", "--force-confmiss", deb)
    commands = {
        wget: completed(wget),
        move: completed(move),
        sha: completed(sha, f"{HOST_LOCK.driver.keyring_sha256}  {deb}\n"),
        dpkg: completed(dpkg),
        ("apt-mark", "hold", HOST_LOCK.driver.package): completed(
            ("apt-mark", "hold", HOST_LOCK.driver.package)
        ),
    }
    for package in packages:
        commands.update(dict(apt_command_results([package])))
    return commands


def assert_nvidia_read_only(test: unittest.TestCase, host: FakeHost) -> None:
    """Assert a refusal made no filesystem or install-side mutation."""

    writes = [
        call for call in host.calls
        if call[0] in {"write_text", "unlink", "mkdir", "chmod", "chown"}
    ]
    test.assertEqual(writes, [])
    installs = [
        argv for argv, _, _ in host.runs
        if argv[0] in {"apt-get", "dpkg", "wget", "mv", "nvidia-ctk"}
        or argv[:2] in {
            ("apt-mark", "hold"),
            ("systemctl", "enable"),
        }
    ]
    test.assertEqual(installs, [])


def baseline_host() -> FakeHost:
    document = json.loads((BASELINE / "commands.json").read_text())
    commands: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {}
    for entry in document["commands"]:
        argv = tuple(entry["argv"])
        commands[argv] = subprocess.CompletedProcess(
            list(argv),
            int(entry.get("returncode", 0)),
            str(entry.get("stdout", "")),
            str(entry.get("stderr", "")),
        )
    lsblk = ("lsblk", "-J", "-b", "-o", "NAME,TYPE,SIZE,WWN,FSTYPE,MOUNTPOINT,PKNAME")
    commands[lsblk] = completed(lsblk, (BASELINE / "lsblk.json").read_text())
    files = {
        "/etc/os-release": (BASELINE / "os-release").read_text(),
        "/etc/fstab": (BASELINE / "fstab").read_text(),
        "/home/fpdadmin/.ssh/authorized_keys": (BASELINE / "fpdadmin-authorized_keys").read_text(),
        os.fspath(LOCK): LOCK.read_text(),
    }
    return FakeHost(files=files, commands=commands)


class RegistryContract(unittest.TestCase):
    def test_names_are_unique_and_require_only_earlier_steps(self) -> None:
        names: set[str] = set()
        for step in STEPS:
            self.assertNotIn(step.name, names)
            self.assertTrue(
                all(requirement in names for requirement in step.requires),
                step.name,
            )
            names.add(step.name)
        self.assertEqual(
            [step.name for step in STEPS[:5]],
            ["platform", "wait-online", "egress-proxy", "host-tools", "gideon-command"],
        )
        self.assertEqual(len(STEPS), 22)
        self.assertEqual(
            {step.name for step in STEPS if step.gpu_host_only},
            {"nvidia-driver", "nvidia-toolkit"},
        )
        self.assertEqual(
            {step.name for step in STEPS if step.build_box_only},
            {"kvm", "registry", "gh-runner"},
        )
        self.assertTrue(
            {
                step.name for step in STEPS if step.gpu_host_only
            }.isdisjoint({step.name for step in STEPS if step.build_box_only})
        )
        ordered_names = [step.name for step in STEPS]
        self.assertEqual(ordered_names[ordered_names.index("time-sync") + 1], "timezone")


class PlatformStepTests(unittest.TestCase):
    def test_supported_platform_converges_and_is_check_only(self) -> None:
        host = FakeHost(
            files={"/etc/os-release": 'ID=ubuntu\nVERSION_ID="26.04"\n'},
            commands={("uname", "-m"): completed(("uname", "-m"), "x86_64\n")},
        )
        step = PlatformStep()
        result = step.check(context(host))
        self.assertEqual(result, CheckResult(Disposition.CONVERGED, "supported platform", ""))
        with self.assertRaises(NotImplementedError):
            step.apply(context(host))

    def test_platform_mismatch_is_halting_unfixable(self) -> None:
        host = FakeHost(
            files={"/etc/os-release": "ID=debian\nVERSION_ID=13\n"},
            commands={("uname", "-m"): completed(("uname", "-m"), "x86_64\n")},
        )
        result = PlatformStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.UNFIXABLE)
        self.assertTrue(result.halts_run)
        self.assertEqual(
            result.fix,
            "Reinstall Ubuntu 26.04 Server on this host, then re-run provision.",
        )


class ProxyStepTests(unittest.TestCase):
    def test_empty_proxy_is_a_noop(self) -> None:
        host = FakeHost()
        current_site = context(host).site
        assert current_site is not None
        site = replace(current_site, egress_proxy="")
        result = EgressProxyStep().check(context(host, site=site))
        self.assertEqual(result.disposition, Disposition.CONVERGED)
        self.assertEqual(
            [call for call in host.calls if call[0] in ("write_text", "unlink")], []
        )

    def test_empty_proxy_removes_a_stale_conf(self) -> None:
        conf = "/etc/apt/apt.conf.d/99gideon-proxy"
        host = FakeHost(files={conf: 'Acquire::http::Proxy "http://old:3128";\n'})
        current_site = context(host).site
        assert current_site is not None
        site = replace(current_site, egress_proxy="")
        step = EgressProxyStep()
        self.assertEqual(step.check(context(host, site=site)).disposition, Disposition.DRIFT)
        step.apply(context(host, site=site))
        self.assertNotIn(conf, host.files)
        self.assertEqual(step.check(context(host, site=site)).disposition, Disposition.CONVERGED)

    def test_proxy_missing_and_different_are_drift_and_apply_writes(self) -> None:
        host = FakeHost()
        current_site = context(host).site
        assert current_site is not None
        site = replace(current_site, egress_proxy="http://proxy.example:3128")
        step = EgressProxyStep()
        self.assertEqual(step.check(context(host, site=site)).disposition, Disposition.DRIFT)
        step.apply(context(host, site=site))
        self.assertEqual(
            host.files["/etc/apt/apt.conf.d/99gideon-proxy"],
            'Acquire::http::Proxy "http://proxy.example:3128";\n'
            'Acquire::https::Proxy "http://proxy.example:3128";\n',
        )
        self.assertEqual(step.check(context(host, site=site)).disposition, Disposition.CONVERGED)


class AccountStepTests(unittest.TestCase):
    SSHD = ("sshd", "-T")
    POLICY = "/etc/ssh/sshd_config.d/00-gideon-key-only.conf"
    RETIRED = "/etc/ssh/sshd_config.d/99-gideon-key-only.conf"
    POLICY_TEXT = "PasswordAuthentication no\nKbdInteractiveAuthentication no\n"

    def csa_host(self, *, password: str, keyboard: str, files: Mapping[str, str] | None = None) -> FakeHost:
        commands = {
            ("getent", "group", "sudo"): completed(
                ("getent", "group", "sudo"), "sudo:x:27:alice,bob\n"
            ),
            ("getent", "passwd", "alice"): completed(
                ("getent", "passwd", "alice"),
                "alice:x:1001:27::/home/alice:/bin/bash\n",
            ),
            ("getent", "passwd", "bob"): completed(
                ("getent", "passwd", "bob"),
                "bob:x:1002:27::/home/bob:/bin/bash\n",
            ),
            self.SSHD: completed(
                self.SSHD,
                f"passwordauthentication {password}\nkbdinteractiveauthentication {keyboard}\n",
            ),
        }
        keys = {
            "/home/alice/.ssh/authorized_keys": "ssh-ed25519 AAAA\n",
            "/home/bob/.ssh/authorized_keys": "ssh-ed25519 BBBB\n",
        }
        keys.update(files or {})
        return FakeHost(commands=commands, files=keys)

    def assert_csa_refusal(self, host: FakeHost, *details: str) -> None:
        step = CsaAccountsStep()
        reading = step.check(context(host))
        self.assertEqual(reading.disposition, Disposition.UNFIXABLE)
        for detail in details:
            self.assertIn(detail, reading.detail)
        host.calls.clear()
        with self.assertRaises(StepFailure) as raised:
            step.apply(context(host))
        self.assertEqual((raised.exception.detail, raised.exception.fix), (reading.detail, reading.fix))
        assert_no_host_mutation(
            self,
            host,
            {self.SSHD, ("getent", "group", "sudo"),
             ("getent", "passwd", "alice"), ("getent", "passwd", "bob")},
        )

    def test_service_user_missing_drifts_and_useradd_applies(self) -> None:
        command = ("useradd", "--system", "--shell", "/usr/sbin/nologin", "gideon")
        host = FakeHost(commands={command: completed(command)})
        step = ServiceUserStep()
        self.assertEqual(step.check(context(host)).disposition, Disposition.DRIFT)
        step.apply(context(host))
        self.assertIn(
            ("run", (("useradd", "--system", "--shell", "/usr/sbin/nologin", "gideon"), True)),
            host.calls,
        )

    def test_service_user_system_nologin_converges(self) -> None:
        host = FakeHost(
            commands={
                ("getent", "passwd", "gideon"): completed(
                    ("getent", "passwd", "gideon"),
                    "gideon:x:998:998::/nonexistent:/usr/sbin/nologin\n",
                )
            }
        )
        self.assertEqual(
            ServiceUserStep().check(context(host)).disposition,
            Disposition.CONVERGED,
        )

    def test_csa_accounts_need_two_keys_before_policy_apply(self) -> None:
        host = FakeHost(
            commands={
                ("getent", "group", "sudo"): completed(
                    ("getent", "group", "sudo"), "sudo:x:27:alice\n"
                ),
                ("getent", "passwd", "alice"): completed(
                    ("getent", "passwd", "alice"),
                    "alice:x:1001:27::/home/alice:/bin/bash\n",
                ),
            },
            files={"/home/alice/.ssh/authorized_keys": "ssh-ed25519 AAAA\n"},
        )
        step = CsaAccountsStep()
        result = step.check(context(host))
        self.assertEqual(result.disposition, Disposition.UNFIXABLE)
        self.assertIn("at least two CSA accounts", result.fix)
        self.assertNotIn("write_text", {call[0] for call in host.calls})
        self.assertNotIn(("run", (self.SSHD, False)), host.calls)
        host.calls.clear()
        with self.assertRaises(StepFailure) as raised:
            step.apply(context(host))
        self.assertEqual((raised.exception.detail, raised.exception.fix), (result.detail, result.fix))
        assert_no_host_mutation(
            self, host, {("getent", "group", "sudo"), ("getent", "passwd", "alice")}
        )

    def test_two_csa_keys_allow_policy_to_converge(self) -> None:
        host = self.csa_host(password="yes", keyboard="no", files={self.RETIRED: self.POLICY_TEXT})
        reload_command = ("systemctl", "reload", "ssh")
        host.commands[reload_command] = completed(reload_command)
        step = CsaAccountsStep()
        self.assertEqual(step.check(context(host)).disposition, Disposition.DRIFT)
        step.apply(context(host))
        self.assertEqual(host.files[self.POLICY], self.POLICY_TEXT)
        self.assertNotIn(self.RETIRED, host.files)
        mutations = [call for call in host.calls if call[0] in {"write_text", "unlink"} or call == ("run", (reload_command, True))]
        self.assertEqual(
            mutations,
            [("write_text", (self.POLICY, self.POLICY_TEXT)),
             ("unlink", self.RETIRED), ("run", (reload_command, True))],
        )
        host.commands[self.SSHD] = completed(
            self.SSHD, "passwordauthentication no\nkbdinteractiveauthentication no\n"
        )
        recheck = step.check(context(host))
        self.assertEqual(recheck.disposition, Disposition.CONVERGED, recheck)

    def test_login_rule_truth_table(self) -> None:
        cases: tuple[tuple[str, str, str, dict[str, str], Disposition, str], ...] = (
            ("default", "yes", "no", {}, Disposition.DRIFT, "installed default"),
            ("outside policy", "no", "no", {}, Disposition.CONVERGED, "outside GIDEON's drop-in"),
            ("own policy", "no", "no", {self.POLICY: self.POLICY_TEXT}, Disposition.CONVERGED, "is current"),
            ("retired file", "no", "no", {self.RETIRED: self.POLICY_TEXT}, Disposition.DRIFT, "retired name"),
            ("current beside retired", "no", "no", {self.POLICY: self.POLICY_TEXT, self.RETIRED: self.POLICY_TEXT}, Disposition.DRIFT, "current beside its retired file"),
        )
        for name, password, keyboard, files, disposition, detail in cases:
            with self.subTest(name=name):
                host = self.csa_host(password=password, keyboard=keyboard, files=files)
                reading = CsaAccountsStep().check(context(host))
                self.assertEqual(reading.disposition, disposition)
                self.assertIn(detail, reading.detail)
                if disposition is Disposition.CONVERGED:
                    host.calls.clear()
                    CsaAccountsStep().apply(context(host))
                    assert_no_host_mutation(
                        self,
                        host,
                        {self.SSHD, ("getent", "group", "sudo"),
                         ("getent", "passwd", "alice"), ("getent", "passwd", "bob")},
                    )

    def test_default_login_rule_writes_the_new_policy(self) -> None:
        host = self.csa_host(password="yes", keyboard="no")
        reload_command = ("systemctl", "reload", "ssh")
        host.commands[reload_command] = completed(reload_command)
        step = CsaAccountsStep()
        self.assertEqual(step.check(context(host)).disposition, Disposition.DRIFT)
        host.calls.clear()
        step.apply(context(host))
        self.assertEqual(host.files[self.POLICY], self.POLICY_TEXT)
        self.assertNotIn(("unlink", self.RETIRED), host.calls)
        self.assertIn(("run", (reload_command, True)), host.calls)

    def test_moved_short_login_rule_refuses_before_mutation(self) -> None:
        for password, keyboard, found in (
            ("yes", "no", "PasswordAuthentication yes"),
            ("no", "yes", "KbdInteractiveAuthentication yes"),
        ):
            with self.subTest(found=found):
                host = self.csa_host(
                    password=password, keyboard=keyboard,
                    files={self.POLICY: self.POLICY_TEXT, self.RETIRED: self.POLICY_TEXT},
                )
                self.assert_csa_refusal(host, "SSH login rule", found, "no for both", self.POLICY)

    def test_sshd_reader_failure_refuses_before_mutation(self) -> None:
        host = self.csa_host(password="yes", keyboard="no")
        host.commands[self.SSHD] = subprocess.CompletedProcess(
            list(self.SSHD), 1, "", "fictitious sshd error\n"
        )
        self.assert_csa_refusal(host, "sshd -T", "fictitious sshd error")


def disk_devices(*, data_children: list[dict[str, object]] | None = None) -> list[dict[str, object]]:
    return [
        {
            "name": "sda",
            "type": "disk",
            "size": 1600000000000,
            "wwn": "os-wwn",
            "fstype": None,
            "mountpoint": None,
            "pkname": None,
            "children": [
                {
                    "name": "vg-root",
                    "type": "lvm",
                    "size": 50000000000,
                    "wwn": None,
                    "fstype": "ext4",
                    "mountpoint": "/",
                    "pkname": "sda",
                },
                {
                    "name": "vg-docker",
                    "type": "lvm",
                    "size": 50000000000,
                    "wwn": None,
                    "fstype": "ext4",
                    "mountpoint": "/var/lib/docker",
                    "pkname": "sda",
                },
                {
                    "name": "vg-swap",
                    "type": "lvm",
                    "size": 16000000000,
                    "wwn": None,
                    "fstype": "swap",
                    "mountpoint": "[SWAP]",
                    "pkname": "sda",
                },
            ],
        },
        {
            "name": "sdb",
            "type": "disk",
            "size": 8730000000000,
            "wwn": "data-wwn",
            "fstype": None,
            "mountpoint": None,
            "pkname": None,
            "children": data_children or [],
        },
    ]


def disk_host(
    devices: list[dict[str, object]],
    *,
    commands: Mapping[tuple[str, ...], subprocess.CompletedProcess[str]] | None = None,
    files: Mapping[str, str] | None = None,
    stats: Mapping[str, os.stat_result] | None = None,
) -> FakeHost:
    all_commands: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {
        ("lsblk", "-J", "-b", "-o", "NAME,TYPE,SIZE,WWN,FSTYPE,MOUNTPOINT,PKNAME"): completed(
            ("lsblk", "-J", "-b", "-o", "NAME,TYPE,SIZE,WWN,FSTYPE,MOUNTPOINT,PKNAME"),
            json.dumps({"blockdevices": devices}),
        ),
        ("wipefs", "-n", "/dev/sdb"): completed(("wipefs", "-n", "/dev/sdb")),
    }
    all_commands.update(commands or {})
    return FakeHost(commands=all_commands, files=files, stats=stats)


def disk_command_output(
    command: Sequence[str], output: str
) -> tuple[tuple[str, ...], subprocess.CompletedProcess[str]]:
    return tuple(command), completed(command, output)


def add_data_directory(host: FakeHost, names: Sequence[str] = ()) -> None:
    host.stats["/data"] = directory_stat(0o755)
    host.files.update({f"/data/{name}": "" for name in names})


class DiskLayoutStepTests(unittest.TestCase):
    def test_occupied_unmounted_data_refuses_with_sorted_top_level_names(self) -> None:
        names = ("omega", ".local", "alpha")
        host = disk_host(disk_devices())
        add_data_directory(host, names)

        result = DiskLayoutStep().check(context(host))

        self.assertEqual(result.disposition, Disposition.UNFIXABLE)
        self.assertEqual(
            result.detail,
            f"/data is not a mount point and holds 3 entries: {', '.join(sorted(names))}",
        )
        self.assertIn("docs/runbooks/install-upgrade.md §8", result.fix)
        self.assertIn("re-run provision", result.fix)

    def test_occupied_data_caps_reported_names_but_keeps_total(self) -> None:
        names = tuple(f"entry-{index:02d}" for index in reversed(range(12)))
        host = disk_host(disk_devices())
        add_data_directory(host, names)

        result = DiskLayoutStep().check(context(host))

        self.assertEqual(result.disposition, Disposition.UNFIXABLE)
        self.assertEqual(
            result.detail,
            "/data is not a mount point and holds 12 entries: "
            f"{', '.join(sorted(names)[:10])}, and 2 more",
        )
        self.assertNotIn(sorted(names)[10], result.detail)
        self.assertNotIn(sorted(names)[11], result.detail)

    def test_one_occupied_data_entry_uses_singular(self) -> None:
        host = disk_host(disk_devices())
        add_data_directory(host, ("only-entry",))

        result = DiskLayoutStep().check(context(host))

        self.assertEqual(result.disposition, Disposition.UNFIXABLE)
        self.assertEqual(
            result.detail,
            "/data is not a mount point and holds 1 entry: only-entry",
        )

    def test_occupied_data_apply_refuses_before_any_disk_mutation(self) -> None:
        host = disk_host(disk_devices())
        add_data_directory(host, ("client-data",))
        step = DiskLayoutStep()
        check = step.check(context(host))

        with self.assertRaises(StepFailure) as raised:
            step.apply(context(host))

        self.assertEqual(raised.exception.detail, check.detail)
        self.assertEqual(raised.exception.fix, check.fix)
        self.assertEqual(
            [
                call
                for call in host.calls
                if call[0] == "run"
                and isinstance(call[1], tuple)
                and isinstance(call[1][0], tuple)
                and call[1][0][0]
                in {"pvcreate", "vgcreate", "lvcreate", "mkfs.xfs", "mount"}
            ],
            [],
        )
        self.assertEqual(
            [call for call in host.calls if call[0] in {"write_text", "mkdir"}],
            [],
        )

    def test_absent_and_empty_data_keep_pv_missing_drift(self) -> None:
        for present in (False, True):
            with self.subTest(data_directory_present=present):
                host = disk_host(disk_devices())
                if present:
                    add_data_directory(host)

                result = DiskLayoutStep().check(context(host))

                self.assertEqual(result.disposition, Disposition.DRIFT)
                self.assertEqual(result.detail, "the data disk PV is missing")

    def test_complete_unmounted_volume_with_occupied_data_refuses(self) -> None:
        devices = disk_devices(
            data_children=[
                {
                    "name": "sdb1",
                    "type": "part",
                    "size": 8730000000000,
                    "wwn": None,
                    "fstype": "LVM2_member",
                    "mountpoint": None,
                    "pkname": "sdb",
                }
            ]
        )
        uuid = "fictitious-data-uuid"
        commands = dict(
            [
                disk_command_output(
                    ("pvs", "--reportformat", "json", "-o", "pv_name,vg_name"),
                    '{"report":[{"pv":[{"pv_name":"/dev/sdb","vg_name":"vg_data"}]}]}',
                ),
                disk_command_output(
                    ("vgs", "--reportformat", "json", "--units", "b", "-o", "vg_name,vg_size"),
                    '{"report":[{"vg":[{"vg_name":"vg_data","vg_size":"8730000000000B"}]}]}',
                ),
                disk_command_output(
                    ("lvs", "--reportformat", "json", "-o", "lv_name,vg_name,lv_path"),
                    '{"report":[{"lv":[{"lv_name":"data","vg_name":"vg_data","lv_path":"/dev/vg_data/data"}]}]}',
                ),
                disk_command_output(
                    ("blkid", "-o", "value", "-s", "TYPE", "/dev/vg_data/data"),
                    "xfs\n",
                ),
                disk_command_output(
                    ("blkid", "-o", "value", "-s", "UUID", "/dev/vg_data/data"),
                    f"{uuid}\n",
                ),
            ]
        )
        host = disk_host(
            devices,
            commands=commands,
            files={
                "/etc/fstab": (
                    "# GIDEON BEGIN provision:disk-layout\n"
                    f"UUID={uuid} /data xfs defaults,nofail 0 2\n"
                    "# GIDEON END provision:disk-layout\n"
                )
            },
        )
        step = DiskLayoutStep()
        unoccupied = step.check(context(host))
        self.assertEqual(unoccupied.disposition, Disposition.DRIFT)
        self.assertEqual(unoccupied.detail, "/data is not mounted")

        add_data_directory(host, ("client-data",))
        occupied = step.check(context(host))
        self.assertEqual(occupied.disposition, Disposition.UNFIXABLE)
        self.assertIn("client-data", occupied.detail)

    def test_unlistable_data_refuses_in_check_and_apply(self) -> None:
        permitted_commands = disk_host(disk_devices()).commands
        permission_denied = PermissionErrorHost("/data", commands=permitted_commands)
        add_data_directory(permission_denied, ("client-data",))
        data_file = disk_host(disk_devices(), files={"/data": "regular file"})

        for host in (permission_denied, data_file):
            with self.subTest(host=type(host).__name__):
                step = DiskLayoutStep()
                result = step.check(context(host))
                self.assertEqual(result.disposition, Disposition.UNFIXABLE)
                self.assertIn("cannot list /data", result.detail)
                self.assertIn("Make /data a directory", result.fix)

                with self.assertRaises(StepFailure) as raised:
                    step.apply(context(host))
                self.assertEqual(raised.exception.detail, result.detail)
                self.assertEqual(raised.exception.fix, result.fix)
                self.assertNotIn("pvcreate", [run[0][0] for run in host.runs])

    def test_mounted_data_does_not_list_its_entries(self) -> None:
        findmnt = ("findmnt", "-rn", "-o", "TARGET", "--mountpoint", "/data")
        host = disk_host(
            disk_devices(),
            commands={findmnt: completed(findmnt, "/data\n")},
        )
        add_data_directory(host, ("client-data",))

        result = DiskLayoutStep().check(context(host))

        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertEqual(result.detail, "the data disk PV is missing")
        self.assertNotIn(("listdir", "/data"), host.calls)
        self.assertEqual([run[0] for run in host.runs].count(findmnt), 1)

    def test_blank_and_partial_gideon_disks_report_first_missing_state(self) -> None:
        blank = disk_host(disk_devices())
        blank_result = DiskLayoutStep().check(context(blank))
        self.assertEqual(blank_result.disposition, Disposition.DRIFT)
        self.assertIn("PV", blank_result.detail)

        partial_children = [
            {
                "name": "sdb1",
                "type": "part",
                "size": 8730000000000,
                "wwn": None,
                "fstype": "LVM2_member",
                "mountpoint": None,
                "pkname": "sdb",
            }
        ]
        pvs = '{"report":[{"pv":[{"pv_name":"/dev/sdb","vg_name":"vg_data"}]}]}'
        vgs = '{"report":[{"vg":[{"vg_name":"vg_data","vg_size":"8730000000000B"}]}]}'
        partial = disk_host(
            disk_devices(data_children=partial_children),
            commands=dict(
                [
                    disk_command_output(("pvs", "--reportformat", "json", "-o", "pv_name,vg_name"), pvs),
                    disk_command_output(
                        ("vgs", "--reportformat", "json", "--units", "b", "-o", "vg_name,vg_size"),
                        vgs,
                    ),
                ]
            ),
        )
        partial_result = DiskLayoutStep().check(context(partial))
        self.assertEqual(partial_result.disposition, Disposition.DRIFT)
        self.assertIn("LV", partial_result.detail)

    def test_foreign_disk_and_ambiguous_identification_never_mutate(self) -> None:
        foreign = disk_host(
            disk_devices(
                data_children=[
                    {
                        "name": "sdb1",
                        "type": "part",
                        "size": 8730000000000,
                        "wwn": None,
                        "fstype": "ext4",
                        "mountpoint": "/foreign",
                        "pkname": "sdb",
                    }
                ]
            )
        )
        result = DiskLayoutStep().check(context(foreign))
        self.assertEqual(result.disposition, Disposition.UNFIXABLE)
        self.assertEqual(
            [call for call in foreign.calls if call[0] in {"write_text", "unlink", "mkdir", "chmod", "chown"}],
            [],
        )

        zero = disk_host(disk_devices()[:1])
        zero_result = DiskLayoutStep().check(context(zero))
        self.assertEqual(zero_result.disposition, Disposition.UNFIXABLE)
        self.assertIn("(none)", zero_result.detail)

        devices = disk_devices()
        devices.append({
            "name": "sdc",
            "type": "disk",
            "size": 9000000000000,
            "wwn": "other-wwn",
            "fstype": None,
            "mountpoint": None,
            "pkname": None,
        })
        multiple = disk_host(devices)
        multiple_result = DiskLayoutStep().check(context(multiple))
        self.assertEqual(multiple_result.disposition, Disposition.UNFIXABLE)
        self.assertIn("sdb", multiple_result.detail)
        self.assertIn("sdc", multiple_result.detail)
        self.assertIn("other-wwn", multiple_result.detail)

        devices = disk_devices()
        devices[1]["size"] = devices[0]["size"]
        small = disk_host(devices)
        small_result = DiskLayoutStep().check(context(small))
        self.assertEqual(small_result.disposition, Disposition.UNFIXABLE)
        self.assertIn("wrong size class for the data-disk layout", small_result.detail)

    def test_wipefs_signature_and_missing_wwn_refuse_mutation(self) -> None:
        signed = disk_host(
            disk_devices(),
            commands={
                ("wipefs", "-n", "/dev/sdb"): completed(
                    ("wipefs", "-n", "/dev/sdb"),
                    "/dev/sdb: 8 bytes were erased at offset 0x218 (zfs_member)\n",
                )
            },
        )
        result = DiskLayoutStep().check(context(signed))
        self.assertEqual(result.disposition, Disposition.UNFIXABLE)
        self.assertEqual(
            [call for call in signed.calls if call[0] in {"write_text", "unlink", "mkdir", "chmod", "chown"}],
            [],
        )

        devices = disk_devices()
        devices[1]["wwn"] = None
        anonymous = disk_host(devices)
        result = DiskLayoutStep().check(context(anonymous))
        self.assertEqual(result.disposition, Disposition.UNFIXABLE)
        self.assertIn("stable identity", result.detail)

    def test_vg_data_on_another_device_refuses(self) -> None:
        host = disk_host(
            disk_devices(),
            commands=dict(
                [
                    disk_command_output(
                        ("vgs", "--reportformat", "json", "--units", "b", "-o", "vg_name,vg_size"),
                        '{"report":[{"vg":[{"vg_name":"vg_data","vg_size":"8730000000000B"}]}]}',
                    ),
                ]
            ),
        )
        result = DiskLayoutStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.UNFIXABLE)
        self.assertIn("wrong device", result.detail)
        self.assertEqual(
            [call for call in host.calls if call[0] in {"write_text", "unlink", "mkdir", "chmod", "chown"}],
            [],
        )

    def test_fstab_rewrite_preserves_foreign_lines_and_recheck_converges(self) -> None:
        children = [
            {
                "name": "sdb1",
                "type": "part",
                "size": 8730000000000,
                "wwn": None,
                "fstype": "LVM2_member",
                "mountpoint": None,
                "pkname": "sdb",
            }
        ]
        commands: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = dict(
            [
                disk_command_output(
                    ("pvs", "--reportformat", "json", "-o", "pv_name,vg_name"),
                    '{"report":[{"pv":[{"pv_name":"/dev/sdb","vg_name":"vg_data"}]}]}',
                ),
                disk_command_output(
                    ("vgs", "--reportformat", "json", "--units", "b", "-o", "vg_name,vg_size"),
                    '{"report":[{"vg":[{"vg_name":"vg_data","vg_size":"8730000000000B"}]}]}',
                ),
                disk_command_output(
                    ("lvs", "--reportformat", "json", "-o", "lv_name,vg_name,lv_path"),
                    '{"report":[{"lv":[{"lv_name":"data","vg_name":"vg_data","lv_path":"/dev/vg_data/data"}]}]}',
                ),
                disk_command_output(
                    ("blkid", "-o", "value", "-s", "TYPE", "/dev/vg_data/data"),
                    "xfs\n",
                ),
                disk_command_output(
                    ("blkid", "-o", "value", "-s", "UUID", "/dev/vg_data/data"),
                    "uuid-data\n",
                ),
                disk_command_output(
                    ("findmnt", "-rn", "-o", "TARGET", "--mountpoint", "/data"),
                    "/data\n",
                ),
                disk_command_output(
                    ("getent", "passwd", "gideon"),
                    "gideon:x:998:998::/nonexistent:/usr/sbin/nologin\n",
                ),
            ]
        )
        stale = (
            "# foreign line\n"
            "# /data was on /dev/vg_data/data during curtin installation\n"
            "/dev/foreign /foreign ext4 defaults 0 2\n"
            "/dev/vg_data/data /data xfs defaults 0 0\n"
            "# GIDEON BEGIN provision:disk-layout\n"
            "UUID=old /data xfs defaults 0 2\n"
            "# GIDEON END provision:disk-layout\n"
        )
        stats = {
            f"/data/{name}": os.stat_result((0o40755, 0, 0, 0, 998, 998, 0, 0, 0, 0, 0))
            for name in (
                "fast",
                "bulk",
                "work",
                "models",
                "registry",
                "drill",
                "ci",
                "backup-staging",
                "acceptance",
                "observability",
            )
        }
        stats["/data/bulk/cas"] = os.stat_result(
            (0o42770, 0, 0, 0, 998, 998, 0, 0, 0, 0, 0)
        )
        stats[str(worker.SNAPSHOTS_ROOT)] = directory_stat(worker.DIR_MODE, 998, 998)
        stats[QDRANT_DATA_ROOT] = os.stat_result(
            (0o40750, 0, 0, 0, 998, 998, 0, 0, 0, 0, 0)
        )
        stats[OPENSEARCH_DATA_ROOT] = directory_stat(
            0o700, OPENSEARCH_UID, OPENSEARCH_GID
        )
        stats["/data/observability/prometheus"] = os.stat_result(
            (0o40750, 0, 0, 0, 65534, 65534, 0, 0, 0, 0)
        )
        stats["/data/observability/grafana"] = os.stat_result(
            (0o40750, 0, 0, 0, 472, 472, 0, 0, 0, 0)
        )
        host = disk_host(
            disk_devices(data_children=children),
            commands=commands,
            files={
                "/etc/fstab": stale,
                "/dev/disk/by-id/wwn-data-wwn": "",
            },
            stats=stats,
        )
        step = DiskLayoutStep()
        self.assertEqual(step.check(context(host)).disposition, Disposition.DRIFT)
        step.apply(context(host))
        self.assertEqual(
            host.files["/etc/fstab"],
            "# foreign line\n"
            "# /data was on /dev/vg_data/data during curtin installation\n"
            "/dev/foreign /foreign ext4 defaults 0 2\n"
            "# GIDEON BEGIN provision:disk-layout\n"
            "UUID=uuid-data /data xfs defaults,nofail 0 2\n"
            "# GIDEON END provision:disk-layout\n",
        )
        data_entries = [
            line
            for line in host.files["/etc/fstab"].splitlines()
            if not line.startswith("#") and line.split()[1:2] == ["/data"]
        ]
        self.assertEqual(len(data_entries), 1)
        recheck = step.check(context(host))
        self.assertEqual(recheck.disposition, Disposition.CONVERGED, recheck)
        self.assertIn(
            ("mkdir", (QDRANT_DATA_ROOT, 0o750, False, True)), host.calls
        )
        self.assertIn(("chmod", (QDRANT_DATA_ROOT, 0o750)), host.calls)
        self.assertIn(("chown", (QDRANT_DATA_ROOT, 998, 998)), host.calls)
        self.assertIn(("mkdir", (OPENSEARCH_DATA_ROOT, 0o700, False, True)), host.calls)
        self.assertIn(("chmod", (OPENSEARCH_DATA_ROOT, 0o700)), host.calls)
        self.assertIn(
            ("chown", (OPENSEARCH_DATA_ROOT, OPENSEARCH_UID, OPENSEARCH_GID)),
            host.calls,
        )
        correct_qdrant = host.stats.pop(QDRANT_DATA_ROOT)
        missing_qdrant = step.check(context(host))
        self.assertEqual(missing_qdrant.disposition, Disposition.DRIFT)
        self.assertIn(QDRANT_DATA_ROOT, missing_qdrant.detail)
        host.stats[QDRANT_DATA_ROOT] = directory_stat(0o755, 998, 998)
        wrong_qdrant_mode = step.check(context(host))
        self.assertEqual(wrong_qdrant_mode.disposition, Disposition.DRIFT)
        self.assertIn("0750", wrong_qdrant_mode.detail)
        host.stats[QDRANT_DATA_ROOT] = correct_qdrant
        correct_opensearch = host.stats.pop(OPENSEARCH_DATA_ROOT)
        missing_opensearch = step.check(context(host))
        self.assertEqual(missing_opensearch.disposition, Disposition.DRIFT)
        self.assertIn(OPENSEARCH_DATA_ROOT, missing_opensearch.detail)
        host.stats[OPENSEARCH_DATA_ROOT] = directory_stat(
            0o755, OPENSEARCH_UID, OPENSEARCH_GID
        )
        wrong_opensearch_mode = step.check(context(host))
        self.assertEqual(wrong_opensearch_mode.disposition, Disposition.DRIFT)
        self.assertIn("0700", wrong_opensearch_mode.detail)
        host.stats[OPENSEARCH_DATA_ROOT] = directory_stat(0o700, 998, 998)
        wrong_opensearch_owner = step.check(context(host))
        self.assertEqual(wrong_opensearch_owner.disposition, Disposition.DRIFT)
        self.assertIn(OPENSEARCH_DATA_ROOT, wrong_opensearch_owner.detail)
        host.stats[OPENSEARCH_DATA_ROOT] = correct_opensearch
        cas_path = "/data/bulk/cas"
        bulk_chown = host.calls.index(("chown", ("/data/bulk", 998, 998)))
        self.assertEqual(
            host.calls[bulk_chown + 1 : bulk_chown + 4],
            [
                ("mkdir", (cas_path, 0o2770, False, True)),
                ("chmod", (cas_path, 0o2770)),
                ("chown", (cas_path, 998, 998)),
            ],
        )
        snapshots_path = str(worker.SNAPSHOTS_ROOT)
        self.assertEqual(
            host.calls[bulk_chown + 4 : bulk_chown + 7],
            [
                ("mkdir", (snapshots_path, worker.DIR_MODE, False, True)),
                ("chmod", (snapshots_path, worker.DIR_MODE)),
                ("chown", (snapshots_path, 998, 998)),
            ],
        )
        correct_cas = host.stats.pop(cas_path)
        missing_cas = step.check(context(host))
        self.assertEqual(missing_cas.disposition, Disposition.DRIFT)
        self.assertIn(cas_path, missing_cas.detail)
        host.stats[cas_path] = directory_stat(0o770, 998, 998)
        wrong_mode = step.check(context(host))
        self.assertEqual(wrong_mode.disposition, Disposition.DRIFT)
        self.assertIn(cas_path, wrong_mode.detail)
        self.assertIn("2770", wrong_mode.detail)
        host.stats[cas_path] = correct_cas
        correct_snapshots = host.stats.pop(snapshots_path)
        missing_snapshots = step.check(context(host))
        self.assertEqual(missing_snapshots.disposition, Disposition.DRIFT)
        self.assertIn(snapshots_path, missing_snapshots.detail)
        host.stats[snapshots_path] = directory_stat(0o770, 998, 998)
        wrong_snapshots_mode = step.check(context(host))
        self.assertEqual(wrong_snapshots_mode.disposition, Disposition.DRIFT)
        self.assertIn("2770", wrong_snapshots_mode.detail)
        host.stats[snapshots_path] = correct_snapshots
        self.assertIn(
            ("chown", ("/data/observability", 998, 998)),
            host.calls,
        )
        self.assertIn(
            ("chmod", ("/data/observability/prometheus", 0o750)),
            host.calls,
        )
        self.assertIn(
            ("chown", ("/data/observability/prometheus", 65534, 65534)),
            host.calls,
        )
        self.assertIn(
            ("chown", ("/data/observability/grafana", 472, 472)),
            host.calls,
        )

        host.files["/etc/fstab"] += "/dev/vg_data/data /data xfs defaults 0 0\n"
        stray = step.check(context(host))
        self.assertEqual(stray.disposition, Disposition.DRIFT)
        self.assertIn("unmanaged /data fstab entry", stray.detail)


def package_result(
    package: str, version: str, *, status: str = "install ok"
) -> tuple[tuple[str, ...], subprocess.CompletedProcess[str]]:
    command = ("dpkg-query", "-W", "-f=${Status} ${Version}\\n", package)
    return command, completed(command, f"{status} installed {version}\n")


def apt_command_results(
    packages: Sequence[str],
) -> tuple[tuple[tuple[str, ...], subprocess.CompletedProcess[str]], ...]:
    """Record a successful absent-package install and its apt rehearsal."""

    update = ("apt-get", "update")
    rehearsal = ("apt-get", "-s", "install", "-y", *packages)
    install = ("apt-get", "install", "-y", *packages)
    stdout = "".join(f"Inst {package} (99.0-fictitious local)\n" for package in packages)
    return (
        (update, completed(update)),
        (rehearsal, completed(rehearsal, stdout)),
        (install, completed(install)),
    )


class PackageVersionTests(unittest.TestCase):
    def test_held_installed_and_removed_states(self) -> None:
        version = "1000.0.0-1ubuntu1"
        held, held_output = package_result("nvidia-open", version, status="hold ok")
        host = FakeHost(commands={held: held_output})
        self.assertEqual(
            package_version(context(host), "nvidia-open"), version
        )

        gone, gone_output = package_result("nvidia-open", version)
        gone_output.stdout = f"deinstall ok config-files {version}\n"
        host = FakeHost(commands={gone: gone_output})
        self.assertIsNone(package_version(context(host), "nvidia-open"))

        self.assertIsNone(package_version(context(FakeHost()), "nvidia-open"))


class AptInstallTests(unittest.TestCase):
    def test_all_present_runs_no_apt_command(self) -> None:
        packages = ("installed-package", "held-package")
        installed, installed_result = package_result(packages[0], "1.0")
        held, held_result = package_result(packages[1], "2.0", status="hold ok")
        host = FakeHost(commands={installed: installed_result, held: held_result})

        apt_install(context(host), packages)

        self.assertEqual([argv for argv, _, _ in host.runs], [installed, held])

    def test_mixed_set_installs_absent_alone_after_rehearsal(self) -> None:
        present, present_result = package_result("present-package", "1.0")
        packages = ("present-package", "absent-package")
        apt_commands = apt_command_results([packages[1]])
        host = FakeHost(commands={present: present_result, **dict(apt_commands)})

        apt_install(context(host), packages)

        absent_probe = ("dpkg-query", "-W", "-f=${Status} ${Version}\\n", packages[1])
        self.assertEqual(
            [argv for argv, _, _ in host.runs],
            [present, absent_probe, *(argv for argv, _ in apt_commands)],
        )
        for argv, _ in apt_commands:
            self.assertIn(("run", (argv, True)), host.calls)

    def test_bracketed_inst_refuses_listed_or_unlisted_package(self) -> None:
        absent = "absent-package"
        for moved in (absent, "other-package"):
            with self.subTest(moved=moved):
                commands = dict(apt_command_results([absent]))
                rehearsal = ("apt-get", "-s", "install", "-y", absent)
                commands[rehearsal] = completed(
                    rehearsal,
                    f"Inst {moved} [1.0-fictitious] (2.0-fictitious local)\n",
                )
                host = FakeHost(commands=commands)

                with self.assertRaises(StepFailure) as raised:
                    apt_install(context(host), [absent])

                self.assertIn(moved, raised.exception.detail)
                self.assertIn("1.0-fictitious", raised.exception.detail)
                self.assertIn("2.0-fictitious", raised.exception.detail)
                self.assertIn("docs/runbooks/install-upgrade.md §9", raised.exception.fix)
                self.assertNotIn(("run", (("apt-get", "install", "-y", absent), True)), host.calls)

    def test_removal_refuses_and_names_each_package(self) -> None:
        absent = "absent-package"
        commands = dict(apt_command_results([absent]))
        rehearsal = ("apt-get", "-s", "install", "-y", absent)
        commands[rehearsal] = completed(
            rehearsal,
            "Remv first-package [1.0-fictitious]\nRemv second-package [2.0-fictitious]\n",
        )
        host = FakeHost(commands=commands)

        with self.assertRaises(StepFailure) as raised:
            apt_install(context(host), [absent])

        self.assertIn("first-package", raised.exception.detail)
        self.assertIn("second-package", raised.exception.detail)
        self.assertNotIn(("run", (("apt-get", "install", "-y", absent), True)), host.calls)

    def test_new_install_lines_pass(self) -> None:
        absent = "absent-package"
        commands = dict(apt_command_results([absent]))
        rehearsal = ("apt-get", "-s", "install", "-y", absent)
        commands[rehearsal] = completed(
            rehearsal,
            "Inst absent-package (99.0-fictitious local)\n"
            "Inst new-dependency (99.0-fictitious local)\n"
            "Conf absent-package (99.0-fictitious local)\n",
        )
        host = FakeHost(commands=commands)

        apt_install(context(host), [absent])

        self.assertIn(("run", (("apt-get", "install", "-y", absent), True)), host.calls)


class DockerKeyringOrderTests(unittest.TestCase):
    def test_apply_fetches_keyring_before_writing_the_source_entry(self) -> None:
        gpg = os.fspath(_KEYRING)
        wget = ("wget", "-qO", f"{gpg}.partial", _KEY_URL)
        move = ("mv", "-f", f"{gpg}.partial", gpg)
        commands: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {
            wget: completed(wget),
            move: completed(move),
        }
        docker_probe = ("docker", "--version")
        commands[docker_probe] = subprocess.CompletedProcess(
            list(docker_probe), 1, "", "not found"
        )
        commands[_JOURNALD_CAT] = docker_commands()[_JOURNALD_CAT]
        commands[_CONTAINERD_DUMP] = containerd_dump(_CONTAINERD_DEFAULT_ROOT)
        commands.update(dict(apt_command_results(_PACKAGES)))
        for argv in (
            ("systemctl", "restart", "systemd-journald"),
            ("systemctl", "enable", "--now", "containerd"),
            ("systemctl", "restart", "containerd"),
            ("systemctl", "enable", "--now", "docker"),
            ("systemctl", "restart", "docker"),
        ):
            commands[argv] = completed(argv)
        host = FakeHost(commands=commands)
        DockerEngineStep().apply(context(host))
        fetch_index = host.calls.index(("run", (wget, True)))
        write_index = next(
            index
            for index, call in enumerate(host.calls)
            if call[0] == "write_text"
            and isinstance(call[1], tuple)
            and call[1][0] == os.fspath(_SOURCE)
        )
        self.assertLess(fetch_index, write_index)


class NvidiaStepTests(unittest.TestCase):
    """Driver and toolkit outcomes over package, module, and apt-source readings."""

    def test_open_driver_from_other_packaging_at_or_above_floor_is_accepted(self) -> None:
        branch = HOST_LOCK.driver.branch
        package = "nvidia-driver-fictitious-open"
        for version in (f"{branch}.0.0-fictitious", f"{int(branch) + 1}.0.0-fictitious"):
            with self.subTest(version=version):
                host = nvidia_driver_host(
                    loaded_version=version,
                    disk_version=version,
                    packages={package: version},
                )
                result = NvidiaDriverStep().check(context(host))
                self.assertEqual(result.disposition, Disposition.CONVERGED, result.detail)
                self.assertIn(version, result.detail)
                self.assertIn(package, result.detail)
                self.assertIn("not the recipe's", result.detail)
                assert_nvidia_read_only(self, host)

    def test_loaded_module_without_modinfo_is_accepted_and_reported(self) -> None:
        version = f"{HOST_LOCK.driver.branch}.0.0-fictitious"
        host = nvidia_driver_host(loaded_version=version)
        result = NvidiaDriverStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.CONVERGED, result.detail)
        self.assertIn("no package", result.detail)
        self.assertIn("installed module's version could not be read", result.detail)
        assert_nvidia_read_only(self, host)

    def test_short_driver_refuses_before_any_write_in_check_and_apply(self) -> None:
        branch = HOST_LOCK.driver.branch
        version = f"{int(branch) - 1}.0.0-fictitious"
        package = "nvidia-driver-fictitious-open"
        host = nvidia_driver_host(
            loaded_version=version,
            disk_version=version,
            packages={package: version},
            files={str(_LEGACY_SOURCE): f"deb [signed-by={_LEGACY_KEYRING}] {CUDA_REPOSITORY}/ /\n"},
        )
        step = NvidiaDriverStep()
        checked = step.check(context(host))
        self.assertEqual(checked.disposition, Disposition.UNFIXABLE)
        self.assertIn(version, checked.detail)
        self.assertIn(branch, checked.detail)
        self.assertIn(package, checked.detail)
        self.assertIn("announced maintenance window", checked.fix)
        with self.assertRaises(StepFailure) as raised:
            step.apply(context(host))
        self.assertEqual((raised.exception.detail, raised.exception.fix), (checked.detail, checked.fix))
        assert_nvidia_read_only(self, host)
        self.assertIn(str(_LEGACY_SOURCE), host.files)

    def test_closed_loaded_or_installed_module_refuses(self) -> None:
        version = f"{HOST_LOCK.driver.branch}.0.0-fictitious"
        package = "nvidia-driver-fictitious-open"
        for loaded, disk, loaded_closed, disk_open in (
            (version, None, True, True),
            (None, version, False, False),
        ):
            with self.subTest(loaded=loaded, disk=disk):
                host = nvidia_driver_host(
                    loaded_version=loaded,
                    disk_version=disk,
                    loaded_closed=loaded_closed,
                    disk_open=disk_open,
                    packages={package: version},
                )
                step = NvidiaDriverStep()
                checked = step.check(context(host))
                self.assertEqual(checked.disposition, Disposition.UNFIXABLE)
                self.assertIn("closed kernel modules", checked.detail)
                self.assertIn(package, checked.detail)
                self.assertIn("open kernel modules", checked.fix)
                with self.assertRaises(StepFailure) as raised:
                    step.apply(context(host))
                self.assertEqual(raised.exception.detail, checked.detail)
                assert_nvidia_read_only(self, host)

    def test_loaded_installed_module_mismatch_requires_window_reboot(self) -> None:
        branch = HOST_LOCK.driver.branch
        floor = f"{branch}.0.0-fictitious"
        older = f"{int(branch) - 1}.0.0-fictitious"
        newer = f"{int(branch) + 1}.0.0-fictitious"
        for loaded, disk, loaded_closed in (
            (floor, newer, False),
            (older, floor, False),
            (floor, floor, True),
        ):
            with self.subTest(loaded=loaded, disk=disk, loaded_closed=loaded_closed):
                host = nvidia_driver_host(
                    loaded_version=loaded,
                    disk_version=disk,
                    loaded_closed=loaded_closed,
                    packages={"nvidia-driver-fictitious-open": disk},
                )
                result = NvidiaDriverStep().check(context(host))
                self.assertEqual(result.disposition, Disposition.REBOOT_REQUIRED, result.detail)
                self.assertIn(loaded, result.detail)
                self.assertIn(disk, result.detail)
                self.assertIn("announced maintenance window", result.fix)
                if loaded_closed:
                    self.assertIn("loaded module is closed", result.detail)
                    self.assertIn("installed module is open", result.detail)
                assert_nvidia_read_only(self, host)

    def test_installed_short_module_refuses_despite_sufficient_loaded_one(self) -> None:
        branch = HOST_LOCK.driver.branch
        loaded = f"{branch}.0.0-fictitious"
        disk = f"{int(branch) - 1}.0.0-fictitious"
        host = nvidia_driver_host(loaded_version=loaded, disk_version=disk)
        step = NvidiaDriverStep()
        checked = step.check(context(host))
        self.assertEqual(checked.disposition, Disposition.UNFIXABLE)
        self.assertIn(disk, checked.detail)
        self.assertIn(branch, checked.detail)
        with self.assertRaises(StepFailure) as raised:
            step.apply(context(host))
        self.assertEqual(raised.exception.detail, checked.detail)
        assert_nvidia_read_only(self, host)

    def test_driver_package_without_module_refuses_and_names_kernel(self) -> None:
        package = "nvidia-driver-fictitious-open"
        version = f"{HOST_LOCK.driver.branch}.0.0-fictitious"
        host = nvidia_driver_host(packages={package: version})
        step = NvidiaDriverStep()
        checked = step.check(context(host))
        self.assertEqual(checked.disposition, Disposition.UNFIXABLE)
        self.assertIn(package, checked.detail)
        self.assertIn("0.0.0-fictitious", checked.detail)
        self.assertIn("dkms autoinstall", checked.fix)
        with self.assertRaises(StepFailure) as raised:
            step.apply(context(host))
        self.assertEqual(raised.exception.detail, checked.detail)
        assert_nvidia_read_only(self, host)

    def test_pinning_package_alone_resumes_driver_install(self) -> None:
        branch = HOST_LOCK.driver.branch
        pinning = f"nvidia-driver-pinning-{branch}"
        driver = HOST_LOCK.driver.package
        host = nvidia_driver_host(
            packages={pinning: "1.0-fictitious"},
            commands=nvidia_install_commands(driver),
        )
        step = NvidiaDriverStep()
        checked = step.check(context(host))
        self.assertEqual(checked.disposition, Disposition.DRIFT)
        self.assertIn("driver is not installed", checked.detail)
        step.apply(context(host))
        runs = [argv for argv, _, _ in host.runs]
        self.assertNotIn(("apt-get", "install", "-y", pinning), runs)
        self.assertIn(("apt-get", "install", "-y", driver), runs)
        self.assertIn(("apt-mark", "hold", driver), runs)

    def test_absent_driver_installs_keyring_before_packages_and_heals_legacy(self) -> None:
        branch = HOST_LOCK.driver.branch
        pinning = f"nvidia-driver-pinning-{branch}"
        driver = HOST_LOCK.driver.package
        version = f"{branch}.0.0-fictitious"
        legacy_entry = f"deb [signed-by={_LEGACY_KEYRING}] {CUDA_REPOSITORY}/ /\n"
        host = nvidia_driver_host(
            files={str(_LEGACY_SOURCE): legacy_entry},
            commands=nvidia_install_commands(pinning, driver),
        )
        step = NvidiaDriverStep()
        checked = step.check(context(host))
        self.assertEqual(checked.disposition, Disposition.DRIFT)
        self.assertIn("legacy", checked.detail)
        step.apply(context(host))
        runs = [argv for argv, _, _ in host.runs]
        deb_install = ("dpkg", "-i", "--force-confmiss", str(_DEB_TMP))
        pinning_install = ("apt-get", "install", "-y", pinning)
        driver_install = ("apt-get", "install", "-y", driver)
        self.assertNotIn(str(_LEGACY_SOURCE), host.files)
        self.assertLess(host.calls.index(("unlink", str(_LEGACY_SOURCE))), host.calls.index(("run", (deb_install, True))))
        self.assertLess(runs.index(deb_install), runs.index(pinning_install))
        self.assertLess(runs.index(pinning_install), runs.index(driver_install))
        self.assertIn(("apt-mark", "hold", driver), runs)
        self.assertEqual([call for call in host.calls if call[0] == "write_text"], [])

        converged = nvidia_driver_host(
            loaded_version=version,
            disk_version=version,
            packages={pinning: "1.0-fictitious", driver: version, "cuda-keyring": "1.0-fictitious"},
            held=True,
            files={CUDA_SOURCE: CUDA_ENTRY, str(_KEYRING_FILE): "keyring"},
        )
        host.files.update(converged.files)
        host.commands.update(converged.commands)
        self.assertEqual(step.check(context(host)).disposition, Disposition.CONVERGED)
        prior = len(host.runs)
        step.apply(context(host))
        self.assertFalse(any(argv[0] in {"apt-get", "dpkg", "wget", "mv"} for argv, _, _ in host.runs[prior:]))

    def test_installed_keyring_without_its_source_is_reinstalled_with_the_source(self) -> None:
        driver = HOST_LOCK.driver.package
        pinning = f"nvidia-driver-pinning-{HOST_LOCK.driver.branch}"
        host = nvidia_driver_host(
            packages={"cuda-keyring": "1.0-fictitious"},
            files={str(_KEYRING_FILE): "keyring"},
            commands=nvidia_install_commands(pinning, driver),
        )
        NvidiaDriverStep().apply(context(host))
        runs = [argv for argv, _, _ in host.runs]
        deb_install = ("dpkg", "-i", "--force-confmiss", str(_DEB_TMP))
        self.assertLess(runs.index(deb_install), runs.index(("apt-get", "install", "-y", driver)))

    def test_recipe_driver_repairs_hold_only_and_then_converges(self) -> None:
        driver = HOST_LOCK.driver.package
        version = f"{HOST_LOCK.driver.branch}.0.0-fictitious"
        hold = ("apt-mark", "hold", driver)
        host = nvidia_driver_host(
            loaded_version=version,
            disk_version=version,
            packages={driver: version},
            commands={hold: completed(hold)},
        )
        step = NvidiaDriverStep()
        checked = step.check(context(host))
        self.assertEqual(checked.disposition, Disposition.DRIFT)
        self.assertIn("not held", checked.detail)
        self.assertIn("apt-mark", checked.fix)
        step.apply(context(host))
        self.assertIn(("run", (hold, True)), host.calls)
        self.assertFalse(any(argv[0] in {"apt-get", "dpkg", "wget"} for argv, _, _ in host.runs))
        host.commands[("apt-mark", "showhold")] = completed(("apt-mark", "showhold"), f"{driver}\n")
        self.assertEqual(step.check(context(host)).disposition, Disposition.CONVERGED)
        prior = len(host.runs)
        step.apply(context(host))
        self.assertNotIn(hold, [argv for argv, _, _ in host.runs[prior:]])

    def test_two_cuda_entries_refuse_both_steps_before_any_write(self) -> None:
        branch = HOST_LOCK.driver.branch
        version = f"{branch}.0.0-fictitious"
        other_path = "/etc/apt/sources.list.d/fictitious-second-cuda.list"
        sources = {
            CUDA_SOURCE: CUDA_ENTRY,
            other_path: f"deb [signed-by=/keys/fictitious.gpg] {CUDA_REPOSITORY}/ /\n",
        }
        for loaded in (None, version):
            for step in (NvidiaDriverStep(), NvidiaToolkitStep()):
                with self.subTest(loaded=loaded, step=step.name):
                    host = nvidia_driver_host(
                        loaded_version=loaded,
                        disk_version=loaded,
                        files=sources,
                    )
                    checked = step.check(context(host))
                    self.assertEqual(checked.disposition, Disposition.UNFIXABLE)
                    self.assertIn("2 apt sources", checked.detail)
                    self.assertIn(CUDA_SOURCE, checked.detail)
                    self.assertIn(other_path, checked.detail)
                    self.assertIn("Keep one apt source", checked.fix)
                    with self.assertRaises(StepFailure) as raised:
                        step.apply(context(host))
                    self.assertEqual(raised.exception.detail, checked.detail)
                    assert_nvidia_read_only(self, host)

    def test_unreadable_cuda_sources_refuse_both_steps_before_drift(self) -> None:
        source_dir = "/etc/apt/sources.list.d"
        for step in (NvidiaDriverStep(), NvidiaToolkitStep()):
            with self.subTest(step=step.name):
                host = PermissionErrorHost(source_dir)
                checked = step.check(context(host))
                self.assertEqual(checked.disposition, Disposition.UNFIXABLE)
                self.assertIn(source_dir, checked.detail)
                self.assertIn("Repair", checked.fix)
                with self.assertRaises(StepFailure) as raised:
                    step.apply(context(host))
                self.assertEqual(raised.exception.detail, checked.detail)
                assert_nvidia_read_only(self, host)

    def test_foreign_source_refuses_only_when_recipe_must_install(self) -> None:
        version = f"{HOST_LOCK.driver.branch}.0.0-fictitious"
        foreign_path = "/etc/apt/sources.list.d/fictitious-cuda.list"
        sources = {foreign_path: f"deb [signed-by=/keys/fictitious.gpg] {CUDA_REPOSITORY}/ /\n"}
        for step, loaded, packages, named_package in (
            (NvidiaDriverStep(), None, {}, HOST_LOCK.driver.package),
            (NvidiaToolkitStep(), version, {}, TOOLKIT_PACKAGE),
        ):
            with self.subTest(absent=step.name):
                host = nvidia_driver_host(loaded_version=loaded, disk_version=loaded, packages=packages, files=sources)
                checked = step.check(context(host))
                self.assertEqual(checked.disposition, Disposition.UNFIXABLE)
                self.assertIn(foreign_path, checked.detail)
                self.assertIn(named_package, checked.fix)
                with self.assertRaises(StepFailure) as raised:
                    step.apply(context(host))
                self.assertEqual(raised.exception.detail, checked.detail)
                assert_nvidia_read_only(self, host)

        driver = nvidia_driver_host(loaded_version=version, disk_version=version, files=sources)
        driver_result = NvidiaDriverStep().check(context(driver))
        self.assertEqual(driver_result.disposition, Disposition.CONVERGED)
        self.assertIn(foreign_path, driver_result.detail)
        assert_nvidia_read_only(self, driver)

        enabled = ("systemctl", "is-enabled", "nvidia-persistenced")
        policy = _TOOLKIT_POLICY
        toolkit = nvidia_driver_host(
            loaded_version=version,
            disk_version=version,
            packages={TOOLKIT_PACKAGE: HOST_LOCK.minimums.toolkit},
            files={**sources, "/etc/cdi/nvidia.yaml": "spec"},
            commands={enabled: completed(enabled, "enabled\n"), policy: completed(policy)},
        )
        toolkit_result = NvidiaToolkitStep().check(context(toolkit))
        self.assertEqual(toolkit_result.disposition, Disposition.CONVERGED)
        self.assertIn(foreign_path, toolkit_result.detail)
        assert_nvidia_read_only(self, toolkit)

    def test_recipe_path_with_foreign_entry_is_reported_or_refused(self) -> None:
        version = f"{HOST_LOCK.driver.branch}.0.0-fictitious"
        foreign = f"deb [signed-by=/keys/fictitious.gpg] {CUDA_REPOSITORY}/ /\n"
        present = nvidia_driver_host(loaded_version=version, disk_version=version, files={CUDA_SOURCE: foreign})
        result = NvidiaDriverStep().check(context(present))
        self.assertEqual(result.disposition, Disposition.CONVERGED)
        self.assertIn(f"source {CUDA_SOURCE} is not the recipe's entry", result.detail)
        absent = nvidia_driver_host(files={CUDA_SOURCE: foreign})
        checked = NvidiaDriverStep().check(context(absent))
        self.assertEqual(checked.disposition, Disposition.UNFIXABLE)
        self.assertIn(CUDA_SOURCE, checked.detail)
        with self.assertRaises(StepFailure):
            NvidiaDriverStep().apply(context(absent))
        assert_nvidia_read_only(self, absent)

    def test_legacy_entry_beside_recipe_is_healed_without_install(self) -> None:
        version = f"{HOST_LOCK.driver.branch}.0.0-fictitious"
        legacy = f"deb [signed-by={_LEGACY_KEYRING}] {CUDA_REPOSITORY}/ /\n"
        host = nvidia_driver_host(
            loaded_version=version,
            disk_version=version,
            packages={HOST_LOCK.driver.package: version},
            held=True,
            files={CUDA_SOURCE: CUDA_ENTRY, str(_LEGACY_SOURCE): legacy},
        )
        step = NvidiaDriverStep()
        checked = step.check(context(host))
        self.assertEqual(checked.disposition, Disposition.DRIFT)
        self.assertIn("legacy", checked.detail)
        step.apply(context(host))
        self.assertNotIn(str(_LEGACY_SOURCE), host.files)
        self.assertEqual(step.check(context(host)).disposition, Disposition.CONVERGED)
        self.assertFalse(any(argv[0] in {"apt-get", "dpkg", "wget"} for argv, _, _ in host.runs))

    def test_occupied_recipe_path_refuses_only_an_install(self) -> None:
        version = f"{HOST_LOCK.driver.branch}.0.0-fictitious"
        occupied = {CUDA_SOURCE: "deb https://other.example.test/ubuntu stable main\n"}
        for step, loaded in ((NvidiaDriverStep(), None), (NvidiaToolkitStep(), version)):
            with self.subTest(step=step.name):
                host = nvidia_driver_host(loaded_version=loaded, disk_version=loaded, files=occupied)
                checked = step.check(context(host))
                self.assertEqual(checked.disposition, Disposition.UNFIXABLE)
                self.assertIn(CUDA_SOURCE, checked.detail)
                self.assertIn("no enabled entry", checked.detail)
                with self.assertRaises(StepFailure) as raised:
                    step.apply(context(host))
                self.assertEqual(raised.exception.detail, checked.detail)
                assert_nvidia_read_only(self, host)
        present = nvidia_driver_host(loaded_version=version, disk_version=version, files=occupied)
        self.assertEqual(NvidiaDriverStep().check(context(present)).disposition, Disposition.CONVERGED)

    def test_toolkit_waits_for_a_loaded_driver(self) -> None:
        host = nvidia_driver_host()
        result = NvidiaToolkitStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.PENDING_INPUT)
        self.assertIn("driver must be loaded", result.detail)
        self.assertIn("docs/runbooks/install-upgrade.md §1", result.fix)
        assert_nvidia_read_only(self, host)

    def test_absent_toolkit_installs_keyring_before_package(self) -> None:
        version = f"{HOST_LOCK.driver.branch}.0.0-fictitious"
        enable = ("systemctl", "enable", "--now", "nvidia-persistenced")
        generate = ("nvidia-ctk", "cdi", "generate", "--output=/etc/cdi/nvidia.yaml")
        commands = nvidia_install_commands(TOOLKIT_PACKAGE)
        commands.update({enable: completed(enable), generate: completed(generate)})
        host = nvidia_driver_host(loaded_version=version, disk_version=version, commands=commands)
        step = NvidiaToolkitStep()
        checked = step.check(context(host))
        self.assertEqual(checked.disposition, Disposition.DRIFT)
        self.assertIn(TOOLKIT_PACKAGE, checked.detail)
        step.apply(context(host))
        runs = [argv for argv, _, _ in host.runs]
        deb_install = ("dpkg", "-i", "--force-confmiss", str(_DEB_TMP))
        toolkit_install = ("apt-get", "install", "-y", TOOLKIT_PACKAGE)
        self.assertLess(runs.index(deb_install), runs.index(("apt-get", "update")))
        self.assertLess(runs.index(("apt-get", "-s", "install", "-y", TOOLKIT_PACKAGE)), runs.index(toolkit_install))
        self.assertLess(runs.index(deb_install), runs.index(toolkit_install))
        self.assertIn(("run", (generate, True)), host.calls)

    def test_short_toolkit_refuses_before_any_write(self) -> None:
        floor = HOST_LOCK.minimums.toolkit
        version = f"{int(floor.split('.')[0]) - 1}.0.0-fictitious"
        loaded = f"{HOST_LOCK.driver.branch}.0.0-fictitious"
        host = nvidia_driver_host(
            loaded_version=loaded,
            disk_version=loaded,
            packages={TOOLKIT_PACKAGE: version},
        )
        step = NvidiaToolkitStep()
        checked = step.check(context(host))
        self.assertEqual(checked.disposition, Disposition.UNFIXABLE)
        self.assertIn(version, checked.detail)
        self.assertIn(floor, checked.detail)
        self.assertIn(f"Upgrade {TOOLKIT_PACKAGE} to at least {floor}", checked.fix)
        with self.assertRaises(StepFailure) as raised:
            step.apply(context(host))
        self.assertEqual((raised.exception.detail, raised.exception.fix), (checked.detail, checked.fix))
        assert_nvidia_read_only(self, host)

    def test_toolkit_service_and_cdi_drift_then_converge(self) -> None:
        version = f"{HOST_LOCK.driver.branch}.0.0-fictitious"
        enabled = ("systemctl", "is-enabled", "nvidia-persistenced")
        host = nvidia_driver_host(
            loaded_version=version,
            disk_version=version,
            packages={TOOLKIT_PACKAGE: HOST_LOCK.minimums.toolkit},
        )
        step = NvidiaToolkitStep()
        service = step.check(context(host))
        self.assertEqual(service.disposition, Disposition.DRIFT)
        self.assertIn("not enabled", service.detail)
        host.commands[enabled] = completed(enabled, "enabled\n")
        cdi = step.check(context(host))
        self.assertEqual(cdi.disposition, Disposition.DRIFT)
        self.assertIn("CDI specification is missing", cdi.detail)
        host.files["/etc/cdi/nvidia.yaml"] = "fictitious spec"
        host.commands[_TOOLKIT_POLICY] = completed(_TOOLKIT_POLICY)
        converged = step.check(context(host))
        self.assertEqual(converged.disposition, Disposition.CONVERGED)
        self.assertIn("toolkit and CDI are current", converged.detail)
        assert_nvidia_read_only(self, host)

    def test_toolkit_reports_distinct_other_policy_repositories(self) -> None:
        version = f"{HOST_LOCK.driver.branch}.0.0-fictitious"
        floor = HOST_LOCK.minimums.toolkit
        first = "https://a.example.test/cuda"
        second = "https://b.example.test/cuda"
        policy_text = (
            f"{TOOLKIT_PACKAGE}:\n  Installed: {floor}\n  Candidate: {floor}\n"
            f"  Version table:\n *** {floor} 600\n"
            f"        600 {CUDA_REPOSITORY}/  Packages\n"
            "        100 /var/lib/dpkg/status\n"
            f"        500 {second} stable/main amd64 Packages\n"
            f"        500 {first} stable/main amd64 Packages\n"
            f"        500 {second} stable/main amd64 Packages\n"
        )
        enabled = ("systemctl", "is-enabled", "nvidia-persistenced")
        host = nvidia_driver_host(
            loaded_version=version,
            disk_version=version,
            packages={TOOLKIT_PACKAGE: floor},
            files={"/etc/cdi/nvidia.yaml": "fictitious spec", CUDA_SOURCE: CUDA_ENTRY},
            commands={enabled: completed(enabled, "enabled\n"), _TOOLKIT_POLICY: completed(_TOOLKIT_POLICY, policy_text)},
        )
        result = NvidiaToolkitStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.CONVERGED)
        self.assertIn(f"2 other apt repositories: {first}, {second}", result.detail)
        self.assertNotIn("/var/lib/dpkg/status", result.detail)
        self.assertNotIn(CUDA_REPOSITORY, result.detail)
        assert_nvidia_read_only(self, host)

    def test_toolkit_policy_failure_is_reported_without_refusal(self) -> None:
        version = f"{HOST_LOCK.driver.branch}.0.0-fictitious"
        enabled = ("systemctl", "is-enabled", "nvidia-persistenced")
        failed = subprocess.CompletedProcess(list(_TOOLKIT_POLICY), 1, "", "fictitious policy failure\nsecond line\n")
        host = nvidia_driver_host(
            loaded_version=version,
            disk_version=version,
            packages={TOOLKIT_PACKAGE: HOST_LOCK.minimums.toolkit},
            files={"/etc/cdi/nvidia.yaml": "fictitious spec"},
            commands={enabled: completed(enabled, "enabled\n"), _TOOLKIT_POLICY: failed},
        )
        result = NvidiaToolkitStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.CONVERGED)
        self.assertIn("apt-cache policy could not be read: fictitious policy failure", result.detail)
        self.assertNotIn("second line", result.detail)
        assert_nvidia_read_only(self, host)

    def test_toolkit_rehearsal_refuses_upgrade_of_present_base(self) -> None:
        loaded = f"{HOST_LOCK.driver.branch}.0.0-fictitious"
        base = "nvidia-container-toolkit-base"
        base_version = "1.0-fictitious"
        base_query, base_result = package_result(base, base_version)
        rehearsal = ("apt-get", "-s", "install", "-y", TOOLKIT_PACKAGE)
        commands = nvidia_install_commands(TOOLKIT_PACKAGE)
        commands[base_query] = base_result
        commands[rehearsal] = completed(
            rehearsal,
            f"Inst {base} [{base_version}] (2.0-fictitious local)\n",
        )
        host = nvidia_driver_host(
            loaded_version=loaded,
            disk_version=loaded,
            packages={"cuda-keyring": "1.0-fictitious", base: base_version},
            files={CUDA_SOURCE: CUDA_ENTRY, str(_KEYRING_FILE): "keyring"},
            commands=commands,
        )
        with self.assertRaises(StepFailure) as raised:
            NvidiaToolkitStep().apply(context(host))
        self.assertIn(base, raised.exception.detail)
        self.assertIn(base_version, raised.exception.detail)
        self.assertIn("docs/runbooks/install-upgrade.md §9", raised.exception.fix)
        self.assertNotIn(("apt-get", "install", "-y", TOOLKIT_PACKAGE), [argv for argv, _, _ in host.runs])


def containerd_dump(root: object) -> subprocess.CompletedProcess[str]:
    """containerd's configuration dump, its first lines, reporting *root* in effect."""

    return completed(
        _CONTAINERD_DUMP,
        f"version = 4\nroot = '{root}'\nimports = ['/etc/containerd/conf.d/*.toml']\n",
    )


def docker_commands(
    *,
    docker_version: str | None = None,
    compose_version: str | None = None,
    containerd_enabled: bool = True,
    containerd_active: bool = True,
    containerd_verify_stdout: str | None = None,
    containerd_verify_returncode: int = 0,
    journald_cat_stdout: str | None = None,
    journald_cat_returncode: int = 0,
    containerd_root: object = _CONTAINERD_ROOT,
) -> dict[tuple[str, ...], subprocess.CompletedProcess[str]]:
    if docker_version is None:
        docker_version = f"{HOST_LOCK.minimums.docker}.0.0"
    if compose_version is None:
        compose_version = f"{HOST_LOCK.minimums.compose}.0.0"
    if containerd_verify_stdout is None:
        containerd_verify_stdout = f"??5?????? c {_CONTAINERD_CONFIG}\n"
    if journald_cat_stdout is None:
        journald_cat_stdout = (
            "# /etc/systemd/journald.conf\n[Journal]\n"
            "# Storage=auto\n# SystemMaxUse=\n# MaxRetentionSec=\n"
            f"# {_JOURNALD}\n{_JOURNALD_TEXT}"
            "# /usr/lib/systemd/journald.conf.d/syslog.conf\n[Journal]\n"
            "ForwardToSyslog=no\n"
        )
    containerd_enabled_command = ("systemctl", "is-enabled", "containerd")
    containerd_active_command = ("systemctl", "is-active", "containerd")
    commands = {
        ("docker", "--version"): completed(("docker", "--version"), f"Docker version {docker_version}, build abc\n"),
        ("docker", "compose", "version"): completed(("docker", "compose", "version"), f"Docker Compose version v{compose_version}\n"),
        containerd_enabled_command: subprocess.CompletedProcess(
            list(containerd_enabled_command),
            0 if containerd_enabled else 1,
            "enabled\n" if containerd_enabled else "disabled\n",
            "",
        ),
        containerd_active_command: subprocess.CompletedProcess(
            list(containerd_active_command),
            0 if containerd_active else 1,
            "active\n" if containerd_active else "inactive\n",
            "",
        ),
        ("systemctl", "is-enabled", "docker"): completed(("systemctl", "is-enabled", "docker"), "enabled\n"),
        ("systemctl", "is-active", "docker"): completed(("systemctl", "is-active", "docker"), "active\n"),
        ("systemctl", "enable", "--now", "containerd"): completed(("systemctl", "enable", "--now", "containerd")),
        ("systemctl", "restart", "containerd"): completed(("systemctl", "restart", "containerd")),
        ("systemctl", "enable", "--now", "docker"): completed(("systemctl", "enable", "--now", "docker")),
        ("systemctl", "restart", "docker"): completed(("systemctl", "restart", "docker")),
        _CONTAINERD_VERIFY: subprocess.CompletedProcess(
            list(_CONTAINERD_VERIFY), containerd_verify_returncode,
            containerd_verify_stdout, "",
        ),
        _JOURNALD_CAT: subprocess.CompletedProcess(
            list(_JOURNALD_CAT), journald_cat_returncode, journald_cat_stdout, "",
        ),
        _CONTAINERD_DUMP: containerd_dump(containerd_root),
    }
    return commands


_OLD_DOCKER_KEYRING = "/etc/apt/keyrings/gideon-docker.asc"
_OLD_DOCKER_SOURCE = "/etc/apt/sources.list.d/gideon-docker.list"


def docker_policy() -> dict[str, object]:
    return {
        "data-root": "/var/lib/docker",
        "features": {"cdi": True},
        "log-driver": "journald",
    }


def docker_files(daemon: Mapping[str, object], *, journald: str = "[Journal]\nStorage=persistent\nSystemMaxUse=50G\nMaxRetentionSec=90day\n") -> dict[str, str]:
    return {
        _OLD_DOCKER_KEYRING: "key",
        _OLD_DOCKER_SOURCE: (
            f"deb [arch={_ARCHITECTURE} signed-by={_OLD_DOCKER_KEYRING}] "
            f"{_REPOSITORY} {_SUITE} {_COMPONENT}\n"
        ),
        "/etc/docker/daemon.json": json.dumps(daemon, indent=2),
        os.fspath(_CONTAINERD_CONFIG): _CONTAINERD_TEXT,
        "/etc/systemd/journald.conf.d/gideon.conf": journald,
    }


def docker_recipe_files(daemon: Mapping[str, object]) -> dict[str, str]:
    files = docker_files(daemon)
    del files[_OLD_DOCKER_KEYRING]
    del files[_OLD_DOCKER_SOURCE]
    files[os.fspath(_KEYRING)] = "key"
    files[os.fspath(_SOURCE)] = _REPO
    return files


def docker_package_commands(*, absent: Sequence[str] = ()) -> dict[tuple[str, ...], subprocess.CompletedProcess[str]]:
    commands: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {}
    for package in _PACKAGES:
        if package not in absent:
            probe, result = package_result(package, "99.0-fictitious")
            commands[probe] = result
    return commands


def docker_absent_commands() -> dict[tuple[str, ...], subprocess.CompletedProcess[str]]:
    commands = docker_commands()
    probe = ("docker", "--version")
    commands[probe] = subprocess.CompletedProcess(list(probe), 1, "", "not found")
    commands.update(dict(apt_command_results(_PACKAGES)))
    gpg = os.fspath(_KEYRING)
    wget = ("wget", "-qO", f"{gpg}.partial", _KEY_URL)
    move = ("mv", "-f", f"{gpg}.partial", gpg)
    commands[wget] = completed(wget)
    commands[move] = completed(move)
    return commands


class DockerStepTests(unittest.TestCase):
    def setting_host(
        self,
        *,
        files: dict[str, str] | None = None,
        commands: dict[tuple[str, ...], subprocess.CompletedProcess[str]] | None = None,
    ) -> FakeHost:
        return FakeHost(
            files=docker_files(docker_policy()) if files is None else files,
            commands={
                **docker_commands(),
                **docker_package_commands(),
                ("systemctl", "restart", "systemd-journald"):
                    completed(("systemctl", "restart", "systemd-journald")),
                **(commands or {}),
            },
        )

    def assert_setting_refusal(self, host: FakeHost, *details: str) -> CheckResult:
        step = DockerEngineStep()
        checked = step.check(context(host))
        self.assertEqual(checked.disposition, Disposition.UNFIXABLE, checked)
        for detail in details:
            self.assertIn(detail, checked.detail)
        host.calls.clear()
        with self.assertRaises(StepFailure) as raised:
            step.apply(context(host))
        self.assertEqual(
            (raised.exception.detail, raised.exception.fix),
            (checked.detail, checked.fix),
        )
        assert_no_host_mutation(
            self,
            host,
            {("docker", "--version"), ("docker", "compose", "version"),
             _CONTAINERD_VERIFY, _CONTAINERD_DUMP, _JOURNALD_CAT},
        )
        return checked

    def recorded_journald(self) -> str:
        return baseline_host().commands[_JOURNALD_CAT].stdout

    def test_insecure_registries_follows_the_plain_registry_rule(self) -> None:
        """A plain, non-loopback site registry at the VM bridge address is listed
        under insecure-registries; loopback is implicit in Docker and a hostname
        registry is TLS, so neither gets the key."""

        base_daemon: dict[str, object] = {
            "data-root": "/var/lib/docker",
            "features": {"cdi": True},
            "log-driver": "journald",
        }
        cases = (
            ("127.0.0.1:5000", base_daemon),
            ("192.168.122.1:5000", {**base_daemon, "insecure-registries": ["192.168.122.1:5000"]}),
            ("registry.example:5000", base_daemon),
        )
        for authority, desired in cases:
            with self.subTest(authority=authority):
                base_site = context(FakeHost()).site
                assert base_site is not None
                site = replace(base_site, registry=authority)
                host = FakeHost(files=docker_files(desired), commands=docker_commands())
                checked = DockerEngineStep().check(context(host, site=site))
                self.assertEqual(checked.disposition, Disposition.CONVERGED)
                self.assertIn(
                    f"Docker's apt source is {_OLD_DOCKER_SOURCE}, not the recipe's {_SOURCE}",
                    checked.detail,
                )
                if desired is not base_daemon:
                    without = FakeHost(files=docker_files(base_daemon), commands=docker_commands())
                    result = DockerEngineStep().check(context(without, site=site))
                    self.assertEqual(result.disposition, Disposition.DRIFT)
                    self.assertEqual(
                        result.detail,
                        f"{_DAEMON} lacks GIDEON's keys: insecure-registries",
                    )

    def test_present_packages_and_daemon_drift_run_no_apt_command(self) -> None:
        packages = (
            "docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin",
            "docker-compose-plugin",
        )
        commands = docker_commands(
            docker_version=f"{HOST_LOCK.minimums.docker}.0.0",
            compose_version=f"{HOST_LOCK.minimums.compose}.0.0",
        )
        probes = []
        for package in packages:
            probe, result = package_result(package, "99.0-fictitious")
            commands[probe] = result
            probes.append(probe)
        host = FakeHost(
            files=docker_files({
                "data-root": "/var/lib/docker",
                "features": {"cdi": True},
                "log-driver": "json-file",
            }),
            commands=commands,
        )
        step = DockerEngineStep()

        checked = step.check(context(host))
        self.assertEqual(checked.disposition, Disposition.DRIFT)
        self.assertEqual(
            checked.detail,
            f"{_DAEMON} lacks GIDEON's keys: log-driver",
        )
        step.apply(context(host))

        runs = [argv for argv, _, _ in host.runs]
        self.assertEqual([argv for argv in runs if argv[0] == "dpkg-query"], probes)
        self.assertFalse(any(argv[0] == "apt-get" for argv in runs))

    def test_below_floor_docker_and_compose_refuse_before_any_write(self) -> None:
        for package, short_version in (
            ("docker-ce", f"{HOST_LOCK.minimums.docker - 1}.0.0"),
            ("docker-compose-plugin", f"{HOST_LOCK.minimums.compose - 1}.0.0"),
        ):
            with self.subTest(package=package):
                floor = (
                    HOST_LOCK.minimums.docker if package == "docker-ce"
                    else HOST_LOCK.minimums.compose
                )
                commands = docker_commands(
                    docker_version=(
                        short_version if package == "docker-ce"
                        else f"{HOST_LOCK.minimums.docker}.0.0"
                    ),
                    compose_version=(
                        short_version if package == "docker-compose-plugin"
                        else f"{HOST_LOCK.minimums.compose}.0.0"
                    ),
                )
                files = docker_files({"data-root": "/var/lib/docker"})
                step = DockerEngineStep()
                present_source = step.check(context(FakeHost(files=files, commands=commands)))
                self.assertEqual(present_source.disposition, Disposition.UNFIXABLE)
                del files[_OLD_DOCKER_SOURCE]
                host = FakeHost(files=files, commands=commands)
                expected = f"{package} is at {floor - 1}.0, below the floor {floor}"

                checked = step.check(context(host))
                self.assertEqual(checked.disposition, Disposition.UNFIXABLE)
                self.assertEqual(checked.detail, expected)
                self.assertEqual(present_source.detail, expected)
                self.assertIn(f"Upgrade {package} to at least {floor}", checked.fix)
                self.assertIn("docs/runbooks/install-upgrade.md §9", checked.fix)

                before_apply = len(host.calls)
                with self.assertRaises(StepFailure) as raised:
                    step.apply(context(host))
                self.assertEqual(raised.exception.detail, checked.detail)
                self.assertEqual(raised.exception.fix, checked.fix)
                apply_calls = host.calls[before_apply:]
                self.assertFalse(any(
                    method in {"write_text", "mkdir", "unlink"}
                    for method, _ in apply_calls
                ))
                for method, arguments in apply_calls:
                    if method == "run":
                        assert isinstance(arguments, tuple)
                        self.assertIn(arguments[0], {
                            ("docker", "--version"), ("docker", "compose", "version"),
                        })

    def test_absent_or_unparsed_docker_or_compose_version_is_drift(self) -> None:
        for probe, detail, fix_text in (
            (("docker", "--version"), "Docker is not installed", "Docker engine"),
            (
                ("docker", "compose", "version"),
                "the Docker Compose plugin is not installed",
                "Docker Compose plugin",
            ),
        ):
            for output in (
                subprocess.CompletedProcess(list(probe), 1, "", "not found"),
                completed(probe, "version unavailable\n"),
            ):
                with self.subTest(probe=probe, output=output):
                    commands = docker_commands(
                        docker_version=f"{HOST_LOCK.minimums.docker}.0.0",
                        compose_version=f"{HOST_LOCK.minimums.compose}.0.0",
                    )
                    commands[probe] = output
                    daemon = {
                        "data-root": "/var/lib/docker",
                        "features": {"cdi": True},
                        "log-driver": "journald",
                    }
                    files = (
                        docker_recipe_files(daemon)
                        if probe == ("docker", "--version")
                        else docker_files(daemon)
                    )
                    host = FakeHost(files=files, commands=commands)

                    checked = DockerEngineStep().check(context(host))

                    self.assertEqual(checked.disposition, Disposition.DRIFT)
                    self.assertEqual(checked.detail, detail)
                    self.assertIn(f"Install the locked {fix_text}", checked.fix)
                    self.assertEqual(host.runs[0][0], ("docker", "--version"))

    def test_containerd_config_text_is_exact(self) -> None:
        self.assertEqual(
            _CONTAINERD_TEXT,
            "version = 4\n"
            "root = '/var/lib/docker/containerd'\n"
            "disabled_plugins = ['io.containerd.grpc.v1.cri']\n",
        )

    def test_containerd_config_missing_and_different_are_drift(self) -> None:
        daemon = {
            "data-root": "/var/lib/docker",
            "features": {"cdi": True},
            "log-driver": "journald",
        }
        missing_files = docker_files(daemon)
        del missing_files[os.fspath(_CONTAINERD_CONFIG)]
        missing = DockerEngineStep().check(
            context(FakeHost(
                files=missing_files,
                commands=docker_commands(containerd_root=_CONTAINERD_DEFAULT_ROOT),
            ))
        )
        self.assertEqual(missing.disposition, Disposition.DRIFT)
        self.assertIn("config.toml is at the package default", missing.detail)
        self.assertIn("Run provision", missing.fix)

        different_files = docker_files(daemon)
        different_files[os.fspath(_CONTAINERD_CONFIG)] = "wrong\n"
        different = DockerEngineStep().check(
            context(FakeHost(
                files=different_files,
                commands=docker_commands(
                    containerd_verify_stdout="", containerd_root=_CONTAINERD_DEFAULT_ROOT
                ),
            ))
        )
        self.assertEqual(different.disposition, Disposition.DRIFT)
        self.assertIn("config.toml is at the package default", different.detail)
        self.assertIn("Run provision", different.fix)

    def test_populated_default_store_with_empty_desired_store_is_unfixable(self) -> None:
        daemon = {
            "data-root": "/var/lib/docker",
            "features": {"cdi": True},
            "log-driver": "journald",
        }
        snapshot = _CONTAINERD_DEFAULT_ROOT / _CONTAINERD_STORE_PATHS[0] / "snapshot-1"
        host = FakeHost(
            files={**docker_files(daemon), os.fspath(snapshot): ""},
            commands=docker_commands(),
        )
        result = DockerEngineStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.UNFIXABLE)
        self.assertIn(os.fspath(_CONTAINERD_DEFAULT_ROOT), result.detail)
        self.assertIn(os.fspath(_CONTAINERD_ROOT), result.detail)
        self.assertIn("docs/runbooks/install-upgrade.md §7", result.fix)

    def test_store_is_converged_when_both_roots_are_populated(self) -> None:
        daemon = {
            "data-root": "/var/lib/docker",
            "features": {"cdi": True},
            "log-driver": "journald",
        }
        default_snapshot = _CONTAINERD_DEFAULT_ROOT / _CONTAINERD_STORE_PATHS[0] / "snapshot-1"
        desired_blob = _CONTAINERD_ROOT / _CONTAINERD_STORE_PATHS[1] / "blob-1"
        host = FakeHost(
            files={
                **docker_files(daemon),
                os.fspath(default_snapshot): "",
                os.fspath(desired_blob): "",
            },
            commands=docker_commands(),
        )
        result = DockerEngineStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.CONVERGED)
        self.assertIn(
            f"Docker's apt source is {_OLD_DOCKER_SOURCE}, not the recipe's {_SOURCE}",
            result.detail,
        )

    def test_empty_store_is_converged_on_a_fresh_host(self) -> None:
        daemon = {
            "data-root": "/var/lib/docker",
            "features": {"cdi": True},
            "log-driver": "journald",
        }
        host = FakeHost(files=docker_files(daemon), commands=docker_commands())
        result = DockerEngineStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.CONVERGED)
        self.assertIn(
            f"Docker's apt source is {_OLD_DOCKER_SOURCE}, not the recipe's {_SOURCE}",
            result.detail,
        )

    def test_unlistable_store_directory_is_unfixable_and_names_path(self) -> None:
        daemon = {
            "data-root": "/var/lib/docker",
            "features": {"cdi": True},
            "log-driver": "journald",
        }
        blocked = _CONTAINERD_DEFAULT_ROOT / _CONTAINERD_STORE_PATHS[0]
        host = PermissionErrorHost(
            blocked,
            files=docker_files(daemon),
            commands=docker_commands(),
        )
        result = DockerEngineStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.UNFIXABLE)
        self.assertIn(os.fspath(blocked), result.detail)

        call_count = len(host.calls)
        with self.assertRaises(StepFailure) as raised:
            DockerEngineStep().apply(context(host))
        self.assertIn(os.fspath(blocked), raised.exception.detail)
        self.assertEqual(
            [call for call in host.calls[call_count:] if call[0] in {"write_text", "run"}],
            [],
        )

    def test_populated_store_apply_refuses_before_mutating(self) -> None:
        daemon = {
            "data-root": "/var/lib/docker",
            "features": {"cdi": True},
            "log-driver": "journald",
        }
        snapshot = _CONTAINERD_DEFAULT_ROOT / _CONTAINERD_STORE_PATHS[0] / "snapshot-1"
        host = FakeHost(
            files={**docker_files(daemon), os.fspath(snapshot): ""},
            commands=docker_commands(),
        )
        with self.assertRaises(StepFailure) as raised:
            DockerEngineStep().apply(context(host))
        self.assertIn(os.fspath(_CONTAINERD_DEFAULT_ROOT), raised.exception.detail)
        self.assertEqual(
            [call for call in host.calls if call[0] in {"write_text", "run"}],
            [],
        )

    def test_containerd_change_restarts_containerd_before_enabling_docker(self) -> None:
        daemon = {
            "data-root": "/var/lib/docker",
            "features": {"cdi": True},
            "log-driver": "journald",
        }
        commands = docker_commands(
            containerd_verify_stdout="", containerd_root=_CONTAINERD_DEFAULT_ROOT
        )
        commands.update(dict(apt_command_results((
            "docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin",
            "docker-compose-plugin",
        ))))
        files = docker_files(daemon)
        files[os.fspath(_CONTAINERD_CONFIG)] = "wrong\n"
        host = FakeHost(files=files, commands=commands)
        DockerEngineStep().apply(context(host))

        systemctl_calls: list[tuple[str, ...]] = []
        for method, arguments in host.calls:
            if method != "run" or not isinstance(arguments, tuple):
                continue
            command = arguments[0]
            if (
                isinstance(command, tuple)
                and command[0] == "systemctl"
                and command[1] in {"enable", "restart"}
            ):
                systemctl_calls.append(command)
        self.assertEqual(
            systemctl_calls,
            [
                ("systemctl", "enable", "--now", "containerd"),
                ("systemctl", "restart", "containerd"),
                ("systemctl", "enable", "--now", "docker"),
                ("systemctl", "restart", "docker"),
            ],
        )

    def test_disabled_containerd_drifts_and_apply_enables_without_restart(self) -> None:
        daemon = {
            "data-root": "/var/lib/docker",
            "features": {"cdi": True},
            "log-driver": "journald",
        }
        commands = docker_commands(containerd_enabled=False)
        host = FakeHost(files=docker_files(daemon), commands=commands)
        result = DockerEngineStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertIn("containerd", result.detail)

        enable = ("systemctl", "enable", "--now", "containerd")
        host.commands[enable] = completed(enable)
        host.commands.update(dict(apt_command_results((
            "docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin",
            "docker-compose-plugin",
        ))))
        DockerEngineStep().apply(context(host))
        self.assertIn(("run", (enable, True)), host.calls)
        self.assertNotIn(
            ("run", (("systemctl", "restart", "containerd"), True)),
            host.calls,
        )

    def test_daemon_json_is_compared_semantically_and_proxy_is_site_driven(self) -> None:
        daemon = {
            "log-driver": "journald",
            "features": {"cdi": True},
            "data-root": "/var/lib/docker",
        }
        host = FakeHost(files=docker_files(daemon), commands=docker_commands())
        checked = DockerEngineStep().check(context(host))
        self.assertEqual(checked.disposition, Disposition.CONVERGED)
        self.assertIn(
            f"Docker's apt source is {_OLD_DOCKER_SOURCE}, not the recipe's {_SOURCE}",
            checked.detail,
        )

        site = context(host).site
        assert site is not None
        proxied = replace(site, egress_proxy="http://proxy.example:3128")
        result = DockerEngineStep().check(context(host, site=proxied))
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertEqual(
            result.detail,
            f"{_DAEMON} lacks GIDEON's keys: proxies.http-proxy, proxies.https-proxy",
        )

    def test_journald_change_restarts_journald(self) -> None:
        daemon = {
            "data-root": "/var/lib/docker",
            "features": {"cdi": True},
            "log-driver": "journald",
        }
        restart = ("systemctl", "restart", "systemd-journald")
        enable = ("systemctl", "enable", "--now", "docker")
        commands = docker_commands(
            journald_cat_stdout=(
                "# /etc/systemd/journald.conf\n[Journal]\n# Storage=auto\n"
                f"# {_JOURNALD}\n[Journal]\nStorage=volatile\n"
            )
        )
        commands.update(dict(apt_command_results((
            "docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin",
            "docker-compose-plugin",
        ))))
        commands.update({
            restart: completed(restart),
            enable: completed(enable),
        })
        host = FakeHost(
            files=docker_files(daemon, journald="[Journal]\nStorage=volatile\n"),
            commands=commands,
        )
        DockerEngineStep().apply(context(host))
        self.assertIn(("run", (restart, True)), host.calls)

    def test_journald_later_volatile_storage_refuses_before_mutation(self) -> None:
        later = "/etc/systemd/journald.conf.d/90-other.conf"
        output = docker_commands()[_JOURNALD_CAT].stdout
        output += f"# {later}\n[Journal]\nStorage=volatile\n"
        host = self.setting_host(commands={_JOURNALD_CAT: completed(_JOURNALD_CAT, output)})
        self.assert_setting_refusal(
            host, "journald storage", "Storage=volatile", "persistent storage", later
        )

    def test_journald_outside_auto_depends_on_journal_directory(self) -> None:
        later = "/etc/systemd/journald.conf.d/90-other.conf"
        output = docker_commands()[_JOURNALD_CAT].stdout
        output += f"# {later}\n[Journal]\nStorage=auto\n"
        commands: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {
            _JOURNALD_CAT: completed(_JOURNALD_CAT, output)
        }
        self.assert_setting_refusal(
            self.setting_host(commands=commands),
            "journald storage", "Storage=auto without /var/log/journal", later,
        )

        files = docker_files(docker_policy())
        files["/var/log/journal"] = ""
        host = self.setting_host(files=files, commands=commands)
        checked = DockerEngineStep().check(context(host))
        self.assertEqual(checked.disposition, Disposition.CONVERGED, checked)
        self.assertIn(f"journald's Storage is set by {later}", checked.detail)
        host.calls.clear()
        DockerEngineStep().apply(context(host))
        self.assertNotIn(("run", (("systemctl", "restart", "systemd-journald"), True)), host.calls)
        self.assertFalse(any(call[0] == "write_text" for call in host.calls))

    def test_journald_all_unset_without_directory_writes_own_default(self) -> None:
        files = docker_files(docker_policy())
        del files[os.fspath(_JOURNALD)]
        output = self.recorded_journald()
        host = self.setting_host(
            files=files,
            commands={_JOURNALD_CAT: completed(_JOURNALD_CAT, output)},
        )
        checked = DockerEngineStep().check(context(host))
        self.assertEqual(checked.disposition, Disposition.DRIFT, checked)
        self.assertIn(f"{_JOURNALD} is missing", checked.detail)
        host.calls.clear()
        DockerEngineStep().apply(context(host))
        self.assertEqual(host.files[os.fspath(_JOURNALD)], _JOURNALD_TEXT)
        self.assertEqual(
            [call for call in host.calls if call[0] == "write_text"],
            [("write_text", (os.fspath(_JOURNALD), _JOURNALD_TEXT))],
        )
        self.assertIn(("run", (("systemctl", "restart", "systemd-journald"), True)), host.calls)

    def test_journald_outside_cap_keeps_absent_or_different_drop_in(self) -> None:
        later = "/etc/systemd/journald.conf.d/90-other.conf"
        for own in (None, "[Journal]\nSystemMaxUse=9G\n"):
            with self.subTest(own=own):
                files = docker_files(docker_policy())
                if own is None:
                    del files[os.fspath(_JOURNALD)]
                else:
                    files[os.fspath(_JOURNALD)] = own
                files["/var/log/journal"] = ""
                output = self.recorded_journald()
                if own is not None:
                    output += f"# {_JOURNALD}\n{own}"
                output += f"# {later}\n[Journal]\nSystemMaxUse=7G\n"
                host = self.setting_host(
                    files=files, commands={_JOURNALD_CAT: completed(_JOURNALD_CAT, output)}
                )
                checked = DockerEngineStep().check(context(host))
                self.assertEqual(checked.disposition, Disposition.CONVERGED, checked)
                self.assertIn(f"journald's SystemMaxUse is set by {later}", checked.detail)
                host.calls.clear()
                DockerEngineStep().apply(context(host))
                self.assertEqual(host.files.get(os.fspath(_JOURNALD)), own)
                self.assertFalse(any(call[0] == "write_text" for call in host.calls))
                self.assertNotIn(
                    ("run", (("systemctl", "restart", "systemd-journald"), True)), host.calls
                )

    def test_recorded_journald_comments_do_not_open_a_file(self) -> None:
        files = docker_files(docker_policy())
        del files[os.fspath(_JOURNALD)]
        output = self.recorded_journald()
        self.assertIn("# /etc/ if the original file is shipped in /usr/)", output)
        output = output.replace("[Journal]\n", "[Journal]\nStorage=persistent\n", 1)
        host = self.setting_host(
            files=files, commands={_JOURNALD_CAT: completed(_JOURNALD_CAT, output)}
        )
        checked = DockerEngineStep().check(context(host))
        self.assertEqual(checked.disposition, Disposition.CONVERGED, checked)
        self.assertIn("journald's Storage is set by /etc/systemd/journald.conf", checked.detail)

    def test_failing_journald_reader_refuses_before_mutation(self) -> None:
        host = self.setting_host(commands={
            _JOURNALD_CAT: subprocess.CompletedProcess(
                list(_JOURNALD_CAT), 2, "", "fictitious journald error\nsecond line\n"
            )
        })
        self.assert_setting_refusal(host, "systemd-analyze", "fictitious journald error")

    def test_package_default_containerd_writes_and_restarts_both(self) -> None:
        files = docker_files(docker_policy())
        files[os.fspath(_CONTAINERD_CONFIG)] = "version = 4\n"
        host = self.setting_host(
            files=files,
            commands={
                _CONTAINERD_VERIFY: completed(_CONTAINERD_VERIFY, ""),
                _CONTAINERD_DUMP: containerd_dump(_CONTAINERD_DEFAULT_ROOT),
            },
        )
        checked = DockerEngineStep().check(context(host))
        self.assertEqual(checked.disposition, Disposition.DRIFT, checked)
        self.assertIn("config.toml is at the package default", checked.detail)
        host.calls.clear()
        DockerEngineStep().apply(context(host))
        self.assertEqual(host.files[os.fspath(_CONTAINERD_CONFIG)], _CONTAINERD_TEXT)
        self.assertEqual(
            [call for call in host.calls if call[0] == "write_text"],
            [("write_text", (os.fspath(_CONTAINERD_CONFIG), _CONTAINERD_TEXT))],
        )
        restarts = [
            call[1][0] for call in host.calls
            if call[0] == "run" and isinstance(call[1], tuple)
            and call[1][0] in {
                ("systemctl", "restart", "containerd"),
                ("systemctl", "restart", "docker"),
            }
        ]
        self.assertEqual(restarts, [
            ("systemctl", "restart", "containerd"),
            ("systemctl", "restart", "docker"),
        ])

    def test_moved_containerd_root_is_met_without_writing_or_restarting(self) -> None:
        files = docker_files(docker_policy())
        moved = f"version = 3\nroot = '{_CONTAINERD_ROOT}'\n"
        files[os.fspath(_CONTAINERD_CONFIG)] = moved
        host = self.setting_host(files=files)
        checked = DockerEngineStep().check(context(host))
        self.assertEqual(checked.disposition, Disposition.CONVERGED, checked)
        self.assertIn(
            f"{_CONTAINERD_CONFIG} is not provision's text, its root is {_CONTAINERD_ROOT}",
            checked.detail,
        )
        host.calls.clear()
        DockerEngineStep().apply(context(host))
        self.assertEqual(host.files[os.fspath(_CONTAINERD_CONFIG)], moved)
        self.assertFalse(any(call[0] == "write_text" for call in host.calls))
        self.assertNotIn(("run", (("systemctl", "restart", "containerd"), True)), host.calls)
        self.assertNotIn(("run", (("systemctl", "restart", "docker"), True)), host.calls)

    def test_modified_containerd_root_shortfalls_refuse_before_mutation(self) -> None:
        # A modified file without root runs on the package's root.
        for config, root in (
            ("version = 4\n", _CONTAINERD_DEFAULT_ROOT),
            ("root = '/srv/other-containerd'\n", "/srv/other-containerd"),
        ):
            with self.subTest(config=config):
                files = docker_files(docker_policy())
                files[os.fspath(_CONTAINERD_CONFIG)] = config
                self.assert_setting_refusal(
                    self.setting_host(
                        files=files, commands={_CONTAINERD_DUMP: containerd_dump(root)}
                    ),
                    "containerd root", os.fspath(root), os.fspath(_CONTAINERD_ROOT),
                    f"set by {_CONTAINERD_CONFIG}",
                )

    def test_an_import_moving_the_root_is_judged_by_the_root_in_effect(self) -> None:
        own = docker_files(docker_policy())
        package_default = docker_files(docker_policy())
        package_default[os.fspath(_CONTAINERD_CONFIG)] = "version = 4\n"
        absent = docker_files(docker_policy())
        del absent[os.fspath(_CONTAINERD_CONFIG)]
        for name, files in (("own", own), ("package default", package_default), ("absent", absent)):
            with self.subTest(name=name, root="short"):
                self.assert_setting_refusal(
                    self.setting_host(files=files, commands={
                        _CONTAINERD_VERIFY: completed(_CONTAINERD_VERIFY, ""),
                        _CONTAINERD_DUMP: containerd_dump("/srv/shared-containerd"),
                    }),
                    "containerd root", "/srv/shared-containerd",
                    f"set by a file {_CONTAINERD_CONFIG} imports",
                )
        with self.subTest(name="package default", root="met"):
            host = self.setting_host(files=package_default, commands={
                _CONTAINERD_VERIFY: completed(_CONTAINERD_VERIFY, ""),
            })
            checked = DockerEngineStep().check(context(host))
            self.assertEqual(checked.disposition, Disposition.CONVERGED, checked)
            self.assertIn("is not provision's text", checked.detail)
            host.calls.clear()
            DockerEngineStep().apply(context(host))
            self.assertFalse(any(call[0] == "write_text" for call in host.calls))

    def test_containerd_configuration_dump_failure_refuses_before_mutation(self) -> None:
        files = docker_files(docker_policy())
        files[os.fspath(_CONTAINERD_CONFIG)] = "root = [\n"
        host = self.setting_host(files=files, commands={
            _CONTAINERD_DUMP: subprocess.CompletedProcess(
                list(_CONTAINERD_DUMP), 1, "",
                'time="fictitious" level=error msg="Failure unmarshaling TOML"\n'
                "containerd: failed to unmarshal TOML at row 1 column 8\n",
            )
        })
        self.assert_setting_refusal(
            host, "containerd could not report its configuration",
            "failed to unmarshal TOML at row 1 column 8",
        )

    def test_failing_containerd_verification_refuses_before_mutation(self) -> None:
        files = docker_files(docker_policy())
        files[os.fspath(_CONTAINERD_CONFIG)] = "version = 4\n"
        host = self.setting_host(files=files, commands={
            _CONTAINERD_VERIFY: subprocess.CompletedProcess(
                list(_CONTAINERD_VERIFY), 2, "", "fictitious dpkg error\nsecond line\n"
            ),
            _CONTAINERD_DUMP: containerd_dump(_CONTAINERD_DEFAULT_ROOT),
        })
        self.assert_setting_refusal(host, "dpkg could not verify", "fictitious dpkg error")

    def test_populated_store_refuses_before_containerd_and_journald_readings(self) -> None:
        snapshot = _CONTAINERD_DEFAULT_ROOT / _CONTAINERD_STORE_PATHS[0] / "snapshot-1"
        files = docker_files(docker_policy())
        files[os.fspath(snapshot)] = ""
        host = self.setting_host(files=files)
        DockerEngineStep().check(context(host))
        self.assertNotIn(("run", (_CONTAINERD_VERIFY, False)), host.calls)
        self.assertNotIn(("run", (_JOURNALD_CAT, False)), host.calls)
        self.assert_setting_refusal(
            host, os.fspath(_CONTAINERD_DEFAULT_ROOT), os.fspath(_CONTAINERD_ROOT)
        )
        self.assertNotIn(("run", (_CONTAINERD_VERIFY, False)), host.calls)
        self.assertNotIn(("run", (_JOURNALD_CAT, False)), host.calls)


class DockerSourceTests(unittest.TestCase):
    def _assert_refusal(self, host: FakeHost, named: str) -> CheckResult:
        step = DockerEngineStep()
        checked = step.check(context(host))
        self.assertEqual(checked.disposition, Disposition.UNFIXABLE)
        self.assertIn(named, checked.detail)
        before_apply = len(host.calls)
        with self.assertRaises(StepFailure) as raised:
            step.apply(context(host))
        self.assertEqual(raised.exception.detail, checked.detail)
        self.assertEqual(raised.exception.fix, checked.fix)
        for method, arguments in host.calls[before_apply:]:
            self.assertNotIn(method, {"write_text", "mkdir", "unlink"})
            if method == "run":
                assert isinstance(arguments, tuple)
                argv = arguments[0]
                assert isinstance(argv, tuple)
                self.assertNotIn(argv[0], {"apt-get", "wget", "mv", "systemctl", "dpkg"})
        return checked

    def test_two_sources_refuse_before_any_mutation(self) -> None:
        files = docker_files(docker_policy())
        files[os.fspath(_SOURCE)] = _REPO
        host = FakeHost(files=files, commands=docker_absent_commands())
        checked = self._assert_refusal(host, _OLD_DOCKER_SOURCE)
        self.assertIn(os.fspath(_SOURCE), checked.detail)
        self.assertIn("2 apt sources", checked.detail)

    def test_foreign_source_refuses_when_docker_is_absent(self) -> None:
        foreign_files = docker_files(docker_policy())
        recipe_path_files = docker_recipe_files(docker_policy())
        recipe_path_files[os.fspath(_SOURCE)] = _REPO.replace(
            os.fspath(_KEYRING), "/etc/apt/keyrings/other.asc"
        )
        for files, path in (
            (foreign_files, _OLD_DOCKER_SOURCE),
            (recipe_path_files, os.fspath(_SOURCE)),
        ):
            with self.subTest(path=path):
                checked = self._assert_refusal(
                    FakeHost(files=files, commands=docker_absent_commands()), path
                )
                self.assertIn("not the recipe's source", checked.detail)

    def test_disabled_recipe_path_refuses_without_changing_its_bytes(self) -> None:
        files = docker_recipe_files(docker_policy())
        files[os.fspath(_SOURCE)] = _REPO + "Enabled: false\n"
        host = FakeHost(files=files, commands=docker_absent_commands())
        before = host.files[os.fspath(_SOURCE)]
        checked = self._assert_refusal(host, os.fspath(_SOURCE))
        self.assertIn("holds no enabled entry", checked.detail)
        self.assertEqual(host.files[os.fspath(_SOURCE)], before)

    def test_recipe_entry_drift_keeps_source_and_fetches_only_empty_keyring(self) -> None:
        for key in (None, "", "key"):
            with self.subTest(key=key):
                files = docker_recipe_files(docker_policy())
                if key is None:
                    del files[os.fspath(_KEYRING)]
                else:
                    files[os.fspath(_KEYRING)] = key
                host = FakeHost(files=files, commands=docker_absent_commands())
                checked = DockerEngineStep().check(context(host))
                self.assertEqual(checked.disposition, Disposition.DRIFT)
                self.assertEqual(checked.detail, "Docker is not installed")

                DockerEngineStep().apply(context(host))

                self.assertEqual(host.files[os.fspath(_SOURCE)], _REPO)
                self.assertFalse(
                    any(method == "write_text" and arguments[0] == os.fspath(_SOURCE)
                        for method, arguments in host.calls if isinstance(arguments, tuple))
                )
                wget = ("wget", "-qO", f"{_KEYRING}.partial", _KEY_URL)
                self.assertEqual(("run", (wget, True)) in host.calls, key is None or key == "")

    def test_recipe_entry_with_folded_or_padded_signed_by_is_the_recipe(self) -> None:
        for text in (
            _REPO.replace(f"Signed-By: {_KEYRING}\n", f"Signed-By: {_KEYRING}  \n"),
            _REPO.replace(f"Signed-By: {_KEYRING}\n", f"Signed-By:\n {_KEYRING}\n"),
        ):
            with self.subTest(text=text):
                files = docker_recipe_files(docker_policy())
                files[os.fspath(_SOURCE)] = text
                host = FakeHost(files=files, commands=docker_absent_commands())
                checked = DockerEngineStep().check(context(host))
                self.assertEqual(checked.disposition, Disposition.DRIFT)
                self.assertEqual(checked.detail, "Docker is not installed")

    def test_absent_docker_without_source_writes_recipe_before_install(self) -> None:
        files = docker_recipe_files(docker_policy())
        del files[os.fspath(_KEYRING)]
        del files[os.fspath(_SOURCE)]
        files["/etc/apt/sources.list.d/ubuntu.sources.curtin.orig"] = _REPO
        host = FakeHost(files=files, commands=docker_absent_commands())
        checked = DockerEngineStep().check(context(host))
        self.assertEqual(checked.disposition, Disposition.DRIFT)
        self.assertEqual(checked.detail, "Docker is not installed")

        DockerEngineStep().apply(context(host))

        move = ("mv", "-f", f"{_KEYRING}.partial", os.fspath(_KEYRING))
        moved = host.calls.index(("run", (move, True)))
        written = next(
            index for index, call in enumerate(host.calls)
            if call[0] == "write_text"
            and isinstance(call[1], tuple)
            and call[1][0] == os.fspath(_SOURCE)
        )
        installed = host.calls.index(("run", (("apt-get", "update"), True)))
        self.assertLess(moved, written)
        self.assertLess(written, installed)
        self.assertIn(
            ("run", (("apt-get", "install", "-y", *_PACKAGES), True)), host.calls
        )
        self.assertEqual(host.files[os.fspath(_SOURCE)], _REPO)

    def test_present_docker_without_source_converges_when_packages_are_installed(self) -> None:
        files = docker_files(docker_policy())
        del files[_OLD_DOCKER_SOURCE]
        commands = docker_commands()
        commands.update(docker_package_commands())
        host = FakeHost(files=files, commands=commands)

        checked = DockerEngineStep().check(context(host))

        self.assertEqual(checked.disposition, Disposition.CONVERGED)
        self.assertNotIn("apt source is", checked.detail)
        probes = [argv for argv, _, _ in host.runs if argv[0] == "dpkg-query"]
        self.assertEqual(len(probes), len(_PACKAGES))

    def test_old_source_text_is_accepted_and_reported(self) -> None:
        files = docker_files(docker_policy())
        host = FakeHost(files=files, commands=docker_commands())
        checked = DockerEngineStep().check(context(host))
        self.assertEqual(checked.disposition, Disposition.CONVERGED)
        self.assertIn(
            f"Docker's apt source is {_OLD_DOCKER_SOURCE}, not the recipe's {_SOURCE}",
            checked.detail,
        )

    def test_foreign_source_at_recipe_path_is_reported_with_its_entry(self) -> None:
        files = docker_recipe_files(docker_policy())
        files[os.fspath(_SOURCE)] = _REPO.replace(
            os.fspath(_KEYRING), "/etc/apt/keyrings/other.asc"
        )
        host = FakeHost(files=files, commands=docker_commands())
        checked = DockerEngineStep().check(context(host))
        self.assertEqual(checked.disposition, Disposition.CONVERGED)
        self.assertIn(
            f"Docker's apt source {_SOURCE} is not the recipe's entry",
            checked.detail,
        )

    def test_present_docker_with_foreign_entry_writes_no_apt_source(self) -> None:
        files = docker_files(docker_policy())
        commands = docker_commands()
        commands.update(docker_package_commands())
        host = FakeHost(files=files, commands=commands)
        before = dict(host.files)
        checked = DockerEngineStep().check(context(host))
        self.assertEqual(checked.disposition, Disposition.CONVERGED)
        self.assertIn(_OLD_DOCKER_SOURCE, checked.detail)

        DockerEngineStep().apply(context(host))

        self.assertEqual(host.files[_OLD_DOCKER_SOURCE], before[_OLD_DOCKER_SOURCE])
        self.assertEqual(host.files[_OLD_DOCKER_KEYRING], before[_OLD_DOCKER_KEYRING])
        self.assertNotIn(os.fspath(_SOURCE), host.files)
        self.assertFalse(any(argv[0] in {"wget", "mv"} for argv, _, _ in host.runs))

    def test_missing_compose_with_an_entry_installs_without_writing_a_source(self) -> None:
        files = docker_files(docker_policy())
        commands = docker_commands()
        compose = ("docker", "compose", "version")
        commands[compose] = subprocess.CompletedProcess(list(compose), 1, "", "not found")
        commands.update(docker_package_commands(absent=("docker-compose-plugin",)))
        commands.update(dict(apt_command_results(("docker-compose-plugin",))))
        host = FakeHost(files=files, commands=commands)
        checked = DockerEngineStep().check(context(host))
        self.assertEqual(checked.disposition, Disposition.DRIFT)
        self.assertIn("Compose plugin", checked.detail)

        DockerEngineStep().apply(context(host))

        self.assertIn(
            ("run", (("apt-get", "install", "-y", "docker-compose-plugin"), True)),
            host.calls,
        )
        self.assertFalse(any(argv[0] in {"wget", "mv"} for argv, _, _ in host.runs))
        self.assertNotIn(os.fspath(_SOURCE), host.files)

    def test_present_docker_without_source_refuses_missing_buildx(self) -> None:
        files = docker_files(docker_policy())
        del files[_OLD_DOCKER_SOURCE]
        commands = docker_commands()
        commands.update(docker_package_commands(absent=("docker-buildx-plugin",)))
        checked = self._assert_refusal(
            FakeHost(files=files, commands=commands), "docker-buildx-plugin"
        )
        self.assertIn("no apt source serves", checked.detail)
        self.assertIn(os.fspath(_SOURCE), checked.fix)

    def test_unreadable_source_refuses_with_its_path(self) -> None:
        class UnreadableSourceHost(FakeHost):
            def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
                if os.fspath(path) == os.fspath(_SOURCE):
                    raise PermissionError("access denied")
                return super().read_text(path, encoding=encoding)

        host = UnreadableSourceHost(
            files=docker_recipe_files(docker_policy()), commands=docker_commands()
        )
        checked = self._assert_refusal(host, os.fspath(_SOURCE))
        self.assertIn("access denied", checked.detail)

    def test_apply_write_set_follows_source_and_engine_state(self) -> None:
        no_source = docker_recipe_files(docker_policy())
        del no_source[os.fspath(_SOURCE)]
        del no_source[os.fspath(_KEYRING)]
        no_source_with_key = docker_recipe_files(docker_policy())
        del no_source_with_key[os.fspath(_SOURCE)]
        ignored_source = dict(no_source)
        ignored_source["/etc/apt/sources.list.d/ubuntu.sources.curtin.orig"] = _REPO
        recipe_without_key = docker_recipe_files(docker_policy())
        del recipe_without_key[os.fspath(_KEYRING)]
        empty_keyring = docker_recipe_files(docker_policy())
        empty_keyring[os.fspath(_KEYRING)] = ""
        foreign_recipe = docker_recipe_files(docker_policy())
        foreign_recipe[os.fspath(_SOURCE)] = _REPO.replace(
            os.fspath(_KEYRING), "/etc/apt/keyrings/other.asc"
        )
        disabled = docker_recipe_files(docker_policy())
        disabled[os.fspath(_SOURCE)] += "Enabled: no\n"
        two_sources = docker_files(docker_policy())
        two_sources[os.fspath(_SOURCE)] = _REPO
        present_no_source = docker_files(docker_policy())
        del present_no_source[_OLD_DOCKER_SOURCE]
        installed = docker_commands()
        installed.update(docker_package_commands())
        missing_buildx = docker_commands()
        missing_buildx.update(docker_package_commands(absent=("docker-buildx-plugin",)))
        missing_compose = docker_commands()
        compose_probe = ("docker", "compose", "version")
        missing_compose[compose_probe] = subprocess.CompletedProcess(
            list(compose_probe), 1, "", "not found"
        )
        missing_compose.update(docker_package_commands(absent=("docker-compose-plugin",)))
        missing_compose.update(dict(apt_command_results(("docker-compose-plugin",))))
        cases = (
            ("absent, no source", no_source, docker_absent_commands(), True, True, False),
            ("absent, no source, present key", no_source_with_key, docker_absent_commands(), True, False, False),
            ("absent, ignored extension", ignored_source, docker_absent_commands(), True, True, False),
            ("absent, recipe, no key", recipe_without_key, docker_absent_commands(), False, True, False),
            ("absent, recipe, empty key", empty_keyring, docker_absent_commands(), False, True, False),
            ("absent, recipe, present key", docker_recipe_files(docker_policy()), docker_absent_commands(), False, False, False),
            ("absent, foreign", docker_files(docker_policy()), docker_absent_commands(), False, False, True),
            ("absent, foreign at recipe path", foreign_recipe, docker_absent_commands(), False, False, True),
            ("absent, disabled", disabled, docker_absent_commands(), False, False, True),
            ("absent, two sources", two_sources, docker_absent_commands(), False, False, True),
            ("present, foreign", docker_files(docker_policy()), installed, False, False, False),
            ("present, foreign at recipe path", foreign_recipe, installed, False, False, False),
            ("present, recipe", docker_recipe_files(docker_policy()), installed, False, False, False),
            ("present, compose missing", docker_files(docker_policy()), missing_compose, False, False, False),
            ("present, no source", present_no_source, installed, False, False, False),
            ("present, missing buildx", present_no_source, missing_buildx, False, False, True),
        )
        allowed = {
            os.fspath(_KEYRING), os.fspath(_SOURCE), "/etc/docker/daemon.json",
            os.fspath(_CONTAINERD_CONFIG), "/etc/systemd/journald.conf.d/gideon.conf",
        }
        for name, files, commands, writes_source, fetches_key, refuses in cases:
            with self.subTest(name=name):
                host = FakeHost(files=files, commands=commands)
                if refuses:
                    with self.assertRaises(StepFailure):
                        DockerEngineStep().apply(context(host))
                else:
                    DockerEngineStep().apply(context(host))
                writes = [arguments[0] for method, arguments in host.calls
                          if method == "write_text" and isinstance(arguments, tuple)]
                self.assertTrue(set(writes) <= allowed)
                self.assertEqual(os.fspath(_SOURCE) in writes, writes_source)
                if os.fspath(_KEYRING) in writes:
                    self.assertTrue(fetches_key)
                fetched = any(
                    argv == ("mv", "-f", f"{_KEYRING}.partial", os.fspath(_KEYRING))
                    for argv, _, _ in host.runs
                )
                self.assertEqual(fetched, fetches_key)


class DockerOwnedKeyTests(unittest.TestCase):
    """The shared daemon file through the provision step's Host seam: GIDEON's keys alone."""

    def setUp(self) -> None:
        self.step = DockerEngineStep()
        self.daemon: dict[str, object] = {
            "data-root": "/var/lib/docker",
            "features": {"cdi": True},
            "log-driver": "journald",
        }
        self.commands = docker_commands(
            docker_version=f"{HOST_LOCK.minimums.docker}.0.0",
            compose_version=f"{HOST_LOCK.minimums.compose}.0.0",
        )
        for package in (
            "docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin",
            "docker-compose-plugin",
        ):
            probe, result = package_result(package, "99.0-fictitious")
            self.commands[probe] = result
        site = context(FakeHost()).site
        if site is None:
            self.fail("the example site did not load")
        self.site = site

    def _host(self, daemon: Mapping[str, object]) -> FakeHost:
        return FakeHost(files=docker_recipe_files(daemon), commands=self.commands)

    def _daemon_writes(self, host: FakeHost) -> list[str]:
        return [
            arguments[1]
            for method, arguments in host.calls
            if method == "write_text"
            and isinstance(arguments, tuple)
            and arguments[0] == os.fspath(_DAEMON)
        ]

    def _docker_restarts(self, host: FakeHost) -> int:
        return host.calls.count(("run", (("systemctl", "restart", "docker"), True)))

    def test_foreign_runtime_is_reported_without_daemon_write_or_restart(self) -> None:
        """A foreign key on a converged file is kept, reported, and costs no restart."""

        daemon = {**self.daemon, "runtimes": {"custom": {"path": "/usr/bin/custom"}}}
        host = self._host(daemon)
        checked = self.step.check(context(host))
        self.assertEqual(checked.disposition, Disposition.CONVERGED)
        self.assertEqual(
            checked.detail,
            "Docker, containerd, and their host policies are current; "
            "daemon.json also holds keys GIDEON does not own: runtimes",
        )

        self.step.apply(context(host))
        self.assertEqual(self._daemon_writes(host), [])
        self.assertEqual(self._docker_restarts(host), 0)
        self.assertEqual(json.loads(host.files[os.fspath(_DAEMON)]), daemon)

    def test_absent_and_default_log_driver_set_once_and_keep_runtime(self) -> None:
        """An absent or package-default key is set, Docker restarts once, and the recheck converges."""

        for value in (None, "json-file"):
            with self.subTest(value=value):
                daemon = {**self.daemon, "runtimes": {"custom": {}}}
                if value is None:
                    del daemon["log-driver"]
                else:
                    daemon["log-driver"] = value
                host = self._host(daemon)
                checked = self.step.check(context(host))
                self.assertEqual(checked.disposition, Disposition.DRIFT)
                self.assertEqual(checked.detail, f"{_DAEMON} lacks GIDEON's keys: log-driver")

                self.step.apply(context(host))
                self.assertEqual(len(self._daemon_writes(host)), 1)
                self.assertEqual(self._docker_restarts(host), 1)
                written = json.loads(host.files[os.fspath(_DAEMON)])
                self.assertEqual(written["runtimes"], daemon["runtimes"])
                self.assertEqual(written["log-driver"], self.daemon["log-driver"])
                self.assertEqual(written["data-root"], self.daemon["data-root"])
                self.assertEqual(written["features"], self.daemon["features"])
                self.assertEqual(
                    self.step.check(context(host)).disposition,
                    Disposition.CONVERGED,
                )

    def test_moved_data_root_refuses_before_any_mutation(self) -> None:
        """A moved owned key short of its need refuses before the host changes."""

        host = self._host({**self.daemon, "data-root": "/srv/docker"})
        checked = self.step.check(context(host))
        self.assertEqual(checked.disposition, Disposition.UNFIXABLE)
        self.assertEqual(
            checked.detail,
            'data-root is "/srv/docker", GIDEON needs "/var/lib/docker"',
        )
        self.assertIn(os.fspath(_DAEMON), checked.fix)
        self.assertIn("restart Docker", checked.fix)

        before_calls = len(host.calls)
        before_runs = len(host.runs)
        with self.assertRaises(StepFailure) as raised:
            self.step.apply(context(host))
        self.assertEqual(raised.exception.detail, checked.detail)
        self.assertEqual(raised.exception.fix, checked.fix)
        self.assertEqual(
            [call for call in host.calls[before_calls:] if call[0] in {"write_text", "mkdir"}],
            [],
        )
        self.assertEqual(
            [argv for argv, _, _ in host.runs[before_runs:]],
            [("docker", "--version"), ("docker", "compose", "version")],
        )

    def test_registry_list_meets_by_membership_and_refuses_when_short(self) -> None:
        """The registry list is met by membership and short without GIDEON's authority."""

        authority = "192.168.122.1:5000"
        site = replace(self.site, registry=authority)
        with_authority = {**self.daemon, "insecure-registries": ["other.example:5000", authority]}
        met = self.step.check(context(self._host(with_authority), site=site))
        self.assertEqual(met.disposition, Disposition.CONVERGED)
        short = self.step.check(context(
            self._host({**self.daemon, "insecure-registries": ["other.example:5000"]}),
            site=site,
        ))
        self.assertEqual(short.disposition, Disposition.UNFIXABLE)
        self.assertIn("insecure-registries is", short.detail)
        self.assertIn('"other.example:5000"', short.detail)
        self.assertIn(f'GIDEON needs "{authority}"', short.detail)

    def test_feature_sibling_survives_a_write_and_false_cdi_refuses(self) -> None:
        """features.cdi is owned while its sibling is kept through a write."""

        features = {"cdi": True, "containerd-snapshotter": True}
        daemon = {**self.daemon, "features": features}
        met = self.step.check(context(self._host(daemon)))
        self.assertEqual(met.disposition, Disposition.CONVERGED)
        self.assertIn("features.containerd-snapshotter", met.detail)

        drifting = dict(daemon)
        del drifting["log-driver"]
        host = self._host(drifting)
        self.step.apply(context(host))
        written = json.loads(host.files[os.fspath(_DAEMON)])
        self.assertEqual(written["features"], features)
        self.assertEqual(len(self._daemon_writes(host)), 1)

        short = self.step.check(context(self._host({**self.daemon, "features": {"cdi": False}})))
        self.assertEqual(short.disposition, Disposition.UNFIXABLE)
        self.assertEqual(short.detail, "features.cdi is false, GIDEON needs true")

    def test_proxy_sibling_is_foreign_and_userinfo_is_redacted(self) -> None:
        """A proxy sibling is kept and a refusal never prints URL userinfo."""

        proxy = "http://proxy.example:3128"
        site = replace(self.site, egress_proxy=proxy)
        proxies = {"http-proxy": proxy, "https-proxy": proxy, "no-proxy": "localhost"}
        met = self.step.check(context(self._host({**self.daemon, "proxies": proxies}), site=site))
        self.assertEqual(met.disposition, Disposition.CONVERGED)
        self.assertIn("proxies.no-proxy", met.detail)

        moved = {**proxies, "http-proxy": "http://user:pass@other.example:3128"}
        short = self.step.check(context(self._host({**self.daemon, "proxies": moved}), site=site))
        self.assertEqual(short.disposition, Disposition.UNFIXABLE)
        self.assertIn("proxies.http-proxy is", short.detail)
        self.assertIn("…@other.example:3128", short.detail)
        self.assertIn(f'GIDEON needs "{proxy}"', short.detail)
        self.assertNotIn("user:pass", short.detail)

    def test_lapsed_proxy_is_reported_and_unwritten(self) -> None:
        """A conditional key the site no longer sets is kept and reported, never removed."""

        daemon = {**self.daemon, "proxies": {
            "http-proxy": "http://proxy.example:3128",
            "https-proxy": "http://proxy.example:3128",
        }}
        host = self._host(daemon)
        checked = self.step.check(context(host))
        self.assertEqual(checked.disposition, Disposition.CONVERGED)
        self.assertTrue(checked.detail.endswith(
            "keys GIDEON does not own: proxies.http-proxy, proxies.https-proxy"
        ))
        self.step.apply(context(host))
        self.assertEqual(self._daemon_writes(host), [])
        self.assertEqual(self._docker_restarts(host), 0)
        self.assertEqual(json.loads(host.files[os.fspath(_DAEMON)]), daemon)

    def test_invalid_json_array_and_malformed_feature_refuse_before_writes(self) -> None:
        """Broken daemon JSON and a malformed owned container refuse before any write."""

        cases = (
            ("{invalid", "not a JSON object", "one JSON object"),
            ("[]", "not a JSON object", "one JSON object"),
            (json.dumps({**self.daemon, "features": "on"}), 'features is "on", not an object', "named key"),
        )
        for daemon_text, detail, fix in cases:
            with self.subTest(daemon_text=daemon_text):
                files = docker_files(self.daemon)
                files[os.fspath(_DAEMON)] = daemon_text
                host = FakeHost(files=files, commands=self.commands)
                checked = self.step.check(context(host))
                self.assertEqual(checked.disposition, Disposition.UNFIXABLE)
                self.assertIn(detail, checked.detail)
                self.assertIn(os.fspath(_DAEMON), checked.fix)
                self.assertIn(fix, checked.fix)

                before_calls = len(host.calls)
                with self.assertRaises(StepFailure) as raised:
                    self.step.apply(context(host))
                self.assertEqual(raised.exception.detail, checked.detail)
                self.assertEqual(raised.exception.fix, checked.fix)
                self.assertEqual(
                    [call for call in host.calls[before_calls:] if call[0] in {"write_text", "mkdir"}],
                    [],
                )

    def test_rewrite_keeps_a_restrictive_mode_and_refusals_hide_nested_userinfo(self) -> None:
        """A rewrite never widens the file's mode, and no refusal prints a nested credential."""

        daemon = {key: value for key, value in self.daemon.items() if key != "log-driver"}
        daemon["proxies"] = {"no-proxy": "localhost", "http-proxy": "http://user:pass@other.example:3128"}
        host = FakeHost(
            files=docker_files(daemon),
            commands=self.commands,
            stats={os.fspath(_DAEMON): os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, 0, 0, 0, 0, 0, 0))},
        )
        self.step.apply(context(host))
        self.assertEqual(
            [mode for path, mode in host.write_modes if path == os.fspath(_DAEMON)],
            [0o600],
        )

        proxy = "http://proxy.example:3128"
        site = replace(self.site, egress_proxy=proxy)
        cases = (
            {**self.daemon, "proxies": ["http://user:pass@other.example:3128"]},
            [{"proxies": {"http-proxy": "http://user:pass@other.example:3128"}}],
        )
        for value in cases:
            with self.subTest(value=value):
                files = docker_files(self.daemon)
                files[os.fspath(_DAEMON)] = json.dumps(value)
                checked = self.step.check(context(FakeHost(files=files, commands=self.commands), site=site))
                self.assertEqual(checked.disposition, Disposition.UNFIXABLE)
                self.assertIn("…@other.example:3128", checked.detail)
                self.assertNotIn("user:pass", checked.detail)

    def test_reordered_json_is_converged_and_unwritten(self) -> None:
        """Daemon content is judged, never key order or indentation."""

        reordered: dict[str, object] = {
            "log-driver": "journald",
            "features": {"cdi": True},
            "data-root": "/var/lib/docker",
        }
        host = self._host(reordered)
        original = host.files[os.fspath(_DAEMON)]
        self.assertNotEqual(original, json.dumps(reordered, indent=2, sort_keys=True) + "\n")
        self.assertEqual(self.step.check(context(host)).disposition, Disposition.CONVERGED)
        self.step.apply(context(host))
        self.assertEqual(self._daemon_writes(host), [])
        self.assertEqual(host.files[os.fspath(_DAEMON)], original)

    def test_empty_object_writes_fresh_box_bytes(self) -> None:
        """An empty daemon object takes every unconditional key in the fresh box's bytes."""

        host = self._host({})
        checked = self.step.check(context(host))
        self.assertEqual(checked.disposition, Disposition.DRIFT)
        self.assertEqual(
            checked.detail,
            f"{_DAEMON} lacks GIDEON's keys: data-root, log-driver, features.cdi",
        )
        self.step.apply(context(host))
        self.assertEqual(len(self._daemon_writes(host)), 1)
        self.assertEqual(self._docker_restarts(host), 1)
        self.assertEqual(
            host.files[os.fspath(_DAEMON)],
            json.dumps(self.daemon, indent=2, sort_keys=True) + "\n",
        )


class DockerDaemonLeafTests(unittest.TestCase):
    """The leaf's table of owned keys is held to the model."""

    @staticmethod
    def _header_index(lines: Sequence[str]) -> int:
        return next(
            index for index, line in enumerate(lines)
            if line.startswith("|") and line.split("|")[1].strip() == "GIDEON's `daemon.json` key"
        )

    def _table_names(self, text: str) -> set[str]:
        lines = text.splitlines()
        names: set[str] = set()
        for line in lines[self._header_index(lines) + 2:]:
            if not line.startswith("|"):
                break
            names.update(re.findall(r"`([^`]+)`", line.split("|")[1]))
        return names

    def test_leaf_table_names_exactly_the_owned_keys(self) -> None:
        """The leaf table and OWNED_KEYS move together, and the parse bites."""

        if absent_from_export("docs/archi/host.md", ROOT):
            self.skipTest("the architecture leaf is absent from this exported tree")
        leaf = (ROOT / "docs/archi/host.md").read_text()
        names = {key.name for key in dockerdaemon.OWNED_KEYS}
        self.assertEqual(self._table_names(leaf), names)

        lines = leaf.splitlines()
        first_row = self._header_index(lines) + 2
        without_one_row = "\n".join(lines[:first_row] + lines[first_row + 1:])
        self.assertNotEqual(self._table_names(without_one_row), names)


class BoxWideSettingsLeafTests(unittest.TestCase):
    """The leaf's box-wide setting names and steps match the step registry."""

    @staticmethod
    def _header_index(lines: Sequence[str]) -> int:
        return next(
            index for index, line in enumerate(lines)
            if line.startswith("|") and line.split("|")[1].strip() == "Box-wide setting"
        )

    def _table_pairs(self, text: str) -> set[tuple[str, str]]:
        lines = text.splitlines()
        pairs: set[tuple[str, str]] = set()
        for line in lines[self._header_index(lines) + 2:]:
            if not line.startswith("|"):
                break
            cells = [cell.strip() for cell in line.split("|")]
            name = re.fullmatch(r"`([^`]+)`", cells[1])
            step = re.fullmatch(r"`([^`]+)`", cells[2])
            if name is not None and step is not None:
                pairs.add((name.group(1), step.group(1)))
        return pairs

    def test_leaf_table_pairs_match_step_settings(self) -> None:
        if absent_from_export("docs/archi/host.md", ROOT):
            self.skipTest("the architecture leaf is absent from this exported tree")
        leaf = (ROOT / "docs/archi/host.md").read_text()
        pairs = {(setting.name, step.name) for step in STEPS for setting in step.settings}
        self.assertEqual(self._table_pairs(leaf), pairs)

        lines = leaf.splitlines()
        first_row = self._header_index(lines) + 2
        without_one_row = "\n".join(lines[:first_row] + lines[first_row + 1:])
        self.assertNotEqual(self._table_pairs(without_one_row), pairs)


def directory_stat(mode: int, uid: int = 0, gid: int = 0) -> os.stat_result:
    return os.stat_result((stat.S_IFDIR | mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))


def file_stat(mode: int, uid: int = 998, gid: int = 998) -> os.stat_result:
    return os.stat_result((stat.S_IFREG | mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))


UFW_AFTER_RULES = (
    "# rules.input-after\n*filter\n:ufw-after-input - [0:0]\n"
    "-A ufw-after-input -p udp --dport 137 -j ufw-skip-to-policy-input\nCOMMIT\n"
)


class NetworkStepTests(unittest.TestCase):
    UFW_STATUS = ("ufw", "status", "numbered")
    UFW_VERBOSE = ("ufw", "status", "verbose")

    def assert_firewall_refusal(self, host: FakeHost, *details: str) -> None:
        step = FirewallStep()
        reading = step.check(context(host))
        self.assertEqual(reading.disposition, Disposition.UNFIXABLE)
        for detail in details:
            self.assertIn(detail, reading.detail)
        host.calls.clear()
        with self.assertRaises(StepFailure) as raised:
            step.apply(context(host))
        self.assertEqual((raised.exception.detail, raised.exception.fix), (reading.detail, reading.fix))
        assert_no_host_mutation(self, host, {self.UFW_STATUS, self.UFW_VERBOSE})

    def test_firewall_removes_only_stale_tagged_rules_and_preserves_order(self) -> None:
        status = (
            "Status: inactive\n"
            "     To                         Action      From\n"
            "     --                         ------      ----\n"
            "[ 3] 22/tcp                    ALLOW IN    192.0.2.0/24 # gideon-provision\n"
            "[ 8] 443/tcp                   ALLOW IN    198.51.100.0/24 # gideon-provision\n"
        )
        host = self.firewall_converged_host()
        host.commands[self.UFW_STATUS] = completed(self.UFW_STATUS, status)
        host.files["/etc/ufw/after.rules"] = UFW_AFTER_RULES
        host.files.pop(os.fspath(_DOCKER_USER_UNIT))
        for command in (
            ("ufw", "--force", "delete", "8"),
            ("ufw", "allow", "proto", "tcp", "from", "192.0.2.0/24", "to", "any", "port", "443", "comment", "gideon-provision"),
            ("ufw", "default", "deny", "incoming"),
            ("ufw", "--force", "enable"),
            ("systemctl", "daemon-reload"),
        ):
            host.commands[command] = completed(command)
        step = FirewallStep()
        reading = step.check(context(host))
        self.assertEqual(reading.disposition, Disposition.DRIFT)
        self.assertIn("ufw is inactive, the installed default", reading.detail)
        step.apply(context(host))
        self.assertNotIn(("run", (self.UFW_VERBOSE, False)), host.calls)
        expected_block = (
            "# GIDEON BEGIN provision:firewall\n"
            "*filter\n"
            ":gideon-docker-user - [0:0]\n"
            "-A gideon-docker-user -m conntrack --ctstate RELATED,ESTABLISHED -m comment --comment gideon-provision -j RETURN\n"
            "-A gideon-docker-user -i docker0 -m comment --comment gideon-provision -j RETURN\n"
            "-A gideon-docker-user -i br-+ -m comment --comment gideon-provision -j RETURN\n"
            "-A gideon-docker-user -s 192.0.2.0/24 -p tcp -m conntrack --ctorigdstport 443 --ctdir ORIGINAL -m comment --comment gideon-provision -j RETURN\n"
            "-A gideon-docker-user -s 192.168.122.0/24 -p tcp -m conntrack --ctorigdstport 5000 --ctdir ORIGINAL -m comment --comment gideon-provision -j RETURN\n"
            "-A gideon-docker-user -o docker0 -p tcp -m conntrack --ctorigdstport 443 --ctdir ORIGINAL -m comment --comment gideon-provision -j DROP\n"
            "-A gideon-docker-user -o br-+ -p tcp -m conntrack --ctorigdstport 443 --ctdir ORIGINAL -m comment --comment gideon-provision -j DROP\n"
            "-A gideon-docker-user -o docker0 -p tcp -m conntrack --ctorigdstport 5000 --ctdir ORIGINAL -m comment --comment gideon-provision -j DROP\n"
            "-A gideon-docker-user -o br-+ -p tcp -m conntrack --ctorigdstport 5000 --ctdir ORIGINAL -m comment --comment gideon-provision -j DROP\n"
            "-A gideon-docker-user -m comment --comment gideon-provision -j RETURN\n"
            "COMMIT\n"
            "# GIDEON END provision:firewall\n"
        )
        self.assertEqual(
            host.files["/etc/ufw/after.rules"],
            UFW_AFTER_RULES.rstrip("\n") + "\n\n" + expected_block,
        )
        self.assertEqual(host.files[os.fspath(_DOCKER_USER_UNIT)], _DOCKER_USER_UNIT_TEXT)
        self.assertIn((os.fspath(_DOCKER_USER_UNIT), 0o644), host.write_modes)
        ordered = [
            ("write_text", ("/etc/ufw/after.rules", host.files["/etc/ufw/after.rules"])),
            ("run", (("ufw", "reload"), True)),
            ("write_text", (os.fspath(_DOCKER_USER_UNIT), _DOCKER_USER_UNIT_TEXT)),
            ("run", (("systemctl", "daemon-reload"), True)),
            ("run", (("systemctl", "enable", "gideon-docker-user"), True)),
            ("run", (("systemctl", "start", "gideon-docker-user"), True)),
            ("run", (("iptables", "-w", "-S", "DOCKER-USER"), True)),
        ]
        positions = [host.calls.index(call) for call in ordered]
        self.assertEqual(positions, sorted(positions))
        mutations: list[tuple[str, ...]] = []
        for method, arguments in host.calls:
            if (
                method == "run"
                and isinstance(arguments, tuple)
                and arguments
                and isinstance(arguments[0], tuple)
                and arguments[0]
                and arguments[0][0] == "ufw"
                and arguments[0][1:] != ("status", "numbered")
            ):
                mutations.append(arguments[0])
        self.assertEqual(mutations[0], ("ufw", "--force", "delete", "8"))
        self.assertLess(
            next(index for index, call in enumerate(mutations) if call[-1] == "gideon-provision"),
            next(index for index, call in enumerate(mutations) if call == ("ufw", "--force", "enable")),
        )
        self.assertLess(
            mutations.index(("ufw", "default", "deny", "incoming")),
            mutations.index(("ufw", "--force", "enable")),
        )

    def firewall_converged_host(self) -> FakeHost:
        status = (
            "Status: active\n"
            "[ 1] 22/tcp                    ALLOW IN    192.0.2.0/24 # gideon-provision\n"
            "[ 2] 443/tcp                   ALLOW IN    192.0.2.0/24 # gideon-provision\n"
        )
        chain = "-N gideon-docker-user\n" + "\n".join(_docker_user_rules(["192.0.2.0/24"])) + "\n"
        owned_chain = ("iptables", "-w", "-S", "gideon-docker-user")
        shared_chain = ("iptables", "-w", "-S", "DOCKER-USER")
        return FakeHost(
            commands={
                ("ufw", "status", "numbered"): completed(("ufw", "status", "numbered"), status),
                ("ufw", "status", "verbose"): completed(("ufw", "status", "verbose"), "Status: active\nDefault: deny (incoming), allow (outgoing)\n"),
                owned_chain: completed(owned_chain, chain),
                shared_chain: completed(shared_chain, "-N DOCKER-USER\n" + _JUMP + "\n"),
                ("systemctl", "is-enabled", "gideon-docker-user"): completed(("systemctl", "is-enabled", "gideon-docker-user"), "enabled\n"),
                ("systemctl", "enable", "gideon-docker-user"): completed(("systemctl", "enable", "gideon-docker-user")),
                ("systemctl", "start", "gideon-docker-user"): completed(("systemctl", "start", "gideon-docker-user")),
                ("ufw", "reload"): completed(("ufw", "reload")),
            },
            files={
                "/etc/ufw/after.rules": UFW_AFTER_RULES + "\n" + _docker_user_block(["192.0.2.0/24"]),
                os.fspath(_DOCKER_USER_UNIT): _DOCKER_USER_UNIT_TEXT,
            },
        )

    def test_firewall_converges_with_the_docker_user_block_loaded(self) -> None:
        result = FirewallStep().check(context(self.firewall_converged_host()))
        self.assertEqual(result.disposition, Disposition.CONVERGED, result)

    def test_active_deny_and_reject_are_met_without_resetting_policy(self) -> None:
        for policy in ("deny", "reject"):
            with self.subTest(policy=policy):
                host = self.firewall_converged_host()
                host.commands[self.UFW_VERBOSE] = completed(
                    self.UFW_VERBOSE,
                    f"Status: active\nDefault: {policy} (incoming), allow (outgoing)\n",
                )
                step = FirewallStep()
                reading = step.check(context(host))
                self.assertEqual(reading.disposition, Disposition.CONVERGED)
                host.calls.clear()
                step.apply(context(host))
                self.assertEqual([call for call in host.calls if call[0] in {"write_text", "unlink", "mkdir"}], [])
                self.assertNotIn(("run", (("ufw", "default", "deny", "incoming"), True)), host.calls)
                self.assertNotIn(("run", (("ufw", "--force", "enable"), True)), host.calls)
                self.assertIn(("run", (("ufw", "reload"), True)), host.calls)

    def test_active_allow_refuses_before_rules_drift_or_mutation(self) -> None:
        host = self.firewall_converged_host()
        host.files.pop("/etc/ufw/after.rules")
        host.commands[self.UFW_VERBOSE] = completed(
            self.UFW_VERBOSE, "Status: active\nDefault: allow (incoming), allow (outgoing)\n"
        )
        self.assert_firewall_refusal(host, "firewall default policy", "allow (incoming)", "deny (incoming)")

    def test_unreadable_default_policy_refuses_before_mutation(self) -> None:
        for response in (
            completed(("ufw", "status", "verbose"), "Status: active\n"),
            subprocess.CompletedProcess(list(self.UFW_VERBOSE), 1, "", "fictitious ufw error"),
        ):
            with self.subTest(response=response.returncode, output=response.stdout):
                host = self.firewall_converged_host()
                host.commands[self.UFW_VERBOSE] = response
                self.assert_firewall_refusal(host, "ufw status verbose", "default incoming policy")

    def test_firewall_block_missing_or_stale_is_drift(self) -> None:
        host = self.firewall_converged_host()
        host.files["/etc/ufw/after.rules"] = UFW_AFTER_RULES
        result = FirewallStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertIn("firewall block in /etc/ufw/after.rules is missing", result.detail)
        host = self.firewall_converged_host()
        host.files["/etc/ufw/after.rules"] = host.files["/etc/ufw/after.rules"].replace("192.0.2.0/24 -p tcp", "198.51.100.0/24 -p tcp")
        result = FirewallStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertIn("differs from site.lan_cidrs", result.detail)

    def test_firewall_previous_markers_are_replaced_by_one_current_block(self) -> None:
        host = self.firewall_converged_host()
        host.files["/etc/ufw/after.rules"] = (
            UFW_AFTER_RULES
            + "\n# BEGIN gideon-provision docker-user\n"
            + "*filter\n:DOCKER-USER - [0:0]\nCOMMIT\n"
            + "# END gideon-provision docker-user\n"
        )
        reading = FirewallStep().check(context(host))
        self.assertEqual(reading.disposition, Disposition.DRIFT)
        self.assertIn("previous markers", reading.detail)
        FirewallStep().apply(context(host))
        after_rules = host.files["/etc/ufw/after.rules"]
        self.assertEqual(after_rules.count("# GIDEON BEGIN provision:firewall"), 1)
        self.assertEqual(after_rules.count("# GIDEON END provision:firewall"), 1)
        self.assertNotIn("# BEGIN gideon-provision docker-user", after_rules)
        self.assertNotIn("# END gideon-provision docker-user", after_rules)
        self.assertNotIn(":DOCKER-USER - [0:0]", after_rules)
        self.assertEqual(after_rules.count(":gideon-docker-user - [0:0]"), 1)
        self.assertEqual(FirewallStep().check(context(host)).disposition, Disposition.CONVERGED)
        host.write_modes.clear()
        FirewallStep().apply(context(host))
        self.assertEqual(host.write_modes, [])

    def test_firewall_chain_absent_or_inexact_is_drift(self) -> None:
        command = ("iptables", "-w", "-S", "gideon-docker-user")
        responses = (
            subprocess.CompletedProcess(list(command), 1, "", "fictitious absent chain"),
            completed(command, "-N gideon-docker-user\n-A gideon-docker-user -j RETURN\n"),
        )
        for response in responses:
            with self.subTest(returncode=response.returncode, stdout=response.stdout):
                host = self.firewall_converged_host()
                host.commands[command] = response
                result = FirewallStep().check(context(host))
                self.assertEqual(result.disposition, Disposition.DRIFT)
                self.assertIn("firewall chain gideon-docker-user does not carry its rules", result.detail)

    def test_firewall_unreadable_chain_or_unit_is_unfixable(self) -> None:
        class UnreadableCommandHost(FakeHost):
            unreadable_command: tuple[str, ...]

            def run(
                self,
                argv: Command,
                *,
                check: bool = False,
                input: str | None = None,
                cwd: PathLike | None = None,
                env: Mapping[str, str] | None = None,
                timeout: float | None = None,
                passthrough: bool = False,
            ) -> subprocess.CompletedProcess[str]:
                if tuple(argv) == self.unreadable_command:
                    raise OSError("fictitious command refusal")
                return super().run(
                    argv, check=check, input=input, cwd=cwd, env=env,
                    timeout=timeout, passthrough=passthrough,
                )

        class UnreadableUnitHost(FakeHost):
            def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
                if os.fspath(path) == os.fspath(_DOCKER_USER_UNIT):
                    raise PermissionError("fictitious unit refusal")
                return super().read_text(path, encoding=encoding)

        base = self.firewall_converged_host()
        for command in (
            ("iptables", "-w", "-S", "gideon-docker-user"),
            ("iptables", "-w", "-S", "DOCKER-USER"),
            ("systemctl", "is-enabled", "gideon-docker-user"),
        ):
            with self.subTest(command=command):
                command_host = UnreadableCommandHost(commands=base.commands, files=base.files)
                command_host.unreadable_command = command
                reading = FirewallStep().check(context(command_host))
                self.assertEqual(reading.disposition, Disposition.UNFIXABLE)
                self.assertIn("cannot read", reading.detail)
                self.assertIn("Rewrite GIDEON's firewall block", reading.fix)
        unit_host = UnreadableUnitHost(commands=base.commands, files=base.files)
        reading = FirewallStep().check(context(unit_host))
        self.assertEqual(reading.disposition, Disposition.UNFIXABLE)
        self.assertIn(os.fspath(_DOCKER_USER_UNIT), reading.detail)
        self.assertIn("Rewrite GIDEON's firewall block", reading.fix)

    def test_docker_user_rules_never_touch_container_egress_or_established_flows(self) -> None:
        rules = _docker_user_rules(["192.0.2.0/24", "198.51.100.0/24"])
        self.assertTrue(rules[0].startswith("-A gideon-docker-user -m conntrack --ctstate RELATED,ESTABLISHED"))
        self.assertEqual(rules[1:3], [
            "-A gideon-docker-user -i docker0 -m comment --comment gideon-provision -j RETURN",
            "-A gideon-docker-user -i br-+ -m comment --comment gideon-provision -j RETURN",
        ])
        self.assertTrue(rules[-1].endswith("-j RETURN"))
        drops = [rule for rule in rules if rule.endswith("-j DROP")]
        # One drop per published port per Docker bridge: only a packet forwarded
        # INTO a Docker bridge is judged. Forwarded traffic that leaves by any
        # other interface — the acceptance VM's NAT egress to an HTTPS host —
        # passes, whatever its destination port (the VM found this).
        self.assertEqual(len(drops), 4)
        self.assertTrue(all("-s " not in rule for rule in drops), "drops never judge by source address")
        self.assertEqual(
            sorted(rule.split(" -p ")[0] for rule in drops),
            sorted(f"-A gideon-docker-user -o {bridge}" for bridge in ("docker0", "br-+") for _ in (443, 5000)),
        )
        self.assertTrue(all("-o docker0" in rule or "-o br-+" in rule for rule in drops))
        self.assertEqual(sum("-s 198.51.100.0/24" in rule for rule in rules), 1)
        self.assertEqual(sum("192.168.122.0/24" in rule and "5000" in rule for rule in rules), 1)
        self.assertLess(max(index for index, rule in enumerate(rules) if "-j RETURN" in rule and "-s " in rule), rules.index(drops[0]))

    def test_firewall_foreign_rules_before_and_after_jump_are_preserved(self) -> None:
        host = self.firewall_converged_host()
        command = ("iptables", "-w", "-S", "DOCKER-USER")
        foreign_before = "-A DOCKER-USER -j RETURN"
        foreign_after = "-A DOCKER-USER -m comment --comment other-application -j RETURN"
        host.commands[command] = completed(
            command,
            "-N DOCKER-USER\n" + foreign_before + "\n" + _JUMP + "\n" + foreign_after + "\n",
        )
        self.assertEqual(FirewallStep().check(context(host)).disposition, Disposition.CONVERGED)
        host.calls.clear()
        FirewallStep().apply(context(host))
        self.assertFalse(any(
            command[:3] == ("iptables", "-w", "-D") for command, _, _ in host.runs
        ))

    def test_firewall_missing_jump_is_restored_by_start(self) -> None:
        host = self.firewall_converged_host()
        command = ("iptables", "-w", "-S", "DOCKER-USER")
        host.commands[command] = completed(command, "-N DOCKER-USER\n")
        reading = FirewallStep().check(context(host))
        self.assertEqual(reading.disposition, Disposition.DRIFT)
        self.assertIn("jump from DOCKER-USER into gideon-docker-user is missing", reading.detail)
        host.calls.clear()
        FirewallStep().apply(context(host))
        self.assertIn(("run", (("systemctl", "start", "gideon-docker-user"), True)), host.calls)
        self.assertLess(
            host.calls.index(("run", (("systemctl", "start", "gideon-docker-user"), True))),
            host.calls.index(("run", (command, True))),
        )

    def test_firewall_duplicate_jump_is_pruned_by_position(self) -> None:
        host = self.firewall_converged_host()
        command = ("iptables", "-w", "-S", "DOCKER-USER")
        foreign = "-A DOCKER-USER -j RETURN"
        host.commands[command] = completed(
            command, "-N DOCKER-USER\n" + _JUMP + "\n" + foreign + "\n" + _JUMP + "\n",
        )
        delete = ("iptables", "-w", "-D", "DOCKER-USER", "3")
        host.commands[delete] = completed(delete)
        reading = FirewallStep().check(context(host))
        self.assertEqual(reading.disposition, Disposition.DRIFT)
        self.assertIn("present more than once", reading.detail)
        host.calls.clear()
        FirewallStep().apply(context(host))
        self.assertEqual(
            [run for run, _, _ in host.runs if run[:3] == ("iptables", "-w", "-D")],
            [delete],
        )
        self.assertIn(("run", (delete, True)), host.calls)

    def test_firewall_previous_shared_rules_are_pruned_from_highest_position(self) -> None:
        host = self.firewall_converged_host()
        command = ("iptables", "-w", "-S", "DOCKER-USER")
        previous = [
            rule.replace("-A gideon-docker-user", "-A DOCKER-USER", 1)
            for rule in _docker_user_rules(["192.0.2.0/24"])
        ]
        self.assertEqual(len(previous), 10)
        host.commands[command] = completed(
            command, "-N DOCKER-USER\n" + _JUMP + "\n" + "\n".join(previous) + "\n",
        )
        expected_deletes = [
            ("iptables", "-w", "-D", "DOCKER-USER", str(position))
            for position in range(11, 1, -1)
        ]
        for delete in expected_deletes:
            host.commands[delete] = completed(delete)
        reading = FirewallStep().check(context(host))
        self.assertEqual(reading.disposition, Disposition.DRIFT)
        self.assertIn("previous rules remain in DOCKER-USER", reading.detail)
        host.calls.clear()
        FirewallStep().apply(context(host))
        deletes = [
            run for run, _, _ in host.runs if run[:3] == ("iptables", "-w", "-D")
        ]
        self.assertEqual(deletes, expected_deletes)
        self.assertNotIn(("iptables", "-w", "-D", "DOCKER-USER", "1"), deletes)

    def test_firewall_comment_prefix_is_foreign(self) -> None:
        host = self.firewall_converged_host()
        command = ("iptables", "-w", "-S", "DOCKER-USER")
        foreign = "-A DOCKER-USER -m comment --comment gideon-provision-x -j RETURN"
        host.commands[command] = completed(
            command, "-N DOCKER-USER\n" + _JUMP + "\n" + foreign + "\n",
        )
        self.assertEqual(FirewallStep().check(context(host)).disposition, Disposition.CONVERGED)
        host.calls.clear()
        FirewallStep().apply(context(host))
        self.assertFalse(any(
            run[:3] == ("iptables", "-w", "-D") for run, _, _ in host.runs
        ))

    def test_firewall_unit_missing_different_or_disabled(self) -> None:
        for state in ("missing", "different", "disabled"):
            with self.subTest(state=state):
                host = self.firewall_converged_host()
                unit = os.fspath(_DOCKER_USER_UNIT)
                if state == "missing":
                    host.files.pop(unit)
                elif state == "different":
                    host.files[unit] = "[Unit]\nDescription=fictitious old unit\n"
                else:
                    command = ("systemctl", "is-enabled", "gideon-docker-user")
                    host.commands[command] = subprocess.CompletedProcess(
                        list(command), 1, "disabled\n", ""
                    )
                daemon_reload = ("systemctl", "daemon-reload")
                host.commands[daemon_reload] = completed(daemon_reload)
                reading = FirewallStep().check(context(host))
                self.assertEqual(reading.disposition, Disposition.DRIFT)
                self.assertIn("gideon-docker-user.service", reading.detail)
                host.calls.clear()
                FirewallStep().apply(context(host))
                wrote_unit = any(path == unit for path, _ in host.write_modes)
                self.assertEqual(wrote_unit, state != "disabled")
                self.assertEqual(
                    ("run", (daemon_reload, True)) in host.calls,
                    state != "disabled",
                )
                self.assertIn(("run", (("systemctl", "enable", "gideon-docker-user"), True)), host.calls)
                self.assertEqual(host.files[unit], _DOCKER_USER_UNIT_TEXT)
                if wrote_unit:
                    self.assertIn((unit, 0o644), host.write_modes)

    def test_time_sync_source_change_reloads_chrony(self) -> None:
        package = ("dpkg-query", "-W", "-f=${Status} ${Version}\\n", "chrony")
        enabled = ("systemctl", "is-enabled", "chrony")
        active = ("systemctl", "is-active", "chrony")
        reload_sources = ("chronyc", "reload", "sources")
        enable = ("systemctl", "enable", "--now", "chrony")
        host = FakeHost(
            files={"/etc/chrony/sources.d/gideon.sources": "pool old.example iburst maxsources 3\n"},
            commands={
                package: completed(package, "install ok installed 1\n"),
                enabled: completed(enabled, "enabled\n"),
                active: completed(active, "active\n"),
                reload_sources: completed(reload_sources),
                enable: completed(enable),
                **dict(apt_command_results(["chrony"])),
            },
        )
        step = TimeSyncStep()
        self.assertEqual(step.check(context(host)).disposition, Disposition.DRIFT)
        step.apply(context(host))
        self.assertEqual(host.files["/etc/chrony/sources.d/gideon.sources"], "pool example.org iburst maxsources 3\n")
        self.assertIn(("run", (reload_sources, True)), host.calls)


class WaitOnlineStepTests(unittest.TestCase):
    """The boot-time network wait accepts any one link online."""

    DROPIN = os.fspath(_WAIT_ONLINE_DROPIN)

    def test_drop_in_truth_table(self) -> None:
        cases = (
            ("missing", None, Disposition.DRIFT, "is missing"),
            ("different", "[Service]\nExecStart=\n", Disposition.DRIFT, "differs"),
            ("present", _WAIT_ONLINE_TEXT, Disposition.CONVERGED, "has the any-link wait"),
        )
        for case, text, disposition, detail in cases:
            with self.subTest(case=case):
                host = FakeHost(files={} if text is None else {self.DROPIN: text})
                result = WaitOnlineStep().check(context(host))
                self.assertEqual(result.disposition, disposition)
                self.assertIn(detail, result.detail)
                self.assertEqual(result.fix, "" if disposition is Disposition.CONVERGED else _WAIT_ONLINE_FIX)
                # The unit's own state is never consulted: a check runs no command.
                self.assertEqual([call for call in host.calls if call[0] == "run"], [])

    def test_an_unreadable_drop_in_is_unfixable(self) -> None:
        class UnreadableHost(FakeHost):
            def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
                raise PermissionError(os.fspath(path))

        result = WaitOnlineStep().check(context(UnreadableHost()))
        self.assertEqual(result.disposition, Disposition.UNFIXABLE)
        self.assertIn("cannot read", result.detail)
        self.assertEqual(result.fix, _WAIT_ONLINE_FIX)

    def test_apply_writes_the_drop_in_then_reloads_and_resets_the_unit(self) -> None:
        reload = ("systemctl", "daemon-reload")
        reset = ("systemctl", "reset-failed", "systemd-networkd-wait-online.service")
        host = FakeHost(commands={reload: completed(reload), reset: completed(reset)})
        WaitOnlineStep().apply(context(host))
        self.assertEqual(
            host.calls,
            [
                ("mkdir", (os.fspath(_WAIT_ONLINE_DROPIN.parent), 0o755, True, True)),
                ("write_text", (self.DROPIN, _WAIT_ONLINE_TEXT)),
                ("run", (reload, True)),
                ("run", (reset, True)),
            ],
        )
        self.assertIn("--any", _WAIT_ONLINE_TEXT)
        self.assertEqual(WaitOnlineStep().check(context(host)).disposition, Disposition.CONVERGED)


class GideonCommandStepTests(unittest.TestCase):
    def test_command_file_truth_table(self) -> None:
        cases: tuple[
            tuple[
                str,
                dict[str, str],
                dict[str, os.stat_result],
                Disposition,
                str,
            ],
            ...,
        ] = (
            ("missing", {}, {}, Disposition.DRIFT, "missing"),
            (
                "different",
                {os.fspath(COMMAND_PATH): "#!/bin/sh\n"},
                {os.fspath(COMMAND_PATH): file_stat(COMMAND_MODE)},
                Disposition.DRIFT,
                "differs",
            ),
            (
                "wrong mode",
                {os.fspath(COMMAND_PATH): command_text(INSTALL_HOME)},
                {os.fspath(COMMAND_PATH): file_stat(0o644)},
                Disposition.DRIFT,
                "0644",
            ),
            (
                "converged",
                {os.fspath(COMMAND_PATH): command_text(INSTALL_HOME)},
                {os.fspath(COMMAND_PATH): file_stat(COMMAND_MODE)},
                Disposition.CONVERGED,
                "the release's command",
            ),
        )
        for name, files, stats, disposition, detail in cases:
            with self.subTest(name=name):
                result = GideonCommandStep().check(context(FakeHost(files=files, stats=stats)))
                self.assertEqual(result.disposition, disposition)
                self.assertIn(detail, result.detail)
                self.assertEqual(result.fix, "" if disposition is Disposition.CONVERGED else COMMAND_FIX)

    def test_unreadable_command_is_unfixable(self) -> None:
        class UnreadableHost(FakeHost):
            def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
                raise OSError(os.fspath(path))

        result = GideonCommandStep().check(context(UnreadableHost()))
        self.assertEqual(result.disposition, Disposition.UNFIXABLE)
        self.assertIn("cannot read", result.detail)
        self.assertIn("Repair access", result.fix)

    def test_apply_creates_command_directory_then_writes_executable(self) -> None:
        host = FakeHost()
        announcement = GideonCommandStep().apply(context(host))
        self.assertEqual(announcement, COMMAND_ANNOUNCEMENT)
        self.assertEqual(
            host.calls,
            [
                ("mkdir", ("/usr/local/bin", 0o755, True, True)),
                ("write_text", (os.fspath(COMMAND_PATH), command_text(INSTALL_HOME))),
            ],
        )
        self.assertEqual(host.write_modes, [(os.fspath(COMMAND_PATH), COMMAND_MODE)])


class TimezoneStepTests(unittest.TestCase):
    SHOW = ("timedatectl", "show", "-p", "Timezone", "--value")

    def test_site_absent_is_pending_input_with_the_shared_site_fix(self) -> None:
        host = FakeHost()
        result = TimezoneStep().check(replace(context(host), site=None))
        self.assertEqual(result.disposition, Disposition.PENDING_INPUT)
        self.assertEqual(result.fix, SITE_MISSING_FIX)

    def test_site_timezone_converges_and_check_runs_one_command(self) -> None:
        host = FakeHost()
        site = context(host).site
        assert site is not None
        host.commands[self.SHOW] = completed(self.SHOW, f"{site.office.timezone}\n")

        result = TimezoneStep().check(context(host))

        self.assertEqual(result.disposition, Disposition.CONVERGED)
        self.assertEqual(host.calls, [("run", (self.SHOW, False))])

    def test_different_timezone_drifts_and_apply_sets_the_site_timezone(self) -> None:
        host = FakeHost()
        site = context(host).site
        assert site is not None
        other_zone = "Etc/UTC"
        apply_command = ("timedatectl", "set-timezone", site.office.timezone)
        host.commands[self.SHOW] = completed(self.SHOW, f"{other_zone}\n")
        host.commands[apply_command] = completed(apply_command)

        result = TimezoneStep().check(context(host))
        TimezoneStep().apply(context(host))

        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertIn(other_zone, result.detail)
        self.assertIn(site.office.timezone, result.detail)
        self.assertEqual(
            host.calls,
            [
                ("run", (self.SHOW, False)),
                ("run", (self.SHOW, False)),
                ("run", (apply_command, True)),
            ],
        )

    def test_utc_alias_is_also_the_installed_default(self) -> None:
        host = FakeHost(commands={self.SHOW: completed(self.SHOW, "UTC\n")})
        reading = TimezoneStep().check(context(host))
        self.assertEqual(reading.disposition, Disposition.DRIFT)
        self.assertIn("installed default", reading.detail)

    def test_site_zone_is_met_without_a_write(self) -> None:
        host = FakeHost()
        site = context(host).site
        assert site is not None
        self.assertNotIn(site.office.timezone, ("Etc/UTC", "UTC"))
        host.commands[self.SHOW] = completed(self.SHOW, f"{site.office.timezone}\n")
        step = TimezoneStep()
        self.assertEqual(step.check(context(host)).disposition, Disposition.CONVERGED)
        host.calls.clear()
        step.apply(context(host))
        self.assertEqual(host.calls, [("run", (self.SHOW, False))])

    def test_moved_short_zone_refuses_before_mutation(self) -> None:
        host = FakeHost(commands={self.SHOW: completed(self.SHOW, "America/New_York\n")})
        site = context(host).site
        assert site is not None
        self.assertNotEqual(site.office.timezone, "America/New_York")
        step = TimezoneStep()
        reading = step.check(context(host))
        self.assertEqual(reading.disposition, Disposition.UNFIXABLE)
        self.assertIn("time zone", reading.detail)
        self.assertIn("America/New_York", reading.detail)
        self.assertIn(site.office.timezone, reading.detail)
        host.calls.clear()
        with self.assertRaises(StepFailure) as raised:
            step.apply(context(host))
        self.assertEqual((raised.exception.detail, raised.exception.fix), (reading.detail, reading.fix))
        assert_no_host_mutation(self, host, {self.SHOW})

    def test_timedatectl_failure_is_unfixable(self) -> None:
        host = FakeHost(
            commands={
                self.SHOW: subprocess.CompletedProcess(
                    list(self.SHOW), 1, "", "timedatectl failed\n"
                )
            }
        )

        result = TimezoneStep().check(context(host))

        self.assertEqual(result.disposition, Disposition.UNFIXABLE)
        self.assertIn("systemd-timedated", result.fix)
        self.assertEqual(host.calls, [("run", (self.SHOW, False))])
        host.calls.clear()
        with self.assertRaises(StepFailure) as raised:
            TimezoneStep().apply(context(host))
        self.assertEqual((raised.exception.detail, raised.exception.fix), (result.detail, result.fix))
        assert_no_host_mutation(self, host, {self.SHOW})


class MaintenanceStepTests(unittest.TestCase):
    PACKAGE = ("dpkg-query", "-W", "-f=${Status} ${Version}\\n", "unattended-upgrades")
    DROP_IN = os.fspath(_DROP_IN)

    def maintenance_host(
        self, *, values: Mapping[str, str] | None = None, drop_in: str | None = None
    ) -> FakeHost:
        output = "APT::Periodic \"\";\n"
        output += "".join(
            f'APT::Periodic::{key} "{value}";\n'
            for key, value in (values or {}).items()
        )
        files = {} if drop_in is None else {self.DROP_IN: drop_in}
        return FakeHost(
            files=files,
            commands={
                self.PACKAGE: completed(self.PACKAGE, "install ok installed 99.0-fictitious\n"),
                _PERIODIC: completed(_PERIODIC, output),
            },
        )

    def assert_periodic_refusal(self, host: FakeHost, *details: str) -> None:
        step = UnattendedUpgradesStep()
        reading = step.check(context(host))
        self.assertEqual(reading.disposition, Disposition.UNFIXABLE)
        for detail in details:
            self.assertIn(detail, reading.detail)
        host.calls.clear()
        with self.assertRaises(StepFailure) as raised:
            step.apply(context(host))
        self.assertEqual((raised.exception.detail, raised.exception.fix), (reading.detail, reading.fix))
        assert_no_host_mutation(self, host, {self.PACKAGE, _PERIODIC})

    def test_unattended_upgrades_installs_and_writes_only_its_dropin(self) -> None:
        install = ("apt-get", "install", "-y", "unattended-upgrades")
        host = FakeHost(commands={
            self.PACKAGE: completed(self.PACKAGE),
            _PERIODIC: completed(_PERIODIC, "APT::Periodic \"\";\n"),
            **dict(apt_command_results(["unattended-upgrades"])),
        })
        step = UnattendedUpgradesStep()
        self.assertEqual(step.check(context(host)).disposition, Disposition.DRIFT)
        step.apply(context(host))
        self.assertIn(("run", (install, True)), host.calls)
        self.assertIn(("run", (_PERIODIC, False)), host.calls)
        self.assertEqual(
            host.files[self.DROP_IN],
            'APT::Periodic::Update-Package-Lists "1";\n'
            'APT::Periodic::Unattended-Upgrade "1";\n',
        )

    def test_zero_without_a_drop_in_refuses_before_mutation(self) -> None:
        host = self.maintenance_host(values={
            "Update-Package-Lists": "0", "Unattended-Upgrade": "1"
        })
        self.assert_periodic_refusal(host, "apt periodic triggers", "Update-Package-Lists 0", "a value other than 0")

    def test_zero_with_a_clean_drop_in_refuses_before_mutation(self) -> None:
        host = self.maintenance_host(
            values={"Update-Package-Lists": "0", "Unattended-Upgrade": "1"},
            drop_in='APT::Periodic::Update-Package-Lists "1";\n',
        )
        self.assert_periodic_refusal(host, "apt periodic triggers", "Update-Package-Lists 0", "a value other than 0")

    def test_zero_in_a_stale_drop_in_rewrites_only_its_key(self) -> None:
        host = self.maintenance_host(
            values={"Update-Package-Lists": "0", "Unattended-Upgrade": "7"},
            drop_in='APT::Periodic::Update-Package-Lists "0";\n',
        )
        step = UnattendedUpgradesStep()
        reading = step.check(context(host))
        self.assertEqual(reading.disposition, Disposition.DRIFT)
        self.assertIn("holds a line GIDEON does not write", reading.detail)
        host.calls.clear()
        step.apply(context(host))
        self.assertEqual(host.files[self.DROP_IN], 'APT::Periodic::Update-Package-Lists "1";\n')
        self.assertEqual(
            [call for call in host.calls if call[0] == "write_text"],
            [("write_text", (self.DROP_IN, 'APT::Periodic::Update-Package-Lists "1";\n'))],
        )

    def test_weekly_lists_with_unset_upgrade_writes_only_the_unset_key(self) -> None:
        host = self.maintenance_host(values={"Update-Package-Lists": "7"})
        step = UnattendedUpgradesStep()
        self.assertEqual(step.check(context(host)).disposition, Disposition.DRIFT)
        step.apply(context(host))
        self.assertEqual(host.files[self.DROP_IN], 'APT::Periodic::Unattended-Upgrade "1";\n')

    def test_both_unset_write_both_lines(self) -> None:
        host = self.maintenance_host()
        step = UnattendedUpgradesStep()
        self.assertEqual(step.check(context(host)).disposition, Disposition.DRIFT)
        step.apply(context(host))
        self.assertEqual(
            host.files[self.DROP_IN],
            'APT::Periodic::Update-Package-Lists "1";\n'
            'APT::Periodic::Unattended-Upgrade "1";\n',
        )

    def test_both_on_without_a_drop_in_are_met_without_a_write(self) -> None:
        host = self.maintenance_host(values={
            "Update-Package-Lists": "7", "Unattended-Upgrade": "1"
        })
        step = UnattendedUpgradesStep()
        reading = step.check(context(host))
        self.assertEqual(reading.disposition, Disposition.CONVERGED)
        self.assertIn("Update-Package-Lists 7", reading.detail)
        host.calls.clear()
        step.apply(context(host))
        assert_no_host_mutation(self, host, {self.PACKAGE, _PERIODIC})
        self.assertNotIn(self.DROP_IN, host.files)

    def test_failing_apt_config_refuses_before_mutation(self) -> None:
        host = self.maintenance_host()
        host.commands[_PERIODIC] = subprocess.CompletedProcess(
            list(_PERIODIC), 2, "", "fictitious apt error\nsecond line\n"
        )
        self.assert_periodic_refusal(host, "apt-config", "fictitious apt error")


class HostToolsStepTests(unittest.TestCase):
    LDAP = ("dpkg-query", "-W", "-f=${Status} ${Version}\\n", "ldap-utils")
    SKOPEO = ("dpkg-query", "-W", "-f=${Status} ${Version}\\n", "skopeo")
    AGE = ("dpkg-query", "-W", "-f=${Status} ${Version}\\n", "age")
    RSYNC = ("dpkg-query", "-W", "-f=${Status} ${Version}\\n", "rsync")

    def test_missing_tools_install_together_and_converge_when_present(self) -> None:
        update = ("apt-get", "update")
        install = ("apt-get", "install", "-y", "ldap-utils", "skopeo", "age", "rsync")
        host = FakeHost(commands=dict(apt_command_results((
            "ldap-utils", "skopeo", "age", "rsync",
        ))))
        step = HostToolsStep()
        result = step.check(context(host))
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertIn("ldap-utils, skopeo, age, rsync", result.detail)
        self.assertIn("apt-get install -y ldap-utils skopeo age rsync", result.fix)
        step.apply(context(host))
        # The lists are refreshed before the install: a fresh cloud image ships
        # none, and skopeo and age live in universe (the acceptance VM found this).
        runs = [command for command, _cwd, _env in host.runs]
        self.assertLess(runs.index(update), runs.index(install))
        self.assertIn(("run", (install, True)), host.calls)
        host.commands[self.LDAP] = completed(self.LDAP, "hold ok installed 2.6.7\n")
        host.commands[self.SKOPEO] = completed(self.SKOPEO, "install ok installed 1.21.0\n")
        host.commands[self.AGE] = completed(self.AGE, "install ok installed 1.0.0\n")
        host.commands[self.RSYNC] = completed(self.RSYNC, "install ok installed 3.2.7\n")
        self.assertEqual(step.check(context(host)).disposition, Disposition.CONVERGED)

    def test_only_the_missing_tool_is_installed(self) -> None:
        install = ("apt-get", "install", "-y", "skopeo")
        host = FakeHost(
            commands={
                self.LDAP: completed(self.LDAP, "install ok installed 2.6.7\n"),
                self.AGE: completed(self.AGE, "install ok installed 1.0.0\n"),
                self.RSYNC: completed(self.RSYNC, "install ok installed 3.2.7\n"),
                **dict(apt_command_results(["skopeo"])),
            }
        )
        step = HostToolsStep()
        self.assertIn("skopeo", step.check(context(host)).detail)
        step.apply(context(host))
        self.assertIn(("run", (install, True)), host.calls)


class AgeRecipientStepTests(unittest.TestCase):
    RECIPIENT = "age1" + "a" * 58
    IDENTITY = "AGE-SECRET-KEY-1" + "a" * 58

    def test_check_truth_table(self) -> None:
        step = AgeRecipientStep()
        missing = FakeHost()
        self.assertEqual(step.check(context(missing)).disposition, Disposition.DRIFT)

        valid = FakeHost(files={os.fspath(AGE_RECIPIENT_PATH): self.RECIPIENT + "\n"})
        result = step.check(context(valid))
        self.assertEqual(result.disposition, Disposition.CONVERGED)
        self.assertIn(self.RECIPIENT, result.detail)

        malformed = FakeHost(
            files={os.fspath(AGE_RECIPIENT_PATH): "age1not-valid\n"}
        )
        result = step.check(context(malformed))
        self.assertEqual(result.disposition, Disposition.UNFIXABLE)
        self.assertIn("every earlier tarball", result.fix)
        self.assertIn("office password manager", result.fix)

    def test_apply_runs_age_keygen_writes_only_recipient_and_returns_identity(self) -> None:
        command = ("age-keygen",)
        output = f"# public key: {self.RECIPIENT}\n{self.IDENTITY}\n"
        host = FakeHost(commands={command: completed(command, output)})

        announcement = AgeRecipientStep().apply(context(host))

        self.assertIsNotNone(announcement)
        match = AGE_IDENTITY_LINE.fullmatch(announcement or "")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group("value"), self.IDENTITY)
        self.assertIn(("run", (command, True)), host.calls)
        self.assertEqual(
            host.files[os.fspath(AGE_RECIPIENT_PATH)], self.RECIPIENT + "\n"
        )
        self.assertEqual(
            [key for key in host.files if key == os.fspath(AGE_RECIPIENT_PATH)],
            [os.fspath(AGE_RECIPIENT_PATH)],
        )

        host.calls.clear()
        self.assertIsNone(AgeRecipientStep().apply(context(host)))
        self.assertNotIn(("run", (command, True)), host.calls)


class AgeIdentityStepTests(unittest.TestCase):
    IDENTITY = "AGE-SECRET-KEY-1" + ("ACDEFGHJKLMNPQRSTUVWXYZ023456789" * 2)[:58]

    def test_check_truth_table_keeps_identity_content_private(self) -> None:
        step = AgeIdentityStep()
        missing = step.check(context(FakeHost()))
        self.assertEqual(missing.disposition, Disposition.DRIFT)
        self.assertEqual(missing.fix, "Run sudo python3 -m gideon host provision --only age-identity.")

        valid = FakeHost(
            files={os.fspath(AGE_IDENTITY_PATH): self.IDENTITY + "\n"},
            stats={os.fspath(AGE_IDENTITY_PATH): file_stat(AGE_IDENTITY_MODE, 0, 0)},
        )
        result = step.check(context(valid))
        self.assertEqual(result.disposition, Disposition.CONVERGED)
        self.assertNotIn(self.IDENTITY, result.detail)
        self.assertNotIn(self.IDENTITY, result.fix)

    def test_mode_or_owner_drift_is_repaired_without_keygen(self) -> None:
        for details in (
            file_stat(0o644, 0, 0),
            file_stat(AGE_IDENTITY_MODE, 1000, 1000),
        ):
            with self.subTest(details=details):
                host = FakeHost(
                    files={os.fspath(AGE_IDENTITY_PATH): self.IDENTITY + "\n"},
                    stats={os.fspath(AGE_IDENTITY_PATH): details},
                )
                result = AgeIdentityStep().check(context(host))
                self.assertEqual(result.disposition, Disposition.DRIFT)
                AgeIdentityStep().apply(context(host))
                self.assertIn(("chmod", (os.fspath(AGE_IDENTITY_PATH), AGE_IDENTITY_MODE)), host.calls)
                self.assertIn(("chown", (os.fspath(AGE_IDENTITY_PATH), 0, 0)), host.calls)
                self.assertNotIn(("run", (("age-keygen",), True)), host.calls)

    def test_malformed_content_is_unfixable_without_echoing_identity(self) -> None:
        result = AgeIdentityStep().check(
            context(
                FakeHost(
                    files={os.fspath(AGE_IDENTITY_PATH): "not-an-identity\n"},
                )
            )
        )
        self.assertEqual(result.disposition, Disposition.UNFIXABLE)
        self.assertEqual(result.fix, AGE_IDENTITY_FIX)
        self.assertNotIn("not-an-identity", result.detail)

    def test_apply_mints_only_the_secret_line(self) -> None:
        command = ("age-keygen",)
        public = "age1" + "a" * 58
        host = FakeHost(
            commands={
                command: completed(command, f"# public key: {public}\n{self.IDENTITY}\n"),
            }
        )

        step: Step = AgeIdentityStep()
        announcement = step.apply(context(host))
        self.assertIsNone(announcement)
        self.assertIn(("run", (command, True)), host.calls)
        self.assertEqual(host.files[os.fspath(AGE_IDENTITY_PATH)], self.IDENTITY + "\n")
        self.assertEqual(host.write_modes, [(os.fspath(AGE_IDENTITY_PATH), AGE_IDENTITY_MODE)])
        self.assertIn(("chown", (os.fspath(AGE_IDENTITY_PATH), 0, 0)), host.calls)
        self.assertNotIn(f"# public key: {public}", host.files[os.fspath(AGE_IDENTITY_PATH)])

        host.calls.clear()
        announcement = step.apply(context(host))
        self.assertIsNone(announcement)
        self.assertNotIn(("run", (command, True)), host.calls)


class SecretsStepTests(unittest.TestCase):
    def test_secrets_dirs_requires_service_user_and_reports_group_drift(self) -> None:
        self.assertEqual(SecretsDirsStep.requires, ("service-user",))
        directory_paths = ("/etc/gideon", "/etc/gideon/tls", "/etc/gideon/rendered", "/etc/gideon/secrets")
        stats = {path: directory_stat(0o755) for path in directory_paths[:-1]}
        stats[directory_paths[-1]] = directory_stat(0o700)
        stats["/etc/gideon/secrets/supplied"] = file_stat(0o440, 0, 7)
        host = FakeHost(
            files={"/etc/gideon/secrets/supplied": "secret\n"},
            stats=stats,
            commands={("getent", "group", "gideon"): completed(("getent", "group", "gideon"), "gideon:x:4242:\n")},
        )
        result = SecretsDirsStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertIn("ownership", result.detail)

    def test_secrets_dirs_normalizes_directory_and_file_modes(self) -> None:
        directory_paths = ("/etc/gideon", "/etc/gideon/tls", "/etc/gideon/rendered", "/etc/gideon/secrets")
        stats = {path: directory_stat(0o755) for path in directory_paths[:-1]}
        stats[directory_paths[-1]] = directory_stat(0o755)
        files = {"/etc/gideon/secrets/example": "secret\n"}
        stats["/etc/gideon/secrets/example"] = file_stat(0o644)
        group = ("getent", "group", "gideon")
        host = FakeHost(
            files=files,
            stats=stats,
            commands={group: completed(group, "gideon:x:4242:\n")},
        )
        step = SecretsDirsStep()
        result = step.check(context(host))
        self.assertEqual(result.disposition, Disposition.DRIFT)
        step.apply(context(host))
        self.assertIn(("chmod", ("/etc/gideon/secrets", 0o700)), host.calls)
        self.assertIn(("chmod", ("/etc/gideon/secrets/example", 0o440)), host.calls)
        self.assertIn(("chown", ("/etc/gideon/secrets/example", 0, 4242)), host.calls)

    def test_secrets_dirs_exempts_backup_keypair_but_converges_supplied_secret_group(self) -> None:
        directory_paths = ("/etc/gideon", "/etc/gideon/tls", "/etc/gideon/rendered", "/etc/gideon/secrets")
        stats = {path: directory_stat(0o755) for path in directory_paths[:-1]}
        stats[directory_paths[-1]] = directory_stat(0o700)
        files = {
            "/etc/gideon/secrets/backup_ssh_key": "private\n",
            "/etc/gideon/secrets/backup_ssh_key.pub": "public\n",
            "/etc/gideon/secrets/supplied": "secret\n",
        }
        stats.update(
            {
                "/etc/gideon/secrets/backup_ssh_key": file_stat(0o400, 998, 998),
                "/etc/gideon/secrets/backup_ssh_key.pub": file_stat(0o440, 998, 998),
                "/etc/gideon/secrets/supplied": file_stat(0o440, 0, 4242),
            }
        )
        group = ("getent", "group", "gideon")
        host = FakeHost(
            files=files,
            stats=stats,
            commands={group: completed(group, "gideon:x:4242:\n")},
        )
        self.assertEqual(SecretsDirsStep().check(context(host)).disposition, Disposition.CONVERGED)

    def test_backup_keypair_public_line_is_reported_when_converged_and_drifted(self) -> None:
        passwd = ("getent", "passwd", "gideon")
        public = "ssh-ed25519 AAAATEST gideon-backup"
        files = {
            "/etc/gideon/secrets/backup_ssh_key": "private",
            "/etc/gideon/secrets/backup_ssh_key.pub": public + "\n",
        }
        stats = {
            "/etc/gideon/secrets/backup_ssh_key": file_stat(0o400),
            "/etc/gideon/secrets/backup_ssh_key.pub": file_stat(0o440),
        }
        host = FakeHost(
            files=files,
            stats=stats,
            commands={passwd: completed(passwd, "gideon:x:998:998::/nonexistent:/usr/sbin/nologin\n")},
        )
        step = BackupKeypairStep()
        result = step.check(context(host))
        self.assertEqual(result.disposition, Disposition.CONVERGED)
        self.assertIn(public, result.detail)

        host.stats["/etc/gideon/secrets/backup_ssh_key"] = file_stat(0o644)
        drifted = step.check(context(host))
        self.assertEqual(drifted.disposition, Disposition.DRIFT)
        self.assertIn(public, drifted.detail)

    def test_backup_keypair_apply_generates_and_owns_both_halves(self) -> None:
        passwd = ("getent", "passwd", "gideon")
        generate = (
            "ssh-keygen", "-t", "ed25519", "-N", "", "-C", "gideon-backup",
            "-f", "/etc/gideon/secrets/backup_ssh_key",
        )
        host = FakeHost(
            commands={
                generate: completed(generate),
                passwd: completed(passwd, "gideon:x:998:998::/nonexistent:/usr/sbin/nologin\n"),
            }
        )
        BackupKeypairStep().apply(context(host))
        self.assertIn(("run", (generate, True)), host.calls)
        self.assertIn(("chmod", ("/etc/gideon/secrets/backup_ssh_key", 0o400)), host.calls)
        self.assertIn(("chown", ("/etc/gideon/secrets/backup_ssh_key.pub", 998, 998)), host.calls)


def acceptance_image() -> str:
    """The KVM step's local image path: the lock URL's basename, never typed."""
    return str(acceptance_image_path(HOST_LOCK))


def kvm_commands() -> dict[tuple[str, ...], subprocess.CompletedProcess[str]]:
    image = acceptance_image()
    commands: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {
        ("dpkg-query", "-W", "-f=${Status} ${Version}\\n", package): completed(
            ("dpkg-query", "-W"), "install ok installed 1\\n"
        )
        for package in (
            "qemu-system-x86",
            "libvirt-daemon-system",
            "guestfs-tools",
            "libguestfs-tools",
            "cloud-image-utils",
        )
    }
    commands.update({
        ("systemctl", "is-enabled", "libvirtd"): completed(("systemctl", "is-enabled", "libvirtd"), "enabled\n"),
        ("systemctl", "is-active", "libvirtd"): completed(("systemctl", "is-active", "libvirtd"), "active\n"),
        ("virsh", "net-info", "default"): completed(("virsh", "net-info", "default"), "Name: default\nActive: yes\nAutostart: yes\n"),
        ("sha256sum", image): completed(("sha256sum", image), "0" * 64 + "  " + image + "\n"),
    })
    commands.update(dict(apt_command_results((
        "qemu-system-x86", "libvirt-daemon-system", "guestfs-tools",
        "libguestfs-tools", "cloud-image-utils",
    ))))
    return commands


RUNNER_UNIT = "actions.runner.TNMD-FDO.gideon.service"
RUNNER_FIXTURES = ROOT / "tests/fixtures/host/gh-runner"


def runner_settings_fixture(which: bool | str) -> str:
    """A recorded .runner file, decoded so the runner's byte-order mark stays in the text."""

    name = "runner-updates-off.json" if which == "off" else "runner-updates-on.json"
    return (RUNNER_FIXTURES / name).read_bytes().decode("utf-8")


RUNNER_REMOVE_ARGV = ("runuser", "-u", "gh-runner", "--", "./config.sh", "remove", "--local")
RUNNER_SUDOERS = "/etc/sudoers.d/gideon-acceptance"
RUNNER_SUDOERS_CANDIDATE = "/etc/sudoers.d/gideon-acceptance.candidate"
RUNNER_SUDOERS_TEXT = (
    "gh-runner ALL=(root) NOPASSWD: /usr/bin/python3 -m tools.acceptance *\n"
    "gh-runner ALL=(root) NOPASSWD: /usr/bin/python3 -B -m tools.cistack smoke\n"
    "gh-runner ALL=(root) NOPASSWD: /usr/bin/unshare --mount --net -- sh -c *\n"
)
RUNNER_VENV_QUERY = ("dpkg-query", "-W", "-f=${Status} ${Version}\\n", "python3-venv")
RUNNER_VENV_INSTALL = ("apt-get", "install", "-y", "python3-venv")
RUNNER_CONFIG_ARGV = (
    "runuser", "-u", "gh-runner", "--", "./config.sh", "--unattended", "--replace",
    "--disableupdate", "--url", "https://github.com/TNMD-FDO", "--labels",
    "self-hosted,linux,x64,gpu,dl385-gen11",
)


class RunnerFakeHost(FakeHost):
    """A FakeHost whose runner scripts leave the files the real ones do."""

    def run(
        self,
        argv: Command,
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        result = super().run(argv, check=check, input=input, cwd=cwd, env=env, timeout=timeout)
        command = tuple(argv)
        if result.returncode != 0:
            return result
        if command == ("./svc.sh", "install", "gh-runner"):
            self.files["/opt/gh-runner/.service"] = RUNNER_UNIT + "\n"
        elif command == ("./svc.sh", "uninstall"):
            self.files.pop("/opt/gh-runner/.service", None)
        elif command == RUNNER_REMOVE_ARGV:
            self.files.pop("/opt/gh-runner/.runner", None)
            self.files.pop("/opt/gh-runner/.runner_migrated", None)
        elif command == RUNNER_CONFIG_ARGV:
            self.files["/opt/gh-runner/.runner"] = runner_settings_fixture("off")
        elif command == ("mv", "-f", RUNNER_SUDOERS_CANDIDATE, RUNNER_SUDOERS):
            self.files[RUNNER_SUDOERS] = self.files.pop(RUNNER_SUDOERS_CANDIDATE)
            self.stats[RUNNER_SUDOERS] = file_stat(0o440, 0, 0)
        elif command == RUNNER_VENV_INSTALL:
            self.commands[RUNNER_VENV_QUERY] = completed(
                RUNNER_VENV_QUERY, "install ok installed 1\n"
            )
        return result


class ServiceStepTests(unittest.TestCase):
    def test_kvm_check_names_missing_acceptance_package(self) -> None:
        commands = kvm_commands()
        missing = ("dpkg-query", "-W", "-f=${Status} ${Version}\\n", "guestfs-tools")
        del commands[missing]

        result = KvmStep().check(context(FakeHost(files={acceptance_image(): "image"}, commands=commands)))

        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertIn("guestfs-tools", result.detail)

    def test_kvm_checksum_mismatch_unlinks_and_redownloads(self) -> None:
        image = acceptance_image()
        commands = kvm_commands()
        commands[("sha256sum", image)] = completed(("sha256sum", image), "f" * 64 + "  " + image + "\n")
        host = FakeHost(files={image: "bad"}, commands=commands)
        digest = HOST_LOCK.acceptance_vm_image.sha256
        step = KvmStep()
        self.assertEqual(step.check(context(host)).disposition, Disposition.DRIFT)

        commands[("sha256sum", image)] = completed(("sha256sum", image), digest + "  " + image + "\n")
        download = ("wget", "-qO", image, HOST_LOCK.acceptance_vm_image.url)
        commands[download] = completed(download)
        commands[("systemctl", "enable", "--now", "libvirtd")] = completed(("systemctl", "enable", "--now", "libvirtd"))
        del host.files[image]
        host.commands = commands
        step.apply(context(host))
        self.assertIn(("unlink", image), host.calls)
        self.assertIn(("run", (download, True)), host.calls)

    def test_registry_unit_change_restarts_and_adds_its_own_ufw_tag(self) -> None:
        unit = "/etc/systemd/system/gideon-registry.service"
        pull = ("docker", "pull", HOST_LOCK.registry_image)
        ufw_status = ("ufw", "status", "numbered")
        ufw_add = ("ufw", "allow", "proto", "tcp", "from", "192.168.122.0/24", "to", "any", "port", "5000", "comment", "gideon-registry")
        daemon_reload = ("systemctl", "daemon-reload")
        restart = ("systemctl", "restart", "gideon-registry")
        enable = ("systemctl", "enable", "--now", "gideon-registry")
        commands = {
            pull: completed(pull),
            ufw_status: completed(ufw_status, "Status: active\n"),
            ufw_add: completed(ufw_add),
            daemon_reload: completed(daemon_reload),
            restart: completed(restart),
            enable: completed(enable),
        }
        host = FakeHost(files={unit: "old unit\n"}, commands=commands)
        RegistryStep().apply(context(host))
        self.assertIn("--name gideon-registry", host.files[unit])
        self.assertIn(("run", (ufw_add, True)), host.calls)
        self.assertIn(("run", (daemon_reload, True)), host.calls)
        self.assertIn(("run", (restart, True)), host.calls)
        self.assertNotIn("gideon-provision", ufw_add)

    def _runner_host(
        self,
        *,
        registered: bool | str = False,
        runner_migrated: bool | str = False,
        token: bool = False,
        busy: bool = False,
        installed_version: str | None = None,
        service: bool = False,
        service_enabled: bool = True,
        service_active: bool = True,
        manifest: bool = True,
    ) -> FakeHost:
        image = f"/opt/gh-runner/actions-runner-linux-x64-{HOST_LOCK.gh_runner.version}.tar.gz"
        files = {
            image: "archive",
            "/opt/gh-runner/config.sh": "#!/bin/sh\n",
        }
        if registered:
            files["/opt/gh-runner/.runner"] = runner_settings_fixture(registered)
        if runner_migrated:
            files["/opt/gh-runner/.runner_migrated"] = runner_settings_fixture(
                registered if runner_migrated is True else runner_migrated
            )
        if token:
            files["/etc/gideon/secrets/gh_runner_token"] = "token\n"
        if manifest:
            version = installed_version or HOST_LOCK.gh_runner.version
            files["/opt/gh-runner/bin/Runner.Listener.deps.json"] = json.dumps(
                {"targets": {"runner": {f"Runner.Listener/{version}": {}}}}
            )
        if service:
            files["/opt/gh-runner/.service"] = RUNNER_UNIT + "\n"
        files[RUNNER_SUDOERS] = RUNNER_SUDOERS_TEXT
        tar = ("tar", "-xzf", image)
        stop = ("./svc.sh", "stop")
        uninstall = ("./svc.sh", "uninstall")
        remove = RUNNER_REMOVE_ARGV
        config = RUNNER_CONFIG_ARGV
        install = ("./svc.sh", "install", "gh-runner")
        enable = ("systemctl", "enable", RUNNER_UNIT)
        start = ("./svc.sh", "start")
        commands = {
            ("getent", "passwd", "gh-runner"): completed(("getent", "passwd", "gh-runner"), "gh-runner:x:997:997::/home/gh-runner:/usr/sbin/nologin\n"),
            ("getent", "group", "docker"): completed(("getent", "group", "docker"), "docker:x:999:gh-runner\n"),
            RUNNER_VENV_QUERY: completed(RUNNER_VENV_QUERY, "install ok installed 1\n"),
            ("sha256sum", image): completed(
                ("sha256sum", image), HOST_LOCK.gh_runner.sha256 + "  " + image + "\n"
            ),
            ("pgrep", "-u", "gh-runner", "-x", "Runner.Worker"): subprocess.CompletedProcess(
                ["pgrep", "-u", "gh-runner", "-x", "Runner.Worker"],
                0 if busy else 1,
                "",
                "",
            ),
            ("systemctl", "is-enabled", RUNNER_UNIT): subprocess.CompletedProcess(
                ["systemctl", "is-enabled", RUNNER_UNIT],
                0 if service_enabled else 1,
                "",
                "",
            ),
            ("systemctl", "is-active", RUNNER_UNIT): subprocess.CompletedProcess(
                ["systemctl", "is-active", RUNNER_UNIT],
                0 if service_active else 1,
                "",
                "",
            ),
            tar: completed(tar),
            ("cp", "./bin/runsvc.sh", "./runsvc.sh"): completed(
                ("cp", "./bin/runsvc.sh", "./runsvc.sh")
            ),
            stop: completed(stop),
            uninstall: completed(uninstall),
            remove: completed(remove),
            config: completed(config),
            install: completed(install),
            enable: completed(enable),
            start: completed(start),
            (
                "chown",
                "-R",
                "gh-runner:gh-runner",
                "/opt/gh-runner",
            ): completed(("chown", "-R")),
            ("visudo", "-c", "-f", RUNNER_SUDOERS_CANDIDATE): completed(
                ("visudo", "-c", "-f", RUNNER_SUDOERS_CANDIDATE)
            ),
            ("mv", "-f", RUNNER_SUDOERS_CANDIDATE, RUNNER_SUDOERS): completed(
                ("mv", "-f", RUNNER_SUDOERS_CANDIDATE, RUNNER_SUDOERS)
            ),
        }
        commands.update(dict(apt_command_results(["python3-venv"])))
        stats = {
            "/opt/gh-runner": directory_stat(0o755, 997, 997),
            RUNNER_SUDOERS: file_stat(0o440, 0, 0),
        }
        return RunnerFakeHost(files=files, stats=stats, commands=commands)

    def test_gh_runner_without_token_is_pending_input(self) -> None:
        result = GhRunnerStep().check(context(self._runner_host()))
        self.assertEqual(result.disposition, Disposition.PENDING_INPUT)
        self.assertIn("gh_runner_token", result.fix)

    def test_unregistered_runner_registers_with_token_in_child_environment(self) -> None:
        host = self._runner_host(token=True)
        step = GhRunnerStep()

        self.assertEqual(step.check(context(host)).disposition, Disposition.DRIFT)
        self.assertIsNone(step.apply(context(host)))

        config_runs = [run for run in host.runs if run[0] == RUNNER_CONFIG_ARGV]
        self.assertEqual(len(config_runs), 1)
        config, cwd, env = config_runs[0]
        self.assertIn("--replace", config)
        self.assertIn("--disableupdate", config)
        self.assertFalse(any("token" in argv for argv, _, _ in host.runs))
        self.assertEqual(cwd, Path("/opt/gh-runner"))
        assert env is not None
        self.assertEqual(env[_RUNNER_TOKEN_VARIABLE], "token")
        self.assertIn("PATH", env)
        self.assertNotIn("/etc/gideon/secrets/gh_runner_token", host.files)

    def test_registered_runner_reregisters_in_order_and_announces(self) -> None:
        host = self._runner_host(registered=True, token=True, service=True)
        step = GhRunnerStep()

        self.assertEqual(step.check(context(host)).disposition, Disposition.DRIFT)
        self.assertEqual(step.apply(context(host)), _RUNNER_REREGISTERED_ANNOUNCEMENT)

        stop = ("./svc.sh", "stop")
        uninstall = ("./svc.sh", "uninstall")
        remove = RUNNER_REMOVE_ARGV
        config = RUNNER_CONFIG_ARGV
        install = ("./svc.sh", "install", "gh-runner")
        enable = ("systemctl", "enable", RUNNER_UNIT)
        start = ("./svc.sh", "start")
        expected = [stop, uninstall, remove, config, install, enable, start]
        actual = [
            command for command, _, _ in host.runs
            if command in {stop, uninstall, remove, config, install, enable, start}
        ]
        self.assertEqual(actual, expected)
        self.assertNotIn("/etc/gideon/secrets/gh_runner_token", host.files)

    def test_registered_busy_runner_is_not_stopped(self) -> None:
        host = self._runner_host(registered=True, token=True, service=True, busy=True)
        with self.assertRaises(StepFailure) as raised:
            GhRunnerStep().apply(context(host))
        self.assertEqual(raised.exception.fix, _RUNNER_WAIT_FIX)
        self.assertNotIn(("./svc.sh", "stop"), [run[0] for run in host.runs])

    def test_registration_refusal_keeps_token_and_carries_diagnostic(self) -> None:
        host = self._runner_host(token=True)
        host.commands[RUNNER_CONFIG_ARGV] = subprocess.CompletedProcess(
            list(RUNNER_CONFIG_ARGV), 1, "", "registration token expired\n"
        )
        with self.assertRaises(StepFailure) as raised:
            GhRunnerStep().apply(context(host))
        self.assertEqual(raised.exception.fix, _RUNNER_FRESH_TOKEN_FIX)
        self.assertIn("registration token expired", raised.exception.detail)
        self.assertIn("/etc/gideon/secrets/gh_runner_token", host.files)

    def test_version_drift_extracts_and_restarts_without_configure(self) -> None:
        host = self._runner_host(
            registered="off", installed_version="0.0.0", service=True
        )
        image = f"/opt/gh-runner/actions-runner-linux-x64-{HOST_LOCK.gh_runner.version}.tar.gz"
        old_archive = "/opt/gh-runner/actions-runner-linux-x64-old.tar.gz"
        host.files[old_archive] = "old archive"
        host.files["/opt/gh-runner/bin/runsvc.sh"] = "new wrapper"
        host.files["/opt/gh-runner/runsvc.sh"] = "old wrapper"
        before = host.files.get("/opt/gh-runner/.runner", None)

        self.assertIsNone(GhRunnerStep().apply(context(host)))
        expected = [
            ("./svc.sh", "stop"),
            ("tar", "-xzf", image),
            ("cp", "./bin/runsvc.sh", "./runsvc.sh"),
            ("chown", "-R", "gh-runner:gh-runner", "/opt/gh-runner"),
            ("systemctl", "enable", RUNNER_UNIT),
            ("./svc.sh", "start"),
        ]
        actual = [
            command for command, _, _ in host.runs if command in set(expected)
        ]
        self.assertEqual(actual, expected)
        self.assertNotIn(old_archive, host.files)
        self.assertIn(image, host.files)
        self.assertEqual(host.files.get("/opt/gh-runner/.runner"), before)
        self.assertFalse(any("config.sh" in command for command, _, _ in host.runs))

    def test_release_moves_and_service_restarts_without_a_token(self) -> None:
        # A new release on a runner still registered with updates on, no token on
        # disk: the release is installed and the service comes back; the
        # re-registration waits for the token (the re-check reports pending-input).
        host = self._runner_host(registered=True, installed_version="0.0.0", service=True)
        image = f"/opt/gh-runner/actions-runner-linux-x64-{HOST_LOCK.gh_runner.version}.tar.gz"
        self.assertIsNone(GhRunnerStep().apply(context(host)))
        commands = [command for command, _, _ in host.runs]
        self.assertEqual(
            [c for c in commands if c in {("./svc.sh", "stop"), ("tar", "-xzf", image), ("./svc.sh", "start")}],
            [("./svc.sh", "stop"), ("tar", "-xzf", image), ("./svc.sh", "start")],
        )
        self.assertNotIn(("./svc.sh", "uninstall"), commands)
        self.assertFalse(any("config.sh" in command for command in commands))
        # The extraction (a no-op in the fake) would have moved the manifest.
        host.files["/opt/gh-runner/bin/Runner.Listener.deps.json"] = json.dumps(
            {"targets": {"runner": {f"Runner.Listener/{HOST_LOCK.gh_runner.version}": {}}}}
        )
        self.assertEqual(
            GhRunnerStep().check(context(host)).disposition, Disposition.PENDING_INPUT
        )

    def test_busy_runner_at_extract_refuses_before_stop(self) -> None:
        host = self._runner_host(installed_version="0.0.0", service=True, busy=True)
        with self.assertRaises(StepFailure) as raised:
            GhRunnerStep().apply(context(host))
        self.assertEqual(raised.exception.fix, _RUNNER_WAIT_FIX)
        self.assertNotIn(("./svc.sh", "stop"), [run[0] for run in host.runs])

    def test_inactive_or_disabled_service_is_enabled_then_started(self) -> None:
        for enabled, active in ((False, True), (True, False)):
            with self.subTest(enabled=enabled, active=active):
                host = self._runner_host(
                    registered="off", service=True,
                    service_enabled=enabled, service_active=active,
                )
                self.assertEqual(GhRunnerStep().check(context(host)).disposition, Disposition.DRIFT)
                self.assertIsNone(GhRunnerStep().apply(context(host)))
                commands = [command for command, _, _ in host.runs]
                self.assertLess(
                    commands.index(("systemctl", "enable", RUNNER_UNIT)),
                    commands.index(("./svc.sh", "start")),
                )
                self.assertNotIn(("./svc.sh", "install", "gh-runner"), commands)
                self.assertFalse(any("config.sh" in command for command in commands))

    def test_unregistered_runner_with_token_is_drift(self) -> None:
        result = GhRunnerStep().check(context(self._runner_host(token=True)))
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertIn("registration token is available", result.detail)

    def test_registered_runner_without_flag_needs_a_token(self) -> None:
        result = GhRunnerStep().check(context(self._runner_host(registered=True)))
        self.assertEqual(result.disposition, Disposition.PENDING_INPUT)
        self.assertIn("registered with automatic updates on", result.detail)
        self.assertIn("gh_runner_token", result.fix)

    def test_registered_runner_without_flag_waits_for_a_job(self) -> None:
        result = GhRunnerStep().check(
            context(self._runner_host(registered=True, token=True, busy=True))
        )
        self.assertEqual(result.disposition, Disposition.PENDING_INPUT)
        self.assertIn("a job is running", result.detail)
        self.assertIn("running job", result.fix)

    def test_registered_runner_without_flag_is_drift_when_idle(self) -> None:
        result = GhRunnerStep().check(
            context(self._runner_host(registered=True, token=True))
        )
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertIn("registered with automatic updates on", result.detail)

    def test_registered_runner_with_flag_and_active_service_converges(self) -> None:
        host = self._runner_host(
            registered="off", service=True, service_enabled=True, service_active=True
        )
        result = GhRunnerStep().check(context(host))
        self.assertEqual(
            result,
            CheckResult(
                Disposition.CONVERGED,
                "GitHub runner is registered with updates disabled and active",
                "",
            ),
        )
        self.assertIn(
            (("systemctl", "is-enabled", RUNNER_UNIT), None, None),
            host.runs,
        )

    def test_runner_without_python_venv_drifts_with_install_fix(self) -> None:
        for registered in (False, "off"):
            with self.subTest(registered=registered):
                host = self._runner_host(registered=registered, service=bool(registered))
                host.commands.pop(RUNNER_VENV_QUERY)

                result = GhRunnerStep().check(context(host))

                self.assertEqual(result.disposition, Disposition.DRIFT)
                self.assertIn("python3-venv", result.detail)
                self.assertIn("apt-get install -y python3-venv", result.fix)
                self.assertIn("re-run provision", result.fix)

    def test_runner_installs_python_venv_before_sudoers(self) -> None:
        host = self._runner_host(registered="off", service=True)
        host.commands.pop(RUNNER_VENV_QUERY)
        host.files[RUNNER_SUDOERS] = "old rules\n"

        self.assertIsNone(GhRunnerStep().apply(context(host)))

        commands = [command for command, _, _ in host.runs]
        self.assertLess(commands.index(("apt-get", "update")), commands.index(RUNNER_VENV_INSTALL))
        self.assertLess(
            commands.index(RUNNER_VENV_INSTALL),
            commands.index(("visudo", "-c", "-f", RUNNER_SUDOERS_CANDIDATE)),
        )
        self.assertEqual(host.files[RUNNER_SUDOERS], RUNNER_SUDOERS_TEXT)
        self.assertEqual(GhRunnerStep().check(context(host)).disposition, Disposition.CONVERGED)

    def test_runner_sudoers_truth_table_reports_each_kind_of_drift(self) -> None:
        cases = ("absent", "content", "mode", "owner")
        for case in cases:
            with self.subTest(case=case):
                host = self._runner_host(
                    registered="off", service=True, service_enabled=True, service_active=True
                )
                if case == "absent":
                    del host.files[RUNNER_SUDOERS]
                    del host.stats[RUNNER_SUDOERS]
                elif case == "content":
                    host.files[RUNNER_SUDOERS] = "wrong rule\n"
                elif case == "mode":
                    host.stats[RUNNER_SUDOERS] = file_stat(0o644, 0, 0)
                else:
                    host.stats[RUNNER_SUDOERS] = file_stat(0o440, 1000, 0)

                result = GhRunnerStep().check(context(host))

                self.assertEqual(result.disposition, Disposition.DRIFT)
                self.assertIn(RUNNER_SUDOERS, result.detail)

    def test_runner_sudoers_names_the_acceptance_smoke_and_mask_commands(self) -> None:
        rules = RUNNER_SUDOERS_TEXT.splitlines()
        self.assertEqual(len(rules), 3)
        self.assertEqual(
            rules[1],
            "gh-runner ALL=(root) NOPASSWD: /usr/bin/python3 -B -m tools.cistack smoke",
        )
        self.assertEqual(
            rules[2],
            "gh-runner ALL=(root) NOPASSWD: /usr/bin/unshare --mount --net -- sh -c *",
        )

    def test_runner_sudoers_detects_drift_on_each_command_line(self) -> None:
        expected = RUNNER_SUDOERS_TEXT.splitlines()
        step = GhRunnerStep()
        for index in range(len(expected)):
            with self.subTest(line=index):
                host = self._runner_host(
                    registered="off", service=True, service_enabled=True, service_active=True
                )
                changed = expected.copy()
                changed[index] += " altered"
                host.files[RUNNER_SUDOERS] = "\n".join(changed) + "\n"

                result = step.check(context(host))

                self.assertEqual(result.disposition, Disposition.DRIFT)
                self.assertIn("differs from the runner's rules", result.detail)

    def test_workflow_python_commands_are_named_by_runner_sudoers(self) -> None:
        rules = [line.split("NOPASSWD: ", 1)[1] for line in RUNNER_SUDOERS_TEXT.splitlines()]
        workflow_paths = [".github/workflows/ci.yml"]
        acceptance = ".github/workflows/acceptance.yml"
        if not absent_from_export(acceptance, ROOT):
            workflow_paths.append(acceptance)

        def allowed(command: str, rule: str) -> bool:
            # sudo matches a rule without a wildcard exactly, arguments included.
            if rule.endswith(" *"):
                return command.startswith(rule.removesuffix("*"))
            return command == rule

        seen = 0
        for relative in workflow_paths:
            for line in (ROOT / relative).read_text(encoding="utf-8").splitlines():
                if "sudo python3 " not in line or line.lstrip().startswith("#"):
                    continue
                command = re.split(r" (?:[12]?>|\|)", line.split("sudo ", 1)[1], maxsplit=1)[0]
                command = command.strip().replace("python3", "/usr/bin/python3", 1)
                seen += 1
                self.assertTrue(
                    any(allowed(command, rule) for rule in rules),
                    f"{relative} command has no sudoers rule: {command}",
                )
        self.assertGreaterEqual(seen, 1)

    def test_mask_root_stage_is_admitted_by_runner_sudoers(self) -> None:
        facts = Facts(
            effective_uid=lambda: 997,
            effective_gid=lambda: 997,
            supplementary_groups=lambda: (997, 999),
            # Injected: uid 997 has no account on a hosted runner.
            password_entry=lambda uid: pwd.struct_passwd(
                ("gh-runner", "x", uid, 997, "", "/home/gh-runner", "/bin/bash")
            ),
            environment=lambda: {"HOME": "/home/gh-runner", "PATH": "/usr/bin"},
        )
        argv = wrap(
            (".venv/bin/pytest", "tests/test_host_import_boundary.py"),
            paths=("/etc/gideon",),
            facts=facts,
        )
        self.assertEqual(argv[:3], ("sudo", "-n", "unshare"))
        # sudo resolves the command to its absolute path and matches the rest
        # word by word; the box's sudo-rs takes a wildcard only as a final "*".
        command = ("/usr/bin/unshare", *argv[3:])

        def admitted(rule: str) -> bool:
            words = tuple(rule.split(" "))
            if "*" not in rule:
                return command == words
            if words[-1] != "*" or rule.count("*") != 1:
                return False
            return command[: len(words) - 1] == words[:-1]

        rules = [line.split("NOPASSWD: ", 1)[1] for line in RUNNER_SUDOERS_TEXT.splitlines()]
        self.assertTrue(any(admitted(rule) for rule in rules))

    def test_runner_sudoers_apply_validates_and_promotes_last(self) -> None:
        host = self._runner_host(
            registered="off", service=True, service_enabled=True, service_active=True
        )
        del host.files[RUNNER_SUDOERS]
        del host.stats[RUNNER_SUDOERS]
        step = GhRunnerStep()

        self.assertIsNone(step.apply(context(host)))

        operations = [
            call
            for call in host.calls
            if call[0] in {"write_text", "run", "chmod", "chown"}
            and (
                call[1] == (RUNNER_SUDOERS_CANDIDATE, RUNNER_SUDOERS_TEXT)
                or call[1] == (("visudo", "-c", "-f", RUNNER_SUDOERS_CANDIDATE), False)
                or call[1] == (("mv", "-f", RUNNER_SUDOERS_CANDIDATE, RUNNER_SUDOERS), True)
                or call[1] == (RUNNER_SUDOERS, 0o440)
                or call[1] == (RUNNER_SUDOERS, 0, 0)
            )
        ]
        self.assertEqual(
            [call[0] for call in operations],
            ["write_text", "run", "run", "chmod", "chown"],
        )
        self.assertEqual(host.files[RUNNER_SUDOERS], RUNNER_SUDOERS_TEXT)

    def test_runner_sudoers_visudo_failure_removes_candidate(self) -> None:
        host = self._runner_host(
            registered="off", service=True, service_enabled=True, service_active=True
        )
        del host.files[RUNNER_SUDOERS]
        del host.stats[RUNNER_SUDOERS]
        visudo = ("visudo", "-c", "-f", RUNNER_SUDOERS_CANDIDATE)
        host.commands[visudo] = subprocess.CompletedProcess(
            list(visudo), 1, "", "syntax error\n"
        )

        with self.assertRaises(StepFailure) as raised:
            GhRunnerStep().apply(context(host))

        self.assertIn(RUNNER_SUDOERS_CANDIDATE, raised.exception.detail)
        self.assertIn("syntax error", raised.exception.detail)
        self.assertNotIn(RUNNER_SUDOERS_CANDIDATE, host.files)
        self.assertNotIn(
            ("mv", "-f", RUNNER_SUDOERS_CANDIDATE, RUNNER_SUDOERS),
            [command for command, _cwd, _env in host.runs],
        )

    def test_registered_runner_with_flag_but_inactive_service_drifts(self) -> None:
        result = GhRunnerStep().check(
            context(self._runner_host(registered="off", service=True, service_active=False))
        )
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertIn("not enabled and active", result.detail)

    def test_registered_runner_without_a_service_drifts(self) -> None:
        result = GhRunnerStep().check(context(self._runner_host(registered="off")))
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertEqual(result.detail, "the GitHub runner service is not installed")

    def test_registered_runner_with_active_but_disabled_service_drifts(self) -> None:
        result = GhRunnerStep().check(
            context(self._runner_host(registered="off", service=True, service_enabled=False))
        )
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertIn("not enabled and active", result.detail)

    def test_migrated_settings_without_flag_trigger_reregistration_branch(self) -> None:
        host = self._runner_host(registered="off", runner_migrated="on", token=True)
        result = GhRunnerStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertIn("registered with automatic updates on", result.detail)

    def test_runner_settings_byte_order_mark_is_stripped(self) -> None:
        host = self._runner_host(registered="off", runner_migrated=True, service=True)
        for path in ("/opt/gh-runner/.runner", "/opt/gh-runner/.runner_migrated"):
            self.assertTrue(host.files[path].startswith("\ufeff"), path)
        self.assertEqual(GhRunnerStep().check(context(host)).disposition, Disposition.CONVERGED)

    def test_runner_manifest_is_required(self) -> None:
        result = GhRunnerStep().check(
            context(self._runner_host(manifest=False))
        )
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertIn("manifest is missing", result.detail)
        self.assertIn("Extract", result.fix)

    def test_runner_manifest_without_listener_entry_is_unfixable(self) -> None:
        host = self._runner_host()
        host.files["/opt/gh-runner/bin/Runner.Listener.deps.json"] = (
            '{"targets": {"runner": {"Other.Library/1.0": {}}}}'
        )
        result = GhRunnerStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.UNFIXABLE)
        self.assertIn("Runner.Listener.deps.json", result.detail)
        self.assertIn("Runner.Listener --version", result.fix)

    def test_runner_version_behind_lock_drifts_naming_both_versions(self) -> None:
        result = GhRunnerStep().check(
            context(self._runner_host(installed_version="0.0.0"))
        )
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertIn("0.0.0", result.detail)
        self.assertIn(HOST_LOCK.gh_runner.version, result.detail)

    def test_unparsable_runner_settings_are_unfixable(self) -> None:
        host = self._runner_host(registered=True)
        host.files["/opt/gh-runner/.runner"] = "not json"
        result = GhRunnerStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.UNFIXABLE)
        self.assertIn("config.sh remove --local", result.fix)

    def test_non_object_runner_settings_are_unfixable(self) -> None:
        host = self._runner_host(registered=True)
        host.files["/opt/gh-runner/.runner"] = "[]"
        result = GhRunnerStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.UNFIXABLE)
        self.assertIn("config.sh remove --local", result.fix)

    def test_busy_probe_failure_is_unfixable(self) -> None:
        host = self._runner_host(registered=True, token=True)
        pgrep = ("pgrep", "-u", "gh-runner", "-x", "Runner.Worker")
        host.commands[pgrep] = subprocess.CompletedProcess(list(pgrep), 2, "", "error")
        result = GhRunnerStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.UNFIXABLE)
        self.assertIn("cannot tell whether a job is running", result.detail)
        self.assertIn("procps", result.fix)


class BaselineCheckPass(unittest.TestCase):
    def test_recorded_baseline_has_no_docker_source_and_engine_is_absent(self) -> None:
        host = baseline_host()
        self.assertFalse(
            any(path.startswith("/etc/apt/sources.list") for path in host.files)
        )
        result = DockerEngineStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertEqual(result.detail, "Docker is not installed")

    def test_recorded_baseline_has_expected_non_site_dispositions(self) -> None:
        # The recorded post-reinstall box still has no observability homes;
        # disk-layout's first pass therefore remains drift.
        expected = {
            "platform": Disposition.CONVERGED,
            "wait-online": Disposition.DRIFT,
            "host-tools": Disposition.DRIFT,
            "gideon-command": Disposition.DRIFT,
            "service-user": Disposition.DRIFT,
            "csa-accounts": Disposition.UNFIXABLE,
            "disk-layout": Disposition.DRIFT,
            "nvidia-driver": Disposition.DRIFT,
            "nvidia-toolkit": Disposition.PENDING_INPUT,
            "docker-engine": Disposition.DRIFT,
            # The installer's periodic triggers already meet the need.
            "unattended-upgrades": Disposition.CONVERGED,
            "secrets-dirs": Disposition.DRIFT,
            "backup-keypair": Disposition.DRIFT,
            "age-recipient": Disposition.DRIFT,
            "age-identity": Disposition.DRIFT,
            "kvm": Disposition.DRIFT,
            "registry": Disposition.DRIFT,
            "gh-runner": Disposition.DRIFT,
        }
        host = baseline_host()
        for step in STEPS:
            if step.needs_site:
                continue
            result = step.check(context(host))
            self.assertEqual(result.disposition, expected[step.name], step.name)

    def test_recorded_baseline_disk_replay_sees_the_new_home_after_user_convergence(self) -> None:
        host = baseline_host()
        host.commands[("getent", "passwd", "gideon")] = completed(
            ("getent", "passwd", "gideon"),
            "gideon:x:998:998::/nonexistent:/usr/sbin/nologin\n",
        )
        host.stats.update(
            {
                f"/data/{name}": directory_stat(0o755, 998, 998)
                for name in (
                    "fast",
                    "bulk",
                    "work",
                    "models",
                    "registry",
                    "drill",
                    "ci",
                    "backup-staging",
                    "acceptance",
                )
            }
        )
        host.stats["/data/bulk/cas"] = directory_stat(0o2770, 998, 998)
        host.stats[str(worker.SNAPSHOTS_ROOT)] = directory_stat(worker.DIR_MODE, 998, 998)
        host.stats[QDRANT_DATA_ROOT] = directory_stat(0o750, 998, 998)
        host.stats[OPENSEARCH_DATA_ROOT] = directory_stat(
            0o700, OPENSEARCH_UID, OPENSEARCH_GID
        )
        result = DiskLayoutStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertIn("/data/observability", result.detail)

    def test_site_steps_pass_through_runner_when_site_is_missing(self) -> None:
        host = baseline_host()
        args = type("Arguments", (), {"only": None, "dry_run": True, "list": False})()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            run_provision(
                args,
                host=host,
                lock_path=LOCK,
                site_path="/etc/gideon/site.yaml",
            )
        report = stdout.getvalue()
        for name in ("egress-proxy", "firewall", "time-sync", "timezone"):
            self.assertIn(f"{name}: blocked", report)

    def test_site_steps_have_recorded_dispositions_with_example_site(self) -> None:
        host = baseline_host()
        expected = {
            "egress-proxy": Disposition.CONVERGED,
            "firewall": Disposition.DRIFT,
            "time-sync": Disposition.DRIFT,
            "timezone": Disposition.DRIFT,
        }
        for step in STEPS:
            if step.name in expected:
                result = step.check(context(host))
                self.assertEqual(result.disposition, expected[step.name], step.name)
