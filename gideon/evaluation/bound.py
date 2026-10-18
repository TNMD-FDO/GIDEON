"""Report the rule-of-three bound without gating on it.

A clean n of n allows a true failure rate up to 3/n at 95 % one-sided.
The bound reports what the clean run still allows and never gates.
"""

from typing import Final

RULE_NUMERATOR: Final[int] = 3
ONE_SIDED_LEVEL_PERCENT: Final[int] = 95


def upper_bound_percent(count: int) -> float | None:
    """Return the unrounded percentage for a non-negative clean-run count."""

    if count == 0:
        return None
    return min(100.0, 100.0 * RULE_NUMERATOR / count)


def bound_text(count: int, verdict: bool) -> str:
    """State the count and its bound only when the verdict is passing."""

    cases = f"{count} {'case' if count == 1 else 'cases'}"
    if count == 0:
        return f"{cases}, no bound"
    rule = f"{RULE_NUMERATOR}/n at {ONE_SIDED_LEVEL_PERCENT} %"
    if not verdict:
        return f"{cases}, failed: no bound ({rule} is a clean run's)"
    return (
        f"{cases}, true failure rate ≤ {upper_bound_percent(count):.1f} % "
        f"({rule})"
    )
