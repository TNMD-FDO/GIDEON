"""The rendered stack's secret consumers (spec §1.7 and §3.5)."""

import unittest
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

from gideon.host.images import load_image_lock
from gideon.host.lock import load_host_lock
from gideon.host.models import HardwareProfile, load_models_lock, select_profile
from gideon.host.render import ARTIFACTS, RenderInputs
from gideon.host.render.consumers import SecretConsumers, consumers_of, secret_consumers
from gideon.host.render.facts import HostFacts
from gideon.host.render.owui import OwuiEnvArtifact
from gideon.host.render.searxng import SearxngEnvArtifact
from gideon.host.site import load_site

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
    "searxng_secret_key": "searxng-secret-key",
}


class RecordingSecrets(Mapping[str, str]):
    """A mapping that records value reads while leaving membership side-effect free."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)
        self.reads: list[str] = []

    def __getitem__(self, name: str) -> str:
        self.reads.append(name)
        return self._values[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __contains__(self, name: object) -> bool:
        return name in self._values


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
        secrets={
            **SECRETS,
            **({"proxy_auth": "proxy-user:proxy-password"} if site_path == SECOND else {}),
        },
        checkout="/opt/gideon",
        api_sources_digest="sha256:" + "0" * 64,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


class ConsumerMap(unittest.TestCase):
    def test_the_map_follows_mounts_and_secret_env_artifacts(self) -> None:
        gpu = secret_consumers(inputs())
        second = secret_consumers(inputs(SECOND))
        no_gpu = secret_consumers(inputs(no_gpu=True))

        self.assertEqual(gpu["engine_api_key"], SecretConsumers(("gideon-generator", "gideon-api"), ("open-webui",)))
        self.assertEqual(gpu["gideon_api_key"], SecretConsumers(("gideon-api",), ()))
        self.assertEqual(no_gpu.get("engine_api_key"), None)
        self.assertEqual(no_gpu.get("gideon_api_key"), None)
        self.assertEqual(gpu["webui_secret_key"], SecretConsumers(("open-webui",), ()))
        self.assertEqual(gpu["postgres_gideon_audit_password"], SecretConsumers(("open-webui",), ()))
        self.assertEqual(gpu["postgres_gideon_ro_metrics_password"], SecretConsumers(("grafana", "postgres-exporter"), ()))
        self.assertEqual(consumers_of(inputs(), "postgres_gideon_eval_password"), SecretConsumers((), ()))
        self.assertEqual(gpu["tls_key"], SecretConsumers(("caddy",), ()))
        self.assertEqual(gpu["ldap_bind_password"], SecretConsumers(("grafana",), ("open-webui",)))
        self.assertEqual(gpu["searxng_secret_key"], SecretConsumers((), ("searxng",)))
        self.assertEqual(consumers_of(inputs(), "gideon_admin_api_key"), SecretConsumers((), ()))
        self.assertEqual(consumers_of(inputs(), "gideon_eval_api_key"), SecretConsumers((), ()))

        self.assertEqual(second["smtp_password"], SecretConsumers(("grafana",), ()))
        self.assertEqual(second["proxy_auth"], SecretConsumers((), ("open-webui",)))
        self.assertEqual(consumers_of(inputs(SECOND), "searxng_secret_key"), SecretConsumers((), ()))

        # A leftover credential file with the proxy off is carried by nothing:
        # the helper reads it only under a configured proxy.
        leftover = inputs(secrets={**SECRETS, "proxy_auth": "proxy-user:proxy-password"})
        self.assertFalse(leftover.site.egress_proxy)
        self.assertEqual(consumers_of(leftover, "proxy_auth"), SecretConsumers((), ()))

    def test_secret_declarations_equal_the_names_read_by_each_env_emitter(self) -> None:
        leftover = inputs(secrets={**SECRETS, "proxy_auth": "proxy-user:proxy-password"})
        cases = (
            (inputs(), OwuiEnvArtifact()),
            (inputs(SECOND), OwuiEnvArtifact()),
            (inputs(no_gpu=True), OwuiEnvArtifact()),
            (leftover, OwuiEnvArtifact()),
            (inputs(), SearxngEnvArtifact()),
            (inputs(SECOND), SearxngEnvArtifact()),
            (inputs(no_gpu=True), SearxngEnvArtifact()),
            (leftover, SearxngEnvArtifact()),
        )
        for base, artifact in cases:
            with self.subTest(artifact=artifact.name, site=base.site.hostname, no_gpu=base.no_gpu):
                recorder = RecordingSecrets(cast(Mapping[str, str], base.secrets))
                rendered = replace(base, secrets=recorder)
                declared = artifact.secret_names(base)
                artifact.emit(rendered)
                self.assertEqual(set(recorder.reads), set(declared))
