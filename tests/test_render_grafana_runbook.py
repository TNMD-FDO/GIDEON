"""The observability runbook tables held to the render."""

import json
import re
import unittest
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any

import yaml  # type: ignore[import-untyped]
from test_render_grafana import EXAMPLE, ROOT, SECOND, inputs

from gideon.host.render import RenderInputs
from gideon.host.render.compose import service_blocks
from gideon.host.render.grafana import (
    GrafanaOverviewArtifact,
    GrafanaRulesArtifact,
)
from gideon.host.render.prometheus import PrometheusConfigArtifact
from gideon.host.render.searxng import search_enabled

OBSERVABILITY_RUNBOOK = ROOT / "docs/runbooks/observability.md"
TILE_TABLE_HEADING = "### The tiles on *Services and probes*"
SECTION_FOUR_HEADING = "## 4. What pages, and what to do"
GPU_ONLY = "A GPU host only."
SEARCH_ONLY = "Present only while `web.search` is on"
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
class RuleRow:
    titles: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class RenderedRule:
    title: str
    rule_class: str


@dataclass(frozen=True)
class RuleRender:
    label: str
    rules: tuple[RenderedRule, ...]


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


def code_span_names(cell: str, line: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """A cell's names, written as code spans separated by commas and nothing else.

    One spelling per name lets a table cell be compared exactly with the
    render's tile names and rule titles.
    """

    if re.fullmatch(r"`[^`]+`(?:, `[^`]+`)*", cell) is None:
        return (), (
            f"runbook line {line}: write names in code spans, separated by commas and nothing else",
        )
    return tuple(re.findall(r"`([^`]+)`", cell)), ()


def tile_table(text: str) -> tuple[tuple[TileRow, ...], tuple[str, ...]]:
    """The tile table's rows: tile names, what each checks, and its page."""

    rows, issues = markdown_table(text, TILE_TABLE_HEADING)
    findings = list(issues)
    parsed: list[TileRow] = []
    for row in rows:
        if len(row.cells) != 3:
            findings.append(f"runbook line {row.line}: give the tile table three columns")
            continue
        names, name_findings = code_span_names(row.cells[0], row.line)
        pages, page_findings = code_span_names(row.cells[2], row.line)
        findings.extend(name_findings)
        findings.extend(page_findings)
        if len(pages) != 1 and not page_findings:
            findings.append(f"runbook line {row.line}: give the page cell exactly one code-span title")
        if names and len(pages) == 1:
            parsed.append(TileRow(names, row.cells[1], pages[0], row.line))
    return tuple(parsed), tuple(findings)


def section_four_rows(text: str) -> tuple[tuple[RuleRow, ...], tuple[str, ...]]:
    """Read section 4's rule titles and their lines for render comparisons."""

    rows, issues = markdown_table(text, SECTION_FOUR_HEADING)
    findings = list(issues)
    parsed: list[RuleRow] = []
    for row in rows:
        if len(row.cells) != 3:
            findings.append(f"runbook line {row.line}: give the rule table three columns")
            continue
        titles, title_findings = code_span_names(row.cells[0], row.line)
        findings.extend(title_findings)
        if titles:
            parsed.append(RuleRow(titles, row.line))
    return tuple(parsed), tuple(findings)


def render_rule_facts(site_inputs: RenderInputs, label: str) -> RuleRender:
    """Read each rule's rendered title and class for one labelled host kind."""

    document = yaml.safe_load(GrafanaRulesArtifact().emit(site_inputs))
    return RuleRender(
        label,
        tuple(
            RenderedRule(rule["title"], rule["labels"]["class"])
            for group in document["groups"]
            for rule in group["rules"]
        ),
    )


def rule_table_findings(
    rows: tuple[RuleRow, ...], renders: tuple[RuleRender, ...]
) -> tuple[str, ...]:
    """Every way section 4's Rule cells disagree with the rendered rules.

    A page-class title any host kind renders needs exactly one row; a
    dashboard-class title may have one and never needs it; a listed title
    must be one some host kind renders, spelled exactly.
    """

    listed = Counter(title for row in rows for title in row.titles)
    rendered = {rule.title for render in renders for rule in render.rules}
    page_kinds: dict[str, list[str]] = {}
    for render in renders:
        for rule in render.rules:
            if rule.rule_class == "page":
                page_kinds.setdefault(rule.title, []).append(render.label)
    findings = [
        f"{title}: list this title in one section 4 row"
        for title, count in sorted(listed.items())
        if count > 1
    ]
    findings.extend(
        f"{title}: give this page-class rule a section 4 row, or list it in the row whose steps"
        f" cover it (rendered on {', '.join(labels)})"
        for title, labels in sorted(page_kinds.items())
        if title not in listed
    )
    findings.extend(
        f"{title}: spell this as a rendered rule title or remove it from section 4"
        for title in sorted(listed.keys() - rendered)
    )
    return tuple(findings)


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
    rows: tuple[TileRow, ...], rule_rows: tuple[RuleRow, ...], renders: tuple[RenderFacts, ...]
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
    titles = {title for row in rule_rows for title in row.titles}

    findings: list[str] = []
    counts = Counter(name for row in rows for name in row.tiles)
    findings += [
        f"{name}: name this tile in one row of the table" for name, count in sorted(counts.items()) if count > 1
    ]
    for row in rows:
        first = row.tiles[0]
        if row.page not in titles:
            findings.append(f"{first}: list its page-cell title `{row.page}` in section 4")
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
        if watched.rule not in titles:
            findings.append(f"{watched.name}: list its watching rule `{watched.rule}` in section 4")
        if row.page != watched.rule:
            findings.append(
                f"{watched.name}: set its page cell to `{watched.rule}`, its watching rule's title"
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
    rule_rows: tuple[RuleRow, ...]
    renders: tuple[RenderFacts, ...]

    @classmethod
    def setUpClass(cls) -> None:
        text = OBSERVABILITY_RUNBOOK.read_text(encoding="utf-8")
        cls.rows, row_findings = tile_table(text)
        cls.rule_rows, rule_findings = section_four_rows(text)
        cls.renders = tuple(
            render_tile_facts(inputs(path, no_gpu=no_gpu), label)
            for path, no_gpu, label in (
                (EXAMPLE, False, "example"),
                (SECOND, False, "second office"),
                (EXAMPLE, True, "no-GPU"),
            )
        )
        if row_findings or rule_findings:
            raise AssertionError(row_findings + rule_findings)

    def test_the_runbook_table_matches_every_render(self) -> None:
        full, search_off, no_gpu = self.renders
        self.assertEqual((full.gpu, full.search), (True, True))
        self.assertEqual((search_off.gpu, search_off.search), (True, False))
        self.assertEqual((no_gpu.gpu, no_gpu.search), (False, True))
        self.assertEqual(len({frozenset(tile.job for tile in render.tiles) for render in self.renders}), 3)
        for render in self.renders:
            self.assertEqual(render.findings, (), render.label)
        self.assertEqual(service_tile_findings(self.rows, self.rule_rows, self.renders), ())

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
        rows, rule_rows, renders = self.rows, self.rule_rows, self.renders

        def row_of(tile: str) -> TileRow:
            return next(row for row in rows if tile in row.tiles)

        def with_row(tile: str, **changes: Any) -> tuple[TileRow, ...]:
            return tuple(replace(row, **changes) if tile in row.tiles else row for row in rows)

        def without_row(tile: str) -> tuple[TileRow, ...]:
            return tuple(row for row in rows if tile not in row.tiles)

        def each_render(change: Any) -> tuple[RenderFacts, ...]:
            return tuple(replace(render, tiles=change(render.tiles)) for render in renders)

        added = ServiceTile("fictitious", "fictitious", "plain", "Target down")
        cases: list[tuple[str, tuple[str, ...], tuple[TileRow, ...], tuple[RuleRow, ...], tuple[RenderFacts, ...]]] = [
            ("job added", ("fictitious",), rows, rule_rows, each_render(lambda tiles: (*tiles, added))),
            (
                "job renamed",
                ("renamed", "cadvisor"),
                rows,
                rule_rows,
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
                rule_rows,
                each_render(lambda tiles: tuple(tile for tile in tiles if tile.job != "cadvisor")),
            ),
            ("row removed", ("cadvisor",), without_row("cadvisor"), rule_rows, renders),
            ("probe span dropped", ("ingress probe",), with_row("ingress", tiles=("ingress",)), rule_rows, renders),
            ("database row dropped", ("postgres database",), without_row("postgres database"), rule_rows, renders),
            (
                "GPU sentence on an unconditional row",
                ("node",),
                with_row("node", description=f"{row_of('node').description} {GPU_ONLY}"),
                rule_rows,
                renders,
            ),
            (
                "GPU sentence off a GPU row",
                ("dcgm",),
                with_row("dcgm", description=row_of("dcgm").description.replace(GPU_ONLY, "")),
                rule_rows,
                renders,
            ),
            (
                "search sentence on an unconditional row",
                ("node",),
                with_row("node", description=f"{row_of('node').description} {SEARCH_ONLY}."),
                rule_rows,
                renders,
            ),
            (
                "search sentence off the search row",
                ("search",),
                with_row("search", description=row_of("search").description.replace(SEARCH_ONLY, "")),
                rule_rows,
                renders,
            ),
            ("page cell no section 4 row", ("search",), with_row("search", page="Search down"), rule_rows, renders),
            ("search page cell set to Target down", ("search probe",), with_row("search", page="Target down"), rule_rows, renders),
            (
                "Target down absent from section 4",
                ("caddy",),
                rows,
                tuple(
                    replace(row, titles=tuple(title for title in row.titles if title != "Target down"))
                    for row in rule_rows
                ),
                renders,
            ),
        ]
        for label, names, variant_rows, variant_rule_rows, variant_renders in cases:
            with self.subTest(variant=label):
                findings = service_tile_findings(variant_rows, variant_rule_rows, variant_renders)
                for name in names:
                    self.assertTrue(
                        any(finding.startswith(f"{name}:") for finding in findings), (name, findings)
                    )

        text = OBSERVABILITY_RUNBOOK.read_text(encoding="utf-8")
        search_line = next(line for line in text.splitlines() if line.startswith("| `search`, `search probe` |"))
        malformed = text.replace(
            search_line,
            search_line.replace("`Search probe failing`", "`Search probe failing`, `Target down`"),
        )
        _, findings = tile_table(malformed)
        self.assertTrue(
            any(
                finding.startswith(f"runbook line {row_of('search').line}:")
                and "exactly one code-span title" in finding
                for finding in findings
            ),
            findings,
        )


class RuleTable(unittest.TestCase):
    rows: tuple[RuleRow, ...]
    renders: tuple[RuleRender, ...]

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows, findings = section_four_rows(OBSERVABILITY_RUNBOOK.read_text(encoding="utf-8"))
        cls.renders = (
            *(
                render_rule_facts(inputs(path, build_box=build_box), label)
                for path, build_box, label in (
                    (EXAMPLE, False, "example"),
                    (EXAMPLE, True, "example build box"),
                    (SECOND, False, "second office"),
                    (SECOND, True, "second office build box"),
                )
            ),
            render_rule_facts(inputs(EXAMPLE, no_gpu=True), "no-GPU"),
        )
        if findings:
            raise AssertionError(findings)

    def test_every_host_kind_has_a_row_for_each_page_title(self) -> None:
        title_sets = {frozenset(rule.title for rule in render.rules) for render in self.renders}
        self.assertEqual(len(title_sets), 5)
        self.assertEqual(rule_table_findings(self.rows, self.renders), ())

    def test_each_seeded_variant_names_its_title(self) -> None:
        rows, renders = self.rows, self.renders
        missing = "Backup set overdue"
        duplicate = "Push overdue"
        added_page = "Fictitious page"
        listed_only = "Fictitious title"
        dashboard_only = "Fictitious dashboard"

        def add_rule(title: str, rule_class: str) -> tuple[RuleRender, ...]:
            first, *others = renders
            return (
                replace(first, rules=(*first.rules, RenderedRule(title, rule_class))),
                *others,
            )

        cases = (
            ("page added to one render", added_page, rows, add_rule(added_page, "page")),
            (
                "rendered title dropped from its row",
                missing,
                tuple(
                    replace(row, titles=tuple(title for title in row.titles if title != missing))
                    for row in rows
                ),
                renders,
            ),
            (
                "title listed in two rows",
                duplicate,
                tuple(
                    replace(row, titles=(*row.titles, duplicate)) if missing in row.titles else row
                    for row in rows
                ),
                renders,
            ),
            ("unrendered title listed", listed_only, (*rows, RuleRow((listed_only,), 0)), renders),
        )
        for label, title, variant_rows, variant_renders in cases:
            with self.subTest(variant=label):
                findings = rule_table_findings(variant_rows, variant_renders)
                self.assertTrue(
                    any(finding.startswith(f"{title}:") for finding in findings), findings
                )

        text = OBSERVABILITY_RUNBOOK.read_text(encoding="utf-8")
        rule_line = next(line for line in text.splitlines() if line.startswith("| `Backup set overdue` |"))
        malformed = text.replace(rule_line, rule_line.replace("`Backup set overdue`", "Backup set overdue"))
        _, findings = section_four_rows(malformed)
        line = next(row.line for row in rows if missing in row.titles)
        self.assertTrue(any(finding.startswith(f"runbook line {line}:") for finding in findings), findings)

        with self.subTest(variant="dashboard title added to one render"):
            self.assertEqual(rule_table_findings(rows, add_rule(dashboard_only, "dashboard")), ())

    def test_silenced_page_remains_listed_until_its_condition_clears(self) -> None:
        dashboard = json.loads(GrafanaOverviewArtifact().emit(inputs()))
        panel = next(item for item in dashboard["panels"] if item["title"] == "Needs attention")
        description = panel["description"]
        self.assertIn("A silenced rule stays listed here until its condition clears", description)
        self.assertIn("the box status marks it silenced and does not count it", description)
        runbook_path = OBSERVABILITY_RUNBOOK.relative_to(ROOT).as_posix()
        self.assertTrue(description.endswith(f"Follow {runbook_path} §4."))

        text = OBSERVABILITY_RUNBOOK.read_text(encoding="utf-8")
        section_five = re.sub(r"\s+", " ", text.split("## 5.", 1)[1].split("## 6.", 1)[0])
        self.assertIn("A silence stops the email alone", section_five)
        self.assertIn(
            "the rule stays listed on the Overview's *Needs attention* panel until its condition clears",
            section_five,
        )
        self.assertIn("`gideon status` marks it `silenced` and does not count it", section_five)
        self.assertIn(
            "Grafana keeps sending a cleared rule's resolved alert for 15 minutes", section_five
        )


if __name__ == "__main__":
    unittest.main()
