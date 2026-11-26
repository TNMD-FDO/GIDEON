"""Score Research pools or ranked files against a generation's manifest."""

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final, cast

from gideon.evaluation import baselines, pool_coordinates, research_door
from gideon.evaluation.assessments import Assessments, agreement
from gideon.evaluation.evalset import LoadedSet, select_cases
from gideon.evaluation.generation import GenerationAccess
from gideon.evaluation.golddrop_slice import read_manifest
from gideon.evaluation.judgments import KappaValue, coordinates
from gideon.evaluation.rankmetrics import (
    HOLE_DEPTH,
    RANKING_FLOOR,
    RELEVANT_GRADE,
    Coordinates,
    GradedPassage,
    crediting,
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

PROBLEMS: Final[frozenset[str]] = frozenset({
    "generation-unresolved", "record-unreachable", "turn-failed", "turn-cut",
    "turn-reply-invalid", "generation-differs", "config-differs", "pool-unresolved", "case-not-judgments",
})
_JUDGMENTS_CATEGORY: Final[tuple[str, str]] = ("judgments", "judgments")
_FIGURE_KEYS: Final[tuple[str, ...]] = ("ndcg_at_10", "recall_at_50", "hole_at_10")
_GRADES: Final = range(4)


@dataclass(frozen=True, slots=True)
class _RankedTurns:
    """The resolved rankings, per-query refusals, and reply latencies."""

    ranked: dict[str, tuple[Coordinates, ...]] | Problem
    failures: dict[str, dict[str, JSONValue]]
    latencies: dict[str, float]


def _rank_turns(
    eval_set: LoadedSet, case_ids: tuple[str, ...], generation: GenerationAccess,
    expected_hash: str, context: RunContext,
) -> _RankedTurns:
    pools: dict[str, tuple[str, ...]] = {}
    failures: dict[str, dict[str, JSONValue]] = {}
    latencies: dict[str, float] = {}
    for case_id in case_ids:
        context.checkpoint()
        reply = research_door.send(
            context.host, context.turns_dir, cast(str, eval_set.cases_by_id[case_id]["question"]),
            context.turn_overrides,
        )
        if isinstance(reply, research_door.Failure):
            failure: dict[str, JSONValue] = {"problem": reply.code}
            if reply.state is not None:
                failure["turn_state"] = reply.state
            failures[case_id] = failure
            continue
        latencies[case_id] = reply.latency_ms
        if reply.generation_id != generation.name or generation.corpus_lockfile not in reply.lockfiles:
            failures[case_id] = {"problem": "generation-differs"}
        elif reply.config_hash != expected_hash:
            failures[case_id] = {"problem": "config-differs"}
        else:
            pools[case_id] = reply.chunk_ids

    context.checkpoint()
    resolved = pool_coordinates.read_coordinates(generation.name, pools, context)
    if isinstance(resolved, Problem):
        return _RankedTurns(resolved, failures, latencies)
    ranked: dict[str, tuple[Coordinates, ...]] = {}
    for case_id, entries in resolved.items():
        coordinates_at_rank = tuple(entry for entry in entries if entry is not None)
        if len(coordinates_at_rank) != len(entries):
            failures[case_id] = {"problem": "pool-unresolved"}
        else:
            ranked[case_id] = coordinates_at_rank
    return _RankedTurns(ranked, failures, latencies)


@dataclass(frozen=True, slots=True)
class _QueryAssessment:
    """Assessor counts and matched human grades for one active query."""

    hole_count: int | None
    hole_grades: Counter[int]
    pairs: tuple[tuple[int, int], ...]

    def judge(self, assessments: Assessments) -> Mapping[str, JSONValue]:
        comparison = agreement(self.pairs)
        return {
            "prompt": assessments.prompt,
            "model": assessments.model,
            "hole_count": self.hole_count,
            "assessed_hole_count": None if self.hole_count is None else self.hole_grades.total(),
            **{
                f"grade_{grade}": None if self.hole_count is None else self.hole_grades[grade]
                for grade in _GRADES
            },
            "pair_count": comparison.pair_count,
            "four_grade_differing": comparison.four_grade_differing,
            "relevant_differing": comparison.relevant_differing,
        }


def _assess_query(
    assessments: Assessments,
    case_id: str,
    passages: Iterable[GradedPassage],
    *,
    ranked: tuple[Coordinates, ...] | None,
    held: tuple[GradedPassage, ...],
) -> _QueryAssessment:
    """Pair every primary grade and count the top ten's holes against held passages.

    ``ranked`` is None for a query whose figures are not computed: it keeps its
    pairs, since the agreement is about the grader and not the generation, and
    has no holes to count.
    """

    grades = assessments.grades.get(case_id, {})
    pairs = tuple(
        (grade, grades[coordinate]) for coordinate, grade in passages if coordinate in grades
    )
    if ranked is None:
        return _QueryAssessment(None, Counter(), pairs)

    top = ranked[:HOLE_DEPTH]
    holes = tuple(
        coordinate
        for coordinate, credit in zip(top, crediting(top, held), strict=True)
        if not credit.judged
    )
    return _QueryAssessment(
        len(holes),
        Counter(grades[coordinate] for coordinate in holes if coordinate in grades),
        pairs,
    )


def _rounded(value: float | None) -> float | None:
    """Round a defined metric at the result boundary, never producing NaN."""

    return None if value is None else round(value, 6)


def _mean(values: tuple[float, ...]) -> float | None:
    return None if not values else sum(values) / len(values)


def _render_figure(value: float | None) -> str:
    return "null" if value is None else f"{value:.6f}"


def _render_kappa(value: KappaValue) -> str:
    return _render_figure(value) if isinstance(value, float) else value


def _assessment_lines(
    assessments: Assessments, queries: Iterable[_QueryAssessment],
) -> tuple[str, str]:
    hole_count = hole_queries = pair_queries = 0
    grades: Counter[int] = Counter()
    pairs: list[tuple[int, int]] = []
    for query in queries:
        hole_count += query.hole_count or 0
        hole_queries += bool(query.hole_count)
        pair_queries += bool(query.pairs)
        grades.update(query.hole_grades)
        pairs.extend(query.pairs)

    assessed_holes = grades.total()
    grade_counts = ", ".join(f"grade {grade} {grades[grade]}" for grade in _GRADES)
    relevant_holes = sum(count for grade, count in grades.items() if grade >= RELEVANT_GRADE)
    comparison = agreement(pairs)
    pair_count = comparison.pair_count
    four_grade_rate = comparison.four_grade_differing / pair_count if pair_count else None
    relevant_rate = comparison.relevant_differing / pair_count if pair_count else None
    return (
        f"assessor {assessments.prompt}, model {assessments.model}, "
        f"holes at ranks 1-{HOLE_DEPTH} {hole_count} across {hole_queries} queries, "
        f"assessed {assessed_holes}, unassessed {hole_count - assessed_holes}, "
        f"{grade_counts}, relevant {relevant_holes}",
        f"assessor disagreement {pair_count} pairs across {pair_queries} queries, "
        f"four-grade differing {comparison.four_grade_differing} "
        f"rate {_render_figure(four_grade_rate)} kappa {_render_kappa(comparison.four_grade_kappa)}, "
        f"relevant differing {comparison.relevant_differing} "
        f"rate {_render_figure(relevant_rate)} kappa {_render_kappa(comparison.relevant_kappa)}",
    )


def _report(
    results: tuple[CaseResult, ...],
    held: Mapping[str, tuple[GradedPassage, ...]],
    ranked: Mapping[str, tuple[Coordinates, ...]],
    generation: GenerationAccess,
    drop_counts: Counter[DropKind],
    assessment_lines: tuple[str, ...],
    *,
    door_detail: str,
    uses_turn: bool,
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
        f"generation {generation.name} ({generation.corpus_lockfile}), {door_detail}"
    ]
    for key in _FIGURE_KEYS:
        values = tuple(
            cast(float, result.metrics[key])
            for result in results
            if isinstance(result.metrics[key], float)
        )
        lines.append(f"{key} mean {_render_figure(_mean(values))} count {len(values)}")
    lines.extend(assessment_lines)
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
    if uses_turn:
        failed_turns = []
        unresolved_pools = []
        for result in results:
            problem = result.metrics.get("problem")
            if problem == "pool-unresolved":
                unresolved_pools.append(result.case_id)
            elif problem is not None:
                failed_turns.append(result.case_id)
        lines.append(f"failed turns: {' '.join(failed_turns) or 'none'}")
        lines.append(f"unresolved pools: {' '.join(unresolved_pools) or 'none'}")
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
    *, door_detail: str, latencies: Mapping[str, float] | None = None,
) -> SliceResult:
    """Fail every counted query and report the problem with its fix."""

    reply_latencies = {} if latencies is None else latencies
    results = tuple(
        CaseResult(case_id, 1, "fail", {"problem": code}, latency_ms=reply_latencies.get(case_id))
        for case_id in case_ids
    )
    lines = (
        f"{len(case_ids)} counted, {unsigned} unsigned excluded, {door_detail}",
        *report.failure_lines(problem.fix, f"{code}: {problem.problem}"),
        "judgments: fail",
    )
    return SliceResult(False, "\n".join(lines) + "\n", results)


def run_judgments(eval_set: LoadedSet, slice_name: str, context: RunContext) -> SliceResult:
    """Score active queries in id order over the primary grades the generation holds."""

    selected = select_cases(eval_set, slice_name)
    case_ids = tuple(sorted(selected.counted))
    assessments = context.assessments
    generation = context.generation
    ranked_lists = context.ranked
    uses_turn = ranked_lists is None
    expected_hash: str | Problem | None = None
    door_detail = "door ranked-file"
    if uses_turn:
        expected_hash = baselines.expected_config_hash(context.turn_overrides)
        baseline = next(
            (word for word, overrides in baselines.BASELINES.items() if overrides == context.turn_overrides),
            "shipped",
        )
        hash_text = expected_hash if isinstance(expected_hash, str) else "unavailable"
        door_detail = f"door {research_door.TURN_PATH}, config hash {hash_text}, baseline {baseline}"
    if generation is None:
        ranked_flag = "" if uses_turn else " --ranked <file>"
        assessed_flag = " --assessed <file>" if assessments is not None else ""
        retry = report.command(
            f"eval run --slice {slice_name}{ranked_flag}{assessed_flag} --generation <name>"
        )
        return _failed(case_ids, len(selected.unsigned), "generation-unresolved", Problem(
            "the stores generation was not resolved",
            f"Run {retry} through the command's stores preconditions, then retry.",
        ), door_detail=door_detail)

    failures: dict[str, dict[str, JSONValue]] = {}
    latencies: dict[str, float] = {}
    if ranked_lists is None:
        if isinstance(expected_hash, Problem):
            return _failed(case_ids, len(selected.unsigned), "config-differs", expected_hash, door_detail=door_detail)
        assert expected_hash is not None
        # A supplied set may count another suite's case in this slice's list; it
        # carries no question, so the run stops before its first turn.
        foreign_cases = tuple(
            case_id for case_id in case_ids
            if (eval_set.cases_by_id[case_id].get("suite"), eval_set.cases_by_id[case_id].get("category"))
            != _JUDGMENTS_CATEGORY
        )
        if foreign_cases:
            return _failed(case_ids, len(selected.unsigned), "case-not-judgments", Problem(
                f"counted cases outside the judgments queries: {' '.join(foreign_cases)}",
                f"List only judgments queries in the set's {slice_name} slice, then retry.",
            ), door_detail=door_detail)
        turn_inputs = _rank_turns(eval_set, case_ids, generation, expected_hash, context)
        latencies = turn_inputs.latencies
        if isinstance(turn_inputs.ranked, Problem):
            return _failed(
                case_ids, len(selected.unsigned), "record-unreachable", turn_inputs.ranked,
                door_detail=door_detail, latencies=latencies,
            )
        ranked_lists = turn_inputs.ranked
        failures = turn_inputs.failures

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
        return _failed(
            case_ids, len(selected.unsigned), "record-unreachable", manifest,
            door_detail=door_detail, latencies=latencies,
        )

    case_results: list[CaseResult] = []
    query_assessments: list[_QueryAssessment] = []
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
        failure = failures.get(case_id, {})
        can_score = bool(held and covered and not foreign and not failure)
        if can_score:
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
            **failure,
        }
        judge = None
        if assessments is not None:
            query_assessment = _assess_query(
                assessments, case_id, passages, ranked=ranked if can_score else None, held=held,
            )
            query_assessments.append(query_assessment)
            judge = query_assessment.judge(assessments)
        case_results.append(
            CaseResult(
                case_id,
                1,
                "fail" if failure or foreign or (passages and not covered) else "pass",
                metrics,
                judge=judge,
                latency_ms=latencies.get(case_id),
            )
        )

    results = tuple(case_results)
    assessment_lines = (
        () if assessments is None else _assessment_lines(assessments, query_assessments)
    )
    # The verdict is coverage and input validity, never a metric: a row fails
    # exactly when its turn failed, its pool did not resolve, a judged query had
    # no ranked list, or its list holds an entry the generation does not hold.
    return SliceResult(
        all(result.verdict == "pass" for result in results),
        _report(
            results, held_by_query, ranked_lists, generation, drop_counts, assessment_lines,
            door_detail=door_detail, uses_turn=uses_turn,
        ),
        results,
    )
