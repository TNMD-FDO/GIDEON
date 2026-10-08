"""Truth tables for install-time preflight and egress checks."""

import dataclasses
import json
import os
import re
import shlex
import subprocess
import unittest
from collections.abc import Collection, Mapping
from fractions import Fraction
from pathlib import Path
from typing import ClassVar, Self, cast
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from gideon.host import cotenants, nogpu
from gideon.host.checks import (
    CHECKS,
    PreflightContext,
    Severity,
    capacity,
    format_gb,
    meminfo_kb,
    services,
)
from gideon.host.checks.artifacts import (
    DriverTestedCheck,
    HardwareProfileCheck,
    JurisdictionCheck,
    OsKernelCheck,
)
from gideon.host.checks.capacity import (
    DATA_DF_ARGV,
    HOST_MEMORY_FLOOR_FRACTION,
    MEMORY_LOW_QUERY,
    PROMETHEUS_QUERY_PATH,
    PROMETHEUS_TIMEOUT_SECONDS,
    DataVolumeCheck,
    HostMemoryCheck,
    LowReader,
    LowReading,
    parse_size_and_available,
    read_prometheus_low,
)
from gideon.host.checks.network import (
    PROBE_TIMEOUT_SECONDS,
    EgressCheck,
    HostnameCheck,
    NtpCheck,
    PortsCheck,
)
from gideon.host.checks.services import BackupSshCheck, LdapCheck, SmtpCheck
from gideon.host.corpus.lockfile import (
    PIPELINE_VERSION,
    SCHEMA_VERSION,
    Lockfile,
    SourcePin,
    load_lockfile,
)
from gideon.host.courts import Court, CourtMap, CourtSource
from gideon.host.egress import EgressAllowlist, EgressGroup, EgressHost
from gideon.host.ldap import ldapsearch_argv
from gideon.host.lock import load_host_lock
from gideon.host.models import GIGABYTE, load_models_lock
from gideon.host.render.services.prometheus import PROMETHEUS_LOOPBACK_ADDRESS
from gideon.host.site import load_site, render_errors
from gideon.host.sshtarget import BACKUP_PROBE_SHA256
from gideon.host.steps.nvidia import _LOADED_VERSION
from gideon.host.sysio import Command, PathLike
from tools.exportboundary import absent_from_export

ROOT = Path(__file__).resolve().parents[1]
MEMINFO = "/proc/meminfo"

_LOCK = load_host_lock("host.lock").lock
assert _LOCK is not None
LOCK = _LOCK
_MODELS = load_models_lock("models.lock").lock
assert _MODELS is not None
MODELS = _MODELS

COURTS = CourtMap(
    source=CourtSource(
        file="courts-2099-01-02.csv.bz2",
        date="2099-01-02",
        sha256="f" * 64,
        rows=7,
        levels={
            "scotus": 0,
            "circuit": 2,
            "district": 2,
            "state_supreme": 1,
            "state_appellate": 1,
            "other": 0,
        },
    ),
    courts={
        "fx-circuit": Court("fx-circuit", "fx-circuit", None, "circuit", "Fictitious Circuit"),
        "fx-circuit-alt": Court("fx-circuit-alt", "fx-circuit-alt", None, "circuit", "Fictitious Alternate Circuit"),
        "fx-district": Court("fx-district", "fx-circuit", "TN", "district", "Fictitious District"),
        "fx-district-alt": Court("fx-district-alt", "fx-circuit", "TN", "district", "Fictitious Alternate District"),
        "fx-state": Court("fx-state", "fx-circuit", "TN", "state_supreme", "Fictitious Supreme Court"),
        "fx-app": Court("fx-app", "fx-circuit", "TN", "state_appellate", "Fictitious Appellate Court"),
        "fx-other": Court("fx-other", None, None, "other", "Fictitious Other Court"),
    },
)
LOCKFILE_LABEL = "corpus-2099-01-02"
PREFLIGHT_LOCKFILES = Path(__file__).parent / "fixtures/preflight/lockfiles"

SITE_TEMPLATE = """\
office:
  name: Test Office
  short_name: TEST
  timezone: America/Chicago
hostname: gideon.test
lan_cidrs: [192.0.2.0/24]
jurisdiction: {{circuit: ca6, districts: [tnmd], states: [{states}]}}
auth:
  ldap:
    host: ad.test
backup:
  target:
    host: nas.test
    path: /volume1/backup
alerts:
  smtp:
    host: smtp.test
    from: gideon@ad.test
{smtp_user_line}  recipients: [csa@ad.test]
{extra}"""


def make_site(*, states: str = "", smtp_user: str | None = None, extra: str = ""):
    text = SITE_TEMPLATE.format(
        states=states,
        smtp_user_line=f"    user: {smtp_user}\n" if smtp_user is not None else "",
        extra=extra,
    )
    host = FakeHost(files={"/tmp/site.yaml": text})
    result = load_site(Path("/tmp/site.yaml"), host=host)
    assert result.config is not None, render_errors(result.errors)
    return result.config


def make_jurisdiction_site(
    *, circuit: str = "fx-circuit", district: str = "fx-district", states: str = ""
):
    text = SITE_TEMPLATE.format(states=states, smtp_user_line="", extra="")
    text = text.replace(
        "circuit: ca6, districts: [tnmd]",
        f"circuit: {circuit}, districts: [{district}]",
    )
    host = FakeHost(files={"/tmp/site.yaml": text})
    result = load_site(Path("/tmp/site.yaml"), host=host)
    assert result.config is not None, render_errors(result.errors)
    return result.config


ALLOWLIST = EgressAllowlist(
    version=1,
    groups=(
        EgressGroup(
            "host-provisioning",
            (EgressHost("a.example", "https://a.example/"),),
        ),
        EgressGroup(
            "install-upgrade",
            (EgressHost("b.example", "https://b.example/"),),
        ),
        EgressGroup("corpus", (EgressHost("c.example", "https://c.example/"),)),
    ),
)


class FakeHost:
    """A command→outcome Host; ``env`` prefixes are stripped before lookup."""

    def __init__(
        self,
        *,
        commands: Mapping[tuple[str, ...], subprocess.CompletedProcess[str]] | None = None,
        files: Mapping[str, str] | None = None,
    ) -> None:
        self.commands = dict(commands or {})
        self.files = dict(files or {})
        self.calls: list[tuple[str, object]] = []

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
        del check, input, cwd, env, timeout
        command = tuple(argv)
        self.calls.append(("run", command))
        bare = command
        if bare and bare[0] == "env":
            index = 1
            while index < len(bare) and "=" in bare[index]:
                index += 1
            bare = bare[index:]
        result = self.commands.get(bare) or self.commands.get(command)
        if result is None:
            result = subprocess.CompletedProcess(list(command), 1, "", "not found")
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
        self.calls.append(("write_text", (key, text, mode)))
        self.files[key] = text

    def exists(self, path: PathLike) -> bool:
        return os.fspath(path) in self.files

    def listdir(self, path: PathLike) -> list[str]:
        root = Path(path)
        return [Path(name).name for name in self.files if Path(name).parent == root]

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del missing_ok
        self.calls.append(("unlink", os.fspath(path)))
        self.files.pop(os.fspath(path), None)

    def stat(self, path: PathLike) -> os.stat_result:
        raise FileNotFoundError(os.fspath(path))

    def chmod(self, path: PathLike, mode: int) -> None:
        del path, mode

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        del path, uid, gid

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del path, mode, parents, exist_ok

    def geteuid(self) -> int:
        return 0


def completed(
    argv: tuple[str, ...], stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)


def context(
    host: FakeHost,
    site=None,
    lock=None,
    models=None,
    courts=None,
    *,
    lockfile: Collection[str] | None = None,
    no_gpu: bool = False,
) -> PreflightContext:
    corpus_lockfile = None
    if lockfile is not None:
        corpus_lockfile = Lockfile(
            schema=SCHEMA_VERSION,
            label=LOCKFILE_LABEL,
            pipeline=PIPELINE_VERSION,
            cut_at="2099-01-02T00:00:00Z",
            reason="tranche",
            sources={
                "caselaw": SourcePin(
                    snapshot_date="2099-01-02",
                    base_url="https://example.test/",
                    mirror_url=None,
                    sidecar_sha256="f" * 64,
                    files=0,
                    bytes=0,
                    index=(),
                    entries=(),
                    courts=tuple(sorted(lockfile)),
                )
            },
        )
    return PreflightContext(
        host=host,
        lock=lock or LOCK,
        models=models or MODELS,
        site=site or make_site(),
        egress=ALLOWLIST,
        courts=courts or COURTS,
        lockfile=corpus_lockfile,
        no_gpu=no_gpu,
    )


def probe_argv(url: str) -> tuple[str, ...]:
    return (
        "wget",
        "--tries=1",
        "-S",
        f"--connect-timeout={PROBE_TIMEOUT_SECONDS}",
        f"--read-timeout={PROBE_TIMEOUT_SECONDS}",
        "-qO",
        "/dev/null",
        url,
    )


class Registry(unittest.TestCase):
    def test_registered_check_names_are_unique_and_ordered(self) -> None:
        names = [check.name for check in CHECKS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(
            names,
            [
                "egress",
                "ports",
                "hostname",
                "ntp",
                "ldap",
                "backup-ssh",
                "smtp",
                "data-volume",
                "host-memory",
                "jurisdiction",
                "hardware-profile",
                "driver-tested",
                "os-kernel",
            ],
        )


class Egress(unittest.TestCase):
    """The check probes both pre-install egress groups and the site's registry host."""

    REGISTRY_PROBE = probe_argv("https://ghcr.io/v2/")

    def outcomes(self, **returncodes: int) -> dict[tuple[str, ...], subprocess.CompletedProcess[str]]:
        codes = {"a": 0, "b": 0, "registry": 0}
        codes.update(returncodes)
        return {
            probe_argv("https://a.example/"): completed((), returncode=codes["a"]),
            probe_argv("https://b.example/"): completed((), returncode=codes["b"]),
            self.REGISTRY_PROBE: completed((), returncode=codes["registry"]),
        }

    def test_all_reachable_passes(self) -> None:
        host = FakeHost(commands=self.outcomes())
        report = EgressCheck().run(context(host))
        self.assertEqual(report.severity, Severity.PASS)

    def test_auth_gated_registry_response_still_counts_as_reachable(self) -> None:
        """A 401 on the registry /v2/ endpoint proves the origin answered."""

        commands = self.outcomes()
        commands[self.REGISTRY_PROBE] = completed(
            (), returncode=8, stderr="  HTTP/1.1 401 Unauthorized\n"
        )
        host = FakeHost(commands=commands)
        report = EgressCheck().run(context(host))
        self.assertEqual(report.severity, Severity.PASS, report.detail)

    def test_wget_auth_failure_exit_counts_as_reachable(self) -> None:
        """GNU wget exits 6, not 8, on a 401 — live finding, v0.0.9 hotfix."""

        commands = self.outcomes()
        commands[self.REGISTRY_PROBE] = completed((), returncode=6)
        host = FakeHost(commands=commands)
        report = EgressCheck().run(context(host))
        self.assertEqual(report.severity, Severity.PASS, report.detail)

    def test_declared_expected_status_counts_as_reachable_for_that_host_only(self) -> None:
        """A signed-URL CDN's 403 passes iff the allowlist entry declares it."""

        allowlist = EgressAllowlist(
            version=2,
            groups=(
                EgressGroup(
                    "host-provisioning",
                    (EgressHost("cdn.example", "https://cdn.example/", (403,)),),
                ),
                EgressGroup("install-upgrade", ()),
                EgressGroup("corpus", ()),
            ),
        )
        probe = probe_argv("https://cdn.example/")
        registry = self.REGISTRY_PROBE
        blocked = completed((), returncode=8, stderr="  HTTP/1.1 403 Forbidden\n")
        host = FakeHost(commands={probe: blocked, registry: completed(())})
        preflight_context = PreflightContext(
            host=host,
            lock=LOCK,
            models=MODELS,
            site=make_site(),
            egress=allowlist,
            courts=COURTS,
            no_gpu=False,
        )
        report = EgressCheck().run(preflight_context)
        self.assertEqual(report.severity, Severity.PASS, report.detail)

    def test_proxy_block_page_is_a_miss_naming_the_status(self) -> None:
        """A 403 block page is not reachability — the download would fail."""

        commands = self.outcomes()
        commands[probe_argv("https://a.example/")] = completed(
            (), returncode=8, stderr="  HTTP/1.1 403 Forbidden\n"
        )
        host = FakeHost(commands=commands)
        report = EgressCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("a.example", report.detail)
        self.assertIn("403", report.detail)

    def test_every_missed_host_is_named(self) -> None:
        host = FakeHost(commands=self.outcomes(a=4, b=5))
        report = EgressCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("a.example", report.detail)
        self.assertIn("b.example", report.detail)
        self.assertIn("egress_proxy", report.fix)
        self.assertIn("docs/runbooks/release-files.md §5", report.fix)

    def test_corpus_group_is_not_probed_pre_install(self) -> None:
        host = FakeHost(commands=self.outcomes())
        EgressCheck().run(context(host))
        probed = [call for call in host.calls if call[0] == "run"]
        self.assertNotIn(("run", probe_argv("https://c.example/")), probed)

    def test_loopback_registry_is_probed_over_http(self) -> None:
        site = make_site(extra="registry: 127.0.0.1:5000\n")
        probe = probe_argv("http://127.0.0.1:5000/v2/")
        host = FakeHost(
            commands={
                probe_argv("https://a.example/"): completed(()),
                probe_argv("https://b.example/"): completed(()),
                probe: completed(()),
            }
        )
        report = EgressCheck().run(context(host, site=site))
        self.assertEqual(report.severity, Severity.PASS, report.detail)
        self.assertIn(("run", probe), host.calls)

    def test_libvirt_bridge_registry_is_probed_over_http(self) -> None:
        site = make_site(extra="registry: 192.168.122.1:5000\n")
        probe = probe_argv("http://192.168.122.1:5000/v2/")
        host = FakeHost(
            commands={
                probe_argv("https://a.example/"): completed(()),
                probe_argv("https://b.example/"): completed(()),
                probe: completed(()),
            }
        )
        report = EgressCheck().run(context(host, site=site))
        self.assertEqual(report.severity, Severity.PASS, report.detail)
        self.assertIn(("run", probe), host.calls)

    def test_proxy_auth_travels_by_wgetrc_never_argv(self) -> None:
        site = make_site(extra="egress_proxy: http://proxy.test:3128\n")
        host = FakeHost(files={"/etc/gideon/secrets/proxy_auth": "alice:s3cret\n"})
        report = EgressCheck().run(context(host, site=site))
        self.assertEqual(report.severity, Severity.REFUSE)  # probes unmapped -> misses

        writes = [call for call in host.calls if call[0] == "write_text"]
        self.assertEqual(len(writes), 1)
        path, text, mode = cast(tuple[str, str, int], writes[0][1])
        self.assertTrue(str(path).startswith("/run/"))
        self.assertEqual(mode, 0o600)
        self.assertIn("proxy_user = alice", text)
        self.assertIn("proxy_password = s3cret", text)
        self.assertIn(("unlink", str(path)), host.calls)

        for call in host.calls:
            if call[0] == "run":
                argv = call[1]
                assert isinstance(argv, tuple)
                self.assertTrue(all("s3cret" not in word for word in argv), argv)
                if argv and argv[0] == "env":
                    self.assertTrue(argv[1].startswith("WGETRC=/run/"))

    def test_malformed_proxy_auth_refuses(self) -> None:
        host = FakeHost(files={"/etc/gideon/secrets/proxy_auth": "no-colon\n"})
        report = EgressCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("proxy_auth", report.detail + report.fix)


class Ports(unittest.TestCase):
    """Required ports are free or held by marked GIDEON containers."""

    SS = ("ss", "-ltnp")

    def proxy_host(
        self, rows: Mapping[int, str], *, failed: Collection[int] = ()
    ) -> FakeHost:
        lines = "".join(
            f'LISTEN 0 128 127.0.0.1:{port} 0.0.0.0:* '
            f'users:(("docker-proxy",pid={port},fd=4))\n'
            for port in rows
        )
        commands: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {
            self.SS: completed(self.SS, lines)
        }
        for port, stdout in rows.items():
            argv = cotenants.publish_ps_argv(port)
            commands[argv] = completed(argv, stdout, 1 if port in failed else 0)
        return FakeHost(commands=commands)

    def report(self, stdout: str, returncode: int = 0):
        host = FakeHost(commands={self.SS: completed(self.SS, stdout, returncode)})
        return PortsCheck().run(context(host))

    def test_both_free_passes(self) -> None:
        report = self.report('LISTEN 0 128 127.0.0.1:22 0.0.0.0:* users:(("sshd",pid=1,fd=3))\n')
        self.assertEqual(report.severity, Severity.PASS)

    def test_expected_owners_pass(self) -> None:
        host = self.proxy_host(
            {
                443: "gideon-caddy-1\tgideon\n",
                5000: "gideon-registry\t\n",
                9090: "gideon-prometheus-1\tgideon\n",
            }
        )
        report = PortsCheck().run(context(host))
        self.assertEqual(report.severity, Severity.PASS, report.detail)
        self.assertIn("443 held by container gideon-caddy-1 (project gideon)", report.detail)
        self.assertIn("5000 held by container gideon-registry (no Compose project)", report.detail)
        self.assertIn("9090 held by container gideon-prometheus-1 (project gideon)", report.detail)
        for port in (443, 5000, 9090):
            self.assertEqual(host.calls.count(("run", cotenants.publish_ps_argv(port))), 1)

    def test_prometheus_port_expected_owner_passes(self) -> None:
        host = self.proxy_host({9090: "gideon-prometheus-1\tgideon\n"})
        report = PortsCheck().run(context(host))
        self.assertEqual(report.severity, Severity.PASS, report.detail)

    def test_prometheus_port_stranger_refuses(self) -> None:
        host = self.proxy_host({9090: "other-app-monitor-1\tother-app\n"})
        report = PortsCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertEqual(report.detail, "port 9090 held by project other-app (other-app-monitor-1)")

    def test_docker_proxy_for_a_stranger_container_refuses(self) -> None:
        host = self.proxy_host({5000: "other-app-registry-1\tother-app\n"})
        report = PortsCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertEqual(report.detail, "port 5000 held by project other-app (other-app-registry-1)")

    def test_unmarked_container_named_with_caddy_refuses(self) -> None:
        host = self.proxy_host({443: "other-app-caddy-1\tother-app\n"})
        report = PortsCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertEqual(report.detail, "port 443 held by project other-app (other-app-caddy-1)")
        self.assertIn("publish the container on another port", report.fix)

    def test_unlabelled_container_named_with_caddy_refuses(self) -> None:
        host = self.proxy_host({443: "other-app-caddy-1\t\n"})
        report = PortsCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertEqual(report.detail, "port 443 held by no Compose project (other-app-caddy-1)")

    def test_marked_project_passes_with_any_container_name(self) -> None:
        host = self.proxy_host({443: "container\tgideon-ci\n"})
        report = PortsCheck().run(context(host))
        self.assertEqual(report.severity, Severity.PASS)
        self.assertIn("443 held by container container (project gideon-ci)", report.detail)

    def test_two_publishing_containers_pass_and_name_each_when_marked(self) -> None:
        host = self.proxy_host({443: "container-one\tgideon\ncontainer-two\tgideon-ci\n"})
        report = PortsCheck().run(context(host))
        self.assertEqual(report.severity, Severity.PASS)
        self.assertIn("container container-one (project gideon)", report.detail)
        self.assertIn("container container-two (project gideon-ci)", report.detail)

    def test_two_publishing_containers_refuse_only_unmarked_holder(self) -> None:
        host = self.proxy_host({443: "gideon-caddy-1\tgideon\nother-app-ingress-1\tother-app\n"})
        report = PortsCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertEqual(report.detail, "port 443 held by project other-app (other-app-ingress-1)")

    def test_failed_docker_read_refuses_with_repair(self) -> None:
        host = self.proxy_host({443: "gideon-caddy-1\tgideon\n"}, failed=(443,))
        report = PortsCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertEqual(
            report.detail,
            "port 443 held by docker-proxy, and docker ps could not list the container publishing it",
        )
        self.assertIn("Repair docker ps", report.fix)

    def test_empty_docker_read_refuses_with_repair(self) -> None:
        host = self.proxy_host({443: ""})
        report = PortsCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertEqual(
            report.detail,
            "port 443 held by docker-proxy, and no running container publishes it",
        )
        self.assertIn("Repair docker ps", report.fix)

    def test_bare_caddy_process_refuses(self) -> None:
        report = self.report(
            'LISTEN 0 128 0.0.0.0:443 0.0.0.0:* users:(("caddy",pid=2,fd=3))\n'
        )
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertEqual(report.detail, "port 443 held by caddy")

    def test_mixed_holder_kinds_keep_both_fixes(self) -> None:
        host = self.proxy_host({443: ""})
        host.commands[self.SS] = completed(
            self.SS,
            'LISTEN 0 128 0.0.0.0:443 0.0.0.0:* users:(("docker-proxy",pid=2,fd=3))\n'
            'LISTEN 0 128 0.0.0.0:5000 0.0.0.0:* users:(("python3",pid=3,fd=4))\n',
        )
        report = PortsCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("port 5000 held by python3", report.detail)
        self.assertIn("Stop the process or container", report.fix)
        self.assertIn("Repair docker ps", report.fix)

    def test_unexpected_holder_refuses_and_is_named(self) -> None:
        report = self.report(
            'LISTEN 0 128 0.0.0.0:5000 0.0.0.0:* users:(("python3",pid=9,fd=5))\n'
        )
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("python3", report.detail)
        self.assertIn("5000", report.detail)
        self.assertTrue(report.fix)

    def test_ss_failure_refuses(self) -> None:
        report = self.report("", returncode=1)
        self.assertEqual(report.severity, Severity.REFUSE)


class Hostname(unittest.TestCase):
    GETENT = ("getent", "hosts", "gideon.test")

    def test_resolving_hostname_passes(self) -> None:
        host = FakeHost(commands={self.GETENT: completed(self.GETENT, "192.0.2.5 gideon.test\n")})
        report = HostnameCheck().run(context(host))
        self.assertEqual(report.severity, Severity.PASS)

    def test_unresolvable_hostname_refuses_with_fix(self) -> None:
        host = FakeHost(commands={self.GETENT: completed(self.GETENT, "", 2)})
        report = HostnameCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("gideon.test", report.detail)
        self.assertIn("docs/runbooks/office-services-setup.md §2", report.fix)


class Ntp(unittest.TestCase):
    TIMEDATECTL = ("timedatectl", "show", "-p", "NTPSynchronized", "--value")

    def test_synchronized_passes(self) -> None:
        host = FakeHost(commands={self.TIMEDATECTL: completed(self.TIMEDATECTL, "yes\n")})
        self.assertEqual(NtpCheck().run(context(host)).severity, Severity.PASS)

    def test_unsynchronized_warns_not_refuses(self) -> None:
        """An unsynchronized NTP clock produces a warning."""

        host = FakeHost(commands={self.TIMEDATECTL: completed(self.TIMEDATECTL, "no\n")})
        report = NtpCheck().run(context(host))
        self.assertEqual(report.severity, Severity.WARN)
        self.assertTrue(report.fix)


class DataVolume(unittest.TestCase):
    DF = DATA_DF_ARGV

    def host(self, size: int | None, available: int = 10_000) -> FakeHost:
        if size is None:
            return FakeHost(commands={self.DF: completed(self.DF, "", 1)})
        return FakeHost(commands={self.DF: completed(self.DF, f" Size Avail\n{size} {available}\n")})

    def test_one_byte_below_floor_refuses_with_raw_and_rounded_size(self) -> None:
        floor = MODELS.profiles[0].requires.data_volume_gb * GIGABYTE
        size = floor - 1
        report = DataVolumeCheck().run(context(self.host(size)))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn(f"{size} bytes", report.detail)
        self.assertIn(format_gb(size), report.detail)
        self.assertIn(f"{MODELS.profiles[0].requires.data_volume_gb} GB", report.detail)
        self.assertIn(f"{MODELS.profiles[0].requires.data_volume_gb} GB", report.fix)
        self.assertIn(" GB at /data, or set", report.fix)
        self.assertIn("hardware_profile", report.fix)

    def test_at_floor_passes_with_size_free_bytes_and_floor(self) -> None:
        floor = MODELS.profiles[0].requires.data_volume_gb * GIGABYTE
        available = 123 * GIGABYTE
        report = DataVolumeCheck().run(context(self.host(floor, available)))
        self.assertEqual(report.severity, Severity.PASS, report.detail)
        self.assertIn(format_gb(floor), report.detail)
        self.assertIn(format_gb(available), report.detail)
        self.assertIn(str(MODELS.profiles[0].requires.data_volume_gb), report.detail)

    def test_unknown_profile_refuses_before_df(self) -> None:
        site = make_site(extra="hardware_profile: missing-profile\n")
        host = self.host(MODELS.profiles[0].requires.data_volume_gb * GIGABYTE)
        report = DataVolumeCheck().run(context(host, site=site))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("hardware_profile", report.fix)
        self.assertNotIn(("run", self.DF), host.calls)

    def test_unmeasurable_volume_refuses_on_both_host_kinds(self) -> None:
        for no_gpu in (False, True):
            with self.subTest(no_gpu=no_gpu):
                report = DataVolumeCheck().run(context(self.host(None), no_gpu=no_gpu))
                self.assertEqual(report.severity, Severity.REFUSE)
                self.assertIn("/data", report.detail + report.fix)

    def test_no_gpu_skips_floor_judgment_after_measuring_volume(self) -> None:
        floor = MODELS.profiles[0].requires.data_volume_gb * GIGABYTE
        size = floor - 1
        host = self.host(size)
        report = DataVolumeCheck().run(context(host, no_gpu=True))
        self.assertEqual(report.severity, Severity.PASS, report.detail)
        self.assertIn("skipped: no-GPU host", report.detail)
        self.assertIn(format_gb(size), report.detail)
        self.assertEqual(report.fix, "")

    def test_lifted_df_parser_reads_the_last_measurement_row(self) -> None:
        self.assertEqual(parse_size_and_available(" Size Avail\n123 45\n"), (123, 45))

    def test_lifted_df_parser_refuses_malformed_or_negative_values(self) -> None:
        for output in ("", "Size Avail\n", "Size Avail\n1 two\n", "Size Avail\n-1 2\n"):
            with self.subTest(output=output):
                self.assertIsNone(parse_size_and_available(output))


class MeminfoParser(unittest.TestCase):
    def test_reads_both_kilobyte_fields(self) -> None:
        output = "  MemTotal:\t12345 kB\nMemAvailable: 6789 kB\n"
        self.assertEqual(meminfo_kb(output, "MemTotal"), 12345)
        self.assertEqual(meminfo_kb(output, "MemAvailable"), 6789)

    def test_absent_or_malformed_field_is_unreadable(self) -> None:
        for output in ("MemFree: 12 kB\n", "MemAvailable: unknown kB\n", "MemAvailable: 12 MB\n"):
            with self.subTest(output=output):
                self.assertIsNone(meminfo_kb(output, "MemAvailable"))


def prepared_low(reading: LowReading) -> LowReader:
    def read() -> LowReading:
        return reading

    return read


class HostMemory(unittest.TestCase):
    TOTAL_KB = 10_000_000
    FLOOR_BYTES = int(TOTAL_KB * 1024 * HOST_MEMORY_FLOOR_FRACTION)

    def host(self, available_kb: int) -> FakeHost:
        return FakeHost(
            files={MEMINFO: f"MemTotal: {self.TOTAL_KB} kB\nMemAvailable: {available_kb} kB\n"}
        )

    def test_current_reading_below_warns_and_above_passes_with_figures(self) -> None:
        for available_kb, expected in ((1_000_000, Severity.WARN), (4_000_000, Severity.PASS)):
            with self.subTest(available_kb=available_kb):
                host = self.host(available_kb)
                low = self.FLOOR_BYTES + 1
                report = HostMemoryCheck(prepared_low(LowReading(low))).run(context(host))
                self.assertEqual(report.severity, expected, report.detail)
                self.assertIn(format_gb(available_kb * 1024), report.detail)
                self.assertIn(format_gb(low), report.detail)
                self.assertIn("at its fourteen-day low on Prometheus", report.detail)
                self.assertIn(format_gb(self.FLOOR_BYTES), report.detail)
                self.assertIn(format_gb(self.TOTAL_KB * 1024), report.detail)
                self.assertEqual(host.calls, [("read_text", MEMINFO)])
                if expected is Severity.WARN:
                    self.assertIn("the current reading is below", report.detail)
                    self.assertIn("container-memory panel", report.fix)
                else:
                    self.assertEqual(report.fix, "")

    def test_fourteen_day_low_below_warns_while_current_is_above(self) -> None:
        low = self.FLOOR_BYTES - 1
        report = HostMemoryCheck(prepared_low(LowReading(low))).run(context(self.host(4_000_000)))
        self.assertEqual(report.severity, Severity.WARN, report.detail)
        self.assertIn("the fourteen-day low is below", report.detail)
        self.assertIn("at its fourteen-day low on Prometheus", report.detail)
        self.assertIn(format_gb(low), report.detail)
        self.assertIn(format_gb(self.FLOOR_BYTES), report.detail)

    def test_both_readings_below_name_both(self) -> None:
        report = HostMemoryCheck(prepared_low(LowReading(self.FLOOR_BYTES - 1))).run(
            context(self.host(1_000_000))
        )
        self.assertEqual(report.severity, Severity.WARN, report.detail)
        self.assertIn("both readings are below", report.detail)

    def test_low_one_byte_either_side_of_floor(self) -> None:
        for low, expected in (
            (self.FLOOR_BYTES - 1, Severity.WARN),
            (self.FLOOR_BYTES, Severity.PASS),
            (self.FLOOR_BYTES + 1, Severity.PASS),
        ):
            with self.subTest(low=low):
                report = HostMemoryCheck(prepared_low(LowReading(low))).run(context(self.host(4_000_000)))
                self.assertEqual(report.severity, expected, report.detail)

    def test_each_missing_low_reason_follows_current_reading(self) -> None:
        address = PROMETHEUS_LOOPBACK_ADDRESS
        reasons = (
            f"Prometheus did not answer at {address}",
            f"Prometheus timed out at {address}",
            f"Prometheus returned HTTP 503 at {address}",
            f"Prometheus returned unreadable JSON at {address}",
            f"Prometheus returned a non-success status at {address}",
            f"no fourteen-day low on Prometheus at {address} yet",
            f"Prometheus returned an unreadable value at {address}",
        )
        for reason in reasons:
            for available_kb, expected in ((1_000_000, Severity.WARN), (4_000_000, Severity.PASS)):
                with self.subTest(reason=reason, available_kb=available_kb):
                    report = HostMemoryCheck(prepared_low(LowReading(None, reason))).run(
                        context(self.host(available_kb))
                    )
                    self.assertEqual(report.severity, expected, report.detail)
                    self.assertIn(f"fourteen-day low not read: {reason}", report.detail)
                    self.assertIn(format_gb(self.FLOOR_BYTES), report.detail)
                    self.assertNotEqual(report.severity, Severity.REFUSE)

    def test_unreadable_or_missing_meminfo_warns_without_querying_low(self) -> None:
        cases = (
            (FakeHost(), "/proc/meminfo"),
            (FakeHost(files={MEMINFO: "MemAvailable: 1 kB\n"}), "MemTotal"),
            (FakeHost(files={MEMINFO: "MemTotal: 1 kB\n"}), "MemAvailable"),
        )
        for host, detail in cases:
            with self.subTest(detail=detail):
                reader = mock.Mock(return_value=LowReading(self.FLOOR_BYTES))
                report = HostMemoryCheck(reader).run(context(host))
                self.assertEqual(report.severity, Severity.WARN, report.detail)
                self.assertIn(detail, report.detail)
                self.assertIn("Restore /proc/meminfo", report.fix)
                reader.assert_not_called()

    def test_no_gpu_host_is_judged(self) -> None:
        report = HostMemoryCheck(prepared_low(LowReading(self.FLOOR_BYTES + 1))).run(
            context(self.host(1_000_000), no_gpu=True)
        )
        self.assertEqual(report.severity, Severity.WARN, report.detail)
        self.assertNotIn("skipped", report.detail)

    def test_fraction_matches_shared_host_ram_agreement(self) -> None:
        if absent_from_export("docs/box-ledger.md", ROOT):
            self.skipTest("shared host RAM record is absent from the exported tree")
        text = (ROOT / "docs/box-ledger.md").read_text(encoding="utf-8")
        paragraph = text.split("**Host RAM.**", 1)[1].split("\n\n", 1)[0]
        host = re.search(r"The host has ([0-9]+(?:\.[0-9]+)?) GB", paragraph)
        floor = re.search(r"a floor of ([0-9]+(?:\.[0-9]+)?) GB, a fifth of the host\b", paragraph)
        self.assertIsNotNone(host)
        self.assertIsNotNone(floor)
        assert host is not None and floor is not None
        host_gb = Fraction(host.group(1))
        floor_gb = Fraction(floor.group(1))
        self.assertEqual(HOST_MEMORY_FLOOR_FRACTION, Fraction(1, 5))
        self.assertEqual(int(host_gb * HOST_MEMORY_FLOOR_FRACTION), floor_gb)
        self.assertIn(f"`{MEMORY_LOW_QUERY}`", paragraph)

    def test_floor_is_exact_at_the_reference_host(self) -> None:
        total_kb = 259_277_344  # 265.5 GB, rounded up to a whole kB
        host = FakeHost(files={MEMINFO: f"MemTotal: {total_kb} kB\nMemAvailable: {total_kb // 2} kB\n"})
        report = HostMemoryCheck(prepared_low(LowReading(None, "not asked"))).run(context(host))
        self.assertEqual(report.severity, Severity.PASS, report.detail)
        self.assertIn("floor 53.1 GB (a fifth of 265.5 GB)", report.detail)


class PrometheusReader(unittest.TestCase):
    def read_reply(
        self,
        *,
        body: bytes | None = None,
        status: int = 200,
        failure: OSError | None = None,
    ) -> tuple[LowReading, dict[str, object]]:
        if body is None:
            body = json.dumps(
                {"status": "success", "data": {"resultType": "vector", "result": [{"value": [0, "1000000000.5"]}]}}
            ).encode()
        observed: dict[str, object] = {}

        class Response:
            def __init__(self) -> None:
                self.status = status

            def read(self) -> bytes:
                assert body is not None
                return body

        class Connection:
            def __init__(self, host: str, *, timeout: float) -> None:
                observed["host"] = host
                observed["timeout"] = timeout

            def request(self, method: str, target: str) -> None:
                observed["request"] = (method, target)
                if failure is not None:
                    raise failure

            def getresponse(self) -> Response:
                return Response()

            def close(self) -> None:
                observed["closed"] = True

        with mock.patch.object(capacity.http.client, "HTTPConnection", Connection):
            reading = read_prometheus_low()
        return reading, observed

    def test_success_uses_loopback_query_and_integer_bytes(self) -> None:
        reading, observed = self.read_reply()
        self.assertEqual(reading, LowReading(1_000_000_000))
        self.assertEqual(observed["host"], PROMETHEUS_LOOPBACK_ADDRESS)
        self.assertEqual(observed["timeout"], PROMETHEUS_TIMEOUT_SECONDS)
        method, target = cast(tuple[str, str], observed["request"])
        self.assertEqual(method, "GET")
        parsed = urlsplit(target)
        self.assertEqual(parsed.path, PROMETHEUS_QUERY_PATH)
        self.assertEqual(parse_qs(parsed.query), {"query": [MEMORY_LOW_QUERY]})
        self.assertEqual(observed["closed"], True)

    def test_connection_refusal_and_timeout_return_reasons(self) -> None:
        for failure, phrase in (
            (ConnectionRefusedError("down"), "did not answer"),
            (OSError("network unavailable"), "did not answer"),
            (TimeoutError("hung"), "timed out"),
        ):
            with self.subTest(failure=type(failure).__name__):
                reading, observed = self.read_reply(failure=failure)
                self.assertIsNone(reading.bytes)
                self.assertIn(phrase, reading.reason)
                self.assertIn(PROMETHEUS_LOOPBACK_ADDRESS, reading.reason)
                self.assertEqual(observed["closed"], True)

    def test_http_status_and_unreadable_body_return_reasons(self) -> None:
        for status, body, phrase in (
            (503, b"", "HTTP 503"),
            (200, b"not json", "unreadable JSON"),
            (200, b"[]", "unreadable result"),
        ):
            with self.subTest(status=status, body=body):
                reading, observed = self.read_reply(status=status, body=body)
                self.assertIsNone(reading.bytes)
                self.assertIn(phrase, reading.reason)
                self.assertIn(PROMETHEUS_LOOPBACK_ADDRESS, reading.reason)
                self.assertEqual(observed["closed"], True)

    def test_non_success_empty_vector_and_nonfinite_value_return_reasons(self) -> None:
        for document, phrase in (
            ({"status": "error"}, "non-success status"),
            ({"status": "success", "data": {"resultType": "vector", "result": []}}, "no fourteen-day low"),
            ({"status": "success", "data": {"resultType": "vector", "result": [{"value": [0, "NaN"]}]}}, "unreadable value"),
            ({"status": "success", "data": {"resultType": "vector", "result": [{"value": [0, "Infinity"]}]}}, "unreadable value"),
        ):
            with self.subTest(document=document):
                reading, _ = self.read_reply(body=json.dumps(document).encode())
                self.assertIsNone(reading.bytes)
                self.assertIn(phrase, reading.reason)
                self.assertIn(PROMETHEUS_LOOPBACK_ADDRESS, reading.reason)

    def test_proxy_environment_cannot_redirect_loopback_query(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"HTTP_PROXY": "http://proxy.example:3128", "http_proxy": "http://proxy.example:3128"},
        ):
            reading, observed = self.read_reply()
        self.assertIsNotNone(reading.bytes)
        self.assertEqual(observed["host"], PROMETHEUS_LOOPBACK_ADDRESS)
        _, target = cast(tuple[str, str], observed["request"])
        self.assertEqual(parse_qs(urlsplit(target).query), {"query": [MEMORY_LOW_QUERY]})


class FormatGb(unittest.TestCase):
    def test_renders_decimal_gigabytes_with_one_decimal(self) -> None:
        self.assertEqual(format_gb(265_449_404_416), "265.4 GB")
        self.assertEqual(format_gb(0), "0.0 GB")

    def test_rounding_alone_would_hide_a_one_byte_shortfall(self) -> None:
        """Why every figure refusal prints the raw native-unit value beside the rounded one."""

        self.assertEqual(format_gb(4_000 * GIGABYTE - 1), "4000.0 GB")


def ldap_argv(site, group: str) -> tuple[str, ...]:
    ldap = site.auth.ldap
    return tuple(
        ldapsearch_argv(
            ldap.host,
            ldap.port,
            ldap.bind_user,
            f"CN={group},CN=Users,{ldap.search_base}",
            "(objectClass=group)",
            ("dn", "member"),
            scope="base",
        )
    )


def member_of_argv(site) -> tuple[str, ...]:
    ldap = site.auth.ldap
    return tuple(
        ldapsearch_argv(
            ldap.host,
            ldap.port,
            ldap.bind_user,
            ldap.search_base,
            f"(memberOf=CN=GIDEON-Users,CN=Users,{ldap.search_base})",
            ("sAMAccountName", "userPrincipalName"),
        )
    )


def group_ldif(group: str, *members: str) -> str:
    lines = [f"dn: CN={group},DC=ad,DC=test"] + [f"member: CN={member},OU=People,DC=ad,DC=test" for member in members]
    return "\n".join(lines) + "\n"


def member_of_ldif(*members: tuple[str, str | None, str | None]) -> str:
    records = []
    for account, samaccountname, upn in members:
        lines = [f"dn: CN={account},OU=People,DC=ad,DC=test"]
        if samaccountname is not None:
            lines.append(f"sAMAccountName: {samaccountname}")
        if upn is not None:
            lines.append(f"userPrincipalName: {upn}")
        records.append("\n".join(lines))
    return "\n\n".join(records) + "\n"


class Ldap(unittest.TestCase):
    PASSWORD: ClassVar[dict[str, str]] = {
        "/etc/gideon/secrets/ldap_bind_password": "hunter2\n"
    }

    def test_missing_password_file_refuses_naming_it(self) -> None:
        report = LdapCheck().run(context(FakeHost()))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("ldap_bind_password", report.detail + report.fix)

    def test_both_groups_resolving_passes_and_password_stays_out_of_argv(self) -> None:
        site = make_site()
        commands = {
            ldap_argv(site, "GIDEON-Users"): completed((), group_ldif("GIDEON-Users", "alice", "bob")),
            ldap_argv(site, "GIDEON-Admins"): completed((), group_ldif("GIDEON-Admins", "alice")),
            member_of_argv(site): completed(
                (),
                member_of_ldif(
                    ("alice", "alice", "alice@ad.test"),
                    ("bob", "bob", "bob@ad.test"),
                ),
            ),
        }
        host = FakeHost(commands=commands, files=dict(self.PASSWORD))
        report = LdapCheck().run(context(host, site=site))
        self.assertEqual(report.severity, Severity.PASS, report.detail)
        self.assertEqual(
            report.detail,
            "LDAP bind, all configured GIDEON groups, and 2 users-group member(s) resolved, each with a userPrincipalName",
        )
        for call in host.calls:
            if call[0] == "run":
                argv = call[1]
                assert isinstance(argv, tuple)
                self.assertNotIn("hunter2", argv)
                if "ldapsearch" in argv:
                    self.assertEqual(argv[0], "env")
                    self.assertEqual(argv[1], "LDAPTLS_CACERT=/etc/gideon/ca.pem")

    def test_invisible_member_of_refuses_with_the_directory_permission_fix(self) -> None:
        site = make_site()
        commands = {
            ldap_argv(site, "GIDEON-Users"): completed((), group_ldif("GIDEON-Users", "alice", "bob")),
            ldap_argv(site, "GIDEON-Admins"): completed((), group_ldif("GIDEON-Admins", "alice")),
            member_of_argv(site): completed((), ""),
        }
        report = LdapCheck().run(context(FakeHost(commands=commands, files=dict(self.PASSWORD)), site=site))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("memberOf", report.detail)
        self.assertIn("2 member(s)", report.detail)
        self.assertIn("Pre-Windows 2000 Compatible Access", report.fix)
        self.assertIn(site.auth.ldap.bind_user, report.fix)

    def test_partial_member_of_refuses_with_the_invisible_dns_and_scope_fix(self) -> None:
        site = make_site()
        commands = {
            ldap_argv(site, "GIDEON-Users"): completed((), group_ldif("GIDEON-Users", "alice", "bob")),
            ldap_argv(site, "GIDEON-Admins"): completed((), group_ldif("GIDEON-Admins", "alice")),
            member_of_argv(site): completed(
                (), member_of_ldif(("alice", "alice", "alice@ad.test"))
            ),
        }
        report = LdapCheck().run(
            context(FakeHost(commands=commands, files=dict(self.PASSWORD)), site=site)
        )
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("1 of 2", report.detail)
        self.assertIn("CN=bob,OU=People,DC=ad,DC=test", report.detail)
        self.assertIn("memberOf", report.fix)
        self.assertIn("search_base", report.fix)

    def test_missing_upns_refuse_naming_account_and_dn_with_the_aduc_fix(self) -> None:
        site = make_site()
        commands = {
            ldap_argv(site, "GIDEON-Users"): completed(
                (), group_ldif("GIDEON-Users", "alice", "bob", "nested")
            ),
            ldap_argv(site, "GIDEON-Admins"): completed((), group_ldif("GIDEON-Admins", "alice")),
            member_of_argv(site): completed(
                (),
                member_of_ldif(
                    ("alice", "alice", "alice@ad.test"),
                    ("bob", "bob", None),
                    ("nested", None, None),
                ),
            ),
        }
        report = LdapCheck().run(
            context(FakeHost(commands=commands, files=dict(self.PASSWORD)), site=site)
        )
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn(
            "users-group member(s) without a userPrincipalName: bob, "
            "CN=nested,OU=People,DC=ad,DC=test",
            report.detail,
        )
        self.assertIn("userPrincipalName", report.fix)
        self.assertIn("ADUC", report.fix)

    def test_invisible_member_refuses_before_a_visible_member_without_a_upn(self) -> None:
        site = make_site()
        commands = {
            ldap_argv(site, "GIDEON-Users"): completed(
                (), group_ldif("GIDEON-Users", "alice", "bob", "carol")
            ),
            ldap_argv(site, "GIDEON-Admins"): completed((), group_ldif("GIDEON-Admins", "alice")),
            member_of_argv(site): completed(
                (),
                member_of_ldif(("alice", "alice", "alice@ad.test"), ("bob", "bob", None)),
            ),
        }
        report = LdapCheck().run(
            context(FakeHost(commands=commands, files=dict(self.PASSWORD)), site=site)
        )
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("cannot see 1 of 3", report.detail)
        self.assertIn("CN=carol,OU=People,DC=ad,DC=test", report.detail)
        self.assertNotIn("userPrincipalName", report.detail)

    def test_empty_users_group_warns(self) -> None:
        site = make_site()
        commands = {
            ldap_argv(site, "GIDEON-Users"): completed((), group_ldif("GIDEON-Users")),
            ldap_argv(site, "GIDEON-Admins"): completed((), group_ldif("GIDEON-Admins")),
        }
        host = FakeHost(commands=commands, files=dict(self.PASSWORD))
        report = LdapCheck().run(context(host, site=site))
        self.assertEqual(report.severity, Severity.WARN)
        self.assertIn("no members", report.detail)
        self.assertFalse(any(call[1] == member_of_argv(site) for call in host.calls if call[0] == "run"))

    def test_missing_group_refuses_naming_it(self) -> None:
        site = make_site()
        commands = {
            ldap_argv(site, "GIDEON-Users"): completed((), group_ldif("GIDEON-Users", "alice")),
            ldap_argv(site, "GIDEON-Admins"): completed((), ""),
        }
        host = FakeHost(commands=commands, files=dict(self.PASSWORD))
        report = LdapCheck().run(context(host, site=site))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("GIDEON-Admins", report.detail)

    def test_bind_failure_refuses_with_checklist_fix(self) -> None:
        host = FakeHost(files=dict(self.PASSWORD))
        report = LdapCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("docs/runbooks/office-services-setup.md §1", report.fix)


SSH_BASE = (
    "ssh",
    "-i",
    "/etc/gideon/secrets/backup_ssh_key",
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "UserKnownHostsFile=/etc/gideon/backup_known_hosts",
    "-o",
    "ConnectTimeout=10",
    "gideon-backup@nas.test",
)
SSH_CONNECT = SSH_BASE + ("true",)
SSH_PROBE = SSH_BASE + (
    (
        "sh -c 'probe_dir=/volume1/backup; probe=\"$probe_dir/.gideon-preflight.$$\"; "
        '(umask 077; : > "$probe") && rm -f -- "$probe"\''
    ),
)
TOOLS_SCRIPT = (
    "probe_dir=/volume1/backup; probe=\"$probe_dir/.gideon-preflight-tools.$$\"; "
    "command -v rsync sha256sum >/dev/null "
    "&& printf 'gideon\\n' > \"$probe\" "
    f"&& printf '%s  %s\\n' {BACKUP_PROBE_SHA256} \"$probe\" | sha256sum -c - >/dev/null; "
    'rc=$?; rm -f -- "$probe"; exit $rc'
)
SSH_TOOLS = SSH_BASE + (f"sh -c {shlex.quote(TOOLS_SCRIPT)}",)


class BackupSsh(unittest.TestCase):
    def test_connect_failure_refuses_with_key_fix(self) -> None:
        host = FakeHost(commands={SSH_CONNECT: completed(SSH_CONNECT, "", 255)})
        report = BackupSshCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("docs/runbooks/office-services-setup.md §3", report.fix)

    def test_unwritable_path_refuses_with_path_fix(self) -> None:
        host = FakeHost(
            commands={
                SSH_CONNECT: completed(SSH_CONNECT),
                SSH_PROBE: completed(SSH_PROBE, "", 1),
            }
        )
        report = BackupSshCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("/volume1/backup", report.detail)
        self.assertIn("docs/runbooks/office-services-setup.md §3", report.fix)

    def test_writable_path_passes(self) -> None:
        host = FakeHost(
            commands={
                SSH_CONNECT: completed(SSH_CONNECT),
                SSH_PROBE: completed(SSH_PROBE),
                SSH_TOOLS: completed(SSH_TOOLS),
            }
        )
        report = BackupSshCheck().run(context(host))
        self.assertEqual(report.severity, Severity.PASS, report.detail)

    def test_tools_probe_uses_exact_argv_and_passes(self) -> None:
        host = FakeHost(
            commands={
                SSH_CONNECT: completed(SSH_CONNECT),
                SSH_PROBE: completed(SSH_PROBE),
                SSH_TOOLS: completed(SSH_TOOLS),
            }
        )
        report = BackupSshCheck().run(context(host))
        self.assertEqual(report.severity, Severity.PASS)
        self.assertEqual(host.calls[-1], ("run", SSH_TOOLS))

    def test_tools_probe_failure_refuses_with_runbook_fix(self) -> None:
        host = FakeHost(
            commands={
                SSH_CONNECT: completed(SSH_CONNECT),
                SSH_PROBE: completed(SSH_PROBE),
                SSH_TOOLS: completed(SSH_TOOLS, "", 1),
            }
        )
        report = BackupSshCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertEqual(
            report.detail, "backup target lacks rsync or a working sha256sum -c"
        )
        self.assertEqual(
            report.fix,
            "Install rsync and coreutils sha256sum on the backup target per "
            "docs/runbooks/office-services-setup.md §3, then re-run preflight.",
        )


class Smtp(unittest.TestCase):
    def transport_recorder(self, sent: list[tuple[object, ...]], *, fail: bool = False):
        def transport(*args: object) -> None:
            if fail:
                raise ConnectionRefusedError("relay said no")
            sent.append(args)

        return transport

    def test_message_without_auth_when_no_password_file(self) -> None:
        sent: list[tuple[object, ...]] = []
        report = SmtpCheck(self.transport_recorder(sent)).run(context(FakeHost()))
        self.assertEqual(report.severity, Severity.PASS)
        host, port, sender, recipients, subject, user, password = sent[0]
        self.assertEqual((host, port, sender), ("smtp.test", 25, "gideon@ad.test"))
        self.assertEqual(list(recipients), ["csa@ad.test"])  # type: ignore[call-overload]
        self.assertIn("gideon.test", str(subject))
        self.assertIsNone(user)
        self.assertIsNone(password)

    def test_auth_uses_the_site_user_and_secret(self) -> None:
        sent: list[tuple[object, ...]] = []
        site = make_site(smtp_user="relay-svc")
        host = FakeHost(files={"/etc/gideon/secrets/smtp_password": "pw\n"})
        report = SmtpCheck(self.transport_recorder(sent)).run(context(host, site=site))
        self.assertEqual(report.severity, Severity.PASS)
        self.assertEqual(sent[0][5], "relay-svc")
        self.assertEqual(sent[0][6], "pw")

    def test_password_without_user_refuses_naming_the_key(self) -> None:
        """Spec gap (e): the AUTH identity must be stated, never guessed."""

        host = FakeHost(files={"/etc/gideon/secrets/smtp_password": "pw\n"})
        report = SmtpCheck(self.transport_recorder([])).run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("alerts.smtp.user", report.detail + report.fix)

    def test_default_transport_refuses_credentials_without_starttls(self) -> None:
        """A relay that cannot upgrade to TLS never sees the AUTH password."""

        class ClearTextSmtp:
            def __init__(self, host: str, port: int, timeout: float) -> None:
                del host, port, timeout
                self.logins: list[tuple[str, str]] = []

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def ehlo(self) -> None:
                return None

            def has_extn(self, name: str) -> bool:
                del name
                return False

            def login(self, user: str, password: str) -> None:
                self.logins.append((user, password))

            def send_message(self, message: object) -> None:
                del message

        with (
            mock.patch.object(services.smtplib, "SMTP", ClearTextSmtp),
            self.assertRaises(RuntimeError) as raised,
        ):
            services.smtp_transport(
                "smtp.test", 25, "gideon@ad.test", ["csa@ad.test"],
                "subject", "relay-svc", "pw",
            )
        self.assertIn("STARTTLS", str(raised.exception))

    def test_transport_failure_refuses(self) -> None:
        report = SmtpCheck(self.transport_recorder([], fail=True)).run(context(FakeHost()))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("relay said no", report.detail)


class Jurisdiction(unittest.TestCase):
    def test_lockfile_courts_join_sources_in_sorted_order(self) -> None:
        paths = list(PREFLIGHT_LOCKFILES.glob("*.yaml"))
        self.assertEqual(len(paths), 1)
        loaded = load_lockfile(paths[0])
        self.assertTrue(loaded.ok, loaded.errors)
        assert loaded.lockfile is not None
        lockfile = loaded.lockfile
        pin = lockfile.sources["caselaw"]
        self.assertEqual(lockfile.courts, pin.courts)

        other = COURTS.ids_at_level("state_supreme")[0]
        joined = dataclasses.replace(
            lockfile,
            sources={
                "caselaw": dataclasses.replace(pin, courts=(other,)),
                "second": dataclasses.replace(pin, courts=(*lockfile.courts, other)),
            },
        )
        self.assertEqual(joined.courts, tuple(sorted({*lockfile.courts, other})))
        empty = dataclasses.replace(
            lockfile, sources={"caselaw": dataclasses.replace(pin, courts=None)}
        )
        self.assertEqual(empty.courts, ())

    def test_known_ids_pass(self) -> None:
        report = JurisdictionCheck().run(
            context(FakeHost(), site=make_jurisdiction_site(), courts=COURTS)
        )
        self.assertEqual(report.severity, Severity.PASS)
        self.assertIn("2 jurisdiction id(s) resolved", report.detail)
        self.assertIn("courts[] rules are not judged", report.detail)

    def test_no_lockfile_pass_names_the_unjudged_rules(self) -> None:
        report = JurisdictionCheck().run(
            context(FakeHost(), site=make_jurisdiction_site())
        )
        self.assertEqual(report.severity, Severity.PASS)
        self.assertIn("rules are not judged until a lockfile is installed", report.detail)

    def test_unknown_district_refuses_naming_it(self) -> None:
        site = make_jurisdiction_site(district="fx-distrct")
        report = JurisdictionCheck().run(context(FakeHost(), site=site))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("district 'fx-distrct' (nearest 'fx-district')", report.detail)

    def test_unknown_state_refuses_naming_it(self) -> None:
        """An unknown jurisdiction ID, including a state ID, refuses."""

        site = make_jurisdiction_site(states="fx-stte")
        report = JurisdictionCheck().run(context(FakeHost(), site=site))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("state 'fx-stte' (nearest 'fx-state')", report.detail)

    def test_every_bad_id_is_reported_together(self) -> None:
        site = make_jurisdiction_site(
            circuit="fx-circut", district="fx-distrct", states="fx-stte"
        )
        report = JurisdictionCheck().run(context(FakeHost(), site=site))
        self.assertEqual(report.severity, Severity.REFUSE)
        for identifier in ("fx-circut", "fx-distrct", "fx-stte"):
            self.assertIn(identifier, report.detail)

    def test_wrong_circuit_level_refuses_naming_both_levels(self) -> None:
        site = make_jurisdiction_site(circuit="fx-district")
        report = JurisdictionCheck().run(context(FakeHost(), site=site))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("circuit 'fx-district' is a district court, not a circuit", report.detail)

    def test_wrong_district_level_refuses_naming_both_levels(self) -> None:
        site = make_jurisdiction_site(district="fx-circuit")
        report = JurisdictionCheck().run(context(FakeHost(), site=site))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("district 'fx-circuit' is a circuit court, not a district", report.detail)

    def test_wrong_state_level_names_the_state_supreme_court(self) -> None:
        site = make_jurisdiction_site(states="fx-app")
        report = JurisdictionCheck().run(context(FakeHost(), site=site))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("state 'fx-app' is a state_appellate court, not a state_supreme", report.detail)
        self.assertIn("(its state's court of last resort: 'fx-state')", report.detail)

    def test_map_refusal_fix_names_the_site_key_and_map(self) -> None:
        site = make_jurisdiction_site(district="fx-missing")
        report = JurisdictionCheck().run(context(FakeHost(), site=site))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("/etc/gideon/site.yaml", report.fix)
        self.assertIn("courts.yaml lists every court id with its level", report.fix)
        self.assertTrue(report.fix.endswith("re-run preflight."))

    def test_state_without_lockfile_courts_warns_naming_the_derived_cut(self) -> None:
        """A known state absent from the lockfile produces a warning."""

        site = make_jurisdiction_site(states="fx-state")
        report = JurisdictionCheck().run(
            context(FakeHost(), site=site, lockfile={"fx-circuit", "fx-district"})
        )
        self.assertEqual(report.severity, Severity.WARN)
        self.assertIn(f"lockfile {LOCKFILE_LABEL}", report.detail)
        self.assertIn("state 'fx-state' (no appellate court", report.detail)
        self.assertIn(f"corpus cut --base {LOCKFILE_LABEL} --add-courts", report.fix)
        self.assertIn("state_supreme and state_appellate levels", report.fix)
        self.assertTrue(report.fix.endswith("re-run preflight."))

    def test_state_with_lockfile_courts_passes(self) -> None:
        site = make_jurisdiction_site(states="fx-state")
        report = JurisdictionCheck().run(
            context(
                FakeHost(), site=site,
                lockfile={"fx-circuit", "fx-district", "fx-app"},
            )
        )
        self.assertEqual(report.severity, Severity.PASS)
        self.assertIn(f"lockfile {LOCKFILE_LABEL}", report.detail)

    def test_missing_circuit_in_lockfile_warns_without_circuit_tier(self) -> None:
        site = make_jurisdiction_site(states="fx-state")
        report = JurisdictionCheck().run(
            context(FakeHost(), site=site, lockfile={"fx-district", "fx-app"})
        )
        self.assertEqual(report.severity, Severity.WARN)
        self.assertIn("circuit 'fx-circuit' (circuit tier is not in the lockfile)", report.detail)
        self.assertIn(f"--base {LOCKFILE_LABEL} --add-courts fx-circuit", report.fix)

    def test_missing_circuit_refuses_when_circuit_tier_is_carried(self) -> None:
        site = make_jurisdiction_site()
        report = JurisdictionCheck().run(
            context(FakeHost(), site=site, lockfile={"fx-circuit-alt", "fx-district"})
        )
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn(f"lockfile {LOCKFILE_LABEL}", report.detail)
        self.assertIn("circuit 'fx-circuit' (circuit tier is present)", report.detail)
        self.assertIn("Correct jurisdiction in /etc/gideon/site.yaml", report.fix)
        self.assertIn(f"corpus cut --base {LOCKFILE_LABEL} --add-courts fx-circuit", report.fix)
        self.assertIn("then re-run preflight", report.fix)

    def test_missing_district_in_lockfile_warns_without_district_tier(self) -> None:
        site = make_jurisdiction_site(states="fx-state")
        report = JurisdictionCheck().run(
            context(FakeHost(), site=site, lockfile={"fx-circuit", "fx-app"})
        )
        self.assertEqual(report.severity, Severity.WARN)
        self.assertIn("district 'fx-district' (district tier is not in the lockfile)", report.detail)
        self.assertIn(f"--base {LOCKFILE_LABEL} --add-courts fx-district", report.fix)

    def test_missing_district_refuses_when_district_tier_is_carried(self) -> None:
        site = make_jurisdiction_site()
        report = JurisdictionCheck().run(
            context(FakeHost(), site=site, lockfile={"fx-circuit", "fx-district-alt"})
        )
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("district 'fx-district' (district tier is present)", report.detail)
        self.assertIn(f"corpus cut --base {LOCKFILE_LABEL} --add-courts fx-district", report.fix)

    def test_refusal_and_pending_courts_share_one_row(self) -> None:
        site = make_jurisdiction_site(states="fx-state")
        report = JurisdictionCheck().run(
            context(FakeHost(), site=site, lockfile={"fx-circuit-alt"})
        )
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn(f"lockfile {LOCKFILE_LABEL}", report.detail)
        self.assertIn("circuit 'fx-circuit' (circuit tier is present)", report.detail)
        self.assertIn("district 'fx-district' (district tier is not in the lockfile)", report.detail)
        self.assertIn("state 'fx-state' (no appellate court", report.detail)
        self.assertIn("<state-court-ids>", report.fix)
        self.assertIn("state_supreme and state_appellate levels", report.fix)


UNAME = ("uname", "-m")
NVIDIA_SMI = (
    "nvidia-smi",
    "--query-gpu=name,compute_cap,memory.total",
    "--format=csv,noheader",
)
NVIDIA_SMI_Q = ("nvidia-smi", "-q")


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


def _gpu_line(
    *,
    model: str | None = None,
    capability: str | None = None,
    vram_mib: int | None = None,
    include_unit: bool = True,
) -> str:
    requirements = MODELS.profiles[0].requires
    rendered_vram = vram_mib
    if rendered_vram is None:
        rendered_vram = _ceil_div(requirements.gpu.vram_gb * GIGABYTE, 2**20)
    vram = f"{rendered_vram} MiB" if include_unit else str(rendered_vram)
    return ", ".join(
        (
            model or requirements.gpu.model,
            capability or requirements.gpu.compute_capability,
            vram,
        )
    )


class HardwareProfile(unittest.TestCase):
    def gpu_host(
        self,
        *,
        gpu_stdout: str | None = None,
        gpu_returncode: int = 0,
        gpu_stderr: str = "",
        architecture_stdout: str | None = None,
        architecture_returncode: int = 0,
        architecture_stderr: str = "",
        platform: str | None = None,
        dram_kb: int | None = None,
    ) -> FakeHost:
        requirements = MODELS.profiles[0].requires
        if gpu_stdout is None:
            gpu_stdout = "\n".join(_gpu_line() for _ in range(requirements.gpu.count)) + "\n"
        if architecture_stdout is None:
            architecture_stdout = "\n".join(
                f"    Product Architecture              : {requirements.gpu.architecture}"
                for _ in range(requirements.gpu.count)
            ) + "\n"
        if platform is None:
            platform = requirements.platform
        if dram_kb is None:
            dram_kb = _ceil_div(requirements.dram_gb * GIGABYTE, 1024)
        return FakeHost(
            commands={
                ("uname", "-m"): completed(UNAME, f"{platform}\n"),
                NVIDIA_SMI: completed(NVIDIA_SMI, gpu_stdout, gpu_returncode, gpu_stderr),
                NVIDIA_SMI_Q: completed(
                    NVIDIA_SMI_Q,
                    architecture_stdout,
                    architecture_returncode,
                    architecture_stderr,
                ),
            },
            files={MEMINFO: f"MemTotal:       {dram_kb} kB\n"},
        )

    def test_unknown_profile_refuses_before_gpu_facts_on_gpu_host(self) -> None:
        site = make_site(extra="hardware_profile: missing-profile\n")
        host = self.gpu_host()
        report = HardwareProfileCheck().run(context(host, site=site))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn(MODELS.profiles[0].name, report.detail)
        self.assertIn("hardware_profile", report.fix)
        self.assertNotIn(("run", UNAME), host.calls)
        self.assertNotIn(("run", NVIDIA_SMI), host.calls)

    def test_unknown_profile_refuses_before_gpu_facts_on_no_gpu_host(self) -> None:
        site = make_site(extra="hardware_profile: missing-profile\n")
        host = self.gpu_host()
        report = HardwareProfileCheck().run(context(host, site=site, no_gpu=True))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn(MODELS.profiles[0].name, report.detail)
        self.assertIn("hardware_profile", report.fix)
        self.assertNotIn(("run", UNAME), host.calls)
        self.assertNotIn(("run", NVIDIA_SMI), host.calls)

    def test_no_gpu_skips_all_hardware_probes(self) -> None:
        host = self.gpu_host()
        report = HardwareProfileCheck().run(context(host, no_gpu=True))
        self.assertEqual(report.severity, Severity.PASS)
        self.assertIn("platform", report.detail)
        self.assertIn("GPU facts", report.detail)
        self.assertIn("DRAM", report.detail)
        self.assertFalse(host.calls)

    def test_non_reference_profile_warns_with_matching_facts(self) -> None:
        non_reference = dataclasses.replace(
            MODELS,
            profiles=(dataclasses.replace(MODELS.profiles[0], name="alternate"),),
        )
        site = make_site(extra="hardware_profile: alternate\n")
        report = HardwareProfileCheck().run(
            context(self.gpu_host(), site=site, models=non_reference)
        )
        self.assertEqual(report.severity, Severity.WARN)
        self.assertIn("alternate", report.detail)
        self.assertIn("nvidia-smi -q", report.detail)

    def test_matching_hardware_passes_with_all_fact_details(self) -> None:
        report = HardwareProfileCheck().run(context(self.gpu_host()))
        self.assertEqual(report.severity, Severity.PASS, report.detail)
        requirements = MODELS.profiles[0].requires
        self.assertIn(f"GPU count {requirements.gpu.count}", report.detail)
        self.assertIn(requirements.gpu.model, report.detail)
        self.assertIn(requirements.gpu.architecture, report.detail)
        self.assertIn(requirements.gpu.compute_capability, report.detail)
        self.assertIn(format_gb(_ceil_div(requirements.gpu.vram_gb * GIGABYTE, 2**20) * 2**20), report.detail)
        self.assertIn(format_gb(_ceil_div(requirements.dram_gb * GIGABYTE, 1024) * 1024), report.detail)
        self.assertIn(requirements.platform, report.detail)

    def test_wrong_count_refuses(self) -> None:
        requirements = MODELS.profiles[0].requires
        host = self.gpu_host(
            gpu_stdout=_gpu_line() + "\n",
            architecture_stdout=f"Product Architecture : {requirements.gpu.architecture}\n",
        )
        report = HardwareProfileCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("count", report.detail)
        self.assertIn(str(requirements.gpu.count), report.detail)

    def test_wrong_model_refuses(self) -> None:
        host = self.gpu_host(
            gpu_stdout="\n".join(_gpu_line(model="Other GPU") for _ in range(MODELS.profiles[0].requires.gpu.count)) + "\n"
        )
        report = HardwareProfileCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("Other GPU", report.detail)

    def test_wrong_capability_refuses(self) -> None:
        host = self.gpu_host(
            gpu_stdout="\n".join(_gpu_line(capability="0.0") for _ in range(MODELS.profiles[0].requires.gpu.count)) + "\n"
        )
        report = HardwareProfileCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("compute capability", report.detail)

    def test_wrong_architecture_refuses(self) -> None:
        host = self.gpu_host(
            architecture_stdout="\n".join(
                "    Product Architecture              : Other"
                for _ in range(MODELS.profiles[0].requires.gpu.count)
            )
            + "\n"
        )
        report = HardwareProfileCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("per nvidia-smi -q", report.detail)

    def test_vram_one_mib_below_floor_refuses_with_raw_and_rounded_value(self) -> None:
        requirements = MODELS.profiles[0].requires
        vram_mib = requirements.gpu.vram_gb * GIGABYTE // (2**20) - 1
        host = self.gpu_host(
            gpu_stdout="\n".join(
                _gpu_line(vram_mib=vram_mib)
                for _ in range(requirements.gpu.count)
            )
            + "\n"
        )
        report = HardwareProfileCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn(f"{vram_mib} MiB", report.detail)
        self.assertIn(format_gb(vram_mib * 2**20), report.detail)

    def test_dram_one_kb_below_floor_refuses_with_raw_and_rounded_value(self) -> None:
        requirements = MODELS.profiles[0].requires
        dram_kb = requirements.dram_gb * GIGABYTE // 1024 - 1
        host = self.gpu_host(dram_kb=dram_kb)
        report = HardwareProfileCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn(f"{dram_kb} kB", report.detail)
        self.assertIn(format_gb(dram_kb * 1024), report.detail)

    def test_wrong_platform_refuses(self) -> None:
        host = self.gpu_host(platform="other_platform")
        report = HardwareProfileCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("platform", report.detail)

    def test_count_and_dram_shortfalls_are_collected(self) -> None:
        requirements = MODELS.profiles[0].requires
        host = self.gpu_host(
            gpu_stdout=_gpu_line() + "\n",
            architecture_stdout=f"Product Architecture : {requirements.gpu.architecture}\n",
            dram_kb=requirements.dram_gb * GIGABYTE // 1024 - 1,
        )
        report = HardwareProfileCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("GPU count", report.detail)
        self.assertIn("DRAM", report.detail)

    def test_missing_nvidia_smi_refuses_with_driver_fix(self) -> None:
        host = self.gpu_host(gpu_returncode=127)
        report = HardwareProfileCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("nvidia-smi is not available", report.detail)
        self.assertIn("host provision --no-gpu", report.fix)

    def test_failing_nvidia_smi_refuses_with_stderr_and_driver_fix(self) -> None:
        host = self.gpu_host(gpu_returncode=1, gpu_stderr="driver failed")
        report = HardwareProfileCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("driver failed", report.detail)
        self.assertIn("host provision --only nvidia-driver", report.fix)

    def test_failing_architecture_probe_refuses_with_driver_fix(self) -> None:
        host = self.gpu_host(architecture_returncode=127)
        report = HardwareProfileCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("nvidia-smi is not available", report.detail)
        self.assertIn("host provision --no-gpu", report.fix)

    def test_architecture_count_must_match_gpu_count(self) -> None:
        architecture = MODELS.profiles[0].requires.gpu.architecture
        host = self.gpu_host(architecture_stdout=f"Product Architecture : {architecture}\n")
        report = HardwareProfileCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("architecture line", report.detail)

    def test_vram_without_mib_is_unreadable(self) -> None:
        host = self.gpu_host(gpu_stdout=_gpu_line(include_unit=False) + "\n")
        report = HardwareProfileCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("unreadable", report.detail)

    def test_missing_memtotal_is_unreadable(self) -> None:
        host = self.gpu_host()
        host.files[MEMINFO] = "MemFree: 1 kB\n"
        report = HardwareProfileCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("MemTotal", report.detail)

    def test_unreadable_gpus_refuse(self) -> None:
        report = HardwareProfileCheck().run(context(self.gpu_host(gpu_returncode=12)))
        self.assertEqual(report.severity, Severity.REFUSE)


class DriverTested(unittest.TestCase):
    def lock_with(self, tested: str | None):
        return dataclasses.replace(LOCK, driver=dataclasses.replace(LOCK.driver, tested=tested))

    def driver_host(self, version: str | None) -> FakeHost:
        return FakeHost(files={str(_LOADED_VERSION): f"{version}\n"} if version else {})

    def driver_tested_version(self) -> str:
        return LOCK.driver.tested or f"{LOCK.driver.branch}.0.0-fictitious"

    def test_null_tested_passes(self) -> None:
        tested = self.driver_tested_version()
        host = self.driver_host(f"{tested}-1")
        report = DriverTestedCheck().run(
            context(host, lock=self.lock_with(None))
        )
        self.assertEqual(report.severity, Severity.PASS)
        self.assertNotIn(("read_text", str(_LOADED_VERSION)), host.calls)

    def test_matching_driver_passes(self) -> None:
        tested = self.driver_tested_version()
        report = DriverTestedCheck().run(
            context(self.driver_host(f"{tested}-0ubuntu1"), lock=self.lock_with(tested))
        )
        self.assertEqual(report.severity, Severity.PASS, report.detail)
        self.assertIn(f"matches tested {tested}", report.detail)

    def test_driver_above_tested_warns(self) -> None:
        """A driver above the tested version produces a warning."""

        tested = self.driver_tested_version()
        next_major = int(tested.split(".", 1)[0]) + 1
        report = DriverTestedCheck().run(
            context(
                self.driver_host(f"{next_major}.0.0-fictitious"),
                lock=self.lock_with(tested),
            )
        )
        self.assertEqual(report.severity, Severity.WARN)
        self.assertIn("above tested", report.detail)

    def test_driver_below_tested_warns(self) -> None:
        tested = self.driver_tested_version()
        previous_major = int(tested.split(".", 1)[0]) - 1
        actual = f"{previous_major}.0.0-fictitious"
        report = DriverTestedCheck().run(
            context(self.driver_host(actual), lock=self.lock_with(tested))
        )
        self.assertEqual(report.severity, Severity.WARN)
        self.assertIn(f"installed driver {actual} is below tested {tested}", report.detail)

    def test_no_loaded_module_refuses_with_gpu_fix(self) -> None:
        report = DriverTestedCheck().run(
            context(self.driver_host(None), lock=self.lock_with(self.driver_tested_version()))
        )
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertEqual(report.detail, "no NVIDIA driver is loaded")
        self.assertEqual(report.fix, nogpu.GPU_DRIVER_FIX)

    def test_no_gpu_host_skips_driver_probe(self) -> None:
        host = self.driver_host(self.driver_tested_version())
        report = DriverTestedCheck().run(context(host, no_gpu=True))
        self.assertEqual(report.severity, Severity.PASS)
        self.assertIn("skipped", report.detail)
        self.assertNotIn(("read_text", str(_LOADED_VERSION)), host.calls)


UNAME = ("uname", "-r")


class OsKernel(unittest.TestCase):
    def kernel_lock(self, tested: str | None):
        return dataclasses.replace(LOCK, kernel_tested=tested)

    def kernel_host(self, running: str) -> FakeHost:
        return FakeHost(commands={UNAME: completed(UNAME, f"{running}\n")})

    def test_no_recorded_kernel_passes(self) -> None:
        report = OsKernelCheck().run(
            context(self.kernel_host("6.14.0-32-generic"), lock=self.kernel_lock(None))
        )
        self.assertEqual(report.severity, Severity.PASS)
        self.assertIn("no tested kernel", report.detail)

    def test_matching_kernel_passes(self) -> None:
        report = OsKernelCheck().run(
            context(self.kernel_host("6.14.0-32-generic"), lock=self.kernel_lock("6.14.0-32-generic"))
        )
        self.assertEqual(report.severity, Severity.PASS)

    def test_kernel_drift_warns(self) -> None:
        """Spec bug (f): within-LTS kernel drift warns, never refuses."""

        report = OsKernelCheck().run(
            context(self.kernel_host("6.14.0-33-generic"), lock=self.kernel_lock("6.14.0-32-generic"))
        )
        self.assertEqual(report.severity, Severity.WARN)

    def test_unreadable_kernel_refuses(self) -> None:
        host = FakeHost(commands={UNAME: completed(UNAME, "", 1)})
        report = OsKernelCheck().run(context(host, lock=self.kernel_lock(None)))
        self.assertEqual(report.severity, Severity.REFUSE)
