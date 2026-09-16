"""Loader and validator for the repository's host provisioning lock."""

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

import yaml  # type: ignore[import-untyped]

from gideon.host.sysio import Host, PathLike, RealHost


@dataclass(frozen=True, slots=True)
class DriverPin:
    package: str
    branch: str
    repo: str
    keyring_deb: str
    keyring_sha256: str
    tested: str | None


@dataclass(frozen=True, slots=True)
class Minimums:
    docker: int
    compose: int
    toolkit: str


@dataclass(frozen=True, slots=True)
class GitHubRunnerPin:
    version: str
    sha256: str


@dataclass(frozen=True, slots=True)
class AcceptanceVmImage:
    url: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ControllerFacts:
    model: str
    firmware: str
    personality: str
    slot: int
    psoc: str
    serial: str


@dataclass(frozen=True, slots=True)
class VirtualDiskFacts:
    name: str
    role: str
    raid_level: str
    size_tib: float
    linux_device: str
    wwn: str


@dataclass(frozen=True, slots=True)
class ReferenceHost:
    controller: ControllerFacts
    vd_layout: tuple[VirtualDiskFacts, ...]


@dataclass(frozen=True, slots=True)
class HostLock:
    os_lts: str
    driver: DriverPin
    minimums: Minimums
    registry_image: str
    gh_runner: GitHubRunnerPin
    acceptance_vm_image: AcceptanceVmImage
    reference_host: ReferenceHost
    kernel_tested: str | None = None


@dataclass(frozen=True, slots=True)
class LockError:
    """A structured refusal from lock loading or validation."""

    key_path: str | None
    problem: str
    fix: str


@dataclass(frozen=True, slots=True)
class HostLockLoadResult:
    """The parsed lock, or every error found while loading it."""

    lock: HostLock | None = None
    document: Mapping[str, object] | None = None
    errors: tuple[LockError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors and self.lock is not None


class _DuplicateKeyError(yaml.YAMLError):
    pass


class LockLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate keys."""


LockLoader.yaml_implicit_resolvers = {
    initial: list(resolvers)
    for initial, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_mapping_without_duplicates(
    loader: LockLoader, node: Any, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            line = key_node.start_mark.line + 1
            raise _DuplicateKeyError(f"duplicate mapping key {key!r} at line {line}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


LockLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_without_duplicates,
)


_FIX: Final = "Edit host.lock; consult docs/archi/host.md."
_ROOT_KEYS: Final = (
    "os_lts",
    "kernel_tested",
    "driver",
    "minimums",
    "registry_image",
    "gh_runner",
    "acceptance_vm_image",
    "reference_host",
)
_SECTIONS: Final = {
    "driver": ("package", "branch", "repo", "keyring_deb", "keyring_sha256", "tested"),
    "minimums": ("docker", "compose", "toolkit"),
    "gh_runner": ("version", "sha256"),
    "acceptance_vm_image": ("url", "sha256"),
    "reference_host": ("controller", "vd_layout"),
    "reference_host.controller": (
        "model",
        "firmware",
        "personality",
        "slot",
        "psoc",
        "serial",
    ),
    "reference_host.vd_layout": (
        "name",
        "role",
        "raid_level",
        "size_tib",
        "linux_device",
        "wwn",
    ),
}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
VERSION: Final = re.compile(r"^\d+(?:\.\d+){1,3}$")
IMAGE_REFERENCE: Final = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")


def _path(prefix: str, key: object) -> str:
    rendered = str(key)
    return f"{prefix}.{rendered}" if prefix else rendered


def _nearest(path: str, valid: Sequence[str]) -> str:
    return difflib.get_close_matches(path, list(valid), n=1, cutoff=0.0)[0]


def _unknown(path: str, valid: Sequence[str]) -> LockError:
    return LockError(
        key_path=path,
        problem=(
            f"Unknown key '{path}'; nearest valid key is '{_nearest(path, valid)}'."
        ),
        fix=_FIX,
    )


def _walk_unknown(
    value: object,
    keys: Sequence[str],
    prefix: str,
    errors: list[LockError],
) -> None:
    if not isinstance(value, Mapping):
        return
    for key, child in value.items():
        path = _path(prefix, key)
        if not isinstance(key, str) or key not in keys:
            errors.append(_unknown(path, keys))
            continue
        child_keys = _SECTIONS.get(path)
        if child_keys is not None:
            _walk_unknown(child, child_keys, path, errors)


def _lookup(document: Mapping[str, object], path: str) -> tuple[bool, object]:
    current: object = document
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return False, None
        current = current[segment]
    return True, current


def _error(path: str, detail: str) -> LockError:
    return LockError(
        key_path=path,
        problem=f"Invalid value for '{path}': {detail}.",
        fix=_FIX,
    )


def _mapping_error(path: str, value: object) -> LockError:
    return _error(path, f"expected a mapping (got {value!r})")


def _string(
    document: Mapping[str, object],
    path: str,
    errors: list[LockError],
    *,
    non_empty: bool = True,
    error_path: str | None = None,
) -> str | None:
    rendered_path = error_path or path
    present, value = _lookup(document, path)
    if not present:
        errors.append(_error(rendered_path, "missing required key"))
        return None
    if not isinstance(value, str) or (non_empty and not value):
        expected = "a non-empty string" if non_empty else "a string"
        errors.append(_error(rendered_path, f"expected {expected} (got {value!r})"))
        return None
    return value


def _int(
    document: Mapping[str, object],
    path: str,
    errors: list[LockError],
    *,
    error_path: str | None = None,
) -> int | None:
    rendered_path = error_path or path
    present, value = _lookup(document, path)
    if not present:
        errors.append(_error(rendered_path, "missing required key"))
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        errors.append(
            _error(rendered_path, f"expected a positive integer (got {value!r})")
        )
        return None
    return value


def _sha256(
    document: Mapping[str, object],
    path: str,
    errors: list[LockError],
    *,
    error_path: str | None = None,
) -> str | None:
    rendered_path = error_path or path
    value = _string(document, path, errors, error_path=rendered_path)
    if value is not None and not _HEX64.fullmatch(value):
        errors.append(
            _error(rendered_path, "expected 64 lowercase hexadecimal characters")
        )
        return None
    return value


def _check_mapping(
    document: Mapping[str, object],
    path: str,
    errors: list[LockError],
    *,
    error_path: str | None = None,
) -> Mapping[str, object] | None:
    rendered_path = error_path or path
    present, value = _lookup(document, path)
    if not present:
        errors.append(_error(rendered_path, "missing required key"))
        return None
    if not isinstance(value, Mapping):
        errors.append(_mapping_error(rendered_path, value))
        return None
    return cast(Mapping[str, object], value)


def validate_host_lock(document: Mapping[str, object]) -> list[LockError]:
    """Return all shape, pin, and unknown-key errors in a lock document."""

    errors: list[LockError] = []
    _walk_unknown(document, _ROOT_KEYS, "", errors)

    _string(document, "os_lts", errors)
    present, kernel_tested = _lookup(document, "kernel_tested")
    if present and kernel_tested is not None and (
        not isinstance(kernel_tested, str) or not kernel_tested
    ):
        errors.append(
            _error(
                "kernel_tested",
                f"expected a version or null (got {kernel_tested!r})",
            )
        )
    driver = _check_mapping(document, "driver", errors)
    minimums = _check_mapping(document, "minimums", errors)
    registry_image = _string(document, "registry_image", errors)
    gh_runner = _check_mapping(document, "gh_runner", errors)
    acceptance = _check_mapping(document, "acceptance_vm_image", errors)
    reference = _check_mapping(document, "reference_host", errors)

    if driver is not None:
        package = _string(driver, "package", errors, error_path="driver.package")
        branch = _string(driver, "branch", errors, error_path="driver.branch")
        repo = _string(driver, "repo", errors, error_path="driver.repo")
        _string(driver, "keyring_deb", errors, error_path="driver.keyring_deb")
        _sha256(driver, "keyring_sha256", errors, error_path="driver.keyring_sha256")
        present, tested = _lookup(driver, "tested")
        if not present:
            errors.append(_error("driver.tested", "missing required key"))
        elif tested is not None and (not isinstance(tested, str) or not tested):
            errors.append(
                _error("driver.tested", f"expected a version or null (got {tested!r})")
            )
        if branch is not None and not branch.isdigit():
            errors.append(
                _error("driver.branch", f"expected a numeric branch (got {branch!r})")
            )
        if package is not None and package != "nvidia-open":
            errors.append(_error("driver.package", "must be 'nvidia-open'"))
        if repo is not None and not re.fullmatch(r"ubuntu\d{4}/x86_64", repo):
            errors.append(
                _error(
                    "driver.repo",
                    f"expected an Ubuntu x86-64 repository path (got {repo!r})",
                )
            )

    if minimums is not None:
        _int(minimums, "docker", errors, error_path="minimums.docker")
        _int(minimums, "compose", errors, error_path="minimums.compose")
        toolkit = _string(minimums, "toolkit", errors, error_path="minimums.toolkit")
        if toolkit is not None and not VERSION.fullmatch(toolkit):
            errors.append(
                _error("minimums.toolkit", f"expected a version (got {toolkit!r})")
            )

    if registry_image is not None and not IMAGE_REFERENCE.fullmatch(registry_image):
        errors.append(
            _error(
                "registry_image",
                "expected an image name followed by @sha256:<64 hex characters>",
            )
        )

    if gh_runner is not None:
        version = _string(gh_runner, "version", errors, error_path="gh_runner.version")
        _sha256(gh_runner, "sha256", errors, error_path="gh_runner.sha256")
        if version is not None and not VERSION.fullmatch(version):
            errors.append(
                _error("gh_runner.version", f"expected a version (got {version!r})")
            )

    if acceptance is not None:
        url = _string(
            acceptance,
            "url",
            errors,
            error_path="acceptance_vm_image.url",
        )
        _sha256(
            acceptance,
            "sha256",
            errors,
            error_path="acceptance_vm_image.sha256",
        )
        if url is not None and not re.fullmatch(r"https?://[^\s]+", url):
            errors.append(
                _error(
                    "acceptance_vm_image.url", f"expected an HTTP(S) URL (got {url!r})"
                )
            )

    if reference is not None:
        controller = _check_mapping(
            reference,
            "controller",
            errors,
            error_path="reference_host.controller",
        )
        vd_layout_present, vd_layout = _lookup(reference, "vd_layout")
        if not vd_layout_present:
            errors.append(_error("reference_host.vd_layout", "missing required key"))
        elif not isinstance(vd_layout, list) or not vd_layout:
            errors.append(
                _error(
                    "reference_host.vd_layout",
                    f"expected a non-empty list (got {vd_layout!r})",
                )
            )
        if controller is not None:
            for path in ("model", "firmware", "personality", "psoc", "serial"):
                _string(
                    controller,
                    path,
                    errors,
                    error_path=f"reference_host.controller.{path}",
                )
            _int(
                controller,
                "slot",
                errors,
                error_path="reference_host.controller.slot",
            )
        if isinstance(vd_layout, list):
            for index, item in enumerate(vd_layout):
                item_path = f"reference_host.vd_layout[{index}]"
                if not isinstance(item, Mapping):
                    errors.append(_mapping_error(item_path, item))
                    continue
                item_mapping = cast(Mapping[str, object], item)
                _walk_unknown(
                    item_mapping,
                    _SECTIONS["reference_host.vd_layout"],
                    item_path,
                    errors,
                )
                for path in ("name", "role", "raid_level", "linux_device", "wwn"):
                    _string(
                        item_mapping,
                        path,
                        errors,
                        error_path=f"{item_path}.{path}",
                    )
                present, size = _lookup(item_mapping, "size_tib")
                if not present:
                    errors.append(
                        _error(f"{item_path}.size_tib", "missing required key")
                    )
                elif (
                    not isinstance(size, (int, float))
                    or isinstance(size, bool)
                    or size <= 0
                ):
                    errors.append(
                        _error(
                            f"{item_path}.size_tib",
                            f"expected a positive number (got {size!r})",
                        )
                    )

    return errors


def _construct(document: Mapping[str, object]) -> HostLock:
    driver = cast(Mapping[str, object], document["driver"])
    minimums = cast(Mapping[str, object], document["minimums"])
    gh_runner = cast(Mapping[str, object], document["gh_runner"])
    acceptance = cast(Mapping[str, object], document["acceptance_vm_image"])
    reference = cast(Mapping[str, object], document["reference_host"])
    controller = cast(Mapping[str, object], reference["controller"])
    disks = cast(list[Mapping[str, object]], reference["vd_layout"])
    return HostLock(
        os_lts=cast(str, document["os_lts"]),
        driver=DriverPin(
            package=cast(str, driver["package"]),
            branch=cast(str, driver["branch"]),
            repo=cast(str, driver["repo"]),
            keyring_deb=cast(str, driver["keyring_deb"]),
            keyring_sha256=cast(str, driver["keyring_sha256"]),
            tested=cast(str | None, driver["tested"]),
        ),
        minimums=Minimums(
            docker=cast(int, minimums["docker"]),
            compose=cast(int, minimums["compose"]),
            toolkit=cast(str, minimums["toolkit"]),
        ),
        registry_image=cast(str, document["registry_image"]),
        gh_runner=GitHubRunnerPin(
            version=cast(str, gh_runner["version"]),
            sha256=cast(str, gh_runner["sha256"]),
        ),
        acceptance_vm_image=AcceptanceVmImage(
            url=cast(str, acceptance["url"]),
            sha256=cast(str, acceptance["sha256"]),
        ),
        reference_host=ReferenceHost(
            controller=ControllerFacts(
                model=cast(str, controller["model"]),
                firmware=cast(str, controller["firmware"]),
                personality=cast(str, controller["personality"]),
                slot=cast(int, controller["slot"]),
                psoc=cast(str, controller["psoc"]),
                serial=cast(str, controller["serial"]),
            ),
            vd_layout=tuple(
                VirtualDiskFacts(
                    name=cast(str, disk["name"]),
                    role=cast(str, disk["role"]),
                    raid_level=cast(str, disk["raid_level"]),
                    size_tib=cast(float, disk["size_tib"]),
                    linux_device=cast(str, disk["linux_device"]),
                    wwn=cast(str, disk["wwn"]),
                )
                for disk in disks
            ),
        ),
        kernel_tested=cast(str | None, document.get("kernel_tested")),
    )


def render_errors(errors: Sequence[LockError]) -> str:
    """Render one refusal per line, with its corrective action last."""

    return "\n".join(
        f"{' '.join(error.problem.splitlines())} Fix: {error.fix}" for error in errors
    )


def read_host_lock(path: PathLike, *, host: Host | None = None) -> HostLockLoadResult:
    """Read and parse a lock file without constructing its dataclasses."""

    io = host or RealHost()
    try:
        text = io.read_text(path)
    except FileNotFoundError:
        return HostLockLoadResult(
            errors=(LockError(None, f"host lock is missing: {path}", _FIX),)
        )
    except PermissionError:
        return HostLockLoadResult(
            errors=(
                LockError(
                    None, f"host lock is unreadable due to permissions: {path}", _FIX
                ),
            )
        )
    except UnicodeDecodeError:
        return HostLockLoadResult(
            errors=(LockError(None, f"host lock is not valid UTF-8: {path}", _FIX),)
        )
    except OSError as exc:
        return HostLockLoadResult(
            errors=(LockError(None, f"host lock is unreadable: {path} ({exc})", _FIX),)
        )
    return parse_host_lock(text)


def parse_host_lock(text: str) -> HostLockLoadResult:
    """Parse lock text into its document (no validation, no dataclasses)."""

    try:
        document = yaml.load(text, Loader=LockLoader)
    except _DuplicateKeyError as exc:
        return HostLockLoadResult(
            errors=(LockError(None, f"host lock has a {exc}", _FIX),)
        )
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = getattr(mark, "line", None)
        location = f" at line {line + 1}" if isinstance(line, int) else ""
        return HostLockLoadResult(
            errors=(
                LockError(
                    None, f"host lock has a YAML parse error{location}: {exc}", _FIX
                ),
            )
        )
    if document is None:
        return HostLockLoadResult(errors=(LockError(None, "host lock is empty", _FIX),))
    if not isinstance(document, Mapping):
        return HostLockLoadResult(
            errors=(LockError(None, "host lock root must be a mapping", _FIX),)
        )
    return HostLockLoadResult(document=cast(Mapping[str, object], document))


def _finish(result: HostLockLoadResult) -> HostLockLoadResult:
    if result.errors or result.document is None:
        return result
    errors = validate_host_lock(result.document)
    if errors:
        return HostLockLoadResult(document=result.document, errors=tuple(errors))
    return HostLockLoadResult(
        lock=_construct(result.document),
        document=result.document,
    )


def load_host_lock(path: PathLike, *, host: Host | None = None) -> HostLockLoadResult:
    """Parse, validate, and construct a host lock."""

    return _finish(read_host_lock(path, host=host))


def load_host_lock_text(text: str) -> HostLockLoadResult:
    """Parse, validate, and construct a host lock from text already in hand.

    The pin watch loads ``origin/main``'s lock this way, so a bump is always
    patched onto the tip it will be proposed against.
    """

    return _finish(parse_host_lock(text))
