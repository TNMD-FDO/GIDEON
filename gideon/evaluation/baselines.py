"""Named eval baselines and the retrieval configuration hash each run expects.

Section 18.5's off states are rerank off/off, abstention floor zero, and query
planning off. Each ticket adding an LLM-touching key to the turn's whitelist
adds its off value here.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from gideon.host.report import Problem
from gideon.turn.config import OverrideRefusal, config_hash, effective_config

BASELINES: Final = MappingProxyType({"off": MappingProxyType({
    "query_plan": "off", "rerank.stage1": "off", "rerank.stage2": "off",
})})


def expected_config_hash(overrides: Mapping[str, object] | None, *, reranker: bool) -> str | Problem:
    """Return the hash the checkout expects, or a content-free override refusal."""

    effective = effective_config(overrides, reranker=reranker)
    if isinstance(effective, OverrideRefusal):
        return Problem(
            f"the checkout's retrieval configuration refused the overrides ({effective.state})",
            "Use overrides allowed by this checkout's retrieval whitelist, then retry.",
        )
    return config_hash(effective)
