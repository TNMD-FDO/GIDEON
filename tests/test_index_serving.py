"""Select a live generation and address both stores by its name."""

import unittest
from dataclasses import dataclass

from gideon.host.index.serving import (
    AmbiguousGeneration,
    ServingGeneration,
    StoreNames,
    select_serving,
    store_names,
)


@dataclass(frozen=True)
class Row:
    gen_id: str
    collection: str


LIVE = Row("fictitious-caselaw-generation", "caselaw")
OTHER = Row("fictitious-other-generation", "other")


class Selection(unittest.TestCase):
    """Only the requested collection's sole live row can serve a turn."""

    def test_no_live_row_for_the_collection_is_unavailable(self) -> None:
        for rows in ((), (OTHER,)):
            with self.subTest(rows=rows):
                self.assertIsNone(select_serving(rows, "caselaw"))

    def test_one_matching_row_is_selected_among_other_collections(self) -> None:
        rows = iter((OTHER, LIVE))
        self.assertEqual(select_serving(rows, LIVE.collection), ServingGeneration(LIVE.gen_id, LIVE.collection))

    def test_multiple_matching_rows_report_the_ambiguous_collection(self) -> None:
        second = Row("fictitious-second-caselaw-generation", LIVE.collection)
        for rows in ((LIVE, second), (LIVE, LIVE)):
            with self.subTest(rows=rows):
                self.assertEqual(select_serving(rows, LIVE.collection), AmbiguousGeneration(LIVE.collection))

    def test_both_store_addresses_are_the_generation_name(self) -> None:
        self.assertEqual(
            store_names(ServingGeneration(LIVE.gen_id, LIVE.collection)),
            StoreNames(LIVE.gen_id, LIVE.gen_id),
        )
