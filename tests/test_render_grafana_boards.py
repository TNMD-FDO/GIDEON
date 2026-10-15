"""Grafana boards held at render."""

import json
import math
import re
import unittest
from itertools import combinations, pairwise
from typing import Any

import yaml  # type: ignore[import-untyped]
from test_render_grafana import EXAMPLE, ROOT, SECOND, _datasource_uids, inputs
from test_render_grafana_runbook import render_tile_facts

from gideon.evaluation.evalset import SHAPE_REGISTRY, TIER_2_CATEGORY
from gideon.evaluation.guardrails_slice import OVER_TRIP_DIVISOR
from gideon.evaluation.window import QUIET_WINDOW_END_HOUR
from gideon.host.render import ARTIFACTS, VerbatimArtifact, render_all
from gideon.host.render.engine import ENGINE_JOB_NAME
from gideon.host.render.grafana import (
    BACKUP_TEMPLATE,
    CORPUS_TEMPLATE,
    DATASOURCES_TEMPLATE,
    DRILL_MAX_GAP_DAYS,
    EVAL_TEMPLATE,
    GrafanaBackupArtifact,
    GrafanaCorpusArtifact,
    GrafanaEvalArtifact,
    GrafanaGpuArtifact,
    GrafanaOverviewArtifact,
)
from gideon.host.render.systemd import NIGHTLY_CALENDAR
from gideon.host.site import FIELD_REGISTRY
from gideon.improvement import tally
from tools.boards.page import GRID_CELL_HEIGHT, GRID_CELL_MARGIN, VIEWPORT_WIDTH

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

# The pinned Grafana's stat panel, read from its source: no panel padding, so
# the tiles fill the card inside its border and title bar.
STAT_BORDER = 2  # a 1 px border on each side
STAT_TITLE_BAR = 40  # the title bar above the tiles
STAT_ITEM_SPACING = 3  # the gap between tiles
STAT_LINE_HEIGHT = 1.2  # a title or value line per pixel of font size
STAT_TILE_PADDING_HEIGHT = 100  # a tile taller than this takes the larger padding
STAT_TILE_PADDING_SMALL = 8
STAT_TILE_PADDING_LARGE = 12
TITLE_CHAR_WIDTH_BOUND = 9  # an upper bound per 14 px title character of ten or more, not a glyph metric


def _square(pw: float, ph: float, count: int) -> float:
    """Return the side of the largest square that fits the count along one orientation."""

    parts = math.ceil(math.sqrt(count * pw / ph))
    if math.floor(parts * ph / pw) * parts < count:
        return ph / math.ceil(parts * ph / pw)
    return pw / parts


def stat_inner(grid_w: int, grid_h: int) -> tuple[int, int]:
    """Resolve a stat card's tile area in the board check's kiosk viewport."""

    column = (VIEWPORT_WIDTH - KIOSK_SIDE_PADDING - GRID_CELL_MARGIN * (GRID_COLUMNS - 1)) / GRID_COLUMNS
    card_w = math.floor(column * grid_w + GRID_CELL_MARGIN * (grid_w - 1) + 0.5)  # JavaScript's Math.round
    card_h = (GRID_CELL_HEIGHT + GRID_CELL_MARGIN) * grid_h - GRID_CELL_MARGIN
    return card_w - STAT_BORDER, card_h - STAT_TITLE_BAR - STAT_BORDER


def stat_tiles(grid_w: int, grid_h: int, count: int) -> dict[str, int | float]:
    """Lay the count's tiles out in rows, a short last row's tiles wider."""

    pw, ph = stat_inner(grid_w, grid_h)
    square = max(_square(pw, ph, count), _square(ph, pw, count))
    cols = math.floor(pw / square)
    rows = math.ceil(count / cols)
    cols = math.ceil(count / rows)
    last = cols - (cols * rows - count)
    spacing = STAT_ITEM_SPACING
    return {
        "cols": cols,
        "rows": rows,
        "last_row_items": last,
        "width": pw / cols - spacing + spacing / cols,
        "height": ph / rows - spacing + spacing / rows,
        "last_row_width": pw / last - spacing + spacing / last,
    }


def tile_padding(tile_height: float) -> int:
    """Return a tile's padding on each side, larger past the height step."""

    return STAT_TILE_PADDING_LARGE if tile_height > STAT_TILE_PADDING_HEIGHT else STAT_TILE_PADDING_SMALL


def title_room(tile_width: float, tile_height: float) -> float:
    """Return the width a tile's title line has inside its padding."""

    return tile_width - 2 * tile_padding(tile_height)


def tile_content_height(title_lines: int, value_size: int = 28, title_size: int = 14) -> float:
    """Return the height of a tile's title lines and its value line."""

    return title_lines * title_size * STAT_LINE_HEIGHT + value_size * STAT_LINE_HEIGHT


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


def _leaf_panels(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    """Every panel but a row, the panels nested under a row included."""

    leaves = []
    for panel in dashboard["panels"]:
        if panel["type"] == "row":
            leaves.extend(panel.get("panels", []))
        else:
            leaves.append(panel)
    return leaves


class Dashboards(unittest.TestCase):
    def test_overview_tiles_hold_every_scraped_name(self) -> None:
        for label, site_path, no_gpu in (
            ("example", EXAMPLE, False),
            ("second office", SECOND, False),
            ("no-GPU", EXAMPLE, True),
        ):
            with self.subTest(host=label):
                site_inputs = inputs(site_path, no_gpu=no_gpu)
                facts = render_tile_facts(site_inputs, label)
                self.assertEqual(facts.findings, ())
                board = json.loads(GrafanaOverviewArtifact().emit(site_inputs))
                panel = next(panel for panel in board["panels"] if panel["title"] == "Services and probes")
                text = panel["options"]["text"]
                self.assertIn("titleSize", text)
                self.assertEqual(text["titleSize"], 14)
                grid = stat_tiles(panel["gridPos"]["w"], panel["gridPos"]["h"], len(facts.tiles))
                # Every name must fit the ordinary tile, whichever row gets it;
                # a short last row only gives its tiles more room.
                room = title_room(grid["width"], grid["height"])
                for tile in facts.tiles:
                    with self.subTest(tile=tile.name):
                        lines = 1
                        line = ""
                        # The browser breaks a title at a space or after a hyphen,
                        # and a segment wider than the room runs under the next tile.
                        for segment in re.split(r"(?<=-)|(?<= )", tile.name):
                            self.assertLessEqual(len(segment.rstrip()) * TITLE_CHAR_WIDTH_BOUND, room, tile.name)
                            if line and len((line + segment).rstrip()) * TITLE_CHAR_WIDTH_BOUND > room:
                                lines += 1
                                line = segment
                            else:
                                line += segment
                        self.assertLessEqual(
                            tile_content_height(lines, text["valueSize"], text["titleSize"]),
                            grid["height"] - 2 * tile_padding(grid["height"]),
                            tile.name,
                        )

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

        for grid_w, grid_h, cols, rows, last, width, height, last_width, room in (
            (12, 8, 8, 3, 7, 94.625, 82.667, 108.571, 78.625),
            (24, 8, 12, 2, 11, 127.75, 125.5, 139.636, 103.75),
            (24, 12, 8, 3, 7, 193.125, 133.333, 221.143, 169.125),
        ):
            with self.subTest(grid_w=grid_w, grid_h=grid_h):
                grid = stat_tiles(grid_w, grid_h, 23)
                self.assertEqual((grid["cols"], grid["rows"], grid["last_row_items"]), (cols, rows, last))
                self.assertAlmostEqual(grid["width"], width, places=3)
                self.assertAlmostEqual(grid["height"], height, places=3)
                self.assertAlmostEqual(grid["last_row_width"], last_width, places=3)
                self.assertAlmostEqual(title_room(grid["width"], grid["height"]), room, places=3)
        self.assertAlmostEqual(tile_content_height(1), 50.4)
        self.assertAlmostEqual(tile_content_height(2), 67.2)

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

    def test_every_board_table_headers_fit_without_sideways_scroll(self) -> None:
        for artifact in (GrafanaBackupArtifact, GrafanaEvalArtifact, GrafanaCorpusArtifact):
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


class CorpusBoard(unittest.TestCase):
    def test_board_is_verbatim_and_renders_on_every_host(self) -> None:
        self.assertIsInstance(GrafanaCorpusArtifact, VerbatimArtifact)
        self.assertEqual(GrafanaCorpusArtifact.name, "grafana-corpus")
        self.assertEqual(GrafanaCorpusArtifact.owners, ("grafana",))
        self.assertIn(GrafanaCorpusArtifact, ARTIFACTS)
        for no_gpu in (False, True):
            with self.subTest(no_gpu=no_gpu):
                site_inputs = inputs(
                    no_gpu=no_gpu,
                    secrets={
                        "ldap_bind_password": "bind",
                        "postgres_openwebui_password": "postgres",
                        "gideon_admin_password": "admin",
                        "searxng_secret_key": "searxng",
                        "qdrant_api_key": "qdrant",
                        "qdrant_read_only_api_key": "qdrant-read-only",
                        "gideon_api_key": "api",
                    },
                )
                self.assertTrue(GrafanaCorpusArtifact.applies(site_inputs))
                self.assertEqual(
                    GrafanaCorpusArtifact.emit(site_inputs),
                    site_inputs.templates[CORPUS_TEMPLATE],
                )
                self.assertIn(GrafanaCorpusArtifact.relative_path, render_all(site_inputs).by_path)

    def test_nineteen_panels_cover_the_record_embed_stores_and_worker(self) -> None:
        board = json.loads(GrafanaCorpusArtifact.emit(inputs()))
        eval_board = json.loads(GrafanaEvalArtifact.emit(inputs()))
        self.assertEqual(board["uid"], "gideon-corpus")
        self.assertEqual(board["title"], "GIDEON Corpus")
        self.assertEqual(board["tags"], ["GIDEON", "corpus"])
        self.assertNotIn("id", board)
        self.assertEqual(board["time"], {"from": "now-24h", "to": "now"})
        self.assertEqual(board["schemaVersion"], eval_board["schemaVersion"])
        self.assertEqual(board["refresh"], eval_board["refresh"])
        self.assertEqual(
            [panel["title"] for panel in board["panels"]],
            [
                "Generations",
                "Documents by status",
                "Documents by text source",
                "Documents by precedential status",
                "Failures by reason",
                "Anchors, agreement, and the accounted gate",
                "Build wall clock",
                "Embedding progress",
                "Embedding requests waiting and running",
                "Embedding cache use",
                "Tokens embedded per second",
                "Embedding requests per second",
                "Embedding request latency",
                "Store memory",
                "Documents indexed per second",
                "Collections",
                "Store work in flight",
                "Worker queue depth",
                "Worker heartbeat and slots",
            ],
        )
        self.assertEqual([panel["id"] for panel in board["panels"]], list(range(1, 20)))

    def test_rows_panels_read_only_recorded_counts_and_ids(self) -> None:
        board = json.loads(GrafanaCorpusArtifact.emit(inputs()))
        panels = [panel for panel in board["panels"] if panel["datasource"]["uid"] == "gideon-rows"]
        self.assertEqual(len(panels), 8)
        for panel in panels:
            with self.subTest(panel=panel["title"]):
                self.assertEqual(panel["type"], "table")
                self.assertEqual(panel["datasource"], {"type": "postgres", "uid": "gideon-rows"})
                self.assertEqual(len(panel["targets"]), 1)
                target = panel["targets"][0]
                self.assertEqual(target["datasource"], panel["datasource"])
                self.assertEqual(target["format"], "table")
                sql = target["rawSql"]
                sources = set(re.findall(r"\b(?:FROM|JOIN)\s+(?!LATERAL\b)(\w+)", sql, re.IGNORECASE))
                self.assertTrue(sources & {"build_reports", "index_generations"})
                self.assertTrue(sources <= {"build_reports", "index_generations", "newest"})
                self.assertNotRegex(sql, r"(?i)\b(?:text|caption|cites|slice|content)\b")
                self.assertNotIn("AT TIME ZONE", sql.upper())

    def test_embed_series_use_only_the_embedding_server_and_describe_empty_hosts(self) -> None:
        board = json.loads(GrafanaCorpusArtifact.emit(inputs()))
        embed_panels = []
        for panel in board["panels"]:
            for target in panel["targets"]:
                expr = target.get("expr", "")
                if "vllm:" not in expr:
                    continue
                with self.subTest(panel=panel["title"], target=target["refId"]):
                    self.assertEqual(panel["datasource"], {"type": "prometheus", "uid": "prometheus"})
                    self.assertEqual(target["datasource"], panel["datasource"])
                    series = re.findall(r"vllm:[a-z0-9_]+(?:\{[^}]*\})?", expr)
                    self.assertTrue(series)
                    self.assertTrue(all(name.endswith('{job="embed"}') for name in series))
            if any("vllm:" in target.get("expr", "") for target in panel["targets"]):
                embed_panels.append(panel)
                description = panel["description"]
                self.assertIn("empty", description)
                self.assertIn("no-GPU host", description)
                self.assertIn("no embedding server runs", description)
        self.assertEqual(len(embed_panels), 5)

    def test_progress_rate_divides_recorded_tokens_by_nonzero_seconds(self) -> None:
        board = json.loads(GrafanaCorpusArtifact.emit(inputs()))
        panel = next(panel for panel in board["panels"] if panel["title"] == "Embedding progress")
        sql = panel["targets"][0]["rawSql"]
        self.assertEqual(
            outer_select_columns(sql),
            ["gen_id", "scope", "written", "present", "copied", "embedded", "tokens", "requests", "seconds", "indexed", "tokens_per_second"],
        )
        for key in ("scope", "present", "copied", "embedded", "tokens", "requests", "seconds", "indexed"):
            self.assertIn(f"(counts->>'dense_{key}')::bigint AS {key}", sql)
        self.assertIn(
            "(counts->>'dense_copied')::bigint + (counts->>'dense_embedded')::bigint AS written",
            sql,
        )
        self.assertIn(
            "(counts->>'dense_tokens')::numeric / NULLIF((counts->>'dense_seconds')::numeric, 0) AS tokens_per_second",
            sql,
        )
        self.assertNotIn("COALESCE", sql.upper())

    def test_store_node_series_are_aggregated_and_heap_limit_is_dashed(self) -> None:
        board = json.loads(GrafanaCorpusArtifact.emit(inputs()))
        panels = {panel["title"]: panel for panel in board["panels"]}
        node_series = []
        for title in ("Store memory", "Store work in flight"):
            for target in panels[title]["targets"]:
                if "opensearch_" in target["expr"]:
                    node_series.append(target)
                    with self.subTest(panel=title, target=target["refId"]):
                        self.assertRegex(target["expr"], r'^max\(opensearch_[a-z_]+\{[^}]*\}\)$')
                        self.assertNotIn("{{node}}", target["legendFormat"])
        self.assertEqual(len(node_series), 4)
        memory = panels["Store memory"]
        limit = next(target for target in memory["targets"] if "heap_max_bytes" in target["expr"])
        self.assertTrue(limit["legendFormat"].endswith(" limit"))
        override = memory["fieldConfig"]["overrides"][0]
        self.assertEqual(override["matcher"], {"id": "byRegexp", "options": "/ limit$/"})
        self.assertRegex(limit["legendFormat"], override["matcher"]["options"][1:-1])
        self.assertEqual(
            override["properties"],
            [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}],
        )


class GpuBoard(unittest.TestCase):
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

    def test_gpu_driver_version_uses_the_exporters_field_label(self) -> None:
        dashboard = json.loads(
            (ROOT / "compose/grafana/dashboards/gpu.json").read_text(encoding="utf-8")
        )
        driver_panel = next(panel for panel in dashboard["panels"] if panel["title"] == "Driver version")
        self.assertIn("DCGM_FI_DRIVER_VERSION", driver_panel["targets"][0]["expr"])
        self.assertEqual(driver_panel["targets"][0]["legendFormat"], "{{DCGM_FI_DRIVER_VERSION}}")
        self.assertEqual(driver_panel["options"]["textMode"], "name")


if __name__ == "__main__":
    unittest.main()
