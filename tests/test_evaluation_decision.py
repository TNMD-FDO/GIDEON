"""Hand-computed paired metrics keep the comparison reproducible."""

import ast
import math
import unittest
from collections.abc import Mapping
from pathlib import Path

from gideon.evaluation import decision
from gideon.evaluation.results import JSONValue


def _score(metrics: Mapping[str, JSONValue]) -> float | None:
    value = metrics.get("score")
    if isinstance(value, int | float):
        return float(value)
    return None


def _metric(*, higher_is_better: bool = True) -> decision.DecisionMetric:
    return decision.DecisionMetric("score", higher_is_better, _score)


def _from_differences(
    differences: tuple[float, ...], cluster_ids: tuple[str, ...]
) -> decision.PairedDecision:
    case_ids = tuple(f"case-{index}" for index in range(len(differences)))
    return decision.paired_decision(
        {case_id: (difference,) for case_id, difference in zip(case_ids, differences, strict=True)},
        dict.fromkeys(case_ids, (0.0,)),
        dict(zip(case_ids, cluster_ids, strict=True)),
        _metric(),
        against="run-example",
        requested_repeats=decision.DECISION_REPEATS,
        completed_repeats=decision.DECISION_REPEATS,
        digest_equal=True,
    )


class PairedStatistic(unittest.TestCase):
    """Paired differences describe metric changes without retaining case text."""

    def test_singleton_and_shared_clusters_have_hand_computed_errors(self) -> None:
        candidate = {
            "case-a": (1.0,),
            "case-b": (1.0,),
            "case-c": (3.0,),
            "case-d": (3.0,),
            "candidate-only": (8.0,),
        }
        comparand = {
            "case-a": (0.0,),
            "case-b": (0.0,),
            "case-c": (0.0,),
            "case-d": (0.0,),
            "comparand-only": (7.0,),
        }
        metric = _metric()

        singleton_clusters = decision.paired_decision(
            candidate,
            comparand,
            {"case-a": "source-a", "case-b": "source-b", "case-c": "source-c", "case-d": "source-d"},
            metric,
            against="run-example",
            requested_repeats=5,
            completed_repeats=5,
            digest_equal=True,
        )
        # Differences [1, 1, 3, 3] average 2; residuals [-1, -1, 1, 1]
        # have four singleton squares, so sqrt(4) / 4 = 0.5 for either error.
        self.assertEqual(singleton_clusters.mean_difference, 2.0)
        self.assertEqual(singleton_clusters.se_unclustered, 0.5)
        self.assertEqual(singleton_clusters.se_clustered, 0.5)
        self.assertEqual(singleton_clusters.ci_low, 1.02)
        self.assertEqual(singleton_clusters.ci_high, 2.98)
        self.assertEqual(singleton_clusters.verdict, decision.WINS)
        self.assertEqual(singleton_clusters.paired, 4)
        self.assertEqual(singleton_clusters.clusters, 4)
        self.assertEqual(singleton_clusters.candidate_only, 1)
        self.assertEqual(singleton_clusters.comparand_only, 1)

        shared_clusters = decision.paired_decision(
            candidate,
            comparand,
            {"case-a": "source-left", "case-b": "source-left", "case-c": "source-right", "case-d": "source-right"},
            metric,
            against="run-example",
            requested_repeats=5,
            completed_repeats=5,
            digest_equal=False,
        )
        # Each pair of residuals sums to -2 or +2: sqrt(4 + 4) / 4 = sqrt(8) / 4.
        # The 95% interval is 2 +/- 1.96 * sqrt(8) / 4 = [0.6141, 3.3859].
        clustered_error = math.sqrt(8) / 4
        self.assertEqual(shared_clusters.se_unclustered, 0.5)
        assert shared_clusters.se_clustered is not None
        self.assertAlmostEqual(shared_clusters.se_clustered, clustered_error)
        self.assertEqual(shared_clusters.ci_low, 2.0 - decision.Z_95 * clustered_error)
        self.assertEqual(shared_clusters.ci_high, 2.0 + decision.Z_95 * clustered_error)
        self.assertEqual(shared_clusters.verdict, decision.WINS)
        self.assertEqual(shared_clusters.clusters, 2)
        self.assertFalse(shared_clusters.digest_equal)
        self.assertEqual(
            decision.describe(shared_clusters),
            "score vs run-example: 4 paired in 2 clusters, mean +2.0000, "
            "SE +0.7071 (unclustered +0.5000), 95 % [+0.6141, +3.3859]: "
            "wins; candidate-only 1, comparand-only 1",
        )

    def test_lower_values_can_be_oriented_as_better(self) -> None:
        result = decision.paired_decision(
            {"case-a": (3.0,), "case-b": (6.0,)},
            {"case-a": (5.0,), "case-b": (7.0,)},
            {"case-a": "source-a", "case-b": "source-b"},
            _metric(higher_is_better=False),
            against="run-example",
            requested_repeats=2,
            completed_repeats=2,
            digest_equal=True,
        )
        # Raw changes are -2 and -1; reversing polarity gives [2, 1], mean 1.5.
        self.assertEqual(result.mean_difference, 1.5)
        self.assertEqual(result.verdict, decision.WINS)

    def test_repeats_are_averaged_and_undefined_cases_are_not_paired(self) -> None:
        metric = _metric()
        candidate = decision.per_case_values(
            (
                ("case-shared-a", 1, {"score": 2}),
                ("case-shared-a", 2, {"score": 4}),
                ("case-shared-a", 3, {"score": None}),
                ("case-shared-b", 1, {"score": 4}),
                ("case-shared-b", 2, {"score": 6}),
                ("case-candidate-only", 1, {"score": 9}),
                ("case-comparand-only", 1, {"score": None}),
            ),
            metric,
        )
        comparand = decision.per_case_values(
            (
                ("case-shared-a", 1, {"score": 1}),
                ("case-shared-a", 2, {"score": 3}),
                ("case-shared-b", 1, {"score": 7}),
                ("case-shared-b", 2, {"score": 9}),
                ("case-candidate-only", 1, {"score": None}),
                ("case-comparand-only", 1, {"score": 11}),
            ),
            metric,
        )
        self.assertEqual(candidate["case-shared-a"], (2.0, 4.0))
        self.assertNotIn("case-comparand-only", candidate)
        self.assertNotIn("case-candidate-only", comparand)

        result = decision.paired_decision(
            candidate,
            comparand,
            {"case-shared-a": "source-a", "case-shared-b": "source-b"},
            metric,
            against="run-example",
            requested_repeats=5,
            completed_repeats=3,
            digest_equal=True,
        )
        # The paired means are (3 - 2) and (5 - 8), so their mean difference is -1.
        self.assertEqual(result.paired, 2)
        self.assertEqual(result.clusters, 2)
        self.assertEqual(result.candidate_only, 1)
        self.assertEqual(result.comparand_only, 1)
        self.assertEqual(result.mean_difference, -1.0)
        self.assertEqual(result.requested_repeats, 5)
        self.assertEqual(result.completed_repeats, 3)

    def test_three_verdicts_and_fewer_than_two_pairs(self) -> None:
        examples = (
            ((1.0, 1.0, 3.0, 3.0), ("a", "a", "b", "b"), decision.WINS),
            ((-1.0, -1.0, -3.0, -3.0), ("a", "a", "b", "b"), decision.LOSES),
            ((-1.0, 1.0), ("a", "b"), decision.UNDECIDED),
        )
        for differences, cluster_ids, verdict in examples:
            with self.subTest(verdict=verdict):
                self.assertEqual(_from_differences(differences, cluster_ids).verdict, verdict)

        one_pair = decision.paired_decision(
            {"case-shared": (2.0,), "candidate-only": (4.0,)},
            {"case-shared": (1.0,), "comparand-only": (5.0,)},
            {"case-shared": "source-a"},
            _metric(),
            against="run-example",
            requested_repeats=5,
            completed_repeats=1,
            digest_equal=True,
        )
        self.assertEqual(one_pair.paired, 1)
        self.assertEqual(one_pair.mean_difference, 1.0)
        self.assertIsNone(one_pair.se_unclustered)
        self.assertIsNone(one_pair.se_clustered)
        self.assertIsNone(one_pair.ci_low)
        self.assertIsNone(one_pair.ci_high)
        self.assertEqual(one_pair.verdict, decision.UNDECIDED)
        self.assertIn("SE n/a", decision.describe(one_pair))

        no_pairs = decision.paired_decision(
            {"candidate-only": (1.0,)},
            {"comparand-only": (2.0,)},
            {},
            _metric(),
            against="run-example",
            requested_repeats=5,
            completed_repeats=0,
            digest_equal=True,
        )
        self.assertEqual(no_pairs.paired, 0)
        self.assertEqual(no_pairs.clusters, 0)
        self.assertIsNone(no_pairs.mean_difference)
        self.assertEqual(no_pairs.verdict, decision.UNDECIDED)
        self.assertEqual(
            decision.describe(no_pairs),
            "score vs run-example: nothing paired; candidate-only 1, comparand-only 1",
        )

    def test_json_contains_every_field_at_full_precision(self) -> None:
        result = _from_differences(
            (1.0, 1.0, 3.0, 3.0), ("source-left", "source-left", "source-right", "source-right")
        )
        clustered_error = math.sqrt(8) / 4
        expected = {
            "metric": "score",
            "against": "run-example",
            "requested_repeats": 5,
            "completed_repeats": 5,
            "paired": 4,
            "clusters": 2,
            "candidate_only": 0,
            "comparand_only": 0,
            "mean_difference": 2.0,
            "se_unclustered": 0.5,
            "se_clustered": clustered_error,
            "ci_low": 2.0 - decision.Z_95 * clustered_error,
            "ci_high": 2.0 + decision.Z_95 * clustered_error,
            "verdict": decision.WINS,
            "digest_equal": True,
        }
        serialized = decision.to_json(result)
        self.assertEqual(serialized, expected)
        self.assertEqual(set(serialized), set(expected))
        se_json = serialized["se_clustered"]
        assert isinstance(se_json, float)
        self.assertNotEqual(se_json, round(clustered_error, 4))

    def test_module_imports_only_standard_library_and_json_value(self) -> None:
        source = Path(decision.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules: set[str] = set()
        package_imports: list[tuple[str, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imported_modules.add(module)
                if module.startswith("gideon."):
                    package_imports.extend((module, alias.name) for alias in node.names)
        self.assertEqual(
            imported_modules,
            {"collections.abc", "dataclasses", "math", "typing", "gideon.evaluation.results"},
        )
        self.assertEqual(package_imports, [("gideon.evaluation.results", "JSONValue")])


if __name__ == "__main__":
    unittest.main()
