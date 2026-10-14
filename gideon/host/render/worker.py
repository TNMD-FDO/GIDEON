"""Names shared by the queue worker's block, host command, and scrape job.

The Compose block, `worker verify`, and the Prometheus configuration read them
here; the worker package restates them, since it never imports the host.
"""

from pathlib import Path
from typing import Final

from gideon.host import models
from gideon.host.render.engine import ENGINE_PORT

WORKER_SERVICE_NAME: Final[str] = "gideon-worker"
WORKER_JOB_NAME: Final[str] = "worker"
WORKER_METRICS_PORT: Final[int] = ENGINE_PORT
WORKER_METRICS_PATH: Final[str] = "/metrics"
WORKER_ROLE: Final[str] = "gideon_worker"
WORKER_SECRET_NAME: Final[str] = "postgres_gideon_worker_password"
WORKER_DATABASE_HOST: Final[str] = "postgres"
WORKER_DATABASE_PORT: Final[int] = 5432
WORKER_DATABASE_NAME: Final[str] = "gideon"
# exempt: four job slots are the starting value until a bulk build measures one.
WORKER_CONCURRENCY: Final[int] = 4
WORKER_VERIFY_TASK: Final[str] = "gideon.worker.tasks.verify"
WORKER_VERIFY_QUEUE: Final[str] = "verify"
WORKER_RECOVERY_TASK: Final[str] = "gideon.worker.tasks.retry_stalled_jobs"
WORKER_RECOVERY_QUEUE: Final[str] = "maintenance"
WORKER_RECOVERY_CRON: Final[str] = "* * * * *"
WORKER_MIGRATION_STEM: Final[str] = "0007_procrastinate_queue"

FETCH_TASK: Final[str] = "gideon.worker.tasks.fetch"
FETCH_QUEUE: Final[str] = "fetch"
STAGE_TASK: Final[str] = "gideon.worker.tasks.stage"
STAGE_QUEUE: Final[str] = "stage"
CASELAW_TASK: Final[str] = "gideon.worker.tasks.caselaw"
CASELAW_QUEUE: Final[str] = "caselaw"
CASELAW_FAILURE_NAME: Final[str] = "caselaw-failed.json"
AGREEMENT_TASK: Final[str] = "gideon.worker.tasks.agreement"
AGREEMENT_QUEUE: Final[str] = "caselaw"
AGREEMENT_TABLE: Final[str] = "citation-map"
AGREEMENT_MAP_DIRECTORY: Final[str] = "citation-map"
AGREEMENT_MAP_RECORD_NAME: Final[str] = "map.json"
AGREEMENT_RECORD_NAME: Final[str] = "agreement.json"
AGREEMENT_FAILURE_NAME: Final[str] = "agreement-failed.json"
AGREEMENT_FAILURE_REASONS: Final = frozenset({
    "invalid", "missing-stage", "stage-mismatch", "missing-input",
    "input-mismatch", "malformed", "database", "local", "busy",
})
OVERRULED_TASK: Final[str] = "gideon.worker.tasks.overruled"
OVERRULED_QUEUE: Final[str] = "caselaw"
OVERRULED_SOURCE: Final[str] = "treatment"
OVERRULED_COURT: Final[str] = "scotus"
OVERRULED_FAILURE_NAME: Final[str] = "overruled-failed.json"
OVERRULED_LOCK_NAME: Final[str] = "overruled.lock"
OVERRULED_FAILURE_REASONS: Final = frozenset({
    "invalid", "missing-input", "input-mismatch", "missing-stage",
    "stage-mismatch", "malformed", "database", "local", "busy",
})
OVERRULED_RESOLUTIONS: Final = ("cite", "name", "docket", "ambiguous", "absent")
OVERRULED_WORK_DIRECTORY: Final[str] = "treatment"
LEXICAL_TASK: Final[str] = "gideon.worker.tasks.lexical"
LEXICAL_QUEUE: Final[str] = "index"
LEXICAL_FAILURE_REASONS: Final = frozenset({
    "invalid", "store", "database", "local", "busy", "mapping-mismatch",
    "text-mismatch", "missing-text", "generation-mismatch",
})
DENSE_TASK: Final[str] = "gideon.worker.tasks.dense"
DENSE_QUEUE: Final[str] = "index"
DENSE_LOCK_PREFIX: Final[str] = "dense-"
DENSE_LOCK_SUFFIX: Final[str] = ".dense.lock"
DENSE_FAILURE_REASONS: Final = frozenset({
    "invalid", "store", "embed", "database", "local", "busy",
    "config-mismatch", "chunk-too-long", "text-mismatch", "missing-text",
    "generation-mismatch",
})
DUPES_TASK: Final[str] = "gideon.worker.tasks.dupes"
DUPES_QUEUE: Final[str] = "index"
DUPES_LOCK_PREFIX: Final[str] = "dupes-"
DUPES_LOCK_SUFFIX: Final[str] = ".dupes.lock"
DUPES_FAILURE_REASONS: Final = frozenset({
    "invalid", "database", "local", "busy", "text-mismatch", "missing-text",
    "generation-mismatch", "cluster-budget",
})
GENERATION_FAILURE_REASONS: Final = (
    LEXICAL_FAILURE_REASONS | DENSE_FAILURE_REASONS | DUPES_FAILURE_REASONS
)
DUPES_COUNT_KEYS: Final = (
    "cluster_chunks", "cluster_texts", "cluster_documents", "cluster_clusters",
    "cluster_members", "cluster_grouped", "cluster_boilerplate", "cluster_flagged",
    "cluster_seconds", "cluster_reused",
)
# exempt: one hour is the clustering's starting budget; past it the clustering turns incremental.
CLUSTER_BUDGET_SECONDS: Final[int] = 3_600
DENSE_COUNT_KEYS: Final = (
    "dense_total", "dense_scope", "dense_present", "dense_copied",
    "dense_embedded", "dense_texts", "dense_tokens", "dense_requests",
    "dense_seconds", "dense_points", "dense_indexed", "dense_limit",
    "dense_hour_seconds", "dense_hour_tokens", "dense_hour_written",
    "dense_leveled",
)
SERVED_MODEL_PATTERN: Final[str] = r"[A-Za-z0-9._-]{1,64}"
# exempt: the bound caps a job argument's vector width and its allocation.
DIMENSIONS_MAX: Final[int] = 65_536
GENERATION_STATES: Final = ("building", "built", "live", "retired", "failed")
COLLECTION_NAMES: Final = ("caselaw",)
GENERATION_PATTERN: Final[str] = (
    rf"({'|'.join(COLLECTION_NAMES)})__corpus-[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}__"
    rf"{models.EMBEDDING_SPACE_ID.pattern.removeprefix('^').removesuffix('$')}"
)
LEXICAL_MAPPING_VERSION: Final[int] = 1
CASELAW_FAILURE_REASONS: Final = frozenset({
    "invalid", "missing-stage", "stage-mismatch", "malformed", "store",
    "database", "local", "busy", "segmenter", "anchors", "text-mismatch",
    "citations", "treatment", "tokenizer", "chunker", "pipeline",
})
PIPELINE_VERSION: Final[str] = "1.0.0"
PIPELINE_CHUNK_SIZES: Final = {"1.0.0": (512, 1024, 64)}
DOCUMENT_FAILURE_REASONS: Final = frozenset({
    "no-text", "unparseable", "empty", "interrupted",
})
TEXT_SOURCES: Final = (
    "xml_harvard", "html_columbia", "html_lawbox", "html_anon_2020",
    "html", "plain_text",
)
SECTION_TYPES: Final = (
    "syllabus", "headmatter", "majority", "plurality", "per_curiam",
    "concurrence", "dissent", "concurrence_dissent", "footnote", "appendix",
    "order", "unknown",
)
SECTION_TYPED_BY: Final = ("row", "flag", "element", "line", "markup", "none")
ANCHOR_KINDS: Final = (
    "reporter_page", "pdf_page", "bates", "tr_page", "tr_line",
    "uslm_id", "guideline_id",
)
CITE_TYPES: Final = (
    "statute", "guideline", "court_rule", "bare_section", "bare_rule",
    "regulation", "appendix_statute", "habeas_rule", "scotus_rule",
    "state_code", "case_cite", "law_cite", "journal_cite", "unknown",
)
CITE_FORMS: Final = ("full", "short", "id", "supra", "reference")
PATTERN_SET_ID: Final = "treatment/patterns@2"
# On at 0.997 measured precision; the build's materialization reads
# the switch here, held equal to the worker's, which never imports the host.
SIGNAL_PATTERN: Final[bool] = True
TREATMENT_SIGNALS: Final = (
    "overruled", "abrogated", "superseded", "reversed", "vacated", "disapproved",
)
QUALIFIERS: Final = ("in_part", "on_other_grounds", "none")
SIGNAL_SOURCES: Final = ("pattern", "list", "llm")
TREATMENT_STATES: Final = ("negative", "caution")
NO_STATE_REASONS: Final = ("non_holding", "lineage_unverified", "unresolved")
PRECEDENTIAL_VALUES: Final = ("published", "unpublished", "unknown")
# exempt: the bound limits a queue argument and exceeds any one court's rows.
CASELAW_LIMIT_MAX: Final[int] = 10_000_000
SNAPSHOTS_ROOT: Final = Path("/data/bulk/snapshots")
WORK_ROOT: Final = Path("/data/work")
DIR_MODE: Final[int] = 0o2770
RESOLVE_DIR: Final[str] = "resolve"
KEPT_FORM: Final[str] = "kept"
FRESH_FORM: Final[str] = "fresh"
# A file the host copied from the release checkout, with no job.
SEEDED_FORM: Final[str] = "seeded"
RECORD_SUFFIX: Final[str] = ".fetch.json"
FAILURE_SUFFIX: Final[str] = ".fetch-failed.json"
PARTIAL_SUFFIX: Final[str] = ".partial"
STAGE_RECORD_NAME: Final[str] = "stage.json"
STAGE_FAILURE_NAME: Final[str] = "stage-failed.json"
STAGE_TABLES: Final = (
    "courts", "dockets", "opinion-clusters", "citations", "opinions",
)
COURT_PATTERN: Final[str] = r"[a-z0-9]{1,32}"
STAGE_FAILURE_REASONS: Final = frozenset({
    "invalid", "missing-input", "input-mismatch", "unknown-court",
    "malformed", "local", "busy",
})
RESERVED_SUFFIXES: Final = (PARTIAL_SUFFIX, RECORD_SUFFIX, FAILURE_SUFFIX)
# exempt: two fetch lanes are the starting value until the corpus run measures one.
LANE_COUNT: Final[int] = 2
SEGMENT_PATTERN: Final[str] = r"[A-Za-z0-9][A-Za-z0-9._-]*"
SOURCE_PATTERN: Final[str] = rf"{SEGMENT_PATTERN}-[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}"
DNS_PATTERN: Final[str] = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
# exempt: the address bound limits a queue argument's size.
URL_MAX_LENGTH: Final[int] = 2048
FAILURE_REASONS: Final = frozenset({
    "invalid", "refused-host", "egress-failed", "upstream-status", "changed",
    "redirect", "transport", "local", "busy",
})

DATABASE_HOST_ENV: Final[str] = "GIDEON_WORKER_DB_HOST"
DATABASE_PORT_ENV: Final[str] = "GIDEON_WORKER_DB_PORT"
DATABASE_NAME_ENV: Final[str] = "GIDEON_WORKER_DB_NAME"
DATABASE_ROLE_ENV: Final[str] = "GIDEON_WORKER_DB_ROLE"
PASSWORD_FILE_ENV: Final[str] = "GIDEON_WORKER_PASSWORD_FILE"
CONCURRENCY_ENV: Final[str] = "GIDEON_WORKER_CONCURRENCY"
OPENSEARCH_URL_ENV: Final[str] = "GIDEON_WORKER_OPENSEARCH_URL"
OPENSEARCH_USER_ENV: Final[str] = "GIDEON_WORKER_OPENSEARCH_USER"
OPENSEARCH_PASSWORD_FILE_ENV: Final[str] = "GIDEON_WORKER_OPENSEARCH_PASSWORD_FILE"
QDRANT_URL_ENV: Final[str] = "GIDEON_WORKER_QDRANT_URL"
QDRANT_KEY_FILE_ENV: Final[str] = "GIDEON_WORKER_QDRANT_KEY_FILE"
EMBED_URL_ENV: Final[str] = "GIDEON_WORKER_EMBED_URL"
EMBED_KEY_FILE_ENV: Final[str] = "GIDEON_WORKER_EMBED_KEY_FILE"


def worker_metrics_target() -> str:
    """Return the metrics listener as a Compose host and port."""

    return f"{WORKER_SERVICE_NAME}:{WORKER_METRICS_PORT}"
