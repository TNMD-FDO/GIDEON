"""Contract tests for the generated site-file JSON Schema."""

import json
import unittest
from pathlib import Path

from gideon.host.site import FIELD_REGISTRY
from gideon.host.site_schema import render_schema

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = ROOT / "config/site.schema.json"


def schema_at(schema: dict, path: str) -> dict:
    current = schema
    for part in path.split("."):
        current = current["properties"][part]
    return current


class SiteSchema(unittest.TestCase):
    def test_rendering_matches_committed_artifact(self) -> None:
        self.assertEqual(render_schema(), SCHEMA_FILE.read_text(encoding="utf-8"))

    def test_registry_types_defaults_enums_and_required_arrays_match(self) -> None:
        schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
        expected_required: dict[str, list[str]] = {}
        for spec in FIELD_REGISTRY:
            if spec.required:
                parts = spec.path.split(".")
                for depth, child in enumerate(parts):
                    parent = ".".join(parts[:depth])
                    required = expected_required.setdefault(parent, [])
                    if child not in required:
                        required.append(child)

            leaf = schema_at(schema, spec.path)
            self.assertEqual(leaf["description"], spec.description)
            kind = spec.kind
            if kind == "string" or kind == "non-empty string" or kind == "timezone":
                self.assertEqual(leaf["type"], "string")
            elif kind == "int":
                self.assertEqual(leaf["type"], "integer")
            elif kind == "enum":
                self.assertEqual(leaf["type"], "string")
            elif (
                kind == "string list"
                or kind == "non-empty string list"
                or kind == "CIDR list"
            ):
                self.assertEqual(leaf["type"], "array")
                self.assertEqual(leaf["items"]["type"], "string")
            else:
                self.fail(f"unhandled field kind: {kind}")

            if spec.allowed_values:
                if kind in ("string list", "non-empty string list", "CIDR list"):
                    self.assertEqual(leaf["items"]["enum"], list(spec.allowed_values))
                else:
                    self.assertEqual(leaf["enum"], list(spec.allowed_values))
            else:
                self.assertNotIn("enum", leaf)

            if spec.pattern is not None:
                self.assertEqual(leaf["items"]["pattern"], spec.pattern)

            if spec.required or spec.derived:
                self.assertNotIn("default", leaf)
            else:
                self.assertEqual(leaf["default"], spec.default)

            if spec.minimum is None:
                self.assertNotIn("minimum", leaf)
            else:
                self.assertEqual(leaf["minimum"], spec.minimum)

        self.assertEqual(
            schema_at(schema, "web.search")["enum"], ["on", "off"]
        )
        self.assertNotIn(
            "default", schema_at(schema, "auth.ldap.search_base")
        )

        self._assert_required_arrays(schema, expected_required)

    def test_additional_properties_is_false_at_every_object_level(self) -> None:
        schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
        self._assert_closed_objects(schema)

    def _assert_required_arrays(
        self, node: dict, expected: dict[str, list[str]], path: str = ""
    ) -> None:
        required = expected.get(path)
        if required:
            self.assertEqual(node.get("required"), required, path)
        else:
            self.assertNotIn("required", node, path)
        for name, child in node["properties"].items():
            if child.get("type") == "object":
                child_path = f"{path}.{name}" if path else name
                self._assert_required_arrays(child, expected, child_path)

    def _assert_closed_objects(self, node: dict) -> None:
        if node.get("type") != "object":
            return
        self.assertIs(node.get("additionalProperties"), False)
        for child in node["properties"].values():
            self._assert_closed_objects(child)
