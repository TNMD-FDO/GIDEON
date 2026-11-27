"""The API service, probes, alert, and service-source digest contracts."""

import hashlib
import json
import tempfile
import unittest
from collections import UserDict
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml  # type: ignore[import-untyped]
from regenerate_render_fixtures import fixture_lockfile_courts
from test_courtmap import _generate
from test_render import (
    RENDERED,
    SITE,
    DirHost,
    checkout_files,
    inputs,
    render,
    trial_inputs,
)

from gideon.api.settings import load_settings
from gideon.host.cas import ROOT as CAS_ROOT
from gideon.host.courts import CourtMap
from gideon.host.images import parse_registry
from gideon.host.index import header
from gideon.host.render import RenderInputs, render_all
from gideon.host.render.api import (
    API_CHAT_HEADER,
    API_COURTS_CIRCUIT_ENV,
    API_COURTS_DISTRICTS_ENV,
    API_COURTS_FILE_ENV,
    API_COURTS_MOUNT,
    API_COURTS_PATH,
    API_COURTS_SCOTUS_ENV,
    API_COURTS_STATES_ENV,
    API_EMBED_DIMENSIONS_ENV,
    API_EMBED_MODEL_ENV,
    API_EMBED_REPOSITORY_ENV,
    API_EMBED_SPACE_ENV,
    API_HEALTH_PATH,
    API_INSTRUCTION_MOUNT,
    API_INSTRUCTION_PATH,
    API_JOB_NAME,
    API_MOUNT_TARGET,
    API_RERANK_KEY_FILE_ENV,
    API_RERANK_URL_ENV,
    API_SECRET_NAME,
    API_SERVICE_NAME,
    API_SOURCE_HEADER,
    API_USER_EMAIL_HEADER,
    API_USER_ID_HEADER,
    TURN_READER_ROLE,
    TURN_READER_SECRET_NAME,
)
from gideon.host.render.command import (
    AppliedManifest,
    compose_digests,
    gather_sources_digest,
    recreate_judgment,
)
from gideon.host.render.compose import service_names
from gideon.host.render.courts import ApiCourtsArtifact, api_courts_text
from gideon.host.render.engine import (
    EMBED,
    EMBED_SERVICE_NAME,
    ENGINE_SECRET_NAME,
    ENGINE_SERVICE_NAME,
    RERANK,
)
from gideon.host.render.opensearch import (
    OPENSEARCH_TURN_SECRET_NAME,
    OPENSEARCH_TURN_USER,
    opensearch_rest_url,
)
from gideon.host.render.owui import EVAL_IDENTITY, GENERAL_MODEL_ID
from gideon.host.render.qdrant import QDRANT_READ_ONLY_SECRET_NAME, qdrant_rest_url
from gideon.host.render.services import declared_sources
from gideon.host.render.services.api import api_service
from gideon.host.render.services.generator import engine_service
from gideon.host.site import Jurisdiction
from gideon.host.stores import ROLE_SPECS, RoleSpec
from gideon.host.sysio import PathLike
from gideon.turn.scope import read_court_file

ROOT = Path(__file__).resolve().parent.parent
OTHER_DIGEST = "sha256:" + "1" * 64


class RecordingEnvironment(UserDict[str, str]):
    def __init__(self, values: dict[str, str]) -> None:
        super().__init__(values)
        self.reads: set[str] = set()

    def __getitem__(self, name: str) -> str:
        self.reads.add(name)
        return super().__getitem__(name)


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
    def test_turn_reader_role_matches_the_store(self) -> None:
        self.assertIn(RoleSpec(TURN_READER_ROLE, TURN_READER_SECRET_NAME), ROLE_SPECS)

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
            [
                ENGINE_SECRET_NAME,
                API_SECRET_NAME,
                "postgres_gideon_audit_password",
                TURN_READER_SECRET_NAME,
                QDRANT_READ_ONLY_SECRET_NAME,
                EMBED.secret_name,
                OPENSEARCH_TURN_SECRET_NAME,
                RERANK.secret_name,
            ],
        )
        self.assertEqual(api["environment"]["GIDEON_SOURCE_HEADER"], API_SOURCE_HEADER)
        self.assertEqual(api["environment"]["GIDEON_CHAT_HEADER"], API_CHAT_HEADER)
        self.assertEqual(api["environment"]["GIDEON_USER_HEADER"], API_USER_ID_HEADER)
        self.assertEqual(api["environment"]["GIDEON_EVAL_IDENTITY"], EVAL_IDENTITY.email)
        self.assertEqual(api["environment"]["GIDEON_INSTRUCTION_FILE"], API_INSTRUCTION_MOUNT)
        self.assertEqual(api["environment"][API_COURTS_FILE_ENV], API_COURTS_MOUNT)
        court_file = gpu_rendered.by_path[API_COURTS_PATH]
        self.assertEqual((court_file.owners, court_file.mode), ((API_SERVICE_NAME,), 0o644))
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
                f"/etc/gideon/rendered/{API_COURTS_PATH}:{API_COURTS_MOUNT}:ro",
                f"{CAS_ROOT}:{CAS_ROOT}:ro",
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
        self.assertNotIn(API_COURTS_PATH, no_gpu_rendered.by_path)
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

    def test_reranker_settings_and_mount_follow_the_selected_profile(self) -> None:
        reference = yaml.safe_load(render_all(inputs()).by_path["compose.yaml"].content)
        trial = yaml.safe_load(render_all(trial_inputs()).by_path["compose.yaml"].content)
        reference_api = reference["services"][API_SERVICE_NAME]
        trial_api = trial["services"][API_SERVICE_NAME]
        self.assertEqual(
            reference_api["environment"][API_RERANK_URL_ENV], f"http://{RERANK.service_name}:{RERANK.port}"
        )
        self.assertEqual(
            reference_api["environment"][API_RERANK_KEY_FILE_ENV], f"/run/secrets/{RERANK.secret_name}"
        )
        self.assertIn(RERANK.secret_name, reference_api["secrets"])
        self.assertIn(RERANK.secret_name, reference["secrets"])
        self.assertNotIn(API_RERANK_URL_ENV, trial_api["environment"])
        self.assertNotIn(API_RERANK_KEY_FILE_ENV, trial_api["environment"])
        self.assertNotIn(RERANK.secret_name, trial_api["secrets"])
        self.assertNotIn(RERANK.secret_name, trial["secrets"])

    def test_api_reranker_pair_reads_the_shared_membership_rule(self) -> None:
        base = inputs()
        registry = parse_registry(base.site.registry)
        assert registry is not None
        for renders in (False, True):
            with self.subTest(renders=renders), patch(
                "gideon.host.render.services.api.has_reranker", return_value=renders,
            ) as membership:
                block = api_service(base, registry)
                membership.assert_called_once_with(base.profile, no_gpu=base.no_gpu)
                environment = block["environment"]
                mounted = block["secrets"]
                assert isinstance(environment, dict) and isinstance(mounted, list)
                self.assertEqual(API_RERANK_URL_ENV in environment, renders)
                self.assertEqual(API_RERANK_KEY_FILE_ENV in environment, renders)
                self.assertEqual(RERANK.secret_name in mounted, renders)

    def test_rendered_api_settings_and_loader_reads_match(self) -> None:
        rendered = render_all(inputs())
        document = yaml.safe_load(rendered.by_path["compose.yaml"].content)
        values: dict[str, str] = document["services"][API_SERVICE_NAME]["environment"]
        del values["TZ"]  # The container uses TZ independently of the settings loader.
        self.assertEqual(values["GIDEON_API_QDRANT_KEY_FILE"], f"/run/secrets/{QDRANT_READ_ONLY_SECRET_NAME}")
        self.assertEqual(values["GIDEON_API_EMBED_KEY_FILE"], f"/run/secrets/{EMBED.secret_name}")
        self.assertEqual(
            values["GIDEON_API_OPENSEARCH_PASSWORD_FILE"],
            f"/run/secrets/{OPENSEARCH_TURN_SECRET_NAME}",
        )
        with tempfile.TemporaryDirectory() as directory:
            for name, value in values.items():
                if name.endswith("_FILE"):
                    path = Path(directory) / Path(value).name
                    contents = rendered.by_path[API_COURTS_PATH].content if name == API_COURTS_FILE_ENV else "fictitious mount contents\n"
                    path.write_text(contents, encoding="utf-8")
                    values[name] = str(path)
            environment = RecordingEnvironment(values)
            settings = load_settings(environment)

        self.assertEqual(environment.reads, set(values))
        self.assertEqual(settings.cas_root, CAS_ROOT)
        self.assertEqual(settings.qdrant_url, qdrant_rest_url())
        self.assertEqual(settings.qdrant_key_file, Path(values["GIDEON_API_QDRANT_KEY_FILE"]))
        self.assertEqual(settings.embed_url, f"http://{EMBED.service_name}:{EMBED.port}")
        self.assertEqual(settings.embed_key_file, Path(values["GIDEON_API_EMBED_KEY_FILE"]))
        self.assertEqual(settings.embed_model, values[API_EMBED_MODEL_ENV])
        self.assertEqual(settings.embed_repository, values[API_EMBED_REPOSITORY_ENV])
        self.assertEqual(settings.embed_space, values[API_EMBED_SPACE_ENV])
        self.assertEqual(settings.embed_dimensions, int(values[API_EMBED_DIMENSIONS_ENV]))
        self.assertEqual(settings.opensearch_url, opensearch_rest_url())
        self.assertEqual(settings.opensearch_user, OPENSEARCH_TURN_USER)
        self.assertEqual(
            settings.opensearch_password_file, Path(values["GIDEON_API_OPENSEARCH_PASSWORD_FILE"]),
        )
        self.assertEqual(settings.courts_scotus, tuple(values[API_COURTS_SCOTUS_ENV].split(",")))
        self.assertEqual(settings.courts_circuit, (values[API_COURTS_CIRCUIT_ENV],))
        self.assertEqual(settings.courts_districts, tuple(values[API_COURTS_DISTRICTS_ENV].split(",")))
        states = values[API_COURTS_STATES_ENV]
        self.assertEqual(settings.courts_states, tuple(states.split(",")) if states else ())
        self.assertEqual(settings.courts_file, Path(values[API_COURTS_FILE_ENV]))

    def test_court_file_is_byte_stable_and_readable_with_fixture_lockfiles(self) -> None:
        court_map = inputs().courts
        assert court_map is not None
        lockfiles = fixture_lockfile_courts()
        self.assertTrue(lockfiles)
        fixture = ROOT / "tests/fixtures/render/example" / API_COURTS_PATH
        text = api_courts_text(court_map, lockfiles)
        self.assertEqual(text.encode("utf-8"), fixture.read_bytes())
        reordered = replace(court_map, courts=dict(reversed(tuple(court_map.courts.items()))))
        self.assertEqual(text, api_courts_text(reordered, dict(reversed(tuple(lockfiles.items())))))
        self.assertEqual(json.loads(text)["lockfiles"], {label: list(ids) for label, ids in lockfiles.items()})
        read_court_file(fixture)

    def test_rendered_court_groups_resolve_to_the_controlling_set(self) -> None:
        court_map = _generate()
        assert isinstance(court_map, CourtMap)
        base = inputs(courts=court_map)
        for states in (["fx-sup-al"], []):
            with self.subTest(states=states):
                jurisdiction = Jurisdiction("ca11", ["alsd", "alnd"], states)
                configured = replace(base, site=replace(base.site, jurisdiction=jurisdiction))
                document = yaml.safe_load(render_all(configured).by_path["compose.yaml"].content)
                environment = document["services"][API_SERVICE_NAME]["environment"]
                groups = tuple(environment[name] for name in (
                    API_COURTS_SCOTUS_ENV, API_COURTS_CIRCUIT_ENV, API_COURTS_DISTRICTS_ENV, API_COURTS_STATES_ENV,
                ))
                self.assertEqual(groups, (
                    ",".join(court_map.ids_at_level("scotus")), jurisdiction.circuit,
                    ",".join(sorted(jurisdiction.districts)), ",".join(court_map.appellate_courts("AL")) if states else "",
                ))
                self.assertEqual(
                    {court for group in groups if group for court in group.split(",")},
                    set(court_map.controlling_courts(jurisdiction.circuit, jurisdiction.districts, states)),
                )

    def test_render_refuses_unknown_court_and_wrong_level_with_preflight_fix(self) -> None:
        base = inputs()
        for leaf, value, identifier, reason in (
            ("districts", ["fixturemissingcourt"], "fixturemissingcourt", "absent from courts.yaml"),
            ("circuit", base.site.jurisdiction.districts[0], base.site.jurisdiction.districts[0], "expected 'circuit'"),
        ):
            with self.subTest(leaf=leaf):
                files = checkout_files()
                site = yaml.safe_load(files[SITE])
                site["jurisdiction"][leaf] = value
                files[SITE] = yaml.safe_dump(site)
                host = DirHost(files)
                code, _, error = render(host)
                self.assertEqual(code, 1)
                line, = error.splitlines()
                for detail in (f"jurisdiction.{leaf}", repr(identifier), reason, "re-run preflight"):
                    self.assertIn(detail, line)
                self.assertEqual([path for path in host.writes if path.startswith(f"{RENDERED}/")], [])

    def test_embedding_settings_follow_the_profile_and_release_header(self) -> None:
        for rendered_inputs in (inputs(), trial_inputs()):
            pin = rendered_inputs.profile.model(EMBED.role)
            space = rendered_inputs.profile.embedding_space
            assert pin is not None and space is not None
            for header_id in dict.fromkeys((header.RELEASE_HEADER, *header.HEADERS)):
                with self.subTest(profile=rendered_inputs.profile.name, header=header_id), patch.object(
                    header, "RELEASE_HEADER", header_id,
                ):
                    document = yaml.safe_load(render_all(rendered_inputs).by_path["compose.yaml"].content)
                    environment = document["services"][API_SERVICE_NAME]["environment"]
                    self.assertEqual(environment[API_EMBED_MODEL_ENV], pin.serve.served_name)
                    self.assertEqual(environment[API_EMBED_REPOSITORY_ENV], pin.repo)
                    self.assertEqual(environment[API_EMBED_SPACE_ENV], header.embedding_space(space.id, header_id))
                    self.assertEqual(environment[API_EMBED_DIMENSIONS_ENV], str(space.dimensions))

    def test_missing_checkout_court_map_or_digest_refuses(self) -> None:
        base = inputs()
        registry = parse_registry(base.site.registry)
        assert registry is not None
        with self.assertRaisesRegex(ValueError, "Re-run render with a checkout\\."):
            api_service(replace(base, checkout=""), registry)
        with self.assertRaisesRegex(ValueError, r"Restore courts\.yaml and re-run render"):
            api_service(replace(base, courts=None), registry)
        with self.assertRaisesRegex(ValueError, r"Restore courts\.yaml and re-run render"):
            ApiCourtsArtifact().emit(replace(base, courts=None))
        with self.assertRaisesRegex(
            ValueError, "Re-run render from a release checkout, whose loader gathers it\\."
        ):
            api_service(replace(base, source_digests={}), registry)

    def test_api_file_mounts_accept_a_rendered_root(self) -> None:
        base = inputs()
        registry = parse_registry(base.site.registry)
        assert registry is not None
        block = api_service(base, registry, rendered_root="/tmp/fixture-rendered")
        self.assertEqual(
            block["volumes"],
            [
                f"{base.checkout}/gideon:{API_MOUNT_TARGET}:ro",
                f"/tmp/fixture-rendered/{API_INSTRUCTION_PATH}:{API_INSTRUCTION_MOUNT}:ro",
                f"/tmp/fixture-rendered/{API_COURTS_PATH}:{API_COURTS_MOUNT}:ro",
                f"{CAS_ROOT}:{CAS_ROOT}:ro",
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

    def test_moving_a_lockfile_recreates_only_api_for_the_changed_court_file(self) -> None:
        lockfiles = fixture_lockfile_courts()
        original = inputs(lockfile_courts=lockfiles)
        label = next(iter(lockfiles))
        moved = replace(original, lockfile_courts={key: ids for key, ids in lockfiles.items() if key != label})
        before = applied_record(original)
        after = render_all(moved)
        self.assertNotEqual(before.files[API_COURTS_PATH]["sha256"], hashlib.sha256(
            after.by_path[API_COURTS_PATH].content.encode(),
        ).hexdigest())
        judgment = recreate_judgment(after, before, service_names(moved), compose_digests(moved))
        self.assertEqual(judgment.services, (API_SERVICE_NAME,))
        self.assertEqual(judgment.files, (API_SERVICE_NAME,))
        self.assertEqual(judgment.block, ())
        self.assertEqual(judgment.top_level, ())

    def test_moving_the_digest_recreates_only_the_api_service_block(self) -> None:
        original = inputs()
        moved = replace(original, source_digests={**original.source_digests, API_SERVICE_NAME: OTHER_DIGEST})
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

    def test_moving_user_header_recreates_only_the_api_service_block(self) -> None:
        original = inputs()
        applied = applied_record(original)
        with patch("gideon.host.render.services.api.API_USER_ID_HEADER", "X-Moved-User"):
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

    def test_moving_reader_url_recreates_only_the_api_service_block(self) -> None:
        original = inputs()
        applied = applied_record(original)
        with patch(
            "gideon.host.render.services.api.opensearch_rest_url",
            return_value="http://fixture-lexical:7003",
        ):
            judgment = recreate_judgment(
                render_all(original), applied, service_names(original), compose_digests(original),
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
        self.assertEqual(before_digests[EMBED_SERVICE_NAME], after_digests[EMBED_SERVICE_NAME])
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
        self.assertEqual(
            before_compose["services"][EMBED_SERVICE_NAME],
            after_compose["services"][EMBED_SERVICE_NAME],
        )
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
    def test_turn_extraction_and_serving_sources_move_the_api_digest(self) -> None:
        paths = (
            ROOT / "gideon/turn/__init__.py", ROOT / "gideon/extraction/contract.py",
            ROOT / "gideon/casecite/adapter.py", ROOT / "gideon/host/index/serving.py",
        )
        files = {**checkout_files(), **{str(path): path.read_text(encoding="utf-8") for path in paths}}
        sources = declared_sources()[API_SERVICE_NAME]
        original = gather_sources_digest(DirHost(files), ROOT, sources)
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                edited = {**files, str(path): files[str(path)] + "\n# Fictitious source change.\n"}
                self.assertNotEqual(original, gather_sources_digest(DirHost(edited), ROOT, sources))

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
