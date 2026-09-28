"""Turn code-computed metrics into a paired signal without using judge output."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from math import fsum, sqrt
from typing import Final

from gideon.evaluation.results import JSONValue

DECISION_REPEATS: Final[int] = 5
"""The candidate's repeats in a decision run: each case's metric is averaged
over them before it is paired."""

Z_95: Final[float] = 1.96
"""The two-sided 95 percent normal quantile, since the interval rests on the
central limit theorem over the paired cases."""

WINS: Final[str] = "wins"
LOSES: Final[str] = "loses"
UNDECIDED: Final[str] = "undecided"


@dataclass(frozen=True, slots=True)
class DecisionMetric:
    """A code-computed metric a decision is taken on.

    ``value`` reads one result's ``metrics`` mapping and never its ``judge``
    mapping, so no judge-derived figure can reach a decision. It returns
    ``None`` for a case the metric does not define, which is then never paired.
    """

    name: str
    higher_is_better: bool
    value: Callable[[Mapping[str, JSONValue]], float | None]


@dataclass(frozen=True, slots=True)
class PairedDecision:
    """The paired measurements, uncertainty, and verdict for one comparison."""

    metric: str
    against: str
    requested_repeats: int
    completed_repeats: int
    paired: int
    clusters: int
    candidate_only: int
    comparand_only: int
    mean_difference: float | None
    se_unclustered: float | None
    se_clustered: float | None
    ci_low: float | None
    ci_high: float | None
    verdict: str
    digest_equal: bool


def per_case_values(
    results: Iterable[tuple[str, int, Mapping[str, JSONValue]]],
    metric: DecisionMetric,
) -> dict[str, tuple[float, ...]]:
    """Collect each case's defined metric values across its result rows."""

    values: dict[str, list[float]] = {}
    for case_id, _repeat, metrics in results:
        value = metric.value(metrics)
        if value is not None:
            values.setdefault(case_id, []).append(float(value))
    return {case_id: tuple(case_values) for case_id, case_values in values.items()}


def paired_decision(
    candidate: Mapping[str, tuple[float, ...]],
    comparand: Mapping[str, tuple[float, ...]],
    clusters: Mapping[str, str],
    metric: DecisionMetric,
    *,
    against: str,
    requested_repeats: int,
    completed_repeats: int,
    digest_equal: bool,
) -> PairedDecision:
    """Compare per-case means and cluster the residuals by their case groups."""

    paired_cases = sorted(candidate.keys() & comparand.keys())
    candidate_only = len(candidate.keys() - comparand.keys())
    comparand_only = len(comparand.keys() - candidate.keys())
    differences = [
        (1.0 if metric.higher_is_better else -1.0)
        * (
            fsum(candidate[case_id]) / len(candidate[case_id])
            - fsum(comparand[case_id]) / len(comparand[case_id])
        )
        for case_id in paired_cases
    ]
    paired = len(differences)
    cluster_ids = {clusters[case_id] for case_id in paired_cases}

    mean_difference: float | None = None
    se_unclustered: float | None = None
    se_clustered: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    verdict = UNDECIDED

    if paired:
        mean_difference = fsum(differences) / paired
    if paired >= 2:
        mean = fsum(differences) / paired
        residuals = [difference - mean for difference in differences]
        by_cluster: dict[str, list[float]] = {}
        for case_id, residual in zip(paired_cases, residuals, strict=True):
            by_cluster.setdefault(clusters[case_id], []).append(residual)
        se_unclustered = sqrt(fsum(residual * residual for residual in residuals)) / paired
        se_clustered = sqrt(fsum(fsum(group) ** 2 for group in by_cluster.values())) / paired
        ci_low = mean - Z_95 * se_clustered
        ci_high = mean + Z_95 * se_clustered
        if ci_low > 0:
            verdict = WINS
        elif ci_high < 0:
            verdict = LOSES

    return PairedDecision(
        metric=metric.name,
        against=against,
        requested_repeats=requested_repeats,
        completed_repeats=completed_repeats,
        paired=paired,
        clusters=len(cluster_ids),
        candidate_only=candidate_only,
        comparand_only=comparand_only,
        mean_difference=mean_difference,
        se_unclustered=se_unclustered,
        se_clustered=se_clustered,
        ci_low=ci_low,
        ci_high=ci_high,
        verdict=verdict,
        digest_equal=digest_equal,
    )


def describe(decision: PairedDecision) -> str:
    """Format a content-free comparison line with figures rounded for reading."""

    counts = (
        f"candidate-only {decision.candidate_only}, "
        f"comparand-only {decision.comparand_only}"
    )
    if not decision.paired:
        return f"{decision.metric} vs {decision.against}: nothing paired; {counts}"

    mean = _format_number(decision.mean_difference)
    se_clustered = _format_number(decision.se_clustered)
    se_unclustered = _format_number(decision.se_unclustered)
    if decision.ci_low is None or decision.ci_high is None:
        interval = "n/a"
    else:
        interval = f"[{_format_number(decision.ci_low)}, {_format_number(decision.ci_high)}]"
    return (
        f"{decision.metric} vs {decision.against}: {decision.paired} paired in "
        f"{decision.clusters} clusters, mean {mean}, SE {se_clustered} "
        f"(unclustered {se_unclustered}), 95 % {interval}: {decision.verdict}; {counts}"
    )


def to_json(decision: PairedDecision) -> dict[str, JSONValue]:
    """Return the full-precision, content-free fields stored with a run."""

    return {
        "metric": decision.metric,
        "against": decision.against,
        "requested_repeats": decision.requested_repeats,
        "completed_repeats": decision.completed_repeats,
        "paired": decision.paired,
        "clusters": decision.clusters,
        "candidate_only": decision.candidate_only,
        "comparand_only": decision.comparand_only,
        "mean_difference": decision.mean_difference,
        "se_unclustered": decision.se_unclustered,
        "se_clustered": decision.se_clustered,
        "ci_low": decision.ci_low,
        "ci_high": decision.ci_high,
        "verdict": decision.verdict,
        "digest_equal": decision.digest_equal,
    }


def _format_number(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.4f}".replace("-", "−")
