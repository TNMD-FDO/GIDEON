"""Run the reference-guided judge over the frozen triples slice."""

import hashlib
from collections.abc import Mapping
from typing import cast

from gideon.evaluation import judge
from gideon.evaluation.evalset import Case, LoadedSet, select_cases
from gideon.evaluation.results import CaseResult, JSONValue, RunContext, SliceResult


def _triple_inputs(case: Case) -> tuple[str, str, str, tuple[int, int]]:
    question = case["question"]
    expected = case["expected"]
    candidate = case["candidate"]
    assert isinstance(question, str)
    assert isinstance(expected, Mapping)
    assert isinstance(candidate, str)
    reference = expected["answer"]
    band = expected["band"]
    assert isinstance(reference, str)
    assert isinstance(band, list)
    assert len(band) == 2
    low, high = band
    assert type(low) is int
    assert type(high) is int
    return question, reference, candidate, (low, high)


def _figure(value: int | float | None) -> str:
    return "unknown" if value is None else str(value)


def _progress_line(case_id: str, repeat: int, grading: judge.Grading) -> str:
    if grading.verdict is None:
        assert grading.failure is not None
        outcome = grading.failure.code
        mode_count = "unknown"
        reason_length = "unknown"
        reason_digest = "unknown"
    else:
        outcome = str(grading.verdict.score)
        mode_count = str(len(grading.verdict.failure_modes))
        reason = grading.verdict.reason.encode("utf-8")
        reason_length = str(len(grading.verdict.reason))
        reason_digest = hashlib.sha256(reason).hexdigest()[:12]
    return (
        f"judge {case_id} repeat {repeat}: {outcome}; modes {mode_count}; "
        f"reason_length {reason_length}; reason_sha256 {reason_digest}; "
        f"prompt_tokens {_figure(grading.prompt_tokens)}; "
        f"completion_tokens {_figure(grading.completion_tokens)}; "
        f"seconds {_figure(grading.elapsed_seconds)}"
    )


def _report(
    case_ids: tuple[str, ...],
    bands: Mapping[str, tuple[int, int]],
    results: tuple[CaseResult, ...],
    repeats: int,
) -> str:
    by_case: dict[str, list[CaseResult]] = {case_id: [] for case_id in case_ids}
    for result in results:
        by_case[result.case_id].append(result)

    header = ["id", "band"]
    for repeat in range(1, repeats + 1):
        header.extend((f"repeat {repeat} score", f"repeat {repeat} in band"))
    header.append("equal")
    lines = [" | ".join(header)]
    in_band_cases = 0
    equal_cases = 0
    failed = 0
    for case_id in case_ids:
        case_results = by_case[case_id]
        scores: list[int] = []
        row = [case_id, f"{bands[case_id][0]}-{bands[case_id][1]}"]
        all_in_band = True
        for result in case_results:
            judge_field = result.judge
            assert judge_field is not None
            if "score" not in judge_field:
                row.extend((f"failed:{judge_field['failed']}", "unknown"))
                all_in_band = False
                failed += 1
                continue
            score = judge_field["score"]
            in_band = judge_field.get("in_band")
            assert type(score) is int
            assert type(in_band) is bool
            scores.append(score)
            row.extend((str(score), str(in_band)))
            all_in_band = all_in_band and in_band
        equal = len(scores) == repeats and len(set(scores)) == 1
        if all_in_band and len(case_results) == repeats:
            in_band_cases += 1
        if equal:
            equal_cases += 1
        row.append(str(equal))
        lines.append(" | ".join(row))
    lines.append(
        f"in band {in_band_cases} of {len(case_ids)}, equal {equal_cases} of "
        f"{len(case_ids)}, failed gradings {failed}"
    )
    return "\n".join(lines) + "\n"


def run_judge_triples(eval_set: LoadedSet, slice_name: str, context: RunContext) -> SliceResult:
    """Grade active triples in id order, completing every earlier repeat first."""

    assert context.judge_prompt_id is not None
    assert context.served_model_name is not None
    prompt = judge.PROMPT_REGISTRY[context.judge_prompt_id]
    selected = select_cases(eval_set, slice_name)
    case_ids = tuple(sorted(selected.counted))
    bands: dict[str, tuple[int, int]] = {}
    inputs: dict[str, tuple[str, str, str]] = {}
    for case_id in case_ids:
        question, reference, candidate, band = _triple_inputs(eval_set.cases_by_id[case_id])
        bands[case_id] = band
        inputs[case_id] = (question, reference, candidate)

    case_results: list[CaseResult] = []
    for repeat in range(1, context.repeats + 1):
        for case_id in case_ids:
            question, reference, candidate = inputs[case_id]
            grading = judge.grade(
                context.host,
                context.rendered_dir,
                served_model_name=context.served_model_name,
                prompt=prompt,
                question=question,
                reference=reference,
                candidate=candidate,
            )
            judge_field = cast(
                Mapping[str, JSONValue], judge.render_judge(grading, band=bands[case_id])
            )
            metrics: Mapping[str, JSONValue] = {
                "prompt_tokens": grading.prompt_tokens,
                "completion_tokens": grading.completion_tokens,
            }
            latency_ms = (
                None
                if grading.elapsed_seconds is None
                else grading.elapsed_seconds * 1000
            )
            case_results.append(
                CaseResult(
                    case_id,
                    repeat,
                    "pass" if grading.verdict is not None else "fail",
                    metrics,
                    judge=judge_field,
                    latency_ms=latency_ms,
                )
            )
            context.progress(_progress_line(case_id, repeat, grading))

    results = tuple(case_results)
    failed = sum(result.verdict != "pass" for result in results)
    return SliceResult(
        failed == 0,
        _report(case_ids, bands, results, context.repeats),
        results,
    )
