"""Turn code-computed metrics into a paired signal without using judge output."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from math import fsum, sqrt
from typing import Final

from gideon.evaluation.results import JSONValue
from gideon.evaluation.studentt import two_sided_quantile

DECISION_REPEATS: Final[int] = 5
"""The candidate's repeats in a decision run: each case's metric is averaged
over them before it is paired."""

CLUSTER_FLOOR: Final[int] = 20
"""The fewest distinct clusters a decision computes an interval over: Cameron
and Miller (2015)'s lower edge of 'few clusters', below which the clustered
error is unreliable and the verdict is refused. A statistical-method threshold,
moved by a ruling and never by a measurement."""

CONFIDENCE: Final[float] = 0.95
"""The two-sided confidence level used to read the Student's t quantile."""

WINS: Final[str] = "wins"
LOSES: Final[str] = "loses"
UNDECIDED: Final[str] = "undecided"
REFUSED: Final[str] = "refused"
"""The verdict for a decision that computed no interval."""


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
    cluster_floor: int
    candidate_only: int
    comparand_only: int
    mean_difference: float | None
    se_unclustered: float | None
    se_clustered: float | None
    critical_value: float | None
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
    """Compare paired means, refusing an interval below CLUSTER_FLOOR clusters.

    The mean and errors remain visible below the floor. One cluster's residuals
    sum to zero, as can each of a few clusters' residual sums, giving a zero
    clustered error without enough independent groups to support a verdict.
    At or above the floor the interval uses Student's t at clusters minus one
    degrees of freedom.
    """

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
    critical_value: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    verdict = REFUSED

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
        if len(cluster_ids) >= CLUSTER_FLOOR:
            critical_value = two_sided_quantile(CONFIDENCE, len(cluster_ids) - 1)
            ci_low = mean - critical_value * se_clustered
            ci_high = mean + critical_value * se_clustered
            verdict = UNDECIDED
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
        cluster_floor=CLUSTER_FLOOR,
        candidate_only=candidate_only,
        comparand_only=comparand_only,
        mean_difference=mean_difference,
        se_unclustered=se_unclustered,
        se_clustered=se_clustered,
        critical_value=critical_value,
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
    noun = "cluster" if decision.clusters == 1 else "clusters"
    cluster_count = f"{decision.clusters} {noun}"
    if decision.clusters < decision.cluster_floor:
        cluster_count += f" below the floor of {decision.cluster_floor}"
    else:
        cluster_count += f" (floor {decision.cluster_floor})"
    if not decision.paired:
        return (
            f"{decision.metric} vs {decision.against}: nothing paired; "
            f"{cluster_count}; {counts}: {decision.verdict}"
        )

    mean = _format_number(decision.mean_difference)
    se_clustered = _format_number(decision.se_clustered)
    se_unclustered = _format_number(decision.se_unclustered)
    if decision.ci_low is None or decision.ci_high is None:
        interval = "n/a"
    else:
        interval = f"[{_format_number(decision.ci_low)}, {_format_number(decision.ci_high)}]"
    if decision.critical_value is not None:
        interval += f" (t {decision.critical_value:.3f}, df {decision.clusters - 1})"
    return (
        f"{decision.metric} vs {decision.against}: {decision.paired} paired in "
        f"{cluster_count}, mean {mean}, SE {se_clustered} "
        f"(unclustered {se_unclustered}), {CONFIDENCE * 100:g} % {interval}; "
        f"{counts}: {decision.verdict}"
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
        "cluster_floor": decision.cluster_floor,
        "candidate_only": decision.candidate_only,
        "comparand_only": decision.comparand_only,
        "mean_difference": decision.mean_difference,
        "se_unclustered": decision.se_unclustered,
        "se_clustered": decision.se_clustered,
        "critical_value": decision.critical_value,
        "ci_low": decision.ci_low,
        "ci_high": decision.ci_high,
        "verdict": decision.verdict,
        "digest_equal": decision.digest_equal,
    }


def _format_number(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.4f}".replace("-", "−")
