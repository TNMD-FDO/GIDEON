"""Image-lock loading and registry reference helpers for host operations.

``images.lock`` has two timeless pin kinds: mirrored pins track an upstream
``source`` and digest, while built pins track a relative build directory, a
mirrored base, build inputs, and the digest of the image produced from those
inputs.
"""

import difflib
import hashlib
import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast
from urllib.parse import quote, urlsplit, urlunsplit

import yaml  # type: ignore[import-untyped]

from gideon.host.site import SiteConfig
from gideon.host.sysio import Host, PathLike, RealHost


@dataclass(frozen=True, slots=True)
class ImagePin:
    """The common name and produced-image digest for one lock pin."""

    name: str
    digest: str


@dataclass(frozen=True, slots=True)
class MirroredImagePin(ImagePin):
    """An upstream image copied into the release registry."""

    source: str


@dataclass(frozen=True, slots=True)
class AptWatch:
    """An apt index and package watched for a built image input."""

    apt_index: str
    package: str


@dataclass(frozen=True, slots=True)
class PypiWatch:
    """A normalized PyPI project watched for a built image input."""

    project: str


type WatchEntry = AptWatch | PypiWatch


@dataclass(frozen=True, slots=True)
class BuiltImagePin(ImagePin):
    """A locally built image whose base and inputs are recorded."""

    build: str
    base: str
    base_digest: str
    build_args: Mapping[str, str]
    watch: Mapping[str, WatchEntry]
    inputs_digest: str

    @property
    def built(self) -> bool:
        """Whether both recorded digests identify a completed build."""

        return self.digest != "unbuilt" and self.inputs_digest != "unbuilt"


@dataclass(frozen=True, slots=True)
class ImageLock:
    """The version and ordered image pins in ``images.lock``."""

    version: int
    images: tuple[ImagePin, ...]


@dataclass(frozen=True, slots=True)
class ImageLockError:
    """A structured refusal from image-lock loading or validation."""

    key_path: str | None
    problem: str
    fix: str


@dataclass(frozen=True, slots=True)
class ImageLockLoadResult:
    """The parsed image lock, or every error found while loading it."""

    lock: ImageLock | None = None
    document: Mapping[str, object] | None = None
    errors: tuple[ImageLockError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors and self.lock is not None


@dataclass(frozen=True, slots=True)
class RegistryTarget:
    """The transport, authority, and image-path prefix of a registry."""

    scheme: str
    authority: str
    prefix: str


LIBVIRT_BRIDGE_CIDR: Final = "192.168.122.0/24"
# The acceptance VM reaches the release registry through the host's bridge.
_LIBVIRT_BRIDGE_NETWORK = ipaddress.ip_network(LIBVIRT_BRIDGE_CIDR)

_PROXY_AUTH_PATH: Final = Path("/etc/gideon/secrets/proxy_auth")
# Loopback (the pre-1.0 release registry) and libvirt's bridge address
# ([29] item 13, the acceptance VM's path) are never proxied.
NO_PROXY_LOCAL: Final = "127.0.0.1,localhost,::1,192.168.122.1"
_PROXY_AUTH_FIX: Final = (
    "Run with sudo so the process can read /etc/gideon/secrets/proxy_auth, "
    "then retry."
)
_PROXY_CREDENTIALS_FIX: Final = (
    "Correct /etc/gideon/secrets/proxy_auth so it contains user:password, "
    "then retry."
)
_FIX: Final = (
    "Edit images.lock; consult docs/runbooks/release-files.md §3."
)
_ROOT_KEYS: Final = ("version", "images")
_MIRRORED_KEYS: Final = ("source", "digest")
_BUILT_KEYS: Final = (
    "build",
    "base",
    "base_digest",
    "build_args",
    "watch",
    "inputs_digest",
    "digest",
)
BUILD_ARG_NAME: Final = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SECRET_ARG_TOKENS: Final = frozenset(
    {"PASSWORD", "PASSWD", "SECRET", "TOKEN", "KEY", "CREDENTIAL", "CREDENTIALS", "PRIVATE"}
)
DEBIAN_PACKAGE_NAME: Final = re.compile(r"^[a-z0-9][a-z0-9+.-]{1,99}$")
PYPI_PROJECT_NAME: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_PYPI_PROJECT_NAME_INPUT: Final = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"
)
DIGEST: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
"""The one digest grammar; the pin watch validates what it writes against it."""
_IMAGE_NAME = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$"
)
_SOURCE_HOST = r"(?:localhost|[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?)"
_SOURCE_COMPONENT = r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
SOURCE_REFERENCE: Final = re.compile(
    rf"^(?:(?:{_SOURCE_HOST})/)?{_SOURCE_COMPONENT}"
    rf"(?:/{_SOURCE_COMPONENT})*:[A-Za-z0-9_][A-Za-z0-9_.-]{{0,127}}$"
)
"""The one tagged-reference grammar, shared with the pin watch."""


class _DuplicateKeyError(yaml.YAMLError):
    """Raised when a YAML mapping repeats a key."""


class ImageLockLoader(yaml.SafeLoader):
    """SafeLoader with duplicate-key refusal."""


ImageLockLoader.yaml_implicit_resolvers = {
    initial: list(resolvers)
    for initial, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_mapping_without_duplicates(
    loader: ImageLockLoader, node: Any, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            line = key_node.start_mark.line + 1
            raise _DuplicateKeyError(f"duplicate mapping key {key!r} at line {line}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


ImageLockLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_without_duplicates,
)


def _path(prefix: str, key: object) -> str:
    rendered = str(key)
    return f"{prefix}.{rendered}" if prefix else rendered


def _nearest(path: str, valid: Sequence[str]) -> str:
    return difflib.get_close_matches(path, list(valid), n=1, cutoff=0.0)[0]


def _unknown(path: str, valid: Sequence[str]) -> ImageLockError:
    return ImageLockError(
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
    errors: list[ImageLockError],
) -> None:
    if not isinstance(value, Mapping):
        return
    for key in value:
        path = _path(prefix, key)
        if not isinstance(key, str) or key not in keys:
            errors.append(_unknown(path, keys))
            continue
        if prefix == "" and key == "images":
            continue


def _lookup(document: Mapping[str, object], path: str) -> tuple[bool, object]:
    current: object = document
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return False, None
        current = current[segment]
    return True, current


def _error(path: str, detail: str) -> ImageLockError:
    return ImageLockError(
        key_path=path,
        problem=f"Invalid value for '{path}': {detail}.",
        fix=_FIX,
    )


def _mapping_error(path: str, value: object) -> ImageLockError:
    return _error(path, f"expected a mapping (got {value!r})")


def _string(
    document: Mapping[str, object],
    path: str,
    errors: list[ImageLockError],
    *,
    error_path: str | None = None,
) -> str | None:
    """A non-empty string at *path*, reporting errors under *error_path* when given."""

    reported = path if error_path is None else error_path
    present, value = _lookup(document, path)
    if not present:
        errors.append(_error(reported, "missing required key"))
        return None
    if not isinstance(value, str) or not value:
        errors.append(_error(reported, f"expected a non-empty string (got {value!r})"))
        return None
    return value


def _validate_image_name(path: str, name: object, errors: list[ImageLockError]) -> None:
    if not isinstance(name, str) or not name or _IMAGE_NAME.fullmatch(name) is None:
        errors.append(_error(path, f"expected a lowercase image path (got {name!r})"))


def _validate_digest(
    path: str, value: str | None, errors: list[ImageLockError], *, allow_unbuilt: bool = False
) -> None:
    if value is None:
        return
    if allow_unbuilt and value == "unbuilt":
        return
    if DIGEST.fullmatch(value) is None:
        errors.append(
            _error(path, "expected sha256:<64 lowercase hexadecimal characters>")
        )


def _validate_apt_index(path: str, value: str, errors: list[ImageLockError]) -> None:
    if not _is_https_url(value):
        errors.append(_error(path, "expected an https:// URL"))


def _is_https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.netloc)


def _validate_built_pin(
    image_path: str,
    pin: Mapping[str, object],
    errors: list[ImageLockError],
) -> None:
    build = _string(pin, "build", errors, error_path=f"{image_path}.build")
    if build is not None:
        parts = build.split("/")
        if build.startswith("/") or "\\" in build or ".." in parts:
            errors.append(
                _error(
                    f"{image_path}.build",
                    "expected a relative POSIX path that stays within the checkout",
                )
            )

    base = _string(pin, "base", errors, error_path=f"{image_path}.base")
    if base is not None and SOURCE_REFERENCE.fullmatch(base) is None:
        errors.append(
            _error(
                f"{image_path}.base",
                f"expected a tagged container image reference (got {base!r})",
            )
        )

    base_digest = _string(
        pin, "base_digest", errors, error_path=f"{image_path}.base_digest"
    )
    _validate_digest(f"{image_path}.base_digest", base_digest, errors)

    digest = _string(pin, "digest", errors, error_path=f"{image_path}.digest")
    inputs_digest = _string(
        pin, "inputs_digest", errors, error_path=f"{image_path}.inputs_digest"
    )
    if (
        digest is not None
        and inputs_digest is not None
        and (digest == "unbuilt") != (inputs_digest == "unbuilt")
    ):
        errors.append(
            _error(
                image_path,
                "digest and inputs_digest must both be 'unbuilt' or both be digests",
            )
        )
    _validate_digest(f"{image_path}.digest", digest, errors, allow_unbuilt=True)
    _validate_digest(
        f"{image_path}.inputs_digest", inputs_digest, errors, allow_unbuilt=True
    )

    build_args_value = pin.get("build_args", {})
    if not isinstance(build_args_value, Mapping):
        errors.append(_mapping_error(f"{image_path}.build_args", build_args_value))
        build_args: dict[str, str] = {}
    else:
        build_args = {}
        for key, value in build_args_value.items():
            arg_path = _path(f"{image_path}.build_args", key)
            if not isinstance(key, str) or BUILD_ARG_NAME.fullmatch(key) is None:
                errors.append(
                    _error(
                        arg_path,
                        "expected an ARG name matching [A-Z][A-Z0-9_]*",
                    )
                )
            elif _SECRET_ARG_TOKENS & set(key.split("_")):
                # A build argument is a lock value and an image label: never a secret.
                errors.append(
                    _error(
                        arg_path,
                        "a build argument is release content and an image label, "
                        "never a secret; this name looks like one",
                    )
                )
            if not isinstance(value, str) or not value:
                errors.append(
                    _error(
                        arg_path,
                        f"expected a non-empty single-line string (got {value!r})",
                    )
                )
            elif "\n" in value or "\r" in value:
                errors.append(_error(arg_path, "expected a single-line string"))
            if isinstance(key, str) and isinstance(value, str):
                build_args[key] = value

    watch_value = pin.get("watch", {})
    watch_items: Mapping[object, object]
    if not isinstance(watch_value, Mapping):
        errors.append(_mapping_error(f"{image_path}.watch", watch_value))
        watch_items = {}
    else:
        watch_items = watch_value
    watch: dict[str, WatchEntry] = {}
    for key, value in watch_items.items():
        watch_path = _path(f"{image_path}.watch", key)
        if not isinstance(key, str) or key not in build_args:
            errors.append(
                _error(
                    watch_path,
                    "watch entry must name a key present in build_args",
                )
            )
        if not isinstance(value, Mapping):
            errors.append(_mapping_error(watch_path, value))
            continue
        watch_mapping = cast(Mapping[str, object], value)
        for child_key in watch_mapping:
            if child_key not in ("apt_index", "package", "pypi_project"):
                errors.append(
                    _unknown(
                        _path(watch_path, child_key),
                        ("apt_index", "package", "pypi_project"),
                    )
                )
        has_pypi_project = "pypi_project" in watch_mapping
        has_apt_kind = "apt_index" in watch_mapping or "package" in watch_mapping
        if has_pypi_project and has_apt_kind:
            errors.append(
                _error(
                    watch_path,
                    "a watch entry must name exactly one kind: apt_index and "
                    "package, or pypi_project",
                )
            )
            continue
        if not has_pypi_project and not has_apt_kind:
            errors.append(
                _error(
                    watch_path,
                    "a watch entry must name either apt_index and package, "
                    "or pypi_project",
                )
            )
            continue
        if has_pypi_project:
            project = _string(
                watch_mapping,
                "pypi_project",
                errors,
                error_path=f"{watch_path}.pypi_project",
            )
            if project is not None:
                if PYPI_PROJECT_NAME.fullmatch(project) is None:
                    if _PYPI_PROJECT_NAME_INPUT.fullmatch(project) is not None:
                        normalized = re.sub(r"[-_.]+", "-", project).lower()
                        detail = (
                            "expected the normalized PyPI project name "
                            f"{normalized!r} (got {project!r})"
                        )
                    else:
                        detail = f"expected a PyPI project name (got {project!r})"
                    errors.append(_error(f"{watch_path}.pypi_project", detail))
                elif isinstance(key, str) and key in build_args:
                    watch[key] = PypiWatch(project=project)
            continue
        apt_index = _string(
            watch_mapping,
            "apt_index",
            errors,
            error_path=f"{watch_path}.apt_index",
        )
        package = _string(
            watch_mapping,
            "package",
            errors,
            error_path=f"{watch_path}.package",
        )
        if apt_index is not None:
            _validate_apt_index(f"{watch_path}.apt_index", apt_index, errors)
        if package is not None and DEBIAN_PACKAGE_NAME.fullmatch(package) is None:
            errors.append(
                _error(
                    f"{watch_path}.package",
                    f"expected a Debian package name (got {package!r})",
                )
            )
        if (
            isinstance(key, str)
            and key in build_args
            and apt_index is not None
            and package is not None
            and _is_https_url(apt_index)
            and DEBIAN_PACKAGE_NAME.fullmatch(package) is not None
        ):
            watch[key] = AptWatch(apt_index=apt_index, package=package)


def _validate_mirrored_pin(
    image_path: str,
    pin: Mapping[str, object],
    errors: list[ImageLockError],
) -> None:
    source = _string(pin, "source", errors, error_path=f"{image_path}.source")
    digest = _string(pin, "digest", errors, error_path=f"{image_path}.digest")
    if source is not None and SOURCE_REFERENCE.fullmatch(source) is None:
        errors.append(
            _error(
                f"{image_path}.source",
                f"expected a tagged container image reference (got {source!r})",
            )
        )
    _validate_digest(f"{image_path}.digest", digest, errors)


def validate_image_lock(document: Mapping[str, object]) -> list[ImageLockError]:
    """Return all shape, pin, and unknown-key errors in an image lock."""

    errors: list[ImageLockError] = []
    _walk_unknown(document, _ROOT_KEYS, "", errors)

    present, version = _lookup(document, "version")
    if not present:
        errors.append(_error("version", "missing required key"))
    elif not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        errors.append(_error("version", f"expected a positive integer (got {version!r})"))

    present, images_value = _lookup(document, "images")
    if not present:
        errors.append(_error("images", "missing required key"))
        return errors
    if not isinstance(images_value, Mapping):
        errors.append(_error("images", f"expected a mapping (got {images_value!r})"))
        return errors
    if not images_value:
        errors.append(_error("images", "expected a non-empty mapping"))
        return errors

    for name, pin_value in images_value.items():
        image_path = _path("images", name)
        _validate_image_name(image_path, name, errors)
        if not isinstance(pin_value, Mapping):
            errors.append(_mapping_error(image_path, pin_value))
            continue
        pin = cast(Mapping[str, object], pin_value)
        has_source = "source" in pin
        has_build = "build" in pin
        if has_source and has_build:
            errors.append(
                _error(
                    image_path,
                    "a pin must use either the mirrored shape or the built shape, not both",
                )
            )
            _walk_unknown(pin, _MIRRORED_KEYS + _BUILT_KEYS, image_path, errors)
        elif has_build:
            _walk_unknown(pin, _BUILT_KEYS, image_path, errors)
            _validate_built_pin(image_path, pin, errors)
        elif has_source:
            _walk_unknown(pin, _MIRRORED_KEYS, image_path, errors)
            _validate_mirrored_pin(image_path, pin, errors)
        else:
            # Neither `source` nor `build`: the mirrored shape is the default,
            # so its missing-`source` error names the one thing to add.
            _walk_unknown(pin, _MIRRORED_KEYS, image_path, errors)
            _validate_mirrored_pin(image_path, pin, errors)
    return errors


def _construct(document: Mapping[str, object]) -> ImageLock:
    images_value = cast(Mapping[str, object], document["images"])
    return ImageLock(
        version=cast(int, document["version"]),
        images=tuple(
            _construct_pin(cast(str, name), cast(Mapping[str, object], pin))
            for name, pin in images_value.items()
        ),
    )


def _construct_pin(name: str, pin: Mapping[str, object]) -> ImagePin:
    digest = cast(str, pin["digest"])
    if "source" in pin:
        return MirroredImagePin(
            name=name,
            digest=digest,
            source=cast(str, pin["source"]),
        )
    build_args_value = pin.get("build_args", {})
    watch_value = pin.get("watch", {})
    build_args = {
        cast(str, key): cast(str, value)
        for key, value in cast(Mapping[object, object], build_args_value).items()
    }
    watch: dict[str, WatchEntry] = {}
    for key, value in cast(Mapping[object, object], watch_value).items():
        watch_mapping = cast(Mapping[str, object], value)
        if "pypi_project" in watch_mapping:
            watch[cast(str, key)] = PypiWatch(
                project=cast(str, watch_mapping["pypi_project"])
            )
        else:
            watch[cast(str, key)] = AptWatch(
                apt_index=cast(str, watch_mapping["apt_index"]),
                package=cast(str, watch_mapping["package"]),
            )
    return BuiltImagePin(
        name=name,
        digest=digest,
        build=cast(str, pin["build"]),
        base=cast(str, pin["base"]),
        base_digest=cast(str, pin["base_digest"]),
        build_args=build_args,
        watch=watch,
        inputs_digest=cast(str, pin["inputs_digest"]),
    )


def inputs_digest_text(
    base_digest: str,
    build_args: Mapping[str, str],
    dockerfile_bytes: bytes,
) -> str:
    """Return canonical text for the inputs of a built image."""

    lines = [f"base_digest={base_digest}"]
    lines.extend(f"arg {name}={build_args[name]}" for name in sorted(build_args))
    lines.append(f"dockerfile sha256={hashlib.sha256(dockerfile_bytes).hexdigest()}")
    return "\n".join(lines)


def compute_inputs_digest(
    base_digest: str,
    build_args: Mapping[str, str],
    dockerfile_bytes: bytes,
) -> str:
    """Compute the digest recorded for a built image's inputs."""

    return "sha256:" + hashlib.sha256(
        inputs_digest_text(base_digest, build_args, dockerfile_bytes).encode("utf-8")
    ).hexdigest()


def render_errors(errors: Sequence[ImageLockError]) -> str:
    """Render one refusal per line, with its corrective action last."""

    return "\n".join(
        f"{' '.join(error.problem.splitlines())} Fix: {error.fix}" for error in errors
    )


def read_image_lock(path: PathLike, *, host: Host | None = None) -> ImageLockLoadResult:
    """Read and parse an image lock without constructing its dataclasses."""

    io = host or RealHost()
    try:
        text = io.read_text(path)
    except FileNotFoundError:
        return ImageLockLoadResult(
            errors=(ImageLockError(None, f"image lock is missing: {path}", _FIX),)
        )
    except PermissionError:
        return ImageLockLoadResult(
            errors=(
                ImageLockError(
                    None, f"image lock is unreadable due to permissions: {path}", _FIX
                ),
            )
        )
    except UnicodeDecodeError:
        return ImageLockLoadResult(
            errors=(ImageLockError(None, f"image lock is not valid UTF-8: {path}", _FIX),)
        )
    except OSError as exc:
        return ImageLockLoadResult(
            errors=(ImageLockError(None, f"image lock is unreadable: {path} ({exc})", _FIX),)
        )
    return parse_image_lock(text)


def parse_image_lock(text: str) -> ImageLockLoadResult:
    """Parse lock text into its document (no validation, no dataclasses)."""

    try:
        document = yaml.load(text, Loader=ImageLockLoader)
    except _DuplicateKeyError as exc:
        return ImageLockLoadResult(
            errors=(ImageLockError(None, f"image lock has a {exc}", _FIX),)
        )
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = getattr(mark, "line", None)
        location = f" at line {line + 1}" if isinstance(line, int) else ""
        return ImageLockLoadResult(
            errors=(
                ImageLockError(
                    None, f"image lock has a YAML parse error{location}: {exc}", _FIX
                ),
            )
        )
    if document is None:
        return ImageLockLoadResult(errors=(ImageLockError(None, "image lock is empty", _FIX),))
    if not isinstance(document, Mapping):
        return ImageLockLoadResult(
            errors=(ImageLockError(None, "image lock root must be a mapping", _FIX),)
        )
    return ImageLockLoadResult(document=cast(Mapping[str, object], document))


def _finish(result: ImageLockLoadResult) -> ImageLockLoadResult:
    if result.errors or result.document is None:
        return result
    errors = validate_image_lock(result.document)
    if errors:
        return ImageLockLoadResult(document=result.document, errors=tuple(errors))
    return ImageLockLoadResult(
        lock=_construct(result.document),
        document=result.document,
    )


def load_image_lock(path: PathLike, *, host: Host | None = None) -> ImageLockLoadResult:
    """Parse, validate, and construct an image lock."""

    return _finish(read_image_lock(path, host=host))


def load_image_lock_text(text: str) -> ImageLockLoadResult:
    """Parse, validate, and construct an image lock from text already in hand.

    The pin watch loads ``origin/main``'s lock this way, so a bump is always
    patched onto the tip it will be proposed against.
    """

    return _finish(parse_image_lock(text))


def _authority_host(authority: str) -> str | None:
    """Extract a host from a normalized registry authority."""

    try:
        return urlsplit(f"//{authority}").hostname
    except ValueError:
        return None


def is_loopback_registry(authority: str) -> bool:
    """Return whether a registry authority names loopback."""

    host = _authority_host(authority)
    if host is None:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def is_plain_registry(authority: str) -> bool:
    """Return whether a registry authority must use plain HTTP."""

    host = _authority_host(authority)
    if host is None:
        return False
    if is_loopback_registry(authority):
        return True
    try:
        return ipaddress.ip_address(host) in _LIBVIRT_BRIDGE_NETWORK
    except ValueError:
        return False


def parse_registry(registry: str) -> RegistryTarget | None:
    """Parse a registry prefix and choose its local-or-public transport."""

    if not registry or any(character.isspace() for character in registry):
        return None
    parsed = urlsplit(registry if "://" in registry else f"//{registry}")
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if host is None or parsed.username is not None or parsed.password is not None:
        return None
    if parsed.query or parsed.fragment:
        return None

    authority = host
    if ":" in host and not host.startswith("["):
        authority = f"[{host}]"
    if port is not None:
        authority = f"{authority}:{port}"

    scheme = "http" if is_plain_registry(authority) else "https"

    path = parsed.path.strip("/")
    if not path:
        prefix = ""
    elif any(not segment for segment in path.split("/")):
        return None
    else:
        prefix = f"{path}/"
    return RegistryTarget(scheme=scheme, authority=authority, prefix=prefix)


def reference(target: RegistryTarget, pin: ImagePin) -> str:
    """Build the digest-pinned image reference for a registry target."""

    if isinstance(pin, BuiltImagePin) and not pin.built:
        raise ValueError(
            f"built image '{pin.name}' is unbuilt; run "
            f"python3 -m tools.imagebuild {pin.name}"
        )
    return f"{target.authority}/{target.prefix}{pin.name}@{pin.digest}"


@dataclass(frozen=True, slots=True)
class ProxyEnvironment:
    """Child-process proxy variables, or a fix-bearing problem for the caller to report."""

    variables: Mapping[str, str] = field(default_factory=dict)
    problem: str | None = None
    fix: str = ""

    @property
    def ok(self) -> bool:
        return self.problem is None


def _proxy_failure(problem: str, fix: str) -> ProxyEnvironment:
    return ProxyEnvironment(problem=problem, fix=fix)


def _proxy_auth(host: Host) -> tuple[str, str] | ProxyEnvironment | None:
    """Read optional proxy credentials without exposing their contents."""

    try:
        value = host.read_text(_PROXY_AUTH_PATH).rstrip("\r\n")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        return _proxy_failure(
            f"Proxy credentials are not valid UTF-8: {_PROXY_AUTH_PATH}.",
            _PROXY_CREDENTIALS_FIX,
        )
    except OSError as exc:
        return _proxy_failure(
            f"Proxy credentials are unreadable: {_PROXY_AUTH_PATH} ({exc}).",
            _PROXY_AUTH_FIX,
        )

    user, separator, password = value.partition(":")
    if not separator or not user or not password:
        return _proxy_failure(
            "proxy_auth must contain a non-empty user:password pair.",
            _PROXY_CREDENTIALS_FIX,
        )
    return user, password


def proxy_url(
    egress_proxy: str,
    credentials: tuple[str, str] | str | None = None,
) -> str:
    """Build a validated proxy URL without reading host state.

    ``credentials`` is either an already split ``(user, password)`` pair or
    the raw ``user:password`` form.  Both user-info components are encoded
    independently so reserved characters remain data, not URL syntax.
    """

    if not egress_proxy:
        return ""
    parsed = urlsplit(egress_proxy)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            f"egress_proxy is not a usable HTTP(S) proxy: {egress_proxy!r}."
        )
    try:
        _ = parsed.port
    except ValueError:
        raise ValueError(
            f"egress_proxy has an invalid port: {egress_proxy!r}."
        ) from None

    if credentials is None:
        return egress_proxy
    if isinstance(credentials, str):
        user, separator, password = credentials.partition(":")
        if not separator:
            raise ValueError("proxy credentials must contain a user:password pair")
    else:
        user, password = credentials
    if not user or not password:
        raise ValueError("proxy credentials must contain a non-empty user:password pair")
    hostport = parsed.netloc.rsplit("@", 1)[-1]
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{hostport}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def proxy_environment(site: SiteConfig, host: Host) -> ProxyEnvironment:
    """Build child-process proxy variables for a configured egress proxy.

    Credentials are read only through the host seam and percent-encoded into
    proxy URL userinfo.  They never appear in command arguments or refusal
    text.  An empty ``egress_proxy`` deliberately returns no overrides.
    """

    proxy = site.egress_proxy
    if not proxy:
        return ProxyEnvironment()

    credentials = _proxy_auth(host)
    if isinstance(credentials, ProxyEnvironment):
        return credentials
    try:
        proxy = proxy_url(proxy, credentials)
    except ValueError as exc:
        return _proxy_failure(
            str(exc),
            "Correct egress_proxy in /etc/gideon/site.yaml, then retry.",
        )

    variables = {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "NO_PROXY": NO_PROXY_LOCAL,
    }
    return ProxyEnvironment(variables=variables)
