"""Per-type precision and recall of an extraction over labelled cases.

The measure is pure: case mappings (the set's JSONL shape, one sequence per
file) and the objects extracted from each active case's question go in, a
:class:`SetScore` comes out; nothing here opens a file.  The active set comes
first: a case another case's ``supersedes`` names is retired and contributes
no hit, miss, or false hit.  A hit is the same type, the same offsets, equal
``subsections``, and an equal key under :func:`keys_equal`; any other
extracted object is a false hit and any unmatched label a miss, so a right
span with a wrong key or wrong subsections is both.

The landed types are an argument: the caller passes the types the grammar's
registry declares, never the types an extraction returned, so a family that
emits nothing stays gated and fails on recall.  A gated type with no active
label fails too.  Findings name a case id, a type, and offsets, never text.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from gideon.extraction.contract import OBJECT_TYPES, ExactObject, ObjectType, keys_equal

type Case = Mapping[str, Any]

MIN_PRECISION: Final[float] = 0.95
"""Spec §11.2: each landed type's precision over the extraction set."""

MIN_RECALL: Final[float] = 0.90
"""Spec §11.2: each landed type's recall over the extraction set."""


@dataclass(frozen=True, slots=True)
class Finding:
    """A miss or a false hit, located without its words."""

    case_id: str
    type: ObjectType
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class TypeScore:
    """One type's counts, figures, and verdict."""

    type: ObjectType
    hits: int
    false_hits: int
    misses: int
    precision: float | None
    recall: float | None
    gated: bool
    verdict: bool


@dataclass(frozen=True, slots=True)
class SetScore:
    """The score over the active cases of every file handed in."""

    by_type: Mapping[ObjectType, TypeScore]
    by_origin: Mapping[str, Mapping[ObjectType, TypeScore]]
    unlanded_label_counts: Mapping[ObjectType, int]
    misses: tuple[Finding, ...]
    false_hits: tuple[Finding, ...]
    verdict: bool


@dataclass
class _Counts:
    hits: int = 0
    false_hits: int = 0
    misses: int = 0


def active_cases(files: Sequence[Sequence[Case]]) -> tuple[Case, ...]:
    """Return the cases of every file that no case's ``supersedes`` names."""

    cases = tuple(case for file in files for case in file)
    retired = {case["supersedes"] for case in cases if "supersedes" in case}
    return tuple(case for case in cases if case["id"] not in retired)


def _label_object(label: Mapping[str, Any]) -> ExactObject:
    return ExactObject(
        label["type"],
        label["start"],
        label["end"],
        label["text"],
        label.get("key"),
        subsections=tuple(label.get("subsections", ())),
    )


def _is_hit(label: ExactObject, extracted: ExactObject) -> bool:
    return (
        label.type == extracted.type
        and label.start == extracted.start
        and label.end == extracted.end
        and label.subsections == extracted.subsections
        and keys_equal(label.key, extracted.key)
    )


def _type_score(object_type: ObjectType, counts: _Counts, gated: bool) -> TypeScore:
    extracted = counts.hits + counts.false_hits
    labelled = counts.hits + counts.misses
    precision = counts.hits / extracted if extracted else None
    recall = counts.hits / labelled if labelled else None
    verdict = not gated or (
        precision is not None
        and recall is not None
        and precision >= MIN_PRECISION
        and recall >= MIN_RECALL
    )
    return TypeScore(
        object_type,
        counts.hits,
        counts.false_hits,
        counts.misses,
        precision,
        recall,
        gated,
        verdict,
    )


def _scores(
    counts: Mapping[ObjectType, _Counts], landed: frozenset[ObjectType]
) -> dict[ObjectType, TypeScore]:
    return {
        object_type: _type_score(object_type, counts.get(object_type, _Counts()), object_type in landed)
        for object_type in OBJECT_TYPES
        if object_type in counts or object_type in landed
    }


def score(
    files: Sequence[Sequence[Case]],
    extracted: Mapping[str, Sequence[ExactObject]],
    landed_types: Iterable[ObjectType],
) -> SetScore:
    """Score the active cases of ``files`` against each case's extracted objects."""

    landed = frozenset(landed_types)
    totals: dict[ObjectType, _Counts] = {}
    origins: dict[str, dict[ObjectType, _Counts]] = {}
    misses: list[Finding] = []
    false_hits: list[Finding] = []

    def tally(origin: str, object_type: ObjectType) -> tuple[_Counts, _Counts]:
        by_origin = origins.setdefault(origin, {})
        return (
            totals.setdefault(object_type, _Counts()),
            by_origin.setdefault(object_type, _Counts()),
        )

    for case in active_cases(files):
        case_id = case["id"]
        origin = case["labels"][0]
        unmatched = list(extracted.get(case_id, ()))
        for label in map(_label_object, case["expected"]["objects"]):
            hit = next((obj for obj in unmatched if _is_hit(label, obj)), None)
            for counts in tally(origin, label.type):
                if hit is None:
                    counts.misses += 1
                else:
                    counts.hits += 1
            if hit is None:
                misses.append(Finding(case_id, label.type, label.start, label.end))
            else:
                unmatched.remove(hit)
        for obj in unmatched:
            for counts in tally(origin, obj.type):
                counts.false_hits += 1
            false_hits.append(Finding(case_id, obj.type, obj.start, obj.end))

    by_type = _scores(totals, landed)
    return SetScore(
        by_type,
        {origin: _scores(counts, frozenset()) for origin, counts in origins.items()},
        {
            object_type: counts.hits + counts.misses
            for object_type, counts in totals.items()
            if object_type not in landed and counts.hits + counts.misses
        },
        tuple(misses),
        tuple(false_hits),
        all(value.verdict for value in by_type.values()),
    )


def _figure(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def build_report(result: SetScore) -> str:
    """Render the score as plain text: types, counts, case ids, and offsets."""

    lines = ["type hits false_hits misses precision recall verdict"]
    for value in result.by_type.values():
        verdict = ("pass" if value.verdict else "FAIL") if value.gated else "not gated"
        lines.append(
            f"{value.type} {value.hits} {value.false_hits} {value.misses} "
            f"{_figure(value.precision)} {_figure(value.recall)} {verdict}"
        )
    for origin, values in result.by_origin.items():
        lines.append(f"origin {origin}")
        lines.extend(
            f"  {value.type} {value.hits} {value.false_hits} {value.misses} "
            f"{_figure(value.precision)} {_figure(value.recall)}"
            for value in values.values()
        )
    lines.extend(
        f"unlanded {object_type}: {count} labels, reported, not gated"
        for object_type, count in result.unlanded_label_counts.items()
    )
    for heading, findings in (("miss", result.misses), ("false hit", result.false_hits)):
        lines.extend(
            f"{heading} {finding.case_id} {finding.type} {finding.start}:{finding.end}"
            for finding in findings
        )
    lines.append(f"verdict {'pass' if result.verdict else 'FAIL'}")
    return "\n".join(lines) + "\n"
