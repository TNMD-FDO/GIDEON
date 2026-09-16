"""Acceptance harness contracts over fake Host and Fetcher seams."""

import ast
import contextlib
import io
import json
import os
import shlex
import subprocess
import unittest
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

import yaml  # type: ignore[import-untyped]

from gideon.host import install as host_install
from gideon.host import site as host_site
from gideon.host.apply import PRINT_ONCE_SUFFIX
from gideon.host.lock import load_host_lock
from gideon.host.report import Problem, StageResult, stage_line
from gideon.host.steps.services import acceptance_image_path
from gideon.host.sysio import Command, PathLike
from tools.acceptance import domain, image, rehearsal, seed, services, vm
from tools.acceptance.cli import main
from tools.acceptance.context import (
    HARNESS_ROOT,
    HarnessContext,
    RunSpec,
    ServiceMaterial,
    SinkFactory,
    SinkLike,
)
from tools.acceptance.run import STAGE_IDENTIFIERS, STAGES
from tools.pinwatch.fetch import FetchError, Response

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "config/site.example.yaml"
TEMPLATE = ROOT / "tools/acceptance/domain.xml.tmpl"
LOCK = ROOT / "host.lock"
LOCK_RESULT = load_host_lock(LOCK)
assert LOCK_RESULT.lock is not None
HOST_LOCK = LOCK_RESULT.lock
IMAGE = acceptance_image_path(HOST_LOCK)
IMAGE_CHECKSUM = HOST_LOCK.acceptance_vm_image.sha256
CHECKOUT = Path("/repo")
OUT = Path("/tmp/acceptance-out")
RUN_DIR = HARNESS_ROOT / "test-vm"
TOOL_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("virsh", "--version"),
    ("qemu-img", "--version"),
    ("guestfish", "--version"),
    ("virt-customize", "--version"),
    ("cloud-localds", "--version"),
    ("openssl", "version"),
    ("ssh", "-V"),
    ("git", "--version"),
)
NETWORK = (
    "Name: default\n"
    "Active: yes\n"
    "Autostart: yes\n"
)
RESOLVED = "a" * 40
# Every git call on the box's checkout names it safe: the harness runs as root
# over a CSA-owned tree and never runs git as that owner.
REV_PARSE = ("git", "-c", f"safe.directory={CHECKOUT}", "rev-parse", "--verify", "HEAD^{commit}")


def completed(
    argv: Sequence[str],
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)


class FakeFetcher:
    """Return one HTTP answer, or refuse the registry transport."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        method: str = "GET",
    ) -> Response:
        del headers, method
        self.calls.append(url)
        if self.fail:
            raise FetchError(url, "connection refused")
        return Response(503, {}, b"registry is alive")


class FakeMessage:
    def __init__(self, *, user: str, tls: bool) -> None:
        self.user = user
        self.tls = tls


class FakeSink:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        requested = int(cast(int, kwargs["port"]))
        # Port 0 asks the kernel for a free port; the fake allocates one.
        self.port = requested or 3000
        self.messages: list[FakeMessage] = []
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def fake_sink_factory(**kwargs: object) -> FakeSink:
    return FakeSink(**kwargs)


class FakeHost:
    """Record argv, file reads/writes, and ownership through the Host seam."""

    def __init__(
        self,
        *,
        euid: int = 0,
        commands: Mapping[
            tuple[str, ...],
            subprocess.CompletedProcess[str] | list[subprocess.CompletedProcess[str]],
        ]
        | None = None,
        files: Mapping[str, str] | None = None,
        install_stdout: str | None = None,
    ) -> None:
        self.euid = euid
        self.commands = dict(commands or {})
        self.files = dict(files or {})
        self.install_stdout = install_stdout
        self.calls: list[
            tuple[tuple[str, ...], Mapping[str, str] | None, str | None, PathLike | None]
        ] = []
        self.writes: list[str] = []
        self.directories: set[str] = set()
        self.chowns: list[tuple[str, int, int]] = []
        self.domain_xml: str | None = None
        # After a reboot request the next SSH probe finds the VM down.
        self.rebooting = False

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
        del check, timeout, passthrough
        command = tuple(argv)
        self.calls.append((command, env, input, cwd))
        if command[:2] == ("ssh-keygen", "-t"):
            key_path = command[command.index("-f") + 1]
            self.files[f"{key_path}.pub"] = "ssh-ed25519 AAAA acceptance-key\n"
        if command[:2] == ("virsh", "define"):
            self.domain_xml = self.files.get(command[2], "")
        outcome = self.commands.get(command)
        if isinstance(outcome, list):
            return outcome.pop(0) if len(outcome) > 1 else outcome[0]
        if outcome is not None:
            return outcome
        if command[0] == "openssl" and len(command) > 1 and command[1] in {
            "req",
            "x509",
        }:
            for option in ("-keyout", "-out"):
                if option in command:
                    path = command[command.index(option) + 1]
                    self.files[path] = "CERTIFICATE\n" if option == "-out" else "PRIVATE KEY\n"
            return completed(command)
        if command[0] == "ufw":
            return completed(command)
        if command[:2] == ("virsh", "dumpxml") and self.domain_xml is not None:
            return completed(command, stdout=self.domain_xml)
        if command[0] == "ssh":
            # The script travels as one shell-quoted word.
            script = shlex.split(command[-1])[0]
            if script == "true" and self.rebooting:
                self.rebooting = False
                return completed(command, returncode=255)
            if "cloud-init status --wait" in script or script == "true":
                return completed(command)
            if script.startswith("sudo cat /etc/gideon/secrets/backup_ssh_key.pub"):
                return completed(command, stdout="ssh-ed25519 AAAA backup-key\n")
            if "python3 -m gideon --version" in script:
                return completed(command, stdout="gideon 1.2.3\n")
            if script.startswith("sudo sh -c "):
                return completed(command)
            if "sudo ./preflight.sh" in script:
                return completed(command, stdout="preflight: ok\n")
            if "sudo ./install.sh" in script:
                output = self.install_stdout
                if output is None:
                    output = (
                        "install: ok\n"
                        + stage_line(
                            StageResult(
                                "engine-verify",
                                True,
                                host_install.ENGINE_VERIFY_SKIPPED_DETAIL,
                                "",
                            )
                        )
                        + "\nhttps://test-vm.acceptance.invalid\n"
                    )
                return completed(command, stdout=output)
            if "sudo python3 -m gideon alerts test" in script:
                return completed(command, stdout="alerts: ok\n")
            if "git clone --branch" in script or "sudo install -d" in script:
                return completed(command)
            if "sudo python3 -m gideon host provision" in script:
                return completed(command, stdout="provision: ok — converged\n")
            if script == vm.REBOOT_SCRIPT:
                self.rebooting = True
                return completed(command)
        if command[0] == "rm" or command[:2] in {
            ("virsh", "destroy"),
            ("virsh", "undefine"),
        }:
            return completed(command)
        return completed(command, 127, stderr="not configured")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        try:
            return self.files[key]
        except KeyError:
            raise FileNotFoundError(key) from None

    def write_text(
        self,
        path: PathLike,
        text: str,
        *,
        encoding: str = "utf-8",
        mode: int = 0o644,
    ) -> None:
        del encoding, mode
        key = os.fspath(path)
        self.files[key] = text
        self.writes.append(key)

    def exists(self, path: PathLike) -> bool:
        key = os.fspath(path)
        return key in self.files or key in self.directories

    def listdir(self, path: PathLike) -> list[str]:
        if os.fspath(path) in self.files:
            raise NotADirectoryError(os.fspath(path))
        parent = os.fspath(path).rstrip("/") + "/"
        return sorted(
            {
                candidate[len(parent) :].split("/", 1)[0]
                for candidate in (*self.files, *self.directories)
                if candidate.startswith(parent)
            }
        )

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del missing_ok
        self.files.pop(os.fspath(path), None)

    def stat(self, path: PathLike) -> os.stat_result:
        del path
        raise FileNotFoundError

    def chmod(self, path: PathLike, mode: int) -> None:
        del path, mode

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        self.chowns.append((os.fspath(path), uid, gid))

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del mode, parents, exist_ok
        self.directories.add(os.fspath(path))

    def geteuid(self) -> int:
        return self.euid


def base_files() -> dict[str, str]:
    return {
        str(CHECKOUT / "host.lock"): LOCK.read_text(),
        str(CHECKOUT / "tools/acceptance/domain.xml.tmpl"): TEMPLATE.read_text(),
        str(CHECKOUT / "tools/acceptance/site.yaml.tmpl"): (
            ROOT / "tools/acceptance/site.yaml.tmpl"
        ).read_text(),
        str(SITE): SITE.read_text(),
        str(IMAGE): "pinned image",
        "/etc/gideon/ca.pem": "OFFICE ROOT\n",
        "/etc/gideon/secrets/ldap_bind_password": "bind-password\n",
    }


def base_commands(*, checksum: str = IMAGE_CHECKSUM) -> dict[tuple[str, ...], subprocess.CompletedProcess[str]]:
    commands = {command: completed(command) for command in TOOL_COMMANDS}
    commands[("sha256sum", str(IMAGE))] = completed(
        ("sha256sum", str(IMAGE)), stdout=f"{checksum}  {IMAGE}\n"
    )
    commands[("virsh", "net-info", "default")] = completed(
        ("virsh", "net-info", "default"), stdout=NETWORK
    )
    commands[("virsh", "dominfo", "gideon-acceptance")] = completed(
        ("virsh", "dominfo", "gideon-acceptance"), returncode=1
    )
    commands[REV_PARSE] = completed(REV_PARSE, stdout=RESOLVED + "\n")
    commands[("virsh", "domstate", "test-vm")] = completed(
        ("virsh", "domstate", "test-vm"), stdout="running\n"
    )
    commands[("rm", "-rf", str(HARNESS_ROOT / "gideon-acceptance"))] = completed(
        ("rm", "-rf")
    )
    return commands


def phase_b_commands(name: str = "test-vm") -> dict[tuple[str, ...], subprocess.CompletedProcess[str]]:
    run_dir = HARNESS_ROOT / name
    commands = base_commands()
    for size, filename in (("40G", "os.qcow2"), ("2T", "data.qcow2")):
        command = ("qemu-img", "create", "-f", "qcow2", str(run_dir / filename), size)
        commands[command] = completed(command)
    commands[("guestfish",)] = completed(("guestfish",))
    mirror_clone = (
        "git",
        "-c",
        f"safe.directory={CHECKOUT}",
        "clone",
        "--mirror",
        str(CHECKOUT),
        str(run_dir / "GIDEON.git"),
    )
    commands[mirror_clone] = completed(mirror_clone)
    tag_probe = (
        "git",
        "-c",
        f"safe.directory={CHECKOUT}",
        "rev-parse",
        "--verify",
        "refs/tags/HEAD",
    )
    commands[tag_probe] = completed(tag_probe, returncode=128)
    tag = ("git", "-C", str(run_dir / "GIDEON.git"), "tag", f"acceptance-{RESOLVED[:12]}", RESOLVED)
    commands[tag] = completed(tag)
    customize = (
        "virt-customize",
        "-a",
        str(run_dir / "os.qcow2"),
        "--no-network",
        "--copy-in",
        f"{run_dir / 'GIDEON.git'}:/srv",
        "--run-command",
        "grub-install --target=i386-pc /dev/sda",
        "--run-command",
        "update-grub",
        "--run-command",
        "dracut --force --no-hostonly --regenerate-all",
    )
    commands[customize] = completed(customize)
    commands[("ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(run_dir / "id_ed25519"), "-C", f"{name}-{RESOLVED[:12]}")] = completed(("ssh-keygen",))
    commands[("cloud-localds", str(run_dir / "seed.iso"), str(run_dir / "user-data"), str(run_dir / "meta-data"))] = completed(("cloud-localds",))
    commands[("virsh", "define", str(run_dir / "domain.xml"))] = completed(("virsh", "define"))
    commands[("virsh", "start", name)] = completed(("virsh", "start"))
    commands[("virsh", "domifaddr", name, "--source", "lease")] = completed(
        ("virsh", "domifaddr"), stdout="vnet0 ipv4 192.168.122.10/24\n"
    )
    commands[("virsh", "dominfo", name)] = completed(("virsh", "dominfo", name), returncode=1)
    commands[("virsh", "domstate", name)] = completed(("virsh", "domstate", name), stdout="running\n")
    commands[("virsh", "dumpxml", name)] = completed(("virsh", "dumpxml", name), stdout="")
    commands[("git", "-c", f"safe.directory={CHECKOUT}", "rev-parse", "--verify", "refs/tags/HEAD")] = completed(("git", "rev-parse"), returncode=128)
    commands[("rm", "-rf", str(run_dir))] = completed(("rm", "-rf"))
    return commands


def invoke(
    host: FakeHost,
    *,
    fetcher: FakeFetcher | None = None,
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
    sink_factory: SinkFactory | None = None,
) -> tuple[int, str, str, FakeFetcher]:
    output, errors = io.StringIO(), io.StringIO()
    transport = fetcher or FakeFetcher()
    with (
        contextlib.redirect_stdout(output),
        contextlib.redirect_stderr(errors),
        patch.dict(os.environ, dict(environ or {}), clear=True),
    ):
        code = main(
            argv or ["HEAD"],
            host=host,
            fetcher=transport,
            root=CHECKOUT,
            site_path=SITE,
            sleep=lambda _seconds: None,
            sink_factory=cast(SinkFactory, sink_factory or fake_sink_factory),
        )
    return code, output.getvalue(), errors.getvalue(), transport


def stage_index(identifier: str) -> int:
    """The 1-based position the runner stamps on the context: the transcript prefix."""

    return STAGE_IDENTIFIERS.index(identifier) + 1


def harness_context(
    host: FakeHost,
    *,
    name: str = "test-vm",
    ref: str = "HEAD",
    clone_ref: str = "acceptance-aaaaaaaaaaaa",
    address: str | None = "192.168.122.10",
    sleep: list[float] | None = None,
    stage: str = "provision",
) -> HarnessContext:
    sleeps = sleep
    spec = RunSpec(
        ref=ref,
        resolved_commit=RESOLVED,
        vm_name=name,
        run_dir=HARNESS_ROOT / name,
        out=OUT,
        keep=False,
        until=None,
        clone_ref=clone_ref,
    )
    return HarnessContext(
        host,
        FakeFetcher(),
        CHECKOUT,
        SITE,
        CHECKOUT / "tools/acceptance/domain.xml.tmpl",
        spec,
        lock=HOST_LOCK,
        run_id=f"{name}-{RESOLVED[:12]}",
        address=address,
        stage_index=stage_index(stage),
        sleep=(lambda seconds: sleeps.append(seconds)) if sleeps is not None else (lambda _seconds: None),
    )


class HarnessCli(unittest.TestCase):
    def test_dry_run_prints_the_complete_plan_without_host_io(self) -> None:
        host = FakeHost()
        code, out, err, _ = invoke(host, argv=["HEAD", "--dry-run"])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("preconditions, image, seed, services, boot", out)
        self.assertIn("provision-2", out)
        self.assertIn(str(HARNESS_ROOT / "gideon-acceptance"), out)
        self.assertIn("8 vCPUs, 16 GiB RAM, machine pc", out)
        self.assertEqual(host.calls, [])
        self.assertEqual(host.writes, [])
        self.assertEqual(host.chowns, [])

    def test_preconditions_refuse_each_missing_requirement_with_its_fix(self) -> None:
        cases: list[tuple[str, FakeHost, FakeFetcher, str]] = []
        cases.append(("root", FakeHost(euid=1000, commands=base_commands(), files=base_files()), FakeFetcher(), "sudo python3 -m tools.acceptance"))
        for command in TOOL_COMMANDS:
            commands = base_commands()
            del commands[command]
            cases.append((command[0], FakeHost(commands=commands, files=base_files()), FakeFetcher(), "host provision --only kvm"))
        bad_image = FakeHost(commands=base_commands(checksum="f" * 64), files=base_files())
        cases.append(("image", bad_image, FakeFetcher(), "host provision --only kvm"))
        cases.append(("registry", FakeHost(commands=base_commands(), files=base_files()), FakeFetcher(fail=True), "registry"))
        bad_site_files = base_files()
        bad_site_files[str(SITE)] = "not: [valid"
        cases.append(("site", FakeHost(commands=base_commands(), files=bad_site_files), FakeFetcher(), "site.yaml"))
        bad_ref_commands = base_commands()
        bad_ref_commands[REV_PARSE] = completed(REV_PARSE, returncode=128)
        cases.append(("ref", FakeHost(commands=bad_ref_commands, files=base_files()), FakeFetcher(), "checkout ref"))
        inactive_commands = base_commands()
        inactive_commands[("virsh", "net-info", "default")] = completed(("virsh", "net-info", "default"), stdout="Active: no\n")
        cases.append(("network", FakeHost(commands=inactive_commands, files=base_files()), FakeFetcher(), "default network"))

        for name, host, fetcher, expected in cases:
            with self.subTest(name=name):
                code, out, _err, _ = invoke(host, fetcher=fetcher)
                self.assertEqual(code, 1)
                self.assertIn("preconditions: refuse", out)
                self.assertIn(expected, out)

    def test_a_tool_that_fails_its_version_probe_is_still_present(self) -> None:
        """Only the seam's 127 means absent (cloud-localds has no --version)."""

        commands = base_commands()
        commands[("cloud-localds", "--version")] = completed(("cloud-localds", "--version"), returncode=1, stderr="usage")
        host = FakeHost(commands=commands, files=base_files())
        code, out, _err, _ = invoke(host, argv=["HEAD", "--until", "preconditions"])
        self.assertEqual(code, 0, out)

    def test_image_failure_stops_and_teardown_runs(self) -> None:
        host = FakeHost(commands=base_commands(), files=base_files())

        code, out, _err, _ = invoke(host)

        self.assertEqual(code, 1)
        names = [line.split(":", 1)[0] for line in out.splitlines()]
        self.assertEqual(names, ["preconditions", "image", "teardown"])
        self.assertIn("image: refuse — created os.qcow2 (40G): not configured", out)
        self.assertIn("teardown: ok — removed acceptance run directory", out)

    def test_keep_skips_teardown_and_until_stops_after_the_named_stage(self) -> None:
        kept = FakeHost(commands=base_commands(), files=base_files())
        code, out, _err, _ = invoke(kept, argv=["HEAD", "--keep"])
        self.assertEqual(code, 1)
        self.assertNotIn("teardown:", out)
        self.assertFalse(any(call[0][0] == "rm" for call in kept.calls))

        until = FakeHost(commands=base_commands(), files=base_files())
        code, out, _err, _ = invoke(until, argv=["HEAD", "--until", "preconditions"])
        self.assertEqual(code, 0, out)
        self.assertIn("preconditions: ok", out)
        self.assertNotIn("image:", out)
        with self.assertRaises(SystemExit):
            main(["HEAD", "--until", "unknown"], root=CHECKOUT)

    def test_out_ownership_is_restored_only_when_sudo_ids_exist(self) -> None:
        owned = FakeHost(commands=base_commands(), files=base_files())
        code, _out, _err, _ = invoke(owned, environ={"SUDO_UID": "1001", "SUDO_GID": "1002"})
        self.assertEqual(code, 1)
        out_dir = str(Path.cwd() / "acceptance-out/HEAD")
        self.assertIn((out_dir, 1001, 1002), owned.chowns)
        # Only the harness's own tree, file by file: never chown -R, so an
        # --out naming someone's directory can never be handed over wholesale.
        self.assertFalse(any(call[0][0] == "chown" for call in owned.calls))

        unowned = FakeHost(commands=base_commands(), files=base_files())
        code, _out, _err, _ = invoke(unowned)
        self.assertEqual(code, 1)
        self.assertEqual(unowned.chowns, [])
        self.assertFalse(any(call[0][0] == "chown" for call in unowned.calls))

    def test_out_is_absolute_whatever_the_caller_typed(self) -> None:
        # libvirt opens the console log by path from its own working directory:
        # the workflow's relative --out refused the v0.1.0 tag run at boot.
        host = FakeHost(commands=base_commands(), files=base_files())
        code, out, _err, _ = invoke(host, argv=["HEAD", "--dry-run", "--out", "acceptance-out"])
        self.assertEqual(code, 0)
        self.assertIn(f"output directory: {Path.cwd() / 'acceptance-out'}", out)

    def test_bytecode_caches_root_left_under_the_checkout_are_handed_back(self) -> None:
        # Root imports the harness from a CSA's checkout or the runner's
        # workspace; a root-owned cache broke the runner's next checkout after
        # the v0.1.0 tag run. The caches go back with the transcripts, file by
        # file, never recursively.
        cache = CHECKOUT / "tools/__pycache__"
        cached = cache / "__init__.cpython-314.pyc"
        find = (
            "find",
            str(CHECKOUT / "tools"),
            str(CHECKOUT / "gideon"),
            "-type",
            "d",
            "-name",
            "__pycache__",
        )
        host = FakeHost(
            commands={**base_commands(), find: completed(find, stdout=f"{cache}\n")},
            files={**base_files(), str(cached): "bytecode"},
        )
        host.directories.add(str(cache))
        code, _out, _err, _ = invoke(host, environ={"SUDO_UID": "1001", "SUDO_GID": "1002"})
        self.assertEqual(code, 1)
        self.assertIn((str(cache), 1001, 1002), host.chowns)
        self.assertIn((str(cached), 1001, 1002), host.chowns)
        self.assertFalse(any(call[0][0] == "chown" for call in host.calls))

        direct = FakeHost(
            commands={**base_commands(), find: completed(find, stdout=f"{cache}\n")},
            files={**base_files(), str(cached): "bytecode"},
        )
        invoke(direct)
        self.assertFalse(any(call[0][0] == "find" for call in direct.calls))
        self.assertEqual(direct.chowns, [])

    def test_entry_point_writes_no_bytecode_before_importing_the_harness(self) -> None:
        tree = ast.parse((ROOT / "tools/acceptance/__main__.py").read_text())
        events: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Attribute) and target.attr == "dont_write_bytecode"
                for target in node.targets
            ):
                events.append((node.lineno, "dont_write_bytecode"))
            if isinstance(node, ast.ImportFrom) and node.module == "tools.acceptance.cli":
                events.append((node.lineno, "import"))
        self.assertEqual([kind for _line, kind in sorted(events)], ["dont_write_bytecode", "import"])

    def test_out_that_exists_and_is_not_empty_is_refused_before_anything_runs(self) -> None:
        host = FakeHost(commands=base_commands(), files={**base_files(), "/srv/evidence/notes.txt": "mine\n"})
        host.directories.add("/srv/evidence")
        code, out, err, _ = invoke(host, argv=["HEAD", "--out", "/srv/evidence"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("--out /srv/evidence exists and is not empty", err)
        self.assertFalse(any(call[0][0] == "virsh" for call in host.calls))

    def test_out_naming_a_regular_file_is_refused_not_a_traceback(self) -> None:
        host = FakeHost(commands=base_commands(), files={**base_files(), "/srv/notes.txt": "mine\n"})
        code, out, err, _ = invoke(host, argv=["HEAD", "--out", "/srv/notes.txt"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("--out /srv/notes.txt is not a usable directory", err)
        self.assertIn("Fix:", err)

    def test_default_out_carries_the_vm_name_for_a_non_default_run(self) -> None:
        from tools.acceptance.cli import default_out

        self.assertEqual(default_out("v1.2.0", "gideon-acceptance").name, "v1.2.0")
        self.assertEqual(default_out("v1.2.0", "second").name, "v1.2.0-second")

    def test_services_prepares_the_sink_and_site_before_boot(self) -> None:
        host = FakeHost(commands=phase_b_commands(), files=base_files())
        code, out, _err, _ = invoke(
            host, argv=["HEAD", "--name", "test-vm", "--until", "services"]
        )

        self.assertEqual(code, 0, out)
        self.assertIn("services: ok — prepared the acceptance CA, site, secrets, and SMTP sink", out)
        self.assertNotIn("boot:", out)

    def test_phase_two_runs_through_reboot_and_keep_reports_the_vm_address(self) -> None:
        host = FakeHost(commands=phase_b_commands(), files=base_files())
        code, out, _err, _ = invoke(
            host,
            argv=["HEAD", "--name", "test-vm", "--until", "reboot", "--keep"],
        )

        self.assertEqual(code, 0, out)
        self.assertIn("reboot: ok", out)
        self.assertIn("VM test-vm kept at 192.168.122.10", out)
        commands = [call[0] for call in host.calls]
        self.assertNotIn(("virsh", "destroy", "test-vm"), commands)
        self.assertNotIn(("virsh", "undefine", "test-vm", "--remove-all-storage"), commands)


class ServicesContracts(unittest.TestCase):
    def _context(self, host: FakeHost) -> HarnessContext:
        ctx = harness_context(host)
        loaded = host_site.load_site(SITE, host=host)
        assert loaded.config is not None
        ctx.site = loaded.config
        ctx.sink_factory = cast(SinkFactory, fake_sink_factory)
        return ctx

    def _prepared(self) -> tuple[FakeHost, HarnessContext]:
        host = FakeHost(files=base_files())
        ctx = self._context(host)
        result = services.prepare(ctx)
        self.assertTrue(result.ok, result.detail)
        self.assertIsNotNone(ctx.services)
        return host, ctx

    def test_services_loads_the_rendered_vm_site_and_binds_the_sink_redactor(self) -> None:
        """Ticket 55's harness ruling keeps the VM site and sink redactor aligned."""

        _host, ctx = self._prepared()
        self.assertIsNotNone(ctx.vm_site)
        assert ctx.vm_site is not None
        assert ctx.sink is not None
        redactor = cast(Callable[[str], str], cast(FakeSink, ctx.sink).kwargs["redact"])
        self.assertEqual(
            redactor(f"https://{ctx.vm_site.hostname}"), "https://<hostname>"
        )
        self.assertEqual(
            redactor(f"base {ctx.vm_site.auth.ldap.search_base}"),
            "base <auth.ldap.search_base>",
        )

    def test_stored_message_sidecar_and_body_use_the_vm_site_redactor(self) -> None:
        """Ticket 55 sends both stored message records through the sink callable."""

        _host, ctx = self._prepared()
        assert ctx.vm_site is not None
        assert ctx.sink is not None
        redactor = cast(Callable[[str], str], cast(FakeSink, ctx.sink).kwargs["redact"])
        sidecar = json.dumps({"sender": ctx.vm_site.alerts.smtp.from_})
        body = f"Subject: {ctx.vm_site.hostname}\n\n{ctx.vm_site.hostname}\n"

        self.assertEqual(redactor(sidecar), '{"sender": "<alerts.smtp.from>"}')
        self.assertEqual(redactor(body), "Subject: <hostname>\n\n<hostname>\n")

    def test_services_refuses_when_the_rendered_site_does_not_load(self) -> None:
        """Ticket 55 reports a rendered-site loader refusal as a services row."""

        files = base_files()
        files[str(CHECKOUT / "tools/acceptance/site.yaml.tmpl")] = "not: [valid\n"
        host = FakeHost(files=files)
        ctx = self._context(host)

        result = services.prepare(ctx)

        self.assertFalse(result.ok)
        self.assertEqual(result.name, "services")
        self.assertIn("YAML parse error", result.detail)
        self.assertEqual(result.fix, services.SERVICE_FIX)
        self.assertIsNone(ctx.vm_site)

    def test_ca_commands_have_ordered_subjects_and_sans_and_never_touch_the_pinned_image(self) -> None:
        host, ctx = self._prepared()
        openssl = [call[0] for call in host.calls if call[0][0] == "openssl"]

        self.assertEqual(len(openssl), 5)
        self.assertEqual(openssl[0][0:7], ("openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1"))
        self.assertEqual(openssl[0][openssl[0].index("-subj") + 1], f"/CN=GIDEON acceptance CA {ctx.run_id}")
        self.assertIn("basicConstraints=critical,CA:TRUE", openssl[0])
        # Strict X.509 (Python 3.13+'s default context): the CA carries key usage
        # and a subject key id, each leaf its usages and the server-auth purpose.
        self.assertIn("keyUsage=critical,keyCertSign,cRLSign", openssl[0])
        self.assertIn("subjectKeyIdentifier=hash", openssl[0])
        for leaf in (openssl[1], openssl[3]):
            self.assertIn("basicConstraints=CA:FALSE", leaf)
            self.assertIn("keyUsage=critical,digitalSignature,keyEncipherment", leaf)
            self.assertIn("extendedKeyUsage=serverAuth", leaf)
        self.assertEqual(openssl[1][openssl[1].index("-subj") + 1], "/CN=test-vm.acceptance.invalid")
        self.assertIn("subjectAltName=DNS:test-vm.acceptance.invalid", openssl[1])
        self.assertIn("-CAcreateserial", openssl[2])
        self.assertIn("subjectAltName=IP:192.168.122.1", openssl[3])
        self.assertIn("-CAcreateserial", openssl[4])
        self.assertFalse(any(str(IMAGE) in word for command in openssl for word in command))

    def test_bundle_and_rendered_site_load_with_box_values(self) -> None:
        host, ctx = self._prepared()
        assert ctx.services is not None
        self.assertEqual(ctx.services.ca_bundle_text, "OFFICE ROOT\nCERTIFICATE\n")

        generated = OUT / "site.yaml"
        host.files[str(generated)] = ctx.services.site_text
        loaded = host_site.load_site(generated, host=host)
        self.assertFalse(loaded.errors, loaded.errors)
        assert loaded.config is not None
        box = host_site.load_site(SITE).config
        assert box is not None
        self.assertEqual(loaded.config.hostname, "test-vm.acceptance.invalid")
        self.assertEqual(loaded.config.lan_cidrs, ["192.168.122.0/24"])
        self.assertEqual(loaded.config.registry, "192.168.122.1:5000")
        self.assertEqual(loaded.config.alerts.smtp.host, "192.168.122.1")
        # The named run's site names the port the sink actually bound (the fake allocates 3000).
        self.assertEqual(loaded.config.alerts.smtp.port, 3000)
        self.assertEqual(loaded.config.alerts.smtp.user, "gideon-acceptance")
        self.assertEqual(loaded.config.backup.target.host, "127.0.0.1")
        self.assertEqual(loaded.config.backup.target.path, "/srv/gideon-backup")
        self.assertEqual(loaded.config.auth.ldap.host, box.auth.ldap.host)
        self.assertEqual(loaded.config.auth.ldap.search_base, box.auth.ldap.search_base)
        self.assertEqual(loaded.config.auth.ldap.users_group_dn, box.auth.ldap.users_group_dn)
        self.assertEqual(loaded.config.auth.ldap.admins_group_dn, box.auth.ldap.admins_group_dn)
        self.assertEqual(loaded.config.auth.ldap.mirror_group_dns, box.auth.ldap.mirror_group_dns)

    def test_site_secrets_cross_ssh_on_stdin_and_root_copy_has_owner_script(self) -> None:
        host, ctx = self._prepared()
        result = services.install_site(ctx)
        self.assertTrue(result.ok, result.detail)
        assert ctx.services is not None
        material_values = {
            ctx.services.site_text,
            ctx.services.ca_bundle_text,
            ctx.services.vm_certificate_text,
            ctx.services.ldap_bind_password,
            ctx.services.smtp_password,
            ctx.services.vm_key_text,
        }
        secret_values = {
            ctx.services.ldap_bind_password,
            ctx.services.smtp_password,
            ctx.services.vm_key_text,
        }
        copied_inputs = {call[2] for call in host.calls if call[0][0] == "ssh" and call[2] is not None}
        self.assertTrue(secret_values <= copied_inputs)
        for command, _env, input_text, _cwd in host.calls:
            self.assertFalse(any(value in command for value in material_values))
            if input_text is not None:
                self.assertNotIn(input_text, command)

        root_copy = vm.copy_in(
            ctx,
            "/etc/gideon/secrets/example",
            "private-value",
            0o440,
            as_root=True,
            owner="root:gideon",
        )
        self.assertTrue(root_copy.ok, root_copy.detail)
        script = shlex.split(host.calls[-1][0][-1])[0]
        self.assertIn("sudo sh -c", script)
        self.assertIn('umask 077 && cat > "$1" && chmod 0440 "$1" && chown root:gideon "$1"', script)
        self.assertNotIn("private-value", host.calls[-1][0])
        self.assertEqual(host.calls[-1][2], "private-value")

    def test_ufw_rule_is_removed_and_a_leftover_is_swept(self) -> None:
        host = FakeHost(commands=phase_b_commands(), files=base_files())
        sinks: list[FakeSink] = []

        def factory(**kwargs: object) -> FakeSink:
            sink = FakeSink(**kwargs)
            sinks.append(sink)
            return sink

        code, out, _err, _ = invoke(
            host,
            argv=["HEAD", "--name", "test-vm", "--until", "services"],
            sink_factory=cast(SinkFactory, factory),
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(len(sinks), 1)
        self.assertTrue(sinks[0].started)
        self.assertTrue(sinks[0].stopped)
        calls = [call[0] for call in host.calls]
        rules = [call for call in calls if call[:2] == ("ufw", "allow")]
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        # A named run (--name test-vm) has its own listener and its own
        # comment, so two harnesses at once never share a rule or a port.
        self.assertEqual(rule[:9], ("ufw", "allow", "proto", "tcp", "from", "192.168.122.0/24", "to", "any", "port"))
        self.assertEqual(rule[9], "3000")
        self.assertEqual(rule[10:], ("comment", "gideon-acceptance-test-vm"))
        self.assertEqual(sinks[0].port, int(rule[9]))
        self.assertEqual(sinks[0].kwargs["port"], 0)
        self.assertIn(("ufw", "delete", *rule[1:]), calls)

        # The sweep deletes only this run's leftover; another run's rule stays.
        leftover = FakeHost(commands=base_commands(), files=base_files())
        listed = (
            f"ufw allow proto tcp from 192.168.122.0/24 to any port {rule[9]} comment 'gideon-acceptance-test-vm'\n"
            "ufw allow proto tcp from 192.168.122.0/24 to any port 2525 comment 'gideon-acceptance'\n"
        )
        leftover.commands[("ufw", "show", "added")] = completed(("ufw", "show", "added"), stdout=listed)
        leftover.commands[("virsh", "dominfo", "test-vm")] = completed(("virsh", "dominfo", "test-vm"), returncode=1)
        code, out, _err, _ = invoke(leftover, argv=["HEAD", "--name", "test-vm", "--until", "preconditions"])
        self.assertEqual(code, 0, out)
        deletes = [call for call in (c[0] for c in leftover.calls) if call[:2] == ("ufw", "delete")]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0][-1], "gideon-acceptance-test-vm")

    def test_the_default_run_keeps_the_documented_port_and_a_named_run_binds_a_free_one(self) -> None:
        from tools.acceptance import services

        self.assertEqual(services.sink_port("gideon-acceptance"), 2525)
        self.assertEqual(services.ufw_comment("gideon-acceptance"), "gideon-acceptance")
        # No name-derived number is collision-free: a named run asks for port 0
        # and the site file and rule are written from the port actually bound.
        self.assertEqual(services.sink_port("second"), 0)
        self.assertEqual(services.ufw_comment("second"), "gideon-acceptance-second")

    def test_authorize_reads_and_installs_the_backup_key(self) -> None:
        host, ctx = self._prepared()
        result = services.authorize(ctx)
        self.assertTrue(result.ok, result.detail)
        scripts = [shlex.split(call[0][-1])[0] for call in host.calls if call[0][0] == "ssh"]
        self.assertEqual(scripts[0], "sudo cat /etc/gideon/secrets/backup_ssh_key.pub")
        self.assertIn(
            "sudo install -d -m 0700 -o gideon-backup -g gideon-backup /srv/gideon-backup/.ssh",
            scripts,
        )
        self.assertEqual(host.calls[-1][2], "ssh-ed25519 AAAA backup-key\n")

    def test_preflight_requires_an_authenticated_sink_message(self) -> None:
        _host, ctx = self._prepared()
        assert ctx.sink is not None
        sink = cast(FakeSink, ctx.sink)
        stage = next(stage for stage in STAGES if stage.identifier == "preflight")
        result = stage.callable(ctx)
        self.assertFalse(result.ok)
        sink.messages.append(FakeMessage(user="gideon-acceptance", tls=True))
        result = stage.callable(ctx)
        self.assertTrue(result.ok, result.detail)
        self.assertIn("1 authenticated TLS", result.detail)

    def test_install_captures_the_last_nonempty_url(self) -> None:
        _host, ctx = self._prepared()
        stage = next(stage for stage in STAGES if stage.identifier == "install")
        result = stage.callable(ctx)
        self.assertTrue(result.ok, result.detail)
        # The transcript is redacted against the VM's site before the URL is read (ticket 55).
        self.assertEqual(ctx.install_url, "https://<hostname>")
        self.assertIn(ctx.install_url or "", result.detail)
        self.assertIn(host_install.ENGINE_VERIFY_SKIPPED_DETAIL, result.detail)

    def test_install_requires_the_exact_engine_verify_skip_row(self) -> None:
        variants = (
            "install: ok\nhttps://test-vm.acceptance.invalid\n",
            "install: ok\nengine-verify: refuse — bad Fix: fix\nhttps://test-vm.acceptance.invalid\n",
            "install: ok\nengine-verify: ok — inert — ships with slice 1 (§6.7)\nhttps://test-vm.acceptance.invalid\n",
            "install: ok\nengine-verify: ok — another detail\nhttps://test-vm.acceptance.invalid\n",
        )
        for transcript in variants:
            with self.subTest(transcript=transcript):
                host, ctx = self._prepared()
                host.install_stdout = transcript
                stage = next(stage for stage in STAGES if stage.identifier == "install")
                result = stage.callable(ctx)
                self.assertFalse(result.ok)
                self.assertIn("transcript", result.detail)
                self.assertIn(str(ctx.spec.out), result.detail)
                self.assertEqual(
                    result.fix,
                    f"Inspect {ctx.spec.out / f'{ctx.stage_index:02d}-install.txt'}, then retry acceptance.",
                )

    def test_alerts_waits_for_a_new_authenticated_message(self) -> None:
        host, ctx = self._prepared()
        assert ctx.sink is not None
        sink = cast(FakeSink, ctx.sink)
        sink.messages.append(FakeMessage(user="gideon-acceptance", tls=True))

        class AlertHost(FakeHost):
            def run(self, argv: Command, **kwargs: object) -> subprocess.CompletedProcess[str]:
                result = super().run(argv, **kwargs)  # type: ignore[arg-type]
                if argv and argv[0] == "ssh" and "alerts test" in shlex.split(argv[-1])[0]:
                    sink.messages.append(FakeMessage(user="gideon-acceptance", tls=True))
                return result

        alert_host = AlertHost(commands=host.commands, files=host.files)
        ctx.host = alert_host
        stage = next(stage for stage in STAGES if stage.identifier == "alerts")
        result = stage.callable(ctx)
        self.assertTrue(result.ok, result.detail)
        self.assertIn("2 authenticated TLS", result.detail)

    def test_probe_compares_served_and_run_leaf_fingerprints(self) -> None:
        host, ctx = self._prepared()
        ctx.address = "192.168.122.10"
        served_command = (
            "openssl",
            "s_client",
            "-connect",
            "192.168.122.10:443",
            "-servername",
            "test-vm.acceptance.invalid",
            "-verify_hostname",
            "test-vm.acceptance.invalid",
            "-CAfile",
            str(RUN_DIR / "ca/bundle.pem"),
            "-verify_return_error",
        )
        served_x509 = ("openssl", "x509", "-noout", "-fingerprint", "-sha256")
        leaf_x509 = ("openssl", "x509", "-in", str(RUN_DIR / "ca/vm.pem"), "-noout", "-fingerprint", "-sha256")
        host.commands[served_command] = completed(
            served_command,
            stdout="-----BEGIN CERTIFICATE-----\nCERT\n-----END CERTIFICATE-----\n",
        )
        host.commands[served_x509] = completed(served_x509, stdout="sha256 Fingerprint=AA:BB\n")
        host.commands[leaf_x509] = completed(leaf_x509, stdout="sha256 Fingerprint=AA:BB\n")
        stage = next(stage for stage in STAGES if stage.identifier == "probe")
        result = stage.callable(ctx)
        self.assertTrue(result.ok, result.detail)
        self.assertIn(served_command, [call[0] for call in host.calls])

        host.commands[leaf_x509] = completed(leaf_x509, stdout="sha256 Fingerprint=CC:DD\n")
        result = stage.callable(ctx)
        self.assertFalse(result.ok)
        self.assertIn("aabb", result.detail)
        self.assertIn("ccdd", result.detail)

    def test_stage_table_runs_the_receiving_office_legs_in_order(self) -> None:
        identifiers = [stage.identifier for stage in STAGES]
        self.assertEqual(
            identifiers[8:15],
            [
                "site",
                "provision-2",
                "authorize",
                "preflight",
                "install",
                "alerts",
                "probe",
            ],
        )


class PhaseBBuild(unittest.TestCase):
    def test_image_conversion_keeps_source_read_only_and_records_the_mirror_tag(self) -> None:
        host = FakeHost(commands=phase_b_commands(), files=base_files())
        ctx = harness_context(host)

        result = image.build(ctx)

        self.assertTrue(result.ok, result.detail)
        commands = [call[0] for call in host.calls]
        self.assertEqual(
            commands[:2],
            [
                ("qemu-img", "create", "-f", "qcow2", str(RUN_DIR / "os.qcow2"), "40G"),
                ("qemu-img", "create", "-f", "qcow2", str(RUN_DIR / "data.qcow2"), "2T"),
            ],
        )
        guestfish_call = next(call for call in host.calls if call[0] == ("guestfish",))
        script = guestfish_call[2]
        assert script is not None
        lines = script.splitlines()
        self.assertEqual(lines[0], f"add-drive {RUN_DIR / 'os.qcow2'}")
        self.assertEqual(lines[1], f"add-drive-ro {IMAGE}")
        self.assertEqual(lines[2], "run")
        self.assertIn("part-set-gpt-type /dev/sda 1 21686148-6449-6E6F-744E-656564454649", lines)
        self.assertIn("part-set-gpt-type /dev/sda 2 C12A7328-F81F-11D2-BA4B-00A0C93EC93B", lines)
        self.assertIn("part-set-gpt-type /dev/sda 4 E6D6D379-F507-44C2-A23C-238F2A3DF928", lines)
        self.assertIn("copy-device-to-device /dev/sdb15 /dev/sda2", lines)
        self.assertIn("copy-device-to-device /dev/sdb13 /dev/sda3", lines)
        self.assertIn("copy-device-to-device /dev/sdb1 /dev/ubuntu-vg/root", lines)
        self.assertIn("write /etc/fstab", script)
        self.assertIn("umount-all", lines)
        self.assertEqual([line for line in lines if str(IMAGE) in line], [f"add-drive-ro {IMAGE}"])
        self.assertIn(
            (
                "git",
                "-c",
                f"safe.directory={CHECKOUT}",
                "clone",
                "--mirror",
                str(CHECKOUT),
                str(RUN_DIR / "GIDEON.git"),
            ),
            commands,
        )
        self.assertIn(
            ("git", "-C", str(RUN_DIR / "GIDEON.git"), "tag", f"acceptance-{RESOLVED[:12]}", RESOLVED),
            commands,
        )
        self.assertEqual(ctx.spec.clone_ref, f"acceptance-{RESOLVED[:12]}")
        customize = next(command for command in commands if command[:1] == ("virt-customize",))
        self.assertIn("--no-network", customize)
        self.assertIn("--copy-in", customize)
        self.assertIn("dracut --force --no-hostonly --regenerate-all", customize)

    def test_tag_ref_is_cloned_without_creating_a_mirror_only_tag(self) -> None:
        host = FakeHost(commands=phase_b_commands(), files=base_files())
        tag_ref = replace(harness_context(host).spec, ref="v9.8.7")
        ctx = harness_context(host)
        ctx.spec = tag_ref
        tag_probe = (
            "git",
            "-c",
            f"safe.directory={CHECKOUT}",
            "rev-parse",
            "--verify",
            "refs/tags/v9.8.7",
        )
        host.commands[tag_probe] = completed(tag_probe, stdout=RESOLVED + "\n")

        result = image.build(ctx)

        self.assertTrue(result.ok, result.detail)
        self.assertEqual(ctx.spec.clone_ref, "v9.8.7")
        self.assertFalse(any(command[0:4] == ("git", "-C", str(RUN_DIR / "GIDEON.git"), "tag") for command in (call[0] for call in host.calls)))

    def test_seed_contains_the_two_csas_and_unprivileged_backup_account(self) -> None:
        host = FakeHost(commands=phase_b_commands(), files=base_files())
        ctx = harness_context(host)

        result = seed.build(ctx)

        self.assertTrue(result.ok, result.detail)
        user_data = yaml.safe_load(host.files[str(RUN_DIR / "user-data")].split("\n", 1)[1])
        self.assertEqual(user_data["hostname"], "test-vm")
        self.assertEqual(user_data["fqdn"], "test-vm.acceptance.invalid")
        self.assertTrue(user_data["manage_etc_hosts"])
        self.assertFalse(user_data["ssh_pwauth"])
        users = {user["name"]: user for user in user_data["users"]}
        self.assertEqual(set(users), {"csa1", "csa2", "gideon-backup"})
        for name in ("csa1", "csa2"):
            self.assertEqual(users[name]["groups"], ["sudo"])
            self.assertEqual(users[name]["sudo"], "ALL=(ALL) NOPASSWD:ALL")
            self.assertEqual(users[name]["shell"], "/bin/bash")
            self.assertEqual(users[name]["ssh_authorized_keys"], ["ssh-ed25519 AAAA acceptance-key"])
        self.assertEqual(users["gideon-backup"]["shell"], "/bin/sh")
        self.assertEqual(users["gideon-backup"]["homedir"], "/srv/gideon-backup")
        self.assertNotIn("sudo", users["gideon-backup"])
        self.assertNotIn("ssh_authorized_keys", users["gideon-backup"])
        self.assertEqual(user_data["runcmd"], ["chown -R csa1:csa1 /srv/GIDEON.git"])
        metadata = yaml.safe_load(host.files[str(RUN_DIR / "meta-data")])
        self.assertEqual(metadata, {"instance-id": ctx.run_id, "local-hostname": "test-vm.acceptance.invalid"})
        commands = [call[0] for call in host.calls]
        self.assertIn(("ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(RUN_DIR / "id_ed25519"), "-C", ctx.run_id), commands)
        self.assertIn(("cloud-localds", str(RUN_DIR / "seed.iso"), str(RUN_DIR / "user-data"), str(RUN_DIR / "meta-data")), commands)


class VmContracts(unittest.TestCase):
    def test_redact_masks_print_once_lines_and_age_keys_only(self) -> None:
        complete_key = "AGE-SECRET-KEY-1" + "a" * 58
        short_key = "AGE-SECRET-KEY-1abc"
        transcript = (
            "before\n"
            f"gideon_admin_password (break-glass administrator): secret{PRINT_ONCE_SUFFIX}\n"
            f"age identity (store it in the office password manager now): {complete_key}\n"
            f"short key: {short_key}\n"
            "after\n"
        )

        redacted = vm.redact(transcript)

        self.assertEqual(
            redacted,
            "before\n"
            f"gideon_admin_password (break-glass administrator): <redacted>{PRINT_ONCE_SUFFIX}\n"
            "age identity (store it in the office password manager now): "
            "<redacted>\n"
            "short key: AGE-SECRET-KEY-1<redacted>\n"
            "after\n",
        )

    def test_run_product_writes_a_redacted_print_once_transcript(self) -> None:
        host = FakeHost()
        ctx = harness_context(host)
        secret_line = f"gideon_admin_password (break-glass administrator): secret{PRINT_ONCE_SUFFIX}\n"
        script = "cd /opt/gideon && sudo print-secrets 2>&1 | tee ~/acceptance/secrets.txt"
        command = tuple(vm.ssh_argv(ctx, script))
        host.commands[command] = completed(command, stdout="before\n" + secret_line + "after\n")

        result, text = vm.run_product(ctx, "provision", "secrets.txt", ["print-secrets"])

        expected = (
            "before\n"
            f"gideon_admin_password (break-glass administrator): <redacted>{PRINT_ONCE_SUFFIX}\n"
            "after\n"
        )
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(text, expected)
        self.assertEqual(host.files[str(OUT / "07-secrets.txt")], expected)

    def test_run_product_writes_vm_site_values_as_placeholders(self) -> None:
        """Ticket 55 sends product transcripts through the rendered VM site."""

        host = FakeHost()
        ctx = harness_context(host)
        loaded = host_site.load_site(SITE)
        assert loaded.config is not None
        ctx.vm_site = loaded.config
        script = "cd /opt/gideon && sudo print-site 2>&1 | tee ~/acceptance/site.txt"
        command = tuple(vm.ssh_argv(ctx, script))
        host.commands[command] = completed(
            command, stdout=f"https://{ctx.vm_site.hostname}/health\n"
        )

        result, text = vm.run_product(ctx, "provision", "site.txt", ["print-site"])

        self.assertTrue(result.ok, result.detail)
        self.assertEqual(text, "https://<hostname>/health\n")
        self.assertEqual(host.files[str(OUT / "07-site.txt")], text)

    def test_boot_defines_starts_polls_a_lease_and_waits_for_cloud_init(self) -> None:
        host = FakeHost(commands=phase_b_commands(), files=base_files())
        ctx = harness_context(host, address=None)

        result = vm.boot(ctx)

        self.assertTrue(result.ok, result.detail)
        self.assertEqual(ctx.address, "192.168.122.10")
        commands = [call[0] for call in host.calls]
        self.assertLess(commands.index(("virsh", "define", str(RUN_DIR / "domain.xml"))), commands.index(("virsh", "start", "test-vm")))
        self.assertLess(commands.index(("virsh", "start", "test-vm")), commands.index(("virsh", "domifaddr", "test-vm", "--source", "lease")))
        self.assertTrue(any(command[0] == "ssh" and "cloud-init status --wait" in command[-1] for command in commands))

    def test_lease_timeout_names_the_console_log_and_uses_injected_sleep(self) -> None:
        host = FakeHost()
        sleeps: list[float] = []

        result, address = vm.wait_for_address(
            host,
            "test-vm",
            timeout=2,
            sleep=sleeps.append,
            console_path=OUT / "console.log",
        )

        self.assertFalse(result.ok)
        self.assertIsNone(address)
        self.assertIn(str(OUT / "console.log"), result.detail)
        self.assertEqual(sleeps, [1, 1])

    def test_product_and_copy_in_keep_secret_text_out_of_argv(self) -> None:
        host = FakeHost()
        ctx = harness_context(host)
        product_script = "cd /opt/gideon && sudo python3 -m gideon host provision --no-gpu 2>&1 | tee ~/acceptance/provision.txt"
        product_command = tuple(vm.ssh_argv(ctx, product_script))
        host.commands[product_command] = completed(product_command, stdout="provision output\n")
        copy_script = "cat > /tmp/secret && chmod 600 /tmp/secret"
        copy_command = tuple(vm.ssh_argv(ctx, copy_script))
        host.commands[copy_command] = completed(copy_command)

        product, text = vm.run_product(
            ctx,
            "provision",
            "provision.txt",
            ["python3", "-m", "gideon", "host", "provision", "--no-gpu"],
        )
        copied = vm.copy_in(ctx, "/tmp/secret", "super-secret", 0o600)
        self.assertEqual(text, "provision output\n")

        self.assertTrue(product.ok, product.detail)
        self.assertTrue(copied.ok, copied.detail)
        self.assertEqual(host.calls[-1][2], "super-secret")
        self.assertNotIn("super-secret", host.calls[-1][0])
        self.assertEqual(host.files[str(OUT / "07-provision.txt")], "provision output\n")

    def test_failed_product_command_names_the_host_transcript(self) -> None:
        host = FakeHost()
        ctx = harness_context(host, stage="alerts")
        script = "cd /opt/gideon && sudo alerts test 2>&1 | tee ~/acceptance/alerts.txt"
        command = tuple(vm.ssh_argv(ctx, script))
        host.commands[command] = completed(command, returncode=1, stdout="alert failed\n")

        result, _ = vm.run_product(ctx, "alerts", "alerts.txt", ["alerts", "test"])

        transcript = OUT / "14-alerts.txt"
        self.assertFalse(result.ok)
        self.assertIn(str(transcript), result.detail)
        self.assertIn(str(transcript), result.fix)
        self.assertEqual(host.files[str(transcript)], "alert failed\n")

    def test_degraded_cloud_init_is_reported_but_not_fatal(self) -> None:
        host = FakeHost()
        ctx = harness_context(host)
        script = "cloud-init status --wait"
        command = tuple(vm.ssh_argv(ctx, script))
        host.commands[command] = completed(command, returncode=2, stdout="status: degraded\n")

        result = vm.wait_for_cloud_init(ctx)

        self.assertTrue(result.ok)
        self.assertIn("degraded", result.detail)

    def test_clone_creates_owned_checkout_then_uses_the_selected_mirror_tag(self) -> None:
        host = FakeHost()
        ctx = harness_context(host)

        result = vm.clone(ctx)

        self.assertTrue(result.ok, result.detail)
        scripts = [shlex.split(call[0][-1])[0] for call in host.calls if call[0][0] == "ssh"]
        self.assertEqual(scripts[0], "sudo install -d -o csa1 -g csa1 /opt/gideon /home/csa1/acceptance")
        self.assertEqual(scripts[1], "git clone --branch acceptance-aaaaaaaaaaaa /srv/GIDEON.git /opt/gideon")

    def test_reboot_tolerates_disconnect_then_reconnects_and_waits_for_cloud_init(self) -> None:
        host = FakeHost()
        ctx = harness_context(host)
        reboot_command = tuple(vm.ssh_argv(ctx, vm.REBOOT_SCRIPT))
        true_command = tuple(vm.ssh_argv(ctx, "true"))
        cloud_command = tuple(vm.ssh_argv(ctx, "cloud-init status --wait"))
        host.commands[reboot_command] = completed(reboot_command)
        # Down for two probes, then back.
        host.commands[true_command] = [
            completed(true_command, returncode=255),
            completed(true_command, returncode=255),
            completed(true_command),
        ]
        host.commands[cloud_command] = completed(cloud_command)
        sleeps: list[float] = []
        ctx.sleep = sleeps.append

        result = vm.reboot_and_wait(ctx)

        self.assertTrue(result.ok, result.detail)
        self.assertEqual(sleeps, [1, 1])
        self.assertEqual(sum(call[0] == true_command for call in host.calls), 3)

    def test_an_ssh_timeout_is_final_and_never_retried_as_a_disconnect(self) -> None:
        """A 900 s wait that times out must not be retried hundreds of times."""

        host = FakeHost()
        ctx = harness_context(host)
        command = tuple(vm.ssh_argv(ctx, "cloud-init status --wait"))

        def timing_out(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(list(command), vm.CLOUD_INIT_TIMEOUT_SECONDS)

        original = host.run

        def run(argv: Command, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if tuple(argv) == command:
                host.calls.append((tuple(argv), None, None, None))
                return timing_out()
            return original(argv, **kwargs)  # type: ignore[arg-type]

        host.run = run  # type: ignore[method-assign]
        ctx.sleep = lambda _seconds: None
        result = vm.wait_for_cloud_init(ctx)
        self.assertFalse(result.ok)
        self.assertIn("did not finish within", result.detail)
        self.assertEqual(sum(call[0] == command for call in host.calls), 1)

    def test_reboot_gives_up_at_its_deadline(self) -> None:
        host = FakeHost()
        ctx = harness_context(host)
        reboot_command = tuple(vm.ssh_argv(ctx, vm.REBOOT_SCRIPT))
        true_command = tuple(vm.ssh_argv(ctx, "true"))
        host.commands[reboot_command] = completed(reboot_command)
        host.commands[true_command] = completed(true_command, returncode=255)
        ticks = iter(range(0, 10_000, 100))
        ctx.clock = lambda: float(next(ticks))
        ctx.sleep = lambda _seconds: None

        result = vm.reboot_and_wait(ctx)

        self.assertFalse(result.ok)
        self.assertIn("did not come back", result.detail)
        probes = sum(call[0] == true_command for call in host.calls)
        self.assertLessEqual(probes, vm.RECONNECT_TIMEOUT_SECONDS // 100 + 2)

    def test_reboot_waits_for_the_vm_to_go_down_before_trusting_an_answer(self) -> None:
        """A probe answered by the old boot is not the reboot; the VM must drop first."""

        host = FakeHost()
        ctx = harness_context(host)
        reboot_command = tuple(vm.ssh_argv(ctx, vm.REBOOT_SCRIPT))
        true_command = tuple(vm.ssh_argv(ctx, "true"))
        cloud_command = tuple(vm.ssh_argv(ctx, "cloud-init status --wait"))
        host.commands[reboot_command] = completed(reboot_command)
        host.commands[true_command] = [
            completed(true_command),
            completed(true_command, returncode=255),
            completed(true_command),
        ]
        host.commands[cloud_command] = completed(cloud_command)
        ctx.sleep = lambda _seconds: None

        result = vm.reboot_and_wait(ctx)

        self.assertTrue(result.ok, result.detail)
        self.assertEqual(sum(call[0] == true_command for call in host.calls), 3)

    def test_ssh_script_travels_as_one_quoted_word(self) -> None:
        """ssh joins remote words with spaces; an unquoted -c script would be split."""

        ctx = harness_context(FakeHost())
        argv = vm.ssh_argv(ctx, "cd /opt/gideon && sudo true 2>&1 | tee ~/acceptance/x.txt")
        self.assertEqual(argv[-4:-1], ["-o", "pipefail", "-c"])
        self.assertEqual(shlex.split(argv[-1]), ["cd /opt/gideon && sudo true 2>&1 | tee ~/acceptance/x.txt"])
        self.assertEqual(vm.ssh_argv(ctx, "true")[-1], "true")

    def test_blocked_rows_pass_the_first_provision_and_fail_the_second(self) -> None:
        """Before the site file the site steps are blocked by design; after it none may be."""

        output = "platform: ok — supported platform\nfirewall: blocked — site file is required by this step Fix: write it\n"
        for allow_blocked, expected in ((True, True), (False, False)):
            with self.subTest(allow_blocked=allow_blocked):
                host = FakeHost()
                ctx = harness_context(host)
                script = "cd /opt/gideon && sudo python3 -m gideon host provision --no-gpu 2>&1 | tee ~/acceptance/provision.txt"
                command = tuple(vm.ssh_argv(ctx, script))
                host.commands[command] = completed(command, stdout=output)
                result = vm.provision(ctx, allow_blocked=allow_blocked)
                self.assertEqual(result.ok, expected, result.detail)
                if not expected:
                    self.assertIn("firewall", result.detail)

    def test_provision_retries_reboot_required_at_most_twice(self) -> None:
        host = FakeHost()
        ctx = harness_context(host)
        outputs = (
            "provision: reboot-required — reboot\n",
            "provision: ok — converged\n",
        )
        for suffix, output in (("", outputs[0]), ("-2", outputs[1])):
            script = f"cd /opt/gideon && sudo python3 -m gideon host provision --no-gpu 2>&1 | tee ~/acceptance/provision{suffix}.txt"
            command = tuple(vm.ssh_argv(ctx, script))
            host.commands[command] = completed(command, stdout=output)
        cloud_command = tuple(vm.ssh_argv(ctx, "cloud-init status --wait"))
        host.commands[cloud_command] = completed(cloud_command)
        ctx.sleep = lambda _seconds: None
        reboot_command = tuple(vm.ssh_argv(ctx, vm.REBOOT_SCRIPT))

        result = vm.provision(ctx, allow_blocked=True)

        self.assertTrue(result.ok, result.detail)
        self.assertIn(str(OUT / "07-provision.txt"), host.files)
        self.assertIn(str(OUT / "07-provision-2.txt"), host.files)
        self.assertEqual(sum(call[0] == reboot_command for call in host.calls), 1)


class PhaseFourRehearsal(unittest.TestCase):
    def _tag_context(
        self,
        *,
        leg_outputs: Sequence[str] | None = None,
        push_result: subprocess.CompletedProcess[str] | None = None,
    ) -> tuple[FakeHost, HarnessContext]:
        host = FakeHost(files=base_files())
        ctx = harness_context(host, stage="rehearse")
        mirror = RUN_DIR / "GIDEON.git"
        worktree = RUN_DIR / "rehearsal"
        show = (
            "git",
            "-C",
            str(mirror),
            "show",
            f"{RESOLVED}:gideon/__init__.py",
        )
        host.commands[show] = completed(show, stdout='__version__ = "1.2.3"\n')
        host.files[str(worktree / "gideon/__init__.py")] = '__version__ = "1.2.3"\n'
        add = ("git", "-C", str(mirror), "worktree", "add", "--detach", str(worktree), RESOLVED)
        commit = (
            "git",
            "-C",
            str(worktree),
            "-c",
            "user.name=gideon-acceptance",
            "-c",
            "user.email=acceptance@gideon.invalid",
            "commit",
            "-qam",
            "acceptance: rehearse v1.2.4-rc.1",
        )
        tag = ("git", "-C", str(worktree), "tag", "v1.2.4-rc.1")
        push = (
            "git",
            "-C",
            str(worktree),
            "push",
            "ssh://csa1@192.168.122.10/srv/GIDEON.git",
            "refs/tags/v1.2.4-rc.1",
        )
        remove = ("git", "-C", str(mirror), "worktree", "remove", "--force", str(worktree))
        for git_command in (add, commit, tag, remove):
            host.commands[git_command] = completed(git_command)
        host.commands[push] = push_result or completed(push)
        outputs = leg_outputs or ("upgrade: ok — complete\n",) * 4
        for number, output in enumerate(outputs, start=1):
            if number % 2:
                leg_command = ("./upgrade.sh", "v1.2.4-rc.1")
                name = f"rehearse-{number}-upgrade.txt"
            else:
                leg_command = ("./upgrade.sh", "--rollback")
                name = f"rehearse-{number}-rollback.txt"
            script = (
                f"cd /opt/gideon && sudo {shlex.join(leg_command)} 2>&1 | tee ~/acceptance/{name}"
            )
            ssh = tuple(vm.ssh_argv(ctx, script))
            host.commands[ssh] = completed(ssh, stdout=output)
        return host, ctx

    def test_rc_tag_increments_only_the_patch(self) -> None:
        self.assertEqual(rehearsal.rc_tag("0.1.0"), "v0.1.1-rc.1")
        self.assertEqual(rehearsal.rc_tag("1.2.9"), "v1.2.10-rc.1")

    def test_base_version_reads_the_resolved_commit_and_refuses_missing_pin(self) -> None:
        host, ctx = self._tag_context()
        parsed = rehearsal.base_version(ctx)
        self.assertEqual(parsed, "1.2.3")
        show_call = next(call for call in host.calls if call[0][0:3] == ("git", "-C", str(RUN_DIR / "GIDEON.git")))
        self.assertEqual(show_call[0][4], f"{RESOLVED}:gideon/__init__.py")

        absent = FakeHost()
        absent_ctx = harness_context(absent, stage="rehearse")
        absent_show = (
            "git",
            "-C",
            str(RUN_DIR / "GIDEON.git"),
            "show",
            f"{RESOLVED}:gideon/__init__.py",
        )
        absent.commands[absent_show] = completed(absent_show, stdout="no version here\n")
        parsed = rehearsal.base_version(absent_ctx)
        self.assertIsInstance(parsed, Problem)
        assert isinstance(parsed, Problem)
        self.assertIn("__version__", parsed.problem)

    def test_rehearsal_rewrites_the_pin_pushes_the_tag_and_removes_the_worktree(self) -> None:
        host, ctx = self._tag_context()

        result = rehearsal.rehearse(ctx)

        self.assertTrue(result.ok, result.detail)
        self.assertEqual(ctx.rc_tag, "v1.2.4-rc.1")
        self.assertIn("v1.2.4-rc.1", result.detail)
        self.assertEqual(
            host.files[str(RUN_DIR / "rehearsal/gideon/__init__.py")],
            '__version__ = "1.2.4-rc.1"\n',
        )
        self.assertNotIn(str(RUN_DIR / "rehearsal/tests/test_version.py"), host.files)
        git_calls = [call for call in host.calls if call[0][0] == "git"]
        self.assertEqual(
            [call[0] for call in git_calls[1:]],
            [
                ("git", "-C", str(RUN_DIR / "GIDEON.git"), "worktree", "add", "--detach", str(RUN_DIR / "rehearsal"), RESOLVED),
                ("git", "-C", str(RUN_DIR / "rehearsal"), "-c", "user.name=gideon-acceptance", "-c", "user.email=acceptance@gideon.invalid", "commit", "-qam", "acceptance: rehearse v1.2.4-rc.1"),
                ("git", "-C", str(RUN_DIR / "rehearsal"), "tag", "v1.2.4-rc.1"),
                ("git", "-C", str(RUN_DIR / "rehearsal"), "push", "ssh://csa1@192.168.122.10/srv/GIDEON.git", "refs/tags/v1.2.4-rc.1"),
                ("git", "-C", str(RUN_DIR / "GIDEON.git"), "worktree", "remove", "--force", str(RUN_DIR / "rehearsal")),
            ],
        )
        self.assertTrue(git_calls[4][1] is not None)
        assert git_calls[4][1] is not None
        self.assertIn("GIT_SSH_COMMAND", git_calls[4][1])
        self.assertIn("id_ed25519", git_calls[4][1]["GIT_SSH_COMMAND"])
        self.assertIn("known_hosts", git_calls[4][1]["GIT_SSH_COMMAND"])
        self.assertFalse(any(str(CHECKOUT) in word for call in git_calls for word in call[0]))

    def test_rehearsal_removes_the_worktree_after_a_failed_push(self) -> None:
        push = (
            "git",
            "-C",
            str(RUN_DIR / "rehearsal"),
            "push",
            "ssh://csa1@192.168.122.10/srv/GIDEON.git",
            "refs/tags/v1.2.4-rc.1",
        )
        host, ctx = self._tag_context(push_result=completed(push, returncode=1, stderr="push failed"))

        result = rehearsal.rehearse(ctx)

        self.assertFalse(result.ok)
        self.assertIn("push", result.detail)
        self.assertIn(
            ("git", "-C", str(RUN_DIR / "GIDEON.git"), "worktree", "remove", "--force", str(RUN_DIR / "rehearsal")),
            [call[0] for call in host.calls],
        )

    def test_rehearsal_runs_four_legs_and_rejects_a_refusal_row(self) -> None:
        host, ctx = self._tag_context()
        result = rehearsal.rehearse(ctx)
        self.assertTrue(result.ok, result.detail)
        scripts = [shlex.split(call[0][-1])[0] for call in host.calls if call[0][0] == "ssh"]
        self.assertEqual(
            scripts,
            [
                "cd /opt/gideon && sudo ./upgrade.sh v1.2.4-rc.1 2>&1 | tee ~/acceptance/rehearse-1-upgrade.txt",
                "cd /opt/gideon && sudo ./upgrade.sh --rollback 2>&1 | tee ~/acceptance/rehearse-2-rollback.txt",
                "cd /opt/gideon && sudo ./upgrade.sh v1.2.4-rc.1 2>&1 | tee ~/acceptance/rehearse-3-upgrade.txt",
                "cd /opt/gideon && sudo ./upgrade.sh --rollback 2>&1 | tee ~/acceptance/rehearse-4-rollback.txt",
            ],
        )
        self.assertEqual(len(ctx.transcripts), 4)

        host, ctx = self._tag_context(leg_outputs=("provision: refuse — bad\n",))
        result = rehearsal.rehearse(ctx)
        self.assertFalse(result.ok)
        self.assertIn("rehearse-1-upgrade.txt", result.detail)

    def test_restore_runs_push_restore_and_apply_in_order(self) -> None:
        host = FakeHost()
        ctx = harness_context(host, stage="restore")
        commands = (
            ("restore-1-push.txt", ("python3", "-m", "gideon", "backup", "push")),
            ("restore-2-restore.txt", ("python3", "-m", "gideon", "restore", "--from", "target")),
            ("restore-3-apply.txt", ("python3", "-m", "gideon", "apply")),
        )
        expected_scripts: list[str] = []
        for transcript, command in commands:
            script = f"cd /opt/gideon && sudo {shlex.join(command)} 2>&1 | tee ~/acceptance/{transcript}"
            ssh = tuple(vm.ssh_argv(ctx, script))
            host.commands[ssh] = completed(ssh, stdout="ok\n")
            expected_scripts.append(script)

        result = rehearsal.restore(ctx)

        self.assertTrue(result.ok, result.detail)
        scripts = [shlex.split(call[0][-1])[0] for call in host.calls]
        self.assertEqual(scripts, expected_scripts)

    def _verify_context(
        self,
        *,
        release: str = "1.2.3",
        messages: int = 2,
    ) -> tuple[FakeHost, HarnessContext]:
        host = FakeHost()
        ctx = harness_context(host, stage="verify")
        ctx.base_version = "1.2.3"
        ctx.services = ServiceMaterial("", "", "", "", "", "", "run-user")
        sink = FakeSink(port=2525)
        sink.messages.extend(FakeMessage(user="run-user", tls=True) for _ in range(messages))
        ctx.sink = cast(SinkLike, sink)
        host.files["/etc/gideon/rendered/applied.yaml"] = yaml.safe_dump({"release": release})
        read_script = "cat /etc/gideon/rendered/applied.yaml"
        read_command = tuple(vm.ssh_argv(ctx, read_script))
        host.commands[read_command] = completed(
            read_command,
            stdout=host.files["/etc/gideon/rendered/applied.yaml"],
        )
        existing = OUT / "prior-transcript.txt"
        host.files[str(existing)] = "present\n"
        ctx.transcripts.append(existing)
        return host, ctx

    def test_verify_checks_release_sink_and_all_transcripts(self) -> None:
        _host, ctx = self._verify_context()
        result = rehearsal.verify(ctx)
        self.assertTrue(result.ok, result.detail)
        self.assertIn("release 1.2.3", result.detail)
        self.assertIn("2 authenticated TLS", result.detail)

        _host, mismatch = self._verify_context(release="9.9.9")
        result = rehearsal.verify(mismatch)
        self.assertFalse(result.ok)
        self.assertIn("9.9.9", result.detail)
        self.assertIn("1.2.3", result.detail)

        _host, missing = self._verify_context()
        missing.transcripts.append(OUT / "missing.txt")
        result = rehearsal.verify(missing)
        self.assertFalse(result.ok)
        self.assertIn("missing.txt", result.detail)

        _host, too_few = self._verify_context(messages=1)
        result = rehearsal.verify(too_few)
        self.assertFalse(result.ok)
        self.assertIn("at least 2", result.detail)


class DomainContracts(unittest.TestCase):
    def test_domain_xml_has_owned_disks_seed_network_console_and_metadata(self) -> None:
        host = FakeHost(files={str(TEMPLATE): TEMPLATE.read_text()})
        xml = domain.domain_xml(
            host,
            template_path=TEMPLATE,
            name="test-vm",
            os_path=RUN_DIR / "os.qcow2",
            data_path=RUN_DIR / "data.qcow2",
            seed_path=RUN_DIR / "seed.iso",
            console_path=OUT / "console.log",
            run_id="run-123",
        )
        root = ET.fromstring(xml)
        disks = [disk for disk in root.findall("./devices/disk") if disk.get("device") == "disk"]
        self.assertEqual(len(disks), 2)
        targets = [disk.find("target") for disk in disks]
        assert all(target is not None for target in targets)
        self.assertEqual(
            {target.get("bus") for target in targets if target is not None}, {"scsi"}
        )
        self.assertEqual(len({disk.findtext("wwn") for disk in disks}), 2)
        cdrom_source = root.find("./devices/disk[@device='cdrom']/source")
        assert cdrom_source is not None
        self.assertEqual(
            cdrom_source.get("file"),
            str(RUN_DIR / "seed.iso"),
        )
        interface_source = root.find("./devices/interface/source")
        interface_model = root.find("./devices/interface/model")
        serial_source = root.find("./devices/serial/source")
        assert interface_source is not None
        assert interface_model is not None
        assert serial_source is not None
        self.assertEqual(interface_source.get("network"), "default")
        self.assertEqual(interface_model.get("type"), "virtio")
        self.assertEqual(serial_source.get("path"), str(OUT / "console.log"))
        metadata = root.find(f"./metadata/{{{domain.HARNESS_NAMESPACE}}}run")
        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(metadata.get("id"), "run-123")

    def test_owned_leftover_is_destroyed_and_foreign_domain_is_never_destroyed(self) -> None:
        host = FakeHost(commands=base_commands(), files=base_files())
        owned_xml = domain.domain_xml(
            host,
            template_path=CHECKOUT / "tools/acceptance/domain.xml.tmpl",
            name="test-vm",
            os_path=RUN_DIR / "os.qcow2",
            data_path=RUN_DIR / "data.qcow2",
            seed_path=RUN_DIR / "seed.iso",
            console_path=OUT / "console.log",
            run_id="run-123",
        )
        host.commands[("virsh", "dominfo", "test-vm")] = completed(("virsh", "dominfo"))
        host.commands[("virsh", "dumpxml", "test-vm")] = completed(("virsh", "dumpxml"), stdout=owned_xml)
        host.commands[("virsh", "destroy", "test-vm")] = completed(("virsh", "destroy"))
        host.commands[("virsh", "undefine", "test-vm", "--remove-all-storage")] = completed(("virsh", "undefine"))
        host.commands[("rm", "-rf", str(RUN_DIR))] = completed(("rm", "-rf"))
        code, out, _err, _ = invoke(host, argv=["HEAD", "--name", "test-vm"])
        self.assertEqual(code, 1)
        self.assertIn(("virsh", "destroy", "test-vm"), [call[0] for call in host.calls])
        self.assertIn(("virsh", "undefine", "test-vm", "--remove-all-storage"), [call[0] for call in host.calls])

        # A shut-off leftover is undefined without a destroy.
        off = FakeHost(commands=dict(host.commands), files=base_files())
        off.commands[("virsh", "domstate", "test-vm")] = completed(("virsh", "domstate"), stdout="shut off\n")
        code, out, _err, _ = invoke(off, argv=["HEAD", "--name", "test-vm"])
        self.assertEqual(code, 1)
        off_calls = [call[0] for call in off.calls]
        self.assertNotIn(("virsh", "destroy", "test-vm"), off_calls)
        self.assertIn(("virsh", "undefine", "test-vm", "--remove-all-storage"), off_calls)

        # No harness metadata, or a disk outside the harness root: refused by
        # name, never destroyed — --name collisions cannot delete someone's VM.
        elsewhere_xml = owned_xml.replace(str(RUN_DIR / "data.qcow2"), "/var/lib/libvirt/images/other.qcow2")
        for label, xml in (
            ("no-metadata", "<domain><name>test-vm</name><devices/></domain>"),
            ("disk-elsewhere", elsewhere_xml),
        ):
            with self.subTest(label=label):
                foreign = FakeHost(commands=base_commands(), files=base_files())
                foreign.commands[("virsh", "dominfo", "test-vm")] = completed(("virsh", "dominfo"))
                foreign.commands[("virsh", "dumpxml", "test-vm")] = completed(("virsh", "dumpxml"), stdout=xml)
                code, out, _err, _ = invoke(foreign, argv=["HEAD", "--name", "test-vm"])
                self.assertEqual(code, 1)
                self.assertIn("not an acceptance VM", out)
                calls = [call[0] for call in foreign.calls]
                self.assertNotIn(("virsh", "destroy", "test-vm"), calls)
                self.assertNotIn(("virsh", "undefine", "test-vm", "--remove-all-storage"), calls)

    def test_a_raising_stage_is_a_failed_row_with_the_stage_fix_and_teardown_runs(self) -> None:
        """The command boundary: a stage's traceback is a bug, never the run's output."""

        from tools.acceptance import run as run_module
        from tools.acceptance.context import Stage

        def explode(_ctx: HarnessContext) -> StageResult:
            raise RuntimeError("boom")

        host = FakeHost(commands=base_commands(), files=base_files())
        stages = (
            Stage("preconditions", run_module.preconditions, "fix-preconditions"),
            Stage("image", explode, "Inspect the image stage."),
            STAGES[-1],
        )
        spec = RunSpec("HEAD", "", "gideon-acceptance", HARNESS_ROOT / "gideon-acceptance", OUT, False, None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.dict(os.environ, {}, clear=True):
            code = run_module.run(
                spec,
                host=host,
                fetcher=FakeFetcher(),
                checkout=CHECKOUT,
                site_path=SITE,
                template_path=CHECKOUT / "tools/acceptance/domain.xml.tmpl",
                sleep=lambda _seconds: None,
                stages=stages,
            )
        self.assertEqual(code, 1)
        self.assertIn("image: refuse — internal error: RuntimeError: boom Fix: Inspect the image stage.", out.getvalue())
        self.assertIn("teardown: ok", out.getvalue())

    def test_stage_identifiers_include_both_provision_entries(self) -> None:
        self.assertEqual(STAGE_IDENTIFIERS.count("provision"), 1)
        self.assertIn("provision-2", STAGE_IDENTIFIERS)
        self.assertEqual([stage.name for stage in STAGES].count("provision"), 2)
