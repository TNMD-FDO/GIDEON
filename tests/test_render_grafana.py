"""Grafana configuration, alerting, and dashboard render contracts."""

import json
import re
import tomllib
import unittest
from collections import Counter
from dataclasses import dataclass, replace
from itertools import combinations, pairwise
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from gideon.evaluation.evalset import SHAPE_REGISTRY, TIER_2_CATEGORY
from gideon.evaluation.guardrails_slice import OVER_TRIP_DIVISOR
from gideon.evaluation.window import QUIET_WINDOW_END_HOUR
from gideon.host.images import load_image_lock
from gideon.host.lock import load_host_lock, load_host_lock_text
from gideon.host.models import HardwareProfile, load_models_lock, select_profile
from gideon.host.render import ARTIFACTS, RenderInputs, VerbatimArtifact, render_all
from gideon.host.render.api import API_JOB_NAME
from gideon.host.render.compose import service_blocks
from gideon.host.render.engine import ENGINE_JOB_NAME
from gideon.host.render.facts import HostFacts
from gideon.host.render.grafana import (
    BACKUP_PUSH_AGE_TITLE,
    BACKUP_SET_AGE_TITLE,
    BACKUP_TEMPLATE,
    CERTIFICATE_DAYS_LEFT_TITLE,
    DASHBOARDS_MOUNT,
    DATASOURCES_TEMPLATE,
    DRILL_MAX_GAP_DAYS,
    EVAL_TEMPLATE,
    FILESYSTEMS_FREE_TITLE,
    FRONT_DOOR_BOARDS,
    GRAFANA_SILENCES_ROUTE,
    GRAFANA_SUB_PATH,
    NIGHTLY_OVERDUE_SECONDS,
    OVERVIEW_TEMPLATE,
    PASSING_DRILL_AGE_TITLE,
    PLUGINS_TEMPLATE,
    PUBLIC_REPOSITORY_URL,
    START_HERE_CARD,
    START_HERE_TITLE,
    GrafanaBackupArtifact,
    GrafanaContactPointsArtifact,
    GrafanaDashboardsProviderArtifact,
    GrafanaDatasourcesArtifact,
    GrafanaEvalArtifact,
    GrafanaGpuArtifact,
    GrafanaLdapArtifact,
    GrafanaOverviewArtifact,
    GrafanaPluginsArtifact,
    GrafanaPoliciesArtifact,
    GrafanaRulesArtifact,
    GrafanaTimeIntervalsArtifact,
)
from gideon.host.render.prometheus import PrometheusConfigArtifact
from gideon.host.render.searxng import search_enabled
from gideon.host.render.services import declared_sources
from gideon.host.render.systemd import NIGHTLY_CALENDAR, NIGHTLY_SUITES
from gideon.host.site import FIELD_REGISTRY, load_site
from gideon.host.steps.command import INSTALL_HOME
from gideon.improvement import tally
from gideon.status import attention
from tools.boards.page import GRID_CELL_HEIGHT, GRID_CELL_MARGIN, VIEWPORT_WIDTH

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config/site.example.yaml"
SECOND = ROOT / "tests/fixtures/site/second-office.yaml"
FULL_DN = ROOT / "tests/fixtures/site/dn-groups.yaml"
ESCAPED_DN = ROOT / "tests/fixtures/site/dn-groups-escaping.yaml"

# The pinned Grafana's table panel and dashboard grid, read from its source;
# a Grafana pin bump re-reads these against the new tag.
PANEL_INNER_WIDTH_LOSS = 18  # a 1 px border and 8 px padding on each side
PANEL_VERTICAL_CHROME = 58  # the same border and padding, and a 40 px title bar
TABLE_HEADER_HEIGHT = 34  # a header row on one line
TABLE_ROW_HEIGHT = {"sm": 36, "md": 42, "lg": 48}  # a data row per cell height
TABLE_MIN_COLUMN_WIDTH = 150  # a column's minimum width when none is set
HEADER_TEXT_INSET = 13  # a header cell's 6 px padding each side and its right border
KIOSK_SIDE_PADDING = 32  # the kiosk page's 16 px padding on each side
GRID_COLUMNS = 24  # the dashboard grid's columns
HEADER_CHAR_WIDTH_BOUND = 9  # an upper bound per header character, not a glyph metric
GPU_AGGREGATED_FIELD = re.compile(r"max by \(gpu\) \((DCGM_FI_[A-Z0-9_]+)\)")


def table_whole_rows(panel: dict[str, Any]) -> int:
    """Count whole data rows inside a table card at the pinned Grafana layout."""

    cell_height = panel.get("options", {}).get("cellHeight", "sm")
    card_height = (GRID_CELL_HEIGHT + GRID_CELL_MARGIN) * panel["gridPos"]["h"] - GRID_CELL_MARGIN
    return (card_height - PANEL_VERTICAL_CHROME - TABLE_HEADER_HEIGHT) // TABLE_ROW_HEIGHT[cell_height]


def table_inner_width(panel: dict[str, Any]) -> int:
    """Resolve the table's available pixels in the board check's kiosk viewport."""

    grid_width = VIEWPORT_WIDTH - KIOSK_SIDE_PADDING
    column_width = (grid_width - GRID_CELL_MARGIN * (GRID_COLUMNS - 1)) / GRID_COLUMNS
    width = panel["gridPos"]["w"]
    return round(column_width * width + GRID_CELL_MARGIN * (width - 1)) - PANEL_INNER_WIDTH_LOSS


def column_overrides(panel: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map each by-name override's column to its property values."""

    return {
        override["matcher"]["options"]: {item["id"]: item["value"] for item in override["properties"]}
        for override in panel["fieldConfig"]["overrides"]
        if override["matcher"]["id"] == "byName"
    }


def table_column_widths(panel: dict[str, Any], columns: list[str]) -> dict[str, float]:
    """Apply Grafana's explicit widths and shared width floor to query columns."""

    defaults = panel["fieldConfig"]["defaults"].get("custom", {})
    properties = column_overrides(panel)
    explicit = {
        name: properties.get(name, {}).get("custom.width", defaults.get("width"))
        for name in columns
    }
    fixed = sum(value for value in explicit.values() if value)
    auto_count = sum(not value for value in explicit.values())
    shared = (table_inner_width(panel) - fixed) / auto_count if auto_count else 0
    return {
        name: float(value) if value else max(
            properties.get(name, {}).get("custom.minWidth", defaults.get("minWidth", TABLE_MIN_COLUMN_WIDTH)),
            shared,
        )
        for name, value in explicit.items()
    }


def _sql_top_level_tokens(sql: str) -> list[tuple[str, int, int]]:
    """Find words and commas outside strings, identifiers, and parentheses."""

    tokens = []
    depth = 0
    quote = ""
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 2
                    continue
                quote = ""
        elif char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and char == ",":
            tokens.append((char, index, index + 1))
        elif depth == 0 and (char.isalpha() or char == "_"):
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            tokens.append((sql[index:end].upper(), index, end))
            index = end
            continue
        index += 1
    return tokens


def outer_select_columns(sql: str) -> list[str]:
    """Read outermost SELECT names, using an alias or the final identifier."""

    tokens = _sql_top_level_tokens(sql)
    select = next((end for word, _, end in tokens if word == "SELECT"), None)
    assert select is not None
    source = next((start for word, start, _ in tokens if word == "FROM" and start > select), None)
    assert source is not None
    clause = sql[select:source]
    clause = re.sub(r"(?is)^\s*DISTINCT\s+ON\s*\([^)]*\)\s*", "", clause, count=1)
    commas = [start for word, start, _ in _sql_top_level_tokens(clause) if word == ","]
    boundaries = [-1, *commas, len(clause)]
    names = []
    for left, right in pairwise(boundaries):
        item = clause[left + 1:right].strip()
        alias = re.search(r"(?i)\bAS\s+([a-z_][a-z_0-9]*)\s*$", item)
        identifier = re.search(r"([a-z_][a-z_0-9]*)\s*$", item, re.IGNORECASE)
        match = alias or identifier
        assert match is not None, item
        names.append(match.group(1))
    return names


def dcgm_counter_rows() -> dict[str, tuple[str, str]]:
    """Read the release's active DCGM fields, types, and help text."""

    text = (ROOT / "compose/dcgm-exporter/counters.csv").read_text(encoding="utf-8")
    rows = {}
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            field, kind, help_text = (part.strip() for part in line.split(",", 2))
            rows[field] = (kind, help_text)
    return rows


def gpu_aggregated_field(expr: str) -> str:
    """Read a DCGM field grouped to draw one series per gpu."""

    match = GPU_AGGREGATED_FIELD.fullmatch(expr)
    if match is None:
        raise AssertionError(f"{expr!r} must draw one series per gpu")
    return match.group(1)


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
        facts=HostFacts(("GPU-fictitious-0", "GPU-fictitious-1"), service_gid=4242),
        profile=profile,
        templates=templates,
        release="fixture",
        secrets={},
        checkout="/opt/gideon",
        source_digests=dict.fromkeys(declared_sources(), "sha256:" + "0" * 64),
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
    def test_plugins_file_is_empty_and_inside_the_mounted_provisioning_tree(self) -> None:
        artifact = GrafanaPluginsArtifact
        template = (ROOT / "compose" / PLUGINS_TEMPLATE).read_text(encoding="utf-8")
        self.assertIn(artifact, ARTIFACTS)
        self.assertIsInstance(artifact, VerbatimArtifact)
        self.assertEqual(artifact.owners, ("grafana",))
        self.assertEqual(artifact.mode, 0o644)
        for no_gpu in (False, True):
            with self.subTest(no_gpu=no_gpu):
                site_inputs = inputs(no_gpu=no_gpu)
                self.assertTrue(artifact.applies(site_inputs))
                self.assertEqual(artifact.emit(site_inputs), template)
                self.assertEqual(
                    yaml.safe_load(artifact.emit(site_inputs)), {"apiVersion": 1, "apps": []}
                )
                grafana = service_blocks(site_inputs)["grafana"]
                assert isinstance(grafana, dict)
                mounts = [volume.split(":") for volume in grafana["volumes"]]
                source = next(
                    mount[0] for mount in mounts if mount[1] == "/etc/grafana/provisioning"
                )
                rendered_path = Path("/etc/gideon/rendered") / artifact.relative_path
                self.assertTrue(rendered_path.is_relative_to(source))

    def test_datasources_have_fixed_uids_and_file_password(self) -> None:
        document = yaml.safe_load(GrafanaDatasourcesArtifact().emit(inputs()))
        datasources = document["datasources"]
        self.assertEqual([item["uid"] for item in datasources], ["prometheus", "gideon-rows"])
        self.assertTrue(all(item["editable"] is False for item in datasources))
        self.assertEqual(datasources[0]["url"], "http://prometheus:9090")
        self.assertEqual(datasources[1]["user"], "gideon_ro_metrics")
        # The browser's query path reads the database from jsonData alone and
        # refuses every panel without it; only the backend falls back to the
        # top-level key, so the alert rules never showed the gap.
        self.assertEqual(datasources[1]["jsonData"]["database"], "gideon")
        self.assertNotIn("database", datasources[1])
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


def _leaf_panels(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    """Every panel but a row, the panels nested under a row included."""

    leaves = []
    for panel in dashboard["panels"]:
        if panel["type"] == "row":
            leaves.extend(panel.get("panels", []))
        else:
            leaves.append(panel)
    return leaves


class Overview(unittest.TestCase):
    def test_rendered_overview_has_declared_datasources_and_literal_dollars(self) -> None:
        for no_gpu in (False, True):
            with self.subTest(no_gpu=no_gpu):
                site_inputs = inputs(no_gpu=no_gpu)
                text = GrafanaOverviewArtifact().emit(site_inputs)
                dashboard = json.loads(text)
                self.assertEqual(dashboard["schemaVersion"], 41)
                self.assertEqual(dashboard["uid"], "gideon-overview")
                self.assertNotIn("id", dashboard)
                self.assertEqual(
                    {panel["title"] for panel in dashboard["panels"]},
                    {
                        "Needs attention",
                        START_HERE_TITLE,
                        "Services and probes",
                        FILESYSTEMS_FREE_TITLE,
                        CERTIFICATE_DAYS_LEFT_TITLE,
                        BACKUP_SET_AGE_TITLE,
                        BACKUP_PUSH_AGE_TITLE,
                        PASSING_DRILL_AGE_TITLE,
                        "Last drill result",
                        "Guardrail trips",
                        "Host detail",
                    },
                )
                self.assertEqual(len(dashboard["panels"]), 11)
                row = next(panel for panel in dashboard["panels"] if panel["type"] == "row")
                self.assertEqual(
                    [panel["title"] for panel in row["panels"]],
                    ["Host load", "Host memory", "Container memory"],
                )
                self.assertTrue(
                    all("id" not in panel for panel in (*dashboard["panels"], *row["panels"]))
                )
                declared = {
                    item["uid"]
                    for item in yaml.safe_load(site_inputs.templates[DATASOURCES_TEMPLATE])[
                        "datasources"
                    ]
                }
                self.assertTrue(_datasource_uids(dashboard) <= declared)
                template_text = (ROOT / "compose" / OVERVIEW_TEMPLATE).read_text(encoding="utf-8")
                for board_text in (text, template_text):
                    self.assertIn("$__timeFilter(at)", board_text)
                    self.assertIn("/^result$/", board_text)
                    self.assertIn("/ limit$/", board_text)
                    self.assertNotIn("$$", board_text)
                template = json.loads(template_text)
                expected = {panel["title"]: panel for panel in template["panels"]}
                rendered = {panel["title"]: panel for panel in dashboard["panels"]}
                self.assertEqual(expected[START_HERE_TITLE]["options"]["content"], "$start_here")
                expected[START_HERE_TITLE]["options"]["content"] = rendered[START_HERE_TITLE][
                    "options"
                ]["content"]
                for title, step_name, description_name, divisor in (
                    (PASSING_DRILL_AGE_TITLE, "drill_threshold", "drill_days", 86400),
                    (BACKUP_SET_AGE_TITLE, "backup_threshold", "backup_hours", 3600),
                    (BACKUP_PUSH_AGE_TITLE, "backup_threshold", "backup_hours", 3600),
                    (FILESYSTEMS_FREE_TITLE, "filesystem_percent", "filesystem_percent", 1),
                    (CERTIFICATE_DAYS_LEFT_TITLE, "certificate_days", "certificate_days", 1),
                ):
                    with self.subTest(no_gpu=no_gpu, panel=title):
                        step = expected[title]["fieldConfig"]["defaults"]["thresholds"]["steps"][-1]
                        value = rendered[title]["fieldConfig"]["defaults"]["thresholds"]["steps"][
                            -1
                        ]["value"]
                        self.assertEqual(step["value"], f"${step_name}")
                        step["value"] = value
                        description = expected[title]["description"]
                        self.assertIn(f"${description_name}", description)
                        expected[title]["description"] = description.replace(
                            f"${description_name}", str(value // divisor)
                        )
                self.assertEqual(dashboard, template)

    def test_overview_refuses_renamed_panel_or_missing_sentinel(self) -> None:
        site_inputs = inputs()
        for title, change, placeholder in (
            (PASSING_DRILL_AGE_TITLE, "title", "drill_threshold"),
            (PASSING_DRILL_AGE_TITLE, "description", "drill_days"),
            (BACKUP_SET_AGE_TITLE, "step", "backup_threshold"),
            (BACKUP_PUSH_AGE_TITLE, "description", "backup_hours"),
            (FILESYSTEMS_FREE_TITLE, "step", "filesystem_percent"),
            (CERTIFICATE_DAYS_LEFT_TITLE, "description", "certificate_days"),
        ):
            with self.subTest(panel=title, change=change):
                template = json.loads(site_inputs.templates[OVERVIEW_TEMPLATE])
                panel = next(panel for panel in template["panels"] if panel["title"] == title)
                if change == "title":
                    panel["title"] = "Fictitious renamed card"
                elif change == "step":
                    panel["fieldConfig"]["defaults"]["thresholds"]["steps"][-1][
                        "value"
                    ] = "fictitious"
                else:
                    panel["description"] = panel["description"].replace(
                        f"${placeholder}", "fictitious"
                    )
                altered = replace(
                    site_inputs,
                    templates={**site_inputs.templates, OVERVIEW_TEMPLATE: json.dumps(template)},
                )
                with self.assertRaisesRegex(
                    ValueError, rf"{re.escape(OVERVIEW_TEMPLATE)}.*{placeholder}"
                ):
                    GrafanaOverviewArtifact().emit(altered)

    def test_alert_list_selects_page_instances_without_the_heartbeat(self) -> None:
        panel = json.loads(GrafanaOverviewArtifact().emit(inputs()))["panels"][0]
        self.assertEqual(panel["type"], "alertlist")
        self.assertEqual(panel["title"], "Needs attention")
        self.assertEqual(panel["gridPos"], {"x": 0, "y": 0, "w": 16, "h": 8})
        self.assertNotIn("datasource", panel)
        options = panel["options"]
        self.assertEqual(
            {key: value for key, value in options.items() if key != "alertInstanceLabelFilter"},
            {
                "viewMode": "list",
                "groupMode": "default",
                "groupBy": [],
                "maxItems": 20,
                "sortOrder": 4,
                "dashboardAlerts": False,
                "alertName": "",
                "showInstances": True,
                "showInactiveAlerts": False,
                "stateFilter": {
                    "firing": True,
                    "pending": False,
                    "noData": True,
                    "normal": False,
                    "error": True,
                    "recovering": False,
                },
            },
        )
        self.assertIn("alertInstanceLabelFilter", options)
        label_filter = options["alertInstanceLabelFilter"]
        self.assertTrue(label_filter.startswith("{") and label_filter.endswith("}"))
        clauses = label_filter[1:-1].split(",")
        self.assertEqual(len(clauses), 2)
        matchers = []
        for clause in clauses:
            match = re.fullmatch(r'\s*([a-z_]+)\s*(!?=)\s*"([^"]+)"\s*', clause)
            self.assertIsNotNone(match)
            assert match is not None
            matchers.append(match.groups())
        self.assertEqual(
            set(matchers),
            {("class", "=", attention.PAGE_CLASS), (attention.HEARTBEAT_LABEL, "!=", "true")},
        )

    def test_start_here_links_follow_applicable_registered_boards(self) -> None:
        registered = {
            artifact.name
            for artifact in ARTIFACTS
            if artifact.relative_path.startswith("grafana/dashboards/")
            and artifact.name != GrafanaOverviewArtifact.name
        }
        self.assertEqual({board.name for board in FRONT_DOOR_BOARDS}, registered)
        for site_path, no_gpu in ((EXAMPLE, False), (SECOND, False), (EXAMPLE, True)):
            with self.subTest(site=site_path.name, no_gpu=no_gpu):
                site_inputs = inputs(site_path, no_gpu=no_gpu)
                panel = json.loads(GrafanaOverviewArtifact().emit(site_inputs))["panels"][1]
                self.assertEqual(panel["type"], "text")
                self.assertEqual(panel["title"], "Start here")
                self.assertEqual(panel["gridPos"], {"x": 16, "y": 0, "w": 8, "h": 8})
                self.assertNotIn("datasource", panel)
                self.assertEqual(panel["options"]["mode"], "markdown")
                content = panel["options"]["content"]
                links = re.findall(r"(?m)^- \[([^]]+)\]\(([^)]+)\)$", content)
                expected = []
                for board in FRONT_DOOR_BOARDS:
                    if board.applies(site_inputs):
                        document = json.loads((ROOT / "compose" / board.template_paths[0]).read_text(encoding="utf-8"))
                        expected.append((document["title"], GRAFANA_SUB_PATH + "d/" + document["uid"]))
                self.assertEqual(links, expected)
                self.assertEqual(len(links), 1 if no_gpu else 3)
                self.assertFalse(GRAFANA_SILENCES_ROUTE.startswith("/"))
                self.assertTrue(GRAFANA_SUB_PATH.endswith("/"))
                self.assertFalse(GRAFANA_SUB_PATH.endswith("//"))
                silences_path = GRAFANA_SUB_PATH + GRAFANA_SILENCES_ROUTE
                silences_lines = [
                    line for line in content.splitlines() if f"]({silences_path})" in line
                ]
                self.assertEqual(len(silences_lines), 1)
                self.assertIn("[silence the rule that would page]", silences_lines[0])
                self.assertFalse(silences_lines[0].startswith("- "))
                self.assertNotIn("target=", silences_lines[0])
                self.assertIn(str(INSTALL_HOME / START_HERE_CARD), content)
                export_link = re.search(r'<a href="([^"]+)" target="_blank">([^<]+)</a>', content)
                self.assertIsNotNone(export_link)
                assert export_link is not None
                self.assertEqual(
                    export_link.group(1),
                    PUBLIC_REPOSITORY_URL + "/blob/vfixture/" + START_HERE_CARD,
                )
                self.assertEqual(export_link.group(2), "in the public export at this release")
                self.assertEqual(content.count('target="_blank"'), 1)
                self.assertTrue(all('target=' not in line for line in content.splitlines() if line.startswith("- [")))

    def test_filesystem_panel_has_three_paged_mountpoints_and_shared_threshold(self) -> None:
        """The Overview filesystem panel follows the page rule."""

        dashboard = json.loads(GrafanaOverviewArtifact().emit(inputs()))
        panel = next(panel for panel in dashboard["panels"] if panel["title"] == FILESYSTEMS_FREE_TITLE)
        targets = panel["targets"]
        expected_mountpoints = {"/", "/var/lib/docker", "/data"}
        self.assertEqual(panel["type"], "bargauge")
        self.assertEqual(len(targets), 3)
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
                self.assertTrue(target["instant"])
                self.assertFalse(target["range"])

        rules = yaml.safe_load(GrafanaRulesArtifact().emit(inputs()))
        by_uid = {rule["uid"]: rule for group in rules["groups"] for rule in group["rules"]}
        paged_mountpoints: set[str] = set()
        for uid in ("gideon-data-volume-low", "gideon-host-filesystem-low"):
            expression = by_uid[uid]["data"][0]["model"]["expr"]
            matchers = re.findall(r'mountpoint(?:=|=~)"([^"]+)"', expression)
            self.assertEqual(len(matchers), 2)
            self.assertEqual(matchers[0], matchers[1])
            paged_mountpoints.update(matchers[0].split("|"))
        self.assertEqual(expected_mountpoints, paged_mountpoints)
        data_rule = by_uid["gideon-data-volume-low"]
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
        self.assertEqual(panel["fieldConfig"]["defaults"]["min"], 0)
        self.assertEqual(panel["fieldConfig"]["defaults"]["max"], 100)
        self.assertEqual(panel["options"]["orientation"], "horizontal")
        self.assertEqual(green_step["value"], data_threshold)

    def test_backup_cards_show_latest_age_and_drill_result(self) -> None:
        for site_path in (EXAMPLE, SECOND):
            with self.subTest(site=site_path.name):
                site_inputs = inputs(site_path)
                panels = {
                    panel["title"]: panel
                    for panel in json.loads(GrafanaOverviewArtifact().emit(site_inputs))["panels"]
                }
                rules = {
                    rule["uid"]: rule
                    for group in yaml.safe_load(GrafanaRulesArtifact().emit(site_inputs))["groups"]
                    for rule in group["rules"]
                }
                for title, kind, rule_uid in (
                    ("Backup set age", "backup_run", "gideon-backup-set-overdue"),
                    ("Backup push age", "backup_push", "gideon-push-overdue"),
                    (PASSING_DRILL_AGE_TITLE, "backup_drill", "gideon-drill-overdue"),
                ):
                    with self.subTest(panel=title):
                        panel = panels[title]
                        rule = rules[rule_uid]
                        condition = next(item for item in rule["data"] if item["refId"] == rule["condition"])
                        threshold = condition["model"]["conditions"][0]["evaluator"]["params"][0]
                        self.assertEqual(panel["type"], "stat")
                        self.assertEqual(panel["targets"][0]["format"], "table")
                        sql = panel["targets"][0]["rawSql"]
                        self.assertIn("now() - max(at)", sql)
                        self.assertIn(f"kind = '{kind}'", sql)
                        if kind == "backup_run":
                            self.assertIn("detail->>'phase' = 'applied'", sql)
                        if kind == "backup_drill":
                            rule_sql = rule["data"][0]["model"]["rawSql"]
                            drill_filter = "WHERE kind = 'backup_drill' AND detail->>'result' = 'pass'"
                            self.assertIn(drill_filter, sql)
                            self.assertIn(drill_filter, rule_sql)
                            self.assertEqual(panel["fieldConfig"]["defaults"]["unit"], "s")
                            self.assertIn(f"Under {threshold // 86400} days", panel["description"])
                            self.assertIn("docs/runbooks/observability.md §4.", panel["description"])
                        self.assertEqual(
                            panel["fieldConfig"]["defaults"]["thresholds"]["steps"],
                            [{"color": "green", "value": None}, {"color": "red", "value": threshold}],
                        )
                        self.assertEqual(panel["options"]["colorMode"], "value")
                        self.assertEqual(panel["options"]["graphMode"], "none")
                        self.assertEqual(panel["options"]["textMode"], "value")

                for title, column in (
                    ("Backup set age", 0),
                    ("Backup push age", 6),
                    (PASSING_DRILL_AGE_TITLE, 12),
                    ("Last drill result", 18),
                ):
                    with self.subTest(panel=title):
                        self.assertEqual(panels[title]["gridPos"], {"h": 7, "w": 6, "x": column, "y": 16})

                drill = panels["Last drill result"]
                self.assertEqual(drill["targets"][0]["format"], "table")
                self.assertIn("detail->>'result' AS result", drill["targets"][0]["rawSql"])
                self.assertEqual(drill["options"]["reduceOptions"]["fields"], "/^result$/")
                self.assertEqual(drill["options"]["colorMode"], "value")
                self.assertEqual(drill["options"]["graphMode"], "none")
                self.assertEqual(drill["options"]["textMode"], "value")
                mappings = drill["fieldConfig"]["defaults"]["mappings"]
                self.assertEqual(len(mappings), 1)
                self.assertEqual(mappings[0]["type"], "value")
                self.assertEqual(mappings[0]["options"]["pass"]["color"], "green")
                self.assertEqual(mappings[0]["options"]["failed"]["color"], "red")

    def test_coloured_cards_take_each_line_from_their_page_rule(self) -> None:
        """Each graded Overview measure changes colour at its rendered page line."""

        self.assertNotEqual(
            inputs(EXAMPLE).site.backup.drill_interval,
            inputs(SECOND).site.backup.drill_interval,
        )
        for site_path in (EXAMPLE, SECOND):
            for no_gpu in (False, True):
                site_inputs = inputs(site_path, no_gpu=no_gpu)
                panels = {
                    panel["title"]: panel
                    for panel in json.loads(GrafanaOverviewArtifact().emit(site_inputs))["panels"]
                }
                rules = {
                    rule["uid"]: rule
                    for group in yaml.safe_load(GrafanaRulesArtifact().emit(site_inputs))["groups"]
                    for rule in group["rules"]
                }
                for title, rule_uid, colour, evaluator_type, divisor in (
                    (BACKUP_SET_AGE_TITLE, "gideon-backup-set-overdue", "red", "gt", 1),
                    (BACKUP_PUSH_AGE_TITLE, "gideon-push-overdue", "red", "gt", 1),
                    (PASSING_DRILL_AGE_TITLE, "gideon-drill-overdue", "red", "gt", 1),
                    (FILESYSTEMS_FREE_TITLE, "gideon-data-volume-low", "green", "lt", 1),
                    (FILESYSTEMS_FREE_TITLE, "gideon-host-filesystem-low", "green", "lt", 1),
                    (CERTIFICATE_DAYS_LEFT_TITLE, "gideon-tls-expiring", "green", "lt", 86400),
                ):
                    with self.subTest(
                        site=site_path.name, no_gpu=no_gpu, panel=title, rule=rule_uid
                    ):
                        panel = panels[title]
                        rule = rules[rule_uid]
                        condition = next(
                            item for item in rule["data"] if item["refId"] == rule["condition"]
                        )
                        evaluator = condition["model"]["conditions"][0]["evaluator"]
                        self.assertEqual(evaluator["type"], evaluator_type)
                        line = evaluator["params"][0]
                        steps = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
                        self.assertEqual(steps[-1], {"color": colour, "value": line // divisor})
                        self.assertEqual(
                            steps[0],
                            {"color": "green" if colour == "red" else "red", "value": None},
                        )
                        if title in (BACKUP_SET_AGE_TITLE, BACKUP_PUSH_AGE_TITLE):
                            self.assertIn(f"Under {line // 3600} hours", panel["description"])
                        elif title == PASSING_DRILL_AGE_TITLE:
                            self.assertIn(f"Under {line // 86400} days", panel["description"])
                        elif title == FILESYSTEMS_FREE_TITLE:
                            self.assertIn(f"{line} percent free", panel["description"])
                        else:
                            self.assertIn(f"{line // divisor} days or more", panel["description"])
                            expression = rule["data"][0]["model"]["expr"]
                            self.assertEqual(
                                panel["targets"][0]["expr"],
                                f"floor(({expression}) / {divisor})",
                            )
                            self.assertEqual(panel["fieldConfig"]["defaults"]["decimals"], 0)

    def test_service_tiles_cover_every_rendered_down_rule_job(self) -> None:
        """A rendered scrape or probe job appears in the tiles and has a page rule."""

        for site_path, no_gpu in ((EXAMPLE, False), (SECOND, False), (EXAMPLE, True)):
            with self.subTest(site=site_path.name, no_gpu=no_gpu):
                site_inputs = inputs(site_path, no_gpu=no_gpu)
                panels = json.loads(GrafanaOverviewArtifact().emit(site_inputs))["panels"]
                panel = next(item for item in panels if item["title"] == "Services and probes")
                self.assertEqual(panel["type"], "stat")
                self.assertEqual(panel["options"]["colorMode"], "background")
                self.assertEqual(panel["options"]["graphMode"], "none")
                self.assertEqual(panel["options"]["textMode"], "value_and_name")
                # Grafana's automatic size shrinks a name to unreadable across many tiles.
                self.assertGreaterEqual(panel["options"]["text"]["titleSize"], 14)
                self.assertEqual(
                    [(target["expr"], target["legendFormat"]) for target in panel["targets"]],
                    [("up", "{{job}}"), ("probe_success", "{{job}} probe"), ("pg_up", "{{job}} database")],
                )
                self.assertTrue(all(target["instant"] and not target["range"] for target in panel["targets"]))
                mappings = panel["fieldConfig"]["defaults"]["mappings"]
                self.assertEqual(len(mappings), 1)
                self.assertEqual(mappings[0]["type"], "value")
                self.assertEqual(
                    {key: (value["text"], value["color"]) for key, value in mappings[0]["options"].items()},
                    {"1": ("up", "green"), "0": ("down", "red")},
                )

                jobs = yaml.safe_load(PrometheusConfigArtifact().emit(site_inputs))[
                    "scrape_configs"
                ]
                scrape_jobs = {job["job_name"] for job in jobs}
                probe_jobs = {
                    job["job_name"] for job in jobs if job.get("metrics_path") == "/probe"
                }
                rules = {
                    rule["uid"]: rule
                    for group in yaml.safe_load(GrafanaRulesArtifact().emit(site_inputs))["groups"]
                    for rule in group["rules"]
                }
                target_down = rules["gideon-target-down"]["data"][0]["model"]["expr"]
                excluded = set(re.findall(r'job!="([^"]+)"', target_down))
                self.assertRegex(target_down, r'^min by \(job, instance\) \(up\{job!="[^"]+"\}\) == bool 0$')
                self.assertEqual(rules["gideon-target-down"]["labels"]["class"], "page")
                self.assertEqual(excluded & scrape_jobs, {ENGINE_JOB_NAME} if not no_gpu else set())
                for job in excluded & scrape_jobs:
                    with self.subTest(job=job):
                        self.assertEqual(
                            rules["gideon-engine-down"]["data"][0]["model"]["expr"],
                            f'up{{job="{job}"}} == bool 0',
                        )
                        self.assertEqual(rules["gideon-engine-down"]["labels"]["class"], "page")
                probe_rule_jobs = []
                for rule in rules.values():
                    expression = rule["data"][0]["model"].get("expr", "")
                    match = re.fullmatch(r'probe_success\{job="([^"]+)"\} == bool 0', expression)
                    if match:
                        probe_rule_jobs.append(match.group(1))
                        self.assertEqual(rule["labels"]["class"], "page")
                self.assertEqual(len(probe_rule_jobs), len(set(probe_rule_jobs)))
                self.assertEqual(set(probe_rule_jobs), probe_jobs)
                self.assertEqual(rules["gideon-postgres-down"]["data"][0]["model"]["expr"], "pg_up == bool 0")

    def test_collapsed_row_holds_host_detail_and_grid_positions(self) -> None:
        """A collapsed row saves its children at their absolute open positions."""

        for site_path, no_gpu in ((EXAMPLE, False), (SECOND, False), (EXAMPLE, True)):
            with self.subTest(site=site_path.name, no_gpu=no_gpu):
                site_inputs = inputs(site_path, no_gpu=no_gpu)
                overview = json.loads(GrafanaOverviewArtifact().emit(site_inputs))
                row = next(item for item in overview["panels"] if item["type"] == "row")
                self.assertEqual(row["title"], "Host detail")
                self.assertNotIn("$", row["title"])
                self.assertTrue(row["collapsed"])
                self.assertEqual(row["gridPos"], {"h": 1, "w": 24, "x": 0, "y": 30})
                self.assertEqual(
                    [panel["title"] for panel in row["panels"]],
                    ["Host load", "Host memory", "Container memory"],
                )
                self.assertEqual(
                    [panel["gridPos"] for panel in row["panels"]],
                    [
                        {"h": 8, "w": 6, "x": 0, "y": 31},
                        {"h": 8, "w": 6, "x": 6, "y": 31},
                        {"h": 8, "w": 12, "x": 12, "y": 31},
                    ],
                )
                for panel in row["panels"]:
                    self.assertIn("docs/runbooks/observability.md §4", panel["description"])
                    self.assertTrue(panel["description"].strip())
                for artifact in ARTIFACTS:
                    if not artifact.relative_path.startswith("grafana/dashboards/") or not artifact.applies(site_inputs):
                        continue
                    board = json.loads(artifact.emit(site_inputs))
                    for item in board["panels"]:
                        if item["type"] == "row" and item.get("collapsed") and item.get("panels"):
                            with self.subTest(board=board["uid"], row=item["title"]):
                                self.assertEqual(
                                    item["panels"][0]["gridPos"]["y"],
                                    item["gridPos"]["y"] + 1,
                                )

    def test_container_memory_compares_working_set_with_positive_limits(self) -> None:
        dashboard = json.loads(GrafanaOverviewArtifact().emit(inputs()))
        row = next(panel for panel in dashboard["panels"] if panel["title"] == "Host detail")
        panel = next(panel for panel in row["panels"] if panel["title"] == "Container memory")
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

    def test_trip_panels_use_their_own_boards_and_source_filters(self) -> None:
        overview = json.loads(GrafanaOverviewArtifact().emit(inputs()))
        evaluation = json.loads(GrafanaEvalArtifact.emit(inputs()))
        self.assertNotIn("Last eval trip", {panel["title"] for panel in overview["panels"]})
        for board, title, source in (
            (overview, "Guardrail trips", "user"),
            (evaluation, "Last eval trip", "eval"),
        ):
            with self.subTest(board=board["uid"], panel=title):
                panel = next(panel for panel in board["panels"] if panel["title"] == title)
                target = panel["targets"][0]
                self.assertEqual(panel["datasource"]["uid"], "gideon-rows")
                self.assertIn("guardrail_trips", target["rawSql"])
                self.assertIn(f"source = '{source}'", target["rawSql"])
        # Day buckets sit at midnight, outside the board's six-hour default
        # range after 06:00; the count panel carries its own thirty-day range.
        self.assertEqual(
            next(panel for panel in overview["panels"] if panel["title"] == "Guardrail trips")[
                "timeFrom"
            ],
            "30d",
        )
        self.assertEqual(evaluation["panels"][-1]["options"]["colorMode"], "none")

    def test_verbatim_artifact_emits_its_template_without_expansion(self) -> None:
        artifact = VerbatimArtifact(
            name="example",
            relative_path="example.txt",
            template_path=BACKUP_TEMPLATE,
        )
        self.assertEqual(artifact.emit(inputs()), inputs().templates[BACKUP_TEMPLATE])


OBSERVABILITY_RUNBOOK = ROOT / "docs/runbooks/observability.md"
TILE_TABLE_HEADING = "### The tiles on *Services and probes*"
SECTION_FOUR_HEADING = "## 4. What pages, and what to do"
GPU_ONLY = "A GPU host only."
SEARCH_ONLY = "Present only while `web.search` is on"
CORE_PAGE = "core service down"
POSTGRES_EXPORTER_SERVICE = "postgres-exporter"
TILE_KINDS = {"up": "plain", "probe_success": "probe", "pg_up": "database"}
WATCHED_KIND_ORDER = ("probe", "database", "plain")  # a row is held to its most specific tile


@dataclass(frozen=True)
class MarkdownRow:
    cells: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class TileRow:
    tiles: tuple[str, ...]
    description: str
    page: str
    line: int


@dataclass(frozen=True)
class ServiceTile:
    name: str
    job: str
    kind: str
    rule: str


@dataclass(frozen=True)
class RenderFacts:
    label: str
    gpu: bool
    search: bool
    tiles: tuple[ServiceTile, ...]
    findings: tuple[str, ...]


def markdown_table(text: str, heading: str) -> tuple[tuple[MarkdownRow, ...], tuple[str, ...]]:
    """The first table below a heading, its header and separator skipped.

    A row whose cell count differs from the header's is a finding, so a pipe
    inside a cell cannot shift a column silently.
    """

    lines = text.splitlines()
    if heading not in lines:
        return (), (f"{heading}: restore this runbook heading and its table",)
    table: list[tuple[int, str]] = []
    for number, line in enumerate(lines[lines.index(heading) + 1 :], lines.index(heading) + 2):
        if line.startswith("#"):
            break
        if line.startswith("|"):
            table.append((number, line))
        elif table:
            break
    if len(table) < 2:
        return (), (f"{heading}: restore the table below this heading",)
    width = len(table[0][1].strip("|").split("|"))
    rows: list[MarkdownRow] = []
    findings: list[str] = []
    for number, line in table[2:]:
        cells = tuple(cell.strip() for cell in line.strip("|").split("|"))
        if len(cells) == width:
            rows.append(MarkdownRow(cells, number))
        else:
            findings.append(f"runbook line {number}: give the row {width} cells, no pipe inside one")
    return tuple(rows), tuple(findings)


def tile_table(text: str) -> tuple[tuple[TileRow, ...], tuple[str, ...]]:
    """The tile table's rows: tile names, what each checks, and its page."""

    rows, issues = markdown_table(text, TILE_TABLE_HEADING)
    findings = list(issues)
    parsed: list[TileRow] = []
    for row in rows:
        if len(row.cells) != 3:
            findings.append(f"runbook line {row.line}: give the tile table three columns")
        elif re.fullmatch(r"`[^`]+`(?:, `[^`]+`)*", row.cells[0]) is None:
            findings.append(
                f"runbook line {row.line}: write the first cell as tile names in code spans,"
                " separated by commas and nothing else"
            )
        else:
            names = tuple(re.findall(r"`([^`]+)`", row.cells[0]))
            parsed.append(TileRow(names, row.cells[1], row.cells[2], row.line))
    return tuple(parsed), tuple(findings)


def section_four_rows(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The rule names section 4's table holds, as it spells them."""

    rows, findings = markdown_table(text, SECTION_FOUR_HEADING)
    return tuple(row.cells[0] for row in rows), findings


def _watching_expression(kind: str, job: str, engine_jobs: set[str], target_down: str) -> str:
    """The expression of the page rule that watches one tile."""

    if kind == "probe":
        return f'probe_success{{job="{job}"}} == bool 0'
    if kind == "database":
        return "pg_up == bool 0"
    if job in engine_jobs:
        return f'up{{job="{job}"}} == bool 0'
    return target_down


def render_tile_facts(site_inputs: RenderInputs, label: str) -> RenderFacts:
    """The tiles one render's card draws, each with its job and watching rule."""

    panel = next(
        item
        for item in json.loads(GrafanaOverviewArtifact().emit(site_inputs))["panels"]
        if item["title"] == "Services and probes"
    )
    jobs = yaml.safe_load(PrometheusConfigArtifact().emit(site_inputs))["scrape_configs"]
    rules = [
        rule
        for group in yaml.safe_load(GrafanaRulesArtifact().emit(site_inputs))["groups"]
        for rule in group["rules"]
    ]
    titles = {rule["data"][0]["model"].get("expr"): rule["title"] for rule in rules}
    target_down = next(rule for rule in rules if rule["title"] == "Target down")
    target_down_expr = target_down["data"][0]["model"]["expr"]
    engine_jobs = set(re.findall(r'job!="([^"]+)"', target_down_expr))

    findings: list[str] = []
    if POSTGRES_EXPORTER_SERVICE not in service_blocks(site_inputs):
        findings.append(f"{POSTGRES_EXPORTER_SERVICE}: the render has no such Compose service")
    postgres_jobs: list[str] = []
    for job in jobs:
        targets = [target for config in job["static_configs"] for target in config["targets"]]
        if len(targets) != 1:
            findings.append(f"{job['job_name']}: one static target per job, or the card draws one name twice")
        if any(target.split(":")[0] == POSTGRES_EXPORTER_SERVICE for target in targets):
            postgres_jobs.append(job["job_name"])
    if len(postgres_jobs) != 1:
        findings.append(f"{POSTGRES_EXPORTER_SERVICE}: exactly one job must target it, found {postgres_jobs}")

    drawn = {
        "plain": jobs,
        "probe": [job for job in jobs if job.get("metrics_path") == "/probe"],
        "database": [job for job in jobs if job["job_name"] in postgres_jobs],
    }
    tiles: list[ServiceTile] = []
    for target in panel["targets"]:
        kind = TILE_KINDS.get(target["expr"])
        if kind is None:
            findings.append(f"{target['expr']}: a card target of no known tile kind")
            continue
        for job in drawn[kind]:
            name = job["job_name"]
            expression = _watching_expression(kind, name, engine_jobs, target_down_expr)
            if expression not in titles:
                findings.append(f"{name}: no rendered rule watches its {kind} tile")
                continue
            tile_name = target["legendFormat"].replace("{{job}}", name)
            tiles.append(ServiceTile(tile_name, name, kind, titles[expression]))
    return RenderFacts(
        label, not site_inputs.no_gpu, search_enabled(site_inputs), tuple(tiles), tuple(findings)
    )


def _offered(row: TileRow, render: RenderFacts) -> bool:
    return (render.gpu or GPU_ONLY not in row.description) and (
        render.search or SEARCH_ONLY not in row.description
    )


def service_tile_findings(
    rows: tuple[TileRow, ...], pages: tuple[str, ...], renders: tuple[RenderFacts, ...]
) -> tuple[str, ...]:
    """Every way the runbook's tile table disagrees with the renders.

    The renders are, in order, a GPU host with search on, a GPU host with
    search off, and a host without a GPU with search on: the jobs leaving
    between the first and the third are the GPU jobs, those leaving between
    the first and the second the search jobs.
    """

    def jobs_of(render: RenderFacts) -> set[str]:
        return {tile.job for tile in render.tiles}

    full, search_off, no_gpu = renders
    gpu_jobs = jobs_of(full) - jobs_of(no_gpu)
    search_jobs = jobs_of(full) - jobs_of(search_off)
    tiles = {tile.name: tile for render in renders for tile in render.tiles}
    by_casefold = {page.casefold(): page for page in pages}
    core = by_casefold.get(CORE_PAGE)

    findings: list[str] = []
    if core is None:
        findings.append(f"{CORE_PAGE}: restore this row to section 4's table")
    counts = Counter(name for row in rows for name in row.tiles)
    findings += [
        f"{name}: name this tile in one row of the table" for name, count in sorted(counts.items()) if count > 1
    ]
    for row in rows:
        first = row.tiles[0]
        if row.page not in pages:
            findings.append(f"{first}: its page cell `{row.page}` is no section 4 row")
        drawn = [tiles[name] for name in row.tiles if name in tiles]
        row_jobs = sorted({tile.job for tile in drawn})
        if len(row_jobs) > 1:
            findings.append(f"{first}: the row mixes the jobs {row_jobs}; give each job its own row")
        if not drawn:
            continue  # a row no render draws is named by the offering below
        job = row_jobs[0]
        for sentence, governed in ((GPU_ONLY, gpu_jobs), (SEARCH_ONLY, search_jobs)):
            if (sentence in row.description) != (job in governed):
                change = "add" if job in governed else "remove"
                findings.append(f"{job}: {change} the sentence {sentence!r} in its row")
        plain_jobs = {tile.job for tile in drawn if tile.kind == "plain"}
        for tile in drawn:
            if tile.kind == "probe" and tile.job not in plain_jobs:
                findings.append(f"{tile.name}: move it into the row naming `{tile.job}`")
        watched = min(drawn, key=lambda tile: WATCHED_KIND_ORDER.index(tile.kind))
        wanted = by_casefold.get(watched.rule.casefold(), core)
        if wanted is not None and row.page != wanted:
            findings.append(
                f"{watched.name}: set its page cell to `{wanted}`, the section 4 row"
                f" of its rule {watched.rule!r}"
            )
    for render in renders:
        offered = {name for row in rows if _offered(row, render) for name in row.tiles}
        drawn_names = {tile.name for tile in render.tiles}
        findings += [
            f"{name}: the {render.label} render draws this tile and no offered row names it"
            for name in sorted(drawn_names - offered)
        ]
        findings += [
            f"{name}: an offered row names this tile and the {render.label} render does not draw it"
            for name in sorted(offered - drawn_names)
        ]
    return tuple(findings)


class ServiceTileTable(unittest.TestCase):
    rows: tuple[TileRow, ...]
    pages: tuple[str, ...]
    renders: tuple[RenderFacts, ...]

    @classmethod
    def setUpClass(cls) -> None:
        text = OBSERVABILITY_RUNBOOK.read_text(encoding="utf-8")
        cls.rows, row_findings = tile_table(text)
        cls.pages, page_findings = section_four_rows(text)
        cls.renders = tuple(
            render_tile_facts(inputs(path, no_gpu=no_gpu), label)
            for path, no_gpu, label in (
                (EXAMPLE, False, "example"),
                (SECOND, False, "second office"),
                (EXAMPLE, True, "no-GPU"),
            )
        )
        if row_findings or page_findings:
            raise AssertionError(row_findings + page_findings)

    def test_the_runbook_table_matches_every_render(self) -> None:
        full, search_off, no_gpu = self.renders
        self.assertEqual((full.gpu, full.search), (True, True))
        self.assertEqual((search_off.gpu, search_off.search), (True, False))
        self.assertEqual((no_gpu.gpu, no_gpu.search), (False, True))
        self.assertEqual(len({frozenset(tile.job for tile in render.tiles) for render in self.renders}), 3)
        for render in self.renders:
            self.assertEqual(render.findings, (), render.label)
        self.assertEqual(service_tile_findings(self.rows, self.pages, self.renders), ())

    def test_card_description_cites_the_tile_table_and_pages(self) -> None:
        dashboard = json.loads(GrafanaOverviewArtifact().emit(inputs()))
        panel = next(
            item for item in dashboard["panels"] if item["title"] == "Services and probes"
        )
        description = panel["description"]
        references = re.findall(r"(docs/runbooks/[\w-]+\.md) §(\d+)", description)
        runbook_path = OBSERVABILITY_RUNBOOK.relative_to(ROOT).as_posix()
        self.assertEqual(references, [(runbook_path, "3"), (runbook_path, "4")])
        self.assertTrue(
            description.endswith(
                f"Each tile is named in {runbook_path} §3. Follow {runbook_path} §4."
            )
        )

        lines = OBSERVABILITY_RUNBOOK.read_text(encoding="utf-8").splitlines()
        section_three = next(index for index, line in enumerate(lines) if line.startswith("## 3."))
        section_four = next(index for index, line in enumerate(lines) if line.startswith("## 4."))
        self.assertLess(section_three, lines.index(TILE_TABLE_HEADING))
        self.assertLess(lines.index(TILE_TABLE_HEADING), section_four)

    def test_each_seeded_variant_names_its_job_or_tile(self) -> None:
        rows, pages, renders = self.rows, self.pages, self.renders

        def row_of(tile: str) -> TileRow:
            return next(row for row in rows if tile in row.tiles)

        def with_row(tile: str, **changes: Any) -> tuple[TileRow, ...]:
            return tuple(replace(row, **changes) if tile in row.tiles else row for row in rows)

        def without_row(tile: str) -> tuple[TileRow, ...]:
            return tuple(row for row in rows if tile not in row.tiles)

        def each_render(change: Any) -> tuple[RenderFacts, ...]:
            return tuple(replace(render, tiles=change(render.tiles)) for render in renders)

        added = ServiceTile("fictitious", "fictitious", "plain", "Target down")
        cases: list[tuple[str, tuple[str, ...], tuple[TileRow, ...], tuple[str, ...], tuple[RenderFacts, ...]]] = [
            ("job added", ("fictitious",), rows, pages, each_render(lambda tiles: (*tiles, added))),
            (
                "job renamed",
                ("renamed", "cadvisor"),
                rows,
                pages,
                each_render(
                    lambda tiles: tuple(
                        replace(tile, name="renamed", job="renamed") if tile.job == "cadvisor" else tile
                        for tile in tiles
                    )
                ),
            ),
            (
                "job removed",
                ("cadvisor",),
                rows,
                pages,
                each_render(lambda tiles: tuple(tile for tile in tiles if tile.job != "cadvisor")),
            ),
            ("row removed", ("cadvisor",), without_row("cadvisor"), pages, renders),
            ("probe span dropped", ("ingress probe",), with_row("ingress", tiles=("ingress",)), pages, renders),
            ("database row dropped", ("postgres database",), without_row("postgres database"), pages, renders),
            (
                "GPU sentence on an unconditional row",
                ("node",),
                with_row("node", description=f"{row_of('node').description} {GPU_ONLY}"),
                pages,
                renders,
            ),
            (
                "GPU sentence off a GPU row",
                ("dcgm",),
                with_row("dcgm", description=row_of("dcgm").description.replace(GPU_ONLY, "")),
                pages,
                renders,
            ),
            (
                "search sentence on an unconditional row",
                ("node",),
                with_row("node", description=f"{row_of('node').description} {SEARCH_ONLY}."),
                pages,
                renders,
            ),
            (
                "search sentence off the search row",
                ("search",),
                with_row("search", description=row_of("search").description.replace(SEARCH_ONLY, "")),
                pages,
                renders,
            ),
            ("page cell no section 4 row", ("search",), with_row("search", page="search down"), pages, renders),
            ("search page cell set to the core row", ("search probe",), with_row("search", page=CORE_PAGE), pages, renders),
            (
                "core row absent from section 4",
                ("caddy",),
                rows,
                tuple(page for page in pages if page != CORE_PAGE),
                renders,
            ),
        ]
        for label, names, variant_rows, variant_pages, variant_renders in cases:
            with self.subTest(variant=label):
                findings = service_tile_findings(variant_rows, variant_pages, variant_renders)
                for name in names:
                    self.assertTrue(
                        any(finding.startswith(f"{name}:") for finding in findings), (name, findings)
                    )


class Dashboards(unittest.TestCase):
    def test_every_range_card_draws_one_series_per_gpu(self) -> None:
        for path in sorted((ROOT / "compose/grafana/dashboards").rglob("*.json")):
            dashboard = json.loads(path.read_text(encoding="utf-8"))
            for panel in _leaf_panels(dashboard):
                if panel["type"] == "stat":
                    continue
                for target in panel.get("targets", []):
                    expr = target.get("expr", "")
                    if "DCGM_FI_" not in expr:
                        continue
                    with self.subTest(
                        dashboard=path.name, panel=panel["title"], refId=target["refId"]
                    ):
                        gpu_aggregated_field(expr)
                        labels = re.findall(r"\{\{\s*(\w+)\s*\}\}", target["legendFormat"])
                        self.assertTrue(all(label == "gpu" for label in labels))

        dashboard = json.loads(GrafanaGpuArtifact.emit(inputs()))
        panels = {panel["title"]: panel for panel in _leaf_panels(dashboard)}
        expected = {
            "GPU utilisation": [("DCGM_FI_DEV_GPU_UTIL", "GPU {{gpu}}")],
            "GPU memory": [
                ("DCGM_FI_DEV_FB_USED", "GPU {{gpu}}"),
                ("DCGM_FI_DEV_FB_FREE", "GPU {{gpu}} free"),
            ],
            "GPU temperature": [
                ("DCGM_FI_DEV_GPU_TEMP", "GPU {{gpu}}"),
                ("DCGM_FI_DEV_GPU_MAX_OP_TEMP", "GPU {{gpu}} limit"),
            ],
            "GPU power": [("DCGM_FI_DEV_POWER_USAGE", "GPU {{gpu}}")],
        }
        for title, pairs in expected.items():
            with self.subTest(panel=title):
                self.assertEqual(
                    [
                        (gpu_aggregated_field(target["expr"]), target["legendFormat"])
                        for target in panels[title]["targets"]
                    ],
                    pairs,
                )

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
                for panel in _leaf_panels(dashboard):
                    with self.subTest(dashboard=path.name, panel=panel["title"]):
                        self.assertTrue(_datasource_uids(panel) <= declared)

    def test_every_dashboard_links_to_the_gideon_boards(self) -> None:
        dashboard_dir = ROOT / "compose/grafana/dashboards"
        paths = sorted(dashboard_dir.glob("*.json"))
        self.assertTrue(paths)
        expected_link = {
            "type": "dashboards",
            "tags": ["GIDEON"],
            "asDropdown": False,
            "title": "GIDEON boards",
            "includeVars": False,
            "keepTime": False,
            "targetBlank": False,
        }
        for path in paths:
            with self.subTest(dashboard=path.name):
                dashboard = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(dashboard["links"], [expected_link])
                self.assertIn("GIDEON", dashboard["tags"])

    def test_every_panel_description_names_an_existing_runbook_section(self) -> None:
        dashboard_dir = ROOT / "compose/grafana/dashboards"
        paths = sorted(dashboard_dir.glob("*.json"))
        self.assertTrue(paths)
        for path in paths:
            dashboard = json.loads(path.read_text(encoding="utf-8"))
            for panel in _leaf_panels(dashboard):
                with self.subTest(dashboard=path.name, panel=panel["title"]):
                    description = panel.get("description")
                    self.assertIsInstance(description, str)
                    assert isinstance(description, str)
                    self.assertTrue(description.strip())
                    references = re.findall(
                        r"(docs/runbooks/[\w-]+\.md) §(\d+)", description
                    )
                    self.assertTrue(references)
                    self.assertEqual(description.count("docs/runbooks/"), len(references))
                    for runbook_path, section in references:
                        runbook = ROOT / runbook_path
                        self.assertTrue(runbook.is_file(), runbook_path)
                        headings = re.findall(
                            r"(?m)^## (\d+)\.", runbook.read_text(encoding="utf-8")
                        )
                        self.assertIn(section, headings)


class TableLayout(unittest.TestCase):
    def test_helpers_match_v038_board_readings(self) -> None:
        panel: dict[str, Any] = {
            "gridPos": {"h": 7, "w": 24},
            "fieldConfig": {"defaults": {}, "overrides": []},
        }
        self.assertEqual(table_whole_rows(panel), 4)
        self.assertEqual(table_inner_width(panel), 1550)
        panel["gridPos"]["h"] = 10
        self.assertEqual(table_whole_rows(panel), 7)
        self.assertEqual(sum(table_column_widths(panel, [f"field_{i}" for i in range(12)]).values()), 1800)
        panel["gridPos"]["w"] = 12
        self.assertEqual(table_inner_width(panel), 762)
        self.assertEqual(sum(table_column_widths(panel, [f"field_{i}" for i in range(10)]).values()), 1500)

    def test_column_reader_uses_outer_select_and_skips_distinct_on(self) -> None:
        sql = (
            "WITH fictitious AS (SELECT hidden FROM invented_rows) "
            "SELECT DISTINCT ON (group_key, item_key) t.at, "
            "COALESCE(t.size, 0) AS bytes FROM fictitious t"
        )
        self.assertEqual(outer_select_columns(sql), ["at", "bytes"])


class BackupBoard(unittest.TestCase):
    def test_board_is_verbatim_and_applies_on_every_host(self) -> None:
        self.assertIn(GrafanaBackupArtifact, ARTIFACTS)
        self.assertIsInstance(GrafanaBackupArtifact, VerbatimArtifact)
        self.assertEqual(GrafanaBackupArtifact.owners, ("grafana",))
        self.assertEqual(GrafanaBackupArtifact.emit(inputs()), inputs().templates[BACKUP_TEMPLATE])
        self.assertTrue(GrafanaBackupArtifact.applies(inputs()))
        self.assertTrue(GrafanaBackupArtifact.applies(inputs(no_gpu=True)))

    def test_range_follows_the_default_drill_cadence(self) -> None:
        board = json.loads(inputs().templates[BACKUP_TEMPLATE])
        interval = next(spec.default for spec in FIELD_REGISTRY if spec.path == "backup.drill_interval")
        assert isinstance(interval, str)
        self.assertEqual(
            board["time"],
            {"from": f"now-{DRILL_MAX_GAP_DAYS[interval]}d", "to": "now"},
        )

    def test_drill_and_runs_tables_hold_their_limited_rows(self) -> None:
        board = json.loads(inputs().templates[BACKUP_TEMPLATE])
        panels = {panel["title"]: panel for panel in board["panels"]}
        drill = panels["Restore drill result"]
        duration = panels["Restore drill duration"]
        runs = panels["Last ten backup and restore runs"]
        for panel, count in ((drill, 5), (runs, 10)):
            with self.subTest(panel=panel["title"]):
                self.assertRegex(panel["targets"][0]["rawSql"], rf"(?i)\bORDER BY at DESC LIMIT {count}$")
                self.assertGreaterEqual(table_whole_rows(panel), count)
                self.assertEqual(panel["options"]["cellHeight"], "sm")
                self.assertTrue(panel["options"]["showHeader"])
        self.assertEqual(drill["gridPos"]["y"], duration["gridPos"]["y"])
        self.assertEqual(drill["gridPos"]["h"], duration["gridPos"]["h"])
        self.assertEqual(runs["gridPos"]["y"], drill["gridPos"]["y"] + drill["gridPos"]["h"])

    def test_runs_table_uses_the_chart_ratio_and_recorded_result(self) -> None:
        board = json.loads(inputs().templates[BACKUP_TEMPLATE])
        panels = {panel["title"]: panel for panel in board["panels"]}
        chart = panels["Hard-link ratio"]
        runs = panels["Last ten backup and restore runs"]
        chart_sql = chart["targets"][0]["rawSql"]
        runs_sql = runs["targets"][0]["rawSql"]
        expression = r"CASE WHEN jsonb_typeof\(detail->'hard_links'\).*?END AS hard_link_ratio"
        chart_ratio = re.search(expression, chart_sql)
        runs_ratio = re.search(expression, runs_sql)
        self.assertIsNotNone(chart_ratio)
        self.assertIsNotNone(runs_ratio)
        assert chart_ratio is not None and runs_ratio is not None
        self.assertEqual(runs_ratio.group(), chart_ratio.group())
        self.assertIn("COALESCE(detail->>'result', 'completed') AS result", runs_sql)
        self.assertEqual(
            outer_select_columns(runs_sql),
            ["at", "kind", "duration_s", "result", "set_bytes", "hard_link_ratio", "transferred_bytes", "total_bytes", "verified", "pruned"],
        )
        self.assertNotRegex(runs_sql, r"detail->'hard_links'\s+AS")
        kinds = re.search(r"kind IN \(([^)]*)\)", runs_sql)
        self.assertIsNotNone(kinds)
        assert kinds is not None
        self.assertEqual(set(re.findall(r"'([^']+)'", kinds.group(1))), {"backup_run", "backup_push", "backup_drill", "restore"})
        self.assertIn("(kind <> 'backup_run' OR detail->>'phase' = 'applied')", runs_sql)
        overrides = column_overrides(runs)
        self.assertEqual(overrides["hard_link_ratio"]["unit"], chart["fieldConfig"]["defaults"]["unit"])
        self.assertEqual(overrides["transferred_bytes"]["custom.width"], 170)


class EvalBoard(unittest.TestCase):
    def test_board_is_verbatim_and_only_applies_on_gpu_hosts(self) -> None:
        self.assertEqual(GrafanaEvalArtifact.emit(inputs()), inputs().templates[EVAL_TEMPLATE])
        self.assertEqual(GrafanaEvalArtifact.owners, ("grafana",))
        self.assertTrue(GrafanaEvalArtifact.applies(inputs()))
        self.assertFalse(GrafanaEvalArtifact.applies(inputs(no_gpu=True)))
        self.assertIn(GrafanaEvalArtifact, ARTIFACTS)
        self.assertNotIn(
            GrafanaEvalArtifact.relative_path,
            render_all(
                inputs(
                    no_gpu=True,
                    secrets={
                        "ldap_bind_password": "bind",
                        "postgres_openwebui_password": "postgres",
                        "gideon_admin_password": "admin",
                        "searxng_secret_key": "searxng",
                        "qdrant_api_key": "qdrant",
                        "qdrant_read_only_api_key": "qdrant-read-only",
                    },
                )
            ).by_path,
        )

    def test_eight_panels_show_only_recorded_fields(self) -> None:
        board = json.loads(GrafanaEvalArtifact.emit(inputs()))
        gpu_board = json.loads(GrafanaGpuArtifact.emit(inputs()))
        self.assertEqual(board["uid"], "gideon-eval")
        self.assertEqual(board["title"], "GIDEON Eval")
        self.assertNotIn("id", board)
        self.assertEqual(board["schemaVersion"], gpu_board["schemaVersion"])
        self.assertEqual(board["refresh"], gpu_board["refresh"])
        self.assertEqual(board["time"], {"from": "now-30d", "to": "now"})
        panels = {panel["title"]: panel for panel in board["panels"]}
        self.assertEqual(
            set(panels),
            {
                "Last run per suite and kind",
                "Nightly verdicts",
                "Guardrails: gate counts per family",
                "False refusal: harness and judge",
                "Run duration against the night",
                "Failed cases of newest nightly runs",
                "Proposals waiting",
                "Last eval trip",
            },
        )
        self.assertEqual(len(panels), len(board["panels"]))
        for panel in board["panels"][:6]:
            title = panel["title"]
            with self.subTest(panel=title):
                self.assertNotIn("id", panel)
                self.assertEqual(panel["datasource"], {"type": "postgres", "uid": "gideon-rows"})
                self.assertEqual(len(panel["targets"]), 1)
                target = panel["targets"][0]
                self.assertEqual(target["datasource"], panel["datasource"])
                self.assertEqual(target["format"], "table" if panel["type"] == "table" else "time_series")
                sql = target["rawSql"]
                tables = re.findall(r"\b(?:FROM|JOIN)\s+(eval_\w+)", sql, re.IGNORECASE)
                self.assertTrue(tables)
                self.assertTrue(set(tables) <= {"eval_runs", "eval_results"})
                self.assertNotRegex(sql, r"(?i)\b(?:question|answer|candidate|reason|prompt)\b")
                self.assertNotRegex(sql, r"(?i)\b(?:metrics|judge)\b(?!\s*->)")
                self.assertNotIn("AT TIME ZONE", sql.upper())

        last = panels["Last run per suite and kind"]["targets"][0]["rawSql"]
        for column in ("stack", "slice", "kind", "verdict", "partial", "forced", "started_at", "finished_at", "product_version", "eval_set_version", "run_id"):
            self.assertIn(column, last)
        self.assertIn("DISTINCT ON (stack, slice, kind)", last)
        self.assertIn("duration_seconds", last)

        verdicts = panels["Nightly verdicts"]["targets"][0]["rawSql"]
        self.assertIn("partial THEN -1", verdicts)
        self.assertIn("verdict = 'pass' THEN 1 ELSE 0", verdicts)
        self.assertIn("slice AS metric", verdicts)
        self.assertEqual(
            panels["Nightly verdicts"]["fieldConfig"]["defaults"]["custom"],
            {"drawStyle": "points", "showPoints": "always"},
        )

        for title in (
            "Nightly verdicts",
            "Guardrails: gate counts per family",
            "False refusal: harness and judge",
            "Run duration against the night",
        ):
            self.assertIn("$__timeFilter(", panels[title]["targets"][0]["rawSql"])

        failed = panels["Failed cases of newest nightly runs"]["targets"][0]["rawSql"]
        self.assertIn("DISTINCT ON (slice)", failed)
        self.assertIn("WHERE e.verdict = 'fail'", failed)
        for column in ("n.slice", "n.run_id", "e.case_id", "e.repeat", "family", "role", "class", "problem"):
            self.assertIn(column, failed)

        trip = panels["Last eval trip"]
        self.assertEqual(trip["type"], "stat")
        self.assertEqual(trip["gridPos"], {"h": 5, "w": 8, "x": 0, "y": 51})
        self.assertEqual(trip["options"]["colorMode"], "none")
        self.assertEqual(
            trip["targets"][0]["rawSql"],
            "SELECT EXTRACT(EPOCH FROM (now() - max(at))) AS age_seconds "
            "FROM guardrail_trips WHERE source = 'eval'",
        )

    def test_proposals_panel_reads_the_newest_tally_and_keeps_empty_lists_visible(self) -> None:
        board = json.loads(GrafanaEvalArtifact.emit(inputs()))
        panel = board["panels"][-2]
        self.assertEqual(panel["title"], "Proposals waiting")
        self.assertEqual(panel["type"], "table")
        self.assertEqual(panel["datasource"], {"type": "postgres", "uid": "gideon-rows"})
        sixth = board["panels"][-3]["gridPos"]
        self.assertEqual(panel["gridPos"]["y"], sixth["y"] + sixth["h"])
        self.assertEqual(panel["gridPos"]["w"], 24)
        self.assertEqual(panel["gridPos"]["x"], 0)
        self.assertEqual(len(panel["targets"]), 1)
        target = panel["targets"][0]
        self.assertEqual(target["datasource"], panel["datasource"])
        self.assertEqual(target["format"], "table")
        sql = target["rawSql"]
        self.assertEqual(re.findall(r"\bFROM\s+([a-z_]+)\b", sql, re.IGNORECASE), ["audit_log"])
        self.assertEqual(re.findall(r"\bkind\s*=\s*'([^']+)'", sql), [tally.TALLY_KIND])
        detail_keys = set(re.findall(r"\bdetail->>?'([^']+)'", sql))
        self.assertEqual(detail_keys, {"fired", "refused", "triggers"})
        self.assertTrue(detail_keys <= set(tally.DETAIL_KEYS))
        self.assertIn("ORDER BY at DESC", sql)
        self.assertIn("LIMIT 1", sql)
        self.assertIn("tally.at AS instant", sql)
        self.assertIn("::integer AS fired", sql)
        self.assertIn("::integer AS refused", sql)
        self.assertIn("(tally.detail->>'refused')::integer > 0", sql)
        self.assertIn("THEN 'incomplete' ELSE 'complete' END AS completeness", sql)
        self.assertIn("LEFT JOIN LATERAL jsonb_array_elements(tally.detail->'triggers')", sql)
        self.assertIn("AS trigger_row(entry) ON true", sql)
        for column in ("id", "state", "detail"):
            self.assertIn(f"trigger_row.entry->>'{column}' AS {column}", sql)

    def test_guardrails_counts_match_the_family_gate(self) -> None:
        board = json.loads(GrafanaEvalArtifact.emit(inputs()))
        panel = next(panel for panel in board["panels"] if panel["title"] == "Guardrails: gate counts per family")
        sql = panel["targets"][0]["rawSql"]
        self.assertIn("r.slice = 'guardrails'", sql)
        self.assertIn("r.kind = 'nightly'", sql)
        self.assertIn("r.stack = 'production'", sql)
        gated_families = sorted(
            category
            for suite, category in SHAPE_REGISTRY
            if suite == "guardrails" and category != TIER_2_CATEGORY
        )
        family_filter = "e.metrics->>'family' IN (" + ", ".join(
            f"'{family}'" for family in gated_families
        ) + ")"
        self.assertIn(family_filter, sql)
        self.assertIn("verdict AS run_verdict", sql)
        self.assertIn("door_class IS NOT NULL", sql)
        self.assertIn("door_class IN ('replaced', 'declined', 'disclaimed')", sql)
        self.assertIn("checks->>'must_not' = 'true'", sql)
        self.assertIn("checks->'must_not' IS NULL", sql)
        self.assertNotIn(" ? ", sql)
        self.assertIn("stream = 'leak' OR door_class = 'leak' OR frontend_class = 'leak'", sql)
        self.assertIn("problem IS NOT NULL", sql)
        self.assertIn("role = 'control' AND door_class = 'replaced'", sql)
        self.assertIn("frontend_agrees = 'false'", sql)
        divisor = re.search(r"COUNT\(\*\) FILTER \(WHERE role = 'control'\) / (\d+) AS ceiling", sql)
        self.assertIsNotNone(divisor)
        assert divisor is not None
        self.assertEqual(int(divisor.group(1)), OVER_TRIP_DIVISOR)

    def test_false_refusal_and_duration_follow_recorded_readings_and_night(self) -> None:
        board = json.loads(GrafanaEvalArtifact.emit(inputs()))
        panels = {panel["title"]: panel for panel in board["panels"]}
        refusal = panels["False refusal: harness and judge"]["targets"][0]["rawSql"]
        gated_families = sorted(
            category
            for suite, category in SHAPE_REGISTRY
            if suite == "guardrails" and category != TIER_2_CATEGORY
        )
        self.assertIn(
            "e.metrics->>'family' IN (" + ", ".join(f"'{family}'" for family in gated_families) + ")",
            refusal,
        )
        self.assertIn("e.metrics->>'class' = 'declined'", refusal)
        self.assertIn("e.judge->>'withheld' = 'true'", refusal)
        self.assertIn("e.metrics->>'class' IN ('declined', 'disclaimed')", refusal)
        self.assertIn("e.judge->>'withheld' IS NULL", refusal)
        duration = panels["Run duration against the night"]["targets"][0]["rawSql"]
        self.assertIn("finished_at - started_at", duration)
        self.assertIn("slice AS metric", duration)
        hours = re.search(r"(\d+) AS night_hours", duration)
        self.assertIsNotNone(hours)
        assert hours is not None
        start_hour = int(NIGHTLY_CALENDAR.split(" ")[1].split(":")[0])
        self.assertEqual(int(hours.group(1)), (QUIET_WINDOW_END_HOUR - start_hour) % 24)

    def test_grid_places_charts_and_gate_counts_in_screen_order_without_overlap(self) -> None:
        board = json.loads(GrafanaEvalArtifact.emit(inputs()))
        panels = board["panels"]
        by_title = {panel["title"]: panel["gridPos"] for panel in panels}
        nightly = by_title["Nightly verdicts"]
        duration = by_title["Run duration against the night"]
        gate = by_title["Guardrails: gate counts per family"]
        refusal = by_title["False refusal: harness and judge"]
        self.assertEqual((nightly["y"], nightly["h"]), (duration["y"], duration["h"]))
        self.assertEqual(duration["x"], nightly["x"] + nightly["w"])
        self.assertEqual(nightly["w"] + duration["w"], GRID_COLUMNS)
        self.assertEqual((gate["x"], gate["w"]), (0, GRID_COLUMNS))
        self.assertEqual(gate["y"], nightly["y"] + nightly["h"])
        self.assertEqual(refusal["y"], gate["y"] + gate["h"])
        self.assertEqual(
            [panel["title"] for panel in panels],
            [panel["title"] for panel in sorted(panels, key=lambda panel: (panel["gridPos"]["y"], panel["gridPos"]["x"]))],
        )
        row_widths: dict[int, int] = {}
        for panel in panels:
            pos = panel["gridPos"]
            self.assertGreaterEqual(pos["x"], 0)
            self.assertLessEqual(pos["x"] + pos["w"], GRID_COLUMNS)
            row_widths[pos["y"]] = row_widths.get(pos["y"], 0) + pos["w"]
        self.assertTrue(all(width <= GRID_COLUMNS for width in row_widths.values()))
        for left, right in combinations(panels, 2):
            a = left["gridPos"]
            b = right["gridPos"]
            overlap = (
                a["x"] < b["x"] + b["w"]
                and b["x"] < a["x"] + a["w"]
                and a["y"] < b["y"] + b["h"]
                and b["y"] < a["y"] + a["h"]
            )
            self.assertFalse(overlap, (left["title"], right["title"]))

    def test_all_backup_and_eval_table_headers_fit_without_sideways_scroll(self) -> None:
        for artifact in (GrafanaBackupArtifact, GrafanaEvalArtifact):
            board = json.loads(artifact.emit(inputs()))
            for panel in board["panels"]:
                if panel["type"] != "table":
                    continue
                with self.subTest(board=board["uid"], panel=panel["title"]):
                    columns = outer_select_columns(panel["targets"][0]["rawSql"])
                    widths = table_column_widths(panel, columns)
                    self.assertEqual(len(widths), len(columns))
                    self.assertLessEqual(sum(widths.values()), table_inner_width(panel))
                    for name in columns:
                        with self.subTest(column=name):
                            self.assertLessEqual(
                                len(name) * HEADER_CHAR_WIDTH_BOUND,
                                widths[name] - HEADER_TEXT_INSET,
                            )


class Alerting(unittest.TestCase):
    def test_contact_point_uses_each_sites_recipients_and_subject(self) -> None:
        for site_path in (EXAMPLE, SECOND):
            with self.subTest(site=site_path.name):
                site_inputs = inputs(site_path)
                text = GrafanaContactPointsArtifact().emit(site_inputs)
                document = yaml.safe_load(text)
                contact = document["contactPoints"][0]
                receiver = contact["receivers"][0]
                self.assertEqual([item["name"] for item in document["contactPoints"]], ["page", "nudge"])
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
                page_message = receiver["settings"]["message"]
                self.assertEqual(
                    {action.strip() for action in re.findall(r"\{\{(.*?)\}\}", page_message)},
                    {
                        "range .Alerts",
                        ".Status | toUpper",
                        ".Labels.alertname",
                        ".Annotations.summary",
                        "with .Annotations.runbook",
                        ".",
                        "end",
                    },
                )
                self.assertNotIn(".Values", page_message)
                self.assertNotIn("URL", page_message)
                message_lines = page_message.rstrip("\n").splitlines()
                self.assertEqual(
                    message_lines[-1],
                    "What to do: on the box, run gideon status; the full steps are in the runbook named above.",
                )
                self.assertEqual(message_lines[-2], "{{ end }}")
                recipients = ";".join(site_inputs.site.alerts.recipients)
                page_text = (
                    "apiVersion: 1\ncontactPoints:\n"
                    "  - orgId: 1\n    name: page\n    receivers:\n"
                    "      - uid: page-email\n        type: email\n        settings:\n"
                    f'          addresses: "{recipients}"\n'
                    "          singleEmail: true\n"
                    f'          subject: "[GIDEON {site_inputs.site.office.short_name}] '
                    '{{ .Status | toUpper }}: {{ .CommonLabels.alertname }}"\n'
                    "          message: |\n"
                    "            {{ range .Alerts }}{{ .Status | toUpper }}: {{ .Labels.alertname }}: "
                    "{{ .Annotations.summary }}{{ with .Annotations.runbook }} "
                    "(runbook: {{ . }}){{ end }}\n"
                    "            {{ end }}\n"
                    "            What to do: on the box, run gideon status; "
                    "the full steps are in the runbook named above.\n"
                )
                self.assertEqual(text.split("  - orgId: 1\n    name: nudge", 1)[0], page_text)

                nudge = document["contactPoints"][1]
                self.assertEqual(nudge["orgId"], 1)
                self.assertEqual(nudge["name"], "nudge")
                self.assertEqual(len(nudge["receivers"]), 1)
                nudge_receiver = nudge["receivers"][0]
                self.assertEqual(nudge_receiver["uid"], "nudge-email")
                self.assertEqual(nudge_receiver["type"], "email")
                self.assertTrue(nudge_receiver["disableResolveMessage"])
                settings = nudge_receiver["settings"]
                self.assertEqual(settings["addresses"], ";".join(site_inputs.site.alerts.recipients))
                self.assertTrue(settings["singleEmail"])
                self.assertEqual(settings["subject"], f"[GIDEON {site_inputs.site.office.short_name}] Proposals waiting")
                message = settings["message"]
                self.assertEqual(
                    message,
                    "Proposals waiting on this box: {{ (index .Alerts 0).Values.B }}\n"
                    "Read them with gideon proposals.\n",
                )
                self.assertEqual(
                    [expression.strip() for expression in re.findall(r"{{(.*?)}}", message)],
                    ["(index .Alerts 0).Values.B"],
                )
                self.assertNotIn(".Values.C", message)
                self.assertNotIn(".Labels", message)
                self.assertNotIn(".Annotations", message)

    def test_policy_and_time_interval_use_the_site_timezone(self) -> None:
        for site_path in (EXAMPLE, SECOND):
            with self.subTest(site=site_path.name):
                site_inputs = inputs(site_path)
                policy = yaml.safe_load(GrafanaPoliciesArtifact().emit(site_inputs))["policies"][0]
                self.assertEqual(len(policy["routes"]), 2)
                child = policy["routes"][0]
                self.assertEqual(policy["receiver"], "page")
                self.assertEqual(policy["group_by"], ["alertname"])
                self.assertEqual(child["repeat_interval"], "6d")
                self.assertEqual(child["active_time_intervals"], ["saturday-morning"])
                self.assertFalse(child["continue"])
                nudge_route = policy["routes"][1]
                self.assertEqual(nudge_route["receiver"], "nudge")
                self.assertEqual(nudge_route["object_matchers"], [["nudge", "=", "true"]])
                self.assertEqual(nudge_route["repeat_interval"], "6d")
                self.assertEqual(nudge_route["active_time_intervals"], ["monday-morning"])
                self.assertFalse(nudge_route["continue"])
                # `muteTimes` is the provisioning key for time intervals; a
                # route uses one as an active window, not only as a mute.
                intervals = yaml.safe_load(GrafanaTimeIntervalsArtifact().emit(site_inputs))["muteTimes"]
                self.assertEqual([item["name"] for item in intervals], ["saturday-morning", "monday-morning"])
                interval = intervals[0]
                entry = interval["time_intervals"][0]
                self.assertEqual(entry["weekdays"], ["saturday"])
                self.assertEqual(entry["times"], [{"start_time": "08:00", "end_time": "09:00"}])
                self.assertEqual(entry["location"], site_inputs.site.office.timezone)
                monday = intervals[1]["time_intervals"][0]
                self.assertEqual(monday["weekdays"], ["monday"])
                self.assertEqual(monday["times"], [{"start_time": "08:00", "end_time": "09:00"}])
                self.assertEqual(monday["location"], site_inputs.site.office.timezone)

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
            "gideon-opensearch-probe-failing",
            "gideon-systemd-collector-failed",
            "gideon-data-volume-low",
            "gideon-host-filesystem-low",
            "gideon-tls-expiring",
            "gideon-driver-drift",
            "gideon-engine-down",
            "gideon-heartbeat",
            "gideon-proposals-waiting",
            "gideon-api-probe-failing",
            "gideon-nightly-run-failed",
            "gideon-nightly-run-aborted",
            "gideon-nightly-run-overdue",
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
                self.assertIn(
                    f'probe_success{{job="{API_JOB_NAME}"}}',
                    rules["gideon-api-probe-failing"]["data"][0]["model"]["expr"],
                )
                for rule in rules.values():
                    self.assertIn(rule["condition"], {item["refId"] for item in rule["data"]})
                    self.assertFalse(rule["isPaused"])
                    if rule["uid"] == "gideon-proposals-waiting":
                        self.assertEqual(rule["labels"], {"class": "dashboard", "nudge": "true"})
                        self.assertEqual(rule["annotations"]["runbook"], "docs/runbooks/observability.md §10")
                    else:
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

    def test_proposals_rule_uses_the_newest_tally_count_and_reduced_value(self) -> None:
        for site_path, no_gpu in ((EXAMPLE, False), (SECOND, False), (EXAMPLE, True)):
            with self.subTest(site=site_path.name, no_gpu=no_gpu):
                groups = yaml.safe_load(
                    GrafanaRulesArtifact().emit(inputs(site_path, no_gpu=no_gpu))
                )["groups"]
                rows = next(group for group in groups if group["name"] == "rows")
                rule = next(
                    item for item in rows["rules"]
                    if item["uid"] == "gideon-proposals-waiting"
                )
                if not no_gpu:
                    nightly_index = next(
                        index for index, item in enumerate(rows["rules"])
                        if item["uid"] == "gideon-nightly-run-failed"
                    )
                    self.assertLess(rows["rules"].index(rule), nightly_index)
                self.assertEqual(rule["title"], "Proposals waiting")
                self.assertEqual(rule["condition"], "C")
                self.assertEqual(rule["for"], "0s")
                self.assertEqual(rule["noDataState"], "OK")
                self.assertEqual(rule["execErrState"], "OK")
                self.assertEqual(rule["labels"], {"class": "dashboard", "nudge": "true"})
                self.assertNotEqual(rule["labels"]["class"], "page")
                self.assertEqual(rule["annotations"], {
                    "summary": "Proposals are waiting for review",
                    "runbook": "docs/runbooks/observability.md §10",
                })
                self.assertNotIn("{{", json.dumps(rule["annotations"]))
                self.assertNotIn("$", json.dumps(rule["annotations"]))

                query, reduce, threshold = rule["data"]
                self.assertEqual(query["refId"], "A")
                self.assertEqual(query["datasourceUid"], "gideon-rows")
                self.assertEqual(query["model"]["datasource"]["uid"], "gideon-rows")
                self.assertEqual(query["model"]["format"], "table")
                sql = query["model"]["rawSql"]
                self.assertEqual(
                    re.findall(r"\b(?:FROM|JOIN)\s+(\w+)", sql, re.IGNORECASE),
                    ["audit_log"],
                )
                self.assertEqual(re.findall(r"WHERE kind = '([^']+)'", sql), [tally.TALLY_KIND])
                self.assertEqual(re.findall(r"detail->>'([^']+)'", sql), ["fired"])
                self.assertIn("fired", tally.DETAIL_KEYS)
                self.assertIn("AS value", sql)
                self.assertIn("ORDER BY at DESC LIMIT 1", sql)
                self.assertEqual(reduce["refId"], "B")
                self.assertEqual(reduce["model"]["type"], "reduce")
                self.assertEqual(reduce["model"]["expression"], "A")
                self.assertEqual(reduce["model"]["reducer"], "last")
                self.assertEqual(threshold["refId"], "C")
                self.assertEqual(threshold["model"]["type"], "threshold")
                self.assertEqual(threshold["model"]["expression"], "B")
                self.assertEqual(
                    threshold["model"]["conditions"][0]["evaluator"],
                    {"params": [0], "type": "gt"},
                )

    def test_nightly_rules_cover_each_suite_and_only_gpu_hosts(self) -> None:
        nightly_uids = {
            "gideon-nightly-run-failed",
            "gideon-nightly-run-aborted",
            "gideon-nightly-run-overdue",
        }
        uids_by_host: dict[bool, set[str]] = {}
        for no_gpu in (False, True):
            with self.subTest(no_gpu=no_gpu):
                groups = yaml.safe_load(GrafanaRulesArtifact().emit(inputs(no_gpu=no_gpu)))[
                    "groups"
                ]
                uids_by_host[no_gpu] = {
                    rule["uid"] for group in groups for rule in group["rules"]
                }
                nightly = {
                    rule["uid"]: rule
                    for group in groups
                    for rule in group["rules"]
                    if rule["uid"] in nightly_uids
                }
                self.assertEqual(set(nightly), set() if no_gpu else nightly_uids)
                if no_gpu:
                    continue
                rows = next(group for group in groups if group["name"] == "rows")
                for uid, rule in nightly.items():
                    self.assertIn(rule, rows["rules"])
                    self.assertEqual(rule["condition"], "C")
                    self.assertEqual(rule["for"], "0s")
                    self.assertEqual(rule["labels"], {"class": "page"})
                    self.assertEqual(rule["annotations"]["runbook"], "docs/runbooks/observability.md §4")
                    self.assertEqual(rule["annotations"]["summary"].count("$"), 1)
                    self.assertIn("{{ $labels.slice }}", rule["annotations"]["summary"])
                    query = rule["data"][0]
                    self.assertEqual(query["datasourceUid"], "gideon-rows")
                    self.assertEqual(query["model"]["datasource"]["uid"], "gideon-rows")
                    self.assertEqual(query["model"]["format"], "table")
                    sql = query["model"]["rawSql"]
                    self.assertEqual(
                        re.findall(r"\('([^']+)'\)", sql), list(NIGHTLY_SUITES)
                    )
                    self.assertIn("SELECT suites.slice AS slice,", sql)
                    self.assertIn("AS value", sql)
                    self.assertIn("FROM suites", sql)
                    self.assertIn("LEFT JOIN LATERAL", sql)
                    self.assertIn("FROM eval_runs", sql)
                    self.assertIn("kind = 'nightly'", sql)
                    self.assertIn("stack = 'production'", sql)
                    self.assertIn("slice = suites.slice", sql)
                    self.assertIn("ORDER BY started_at DESC LIMIT 1", sql)
                    self.assertEqual(rule["data"][1]["model"]["expression"], "A")
                    threshold = rule["data"][2]["model"]["conditions"][0]["evaluator"]
                    self.assertEqual(threshold["type"], "gt")
                    if uid == "gideon-nightly-run-overdue":
                        self.assertEqual(threshold["params"], [NIGHTLY_OVERDUE_SECONDS])
                        self.assertIn(f", {NIGHTLY_OVERDUE_SECONDS + 1}) AS value", sql)
                        self.assertEqual(rule["noDataState"], "Alerting")
                        self.assertEqual(rule["execErrState"], "Alerting")
                    else:
                        self.assertEqual(threshold["params"], [0])
                        self.assertIn("ELSE 0 END AS value", sql)
                        self.assertEqual(rule["noDataState"], "OK")
                        self.assertEqual(rule["execErrState"], "OK")
                self.assertIn("NOT latest.partial", nightly["gideon-nightly-run-failed"]["data"][0]["model"]["rawSql"])
                self.assertIn("WHEN latest.partial", nightly["gideon-nightly-run-aborted"]["data"][0]["model"]["rawSql"])
        gpu_only_rules = nightly_uids | {
            "gideon-engine-down",
            "gideon-driver-drift",
            "gideon-api-probe-failing",
        }
        self.assertEqual(uids_by_host[True], uids_by_host[False] - gpu_only_rules)

    def test_host_filesystem_rule_covers_both_mountpoints_and_reuses_data_threshold(self) -> None:
        """Each rendered host filesystem page follows its exact contract."""

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
        self.assertIn("Requests waiting and running", panels)
        self.assertIn("Answer speed", panels)
        self.assertNotIn("Queue depth", panels)
        self.assertEqual(
            panels["KV-cache usage"]["fieldConfig"]["defaults"]["unit"],
            "percentunit",
        )
        self.assertEqual(panels["KV-cache usage"]["fieldConfig"]["defaults"]["min"], 0)
        self.assertEqual(panels["KV-cache usage"]["fieldConfig"]["defaults"]["max"], 1)
        engine_titles = (
            "KV-cache usage",
            "Requests waiting and running",
            "Answer speed",
        )
        speed_expr = (
            "sum by (model_name) "
            f"(increase(vllm:inter_token_latency_seconds_count{{job=\"{ENGINE_JOB_NAME}\"}}[$__range])) / "
            "(sum by (model_name) "
            f"(increase(vllm:inter_token_latency_seconds_sum{{job=\"{ENGINE_JOB_NAME}\"}}[$__range])) > 0)"
        )
        self.assertEqual(
            {
                target["expr"]
                for title in engine_titles
                for target in panels[title]["targets"]
            },
            {
                f'vllm:kv_cache_usage_perc{{job="{ENGINE_JOB_NAME}"}}',
                f'vllm:num_requests_waiting{{job="{ENGINE_JOB_NAME}"}}',
                f'vllm:num_requests_running{{job="{ENGINE_JOB_NAME}"}}',
                speed_expr,
            },
        )
        for title in engine_titles:
            for target in panels[title]["targets"]:
                with self.subTest(title=title, target=target["refId"]):
                    series = re.findall(r"vllm:[a-z_]+(?:\{[^}]*\})?", target["expr"])
                    self.assertTrue(series)
                    self.assertTrue(
                        all(series_name.endswith(f'{{job="{ENGINE_JOB_NAME}"}}') for series_name in series)
                    )
        self.assertEqual(
            [panels[title]["gridPos"] for title in engine_titles],
            [{"h": 8, "w": 8, "x": x, "y": 16} for x in (0, 8, 16)],
        )
        speed = panels["Answer speed"]
        self.assertEqual(speed["type"], "stat")
        self.assertEqual(speed["fieldConfig"]["defaults"]["unit"], "suffix: tokens/s")
        self.assertEqual(speed["fieldConfig"]["defaults"]["decimals"], 1)
        self.assertEqual(speed["options"]["colorMode"], "none")
        self.assertEqual(speed["options"]["graphMode"], "none")
        self.assertEqual(speed["options"]["reduceOptions"]["calcs"], ["lastNotNull"])
        self.assertEqual(speed["targets"][0]["legendFormat"], "{{model_name}}")
        self.assertTrue(speed["targets"][0]["instant"])
        for title in engine_titles:
            with self.subTest(panel=title):
                panel = panels[title]
                self.assertNotIn("id", panel)
                self.assertEqual(panel["datasource"]["uid"], "prometheus")
                for target in panel["targets"]:
                    self.assertEqual(target["datasource"]["uid"], "prometheus")
        for title in engine_titles[:2]:
            self.assertEqual(panels[title]["type"], "timeseries")

    def test_gpu_board_memory_uses_mebibyte_unit(self) -> None:
        dashboard = json.loads(
            (ROOT / "compose/grafana/dashboards/gpu.json").read_text(encoding="utf-8")
        )
        memory = next(
            panel for panel in dashboard["panels"] if panel["title"] == "GPU memory"
        )
        self.assertEqual(
            {gpu_aggregated_field(target["expr"]) for target in memory["targets"]},
            {"DCGM_FI_DEV_FB_USED", "DCGM_FI_DEV_FB_FREE"},
        )
        self.assertEqual(memory["fieldConfig"]["defaults"]["unit"], "mbytes")
        rows = dcgm_counter_rows()
        for target in memory["targets"]:
            self.assertIn("MiB", rows[gpu_aggregated_field(target["expr"])][1])

    def test_gpu_board_temperature_limit_is_dashed(self) -> None:
        dashboard = json.loads(
            (ROOT / "compose/grafana/dashboards/gpu.json").read_text(encoding="utf-8")
        )
        temperature = next(
            panel for panel in dashboard["panels"] if panel["title"] == "GPU temperature"
        )
        targets = temperature["targets"]
        self.assertEqual(
            [gpu_aggregated_field(target["expr"]) for target in targets],
            ["DCGM_FI_DEV_GPU_TEMP", "DCGM_FI_DEV_GPU_MAX_OP_TEMP"],
        )
        self.assertTrue(targets[1]["legendFormat"].endswith(" limit"))
        overrides = temperature["fieldConfig"]["overrides"]
        self.assertEqual(len(overrides), 1)
        matcher = overrides[0]["matcher"]
        self.assertEqual(matcher["id"], "byRegexp")
        pattern = matcher["options"]
        self.assertTrue(pattern.startswith("/") and pattern.endswith("/"))
        self.assertIsNone(re.search(pattern[1:-1], targets[0]["legendFormat"]))
        self.assertIsNotNone(re.search(pattern[1:-1], targets[1]["legendFormat"]))
        self.assertEqual(
            overrides[0]["properties"],
            [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}],
        )
        self.assertEqual(dcgm_counter_rows()[gpu_aggregated_field(targets[1]["expr"])][0], "gauge")

    def test_dcgm_fields_read_by_boards_and_rules_are_collected(self) -> None:
        dashboard_paths = (ROOT / "compose/grafana/dashboards").rglob("*.json")
        alert_paths = (ROOT / "compose/grafana/provisioning/alerting").rglob("*.tmpl")
        referenced = {
            name
            for path in (*dashboard_paths, *alert_paths)
            for name in re.findall(
                r"DCGM_FI_[A-Z0-9_]+", path.read_text(encoding="utf-8")
            )
        }
        rows = dcgm_counter_rows()
        self.assertTrue(referenced)
        self.assertEqual(referenced - rows.keys(), set())
        self.assertEqual(rows["DCGM_FI_DRIVER_VERSION"][0], "label")

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
                        "qdrant_api_key": "qdrant",
                        "qdrant_read_only_api_key": "qdrant-read-only",
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
        self.assertEqual(driver_panel["targets"][0]["legendFormat"], "{{DCGM_FI_DRIVER_VERSION}}")
        self.assertEqual(driver_panel["options"]["textMode"], "name")
