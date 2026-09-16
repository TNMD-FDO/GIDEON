"""Pure Prometheus configuration rendering (spec §19.5)."""

from typing import Final

from gideon.host.render import Artifact, RenderInputs, substitute_template
from gideon.host.render.engine import ENGINE_JOB_NAME, engine_metrics_target
from gideon.host.render.searxng import (
    SEARXNG_JOB_NAME,
    search_enabled,
    searxng_health_url,
)

PROMETHEUS_TEMPLATE: Final = "prometheus/prometheus.yml.tmpl"
BLACKBOX_TEMPLATE: Final = "blackbox/blackbox.yml.tmpl"
# The template's $gpu_jobs line: the DCGM job and the engine's own /metrics
# (keyless by vLLM's design, on the Compose network alone), both governed by
# the no-GPU marker like the services they scrape; a no-GPU host renders the
# block as one blank line (the rules file's $gpu_rules is the same pattern).
_GPU_JOBS: Final = (
    "  - job_name: dcgm\n"
    "    static_configs:\n"
    "      - targets: [dcgm-exporter:9400]\n"
    f"  - job_name: {ENGINE_JOB_NAME}\n"
    "    static_configs:\n"
    f"      - targets: [{engine_metrics_target()}]"
)
# The template's $search_jobs line: one blackbox probe of SearXNG's health
# endpoint, rendered only while the service is (web.search on) and as one
# blank line otherwise, the $gpu_jobs pattern; its own `probe_success` rule
# in the Grafana rules file pages on it (slice-1 ticket 15).
_SEARCH_JOBS: Final = (
    f"  - job_name: {SEARXNG_JOB_NAME}\n"
    "    metrics_path: /probe\n"
    "    params:\n"
    "      module: [http_2xx]\n"
    "    static_configs:\n"
    f"      - targets: [{searxng_health_url()}]\n"
    "    relabel_configs:\n"
    "      - source_labels: [__address__]\n"
    "        target_label: __param_target\n"
    "      - source_labels: [__param_target]\n"
    "        target_label: instance\n"
    "      - target_label: __address__\n"
    "        replacement: blackbox-exporter:9115"
)


class PrometheusConfigArtifact(Artifact):
    """The scrape configuration: every job is a Compose-network target."""

    name = "prometheus-config"
    relative_path = "prometheus/prometheus.yml"
    owners = ("prometheus",)
    template_paths = (PROMETHEUS_TEMPLATE,)

    def emit(self, inputs: RenderInputs) -> str:
        return substitute_template(
            inputs,
            PROMETHEUS_TEMPLATE,
            {
                "gpu_jobs": "" if inputs.no_gpu else _GPU_JOBS,
                "search_jobs": _SEARCH_JOBS if search_enabled(inputs) else "",
            },
        )


class BlackboxConfigArtifact(Artifact):
    """Render the TLS-verified ingress and plain frontend probe modules."""

    name = "blackbox-config"
    relative_path = "blackbox/blackbox.yml"
    owners = ("blackbox-exporter",)
    template_paths = (BLACKBOX_TEMPLATE,)

    def emit(self, inputs: RenderInputs) -> str:
        return substitute_template(
            inputs, BLACKBOX_TEMPLATE, {"hostname": inputs.site.hostname}
        )
