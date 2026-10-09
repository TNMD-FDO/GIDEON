"""Measure the treatment pattern set against the CaseHOLD overruling sentences.

The sentences are lowercased, which defeats eyecite, so the anchors come from
this package's own bounded finder of case names and reporter forms; it stands
in for the production citation finder here and nowhere else.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from gideon.worker.citations import NewCitation
from gideon.worker.treatment import (
    LINEAGE_SIGNALS,
    Geography,
    PatternRules,
    occurrences,
    signals,
)


@dataclass(frozen=True, slots=True)
class PinnedFile:
    """A dataset file identified by its path, byte count, and digest."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class Dataset:
    """The source and attribution for the two pinned sentence files."""

    host: str
    repository: str
    revision: str
    files: tuple[PinnedFile, ...]
    licence: str
    attribution: str


DATASET: Final = Dataset(
    host="huggingface.co",
    repository="datasets/nguha/legalbench",
    revision="daec8237410aa23e3faf4bc41ad8b3a7e1696826",
    files=(
        PinnedFile(
            "data/overruling/train.tsv", 599,
            "783d532cbee2f830dc933b49cc55408ddc8f65f3c69cc5e8b4eb866711ccbf30",
        ),
        PinnedFile(
            "data/overruling/test.tsv", 424122,
            "49dfa6ee1221f4a810df0334629859d4ad5d44b8f6b182f037b6c83a3e9e8661",
        ),
    ),
    # LegalBench states this licence for the task; the original authors state none.
    licence="CC-BY-4.0",
    attribution="Zheng et al. 2021; Guha et al. 2023",
)


@dataclass(frozen=True, slots=True)
class Sentence:
    """One labelled sentence, identified within its split."""

    case_id: str
    positive: bool
    text: str


@dataclass(frozen=True, slots=True)
class Section:
    """A synthetic majority section spanning one sentence."""

    section_id: str
    section_type: str
    char_start: int
    char_end: int
    parent_section_id: str | None = None


@dataclass(frozen=True, slots=True)
class Figures:
    """Prediction counts and the ids needed to inspect mistakes."""

    predicted: int
    true_positives: int
    false_positives: int
    false_negatives: int
    false_positive_ids: tuple[str, ...]
    false_negative_ids: tuple[str, ...]

    @property
    def precision(self) -> float:
        """Return the share of predictions that are positive."""

        return self.true_positives / self.predicted if self.predicted else 0.0

    @property
    def recall(self) -> float:
        """Return the share of positive labels found in this unit."""

        positives = self.true_positives + self.false_negatives
        return self.true_positives / positives if positives else 0.0


@dataclass(frozen=True, slots=True)
class Row:
    """Edge and sentence figures with coverage and diagnostic counts."""

    edge: Figures
    sentence: Figures
    anchored_positive: int
    anchored_negative: int
    lineage_only: int
    per_verb: tuple[tuple[str, int], ...]


# A party word is never a treatment verb, so an anchor cannot swallow one.
_PARTY = r"(?!(?:overrul|abrogat|supersed|reversed|vacated|disapprov))[a-z][a-z'.-]*"
_CASE_NAME = re.compile(
    rf"\b{_PARTY}(?:\s+{_PARTY}){{0,4}}\s+v\.\s+"
    rf"{_PARTY}(?:\s+{_PARTY}){{0,3}}\b"
)
_REPORTER = re.compile(
    r"\b\d{1,4}\s+(?:[a-z]+\.\s?){1,4}(?:(?:2d|3d|4th)\s?)?\s?\d{1,5}\b"
)


def resolve_url(pinned: PinnedFile) -> str:
    """Return the revision-pinned URL for a dataset file."""

    return (
        f"https://{DATASET.host}/{DATASET.repository}/resolve/"
        f"{DATASET.revision}/{pinned.path}"
    )


def read(root: Path) -> tuple[Sentence, ...]:
    """Read both pinned split paths as labelled tab-separated sentences."""

    sentences: list[Sentence] = []
    for pinned in DATASET.files:
        split = Path(pinned.path).stem
        with (root / pinned.path).open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, delimiter="\t")
            if reader.fieldnames != ["index", "answer", "text"]:
                raise ValueError(f"{pinned.path}: expected index, answer, text header")
            for line, record in enumerate(reader, start=2):
                label = record.get("answer")
                if (
                    label not in {"Yes", "No"}
                    or record.get("index") is None
                    or record.get("text") is None
                    or None in record
                ):
                    raise ValueError(f"{pinned.path}:{line}: invalid label or row")
                sentences.append(Sentence(
                    f"{split}-{record['index']}", label == "Yes", record["text"],
                ))
    return tuple(sentences)


def find(text: str) -> tuple[tuple[int, int], ...]:
    """Find bounded case names and reporter forms, merging overlapping spans."""

    spans = sorted(
        (match.start(), match.end())
        for pattern in (_CASE_NAME, _REPORTER)
        for match in pattern.finditer(text)
    )
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return tuple(merged)


def rows(sentence: Sentence) -> tuple[Section, tuple[NewCitation, ...]]:
    """Place every found anchor in one synthetic majority section."""

    section = Section("body", "majority", 0, len(sentence.text))
    citations = tuple(
        NewCitation(
            f"{sentence.case_id}-{ordinal}", sentence.case_id, ordinal,
            start, end, section.section_id, section.section_type, "case_cite",
            "full", sentence.text[start:end], None, None, None, None,
            "treatment-measure/citation@1",
        )
        for ordinal, (start, end) in enumerate(find(sentence.text))
    )
    return section, citations


def _figures(labels: list[tuple[str, bool, bool]]) -> Figures:
    predicted = sum(prediction for _, _, prediction in labels)
    true_positives = sum(positive and prediction for _, positive, prediction in labels)
    false_positive_ids = tuple(
        case_id for case_id, positive, prediction in labels if prediction and not positive
    )
    false_negative_ids = tuple(
        case_id for case_id, positive, prediction in labels if positive and not prediction
    )
    return Figures(
        predicted, true_positives, len(false_positive_ids),
        len(false_negative_ids), false_positive_ids, false_negative_ids,
    )


def measure(sentences: Iterable[Sentence], rules: PatternRules) -> Row:
    """Measure one rule set at citation and whole-sentence units."""

    edge_labels: list[tuple[str, bool, bool]] = []
    sentence_labels: list[tuple[str, bool, bool]] = []
    anchored_positive = anchored_negative = lineage_only = 0
    verbs: Counter[str] = Counter()
    for item in sentences:
        section, citations = rows(item)
        if citations:
            if item.positive:
                anchored_positive += 1
            else:
                anchored_negative += 1
        findings = signals(
            item.case_id, item.text, (section,), citations,
            Geography("other", None, None), lambda cluster: None,
            lambda court: None, rules,
        )
        substantive = tuple(
            finding for finding in findings
            if finding.treatment_signal not in LINEAGE_SIGNALS
        )
        verbs.update({finding.treatment_signal for finding in substantive})
        if findings and not substantive:
            lineage_only += 1
        edge_labels.append((item.case_id, item.positive, bool(substantive)))
        sentence_labels.append((
            item.case_id, item.positive,
            any(hit.signal not in LINEAGE_SIGNALS for hit in occurrences(item.text, rules)),
        ))
    return Row(
        _figures(edge_labels), _figures(sentence_labels),
        anchored_positive, anchored_negative, lineage_only,
        tuple(sorted(verbs.items())),
    )
