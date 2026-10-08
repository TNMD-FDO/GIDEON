"""GIDEON's keys in Docker's shared daemon file.

GIDEON owns the keys `OWNED_KEYS` names and sets one only while it is absent or
at its default; it never changes or removes any other key. The address pool is
met only by GIDEON's exact one-entry list, never by membership: Docker allocates
across every pool listed, so a second pool would hand out ranges the first keeps
clear of the office's networks.
"""

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit, urlunsplit

# The prefix length of each network Docker carves from GIDEON's pool, and the
# longest prefix a pool may have so it holds at least sixteen such networks: the
# default bridge, rebuilt from the pool at Docker's restart, the standing
# bridges, the throwaway CI projects, and headroom.
ADDRESS_POOL_SIZE: Final = 24
ADDRESS_POOL_MAX_PREFIX: Final = 20


@dataclass(frozen=True)
class OwnedKey:
    name: str
    path: tuple[str, ...]
    defaults: tuple[object, ...]
    membership: bool


@dataclass(frozen=True)
class Need:
    key: OwnedKey
    value: object


@dataclass(frozen=True)
class Reading:
    short: tuple[tuple[str, str, str], ...]
    to_set: tuple[str, ...]
    foreign: tuple[str, ...]
    malformed: tuple[tuple[str, str], ...]


_DATA_ROOT = OwnedKey("data-root", ("data-root",), ("/var/lib/docker",), False)
_LOG_DRIVER = OwnedKey("log-driver", ("log-driver",), ("json-file",), False)
_CDI = OwnedKey("features.cdi", ("features", "cdi"), (), False)
_HTTP_PROXY = OwnedKey("proxies.http-proxy", ("proxies", "http-proxy"), (), False)
_HTTPS_PROXY = OwnedKey("proxies.https-proxy", ("proxies", "https-proxy"), (), False)
_REGISTRIES = OwnedKey("insecure-registries", ("insecure-registries",), ([],), True)
_ADDRESS_POOLS = OwnedKey("default-address-pools", ("default-address-pools",), ([],), False)
OWNED_KEYS = (
    _DATA_ROOT,
    _LOG_DRIVER,
    _CDI,
    _HTTP_PROXY,
    _HTTPS_PROXY,
    _REGISTRIES,
    _ADDRESS_POOLS,
)
# The top-level keys whose value must be an object or a list for GIDEON's paths.
CONTAINER_KINDS: dict[str, type] = {
    "features": dict,
    "proxies": dict,
    "insecure-registries": list,
    "default-address-pools": list,
}


def address_pool_value(base: str) -> list[dict[str, object]]:
    """Build the one pool entry Docker uses to allocate container networks."""

    return [{"base": base, "size": ADDRESS_POOL_SIZE}]


def needs(
    egress_proxy: str | None, insecure_registry: str | None, address_pool: str | None
) -> tuple[Need, ...]:
    """Select the owned keys required by the current site facts."""

    wanted = [Need(_DATA_ROOT, "/var/lib/docker"), Need(_LOG_DRIVER, "journald"), Need(_CDI, True)]
    if egress_proxy:
        wanted.extend((Need(_HTTP_PROXY, egress_proxy), Need(_HTTPS_PROXY, egress_proxy)))
    if insecure_registry:
        wanted.append(Need(_REGISTRIES, insecure_registry))
    if address_pool:
        wanted.append(Need(_ADDRESS_POOLS, address_pool_value(address_pool)))
    return tuple(wanted)


def _redacted(value: object) -> object:
    if isinstance(value, dict):
        return {name: _redacted(child) for name, child in value.items()}
    if isinstance(value, list):
        return [_redacted(child) for child in value]
    if isinstance(value, str):
        try:
            url = urlsplit(value)
            if url.scheme and url.netloc and url.username is not None:
                return urlunsplit(
                    (url.scheme, f"…@{url.netloc.rsplit('@', 1)[1]}", url.path, url.query, url.fragment)
                )
        except ValueError:
            pass
    return value


def render(value: object) -> str:
    """Show a JSON value with every URL's credentials, however deep, redacted."""

    return json.dumps(_redacted(value), ensure_ascii=False)


def _same(found: object, wanted: object) -> bool:
    return type(found) is type(wanted) and found == wanted


def _foreign(current: dict[str, object], wanted: tuple[Need, ...]) -> tuple[str, ...]:
    paths = {need.key.path for need in wanted}
    names: list[str] = []
    for name, value in current.items():
        if name in ("features", "proxies") and isinstance(value, dict) and value:
            names.extend(f"{name}.{child}" for child in value if (name, child) not in paths)
        elif not any(path[0] == name for path in paths):
            names.append(name)
    return tuple(sorted(names))


def read(current: dict[str, object], wanted: tuple[Need, ...]) -> Reading:
    """Classify needed values and foreign keys in an existing JSON object."""

    entered = {need.key.path[0] for need in wanted}
    malformed = tuple(
        (name, render(current[name]))
        for name, kind in CONTAINER_KINDS.items()
        if name in entered and name in current and not isinstance(current[name], kind)
    )
    malformed_names = {name for name, _ in malformed}
    short: list[tuple[str, str, str]] = []
    to_set: list[str] = []
    for need in wanted:
        key = need.key
        if key.path[0] in malformed_names:
            continue
        parent: dict[str, object] = current
        for segment in key.path[:-1]:
            child = parent.get(segment)
            if not isinstance(child, dict):
                break
            parent = child
        else:
            segment = key.path[-1]
            if segment in parent:
                found = parent[segment]
                met = (
                    isinstance(found, list) and need.value in found
                    if key.membership
                    else _same(found, need.value)
                )
                if met:
                    continue
                if any(_same(found, default) for default in key.defaults):
                    to_set.append(key.name)
                else:
                    short.append((key.name, render(found), render(need.value)))
                continue
        to_set.append(key.name)
    return Reading(tuple(short), tuple(to_set), _foreign(current, wanted), malformed)


def merge(current: dict[str, object], wanted: tuple[Need, ...]) -> tuple[dict[str, object], bool]:
    """Write only default or absent needed values into a deep copy."""

    merged = deepcopy(current)
    to_set = set(read(current, wanted).to_set)
    for need in wanted:
        if need.key.name not in to_set:
            continue
        parent = merged
        for segment in need.key.path[:-1]:
            child = parent.setdefault(segment, {})
            if not isinstance(child, dict):
                raise ValueError(f"{segment} is not an object")
            parent = child
        parent[need.key.path[-1]] = [need.value] if need.key.membership else need.value
    return merged, merged != current


def text(obj: dict[str, object]) -> str:
    """Serialize a daemon file in the existing stable format."""

    return json.dumps(obj, indent=2, sort_keys=True) + "\n"
