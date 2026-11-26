"""Select a serving generation from live rows for one turn.

Address every store query in that turn through ``store_names``, never through
an alias. A promotion after selection leaves the turn's generation
unchanged; a retired generation answers until ``index gc`` drops it.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol


class _ServingRow(Protocol):
    """The fields needed from a live generation row."""

    @property
    def gen_id(self) -> str: ...

    @property
    def collection(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ServingGeneration:
    """The generation and collection selected for one turn."""

    gen_id: str
    collection: str


@dataclass(frozen=True, slots=True)
class AmbiguousGeneration:
    """The collection has more than one live row."""

    collection: str


@dataclass(frozen=True, slots=True)
class StoreNames:
    """The explicit generation addresses for both derived stores."""

    lexical_index: str
    dense_collection: str


def select_serving(
    rows: Iterable[_ServingRow], collection: str,
) -> ServingGeneration | None | AmbiguousGeneration:
    """Select the collection's sole live generation and flag multiple matches."""

    matching = [row for row in rows if row.collection == collection]
    if not matching:
        return None
    if len(matching) > 1:
        return AmbiguousGeneration(collection)
    return ServingGeneration(matching[0].gen_id, collection)


def store_names(generation: ServingGeneration) -> StoreNames:
    """Address both stores by the selected generation's own name."""

    return StoreNames(generation.gen_id, generation.gen_id)
