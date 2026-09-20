"""Contracts for the box-side built-image tool over fake Host/Fetcher seams."""

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest.mock import patch as mock_patch

from gideon.host.images import (
    BuiltImagePin,
    RegistryTarget,
    compute_inputs_digest,
    load_image_lock_text,
)
from gideon.host.sysio import Command, PathLike
from tools.imagebuild.build import SMOKE, BuildPlan, Smoke, build_plan
from tools.imagebuild.cli import main
from tools.pinwatch.fetch import FetchError, Response

ROOT = Path(__file__).resolve().parent.parent
REPO = Path("/repo")
SITE_PATH = "/tmp/site.yaml"
LOCK_PATH = "/repo/images.lock"
DOCKERFILE_PATH = "/repo/images/postgres/Dockerfile"
BASE_DIGEST = "sha256:" + "a" * 64
INPUTS_DIGEST = "sha256:" + "b" * 64
BUILT_DIGEST = "sha256:" + "c" * 64
PUSHED_DIGEST = "sha256:" + "d" * 64
VERSION = "1000.0.0-1.example"
DOCKERFILE = "ARG BASE\nFROM ${BASE}\nARG PGBACKREST_VERSION\nRUN echo built\n"
LOCK_TEXT = (
    "# fictitious built lock\n"
    "version: 1\n"
    "images:\n"
    "  postgres:\n"
    "    build: images/postgres\n"
    "    base: docker.io/library/example:18\n"
    f"    base_digest: {BASE_DIGEST}\n"
    "    build_args:\n"
    f"      PGBACKREST_VERSION: {VERSION}\n"
    "    watch:\n"
    "      PGBACKREST_VERSION:\n"
    "        apt_index: https://apt.example/dists/fictitious/Packages\n"
    "        package: pgbackrest\n"
    f"    inputs_digest: {INPUTS_DIGEST}\n"
    f"    digest: {BUILT_DIGEST}\n"
)


MIRRORED_LOCK_TEXT = (
    "version: 1\n"
    "images:\n"
    "  postgres:\n"
    "    source: docker.io/library/example:18\n"
    f"    digest: {BUILT_DIGEST}\n"
)


def done(
    argv: Sequence[str],
    rc: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), rc, stdout, stderr)


class FakeFetcher:
    """Return HTTP answers for every URL, or raise FetchError for named hosts."""

    def __init__(self, unreachable: set[str] | None = None) -> None:
        self.unreachable = unreachable or set()
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
        if any(host in url for host in self.unreachable):
            raise FetchError(url, "unreachable")
        return Response(503, {}, b"still an HTTP answer")


class FakeHost:
    """A dict-backed Host recording every command, write, and ownership change."""

    def __init__(
        self,
        commands: Mapping[tuple[str, ...], subprocess.CompletedProcess[str] | list[subprocess.CompletedProcess[str]]] | None = None,
        *,
        files: Mapping[str, str] | None = None,
        directories: set[str] | None = None,
    ) -> None:
        self.commands = dict(commands or {})
        self.files = dict(files or {})
        self.directories = set(directories or set())
        self.calls: list[tuple[tuple[str, ...], Mapping[str, str] | None, PathLike | None]] = []
        self.writes: list[str] = []
        self.chowns: list[tuple[str, int, int]] = []

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
        del check, input, timeout
        command = tuple(argv)
        self.calls.append((command, env, cwd))
        outcome = self.commands.get(command)
        if isinstance(outcome, list):
            return outcome.pop(0) if len(outcome) > 1 else outcome[0]
        if outcome is not None:
            return outcome
        if command == (sys.executable, "tests/regenerate_render_fixtures.py"):
            return done(command)
        return done(command, 127, stderr="docker: command not found")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        return self.files[os.fspath(path)]

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
        parent = os.fspath(path).rstrip("/")
        names: set[str] = set()
        for candidate in (*self.files, *self.directories):
            if not candidate.startswith(parent + "/"):
                continue
            remainder = candidate[len(parent) + 1 :]
            names.add(remainder.split("/", 1)[0])
        return sorted(names)

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del missing_ok
        self.files.pop(os.fspath(path), None)

    def stat(self, path: PathLike) -> os.stat_result:
        key = os.fspath(path)
        if key in self.directories:
            return os.stat_result((stat.S_IFDIR | 0o755, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        if key in self.files:
            return os.stat_result((stat.S_IFREG | 0o644, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        raise FileNotFoundError(key)

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
        return 1000


def files(*, lock_text: str = LOCK_TEXT) -> dict[str, str]:
    return {
        LOCK_PATH: lock_text,
        SITE_PATH: (ROOT / "config/site.example.yaml").read_text(),
        "/repo/config/egress.yaml": (ROOT / "config/egress.yaml").read_text(),
        DOCKERFILE_PATH: DOCKERFILE,
    }


def build_commands(
    *,
    target: str = "127.0.0.1:5000",
    digest: str = PUSHED_DIGEST,
    smoke: subprocess.CompletedProcess[str] | None = None,
) -> dict[tuple[str, ...], subprocess.CompletedProcess[str]]:
    repository = f"{target}/postgres"
    tag = f"{repository}:18-gideon"
    smoke_argv = ("docker", "run", "--rm", "--entrypoint", "pgbackrest", tag, "version")
    return {
        ("docker", "build", "--provenance=false", "--sbom=false", "--build-arg", f"BASE={repository}@{BASE_DIGEST}", "--build-arg", f"PGBACKREST_VERSION={VERSION}", "--label", "org.opencontainers.image.base.name=docker.io/library/example:18", "--label", f"org.opencontainers.image.base.digest={BASE_DIGEST}", "--label", f"gideon.build-arg.PGBACKREST_VERSION={VERSION}", "--tag", tag, "/repo/images/postgres"): done(("docker", "build")),
        smoke_argv: smoke or done(smoke_argv, stdout=f"pgBackRest {VERSION.split('-', 1)[0]}\n"),
        ("docker", "push", tag): done(("docker", "push", tag)),
        ("docker", "image", "inspect", "--format", "{{json .RepoDigests}}", tag): done(("docker", "image", "inspect"), stdout=json.dumps([f"{repository}@{digest}"])),
    }


def run_tool(
    host: FakeHost,
    fetcher: FakeFetcher | None = None,
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    output, errors = io.StringIO(), io.StringIO()
    with (
        contextlib.redirect_stdout(output),
        contextlib.redirect_stderr(errors),
        mock_patch.dict(os.environ, dict(environ or {}), clear=False),
    ):
        code = main(
            argv or ["postgres", "--to", "127.0.0.1:5000"],
            host=host,
            fetcher=fetcher or FakeFetcher(),
            root=REPO,
            site_path=SITE_PATH,
        )
    return code, output.getvalue(), errors.getvalue()


class BuildPlans(unittest.TestCase):
    def test_build_plan_has_the_exact_base_tag_args_labels_and_context(self) -> None:
        result = load_image_lock_text(LOCK_TEXT)
        assert result.lock is not None
        pin = result.lock.images[0]
        assert isinstance(pin, BuiltImagePin)
        plan = build_plan(pin, RegistryTarget("http", "127.0.0.1:5000", ""), REPO)
        self.assertIsInstance(plan, BuildPlan)
        self.assertEqual(plan.context, REPO / "images/postgres")
        self.assertEqual(
            plan.build_argv,
            next(iter(build_commands())),
        )
        self.assertEqual(plan.smoke, Smoke(("pgbackrest", "version"), ("PGBACKREST_VERSION",)))

    def test_gideon_smoke_imports_direct_packages_and_reports_all_versions(self) -> None:
        smoke = SMOKE["gideon"]

        self.assertEqual(smoke.argv[:2], ("python", "-c"))
        self.assertEqual(
            smoke.version_args,
            (
                "STARLETTE_VERSION",
                "UVICORN_VERSION",
                "HTTPX_VERSION",
                "ANYIO_VERSION",
                "HTTPCORE_VERSION",
                "H11_VERSION",
                "CERTIFI_VERSION",
                "IDNA_VERSION",
                "CLICK_VERSION",
                "TYPING_EXTENSIONS_VERSION",
            ),
        )
        for package in ("starlette", "uvicorn", "httpx"):
            self.assertIn(f"import {package}", smoke.argv[2])
        for package in (
            "starlette",
            "uvicorn",
            "httpx",
            "anyio",
            "httpcore",
            "h11",
            "certifi",
            "idna",
            "click",
            "typing_extensions",
        ):
            self.assertIn(f'"{package}"', smoke.argv[2])
        self.assertIn("importlib.metadata.version", smoke.argv[2])


PROXY = {
    "HTTP_PROXY": "http://user:s3cret@proxy.example:3128",
    "HTTPS_PROXY": "http://user:s3cret@proxy.example:3128",
    "NO_PROXY": "127.0.0.1,localhost",
}


class ProxyPlans(unittest.TestCase):

    def test_proxy_reaches_the_build_by_environment_never_argv(self) -> None:
        result = load_image_lock_text(LOCK_TEXT)
        assert result.lock is not None
        pin = result.lock.images[0]
        assert isinstance(pin, BuiltImagePin)
        plan = build_plan(pin, RegistryTarget("http", "127.0.0.1:5000", ""), REPO, PROXY)
        argv = " ".join(plan.build_argv)
        self.assertNotIn("s3cret", argv)
        self.assertIn("--build-arg HTTPS_PROXY --build-arg NO_PROXY", argv)
        self.assertEqual(plan.environment["HTTPS_PROXY"], PROXY["HTTPS_PROXY"])

    def test_docker_child_keeps_its_environment_plus_the_proxy(self) -> None:
        from tools.imagebuild.build import _child_env

        self.assertIsNone(_child_env({}))
        merged = _child_env(PROXY)
        assert merged is not None
        self.assertEqual(merged["HTTPS_PROXY"], PROXY["HTTPS_PROXY"])
        self.assertIn("PATH", merged)


class BuildTool(unittest.TestCase):
    def test_build_sequence_records_digest_and_regenerates_fixtures(self) -> None:
        host = FakeHost(build_commands(), files=files())
        code, out, err = run_tool(host)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("postgres: built — 127.0.0.1:5000/postgres@" + PUSHED_DIGEST, out)
        self.assertEqual(
            [call[0] for call in host.calls],
            [
                *build_commands().keys(),
                (sys.executable, "tests/regenerate_render_fixtures.py"),
            ],
        )
        self.assertEqual(host.calls[-1][2], REPO)
        lock = load_image_lock_text(host.files[LOCK_PATH]).lock
        assert lock is not None
        pin = lock.images[0]
        assert isinstance(pin, BuiltImagePin)
        self.assertEqual(pin.digest, PUSHED_DIGEST)
        self.assertEqual(
            pin.inputs_digest,
            compute_inputs_digest(BASE_DIGEST, {"PGBACKREST_VERSION": VERSION}, DOCKERFILE.encode()),
        )

    def test_failed_smoke_does_not_push_or_write_lock(self) -> None:
        commands = build_commands(smoke=done(("docker", "run"), 1, stdout="wrong output"))
        host = FakeHost(commands, files=files())
        original = host.files[LOCK_PATH]
        code, out, err = run_tool(host)
        self.assertEqual((code, err), (1, ""))
        self.assertIn("wrong output", out)
        self.assertIn("the push did not happen", out)
        self.assertEqual(host.files[LOCK_PATH], original)
        self.assertNotIn(("docker", "push", "127.0.0.1:5000/postgres:18-gideon"), [call[0] for call in host.calls])

    def test_sudo_ownership_is_restored_for_lock_and_fixture_files(self) -> None:
        fixture = "/repo/tests/fixtures/render/example/compose.yaml"
        host = FakeHost(
            build_commands(),
            files={**files(), fixture: "fixture"},
            directories={"/repo/tests/fixtures/render", "/repo/tests/fixtures/render/example"},
        )
        code, _, err = run_tool(host, environ={"SUDO_UID": "123", "SUDO_GID": "456"})
        self.assertEqual((code, err), (0, ""))
        self.assertIn((LOCK_PATH, 123, 456), host.chowns)
        self.assertIn((fixture, 123, 456), host.chowns)


class CheckTool(unittest.TestCase):
    def check_commands(self, labels: Mapping[str, str] | None = None) -> dict[tuple[str, ...], subprocess.CompletedProcess[str]]:
        ref = f"127.0.0.1:5000/postgres@{BUILT_DIGEST}"
        expected_labels = labels or {
            "org.opencontainers.image.base.name": "docker.io/library/example:18",
            "org.opencontainers.image.base.digest": BASE_DIGEST,
            "gideon.build-arg.PGBACKREST_VERSION": VERSION,
        }
        return {
            ("docker", "manifest", "inspect", "--insecure", ref): done(("docker", "manifest", "inspect")),
            ("docker", "pull", ref): done(("docker", "pull", ref)),
            ("docker", "image", "inspect", "--format", "{{json .Config.Labels}}", ref): done(("docker", "image", "inspect"), stdout=json.dumps(expected_labels)),
            ("docker", "run", "--rm", "--entrypoint", "pgbackrest", ref, "version"): done(("docker", "run"), stdout=f"pgBackRest {VERSION.split('-', 1)[0]}\n"),
        }

    def test_check_probes_pulls_compares_labels_and_smokes(self) -> None:
        host = FakeHost(self.check_commands(), files=files())
        code, out, err = run_tool(host, argv=["postgres", "--to", "127.0.0.1:5000", "--check"])
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("postgres: ok — 127.0.0.1:5000/postgres@", out)

    def test_check_label_mismatch_names_the_label(self) -> None:
        host = FakeHost(
            self.check_commands({"org.opencontainers.image.base.name": "wrong"}),
            files=files(),
        )
        code, out, err = run_tool(host, argv=["postgres", "--to", "127.0.0.1:5000", "--check"])
        self.assertEqual((code, err), (1, ""))
        self.assertIn("label org.opencontainers.image.base.name", out)


class Refusals(unittest.TestCase):
    def test_egress_refusal_lists_unreachable_hosts(self) -> None:
        host = FakeHost(files=files())
        fetcher = FakeFetcher({"apt.postgresql.org", "deb.debian.org"})
        code, out, err = run_tool(host, fetcher)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("apt.postgresql.org", err)
        self.assertIn("deb.debian.org", err)
        self.assertIn("allow these hosts from the box, or set egress_proxy", err)
        self.assertEqual(host.calls, [])

    def test_docker_permission_denied_is_a_stderr_refusal(self) -> None:
        commands = build_commands()
        first = next(iter(commands))
        commands[first] = done(first, 1, stderr="permission denied while trying to connect")
        host = FakeHost(commands, files=files())
        code, out, err = run_tool(host)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Docker access", err)

    def test_dry_run_makes_no_network_or_docker_call_and_writes_nothing(self) -> None:
        host = FakeHost(files=files())
        fetcher = FakeFetcher()
        code, out, err = run_tool(host, fetcher, ["postgres", "--to", "127.0.0.1:5000", "--dry-run"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("postgres: would build — docker build", out)
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(host.calls, [])
        self.assertEqual(host.writes, [])

    def test_mirrored_and_unknown_names_refuse(self) -> None:
        host = FakeHost(files=files(lock_text=MIRRORED_LOCK_TEXT))
        code, out, err = run_tool(host, argv=["postgres", "--to", "127.0.0.1:5000"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("mirrored", err)
        host = FakeHost(files=files())
        code, out, err = run_tool(host, argv=["missing", "--to", "127.0.0.1:5000"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("unknown image", err)


if __name__ == "__main__":
    unittest.main()
