"""Compose definition for gideon-generator."""

from collections.abc import Mapping
from typing import Final

from gideon.host import weights
from gideon.host.images import RegistryTarget, parse_registry, reference
from gideon.host.models import ModelPin
from gideon.host.render import RenderInputs
from gideon.host.render.engine import (
    ENGINE_PORT,
    ENGINE_SECRET_NAME,
    ENGINE_SERVICE_NAME,
)
from gideon.host.render.services import ServiceDefinition, image_pin, secret_wrapper

# vLLM v0.27.1's image supplies ``[vllm, serve]`` as its entrypoint and no
# command. The wrapper replaces that entrypoint to read the mounted API-key
# file before execing the same server.
ENGINE_READY_SECONDS: Final = 900
ENGINE_SERVER: Final = "vllm serve"
ENGINE_USAGE_SWITCHES: Final[Mapping[str, str]] = {
    "VLLM_NO_USAGE_STATS": "1",
    "DO_NOT_TRACK": "1",
}
# The engine's two probes — the container healthcheck's path below and
# Prometheus's default metrics path — are the lines vLLM's access log excludes:
# render provisions both probes, so render silences what it causes, and a
# turn's own request line keeps its shape. The option takes one
# comma-separated string on v0.27.1, never two arguments.
# exempt: no figure — a decision.
ENGINE_HEALTH_PATH: Final = "/health"
ENGINE_ACCESS_LOG_EXCLUDED_PATHS: Final[tuple[str, ...]] = (
    ENGINE_HEALTH_PATH,
    "/metrics",
)
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

    return secret_wrapper(
        secret_path, "VLLM_API_KEY", "engine API key", server, ENGINE_SERVICE_NAME
    )


def generator_pin(inputs: RenderInputs) -> ModelPin:
    """Return the profile's generator pin, which the engine and the service both name."""

    model = inputs.profile.model("generator")
    if model is None:
        raise ValueError(
            f"Cannot render Compose: profile {inputs.profile.name} has no generator model. "
            "Add the generator model to models.lock, then re-run render."
        )
    return model


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

    model = generator_pin(inputs)
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


class GeneratorService(ServiceDefinition):
    """The gideon-generator service in the Compose project."""

    name = ENGINE_SERVICE_NAME
    slow_start_seconds = ENGINE_READY_SECONDS

    def applies(self, inputs: RenderInputs) -> bool:
        return not inputs.no_gpu

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        return engine_service(inputs, target)
