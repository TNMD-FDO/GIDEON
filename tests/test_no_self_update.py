"""Rendered services must not check for their own updates.

The registry grows with each image added by a later slice.  Future entries
must cover Qdrant's
``QDRANT__TELEMETRY_DISABLED``, Grafana's ``GF_ANALYTICS_CHECK_FOR_UPDATES``
and ``GF_ANALYTICS_REPORTING_ENABLED``, and Loki's
``analytics.reporting_enabled`` (a config-file switch, which will need a file
form of this registry).
"""

import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

SELF_UPDATE_SWITCHES: Mapping[str, Mapping[str, str]] = {
    "open-webui": {"ENABLE_VERSION_UPDATE_CHECK": "false"},
    "gideon-generator": {
        "VLLM_NO_USAGE_STATS": "1",
        "DO_NOT_TRACK": "1",
    },
    # Caddy 2 has no update check or telemetry.
    "caddy": {},
    # PostgreSQL has no update check or telemetry.
    "postgres": {},
    # No update check or telemetry at the pinned commit; no checker module,
    # and the two user-triggered outbound resolvers are off in its settings.
    "searxng": {},
    # Grafana's update checks and reporting are disabled in its environment.
    "grafana": {
        "GF_ANALYTICS_REPORTING_ENABLED": "false",
        "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
        "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES": "false",
    },
    # Prometheus has no update check or telemetry.
    "prometheus": {},
    # Node exporter has no update check or telemetry.
    "node-exporter": {},
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


if __name__ == "__main__":
    unittest.main()
