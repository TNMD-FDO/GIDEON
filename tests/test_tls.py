"""TLS contracts: material validation, the ingress probe, and `tls reload`."""

import argparse
import contextlib
import io
import os
import subprocess
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from gideon.host import tls
from gideon.host.stack import compose_argv
from gideon.host.sysio import Command, PathLike

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config/site.example.yaml"
HOSTNAME = "gideon.example.org"
CERT, KEY, CA = tls.CERT_PATH, tls.KEY_PATH, tls.CA_PATH
RENDERED = "/etc/gideon/rendered"
PUBKEY = "-----BEGIN PUBLIC KEY-----\nMIIB\n-----END PUBLIC KEY-----\n"
LEAF = "-----BEGIN CERTIFICATE-----\nMIIC\n-----END CERTIFICATE-----\n"
FINGERPRINT = "sha256 Fingerprint=81:2C:7A:46:FB:2D:27:AA\n"

Outcome = subprocess.CompletedProcess[str] | list[subprocess.CompletedProcess[str]]


def done(argv: tuple[str, ...], rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), rc, stdout, stderr)


X509_PARSE = ("openssl", "x509", "-in", CERT, "-noout")
KEY_PARSE = ("openssl", "pkey", "-in", KEY, "-noout", "-passin", "pass:")
X509_PUB = ("openssl", "x509", "-in", CERT, "-noout", "-pubkey")
KEY_PUB = ("openssl", "pkey", "-in", KEY, "-pubout", "-passin", "pass:")
VERIFY = ("openssl", "verify", "-CAfile", CA, CERT)
CHECKHOST = ("openssl", "x509", "-in", CERT, "-noout", "-checkhost", HOSTNAME)
CHECKEND = ("openssl", "x509", "-in", CERT, "-noout", "-checkend", "0")
HANDSHAKE = ("openssl", "s_client", "-connect", "127.0.0.1:443", "-servername", HOSTNAME, "-verify_hostname", HOSTNAME, "-CAfile", CA, "-verify_return_error")
SERVED_FP = ("openssl", "x509", "-noout", "-fingerprint", "-sha256")
PLACED_FP = ("openssl", "x509", "-in", CERT, "-noout", "-fingerprint", "-sha256")
SERVED_ENDDATE = ("openssl", "x509", "-noout", "-enddate")
RECREATE = tuple(compose_argv(RENDERED, "up", "-d", "--no-deps", "--force-recreate", "caddy"))


def valid_material() -> dict[tuple[str, ...], Outcome]:
    return {
        X509_PARSE: done(X509_PARSE),
        KEY_PARSE: done(KEY_PARSE),
        X509_PUB: done(X509_PUB, stdout=PUBKEY),
        KEY_PUB: done(KEY_PUB, stdout=PUBKEY),
        VERIFY: done(VERIFY, stdout=f"{CERT}: OK\n"),
        CHECKHOST: done(CHECKHOST, stdout=f"Hostname {HOSTNAME} does match certificate\n"),
        CHECKEND: done(CHECKEND, stdout="Certificate will not expire\n"),
    }


def healthy_ingress() -> dict[tuple[str, ...], Outcome]:
    return {
        HANDSHAKE: done(HANDSHAKE, stdout=f"CONNECTED\n{LEAF}---\nVerify return code: 0 (ok)\n"),
        SERVED_FP: done(SERVED_FP, stdout=FINGERPRINT),
        PLACED_FP: done(PLACED_FP, stdout=FINGERPRINT),
    }


class FakeHost:
    """Commands map argv → an outcome or a queue of outcomes; files is the set of existing paths."""

    def __init__(self, commands: Mapping[tuple[str, ...], Outcome], *, files: Mapping[str, str] | None = None, euid: int = 0) -> None:
        self.commands: dict[tuple[str, ...], Outcome] = dict(commands)
        self.files = dict(files or {})
        self.euid = euid
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.reads: list[str] = []

    def run(self, argv: Command, *, check: bool = False, input: str | None = None, cwd: PathLike | None = None, env: Mapping[str, str] | None = None, timeout: float | None = None, passthrough: bool = False) -> subprocess.CompletedProcess[str]:
        del check, cwd, env, timeout
        command = tuple(argv)
        self.calls.append((command, input))
        outcome = self.commands.get(command)
        if isinstance(outcome, list):
            return outcome.pop(0) if len(outcome) > 1 else outcome[0]
        if outcome is None:
            return subprocess.CompletedProcess(list(command), 127, "", "")
        return outcome

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        key = os.fspath(path)
        self.reads.append(key)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644) -> None:
        self.files[os.fspath(path)] = text

    def exists(self, path: PathLike) -> bool:
        return os.fspath(path) in self.files

    def listdir(self, path: PathLike) -> list[str]:
        raise NotImplementedError

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        raise NotImplementedError

    def stat(self, path: PathLike) -> os.stat_result:
        raise NotImplementedError

    def chmod(self, path: PathLike, mode: int) -> None:
        raise NotImplementedError

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        raise NotImplementedError

    def mkdir(self, path: PathLike, *, mode: int = 0o755, parents: bool = False, exist_ok: bool = False) -> None:
        raise NotImplementedError

    def geteuid(self) -> int:
        return self.euid


def material_files() -> dict[str, str]:
    return {CERT: "cert", KEY: "<never read>", CA: "ca"}


class Material(unittest.TestCase):
    def test_valid_material_passes_without_reading_the_key(self) -> None:
        host = FakeHost(valid_material(), files=material_files())
        self.assertEqual(tls.validate_material(host, HOSTNAME), ())
        self.assertEqual(host.reads, [])
        self.assertTrue(all(call[1] == "" for call in host.calls), "openssl must never wait on a terminal")

    def test_missing_file_names_its_fixed_home(self) -> None:
        files = material_files()
        del files[KEY]
        errors = tls.validate_material(FakeHost(valid_material(), files=files), HOSTNAME)
        self.assertEqual(len(errors), 1)
        self.assertIn(KEY, errors[0])
        self.assertIn("Fix:", errors[0])

    def test_each_openssl_failure_is_a_refusal(self) -> None:
        cases = {
            "cert parse": (X509_PARSE, done(X509_PARSE, 1, stderr="unable to load certificate")),
            "key parse": (KEY_PARSE, done(KEY_PARSE, 1, stderr="unable to load key")),
            "chain": (VERIFY, done(VERIFY, 2, stderr="unable to get local issuer certificate")),
            "hostname": (CHECKHOST, done(CHECKHOST, 1, stdout=f"Hostname {HOSTNAME} does NOT match certificate\n")),
            "expired": (CHECKEND, done(CHECKEND, 1, stdout="Certificate will expire\n")),
        }
        for label, (argv, outcome) in cases.items():
            with self.subTest(case=label):
                commands = valid_material()
                commands[argv] = outcome
                errors = tls.validate_material(FakeHost(commands, files=material_files()), HOSTNAME)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("Fix:", errors[0])

    def test_public_key_mismatch_names_both_files(self) -> None:
        commands = valid_material()
        commands[KEY_PUB] = done(KEY_PUB, stdout=PUBKEY.replace("MIIB", "MIIZ"))
        errors = tls.validate_material(FakeHost(commands, files=material_files()), HOSTNAME)
        self.assertEqual(len(errors), 1)
        self.assertIn(CERT, errors[0])
        self.assertIn(KEY, errors[0])

    def test_absent_openssl_names_the_package(self) -> None:
        errors = tls.validate_material(FakeHost({}, files=material_files()), HOSTNAME)
        self.assertTrue(errors)
        self.assertIn("apt-get install -y openssl", errors[0])


class Probe(unittest.TestCase):
    def test_public_fingerprint_helpers_use_the_bounded_probe_vectors(self) -> None:
        commands = healthy_ingress()
        custom_handshake = (
            "openssl",
            "s_client",
            "-connect",
            "192.168.122.10:443",
            "-servername",
            HOSTNAME,
            "-verify_hostname",
            HOSTNAME,
            "-CAfile",
            "/tmp/acceptance-ca.pem",
            "-verify_return_error",
        )
        commands[custom_handshake] = done(
            custom_handshake,
            stdout=f"CONNECTED\n{LEAF}---\nVerify return code: 0 (ok)\n",
        )
        host = FakeHost(commands)
        served = tls.served_fingerprint(
            host,
            connect="192.168.122.10:443",
            hostname=HOSTNAME,
            cafile="/tmp/acceptance-ca.pem",
        )
        self.assertEqual(served, "812c7a46fb2d27aa")
        self.assertEqual(
            host.calls[0][0],
            custom_handshake,
        )
        placed = tls.file_fingerprint(host, CERT)
        self.assertEqual(placed, "812c7a46fb2d27aa")
        self.assertEqual(host.calls[-1][0], PLACED_FP)

    def test_public_fingerprint_helpers_return_problems_for_bad_fingerprints(self) -> None:
        commands = healthy_ingress()
        commands[SERVED_FP] = done(SERVED_FP, stdout="not a fingerprint\n")
        result = tls.served_fingerprint(
            FakeHost(commands),
            connect="192.168.122.10:443",
            hostname=HOSTNAME,
            cafile=CA,
        )
        self.assertIsInstance(result, tls.Problem)

        commands = healthy_ingress()
        commands[PLACED_FP] = done(PLACED_FP, stdout="not a fingerprint\n")
        result = tls.file_fingerprint(FakeHost(commands), CERT)
        self.assertIsInstance(result, tls.Problem)

    def test_served_leaf_matches_placed_certificate(self) -> None:
        host = FakeHost(healthy_ingress())
        result = tls.probe_ingress(host, HOSTNAME, sleep=lambda _: None)
        self.assertTrue(result.ok, result)
        self.assertIn(CERT, result.detail)
        self.assertEqual(host.calls[1], (SERVED_FP, LEAF.rstrip("\n")))

    def test_slow_start_is_retried_then_passes(self) -> None:
        commands = healthy_ingress()
        ok = commands[HANDSHAKE]
        assert not isinstance(ok, list)
        commands[HANDSHAKE] = [done(HANDSHAKE, 1, stderr="connect:errno=111"), done(HANDSHAKE, 1, stderr="connect:errno=111"), ok]
        naps: list[float] = []
        result = tls.probe_ingress(FakeHost(commands), HOSTNAME, attempts=5, sleep=naps.append)
        self.assertTrue(result.ok)
        self.assertEqual(len(naps), 2)

    def test_exhausted_attempts_fail_with_the_logs_fix(self) -> None:
        commands = healthy_ingress()
        commands[HANDSHAKE] = done(HANDSHAKE, 1, stderr="connect:errno=111")
        naps: list[float] = []
        result = tls.probe_ingress(FakeHost(commands), HOSTNAME, attempts=3, sleep=naps.append)
        self.assertFalse(result.ok)
        self.assertIn("after 3 attempts", result.detail)
        self.assertIn("logs caddy", result.fix)
        self.assertEqual(len(naps), 2)

    def test_fingerprint_mismatch_is_not_retried(self) -> None:
        commands = healthy_ingress()
        commands[PLACED_FP] = done(PLACED_FP, stdout=FINGERPRINT.replace("81:2C", "00:00"))
        naps: list[float] = []
        host = FakeHost(commands)
        result = tls.probe_ingress(host, HOSTNAME, attempts=5, sleep=naps.append)
        self.assertFalse(result.ok)
        self.assertIn("does not match", result.detail)
        self.assertEqual(naps, [])
        self.assertEqual(sum(call[0] == HANDSHAKE for call in host.calls), 1)

    def test_fingerprint_timeout_is_a_probe_failure_not_a_traceback(self) -> None:
        class TimingOutHost(FakeHost):
            def run(self, argv: Command, **kwargs: object) -> subprocess.CompletedProcess[str]:
                if tuple(argv) == SERVED_FP:
                    raise subprocess.TimeoutExpired(list(argv), 30)
                return super().run(argv, **kwargs)  # type: ignore[arg-type]

        result = tls.probe_ingress(TimingOutHost(healthy_ingress()), HOSTNAME, sleep=lambda _: None)
        self.assertFalse(result.ok)
        self.assertIn("openssl", result.detail)

    def test_handshake_without_a_certificate_fails(self) -> None:
        commands = healthy_ingress()
        commands[HANDSHAKE] = done(HANDSHAKE, 0, stdout="CONNECTED\n")
        result = tls.probe_ingress(FakeHost(commands), HOSTNAME, sleep=lambda _: None)
        self.assertFalse(result.ok)


class ServedExpiry(unittest.TestCase):
    def test_handshake_and_x509_stdin_return_aware_utc_expiry(self) -> None:
        handshake = (
            "openssl", "s_client", "-connect", "192.0.2.12:443", "-servername",
            HOSTNAME, "-verify_hostname", HOSTNAME, "-CAfile", "/tmp/fake-ca.pem",
            "-verify_return_error",
        )
        host = FakeHost(
            {
                handshake: done(handshake, stdout=f"CONNECTED\n{LEAF}---\n"),
                SERVED_ENDDATE: done(SERVED_ENDDATE, stdout="notAfter=Jan  2 03:04:05 2099 GMT\n"),
            }
        )
        result = tls.served_expiry(
            host,
            connect="192.0.2.12:443",
            hostname=HOSTNAME,
            cafile="/tmp/fake-ca.pem",
        )
        self.assertEqual(result, datetime(2099, 1, 2, 3, 4, 5, tzinfo=UTC))
        self.assertEqual(host.calls[0][0], handshake)
        self.assertEqual(host.calls[1], (SERVED_ENDDATE, LEAF.rstrip("\n")))

    def test_handshake_missing_certificate_and_bad_expiry_return_problems(self) -> None:
        handshake = HANDSHAKE
        for stdout, enddate, expected in (
            ("CONNECTED\n", None, "no certificate"),
            (f"CONNECTED\n{LEAF}---\n", "notAfter=unparseable\n", "invalid expiry"),
            (f"CONNECTED\n{LEAF}---\n", "", "no expiry date"),
        ):
            with self.subTest(expected=expected):
                commands: dict[tuple[str, ...], Outcome] = {
                    handshake: done(handshake, stdout=stdout),
                }
                if enddate is not None:
                    commands[SERVED_ENDDATE] = done(SERVED_ENDDATE, stdout=enddate)
                result = tls.served_expiry(
                    FakeHost(commands),
                    connect="127.0.0.1:443",
                    hostname=HOSTNAME,
                    cafile=CA,
                )
                self.assertIsInstance(result, tls.Problem)
                assert isinstance(result, tls.Problem)
                self.assertIn(expected, result.problem)
                self.assertTrue(result.fix)

    def test_handshake_failure_preserves_caddy_fix(self) -> None:
        result = tls.served_expiry(
            FakeHost({HANDSHAKE: done(HANDSHAKE, rc=1, stderr="connection refused")}),
            connect="127.0.0.1:443",
            hostname=HOSTNAME,
            cafile=CA,
        )
        self.assertIsInstance(result, tls.Problem)
        assert isinstance(result, tls.Problem)
        self.assertIn("handshake failed", result.problem)
        self.assertIn("logs caddy", result.fix)


def reload(host: FakeHost) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = tls.run_tls_reload(argparse.Namespace(), host=host, site_path="/tmp/site.yaml", rendered_dir=RENDERED)
    return code, out.getvalue(), err.getvalue()


def reload_files(*, compose: bool = True) -> dict[str, str]:
    files = material_files()
    files["/tmp/site.yaml"] = EXAMPLE.read_text()
    if compose:
        files[f"{RENDERED}/compose.yaml"] = "name: gideon\n"
    return files


class Reload(unittest.TestCase):
    def test_root_required(self) -> None:
        code, _, err = reload(FakeHost({}, files=reload_files(), euid=1000))
        self.assertEqual(code, 1)
        self.assertIn("Fix:", err)

    def test_missing_rendered_compose_points_at_apply(self) -> None:
        code, _, err = reload(FakeHost({}, files=reload_files(compose=False)))
        self.assertEqual(code, 1)
        self.assertIn("gideon apply", err)

    def test_invalid_material_stops_before_caddy_is_touched(self) -> None:
        commands = valid_material()
        commands[CHECKEND] = done(CHECKEND, 1, stdout="Certificate will expire\n")
        host = FakeHost(commands, files=reload_files())
        code, _, err = reload(host)
        self.assertEqual(code, 1)
        self.assertIn("Fix:", err)
        self.assertNotIn(RECREATE, [call[0] for call in host.calls])

    def test_recreate_failure_prints_the_logs_fix(self) -> None:
        commands = {**valid_material(), RECREATE: done(RECREATE, 1, stderr="no such service")}
        code, out, _ = reload(FakeHost(commands, files=reload_files()))
        self.assertEqual(code, 1)
        self.assertIn("logs caddy", out)

    def test_success_reports_three_stages(self) -> None:
        commands = {**valid_material(), **healthy_ingress(), RECREATE: done(RECREATE)}
        host = FakeHost(commands, files=reload_files())
        code, out, err = reload(host)
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual([line.split(":")[0] for line in lines], ["material", "recreate", "ingress"])
        self.assertTrue(all(": ok" in line for line in lines))
        self.assertIn(RECREATE, [call[0] for call in host.calls])


class Stack(unittest.TestCase):
    def test_compose_argv_is_pinned(self) -> None:
        self.assertEqual(
            compose_argv("/etc/gideon/rendered", "ps", "--all"),
            ["docker", "compose", "--project-directory", "/etc/gideon/rendered", "-f", "/etc/gideon/rendered/compose.yaml", "ps", "--all"],
        )
