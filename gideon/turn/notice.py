"""Build the abstention notice a person reads.

The service composes this product text from fixed sentences, the pinned
generation's lockfile labels, and the court file's names, never from the
message or a model's output.
"""

from collections.abc import Sequence
from typing import Final

_OPENING: Final = "Legal found nothing in GIDEON's corpus that supports an answer to this question."
_SEARCHED: Final = "Searched: case law as of {labels}."
_OUT_OF_CORPUS_COURT: Final = "The court you named, {court}, is outside GIDEON's corpus."
_CLOSING: Final = "Rephrasing may help. A question about a source GIDEON does not hold cannot be answered here."


def abstention_notice(lockfiles: Sequence[str], out_of_corpus_courts: Sequence[str]) -> str:
    """Render the notice with the searched lockfile labels and supplied court names."""

    if not lockfiles:
        raise ValueError("at least one lockfile is required")

    return "\n\n".join((
        _OPENING,
        _SEARCHED.format(labels=", ".join(lockfiles)),
        *(_OUT_OF_CORPUS_COURT.format(court=court) for court in out_of_corpus_courts),
        _CLOSING,
    ))
