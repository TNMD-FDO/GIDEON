"""SearXNG's rendered service, settings, env, and logging.

The service and its three files exist only while ``web.search`` is on, on a GPU
host and a no-GPU host alike; the frontend's connection to it is held by
``test_render_owui.py`` and the byte-stable fixtures by ``test_render.py``.
"""

import hashlib
import json
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from gideon.host.images import load_image_lock
from gideon.host.lock import load_host_lock
from gideon.host.models import HardwareProfile, load_models_lock, select_profile
from gideon.host.render import ARTIFACTS, RenderInputs, render_all
from gideon.host.render.api import API_SERVICE_NAME
from gideon.host.render.command import (
    AppliedManifest,
    compose_digests,
    recreate_services,
)
from gideon.host.render.compose import (
    SEARXNG_HEALTHCHECK,
    _compose_document,
    service_images,
    service_names,
)
from gideon.host.render.engine import ENGINE_SERVICE_NAME
from gideon.host.render.facts import HostFacts
from gideon.host.render.searxng import (
    SEARXNG_LOGGING_PATH,
    SEARXNG_NETWORK_LOG_LEVEL,
    SEARXNG_NETWORK_LOGGER,
    SEARXNG_SECRET_NAME,
    SEARXNG_SERVICE_NAME,
    SEARXNG_SETTINGS_PATH,
    SearxngEnvArtifact,
    SearxngLoggingArtifact,
    SearxngSettingsArtifact,
    search_enabled,
    searxng_health_url,
    searxng_logging_document,
    searxng_query_url,
    searxng_secret_environment,
    searxng_settings_document,
)
from gideon.host.site import WEB_ENGINE_VALUES, load_site

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config/site.example.yaml"
SECOND = ROOT / "tests/fixtures/site/second-office.yaml"
TEMPLATE_PATHS = tuple(
    dict.fromkeys(path for artifact in ARTIFACTS for path in artifact.template_paths)
)
SECRETS = {
    "ldap_bind_password": "bind-password",
    "postgres_openwebui_password": "postgres-password",
    "gideon_admin_password": "admin-password",
    "engine_api_key": "engine-api-key",
    "gideon_api_key": "gideon-api-key",
    SEARXNG_SECRET_NAME: "searxng-secret-key",
}


def inputs(site_path: Path = EXAMPLE, **overrides: object) -> RenderInputs:
    site = load_site(site_path).config
    lock = load_host_lock(ROOT / "host.lock").lock
    images = load_image_lock(ROOT / "images.lock").lock
    models = load_models_lock(ROOT / "models.lock").lock
    assert site is not None and lock is not None and images is not None and models is not None
    profile = select_profile(models, site.hardware_profile)
    assert isinstance(profile, HardwareProfile)
    base = RenderInputs(
        site=site,
        lock=lock,
        images=images,
        facts=HostFacts(("GPU-a", "GPU-b"), service_gid=4242),
        profile=profile,
        templates={name: (ROOT / "compose" / name).read_text() for name in TEMPLATE_PATHS},
        release="fixture",
        secrets=dict(SECRETS),
        checkout="/opt/gideon",
        api_sources_digest="sha256:" + "0" * 64,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def with_search(base: RenderInputs, search: str, **web: object) -> RenderInputs:
    """The same inputs with ``web.search`` and any other web leaf replaced."""

    return replace(base, site=replace(base.site, web=replace(base.site.web, search=search, **web)))  # type: ignore[arg-type]


def searxng_block(base: RenderInputs) -> Mapping[str, object]:
    services = _compose_document(base)["services"]
    assert isinstance(services, Mapping)
    block = services[SEARXNG_SERVICE_NAME]
    assert isinstance(block, Mapping)
    return block


class Settings(unittest.TestCase):
    def test_the_site_engines_are_kept_and_each_re_enabled_by_name(self) -> None:
        base = inputs()
        document = searxng_settings_document(base)
        engines = list(base.site.web.engines)
        self.assertEqual(document["use_default_settings"], {"engines": {"keep_only": engines}})
        # bing and google are disabled in upstream's own defaults, and keep_only
        # governs membership alone, so every kept engine is re-enabled by name.
        self.assertEqual(
            document["engines"], [{"name": engine, "disabled": False} for engine in engines]
        )
        self.assertNotIn("google", engines)
        self.assertNotIn("google", str(document))

    def test_google_appears_only_when_the_site_names_it(self) -> None:
        engines = [*inputs().site.web.engines, "google"]
        document = searxng_settings_document(with_search(inputs(), "on", engines=engines))
        keep_only = document["use_default_settings"]["engines"]["keep_only"]  # type: ignore[index]
        self.assertEqual(keep_only[-1], "google")
        self.assertIn({"name": "google", "disabled": False}, document["engines"])  # type: ignore[arg-type]

    def test_a_site_naming_duckduckgo_still_renders_it(self) -> None:
        engines = [*inputs().site.web.engines, "duckduckgo"]
        document = searxng_settings_document(with_search(inputs(), "on", engines=engines))
        keep_only = document["use_default_settings"]["engines"]["keep_only"]  # type: ignore[index]
        self.assertEqual(keep_only[-1], "duckduckgo")
        self.assertIn(
            {"name": "duckduckgo", "disabled": False}, document["engines"]  # type: ignore[arg-type]
        )

    def test_pinned_defaults_the_two_formats_and_no_secret_key(self) -> None:
        document = searxng_settings_document(inputs())
        self.assertEqual(document["general"], {"debug": False})
        self.assertEqual(
            document["search"],
            {"formats": ["html", "json"], "autocomplete": "", "favicon_resolver": ""},
        )
        self.assertEqual(
            document["server"],
            {"limiter": False, "public_instance": False, "image_proxy": False, "method": "GET"},
        )
        self.assertNotIn("secret_key", SearxngSettingsArtifact().emit(inputs()))
        self.assertNotIn("proxies", SearxngSettingsArtifact().emit(inputs()))

    def test_an_engine_outside_the_release_list_refuses_naming_the_fix(self) -> None:
        bad = with_search(inputs(), "on", engines=["duckduckgo", "braveapi"])
        with self.assertRaises(ValueError) as caught:
            searxng_settings_document(bad)
        message = str(caught.exception)
        self.assertIn("'braveapi'", message)
        self.assertIn(", ".join(WEB_ENGINE_VALUES), message)
        self.assertIn("/etc/gideon/site.yaml", message)
        self.assertTrue(message.endswith("then re-run render."))

    def test_every_release_engine_name_renders(self) -> None:
        document = searxng_settings_document(
            with_search(inputs(), "on", engines=list(WEB_ENGINE_VALUES))
        )
        self.assertEqual(len(document["engines"]), len(WEB_ENGINE_VALUES))  # type: ignore[arg-type]


class Logging(unittest.TestCase):
    def test_logging_document_keeps_granian_and_sets_the_network_logger(self) -> None:
        document = searxng_logging_document()
        text = SearxngLoggingArtifact().emit(inputs())
        self.assertEqual(json.loads(text), document)
        self.assertEqual(document["version"], 1)
        self.assertFalse(document["disable_existing_loggers"])
        loggers = document["loggers"]
        assert isinstance(loggers, Mapping)
        self.assertEqual(
            set(loggers),
            {"_granian", "granian.access", SEARXNG_NETWORK_LOGGER},
        )
        self.assertEqual(
            loggers["_granian"],
            {"handlers": ["console"], "level": "INFO", "propagate": False},
        )
        self.assertEqual(
            loggers["granian.access"],
            {"handlers": ["access"], "level": "INFO", "propagate": False},
        )
        self.assertEqual(
            loggers[SEARXNG_NETWORK_LOGGER],
            {"level": SEARXNG_NETWORK_LOG_LEVEL},
        )
        self.assertNotIn("handlers", document)
        self.assertNotIn("formatters", document)
        self.assertEqual(text, json.dumps(document, indent=2, sort_keys=True) + "\n")


class Env(unittest.TestCase):
    def test_the_secret_alone_without_a_proxy(self) -> None:
        self.assertEqual(
            searxng_secret_environment(inputs()), {"SEARXNG_SECRET": "searxng-secret-key"}
        )
        artifact = SearxngEnvArtifact()
        self.assertEqual(artifact.emit(inputs()), "SEARXNG_SECRET=searxng-secret-key\n")
        self.assertTrue(artifact.secret)
        self.assertEqual(artifact.mode, 0o600)
        self.assertEqual(artifact.owners, (SEARXNG_SERVICE_NAME,))

    def test_a_proxy_rides_the_env_with_its_credentials_and_no_no_proxy(self) -> None:
        proxied = with_search(inputs(SECOND, secrets={**SECRETS, "proxy_auth": "user:p@ss"}), "on")
        values = searxng_secret_environment(proxied)
        self.assertEqual(values["HTTP_PROXY"], "http://user:p%40ss@proxy.exd.example.internal:3128")
        self.assertEqual(values["HTTPS_PROXY"], values["HTTP_PROXY"])
        self.assertNotIn("NO_PROXY", values)
        bare = with_search(inputs(SECOND), "on")
        self.assertEqual(
            searxng_secret_environment(bare)["HTTP_PROXY"], "http://proxy.exd.example.internal:3128"
        )

    def test_a_missing_secret_or_a_malformed_proxy_auth_refuses(self) -> None:
        without = inputs(secrets={name: value for name, value in SECRETS.items() if name != SEARXNG_SECRET_NAME})
        with self.assertRaises(ValueError) as caught:
            searxng_secret_environment(without)
        self.assertEqual(str(caught.exception), f"Render secret is missing: {SEARXNG_SECRET_NAME}.")
        malformed = with_search(inputs(SECOND, secrets={**SECRETS, "proxy_auth": "no-separator"}), "on")
        with self.assertRaises(ValueError):
            searxng_secret_environment(malformed)


class Artifacts(unittest.TestCase):
    def test_all_files_exist_only_while_search_is_on(self) -> None:
        on, off = inputs(), inputs(SECOND)
        self.assertTrue(search_enabled(on))
        self.assertFalse(search_enabled(off))
        for artifact in (
            SearxngSettingsArtifact(),
            SearxngEnvArtifact(),
            SearxngLoggingArtifact(),
        ):
            with self.subTest(artifact=artifact.name):
                self.assertTrue(artifact.applies(on))
                self.assertFalse(artifact.applies(off))
                self.assertEqual(artifact.owners, (SEARXNG_SERVICE_NAME,))
        paths_on = set(render_all(on).by_path)
        paths_off = set(render_all(off).by_path)
        search_paths = {"searxng/settings.yml", "searxng/env", "searxng/logging.json"}
        self.assertEqual(search_paths & paths_on, search_paths)
        self.assertFalse(search_paths & paths_off)
        self.assertEqual(
            render_all(inputs(no_gpu=True)).by_path.keys() & search_paths,
            search_paths,
        )


class Service(unittest.TestCase):
    def test_present_iff_search_is_on_after_the_frontend_and_the_engine(self) -> None:
        gpu = service_names(inputs())
        self.assertEqual(gpu.index(API_SERVICE_NAME), gpu.index(ENGINE_SERVICE_NAME) + 1)
        self.assertEqual(gpu.index(SEARXNG_SERVICE_NAME), gpu.index(API_SERVICE_NAME) + 1)
        self.assertEqual(gpu.index(ENGINE_SERVICE_NAME), gpu.index("open-webui") + 1)
        no_gpu = service_names(inputs(no_gpu=True))
        self.assertEqual(no_gpu.index(SEARXNG_SERVICE_NAME), no_gpu.index("open-webui") + 1)
        self.assertNotIn(SEARXNG_SERVICE_NAME, service_names(inputs(SECOND)))
        self.assertNotIn(SEARXNG_SERVICE_NAME, service_names(inputs(SECOND, no_gpu=True)))
        for base in (inputs(), inputs(no_gpu=True), inputs(SECOND)):
            self.assertEqual(len(service_images(base)), len(service_names(base)))

    def test_the_block_runs_the_image_as_shipped_on_the_network_alone(self) -> None:
        base = inputs()
        block = searxng_block(base)
        pin = next(pin for pin in base.images.images if pin.name == SEARXNG_SERVICE_NAME)
        self.assertTrue(str(block["image"]).endswith(f"/searxng@{pin.digest}"))
        self.assertEqual(block["restart"], "unless-stopped")
        self.assertEqual(
            block["environment"],
            {
                "GRANIAN_LOG_ACCESS_ENABLED": "false",
                "GRANIAN_LOG_CONFIG": SEARXNG_LOGGING_PATH,
                "TZ": base.site.office.timezone,
            },
        )
        self.assertEqual(
            block["env_file"], [{"path": "/etc/gideon/rendered/searxng/env", "format": "raw"}]
        )
        self.assertEqual(
            block["volumes"],
            [
                f"/etc/gideon/rendered/searxng/settings.yml:{SEARXNG_SETTINGS_PATH}:ro",
                f"/etc/gideon/rendered/searxng/logging.json:{SEARXNG_LOGGING_PATH}:ro",
            ],
        )
        self.assertEqual(block["healthcheck"], dict(SEARXNG_HEALTHCHECK))
        test = SEARXNG_HEALTHCHECK["test"]
        assert isinstance(test, list)
        self.assertEqual(test[:2], ["CMD", "wget"])
        self.assertTrue(test[-1].endswith("/healthz"))
        self.assertEqual(block["networks"], ["gideon"])
        for absent in ("ports", "user", "depends_on", "secrets", "command", "entrypoint"):
            self.assertNotIn(absent, block)
        secrets = _compose_document(base)["secrets"]
        assert isinstance(secrets, Mapping)
        self.assertNotIn(SEARXNG_SECRET_NAME, secrets)

    def test_the_urls_name_the_service_and_port(self) -> None:
        self.assertEqual(searxng_query_url(), "http://searxng:8080/search")
        self.assertEqual(searxng_health_url(), "http://searxng:8080/healthz")


class Flip(unittest.TestCase):
    """The kill switch both ways, judged by the existing recreate rule."""

    @staticmethod
    def applied(base: RenderInputs) -> AppliedManifest:
        digests = compose_digests(base)
        files = {
            rendered.relative_path: {
                "sha256": hashlib.sha256(rendered.content.encode()).hexdigest(),
                "owners": list(rendered.owners),
            }
            for rendered in render_all(base).files
        }
        return AppliedManifest(files, digests.services, digests.top_level)

    def test_off_leaves_the_departed_service_to_orphan_removal(self) -> None:
        on = inputs()
        off = with_search(on, "off")
        recreated = recreate_services(
            render_all(off), self.applied(on), service_names(off), compose_digests(off)
        )
        self.assertNotIn(SEARXNG_SERVICE_NAME, recreated)
        self.assertIn("open-webui", recreated)
        self.assertIn("prometheus", recreated)

    def test_on_renders_both_files_new_and_recreates_their_owner(self) -> None:
        on = inputs()
        off = with_search(on, "off")
        recreated = recreate_services(
            render_all(on), self.applied(off), service_names(on), compose_digests(on)
        )
        self.assertIn(SEARXNG_SERVICE_NAME, recreated)
        self.assertIn("prometheus", recreated)
        self.assertIn("open-webui", recreated)


if __name__ == "__main__":
    unittest.main()
