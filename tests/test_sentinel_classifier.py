"""Hosted tests for the search sentinel's engine comparison, residual classifier, and marker."""

import importlib.util
import re
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = ROOT / "tests/contract/search_sentinel.py"


def load_contract() -> Any:
    spec = importlib.util.spec_from_file_location("search_sentinel_contract", CONTRACT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CONTRACT = load_contract()


class SentinelClassifierTests(unittest.TestCase):
    def test_only_the_aiohttp_standin_error_is_the_residual(self) -> None:
        accepted = (
            "aiohttp.client_exceptions.ClientResponseError: 500, "
            "message='Internal Server Error', "
            "url=URL('http://stub:8000/searxng/search?q="
            "gideon-sentinel-abcdef123456')"
        )
        self.assertTrue(CONTRACT.is_frontend_residual(accepted))
        self.assertFalse(
            CONTRACT.is_frontend_residual(
                "GET http://stub:8000/searxng/search?q=gideon-sentinel-abcdef123456"
            )
        )
        self.assertFalse(
            CONTRACT.is_frontend_residual(
                "DEBUG routers.retrieval queries=['gideon-sentinel-abcdef123456']"
            )
        )
        self.assertFalse(
            CONTRACT.is_frontend_residual(
                "ErrorContext: bing returned HTTP 500 for the request"
            )
        )

    def test_sentinel_has_the_fixed_prefix_and_twelve_hex_characters(self) -> None:
        sentinel = CONTRACT.mint_sentinel()
        self.assertRegex(sentinel, re.compile(r"^gideon-sentinel-[0-9a-f]{12}$"))


class EngineConfigTests(unittest.TestCase):
    def test_equal_lists_have_no_findings_regardless_of_order_or_other_fields(self) -> None:
        self.assertEqual(
            CONTRACT.engine_config_findings(
                ["example-first", "example-second"],
                {
                    "engines": [
                        {"name": "example-second", "enabled": True, "ignored": None},
                        {"name": "example-first", "enabled": True},
                    ],
                    "ignored": "not an engine list",
                },
            ),
            [],
        )

    def test_an_omitted_engine_is_named(self) -> None:
        self.assertEqual(
            CONTRACT.engine_config_findings(
                ["example-first", "example-second"],
                {"engines": [{"name": "example-first", "enabled": True}]},
            ),
            ["rendered engine 'example-second' is omitted from /config"],
        )

    def test_an_extra_engine_is_named(self) -> None:
        self.assertEqual(
            CONTRACT.engine_config_findings(
                ["example-first"],
                {"engines": [
                    {"name": "example-first", "enabled": True},
                    {"name": "example-extra", "enabled": True},
                ]},
            ),
            ["/config lists engine 'example-extra' that the site does not render"],
        )

    def test_enabled_must_be_true(self) -> None:
        entries = [
            {"name": "example-first", "enabled": value}
            for value in (False, None, 0, 1, "true")
        ]
        entries.append({"name": "example-first"})
        for entry in entries:
            with self.subTest(entry=entry):
                self.assertEqual(
                    CONTRACT.engine_config_findings(["example-first"], {"engines": [entry]}),
                    ["/config engine 'example-first' is not enabled"],
                )

    def test_a_missing_or_unreadable_engine_list_is_a_finding(self) -> None:
        bodies: tuple[object, ...] = (
            None, [], "unreadable", {},
            {"engines": None}, {"engines": {}}, {"engines": "unreadable"},
        )
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(
                    CONTRACT.engine_config_findings(["example-first"], body),
                    ["SearXNG /config has no readable engine list"],
                )

    def test_an_unreadable_engine_entry_is_a_finding(self) -> None:
        entries: tuple[object, ...] = (
            None, "unreadable", {}, {"name": []}, {"name": ""}, {"name": 42},
        )
        for entry in entries:
            with self.subTest(entry=entry):
                self.assertEqual(
                    CONTRACT.engine_config_findings(["example-first"], {"engines": [entry]}),
                    ["SearXNG /config has an unreadable engine entry"],
                )

    def test_an_empty_list_omits_every_rendered_engine(self) -> None:
        self.assertEqual(
            CONTRACT.engine_config_findings(["example-first", "example-second"], {"engines": []}),
            [
                "rendered engine 'example-first' is omitted from /config",
                "rendered engine 'example-second' is omitted from /config",
            ],
        )


if __name__ == "__main__":
    unittest.main()
