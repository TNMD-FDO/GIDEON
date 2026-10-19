"""Read and atomically point each derived store's collection alias."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from gideon.host import derivedstores, report, stack
from gideon.host.render.opensearch import (
    OPENSEARCH_PASSWORD_SECRET_NAME,
    OPENSEARCH_SERVICE_NAME,
)
from gideon.host.render.qdrant import QDRANT_SECRET_NAME, QDRANT_SERVICE_NAME
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike


@dataclass(frozen=True, slots=True)
class AliasTargets:
    """The generation names each store currently serves through the alias."""

    lexical: tuple[str, ...]
    dense: tuple[str, ...]


def alias_name(collection: str) -> str:
    """Use the collection's own name for its alias in both stores."""

    return collection


def _invalid_reply(
    rendered_dir: PathLike, service: str, operation: str, command_path: str,
) -> Problem:
    return Problem(
        f"{service} returned an invalid alias {operation}",
        f"Run {stack.logs_fix(rendered_dir, service)}, "
        f"then run {report.command(command_path)} again.",
    )


def read_aliases(
    host: Host, rendered_dir: PathLike, collection: str, *, command_path: str,
) -> AliasTargets | Problem:
    """Read both aliases, treating OpenSearch's missing-alias reply as absence."""

    alias = alias_name(collection)
    password = derivedstores.store_secret(host, OPENSEARCH_PASSWORD_SECRET_NAME)
    if isinstance(password, Problem):
        return password
    lexical = derivedstores.opensearch_request(
        host, rendered_dir, password, "GET", f"/_alias/{alias}",
        accepted_statuses=frozenset({200, 404}),
    )
    if isinstance(lexical, Problem):
        return lexical
    lexical_names: list[str] = []
    if lexical.status == 200:
        if not isinstance(lexical.data, Mapping):
            return _invalid_reply(rendered_dir, OPENSEARCH_SERVICE_NAME, "listing", command_path)
        for name, entry in lexical.data.items():
            aliases = entry.get("aliases") if isinstance(entry, Mapping) else None
            if (
                not isinstance(name, str) or not name
                or not isinstance(aliases, Mapping)
                or not isinstance(aliases.get(alias), Mapping)
            ):
                return _invalid_reply(rendered_dir, OPENSEARCH_SERVICE_NAME, "listing", command_path)
            lexical_names.append(name)

    key = derivedstores.store_secret(host, QDRANT_SECRET_NAME)
    if isinstance(key, Problem):
        return key
    dense = derivedstores.qdrant_request(host, rendered_dir, key, "GET", "/aliases")
    if isinstance(dense, Problem):
        return dense
    result = dense.data.get("result") if isinstance(dense.data, Mapping) else None
    entries = result.get("aliases") if isinstance(result, Mapping) else None
    if not isinstance(entries, list):
        return _invalid_reply(rendered_dir, QDRANT_SERVICE_NAME, "listing", command_path)
    dense_names: list[str] = []
    for entry in entries:
        if (
            not isinstance(entry, Mapping)
            or not isinstance(entry.get("alias_name"), str) or not entry["alias_name"]
            or not isinstance(entry.get("collection_name"), str) or not entry["collection_name"]
        ):
            return _invalid_reply(rendered_dir, QDRANT_SERVICE_NAME, "listing", command_path)
        if entry["alias_name"] == alias:
            dense_names.append(entry["collection_name"])
    return AliasTargets(tuple(sorted(lexical_names)), tuple(sorted(set(dense_names))))


def point_alias(
    host: Host, rendered_dir: PathLike, store: Literal["lexical", "dense"],
    collection: str, name: str, current: tuple[str, ...], *, command_path: str,
) -> str | Problem:
    """Swap one store's alias in one actions request, or leave its target unchanged."""

    if current == (name,):
        return "unchanged"
    alias = alias_name(collection)
    actions: list[dict[str, object]]
    if store == "lexical":
        password = derivedstores.store_secret(host, OPENSEARCH_PASSWORD_SECRET_NAME)
        if isinstance(password, Problem):
            return password
        actions = [{"remove": {"index": previous, "alias": alias}} for previous in current]
        actions.append({"add": {"index": name, "alias": alias}})
        reply = derivedstores.opensearch_request(
            host, rendered_dir, password, "POST", "/_aliases", body={"actions": actions},
        )
        if isinstance(reply, Problem):
            return reply
        if not isinstance(reply.data, Mapping) or reply.data.get("acknowledged") is not True:
            return _invalid_reply(rendered_dir, OPENSEARCH_SERVICE_NAME, "update", command_path)
    else:
        key = derivedstores.store_secret(host, QDRANT_SECRET_NAME)
        if isinstance(key, Problem):
            return key
        actions = [{"delete_alias": {"alias_name": alias}}] if current else []
        actions.append({"create_alias": {"collection_name": name, "alias_name": alias}})
        reply = derivedstores.qdrant_request(
            host, rendered_dir, key, "POST", "/collections/aliases", body={"actions": actions},
        )
        if isinstance(reply, Problem):
            return reply
        if not isinstance(reply.data, Mapping) or reply.data.get("result") is not True:
            return _invalid_reply(rendered_dir, QDRANT_SERVICE_NAME, "update", command_path)
    return f"{', '.join(current) or 'none'} -> {name}"
