"""Generate the JSON Schema for the GIDEON site file."""

import json
from pathlib import Path
from typing import Any, assert_never

from gideon.host.site import FIELD_REGISTRY, FieldSpec
from gideon.host.sysio import Host, RealHost


def _object_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    }


def _leaf_schema(spec: FieldSpec) -> dict[str, Any]:
    schema: dict[str, Any] = {"description": spec.description}
    kind = spec.kind
    item_schema: dict[str, Any] | None = None

    if kind == "string" or kind == "non-empty string" or kind == "timezone":
        schema["type"] = "string"
        if kind == "non-empty string":
            schema["minLength"] = 1
    elif kind == "int":
        schema["type"] = "integer"
    elif kind == "enum":
        schema["type"] = "string"
    elif (
        kind == "string list"
        or kind == "non-empty string list"
        or kind == "CIDR list"
    ):
        schema["type"] = "array"
        item_schema = {"type": "string"}
        if kind == "non-empty string list":
            item_schema["minLength"] = 1
        schema["items"] = item_schema
        if kind != "string list":
            schema["minItems"] = 1
    else:
        assert_never(kind)

    if spec.allowed_values:
        if item_schema is None:
            schema["enum"] = list(spec.allowed_values)
        else:
            item_schema["enum"] = list(spec.allowed_values)
    if spec.pattern is not None:
        if item_schema is None:
            schema["pattern"] = spec.pattern
        else:
            item_schema["pattern"] = spec.pattern
    if spec.minimum is not None:
        schema["minimum"] = spec.minimum
    if not spec.required and not spec.derived:
        schema["default"] = spec.default
    return schema


def build_schema() -> dict[str, Any]:
    """Build a JSON Schema document from the site field registry."""

    root = _object_schema()
    for spec in FIELD_REGISTRY:
        parts = spec.path.split(".")
        node = root
        for segment in parts[:-1]:
            properties = node["properties"]
            child = properties.get(segment)
            if child is None:
                child = _object_schema()
                properties[segment] = child

            if spec.required:
                required = node.setdefault("required", [])
                if segment not in required:
                    required.append(segment)
            node = child

        leaf_name = parts[-1]
        node["properties"][leaf_name] = _leaf_schema(spec)
        if spec.required:
            required = node.setdefault("required", [])
            if leaf_name not in required:
                required.append(leaf_name)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "GIDEON site file",
        **root,
    }


def render_schema() -> str:
    """Serialize the generated schema with stable formatting."""

    return json.dumps(build_schema(), indent=2, sort_keys=True) + "\n"


def write_schema(path: Path | None = None, *, host: Host | None = None) -> Path:
    """Write the schema artifact and return its path."""

    target = path or Path(__file__).parents[2] / "config/site.schema.json"
    (host or RealHost()).write_text(target, render_schema())
    return target


if __name__ == "__main__":
    write_schema()
