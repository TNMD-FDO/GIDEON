"""Loading and validation for the committed hardware-model lock.

The models lock is release data, not site configuration.  It names the
hardware profiles a release has evaluated and the model files and serving
baseline belonging to each profile.
"""

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

import yaml  # type: ignore[import-untyped]

from gideon.host.images import DIGEST
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike, RealHost

GIGABYTE: Final = 1_000_000_000
"""Decimal gigabytes: lock figures compared with kernel and NVIDIA totals converted to bytes."""


@dataclass(frozen=True, slots=True)
class PinnedFile:
    """One regular model-repository file pinned by digest and byte size."""

    path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ServeBaseline:
    """The release-owned serving name, environment, and engine flags."""

    served_name: str
    env: Mapping[str, str]
    flags: Mapping[str, str | int | float | bool]


@dataclass(frozen=True, slots=True)
class MemoryRow:
    """The decimal-gigabyte memory limit for one Compose service."""

    service: str
    gb: int
    role: str | None


@dataclass(frozen=True, slots=True)
class ModelPin:
    """One model role and its immutable release pin."""

    role: str
    repo: str
    revision: str
    gpu: int
    serve: ServeBaseline
    files: tuple[PinnedFile, ...]


@dataclass(frozen=True, slots=True)
class GpuRequirements:
    """Minimum GPU facts required by a hardware profile."""

    architecture: str
    compute_capability: str
    model: str
    count: int
    vram_gb: int


@dataclass(frozen=True, slots=True)
class ProfileRequirements:
    """Minimum host facts required by a hardware profile."""

    platform: str
    gpu: GpuRequirements
    dram_gb: int
    data_volume_gb: int


@dataclass(frozen=True, slots=True)
class HardwareProfile:
    """A named, evaluated hardware configuration in the models lock."""

    name: str
    requires: ProfileRequirements
    memory: tuple[MemoryRow, ...]
    models: tuple[ModelPin, ...]

    def model(self, role: str) -> ModelPin | None:
        """Return the model for *role*, or ``None`` when it is absent."""

        return next((model for model in self.models if model.role == role), None)

    def memory_row(self, service: str) -> MemoryRow | None:
        """Return the memory row for *service*, or ``None`` when absent."""

        return next((row for row in self.memory if row.service == service), None)


@dataclass(frozen=True, slots=True)
class ModelsLock:
    """The version, reference profile, and ordered profiles in ``models.lock``."""

    version: int
    reference: str
    profiles: tuple[HardwareProfile, ...]

    def profile(self, name: str) -> HardwareProfile | None:
        """Return the profile named *name*, or ``None`` when it is absent."""

        return next((profile for profile in self.profiles if profile.name == name), None)


@dataclass(frozen=True, slots=True)
class ModelsLockError:
    """A structured refusal from models-lock loading or validation."""

    key_path: str | None
    problem: str
    fix: str


@dataclass(frozen=True, slots=True)
class ModelsLockLoadResult:
    """The parsed models lock, or every error found while loading it."""

    lock: ModelsLock | None = None
    document: Mapping[str, object] | None = None
    errors: tuple[ModelsLockError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors and self.lock is not None


class _DuplicateKeyError(yaml.YAMLError):
    """Raised when a YAML mapping repeats a key."""


class ModelsLockLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate keys."""


ModelsLockLoader.yaml_implicit_resolvers = {
    initial: list(resolvers)
    for initial, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_mapping_without_duplicates(
    loader: ModelsLockLoader, node: Any, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            line = key_node.start_mark.line + 1
            raise _DuplicateKeyError(f"duplicate mapping key {key!r} at line {line}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


ModelsLockLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_without_duplicates,
)


_FIX: Final = (
    "Edit models.lock; consult docs/archi/host.md."
)
_MEMORY_FIX: Final = (
    "Edit models.lock; consult docs/archi/host.md."
)
_ROOT_KEYS: Final = ("version", "reference", "profiles")
_PROFILE_KEYS: Final = ("requires", "memory", "models")
_REQUIRES_KEYS: Final = ("platform", "gpu", "dram_gb", "data_volume_gb")
_GPU_KEYS: Final = (
    "architecture",
    "compute_capability",
    "model",
    "count",
    "vram_gb",
)
_MODEL_KEYS: Final = ("repo", "revision", "gpu", "serve", "files")
_SERVE_KEYS: Final = ("served_name", "env", "flags")
_FILE_KEYS: Final = ("sha256", "size")
_MEMORY_ROW_KEYS: Final = ("gb", "role")

PROFILE_NAME: Final = re.compile(
    r"^(?:[1-9][0-9]*x[1-9][0-9]*v-[1-9][0-9]*d|[1-9][0-9]*u)$"
)
"""The hardware-profile name grammar from §1.1."""

PLATFORM: Final = re.compile(r"^[a-z0-9_]+$")
"""A machine name as ``uname -m`` prints it (``x86_64``, ``aarch64``)."""

COMPUTE_CAPABILITY: Final = re.compile(r"^[0-9]+\.[0-9]+$")
"""A quoted ``major.minor`` compute capability, as ``nvidia-smi`` prints it."""

ROLE_NAME: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
"""Lowercase words and digits joined by single hyphens."""

SERVICE_NAME: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
"""The lowercase, hyphenated Compose service-name grammar."""

FLAG_NAME: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
"""The patcher-safe grammar for an engine flag without ``--``."""

HF_REPO: Final = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$"
)
"""A Hugging Face owner/name repository reference."""

REVISION: Final = re.compile(r"^[0-9a-f]{40}$")
"""A lowercase forty-character revision commit."""

FILE_PATH: Final = re.compile(
    r"^[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$"
)
"""A relative POSIX path whose components are not dot-files."""

ENV_NAME: Final = re.compile(r"^[A-Z][A-Z0-9_]*$")
"""The environment-variable name grammar used by the serving baseline."""

_SECRET_ENV_TOKENS: Final = frozenset(
    {"PASSWORD", "PASSWD", "SECRET", "TOKEN", "KEY", "CREDENTIAL", "CREDENTIALS", "PRIVATE"}
)


def _path(prefix: str, key: object) -> str:
    rendered = str(key)
    return f"{prefix}.{rendered}" if prefix else rendered


def _nearest(path: str, valid: Sequence[str]) -> str:
    return difflib.get_close_matches(path, list(valid), n=1, cutoff=0.0)[0]


def _unknown(path: str, valid: Sequence[str], *, fix: str = _FIX) -> ModelsLockError:
    return ModelsLockError(
        key_path=path,
        problem=(
            f"Unknown key '{path}'; nearest valid key is '{_nearest(path, valid)}'."
        ),
        fix=fix,
    )


def _walk_unknown(
    value: object,
    keys: Sequence[str],
    prefix: str,
    errors: list[ModelsLockError],
    *,
    fix: str = _FIX,
) -> None:
    """Append unknown-key errors for one fixed mapping level."""

    if not isinstance(value, Mapping):
        return
    for key in value:
        if not isinstance(key, str) or key not in keys:
            errors.append(_unknown(_path(prefix, key), keys, fix=fix))


def _lookup(document: Mapping[str, object], path: str) -> tuple[bool, object]:
    current: object = document
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return False, None
        current = current[segment]
    return True, current


def _error(path: str, detail: str, *, fix: str = _FIX) -> ModelsLockError:
    return ModelsLockError(
        key_path=path,
        problem=f"Invalid value for '{path}': {detail}.",
        fix=fix,
    )


def _mapping_error(path: str, value: object, *, fix: str = _FIX) -> ModelsLockError:
    return _error(path, f"expected a mapping (got {value!r})", fix=fix)


def _string(
    document: Mapping[str, object],
    path: str,
    errors: list[ModelsLockError],
    *,
    error_path: str | None = None,
    fix: str = _FIX,
) -> str | None:
    reported = path if error_path is None else error_path
    present, value = _lookup(document, path)
    if not present:
        errors.append(_error(reported, "missing required key", fix=fix))
        return None
    if not isinstance(value, str) or not value:
        errors.append(_error(reported, f"expected a non-empty string (got {value!r})", fix=fix))
        return None
    return value


def _int(
    document: Mapping[str, object],
    path: str,
    errors: list[ModelsLockError],
    *,
    positive: bool = True,
    error_path: str | None = None,
    fix: str = _FIX,
) -> int | None:
    reported = path if error_path is None else error_path
    present, value = _lookup(document, path)
    if not present:
        errors.append(_error(reported, "missing required key", fix=fix))
        return None
    minimum = 1 if positive else 0
    description = "a positive integer" if positive else "a non-negative integer"
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        errors.append(_error(reported, f"expected {description} (got {value!r})", fix=fix))
        return None
    return value


def _check_mapping(
    document: Mapping[str, object],
    path: str,
    errors: list[ModelsLockError],
    *,
    error_path: str | None = None,
) -> Mapping[str, object] | None:
    reported = path if error_path is None else error_path
    present, value = _lookup(document, path)
    if not present:
        errors.append(_error(reported, "missing required key"))
        return None
    if not isinstance(value, Mapping):
        errors.append(_mapping_error(reported, value))
        return None
    return cast(Mapping[str, object], value)


def _validate_env(
    serve_path: str,
    value: object,
    errors: list[ModelsLockError],
) -> dict[str, str]:
    path = f"{serve_path}.env"
    if not isinstance(value, Mapping):
        errors.append(_mapping_error(path, value))
        return {}
    env: dict[str, str] = {}
    for key, item in value.items():
        item_path = _path(path, key)
        valid_name = isinstance(key, str) and ENV_NAME.fullmatch(key) is not None
        if not valid_name:
            errors.append(_error(item_path, "expected an environment name matching [A-Z][A-Z0-9_]*"))
        elif _SECRET_ENV_TOKENS & set(key.split("_")):
            errors.append(_error(item_path, "an environment name must never be secret-like"))
        if not isinstance(item, str) or not item:
            errors.append(_error(item_path, f"expected a non-empty single-line string (got {item!r})"))
        elif "\n" in item or "\r" in item:
            errors.append(_error(item_path, "expected a single-line string"))
        if valid_name and isinstance(item, str) and item and "\n" not in item and "\r" not in item:
            env[key] = item
    return env


def _validate_flags(
    serve_path: str,
    value: object,
    errors: list[ModelsLockError],
) -> dict[str, str | int | float | bool]:
    path = f"{serve_path}.flags"
    if not isinstance(value, Mapping):
        errors.append(_mapping_error(path, value))
        return {}
    flags: dict[str, str | int | float | bool] = {}
    scalar_types = (str, int, float, bool)
    for key, item in value.items():
        item_path = _path(path, key)
        valid_name = isinstance(key, str) and FLAG_NAME.fullmatch(key) is not None
        if not valid_name:
            errors.append(_error(item_path, "expected a lowercase hyphenated flag name without '--'"))
        if not isinstance(item, scalar_types):
            errors.append(_error(item_path, f"expected a scalar flag value (got {item!r})"))
        elif valid_name:
            flags[key] = item
    return flags


def _validate_files(
    model_path: str,
    value: object,
    errors: list[ModelsLockError],
) -> dict[str, PinnedFile]:
    path = f"{model_path}.files"
    if not isinstance(value, Mapping):
        errors.append(_mapping_error(path, value))
        return {}
    if not value:
        errors.append(_error(path, "expected a non-empty mapping"))
        return {}
    paths = [key for key in value if isinstance(key, str)]
    if paths != sorted(paths):
        errors.append(_error(path, "file entries must be in sorted path order"))
    files: dict[str, PinnedFile] = {}
    for file_path, item in value.items():
        entry_path = _path(path, file_path)
        if not isinstance(file_path, str) or FILE_PATH.fullmatch(file_path) is None:
            errors.append(_error(entry_path, "expected a relative POSIX file path without dot-files"))
        if not isinstance(item, Mapping):
            errors.append(_mapping_error(entry_path, item))
            continue
        entry = cast(Mapping[str, object], item)
        _walk_unknown(entry, _FILE_KEYS, entry_path, errors)
        sha256 = _string(entry, "sha256", errors, error_path=f"{entry_path}.sha256")
        if sha256 is not None and DIGEST.fullmatch(sha256) is None:
            errors.append(_error(f"{entry_path}.sha256", "expected sha256:<64 lowercase hexadecimal characters"))
        size = _int(entry, "size", errors, positive=False, error_path=f"{entry_path}.size")
        if (
            isinstance(file_path, str)
            and FILE_PATH.fullmatch(file_path) is not None
            and sha256 is not None
            and DIGEST.fullmatch(sha256) is not None
            and size is not None
        ):
            files[file_path] = PinnedFile(file_path, sha256, size)
    return files


def _validate_model(
    profile_path: str,
    role: object,
    value: object,
    gpu_count: int | None,
    errors: list[ModelsLockError],
) -> ModelPin | None:
    model_path = _path(f"{profile_path}.models", role)
    role_valid = isinstance(role, str) and ROLE_NAME.fullmatch(role) is not None
    if not role_valid:
        errors.append(_error(model_path, "expected a lowercase hyphenated role name"))
    if not isinstance(value, Mapping):
        errors.append(_mapping_error(model_path, value))
        return None
    model = cast(Mapping[str, object], value)
    _walk_unknown(model, _MODEL_KEYS, model_path, errors)
    repo = _string(model, "repo", errors, error_path=f"{model_path}.repo")
    if repo is not None and HF_REPO.fullmatch(repo) is None:
        errors.append(_error(f"{model_path}.repo", f"expected an owner/name repository (got {repo!r})"))
    revision = _string(model, "revision", errors, error_path=f"{model_path}.revision")
    if revision is not None and REVISION.fullmatch(revision) is None:
        errors.append(_error(f"{model_path}.revision", "expected a 40-character lowercase hexadecimal revision"))
    gpu = _int(model, "gpu", errors, positive=False, error_path=f"{model_path}.gpu")
    if gpu is not None and gpu_count is not None and gpu >= gpu_count:
        errors.append(_error(f"{model_path}.gpu", f"must be below requires.gpu.count ({gpu_count})"))

    serve = _check_mapping(model, "serve", errors, error_path=f"{model_path}.serve")
    serve_baseline: ServeBaseline | None = None
    if serve is not None:
        serve_path = f"{model_path}.serve"
        _walk_unknown(serve, _SERVE_KEYS, serve_path, errors)
        served_name = _string(serve, "served_name", errors, error_path=f"{serve_path}.served_name")
        env = _validate_env(serve_path, serve.get("env"), errors) if "env" in serve else {}
        if "env" not in serve:
            errors.append(_error(f"{serve_path}.env", "missing required key"))
        flags = _validate_flags(serve_path, serve.get("flags"), errors) if "flags" in serve else {}
        if "flags" not in serve:
            errors.append(_error(f"{serve_path}.flags", "missing required key"))
        if served_name is not None and "env" in serve and "flags" in serve:
            serve_baseline = ServeBaseline(served_name, env, flags)

    files = _validate_files(model_path, model.get("files"), errors) if "files" in model else {}
    if "files" not in model:
        errors.append(_error(f"{model_path}.files", "missing required key"))
    if (
        role_valid
        and repo is not None
        and HF_REPO.fullmatch(repo) is not None
        and revision is not None
        and REVISION.fullmatch(revision) is not None
        and gpu is not None
        and serve_baseline is not None
        and files
    ):
        return ModelPin(cast(str, role), repo, revision, gpu, serve_baseline, tuple(files.values()))
    return None


def _validate_memory(
    profile_path: str,
    value: object,
    roles: Sequence[str],
    errors: list[ModelsLockError],
) -> dict[str, MemoryRow]:
    """Validate and collect one profile's service memory table."""

    path = f"{profile_path}.memory"
    if not isinstance(value, Mapping):
        errors.append(_mapping_error(path, value, fix=_MEMORY_FIX))
        return {}
    if not value:
        errors.append(_error(path, "expected a non-empty mapping", fix=_MEMORY_FIX))
        return {}

    memory: dict[str, MemoryRow] = {}
    role_names = ", ".join(roles) or "none"
    for service, row_value in value.items():
        row_path = _path(path, service)
        service_valid = (
            isinstance(service, str) and SERVICE_NAME.fullmatch(service) is not None
        )
        if not service_valid:
            errors.append(
                _error(
                    row_path,
                    "expected a lowercase hyphenated service name",
                    fix=_MEMORY_FIX,
                )
            )
        if not isinstance(row_value, Mapping):
            errors.append(_mapping_error(row_path, row_value, fix=_MEMORY_FIX))
            continue

        row = cast(Mapping[str, object], row_value)
        _walk_unknown(row, _MEMORY_ROW_KEYS, row_path, errors, fix=_MEMORY_FIX)
        gb = _int(
            row,
            "gb",
            errors,
            error_path=f"{row_path}.gb",
            fix=_MEMORY_FIX,
        )
        role: str | None = None
        role_valid = True
        if "role" in row:
            role = _string(
                row,
                "role",
                errors,
                error_path=f"{row_path}.role",
                fix=_MEMORY_FIX,
            )
            role_valid = role is not None and ROLE_NAME.fullmatch(role) is not None
            if role is not None and not role_valid:
                errors.append(
                    _error(
                        f"{row_path}.role",
                        "expected a lowercase hyphenated role name",
                        fix=_MEMORY_FIX,
                    )
                )
            elif role is not None and role not in roles:
                errors.append(
                    _error(
                        f"{row_path}.role",
                        f"role '{role}' is not pinned by this profile (available roles: {role_names})",
                        fix=_MEMORY_FIX,
                    )
                )

        if (
            service_valid
            and gb is not None
            and ("role" not in row or (role is not None and role_valid and role in roles))
        ):
            memory[cast(str, service)] = MemoryRow(cast(str, service), gb, role)
    return memory


def validate_models_lock(document: Mapping[str, object]) -> list[ModelsLockError]:
    """Return all shape, grammar, pin, and unknown-key errors in a lock."""

    errors: list[ModelsLockError] = []
    _walk_unknown(document, _ROOT_KEYS, "", errors)

    present, version = _lookup(document, "version")
    if not present:
        errors.append(_error("version", "missing required key"))
    elif not isinstance(version, int) or isinstance(version, bool) or version != 1:
        errors.append(_error("version", f"expected integer 1 (got {version!r})"))

    reference = _string(document, "reference", errors)
    profiles_value = _check_mapping(document, "profiles", errors)
    if profiles_value is not None and not profiles_value:
        errors.append(_error("profiles", "expected a non-empty mapping"))

    if profiles_value is not None:
        for name, profile_value in profiles_value.items():
            profile_path = _path("profiles", name)
            name_valid = isinstance(name, str) and PROFILE_NAME.fullmatch(name) is not None
            if not name_valid:
                errors.append(_error(profile_path, "expected a hardware profile name such as <count>x<per-GPU-vram>v-<dram>d or <n>u"))
            if not isinstance(profile_value, Mapping):
                errors.append(_mapping_error(profile_path, profile_value))
                continue
            profile = cast(Mapping[str, object], profile_value)
            _walk_unknown(profile, _PROFILE_KEYS, profile_path, errors)
            requires = _check_mapping(profile, "requires", errors, error_path=f"{profile_path}.requires")
            gpu_count: int | None = None
            if requires is not None:
                requires_path = f"{profile_path}.requires"
                _walk_unknown(requires, _REQUIRES_KEYS, requires_path, errors)
                platform = _string(requires, "platform", errors, error_path=f"{requires_path}.platform")
                if platform is not None and PLATFORM.fullmatch(platform) is None:
                    errors.append(_error(f"{requires_path}.platform", "expected a machine name as uname -m prints it"))
                gpu = _check_mapping(requires, "gpu", errors, error_path=f"{requires_path}.gpu")
                _int(requires, "dram_gb", errors, error_path=f"{requires_path}.dram_gb")
                _int(requires, "data_volume_gb", errors, error_path=f"{requires_path}.data_volume_gb")
                if gpu is not None:
                    _walk_unknown(gpu, _GPU_KEYS, f"{requires_path}.gpu", errors)
                    _string(gpu, "architecture", errors, error_path=f"{requires_path}.gpu.architecture")
                    capability = _string(gpu, "compute_capability", errors, error_path=f"{requires_path}.gpu.compute_capability")
                    _string(gpu, "model", errors, error_path=f"{requires_path}.gpu.model")
                    gpu_count = _int(gpu, "count", errors, error_path=f"{requires_path}.gpu.count")
                    _int(gpu, "vram_gb", errors, error_path=f"{requires_path}.gpu.vram_gb")
                    if capability is not None and COMPUTE_CAPABILITY.fullmatch(capability) is None:
                        errors.append(_error(f"{requires_path}.gpu.compute_capability", "expected a quoted major.minor string"))

            models = _check_mapping(profile, "models", errors, error_path=f"{profile_path}.models")
            if models is not None:
                if not models:
                    errors.append(_error(f"{profile_path}.models", "expected a non-empty mapping"))
                if "generator" not in models:
                    errors.append(_error(f"{profile_path}.models.generator", "missing required key"))
                for role, model_value in models.items():
                    _validate_model(profile_path, role, model_value, gpu_count, errors)

            if "memory" not in profile:
                errors.append(_error(f"{profile_path}.memory", "missing required key", fix=_MEMORY_FIX))
            else:
                roles = tuple(role for role in models or {} if isinstance(role, str))
                _validate_memory(profile_path, profile["memory"], roles, errors)

    if reference is not None and profiles_value is not None and reference not in profiles_value:
        errors.append(_error("reference", f"must name a profile held by models.lock (got {reference!r})"))
    return errors


def _construct(document: Mapping[str, object]) -> ModelsLock:
    profiles_value = cast(Mapping[str, object], document["profiles"])
    profiles: list[HardwareProfile] = []
    for name, profile_value in profiles_value.items():
        profile = cast(Mapping[str, object], profile_value)
        requires_value = cast(Mapping[str, object], profile["requires"])
        gpu_value = cast(Mapping[str, object], requires_value["gpu"])
        requirements = ProfileRequirements(
            platform=cast(str, requires_value["platform"]),
            gpu=GpuRequirements(
                architecture=cast(str, gpu_value["architecture"]),
                compute_capability=cast(str, gpu_value["compute_capability"]),
                model=cast(str, gpu_value["model"]),
                count=cast(int, gpu_value["count"]),
                vram_gb=cast(int, gpu_value["vram_gb"]),
            ),
            dram_gb=cast(int, requires_value["dram_gb"]),
            data_volume_gb=cast(int, requires_value["data_volume_gb"]),
        )
        models_value = cast(Mapping[str, object], profile["models"])
        models = tuple(
            _construct_model(cast(str, role), cast(Mapping[str, object], value))
            for role, value in models_value.items()
        )
        memory_value = cast(Mapping[str, object], profile["memory"])
        memory = tuple(
            MemoryRow(
                service=cast(str, service),
                gb=cast(int, cast(Mapping[str, object], value)["gb"]),
                role=cast(str | None, cast(Mapping[str, object], value).get("role")),
            )
            for service, value in memory_value.items()
        )
        profiles.append(HardwareProfile(cast(str, name), requirements, memory, models))
    return ModelsLock(cast(int, document["version"]), cast(str, document["reference"]), tuple(profiles))


def _construct_model(role: str, document: Mapping[str, object]) -> ModelPin:
    serve = cast(Mapping[str, object], document["serve"])
    files = cast(Mapping[str, object], document["files"])
    return ModelPin(
        role=role,
        repo=cast(str, document["repo"]),
        revision=cast(str, document["revision"]),
        gpu=cast(int, document["gpu"]),
        serve=ServeBaseline(
            served_name=cast(str, serve["served_name"]),
            env=cast(Mapping[str, str], serve["env"]),
            flags=cast(Mapping[str, str | int | float | bool], serve["flags"]),
        ),
        files=tuple(
            PinnedFile(
                path=cast(str, path),
                sha256=cast(str, cast(Mapping[str, object], value)["sha256"]),
                size=cast(int, cast(Mapping[str, object], value)["size"]),
            )
            for path, value in files.items()
        ),
    )


def render_errors(errors: Sequence[ModelsLockError]) -> str:
    """Render one refusal per line, with each corrective action last."""

    return "\n".join(
        f"{' '.join(error.problem.splitlines())} Fix: {error.fix}" for error in errors
    )


def read_models_lock(path: PathLike, *, host: Host | None = None) -> ModelsLockLoadResult:
    """Read and parse a models lock without constructing its dataclasses."""

    io = host or RealHost()
    try:
        text = io.read_text(path)
    except FileNotFoundError:
        return ModelsLockLoadResult(errors=(ModelsLockError(None, f"models lock is missing: {path}", _FIX),))
    except PermissionError:
        return ModelsLockLoadResult(errors=(ModelsLockError(None, f"models lock is unreadable due to permissions: {path}", _FIX),))
    except UnicodeDecodeError:
        return ModelsLockLoadResult(errors=(ModelsLockError(None, f"models lock is not valid UTF-8: {path}", _FIX),))
    except OSError as exc:
        return ModelsLockLoadResult(errors=(ModelsLockError(None, f"models lock is unreadable: {path} ({exc})", _FIX),))
    return parse_models_lock(text)


def parse_models_lock(text: str) -> ModelsLockLoadResult:
    """Parse lock text into its YAML document, without validation."""

    try:
        document = yaml.load(text, Loader=ModelsLockLoader)
    except _DuplicateKeyError as exc:
        return ModelsLockLoadResult(errors=(ModelsLockError(None, f"models lock has a {exc}", _FIX),))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = getattr(mark, "line", None)
        location = f" at line {line + 1}" if isinstance(line, int) else ""
        return ModelsLockLoadResult(errors=(ModelsLockError(None, f"models lock has a YAML parse error{location}: {exc}", _FIX),))
    if document is None:
        return ModelsLockLoadResult(errors=(ModelsLockError(None, "models lock is empty", _FIX),))
    if not isinstance(document, Mapping):
        return ModelsLockLoadResult(errors=(ModelsLockError(None, "models lock root must be a mapping", _FIX),))
    return ModelsLockLoadResult(document=cast(Mapping[str, object], document))


def _finish(result: ModelsLockLoadResult) -> ModelsLockLoadResult:
    if result.errors or result.document is None:
        return result
    errors = validate_models_lock(result.document)
    if errors:
        return ModelsLockLoadResult(document=result.document, errors=tuple(errors))
    return ModelsLockLoadResult(lock=_construct(result.document), document=result.document)


def load_models_lock(path: PathLike, *, host: Host | None = None) -> ModelsLockLoadResult:
    """Parse, validate, and construct a models lock from a path."""

    return _finish(read_models_lock(path, host=host))


def load_models_lock_text(text: str) -> ModelsLockLoadResult:
    """Parse, validate, and construct models-lock text already in hand."""

    return _finish(parse_models_lock(text))


def select_profile(lock: ModelsLock, name: str) -> HardwareProfile | Problem:
    """Select *name*, or return the shared problem-and-fix refusal shape."""

    profile = lock.profile(name)
    if profile is not None:
        return profile
    available = ", ".join(profile.name for profile in lock.profiles) or "none"
    return Problem(
        problem=f"Hardware profile '{name}' is not in models.lock; profiles held: {available}.",
        fix=(
            "Set hardware_profile in /etc/gideon/site.yaml to one of "
            f"{available}, then re-run the command."
        ),
    )
