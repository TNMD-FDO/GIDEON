"""Definitions for sources a corpus cut can acquire."""

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from gideon.host import report
from gideon.host.report import Problem

CASELAW_BASE_URL = "https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/bulk-data/"
CASELAW_LISTING_URL = (
    "https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/"
    "?list-type=2&prefix=bulk-data/opinions-"
)
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


def _listing_problem(detail: str) -> Problem:
    return Problem(
        f"caselaw listing.xml {detail}",
        f"Check the upstream listing, then run {report.command('corpus cut')} again.",
    )


def _caselaw_index_documents() -> tuple[IndexRequest, ...]:
    return (IndexRequest("listing.xml", CASELAW_LISTING_URL),)


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
        *(f"{name}-{newest}.csv.bz2" for name in _DATA_FILES),
        f"schema-{newest}.sql",
        f"load-bulk-data-{newest}.sh",
    )
    return SourceResolution(
        newest, tuple(SourceEntry(path, CASELAW_BASE_URL + path) for path in paths)
    )


SOURCES: tuple[SourceDefinition, ...] = (
    SourceDefinition(
        "caselaw", CASELAW_BASE_URL, True, ("ca6", "scotus"),
        _caselaw_index_documents, _read_caselaw_index,
    ),
)


def source_by_name(
    name: str, registry: Sequence[SourceDefinition] = SOURCES
) -> SourceDefinition | None:
    """Find one registered source by its only name."""

    return next((source for source in registry if source.name == name), None)
