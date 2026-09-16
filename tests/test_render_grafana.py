"""Grafana's LDAP, datasource, provider, and Overview render contracts."""

import json
import re
import tomllib
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from gideon.host.images import load_image_lock
from gideon.host.lock import load_host_lock, load_host_lock_text
from gideon.host.models import HardwareProfile, load_models_lock, select_profile
from gideon.host.render import ARTIFACTS, RenderInputs, VerbatimArtifact, render_all
from gideon.host.render.engine import ENGINE_JOB_NAME
from gideon.host.render.facts import HostFacts
from gideon.host.render.grafana import (
    DASHBOARDS_MOUNT,
    DATASOURCES_TEMPLATE,
    DRILL_MAX_GAP_DAYS,
    OVERVIEW_TEMPLATE,
    GrafanaContactPointsArtifact,
    GrafanaDashboardsProviderArtifact,
    GrafanaDatasourcesArtifact,
    GrafanaGpuArtifact,
    GrafanaLdapArtifact,
    GrafanaOverviewArtifact,
    GrafanaPoliciesArtifact,
    GrafanaRulesArtifact,
    GrafanaTimeIntervalsArtifact,
)
from gideon.host.render.searxng import search_enabled
from gideon.host.site import load_site

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config/site.example.yaml"
SECOND = ROOT / "tests/fixtures/site/second-office.yaml"
FULL_DN = ROOT / "tests/fixtures/site/dn-groups.yaml"
ESCAPED_DN = ROOT / "tests/fixtures/site/dn-groups-escaping.yaml"


def inputs(site_path: Path = EXAMPLE, **overrides: object) -> RenderInputs:
    site = load_site(site_path).config
    lock = load_host_lock(ROOT / "host.lock").lock
    images = load_image_lock(ROOT / "images.lock").lock
    models = load_models_lock(ROOT / "models.lock").lock
    assert site is not None and lock is not None and images is not None and models is not None
    profile = select_profile(models, site.hardware_profile)
    assert isinstance(profile, HardwareProfile)
    templates = {
        path: (ROOT / "compose" / path).read_text(encoding="utf-8")
        for artifact in ARTIFACTS
        for path in artifact.template_paths
    }
    base = RenderInputs(
        site=site,
        lock=lock,
        images=images,
        facts=HostFacts(("GPU-fictitious",), service_gid=4242),
        profile=profile,
        templates=templates,
        release="fixture",
        secrets={},
        checkout="/opt/gideon",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


class Ldap(unittest.TestCase):
    def test_both_render_sites_have_one_admin_mapping_and_no_password(self) -> None:
        for site_path in (EXAMPLE, SECOND, FULL_DN):
            with self.subTest(site=site_path.name):
                text = GrafanaLdapArtifact().emit(inputs(site_path))
                document = tomllib.loads(text)
                server = document["servers"][0]
                ldap = load_site(site_path).config
                assert ldap is not None
                config = ldap.auth.ldap
                self.assertEqual(server["host"], config.host)
                self.assertEqual(server["port"], config.port)
                self.assertTrue(server["use_ssl"])
                self.assertEqual(server["root_ca_cert"], "/etc/gideon/ca.pem")
                self.assertEqual(server["bind_dn"], f"{config.bind_user}@{config.host}")
                self.assertEqual(
                    server["bind_password"],
                    "$__file{/run/secrets/ldap_bind_password}",
                )
                self.assertNotIn("password", text.replace("bind_password", ""))
                self.assertEqual(server["search_base_dns"], [config.search_base])
                self.assertEqual(
                    server["attributes"],
                    {
                        "username": "sAMAccountName",
                        "email": "userPrincipalName",
                        "member_of": "memberOf",
                    },
                )
                self.assertEqual(
                    server["group_mappings"],
                    [
                        {
                            "group_dn": config.admins_group_dn,
                            "org_role": "Admin",
                            "grafana_admin": True,
                        }
                    ],
                )

    def test_the_full_dn_site_preserves_its_admins_group(self) -> None:
        text = GrafanaLdapArtifact().emit(inputs(FULL_DN))
        self.assertIn(
            'group_dn = "CN=GIDEON-Admins,OU=Security Groups,DC=ad,DC=test"',
            text,
        )

    def test_escaped_dn_values_round_trip_as_toml_strings(self) -> None:
        text = GrafanaLdapArtifact().emit(inputs(ESCAPED_DN))
        document = tomllib.loads(text)
        group_dn = document["servers"][0]["group_mappings"][0]["group_dn"]
        self.assertEqual(
            group_dn,
            'CN=Last\\, "First",OU=Security Groups,DC=ad,DC=test',
        )


class Provisioning(unittest.TestCase):
    def test_datasources_have_fixed_uids_and_file_password(self) -> None:
        document = yaml.safe_load(GrafanaDatasourcesArtifact().emit(inputs()))
        datasources = document["datasources"]
        self.assertEqual([item["uid"] for item in datasources], ["prometheus", "gideon-rows"])
        self.assertTrue(all(item["editable"] is False for item in datasources))
        self.assertEqual(datasources[0]["url"], "http://prometheus:9090")
        self.assertEqual(datasources[1]["user"], "gideon_ro_metrics")
        self.assertEqual(
            datasources[1]["secureJsonData"]["password"],
            "$__file{/run/secrets/postgres_gideon_ro_metrics_password}",
        )

    def test_dashboard_provider_is_a_non_editable_file_provider(self) -> None:
        text = GrafanaDashboardsProviderArtifact().emit(inputs())
        self.assertIn("disableDeletion: false", text)
        document = yaml.safe_load(text)
        provider = document["providers"][0]
        self.assertEqual(provider["folder"], "GIDEON")
        self.assertFalse(provider["allowUiUpdates"])
        self.assertEqual(provider["updateIntervalSeconds"], 5)
        self.assertEqual(provider["options"]["path"], DASHBOARDS_MOUNT)


def _datasource_uids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        datasource = value.get("datasource")
        if isinstance(datasource, dict) and isinstance(datasource.get("uid"), str):
            found.add(datasource["uid"])
        for child in value.values():
            found.update(_datasource_uids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_datasource_uids(child))
    return found


class Overview(unittest.TestCase):
    def test_overview_is_verbatim_grafana_13_content_with_declared_datasources(self) -> None:
        text = GrafanaOverviewArtifact.emit(inputs())
        self.assertEqual(text, inputs().templates[OVERVIEW_TEMPLATE])
        dashboard = json.loads(text)
        self.assertEqual(dashboard["schemaVersion"], 41)
        self.assertEqual(dashboard["uid"], "gideon-overview")
        self.assertNotIn("id", dashboard)
        self.assertEqual(
            {panel["title"] for panel in dashboard["panels"]},
            {
                "Services and probes",
                "Filesystems free",
                "Backup set age",
                "Backup push age",
                "Last drill result",
                "GPU utilisation",
                "GPU memory",
                "Host load",
                "Host memory",
                "Container memory",
                "Guardrail trips",
                "Last eval trip",
            },
        )
        self.assertTrue(all("id" not in panel for panel in dashboard["panels"]))
        declared = {
            item["uid"]
            for item in yaml.safe_load(inputs().templates[DATASOURCES_TEMPLATE])["datasources"]
        }
        self.assertTrue(_datasource_uids(dashboard) <= declared)

    def test_filesystem_panel_has_four_mountpoint_targets_and_shared_threshold(self) -> None:
        """Spec §19.5: the Overview filesystem panel follows the page rule."""

        dashboard = json.loads(GrafanaOverviewArtifact.emit(inputs()))
        panel = next(panel for panel in dashboard["panels"] if panel["title"] == "Filesystems free")
        targets = panel["targets"]
        expected_mountpoints = {"/", "/var/lib/docker", "/data", "/data/fast"}
        self.assertEqual(len(targets), 4)
        self.assertEqual({target["legendFormat"] for target in targets}, expected_mountpoints)
        expression_pattern = re.compile(
            r'100 \* node_filesystem_avail_bytes\{mountpoint="([^"]+)"\}'
            r' / node_filesystem_size_bytes\{mountpoint="([^"]+)"\}'
        )
        for target in targets:
            with self.subTest(mountpoint=target["legendFormat"]):
                expression = expression_pattern.fullmatch(target["expr"])
                self.assertIsNotNone(expression)
                assert expression is not None
                self.assertEqual(expression.groups(), (target["legendFormat"], target["legendFormat"]))

        rules = yaml.safe_load(GrafanaRulesArtifact().emit(inputs()))
        data_rule = next(
            rule
            for group in rules["groups"]
            for rule in group["rules"]
            if rule["uid"] == "gideon-data-volume-low"
        )
        data_threshold = next(
            item
            for item in data_rule["data"]
            if item["refId"] == data_rule["condition"]
        )["model"]["conditions"][0]["evaluator"]["params"][0]
        green_step = next(
            step
            for step in panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
            if step["color"] == "green"
        )
        self.assertEqual(panel["fieldConfig"]["defaults"]["unit"], "percent")
        self.assertEqual(green_step["value"], data_threshold)

    def test_container_memory_compares_working_set_with_positive_limits(self) -> None:
        dashboard = json.loads(GrafanaOverviewArtifact.emit(inputs()))
        panel = next(panel for panel in dashboard["panels"] if panel["title"] == "Container memory")
        targets = {target["refId"]: target for target in panel["targets"]}
        self.assertEqual(set(targets), {"A", "B"})
        self.assertEqual(
            targets["A"]["expr"],
            'sum by (name) (container_memory_working_set_bytes{name!=""})',
        )
        self.assertIn('container_spec_memory_limit_bytes{name!=""}', targets["B"]["expr"])
        self.assertIn("> 0", targets["B"]["expr"])
        self.assertTrue(targets["B"]["legendFormat"].endswith(" limit"))
        overrides = panel["fieldConfig"]["overrides"]
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0]["matcher"], {"id": "byRegexp", "options": "/ limit$/"})
        self.assertEqual(
            overrides[0]["properties"],
            [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}],
        )

    def test_trip_panels_use_the_metrics_datasource_and_source_filters(self) -> None:
        dashboard = json.loads(GrafanaOverviewArtifact.emit(inputs()))
        panels = {panel["title"]: panel for panel in dashboard["panels"]}
        for title, source in (("Guardrail trips", "user"), ("Last eval trip", "eval")):
            with self.subTest(panel=title):
                panel = panels[title]
                target = panel["targets"][0]
                self.assertEqual(panel["datasource"]["uid"], "gideon-rows")
                self.assertIn("guardrail_trips", target["rawSql"])
                self.assertIn(f"source = '{source}'", target["rawSql"])
        # Day buckets sit at midnight, outside the board's six-hour default
        # range after 06:00; the count panel carries its own thirty-day range.
        self.assertEqual(panels["Guardrail trips"]["timeFrom"], "30d")

    def test_verbatim_artifact_emits_its_template_without_expansion(self) -> None:
        artifact = VerbatimArtifact(
            name="example",
            relative_path="example.txt",
            template_path=OVERVIEW_TEMPLATE,
        )
        self.assertEqual(artifact.emit(inputs()), inputs().templates[OVERVIEW_TEMPLATE])


class Dashboards(unittest.TestCase):
    def test_every_dashboard_has_a_fixed_uid_and_declared_datasources(self) -> None:
        site_inputs = inputs()
        declared = {
            item["uid"]
            for item in yaml.safe_load(site_inputs.templates[DATASOURCES_TEMPLATE])["datasources"]
        }
        dashboard_dir = ROOT / "compose/grafana/dashboards"
        paths = sorted(dashboard_dir.glob("*.json"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(dashboard=path.name):
                dashboard = json.loads(path.read_text(encoding="utf-8"))
                self.assertIsInstance(dashboard.get("uid"), str)
                self.assertNotIn("id", dashboard)
                self.assertTrue(_datasource_uids(dashboard) <= declared)


class Alerting(unittest.TestCase):
    def test_contact_point_uses_each_sites_recipients_and_subject(self) -> None:
        for site_path in (EXAMPLE, SECOND):
            with self.subTest(site=site_path.name):
                site_inputs = inputs(site_path)
                document = yaml.safe_load(GrafanaContactPointsArtifact().emit(site_inputs))
                contact = document["contactPoints"][0]
                receiver = contact["receivers"][0]
                self.assertEqual(contact["name"], "page")
                self.assertEqual(receiver["uid"], "page-email")
                self.assertEqual(receiver["type"], "email")
                self.assertEqual(
                    receiver["settings"]["addresses"],
                    ";".join(site_inputs.site.alerts.recipients),
                )
                self.assertTrue(receiver["settings"]["singleEmail"])
                self.assertEqual(
                    receiver["settings"]["subject"],
                    f"[GIDEON {site_inputs.site.office.short_name}] {{{{ .Status | toUpper }}}}: {{{{ .CommonLabels.alertname }}}}",
                )

    def test_policy_and_time_interval_use_the_site_timezone(self) -> None:
        for site_path in (EXAMPLE, SECOND):
            with self.subTest(site=site_path.name):
                site_inputs = inputs(site_path)
                policy = yaml.safe_load(GrafanaPoliciesArtifact().emit(site_inputs))["policies"][0]
                child = policy["routes"][0]
                self.assertEqual(policy["receiver"], "page")
                self.assertEqual(policy["group_by"], ["alertname"])
                self.assertEqual(child["repeat_interval"], "6d")
                self.assertEqual(child["active_time_intervals"], ["saturday-morning"])
                self.assertFalse(child["continue"])
                # `muteTimes` is the provisioning key for time intervals; a
                # route uses one as an active window, not only as a mute.
                interval = yaml.safe_load(GrafanaTimeIntervalsArtifact().emit(site_inputs))["muteTimes"][0]
                entry = interval["time_intervals"][0]
                self.assertEqual(entry["weekdays"], ["saturday"])
                self.assertEqual(entry["times"], [{"start_time": "08:00", "end_time": "09:00"}])
                self.assertEqual(entry["location"], site_inputs.site.office.timezone)

    def test_rules_have_expected_groups_thresholds_and_states(self) -> None:
        expected_uids = {
            "gideon-backup-set-overdue",
            "gideon-push-overdue",
            "gideon-drill-overdue",
            "gideon-drill-failed",
            "gideon-backup-unit-failed",
            "gideon-target-down",
            "gideon-postgres-down",
            "gideon-ingress-probe-failing",
            "gideon-frontend-probe-failing",
            "gideon-systemd-collector-failed",
            "gideon-data-volume-low",
            "gideon-host-filesystem-low",
            "gideon-tls-expiring",
            "gideon-driver-drift",
            "gideon-engine-down",
            "gideon-heartbeat",
        }
        for site_path, build_box in (
            (EXAMPLE, False),
            (EXAMPLE, True),
            (SECOND, False),
            (SECOND, True),
        ):
            with self.subTest(site=site_path.name, build_box=build_box):
                site_inputs = inputs(site_path, build_box=build_box)
                document = yaml.safe_load(GrafanaRulesArtifact().emit(site_inputs))
                groups = document["groups"]
                self.assertEqual([group["name"] for group in groups], ["rows", "metrics"])
                rules = {rule["uid"]: rule for group in groups for rule in group["rules"]}
                # The search probe's rule exists only where SearXNG does (web.search on).
                search_rule = {"gideon-search-probe-failing"} if search_enabled(site_inputs) else set()
                expected_host_rule = {"gideon-host-unit-inactive"} if build_box else set()
                self.assertEqual(set(rules), expected_uids | expected_host_rule | search_rule)
                if search_rule:
                    self.assertIn('probe_success{job="search"}', rules["gideon-search-probe-failing"]["data"][0]["model"]["expr"])
                for rule in rules.values():
                    self.assertIn(rule["condition"], {item["refId"] for item in rule["data"]})
                    self.assertFalse(rule["isPaused"])
                    self.assertEqual(rule["labels"]["class"], "page")
                    self.assertIn("docs/runbooks/observability.md §4", rule["annotations"]["runbook"])
                    for item in rule["data"]:
                        self.assertIn(item["datasourceUid"], {"prometheus", "gideon-rows", "__expr__"})
                self.assertEqual(
                    rules["gideon-drill-overdue"]["data"][-1]["model"]["conditions"][0]["evaluator"]["params"][0],
                    DRILL_MAX_GAP_DAYS[site_inputs.site.backup.drill_interval] * 86400
                    + 259200,
                )
                for uid in (
                    "gideon-target-down",
                    "gideon-postgres-down",
                    "gideon-ingress-probe-failing",
                    "gideon-frontend-probe-failing",
                ):
                    self.assertEqual(rules[uid]["for"], "10m")
                if build_box:
                    self.assertEqual(rules["gideon-host-unit-inactive"]["for"], "10m")
                # Silence is a page only through target-down: a scrape target
                # that vanishes (or a Prometheus that cannot answer) is that
                # rule's condition, and every other metric rule's no-data case
                # is one of those targets being gone.
                self.assertEqual(rules["gideon-target-down"]["noDataState"], "Alerting")
                self.assertEqual(rules["gideon-target-down"]["execErrState"], "Alerting")
                # The engine's own page: five minutes clears the measured
                # plain-restart and cold-recreate timings and is half the
                # core-service window; Target down leaves the engine to it.
                engine_rule = rules["gideon-engine-down"]
                self.assertEqual(engine_rule["for"], "5m")
                self.assertEqual(engine_rule["noDataState"], "OK")
                self.assertEqual(engine_rule["execErrState"], "OK")
                self.assertEqual(
                    next(
                        group["name"]
                        for group in groups
                        if engine_rule in group["rules"]
                    ),
                    "metrics",
                )
                self.assertEqual(
                    engine_rule["data"][0]["model"]["expr"],
                    f'up{{job="{ENGINE_JOB_NAME}"}} == bool 0',
                )
                self.assertIn(
                    f'up{{job!="{ENGINE_JOB_NAME}"}}',
                    rules["gideon-target-down"]["data"][0]["model"]["expr"],
                )
                # The one blind spot silence would open — the node exporter up
                # while its systemd collector fails — is its own ten-minute page.
                self.assertEqual(rules["gideon-systemd-collector-failed"]["for"], "10m")
                self.assertIn(
                    'node_scrape_collector_success{collector="systemd"}',
                    rules["gideon-systemd-collector-failed"]["data"][0]["model"]["expr"],
                )
                for uid in (
                    "gideon-backup-unit-failed",
                    "gideon-postgres-down",
                    "gideon-ingress-probe-failing",
                    "gideon-frontend-probe-failing",
                    "gideon-systemd-collector-failed",
                    "gideon-data-volume-low",
                    "gideon-host-filesystem-low",
                    "gideon-tls-expiring",
                    "gideon-heartbeat",
                ):
                    self.assertEqual(rules[uid]["noDataState"], "OK")
                    self.assertEqual(rules[uid]["execErrState"], "OK")
                if build_box:
                    self.assertEqual(rules["gideon-host-unit-inactive"]["noDataState"], "OK")
                    self.assertEqual(rules["gideon-host-unit-inactive"]["execErrState"], "OK")
                for uid in (
                    "gideon-backup-set-overdue",
                    "gideon-push-overdue",
                    "gideon-drill-overdue",
                    "gideon-drill-failed",
                ):
                    self.assertEqual(rules[uid]["noDataState"], "Alerting")
                    self.assertEqual(rules[uid]["execErrState"], "Alerting")

    def test_host_filesystem_rule_covers_both_mountpoints_and_reuses_data_threshold(self) -> None:
        """Spec §19.5: each rendered host filesystem page has its exact contract."""

        for site_path, no_gpu in ((EXAMPLE, False), (SECOND, False), (EXAMPLE, True)):
            with self.subTest(site=site_path.name, no_gpu=no_gpu):
                site_inputs = inputs(site_path, no_gpu=no_gpu)
                document = yaml.safe_load(GrafanaRulesArtifact().emit(site_inputs))
                groups = document["groups"]
                rule = next(
                    rule
                    for group in groups
                    for rule in group["rules"]
                    if rule["uid"] == "gideon-host-filesystem-low"
                )
                metrics_group = next(group for group in groups if group["name"] == "metrics")
                self.assertIn(rule, metrics_group["rules"])
                self.assertEqual(rule["condition"], "B")
                self.assertEqual(rule["for"], "0s")
                query = next(item for item in rule["data"] if item["refId"] == "A")
                self.assertTrue(query["model"]["instant"])
                expression = re.fullmatch(
                    r'100 \* node_filesystem_avail_bytes\{mountpoint=~"([^"]+)"\}'
                    r' / node_filesystem_size_bytes\{mountpoint=~"([^"]+)"\}',
                    query["model"]["expr"],
                )
                self.assertIsNotNone(expression)
                assert expression is not None
                expected_mountpoints = {"/", "/var/lib/docker"}
                for matcher in expression.groups():
                    mountpoints = matcher.split("|")
                    self.assertEqual(len(mountpoints), len(expected_mountpoints))
                    self.assertEqual(set(mountpoints), expected_mountpoints)

                data_rule = next(
                    rule
                    for group in groups
                    for rule in group["rules"]
                    if rule["uid"] == "gideon-data-volume-low"
                )
                data_evaluator = next(
                    item
                    for item in data_rule["data"]
                    if item["refId"] == data_rule["condition"]
                )["model"]["conditions"][0]["evaluator"]
                filesystem_evaluator = next(
                    item
                    for item in rule["data"]
                    if item["refId"] == rule["condition"]
                )["model"]["conditions"][0]["evaluator"]
                self.assertEqual(filesystem_evaluator["type"], "lt")
                self.assertEqual(filesystem_evaluator["params"], data_evaluator["params"])
                self.assertEqual(
                    rule["annotations"]["summary"],
                    "The filesystem {{ $labels.mountpoint }} has less than 15 percent free",
                )
                self.assertEqual(rule["annotations"]["runbook"], "docs/runbooks/observability.md §4")
                self.assertEqual(rule["labels"]["class"], "page")

    def test_driver_drift_is_rendered_only_for_a_tested_fictitious_lock(self) -> None:
        lock_text = (ROOT / "host.lock").read_text(encoding="utf-8")
        fake_text = re.sub(
            r"(?m)^  tested:.*$", '  tested: "1000.0.0"', lock_text, count=1
        )
        fake_result = load_host_lock_text(fake_text)
        self.assertTrue(fake_result.ok, fake_result.errors)
        assert fake_result.lock is not None
        with_driver = yaml.safe_load(
            GrafanaRulesArtifact().emit(inputs(lock=fake_result.lock))
        )
        with_driver_rules = {
            rule["uid"]
            for group in with_driver["groups"]
            for rule in group["rules"]
        }
        self.assertIn("gideon-driver-drift", with_driver_rules)
        self.assertIn("gideon-engine-down", with_driver_rules)
        self.assertIn("1000.0.0", GrafanaRulesArtifact().emit(inputs(lock=fake_result.lock)))

        without_driver = yaml.safe_load(
            GrafanaRulesArtifact().emit(inputs(lock=load_host_lock_text(
                fake_text.replace('tested: "1000.0.0"', "tested: null")
            ).lock))
        )
        without_driver_rules = {
            rule["uid"]
            for group in without_driver["groups"]
            for rule in group["rules"]
        }
        self.assertNotIn("gideon-driver-drift", without_driver_rules)
        self.assertIn("gideon-engine-down", without_driver_rules)

        no_gpu_rules = yaml.safe_load(
            GrafanaRulesArtifact().emit(inputs(lock=fake_result.lock, no_gpu=True))
        )
        no_gpu_rule_uids = {
            rule["uid"]
            for group in no_gpu_rules["groups"]
            for rule in group["rules"]
        }
        self.assertNotIn("gideon-driver-drift", no_gpu_rule_uids)
        self.assertNotIn("gideon-engine-down", no_gpu_rule_uids)
        no_gpu_target_down = next(
            rule
            for group in no_gpu_rules["groups"]
            for rule in group["rules"]
            if rule["uid"] == "gideon-target-down"
        )
        self.assertIn(
            f'up{{job!="{ENGINE_JOB_NAME}"}}',
            no_gpu_target_down["data"][0]["model"]["expr"],
        )

    def test_gpu_board_has_engine_metrics_panels(self) -> None:
        dashboard = json.loads(
            (ROOT / "compose/grafana/dashboards/gpu.json").read_text(encoding="utf-8")
        )
        panels = {panel["title"]: panel for panel in dashboard["panels"]}
        self.assertIn("KV-cache usage", panels)
        self.assertIn("Queue depth", panels)
        self.assertEqual(
            panels["KV-cache usage"]["fieldConfig"]["defaults"]["unit"],
            "percentunit",
        )
        self.assertEqual(panels["KV-cache usage"]["fieldConfig"]["defaults"]["min"], 0)
        self.assertEqual(panels["KV-cache usage"]["fieldConfig"]["defaults"]["max"], 1)
        self.assertEqual(
            {target["expr"] for target in panels["KV-cache usage"]["targets"]}
            | {target["expr"] for target in panels["Queue depth"]["targets"]},
            {
                "vllm:kv_cache_usage_perc",
                "vllm:num_requests_waiting",
                "vllm:num_requests_running",
            },
        )
        for title in ("KV-cache usage", "Queue depth"):
            with self.subTest(panel=title):
                panel = panels[title]
                self.assertNotIn("id", panel)
                self.assertEqual(panel["type"], "timeseries")
                self.assertEqual(panel["datasource"]["uid"], "prometheus")
                for target in panel["targets"]:
                    self.assertEqual(target["datasource"]["uid"], "prometheus")

    def test_gpu_board_is_not_applicable_in_no_gpu_mode(self) -> None:
        self.assertTrue(GrafanaGpuArtifact.applies(inputs()))
        self.assertFalse(GrafanaGpuArtifact.applies(inputs(no_gpu=True)))
        rendered_paths = {
            item.relative_path
            for item in render_all(
                inputs(
                    no_gpu=True,
                    secrets={
                        "ldap_bind_password": "bind",
                        "postgres_openwebui_password": "postgres",
                        "gideon_admin_password": "admin",
                        "searxng_secret_key": "searxng",
                    },
                )
            ).files
        }
        self.assertNotIn(GrafanaGpuArtifact.relative_path, rendered_paths)

    def test_alerting_templates_are_all_yaml_documents(self) -> None:
        for artifact in (
            GrafanaContactPointsArtifact(),
            GrafanaPoliciesArtifact(),
            GrafanaTimeIntervalsArtifact(),
            GrafanaRulesArtifact(),
        ):
            with self.subTest(path=artifact.relative_path):
                self.assertIsNotNone(yaml.safe_load(artifact.emit(inputs())))


if __name__ == "__main__":
    unittest.main()


class DrillIntervalTable(unittest.TestCase):
    def test_interval_seconds_cover_exactly_the_calendar_table(self) -> None:
        # The overdue rule's threshold and the drill timer read the same site
        # key; the two tables must accept the same values, derived here.
        from gideon.host.render.systemd import DRILL_CALENDAR

        self.assertEqual(set(DRILL_MAX_GAP_DAYS), set(DRILL_CALENDAR))

    def test_gpu_driver_version_uses_the_exporters_field_label(self) -> None:
        dashboard = json.loads(
            (ROOT / "compose/grafana/dashboards/gpu.json").read_text(encoding="utf-8")
        )
        driver_panel = next(panel for panel in dashboard["panels"] if panel["title"] == "Driver version")
        self.assertIn("DCGM_FI_DRIVER_VERSION", driver_panel["targets"][0]["expr"])
