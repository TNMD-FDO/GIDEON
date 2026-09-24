"""Prove the search sentinel does not enter the stack's retained logs.

This self-hosted contract builds a throwaway Postgres, Open WebUI, SearXNG,
and standard-library stub stack from the image mirror, drives five managed
turns with fresh sentinels, and counts matches in every container journal and
the frontend audit file.  It runs with the system Python and redirects only
the product secret directory into the temporary stack.
"""

import http.client
import json
import os
import secrets as token_secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import gideon.host.secrets as secret_files
from gideon.host import owui, owuiturn, stores
from gideon.host.images import load_image_lock, parse_registry, reference
from gideon.host.lock import load_host_lock
from gideon.host.models import HardwareProfile, load_models_lock, select_profile
from gideon.host.render import ARTIFACTS, RenderInputs
from gideon.host.render.compose import OWUI_COMMAND
from gideon.host.render.facts import HostFacts
from gideon.host.render.owui import (
    ALLOWED_ENDPOINTS,
    AUDIT_LOG_FILE,
    EVAL_IDENTITY,
    EVAL_PASSWORD_SECRET,
    GENERAL_PRESET_ID,
    owui_environment,
)
from gideon.host.render.searxng import (
    SEARXNG_NETWORK_LOGGER,
    SearxngLoggingArtifact,
    searxng_logging_document,
    searxng_query_url,
    searxng_secret_environment,
    searxng_settings_document,
)
from gideon.host.render.yamlout import dump
from gideon.host.site import load_site
from gideon.host.sysio import RealHost

MANIFEST = ROOT / "tests/fixtures/render/example/open-webui/manifest.yaml"
COMPOSE_TEMPLATE = ROOT / "tests/contract/sentinel/compose.yaml"
STUB_TEMPLATE = ROOT / "tests/contract/sentinel/stub.py"
EXAMPLE_SITE = ROOT / "config/site.example.yaml"
REGISTRY = os.environ.get("GIDEON_CONTRACT_REGISTRY", "127.0.0.1:5000")
PORT = int(os.environ.get("GIDEON_CONTRACT_PORT", "18081"))
BASE_URL = f"http://127.0.0.1:{PORT}"
HEALTHY_ATTEMPTS = 60
READY_ATTEMPTS = 90
FRONTEND_STANDIN_URL = "http://stub:8000/searxng/search"
CONTROL_LOG_LEVEL = "WARNING"

# These are the interpolation keys supplied by the rendered frontend posture
# in the sentinel Compose file.  The frontend command rides the same derivation
# below, so a CI-only override cannot be mistaken for a render key.  Stack-only
# values are written separately.
FRONTEND_ENV_KEYS: tuple[str, ...] = (
    "ALLOWED_ENDPOINTS",
    "DEFAULT_MODELS",
    "ENABLE_FORWARD_USER_INFO_HEADERS",
    "BYPASS_EMBEDDING_AND_RETRIEVAL",
    "GLOBAL_LOG_LEVEL",
    "AUDIT_LOG_LEVEL",
    "AUDIT_LOGS_FILE_PATH",
    "ENABLE_AUDIT_STDOUT",
    "AUDIT_EXCLUDED_PATHS",
    "ENABLE_AUDIT_GET_REQUESTS",
    "AUDIT_UVICORN_LOGGER_NAMES",
    "ENABLE_OTEL",
    "ENABLE_OTEL_TRACES",
    "ENABLE_OTEL_METRICS",
    "ENABLE_OTEL_LOGS",
    "ENABLE_QUERIES_CACHE",
    "LOGURU_DIAGNOSE",
    "CONTENT_SECURITY_POLICY",
    "ENABLE_SEARCH_QUERY_GENERATION",
    "ENABLE_RETRIEVAL_QUERY_GENERATION",
    "ENABLE_WEB_SEARCH",
    "WEB_SEARCH_ENGINE",
    "SEARXNG_QUERY_URL",
    "WEB_SEARCH_RESULT_COUNT",
    "WEB_LOADER_ENGINE",
    "BYPASS_WEB_SEARCH_WEB_LOADER",
    "WEB_SEARCH_TRUST_ENV",
    "ENABLE_WEB_LOADER_SSL_VERIFICATION",
    "BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL",
    "WEB_SEARCH_DOMAIN_FILTER_LIST",
    "TASK_MODEL_EXTERNAL",
)

# A source is (display name, Compose service, audit path).  The audit source
# deliberately names its owning frontend service as well as its container
# path, so the enumeration has one representation for every retained source.
LOG_SOURCES: tuple[tuple[str, str, str | None], ...] = (
    ("open-webui", "open-webui", None),
    ("searxng", "searxng", None),
    ("postgres", "postgres", None),
    ("stub", "stub", None),
    ("audit", "open-webui", AUDIT_LOG_FILE),
)


def image_references() -> dict[str, str]:
    """Resolve every stack image through the configured mirror registry."""

    lock = load_image_lock(ROOT / "images.lock").lock
    target = parse_registry(REGISTRY)
    assert lock is not None and target is not None
    return {pin.name: reference(target, pin) for pin in lock.images}


def render_inputs() -> RenderInputs:
    """Build the example render inputs used to derive the contract stack."""

    site = load_site(EXAMPLE_SITE).config
    lock = load_host_lock(ROOT / "host.lock").lock
    images = load_image_lock(ROOT / "images.lock").lock
    models = load_models_lock(ROOT / "models.lock").lock
    assert site is not None and lock is not None and images is not None and models is not None
    profile = select_profile(models, site.hardware_profile)
    assert isinstance(profile, HardwareProfile)
    template_paths = tuple(
        dict.fromkeys(path for artifact in ARTIFACTS for path in artifact.template_paths)
    )
    templates = {
        path: (ROOT / "compose" / path).read_text() for path in template_paths
    }
    return RenderInputs(
        site=site,
        lock=lock,
        images=images,
        facts=HostFacts(("GPU-a", "GPU-b"), service_gid=4242),
        profile=profile,
        templates=templates,
        release="fixture",
        secrets={
            "ldap_bind_password": "bind-$-'\"#password",
            "postgres_openwebui_password": "p@ss/word",
            "gideon_admin_password": "admin-password",
            "engine_api_key": "engine-api-key",
            "gideon_api_key": "gideon-api-key",
            "searxng_secret_key": "searxng-secret-key",
        },
        checkout="/opt/gideon",
        api_sources_digest="sha256:" + "0" * 64,
    )


def _manifest_base_model_id(manifest: Mapping[str, object]) -> str:
    models = manifest.get("models")
    assert isinstance(models, list) and models
    base = models[0]
    assert isinstance(base, Mapping)
    identifier = base.get("id")
    assert isinstance(identifier, str) and identifier
    return identifier


def _control_logging_text() -> str:
    document = dict(searxng_logging_document())
    raw_loggers = document.get("loggers")
    assert isinstance(raw_loggers, Mapping)
    loggers = {name: dict(value) for name, value in raw_loggers.items()}
    loggers[SEARXNG_NETWORK_LOGGER] = {"level": CONTROL_LOG_LEVEL}
    document["loggers"] = loggers
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


class ContractHost(RealHost):
    """The real host with ownership changes suppressed for temp files."""

    def chown(self, path: object, uid: int, gid: int) -> None:
        del path, uid, gid


class SentinelStack:
    """The temporary Compose project and its generated configuration files."""

    def __init__(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="gideon-ci-sentinel-"))
        self.secrets = self.directory / "secrets"
        self.secrets.mkdir(mode=0o700)
        self.searxng = self.directory / "searxng"
        self.searxng.mkdir(mode=0o700)
        self.compose_file = self.directory / "compose.yaml"
        self.logging_file = self.searxng / "logging.json"
        self.control_logging_file = self.searxng / "logging-control.json"
        self.inputs = render_inputs()
        self.manifest = dict(owui.load_manifest(ContractHost(), MANIFEST))
        self.passwords = {
            "postgres_superuser_password": token_secrets.token_urlsafe(24),
            **{
                spec.secret_name: token_secrets.token_urlsafe(24)
                for spec in stores.ROLE_SPECS
            },
            "webui_secret_key": token_secrets.token_urlsafe(24),
            "gideon_admin_password": token_secrets.token_urlsafe(24),
            "gideon_eval_password": token_secrets.token_urlsafe(24),
        }
        self._write_secrets()
        self._write_frontend_env_file()
        self._write_searxng_files()
        self._write_environment()
        shutil.copy2(COMPOSE_TEMPLATE, self.compose_file)
        shutil.copy2(STUB_TEMPLATE, self.directory / "stub.py")

    def _write_secrets(self) -> None:
        for name, value in self.passwords.items():
            path = self.secrets / name
            path.write_text(value + "\n")
            path.chmod(0o440)

    def _write_frontend_env_file(self) -> None:
        password = self.passwords["postgres_openwebui_password"]
        database_url = (
            f"postgresql://openwebui:{quote(password, safe='')}"
            "@postgres:5432/openwebui"
        )
        path = self.directory / "open-webui.env"
        path.write_text(
            f"DATABASE_URL={database_url}\n"
            f"WEBUI_ADMIN_PASSWORD={self.passwords['gideon_admin_password']}\n"
        )
        path.chmod(0o600)

    def _write_searxng_files(self) -> None:
        settings = dict(searxng_settings_document(self.inputs))
        settings["use_default_settings"] = {"engines": {"keep_only": []}}
        # enable_http: SearXNG's engine default refuses a plain-HTTP engine URL
        # (curl_cffi's InvalidSchema before any request leaves), and the stub
        # serves plain HTTP on the Compose network.
        settings["engines"] = [
            {
                "name": "stub-json",
                "engine": "json_engine",
                "shortcut": "sj",
                "enable_http": True,
                "search_url": "http://stub:8000/json?q={query}",
                "url_query": "url",
                "title_query": "title",
                "content_query": "content",
            },
            {
                "name": "bing",
                "engine": "bing",
                "shortcut": "bi",
                "enable_http": True,
                "base_url": "http://stub:8000",
            },
        ]
        (self.searxng / "settings.yml").write_text(dump(settings))
        env = searxng_secret_environment(self.inputs)
        (self.searxng / "env").write_text(
            "".join(f"{name}={value}\n" for name, value in env.items())
        )
        self.logging_file.write_text(SearxngLoggingArtifact().emit(self.inputs))
        self.control_logging_file.write_text(_control_logging_text())

    def _write_environment(self) -> None:
        frontend = dict(owui_environment(self.inputs))
        self.rendered = frontend
        images = image_references()
        values = {
            "POSTGRES_IMAGE": images["postgres"],
            "OPEN_WEBUI_IMAGE": images["open-webui"],
            "SEARXNG_IMAGE": images["searxng"],
            "CONTRACT_PORT": str(PORT),
            "ALLOWED_ENDPOINTS": ",".join(ALLOWED_ENDPOINTS),
            "STUB_MODE": "ok",
            "STUB_SENTINEL": "",
            "STUB_MODEL_ID": _manifest_base_model_id(self.manifest),
            "OPEN_WEBUI_COMMAND": shlex.join(OWUI_COMMAND),
        }
        for name in FRONTEND_ENV_KEYS:
            if name == "ALLOWED_ENDPOINTS":
                continue
            value = frontend.get(name)
            if value is None:
                raise ValueError(f"Rendered frontend environment is missing: {name}.")
            values[name] = value
        values["SEARXNG_QUERY_URL"] = searxng_query_url()
        (self.directory / ".env").write_text(
            "".join(f"{name}={value}\n" for name, value in values.items())
        )

    def compose(self, *args: str) -> subprocess.CompletedProcess[str]:
        argv = [
            "docker",
            "compose",
            "--project-directory",
            str(self.directory),
            "-f",
            str(self.compose_file),
            *args,
        ]
        return subprocess.run(argv, capture_output=True, text=True, check=False)

    def require_success(
        self, result: subprocess.CompletedProcess[str], action: str
    ) -> None:
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            raise AssertionError(f"{action} failed: {detail}")

    def wait_healthy(self, service: str) -> None:
        for attempt in range(HEALTHY_ATTEMPTS):
            result = self.compose("ps", "--all", "--format", "json")
            self.require_success(result, "docker compose ps")
            rows = []
            for line in result.stdout.splitlines():
                if line.strip():
                    value = json.loads(line)
                    if isinstance(value, Mapping):
                        rows.append(value)
            if any(
                row.get("Service") == service and row.get("Health") == "healthy"
                for row in rows
            ):
                return
            if attempt + 1 < HEALTHY_ATTEMPTS:
                time.sleep(5)
        logs = self.compose("logs", "--no-color", service)
        detail = logs.stdout[-2000:] if logs.returncode == 0 else logs.stderr[-2000:]
        raise AssertionError(f"{service} did not become healthy: {detail}")

    def logs(self, service: str) -> str:
        result = self.compose("logs", "--no-color", service)
        self.require_success(result, f"docker compose logs {service}")
        return result.stdout

    def audit(self, path: str) -> str:
        result = self.compose("exec", "-T", "open-webui", "cat", path)
        if result.returncode == 0:
            return result.stdout
        detail = f"{result.stdout}\n{result.stderr}".lower()
        if "no such file or directory" in detail:
            return ""
        self.require_success(result, "docker compose exec open-webui cat audit")
        return ""

    def replace_env(self, name: str, value: str) -> None:
        path = self.directory / ".env"
        lines = path.read_text().splitlines()
        replacement = f"{name}={value}"
        found = False
        updated: list[str] = []
        for line in lines:
            if line.startswith(f"{name}="):
                updated.append(replacement)
                found = True
            else:
                updated.append(line)
        if not found:
            raise AssertionError(f".env key is missing: {name}")
        path.write_text("\n".join(updated) + "\n")

    def down(self) -> None:
        try:
            result = self.compose("down", "-v", "--remove-orphans")
            self.require_success(result, "docker compose down")
        finally:
            shutil.rmtree(self.directory, ignore_errors=True)


def enumerate_logs(
    stack: SentinelStack, sentinel: str
) -> dict[str, tuple[int, tuple[str, ...]]]:
    """Count a sentinel in each container journal and the audit file."""

    observed: dict[str, tuple[int, tuple[str, ...]]] = {}
    for source, service, audit_path in LOG_SOURCES:
        text = stack.audit(audit_path) if audit_path is not None else stack.logs(service)
        lines = tuple(line for line in text.splitlines() if sentinel in line)
        observed[source] = (len(lines), lines)
    return observed


def is_frontend_residual(line: str) -> bool:
    """Recognize only aiohttp's URL-bearing SearXNG failure shape."""

    return (
        "ClientResponseError" in line
        and "url=" in line
        and FRONTEND_STANDIN_URL in line
    )


def mint_sentinel() -> str:
    """Mint the twelve-hex-character marker used by one run."""

    return f"gideon-sentinel-{token_secrets.token_hex(6)}"


def client_factory(
    *, api_key: str | None = None, token: str | None = None
) -> owui.Client:
    """Build an HTTP client for the temporary frontend's loopback port."""

    return owui.Client(BASE_URL, api_key=api_key, token=token)


class SearchSentinelContract(unittest.TestCase):
    """Drive the five ordered search-path log assertions."""

    stack: SentinelStack
    original_secrets_dir: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.stack = SentinelStack()
        cls.original_secrets_dir = secret_files.SECRETS_DIR
        setattr(secret_files, "SECRETS_DIR", cls.stack.secrets)  # noqa: B010
        try:
            postgres = cls.stack.compose("up", "-d", "postgres")
            cls.stack.require_success(postgres, "docker compose up postgres")
            cls.stack.wait_healthy("postgres")
            converged = stores.converge(ContractHost(), cls.stack.directory, root=ROOT)
            if not converged.ok:
                raise AssertionError(converged.problem)
            expected_roles = tuple(spec.name for spec in stores.ROLE_SPECS)
            if converged.created_roles != expected_roles:
                raise AssertionError("store convergence created an unexpected role set")
            expected_migrations = tuple(
                sorted(path.stem for path in (ROOT / "migrations").glob("*.sql"))
            )
            if converged.applied_migrations != expected_migrations:
                raise AssertionError("store convergence applied an unexpected migration set")

            # The stub and SearXNG start before the frontend: the frontend
            # discovers its base model from the stub's listing, and bootstrap's
            # live-listing step then holds General to its attachment list over
            # a real base model (the plan's Overview).
            search = cls.stack.compose("up", "-d", "searxng", "stub")
            cls.stack.require_success(search, "docker compose up searxng stub")
            cls.stack.wait_healthy("searxng")
            cls.stack.wait_healthy("stub")

            frontend = cls.stack.compose("up", "-d", "open-webui")
            cls.stack.require_success(frontend, "docker compose up open-webui")
            ready = owui.wait_ready(
                client_factory(), attempts=READY_ATTEMPTS, sleep=time.sleep
            )
            if not ready.ok:
                raise AssertionError(ready.problem)
            bootstrapped = owui.bootstrap(
                ContractHost(),
                client_factory,
                cls.stack.manifest,
                rendered_dir=cls.stack.directory,
            )
            if not bootstrapped.ok:
                raise AssertionError(bootstrapped.problem)
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        setattr(secret_files, "SECRETS_DIR", cls.original_secrets_dir)  # noqa: B010
        cls.stack.down()

    def test_0_frontend_page_carries_the_rendered_image_policy(self) -> None:
        self.assertTrue(self.stack.rendered["CONTENT_SECURITY_POLICY"])
        parsed = urlsplit(BASE_URL)
        assert parsed.hostname is not None and parsed.port is not None
        connection = http.client.HTTPConnection(
            parsed.hostname, parsed.port, timeout=15
        )
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            response.read()
            self.assertEqual(
                response.getheader("Content-Security-Policy"),
                self.stack.rendered["CONTENT_SECURITY_POLICY"],
            )
        finally:
            connection.close()

    def prepare_run(self, *, mode: str) -> str:
        sentinel = mint_sentinel()
        self.stack.replace_env("STUB_MODE", mode)
        self.stack.replace_env("STUB_SENTINEL", sentinel)
        stub = self.stack.compose("up", "-d", "--force-recreate", "stub")
        self.stack.require_success(stub, "docker compose up stub")
        self.stack.wait_healthy("stub")
        return sentinel

    def recreate_frontend(self) -> None:
        frontend = self.stack.compose(
            "up", "-d", "--force-recreate", "open-webui"
        )
        self.stack.require_success(frontend, "docker compose up open-webui")
        ready = owui.wait_ready(
            client_factory(), attempts=READY_ATTEMPTS, sleep=time.sleep
        )
        if not ready.ok:
            raise AssertionError(ready.problem)

    def recreate_searxng(self) -> None:
        searxng = self.stack.compose(
            "up", "-d", "--force-recreate", "searxng"
        )
        self.stack.require_success(searxng, "docker compose up searxng")
        self.stack.wait_healthy("searxng")

    def managed_turn(self, sentinel: str) -> owuiturn.StoredTurn:
        password = (self.stack.secrets / EVAL_PASSWORD_SECRET).read_text().rstrip(
            "\r\n"
        )
        token = owuiturn.signin(
            client_factory, EVAL_IDENTITY.email, password
        )
        client = client_factory(token=token)
        # The chat route serves a per-worker model cache that only the listing
        # refreshes, so the turn is preceded by one listing, which must show
        # General over the stub's base model.
        listing = client.request("GET", "/api/models")
        if listing.status != 200 or not isinstance(listing.body, Mapping):
            raise AssertionError(f"model listing answered HTTP {listing.status}")
        listed = listing.body.get("data")
        if not isinstance(listed, list) or not any(
            isinstance(row, Mapping) and row.get("id") == GENERAL_PRESET_ID for row in listed
        ):
            raise AssertionError("model listing does not show General over the stub")
        before = owuiturn.chat_ids(client)
        user_id = str(uuid.uuid4())
        assistant_id = str(uuid.uuid4())
        problem = owuiturn.managed_turn(
            client,
            model=GENERAL_PRESET_ID,
            prompt=f"Search the web for {sentinel} and summarize the result.",
            user_id=user_id,
            assistant_id=assistant_id,
            timestamp=int(time.time()),
            features={"web_search": True},
        )
        if problem is not None:
            raise AssertionError("managed turn refused")
        found = owuiturn.find_turn_chat(
            client,
            ids_before=before,
            user_id=user_id,
            assistant_id=assistant_id,
        )
        try:
            stored = found.stored
            if stored is None:
                raise AssertionError(found.refusal().problem)
            if stored.assistant is None:
                raise AssertionError("managed turn did not store an assistant message")
            return stored
        finally:
            if found.chat_id is not None:
                deleted = owuiturn.delete_chat(client, found.chat_id)
                if deleted is not None:
                    raise AssertionError("managed turn chat cleanup failed")

    def assert_zero_counts(
        self, observed: Mapping[str, tuple[int, tuple[str, ...]]]
    ) -> None:
        for source, (count, _lines) in observed.items():
            self.assertEqual(
                count,
                0,
                f"{source}: expected sentinel count 0, got {count}",
            )

    def assert_only_source(
        self,
        observed: Mapping[str, tuple[int, tuple[str, ...]]],
        source_name: str,
    ) -> None:
        for source, (count, _lines) in observed.items():
            if source == source_name:
                self.assertGreater(
                    count, 0, f"{source}: expected sentinel count greater than 0"
                )
            else:
                self.assertEqual(
                    count,
                    0,
                    f"{source}: expected sentinel count 0, got {count}",
                )

    def test_1_happy_path(self) -> None:
        sentinel = self.prepare_run(mode="ok")
        self.managed_turn(sentinel)
        observed = enumerate_logs(self.stack, sentinel)
        self.assert_zero_counts(observed)
        evidence = self.stack.logs("stub")
        for marker in (
            "POST /v1/chat/completions status=200",
            "GET /json status=200 sentinel=yes",
            "GET /search status=200",
            "GET /page/",
        ):
            evidence_count = evidence.count(marker)
            self.assertGreater(
                evidence_count,
                0,
                f"stub: expected request evidence count > 0, got {evidence_count}",
            )

    def test_2_searxng_engine_error(self) -> None:
        sentinel = self.prepare_run(mode="engine-error")
        self.managed_turn(sentinel)
        observed = enumerate_logs(self.stack, sentinel)
        self.assert_zero_counts(observed)
        # The path is proven to have run by the stub's own log (the Bing-shaped
        # request reached it and was answered 500, the JSON engine still
        # served results) and by SearXNG's hostname-only ErrorContext line.
        stub_evidence = self.stack.logs("stub")
        for marker in ("GET /search status=500", "GET /json status=200 sentinel=yes"):
            evidence_count = stub_evidence.count(marker)
            self.assertGreater(
                evidence_count,
                0,
                f"stub: expected request evidence count > 0, got {evidence_count}",
            )
        evidence = self.stack.logs("searxng")
        error_context_count = sum(
            "ErrorContext" in line and "bing" in line
            for line in evidence.splitlines()
        )
        self.assertTrue(
            error_context_count > 0,
            f"searxng: expected bing ErrorContext evidence count > 0, got {error_context_count}",
        )

    def test_3_frontend_searxng_error(self) -> None:
        sentinel = self.prepare_run(mode="engine-error")
        self.stack.replace_env("SEARXNG_QUERY_URL", FRONTEND_STANDIN_URL)
        self.recreate_frontend()
        self.managed_turn(sentinel)
        observed = enumerate_logs(self.stack, sentinel)
        # The residual is real on the pinned frontend, so the frontend's count
        # is required to be positive: a zero here means the frontend stopped
        # printing the URL, and the residual's record is what changes then.
        self.assert_only_source(observed, "open-webui")
        residual_mismatches = sum(
            not is_frontend_residual(line) for line in observed["open-webui"][1]
        )
        self.assertEqual(
            residual_mismatches,
            0,
            f"open-webui: residual-shape mismatch count {residual_mismatches}",
        )

    def test_4_searxng_control(self) -> None:
        sentinel = self.prepare_run(mode="engine-error")
        self.stack.replace_env("SEARXNG_QUERY_URL", searxng_query_url())
        self.stack.replace_env("GLOBAL_LOG_LEVEL", self.stack.rendered["GLOBAL_LOG_LEVEL"])
        self.recreate_frontend()
        shutil.copy2(self.stack.control_logging_file, self.stack.logging_file)
        self.recreate_searxng()
        self.managed_turn(sentinel)
        observed = enumerate_logs(self.stack, sentinel)
        self.assert_only_source(observed, "searxng")
        request_failure_mismatches = sum(
            "HTTP Request failed" not in line for line in observed["searxng"][1]
        )
        self.assertEqual(
            request_failure_mismatches,
            0,
            f"searxng: request-failure shape mismatch count {request_failure_mismatches}",
        )
        self.assertEqual(
            observed["audit"][0],
            0,
            f"audit: expected sentinel count 0, got {observed['audit'][0]}",
        )

    def test_5_frontend_control(self) -> None:
        sentinel = self.prepare_run(mode="engine-error")
        self.stack.replace_env("GLOBAL_LOG_LEVEL", "DEBUG")
        self.recreate_frontend()
        self.managed_turn(sentinel)
        observed = enumerate_logs(self.stack, sentinel)
        self.assertGreater(
            observed["open-webui"][0],
            0,
            f"open-webui: expected sentinel count greater than 0, got {observed['open-webui'][0]}",
        )
        self.assertEqual(
            observed["audit"][0],
            0,
            f"audit: expected sentinel count 0, got {observed['audit'][0]}",
        )


if __name__ == "__main__":
    unittest.main()
