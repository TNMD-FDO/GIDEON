"""Report the build-gates composite's verdict over every required part."""

from collections.abc import Mapping
from typing import Final

from gideon.evaluation.results import CaseResult, SliceResult

PART_ORDER: Final[tuple[str, ...]] = (
    "segmenter-300", "count-gates", "gold-drop", "extraction",
)


def report(
    sub_results: Mapping[str, SliceResult],
    results: tuple[CaseResult, ...],
    unrouted: tuple[str, ...],
) -> tuple[bool, str, int | None]:
    """Gate on every part passing and every counted case being routed."""

    lines: list[str] = []
    gated_counts: list[int] = []
    passed = not unrouted
    for name in PART_ORDER:
        part = sub_results.get(name)
        if part is None:
            lines.append(f"{name}: not run")
            passed = False
        else:
            lines.extend(
                (f"{name} report (its verdict is build-gates'):", part.report.rstrip("\n"))
            )
            passed = passed and part.verdict
            if part.gated_cases is not None:
                gated_counts.append(part.gated_cases)
    lines.append(f"unrouted suite cases: {', '.join(unrouted) if unrouted else 'none'}")
    for name in PART_ORDER:
        part = sub_results.get(name)
        verdict = "not run" if part is None else "pass" if part.verdict else "fail"
        lines.append(f"  {name} {verdict}")
    lines.append(f"build-gates: {'pass' if passed else 'fail'}; {len(results)} cases")
    gated_cases = sum(gated_counts) if gated_counts else None
    return passed, "\n".join(lines) + "\n", gated_cases
