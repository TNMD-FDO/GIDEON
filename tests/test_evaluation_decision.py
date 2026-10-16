"""Hand-computed paired metrics keep the comparison reproducible."""

import ast
import math
import unittest
from collections.abc import Mapping
from pathlib import Path

from gideon.evaluation import decision, studentt
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
        differences: tuple[float, ...] = (1.0,) * 10 + (3.0,) * 10
        candidate = {f"case-{index}": (value,) for index, value in enumerate(differences)}
        comparand = dict.fromkeys(candidate, (0.0,))
        clusters = {case_id: case_id for case_id in candidate}
        candidate["candidate-only"] = (8.0,)
        comparand["comparand-only"] = (7.0,)
        metric = _metric()

        singleton_clusters = decision.paired_decision(
            candidate,
            comparand,
            clusters,
            metric,
            against="run-example",
            requested_repeats=5,
            completed_repeats=5,
            digest_equal=True,
        )
        # Ten differences at 1 and ten at 3 average 2, with residuals +/-1.
        # Twenty singleton squares give sqrt(20) / 20 for either error.
        singleton_error = math.sqrt(20) / 20
        critical_value = studentt.two_sided_quantile(decision.CONFIDENCE, 19)
        self.assertEqual(singleton_clusters.mean_difference, 2.0)
        self.assertEqual(singleton_clusters.se_unclustered, singleton_error)
        self.assertEqual(singleton_clusters.se_clustered, singleton_error)
        self.assertEqual(singleton_clusters.critical_value, critical_value)
        self.assertAlmostEqual(critical_value, 2.093, places=3)
        self.assertEqual(singleton_clusters.ci_low, 2.0 - critical_value * singleton_error)
        self.assertEqual(singleton_clusters.ci_high, 2.0 + critical_value * singleton_error)
        self.assertEqual(singleton_clusters.verdict, decision.WINS)
        self.assertEqual(singleton_clusters.paired, 20)
        self.assertEqual(singleton_clusters.clusters, 20)
        self.assertEqual(singleton_clusters.cluster_floor, 20)
        self.assertEqual(singleton_clusters.candidate_only, 1)
        self.assertEqual(singleton_clusters.comparand_only, 1)

        differences = (1.0,) * 20 + (3.0,) * 20
        candidate = {f"case-{index}": (value,) for index, value in enumerate(differences)}
        comparand = dict.fromkeys(candidate, (0.0,))
        clusters = {f"case-{index}": f"source-{index // 2}" for index in range(40)}
        candidate["candidate-only"] = (8.0,)
        comparand["comparand-only"] = (7.0,)
        shared_clusters = decision.paired_decision(
            candidate,
            comparand,
            clusters,
            metric,
            against="run-example",
            requested_repeats=5,
            completed_repeats=5,
            digest_equal=False,
        )
        # Each of twenty pairs sums to -2 or +2: sqrt(20 * 4) / 40.
        clustered_error = math.sqrt(80) / 40
        self.assertEqual(shared_clusters.se_unclustered, math.sqrt(40) / 40)
        assert shared_clusters.se_clustered is not None
        self.assertAlmostEqual(shared_clusters.se_clustered, clustered_error)
        self.assertEqual(shared_clusters.critical_value, critical_value)
        self.assertEqual(shared_clusters.ci_low, 2.0 - critical_value * clustered_error)
        self.assertEqual(shared_clusters.ci_high, 2.0 + critical_value * clustered_error)
        self.assertEqual(shared_clusters.verdict, decision.WINS)
        self.assertEqual(shared_clusters.paired, 40)
        self.assertEqual(shared_clusters.clusters, 20)
        self.assertFalse(shared_clusters.digest_equal)
        self.assertEqual(
            decision.describe(shared_clusters),
            "score vs run-example: 40 paired in 20 clusters (floor 20), mean +2.0000, "
            "SE +0.2236 (unclustered +0.1581), 95 % [+1.5320, +2.4680] "
            "(t 2.093, df 19); candidate-only 1, comparand-only 1: wins",
        )

    def test_lower_values_can_be_oriented_as_better(self) -> None:
        case_ids = tuple(f"case-{index}" for index in range(20))
        result = decision.paired_decision(
            {case_id: (3.0 if index < 10 else 6.0,) for index, case_id in enumerate(case_ids)},
            {case_id: (5.0 if index < 10 else 7.0,) for index, case_id in enumerate(case_ids)},
            {case_id: case_id for case_id in case_ids},
            _metric(higher_is_better=False),
            against="run-example",
            requested_repeats=2,
            completed_repeats=2,
            digest_equal=True,
        )
        # Ten changes at -2 and ten at -1 become positive, with mean 1.5.
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
        self.assertEqual(result.verdict, decision.REFUSED)
        self.assertIsNone(result.critical_value)
        self.assertIsNone(result.ci_low)
        self.assertIsNone(result.ci_high)

    def test_three_interval_verdicts_at_the_floor(self) -> None:
        cluster_ids = tuple(f"source-{index}" for index in range(20))
        examples = (
            ((1.0,) * 10 + (3.0,) * 10, decision.WINS),
            ((-1.0,) * 10 + (-3.0,) * 10, decision.LOSES),
            ((-1.0, 1.0) * 10, decision.UNDECIDED),
        )
        for differences, verdict in examples:
            with self.subTest(verdict=verdict):
                result = _from_differences(differences, cluster_ids)
                self.assertEqual(result.verdict, verdict)
                self.assertTrue(decision.describe(result).endswith(verdict))

    def test_fewer_than_two_pairs_are_refused(self) -> None:
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
        self.assertIsNone(one_pair.critical_value)
        self.assertEqual(one_pair.verdict, decision.REFUSED)
        self.assertEqual(
            decision.describe(one_pair),
            "score vs run-example: 1 paired in 1 cluster below the floor of 20, "
            "mean +1.0000, SE n/a (unclustered n/a), 95 % n/a; "
            "candidate-only 1, comparand-only 1: refused",
        )

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
        self.assertIsNone(no_pairs.se_unclustered)
        self.assertIsNone(no_pairs.se_clustered)
        self.assertIsNone(no_pairs.critical_value)
        self.assertIsNone(no_pairs.ci_low)
        self.assertIsNone(no_pairs.ci_high)
        self.assertEqual(no_pairs.verdict, decision.REFUSED)
        self.assertEqual(
            decision.describe(no_pairs),
            "score vs run-example: nothing paired; 0 clusters below the floor of 20; "
            "candidate-only 1, comparand-only 1: refused",
        )

    def test_one_cluster_with_zero_clustered_error_is_refused(self) -> None:
        result = _from_differences((0.0, 2.0), ("source-a", "source-a"))
        self.assertEqual(result.paired, 2)
        self.assertEqual(result.clusters, 1)
        self.assertEqual(result.mean_difference, 1.0)
        self.assertEqual(result.se_unclustered, math.sqrt(2) / 2)
        self.assertEqual(result.se_clustered, 0.0)
        self.assertIsNone(result.ci_low)
        self.assertIsNone(result.ci_high)
        self.assertIsNone(result.critical_value)
        self.assertEqual(result.verdict, decision.REFUSED)
        self.assertEqual(
            decision.describe(result),
            "score vs run-example: 2 paired in 1 cluster below the floor of 20, "
            "mean +1.0000, SE +0.0000 (unclustered +0.7071), 95 % n/a; "
            "candidate-only 0, comparand-only 0: refused",
        )

    def test_two_clusters_with_zero_clustered_error_are_refused(self) -> None:
        result = _from_differences((0.0, 2.0, 1.0), ("source-a", "source-a", "source-b"))
        self.assertEqual(result.paired, 3)
        self.assertEqual(result.clusters, 2)
        self.assertEqual(result.mean_difference, 1.0)
        self.assertEqual(result.se_unclustered, math.sqrt(2) / 3)
        self.assertEqual(result.se_clustered, 0.0)
        self.assertIsNone(result.ci_low)
        self.assertIsNone(result.ci_high)
        self.assertIsNone(result.critical_value)
        self.assertEqual(result.verdict, decision.REFUSED)
        self.assertEqual(
            decision.describe(result),
            "score vs run-example: 3 paired in 2 clusters below the floor of 20, "
            "mean +1.0000, SE +0.0000 (unclustered +0.4714), 95 % n/a; "
            "candidate-only 0, comparand-only 0: refused",
        )

    def test_nineteen_clusters_refuse_and_twenty_compute_an_interval(self) -> None:
        for count in (19, 20):
            with self.subTest(clusters=count):
                result = _from_differences(
                    (1.0,) * (count - 1) + (3.0,),
                    tuple(f"source-{index}" for index in range(count)),
                )
                self.assertEqual(result.paired, count)
                self.assertEqual(result.clusters, count)
                self.assertEqual(result.cluster_floor, 20)
                self.assertIsNotNone(result.mean_difference)
                self.assertIsNotNone(result.se_unclustered)
                self.assertIsNotNone(result.se_clustered)
                if count == 19:
                    self.assertEqual(result.verdict, decision.REFUSED)
                    self.assertIsNone(result.critical_value)
                    self.assertIsNone(result.ci_low)
                    self.assertIsNone(result.ci_high)
                    self.assertIn("19 clusters below the floor of 20", decision.describe(result))
                else:
                    self.assertEqual(result.verdict, decision.WINS)
                    self.assertIsNotNone(result.ci_low)
                    self.assertIsNotNone(result.ci_high)
                    assert result.critical_value is not None
                    self.assertAlmostEqual(result.critical_value, 2.093, places=3)
                    self.assertIn("20 clusters (floor 20)", decision.describe(result))
                    self.assertIn("(t 2.093, df 19)", decision.describe(result))

    def test_many_pairs_in_nineteen_clusters_still_refuse(self) -> None:
        result = _from_differences(
            (1.0, 3.0) * 19,
            tuple(f"source-{index // 2}" for index in range(38)),
        )
        self.assertEqual(result.paired, 38)
        self.assertEqual(result.clusters, 19)
        self.assertEqual(result.mean_difference, 2.0)
        self.assertEqual(result.se_clustered, 0.0)
        self.assertEqual(result.verdict, decision.REFUSED)
        self.assertIsNone(result.critical_value)
        self.assertIsNone(result.ci_low)
        self.assertIsNone(result.ci_high)

    def test_zero_error_at_or_above_the_floor_keeps_the_sign_verdict(self) -> None:
        for count in (20, 152):
            for difference, verdict in (
                (1.0, decision.WINS),
                (-1.0, decision.LOSES),
                (0.0, decision.UNDECIDED),
            ):
                with self.subTest(clusters=count, difference=difference):
                    result = _from_differences(
                        (difference,) * count,
                        tuple(f"source-{index}" for index in range(count)),
                    )
                    self.assertEqual(result.se_clustered, 0.0)
                    self.assertEqual(result.ci_low, difference)
                    self.assertEqual(result.ci_high, difference)
                    self.assertEqual(result.verdict, verdict)
                    assert result.critical_value is not None
                    self.assertAlmostEqual(
                        result.critical_value, 2.093 if count == 20 else 1.976, places=3
                    )
                    self.assertIn(f"df {count - 1}", decision.describe(result))
                    self.assertTrue(decision.describe(result).endswith(verdict))

    def test_json_contains_every_field_at_full_precision(self) -> None:
        result = _from_differences(
            (1.0,) * 10 + (3.0,) * 10,
            tuple(f"source-{index}" for index in range(20)),
        )
        clustered_error = math.sqrt(20) / 20
        critical_value = studentt.two_sided_quantile(decision.CONFIDENCE, 19)
        expected = {
            "metric": "score",
            "against": "run-example",
            "requested_repeats": 5,
            "completed_repeats": 5,
            "paired": 20,
            "clusters": 20,
            "cluster_floor": 20,
            "candidate_only": 0,
            "comparand_only": 0,
            "mean_difference": 2.0,
            "se_unclustered": clustered_error,
            "se_clustered": clustered_error,
            "critical_value": critical_value,
            "ci_low": 2.0 - critical_value * clustered_error,
            "ci_high": 2.0 + critical_value * clustered_error,
            "verdict": decision.WINS,
            "digest_equal": True,
        }
        serialized = decision.to_json(result)
        self.assertEqual(serialized, expected)
        self.assertEqual(set(serialized), set(expected))
        se_json = serialized["se_clustered"]
        assert isinstance(se_json, float)
        self.assertNotEqual(se_json, round(clustered_error, 4))
        critical_json = serialized["critical_value"]
        assert isinstance(critical_json, float)
        self.assertNotEqual(critical_json, round(critical_value, 3))

    def test_refused_json_records_the_floor_without_an_interval(self) -> None:
        serialized = decision.to_json(_from_differences((1.0,), ("source-a",)))
        self.assertEqual(serialized["cluster_floor"], 20)
        self.assertIsNone(serialized["critical_value"])
        self.assertIsNone(serialized["ci_low"])
        self.assertIsNone(serialized["ci_high"])
        self.assertEqual(serialized["verdict"], decision.REFUSED)

    def test_module_imports_only_standard_library_and_arithmetic_dependencies(self) -> None:
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
            {
                "collections.abc", "dataclasses", "math", "typing",
                "gideon.evaluation.results", "gideon.evaluation.studentt",
            },
        )
        self.assertEqual(
            package_imports,
            [
                ("gideon.evaluation.results", "JSONValue"),
                ("gideon.evaluation.studentt", "two_sided_quantile"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
