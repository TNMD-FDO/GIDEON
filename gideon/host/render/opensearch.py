"""OpenSearch identity and the settings and security files it reads at start.

The service, probe, and disk layout share these release facts without
importing one another. The security documents contain a password placeholder,
not a password; the node resolves it from its environment on each start.
"""

from collections.abc import Mapping
from typing import Final

from gideon.host.render import Artifact, RenderInputs
from gideon.host.render.yamlout import dump

OPENSEARCH_SERVICE_NAME: Final = "opensearch"
OPENSEARCH_REST_PORT: Final = 9200
OPENSEARCH_USER: Final = "gideon"
OPENSEARCH_PASSWORD_SECRET_NAME: Final = "opensearch_password"
OPENSEARCH_KEY_SECRET_NAME: Final = "opensearch_transport_key"
OPENSEARCH_CERT_SECRET_NAME: Final = "opensearch_transport_cert"
OPENSEARCH_JOB_NAME: Final = "opensearch"
OPENSEARCH_HEALTH_PATH: Final = "/_plugins/_security/health"
OPENSEARCH_DATA_ROOT: Final = "/data/fast/opensearch"
OPENSEARCH_DATA_MOUNT: Final = "/usr/share/opensearch/data"
OPENSEARCH_SNAPSHOTS_ROOT: Final = "/data/fast/opensearch-snapshots"
OPENSEARCH_SNAPSHOTS_MOUNT: Final = "/usr/share/opensearch/snapshots"
OPENSEARCH_REPOSITORY: Final = "gideon"
OPENSEARCH_UID: Final = 1000
OPENSEARCH_GID: Final = 1000
OPENSEARCH_SETTINGS_PATH: Final = "/usr/share/opensearch/config/opensearch.yml"
OPENSEARCH_SECURITY_PATH: Final = "/usr/share/opensearch/config/opensearch-security"
OPENSEARCH_PASSWORD_VARIABLE: Final = "GIDEON_OPENSEARCH_PASSWORD"
OPENSEARCH_METRICS_JOB_NAME: Final = "opensearch-metrics"
# exempt: the metrics plugin's own endpoint, fixed by the plugin.
OPENSEARCH_METRICS_PATH: Final = "/_prometheus/metrics"
OPENSEARCH_METRICS_USER: Final = "metrics"
OPENSEARCH_METRICS_ROLE: Final = "metrics"
OPENSEARCH_METRICS_SECRET_NAME: Final = "opensearch_metrics_password"
OPENSEARCH_METRICS_PASSWORD_VARIABLE: Final = "GIDEON_OPENSEARCH_METRICS_PASSWORD"
# The plugin answers a scrape by running its inner requests as the caller, so
# the metrics role holds the plugin's own action and each request it makes:
# cluster health, nodes info, and nodes stats for the node series, and indices
# stats for the per-index series. It holds no read, search, or cluster-state
# action, so the credential lists and reads no document.
OPENSEARCH_METRICS_CLUSTER_PERMISSIONS: Final = (
    "cluster:monitor/prometheus/metrics",
    "cluster:monitor/health",
    "cluster:monitor/nodes/info",
    "cluster:monitor/nodes/stats*",
    "indices:monitor/stats*",
)
OPENSEARCH_METRICS_INDEX_PATTERN: Final = "*"
OPENSEARCH_METRICS_INDEX_PERMISSIONS: Final = ("indices:monitor/stats*",)
# exempt: 1024 MiB is the acceptance machine's floor, not a tuned heap value.
OPENSEARCH_NO_GPU_HEAP_MIB: Final = 1024
# exempt: 31 GiB (31744 MiB) is the JVM compressed-pointer ceiling;
# the lock's GB is decimal.
_HEAP_CAP_MIB: Final = 31 * 1024
_MEBIBYTE: Final = 1024 * 1024


def opensearch_heap_mib(limit_bytes: int) -> int:
    """Half a byte limit, capped and rounded down to whole mebibytes."""

    return min(limit_bytes // 2 // _MEBIBYTE, _HEAP_CAP_MIB)


def opensearch_health_url(host: str) -> str:
    """Return the unauthenticated security-health URL for a reachable host."""

    return f"http://{host}:{OPENSEARCH_REST_PORT}{OPENSEARCH_HEALTH_PATH}"


def opensearch_metrics_target() -> str:
    """Return the plugin's Compose-network host and port."""

    return f"{OPENSEARCH_SERVICE_NAME}:{OPENSEARCH_REST_PORT}"


def opensearch_settings_document() -> Mapping[str, object]:
    """Render the one-node settings the security plugin requires."""

    certificate = f"/run/secrets/{OPENSEARCH_CERT_SECRET_NAME}"
    return {
        # A stable name identifies this one-node cluster in its own responses.
        "cluster.name": "gideon",
        # The node binds only inside the unpublished Compose network.
        "network.host": "0.0.0.0",
        # There is no peer from which to discover another node.
        "discovery.type": "single-node",
        # An fs repository may only be registered under this directory.
        "path.repo": [OPENSEARCH_SNAPSHOTS_MOUNT],
        # REST is plaintext only inside the Compose network; transport keeps TLS.
        "plugins.security.ssl.http.enabled": False,
        # The transport listener requires a certificate and its private key.
        "plugins.security.ssl.transport.pemcert_filepath": certificate,
        "plugins.security.ssl.transport.pemkey_filepath": f"/run/secrets/{OPENSEARCH_KEY_SECRET_NAME}",
        # A self-signed transport certificate is its own trust anchor.
        "plugins.security.ssl.transport.pemtrustedcas_filepath": certificate,
        # The six mounted security files populate a new security index once.
        "plugins.security.allow_default_init_securityindex": True,
        # One node cannot place a replica; this node setting covers new indexes.
        "cluster.default_number_of_replicas": 0,
        # Query insights otherwise stores request text in a local index.
        "search.insights.top_queries.latency.enabled": False,
        "search.insights.top_queries.cpu.enabled": False,
        "search.insights.top_queries.memory.enabled": False,
        # exempt: per-index series are the figures the corpus board draws.
        "prometheus.indices": True,
        # exempt: cluster-settings series repeat the rendered settings file.
        "prometheus.cluster.settings": False,
    }


def _meta_document(kind: str) -> dict[str, object]:
    return {"_meta": {"type": kind, "config_version": 2}}


def opensearch_config_document() -> Mapping[str, object]:
    """One basic-auth domain backed by reserved internal users."""

    return {
        **_meta_document("config"),
        "config": {
            "dynamic": {
                # No anonymous route can become a data-plane credential.
                "http": {"anonymous_auth_enabled": False},
                "authc": {
                    # Basic auth challenges callers and checks the internal
                    # store; no transport auth domain is needed on one node.
                    "basic_internal_auth_domain": {
                        "http_enabled": True,
                        "order": 0,
                        "http_authenticator": {"type": "basic", "challenge": True},
                        "authentication_backend": {"type": "intern"},
                    }
                },
            }
        },
    }


def opensearch_internal_users_document() -> Mapping[str, object]:
    """Reserved users whose passwords resolve from wrapper variables."""

    return {
        **_meta_document("internalusers"),
        OPENSEARCH_USER: {
            # The index keeps this placeholder, so a file rotation takes effect
            # on restart without a second stored password or a fallback value.
            "hash": f"${{envbc.{OPENSEARCH_PASSWORD_VARIABLE}}}",
            # The REST account API must not create another credential home.
            "reserved": True,
        },
        OPENSEARCH_METRICS_USER: {
            "hash": f"${{envbc.{OPENSEARCH_METRICS_PASSWORD_VARIABLE}}}",
            "reserved": True,
        },
    }


def opensearch_roles_mapping_document() -> Mapping[str, object]:
    """Map each internal user to its own role."""

    return {
        **_meta_document("rolesmapping"),
        "all_access": {"users": [OPENSEARCH_USER]},
        OPENSEARCH_METRICS_ROLE: {"users": [OPENSEARCH_METRICS_USER]},
    }


class _OpensearchArtifact(Artifact):
    owners = (OPENSEARCH_SERVICE_NAME,)


class OpensearchSettingsArtifact(_OpensearchArtifact):
    """The node settings file mounted over the image's shipped settings."""

    name = "opensearch-settings"
    relative_path = "opensearch/opensearch.yml"

    def emit(self, inputs: RenderInputs) -> str:
        del inputs
        return dump(opensearch_settings_document())


class OpensearchConfigArtifact(_OpensearchArtifact):
    """The security plugin's basic-auth domain."""

    name = "opensearch-config"
    relative_path = "opensearch/security/config.yml"

    def emit(self, inputs: RenderInputs) -> str:
        del inputs
        return dump(opensearch_config_document())


class OpensearchInternalUsersArtifact(_OpensearchArtifact):
    """The reserved internal users and their environment placeholders."""

    name = "opensearch-internal-users"
    relative_path = "opensearch/security/internal_users.yml"

    def emit(self, inputs: RenderInputs) -> str:
        del inputs
        return dump(opensearch_internal_users_document())


class OpensearchRolesMappingArtifact(_OpensearchArtifact):
    """The data-plane and metrics user-to-role mappings."""

    name = "opensearch-roles-mapping"
    relative_path = "opensearch/security/roles_mapping.yml"

    def emit(self, inputs: RenderInputs) -> str:
        del inputs
        return dump(opensearch_roles_mapping_document())


class OpensearchRolesArtifact(_OpensearchArtifact):
    """The metrics reader's restricted role beside the built-in roles."""

    name = "opensearch-roles"
    relative_path = "opensearch/security/roles.yml"

    def emit(self, inputs: RenderInputs) -> str:
        del inputs
        return dump({
            **_meta_document("roles"),
            OPENSEARCH_METRICS_ROLE: {
                "reserved": True,
                "cluster_permissions": list(OPENSEARCH_METRICS_CLUSTER_PERMISSIONS),
                "index_permissions": [{
                    "index_patterns": [OPENSEARCH_METRICS_INDEX_PATTERN],
                    "allowed_actions": list(OPENSEARCH_METRICS_INDEX_PERMISSIONS),
                }],
            },
        })


class OpensearchActionGroupsArtifact(_OpensearchArtifact):
    """An empty custom-action-group document required on initialisation."""

    name = "opensearch-action-groups"
    relative_path = "opensearch/security/action_groups.yml"

    def emit(self, inputs: RenderInputs) -> str:
        del inputs
        return dump(_meta_document("actiongroups"))


class OpensearchTenantsArtifact(_OpensearchArtifact):
    """An empty tenant document required on initialisation."""

    name = "opensearch-tenants"
    relative_path = "opensearch/security/tenants.yml"

    def emit(self, inputs: RenderInputs) -> str:
        del inputs
        return dump(_meta_document("tenants"))
