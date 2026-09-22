"""Contracts for the CI sibling stack tool (plan §4)."""

import contextlib
import io
import os
import subprocess
import unittest
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import cast
from unittest.mock import patch

from gideon.host import nogpu, owui
from gideon.host import stack as host_stack
from gideon.host.render import ARTIFACTS, RenderInputs
from gideon.host.render.api import API_SERVICE_NAME
from gideon.host.render.ci import (
    CI_PORT,
    CI_ROOT,
    CI_SECRETS_DIR,
    CI_SKIPPED_SECRETS,
    CI_WIPE_PATHS,
)
from gideon.host.report import StageResult
from gideon.host.secrets import EnsureResult
from gideon.host.sysio import Command, PathLike
from tools.cistack import cli
from tools.cistack import run as cistack_run

ROOT = Path(__file__).resolve().parents[1]
SITE_PATH = Path("/etc/gideon/site.yaml")
TEMPLATE_PATHS = tuple(
    dict.fromkeys(path for artifact in ARTIFACTS for path in artifact.template_paths)
)


def completed(
    argv: Sequence[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)


class FakeHost:
    """A dict-backed Host whose command results are explicit and inspectable."""

    def __init__(self, *, euid: int = 0, network_returncode: int = 0) -> None:
        self.euid = euid
        self.network_returncode = network_returncode
        self.files: dict[str, str] = {
            os.fspath(SITE_PATH): (ROOT / "config/site.example.yaml").read_text(
                encoding="utf-8"
            ),
        }
        self.directories: set[str] = {CI_ROOT, CI_SECRETS_DIR}
        self.commands: list[tuple[str, ...]] = []
        self.writes: list[tuple[str, int]] = []
        self._seed_checkout()

    def _seed_checkout(self) -> None:
        for relative in ("host.lock", "images.lock", "models.lock"):
            path = ROOT / relative
            self.files[os.fspath(path)] = path.read_text(encoding="utf-8")

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
        del check, input, cwd, env, timeout, passthrough
        command = tuple(argv)
        self.commands.append(command)
        if command[:3] == ("docker", "network", "inspect"):
            return completed(
                command,
                returncode=self.network_returncode,
                stdout='[{"Containers": {}}]\n',
                stderr="network unavailable\n" if self.network_returncode else "",
            )
        if command[:2] == ("docker", "compose") and "ps" in command:
            rows = "\n".join(
                f'{{"Service":"{name}","State":"running","Health":"healthy"}}'
                for name in cistack_run.CI_SERVICES
            )
            return completed(command, stdout=rows + "\n")
        if command and command[0] == "rm" and len(command) == 3:
            self.remove(command[2])
        if command[:2] == ("getent", "group"):
            return completed(command, stdout="gideon:x:1001:\n")
        return completed(command)

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
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
        self.files[key] = text
        self.directories.add(os.path.dirname(key))
        self.writes.append((key, mode))

    def exists(self, path: PathLike) -> bool:
        key = os.fspath(path)
        return key in self.files or key in self.directories

    def listdir(self, path: PathLike) -> list[str]:
        key = os.fspath(path)
        if not self.exists(key):
            raise FileNotFoundError(key)
        prefix = key.rstrip("/") + "/"
        children: set[str] = set()
        for candidate in (*self.files, *self.directories):
            if not candidate.startswith(prefix):
                continue
            remainder = candidate.removeprefix(prefix)
            if remainder and "/" not in remainder:
                children.add(remainder)
        return sorted(children)

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del missing_ok
        self.files.pop(os.fspath(path), None)

    def stat(self, path: PathLike) -> os.stat_result:
        if not self.exists(path):
            raise FileNotFoundError(os.fspath(path))
        return os.stat_result((0o040755 if os.fspath(path) in self.directories else 0o100644,))

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
        del mode, parents, exist_ok
        self.directories.add(os.fspath(path))

    def remove(self, path: str) -> None:
        prefix = path.rstrip("/") + "/"
        self.files = {
            key: value
            for key, value in self.files.items()
            if key != path and not key.startswith(prefix)
        }
        self.directories = {
            key for key in self.directories if key != path and not key.startswith(prefix)
        }

    def geteuid(self) -> int:
        return self.euid


def ci_stack() -> cistack_run.CiStack:
    return cistack_run.CiStack(
        project_dir=Path(CI_ROOT),
        secrets_dir=Path(CI_SECRETS_DIR),
        base_url=f"http://127.0.0.1:{CI_PORT}",
        checkout=ROOT,
    )


class Preconditions(unittest.TestCase):
    def result(self, host: FakeHost) -> StageResult:
        return cistack_run._preconditions(ci_stack(), host, site_path=SITE_PATH)

    def test_refusal_matrix_names_the_fix_and_stops_at_preconditions(self) -> None:
        cases = (
            ("root", FakeHost(euid=1000), "tools.cistack up"),
            ("no GPU", self._no_gpu_host(), "no-gpu"),
            ("data root", self._missing_data_host(), "host provision"),
            ("network", FakeHost(network_returncode=1), "gideon apply"),
        )
        for name, host, expected in cases:
            with self.subTest(name=name):
                result = self.result(host)
                self.assertFalse(result.ok)
                self.assertIn(expected, result.fix)
                self.assertEqual(result.name, "preconditions")

    @staticmethod
    def _no_gpu_host() -> FakeHost:
        host = FakeHost()
        host.files[os.fspath(nogpu.NO_GPU_PATH)] = "declared\n"
        return host

    @staticmethod
    def _missing_data_host() -> FakeHost:
        host = FakeHost()
        host.directories.remove(CI_ROOT)
        return host

    def test_foreign_render_file_refuses_before_writing(self) -> None:
        host = FakeHost()
        foreign = f"{CI_ROOT}/notes.txt"
        host.files[foreign] = "hand-written\n"
        fake_inputs = cast(RenderInputs, object())
        with (
            patch.object(cistack_run, "ci_compose_document", return_value={"services": {}}),
            patch.object(cistack_run, "ci_env_file", return_value=""),
            patch.object(cistack_run, "ci_manifest", return_value=""),
        ):
            result = cistack_run._render_stage(ci_stack(), host, fake_inputs)
        self.assertFalse(result.ok)
        self.assertIn("notes.txt", result.detail)
        self.assertEqual(host.writes, [])


class Stages(unittest.TestCase):
    def test_up_stage_order_stops_after_first_refusal(self) -> None:
        host = FakeHost()
        events: list[str] = []

        def preconditions(*args: object, **kwargs: object) -> StageResult:
            del args, kwargs
            events.append("preconditions")
            return StageResult("preconditions", True, "ready", "")

        def secrets_stage(*args: object, **kwargs: object) -> tuple[StageResult, RenderInputs | None]:
            del args, kwargs
            events.append("secrets")
            return StageResult("secrets", False, "refused", "fix"), None

        def render_stage(*args: object, **kwargs: object) -> StageResult:
            del args, kwargs
            events.append("render")
            return StageResult("render", True, "", "")

        with (
            patch.object(cistack_run, "_preconditions", preconditions),
            patch.object(cistack_run, "_secrets_stage", secrets_stage),
            patch.object(cistack_run, "_render_stage", render_stage),
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cistack_run.up(ci_stack(), host, site_path=SITE_PATH, sleep=lambda _: None)
        self.assertEqual(code, 1)
        self.assertEqual(events, ["preconditions", "secrets"])
        self.assertNotIn("render: ok", output.getvalue())

    def test_idempotent_up_reuses_the_same_ordered_stages(self) -> None:
        host = FakeHost()
        inputs = cast(RenderInputs, object())
        events: list[str] = []

        def record(name: str, result: StageResult) -> Callable[..., StageResult]:
            def stage(*args: object, **kwargs: object) -> StageResult:
                del args, kwargs
                events.append(name)
                return result

            return stage

        def secrets_stage(*args: object, **kwargs: object) -> tuple[StageResult, RenderInputs]:
            del args, kwargs
            events.append("secrets")
            return StageResult("secrets", True, "", ""), inputs

        with (
            patch.object(cistack_run, "_preconditions", record("preconditions", StageResult("preconditions", True, "", ""))),
            patch.object(cistack_run, "_secrets_stage", secrets_stage),
            patch.object(cistack_run, "_render_stage", record("render", StageResult("render", True, "", ""))),
            patch.object(cistack_run, "_stores_stage", record("stores", StageResult("stores", True, "", ""))),
            patch.object(cistack_run, "_start_stage", record("start", StageResult("start", True, "", ""))),
            patch.object(cistack_run, "_manifest_stage", record("apply-manifest", StageResult("apply-manifest", True, "", ""))),
            patch.object(cistack_run, "_verify_stage", record("verify", StageResult("verify", True, "", ""))),
        ):
            self.assertEqual(cistack_run.up(ci_stack(), host, site_path=SITE_PATH), 0)
            first = tuple(events)
            events.clear()
            self.assertEqual(cistack_run.up(ci_stack(), host, site_path=SITE_PATH), 0)
        self.assertEqual(tuple(events), first)
        self.assertEqual(
            first,
            ("preconditions", "secrets", "render", "stores", "start", "apply-manifest", "verify"),
        )

    def test_compose_and_exec_argv_use_the_ci_project_directory(self) -> None:
        host = FakeHost()
        with patch.object(cistack_run.apply, "wait_for_services", return_value=(True, "", "postgres")):
            with patch.object(cistack_run.stores, "converge", return_value=type("Report", (), {"ok": True})()):
                self.assertTrue(cistack_run._stores_stage(ci_stack(), host, sleep=lambda _: None).ok)
            self.assertTrue(cistack_run._start_stage(ci_stack(), host).ok)
        for command in host.commands:
            if command[:2] == ("docker", "compose"):
                self.assertEqual(command[2], "--project-directory")
                self.assertEqual(command[3], CI_ROOT)
                self.assertEqual(command[5], f"{CI_ROOT}/compose.yaml")

        client = type("Frontend", (), {"ready": lambda self: True})()
        with patch.object(cistack_run.apply, "wait_for_services", return_value=(True, "", "")):
            result = cistack_run._verify_stage(
                ci_stack(), host, client_factory=lambda **_: cast(owui.Client, client), sleep=lambda _: None
            )
        self.assertTrue(result.ok)
        self.assertIn(
            tuple(
                host_stack.exec_argv(
                    CI_ROOT,
                    API_SERVICE_NAME,
                    "python3",
                    "-c",
                    cistack_run._HEALTH_SCRIPT,
                )
            ),
            host.commands,
        )
        self.assertNotIn("/etc/gideon/rendered", " ".join(" ".join(command) for command in host.commands))
        self.assertNotIn("/data/backup-staging", " ".join(" ".join(command) for command in host.commands))


class DownAndSecrets(unittest.TestCase):
    def test_down_keeps_data_without_wipe_and_wipes_only_named_paths(self) -> None:
        host = FakeHost()
        compose_path = f"{CI_ROOT}/compose.yaml"
        host.files[compose_path] = "name: gideon-ci\n"
        generated = f"{CI_SECRETS_DIR}/postgres_superuser_password"
        admin_key = f"{CI_SECRETS_DIR}/gideon_admin_api_key"
        eval_key = f"{CI_SECRETS_DIR}/gideon_eval_api_key"
        for path in (generated, admin_key, eval_key):
            host.files[path] = "value\n"

        self.assertEqual(cistack_run.down(ci_stack(), host), 0)
        self.assertIn(generated, host.files)
        self.assertTrue(any(command[:2] == ("docker", "compose") for command in host.commands))
        host.commands.clear()
        self.assertEqual(cistack_run.down(ci_stack(), host, wipe=True), 0)
        self.assertIn(generated, host.files)
        self.assertNotIn(admin_key, host.files)
        self.assertNotIn(eval_key, host.files)
        wipe_commands = [command for command in host.commands if command[0] == "rm"]
        self.assertEqual(wipe_commands, [("rm", "-rf", path) for path in CI_WIPE_PATHS])

    def test_selector_precedes_every_render_secret_read(self) -> None:
        host = FakeHost()
        events: list[str] = []
        fake_inputs = cast(RenderInputs, object())

        def select(path: Path) -> None:
            events.append(f"select:{path}")

        def ensure(_host: FakeHost, *, skip: Collection[str] = ()) -> EnsureResult:
            events.append(f"ensure:{','.join(skip)}")
            return EnsureResult()

        with (
            patch.object(cistack_run.secrets, "select_directory", select),
            patch.object(cistack_run.secrets, "ensure_generated", ensure),
            patch.object(cistack_run, "load_render_inputs", lambda *args, **kwargs: (fake_inputs, "", "", "")),
        ):
            result, inputs = cistack_run._secrets_stage(ci_stack(), host, site_path=SITE_PATH)
        self.assertTrue(result.ok)
        self.assertIs(inputs, fake_inputs)
        self.assertEqual(
            events, [f"select:{CI_SECRETS_DIR}", f"ensure:{','.join(CI_SKIPPED_SECRETS)}"]
        )

    def test_bootstrap_uses_the_in_memory_frontend_factory(self) -> None:
        host = FakeHost()
        host.files[f"{CI_ROOT}/open-webui/manifest.yaml"] = "groups: []\nmodels: []\nfunctions: []\n"
        frontend = object()
        calls: list[object] = []

        def factory(**kwargs: object) -> owui.Client:
            calls.append(kwargs)
            return cast(owui.Client, frontend)

        def bootstrap(
            _io: object,
            received_factory: object,
            manifest: object,
            *,
            rendered_dir: Path,
        ) -> owui.BootstrapReport:
            calls.extend((received_factory, manifest, rendered_dir))
            return owui.BootstrapReport()

        with (
            patch.object(cistack_run.owui, "wait_ready", return_value=owui.ReadyResult(True)),
            patch.object(cistack_run.owui, "bootstrap", bootstrap),
        ):
            result = cistack_run._manifest_stage(
                ci_stack(), host, client_factory=factory, sleep=lambda _: None
            )
        self.assertTrue(result.ok)
        self.assertEqual(calls[0], {})
        self.assertIs(calls[1], factory)
        self.assertEqual(calls[3], Path(CI_ROOT))

    def test_up_wipe_up_mints_both_database_issued_keys_again(self) -> None:
        host = FakeHost()
        manifest_path = f"{CI_ROOT}/open-webui/manifest.yaml"
        compose_path = f"{CI_ROOT}/compose.yaml"
        fake_inputs = cast(RenderInputs, object())
        key_names = ("gideon_admin_api_key", "gideon_eval_api_key")
        minted_values: list[tuple[str, str]] = []
        first_values: tuple[str, str] = ("", "")
        second_values: tuple[str, str] = ("", "")

        def render_stage(
            _stack: cistack_run.CiStack, _host: FakeHost, _inputs: RenderInputs
        ) -> StageResult:
            host.files[compose_path] = "name: gideon-ci\n"
            host.files[manifest_path] = "groups: []\nmodels: []\nfunctions: []\n"
            return StageResult("render", True, "", "")

        def bootstrap(
            _io: FakeHost,
            _factory: object,
            _manifest: object,
            *,
            rendered_dir: Path,
        ) -> owui.BootstrapReport:
            self.assertEqual(rendered_dir, Path(CI_ROOT))
            values: list[str] = []
            for name in key_names:
                value = f"{name}-{len(minted_values)}"
                self.assertIsNone(cistack_run.secrets.write_secret(host, name, value))
                values.append(value)
            minted_values.append((values[0], values[1]))
            return owui.BootstrapReport(minted=key_names)

        success = StageResult("stage", True, "", "")
        original_directory = cistack_run.secrets.current_directory()
        try:
            with (
                patch.object(
                    cistack_run,
                    "load_render_inputs",
                    return_value=(fake_inputs, "", "", ""),
                ),
                patch.object(cistack_run, "_preconditions", return_value=success),
                patch.object(cistack_run, "_render_stage", render_stage),
                patch.object(cistack_run, "_stores_stage", return_value=success),
                patch.object(cistack_run, "_start_stage", return_value=success),
                patch.object(cistack_run, "_verify_stage", return_value=success),
                patch.object(
                    cistack_run.owui,
                    "wait_ready",
                    return_value=owui.ReadyResult(True),
                ),
                patch.object(cistack_run.owui, "bootstrap", bootstrap),
            ):
                def client_factory(**_: object) -> owui.Client:
                    return cast(owui.Client, object())

                self.assertEqual(
                    cistack_run.up(
                        ci_stack(),
                        host,
                        site_path=SITE_PATH,
                        client_factory=client_factory,
                    ),
                    0,
                )
                first_values = tuple(
                    host.files[f"{CI_SECRETS_DIR}/{name}"].strip()
                    for name in key_names
                )  # type: ignore[assignment]
                self.assertEqual(cistack_run.down(ci_stack(), host, wipe=True), 0)
                self.assertEqual(
                    [host.files.get(f"{CI_SECRETS_DIR}/{name}") for name in key_names],
                    [None, None],
                )
                self.assertEqual(
                    cistack_run.up(
                        ci_stack(),
                        host,
                        site_path=SITE_PATH,
                        client_factory=client_factory,
                    ),
                    0,
                )
                second_values = tuple(
                    host.files[f"{CI_SECRETS_DIR}/{name}"].strip()
                    for name in key_names
                )  # type: ignore[assignment]
        finally:
            cistack_run.secrets.select_directory(original_directory)
        self.assertEqual(len(minted_values), 2)
        self.assertNotEqual(first_values, second_values)


class Cli(unittest.TestCase):
    def test_default_client_is_plain_loopback_http(self) -> None:
        host = FakeHost()
        captured: dict[str, object] = {}

        def fake_up(
            ci: cistack_run.CiStack,
            _host: object,
            *,
            site_path: PathLike,
            client_factory: object,
        ) -> int:
            captured.update({"stack": ci, "site": site_path, "factory": client_factory})
            return 0

        with patch.object(cli.run, "up", fake_up):
            self.assertEqual(cli.main(["up"], host=host, checkout=ROOT, site_path=SITE_PATH), 0)
        stack_value = cast(cistack_run.CiStack, captured["stack"])
        self.assertEqual(stack_value.base_url, f"http://127.0.0.1:{CI_PORT}")
        self.assertEqual(captured["site"], SITE_PATH)

    def test_relative_checkout_is_made_absolute(self) -> None:
        captured: dict[str, object] = {}

        def fake_up(
            ci: cistack_run.CiStack,
            _host: object,
            *,
            site_path: PathLike,
            client_factory: object,
        ) -> int:
            captured["stack"] = ci
            return 0

        with patch.object(cli.run, "up", fake_up):
            self.assertEqual(
                cli.main(["up", "--checkout", "."], host=FakeHost(), site_path=SITE_PATH), 0
            )
        checkout = cast(cistack_run.CiStack, captured["stack"]).checkout
        self.assertTrue(checkout.is_absolute())
        self.assertEqual(checkout, Path.cwd().resolve())
