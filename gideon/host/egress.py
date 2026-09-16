"""Loader and validator for the repository's egress allowlist artifact."""

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast
from urllib.parse import urlparse

import yaml  # type: ignore[import-untyped]

from gideon.host.sysio import Host, PathLike, RealHost


@dataclass(frozen=True, slots=True)
class EgressHost:
    """One network host and its transport probe URL."""

    host: str
    probe_url: str
    # HTTP statuses that count as this host answering, beyond the global
    # rule (200s, 401, 404) — e.g. 403 from a signed-URL CDN.
    expect: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class EgressGroup:
    """The hosts needed by one GIDEON operation group."""

    name: str
    hosts: tuple[EgressHost, ...]


@dataclass(frozen=True, slots=True)
class EgressAllowlist:
    """The versioned, grouped egress allowlist."""

    version: int
    groups: tuple[EgressGroup, ...]

    def group(self, name: str) -> EgressGroup | None:
        """Return a named group, or ``None`` when it is not present."""

        return next((group for group in self.groups if group.name == name), None)


@dataclass(frozen=True, slots=True)
class EgressError:
    """A structured refusal from egress allowlist loading or validation."""

    key_path: str | None
    problem: str
    fix: str


@dataclass(frozen=True, slots=True)
class EgressAllowlistLoadResult:
    """The parsed allowlist, or every error found while loading it."""

    allowlist: EgressAllowlist | None = None
    document: Mapping[str, object] | None = None
    errors: tuple[EgressError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors and self.allowlist is not None


class _DuplicateKeyError(yaml.YAMLError):
    pass


class EgressLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate keys."""


EgressLoader.yaml_implicit_resolvers = {
    initial: list(resolvers)
    for initial, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_mapping_without_duplicates(
    loader: EgressLoader, node: Any, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            line = key_node.start_mark.line + 1
            raise _DuplicateKeyError(f"duplicate mapping key {key!r} at line {line}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


EgressLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_without_duplicates,
)


_FIX: Final = (
    "Edit config/egress.yaml; consult docs/archi/host.md."
)
_ROOT_KEYS: Final = ("version", "groups")
_GROUP_NAMES: Final = ("host-provisioning", "install-upgrade", "corpus", "image-build")
_HOST_KEYS: Final = ("host", "probe_url", "expect")


def _path(prefix: str, key: object) -> str:
    rendered = str(key)
    return f"{prefix}.{rendered}" if prefix else rendered


def _nearest(path: str, valid: Sequence[str]) -> str:
    return difflib.get_close_matches(path, list(valid), n=1, cutoff=0.0)[0]


def _unknown(path: str, valid: Sequence[str]) -> EgressError:
    return EgressError(
        key_path=path,
        problem=(
            f"Unknown key '{path}'; nearest valid key is '{_nearest(path, valid)}'."
        ),
        fix=_FIX,
    )


def _error(path: str, detail: str) -> EgressError:
    return EgressError(
        key_path=path,
        problem=f"Invalid value for '{path}': {detail}.",
        fix=_FIX,
    )


def _walk_unknown(
    value: object,
    keys: Sequence[str],
    prefix: str,
    errors: list[EgressError],
) -> None:
    if not isinstance(value, Mapping):
        return
    for key in value:
        path = _path(prefix, key)
        if not isinstance(key, str) or key not in keys:
            errors.append(_unknown(path, keys))


def _lookup(document: Mapping[str, object], path: str) -> tuple[bool, object]:
    current: object = document
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return False, None
        current = current[segment]
    return True, current


def _string(
    value: Mapping[str, object],
    key: str,
    path: str,
    errors: list[EgressError],
) -> str | None:
    if key not in value:
        errors.append(_error(path, "missing required key"))
        return None
    item = value[key]
    if not isinstance(item, str) or not item:
        errors.append(_error(path, f"expected a non-empty string (got {item!r})"))
        return None
    return item


def _validate_probe_url(path: str, value: str, errors: list[EgressError]) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        errors.append(_error(path, f"expected an HTTP(S) URL (got {value!r})"))


def validate_egress_allowlist(document: Mapping[str, object]) -> list[EgressError]:
    """Return all shape, URL, and unknown-key errors in an allowlist."""

    errors: list[EgressError] = []
    _walk_unknown(document, _ROOT_KEYS, "", errors)

    present, version = _lookup(document, "version")
    if not present:
        errors.append(_error("version", "missing required key"))
    elif not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        errors.append(_error("version", f"expected a positive integer (got {version!r})"))

    present, groups_value = _lookup(document, "groups")
    if not present:
        errors.append(_error("groups", "missing required key"))
        return errors
    if not isinstance(groups_value, Mapping):
        errors.append(_error("groups", f"expected a mapping (got {groups_value!r})"))
        return errors

    groups = cast(Mapping[str, object], groups_value)
    for group_name in _GROUP_NAMES:
        if group_name not in groups:
            errors.append(_error(f"groups.{group_name}", "missing required group"))

    for group_name, entries in groups.items():
        if not isinstance(group_name, str) or group_name not in _GROUP_NAMES:
            errors.append(_unknown(f"groups.{group_name}", _GROUP_NAMES))
            continue
        group_path = f"groups.{group_name}"
        if not isinstance(entries, list) or not entries:
            errors.append(
                _error(group_path, f"expected a non-empty list (got {entries!r})")
            )
            continue
        seen_hosts: set[str] = set()
        for index, entry in enumerate(entries):
            entry_path = f"{group_path}[{index}]"
            if not isinstance(entry, Mapping):
                errors.append(_error(entry_path, f"expected a mapping (got {entry!r})"))
                continue
            item = cast(Mapping[str, object], entry)
            _walk_unknown(item, _HOST_KEYS, entry_path, errors)
            host = _string(item, "host", f"{entry_path}.host", errors)
            probe_url = _string(item, "probe_url", f"{entry_path}.probe_url", errors)
            if host is not None:
                if any(character.isspace() for character in host):
                    errors.append(_error(f"{entry_path}.host", "must not contain whitespace"))
                elif host in seen_hosts:
                    errors.append(_error(f"{entry_path}.host", "must be unique within its group"))
                seen_hosts.add(host)
            if probe_url is not None:
                _validate_probe_url(f"{entry_path}.probe_url", probe_url, errors)
            if "expect" in item:
                expect = item["expect"]
                if not isinstance(expect, list) or not expect:
                    errors.append(
                        _error(
                            f"{entry_path}.expect",
                            f"expected a non-empty list of HTTP statuses (got {expect!r})",
                        )
                    )
                else:
                    for position, status in enumerate(expect):
                        if (
                            isinstance(status, bool)
                            or not isinstance(status, int)
                            or not 100 <= status <= 599
                        ):
                            errors.append(
                                _error(
                                    f"{entry_path}.expect[{position}]",
                                    f"expected an HTTP status 100-599 (got {status!r})",
                                )
                            )
    return errors


def _construct(document: Mapping[str, object]) -> EgressAllowlist:
    groups_value = cast(Mapping[str, object], document["groups"])
    groups: list[EgressGroup] = []
    for group_name in _GROUP_NAMES:
        entries = cast(list[Mapping[str, object]], groups_value[group_name])
        groups.append(
            EgressGroup(
                name=group_name,
                hosts=tuple(
                    EgressHost(
                        host=cast(str, entry["host"]),
                        probe_url=cast(str, entry["probe_url"]),
                        expect=tuple(cast(list[int], entry.get("expect", []))),
                    )
                    for entry in entries
                ),
            )
        )
    return EgressAllowlist(
        version=cast(int, document["version"]),
        groups=tuple(groups),
    )


def render_errors(errors: Sequence[EgressError]) -> str:
    """Render one refusal per line, with its corrective action last."""

    return "\n".join(
        f"{' '.join(error.problem.splitlines())} Fix: {error.fix}" for error in errors
    )


def read_egress_allowlist(
    path: PathLike, *, host: Host | None = None
) -> EgressAllowlistLoadResult:
    """Read and parse an egress artifact without constructing its dataclasses."""

    io = host or RealHost()
    try:
        text = io.read_text(path)
    except FileNotFoundError:
        return EgressAllowlistLoadResult(
            errors=(EgressError(None, f"egress allowlist is missing: {path}", _FIX),)
        )
    except PermissionError:
        return EgressAllowlistLoadResult(
            errors=(
                EgressError(
                    None,
                    f"egress allowlist is unreadable due to permissions: {path}",
                    _FIX,
                ),
            )
        )
    except UnicodeDecodeError:
        return EgressAllowlistLoadResult(
            errors=(EgressError(None, f"egress allowlist is not valid UTF-8: {path}", _FIX),)
        )
    except OSError as exc:
        return EgressAllowlistLoadResult(
            errors=(EgressError(None, f"egress allowlist is unreadable: {path} ({exc})", _FIX),)
        )

    try:
        document = yaml.load(text, Loader=EgressLoader)
    except _DuplicateKeyError as exc:
        return EgressAllowlistLoadResult(
            errors=(EgressError(None, f"egress allowlist has a {exc}", _FIX),)
        )
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = getattr(mark, "line", None)
        location = f" at line {line + 1}" if isinstance(line, int) else ""
        return EgressAllowlistLoadResult(
            errors=(
                EgressError(
                    None,
                    f"egress allowlist has a YAML parse error{location}: {exc}",
                    _FIX,
                ),
            )
        )
    if document is None:
        return EgressAllowlistLoadResult(
            errors=(EgressError(None, "egress allowlist is empty", _FIX),)
        )
    if not isinstance(document, Mapping):
        return EgressAllowlistLoadResult(
            errors=(EgressError(None, "egress allowlist root must be a mapping", _FIX),)
        )
    return EgressAllowlistLoadResult(document=cast(Mapping[str, object], document))


def load_egress_allowlist(
    path: PathLike, *, host: Host | None = None
) -> EgressAllowlistLoadResult:
    """Parse, validate, and construct an egress allowlist."""

    result = read_egress_allowlist(path, host=host)
    if result.errors or result.document is None:
        return result
    errors = validate_egress_allowlist(result.document)
    if errors:
        return EgressAllowlistLoadResult(document=result.document, errors=tuple(errors))
    return EgressAllowlistLoadResult(
        allowlist=_construct(result.document), document=result.document
    )


def default_egress_path() -> Path:
    """Return the committed egress artifact path for this checkout."""

    return Path(__file__).parents[2] / "config/egress.yaml"
