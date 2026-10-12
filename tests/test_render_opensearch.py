"""OpenSearch's rendered block, startup files, and heap allocation."""

import hashlib
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from test_render import EXAMPLE, SECOND, inputs
from test_secrets import PAIR_CERT_TEXT, PAIR_KEY_TEXT

from gideon.host.images import parse_registry, reference
from gideon.host.models import GIGABYTE
from gideon.host.render import render_all
from gideon.host.render.command import (
    AppliedManifest,
    compose_digests,
    recreate_judgment,
)
from gideon.host.render.compose import SWAP_CEILING_BYTES, service_blocks, service_names
from gideon.host.render.grafana import GrafanaRulesArtifact
from gideon.host.render.opensearch import (
    OPENSEARCH_CERT_SECRET_NAME,
    OPENSEARCH_DATA_MOUNT,
    OPENSEARCH_DATA_ROOT,
    OPENSEARCH_HEALTH_PATH,
    OPENSEARCH_JOB_NAME,
    OPENSEARCH_KEY_SECRET_NAME,
    OPENSEARCH_METRICS_JOB_NAME,
    OPENSEARCH_METRICS_PASSWORD_VARIABLE,
    OPENSEARCH_METRICS_PATH,
    OPENSEARCH_METRICS_ROLE,
    OPENSEARCH_METRICS_SECRET_NAME,
    OPENSEARCH_METRICS_USER,
    OPENSEARCH_NO_GPU_HEAP_MIB,
    OPENSEARCH_PASSWORD_SECRET_NAME,
    OPENSEARCH_PASSWORD_VARIABLE,
    OPENSEARCH_SECURITY_PATH,
    OPENSEARCH_SERVICE_NAME,
    OPENSEARCH_SETTINGS_PATH,
    OPENSEARCH_SNAPSHOTS_MOUNT,
    OPENSEARCH_SNAPSHOTS_ROOT,
    OPENSEARCH_USER,
    opensearch_health_url,
    opensearch_heap_mib,
    opensearch_metrics_target,
)
from gideon.host.render.prometheus import PrometheusConfigArtifact
from gideon.host.render.services.opensearch import OpensearchService, opensearch_service
from gideon.host.render.services.prometheus import PrometheusService

HOSTS = (inputs(EXAMPLE), inputs(SECOND), inputs(EXAMPLE, no_gpu=True))
SECURITY_FILES = (
    "config",
    "internal_users",
    "roles_mapping",
    "roles",
    "action_groups",
    "tenants",
)
PATHS = ("opensearch/opensearch.yml",) + tuple(
    f"opensearch/security/{name}.yml" for name in SECURITY_FILES
)


class Block(unittest.TestCase):
    def test_definition_joins_the_store_tier(self) -> None:
        self.assertTrue(OpensearchService.store)

    def test_builder_uses_supplied_limit_without_profile_row(self) -> None:
        rendered_inputs = inputs()
        target = parse_registry(rendered_inputs.site.registry)
        assert target is not None
        row = rendered_inputs.profile.memory_row(OPENSEARCH_SERVICE_NAME)
        assert row is not None
        without_row = replace(
            rendered_inputs,
            profile=replace(
                rendered_inputs.profile,
                memory=tuple(
                    item
                    for item in rendered_inputs.profile.memory
                    if item.service != OPENSEARCH_SERVICE_NAME
                ),
            ),
        )
        limit_bytes = (row.gb + 1) * GIGABYTE
        supplied = opensearch_service(without_row, target, limit_bytes=limit_bytes)
        from_row = opensearch_service(rendered_inputs, target)
        supplied_environment = supplied["environment"]
        row_environment = from_row["environment"]
        assert isinstance(supplied_environment, dict)
        assert isinstance(row_environment, dict)
        supplied_heap = opensearch_heap_mib(limit_bytes)
        row_heap = opensearch_heap_mib(row.gb * GIGABYTE)
        self.assertEqual(
            supplied_environment["OPENSEARCH_JAVA_OPTS"],
            f"-Xms{supplied_heap}m -Xmx{supplied_heap}m -XX:-HeapDumpOnOutOfMemoryError",
        )
        self.assertEqual(
            row_environment["OPENSEARCH_JAVA_OPTS"],
            f"-Xms{row_heap}m -Xmx{row_heap}m -XX:-HeapDumpOnOutOfMemoryError",
        )
        with self.assertRaisesRegex(ValueError, "no memory row for"):
            opensearch_service(without_row, target)

    def test_definition_on_every_host(self) -> None:
        for base_inputs in HOSTS:
            secret_values = {
                OPENSEARCH_PASSWORD_SECRET_NAME: "fictitious-opensearch-password",
                OPENSEARCH_METRICS_SECRET_NAME: "fictitious-metrics-password",
                OPENSEARCH_KEY_SECRET_NAME: PAIR_KEY_TEXT,
                OPENSEARCH_CERT_SECRET_NAME: PAIR_CERT_TEXT,
            }
            rendered_inputs = replace(
                base_inputs, secrets={**base_inputs.secrets, **secret_values}
            )
            with self.subTest(
                site=rendered_inputs.site.hostname, no_gpu=rendered_inputs.no_gpu
            ):
                block = service_blocks(rendered_inputs)[OPENSEARCH_SERVICE_NAME]
                assert isinstance(block, dict)
                pin = next(
                    pin
                    for pin in rendered_inputs.images.images
                    if pin.name == OPENSEARCH_SERVICE_NAME
                )
                target = parse_registry(rendered_inputs.site.registry)
                assert target is not None
                self.assertEqual(block["image"], reference(target, pin))
                self.assertEqual(block["restart"], "unless-stopped")
                row = rendered_inputs.profile.memory_row(OPENSEARCH_SERVICE_NAME)
                assert row is not None
                heap = (
                    OPENSEARCH_NO_GPU_HEAP_MIB
                    if rendered_inputs.no_gpu
                    else opensearch_heap_mib(row.gb * GIGABYTE)
                )
                self.assertEqual(
                    block["environment"],
                    {
                        "TZ": rendered_inputs.site.office.timezone,
                        "DISABLE_INSTALL_DEMO_CONFIG": "true",
                        "DISABLE_PERFORMANCE_ANALYZER_AGENT_CLI": "true",
                        "OPENSEARCH_JAVA_OPTS": (
                            f"-Xms{heap}m -Xmx{heap}m -XX:-HeapDumpOnOutOfMemoryError"
                        ),
                    },
                )
                self.assertEqual(block["command"], ["opensearch"])
                self.assertEqual(
                    block["volumes"],
                    [
                        f"{OPENSEARCH_DATA_ROOT}:{OPENSEARCH_DATA_MOUNT}",
                        f"{OPENSEARCH_SNAPSHOTS_ROOT}:{OPENSEARCH_SNAPSHOTS_MOUNT}",
                        f"/etc/gideon/rendered/opensearch/opensearch.yml:{OPENSEARCH_SETTINGS_PATH}:ro",
                        f"/etc/gideon/rendered/opensearch/security:{OPENSEARCH_SECURITY_PATH}:ro",
                    ],
                )
                self.assertEqual(
                    block["secrets"],
                    [
                        OPENSEARCH_PASSWORD_SECRET_NAME,
                        OPENSEARCH_METRICS_SECRET_NAME,
                        OPENSEARCH_KEY_SECRET_NAME,
                        OPENSEARCH_CERT_SECRET_NAME,
                    ],
                )
                self.assertEqual(
                    block["group_add"], [str(rendered_inputs.facts.service_gid)]
                )
                self.assertEqual(
                    block["ulimits"], {"nofile": {"soft": 65536, "hard": 65536}}
                )
                self.assertEqual(block["networks"], ["gideon"])
                self.assertEqual(
                    block["healthcheck"]["test"],
                    [
                        "CMD",
                        "curl",
                        "-fsS",
                        "-o",
                        "/dev/null",
                        opensearch_health_url("127.0.0.1"),
                    ],
                )
                self.assertTrue(
                    opensearch_health_url("127.0.0.1").endswith(OPENSEARCH_HEALTH_PATH)
                )
                self.assertNotIn("ports", block)
                self.assertNotIn("user", block)
                self.assertEqual(
                    block["memswap_limit"], block["mem_limit"] + SWAP_CEILING_BYTES
                )
                self.assertNotEqual(block["memswap_limit"], block["mem_limit"])
                environment = str(block["environment"])
                for value in secret_values.values():
                    self.assertNotIn(value.strip(), environment)


class Wrapper(unittest.TestCase):
    def run_rendered_entrypoint(
        self, files: Mapping[str, str | None]
    ) -> subprocess.CompletedProcess[str]:
        block = service_blocks(inputs())[OPENSEARCH_SERVICE_NAME]
        assert isinstance(block, dict)
        entrypoint = block["entrypoint"]
        command = block["command"]
        assert isinstance(entrypoint, list)
        assert isinstance(command, list)
        self.assertEqual(entrypoint[:2], ["sh", "-c"])
        self.assertEqual(entrypoint[3], OPENSEARCH_SERVICE_NAME)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "opensearch-docker-entrypoint.sh"
            script.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"${{{OPENSEARCH_PASSWORD_VARIABLE}}}\" "
                f"\"${{{OPENSEARCH_METRICS_PASSWORD_VARIABLE}}}\"\n"
            )
            script.chmod(0o700)
            rendered_script = entrypoint[2]
            for name, value in files.items():
                path = root / name
                if value is not None:
                    path.write_text(value)
                rendered_script = rendered_script.replace(f"/run/secrets/{name}", str(path))
            return subprocess.run(
                [*entrypoint[:2], rendered_script, entrypoint[3], *command],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
            )

    def test_missing_and_empty_either_password_refuse_before_server(self) -> None:
        main = OPENSEARCH_PASSWORD_SECRET_NAME
        metrics = OPENSEARCH_METRICS_SECRET_NAME
        for name, files in (
            (main, {main: None, metrics: "fixture-metrics\n"}),
            (main, {main: "", metrics: "fixture-metrics\n"}),
            (metrics, {main: "fixture-main\n", metrics: None}),
            (metrics, {main: "fixture-main\n", metrics: ""}),
            (main, {main: None, metrics: None}),
        ):
            with self.subTest(secret=name, files=files):
                result = self.run_rendered_entrypoint(files)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assertIn(name, result.stderr)
                self.assertIn("missing or empty", result.stderr)

    def test_both_passwords_reach_server_in_dotless_variables(self) -> None:
        self.assertNotIn(".", OPENSEARCH_PASSWORD_VARIABLE)
        self.assertNotIn(".", OPENSEARCH_METRICS_PASSWORD_VARIABLE)
        result = self.run_rendered_entrypoint({
            OPENSEARCH_PASSWORD_SECRET_NAME: "fixture-main\n",
            OPENSEARCH_METRICS_SECRET_NAME: "fixture-metrics\n",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["fixture-main", "fixture-metrics"])


class Heap(unittest.TestCase):
    def test_half_the_callers_limit_rounded_down_to_mebibytes(self) -> None:
        mib = 1024 * 1024
        self.assertEqual(opensearch_heap_mib(8 * mib), 4)
        self.assertEqual(opensearch_heap_mib(9 * mib + 2 * mib - 1), 5)
        self.assertEqual(opensearch_heap_mib(48 * GIGABYTE), 22888)

    def test_cap_is_31_gib_above_a_62_gib_limit(self) -> None:
        self.assertEqual(opensearch_heap_mib(64 * 1024**3), 31 * 1024)


class Files(unittest.TestCase):
    def test_settings_have_exact_release_keys(self) -> None:
        expected = {
            "cluster.name": "gideon",
            "network.host": "0.0.0.0",
            "discovery.type": "single-node",
            "path.repo": [OPENSEARCH_SNAPSHOTS_MOUNT],
            "plugins.security.ssl.http.enabled": False,
            "plugins.security.ssl.transport.pemcert_filepath": f"/run/secrets/{OPENSEARCH_CERT_SECRET_NAME}",
            "plugins.security.ssl.transport.pemkey_filepath": f"/run/secrets/{OPENSEARCH_KEY_SECRET_NAME}",
            "plugins.security.ssl.transport.pemtrustedcas_filepath": f"/run/secrets/{OPENSEARCH_CERT_SECRET_NAME}",
            "plugins.security.allow_default_init_securityindex": True,
            "cluster.default_number_of_replicas": 0,
            "search.insights.top_queries.latency.enabled": False,
            "search.insights.top_queries.cpu.enabled": False,
            "search.insights.top_queries.memory.enabled": False,
            "prometheus.indices": True,
            "prometheus.cluster.settings": False,
        }
        for rendered_inputs in HOSTS:
            with self.subTest(
                site=rendered_inputs.site.hostname, no_gpu=rendered_inputs.no_gpu
            ):
                settings = yaml.safe_load(
                    render_all(rendered_inputs).by_path[PATHS[0]].content
                )
                self.assertEqual(settings, expected)
                for absent in (
                    "plugins.security.disabled",
                    "plugins.security.restapi.roles_enabled",
                    "plugins.security.audit.type",
                    "plugins.security.nodes_dn",
                    "plugins.security.authcz.admin_dn",
                ):
                    self.assertNotIn(absent, settings)

    def test_each_security_file_has_its_plugin_meta_block(self) -> None:
        kinds = {
            "config": "config",
            "internal_users": "internalusers",
            "roles_mapping": "rolesmapping",
            "roles": "roles",
            "action_groups": "actiongroups",
            "tenants": "tenants",
        }
        for rendered_inputs in HOSTS:
            files = render_all(rendered_inputs).by_path
            for name, kind in kinds.items():
                with self.subTest(
                    site=rendered_inputs.site.hostname,
                    no_gpu=rendered_inputs.no_gpu,
                    file=name,
                ):
                    document = yaml.safe_load(
                        files[f"opensearch/security/{name}.yml"].content
                    )
                    self.assertEqual(
                        document["_meta"], {"type": kind, "config_version": 2}
                    )

    def test_security_documents_restrict_both_users_and_the_metrics_role(self) -> None:
        for rendered_inputs in HOSTS:
            with self.subTest(
                site=rendered_inputs.site.hostname, no_gpu=rendered_inputs.no_gpu
            ):
                files = render_all(rendered_inputs).by_path
                documents = {
                    name: yaml.safe_load(
                        files[f"opensearch/security/{name}.yml"].content
                    )
                    for name in SECURITY_FILES
                }
                user = documents["internal_users"]
                self.assertEqual(set(user), {"_meta", OPENSEARCH_USER, OPENSEARCH_METRICS_USER})
                self.assertEqual(
                    user[OPENSEARCH_USER],
                    {
                        "hash": f"${{envbc.{OPENSEARCH_PASSWORD_VARIABLE}}}",
                        "reserved": True,
                    },
                )
                self.assertEqual(
                    user[OPENSEARCH_METRICS_USER],
                    {
                        "hash": f"${{envbc.{OPENSEARCH_METRICS_PASSWORD_VARIABLE}}}",
                        "reserved": True,
                    },
                )
                config = documents["config"]["config"]["dynamic"]
                self.assertEqual(config["http"], {"anonymous_auth_enabled": False})
                self.assertEqual(set(config["authc"]), {"basic_internal_auth_domain"})
                auth = config["authc"]["basic_internal_auth_domain"]
                self.assertTrue(auth["http_authenticator"]["challenge"])
                self.assertNotIn("transport_enabled", auth)
                self.assertEqual(
                    documents["roles_mapping"]["all_access"],
                    {"users": [OPENSEARCH_USER]},
                )
                self.assertEqual(
                    documents["roles_mapping"][OPENSEARCH_METRICS_ROLE],
                    {"users": [OPENSEARCH_METRICS_USER]},
                )
                self.assertEqual(
                    set(documents["roles_mapping"]),
                    {"_meta", "all_access", OPENSEARCH_METRICS_ROLE},
                )
                role = documents["roles"][OPENSEARCH_METRICS_ROLE]
                self.assertEqual(set(documents["roles"]), {"_meta", OPENSEARCH_METRICS_ROLE})
                self.assertEqual(
                    role,
                    {
                        "reserved": True,
                        "cluster_permissions": [
                            "cluster:monitor/prometheus/metrics",
                            "cluster:monitor/health",
                            "cluster:monitor/nodes/info",
                            "cluster:monitor/nodes/stats*",
                            "indices:monitor/stats*",
                        ],
                        "index_permissions": [{
                            "index_patterns": ["*"],
                            "allowed_actions": ["indices:monitor/stats*"],
                        }],
                    },
                )
                actions = [
                    *role["cluster_permissions"],
                    *role["index_permissions"][0]["allowed_actions"],
                ]
                for action in actions:
                    for forbidden in ("read", "search", "write", "cluster:monitor/state"):
                        self.assertNotIn(forbidden, action)
                for name in ("action_groups", "tenants"):
                    self.assertEqual(set(documents[name]), {"_meta"})
                for text in (files[path].content for path in PATHS):
                    for demo in (
                        "admin",
                        "anomalyadmin",
                        "kibanaserver",
                        "kibanaro",
                        "logstash",
                        "readall",
                        "snapshotrestore",
                    ):
                        self.assertNotIn(demo, text)

    def test_seven_artifacts_exist_on_every_host_with_one_owner(self) -> None:
        for rendered_inputs in HOSTS:
            with self.subTest(
                site=rendered_inputs.site.hostname, no_gpu=rendered_inputs.no_gpu
            ):
                files = render_all(rendered_inputs).by_path
                for path in PATHS:
                    with self.subTest(path=path):
                        self.assertIn(path, files)
                        self.assertEqual(files[path].owners, (OPENSEARCH_SERVICE_NAME,))
                        self.assertFalse(files[path].secret)

    def test_changed_settings_recreates_opensearch_alone(self) -> None:
        rendered_inputs = inputs()
        rendered = render_all(rendered_inputs)
        digests = compose_digests(rendered_inputs)
        files = {
            item.relative_path: {
                "sha256": (
                    "0" * 64
                    if item.relative_path == PATHS[0]
                    else hashlib.sha256(item.content.encode()).hexdigest()
                ),
                "owners": list(item.owners),
            }
            for item in rendered.files
        }
        applied = AppliedManifest(files, digests.services, digests.top_level)
        judgment = recreate_judgment(
            rendered, applied, service_names(rendered_inputs), digests
        )
        self.assertEqual(judgment.services, (OPENSEARCH_SERVICE_NAME,))
        self.assertEqual(judgment.files, (OPENSEARCH_SERVICE_NAME,))


class Metrics(unittest.TestCase):
    def test_scrape_job_reads_the_secret_file_on_every_host(self) -> None:
        for rendered_inputs in HOSTS:
            with self.subTest(
                site=rendered_inputs.site.hostname, no_gpu=rendered_inputs.no_gpu
            ):
                jobs = yaml.safe_load(PrometheusConfigArtifact().emit(rendered_inputs))[
                    "scrape_configs"
                ]
                matches = [
                    job for job in jobs if job["job_name"] == OPENSEARCH_METRICS_JOB_NAME
                ]
                self.assertEqual(len(matches), 1)
                job = matches[0]
                self.assertEqual(job["metrics_path"], OPENSEARCH_METRICS_PATH)
                self.assertEqual(job["metrics_path"], "/_prometheus/metrics")
                self.assertEqual(
                    job["static_configs"],
                    [{"targets": [opensearch_metrics_target()]}],
                )
                self.assertEqual(
                    job["basic_auth"],
                    {
                        "username": OPENSEARCH_METRICS_USER,
                        "password_file": f"/run/secrets/{OPENSEARCH_METRICS_SECRET_NAME}",
                    },
                )
                self.assertNotIn("password", job)
                self.assertNotIn("password", job["basic_auth"])

    def test_prometheus_mounts_only_the_metrics_password_with_service_group(self) -> None:
        for rendered_inputs in HOSTS:
            with self.subTest(
                site=rendered_inputs.site.hostname, no_gpu=rendered_inputs.no_gpu
            ):
                block = service_blocks(rendered_inputs)[PrometheusService.name]
                assert isinstance(block, dict)
                self.assertEqual(block["secrets"], [OPENSEARCH_METRICS_SECRET_NAME])
                self.assertEqual(block["group_add"], [str(rendered_inputs.facts.service_gid)])


class Probe(unittest.TestCase):
    def test_job_probes_the_credential_free_health_path_on_every_host(self) -> None:
        for rendered_inputs in HOSTS:
            with self.subTest(
                site=rendered_inputs.site.hostname, no_gpu=rendered_inputs.no_gpu
            ):
                jobs = yaml.safe_load(PrometheusConfigArtifact().emit(rendered_inputs))[
                    "scrape_configs"
                ]
                matches = [
                    job for job in jobs if job["job_name"] == OPENSEARCH_JOB_NAME
                ]
                self.assertEqual(len(matches), 1)
                job = matches[0]
                self.assertEqual(job["metrics_path"], "/probe")
                self.assertEqual(job["params"], {"module": ["http_2xx"]})
                self.assertEqual(
                    job["static_configs"],
                    [{"targets": [opensearch_health_url(OPENSEARCH_SERVICE_NAME)]}],
                )
                self.assertEqual(
                    job["relabel_configs"],
                    [
                        {
                            "source_labels": ["__address__"],
                            "target_label": "__param_target",
                        },
                        {
                            "source_labels": ["__param_target"],
                            "target_label": "instance",
                        },
                        {
                            "target_label": "__address__",
                            "replacement": "blackbox-exporter:9115",
                        },
                    ],
                )
                self.assertNotIn("authorization", job)

    def test_rule_pages_on_probe_failure_on_every_host(self) -> None:
        for rendered_inputs in HOSTS:
            with self.subTest(
                site=rendered_inputs.site.hostname, no_gpu=rendered_inputs.no_gpu
            ):
                groups = yaml.safe_load(GrafanaRulesArtifact().emit(rendered_inputs))[
                    "groups"
                ]
                matches = [
                    (group["name"], rule)
                    for group in groups
                    for rule in group["rules"]
                    if rule["uid"] == "gideon-opensearch-probe-failing"
                ]
                self.assertEqual(len(matches), 1)
                group_name, rule = matches[0]
                self.assertEqual(group_name, "metrics")
                self.assertEqual(rule["title"], "OpenSearch probe failing")
                self.assertEqual(
                    rule["data"][0]["model"]["expr"],
                    f'probe_success{{job="{OPENSEARCH_JOB_NAME}"}} == bool 0',
                )
                self.assertEqual(rule["for"], "10m")
                self.assertEqual(rule["noDataState"], "OK")
                self.assertEqual(rule["execErrState"], "OK")
                self.assertEqual(rule["labels"], {"class": "page"})
                self.assertEqual(
                    rule["annotations"],
                    {
                        "summary": "The OpenSearch probe is failing",
                        "runbook": "docs/runbooks/observability.md §4",
                    },
                )


if __name__ == "__main__":
    unittest.main()
