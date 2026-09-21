"""Pure build plans and Host-seam operations for built image pins."""

import json
import os
import stat
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from gideon.host.images import DIGEST, BuiltImagePin, RegistryTarget, reference
from gideon.host.report import command_detail, one_line
from gideon.host.sysio import Host
from tools.pinwatch.patch import PatchError, apply_bump
from tools.pinwatch.pins import Bump, Change


@dataclass(frozen=True, slots=True)
class Smoke:
    """The executable and lock arguments used to smoke-test one image."""

    argv: tuple[str, ...]
    version_args: tuple[str, ...]


_GIDEON_SMOKE_SCRIPT: Final[str] = (
    "import importlib.metadata\n"
    "import starlette\n"
    "import uvicorn\n"
    "import httpx\n"
    "import psycopg\n"
    "if psycopg.pq.__impl__ != \"binary\":\n"
    "    raise SystemExit(\"psycopg is not using the binary libpq implementation\")\n"
    # One build argument installs both driver wheels, so a pair at two versions
    # means the image did not come from this Dockerfile's install line. The
    # generic version check below reads each argument as a substring of the
    # output, which one matching line would satisfy for both.
    "if importlib.metadata.version(\"psycopg\") "
    "!= importlib.metadata.version(\"psycopg-binary\"):\n"
    "    raise SystemExit(\"psycopg and psycopg-binary are at different versions\")\n"
    "for package in (\"starlette\", \"uvicorn\", \"httpx\", \"anyio\", "
    "\"httpcore\", \"h11\", \"certifi\", \"idna\", \"click\", "
    "\"typing_extensions\", \"psycopg\", \"psycopg-binary\"):\n"
    "    print(package, importlib.metadata.version(package))\n"
)


SMOKE: Final[Mapping[str, Smoke]] = {
    "postgres": Smoke(("pgbackrest", "version"), ("PGBACKREST_VERSION",)),
    "gideon": Smoke(("python", "-c", _GIDEON_SMOKE_SCRIPT), (
        "STARLETTE_VERSION",
        "UVICORN_VERSION",
        "HTTPX_VERSION",
        "ANYIO_VERSION",
        "HTTPCORE_VERSION",
        "H11_VERSION",
        "CERTIFI_VERSION",
        "IDNA_VERSION",
        "CLICK_VERSION",
        "TYPING_EXTENSIONS_VERSION",
        "PSYCOPG_VERSION",
    )),
}


@dataclass(frozen=True, slots=True)
class BuildPlan:
    """The deterministic Docker invocation derived from a built pin."""

    pin: BuiltImagePin
    tag: str
    reference: str
    repository: str
    context: Path
    build_argv: tuple[str, ...]
    labels: Mapping[str, str]
    smoke: Smoke
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BuildResult:
    """The pushed digest, or a problem and operator fix."""

    digest: str | None = None
    problem: str | None = None
    fix: str = ""
    refusal: bool = False

    @property
    def ok(self) -> bool:
        return self.digest is not None and self.problem is None


@dataclass(frozen=True, slots=True)
class CheckReport:
    """The result of proving one registry image against its lock pin."""

    ok: bool
    problem: str = ""
    fix: str = ""
    refusal: bool = False


@dataclass(frozen=True, slots=True)
class RecordResult:
    """The result of recording a pushed digest and regenerated fixtures."""

    ok: bool
    problem: str = ""
    fix: str = ""


_DOCKER_FIX: Final = (
    "Run gideon host provision --only docker-engine, then re-run image build."
)
_DOCKER_ACCESS_FIX: Final = (
    "Run image build as a user with Docker access (the docker group, or sudo), "
    "then re-run it."
)
_BUILD_FIX: Final = "Build it with python3 -m tools.imagebuild {name} --to {registry}."
_RECORD_FIX: Final = "Fix the lock or fixture regeneration, then re-run image build."
_PROXY_ARGS: Final = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")


def _child_env(environment: Mapping[str, str] | None) -> dict[str, str] | None:
    """The Docker child's environment: the caller's plus the proxy variables, or inherit."""

    if not environment:
        return None
    return {**os.environ, **environment}


def _repository(target: RegistryTarget, name: str) -> str:
    return f"{target.authority}/{target.prefix}{name}"


def _docker_error(
    action: str, result: subprocess.CompletedProcess[str], *, fix: str
) -> BuildResult:
    if result.returncode == 127:
        return BuildResult(problem=f"Docker {action} is not available", fix=_DOCKER_FIX, refusal=True)
    if "permission denied" in result.stderr.lower():
        return BuildResult(
            problem=f"Docker refused {action}: {command_detail(result)}",
            fix=_DOCKER_ACCESS_FIX,
            refusal=True,
        )
    return BuildResult(
        problem=f"Docker {action} failed: {command_detail(result)}", fix=fix
    )


def image_labels(pin: BuiltImagePin) -> dict[str, str]:
    """The OCI labels a built image carries: its base and every build input.

    Stamped by the build and compared by ``check``, so the registry's image
    proves which lock inputs produced it.
    """

    labels = {
        "org.opencontainers.image.base.name": pin.base,
        "org.opencontainers.image.base.digest": pin.base_digest,
    }
    labels.update({f"gideon.build-arg.{name}": value for name, value in pin.build_args.items()})
    return labels


def build_plan(
    pin: BuiltImagePin,
    target: RegistryTarget,
    root: Path,
    proxy_variables: Mapping[str, str] | None = None,
) -> BuildPlan:
    """Compute the build tag, labels, argv, and context without host access."""

    repository = _repository(target, pin.name)
    base_tag = pin.base.rsplit(":", 1)[1]
    tag = f"{repository}:{base_tag}-gideon"
    base_reference = f"{repository}@{pin.base_digest}"
    build_args: list[tuple[str, str]] = [("BASE", base_reference)]
    build_args.extend(
        (name, value) for name, value in pin.build_args.items()
    )
    environment = dict(proxy_variables or {})
    labels = image_labels(pin)
    argv: list[str] = ["docker", "build", "--provenance=false", "--sbom=false"]
    argv.extend(
        item
        for name, value in build_args
        for item in ("--build-arg", f"{name}={value}")
    )
    # Docker's predefined proxy build args are excluded from image history and
    # cache, and a bare `--build-arg NAME` takes its value from the child's
    # environment — so a proxy credential is never in argv, a process listing,
    # or the dry-run row; it reaches the build only through `_child_env`.
    argv.extend(
        item
        for name in _PROXY_ARGS
        if name in environment
        for item in ("--build-arg", name)
    )
    argv.extend(
        item
        for name, value in labels.items()
        for item in ("--label", f"{name}={value}")
    )
    argv.extend(("--tag", tag, os.fspath(root / pin.build)))
    smoke = SMOKE.get(pin.name)
    if smoke is None:
        raise ValueError(f"no smoke command is registered for built image '{pin.name}'")
    return BuildPlan(
        pin=pin,
        tag=tag,
        reference=f"{repository}@{pin.digest}",
        repository=repository,
        context=root / pin.build,
        build_argv=tuple(argv),
        labels=labels,
        smoke=smoke,
        environment=environment,
    )


def _smoke_argv(image: str, smoke: Smoke) -> tuple[str, ...]:
    return ("docker", "run", "--rm", "--entrypoint", smoke.argv[0], image, *smoke.argv[1:])


def _smoke(
    host: Host,
    image: str,
    pin: BuiltImagePin,
    smoke: Smoke,
    environment: Mapping[str, str],
) -> BuildResult | None:
    missing_args = tuple(name for name in smoke.version_args if name not in pin.build_args)
    if missing_args:
        return BuildResult(
            problem=(
                "smoke arguments "
                f"{', '.join(missing_args)} are absent from build_args"
            ),
            fix=_BUILD_FIX.format(name=pin.name, registry="the selected registry"),
        )
    result = host.run(_smoke_argv(image, smoke), env=_child_env(environment))
    if result.returncode == 127:
        return BuildResult(problem="Docker smoke command is not available", fix=_DOCKER_FIX, refusal=True)
    if "permission denied" in result.stderr.lower():
        return BuildResult(
            problem=f"Docker refused the smoke command: {command_detail(result)}",
            fix=_DOCKER_ACCESS_FIX,
            refusal=True,
        )
    expected = tuple(pin.build_args[name].split("-", 1)[0] for name in smoke.version_args)
    missing_versions = tuple(value for value in expected if value not in result.stdout)
    detail = command_detail(result)
    if result.returncode != 0 or missing_versions:
        return BuildResult(
            problem=(
                f"smoke failed: {detail}; expected {', '.join(repr(value) for value in expected)} "
                f"in output, missing {', '.join(repr(value) for value in missing_versions)}; "
                "the push did not happen"
            ),
            fix="Correct the image build or smoke command, then retry.",
        )
    return None


def build(host: Host, plan: BuildPlan) -> BuildResult:
    """Build, smoke-test, push, and read the digest of one image."""

    result = host.run(plan.build_argv, env=_child_env(plan.environment))
    if result.returncode != 0:
        return _docker_error("build", result, fix="Correct the Dockerfile or build inputs, then retry.")

    smoke_error = _smoke(host, plan.tag, plan.pin, plan.smoke, plan.environment)
    if smoke_error is not None:
        return smoke_error

    result = host.run(("docker", "push", plan.tag), env=_child_env(plan.environment))
    if result.returncode != 0:
        return _docker_error("push", result, fix="Check the destination registry, then retry.")

    inspect = host.run(
        ("docker", "image", "inspect", "--format", "{{json .RepoDigests}}", plan.tag),
        env=_child_env(plan.environment),
    )
    if inspect.returncode != 0:
        return _docker_error("image inspect", inspect, fix="Check the pushed image, then retry.")
    try:
        digests = json.loads(inspect.stdout)
    except json.JSONDecodeError:
        return BuildResult(problem="Docker image inspect returned invalid RepoDigests JSON", fix="Check the pushed image, then retry.")
    if not isinstance(digests, list):
        return BuildResult(problem="Docker image inspect returned no RepoDigests list", fix="Check the pushed image, then retry.")
    prefix = f"{plan.repository}@"
    digest = next(
        (item.removeprefix(prefix) for item in digests if isinstance(item, str) and item.startswith(prefix)),
        None,
    )
    if digest is None or DIGEST.fullmatch(digest) is None:
        return BuildResult(
            problem=f"Docker image inspect has no digest under {prefix}",
            fix="Check the pushed image, then retry.",
        )
    return BuildResult(digest=digest)


def _check_error(
    action: str, result: subprocess.CompletedProcess[str], *, fix: str
) -> CheckReport:
    docker_error = _docker_error(action, result, fix=fix)
    return CheckReport(
        ok=False,
        problem=docker_error.problem or "Docker command failed",
        fix=docker_error.fix,
        refusal=docker_error.refusal,
    )


def check(
    host: Host,
    target: RegistryTarget,
    pin: BuiltImagePin,
    environment: Mapping[str, str] | None = None,
) -> CheckReport:
    """Prove the registry image, labels, and smoke command against a lock pin."""

    if not pin.built:
        return CheckReport(
            ok=False,
            problem="built image is unbuilt",
            fix=_BUILD_FIX.format(name=pin.name, registry=target.authority),
        )
    image = reference(target, pin)
    registry = f"{target.authority}/{target.prefix}".rstrip("/")
    fix = _BUILD_FIX.format(name=pin.name, registry=registry)
    inspect = host.run(
        (
            "docker",
            "manifest",
            "inspect",
            *(("--insecure",) if target.scheme == "http" else ()),
            image,
        ),
        env=_child_env(environment),
    )
    if inspect.returncode != 0:
        return _check_error("manifest probe", inspect, fix=fix)
    pulled = host.run(("docker", "pull", image), env=_child_env(environment))
    if pulled.returncode != 0:
        return _check_error("pull", pulled, fix=fix)
    labels_result = host.run(
        ("docker", "image", "inspect", "--format", "{{json .Config.Labels}}", image),
        env=_child_env(environment),
    )
    if labels_result.returncode != 0:
        return _check_error("image inspect", labels_result, fix=fix)
    try:
        labels = json.loads(labels_result.stdout)
    except json.JSONDecodeError:
        return CheckReport(False, "Docker image inspect returned invalid labels JSON", fix)
    if not isinstance(labels, dict):
        labels = {}
    for name, value in image_labels(pin).items():
        if labels.get(name) != value:
            return CheckReport(
                False,
                f"label {name} is {labels.get(name)!r}, expected {value!r}",
                fix,
            )
    smoke = SMOKE.get(pin.name)
    if smoke is None:
        return CheckReport(False, f"no smoke command is registered for {pin.name}", fix)
    smoke_error = _smoke(host, image, pin, smoke, environment or {})
    if smoke_error is not None:
        return CheckReport(False, smoke_error.problem or "smoke failed", smoke_error.fix, smoke_error.refusal)
    return CheckReport(True)


def _fixture_files(host: Host, directory: Path) -> list[Path]:
    files: list[Path] = []
    try:
        names = host.listdir(directory)
    except OSError:
        return files
    for name in names:
        path = directory / name
        try:
            is_directory = stat.S_ISDIR(host.stat(path).st_mode)
        except OSError:
            continue
        if is_directory:
            files.extend(_fixture_files(host, path))
        else:
            files.append(path)
    return files


def record(
    host: Host,
    root: Path,
    pin: BuiltImagePin,
    digest: str,
    inputs_digest: str,
) -> RecordResult:
    """Patch the two digest paths, regenerate fixtures, and restore ownership."""

    lock_path = root / "images.lock"
    try:
        original = host.read_text(lock_path)
        bump = Bump(
            pin_id=f"images.{pin.name}",
            lock="images.lock",
            changes=(
                Change(f"images.{pin.name}.digest", pin.digest, digest),
                Change(f"images.{pin.name}.inputs_digest", pin.inputs_digest, inputs_digest),
            ),
            upstream_url="",
            proposal=False,
        )
        patched = apply_bump(original, bump)
        host.write_text(lock_path, patched)
    except (OSError, PatchError) as exc:
        return RecordResult(False, f"could not record images.lock: {one_line(exc)}", _RECORD_FIX)

    regenerated = host.run(
        [sys.executable, "tests/regenerate_render_fixtures.py"], cwd=root
    )
    if regenerated.returncode != 0:
        return RecordResult(
            False,
            f"fixture regeneration failed: {command_detail(regenerated)}",
            "Run python3 tests/regenerate_render_fixtures.py, then retry.",
        )

    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if sudo_uid is not None and sudo_gid is not None:
        try:
            uid, gid = int(sudo_uid), int(sudo_gid)
            host.chown(lock_path, uid, gid)
            for path in _fixture_files(host, root / "tests/fixtures/render"):
                host.chown(path, uid, gid)
        except (OSError, ValueError) as exc:
            return RecordResult(False, f"could not restore file ownership: {one_line(exc)}", _RECORD_FIX)
    return RecordResult(True)
