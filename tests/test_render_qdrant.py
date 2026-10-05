"""Qdrant's rendered Compose block, secret wrapper, and swap ceiling."""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from test_render import EXAMPLE, SECOND, inputs

from gideon.host.images import parse_registry, reference
from gideon.host.render.compose import ComposeArtifact, service_blocks
from gideon.host.render.prometheus import PrometheusConfigArtifact
from gideon.host.render.qdrant import (
    QDRANT_DATA_ROOT,
    QDRANT_JOB_NAME,
    QDRANT_METRICS_PORT,
    QDRANT_SECRET_NAME,
    QDRANT_SERVICE_NAME,
    QDRANT_STORAGE_MOUNT,
    qdrant_metrics_target,
)
from gideon.host.render.services import secret_wrapper


class QdrantRender(unittest.TestCase):
    def test_scrape_job_uses_the_keyless_metrics_listener_on_every_host(self) -> None:
        for rendered_inputs in (
            inputs(EXAMPLE),
            inputs(SECOND),
            inputs(EXAMPLE, no_gpu=True),
        ):
            with self.subTest(
                site=rendered_inputs.site.hostname, no_gpu=rendered_inputs.no_gpu
            ):
                document = yaml.safe_load(PrometheusConfigArtifact().emit(rendered_inputs))
                jobs = [
                    job
                    for job in document["scrape_configs"]
                    if job["job_name"] == QDRANT_JOB_NAME
                ]
                self.assertEqual(len(jobs), 1)
                self.assertEqual(
                    jobs[0]["static_configs"],
                    [{"targets": [qdrant_metrics_target()]}],
                )
                self.assertNotIn("authorization", jobs[0])
                self.assertNotIn("metrics_path", jobs[0])

    def test_block_on_each_host_uses_its_pin_key_and_data_root(self) -> None:
        for rendered_inputs in (
            inputs(EXAMPLE),
            inputs(SECOND),
            inputs(EXAMPLE, no_gpu=True),
        ):
            with self.subTest(
                site=rendered_inputs.site.hostname, no_gpu=rendered_inputs.no_gpu
            ):
                block = service_blocks(rendered_inputs)[QDRANT_SERVICE_NAME]
                assert isinstance(block, dict)
                pin = next(
                    pin
                    for pin in rendered_inputs.images.images
                    if pin.name == QDRANT_SERVICE_NAME
                )
                target = parse_registry(rendered_inputs.site.registry)
                assert target is not None
                self.assertEqual(block["image"], reference(target, pin))
                self.assertEqual(block["restart"], "unless-stopped")
                self.assertEqual(
                    block["environment"],
                    {
                        "QDRANT__TELEMETRY_DISABLED": "true",
                        "QDRANT__SERVICE__ENABLE_STATIC_CONTENT": "false",
                        "QDRANT__SERVICE__METRICS_PORT": str(QDRANT_METRICS_PORT),
                        "TZ": rendered_inputs.site.office.timezone,
                    },
                )
                self.assertNotIn(
                    rendered_inputs.secrets[QDRANT_SECRET_NAME],
                    str(block["environment"]),
                )
                entrypoint = block["entrypoint"]
                assert isinstance(entrypoint, list)
                self.assertEqual(entrypoint[:2], ["sh", "-c"])
                self.assertEqual(entrypoint[3], QDRANT_SERVICE_NAME)
                self.assertIn(f"/run/secrets/{QDRANT_SECRET_NAME}", entrypoint[2])
                self.assertIn("QDRANT__SERVICE__API_KEY=$(cat", entrypoint[2])
                self.assertIn('exec ./qdrant "$@"', entrypoint[2])
                self.assertEqual(
                    block["volumes"], [f"{QDRANT_DATA_ROOT}:{QDRANT_STORAGE_MOUNT}"]
                )
                self.assertEqual(block["secrets"], [QDRANT_SECRET_NAME])
                self.assertEqual(block["networks"], ["gideon"])
                self.assertIn("healthcheck", block)
                for absent in ("ports", "user", "command", "depends_on", "group_add"):
                    with self.subTest(absent=absent):
                        self.assertNotIn(absent, block)

    def test_qdrant_alone_has_a_swap_ceiling_one_page_above_its_memory_limit(self) -> None:
        for rendered_inputs in (
            inputs(EXAMPLE),
            inputs(SECOND),
            inputs(EXAMPLE, no_gpu=True),
        ):
            with self.subTest(
                site=rendered_inputs.site.hostname, no_gpu=rendered_inputs.no_gpu
            ):
                for name, block in service_blocks(rendered_inputs).items():
                    assert isinstance(block, dict)
                    if name == QDRANT_SERVICE_NAME:
                        self.assertEqual(
                            block["memswap_limit"], block["mem_limit"] + 4096
                        )
                    else:
                        self.assertNotIn("memswap_limit", block)

    def test_compose_strings_have_no_undoubled_variable_prefix(self) -> None:
        for rendered_inputs in (
            inputs(EXAMPLE),
            inputs(SECOND),
            inputs(EXAMPLE, no_gpu=True),
        ):
            with self.subTest(
                site=rendered_inputs.site.hostname, no_gpu=rendered_inputs.no_gpu
            ):
                document = ComposeArtifact().emit(rendered_inputs)
                self.assertIsNone(re.search(r"(?<!\$)\$[A-Za-z_{]", document))


class QdrantWrapper(unittest.TestCase):
    def test_wrapper_exports_the_secret_to_the_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "qdrant-key"
            secret.write_text("fixture-qdrant-key\n")
            result = subprocess.run(
                secret_wrapper(
                    str(secret),
                    "QDRANT__SERVICE__API_KEY",
                    "qdrant API key",
                    sys.executable,
                    QDRANT_SERVICE_NAME,
                )
                + [
                    "-c",
                    "import os; raise SystemExit(os.environ.get('QDRANT__SERVICE__API_KEY') != 'fixture-qdrant-key')",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrapper_refuses_missing_and_empty_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing-key"
            empty = Path(directory) / "empty-key"
            empty.write_text("")
            for secret in (missing, empty):
                with self.subTest(secret=secret):
                    result = subprocess.run(
                        secret_wrapper(
                            str(secret),
                            "QDRANT__SERVICE__API_KEY",
                            "qdrant API key",
                            sys.executable,
                            QDRANT_SERVICE_NAME,
                        )
                        + ["-c", "raise SystemExit(23)"],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(len(result.stderr.splitlines()), 1)
                    self.assertIn(str(secret), result.stderr)
