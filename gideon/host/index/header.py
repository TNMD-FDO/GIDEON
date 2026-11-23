"""Deterministic context headers and embedding-space identities for the host and worker."""

import hashlib
from collections.abc import Sequence
from datetime import date
from types import MappingProxyType
from typing import Final

HEADER_ID_PATTERN: Final[str] = r"h[0-9]+"
RELEASE_HEADER: Final[str | None] = None
# E-10 starting value to bound the input added by long case names.
CASE_NAME_MAX_LENGTH: Final[int] = 120


def _h1(
    case_name: str | None,
    reporter_cites: Sequence[str],
    docket: str | None,
    court_id: str,
    decided_date: date,
    section_type: str,
) -> str:
    name = " ".join((case_name or "").split())[:CASE_NAME_MAX_LENGTH].rstrip()
    cite = " ".join(next(iter(reporter_cites), "").split())
    docket_number = " ".join((docket or "").split())
    if not cite and docket_number:
        cite = f"No. {docket_number}"

    caption = ", ".join(part for part in (name, cite) if part)
    prefix = f"{caption} " if caption else ""
    court = " ".join(court_id.split())
    section = " ".join(section_type.replace("_", " ").split())
    return f"{prefix}({court} {decided_date.year}) — {section}"


HEADERS: Final = MappingProxyType({"h1": _h1})


def _require_registered(header_id: str) -> None:
    if header_id not in HEADERS:
        raise ValueError("unregistered header id")


def embedding_space(space_id: str, header_id: str | None) -> str:
    """Name the generation's space from the profile's space and optional header."""

    if header_id is None:
        return space_id
    _require_registered(header_id)
    return f"{space_id}-{header_id}"


def header_for_space(space_id: str) -> str | None:
    """Return the registered header id at the end of the space, if present."""

    _, separator, header_id = space_id.rpartition("-")
    return header_id if separator and header_id in HEADERS else None


def compose(
    header_id: str | None,
    case_name: str | None,
    reporter_cites: Sequence[str],
    docket: str | None,
    court_id: str,
    decided_date: date,
    section_type: str,
) -> str:
    """Compose the chosen header line from recorded opinion and chunk values."""

    if header_id is None:
        return ""
    _require_registered(header_id)
    return HEADERS[header_id](case_name, reporter_cites, docket, court_id, decided_date, section_type)


def header_hash(text: str) -> str:
    """Hash the header line as UTF-8."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


BARE_HEADER_HASH: Final[str] = header_hash("")


def embedding_input(header_text: str, text_slice: str) -> str:
    """Prepend a nonempty header and a blank line to the canonical slice."""

    return f"{header_text}\n\n{text_slice}" if header_text else text_slice
