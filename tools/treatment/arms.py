"""Named rule variations for measuring treatment language."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, fields, replace
from typing import Any, Final

from gideon.worker.treatment import RULES_1, PatternRules


@dataclass(frozen=True, slots=True)
class Arm:
    """One named rule set and whether its figures can guide selection."""

    name: str
    rules: PatternRules
    description: str
    selectable: bool


ARMS: Final[tuple[Arm, ...]] = (
    Arm("baseline", RULES_1, "The first pattern set, unchanged", True),
    Arm("window-150", replace(RULES_1, window=150), "Shorter citation window", True),
    Arm("window-600", replace(RULES_1, window=600), "Longer citation window", True),
    Arm("hyphenated", replace(RULES_1, hyphenated=True), "Hyphenated verbs", True),
    Arm(
        "procedural-objects", replace(RULES_1, procedural_objects=True),
        "Exclude procedural objects", True,
    ),
    Arm("negator-5", replace(RULES_1, negator_reach=5), "Longer negator reach", True),
    Arm("semicolon", replace(RULES_1, semicolon_boundary=True), "Semicolon boundary", True),
    Arm("no-direction", replace(RULES_1, direction=False), "Direction diagnostic", False),
    Arm(
        "nearest-citation", replace(RULES_1, attribution="nearest"),
        "Citation attribution diagnostic", False,
    ),
)
# The shipped rules: a registered arm's name, or a composition of them.
SHIPPED: Final = "hyphenated,procedural-objects"


def arm(name: str) -> Arm:
    """Look up one registered arm or name the available choices."""

    for candidate in ARMS:
        if candidate.name == name:
            return candidate
    choices = ", ".join(candidate.name for candidate in ARMS)
    raise ValueError(f"unknown arm {name!r}; choose from: {choices}")


def compose(names: Sequence[str]) -> Arm:
    """Combine distinct selectable one-field changes from the baseline."""

    if not names or any(not name for name in names):
        raise ValueError("composition requires at least one arm name")
    seen: set[str] = set()
    changes: dict[str, Any] = {}
    for name in names:
        if name in seen:
            raise ValueError(f"arm {name!r} appears twice in composition")
        seen.add(name)
        chosen = arm(name)
        if not chosen.selectable:
            raise ValueError(f"arm {name!r} is diagnostic and cannot be composed")
        differing = tuple(
            field.name for field in fields(PatternRules)
            if getattr(chosen.rules, field.name) != getattr(RULES_1, field.name)
        )
        if len(differing) > 1:
            raise ValueError(f"arm {name!r} changes more than one rule field")
        if not differing:
            continue
        field_name = differing[0]
        if field_name in changes:
            raise ValueError(f"two arms change {field_name}")
        changes[field_name] = getattr(chosen.rules, field_name)
    joined = ",".join(names)
    return Arm(joined, replace(RULES_1, **changes), f"Composition of {joined}", True)


def shipped() -> Arm:
    """Resolve the shipped arm, registered or composed."""

    names = SHIPPED.split(",")
    return arm(SHIPPED) if len(names) == 1 else compose(names)
