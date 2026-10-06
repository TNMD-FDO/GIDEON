"""The model-server family's pins, rendered blocks, and key refusal."""

import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from test_render import EXAMPLE, ROOT, SECOND, inputs

from gideon.host import weights
from gideon.host.images import parse_registry, reference
from gideon.host.models import GIGABYTE, load_models_lock
from gideon.host.render.compose import ComposeArtifact, service_blocks
from gideon.host.render.engine import (
    EMBED,
    GENERATOR,
    MODEL_SERVERS,
    metrics_target,
    model_server,
)
from gideon.host.render.prometheus import PrometheusConfigArtifact
from gideon.host.render.services import image_pin
from gideon.host.render.services.model_server import model_command


class Family(unittest.TestCase):
    def test_members_are_unique_and_match_the_committed_pins(self) -> None:
        loaded = load_models_lock(ROOT / "models.lock")
        self.assertTrue(loaded.ok, loaded.errors)
        assert loaded.lock is not None
        profile = loaded.lock.profile(loaded.lock.reference)
        assert profile is not None
        gpu = inputs(EXAMPLE)

        self.assertEqual(MODEL_SERVERS[0], GENERATOR)
        for field in ("role", "service_name", "secret_name", "job_name", "words"):
            with self.subTest(field=field):
                values = [getattr(member, field) for member in MODEL_SERVERS]
                self.assertEqual(len(values), len(set(values)))
        for member in MODEL_SERVERS:
            with self.subTest(role=member.role):
                self.assertTrue(member.words)
                self.assertEqual(member.key_file_words, f"{member.words} API key")
                self.assertIs(model_server(member.role), member)
                pin = profile.model(member.role)
                self.assertIsNotNone(pin)
                assert pin is not None
                self.assertEqual(member.service_name, pin.serve.served_name)
                self.assertGreaterEqual(pin.gpu, 0)
                self.assertLess(pin.gpu, len(gpu.facts.gpu_uuids))
        self.assertIsNone(model_server("absent-role"))


class EmbedRender(unittest.TestCase):
    def test_scrape_jobs_follow_the_family_on_gpu_hosts(self) -> None:
        for site in (EXAMPLE, SECOND):
            with self.subTest(site=site.name):
                document = yaml.safe_load(PrometheusConfigArtifact().emit(inputs(site)))
                jobs = document["scrape_configs"]
                dcgm = next(index for index, job in enumerate(jobs) if job["job_name"] == "dcgm")
                members = jobs[dcgm + 1:dcgm + 1 + len(MODEL_SERVERS)]
                self.assertEqual(
                    [job["job_name"] for job in members],
                    [member.job_name for member in MODEL_SERVERS],
                )
                for job, member in zip(members, MODEL_SERVERS, strict=True):
                    self.assertEqual(job["static_configs"], [{"targets": [metrics_target(member)]}])

        no_gpu = yaml.safe_load(PrometheusConfigArtifact().emit(inputs(EXAMPLE, no_gpu=True)))
        names = {job["job_name"] for job in no_gpu["scrape_configs"]}
        self.assertNotIn("dcgm", names)
        for member in MODEL_SERVERS:
            self.assertNotIn(member.job_name, names)

    def test_block_on_each_host_follows_the_pin_and_member(self) -> None:
        no_gpu = service_blocks(inputs(EXAMPLE, no_gpu=True))
        self.assertNotIn(EMBED.service_name, no_gpu)

        for site in (EXAMPLE, SECOND):
            rendered_inputs = inputs(site)
            with self.subTest(site=site.name):
                pin = rendered_inputs.profile.model(EMBED.role)
                row = rendered_inputs.profile.memory_row(EMBED.service_name)
                assert pin is not None and row is not None
                blocks = service_blocks(rendered_inputs)
                block = blocks[EMBED.service_name]
                generator = blocks[GENERATOR.service_name]
                assert isinstance(block, Mapping) and isinstance(generator, Mapping)
                target = parse_registry(rendered_inputs.site.registry)
                assert target is not None

                self.assertEqual(block["image"], generator["image"])
                self.assertEqual(
                    block["image"], reference(target, image_pin(rendered_inputs, "vllm-openai"))
                )
                self.assertEqual(
                    block["devices"],
                    [f"nvidia.com/gpu={rendered_inputs.facts.gpu_uuids[pin.gpu]}"],
                )
                self.assertEqual(
                    block["devices"],
                    [f"nvidia.com/gpu={rendered_inputs.facts.gpu_uuids[1]}"],
                )
                self.assertNotIn("ports", block)
                self.assertEqual(
                    block["volumes"],
                    [f"{weights.MODELS_ROOT}:{pin.serve.env['HF_HOME']}:ro"],
                )
                self.assertEqual(block["secrets"], [EMBED.secret_name])
                environment = block["environment"]
                assert isinstance(environment, Mapping)
                self.assertNotIn("VLLM_API_KEY", environment)
                self.assertNotIn(rendered_inputs.secrets[EMBED.secret_name], str(block))

                command = block["command"]
                assert isinstance(command, list)
                self.assertEqual(command, model_command(pin, EMBED))
                expected_flags: list[str] = []
                for name, value in pin.serve.flags.items():
                    expected_flags.append(f"--{name}")
                    if value is not True:
                        expected_flags.append(str(value))
                self.assertEqual(command[-len(expected_flags):], expected_flags)
                for name in ("hf-overrides", "pooler-config"):
                    with self.subTest(flag=name):
                        value = pin.serve.flags[name]
                        self.assertIsInstance(value, str)
                        self.assertEqual(command[command.index(f"--{name}") + 1], value)
                        self.assertEqual(command.count(value), 1)

                healthcheck = block["healthcheck"]
                assert isinstance(healthcheck, Mapping)
                self.assertEqual(healthcheck["start_period"], f"{EMBED.ready_seconds}s")
                self.assertEqual(block["mem_limit"], row.gb * GIGABYTE)

    def test_yaml_round_trip_preserves_the_json_flag_arguments(self) -> None:
        for site in (EXAMPLE, SECOND):
            rendered_inputs = inputs(site)
            with self.subTest(site=site.name):
                pin = rendered_inputs.profile.model(EMBED.role)
                assert pin is not None
                document = yaml.safe_load(ComposeArtifact().emit(rendered_inputs))
                command = document["services"][EMBED.service_name]["command"]
                self.assertEqual(command, model_command(pin, EMBED))
                for name in ("hf-overrides", "pooler-config"):
                    value = pin.serve.flags[name]
                    self.assertIsInstance(value, str)
                    self.assertEqual(command[command.index(f"--{name}") + 1], value)

    def test_rendered_wrapper_refuses_missing_and_empty_key_files(self) -> None:
        block = service_blocks(inputs(EXAMPLE))[EMBED.service_name]
        assert isinstance(block, Mapping)
        entrypoint = block["entrypoint"]
        assert isinstance(entrypoint, list)
        self.assertEqual(entrypoint[:2], ["sh", "-c"])
        self.assertEqual(entrypoint[3], EMBED.service_name)
        with tempfile.TemporaryDirectory() as directory:
            for condition in ("missing", "empty"):
                with self.subTest(condition=condition):
                    secret = Path(directory) / condition
                    if condition == "empty":
                        secret.write_text("")
                    script = entrypoint[2].replace(
                        f"/run/secrets/{EMBED.secret_name}", str(secret)
                    )
                    result = subprocess.run(
                        ["/bin/sh", "-c", script, entrypoint[3], "-c", "exit 23"],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotEqual(result.returncode, 23)
                    self.assertEqual(len(result.stderr.splitlines()), 1)
                    self.assertIn(
                        "embedding server API key file is missing or empty",
                        result.stderr,
                    )
                    self.assertIn(EMBED.key_file_words, result.stderr)
                    self.assertIn(str(secret), result.stderr)

    def test_profile_without_embed_pin_refuses_with_the_role_and_fix(self) -> None:
        rendered_inputs = inputs(EXAMPLE)
        profile = replace(
            rendered_inputs.profile,
            models=tuple(
                pin for pin in rendered_inputs.profile.models if pin.role != EMBED.role
            ),
        )
        with self.assertRaises(ValueError) as caught:
            service_blocks(replace(rendered_inputs, profile=profile))
        self.assertIn("has no embed model", str(caught.exception))
        self.assertIn("Add the embed model to models.lock", str(caught.exception))
