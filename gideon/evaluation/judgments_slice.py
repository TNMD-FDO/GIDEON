"""Run the ``judgments@2`` metrics against a generation's manifest."""

from collections import Counter
from collections.abc import Mapping
from typing import Final, cast

from gideon.evaluation.evalset import LoadedSet, select_cases
from gideon.evaluation.generation import GenerationAccess
from gideon.evaluation.golddrop_slice import read_manifest
from gideon.evaluation.judgments import coordinates
from gideon.evaluation.rankmetrics import (
    RANKING_FLOOR,
    RELEVANT_GRADE,
    Coordinates,
    GradedPassage,
)
from gideon.evaluation.rankmetrics2 import (
    DEFINITION_ID,
    DROP_KINDS,
    DropKind,
    foreign_entries,
    held_passages,
    hole_at_10,
    ndcg_at_10,
    recall_at_50,
)
from gideon.evaluation.results import CaseResult, JSONValue, RunContext, SliceResult
from gideon.host import report
from gideon.host.report import Problem

PROBLEMS: Final[frozenset[str]] = frozenset({"generation-unresolved", "record-unreachable"})
_FIGURE_KEYS: Final[tuple[str, ...]] = ("ndcg_at_10", "recall_at_50", "hole_at_10")


def _rounded(value: float | None) -> float | None:
    """Round a defined metric at the result boundary, never producing NaN."""

    return None if value is None else round(value, 6)


def _mean(values: tuple[float, ...]) -> float | None:
    return None if not values else sum(values) / len(values)


def _render_figure(value: float | None) -> str:
    return "null" if value is None else f"{value:.6f}"


def _report(
    results: tuple[CaseResult, ...],
    held: Mapping[str, tuple[GradedPassage, ...]],
    ranked: Mapping[str, tuple[Coordinates, ...]],
    generation: GenerationAccess,
    drop_counts: Counter[DropKind],
) -> str:
    """Render the summary from ids and computed values, never case content."""

    judged_ids = tuple(result.case_id for result in results if result.metrics["graded_passages"])
    no_relevant = tuple(
        result.case_id
        for result in results
        if result.case_id in held and not result.metrics["relevant_passages"]
    )
    no_positive = tuple(
        case_id
        for case_id, passages in held.items()
        if not any(grade > 0 for _coordinate, grade in passages)
    )
    # An ungraded query is left out of Hole@10 for want of grades, not for want
    # of a list, and the no-grades-yet count already reports it.
    empty_ranked = tuple(
        case_id
        for case_id in judged_ids
        if case_id in ranked and not ranked[case_id]
    )
    not_ranked = tuple(case_id for case_id in judged_ids if case_id not in ranked)

    lines = [
        f"judged queries {len(judged_ids)} of {len(results)}, definition {DEFINITION_ID}, "
        f"generation {generation.name} ({generation.corpus_lockfile})"
    ]
    for key in _FIGURE_KEYS:
        values = tuple(
            cast(float, result.metrics[key])
            for result in results
            if isinstance(result.metrics[key], float)
        )
        lines.append(f"{key} mean {_render_figure(_mean(values))} count {len(values)}")
    touched = tuple(result for result in results if result.metrics["dropped_passages"])
    kinds = ", ".join(f"{kind} {drop_counts[kind]}" for kind in DROP_KINDS)
    lines.append(f"dropped passages {drop_counts.total()} ({kinds}) across {len(touched)} queries")
    for result in touched:
        dropped = " ".join(cast(list[str], result.metrics["dropped"]))
        lines.append(f"{result.case_id}: {dropped}")
    dropped_queries = " ".join(case_id for case_id in judged_ids if case_id not in held)
    lines.append(f"dropped queries: {dropped_queries or 'none'}")
    foreign = tuple(result for result in results if result.metrics["foreign_ranked"])
    if foreign:
        count = sum(cast(int, result.metrics["foreign_ranked"]) for result in foreign)
        lines.append(f"foreign ranked entries {count}: " + " ".join(result.case_id for result in foreign))
    if no_relevant:
        lines.append(f"no relevant passage: {' '.join(no_relevant)}")
    if no_positive:
        lines.append(f"no grade above 0: {' '.join(no_positive)}")
    if empty_ranked:
        lines.append(f"empty ranked list: {' '.join(empty_ranked)}")
    if not_ranked:
        lines.append(f"judged and not ranked: {' '.join(not_ranked)}")
    lines.append(f"no grades yet: {len(results) - len(judged_ids)}")
    if len(held) < RANKING_FLOOR:
        lines.append(
            f"small set: {len(held)} judged queries with held passages below {RANKING_FLOOR}; "
            "the set detects regressions and ranks nothing"
        )
    return "\n".join(lines) + "\n"


def _failed(
    case_ids: tuple[str, ...], unsigned: int, code: str, problem: Problem,
) -> SliceResult:
    """Fail every counted query and report the problem with its fix."""

    results = tuple(CaseResult(case_id, 1, "fail", {"problem": code}) for case_id in case_ids)
    lines = (
        f"{len(case_ids)} counted, {unsigned} unsigned excluded",
        *report.failure_lines(problem.fix, f"{code}: {problem.problem}"),
        "judgments: fail",
    )
    return SliceResult(False, "\n".join(lines) + "\n", results)


def run_judgments(eval_set: LoadedSet, slice_name: str, context: RunContext) -> SliceResult:
    """Score active queries in id order over the primary grades the generation holds."""

    selected = select_cases(eval_set, slice_name)
    case_ids = tuple(sorted(selected.counted))
    generation = context.generation
    if generation is None:
        retry = report.command(f"eval run --slice {slice_name} --ranked <file> --generation <name>")
        return _failed(case_ids, len(selected.unsigned), "generation-unresolved", Problem(
            "the stores generation was not resolved",
            f"Run {retry} through the command's stores preconditions, then retry.",
        ))

    ranked_lists = context.ranked or {}
    source_ids = {entry[0] for ranked in ranked_lists.values() for entry in ranked}
    active = set(case_ids)
    grouped: dict[str, list[GradedPassage]] = {}
    for judgment in eval_set.judgments:
        if judgment.assessment == "primary" and judgment.query_id in active:
            grouped.setdefault(judgment.query_id, []).append(
                (coordinates(judgment), judgment.grade)
            )
            source_ids.add(judgment.source_id)
    context.checkpoint()
    manifest = read_manifest(generation.name, tuple(sorted(source_ids)), context)
    if isinstance(manifest, Problem):
        return _failed(case_ids, len(selected.unsigned), "record-unreachable", manifest)

    case_results: list[CaseResult] = []
    held_by_query: dict[str, tuple[GradedPassage, ...]] = {}
    drop_counts: Counter[DropKind] = Counter()
    for case_id in case_ids:
        context.checkpoint()
        passages = grouped.get(case_id, [])
        held, dropped = held_passages(passages, manifest)
        if held:
            held_by_query[case_id] = held
        dropped_coordinates: list[str] = []
        for drop in dropped:
            source_id, sha256, start, end = drop.passage[0]
            dropped_coordinates.append(f"{source_id}@{sha256}:{start}-{end}")
            drop_counts[drop.kind] += 1
        ranked = ranked_lists.get(case_id, ())
        covered = case_id in ranked_lists
        foreign = foreign_entries(ranked, manifest)
        if held and covered and not foreign:
            figures: tuple[float | None, float | None, float | None] = (
                _rounded(ndcg_at_10(ranked, held)),
                _rounded(recall_at_50(ranked, held)),
                _rounded(hole_at_10(ranked, held)),
            )
        else:
            figures = (None, None, None)
        metrics: Mapping[str, JSONValue] = {
            "ndcg_at_10": figures[0],
            "recall_at_50": figures[1],
            "hole_at_10": figures[2],
            "graded_passages": len(passages),
            "relevant_passages": sum(
                grade >= RELEVANT_GRADE for _coordinate, grade in held
            ),
            "ranked_passages": len(ranked),
            "dropped_passages": len(dropped),
            "dropped": list[JSONValue](sorted(dropped_coordinates)),
            "foreign_ranked": len(foreign),
        }
        case_results.append(
            CaseResult(
                case_id,
                1,
                "fail" if foreign or (passages and not covered) else "pass",
                metrics,
                judge=None,
                latency_ms=None,
            )
        )

    results = tuple(case_results)
    # The verdict is coverage and input validity, never a metric: a row fails
    # exactly when a judged query had no ranked list or its list holds an entry
    # the generation does not hold, so the verdict is based on its rows.
    return SliceResult(
        all(result.verdict == "pass" for result in results),
        _report(results, held_by_query, ranked_lists, generation, drop_counts),
        results,
    )
