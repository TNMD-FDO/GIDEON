"""Synthetic co-tenant acceptance contracts over the fake VM host."""

import ast
import contextlib
import io
import ipaddress
import json
import shlex
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml  # type: ignore[import-untyped]
from test_acceptance import ROOT, FakeHost, completed, harness_context

import gideon.host.uninstall as host_uninstall
from gideon.host import cotenants
from gideon.host.apply import PRINT_ONCE_SUFFIX
from gideon.host.steps import disk, docker, network
from gideon.host.steps.command import COMMAND_PATH
from gideon.host.steps.site_dirs import ETC_GIDEON
from gideon.host.sysio import Command, PathLike
from tools.acceptance import cotenant, rehearsal, seed, vm
from tools.acceptance.cli import main
from tools.acceptance.context import DEFAULT_COTENANT_VM_NAMES, HarnessContext
from tools.acceptance.run import (
    COTENANT_AFTER_IDENTIFIERS,
    COTENANT_AFTER_STAGES,
    COTENANT_BEFORE_IDENTIFIERS,
    COTENANT_BEFORE_STAGES,
    STAGES,
    form_stages,
)


def row(name: str, outcome: str, detail: str = "checked") -> str:
    return f"{name}: {outcome} — {detail}"


class ScriptHost(FakeHost):
    """Answer scripted SSH reads and preserve the FakeHost call record."""

    def __init__(self) -> None:
        super().__init__()
        self.replies: dict[str, list[subprocess.CompletedProcess[str]]] = {}
        self.products: dict[tuple[str, ...], list[subprocess.CompletedProcess[str]]] = {}
        self.product_calls: list[tuple[tuple[str, ...], str]] = []
        self.presence: dict[str, bool] = {}
        self.presence_after_product: dict[tuple[str, ...], dict[str, bool]] = {}

    def answer(self, script: str, *, stdout: str = "", code: int = 0) -> None:
        self.replies[script] = [completed(("ssh",), code, stdout)]

    def sequence(self, script: str, replies: list[tuple[int, str]]) -> None:
        self.replies[script] = [completed(("ssh",), code, text) for code, text in replies]

    def product(self, argv: tuple[str, ...], *, stdout: str = "", code: int = 0) -> None:
        self.products[argv] = [completed(("ssh",), code, stdout)]

    @staticmethod
    def _next(replies: list[subprocess.CompletedProcess[str]]) -> subprocess.CompletedProcess[str]:
        return replies.pop(0) if len(replies) > 1 else replies[0]

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
        fallback = super().run(
            argv, check=check, input=input, cwd=cwd, env=env,
            timeout=timeout, passthrough=passthrough,
        )
        command = tuple(argv)
        if not command or command[0] != "ssh":
            return fallback
        script = shlex.split(command[-1])[0]
        if " && sudo " in script and " 2>&1 | tee " in script:
            command_text = script.split(" && sudo ", 1)[1].split(" 2>&1 | tee ", 1)[0]
            product_argv = tuple(shlex.split(command_text))
            self.product_calls.append((product_argv, script))
            if product_argv in self.presence_after_product:
                self.presence = self.presence_after_product[product_argv].copy()
            replies = self.products.get(product_argv)
            return self._next(replies) if replies else completed(command, 127)
        if script.startswith("sudo sh -c ") and "for path in " in script:
            inner = shlex.split(script)[3]
            names = shlex.split(inner.split("for path in ", 1)[1].split("; do ", 1)[0])
            listing = "".join(
                f"{'present' if self.presence.get(name, False) else 'absent'} {name}\n"
                for name in names
            )
            return completed(command, stdout=listing)
        replies = self.replies.get(script)
        if replies:
            return self._next(replies)
        if script.startswith("sudo sh -c "):
            return completed(command)
        return fallback


def context(host: ScriptHost, form: str = "before") -> HarnessContext:
    ctx = harness_context(host)
    ctx.spec = replace(ctx.spec, cotenant=form)
    identifiers = COTENANT_BEFORE_IDENTIFIERS if form == "before" else COTENANT_AFTER_IDENTIFIERS
    ctx.stage_index = identifiers.index("cotenant") + 1
    now = [0.0]
    ctx.clock = lambda: now[0]
    ctx.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
    return ctx


def intact_replies(host: ScriptHost) -> None:
    host.answer(f"sudo docker inspect -f '{{{{.State.Status}}}}' {cotenant.CONTAINER}", stdout="running\n")
    host.answer("sudo cat /etc/docker/daemon.json", stdout=json.dumps({cotenant.KEY: cotenant.KEY_VALUE}))
    host.answer(f"sudo cat {cotenant.DATA_DIR}/marker", stdout=f"{cotenant.MARKER_TEXT}\n")
    host.answer(
        "sudo iptables -w -S DOCKER-USER",
        stdout=f"-A DOCKER-USER -s {cotenant.RULE_CIDR} -m comment "
        f"--comment {cotenant.RULE_COMMENT} -j RETURN\n",
    )
    host.answer(f"sudo systemctl is-enabled {cotenant.TIMER}", stdout="enabled\n")
    host.answer(f"sudo systemctl is-active {cotenant.TIMER}", stdout="active\n")
    host.answer("sudo findmnt -rn -o TARGET --mountpoint /data", stdout="/data\n")


class Tables(unittest.TestCase):
    def test_order_keys_and_shared_stages(self) -> None:
        shared = {stage.identifier: stage for stage in STAGES}
        for stages, identifiers in (
            (COTENANT_BEFORE_STAGES, COTENANT_BEFORE_IDENTIFIERS),
            (COTENANT_AFTER_STAGES, COTENANT_AFTER_IDENTIFIERS),
        ):
            with self.subTest(form=identifiers):
                self.assertEqual(tuple(stage.identifier for stage in stages), identifiers)
                self.assertEqual(len(identifiers), len(set(identifiers)))
                self.assertEqual(identifiers[-1], "teardown")
                self.assertLess(identifiers.index("uninstall"), identifiers.index("purge"))
                self.assertEqual(stages[identifiers.index("provision-2")].key, "provision-2")
                for stage in stages:
                    if stage.identifier in shared and stage.identifier not in (
                        "provision", "provision-2",
                    ):
                        self.assertIs(stage, shared[stage.identifier])
                for identifier in identifiers:
                    if identifier.startswith("intact-"):
                        stage = stages[identifiers.index(identifier)]
                        self.assertEqual(stage.name, "intact")
                        self.assertIs(stage.callable, cotenant.intact)
                        predecessor = (
                            "cotenant" if identifier == "intact-arrival" else
                            "reboot" if identifier == "intact-provision" else
                            identifier.removeprefix("intact-")
                        )
                        self.assertEqual(identifiers[identifiers.index(identifier) - 1], predecessor)
        self.assertLess(
            COTENANT_BEFORE_IDENTIFIERS.index("refuse-data"),
            COTENANT_BEFORE_IDENTIFIERS.index("cotenant"),
        )
        self.assertLess(
            COTENANT_BEFORE_IDENTIFIERS.index("cotenant"),
            COTENANT_BEFORE_IDENTIFIERS.index("provision"),
        )
        self.assertEqual(
            COTENANT_AFTER_IDENTIFIERS[COTENANT_AFTER_IDENTIFIERS.index("install") + 1],
            "cotenant",
        )
        self.assertIs(form_stages(context(ScriptHost(), "before").spec), COTENANT_BEFORE_STAGES)
        self.assertIs(form_stages(context(ScriptHost(), "after").spec), COTENANT_AFTER_STAGES)


class CommandLine(unittest.TestCase):
    def test_dry_run_names_form_and_default_names_without_host_io(self) -> None:
        for form, identifiers in (
            ("before", COTENANT_BEFORE_IDENTIFIERS),
            ("after", COTENANT_AFTER_IDENTIFIERS),
        ):
            with self.subTest(form=form):
                host = ScriptHost()
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = main(["HEAD", "--cotenant", form, "--dry-run"], host=host)
                self.assertEqual(code, 0)
                self.assertIn(f"Acceptance dry run (co-tenant {form}):", output.getvalue())
                self.assertIn(f"stages: {', '.join(identifiers)}", output.getvalue())
                self.assertIn(DEFAULT_COTENANT_VM_NAMES[form], output.getvalue())
                self.assertEqual(host.calls, [])

    def test_other_form_until_refuses_before_host_io(self) -> None:
        for args, flag in (
            (["--cotenant", "before", "--until", "intact-arrival"], "--cotenant after"),
            (["--cotenant", "after", "--until", "refuse-data"], "--cotenant before"),
            (["--until", "set"], "--full-restore"),
        ):
            with self.subTest(args=args):
                host = ScriptHost()
                error = io.StringIO()
                with contextlib.redirect_stderr(error):
                    code = main(["HEAD", *args], host=host)
                self.assertEqual(code, 1)
                self.assertIn(flag, error.getvalue())
                self.assertEqual(host.calls, [])

    def test_cotenant_and_full_restore_are_exclusive(self) -> None:
        error = io.StringIO()
        with contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(["HEAD", "--cotenant", "before", "--full-restore"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("not allowed", error.getvalue())


class Arrival(unittest.TestCase):
    def test_copies_fixture_and_runs_exact_form_command(self) -> None:
        filenames = (
            "arrive.sh", "compose.yaml", "cotenant-rule.service",
            "cotenant-tick.service", "cotenant-tick.timer",
        )
        for form, command in (
            ("before", ("bash", "arrive.sh", "first", cotenant.DATA_DISK)),
            ("after", ("bash", "arrive.sh", "later")),
        ):
            with self.subTest(form=form):
                host = ScriptHost()
                for filename in filenames:
                    path = cotenant.FIXTURE_DIR / filename
                    host.files[str(path)] = path.read_text()
                intact_replies(host)
                host.answer(f"sudo install -d {cotenant.STAGING}")
                host.answer("sudo docker --version", stdout="Docker version synthetic\n")
                host.product(command, stdout="fixture complete\n")
                ctx = context(host, form)
                result = cotenant.arrive(ctx)
                self.assertTrue(result.ok, result.detail)
                self.assertTrue(ctx.cotenant_arrived)
                self.assertEqual([call[0] for call in host.product_calls], [command])
                self.assertTrue(host.product_calls[0][1].startswith(f"cd {cotenant.STAGING} && sudo "))
                copies = [
                    (shlex.split(call[0][-1])[0], call[2])
                    for call in host.calls
                    if call[0][0] == "ssh" and call[2] is not None
                ]
                self.assertEqual(len(copies), len(filenames))
                for filename, (script, content) in zip(filenames, copies, strict=True):
                    self.assertEqual(content, host.files[str(cotenant.FIXTURE_DIR / filename)])
                    self.assertEqual(shlex.split(script)[-1], f"{cotenant.STAGING}/{filename}")
                    self.assertIn("chown root:root", script)
                    self.assertIn("chmod 0755" if filename == "arrive.sh" else "chmod 0644", script)
                transcript = str(ctx.spec.out / f"{ctx.stage_index:02d}-cotenant-arrive.txt")
                self.assertEqual(host.files[transcript], "fixture complete\n")

    def test_transcript_is_redacted_and_failed_script_is_red(self) -> None:
        for code in (0, 1):
            with self.subTest(code=code):
                host = ScriptHost()
                for path in cotenant.FIXTURE_DIR.iterdir():
                    host.files[str(path)] = path.read_text()
                host.answer(f"sudo install -d {cotenant.STAGING}")
                if code == 0:
                    intact_replies(host)
                    host.answer("sudo docker --version", stdout="Docker version synthetic\n")
                secret = f"gideon_admin_password (break-glass administrator): value{PRINT_ONCE_SUFFIX}"
                command = ("bash", "arrive.sh", "first", cotenant.DATA_DISK)
                host.product(command, stdout=secret + "\n", code=code)
                ctx = context(host)
                result = cotenant.arrive(ctx)
                transcript = host.files[str(ctx.spec.out / f"{ctx.stage_index:02d}-cotenant-arrive.txt")]
                self.assertNotIn("value", transcript)
                self.assertIn("<redacted>", transcript)
                self.assertEqual(result.ok, code == 0)


class Intact(unittest.TestCase):
    def test_running_and_recovery_with_fake_clock(self) -> None:
        for readings in (
            [(0, "running\n")],
            [(0, "restarting\n"), (0, "running\n")],
            [(1, ""), (0, "running\n")],
        ):
            with self.subTest(readings=readings):
                host = ScriptHost()
                intact_replies(host)
                host.sequence(
                    f"sudo docker inspect -f '{{{{.State.Status}}}}' {cotenant.CONTAINER}",
                    readings,
                )
                ctx = context(host)
                ctx.cotenant_arrived = True
                self.assertTrue(cotenant.intact(ctx).ok)
                self.assertEqual(ctx.clock(), (len(readings) - 1) * vm.POLL_INTERVAL_SECONDS)

    def test_unhealthy_container_expires_with_status_and_bound(self) -> None:
        for code, status in ((0, "exited\n"), (1, "")):
            with self.subTest(code=code):
                host = ScriptHost()
                intact_replies(host)
                host.sequence(
                    f"sudo docker inspect -f '{{{{.State.Status}}}}' {cotenant.CONTAINER}",
                    [(code, status)],
                )
                ctx = context(host)
                ctx.cotenant_arrived = True
                result = cotenant.intact(ctx)
                self.assertFalse(result.ok)
                self.assertIn(
                    "exited" if code == 0 else "not inspectable (exit 1)", result.detail,
                )
                self.assertIn(str(cotenant.CONTAINER_WAIT_SECONDS), result.detail)
                self.assertEqual(ctx.clock(), cotenant.CONTAINER_WAIT_SECONDS)

    def test_each_state_read_can_refuse(self) -> None:
        cases = (
            ("sudo cat /etc/docker/daemon.json", "{}", cotenant.KEY),
            ("sudo cat /etc/docker/daemon.json", json.dumps({cotenant.KEY: True}), "True"),
            ("sudo cat /etc/docker/daemon.json", json.dumps({cotenant.KEY: cotenant.KEY_VALUE + 1}), str(cotenant.KEY_VALUE + 1)),
            (f"sudo cat {cotenant.DATA_DIR}/marker", "changed\n", "marker"),
            ("sudo iptables -w -S DOCKER-USER", "-A DOCKER-USER -j RETURN\n", cotenant.RULE_COMMENT),
            (f"sudo systemctl is-enabled {cotenant.TIMER}", "disabled\n", "disabled"),
            (f"sudo systemctl is-active {cotenant.TIMER}", "inactive\n", "inactive"),
            ("sudo findmnt -rn -o TARGET --mountpoint /data", "", "/data"),
        )
        for script, reply, detail in cases:
            with self.subTest(script=script, reply=reply):
                host = ScriptHost()
                intact_replies(host)
                host.answer(script, stdout=reply)
                ctx = context(host)
                ctx.cotenant_arrived = True
                result = cotenant.intact(ctx)
                self.assertFalse(result.ok)
                self.assertIn(detail, result.detail)

    def test_arrival_is_required_before_reads(self) -> None:
        host = ScriptHost()
        result = cotenant.intact(context(host))
        self.assertFalse(result.ok)
        self.assertEqual(host.calls, [])


DATA_ONLY = ("python3", "-m", "gideon", "host", "provision", "--no-gpu", "--only", "disk-layout")
USER_ONLY = ("python3", "-m", "gideon", "host", "provision", "--no-gpu", "--only", "service-user")
SOURCE_ONLY = ("python3", "-m", "gideon", "host", "provision", "--no-gpu", "--only", "docker-engine")


def data_host(outcome: str = "failed") -> tuple[ScriptHost, HarnessContext]:
    host = ScriptHost()
    ctx = context(host)
    host.product(USER_ONLY, stdout=row("service-user", "ok") + "\n")
    host.product(DATA_ONLY, code=1 if outcome == "failed" else 0,
                 stdout=row("disk-layout", outcome, f"1 entry {cotenant.NAME}") + "\n")
    host.answer(f"sudo install -d {cotenant.DATA_DIR}")
    host.answer(f"sudo rm -rf -- {cotenant.DATA_DIR}")
    host.answer("sudo pvs --noheadings -o pv_name", stdout="/dev/sda3\n")
    host.answer(f"sudo cat {disk._FSTAB}", stdout="UUID=example / ext4 defaults 0 1\n")
    host.answer(f"sudo findmnt -rn -o TARGET --mountpoint {disk.DATA_MOUNT}", code=1)
    host.answer(
        f"sudo lsblk -J -o NAME,FSTYPE,TYPE {cotenant.DATA_DISK}",
        stdout=json.dumps({"blockdevices": [{
            "name": Path(cotenant.DATA_DISK).name, "fstype": None,
            "type": "disk", "children": [],
        }]}),
    )
    return host, ctx


def source_host(outcome: str = "failed") -> tuple[ScriptHost, HarnessContext]:
    host = ScriptHost()
    ctx = context(host)
    ctx.cotenant_arrived = True
    intact_replies(host)
    host.answer("sudo sh -c '. /etc/os-release; printf %s \"$VERSION_CODENAME\"'", stdout="noble")
    host.answer(f"sudo rm -f -- {cotenant.FOREIGN_SOURCE}")
    host.answer(f"sudo sha256sum {docker._SOURCE} {docker._KEYRING}", stdout="a source\nb key\n")
    host.answer("sudo systemctl show -p ActiveEnterTimestamp docker", stdout="ActiveEnterTimestamp=sample\n")
    host.product(
        SOURCE_ONLY, code=1 if outcome == "failed" else 0,
        stdout=row("docker-engine", outcome, f"{docker._SOURCE} and {cotenant.FOREIGN_SOURCE}") + "\n",
    )
    return host, ctx


class Refusals(unittest.TestCase):
    def test_data_refusal_and_cleanup_on_both_outcomes(self) -> None:
        for outcome in ("failed", "ok", "applied"):
            with self.subTest(outcome=outcome):
                host, ctx = data_host(outcome)
                result = cotenant.refuse_data(ctx)
                self.assertEqual(result.name, "refuse-data")
                self.assertEqual(result.ok, outcome == "failed", result.detail)
                self.assertEqual([call[0] for call in host.product_calls], [USER_ONLY, DATA_ONLY])
                self.assertIn(
                    f"sudo rm -rf -- {cotenant.DATA_DIR}",
                    [shlex.split(call[0][-1])[0] for call in host.calls if call[0][0] == "ssh"],
                )

    def test_data_unchanged_reads_name_each_mismatch(self) -> None:
        cases = (
            ("pvs", "pvs"),
            ("fstab", str(disk._FSTAB)),
            ("mount", str(disk.DATA_MOUNT)),
            ("lsblk", cotenant.DATA_DISK),
        )
        for change, detail in cases:
            with self.subTest(change=change):
                host, ctx = data_host()
                if change == "pvs":
                    host.sequence("sudo pvs --noheadings -o pv_name", [(0, "/dev/sda3\n"), (0, "/dev/sda3\n/dev/sdb1\n")])
                elif change == "fstab":
                    host.answer(f"sudo cat {disk._FSTAB}", stdout=f"{disk._BEGIN}\n")
                elif change == "mount":
                    host.answer(f"sudo findmnt -rn -o TARGET --mountpoint {disk.DATA_MOUNT}", stdout=str(disk.DATA_MOUNT))
                else:
                    host.answer(
                        f"sudo lsblk -J -o NAME,FSTYPE,TYPE {cotenant.DATA_DISK}",
                        stdout=json.dumps({"blockdevices": [{"name": Path(cotenant.DATA_DISK).name, "type": "disk", "fstype": "ext4"}]}),
                    )
                result = cotenant.refuse_data(ctx)
                self.assertFalse(result.ok)
                self.assertIn(detail, result.detail)
                self.assertIn(f"sudo rm -rf -- {cotenant.DATA_DIR}", [shlex.split(call[0][-1])[0] for call in host.calls if call[0][0] == "ssh"])

    def test_source_refusal_and_cleanup_on_both_outcomes(self) -> None:
        for outcome in ("failed", "ok", "applied"):
            with self.subTest(outcome=outcome):
                host, ctx = source_host(outcome)
                result = cotenant.refuse_source(ctx)
                self.assertEqual(result.name, "refuse-source")
                self.assertEqual(result.ok, outcome == "failed", result.detail)
                self.assertEqual([call[0] for call in host.product_calls], [SOURCE_ONLY])
                copied = [(shlex.split(call[0][-1])[0], call[2]) for call in host.calls if call[0][0] == "ssh" and call[2] is not None]
                self.assertEqual(len(copied), 1)
                self.assertEqual(shlex.split(copied[0][0])[-1], cotenant.FOREIGN_SOURCE)
                self.assertIn(str(docker._KEYRING), copied[0][1])
                self.assertIn(docker._REPOSITORY, copied[0][1])
                self.assertIn(cotenant.FOREIGN_SOURCE, [shlex.split(call[0][-1])[0].split()[-1] for call in host.calls if call[0][0] == "ssh"])

    def test_source_unchanged_reads_name_each_mismatch(self) -> None:
        for change, detail in (
            ("hashes", str(docker._SOURCE)),
            ("timestamp", "ActiveEnterTimestamp"),
            ("container", cotenant.CONTAINER),
        ):
            with self.subTest(change=change):
                host, ctx = source_host()
                if change == "hashes":
                    host.sequence(f"sudo sha256sum {docker._SOURCE} {docker._KEYRING}", [(0, "a source\nb key\n"), (0, "c source\nb key\n")])
                elif change == "timestamp":
                    host.sequence("sudo systemctl show -p ActiveEnterTimestamp docker", [(0, "ActiveEnterTimestamp=before\n"), (0, "ActiveEnterTimestamp=after\n")])
                else:
                    host.answer(f"sudo docker inspect -f '{{{{.State.Status}}}}' {cotenant.CONTAINER}", stdout="exited\n")
                result = cotenant.refuse_source(ctx)
                self.assertFalse(result.ok)
                self.assertIn(detail, result.detail)
                self.assertIn(
                    f"sudo rm -f -- {cotenant.FOREIGN_SOURCE}",
                    [shlex.split(call[0][-1])[0] for call in host.calls if call[0][0] == "ssh"],
                )

    def test_cleanup_failure_is_red(self) -> None:
        for factory, command, stage in (
            (data_host, f"sudo rm -rf -- {cotenant.DATA_DIR}", cotenant.refuse_data),
            (source_host, f"sudo rm -f -- {cotenant.FOREIGN_SOURCE}", cotenant.refuse_source),
        ):
            with self.subTest(command=command):
                host, ctx = factory()
                host.answer(command, code=3)
                result = stage(ctx)
                self.assertFalse(result.ok)
                self.assertIn("cleanup", result.detail)
                self.assertIn("3", result.detail)


class Upgrade(unittest.TestCase):
    def test_one_tagged_leg_without_acknowledgment(self) -> None:
        for form, stages in (
            ("before", COTENANT_BEFORE_STAGES),
            ("after", COTENANT_AFTER_STAGES),
        ):
            for outcome in ("ok", "refuse"):
                with self.subTest(form=form, outcome=outcome):
                    host = ScriptHost()
                    ctx = context(host, form)
                    version = "1000.0.0"
                    tag = rehearsal.rc_tag(version)
                    command = ("./upgrade.sh", tag)
                    host.product(command, stdout=row("upgrade", outcome) + "\n")
                    stage = next(stage for stage in stages if stage.identifier == "upgrade")
                    with (
                        patch.object(rehearsal, "base_version", return_value=version),
                        patch.object(rehearsal, "make_tag", return_value=None) as make_tag,
                    ):
                        result = stage.callable(ctx)
                    make_tag.assert_called_once_with(ctx, version, tag)
                    self.assertEqual(result.ok, outcome == "ok")
                    self.assertEqual([call[0] for call in host.product_calls], [command])
                    self.assertNotIn(cotenants.ACKNOWLEDGE_DISRUPTION_FLAG, host.product_calls[0][0])
                    self.assertEqual(ctx.rc_tag, tag)

    def test_before_provisions_acknowledge_and_after_provisions_do_not(self) -> None:
        for stages, acknowledged in (
            (COTENANT_BEFORE_STAGES, True),
            (COTENANT_AFTER_STAGES, False),
        ):
            with self.subTest(acknowledged=acknowledged):
                host = ScriptHost()
                ctx = context(host, "before" if acknowledged else "after")
                argv = (
                    "python3", "-m", "gideon", "host", "provision", "--no-gpu",
                    *((cotenants.ACKNOWLEDGE_DISRUPTION_FLAG,) if acknowledged else ()),
                )
                host.product(argv, stdout=row("provision", "ok") + "\n")
                for identifier in ("provision", "provision-2"):
                    stage = next(stage for stage in stages if stage.identifier == identifier)
                    self.assertTrue(stage.callable(ctx).ok)
                self.assertEqual([call[0] for call in host.product_calls], [argv, argv])


UNINSTALL_ARGV = ("python3", "-m", "gideon", "uninstall")
PURGE_ARGV = (*UNINSTALL_ARGV, "--purge")
DROPINS = (
    *(item.path for item in host_uninstall.KEPT_DROP_INS),
    *(item.path for item in host_uninstall.PURGED_DROP_INS),
)
DATA_PATHS = tuple(disk.DATA_MOUNT / name for name in disk.data_directories())
VOLUMES = ("example-volume-a", "example-volume-b")


def uninstall_text(*, bad_row: int | None = None, closing: bool = True) -> str:
    lines = [row(f"check-{index}", "refuse" if index == bad_row else "ok") for index in range(10)]
    if closing:
        lines.append("GIDEON is removed from this box.")
    return "\n".join(lines) + "\n"


def removal_host() -> tuple[ScriptHost, HarnessContext, dict[str, bool], dict[str, bool]]:
    host = ScriptHost()
    ctx = context(host)
    before = {str(path): index % 2 == 0 for index, path in enumerate(DROPINS)}
    plain = {
        **before,
        str(ETC_GIDEON): True,
        **{str(path): True for path in DATA_PATHS},
        str(COMMAND_PATH): False,
    }
    purged = {
        **{str(path): before[str(path)] for path in (item.path for item in host_uninstall.KEPT_DROP_INS)},
        **{str(path): False for path in (item.path for item in host_uninstall.PURGED_DROP_INS)},
        str(ETC_GIDEON): False,
        **{str(path): False for path in DATA_PATHS},
        str(COMMAND_PATH): False,
        str(disk.DATA_MOUNT): True,
        cotenant.DATA_DIR: True,
    }
    host.presence = before.copy()
    host.presence_after_product[UNINSTALL_ARGV] = plain
    host.presence_after_product[PURGE_ARGV] = purged
    host.product(UNINSTALL_ARGV, stdout=uninstall_text())
    host.product(PURGE_ARGV, stdout=uninstall_text())
    volume_command = f"sudo {shlex.join(cotenants.volume_ls_argv())}"
    volume_rows = "".join(f"{volume}\tgideon\n" for volume in VOLUMES)
    host.sequence(volume_command, [(0, volume_rows), (0, volume_rows), (0, "")])
    host.answer("sudo systemctl list-unit-files --no-legend --no-pager", stdout="cotenant-tick.timer enabled\n")
    host.answer("sudo docker compose ls --all --format json", stdout='[{"Name":"cotenant"}]')
    host.answer("sudo docker network ls --format '{{.Name}}'", stdout="cotenant_net\n")
    host.answer(f"sudo iptables -w -S {network.CHAIN}", code=1)
    host.answer(f"sudo iptables -w -S {network.SHARED_CHAIN}", stdout="-A DOCKER-USER -j RETURN\n")
    host.answer(f"sudo findmnt -rn -o TARGET --mountpoint {disk.DATA_MOUNT}", stdout=f"{disk.DATA_MOUNT}\n")
    return host, ctx, plain, purged


class Removal(unittest.TestCase):
    def test_paths_come_from_their_owners(self) -> None:
        self.assertEqual(cotenant._DATA_PATHS, DATA_PATHS)
        self.assertEqual(cotenant._DROPIN_PATHS, DROPINS)
        self.assertEqual(cotenant._KEPT_AFTER_PLAIN, (ETC_GIDEON, *DATA_PATHS))
        self.assertEqual(cotenant._ABSENT_AFTER_PLAIN, (COMMAND_PATH,))
        self.assertEqual(
            cotenant._ABSENT_AFTER_PURGE,
            (ETC_GIDEON, *DATA_PATHS, COMMAND_PATH,
             *(item.path for item in host_uninstall.PURGED_DROP_INS)),
        )
        self.assertEqual(cotenant._KEPT_AFTER_PURGE, (disk.DATA_MOUNT, Path(cotenant.DATA_DIR)))

    def test_plain_records_state_and_purge_removes_only_owned_state(self) -> None:
        host, ctx, plain, purged = removal_host()
        expected_before = host.presence.copy()
        plain_result = cotenant.uninstall(ctx)
        self.assertTrue(plain_result.ok, plain_result.detail)
        self.assertEqual(ctx.dropins_before, expected_before)
        self.assertEqual(ctx.volumes_before, tuple(sorted(VOLUMES)))
        self.assertEqual(host.presence, plain)
        purged_result = cotenant.purge(ctx)
        self.assertTrue(purged_result.ok, purged_result.detail)
        self.assertEqual(host.presence, purged)
        self.assertEqual([call[0] for call in host.product_calls], [UNINSTALL_ARGV, PURGE_ARGV])
        for argv, script in host.product_calls:
            self.assertTrue(script.startswith(f"cd {vm.VM_CHECKOUT} && sudo "))
            self.assertEqual(argv, PURGE_ARGV if "--purge" in argv else UNINSTALL_ARGV)

    def test_every_row_must_be_ok_and_closing_line_present(self) -> None:
        for bad_row, closing in ((4, True), (None, False)):
            with self.subTest(bad_row=bad_row, closing=closing):
                host, ctx, _plain, _purged = removal_host()
                host.product(UNINSTALL_ARGV, stdout=uninstall_text(bad_row=bad_row, closing=closing))
                result = cotenant.uninstall(ctx)
                self.assertFalse(result.ok)
                self.assertIn("check-4" if bad_row is not None else "closing line", result.detail)

    def test_plain_kept_absent_dropins_and_volumes_are_checked(self) -> None:
        cases = (
            *(("kept", str(path)) for path in cotenant._KEPT_AFTER_PLAIN),
            *(("absent", str(path)) for path in cotenant._ABSENT_AFTER_PLAIN),
            *(("dropin", str(path)) for path in DROPINS),
            ("volumes", VOLUMES[0]),
        )
        for category, expected in cases:
            with self.subTest(category=category, path=expected):
                host, ctx, plain, _purged = removal_host()
                if category == "kept":
                    plain[expected] = False
                elif category == "absent":
                    plain[expected] = True
                elif category == "dropin":
                    plain[expected] = not host.presence[expected]
                else:
                    volume_command = f"sudo {shlex.join(cotenants.volume_ls_argv())}"
                    all_rows = "".join(f"{volume}\tgideon\n" for volume in VOLUMES)
                    host.sequence(volume_command, [(0, all_rows), (0, f"{VOLUMES[1]}\tgideon\n")])
                result = cotenant.uninstall(ctx)
                self.assertFalse(result.ok)
                self.assertIn(expected, result.detail)

    def test_purge_kept_absent_dropins_and_volumes_are_checked(self) -> None:
        cases = (
            *(("kept", str(path)) for path in cotenant._KEPT_AFTER_PURGE),
            *(("absent", str(path)) for path in cotenant._ABSENT_AFTER_PURGE),
            *(("dropin", str(item.path)) for item in host_uninstall.KEPT_DROP_INS),
            ("volumes", VOLUMES[0]),
        )
        for category, expected in cases:
            with self.subTest(category=category, path=expected):
                host, ctx, _plain, purged = removal_host()
                ctx.dropins_before = host.presence.copy()
                ctx.volumes_before = tuple(sorted(VOLUMES))
                if category == "kept":
                    purged[expected] = False
                elif category == "absent":
                    purged[expected] = True
                elif category == "dropin":
                    purged[expected] = not host.presence[expected]
                else:
                    volume_command = f"sudo {shlex.join(cotenants.volume_ls_argv())}"
                    host.answer(volume_command, stdout=f"{expected}\tgideon\n")
                result = cotenant.purge(ctx)
                self.assertFalse(result.ok)
                self.assertIn(expected, result.detail)

    def test_plain_requires_marked_volumes_before_running(self) -> None:
        host, ctx, _plain, _purged = removal_host()
        host.answer(f"sudo {shlex.join(cotenants.volume_ls_argv())}", stdout="")
        result = cotenant.uninstall(ctx)
        self.assertFalse(result.ok)
        self.assertIn("volume", result.detail)
        self.assertEqual(host.product_calls, [])


class Boundaries(unittest.TestCase):
    def test_stage_module_imports_stay_within_the_host_and_harness(self) -> None:
        source = ROOT / "tools/acceptance/cotenant.py"
        tree = ast.parse(source.read_text())
        modules = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ] + [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        for module in modules:
            with self.subTest(module=module):
                self.assertTrue(
                    module.split(".", 1)[0] in sys.stdlib_module_names
                    or module == "yaml"
                    or module.startswith(("gideon.host", "tools.acceptance", "tools.redact")),
                    module,
                )

    def test_fixture_names_cidr_and_disk_are_isolated(self) -> None:
        files = sorted(cotenant.FIXTURE_DIR.iterdir())
        self.assertEqual(
            {path.name for path in files},
            {"arrive.sh", "compose.yaml", "cotenant-rule.service",
             "cotenant-tick.service", "cotenant-tick.timer"},
        )
        example = yaml.safe_load((ROOT / "config/site.example.yaml").read_text())
        office_values = (
            example["office"]["name"], example["office"]["short_name"],
            example["hostname"], example["jurisdiction"]["districts"][0],
        )
        forbidden = (
            "gideon transcribe", "gideon-transcribe", "gideon_transcribe",
            "whisper", "transcribe", *(value.casefold() for value in office_values),
        )
        for path in files:
            with self.subTest(path=path.name):
                content = path.read_text().casefold()
                for name in forbidden:
                    self.assertNotIn(name, content)
        script = cotenant.FIXTURE_DIR / "arrive.sh"
        syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        for filename in ("compose.yaml", "cotenant-rule.service", "cotenant-tick.service", "cotenant-tick.timer"):
            self.assertIn("cotenant", (cotenant.FIXTURE_DIR / filename).read_text())
        rule = ipaddress.IPv4Network(cotenant.RULE_CIDR)
        self.assertTrue(any(rule.subnet_of(ipaddress.IPv4Network(cidr)) for cidr in (
            "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
        )))
        domain = ET.fromstring((ROOT / "tools/acceptance/domain.xml.tmpl").read_text())
        disks = domain.findall("./devices/disk[@device='disk']/target")
        self.assertGreaterEqual(len(disks), 2)
        self.assertEqual(cotenant.DATA_DISK, "/dev/" + disks[1].attrib["dev"])

    def test_default_names_fit_the_certificate_common_names(self) -> None:
        # OpenSSL refuses a CN past 64 characters: the CA's holds the run id,
        # the VM leaf's the hostname.
        for name in DEFAULT_COTENANT_VM_NAMES.values():
            with self.subTest(name=name):
                self.assertLessEqual(len(f"{name}-{'0' * 12}"), 64)
                self.assertLessEqual(len(seed.hostname_for(name)), 64)
