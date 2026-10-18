"""Draft synthetic questions and review sheets from the sibling's eval sample."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final, TextIO, cast

from gideon.evaluation import evalset, judge, record, synthetic
from gideon.host import backuplock, cas, engine, nogpu, report, site, stack
from gideon.host.index import record as indexrecord
from gideon.host.index import sample as indexsample
from gideon.host.render.ci import CI_ROOT
from gideon.host.render.worker import (
    CITE_FORMS,
    CITE_TYPES,
    NO_STATE_REASONS,
    PATTERN_SET_ID,
    PRECEDENTIAL_VALUES,
    QUALIFIERS,
    SECTION_TYPES,
    TREATMENT_SIGNALS,
    TREATMENT_STATES,
)
from gideon.host.sysio import PathLike, RealHost
from gideon.host.worker import bind
from gideon.worker import treatment
from gideon.worker.citations import NewCitation
from tools.segmenter.draft import DraftError, DraftHost, check_output
from tools.segmenter.manifest import make_directory, owner_of, write_atomic
from tools.synthetic import manifest

DEFAULT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
RENDERED_DIR: Final[Path] = Path("/etc/gideon/rendered")
SITE_PATH: Final[Path] = Path("/etc/gideon/site.yaml")
_SOURCE_ID: Final[re.Pattern[str]] = re.compile(r"([A-Za-z0-9._/-]+):opinion:([1-9][0-9]*)")
_SHA256: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_COURT: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")
_SET_FIX: Final[str] = "Restore the evaluation set, then retry."
_OUT_FIX: Final[str] = "Choose an empty output directory outside the checkout, then retry."
DOCUMENTS_SQL: Final[str] = """SELECT COALESCE(jsonb_agg(jsonb_build_object(
    'opinion_id', o.opinion_id,
    'doc_id', d.doc_id,
    'source', d.source,
    'status', d.status,
    'canonical_sha256', d.canonical_text_sha256,
    'court', o.court,
    'decided_date', o.decided_date,
    'reporter_cites', o.reporter_cites,
    'precedential', o.precedential,
    'case_name', o.case_name
)), '[]'::jsonb)::text
FROM opinions o
JOIN documents d ON d.doc_id = o.doc_id
WHERE o.opinion_id IN (
    SELECT value::bigint FROM jsonb_array_elements_text(:'opinion_ids'::jsonb) AS value
);
"""
EDGES_SQL: Final[str] = """SELECT COALESCE(jsonb_agg(to_jsonb(treatment_edges)), '[]'::jsonb)::text
FROM (
    SELECT o.opinion_id, s.doc_id, o.court, o.decided_date, o.reporter_cites, o.case_name,
        s.citation_id, s.signal_id, s.pattern_set, s.treatment_signal AS signal,
        s.qualifier, s.effective_section AS standing, s.state, s.no_state_reason,
        s.char_start AS verb_start, s.char_end AS verb_end,
        c.char_start AS citation_start, c.char_end AS citation_end,
        c.section_id, c.section_type, c.to_cluster AS cluster,
        (SELECT CASE WHEN bool_or(risk.state = 'negative') THEN 'negative'
                     WHEN bool_or(risk.state = 'caution') THEN 'caution' END
         FROM citation_signals risk
         JOIN citations resolved ON resolved.citation_id = risk.citation_id
         WHERE resolved.to_cluster = c.to_cluster
             AND risk.signal_source = 'pattern' AND risk.pattern_set = :'pattern_set'
        ) AS cluster_state,
        EXISTS (SELECT 1 FROM known_overruled known
                WHERE known.overruled_cluster = c.to_cluster) AS listed,
        (SELECT jsonb_build_object(
            'opinion_id', cited.opinion_id, 'court', cited.court,
            'decided_date', cited.decided_date, 'reporter_cites', cited.reporter_cites,
            'case_name', cited.case_name)
         FROM opinions cited JOIN documents ready ON ready.doc_id = cited.doc_id
         WHERE cited.cluster_id = c.to_cluster AND ready.status = 'ready'
         ORDER BY cited.decided_date, cited.opinion_id LIMIT 1
        ) AS cited
    FROM citation_signals s
    JOIN citations c ON c.citation_id = s.citation_id
    JOIN opinions o ON o.doc_id = c.doc_id
    WHERE s.signal_source = 'pattern' AND s.pattern_set = :'pattern_set'
        AND o.opinion_id IN (
            SELECT value::bigint FROM jsonb_array_elements_text(:'opinion_ids'::jsonb) AS value
        )
    ORDER BY o.opinion_id, s.citation_id
) AS treatment_edges;
"""
SECTIONS_SQL: Final[str] = """SELECT COALESCE(jsonb_agg(to_jsonb(section_rows)), '[]'::jsonb)::text
FROM (
    SELECT section_id, section_type, char_start, char_end FROM sections
    WHERE doc_id = :'doc_id' ORDER BY ordinal
) AS section_rows;
"""
SECTION_SUMMARY_SQL: Final[str] = """SELECT COALESCE(jsonb_agg(to_jsonb(section_summaries)), '[]'::jsonb)::text
FROM (
    SELECT o.opinion_id, COALESCE((
        SELECT jsonb_agg(jsonb_build_array(s.section_type, s.char_end - s.char_start) ORDER BY s.ordinal)
        FROM sections s WHERE s.doc_id = o.doc_id AND s.section_type <> 'footnote'
    ), '[]'::jsonb) AS sections
    FROM opinions o WHERE o.opinion_id IN (
        SELECT value::bigint FROM jsonb_array_elements_text(:'opinion_ids'::jsonb) AS value
    ) ORDER BY o.opinion_id
) AS section_summaries;
"""
CITATIONS_SQL: Final[str] = """SELECT COALESCE(jsonb_agg(to_jsonb(citation_rows)), '[]'::jsonb)::text
FROM (
    SELECT citation_id, doc_id, ordinal, char_start, char_end, section_id, section_type,
        cite_type, cite_form, raw_cite, reporter_cite, pincite, key, to_cluster, pattern_id
    FROM citations WHERE doc_id = :'doc_id' ORDER BY ordinal
) AS citation_rows;
"""


@dataclass(frozen=True, slots=True)
class OpinionFacts:
    """Production's opinion identity, readiness, and sampling metadata."""

    opinion_id: int
    source: str
    doc_id: str
    status: str
    sha256: str | None
    court: str
    decided: date
    cites: tuple[str, ...] = field(repr=False)
    precedential: str
    case_name: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class CitedOpinion:
    """The earliest ready opinion naming an edge's cited cluster."""

    opinion_id: int
    court: str
    decided: date
    cites: tuple[str, ...] = field(repr=False)
    case_name: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class Edge:
    """A pattern signal and its citation, cluster risk, and cited metadata."""

    opinion_id: int
    citation_id: str
    signal_id: str
    pattern_set: str
    signal: str
    qualifier: str
    standing: str
    state: str | None
    no_state_reason: str | None
    verb_start: int
    verb_end: int
    citation_start: int
    citation_end: int
    section_id: str
    section_type: str
    cluster: int | None
    cluster_state: str | None
    listed: bool
    cited: CitedOpinion | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class SectionRow:
    """A recorded section's identity, standing, and canonical bounds."""

    section_id: str
    section_type: str
    char_start: int
    char_end: int


@dataclass(frozen=True, slots=True)
class SectionSummary:
    """One drawable opinion's non-footnote section types and lengths."""

    opinion_id: int
    sections: tuple[tuple[str, int], ...]


@dataclass(slots=True)
class Figures:
    """The accumulated engine figures for every attempted request."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0

    def add(self, prompt: int | None, completion: int | None, seconds: float | None) -> None:
        self.prompt_tokens += prompt or 0
        self.completion_tokens += completion or 0
        self.seconds += seconds or 0.0


def _sample_fix() -> str:
    return f"Run {report.command('index build caselaw --sample')}, then retry."


def _record_fix(rendered_dir: PathLike) -> str:
    return f"Run {report.command('apply')}, then {stack.logs_fix(rendered_dir, record.POSTGRES_SERVICE)} and retry."


def _record_rows(io: DraftHost, rendered_dir: PathLike, stage: str, sql: str) -> list[object]:
    fix = _record_fix(rendered_dir)
    try:
        result = io.run(record.reader_argv(rendered_dir), input=sql)
    except (OSError, subprocess.SubprocessError) as error:
        raise DraftError(stage, "record read could not run", fix) from error
    if result.returncode != 0:
        raise DraftError(stage, f"record read exited {result.returncode}", fix)
    try:
        value = json.loads(result.stdout)
    except ValueError as error:
        raise DraftError(stage, "record reply is not JSON", fix) from error
    if not isinstance(value, list):
        raise DraftError(stage, "record reply is not a list", fix)
    return value


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


def _hash(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _span(start: object, end: object) -> bool:
    return type(start) is int and type(end) is int and 0 <= start < end


def _generation(io: DraftHost, name: str | None) -> tuple[indexrecord.GenerationRow, tuple[tuple[str, str, str], ...]]:
    rows = indexrecord.read_generations(io, CI_ROOT, command_path="index build caselaw --sample")
    if isinstance(rows, report.Problem):
        raise DraftError("generation", rows.problem, f"Run {report.command('apply')}, then retry. {rows.fix}")
    candidates = [
        row for row in rows if row.collection == "caselaw"
        and row.gen_id.endswith(f"__{indexsample.SAMPLE_SUFFIX}")
        and row.state in {"built", "live"} and (name is None or row.gen_id == name)
    ]
    if not candidates:
        raise DraftError("generation", "no eligible sample generation is recorded", _sample_fix())
    try:
        selected = max(candidates, key=lambda row: (datetime.fromisoformat(row.created_at), row.gen_id))
    except (ValueError, TypeError) as error:
        raise DraftError("generation", "sample generation creation time is invalid", _sample_fix()) from error
    documents = indexrecord.read_manifest_rows(io, CI_ROOT, selected.gen_id, "index build caselaw --sample")
    if isinstance(documents, report.Problem):
        raise DraftError("generation", documents.problem, f"Run {report.command('apply')}, then retry. {documents.fix}")
    return selected, documents


def _facts(io: DraftHost, rendered_dir: PathLike, rows: Sequence[tuple[str, str, str]]) -> Mapping[int, OpinionFacts]:
    requested: dict[int, str] = {}
    for identifier, _, _ in rows:
        found = _SOURCE_ID.fullmatch(identifier)
        if found is None or int(found[2]) in requested:
            raise DraftError("facts", "sample source id is not an opinion identity", _sample_fix())
        requested[int(found[2])] = identifier
    sql = f"\\set opinion_ids '{json.dumps(sorted(requested))}'\n{DOCUMENTS_SQL}"
    fix = _record_fix(rendered_dir)
    value = _record_rows(io, rendered_dir, "facts", sql)
    facts: dict[int, OpinionFacts] = {}
    for row in value:
        if not isinstance(row, dict) or set(row) != {
            "opinion_id", "doc_id", "source", "status", "canonical_sha256", "court", "decided_date", "reporter_cites",
            "precedential", "case_name",
        }:
            raise DraftError("facts", "record reply has invalid document fields", fix)
        opinion_id, doc_id, source, status, sha256, court, decided, cites = (
            row["opinion_id"], row["doc_id"], row["source"], row["status"], row["canonical_sha256"],
            row["court"], row["decided_date"], row["reporter_cites"] if row["reporter_cites"] is not None else [],
        )
        precedential, case_name = row["precedential"], row["case_name"]
        if (
            type(opinion_id) is not int or opinion_id not in requested or opinion_id in facts
            or not isinstance(source, str) or indexsample.source_id(source, opinion_id) != requested[opinion_id]
            or not isinstance(doc_id, str) or _SHA256.fullmatch(doc_id) is None
            or status not in ("processing", "ready", "failed", "withdrawn")
            or (sha256 is not None and (not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None))
            or (status == "ready" and sha256 is None)
            or not isinstance(court, str) or _COURT.fullmatch(court) is None
            or not evalset.valid_iso_date(decided)
            or not isinstance(cites, list) or not all(isinstance(cite, str) for cite in cites)
            or precedential not in PRECEDENTIAL_VALUES
            or (case_name is not None and not isinstance(case_name, str))
        ):
            raise DraftError("facts", "record reply has invalid document facts", fix)
        facts[opinion_id] = OpinionFacts(
            opinion_id, source, doc_id, status, sha256, court, date.fromisoformat(decided),
            tuple(cites), precedential, case_name,
        )
    return facts


def _edges(
    io: DraftHost, rendered_dir: PathLike, documents: Sequence[synthetic.DrawDocument],
    facts: Mapping[int, OpinionFacts],
) -> tuple[Edge, ...]:
    sql = "\n".join((
        bind("opinion_ids", json.dumps(sorted(document.opinion_id for document in documents))),
        bind("pattern_set", PATTERN_SET_ID), EDGES_SQL,
    ))
    rows = _record_rows(io, rendered_dir, "treatment", sql)
    fix = _record_fix(rendered_dir)
    allowed = {document.opinion_id for document in documents}
    keys = {item.name for item in fields(Edge)}
    seen: set[str] = set()
    result = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != keys | {
            "doc_id", "court", "decided_date", "reporter_cites", "case_name",
        }:
            raise DraftError("treatment", "record reply has invalid edge fields", fix)
        if not _positive(row["opinion_id"]) or row["opinion_id"] not in allowed:
            raise DraftError("treatment", "record reply has invalid citing identity", fix)
        fact = facts[row["opinion_id"]]
        if (
            row["doc_id"] != fact.doc_id or row["court"] != fact.court
            or row["decided_date"] != fact.decided.isoformat()
            or row["reporter_cites"] != list(fact.cites) or row["case_name"] != fact.case_name
            or not _hash(row["citation_id"]) or not _hash(row["signal_id"])
            or row["citation_id"] in seen or row["pattern_set"] != PATTERN_SET_ID
            or row["signal"] not in TREATMENT_SIGNALS or row["qualifier"] not in QUALIFIERS
            or row["standing"] not in SECTION_TYPES or row["section_type"] not in SECTION_TYPES
            or row["state"] not in (None, *TREATMENT_STATES)
            or row["no_state_reason"] not in (None, *NO_STATE_REASONS)
            or (row["state"] is None) == (row["no_state_reason"] is None)
            or not _span(row["verb_start"], row["verb_end"])
            or not _span(row["citation_start"], row["citation_end"])
            or not _hash(row["section_id"])
            or (row["cluster"] is not None and not _positive(row["cluster"]))
            or row["cluster_state"] not in (None, *TREATMENT_STATES)
            or type(row["listed"]) is not bool
        ):
            raise DraftError("treatment", "record reply has invalid edge facts", fix)
        cited = row["cited"]
        if cited is not None:
            if (
                not isinstance(cited, dict)
                or set(cited) != {"opinion_id", "court", "decided_date", "reporter_cites", "case_name"}
                or not _positive(cited["opinion_id"]) or row["cluster"] is None
                or not isinstance(cited["court"], str) or _COURT.fullmatch(cited["court"]) is None
                or not evalset.valid_iso_date(cited["decided_date"])
                or not isinstance(cited["reporter_cites"], list)
                or not all(isinstance(cite, str) for cite in cited["reporter_cites"])
                or (cited["case_name"] is not None and not isinstance(cited["case_name"], str))
            ):
                raise DraftError("treatment", "record reply has invalid cited facts", fix)
            cited = CitedOpinion(
                cited["opinion_id"], cited["court"], date.fromisoformat(cited["decided_date"]),
                tuple(cited["reporter_cites"]), cited["case_name"],
            )
        result.append(Edge(**{key: cited if key == "cited" else row[key] for key in keys}))
        seen.add(row["citation_id"])
    return tuple(result)


def _section_summaries(
    io: DraftHost, rendered_dir: PathLike, documents: Sequence[synthetic.DrawDocument],
) -> Mapping[int, SectionSummary]:
    allowed = {document.opinion_id for document in documents}
    sql = bind("opinion_ids", json.dumps(sorted(allowed))) + "\n" + SECTION_SUMMARY_SQL
    summaries: dict[int, SectionSummary] = {}
    fix = _record_fix(rendered_dir)
    for row in _record_rows(io, rendered_dir, "sections", sql):
        if (
            not isinstance(row, dict) or set(row) != {"opinion_id", "sections"}
            or not _positive(row["opinion_id"]) or row["opinion_id"] not in allowed
            or row["opinion_id"] in summaries or not isinstance(row["sections"], list)
            or any(
                not isinstance(pair, list) or len(pair) != 2 or pair[0] not in SECTION_TYPES
                or pair[0] == "footnote" or not _positive(pair[1])
                for pair in row["sections"]
            )
        ):
            raise DraftError("sections", "record reply has invalid section summary fields", fix)
        summaries[row["opinion_id"]] = SectionSummary(
            row["opinion_id"], tuple((pair[0], pair[1]) for pair in row["sections"]),
        )
    if set(summaries) != allowed:
        raise DraftError("sections", "record reply is missing drawable section summaries", fix)
    return summaries


def _sections(
    io: DraftHost, rendered_dir: PathLike, document: synthetic.DrawDocument, text: str,
) -> tuple[SectionRow, ...]:
    prefix = bind("doc_id", document.doc_id) + "\n"
    fix = _record_fix(rendered_dir)
    sections: list[SectionRow] = []
    section_ids: set[str] = set()
    for row in _record_rows(io, rendered_dir, "sections", prefix + SECTIONS_SQL):
        if (
            not isinstance(row, dict) or set(row) != {item.name for item in fields(SectionRow)}
            or not _hash(row["section_id"]) or row["section_id"] in section_ids
            or row["section_type"] not in SECTION_TYPES
            or not _span(row["char_start"], row["char_end"]) or row["char_end"] > len(text)
        ):
            raise DraftError("sections", "record reply has invalid section fields", fix)
        sections.append(SectionRow(**row))
        section_ids.add(row["section_id"])
    return tuple(sections)


def _document_rows(
    io: DraftHost, rendered_dir: PathLike, document: synthetic.DrawDocument, text: str,
) -> tuple[tuple[SectionRow, ...], tuple[NewCitation, ...]]:
    sections = _sections(io, rendered_dir, document, text)
    prefix = bind("doc_id", document.doc_id) + "\n"
    fix = _record_fix(rendered_dir)
    citations: list[NewCitation] = []
    citation_ids: set[str] = set()
    ordinals: set[int] = set()
    for row in _record_rows(io, rendered_dir, "citations", prefix + CITATIONS_SQL):
        if (
            not isinstance(row, dict) or set(row) != {item.name for item in fields(NewCitation)}
            or not _hash(row["citation_id"]) or row["citation_id"] in citation_ids
            or row["doc_id"] != document.doc_id
            or type(row["ordinal"]) is not int or row["ordinal"] < 0 or row["ordinal"] in ordinals
            or not _span(row["char_start"], row["char_end"]) or row["char_end"] > len(text)
            or not _hash(row["section_id"]) or row["section_type"] not in SECTION_TYPES
            or row["cite_type"] not in CITE_TYPES or row["cite_form"] not in CITE_FORMS
            or not isinstance(row["raw_cite"], str) or row["raw_cite"] != text[row["char_start"]:row["char_end"]]
            or any(row[key] is not None and not isinstance(row[key], str) for key in ("reporter_cite", "pincite", "key"))
            or (row["to_cluster"] is not None and not _positive(row["to_cluster"]))
            or not isinstance(row["pattern_id"], str) or not row["pattern_id"]
        ):
            raise DraftError("citations", "record reply has invalid citation fields", fix)
        citations.append(NewCitation(**row))
        citation_ids.add(row["citation_id"])
        ordinals.add(row["ordinal"])
    return sections, tuple(citations)


def _window(text: str) -> str:
    """Take the canonical head at a paragraph boundary inside the prompt budget.

    A first paragraph longer than the budget is cut at the budget: the retrace
    runs over the whole text, so the cut only narrows what the generator reads.
    """

    if len(text) <= synthetic.TEMPLATE_WINDOW_CHARS:
        return text
    boundary = text.rfind("\n\n", 0, synthetic.TEMPLATE_WINDOW_CHARS + 1)
    if boundary <= 0 or not text[:boundary].strip():
        return text[:synthetic.TEMPLATE_WINDOW_CHARS]
    return text[:boundary]


def _candidate(
    io: DraftHost, rendered_dir: PathLike, served_model_name: str,
    template: judge.JudgePrompt, facts: OpinionFacts, text: str, figures: Figures,
    *, kind: str = "", target: SectionRow | None = None,
) -> tuple[synthetic.Proposal, synthetic.Retraced, synthetic.ClosedBook] | str:
    values = {
        "court": facts.court, "decided": facts.decided.isoformat(),
        "cites": "; ".join(facts.cites), "caption": (facts.case_name or "").strip(),
        "text": _window(text if target is None else text[target.char_start:target.char_end]),
    }
    if target is not None:
        values.update(parties=(facts.case_name or "").strip(), kind=kind, section_type=target.section_type)
    proposal, prompt, completion, seconds = synthetic.propose(
        io, rendered_dir, served_model_name=served_model_name, template=template,
        slots={slot: values[slot] for slot in template.slots},
    )
    figures.add(prompt, completion, seconds)
    if isinstance(proposal, judge.Failure):
        return proposal.code
    retraced = synthetic.retrace(text, proposal.passage)
    if isinstance(retraced, synthetic.Discard):
        return retraced.reason
    if target is not None and not synthetic.within_section(retraced, target.char_start, target.char_end):
        return "off-section"
    return _closed_book(io, rendered_dir, served_model_name, proposal, retraced, figures)


def _closed_book(
    io: DraftHost, rendered_dir: PathLike, served_model_name: str,
    proposal: synthetic.Proposal, retraced: synthetic.Retraced, figures: Figures,
) -> tuple[synthetic.Proposal, synthetic.Retraced, synthetic.ClosedBook] | str:
    answer, prompt, completion, seconds = synthetic.answer_closed_book(
        io, rendered_dir, served_model_name=served_model_name, question=proposal.question,
    )
    figures.add(prompt, completion, seconds)
    if isinstance(answer, judge.Failure):
        if answer.code == "truncated":
            return proposal, retraced, synthetic.ClosedBook(synthetic.CLOSED_BOOK_UNFINISHED, 0, None)
        return "closed-book-failed"
    reading, prompt, completion, seconds = synthetic.read_closed_book(
        io, rendered_dir, served_model_name=served_model_name,
        question=proposal.question, reference=proposal.answer, candidate=answer.answer,
    )
    figures.add(prompt, completion, seconds)
    if isinstance(reading, judge.Failure):
        return "closed-book-failed"
    return proposal, retraced, reading


def _treatment_candidate(
    io: DraftHost, rendered_dir: PathLike, served_model_name: str,
    template: judge.JudgePrompt, facts: OpinionFacts, document: synthetic.DrawDocument,
    edge: Edge, text: str, figures: Figures,
) -> tuple[synthetic.Proposal, synthetic.Retraced, synthetic.ClosedBook] | str:
    sections, citations = _document_rows(io, rendered_dir, document, text)
    citation = next((row for row in citations if row.citation_id == edge.citation_id), None)
    section = next((row for row in sections if row.section_id == edge.section_id), None)
    if (
        citation is None or section is None
        or (citation.char_start, citation.char_end, citation.section_id, citation.section_type, citation.to_cluster)
        != (edge.citation_start, edge.citation_end, edge.section_id, edge.section_type, edge.cluster)
        or section.section_type != citation.section_type
        or not section.char_start <= citation.char_start < citation.char_end <= section.char_end
    ):
        raise DraftError("treatment", "edge citation or section disagrees with the record", _record_fix(rendered_dir))
    first, last = treatment.sentence(text, citation.char_start, citation.char_end, section, citations, treatment.RULES)
    retraced = synthetic.fix_span(text, first, last, edge.verb_start, edge.verb_end)
    if isinstance(retraced, synthetic.Discard):
        return retraced.reason
    assert edge.cited is not None and edge.cited.case_name is not None and facts.case_name is not None
    proposal, prompt, completion, seconds = synthetic.propose(
        io, rendered_dir, served_model_name=served_model_name, template=template,
        slots={
            "court": facts.court, "decided": facts.decided.isoformat(), "cites": "; ".join(facts.cites),
            "parties": facts.case_name, "cited_parties": edge.cited.case_name,
            "cited_cites": "; ".join(edge.cited.cites), "cited_court": edge.cited.court,
            "cited_decided": edge.cited.decided.isoformat(), "kind": document.kind,
            "signal": edge.signal, "qualifier": edge.qualifier, "standing": edge.standing,
            "sentence": retraced.quote,
        },
    )
    figures.add(prompt, completion, seconds)
    if isinstance(proposal, judge.Failure):
        return proposal.code
    if synthetic.good_law_wording(proposal.answer):
        return "good-law-wording"
    return _closed_book(io, rendered_dir, served_model_name, proposal, retraced, figures)


def _document(
    io: DraftHost, rendered_dir: PathLike, served_model_name: str,
    template: judge.JudgePrompt, document: synthetic.DrawDocument, facts: OpinionFacts,
    edge: Edge | None = None,
) -> tuple[
    tuple[synthetic.Proposal, synthetic.Retraced, synthetic.ClosedBook] | str,
    Figures, SectionRow | None, tuple[SectionRow, ...],
]:
    """Fetch and evaluate one document, retaining its own engine figures."""

    figures = Figures()
    value = cas.get(io, document.sha256)
    if isinstance(value, report.Problem):
        raise DraftError("text", f"source {document.source_id} canonical text is unavailable", value.fix)
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DraftError("text", f"source {document.source_id} canonical text is not UTF-8", "Restore the canonical object, then retry.") from error
    if len(text) < synthetic.DOCUMENT_MIN_CHARS:
        return "document-short", figures, None, ()
    target: SectionRow | None = None
    sections: tuple[SectionRow, ...] = ()
    if template.id.startswith("section-attribution@"):
        sections = _sections(io, rendered_dir, document, text)
        target_types = synthetic.SEPARATE_SECTIONS if document.kind == "separate" else treatment.HOLDING_SECTIONS
        target = min(
            (section for section in sections if section.section_type in target_types
             and section.char_end - section.char_start >= synthetic.SECTION_MIN_CHARS),
            key=lambda section: (-(section.char_end - section.char_start), section.char_start), default=None,
        )
        if target is None:
            raise DraftError("sections", "target section is absent from the record", _record_fix(rendered_dir))
    if edge is not None:
        result = _treatment_candidate(io, rendered_dir, served_model_name, template, facts, document, edge, text, figures)
    else:
        result = _candidate(io, rendered_dir, served_model_name, template, facts, text, figures, kind=document.kind, target=target)
    return result, figures, target, sections


def _json_line(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")


def _sheet(ordinal: int, document: synthetic.DrawDocument, facts: OpinionFacts,
           proposal: synthetic.Proposal, retraced: synthetic.Retraced, reading: synthetic.ClosedBook,
           *, required: bool, edge: Edge | None = None, target: SectionRow | None = None,
           sections: Sequence[SectionRow] = ()) -> str:
    suffix = manifest.SHEET_REQUIRED if required else manifest.SHEET_OPTIONAL
    extra: tuple[str, ...] = ()
    if edge is not None:
        assert edge.cited is not None
        extra = (
            f"Cited parties: {edge.cited.case_name}",
            "Cited citations: " + ("; ".join(edge.cited.cites) or "no cite recorded"),
            f"Cited court: {edge.cited.court} | decided {edge.cited.decided.isoformat()}",
            "Cited original: " + manifest.original_link(edge.cited.cites),
            f"Kind: {document.kind} | verb {edge.signal} | qualifier {edge.qualifier} | standing {edge.standing}",
            f"Edge: {edge.citation_id} | signal {edge.signal_id} | pattern set {edge.pattern_set}",
        )
    elif target is not None:
        other_types = sorted({section.section_type for section in sections
                              if section.section_type not in {"footnote", target.section_type}})
        extra = (
            f"Kind: {document.kind} | target section {target.section_type}",
            "Other section types: " + (", ".join(other_types) or "none"),
        )
    return "\n".join((
        f"{manifest.SHEET_BLOCK_HEADER} {ordinal:02d} {suffix}",
        f"Source: {document.source_id} | court {document.court} | decided {facts.decided.isoformat()}",
        *((f"Case name: {facts.case_name.strip()}",) if facts.case_name and facts.case_name.strip() else ()),
        "Cited as: " + ("; ".join(document.cites) or "no cite recorded"),
        "Original: " + manifest.original_link(document.cites),
        *extra,
        "Question: " + proposal.question,
        "Expected answer: " + proposal.answer,
        f"Quote: sha256 {document.sha256} [{retraced.start}, {retraced.end})",
        retraced.quote,
        "Closed-book answer: " + reading.answer,
        f"Closed-book score: {reading.score} | label {reading.label or 'none'}", "", "",
    ))


def _run(io: DraftHost, *, checkout: Path, set_root: Path, out: Path, category: str,
         count: int | None, seed: int, generation: str | None, rendered_dir: PathLike,
         site_path: PathLike, models_path: PathLike, now: Callable[[], datetime],
         sleep: Callable[[float], None], stdout: TextIO) -> None:
    if io.geteuid() != 0:
        raise DraftError("set", "root privileges are required", "Run sudo python3 -B -m tools.synthetic.draft with the same flags, then retry.")
    loaded_result = evalset.load_set(set_root)
    if not loaded_result.ok or loaded_result.loaded is None:
        raise DraftError("set", "evaluation set cannot be loaded", _SET_FIX)
    loaded = loaded_result.loaded
    spec = evalset.SHAPE_REGISTRY.get(("research-qa", category))
    templates = [template for template in synthetic.TEMPLATES.values() if template.id.startswith(f"{category}@")]
    if spec is None or not spec.synthetic_keys or not templates:
        raise DraftError("set", f"category {category} has no synthetic template", "Name a category with a registered synthetic shape and template, then retry.")
    if count is None:
        count = synthetic.PLANNED_COUNTS[category]
    template = max(templates, key=lambda item: int(item.id.rsplit("@", 1)[1]))
    try:
        pin = manifest.pin_file(set_root / synthetic.case_path(category))
    except OSError as error:
        raise DraftError("set", "category file cannot be read", _SET_FIX) from error
    check_output(out, checkout)
    claim = backuplock.claim(io, command="tools.synthetic.draft", now=now(), lock=backuplock.ENGINE_LOCK)
    if claim.refusal is not None:
        raise DraftError("lock", claim.refusal.detail, claim.refusal.fix)
    try:
        if nogpu.is_no_gpu_host(io):
            raise DraftError("engine", "the no-GPU marker refuses the draft", "Run the draft on the GPU box, then retry.")
        site_result = site.load_site(Path(site_path), host=io)
        if site_result.errors or site_result.config is None:
            fix = site_result.errors[0].fix if site_result.errors else "Restore the site file, then retry."
            raise DraftError("engine", "site file cannot be loaded", fix)
        target = engine.resolve_engine_target(
            io, rendered_dir, hardware_profile=site_result.config.hardware_profile,
            models_path=models_path, sleep=sleep,
        )
        if isinstance(target, report.Problem):
            raise DraftError("engine", target.problem, target.fix)
        selected, rows = _generation(io, generation)
        facts = _facts(io, rendered_dir, rows)
        cased = {
            entry["source_id"] for case_id in loaded.active_ids
            for case in (loaded.cases_by_id[case_id],)
            if case.get("category") == category and cast(list[str], case.get("labels", []))[:1] == [synthetic.SYNTHETIC_ORIGIN]
            for entry in cast(list[dict[str, object]], case["gold"])
        }
        discards: list[dict[str, object]] = []
        discarded: Counter[str] = Counter()

        def discard(source_id: str, reason: str, edge: str | None = None, kind: str | None = None) -> None:
            assert reason in manifest.DISCARD_REASONS
            discarded[reason] += 1
            discards.append(dict(zip(manifest.DISCARD_KEYS, (reason, source_id, edge, kind), strict=True)))

        documents: list[synthetic.DrawDocument] = []
        for identifier, sha256, doc_id in rows:
            found = _SOURCE_ID.fullmatch(identifier)
            assert found is not None
            opinion_id = int(found[2])
            fact = facts.get(opinion_id)
            if fact is None or fact.status != "ready":
                discard(identifier, "not-ready")
            elif fact.sha256 != sha256:
                discard(identifier, "hash-changed")
            elif fact.precedential != "published":
                discard(identifier, "unpublished")
            elif fact.case_name is None or not fact.case_name.strip():
                discard(identifier, "no-case-name")
            elif identifier in cased:
                discard(identifier, "already-cased")
            else:
                documents.append(synthetic.DrawDocument(identifier, sha256, doc_id, opinion_id, fact.court, fact.decided.year // 10 * 10, fact.cites))
        print(f"generation: {selected.gen_id}, {len(rows)} documents", file=stdout, flush=True)
        print(f"facts: {len(facts)} opinions, {len(documents)} drawable, {len(discards)} skipped", file=stdout, flush=True)
        for item in discards:
            print(f"{item['source_id']}: skipped {item['reason']}", file=stdout, flush=True)
        try:
            owner = owner_of(out)
            make_directory(out, owner)
        except OSError as error:
            raise DraftError("write", "output directory cannot be created", _OUT_FIX) from error
        edges: dict[str, Edge] = {}
        edge: Edge | None
        if category == "treatment":
            drawn = []
            by_opinion = {document.opinion_id: document for document in documents}
            edge_rows = _edges(io, rendered_dir, documents, facts)
            eligible: Counter[str] = Counter()
            skipped: Counter[str] = Counter()
            for edge in edge_rows:
                document = by_opinion[edge.opinion_id]
                kind = synthetic.treatment_kind(edge.state, edge.no_state_reason, edge.standing, edge.cluster_state, edge.listed)
                outranked = edge.cluster_state is not None or edge.listed
                reason = None
                if kind is None:
                    reason = "state-outranked" if edge.state == "caution" or (edge.no_state_reason == "non_holding" and outranked) else "no-state-kind"
                elif edge.cited is None:
                    reason = "cited-absent"
                elif edge.cited.case_name is None or not edge.cited.case_name.strip():
                    reason = "no-case-name"
                if reason is not None:
                    discard(document.source_id, reason, edge.citation_id, kind)
                    skipped[reason] += 1
                else:
                    assert kind is not None
                    drawn.append(replace(document, kind=kind, item=edge.citation_id))
                    edges[edge.citation_id] = edge
                    eligible[kind] += 1
            documents = drawn
            counts = ", ".join(f"{kind}={eligible[kind]}" for kind in synthetic.TREATMENT_KINDS)
            reasons = ", ".join(f"{reason}={skipped[reason]}" for reason in sorted(skipped)) or "none"
            print(f"treatment: {len(edge_rows)} edges, eligible {counts}, skipped {sum(skipped.values())} ({reasons})", file=stdout, flush=True)
        elif category == "section-attribution":
            summaries = _section_summaries(io, rendered_dir, documents)
            drawn = []
            eligible = Counter()
            no_kind = 0
            for document in documents:
                document_kinds = synthetic.attribution_kinds(summaries[document.opinion_id].sections)
                if not document_kinds:
                    discard(document.source_id, "no-kind")
                    no_kind += 1
                for kind in document_kinds:
                    drawn.append(replace(document, kind=kind))
                    eligible[kind] += 1
            documents = drawn
            counts = ", ".join(f"{kind}={eligible[kind]}" for kind in synthetic.ATTRIBUTION_KINDS)
            print(f"sections: eligible {counts}, no-kind {no_kind}", file=stdout, flush=True)
        entries: list[dict[str, object]] = []
        lines: list[bytes] = []
        sheets: list[str] = []
        read_share = 1 if spec.zero_tolerance else synthetic.READ_SHARE

        def checkpoint() -> None:
            sheet = "".join(sheets).encode("utf-8")
            sheet_digest = hashlib.sha256(sheet).hexdigest()
            read_ordinals = synthetic.sample_ordinals(len(entries), read_share)
            document_manifest = dict(zip(manifest.MANIFEST_KEYS, (
                manifest.MANIFEST_FORMAT, str(set_root.resolve()), loaded.digest, category, pin,
                template.id, synthetic.TEMPLATE_DIGESTS[template.id], "closed-book@1",
                synthetic.READING_DIGESTS["closed-book@1"], selected.gen_id, seed, count,
                read_share, read_ordinals, entries, discards, sheet_digest, len(sheet),
            ), strict=True))
            try:
                for name, payload in (
                    ("drafts.jsonl", b"".join(lines)), ("sheet.txt", sheet),
                    ("manifest.json", _json_line(document_manifest)),
                    ("marks.template.txt", manifest.marks_template(sheet_digest, read_ordinals).encode("ascii")),
                ):
                    write_atomic(out / name, payload, owner=owner)
            except OSError as error:
                raise DraftError("write", "draft outputs cannot be written", _OUT_FIX) from error

        checkpoint()
        checkpoint_discards = len(discards)
        figures = Figures()
        closed_book_count = 0
        order = iter(synthetic.draw_order(documents, seed))
        kept_sources: set[str] = set()
        kept_clusters: set[int] = set()
        kinds: Counter[str] = Counter()

        def duplicate_reason(document: synthetic.DrawDocument) -> str | None:
            if document.source_id in kept_sources:
                return "same-document"
            edge = edges.get(document.item)
            if edge is not None and edge.cluster in kept_clusters:
                return "same-cluster"
            return None

        def discard_item(document: synthetic.DrawDocument, reason: str) -> None:
            discard(document.source_id, reason, document.item or None, document.kind or None)
            print(f"{document.source_id}: discarded {reason}", file=stdout, flush=True)

        with ThreadPoolExecutor(max_workers=synthetic.DRAFT_WINDOW) as executor:
            pending: deque[tuple[synthetic.DrawDocument, Future[tuple[
                tuple[synthetic.Proposal, synthetic.Retraced, synthetic.ClosedBook] | str,
                Figures, SectionRow | None, tuple[SectionRow, ...],
            ]]]] = deque()

            def submit_next() -> bool:
                for document in order:
                    reason = duplicate_reason(document)
                    if reason is not None:
                        discard_item(document, reason)
                        continue
                    pending.append((document, executor.submit(
                        _document, io, rendered_dir, target.served_model_name,
                        template, document, facts[document.opinion_id], edges.get(document.item),
                    )))
                    return True
                return False

            for _ in range(synthetic.DRAFT_WINDOW):
                if not submit_next():
                    break
            while pending:
                document, future = pending.popleft()
                result, used, section_target, sections = future.result()
                edge = edges.get(document.item)
                before = figures.seconds
                figures.add(used.prompt_tokens, used.completion_tokens, used.seconds)
                result = duplicate_reason(document) or result
                if isinstance(result, str):
                    discard_item(document, result)
                else:
                    proposal, retraced, reading = result
                    ordinal = len(entries) + 1
                    labels = [synthetic.SYNTHETIC_ORIGIN] + ([reading.label] if reading.label is not None else [])
                    draft: dict[str, object] = {
                        "suite": spec.suite, "category": category, "branch": spec.fixed_fields["branch"],
                        "question": proposal.question, "jurisdiction": [document.court],
                        "expected": {"answer": proposal.answer, "must_cite": [document.source_id]},
                        "gold": [{"source_id": document.source_id, "sha256": document.sha256,
                                  "start": retraced.start, "end": retraced.end, "quote": retraced.quote}],
                        "labels": labels, "template": template.id, "cluster_id": document.source_id,
                        "notes": "", "closed_book_answer": reading.answer, "ordinal": ordinal,
                    }
                    if edge is not None:
                        cast(dict[str, object], draft["expected"])["state"] = document.kind
                        cast(list[dict[str, object]], draft["gold"])[0].update(
                            citation_id=edge.citation_id, signal_id=edge.signal_id,
                            pattern_set=edge.pattern_set, cluster=edge.cluster,
                        )
                    elif section_target is not None:
                        cast(dict[str, object], draft["expected"])["section_type"] = section_target.section_type
                    line = _json_line({key: draft[key] for key in manifest.DRAFT_KEYS})
                    lines.append(line)
                    sheets.append(_sheet(
                        ordinal, document, facts[document.opinion_id], proposal, retraced, reading,
                        required=ordinal in synthetic.sample_ordinals(ordinal, read_share),
                        edge=edge, target=section_target, sections=sections,
                    ))
                    entries.append(dict(zip(manifest.ENTRY_KEYS, (
                        ordinal, document.source_id, document.sha256, document.doc_id, retraced.start, retraced.end,
                        len(retraced.quote), hashlib.sha256(retraced.quote.encode("utf-8")).hexdigest(),
                        reading.score, reading.label, hashlib.sha256(line).hexdigest(),
                    ), strict=True)))
                    closed_book_count += int(reading.label is not None)
                    kept_sources.add(document.source_id)
                    if edge is not None:
                        assert edge.cluster is not None
                        kept_clusters.add(edge.cluster)
                    if document.kind:
                        kinds[document.kind] += 1
                    checkpoint()
                    checkpoint_discards = len(discards)
                    identity = document.source_id + (f" {edge.citation_id} {document.kind}" if edge is not None else "")
                    if section_target is not None:
                        identity += f" {document.kind} {section_target.section_type}"
                    print(f"{ordinal:02d}: {identity} [{retraced.start}, {retraced.end}) length {len(retraced.quote)}, "
                          f"closed-book score {reading.score}, label {reading.label or 'none'}, {figures.seconds - before:.3f} s", file=stdout, flush=True)
                if len(entries) >= count:
                    break
                submit_next()
        if len(entries) < count:
            print(f"order: exhausted, kept {len(entries)} of {count}", file=stdout, flush=True)
        if len(discards) != checkpoint_discards:
            checkpoint()
        reasons = ", ".join(f"{reason}={discarded[reason]}" for reason in sorted(discarded)) or "none"
        summary_kinds = (synthetic.TREATMENT_KINDS if category == "treatment" else
                         synthetic.ATTRIBUTION_KINDS if category == "section-attribution" else ())
        kind_counts = "kinds " + ", ".join(f"{kind}={kinds[kind]}" for kind in summary_kinds) + ", " if summary_kinds else ""
        print(f"summary: kept {len(entries)}, discarded {sum(discarded.values())} ({reasons}), "
              f"{kind_counts}closed-book-answerable {closed_book_count}, engine {figures.seconds:.3f} s, "
              f"prompt tokens {figures.prompt_tokens}, completion tokens {figures.completion_tokens}", file=stdout, flush=True)
    finally:
        backuplock.release_claim(io, claim, lock=backuplock.ENGINE_LOCK)


def _positive_count(value: str) -> int:
    count = int(value)
    if count <= 0:
        raise argparse.ArgumentTypeError("count must be positive")
    return count


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Draft synthetic evaluation cases")
    parser.add_argument("--category", required=True)
    parser.add_argument("--count", type=_positive_count, default=None, help="default: the category's planned count")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--set", dest="set_root", type=Path)
    parser.add_argument("--generation")
    return parser


def main(argv: Sequence[str] | None = None, *, host: DraftHost | None = None,
         root: Path | None = None, rendered_dir: PathLike = RENDERED_DIR,
         site_path: PathLike = SITE_PATH, models_path: PathLike | None = None,
         clock: Callable[[], datetime] | None = None, sleep: Callable[[float], None] = time.sleep,
         stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    """Run the box-side draft under the engine lock with content-free diagnostics."""

    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    try:
        args = _parser().parse_args(argv)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    checkout = DEFAULT_ROOT if root is None else root
    io = cast(DraftHost, RealHost()) if host is None else host
    try:
        _run(
            io, checkout=checkout, set_root=checkout / evalset.SET_ROOT if args.set_root is None else args.set_root,
            out=args.out, category=args.category, count=args.count, seed=args.seed, generation=args.generation,
            rendered_dir=rendered_dir, site_path=site_path,
            models_path=checkout / "models.lock" if models_path is None else models_path,
            now=(lambda: datetime.now(UTC)) if clock is None else clock, sleep=sleep, stdout=output,
        )
    except DraftError as error:
        print(f"{error.stage}: refused — {error.problem}. Fix: {error.fix}", file=errors)
        return 1
    except Exception as error:  # noqa: BLE001 - opinion and model text never enter diagnostics
        print(f"synthetic draft: refused — internal error ({type(error).__name__}). Fix: Repair the draft command, then retry.", file=errors)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
