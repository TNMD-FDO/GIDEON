"""The ``research-qa@1`` definition over a turn's pool and evidence.

Gold is the case's source id, canonical-text hash, and span for each passage.
A signed harvest case without coordinates uses its sign-off's must-cite ids
at document level. Coordinate gold is held by the generation's manifest
under the ``rankmetrics2.held_passages`` rule and met by a chunk under
``rankmetrics.meets``. Document gold is held when its id is in the manifest
and met by a chunk of that source.

The figures are the first rank meeting any held gold, how many held gold
entries are met at ranks 1 to 10 and 1 to 50, how many are met in the evidence,
and the first evidence passage number that meets any. Cases with no held gold
are listed by id and contribute nothing to the recall denominators.

A counted case passes with a valid turn and resolved pool, at least one held
gold entry, and every held entry met in the evidence. A grading slice also
requires a complete answer. The slice passes when at least one counted case
ran and every counted case is free of problems. Its gold figures never gate
the slice.

The definition grades complete answers of counted cases only, using the
question, reference answer, and turn answer. Grades and grading failures
affect neither verdict. An opted-in unsigned case is marked, keeps its turn
outcome, counts, and message id, and passes when its turn returns a valid
terminal answer held to the run's generation and configuration, failing when
the turn raised a problem; the mark says no gate reads either. It contributes
no gold figures, grade, or slice verdict.
A change to these rules or depths takes the next definition id.
"""

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Final, cast, get_args

from gideon.evaluation import (
    baselines,
    golddrop_slice,
    judge,
    pool_coordinates,
    research_door,
)
from gideon.evaluation.evalset import (
    Case,
    LoadedSet,
    Selection,
    select_cases,
    tuning_cases,
)
from gideon.evaluation.generation import GenerationAccess
from gideon.evaluation.judge_slice import progress_line
from gideon.evaluation.rankmetrics import (
    RECALL_DEPTH,
    RELEVANT_GRADE,
    Coordinates,
    meets,
)
from gideon.evaluation.rankmetrics2 import DROP_KINDS, DropKind, Manifest, held_passages
from gideon.evaluation.results import CaseResult, JSONValue, RunContext, SliceResult
from gideon.host import report
from gideon.host.report import Problem
from gideon.turn.events import EndWord

DEFINITION_ID: Final[str] = "research-qa@1"
RUN_KEY: Final[str] = "research-qa"
# exempt: the depth named by recall@10 is a constant of the definition.
RECALL_DEPTH_10: Final[int] = 10
PROBLEMS: Final[frozenset[str]] = frozenset({
    "generation-unresolved", "record-unreachable", "turn-failed", "turn-cut",
    "turn-reply-invalid", "generation-differs", "config-differs", "pool-unresolved",
    "case-outside-category", "no-cases",
})

_UNGRADED_ENDS: Final[tuple[EndWord, ...]] = ("truncated", "tripped")
_UNGRADED_REASONS: Final[tuple[str, ...]] = (*_UNGRADED_ENDS, "problem", "no-reference")

type _Gold = Coordinates | str


@dataclass(frozen=True, slots=True)
class _Turn:
    """The content-free outcome of one full turn."""

    message_id: str
    chunk_ids: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    end: EndWord | None = None
    attempts: int = 0
    latency_ms: float | None = None
    problem: str | None = None
    turn_state: str | None = None
    grading: judge.Grading | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class _Figures:
    """One valid turn's gold counts, drops, and first matching positions."""

    gold_passages: int
    held_gold_passages: int
    dropped: tuple[tuple[str, DropKind], ...]
    least_rank: int | None
    met_at_10: int
    met_at_50: int
    met_in_pool: int
    met_in_evidence: int
    least_passage_number: int | None


def _run_turns(
    eval_set: LoadedSet, case_ids: tuple[str, ...], generation: GenerationAccess,
    expected_hash: str, context: RunContext, prompt: judge.JudgePrompt | None,
) -> dict[str, _Turn]:
    turns: dict[str, _Turn] = {}
    signed_answers = {} if prompt is None else {signoff.case_id: signoff.answer for signoff in eval_set.signoffs}
    for case_id in case_ids:
        context.checkpoint()
        message_id = context.message_id_factory()
        case = eval_set.cases_by_id[case_id]
        question = cast(str, case["question"])
        reply = research_door.send_full(
            context.host, context.turns_dir, question, context.turn_overrides, message_id=message_id,
        )
        seconds = "unknown"
        if isinstance(reply, research_door.Failure):
            turn = _Turn(message_id, problem=reply.code, turn_state=reply.state)
        else:
            retrieval = reply.retrieval
            problem = None
            if retrieval.generation_id != generation.name or generation.corpus_lockfile not in retrieval.lockfiles:
                problem = "generation-differs"
            elif retrieval.config_hash != expected_hash:
                problem = "config-differs"
            turn = _Turn(
                message_id,
                chunk_ids=retrieval.chunk_ids,
                evidence=reply.evidence,
                end=reply.end,
                attempts=reply.attempts,
                latency_ms=reply.latency_ms,
                problem=problem,
            )
            if reply.elapsed is not None:
                seconds = f"{reply.elapsed:.2f}"
        context.progress(
            f"research-qa {case_id}: {turn.problem or turn.end}, pool {len(turn.chunk_ids)}, "
            f"evidence {len(turn.evidence)}, attempts {turn.attempts}, seconds {seconds}"
        )
        if prompt is not None and isinstance(reply, research_door.FullReply) and turn.problem is None and reply.end == "complete":
            expected = case.get("expected")
            reference = expected.get("answer") if isinstance(expected, Mapping) else signed_answers.get(case_id)
            if isinstance(reference, str):
                assert context.served_model_name is not None and context.judge_retry is not None
                context.checkpoint()
                grading = judge.grade(
                    context.host, context.production_dir,
                    served_model_name=context.served_model_name,
                    prompt=prompt,
                    slots={"question": question, "reference": reference, "candidate": reply.answer},
                    retry=context.judge_retry,
                )
                context.progress(progress_line(case_id, 1, grading))
                turn = replace(turn, grading=grading)
        del reply
        turns[case_id] = turn
    return turns


def _gold(case: Case, must_cite: Mapping[str, tuple[str, ...]], case_id: str) -> tuple[_Gold, ...]:
    if "gold" not in case:
        # A counted case without gold or a sign-off holds no gold and fails its row.
        return must_cite.get(case_id, ())
    return tuple(
        (cast(str, entry["source_id"]), cast(str, entry["sha256"]),
         cast(int, entry["start"]), cast(int, entry["end"]))
        for entry in cast(list[dict[str, object]], case["gold"])
    )


def _first_match(chunks: tuple[Coordinates, ...], gold: _Gold) -> int | None:
    return next(
        (position for position, chunk in enumerate(chunks, start=1)
         if (chunk[0] == gold if isinstance(gold, str) else meets(chunk, gold))),
        None,
    )


def _figures(
    gold: tuple[_Gold, ...], ranked: tuple[Coordinates, ...],
    evidence: tuple[Coordinates, ...], manifest: Manifest,
) -> _Figures:
    # The shared manifest check takes graded coordinates. All case gold is relevant.
    coordinates, coordinate_drops = held_passages(
        ((entry, RELEVANT_GRADE) for entry in gold if not isinstance(entry, str)), manifest,
    )
    held: list[_Gold] = [coordinate for coordinate, _grade in coordinates]
    dropped: list[tuple[str, DropKind]] = [
        (drop.passage[0][0], drop.kind) for drop in coordinate_drops
    ]
    for entry in gold:
        if isinstance(entry, str):
            if entry in manifest:
                held.append(entry)
            else:
                dropped.append((entry, "absent"))
    ranks = tuple(rank for entry in held if (rank := _first_match(ranked, entry)) is not None)
    passages = tuple(
        number for entry in held if (number := _first_match(evidence, entry)) is not None
    )
    return _Figures(
        gold_passages=len(gold),
        held_gold_passages=len(held),
        dropped=tuple(dropped),
        least_rank=min(ranks, default=None),
        met_at_10=sum(rank <= RECALL_DEPTH_10 for rank in ranks),
        met_at_50=sum(rank <= RECALL_DEPTH for rank in ranks),
        met_in_pool=len(ranks),
        met_in_evidence=len(passages),
        least_passage_number=min(passages, default=None),
    )


def _measure(
    eval_set: LoadedSet, turns: Mapping[str, _Turn], generation: GenerationAccess, context: RunContext,
) -> tuple[dict[str, _Figures], dict[str, str], Problem | None]:
    pools = {case_id: turn.chunk_ids for case_id, turn in turns.items() if turn.problem is None}
    if not pools:
        return {}, {}, None
    context.checkpoint()
    resolved = pool_coordinates.read_coordinates(generation.name, pools, context)
    if isinstance(resolved, Problem):
        return {}, dict.fromkeys(pools, "record-unreachable"), resolved

    ranked: dict[str, tuple[Coordinates, ...]] = {}
    problems: dict[str, str] = {}
    for case_id, entries in resolved.items():
        coordinates_at_rank = tuple(entry for entry in entries if entry is not None)
        if len(coordinates_at_rank) != len(entries):
            problems[case_id] = "pool-unresolved"
        else:
            ranked[case_id] = coordinates_at_rank
    must_cite = {signoff.case_id: signoff.must_cite for signoff in eval_set.signoffs}
    gold = {case_id: _gold(eval_set.cases_by_id[case_id], must_cite, case_id) for case_id in pools}
    source_ids = {entry if isinstance(entry, str) else entry[0] for entries in gold.values() for entry in entries}
    context.checkpoint()
    manifest = golddrop_slice.read_manifest(generation.name, tuple(sorted(source_ids)), context)
    if isinstance(manifest, Problem):
        return {}, problems | dict.fromkeys(ranked, "record-unreachable"), manifest

    figures: dict[str, _Figures] = {}
    for case_id, ranking in ranked.items():
        context.checkpoint()
        turn = turns[case_id]
        by_chunk_id = dict(zip(turn.chunk_ids, ranking, strict=True))
        evidence = tuple(by_chunk_id[chunk_id] for chunk_id in turn.evidence)
        figures[case_id] = _figures(gold[case_id], ranking, evidence, manifest)
    return figures, problems, None


def _result(
    case_id: str, turn: _Turn, figures: _Figures | None, problem: str | None,
    *, requires_complete: bool = False, unsigned: bool = False,
) -> CaseResult:
    metrics: dict[str, JSONValue] = {"unsigned": True} if unsigned else {}
    if problem is not None:
        metrics["problem"] = problem
        if turn.turn_state is not None:
            metrics["turn_state"] = turn.turn_state
        passed = False
    else:
        passed = True
        if not unsigned:
            assert figures is not None
            metrics.update({
                "gold_passages": figures.gold_passages,
                "held_gold_passages": figures.held_gold_passages,
                "dropped": [{"source_id": source_id, "kind": kind} for source_id, kind in figures.dropped],
                "least_rank": figures.least_rank,
                "met_at_10": figures.met_at_10,
                "met_at_50": figures.met_at_50,
                "met_in_evidence": figures.met_in_evidence,
                "least_passage_number": figures.least_passage_number,
            })
            passed = (
                0 < figures.held_gold_passages == figures.met_in_evidence
                and (not requires_complete or turn.end == "complete")
            )
        metrics.update(
            pool_size=len(turn.chunk_ids), evidence_size=len(turn.evidence),
            end=turn.end, attempts=turn.attempts,
        )
    judge_field = (
        None if turn.grading is None else cast(Mapping[str, JSONValue], judge.render_judge(turn.grading))
    )
    return CaseResult(
        case_id, 1, "pass" if passed else "fail", metrics, judge=judge_field,
        latency_ms=turn.latency_ms, provenance_ref=turn.message_id,
    )


def _head(
    slice_name: str, selected: Selection, generation: GenerationAccess | None, context: RunContext,
    *, counted: int, unsigned_run: int = 0,
) -> str:
    baseline = next(
        (word for word, overrides in baselines.BASELINES.items() if overrides == context.turn_overrides),
        "shipped",
    )
    target = "unavailable" if generation is None else f"{generation.name} ({generation.corpus_lockfile})"
    mode = "tuning run, " if context.tuning else ""
    unsigned = f"{unsigned_run} unsigned run" if context.unsigned else f"{len(selected.unsigned)} unsigned excluded"
    return (
        f"{slice_name}: {mode}{counted} counted, {len(selected.tuning)} tuning, "
        f"{len(selected.held_out)} held-out, {unsigned}, "
        f"definition {DEFINITION_ID}, generation {target}, door {research_door.TURN_PATH}, "
        f"config hash {context.expected_config_hash or 'unavailable'}, baseline {baseline}"
    )


def _grading_lines(
    prompt: judge.JudgePrompt, turns: Iterable[_Turn], *, stopped: int = 0,
) -> tuple[str, ...]:
    grades: Counter[int] = Counter()
    ungraded: Counter[str] = Counter(problem=stopped)
    failures: Counter[str] = Counter()
    for turn in turns:
        grading = turn.grading
        if grading is None:
            if turn.problem is not None:
                reason = "problem"
            elif turn.end in _UNGRADED_ENDS:
                reason = cast(str, turn.end)
            else:
                reason = "no-reference"
            ungraded[reason] += 1
        elif grading.failure is not None:
            cause = grading.failure.code
            if cause == "schema-violation":
                _, _, path = grading.failure.detail.partition(" at ")
                if path:
                    cause = f"{cause} {path}"
            failures[cause] += 1
        else:
            assert grading.verdict is not None
            score = grading.verdict.document["score"]
            assert type(score) is int
            grades[score] += 1

    properties = cast(Mapping[str, Mapping[str, object]], prompt.schema["properties"])
    scores = cast(list[int], properties["score"]["enum"])
    grade_counts = ", ".join(f"grade {score} {grades[score]}" for score in scores)
    ungraded_counts = ", ".join(f"{reason} {ungraded[reason]}" for reason in _UNGRADED_REASONS)
    failure_counts = ", ".join(f"{cause} {count}" for cause, count in sorted(failures.items()))
    return (
        f"graded {grades.total()}: {grade_counts}",
        f"ungraded {ungraded.total()}: {ungraded_counts}",
        f"failed gradings {failures.total()}: {failure_counts or 'none'}",
    )


def _unsigned_line(results: tuple[CaseResult, ...]) -> str:
    completed = sum(result.verdict == "pass" for result in results)
    return f"unsigned: completed {completed}, failed {len(results) - completed}"


def _report(
    slice_name: str, head: str, turns: Mapping[str, _Turn], figures: Mapping[str, _Figures],
    problems: Mapping[str, str], read_problem: Problem | None, verdict: bool,
    *, prompt: judge.JudgePrompt | None, unsigned_results: tuple[CaseResult, ...] | None,
) -> str:
    held = sum(case.held_gold_passages for case in figures.values())
    ends = Counter(turn.end for turn in turns.values() if turn.end is not None)
    end_counts = ", ".join(f"{word} {ends[word]}" for word in get_args(EndWord))
    failed_turns = " ".join(f"{case_id}={turn.problem}" for case_id, turn in turns.items() if turn.problem)
    unresolved = " ".join(f"{case_id}={code}" for case_id, code in problems.items() if code == "pool-unresolved")
    dropped = Counter(kind for case in figures.values() for _, kind in case.dropped)
    kinds = ", ".join(f"{kind} {dropped[kind]}" for kind in DROP_KINDS)
    dropped_cases = " ".join(case_id for case_id, case in figures.items() if not case.held_gold_passages)
    outside_evidence = " ".join(
        case_id for case_id, case in figures.items() if case.met_in_evidence < case.held_gold_passages
    )
    outside_pool = " ".join(
        case_id for case_id, case in figures.items() if case.met_in_pool < case.held_gold_passages
    )
    lines = [
        head,
        f"recall@{RECALL_DEPTH_10} {sum(case.met_at_10 for case in figures.values())}/{held}",
        f"recall@{RECALL_DEPTH} {sum(case.met_at_50 for case in figures.values())}/{held}",
        f"recall in evidence {sum(case.met_in_evidence for case in figures.values())}/{held}",
        f"answer ends: {end_counts}",
        f"failed turns: {failed_turns or 'none'}",
        f"unresolved pools: {unresolved or 'none'}",
        f"dropped gold {dropped.total()} ({kinds})",
        f"dropped cases: {dropped_cases or 'none'}",
        f"gold outside evidence: {outside_evidence or 'none'}",
        f"gold outside pool: {outside_pool or 'none'}",
    ]
    if read_problem is not None:
        unread = " ".join(case_id for case_id in turns if problems.get(case_id) == "record-unreachable")
        if unread:
            lines.append(f"record-unreachable cases: {unread}")
        lines.extend(report.failure_lines(read_problem.fix, f"record-unreachable: {read_problem.problem}"))
    if prompt is not None:
        lines.extend(_grading_lines(prompt, turns.values()))
    if unsigned_results is not None:
        lines.append(_unsigned_line(unsigned_results))
    lines.append(f"{slice_name}: {'pass' if verdict else 'fail'}")
    return "\n".join(lines) + "\n"


def _failed(
    slice_name: str, case_ids: tuple[str, ...], code: str, problem: Problem,
    *, head: str, prompt: judge.JudgePrompt | None,
    unsigned_results: tuple[CaseResult, ...] | None,
) -> SliceResult:
    """Fail every counted case and report the problem with its fix.

    ``unsigned_results`` is None for a run without ``--unsigned``; with it, the
    rows of any unsigned turns already sent are returned beside the failures.
    """

    results = tuple(CaseResult(case_id, 1, "fail", {"problem": code}) for case_id in case_ids)
    grading = () if prompt is None else _grading_lines(prompt, (), stopped=len(case_ids))
    lines = (
        head,
        *report.failure_lines(problem.fix, f"{code}: {problem.problem}"),
        *grading,
        *(() if unsigned_results is None else (_unsigned_line(unsigned_results),)),
        f"{slice_name}: fail",
    )
    return SliceResult(False, "\n".join(lines) + "\n", results + (unsigned_results or ()))


def _run_unsigned(
    eval_set: LoadedSet, unsigned_ids: tuple[str, ...], generation: GenerationAccess,
    expected_hash: str, context: RunContext,
) -> tuple[CaseResult, ...] | None:
    """Send the unsigned turns a ``--unsigned`` run asks for, marked and ungraded."""

    if not context.unsigned:
        return None
    turns = _run_turns(eval_set, tuple(sorted(unsigned_ids)), generation, expected_hash, context, None)
    return tuple(_result(case_id, turn, None, turn.problem, unsigned=True) for case_id, turn in turns.items())


def _run(
    eval_set: LoadedSet, slice_name: str, context: RunContext, category: tuple[str, str],
) -> SliceResult:
    selected = select_cases(eval_set, slice_name)
    case_ids = tuple(sorted(tuning_cases(eval_set, slice_name) if context.tuning else selected.counted))
    generation = context.generation
    expected_hash = context.expected_config_hash
    prompt = None if context.judge_prompt_id is None else judge.PROMPT_REGISTRY[context.judge_prompt_id]
    head = _head(slice_name, selected, generation, context, counted=len(case_ids))
    if generation is None:
        retry = report.command(f"eval run --slice {slice_name} --generation <name>")
        return _failed(slice_name, case_ids, "generation-unresolved", Problem(
            "the stores generation was not resolved",
            f"Run {retry} through the command's stores preconditions, then retry.",
        ), head=head, prompt=prompt, unsigned_results=() if context.unsigned else None)
    if expected_hash is None:
        return _failed(slice_name, case_ids, "config-differs", Problem(
            "the expected retrieval configuration hash was not prepared",
            f"Run the {slice_name} slice through the command's Research preconditions, then retry.",
        ), head=head, prompt=prompt, unsigned_results=() if context.unsigned else None)
    # A supplied set may list any case in the slice; one of another category would
    # take this category's verdict, so the run stops before its first turn.
    foreign_cases = tuple(
        case_id for case_id in sorted((*case_ids, *selected.unsigned))
        if (eval_set.cases_by_id[case_id].get("suite"), eval_set.cases_by_id[case_id].get("category"))
        != category
    )
    if foreign_cases:
        return _failed(slice_name, case_ids, "case-outside-category", Problem(
            f"selected cases outside {category[0]}/{category[1]}: {' '.join(foreign_cases)}",
            f"List only {category[0]}/{category[1]} cases in the set's {slice_name} slice, then retry.",
        ), head=head, prompt=prompt, unsigned_results=() if context.unsigned else None)
    if not case_ids:
        # The slice fails with no counted case, but the unsigned turns a run
        # asked for still run, so a harvest-only slice can be exercised.
        unsigned_results = _run_unsigned(eval_set, selected.unsigned, generation, expected_hash, context)
        if unsigned_results is not None:
            head = _head(slice_name, selected, generation, context, counted=0, unsigned_run=len(unsigned_results))
        return _failed(slice_name, case_ids, "no-cases", Problem(
            "the slice has no counted cases",
            f"List an active, counted {category[0]}/{category[1]} case in the set's {slice_name} slice, then retry.",
        ), head=head, prompt=prompt, unsigned_results=unsigned_results)

    turns = _run_turns(eval_set, case_ids, generation, expected_hash, context, prompt)
    unsigned_results = _run_unsigned(eval_set, selected.unsigned, generation, expected_hash, context)
    if unsigned_results is not None:
        head = _head(
            slice_name, selected, generation, context, counted=len(case_ids), unsigned_run=len(unsigned_results),
        )
    figures, problems, read_problem = _measure(eval_set, turns, generation, context)
    results = tuple(
        _result(
            case_id, turn, figures.get(case_id), turn.problem or problems.get(case_id),
            requires_complete=prompt is not None,
        )
        for case_id, turn in turns.items()
    )
    verdict = all("problem" not in result.metrics for result in results)
    return SliceResult(
        verdict,
        _report(
            slice_name, head, turns, figures, problems, read_problem, verdict,
            prompt=prompt, unsigned_results=unsigned_results,
        ),
        results + (unsigned_results or ()),
    )


def runner_for(category: tuple[str, str]) -> Callable[[LoadedSet, str, RunContext], SliceResult]:
    """Bind the Research question runner to one suite and category."""

    def run(eval_set: LoadedSet, slice_name: str, context: RunContext) -> SliceResult:
        return _run(eval_set, slice_name, context, category)

    return run
