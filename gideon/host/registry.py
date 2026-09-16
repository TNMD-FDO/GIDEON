"""Mirror the image lock into the release registry."""

import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from gideon.host.images import (
    BuiltImagePin,
    ImageLock,
    MirroredImagePin,
    RegistryTarget,
    load_image_lock,
    parse_registry,
    proxy_environment,
    reference,
    render_errors,
)
from gideon.host.report import command_detail, one_line
from gideon.host.report import refusal as report_refusal
from gideon.host.site import SiteConfig, load_site
from gideon.host.sysio import Host, PathLike, RealHost

_SITE_PATH: Final = "/etc/gideon/site.yaml"
_SKOPEO_FIX: Final = (
    "Run sudo python3 -m gideon host provision --only host-tools, "
    "then re-run registry mirror."
)
_DOCKER_FIX: Final = (
    "Run gideon host provision --only docker-engine, then re-run registry mirror."
)
_DOCKER_ACCESS_FIX: Final = (
    "Run registry mirror as a user with Docker access (the docker group, or sudo), "
    "then re-run it."
)
_DESTINATION_FIX: Final = (
    "Pass --to <registry> when the site file cannot provide a registry."
)
_REGISTRY_FIX: Final = (
    "Correct the registry destination, then re-run registry mirror."
)
_COPY_FIX: Final = (
    "Check egress to the upstream registry and access to the destination registry, "
    "then re-run registry mirror."
)
_BUILD_FIX: Final = (
    "Build and push it with python3 -m tools.imagebuild {name} --to {registry} "
    "(it records the digest in images.lock), commit, then re-run registry mirror."
)


class MirrorOutcome(StrEnum):
    """The result reported for one image lock entry."""

    MIRRORED = "mirrored"
    PRESENT = "present"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class _CopySpec:
    """The source and destination tag data for one registry copy."""

    name: str
    repository: str
    digest: str
    tag: str


def _refusal(problem: object, fix: str) -> str:
    return report_refusal("registry mirror", problem, fix)


def _source_reference(spec: _CopySpec) -> str:
    return f"docker://{spec.repository}@{spec.digest}"


def _destination_repository(target: RegistryTarget, name: str) -> str:
    return f"docker://{target.authority}/{target.prefix}{name}"


def _manifest_argv(target: RegistryTarget, image_reference: str) -> list[str]:
    argv = ["docker", "manifest", "inspect"]
    if target.scheme == "http":
        argv.append("--insecure")
    argv.append(image_reference)
    return argv


def _report(
    name: str,
    outcome: MirrorOutcome,
    image_reference: str,
    *,
    detail: str = "",
    fix: str = "",
) -> None:
    if outcome is MirrorOutcome.FAILED:
        print(f"{name}: failed — {one_line(detail)} Fix: {fix}")
    else:
        print(f"{name}: {outcome.value} — {image_reference}")


def _resolve_destination(
    io: Host, site_path: PathLike, destination: str | None
) -> tuple[SiteConfig | None, str | None]:
    result = load_site(Path(site_path), host=io)
    if destination is not None:
        return result.config, destination
    if result.config is None or result.errors:
        print(
            _refusal(
                "a destination registry is unavailable from the site file",
                _DESTINATION_FIX,
            ),
            file=sys.stderr,
        )
        return None, None
    return result.config, result.config.registry


def _skopeo_available(io: Host) -> bool:
    try:
        result = io.run(["skopeo", "--version"])
    except OSError as exc:
        print(_refusal(f"skopeo is unavailable: {exc}", _SKOPEO_FIX), file=sys.stderr)
        return False
    if result.returncode == 127:
        print(_refusal("skopeo is not available", _SKOPEO_FIX), file=sys.stderr)
        return False
    if result.returncode != 0:
        print(
            _refusal(
                f"skopeo version check failed: {command_detail(result)}",
                _SKOPEO_FIX,
            ),
            file=sys.stderr,
        )
        return False
    return True


def _manifest_probe(
    io: Host,
    target: RegistryTarget,
    image_reference: str,
    environment: Mapping[str, str] | None,
) -> subprocess.CompletedProcess[str] | None:
    try:
        result = io.run(
            _manifest_argv(target, image_reference),
            env=environment,
        )
    except OSError as exc:
        print(
            _refusal(
                f"Docker manifest probe could not run for {image_reference}: {exc}",
                _DOCKER_FIX,
            ),
            file=sys.stderr,
        )
        return None
    if result.returncode == 127:
        print(
            _refusal(
                f"Docker manifest probe is unavailable for {image_reference}",
                _DOCKER_FIX,
            ),
            file=sys.stderr,
        )
        return None
    if result.returncode != 0 and "permission denied" in result.stderr.lower():
        print(
            _refusal(
                f"Docker refused the manifest probe for {image_reference}: {command_detail(result)}",
                _DOCKER_ACCESS_FIX,
            ),
            file=sys.stderr,
        )
        return None
    return result


def _copy(
    io: Host,
    target: RegistryTarget,
    spec: _CopySpec,
    image_reference: str,
    environment: Mapping[str, str] | None,
) -> tuple[MirrorOutcome, str]:
    source = _source_reference(spec)
    destination = f"{_destination_repository(target, spec.name)}:{spec.tag}"
    argv = ["skopeo", "copy", "--all", "--preserve-digests"]
    if target.scheme == "http":
        argv.append("--dest-tls-verify=false")
    argv.extend([source, destination])
    try:
        copied = io.run(argv, env=environment)
    except OSError as exc:
        return MirrorOutcome.FAILED, str(exc)
    if copied.returncode != 0:
        return MirrorOutcome.FAILED, command_detail(copied)

    probe = _manifest_probe(io, target, image_reference, environment)
    if probe is None:
        return MirrorOutcome.FAILED, "Docker manifest probe is unavailable"
    if probe.returncode != 0:
        return MirrorOutcome.FAILED, command_detail(probe)
    return MirrorOutcome.MIRRORED, ""


def _ensure(
    io: Host,
    target: RegistryTarget,
    spec: _CopySpec,
    image_reference: str,
    environment: Mapping[str, str] | None,
) -> tuple[MirrorOutcome, str] | None:
    """Probe a digest and copy it when absent; ``None`` when the probe cannot run."""

    probe = _manifest_probe(io, target, image_reference, environment)
    if probe is None:
        return None
    if probe.returncode == 0:
        return MirrorOutcome.PRESENT, ""
    return _copy(io, target, spec, image_reference, environment)


def run_registry_mirror(
    args: object,
    *,
    host: Host | None = None,
    site_path: PathLike = _SITE_PATH,
    images_path: PathLike | None = None,
    root: PathLike | None = None,
) -> int:
    """Mirror every image lock entry into the selected release registry."""

    io = host or RealHost()
    destination_arg = getattr(args, "to", None)
    destination = destination_arg if isinstance(destination_arg, str) else None
    site, destination = _resolve_destination(io, site_path, destination)
    if destination is None:
        return 1

    target = parse_registry(destination)
    if target is None:
        print(
            _refusal(
                f"registry destination is not usable: {destination}",
                _REGISTRY_FIX,
            ),
            file=sys.stderr,
        )
        return 1

    checkout = Path(__file__).parents[2] if root is None else Path(root)
    actual_images = checkout / "images.lock" if images_path is None else images_path
    image_result = load_image_lock(actual_images, host=io)
    if image_result.errors or image_result.lock is None:
        print(render_errors(image_result.errors), file=sys.stderr)
        return 1
    lock: ImageLock = image_result.lock

    if not _skopeo_available(io):
        return 1

    environment: Mapping[str, str] | None = None
    if site is not None and site.egress_proxy:
        proxy = proxy_environment(site, io)
        if not proxy.ok:
            print(_refusal(proxy.problem or "proxy is unusable", proxy.fix), file=sys.stderr)
            return 1
        environment = proxy.variables

    failed = False
    for pin in lock.images:
        if isinstance(pin, BuiltImagePin):
            build_fix = _BUILD_FIX.format(name=pin.name, registry=destination)
            # The base lives in the same repository under its own tag, mirrored
            # first even for a pin nobody has built: a build starts FROM it
            # here, so a build needs egress for apt only.
            base_repository, base_tag = pin.base.rsplit(":", 1)
            base_spec = _CopySpec(pin.name, base_repository, pin.base_digest, base_tag)
            base_reference = f"{target.authority}/{target.prefix}{pin.name}@{pin.base_digest}"
            ensured = _ensure(io, target, base_spec, base_reference, environment)
            if ensured is None:
                return 1
            outcome, detail = ensured
            _report(f"{pin.name}/base", outcome, base_reference, detail=detail, fix=_COPY_FIX)
            failed = failed or outcome is MirrorOutcome.FAILED

            if not pin.built:
                _report(
                    pin.name,
                    MirrorOutcome.FAILED,
                    f"{target.authority}/{target.prefix}{pin.name}",
                    detail="the image has not been built yet",
                    fix=build_fix,
                )
                failed = True
                continue
            image_reference = reference(target, pin)
            probe = _manifest_probe(io, target, image_reference, environment)
            if probe is None:
                return 1
            if probe.returncode == 0:
                _report(pin.name, MirrorOutcome.PRESENT, image_reference)
                continue
            _report(
                pin.name,
                MirrorOutcome.FAILED,
                image_reference,
                detail=command_detail(probe),
                fix=build_fix,
            )
            failed = True
        elif isinstance(pin, MirroredImagePin):
            image_reference = reference(target, pin)
            source_repository, source_tag = pin.source.rsplit(":", 1)
            spec = _CopySpec(pin.name, source_repository, pin.digest, source_tag)
            ensured = _ensure(io, target, spec, image_reference, environment)
            if ensured is None:
                return 1
            outcome, detail = ensured
            _report(pin.name, outcome, image_reference, detail=detail, fix=_COPY_FIX)
            failed = failed or outcome is MirrorOutcome.FAILED
    return int(failed)
