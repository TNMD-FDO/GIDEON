"""Classify a hit's authority relative to the turn's home jurisdiction."""

from dataclasses import dataclass
from typing import Final, Literal, get_args

AuthorityTier = Literal[
    "scotus", "circuit_published", "circuit_unpublished", "district_home", "state_home",
    "persuasive", "authority", "matter",
]
HitSource = Literal["caselaw", "authorities", "matter"]
AUTHORITY_TIERS: Final[tuple[str, ...]] = get_args(AuthorityTier)
# Held equal to the worker's vocabulary by the tier test, across the service boundary.
PRECEDENTIAL_VALUES: Final = ("published", "unpublished", "unknown")


@dataclass(frozen=True, slots=True)
class Jurisdiction:
    """Court ids resolved for the site's home jurisdiction."""

    scotus: tuple[str, ...]
    circuit: tuple[str, ...]
    districts: tuple[str, ...]
    states: tuple[str, ...]

    @property
    def controlling_courts(self) -> frozenset[str]:
        """The union of the four groups of controlling courts."""

        return frozenset((*self.scotus, *self.circuit, *self.districts, *self.states))


def authority_tier(source: HitSource, court: str, precedential: str, jurisdiction: Jurisdiction) -> AuthorityTier:
    """Label a hit from its source, court, and validated precedential value; an unknown status reads as unpublished."""

    if source == "authorities":
        return "authority"
    if source == "matter":
        return "matter"
    if court in jurisdiction.scotus:
        return "scotus"
    if court in jurisdiction.circuit:
        return "circuit_published" if precedential == "published" else "circuit_unpublished"
    if court in jurisdiction.districts:
        return "district_home"
    if court in jurisdiction.states:
        return "state_home"
    return "persuasive"
