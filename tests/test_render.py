"""Render contracts: host facts, the artifacts, and the pure core (spec §3.5)."""

import argparse
import contextlib
import hashlib
import io
import os
import stat as stat_module
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import yaml  # type: ignore[import-untyped]

from gideon.host import nogpu, weights
from gideon.host.images import (
    ImageLock,
    MirroredImagePin,
    load_image_lock,
    load_image_lock_text,
)
from gideon.host.lock import load_host_lock
from gideon.host.models import (
    GIGABYTE,
    HardwareProfile,
    MemoryRow,
    load_models_lock,
    select_profile,
)
from gideon.host.render import ARTIFACTS, RenderInputs, render_all
from gideon.host.render.api import (
    API_JOB_NAME,
    API_SERVICE_NAME,
    api_base_url,
    api_health_url,
)
from gideon.host.render.caddy import CaddyfileArtifact
from gideon.host.render.command import (
    AppliedManifest,
    RecreateJudgment,
    compose_digests,
    manifest_document,
    read_applied_manifest,
    recreate_judgment,
    recreate_services,
    run_render,
)
from gideon.host.render.compose import (
    BUILD_BOX_UNIT_PATTERN,
    ENGINE_ACCESS_LOG_EXCLUDED_PATHS,
    ENGINE_HEALTHCHECK,
    ENGINE_READY_SECONDS,
    ENGINE_USAGE_SWITCHES,
    OWUI_COMMAND,
    UNIT_PATTERN,
    ComposeArtifact,
    engine_command,
    engine_service,
    engine_wrapper,
    grafana_environment,
    service_images,
    service_names,
)
from gideon.host.render.drill import drill_compose_document
from gideon.host.render.engine import (
    ENGINE_JOB_NAME,
    ENGINE_PORT,
    ENGINE_SECRET_NAME,
    ENGINE_SERVICE_NAME,
    engine_metrics_target,
)
from gideon.host.render.facts import FactsError, HostFacts, gather_facts
from gideon.host.render.grafana import GRAFANA_ADMIN_USER
from gideon.host.render.pgbackrest import (
    PGDATA,
    REPOSITORY_PATH,
    STANZA,
    PgBackRestConfArtifact,
)
from gideon.host.render.prometheus import (
    BLACKBOX_TEMPLATE,
    BlackboxConfigArtifact,
    PrometheusConfigArtifact,
)
from gideon.host.render.searxng import SEARXNG_JOB_NAME, searxng_health_url
from gideon.host.site import SiteConfig, load_site
from gideon.host.sysio import Command, PathLike

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config/site.example.yaml"
SECOND = ROOT / "tests/fixtures/site/second-office.yaml"
TEMPLATE = "caddy/Caddyfile.tmpl"
PROMETHEUS_TEMPLATE = "prometheus/prometheus.yml.tmpl"
PERMISSIONS_TEMPLATE = "open-webui/permissions.yaml"
SERVICE_TEMPLATE = "systemd/gideon-users-reconcile.service.tmpl"
TIMER_TEMPLATE = "systemd/gideon-users-reconcile.timer.tmpl"
PG_BACKREST_TEMPLATE = "postgres/pgbackrest.conf.tmpl"
TEMPLATE_PATHS = tuple(
    dict.fromkeys(path for artifact in ARTIFACTS for path in artifact.template_paths)
)
FACTS = HostFacts(
    ("GPU-11111111-1111-1111-1111-111111111111", "GPU-22222222-2222-2222-2222-222222222222"),
    service_gid=4242,
)
NVIDIA_SMI_L = (
    "GPU 0: NVIDIA RTX PRO 6000 Blackwell Server Edition (UUID: GPU-aaaa)\n"
    "GPU 1: NVIDIA RTX PRO 6000 Blackwell Server Edition (UUID: GPU-bbbb)\n"
)


class FakeHost:
    """A command-and-file Host for the facts gatherer."""

    def __init__(self, *, commands: Mapping[tuple[str, ...], subprocess.CompletedProcess[str]] | None = None) -> None:
        self.commands = dict(commands or {})
        self.calls: list[tuple[str, ...]] = []

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
        self.calls.append(command)
        return self.commands.get(command, subprocess.CompletedProcess(list(command), 127, "", ""))

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        raise FileNotFoundError(os.fspath(path))

    def write_text(self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644) -> None:
        raise NotImplementedError

    def exists(self, path: PathLike) -> bool:
        return False

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


def site(path: Path = EXAMPLE) -> SiteConfig:
    result = load_site(path)
    assert result.config is not None, result.errors
    return result.config


def inputs(site_path: Path = EXAMPLE, **overrides: object) -> RenderInputs:
    lock = load_host_lock(ROOT / "host.lock").lock
    images = load_image_lock(ROOT / "images.lock").lock
    models = load_models_lock(ROOT / "models.lock").lock
    site_config = site(site_path)
    assert lock is not None and images is not None and models is not None
    profile = select_profile(models, site_config.hardware_profile)
    assert isinstance(profile, HardwareProfile)
    base = RenderInputs(
        site=site_config,
        lock=lock,
        images=images,
        facts=FACTS,
        profile=profile,
        templates={
            path: (ROOT / "compose" / path).read_text() for path in TEMPLATE_PATHS
        },
        release="fixture",
        secrets={
            "ldap_bind_password": "bind-$-'\"#password",
            "postgres_openwebui_password": "postgres-openwebui-password",
            "gideon_admin_password": "gideon-admin-password",
            "engine_api_key": "engine-api-key",
            "gideon_api_key": "gideon-api-key",
            "searxng_secret_key": "searxng-secret-key",
            **(
                {"proxy_auth": "proxy-user:p@$$-'\"#password"}
                if site_path == SECOND
                else {}
            ),
        },
        checkout="/opt/gideon",
        api_sources_digest="sha256:" + "0" * 64,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


class Facts(unittest.TestCase):
    SMI = ("nvidia-smi", "-L")

    def test_gpu_uuids_in_nvidia_smi_order(self) -> None:
        group = ("getent", "group", "gideon")
        host = FakeHost(
            commands={
                self.SMI: subprocess.CompletedProcess(list(self.SMI), 0, NVIDIA_SMI_L, ""),
                group: subprocess.CompletedProcess(list(group), 0, "gideon:x:4242:\n", ""),
            }
        )
        self.assertEqual(gather_facts(host), HostFacts(("GPU-aaaa", "GPU-bbbb"), service_gid=4242))

    def test_missing_service_group_refuses_with_the_service_user_fix(self) -> None:
        host = FakeHost(
            commands={
                self.SMI: subprocess.CompletedProcess(list(self.SMI), 0, NVIDIA_SMI_L, ""),
            }
        )
        facts = gather_facts(host)
        self.assertIsInstance(facts, FactsError)
        assert isinstance(facts, FactsError)
        self.assertIn("service-user", facts.fix)

    def test_absent_nvidia_smi_refuses_naming_the_driver_step(self) -> None:
        facts = gather_facts(FakeHost())
        self.assertIsInstance(facts, FactsError)
        assert isinstance(facts, FactsError)
        self.assertIn("nvidia-driver", facts.fix)
        self.assertIn("host provision --no-gpu", facts.fix)

    def test_no_gpu_facts_skip_nvidia_smi_and_keep_the_service_gid(self) -> None:
        group = ("getent", "group", "gideon")
        host = FakeHost(
            commands={
                group: subprocess.CompletedProcess(
                    list(group), 0, "gideon:x:4242:\n", ""
                )
            }
        )
        self.assertEqual(
            gather_facts(host, no_gpu=True),
            HostFacts((), service_gid=4242),
        )
        self.assertNotIn(self.SMI, host.calls)

    def test_failing_nvidia_smi_refuses_with_its_stderr(self) -> None:
        host = FakeHost(commands={self.SMI: subprocess.CompletedProcess(list(self.SMI), 9, "", "NVML: Driver/library version mismatch")})
        facts = gather_facts(host)
        assert isinstance(facts, FactsError)
        self.assertIn("version mismatch", facts.problem)


class Registry(unittest.TestCase):
    def test_artifacts_are_in_order_with_owners_and_modes(self) -> None:
        self.assertEqual(
            [artifact.relative_path for artifact in ARTIFACTS],
            [
                "compose.yaml",
                "prometheus/prometheus.yml",
                "blackbox/blackbox.yml",
                "grafana/ldap.toml",
                "grafana/provisioning/datasources/datasources.yaml",
                "grafana/provisioning/dashboards/provider.yaml",
                "grafana/provisioning/alerting/contact-points.yaml",
                "grafana/provisioning/alerting/policies.yaml",
                "grafana/provisioning/alerting/time-intervals.yaml",
                "grafana/provisioning/alerting/rules.yaml",
                "grafana/dashboards/overview.json",
                "grafana/dashboards/backup.json",
                "grafana/dashboards/gpu.json",
                "postgres/pgbackrest.conf",
                "caddy/Caddyfile",
                "open-webui/env",
                "open-webui/manifest.yaml",
                "searxng/settings.yml",
                "searxng/env",
                "searxng/logging.json",
                "systemd/gideon-users-reconcile.service",
                "systemd/gideon-users-reconcile.timer",
                "systemd/gideon-backup.service",
                "systemd/gideon-backup.timer",
                "systemd/gideon-backup-drill.service",
                "systemd/gideon-backup-drill.timer",
                "systemd/gideon-backup-verify.service",
                "systemd/gideon-backup-verify.timer",
            ],
        )
        self.assertEqual(ARTIFACTS[0].owners, ())
        self.assertEqual(ARTIFACTS[1].owners, ("prometheus",))
        self.assertEqual(ARTIFACTS[2].owners, ("blackbox-exporter",))
        self.assertEqual(ARTIFACTS[3].owners, ("grafana",))
        # ldap.toml carries a file reference, never the password: readable by
        # Grafana's own unprivileged user like every other rendered file.
        self.assertEqual(ARTIFACTS[3].mode, 0o644)
        self.assertEqual(ARTIFACTS[4].owners, ("grafana",))
        self.assertEqual(ARTIFACTS[5].owners, ("grafana",))
        self.assertEqual(ARTIFACTS[6].owners, ("grafana",))
        self.assertEqual(ARTIFACTS[7].owners, ("grafana",))
        self.assertEqual(ARTIFACTS[8].owners, ("grafana",))
        self.assertEqual(ARTIFACTS[9].owners, ("grafana",))
        self.assertEqual(ARTIFACTS[10].owners, ("grafana",))
        self.assertEqual(ARTIFACTS[11].owners, ("grafana",))
        self.assertEqual(ARTIFACTS[12].owners, ("grafana",))
        self.assertEqual(ARTIFACTS[13].owners, ("postgres",))
        self.assertEqual(ARTIFACTS[14].owners, ("caddy",))
        self.assertEqual(ARTIFACTS[15].owners, ("open-webui",))
        self.assertTrue(ARTIFACTS[15].secret)
        self.assertEqual(ARTIFACTS[15].mode, 0o600)
        self.assertEqual(ARTIFACTS[16].owners, ())
        self.assertEqual(ARTIFACTS[17].owners, ("searxng",))
        self.assertEqual(ARTIFACTS[17].mode, 0o644)
        self.assertEqual(ARTIFACTS[18].owners, ("searxng",))
        self.assertTrue(ARTIFACTS[18].secret)
        self.assertEqual(ARTIFACTS[18].mode, 0o600)
        self.assertEqual(ARTIFACTS[19].relative_path, "searxng/logging.json")
        self.assertEqual(ARTIFACTS[19].owners, ("searxng",))
        self.assertFalse(ARTIFACTS[19].secret)
        self.assertEqual(ARTIFACTS[19].mode, 0o644)
        self.assertTrue(
            all(
                artifact.mode == 0o644
                for artifact in ARTIFACTS
                if artifact.relative_path
                not in {"grafana/ldap.toml", "open-webui/env", "searxng/env"}
            )
        )


class Compose(unittest.TestCase):
    def test_frontend_command_is_explicit_in_production_and_drill_documents(self) -> None:
        documents = (
            yaml.safe_load(ComposeArtifact().emit(inputs())),
            drill_compose_document(inputs()),
        )
        for document in documents:
            with self.subTest(document=document):
                services = document["services"]
                assert isinstance(services, Mapping)
                frontend = services["open-webui"]
                assert isinstance(frontend, Mapping)
                command = frontend["command"]
                assert isinstance(command, list)
                self.assertEqual(command, list(OWUI_COMMAND))
                self.assertEqual(command[:2], ["bash", "start.sh"])
                self.assertEqual(command[2:6], ["--workers", "1", "--ws-per-message-deflate", "true"])
                self.assertEqual(command[-1], "--no-access-log")

    def test_no_gpu_omits_dcgm_but_gpu_hosts_keep_it(self) -> None:
        self.assertIn("dcgm-exporter", service_names(inputs()))
        self.assertNotIn("dcgm-exporter", service_names(inputs(no_gpu=True)))
        self.assertIn(ENGINE_SERVICE_NAME, service_names(inputs()))
        self.assertNotIn(ENGINE_SERVICE_NAME, service_names(inputs(no_gpu=True)))
        self.assertIn("gideon-api", service_names(inputs()))
        self.assertNotIn("gideon-api", service_names(inputs(no_gpu=True)))
        # The images apply pulls follow the rendered services, one per service.
        gpu_images = service_images(inputs())
        no_gpu_images = service_images(inputs(no_gpu=True))
        self.assertEqual(len(gpu_images), len(service_names(inputs())))
        self.assertEqual(len(no_gpu_images), len(service_names(inputs(no_gpu=True))))
        self.assertTrue(any("/dcgm-exporter@" in image for image in gpu_images))
        self.assertFalse(any("/dcgm-exporter@" in image for image in no_gpu_images))
        no_gpu = yaml.safe_load(ComposeArtifact().emit(inputs(no_gpu=True)))
        self.assertNotIn("dcgm-exporter", no_gpu["services"])
        self.assertNotIn(ENGINE_SERVICE_NAME, no_gpu["services"])

    def test_every_rendered_service_has_its_profile_memory_limit_as_the_last_key(self) -> None:
        for rendered_inputs in (inputs(), inputs(no_gpu=True)):
            with self.subTest(no_gpu=rendered_inputs.no_gpu):
                document = yaml.safe_load(ComposeArtifact().emit(rendered_inputs))
                services = document["services"]
                for name in service_names(rendered_inputs):
                    with self.subTest(service=name):
                        row = rendered_inputs.profile.memory_row(name)
                        self.assertIsNotNone(row)
                        assert row is not None
                        block = services[name]
                        self.assertIsInstance(block["mem_limit"], int)
                        self.assertEqual(block["mem_limit"], row.gb * GIGABYTE)
                        self.assertEqual(next(reversed(block)), "mem_limit")

    def test_memory_table_names_equal_the_rendered_service_union(self) -> None:
        profile = inputs().profile
        rendered_names = set(service_names(inputs())) | set(service_names(inputs(no_gpu=True)))
        table_names = {row.service for row in profile.memory}
        self.assertEqual(table_names, rendered_names)

    def test_missing_rendered_memory_row_refuses_with_its_fix(self) -> None:
        rendered_inputs = inputs()
        service = service_names(rendered_inputs)[0]
        profile = replace(
            rendered_inputs.profile,
            memory=tuple(
                row for row in rendered_inputs.profile.memory if row.service != service
            ),
        )
        with self.assertRaises(ValueError) as caught:
            ComposeArtifact().emit(replace(rendered_inputs, profile=profile))
        message = str(caught.exception)
        self.assertIn("Cannot render Compose:", message)
        self.assertIn(f"service '{service}'", message)
        self.assertIn(f"memory.{service}.gb", message)
        self.assertTrue(message.endswith("then re-run render."))

    def test_missing_row_for_an_unrendered_service_refuses_on_every_host(self) -> None:
        # The table is release content, complete on every host: a no-GPU host
        # renders no engine, but a lock without the engine's row is refused
        # there too, so one lock is valid everywhere or nowhere.
        rendered_inputs = inputs(no_gpu=True)
        self.assertNotIn(ENGINE_SERVICE_NAME, service_names(rendered_inputs))
        profile = replace(
            rendered_inputs.profile,
            memory=tuple(
                row for row in rendered_inputs.profile.memory if row.service != ENGINE_SERVICE_NAME
            ),
        )
        with self.assertRaises(ValueError) as caught:
            ComposeArtifact().emit(replace(rendered_inputs, profile=profile))
        message = str(caught.exception)
        self.assertIn(f"service '{ENGINE_SERVICE_NAME}'", message)
        self.assertIn(f"memory.{ENGINE_SERVICE_NAME}.gb", message)
        self.assertTrue(message.endswith("then re-run render."))

    def test_unknown_memory_table_row_refuses_with_its_fix(self) -> None:
        rendered_inputs = inputs()
        extra = MemoryRow("qdrant", rendered_inputs.profile.memory[0].gb, None)
        profile = replace(
            rendered_inputs.profile,
            memory=(*rendered_inputs.profile.memory, extra),
        )
        with self.assertRaises(ValueError) as caught:
            ComposeArtifact().emit(replace(rendered_inputs, profile=profile))
        message = str(caught.exception)
        self.assertIn("qdrant", message)
        for service in service_names(rendered_inputs):
            self.assertIn(service, message)
        self.assertTrue(
            message.endswith(
                "Remove or rename the row in models.lock, then re-run render."
            )
        )


class Engine(unittest.TestCase):
    def test_command_environment_device_volume_secret_and_healthcheck_follow_inputs(self) -> None:
        rendered_inputs = inputs()
        model = rendered_inputs.profile.model("generator")
        self.assertIsNotNone(model)
        assert model is not None
        service = engine_service(rendered_inputs)

        expected_command = [
            model.repo,
            "--revision",
            model.revision,
            "--served-model-name",
            model.serve.served_name,
            "--host",
            "0.0.0.0",
            "--port",
            str(ENGINE_PORT),
            "--disable-access-log-for-endpoints",
            ",".join(ENGINE_ACCESS_LOG_EXCLUDED_PATHS),
        ]
        for name, value in model.serve.flags.items():
            expected_command.append(f"--{name}")
            if value is not True:
                expected_command.append(str(value))
        self.assertEqual(service["command"], expected_command)
        self.assertEqual(service["command"], engine_command(model))
        command = service["command"]
        assert isinstance(command, list)
        for forbidden in (
            "--api-key",
            "--enable-prefix-caching",
            "--mamba-cache-mode",
            "--default-chat-template-kwargs",
            "--enable-log-requests",
            "--enable-log-outputs",
            "--disable-uvicorn-access-log",
        ):
            self.assertNotIn(forbidden, command)
        self.assertFalse(any("speculative" in argument for argument in command))

        expected_environment = dict(model.serve.env)
        expected_environment.update(ENGINE_USAGE_SWITCHES)
        expected_environment["TZ"] = rendered_inputs.site.office.timezone
        self.assertEqual(service["environment"], expected_environment)
        self.assertNotIn("VLLM_API_KEY", expected_environment)
        self.assertEqual(
            service["devices"],
            [f"nvidia.com/gpu={FACTS.gpu_uuids[model.gpu]}"],
        )
        self.assertEqual(
            service["volumes"],
            [f"{weights.MODELS_ROOT}:{model.serve.env['HF_HOME']}:ro"],
        )
        self.assertEqual(service["secrets"], [ENGINE_SECRET_NAME])
        self.assertNotIn("ports", service)
        self.assertNotIn("group_add", service)
        self.assertNotIn("shm_size", service)
        self.assertNotIn("ipc", service)
        self.assertEqual(service["healthcheck"], dict(ENGINE_HEALTHCHECK))
        healthcheck = service["healthcheck"]
        assert isinstance(healthcheck, Mapping)
        self.assertEqual(healthcheck["start_period"], f"{ENGINE_READY_SECONDS}s")
        self.assertIn(f"127.0.0.1:{ENGINE_PORT}/health", healthcheck["test"][-1])
        self.assertEqual(
            model.serve.served_name,
            ENGINE_SERVICE_NAME,
        )
        manifest = yaml.safe_load(
            render_all(rendered_inputs).by_path["open-webui/manifest.yaml"].content
        )
        self.assertEqual(manifest["models"][0]["id"], ENGINE_SERVICE_NAME)
        self.assertEqual(manifest["models"][1]["base_model_id"], ENGINE_SERVICE_NAME)
        frontend = yaml.safe_load(ComposeArtifact().emit(rendered_inputs))["services"]["open-webui"]
        frontend_environment = frontend["environment"]
        self.assertEqual(frontend_environment["DEFAULT_MODELS"], manifest["models"][1]["id"])
        self.assertEqual(frontend_environment["TASK_MODEL_EXTERNAL"], ENGINE_SERVICE_NAME)
        self.assertEqual(urlsplit(frontend_environment["OPENAI_API_BASE_URLS"]).hostname, API_SERVICE_NAME)
        self.assertEqual(frontend_environment["OPENAI_API_BASE_URLS"], api_base_url())

    def test_wrapper_has_its_dollar_zero_element_so_arguments_start_at_the_repository(self) -> None:
        wrapper = engine_wrapper("/run/secrets/engine_api_key", "vllm serve")
        self.assertEqual(len(wrapper), 4)
        self.assertEqual(wrapper[:2], ["sh", "-c"])
        self.assertEqual(wrapper[3], ENGINE_SERVICE_NAME)
        self.assertIn('exec vllm serve "$@"', wrapper[2])
        self.assertIn("set -eu", wrapper[2])

    def test_wrapper_exports_a_temporary_secret_to_the_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "engine-key"
            secret.write_text("fixture-engine-key\n")
            server = sys.executable
            result = subprocess.run(
                engine_wrapper(str(secret), server)
                + [
                    "-c",
                    "import os; raise SystemExit(os.environ['VLLM_API_KEY'] != 'fixture-engine-key')",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrapper_refuses_absent_and_empty_secret_without_exec(self) -> None:
        server = sys.executable
        with tempfile.TemporaryDirectory() as directory:
            absent = Path(directory) / "absent-key"
            empty = Path(directory) / "empty-key"
            empty.write_text("")
            for secret in (absent, empty):
                with self.subTest(secret=secret):
                    result = subprocess.run(
                        engine_wrapper(str(secret), server) + ["-c", "raise SystemExit(23)"],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertNotEqual(result.returncode, 23)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(len(result.stderr.splitlines()), 1)
                    self.assertIn(str(secret), result.stderr)

    def test_missing_generator_pin_gpu_and_home_refuse_with_fixes(self) -> None:
        rendered_inputs = inputs()
        model = rendered_inputs.profile.model("generator")
        assert model is not None
        with self.assertRaises(ValueError) as missing_model:
            engine_service(
                replace(
                    rendered_inputs,
                    profile=replace(rendered_inputs.profile, models=()),
                )
            )
        self.assertIn("models.lock", str(missing_model.exception))

        with self.assertRaises(ValueError) as missing_gpu:
            engine_service(replace(rendered_inputs, facts=HostFacts((), FACTS.service_gid)))
        self.assertIn("nvidia-smi -L", str(missing_gpu.exception))
        self.assertIn("hardware-profile", str(missing_gpu.exception))

        without_home = replace(model.serve, env={key: value for key, value in model.serve.env.items() if key != "HF_HOME"})
        profile = replace(rendered_inputs.profile, models=(replace(model, serve=without_home),))
        with self.assertRaises(ValueError) as missing_home:
            engine_service(replace(rendered_inputs, profile=profile))
        self.assertIn("models.lock's serve.env", str(missing_home.exception))

    def test_missing_engine_image_pin_refuses_with_image_pin_shape(self) -> None:
        rendered_inputs = inputs()
        images = ImageLock(
            rendered_inputs.images.version,
            tuple(pin for pin in rendered_inputs.images.images if pin.name != "vllm-openai"),
        )
        with self.assertRaises(ValueError) as missing_pin:
            engine_service(replace(rendered_inputs, images=images))
        self.assertIn("vllm-openai", str(missing_pin.exception))

    def test_public_registry_reference_and_ingress_shape(self) -> None:
        text = ComposeArtifact().emit(inputs())
        self.assertIn('image: "ghcr.io/tnmd-fdo/caddy@sha256:', text)
        self.assertIn('- "0.0.0.0:443:443"', text)
        self.assertIn('file: "/etc/gideon/secrets/tls_key"', text)
        self.assertNotIn("80:80", text)
        self.assertIn('format: "raw"', text)
        self.assertIn('POSTGRES_PASSWORD_FILE: "/run/secrets/postgres_superuser_password"', text)
        self.assertIn('pg_isready', text)
        self.assertIn('image: "ghcr.io/tnmd-fdo/open-webui@sha256:', text)
        self.assertIn('image: "ghcr.io/tnmd-fdo/prometheus@sha256:', text)
        self.assertIn('127.0.0.1:9090:9090', text)
        self.assertIn('--storage.tsdb.retention.time=1y', text)
        self.assertIn('image: "ghcr.io/tnmd-fdo/node-exporter@sha256:', text)
        self.assertIn('pid: "host"', text)
        self.assertIn('--collector.systemd', text)
        self.assertIn('--path.rootfs=/host', text)
        self.assertIn(
            "--collector.systemd.unit-include="
            + BUILD_BOX_UNIT_PATTERN.replace("\\", "\\\\"),
            ComposeArtifact().emit(inputs(build_box=True)),
        )
        self.assertIn(
            "--collector.systemd.unit-include=" + UNIT_PATTERN.replace("\\", "\\\\"),
            text,
        )
        self.assertIn('"/:/host:ro,rslave"', text)
        self.assertIn('/run/dbus/system_bus_socket:/var/run/dbus/system_bus_socket:ro', text)
        # The systemd collector reaches D-Bus only outside Docker's default
        # AppArmor profile (Ubuntu's bus mediates callers by label).
        self.assertIn("apparmor=unconfined", text)
        self.assertIn('image: "ghcr.io/tnmd-fdo/grafana@sha256:', text)
        self.assertIn('GF_SERVER_ROOT_URL: "https://gideon.example.org/grafana/"', text)
        self.assertIn('GF_SERVER_SERVE_FROM_SUB_PATH: "true"', text)
        self.assertIn('GF_AUTH_LDAP_CONFIG_FILE: "/etc/grafana/ldap.toml"', text)
        self.assertIn('GF_SECURITY_ADMIN_USER: "grafana-admin"', text)
        self.assertIn('GF_SECURITY_ADMIN_PASSWORD__FILE: "/run/secrets/grafana_admin_password"', text)
        self.assertIn('GF_SMTP_STARTTLS_POLICY: "OpportunisticStartTLS"', text)
        self.assertIn('group_add:\n      - "4242"', text)
        self.assertIn('/data/observability/grafana:/var/lib/grafana', text)
        self.assertIn('/etc/gideon/rendered/grafana/provisioning:/etc/grafana/provisioning:ro', text)
        self.assertIn('/etc/gideon/rendered/grafana/dashboards:/etc/grafana/dashboards:ro', text)
        self.assertIn('/etc/gideon/rendered/grafana/ldap.toml:/etc/grafana/ldap.toml:ro', text)
        self.assertIn('/etc/gideon/ca.pem:/etc/ssl/certs/gideon-ca.pem:ro', text)
        self.assertIn('  grafana_admin_password:\n    file: "/etc/gideon/secrets/grafana_admin_password"', text)
        self.assertNotIn('  smtp_password:\n', text)
        self.assertEqual(grafana_environment(inputs())["GF_SECURITY_ADMIN_USER"], GRAFANA_ADMIN_USER)
        self.assertNotIn("GF_SMTP_USER", grafana_environment(inputs()))
        smtp = grafana_environment(inputs(SECOND))
        self.assertEqual(smtp["GF_SMTP_STARTTLS_POLICY"], "MandatoryStartTLS")
        self.assertEqual(smtp["GF_SMTP_USER"], "gideon-relay")
        self.assertEqual(smtp["GF_SMTP_PASSWORD__FILE"], "/run/secrets/smtp_password")
        second_text = ComposeArtifact().emit(inputs(SECOND))
        self.assertIn('  smtp_password:\n    file: "/etc/gideon/secrets/smtp_password"', second_text)
        compose = yaml.safe_load(text)
        services = compose["services"]
        self.assertEqual(services["dcgm-exporter"]["devices"], ["nvidia.com/gpu=all"])
        self.assertEqual(services["dcgm-exporter"]["cap_add"], ["SYS_ADMIN"])
        self.assertEqual(services["dcgm-exporter"]["environment"]["TZ"], site().office.timezone)
        postgres_exporter = services["postgres-exporter"]
        self.assertEqual(
            postgres_exporter["environment"],
            {
                "DATA_SOURCE_URI": "postgres:5432/gideon?sslmode=disable",
                "DATA_SOURCE_USER": "gideon_ro_metrics",
                "DATA_SOURCE_PASS_FILE": "/run/secrets/postgres_gideon_ro_metrics_password",
                "TZ": site().office.timezone,
            },
        )
        self.assertEqual(postgres_exporter["group_add"], [str(FACTS.service_gid)])
        self.assertEqual(postgres_exporter["secrets"], ["postgres_gideon_ro_metrics_password"])
        self.assertEqual(
            postgres_exporter["depends_on"],
            {"postgres": {"condition": "service_healthy"}},
        )
        self.assertEqual(
            services["cadvisor"]["command"],
            [
                "--docker_only=true",
                "--housekeeping_interval=30s",
                "--disable_metrics=advtcp,app,cpu_topology,cpuset,hugetlb,memory_numa,perf_event,process,referenced_memory,resctrl,sched,tcp,udp",
            ],
        )
        self.assertTrue(services["cadvisor"]["privileged"])
        self.assertEqual(
            services["cadvisor"]["volumes"],
            [
                "/:/rootfs:ro",
                "/var/run:/var/run:rw",
                "/sys:/sys:ro",
                "/var/lib/docker:/var/lib/docker:ro",
                "/dev/disk:/dev/disk:ro",
            ],
        )
        self.assertEqual(
            services["blackbox-exporter"]["command"],
            ["--config.file=/etc/blackbox_exporter/config.yml"],
        )
        self.assertEqual(
            services["blackbox-exporter"]["volumes"],
            [
                "/etc/gideon/rendered/blackbox/blackbox.yml:/etc/blackbox_exporter/config.yml:ro",
                "/etc/gideon/ca.pem:/etc/gideon/ca.pem:ro",
            ],
        )
        self.assertEqual(text.splitlines()[1], 'name: "gideon"')

    def test_pgbackrest_configuration_uses_each_site_retention_policy(self) -> None:
        for site_path in (EXAMPLE, SECOND):
            with self.subTest(site=site_path.name):
                config = site(site_path)
                text = PgBackRestConfArtifact().emit(inputs(site_path))
                self.assertIn("[global]", text)
                self.assertIn(f"repo1-path={REPOSITORY_PATH}", text)
                self.assertIn("repo1-retention-full-type=time", text)
                self.assertIn(f"repo1-retention-full={config.backup.local_days}", text)
                self.assertIn("start-fast=y", text)
                self.assertIn("log-level-file=off", text)
                self.assertIn("log-level-console=warn", text)
                self.assertIn(f"[{STANZA}]", text)
                self.assertIn(f"pg1-path={PGDATA}", text)

        artifact = PgBackRestConfArtifact()
        self.assertEqual(artifact.relative_path, "postgres/pgbackrest.conf")
        self.assertEqual(artifact.mode, 0o644)
        self.assertEqual(artifact.owners, ("postgres",))

    def test_pgbackrest_configuration_names_missing_template(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            PgBackRestConfArtifact().emit(inputs(templates={}))
        self.assertIn(PG_BACKREST_TEMPLATE, str(ctx.exception))

    def test_loopback_registry_reference(self) -> None:
        text = ComposeArtifact().emit(inputs(SECOND))
        self.assertIn('image: "127.0.0.1:5000/caddy@sha256:', text)

    def test_unusable_registry_key_names_the_fix(self) -> None:
        bad = replace(site(), registry="http://user:pw@ghcr.io")
        with self.assertRaises(ValueError) as ctx:
            ComposeArtifact().emit(inputs(site=bad))
        self.assertIn("registry in /etc/gideon/site.yaml", str(ctx.exception))

    def test_unbuilt_built_pin_refuses_naming_the_build_tool(self) -> None:
        """A built pin still at its `unbuilt` sentinels cannot render a reference (ticket 16)."""

        text = (
            "version: 1\n"
            "images:\n"
            "  caddy:\n"
            "    source: docker.io/library/example:1.0\n"
            "    digest: sha256:" + "1" * 64 + "\n"
            "  open-webui:\n"
            "    source: docker.io/library/example:2.0\n"
            "    digest: sha256:" + "2" * 64 + "\n"
            "  prometheus:\n"
            "    source: docker.io/library/example:3.0\n"
            "    digest: sha256:" + "4" * 64 + "\n"
            "  node-exporter:\n"
            "    source: docker.io/library/example:4.0\n"
            "    digest: sha256:" + "5" * 64 + "\n"
            "  grafana:\n"
            "    source: docker.io/library/example:5.0\n"
            "    digest: sha256:" + "6" * 64 + "\n"
            "  postgres:\n"
            "    build: images/postgres\n"
            "    base: docker.io/library/example:18\n"
            "    base_digest: sha256:" + "3" * 64 + "\n"
            "    inputs_digest: unbuilt\n"
            "    digest: unbuilt\n"
        )
        images = load_image_lock_text(text).lock
        assert images is not None
        with self.assertRaises(ValueError) as ctx:
            ComposeArtifact().emit(inputs(images=images))
        self.assertIn("python3 -m tools.imagebuild postgres", str(ctx.exception))

    def test_missing_caddy_pin_refuses(self) -> None:
        images = ImageLock(
            1,
            (
                MirroredImagePin(
                    "other", "sha256:" + "0" * 64, "docker.io/library/other:1"
                ),
            ),
        )
        with self.assertRaises(ValueError) as ctx:
            ComposeArtifact().emit(inputs(images=images))
        self.assertIn("caddy", str(ctx.exception))


class Caddyfile(unittest.TestCase):
    def test_hostname_is_the_only_substitution(self) -> None:
        text = CaddyfileArtifact().emit(inputs())
        self.assertIn("https://gideon.example.org {", text)
        self.assertNotIn("$", text)
        self.assertIn("auto_https disable_certs", text)
        self.assertIn("tls /etc/gideon/tls/cert.pem /run/secrets/tls_key", text)
        self.assertIn("max_size 2GB", text)
        self.assertIn("reverse_proxy open-webui:8080", text)
        self.assertIn("redir /grafana /grafana/", text)
        self.assertIn("handle /grafana/*", text)
        self.assertIn("reverse_proxy grafana:3000", text)
        self.assertIn("handle {", text)
        self.assertIn("metrics", text)
        self.assertIn("http://:2020", text)
        self.assertIn("metrics\n}", text)
        self.assertIn("flush_interval -1", text)
        self.assertNotIn("503", text)
        self.assertNotIn(":80\n", text)

    def test_filter_encoder_redacts_prompt_parameters_on_all_logged_url_fields(self) -> None:
        parameters = ("q", "shared", "redirect", "load-url", "youtube", "v")
        fields = ("request>uri", "request>headers>Referer", "resp_headers>Location")
        for site_path in (EXAMPLE, SECOND):
            with self.subTest(site=site_path.name):
                text = CaddyfileArtifact().emit(inputs(site_path))
                self.assertIn(f"https://{site(site_path).hostname} {{", text)
                self.assertNotIn("$", text)
                block_start = text.index("    log {\n")
                block_end = text.index("    }\n    redir /grafana", block_start) + len("    }\n")
                block = text[block_start:block_end]
                lines = block.splitlines()
                self.assertIn("        format filter {", lines)
                self.assertIn("            wrap json", lines)
                self.assertNotIn("format json", block)
                self.assertNotIn("delete", block)
                self.assertNotIn("hash", block)
                self.assertNotIn("log_credentials", block)
                self.assertNotIn(";", block)
                self.assertEqual(block.count("replace "), 18)
                for field in fields:
                    with self.subTest(field=field):
                        field_line = f"                {field} query {{"
                        field_index = lines.index(field_line)
                        field_end = lines.index("                }", field_index)
                        self.assertEqual(
                            lines[field_index + 1 : field_end],
                            [f"                    replace {name} redacted" for name in parameters],
                        )

    def test_missing_template_refuses_naming_it(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            CaddyfileArtifact().emit(inputs(templates={}))
        self.assertIn(TEMPLATE, str(ctx.exception))

    def test_unfilled_placeholder_is_named(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            CaddyfileArtifact().emit(inputs(templates={TEMPLATE: "https://$hostname { $upstream }"}))
        self.assertIn("upstream", str(ctx.exception))


class PrometheusConfig(unittest.TestCase):
    def test_initial_scrape_jobs_are_static_and_run_every_fifteen_seconds(self) -> None:
        text = PrometheusConfigArtifact().emit(inputs())
        self.assertIn("scrape_interval: 15s", text)
        self.assertIn("evaluation_interval: 15s", text)
        for job, target in (
            ("prometheus", "prometheus:9090"),
            ("node", "node-exporter:9100"),
            ("grafana", "grafana:3000"),
            ("caddy", "caddy:2020"),
            ("dcgm", "dcgm-exporter:9400"),
            (ENGINE_JOB_NAME, engine_metrics_target()),
            ("postgres", "postgres-exporter:9187"),
            ("cadvisor", "cadvisor:8080"),
        ):
            self.assertIn(f"job_name: {job}", text)
            self.assertIn(f"targets: [{target}]", text)
        for job, module, target in (
            ("ingress", "http_2xx_ca", "https://caddy/"),
            ("frontend", "http_2xx", "http://open-webui:8080/health"),
            (SEARXNG_JOB_NAME, "http_2xx", searxng_health_url()),
            (API_JOB_NAME, "http_2xx", api_health_url()),
        ):
            self.assertIn(f"job_name: {job}", text)
            self.assertIn(f"module: [{module}]", text)
            self.assertIn(f"targets: [{target}]", text)
        self.assertEqual(text.count("target_label: __param_target"), 4)
        self.assertEqual(text.count("replacement: blackbox-exporter:9115"), 4)

    def test_missing_template_is_named(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            PrometheusConfigArtifact().emit(inputs(templates={}))
        self.assertIn(PROMETHEUS_TEMPLATE, str(ctx.exception))

    def test_gpu_jobs_follow_the_gpu_mode(self) -> None:
        gpu_text = PrometheusConfigArtifact().emit(inputs())
        no_gpu_text = PrometheusConfigArtifact().emit(inputs(no_gpu=True))
        self.assertIn("job_name: dcgm", gpu_text)
        self.assertIn("dcgm-exporter:9400", gpu_text)
        self.assertIn(f"job_name: {ENGINE_JOB_NAME}", gpu_text)
        self.assertIn(engine_metrics_target(), gpu_text)
        self.assertNotIn("job_name: dcgm", no_gpu_text)
        self.assertNotIn("dcgm-exporter:9400", no_gpu_text)
        self.assertIn(f"job_name: {API_JOB_NAME}", gpu_text)
        self.assertIn(api_health_url(), gpu_text)
        self.assertNotIn(f"job_name: {API_JOB_NAME}", no_gpu_text)
        self.assertNotIn(f"job_name: {ENGINE_JOB_NAME}", no_gpu_text)
        self.assertNotIn(ENGINE_SERVICE_NAME, no_gpu_text)


class BlackboxConfig(unittest.TestCase):
    def test_modules_carry_each_fixture_hostname_and_the_ca(self) -> None:
        for site_path in (EXAMPLE, SECOND):
            with self.subTest(site=site_path.name):
                text = BlackboxConfigArtifact().emit(inputs(site_path))
                hostname = site(site_path).hostname
                self.assertIn(f'Host: "{hostname}"', text)
                self.assertIn(f'server_name: "{hostname}"', text)
                self.assertIn("ca_file: /etc/gideon/ca.pem", text)
                self.assertIn("fail_if_not_ssl: true", text)
                self.assertIn("http_2xx:", text)

    def test_missing_template_is_named(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            BlackboxConfigArtifact().emit(inputs(templates={}))
        self.assertIn(BLACKBOX_TEMPLATE, str(ctx.exception))


class Core(unittest.TestCase):
    def test_artifacts_apply_on_gpu_hosts_except_the_gpu_board(self) -> None:
        gpu = inputs()
        no_gpu = inputs(no_gpu=True)
        self.assertTrue(all(artifact.applies(gpu) for artifact in ARTIFACTS))
        self.assertEqual(
            [artifact.name for artifact in ARTIFACTS if not artifact.applies(no_gpu)],
            ["grafana-gpu"],
        )

    def test_manifest_records_the_host_modes(self) -> None:
        for no_gpu, build_box in ((False, False), (False, True), (True, False)):
            with self.subTest(no_gpu=no_gpu, build_box=build_box):
                rendered_inputs = inputs(no_gpu=no_gpu, build_box=build_box)
                document = yaml.safe_load(
                    manifest_document(
                        render_all(rendered_inputs),
                        rendered_inputs,
                        site_text=EXAMPLE.read_text(),
                        lock_text=(ROOT / "host.lock").read_text(),
                        models_lock_text=(ROOT / "models.lock").read_text(),
                    )
                )
                self.assertEqual(document["inputs"]["no_gpu"], no_gpu)
                self.assertEqual(document["inputs"]["build_box"], build_box)
                self.assertEqual(
                    document["inputs"]["models_lock_sha256"],
                    hashlib.sha256((ROOT / "models.lock").read_bytes()).hexdigest(),
                )
                self.assertEqual(
                    document["inputs"]["hardware_profile"],
                    rendered_inputs.profile.name,
                )

    def test_render_all_is_deterministic_and_indexed(self) -> None:
        first = render_all(inputs())
        second = render_all(inputs())
        self.assertEqual(first, second)
        self.assertEqual(
            set(first.by_path),
            {
                "compose.yaml",
                "prometheus/prometheus.yml",
                "blackbox/blackbox.yml",
                "grafana/ldap.toml",
                "grafana/provisioning/datasources/datasources.yaml",
                "grafana/provisioning/dashboards/provider.yaml",
                "grafana/provisioning/alerting/contact-points.yaml",
                "grafana/provisioning/alerting/policies.yaml",
                "grafana/provisioning/alerting/time-intervals.yaml",
                "grafana/provisioning/alerting/rules.yaml",
                "grafana/dashboards/overview.json",
                "grafana/dashboards/backup.json",
                "grafana/dashboards/gpu.json",
                "postgres/pgbackrest.conf",
                "caddy/Caddyfile",
                "open-webui/env",
                "open-webui/manifest.yaml",
                "searxng/settings.yml",
                "searxng/env",
                "searxng/logging.json",
                "systemd/gideon-users-reconcile.service",
                "systemd/gideon-users-reconcile.timer",
                "systemd/gideon-backup.service",
                "systemd/gideon-backup.timer",
                "systemd/gideon-backup-drill.service",
                "systemd/gideon-backup-drill.timer",
                "systemd/gideon-backup-verify.service",
                "systemd/gideon-backup-verify.timer",
            },
        )
        self.assertEqual(first.by_path["caddy/Caddyfile"].owners, ("caddy",))
        self.assertEqual(first.by_path["prometheus/prometheus.yml"].owners, ("prometheus",))
        self.assertEqual(first.by_path["blackbox/blackbox.yml"].owners, ("blackbox-exporter",))
        self.assertEqual(first.by_path["grafana/ldap.toml"].owners, ("grafana",))
        self.assertEqual(first.by_path["grafana/dashboards/overview.json"].owners, ("grafana",))
        self.assertEqual(
            first.by_path["postgres/pgbackrest.conf"].owners, ("postgres",)
        )
        self.assertEqual(first.by_path["searxng/settings.yml"].owners, ("searxng",))
        self.assertEqual(first.by_path["searxng/env"].owners, ("searxng",))
        self.assertEqual(first.by_path["searxng/logging.json"].owners, ("searxng",))

    def test_no_release_string_enters_a_rendered_body(self) -> None:
        for rendered in render_all(inputs(release="9.9.9-marker")).files:
            self.assertNotIn("9.9.9-marker", rendered.content)

    def test_facts_do_not_leak_into_non_compose_release_files(self) -> None:
        for rendered in render_all(inputs()).files:
            if rendered.relative_path != "compose.yaml":
                self.assertNotIn("GPU-1111", rendered.content)


# --- The render command over an in-memory rendered directory ---------------

RENDERED = "/etc/gideon/rendered"
SITE = "/etc/gideon/site.yaml"
FIXTURES = ROOT / "tests/fixtures/render"
FIXTURE_CASES = (
    ("example", EXAMPLE, False, True),
    ("second-office", SECOND, False, False),
    ("no-gpu", EXAMPLE, True, False),
)


class DirHost(FakeHost):
    """A Host whose filesystem is a dict of path → text; directories are implied."""

    def __init__(self, files: Mapping[str, str], *, euid: int = 0) -> None:
        group = ("getent", "group", "gideon")
        super().__init__(
            commands={
                Facts.SMI: subprocess.CompletedProcess(list(Facts.SMI), 0, NVIDIA_SMI_L, ""),
                group: subprocess.CompletedProcess(list(group), 0, "gideon:x:4242:\n", ""),
            }
        )
        self.files = dict(files)
        self.modes: dict[str, int] = {}
        self.euid = euid
        self.writes: list[str] = []
        self.reads: list[str] = []

    def _is_dir(self, key: str) -> bool:
        return any(name.startswith(key.rstrip("/") + "/") for name in self.files)

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        key = os.fspath(path)
        self.reads.append(key)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644) -> None:
        key = os.fspath(path)
        self.files[key] = text
        self.modes[key] = mode
        self.writes.append(key)

    def exists(self, path: PathLike) -> bool:
        key = os.fspath(path)
        return key in self.files or self._is_dir(key)

    def listdir(self, path: PathLike) -> list[str]:
        key = os.fspath(path).rstrip("/") + "/"
        if not self._is_dir(key):
            raise FileNotFoundError(key)
        return sorted({name[len(key):].split("/", 1)[0] for name in self.files if name.startswith(key)})

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        key = os.fspath(path)
        if key not in self.files and not missing_ok:
            raise FileNotFoundError(key)
        self.files.pop(key, None)

    def stat(self, path: PathLike) -> os.stat_result:
        key = os.fspath(path)
        if key in self.files:
            mode = stat_module.S_IFREG | 0o644
        elif self._is_dir(key):
            mode = stat_module.S_IFDIR | 0o755
        else:
            raise FileNotFoundError(key)
        return os.stat_result((mode, 0, 0, 1, 0, 0, len(self.files.get(key, "")), 0, 0, 0))

    def mkdir(self, path: PathLike, *, mode: int = 0o755, parents: bool = False, exist_ok: bool = False) -> None:
        return None

    def geteuid(self) -> int:
        return self.euid


def checkout_files() -> dict[str, str]:
    return {
        str(ROOT / "host.lock"): (ROOT / "host.lock").read_text(),
        str(ROOT / "images.lock"): (ROOT / "images.lock").read_text(),
        str(ROOT / "models.lock"): (ROOT / "models.lock").read_text(),
        **{
            str(ROOT / "compose" / path): (ROOT / "compose" / path).read_text()
            for path in TEMPLATE_PATHS
        },
        SITE: EXAMPLE.read_text(),
        str(ROOT / "gideon/api/__init__.py"): (ROOT / "gideon/api/__init__.py").read_text(),
        str(ROOT / "gideon/guardrail/__init__.py"): (ROOT / "gideon/guardrail/__init__.py").read_text(),
        "/etc/gideon/secrets/ldap_bind_password": "bind-$-'\"#password",
        "/etc/gideon/secrets/postgres_openwebui_password": "postgres-openwebui-password",
        "/etc/gideon/secrets/gideon_admin_password": "gideon-admin-password",
        "/etc/gideon/secrets/engine_api_key": "engine-api-key",
        "/etc/gideon/secrets/gideon_api_key": "gideon-api-key",
        "/etc/gideon/secrets/searxng_secret_key": "searxng-secret-key",
    }


def render(host: DirHost, *, diff: bool = False) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = run_render(argparse.Namespace(diff=diff), host=host, root=ROOT)
    return code, out.getvalue(), err.getvalue()


class RenderCommand(unittest.TestCase):
    def test_root_is_required(self) -> None:
        code, _, err = render(DirHost(checkout_files(), euid=1000))
        self.assertEqual(code, 1)
        self.assertIn("Fix:", err)

    def test_missing_site_file_refuses_with_fix(self) -> None:
        files = checkout_files()
        del files[SITE]
        code, _, err = render(DirHost(files))
        self.assertEqual(code, 1)
        self.assertIn("Fix:", err)

    def test_unknown_hardware_profile_refuses_without_writing(self) -> None:
        files = checkout_files()
        files[SITE] = files[SITE].replace(
            "# hardware_profile: 2x96v-256d",
            "hardware_profile: 4x48v-512d",
        )
        host = DirHost(files)
        code, out, err = render(host)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(host.writes, [])
        self.assertIn("2x96v-256d", err)
        self.assertIn("hardware_profile", err)
        self.assertIn("Fix:", err)

    def test_search_on_needs_the_searxng_secret_and_search_off_does_not(self) -> None:
        files = checkout_files()
        del files["/etc/gideon/secrets/searxng_secret_key"]
        code, _, err = render(DirHost(files))
        self.assertEqual(code, 1)
        self.assertIn("searxng_secret_key", err)
        self.assertIn("apply", err)
        files[SITE] = files[SITE].replace("# web:", "web:\n  search: off\n#", 1)
        code, out, err = render(DirHost(files))
        self.assertEqual(code, 0, err)
        self.assertNotIn("searxng", out)

    def test_smtp_password_is_required_only_when_smtp_user_is_set(self) -> None:
        files = checkout_files()
        files[SITE] = SECOND.read_text()
        host = DirHost(files)
        code, _, err = render(host)
        self.assertEqual(code, 1)
        self.assertIn("/etc/gideon/secrets/smtp_password", err)
        self.assertIn("alerts.smtp.user", err)

        files["/etc/gideon/secrets/smtp_password"] = "relay-password"
        host = DirHost(files)
        code, _, err = render(host)
        self.assertEqual((code, err), (0, ""))
        self.assertIn(
            "/etc/gideon/secrets/smtp_password",
            host.reads,
        )

        host = DirHost(checkout_files())
        code, _, err = render(host)
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("/etc/gideon/secrets/smtp_password", host.reads)

    def test_first_render_writes_files_and_manifest_then_is_a_no_op(self) -> None:
        host = DirHost(checkout_files())
        code, out, err = render(host)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("compose.yaml: new", out)
        self.assertIn("caddy/Caddyfile: new", out)
        self.assertIn("open-webui/env: new", out)
        self.assertEqual(host.modes[f"{RENDERED}/caddy/Caddyfile"], 0o644)
        self.assertEqual(host.modes[f"{RENDERED}/open-webui/env"], 0o600)
        self.assertIn(f"{RENDERED}/manifest.yaml", host.files)
        self.assertIn('mode: "0600"', host.files[f"{RENDERED}/manifest.yaml"])
        host.writes.clear()
        code, out, _ = render(host)
        self.assertEqual(code, 0)
        self.assertIn("compose.yaml: unchanged", out)
        self.assertIn("caddy/Caddyfile: unchanged", out)
        self.assertEqual(host.writes, [f"{RENDERED}/manifest.yaml"])

    def test_build_box_marker_reaches_render_inputs(self) -> None:
        files = checkout_files()
        files[os.fspath(nogpu.BUILD_BOX_PATH)] = "declared\n"
        host = DirHost(files)
        code, _, err = render(host)
        self.assertEqual((code, err), (0, ""))
        manifest = yaml.safe_load(host.files[f"{RENDERED}/manifest.yaml"])
        self.assertTrue(manifest["inputs"]["build_box"])

    def test_site_edit_marks_changed_files_only(self) -> None:
        host = DirHost(checkout_files())
        render(host)
        host.files[SITE] = host.files[SITE].replace("gideon.example.org", "gideon2.example.org")
        code, out, _ = render(host)
        self.assertEqual(code, 0)
        self.assertIn("caddy/Caddyfile: changed", out)
        self.assertIn("compose.yaml: changed", out)

    def test_stale_file_from_a_previous_manifest_is_removed(self) -> None:
        host = DirHost(checkout_files())
        render(host)
        manifest = host.files[f"{RENDERED}/manifest.yaml"]
        host.files[f"{RENDERED}/old/thing.conf"] = "gone\n"
        host.files[f"{RENDERED}/manifest.yaml"] = manifest.replace(
            "services:\n",
            '  "old/thing.conf":\n'
            '    sha256: "x"\n'
            '    mode: "0644"\n'
            '    owners: []\n'
            "services:\n",
        )
        code, out, _ = render(host)
        self.assertEqual(code, 0)
        self.assertIn("old/thing.conf: stale", out)
        self.assertNotIn(f"{RENDERED}/old/thing.conf", host.files)

    def test_no_gpu_mode_removes_gpu_board_and_leaving_mode_adds_it(self) -> None:
        host = DirHost(checkout_files())
        code, _, err = render(host)
        self.assertEqual((code, err), (0, ""))
        host.calls.clear()
        host.files["/etc/gideon/no-gpu"] = "declared\n"

        code, out, err = render(host)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("grafana/dashboards/gpu.json: stale", out)
        self.assertIn("compose.yaml: changed", out)
        self.assertIn("prometheus/prometheus.yml: changed", out)
        self.assertNotIn(f"{RENDERED}/grafana/dashboards/gpu.json", host.files)
        self.assertNotIn(Facts.SMI, host.calls)
        no_gpu_manifest = yaml.safe_load(host.files[f"{RENDERED}/manifest.yaml"])
        self.assertNotIn("grafana/dashboards/gpu.json", no_gpu_manifest["files"])

        del host.files["/etc/gideon/no-gpu"]
        code, out, err = render(host)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("grafana/dashboards/gpu.json: new", out)
        self.assertIn(f"{RENDERED}/grafana/dashboards/gpu.json", host.files)

    def test_a_registered_artifact_left_behind_is_stale_never_foreign(self) -> None:
        """The GPU board on disk with no manifest naming it (a mode change
        under an older release, a hand-copied file) is removed through the
        stale path; only a file no artifact ever renders is foreign."""

        host = DirHost(checkout_files())
        host.files["/etc/gideon/no-gpu"] = "declared\n"
        host.files[f"{RENDERED}/grafana/dashboards/gpu.json"] = "{}\n"
        code, out, err = render(host)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("grafana/dashboards/gpu.json: stale", out)
        self.assertNotIn(f"{RENDERED}/grafana/dashboards/gpu.json", host.files)

    def test_foreign_file_refuses_even_under_diff(self) -> None:
        host = DirHost(checkout_files())
        render(host)
        host.files[f"{RENDERED}/notes.txt"] = "hand-written\n"
        for diff in (False, True):
            with self.subTest(diff=diff):
                code, _, err = render(host, diff=diff)
                self.assertEqual(code, 1)
                self.assertIn("notes.txt", err)
                self.assertIn("Fix:", err)

    def test_diff_reports_without_writing_and_names_services(self) -> None:
        host = DirHost(checkout_files())
        code, out, err = render(host, diff=True)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("+++ ", out)
        self.assertIn("open-webui/env: changed (secret; contents not shown)", out)
        self.assertIn(
            "Services apply would recreate: " + ", ".join(service_names(inputs())),
            out,
        )
        self.assertEqual(host.writes, [])
        render(host)
        code, out, _ = render(host, diff=True)
        self.assertEqual(code, 0)
        self.assertNotIn("+++ ", out)
        self.assertIn("Summary: 28 unchanged.", out)

    def test_corrupt_applied_manifest_refuses_under_diff(self) -> None:
        host = DirHost(checkout_files())
        render(host)
        host.files[f"{RENDERED}/applied.yaml"] = "files: [not, a, mapping]\n"
        code, _, err = render(host, diff=True)
        self.assertEqual(code, 1)
        self.assertIn("Fix:", err)

    def test_diff_with_a_mapless_applied_manifest_names_every_service(self) -> None:
        host = DirHost(checkout_files())
        self.assertEqual(render(host)[0], 0)
        applied = yaml.safe_load(host.files[f"{RENDERED}/manifest.yaml"])
        del applied["services"]
        del applied["top_level"]
        host.files[f"{RENDERED}/applied.yaml"] = yaml.safe_dump(
            applied, sort_keys=False
        )
        code, out, err = render(host, diff=True)
        self.assertEqual((code, err), (0, ""))
        self.assertIn(
            "Services apply would recreate: " + ", ".join(service_names(inputs())),
            out,
        )

    def test_emitter_failure_is_a_refusal_not_a_traceback(self) -> None:
        files = checkout_files()
        files[SITE] += "\nregistry: http://user:pw@ghcr.io\n"
        code, _, err = render(DirHost(files))
        self.assertEqual(code, 1)
        self.assertIn("registry", err)
        self.assertNotIn("Traceback", err)


class RecreateRule(unittest.TestCase):
    def rendered(self):
        return render_all(inputs())

    def applied(self, **overrides: str) -> AppliedManifest:
        rendered_inputs = inputs()
        rendered_set = render_all(rendered_inputs)
        files: dict[str, Mapping[str, object]] = {}
        for rendered in rendered_set.files:
            digest = overrides.get(rendered.relative_path)
            if digest is None:
                digest = hashlib.sha256(rendered.content.encode()).hexdigest()
            files[rendered.relative_path] = {"sha256": digest, "owners": list(rendered.owners)}
        digests = compose_digests(rendered_inputs)
        return AppliedManifest(files, digests.services, digests.top_level)

    def judgment(
        self,
        applied: AppliedManifest | None,
        current: tuple[str, ...] | None = None,
    ) -> RecreateJudgment:
        rendered_inputs = inputs()
        return recreate_judgment(
            self.rendered(),
            applied,
            service_names(rendered_inputs) if current is None else current,
            compose_digests(rendered_inputs),
        )

    def test_no_applied_manifest_recreates_every_current_service(self) -> None:
        self.assertEqual(
            recreate_services(
                self.rendered(), None, ("caddy", "later"), compose_digests(inputs())
            ),
            ("caddy", "later"),
        )

    def test_equal_hashes_recreate_nothing(self) -> None:
        self.assertEqual(
            recreate_services(
                self.rendered(), self.applied(), ("caddy",), compose_digests(inputs())
            ),
            (),
        )

    def test_changed_owned_file_recreates_its_owner(self) -> None:
        applied = self.applied(**{"caddy/Caddyfile": "0" * 64})
        self.assertEqual(
            recreate_services(
                self.rendered(), applied, ("caddy",), compose_digests(inputs())
            ),
            ("caddy",),
        )

        applied = self.applied(**{"postgres/pgbackrest.conf": "0" * 64})
        self.assertEqual(
            recreate_services(
                self.rendered(), applied, ("postgres",), compose_digests(inputs())
            ),
            ("postgres",),
        )

    def test_changed_compose_block_names_that_service_alone(self) -> None:
        applied = self.applied()
        services = dict(applied.services or {})
        services["grafana"] = "0" * 64
        judgment = self.judgment(replace(applied, services=services))
        self.assertEqual(judgment.services, ("grafana",))
        self.assertEqual(judgment.block, ("grafana",))
        self.assertEqual(judgment.files, ())
        self.assertEqual(judgment.top_level, ())

    def test_changed_owned_file_with_equal_blocks_names_its_owner(self) -> None:
        judgment = self.judgment(self.applied(**{"caddy/Caddyfile": "0" * 64}))
        self.assertEqual(judgment.services, ("caddy",))
        self.assertEqual(judgment.files, ("caddy",))
        self.assertEqual(judgment.block, ())
        self.assertEqual(judgment.top_level, ())

    def test_service_without_a_record_entry_is_named(self) -> None:
        applied = self.applied()
        services = dict(applied.services or {})
        del services["grafana"]
        judgment = self.judgment(replace(applied, services=services))
        self.assertEqual(judgment.services, ("grafana",))
        self.assertEqual(judgment.block, ("grafana",))

    def test_mapless_record_names_every_service_and_judges_only_blocks(self) -> None:
        current = service_names(inputs())
        judgment = self.judgment(replace(self.applied(), services=None, top_level=None))
        self.assertEqual(judgment.services, current)
        self.assertEqual(judgment.block, current)
        self.assertEqual(judgment.files, ())
        self.assertEqual(judgment.top_level, ())

    def test_changed_or_absent_top_level_digest_names_every_service(self) -> None:
        current = service_names(inputs())
        applied = self.applied()
        for top_level in ("0" * 64, None):
            with self.subTest(top_level=top_level is not None):
                judgment = self.judgment(replace(applied, top_level=top_level))
                self.assertEqual(judgment.services, current)
                self.assertEqual(judgment.top_level, current)
                self.assertEqual(judgment.files, ())
                self.assertEqual(judgment.block, ())

    def test_union_preserves_service_order_and_intersects_current_services(self) -> None:
        applied = self.applied(**{"caddy/Caddyfile": "0" * 64})
        services = dict(applied.services or {})
        services["postgres"] = "0" * 64
        judgment = self.judgment(
            replace(applied, services=services),
            current=("postgres", "later", "caddy"),
        )
        self.assertEqual(judgment.files, ("caddy",))
        self.assertEqual(judgment.block, ("postgres",))
        self.assertEqual(judgment.services, ("postgres", "caddy"))

    def test_changed_compose_header_recreates_every_current_service(self) -> None:
        applied = self.applied(**{"compose.yaml": "0" * 64})
        current = service_names(inputs())
        self.assertEqual(
            recreate_services(
                self.rendered(), applied, current, compose_digests(inputs())
            ),
            current,
        )

    def test_removed_file_uses_applied_owners_within_current_services(self) -> None:
        applied = self.applied()
        files = dict(applied.files)
        files["removed/thing.conf"] = {
            "sha256": "0" * 64,
            "owners": ["searxng", "caddy"],
        }
        applied = replace(applied, files=files)
        self.assertEqual(
            recreate_services(
                self.rendered(), applied, ("caddy",), compose_digests(inputs())
            ),
            ("caddy",),
        )
        self.assertEqual(
            recreate_services(
                self.rendered(),
                applied,
                ("caddy", "searxng"),
                compose_digests(inputs()),
            ),
            ("caddy", "searxng"),
        )

    def test_applied_manifest_absent_vs_corrupt(self) -> None:
        host = DirHost({})
        self.assertIsNone(read_applied_manifest(host, Path(f"{RENDERED}/applied.yaml")))
        host.files[f"{RENDERED}/applied.yaml"] = "not: [a manifest"
        with self.assertRaises(ValueError):
            read_applied_manifest(host, Path(f"{RENDERED}/applied.yaml"))

    def test_applied_manifest_rejects_malformed_compose_digest_fields(self) -> None:
        for text in (
            "files: {}\nservices: []\ntop_level: fake\n",
            "files: {}\nservices:\n  caddy: 1\ntop_level: fake\n",
            "files: {}\nservices: {}\ntop_level: []\n",
        ):
            with self.subTest(text=text):
                host = DirHost({f"{RENDERED}/applied.yaml": text})
                with self.assertRaises((TypeError, ValueError)):
                    read_applied_manifest(host, Path(f"{RENDERED}/applied.yaml"))


class ByteStableFixtures(unittest.TestCase):
    """Spec §3.4: both site files render byte-stable against committed fixtures."""

    REGENERATE = "python3 tests/regenerate_render_fixtures.py"

    def test_every_fixture_matches_committed_bytes(self) -> None:
        lock_text = (ROOT / "host.lock").read_text()
        for name, site_path, no_gpu, build_box in FIXTURE_CASES:
            rendered_inputs = inputs(site_path, no_gpu=no_gpu, build_box=build_box)
            rendered = render_all(rendered_inputs)
            expected_dir = FIXTURES / name
            with self.subTest(fixture=name):
                for rendered_file in rendered.files:
                    expected = (expected_dir / rendered_file.relative_path).read_text()
                    self.assertEqual(rendered_file.content, expected, f"{name}/{rendered_file.relative_path} drifted; run {self.REGENERATE}")
                manifest = manifest_document(
                    rendered,
                    rendered_inputs,
                    site_text=site_path.read_text(),
                    lock_text=lock_text,
                    models_lock_text=(ROOT / "models.lock").read_text(),
                )
                self.assertEqual(manifest, (expected_dir / "manifest.yaml").read_text(), f"{name}/manifest.yaml drifted; run {self.REGENERATE}")
