"""Contracts for the engine-verification sample loader and artifact."""

import hashlib
import os
import subprocess
import unittest
from collections.abc import Mapping
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from gideon.host.enginesample import (
    FrontendCase,
    FrontendSection,
    NeedleCase,
    Sample,
    SampleError,
    SampleLoadResult,
    SmokeCase,
    StructuredCase,
    blocks_for,
    build_needle_prompt,
    check_schema,
    load_sample,
    render_errors,
    validate,
)
from gideon.host.sysio import Command, PathLike

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "eval/engine-verify/sample.yaml"
SITE_EXAMPLE = ROOT / "config/site.example.yaml"


class DictHost:
    """Dict-backed Host seam for sample read and refusal tests."""

    def __init__(self, files: Mapping[str, str] | None = None) -> None:
        self.files = dict(files or {})

    def run(
        self,
        argv: Command,
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, input, cwd, env, timeout, passthrough
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(
        self,
        path: PathLike,
        text: str,
        *,
        encoding: str = "utf-8",
        mode: int = 0o644,
    ) -> None:
        del encoding, mode
        self.files[os.fspath(path)] = text

    def exists(self, path: PathLike) -> bool:
        return os.fspath(path) in self.files

    def listdir(self, path: PathLike) -> list[str]:
        del path
        raise NotImplementedError

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del path, missing_ok
        raise NotImplementedError

    def stat(self, path: PathLike) -> os.stat_result:
        del path
        raise NotImplementedError

    def chmod(self, path: PathLike, mode: int) -> None:
        del path, mode
        raise NotImplementedError

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        del path, uid, gid
        raise NotImplementedError

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del path, mode, parents, exist_ok
        raise NotImplementedError

    def geteuid(self) -> int:
        return 0


class SampleTests(unittest.TestCase):
    def test_committed_sample_loads_with_text_hash_and_fields(self) -> None:
        result = load_sample(SAMPLE, host=DictHost({str(SAMPLE): SAMPLE.read_text()}))

        self.assertTrue(result.ok, render_errors(result.errors))
        self.assertIsInstance(result.sample, Sample)
        assert result.sample is not None
        self.assertEqual(
            result.sample.sha256,
            hashlib.sha256(SAMPLE.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            result.sample.smoke,
            SmokeCase(
                id="open-tabletop-ideas",
                prompt=(
                    "Write three short paragraphs about turning a box of mismatched "
                    "buttons into a playful tabletop game. Keep the ideas concrete "
                    "and imaginative, and give the game a memorable name."
                ),
                max_tokens=256,
            ),
        )
        self.assertEqual(
            result.sample.needle,
            NeedleCase(
                id="archive-locker-recall",
                filler=(
                    "A quiet workshop keeps small inventions in labeled drawers. "
                    "Each afternoon, the caretaker rearranges ordinary materials "
                    "into playful experiments. The room is calm, practical, and "
                    "entirely made up for this exercise."
                ),
                planted="The archive locker code is 4471-ORCHID-92.",
                question="Reply with the archive locker code alone and nothing else.",
                expected="4471-ORCHID-92",
                depth=0.5,
            ),
        )
        self.assertEqual(
            result.sample.structured,
            StructuredCase(
                id="plan-shaped-json",
                prompt=(
                    "Create a short JSON plan for cataloguing a small workshop's "
                    "drawers over a week. Use the requested shape and answer with "
                    "JSON only."
                ),
                schema_name="gideon_plan",
                schema={
                    "type": "object",
                    "properties": {
                        "objective": {"type": "string", "minLength": 1},
                        "route": {"type": "string", "enum": ["map", "sort", "label"]},
                        "steps": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 5,
                        },
                        "confidence": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 100,
                        },
                    },
                    "required": ["objective", "route", "steps", "confidence"],
                    "additionalProperties": False,
                },
            ),
        )
        document = yaml.safe_load(SAMPLE.read_text())
        self.assertIsInstance(document, Mapping)
        assert isinstance(document, Mapping)
        frontend = document["frontend"]
        self.assertIsInstance(frontend, Mapping)
        assert isinstance(frontend, Mapping)
        positives = frontend["positives"]
        self.assertIsInstance(positives, list)
        assert isinstance(positives, list)
        expected_frontend = FrontendSection(
            positives=tuple(
                FrontendCase(id=case["id"], prompt=case["prompt"])
                for case in positives
                if isinstance(case, Mapping)
            ),
            trip=FrontendCase(
                id=frontend["trip"]["id"],
                prompt=frontend["trip"]["prompt"],
            ),
        )
        self.assertEqual(result.sample.frontend, expected_frontend)


class RefusalTests(unittest.TestCase):
    def load_text(self, text: str) -> SampleLoadResult:
        return load_sample(
            "/tmp/engine-sample.yaml",
            host=DictHost({"/tmp/engine-sample.yaml": text}),
        )

    def assert_refuses(self, text: str, fragment: str) -> None:
        result = self.load_text(text)
        self.assertFalse(result.ok)
        self.assertIsNone(result.sample)
        self.assertTrue(result.errors)
        self.assertIn(fragment, render_errors(result.errors))
        for error in result.errors:
            self.assertIsInstance(error, SampleError)
            self.assertTrue(error.fix)
        rendered = render_errors(result.errors)
        self.assertEqual(len(rendered.splitlines()), len(result.errors))
        for line in rendered.splitlines():
            self.assertIn("Fix:", line)

    def valid_document(self) -> dict[str, object]:
        return {
            "version": 1,
            "needle": {
                "id": "fixture-needle",
                "filler": "An invented neutral paragraph.",
                "planted": "The locker code is FICTITIOUS-42.",
                "question": "Return the code alone.",
                "expected": "FICTITIOUS-42",
                "depth": 0.5,
            },
            "structured": {
                "id": "fixture-structured",
                "prompt": "Return JSON.",
                "schema_name": "fixture_schema",
                "schema": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
            "smoke": {"id": "fixture", "prompt": "Say something.", "max_tokens": 1},
            "frontend": {
                "positives": [{"id": "fixture-positive", "prompt": "A fixture prompt."}],
                "trip": {"id": "fixture-trip", "prompt": "Another fixture prompt."},
            },
        }

    def dump(self, document: object) -> str:
        return yaml.safe_dump(document, sort_keys=False)

    def test_missing_file_refuses_with_fix(self) -> None:
        result = load_sample("/tmp/missing-engine-sample.yaml", host=DictHost())
        self.assertFalse(result.ok)
        self.assertIn("missing", render_errors(result.errors))
        self.assertIn("Fix:", render_errors(result.errors))

    def test_invalid_yaml_refuses_with_fix(self) -> None:
        self.assert_refuses("version: [", "YAML parse error")

    def test_non_mapping_document_refuses_with_fix(self) -> None:
        self.assert_refuses("- one\n- two\n", "document")

    def test_wrong_version_refuses_with_fix(self) -> None:
        document = self.valid_document()
        document["version"] = 2
        self.assert_refuses(self.dump(document), "version")

    def test_unknown_top_level_key_refuses_with_fix(self) -> None:
        document = self.valid_document()
        document["verison"] = 1
        self.assert_refuses(self.dump(document), "verison")

    def test_unknown_nested_key_refuses_with_fix(self) -> None:
        document = self.valid_document()
        smoke = document["smoke"]
        assert isinstance(smoke, dict)
        smoke["extra"] = True
        self.assert_refuses(self.dump(document), "smoke.extra")

    def test_frontend_refuses_missing_or_bad_shapes(self) -> None:
        document = self.valid_document()
        del document["frontend"]
        self.assert_refuses(self.dump(document), "frontend")

        document = self.valid_document()
        document["frontend"] = "not a mapping"
        self.assert_refuses(self.dump(document), "frontend")

        document = self.valid_document()
        frontend = document["frontend"]
        assert isinstance(frontend, dict)
        del frontend["positives"]
        self.assert_refuses(self.dump(document), "frontend.positives")

        document = self.valid_document()
        frontend = document["frontend"]
        assert isinstance(frontend, dict)
        frontend["positives"] = []
        self.assert_refuses(self.dump(document), "frontend.positives")

        document = self.valid_document()
        frontend = document["frontend"]
        assert isinstance(frontend, dict)
        del frontend["trip"]
        self.assert_refuses(self.dump(document), "frontend.trip")

    def test_frontend_unknown_keys_name_the_nearest_valid_key(self) -> None:
        document = self.valid_document()
        frontend = document["frontend"]
        assert isinstance(frontend, dict)
        frontend["positivs"] = frontend.pop("positives")
        self.assert_refuses(self.dump(document), "nearest valid key is 'positives'")

        document = self.valid_document()
        frontend = document["frontend"]
        assert isinstance(frontend, dict)
        positives = frontend["positives"]
        assert isinstance(positives, list)
        case = positives[0]
        assert isinstance(case, dict)
        case["promt"] = case.pop("prompt")
        self.assert_refuses(self.dump(document), "nearest valid key is 'prompt'")

    def test_frontend_refuses_prompt_id_and_duplicate_violations(self) -> None:
        cases = (
            ("prompt", 7, "frontend.positives[0].prompt"),
            ("prompt", "", "frontend.positives[0].prompt"),
            ("id", "Not-a-row-id", "frontend.positives[0].id"),
        )
        for key, value, path in cases:
            with self.subTest(key=key, value=value):
                document = self.valid_document()
                frontend = document["frontend"]
                assert isinstance(frontend, dict)
                positives = frontend["positives"]
                assert isinstance(positives, list)
                case = positives[0]
                assert isinstance(case, dict)
                case[key] = value
                self.assert_refuses(self.dump(document), path)

        document = self.valid_document()
        frontend = document["frontend"]
        assert isinstance(frontend, dict)
        trip = frontend["trip"]
        assert isinstance(trip, dict)
        positives = frontend["positives"]
        assert isinstance(positives, list)
        first = positives[0]
        assert isinstance(first, dict)
        trip["id"] = first["id"]
        self.assert_refuses(self.dump(document), "must be unique within frontend")

    def test_missing_smoke_key_refuses_with_fix(self) -> None:
        self.assert_refuses(self.dump({"version": 1}), "smoke")

    def test_non_string_id_refuses_with_fix(self) -> None:
        document = self.valid_document()
        smoke = document["smoke"]
        assert isinstance(smoke, dict)
        smoke["id"] = 7
        self.assert_refuses(self.dump(document), "smoke.id")

    def test_non_string_prompt_refuses_with_fix(self) -> None:
        document = self.valid_document()
        smoke = document["smoke"]
        assert isinstance(smoke, dict)
        smoke["prompt"] = ["not", "text"]
        self.assert_refuses(self.dump(document), "smoke.prompt")

    def test_non_positive_max_tokens_refuses_with_fix(self) -> None:
        for value in (0, -1):
            with self.subTest(value=value):
                document = self.valid_document()
                smoke = document["smoke"]
                assert isinstance(smoke, dict)
                smoke["max_tokens"] = value
                self.assert_refuses(self.dump(document), "smoke.max_tokens")

    def test_non_integer_max_tokens_refuses_with_fix(self) -> None:
        for value in (1.5, True, "256"):
            with self.subTest(value=value):
                document = self.valid_document()
                smoke = document["smoke"]
                assert isinstance(smoke, dict)
                smoke["max_tokens"] = value
                self.assert_refuses(self.dump(document), "smoke.max_tokens")

    def test_needle_refusals(self) -> None:
        cases = (
            ("expected", "not planted", "needle.expected"),
            ("depth", 0, "needle.depth"),
            ("depth", 1, "needle.depth"),
            ("depth", 0.0, "needle.depth"),
            ("depth", 1.0, "needle.depth"),
            ("depth", 1.5, "needle.depth"),
        )
        for key, value, path in cases:
            with self.subTest(key=key, value=value):
                document = self.valid_document()
                needle = document["needle"]
                assert isinstance(needle, dict)
                needle[key] = value
                self.assert_refuses(self.dump(document), path)

        for missing in ("id", "filler", "planted", "question", "expected", "depth"):
            with self.subTest(missing=missing):
                document = self.valid_document()
                needle = document["needle"]
                assert isinstance(needle, dict)
                del needle[missing]
                self.assert_refuses(self.dump(document), f"needle.{missing}")

        document = self.valid_document()
        needle = document["needle"]
        assert isinstance(needle, dict)
        needle["unexpected"] = True
        self.assert_refuses(self.dump(document), "needle.unexpected")

    def test_blocks_and_needle_prompt(self) -> None:
        case = NeedleCase(
            "fixture-needle",
            "A neutral filler sentence.",
            "The locker code is FICTITIOUS-42.",
            "Return the code alone.",
            "FICTITIOUS-42",
            0.5,
        )
        self.assertEqual(blocks_for(100, 20, 10), 5)
        self.assertEqual(blocks_for(5, 20, 10), 1)
        messages = build_needle_prompt(case, 4)
        self.assertEqual(len(messages), 1)
        content = messages[0]["content"]
        paragraphs = content.splitlines()
        self.assertEqual(paragraphs[-1], case.question)
        self.assertEqual(paragraphs[2], case.planted)
        self.assertEqual(paragraphs[:2], [
            "Paragraph 1. A neutral filler sentence.",
            "Paragraph 2. A neutral filler sentence.",
        ])
        self.assertEqual(len(set(paragraphs[:4])), 4)


class SampleHygiene(unittest.TestCase):
    def test_sample_has_no_site_identity_or_dollar_placeholder(self) -> None:
        text = SAMPLE.read_text()
        site = yaml.safe_load(SITE_EXAMPLE.read_text())
        assert isinstance(site, dict)
        office = site["office"]
        alerts = site["alerts"]
        assert isinstance(office, dict)
        assert isinstance(alerts, dict)
        smtp = alerts["smtp"]
        recipients = alerts["recipients"]
        assert isinstance(smtp, dict)
        assert isinstance(recipients, list)
        mail_domains = {
            address.rsplit("@", 1)[1]
            for address in [smtp["from"], *recipients]
            if isinstance(address, str) and "@" in address
        }

        self.assertNotIn(site["hostname"], text)
        self.assertNotIn(office["name"], text)
        for domain in mail_domains:
            self.assertNotIn(domain, text)
        self.assertNotIn("$", text)


class SchemaTests(unittest.TestCase):
    def assert_schema_refused(self, schema: object, fragment: str) -> None:
        errors: list[SampleError] = []
        check_schema(schema, "structured.schema", errors)
        self.assertTrue(errors)
        self.assertIn(fragment, render_errors(errors))
        for error in errors:
            self.assertTrue(error.fix)

    def test_check_schema_refuses_unsupported_and_bad_shapes(self) -> None:
        base = {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
        for schema, fragment in (
            ({**base, "pattern": "x"}, "unsupported schema keyword"),
            ({**base, "type": "string"}, "top-level schema type must be object"),
            ({**base, "additionalProperties": True}, "additionalProperties must be false"),
            ({**base, "additionalProperties": {}}, "additionalProperties must be false"),
            ({**base, "required": ["missing"]}, "missing from properties"),
            ({**base, "items": []}, "expected a mapping"),
            ({**base, "minItems": 1.5}, "non-negative integer"),
        ):
            with self.subTest(fragment=fragment):
                self.assert_schema_refused(schema, fragment)

    def test_nested_object_schema_is_accepted_and_typeless_object_refused(self) -> None:
        nested = {
            "type": "object",
            "properties": {
                "inner": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
            "required": ["inner"],
            "additionalProperties": False,
        }
        errors: list[SampleError] = []
        check_schema(nested, "structured.schema", errors)
        self.assertEqual(errors, [])
        self.assertEqual(validate({"inner": {"value": "x"}}, nested), ())
        self.assertEqual(validate({"inner": {"value": "x", "extra": 1}}, nested), ("/inner/<additional-property>",))
        typeless = {
            "type": "object",
            "properties": {"inner": {"properties": {"value": {"type": "string"}}}},
        }
        self.assert_schema_refused(typeless, "object keywords require type object")
        self.assertEqual(
            validate({"x": 1}, {"type": "object", "additionalProperties": False}), ("/<additional-property>",)
        )

    def test_validate_reports_supported_keyword_paths_only(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 2, "maxLength": 4},
                "kind": {"type": "string", "enum": ["map", "sort"]},
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 2,
                },
                "confidence": {"type": "integer", "minimum": 0, "maximum": 10},
                "ratio": {"type": "number", "minimum": 0.1, "maximum": 1.0},
                "enabled": {"type": "boolean"},
            },
            "required": ["name", "kind", "steps", "confidence", "ratio", "enabled"],
            "additionalProperties": False,
        }
        valid = {
            "name": "box",
            "kind": "map",
            "steps": ["one"],
            "confidence": 5,
            "ratio": 0.5,
            "enabled": True,
        }
        self.assertEqual(validate(valid, schema), ())
        self.assertEqual(
            validate(
                {
                    "name": "",
                    "kind": "other",
                    "steps": [1, "two", "three"],
                    "confidence": True,
                    "ratio": 2,
                    "enabled": "yes",
                    "extra": "value",
                },
                schema,
            ),
            ("/name", "/kind", "/steps", "/steps/0", "/confidence", "/ratio", "/enabled", "/<additional-property>"),
        )
        self.assertEqual(validate({}, schema), ("/name", "/kind", "/steps", "/confidence", "/ratio", "/enabled"))
        self.assertEqual(validate(True, {"type": "integer"}), ("",))
        self.assertEqual(validate(True, {"type": "number"}), ("",))
        self.assertNotIn("value", validate({"value": 1}, {"type": "string"}))
