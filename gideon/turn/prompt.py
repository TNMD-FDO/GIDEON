"""Build the tracer and Legal prompts for a research turn."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from gideon.turn import research
from gideon.turn.history import CitationFinding, HistoryEntry, prior_turn_messages
from gideon.turn.tier import AuthorityTier

TRACER_SYSTEM: Final = "Use only the numbered passages below as sources for your answer, without relying on memory."


def messages(question: str, passages: Sequence[tuple[str, str]], *, support_level: str) -> list[dict[str, str]]:
    """Place the ordered (caption, text) pairs before the question in one user message."""

    if support_level not in ("ok", "thin"):
        raise ValueError("unsupported support level for the tracer prompt")
    parts = (TRACER_SYSTEM, THIN_SUPPORT) if support_level == "thin" else (TRACER_SYSTEM,)
    blocks = [
        f"[{number}] {caption}\n{text}" if caption else f"[{number}] {text}"
        for number, (caption, text) in enumerate(passages, start=1)
    ]
    blocks.append(f"Question: {question}")
    return [
        {"role": "system", "content": "\n\n".join(parts)},
        {"role": "user", "content": "\n\n".join(blocks)},
    ]


CLOSED_BOOK: Final = (
    "You are Legal. Use the retrieved passages in this turn as your only sources. "
    "Use earlier messages to understand the conversation. Set aside what you recall from memory. "
    "If the passages cannot answer a legal question, say where the evidence falls short."
)
SOURCE_TAGS: Final = (
    "End each sentence supported by a passage with [SRC:n], where n is the number in that passage's heading. "
    "Cite with source tags alone. Leave citation strings, reporters, page numbers, and dockets out of your own prose. "
    "Copy quotations verbatim from the tagged passage. Attribute a passage to the opinion or section named "
    "in its heading."
)
SENTENCE_KINDS: Final = (
    "Write one continuous answer. A source claim states what its tagged passage supports. "
    "In a source claim, use dates, times, numbers, and amounts only as shown in the passage or its heading. "
    "A user fact repeats a detail from the user's own messages as given. "
    "Analysis is your own reasoning and names no case, citation, quotation, or figure, except a figure the user "
    "gave. Write no label or mark of a sentence's kind."
)
ARITHMETIC_REFUSAL: Final = (
    "Explain the governing rule or method, including its periods, triggers, tolling, counting rules, rates, "
    "caps, and eligibility requirements. You may explain how offense levels and criminal history categories "
    "are determined and how the sentencing table is read. Never calculate or confirm a filing deadline, "
    "calendar date, or day count from a matter's facts. Never calculate or confirm a Guidelines range, "
    "offense level, or criminal history category for a person or matter, or look up a months range for a level "
    "and category. Never calculate or confirm a release date, credit count, or time to serve. "
    "Use the user's figures with their original values and units when explaining a method. Leave calculations "
    "to a person, including sums, percentages, quantities, counts, intermediate results, and estimates. "
    "If asked to calculate or confirm a result, decline in one sentence and offer to explain the rule or method."
)
THIN_SUPPORT: Final = (
    "The available support is thin. Give a starting point, state what the passages support, "
    "and identify important gaps. Avoid implying that the search was comprehensive."
)


def legal_system(support_level: str) -> str:
    """Build Legal's system instruction for a turn with usable support."""

    if support_level not in ("ok", "thin"):
        raise ValueError("unsupported support level for the Legal prompt")
    parts: tuple[str, ...] = (CLOSED_BOOK, SOURCE_TAGS, SENTENCE_KINDS, ARITHMETIC_REFUSAL)
    if support_level == "thin":
        parts += (THIN_SUPPORT,)
    return "\n\n".join(parts)


TIER_LABELS: Final[Mapping[str, str]] = MappingProxyType({
    "scotus": "U.S. Supreme Court",
    "circuit_published": "Published home-circuit decision",
    "circuit_unpublished": "Unpublished home-circuit decision",
    "district_home": "Home district court",
    "state_home": "Home state court",
    "persuasive": "Persuasive authority",
    "authority": "Authority text",
    "matter": "Matter document",
})
SECTION_LABELS: Final[Mapping[str, str]] = MappingProxyType({
    "syllabus": "Syllabus (separate from the opinion of the Court)",
    "headmatter": "Headmatter",
    "majority": "Majority opinion",
    "plurality": "Plurality opinion",
    "per_curiam": "Per curiam opinion",
    "concurrence": "Concurring opinion",
    "dissent": "Dissenting opinion",
    "concurrence_dissent": "Opinion concurring in part and dissenting in part",
    "footnote": "Footnote",
    "appendix": "Appendix",
    "order": "Order",
    "unknown": "Section not identified",
})
OBJECT_LABELS: Final[Mapping[str, str]] = MappingProxyType({
    "statute": "statute",
    "guideline": "sentencing guideline",
    "court_rule": "court rule",
    "bare_section": "section number",
    "bare_rule": "rule number",
    "regulation": "regulation",
    "appendix_statute": "appendix statute",
    "habeas_rule": "habeas rule",
    "scotus_rule": "Supreme Court rule",
    "docket": "docket number",
    "case_cite": "case citation",
    "state_code": "state code provision",
    "caption": "case caption",
})

_SCOPE_LINE: Final = "The GIDEON corpus snapshots searched were {lockfiles}."
_PLAN_OFF_LINE: Final = "Search plan mode was off. The question was searched as written."
_OBJECT_STATUS: Final[Mapping[str, str]] = MappingProxyType({
    "unresolved": "is not in GIDEON's corpus",
    "unresolved_in_scope": "was not found in the courts searched",
})
_CITING_PASSAGE_LINE: Final = " At least one passage in the evidence cites it."
_OUT_OF_CORPUS_COURT_LINE: Final = "The court you named, {name}, is outside GIDEON's corpus."


@dataclass(frozen=True, slots=True)
class EvidenceHit:
    """A numbered passage with the case's name and attribution."""

    number: int
    case_name: str | None
    court: str | None
    year: int | None
    authority_tier: AuthorityTier
    section_type: str
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class OutOfCorpusObject:
    """An unresolved object indexed by its position in the question's objects."""

    index: int
    object_type: str
    resolved: str
    has_citing_passage: bool


@dataclass(frozen=True, slots=True)
class OutOfCorpusCourt:
    """The id and display name of a requested court outside the corpus."""

    court_id: str
    name: str


@dataclass(frozen=True, slots=True)
class PromptProvenance:
    """The searched snapshots, filters, and unresolved references for the prompt."""

    lockfiles: Sequence[str]
    plan_mode: str
    filter_lines: Sequence[str] = field(default=(), repr=False)
    out_of_corpus_objects: Sequence[OutOfCorpusObject] = ()
    out_of_corpus_courts: Sequence[OutOfCorpusCourt] = ()


def _evidence_block(hits: Sequence[EvidenceHit]) -> str:
    if research.RESEARCH_CONFIG["evidence_order"] != "tier":
        raise ValueError("unsupported evidence_order")
    blocks: list[str] = []
    for position, hit in enumerate(hits, 1):
        if hit.number != position:
            raise ValueError(f"evidence number does not match position {position}")
        tier = TIER_LABELS.get(hit.authority_tier)
        if tier is None:
            raise ValueError(f"unsupported authority tier at evidence position {position}")
        section = SECTION_LABELS.get(hit.section_type)
        if section is None:
            raise ValueError(f"unsupported section type at evidence position {position}")
        name = hit.case_name or "Case name unavailable"
        court_year = " ".join(part for part in (hit.court, str(hit.year) if hit.year is not None else None) if part)
        if court_year:
            name += f" ({court_year})"
        blocks.append(f"[SRC:{hit.number}] {name} | {tier} | {section}\n{hit.text}")
    return "\n\n".join(("Evidence", *blocks)) if blocks else ""


def _provenance_block(provenance: PromptProvenance) -> str:
    if provenance.plan_mode != "off":
        raise ValueError("unsupported plan mode")
    lines = [
        "Retrieval provenance",
        _SCOPE_LINE.format(lockfiles=", ".join(provenance.lockfiles)),
        _PLAN_OFF_LINE,
        *provenance.filter_lines,
    ]
    for entry in provenance.out_of_corpus_objects:
        label = OBJECT_LABELS.get(entry.object_type)
        if label is None:
            raise ValueError(f"unsupported out-of-corpus object type at index {entry.index}")
        status = _OBJECT_STATUS.get(entry.resolved)
        if status is None:
            raise ValueError(f"unsupported resolved word at object index {entry.index}")
        line = f"The {label} identified as object {entry.index + 1} in the question {status}."
        if entry.has_citing_passage:
            line += _CITING_PASSAGE_LINE
        lines.append(line)
    lines.extend(_OUT_OF_CORPUS_COURT_LINE.format(name=court.name) for court in provenance.out_of_corpus_courts)
    return "\n".join(lines)


def legal_messages(
    question: str, hits: Sequence[EvidenceHit], provenance: PromptProvenance, *, support_level: str,
    history: Sequence[HistoryEntry] = (), history_findings: Mapping[int, Sequence[CitationFinding]] | None = None,
) -> list[dict[str, str]]:
    """Build the system instruction, prior messages, and the evidence and question."""

    system = legal_system(support_level)
    earlier = prior_turn_messages(history, history_findings if history_findings is not None else {})
    blocks = (_evidence_block(hits), _provenance_block(provenance), f"Question\n{question}")
    return [
        {"role": "system", "content": system},
        *earlier,
        {"role": "user", "content": "\n\n".join(block for block in blocks if block)},
    ]
