"""Pack canonical section spans into deterministic, citation-safe chunks."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from gideon.worker.pipeline import Pipeline
from gideon.worker.sections import paragraphs

if TYPE_CHECKING:
    from gideon.worker.citations import SectionSpan


SENTENCE_TERMINALS: Final = ".?!"
SENTENCE_CLOSERS: Final = "\"'”’)]}"
SENTENCE_OPENERS: Final = "\"'“‘([{"
SENTENCE_PATTERN: Final = re.compile(
    rf"[{re.escape(SENTENCE_TERMINALS)}][{re.escape(SENTENCE_CLOSERS)}]*"
    rf"\s+(?=[A-Z0-9{re.escape(SENTENCE_OPENERS)}])"
)
_WHITESPACE: Final = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class NewChunk:
    """One chunk row, identified by offsets into canonical text."""

    chunk_id: str
    doc_id: str
    pipeline: str
    ordinal: int
    section_id: str
    section_type: str
    char_start: int
    char_end: int
    content_hash: str
    token_count: int


def _legal(boundary: int, citation_spans: Sequence[tuple[int, int]]) -> bool:
    return all(not (start < boundary < end) for start, end in citation_spans)


def _greedy_split(
    text: str, start: int, end: int, maximum: int,
    citation_spans: Sequence[tuple[int, int]], count: Callable[[str], int],
) -> list[tuple[int, int]]:
    """Split a sentence over the maximum at whitespace, else at code points."""

    pieces: list[tuple[int, int]] = []
    while count(text[start:end]) > maximum:
        boundary = None
        whitespace = (match.end() for match in _WHITESPACE.finditer(text, start, end))
        for candidates in (whitespace, range(start + 1, end)):
            legal = [candidate for candidate in candidates
                     if start < candidate < end and _legal(candidate, citation_spans)]
            # The longest prefix within the maximum; a prefix's count grows with it.
            low, high = 0, len(legal)
            while low < high:
                middle = (low + high) // 2
                if count(text[start:legal[middle]]) <= maximum:
                    low = middle + 1
                else:
                    high = middle
            if low:
                boundary = legal[low - 1]
                break
        if boundary is None:
            raise ValueError("a citation span exceeds the pipeline's chunk maximum")
        pieces.append((start, boundary))
        start = boundary
    pieces.append((start, end))
    return pieces


def _split_overlong(
    text: str, start: int, end: int, maximum: int,
    citation_spans: Sequence[tuple[int, int]], count: Callable[[str], int],
) -> list[tuple[int, int]]:
    """Split a unit over the maximum into its sentences, each then held to it."""

    if count(text[start:end]) <= maximum:
        return [(start, end)]
    boundaries = [match.end() for match in SENTENCE_PATTERN.finditer(text, start, end)
                  if start < match.end() < end and _legal(match.end(), citation_spans)]
    edges = [start, *boundaries, end]
    pieces: list[tuple[int, int]] = []
    for piece_start, piece_end in zip(edges, edges[1:], strict=False):
        pieces.extend(_greedy_split(text, piece_start, piece_end, maximum, citation_spans, count))
    return pieces


def _units(
    text: str, section: SectionSpan, citation_spans: Sequence[tuple[int, int]],
    maximum: int, count: Callable[[str], int],
) -> list[tuple[int, int]]:
    paragraph_rows = paragraphs(text, section.char_start, section.char_end)
    if not paragraph_rows:
        return []
    starts = [section.char_start]
    starts.extend(paragraph.start for paragraph in paragraph_rows[1:]
                  if _legal(paragraph.start, citation_spans))
    ends = starts[1:] + [section.char_end]
    units: list[tuple[int, int]] = []
    for start, end in zip(starts, ends, strict=True):
        units.extend(_split_overlong(text, start, end, maximum, citation_spans, count))
    return units


def _groups(
    text: str, units: Sequence[tuple[int, int]], pipeline: Pipeline,
    count: Callable[[str], int],
) -> list[tuple[int, int]]:
    unit_counts = [count(text[start:end]) for start, end in units]
    groups: list[tuple[int, int]] = []
    position = 0
    while position < len(units):
        start = position
        estimate = unit_counts[position]
        position += 1
        while position < len(units):
            next_estimate = estimate + unit_counts[position]
            if next_estimate > pipeline.chunk_target and not (
                estimate < pipeline.chunk_minimum and next_estimate <= pipeline.chunk_maximum
            ):
                break
            estimate = next_estimate
            position += 1
        groups.append((start, position))

    if len(groups) > 1:
        last_start, last_end = groups[-1]
        if count(text[units[last_start][0]:units[last_end - 1][1]]) < pipeline.chunk_minimum:
            previous_start, _previous_end = groups[-2]
            if count(text[units[previous_start][0]:units[last_end - 1][1]]) <= pipeline.chunk_maximum:
                groups[-2] = (previous_start, last_end)
                groups.pop()

    index = 0
    while index < len(groups):
        start, end = groups[index]
        while count(text[units[start][0]:units[end - 1][1]]) > pipeline.chunk_maximum:
            if end - start == 1:
                raise ValueError("a chunk unit exceeds the pipeline's chunk maximum")
            old_end = end
            end -= 1
            groups[index] = (start, end)
            if index + 1 == len(groups):
                groups.append((end, old_end))
            else:
                _next_start, next_end = groups[index + 1]
                groups[index + 1] = (end, next_end)
        index += 1
    return groups


def chunk(
    doc_id: str, pipeline: Pipeline, text: str, sections: Sequence[SectionSpan],
    citation_spans: Sequence[tuple[int, int]], count: Callable[[str], int],
) -> tuple[NewChunk, ...]:
    """Return ordered chunks whose spans tile each nonblank section."""

    rows: list[NewChunk] = []
    for section in sections:
        units = _units(text, section, citation_spans, pipeline.chunk_maximum, count)
        if not units:
            continue
        for first, last in _groups(text, units, pipeline, count):
            start, end = units[first][0], units[last - 1][1]
            slice_text = text[start:end]
            chunk_id = hashlib.sha256(
                f"{doc_id}\n{pipeline.version}\n{start}\n{end}".encode()
            ).hexdigest()
            rows.append(NewChunk(
                chunk_id, doc_id, pipeline.version, len(rows), section.section_id,
                section.section_type, start, end,
                hashlib.sha256(slice_text.encode("utf-8")).hexdigest(), count(slice_text),
            ))
    return tuple(rows)
