"""The API service, probes, alert, and service-source digest contracts."""

import hashlib
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml  # type: ignore[import-untyped]
from test_render import DirHost, checkout_files, inputs

from gideon.host.images import parse_registry
from gideon.host.render import RenderInputs, render_all
from gideon.host.render.api import (
    API_CHAT_HEADER,
    API_HEALTH_PATH,
    API_INSTRUCTION_MOUNT,
    API_INSTRUCTION_PATH,
    API_JOB_NAME,
    API_MOUNT_TARGET,
    API_SECRET_NAME,
    API_SERVICE_NAME,
    API_SOURCE_HEADER,
    API_USER_EMAIL_HEADER,
)
from gideon.host.render.command import (
    AppliedManifest,
    compose_digests,
    gather_sources_digest,
    recreate_judgment,
)
from gideon.host.render.compose import engine_service, service_names
from gideon.host.render.engine import ENGINE_SECRET_NAME, ENGINE_SERVICE_NAME
from gideon.host.render.owui import EVAL_IDENTITY, GENERAL_MODEL_ID
from gideon.host.render.services.api import api_service
from gideon.host.sysio import PathLike

ROOT = Path(__file__).resolve().parent.parent
OTHER_DIGEST = "sha256:" + "1" * 64


def applied_record(rendered_inputs: RenderInputs) -> AppliedManifest:
    rendered = render_all(rendered_inputs)
    files = {
        item.relative_path: {
            "sha256": hashlib.sha256(item.content.encode()).hexdigest(),
            "owners": list(item.owners),
        }
        for item in rendered.files
    }
    digests = compose_digests(rendered_inputs)
    return AppliedManifest(files, digests.services, digests.top_level)


class ApiRender(unittest.TestCase):
    def test_service_secret_probe_and_rule_follow_the_gpu_marker(self) -> None:
        gpu = inputs()
        gpu_rendered = render_all(gpu)
        gpu_document = yaml.safe_load(gpu_rendered.by_path["compose.yaml"].content)
        gpu_services = gpu_document["services"]
        api = gpu_services[API_SERVICE_NAME]
        self.assertIn(API_SERVICE_NAME, service_names(gpu))
        self.assertIn(API_SECRET_NAME, gpu_document["secrets"])
        self.assertEqual(
            api["secrets"],
            [ENGINE_SECRET_NAME, API_SECRET_NAME, "postgres_gideon_audit_password"],
        )
        self.assertEqual(api["environment"]["GIDEON_SOURCE_HEADER"], API_SOURCE_HEADER)
        self.assertEqual(api["environment"]["GIDEON_CHAT_HEADER"], API_CHAT_HEADER)
        self.assertEqual(api["environment"]["GIDEON_EVAL_IDENTITY"], EVAL_IDENTITY.email)
        self.assertEqual(api["environment"]["GIDEON_INSTRUCTION_FILE"], API_INSTRUCTION_MOUNT)
        generator = gpu.profile.model("generator")
        assert generator is not None
        self.assertEqual(api["environment"]["GIDEON_MODEL_ID"], GENERAL_MODEL_ID)
        self.assertEqual(
            api["environment"]["GIDEON_ENGINE_MODEL"], generator.serve.served_name
        )
        self.assertEqual(API_SOURCE_HEADER, API_USER_EMAIL_HEADER)
        self.assertNotIn("ports", api)
        self.assertNotIn("depends_on", api)
        self.assertEqual(
            api["volumes"],
            [
                f"{gpu.checkout}/gideon:{API_MOUNT_TARGET}:ro",
                f"/etc/gideon/rendered/{API_INSTRUCTION_PATH}:{API_INSTRUCTION_MOUNT}:ro",
            ],
        )
        self.assertEqual(
            api["labels"],
            {"org.gideon.api-sources-digest": gpu.source_digests[API_SERVICE_NAME]},
        )

        gpu_prometheus = gpu_rendered.by_path["prometheus/prometheus.yml"].content
        self.assertIn(f"job_name: {API_JOB_NAME}", gpu_prometheus)
        self.assertIn(API_HEALTH_PATH, gpu_prometheus)
        gpu_rules = yaml.safe_load(
            gpu_rendered.by_path["grafana/provisioning/alerting/rules.yaml"].content
        )
        gpu_rule = {
            rule["uid"]: rule
            for group in gpu_rules["groups"]
            for rule in group["rules"]
        }["gideon-api-probe-failing"]
        self.assertIn('probe_success{job="api"}', gpu_rule["data"][0]["model"]["expr"])
        self.assertIn("gideon-api", gpu_rule["annotations"]["summary"])
        self.assertNotIn("General", gpu_rule["annotations"]["summary"])

        no_gpu = inputs(no_gpu=True)
        no_gpu_rendered = render_all(no_gpu)
        no_gpu_document = yaml.safe_load(
            no_gpu_rendered.by_path["compose.yaml"].content
        )
        self.assertNotIn(API_SERVICE_NAME, no_gpu_document["services"])
        self.assertNotIn(API_INSTRUCTION_PATH, no_gpu_rendered.by_path)
        self.assertNotIn(API_SECRET_NAME, no_gpu_document["secrets"])
        self.assertNotIn("postgres_gideon_audit_password", no_gpu_document["secrets"])
        self.assertNotIn(
            f"job_name: {API_JOB_NAME}",
            no_gpu_rendered.by_path["prometheus/prometheus.yml"].content,
        )
        no_gpu_rules = yaml.safe_load(
            no_gpu_rendered.by_path["grafana/provisioning/alerting/rules.yaml"].content
        )
        self.assertNotIn(
            "gideon-api-probe-failing",
            {
                rule["uid"]
                for group in no_gpu_rules["groups"]
                for rule in group["rules"]
            },
        )

    def test_empty_checkout_and_digest_refuse(self) -> None:
        base = inputs()
        registry = parse_registry(base.site.registry)
        assert registry is not None
        with self.assertRaisesRegex(ValueError, "Re-run render with a checkout\\."):
            api_service(replace(base, checkout=""), registry)
        with self.assertRaisesRegex(
            ValueError, "Re-run render from a release checkout, whose loader gathers it\\."
        ):
            api_service(replace(base, source_digests={}), registry)

    def test_instruction_mount_accepts_a_rendered_root(self) -> None:
        base = inputs()
        registry = parse_registry(base.site.registry)
        assert registry is not None
        block = api_service(base, registry, rendered_root="/tmp/fixture-rendered")
        self.assertEqual(
            block["volumes"],
            [
                f"{base.checkout}/gideon:{API_MOUNT_TARGET}:ro",
                f"/tmp/fixture-rendered/{API_INSTRUCTION_PATH}:{API_INSTRUCTION_MOUNT}:ro",
            ],
        )

    def test_moving_office_name_recreates_only_api_for_changed_instruction(self) -> None:
        original = inputs()
        moved_office = replace(original.site.office, name="Fictitious Moved Office")
        moved = replace(original, site=replace(original.site, office=moved_office))
        original_rendered = render_all(original)
        moved_rendered = render_all(moved)
        self.assertNotEqual(
            original_rendered.by_path[API_INSTRUCTION_PATH].content,
            moved_rendered.by_path[API_INSTRUCTION_PATH].content,
        )
        original_compose = yaml.safe_load(original_rendered.by_path["compose.yaml"].content)
        moved_compose = yaml.safe_load(moved_rendered.by_path["compose.yaml"].content)
        self.assertEqual(original_compose, moved_compose)
        judgment = recreate_judgment(
            moved_rendered,
            applied_record(original),
            service_names(moved),
            compose_digests(moved),
        )
        self.assertEqual(judgment.services, (API_SERVICE_NAME,))
        self.assertEqual(judgment.files, (API_SERVICE_NAME,))
        self.assertEqual(judgment.block, ())
        self.assertEqual(judgment.top_level, ())

    def test_moving_the_digest_recreates_only_the_api_service_block(self) -> None:
        original = inputs()
        moved = replace(original, source_digests={API_SERVICE_NAME: OTHER_DIGEST})
        judgment = recreate_judgment(
            render_all(moved),
            applied_record(original),
            service_names(moved),
            compose_digests(moved),
        )
        self.assertEqual(judgment.services, (API_SERVICE_NAME,))
        self.assertEqual(judgment.block, (API_SERVICE_NAME,))
        self.assertEqual(judgment.files, ())
        self.assertEqual(judgment.top_level, ())

    def test_moving_the_api_settings_recreates_only_the_api_service_block(self) -> None:
        original = inputs()
        applied = applied_record(original)
        moved_identity = replace(EVAL_IDENTITY, email="moved@gideon.invalid")
        with (
            patch("gideon.host.render.services.api.API_SOURCE_HEADER", "X-Moved-Source"),
            patch("gideon.host.render.services.api.API_CHAT_HEADER", "X-Moved-Chat"),
            patch("gideon.host.render.services.api.EVAL_IDENTITY", moved_identity),
        ):
            moved = render_all(original)
            judgment = recreate_judgment(
                moved,
                applied,
                service_names(original),
                compose_digests(original),
            )
        self.assertEqual(judgment.services, (API_SERVICE_NAME,))
        self.assertEqual(judgment.block, (API_SERVICE_NAME,))
        self.assertEqual(judgment.files, ())
        self.assertEqual(judgment.top_level, ())

    def test_moving_served_name_moves_api_and_generator_blocks(self) -> None:
        original = inputs()
        generator = original.profile.model("generator")
        assert generator is not None
        moved_name = generator.serve.served_name + "-fixture"
        moved_generator = replace(
            generator, serve=replace(generator.serve, served_name=moved_name)
        )
        moved_profile = replace(
            original.profile,
            models=tuple(
                moved_generator if model.role == "generator" else model
                for model in original.profile.models
            ),
        )
        moved = replace(original, profile=moved_profile)
        target = parse_registry(original.site.registry)
        assert target is not None

        before_api = api_service(original, target)
        after_api = api_service(moved, target)
        before_environment = before_api["environment"]
        after_environment = after_api["environment"]
        assert isinstance(before_environment, dict) and isinstance(after_environment, dict)
        self.assertEqual(before_environment["GIDEON_ENGINE_MODEL"], generator.serve.served_name)
        self.assertEqual(after_environment["GIDEON_ENGINE_MODEL"], moved_name)
        self.assertEqual(
            {key: value for key, value in before_environment.items() if key != "GIDEON_ENGINE_MODEL"},
            {key: value for key, value in after_environment.items() if key != "GIDEON_ENGINE_MODEL"},
        )
        self.assertEqual(
            {key: value for key, value in before_api.items() if key != "environment"},
            {key: value for key, value in after_api.items() if key != "environment"},
        )

        before_engine = engine_service(original, target)
        after_engine = engine_service(moved, target)
        before_command = before_engine["command"]
        after_command = after_engine["command"]
        assert isinstance(before_command, list) and isinstance(after_command, list)
        name_position = before_command.index("--served-model-name") + 1
        self.assertEqual(after_command[name_position], moved_name)
        self.assertEqual(
            before_command[:name_position] + before_command[name_position + 1:],
            after_command[:name_position] + after_command[name_position + 1:],
        )
        self.assertEqual(
            {key: value for key, value in before_engine.items() if key != "command"},
            {key: value for key, value in after_engine.items() if key != "command"},
        )
        before_digests = compose_digests(original).services
        after_digests = compose_digests(moved).services
        for service in (API_SERVICE_NAME, ENGINE_SERVICE_NAME):
            with self.subTest(service=service):
                self.assertNotEqual(before_digests[service], after_digests[service])
        for service in set(before_digests) - {API_SERVICE_NAME, ENGINE_SERVICE_NAME}:
            with self.subTest(unchanged_service=service):
                self.assertEqual(before_digests[service], after_digests[service])
        before_rendered = render_all(original)
        after_rendered = render_all(moved)
        before_compose = yaml.safe_load(before_rendered.by_path["compose.yaml"].content)
        after_compose = yaml.safe_load(after_rendered.by_path["compose.yaml"].content)
        self.assertEqual(before_compose["services"]["open-webui"], after_compose["services"]["open-webui"])
        frontend_environment = after_compose["services"]["open-webui"]["environment"]
        self.assertNotIn(generator.serve.served_name, str(frontend_environment))
        self.assertNotIn(moved_name, str(frontend_environment))
        self.assertEqual(
            before_rendered.by_path["open-webui/manifest.yaml"].content,
            after_rendered.by_path["open-webui/manifest.yaml"].content,
        )
        judgment = recreate_judgment(
            after_rendered, applied_record(original), service_names(moved),
            compose_digests(moved),
        )
        self.assertEqual(set(judgment.block), {API_SERVICE_NAME, ENGINE_SERVICE_NAME})
        self.assertEqual(set(judgment.services), {API_SERVICE_NAME, ENGINE_SERVICE_NAME})


class SourceDigest(unittest.TestCase):
    def test_collect_is_order_independent_and_ignores_caches_and_outside_files(self) -> None:
        files = {
            str(ROOT / "service" / "a.py"): "a",
            str(ROOT / "service" / "nested" / "b.py"): "b",
            str(ROOT / "service" / "__pycache__" / "cached.py"): "cached",
            str(ROOT / "service" / "ignored.pyc"): "ignored",
            str(ROOT / "outside.py"): "outside",
        }

        class ShuffledHost(DirHost):
            def listdir(self, path: PathLike) -> list[str]:
                return list(reversed(super().listdir(path)))

        digest = gather_sources_digest(DirHost(files), ROOT, ("service",))
        shuffled = gather_sources_digest(ShuffledHost(files), ROOT, ("service",))
        self.assertEqual(digest, shuffled)
        # A cache entry and a file outside the declared source are not the
        # container's code: neither moves the digest, so neither recreates it.
        blind = dict(files)
        blind[str(ROOT / "service" / "__pycache__" / "cached.py")] = "recompiled"
        blind[str(ROOT / "outside.py")] = "edited"
        self.assertEqual(digest, gather_sources_digest(DirHost(blind), ROOT, ("service",)))
        moved = dict(files)
        moved[str(ROOT / "service" / "nested" / "b.py")] = "changed"
        self.assertNotEqual(
            digest,
            gather_sources_digest(DirHost(moved), ROOT, ("service",)),
        )

    def test_missing_declared_source_names_the_path(self) -> None:
        with self.assertRaisesRegex(ValueError, r"missing.*service-missing"):
            gather_sources_digest(
                DirHost(checkout_files()), ROOT, ("service-missing",)
            )


if __name__ == "__main__":
    unittest.main()
