"""Pure Prometheus configuration rendering."""

from typing import Final

from gideon.host.render import (
    Artifact,
    RenderInputs,
    VerbatimArtifact,
    substitute_template,
)
from gideon.host.render.api import API_JOB_NAME, api_enabled, api_health_url
from gideon.host.render.engine import MODEL_SERVERS, metrics_target
from gideon.host.render.opensearch import (
    OPENSEARCH_JOB_NAME,
    OPENSEARCH_SERVICE_NAME,
    opensearch_health_url,
)
from gideon.host.render.qdrant import QDRANT_JOB_NAME, qdrant_metrics_target
from gideon.host.render.searxng import (
    SEARXNG_JOB_NAME,
    search_enabled,
    searxng_health_url,
)
from gideon.host.render.worker import WORKER_JOB_NAME, worker_metrics_target

PROMETHEUS_TEMPLATE: Final = "prometheus/prometheus.yml.tmpl"
BLACKBOX_TEMPLATE: Final = "blackbox/blackbox.yml.tmpl"
# The DCGM exporter's counters file: the fields it collects, passed by its -f
# flag and mounted beside the image's own default file, never over it.
DCGM_COUNTERS_TEMPLATE: Final = "dcgm-exporter/counters.csv"
DCGM_COUNTERS_PATH: Final = "dcgm-exporter/counters.csv"
DCGM_COUNTERS_MOUNT: Final = "/etc/dcgm-exporter/gideon-counters.csv"
# The template's $gpu_jobs line: the DCGM job and the model servers' /metrics
# (keyless by vLLM's design, on the Compose network alone), all governed by
# the no-GPU marker like the services they scrape; a no-GPU host renders the
# block as one blank line (the rules file's $gpu_rules is the same pattern).
_GPU_JOBS: Final = (
    "  - job_name: dcgm\n"
    "    static_configs:\n"
    "      - targets: [dcgm-exporter:9400]\n"
    + "\n".join(
        f"  - job_name: {member.job_name}\n"
        "    static_configs:\n"
        f"      - targets: [{metrics_target(member)}]"
        for member in MODEL_SERVERS
    )
)


def _probe_job(name: str, target: str) -> str:
    """One blackbox HTTP probe with the target carried through its relabels."""

    return (
        f"  - job_name: {name}\n"
        "    metrics_path: /probe\n"
        "    params:\n"
        "      module: [http_2xx]\n"
        "    static_configs:\n"
        f"      - targets: [{target}]\n"
        "    relabel_configs:\n"
        "      - source_labels: [__address__]\n"
        "        target_label: __param_target\n"
        "      - source_labels: [__param_target]\n"
        "        target_label: instance\n"
        "      - target_label: __address__\n"
        "        replacement: blackbox-exporter:9115"
    )


# The template's $store_jobs line, on every host: the vector store's metrics
# listener sits outside its API key. OpenSearch's health path answers without
# a credential, so its probe holds none; its own probe_success rule pages on it.
_STORE_JOBS: Final = (
    f"  - job_name: {QDRANT_JOB_NAME}\n"
    "    static_configs:\n"
    f"      - targets: [{qdrant_metrics_target()}]\n"
    + _probe_job(OPENSEARCH_JOB_NAME, opensearch_health_url(OPENSEARCH_SERVICE_NAME))
)
# The template's $search_jobs line: one blackbox probe of SearXNG's health
# endpoint, rendered only while the service is (web.search on) and as one
# blank line otherwise, the $gpu_jobs pattern; its own `probe_success` rule
# in the Grafana rules file pages on it.
_SEARCH_JOBS: Final = _probe_job(SEARXNG_JOB_NAME, searxng_health_url())
# The template's $api_jobs line: one blackbox probe of the API service's
# health endpoint, rendered on every GPU host where the service renders.
_API_JOBS: Final = _probe_job(API_JOB_NAME, api_health_url())
# The worker target is on the internal network, which Prometheus joins to
# scrape its metrics on every host.
_WORKER_JOBS: Final = (
    f"  - job_name: {WORKER_JOB_NAME}\n"
    "    static_configs:\n"
    f"      - targets: [{worker_metrics_target()}]"
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
                "store_jobs": _STORE_JOBS,
                "search_jobs": _SEARCH_JOBS if search_enabled(inputs) else "",
                "api_jobs": _API_JOBS if api_enabled(inputs.no_gpu) else "",
                "worker_jobs": _WORKER_JOBS,
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


DcgmCountersArtifact = VerbatimArtifact(
    name="dcgm-counters",
    relative_path=DCGM_COUNTERS_PATH,
    template_path=DCGM_COUNTERS_TEMPLATE,
    owners=("dcgm-exporter",),
    applies=lambda inputs: not inputs.no_gpu,
)
