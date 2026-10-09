"""Contracts for the service's one model id and the engine's name behind it."""

import ast
import json
import sys
import unittest
from pathlib import Path

from gideon.api.model import (
    MODEL_NOT_FOUND_ERROR,
    MODEL_NOT_FOUND_STATUS,
    MODEL_OWNER,
    address_completion_body,
    admits_model,
    model_listing,
)

MODEL_ID = "fixture-general"
ENGINE_MODEL = "fixture-engine"


class Admission(unittest.TestCase):
    """A caller must name the one service model before any other work."""

    def test_only_the_accepted_string_in_an_object_is_admitted(self) -> None:
        self.assertTrue(admits_model(json.dumps({"model": MODEL_ID}).encode(), MODEL_ID))
        for body in (
            json.dumps({"model": "another-model"}).encode(),
            json.dumps({"model": ENGINE_MODEL}).encode(),
            b"{}", b'{"model":null}', b'{"model":7}',
            b'["fixture-general"]', b"not JSON",
            b"[" * 1000 + b"0" + b"]" * 1000,
        ):
            with self.subTest(body=body[:40]):
                self.assertFalse(admits_model(body, MODEL_ID))

    def test_refusal_is_fixed_and_names_no_caller_value(self) -> None:
        self.assertEqual(MODEL_NOT_FOUND_STATUS, 404)
        self.assertEqual(MODEL_NOT_FOUND_ERROR["error"]["code"], "model_not_found")
        self.assertEqual(MODEL_NOT_FOUND_ERROR["error"]["type"], "invalid_request_error")
        self.assertEqual(MODEL_NOT_FOUND_ERROR["error"]["param"], "model")
        self.assertNotIn("caller-secret", json.dumps(MODEL_NOT_FOUND_ERROR))


class Addressing(unittest.TestCase):
    """Only the upstream model changes after admission and instruction."""

    def test_key_order_and_compact_form(self) -> None:
        body = b'{ "temperature": 0.4, "model": "fixture-general", "messages": [] }'
        self.assertEqual(
            address_completion_body(body, ENGINE_MODEL),
            b'{"temperature":0.4,"model":"fixture-engine","messages":[]}',
        )

    def test_unreadable_body_stands(self) -> None:
        self.assertEqual(address_completion_body(b"not JSON", ENGINE_MODEL), b"not JSON")


class Listing(unittest.TestCase):
    """The service publishes one entry only when the engine lists its name."""

    def test_engine_entry_is_reduced_to_the_service_entry(self) -> None:
        body = json.dumps({"object": "list", "data": [
            {"id": "other", "created": 12},
            {"id": ENGINE_MODEL, "created": 42, "owned_by": "engine-secret", "extra": "secret"},
        ]}).encode()
        self.assertEqual(json.loads(model_listing(body, ENGINE_MODEL, MODEL_ID) or b""), {
            "object": "list", "data": [{
                "id": MODEL_ID, "object": "model", "created": 42, "owned_by": MODEL_OWNER,
            }],
        })
        self.assertNotIn(ENGINE_MODEL.encode(), model_listing(body, ENGINE_MODEL, MODEL_ID) or b"")

    def test_missing_created_uses_zero(self) -> None:
        for created in (None, "42", True):
            with self.subTest(created=created):
                body = json.dumps({"data": [{"id": ENGINE_MODEL, "created": created}]}).encode()
                listing = model_listing(body, ENGINE_MODEL, MODEL_ID)
                self.assertIsNotNone(listing)
                self.assertEqual(json.loads(listing or b"")["data"][0]["created"], 0)

    def test_unreadable_or_missing_engine_is_unavailable(self) -> None:
        for body in (b"not JSON", b"[]", b"{}", b'{"data":{}}', b'{"data":[]}',
                     b'{"data":[{"id":"other"}]}',
                     b"[" * 1000 + b"0" + b"]" * 1000):
            with self.subTest(body=body[:40]):
                self.assertIsNone(model_listing(body, ENGINE_MODEL, MODEL_ID))


class ImportBoundary(unittest.TestCase):
    """The pure model rule uses the standard library and its error builder."""

    def test_imports_are_standard_library(self) -> None:
        source = Path(__file__).resolve().parents[1] / "gideon/api/model.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.level == 1
                and node.module == "errors"
                and len(node.names) == 1
                and node.names[0].name == "error_body"
                and node.names[0].asname is None
            ):
                continue
            names = (
                [alias.name for alias in node.names] if isinstance(node, ast.Import)
                else [node.module or ""] if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                self.assertTrue(
                    (not isinstance(node, ast.ImportFrom) or node.level == 0)
                    and name.split(".")[0] in sys.stdlib_module_names,
                    f"model.py imports disallowed module {name}; the rule stays a pure parse plus the service error builder",
                )
