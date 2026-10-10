"""Observable removal contracts over a recording bare-host seam."""

import argparse
import contextlib
import fnmatch
import io
import json
import os
import subprocess
import unittest
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from gideon.host import backuplock, cotenants, report, uninstall
from gideon.host.render.compose import PROJECT_NAME
from gideon.host.steps import disk, network
from gideon.host.steps.accounts import SSHD_POLICY
from gideon.host.steps.command import COMMAND_PATH, INSTALL_HOME
from gideon.host.steps.docker import JOURNALD_DROP_IN
from gideon.host.steps.maintenance import AUTO_UPGRADES_DROP_IN
from gideon.host.steps.proxy import PROXY_CONF
from gideon.host.steps.services import REGISTRY_UNIT, RUNNER_SUDOERS
from gideon.host.steps.site_dirs import ETC_GIDEON
from gideon.host.sysio import LockingHost, PathLike

CHECKOUT = Path("/fictitious-checkout")
RENDERED = Path("/fictitious-rendered")
STAGES = (
    "preconditions", "attached", "units", "projects", "firewall", "command",
    "files", "data", "configuration", "networks",
)
LONG_FORM = f"sudo python3 -m gideon uninstall from {CHECKOUT}"
PURGE_FORM = f"sudo python3 -m gideon uninstall --purge from {CHECKOUT}"


def completed(
    argv: Sequence[str], *, stdout: str = "", returncode: int = 0, stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)


class FakeHost:
    """Serve file and command reads from memory while recording mutations."""

    def __init__(self, *, docker: str = "absent") -> None:
        self.docker = docker
        self.euid = 0
        self.files: dict[str, str] = {
            os.fspath(COMMAND_PATH): "wrapper",
            os.fspath(network.DOCKER_USER_UNIT): "unit",
        }
        self.locks: dict[str, str] = {}
        self.lock_events: list[tuple[str, str]] = []
        self.commands: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {}
        self.calls: list[tuple[tuple[str, ...], float | None]] = []
        self.removed: list[str] = []
        self.writes: list[tuple[str, int]] = []
        self.unit_files: list[str] = []
        self.not_found_units: set[str] = set()
        self.projects: list[str] = []
        self.volumes: dict[str, str] = {}
        self.networks: list[str] = []
        self.attached: dict[str, str] = {}
        self.shared_rules: list[str] = []
        self.shared_chain = False
        self.owned_chain = False
        self.ufw_is_active = False
        self.ufw_numbered = "Status: inactive\n"
        self.chrony_is_active = False

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, input, cwd, env, passthrough
        command = tuple(argv)
        self.calls.append((command, timeout))
        if command in self.commands:
            return self.commands[command]
        if command == cotenants.DOCKER_VERSION_ARGV:
            return completed(command, returncode=127 if self.docker == "absent" else 0)
        if command == cotenants.DOCKER_ACTIVE_ARGV:
            active = self.docker not in ("absent", "stopped")
            return completed(command, stdout="active\n" if active else "inactive\n", returncode=0 if active else 3)
        if command == cotenants.DOCKER_PS_ARGV:
            if self.docker == "unreadable":
                return completed(command, returncode=1, stderr="permission denied\nsecond line\n")
            return completed(command)
        if command == ("docker", "compose", "version"):
            return completed(command, stdout="Docker Compose version fictitious\n")
        if command == cotenants.compose_projects_argv():
            return completed(command, stdout=json.dumps([{"Name": name} for name in self.projects]))
        if "down" in command and command[:2] == ("docker", "compose"):
            name = command[command.index("-p") + 1] if "-p" in command else PROJECT_NAME
            self.projects.remove(name)
            if "--volumes" in command:
                self.volumes = {
                    volume: project for volume, project in self.volumes.items() if project != name
                }
            return completed(command)
        if command == cotenants.volume_ls_argv():
            return completed(
                command,
                stdout="".join(f"{volume}\t{project}\n" for volume, project in self.volumes.items()),
            )
        if command[:3] == ("docker", "volume", "rm"):
            del self.volumes[command[3]]
            return completed(command)
        if command == cotenants.network_ls_argv():
            return completed(command, stdout="\n".join(self.networks))
        if command[:3] == ("docker", "ps", "--filter") and command[3].startswith("network="):
            return completed(command, stdout=self.attached.get(command[3].removeprefix("network="), ""))
        if command[:3] == ("docker", "network", "rm"):
            self.networks.remove(command[3])
            return completed(command)
        if command[:2] == ("systemctl", "list-unit-files"):
            # As systemd does: patterns that match no unit file exit 1, silently.
            patterns = [word for word in command[2:] if not word.startswith("-")]
            listed = [
                unit for unit in self.unit_files
                if not patterns or any(fnmatch.fnmatchcase(unit, pattern) for pattern in patterns)
            ]
            if patterns and not listed:
                return completed(command, returncode=1)
            return completed(command, stdout="".join(f"{unit} enabled\n" for unit in listed))
        if command[:3] == ("systemctl", "disable", "--now"):
            unit = command[3]
            if unit in self.unit_files:
                self.unit_files.remove(unit)
            if unit in self.not_found_units or (
                unit == network.DOCKER_USER_UNIT.name
                and os.fspath(network.DOCKER_USER_UNIT) not in self.files
            ):
                return completed(command, returncode=1, stderr="unit not found\n")
            return completed(command)
        if command[:3] == ("systemctl", "show", "--property=LoadState"):
            return completed(command, stdout="not-found\n")
        if command == ("systemctl", "daemon-reload"):
            return completed(command)
        if command == ("systemctl", "is-active", "chrony"):
            return completed(
                command, stdout="active\n" if self.chrony_is_active else "inactive\n",
                returncode=0 if self.chrony_is_active else 3,
            )
        if command == ("chronyc", "reload", "sources"):
            return completed(command)
        if command == ("iptables", "-w", "-S", network.SHARED_CHAIN):
            if not self.shared_chain:
                return completed(command, returncode=1, stderr="no chain\n")
            return completed(command, stdout="\n".join((f"-N {network.SHARED_CHAIN}", *self.shared_rules)))
        if command[:4] == ("iptables", "-w", "-D", network.SHARED_CHAIN):
            del self.shared_rules[int(command[4]) - 1]
            return completed(command)
        if command == ("iptables", "-w", "-S", network.CHAIN):
            return completed(
                command, stdout=f"-N {network.CHAIN}\n" if self.owned_chain else "",
                returncode=0 if self.owned_chain else 1,
            )
        if command == ("iptables", "-w", "-F", network.CHAIN):
            return completed(command)
        if command == ("iptables", "-w", "-X", network.CHAIN):
            self.owned_chain = False
            return completed(command)
        if command == ("ufw", "status"):
            return completed(command, stdout="Status: active\n" if self.ufw_is_active else "Status: inactive\n")
        if command == ("ufw", "status", "numbered"):
            return completed(command, stdout=self.ufw_numbered)
        if command == ("ufw", "reload"):
            return completed(command)
        if command[:2] == ("rm", "-rf"):
            path = command[-1]
            for key in tuple(self.files):
                if key == path or key.startswith(path + "/"):
                    del self.files[key]
            self.removed.append(path)
            return completed(command)
        raise AssertionError(f"unexpected command: {command}")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(
        self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644,
    ) -> None:
        del encoding
        key = os.fspath(path)
        self.files[key] = text
        self.writes.append((key, mode))

    def exists(self, path: PathLike) -> bool:
        key = os.fspath(path)
        return key in self.files or any(name.startswith(key + "/") for name in self.files)

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        key = os.fspath(path)
        if key not in self.files and not missing_ok:
            raise FileNotFoundError(key)
        if key in self.files:
            self.files.pop(key)
            self.removed.append(key)

    def mkdir(
        self, path: PathLike, *, mode: int = 0o755, parents: bool = False, exist_ok: bool = False,
    ) -> None:
        del path, mode, parents, exist_ok

    def take_lock(self, path: PathLike, record: str) -> str | None:
        key = os.fspath(path)
        self.lock_events.append(("take", key))
        if key in self.locks:
            return self.locks[key]
        self.locks[key] = record
        return None

    def release_lock(self, path: PathLike) -> None:
        key = os.fspath(path)
        self.lock_events.append(("release", key))
        self.locks.pop(key)

    def geteuid(self) -> int:
        return self.euid


def execute(host: FakeHost, *, purge: bool = False) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = uninstall.run_uninstall(
            argparse.Namespace(purge=purge), host=cast(LockingHost, host),
            rendered_dir=RENDERED, checkout=CHECKOUT,
        )
    return status, out.getvalue(), err.getvalue()


def argv(host: FakeHost) -> list[tuple[str, ...]]:
    return [command for command, _ in host.calls]


class UninstallContracts(unittest.TestCase):
    def test_stage_order_closing_lines_and_two_locks(self) -> None:
        host = FakeHost()
        status, out, err = execute(host)
        self.assertEqual((status, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(tuple(line.split(":", 1)[0] for line in lines[:10]), STAGES)
        self.assertTrue(all(": ok — " in line for line in lines[:10]))
        self.assertEqual(lines[-2], "GIDEON is removed from this box.")
        self.assertTrue(lines[-1].startswith("Kept: "))
        self.assertIn(str(INSTALL_HOME), lines[-1])
        self.assertIn(f"Every re-run is {LONG_FORM}.", lines[-1])
        self.assertEqual(
            host.lock_events,
            [("take", backuplock.BACKUP_LOCK.path), ("take", backuplock.ENGINE_LOCK.path),
             ("release", backuplock.ENGINE_LOCK.path), ("release", backuplock.BACKUP_LOCK.path)],
        )
        self.assertEqual(host.locks, {})
        self.assertNotIn(os.fspath(COMMAND_PATH), host.files)
        self.assertIn("Docker absent", out)
        self.assertNotIn(cotenants.compose_projects_argv(), argv(host))
        self.assertNotIn(cotenants.network_ls_argv(), argv(host))

    def test_root_and_held_locks_refuse_before_any_mutation(self) -> None:
        host = FakeHost()
        host.euid = 1000
        status, out, err = execute(host)
        self.assertEqual(status, 1)
        self.assertEqual(out, "")
        self.assertIn(f"Fix: Run {LONG_FORM}.", err)
        self.assertEqual(host.lock_events, [])
        self.assertEqual(host.calls, [])

        holder = backuplock.Record("gideon backup", 987654, datetime(2026, 1, 1, tzinfo=UTC))
        for lock in (backuplock.BACKUP_LOCK, backuplock.ENGINE_LOCK):
            with self.subTest(lock=lock.noun):
                host = FakeHost()
                host.locks[lock.path] = holder.to_json()
                status, out, err = execute(host)
                self.assertEqual((status, err), (1, ""))
                self.assertEqual(len(out.splitlines()), 1)
                self.assertIn("preconditions: refuse", out)
                self.assertIn("gideon backup", out)
                self.assertIn("Wait for it to finish", out)
                self.assertEqual(host.calls, [])
                self.assertIn(lock.path, host.locks)
                if lock is backuplock.ENGINE_LOCK:
                    self.assertNotIn(backuplock.BACKUP_LOCK.path, host.locks)

    def test_stopped_and_unreadable_docker_refuse_before_stages(self) -> None:
        for state, detail in (("stopped", "not active"), ("unreadable", "permission denied")):
            with self.subTest(state=state):
                host = FakeHost(docker=state)
                status, out, err = execute(host)
                self.assertEqual((status, err), (1, ""))
                self.assertEqual(len(out.splitlines()), 1)
                self.assertIn("preconditions: refuse", out)
                self.assertIn(detail, out)
                self.assertIn(f"Run {LONG_FORM}.", out)
                self.assertEqual(host.locks, {})
                self.assertIn(os.fspath(COMMAND_PATH), host.files)

    def test_attached_lists_foreign_containers_and_units_leave_registry(self) -> None:
        host = FakeHost(docker="active")
        host.networks = ["gideon_integration", "foreign_network"]
        host.attached["gideon_integration"] = "own\tgideon\nclient\tother-project\n"
        host.unit_files = ["gideon-api.service", "gideon-worker.timer", REGISTRY_UNIT.name,
                           network.DOCKER_USER_UNIT.name, "foreign.service"]
        host.files[os.fspath(REGISTRY_UNIT)] = "role unit"
        host.not_found_units.add("gideon-worker.timer")
        status, out, err = execute(host)
        self.assertEqual((status, err), (1, ""))
        self.assertIn("attached: ok — gideon_integration: project other-project (client) loses the engine", out)
        self.assertIn("units: ok — disabled gideon-api.service, gideon-worker.timer", out)
        self.assertIn(f"kept {REGISTRY_UNIT.name} for the registry role", out)
        self.assertIn(("systemctl", "disable", "--now", "gideon-api.service"), argv(host))
        self.assertIn(("systemctl", "disable", "--now", "gideon-worker.timer"), argv(host))
        self.assertIn(
            ("systemctl", "show", "--property=LoadState", "--value", "gideon-worker.timer"), argv(host),
        )
        self.assertNotIn(("systemctl", "disable", "--now", REGISTRY_UNIT.name), argv(host))
        self.assertEqual(argv(host).count(("systemctl", "daemon-reload")), 2)
        self.assertEqual(
            argv(host).count(("systemctl", "disable", "--now", network.DOCKER_USER_UNIT.name)), 1,
        )
        self.assertIn(cotenants.network_ps_argv("gideon_integration"), argv(host))
        self.assertNotIn(cotenants.network_ps_argv("foreign_network"), argv(host))

    def test_empty_attached_listing_and_project_order(self) -> None:
        host = FakeHost(docker="active")
        host.projects = [PROJECT_NAME, "gideon-drill", "gideon-ci", "other"]
        host.files[os.fspath(RENDERED / "compose.yaml")] = "services: {}\n"
        status, out, err = execute(host)
        self.assertEqual((status, err), (0, ""))
        self.assertIn("attached: ok — no unmarked containers attached", out)
        downs = [(command, timeout) for command, timeout in host.calls if "down" in command]
        self.assertEqual(
            [command for command, _ in downs],
            [("docker", "compose", "-p", "gideon-drill", "down", "--volumes", "--remove-orphans"),
             ("docker", "compose", "-p", "gideon-ci", "down", "--volumes", "--remove-orphans"),
             ("docker", "compose", "--project-directory", os.fspath(RENDERED), "-f",
              os.fspath(RENDERED / "compose.yaml"), "down", "--remove-orphans")],
        )
        self.assertTrue(all(timeout is not None and timeout > 0 for _, timeout in downs))
        self.assertTrue(all(
            timeout is not None and timeout > 0
            for command, timeout in host.calls if command[:2] == ("docker", "compose")
        ))
        self.assertEqual(host.projects, ["other"])

    def test_failed_compose_down_stops_before_firewall_with_manual_fix(self) -> None:
        host = FakeHost(docker="active")
        host.projects = ["gideon-drill", PROJECT_NAME]
        down = ("docker", "compose", "-p", "gideon-drill", "down", "--volumes", "--remove-orphans")
        host.commands[down] = completed(down, returncode=1, stderr="cannot stop\nmore detail\n")
        status, out, _ = execute(host)
        self.assertEqual(status, 1)
        self.assertIn("projects: refuse", out)
        self.assertIn("cannot stop", out)
        self.assertIn(f"Run {' '.join(down)} by hand. Run {LONG_FORM}.", out)
        self.assertNotIn("firewall:", out)
        self.assertIn(os.fspath(COMMAND_PATH), host.files)

    def test_production_without_rendered_file_and_with_purge_volumes(self) -> None:
        for purge in (False, True):
            with self.subTest(purge=purge):
                host = FakeHost(docker="active")
                host.projects = [PROJECT_NAME]
                status, _, _ = execute(host, purge=purge)
                self.assertEqual(status, 0)
                command: tuple[str, ...] = ("docker", "compose", "-p", PROJECT_NAME, "down", "--remove-orphans")
                if purge:
                    command += ("--volumes",)
                self.assertIn(command, argv(host))

        host = FakeHost(docker="active")
        host.projects = [PROJECT_NAME]
        host.files[os.fspath(RENDERED / "compose.yaml")] = "services: {}\n"
        self.assertEqual(execute(host, purge=True)[0], 0)
        self.assertIn(
            ("docker", "compose", "--project-directory", os.fspath(RENDERED), "-f",
             os.fspath(RENDERED / "compose.yaml"), "down", "--remove-orphans", "--volumes"),
            argv(host),
        )

    def test_purge_after_a_plain_run_removes_the_volumes_the_plain_run_kept(self) -> None:
        host = FakeHost(docker="active")
        host.projects = [PROJECT_NAME]
        host.volumes = {
            "gideon_caddy_data": PROJECT_NAME,
            "gideon_caddy_config": PROJECT_NAME,
            "other_data": "other",
        }
        status, out, _ = execute(host)
        self.assertEqual(status, 0)
        self.assertEqual(set(host.volumes), {"gideon_caddy_data", "gideon_caddy_config", "other_data"})
        self.assertNotIn(cotenants.volume_ls_argv(), argv(host))

        host.calls.clear()
        status, out, _ = execute(host, purge=True)
        self.assertEqual(status, 0)
        self.assertEqual(host.volumes, {"other_data": "other"})
        self.assertIn(("docker", "volume", "rm", "gideon_caddy_data"), argv(host))
        self.assertIn(("docker", "volume", "rm", "gideon_caddy_config"), argv(host))
        self.assertNotIn(("docker", "volume", "rm", "other_data"), argv(host))
        self.assertIn("removed volumes gideon_caddy_data, gideon_caddy_config", out)

    def test_firewall_removes_only_tagged_jump_and_own_block(self) -> None:
        host = FakeHost()
        host.shared_chain = True
        host.shared_rules = [
            f"-A {network.SHARED_CHAIN} -j OTHER_BEFORE",
            f"-A {network.SHARED_CHAIN} -m comment --comment {network.UFW_TAG} -j {network.CHAIN}",
            f"-A {network.SHARED_CHAIN} -j OTHER_AFTER",
        ]
        host.owned_chain = True
        host.files[os.fspath(network.AFTER_RULES)] = (
            "foreign before\n\n# GIDEON BEGIN provision:firewall\nown rule\n"
            "# GIDEON END provision:firewall\nforeign after\n"
        )
        host.ufw_numbered = f"Status: inactive\n[ 1] 22/tcp ALLOW IN Anywhere # {network.UFW_TAG}\n"
        status, out, err = execute(host)
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(host.shared_rules, [
            f"-A {network.SHARED_CHAIN} -j OTHER_BEFORE",
            f"-A {network.SHARED_CHAIN} -j OTHER_AFTER",
        ])
        self.assertEqual(host.files[os.fspath(network.AFTER_RULES)], "foreign before\n\nforeign after\n")
        self.assertIn((os.fspath(network.AFTER_RULES), 0o640), host.writes)
        self.assertNotIn(("ufw", "reload"), argv(host))
        self.assertIn("kept 1 tagged ufw allow rule", out)
        commands = argv(host)
        start = commands.index(("systemctl", "disable", "--now", network.DOCKER_USER_UNIT.name))
        end = commands.index(("ufw", "status", "numbered"))
        self.assertEqual(commands[start:end + 1], [
            ("systemctl", "disable", "--now", network.DOCKER_USER_UNIT.name),
            ("systemctl", "daemon-reload"),
            ("iptables", "-w", "-S", network.SHARED_CHAIN),
            ("iptables", "-w", "-D", network.SHARED_CHAIN, "2"),
            ("ufw", "status"),
            ("iptables", "-w", "-S", network.CHAIN),
            ("iptables", "-w", "-F", network.CHAIN),
            ("iptables", "-w", "-X", network.CHAIN),
            ("ufw", "status", "numbered"),
        ])
        self.assertNotIn(os.fspath(network.DOCKER_USER_UNIT), host.files)

    def test_firewall_legacy_and_absent_blocks_and_unreadable_status(self) -> None:
        for after in (
            "foreign\n\n# BEGIN gideon-provision docker-user\nown\n# END gideon-provision docker-user\n",
            "foreign\n",
        ):
            with self.subTest(after=after):
                host = FakeHost()
                host.files[os.fspath(network.AFTER_RULES)] = after
                host.commands[("ufw", "status", "numbered")] = completed(
                    ("ufw", "status", "numbered"), returncode=1, stderr="unreadable\n",
                )
                status, out, err = execute(host)
                self.assertEqual((status, err), (0, ""))
                self.assertIn("tagged ufw allow rules not counted", out)
                self.assertEqual(host.files[os.fspath(network.AFTER_RULES)], "foreign\n")
                self.assertNotIn(("ufw", "reload"), argv(host))
                self.assertNotIn(("iptables", "-w", "-F", network.CHAIN), argv(host))
                self.assertNotIn(("iptables", "-w", "-X", network.CHAIN), argv(host))

    def test_files_and_configuration_plain_then_purge(self) -> None:
        host = FakeHost()
        kept = (JOURNALD_DROP_IN, SSHD_POLICY, AUTO_UPGRADES_DROP_IN)
        purged = (PROXY_CONF, network.CHRONY_SOURCES, network.WAIT_ONLINE_DROPIN)
        for path in (*kept, *purged, ETC_GIDEON):
            host.files[os.fspath(path)] = "retained value"
        host.chrony_is_active = True
        status, out, _ = execute(host)
        self.assertEqual(status, 0)
        self.assertTrue(all(os.fspath(path) in host.files for path in (*kept, *purged, ETC_GIDEON)))
        self.assertIn("kept box-wide settings", out)
        self.assertIn(f"run {PURGE_FORM}.", out)
        self.assertNotIn(("chronyc", "reload", "sources"), argv(host))
        host.calls.clear()
        status, out, _ = execute(host, purge=True)
        self.assertEqual(status, 0)
        self.assertTrue(all(os.fspath(path) in host.files for path in kept))
        self.assertTrue(all(os.fspath(path) not in host.files for path in (*purged, ETC_GIDEON)))
        self.assertIn(("systemctl", "is-active", "chrony"), argv(host))
        self.assertIn(("chronyc", "reload", "sources"), argv(host))
        self.assertEqual(argv(host).count(("systemctl", "daemon-reload")), 3)
        self.assertIn(("rm", "-rf", os.fspath(ETC_GIDEON)), argv(host))
        self.assertIn(f"Every re-run is {PURGE_FORM}.", out)

    def test_purge_skips_chrony_reload_when_inactive_or_dropin_absent(self) -> None:
        for present, active in ((True, False), (False, True)):
            with self.subTest(present=present, active=active):
                host = FakeHost()
                host.chrony_is_active = active
                if present:
                    host.files[os.fspath(network.CHRONY_SOURCES)] = "source"
                status, _, _ = execute(host, purge=True)
                self.assertEqual(status, 0)
                self.assertNotIn(("chronyc", "reload", "sources"), argv(host))
                if not present:
                    self.assertNotIn(("systemctl", "is-active", "chrony"), argv(host))
                self.assertEqual(argv(host).count(("systemctl", "daemon-reload")), 2)

    def test_data_purge_keeps_build_box_roles_across_two_runs(self) -> None:
        host = FakeHost()
        host.files[os.fspath(REGISTRY_UNIT)] = "role"
        host.files[os.fspath(RUNNER_SUDOERS)] = "role"
        host.files[os.fspath(ETC_GIDEON)] = "configuration"
        for name in disk.data_directories():
            host.files[os.fspath(disk.DATA_MOUNT / name)] = "directory"
        expected_removed = {
            os.fspath(disk.DATA_MOUNT / name) for name in disk.data_directories()
            if name not in ("registry", "acceptance")
        }
        for run in range(2):
            with self.subTest(run=run):
                status, out, _ = execute(host, purge=True)
                self.assertEqual(status, 0)
                self.assertIn("/data/registry (registry role)", out)
                self.assertIn("/data/acceptance (runner role)", out)
                self.assertIn(os.fspath(REGISTRY_UNIT), host.files)
                self.assertIn(os.fspath(RUNNER_SUDOERS), host.files)
                self.assertIn(os.fspath(disk.DATA_MOUNT / "registry"), host.files)
                self.assertIn(os.fspath(disk.DATA_MOUNT / "acceptance"), host.files)
                self.assertNotIn(os.fspath(ETC_GIDEON), host.files)
        removals = [command for command in argv(host) if command[:2] == ("rm", "-rf")]
        self.assertEqual(
            {command[-1] for command in removals if "--one-file-system" in command},
            expected_removed,
        )
        self.assertTrue(all(command[:3] == ("rm", "-rf", "--one-file-system")
                            for command in removals if command[-1] in expected_removed))
        self.assertEqual(len([command for command in removals if command[-1] in expected_removed]),
                         len(expected_removed))
        self.assertFalse(any(os.fspath(disk.DATA_MOUNT) == part for command in argv(host) for part in command))

    def test_plain_data_row_counts_layout_and_keeps_directories(self) -> None:
        host = FakeHost()
        for name in disk.data_directories():
            host.files[os.fspath(disk.DATA_MOUNT / name)] = "directory"
        status, out, _ = execute(host)
        self.assertEqual(status, 0)
        self.assertIn(f"data: ok — kept {len(disk.data_directories())} GIDEON directories", out)
        self.assertIn(f"run {PURGE_FORM}.", out)
        self.assertTrue(all(os.fspath(disk.DATA_MOUNT / name) in host.files
                            for name in disk.data_directories()))
        self.assertFalse(any(command[:2] == ("rm", "-rf") for command in argv(host)))

    def test_held_network_fails_last_then_second_run_removes_it(self) -> None:
        host = FakeHost(docker="active")
        host.networks = ["gideon_integration", "gideon_free", "foreign_network"]
        host.attached["gideon_integration"] = "client\tother-project\n"
        status, out, err = execute(host)
        self.assertEqual((status, err), (1, ""))
        self.assertIn("networks: refuse", out)
        self.assertIn("gideon_integration: project other-project (client)", out)
        self.assertIn("Fix: Announce to the attached containers' operator", out)
        self.assertIn(f"Run {LONG_FORM}.", out)
        self.assertIn("gideon_integration", host.networks)
        self.assertNotIn("gideon_free", host.networks)
        self.assertIn("foreign_network", host.networks)
        self.assertNotIn("GIDEON is removed from this box.", out)
        host.attached["gideon_integration"] = ""
        status, out, _ = execute(host)
        self.assertEqual(status, 0)
        self.assertIn(("docker", "network", "rm", "gideon_integration"), argv(host))
        self.assertIn("GIDEON is removed from this box.", out)

    def test_second_plain_run_is_ok_with_absent_details_and_no_new_removals(self) -> None:
        host = FakeHost()
        self.assertEqual(execute(host)[0], 0)
        removed = list(host.removed)
        status, out, _ = execute(host)
        self.assertEqual(status, 0)
        self.assertEqual(host.removed, removed)
        self.assertEqual(tuple(line.split(":", 1)[0] for line in out.splitlines()[:10]), STAGES)
        self.assertIn(f"{COMMAND_PATH} already absent", out)
        self.assertIn("chain already absent", out)

    def test_secret_path_never_reaches_subprocess_and_fixes_use_checkout_form(self) -> None:
        for installed in (False, True):
            with self.subTest(installed=installed):
                report.set_installed_form(installed)
                try:
                    unprivileged = FakeHost()
                    unprivileged.euid = 1000
                    status, out, err = execute(unprivileged)
                    self.assertEqual((status, out), (1, ""))
                    self.assertIn(f"Fix: Run {LONG_FORM}.", err)
                    host = FakeHost(docker="active")
                    host.files[os.fspath(ETC_GIDEON / "secrets" / "fictitious-secret")] = "secret"
                    host.projects = [PROJECT_NAME]
                    host.commands[cotenants.compose_projects_argv()] = completed(
                        cotenants.compose_projects_argv(), stdout="malformed",
                    )
                    status, out, _ = execute(host)
                    self.assertEqual(status, 1)
                    self.assertIn(f"Fix: Run {LONG_FORM}.", out)
                    self.assertNotIn("fictitious-secret", repr(argv(host)))
                    self.assertNotIn("Fix: Run gideon uninstall", out)
                    host.commands.clear()
                    host.projects.clear()
                    status, out, _ = execute(host)
                    self.assertEqual(status, 0)
                    self.assertIn(f"run {PURGE_FORM}.", out)
                    self.assertIn(f"Every re-run is {LONG_FORM}.", out)
                    status, out, _ = execute(host, purge=True)
                    self.assertEqual(status, 0)
                    self.assertIn(f"Every re-run is {PURGE_FORM}.", out)
                    self.assertNotIn(os.fspath(ETC_GIDEON / "secrets" / "fictitious-secret"), host.files)
                    self.assertIn(("rm", "-rf", os.fspath(ETC_GIDEON)), argv(host))
                    self.assertNotIn("fictitious-secret", repr(argv(host)))
                    self.assertNotIn("Fix: Run gideon uninstall", out)
                finally:
                    report.set_installed_form(False)
