"""Registered corpus pipelines and their fixed chunk and parser inputs.

Every entry's tokenizer and eyecite pins must match the current locks. A bump
to either retires affected entries and registers a new version in the same
change. Changes to the parser walk, segmenter, chunker rules, or token counter
construction also require a new version, since a chunk's identity and offsets
must mean one thing under one version; a version is never registered again with
different values. E11's arms register separate versions that share these pins.
A segmenter change registers a new version beside the old, whose entry stays
while a committed lockfile or a recorded generation names it.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final


@dataclass(frozen=True, slots=True)
class TokenizerPin:
    """Embedding tokenizer repository, revision, and file digests."""

    repository: str
    revision: str
    files: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Pipeline:
    """One immutable chunking version and the inputs that define it."""

    version: str
    chunk_target: int
    chunk_maximum: int
    chunk_minimum: int
    tokenizer: TokenizerPin
    eyecite_version: str


VERSION_PATTERN: Final = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
PIPELINES: Final[Mapping[str, Pipeline]] = MappingProxyType({
    "1.0.0": Pipeline(
        version="1.0.0",
        chunk_target=512,  # E11: a starting value; a new target is a new version.
        chunk_maximum=1024,  # exempt: the stage-1 reranker's input cap
        chunk_minimum=64,  # E11: a packing rule's starting value, never a floor.
        tokenizer=TokenizerPin(
            repository="Qwen/Qwen3-Embedding-8B",
            revision="1d8ad4ca9b3dd8059ad90a75d4983776a23d44af",
            files=(
                ("tokenizer.json", "sha256:83cdf8c3a34f68862319cb1810ee7b1e2c0a44e0864ae930194ddb76bb7feb8d"),
                ("vocab.json", "sha256:ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910"),
                ("merges.txt", "sha256:8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5"),
            ),
        ),
        eyecite_version="2.7.8",
    ),
    # The segmenter changed; every pin and size is 1.0.0's.
    "1.1.0": Pipeline(
        version="1.1.0",
        chunk_target=512,  # E11: a starting value; a new target is a new version.
        chunk_maximum=1024,  # exempt: the stage-1 reranker's input cap
        chunk_minimum=64,  # E11: a packing rule's starting value, never a floor.
        tokenizer=TokenizerPin(
            repository="Qwen/Qwen3-Embedding-8B",
            revision="1d8ad4ca9b3dd8059ad90a75d4983776a23d44af",
            files=(
                ("tokenizer.json", "sha256:83cdf8c3a34f68862319cb1810ee7b1e2c0a44e0864ae930194ddb76bb7feb8d"),
                ("vocab.json", "sha256:ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910"),
                ("merges.txt", "sha256:8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5"),
            ),
        ),
        eyecite_version="2.7.8",
    ),
})
PIPELINE_VERSION: Final[str] = "1.1.0"


def tokenizer_paths(pin: TokenizerPin, root: Path) -> tuple[Path, ...]:
    """Locate the pinned files in the weights tree's hub snapshot."""

    owner, name = pin.repository.split("/", 1)
    snapshot = root / "hub" / f"models--{owner}--{name}" / "snapshots" / pin.revision
    return tuple(snapshot / filename for filename, _digest in pin.files)
