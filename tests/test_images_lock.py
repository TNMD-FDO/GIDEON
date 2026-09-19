"""Image-lock contracts: the committed artifact loads, refusal shapes, registry parsing."""

import os
import subprocess
import unittest
from collections.abc import Mapping
from pathlib import Path

from gideon.host.images import (
    DIGEST,
    SOURCE_REFERENCE,
    AptWatch,
    BuiltImagePin,
    ImageLock,
    MirroredImagePin,
    PypiWatch,
    RegistryTarget,
    compute_inputs_digest,
    inputs_digest_text,
    is_plain_registry,
    load_image_lock,
    parse_registry,
    proxy_environment,
    reference,
    render_errors,
)
from gideon.host.site import load_site
from gideon.host.sysio import Command, PathLike

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "images.lock"
EXAMPLE = ROOT / "config/site.example.yaml"
CADDY_DIGEST = "sha256:" + "e" * 64

VALID = (
    "version: 1\n"
    "images:\n"
    "  caddy:\n"
    "    source: docker.io/library/example:1.0\n"
    f"    digest: {CADDY_DIGEST}\n"
)
BUILT_DIGEST = "sha256:" + "f" * 64
BUILT_LOCK = (
    "version: 1\n"
    "images:\n"
    "  postgres:\n"
    "    build: images/postgres\n"
    "    base: docker.io/library/example:18\n"
    f"    base_digest: {CADDY_DIGEST}\n"
    "    build_args:\n"
    "      PGBACKREST_VERSION: 1000.0.0-1.example\n"
    "    watch:\n"
    "      PGBACKREST_VERSION:\n"
    "        apt_index: https://apt.example/dists/trixie/Packages\n"
    "        package: pgbackrest\n"
    f"    inputs_digest: {BUILT_DIGEST}\n"
    f"    digest: {BUILT_DIGEST}\n"
)
BUILT_LOCK_WITH_PYPI = BUILT_LOCK.replace(
    "      PGBACKREST_VERSION: 1000.0.0-1.example\n",
    "      PGBACKREST_VERSION: 1000.0.0-1.example\n"
    "      PYPI_VERSION: 1.2.3\n",
).replace(
    "    watch:\n      PGBACKREST_VERSION:\n"
    "        apt_index: https://apt.example/dists/trixie/Packages\n"
    "        package: pgbackrest\n",
    "    watch:\n      PGBACKREST_VERSION:\n"
    "        apt_index: https://apt.example/dists/trixie/Packages\n"
    "        package: pgbackrest\n"
    "      PYPI_VERSION:\n"
    "        pypi_project: example-project\n",
)


class FakeHost:
    """A file-backed Host: ``files`` map paths to text; ``denied`` raise PermissionError."""

    def __init__(
        self,
        *,
        files: Mapping[str, str] | None = None,
        denied: frozenset[str] = frozenset(),
    ) -> None:
        self.files = dict(files or {})
        self.denied = denied

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
        return subprocess.CompletedProcess(list(argv), 127, "", "")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        if key in self.denied:
            raise PermissionError(key)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644) -> None:
        del encoding, mode
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
        return 0


def load_text(text: str):
    return load_image_lock("/tmp/images.lock", host=FakeHost(files={"/tmp/images.lock": text}))


class CommittedLock(unittest.TestCase):
    def test_committed_lock_pins_images_by_index_digest(self) -> None:
        result = load_image_lock(LOCK)
        self.assertEqual(result.errors, ())
        assert result.lock is not None
        self.assertEqual(result.lock.version, 1)
        # Shape, never values: the pin watch (ADR-0031) moves the values through
        # pull requests, and this contract must hold on every bump branch.
        self.assertEqual(
            [pin.name for pin in result.lock.images],
            [
                "caddy",
                "open-webui",
                "vllm-openai",
                "searxng",
                "prometheus",
                "grafana",
                "node-exporter",
                "dcgm-exporter",
                "postgres-exporter",
                "cadvisor",
                "blackbox-exporter",
                "postgres",
            ],
        )
        for pin in result.lock.images:
            with self.subTest(image=pin.name):
                if isinstance(pin, MirroredImagePin):
                    self.assertIsNotNone(SOURCE_REFERENCE.fullmatch(pin.source))
                else:
                    self.assertIsInstance(pin, BuiltImagePin)
                    assert isinstance(pin, BuiltImagePin)
                    self.assertTrue(pin.built, "committed built pins cannot be unbuilt")
                    self.assertIsNotNone(SOURCE_REFERENCE.fullmatch(pin.base))
                    self.assertIsNotNone(DIGEST.fullmatch(pin.base_digest))
                    self.assertEqual(set(pin.watch), set(pin.build_args))
                self.assertIsNotNone(DIGEST.fullmatch(pin.digest))

    def test_watch_and_build_args_are_optional(self) -> None:
        text = BUILT_LOCK.replace(
            "    build_args:\n      PGBACKREST_VERSION: 1000.0.0-1.example\n"
            "    watch:\n      PGBACKREST_VERSION:\n"
            "        apt_index: https://apt.example/dists/trixie/Packages\n"
            "        package: pgbackrest\n",
            "",
        )
        result = load_text(text)
        self.assertEqual(result.errors, ())
        assert result.lock is not None
        pin = result.lock.images[0]
        assert isinstance(pin, BuiltImagePin)
        self.assertEqual((dict(pin.build_args), dict(pin.watch)), ({}, {}))

    def test_fictitious_lock_accepts_both_pin_kinds(self) -> None:
        mirrored = load_text(VALID).lock
        built = load_text(BUILT_LOCK_WITH_PYPI).lock
        assert mirrored is not None and built is not None
        self.assertIsInstance(mirrored.images[0], MirroredImagePin)
        self.assertIsInstance(built.images[0], BuiltImagePin)
        pin = built.images[0]
        assert isinstance(pin, BuiltImagePin)
        self.assertEqual(pin.base, "docker.io/library/example:18")
        apt_watch = pin.watch["PGBACKREST_VERSION"]
        self.assertIsInstance(apt_watch, AptWatch)
        assert isinstance(apt_watch, AptWatch)
        self.assertEqual(apt_watch.package, "pgbackrest")
        pypi_watch = pin.watch["PYPI_VERSION"]
        self.assertIsInstance(pypi_watch, PypiWatch)
        assert isinstance(pypi_watch, PypiWatch)
        self.assertEqual(pypi_watch.project, "example-project")
        self.assertTrue(pin.built)

    def test_inputs_digest_is_canonical_and_sorted(self) -> None:
        text = inputs_digest_text(
            CADDY_DIGEST,
            {"Z_ARG": "z", "A_ARG": "a"},
            b"FROM example\n",
        )
        self.assertEqual(
            text.splitlines()[:3],
            [f"base_digest={CADDY_DIGEST}", "arg A_ARG=a", "arg Z_ARG=z"],
        )
        self.assertTrue(compute_inputs_digest(CADDY_DIGEST, {"A_ARG": "a"}, b"x").startswith("sha256:"))

    def test_committed_built_inputs_would_be_recomputed(self) -> None:
        result = load_image_lock(LOCK)
        assert result.lock is not None
        for pin in result.lock.images:
            if isinstance(pin, BuiltImagePin):
                dockerfile = ROOT / pin.build / "Dockerfile"
                self.assertEqual(
                    pin.inputs_digest,
                    compute_inputs_digest(pin.base_digest, pin.build_args, dockerfile.read_bytes()),
                )


class Refusals(unittest.TestCase):
    def assert_single_error(self, text: str, key_path: str | None, fragment: str) -> None:
        result = load_text(text)
        self.assertIsNone(result.lock)
        self.assertEqual(len(result.errors), 1, result.errors)
        self.assertEqual(result.errors[0].key_path, key_path)
        self.assertIn(fragment, result.errors[0].problem)
        self.assertTrue(result.errors[0].fix)

    def test_missing_file(self) -> None:
        result = load_image_lock("/tmp/none.lock", host=FakeHost())
        self.assertEqual(len(result.errors), 1)
        self.assertIn("missing", result.errors[0].problem)

    def test_unknown_key_names_nearest(self) -> None:
        self.assert_single_error(
            VALID.replace("    source:", "    sauce:").replace("  caddy:\n", "  caddy:\n    source: docker.io/library/example:1.0\n"),
            "images.caddy.sauce",
            "nearest valid key is 'source'",
        )

    def test_bad_digest(self) -> None:
        self.assert_single_error(
            VALID.replace(CADDY_DIGEST, "sha256:abc"), "images.caddy.digest", "sha256:<64"
        )

    def test_untagged_source(self) -> None:
        self.assert_single_error(
            VALID.replace("example:1.0", "example"), "images.caddy.source", "tagged"
        )

    def test_duplicate_key(self) -> None:
        self.assert_single_error(VALID + "version: 2\n", None, "duplicate mapping key 'version'")

    def test_empty_images(self) -> None:
        self.assert_single_error("version: 1\nimages: {}\n", "images", "non-empty")

    def test_collect_all(self) -> None:
        result = load_text("version: 0\nimages:\n  Caddy!:\n    digest: nope\n")
        self.assertIsNone(result.lock)
        paths = sorted(error.key_path or "" for error in result.errors)
        self.assertEqual(paths, ["images.Caddy!", "images.Caddy!.digest", "images.Caddy!.source", "version"])
        rendered = render_errors(result.errors)
        self.assertEqual(rendered.count("Fix:"), 4)

    def test_mixed_pin_shapes_are_refused(self) -> None:
        text = VALID.replace(
            "    digest:", "    build: images/postgres\n    digest:"
        )
        self.assert_single_error(text, "images.caddy", "either the mirrored shape")

    def test_build_path_cannot_be_absolute_or_escape_checkout(self) -> None:
        for build in ("/images/postgres", "../images/postgres", "images/../postgres"):
            with self.subTest(build=build):
                result = load_text(BUILT_LOCK.replace("images/postgres", build))
                self.assertIn("images.postgres.build", [error.key_path for error in result.errors])

    def test_build_arg_name_must_follow_grammar(self) -> None:
        result = load_text(BUILT_LOCK.replace("PGBACKREST_VERSION:", "bad-name:"))
        self.assertTrue(any("[A-Z][A-Z0-9_]*" in error.problem for error in result.errors))

    def test_secret_like_build_arg_names_are_refused(self) -> None:
        for name in ("DATABASE_PASSWORD", "API_TOKEN", "SIGNING_KEY", "SECRET"):
            with self.subTest(name=name):
                text = BUILT_LOCK.replace("PGBACKREST_VERSION", name)
                errors = load_text(text).errors
                self.assertTrue(any("never a secret" in error.problem for error in errors), name)

    def test_watch_must_match_an_arg_and_https_index(self) -> None:
        orphan = BUILT_LOCK.replace("PGBACKREST_VERSION:\n        apt_index", "OTHER:\n        apt_index")
        result = load_text(orphan)
        self.assertTrue(any("present in build_args" in error.problem for error in result.errors))
        non_https = load_text(BUILT_LOCK.replace("https://apt.example", "http://apt.example"))
        self.assertTrue(any("https://" in error.problem for error in non_https.errors))

    def test_watch_rejects_both_kinds_in_one_entry(self) -> None:
        text = BUILT_LOCK.replace(
            "        package: pgbackrest\n",
            "        package: pgbackrest\n        pypi_project: pgbackrest\n",
        )
        self.assert_single_error(text, "images.postgres.watch.PGBACKREST_VERSION", "exactly one kind")

    def test_watch_rejects_entry_without_a_kind(self) -> None:
        text = BUILT_LOCK.replace(
            "      PGBACKREST_VERSION:\n"
            "        apt_index: https://apt.example/dists/trixie/Packages\n"
            "        package: pgbackrest\n",
            "      PGBACKREST_VERSION: {}\n",
        )
        result = load_text(text)
        self.assertTrue(any("either apt_index and package, or pypi_project" in error.problem for error in result.errors))

    def test_watch_unknown_key_names_pypi_kind(self) -> None:
        text = BUILT_LOCK_WITH_PYPI.replace(
            "        pypi_project: example-project",
            "        pypi_projec: example-project\n"
            "        pypi_project: example-project",
        )
        result = load_text(text)
        self.assertTrue(any("nearest valid key is 'pypi_project'" in error.problem for error in result.errors))

    def test_pypi_project_must_be_a_non_empty_string(self) -> None:
        for value in ("7", "''"):
            with self.subTest(value=value):
                text = BUILT_LOCK_WITH_PYPI.replace("pypi_project: example-project", f"pypi_project: {value}")
                result = load_text(text)
                self.assertTrue(any("pypi_project" in (error.key_path or "") for error in result.errors))

    def test_pypi_project_must_use_normalized_name(self) -> None:
        text = BUILT_LOCK_WITH_PYPI.replace("example-project", "Example_Project")
        self.assert_single_error(
            text,
            "images.postgres.watch.PYPI_VERSION.pypi_project",
            "example-project",
        )

    def test_pypi_project_must_match_name_grammar(self) -> None:
        text = BUILT_LOCK_WITH_PYPI.replace("example-project", "not!a-project")
        self.assert_single_error(
            text,
            "images.postgres.watch.PYPI_VERSION.pypi_project",
            "expected a PyPI project name",
        )

    def test_pypi_watch_requires_build_args(self) -> None:
        text = BUILT_LOCK_WITH_PYPI.replace("      PYPI_VERSION: 1.2.3\n", "")
        result = load_text(text)
        self.assertTrue(any("present in build_args" in error.problem for error in result.errors))

    def test_multiline_build_arg_is_refused(self) -> None:
        text = BUILT_LOCK.replace(
            "PGBACKREST_VERSION: 1000.0.0-1.example",
            "PGBACKREST_VERSION: |\n        1000.0.0-1.example\n        second-line",
        )
        self.assertTrue(any("single-line" in error.problem for error in load_text(text).errors))

    def test_unbuilt_sentinels_must_be_paired_and_reference_refuses(self) -> None:
        unbuilt = BUILT_LOCK.replace(BUILT_DIGEST, "unbuilt")
        result = load_text(unbuilt)
        self.assertEqual(result.errors, ())
        assert result.lock is not None
        pin = result.lock.images[0]
        assert isinstance(pin, BuiltImagePin)
        self.assertFalse(pin.built)
        with self.assertRaises(ValueError) as caught:
            reference(RegistryTarget("http", "127.0.0.1:5000", ""), pin)
        self.assertIn("python3 -m tools.imagebuild postgres", str(caught.exception))

        one = load_text(BUILT_LOCK.replace(f"inputs_digest: {BUILT_DIGEST}", "inputs_digest: unbuilt"))
        self.assertTrue(any("both be 'unbuilt'" in error.problem for error in one.errors))


class Registry(unittest.TestCase):
    def test_plain_registry_predicate_covers_loopback_and_libvirt_bridge(self) -> None:
        for authority in ("127.0.0.1:5000", "localhost:5000", "[::1]:5000", "192.168.122.1:5000"):
            with self.subTest(authority=authority):
                self.assertTrue(is_plain_registry(authority))
        self.assertFalse(is_plain_registry("registry.example:5000"))

    def test_loopback_is_http_without_prefix(self) -> None:
        self.assertEqual(parse_registry("127.0.0.1:5000"), RegistryTarget("http", "127.0.0.1:5000", ""))
        self.assertEqual(parse_registry("localhost:5000"), RegistryTarget("http", "localhost:5000", ""))
        self.assertEqual(parse_registry("[::1]:5000"), RegistryTarget("http", "[::1]:5000", ""))

    def test_libvirt_bridge_is_http(self) -> None:
        self.assertEqual(
            parse_registry("192.168.122.1:5000"),
            RegistryTarget("http", "192.168.122.1:5000", ""),
        )

    def test_hostname_is_https(self) -> None:
        self.assertEqual(
            parse_registry("registry.example:5000"),
            RegistryTarget("https", "registry.example:5000", ""),
        )

    def test_namespaced_public_registry(self) -> None:
        self.assertEqual(parse_registry("ghcr.io/tnmd-fdo"), RegistryTarget("https", "ghcr.io", "tnmd-fdo/"))
        self.assertEqual(parse_registry("ghcr.io/tnmd-fdo/"), RegistryTarget("https", "ghcr.io", "tnmd-fdo/"))

    def test_unusable_values(self) -> None:
        for value in ("", "http://user:pw@ghcr.io", "host:notaport", "ghcr.io/x?y", "ghcr.io/x#f"):
            with self.subTest(value=value):
                self.assertIsNone(parse_registry(value))

    def test_reference_pins_by_digest(self) -> None:
        pin = MirroredImagePin("caddy", CADDY_DIGEST, "docker.io/library/example:1.0")
        self.assertEqual(
            reference(RegistryTarget("http", "127.0.0.1:5000", ""), pin),
            f"127.0.0.1:5000/caddy@{CADDY_DIGEST}",
        )
        self.assertEqual(
            reference(RegistryTarget("https", "ghcr.io", "tnmd-fdo/"), pin),
            f"ghcr.io/tnmd-fdo/caddy@{CADDY_DIGEST}",
        )


def site(proxy: str = ""):
    text = EXAMPLE.read_text()
    if proxy:
        text += f'\negress_proxy: "{proxy}"\n'
    host = FakeHost(files={"/tmp/site.yaml": text})
    result = load_site(Path("/tmp/site.yaml"), host=host)
    assert result.config is not None, result.errors
    return result.config


class ProxyEnvironment(unittest.TestCase):
    PROXY = "http://proxy.example.org:3128"
    AUTH = "/etc/gideon/secrets/proxy_auth"

    def test_no_proxy_means_no_variables(self) -> None:
        env = proxy_environment(site(), FakeHost())
        self.assertTrue(env.ok)
        self.assertEqual(dict(env.variables), {})

    def test_proxy_without_credentials(self) -> None:
        env = proxy_environment(site(self.PROXY), FakeHost())
        self.assertTrue(env.ok)
        self.assertEqual(env.variables["HTTPS_PROXY"], self.PROXY)
        self.assertEqual(env.variables["HTTP_PROXY"], self.PROXY)
        for never_proxied in ("127.0.0.1", "localhost", "192.168.122.1"):
            self.assertIn(never_proxied, env.variables["NO_PROXY"].split(","))

    def test_credentials_are_percent_encoded_userinfo(self) -> None:
        host = FakeHost(files={self.AUTH: "al ice:p@ss:w/rd#%\n"})
        env = proxy_environment(site(self.PROXY), host)
        self.assertTrue(env.ok)
        self.assertEqual(
            env.variables["HTTPS_PROXY"],
            "http://al%20ice:p%40ss%3Aw%2Frd%23%25@proxy.example.org:3128",
        )

    def test_unreadable_credentials_report_sudo_fix(self) -> None:
        env = proxy_environment(site(self.PROXY), FakeHost(denied=frozenset({self.AUTH})))
        self.assertFalse(env.ok)
        self.assertIn("sudo", env.fix)
        self.assertNotIn("HTTPS_PROXY", env.variables)

    def test_malformed_credentials_refuse(self) -> None:
        env = proxy_environment(site(self.PROXY), FakeHost(files={self.AUTH: "no-colon\n"}))
        self.assertFalse(env.ok)
        self.assertIn("user:password", env.fix)

    def test_unusable_proxy_url_refuses(self) -> None:
        env = proxy_environment(site("proxy.example.org:3128"), FakeHost())
        self.assertFalse(env.ok)
        self.assertIn("egress_proxy", env.problem or "")


class Types(unittest.TestCase):
    def test_lock_is_frozen(self) -> None:
        lock = ImageLock(
            1,
            (MirroredImagePin("caddy", CADDY_DIGEST, "docker.io/library/example:1.0"),),
        )
        with self.assertRaises(AttributeError):
            lock.version = 2  # type: ignore[misc]
