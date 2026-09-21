"""Run the ``judgments@1`` metrics over a ranked-list file."""

from collections.abc import Mapping
from typing import Final, cast

from gideon.evaluation.evalset import LoadedSet
from gideon.evaluation.judgments import coordinates
from gideon.evaluation.rankmetrics import (
    DEFINITION_ID,
    RANKING_FLOOR,
    RELEVANT_GRADE,
    Coordinates,
    GradedPassage,
    hole_at_10,
    ndcg_at_10,
    recall_at_50,
)
from gideon.evaluation.results import CaseResult, JSONValue, RunContext, SliceResult

_FIGURE_KEYS: Final[tuple[str, ...]] = ("ndcg_at_10", "recall_at_50", "hole_at_10")


def _rounded(value: float | None) -> float | None:
    """Round a defined metric at the result boundary, never producing NaN."""

    return None if value is None else round(value, 6)


def _mean(values: tuple[float, ...]) -> float | None:
    return None if not values else sum(values) / len(values)


def _render_figure(value: float | None) -> str:
    return "null" if value is None else f"{value:.6f}"


def _report(
    case_ids: tuple[str, ...],
    results: tuple[CaseResult, ...],
    graded: Mapping[str, tuple[GradedPassage, ...]],
    ranked: Mapping[str, tuple[Coordinates, ...]] | None,
) -> str:
    """Render the summary from ids and computed values, never case content."""

    judged_ids = tuple(case_id for case_id in case_ids if graded.get(case_id))
    no_relevant = tuple(
        case_id
        for case_id in judged_ids
        if not any(grade >= RELEVANT_GRADE for _coordinate, grade in graded[case_id])
    )
    no_positive = tuple(
        case_id
        for case_id in judged_ids
        if not any(grade > 0 for _coordinate, grade in graded[case_id])
    )
    no_grades = tuple(case_id for case_id in case_ids if not graded.get(case_id))
    # An ungraded query is left out of Hole@10 for want of grades, not for want
    # of a list, and the no-grades-yet count already reports it.
    empty_ranked = tuple(
        case_id
        for case_id in judged_ids
        if ranked is not None and case_id in ranked and not ranked[case_id]
    )
    not_ranked = tuple(
        case_id for case_id in judged_ids if ranked is None or case_id not in ranked
    )

    lines = [
        f"judged queries {len(judged_ids)} of {len(case_ids)}; definition {DEFINITION_ID}"
    ]
    for key in _FIGURE_KEYS:
        values = tuple(
            cast(float, result.metrics[key])
            for result in results
            if isinstance(result.metrics[key], float)
        )
        lines.append(f"{key} mean {_render_figure(_mean(values))} count {len(values)}")
    if no_relevant:
        lines.append(f"no relevant passage: {' '.join(no_relevant)}")
    if no_positive:
        lines.append(f"no grade above 0: {' '.join(no_positive)}")
    if empty_ranked:
        lines.append(f"empty ranked list: {' '.join(empty_ranked)}")
    if not_ranked:
        lines.append(f"judged and not ranked: {' '.join(not_ranked)}")
    lines.append(f"no grades yet: {len(no_grades)}")
    if len(judged_ids) < RANKING_FLOOR:
        lines.append(
            f"small set: {len(judged_ids)} judged queries below {RANKING_FLOOR}; "
            "the set detects regressions and ranks nothing"
        )
    return "\n".join(lines) + "\n"


def run_judgments(eval_set: LoadedSet, slice_name: str, context: RunContext) -> SliceResult:
    """Score active judgment queries in id order from primary grades only."""

    active = set(eval_set.active_ids)
    case_ids = tuple(sorted(case_id for case_id in eval_set.slices[slice_name] if case_id in active))
    grouped: dict[str, list[GradedPassage]] = {}
    for judgment in eval_set.judgments:
        if judgment.assessment == "primary":
            grouped.setdefault(judgment.query_id, []).append(
                (coordinates(judgment), judgment.grade)
            )
    graded = {query_id: tuple(values) for query_id, values in grouped.items()}

    case_results: list[CaseResult] = []
    ranked_lists = context.ranked
    for case_id in case_ids:
        passages = graded.get(case_id, ())
        ranked: tuple[Coordinates, ...]
        if ranked_lists is None or case_id not in ranked_lists:
            covered = False
            ranked = ()
        else:
            covered = True
            ranked = ranked_lists[case_id]
        judged = bool(passages)
        if judged and covered:
            figures: tuple[float | None, float | None, float | None] = (
                _rounded(ndcg_at_10(ranked, passages)),
                _rounded(recall_at_50(ranked, passages)),
                _rounded(hole_at_10(ranked, passages)),
            )
        else:
            figures = (None, None, None)
        metrics: Mapping[str, JSONValue] = {
            "ndcg_at_10": figures[0],
            "recall_at_50": figures[1],
            "hole_at_10": figures[2],
            "graded_passages": len(passages),
            "relevant_passages": sum(
                grade >= RELEVANT_GRADE for _coordinate, grade in passages
            ),
            "ranked_passages": len(ranked),
        }
        case_results.append(
            CaseResult(
                case_id,
                1,
                "pass" if not judged or covered else "fail",
                metrics,
                judge=None,
                latency_ms=None,
            )
        )

    results = tuple(case_results)
    # The verdict is coverage and never a metric: a row fails exactly when a
    # judged query had no ranked list, so the slice verdict is its rows' (§18.3).
    return SliceResult(
        all(result.verdict == "pass" for result in results),
        _report(case_ids, results, graded, ranked_lists),
        results,
    )
