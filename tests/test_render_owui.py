"""The frontend's rendered artifacts: env, env_file, manifest, and reconcile units."""

import unittest
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from string import Template
from typing import cast

import yaml  # type: ignore[import-untyped]

from gideon.host.images import load_image_lock
from gideon.host.lock import load_host_lock
from gideon.host.models import (
    GIGABYTE,
    HardwareProfile,
    load_models_lock,
    select_profile,
)
from gideon.host.render import ARTIFACTS, RenderInputs, render_all
from gideon.host.render.api import API_SERVICE_NAME, api_base_url
from gideon.host.render.compose import STORE_SERVICES, service_names
from gideon.host.render.drill import (
    DRILL_PORT,
    DRILL_PROJECT,
    DRILL_ROOT,
    drill_compose_document,
)
from gideon.host.render.engine import ENGINE_SERVICE_NAME
from gideon.host.render.facts import HostFacts
from gideon.host.render.owui import (
    ALLOWED_ENDPOINTS,
    AUDIT_EXCLUDED_PATHS,
    AUDIT_LOG_FILE,
    BASE_MODEL_CAPABILITIES,
    BASE_MODEL_HIDDEN,
    BRANCH_GATE_DESCRIPTION,
    BRANCH_GATE_ID,
    BRANCH_GATE_NAME,
    BRANCH_GATE_TEMPLATE,
    BREAK_GLASS,
    EVAL_IDENTITY,
    FEEDBACK_LIST_ROUTE,
    GENERAL_CAPABILITIES,
    GENERAL_FUNCTION_CALLING,
    GENERAL_PRESET_ID,
    MODEL_GRANT_CREATED_AT,
    MODEL_GRANT_ID,
    MODEL_GRANT_RESOURCE_TYPE,
    OWUI_SECRET_NAMES,
    SERVICE_GROUP,
    SYNC_ROW_CREATED_AT,
    SYNC_ROW_UPDATED_AT,
    SYNC_ROW_USER_ID,
    WEB_LOADER_TIMEOUT_SECONDS,
    WEB_LOADER_USER_AGENT,
    WEB_SEARCH_CONFIRMATION_TEXT,
    WEB_SEARCH_RESULT_COUNT,
    ApplyManifestArtifact,
    OwuiEnvArtifact,
    branch_gate_function,
    general_preset_record,
    general_texts,
    owui_environment,
    owui_secret_environment,
    owui_secret_names,
    permission_tree,
    public_read_grant,
)
from gideon.host.render.searxng import searxng_query_url
from gideon.host.render.systemd import (
    BACKUP_CALENDAR,
    DRILL_CALENDAR,
    RECONCILE_CALENDAR,
    VERIFY_ALL_CALENDAR,
    BackupServiceArtifact,
    BackupTimerArtifact,
    DrillServiceArtifact,
    DrillTimerArtifact,
    ReconcileServiceArtifact,
    ReconcileTimerArtifact,
    VerifyServiceArtifact,
    VerifyTimerArtifact,
)
from gideon.host.site import load_site

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config/site.example.yaml"
SECOND = ROOT / "tests/fixtures/site/second-office.yaml"
DN_GROUPS = ROOT / "tests/fixtures/site/dn-groups.yaml"
TEMPLATE_PATHS = tuple(
    dict.fromkeys(path for artifact in ARTIFACTS for path in artifact.template_paths)
)
SECRETS = {
    "ldap_bind_password": "bind-$-'\"#password",
    "postgres_openwebui_password": "p@ss/word",
    "gideon_admin_password": "admin-password",
    "engine_api_key": "engine-api-key",
    "gideon_api_key": "gideon-api-key",
    "searxng_secret_key": "searxng-secret-key",
}


def inputs(site_path: Path = EXAMPLE, **overrides: object) -> RenderInputs:
    site = load_site(site_path).config
    lock = load_host_lock(ROOT / "host.lock").lock
    images = load_image_lock(ROOT / "images.lock").lock
    models = load_models_lock(ROOT / "models.lock").lock
    assert site is not None and lock is not None and images is not None and models is not None
    profile = select_profile(models, site.hardware_profile)
    assert isinstance(profile, HardwareProfile)
    base = RenderInputs(
        site=site,
        lock=lock,
        images=images,
        facts=HostFacts(("GPU-a", "GPU-b"), service_gid=4242),
        profile=profile,
        templates={name: (ROOT / "compose" / name).read_text() for name in TEMPLATE_PATHS},
        release="fixture",
        secrets=dict(SECRETS),
        checkout="/opt/gideon",
        api_sources_digest="sha256:" + "0" * 64,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


class Environment(unittest.TestCase):
    def test_rating_switch_is_on_for_every_rendered_host_kind(self) -> None:
        for site_path, no_gpu in ((EXAMPLE, False), (SECOND, False), (EXAMPLE, True)):
            with self.subTest(site_path=site_path, no_gpu=no_gpu):
                env = owui_environment(inputs(site_path, no_gpu=no_gpu))
                self.assertEqual(env["ENABLE_MESSAGE_RATING"], "true")
        self.assertEqual(len(ALLOWED_ENDPOINTS), 9)
        self.assertEqual(ALLOWED_ENDPOINTS[8], FEEDBACK_LIST_ROUTE)

    def test_the_identity_and_policy_env_is_the_spec_verbatim(self) -> None:
        env = owui_environment(inputs())
        self.assertEqual(env["WEBUI_NAME"], "GIDEON")
        self.assertEqual(env["WEBUI_URL"], "https://gideon.example.org")
        self.assertEqual(env["ENABLE_PERSISTENT_CONFIG"], "false")
        self.assertEqual(env["ENABLE_SIGNUP"], "false")
        self.assertEqual(env["DEFAULT_USER_ROLE"], "user")
        self.assertEqual(env["WEBUI_ADMIN_EMAIL"], BREAK_GLASS.email)
        self.assertEqual(env["WEBUI_SECRET_KEY_FILE"], "/run/secrets/webui_secret_key")
        self.assertEqual(env["JWT_EXPIRES_IN"], "12h")
        self.assertEqual(env["WEBUI_SESSION_COOKIE_SAME_SITE"], "strict")
        self.assertEqual(env["GLOBAL_LOG_LEVEL"], "INFO")
        self.assertEqual(env["AUDIT_LOG_LEVEL"], "METADATA")
        self.assertEqual(env["AUDIT_LOGS_FILE_PATH"], AUDIT_LOG_FILE)
        self.assertEqual(env["ENABLE_AUDIT_STDOUT"], "false")
        self.assertEqual(env["AUDIT_EXCLUDED_PATHS"], AUDIT_EXCLUDED_PATHS)
        self.assertEqual(env["ENABLE_AUDIT_GET_REQUESTS"], "false")
        self.assertEqual(env["AUDIT_UVICORN_LOGGER_NAMES"], "uvicorn.error")
        for name in (
            "ENABLE_OTEL",
            "ENABLE_OTEL_TRACES",
            "ENABLE_OTEL_METRICS",
            "ENABLE_OTEL_LOGS",
            "ENABLE_QUERIES_CACHE",
            "LOGURU_DIAGNOSE",
        ):
            self.assertEqual(env[name], "false")
        self.assertEqual(env["ENABLE_API_KEYS"], "true")
        self.assertEqual(env["ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS"], "true")
        self.assertEqual(env["API_KEYS_ALLOWED_ENDPOINTS"], ",".join(ALLOWED_ENDPOINTS))
        self.assertEqual(env["BYPASS_EMBEDDING_AND_RETRIEVAL"], "true")
        self.assertEqual(env["BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL"], "true")
        self.assertEqual(env["ENABLE_WEB_SEARCH"], "true")
        self.assertEqual(env["WEB_SEARCH_ENGINE"], "searxng")
        self.assertEqual(env["SEARXNG_QUERY_URL"], searxng_query_url())
        self.assertEqual(env["WEB_SEARCH_RESULT_COUNT"], str(WEB_SEARCH_RESULT_COUNT))
        self.assertEqual(env["WEB_LOADER_ENGINE"], "safe_web")
        self.assertEqual(env["USER_AGENT"], WEB_LOADER_USER_AGENT)
        self.assertEqual(env["WEB_LOADER_TIMEOUT"], str(WEB_LOADER_TIMEOUT_SECONDS))
        self.assertEqual(env["BYPASS_WEB_SEARCH_WEB_LOADER"], "false")
        self.assertEqual(env["WEB_SEARCH_TRUST_ENV"], "true")
        self.assertEqual(env["ENABLE_WEB_LOADER_SSL_VERIFICATION"], "true")
        self.assertEqual(env["ENABLE_WEB_SEARCH_CONFIRMATION"], "true")
        self.assertEqual(
            env["WEB_SEARCH_CONFIRMATION_CONTENT"], WEB_SEARCH_CONFIRMATION_TEXT
        )
        self.assertEqual(env["WEB_SEARCH_DOMAIN_FILTER_LIST"], "[]")
        self.assertEqual(env["DEFAULT_MODELS"], GENERAL_PRESET_ID)
        self.assertEqual(env["ENABLE_EVALUATION_ARENA_MODELS"], "false")
        self.assertEqual(env["ENABLE_OPENAI_API"], "true")
        self.assertEqual(env["OPENAI_API_BASE_URLS"], api_base_url())
        self.assertEqual(env["ENABLE_FORWARD_USER_INFO_HEADERS"], "true")
        generator = inputs().profile.model("generator")
        assert generator is not None
        self.assertEqual(env["TASK_MODEL_EXTERNAL"], generator.serve.served_name)
        self.assertEqual(
            {
                name: env[name]
                for name in (
                    "ENABLE_TITLE_GENERATION",
                    "ENABLE_TAGS_GENERATION",
                    "ENABLE_SEARCH_QUERY_GENERATION",
                    "ENABLE_RETRIEVAL_QUERY_GENERATION",
                    "ENABLE_FOLLOW_UP_GENERATION",
                    "ENABLE_AUTOCOMPLETE_GENERATION",
                    "DEFAULT_MODELS",
                )
            },
            {
                "ENABLE_TITLE_GENERATION": "true",
                "ENABLE_TAGS_GENERATION": "true",
                "ENABLE_SEARCH_QUERY_GENERATION": "true",
                "ENABLE_RETRIEVAL_QUERY_GENERATION": "false",
                "ENABLE_FOLLOW_UP_GENERATION": "false",
                "ENABLE_AUTOCOMPLETE_GENERATION": "false",
                "DEFAULT_MODELS": GENERAL_PRESET_ID,
            },
        )
        self.assertEqual(
            set(env) - set(owui_environment(inputs(), engine=False)),
            {
                "OPENAI_API_BASE_URLS",
                "ENABLE_FORWARD_USER_INFO_HEADERS",
                "TASK_MODEL_EXTERNAL",
                "ENABLE_TITLE_GENERATION",
                "ENABLE_TAGS_GENERATION",
                "ENABLE_SEARCH_QUERY_GENERATION",
                "ENABLE_RETRIEVAL_QUERY_GENERATION",
                "ENABLE_FOLLOW_UP_GENERATION",
                "ENABLE_AUTOCOMPLETE_GENERATION",
                "DEFAULT_MODELS",
                "WEB_SEARCH_ENGINE",
                "SEARXNG_QUERY_URL",
                "WEB_SEARCH_RESULT_COUNT",
                "WEB_LOADER_ENGINE",
                "USER_AGENT",
                "WEB_LOADER_TIMEOUT",
                "BYPASS_WEB_SEARCH_WEB_LOADER",
                "WEB_SEARCH_TRUST_ENV",
                "ENABLE_WEB_LOADER_SSL_VERIFICATION",
                "ENABLE_WEB_SEARCH_CONFIRMATION",
                "WEB_SEARCH_CONFIRMATION_CONTENT",
                "WEB_SEARCH_DOMAIN_FILTER_LIST",
            },
        )
        # With no OPENAI_API_CONFIGS, the pinned frontend uses the Chat
        # Completions path that supports withholding.
        for name in ("OPENAI_API_CONFIGS", "TASK_MODEL", "TASK_MODEL_PARAMS", "TEMPERATURE"):
            self.assertNotIn(name, env)
        self.assertEqual(env["ENABLE_VERSION_UPDATE_CHECK"], "false")
        self.assertEqual(env["CONTENT_SECURITY_POLICY"], "img-src 'self' data: blob:")
        self.assertNotIn("VECTOR_DB", env)
        self.assertNotIn("NO_PROXY", env)
        for value in env.values():
            self.assertNotIn("password", value.lower())

    def test_logging_audit_and_telemetry_posture_is_on_every_host_and_the_drill(self) -> None:
        """The pinned search-path posture is present on GPU, no-GPU, and drill frontends."""

        posture = {
            "GLOBAL_LOG_LEVEL": "INFO",
            "AUDIT_LOG_LEVEL": "METADATA",
            "AUDIT_LOGS_FILE_PATH": AUDIT_LOG_FILE,
            "ENABLE_AUDIT_STDOUT": "false",
            "AUDIT_EXCLUDED_PATHS": AUDIT_EXCLUDED_PATHS,
            "ENABLE_AUDIT_GET_REQUESTS": "false",
            "AUDIT_UVICORN_LOGGER_NAMES": "uvicorn.error",
            "ENABLE_OTEL": "false",
            "ENABLE_OTEL_TRACES": "false",
            "ENABLE_OTEL_METRICS": "false",
            "ENABLE_OTEL_LOGS": "false",
            "ENABLE_QUERIES_CACHE": "false",
            "LOGURU_DIAGNOSE": "false",
            "CONTENT_SECURITY_POLICY": "img-src 'self' data: blob:",
        }
        drill = drill_compose_document(inputs())
        services = drill["services"]
        assert isinstance(services, Mapping)
        drill_environment = services["open-webui"]["environment"]
        assert isinstance(drill_environment, Mapping)
        for environment in (
            owui_environment(inputs()),
            owui_environment(inputs(no_gpu=True)),
            owui_environment(inputs(), engine=False),
            drill_environment,
        ):
            with self.subTest(environment=environment):
                self.assertEqual(
                    {name: environment[name] for name in posture}, posture
                )
                self.assertNotIn("ENABLE_LOCAL_WEB_FETCH", environment)
        self.assertNotIn("ENABLE_RETRIEVAL_QUERY_GENERATION", drill_environment)

    def test_the_ldap_block_gates_login_on_the_users_group_dn(self) -> None:
        env = owui_environment(inputs())
        self.assertEqual(env["LDAP_SERVER_HOST"], "example.org")
        self.assertEqual(env["LDAP_SERVER_PORT"], "636")
        self.assertEqual(env["LDAP_USE_TLS"], "true")
        self.assertEqual(env["LDAP_VALIDATE_CERT"], "true")
        self.assertEqual(env["LDAP_CA_CERT_FILE"], "/etc/gideon/ca.pem")
        self.assertEqual(env["LDAP_APP_DN"], "svc-gideon-ldap@example.org")
        self.assertEqual(env["LDAP_SEARCH_FILTERS"], "(memberOf=CN=GIDEON-Users,CN=Users,DC=example,DC=org)")
        self.assertEqual(env["LDAP_ATTRIBUTE_FOR_USERNAME"], "sAMAccountName")
        self.assertEqual(env["LDAP_ATTRIBUTE_FOR_MAIL"], "userPrincipalName")
        self.assertEqual(env["ENABLE_LDAP_GROUP_MANAGEMENT"], "true")
        self.assertEqual(env["ENABLE_LDAP_GROUP_CREATION"], "false")
        self.assertNotIn("LDAP_APP_PASSWORD", env)

    def test_search_false_leaves_only_the_search_switch(self) -> None:
        enabled = owui_environment(inputs())
        disabled = owui_environment(inputs(), search=False)
        self.assertEqual(disabled["ENABLE_WEB_SEARCH"], "false")
        self.assertEqual(
            set(enabled) - set(disabled),
            {
                "WEB_SEARCH_ENGINE",
                "SEARXNG_QUERY_URL",
                "WEB_SEARCH_RESULT_COUNT",
                "WEB_LOADER_ENGINE",
                "USER_AGENT",
                "WEB_LOADER_TIMEOUT",
                "BYPASS_WEB_SEARCH_WEB_LOADER",
                "WEB_SEARCH_TRUST_ENV",
                "ENABLE_WEB_LOADER_SSL_VERIFICATION",
                "ENABLE_WEB_SEARCH_CONFIRMATION",
                "WEB_SEARCH_CONFIRMATION_CONTENT",
                "WEB_SEARCH_DOMAIN_FILTER_LIST",
            },
        )

    def test_directory_false_disables_ldap_without_directory_settings(self) -> None:
        enabled = owui_environment(inputs())
        disabled = owui_environment(inputs(), directory=False)
        self.assertEqual(disabled["ENABLE_LDAP"], "false")
        self.assertFalse(
            any(
                name.startswith("LDAP_") or name.startswith("ENABLE_LDAP_")
                for name in disabled
            )
        )
        self.assertEqual(
            set(enabled) - set(disabled),
            {
                name
                for name in enabled
                if name.startswith("LDAP_") or name.startswith("ENABLE_LDAP_")
            },
        )

    def test_a_dn_form_group_is_used_as_given(self) -> None:
        env = owui_environment(inputs(DN_GROUPS))
        self.assertEqual(env["LDAP_SEARCH_FILTERS"], "(memberOf=CN=GIDEON-Users,OU=Security Groups,DC=ad,DC=test)")

    def test_filter_syntax_characters_in_a_dn_are_escaped(self) -> None:
        site = load_site(DN_GROUPS).config
        assert site is not None
        escaped = replace(site.auth.ldap, users_group="CN=GIDEON (Users)\\, all,OU=Groups,DC=ad,DC=test")
        env = owui_environment(inputs(DN_GROUPS, site=replace(site, auth=replace(site.auth, ldap=escaped))))
        self.assertEqual(env["LDAP_SEARCH_FILTERS"], "(memberOf=CN=GIDEON \\28Users\\29\\5c, all,OU=Groups,DC=ad,DC=test)")

    def test_permission_env_covers_every_leaf_with_the_access_suffix_rule(self) -> None:
        """Each permission leaf gets an environment entry; workspace.knowledge stays disabled until Ingestion GA (v0.7.0)."""

        env = owui_environment(inputs())
        tree = permission_tree(inputs())
        leaves = sum(len(section) for section in tree.values())
        names = [name for name in env if name.startswith("USER_PERMISSIONS_")]
        self.assertEqual(len(names), leaves)
        self.assertEqual(env["USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ACCESS"], "false")
        self.assertEqual(env["USER_PERMISSIONS_WORKSPACE_MODELS_ACCESS"], "false")
        self.assertEqual(env["USER_PERMISSIONS_WORKSPACE_MODELS_IMPORT"], "false")
        self.assertEqual(env["USER_PERMISSIONS_FEATURES_API_KEYS"], "false")
        self.assertEqual(env["USER_PERMISSIONS_FEATURES_WEB_SEARCH"], "true")
        self.assertEqual(env["USER_PERMISSIONS_CHAT_CONTROLS"], "false")
        self.assertEqual(env["USER_PERMISSIONS_CHAT_WEB_UPLOAD"], "false")
        self.assertEqual(env["USER_PERMISSIONS_CHAT_MULTIPLE_MODELS"], "false")
        self.assertEqual(env["USER_PERMISSIONS_SHARING_PROMPTS"], "true")
        for name, value in env.items():
            if name.startswith("USER_PERMISSIONS_SHARING_PUBLIC_"):
                self.assertEqual(value, "false", name)

    def test_proxied_site_gets_only_no_proxy_in_the_open_env(self) -> None:
        second = inputs(SECOND)
        env = owui_environment(second)
        self.assertTrue(
            env["NO_PROXY"].endswith(
                ",caddy,postgres,open-webui,gideon-api"
            )
        )
        self.assertIn(API_SERVICE_NAME, env["NO_PROXY"])
        self.assertNotIn(ENGINE_SERVICE_NAME, env["NO_PROXY"])
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("HTTPS_PROXY", env)
        searching_site = replace(
            second.site,
            web=replace(second.site.web, search="on"),
        )
        searching = owui_environment(replace(second, site=searching_site))
        self.assertTrue(
            searching["NO_PROXY"].endswith(
                ",caddy,postgres,open-webui,gideon-api,searxng"
            )
        )

    def test_no_gpu_and_explicitly_disconnected_environments_have_no_connection(self) -> None:
        no_gpu = owui_environment(inputs(no_gpu=True))
        disconnected = owui_environment(inputs(), engine=False)
        self.assertNotEqual(no_gpu, owui_environment(inputs(no_gpu=True), engine=False))
        self.assertEqual(no_gpu["ENABLE_WEB_SEARCH"], "true")
        self.assertEqual(no_gpu["USER_AGENT"], WEB_LOADER_USER_AGENT)
        self.assertEqual(no_gpu["WEB_LOADER_TIMEOUT"], str(WEB_LOADER_TIMEOUT_SECONDS))
        self.assertEqual(no_gpu["ENABLE_OPENAI_API"], "false")
        self.assertNotIn("OPENAI_API_BASE_URLS", no_gpu)
        self.assertNotIn("ENABLE_FORWARD_USER_INFO_HEADERS", no_gpu)
        self.assertNotIn("TASK_MODEL_EXTERNAL", no_gpu)
        self.assertNotIn("DEFAULT_MODELS", no_gpu)
        self.assertNotIn("ENABLE_RETRIEVAL_QUERY_GENERATION", no_gpu)
        self.assertEqual(no_gpu["ENABLE_EVALUATION_ARENA_MODELS"], "false")
        self.assertNotIn("NO_PROXY", no_gpu)
        self.assertEqual(disconnected["ENABLE_OPENAI_API"], "false")
        self.assertNotIn("OPENAI_API_BASE_URLS", disconnected)
        self.assertNotIn("ENABLE_FORWARD_USER_INFO_HEADERS", disconnected)
        self.assertNotIn("DEFAULT_MODELS", disconnected)
        self.assertEqual(disconnected["ENABLE_EVALUATION_ARENA_MODELS"], "false")
        self.assertEqual(disconnected["ENABLE_WEB_SEARCH"], "false")
        self.assertNotIn("USER_AGENT", disconnected)
        self.assertNotIn("WEB_LOADER_TIMEOUT", disconnected)
        self.assertNotIn("SEARXNG_QUERY_URL", disconnected)
        proxied_no_gpu = owui_environment(inputs(SECOND, no_gpu=True))
        self.assertTrue(
            proxied_no_gpu["NO_PROXY"].endswith(",caddy,postgres,open-webui")
        )
        self.assertNotIn(ENGINE_SERVICE_NAME, proxied_no_gpu["NO_PROXY"])


    def test_the_domain_filter_is_a_json_array_of_bare_names(self) -> None:
        base = inputs()
        filtered = replace(
            base,
            site=replace(
                base.site,
                web=replace(base.site.web, domain_filter=["law.cornell.edu", "uscourts.gov"]),
            ),
        )
        env = owui_environment(filtered)
        self.assertEqual(env["WEB_SEARCH_DOMAIN_FILTER_LIST"], '["law.cornell.edu","uscourts.gov"]')
        self.assertEqual(owui_environment(base)["WEB_SEARCH_DOMAIN_FILTER_LIST"], "[]")

class SecretEnv(unittest.TestCase):
    def test_env_file_carries_only_the_values_without_a_file_path(self) -> None:
        values = owui_secret_environment(inputs())
        self.assertEqual(list(values), ["LDAP_APP_PASSWORD", "DATABASE_URL", "WEBUI_ADMIN_PASSWORD", "OPENAI_API_KEYS"])
        self.assertEqual(values["LDAP_APP_PASSWORD"], SECRETS["ldap_bind_password"])
        self.assertEqual(values["DATABASE_URL"], "postgresql://openwebui:p%40ss%2Fword@postgres:5432/openwebui")
        self.assertEqual(values["OPENAI_API_KEYS"], SECRETS["gideon_api_key"])
        self.assertNotIn(SECRETS["engine_api_key"], values.values())

    def test_directory_false_omits_the_bind_secret_and_name(self) -> None:
        values = owui_secret_environment(inputs(), directory=False)
        self.assertNotIn("LDAP_APP_PASSWORD", values)
        self.assertNotIn("ldap_bind_password", owui_secret_names(inputs(), directory=False))

    def test_no_gpu_env_file_omits_the_connection_key(self) -> None:
        values = owui_secret_environment(inputs(no_gpu=True, secrets={
            name: value
            for name, value in SECRETS.items()
            if name not in {"engine_api_key", "gideon_api_key"}
        }))
        self.assertNotIn("OPENAI_API_KEYS", values)
        self.assertNotIn(SECRETS["engine_api_key"], values.values())

    def test_proxy_credentials_ride_in_the_secret_env_only(self) -> None:
        values = owui_secret_environment(inputs(SECOND, secrets={**SECRETS, "proxy_auth": "user:p@ss"}))
        self.assertEqual(values["HTTPS_PROXY"], "http://user:p%40ss@proxy.exd.example.internal:3128")
        self.assertEqual(values["HTTP_PROXY"], values["HTTPS_PROXY"])
        self.assertEqual(owui_secret_environment(inputs(SECOND))["HTTP_PROXY"], "http://proxy.exd.example.internal:3128")

    def test_malformed_proxy_auth_refuses(self) -> None:
        with self.assertRaises(ValueError):
            owui_secret_environment(inputs(SECOND, secrets={**SECRETS, "proxy_auth": "no-separator"}))

    def test_missing_secret_names_it(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            owui_secret_environment(inputs(secrets={"ldap_bind_password": "x"}))
        self.assertIn("postgres_openwebui_password", str(ctx.exception))

    def test_artifact_is_a_raw_line_file_at_0600_owned_by_the_frontend(self) -> None:
        artifact = OwuiEnvArtifact()
        self.assertTrue(artifact.secret)
        self.assertEqual(artifact.mode, 0o600)
        self.assertEqual(artifact.owners, ("open-webui",))
        lines = artifact.emit(inputs()).splitlines()
        self.assertEqual(lines[0], f"LDAP_APP_PASSWORD={SECRETS['ldap_bind_password']}")
        self.assertTrue(all("=" in line and not line.startswith(" ") for line in lines))
        self.assertEqual(
            tuple(name for name in OWUI_SECRET_NAMES),
            (
                "ldap_bind_password",
                "postgres_openwebui_password",
                "gideon_admin_password",
                "gideon_api_key",
            ),
        )


class Manifest(unittest.TestCase):
    def test_groups_identities_and_both_model_records(self) -> None:
        document = yaml.safe_load(ApplyManifestArtifact().emit(inputs(SECOND)))
        groups = {group["name"]: group for group in document["groups"]}
        self.assertEqual(list(groups), ["GIDEON-Users", "GIDEON-Admins", "EXD-Trial", "EXD-Habeas", SERVICE_GROUP])
        for name in ("GIDEON-Users", "GIDEON-Admins", "EXD-Trial", "EXD-Habeas"):
            self.assertEqual(groups[name]["membership"], "ldap")
            self.assertNotIn("members", groups[name])
            self.assertFalse(groups[name]["permissions"]["features"]["api_keys"])
        for group in groups.values():
            self.assertFalse(group["permissions"]["chat"]["web_upload"])
        service = groups[SERVICE_GROUP]
        self.assertEqual(service["membership"], "manifest")
        self.assertEqual(service["members"], [EVAL_IDENTITY.username])
        self.assertTrue(service["permissions"]["features"]["api_keys"])
        without_key = {**service["permissions"], "features": {**service["permissions"]["features"], "api_keys": False}}
        self.assertEqual(without_key, groups["GIDEON-Users"]["permissions"])
        self.assertEqual(
            document["identities"],
            [
                {"username": BREAK_GLASS.username, "email": BREAK_GLASS.email, "role": "admin"},
                {"username": EVAL_IDENTITY.username, "email": EVAL_IDENTITY.email, "role": "user", "groups": [SERVICE_GROUP]},
            ],
        )
        self.assertEqual(
            document["functions"],
            [branch_gate_function(inputs(SECOND))],
        )
        function = document["functions"][0]
        self.assertEqual(
            function,
            {
                "id": BRANCH_GATE_ID,
                "user_id": SYNC_ROW_USER_ID,
                "name": BRANCH_GATE_NAME,
                "type": "filter",
                "content": inputs(SECOND).templates[BRANCH_GATE_TEMPLATE],
                "meta": {"description": BRANCH_GATE_DESCRIPTION},
                "valves": {},
                "is_active": True,
                "is_global": True,
                "updated_at": SYNC_ROW_UPDATED_AT,
                "created_at": SYNC_ROW_CREATED_AT,
            },
        )
        model = document["models"]
        self.assertEqual(len(model), 2)
        self.assertEqual(
            model[0],
            {
                "id": ENGINE_SERVICE_NAME,
                "user_id": SYNC_ROW_USER_ID,
                "base_model_id": None,
                "name": ENGINE_SERVICE_NAME,
                "params": {},
                "meta": {"hidden": True, "capabilities": dict(BASE_MODEL_CAPABILITIES)},
                "access_grants": [dict(public_read_grant(ENGINE_SERVICE_NAME))],
                "is_active": True,
                "updated_at": SYNC_ROW_UPDATED_AT,
                "created_at": SYNC_ROW_CREATED_AT,
            },
        )
        general = model[1]
        self.assertEqual(general, general_preset_record(inputs(SECOND)))
        self.assertEqual(general["id"], GENERAL_PRESET_ID)
        self.assertEqual(general["name"], "General")
        self.assertEqual(general["base_model_id"], ENGINE_SERVICE_NAME)
        self.assertEqual(
            general["params"],
            {
                "system": general_texts(inputs(SECOND)).system_prompt,
                "function_calling": GENERAL_FUNCTION_CALLING,
            },
        )
        self.assertEqual(
            general["meta"],
            {
                "description": general_texts(inputs(SECOND)).description,
                "capabilities": dict(GENERAL_CAPABILITIES),
                "suggestion_prompts": [],
                "filterIds": [],
            },
        )
        self.assertNotIn("filterIds", model[0]["meta"])
        self.assertEqual(
            general["access_grants"],
            [
                {
                    "id": MODEL_GRANT_ID,
                    "resource_type": MODEL_GRANT_RESOURCE_TYPE,
                    "resource_id": GENERAL_PRESET_ID,
                    "principal_type": "user",
                    "principal_id": "*",
                    "permission": "read",
                    "created_at": MODEL_GRANT_CREATED_AT,
                }
            ],
        )
        self.assertEqual(general["user_id"], SYNC_ROW_USER_ID)
        self.assertEqual(general["updated_at"], SYNC_ROW_UPDATED_AT)
        self.assertEqual(general["created_at"], SYNC_ROW_CREATED_AT)
        self.assertEqual(set(BASE_MODEL_CAPABILITIES), {
            "builtin_tools",
            "file_upload",
            "file_context",
            "vision",
            "memory",
            "code_interpreter",
            "image_generation",
            "web_search",
            "terminal",
        })
        self.assertTrue(all(value is False for value in BASE_MODEL_CAPABILITIES.values()))
        self.assertIs(BASE_MODEL_HIDDEN, True)
        self.assertEqual(set(GENERAL_CAPABILITIES), set(BASE_MODEL_CAPABILITIES))
        self.assertEqual(
            [name for name in BASE_MODEL_CAPABILITIES if BASE_MODEL_CAPABILITIES[name] != GENERAL_CAPABILITIES[name]],
            ["file_context", "web_search"],
        )
        self.assertFalse(GENERAL_CAPABILITIES["file_upload"])
        self.assertFalse(GENERAL_CAPABILITIES["builtin_tools"])

    def test_general_records_differ_between_offices_only_by_the_office_name(self) -> None:
        first = yaml.safe_load(ApplyManifestArtifact().emit(inputs(EXAMPLE)))["models"][1]
        second = yaml.safe_load(ApplyManifestArtifact().emit(inputs(SECOND)))["models"][1]
        self.assertEqual(first["meta"]["filterIds"], [])
        self.assertEqual(second["meta"]["filterIds"], [])
        first_name = inputs(EXAMPLE).site.office.name
        second_name = inputs(SECOND).site.office.name
        self.assertNotEqual(first, second)
        self.assertIn(first_name, first["params"]["system"])
        self.assertIn(first_name, first["meta"]["description"])
        # The second office's web.search is off; its record still differs only by the name.
        renamed = deepcopy(first)
        renamed["params"]["system"] = first["params"]["system"].replace(first_name, second_name)
        renamed["meta"]["description"] = first["meta"]["description"].replace(first_name, second_name)
        self.assertEqual(renamed, second)

    def test_general_template_has_exact_keys_and_only_its_release_placeholder(self) -> None:
        source = inputs().templates["open-webui/general.yaml"]
        document = yaml.safe_load(source)
        self.assertEqual(set(document), {"name", "description", "system_prompt"})
        for leaf, value in document.items():
            with self.subTest(leaf=leaf):
                self.assertIsInstance(value, str)
                self.assertNotIn("{{", value)
                self.assertNotIn("}}", value)
                Template(value).substitute(office_name="Fictitious Office")
        self.assertNotIn("$office_name", document["name"])
        self.assertIn("$office_name", document["description"])
        self.assertIn("$office_name", document["system_prompt"])

    def test_general_loader_refuses_invalid_template_shapes(self) -> None:
        source = yaml.safe_load(inputs().templates["open-webui/general.yaml"])
        cases = {
            "missing key": ({"name": source["name"], "description": source["description"]}, "system_prompt"),
            "extra key": ({**source, "extra": "nope"}, "extra"),
            "empty text": ({**source, "name": ""}, "name"),
            "non-string leaf": ({**source, "description": 1}, "description"),
            "stray placeholder": ({**source, "description": "$unknown"}, "description"),
            "unescaped dollar": ({**source, "system_prompt": "costs $5"}, "system_prompt"),
        }
        for label, (document, leaf) in cases.items():
            with self.subTest(case=label):
                rendered_inputs = inputs(
                    templates={
                        **inputs().templates,
                        "open-webui/general.yaml": yaml.safe_dump(document, sort_keys=False),
                    }
                )
                with self.assertRaises(ValueError) as ctx:
                    general_texts(rendered_inputs)
                self.assertIn("open-webui/general.yaml", str(ctx.exception))
                self.assertIn(leaf, str(ctx.exception))

    def test_general_loader_refuses_a_missing_template(self) -> None:
        rendered_inputs = inputs(templates={name: value for name, value in inputs().templates.items() if name != "open-webui/general.yaml"})
        with self.assertRaises(ValueError) as ctx:
            general_texts(rendered_inputs)
        self.assertIn("open-webui/general.yaml", str(ctx.exception))

    def test_no_gpu_manifest_has_no_model_records(self) -> None:
        document = yaml.safe_load(ApplyManifestArtifact().emit(inputs(no_gpu=True)))
        self.assertEqual(document["models"], [])
        self.assertEqual(
            document["functions"],
            [branch_gate_function(inputs(no_gpu=True))],
        )

    def test_branch_gate_is_held_for_all_fixture_sites(self) -> None:
        for site_path, no_gpu in ((EXAMPLE, False), (SECOND, False), (EXAMPLE, True)):
            with self.subTest(site_path=site_path, no_gpu=no_gpu):
                rendered_inputs = inputs(site_path, no_gpu=no_gpu)
                document = yaml.safe_load(ApplyManifestArtifact().emit(rendered_inputs))
                self.assertEqual(
                    document["functions"],
                    [branch_gate_function(rendered_inputs)],
                )

    def test_branch_gate_refuses_syntax_errors_with_template_and_line(self) -> None:
        rendered_inputs = inputs(
            templates={
                **inputs().templates,
                BRANCH_GATE_TEMPLATE: "class Filter(\n",
            }
        )
        with self.assertRaises(ValueError) as ctx:
            branch_gate_function(rendered_inputs)
        self.assertIn(BRANCH_GATE_TEMPLATE, str(ctx.exception))
        self.assertIn("line 1", str(ctx.exception))

    def test_branch_gate_refuses_a_missing_template(self) -> None:
        rendered_inputs = inputs(
            templates={name: value for name, value in inputs().templates.items() if name != BRANCH_GATE_TEMPLATE}
        )
        with self.assertRaises(ValueError) as ctx:
            branch_gate_function(rendered_inputs)
        self.assertIn(BRANCH_GATE_TEMPLATE, str(ctx.exception))

    def test_missing_generator_refuses_from_manifest(self) -> None:
        rendered_inputs = inputs()
        missing_generator = replace(
            rendered_inputs,
            profile=replace(rendered_inputs.profile, models=()),
        )
        with self.assertRaises(ValueError) as ctx:
            ApplyManifestArtifact().emit(missing_generator)
        self.assertIn("models.lock", str(ctx.exception))

    def test_dn_form_groups_are_named_by_their_cn(self) -> None:
        document = yaml.safe_load(ApplyManifestArtifact().emit(inputs(DN_GROUPS)))
        self.assertEqual([group["name"] for group in document["groups"]], ["GIDEON-Users", "GIDEON-Admins", "GIDEON-ReadOnly", SERVICE_GROUP])

    def test_manifest_has_no_owner_and_no_secret(self) -> None:
        artifact = ApplyManifestArtifact()
        self.assertEqual(artifact.owners, ())
        self.assertFalse(artifact.secret)
        self.assertNotIn(SECRETS["ldap_bind_password"], artifact.emit(inputs()))


class Units(unittest.TestCase):
    def test_service_runs_reconcile_now_from_the_checkout_after_docker(self) -> None:
        text = ReconcileServiceArtifact().emit(inputs(checkout="/srv/gideon"))
        self.assertIn("WorkingDirectory=/srv/gideon", text)
        self.assertIn("ExecStart=/usr/bin/python3 -m gideon users reconcile --now", text)
        self.assertIn("Requires=docker.service", text)
        self.assertIn("After=docker.service network-online.target", text)
        self.assertIn("Type=oneshot", text)
        self.assertIn("TimeoutStartSec=900", text)

    def test_timer_is_nightly_and_persistent(self) -> None:
        for site_path in (EXAMPLE, SECOND):
            with self.subTest(site=site_path.name):
                site_inputs = inputs(site_path)
                text = ReconcileTimerArtifact().emit(site_inputs)
                self.assertIn(
                    f"OnCalendar={RECONCILE_CALENDAR} {site_inputs.site.office.timezone}",
                    text,
                )
                self.assertIn("Persistent=true", text)
                self.assertIn("WantedBy=timers.target", text)

    def test_empty_checkout_refuses(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ReconcileServiceArtifact().emit(inputs(checkout=""))
        self.assertIn("checkout", str(ctx.exception))

    def test_backup_service_and_timer_are_daily_and_bounded_for_both_sites(self) -> None:
        for site_path in (EXAMPLE, SECOND):
            with self.subTest(site=site_path.name):
                service = BackupServiceArtifact().emit(inputs(site_path))
                timer = BackupTimerArtifact().emit(inputs(site_path))
                self.assertIn("Type=oneshot", service)
                self.assertIn("ExecStart=/usr/bin/python3 -m gideon backup run", service)
                self.assertIn("ExecStart=/usr/bin/python3 -m gideon backup push", service)
                self.assertIn("Requires=docker.service", service)
                self.assertIn("WorkingDirectory=/opt/gideon", service)
                self.assertIn("StandardOutput=journal", service)
                self.assertIn("StandardError=journal", service)
                self.assertRegex(service, r"TimeoutStartSec=[1-9][0-9]*")
                self.assertIn(
                    f"OnCalendar={BACKUP_CALENDAR} {inputs(site_path).site.office.timezone}",
                    timer,
                )
                self.assertIn("Persistent=true", timer)

    def test_drill_calendar_has_the_six_release_values(self) -> None:
        self.assertEqual(
            DRILL_CALENDAR,
            {
                "1w": "Sat *-*-* 04:00:00",
                "2w": "Sat *-*-1..7,15..21 04:00:00",
                "1m": "Sat *-*-1..7 04:00:00",
                "3m": "Sat *-01,04,07,10-1..7 04:00:00",
                "6m": "Sat *-01,07-1..7 04:00:00",
                "1y": "Sat *-01-1..7 04:00:00",
            },
        )

    def test_drill_service_and_timer_use_each_site_interval(self) -> None:
        for site_path in (EXAMPLE, SECOND):
            with self.subTest(site=site_path.name):
                service = DrillServiceArtifact().emit(inputs(site_path))
                timer = DrillTimerArtifact().emit(inputs(site_path))
                self.assertIn("ExecStart=/usr/bin/python3 -m gideon backup drill", service)
                self.assertIn("Requires=docker.service", service)
                self.assertIn("After=gideon-backup.service", service)
                self.assertIn("StandardOutput=journal", service)
                self.assertIn("StandardError=journal", service)
                self.assertRegex(service, r"TimeoutStartSec=[1-9][0-9]*")
                self.assertIn(
                    "OnCalendar="
                    f"{DRILL_CALENDAR[inputs(site_path).site.backup.drill_interval]} "
                    f"{inputs(site_path).site.office.timezone}",
                    timer,
                )
                self.assertIn("Persistent=true", timer)

    def test_verify_all_is_quarterly_and_runs_the_full_push_verification(self) -> None:
        self.assertEqual(VERIFY_ALL_CALENDAR, "Sat *-01,04,07,10-8..14 04:00:00")
        for site_path in (EXAMPLE, SECOND):
            with self.subTest(site=site_path.name):
                site_inputs = inputs(site_path)
                service = VerifyServiceArtifact().emit(site_inputs)
                timer = VerifyTimerArtifact().emit(site_inputs)
                self.assertIn(
                    "ExecStart=/usr/bin/python3 -m gideon backup push --verify-all",
                    service,
                )
                self.assertIn("WorkingDirectory=/opt/gideon", service)
                self.assertIn("After=gideon-backup.service", service)
                self.assertIn("StandardOutput=journal", service)
                self.assertIn("StandardError=journal", service)
                self.assertIn(
                    f"OnCalendar={VERIFY_ALL_CALENDAR} {site_inputs.site.office.timezone}",
                    timer,
                )
                self.assertIn("Persistent=true", timer)

    def test_every_rendered_timer_carries_the_site_timezone(self) -> None:
        timer_artifacts = [
            artifact for artifact in ARTIFACTS if artifact.relative_path.endswith(".timer")
        ]
        for site_path in (EXAMPLE, SECOND):
            with self.subTest(site=site_path.name):
                site_inputs = inputs(site_path)
                for artifact in timer_artifacts:
                    with self.subTest(timer=artifact.relative_path):
                        text = artifact.emit(site_inputs)
                        calendar_lines = [
                            line
                            for line in text.splitlines()
                            if line.startswith("OnCalendar=")
                        ]
                        self.assertEqual(len(calendar_lines), 1)
                        self.assertTrue(
                            calendar_lines[0].endswith(
                                " " + site_inputs.site.office.timezone
                            )
                        )


class ComposeShape(unittest.TestCase):
    def test_service_order_and_the_store_tier(self) -> None:
        fixture = yaml.safe_load(
            (ROOT / "tests/fixtures/render/example/compose.yaml").read_text()
        )
        self.assertEqual(
            service_names(inputs()),
            tuple(fixture["services"]),
        )
        self.assertEqual(STORE_SERVICES, ("postgres",))

    def test_compose_document_wires_the_frontend_to_its_dependencies(self) -> None:
        rendered = render_all(inputs())
        compose = yaml.safe_load(rendered.by_path["compose.yaml"].content)
        frontend = compose["services"]["open-webui"]
        self.assertEqual(frontend["depends_on"], {"postgres": {"condition": "service_healthy"}})
        self.assertEqual(frontend["env_file"], [{"path": "/etc/gideon/rendered/open-webui/env", "format": "raw"}])
        self.assertIn("/data/bulk/openwebui:/app/backend/data", frontend["volumes"])
        self.assertEqual(
            frontend["secrets"],
            ["webui_secret_key"],
        )
        self.assertEqual(frontend["healthcheck"]["test"][0], "CMD-SHELL")
        self.assertNotIn("ports", frontend)
        postgres = compose["services"]["postgres"]
        self.assertEqual(postgres["environment"]["POSTGRES_PASSWORD_FILE"], "/run/secrets/postgres_superuser_password")
        self.assertIn("/data/fast/postgres:/var/lib/postgresql", postgres["volumes"])
        self.assertEqual(
            postgres["command"],
            [
                "postgres",
                "-c",
                "archive_mode=on",
                "-c",
                "archive_command=pgbackrest --stanza=gideon archive-push %p",
                "-c",
                "archive_timeout=300",
            ],
        )
        self.assertIn(
            "/data/backup-staging/pgbackrest:/data/backup-staging/pgbackrest",
            postgres["volumes"],
        )
        self.assertIn(
            "/etc/gideon/rendered/postgres/pgbackrest.conf:/etc/pgbackrest.conf:ro",
            postgres["volumes"],
        )
        self.assertEqual(postgres["healthcheck"]["test"][:2], ["CMD", "pg_isready"])
        self.assertNotIn("ports", postgres)
        self.assertEqual(
            set(compose["secrets"]),
            {
                "tls_key",
                "postgres_superuser_password",
                "webui_secret_key",
                "grafana_admin_password",
                "ldap_bind_password",
                "postgres_gideon_ro_metrics_password",
                "postgres_gideon_audit_password",
                "engine_api_key",
                "gideon_api_key",
            },
        )
        for secret in SECRETS.values():
            self.assertNotIn(secret, rendered.by_path["compose.yaml"].content)

    def test_every_rendered_service_carries_the_site_timezone(self) -> None:
        for site_path in (EXAMPLE, SECOND):
            with self.subTest(site=site_path.name):
                site_inputs = inputs(site_path)
                rendered = render_all(site_inputs)
                compose = yaml.safe_load(rendered.by_path["compose.yaml"].content)
                for name, service in compose["services"].items():
                    with self.subTest(service=name):
                        self.assertEqual(
                            service["environment"]["TZ"],
                            site_inputs.site.office.timezone,
                        )

    def test_drill_document_is_isolated_and_does_not_archive(self) -> None:
        document = drill_compose_document(inputs(SECOND))
        self.assertEqual(document["name"], DRILL_PROJECT)
        services = cast(Mapping[str, object], document["services"])
        postgres = cast(Mapping[str, object], services["postgres"])
        frontend = cast(Mapping[str, object], services["open-webui"])
        postgres_volumes = cast(list[str], postgres["volumes"])
        frontend_volumes = cast(list[str], frontend["volumes"])
        frontend_environment = cast(Mapping[str, str], frontend["environment"])
        self.assertNotIn("command", postgres)
        self.assertIn(f"{DRILL_ROOT}/postgres:/var/lib/postgresql", postgres_volumes)
        self.assertIn("/data/backup-staging/pgbackrest:/data/backup-staging/pgbackrest:ro", postgres_volumes)
        self.assertIn("/etc/gideon/rendered/postgres/pgbackrest.conf:/etc/pgbackrest.conf:ro", postgres_volumes)
        self.assertEqual(frontend["ports"], [f"127.0.0.1:{DRILL_PORT}:8080"])
        self.assertIn(f"{DRILL_ROOT}/openwebui:/app/backend/data", frontend_volumes)
        self.assertIn("/etc/gideon/ca.pem:/etc/gideon/ca.pem:ro", frontend_volumes)
        self.assertEqual(frontend_environment["WEBUI_URL"], f"http://127.0.0.1:{DRILL_PORT}")
        self.assertEqual(frontend_environment["ENABLE_OPENAI_API"], "false")
        self.assertNotIn("DEFAULT_MODELS", frontend_environment)
        self.assertEqual(frontend_environment["ENABLE_EVALUATION_ARENA_MODELS"], "false")
        self.assertNotIn("OPENAI_API_BASE_URLS", frontend_environment)
        self.assertNotIn("TASK_MODEL_EXTERNAL", frontend_environment)
        self.assertEqual(frontend["depends_on"], {"postgres": {"condition": "service_healthy"}})
        self.assertNotIn("caddy", services)
        secrets = cast(Mapping[str, object], document["secrets"])
        self.assertEqual(set(secrets), {"postgres_superuser_password", "webui_secret_key"})

    def test_drill_services_use_their_production_memory_rows(self) -> None:
        rendered_inputs = inputs(SECOND)
        services = cast(
            Mapping[str, object], drill_compose_document(rendered_inputs)["services"]
        )
        for name in ("postgres", "open-webui"):
            with self.subTest(service=name):
                row = rendered_inputs.profile.memory_row(name)
                self.assertIsNotNone(row)
                assert row is not None
                block = cast(Mapping[str, object], services[name])
                self.assertEqual(block["mem_limit"], row.gb * GIGABYTE)
                self.assertEqual(list(block)[-1], "mem_limit")

    def test_drill_missing_postgres_memory_row_refuses_with_its_fix(self) -> None:
        rendered_inputs = inputs(SECOND)
        profile = replace(
            rendered_inputs.profile,
            memory=tuple(
                row
                for row in rendered_inputs.profile.memory
                if row.service != "postgres"
            ),
        )
        with self.assertRaises(ValueError) as caught:
            drill_compose_document(replace(rendered_inputs, profile=profile))
        message = str(caught.exception)
        self.assertIn("Cannot render Compose:", message)
        self.assertIn("service 'postgres'", message)
        self.assertIn("memory.postgres.gb", message)
        self.assertTrue(message.endswith("then re-run render."))


class CommandSecrets(unittest.TestCase):
    """The command layer's secret loading (render and apply share it)."""

    def files(self) -> dict[str, str]:
        files = {
            str(ROOT / "host.lock"): (ROOT / "host.lock").read_text(),
            str(ROOT / "images.lock"): (ROOT / "images.lock").read_text(),
            str(ROOT / "models.lock"): (ROOT / "models.lock").read_text(),
            "/etc/gideon/site.yaml": EXAMPLE.read_text(),
            str(ROOT / "gideon/api/__init__.py"): (ROOT / "gideon/api/__init__.py").read_text(),
            str(ROOT / "gideon/guardrail/__init__.py"): (ROOT / "gideon/guardrail/__init__.py").read_text(),
        }
        for name in TEMPLATE_PATHS:
            files[str(ROOT / "compose" / name)] = (ROOT / "compose" / name).read_text()
        for name, value in SECRETS.items():
            files[f"/etc/gideon/secrets/{name}"] = value + "\n"
        return files

    def render(self, files: dict[str, str]) -> tuple[int, str, str]:
        import argparse
        import contextlib
        import io as io_module
        import os
        import subprocess
        from collections.abc import Mapping

        from gideon.host.render.command import run_render
        from gideon.host.sysio import Command, PathLike

        class DirHost:
            def __init__(self) -> None:
                self.files = dict(files)
                self.dirs: set[str] = set()

            def run(self, argv: Command, *, check: bool = False, input: str | None = None, cwd: PathLike | None = None, env: Mapping[str, str] | None = None, timeout: float | None = None, passthrough: bool = False) -> subprocess.CompletedProcess[str]:
                if tuple(argv) == ("nvidia-smi", "-L"):
                    return subprocess.CompletedProcess(list(argv), 0, "GPU 0: X (UUID: GPU-a)\n", "")
                if tuple(argv) == ("getent", "group", "gideon"):
                    return subprocess.CompletedProcess(list(argv), 0, "gideon:x:4242:\n", "")
                return subprocess.CompletedProcess(list(argv), 127, "", "")

            def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
                key = os.fspath(path)
                if key not in self.files:
                    raise FileNotFoundError(key)
                return self.files[key]

            def write_text(self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644) -> None:
                self.files[os.fspath(path)] = text

            def exists(self, path: PathLike) -> bool:
                key = os.fspath(path)
                return key in self.files or key in self.dirs

            def listdir(self, path: PathLike) -> list[str]:
                prefix = os.fspath(path).rstrip("/") + "/"
                return sorted({key[len(prefix):].split("/")[0] for key in self.files if key.startswith(prefix)})

            def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
                self.files.pop(os.fspath(path), None)

            def stat(self, path: PathLike) -> os.stat_result:
                import stat as stat_module

                key = os.fspath(path)
                mode = stat_module.S_IFREG | 0o644 if key in self.files else stat_module.S_IFDIR | 0o755
                return os.stat_result((mode, 0, 0, 1, 0, 0, 0, 0, 0, 0))

            def chmod(self, path: PathLike, mode: int) -> None:
                return None

            def chown(self, path: PathLike, uid: int, gid: int) -> None:
                return None

            def mkdir(self, path: PathLike, *, mode: int = 0o755, parents: bool = False, exist_ok: bool = False) -> None:
                self.dirs.add(os.fspath(path))

            def geteuid(self) -> int:
                return 0

        out, err = io_module.StringIO(), io_module.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run_render(argparse.Namespace(diff=False), host=DirHost(), root=ROOT)
        return code, out.getvalue(), err.getvalue()

    def test_missing_generated_secret_names_apply(self) -> None:
        files = self.files()
        del files["/etc/gideon/secrets/gideon_admin_password"]
        code, _, err = self.render(files)
        self.assertEqual(code, 1)
        self.assertIn("gideon_admin_password", err)
        self.assertIn("gideon apply", err)

    def test_missing_supplied_secret_names_its_path(self) -> None:
        files = self.files()
        del files["/etc/gideon/secrets/ldap_bind_password"]
        code, _, err = self.render(files)
        self.assertEqual(code, 1)
        self.assertIn("Place the secret in /etc/gideon/secrets/ldap_bind_password", err)
        self.assertNotIn("gideon apply", err)

    def test_line_break_in_a_secret_refuses_naming_the_file(self) -> None:
        files = self.files()
        files["/etc/gideon/secrets/ldap_bind_password"] = "two\nlines\n"
        code, _, err = self.render(files)
        self.assertEqual(code, 1)
        self.assertIn("CR, LF, or NUL: /etc/gideon/secrets/ldap_bind_password", err)

    def test_secrets_never_reach_stdout_and_the_env_file_is_written(self) -> None:
        files = self.files()
        files["/etc/gideon/secrets/proxy_auth"] = "u:p\n"
        code, out, err = self.render(files)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("open-webui/env: new", out)
        for value in SECRETS.values():
            self.assertNotIn(value, out)
