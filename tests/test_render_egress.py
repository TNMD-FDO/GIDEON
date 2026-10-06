"""The egress service's identity, rendered block, and recreate boundary."""

import hashlib
import unittest
from collections.abc import Mapping
from dataclasses import replace
from urllib.parse import unquote, urlsplit

from test_render import EXAMPLE, ROOT, SECOND, DirHost, declared_source_files, inputs

from gideon.egress import settings
from gideon.host.images import parse_registry, reference
from gideon.host.models import GIGABYTE
from gideon.host.render import RenderInputs, compose, render_all
from gideon.host.render.api import (
    API_IMAGE_NAME,
    API_MOUNT_TARGET,
    API_WORKING_DIRECTORY,
)
from gideon.host.render.command import (
    AppliedManifest,
    compose_digests,
    gather_source_digests,
    recreate_judgment,
)
from gideon.host.render.compose import NETWORK_NAME, service_blocks, service_names
from gideon.host.render.egress import (
    EGRESS_ENV_FILE,
    EGRESS_GROUP,
    EGRESS_PORT,
    EGRESS_SERVICE_NAME,
    EGRESS_SOURCES_DIGEST_LABEL,
    HOSTS_ENV,
    INTERNAL_NETWORK_NAME,
    PARENT_PROXY_ENV,
    PORT_ENV,
    EgressEnvArtifact,
)
from gideon.host.render.services import SERVICES, image_pin
from gideon.host.render.services.egress import EgressService
from gideon.host.render.worker import WORKER_SERVICE_NAME


def _applied(rendered_inputs: RenderInputs) -> AppliedManifest:
    rendered = render_all(rendered_inputs)
    digests = compose_digests(rendered_inputs)
    return AppliedManifest(
        files={
            rendered_file.relative_path: {
                "sha256": hashlib.sha256(rendered_file.content.encode()).hexdigest(),
                "owners": list(rendered_file.owners),
            }
            for rendered_file in rendered.files
        },
        services=digests.services,
        top_level=digests.top_level,
        top_level_parts=digests.top_level_parts,
    )


class Identity(unittest.TestCase):
    def test_environment_names_match_the_runtime_and_network_names_match(self) -> None:
        self.assertEqual(
            (HOSTS_ENV, PORT_ENV, PARENT_PROXY_ENV),
            (settings.HOSTS_ENV, settings.PORT_ENV, settings.PARENT_PROXY_ENV),
        )
        self.assertEqual(INTERNAL_NETWORK_NAME, compose.INTERNAL_NETWORK_NAME)

    def test_definition_follows_worker_with_one_mounted_source(self) -> None:
        names = tuple(definition.name for definition in SERVICES)
        self.assertEqual(names.index(EGRESS_SERVICE_NAME), names.index(WORKER_SERVICE_NAME) + 1)
        definition = SERVICES[names.index(EGRESS_SERVICE_NAME)]
        self.assertIsInstance(definition, EgressService)
        self.assertEqual(definition.sources, ("gideon/egress",))
        self.assertFalse(definition.store)


class ServiceBlock(unittest.TestCase):
    def test_block_on_each_host_kind(self) -> None:
        for site_path, no_gpu in ((EXAMPLE, False), (SECOND, False), (EXAMPLE, True)):
            rendered_inputs = inputs(site_path, no_gpu=no_gpu)
            with self.subTest(site=site_path.name, no_gpu=no_gpu):
                self.assertTrue(EgressService().applies(rendered_inputs))
                self.assertIn(EGRESS_SERVICE_NAME, service_names(rendered_inputs))
                target = parse_registry(rendered_inputs.site.registry)
                assert target is not None
                allowlist = rendered_inputs.egress
                assert allowlist is not None
                group = allowlist.group(EGRESS_GROUP)
                assert group is not None
                row = rendered_inputs.profile.memory_row(EGRESS_SERVICE_NAME)
                assert row is not None
                block = service_blocks(rendered_inputs)[EGRESS_SERVICE_NAME]
                assert isinstance(block, Mapping)
                expected: dict[str, object] = {
                    "image": reference(target, image_pin(rendered_inputs, API_IMAGE_NAME)),
                    "restart": "unless-stopped",
                    "command": ["python", "-m", "gideon.egress"],
                    "working_dir": API_WORKING_DIRECTORY,
                    "volumes": [f"{rendered_inputs.checkout}/gideon:{API_MOUNT_TARGET}:ro"],
                    "read_only": True,
                    "environment": {
                        HOSTS_ENV: ",".join(host.host for host in group.hosts),
                        PORT_ENV: str(EGRESS_PORT),
                        "TZ": rendered_inputs.site.office.timezone,
                    },
                    "labels": {
                        EGRESS_SOURCES_DIGEST_LABEL: rendered_inputs.source_digests[EGRESS_SERVICE_NAME]
                    },
                    "healthcheck": {
                        "test": [
                            "CMD",
                            "python",
                            "-c",
                            "import urllib.request; urllib.request.urlopen("
                            f"'http://127.0.0.1:{EGRESS_PORT}/healthz', timeout=4).read()",
                        ],
                        "interval": "30s",
                        "timeout": "5s",
                        "retries": 3,
                        "start_period": "30s",
                    },
                    "networks": [NETWORK_NAME, INTERNAL_NETWORK_NAME],
                    "mem_limit": row.gb * GIGABYTE,
                }
                if rendered_inputs.site.egress_proxy:
                    expected["env_file"] = [dict(entry) for entry in EGRESS_ENV_FILE]
                self.assertEqual(block, expected)
                self.assertNotIn("ports", block)
                self.assertNotIn("secrets", block)
                if not rendered_inputs.site.egress_proxy:
                    self.assertNotIn("env_file", block)

    def test_parent_env_is_one_secret_line_for_the_second_office(self) -> None:
        rendered_inputs = inputs(SECOND)
        block = service_blocks(rendered_inputs)[EGRESS_SERVICE_NAME]
        assert isinstance(block, Mapping)
        self.assertEqual(block["env_file"], [dict(entry) for entry in EGRESS_ENV_FILE])
        artifact = EgressEnvArtifact()
        self.assertTrue(artifact.applies(rendered_inputs))
        self.assertEqual(artifact.secret_names(rendered_inputs), ("proxy_auth",))
        self.assertEqual(artifact.owners, (EGRESS_SERVICE_NAME,))
        self.assertEqual(artifact.mode, 0o600)
        self.assertTrue(artifact.secret)
        lines = artifact.emit(rendered_inputs).splitlines()
        self.assertEqual(len(lines), 1)
        variable, separator, value = lines[0].partition("=")
        self.assertEqual((variable, separator), (PARENT_PROXY_ENV, "="))
        parsed = urlsplit(value)
        parent = urlsplit(rendered_inputs.site.egress_proxy)
        user, separator, password = rendered_inputs.secrets["proxy_auth"].partition(":")
        self.assertEqual(separator, ":")
        self.assertEqual((parsed.scheme, parsed.hostname, parsed.port), (parent.scheme, parent.hostname, parent.port))
        self.assertEqual((unquote(parsed.username or ""), unquote(parsed.password or "")), (user, password))
        self.assertFalse(EgressEnvArtifact().applies(inputs()))
        self.assertEqual(EgressEnvArtifact().secret_names(inputs()), ())

    def test_absent_allowlist_refuses_with_the_loader_named(self) -> None:
        with self.assertRaises(ValueError) as refusal:
            service_blocks(inputs(egress=None))
        self.assertIn("loader", str(refusal.exception))
        self.assertIn("config/egress.yaml", str(refusal.exception))


class RecreateBoundary(unittest.TestCase):
    def test_moving_a_corpus_host_recreates_only_egress(self) -> None:
        previous_inputs = inputs()
        allowlist = previous_inputs.egress
        assert allowlist is not None
        groups = tuple(
            replace(group, hosts=(replace(group.hosts[0], host="corpus.example"), *group.hosts[1:]))
            if group.name == EGRESS_GROUP
            else group
            for group in allowlist.groups
        )
        current_inputs = replace(previous_inputs, egress=replace(allowlist, groups=groups))
        judgment = recreate_judgment(
            render_all(current_inputs),
            _applied(previous_inputs),
            service_names(current_inputs),
            compose_digests(current_inputs),
        )
        self.assertEqual(judgment.services, (EGRESS_SERVICE_NAME,))
        self.assertEqual(judgment.reasons(), (("changed compose block", (EGRESS_SERVICE_NAME,)),))

    def test_egress_source_change_recreates_only_egress(self) -> None:
        files = declared_source_files()
        previous_digests = gather_source_digests(DirHost(files), ROOT)
        previous_inputs = replace(inputs(), source_digests=previous_digests)
        changed_files = dict(files)
        changed_files[str(ROOT / "gideon/egress/__init__.py")] += "\n# fixture change\n"
        current_digests = gather_source_digests(DirHost(changed_files), ROOT)
        current_inputs = replace(previous_inputs, source_digests=current_digests)
        judgment = recreate_judgment(
            render_all(current_inputs),
            _applied(previous_inputs),
            service_names(current_inputs),
            compose_digests(current_inputs),
        )
        self.assertEqual(judgment.services, (EGRESS_SERVICE_NAME,))
        self.assertEqual(judgment.reasons(), (("changed compose block", (EGRESS_SERVICE_NAME,)),))
