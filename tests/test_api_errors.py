"""Contracts for the service's fixed OpenAI-shaped error bodies."""

import ast
import json
import sys
import unittest
from pathlib import Path

from gideon.api.errors import error_body


class ErrorBody(unittest.TestCase):
    def test_key_order_and_default_param(self) -> None:
        body = error_body("Fictitious failure.", error_type="server_error", code="fixture")

        self.assertEqual(list(body), ["error"])
        self.assertEqual(list(body["error"]), ["message", "type", "param", "code"])
        self.assertEqual(body["error"]["param"], None)
        self.assertEqual(
            error_body(
                "Fictitious failure.",
                error_type="invalid_request_error",
                code="fixture",
                param="model",
            )["error"]["param"],
            "model",
        )

    def test_compact_serialization_preserves_the_fixed_body(self) -> None:
        body = error_body(
            "Incorrect API key provided.",
            error_type="invalid_request_error",
            code="invalid_api_key",
        )
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")

        self.assertEqual(
            encoded,
            b'{"error":{"message":"Incorrect API key provided.",'
            b'"type":"invalid_request_error","param":null,"code":"invalid_api_key"}}',
        )
        self.assertEqual(json.loads(encoded), body)


class ImportBoundary(unittest.TestCase):
    def test_imports_are_standard_library(self) -> None:
        source = Path(__file__).resolve().parents[1] / "gideon/api/errors.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = (
                [alias.name for alias in node.names] if isinstance(node, ast.Import)
                else [node.module or ""] if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                self.assertIn(
                    name.split(".")[0], sys.stdlib_module_names,
                    f"errors.py imports non-standard module {name}; the builder stays standard-library only",
                )
