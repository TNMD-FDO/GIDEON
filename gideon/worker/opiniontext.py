"""Choose an opinion's source text and extract canonical text with locators."""

import re
import unicodedata
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from lxml import etree, html

TEXT_SOURCES: Final = (
    "xml_harvard", "html_columbia", "html_lawbox", "html_anon_2020",
    "html", "plain_text",
)

MarkerKind = Literal["page", "footnote-mark", "editorial"]
BlockKind = Literal[
    "opinion", "author", "headmatter", "footnote", "heading", "quote", "pagemap",
]
FailureReason = Literal["empty", "unparseable"]
Attrs = tuple[tuple[str, str], ...]

_XML_BLOCKS: Final = frozenset({
    "author", "p", "blockquote", "footnote", "pre", "judges", "attorneys",
    "headnotes", "summary", "history", "decisiondate", "otherdate",
    "docketnumber", "seealso", "citation", "court",
})
_HTML_BLOCKS: Final = frozenset({
    "p", "div", "blockquote", "center", "h1", "h2", "h3", "h4", "h5",
    "h6", "h", "li", "tr", "pre", "bodytext", "excerpt", "footnote_body",
})
_DROP_SUBTREES: Final = frozenset({"script", "style", "nav", "img"})
_WORD: Final = re.compile(r"\S+")
_BRACKETED_LABEL: Final = re.compile(r"\[[^\[\]\s]+\]")


@dataclass(frozen=True, slots=True)
class Marker:
    """A dropped locator with its printed label, text offset, and source attributes."""

    kind: MarkerKind
    label: str
    offset: int
    attrs: Attrs = ()


@dataclass(frozen=True, slots=True)
class Block:
    """A text-bearing element's kind, label, and span in canonical text."""

    kind: BlockKind
    label: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Parsed:
    """The canonical text with its dropped locators and structural blocks."""

    text: str
    markers: tuple[Marker, ...]
    blocks: tuple[Block, ...]


class OpinionTextFailure(Exception):
    """A markup read failed with a reason suitable for a document row."""

    def __init__(self, reason: FailureReason) -> None:
        self.reason = reason
        super().__init__(reason)


def choose(row: Mapping[str, str | None]) -> tuple[str, str] | None:
    """Return the first source column containing nonblank text."""

    for column in TEXT_SOURCES:
        value = row.get(column)
        if value is not None and value.strip():
            return column, value
    return None


def _normalized_segment(
    raw: str, markers: list[tuple[int, MarkerKind, str, Attrs]],
    preserve: bool,
    positions: list[int],
) -> tuple[str, list[Marker], list[int]]:
    """One line's text and normalized offsets for locators and block boundaries.

    Each word is normalized alone: a space never composes, so the joined words
    equal the line's normalization, and a marker's offset counts the normalized
    prefix rather than the raw one.
    """

    if preserve:
        start = len(raw) - len(raw.lstrip("\r\n"))
        end = len(raw.rstrip("\r\n"))
        text = unicodedata.normalize("NFC", raw[start:end])
        def locate_preserved(pos: int) -> int:
            return len(unicodedata.normalize("NFC", raw[start:max(start, min(pos, end))]))

        return (text, [Marker(kind, label, locate_preserved(pos), attrs)
                       for pos, kind, label, attrs in markers],
                [locate_preserved(pos) for pos in positions])

    words = list(_WORD.finditer(raw))
    starts = [word.start() for word in words]
    pieces: list[str] = []
    offsets: list[int] = []
    length = 0
    for word in words:
        if pieces:
            length += 1
        offsets.append(length)
        part = unicodedata.normalize("NFC", word.group())
        pieces.append(part)
        length += len(part)

    def locate_collapsed(pos: int) -> int:
        index = bisect_right(starts, pos) - 1
        if index < 0:
            return 0
        if pos <= words[index].end():
            prefix = raw[words[index].start():pos]
            return offsets[index] + len(unicodedata.normalize("NFC", prefix))
        if index + 1 < len(words):
            return offsets[index + 1]
        return length

    return (" ".join(pieces),
            [Marker(kind, label, locate_collapsed(pos), attrs)
             for pos, kind, label, attrs in markers],
            [locate_collapsed(pos) for pos in positions])


@dataclass(slots=True)
class _OpenBlock:
    kind: BlockKind
    label: str
    raw_start: int | None
    raw_end: int | None = None
    start: int | None = None
    end: int | None = None


class _Builder:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.markers: list[Marker] = []
        self.pending_markers: list[tuple[MarkerKind, str, Attrs]] = []
        self.raw: list[str] = []
        self.raw_length = 0
        self.raw_markers: list[tuple[int, MarkerKind, str, Attrs]] = []
        self.length = 0
        self.breaks = 0
        self.preserve = False
        self.blocks: list[_OpenBlock] = []
        self.pending_blocks: list[_OpenBlock] = []

    def open_block(self, kind: BlockKind, label: str) -> _OpenBlock:
        block = _OpenBlock(kind, label, self.raw_length)
        self.blocks.append(block)
        self.pending_blocks.append(block)
        return block

    def close_block(self, block: _OpenBlock) -> None:
        block.raw_end = self.raw_length

    def text(self, value: str | None) -> None:
        if value:
            self.raw.append(value)
            self.raw_length += len(value)

    def marker(
        self, kind: MarkerKind, label: str, attrs: Attrs,
    ) -> None:
        self.raw_markers.append((self.raw_length, kind, label, attrs))

    def flush(self) -> None:
        raw = "".join(self.raw)
        pending = self.pending_blocks
        positions: list[int] = []
        for block in pending:
            assert block.raw_start is not None
            positions.extend((block.raw_start, block.raw_end if block.raw_end is not None else len(raw)))
        value, markers, offsets = _normalized_segment(
            raw, self.raw_markers, self.preserve, positions,
        )
        self.raw = []
        self.raw_length = 0
        self.raw_markers = []
        separator = "\n" * self.breaks if self.parts and value else ""
        base = self.length + len(separator)
        for index, block in enumerate(pending):
            assert block.raw_start is not None
            raw_end = block.raw_end if block.raw_end is not None else len(raw)
            has_text = bool(raw[block.raw_start:raw_end].strip())
            if has_text and value and block.start is None:
                block.start = base + offsets[2 * index]
            if block.raw_end is not None:
                block.end = base + offsets[2 * index + 1] if has_text and value else self.length
                block.raw_start = None
            else:
                block.raw_start = 0
        self.pending_blocks = [block for block in pending if block.raw_start is not None]
        if not value:
            self.pending_markers.extend(
                (marker.kind, marker.label, marker.attrs) for marker in markers
            )
            return
        self.parts.append(separator + value)
        self.markers.extend(
            Marker(kind, label, base, attrs) for kind, label, attrs in self.pending_markers
        )
        self.markers.extend(
            Marker(marker.kind, marker.label, base + marker.offset, marker.attrs)
            for marker in markers
        )
        self.pending_markers = []
        self.length = base + len(value)
        self.breaks = 0

    def block_break(self) -> None:
        self.flush()
        self.breaks = max(self.breaks, 2)

    def line_break(self) -> None:
        self.flush()
        self.breaks = min(self.breaks + 1, 2)

    def finish(self) -> Parsed:
        self.flush()
        self.markers.extend(
            Marker(kind, label, self.length, attrs)
            for kind, label, attrs in self.pending_markers
        )
        value = "".join(self.parts)
        if not value.strip():
            raise OpinionTextFailure("empty")
        blocks = tuple(
            Block(block.kind, block.label, block.start, block.end)
            for block in self.blocks
            if block.start is not None and block.end is not None and block.end > block.start
        )
        return Parsed(value, tuple(self.markers), blocks)


def _tag(element: etree._Element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower()


def _marker(
    element: etree._Element, column: str,
) -> tuple[MarkerKind, str, Attrs] | None:
    tag = _tag(element)
    classes = set((element.get("class") or "").split())
    if (
        tag == "page-number"
        or tag == "span" and ("star-pagination" in classes or "ldml-pagenumber" in classes)
        or tag == "a" and "page-label" in classes
    ):
        raw_label = "".join(element.itertext()).strip()
        stars = len(raw_label) - len(raw_label.lstrip("*"))
        printed = raw_label.lstrip("*").strip()
        if printed.lower().startswith("page "):
            printed = printed[5:].strip()
        label = printed or element.get("label") or element.get("data-label") or ""
        attrs: list[tuple[str, str]] = []
        # The CAP HTML form, data-citation-index, also appears inside xml_harvard.
        citation_index = element.get("citation-index") or element.get("data-citation-index")
        if citation_index is not None:
            attrs.append(("citation_index", citation_index))
        if column == "html_anon_2020":
            for name in ("number", "pagescheme"):
                value = element.get(name)
                if value is not None:
                    attrs.append((name, value))
        if stars > 1:
            attrs.append(("stars", str(stars)))
        return "page", label, tuple(attrs)
    if (
        tag in {"footnotemark", "footnotereference"}
        or tag == "a" and (element.get("href") or "").startswith("#")
        or tag == "sup" and _BRACKETED_LABEL.fullmatch("".join(element.itertext()).strip())
        or tag == "span" and "MsoFootnoteReference" in classes
    ):
        return "footnote-mark", "".join(element.itertext()).strip(), ()
    if tag == "bracketnum" or column == "html" and tag == "span" and "num" in classes:
        return "editorial", "".join(element.itertext()).strip(), ()
    return None


def _is_block(element: etree._Element, column: str) -> bool:
    tag = _tag(element)
    if column != "xml_harvard":
        return tag in _HTML_BLOCKS
    classes = set((element.get("class") or "").split())
    return tag in _XML_BLOCKS or tag == "div" and bool(classes & {"footnote", "footnotes"})


def _block_kind(
    element: etree._Element, column: str, header: etree._Element | None,
) -> tuple[BlockKind, str] | None:
    tag = _tag(element)
    classes = set((element.get("class") or "").split())
    if column == "xml_harvard":
        if tag == "opinion":
            return "opinion", (element.get("type") or "").lower()
        if tag == "author":
            return "author", ""
        if tag in {
            "judges", "attorneys", "headnotes", "summary", "history", "decisiondate",
            "otherdate", "docketnumber", "seealso", "citation", "court",
        }:
            return "headmatter", tag
        if tag == "footnote" or tag == "div" and "footnote" in classes:
            return "footnote", element.get("label") or ""
        if tag == "blockquote":
            return "quote", ""
    elif column == "html_columbia":
        if tag in {"h3", "center"}:
            return "heading", ""
        if tag == "blockquote":
            return "quote", ""
        if tag == "footnote_body" or tag == "div" and "footnote" in classes:
            return "footnote", ""
    elif column == "html_lawbox":
        if element is header:
            return "headmatter", "header"
        if tag in {"h1", "h2", "center"}:
            return "heading", ""
        if tag == "blockquote":
            return "quote", ""
    elif column == "html_anon_2020":
        if tag == "div":
            if "opinion" in classes:
                return "opinion", element.get("opiniontype") or ""
            if "caseopinionby" in classes:
                return "author", ""
            if classes & {"courtcasedochead", "judges", "counsel", "representation", "panel"}:
                return "headmatter", ""
        if tag == "counselor":
            return "headmatter", ""
        if tag == "h" or tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            return "heading", ""
        if tag == "excerpt":
            return "quote", ""
        if tag == "li" and any(
            _tag(parent) == "div" and "footnotes" in (parent.get("class") or "").split()
            for parent in element.iterancestors()
        ):
            return "footnote", ""
    elif column == "html":
        if tag == "p" and classes & {"case_cite", "date", "parties", "docket", "court"}:
            return "headmatter", ""
        if tag == "div" and "prelims" in classes:
            return "headmatter", ""
        if tag == "div" and "footnote" in classes or tag == "p" and "MsoFootnoteText" in classes:
            return "footnote", ""
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "center"}:
            return "heading", ""
        if tag == "blockquote":
            return "quote", ""
    return None


def _lawbox_header(root: etree._Element) -> etree._Element | None:
    first_h1 = next((element for element in root.iter()
                     if isinstance(element.tag, str) and _tag(element) == "h1"), None)
    if first_h1 is None:
        return None
    for parent in first_h1.iterancestors():
        if parent is root:
            break
        if _tag(parent) == "div":
            return parent
    return None


def _walk(
    element: etree._Element, column: str, builder: _Builder,
    header: etree._Element | None,
) -> None:
    if not isinstance(element.tag, str):
        return
    marker = _marker(element, column)
    if marker is not None:
        builder.marker(*marker)
        return
    tag = _tag(element)
    if tag in _DROP_SUBTREES:
        return
    if tag == "br":
        builder.line_break()
        return
    block = _is_block(element, column)
    if block:
        builder.block_break()
    kind = _block_kind(element, column, header)
    opened = builder.open_block(*kind) if kind is not None else None
    pagemap = element.get("pgmap") if column == "xml_harvard" else None
    opened_pagemap = builder.open_block("pagemap", pagemap) if pagemap is not None else None
    previous_preserve = builder.preserve
    if tag == "pre":
        builder.preserve = True
    builder.text(element.text)
    for child in element:
        _walk(child, column, builder, header)
        builder.text(child.tail)
    if opened is not None:
        builder.close_block(opened)
    if opened_pagemap is not None:
        builder.close_block(opened_pagemap)
    if block:
        builder.block_break()
    builder.preserve = previous_preserve


def canonical_text(column: str, raw: str) -> Parsed:
    """Parse one source column without adding text for dropped locators."""

    if column not in TEXT_SOURCES:
        raise ValueError("unknown opinion text source")
    if not raw.strip():
        raise OpinionTextFailure("empty")
    if column == "plain_text":
        value = unicodedata.normalize(
            "NFC", raw.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n"),
        )
        if not value.strip():
            raise OpinionTextFailure("empty")
        return Parsed(value, (), ())

    try:
        if column == "xml_harvard":
            parser = etree.XMLParser(
                recover=True, resolve_entities=False, no_network=True, huge_tree=True,
            )
            root = etree.fromstring(raw.encode("utf-8"), parser=parser)
        else:
            root = html.fromstring(raw, parser=html.HTMLParser(no_network=True))
    except (etree.LxmlError, ValueError) as exc:
        raise OpinionTextFailure("unparseable") from exc
    if root is None:
        raise OpinionTextFailure("unparseable")
    builder = _Builder()
    header = _lawbox_header(root) if column == "html_lawbox" else None
    _walk(root, column, builder, header)
    return builder.finish()
