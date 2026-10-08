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
FailureReason = Literal["empty", "unparseable"]

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
    """A dropped locator: its kind, printed label, and code-point offset in the text."""

    kind: MarkerKind
    label: str
    offset: int


@dataclass(frozen=True, slots=True)
class Parsed:
    """The NFC canonical text and the markers dropped from it, in document order."""

    text: str
    markers: tuple[Marker, ...]


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
    raw: str, markers: list[tuple[int, MarkerKind, str]], preserve: bool,
) -> tuple[str, list[Marker]]:
    """One line's text, its whitespace collapsed unless preserved, and its markers.

    Each word is normalized alone: a space never composes, so the joined words
    equal the line's normalization, and a marker's offset counts the normalized
    prefix rather than the raw one.
    """

    if preserve:
        start = len(raw) - len(raw.lstrip("\r\n"))
        end = len(raw.rstrip("\r\n"))
        text = unicodedata.normalize("NFC", raw[start:end])
        return text, [
            Marker(kind, label, len(unicodedata.normalize("NFC", raw[start:max(start, min(pos, end))])))
            for pos, kind, label in markers
        ]

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

    located: list[Marker] = []
    for pos, kind, label in markers:
        index = bisect_right(starts, pos) - 1
        if index < 0:
            offset = 0
        elif pos <= words[index].end():
            prefix = raw[words[index].start():pos]
            offset = offsets[index] + len(unicodedata.normalize("NFC", prefix))
        elif index + 1 < len(words):
            offset = offsets[index + 1]
        else:
            offset = length
        located.append(Marker(kind, label, offset))
    return " ".join(pieces), located


class _Builder:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.markers: list[Marker] = []
        self.pending_markers: list[tuple[MarkerKind, str]] = []
        self.raw: list[str] = []
        self.raw_length = 0
        self.raw_markers: list[tuple[int, MarkerKind, str]] = []
        self.length = 0
        self.breaks = 0
        self.preserve = False

    def text(self, value: str | None) -> None:
        if value:
            self.raw.append(value)
            self.raw_length += len(value)

    def marker(self, kind: MarkerKind, label: str) -> None:
        self.raw_markers.append((self.raw_length, kind, label))

    def flush(self) -> None:
        value, markers = _normalized_segment(
            "".join(self.raw), self.raw_markers, self.preserve,
        )
        self.raw = []
        self.raw_length = 0
        self.raw_markers = []
        if not value:
            self.pending_markers.extend((marker.kind, marker.label) for marker in markers)
            return
        separator = "\n" * self.breaks if self.parts else ""
        base = self.length + len(separator)
        self.parts.append(separator + value)
        self.markers.extend(Marker(kind, label, base) for kind, label in self.pending_markers)
        self.markers.extend(
            Marker(marker.kind, marker.label, base + marker.offset) for marker in markers
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
            Marker(kind, label, self.length) for kind, label in self.pending_markers
        )
        value = "".join(self.parts)
        if not value.strip():
            raise OpinionTextFailure("empty")
        return Parsed(value, tuple(self.markers))


def _tag(element: etree._Element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower()


def _marker(element: etree._Element, column: str) -> tuple[MarkerKind, str] | None:
    tag = _tag(element)
    classes = set((element.get("class") or "").split())
    if (
        tag == "page-number"
        or tag == "span" and ("star-pagination" in classes or "ldml-pagenumber" in classes)
        or tag == "a" and "page-label" in classes
    ):
        printed = "".join(element.itertext()).strip().lstrip("*").strip()
        if printed.lower().startswith("page "):
            printed = printed[5:].strip()
        label = printed or element.get("label") or element.get("data-label") or ""
        return "page", label
    if (
        tag in {"footnotemark", "footnotereference"}
        or tag == "a" and (element.get("href") or "").startswith("#")
        or tag == "sup" and _BRACKETED_LABEL.fullmatch("".join(element.itertext()).strip())
        or tag == "span" and "MsoFootnoteReference" in classes
    ):
        return "footnote-mark", "".join(element.itertext()).strip()
    if tag == "bracketnum" or column == "html" and tag == "span" and "num" in classes:
        return "editorial", "".join(element.itertext()).strip()
    return None


def _is_block(element: etree._Element, column: str) -> bool:
    tag = _tag(element)
    if column != "xml_harvard":
        return tag in _HTML_BLOCKS
    classes = set((element.get("class") or "").split())
    return tag in _XML_BLOCKS or tag == "div" and bool(classes & {"footnote", "footnotes"})


def _walk(element: etree._Element, column: str, builder: _Builder) -> None:
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
    previous_preserve = builder.preserve
    if tag == "pre":
        builder.preserve = True
    builder.text(element.text)
    for child in element:
        _walk(child, column, builder)
        builder.text(child.tail)
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
        return Parsed(value, ())

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
    _walk(root, column, builder)
    return builder.finish()
