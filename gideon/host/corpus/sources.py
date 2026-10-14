"""Definitions for fetched and captured corpus sources."""

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from gideon.host.report import Problem

CASELAW_BASE_URL = "https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/bulk-data/"
CASELAW_LISTING_URL = (
    "https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/"
    "?list-type=2&prefix=bulk-data/opinions-"
)
TREATMENT_URL = "https://constitution.congress.gov/resources/decisions-overruled/"
TREATMENT_FILE = "decisions-overruled.html"
TREATMENT_JOINED = "2026-10-14"
CAPTURES_DIR = "corpus/captures"
_CAPTURE_NAME = re.compile(r"(.+)-(\d{4}-\d{2}-\d{2})\.html")
MAX_LISTING_BYTES = 4 * 1024 * 1024
_S3_NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/"
_OPINIONS_KEY = re.compile(r"bulk-data/opinions-(\d{4}-\d{2}-\d{2})\.csv\.bz2")
_DATA_FILES = (
    "opinions", "opinion-clusters", "dockets", "citations", "citation-map", "courts",
)


@dataclass(frozen=True, slots=True)
class IndexRequest:
    """An index document to fetch freshly for a source."""

    name: str
    url: str


@dataclass(frozen=True, slots=True)
class SourceEntry:
    """One fetchable object selected by a source's index."""

    path: str
    url: str


@dataclass(frozen=True, slots=True)
class SourceResolution:
    """A dated snapshot selected from freshly fetched index bytes."""

    snapshot_date: str
    entries: tuple[SourceEntry, ...]


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    """A source's identity and pure index interpretation functions."""

    name: str
    base_url: str
    carries_courts: bool
    first_courts: tuple[str, ...]
    index_documents: Callable[[], tuple[IndexRequest, ...]]
    read_index: Callable[[Mapping[str, bytes]], SourceResolution | Problem]
    captured_file: str | None = None
    joined: str | None = None

    @property
    def captured(self) -> bool:
        """Whether the source carries a saved file instead of fetched objects."""
        return self.captured_file is not None


def newest_capture(names: Sequence[str], source_name: str) -> str | None:
    """Select the newest valid dated HTML capture for one source."""
    captures: list[tuple[date, str]] = []
    for name in names:
        match = _CAPTURE_NAME.fullmatch(name)
        if match is None or match.group(1) != source_name:
            continue
        try:
            captured_date = date.fromisoformat(match.group(2))
        except ValueError:
            continue
        captures.append((captured_date, name))
    return max(captures)[1] if captures else None


def _listing_problem(detail: str) -> Problem:
    return Problem(
        f"caselaw listing.xml {detail}",
        "Check the upstream listing, then retry.",
    )


def _caselaw_index_documents() -> tuple[IndexRequest, ...]:
    return (IndexRequest("listing.xml", CASELAW_LISTING_URL),)


def data_file(name: str, snapshot_date: str) -> str:
    """Name one dated compressed CSV file in a source snapshot."""

    return f"{name}-{snapshot_date}.csv.bz2"


def _read_caselaw_index(documents: Mapping[str, bytes]) -> SourceResolution | Problem:
    data = documents.get("listing.xml")
    if data is None:
        return _listing_problem("document is missing")
    if len(data) > MAX_LISTING_BYTES:
        return _listing_problem("document exceeds the size limit")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return _listing_problem("document is malformed XML")
    namespace = f"{{{_S3_NAMESPACE}}}"
    if root.tag != f"{namespace}ListBucketResult":
        return _listing_problem("document is not a ListBucketResult")
    truncated = root.findtext(f"{namespace}IsTruncated")
    if truncated is None or truncated.strip().lower() not in {"true", "false"}:
        return _listing_problem("document has no valid IsTruncated value")
    if truncated.strip().lower() == "true":
        return _listing_problem("document is truncated")
    dates: list[date] = []
    for item in root.findall(f"{namespace}Contents/{namespace}Key"):
        if item.text is None:
            continue
        match = _OPINIONS_KEY.fullmatch(item.text)
        if match is None:
            continue
        try:
            dates.append(date.fromisoformat(match.group(1)))
        except ValueError:
            continue
    if not dates:
        return _listing_problem("document has no dated opinions key")
    newest = max(dates).isoformat()
    paths = (
        *(data_file(name, newest) for name in _DATA_FILES),
        f"schema-{newest}.sql",
        f"load-bulk-data-{newest}.sh",
    )
    return SourceResolution(
        newest, tuple(SourceEntry(path, CASELAW_BASE_URL + path) for path in paths)
    )


def _treatment_index_documents() -> tuple[IndexRequest, ...]:
    return ()


def _read_treatment_index(documents: Mapping[str, bytes]) -> SourceResolution | Problem:
    return Problem(
        "treatment is a captured source and reads no index",
        f"Save the page under {CAPTURES_DIR}/treatment-YYYY-MM-DD.html, then retry.",
    )


SOURCES: tuple[SourceDefinition, ...] = (
    SourceDefinition(
        "caselaw", CASELAW_BASE_URL, True, ("ca6", "scotus"),
        _caselaw_index_documents, _read_caselaw_index,
    ),
    SourceDefinition(
        "treatment", TREATMENT_URL, False, (),
        _treatment_index_documents, _read_treatment_index,
        captured_file=TREATMENT_FILE, joined=TREATMENT_JOINED,
    ),
)


def source_by_name(
    name: str, registry: Sequence[SourceDefinition] = SOURCES
) -> SourceDefinition | None:
    """Find one registered source by its only name."""

    return next((source for source in registry if source.name == name), None)
