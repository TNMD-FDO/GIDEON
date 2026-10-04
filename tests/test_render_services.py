"""Behavior of the ordered Compose service definitions."""

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
from gideon.host.images import RegistryTarget, parse_registry
from gideon.host.models import MemoryRow
from gideon.host.render import RenderInputs
from gideon.host.render.api import API_SERVICE_NAME
from gideon.host.render.compose import STORE_SERVICES, service_blocks, service_names
from gideon.host.render.engine import ENGINE_SERVICE_NAME
from gideon.host.render.services import (
    SERVICES,
    ServiceDefinition,
    all_service_names,
    applying_services,
    slow_start_services,
    store_services,
)


def host_inputs() -> tuple[RenderInputs, ...]:
    return (inputs(EXAMPLE), inputs(SECOND), inputs(EXAMPLE, no_gpu=True))


class Registry(unittest.TestCase):
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
