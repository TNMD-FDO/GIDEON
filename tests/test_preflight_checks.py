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

from gideon.host import cotenants, gpus, nogpu, prometheus, worker
from gideon.host.checks import (
    CHECKS,
    MEBIBYTE,
    CheckReport,
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
    GPU_MEMORY_CARD_ARGV,
    GPU_MEMORY_CONTAINERS_ARGV,
    GPU_MEMORY_FLAG,
    GPU_MEMORY_PROCESS_ARGV,
    HOST_MEMORY_FLOOR_FRACTION,
    MEMORY_LOW_QUERY,
    DataVolumeCheck,
    GpuMemoryCheck,
    HostMemoryCheck,
    LowReader,
    LowReading,
    parse_size_and_available,
    read_prometheus_low,
    read_promoted_bytes,
)
from gideon.host.checks.network import (
    DOCKER_NETWORK_TIMEOUT_SECONDS,
    GETENT_TIMEOUT_SECONDS,
    NETWORK_LS_ARGV,
    PROBE_RETRY_WAIT_SECONDS,
    PROBE_TIMEOUT_SECONDS,
    DockerNetworksCheck,
    EgressCheck,
    HostnameCheck,
    NtpCheck,
    PortsCheck,
    network_inspect_argv,
)
from gideon.host.checks.services import BackupSshCheck, LdapCheck, SmtpCheck
from gideon.host.corpus.lockfile import (
    SCHEMA_VERSION,
    Lockfile,
    SourcePin,
    load_lockfile,
)
from gideon.host.courts import Court, CourtMap, CourtSource
from gideon.host.egress import EgressAllowlist, EgressGroup, EgressHost
from gideon.host.index import reserve
from gideon.host.ldap import ldapsearch_argv
from gideon.host.lock import load_host_lock
from gideon.host.models import GIGABYTE, ModelPin, ModelsLock, load_models_lock
from gideon.host.prometheus import PROMETHEUS_QUERY_PATH, PROMETHEUS_TIMEOUT_SECONDS
from gideon.host.render.services.prometheus import PROMETHEUS_LOOPBACK_ADDRESS
from gideon.host.render.worker import PIPELINE_VERSION
from gideon.host.site import SiteConfig, load_site, render_errors
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
        EgressGroup("archive", (EgressHost("archive.example", "https://archive.example/"),)),
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
                "docker-networks",
                "ldap",
                "backup-ssh",
                "smtp",
                "data-volume",
                "host-memory",
                "jurisdiction",
                "hardware-profile",
                "gpu-memory",
                "driver-tested",
                "os-kernel",
            ],
        )


class SequenceHost(FakeHost):
    """A FakeHost whose listed argvs answer their outcomes in turn, the last repeating."""

    def __init__(
        self,
        *,
        sequences: Mapping[tuple[str, ...], list[subprocess.CompletedProcess[str]]],
        commands: Mapping[tuple[str, ...], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        super().__init__(commands=commands)
        self.sequences = {argv: list(outcomes) for argv, outcomes in sequences.items()}

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
        outcomes = self.sequences.get(tuple(argv))
        if outcomes:
            self.calls.append(("run", tuple(argv)))
            return outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        return super().run(
            argv,
            check=check,
            input=input,
            cwd=cwd,
            env=env,
            timeout=timeout,
            passthrough=passthrough,
        )


class Egress(unittest.TestCase):
    """The check probes both pre-install egress groups and the site's registry host."""

    REGISTRY_PROBE = probe_argv("https://ghcr.io/v2/")

    def setUp(self) -> None:
        self.sleeps: list[float] = []
        self.check = EgressCheck(sleep=self.sleeps.append)

    def probes(self, host: FakeHost, argv: tuple[str, ...]) -> int:
        return host.calls.count(("run", argv))

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
        report = self.check.run(context(host))
        self.assertEqual(report.severity, Severity.PASS)

    def test_no_first_miss_waits_for_nothing_and_reads_as_before(self) -> None:
        host = FakeHost(commands=self.outcomes())
        report = self.check.run(context(host))
        self.assertEqual(report.detail, "all 3 required egress hosts responded")
        self.assertEqual(self.sleeps, [])
        for argv in self.outcomes():
            self.assertEqual(self.probes(host, argv), 1)

    def test_unanswered_host_answering_its_second_probe_passes_naming_it(self) -> None:
        probe = probe_argv("https://a.example/")
        commands = self.outcomes()
        del commands[probe]
        host = SequenceHost(
            sequences={probe: [completed((), returncode=4), completed(())]},
            commands=commands,
        )
        report = self.check.run(context(host))
        self.assertEqual(report.severity, Severity.PASS, report.detail)
        self.assertEqual(
            report.detail,
            "all 3 required egress hosts responded; "
            "on a second probe: a.example (first no response)",
        )
        self.assertEqual(self.sleeps, [PROBE_RETRY_WAIT_SECONDS])
        self.assertEqual(self.probes(host, probe), 2)
        self.assertEqual(self.probes(host, probe_argv("https://b.example/")), 1)
        self.assertEqual(self.probes(host, self.REGISTRY_PROBE), 1)
        runs = [call[1] for call in host.calls if call[0] == "run"]
        self.assertEqual(runs[-1], probe)

    def test_twice_missed_host_refuses_naming_both_verdicts(self) -> None:
        probe = probe_argv("https://a.example/")
        commands = self.outcomes()
        del commands[probe]
        host = SequenceHost(
            sequences={
                probe: [
                    completed((), returncode=4),
                    completed((), returncode=8, stderr="  HTTP/1.1 403 Forbidden\n"),
                ]
            },
            commands=commands,
        )
        report = self.check.run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertEqual(
            report.detail, "unreachable hosts: a.example (no response, then HTTP 403)"
        )
        self.assertEqual(self.probes(host, probe), 2)

    def test_refuse_row_names_a_second_probe_answer_after_the_misses(self) -> None:
        probe = probe_argv("https://a.example/")
        commands = self.outcomes(b=4)
        del commands[probe]
        host = SequenceHost(
            sequences={probe: [completed((), returncode=4), completed(())]},
            commands=commands,
        )
        report = self.check.run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertEqual(
            report.detail,
            "unreachable hosts: b.example (no response, then no response); "
            "on a second probe: a.example (first no response)",
        )
        self.assertEqual(self.sleeps, [PROBE_RETRY_WAIT_SECONDS])

    def test_no_host_is_probed_more_than_twice(self) -> None:
        host = FakeHost(commands=self.outcomes(a=4, registry=5))
        report = self.check.run(context(host))
        self.assertEqual(
            report.detail,
            "unreachable hosts: a.example (no response, then no response), "
            "ghcr.io (no response, then no response)",
        )
        self.assertEqual(self.sleeps, [PROBE_RETRY_WAIT_SECONDS])
        self.assertEqual(self.probes(host, probe_argv("https://a.example/")), 2)
        self.assertEqual(self.probes(host, self.REGISTRY_PROBE), 2)
        self.assertEqual(self.probes(host, probe_argv("https://b.example/")), 1)

    def test_host_answering_with_a_status_is_not_probed_again(self) -> None:
        probe = probe_argv("https://a.example/")
        commands = self.outcomes()
        commands[probe] = completed((), returncode=8, stderr="  HTTP/1.1 403 Forbidden\n")
        host = FakeHost(commands=commands)
        report = self.check.run(context(host))
        self.assertEqual(report.detail, "unreachable hosts: a.example (HTTP 403)")
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.probes(host, probe), 1)

    def test_auth_gated_registry_response_still_counts_as_reachable(self) -> None:
        """A 401 on the registry /v2/ endpoint proves the origin answered."""

        commands = self.outcomes()
        commands[self.REGISTRY_PROBE] = completed(
            (), returncode=8, stderr="  HTTP/1.1 401 Unauthorized\n"
        )
        host = FakeHost(commands=commands)
        report = self.check.run(context(host))
        self.assertEqual(report.severity, Severity.PASS, report.detail)

    def test_wget_auth_failure_exit_counts_as_reachable(self) -> None:
        """GNU wget exits 6, not 8, on a 401 — live finding, v0.0.9 hotfix."""

        commands = self.outcomes()
        commands[self.REGISTRY_PROBE] = completed((), returncode=6)
        host = FakeHost(commands=commands)
        report = self.check.run(context(host))
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
        report = self.check.run(preflight_context)
        self.assertEqual(report.severity, Severity.PASS, report.detail)

    def test_proxy_block_page_is_a_miss_naming_the_status(self) -> None:
        """A 403 block page is not reachability — the download would fail."""

        commands = self.outcomes()
        commands[probe_argv("https://a.example/")] = completed(
            (), returncode=8, stderr="  HTTP/1.1 403 Forbidden\n"
        )
        host = FakeHost(commands=commands)
        report = self.check.run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("a.example", report.detail)
        self.assertIn("403", report.detail)

    def test_every_missed_host_is_named(self) -> None:
        host = FakeHost(commands=self.outcomes(a=4, b=5))
        report = self.check.run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("a.example", report.detail)
        self.assertIn("b.example", report.detail)
        self.assertIn("egress_proxy", report.fix)
        self.assertIn("docs/runbooks/release-files.md §5", report.fix)

    def test_corpus_group_is_not_probed_pre_install(self) -> None:
        host = FakeHost(commands=self.outcomes())
        self.check.run(context(host))
        probed = [call for call in host.calls if call[0] == "run"]
        self.assertNotIn(("run", probe_argv("https://c.example/")), probed)

    def test_archive_group_is_not_probed_pre_install(self) -> None:
        host = FakeHost(commands=self.outcomes())
        report = self.check.run(context(host))
        self.assertEqual(report.severity, Severity.PASS, report.detail)
        probed = [call[1] for call in host.calls if call[0] == "run"]
        self.assertCountEqual(probed, self.outcomes())
        self.assertNotIn(probe_argv("https://archive.example/"), probed)

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
        report = self.check.run(context(host, site=site))
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
        report = self.check.run(context(host, site=site))
        self.assertEqual(report.severity, Severity.PASS, report.detail)
        self.assertIn(("run", probe), host.calls)

    def test_proxy_auth_travels_by_wgetrc_never_argv(self) -> None:
        site = make_site(extra="egress_proxy: http://proxy.test:3128\n")
        host = FakeHost(files={"/etc/gideon/secrets/proxy_auth": "alice:s3cret\n"})
        report = self.check.run(context(host, site=site))
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

        # Every probe gave no response, so each was sent twice with one argv.
        wgets = [call[1] for call in host.calls if call[0] == "run"]
        self.assertEqual(len(wgets), 6)
        self.assertTrue(all(wgets.count(argv) == 2 for argv in wgets), wgets)
        self.assertEqual(self.sleeps, [PROBE_RETRY_WAIT_SECONDS])

    def test_malformed_proxy_auth_refuses(self) -> None:
        host = FakeHost(files={"/etc/gideon/secrets/proxy_auth": "no-colon\n"})
        report = self.check.run(context(host))
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


class DockerNetworks(unittest.TestCase):
    """Docker network subnets are judged against named site hosts and LAN ranges."""

    ADDRESSES: Mapping[str, tuple[str, ...]] = {
        "gideon.test": ("198.51.100.10",),
        "ad.test": ("198.51.100.11",),
        "nas.test": ("198.51.100.12",),
        "smtp.test": ("198.51.100.13",),
        "registry.test": ("198.51.100.14",),
        "proxy.test": ("198.51.100.15",),
    }

    class TimedHost(FakeHost):
        """Record each bound and expire one chosen command."""

        def __init__(
            self,
            commands: Mapping[tuple[str, ...], subprocess.CompletedProcess[str]],
            expire: tuple[str, ...] | None = None,
        ) -> None:
            super().__init__(commands=commands)
            self.expire = expire
            self.timeouts: list[tuple[tuple[str, ...], float | None]] = []

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
            command = tuple(argv)
            self.timeouts.append((command, timeout))
            if command == self.expire:
                assert timeout is not None
                raise subprocess.TimeoutExpired(command, timeout)
            return super().run(
                argv,
                check=check,
                input=input,
                cwd=cwd,
                env=env,
                timeout=timeout,
                passthrough=passthrough,
            )

    def network_host(
        self,
        rows: tuple[str, ...] = ("shared\tother\t203.0.113.0/24",),
        *,
        addresses: Mapping[str, tuple[str, ...]] | None = None,
        unresolved: Collection[str] = (),
        proxy: bool = False,
        listing_code: int = 0,
        inspect_code: int = 0,
    ) -> tuple[FakeHost, SiteConfig]:
        extra = "registry: registry.test/example\n"
        if proxy:
            extra += "egress_proxy: http://proxy.test:3128\n"
        site = make_site(extra=extra)
        ids = tuple(f"network-{index}" for index in range(len(rows)))
        commands: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {
            NETWORK_LS_ARGV: completed(
                NETWORK_LS_ARGV, "".join(f"{network_id}\n" for network_id in ids), listing_code
            )
        }
        if ids:
            argv = network_inspect_argv(ids)
            commands[argv] = completed(argv, "".join(f"{row}\n" for row in rows), inspect_code)
        answers = dict(self.ADDRESSES)
        if addresses is not None:
            answers.update(addresses)
        for host, values in answers.items():
            argv = ("getent", "ahosts", host)
            stdout = "".join(f"{value} STREAM {host}\n" for value in values)
            commands[argv] = completed(argv, stdout, 2 if host in unresolved else 0)
        return FakeHost(commands=commands), site

    def report(
        self,
        rows: tuple[str, ...],
        *,
        addresses: Mapping[str, tuple[str, ...]] | None = None,
        unresolved: Collection[str] = (),
        proxy: bool = False,
        listing_code: int = 0,
        inspect_code: int = 0,
    ) -> tuple[CheckReport, FakeHost]:
        host, site = self.network_host(
            rows,
            addresses=addresses,
            unresolved=unresolved,
            proxy=proxy,
            listing_code=listing_code,
            inspect_code=inspect_code,
        )
        return DockerNetworksCheck().run(context(host, site=site)), host

    def assert_read_fix(self, report: CheckReport, command: str) -> None:
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn(f"Repair docker network {command}", report.fix)

    def assert_bounds(self, host: TimedHost) -> None:
        for argv, timeout in host.timeouts:
            if argv[:2] == ("getent", "ahosts"):
                expected = GETENT_TIMEOUT_SECONDS
            else:
                self.assertEqual(argv[:2], ("docker", "network"))
                expected = DOCKER_NETWORK_TIMEOUT_SECONDS
            self.assertEqual(timeout, expected, argv)

    def test_relay_cover_names_network_project_and_key_without_addresses(self) -> None:
        subnet = "198.51.100.13/32"
        address = self.ADDRESSES["smtp.test"][0]
        report, _ = self.report((f"relay-bridge\ttranscribe\t{subnet}",))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("relay-bridge (project transcribe) covers", report.detail)
        self.assertIn("alerts.smtp.host", report.detail)
        for value in (
            subnet,
            "192.0.2.0/24",
            address,
            *(item for values in self.ADDRESSES.values() for item in values),
        ):
            self.assertNotIn(value, report.detail)
            self.assertNotIn(value, report.fix)

    def test_cover_without_label_names_no_compose_project(self) -> None:
        report, _ = self.report(("bridge\t\t198.51.100.0/24",))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("bridge (no Compose project)", report.detail)

    def test_directory_host_checks_all_three_addresses(self) -> None:
        addresses = {
            "ad.test": ("203.0.113.10", "198.51.100.11", "203.0.113.11")
        }
        report, _ = self.report(
            ("directory-bridge\tother\t198.51.100.11/32",), addresses=addresses
        )
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("auth.ldap.host", report.detail)

    def test_gideon_project_is_named_on_cover(self) -> None:
        report, _ = self.report(("gideon-bridge\tgideon\t198.51.100.10/32",))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("gideon-bridge (project gideon)", report.detail)

    def test_lan_range_overlap_names_its_index(self) -> None:
        report, _ = self.report(("lan-bridge\tother\t192.0.2.128/25",))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("lan_cidrs[0]", report.detail)

    def test_unresolved_host_keeps_pass_text(self) -> None:
        rows = ("clear\tother\t203.0.113.0/24",)
        resolved, _ = self.report(rows)
        unresolved, _ = self.report(rows, unresolved=("smtp.test",))
        self.assertEqual(resolved.severity, Severity.PASS)
        self.assertEqual(unresolved.severity, Severity.PASS)
        self.assertEqual(unresolved.detail, resolved.detail)

    def test_failed_listing_refuses_with_read_fix(self) -> None:
        report, _ = self.report((), listing_code=127)
        self.assert_read_fix(report, "ls")

    def test_failed_inspect_refuses_with_read_fix(self) -> None:
        report, _ = self.report(("clear\tother\t203.0.113.0/24",), inspect_code=1)
        self.assert_read_fix(report, "inspect")

    def test_inspect_line_with_too_few_tabs_refuses(self) -> None:
        report, _ = self.report(("broken\tother",))
        self.assert_read_fix(report, "inspect")

    def test_inspect_line_with_bad_subnet_refuses(self) -> None:
        report, _ = self.report(("broken\tother\tnot-a-subnet",))
        self.assert_read_fix(report, "inspect")

    def test_host_and_none_networks_without_subnets_pass(self) -> None:
        report, _ = self.report(("host\t\t", "none\t\t"))
        self.assertEqual(report.severity, Severity.PASS, report.detail)
        self.assertIn("2 Docker networks", report.detail)

    def test_ipv6_subnet_does_not_cover_ipv4_addresses(self) -> None:
        report, _ = self.report(("ipv6\tother\t2001:db8::/64",))
        self.assertEqual(report.severity, Severity.PASS, report.detail)

    def test_proxy_host_is_resolved_and_judged_when_set(self) -> None:
        report, host = self.report(
            ("proxy-bridge\tother\t203.0.113.0/24",),
            proxy=True,
            addresses={"proxy.test": ("203.0.113.15",)},
        )
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("egress_proxy", report.detail)
        self.assertEqual(host.calls.count(("run", ("getent", "ahosts", "proxy.test"))), 1)

    def test_proxy_host_is_not_resolved_when_unset(self) -> None:
        report, host = self.report(("clear\tother\t203.0.113.0/24",))
        self.assertEqual(report.severity, Severity.PASS)
        self.assertNotIn(("run", ("getent", "ahosts", "proxy.test")), host.calls)

    def test_registry_host_is_judged(self) -> None:
        report, host = self.report(
            ("registry-bridge\tother\t203.0.113.0/24",),
            addresses={"registry.test": ("203.0.113.14",)},
        )
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("covers registry", report.detail)
        self.assertEqual(host.calls.count(("run", ("getent", "ahosts", "registry.test"))), 1)

    def test_two_keys_on_one_host_resolve_once_and_both_are_named(self) -> None:
        host, site = self.network_host(("shared\tother\t198.51.100.11/32",))
        smtp = dataclasses.replace(site.alerts.smtp, host="ad.test")
        site = dataclasses.replace(site, alerts=dataclasses.replace(site.alerts, smtp=smtp))
        report = DockerNetworksCheck().run(context(host, site=site))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("auth.ldap.host", report.detail)
        self.assertIn("alerts.smtp.host", report.detail)
        self.assertEqual(host.calls.count(("run", ("getent", "ahosts", "ad.test"))), 1)

    def test_empty_listing_passes_without_inspect(self) -> None:
        report, host = self.report(())
        self.assertEqual(report.severity, Severity.PASS)
        self.assertIn("0 Docker networks", report.detail)
        self.assertFalse(
            any(
                call[0] == "run"
                and isinstance(call[1], tuple)
                and call[1][:3] == ("docker", "network", "inspect")
                for call in host.calls
            )
        )

    def test_clear_network_reports_judged_counts(self) -> None:
        report, _ = self.report(("clear\tother\t203.0.113.0/24",))
        self.assertEqual(report.severity, Severity.PASS)
        self.assertIn("1 Docker networks", report.detail)
        self.assertIn("5 site hosts", report.detail)
        self.assertIn("1 LAN ranges", report.detail)

    def test_expired_listing_refuses_with_bound_and_read_fix(self) -> None:
        base, site = self.network_host(("clear\tother\t203.0.113.0/24",))
        host = self.TimedHost(base.commands, NETWORK_LS_ARGV)
        report = DockerNetworksCheck().run(context(host, site=site))
        self.assert_read_fix(report, "ls")
        self.assertIn("timed out", report.detail)
        self.assert_bounds(host)

    def test_expired_inspect_refuses_with_bound_and_read_fix(self) -> None:
        base, site = self.network_host(("clear\tother\t203.0.113.0/24",))
        argv = network_inspect_argv(("network-0",))
        host = self.TimedHost(base.commands, argv)
        report = DockerNetworksCheck().run(context(host, site=site))
        self.assert_read_fix(report, "inspect")
        self.assertIn("timed out", report.detail)
        self.assert_bounds(host)

    def test_expired_lookup_keeps_pass_text_and_all_bounds(self) -> None:
        base, site = self.network_host(("clear\tother\t203.0.113.0/24",))
        passing = DockerNetworksCheck().run(context(base, site=site))
        host = self.TimedHost(base.commands, ("getent", "ahosts", "smtp.test"))
        report = DockerNetworksCheck().run(context(host, site=site))
        self.assertEqual(report.severity, Severity.PASS)
        self.assertEqual(report.detail, passing.detail)
        self.assert_bounds(host)


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
        reader = mock.Mock(return_value=reserve.PromotedReading(1))
        report = DataVolumeCheck(reader=reader).run(context(self.host(size)))
        reader.assert_not_called()
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertEqual(report.detail, f"/data is {size} bytes ({format_gb(size)}), profile requires a {MODELS.profiles[0].requires.data_volume_gb} GB data volume")
        self.assertEqual(report.fix,
            f"Provide a data volume of at least {MODELS.profiles[0].requires.data_volume_gb} GB at /data, or set "
            "hardware_profile in /etc/gideon/site.yaml to a profile this host satisfies, then re-run preflight.")
        self.assertIn(f"{size} bytes", report.detail)
        self.assertIn(format_gb(size), report.detail)
        self.assertIn(f"{MODELS.profiles[0].requires.data_volume_gb} GB", report.detail)
        self.assertIn(f"{MODELS.profiles[0].requires.data_volume_gb} GB", report.fix)
        self.assertIn(" GB at /data, or set", report.fix)
        self.assertIn("hardware_profile", report.fix)

    def test_at_floor_passes_with_size_free_bytes_and_floor(self) -> None:
        floor = MODELS.profiles[0].requires.data_volume_gb * GIGABYTE
        available = 123 * GIGABYTE
        host = self.host(floor, available)
        reader = mock.Mock(return_value=reserve.PromotedReading(None, "no promoted generation"))
        report = DataVolumeCheck(reader=reader).run(context(host))
        reader.assert_called_once_with(host)
        self.assertEqual(report.severity, Severity.PASS, report.detail)
        self.assertEqual(report.detail,
            f"/data size {format_gb(floor)}, {format_gb(available)} free; profile requires a "
            f"{MODELS.profiles[0].requires.data_volume_gb} GB data volume; reserve skipped: no promoted generation")
        self.assertEqual(report.fix, "")
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
        reader = mock.Mock(return_value=reserve.PromotedReading(1))
        report = DataVolumeCheck(reader=reader).run(context(host, no_gpu=True))
        reader.assert_not_called()
        self.assertEqual(report.severity, Severity.PASS, report.detail)
        self.assertEqual(report.detail,
            f"skipped: no-GPU host; /data size {format_gb(size)}, {format_gb(10_000)} free; data-volume floor is not judged")
        self.assertIn("skipped: no-GPU host", report.detail)
        self.assertIn(format_gb(size), report.detail)
        self.assertEqual(report.fix, "")

    def test_unreadable_promoted_bytes_keep_the_size_verdict_and_name_the_reason(self) -> None:
        floor = MODELS.profiles[0].requires.data_volume_gb * GIGABYTE
        reader = mock.Mock(return_value=reserve.PromotedReading(None, "fictitious record unavailable"))
        report = DataVolumeCheck(reader=reader).run(context(self.host(floor)))
        self.assertEqual(report.severity, Severity.PASS)
        self.assertTrue(report.detail.endswith("reserve skipped: fictitious record unavailable"))
        self.assertEqual(report.fix, "")

    def test_below_the_alert_line_warns_even_below_the_build_line_and_names_both(self) -> None:
        floor = MODELS.profiles[0].requires.data_volume_gb * GIGABYTE
        promoted = 8 * GIGABYTE
        for available in (0, 9 * GIGABYTE, 16 * GIGABYTE - 1):
            with self.subTest(available=available):
                reader = mock.Mock(return_value=reserve.PromotedReading(promoted))
                report = DataVolumeCheck(reader=reader).run(context(self.host(floor, available)))
                self.assertEqual(report.severity, Severity.WARN)
                self.assertIn(f"/data size {format_gb(floor)}, {format_gb(available)} free", report.detail)
                self.assertIn(f"profile requires a {MODELS.profiles[0].requires.data_volume_gb} GB data volume", report.detail)
                self.assertIn(reserve.describe(reserve.judge(available, promoted)), report.detail)
                self.assertIn(f"Free space under /data as {reserve.RUNBOOK_ROW} says", report.fix)
                self.assertIn("old drill or staging directories", report.fix)
                self.assertIn("retention", report.fix)
                self.assertIn(capacity.report.command("preflight"), report.fix)

    def test_at_or_above_the_alert_line_passes_with_both_lines(self) -> None:
        floor = MODELS.profiles[0].requires.data_volume_gb * GIGABYTE
        promoted = 8 * GIGABYTE
        for available in (16 * GIGABYTE, 17 * GIGABYTE):
            with self.subTest(available=available):
                reader = mock.Mock(return_value=reserve.PromotedReading(promoted))
                report = DataVolumeCheck(reader=reader).run(context(self.host(floor, available)))
                self.assertEqual(report.severity, Severity.PASS)
                self.assertIn(reserve.describe(reserve.judge(available, promoted)), report.detail)
                self.assertEqual(report.fix, "")

    def test_default_reader_skips_a_failed_psql_read_with_its_reason(self) -> None:
        floor = MODELS.profiles[0].requires.data_volume_gb * GIGABYTE
        host = self.host(floor)
        argv = tuple(worker.psql_argv("/etc/gideon/rendered"))
        host.commands[argv] = subprocess.CompletedProcess(argv, 3, "[]", "private diagnostic")
        report = DataVolumeCheck().run(context(host))
        self.assertEqual(report.severity, Severity.PASS)
        self.assertTrue(report.detail.endswith("reserve skipped: promoted generation read failed (exit 3)"))
        self.assertNotIn("private diagnostic", report.detail + report.fix)
        self.assertEqual(host.calls.count(("run", argv)), 1)

    def test_default_reader_returns_no_generation_or_the_sum_across_collections(self) -> None:
        argv = tuple(worker.psql_argv("/etc/gideon/rendered"))
        for rows, expected in (
            ([], reserve.PromotedReading(None, "no promoted generation")),
            ([{"gen_id": "fictitious-live-a", "collection": "caselaw", "bytes": 7},
              {"gen_id": "fictitious-live-b", "collection": "authorities", "bytes": 14}],
             reserve.PromotedReading(21)),
        ):
            with self.subTest(rows=rows):
                host = FakeHost(commands={argv: completed(argv, json.dumps(rows))})
                self.assertEqual(read_promoted_bytes(host), expected)
                self.assertEqual(host.calls, [("run", argv)])

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

        with mock.patch.object(prometheus.http.client, "HTTPConnection", Connection):
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
        self.assertIn("docs/runbooks/release-files.md §11", report.fix)
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
        self.assertIn("docs/runbooks/release-files.md §11", report.fix)

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
    "--query-gpu=uuid,name,compute_cap,memory.total",
    "--format=csv,noheader",
)
NVIDIA_SMI_Q = ("nvidia-smi", "-q")


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


def _gpu_line(
    *,
    uuid: str = "GPU-fictitious-0",
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
            uuid,
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
        model: str | None = None,
        gpu_returncode: int = 0,
        gpu_stderr: str = "",
        architecture_stdout: str | None = None,
        architecture_returncode: int = 0,
        architecture_stderr: str = "",
        platform: str | None = None,
        dram_kb: int | None = None,
        record: str | None = "valid",
    ) -> FakeHost:
        requirements = MODELS.profiles[0].requires
        if gpu_stdout is None:
            gpu_stdout = "\n".join(
                _gpu_line(uuid=f"GPU-fictitious-{position}", model=model)
                for position in range(requirements.gpu.count)
            ) + "\n"
        uuids = tuple(line.split(",")[0].strip() for line in gpu_stdout.splitlines() if line.strip())
        if architecture_stdout is None:
            architecture_stdout = "\n".join(
                f"    Product Architecture              : {requirements.gpu.architecture}"
                for _ in uuids
            ) + "\n"
        if platform is None:
            platform = requirements.platform
        if dram_kb is None:
            dram_kb = _ceil_div(requirements.dram_gb * GIGABYTE, 1024)
        files = {MEMINFO: f"MemTotal:       {dram_kb} kB\n"}
        if record is not None:
            files[str(gpus.GPU_RECORD_PATH)] = (
                "gpus:\n" + "".join(f"  - {uuid}\n" for uuid in uuids)
                if record == "valid" else record
            )
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
            files=files,
        )

    def models_with_count(self, count: int, *, name: str | None = None) -> ModelsLock:
        profile = MODELS.profiles[0]
        return dataclasses.replace(
            MODELS,
            profiles=(dataclasses.replace(
                profile,
                name=name or profile.name,
                requires=dataclasses.replace(
                    profile.requires,
                    gpu=dataclasses.replace(profile.requires.gpu, count=count),
                ),
            ),),
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
        requirements = non_reference.profiles[0].requires
        reported = requirements.gpu.model + " Workstation Edition"
        host = self.gpu_host(model=reported)
        report = HardwareProfileCheck().run(
            context(host, site=site, models=non_reference)
        )
        self.assertEqual(report.severity, Severity.WARN)
        self.assertIn("alternate", report.detail)
        self.assertIn("nvidia-smi -q", report.detail)
        self.assertIn(f"model {reported}", report.detail)

    def test_matching_hardware_passes_with_all_fact_details(self) -> None:
        report = HardwareProfileCheck().run(context(self.gpu_host()))
        self.assertEqual(report.severity, Severity.PASS, report.detail)
        requirements = MODELS.profiles[0].requires
        self.assertIn(f"GPU count {requirements.gpu.count}", report.detail)
        self.assertNotIn("unused", report.detail)
        self.assertNotIn("unrecorded", report.detail)
        self.assertIn(requirements.gpu.model, report.detail)
        self.assertIn(requirements.gpu.architecture, report.detail)
        self.assertIn(requirements.gpu.compute_capability, report.detail)
        self.assertIn(format_gb(_ceil_div(requirements.gpu.vram_gb * GIGABYTE, 2**20) * 2**20), report.detail)
        self.assertIn(format_gb(_ceil_div(requirements.dram_gb * GIGABYTE, 1024) * 1024), report.detail)
        self.assertIn(requirements.platform, report.detail)

    def test_card_editions_pass_with_reported_name(self) -> None:
        """Each edition selects the card's profile and reports its name."""

        requirements = MODELS.profiles[0].requires
        for suffix in (" Server Edition", " Workstation Edition", " Max-Q Workstation Edition"):
            with self.subTest(suffix=suffix):
                reported = requirements.gpu.model + suffix
                host = self.gpu_host(model=reported)
                report = HardwareProfileCheck().run(context(host))
                self.assertEqual(report.severity, Severity.PASS, report.detail)
                self.assertIn(f"model {reported}", report.detail)

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
        self.assertIn(f"GPU count is 1, profile requires {requirements.gpu.count}", report.detail)

    def test_wrong_model_refuses(self) -> None:
        host = self.gpu_host(
            gpu_stdout="\n".join(
                _gpu_line(uuid=f"GPU-fictitious-{position}", model="Other GPU")
                for position in range(MODELS.profiles[0].requires.gpu.count)
            ) + "\n"
        )
        report = HardwareProfileCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("Other GPU", report.detail)
        self.assertIn(f"profile requires {MODELS.profiles[0].requires.gpu.model} (any edition)", report.detail)

    def test_different_card_names_refuse_with_reported_and_required_names(self) -> None:
        """A card name changed by one edit selects no matching profile."""

        requirements = MODELS.profiles[0].requires
        before_last, last = requirements.gpu.model.rsplit(" ", 1)
        before_number, number = before_last.rsplit(" ", 1)
        cases = (
            ("letter in preceding word", f"{before_last}D {last}"),
            ("different number", f"{before_number} {int(number) - 1000} {last}"),
            ("word before name", f"Other {requirements.gpu.model}"),
            ("cut short", before_last),
            ("glued letters", f"{requirements.gpu.model}XYZ"),
        )
        for edit, reported in cases:
            with self.subTest(edit=edit, reported=reported):
                host = self.gpu_host(model=reported)
                report = HardwareProfileCheck().run(context(host))
                self.assertEqual(report.severity, Severity.REFUSE, report.detail)
                for position in range(requirements.gpu.count):
                    self.assertIn(
                        f"GPU index {position} model is {reported}, profile requires "
                        f"{requirements.gpu.model} (any edition)",
                        report.detail,
                    )

    def test_wrong_capability_refuses(self) -> None:
        host = self.gpu_host(
            gpu_stdout="\n".join(
                _gpu_line(uuid=f"GPU-fictitious-{position}", capability="0.0")
                for position in range(MODELS.profiles[0].requires.gpu.count)
            ) + "\n"
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
                _gpu_line(uuid=f"GPU-fictitious-{position}", vram_mib=vram_mib)
                for position in range(requirements.gpu.count)
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
        self.assertIn(f"GPU count is 1, profile requires {requirements.gpu.count}", report.detail)

    def test_surplus_card_passes_with_counts_and_unused_position(self) -> None:
        count = MODELS.profiles[0].requires.gpu.count
        host = self.gpu_host(gpu_stdout="\n".join(
            _gpu_line(uuid=f"GPU-fictitious-{position}") for position in range(count + 1)
        ))
        report = HardwareProfileCheck().run(context(host))
        self.assertEqual(report.severity, Severity.PASS, report.detail)
        self.assertIn(
            f"GPU count {count + 1}, profile requires {count}, unused GPU index {count}",
            report.detail,
        )

    def test_surplus_card_requirements_are_not_judged(self) -> None:
        requirements = MODELS.profiles[0].requires
        count = requirements.gpu.count
        judged = "\n".join(
            _gpu_line(uuid=f"GPU-fictitious-{position}") for position in range(count)
        )
        for field, changes in (
            ("model", {"model": "Fictitious Other GPU"}),
            ("capability", {"capability": "0.0"}),
            ("vram", {"vram_mib": 1}),
            ("architecture", {}),
        ):
            with self.subTest(field=field):
                surplus = _gpu_line(uuid=f"GPU-fictitious-{count}", **changes)
                architectures = [requirements.gpu.architecture] * count + [
                    "Fictitious Other Architecture" if field == "architecture"
                    else requirements.gpu.architecture
                ]
                host = self.gpu_host(
                    gpu_stdout=judged + "\n" + surplus,
                    architecture_stdout="\n".join(
                        f"Product Architecture : {architecture}" for architecture in architectures
                    ),
                )
                report = HardwareProfileCheck().run(context(host))
                self.assertEqual(report.severity, Severity.PASS, report.detail)
                self.assertIn(f"unused GPU index {count}", report.detail)
                self.assertNotIn("Fictitious Other", report.detail)

    def test_judged_card_shortfall_names_position_zero_only(self) -> None:
        count = MODELS.profiles[0].requires.gpu.count
        host = self.gpu_host(gpu_stdout="\n".join(
            _gpu_line(
                uuid=f"GPU-fictitious-{position}",
                model="Fictitious Other GPU" if position == 0 else None,
            )
            for position in range(count + 1)
        ))
        report = HardwareProfileCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE, report.detail)
        self.assertIn("GPU index 0 model is Fictitious Other GPU", report.detail)
        self.assertNotIn(f"GPU index {count}", report.detail)

    def test_non_reference_profile_warns_with_unused_position(self) -> None:
        host = self.gpu_host()
        count = MODELS.profiles[0].requires.gpu.count - 1
        models = self.models_with_count(count, name="fictitious-alternate")
        site = make_site(extra="hardware_profile: fictitious-alternate\n")
        report = HardwareProfileCheck().run(context(host, models=models, site=site))
        self.assertEqual(report.severity, Severity.WARN, report.detail)
        self.assertIn("fictitious-alternate", report.detail)
        self.assertIn(
            f"GPU count {count + 1}, profile requires {count}, unused GPU index {count}",
            report.detail,
        )

    def test_architecture_count_includes_surplus_cards(self) -> None:
        host = self.gpu_host()
        count = MODELS.profiles[0].requires.gpu.count
        architecture = MODELS.profiles[0].requires.gpu.architecture
        host.commands[NVIDIA_SMI_Q] = completed(
            NVIDIA_SMI_Q,
            "\n".join(f"Product Architecture : {architecture}" for _ in range(count - 1)),
        )
        report = HardwareProfileCheck().run(context(host, models=self.models_with_count(count - 1)))
        self.assertEqual(report.severity, Severity.REFUSE, report.detail)
        self.assertIn(f"{count - 1} architecture line(s) for {count} GPU(s)", report.detail)

    def test_record_order_selects_judged_heterogeneous_card(self) -> None:
        models = self.models_with_count(1)
        architecture = models.profiles[0].requires.gpu.architecture
        rows = (
            _gpu_line(uuid="GPU-fictitious-0"),
            _gpu_line(uuid="GPU-fictitious-1", model="Fictitious Other GPU"),
        )
        for driver_order in ((0, 1), (1, 0)):
            for recorded_order in ((0, 1), (1, 0)):
                with self.subTest(driver_order=driver_order, recorded_order=recorded_order):
                    host = self.gpu_host(
                        gpu_stdout="\n".join(rows[position] for position in driver_order),
                        architecture_stdout="\n".join(
                            f"Product Architecture : {architecture if position == 0 else 'Fictitious Other'}"
                            for position in driver_order
                        ),
                        record="gpus:\n" + "".join(
                            f"  - GPU-fictitious-{position}\n" for position in recorded_order
                        ),
                    )
                    report = HardwareProfileCheck().run(context(host, models=models))
                    if recorded_order[0] == 0:
                        self.assertEqual(report.severity, Severity.PASS, report.detail)
                        self.assertIn("unused GPU index 1", report.detail)
                        self.assertIn(f"architecture {architecture}", report.detail)
                    else:
                        self.assertEqual(report.severity, Severity.REFUSE, report.detail)
                        self.assertIn("GPU index 0 model is Fictitious Other GPU", report.detail)
                        self.assertIn("GPU index 0 architecture is Fictitious Other", report.detail)
                        self.assertNotIn("GPU index 1", report.detail)

    def test_unrecorded_host_pass_and_warn_carry_order_prefix(self) -> None:
        for name, severity in ((MODELS.profiles[0].name, Severity.PASS), ("fictitious-alternate", Severity.WARN)):
            with self.subTest(name=name):
                host = self.gpu_host(record=None)
                models = self.models_with_count(1, name=name)
                site = make_site(extra=f"hardware_profile: {name}\n")
                report = HardwareProfileCheck().run(context(host, models=models, site=site))
                self.assertEqual(report.severity, severity, report.detail)
                self.assertTrue(report.detail.startswith(
                    "unrecorded; today's order, which the first render records; "
                ))
                self.assertNotIn(str(gpus.GPU_RECORD_PATH), host.files)
                self.assertFalse(any(call[0] == "write_text" for call in host.calls))

    def test_unrecorded_host_judges_driver_order(self) -> None:
        host = self.gpu_host(
            gpu_stdout="\n".join((
                _gpu_line(uuid="GPU-fictitious-1", model="Fictitious Other GPU"),
                _gpu_line(uuid="GPU-fictitious-0"),
            )),
            record=None,
        )
        report = HardwareProfileCheck().run(context(host, models=self.models_with_count(1)))
        self.assertEqual(report.severity, Severity.REFUSE, report.detail)
        self.assertIn("GPU index 0 model is Fictitious Other GPU", report.detail)
        self.assertNotIn("GPU index 1", report.detail)

    def test_lost_recorded_card_refuses_with_re_record_fix(self) -> None:
        count = MODELS.profiles[0].requires.gpu.count
        record = "gpus:\n" + "".join(
            f"  - GPU-fictitious-{position}\n" for position in range(count)
        ) + "  - GPU-fictitious-lost\n"
        host = self.gpu_host(record=record)
        report = HardwareProfileCheck().run(context(host, models=self.models_with_count(1)))
        self.assertEqual(report.severity, Severity.REFUSE, report.detail)
        self.assertIn("GPU-fictitious-lost", report.detail)
        self.assertEqual(report.fix, gpus.re_record_fix())

    def test_malformed_record_refuses_with_its_fix(self) -> None:
        host = self.gpu_host(record="gpus: [GPU-fictitious-0, GPU-fictitious-0]\n")
        report = HardwareProfileCheck().run(context(host))
        self.assertEqual(report.severity, Severity.REFUSE, report.detail)
        self.assertIn(str(gpus.GPU_RECORD_PATH), report.detail)
        self.assertEqual(report.fix, gpus.re_record_fix())

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


class GpuMemory(unittest.TestCase):
    PROFILE = next(
        profile for profile in MODELS.profiles if profile.name == MODELS.reference
    )
    UUIDS = tuple(f"GPU-fictitious-{position}" for position in range(PROFILE.requires.gpu.count))
    TOTAL_MIB = _ceil_div(PROFILE.requires.gpu.vram_gb * GIGABYTE, MEBIBYTE)
    CONTAINER_ID = "a" * 64

    def budgets(self) -> dict[int, int]:
        totals: dict[int, int] = {}
        for model in self.PROFILE.models:
            fraction = Fraction(str(model.serve.flags[GPU_MEMORY_FLAG]))
            mib = _ceil_div(self.TOTAL_MIB * fraction.numerator, fraction.denominator)
            totals[model.gpu] = totals.get(model.gpu, 0) + mib
        return totals

    def host(
        self,
        *,
        free: Mapping[int, int] | None = None,
        processes: tuple[tuple[int, int, int], ...] = (),
        containers: tuple[tuple[str, str, str, str], ...] = (),
        cgroups: Mapping[int, str] | None = None,
        record: str | None = "valid",
        card_output: str | None = None,
        card_code: int = 0,
        process_output: str | None = None,
        process_code: int = 0,
        docker_code: int = 0,
    ) -> FakeHost:
        if card_output is None:
            card_output = "\n".join(
                f"{uuid}, {self.TOTAL_MIB}, {(free or {}).get(position, self.TOTAL_MIB)}"
                for position, uuid in enumerate(self.UUIDS)
            )
        if process_output is None:
            process_output = "\n".join(
                f"{self.UUIDS[position]}, {pid}, {used}"
                for position, pid, used in processes
            )
        listing = "\n".join("\t".join(row) for row in containers)
        files = {f"/proc/{pid}/cgroup": text for pid, text in (cgroups or {}).items()}
        if record is not None:
            files[str(gpus.GPU_RECORD_PATH)] = (
                "gpus:\n" + "".join(f"  - {uuid}\n" for uuid in self.UUIDS)
                if record == "valid" else record
            )
        return FakeHost(
            commands={
                GPU_MEMORY_CARD_ARGV: completed(GPU_MEMORY_CARD_ARGV, card_output, card_code, "driver failed"),
                GPU_MEMORY_PROCESS_ARGV: completed(
                    GPU_MEMORY_PROCESS_ARGV, process_output, process_code, "process query failed"
                ),
                GPU_MEMORY_CONTAINERS_ARGV: completed(
                    GPU_MEMORY_CONTAINERS_ARGV, listing, docker_code, "daemon unavailable"
                ),
            },
            files=files,
        )

    def run_check(
        self, host: FakeHost, *, models: ModelsLock | None = None, no_gpu: bool = False
    ) -> CheckReport:
        return GpuMemoryCheck().run(context(host, models=models, no_gpu=no_gpu))

    def lock_with(self, *models: ModelPin) -> ModelsLock:
        profile = dataclasses.replace(self.PROFILE, models=models)
        return dataclasses.replace(
            MODELS,
            profiles=tuple(
                profile if item.name == self.PROFILE.name else item for item in MODELS.profiles
            ),
        )

    def with_flags(
        self, model: ModelPin, flags: Mapping[str, str | int | float | bool]
    ) -> ModelPin:
        return dataclasses.replace(model, serve=dataclasses.replace(model.serve, flags=flags))

    def test_fresh_start_fits_on_every_card(self) -> None:
        host = self.host()
        report = self.run_check(host)
        self.assertEqual(report.severity, Severity.PASS, report.detail)
        for uuid in self.UUIDS:
            self.assertIn(uuid, report.detail)
        self.assertEqual(
            [value for name, value in host.calls if name == "run"],
            [GPU_MEMORY_CARD_ARGV, GPU_MEMORY_PROCESS_ARGV, GPU_MEMORY_CONTAINERS_ARGV],
        )
        self.assertFalse(any(name == "write_text" for name, _ in host.calls))

    def test_short_card_refuses_with_card_holders_and_budget(self) -> None:
        model = self.PROFILE.models[0]
        budget = self.budgets()[model.gpu]
        free = budget - 1
        host = self.host(free={model.gpu: free})
        report = self.run_check(host)
        self.assertEqual(report.severity, Severity.REFUSE)
        for fragment in (
            f"GPU index {model.gpu}", self.UUIDS[model.gpu], f"{free} of {self.TOTAL_MIB} MiB",
            "GIDEON holding none", "others 0 process(es), 0 MiB",
            f"{model.role} {budget} MiB", "preflight",
        ):
            self.assertIn(fragment, report.detail + report.fix)
        self.assertTrue(report.detail.startswith(f"GPU index {model.gpu}"))

    def test_own_server_holding_counts_as_available(self) -> None:
        model = self.PROFILE.models[0]
        budget = self.budgets()[model.gpu]
        free = max(1, budget // 10)
        used = budget - free
        host = self.host(
            free={model.gpu: free},
            processes=((model.gpu, 101, used),),
            containers=((self.CONTAINER_ID, "server", "gideon", "model-server"),),
            cgroups={101: f"0::/system.slice/docker-{self.CONTAINER_ID}.scope\n"},
        )
        report = self.run_check(host)
        self.assertEqual(report.severity, Severity.PASS, report.detail)
        self.assertIn(f"model-server {used} MiB", report.detail)
        self.assertIn("others 0 process(es), 0 MiB", report.detail)

        unlabeled = self.host(
            free={model.gpu: free},
            processes=((model.gpu, 102, used),),
            containers=((self.CONTAINER_ID, "gideon-server", "", "model-server"),),
            cgroups={102: f"0::/system.slice/docker-{self.CONTAINER_ID}.scope\n"},
        )
        self.assertEqual(self.run_check(unlabeled).severity, Severity.PASS)

    def test_cotenant_processes_make_a_card_short(self) -> None:
        model = self.PROFILE.models[0]
        budget = self.budgets()[model.gpu]
        free = budget - 1
        foreign_id = "b" * 64
        host = self.host(
            free={model.gpu: free},
            processes=((model.gpu, 201, 17), (model.gpu, 202, 23)),
            containers=((foreign_id, "other-service", "other-project", "other"),),
            cgroups={
                201: f"0::/system.slice/docker-{foreign_id}.scope\n",
                202: f"0::/system.slice/docker-{foreign_id}.scope\n",
            },
        )
        report = self.run_check(host)
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("others 2 process(es), 40 MiB", report.detail)
        self.assertNotIn("other-service", report.detail)

    def test_unrecorded_cards_use_todays_order_without_writing(self) -> None:
        host = self.host(record=None)
        report = self.run_check(host)
        self.assertEqual(report.severity, Severity.PASS)
        self.assertIn("unrecorded; today's order, which the first render records", report.detail)
        self.assertFalse(any(name == "write_text" for name, _ in host.calls))

    def test_missing_recorded_card_refuses_with_re_record_fix(self) -> None:
        host = self.host(card_output=f"{self.UUIDS[0]}, {self.TOTAL_MIB}, {self.TOTAL_MIB}\n")
        report = self.run_check(host)
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn(self.UUIDS[1], report.detail)
        self.assertIn("render --diff", report.fix)
        self.assertNotIn(("run", GPU_MEMORY_PROCESS_ARGV), host.calls)

    def test_malformed_record_refuses(self) -> None:
        report = self.run_check(self.host(record="gpus: []\n"))
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn(str(gpus.GPU_RECORD_PATH), report.detail)
        self.assertIn("render --diff", report.fix)

    def test_no_gpu_host_skips_without_readings(self) -> None:
        host = self.host()
        report = self.run_check(host, no_gpu=True)
        self.assertEqual(report.severity, Severity.PASS)
        self.assertIn("skipped: no-GPU host", report.detail)
        self.assertEqual(host.calls, [])

    def test_nvidia_failures_refuse_with_driver_fix(self) -> None:
        for host in (self.host(card_code=127), self.host(process_code=1)):
            with self.subTest(host=host):
                report = self.run_check(host)
                self.assertEqual(report.severity, Severity.REFUSE)
                self.assertIn("nvidia-smi", report.detail)
                self.assertIn("nvidia-driver", report.fix)

    def test_unreadable_card_or_process_memory_refuses(self) -> None:
        for host, fragment in (
            (self.host(card_output=f"{self.UUIDS[0]}, N/A, 1"), "GPU memory"),
            (self.host(process_output=f"{self.UUIDS[0]}, 123, N/A"), "per-process memory"),
        ):
            with self.subTest(fragment=fragment):
                report = self.run_check(host)
                self.assertEqual(report.severity, Severity.REFUSE)
                self.assertIn(fragment, report.detail)
                self.assertIn("nvidia-driver", report.fix)

    def test_failed_docker_listing_judges_free_alone(self) -> None:
        model = self.PROFILE.models[0]
        budget = self.budgets()[model.gpu]
        host = self.host(
            free={model.gpu: budget - 1},
            processes=((model.gpu, 301, budget),),
            containers=((self.CONTAINER_ID, "server", "gideon", "model-server"),),
            cgroups={301: f"0::/system.slice/docker-{self.CONTAINER_ID}.scope\n"},
            docker_code=1,
        )
        report = self.run_check(host)
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("judged on free memory alone", report.detail)
        self.assertIn(f"others 1 process(es), {budget} MiB", report.detail)
        self.assertNotIn(("read_text", "/proc/301/cgroup"), host.calls)

    def test_unreadable_cgroup_counts_as_other(self) -> None:
        model = self.PROFILE.models[0]
        budget = self.budgets()[model.gpu]
        host = self.host(
            free={model.gpu: budget - 1},
            processes=((model.gpu, 401, 37),),
            containers=((self.CONTAINER_ID, "server", "gideon", "model-server"),),
        )
        report = self.run_check(host)
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn("others 1 process(es), 37 MiB", report.detail)
        self.assertIn(("read_text", "/proc/401/cgroup"), host.calls)

    def test_missing_fraction_refuses_before_driver_read(self) -> None:
        model = self.PROFILE.models[0]
        flags = {key: value for key, value in model.serve.flags.items() if key != GPU_MEMORY_FLAG}
        changed_lock = self.lock_with(self.with_flags(model, flags), *self.PROFILE.models[1:])
        host = self.host()
        report = self.run_check(host, models=changed_lock)
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn(f"models.{model.role}.serve.flags.{GPU_MEMORY_FLAG}", report.detail)
        self.assertIn("docs/runbooks/release-files.md §4", report.fix)
        self.assertEqual(host.calls, [])

    def test_out_of_range_fraction_refuses_before_driver_read(self) -> None:
        model = self.PROFILE.models[0]
        for value in (0, 1.1, True):
            with self.subTest(value=value):
                flags = {**model.serve.flags, GPU_MEMORY_FLAG: value}
                changed_lock = self.lock_with(self.with_flags(model, flags), *self.PROFILE.models[1:])
                host = self.host()
                report = self.run_check(host, models=changed_lock)
                self.assertEqual(report.severity, Severity.REFUSE)
                self.assertIn(GPU_MEMORY_FLAG, report.detail)
                self.assertEqual(host.calls, [])

    def test_model_position_past_resolved_cards_refuses(self) -> None:
        model = dataclasses.replace(self.PROFILE.models[0], gpu=len(self.UUIDS))
        changed_lock = self.lock_with(model, *self.PROFILE.models[1:])
        report = self.run_check(self.host(), models=changed_lock)
        self.assertEqual(report.severity, Severity.REFUSE)
        self.assertIn(f"GPU index {len(self.UUIDS)}", report.detail)
        self.assertIn(f"{len(self.UUIDS)} card(s)", report.detail)
        self.assertIn("render --diff", report.fix)

    def test_card_without_a_server_is_not_judged_or_named(self) -> None:
        model = self.PROFILE.models[0]
        unused = next(position for position in range(len(self.UUIDS)) if position != model.gpu)
        report = self.run_check(self.host(free={unused: 0}), models=self.lock_with(model))
        self.assertEqual(report.severity, Severity.PASS, report.detail)
        self.assertIn(self.UUIDS[model.gpu], report.detail)
        self.assertNotIn(self.UUIDS[unused], report.detail)

    def test_committed_fractions_set_the_exact_budget_boundary(self) -> None:
        budgets = self.budgets()
        at_budget = self.run_check(self.host(free=budgets))
        self.assertEqual(at_budget.severity, Severity.PASS, at_budget.detail)
        for position, budget in budgets.items():
            self.assertIn(f"{budget} MiB", at_budget.detail)
            short = dict(budgets)
            short[position] -= 1
            report = self.run_check(self.host(free=short))
            self.assertEqual(report.severity, Severity.REFUSE, report.detail)
            self.assertTrue(report.detail.startswith(f"GPU index {position}"))


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
