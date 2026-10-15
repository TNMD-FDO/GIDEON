"""Load the eval set's gold documents and build their sample on the CI sibling."""

import argparse
from pathlib import Path

from gideon.evaluation import evalset
from gideon.host.index import build, sample


def gold_documents(loaded: evalset.LoadedSet) -> tuple[sample.GoldDocument, ...]:
    """Collect sorted gold ids, retaining judged hashes over must-cite-only ids."""

    documents = {
        judgment.source_id: sample.GoldDocument(judgment.source_id, judgment.sha256)
        for judgment in loaded.judgments
    }
    for signoff in loaded.signoffs:
        for identifier in signoff.must_cite:
            documents.setdefault(identifier, sample.GoldDocument(identifier, None))
    return tuple(documents[identifier] for identifier in sorted(documents))


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
