"""Map eyecite's full case citations to exact objects from the source text."""

from typing import Final

from eyecite import get_citations
from eyecite.models import FullCaseCitation

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
