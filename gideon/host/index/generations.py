"""Collection definitions and deterministic index generation identity."""

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from gideon.host.caselaw import CourtGeography
from gideon.host.corpus.lockfile import Lockfile
from gideon.host.corpus.sources import SOURCES, SourceDefinition
from gideon.host.index.sample import SampleInputs
from gideon.host.render import worker


@dataclass(frozen=True, slots=True)
class Collection:
    """A collection's document profile and feeding-source rule."""

    name: str
    profile: str
    feeds: Callable[[SourceDefinition], bool]


COLLECTIONS: tuple[Collection, ...] = (
    Collection(worker.COLLECTION_NAMES[0], "caselaw", lambda source: source.carries_courts),
)


def generation_name(collection: str, label: str, space_id: str) -> str:
    """Name a generation using safe components within the index byte limit."""

    name = f"{collection}__{label}__{space_id}"
    if re.fullmatch(worker.GENERATION_PATTERN, name) is None or len(name.encode()) >= 255:
        raise ValueError(
            "Invalid generation name. Fix: use a registered collection, a corpus-YYYY-MM-DD "
            "label, and a lowercase embedding space id within the index name byte limit."
        )
    return name


def inputs_hash(
    lockfile: Lockfile, collection: str, space_id: str,
    *, sources: Sequence[SourceDefinition] = SOURCES,
) -> str:
    """Hash feeding sidecars and courts in registry order, pipeline, and space."""

    definition = next((item for item in COLLECTIONS if item.name == collection), None)
    if definition is None:
        raise ValueError("Unknown collection. Fix: use a collection from the collection registry.")
    lines: list[str] = []
    for source in sources:
        if definition.feeds(source):
            pin = lockfile.sources[source.name]
            lines.extend((source.name, pin.sidecar_sha256))
            if source.carries_courts:
                lines.extend(sorted(pin.courts or ()))
    lines.extend((lockfile.pipeline, space_id))
    return hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class GenerationInputs:
    """The immutable scope and identity passed to the record and lexical job."""

    name: str
    collection: str
    label: str
    snapshot: str
    courts: tuple[str, ...]
    geography: Mapping[str, CourtGeography]
    pipeline: str
    embedding_space: str
    inputs_hash: str
    sample: SampleInputs | None = field(default=None, kw_only=True)
