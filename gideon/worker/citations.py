"""Build immutable citation edges from grammar objects and adapter findings."""

import hashlib
import unicodedata
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from gideon.casecite.found import CITE_FORMS, EDGE_PATTERN_ID, CiteForm, FoundCitation
from gideon.extraction.contract import OBJECT_TYPES, ExactObject

CITE_TYPES: Final[tuple[str, ...]] = tuple(
    kind for kind in OBJECT_TYPES if kind not in {"docket", "caption", "case_cite"}
) + ("case_cite", "law_cite", "journal_cite", "unknown")

type Resolver = Callable[[tuple[str, str, str]], int | None]


class SectionSpan(Protocol):
    """The section fields needed to place a citation."""

    @property
    def section_id(self) -> str: ...

    @property
    def section_type(self) -> str: ...

    @property
    def char_start(self) -> int: ...

    @property
    def char_end(self) -> int: ...


@dataclass(frozen=True, slots=True)
class NewCitation:
    """One citation row to insert with a document's completed pass."""

    citation_id: str
    doc_id: str
    ordinal: int
    char_start: int
    char_end: int
    section_id: str
    section_type: str
    cite_type: str
    cite_form: CiteForm
    raw_cite: str
    reporter_cite: str | None
    pincite: str | None
    key: str | None
    to_cluster: int | None
    pattern_id: str


def _designator_gap(gap: str) -> bool:
    stripped = "".join(
        char for char in gap.casefold()
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )
    return stripped in {"", "at"}


def edges(
    doc_id: str,
    text: str,
    sections: Sequence[SectionSpan],
    found: Sequence[FoundCitation],
    objects: Sequence[ExactObject],
    resolve: Resolver,
) -> tuple[NewCitation, ...]:
    """Merge disjoint spans, place them in sections, and resolve case cites."""

    if not sections:
        raise ValueError("citation edges require document sections")

    selected: list[ExactObject | FoundCitation] = [
        obj for obj in objects
        if obj.type in CITE_TYPES and obj.type != "case_cite"
    ]
    for citation in sorted(found, key=lambda item: (item.start, -(item.end - item.start))):
        if any(citation.start < kept.end and kept.start < citation.end for kept in selected):
            continue
        selected.append(citation)
    selected.sort(key=lambda item: item.start)

    starts = tuple(section.char_start for section in sections)
    barrier: ExactObject | None = None
    rows: list[NewCitation] = []
    for ordinal, candidate in enumerate(selected):
        section_index = bisect_right(starts, candidate.start) - 1
        if section_index < 0 or candidate.start >= sections[section_index].char_end:
            raise ValueError("citation starts outside document sections")
        section = sections[section_index]

        if isinstance(candidate, ExactObject):
            cite_type: str = candidate.type
            cite_form: CiteForm = "full"
            key = candidate.key
            reporter_cite = pincite = None
            to_cluster = None
            pattern_id = candidate.pattern_id
            barrier = candidate
        else:
            if candidate.form not in CITE_FORMS:
                raise ValueError("unknown citation form")
            cite_type = f"{candidate.kind}_cite" if candidate.kind != "unknown" else "unknown"
            cite_form = candidate.form
            key = None
            reporter_cite = candidate.reporter_cite
            pincite = candidate.pincite
            pattern_id = EDGE_PATTERN_ID
            to_cluster = None
            if candidate.form == "id" and barrier is not None:
                cite_type = barrier.type
                reporter_cite = None
                next_candidate = selected[ordinal + 1] if ordinal + 1 < len(selected) else None
                designator = (
                    isinstance(next_candidate, ExactObject)
                    and _designator_gap(text[candidate.end:next_candidate.start])
                )
                if not pincite and not designator:
                    key = barrier.key
            else:
                barrier = None
                if (
                    cite_type == "case_cite"
                    and candidate.resource is not None
                    and candidate.volume is not None
                    and candidate.reporter is not None
                    and candidate.page is not None
                ):
                    to_cluster = resolve((
                        candidate.volume, candidate.reporter, candidate.page,
                    ))

        if pattern_id is None:
            raise ValueError("citation pattern is missing")
        citation_id = hashlib.sha256(
            f"{doc_id}\n{candidate.start}\n{candidate.end}".encode()
        ).hexdigest()
        rows.append(NewCitation(
            citation_id, doc_id, ordinal, candidate.start, candidate.end,
            section.section_id, section.section_type, cite_type, cite_form,
            text[candidate.start:candidate.end], reporter_cite, pincite,
            key, to_cluster, pattern_id,
        ))
    return tuple(rows)
