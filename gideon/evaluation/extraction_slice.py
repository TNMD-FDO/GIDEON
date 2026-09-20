"""Run the extraction grammar over the frozen extraction slice."""

import time
from collections import Counter
from collections.abc import Mapping
from typing import cast

from gideon.evaluation.evalset import Case, LoadedSet
from gideon.evaluation.results import CaseResult, RunContext, SliceResult
from gideon.extraction import ExactObject
from gideon.extraction.grammar import extract, registry_types
from gideon.extraction.scoring import SetScore, build_report, score


def _objects(case: Case) -> tuple[Mapping[str, object], ...]:
    expected = case.get("expected")
    if not isinstance(expected, Mapping):
        return ()
    values = expected.get("objects")
    if not isinstance(values, list):
        return ()
    return tuple(value for value in values if isinstance(value, Mapping))


def _type_counts(
    case: Case,
    extracted: tuple[ExactObject, ...],
    result: SetScore,
    landed: frozenset[str],
) -> tuple[Mapping[str, Mapping[str, int]], str]:
    case_id = cast(str, case["id"])
    labels: Counter[str] = Counter()
    for value in _objects(case):
        object_type = value.get("type")
        if isinstance(object_type, str):
            labels[object_type] += 1
    misses: Counter[str] = Counter(
        finding.type for finding in result.misses if finding.case_id == case_id
    )
    false_hits: Counter[str] = Counter(
        finding.type for finding in result.false_hits if finding.case_id == case_id
    )
    types = sorted(set(labels) | {value.type for value in extracted})
    metrics: dict[str, Mapping[str, int]] = {}
    for object_type in types:
        miss_count = misses[object_type]
        metrics[object_type] = {
            "hits": labels[object_type] - miss_count,
            "false_hits": false_hits[object_type],
            "misses": miss_count,
        }
    failed = any(
        metrics[object_type]["misses"] or metrics[object_type]["false_hits"]
        for object_type in landed
        if object_type in metrics
    )
    return metrics, "fail" if failed else "pass"


def run_extraction(eval_set: LoadedSet, slice_name: str, context: RunContext) -> SliceResult:
    """Run the extraction measure for *slice_name* in deterministic id order."""

    del context
    ids = eval_set.slices[slice_name]
    active = set(eval_set.active_ids)
    cases = tuple(eval_set.cases_by_id[case_id] for case_id in ids if case_id in active)
    extracted: dict[str, tuple[ExactObject, ...]] = {}
    latencies: dict[str, float] = {}
    for case in cases:
        case_id = cast(str, case["id"])
        question = case.get("question")
        source = question if isinstance(question, str) else ""
        started = time.monotonic_ns()
        extracted[case_id] = extract(source)
        latencies[case_id] = (time.monotonic_ns() - started) / 1_000_000

    landed = frozenset(registry_types())
    result = score((cases,), extracted, landed)
    case_results: list[CaseResult] = []
    for case in cases:
        case_id = cast(str, case["id"])
        metrics, verdict = _type_counts(case, extracted[case_id], result, landed)
        case_results.append(
            CaseResult(case_id, 1, verdict, metrics, judge=None, latency_ms=latencies[case_id])
        )
    return SliceResult(result.verdict, build_report(result), tuple(case_results))
