"""Map eyecite's citations to exact objects and resolved findings."""

import warnings
from typing import Final

from eyecite import get_citations, resolve_citations
from eyecite.models import (
    FullCaseCitation,
    FullJournalCitation,
    FullLawCitation,
    IdCitation,
    ReferenceCitation,
    ShortCaseCitation,
    SupraCitation,
)

from gideon.casecite.found import CiteForm, CiteKind, FoundCitation
from gideon.extraction.contract import ExactObject, combine_extractions

PATTERN_ID: Final = "eyecite/full-case@1"


def extract_case_cites(text: str) -> tuple[ExactObject, ...]:
    """Keep full case citations at their verbatim spans, disjoint by start."""

    cites: list[ExactObject] = []
    for citation in get_citations(text):
        if not isinstance(citation, FullCaseCitation):
            continue
        start, end = citation.span()
        cites.append(
            ExactObject(
                "case_cite", start, end, text[start:end], pattern_id=PATTERN_ID
            )
        )
    return combine_extractions((), cites)


_FORMS: Final[tuple[tuple[type, CiteForm], ...]] = (
    (FullCaseCitation, "full"),
    (FullLawCitation, "full"),
    (FullJournalCitation, "full"),
    (ShortCaseCitation, "short"),
    (IdCitation, "id"),
    (SupraCitation, "supra"),
    (ReferenceCitation, "reference"),
)


def _form(citation: object) -> CiteForm:
    return next(form for kind, form in _FORMS if isinstance(citation, kind))


def find_citations(text: str) -> tuple[FoundCitation, ...]:
    """Find citation spans and link each resolved form to its full citation."""

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # The library answers its own name with a canned cite whose span runs
        # past the text, and lists a bare section sign after later cites;
        # resolution reads the list as the text's order, so it is put back.
        citations = sorted(
            (
                citation for citation in get_citations(text)
                if 0 <= citation.span()[0] < citation.span()[1] <= len(text)
            ),
            key=lambda citation: citation.span()[0],
        )
        resolutions = resolve_citations(citations)

    # An unknown citation (a bare section sign) is no finding.
    kept = [
        citation for citation in citations
        if isinstance(citation, tuple(kind for kind, _ in _FORMS))
    ]
    # By identity: two equal full cites are one resource but two findings.
    index_by_identity = {id(citation): index for index, citation in enumerate(kept)}
    resource_by_identity: dict[int, int] = {}
    for members in resolutions.values():
        # A resource's list opens with the full cite that defined it.
        full_index = index_by_identity[id(members[0])]
        for citation in members:
            resource_by_identity[id(citation)] = full_index

    found: list[FoundCitation] = []
    for index, citation in enumerate(kept):
        form = _form(citation)
        resource = index if form == "full" else resource_by_identity.get(id(citation))
        full = kept[resource] if resource is not None else None
        kind: CiteKind
        if isinstance(full, FullCaseCitation) or isinstance(citation, ShortCaseCitation):
            kind = "case"
        elif isinstance(full, FullLawCitation):
            kind = "law"
        elif isinstance(full, FullJournalCitation):
            kind = "journal"
        else:
            kind = "unknown"

        volume = reporter = page = reporter_cite = None
        if isinstance(full, FullCaseCitation):
            # A full cite lacking any of the three, such as the library's reply
            # to its own name, states no reporter cite and keys nothing.
            if all(full.groups.get(field) for field in ("volume", "reporter", "page")):
                volume = str(full.groups["volume"])
                reporter = str(full.corrected_reporter())
                page = str(full.corrected_page())
                reporter_cite = str(full.corrected_citation())
        elif isinstance(citation, ShortCaseCitation):
            # A short form's page is a pin page, so it never reaches the map.
            reporter_cite = str(citation.corrected_citation())

        start, end = citation.span()
        found.append(
            FoundCitation(
                start, end, form, kind, resource, volume, reporter, page,
                reporter_cite, citation.metadata.pin_cite,
            )
        )
    return tuple(found)
