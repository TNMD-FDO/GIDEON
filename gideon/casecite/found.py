"""Pure citation findings shared by the image adapter and worker."""

from dataclasses import dataclass
from typing import Final, Literal

CiteForm = Literal["full", "short", "id", "supra", "reference"]
CiteKind = Literal["case", "law", "journal", "unknown"]
CITE_FORMS: Final[tuple[CiteForm, ...]] = ("full", "short", "id", "supra", "reference")
CITE_KINDS: Final[tuple[CiteKind, ...]] = ("case", "law", "journal", "unknown")
EDGE_PATTERN_ID: Final = "eyecite/edges@1"


@dataclass(frozen=True, slots=True)
class FoundCitation:
    """One eyecite span and the full citation, if any, that resolves it."""

    start: int
    end: int
    form: CiteForm
    kind: CiteKind
    resource: int | None
    volume: str | None
    reporter: str | None
    page: str | None
    reporter_cite: str | None
    pincite: str | None
