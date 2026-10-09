"""Derive reporter-page spans from canonical opinion markers and sections."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from .opiniontext import Attrs, Marker, Parsed
from .sections import Section

ANCHOR_KINDS: Final = (
    "reporter_page", "pdf_page", "bates", "tr_page", "tr_line",
    "uslm_id", "guideline_id",
)


@dataclass(frozen=True, slots=True)
class Anchor:
    """A labelled span of canonical text in one pagination scheme."""

    kind: str
    label: str
    char_start: int
    char_end: int
    attrs: Attrs


@dataclass(frozen=True, slots=True)
class Anchoring:
    """The derived anchors and the page-map cross-check counts."""

    anchors: tuple[Anchor, ...]
    pgmap_checked: int
    pgmap_disagreeing: int


def _scheme(marker: Marker) -> str:
    attrs = dict(marker.attrs)
    for name in ("pagescheme", "citation_index", "stars"):
        if name in attrs:
            return attrs[name]
    return "1"


def _attrs(marker: Marker, parsed: Parsed) -> Attrs:
    attrs = (*marker.attrs, ("scheme", _scheme(marker)))
    containing = [
        block for block in parsed.blocks
        if block.kind == "pagemap" and block.start < marker.offset < block.end
    ]
    if containing:
        nearest = min(containing, key=lambda block: block.end - block.start)
        return (*attrs, ("pgmap", nearest.label))
    return attrs


def _regions(sections: tuple[Section, ...]) -> list[list[tuple[int, int]]]:
    body: list[tuple[int, int]] = []
    regions = [body]
    for section in sections:
        span = (section.char_start, section.char_end)
        if section.section_type == "footnote":
            regions.append([span])
        elif body and body[-1][1] == span[0]:
            body[-1] = (body[-1][0], span[1])
        else:
            body.append(span)
    return regions


def _page_rows(
    parsed: Parsed, spans: list[tuple[int, int]], markers: tuple[Marker, ...],
) -> list[Anchor]:
    rows: list[Anchor] = []
    running: dict[str, Marker] = {}
    for start, end in spans:
        by_scheme: dict[str, list[Marker]] = {}
        for marker in markers:
            if start <= marker.offset < end:
                by_scheme.setdefault(_scheme(marker), []).append(marker)
        for scheme in sorted(running.keys() | by_scheme.keys()):
            turns = by_scheme.get(scheme, [])
            current = running.get(scheme)
            cursor = start if current is not None else turns[0].offset
            covered_from = cursor
            first_row = len(rows)
            for marker in turns:
                if current is not None and cursor < marker.offset:
                    rows.append(Anchor(
                        "reporter_page", current.label, cursor, marker.offset,
                        _attrs(current, parsed),
                    ))
                current = marker
                cursor = marker.offset
            assert current is not None
            if cursor < end:
                rows.append(Anchor(
                    "reporter_page", current.label, cursor, end, _attrs(current, parsed),
                ))
            running[scheme] = current
            tiled = rows[first_row:]
            assert tiled and tiled[0].char_start == covered_from
            assert tiled[-1].char_end == end
            assert all(left.char_end == right.char_start
                       for left, right in zip(tiled, tiled[1:], strict=False))
    return rows


def anchor(parsed: Parsed, sections: tuple[Section, ...]) -> Anchoring:
    """Anchor each footnote separately and carry body pages across footnote gaps."""

    text_length = len(parsed.text)
    assert (not sections and text_length == 0) or (
        bool(sections) and sections[0].char_start == 0
        and sections[-1].char_end == text_length
        and all(left.char_end == right.char_start
                for left, right in zip(sections, sections[1:], strict=False))
    )
    markers = tuple(marker for marker in parsed.markers if marker.kind == "page")
    rows = [row for spans in _regions(sections) for row in _page_rows(parsed, spans, markers)]
    rows.sort(key=lambda row: (row.char_start, dict(row.attrs)["scheme"]))
    assert all(0 <= row.char_start < row.char_end <= text_length for row in rows)
    assert all(left.char_start <= right.char_start
               for left, right in zip(rows, rows[1:], strict=False))
    by_scheme: dict[str, list[Anchor]] = {}
    for row in rows:
        by_scheme.setdefault(dict(row.attrs)["scheme"], []).append(row)
    assert all(
        left.char_end <= right.char_start
        for scheme_rows in by_scheme.values()
        for left, right in zip(scheme_rows, scheme_rows[1:], strict=False)
    )

    pagemaps = [block for block in parsed.blocks if block.kind == "pagemap"]
    disagreeing = sum(
        len(block.label.split()) - 1 != sum(
            block.start < marker.offset < block.end and _scheme(marker) == "1"
            for marker in markers
        )
        for block in pagemaps
    )
    return Anchoring(tuple(rows), len(pagemaps), disagreeing)


def intersecting(anchors: Sequence[Anchor], start: int, end: int) -> tuple[Anchor, ...]:
    """Return every anchor that meets the half-open text span."""

    return tuple(row for row in anchors if row.char_start < end and start < row.char_end)
