"""Pure Grafana configuration and dashboard rendering (spec §19.5, ADR-0034)."""

import json
from typing import Final

from gideon.host.ldap import bind_identity
from gideon.host.render import (
    Artifact,
    RenderInputs,
    VerbatimArtifact,
    substitute_template,
)
from gideon.host.render.api import API_JOB_NAME, api_enabled
from gideon.host.render.engine import ENGINE_JOB_NAME
from gideon.host.render.searxng import SEARXNG_JOB_NAME, search_enabled

LDAP_TEMPLATE: Final = "grafana/ldap.toml.tmpl"
DATASOURCES_TEMPLATE: Final = "grafana/provisioning/datasources/datasources.yaml.tmpl"
DASHBOARDS_TEMPLATE: Final = "grafana/provisioning/dashboards/provider.yaml.tmpl"
OVERVIEW_TEMPLATE: Final = "grafana/dashboards/overview.json"
BACKUP_TEMPLATE: Final = "grafana/dashboards/backup.json"
GPU_TEMPLATE: Final = "grafana/dashboards/gpu.json"
CONTACT_POINTS_TEMPLATE: Final = "grafana/provisioning/alerting/contact-points.yaml.tmpl"
POLICIES_TEMPLATE: Final = "grafana/provisioning/alerting/policies.yaml.tmpl"
TIME_INTERVALS_TEMPLATE: Final = "grafana/provisioning/alerting/time-intervals.yaml.tmpl"
RULES_TEMPLATE: Final = "grafana/provisioning/alerting/rules.yaml.tmpl"
DRIFT_RULE_TEMPLATE: Final = "grafana/provisioning/alerting/driver-drift-rule.yaml.tmpl"
ENGINE_RULE_TEMPLATE: Final = "grafana/provisioning/alerting/engine-down-rule.yaml.tmpl"
# The host-unit rule pages on the registry and runner units, which only the
# build box runs (slice-0 ticket 21).
HOST_UNIT_RULE_TEMPLATE: Final = "grafana/provisioning/alerting/host-unit-rule.yaml.tmpl"
# The search probe's rule (slice-1 ticket 15): its own template like the two
# GPU-host rules, rendered only while SearXNG is; "Target down" reads a scrape's
# `up`, which the blackbox exporter keeps at 1 while the probe itself fails,
# so each probe has a `probe_success` rule of its own.
SEARCH_RULE_TEMPLATE: Final = "grafana/provisioning/alerting/search-probe-rule.yaml.tmpl"
# The API probe's rule, rendered while the GPU-only API service is present.
API_RULE_TEMPLATE: Final = "grafana/provisioning/alerting/api-probe-rule.yaml.tmpl"
# The local break-glass administrator (§19.5); the password is the generated
# print-once secret `grafana_admin_password`.
GRAFANA_ADMIN_USER: Final = "grafana-admin"
# Where the container sees the rendered boards; the provisioning provider
# names the same path.
DASHBOARDS_MOUNT: Final = "/etc/grafana/dashboards"
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
# The "Engine down" rule's pending period. Slice-1 ticket 06 measured the
# engine healthy 57 s after a plain `compose start` and 185 s after a cold
# recreate (the bound is ENGINE_READY_SECONDS): the period must clear both, so
# a deliberate stop/start or an apply's recreate never pages, and it stays
# below the core-service rules' ten minutes because the frontend shows nothing
# while the engine is away (its model list keeps the last fetch). A first
# start that outruns it pages once and resolves by itself.
ENGINE_PENDING_PERIOD: Final = "5m"


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
    """Provision the single page email contact point."""

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
    """Provision the page policy and the weekly heartbeat route."""

    name = "grafana-policies"
    relative_path = "grafana/provisioning/alerting/policies.yaml"
    owners = ("grafana",)
    template_paths = (POLICIES_TEMPLATE,)

    def emit(self, inputs: RenderInputs) -> str:
        return substitute_template(inputs, POLICIES_TEMPLATE, {})


class GrafanaTimeIntervalsArtifact(Artifact):
    """Provision the office-time Saturday heartbeat window."""

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
    )

    def emit(self, inputs: RenderInputs) -> str:
        # The two GPU-host rules are their own templates so the rules file
        # carries no code: the engine rule on every GPU host, the drift rule
        # only when host.lock records a tested driver version. A no-GPU host
        # renders neither; the host-unit rule belongs only to the build box.
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
        return substitute_template(
            inputs,
            RULES_TEMPLATE,
            {
                "drill_threshold": drill_overdue_seconds(inputs.site.backup.drill_interval),
                "search_rules": search_rule,
                "api_rules": api_rule,
                "gpu_rules": engine_rule + driver_rule,
                "host_unit_rules": host_unit_rule,
                "engine_job": ENGINE_JOB_NAME,
            },
        )


GrafanaOverviewArtifact = VerbatimArtifact(
    name="grafana-overview",
    relative_path="grafana/dashboards/overview.json",
    template_path=OVERVIEW_TEMPLATE,
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
