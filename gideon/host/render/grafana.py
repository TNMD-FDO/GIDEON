"""Pure Grafana configuration and dashboard rendering."""

import json
from pathlib import PurePosixPath
from typing import Any, Final

from gideon.host.ldap import bind_identity
from gideon.host.render import (
    Artifact,
    RenderInputs,
    VerbatimArtifact,
    substitute_template,
    template_text,
    unfilled_placeholder,
)
from gideon.host.render.api import API_JOB_NAME, api_enabled
from gideon.host.render.engine import ENGINE_JOB_NAME
from gideon.host.render.opensearch import OPENSEARCH_JOB_NAME
from gideon.host.render.searxng import SEARXNG_JOB_NAME, search_enabled
from gideon.host.render.systemd import NIGHTLY_SUITES
from gideon.host.steps.command import INSTALL_HOME

LDAP_TEMPLATE: Final = "grafana/ldap.toml.tmpl"
DATASOURCES_TEMPLATE: Final = "grafana/provisioning/datasources/datasources.yaml.tmpl"
DASHBOARDS_TEMPLATE: Final = "grafana/provisioning/dashboards/provider.yaml.tmpl"
# The Overview's relative path in the rendered tree.
OVERVIEW_PATH: Final = "grafana/dashboards/overview.json"
# The source template for the Overview dashboard.
OVERVIEW_TEMPLATE: Final = OVERVIEW_PATH
BACKUP_TEMPLATE: Final = "grafana/dashboards/backup.json"
GPU_TEMPLATE: Final = "grafana/dashboards/gpu.json"
EVAL_TEMPLATE: Final = "grafana/dashboards/evaluation.json"
CONTACT_POINTS_TEMPLATE: Final = "grafana/provisioning/alerting/contact-points.yaml.tmpl"
POLICIES_TEMPLATE: Final = "grafana/provisioning/alerting/policies.yaml.tmpl"
TIME_INTERVALS_TEMPLATE: Final = "grafana/provisioning/alerting/time-intervals.yaml.tmpl"
RULES_TEMPLATE: Final = "grafana/provisioning/alerting/rules.yaml.tmpl"
PLUGINS_TEMPLATE: Final = "grafana/provisioning/plugins/plugins.yaml"
DRIFT_RULE_TEMPLATE: Final = "grafana/provisioning/alerting/driver-drift-rule.yaml.tmpl"
ENGINE_RULE_TEMPLATE: Final = "grafana/provisioning/alerting/engine-down-rule.yaml.tmpl"
# The host-unit rule pages on the registry and runner units, which only the
# build box runs.
HOST_UNIT_RULE_TEMPLATE: Final = "grafana/provisioning/alerting/host-unit-rule.yaml.tmpl"
# The search probe's rule: its own template like the two
# GPU-host rules, rendered only while SearXNG is; "Target down" reads a scrape's
# `up`, which the blackbox exporter keeps at 1 while the probe itself fails,
# so each probe has a `probe_success` rule of its own.
SEARCH_RULE_TEMPLATE: Final = "grafana/provisioning/alerting/search-probe-rule.yaml.tmpl"
# The API probe's rule, rendered while the GPU-only API service is present.
API_RULE_TEMPLATE: Final = "grafana/provisioning/alerting/api-probe-rule.yaml.tmpl"
NIGHTLY_RULE_TEMPLATE: Final = "grafana/provisioning/alerting/nightly-run-rules.yaml.tmpl"
# The local break-glass administrator; the password is the generated
# print-once secret `grafana_admin_password`.
GRAFANA_ADMIN_USER: Final = "grafana-admin"
# Where the container sees the rendered boards; the provisioning provider
# names the same path.
DASHBOARDS_MOUNT: Final = "/etc/grafana/dashboards"
# The ingress route and Grafana root URL path.
GRAFANA_SUB_PATH: Final = "/grafana/"
# The pinned Grafana frontend's silences route under the sub path, read from
# the image's bundle; re-read at a Grafana pin bump.
GRAFANA_SILENCES_ROUTE: Final = "alerting/silences"
# The Overview panels whose content the render fills, and the sentinel strings
# the template carries in their place, so the file on disk stays valid JSON.
START_HERE_TITLE: Final = "Start here"
BACKUP_SET_AGE_TITLE: Final = "Backup set age"
BACKUP_PUSH_AGE_TITLE: Final = "Backup push age"
FILESYSTEMS_FREE_TITLE: Final = "Filesystems free"
CERTIFICATE_DAYS_LEFT_TITLE: Final = "Certificate days left"
PASSING_DRILL_AGE_TITLE: Final = "Passing drill age"
OVERVIEW_SENTINELS: Final = (
    "start_here",
    "drill_threshold",
    "drill_days",
    "backup_threshold",
    "backup_hours",
    "filesystem_percent",
    "certificate_days",
)
# The Overview's home dashboard path inside the container.
HOME_DASHBOARD_PATH: Final = DASHBOARDS_MOUNT + "/" + PurePosixPath(OVERVIEW_PATH).name
# The start-here card's relative path under the install home.
START_HERE_CARD: Final = "docs/runbooks/start-here.md"
# The public export's repository URL.
PUBLIC_REPOSITORY_URL: Final = "https://github.com/TNMD-FDO/GIDEON"
# These are the maximum gaps permitted by the actual Saturday calendars, not
# the nominal cadence: 1w is seven days; a 2w run on days 15–21 can miss the
# next month's first-week Saturday by 21 days; first-week 1m Saturdays can be
# 35 days apart; 3m can span Jul 1 → Oct 7 or Oct 1 → Jan 7 (98 days); 6m can
# span Jul 1 → Jan 7 (189 days: 184 + 6, rounded down to the Saturday multiple
# of seven); and 1y can span Jan 1 → Jan 7 across a leap year (371 days). Two
# Saturdays are always a multiple of seven days apart, so each gap is one.
DRILL_MAX_GAP_DAYS: Final[dict[str, int]] = {
    "1w": 7,
    "2w": 21,
    "1m": 35,
    "3m": 98,
    "6m": 189,
    "1y": 371,
}
# The "Engine down" rule's pending period. A measurement on the box found the
# engine healthy 57 s after a plain `compose start` and 185 s after a cold
# recreate (the bound is ENGINE_READY_SECONDS): the period must clear both, so
# a deliberate stop/start or an apply's recreate never pages, and it stays
# below the core-service rules' ten minutes because the frontend shows nothing
# while the engine is away (its model list keeps the last fetch). A first
# start that outruns it pages once and resolves by itself.
ENGINE_PENDING_PERIOD: Final = "5m"
# The timer starts at 21:00, but an engine wait may defer a run until 06:00.
# Consecutive starts can be 33 hours apart; 36 hours leaves room for that
# wait and pages a missed night at about 09:00. This is a starting value.
NIGHTLY_OVERDUE_SECONDS: Final = 36 * 3600
# exempt: schedule — a new upstream notice pages for one full weekly interval.
UPSTREAM_NOTICE_DAYS: Final = 7
# exempt: schedule — longer than the notice's window, so its email is sent once.
UPSTREAM_REPEAT: Final = "8d"
# exempt: schedule — Grafana's five-day default would forget a sent notice before
# the repeat ends and send it again.
NOTIFICATION_LOG_RETENTION: Final = "8d"
# The page lines below are each one figure, read by its rule and by the
# Overview card that mirrors it, so a card turns red where its page fires.
# "Backup set overdue" and "Push overdue": a nightly set or push with two
# hours of grace past the day.
BACKUP_OVERDUE_SECONDS: Final = 26 * 3600
# exempt: fixed by rule. "Data volume low" and "Host filesystem low": the data
# volume's interim free-space line, applied to the host filesystems too.
FILESYSTEM_LOW_PERCENT: Final = 15
# "TLS certificate expiring": two weeks' lead time, since a renewal comes from
# the office CA.
CERTIFICATE_EXPIRING_SECONDS: Final = 14 * 86400


def drill_overdue_seconds(interval: str) -> int:
    """Return the maximum calendar gap plus the three-day overdue grace."""

    try:
        return DRILL_MAX_GAP_DAYS[interval] * 86400 + 259200
    except KeyError as exc:
        raise ValueError(
            "Render input backup.drill_interval is unsupported. "
            "Correct backup.drill_interval, then re-run render."
        ) from exc


class GrafanaLdapArtifact(Artifact):
    """Grafana's LDAP server and its one group mapping (the admins group)."""

    name = "grafana-ldap"
    relative_path = "grafana/ldap.toml"
    owners = ("grafana",)
    template_paths = (LDAP_TEMPLATE,)

    def emit(self, inputs: RenderInputs) -> str:
        ldap = inputs.site.auth.ldap
        return substitute_template(
            inputs,
            LDAP_TEMPLATE,
            {
                "ldap_host": json.dumps(ldap.host),
                "ldap_port": ldap.port,
                "bind_identity": json.dumps(bind_identity(ldap.bind_user, ldap.host)),
                "search_base": json.dumps(ldap.search_base),
                "admins_group_dn": json.dumps(ldap.admins_group_dn),
            },
        )


class GrafanaDatasourcesArtifact(Artifact):
    """The fixed Prometheus and read-only PostgreSQL datasources."""

    name = "grafana-datasources"
    relative_path = "grafana/provisioning/datasources/datasources.yaml"
    owners = ("grafana",)
    template_paths = (DATASOURCES_TEMPLATE,)

    def emit(self, inputs: RenderInputs) -> str:
        return substitute_template(inputs, DATASOURCES_TEMPLATE, {})


class GrafanaDashboardsProviderArtifact(Artifact):
    """The file-backed GIDEON dashboard provider."""

    name = "grafana-dashboards-provider"
    relative_path = "grafana/provisioning/dashboards/provider.yaml"
    owners = ("grafana",)
    template_paths = (DASHBOARDS_TEMPLATE,)

    def emit(self, inputs: RenderInputs) -> str:
        return substitute_template(inputs, DASHBOARDS_TEMPLATE, {"dashboards_mount": DASHBOARDS_MOUNT})


class GrafanaContactPointsArtifact(Artifact):
    """Provision the page, nudge, and upstream email contact points."""

    name = "grafana-contact-points"
    relative_path = "grafana/provisioning/alerting/contact-points.yaml"
    owners = ("grafana",)
    template_paths = (CONTACT_POINTS_TEMPLATE,)

    def emit(self, inputs: RenderInputs) -> str:
        return substitute_template(
            inputs,
            CONTACT_POINTS_TEMPLATE,
            {
                "recipients": ";".join(inputs.site.alerts.recipients),
                "short_name": inputs.site.office.short_name,
            },
        )


class GrafanaPoliciesArtifact(Artifact):
    """Provision the page policy and its heartbeat, nudge, and upstream routes."""

    name = "grafana-policies"
    relative_path = "grafana/provisioning/alerting/policies.yaml"
    owners = ("grafana",)
    template_paths = (POLICIES_TEMPLATE,)

    def emit(self, inputs: RenderInputs) -> str:
        return substitute_template(
            inputs, POLICIES_TEMPLATE, {"upstream_repeat": UPSTREAM_REPEAT}
        )


class GrafanaTimeIntervalsArtifact(Artifact):
    """Provision the office-time Saturday heartbeat and Monday nudge windows."""

    name = "grafana-time-intervals"
    relative_path = "grafana/provisioning/alerting/time-intervals.yaml"
    owners = ("grafana",)
    template_paths = (TIME_INTERVALS_TEMPLATE,)

    def emit(self, inputs: RenderInputs) -> str:
        return substitute_template(
            inputs,
            TIME_INTERVALS_TEMPLATE,
            {"timezone": inputs.site.office.timezone},
        )


class GrafanaRulesArtifact(Artifact):
    """Provision the SQL and Prometheus page-class rules."""

    name = "grafana-rules"
    relative_path = "grafana/provisioning/alerting/rules.yaml"
    owners = ("grafana",)
    template_paths = (
        RULES_TEMPLATE,
        DRIFT_RULE_TEMPLATE,
        ENGINE_RULE_TEMPLATE,
        HOST_UNIT_RULE_TEMPLATE,
        SEARCH_RULE_TEMPLATE,
        API_RULE_TEMPLATE,
        NIGHTLY_RULE_TEMPLATE,
    )

    def emit(self, inputs: RenderInputs) -> str:
        # The engine and drift rules are their own templates so the rules file
        # carries no code: the engine rule on every GPU host, the drift rule
        # only when host.lock records a tested driver version. A no-GPU host
        # renders neither; the host-unit rule belongs only to the build box.
        # The nightly run's rules render where its unit does, on a GPU host,
        # each query enumerating the unit's suites so one that never ran
        # still has a series.
        # Target down's exclusion of the engine job is one text on every host,
        # inert where no engine job exists.
        driver = inputs.lock.driver.tested
        engine_rule = (
            ""
            if inputs.no_gpu
            else substitute_template(
                inputs,
                ENGINE_RULE_TEMPLATE,
                {"engine_job": ENGINE_JOB_NAME, "pending": ENGINE_PENDING_PERIOD},
            )
        )
        driver_rule = (
            ""
            if inputs.no_gpu or driver is None
            else substitute_template(inputs, DRIFT_RULE_TEMPLATE, {"driver": driver})
        )
        search_rule = (
            substitute_template(inputs, SEARCH_RULE_TEMPLATE, {"search_job": SEARXNG_JOB_NAME})
            if search_enabled(inputs)
            else ""
        )
        api_rule = (
            substitute_template(inputs, API_RULE_TEMPLATE, {"api_job": API_JOB_NAME})
            if api_enabled(inputs.no_gpu)
            else ""
        )
        # The placeholder's own line ends the block, so the build box's file
        # keeps today's bytes; every other host carries an empty line there.
        host_unit_rule = (
            substitute_template(inputs, HOST_UNIT_RULE_TEMPLATE, {}).removesuffix("\n")
            if inputs.build_box
            else ""
        )
        nightly_rules = (
            substitute_template(
                inputs,
                NIGHTLY_RULE_TEMPLATE,
                {
                    "nightly_suites": ", ".join(f"('{suite}')" for suite in NIGHTLY_SUITES),
                    "overdue_seconds": NIGHTLY_OVERDUE_SECONDS,
                    "missing_age_seconds": NIGHTLY_OVERDUE_SECONDS + 1,
                },
            ).removesuffix("\n")
            if not inputs.no_gpu
            else ""
        )
        return substitute_template(
            inputs,
            RULES_TEMPLATE,
            {
                "drill_threshold": drill_overdue_seconds(inputs.site.backup.drill_interval),
                "backup_threshold": BACKUP_OVERDUE_SECONDS,
                "filesystem_percent": FILESYSTEM_LOW_PERCENT,
                "certificate_threshold": CERTIFICATE_EXPIRING_SECONDS,
                "certificate_days": CERTIFICATE_EXPIRING_SECONDS // 86400,
                "search_rules": search_rule,
                "api_rules": api_rule,
                "gpu_rules": engine_rule + driver_rule,
                "host_unit_rules": host_unit_rule,
                "nightly_rules": nightly_rules,
                "engine_job": ENGINE_JOB_NAME,
                "opensearch_job": OPENSEARCH_JOB_NAME,
                "upstream_notice_days": UPSTREAM_NOTICE_DAYS,
            },
        )


class GrafanaOverviewArtifact(Artifact):
    """Render the Overview's board links and page-rule threshold cards."""

    name = "grafana-overview"
    relative_path = OVERVIEW_PATH
    owners = ("grafana",)
    template_paths = (OVERVIEW_TEMPLATE, BACKUP_TEMPLATE, GPU_TEMPLATE, EVAL_TEMPLATE)

    def emit(self, inputs: RenderInputs) -> str:
        document = json.loads(template_text(inputs, OVERVIEW_TEMPLATE))
        panels = {panel.get("title"): panel for panel in document["panels"]}
        start = panels.get(START_HERE_TITLE, {}).get("options", {})
        drill = panels.get(PASSING_DRILL_AGE_TITLE, {})
        threshold = drill_overdue_seconds(inputs.site.backup.drill_interval)
        filesystems = panels.get(FILESYSTEMS_FREE_TITLE, {})
        certificate = panels.get(CERTIFICATE_DAYS_LEFT_TITLE, {})
        _fill(start, "content", "start_here", start_here_markdown(inputs))
        _fill(_last_step(drill), "value", "drill_threshold", threshold)
        _fill(drill, "description", "drill_days", threshold // 86400)
        for title in (BACKUP_SET_AGE_TITLE, BACKUP_PUSH_AGE_TITLE):
            card = panels.get(title, {})
            _fill(_last_step(card), "value", "backup_threshold", BACKUP_OVERDUE_SECONDS)
            _fill(card, "description", "backup_hours", BACKUP_OVERDUE_SECONDS // 3600)
        _fill(_last_step(filesystems), "value", "filesystem_percent", FILESYSTEM_LOW_PERCENT)
        _fill(filesystems, "description", "filesystem_percent", FILESYSTEM_LOW_PERCENT)
        certificate_days = CERTIFICATE_EXPIRING_SECONDS // 86400
        _fill(_last_step(certificate), "value", "certificate_days", certificate_days)
        _fill(certificate, "description", "certificate_days", certificate_days)
        rendered = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        for name in OVERVIEW_SENTINELS:
            if f"${name}" in rendered:
                raise unfilled_placeholder(OVERVIEW_TEMPLATE, name)
        return rendered


def _last_step(panel: dict[str, Any]) -> dict[str, object]:
    """Return a card's last threshold step, the one its line sits in."""

    steps = panel.get("fieldConfig", {}).get("defaults", {}).get("thresholds", {}).get("steps")
    return (steps or [{}])[-1]


def _fill(holder: dict[str, object], key: str, name: str, value: object) -> None:
    """Fill one Overview sentinel in place: a whole string takes the typed value, a phrase its text."""

    text = holder.get(key)
    sentinel = f"${name}"
    if not isinstance(text, str) or sentinel not in text:
        raise unfilled_placeholder(OVERVIEW_TEMPLATE, name)
    holder[key] = value if text == sentinel else text.replace(sentinel, str(value))


GrafanaPluginsArtifact = VerbatimArtifact(
    name="grafana-plugins",
    relative_path=PLUGINS_TEMPLATE,
    template_path=PLUGINS_TEMPLATE,
    owners=("grafana",),
)


GrafanaBackupArtifact = VerbatimArtifact(
    name="grafana-backup",
    relative_path="grafana/dashboards/backup.json",
    template_path=BACKUP_TEMPLATE,
    owners=("grafana",),
)

GrafanaGpuArtifact = VerbatimArtifact(
    name="grafana-gpu",
    relative_path="grafana/dashboards/gpu.json",
    template_path=GPU_TEMPLATE,
    owners=("grafana",),
    applies=lambda inputs: not inputs.no_gpu,
)

GrafanaEvalArtifact = VerbatimArtifact(
    name="grafana-eval",
    relative_path="grafana/dashboards/evaluation.json",
    template_path=EVAL_TEMPLATE,
    owners=("grafana",),
    applies=lambda inputs: not inputs.no_gpu,
)

# The dashboard artifacts linked from the Overview, in display order.
FRONT_DOOR_BOARDS: Final = (GrafanaBackupArtifact, GrafanaGpuArtifact, GrafanaEvalArtifact)


def start_here_markdown(inputs: RenderInputs) -> str:
    """Build the Overview's links to available boards and the start-here card."""

    links = []
    for board in FRONT_DOOR_BOARDS:
        if board.applies(inputs):
            dashboard = json.loads(template_text(inputs, board.template_paths[0]))
            links.append(f"- [{dashboard['title']}]({GRAFANA_SUB_PATH}d/{dashboard['uid']})")
    card_url = f"{PUBLIC_REPOSITORY_URL}/blob/v{inputs.release}/{START_HERE_CARD}"
    paragraphs = (
        # One paragraph: the panel's fixed height holds every line only so.
        "On the box, run `gideon status` for what needs attention now. "
        "Before planned work, [silence the rule that would page]"
        f"({GRAFANA_SUB_PATH}{GRAFANA_SILENCES_ROUTE}).",
        "The other boards on this host:",
        "\n".join(links),
        f"The start-here card: `{INSTALL_HOME / START_HERE_CARD}` on the box, "
        f'or <a href="{card_url}" target="_blank">in the public export at this release</a>.',
    )
    return "\n\n".join(paragraphs)
