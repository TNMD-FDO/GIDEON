"""Shared Compose builders for services that serve a pinned model."""

from collections.abc import Mapping
from typing import Final

from gideon.host import gpus, weights
from gideon.host.images import RegistryTarget, parse_registry, reference
from gideon.host.models import ModelPin
from gideon.host.render import RenderInputs
from gideon.host.render.engine import (
    GENERATOR,
    INTEGRATION_NETWORK_NAME,
    ModelServerMember,
)
from gideon.host.render.services import (
    MountedSecret,
    ServiceDefinition,
    image_pin,
    secret_wrapper,
)

# vLLM v0.27.1's image supplies ``[vllm, serve]`` as its entrypoint and no
# command. The wrapper replaces that entrypoint to read the mounted API-key
# file before execing the same server.
ENGINE_SERVER: Final = "vllm serve"
ENGINE_USAGE_SWITCHES: Final[Mapping[str, str]] = {
    "VLLM_NO_USAGE_STATS": "1",
    "DO_NOT_TRACK": "1",
}
# A model server's two probes — the container healthcheck's path below and
# Prometheus's default metrics path — are the lines vLLM's access log excludes:
# render provisions both probes, so render silences what it causes, and a
# request's own line keeps its shape. The option takes one comma-separated
# string on v0.27.1, never two arguments.
# exempt: no figure — a decision.
ENGINE_HEALTH_PATH: Final = "/health"
ENGINE_ACCESS_LOG_EXCLUDED_PATHS: Final[tuple[str, ...]] = (
    ENGINE_HEALTH_PATH,
    "/metrics",
)


def model_healthcheck(member: ModelServerMember) -> Mapping[str, object]:
    """Return the member's HTTP healthcheck and startup allowance."""

    return {
        "test": [
            "CMD-SHELL",
            f"curl --silent --fail http://127.0.0.1:{member.port}{ENGINE_HEALTH_PATH}",
        ],
        "interval": "30s",
        "timeout": "10s",
        "retries": 3,
        "start_period": f"{member.ready_seconds}s",
    }


ENGINE_HEALTHCHECK: Mapping[str, object] = model_healthcheck(GENERATOR)


def model_wrapper(secret_path: str, server: str, member: ModelServerMember) -> list[str]:
    """Build the fail-closed entrypoint for a member's API key."""

    return secret_wrapper(
        (MountedSecret(secret_path, "VLLM_API_KEY", member.key_file_words),),
        server,
        member.service_name,
    )


def model_pin(inputs: RenderInputs, member: ModelServerMember) -> ModelPin:
    """Return the profile's pin for a model-server member."""

    model = inputs.profile.model(member.role)
    if model is None:
        raise ValueError(
            f"Cannot render Compose: profile {inputs.profile.name} has no {member.role} model. "
            f"Add the {member.role} model to models.lock, then re-run render."
        )
    return model


def model_command(pin: ModelPin, member: ModelServerMember) -> list[str]:
    """Build the vLLM command from a member's locked serving baseline."""

    command = [
        pin.repo,
        "--revision",
        pin.revision,
        "--served-model-name",
        pin.serve.served_name,
        "--host",
        "0.0.0.0",
        "--port",
        str(member.port),
        "--disable-access-log-for-endpoints",
        ",".join(ENGINE_ACCESS_LOG_EXCLUDED_PATHS),
    ]
    for name, value in pin.serve.flags.items():
        command.append(f"--{name}")
        if value is not True:
            command.append(str(value))
    return command


def model_service(
    inputs: RenderInputs,
    member: ModelServerMember,
    target: RegistryTarget | None = None,
) -> Mapping[str, object]:
    """Build a model server from release and profile inputs."""

    model = model_pin(inputs, member)
    if model.gpu < 0 or model.gpu >= len(inputs.facts.gpu_uuids):
        raise ValueError(
            f"Cannot render Compose: profile {inputs.profile.name} assigns {member.role} GPU "
            f"index {model.gpu}, but the GPU record {gpus.GPU_RECORD_PATH} names "
            f"{len(inputs.facts.gpu_uuids)} card(s). Run nvidia-smi -L and re-run "
            f"preflight's hardware-profile check. {gpus.re_record_fix()}"
        )
    hf_home = model.serve.env.get("HF_HOME")
    if not hf_home:
        raise ValueError(
            f"Cannot render Compose: profile {inputs.profile.name}'s {member.role} serve.env "
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
    networks = ["gideon"]
    if member.joins_integration_network:
        networks.append(INTEGRATION_NETWORK_NAME)
    return {
        "image": reference(target, image_pin(inputs, "vllm-openai")),
        "restart": "unless-stopped",
        "environment": environment,
        "entrypoint": model_wrapper(
            f"/run/secrets/{member.secret_name}", ENGINE_SERVER, member
        ),
        "command": model_command(model, member),
        "devices": [f"nvidia.com/gpu={inputs.facts.gpu_uuids[model.gpu]}"],
        "volumes": [f"{weights.MODELS_ROOT}:{hf_home}:ro"],
        "secrets": [member.secret_name],
        "healthcheck": dict(model_healthcheck(member)),
        "networks": networks,
    }


class ModelServerService(ServiceDefinition):
    """A registry definition bound to one model-server member."""

    def __init__(self, member: ModelServerMember) -> None:
        self.member = member
        self.name = member.service_name
        self.slow_start_seconds = member.ready_seconds

    def applies(self, inputs: RenderInputs) -> bool:
        return not inputs.no_gpu

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        return model_service(inputs, self.member, target)
