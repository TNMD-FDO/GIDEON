"""Hold Compose services and their self-update switches to the registry.

The table is the suite's one literal naming of Compose services, held in order
to the registry. When a service is added, add its self-update switches to the
table, or an empty entry with a comment saying the image has none. Future
entries must cover Grafana's ``GF_ANALYTICS_CHECK_FOR_UPDATES`` and
``GF_ANALYTICS_REPORTING_ENABLED``, and Loki's ``analytics.reporting_enabled``
(a config-file switch, which will need a file form of the table).
"""

import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml  # type: ignore[import-untyped]

from gideon.host.render.services import ServiceDefinition, all_service_names

SELF_UPDATE_SWITCHES: Mapping[str, Mapping[str, str]] = {
    # Caddy 2 has no update check or telemetry.
    "caddy": {},
    # Prometheus has no update check or telemetry.
    "prometheus": {},
    # Node exporter has no update check or telemetry.
    "node-exporter": {},
    # Grafana's update checks and reporting are off; plugins are neither
    # installed nor refreshed by the image. Signed-in actions cannot trigger
    # Gravatar, external snapshot, or catalog install requests, and the browser's
    # news feed is off; the catalog listing's proxy stays, as the runbook records.
    "grafana": {
        "GF_ANALYTICS_REPORTING_ENABLED": "false",
        "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
        "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES": "false",
        "GF_PLUGINS_PREINSTALL_DISABLED": "true",
        "GF_PLUGINS_PUBLIC_KEY_RETRIEVAL_DISABLED": "true",
        "GF_SECURITY_DISABLE_GRAVATAR": "true",
        "GF_SNAPSHOTS_EXTERNAL_ENABLED": "false",
        "GF_PLUGINS_PLUGIN_ADMIN_ENABLED": "false",
        "GF_NEWS_NEWS_FEED_ENABLED": "false",
    },
    # PostgreSQL has no update check or telemetry.
    "postgres": {},
    # Qdrant has no update check; its usage report and dashboard are off.
    "qdrant": {
        "QDRANT__TELEMETRY_DISABLED": "true",
        "QDRANT__SERVICE__ENABLE_STATIC_CONTENT": "false",
    },
    # OpenSearch's image has no update check; nothing in it updates itself.
    "opensearch": {},
    "open-webui": {"ENABLE_VERSION_UPDATE_CHECK": "false"},
    "gideon-generator": {
        "VLLM_NO_USAGE_STATS": "1",
        "DO_NOT_TRACK": "1",
    },
    # The API image has no update check or telemetry.
    "gideon-api": {},
    # No update check or telemetry at the pinned commit; no checker module,
    # and the two user-triggered outbound resolvers are off in its settings.
    "searxng": {},
    # The DCGM exporter has no update check or telemetry.
    "dcgm-exporter": {},
    # The PostgreSQL exporter has no update check or telemetry.
    "postgres-exporter": {},
    # cAdvisor has no update check or telemetry.
    "cadvisor": {},
    # Blackbox exporter has no update check or telemetry.
    "blackbox-exporter": {},
}

ROOT = Path(__file__).resolve().parent.parent


def check_self_update_switches(project: Mapping[str, Any]) -> list[str]:
    """Return registry or switch violations in a loaded Compose project."""

    services = project.get("services")
    if not isinstance(services, Mapping):
        return ["project has no services mapping"]
    errors: list[str] = []
    for name, service in services.items():
        if name not in SELF_UPDATE_SWITCHES:
            errors.append(
                f"{name}: declare its self-update switches in "
                "SELF_UPDATE_SWITCHES or record that it has none"
            )
            continue
        if not isinstance(service, Mapping):
            errors.append(f"{name}: service is not a mapping")
            continue
        environment = service.get("environment", {})
        if not isinstance(environment, Mapping):
            environment = {}
        for switch, expected in SELF_UPDATE_SWITCHES[name].items():
            if switch not in environment:
                errors.append(
                    f"{name}: missing {switch} in SELF_UPDATE_SWITCHES contract"
                )
            elif environment[switch] != expected:
                errors.append(
                    f"{name}: {switch} must be {expected!r} in the "
                    "SELF_UPDATE_SWITCHES registry contract"
                )
    return errors


def check_self_update_service_names(registry_names: tuple[str, ...]) -> list[str]:
    """Return differences between the table and the service registry."""

    table_names = tuple(SELF_UPDATE_SWITCHES)
    table_set = set(table_names)
    registry_set = set(registry_names)
    errors = [
        f"{name}: missing from SELF_UPDATE_SWITCHES; declare its switches or "
        "add an empty entry with a comment saying the image has none"
        for name in registry_names
        if name not in table_set
    ]
    errors.extend(
        f"{name}: extra in SELF_UPDATE_SWITCHES; remove its entry"
        for name in table_names
        if name not in registry_set
    )
    if not errors:
        for position, (table_name, registry_name) in enumerate(
            zip(table_names, registry_names, strict=True), start=1
        ):
            if table_name != registry_name:
                errors.append(
                    f"SELF_UPDATE_SWITCHES order differs at position {position}: "
                    f"{table_name} appears where {registry_name} belongs; "
                    "reorder SELF_UPDATE_SWITCHES to match the service registry"
                )
                break
    return errors


class SelfUpdateContracts(unittest.TestCase):
    def test_rendered_projects_have_registered_switches(self) -> None:
        fixtures = sorted((ROOT / "tests/fixtures/render").glob("*/compose.yaml"))
        self.assertTrue(fixtures)
        for path in fixtures:
            with self.subTest(path=path):
                project = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.assertEqual(check_self_update_switches(project), [])

    def test_unregistered_service_names_the_registry(self) -> None:
        project: dict[str, Any] = {"services": {"new-service": {"environment": {}}}}
        errors = check_self_update_switches(project)
        self.assertEqual(len(errors), 1)
        self.assertIn("SELF_UPDATE_SWITCHES", errors[0])

    def test_missing_or_enabled_frontend_switch_names_the_registry(self) -> None:
        missing: dict[str, Any] = {"services": {"open-webui": {"environment": {}}}}
        enabled: dict[str, Any] = {
            "services": {
                "open-webui": {"environment": {"ENABLE_VERSION_UPDATE_CHECK": "true"}}
            }
        }
        for project in (missing, enabled):
            with self.subTest(project=project):
                errors = check_self_update_switches(project)
                self.assertEqual(len(errors), 1)
                self.assertIn("SELF_UPDATE_SWITCHES", errors[0])

    def test_table_names_match_registry(self) -> None:
        self.assertEqual(check_self_update_service_names(all_service_names()), [])

    def test_added_service_names_the_table(self) -> None:
        services = self._stub_services()
        with patch("gideon.host.render.services.SERVICES", services):
            self.assertEqual(check_self_update_service_names(all_service_names()), [])
            services.append(self._stub_service("fictitious-new-service"))
            errors = check_self_update_service_names(all_service_names())
        self.assertEqual(len(errors), 1)
        self.assertIn("fictitious-new-service", errors[0])
        self.assertIn("SELF_UPDATE_SWITCHES", errors[0])
        self.assertIn("declare its switches", errors[0])

    def test_removed_service_names_the_table(self) -> None:
        services = self._stub_services()
        with patch("gideon.host.render.services.SERVICES", services):
            self.assertEqual(check_self_update_service_names(all_service_names()), [])
            removed = services.pop()
            errors = check_self_update_service_names(all_service_names())
        self.assertEqual(len(errors), 1)
        self.assertIn(removed.name, errors[0])
        self.assertIn("SELF_UPDATE_SWITCHES", errors[0])
        self.assertIn("remove its entry", errors[0])

    def test_swapped_services_name_the_first_position(self) -> None:
        services = self._stub_services()
        with patch("gideon.host.render.services.SERVICES", services):
            self.assertEqual(check_self_update_service_names(all_service_names()), [])
            services[0], services[1] = services[1], services[0]
            errors = check_self_update_service_names(all_service_names())
        self.assertEqual(len(errors), 1)
        self.assertIn("SELF_UPDATE_SWITCHES", errors[0])
        self.assertIn("position 1", errors[0])
        self.assertIn("reorder", errors[0])

    @staticmethod
    def _stub_service(name: str) -> ServiceDefinition:
        service = ServiceDefinition()
        service.name = name
        return service

    @classmethod
    def _stub_services(cls) -> list[ServiceDefinition]:
        return [cls._stub_service(name) for name in SELF_UPDATE_SWITCHES]


if __name__ == "__main__":
    unittest.main()
