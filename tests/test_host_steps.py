"""Host-step truth tables, including the post-reinstall baseline."""

import contextlib
import io
import json
import os
import re
import stat
import subprocess
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from gideon.host.lock import load_host_lock
from gideon.host.provision import run_provision
from gideon.host.site import SiteConfig, load_site
from gideon.host.steps import (
    SITE_MISSING_FIX,
    STEPS,
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    StepFailure,
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
    _CONTAINERD_CONFIG,
    _CONTAINERD_DEFAULT_ROOT,
    _CONTAINERD_ROOT,
    _CONTAINERD_STORE_PATHS,
    _CONTAINERD_TEXT,
    DockerEngineStep,
)
from gideon.host.steps.maintenance import UnattendedUpgradesStep
from gideon.host.steps.network import (
    _WAIT_ONLINE_DROPIN,
    _WAIT_ONLINE_FIX,
    _WAIT_ONLINE_TEXT,
    FirewallStep,
    TimeSyncStep,
    WaitOnlineStep,
)
from gideon.host.steps.nvidia import NvidiaDriverStep, NvidiaToolkitStep
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

    def test_two_csa_keys_allow_policy_to_converge(self) -> None:
        commands: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {
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
        }
        files = {
            "/home/alice/.ssh/authorized_keys": "ssh-ed25519 AAAA\n",
            "/home/bob/.ssh/authorized_keys": "ssh-ed25519 BBBB\n",
            "/etc/ssh/sshd_config.d/99-gideon-key-only.conf": "",
        }
        reload_command = ("systemctl", "reload", "ssh")
        commands[reload_command] = completed(reload_command)
        host = FakeHost(commands=commands, files=files)
        step = CsaAccountsStep()
        self.assertEqual(step.check(context(host)).disposition, Disposition.DRIFT)
        step.apply(context(host))
        self.assertEqual(
            host.files["/etc/ssh/sshd_config.d/99-gideon-key-only.conf"],
            "PasswordAuthentication no\nKbdInteractiveAuthentication no\n",
        )
        self.assertIn(
            ("run", (("systemctl", "reload", "ssh"), True)),
            host.calls,
        )
        recheck = step.check(context(host))
        self.assertEqual(recheck.disposition, Disposition.CONVERGED, recheck)


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


class DiskLayoutStepTests(unittest.TestCase):
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

    def test_firewall_default_allow_policy_is_drift(self) -> None:
        current_site = context(disk_host(disk_devices())).site
        assert current_site is not None
        rules = "".join(
            f"[ {index}] {port}/tcp                   ALLOW IN    {cidr}                  # gideon-provision\n"
            for index, (cidr, port) in enumerate(
                sorted((cidr, port) for cidr in current_site.lan_cidrs for port in (22, 443)),
                start=1,
            )
        )
        host = FakeHost(
            commands={
                ("ufw", "status", "numbered"): completed(
                    ("ufw", "status", "numbered"), "Status: active\n" + rules
                ),
                ("ufw", "status", "verbose"): completed(
                    ("ufw", "status", "verbose"),
                    "Status: active\nDefault: allow (incoming), allow (outgoing)\n",
                ),
            }
        )
        from gideon.host.steps.network import FirewallStep

        result = FirewallStep().check(context(host, site=current_site))
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertIn("default incoming policy", result.detail)

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


class DockerKeyringOrderTests(unittest.TestCase):
    def test_apply_fetches_keyring_before_writing_the_source_entry(self) -> None:
        gpg = "/etc/apt/keyrings/gideon-docker.asc"
        wget = ("wget", "-qO", f"{gpg}.partial", "https://download.docker.com/linux/ubuntu/gpg")
        move = ("mv", "-f", f"{gpg}.partial", gpg)
        commands: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {
            wget: completed(wget),
            move: completed(move),
        }
        for argv in (
            ("apt-get", "update"),
            ("apt-get", "install", "-y", "docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"),
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
            and call[1][0] == "/etc/apt/sources.list.d/gideon-docker.list"
        )
        self.assertLess(fetch_index, write_index)


class NvidiaStepTests(unittest.TestCase):
    def test_driver_disposition_ladder(self) -> None:
        step = NvidiaDriverStep()
        missing = FakeHost()
        branch = HOST_LOCK.driver.branch
        self.assertEqual(step.check(context(missing)).disposition, Disposition.DRIFT)

        legacy = FakeHost(files={"/etc/apt/sources.list.d/gideon-nvidia.list": "dead\n"})
        legacy_result = step.check(context(legacy))
        self.assertEqual(legacy_result.disposition, Disposition.DRIFT)
        self.assertIn("legacy gideon-nvidia", legacy_result.detail)

        files = {"/usr/share/keyrings/cuda-archive-keyring.gpg": "keyring"}
        keyring, keyring_output = package_result("cuda-keyring", "1.1-1")
        package, package_output = package_result(
            "nvidia-open", f"{branch}.71.05-1ubuntu1", status="hold ok"
        )
        pinning, pinning_output = package_result(
            f"nvidia-driver-pinning-{branch}", f"{branch}-1ubuntu1"
        )
        hold = ("apt-mark", "showhold")
        lsmod = ("lsmod",)
        commands = {
            keyring: keyring_output,
            package: package_output,
            pinning: pinning_output,
            hold: completed(hold, "nvidia-open\n"),
            lsmod: completed(lsmod, "nouveau 12345 0\n"),
        }
        reboot = FakeHost(files=files, commands=commands)
        self.assertEqual(step.check(context(reboot)).disposition, Disposition.REBOOT_REQUIRED)

        commands[lsmod] = completed(lsmod, "nvidia 12345 0\n")
        live = FakeHost(files={**files, "/proc/driver/nvidia": ""}, commands=commands)
        self.assertEqual(step.check(context(live)).disposition, Disposition.CONVERGED)

    def test_driver_apply_installs_keyring_deb_and_heals_legacy_source(self) -> None:
        host = FakeHost(
            files={"/etc/apt/sources.list.d/gideon-nvidia.list": "dead\n"},
        )
        ctx = context(host)
        deb = "/var/tmp/gideon-cuda-keyring.deb"
        url = (
            "https://developer.download.nvidia.com/compute/cuda/repos/"
            f"{ctx.lock.driver.repo}/{ctx.lock.driver.keyring_deb}"
        )
        wget = ("wget", "-qO", f"{deb}.partial", url)
        move = ("mv", "-f", f"{deb}.partial", deb)
        sha = ("sha256sum", deb)
        dpkg = ("dpkg", "-i", deb)
        update = ("apt-get", "update")
        pinning = ("apt-get", "install", "-y", f"nvidia-driver-pinning-{ctx.lock.driver.branch}")
        install = ("apt-get", "install", "-y", "nvidia-open")
        hold = ("apt-mark", "hold", "nvidia-open")
        host.commands.update(
            {
                wget: completed(wget),
                move: completed(move),
                sha: completed(sha, f"{ctx.lock.driver.keyring_sha256}  {deb}\n"),
                dpkg: completed(dpkg),
                update: completed(update),
                pinning: completed(pinning),
                install: completed(install),
                hold: completed(hold),
            }
        )
        NvidiaDriverStep().apply(ctx)
        self.assertIn(("unlink", "/etc/apt/sources.list.d/gideon-nvidia.list"), host.calls)
        self.assertIn(("run", (dpkg, True)), host.calls)
        self.assertIn(("run", (install, True)), host.calls)
        writes = [call for call in host.calls if call[0] == "write_text"]
        self.assertEqual(writes, [])

    def test_toolkit_is_blocked_until_driver_is_loaded(self) -> None:
        result = NvidiaToolkitStep().check(context(FakeHost()))
        self.assertEqual(result.disposition, Disposition.PENDING_INPUT)
        self.assertIn("docs/runbooks/install-upgrade.md §1", result.fix)


def docker_commands(
    *,
    docker_version: str = "29.0.0",
    compose_version: str = "5.0.0",
    containerd_enabled: bool = True,
    containerd_active: bool = True,
) -> dict[tuple[str, ...], subprocess.CompletedProcess[str]]:
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
    }
    return commands


def docker_files(daemon: Mapping[str, object], *, journald: str = "[Journal]\nStorage=persistent\nSystemMaxUse=50G\nMaxRetentionSec=90day\n") -> dict[str, str]:
    return {
        "/etc/apt/keyrings/gideon-docker.asc": "key",
        "/etc/apt/sources.list.d/gideon-docker.list": "deb [arch=amd64 signed-by=/etc/apt/keyrings/gideon-docker.asc] https://download.docker.com/linux/ubuntu resolute stable\n",
        "/etc/docker/daemon.json": json.dumps(daemon, indent=2),
        os.fspath(_CONTAINERD_CONFIG): _CONTAINERD_TEXT,
        "/etc/systemd/journald.conf.d/gideon.conf": journald,
    }


class DockerStepTests(unittest.TestCase):
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
                self.assertEqual(
                    DockerEngineStep().check(context(host, site=site)).disposition,
                    Disposition.CONVERGED,
                )
                if desired is not base_daemon:
                    without = FakeHost(files=docker_files(base_daemon), commands=docker_commands())
                    result = DockerEngineStep().check(context(without, site=site))
                    self.assertEqual(result.disposition, Disposition.DRIFT)
                    self.assertIn("daemon.json differs", result.detail)

    def test_docker_version_minimum_drift(self) -> None:
        host = FakeHost(
            files=docker_files({"data-root": "/var/lib/docker"}),
            commands=docker_commands(docker_version="28.0.0"),
        )
        result = DockerEngineStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertIn("minimum", result.detail)

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
            context(FakeHost(files=missing_files, commands=docker_commands()))
        )
        self.assertEqual(missing.disposition, Disposition.DRIFT)
        self.assertIn("config.toml is missing", missing.detail)
        self.assertIn("Write the provision-owned", missing.fix)

        different_files = docker_files(daemon)
        different_files[os.fspath(_CONTAINERD_CONFIG)] = "wrong\n"
        different = DockerEngineStep().check(
            context(FakeHost(files=different_files, commands=docker_commands()))
        )
        self.assertEqual(different.disposition, Disposition.DRIFT)
        self.assertIn("config.toml differs", different.detail)
        self.assertIn("Rewrite the provision-owned", different.fix)

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

    def test_empty_store_is_converged_on_a_fresh_host(self) -> None:
        daemon = {
            "data-root": "/var/lib/docker",
            "features": {"cdi": True},
            "log-driver": "journald",
        }
        host = FakeHost(files=docker_files(daemon), commands=docker_commands())
        result = DockerEngineStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.CONVERGED)

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
        update = ("apt-get", "update")
        install = (
            "apt-get", "install", "-y", "docker-ce", "docker-ce-cli", "containerd.io",
            "docker-buildx-plugin", "docker-compose-plugin",
        )
        commands = docker_commands()
        commands.update({update: completed(update), install: completed(install)})
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
        update = ("apt-get", "update")
        install = (
            "apt-get", "install", "-y", "docker-ce", "docker-ce-cli", "containerd.io",
            "docker-buildx-plugin", "docker-compose-plugin",
        )
        host.commands.update({update: completed(update), install: completed(install)})
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
        self.assertEqual(DockerEngineStep().check(context(host)).disposition, Disposition.CONVERGED)

        site = context(host).site
        assert site is not None
        proxied = replace(site, egress_proxy="http://proxy.example:3128")
        result = DockerEngineStep().check(context(host, site=proxied))
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertIn("daemon.json", result.detail)

    def test_journald_change_restarts_journald(self) -> None:
        daemon = {
            "data-root": "/var/lib/docker",
            "features": {"cdi": True},
            "log-driver": "journald",
        }
        restart = ("systemctl", "restart", "systemd-journald")
        update = ("apt-get", "update")
        install = (
            "apt-get", "install", "-y", "docker-ce", "docker-ce-cli", "containerd.io",
            "docker-buildx-plugin", "docker-compose-plugin",
        )
        enable = ("systemctl", "enable", "--now", "docker")
        commands = docker_commands()
        commands.update({
            update: completed(update),
            install: completed(install),
            restart: completed(restart),
            enable: completed(enable),
        })
        host = FakeHost(
            files=docker_files(daemon, journald="[Journal]\nStorage=volatile\n"),
            commands=commands,
        )
        DockerEngineStep().apply(context(host))
        self.assertIn(("run", (restart, True)), host.calls)


def directory_stat(mode: int, uid: int = 0, gid: int = 0) -> os.stat_result:
    return os.stat_result((stat.S_IFDIR | mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))


def file_stat(mode: int, uid: int = 998, gid: int = 998) -> os.stat_result:
    return os.stat_result((stat.S_IFREG | mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))


UFW_AFTER_RULES = (
    "# rules.input-after\n*filter\n:ufw-after-input - [0:0]\n"
    "-A ufw-after-input -p udp --dport 137 -j ufw-skip-to-policy-input\nCOMMIT\n"
)


class NetworkStepTests(unittest.TestCase):
    def test_firewall_removes_only_stale_tagged_rules_and_preserves_order(self) -> None:
        status = (
            "Status: active\n"
            "     To                         Action      From\n"
            "     --                         ------      ----\n"
            "[ 3] 22/tcp                    ALLOW IN    192.0.2.0/24 # gideon-provision\n"
            "[ 8] 443/tcp                   ALLOW IN    198.51.100.0/24 # gideon-provision\n"
        )
        commands = {
            ("ufw", "status", "numbered"): completed(("ufw", "status", "numbered"), status),
            ("ufw", "--force", "delete", "8"): completed(("ufw", "--force", "delete", "8")),
            ("ufw", "allow", "proto", "tcp", "from", "192.0.2.0/24", "to", "any", "port", "443", "comment", "gideon-provision"): completed(("ufw", "allow")),
            ("ufw", "default", "deny", "incoming"): completed(("ufw", "default", "deny", "incoming")),
            ("ufw", "--force", "enable"): completed(("ufw", "--force", "enable")),
            ("ufw", "reload"): completed(("ufw", "reload")),
        }
        host = FakeHost(commands=commands, files={"/etc/ufw/after.rules": UFW_AFTER_RULES})
        step = FirewallStep()
        self.assertEqual(step.check(context(host)).disposition, Disposition.DRIFT)
        step.apply(context(host))
        self.assertTrue(host.files["/etc/ufw/after.rules"].startswith(UFW_AFTER_RULES.rstrip("\n")))
        self.assertIn("# BEGIN gideon-provision docker-user\n*filter\n:DOCKER-USER - [0:0]\n", host.files["/etc/ufw/after.rules"])
        self.assertIn("-s 192.0.2.0/24 -p tcp -m conntrack --ctorigdstport 443 --ctdir ORIGINAL", host.files["/etc/ufw/after.rules"])
        self.assertIn(("run", (("ufw", "reload"), True)), host.calls)
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

    def firewall_converged_host(self) -> FakeHost:
        from gideon.host.steps.network import _docker_user_block, _docker_user_rules

        status = (
            "Status: active\n"
            "[ 1] 22/tcp                    ALLOW IN    192.0.2.0/24 # gideon-provision\n"
            "[ 2] 443/tcp                   ALLOW IN    192.0.2.0/24 # gideon-provision\n"
        )
        chain = "-N DOCKER-USER\n" + "\n".join(_docker_user_rules(["192.0.2.0/24"])) + "\n"
        return FakeHost(
            commands={
                ("ufw", "status", "numbered"): completed(("ufw", "status", "numbered"), status),
                ("ufw", "status", "verbose"): completed(("ufw", "status", "verbose"), "Status: active\nDefault: deny (incoming), allow (outgoing)\n"),
                ("iptables", "-S", "DOCKER-USER"): completed(("iptables", "-S", "DOCKER-USER"), chain),
            },
            files={"/etc/ufw/after.rules": UFW_AFTER_RULES + "\n" + _docker_user_block(["192.0.2.0/24"])},
        )

    def test_firewall_converges_with_the_docker_user_block_loaded(self) -> None:
        result = FirewallStep().check(context(self.firewall_converged_host()))
        self.assertEqual(result.disposition, Disposition.CONVERGED, result)

    def test_firewall_block_missing_or_stale_is_drift(self) -> None:
        host = self.firewall_converged_host()
        host.files["/etc/ufw/after.rules"] = UFW_AFTER_RULES
        result = FirewallStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertIn("after.rules", result.detail)
        host = self.firewall_converged_host()
        host.files["/etc/ufw/after.rules"] = host.files["/etc/ufw/after.rules"].replace("192.0.2.0/24 -p tcp", "198.51.100.0/24 -p tcp")
        self.assertEqual(FirewallStep().check(context(host)).disposition, Disposition.DRIFT)

    def test_firewall_block_present_but_chain_unloaded_is_drift(self) -> None:
        host = self.firewall_converged_host()
        host.commands[("iptables", "-S", "DOCKER-USER")] = completed(("iptables", "-S", "DOCKER-USER"), "-N DOCKER-USER\n")
        result = FirewallStep().check(context(host))
        self.assertEqual(result.disposition, Disposition.DRIFT)
        self.assertIn("DOCKER-USER chain", result.detail)

    def test_docker_user_rules_never_touch_container_egress_or_established_flows(self) -> None:
        from gideon.host.steps.network import _docker_user_rules

        rules = _docker_user_rules(["192.0.2.0/24", "198.51.100.0/24"])
        self.assertTrue(rules[0].startswith("-A DOCKER-USER -m conntrack --ctstate RELATED,ESTABLISHED"))
        self.assertEqual(rules[1:3], [
            "-A DOCKER-USER -i docker0 -m comment --comment gideon-provision -j RETURN",
            "-A DOCKER-USER -i br-+ -m comment --comment gideon-provision -j RETURN",
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
            sorted(f"-A DOCKER-USER -o {bridge}" for bridge in ("docker0", "br-+") for _ in (443, 5000)),
        )
        self.assertTrue(all("-o docker0" in rule or "-o br-+" in rule for rule in drops))
        self.assertEqual(sum("-s 198.51.100.0/24" in rule for rule in rules), 1)
        self.assertEqual(sum("192.168.122.0/24" in rule and "5000" in rule for rule in rules), 1)
        self.assertLess(max(index for index, rule in enumerate(rules) if "-j RETURN" in rule and "-s " in rule), rules.index(drops[0]))

    def test_firewall_foreign_rule_ahead_of_the_owned_ones_is_drift(self) -> None:
        from gideon.host.steps.network import _docker_user_rules

        host = self.firewall_converged_host()
        chain = "-N DOCKER-USER\n-A DOCKER-USER -j RETURN\n" + "\n".join(_docker_user_rules(["192.0.2.0/24"])) + "\n"
        host.commands[("iptables", "-S", "DOCKER-USER")] = completed(("iptables", "-S", "DOCKER-USER"), chain)
        self.assertEqual(FirewallStep().check(context(host)).disposition, Disposition.DRIFT)

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
                ("run", (apply_command, True)),
            ],
        )

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


class MaintenanceStepTests(unittest.TestCase):
    def test_unattended_upgrades_installs_and_writes_only_its_dropin(self) -> None:
        package = ("dpkg-query", "-W", "-f=${Status} ${Version}\\n", "unattended-upgrades")
        install = ("apt-get", "install", "-y", "unattended-upgrades")
        host = FakeHost(commands={
                ("apt-get", "update"): completed(("apt-get", "update")),
package: completed(package), install: completed(install)})
        step = UnattendedUpgradesStep()
        self.assertEqual(step.check(context(host)).disposition, Disposition.DRIFT)
        step.apply(context(host))
        self.assertIn(("run", (install, True)), host.calls)
        self.assertIn('APT::Periodic::Unattended-Upgrade "1";', host.files["/etc/apt/apt.conf.d/52gideon-auto-upgrades"])


class HostToolsStepTests(unittest.TestCase):
    LDAP = ("dpkg-query", "-W", "-f=${Status} ${Version}\\n", "ldap-utils")
    SKOPEO = ("dpkg-query", "-W", "-f=${Status} ${Version}\\n", "skopeo")
    AGE = ("dpkg-query", "-W", "-f=${Status} ${Version}\\n", "age")
    RSYNC = ("dpkg-query", "-W", "-f=${Status} ${Version}\\n", "rsync")

    def test_missing_tools_install_together_and_converge_when_present(self) -> None:
        update = ("apt-get", "update")
        install = ("apt-get", "install", "-y", "ldap-utils", "skopeo", "age", "rsync")
        host = FakeHost(commands={update: completed(update), install: completed(install)})
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
        update = ("apt-get", "update")
        install = ("apt-get", "install", "-y", "skopeo")
        host = FakeHost(
            commands={
                self.LDAP: completed(self.LDAP, "install ok installed 2.6.7\n"),
                self.AGE: completed(self.AGE, "install ok installed 1.0.0\n"),
                self.RSYNC: completed(self.RSYNC, "install ok installed 3.2.7\n"),
                update: completed(update),
                install: completed(install),
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
)
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

    def test_runner_sudoers_names_the_acceptance_and_smoke_commands(self) -> None:
        rules = RUNNER_SUDOERS_TEXT.splitlines()
        self.assertEqual(len(rules), 2)
        self.assertEqual(
            rules[1],
            "gh-runner ALL=(root) NOPASSWD: /usr/bin/python3 -B -m tools.cistack smoke",
        )

    def test_runner_sudoers_detects_drift_on_either_command_line(self) -> None:
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
            "unattended-upgrades": Disposition.DRIFT,
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
