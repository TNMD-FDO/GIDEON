"""The rendered stack's secret mounts and carried secret environment values."""

from collections.abc import Mapping
from dataclasses import dataclass

from gideon.host.render import ARTIFACTS, RenderInputs
from gideon.host.render.compose import service_blocks


@dataclass(frozen=True, slots=True)
class SecretConsumers:
    """The services that mount or carry one rendered secret."""

    mounts: tuple[str, ...]
    carried: tuple[str, ...]


def secret_consumers(inputs: RenderInputs) -> Mapping[str, SecretConsumers]:
    """Return the rendered stack's consumers, preserving first-seen order."""

    mounts: dict[str, list[str]] = {}
    carried: dict[str, list[str]] = {}
    for service, block in service_blocks(inputs).items():
        assert isinstance(block, Mapping)
        service_secrets = block.get("secrets", ())
        assert isinstance(service_secrets, (list, tuple))
        for name in service_secrets:
            assert isinstance(name, str)
            mounts.setdefault(name, []).append(service)

    for artifact in ARTIFACTS:
        if not artifact.secret or not artifact.applies(inputs):
            continue
        for name in artifact.secret_names(inputs):
            carried.setdefault(name, [])
            for owner in artifact.owners:
                if owner not in carried[name]:
                    carried[name].append(owner)

    names = (*mounts, *(name for name in carried if name not in mounts))
    return {
        name: SecretConsumers(tuple(mounts.get(name, ())), tuple(carried.get(name, ())))
        for name in names
    }


def consumers_of(inputs: RenderInputs, name: str) -> SecretConsumers:
    """Return one secret's consumers, or empty tuples when it is not rendered."""

    return secret_consumers(inputs).get(name, SecretConsumers((), ()))
