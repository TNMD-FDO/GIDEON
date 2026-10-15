"""Deterministic sample selection, document identities, and job argument validation.

The host and the worker both import this module, so it reads the standard
library alone and is the one home of the sample's names and bounds.
"""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

SAMPLE_SUFFIX: Final[str] = "sample"
# exempt: the eval sample fills about one document in a hundred, a starting value.
SAMPLE_FILL_PER_MILLION: Final[int] = 10_000
# exempt: a per-million rate's fixed denominator, the whole population at its maximum.
SAMPLE_FILL_MAX: Final[int] = 1_000_000
SOURCE_ID_PATTERN: Final[str] = r"[A-Za-z0-9._:/-]+"
# exempt: the bound limits a source id carried in a queue argument and a record row.
SOURCE_ID_MAX_LENGTH: Final[int] = 200
# exempt: the bound caps the gold list carried in a queue argument.
GOLD_MAX: Final[int] = 10_000
_SHA256: Final = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class GoldDocument:
    """A gold source id with a judged hash or an unversioned must-cite id."""

    source_id: str
    sha256: str | None


@dataclass(frozen=True, slots=True)
class SampleInputs:
    """The gold documents, fill rate, and reported eval set version."""

    gold: tuple[GoldDocument, ...]
    fill_per_million: int
    set_version: str


def in_fill(label: str, doc_id: str, per_million: int) -> bool:
    """Select a document by a label-seeded hash using exact integer arithmetic."""

    digest = hashlib.sha256(f"{label}\n{doc_id}".encode()).digest()
    value = int.from_bytes(digest[:8], "big")
    return value * SAMPLE_FILL_MAX < per_million * (1 << 64)


def source_id(source: str, opinion_id: int) -> str:
    """Name an opinion independently of its source snapshot."""

    return f"{source}:opinion:{opinion_id}"


def sample_name(name: str) -> str:
    """Append the sample marker to a generation name."""

    return f"{name}__{SAMPLE_SUFFIX}"


def sample_inputs_hash(base_hash: str, label: str, per_million: int) -> str:
    """Hash the base identity, sample marker, seed label, and fill rate."""

    text = "\n".join((base_hash, SAMPLE_SUFFIX, label, str(per_million)))
    return hashlib.sha256(text.encode()).hexdigest()


def arguments(inputs: SampleInputs) -> dict[str, object]:
    """Serialize a sample's bounded queue arguments without its reporting version."""

    return {
        "gold": [
            {"source_id": document.source_id, "sha256": document.sha256}
            for document in inputs.gold
        ],
        "fill_per_million": inputs.fill_per_million,
    }


def validate(value: object) -> tuple[str, ...]:
    """Return invalid field names without echoing argument values."""

    if not isinstance(value, Mapping):
        return ("sample",)
    findings: list[str] = []
    if set(value) != {"gold", "fill_per_million"}:
        findings.append("keys")
    gold = value.get("gold")
    if not isinstance(gold, list) or len(gold) > GOLD_MAX:
        findings.append("gold")
    else:
        for document in gold:
            if not isinstance(document, Mapping) or set(document) != {"source_id", "sha256"}:
                findings.append("gold")
                continue
            identifier = document.get("source_id")
            if (
                not isinstance(identifier, str) or not 1 <= len(identifier) <= SOURCE_ID_MAX_LENGTH
                or re.fullmatch(SOURCE_ID_PATTERN, identifier) is None
            ):
                findings.append("source_id")
            digest = document.get("sha256")
            if digest is not None and (
                not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
            ):
                findings.append("sha256")
    rate = value.get("fill_per_million")
    if type(rate) is not int or not 1 <= rate <= SAMPLE_FILL_MAX:
        findings.append("fill_per_million")
    return tuple(dict.fromkeys(findings))
