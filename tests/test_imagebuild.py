"""Contracts for the box-side built-image tool over fake Host/Fetcher seams."""

import contextlib
import io
import json
import os
import re
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
    load_image_lock,
    load_image_lock_text,
)
from gideon.host.sysio import Command, PathLike
from tools.imagebuild.build import (
    SMOKE,
    BuildPlan,
    CacheRecord,
    Smoke,
    build_plan,
    owned_records,
    parse_usage,
    prune_filter,
)
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


# Fictitious cache records, with descendants listed before their registry pulls.
USAGE_TEXT = "\n".join(
    json.dumps({
        "CreatedAt": "2001-01-01T00:00:00Z",
        "Description": description,
        "ID": letter * 25,
        "LastUsedAt": "2001-01-02T00:00:00Z",
        "Mutable": False,
        "Parents": [parent * 25 for parent in parents] if parents else None,
        "Reclaimable": True,
        "Shared": True,
        "Size": size,
        "Type": "regular",
        "UsageCount": 2,
    })
    for letter, description, parents, size in (
        ("g", "COPY fictitious wheel", "f", "7MB"),
        ("f", "mount / from exec fictitious install", "e", "6MB"),
        ("e", "mount / from exec fictitious compile", "d", "5MB"),
        ("d", f"pulled from 127.0.0.1:5000/python@{BASE_DIGEST}", "", "4MB"),
        ("c", "COPY fictitious configuration", "b", "3MB"),
        ("b", "mount / from exec fictitious packages", "a", "2MB"),
        ("a", f"pulled from 127.0.0.1:5000/postgres@{BASE_DIGEST}", "", "1MB"),
        ("h", f"pulled from docker.io/library/python@{BASE_DIGEST}", "", "8MB"),
        ("i", "mount / from exec fictitious foreign packages", "h", "10.4GB"),
        ("j", "local source for context", "", "9kB"),
        ("k", "", "", "10MB"),
        ("l", "", "k", "11MB"),
        ("m", "mount / from exec fictitious image-store base", "l", "12MB"),
        ("n", "COPY fictitious mixed parents", "ci", "13MB"),
        ("o", "local source for dockerfile", "", "14kB"),
    )
) + "\n"
OWNED_IDS = tuple(letter * 25 for letter in "gfedcba")
USAGE_ARGV = ("docker", "buildx", "du", "--builder", "default", "--format", "json")
PRUNE_ARGV = (
    "docker", "buildx", "prune", "--builder", "default", "--all", "--force",
    "--filter", f"id=^({'|'.join(OWNED_IDS)})$",
)
RECLAIMED = "71.25MB"
PRUNE_OUTPUT = (
    "ID\tRECLAIMABLE\tSIZE\tLAST ACCESSED\n"
    + "".join(f"{record_id}\ttrue\t1.5MB\t3 minutes ago\n" for record_id in OWNED_IDS)
    + f"Total:\t{RECLAIMED}\n"
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
        ("docker", "build", "--builder", "default", "--provenance=false", "--sbom=false", "--build-arg", f"BASE={repository}@{BASE_DIGEST}", "--build-arg", f"PGBACKREST_VERSION={VERSION}", "--label", "org.opencontainers.image.base.name=docker.io/library/example:18", "--label", f"org.opencontainers.image.base.digest={BASE_DIGEST}", "--label", f"gideon.build-arg.PGBACKREST_VERSION={VERSION}", "--tag", tag, "/repo/images/postgres"): done(("docker", "build")),
        smoke_argv: smoke or done(smoke_argv, stdout=f"pgBackRest {VERSION.split('-', 1)[0]}\n"),
        ("docker", "push", tag): done(("docker", "push", tag)),
        ("docker", "image", "inspect", "--format", "{{json .RepoDigests}}", tag): done(("docker", "image", "inspect"), stdout=json.dumps([f"{repository}@{digest}"])),
        USAGE_ARGV: done(USAGE_ARGV, stdout=USAGE_TEXT),
        PRUNE_ARGV: done(PRUNE_ARGV, stdout=PRUNE_OUTPUT),
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
    def test_every_committed_built_pin_has_a_smoke_with_declared_version_args(self) -> None:
        result = load_image_lock(ROOT / "images.lock")
        assert result.lock is not None
        for pin in result.lock.images:
            if isinstance(pin, BuiltImagePin):
                with self.subTest(image=pin.name):
                    self.assertIn(pin.name, SMOKE)
                    self.assertTrue(set(SMOKE[pin.name].version_args) <= set(pin.build_args))

    def test_opensearch_smoke_reads_plugin_descriptor_without_starting_node(self) -> None:
        result = load_image_lock(ROOT / "images.lock")
        assert result.lock is not None
        pin = next(pin for pin in result.lock.images if pin.name == "opensearch")
        assert isinstance(pin, BuiltImagePin)
        smoke = SMOKE[pin.name]
        self.assertEqual(
            smoke.argv,
            ("cat", "/usr/share/opensearch/plugins/prometheus-exporter/plugin-descriptor.properties"),
        )
        self.assertEqual(smoke.version_args, ("PROMETHEUS_EXPORTER_VERSION",))

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
        self.assertEqual(plan.usage_argv, USAGE_ARGV)
        self.assertEqual(plan.prune_argv, PRUNE_ARGV[:-1])
        self.assertEqual(plan.owner, "pulled from 127.0.0.1:5000/")

    def test_cache_owner_follows_destination_registry_prefix(self) -> None:
        result = load_image_lock_text(LOCK_TEXT)
        assert result.lock is not None
        pin = result.lock.images[0]
        assert isinstance(pin, BuiltImagePin)
        plan = build_plan(pin, RegistryTarget("https", "registry.example", "fictitious/"), REPO)
        self.assertEqual(plan.owner, "pulled from registry.example/fictitious/")
        records = parse_usage(USAGE_TEXT.replace("127.0.0.1:5000/", "registry.example/fictitious/"))
        self.assertEqual(owned_records(records, plan.owner), OWNED_IDS)
        self.assertEqual(owned_records(parse_usage(USAGE_TEXT), plan.owner), ())

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
                "PSYCOPG_VERSION",
                "PROCRASTINATE_VERSION",
                "PSYCOPG_POOL_VERSION",
                "ASGIREF_VERSION",
                "ATTRS_VERSION",
                "CRONITER_VERSION",
                "PYTHON_DATEUTIL_VERSION",
                "SIX_VERSION",
                "PACKAGING_VERSION",
                "EYECITE_VERSION",
                "REPORTERS_DB_VERSION",
                "COURTS_DB_VERSION",
                "PYAHOCORASICK_VERSION",
                "LXML_VERSION",
                "REGEX_VERSION",
                "TIKTOKEN_VERSION",
                "REQUESTS_VERSION",
                "URLLIB3_VERSION",
                "CHARSET_NORMALIZER_VERSION",
                "FAST_DIFF_MATCH_PATCH_VERSION",
            ),
        )
        for package in ("starlette", "uvicorn", "httpx", "psycopg", "procrastinate", "eyecite", "tiktoken"):
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
            "psycopg",
            "psycopg-binary",
            "procrastinate",
            "psycopg-pool",
            "asgiref",
            "attrs",
            "croniter",
            "python-dateutil",
            "six",
            "packaging",
            "eyecite",
            "reporters-db",
            "courts-db",
            "pyahocorasick",
            "lxml",
            "regex",
            "tiktoken",
            "requests",
            "urllib3",
            "charset-normalizer",
            "fast-diff-match-patch",
        ):
            self.assertIn(f'"{package}"', smoke.argv[2])
        self.assertIn("eyecite.get_citations", smoke.argv[2])
        self.assertIn("tiktoken.Encoding", smoke.argv[2])
        self.assertIn('encoding.encode("ab")', smoke.argv[2])
        self.assertIn("importlib.metadata.version", smoke.argv[2])
        self.assertIn("if os.getuid() == 0:", smoke.argv[2])
        self.assertIn("default user must be unprivileged", smoke.argv[2])
        self.assertIn('psycopg.pq.__impl__ != "binary"', smoke.argv[2])
        self.assertIn(
            'importlib.metadata.version("psycopg") '
            '!= importlib.metadata.version("psycopg-binary")',
            smoke.argv[2],
        )


PROXY = {
    "HTTP_PROXY": "http://user:s3cret@proxy.example:3128",
    "HTTPS_PROXY": "http://user:s3cret@proxy.example:3128",
    "NO_PROXY": "127.0.0.1,localhost",
}


class CacheOwnership(unittest.TestCase):
    """Cache attribution uses registry pulls and wholly owned ancestry."""

    def test_usage_preserves_record_fields_and_skips_blank_lines(self) -> None:
        records = parse_usage("\n \n" + USAGE_TEXT + "\n")
        source = [json.loads(line) for line in USAGE_TEXT.splitlines()]
        self.assertEqual(len(records), len(source))
        for record, item in zip(records, source, strict=True):
            with self.subTest(record=record.id):
                self.assertEqual(record.id, item["ID"])
                self.assertEqual(record.description, item["Description"])
                self.assertEqual(record.size, item["Size"])
                self.assertEqual(record.parents, tuple(item["Parents"] or ()))

    def test_null_parents_parse_as_no_parents(self) -> None:
        line = next(line for line in USAGE_TEXT.splitlines() if json.loads(line)["ID"] == "k" * 25)
        self.assertIsNone(json.loads(line)["Parents"])
        self.assertEqual(parse_usage(line), (CacheRecord("k" * 25, "", (), "10MB"),))

    def test_missing_parents_parse_as_no_parents(self) -> None:
        item = json.loads(USAGE_TEXT.splitlines()[0])
        del item["Parents"]
        self.assertEqual(parse_usage(json.dumps(item))[0].parents, ())

    def test_ownership_reaches_fixpoint_and_keeps_listing_order(self) -> None:
        records = parse_usage(USAGE_TEXT)
        self.assertEqual(owned_records(records, "pulled from 127.0.0.1:5000/"), OWNED_IDS)
        self.assertEqual(
            owned_records(tuple(reversed(records)), "pulled from 127.0.0.1:5000/"),
            tuple(reversed(OWNED_IDS)),
        )

    def test_foreign_parentless_anonymous_chains_and_mixed_parents_stay_unowned(self) -> None:
        records = parse_usage(USAGE_TEXT)
        owned = owned_records(records, "pulled from 127.0.0.1:5000/")
        for letter in "hijklmno":
            with self.subTest(record=letter):
                self.assertNotIn(letter * 25, owned)

    def test_filter_is_anchored_and_matches_only_owned_ids(self) -> None:
        value = prune_filter(OWNED_IDS)
        self.assertEqual(value, PRUNE_ARGV[-1])
        expression = re.compile(value.removeprefix("id="))
        for record in parse_usage(USAGE_TEXT):
            with self.subTest(record=record.id):
                self.assertEqual(expression.search(record.id) is not None, record.id in OWNED_IDS)
        self.assertIsNone(expression.search("z" + OWNED_IDS[0]))
        self.assertIsNone(expression.search(OWNED_IDS[0] + "z"))

    def test_empty_filter_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            prune_filter(())

    def test_malformed_usage_is_refused(self) -> None:
        for text in ("not json", "[]", "null", '{"Description": "fictitious"}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_usage(text)


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
        prune_row = f"postgres: pruned — {RECLAIMED} reclaimed from {len(OWNED_IDS)} of {len(OWNED_IDS)} records"
        self.assertIn(prune_row, out)
        self.assertLess(out.index(prune_row), out.index("postgres: built"))
        self.assertEqual(
            [call[0] for call in host.calls],
            [
                *build_commands().keys(),
                (sys.executable, "tests/regenerate_render_fixtures.py"),
            ],
        )
        self.assertEqual(host.calls[-1][2], REPO)
        for argv, _, _ in host.calls:
            if argv[:2] == ("docker", "build") or argv[:2] == ("docker", "buildx"):
                with self.subTest(argv=argv):
                    self.assertEqual(argv[argv.index("--builder") + 1], "default")
            if argv[:3] == ("docker", "buildx", "prune"):
                self.assertEqual(argv, PRUNE_ARGV)
        lock = load_image_lock_text(host.files[LOCK_PATH]).lock
        assert lock is not None
        pin = lock.images[0]
        assert isinstance(pin, BuiltImagePin)
        self.assertEqual(pin.digest, PUSHED_DIGEST)
        self.assertEqual(
            pin.inputs_digest,
            compute_inputs_digest(BASE_DIGEST, {"PGBACKREST_VERSION": VERSION}, DOCKERFILE.encode()),
        )

    def test_prune_shortfall_reports_removed_and_named_counts(self) -> None:
        commands = build_commands()
        output = PRUNE_OUTPUT.replace(f"{OWNED_IDS[-1]}\ttrue\t1.5MB\t3 minutes ago\n", "")
        commands[PRUNE_ARGV] = done(PRUNE_ARGV, stdout=output)
        host = FakeHost(commands, files=files())
        code, out, err = run_tool(host)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(
            f"postgres: pruned — {RECLAIMED} reclaimed from {len(OWNED_IDS) - 1} of {len(OWNED_IDS)} records",
            out,
        )

    def test_prune_without_total_reports_unreported_size(self) -> None:
        commands = build_commands()
        commands[PRUNE_ARGV] = done(PRUNE_ARGV, stdout=PRUNE_OUTPUT.rsplit("Total:", 1)[0])
        host = FakeHost(commands, files=files())
        code, out, err = run_tool(host)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(
            f"postgres: pruned — unreported reclaimed from {len(OWNED_IDS)} of {len(OWNED_IDS)} records",
            out,
        )

    def test_no_owned_records_skips_prune_and_still_records_digest(self) -> None:
        commands = build_commands()
        usage = "\n".join(
            line for line in USAGE_TEXT.splitlines() if json.loads(line)["ID"] not in OWNED_IDS
        )
        commands[USAGE_ARGV] = done(USAGE_ARGV, stdout=usage)
        host = FakeHost(commands, files=files())
        code, out, err = run_tool(host)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("postgres: pruned — nothing to prune", out)
        self.assertEqual(
            [call[0] for call in host.calls],
            [*(argv for argv in commands if argv != PRUNE_ARGV),
             (sys.executable, "tests/regenerate_render_fixtures.py")],
        )
        lock = load_image_lock_text(host.files[LOCK_PATH]).lock
        assert lock is not None
        self.assertEqual(lock.images[0].digest, PUSHED_DIGEST)

    def test_cache_failure_still_records_digest_regenerates_fixtures_and_prints_built(self) -> None:
        cases = (
            (
                PRUNE_ARGV,
                done(PRUNE_ARGV, 1, stdout=PRUNE_OUTPUT, stderr="fictitious prune failure"),
                "Docker buildx prune failed: fictitious prune failure",
            ),
            (
                USAGE_ARGV,
                done(USAGE_ARGV, 1, stdout=USAGE_TEXT, stderr="fictitious usage failure"),
                "Docker buildx du failed: fictitious usage failure",
            ),
            (
                USAGE_ARGV,
                done(USAGE_ARGV, 127, stderr="fictitious unavailable buildx"),
                "Docker buildx du is not available",
            ),
            (
                USAGE_ARGV,
                done(USAGE_ARGV, 1, stderr="permission denied"),
                "Docker refused buildx du: permission denied",
            ),
            (
                USAGE_ARGV,
                done(USAGE_ARGV, stdout="not JSON"),
                "Docker buildx du returned invalid cache usage",
            ),
        )
        for failed_argv, failure, problem in cases:
            with self.subTest(problem=problem):
                commands = build_commands()
                commands[failed_argv] = failure
                host = FakeHost(commands, files=files())
                code, out, err = run_tool(host)
                self.assertEqual((code, err), (1, ""), out)
                self.assertIn(f"postgres: prune failed — {problem}", out)
                self.assertIn(
                    "Fix: Run sudo docker buildx prune --builder default --all --force --filter '",
                    out,
                )
                if failed_argv == PRUNE_ARGV:
                    self.assertIn(PRUNE_ARGV[-1], out)
                self.assertIn("by hand, then re-run image build --check", out)
                self.assertIn("postgres: built — 127.0.0.1:5000/postgres@" + PUSHED_DIGEST, out)
                self.assertLess(out.index("postgres: prune failed"), out.index("postgres: built"))
                expected_calls = [argv for argv in commands if failed_argv == PRUNE_ARGV or argv != PRUNE_ARGV]
                self.assertEqual(
                    [call[0] for call in host.calls],
                    [*expected_calls, (sys.executable, "tests/regenerate_render_fixtures.py")],
                )
                self.assertIn(LOCK_PATH, host.writes)
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
        self.assertFalse(any("buildx" in call[0] for call in host.calls))

    def test_failed_push_does_not_prune_or_write_lock(self) -> None:
        commands = build_commands()
        push = ("docker", "push", "127.0.0.1:5000/postgres:18-gideon")
        commands[push] = done(push, 1, stderr="fictitious registry failure")
        host = FakeHost(commands, files=files())
        code, out, err = run_tool(host)
        self.assertEqual((code, err), (1, ""))
        self.assertIn("Docker push failed", out)
        self.assertFalse(any("buildx" in call[0] for call in host.calls))
        self.assertEqual(host.writes, [])
        self.assertEqual(host.files[LOCK_PATH], LOCK_TEXT)

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
        self.assertEqual([call[0] for call in host.calls], list(self.check_commands()))
        self.assertFalse(any("buildx" in call[0] for call in host.calls))

    def test_check_label_mismatch_names_the_label(self) -> None:
        host = FakeHost(
            self.check_commands({"org.opencontainers.image.base.name": "wrong"}),
            files=files(),
        )
        code, out, err = run_tool(host, argv=["postgres", "--to", "127.0.0.1:5000", "--check"])
        self.assertEqual((code, err), (1, ""))
        self.assertIn("label org.opencontainers.image.base.name", out)
        self.assertFalse(any("buildx" in call[0] for call in host.calls))


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
        self.assertIn(
            "postgres: would prune — docker buildx du --builder default --format json, "
            "then docker buildx prune --builder default --all --force --filter "
            "id=^(<GIDEON's record ids>)$",
            out,
        )
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
