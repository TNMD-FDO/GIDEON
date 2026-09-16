"""Hosted tests for the search sentinel's residual classifier and marker."""

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


if __name__ == "__main__":
    unittest.main()
