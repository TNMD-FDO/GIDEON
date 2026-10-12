"""The rendered model-server guard's ownership, mount identity, and source input."""

import hashlib
import inspect
import unittest
from dataclasses import replace
from unittest.mock import patch

from test_render import ROOT, SECOND, DirHost, checkout_files, inputs, render

from gideon.host.render import ARTIFACTS, render_all
from gideon.host.render.command import (
    AppliedManifest,
    compose_digests,
    load_templates,
    recreate_judgment,
)
from gideon.host.render.compose import service_names
from gideon.host.render.engine import MODEL_SERVERS
from gideon.host.render.routeguard import (
    ROUTE_GUARD_CLASS,
    ROUTE_GUARD_MODULE,
    ROUTE_GUARD_MOUNT_DIR,
    ROUTE_GUARD_RENDERED_PATH,
    ROUTE_GUARD_SOURCE,
    RouteGuardArtifact,
    route_guard_import_path,
    route_guard_mount,
)
from gideon.host.render.services import MountedSecret
from gideon.host.render.services.model_server import (
    ENGINE_ACCESS_LOG_EXCLUDED_PATHS,
    model_wrapper,
)
from gideon.modelserver import guard


class ArtifactContract(unittest.TestCase):
    def test_gpu_hosts_emit_verbatim_source_owned_by_each_member(self) -> None:
        artifact = RouteGuardArtifact()
        self.assertEqual(sum(isinstance(item, RouteGuardArtifact) for item in ARTIFACTS), 1)
        self.assertEqual(artifact.owners, tuple(member.service_name for member in MODEL_SERVERS))
        self.assertEqual(artifact.mode, 0o644)
        self.assertEqual(artifact.source_paths, (ROUTE_GUARD_SOURCE,))
        source = (ROOT / ROUTE_GUARD_SOURCE).read_text()
        for rendered_inputs in (inputs(), inputs(SECOND)):
            with self.subTest(profile=rendered_inputs.profile.name):
                rendered = render_all(rendered_inputs).by_path[ROUTE_GUARD_RENDERED_PATH]
                self.assertEqual(rendered.content, source)
                self.assertEqual(rendered.owners, artifact.owners)
                self.assertEqual(rendered.mode, artifact.mode)
        self.assertNotIn(ROUTE_GUARD_RENDERED_PATH, render_all(inputs(no_gpu=True)).by_path)

    def test_mount_import_and_key_match_the_running_wrapper(self) -> None:
        module, class_name = route_guard_import_path().split(".")
        self.assertEqual(module, ROUTE_GUARD_MODULE)
        self.assertEqual(class_name, ROUTE_GUARD_CLASS)
        self.assertIs(getattr(guard, class_name), guard.RouteGuard)
        self.assertEqual(len(inspect.signature(guard.RouteGuard).parameters), 1)
        self.assertEqual(guard.OPEN_PATHS, ENGINE_ACCESS_LOG_EXCLUDED_PATHS)
        self.assertEqual(
            route_guard_mount(),
            f"/etc/gideon/rendered/{ROUTE_GUARD_RENDERED_PATH}:"
            f"{ROUTE_GUARD_MOUNT_DIR}/{ROUTE_GUARD_MODULE}.py:ro",
        )
        with patch(
            "gideon.host.render.services.model_server.secret_wrapper",
            return_value=[],
        ) as wrapper:
            model_wrapper("/run/secrets/fixture", "server", MODEL_SERVERS[0])
        mounted = wrapper.call_args.args[0][0]
        self.assertIsInstance(mounted, MountedSecret)
        self.assertEqual(guard.KEY_VARIABLE, mounted.variable)


class SourceInput(unittest.TestCase):
    def test_changed_source_recreates_only_its_two_owners(self) -> None:
        host = DirHost(checkout_files())
        previous_inputs = replace(inputs(), templates=load_templates(host, ROOT))
        previous = render_all(previous_inputs)
        digests = compose_digests(previous_inputs)
        applied = AppliedManifest(
            {
                file.relative_path: {
                    "sha256": hashlib.sha256(file.content.encode()).hexdigest(),
                    "owners": list(file.owners),
                }
                for file in previous.files
            },
            digests.services,
            digests.top_level,
        )

        host.files[str(ROOT / ROUTE_GUARD_SOURCE)] += "\n# fictitious guard change\n"
        current_inputs = replace(previous_inputs, templates=load_templates(host, ROOT))
        judgment = recreate_judgment(
            render_all(current_inputs),
            applied,
            service_names(current_inputs),
            compose_digests(current_inputs),
        )
        owners = tuple(member.service_name for member in MODEL_SERVERS)
        self.assertEqual(judgment.services, owners)
        self.assertEqual(judgment.files, owners)
        self.assertEqual(judgment.block, ())
        self.assertEqual(judgment.top_level, ())
        self.assertEqual(judgment.reasons(), (("changed rendered files", owners),))

    def test_missing_source_refuses_with_the_release_checkout_fix(self) -> None:
        files = checkout_files()
        del files[str(ROOT / ROUTE_GUARD_SOURCE)]
        code, _, err = render(DirHost(files))
        self.assertEqual(code, 1)
        self.assertIn(f"Render source is missing: {ROUTE_GUARD_SOURCE}", err)
        self.assertIn("A checkout without it is not a release tree", err)
        self.assertNotIn("Traceback", err)
