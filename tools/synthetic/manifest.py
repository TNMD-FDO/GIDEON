"""Synthetic draft formats, file-prefix holds, and public opinion links."""

import hashlib
import re
import urllib.parse
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from gideon.evaluation import synthetic

MANIFEST_FORMAT: Final[int] = 3
MANIFEST_KEYS: Final[tuple[str, ...]] = (
    "format", "set_root", "set_digest", "category", "case_pin", "template",
    "template_digest", "reading", "reading_digest", "generation", "seed", "asked",
    "read_share", "read_ordinals", "entries", "discards", "sheet_sha256", "sheet_bytes",
)
ENTRY_KEYS: Final[tuple[str, ...]] = (
    "ordinal", "source_id", "sha256", "doc_id", "start", "end", "length",
    "quote_sha256", "closed_book_score", "label", "draft_sha256",
)
DRAFT_KEYS: Final[tuple[str, ...]] = tuple(
    key for key in synthetic.CASE_KEYS if key not in {"id", "review"}
) + ("closed_book_answer", "ordinal")
DISCARD_KEYS: Final[tuple[str, ...]] = ("reason", "source_id", "edge", "kind")
# A manifest's discard reasons: the retrace's, a failed engine request's, and
# the draft's skips before any request.
DISCARD_REASONS: Final[tuple[str, ...]] = synthetic.DISCARD_REASONS + (
    "request-failed", "http-status", "truncated", "off-schema", "closed-book-failed",
    "hash-changed", "not-ready", "no-case-name", "already-cased", "document-short",
    "unpublished", "cited-absent", "state-outranked", "no-state-kind",
    "same-document", "same-cluster", "no-kind", "premise-in-record",
)
SHEET_BLOCK_HEADER: Final[str] = "Synthetic case"
SHEET_REQUIRED: Final[str] = "required"
SHEET_OPTIONAL: Final[str] = "optional"
# exempt: a starting display length for the sheet digest in a marks header.
DIGEST_PREFIX_CHARS: Final[int] = 12
MARKS_HEADER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"synthetic: (?P<role>[A-Za-z0-9-]+) (?P<digest>[0-9a-f]+) (?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})"
)
_REPORTER_CITE: Final[re.Pattern[str]] = re.compile(r"([0-9]+) (.+) ([0-9]+)")
ORIGINAL_BASE: Final[str] = "https://www.courtlistener.com/c/"


def marks_template(sheet_digest: str, read_ordinals: Sequence[int]) -> str:
    """Render the CSA header to complete and the required ordinal lines."""

    return (
        f"synthetic: <CSA role id> {sheet_digest[:DIGEST_PREFIX_CHARS]} <ISO date>\n"
        + "".join(f"{ordinal:02d} \n" for ordinal in read_ordinals)
    )


def pin_file(path: Path) -> tuple[int, str] | None:
    """Record one file's line count and byte-prefix digest, or its absence."""

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    return len(raw.splitlines()), hashlib.sha256(raw).hexdigest()


def hold_file(path: Path, pin: tuple[int, str] | None) -> bool:
    """Hold a recorded prefix while allowing complete-line appends."""

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return pin is None
    if raw and not raw.endswith(b"\n"):
        return False
    if pin is None:
        return True
    count, digest = pin
    lines = raw.splitlines(keepends=True)
    return len(lines) >= count and hashlib.sha256(b"".join(lines[:count])).hexdigest() == digest


def original_link(cites: Sequence[str]) -> str:
    """The public copy of the opinion, found by its first printed reporter cite."""

    for cite in cites:
        found = _REPORTER_CITE.fullmatch(cite)
        if found is not None and "LEXIS" not in found.group(2):
            volume, reporter, page = found.groups()
            return f"{ORIGINAL_BASE}{urllib.parse.quote(reporter)}/{volume}/{page}/"
    return "no printed reporter cite recorded; search the opinion id"
