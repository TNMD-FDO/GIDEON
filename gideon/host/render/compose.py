"""Pure Compose model rendering for the GIDEON service stack."""

from collections.abc import Mapping
from shlex import quote
from typing import Final

from gideon.host import weights
from gideon.host.images import ImagePin, RegistryTarget, parse_registry, reference
from gideon.host.models import GIGABYTE, HardwareProfile, ModelPin
from gideon.host.render import Artifact, RenderInputs
from gideon.host.render.api import (
    API_HEALTH_PATH,
    API_IMAGE_NAME,
    API_MOUNT_TARGET,
    API_SECRET_NAME,
    API_SERVICE_NAME,
    API_SOURCE_HEADER,
    API_WORKING_DIRECTORY,
    api_enabled,
)
from gideon.host.render.engine import (
    ENGINE_PORT,
    ENGINE_SECRET_NAME,
    ENGINE_SERVICE_NAME,
    engine_base_url,
)
from gideon.host.render.grafana import DASHBOARDS_MOUNT, GRAFANA_ADMIN_USER
from gideon.host.render.owui import (
    EVAL_IDENTITY,
    PERMISSIONS_TEMPLATE,
    owui_environment,
)
from gideon.host.render.pgbackrest import (
    PG_BACKREST_CONF_PATH,
    PGDATA,
    REPOSITORY_PATH,
    STANZA,
)
from gideon.host.render.searxng import (
    SEARXNG_ENV_FILE,
    SEARXNG_LOGGING_PATH,
    SEARXNG_PORT,
    SEARXNG_SERVICE_NAME,
    SEARXNG_SETTINGS_PATH,
    search_enabled,
)
from gideon.host.render.yamlout import dump

# The systemd collector's units: the registry and the runner exist on the
# build box alone (slice-0 ticket 21), so every other host collects GIDEON's.
BUILD_BOX_UNIT_PATTERN: Final = r"^(gideon-registry|actions\.runner\..+|gideon-.*)\.service$"
UNIT_PATTERN: Final = r"^gideon-.*\.service$"

# The tier apply starts and converges (roles, databases, migrations) before
# any other service is recreated; grows with the stores slice.
STORE_SERVICES: tuple[str, ...] = ("postgres",)

# Service fragments the drill document (render/drill.py) shares with the
# production project, so the two never drift apart.
POSTGRES_HEALTHCHECK: Mapping[str, object] = {
    "test": ["CMD", "pg_isready", "-U", "postgres", "-h", "127.0.0.1"],
    "interval": "10s",
    "timeout": "5s",
    "retries": 12,
    "start_period": "30s",
}
# The published OCI config carries no HEALTHCHECK (OCI drops Docker's), so
# the Dockerfile's check is declared here.
OWUI_HEALTHCHECK: Mapping[str, object] = {
    "test": [
        "CMD-SHELL",
        'curl --silent --fail http://localhost:8080/health | jq -ne "input.status == true"',
    ],
    "interval": "30s",
    "timeout": "10s",
    "retries": 5,
    "start_period": "120s",
}
# The frontend's command (slice-1 ticket 69): the image declares no entrypoint
# and its command is ``bash start.sh``, whose script hands any container
# argument to uvicorn *in place of* its own defaults, so the two defaults are
# restated before ``--no-access-log``.  The flag empties the access logger's
# handlers and stops its propagation, and it holds only because the rendered
# ``AUDIT_UVICORN_LOGGER_NAMES`` (render/owui.py) keeps the frontend's startup
# from re-attaching a handler there — each connection asks that logger for
# handlers once, when it opens.  The pinned image's and uvicorn's facts are
# docs/research/ingress-log-filter-and-frontend-access-line.md §B4, §C1–C2,
# re-read at a frontend bump (ticket 41's runbook step); the drill's frontend
# shares it.  exempt: no figure — a decision.
OWUI_COMMAND: Final[tuple[str, ...]] = (
    "bash",
    "start.sh",
    "--workers",
    "1",
    "--ws-per-message-deflate",
    "true",
    "--no-access-log",
)
OWUI_ENV_FILE: tuple[Mapping[str, str], ...] = (
    {"path": "/etc/gideon/rendered/open-webui/env", "format": "raw"},
)
# SearXNG (§15, [06] item 9) runs as shipped — the pinned image's entrypoint,
# default user, and command untouched — on the Compose network alone, and is
# present iff web.search is on.  The image declares no HEALTHCHECK and carries
# wget but no curl (docs/research/searxng-service-and-owui-search.md §3); the
# bounds are starting values (ADR-0017), the start period the seconds granian
# takes to serve /healthz on the box.
SEARXNG_HEALTHCHECK: Final[Mapping[str, object]] = {
    "test": [
        "CMD",
        "wget",
        "-q",
        "-O",
        "/dev/null",
        f"http://127.0.0.1:{SEARXNG_PORT}/healthz",
    ],
    "interval": "30s",
    "timeout": "5s",
    "retries": 3,
    "start_period": "30s",
}
# The API image carries Python but no curl, so its healthcheck uses the
# standard-library client; these are SearXNG's starting bounds. exempt: the
# service is expected to become ready in a second or two.
API_HEALTHCHECK: Final[Mapping[str, object]] = {
    "test": [
        "CMD",
        "python",
        "-c",
        "import urllib.request; urllib.request.urlopen("
        f"'http://127.0.0.1:{ENGINE_PORT}{API_HEALTH_PATH}', timeout=4).read()",
    ],
    "interval": "30s",
    "timeout": "5s",
    "retries": 3,
    "start_period": "30s",
}

# vLLM v0.27.1's image supplies ``[vllm, serve]`` as its entrypoint and no
# command.  The wrapper replaces that entrypoint so the Compose secret is read
# before the same server is exec'd (docs/research/vllm-engine-service.md §1, §3).
ENGINE_READY_SECONDS: Final = 900
ENGINE_SERVER: Final = "vllm serve"
ENGINE_USAGE_SWITCHES: Final[Mapping[str, str]] = {
    "VLLM_NO_USAGE_STATS": "1",
    "DO_NOT_TRACK": "1",
}
# The engine's two probes — the container healthcheck's path below and
# Prometheus's default metrics path — are the lines vLLM's access log excludes
# (slice-1 ticket 51): render provisions both probes, so render silences what
# it causes, and a turn's own request line keeps its shape.  The option takes
# one comma-separated string on v0.27.1, never two arguments
# (docs/research/vllm-engine-service.md §11).  exempt: no figure — a decision.
ENGINE_HEALTH_PATH: Final = "/health"
ENGINE_ACCESS_LOG_EXCLUDED_PATHS: Final[tuple[str, ...]] = (ENGINE_HEALTH_PATH, "/metrics")
ENGINE_HEALTHCHECK: Mapping[str, object] = {
    "test": [
        "CMD-SHELL",
        f"curl --silent --fail http://127.0.0.1:{ENGINE_PORT}{ENGINE_HEALTH_PATH}",
    ],
    "interval": "30s",
    "timeout": "10s",
    "retries": 3,
    "start_period": f"{ENGINE_READY_SECONDS}s",
}


def engine_wrapper(secret_path: str, server: str) -> list[str]:
    """Build the fail-closed entrypoint that supplies vLLM's API key."""

    secret = quote(secret_path)
    line = (
        "set -eu; "
        f'if [ ! -s {secret} ]; then echo "engine API key file is missing or empty: '
        f'{secret_path}" >&2; exit 1; fi; '
        f"VLLM_API_KEY=$(cat {secret}); export VLLM_API_KEY; "
        f'exec {server} "$@"'
    )
    return ["sh", "-c", line, ENGINE_SERVICE_NAME]


def engine_command(pin: ModelPin) -> list[str]:
    """Build the vLLM command from the selected model's locked baseline.

    Render's own server flags come first — the bind address, the port, and the
    access-log exclusions — then the profile's flags verbatim in lock order.
    """

    command = [
        pin.repo,
        "--revision",
        pin.revision,
        "--served-model-name",
        pin.serve.served_name,
        "--host",
        "0.0.0.0",
        "--port",
        str(ENGINE_PORT),
        "--disable-access-log-for-endpoints",
        ",".join(ENGINE_ACCESS_LOG_EXCLUDED_PATHS),
    ]
    for name, value in pin.serve.flags.items():
        command.append(f"--{name}")
        if value is not True:
            command.append(str(value))
    return command


def engine_service(
    inputs: RenderInputs, target: RegistryTarget | None = None
) -> Mapping[str, object]:
    """Build the GPU-only generator service from release and profile inputs.

    ``target`` is the parsed registry when the caller already holds it; the
    document builder passes its own so the key is parsed once.
    """

    model = inputs.profile.model("generator")
    if model is None:
        raise ValueError(
            f"Cannot render Compose: profile {inputs.profile.name} has no generator model. "
            "Add the generator model to models.lock, then re-run render."
        )
    if model.gpu < 0 or model.gpu >= len(inputs.facts.gpu_uuids):
        raise ValueError(
            f"Cannot render Compose: profile {inputs.profile.name} assigns generator GPU "
            f"index {model.gpu}, but the host has {len(inputs.facts.gpu_uuids)} GPU UUID(s). "
            "Run nvidia-smi -L and re-run preflight's hardware-profile check."
        )
    hf_home = model.serve.env.get("HF_HOME")
    if not hf_home:
        raise ValueError(
            f"Cannot render Compose: profile {inputs.profile.name}'s generator serve.env "
            "has no HF_HOME. Add HF_HOME to models.lock's serve.env, then re-run render."
        )
    if target is None:
        target = parse_registry(inputs.site.registry)
        if target is None:
            raise ValueError(
                "Cannot render Compose: the registry key is not usable. "
                "Correct registry in /etc/gideon/site.yaml, then re-run render."
            )

    environment: dict[str, str] = {}
    for name, value in model.serve.env.items():
        environment[name] = value
    for name, value in ENGINE_USAGE_SWITCHES.items():
        environment.pop(name, None)
        environment[name] = value
    environment.pop("TZ", None)
    environment["TZ"] = inputs.site.office.timezone
    return {
        "image": reference(target, image_pin(inputs, "vllm-openai")),
        "restart": "unless-stopped",
        "environment": environment,
        "entrypoint": engine_wrapper(
            f"/run/secrets/{ENGINE_SECRET_NAME}", ENGINE_SERVER
        ),
        "command": engine_command(model),
        "devices": [f"nvidia.com/gpu={inputs.facts.gpu_uuids[model.gpu]}"],
        "volumes": [f"{weights.MODELS_ROOT}:{hf_home}:ro"],
        "secrets": [ENGINE_SECRET_NAME],
        "healthcheck": dict(ENGINE_HEALTHCHECK),
        "networks": ["gideon"],
    }


def searxng_service(
    inputs: RenderInputs, target: RegistryTarget
) -> Mapping[str, object]:
    """Build SearXNG's run-as-shipped, Compose-network-only service.

    No ``ports`` (nothing outside the network reaches it), no ``user`` (as
    upstream's own Compose file runs it), no ``depends_on`` either way (a
    search is a per-turn call, and the frontend must start without it).
    The settings file is a read-only bind mount at the entrypoint's config
    path, which leaves an existing file untouched (the note's §2); the
    signing key and any proxy ride the env file, never the settings.  The
    access log is granian's default off, pinned by name so no request line
    carrying a query URL is ever written ([22] item 15; the note's §5e).  The
    logging file separately sets SearXNG's network logger to ERROR: access-log
    off controls request lines, while this file suppresses application warning
    lines that carry a failed request URL (research note §B2, §B3, and §B7.4).
    """

    return {
        "image": reference(target, image_pin(inputs, SEARXNG_SERVICE_NAME)),
        "restart": "unless-stopped",
        "environment": {
            "GRANIAN_LOG_ACCESS_ENABLED": "false",
            "GRANIAN_LOG_CONFIG": SEARXNG_LOGGING_PATH,
            "TZ": inputs.site.office.timezone,
        },
        "env_file": [dict(entry) for entry in SEARXNG_ENV_FILE],
        "volumes": [
            f"/etc/gideon/rendered/searxng/settings.yml:{SEARXNG_SETTINGS_PATH}:ro",
            f"/etc/gideon/rendered/searxng/logging.json:{SEARXNG_LOGGING_PATH}:ro",
        ],
        "healthcheck": dict(SEARXNG_HEALTHCHECK),
        "networks": ["gideon"],
    }


def api_service(inputs: RenderInputs, target: RegistryTarget) -> Mapping[str, object]:
    """Build the API service from the applying checkout's mounted package.

    It has no ports: callers reach it on the Compose network. The read-only
    mount is the tree that applied this render, and the label is what causes
    the service to be recreated when its declared code moves.
    """

    if not inputs.checkout:
        raise ValueError(
            "Render input checkout is empty; the gideon-api service needs the "
            "release checkout's absolute path. Re-run render with a checkout."
        )
    if not inputs.api_sources_digest:
        raise ValueError(
            "Render input api_sources_digest is empty; the gideon-api service's "
            "label needs the digest of its declared sources. Re-run render from a "
            "release checkout, whose loader gathers it."
        )
    return {
        "image": reference(target, image_pin(inputs, API_IMAGE_NAME)),
        "restart": "unless-stopped",
        "environment": {
            "GIDEON_ENGINE_URL": engine_base_url(),
            "GIDEON_ENGINE_API_KEY_FILE": f"/run/secrets/{ENGINE_SECRET_NAME}",
            "GIDEON_API_KEY_FILE": f"/run/secrets/{API_SECRET_NAME}",
            "GIDEON_API_PORT": str(ENGINE_PORT),
            "GIDEON_SOURCE_HEADER": API_SOURCE_HEADER,
            "GIDEON_EVAL_IDENTITY": EVAL_IDENTITY.email,
            "TZ": inputs.site.office.timezone,
        },
        "command": ["python", "-m", "gideon.api"],
        "working_dir": API_WORKING_DIRECTORY,
        "volumes": [
            f"{inputs.checkout}/gideon:{API_MOUNT_TARGET}:ro",
        ],
        "group_add": [str(inputs.facts.service_gid)],
        "secrets": [
            ENGINE_SECRET_NAME,
            API_SECRET_NAME,
            "postgres_gideon_audit_password",
        ],
        "labels": {"org.gideon.api-sources-digest": inputs.api_sources_digest},
        "healthcheck": dict(API_HEALTHCHECK),
        "networks": ["gideon"],
    }


def grafana_environment(inputs: RenderInputs) -> Mapping[str, str]:
    """Return Grafana's non-secret environment for the rendered Compose file."""

    smtp = inputs.site.alerts.smtp
    environment: dict[str, str] = {
        "GF_SERVER_ROOT_URL": f"https://{inputs.site.hostname}/grafana/",
        "GF_SERVER_SERVE_FROM_SUB_PATH": "true",
        "GF_AUTH_LDAP_ENABLED": "true",
        "GF_AUTH_LDAP_CONFIG_FILE": "/etc/grafana/ldap.toml",
        "GF_AUTH_LDAP_ALLOW_SIGN_UP": "true",
        "GF_SECURITY_ADMIN_USER": GRAFANA_ADMIN_USER,
        "GF_SECURITY_ADMIN_PASSWORD__FILE": "/run/secrets/grafana_admin_password",
        "GF_AUTH_ANONYMOUS_ENABLED": "false",
        "GF_USERS_ALLOW_SIGN_UP": "false",
        "GF_SECURITY_COOKIE_SECURE": "true",
        "GF_DATE_FORMATS_DEFAULT_TIMEZONE": inputs.site.office.timezone,
        "GF_SMTP_ENABLED": "true",
        "GF_SMTP_HOST": f"{smtp.host}:{smtp.port}",
        "GF_SMTP_FROM_ADDRESS": smtp.from_,
        # No angle brackets in a display name: mail filters read them as a
        # spoofed address and flag every page as suspicious.
        "GF_SMTP_FROM_NAME": f"GIDEON ({inputs.site.office.short_name})",
        "GF_SMTP_STARTTLS_POLICY": (
            "MandatoryStartTLS" if smtp.user else "OpportunisticStartTLS"
        ),
        "GF_INSTANCE_NAME": inputs.site.office.short_name,
        "GF_METRICS_ENABLED": "true",
        "GF_ANALYTICS_REPORTING_ENABLED": "false",
        "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
        "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES": "false",
        "TZ": inputs.site.office.timezone,
    }
    if smtp.user:
        environment["GF_SMTP_USER"] = smtp.user
        environment["GF_SMTP_PASSWORD__FILE"] = "/run/secrets/smtp_password"
    return environment


class ComposeArtifact(Artifact):
    """Render the ordered Compose project containing the GIDEON services."""

    name = "compose"
    relative_path = "compose.yaml"
    template_paths = (PERMISSIONS_TEMPLATE,)

    def emit(self, inputs: RenderInputs) -> str:
        return dump(_compose_document(inputs))


def service_names(inputs: RenderInputs) -> tuple[str, ...]:
    """The Compose services this release renders, in file order."""

    return tuple(service_blocks(inputs))


def service_blocks(inputs: RenderInputs) -> Mapping[str, object]:
    """The rendered document's service blocks by name, in file order."""

    document = _compose_document(inputs)
    services = document["services"]
    assert isinstance(services, Mapping)
    return services


def compose_top_level(inputs: RenderInputs) -> Mapping[str, object]:
    """The rendered document without its service blocks: the sections every service shares."""

    document = dict(_compose_document(inputs))
    del document["services"]
    return document


def service_images(inputs: RenderInputs) -> tuple[str, ...]:
    """The image references the rendered services name, in file order.

    What the stack pulls: apply probes and verifies these, not every lock pin —
    a no-GPU host renders no DCGM exporter, so its pin stays in the lock and the
    mirror but is never pulled (the acceptance VM's first install found this).
    """

    services = service_blocks(inputs)
    images: list[str] = []
    for service in services.values():
        assert isinstance(service, Mapping)
        image = service["image"]
        assert isinstance(image, str)
        images.append(image)
    return tuple(images)


def image_pin(inputs: RenderInputs, name: str) -> ImagePin:
    pin = next((candidate for candidate in inputs.images.images if candidate.name == name), None)
    if pin is None:
        raise ValueError(
            f"Cannot render Compose: images.lock has no {name} pin. "
            f"Add the {name} image to images.lock, then re-run render."
        )
    return pin


def memory_limit_bytes(profile: HardwareProfile, service: str) -> int:
    """Return a service's decimal-gigabyte memory limit in bytes."""

    row = profile.memory_row(service)
    if row is None:
        raise ValueError(
            f"Cannot render Compose: profile {profile.name} has no memory row for "
            f"service '{service}'. Add memory.{service}.gb to models.lock, then "
            "re-run render."
        )
    return row.gb * GIGABYTE


def _compose_document(inputs: RenderInputs) -> Mapping[str, object]:
    target = parse_registry(inputs.site.registry)
    if target is None:
        raise ValueError(
            "Cannot render Compose: the registry key is not usable. "
            "Correct registry in /etc/gideon/site.yaml, then re-run render."
        )

    caddy_pin = image_pin(inputs, "caddy")
    prometheus_pin = image_pin(inputs, "prometheus")
    node_exporter_pin = image_pin(inputs, "node-exporter")
    grafana_pin = image_pin(inputs, "grafana")
    postgres_pin = image_pin(inputs, "postgres")
    open_webui_pin = image_pin(inputs, "open-webui")
    secrets: dict[str, object] = {
        "tls_key": {"file": "/etc/gideon/secrets/tls_key"},
        "postgres_superuser_password": {
            "file": "/etc/gideon/secrets/postgres_superuser_password"
        },
        "webui_secret_key": {"file": "/etc/gideon/secrets/webui_secret_key"},
        "grafana_admin_password": {
            "file": "/etc/gideon/secrets/grafana_admin_password"
        },
        "ldap_bind_password": {"file": "/etc/gideon/secrets/ldap_bind_password"},
        "postgres_gideon_ro_metrics_password": {
            "file": "/etc/gideon/secrets/postgres_gideon_ro_metrics_password"
        },
        "postgres_gideon_audit_password": {
            "file": "/etc/gideon/secrets/postgres_gideon_audit_password"
        },
    }
    if inputs.site.alerts.smtp.user:
        secrets["smtp_password"] = {"file": "/etc/gideon/secrets/smtp_password"}

    services: dict[str, object] = {
        "caddy": {
            "image": reference(target, caddy_pin),
            "restart": "unless-stopped",
            "environment": {
                "TZ": inputs.site.office.timezone,
            },
            "ports": ["0.0.0.0:443:443"],
            "volumes": [
                "/etc/gideon/rendered/caddy/Caddyfile:/etc/caddy/Caddyfile:ro",
                "/etc/gideon/tls:/etc/gideon/tls:ro",
                "caddy_data:/data",
                "caddy_config:/config",
            ],
            "secrets": ["tls_key"],
            "networks": ["gideon"],
        },
        "prometheus": {
            "image": reference(target, prometheus_pin),
            "restart": "unless-stopped",
            "environment": {"TZ": inputs.site.office.timezone},
            "command": [
                "--config.file=/etc/prometheus/prometheus.yml",
                "--storage.tsdb.path=/data/observability/prometheus",
                "--storage.tsdb.retention.time=1y",
                "--web.listen-address=0.0.0.0:9090",
            ],
            "volumes": [
                "/etc/gideon/rendered/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro",
                "/data/observability/prometheus:/data/observability/prometheus",
            ],
            "ports": ["127.0.0.1:9090:9090"],
            "networks": ["gideon"],
        },
        "node-exporter": {
            "image": reference(target, node_exporter_pin),
            "restart": "unless-stopped",
            "environment": {"TZ": inputs.site.office.timezone},
            "pid": "host",
            "command": [
                "--path.rootfs=/host",
                "--collector.systemd",
                "--collector.systemd.unit-include="
                + (BUILD_BOX_UNIT_PATTERN if inputs.build_box else UNIT_PATTERN),
            ],
            "volumes": [
                "/:/host:ro,rslave",
                "/run/dbus/system_bus_socket:/var/run/dbus/system_bus_socket:ro",
            ],
            # Ubuntu's D-Bus daemon mediates callers by AppArmor label and
            # Docker's default profile carries no D-Bus rules, so the
            # systemd collector is refused under it. Nothing here is
            # published, and every mount is read-only.
            "security_opt": ["apparmor=unconfined"],
            "networks": ["gideon"],
        },
        "grafana": {
            "image": reference(target, grafana_pin),
            "restart": "unless-stopped",
            "environment": grafana_environment(inputs),
            "group_add": [str(inputs.facts.service_gid)],
            "depends_on": {
                "postgres": {"condition": "service_healthy"},
            },
            "volumes": [
                "/data/observability/grafana:/var/lib/grafana",
                "/etc/gideon/rendered/grafana/provisioning:/etc/grafana/provisioning:ro",
                f"/etc/gideon/rendered/grafana/dashboards:{DASHBOARDS_MOUNT}:ro",
                "/etc/gideon/rendered/grafana/ldap.toml:/etc/grafana/ldap.toml:ro",
                "/etc/gideon/ca.pem:/etc/gideon/ca.pem:ro",
                "/etc/gideon/ca.pem:/etc/ssl/certs/gideon-ca.pem:ro",
            ],
            "secrets": [
                "grafana_admin_password",
                "ldap_bind_password",
                "postgres_gideon_ro_metrics_password",
                *(["smtp_password"] if inputs.site.alerts.smtp.user else []),
            ],
            "networks": ["gideon"],
        },
        "postgres": {
            "image": reference(target, postgres_pin),
            "restart": "unless-stopped",
            "environment": {
                "POSTGRES_PASSWORD_FILE": "/run/secrets/postgres_superuser_password",
                "PGDATA": PGDATA,
                "TZ": inputs.site.office.timezone,
            },
            "command": [
                "postgres",
                "-c",
                "archive_mode=on",
                "-c",
                f"archive_command=pgbackrest --stanza={STANZA} archive-push %p",
                "-c",
                "archive_timeout=300",
            ],
            "volumes": [
                "/data/fast/postgres:/var/lib/postgresql",
                f"{REPOSITORY_PATH}:{REPOSITORY_PATH}",
                f"/etc/gideon/rendered/postgres/pgbackrest.conf:{PG_BACKREST_CONF_PATH}:ro",
            ],
            "secrets": ["postgres_superuser_password"],
            "healthcheck": dict(POSTGRES_HEALTHCHECK),
            "networks": ["gideon"],
        },
        "open-webui": {
            "image": reference(target, open_webui_pin),
            "restart": "unless-stopped",
            "depends_on": {
                "postgres": {"condition": "service_healthy"},
            },
            "env_file": [dict(entry) for entry in OWUI_ENV_FILE],
            "environment": owui_environment(inputs),
            "command": list(OWUI_COMMAND),
            "volumes": [
                "/data/bulk/openwebui:/app/backend/data",
                "/etc/gideon/ca.pem:/etc/gideon/ca.pem:ro",
            ],
            "secrets": ["webui_secret_key"],
            "healthcheck": dict(OWUI_HEALTHCHECK),
            "networks": ["gideon"],
        },
        "dcgm-exporter": {
            "image": reference(target, image_pin(inputs, "dcgm-exporter")),
            "restart": "unless-stopped",
            "environment": {"TZ": inputs.site.office.timezone},
            "devices": ["nvidia.com/gpu=all"],
            "cap_add": ["SYS_ADMIN"],
            "networks": ["gideon"],
        },
        "postgres-exporter": {
            "image": reference(target, image_pin(inputs, "postgres-exporter")),
            "restart": "unless-stopped",
            "environment": {
                "DATA_SOURCE_URI": "postgres:5432/gideon?sslmode=disable",
                "DATA_SOURCE_USER": "gideon_ro_metrics",
                "DATA_SOURCE_PASS_FILE": "/run/secrets/postgres_gideon_ro_metrics_password",
                "TZ": inputs.site.office.timezone,
            },
            "group_add": [str(inputs.facts.service_gid)],
            "depends_on": {
                "postgres": {"condition": "service_healthy"},
            },
            "secrets": ["postgres_gideon_ro_metrics_password"],
            "networks": ["gideon"],
        },
        "cadvisor": {
            "image": reference(target, image_pin(inputs, "cadvisor")),
            "restart": "unless-stopped",
            "environment": {"TZ": inputs.site.office.timezone},
            "privileged": True,
            "command": [
                "--docker_only=true",
                "--housekeeping_interval=30s",
                "--disable_metrics=advtcp,app,cpu_topology,cpuset,hugetlb,memory_numa,perf_event,process,referenced_memory,resctrl,sched,tcp,udp",
            ],
            "volumes": [
                "/:/rootfs:ro",
                "/var/run:/var/run:rw",
                "/sys:/sys:ro",
                "/var/lib/docker:/var/lib/docker:ro",
                "/dev/disk:/dev/disk:ro",
            ],
            "networks": ["gideon"],
        },
        "blackbox-exporter": {
            "image": reference(target, image_pin(inputs, "blackbox-exporter")),
            "restart": "unless-stopped",
            "environment": {"TZ": inputs.site.office.timezone},
            "command": ["--config.file=/etc/blackbox_exporter/config.yml"],
            "volumes": [
                "/etc/gideon/rendered/blackbox/blackbox.yml:/etc/blackbox_exporter/config.yml:ro",
                "/etc/gideon/ca.pem:/etc/gideon/ca.pem:ro",
            ],
            "networks": ["gideon"],
        },
    }
    # The memory table is held to the services the stack knows on any host in
    # both directions, before the marker and the site gate which ones this
    # host runs: the lock is release content, complete everywhere, so a no-GPU
    # host or a search-off site refuses the same lock a GPU host would.
    known_services = (
        *services,
        ENGINE_SERVICE_NAME,
        SEARXNG_SERVICE_NAME,
        API_SERVICE_NAME,
    )
    unknown_services = tuple(
        row.service for row in inputs.profile.memory if row.service not in known_services
    )
    if unknown_services:
        names = ", ".join(unknown_services)
        allowed = ", ".join(known_services)
        raise ValueError(
            f"Cannot render Compose: profile {inputs.profile.name}'s memory table names "
            f"{names}, which no GIDEON service is called; the services are {allowed}. "
            "Remove or rename the row in models.lock, then re-run render."
        )
    missing_services = tuple(
        name for name in known_services if inputs.profile.memory_row(name) is None
    )
    if missing_services:
        names = ", ".join(f"service '{name}'" for name in missing_services)
        rows = ", ".join(f"memory.{name}.gb" for name in missing_services)
        raise ValueError(
            f"Cannot render Compose: profile {inputs.profile.name} has no memory row for "
            f"{names}. Add {rows} to models.lock, then re-run render."
        )
    engine = None if inputs.no_gpu else engine_service(inputs, target)
    api = api_service(inputs, target) if api_enabled(inputs.no_gpu) else None

    ordered: dict[str, object] = {}
    for name, service in services.items():
        ordered[name] = service
        if name == "open-webui":
            if engine is not None:
                ordered[ENGINE_SERVICE_NAME] = engine
            if api is not None:
                ordered[API_SERVICE_NAME] = api
            if search_enabled(inputs):
                ordered[SEARXNG_SERVICE_NAME] = searxng_service(inputs, target)
    services = ordered

    if engine is not None:
        secrets[ENGINE_SECRET_NAME] = {
            "file": f"/etc/gideon/secrets/{ENGINE_SECRET_NAME}"
        }
        if api is not None:
            secrets[API_SECRET_NAME] = {
                "file": f"/etc/gideon/secrets/{API_SECRET_NAME}"
            }
    else:
        # No engine and no GPU exporter are rendered on a no-GPU host; their
        # pins stay required and mirrored, but the existing exporter entry is
        # removed after the common service document is built.
        del services["dcgm-exporter"]
    # Every rendered service carries its row's limit, applied here in one place
    # after the marker and the site have settled which services this host runs.
    # §7.6's key is mem_limit, not deploy.resources.limits.memory, and the value
    # is an exact byte count: Compose reads a g suffix as binary, 7.4 % over the
    # lock's decimal-gigabyte unit.
    limited: dict[str, object] = {}
    for name, service in services.items():
        assert isinstance(service, Mapping)
        limited[name] = {**service, "mem_limit": memory_limit_bytes(inputs.profile, name)}
    services = limited
    return {
        "name": "gideon",
        "services": services,
        "networks": {"gideon": {}},
        "volumes": {"caddy_data": {}, "caddy_config": {}},
        "secrets": secrets,
    }
