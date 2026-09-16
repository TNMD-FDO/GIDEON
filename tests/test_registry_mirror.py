"""Registry mirror contracts: destination, skopeo argv, idempotence, refusals (spec §2.4)."""

import argparse
import contextlib
import io
import os
import subprocess
import unittest
from collections.abc import Mapping
from pathlib import Path

from gideon.host.images import BuiltImagePin, MirroredImagePin, load_image_lock
from gideon.host.registry import run_registry_mirror
from gideon.host.sysio import Command, PathLike

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config/site.example.yaml"
SECOND = ROOT / "tests/fixtures/site/second-office.yaml"
SITE = "/tmp/site.yaml"
# Every expectation derives from the committed lock, so a pin-watch bump
# (ADR-0031) never breaks these contracts.
_COMMITTED_LOCK = load_image_lock(ROOT / "images.lock").lock
assert _COMMITTED_LOCK is not None, "the committed images.lock must load"
_PINS = {pin.name: pin for pin in _COMMITTED_LOCK.images}
DIGEST = _PINS["caddy"].digest
OPEN_WEBUI_DIGEST = _PINS["open-webui"].digest


def mirrored(name: str) -> MirroredImagePin:
    pin = _PINS[name]
    assert isinstance(pin, MirroredImagePin)
    return pin


def built(name: str) -> BuiltImagePin:
    pin = _PINS[name]
    assert isinstance(pin, BuiltImagePin)
    return pin


# The committed postgres pin is built (ticket 16): the mirror probes its base
# and its built digest, both derived from the loaded lock.
POSTGRES_BASE_DIGEST = built("postgres").base_digest
POSTGRES_DIGEST = built("postgres").digest


CADDY_SOURCE = mirrored("caddy").source
CADDY_REPOSITORY, CADDY_TAG = CADDY_SOURCE.rsplit(":", 1)
LOOPBACK_REF = f"127.0.0.1:5000/caddy@{DIGEST}"
PUBLIC_REF = f"ghcr.io/tnmd-fdo/caddy@{DIGEST}"
SKOPEO_VERSION = ("skopeo", "--version")
PROBE_LOOPBACK = ("docker", "manifest", "inspect", "--insecure", LOOPBACK_REF)
PROBE_PUBLIC = ("docker", "manifest", "inspect", PUBLIC_REF)
COPY_LOOPBACK = (
    "skopeo", "copy", "--all", "--preserve-digests", "--dest-tls-verify=false",
    f"docker://{CADDY_REPOSITORY}@{DIGEST}", f"docker://127.0.0.1:5000/caddy:{CADDY_TAG}",
)
COPY_PUBLIC = (
    "skopeo", "copy", "--all", "--preserve-digests",
    f"docker://{CADDY_REPOSITORY}@{DIGEST}", f"docker://ghcr.io/tnmd-fdo/caddy:{CADDY_TAG}",
)

LOCK_IMAGES = tuple(
    (pin.name, pin.digest, pin.source)
    for pin in _COMMITTED_LOCK.images
    if isinstance(pin, MirroredImagePin)
)
BUILT_BASE_DIGEST = "sha256:" + "a" * 64
BUILT_DIGEST = "sha256:" + "b" * 64
BUILT_LOCK_PATH = "/tmp/built-images.lock"
BUILT_LOCK_TEXT = (
    "version: 1\n"
    "images:\n"
    "  postgres:\n"
    "    build: images/postgres\n"
    "    base: docker.io/library/example:18\n"
    f"    base_digest: {BUILT_BASE_DIGEST}\n"
    "    build_args: {}\n"
    "    watch: {}\n"
    f"    inputs_digest: {BUILT_DIGEST}\n"
    f"    digest: {BUILT_DIGEST}\n"
)

Outcome = subprocess.CompletedProcess[str] | list[subprocess.CompletedProcess[str]]


def done(argv: tuple[str, ...], rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), rc, stdout, stderr)


class FakeHost:
    def __init__(self, commands: Mapping[tuple[str, ...], Outcome], files: Mapping[str, str] | None = None, *, denied: frozenset[str] = frozenset()) -> None:
        self.commands: dict[tuple[str, ...], Outcome] = dict(commands)
        self.files = dict(files or {})
        self.denied = denied
        self.calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []

    def run(self, argv: Command, *, check: bool = False, input: str | None = None, cwd: PathLike | None = None, env: Mapping[str, str] | None = None, timeout: float | None = None, passthrough: bool = False) -> subprocess.CompletedProcess[str]:
        del check, input, cwd, timeout
        command = tuple(argv)
        self.calls.append((command, env))
        outcome = self.commands.get(command)
        if isinstance(outcome, list):
            return outcome.pop(0) if len(outcome) > 1 else outcome[0]
        if outcome is None:
            return subprocess.CompletedProcess(list(command), 127, "", "")
        return outcome

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        key = os.fspath(path)
        if key in self.denied:
            raise PermissionError(key)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644) -> None:
        raise NotImplementedError

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
        return 1000


def files(site: Path | None = EXAMPLE) -> dict[str, str]:
    result = {str(ROOT / "images.lock"): (ROOT / "images.lock").read_text()}
    if site is not None:
        result[SITE] = site.read_text()
    return result


def mirror(
    host: FakeHost,
    to: str | None = None,
    *,
    images_path: str | None = None,
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = run_registry_mirror(
            argparse.Namespace(to=to),
            host=host,
            site_path=SITE,
            images_path=images_path,
            root=ROOT,
        )
    return code, out.getvalue(), err.getvalue()


def argv_calls(host: FakeHost) -> list[tuple[str, ...]]:
    return [call[0] for call in host.calls]


def image_ref(destination: str, name: str, digest: str) -> str:
    return f"{destination}/{name}@{digest}"


# The test's own statement of which destinations are plain HTTP (loopback and
# the libvirt bridge, [29] item 13) — never derived from the code under test.
_PLAIN_PREFIXES = ("127.", "192.168.122.")


def plain(destination: str) -> bool:
    return destination.startswith(_PLAIN_PREFIXES)


def image_probe(destination: str, name: str, digest: str) -> tuple[str, ...]:
    ref = image_ref(destination, name, digest)
    return (
        "docker",
        "manifest",
        "inspect",
        *(("--insecure",) if plain(destination) else ()),
        ref,
    )


def image_copy(destination: str, name: str, digest: str, source: str) -> tuple[str, ...]:
    argv = ["skopeo", "copy", "--all", "--preserve-digests"]
    if plain(destination):
        argv.append("--dest-tls-verify=false")
    repository, tag = source.rsplit(":", 1)
    argv.extend(
        [
            f"docker://{repository}@{digest}",
            f"docker://{destination}/{name}:{tag}",
        ]
    )
    return tuple(argv)


def present_probes(destination: str) -> dict[tuple[str, ...], Outcome]:
    """Every committed pin answers present: mirrored digests, and a built pin's base and digest."""

    probes = [(name, digest) for name, digest, _source in LOCK_IMAGES]
    for pin in _PINS.values():
        if isinstance(pin, BuiltImagePin):
            probes.extend(((pin.name, pin.base_digest), (pin.name, pin.digest)))
    return {
        image_probe(destination, name, digest): done(
            image_probe(destination, name, digest), stdout="{}"
        )
        for name, digest in probes
    }


def built_files(lock_text: str = BUILT_LOCK_TEXT) -> dict[str, str]:
    result = files()
    result[BUILT_LOCK_PATH] = lock_text
    return result


def built_probe(destination: str, digest: str, name: str = "postgres") -> tuple[str, ...]:
    return image_probe(destination, name, digest)


def base_probe(destination: str) -> tuple[str, ...]:
    return image_probe(destination, "postgres", BUILT_BASE_DIGEST)


def base_copy(destination: str) -> tuple[str, ...]:
    return (
        "skopeo",
        "copy",
        "--all",
        "--preserve-digests",
        *(("--dest-tls-verify=false",) if plain(destination) else ()),
        f"docker://docker.io/library/example@{BUILT_BASE_DIGEST}",
        f"docker://{destination}/postgres:18",
    )


class Mirroring(unittest.TestCase):
    def test_absent_digest_is_copied_by_digest_then_verified(self) -> None:
        probes = present_probes("127.0.0.1:5000")
        caddy_probe = image_probe("127.0.0.1:5000", "caddy", DIGEST)
        caddy_copy = image_copy(
            "127.0.0.1:5000", "caddy", DIGEST, CADDY_SOURCE
        )
        probes[caddy_probe] = [
            done(caddy_probe, 1, stderr="manifest unknown"),
            done(caddy_probe, stdout="{}"),
        ]
        host = FakeHost(
            {
                SKOPEO_VERSION: done(SKOPEO_VERSION, stdout="skopeo version 1.21.0\n"),
                **probes,
                caddy_copy: done(caddy_copy),
            },
            files(),
        )
        code, out, err = mirror(host, to="127.0.0.1:5000")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"caddy: mirrored — {LOOPBACK_REF}\n", out)
        # Every other mirrored pin is probed once in lock order, then the
        # built pin's base and digest — derived, so a new pin never moves this.
        self.assertEqual(
            argv_calls(host),
            [
                SKOPEO_VERSION,
                caddy_probe,
                caddy_copy,
                caddy_probe,
                *(
                    image_probe("127.0.0.1:5000", name, digest)
                    for name, digest, _source in LOCK_IMAGES
                    if name != "caddy"
                ),
                image_probe("127.0.0.1:5000", "postgres", POSTGRES_BASE_DIGEST),
                image_probe("127.0.0.1:5000", "postgres", POSTGRES_DIGEST),
            ],
        )

    def test_present_digest_is_not_copied(self) -> None:
        host = FakeHost(
            {SKOPEO_VERSION: done(SKOPEO_VERSION), **present_probes("127.0.0.1:5000")},
            files(),
        )
        code, out, _ = mirror(host, to="127.0.0.1:5000")
        self.assertEqual(code, 0)
        self.assertIn(f"caddy: present — {LOOPBACK_REF}\n", out)
        self.assertNotIn(COPY_LOOPBACK, argv_calls(host))

    def test_public_destination_keeps_tls_verification(self) -> None:
        probes = present_probes("ghcr.io/tnmd-fdo")
        caddy_probe = image_probe("ghcr.io/tnmd-fdo", "caddy", DIGEST)
        caddy_copy = image_copy(
            "ghcr.io/tnmd-fdo", "caddy", DIGEST, CADDY_SOURCE
        )
        probes[caddy_probe] = [done(caddy_probe, 1), done(caddy_probe)]
        host = FakeHost(
            {
                SKOPEO_VERSION: done(SKOPEO_VERSION),
                **probes,
                caddy_copy: done(caddy_copy),
            },
            files(),
        )
        code, out, _ = mirror(host, to="ghcr.io/tnmd-fdo")
        self.assertEqual(code, 0, out)
        self.assertIn(caddy_copy, argv_calls(host))

    def test_bridge_destination_uses_plain_http_for_probe_and_copy(self) -> None:
        destination = "192.168.122.1:5000"
        probes = present_probes(destination)
        caddy_probe = image_probe(destination, "caddy", DIGEST)
        caddy_copy = image_copy(destination, "caddy", DIGEST, CADDY_SOURCE)
        probes[caddy_probe] = [done(caddy_probe, 1), done(caddy_probe)]
        host = FakeHost(
            {
                SKOPEO_VERSION: done(SKOPEO_VERSION),
                **probes,
                caddy_copy: done(caddy_copy),
            },
            files(),
        )
        code, out, _ = mirror(host, to=destination)
        self.assertEqual(code, 0, out)
        self.assertIn("--insecure", caddy_probe)
        self.assertIn("--dest-tls-verify=false", caddy_copy)

    def test_hostname_destination_keeps_tls_verification(self) -> None:
        destination = "registry.example:5000"
        probes = present_probes(destination)
        caddy_probe = image_probe(destination, "caddy", DIGEST)
        caddy_copy = image_copy(destination, "caddy", DIGEST, CADDY_SOURCE)
        probes[caddy_probe] = [done(caddy_probe, 1), done(caddy_probe)]
        host = FakeHost(
            {
                SKOPEO_VERSION: done(SKOPEO_VERSION),
                **probes,
                caddy_copy: done(caddy_copy),
            },
            files(),
        )
        code, out, _ = mirror(host, to=destination)
        self.assertEqual(code, 0, out)
        self.assertNotIn("--insecure", caddy_probe)
        self.assertNotIn("--dest-tls-verify=false", caddy_copy)

    def test_destination_defaults_to_the_site_registry(self) -> None:
        host = FakeHost(
            {SKOPEO_VERSION: done(SKOPEO_VERSION), **present_probes("127.0.0.1:5000")},
            files(SECOND),
        )
        code, out, _ = mirror(host)
        self.assertEqual(code, 0)
        self.assertIn("127.0.0.1:5000/caddy", out)

    def test_failed_copy_reports_detail_and_exits_1(self) -> None:
        probes = present_probes("127.0.0.1:5000")
        caddy_probe = image_probe("127.0.0.1:5000", "caddy", DIGEST)
        caddy_copy = image_copy(
            "127.0.0.1:5000", "caddy", DIGEST, CADDY_SOURCE
        )
        probes[caddy_probe] = done(caddy_probe, 1)
        host = FakeHost(
            {
                SKOPEO_VERSION: done(SKOPEO_VERSION),
                **probes,
                caddy_copy: done(caddy_copy, 1, stderr="reading manifest: unauthorized"),
            },
            files(),
        )
        code, out, _ = mirror(host, to="127.0.0.1:5000")
        self.assertEqual(code, 1)
        self.assertIn("caddy: failed — reading manifest: unauthorized Fix:", out)

    def test_built_pin_copies_base_then_probes_built_digest(self) -> None:
        base_absent = base_probe("127.0.0.1:5000")
        built = built_probe("127.0.0.1:5000", BUILT_DIGEST)
        copy = base_copy("127.0.0.1:5000")
        host = FakeHost(
            {
                SKOPEO_VERSION: done(SKOPEO_VERSION),
                base_absent: [done(base_absent, 1, stderr="manifest unknown"), done(base_absent)],
                copy: done(copy),
                built: done(built),
            },
            built_files(),
        )
        code, out, err = mirror(host, to="127.0.0.1:5000", images_path=BUILT_LOCK_PATH)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("postgres/base: mirrored", out)
        self.assertIn("postgres: present", out)
        self.assertEqual(
            argv_calls(host),
            [SKOPEO_VERSION, base_absent, copy, base_absent, built],
        )

    def test_built_pin_with_present_base_does_not_copy(self) -> None:
        base = base_probe("127.0.0.1:5000")
        built = built_probe("127.0.0.1:5000", BUILT_DIGEST)
        host = FakeHost(
            {
                SKOPEO_VERSION: done(SKOPEO_VERSION),
                base: done(base),
                built: done(built),
            },
            built_files(),
        )
        code, out, err = mirror(host, to="127.0.0.1:5000", images_path=BUILT_LOCK_PATH)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("postgres/base: present", out)
        self.assertIn("postgres: present", out)
        self.assertNotIn("skopeo", " ".join(" ".join(call) for call in argv_calls(host)[1:]))

    def test_built_pin_digest_absent_reports_build_fix_without_copy(self) -> None:
        base = base_probe("127.0.0.1:5000")
        built = built_probe("127.0.0.1:5000", BUILT_DIGEST)
        host = FakeHost(
            {
                SKOPEO_VERSION: done(SKOPEO_VERSION),
                base: done(base),
                built: done(built, 1, stderr="manifest unknown"),
            },
            built_files(),
        )
        code, out, err = mirror(host, to="127.0.0.1:5000", images_path=BUILT_LOCK_PATH)
        self.assertEqual((code, err), (1, ""))
        self.assertIn("postgres: failed", out)
        self.assertIn("python3 -m tools.imagebuild postgres --to 127.0.0.1:5000", out)
        self.assertNotIn("skopeo", " ".join(" ".join(call) for call in argv_calls(host)[2:]))

    def test_built_base_failure_does_not_skip_built_probe(self) -> None:
        base = base_probe("127.0.0.1:5000")
        built = built_probe("127.0.0.1:5000", BUILT_DIGEST)
        host = FakeHost(
            {
                SKOPEO_VERSION: done(SKOPEO_VERSION),
                base: done(base, 1, stderr="manifest unavailable"),
                base_copy("127.0.0.1:5000"): done(base_copy("127.0.0.1:5000"), 1, stderr="copy failed"),
                built: done(built),
            },
            built_files(),
        )
        code, out, err = mirror(host, to="127.0.0.1:5000", images_path=BUILT_LOCK_PATH)
        self.assertEqual((code, err), (1, ""))
        self.assertIn("postgres/base: failed", out)
        self.assertIn("postgres: present", out)
        self.assertIn(built, argv_calls(host))

    def test_unbuilt_pin_reports_build_fix_without_probing(self) -> None:
        base = base_probe("127.0.0.1:5000")
        host = FakeHost(
            {SKOPEO_VERSION: done(SKOPEO_VERSION), base: done(base)},
            built_files(BUILT_LOCK_TEXT.replace(BUILT_DIGEST, "unbuilt")),
        )
        code, out, err = mirror(host, to="127.0.0.1:5000", images_path=BUILT_LOCK_PATH)
        self.assertEqual((code, err), (1, ""))
        # The base is mirrored first so the first build can start FROM it.
        self.assertIn("postgres/base: present", out)
        self.assertIn("postgres: failed", out)
        self.assertIn("python3 -m tools.imagebuild postgres --to 127.0.0.1:5000", out)
        self.assertEqual(argv_calls(host), [SKOPEO_VERSION, base])


class Refusals(unittest.TestCase):
    def test_no_destination_without_a_site_file_asks_for_to(self) -> None:
        code, _, err = mirror(FakeHost({SKOPEO_VERSION: done(SKOPEO_VERSION)}, files(site=None)))
        self.assertEqual(code, 1)
        self.assertIn("--to", err)

    def test_unusable_destination_refuses(self) -> None:
        code, _, err = mirror(FakeHost({}, files()), to="http://user:pw@ghcr.io")
        self.assertEqual(code, 1)
        self.assertIn("Fix:", err)

    def test_missing_skopeo_names_the_provision_step(self) -> None:
        code, _, err = mirror(FakeHost({}, files()), to="127.0.0.1:5000")
        self.assertEqual(code, 1)
        self.assertIn("host provision --only host-tools", err)

    def test_docker_permission_denied_names_docker_access(self) -> None:
        host = FakeHost(
            {
                SKOPEO_VERSION: done(SKOPEO_VERSION),
                PROBE_LOOPBACK: done(PROBE_LOOPBACK, 1, stderr="permission denied while trying to connect to the Docker daemon socket"),
            },
            files(),
        )
        code, _, err = mirror(host, to="127.0.0.1:5000")
        self.assertEqual(code, 1)
        self.assertIn("Docker access", err)
        self.assertNotIn(COPY_LOOPBACK, argv_calls(host))

    def test_proxy_credentials_unreadable_by_non_root_refuse_with_sudo(self) -> None:
        host = FakeHost(
            {SKOPEO_VERSION: done(SKOPEO_VERSION)},
            files(SECOND),
            denied=frozenset({"/etc/gideon/secrets/proxy_auth"}),
        )
        code, _, err = mirror(host)
        self.assertEqual(code, 1)
        self.assertIn("sudo", err)

    def test_proxy_environment_reaches_skopeo_and_the_probe(self) -> None:
        probes = present_probes("127.0.0.1:5000")
        caddy_probe = image_probe("127.0.0.1:5000", "caddy", DIGEST)
        caddy_copy = image_copy(
            "127.0.0.1:5000", "caddy", DIGEST, CADDY_SOURCE
        )
        probes[caddy_probe] = [done(caddy_probe, 1), done(caddy_probe)]
        host = FakeHost(
            {
                SKOPEO_VERSION: done(SKOPEO_VERSION),
                **probes,
                caddy_copy: done(caddy_copy),
            },
            files(SECOND),
        )
        code, _, _ = mirror(host)
        self.assertEqual(code, 0)
        for command, env in host.calls:
            if command in (*probes, caddy_copy):
                assert env is not None
                self.assertEqual(env["HTTPS_PROXY"], "http://proxy.exd.example.internal:3128")
