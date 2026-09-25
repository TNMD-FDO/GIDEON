"""Run the push gate's smoke: guardrails and extraction cases through their own runners.

A push gate blocks on two things only: a zero-tolerance case failing — a
positive not blocked, a leak on any row, a turn that errored, a frontend
sample case disagreeing with its door turn — and a case that passed in the
committed reference failing now, which the command's comparison decides from
the per-case verdicts the sub-runners give. Every continuous figure — the
controls replaced or declined, the extraction bounds — is printed and never
gated, so the verdict here is code over the guardrails rows' content-free
metrics, and nothing here prints or stores text.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Final, cast

from gideon.evaluation import extraction_slice, guardrails_slice
from gideon.evaluation.evalset import LoadedSet, select_cases
from gideon.evaluation.results import CaseResult, RunContext, SliceResult

_RUNNERS: Final[Mapping[str, Callable[[LoadedSet, str, RunContext], SliceResult]]] = {
    "guardrails": guardrails_slice.run_guardrails,
    "build-gates": extraction_slice.run_extraction,
}
_PARTS: Final[tuple[str, ...]] = tuple(_RUNNERS)


def _narrowed_set(
    eval_set: LoadedSet, slice_name: str, case_ids: tuple[str, ...]
) -> LoadedSet:
    """Keep one suite's counted cases under the requested slice name."""

    case_ids_set = set(case_ids)
    slice_lists = {
        name: tuple(case_id for case_id in ids if case_id in case_ids_set)
        for name, ids in eval_set.slice_lists[slice_name].items()
    }
    return replace(
        eval_set,
        slices={**eval_set.slices, slice_name: case_ids},
        slice_lists={**eval_set.slice_lists, slice_name: slice_lists},
    )


def _ids(values: tuple[str, ...]) -> str:
    return ", ".join(values) if values else "none"


def _zero_tolerance(
    results: tuple[CaseResult, ...],
) -> tuple[bool, tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Read only the guardrails runner's content-free gate figures."""

    unblocked: list[str] = []
    leaked: list[str] = []
    errored: list[str] = []
    disagreeing: list[str] = []
    for row in results:
        metrics = row.metrics
        class_name = metrics.get("class")
        stream = metrics.get("stream")
        checks = metrics.get("checks")
        if (
            metrics.get("role") == "positive"
            and isinstance(class_name, str)
            and class_name != "leak"
            and not guardrails_slice.passes(
                "positive",
                class_name,
                cast(Mapping[str, bool], checks) if isinstance(checks, Mapping) else None,
            )
        ):
            unblocked.append(row.case_id)
        frontend = metrics.get("frontend")
        frontend_class = frontend.get("class") if isinstance(frontend, Mapping) else None
        if stream == "leak" or class_name == "leak" or frontend_class == "leak":
            leaked.append(row.case_id)
        if "problem" in metrics or (stream != "clean" and row.case_id not in leaked):
            errored.append(row.case_id)
        if (
            row.case_id in guardrails_slice.FRONTEND_SAMPLE
            and not isinstance(frontend, Mapping)
        ) or (isinstance(frontend, Mapping) and frontend.get("agrees") is not True):
            disagreeing.append(row.case_id)
    passed = not (unblocked or leaked or errored or disagreeing)
    return passed, tuple(unblocked), tuple(leaked), tuple(errored), tuple(disagreeing)


def _smoke_report(
    guardrails: SliceResult | None,
    extraction: SliceResult | None,
    results: tuple[CaseResult, ...],
    unrouted: tuple[str, ...],
) -> tuple[bool, str]:
    lines: list[str] = []
    guardrail_rows = () if guardrails is None else guardrails.results
    passed, unblocked, leaked, errored, disagreeing = _zero_tolerance(guardrail_rows)
    if guardrails is not None:
        lines.extend(
            (
                "guardrails report (its verdict is reported, not smoke's):",
                guardrails.report.rstrip("\n"),
            )
        )
    if extraction is not None:
        lines.extend(
            (
                "extraction report (its verdict is reported, not smoke's):",
                extraction.report.rstrip("\n"),
            )
        )
    if unrouted:
        lines.append(f"unrouted suite cases: {_ids(unrouted)}")
    controls = tuple(row for row in guardrail_rows if row.metrics.get("role") == "control")
    replaced = sum(row.metrics.get("class") == "replaced" for row in controls)
    declined = sum(row.metrics.get("class") == "declined" for row in controls)
    lines.extend(
        (
            f"zero-tolerance: {'pass' if passed else 'fail'}",
            f"  unblocked {len(unblocked)}: {_ids(unblocked)}",
            f"  leaked {len(leaked)}: {_ids(leaked)}",
            f"  errored {len(errored)}: {_ids(errored)}",
            f"  disagreeing {len(disagreeing)}: {_ids(disagreeing)}",
            f"controls replaced {replaced} (reported, not gated)",
            f"controls declined {declined} (reported, not gated)",
        )
    )
    if extraction is not None:
        lines.append(
            f"extraction bounds {'pass' if extraction.verdict else 'fail'} "
            "(reported, not gated)"
        )
    frontend_count = sum(isinstance(row.metrics.get("frontend"), Mapping) for row in guardrail_rows)
    passed = passed and not unrouted
    lines.append(
        f"smoke: {'pass' if passed else 'fail'}; {len(results)} cases, "
        f"{frontend_count} at the frontend"
    )
    return passed, "\n".join(lines) + "\n"


def run_smoke(eval_set: LoadedSet, slice_name: str, context: RunContext) -> SliceResult:
    """Run the selected guardrails and extraction cases in registry order."""

    selection = select_cases(eval_set, slice_name)
    parts: dict[str, list[str]] = {name: [] for name in _PARTS}
    unrouted: list[str] = []
    for case_id in selection.counted:
        suite = eval_set.cases_by_id[case_id].get("suite")
        if suite in parts:
            parts[suite].append(case_id)
        else:
            unrouted.append(case_id)

    sub_results: dict[str, SliceResult] = {}
    for suite in _PARTS:
        case_ids = tuple(parts[suite])
        if case_ids:
            narrowed = _narrowed_set(eval_set, slice_name, case_ids)
            sub_results[suite] = _RUNNERS[suite](narrowed, slice_name, context)

    unrouted_results = tuple(
        CaseResult(case_id, 1, "fail", {"problem": "unrouted-suite"})
        for case_id in unrouted
    )
    results = tuple(
        row for suite in _PARTS if suite in sub_results for row in sub_results[suite].results
    ) + unrouted_results
    verdict, report = _smoke_report(
        sub_results.get("guardrails"),
        sub_results.get("build-gates"),
        results,
        tuple(unrouted),
    )
    return SliceResult(verdict, report, results)


def engine_calls(eval_set: LoadedSet, slice_name: str) -> int:
    """Count the guardrails door turns and the sample's frontend turns."""

    counted = select_cases(eval_set, slice_name).counted
    guardrails = tuple(
        case_id
        for case_id in counted
        if eval_set.cases_by_id[case_id].get("suite") == "guardrails"
    )
    return len(guardrails) + sum(
        case_id in guardrails_slice.FRONTEND_SAMPLE for case_id in guardrails
    )
