"""The exact-object contract: what the grammar emits and a label records.

An :class:`ExactObject` is an object present verbatim in the user's message:
its ``text`` is exactly the source's ``[start:end]`` slice, the offsets
half-open code points of the string; nothing is respelled or normalized away
(ADR-0019).  The objects of one extraction never overlap and are ordered by
``start``.  The type vocabulary is closed and written once, for both
extractors and both halves of the grammar, so a label never renames.

A keyed type always carries its authority's permanent key, at section level
(ADR-0024), and every other type never does:

- ``statute`` — ``/us/usc/t<title>/s<section>``, ``/us/usc/t18/s3663A``;
- ``guideline`` — ``ussg/<id>``, the id upper-cased as every Manual prints it;
- ``court_rule`` — the USLM path of its set, ``/us/usc/t18a/courtRules/Crim/rule<N>``
  and ``/us/usc/t28a/courtRules/{Civil,App,Evid}/rule<N>``;
- ``regulation`` — ``cfr/<title>/<section>``; ``habeas_rule`` —
  ``rules/2254/rule<N>`` or ``rules/2255/rule<N>``; ``scotus_rule`` —
  ``rules/scotus/rule<N>``; ``appendix_statute`` — its USLM appendix path.

The parenthesised designators after a section (``(b)(1)``) are inside the
span and carried beside the key as ``subsections`` (``("b", "1")``), in order
and as typed, never inside it: the subsection anchor's form is not yet
minted, and the key is what ``authority_version(id, date?)`` takes.  Key
comparison folds case, because a U.S.C. section's letter cannot be derived
from the text (``3663A`` against ``78j``), so the key carries it as typed.

A ``bare_section`` (a section with no title) and a ``bare_rule`` (a rule
number with no rule set) never carry a key: the text does not state the
authority, and deterministic code never guesses one (ADR-0006) — ``Rule 41``
is a rule of three sets.
"""


from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, get_args

type ObjectType = Literal[
    "statute",
    "guideline",
    "court_rule",
    "bare_section",
    "bare_rule",
    "regulation",
    "appendix_statute",
    "habeas_rule",
    "scotus_rule",
    "docket",
    "case_cite",
    "state_code",
    "caption",
]

OBJECT_TYPES: Final[tuple[ObjectType, ...]] = get_args(ObjectType.__value__)

KEYED_TYPES: Final[frozenset[ObjectType]] = frozenset(
    {
        "statute",
        "guideline",
        "court_rule",
        "regulation",
        "appendix_statute",
        "habeas_rule",
        "scotus_rule",
    }
)

SECTION_TYPES: Final[frozenset[ObjectType]] = frozenset(
    {
        "statute",
        "guideline",
        "court_rule",
        "bare_section",
        "bare_rule",
        "regulation",
        "appendix_statute",
        "habeas_rule",
        "scotus_rule",
    }
)
"""The section-like types, whose objects carry ``subsections``."""


@dataclass(frozen=True, slots=True)
class ExactObject:
    """One verbatim object emitted from a message or recorded as a label."""

    type: ObjectType
    start: int
    end: int
    text: str
    key: str | None = None
    pattern_id: str | None = None
    subsections: tuple[str, ...] = ()


def keys_equal(left: str | None, right: str | None) -> bool:
    """Compare authority keys without making citation case significant."""

    if left is None or right is None:
        return left is right
    return left.casefold() == right.casefold()


def span_violations(
    source: str, objects: Sequence[ExactObject]
) -> tuple[ExactObject, ...]:
    """Return objects whose recorded text is not their source slice."""

    return tuple(
        obj
        for obj in objects
        if obj.start < 0
        or obj.end <= obj.start
        or obj.end > len(source)
        or source[obj.start : obj.end] != obj.text
    )


def key_violations(objects: Sequence[ExactObject]) -> tuple[ExactObject, ...]:
    """Return objects that violate the keyed and unkeyed type rule."""

    return tuple(
        obj
        for obj in objects
        if obj.type not in OBJECT_TYPES
        or (obj.type in KEYED_TYPES and obj.key is None)
        or (obj.type not in KEYED_TYPES and obj.key is not None)
    )


def ordering_violations(
    objects: Sequence[ExactObject],
) -> tuple[ExactObject, ...]:
    """Return objects that are out of order or overlap their predecessor."""

    violations: list[ExactObject] = []
    previous: ExactObject | None = None
    for obj in objects:
        if previous is not None and obj.start < previous.end:
            violations.append(obj)
        previous = obj
    return tuple(violations)

