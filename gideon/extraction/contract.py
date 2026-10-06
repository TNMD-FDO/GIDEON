"""The exact-object contract: what the grammar emits and a label records.

An :class:`ExactObject` is an object present verbatim in the user's message:
its ``text`` is exactly the source's ``[start:end]`` slice, the offsets
half-open code points of the string; nothing is respelled or normalized away.
The objects of one extraction never overlap and are ordered by ``start``.
The type vocabulary is closed and written once, for both extractors and both
halves of the grammar, so a label never renames.

A keyed type always carries its authority's permanent key, at section level,
and every other type never does:

- ``statute`` — ``/us/usc/t<title>/s<section>``, ``/us/usc/t18/s3663A``;
- ``guideline`` — ``ussg/<id>``, the id upper-cased as every Manual prints it;
- ``court_rule`` — the USLM path of its set, ``/us/usc/t18a/courtRules/Crim/rule<N>``
  and ``/us/usc/t28a/courtRules/{Civil,App,Evid}/rule<N>``;
- ``regulation`` — ``cfr/<title>/<section>``; ``habeas_rule`` —
  ``rules/2254/rule<N>`` or ``rules/2255/rule<N>``; ``scotus_rule`` — its
  rule-level key, ``rules/scotus/rule<N>``; a dotted paragraph designator is
  carried in ``subsections`` in order (``14.1(a)`` becomes ``("1", "a")``),
  the one type whose designator is not parenthesised; ``appendix_statute`` —
  its USLM appendix path from the title/ordinal table, and a compilation
  outside that table is no object.

The ``docket`` object is the number alone and never carries a key.

A ``case_cite`` spans eyecite's ``span()``: the volume, reporter, and first
page. A reporter abbreviation's trailing period is inside the span when typed
before the page, and an apostrophe stays as typed. It carries no key or
subsections. A short-form cite, an ``Id.``, a ``supra``, and a reference by
party name alone are no object: none states a first page.

The parenthesised designators after a section (``(b)(1)``) are inside the
span and carried beside the key as ``subsections`` (``("b", "1")``), in order
and as typed, never inside it: the subsection anchor's form is not yet
minted, and the key is what ``authority_version(id, date?)`` takes.  Key
comparison folds case, because a U.S.C. section's letter cannot be derived
from the text (``3663A`` against ``78j``), so the key carries it as typed.

A ``bare_section`` (a section with no title) and a ``bare_rule`` (a rule
number with no rule set, including the unnamed habeas form ``Habeas Rule 6``)
never carry a key: the text does not state the authority, and deterministic
code never guesses one — ``Habeas Rule 6``, beside ``Rule 41``,
names no set.
"""


from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast, get_args

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

EYECITE_TYPES: Final[tuple[ObjectType, ...]] = ("case_cite",)

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


def combine_extractions(
    first: Sequence[ExactObject], second: Sequence[ExactObject]
) -> tuple[ExactObject, ...]:
    """Keep every first object; add a second one only where it overlaps none kept.

    The first extraction wins every overlap, as the grammar's precedence
    does; the second's are taken earliest start, then longest, so combining
    an extraction with nothing makes it disjoint. The result is by start.
    """

    selected = list(first)
    for obj in sorted(second, key=lambda value: (value.start, -(value.end - value.start))):
        if any(obj.start < kept.end and kept.start < obj.end for kept in selected):
            continue
        selected.append(obj)
    return tuple(sorted(selected, key=lambda value: value.start))


def object_to_wire(obj: ExactObject) -> dict[str, object]:
    """Write one exact object as a JSON-compatible mapping of all seven fields."""

    return {
        "type": obj.type,
        "start": obj.start,
        "end": obj.end,
        "text": obj.text,
        "key": obj.key,
        "pattern_id": obj.pattern_id,
        "subsections": list(obj.subsections),
    }


def object_from_wire(value: object) -> ExactObject:
    """Read the closed wire shape, refusing malformed objects."""

    if not isinstance(value, Mapping) or set(value) != {
        "type", "start", "end", "text", "key", "pattern_id", "subsections"
    }:
        raise ValueError("invalid exact-object fields")

    object_type = value["type"]
    start = value["start"]
    end = value["end"]
    source_text = value["text"]
    key = value["key"]
    pattern_id = value["pattern_id"]
    subsections = value["subsections"]
    if not isinstance(object_type, str) or object_type not in OBJECT_TYPES:
        raise ValueError("invalid exact-object type")
    if (
        type(start) is not int
        or type(end) is not int
        or start < 0
        or end <= start
        or not isinstance(source_text, str)
        or len(source_text) != end - start
    ):
        raise ValueError("invalid exact-object span")
    if key is not None and not isinstance(key, str):
        raise ValueError("invalid exact-object key")
    if pattern_id is not None and not isinstance(pattern_id, str):
        raise ValueError("invalid exact-object pattern id")
    if not isinstance(subsections, list) or any(
        not isinstance(part, str) for part in subsections
    ):
        raise ValueError("invalid exact-object subsections")

    obj = ExactObject(
        cast(ObjectType, object_type),
        start,
        end,
        source_text,
        key,
        pattern_id,
        tuple(subsections),
    )
    if key_violations((obj,)) or (obj.type not in SECTION_TYPES and obj.subsections):
        raise ValueError("invalid exact-object type fields")
    return obj


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
