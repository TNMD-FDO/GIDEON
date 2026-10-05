"""The queue worker's identity, Compose block, and recreate boundary."""

import hashlib
import unittest
from collections.abc import Mapping
from dataclasses import replace

from test_render import EXAMPLE, ROOT, SECOND, DirHost, declared_source_files, inputs

from gideon.host.images import parse_registry, reference
from gideon.host.models import GIGABYTE
from gideon.host.render import render_all, worker
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
from gideon.host.render.compose import service_blocks, service_names
from gideon.host.render.services import SERVICES, image_pin
from gideon.host.render.services.worker import (
    WORKER_HEALTHCHECK,
    WORKER_SOURCES_DIGEST_LABEL,
    WorkerService,
)
from gideon.host.stores import ROLE_SPECS
from gideon.worker import settings, tasks


class WorkerIdentity(unittest.TestCase):
    def test_role_matches_the_store_and_runtime_names_match_the_render(self) -> None:
        role = next(spec for spec in ROLE_SPECS if spec.name == worker.WORKER_ROLE)
        self.assertEqual(
            role.secret_name, worker.WORKER_SECRET_NAME,
            "Fix: render the secret assigned to the worker database role.",
        )
        self.assertEqual(
            (
                worker.DATABASE_HOST_ENV,
                worker.DATABASE_PORT_ENV,
                worker.DATABASE_NAME_ENV,
                worker.DATABASE_ROLE_ENV,
                worker.PASSWORD_FILE_ENV,
                worker.CONCURRENCY_ENV,
            ),
            (
                settings.DATABASE_HOST_ENV,
                settings.DATABASE_PORT_ENV,
                settings.DATABASE_NAME_ENV,
                settings.DATABASE_ROLE_ENV,
                settings.PASSWORD_FILE_ENV,
                settings.CONCURRENCY_ENV,
            ),
            "Fix: keep the rendered environment names equal to worker settings.",
        )
        self.assertEqual(
            (
                worker.WORKER_RECOVERY_TASK,
                worker.WORKER_RECOVERY_QUEUE,
                worker.WORKER_RECOVERY_CRON,
            ),
            (tasks.RECOVERY_TASK, tasks.RECOVERY_QUEUE, tasks.RECOVERY_CRON),
            "Fix: keep the recovery identity equal to the registered task.",
        )
        self.assertEqual(
            worker.WORKER_VERIFY_TASK, "gideon.worker.tasks.verify",
            "Fix: keep the host command's verify task name at the worker's entry.",
        )
        self.assertEqual(
            worker.WORKER_VERIFY_QUEUE, "verify",
            "Fix: send the host's verify job to its worker queue.",
        )
        self.assertEqual(
            worker.WORKER_MIGRATION_STEM, "0007_procrastinate_queue",
            "Fix: point the host command at the installed queue migration.",
        )
        self.assertEqual(
            worker.WORKER_CONCURRENCY, 4,
            "Fix: restore the worker's starting concurrency.",
        )
        self.assertEqual(
            (
                worker.WORKER_DATABASE_HOST,
                worker.WORKER_DATABASE_PORT,
                worker.WORKER_DATABASE_NAME,
            ),
            ("postgres", 5432, "gideon"),
            "Fix: connect the worker to the rendered database endpoint.",
        )


class WorkerBlock(unittest.TestCase):
    def test_definition_and_block_on_each_host_kind(self) -> None:
        definition = next(
            service for service in SERVICES if service.name == worker.WORKER_SERVICE_NAME
        )
        self.assertIsInstance(
            definition, WorkerService,
            "Fix: register the worker's service definition.",
        )
        self.assertEqual(
            definition.sources, ("gideon/worker",),
            "Fix: declare the worker package as its mounted source.",
        )
        self.assertFalse(
            definition.store, "Fix: start the worker after store convergence."
        )
        self.assertTrue(definition.swap, "Fix: retain the worker's default swap policy.")
        self.assertIsNone(
            definition.slow_start_seconds,
            "Fix: leave worker startup to the healthcheck's start period.",
        )
        names = tuple(service.name for service in SERVICES)
        self.assertEqual(
            names.index(worker.WORKER_SERVICE_NAME), names.index("gideon-api") + 1,
            "Fix: register the worker directly after the API.",
        )

        for site_path, no_gpu in ((EXAMPLE, False), (SECOND, False), (EXAMPLE, True)):
            rendered_inputs = inputs(site_path, no_gpu=no_gpu)
            with self.subTest(site=site_path.name, no_gpu=no_gpu):
                self.assertTrue(
                    definition.applies(rendered_inputs),
                    "Fix: render the worker on every host kind.",
                )
                self.assertIn(
                    worker.WORKER_SERVICE_NAME, service_names(rendered_inputs),
                    "Fix: include the worker in each rendered Compose project.",
                )
                target = parse_registry(rendered_inputs.site.registry)
                assert target is not None
                block = service_blocks(rendered_inputs)[worker.WORKER_SERVICE_NAME]
                assert isinstance(block, Mapping)
                row = rendered_inputs.profile.memory_row(worker.WORKER_SERVICE_NAME)
                assert row is not None
                self.assertEqual(
                    block,
                    {
                        "image": reference(
                            target, image_pin(rendered_inputs, API_IMAGE_NAME)
                        ),
                        "restart": "unless-stopped",
                        "command": ["python", "-m", "gideon.worker"],
                        "working_dir": API_WORKING_DIRECTORY,
                        "volumes": [
                            f"{rendered_inputs.checkout}/gideon:{API_MOUNT_TARGET}:ro"
                        ],
                        "read_only": True,
                        "group_add": [str(rendered_inputs.facts.service_gid)],
                        "secrets": [worker.WORKER_SECRET_NAME],
                        "environment": {
                            worker.DATABASE_HOST_ENV: worker.WORKER_DATABASE_HOST,
                            worker.DATABASE_PORT_ENV: str(worker.WORKER_DATABASE_PORT),
                            worker.DATABASE_NAME_ENV: worker.WORKER_DATABASE_NAME,
                            worker.DATABASE_ROLE_ENV: worker.WORKER_ROLE,
                            worker.PASSWORD_FILE_ENV: (
                                f"/run/secrets/{worker.WORKER_SECRET_NAME}"
                            ),
                            worker.CONCURRENCY_ENV: str(worker.WORKER_CONCURRENCY),
                            "TZ": rendered_inputs.site.office.timezone,
                        },
                        "labels": {
                            WORKER_SOURCES_DIGEST_LABEL: rendered_inputs.source_digests[
                                worker.WORKER_SERVICE_NAME
                            ]
                        },
                        "stop_grace_period": "20s",
                        "healthcheck": dict(WORKER_HEALTHCHECK),
                        "networks": ["gideon"],
                        "mem_limit": row.gb * GIGABYTE,
                    },
                    "Fix: restore the worker's reviewed Compose block.",
                )
                self.assertEqual(
                    block["healthcheck"],
                    {
                        "test": ["CMD", "python", "-m", "gideon.worker.health"],
                        "interval": "30s",
                        "timeout": "10s",
                        "retries": 3,
                        "start_period": "30s",
                    },
                    "Fix: run the worker heartbeat probe with its reviewed bounds.",
                )
                for absent in ("ports", "depends_on", "user", "tmpfs", "memswap_limit"):
                    self.assertNotIn(
                        absent, block,
                        f"Fix: remove {absent} from the worker's Compose block.",
                    )

    def test_worker_source_change_recreates_only_the_worker(self) -> None:
        files = declared_source_files()
        previous_digests = gather_source_digests(DirHost(files), ROOT)
        previous_inputs = replace(inputs(), source_digests=previous_digests)
        previous = render_all(previous_inputs)
        previous_compose = compose_digests(previous_inputs)
        applied = AppliedManifest(
            files={
                rendered_file.relative_path: {
                    "sha256": hashlib.sha256(rendered_file.content.encode()).hexdigest(),
                    "owners": list(rendered_file.owners),
                }
                for rendered_file in previous.files
            },
            services=previous_compose.services,
            top_level=previous_compose.top_level,
            top_level_parts=previous_compose.top_level_parts,
        )
        changed_files = dict(files)
        changed_files[str(ROOT / "gideon/worker/__init__.py")] += "\n# fixture change\n"
        current_digests = gather_source_digests(DirHost(changed_files), ROOT)
        current_inputs = replace(previous_inputs, source_digests=current_digests)
        judgment = recreate_judgment(
            render_all(current_inputs),
            applied,
            service_names(current_inputs),
            compose_digests(current_inputs),
        )
        self.assertEqual(
            judgment.services,
            (worker.WORKER_SERVICE_NAME,),
            "Fix: label the worker block with only its declared source digest.",
        )
        self.assertEqual(
            judgment.reasons(),
            (("changed compose block", (worker.WORKER_SERVICE_NAME,)),),
            "Fix: recreate the worker for its source digest change alone.",
        )


if __name__ == "__main__":
    unittest.main()
