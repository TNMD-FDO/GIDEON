"""Compute the exact binomial ceiling for the family's false-refusal gate.

At a rate given as (declined, controls), the ceiling is the smallest count
from zero through the trial count whose probability of being exceeded is at
most the remainder of ONE_SIDED_LEVEL_PERCENT. Exact fractions keep the gate
deterministic at the boundary. Zero trials or a zero rate give zero; a rate
of one gives the trial count.
"""

from collections.abc import Iterator
from fractions import Fraction
from math import comb
from typing import Final

ONE_SIDED_LEVEL_PERCENT: Final[int] = 99
"""The one-sided level, a starting value changed only by a ruling."""


def _masses(rate: tuple[int, int], trials: int) -> Iterator[Fraction]:
    if (
        not isinstance(rate, tuple)
        or len(rate) != 2
        or any(type(value) is not int for value in rate)
        or rate[0] < 0
        or rate[1] <= 0
        or rate[0] > rate[1]
    ):
        raise ValueError("rate must be (declined, controls) with 0 <= declined <= controls and controls > 0")
    if type(trials) is not int or trials < 0:
        raise ValueError("trials must be a non-negative integer")
    probability = Fraction(*rate)
    for count in range(trials + 1):
        yield comb(trials, count) * probability**count * (1 - probability)**(trials - count)


def tail_above(rate: tuple[int, int], trials: int, count: int) -> Fraction:
    """Return the exact probability of exceeding count."""

    return sum(
        (mass for index, mass in enumerate(_masses(rate, trials)) if index > count),
        Fraction(0),
    )


def upper_count(rate: tuple[int, int], trials: int) -> int:
    """Return the false-refusal ceiling at the module's one-sided level."""

    cumulative = Fraction(0)
    level = Fraction(ONE_SIDED_LEVEL_PERCENT, 100)
    for count, mass in enumerate(_masses(rate, trials)):
        cumulative += mass
        if cumulative >= level:
            return count
    return trials
