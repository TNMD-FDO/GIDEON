"""Contracts for the standing CI sibling render."""

import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from gideon.host.images import load_image_lock, parse_registry, reference
from gideon.host.lock import load_host_lock
from gideon.host.models import (
    GIGABYTE,
    HardwareProfile,
    load_models_lock,
    select_profile,
)
from gideon.host.render import ARTIFACTS, RenderInputs
from gideon.host.render.api import API_SERVICE_NAME
from gideon.host.render.ci import (
    CI_PORT,
    CI_POSTGRES_MEMORY_GB,
    CI_PROJECT,
    CI_ROOT,
    CI_SECRET_NAMES,
    CI_SECRETS_DIR,
    CI_WIPE_PATHS,
    RELAY_MEMORY_LIMIT,
    RELAY_SERVICE_NAME,
    RELAY_SOURCE,
    ci_compose_document,
    ci_env_file,
    ci_manifest,
    production_network_name,
)
from gideon.host.render.compose import (
    NETWORK_NAME,
    PROJECT_NAME,
    image_pin,
    memory_limit_bytes,
    service_names,
)
from gideon.host.render.engine import (
    ENGINE_PORT,
    ENGINE_SECRET_NAME,
    ENGINE_SERVICE_NAME,
)
from gideon.host.render.facts import HostFacts
from gideon.host.render.owui import owui_secret_names
from gideon.host.site import load_site

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config/site.example.yaml"
TEMPLATE_PATHS = tuple(
    dict.fromkeys(path for artifact in ARTIFACTS for path in artifact.template_paths)
)


def inputs(**overrides: object) -> RenderInputs:
    site = load_site(EXAMPLE).config
    lock = load_host_lock(ROOT / "host.lock").lock
    images = load_image_lock(ROOT / "images.lock").lock
    models = load_models_lock(ROOT / "models.lock").lock
    assert site is not None and lock is not None and images is not None and models is not None
    profile = select_profile(models, site.hardware_profile)
    assert isinstance(profile, HardwareProfile)
    base = RenderInputs(
        site=site,
        lock=lock,
        images=images,
        facts=HostFacts(("GPU-a", "GPU-b"), service_gid=4242),
        profile=profile,
        templates={name: (ROOT / "compose" / name).read_text() for name in TEMPLATE_PATHS},
        release="fixture",
        secrets={
            "ldap_bind_password": "fixture-ldap-bind",
            "postgres_openwebui_password": "fixture-postgres-openwebui",
            "gideon_admin_password": "fixture-gideon-admin",
            "engine_api_key": "fixture-engine",
            "gideon_api_key": "fixture-api",
        },
        checkout="/opt/gideon",
        api_sources_digest="sha256:" + "0" * 64,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


class Compose(unittest.TestCase):
    def test_each_service_memory_limit_uses_its_release_value(self) -> None:
        render_inputs = inputs()
        services = mapping(ci_compose_document(render_inputs)["services"])
        expected = {
            "postgres": CI_POSTGRES_MEMORY_GB * GIGABYTE,
            "open-webui": memory_limit_bytes(render_inputs.profile, "open-webui"),
            API_SERVICE_NAME: memory_limit_bytes(render_inputs.profile, API_SERVICE_NAME),
            RELAY_SERVICE_NAME: RELAY_MEMORY_LIMIT,
        }
        for name, limit in expected.items():
            with self.subTest(service=name):
                service = mapping(services[name])
                self.assertIsInstance(service["mem_limit"], int)
                self.assertEqual(service["mem_limit"], limit)
                self.assertEqual(tuple(service)[-1], "mem_limit")

    def test_postgres_limit_is_own_and_frontend_tracks_its_profile_row(self) -> None:
        render_inputs = inputs()
        changed_rows = tuple(
            replace(row, gb=row.gb + 1)
            if row.service in {"postgres", "open-webui"}
            else row
            for row in render_inputs.profile.memory
        )
        changed_profile = replace(render_inputs.profile, memory=changed_rows)
        changed_inputs = replace(render_inputs, profile=changed_profile)
        baseline = mapping(ci_compose_document(render_inputs)["services"])
        changed = mapping(ci_compose_document(changed_inputs)["services"])
        baseline_postgres = mapping(baseline["postgres"])
        changed_postgres = mapping(changed["postgres"])
        self.assertEqual(
            baseline_postgres["mem_limit"], CI_POSTGRES_MEMORY_GB * GIGABYTE
        )
        self.assertEqual(changed_postgres["mem_limit"], baseline_postgres["mem_limit"])
        self.assertGreaterEqual(CI_POSTGRES_MEMORY_GB, 1)
        self.assertIsInstance(CI_POSTGRES_MEMORY_GB, int)

        frontend = mapping(changed["open-webui"])
        self.assertEqual(
            frontend["mem_limit"], memory_limit_bytes(changed_profile, "open-webui")
        )
        self.assertNotEqual(
            frontend["mem_limit"], mapping(baseline["open-webui"])["mem_limit"]
        )

    def test_has_the_four_isolated_services_and_expected_networks(self) -> None:
        render_inputs = inputs()
        document = ci_compose_document(render_inputs)
        self.assertEqual(document["name"], CI_PROJECT)
        services = mapping(document["services"])
        self.assertEqual(
            tuple(services),
            ("postgres", "open-webui", API_SERVICE_NAME, RELAY_SERVICE_NAME),
        )
        networks = mapping(document["networks"])
        self.assertEqual(networks[NETWORK_NAME], {})
        self.assertEqual(
            networks["production"],
            {"external": True, "name": production_network_name()},
        )
        self.assertEqual(production_network_name(), f"{PROJECT_NAME}_{NETWORK_NAME}")

    def test_only_the_frontend_publishes_a_loopback_port(self) -> None:
        services = mapping(ci_compose_document(inputs())["services"])
        published: list[tuple[str, object]] = []
        for name, value in services.items():
            service = mapping(value)
            ports = service.get("ports", [])
            assert isinstance(ports, list)
            for port in ports:
                published.append((name, port))
                self.assertTrue(str(port).startswith("127.0.0.1:"))
        self.assertEqual(published, [("open-webui", f"127.0.0.1:{CI_PORT}:8080")])

    def test_bind_mounts_and_secret_files_stay_out_of_production_state(self) -> None:
        render_inputs = inputs()
        document = ci_compose_document(render_inputs)
        services = mapping(document["services"])
        allowed_read_only = {
            f"{render_inputs.checkout}/gideon",
            f"{render_inputs.checkout}/{RELAY_SOURCE}",
        }
        for value in services.values():
            service = mapping(value)
            volumes = service.get("volumes", [])
            assert isinstance(volumes, list)
            for volume in volumes:
                source = str(volume).split(":", 1)[0]
                self.assertNotIn("/etc/gideon/rendered", source)
                self.assertFalse(source == "/data/backup-staging" or source.startswith("/data/backup-staging/"))
                self.assertTrue(
                    source == CI_ROOT
                    or source.startswith(f"{CI_ROOT}/")
                    or source in allowed_read_only
                )
                if source in allowed_read_only:
                    self.assertTrue(str(volume).endswith(":ro"))

        secrets = mapping(document["secrets"])
        for value in secrets.values():
            secret = mapping(value)
            path = str(secret["file"])
            self.assertNotIn("/etc/gideon/rendered", path)
            self.assertFalse(path == "/data/backup-staging" or path.startswith("/data/backup-staging/"))
            self.assertTrue(
                path.startswith(f"{CI_ROOT}/")
                or path == f"/etc/gideon/secrets/{ENGINE_SECRET_NAME}"
            )

    def test_relay_is_the_only_production_network_joiner_and_api_uses_it(self) -> None:
        render_inputs = inputs()
        services = mapping(ci_compose_document(render_inputs)["services"])
        relay = mapping(services[RELAY_SERVICE_NAME])
        self.assertEqual(relay["networks"], [NETWORK_NAME, "production"])
        self.assertEqual(relay["entrypoint"], ["python3", "/relay/relay.py"])
        relay_volume = relay["volumes"]
        assert isinstance(relay_volume, list)
        self.assertEqual(
            relay_volume,
            [f"{render_inputs.checkout}/{RELAY_SOURCE}:/relay/relay.py:ro"],
        )
        healthcheck = mapping(relay["healthcheck"])
        healthcheck_test = healthcheck["test"]
        assert isinstance(healthcheck_test, list)
        self.assertEqual(healthcheck_test[:3], ["CMD", "python3", "-c"])
        self.assertIn("socket.create_connection", healthcheck_test[3])
        self.assertNotIn("curl", str(healthcheck_test).lower())
        production_services = service_names(render_inputs)
        joiners = [
            name
            for name, value in services.items()
            if "production" in list(mapping(value).get("networks", []))  # type: ignore[call-overload]
        ]
        self.assertEqual(joiners, [RELAY_SERVICE_NAME])
        for name in joiners:
            self.assertNotIn(name, production_services)

        api = mapping(services[API_SERVICE_NAME])
        environment = mapping(api["environment"])
        self.assertEqual(
            environment["GIDEON_ENGINE_URL"],
            f"http://{RELAY_SERVICE_NAME}:{ENGINE_PORT}/v1",
        )
        self.assertEqual(
            relay["environment"],
            {
                "RELAY_TARGET": f"{ENGINE_SERVICE_NAME}:{ENGINE_PORT}",
                "RELAY_PORT": str(ENGINE_PORT),
            },
        )
        self.assertEqual(
            api["depends_on"],
            {
                "postgres": {"condition": "service_healthy"},
                RELAY_SERVICE_NAME: {"condition": "service_started"},
            },
        )

    def test_every_service_image_is_a_loaded_lock_reference(self) -> None:
        render_inputs = inputs()
        target = parse_registry(render_inputs.site.registry)
        assert target is not None
        services = mapping(ci_compose_document(render_inputs)["services"])
        self.assertEqual(
            mapping(services["postgres"])["image"],
            reference(target, image_pin(render_inputs, "postgres")),
        )
        self.assertEqual(
            mapping(services["open-webui"])["image"],
            reference(target, image_pin(render_inputs, "open-webui")),
        )
        for name in (API_SERVICE_NAME, RELAY_SERVICE_NAME):
            self.assertEqual(
                mapping(services[name])["image"],
                reference(target, image_pin(render_inputs, "gideon")),
            )

    def test_the_frontend_has_no_search_or_directory_settings(self) -> None:
        services = mapping(ci_compose_document(inputs())["services"])
        frontend = mapping(services["open-webui"])
        environment = mapping(frontend["environment"])
        self.assertEqual(environment["ENABLE_WEB_SEARCH"], "false")
        self.assertEqual(environment["ENABLE_LDAP"], "false")
        for name in environment:
            if name in {"ENABLE_WEB_SEARCH", "ENABLE_LDAP"}:
                continue
            self.assertFalse(name.startswith("WEB_SEARCH_"))
            self.assertFalse(name.startswith("WEB_LOADER_"))
            self.assertFalse(name.startswith("LDAP_"))
            self.assertFalse(name.startswith("ENABLE_LDAP_"))
        self.assertEqual(frontend["secrets"], ["webui_secret_key"])
        self.assertEqual(frontend["env_file"], [{"path": "./open-webui/env", "format": "raw"}])
        self.assertEqual(frontend["volumes"], [f"{CI_ROOT}/openwebui:/app/backend/data"])

    def test_ci_env_and_manifest_omit_search_and_secret_directory_settings(self) -> None:
        render_inputs = inputs()
        env_text = ci_env_file(render_inputs)
        self.assertNotIn("LDAP_APP_PASSWORD=", env_text)
        self.assertNotIn("ENABLE_WEB_SEARCH=", env_text)
        self.assertIn("OPENAI_API_KEYS=fixture-api\n", env_text)

        manifest_text = ci_manifest(render_inputs)
        self.assertNotIn("ENABLE_WEB_SEARCH", manifest_text)
        self.assertNotIn("ldap_bind_password", manifest_text)
        manifest = yaml.safe_load(manifest_text)
        self.assertIsInstance(manifest, Mapping)
        assert isinstance(manifest, Mapping)
        self.assertIn("groups", manifest)
        self.assertIn("models", manifest)

    def test_secret_names_are_the_env_files_reads_with_the_directory_off(self) -> None:
        self.assertEqual(CI_SECRET_NAMES, owui_secret_names(inputs(), directory=False))

    def test_wipe_paths_are_all_inside_the_ci_root(self) -> None:
        self.assertTrue(CI_SECRETS_DIR.startswith(f"{CI_ROOT}/"))
        self.assertTrue(all(path.startswith(f"{CI_ROOT}/") for path in CI_WIPE_PATHS))
        self.assertEqual(
            {path.removeprefix(f"{CI_ROOT}/") for path in CI_WIPE_PATHS},
            {
                "postgres",
                "openwebui",
                "open-webui",
                "compose.yaml",
                "secrets/gideon_admin_api_key",
                "secrets/gideon_eval_api_key",
            },
        )
