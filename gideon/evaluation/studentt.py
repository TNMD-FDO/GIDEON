"""Compute Student's t probabilities and critical values on the standard library.

Abramowitz and Stegun 26.7.3 (odd degrees of freedom) and 26.7.4 (even)
give finite series in theta = atan(t / sqrt(df)) for the central probability
A(t), so the cumulative probability is (1 + A(t)) / 2. Symmetry gives
F(-t) = 1 - F(t), and a two-sided confidence c therefore needs F(t) = (1 + c) / 2.
Bisection inverts this identity to BISECTION_TOLERANCE in t, or floating-point
resolution. The tests read the NIST/SEMATECH e-Handbook's Student's t critical
values table (section 1.3.6.7.2) to three decimals, including the normal limit.
"""

from math import atan, cos, pi, sin, sqrt
from typing import Final

BISECTION_TOLERANCE: Final[float] = 1e-12
"""The absolute bracket-width tolerance for a critical value, far below the
three decimals used to read the published table."""


def cdf(t: float, df: int) -> float:
    """Return P(T <= t) at integer df >= 1 using the exact finite series."""

    if df < 1:
        raise ValueError("Student's t degrees of freedom must be at least one")
    if t == 0:
        return 0.5

    theta = atan(t / sqrt(df))
    if df == 1:
        return 0.5 + theta / pi

    cosine = cos(theta)
    cosine_squared = cosine * cosine
    if df % 2:
        # 26.7.3: theta + sin(theta) * (cos(theta) + 2/3 cos(theta)^3 + ...).
        term = cosine
        total = term
        for index in range(2, df - 1, 2):
            term *= index / (index + 1) * cosine_squared
            total += term
        return 0.5 + (theta + sin(theta) * total) / pi

    # 26.7.4: sin(theta) * (1 + 1/2 cos(theta)^2 + 3/8 cos(theta)^4 + ...).
    term = 1.0
    total = term
    for index in range(1, df - 1, 2):
        term *= index / (index + 1) * cosine_squared
        total += term
    return 0.5 + sin(theta) * total / 2


def two_sided_quantile(confidence: float, df: int) -> float:
    """Return positive t with P(-t <= T <= t) = confidence at integer df >= 1."""

    if df < 1:
        raise ValueError("Student's t degrees of freedom must be at least one")
    if not 0 < confidence < 1:
        raise ValueError("Student's t confidence must be strictly between zero and one")

    target = (1 + confidence) / 2
    lower = 0.0
    upper = 1.0
    while cdf(upper, df) < target:
        upper *= 2

    while upper - lower > BISECTION_TOLERANCE:
        midpoint = (lower + upper) / 2
        if midpoint in (lower, upper):
            break
        if cdf(midpoint, df) < target:
            lower = midpoint
        else:
            upper = midpoint
    return (lower + upper) / 2
