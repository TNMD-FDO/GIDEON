"""The model-server family's pins, rendered blocks, and key refusal."""

import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from test_render import EXAMPLE, FIXTURE_CASES, ROOT, SECOND, inputs, trial_inputs

from gideon.host import gpus, weights
from gideon.host.images import parse_registry, reference
from gideon.host.models import GIGABYTE, load_models_lock
from gideon.host.render import render_all
from gideon.host.render.api import (
    API_RERANK_KEY_FILE_ENV,
    API_RERANK_URL_ENV,
    API_SERVICE_NAME,
)
from gideon.host.render.compose import ComposeArtifact, service_blocks
from gideon.host.render.egress import INTERNAL_NETWORK_NAME
from gideon.host.render.engine import (
    EMBED,
    GENERATOR,
    INTEGRATION_NETWORK_NAME,
    MODEL_SERVERS,
    RERANK,
    metrics_target,
    model_server,
)
from gideon.host.render.facts import HostFacts
from gideon.host.render.prometheus import DCGM_JOB_NAME, PrometheusConfigArtifact
from gideon.host.render.routeguard import (
    ROUTE_GUARD_MOUNT_DIR,
    route_guard_import_path,
    route_guard_mount,
)
from gideon.host.render.scoretemplate import SCORE_TEMPLATE_MOUNT, SCORE_TEMPLATE_PATH
from gideon.host.render.services import image_pin
from gideon.host.render.services.model_server import (
    ENGINE_ACCESS_LOG_EXCLUDED_PATHS,
    ModelServerService,
    has_reranker,
    model_command,
    model_flag_arguments,
    model_server_members,
    model_service,
)
from gideon.host.render.yamlout import dump_fragment
from gideon.turn.rerank import RERANK_SERVED_NAME


class Family(unittest.TestCase):
    def test_projected_flags_extend_the_model_command_in_lock_order(self) -> None:
        pin = inputs().profile.model(RERANK.role)
        assert pin is not None
        flags: dict[str, str | float | bool] = {
            "gpu-memory-utilization": 0.375,
            "enable-fixture-mode": True,
            "hf-overrides": '{"fixture": "value with spaces"}',
        }
        projected = model_flag_arguments(flags)
        self.assertEqual(projected, [
            ("--gpu-memory-utilization", str(flags["gpu-memory-utilization"])),
            ("--enable-fixture-mode",),
            ("--hf-overrides", flags["hf-overrides"]),
        ])
        baseline = replace(pin, serve=replace(pin.serve, flags={}))
        configured = replace(pin, serve=replace(pin.serve, flags=flags))
        self.assertEqual(
            model_command(configured, RERANK),
            model_command(baseline, RERANK) + [token for group in projected for token in group],
        )

    def test_members_are_unique_and_match_the_committed_pins(self) -> None:
        loaded = load_models_lock(ROOT / "models.lock")
        self.assertTrue(loaded.ok, loaded.errors)
        assert loaded.lock is not None
        profile = loaded.lock.profile(loaded.lock.reference)
        assert profile is not None
        gpu = inputs(EXAMPLE)

        self.assertEqual(MODEL_SERVERS[0], GENERATOR)
        self.assertTrue(GENERATOR.joins_integration_network)
        self.assertFalse(EMBED.joins_integration_network)
        self.assertFalse(GENERATOR.joins_internal_network)
        self.assertTrue(EMBED.joins_internal_network)
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


class RerankRender(unittest.TestCase):
    def test_the_family_has_three_members_in_serving_order(self) -> None:
        self.assertEqual(MODEL_SERVERS, (GENERATOR, EMBED, RERANK))

    def test_client_served_name_matches_every_profile_with_a_reranker(self) -> None:
        loaded = load_models_lock(ROOT / "models.lock")
        self.assertTrue(loaded.ok, loaded.errors)
        assert loaded.lock is not None
        served_names = {
            profile.name: pin.serve.served_name
            for profile in loaded.lock.profiles if (pin := profile.model(RERANK.role)) is not None
        }
        self.assertTrue(served_names)
        for profile, served_name in served_names.items():
            with self.subTest(profile=profile):
                self.assertEqual(RERANK_SERVED_NAME, served_name)

    def test_reranker_membership_matches_the_model_server_family(self) -> None:
        for base in (inputs(), inputs(no_gpu=True), trial_inputs()):
            with self.subTest(profile=base.profile.name, no_gpu=base.no_gpu):
                renders = has_reranker(base.profile, no_gpu=base.no_gpu)
                self.assertEqual(renders, RERANK in model_server_members(base))
                self.assertEqual(renders, not base.no_gpu and base.profile.model(RERANK.role) is not None)

    def test_reranker_block_follows_the_reference_pin_on_gpu_hosts(self) -> None:
        for host, site, no_gpu, build_box in FIXTURE_CASES:
            with self.subTest(host=host):
                base = inputs(site, no_gpu=no_gpu, build_box=build_box)
                compose = yaml.safe_load(ComposeArtifact().emit(base))
                services = compose["services"]
                if no_gpu:
                    self.assertNotIn(RERANK.service_name, services)
                    self.assertNotIn(RERANK.secret_name, compose["secrets"])
                    continue

                pin = base.profile.model(RERANK.role)
                assert pin is not None
                target = parse_registry(base.site.registry)
                assert target is not None
                vllm = image_pin(base, "vllm-openai")
                block = services[RERANK.service_name]
                self.assertEqual(block["image"], reference(target, vllm))
                device = f"nvidia.com/gpu={base.facts.gpu_uuids[1]}"
                self.assertEqual(block["devices"], [device])
                self.assertNotIn("ports", block)
                self.assertEqual(block["networks"], ["gideon"])
                self.assertEqual(
                    block["volumes"],
                    [
                        f"{weights.MODELS_ROOT}:{pin.serve.env['HF_HOME']}:ro",
                        route_guard_mount(),
                        SCORE_TEMPLATE_MOUNT,
                    ],
                )
                self.assertEqual(block["secrets"], [RERANK.secret_name])
                self.assertEqual(block["healthcheck"]["start_period"], f"{RERANK.ready_seconds}s")
                command = block["command"]
                self.assertEqual(
                    command[:5],
                    [pin.repo, "--revision", pin.revision, "--served-model-name", pin.serve.served_name],
                )
                self.assertEqual(command[command.index("--port") + 1], str(RERANK.port))
                flags: list[str] = []
                for name, value in pin.serve.flags.items():
                    flags.append(f"--{name}")
                    if value is not True:
                        flags.append(str(value))
                self.assertEqual(command[-len(flags):], flags)
                overrides = pin.serve.flags["hf-overrides"]
                self.assertEqual(command.count("--hf-overrides"), 1)
                self.assertEqual(command[command.index("--hf-overrides") + 1], overrides)
                target_path = block["volumes"][-1].split(":")[1]
                self.assertEqual(command[command.index("--chat-template") + 1], target_path)

    def test_trial_renders_the_required_servers_without_reranker_resources(self) -> None:
        rendered = render_all(trial_inputs())
        compose = yaml.safe_load(rendered.by_path["compose.yaml"].content)
        services = compose["services"]
        for member in (GENERATOR, EMBED):
            self.assertIn(member.service_name, services)
        self.assertNotIn(RERANK.service_name, services)
        self.assertNotIn(RERANK.secret_name, compose["secrets"])
        self.assertNotIn(SCORE_TEMPLATE_PATH, rendered.by_path)
        environment = services[API_SERVICE_NAME]["environment"]
        self.assertNotIn(API_RERANK_URL_ENV, environment)
        self.assertNotIn(API_RERANK_KEY_FILE_ENV, environment)
        jobs = yaml.safe_load(rendered.by_path["prometheus/prometheus.yml"].content)["scrape_configs"]
        self.assertNotIn(RERANK.job_name, [job["job_name"] for job in jobs])

    def test_optional_reranker_leaves_the_required_blocks_byte_for_byte(self) -> None:
        for site in (EXAMPLE, SECOND):
            with self.subTest(site=site.name):
                base = inputs(site)
                profile = base.profile
                without_reranker = replace(
                    profile,
                    models=tuple(pin for pin in profile.models if pin.role != RERANK.role),
                    memory=tuple(row for row in profile.memory if row.service != RERANK.service_name),
                )
                with_blocks = service_blocks(base)
                without_blocks = service_blocks(replace(base, profile=without_reranker))
                self.assertNotIn(RERANK.service_name, without_blocks)
                for member in (GENERATOR, EMBED):
                    before = without_blocks[member.service_name]
                    after = with_blocks[member.service_name]
                    assert isinstance(before, Mapping) and isinstance(after, Mapping)
                    self.assertEqual(dump_fragment(after).encode(), dump_fragment(before).encode())

    def test_scrapes_follow_rendered_model_server_order(self) -> None:
        for base in (inputs(), inputs(SECOND), inputs(no_gpu=True), trial_inputs()):
            with self.subTest(profile=base.profile.name, no_gpu=base.no_gpu, site=base.site.hostname):
                rendered = render_all(base)
                services = yaml.safe_load(rendered.by_path["compose.yaml"].content)["services"]
                jobs = yaml.safe_load(rendered.by_path["prometheus/prometheus.yml"].content)["scrape_configs"]
                members = {member.service_name: member for member in MODEL_SERVERS}
                expected = [
                    {"job_name": member.job_name, "static_configs": [{"targets": [metrics_target(member)]}]}
                    for name in services if (member := members.get(name)) is not None
                ]
                model_jobs = {member.job_name for member in MODEL_SERVERS}
                self.assertEqual([job for job in jobs if job["job_name"] in model_jobs], expected)
                names = [job["job_name"] for job in jobs]
                if base.no_gpu:
                    self.assertNotIn(DCGM_JOB_NAME, names)
                else:
                    start = names.index(DCGM_JOB_NAME) + 1
                    self.assertEqual(jobs[start:start + len(expected)], expected)


class EmbedRender(unittest.TestCase):
    def test_trial_profile_shares_position_zero_with_its_flags_and_memory(self) -> None:
        loaded = load_models_lock(ROOT / "models.lock")
        self.assertTrue(loaded.ok, loaded.errors)
        assert loaded.lock is not None
        profile = loaded.lock.profile("1x32v-120d")
        assert profile is not None
        rendered_inputs = inputs(EXAMPLE)
        rendered_inputs = replace(
            rendered_inputs,
            site=replace(rendered_inputs.site, hardware_profile=profile.name),
            profile=profile,
        )
        self.assertEqual(len(rendered_inputs.facts.gpu_uuids), 2)
        blocks = service_blocks(rendered_inputs)
        for member in (GENERATOR, EMBED):
            with self.subTest(role=member.role):
                pin = profile.model(member.role)
                row = profile.memory_row(member.service_name)
                assert pin is not None and row is not None
                block = blocks[member.service_name]
                assert isinstance(block, Mapping)
                self.assertEqual(
                    block["devices"],
                    [f"nvidia.com/gpu={rendered_inputs.facts.gpu_uuids[0]}"],
                )
                self.assertEqual(block["mem_limit"], row.gb * GIGABYTE)
                command = block["command"]
                assert isinstance(command, list)
                flag_names = (
                    ("gpu-memory-utilization", "max-num-seqs", "reasoning-parser")
                    if member is GENERATOR
                    else ("kv-cache-memory-bytes", "max-model-len", "gpu-memory-utilization")
                )
                for name in flag_names:
                    self.assertEqual(command[command.index(f"--{name}") + 1], str(pin.serve.flags[name]))
                if member is GENERATOR:
                    self.assertIn("--no-enable-prefix-caching", command)
                    self.assertNotIn("--kv-cache-dtype", command)
                    environment = block["environment"]
                    assert isinstance(environment, Mapping)
                    self.assertNotIn("VLLM_USE_DEEP_GEMM", environment)

    def test_out_of_range_gpu_refusal_names_the_record_and_re_record_step(self) -> None:
        rendered_inputs = inputs(EXAMPLE)
        pin = rendered_inputs.profile.model(EMBED.role)
        assert pin is not None
        recorded = rendered_inputs.facts.gpu_uuids[:pin.gpu]
        facts = HostFacts(recorded, rendered_inputs.facts.service_gid)

        with self.assertRaises(ValueError) as caught:
            model_service(replace(rendered_inputs, facts=facts), EMBED)

        refusal = str(caught.exception)
        for fragment in (
            EMBED.role,
            f"index {pin.gpu}",
            str(gpus.GPU_RECORD_PATH),
            f"{len(recorded)} card(s)",
            "nvidia-smi -L",
            "hardware-profile",
            "as root",
            "render --diff",
        ):
            self.assertIn(fragment, refusal)

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
                self.assertEqual(generator["networks"], ["gideon", INTEGRATION_NETWORK_NAME])
                self.assertEqual(block["networks"], ["gideon", INTERNAL_NETWORK_NAME])
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
                    [f"{weights.MODELS_ROOT}:{pin.serve.env['HF_HOME']}:ro", route_guard_mount()],
                )
                self.assertEqual(block["secrets"], [EMBED.secret_name])
                environment = block["environment"]
                assert isinstance(environment, Mapping)
                self.assertNotIn("VLLM_API_KEY", environment)
                self.assertEqual(environment["PYTHONPATH"], ROUTE_GUARD_MOUNT_DIR)
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
                cache_bound = "kv-cache-memory-bytes"
                self.assertEqual(
                    command[command.index(f"--{cache_bound}") + 1],
                    str(pin.serve.flags[cache_bound]),
                )
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

    def test_each_member_uses_the_same_guard_additions(self) -> None:
        rendered_inputs = inputs(EXAMPLE)
        blocks = service_blocks(rendered_inputs)
        for member in MODEL_SERVERS:
            with self.subTest(role=member.role):
                block = blocks[member.service_name]
                assert isinstance(block, Mapping)
                pin = rendered_inputs.profile.model(member.role)
                assert pin is not None
                volumes = block["volumes"]
                environment = block["environment"]
                command = block["command"]
                assert isinstance(volumes, list)
                assert isinstance(environment, Mapping)
                assert isinstance(command, list)
                self.assertEqual(len(volumes), 3 if member is RERANK else 2)
                self.assertEqual(volumes[1], route_guard_mount())
                self.assertEqual(environment["PYTHONPATH"], ROUTE_GUARD_MOUNT_DIR)
                self.assertNotIn("VLLM_API_KEY", environment)
                self.assertNotIn("labels", block)
                self.assertEqual(ModelServerService(member).sources, ())
                self.assertEqual(command.count("--middleware"), 1)
                self.assertEqual(command.count(route_guard_import_path()), 1)
                filter_index = command.index("--disable-access-log-for-endpoints")
                self.assertEqual(
                    command[filter_index : filter_index + 4],
                    [
                        "--disable-access-log-for-endpoints",
                        ",".join(ENGINE_ACCESS_LOG_EXCLUDED_PATHS),
                        "--middleware",
                        route_guard_import_path(),
                    ],
                )
                expected_flags: list[str] = []
                for name, value in pin.serve.flags.items():
                    expected_flags.append(f"--{name}")
                    if value is not True:
                        expected_flags.append(str(value))
                self.assertEqual(command[-len(expected_flags) :], expected_flags)

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
