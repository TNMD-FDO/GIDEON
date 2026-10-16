"""Load the eval set's gold documents and build their sample on the CI sibling."""

import argparse
from collections.abc import Iterator, Mapping
from pathlib import Path

from gideon.evaluation import evalset
from gideon.host.index import build, sample


def _gold_sources(loaded: evalset.LoadedSet) -> Iterator[tuple[str, str | None]]:
    """Walk judged ids and hashes, then every sign-off's unversioned must-cite ids."""

    for judgment in loaded.judgments:
        yield judgment.source_id, judgment.sha256
    for signoff in loaded.signoffs:
        for identifier in signoff.must_cite:
            yield identifier, None


def gold_documents(loaded: evalset.LoadedSet) -> tuple[sample.GoldDocument, ...]:
    """Collect sorted gold ids, retaining judged hashes over must-cite-only ids."""

    documents: dict[str, sample.GoldDocument] = {}
    for identifier, sha256 in _gold_sources(loaded):
        if sha256 is not None:
            documents[identifier] = sample.GoldDocument(identifier, sha256)
        else:
            documents.setdefault(identifier, sample.GoldDocument(identifier, None))
    return tuple(documents[identifier] for identifier in sorted(documents))


def gold_hashes(loaded: evalset.LoadedSet) -> Mapping[str, frozenset[str]]:
    """Collect sorted gold ids and every judged hash, empty for must-cite-only ids."""

    hashes: dict[str, set[str]] = {}
    for identifier, sha256 in _gold_sources(loaded):
        values = hashes.setdefault(identifier, set())
        if sha256 is not None:
            values.add(sha256)
    return {identifier: frozenset(hashes[identifier]) for identifier in sorted(hashes)}


def run_sample_build(args: argparse.Namespace) -> int:
    """Refuse eval-set findings before passing gold and fill inputs to the build."""

    root = Path(__file__).parents[2]
    result = evalset.load_set(root / evalset.SET_ROOT)
    if result.findings:
        evalset.print_findings(result.findings)
        return 1
    assert result.loaded is not None
    selection = sample.SampleInputs(
        gold_documents(result.loaded), sample.SAMPLE_FILL_PER_MILLION, result.loaded.version,
    )
    return build.run_index_build(args, sample=selection)
