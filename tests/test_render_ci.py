"""Contracts for the standing CI sibling render."""

import hashlib
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import yaml  # type: ignore[import-untyped]
from test_render import render_templates
from test_secrets import FakeHost

from gideon.host.cas import ROOT as CAS_ROOT
from gideon.host.egress import load_egress_allowlist
from gideon.host.images import load_image_lock, parse_registry, reference
from gideon.host.lock import load_host_lock
from gideon.host.models import (
    GIGABYTE,
    HardwareProfile,
    load_models_lock,
    select_profile,
)
from gideon.host.render import RenderInputs, worker
from gideon.host.render.api import (
    API_INSTRUCTION_MOUNT,
    API_INSTRUCTION_PATH,
    API_MOUNT_TARGET,
    API_SERVICE_NAME,
)
from gideon.host.render.ci import (
    CI_BASE_URL,
    CI_CORPUS_SECRET_NAME,
    CI_INSTRUCTION_DIGEST_LABEL,
    CI_OPENSEARCH_DATA_ROOT,
    CI_OPENSEARCH_MEMORY_GB,
    CI_OPENSEARCH_RENDERED_DIR,
    CI_PORT,
    CI_POSTGRES_MEMORY_GB,
    CI_PROJECT,
    CI_QDRANT_DATA_ROOT,
    CI_QDRANT_MEMORY_GB,
    CI_ROOT,
    CI_SECRET_NAMES,
    CI_SECRETS_DIR,
    CI_SKIPPED_SECRETS,
    CI_WIPE_PATHS,
    CI_WORK_ROOT,
    EMBED_RELAY_SERVICE_NAME,
    RECORD_RELAY_SERVICE_NAME,
    RELAY_MEMORY_LIMIT,
    RELAY_SERVICE_NAME,
    RELAY_SOURCE,
    ci_compose_document,
    ci_env_file,
    ci_instruction,
    ci_manifest,
)
from gideon.host.render.compose import (
    INTERNAL_NETWORK_NAME,
    NETWORK_NAME,
    SWAP_CEILING_BYTES,
    container_name,
    integration_network_name,
    memory_limit_bytes,
    mounted_secret_names,
    network_name,
    service_names,
)
from gideon.host.render.engine import (
    EMBED,
    EMBED_SECRET_NAME,
    ENGINE_PORT,
    ENGINE_SECRET_NAME,
    ENGINE_SERVICE_NAME,
    INTEGRATION_NETWORK_NAME,
)
from gideon.host.render.facts import HostFacts
from gideon.host.render.opensearch import (
    OPENSEARCH_CERT_SECRET_NAME,
    OPENSEARCH_DATA_MOUNT,
    OPENSEARCH_KEY_SECRET_NAME,
    OPENSEARCH_METRICS_SECRET_NAME,
    OPENSEARCH_PASSWORD_SECRET_NAME,
    OPENSEARCH_SECURITY_PATH,
    OPENSEARCH_SERVICE_NAME,
    OPENSEARCH_SETTINGS_PATH,
    OPENSEARCH_SNAPSHOTS_ROOT,
    opensearch_heap_mib,
)
from gideon.host.render.owui import (
    GENERAL_MODEL_ID,
    owui_environment,
    owui_secret_names,
)
from gideon.host.render.qdrant import (
    QDRANT_SERVICE_NAME,
    QDRANT_SNAPSHOTS_ROOT,
    QDRANT_STORAGE_MOUNT,
)
from gideon.host.render.services import declared_sources, image_pin
from gideon.host.render.services.open_webui import OpenWebuiService
from gideon.host.render.services.opensearch import OpensearchService
from gideon.host.render.services.postgres import (
    POSTGRES_SHM_SIZE_BYTES,
    PostgresService,
)
from gideon.host.render.services.qdrant import QdrantService
from gideon.host.render.services.worker import WorkerService
from gideon.host.render.worker import METRICS_SECRET_NAME, WORKER_SERVICE_NAME
from gideon.host.secrets import current_directory, ensure_generated, select_directory
from gideon.host.site import load_site

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config/site.example.yaml"


def inputs(**overrides: object) -> RenderInputs:
    egress = load_egress_allowlist(ROOT / "config/egress.yaml").allowlist
    site = load_site(EXAMPLE).config
    lock = load_host_lock(ROOT / "host.lock").lock
    images = load_image_lock(ROOT / "images.lock").lock
    models = load_models_lock(ROOT / "models.lock").lock
    assert site is not None and lock is not None and images is not None and models is not None and egress is not None
    profile = select_profile(models, site.hardware_profile)
    assert isinstance(profile, HardwareProfile)
    base = RenderInputs(
        site=site,
        lock=lock,
        images=images,
        facts=HostFacts(("GPU-a", "GPU-b"), service_gid=4242),
        profile=profile,
        templates=render_templates(),
        release="fixture",
        secrets={
            "ldap_bind_password": "fixture-ldap-bind",
            "postgres_openwebui_password": "fixture-postgres-openwebui",
            "gideon_admin_password": "fixture-gideon-admin",
            "engine_api_key": "fixture-engine",
            "embed_api_key": "fixture-embed",
            "gideon_api_key": "fixture-api",
            "qdrant_read_only_api_key": "fixture-qdrant-read-only",
        },
        checkout="/opt/gideon",
        source_digests=dict.fromkeys(declared_sources(), "sha256:" + "0" * 64),
        egress=egress,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


class RelaySocket:
    """Record the health snippet's connections and refuse a selected endpoint."""

    def __init__(self, failure: tuple[str, int] | None) -> None:
        self.failure = failure
        self.connected: list[tuple[str, int]] = []
        self.closed: list[tuple[str, int]] = []
        self.timeouts: list[int] = []

    def create_connection(self, address: tuple[str, int], *, timeout: int) -> SimpleNamespace:
        self.connected.append(address)
        self.timeouts.append(timeout)
        if address == self.failure:
            raise OSError("fictitious unreachable relay endpoint")
        return SimpleNamespace(close=lambda: self.closed.append(address))


class Compose(unittest.TestCase):
    def test_each_service_memory_limit_uses_its_release_value(self) -> None:
        render_inputs = inputs()
        services = mapping(ci_compose_document(render_inputs)["services"])
        expected = {
            "postgres": CI_POSTGRES_MEMORY_GB * GIGABYTE,
            "open-webui": memory_limit_bytes(render_inputs.profile, "open-webui"),
            QDRANT_SERVICE_NAME: CI_QDRANT_MEMORY_GB * GIGABYTE,
            OPENSEARCH_SERVICE_NAME: CI_OPENSEARCH_MEMORY_GB * GIGABYTE,
            API_SERVICE_NAME: memory_limit_bytes(render_inputs.profile, API_SERVICE_NAME),
            WORKER_SERVICE_NAME: memory_limit_bytes(render_inputs.profile, WORKER_SERVICE_NAME),
            RELAY_SERVICE_NAME: RELAY_MEMORY_LIMIT,
            RECORD_RELAY_SERVICE_NAME: RELAY_MEMORY_LIMIT,
            EMBED_RELAY_SERVICE_NAME: RELAY_MEMORY_LIMIT,
        }
        for name, limit in expected.items():
            with self.subTest(service=name):
                service = mapping(services[name])
                self.assertIsInstance(service["mem_limit"], int)
                self.assertEqual(service["mem_limit"], limit)
                if name in {QDRANT_SERVICE_NAME, OPENSEARCH_SERVICE_NAME}:
                    self.assertEqual(
                        service["memswap_limit"], limit + SWAP_CEILING_BYTES
                    )
                    self.assertEqual(tuple(service)[-2:], ("mem_limit", "memswap_limit"))
                else:
                    self.assertNotIn("memswap_limit", service)
                    self.assertEqual(tuple(service)[-1], "mem_limit")

    def test_postgres_limit_is_own_and_frontend_tracks_its_profile_row(self) -> None:
        render_inputs = inputs()
        changed_rows = tuple(
            replace(row, gb=row.gb + 1)
            if row.service in {"postgres", "open-webui", WORKER_SERVICE_NAME}
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
        worker_block = mapping(changed[WORKER_SERVICE_NAME])
        self.assertEqual(worker_block["mem_limit"], memory_limit_bytes(changed_profile, WORKER_SERVICE_NAME))
        self.assertNotEqual(worker_block["mem_limit"], mapping(baseline[WORKER_SERVICE_NAME])["mem_limit"])

    def test_service_blocks_differ_from_definitions_only_at_sibling_keys(self) -> None:
        render_inputs = inputs()
        target = parse_registry(render_inputs.site.registry)
        assert target is not None
        services = mapping(ci_compose_document(render_inputs)["services"])
        cases = (
            (PostgresService(), {"volumes", "command", "networks"}),
            (OpenWebuiService(), {"env_file", "environment", "volumes", "ports"}),
            (QdrantService(), {"volumes", "networks"}),
            (OpensearchService(), {"volumes", "environment", "networks"}),
        )
        for definition, differences in cases:
            with self.subTest(service=definition.name):
                sibling = mapping(services[definition.name])
                production = definition.block(render_inputs, target)
                excluded = differences | {"mem_limit", "memswap_limit"}
                self.assertEqual(
                    {key: value for key, value in sibling.items() if key not in excluded},
                    {key: value for key, value in production.items() if key not in excluded},
                )

        postgres = mapping(services[PostgresService.name])
        self.assertEqual(postgres["volumes"], [f"{CI_ROOT}/postgres:/var/lib/postgresql"])
        self.assertEqual(postgres["networks"], [NETWORK_NAME])
        self.assertNotIn("command", postgres)
        self.assertEqual(postgres["shm_size"], POSTGRES_SHM_SIZE_BYTES)

        frontend = mapping(services[OpenWebuiService.name])
        definition_env_file = OpenWebuiService().block(render_inputs, target)["env_file"]
        assert isinstance(definition_env_file, list)
        self.assertEqual(
            frontend["env_file"],
            [{**entry, "path": "./open-webui/env"} for entry in definition_env_file],
        )
        expected_environment = {
            **owui_environment(render_inputs, search=False, directory=False),
            "WEBUI_URL": CI_BASE_URL,
        }
        self.assertEqual(frontend["environment"], expected_environment)
        environment = mapping(frontend["environment"])
        self.assertEqual(environment["VECTOR_DB"], QDRANT_SERVICE_NAME)
        self.assertEqual(urlsplit(str(environment["QDRANT_URI"])).hostname, QDRANT_SERVICE_NAME)
        self.assertEqual(frontend["volumes"], [f"{CI_ROOT}/openwebui:/app/backend/data"])
        self.assertEqual(frontend["ports"], [f"127.0.0.1:{CI_PORT}:8080"])

        qdrant = mapping(services[QDRANT_SERVICE_NAME])
        self.assertEqual(qdrant["networks"], [NETWORK_NAME])
        self.assertEqual(qdrant["volumes"], [f"{CI_QDRANT_DATA_ROOT}:{QDRANT_STORAGE_MOUNT}"])
        self.assertNotIn(QDRANT_SNAPSHOTS_ROOT, str(qdrant["volumes"]))

        opensearch = mapping(services[OPENSEARCH_SERVICE_NAME])
        self.assertEqual(opensearch["networks"], [NETWORK_NAME])
        self.assertEqual(
            opensearch["volumes"],
            [
                f"{CI_OPENSEARCH_DATA_ROOT}:{OPENSEARCH_DATA_MOUNT}",
                f"{CI_OPENSEARCH_RENDERED_DIR}/opensearch.yml:{OPENSEARCH_SETTINGS_PATH}:ro",
                f"{CI_OPENSEARCH_RENDERED_DIR}/security:{OPENSEARCH_SECURITY_PATH}:ro",
            ],
        )
        self.assertNotIn(OPENSEARCH_SNAPSHOTS_ROOT, str(opensearch["volumes"]))
        self.assertEqual(
            opensearch["secrets"],
            [
                OPENSEARCH_PASSWORD_SECRET_NAME,
                OPENSEARCH_METRICS_SECRET_NAME,
                OPENSEARCH_KEY_SECRET_NAME,
                OPENSEARCH_CERT_SECRET_NAME,
            ],
        )
        production_environment = mapping(
            OpensearchService().block(render_inputs, target)["environment"]
        )
        sibling_environment = mapping(opensearch["environment"])
        self.assertEqual(
            {key: value for key, value in sibling_environment.items() if key != "OPENSEARCH_JAVA_OPTS"},
            {key: value for key, value in production_environment.items() if key != "OPENSEARCH_JAVA_OPTS"},
        )
        heap = opensearch_heap_mib(CI_OPENSEARCH_MEMORY_GB * GIGABYTE)
        self.assertEqual(
            sibling_environment["OPENSEARCH_JAVA_OPTS"],
            f"-Xms{heap}m -Xmx{heap}m -XX:-HeapDumpOnOutOfMemoryError",
        )

    def test_has_the_nine_isolated_services_and_expected_networks(self) -> None:
        render_inputs = inputs()
        document = ci_compose_document(render_inputs)
        self.assertEqual(document["name"], CI_PROJECT)
        services = mapping(document["services"])
        self.assertEqual(
            tuple(services),
            (
                "postgres",
                "open-webui",
                QDRANT_SERVICE_NAME,
                OPENSEARCH_SERVICE_NAME,
                API_SERVICE_NAME,
                WORKER_SERVICE_NAME,
                RELAY_SERVICE_NAME,
                RECORD_RELAY_SERVICE_NAME,
                EMBED_RELAY_SERVICE_NAME,
            ),
        )
        networks = mapping(document["networks"])
        self.assertEqual(tuple(networks), (NETWORK_NAME, INTEGRATION_NETWORK_NAME, INTERNAL_NETWORK_NAME))
        self.assertNotIn("production", networks)
        self.assertEqual(networks[NETWORK_NAME], {})
        self.assertEqual(
            networks[INTEGRATION_NETWORK_NAME],
            {"external": True, "name": integration_network_name()},
        )
        self.assertEqual(integration_network_name(), "gideon_integration")
        self.assertEqual(
            networks[INTERNAL_NETWORK_NAME],
            {"external": True, "name": network_name(INTERNAL_NETWORK_NAME)},
        )

    def test_worker_inherits_its_definition_except_the_sibling_differences(self) -> None:
        render_inputs = inputs()
        target = parse_registry(render_inputs.site.registry)
        assert target is not None
        production = dict(WorkerService().block(render_inputs, target))
        sibling = mapping(mapping(ci_compose_document(render_inputs)["services"])[WORKER_SERVICE_NAME])
        changed = {"volumes", "environment", "secrets", "networks", "mem_limit", "memswap_limit"}
        self.assertEqual(
            {key: value for key, value in sibling.items() if key not in changed},
            {key: value for key, value in production.items() if key not in changed},
        )
        volumes = production["volumes"]
        assert isinstance(volumes, list)
        self.assertEqual(sibling["volumes"], [
            *(volume for volume in volumes if volume.startswith(f"{render_inputs.checkout}/")),
            f"{CAS_ROOT}:{CAS_ROOT}:ro",
            f"{CI_WORK_ROOT}:{worker.WORK_ROOT}",
        ])
        original_environment = mapping(production["environment"])
        self.assertEqual(sibling["environment"], {
            **{key: value for key, value in original_environment.items()
               if key not in {"HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"}},
            worker.CORPUS_DATABASE_HOST_ENV: RECORD_RELAY_SERVICE_NAME,
            worker.CORPUS_DATABASE_PORT_ENV: str(worker.WORKER_DATABASE_PORT),
            worker.CORPUS_DATABASE_NAME_ENV: worker.WORKER_DATABASE_NAME,
            worker.CORPUS_DATABASE_ROLE_ENV: worker.METRICS_ROLE,
            worker.CORPUS_PASSWORD_FILE_ENV: f"/run/secrets/{CI_CORPUS_SECRET_NAME}",
            worker.EMBED_URL_ENV: f"http://{EMBED_RELAY_SERVICE_NAME}:{EMBED.port}",
        })
        secrets = production["secrets"]
        assert isinstance(secrets, list)
        self.assertEqual(sibling["secrets"], [*secrets, CI_CORPUS_SECRET_NAME])
        self.assertIn(EMBED_SECRET_NAME, secrets)
        self.assertEqual(sibling["networks"], [NETWORK_NAME])
        self.assertEqual("memswap_limit" in sibling, not WorkerService.swap)

    def test_worker_keeps_checkout_mounts_supplied_by_its_definition(self) -> None:
        render_inputs = inputs()
        target = parse_registry(render_inputs.site.registry)
        assert target is not None
        definition = dict(WorkerService().block(render_inputs, target))
        mounts = [
            f"{render_inputs.checkout}/fictitious-package:/fictitious-package:ro",
            f"{render_inputs.checkout}/fictitious-extra:/fictitious-extra:ro",
        ]
        definition["volumes"] = [
            *mounts, f"{worker.SNAPSHOTS_ROOT}:{worker.SNAPSHOTS_ROOT}",
            f"{worker.WORK_ROOT}:{worker.WORK_ROOT}", f"{CAS_ROOT}:{CAS_ROOT}",
        ]
        definition["fictitious-inherited-key"] = "fictitious-value"
        with (
            patch.object(WorkerService, "block", return_value=definition),
            patch.object(WorkerService, "swap", False),
        ):
            block = mapping(mapping(ci_compose_document(render_inputs)["services"])[WORKER_SERVICE_NAME])
        self.assertEqual(block["volumes"], [
            *mounts, f"{CAS_ROOT}:{CAS_ROOT}:ro", f"{CI_WORK_ROOT}:{worker.WORK_ROOT}",
        ])
        self.assertEqual(block["fictitious-inherited-key"], "fictitious-value")
        self.assertEqual(block["memswap_limit"],
                         memory_limit_bytes(render_inputs.profile, WORKER_SERVICE_NAME) + SWAP_CEILING_BYTES)

    def test_relays_use_their_production_targets_and_only_the_required_networks(self) -> None:
        render_inputs = inputs()
        document = ci_compose_document(render_inputs)
        services = mapping(document["services"])
        production_services = service_names(render_inputs)
        for name, host, port, network in (
            (RELAY_SERVICE_NAME, ENGINE_SERVICE_NAME, ENGINE_PORT, INTEGRATION_NETWORK_NAME),
            (RECORD_RELAY_SERVICE_NAME, container_name(PostgresService.name), worker.WORKER_DATABASE_PORT, INTERNAL_NETWORK_NAME),
            (EMBED_RELAY_SERVICE_NAME, container_name(EMBED.service_name), EMBED.port, INTERNAL_NETWORK_NAME),
        ):
            with self.subTest(relay=name):
                relay = mapping(services[name])
                self.assertEqual(relay["environment"], {"RELAY_TARGET": f"{host}:{port}", "RELAY_PORT": str(port)})
                self.assertEqual(relay["networks"], [NETWORK_NAME, network])
                self.assertEqual(relay["volumes"], [f"{render_inputs.checkout}/{RELAY_SOURCE}:/relay/relay.py:ro"])
                self.assertNotIn("ports", relay)
                self.assertNotIn("secrets", relay)
                self.assertNotIn(name, production_services)
        internal_joiners = []
        for name, block in services.items():
            networks = mapping(block)["networks"]
            assert isinstance(networks, list)
            if INTERNAL_NETWORK_NAME in networks:
                internal_joiners.append(name)
        self.assertEqual(internal_joiners, [RECORD_RELAY_SERVICE_NAME, EMBED_RELAY_SERVICE_NAME])

    def test_relay_health_requires_both_its_listener_and_target(self) -> None:
        services = mapping(ci_compose_document(inputs())["services"])
        for name in (RELAY_SERVICE_NAME, RECORD_RELAY_SERVICE_NAME, EMBED_RELAY_SERVICE_NAME):
            relay = mapping(services[name])
            environment = {
                **mapping(relay["environment"]),
                "RELAY_TARGET": "fictitious-health-target.example.test:18333",
            }
            target_host, target_port = str(environment["RELAY_TARGET"]).rsplit(":", 1)
            endpoints = [("127.0.0.1", int(str(environment["RELAY_PORT"]))), (target_host, int(target_port))]
            healthcheck = mapping(relay["healthcheck"])
            command = healthcheck["test"]
            assert isinstance(command, list) and isinstance(command[3], str)
            self.assertEqual(command[:3], ["CMD", "python3", "-c"])
            for failure in (None, *endpoints):
                with self.subTest(relay=name, failure=failure):
                    sockets = RelaySocket(failure)
                    with patch.dict("sys.modules", {
                        "os": SimpleNamespace(environ=dict(environment)),
                        "socket": sockets,
                    }):
                        if failure is None:
                            exec(command[3], {})
                        else:
                            with self.assertRaises(OSError):
                                exec(command[3], {})
                    attempted = endpoints if failure != endpoints[0] else endpoints[:1]
                    self.assertEqual(sockets.connected, attempted)
                    self.assertEqual(sockets.closed, [endpoint for endpoint in attempted if endpoint != failure])
                    self.assertTrue(all(timeout > 0 for timeout in sockets.timeouts))
                    self.assertLessEqual(sum(sockets.timeouts), int(str(healthcheck["timeout"]).removesuffix("s")))

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
        api = mapping(services[API_SERVICE_NAME])
        self.assertEqual(
            api["volumes"],
            [
                f"{render_inputs.checkout}/gideon:{API_MOUNT_TARGET}:ro",
                f"{CI_ROOT}/{API_INSTRUCTION_PATH}:{API_INSTRUCTION_MOUNT}:ro",
            ],
        )
        self.assertEqual(
            mapping(api["environment"])["GIDEON_INSTRUCTION_FILE"],
            API_INSTRUCTION_MOUNT,
        )
        allowed_read_only = {
            str(CAS_ROOT),
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
                or path in {
                    f"/etc/gideon/secrets/{name}"
                    for name in (ENGINE_SECRET_NAME, EMBED_SECRET_NAME, METRICS_SECRET_NAME)
                }
            )

    def test_relay_is_the_only_integration_network_joiner_and_api_uses_it(self) -> None:
        render_inputs = inputs()
        services = mapping(ci_compose_document(render_inputs)["services"])
        relay = mapping(services[RELAY_SERVICE_NAME])
        self.assertEqual(relay["networks"], [NETWORK_NAME, INTEGRATION_NETWORK_NAME])
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
            if INTEGRATION_NETWORK_NAME in list(mapping(value).get("networks", []))  # type: ignore[call-overload]
        ]
        self.assertEqual(joiners, [RELAY_SERVICE_NAME])
        for name in joiners:
            self.assertNotIn(name, production_services)

        api = mapping(services[API_SERVICE_NAME])
        environment = mapping(api["environment"])
        generator = render_inputs.profile.model("generator")
        assert generator is not None
        self.assertEqual(environment["GIDEON_MODEL_ID"], GENERAL_MODEL_ID)
        self.assertEqual(environment["GIDEON_ENGINE_MODEL"], generator.serve.served_name)
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
        for name in (QDRANT_SERVICE_NAME, OPENSEARCH_SERVICE_NAME):
            self.assertEqual(
                mapping(services[name])["image"],
                reference(target, image_pin(render_inputs, name)),
            )
        for name in (API_SERVICE_NAME, WORKER_SERVICE_NAME, RELAY_SERVICE_NAME, RECORD_RELAY_SERVICE_NAME, EMBED_RELAY_SERVICE_NAME):
            self.assertEqual(
                mapping(services[name])["image"],
                reference(target, image_pin(render_inputs, "gideon")),
            )

    def test_the_frontend_uses_its_store_without_search_or_directory_settings(self) -> None:
        services = mapping(ci_compose_document(inputs())["services"])
        frontend = mapping(services["open-webui"])
        environment = mapping(frontend["environment"])
        self.assertEqual(environment["ENABLE_WEB_SEARCH"], "false")
        self.assertEqual(environment["ENABLE_LDAP"], "false")
        for name in (
            "VECTOR_DB",
            "QDRANT_URI",
            "QDRANT_COLLECTION_PREFIX",
            "ENABLE_QDRANT_MULTITENANCY_MODE",
            "QDRANT_PREFER_GRPC",
        ):
            self.assertIn(name, environment)
        for name in environment:
            if name in {"ENABLE_WEB_SEARCH", "ENABLE_LDAP"}:
                continue
            self.assertFalse(name.startswith("WEB_SEARCH_"))
            self.assertFalse(name.startswith("WEB_LOADER_"))
            self.assertFalse(name.startswith("LDAP_"))
            self.assertFalse(name.startswith("ENABLE_LDAP_"))

    def test_ci_env_has_store_key_without_search_or_directory_settings(self) -> None:
        render_inputs = inputs()
        env_text = ci_env_file(render_inputs)
        self.assertNotIn("LDAP_APP_PASSWORD=", env_text)
        self.assertNotIn("ENABLE_WEB_SEARCH=", env_text)
        self.assertIn("QDRANT_API_KEY=fixture-qdrant-read-only\n", env_text)
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

    def test_top_level_secrets_follow_the_blocks_mounted_names(self) -> None:
        document = ci_compose_document(inputs())
        services = mapping(document["services"])
        names = mounted_secret_names(services)
        shared = {
            ENGINE_SECRET_NAME: ENGINE_SECRET_NAME,
            EMBED_SECRET_NAME: EMBED_SECRET_NAME,
            CI_CORPUS_SECRET_NAME: METRICS_SECRET_NAME,
        }
        self.assertEqual(
            document["secrets"],
            {
                name: {
                    "file": (
                        f"/etc/gideon/secrets/{shared[name]}"
                        if name in shared
                        else f"{CI_SECRETS_DIR}/{name}"
                    )
                }
                for name in names
            },
        )
        self.assertIn(ENGINE_SECRET_NAME, names)

    def test_sibling_mints_its_own_metrics_password(self) -> None:
        original = current_directory()
        try:
            select_directory(Path(CI_SECRETS_DIR))
            host = FakeHost(dirs={CI_SECRETS_DIR})
            result = ensure_generated(host, skip=CI_SKIPPED_SECRETS)
            self.assertTrue(result.ok, result.problem)
            self.assertIn(OPENSEARCH_METRICS_SECRET_NAME, result.created)
            self.assertIn(f"{CI_SECRETS_DIR}/{OPENSEARCH_METRICS_SECRET_NAME}", host.files)
            for shared in (ENGINE_SECRET_NAME, EMBED_SECRET_NAME):
                self.assertIn(shared, CI_SKIPPED_SECRETS)
                self.assertNotIn(shared, result.created)
                self.assertNotIn(f"{CI_SECRETS_DIR}/{shared}", host.files)
            self.assertNotIn(CI_CORPUS_SECRET_NAME, result.created)
        finally:
            select_directory(original)

    def test_wipe_paths_are_all_inside_the_ci_root(self) -> None:
        self.assertTrue(CI_SECRETS_DIR.startswith(f"{CI_ROOT}/"))
        self.assertTrue(all(path.startswith(f"{CI_ROOT}/") for path in CI_WIPE_PATHS))
        self.assertEqual(
            {path.removeprefix(f"{CI_ROOT}/") for path in CI_WIPE_PATHS},
            {
                "postgres",
                "openwebui",
                CI_WORK_ROOT.removeprefix(f"{CI_ROOT}/"),
                CI_QDRANT_DATA_ROOT.removeprefix(f"{CI_ROOT}/"),
                CI_OPENSEARCH_DATA_ROOT.removeprefix(f"{CI_ROOT}/"),
                CI_OPENSEARCH_RENDERED_DIR.removeprefix(f"{CI_ROOT}/"),
                "open-webui",
                API_SERVICE_NAME,
                "compose.yaml",
                "secrets/gideon_admin_api_key",
                "secrets/gideon_eval_api_key",
            },
        )

    def test_office_name_moves_only_the_api_instruction_digest_label(self) -> None:
        original = inputs()
        moved_office = replace(original.site.office, name="Fictitious Moved Office")
        moved = replace(original, site=replace(original.site, office=moved_office))
        before = ci_compose_document(original)
        after = ci_compose_document(moved)
        self.assertNotEqual(ci_instruction(original), ci_instruction(moved))
        self.assertEqual(before.keys(), after.keys())
        self.assertEqual(before["networks"], after["networks"])
        self.assertEqual(before["secrets"], after["secrets"])
        before_services = mapping(before["services"])
        after_services = mapping(after["services"])
        self.assertEqual(before_services.keys(), after_services.keys())
        for name in before_services:
            if name != API_SERVICE_NAME:
                self.assertEqual(before_services[name], after_services[name])
        before_api = mapping(before_services[API_SERVICE_NAME])
        after_api = mapping(after_services[API_SERVICE_NAME])
        self.assertEqual(
            {key: value for key, value in before_api.items() if key != "labels"},
            {key: value for key, value in after_api.items() if key != "labels"},
        )
        before_labels = mapping(before_api["labels"])
        after_labels = mapping(after_api["labels"])
        self.assertEqual(
            before_labels["org.gideon.api-sources-digest"],
            after_labels["org.gideon.api-sources-digest"],
        )
        self.assertEqual(
            after_labels[CI_INSTRUCTION_DIGEST_LABEL],
            hashlib.sha256(ci_instruction(moved).encode("utf-8")).hexdigest(),
        )
        self.assertNotEqual(
            before_labels[CI_INSTRUCTION_DIGEST_LABEL],
            after_labels[CI_INSTRUCTION_DIGEST_LABEL],
        )
