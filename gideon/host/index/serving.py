"""Pin a turn to one generation by reading the live record once per turn.

Address every store query in that turn through ``store_names``, never through
an alias. A promotion between the read and a query leaves the turn's generation
unchanged; a retired generation answers until ``index gc`` drops it.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from gideon.host import report
from gideon.host.index import record
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike


@dataclass(frozen=True, slots=True)
class ServingGeneration:
    """The generation and collection selected for one turn."""

    gen_id: str
    collection: str


@dataclass(frozen=True, slots=True)
class StoreNames:
    """The explicit generation addresses for both derived stores."""

    lexical_index: str
    dense_collection: str


def select_serving(
    rows: Sequence[record.PromotedRow], collection: str,
) -> ServingGeneration | None | Problem:
    """Select the collection's sole live generation, or refuse an ambiguous record."""

    matching = [row for row in rows if row.collection == collection]
    if not matching:
        return None
    if len(matching) > 1:
        return Problem(
            f"collection {collection} has multiple live generations",
            f"Inspect the Postgres logs and restore a single live generation for {collection}, "
            f"then run {report.command('index report')} again.",
        )
    return ServingGeneration(matching[0].gen_id, collection)


def read_serving(
    host: Host, rendered_dir: PathLike, collection: str, *, command_path: str,
) -> ServingGeneration | None | Problem:
    """Read the promoted rows once and select the turn's generation."""

    rows = record.read_promoted(host, rendered_dir, command_path=command_path)
    if isinstance(rows, Problem):
        return rows
    return select_serving(rows, collection)


def store_names(generation: ServingGeneration) -> StoreNames:
    """Address both stores by the selected generation's own name."""

    return StoreNames(generation.gen_id, generation.gen_id)
