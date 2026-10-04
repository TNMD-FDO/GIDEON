"""Behavior of the ordered Compose service definitions."""

import hashlib
import json
import unittest
from collections.abc import Mapping
from dataclasses import replace
from unittest.mock import patch

from test_apply import (
    PS,
    ApplyHost,
    apply,
    argv_calls,
    base_files,
    done,
    healthy_commands,
    ps_rows,
    running_rows,
)
from test_render import EXAMPLE, SECOND, inputs

from gideon.host import apply as apply_module
from gideon.host.images import RegistryTarget, parse_registry, reference
from gideon.host.models import MemoryRow
from gideon.host.render import RenderedFile, RenderedSet, RenderInputs
from gideon.host.render.api import API_SECRET_NAME, API_SERVICE_NAME
from gideon.host.render.command import (
    AppliedManifest,
    ComposeDigests,
    compose_digests,
    recreate_judgment,
)
from gideon.host.render.compose import (
    STORE_SERVICES,
    ComposeArtifact,
    compose_top_level,
    service_blocks,
    service_images,
    service_names,
)
from gideon.host.render.consumers import secret_consumers
from gideon.host.render.engine import ENGINE_SECRET_NAME, ENGINE_SERVICE_NAME
from gideon.host.render.services import (
    SERVICES,
    ServiceDefinition,
    all_service_names,
    applying_services,
    slow_start_services,
    store_services,
)
from gideon.host.render.services.api import ApiService
from gideon.host.render.services.dcgm_exporter import DcgmExporterService
from gideon.host.render.services.generator import GeneratorService
from gideon.host.render.services.searxng import SearxngService


def host_inputs() -> tuple[RenderInputs, ...]:
    return (inputs(EXAMPLE), inputs(SECOND), inputs(EXAMPLE, no_gpu=True))


class Registry(unittest.TestCase):
    def test_declared_secrets_equal_rendered_mounts_on_each_host(self) -> None:
        """Declarations include every mount and no orphan."""

        for rendered_inputs in host_inputs():
            with self.subTest(
                site=rendered_inputs.site.hostname, no_gpu=rendered_inputs.no_gpu
            ):
                blocks = service_blocks(rendered_inputs)
                mounted: set[str] = set()
                for block in blocks.values():
                    assert isinstance(block, Mapping)
                    names = block.get("secrets", ())
                    assert isinstance(names, (list, tuple))
                    mounted.update(names)
                declared = compose_top_level(rendered_inputs)["secrets"]
                assert isinstance(declared, Mapping)
                self.assertEqual(set(declared), mounted)

    def test_declared_secrets_match_consumer_order_and_files(self) -> None:
        """Each mounted secret uses its production file path."""

        for rendered_inputs in host_inputs():
            with self.subTest(
                site=rendered_inputs.site.hostname, no_gpu=rendered_inputs.no_gpu
            ):
                declared = compose_top_level(rendered_inputs)["secrets"]
                assert isinstance(declared, Mapping)
                mounted = tuple(
                    name
                    for name, consumers in secret_consumers(rendered_inputs).items()
                    if consumers.mounts
                )
                self.assertEqual(tuple(declared), mounted)
                self.assertEqual(
                    dict(declared),
                    {
                        name: {"file": f"/etc/gideon/secrets/{name}"}
                        for name in mounted
                    },
                )

    def test_no_gpu_omits_gpu_only_secrets(self) -> None:
        """Skipped service mounts make no declarations."""

        declared = compose_top_level(inputs(EXAMPLE, no_gpu=True))["secrets"]
        assert isinstance(declared, Mapping)
        for name in (
            ENGINE_SECRET_NAME,
            API_SECRET_NAME,
            "postgres_gideon_audit_password",
        ):
            with self.subTest(secret=name):
                self.assertNotIn(name, declared)

    def test_substituted_registry_derives_secrets_from_applying_blocks(self) -> None:
        """A credential joins through its rendered block alone."""

        class FirstService(ServiceDefinition):
            name = "example-first"

            def applies(self, inputs: RenderInputs) -> bool:
                return not inputs.no_gpu

            def block(
                self, inputs: RenderInputs, target: RegistryTarget
            ) -> Mapping[str, object]:
                return {
                    "image": "example.invalid/first",
                    "secrets": ["example-shared", "example-first-secret"],
                }

        class SecondService(ServiceDefinition):
            name = "example-second"

            def block(
                self, inputs: RenderInputs, target: RegistryTarget
            ) -> Mapping[str, object]:
                return {
                    "image": "example.invalid/second",
                    "secrets": ["example-second-secret", "example-shared"],
                }

        base = inputs()
        gb = base.profile.memory[0].gb
        profile = replace(
            base.profile,
            memory=(
                MemoryRow(FirstService.name, gb, None),
                MemoryRow(SecondService.name, gb, None),
            ),
        )
        gpu = replace(base, profile=profile)
        no_gpu = replace(gpu, no_gpu=True)
        with patch(
            "gideon.host.render.services.SERVICES", [FirstService(), SecondService()]
        ):
            for rendered_inputs, expected in (
                (
                    gpu,
                    ("example-shared", "example-first-secret", "example-second-secret"),
                ),
                (no_gpu, ("example-second-secret", "example-shared")),
            ):
                with self.subTest(no_gpu=rendered_inputs.no_gpu):
                    declared = compose_top_level(rendered_inputs)["secrets"]
                    assert isinstance(declared, Mapping)
                    self.assertEqual(tuple(declared), expected)
                    self.assertEqual(
                        dict(declared),
                        {
                            name: {"file": f"/etc/gideon/secrets/{name}"}
                            for name in expected
                        },
                    )

    def test_secret_joining_or_leaving_a_block_names_its_mounter(self) -> None:
        """The changed block and secret entry select their one mounter."""

        secret_name = "example_secret"

        class FirstService(ServiceDefinition):
            name = "example-first"

            def __init__(self, mounts_secret: bool) -> None:
                self.mounts_secret = mounts_secret

            def block(
                self, inputs: RenderInputs, target: RegistryTarget
            ) -> Mapping[str, object]:
                block: dict[str, object] = {"image": "example.invalid/first"}
                if self.mounts_secret:
                    block["secrets"] = [secret_name]
                return block

        class SecondService(ServiceDefinition):
            name = "example-second"

            def block(
                self, inputs: RenderInputs, target: RegistryTarget
            ) -> Mapping[str, object]:
                return {"image": "example.invalid/second"}

        base = inputs()
        gb = base.profile.memory[0].gb
        profile = replace(
            base.profile,
            memory=(
                MemoryRow(FirstService.name, gb, None),
                MemoryRow(SecondService.name, gb, None),
            ),
        )
        rendered_inputs = replace(base, profile=profile)

        def state(mounts_secret: bool) -> tuple[RenderedSet, ComposeDigests]:
            with patch(
                "gideon.host.render.services.SERVICES",
                [FirstService(mounts_secret), SecondService()],
            ):
                artifact = ComposeArtifact()
                rendered = RenderedSet(
                    (
                        RenderedFile(
                            artifact.relative_path,
                            artifact.emit(rendered_inputs),
                            artifact.mode,
                            artifact.owners,
                        ),
                    )
                )
                return rendered, compose_digests(rendered_inputs)

        for before, after in ((False, True), (True, False)):
            with self.subTest(joining=after):
                previous_rendered, previous_digests = state(before)
                current_rendered, current_digests = state(after)
                applied = AppliedManifest(
                    files={
                        "compose.yaml": {
                            "sha256": hashlib.sha256(
                                previous_rendered.by_path["compose.yaml"].content.encode()
                            ).hexdigest(),
                            "owners": [],
                        }
                    },
                    services=previous_digests.services,
                    top_level=previous_digests.top_level,
                    top_level_parts=previous_digests.top_level_parts,
                )
                judgment = recreate_judgment(
                    current_rendered,
                    applied,
                    tuple(current_digests.services),
                    current_digests,
                )
                self.assertEqual(judgment.services, (FirstService.name,))
                self.assertEqual(judgment.block, (FirstService.name,))
                self.assertEqual(judgment.secrets, (FirstService.name,))
                self.assertEqual(judgment.top_level, ())
                self.assertEqual(judgment.changed_entries, (secret_name,))
                self.assertEqual(judgment.files, ())
                source_parts = (
                    current_digests.top_level_parts
                    if after
                    else previous_digests.top_level_parts
                )
                self.assertEqual(
                    source_parts.secrets[secret_name].mounts, (FirstService.name,)
                )

    def test_names_are_unique_and_nonempty(self) -> None:
        names = all_service_names()
        self.assertTrue(names)
        self.assertTrue(all(names))
        self.assertEqual(len(names), len(set(names)))

    def test_applying_definitions_match_rendered_blocks_on_each_host(self) -> None:
        for rendered_inputs in host_inputs():
            with self.subTest(
                site=rendered_inputs.site.hostname, no_gpu=rendered_inputs.no_gpu
            ):
                definitions = applying_services(rendered_inputs)
                self.assertEqual(
                    tuple(definition.name for definition in definitions),
                    service_names(rendered_inputs),
                )
                target = parse_registry(rendered_inputs.site.registry)
                assert target is not None
                blocks = service_blocks(rendered_inputs)
                for definition in definitions:
                    with self.subTest(service=definition.name):
                        block = blocks[definition.name]
                        self.assertIsInstance(block, Mapping)
                        assert isinstance(block, Mapping)
                        rendered_block = dict(block)
                        del rendered_block["mem_limit"]
                        self.assertEqual(
                            definition.block(rendered_inputs, target), rendered_block
                        )

    def test_store_members_apply_on_every_host(self) -> None:
        stores = store_services()
        self.assertEqual(stores, STORE_SERVICES)
        for rendered_inputs in host_inputs():
            with self.subTest(
                site=rendered_inputs.site.hostname, no_gpu=rendered_inputs.no_gpu
            ):
                applying = {
                    definition.name for definition in applying_services(rendered_inputs)
                }
                self.assertTrue(set(stores) <= applying)

    def test_slow_starters_have_positive_allowances_and_matching_healthchecks(
        self,
    ) -> None:
        rendered_inputs = inputs()
        target = parse_registry(rendered_inputs.site.registry)
        assert target is not None
        allowances = slow_start_services()
        self.assertTrue(allowances)
        for name, seconds in allowances.items():
            with self.subTest(service=name):
                self.assertGreater(seconds, 0)
                definition = next(
                    definition for definition in SERVICES if definition.name == name
                )
                healthcheck = definition.block(rendered_inputs, target)["healthcheck"]
                self.assertIsInstance(healthcheck, Mapping)
                assert isinstance(healthcheck, Mapping)
                self.assertEqual(healthcheck["start_period"], f"{seconds}s")

    def test_names_equal_the_committed_profile_memory_table(self) -> None:
        self.assertEqual(
            set(all_service_names()),
            {row.service for row in inputs().profile.memory},
        )

    def test_rendered_images_match_committed_lock_pins(self) -> None:
        hosts = host_inputs()
        pin_names = {pin.name for pin in hosts[0].images.images}
        matched_pin_names: set[str] = set()
        for rendered_inputs in hosts:
            with self.subTest(
                site=rendered_inputs.site.hostname, no_gpu=rendered_inputs.no_gpu
            ):
                target = parse_registry(rendered_inputs.site.registry)
                assert target is not None
                pin_names_by_reference = {
                    reference(target, pin): pin.name
                    for pin in rendered_inputs.images.images
                }
                for image in service_images(rendered_inputs):
                    self.assertIn(image, pin_names_by_reference, f"unpinned image {image}")
                    matched_pin_names.add(pin_names_by_reference[image])
        self.assertEqual(
            matched_pin_names,
            pin_names,
            f"orphan pin: {', '.join(sorted(pin_names - matched_pin_names))}",
        )

    def test_conditional_definitions_apply_on_their_hosts(self) -> None:
        hosts = host_inputs()
        for definition, expected in (
            (GeneratorService(), (True, True, False)),
            (ApiService(), (True, True, False)),
            (DcgmExporterService(), (True, True, False)),
            (SearxngService(), (True, False, True)),
        ):
            with self.subTest(service=definition.name):
                self.assertEqual(
                    tuple(definition.applies(rendered_inputs) for rendered_inputs in hosts),
                    expected,
                )

    def test_substituted_registry_orders_and_skips_blocks(self) -> None:
        class FirstService(ServiceDefinition):
            name = "example-first"

            def block(
                self, inputs: RenderInputs, target: RegistryTarget
            ) -> Mapping[str, object]:
                return {"image": "example.invalid/first"}

        class SecondService(ServiceDefinition):
            name = "example-second"

            def applies(self, inputs: RenderInputs) -> bool:
                return not inputs.no_gpu

            def block(
                self, inputs: RenderInputs, target: RegistryTarget
            ) -> Mapping[str, object]:
                if inputs.no_gpu:
                    raise AssertionError("a skipped service built its block")
                return {"image": "example.invalid/second"}

        base = inputs()
        gb = base.profile.memory[0].gb
        profile = replace(
            base.profile,
            memory=(
                MemoryRow(SecondService.name, gb, None),
                MemoryRow(FirstService.name, gb, None),
            ),
        )
        gpu = replace(base, profile=profile)
        no_gpu = replace(gpu, no_gpu=True)
        with patch(
            "gideon.host.render.services.SERVICES", [SecondService(), FirstService()]
        ):
            self.assertEqual(
                service_names(gpu), (SecondService.name, FirstService.name)
            )
            self.assertEqual(
                tuple(service_blocks(gpu)), (SecondService.name, FirstService.name)
            )
            self.assertEqual(service_names(no_gpu), (FirstService.name,))
            self.assertEqual(tuple(service_blocks(no_gpu)), (FirstService.name,))

    def test_no_gpu_render_does_not_need_the_exporter_pin(self) -> None:
        rendered_inputs = inputs(no_gpu=True)
        images = replace(
            rendered_inputs.images,
            images=tuple(
                pin
                for pin in rendered_inputs.images.images
                if pin.name != "dcgm-exporter"
            ),
        )
        blocks = service_blocks(replace(rendered_inputs, images=images))
        self.assertNotIn("dcgm-exporter", blocks)


class Verify(unittest.TestCase):
    def test_two_slow_starters_share_the_longer_health_wait(self) -> None:
        sleep_seconds = int(apply_module._VERIFY_SLEEP_SECONDS)
        short_allowance = 2 * sleep_seconds
        long_allowance = 5 * sleep_seconds
        attempts = long_allowance // sleep_seconds

        def rows_with_unhealthy(name: str) -> str:
            rows = [json.loads(line) for line in running_rows().splitlines()]
            for row in rows:
                if row["Service"] == name:
                    row["Health"] = "starting"
            return ps_rows(*rows)

        commands = healthy_commands()
        commands[PS] = [
            done(PS, stdout=running_rows()),
            done(PS, stdout=rows_with_unhealthy(API_SERVICE_NAME)),
            *(
                done(PS, stdout=rows_with_unhealthy(API_SERVICE_NAME))
                for _ in range(attempts - 2)
            ),
            done(PS, stdout=rows_with_unhealthy(ENGINE_SERVICE_NAME)),
            done(PS, stdout=running_rows()),
            done(PS, stdout=running_rows()),
        ]
        host = ApplyHost(commands, base_files())
        with patch.object(
            apply_module,
            "slow_start_services",
            return_value={
                ENGINE_SERVICE_NAME: short_allowance,
                API_SERVICE_NAME: long_allowance,
            },
        ):
            code, out, _ = apply(host)
            refused_commands = healthy_commands()
            refused_commands[PS] = [
                done(PS, stdout=running_rows()),
                *(
                    done(PS, stdout=rows_with_unhealthy(API_SERVICE_NAME))
                    for _ in range(attempts + 1)
                ),
            ]
            refused_host = ApplyHost(refused_commands, base_files())
            refused_code, refused_out, _ = apply(refused_host)

        self.assertEqual(code, 0, out)
        self.assertIn("engine healthy", out)
        self.assertEqual(argv_calls(host).count(PS), attempts + 3)
        self.assertEqual(refused_code, 1)
        self.assertIn(f"logs {API_SERVICE_NAME}", refused_out)
        self.assertEqual(argv_calls(refused_host).count(PS), attempts + 2)
