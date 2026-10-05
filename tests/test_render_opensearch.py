"""OpenSearch's rendered block, startup files, and heap allocation."""

import hashlib
import subprocess
import sys
import tempfile
import unittest
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
    OPENSEARCH_NO_GPU_HEAP_MIB,
    OPENSEARCH_PASSWORD_SECRET_NAME,
    OPENSEARCH_PASSWORD_VARIABLE,
    OPENSEARCH_SECURITY_PATH,
    OPENSEARCH_SERVICE_NAME,
    OPENSEARCH_SETTINGS_PATH,
    OPENSEARCH_USER,
    opensearch_health_url,
    opensearch_heap_mib,
)
from gideon.host.render.prometheus import PrometheusConfigArtifact
from gideon.host.render.services import MountedSecret, secret_wrapper

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
    def test_definition_on_every_host(self) -> None:
        for base_inputs in HOSTS:
            secret_values = {
                OPENSEARCH_PASSWORD_SECRET_NAME: "fictitious-opensearch-password",
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
                        f"/etc/gideon/rendered/opensearch/opensearch.yml:{OPENSEARCH_SETTINGS_PATH}:ro",
                        f"/etc/gideon/rendered/opensearch/security:{OPENSEARCH_SECURITY_PATH}:ro",
                    ],
                )
                self.assertEqual(
                    block["secrets"],
                    [
                        OPENSEARCH_PASSWORD_SECRET_NAME,
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
    def test_missing_and_empty_password_refuse_before_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            absent = Path(directory) / "absent"
            empty = Path(directory) / "empty"
            empty.write_text("")
            for secret in (absent, empty):
                with self.subTest(secret=secret):
                    result = subprocess.run(
                        secret_wrapper(
                            (
                                MountedSecret(
                                    str(secret),
                                    OPENSEARCH_PASSWORD_VARIABLE,
                                    "OpenSearch password",
                                ),
                            ),
                            sys.executable,
                            OPENSEARCH_SERVICE_NAME,
                        )
                        + ["-c", "raise SystemExit(23)"],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(len(result.stderr.splitlines()), 1)
                    self.assertIn(str(secret), result.stderr)

    def test_password_reaches_server_in_dotless_variable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "password"
            secret.write_text("fictitious-password\n")
            result = subprocess.run(
                secret_wrapper(
                    (
                        MountedSecret(
                            str(secret),
                            OPENSEARCH_PASSWORD_VARIABLE,
                            "OpenSearch password",
                        ),
                    ),
                    sys.executable,
                    OPENSEARCH_SERVICE_NAME,
                )
                + [
                    "-c",
                    "import os; raise SystemExit(os.environ.get('GIDEON_OPENSEARCH_PASSWORD') != 'fictitious-password')",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


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
            "plugins.security.ssl.http.enabled": False,
            "plugins.security.ssl.transport.pemcert_filepath": f"/run/secrets/{OPENSEARCH_CERT_SECRET_NAME}",
            "plugins.security.ssl.transport.pemkey_filepath": f"/run/secrets/{OPENSEARCH_KEY_SECRET_NAME}",
            "plugins.security.ssl.transport.pemtrustedcas_filepath": f"/run/secrets/{OPENSEARCH_CERT_SECRET_NAME}",
            "plugins.security.allow_default_init_securityindex": True,
            "cluster.default_number_of_replicas": 0,
            "search.insights.top_queries.latency.enabled": False,
            "search.insights.top_queries.cpu.enabled": False,
            "search.insights.top_queries.memory.enabled": False,
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

    def test_security_documents_have_one_user_and_no_demo_user(self) -> None:
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
                self.assertEqual(set(user), {"_meta", OPENSEARCH_USER})
                self.assertEqual(
                    user[OPENSEARCH_USER],
                    {
                        "hash": f"${{envbc.{OPENSEARCH_PASSWORD_VARIABLE}}}",
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
                    set(documents["roles_mapping"]), {"_meta", "all_access"}
                )
                for name in ("roles", "action_groups", "tenants"):
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
