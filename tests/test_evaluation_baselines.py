"""Named retrieval baselines and the checkout's whitelist contract."""

import ast
import sys
import unittest
from pathlib import Path
from types import ModuleType

from test_host_import_boundary import absolute_targets, iter_imports, resolve_internal

from gideon.evaluation import baselines
from gideon.host.report import Problem
from gideon.turn.config import (
    RETRIEVAL_CONFIG,
    OverrideRefusal,
    config_hash,
    effective_config,
)


def assert_import_boundary(test: unittest.TestCase, module: ModuleType, allowed: set[str]) -> None:
    assert module.__file__ is not None
    path = Path(module.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in iter_imports(tree, module_level_only=False):
        for name in absolute_targets(node, path):
            root = name.partition(".")[0]
            if root == "gideon" and resolve_internal(name) is None:
                continue
            with test.subTest(module=module.__name__, name=name, line=node.lineno):
                test.assertTrue(root in sys.stdlib_module_names or name in allowed, name)


class Baselines(unittest.TestCase):
    def test_every_named_baseline_is_accepted_and_hashed_by_the_turn(self) -> None:
        self.assertEqual(set(baselines.BASELINES), {"off"})
        self.assertEqual(baselines.BASELINES["off"], {
            "query_plan": "off", "rerank.stage1": "off", "rerank.stage2": "off",
        })
        for name, overrides in baselines.BASELINES.items():
            for reranker in (True, False):
                with self.subTest(name=name, reranker=reranker):
                    self.assertLessEqual(overrides.keys(), RETRIEVAL_CONFIG.keys())
                    effective = effective_config(overrides, reranker=reranker)
                    assert not isinstance(effective, OverrideRefusal)
                    self.assertEqual(baselines.expected_config_hash(overrides, reranker=reranker), config_hash(effective))

    def test_no_overrides_uses_the_configuration_shipped_for_the_stack(self) -> None:
        self.assertEqual(baselines.expected_config_hash(None, reranker=True), config_hash(RETRIEVAL_CONFIG))
        off = {**RETRIEVAL_CONFIG, "rerank.stage2": "off"}
        self.assertEqual(baselines.expected_config_hash(None, reranker=False), config_hash(off))
        self.assertIsInstance(baselines.expected_config_hash({"rerank.stage2": "on"}, reranker=False), Problem)

    def test_controlling_reservation_override_is_hashed_with_the_shipped_configuration(self) -> None:
        overrides = {"reservations.controlling": 1}
        self.assertEqual(baselines.expected_config_hash(overrides, reranker=True), config_hash({**RETRIEVAL_CONFIG, **overrides}))

    def test_refused_override_does_not_leak_its_key_or_value(self) -> None:
        sentinel = "Fictitious private override text"
        for overrides in (
            {sentinel: True}, {"query_plan": sentinel},
            {"reservations.exact_object": sentinel}, {"reservations.controlling": sentinel},
        ):
            with self.subTest(overrides=overrides):
                refusal = effective_config(overrides, reranker=True)
                assert isinstance(refusal, OverrideRefusal)
                problem = baselines.expected_config_hash(overrides, reranker=True)
                assert isinstance(problem, Problem)
                self.assertIn(refusal.state, problem.problem)
                self.assertIn("whitelist", problem.fix)
                self.assertNotIn(sentinel, repr(problem))

    def test_imports_stay_within_the_turn_config_and_shared_problem_type(self) -> None:
        assert_import_boundary(self, baselines, {"gideon.turn.config", "gideon.host.report"})
